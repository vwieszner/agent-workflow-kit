"""gate_profile.py: profiles come from config; the switch recreates the configured services with the
profile value in the configured env var and every slot port var, from the stack's own tree; the
steps run drain → recreate → ready → verify and stop at the first failure; ensure() reads first and
switches only on a mismatch; a failed or garbled probe is a problem, never a pass."""
from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import gate_profile as gp  # noqa: E402

CONFIG = """
[stack]
runtime = "docker-compose"
compose_cmd = "docker compose"
services = ["app", "worker", "db"]
[stack.ports]
APP_PORT = 8000
[slots]
port_stride = 10
[profiles]
gate = "fake"
story_resting = "fake"
shared_resting = "real"
services = ["app", "worker"]
env_var = "APP_PROFILE"
drain_cmd = "drain {stack}"
drain_timeout_s = 10
ready_timeout_s = 10
poll_s = 1
[profiles.defs.fake]
value = "settings.fake"
probe_cmd = "probe {service} {stack}"
expect = { MODEL = "Fake", COLLECTION = "c_gate" }
[profiles.defs.real]
value = "settings.real"
probe_cmd = "probe {service} {stack}"
expect = { MODEL = "real", COLLECTION = "c" }
"""


def done(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, out, "")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)
        self.stack = gp.Stack("story-1-2", 1, str(self.root), False)
        self.clock = [0.0]
        for name, fn in (("_sleep", lambda s: self.clock.__setitem__(0, self.clock[0] + s)),
                         ("_monotonic", lambda: self.clock[0])):
            p = mock.patch.object(gp, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)


class ConfigTests(Base):
    def test_profiles_and_resting(self):
        self.assertTrue(gp.enabled())
        self.assertEqual(gp.gate_profile().value, "settings.fake")
        self.assertEqual(gp.resting_profile(self.stack).name, "fake")
        self.assertEqual(gp.resting_profile(gp.Stack("proj", 0, str(self.root), True)).name, "real")

    def test_an_undefined_profile_is_an_error(self):
        with self.assertRaises(gp.ProfileError):
            gp.load("nope")

    def test_services_fall_back_to_stack_services(self):
        self.assertEqual(gp.services(), ["app", "worker"])

    def test_switch_command_env_and_cwd(self):
        cmd = gp.switch_cmd(self.stack)
        self.assertEqual(cmd[-7:], ["up", "-d", "--build", "--force-recreate", "--no-deps", "app", "worker"])
        self.assertIn("story-1-2", cmd)
        env = gp.switch_env(self.stack, gp.load("real"))
        self.assertEqual(env["APP_PROFILE"], "settings.real")
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], "story-1-2")
        self.assertIn("APP_PORT", env)
        seen = {}
        with mock.patch.object(gp, "_run", lambda c, **kw: seen.update(kw) or done(3)):
            self.assertEqual(gp.switch(self.stack, gp.load("real")), 3)
        self.assertEqual(Path(seen["cwd"]), self.root)


class VerifyTests(Base):
    def probes(self, replies):
        def fake(cmd, **kw):
            svc = cmd.split()[1]
            r = replies[svc]
            return r if isinstance(r, subprocess.CompletedProcess) else done(0, f"noise\nPROFILE {r}\n")
        return mock.patch.object(gp, "_run", fake)

    def test_matching_services_have_no_problems_and_strings_compare_case_insensitively(self):
        ok = '{"MODEL": "fake", "COLLECTION": "c_gate"}'
        with self.probes({"app": ok, "worker": ok}):
            self.assertEqual(gp.verify(self.stack, gp.load("fake")), [])

    def test_each_mismatch_is_named(self):
        with self.probes({"app": '{"MODEL": "fake", "COLLECTION": "c"}', "worker": done(1)}):
            problems = gp.verify(self.stack, gp.load("fake"))
        self.assertTrue(any("app: COLLECTION is 'c'" in p for p in problems))
        self.assertTrue(any(p.startswith("worker: the running profile could not be read") for p in problems))

    def test_garbled_probe_is_a_problem(self):
        self.assertIsNone(gp.parse_marked(done(0, "PROFILE {nope")))
        self.assertIsNone(gp.parse_marked(done(0, "PROFILE [1]")))


class StepTests(Base):
    def test_order_and_stop_at_first_failure(self):
        calls = []
        with mock.patch.object(gp, "drain", lambda s: calls.append("drain") or True), \
                mock.patch.object(gp, "switch", lambda s, p: calls.append("switch") or 0), \
                mock.patch.object(gp, "wait_ready", lambda s: calls.append("ready") or True), \
                mock.patch.object(gp, "verify", lambda s, p: calls.append("verify") or []):
            self.assertEqual(gp.prepare(self.stack, gp.load("real")), (True, ""))
        self.assertEqual(calls, ["drain", "switch", "ready", "verify"])
        self.assertIn("recreate", self.out.getvalue())
        with mock.patch.object(gp, "drain", lambda s: False), \
                mock.patch.object(gp, "switch", lambda s, p: self.fail("switched after a drain timeout")):
            ok, why = gp.prepare(self.stack, gp.load("real"))
        self.assertFalse(ok)
        self.assertIn("drain timed out", why)
        with mock.patch.object(gp, "drain", lambda s: True), mock.patch.object(gp, "switch", lambda s, p: 4), \
                mock.patch.object(gp, "wait_ready", lambda s: self.fail("waited after a failed recreate")):
            self.assertEqual(gp.prepare(self.stack, gp.load("real"))[1], "recreate for the real profile exited 4")

    def test_drain_polls_until_exit_zero_or_timeout(self):
        replies = iter([done(1, "2 queued"), done(0)])
        with mock.patch.object(gp, "_run", lambda c, **kw: next(replies)):
            self.assertTrue(gp.drain(self.stack))
        with mock.patch.object(gp, "_run", lambda c, **kw: done(1, "busy")):
            self.assertFalse(gp.drain(self.stack))

    def test_ready_waits_for_every_switched_service(self):
        replies = iter([{"app"}, {"app", "worker"}])
        with mock.patch.object(gp, "_ready", lambda s: next(replies)):
            self.assertTrue(gp.wait_ready(self.stack))
        with mock.patch.object(gp, "_ready", lambda s: {"app"}):
            self.assertFalse(gp.wait_ready(self.stack))

    def test_ensure_reads_first_and_switches_only_on_a_mismatch(self):
        with mock.patch.object(gp, "verify", lambda s, p: []), \
                mock.patch.object(gp, "prepare", lambda s, p: self.fail("switched a stack already in the profile")):
            self.assertEqual(gp.ensure(self.stack, gp.load("fake")), (True, ""))
        with mock.patch.object(gp, "verify", lambda s, p: ["app: MODEL is 'real'"]), \
                mock.patch.object(gp, "prepare", lambda s, p: (False, "boom")):
            self.assertEqual(gp.ensure(self.stack, gp.load("fake")), (False, "boom"))
        self.assertIn("app: MODEL is 'real'", self.out.getvalue())


class DisabledTests(unittest.TestCase):
    def test_no_gate_profile_is_disabled_and_the_cli_says_skipped(self):
        te.enter(self, "[profiles]\n")
        self.assertFalse(gp.enabled())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(gp.main(["status"]), 0)
            self.assertEqual(gp.main(["ensure", "x"]), 2)
        self.assertIn("SKIPPED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
