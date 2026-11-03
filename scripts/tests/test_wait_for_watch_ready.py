"""wait_for_watch_ready.py: wait on a compose-watch output file for `Watch enabled`, fail closed on a failure line.

Pins: a `Watch enabled` line exits 0; the stale-lock line exits 1 with its remedy even when an exited line follows it
(the first match decides) and the deciding line is printed once; the exited line exits 1; a file with neither exits
2 at the timeout with its last lines, a line containing `error` in build output not counting; a line split across
two polls is matched once it is whole; a file that shrinks is read from its start and the lines kept from before it
shrank are dropped. Fixtures: `fixtures/watch_ready/*.output`. The clock is faked, so no test sleeps.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import wait_for_watch_ready as wfr  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "watch_ready"
LOCK_LINE = 'cannot take exclusive lock for project "story-1-2": process with PID 37980 is still running'
EXITED_LINE = "docker compose watch exited with code 17"


class FakeClock:
    """`time.monotonic` / `time.sleep` stand-ins: a sleep advances the clock instead of waiting."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class WaitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="watch-ready-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_main(self, path: Path, *extra: str) -> tuple[int, str, FakeClock]:
        clock, out = FakeClock(), io.StringIO()
        with mock.patch.object(wfr.time, "monotonic", clock.monotonic), \
                mock.patch.object(wfr.time, "sleep", clock.sleep), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = wfr.main([str(path), *extra])
        return rc, out.getvalue(), clock


class MainTests(WaitCase):
    def test_a_watch_enabled_line_exits_0(self):
        path = self.tmp / "ready.output"
        path.write_bytes((FIXTURES / "building.output").read_bytes() + b" Watch enabled\n")
        rc, out, clock = self.run_main(path)
        self.assertEqual(rc, 0, out)
        self.assertIn("READY: Watch enabled", out)
        self.assertEqual(clock.slept, [])

    def test_the_lock_line_wins_over_a_later_exited_line_and_is_printed_once(self):
        rc, out, _ = self.run_main(FIXTURES / "lock.output")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"FAILED: {LOCK_LINE}", out)
        self.assertEqual(out.count(LOCK_LINE), 1, out)
        self.assertIn("remedy: stale compose-watch lock", out)
        self.assertNotIn("exited with code", out)
        self.assertIn("API_KEY", out)

    def test_the_exited_line_exits_1_printed_once_without_a_remedy(self):
        rc, out, _ = self.run_main(FIXTURES / "exited.output")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"FAILED: {EXITED_LINE}", out)
        self.assertEqual(out.count(EXITED_LINE), 1, out)
        self.assertNotIn("remedy:", out)
        self.assertIn("  | #1 DONE 0.0s", out)

    def test_neither_line_times_out_with_exit_2_and_the_last_lines(self):
        rc, out, clock = self.run_main(FIXTURES / "building.output", "--timeout-sec", "12", "--poll-sec", "5")
        self.assertEqual(rc, 2, out)
        self.assertIn("TIMEOUT after 12s", out)
        self.assertNotIn("never appeared", out)
        self.assertIn("  | #21 ERROR-free line naming error in a package path: site-packages/errorhandler", out)
        self.assertEqual(clock.slept, [5, 5, 2])
        self.assertEqual(out.count("still waiting on compose watch"), 3)

    def test_a_file_that_never_appears_times_out(self):
        rc, out, _ = self.run_main(self.tmp / "missing.output", "--timeout-sec", "3", "--poll-sec", "1")
        self.assertEqual(rc, 2, out)
        self.assertIn("(the file never appeared)", out)

    def test_bad_arguments_exit_3(self):
        for extra in (("--timeout-sec", "541"), ("--timeout-sec", "0"), ("--poll-sec", "61")):
            with self.subTest(extra=extra):
                rc = None
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        wfr.main([str(self.tmp / "x.output"), *extra])
                    except SystemExit as exc:
                        rc = exc.code
                self.assertEqual(rc, 3)


class TailTests(WaitCase):
    def test_a_line_split_across_two_polls_is_matched_once_whole(self):
        path = self.tmp / "split.output"
        path.write_bytes(b" Image story-x-backend Building\n Watch en")
        tail = wfr.Tail(path)
        self.assertIsNone(tail.poll())
        with path.open("ab") as f:
            f.write(b"abled\n")
        self.assertEqual(tail.poll(), (0, " Watch enabled", ""))

    def test_a_failure_line_split_across_two_polls_is_matched(self):
        path = self.tmp / "split-fail.output"
        path.write_bytes(b"docker compose watch exi")
        tail = wfr.Tail(path)
        self.assertIsNone(tail.poll())
        with path.open("ab") as f:
            f.write(b"ted with code 1\r\n")
        self.assertEqual(tail.poll(), (1, "docker compose watch exited with code 1", ""))
        self.assertEqual(list(tail.last), [])

    def test_a_truncated_file_is_read_from_its_start_and_the_kept_lines_are_dropped(self):
        path = self.tmp / "truncated.output"
        path.write_bytes(b"".join(f"old build line {n}\n".encode() for n in range(10)))
        tail = wfr.Tail(path)
        self.assertIsNone(tail.poll())
        self.assertEqual(list(tail.last)[-1], "old build line 9")
        path.write_bytes(b"new run\n")
        self.assertIsNone(tail.poll())
        self.assertEqual(list(tail.last), ["new run"])
        self.assertEqual(tail.offset, len(b"new run\n"))
        with path.open("ab") as f:
            f.write(EXITED_LINE.encode() + b"\n")
        self.assertEqual(tail.poll(), (1, EXITED_LINE, ""))
        self.assertEqual(list(tail.last), ["new run"])


if __name__ == "__main__":
    unittest.main()
