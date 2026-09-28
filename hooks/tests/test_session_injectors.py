"""Tests for hooks/session/{inject_handoff,inject_story_ledger,inject_symbol_nav_directive,write_handoff}.py."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from _hookenv import BASE_CONFIG, SESSION, HookTestCase


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_mod", SESSION / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class InjectHandoffTests(HookTestCase):
    def test_silent_without_handoff(self):
        p = self.run_hook(SESSION / "inject_handoff.py", "{}")
        self.assertEqual((0, b""), (p.returncode, p.stdout))

    def test_prints_handoff(self):
        state = self.root / ".workflow" / "state"
        state.mkdir(parents=True)
        (state / "handoff.md").write_text("editing a.py → next: tests\n", encoding="utf-8")
        out = self.run_hook(SESSION / "inject_handoff.py", "{}").stdout.decode("utf-8")
        self.assertIn("=== Resuming from prior state ===", out)
        self.assertIn("editing a.py → next: tests", out)


class InjectStoryLedgerTests(HookTestCase):
    REGISTRY = (
        "| Slot | Status | Story | Branch |\n|---|---|---|---|\n"
        "| 0 | shared | — | development |\n| 1 | free | — | — |\n"
        "| 2 | in-use | 7-21 | story/7-21 |\n| 3 | reserved | 9-10 | — |\n"
    )

    def test_in_flight_parsing(self):
        mod = load("inject_story_ledger")
        reg = self.root / "docs" / "slot-registry.md"
        reg.parent.mkdir(parents=True)
        reg.write_text(self.REGISTRY, encoding="utf-8")
        self.assertEqual([("0", "—"), ("2", "7-21"), ("3", "9-10")][1:], mod.in_flight(reg, 6))
        self.assertEqual([("2", "7-21")], mod.in_flight(reg, 1))

    def test_silent_when_registry_missing(self):
        p = self.run_hook(SESSION / "inject_story_ledger.py", "{}")
        self.assertEqual((0, b""), (p.returncode, p.stdout))


class SymbolNavTests(HookTestCase):
    def out(self, tool):
        return self.run_hook(SESSION / "inject_symbol_nav_directive.py", "{}", ["--tool", tool]).stdout.decode()

    def test_silent_when_nothing_configured(self):
        self.assertEqual("", self.out("claude"))

    def test_claude_graph_and_docs(self):
        base = BASE_CONFIG.replace('tool = "none"', 'tool = "codebase-memory-mcp"')
        (self.root / ".workflow" / "config.toml").write_text(
            base + '\n[session]\ndocs_mcp_tools = ["mcp__context7__resolve-library-id", "mcp__context7__query-docs"]\n',
            encoding="utf-8")
        text = self.out("claude")
        self.assertIn("select:mcp__codebase-memory-mcp__search_graph", text)
        self.assertIn("mcp__context7__query-docs", text)
        self.assertIn("library-docs tools", text)
        oc = self.out("opencode")
        self.assertNotIn("ToolSearch", oc)
        self.assertIn("code graph", oc)


class WriteHandoffTests(HookTestCase):
    def test_argv_template(self):
        mod = load("write_handoff")
        self.assertEqual(["claude", "-p", "do x"], mod.build_argv("claude -p {prompt}", "do x"))
        self.assertEqual(["opencode", "run", "do x"], mod.build_argv("opencode run", "do x"))

    def test_prompt_prefix_and_source(self):
        mod = load("write_handoff")
        t = self.root / "t.jsonl"
        t.write_text("{}", encoding="utf-8")
        p = mod.build_prompt(self.root / "h.md", str(t))
        self.assertTrue(p.startswith(mod.HANDOFF_PROMPT_PREFIX))
        self.assertIn(str(t), p)

    def test_runs_configured_command(self):
        marker = self.root / "ran.txt"
        script = self.root / "fake_agent.py"
        script.write_text(
            "import os,sys\nopen(sys.argv[1],'w').write(os.environ.get('WORKFLOW_HEADLESS','')+'|'+sys.argv[2])\n",
            encoding="utf-8")
        cmd = f"{Path(sys.executable).as_posix()} {script.as_posix()} {marker.as_posix()} {{prompt}}"
        (self.root / ".workflow" / "config.toml").write_text(
            BASE_CONFIG + f"\n[session]\nhandoff_cmd = {cmd!r}\n".replace("'", '"'), encoding="utf-8")
        p = self.run_hook(SESSION / "write_handoff.py", '{"session_id": "s1"}', ["--tool", "claude"])
        self.assertEqual(0, p.returncode)
        content = marker.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("1|Write current session state to "), content)

    def test_noop_when_retro_running(self):
        marker = self.root / "ran.txt"
        script = self.root / "fake_agent.py"
        script.write_text("import sys\nopen(sys.argv[1],'w').write('ran')\n", encoding="utf-8")
        cmd = f"{Path(sys.executable).as_posix()} {script.as_posix()} {marker.as_posix()} {{prompt}}"
        (self.root / ".workflow" / "config.toml").write_text(
            BASE_CONFIG + f"\n[session]\nhandoff_cmd = {cmd!r}\n".replace("'", '"'), encoding="utf-8")
        os.environ["WORKFLOW_RETRO_RUNNING"] = "1"
        try:
            p = self.run_hook(SESSION / "write_handoff.py", '{"session_id": "s1"}', ["--tool", "claude"])
        finally:
            os.environ.pop("WORKFLOW_RETRO_RUNNING", None)
        self.assertEqual(0, p.returncode)
        self.assertFalse(marker.exists(), "handoff writer ran inside the retrospective analyst")

    def test_noop_when_headless(self):
        env_backup = os.environ.get("WORKFLOW_HEADLESS")
        mod = load("write_handoff")
        os.environ["WORKFLOW_HEADLESS"] = "1"
        try:
            self.assertEqual(0, mod.main(["--tool", "claude"]))
        finally:
            if env_backup is None:
                os.environ.pop("WORKFLOW_HEADLESS", None)
            else:
                os.environ["WORKFLOW_HEADLESS"] = env_backup
