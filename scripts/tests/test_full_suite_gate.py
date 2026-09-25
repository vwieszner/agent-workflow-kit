"""Verdict tests for run_full_suite.py — the summary is the only thing anyone reads,
so every way a red gate could print green is pinned here:

- a green gate reports each leg's result line (a silently shrunken suite stays visible);
- a red leg flips the verdict and the headline names the leg;
- a post-phase failing after every leg passed still fails the gate, distinctly;
- a configured leg with no result is not green (fail closed);
- a leg exiting 0 while printing failing ids is not green;
- the legs really run concurrently, a pre-phase abort skips the legs and propagates its
  exit code, and the full failing-id list reaches stdout (end-to-end through main()).
"""

from __future__ import annotations

import io
import re
import shlex
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import run_full_suite  # noqa: E402

FAIL_RE = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)
SUMMARY_RE = re.compile(r"^(?:Ran \d+ tests?|=+ .* in [\d.]+s)", re.MULTILINE)

GREEN_FULL = "....\nRan 2694 tests in 214.234s\n\nOK (skipped=3)\n"
GREEN_E2E = "===== 26 passed in 45.5s =====\n"
RED_FULL = ("F.E\nFAIL: pkg.tests.test_a\nAssertionError\n"
            "ERROR: pkg.tests.test_b\nRuntimeError: boom\nRan 3 tests in 0.2s\n\nFAILED (failures=1, errors=1)\n")

RUNNER = r'''
import os, sys, time
mode = sys.argv[1]
if mode == "green":
    print("Ran 3 tests in 0.1s"); print("OK")
elif mode == "red":
    print("FAIL: pkg.test_one"); print("ERROR: pkg.test_two"); print("Ran 3 tests in 0.1s"); print("FAILED")
    sys.exit(1)
elif mode == "sleep":
    time.sleep(float(sys.argv[2])); print("Ran 1 test in 1.0s"); print("OK")
elif mode == "exit":
    sys.exit(int(sys.argv[2]))
'''


def _summ(expected, results, **kw):
    return run_full_suite.summarize(expected, results, fail_re=FAIL_RE, summary_re=SUMMARY_RE, **kw)


class SummaryVerdictTests(unittest.TestCase):
    def test_green_gate_reports_each_legs_result_line(self):
        code, out = _summ(["FULL", "E2E"], {"FULL": {"exit": 0, "text": GREEN_FULL},
                                            "E2E": {"exit": 0, "text": GREEN_E2E}})
        self.assertEqual(code, 0, out)
        self.assertIn("all green", out)
        self.assertIn("2694", out, "the green summary never shows how many tests ran")
        self.assertIn("26 passed", out)

    def test_a_red_leg_flips_the_verdict_and_is_named(self):
        code, out = _summ(["FULL", "E2E"], {"FULL": {"exit": 1, "text": RED_FULL},
                                            "E2E": {"exit": 0, "text": GREEN_E2E}})
        self.assertEqual(code, 1, out)
        headline = out.splitlines()[0]
        self.assertIn("FULL F(exit 1)", headline)
        self.assertIn("pkg.tests.test_a", out)
        self.assertIn("pkg.tests.test_b", out)

    def test_a_post_phase_failure_after_green_legs_fails_the_gate(self):
        code, out = _summ(["FULL"], {"FULL": {"exit": 0, "text": GREEN_FULL}},
                          phases_exit=1, failed_phase="coverage-check")
        self.assertEqual(code, 1, out)
        self.assertIn("gate exited non-zero", out)
        self.assertIn("coverage-check", out)

    def test_a_leg_with_no_result_is_not_green(self):
        code, out = _summ(["FULL", "E2E"], {"FULL": {"exit": 0, "text": GREEN_FULL}})
        self.assertEqual(code, 1, f"a gate whose e2e leg never reported was green:\n{out}")
        self.assertIn("E2E ?", out.splitlines()[0])

    def test_exit_zero_with_failing_ids_is_not_green(self):
        code, out = _summ(["FULL"], {"FULL": {"exit": 0, "text": RED_FULL}})
        self.assertEqual(code, 1, out)

    def test_a_pre_phase_abort_is_reported_as_such(self):
        code, out = _summ(["FULL"], {}, phases_exit=3, failed_phase="lint", log_tail=["lint: bad"])
        self.assertEqual(code, 1)
        self.assertIn("gate aborted in phase lint", out)
        self.assertIn("lint: bad", out)


class GateEndToEndTests(unittest.TestCase):
    def _setup(self, *, full: str, e2e: str = "", pre: str = "", post: str = ""):
        py = shlex.quote(sys.executable)
        cfg = ['[stack]', 'runtime = "none"', '[tests]']
        root_holder = {}

        def runner(args: str) -> str:
            return f"{py} {shlex.quote(str(root_holder['root'] / 'runner.py'))} {args}"

        # Root is only known after enter(); write config afterwards.
        root = te.enter(self, "")
        root_holder["root"] = root
        (root / "runner.py").write_text(RUNNER, encoding="utf-8")
        cfg.append(f"full_cmd = {te.toml_str(runner(full))}")
        if e2e:
            cfg.append(f"e2e_cmd = {te.toml_str(runner(e2e))}")
        if pre:
            cfg.append(f"full_pre_phases = [{{ name = \"pre\", cmd = {te.toml_str(runner(pre))} }}]")
        if post:
            cfg.append(f"full_post_phases = [{{ name = \"post\", cmd = {te.toml_str(runner(post))} }}]")
        (root / ".workflow" / "config.toml").write_text("\n".join(cfg) + "\n", encoding="utf-8")
        te.clear_caches()
        real = run_full_suite.LOCK_DIR
        run_full_suite.LOCK_DIR = root / "locks"
        self.addCleanup(setattr, run_full_suite, "LOCK_DIR", real)
        return root

    def _main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_full_suite.main(list(argv))
        return code, out.getvalue()

    def test_green_run_exits_zero(self):
        self._setup(full="green", e2e="green")
        code, out = self._main()
        self.assertEqual(code, 0, out)
        self.assertIn("all green", out)
        self.assertIn("preflight skipped", out)

    def test_red_run_lists_every_failing_id(self):
        self._setup(full="red", e2e="green")
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn("pkg.test_one", out)
        self.assertIn("pkg.test_two", out)

    def test_legs_run_concurrently(self):
        self._setup(full="sleep 1.5", e2e="sleep 1.5")
        start = time.monotonic()
        code, out = self._main()
        elapsed = time.monotonic() - start
        self.assertEqual(code, 0, out)
        self.assertLess(elapsed, 2.9, f"two 1.5s legs took {elapsed:.1f}s — they ran serially")

    def test_pre_phase_abort_skips_legs_and_propagates_its_exit(self):
        self._setup(full="green", pre="exit 3")
        code, out = self._main()
        self.assertEqual(code, 3, out)
        self.assertIn("gate aborted in phase pre", out)

    def test_post_phase_failure_fails_the_gate(self):
        self._setup(full="green", post="exit 4")
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn("gate exited non-zero", out)

    def test_unset_full_cmd_is_a_config_error(self):
        te.enter(self, '[stack]\nruntime = "none"\n')
        code, out = self._main()
        self.assertEqual(code, 2, out)

    def test_unknown_story_id_is_a_registry_error(self):
        self._setup(full="green")
        code, out = self._main("--story-id", "9-99")
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
