"""Tests for gate_campaign: slot-registry parse, the stop-rule order (red gate 1, wall 2, forbidden
log line 3; a failed or timed-out log fetch is a hit), the campaign.jsonl records and kept
artifacts, the run-through of extra run_full_suite args, and the `status` read."""

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import gate_campaign  # noqa: E402

HEADER = ("| Slot | Status | Story ID | Branch | Worktree | Since |\n"
          "|------|--------|----------|--------|----------|-------|\n")


class FakeRun:
    """Stands in for subprocess.run. Dispatches on argv[0]; anything else fails the test, so no
    real gate, docker or git process can start from this file."""

    def __init__(self, gate_rcs, logs="", logs_rc=0, logs_timeout=False, console="",
                 tmp_dir=None):
        self.gate_rcs = list(gate_rcs)
        self.logs, self.logs_rc, self.logs_timeout = logs, logs_rc, logs_timeout
        self.console, self.tmp_dir = console, tmp_dir
        self.gate_calls: list[list[str]] = []
        self.logs_calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        if argv[0] == sys.executable:
            self.gate_calls.append(list(argv))
            n = len(self.gate_calls)
            if self.tmp_dir is not None:  # the raw log run_full_suite writes to the temp dir
                (Path(self.tmp_dir) / f"full-suite-story-x-{n}.log").write_text(f"phase log {n}",
                                                                              encoding="utf-8")
            console = self.console.format(n=n)
            return subprocess.CompletedProcess(argv, self.gate_rcs.pop(0), console, "")
        if "logs" in argv:
            self.logs_calls.append(list(argv))
            if self.logs_timeout:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            return subprocess.CompletedProcess(argv, self.logs_rc, self.logs, "")
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "abcdef0123456789\n", "")
        raise AssertionError(f"unexpected subprocess: {argv}")


class CampaignCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.worktree = self.root / "wt"
        self.results = self.worktree / "test-results" / "frontend" / "test-results"
        self.results.mkdir(parents=True)
        (self.results / "results.xml").write_text("<testsuites/>", encoding="utf-8")
        self.tmp_dir = self.root / "systemp"
        self.tmp_dir.mkdir()
        self.out = self.root / "campaign"
        te_root = te.enter(self, '[stack]\nruntime = "docker-compose"\nservices = ["backend", "worker"]\n'
                                 '[tests]\nartifact_dirs = ["test-results/frontend/test-results"]\n')
        self.registry = te_root / "docs" / "implementation-artifacts" / "slot-registry.md"
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(
            HEADER
            + f"| 1 | in_use | ab-37 | story/ab-37 | {self.worktree.as_posix()} | 2026-10-07 |\n"
            + "| 2 | in_use | ab-9 | story/ab-9 | \u2014 | 2026-10-07 |\n"
            + "| 3 | free | \u2014 | \u2014 | \u2014 | \u2014 |\n",
            encoding="utf-8")
        for patcher in (mock.patch.object(gate_campaign.tempfile, "gettempdir",
                                          return_value=str(self.tmp_dir)),):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def args(self, runs=2, max_wall_s=0, forbid=(), extra=()):
        return argparse.Namespace(story_id="ab-37", runs=runs, max_wall_s=max_wall_s,
                                  forbid=list(forbid), out=str(self.out), extra=list(extra))

    def run_campaign(self, fake: FakeRun, walls, **kw) -> tuple[int, str]:
        clock = []
        for w in walls:
            clock += [0.0, float(w)]
        out = io.StringIO()
        with mock.patch.object(gate_campaign.subprocess, "run", fake), \
                mock.patch.object(gate_campaign, "time") as fake_time, contextlib.redirect_stdout(out):
            fake_time.monotonic.side_effect = clock
            rc = gate_campaign.cmd_run(self.args(**kw))
        return rc, out.getvalue()

    def records(self) -> list[dict]:
        ledger = self.out / "campaign.jsonl"
        return [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines()]


class RegistryTests(CampaignCase):
    def test_worktree_from_registry_row(self):
        self.assertEqual(gate_campaign._worktree("ab-37"), self.worktree)

    def test_unknown_story_is_a_setup_error_not_a_red_gate(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gate_campaign._worktree("ab-3")  # a prefix of ab-37 must not match its row
        self.assertEqual(cm.exception.code, gate_campaign.SETUP_ERROR)

    def test_em_dash_worktree_is_a_setup_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gate_campaign._worktree("ab-9")
        self.assertEqual(cm.exception.code, gate_campaign.SETUP_ERROR)


class StopRuleTests(CampaignCase):
    def test_all_runs_good_exits_zero_with_one_record_each(self):
        fake = FakeRun([0, 0, 0], logs="all fine\n", console="log: {n}", tmp_dir=self.tmp_dir)
        rc, out = self.run_campaign(fake, [100, 110, 120], runs=3, max_wall_s=500,
                                    forbid=["Traceback"])
        self.assertEqual(rc, 0)
        self.assertIn("ALLDONE 3/3", out)
        recs = self.records()
        self.assertEqual([r["label"] for r in recs], ["run1", "run2", "run3"])
        self.assertEqual([r["wall_s"] for r in recs], [100, 110, 120])
        self.assertEqual(recs[0]["rc"], 0)
        self.assertEqual(recs[0]["tree"], "abcdef012345")
        self.assertEqual(recs[0]["forbid_hits"], {"Traceback": 0})
        self.assertFalse((self.out / "running.json").exists())

    def test_red_gate_wins_over_wall_and_forbid(self):
        fake = FakeRun([1, 0], logs="Traceback (most recent call last)\n")
        rc, out = self.run_campaign(fake, [900], max_wall_s=500, forbid=["Traceback"])
        self.assertEqual(rc, 1)
        self.assertIn("STOP: red gate rc=1", out)
        self.assertEqual(len(fake.gate_calls), 1)  # run2 never starts
        self.assertEqual(len(self.records()), 1)
        self.assertFalse((self.out / "running.json").exists())

    def test_wall_over_budget_wins_over_forbid(self):
        fake = FakeRun([0, 0], logs="Traceback\n")
        rc, out = self.run_campaign(fake, [501], max_wall_s=500, forbid=["Traceback"])
        self.assertEqual(rc, 2)
        self.assertIn("wall 501s > --max-wall-s 500", out)
        self.assertEqual(len(fake.gate_calls), 1)

    def test_wall_at_budget_is_not_over(self):
        fake = FakeRun([0])
        rc, _ = self.run_campaign(fake, [500], runs=1, max_wall_s=500)
        self.assertEqual(rc, 0)

    def test_forbidden_log_line_exits_three(self):
        fake = FakeRun([0, 0], logs="x\nKeyError: 'nodes'\nKeyError: 'nodes'\n")
        rc, out = self.run_campaign(fake, [100], max_wall_s=500,
                                    forbid=["KeyError: 'nodes'", "Traceback"])
        self.assertEqual(rc, 3)
        self.assertEqual(self.records()[0]["forbid_hits"], {"KeyError: 'nodes'": 2, "Traceback": 0})
        self.assertIn("forbidden log lines", out)
        self.assertEqual(len(fake.gate_calls), 1)
        argv = fake.logs_calls[0]
        self.assertEqual(argv[argv.index("-p") + 1], "story-ab-37")
        self.assertEqual(argv[-2:], ["backend", "worker"])

    def test_log_fetch_timeout_counts_as_a_hit(self):
        fake = FakeRun([0, 0], logs_timeout=True)
        rc, _ = self.run_campaign(fake, [100], forbid=["Traceback"])
        self.assertEqual(rc, 3)
        self.assertEqual(self.records()[0]["forbid_hits"], {"<compose logs timed out>": 1})

    def test_log_fetch_failure_counts_as_a_hit(self):
        fake = FakeRun([0, 0], logs_rc=1)
        rc, _ = self.run_campaign(fake, [100], forbid=["Traceback"])
        self.assertEqual(rc, 3)
        self.assertEqual(self.records()[0]["forbid_hits"], {"<compose logs exit 1>": 1})

    def test_no_forbid_patterns_skips_the_log_fetch(self):
        fake = FakeRun([0])
        rc, _ = self.run_campaign(fake, [100], runs=1)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.logs_calls, [])

    def test_invalid_regex_and_zero_runs_stop_before_any_gate(self):
        for kw in ({"forbid": ["("]}, {"runs": 0}):
            fake = FakeRun([])
            with contextlib.redirect_stderr(io.StringIO()):
                rc, _ = self.run_campaign(fake, [], **kw)
            self.assertEqual(rc, gate_campaign.SETUP_ERROR, kw)
            self.assertEqual(fake.gate_calls, [])


class ArtifactAndArgvTests(CampaignCase):
    def test_keeps_console_phase_log_and_playwright_results(self):
        console = "summary\nlog: " + str(self.tmp_dir) + "/full-suite-story-x-{n}.log\n"
        fake = FakeRun([0], console=console, tmp_dir=self.tmp_dir)
        rc, _ = self.run_campaign(fake, [100], runs=1)
        self.assertEqual(rc, 0)
        art = "art-run1-test-results_frontend_test-results"
        self.assertEqual(self.records()[0]["kept"], ["run1.full-suite-story-x-1.log", art + "/"])
        self.assertEqual((self.out / "run1.full-suite-story-x-1.log").read_text(encoding="utf-8"), "phase log 1")
        self.assertIn("summary", (self.out / "run1.out").read_text(encoding="utf-8"))
        self.assertTrue((self.out / art / "results.xml").exists())

    def test_cli_passes_args_after_double_dash_to_the_worktree_runner(self):
        fake = FakeRun([0])
        argv = ["run", "--story-id", "ab-37", "--runs", "1",
                "--out", str(self.out), "--forbid", "A", "--", "--record"]
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")  # main() reconfigures stdout
        with mock.patch.object(gate_campaign.subprocess, "run", fake), \
                mock.patch.object(gate_campaign, "time") as fake_time, \
                mock.patch.object(sys, "stdout", stdout):
            fake_time.monotonic.side_effect = [0.0, 1.0]
            self.assertEqual(gate_campaign.main(argv), 0)
        self.assertEqual(fake.gate_calls[0], [
            sys.executable, str(gate_campaign.SCRIPTS_DIR / "run_full_suite.py"),
            "--story-id", "ab-37", "--record"])

    def test_cli_usage_error_is_a_setup_error(self):
        with mock.patch.object(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8")), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gate_campaign.main(["run", "--story-id", "ab-37"])
        self.assertEqual(cm.exception.code, gate_campaign.SETUP_ERROR)


class StatusTests(unittest.TestCase):
    def status(self, out_dir: Path) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(gate_campaign.cmd_status(argparse.Namespace(out=str(out_dir))), 0)
        return buf.getvalue()

    def test_empty_dir_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.status(Path(tmp)), "")

    def test_records_and_run_in_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            recs = [{"label": "run1", "rc": 0, "wall_s": 480, "tree": "abc123", "forbid_hits": {}},
                    {"label": "run2", "rc": 0, "wall_s": 510, "tree": "abc123",
                     "forbid_hits": {"Traceback": 0}}]
            (d / "campaign.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs),
                                              encoding="utf-8")
            started = datetime.now(timezone.utc) - timedelta(seconds=120)
            (d / "running.json").write_text(
                json.dumps({"label": "run3", "started": started.isoformat()}), encoding="utf-8")
            lines = self.status(d).splitlines()
        self.assertEqual(lines[0], "run1 rc=0 wall=480s tree=abc123 forbid={}")
        self.assertEqual(lines[1], "run2 rc=0 wall=510s tree=abc123 forbid={'Traceback': 0}")
        self.assertIn("in progress: run3 for ", lines[2])
        age = int(lines[2].split(" for ")[1].rstrip("s"))
        self.assertGreaterEqual(age, 120)
        self.assertLess(age, 180)


if __name__ == "__main__":
    unittest.main()
