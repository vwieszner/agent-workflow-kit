"""session_journal.py: plugin payloads carry user text and interrupts into facts, a subagent's
plugin turn lands in `<parent>.sub-<agent>.jsonl`, Agent summaries are not doubled, and a
Claude SubagentStop reads the subagent's own transcript when `agent_transcript_path` is given."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hookenv import SESSION, HookTestCase  # noqa: E402

JOURNAL = SESSION / "session_journal.py"


class JournalTests(HookTestCase):
    def env(self) -> dict:
        e = super().env()
        e.pop("WORKFLOW_RETRO_RUNNING", None)
        return e

    def entries(self, name: str) -> list:
        f = self.root / ".workflow" / "state" / "journal" / name
        self.assertTrue(f.is_file(), f"journal {name} not written")
        return [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_plugin_user_text_interrupt_and_agent_summary(self):
        p = self.run_hook(JOURNAL, {
            "hook_event_name": "Stop", "session_id": "s1",
            "workflow_tool_calls": [{"tool": "Agent", "input": "story-impl: go", "ok": True}],
            "workflow_user_messages": ["implement story 1-2", "<system-reminder>x"],
            "workflow_interrupted": True,
        })
        self.assertEqual(0, p.returncode)
        (entry,) = self.entries("s1.jsonl")
        self.assertEqual("story-impl: go", entry["tool_calls"][0]["input"])
        kinds = [f["kind"] for f in entry["facts"]]
        self.assertIn("interrupted", kinds)
        texts = [f["text"] for f in entry["facts"] if f["kind"] == "user_message"]
        self.assertEqual(["implement story 1-2"], texts, "injection text journaled as user input")
        spawned = [f for f in entry["facts"] if f["kind"] == "agent_spawned"]
        self.assertEqual("story-impl", spawned[0]["agent_type"])
        self.assertEqual("go", spawned[0]["desc"])

    def test_plugin_subagent_turn_goes_to_parent_sub_file(self):
        self.run_hook(JOURNAL, {
            "hook_event_name": "SubagentStop", "session_id": "parent1", "agent_id": "story-impl",
            "workflow_tool_calls": [{"tool": "Write", "input": "scripts/x.py", "ok": True}],
        })
        (entry,) = self.entries("parent1.sub-story-impl.jsonl")
        self.assertEqual("story-impl", entry["agent_id"])
        self.assertIn("script_written", [f["kind"] for f in entry["facts"]])

    def test_subagent_stop_prefers_agent_transcript(self):
        main_t = self.root / "main.jsonl"
        sub_t = self.root / "sub.jsonl"
        use = lambda cmd: json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": cmd, "name": "Bash", "input": {"command": cmd}}]}}) + "\n"
        main_t.write_text(use("echo main"), encoding="utf-8")
        sub_t.write_text(use("echo sub"), encoding="utf-8")
        self.run_hook(JOURNAL, {"hook_event_name": "SubagentStop", "session_id": "s2",
                                "agent_id": "a1", "transcript_path": str(main_t),
                                "agent_transcript_path": str(sub_t)})
        (entry,) = self.entries("s2.sub-a1.jsonl")
        self.assertEqual(["echo sub"], [c["input"] for c in entry["tool_calls"]])


if __name__ == "__main__":
    unittest.main()
