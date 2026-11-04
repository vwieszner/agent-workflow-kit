"""auto_dev_guard.py (keep-halted rules), milestone_map_check.py (the YAML subset and the
STARTABLE / HALTED / UNMAPPED / UNTRIAGED / DEAD-DEP lists), check_agent_frontmatter.py,
auto_dev_audit.py problems, and auto_dev_run_report.py over a synthetic run."""

import contextlib
import io
import json
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import auto_dev_audit  # noqa: E402
import auto_dev_guard as guard  # noqa: E402
import auto_dev_run_report as report  # noqa: E402
import check_agent_frontmatter as cf  # noqa: E402
import milestone_map_check as mmc  # noqa: E402

CONFIG = """
[project]
name = "demo"
[git]
base_branch = "development"
branch_prefix = "story/"
worktrees_root = "demo-worktrees"
[paths]
sprint_status = "docs/sprint-status.yaml"
slot_registry = "docs/slot-registry.md"
auto_dev_failure_log = "docs/auto-dev-failures.md"
[slots]
stack_prefix = "story-"
[stack]
runtime = "none"
[loop]
milestone = "r1"
[milestones.r1]
member_marker = "\\\\[R1\\\\]"
non_member_marker = "NOT R1"
untriaged_marker = "UNPHASED"
"""


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)
        self.env = {guard.MARKER: "run"}

    def deny(self, command: str) -> str | None:
        return guard.shell_check({"tool_input": {"command": command}}, env=self.env)

    def test_inactive_session_allows_everything(self):
        self.assertIsNone(guard.shell_check({"tool_input": {"command": "git worktree remove x"}}, env={}))

    def test_teardown_commands_denied(self):
        self.assertIn("git worktree remove", self.deny("git worktree remove --force ../x"))
        self.assertIn("story branch", self.deny("git branch -D story/1-2"))
        self.assertIn("volumes", self.deny("docker volume prune -f"))
        self.assertIn("worktree root", self.deny("rm -rf /home/u/demo-worktrees/1-2"))
        self.assertIn("worktree root", self.deny("rm -rf demo-worktrees/"))
        self.assertIn("marker", self.deny(f"unset {guard.MARKER}"))
        self.assertIsNone(self.deny("rm -rf demo-worktrees/1-2/node_modules/cache"))
        self.assertIsNone(self.deny("git branch -D feature/x"))

    def test_data_only_script_call_is_not_checked(self):
        cmd = ("uv run --no-project .workflow/scripts/halt_release.py --story-id 1-2 --failure-step x "
               "--failure-summary 'git worktree remove demo-worktrees/1-2 failed'")
        self.assertIsNone(self.deny(cmd))
        self.assertIsNotNone(self.deny(cmd + "; git worktree remove y"))
        self.assertIsNotNone(self.deny(cmd.replace("--failure-summary", "--bogus")))
        self.assertIsNone(self.deny(".workflow/scripts/resume_halted.py 1-2 --ruling 'rm -rf demo-worktrees/1-2'"
                                    .join(["python3 ", ""])))

    def test_bring_up_down_of_a_halted_story(self):
        log = self.root / "docs" / "auto-dev-failures.md"
        log.parent.mkdir(parents=True)
        log.write_text("## 1.2 — 2026-01-01T00:00:00 — story-impl\n", encoding="utf-8")
        msg = self.deny("uv run --no-project .workflow/scripts/bring_up_story_stack.py --slot 1 "
                        "--project-name story-1-2 --worktree x --mode down")
        self.assertIn("halted story 1-2", msg)

    def test_mark_from_payload(self):
        self.assertFalse(guard.mark_from_payload({"tool_name": "Skill", "tool_input": {"skill": "land-story"},
                                                  "session_id": "s1"}))
        self.assertTrue(guard.mark_from_payload({"hook_event_name": "UserPromptExpansion",
                                                 "command_name": "/auto-dev-loop", "session_id": "s1"}))
        self.assertTrue(guard.active(env={}, payload={"session_id": "s1"}))


STATUS = """development_status:
  1-1-base: done  # [R1]
  1-2-next: ready-for-dev  # [R1]
  1-3-after: backlog  # [R1]
  1-4-halted: blocked  # [R1] HALTED 2026-01-01 at x; worktree kept
  1-5-unmapped: backlog  # [R1]
  1-6-loose: backlog  # some note
  1-7-out: backlog  # NOT R1
  1-8-dead: backlog  # [R1]
  1-9-gone: superseded  # [R1]
last_updated: 2026-01-01

r1:
  dependency_map:
    refreshed: "2026-01-01"
    deps:
      1-2: [1-1]
      1-3: [1-2]
      1-4: []
      1-8: [1-9]
    parallel_groups:
      group_0_startable_now: [1-2, 1-4]
      group_1_after_group_0_merges:
        - 1-3
        - 1-8
    excluded:
      later: [1-7]
    critical_path_floor: {chain: "1-2 -> 1-3", length: 2}
"""


class MapCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "sprint-status.yaml").write_text(STATUS, encoding="utf-8")
        (docs / "auto-dev-failures.md").write_text("## 1-4 — 2026-01-01T00:00:00 — story-impl\n\n- x: y\n",
                                                   encoding="utf-8")

    def test_yaml_subset(self):
        tree = mmc.parse_yaml(STATUS)
        dmap = mmc.dig(tree, "r1.dependency_map")
        self.assertEqual(dmap["deps"]["1-2"], ["1-1"])
        self.assertEqual(dmap["parallel_groups"]["group_1_after_group_0_merges"], ["1-3", "1-8"])
        self.assertEqual(dmap["critical_path_floor"], {"chain": "1-2 -> 1-3", "length": 2})

    def test_report(self):
        r = mmc.Check("r1", self.root).report()
        self.assertEqual([s["story"] for s in r["startable"]], ["1-2-next"])
        self.assertEqual([h["story"] for h in r["halted"]], ["1-4-halted"])
        self.assertEqual(r["unmapped"], ["1-5-unmapped"])
        self.assertEqual(r["untriaged"], ["1-6-loose"])
        self.assertEqual([x["dep"] for x in r["dead_dep"]], ["1-9"])
        self.assertFalse(r["empty"]["due"])

    def test_missing_milestone_exits_2(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            mmc.Check("nope", self.root)
        self.assertEqual(cm.exception.code, 2)


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)
        (self.root / ".claude" / "agents").mkdir(parents=True)
        (self.root / ".claude" / "workflows").mkdir()

    def agent(self, name: str, fm: str) -> None:
        (self.root / ".claude" / "agents" / f"{name}.md").write_text(f"---\nname: {name}\n{fm}---\nbody\n",
                                                                    encoding="utf-8")

    def test_policy(self):
        self.agent("a", "model: opus\neffort: high\n")
        self.agent("b", "effort: huge\n")
        (self.root / ".claude" / "workflows" / "w.js").write_text("const EFFORT = {'a': 'max', 'zz': 'low'}\n",
                                                                 encoding="utf-8")
        problems = cf.check(self.root)
        self.assertTrue(any("b.md: no model" in p for p in problems))
        self.assertTrue(any("effort 'huge'" in p for p in problems))
        self.assertTrue(any("EFFORT['a'] is 'max'" in p for p in problems))
        self.assertTrue(any("'zz'" in p for p in problems))

    def test_hook_blocks_only_agent_files(self):
        self.agent("b", "effort: huge\n")
        payload = {"tool_input": {"file_path": str(self.root / ".claude" / "agents" / "b.md")}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch("sys.stdin", io.TextIOWrapper(
                io.BytesIO(json.dumps(payload).encode())) ):
            self.assertEqual(cf.hook(), 0)
        self.assertEqual(json.loads(out.getvalue())["decision"], "block")


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)

    def test_problems(self):
        found = {"worktree": False, "branch": True, "volumes": {"story-1-2_db"}, "slot_in_use": True,
                 "running": 0, "uncommitted": ["docs/x row"]}
        out = auto_dev_audit.problems("1-2", "halted", {"volumes": {"story-1-2_db", "story-1-2_cache"}}, found)
        self.assertTrue(any("worktree" in p for p in out))
        self.assertTrue(any("story-1-2_cache" in p for p in out))
        self.assertTrue(any("in_use" in p for p in out))
        self.assertTrue(any("uncommitted: docs/x row" in p for p in out))
        self.assertEqual(auto_dev_audit.problems("1-2", "landed", None, found), [])


class RunReportTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)

    def test_report_of_a_run(self):
        run = self.root / "runs" / "20260101-000000"
        run.mkdir(parents=True)
        (run / "session-1.jsonl").write_text("\n".join([
            json.dumps({"type": "system", "subtype": "init", "claude_code_version": "t", "permissionMode": "auto",
                        "model": "m", "session_id": "s"}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Agent", "input": {"subagent_type": "story-impl",
                                                                              "description": "impl"}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "denied\n- [rule] x"}]}}),
            "not json",
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                        "total_cost_usd": 3.0, "duration_ms": 120000,
                        "result": "AUTO-DEV-PHASE story=1-2 outcome=halted step=6",
                        "modelUsage": {"m": {"costUSD": 3.0}}}),
        ]) + "\n", encoding="utf-8")
        (run / "sessions.jsonl").write_text(json.dumps({"session": 1, "knobs_id": "k1", "harness_id": "h",
                                                        "head": "abc", "story": "1-2", "outcome": "halted",
                                                        "minutes": 2, "cost_usd": 3.0, "knobs": {"x": 1}}) + "\n",
                                            encoding="utf-8")
        doc = report.build_document(run, run.parent)["run"]
        s = doc["sessions"][0]
        self.assertEqual((s["story"], s["outcome"], s["step"]), ("1-2", "halted", "6"))
        self.assertEqual(s["dispatches"][0]["type"], "story-impl")
        self.assertEqual(s["errors"][0]["text"], "denied - [rule] x")
        self.assertEqual(s["unparsable_lines"], {"count": 1, "first_line": 4})
        self.assertEqual(doc["totals"]["stories_by_outcome"], {"halted": 1})
        self.assertEqual(report.knobs_diff({"a": {"b": 1}}, {"a": {"b": 2}, "c": 3}),
                         ["a.b: 1 → 2", "c: (absent) → 3"])


if __name__ == "__main__":
    unittest.main()
