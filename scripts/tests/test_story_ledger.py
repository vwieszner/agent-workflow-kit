"""Tests for story_ledger.py / story_record.py: boundary-aware id matching, worktree
resolution through the slot registry, positive-verdict-only record checks, DESIGN §8
journal parsing into phases/rounds, and ground truth (sprint-status done) outranking
dispatches."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import story_ledger  # noqa: E402
import story_record  # noqa: E402

SPECS = "docs/implementation-artifacts"


class MentionsTests(unittest.TestCase):
    def test_boundaries(self):
        vs = story_ledger.variants("7-5")
        self.assertTrue(story_ledger.mentions("dispatch story 7-5 now", vs))
        self.assertTrue(story_ledger.mentions("7-5-session-summary", vs), "slug suffix must match")
        self.assertTrue(story_ledger.mentions("story 7.5", vs), "dotted form must match")
        self.assertFalse(story_ledger.mentions("7-5-11", vs), "a longer sibling id matched")
        self.assertFalse(story_ledger.mentions("7-51", vs))
        self.assertFalse(story_ledger.mentions("17-5", vs))
        self.assertFalse(story_ledger.mentions("7-20b", story_ledger.variants("7-20")))


class LedgerAndRecordTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, '[stack]\nruntime = "none"\n')
        self.wt = self.root / "worktrees" / "7-2"
        (self.wt / SPECS).mkdir(parents=True)
        (self.wt / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")
        (self.root / SPECS).mkdir(parents=True)
        (self.root / SPECS / "slot-registry.md").write_text(
            "| Slot | Status | Story ID | Branch | Worktree | Since |\n"
            "|------|--------|----------|--------|----------|-------|\n"
            f"| 3 | in_use | 7-2 | story/7-2 | {self.wt} | 2026-01-01 |\n", encoding="utf-8")
        (self.wt / SPECS / "7-2-some-feature.md").write_text("# spec\n", encoding="utf-8")

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = story_record.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_record_goes_to_the_worktree_and_check_needs_a_positive_verdict(self):
        code, _ = self._run("append", "7-2", "phase-2 tests RED — pkg (3 tests)")
        self.assertEqual(code, 0)
        rec = self.wt / SPECS / "7-2-record.md"
        self.assertTrue(rec.is_file(), "record not written into the story worktree")
        self.assertEqual(self._run("check", "7-2", "phase-2")[0], 1,
                         "a RED verdict satisfied the phase-2 check")
        self._run("append", "7-2", "phase-2 tests GREEN — pkg (3 tests)")
        self.assertEqual(self._run("check", "7-2", "phase-2")[0], 0)
        code, out = self._run("append", "7-2", "phase-2 tests GREEN — pkg (3 tests)")
        self.assertIn("not duplicated", out)
        self.assertEqual(rec.read_text(encoding="utf-8").count("GREEN"), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(story_record.cmd_check("7-2", "bogus"), 2)
        self.assertEqual(self._run("append", "7-2", "  -  ")[0], 2)

    def test_journal_turns_become_phases_and_rounds(self):
        jdir = self.root / ".workflow" / "state" / "journal"
        jdir.mkdir(parents=True)
        turns = [
            {"ts": "2026-01-01T10:00:00Z", "session_id": "s1", "tool_calls": [
                {"tool": "Agent", "input": {"subagent_type": "story-impl", "description": "impl 7-2"}, "ok": True}]},
            {"ts": "2026-01-01T11:00:00Z", "session_id": "s1", "tool_calls": [
                {"tool": "Agent", "input": {"subagent_type": "blind-hunter", "description": "review 7-2"}, "ok": True},
                {"tool": "task", "input": "subagent_type=edge-case-hunter review story 7-2", "ok": True},
                {"tool": "Bash", "input": "run tests for 7-2", "ok": False}]},
            {"ts": "2026-01-01T11:05:00Z", "session_id": "s1", "tool_calls": [
                {"tool": "Agent", "input": {"subagent_type": "story-impl", "description": "impl 7-21"}, "ok": True}]},
        ]
        (jdir / "s1.jsonl").write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")
        report = story_ledger.build("7-2")
        pos = next(ln for ln in report.splitlines() if ln.startswith("**Position:**"))
        self.assertIn("review round 1", pos)
        self.assertIn("rounds observed: 1", pos)
        self.assertIn("phase-2 (implementation)", report)
        self.assertIn("review round 1 — blind-hunter, edge-case-hunter", report)
        self.assertIn("tool_error", report)
        self.assertIn("spec", report)
        self.assertIn("7-2-some-feature.md", report)
        self.assertIn("**RESUMABLE: NO**", report, "a missing spec approval was not reported")

    def test_sprint_status_done_outranks_dispatches(self):
        (self.root / SPECS / "sprint-status.yaml").write_text(
            "development_status:\n  7-2-some-feature: done # MERGED\n", encoding="utf-8")
        report = story_ledger.build("7-2")
        self.assertIn("**Position:** COMPLETE", report)
        self.assertIn("**RESUMABLE: n/a**", report)


if __name__ == "__main__":
    unittest.main()
