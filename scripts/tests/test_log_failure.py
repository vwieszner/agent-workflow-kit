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

ARGS = ["--story-id", "7-2", "--branch-name", "7-2", "--failure-step", "story-impl",
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
        self.assertIn("- branch: feat/7-2 (PRESERVED", body)
        self.assertIn("- wip_commit: abc1234", body)
        self.assertIn("FAIL: x", body)
        self.assertIn("git worktree add ", body)
        self.assertIn("/wt/7-2 feat/7-2", body)
        self._main()
        self.assertEqual(log.read_text(encoding="utf-8").count("## 7-2 — "), 2)
        self.assertEqual(log.read_text(encoding="utf-8").count("# Auto-Dev Loop"), 1)

    def test_held_mutex_times_out_with_error_json(self):
        with NamedMutex(log_failure.MUTEX_NAME, 1000):
            code, res = self._main()
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "error")
        self.assertFalse((self.root / "logs" / "failures.md").exists())


if __name__ == "__main__":
    unittest.main()
