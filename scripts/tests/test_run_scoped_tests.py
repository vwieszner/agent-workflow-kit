"""Tests for run_scoped_tests.py: output parsing (canned runner output) and the CLI's
config/registry resolution and exit-code contract (0 green / 1 failures / 2 infra)."""

import io
import re
import shlex
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import run_scoped_tests  # noqa: E402
from run_scoped_tests import parse_output  # noqa: E402

FAIL_RE = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<id>\S+ \([^)]+\))", re.MULTILINE)
SUMMARY_RE = re.compile(r"^Ran \d+ tests? in [\d.]+s", re.MULTILINE)

GREEN = """\
Found 12 test(s).
............
----------------------------------------------------------------------
Ran 12 tests in 3.412s

OK
"""

RED = """\
Found 3 test(s).
F.E
======================================================================
FAIL: test_alpha (app.tests.test_x.AlphaTests.test_alpha)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/app/tests/test_x.py", line 10, in test_alpha
    self.assertEqual(1, 2)
AssertionError: 1 != 2
======================================================================
ERROR: test_beta (app.tests.test_x.BetaTests.test_beta)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/app/tests/test_x.py", line 20, in test_beta
    raise RuntimeError("boom")
RuntimeError: boom
----------------------------------------------------------------------
Ran 3 tests in 0.201s

FAILED (failures=1, errors=1)
"""

NO_RESULT = 'service "app" is not running\n'

RUNNER = r'''
import os, sys
print("cwd=" + os.getcwd())
print("labels=" + "|".join(sys.argv[2:]))
if sys.argv[1] == "red":
    print("FAIL: pkg.test_one (pkg.T.test_one)")
    print("Ran 1 test in 0.1s"); print("FAILED"); sys.exit(1)
if sys.argv[1] == "silent":
    sys.exit(1)
print("Ran 2 tests in 0.1s"); print("OK")
'''


class ParseOutputTests(unittest.TestCase):
    def test_green_run(self):
        p = parse_output(GREEN, 0, FAIL_RE, SUMMARY_RE)
        self.assertEqual(p["status"], "pass")
        self.assertEqual(p["summary"], ["Ran 12 tests in 3.412s"])
        self.assertEqual(p["failures"], [])

    def test_red_run_collects_all_failures_with_kinds(self):
        p = parse_output(RED, 1, FAIL_RE, SUMMARY_RE)
        self.assertEqual(p["status"], "fail")
        self.assertEqual(
            [(f["kind"], f["id"]) for f in p["failures"]],
            [("FAIL", "test_alpha (app.tests.test_x.AlphaTests.test_alpha)"),
             ("ERROR", "test_beta (app.tests.test_x.BetaTests.test_beta)")],
        )
        self.assertIn("AssertionError: 1 != 2", p["failures"][0]["block"])
        self.assertNotIn("RuntimeError", p["failures"][0]["block"])

    def test_no_result_line_is_infra(self):
        self.assertEqual(parse_output(NO_RESULT, 1, FAIL_RE, SUMMARY_RE)["status"], "infra")

    def test_nonzero_exit_without_ids_still_fails(self):
        self.assertEqual(parse_output(GREEN, 1, FAIL_RE, SUMMARY_RE)["status"], "fail")

    def test_group_one_is_the_id_without_named_groups(self):
        p = parse_output("FAIL: a.b.c\nRan 1 test in 0.1s\n", 1,
                         re.compile(r"^(?:FAIL|ERROR): (\S+)", re.M), SUMMARY_RE)
        self.assertEqual(p["ids"], ["a.b.c"])
        self.assertEqual(p["failures"][0]["kind"], "FAIL")


class ScopedCliTests(unittest.TestCase):
    def _setup(self, extra: str = ""):
        root = te.enter(self, "")
        (root / "runner.py").write_text(RUNNER, encoding="utf-8")
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(root / 'runner.py'))} {{labels}}"
        (root / ".workflow" / "config.toml").write_text(
            f'[stack]\nruntime = "none"\n[tests]\nscoped_cmd = {te.toml_str(cmd)}\n'
            f'fail_id_regex = {te.toml_str(FAIL_RE.pattern)}\n'
            f'summary_regex = {te.toml_str(SUMMARY_RE.pattern)}\n{extra}', encoding="utf-8")
        te.clear_caches()
        return root

    def _main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_scoped_tests.main(list(argv))
        return code, out.getvalue()

    def test_green_labels_are_substituted(self):
        self._setup()
        code, out = self._main("green", "a.b c")
        self.assertEqual(code, 0, out)
        self.assertIn("[SCOPED] PASS", out)

    def test_red_lists_failing_ids_and_exits_one(self):
        self._setup()
        code, out = self._main("red")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL: pkg.test_one (pkg.T.test_one)", out)

    def test_no_result_line_is_infra_exit_two(self):
        self._setup()
        code, out = self._main("silent")
        self.assertEqual(code, 2, out)
        self.assertIn("INFRA-ERROR", out)

    def test_unset_scoped_cmd_is_exit_two(self):
        te.enter(self, "")
        code, out = self._main("anything")
        self.assertEqual(code, 2, out)

    def test_story_id_runs_in_the_registry_worktree(self):
        root = self._setup()
        wt = root / "wt" / "7-2"
        wt.mkdir(parents=True)
        reg = root / "docs" / "implementation-artifacts" / "slot-registry.md"
        reg.parent.mkdir(parents=True)
        reg.write_text(
            "| Slot | Status | Story ID | Branch | Worktree | Since |\n"
            "|------|--------|----------|--------|----------|-------|\n"
            "| 1 | free | — | — | — | — |\n"
            f"| 2 | in_use | 7-2 | story/7-2 | {wt} | 2026-01-01 |\n", encoding="utf-8")
        ctx, err = run_scoped_tests.resolve_target("7-2", None)
        self.assertIsNone(err)
        self.assertEqual(ctx["slot"], 2)
        self.assertEqual(Path(ctx["worktree"]), wt.resolve())
        self.assertEqual(ctx["stack"], "story-7-2")
        code, out = self._main("--story-id", "7-2", "green")
        self.assertEqual(code, 0, out)
        code, out = self._main("--story-id", "9-9", "green")
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
