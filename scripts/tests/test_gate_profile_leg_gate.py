"""run_full_suite.py with serving profiles and `--profile-leg`:

- no profiles configured: the gate runs unchanged and says profiles are SKIPPED; `--profile-leg` is a
  config error (exit 2) and runs nothing;
- the gate legs run under the gate profile; one that cannot be entered fails the legs that need it
  (NO RESULT) and nothing else;
- `--profile-leg` runs its leg alone under its profile after the concurrent legs, whatever they returned;
- the stack always ends in its resting profile; a failed return fails an otherwise green gate;
- a green `--profile-leg` run appends the counter entry; a red one, or a plain gate, does not.
"""
from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import gate_profile  # noqa: E402
import profile_leg  # noqa: E402
import run_full_suite  # noqa: E402

RUNNER = r'''
import sys
if sys.argv[1] == "green":
    print("Ran 3 tests in 0.1s"); print("OK")
else:
    print("FAIL: pkg.test_one"); print("Ran 3 tests in 0.1s"); print("FAILED"); sys.exit(1)
'''


class ProfiledGateTests(unittest.TestCase):
    def setup(self, *, full="green", e2e="green", leg="green", profiles=True, gate_legs=None):
        root = te.enter(self, "")
        (root / "runner.py").write_text(RUNNER, encoding="utf-8")
        for args in (["init", "-q", "-b", "development"], ["config", "user.email", "t@example.invalid"],
                     ["config", "user.name", "t"], ["add", "runner.py"], ["commit", "-q", "-m", "root"]):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        py = shlex.quote(sys.executable)
        run = lambda m: te.toml_str(f"{py} {shlex.quote(str(root / 'runner.py'))} {m}")  # noqa: E731
        cfg = ['[stack]', 'runtime = "none"', '[tests]', f"full_cmd = {run(full)}", f"e2e_cmd = {run(e2e)}"]
        if profiles:
            cfg += ['[profiles]', 'gate = "fake"', 'story_resting = "fake"', 'shared_resting = "real"']
            if gate_legs is not None:
                cfg.append(f"gate_legs = {json.dumps(gate_legs)}")
            cfg += ['[profiles.defs.fake]', 'probe_cmd = "x"', '[profiles.defs.real]', 'probe_cmd = "x"',
                    '[profile_leg]', 'profile = "real"', 'label = "REAL"', f"cmd = {run(leg)}"]
        (root / ".workflow" / "config.toml").write_text("\n".join(cfg) + "\n", encoding="utf-8")
        te.clear_caches()
        real = run_full_suite.LOCK_DIR
        run_full_suite.LOCK_DIR = root / "locks"
        self.addCleanup(setattr, run_full_suite, "LOCK_DIR", real)
        self.calls: list[str] = []
        self.state = {"profile": "real"}   # the shared stack rests real; the gate needs fake
        self.fail_enter: set[str] = set()

        def ensure(stack, profile):
            self.calls.append(f"ensure {profile.name}")
            if profile.name in self.fail_enter:
                return False, f"{profile.name} would not start"
            self.state["profile"] = profile.name
            return True, ""

        p = mock.patch.object(gate_profile, "ensure", ensure)
        p.start()
        self.addCleanup(p.stop)
        return root

    def main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = run_full_suite.main(list(argv))
        return rc, out.getvalue()

    def test_without_profiles_the_gate_is_unchanged_and_the_leg_is_a_config_error(self):
        self.setup(profiles=False)
        rc, out = self.main()
        self.assertEqual(rc, 0, out)
        self.assertIn("profiles: SKIPPED", out)
        rc, out = self.main("--profile-leg")
        self.assertEqual(rc, 2, out)
        self.assertNotIn("all green", out)

    def test_a_per_merge_gate_enters_the_gate_profile_and_returns_to_rest(self):
        self.setup()
        rc, out = self.main()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls, ["ensure fake", "ensure real"])
        self.assertIsNone(profile_leg.last_green(profile_leg.state_path()))

    def test_the_profile_leg_runs_after_the_legs_and_a_green_run_is_counted(self):
        self.setup()
        rc, out = self.main("--profile-leg")
        self.assertEqual(rc, 0, out)
        self.assertIn("REAL:", out)
        # the leg's profile is the shared stack's resting one: no restore switch needed
        self.assertEqual(self.calls, ["ensure fake", "ensure real"])
        self.assertIsNotNone(profile_leg.last_green(profile_leg.state_path()))

    def test_the_profile_leg_runs_even_when_a_leg_failed_and_a_red_run_is_not_counted(self):
        self.setup(full="red")
        rc, out = self.main("--profile-leg")
        self.assertEqual(rc, 1, out)
        self.assertIn("REAL", out)
        self.assertEqual(self.calls, ["ensure fake", "ensure real"])
        self.assertIsNone(profile_leg.last_green(profile_leg.state_path()))

    def test_a_gate_profile_that_cannot_be_entered_fails_only_the_legs_that_need_it(self):
        self.setup(gate_legs=["E2E"])
        self.fail_enter.add("fake")
        rc, out = self.main()
        self.assertEqual(rc, 1, out)
        self.assertIn("E2E: NO RESULT — profile not entered: fake would not start", out)
        self.assertIn("FULL: PASSED", out)

    def test_a_failed_return_to_rest_fails_a_green_gate(self):
        self.setup()
        self.fail_enter.add("real")   # entering fake works; returning to real does not
        rc, out = self.main()
        self.assertEqual(rc, 1, out)
        self.assertIn("RESTORE: FAILED", out)

    def test_train_and_profile_leg_are_exclusive(self):
        self.setup()
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                run_full_suite.main(["--story-id", "1-2", "--record", "--train", "--profile-leg"])


class SummaryTests(unittest.TestCase):
    def test_restore_failure_is_named_and_not_green(self):
        res = {"FULL": {"exit": 0, "text": "Ran 1 test in 0.1s\nOK\n"}}
        import re
        code, text = run_full_suite.summarize(["FULL"], res, restore_failure="x",
                                              fail_re=re.compile(r"^FAIL: (\S+)", re.M),
                                              summary_re=re.compile(r"^Ran .*", re.M))
        self.assertEqual(code, 1)
        self.assertIn("RESTORE: FAILED (x)", text)


if __name__ == "__main__":
    unittest.main()
