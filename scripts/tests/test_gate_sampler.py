"""gate_sampler.py: argument refusals before any engine call; the reference container (configured
services first, then any container of the stack; a down engine is an error the startup retry keeps);
the Sink (no merged lines after a killed run, nothing written after close); summarize (appended runs,
windows, block I/O restarts) and the budgets (load over `load_budget` x CPUs, probe failures and slow
runs; `--check` exits 1); a probe run is recorded with its exit, duration and slow flag."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import gate_sampler as gs  # noqa: E402


def ps(names, rc=0):
    return subprocess.CompletedProcess([], rc, "\n".join(names), "daemon down" if rc else "")


class ArgsAndReferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, '[sampler]\nreference_services = ["cache", "proxy"]\n')

    def sample(self, *argv):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = gs.main(["sample", "--out", str(self.root / "o.jsonl"), *argv])
        return rc, err.getvalue()

    def test_bad_periods_and_an_existing_stop_file_are_refused_before_any_engine_call(self):
        with mock.patch.object(gs, "_engine", side_effect=AssertionError("engine called")):
            for flag in ("--interval", "--stats-interval", "--max-seconds"):
                self.assertEqual(self.sample(flag, "0")[0], 2)
            self.assertEqual(self.sample("--interval", "16")[0], 2)
            stop = self.root / "stop"
            stop.write_text("")
            self.assertEqual(self.sample("--stop-file", str(stop))[0], 2)
        self.assertFalse((self.root / "o.jsonl").exists())

    def test_configured_services_win_then_any_stack_container(self):
        with mock.patch.object(gs, "_engine", return_value=ps(["s-app-1", "s-proxy-1", "s-cache-1"])):
            self.assertEqual(gs.pick_reference("s"), "s-cache-1")
        with mock.patch.object(gs, "_engine", return_value=ps(["other-x-1", "s-app-1"])):
            self.assertEqual(gs.pick_reference("s"), "s-app-1")
        with mock.patch.object(gs, "_engine", return_value=ps(["other-x-1"])):
            with self.assertRaises(gs.NoReferenceContainer):
                gs.pick_reference("s")
        with mock.patch.object(gs, "_engine", return_value=ps([], rc=1)):
            with self.assertRaises(RuntimeError) as cm:
                gs.pick_reference("s")
        self.assertNotIsInstance(cm.exception, gs.NoReferenceContainer)

    def test_a_missing_stack_exits_2(self):
        with mock.patch.object(gs, "_engine", return_value=ps(["other-x-1"])):
            rc, err = self.sample("--stack", "s", "--max-seconds", "1")
        self.assertEqual(rc, 2)
        self.assertIn("no running container of stack s", err)


class SinkTests(unittest.TestCase):
    def test_unterminated_last_line_is_closed_and_writes_after_close_are_dropped(self):
        root = te.enter(self, "")
        f = root / "x.jsonl"
        f.write_text('{"kind":"start"')
        sink = gs.Sink(f)
        sink.write({"kind": "stop"})
        sink.close()
        sink.write({"kind": "late"})
        self.assertEqual(f.read_text().splitlines(), ['{"kind":"start"', '{"kind":"stop"}'])


def rec(kind, hms, **kw):
    today = datetime.now().date()
    epoch = datetime.combine(today, datetime.strptime(hms, "%H:%M:%S").time()).timestamp()
    return {"kind": kind, "epoch": epoch, "hms": hms, **kw}


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, "[sampler]\nload_budget = 1.0\n")

    def summarize(self, recs, *argv):
        f = self.root / "s.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in recs) + "\nnot json\n")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = gs.main(["summarize", str(f), *argv])
        return rc, out.getvalue()

    def test_default_window_is_the_last_run_and_budgets_are_reported(self):
        recs = [rec("start", "10:00:00"), rec("sample", "10:00:01", load1=99.0, load5=1, ncpu=4),
                rec("start", "11:00:00"),
                rec("sample", "11:00:01", load1=2.0, load5=1, ncpu=4, busy_cores=1.0, cpu_iowait_pct=0.5),
                rec("sample", "11:00:11", load1=5.0, load5=1, ncpu=4, busy_cores=3.0, cpu_iowait_pct=1.0),
                rec("probe", "11:00:05", name="db", ok=True, slow=False, dur_s=0.1, slow_s=1.0),
                rec("probe", "11:00:06", name="db", ok=False, slow=False, dur_s=5.0, exit=None, error="timed out", slow_s=1.0)]
        rc, out = self.summarize(recs)
        self.assertEqual(rc, 0)
        self.assertIn("skipped 1 unparseable line", out)
        self.assertIn("2 samples", out)                      # the 10:00 run is outside the default window
        self.assertIn("over budget (1 x 4 CPUs): 1 of 2", out)
        self.assertIn("probe db", out)
        self.assertIn("failures 1", out)
        self.assertIn("first failure 11:00:06: timed out", out)
        self.assertIn("budget: OVER", out)
        rc, _ = self.summarize(recs, "--check")
        self.assertEqual(rc, 1)

    def test_within_budget_passes_check(self):
        recs = [rec("start", "11:00:00"), rec("sample", "11:00:01", load1=1.0, load5=1, ncpu=4)]
        rc, out = self.summarize(recs, "--check")
        self.assertEqual(rc, 0)
        self.assertIn("budget: within", out)

    def test_from_selects_across_appended_runs_and_block_io_restarts_count_from_zero(self):
        recs = [rec("start", "10:00:00"), rec("sample", "10:00:01", load1=1.0, load5=1, ncpu=4),
                rec("ctr", "10:00:02", containers=[{"n": "s-app-1", "cpu": 50.0, "br": 1_000_000, "bw": 0}]),
                rec("start", "11:00:00"), rec("sample", "11:00:01", load1=1.0, load5=1, ncpu=4),
                rec("ctr", "11:00:02", containers=[{"n": "s-app-1", "cpu": 150.0, "br": 300_000, "bw": 0}])]
        rc, out = self.summarize(recs, "--from", "09:59:00", "--stack", "s")
        self.assertEqual(rc, 0)
        self.assertIn("2 samples", out)
        self.assertIn("<-- this stack", out)
        self.assertIn("blk r       0.3", out)   # the drop is a restart: 0.3 MB since it, not -0.7

    def test_no_samples(self):
        rc, out = self.summarize([rec("start", "11:00:00")])
        self.assertEqual(rc, 1)
        self.assertIn("no samples", out)


class ProbeTests(unittest.TestCase):
    def test_a_probe_run_records_exit_duration_and_slow(self):
        root = te.enter(self, "")
        sink = gs.Sink(root / "p.jsonl")
        halt = threading.Event()
        py = sys.executable
        fast = gs.ProbePoller("fast", f'"{py}" -c "print(1)"', 60, 10, 5.0, None, None, sink, halt)
        slow = gs.ProbePoller("slow", f'"{py}" -c "import time; time.sleep(0.3)"', 60, 10, 0.1, None, None, sink, halt)
        bad = gs.ProbePoller("bad", f'"{py}" -c "raise SystemExit(3)"', 60, 10, 5.0, None, None, sink, halt)
        for t in (fast, slow, bad):
            t.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and min(t.runs for t in (fast, slow, bad)) < 1:
            time.sleep(0.05)
        halt.set()
        for t in (fast, slow, bad):
            t.join(10)
        sink.close()
        rows = {r["name"]: r for r in map(json.loads, (root / "p.jsonl").read_text().splitlines())}
        self.assertTrue(rows["fast"]["ok"] and not rows["fast"]["slow"])
        self.assertTrue(rows["slow"]["slow"])
        self.assertEqual(rows["bad"]["exit"], 3)
        self.assertFalse(rows["bad"]["ok"])

    def test_configured_probes_render_templates(self):
        te.enter(self, '[sampler]\nprobes = [{ name = "q", cmd = "probe {stack}", interval_s = 2, slow_s = 0.5 }, { name = "empty", cmd = "" }]\n')
        probes = gs.configured_probes({"stack": "story-1-2", "slot": 1, "worktree": "."})
        self.assertEqual(probes, [{"name": "q", "cmd": "probe story-1-2", "interval": 2.0, "timeout": 10.0,
                                   "slow_s": 0.5}])


class ParseTests(unittest.TestCase):
    def test_vm_parse_and_split(self):
        out = ("1.50 2.00 3.00 4/500 99\ncpu  100 0 50 800 10 0 0 0 0 0\nprocs_running 3\nprocs_blocked 1\n8\n")
        vm = gs.parse_vm(out)
        self.assertEqual((vm["load1"], vm["runnable"], vm["procs_blocked"], vm["ncpu"]), (1.5, 4, 1, 8))
        split = gs.cpu_split([0] * 8, [100, 0, 50, 800, 50, 0, 0, 0], 8)
        self.assertEqual(split["cpu_busy_pct"], 15.0)
        self.assertEqual(split["cpu_iowait_pct"], 5.0)
        self.assertEqual(gs.stack_of("story-1-2-app-1"), "story-1-2")
        self.assertEqual(gs.to_bytes("1.5MiB"), int(1.5 * 1024 ** 2))


if __name__ == "__main__":
    unittest.main()
