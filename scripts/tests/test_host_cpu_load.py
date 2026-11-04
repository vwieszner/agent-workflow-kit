"""Unit tests for ``scripts/host_cpu_load.py``.

No test generates load: the busy loops are exercised only on paths that stop at once (deadline
already past, stop-file already present) and the worker processes are replaced by fakes. The tests
cover the paths a measurement session cannot exercise on demand: argument validation, the
stop-file refusal, the two stop conditions in the parent and in each worker, and the heartbeat.

Usage
-----
    python3 -m unittest discover -s scripts/tests -p test_host_cpu_load.py
"""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Make ``scripts`` importable regardless of where the tests are invoked from.
sys.path.insert(0, str(REPO_ROOT))

load = importlib.import_module("scripts.host_cpu_load")


class _FakeWorker:
    """Stands in for a ``multiprocessing.Process``: alive until ``terminate`` or ``stop_on_join``."""

    def __init__(self, exits_on_join: bool = True):
        self.alive = True
        self.exits_on_join = exits_on_join
        self.terminated = False

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        if self.exits_on_join:
            self.alive = False

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False


def _forbid_spawn(*args, **kwargs):
    raise AssertionError(f"no worker may be started on this path: {args}")


def _run(argv: list[str], spawn=_forbid_spawn) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(load, "_spawn_workers", spawn), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = load.main(argv)
    return rc, out.getvalue(), err.getvalue()


class ArgumentValidationTests(unittest.TestCase):
    def test_workers_is_required(self):
        rc, _, err = _run(["--max-seconds", "5"])
        self.assertEqual(rc, 2)
        self.assertIn("--workers", err)

    def test_max_seconds_is_required(self):
        rc, _, err = _run(["--workers", "2"])
        self.assertEqual(rc, 2)
        self.assertIn("--max-seconds", err)

    def test_non_positive_workers_refused(self):
        for value in ("0", "-3"):
            with self.subTest(workers=value):
                rc, _, err = _run(["--workers", value, "--max-seconds", "5"])
                self.assertEqual(rc, 2)
                self.assertIn("--workers must be > 0", err)

    def test_non_positive_max_seconds_refused(self):
        for value in ("0", "-1", "-0.5"):
            with self.subTest(max_seconds=value):
                rc, _, err = _run(["--workers", "2", "--max-seconds", value])
                self.assertEqual(rc, 2)
                self.assertIn("--max-seconds must be > 0", err)

    def test_non_positive_interval_refused(self):
        rc, _, err = _run(["--workers", "2", "--max-seconds", "5", "--interval", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("--interval must be > 0", err)

    def test_non_finite_max_seconds_refused(self):
        for value in ("nan", "inf"):
            with self.subTest(max_seconds=value):
                rc, _, err = _run(["--workers", "2", "--max-seconds", value])
                self.assertEqual(rc, 2)
                self.assertIn("--max-seconds must be a finite number", err)

    def test_non_finite_interval_refused(self):
        for value in ("nan", "inf"):
            with self.subTest(interval=value):
                rc, _, err = _run(["--workers", "2", "--max-seconds", "5", "--interval", value])
                self.assertEqual(rc, 2)
                self.assertIn("--interval must be a finite number", err)

    def test_non_numeric_workers_refused(self):
        rc, _, err = _run(["--workers", "many", "--max-seconds", "5"])
        self.assertEqual(rc, 2)
        self.assertIn("--workers", err)

    def test_existing_stop_file_refused_before_any_worker_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "load.stop"
            stop.write_text("")
            rc, _, err = _run(["--workers", "2", "--max-seconds", "5", "--stop-file", str(stop)])
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)
        self.assertIn(str(stop), err)


class StopConditionTests(unittest.TestCase):
    def test_should_stop_names_the_deadline(self):
        self.assertEqual(load.should_stop(100.0, None, now=100.0), "max-seconds")
        self.assertEqual(load.should_stop(100.0, None, now=101.0), "max-seconds")

    def test_should_stop_is_none_before_the_deadline_without_a_stop_file(self):
        self.assertIsNone(load.should_stop(100.0, None, now=99.9))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load.should_stop(100.0, str(Path(tmp) / "absent.stop"), now=99.9))

    def test_should_stop_names_the_stop_file_when_it_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "load.stop"
            self.assertIsNone(load.should_stop(100.0, str(stop), now=50.0))
            stop.write_text("")
            self.assertEqual(load.should_stop(100.0, str(stop), now=50.0), "stop-file")

    def test_worker_returns_at_once_when_the_deadline_has_passed(self):
        t0 = time.monotonic()
        reason = load.burn(time.time() - 1.0, None)
        self.assertEqual(reason, "max-seconds")
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_worker_returns_at_once_when_the_stop_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "load.stop"
            stop.write_text("")
            t0 = time.monotonic()
            reason = load.burn(time.time() + 600.0, str(stop))
            elapsed = time.monotonic() - t0
        self.assertEqual(reason, "stop-file")
        self.assertLess(elapsed, 2.0)

    def test_worker_entry_point_runs_the_same_loop(self):
        t0 = time.monotonic()
        load.worker_main(time.time() - 1.0, "")
        self.assertLess(time.monotonic() - t0, 2.0)


class ParentLoopTests(unittest.TestCase):
    def test_stops_at_max_seconds_and_reaps_every_worker(self):
        workers = [_FakeWorker(), _FakeWorker()]
        t0 = time.monotonic()
        rc, out, _ = _run(["--workers", "2", "--max-seconds", "0.4", "--interval", "0.1"],
                          spawn=lambda n, deadline, stop: workers)
        self.assertEqual(rc, 0)
        self.assertLess(time.monotonic() - t0, 10.0)
        self.assertIn("reason=max-seconds", out)
        self.assertFalse(any(w.alive for w in workers))

    def test_stops_when_the_stop_file_appears(self):
        workers = [_FakeWorker()]
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "load.stop"
            timer = threading.Timer(0.3, stop.write_text, args=("",))
            timer.start()
            t0 = time.monotonic()
            try:
                rc, out, _ = _run(["--workers", "1", "--max-seconds", "60", "--interval", "0.1",
                                   "--stop-file", str(stop)],
                                  spawn=lambda n, deadline, stop_file: workers)
            finally:
                timer.cancel()
            elapsed = time.monotonic() - t0
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 10.0)
        self.assertIn("reason=stop-file", out)

    def test_a_worker_that_outlives_the_join_is_terminated(self):
        stuck = _FakeWorker(exits_on_join=False)
        rc, _, _ = _run(["--workers", "1", "--max-seconds", "0.2", "--interval", "0.1"],
                        spawn=lambda n, deadline, stop: [stuck])
        self.assertEqual(rc, 0)
        self.assertTrue(stuck.terminated)

    def test_a_lost_worker_ends_the_run_with_exit_3_and_a_stderr_line(self):
        gone = _FakeWorker()
        gone.alive = False
        workers = [_FakeWorker(), gone]
        t0 = time.monotonic()
        rc, out, err = _run(["--workers", "2", "--max-seconds", "60", "--interval", "0.1"],
                            spawn=lambda n, deadline, stop: workers)
        self.assertEqual(rc, 3)
        self.assertLess(time.monotonic() - t0, 10.0)
        self.assertIn("WORKER LOST workers=2 alive=1", err)
        self.assertIn("reason=worker-lost", out)
        self.assertFalse(any(w.alive for w in workers))

    def test_a_worker_exiting_on_the_stop_file_is_not_a_lost_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "load.stop"

            class _ExitsOnStopFile(_FakeWorker):
                def is_alive(self):
                    stop.write_text("")
                    self.alive = False
                    return False

            rc, out, err = _run(["--workers", "1", "--max-seconds", "60", "--interval", "0.1",
                                 "--stop-file", str(stop)],
                                spawn=lambda n, deadline, stop_file: [_ExitsOnStopFile()])
        self.assertEqual(rc, 0)
        self.assertNotIn("WORKER LOST", err)
        self.assertIn("reason=stop-file", out)

    def test_workers_started_before_a_spawn_failure_are_reaped(self):
        started = _FakeWorker(exits_on_join=False)

        def spawn(n, deadline, stop_file):
            yield started
            raise OSError("process creation failed")

        with self.assertRaises(OSError):
            _run(["--workers", "2", "--max-seconds", "60", "--interval", "0.1"], spawn=spawn)
        self.assertTrue(started.terminated)

    def test_workers_receive_the_count_the_absolute_deadline_and_the_stop_file(self):
        seen = {}

        def spawn(n, deadline, stop_file):
            seen.update(n=n, deadline=deadline, stop_file=stop_file)
            return [_FakeWorker() for _ in range(n)]

        before = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            stop = str(Path(tmp) / "x.stop")
            _run(["--workers", "3", "--max-seconds", "0.2", "--interval", "0.1", "--stop-file", stop],
                 spawn=spawn)
        self.assertEqual(seen["n"], 3)
        self.assertEqual(seen["stop_file"], stop)
        self.assertGreaterEqual(seen["deadline"], before + 0.2)
        self.assertLess(seen["deadline"], before + 5.0)

    def test_heartbeat_line_is_timestamped_and_names_workers_and_remaining(self):
        workers = [_FakeWorker(), _FakeWorker()]
        _, out, _ = _run(["--workers", "2", "--max-seconds", "0.5", "--interval", "0.1"],
                         spawn=lambda n, deadline, stop: workers)
        beats = [line for line in out.splitlines() if "alive=" in line]
        self.assertTrue(beats, out)
        for line in beats:
            self.assertRegex(line, r"^\[\d\d:\d\d:\d\d\] ")
            self.assertRegex(line, r"workers=2 alive=2 remaining=\d+(\.\d+)?s")

    def test_start_line_names_the_setting_and_the_limits(self):
        workers = [_FakeWorker()]
        _, out, _ = _run(["--workers", "1", "--max-seconds", "0.2", "--interval", "0.1"],
                         spawn=lambda n, deadline, stop: workers)
        first = out.splitlines()[0]
        self.assertRegex(first, r"^\[\d\d:\d\d:\d\d\] host_cpu_load start: workers=1 max-seconds=0\.2")


class SpawnTests(unittest.TestCase):
    def test_spawn_workers_starts_one_process_per_worker_with_the_worker_entry(self):
        started = []

        class _Proc:
            def __init__(self, target, args, daemon):
                self.target, self.args, self.daemon = target, args, daemon

            def start(self):
                started.append(self)

        ctx = mock.Mock()
        ctx.Process.side_effect = lambda target, args, daemon: _Proc(target, args, daemon)
        with mock.patch.object(load.multiprocessing, "get_context", return_value=ctx) as get_ctx:
            procs = list(load._spawn_workers(3, 123.5, "s.stop"))
        get_ctx.assert_called_once_with("spawn")
        self.assertEqual(len(procs), 3)
        self.assertEqual(len(started), 3)
        for proc in started:
            self.assertIs(proc.target, load.worker_main)
            self.assertEqual(proc.args, (123.5, "s.stop"))
            self.assertFalse(proc.daemon)

    def test_no_stop_file_reaches_workers_as_an_empty_string(self):
        seen = {}

        def spawn(n, deadline, stop_file):
            seen["stop_file"] = stop_file
            return [_FakeWorker()]

        _run(["--workers", "1", "--max-seconds", "0.2", "--interval", "0.1"], spawn=spawn)
        self.assertEqual(seen["stop_file"], "")


if __name__ == "__main__":
    unittest.main()
