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
import subprocess
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

    def test_full_cmd_missing_a_required_flag_is_a_config_error(self):
        root = self._setup(full="green")
        cfg = root / ".workflow" / "config.toml"
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       + '[tests_policy]\nrequired_flags = ["--noinput"]\n', encoding="utf-8")
        te.clear_caches()
        code, out = self._main()
        self.assertEqual(code, 2, out)
        self.assertIn("--noinput", out)

    def test_schema_drift_is_skipped_and_announced_when_unset(self):
        self._setup(full="green")
        code, out = self._main()
        self.assertEqual(code, 0, out)
        self.assertIn("schema-drift: SKIPPED", out)

    def test_schema_drift_runs_first_when_a_schema_file_changed(self):
        root = self._setup(full="green")
        git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "base"], check=True)
        subprocess.run([*git, "checkout", "-qb", "story/1"], check=True)
        (root / "migrations").mkdir()
        (root / "migrations" / "0001_initial.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "schema"], check=True)
        cfg = root / ".workflow" / "config.toml"
        drift = f"{shlex.quote(sys.executable)} {shlex.quote(str(root / 'runner.py'))} exit 1"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            "[tests]\n", f"[tests]\nschema_drift_cmd = {te.toml_str(drift)}\n")
            + '[git]\nbase_branch = "main"\n[pipeline]\nschema_globs = ["**/migrations/*.py"]\n',
            encoding="utf-8")
        te.clear_caches()
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn("schema-drift: 1 schema file(s) changed", out)
        self.assertIn("phase schema-drift", out)

    def test_unset_full_cmd_is_a_config_error(self):
        te.enter(self, '[stack]\nruntime = "none"\n')
        code, out = self._main()
        self.assertEqual(code, 2, out)

    def test_unknown_story_id_is_a_registry_error(self):
        self._setup(full="green")
        code, out = self._main("--story-id", "9-999")
        self.assertEqual(code, 2, out)


class TrainGateTests(unittest.TestCase):
    """`--train`: the train checks run under the gate lock (a failed check exits 75 and withdraws nothing);
    a green train gate records the train GREEN line on the train branch and marks the train green; a red one
    withdraws the approval through merge_train.withdraw."""

    _setup = GateEndToEndTests._setup
    _main = GateEndToEndTests._main

    def _fakes(self, root, reason=None):
        from unittest import mock
        calls = []

        class LL:
            LeaseError = OSError
            @staticmethod
            def default_path():
                return root / "lease.json"
            @staticmethod
            def read(path):
                return None

        class MT:
            TrainError = RuntimeError
            NOT_RUN_MARKER = "train gate not run:"
            NOT_RUN_EXIT = 75
            @staticmethod
            def not_run_reason(story, worktree, expected):
                calls.append(("check", expected))
                return reason
            @staticmethod
            def begin_gate(story, path):
                calls.append(("begin", story))
                return ["1-1"]
            @staticmethod
            def finish_green(story, path):
                calls.append(("green", story))
            @staticmethod
            def withdraw(story, rc, head, path):
                calls.append(("withdraw", rc))
            @staticmethod
            def reset_train(story, path):
                calls.append(("reset", story))
            @staticmethod
            def mark_holder(story, worktree, state):
                calls.append(("holder", state))
            @staticmethod
            def heartbeat(story, path):
                import contextlib
                return contextlib.nullcontext()

        appended = []
        ctx = {"worktree": str(root), "stack": "story-1-2", "branch": "story/1-2", "story_id": "1-2", "slot": 1}
        for target, value in (("_merge_train", lambda: MT), ("_land_lease", lambda: LL),
                              ("resolve_target", lambda *a: (dict(ctx), None)),
                              ("base_of", lambda w: "b" * 40)):
            patcher = mock.patch.object(run_full_suite, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        import run_scoped_tests, story_record
        for mod, name, value in ((run_scoped_tests, "tested_tree", lambda w: ("h" * 12, "t" * 12)),
                                 (story_record, "cmd_append", lambda sid, fact: appended.append(fact) or 0)):
            patcher = mock.patch.object(mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls, appended

    def test_a_failed_train_check_exits_75_and_withdraws_nothing(self):
        root = self._setup(full="green")
        calls, appended = self._fakes(root, reason="it is not 1-2's gate turn")
        code, out = self._main("--story-id", "1-2", "--record", "--train")
        self.assertEqual(code, 75, out)
        self.assertIn("train gate not run: it is not 1-2's gate turn", out)
        self.assertEqual(calls, [("check", "train/1-2")])
        self.assertEqual(appended, [])

    def test_a_green_train_gate_records_the_train_line_and_marks_it_green(self):
        root = self._setup(full="green")
        calls, appended = self._fakes(root)
        code, out = self._main("--story-id", "1-2", "--record", "--train")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(appended), 1)
        self.assertIn("pre-merge gate GREEN -- run_full_suite all legs on train/1-2 (exit 0)", appended[0])
        self.assertTrue(appended[0].endswith("train 1-1"))
        self.assertIn(("green", "1-2"), calls)
        self.assertNotIn(("withdraw", 1), calls)

    def test_a_red_train_gate_withdraws_through_merge_train(self):
        root = self._setup(full="red")
        calls, appended = self._fakes(root)
        code, out = self._main("--story-id", "1-2", "--record", "--train")
        self.assertEqual(code, 1, out)
        self.assertIn(("withdraw", 1), calls)
        self.assertEqual(appended, [])

    def test_the_window_gate_marks_the_holder_gating_then_green(self):
        root = self._setup(full="green")
        calls, appended = self._fakes(root)
        code, out = self._main("--story-id", "1-2", "--record")
        self.assertEqual(code, 0, out)
        self.assertEqual([c for c in calls if c[0] == "holder"], [("holder", "gating"), ("holder", "green")])
        self.assertIn("pre-merge gate GREEN -- run_full_suite all legs (exit 0)", appended[0])


if __name__ == "__main__":
    unittest.main()
