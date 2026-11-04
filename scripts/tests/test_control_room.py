"""Tests for the control room: control_room.py (routes, request checks, documents), control_room_launch.py (window
backends, tool commands, the launch record and its guard, process probing) and auto_dev_metrics.py (the run and
compare documents).

No test starts an agent, a terminal or tmux: the launcher's seams (`which`, `spawn_window`, `run_starter`,
`process_alive`) are replaced. The server tests bind 127.0.0.1 on a free port and seed the document cache; one test
runs a real `story_state.py --attention` refresh against a throwaway main checkout.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import auto_dev_metrics as adm  # noqa: E402
import control_room as cr  # noqa: E402
import control_room_launch as launch  # noqa: E402

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def make_repo(test, config: str = "") -> Path:
    root = te.enter(test, '[stack]\nruntime = "none"\n[git]\nbase_branch = "main"\n' + config)
    subprocess.run(["git", *GIT_ID, "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", *GIT_ID, "-C", str(root), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    arts = root / "docs" / "implementation-artifacts"
    arts.mkdir(parents=True)
    (arts / "sprint-status.yaml").write_text("development_status:\n  1-1-a: in-progress\n")
    (arts / "slot-registry.md").write_text("| Slot | Status | Story ID | Branch | Worktree | Since |\n|---|---|---|---|---|---|\n")
    return root


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        self.t += 1
        return self.t


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)

    def set_config(self, extra: str):
        cfg = self.root / ".workflow" / "config.toml"
        cfg.write_text(cfg.read_text() + extra)
        te.clear_caches()

    def test_auto_picks_tmux_then_background_off_windows(self):
        with mock.patch.object(launch.os, "name", "posix"):
            with mock.patch.object(launch, "which", lambda n: "/usr/bin/tmux" if n == "tmux" else None):
                self.assertEqual(launch.backend(), "tmux")
            with mock.patch.object(launch, "which", lambda n: None):
                self.assertEqual(launch.backend(), "background")

    def test_configured_backend_wins(self):
        self.set_config('[control_room]\nlauncher = "background"\n')
        with mock.patch.object(launch, "which", lambda n: "/x/" + n):
            self.assertEqual(launch.backend(), "background")

    def test_tmux_window_inside_tmux_and_session_outside(self):
        with mock.patch.object(launch, "which", lambda n: "/usr/bin/tmux"):
            with mock.patch.dict(os.environ, {"TMUX": "/tmp/t,1,0"}):
                argv, where, _ = launch.tmux_argv("/w", ["claude", "--resume", "u"], "cr-u")
                self.assertEqual(argv[:6], ["/usr/bin/tmux", "new-window", "-d", "-c", "/w", "-n"])
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TMUX", None)
                argv, where, _ = launch.tmux_argv("/w", ["claude", "--resume", "u"], "cr u;x")
                self.assertEqual(argv[1:4], ["new-session", "-d", "-s"])
                self.assertEqual(argv[4], "cr-u-x")
                self.assertIn("tmux attach -t cr-u-x", where)
        self.assertIn("-u CLAUDECODE", argv[-1])
        self.assertIn("claude --resume u", argv[-1])

    def test_open_window_through_tmux_records_and_spawns(self):
        state = self.root / ".workflow" / "state" / "control-room"
        spawned = []
        with mock.patch.object(launch, "backend", lambda: "tmux"), \
                mock.patch.object(launch, "which", lambda n: "/usr/bin/tmux"), \
                mock.patch.object(launch, "spawn_window", lambda argv, **kw: spawned.append(argv)):
            out = launch.open_window(state, Clock(), kind="resume", ident="abc", directory=str(self.root))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(launch.read_records(state)[0]["event"], "open")

    def test_background_backend_refuses_a_window_with_the_command(self):
        with mock.patch.object(launch, "backend", lambda: "background"):
            out = launch.open_window(self.root, Clock(), kind="attach", ident="abcd1234", directory=str(self.root))
        self.assertEqual(out["status"], "refused")
        self.assertIn("claude attach abcd1234", out["message"])

    def test_opencode_attach_is_refused_and_resume_uses_session(self):
        self.set_config('[loop]\ndriver_tool = "opencode"\n')
        self.assertIsNone(launch.window_command("attach", "x")[0])
        self.assertEqual(launch.window_command("resume", "x")[0], ["opencode", "--session", "x"])

    def test_windows_terminal_needs_wt(self):
        with mock.patch.object(launch, "which", lambda n: None):
            argv, _, why = launch.windows_terminal_argv("C:\\w", ["claude"])
        self.assertIsNone(argv)
        self.assertIn("wt", why)

    def test_windows_terminal_script_drops_session_vars(self):
        with mock.patch.object(launch, "which", lambda n: "C:\\wt.exe"), \
                mock.patch.object(launch.auto_dev_driver, "git_bash_path", lambda env: Path("C:\\bash.exe")):
            argv, script, _ = launch.windows_terminal_argv("C:\\w", ["claude", "attach", "abcd1234"])
        self.assertEqual(argv[:6], ["C:\\wt.exe", "-w", "new", "new-tab", "-d", "C:\\w"])
        self.assertIn("Env:WT_SESSION", script)
        self.assertIn("CLAUDE_CODE_GIT_BASH_PATH", script)


class StartSessionTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        self.state = self.root / ".workflow" / "state" / "control-room"

    def test_claude_start_reads_the_session_id_and_records_every_step(self):
        class Proc:
            pid = 4242

        def starter(argv, *, cwd, env, timeout, out_prefix, on_start=None):
            self.argv = argv
            on_start(Proc())
            return 0, "backgrounded session 1a2b3c4d now\n", ""

        (self.root / ".mcp.json").write_text(json.dumps({"mcpServers": {"b": {}, "a": {}}}))
        with mock.patch.object(launch, "which", lambda n: "/bin/claude"), \
                mock.patch.object(launch, "run_starter", starter), \
                mock.patch.object(launch, "process_created", lambda pid: 123.0):
            out = launch.start_session(self.root, self.state, Clock(), "1-1", "1-1-a")
        self.assertEqual(out["session_id"], "1a2b3c4d", out)
        self.assertEqual(self.argv[:4], ["/bin/claude", "--bg", "--name", "1-1"])
        self.assertEqual(self.argv[-1], "/parallel-dev-wave 1-1")
        events = [r["event"] for r in launch.read_records(self.state)]
        self.assertEqual(events, ["launch-attempt", "launch-started", "launch-result"])
        attempt = launch.read_records(self.state)[0]
        self.assertEqual(attempt["settings"], {"enabledMcpjsonServers": ["a", "b"]})

    def test_no_mcp_json_means_no_settings_flag(self):
        with mock.patch.object(launch, "which", lambda n: "/bin/claude"), \
                mock.patch.object(launch, "run_starter", lambda argv, **kw: (0, "", "")):
            argv, _, extras, why = launch.start_argv(self.root, self.state, "1-1")
        self.assertNotIn("--settings", argv)
        self.assertEqual((extras, why), ({}, ""))

    def test_opencode_start_in_background_logs_and_records_the_pid(self):
        cfg = self.root / ".workflow" / "config.toml"
        cfg.write_text(cfg.read_text() + '[loop]\ndriver_tool = "opencode"\n[control_room]\nlauncher = "background"\n'
                       'start_prompt = "run parallel-dev-wave for {story}"\n')
        te.clear_caches()

        class Proc:
            pid = 77

        with mock.patch.object(launch, "which", lambda n: "/bin/opencode"), \
                mock.patch.object(launch, "spawn_window", lambda argv, **kw: (setattr(self, "argv", argv), Proc())[1]), \
                mock.patch.object(launch, "process_created", lambda pid: None):
            out = launch.start_session(self.root, self.state, Clock(), "1-1")
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(self.argv, ["opencode", "run", "run parallel-dev-wave for 1-1"])
        started = [r for r in launch.read_records(self.state) if r["event"] == "launch-started"]
        self.assertEqual(started[0]["pid"], 77)

    def test_launch_guard_blocks_until_the_process_is_gone(self):
        launch.append_record(self.state, {"event": "launch-attempt", "launch_id": "L1", "ts": launch.iso(100),
                                          "story": "1-1"})
        launch.append_record(self.state, {"event": "launch-started", "launch_id": "L1", "ts": launch.iso(101),
                                          "pid": 5, "created": 1.0})
        with mock.patch.object(launch, "process_alive", lambda pid, created: True):
            launch.settle_exited_launches(self.state, Clock(), "1-1")
        self.assertEqual(launch.blocking_launches(self.state, "1-1", 0)[0]["ended"], None)
        with mock.patch.object(launch, "process_alive", lambda pid, created: False):
            launch.settle_exited_launches(self.state, lambda: 200.0, "1-1")
        blockers = launch.blocking_launches(self.state, "1-1", 300.0)
        self.assertEqual(blockers, [])

    def test_find_duplicate(self):
        entries = [{"name": "1-1", "pid": None, "state": "done"}, {"story": "1-1-a", "pid": 3}]
        self.assertEqual(launch.find_duplicate(entries, "1-1", "1-1-a"), entries[1])


@unittest.skipIf(os.name == "nt", "POSIX probe")
class ProbeTests(unittest.TestCase):
    def test_own_process_is_alive_and_a_reaped_child_is_gone(self):
        created = launch.process_created(os.getpid())
        self.assertTrue(launch.process_alive(os.getpid(), created))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(launch.process_alive(child.pid, None))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        self.app = cr.App(self.root, 0)
        self.server = cr.create_server(self.app, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, body="", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if body:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, data

    def seed(self, key, doc):
        with self.app.docs.lock:
            self.app.docs.entry(key).update(doc=doc, fetched_at=time.time(), last_start=time.time())

    def test_board_renders_from_the_cached_document(self):
        self.seed("board", {"generated_at": "x", "stories": [{"story_id": "1-1", "slot": {"status": "none",
                                                                                          "source": "r", "reason": "n"}}]})
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("1-1", body)

    def test_host_header_and_post_checks(self):
        status, _ = self.request("GET", "/", headers={"Host": "evil:1"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/runs/start", body="story=1-1",
                                 headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 403)

    def test_attention_filter_takes_owner_external_all(self):
        self.seed("attention", {"generated_at": "x", "sessions": {"status": "none", "source": "s", "reason": "n/a"},
                                "attention": {"status": "ok", "source": "a", "sources": {}, "note": "",
                                              "entries": [{"story": "1-1", "kind": "open-question",
                                                           "asked_of": "external", "source": {"file": "f", "line": 1},
                                                           "asked": "which icon?", "since": None}]}})
        self.assertEqual(self.request("GET", "/attention?asked_of=external")[0], 200)
        self.assertIn("which icon?", self.request("GET", "/attention?asked_of=external")[1])
        self.assertEqual(self.request("GET", "/attention?asked_of=someone")[0], 400)

    def test_static_allowlist(self):
        self.assertEqual(self.request("GET", "/static/htmx.min.js")[0], 200)
        self.assertEqual(self.request("GET", "/static/../control_room.py")[0], 404)

    def test_a_real_attention_refresh_reads_story_state(self):
        self.assertTrue(self.app.docs.refresh("attention"))
        self.app.docs.join(120)
        state = self.app.docs.state("attention")
        self.assertIsNone(state.failure, state.failure)
        self.assertEqual(state.doc["main_repo"], str(self.root))

    def test_driver_command_uses_the_kit_script(self):
        argv = self.app.docs.driver("list", "--json")
        self.assertTrue(argv[-3].endswith("auto_dev_driver.py"))


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = adm.main(list(argv))
        return rc, out.getvalue()

    def test_compare_groups_sessions_by_configuration(self):
        state = self.root / ".workflow" / "state" / "auto-dev"
        for run_id, model in (("20260101-100000", "opus"), ("20260102-100000", "sonnet")):
            d = state / run_id
            d.mkdir(parents=True)
            knobs = {"driver": {"orchestrator_model": model, "max_stories": 1}}
            (d / "sessions.jsonl").write_text(json.dumps({"session": 1, "story": "none", "outcome": "advanced",
                                                          "knobs": knobs, "knobs_id": "k", "cost_usd": 1.0,
                                                          "minutes": 2.0}) + "\n")
            (d / "driver.log").write_text("[10:00:00] start\n")
        rc, out = self.run_main("compare", "--json", "--cost", "cached")
        self.assertEqual(rc, 0, out)
        doc = json.loads(out)
        self.assertEqual(len(doc["groups"]), 2)
        self.assertEqual(adm.config_id({"driver": {"max_stories": 1, "x": 1}}),
                         adm.config_id({"driver": {"max_stories": 9, "x": 1}}))

    def test_unknown_run_is_exit_2(self):
        rc, out = self.run_main("run", "20990101-000000", "--json")
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["status"], "error")


if __name__ == "__main__":
    unittest.main()
