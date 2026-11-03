"""auto_dev_driver.py: the pure parts (phase line, ledger fingerprint, progress rule, init check,
harness drift, run record), the list/stop commands over synthetic run directories, and the
foreground run against a fake `claude` CLI (POSIX only)."""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import auto_dev_driver as d  # noqa: E402

CONFIG = """
[project]
name = "demo"
[git]
base_branch = "development"
worktrees_root = "wt"
[paths]
auto_dev_failure_log = "docs/auto-dev-failures.md"
sprint_status = "docs/sprint-status.yaml"
[stack]
runtime = "none"
[loop]
required_agents = ["story-impl"]
"""


class PureTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)

    def test_phase_line_takes_the_last(self):
        text = "x\nAUTO-DEV-PHASE story=1-2 outcome=advanced step=4b\nAUTO-DEV-PHASE story=1-2 outcome=halted step=6\n"
        self.assertEqual(d.phase_line(text), {"story": "1-2", "outcome": "halted", "step": "6"})
        self.assertIsNone(d.phase_line("AUTO-DEV-PHASE story=1 outcome=bogus"))

    def test_ledger_fingerprint_keeps_record_facts_only(self):
        rec = self.root / "rec.md"
        rec.write_text("# r\n- 2026-01-01 checkpoint-1 spec approved\n", encoding="utf-8")
        with unittest.mock.patch.object(d, "REPO", self.root):
            fp = d.ledger_fingerprint(textwrap.dedent(f"""\
                **Position:** review round 1
                _sprint-status: **in-progress**_
                _record file: rec.md_
                ## Commits (git)
                - abc1234 feat: x
                ## Handoff state
                - ok  spec approved
                ## Phases observed
                - something journal-projected
                """))
        self.assertIn("_sprint-status: **in-progress**_", fp)
        self.assertIn("- abc1234 feat: x", fp)
        self.assertIn("- ok  spec approved", fp)
        self.assertIn("- 2026-01-01 checkpoint-1 spec approved", fp)
        self.assertNotIn("- something journal-projected", fp)

    def test_progress_rule(self):
        self.assertTrue(d.progressed(("a",), 0, ("a",), 1))
        self.assertFalse(d.progressed(("a",), 0, ("a",), 0))
        self.assertTrue(d.progressed(("a",), 0, ("b",), 0))
        self.assertTrue(d.progressed(None, 0, ("- ok  spec approved",), 0))
        self.assertFalse(d.progressed(None, 0, ("- MISSING spec approved",), 0))

    def test_init_problem_reads_config(self):
        ok = {"tools": ["Bash", "Task", "Skill"], "skills": ["auto-dev-loop"], "agents": ["story-impl"]}
        self.assertIsNone(d.init_problem(ok))
        self.assertIn("agent story-impl", d.init_problem({**ok, "agents": []}))
        self.assertIn("skill auto-dev-loop", d.init_problem({**ok, "skills": []}))

    def test_harness_drift(self):
        (self.root / ".claude" / "agents").mkdir(parents=True)
        agent = self.root / ".claude" / "agents" / "story-impl.md"
        agent.write_text("---\nname: story-impl\nmodel: opus\neffort: high\nmaxTurns: 9\n---\nbody\n", encoding="utf-8")
        before = d.harness_files(self.root)
        self.assertIn(".claude/agents/story-impl.md", before)
        self.assertEqual(d.agent_knobs(self.root)["story-impl"], {"model": "opus", "effort": "high", "maxTurns": "9"})
        agent.write_text("changed", encoding="utf-8")
        self.assertEqual(d.harness_changes(before, d.harness_files(self.root)), [".claude/agents/story-impl.md"])

    def test_run_record_first_write_is_exclusive(self):
        run_dir = self.root / "20260101-000000"
        run_dir.mkdir()
        (run_dir / "run.json").write_text('{"pid": 1}', encoding="utf-8")
        rec = d.RunRecord(run_dir, d.new_run_data(run_dir, 2, None, False, {"story": None}))
        with self.assertRaises(FileExistsError):
            rec.write()
        self.assertEqual(json.loads((run_dir / "run.json").read_text())["pid"], 1)

    def test_list_and_stop(self):
        state = self.root / "state"
        live = state / "20260101-000000"
        live.mkdir(parents=True)
        data = d.new_run_data(live, os.getpid(), None, False, {"story": "1-2", "max_cost_usd": 10,
                                                                  "session_timeout_min": 300})
        (live / "run.json").write_text(json.dumps(data), encoding="utf-8")
        (live / "driver.log").write_text("[00:00:00] x\n", encoding="utf-8")
        (live / "sessions.jsonl").write_text(json.dumps({"session": 1, "cost_usd": 2.5, "step": "4b",
                                                         "outcome": "advanced"}) + "\n", encoding="utf-8")
        ended = state / "20260101-000001"
        ended.mkdir()
        done = d.new_run_data(ended, 999999, None, False, {"story": None})
        done["ended"] = {"utc": "x", "exit_code": 0, "reason": "terminated"}
        (ended / "run.json").write_text(json.dumps(done), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(d.main(["list", "--json", "--state-dir", str(state)]), 0)
        rows = {r["run_id"]: r for r in json.loads(out.getvalue())["runs"]}
        self.assertEqual(rows[live.name]["state"]["name"], "running")
        self.assertEqual(rows[live.name]["spent_usd"], 2.5)
        self.assertEqual(rows[ended.name]["state"]["name"], "ended")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.main(["stop", ended.name, "--state-dir", str(state)]), 1)
            self.assertEqual(d.main(["stop", live.name, "--state-dir", str(state)]), 0)
            self.assertEqual(d.main(["stop", "bogus", "--state-dir", str(state)]), 2)
        self.assertTrue((live / "STOP").is_file())


FAKE_CLAUDE = """#!{python}
import json, sys
outcome = {outcome!r}
print(json.dumps({{"type": "system", "subtype": "init", "tools": ["Bash", "Agent", "Skill"],
                  "skills": ["auto-dev-loop"], "agents": ["story-impl"], "model": "opus",
                  "claude_code_version": "test"}}))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                  "total_cost_usd": 0.25, "result": "AUTO-DEV-PHASE story=" + {story!r} + " outcome=" + outcome + " step=1"}}))
"""


@unittest.skipIf(os.name == "nt", "the fake CLI is a POSIX script")
class ForegroundRunTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)
        subprocess.run(["git", "init", "-q", "-b", "development", str(self.root)], check=True)
        self.bin = Path(tempfile.mkdtemp(prefix="fake-claude-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.bin, ignore_errors=True))
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path))
        patcher = unittest.mock.patch.multiple(d, REPO=self.root, STATE=self.root / ".workflow" / "state" / "auto-dev")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, story: str, outcome: str) -> None:
        p = self.bin / "claude"
        p.write_text(FAKE_CLAUDE.format(python=sys.executable, story=story, outcome=outcome), encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def run_driver(self, *argv) -> tuple[int, str]:
        state = self.root / "runs"
        with contextlib.redirect_stdout(io.StringIO()):
            code = d.main(["--state-dir", str(state), "--session-timeout-min", "1", *argv])
        run = sorted(state.iterdir())[-1]
        return code, (run / "driver.log").read_text(encoding="utf-8"), run

    def test_terminated_ends_with_exit_0_and_records_the_session(self):
        self.fake("none", "terminated")
        code, log, run = self.run_driver()
        self.assertEqual(code, 0, log)
        self.assertIn("no startable story left; stopping", log)
        rec = json.loads((run / "run.json").read_text())
        self.assertEqual(rec["ended"]["exit_code"], 0)
        line = json.loads((run / "sessions.jsonl").read_text().splitlines()[0])
        self.assertEqual(line["outcome"], "terminated")
        self.assertEqual(line["cost_usd"], 0.25)

    def test_advanced_without_a_new_fact_stops_with_exit_1(self):
        self.fake("9-99", "advanced")
        code, log, _ = self.run_driver()
        self.assertEqual(code, 1, log)
        self.assertTrue("audit of 9-99" in log or "no new fact" in log, log)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
