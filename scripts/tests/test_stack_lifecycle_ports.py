"""Stack lifecycle helpers: the bring-up seed and its fresh-stack outcomes, the waiter's retry
anchor, bring_up_story_stack's shared-stack refusals and `.env` seeding, check_container_sync
--absent reporting, post_merge_smoke's stopped-stack diagnosis, and the integrity check's
repo-relative hook paths."""
from __future__ import annotations

import contextlib
import io
import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import bring_up_stack_async as bsa  # noqa: E402
import bring_up_story_stack as bss  # noqa: E402
import check_container_sync as ccs  # noqa: E402
import check_settings_and_docs_integrity as integ  # noqa: E402
import post_merge_smoke  # noqa: E402
import wait_for_stack_ready as wsr  # noqa: E402

PY = shlex.quote(sys.executable)


class WaiterAnchorTests(unittest.TestCase):
    def test_the_later_anchor_decides(self):
        self.assertEqual(wsr.spent_anchor(10, None, 5), "the waiter start")
        self.assertIsNone(wsr.spent_anchor(4, None, 5))
        self.assertIsNone(wsr.spent_anchor(10, 3, 5))  # a retry started after the waiter: its own budget
        self.assertEqual(wsr.spent_anchor(10, 6, 5), "the retry start")
        self.assertEqual(wsr.spent_anchor(10, 20, 5), "the waiter start")  # an older retry never shortens it

    def test_retry_started_reads_only_a_retrying_record(self):
        self.assertIsNone(wsr.retry_started({"status": "starting"}))
        self.assertIsNone(wsr.retry_started({"status": "retrying"}))
        stamp = wsr.retry_started({"status": "retrying", "retry_started_at": "2026-01-01T10:00:00+00:00"})
        self.assertEqual(stamp.isoformat(), "2026-01-01T10:00:00+00:00")


class SeedTests(unittest.TestCase):
    def _cfg(self, seed: str, absent: str = "") -> None:
        root = te.enter(self, "")
        (root / ".workflow" / "config.toml").write_text(
            f'[stack]\nseed_cmd = {te.toml_str(seed)}\nseed_absent_regex = {te.toml_str(absent)}\n', encoding="utf-8")
        te.clear_caches()

    def test_outcomes(self):
        self._cfg(f"{PY} -c pass")
        self.assertEqual(bsa.run_ui_seed("story-1-1", ".", {}), "ok")
        self.assertEqual(bsa.run_ui_seed("shared", ".", {}), "skipped")
        self._cfg(f"{PY} -c \"import sys; print(sys.argv[1]); sys.exit(3)\" boom")
        self.assertEqual(bsa.run_ui_seed("story-1-1", ".", {}), "failed: boom")
        self._cfg(f"{PY} -c \"import sys; print(sys.argv[1]); sys.exit(1)\" Unknown-command", "Unknown-command")
        self.assertEqual(bsa.run_ui_seed("story-1-1", ".", {}), "absent")
        self._cfg(f"{PY} -c pass")
        self.assertTrue(bsa.run_ui_seed("story-1-1", "/no/such/dir", {}).startswith("error: "))

    def test_no_seed_configured_is_skipped(self):
        te.enter(self, "")
        self.assertEqual(bsa.run_ui_seed("story-1-1", ".", {}), "skipped")


class BringUpStoryStackTests(unittest.TestCase):
    def setUp(self):
        te.enter(self, '[slots]\nshared_stack_name = "proj"\n[stack]\nruntime = "docker-compose"\n')

    def _main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                return bss.main(list(argv)), err.getvalue()
            except SystemExit as exc:
                return exc.code, err.getvalue()

    def test_stop_down_and_seed_are_refused_on_the_shared_stack(self):
        for mode in ("stop", "down"):
            code, err = self._main("--slot", "0", "--project-name", "proj", "--worktree", ".", "--mode", mode)
            self.assertEqual(code, 2, mode)
            self.assertIn("refused", err)
        self.assertEqual(self._main("--project-name", "proj", "--mode", "seed")[0], 2)

    def test_seed_mode_takes_no_slot_and_other_modes_need_one(self):
        self.assertEqual(self._main("--slot", "1", "--project-name", "story-1", "--mode", "seed")[0], 2)
        self.assertEqual(self._main("--project-name", "story-1", "--mode", "up")[0], 2)

    def test_story_env_is_appended_without_touching_existing_keys(self):
        root = te.enter(self, "")
        env = root / ".env"
        env.write_text("A=keep\n# B=comment\n", encoding="utf-8")
        self.assertEqual(bss.seed_env_file(env, {"A": "new", "B": "2"}), ["B"])
        self.assertEqual(env.read_text(encoding="utf-8"), "A=keep\n# B=comment\nB=2\n")
        self.assertEqual(bss.seed_env_file(env, {"A": "new", "B": "2"}), [])


class AbsentReportTests(unittest.TestCase):
    def test_gone_left_unanswered_and_host_present(self):
        rows = [
            {"rel": "a.py", "crel": "a.py", "root": "/app", "host_present": False,
             "services": ("web", "worker"), "containers": {"web": "absent", "worker": "present"}},
            {"rel": "b.py", "crel": "b.py", "root": "/app", "host_present": False,
             "services": ("web",), "containers": {"web": None}},
            {"rel": "c.py", "crel": "c.py", "root": "/app", "host_present": True, "services": ("web",),
             "containers": {}},
            {"rel": "x/d.py", "crel": None, "root": None, "host_present": False, "services": None, "containers": {}},
        ]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            left, unanswered, on_host, checked, skipped = ccs.report_absent(rows)
        self.assertEqual(left, [("a.py", "worker")])
        self.assertEqual(unanswered, [("b.py", "web")])
        self.assertEqual(on_host, ["c.py"])
        self.assertEqual((checked, skipped), (2, 1))
        self.assertIn("[GONE] a.py", out.getvalue())


class SmokeStoppedStackTests(unittest.TestCase):
    def test_every_service_down_is_diagnosis_s(self):
        te.enter(self, '[slots]\nshared_stack_name = "proj"\n[stack]\nruntime = "docker-compose"\n'
                       'services = ["web", "worker"]\n')
        ps = "\n".join(json.dumps({"Service": s, "State": "exited", "Status": "Exited (0) 1 hour ago"})
                       for s in ("web", "worker"))
        calls = []

        def fake_run(cmd, cwd=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, ps, "")

        with mock.patch.object(post_merge_smoke, "run", fake_run), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = post_merge_smoke.main()
        self.assertEqual(code, 1)
        self.assertIn("[Diagnosis S]", out.getvalue())
        self.assertIn("-a", calls[0])
        self.assertEqual(len(calls), 1, "no log or sync step runs on a stopped stack")


class IntegrityPathTests(unittest.TestCase):
    def test_an_absolute_main_checkout_path_is_made_repo_relative(self):
        roots = (Path("/work/wt/1-1"), Path("/work/proj"))
        self.assertEqual(integ.repo_relative("/work/proj/.workflow/hooks/guards/x.py", roots),
                         ".workflow/hooks/guards/x.py")
        self.assertEqual(integ.repo_relative("./scripts/y.py", roots), "scripts/y.py")
        self.assertTrue(integ.SCRIPT_TOKEN.fullmatch("C:/work/proj/scripts/y.py"))


if __name__ == "__main__":
    unittest.main()
