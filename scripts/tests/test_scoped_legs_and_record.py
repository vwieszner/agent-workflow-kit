"""run_scoped_tests.py: runner parsers (unittest / vitest / playwright), named legs, the host
script-tests leg, --record (GREEN line + tested tree + saved command) and run_full_suite's
--record GREEN line."""
from __future__ import annotations

import io
import shlex
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import run_scoped_tests as rst  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "scoped_e2e"

UT_OK = "....\n----------------------------------------------------------------------\nRan 4 tests in 0.010s\n\nOK\n"
UT_RED = """\
F.
======================================================================
FAIL: test_a (pkg.test_x.T.test_a)
----------------------------------------------------------------------
AssertionError: 1 != 2
----------------------------------------------------------------------
Ran 2 tests in 0.010s

FAILED (failures=1)
"""
VITEST_RED = """\
 ❯ src/a.test.js (1 test | 1 failed) 49ms
 ✓ src/b.test.js (3 tests) 9ms

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 3 passed (4)

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/a.test.js > reader > finds the nested rule
AssertionError: expected [] to have a length of 1

 ❯ src/a.test.js:23:94

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯
"""

RUNNER = r'''
import sys
mode = sys.argv[1]
if mode == "ut-ok":
    print("..\n" + "-" * 70 + "\nRan 2 tests in 0.01s\n\nOK")
elif mode == "vitest-red":
    sys.stdout.write(open(sys.argv[2], encoding="utf-8").read()); sys.exit(1)
elif mode == "pw":
    sys.stdout.write(open(sys.argv[2], encoding="utf-8").read()); sys.exit(int(sys.argv[3]))
'''


class ParserTests(unittest.TestCase):
    def test_unittest_green_and_red(self):
        self.assertEqual(rst.parse_unittest_output(UT_OK)["ran"], 4)
        red = rst.parse_unittest_output(UT_RED)
        self.assertEqual(red["status"], "fail")
        self.assertEqual([f["id"] for f in red["failures"]], ["test_a (pkg.test_x.T.test_a)"])
        self.assertEqual(rst.parse_unittest_output("boom")["status"], "infra")

    def test_unittest_takes_the_last_summary(self):
        quoted = "output quotes Ran 99 tests in 1.0s\nOK\n" + UT_RED
        self.assertEqual(rst.parse_unittest_output(quoted)["ran"], 2)

    def test_vitest_files_summary_and_failures(self):
        p = rst.parse_vitest_output(VITEST_RED)
        self.assertEqual(len(p["files"]), 2)
        self.assertTrue(p["has_result"])
        self.assertEqual([f["id"] for f in p["failures"]], ["src/a.test.js > reader > finds the nested rule"])
        self.assertIn("AssertionError", "\n".join(p["failures"][0]["block"]))

    def test_playwright_footer_and_blocks(self):
        p = rst.parse_playwright_output((FIX / "two_failed_17_passed.log").read_text(encoding="utf-8"))
        self.assertEqual(p["counts"]["failed"], 2)
        self.assertEqual(p["counts"]["passed"], 17)
        self.assertEqual(len(p["listed"]["failed"]), 2)
        self.assertEqual(len(p["blocks"]), 2)
        self.assertTrue(p["running"].startswith("Running 19 tests"))
        green = rst.parse_playwright_output((FIX / "five_passed.log").read_text(encoding="utf-8"))
        self.assertEqual((green["counts"], green["duration"]), ({"passed": 5}, "1.3m"))

    def test_green_line_carries_fixed_vocabulary_only(self):
        line = rst.green_line("phase-2", ["tests 3 tests", "", "e2e"], "a" * 12, "b" * 12, "c" * 12)
        self.assertEqual(line, "phase-2 tests GREEN -- run_scoped_tests tests 3 tests, e2e (exit 0) "
                               f"HEAD {'a' * 12} code-tree {'b' * 12} scripts-tree {'c' * 12}")
        for word in ("red", "fail", "error", "pending", "not run"):
            self.assertNotIn(word, line.lower().replace("green", ""))


class LegCliTests(unittest.TestCase):
    def _setup(self, extra: str = "") -> Path:
        root = te.enter(self, "")
        (root / "runner.py").write_text(RUNNER, encoding="utf-8")
        py = shlex.quote(sys.executable)
        run = f"{py} {shlex.quote(str(root / 'runner.py'))}"
        (root / "vitest.txt").write_text(VITEST_RED, encoding="utf-8")
        cfg = (f'[stack]\nruntime = "none"\n[tests]\n'
               f'scoped_cmd = {te.toml_str(run + " ut-ok {labels}")}\nscoped_parser = "unittest"\n'
               f'scoped_legs = [\n'
               f'  {{ name = "unit", cmd = {te.toml_str(run + " vitest-red " + shlex.quote(str(root / "vitest.txt")))}, parser = "vitest", strip_prefix = "web/" }},\n'
               f'  {{ name = "e2e", cmd = {te.toml_str(run + " pw {labels}")}, parser = "playwright" }},\n'
               f']\n{extra}')
        (root / ".workflow" / "config.toml").write_text(cfg, encoding="utf-8")
        te.clear_caches()
        return root

    def _main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = rst.main(list(argv))
        return code, out.getvalue()

    def test_default_unittest_leg_green(self):
        self._setup()
        code, out = self._main("pkg.tests")
        self.assertEqual(code, 0, out)
        self.assertIn("ran=2", out)

    def test_named_vitest_leg_lists_failures(self):
        self._setup()
        code, out = self._main("--leg", "unit=web/src/a.test.js")
        self.assertEqual(code, 1, out)
        self.assertIn("[SCOPED] unit FAIL", out)
        self.assertIn("FAIL: src/a.test.js > reader > finds the nested rule", out)

    def test_named_playwright_leg_uses_the_exit_code(self):
        self._setup()
        log = FIX / "one_failed.log"
        code, out = self._main("--leg", f"e2e={log}", "--leg", "e2e=1")
        self.assertEqual(code, 1, out)
        self.assertIn("[SCOPED] e2e FAIL exit=1", out)

    def test_unknown_leg_is_a_usage_error(self):
        self._setup()
        code, _ = self._main("--leg", "nope=x")
        self.assertEqual(code, 2)

    def test_scripts_test_path_shape_and_combinations(self):
        self._setup()
        self.assertEqual(self._main("--scripts-test", "other/test_x.py")[0], 2)
        self.assertEqual(self._main("--scripts", "--scripts-test", "scripts/tests/test_x.py")[0], 2)
        code, out = self._main("--scripts-test", "scripts/tests/test_absent.py")
        self.assertEqual(code, 2, out)
        self.assertIn("NOT RUN", out)

    def test_scripts_leg_runs_on_the_host_and_requires_tests(self):
        root = self._setup()
        tests = root / "scripts" / "tests"
        tests.mkdir(parents=True)
        (root / "scripts" / "__init__.py").write_text("")
        (tests / "__init__.py").write_text("")
        (tests / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n"
                                          "    def test_a(self):\n        pass\n")
        code, out = self._main("--scripts")
        self.assertEqual(code, 0, out)
        self.assertIn("[SCOPED] scripts PASS ran=1", out)
        code, out = self._main("--scripts-test", "scripts/tests/test_ok.py")
        self.assertEqual(code, 0, out)
        (tests / "test_ok.py").unlink()
        code, out = self._main("--scripts")
        self.assertEqual(code, 2, out)
        self.assertIn("no tests ran", out)

    def test_require_synced_is_skipped_without_a_stack(self):
        self._setup()
        code, out = self._main("--require-synced", "src/x.py", "pkg")
        self.assertEqual(code, 0, out)
        self.assertIn("--require-synced SKIPPED", out)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, "")
        root = self.root
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("x = 1\n")
        (root / ".gitignore").write_text(".workflow/state/\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")
        _git(root, "branch", "development")
        (root / "runner.py").write_text(RUNNER, encoding="utf-8")
        run = f"{shlex.quote(sys.executable)} {shlex.quote(str(root / 'runner.py'))}"
        reg = root / "docs" / "implementation-artifacts" / "slot-registry.md"
        reg.parent.mkdir(parents=True)
        reg.write_text("| Slot | Status | Story ID | Branch | Worktree | Since |\n"
                       "|------|--------|----------|--------|----------|-------|\n"
                       f"| 1 | in_use | 7-2 | main | {root} | 2026-01-01 |\n", encoding="utf-8")
        (root / ".workflow" / "config.toml").write_text(
            f'[stack]\nruntime = "none"\n[tests]\ncode_root = "src"\n'
            f'scoped_cmd = {te.toml_str(run + " ut-ok {labels}")}\nscoped_parser = "unittest"\n'
            f'record_invariant_labels = ["inv.label"]\n'
            f'record_checks = [{{ name = "lint", cmd = {te.toml_str(shlex.quote(sys.executable) + " -c pass")} }}]\n',
            encoding="utf-8")
        te.clear_caches()

    def _main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = rst.main(list(argv))
        return code, out.getvalue()

    def test_tested_tree_matches_the_committed_subtree_and_includes_untracked(self):
        head, tree = rst.tested_tree(str(self.root))
        self.assertTrue(_git(self.root, "rev-parse", "HEAD:src").startswith(tree))
        (self.root / "src" / "new.py").write_text("y = 2\n")
        self.assertNotEqual(rst.tested_tree(str(self.root))[1], tree)
        self.assertEqual(_git(self.root, "status", "--porcelain", "--", "src"), "?? src/new.py")

    def test_green_record_appends_the_line_and_saves_the_command(self):
        code, out = self._main("--record", "7-2", "phase-2", "pkg.tests")
        self.assertEqual(code, 0, out)
        self.assertIn("[SCOPED] lint PASS", out)
        records = list(self.root.rglob("7-2-record.md"))
        self.assertEqual(len(records), 1, out)
        text = records[0].read_text(encoding="utf-8")
        self.assertRegex(text, r"phase-2 tests GREEN -- run_scoped_tests tests 2 tests, lint \(exit 0\) "
                               r"HEAD [0-9a-f]{12} code-tree [0-9a-f]{12}")
        cmd = self.root / ".workflow" / "state" / "story-setup" / "7-2-phase-2.command"
        self.assertIn("--record 7-2 phase-2", cmd.read_text(encoding="utf-8"))

    def test_record_for_a_story_without_a_slot_is_refused(self):
        code, out = self._main("--record", "9-9", "phase-2", "pkg.tests")
        self.assertEqual(code, 2, out)
        self.assertEqual(list(self.root.rglob("9-9-record.md")), [])

    def test_bad_phase_is_a_usage_error(self):
        self.assertEqual(self._main("--record", "7-2", "phase-9", "pkg")[0], 2)

    def test_full_gate_record_appends_the_pre_merge_line(self):
        import run_full_suite
        cfg = self.root / ".workflow" / "config.toml"
        run = f"{shlex.quote(sys.executable)} {shlex.quote(str(self.root / 'runner.py'))}"
        cfg.write_text(cfg.read_text() + f"full_cmd = {te.toml_str(run + ' ut-ok')}\n", encoding="utf-8")
        te.clear_caches()
        real = run_full_suite.LOCK_DIR
        run_full_suite.LOCK_DIR = self.root / "locks"
        self.addCleanup(setattr, run_full_suite, "LOCK_DIR", real)
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_full_suite.main(["--story-id", "7-2", "--record"])
        self.assertEqual(code, 0, out.getvalue())
        text = next(self.root.rglob("7-2-record.md")).read_text(encoding="utf-8")
        self.assertRegex(text, r"pre-merge gate GREEN -- run_full_suite all legs \(exit 0\) HEAD [0-9a-f]{12} "
                               r"code-tree [0-9a-f]{12} base [0-9a-f]{12}")


if __name__ == "__main__":
    unittest.main()
