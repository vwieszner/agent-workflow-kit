"""Tests for the story-pipeline guards: land_lease_wait, story_write_paths, propagate_paths, and the
effort-variant / sed -i / verification-gap rules of agent_source_read and dispatch_prompt."""
from __future__ import annotations

import json
from pathlib import Path

from _hookenv import GUARDS, HookTestCase

LEASE_WAIT = GUARDS / "land_lease_wait.py"
WRITE_PATHS = GUARDS / "story_write_paths.py"
PROPAGATE = GUARDS / "propagate_paths.py"
SOURCE_READ = GUARDS / "agent_source_read.py"
DISPATCH = GUARDS / "dispatch_prompt.py"


def transcript(path: Path, prompt: str, tool_uses: list[dict] | None = None) -> Path:
    lines = [{"type": "user", "message": {"role": "user", "content": prompt}}]
    for tu in tool_uses or []:
        lines.append({"type": "assistant", "message": {"role": "assistant", "content": [tu]}})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


class LandLeaseWaitTests(HookTestCase):
    def _lease(self, story: str) -> None:
        state = self.root / ".workflow" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "land-lease.json").write_text(json.dumps({"story": story, "acquired": "2026-01-01T00:00:00Z"}),
                                               encoding="utf-8")

    def _payload(self, **kw) -> dict:
        tp = transcript(self.root / "t.jsonl", "go", [{"type": "tool_use", "name": "Bash", "input": {
            "command": "uv run --no-project .workflow/scripts/land_lease.py acquire 7-2"}}])
        return {"hook_event_name": "Stop", "transcript_path": str(tp), "background_tasks": [], **kw}

    def test_free_lease_and_background_task_allow(self):
        p = self.run_hook(LEASE_WAIT, self._payload())
        self.assertEqual((0, b""), (p.returncode, p.stdout.strip()))
        self._lease("7-2")
        p = self.run_hook(LEASE_WAIT, self._payload(background_tasks=[{"type": "bash"}]))
        self.assertEqual(b"", p.stdout.strip())

    def test_holder_session_waiting_on_the_owner_is_blocked_then_released(self):
        self._lease("7-2")
        p = self.run_hook(LEASE_WAIT, self._payload())
        out = json.loads(p.stdout or b"{}")
        self.assertEqual(out.get("decision"), "block", p.stderr)
        p = self.run_hook(LEASE_WAIT, self._payload(stop_hook_active=True))
        out = json.loads(p.stdout or b"{}")
        self.assertIn("released", out.get("systemMessage", ""))
        self.assertFalse((self.root / ".workflow" / "state" / "land-lease.json").exists())

    def test_question_tool_is_denied_and_another_sessions_lease_is_ignored(self):
        self._lease("7-2")
        p = self.run_hook(LEASE_WAIT, self._payload(tool_name="AskUserQuestion", hook_event_name="PreToolUse"))
        self.assertIn(b'"deny"', p.stdout)
        tp = transcript(self.root / "other.jsonl", "go")
        p = self.run_hook(LEASE_WAIT, {"hook_event_name": "Stop", "transcript_path": str(tp), "background_tasks": []})
        self.assertEqual(b"", p.stdout.strip())


class StoryWritePathsTests(HookTestCase):
    def _payload(self, tool: str, ti: dict) -> dict:
        tp = transcript(self.root / "agent-a1.jsonl", f"story_id: 7-2\nworktree_path: {self.root.parent}/wt-7-2\n")
        return {"tool_name": tool, "tool_input": ti, "agent_type": "story-impl", "agent_id": "a1",
                "session_id": "s1", "transcript_path": str(tp)}

    def test_write_into_the_main_checkout_is_denied(self):
        self.assertTrue(self.json_denied(WRITE_PATHS, self._payload(
            "Write", {"file_path": str(self.root / "src" / "a.py"), "content": "x"})))
        self.assertFalse(self.json_denied(WRITE_PATHS, self._payload(
            "Write", {"file_path": f"{self.root.parent}/wt-7-2/src/a.py", "content": "x"})))

    def test_shell_redirect_into_the_main_checkout_is_denied(self):
        self.assertTrue(self.json_denied(WRITE_PATHS, self._payload(
            "Bash", {"command": f"echo x > {self.root}/notes.md"})))
        self.assertFalse(self.json_denied(WRITE_PATHS, self._payload(
            "Bash", {"command": f"cat <<'EOF' > /tmp/probe.py\nprint('> {self.root}/x')\nEOF"})))

    def test_main_session_and_shared_paths_pass(self):
        self.assertFalse(self.json_denied(WRITE_PATHS, {"tool_name": "Write", "tool_input": {
            "file_path": str(self.root / "a.py")}}))
        self.write_config('[guards]\nmain_checkout_shared_paths = ["docs/questions.md"]\n')
        self.assertFalse(self.json_denied(WRITE_PATHS, self._payload(
            "Edit", {"file_path": str(self.root / "docs" / "questions.md")})))


class PropagatePathsTests(HookTestCase):
    def _payload(self, tool: str, ti: dict, prompt: str, agent: str = "story-propagate") -> dict:
        tp = transcript(self.root / "agent-p1.jsonl", prompt)
        return {"tool_name": tool, "tool_input": ti, "agent_type": agent, "agent_id": "p1",
                "session_id": "s1", "transcript_path": str(tp)}

    def test_propose_is_read_only(self):
        self.assertTrue(self.json_denied(PROPAGATE, self._payload(
            "Write", {"file_path": "/x/epic.md"}, "mode: propose\nstory_id: 7-2\n")))
        self.assertFalse(self.json_denied(PROPAGATE, self._payload(
            "Bash", {"command": "git -C /x log --oneline -5"}, "mode: propose\n")))
        self.assertTrue(self.json_denied(PROPAGATE, self._payload(
            "Bash", {"command": "git -C /x worktree add y"}, "mode: propose\n")))
        self.assertTrue(self.json_denied(PROPAGATE, self._payload(
            "Bash", {"command": "git log --output=/tmp/x"}, "mode: propose\n")))
        self.assertTrue(self.json_denied(PROPAGATE, self._payload(
            "Bash", {"command": "uv run --no-project .workflow/scripts/sprint_status.py set 7-2 done"},
            "mode: propose\n")))

    def test_apply_edits_only_approved_paths(self):
        prompt = "mode: apply\nstory_id: 7-2\napproved:\n- /x/docs/epic.md — P1 update\nrows: ...\n"
        self.assertFalse(self.json_denied(PROPAGATE, self._payload("Edit", {"file_path": "/x/docs/epic.md"}, prompt)))
        self.assertTrue(self.json_denied(PROPAGATE, self._payload("Edit", {"file_path": "/x/docs/other.md"}, prompt)))
        self.assertFalse(self.json_denied(PROPAGATE, self._payload(
            "Bash", {"command": "uv run --no-project .workflow/scripts/sprint_status.py set 7-2 done --note 'x; y'"},
            prompt)))

    def test_other_agents_are_not_checked(self):
        self.assertFalse(self.json_denied(PROPAGATE, self._payload(
            "Write", {"file_path": "/x/a.md"}, "mode: propose\n", agent="story-impl")))


class SourceReadAndDispatchRuleTests(HookTestCase):
    extra_config = '[graph]\ntool = "codebase-memory-mcp"\n'

    def write_config(self, extra: str) -> None:
        base = Path(__file__).resolve().parent / "_hookenv.py"
        del base
        super().write_config(extra.replace('[graph]\ntool = "codebase-memory-mcp"\n', ""))
        cfg = self.root / ".workflow" / "config.toml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace('tool = "none"', 'tool = "codebase-memory-mcp"'),
                       encoding="utf-8")

    def _shell(self, agent: str, cmd: str) -> dict:
        return {"tool_name": "Bash", "agent_type": agent, "tool_input": {"command": cmd}}

    def test_sed_in_place_allowed_for_editors_only(self):
        self.assertFalse(self.json_denied(SOURCE_READ, self._shell("story-impl", "sed -i 's/a/b/' src/a.py")))
        self.assertTrue(self.json_denied(SOURCE_READ, self._shell("story-diagnose", "sed -i 's/a/b/' src/a.py")))
        self.assertTrue(self.json_denied(SOURCE_READ, self._shell("story-impl-max", "cat src/a.py")))

    def test_verification_gap_dispatch_needs_the_preamble(self):
        bad = {"tool_name": "Agent", "tool_input": {"subagent_type": "verification-gap", "prompt": "look"}}
        good = {"tool_name": "Agent", "tool_input": {"subagent_type": "verification-gap",
                                                      "prompt": "Report findings only — do NOT auto-apply patches. look"}}
        self.assertEqual(2, self.exit_code(DISPATCH, bad))
        self.assertEqual(0, self.exit_code(DISPATCH, good))


if __name__ == "__main__":
    import unittest
    unittest.main()
