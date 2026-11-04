"""Tests for hooks/session/{session_start,check_pending_proposals,inject_story_ledger,inject_symbol_nav_directive}.py."""
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


class InjectStoryLedgerTests(HookTestCase):
    REGISTRY = (
        "| Slot | Status | Story | Branch |\n|---|---|---|---|\n"
        "| 0 | shared | — | development |\n| 1 | free | — | — |\n"
        "| 2 | in-use | 7-921 | story/7-921 |\n| 3 | reserved | 9-910 | — |\n"
    )

    def test_in_flight_parsing(self):
        mod = load("inject_story_ledger")
        reg = self.root / "docs" / "slot-registry.md"
        reg.parent.mkdir(parents=True)
        reg.write_text(self.REGISTRY, encoding="utf-8")
        self.assertEqual([("0", "—"), ("2", "7-921"), ("3", "9-910")][1:], mod.in_flight(reg, 6))
        self.assertEqual([("2", "7-921")], mod.in_flight(reg, 1))

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


class SessionStartTests(HookTestCase):
    def test_unknown_source_is_silent(self):
        p = self.run_hook(SESSION / "session_start.py", "{}", ["nonsense"])
        self.assertEqual((0, b""), (p.returncode, p.stdout))

    def test_compact_emits_directive_first(self):
        base = BASE_CONFIG.replace('tool = "none"', 'tool = "codebase-memory-mcp"')
        (self.root / ".workflow" / "config.toml").write_text(base, encoding="utf-8")
        reg = self.root / "docs" / "slot-registry.md"
        reg.parent.mkdir(parents=True)
        reg.write_text("| Slot | Status | Story |\n|---|---|---|\n| 1 | free | — |\n", encoding="utf-8")
        out = self.run_hook(SESSION / "session_start.py", "{}", ["compact"]).stdout.decode("utf-8")
        self.assertTrue(out.startswith("=== MUST-DO: FIRST ACTION THIS SESSION ==="), out)
        oc = self.run_hook(SESSION / "session_start.py", "{}",
                           ["resume", "--tool", "opencode"]).stdout.decode("utf-8")
        self.assertNotIn("ToolSearch", oc)

    def test_startup_emits_curation_nudge(self):
        pdir = self.root / ".workflow" / "state" / "proposals"
        pdir.mkdir(parents=True)
        (pdir / "script-foo.md").write_text("---\nkind: script\ntitle: Foo\n---\n", encoding="utf-8")
        out = self.run_hook(SESSION / "session_start.py", "{}", ["startup"]).stdout.decode("utf-8")
        self.assertIn("1 proposal(s) awaiting review: script-foo", out)


class CheckPendingProposalsTests(HookTestCase):
    def write_launch(self, rec: dict) -> None:
        import json
        state = self.root / ".workflow" / "state"
        state.mkdir(parents=True, exist_ok=True)
        with open(state / "retro-launches.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def out(self) -> str:
        return self.run_hook(SESSION / "check_pending_proposals.py", "{}").stdout.decode("utf-8")

    def test_silent_with_nothing_to_do(self):
        self.assertEqual("", self.out())

    def test_failed_launch_reported_until_marked(self):
        self.write_launch({"run_id": "r1", "session": "sess-abcdef12", "facts": 9, "ok": False,
                           "error": "login expired"})
        self.assertIn("failed: login expired", self.out())
        markers = self.root / ".workflow" / "state" / "journal" / ".markers"
        markers.mkdir(parents=True)
        (markers / "sess-abcdef12.json").write_text('{"analyzed_facts": 9, "launched_facts": 0}',
                                                    encoding="utf-8")
        self.assertEqual("", self.out())

    def test_successful_launch_silent(self):
        self.write_launch({"run_id": "r1", "session": "s", "facts": 9, "ok": True})
        self.assertEqual("", self.out())
