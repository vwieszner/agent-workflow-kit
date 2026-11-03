"""Hooks of the autonomous loop: agent_dispatch (edge allowlist, story-impl escalation),
auto_dev_keep (session marking, keep-halted denies), clear_offer (held once after a heavy
checkpoint) and agent_outcome (completion-claim gate) — each run as the harness runs it."""
from __future__ import annotations

import json
import os
import subprocess

from _hookenv import GUARDS, SESSION, HookTestCase

DISPATCH = GUARDS / "agent_dispatch.py"
KEEP = GUARDS / "auto_dev_keep.py"
CLEAR = GUARDS / "clear_offer.py"
OUTCOME = SESSION / "agent_outcome.py"


class AgentDispatchTests(HookTestCase):
    def call(self, caller, callee, **extra):
        payload = {"tool_name": "Agent", "agent_type": caller,
                   "tool_input": {"subagent_type": callee, "prompt": "x", **extra}}
        return self.json_denied(DISPATCH, payload)

    def test_main_session_is_not_edge_checked(self):
        self.assertFalse(self.json_denied(DISPATCH, {"tool_name": "Agent",
                                                     "tool_input": {"subagent_type": "anything"}}))

    def test_edges(self):
        self.assertFalse(self.call("story-impl", "story-diagnose", run_in_background=False))
        self.assertFalse(self.call("story-impl-max", "dev-preflight", run_in_background=False))
        self.assertTrue(self.call("story-impl", "story-diagnose"))  # background not false
        self.assertTrue(self.call("story-impl", "story-finalize", run_in_background=False))
        self.assertTrue(self.call("blind-hunter", "Explore", run_in_background=False))

    def test_edge_model_from_config(self):
        self.write_config('[guards]\ndispatch_edge_models = { "story-spec->Explore" = "sonnet" }\n')
        self.assertTrue(self.call("story-spec", "Explore", run_in_background=False))
        self.assertFalse(self.call("story-spec", "Explore", run_in_background=False, model="sonnet"))

    def test_escalation_needs_installed_variants(self):
        handoff = self.root / "handoff.md"
        handoff.write_text("rounds_used: 3\n", encoding="utf-8")
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "story-impl",
                                                        "prompt": f"story_id: 1-2\nhandoff: {handoff}"}}
        self.assertFalse(self.json_denied(DISPATCH, payload))
        (self.root / ".claude" / "agents").mkdir(parents=True)
        (self.root / ".claude" / "agents" / "story-impl-max.md").write_text("x", encoding="utf-8")
        self.assertTrue(self.json_denied(DISPATCH, payload))
        payload["tool_input"]["subagent_type"] = "story-impl-max"
        self.assertFalse(self.json_denied(DISPATCH, payload))


class KeepHaltedTests(HookTestCase):
    def test_marked_session_denies_teardown(self):
        cmd = {"tool_name": "Bash", "session_id": "s9", "tool_input": {"command": "git worktree remove ../x"}}
        self.assertFalse(self.json_denied(KEEP, cmd))
        self.assertFalse(self.json_denied(KEEP, {"tool_name": "Skill", "session_id": "s9",
                                                 "tool_input": {"skill": "auto-dev-loop"}}))
        self.assertTrue((self.root / ".workflow" / "state" / "auto-dev" / "sessions" / "s9").is_file())
        self.assertTrue(self.json_denied(KEEP, cmd))

    def test_driver_marker_env(self):
        env = self.env()
        env["WORKFLOW_AUTO_DEV_RUN"] = "r1"
        p = subprocess.run(["python3", str(KEEP)], input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "docker volume rm story-1-2_db"}}).encode(),
            capture_output=True, env=env, cwd=str(self.root))
        self.assertIn(b'"deny"', p.stdout)


class ClearOfferTests(HookTestCase):
    def transcript(self, entries) -> str:
        path = self.root / "t.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        return str(path)

    def checkpoint(self, marker=""):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {
            "command": f'uv run --no-project .workflow/scripts/story_record.py append 1-2 "checkpoint-1 spec approved{marker}"'}}],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 400_000}}}

    def dispatch(self, path):
        return {"tool_name": "Agent", "transcript_path": path,
                "tool_input": {"subagent_type": "story-impl", "prompt": "story_id: 1-2"}}

    def test_held_once_then_released_by_offer_and_reply(self):
        entries = [self.checkpoint()]
        self.assertTrue(self.json_denied(CLEAR, self.dispatch(self.transcript(entries))))
        entries += [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "State saved — /clear, then /parallel-dev-wave 1-2"}]}},
            {"type": "user", "message": {"content": "continue"}}]
        self.assertFalse(self.json_denied(CLEAR, self.dispatch(self.transcript(entries))))

    def test_auto_dev_checkpoint_and_small_context_pass(self):
        self.assertFalse(self.json_denied(CLEAR, self.dispatch(self.transcript([self.checkpoint(" (auto-dev-loop)")]))))
        self.write_config("[guards]\nclear_offer_context_tokens = 900000\n")
        self.assertFalse(self.json_denied(CLEAR, self.dispatch(self.transcript([self.checkpoint()]))))


class AgentOutcomeTests(HookTestCase):
    def setUp(self):
        super().setUp()
        self.wt = self.root / "wt"
        self.wt.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(self.wt), *a], check=True, capture_output=True)  # noqa: E731
        g("init", "-q", "-b", "development")
        g("config", "user.email", "t@e")
        g("config", "user.name", "t")
        (self.wt / "a.txt").write_text("a", encoding="utf-8")
        g("add", "-A")
        g("commit", "-q", "-m", "base")
        g("checkout", "-q", "-b", "story/1-2")
        (self.wt / "b.txt").write_text("b", encoding="utf-8")
        g("add", "-A")
        g("commit", "-q", "-m", "feat: [Story 1-2] thing")
        self.g = g

    def stop(self, report, role="impl", prompt=""):
        return self.run_hook(OUTCOME, {"last_assistant_message": report, "agent_id": os.urandom(4).hex(),
                                       "agent_type": "story-impl"}, ["--role", role])

    def test_claim_holds(self):
        p = self.stop(f"✅ story-impl complete\nstory_id: 1-2\nworktree_path: {self.wt}\n")
        self.assertEqual(b"", p.stdout.strip(), p.stderr)

    def test_dirty_tree_and_second_commit_block(self):
        (self.wt / "a.txt").write_text("changed", encoding="utf-8")
        p = self.stop(f"✅ story-impl complete\nstory_id: 1-2\nworktree_path: {self.wt}\n")
        out = json.loads(p.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("uncommitted tracked changes", out["reason"])

    def test_no_claim_passes(self):
        (self.wt / "a.txt").write_text("changed", encoding="utf-8")
        self.assertEqual(b"", self.stop("I could not finish: tests red").stdout.strip())

    def test_tested_tree_check(self):
        self.write_config('[story_record]\ntested_trees = ["."]\n')
        arts = self.wt / "docs" / "implementation-artifacts"
        arts.mkdir(parents=True)
        (arts / "1-2-record.md").write_text("- phase-2 tests GREEN — x repo-tree 0000000\n", encoding="utf-8")
        self.g("add", "-A")
        self.g("commit", "-q", "--amend", "--no-edit")
        p = self.stop(f"✅ story-impl complete\nstory_id: 1-2\nworktree_path: {self.wt}\n")
        self.assertIn("code changed after the green run", json.loads(p.stdout)["reason"])
