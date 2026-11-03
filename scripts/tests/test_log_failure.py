"""Tests for log_failure.py: header on first use, append-only entries, branch/worktree
from config, JSON output contract, and the mutex-timeout error path."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import log_failure  # noqa: E402
from _named_mutex import NamedMutex  # noqa: E402

CONFIG = """
[project]
name = "demo"
[git]
branch_prefix = "feat/"
worktrees_root = "../wt"
[paths]
auto_dev_failure_log = "logs/failures.md"
"""

ARGS = ["--story-id", "7-92", "--branch-name", "7-92", "--failure-step", "story-impl",
        "--failure-summary", "Tests failed after 5 rounds.", "--mutex-timeout-ms", "500"]


class LogFailureTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)

    def _main(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = log_failure.main(ARGS + list(extra))
        return code, json.loads(out.getvalue())

    def test_first_entry_writes_header_then_appends(self):
        code, res = self._main("--wip-commit", "abc1234", "--last-test-output", "FAIL: x")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["status"], "logged")
        log = self.root / "logs" / "failures.md"
        self.assertEqual(Path(res["file"]), log)
        body = log.read_text(encoding="utf-8")
        self.assertTrue(body.startswith("# Auto-Dev Loop — Permanent Failures"))
        self.assertIn("- branch: feat/7-92 (PRESERVED", body)
        self.assertIn("- wip_commit: abc1234", body)
        self.assertIn("FAIL: x", body)
        self.assertIn("/wt/7-92`, branch `feat/7-92`", body)
        self.assertIn("resume_halted.py 7-92 --ruling", body)
        self._main()
        self.assertEqual(log.read_text(encoding="utf-8").count("## 7-92 — "), 2)
        self.assertEqual(log.read_text(encoding="utf-8").count("# Auto-Dev Loop"), 1)

    def test_held_mutex_times_out_with_error_json(self):
        with NamedMutex(log_failure.LOG_MUTEX, 1000):
            code, res = self._main()
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "error")
        self.assertFalse((self.root / "logs" / "failures.md").exists())

    def test_line_break_in_heading_flag_is_refused(self):
        for flag in ("--story-id", "--failure-step", "--branch-name", "--wip-commit"):
            argv = list(ARGS) + ["--wip-commit", "a"]
            i = argv.index(flag)
            argv[i + 1] = "x\ny"
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                log_failure.main(argv)
            self.assertEqual(cm.exception.code, 2, flag)
        self.assertFalse((self.root / "logs" / "failures.md").exists())

    def test_multiline_summary_is_indented_and_branch_none_has_no_resume(self):
        argv = list(ARGS)
        argv[argv.index("--failure-summary") + 1] = "first\r\n\n- asked_of: x"
        argv[argv.index("--branch-name") + 1] = "none"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(log_failure.main(argv), 0)
        body = (self.root / "logs" / "failures.md").read_text(encoding="utf-8")
        self.assertIn("- failure_summary: first\n  \n  - asked_of: x\n", body)
        self.assertIn("- branch: none", body)
        self.assertNotIn("Resume:", body)


if __name__ == "__main__":
    unittest.main()
