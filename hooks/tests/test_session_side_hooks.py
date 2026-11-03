"""notification.py, instructions_loaded.py, inject_path_rules.py and context_cap.py."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hookenv import SESSION, HookTestCase  # noqa: E402


class NotificationTests(HookTestCase):
    def test_records_line(self):
        p = self.run_hook(SESSION / "notification.py", {"session_id": "abc-1", "notification_type":
                                                         "idle_prompt", "message": "waiting"})
        self.assertEqual((0, b""), (p.returncode, p.stdout))
        line = json.loads((self.root / ".workflow" / "state" / "attention" / "abc-1.jsonl")
                          .read_text(encoding="utf-8"))
        self.assertEqual(("abc-1", "idle_prompt", None), (line["session_id"], line["notification_type"],
                                                         line["cwd"]))
        self.assertIn("received", line)

    def test_rejects_unsafe_session_id(self):
        self.run_hook(SESSION / "notification.py", {"session_id": "../x"})
        self.assertFalse((self.root / ".workflow" / "state" / "attention").exists())

    def test_garbage_is_silent(self):
        p = self.run_hook(SESSION / "notification.py", "not json")
        self.assertEqual((0, b""), (p.returncode, p.stdout))


class InstructionsLoadedTests(HookTestCase):
    def test_appends_record(self):
        self.run_hook(SESSION / "instructions_loaded.py", {
            "session_id": "s", "file_path": "/x/CLAUDE.md", "memory_type": "Project",
            "load_reason": "session_start", "agent_type": "story-impl"})
        rec = json.loads((self.root / ".workflow" / "state" / "instructions-loaded.jsonl")
                         .read_text(encoding="utf-8"))
        self.assertEqual(("story-impl", "session_start"), (rec["agent_type"], rec["load_reason"]))


class InjectPathRulesTests(HookTestCase):
    extra_config = '[path_rules]\ndir = ".claude/rules"\nnames = ["testing"]\n'

    def setUp(self):
        super().setUp()
        d = self.root / ".claude" / "rules"
        d.mkdir(parents=True)
        (d / "testing.md").write_text("---\npaths:\n  - \"src/**/test_*.py\"\n---\n# Tests\nRule body.\n",
                                      encoding="utf-8")
        (d / "secret.md").write_text("not configured\n", encoding="utf-8")

    def ctx(self, *args):
        p = self.run_hook(SESSION / "inject_path_rules.py", {"hook_event_name": "SubagentStart"}, list(args))
        return p.stdout.decode()

    def test_injects_configured_rule(self):
        out = json.loads(self.ctx("testing"))["hookSpecificOutput"]
        self.assertEqual("SubagentStart", out["hookEventName"])
        self.assertIn("Rule body.", out["additionalContext"])
        self.assertIn("relative to your worktree root", out["additionalContext"])

    def test_unconfigured_missing_or_bad_names_emit_nothing(self):
        for args in (("secret",), ("absent",), ("../x",), ()):
            self.assertEqual("", self.ctx(*args), args)


class ContextCapTests(HookTestCase):
    extra_config = "[context_cap]\ntokens = 1000\n"

    def transcript(self, ctx_tokens: int) -> Path:
        t = self.root / "agent-a1.jsonl"
        usage = {"input_tokens": 10, "cache_read_input_tokens": ctx_tokens - 10, "cache_creation_input_tokens": 0,
                 "iterations": [{"type": "advisor_message", "input_tokens": 99999},
                                {"type": "message", "input_tokens": 10,
                                 "cache_read_input_tokens": ctx_tokens - 10}]}
        t.write_text(json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n", encoding="utf-8")
        return t

    def run_cap(self, t: Path):
        return self.run_hook(SESSION / "context_cap.py", {"agent_id": "a1", "session_id": "s",
                                                          "transcript_path": str(t)}, ["--role", "impl"])

    def log(self):
        return [json.loads(l) for l in (self.root / ".workflow" / "state" / "context-cap.jsonl")
                .read_text(encoding="utf-8").splitlines()]

    def test_below_cap_silent(self):
        self.assertEqual(b"", self.run_cap(self.transcript(500)).stdout)

    def test_at_cap_reminds_and_logs(self):
        out = json.loads(self.run_cap(self.transcript(2000)).stdout)["hookSpecificOutput"]
        self.assertIn("Context cap reached: your context is 2k tokens (cap 1k)", out["additionalContext"])
        self.assertEqual("cap", self.log()[-1]["event"])

    def test_no_transcript_logged(self):
        self.run_hook(SESSION / "context_cap.py", {"agent_id": "a1"}, ["--role", "diagnose"])
        self.assertEqual("no-transcript", self.log()[-1]["event"])


if __name__ == "__main__":
    unittest.main()
