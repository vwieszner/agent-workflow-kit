"""Tests for install.py: Claude settings merge (hooks + permissions.deny), MCP matcher
rendering from config, permissionMode rendering, and manifest-based pruning."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(KIT))
import install  # noqa: E402


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="wfinstall-"))
        subprocess.run(["git", "init", "-q", str(self.t)], check=True)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def run_install(self, *extra):
        return subprocess.run([sys.executable, str(KIT / "install.py"), "install", "--target",
                               str(self.t), "--tool", "both", *extra],
                              capture_output=True, text=True)

    def settings(self):
        return json.loads((self.t / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_permissions_deny_merged_and_user_rules_kept(self):
        (self.t / ".claude").mkdir()
        (self.t / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"deny": ["Bash(curl *)"], "allow": ["Read"]}}), encoding="utf-8")
        self.assertEqual(0, self.run_install().returncode)
        deny = self.settings()["permissions"]["deny"]
        self.assertIn("Bash(curl *)", deny)
        self.assertIn("Bash(git reset --hard*)", deny)
        self.assertEqual(["Read"], self.settings()["permissions"]["allow"])
        self.run_install()
        self.assertEqual(len(deny), len(self.settings()["permissions"]["deny"]), "deny rules duplicated")

    def test_mcp_matchers_follow_config(self):
        self.run_install()
        matchers = [g.get("matcher") for g in self.settings()["hooks"]["PreToolUse"]]
        self.assertFalse(any(str(m).startswith("mcp__") for m in matchers), matchers)
        cfg = self.t / ".workflow" / "config.toml"
        text = cfg.read_text(encoding="utf-8")
        text = text.replace('tool = "none"                     # "codebase-memory-mcp" | "none"',
                            'tool = "codebase-memory-mcp"')
        text = text.replace("mcp_readonly_servers = []",
                            'mcp_readonly_servers = [ { server = "graphdb", dialect = "cypher" } ]')
        cfg.write_text(text, encoding="utf-8")
        self.run_install()
        groups = {g["matcher"]: g for g in self.settings()["hooks"]["PreToolUse"]}
        self.assertIn("mcp__codebase-memory-mcp__.*", groups)
        self.assertIn("graph_query.py", json.dumps(groups["mcp__codebase-memory-mcp__.*"]))
        self.assertIn("mcp_readonly.py", json.dumps(groups["mcp__graphdb__.*"]))
        cmds = json.dumps(self.settings()["hooks"]["PreToolUse"])
        self.assertEqual(1, cmds.count("graph_query.py"), "kit MCP hook wired twice")

    def test_permission_mode_rendered(self):
        self.run_install()
        agent = (self.t / ".claude" / "agents" / "story-impl.md").read_text(encoding="utf-8")
        self.assertIn("permissionMode: auto", agent)
        oc = (self.t / ".opencode" / "agents" / "story-impl.md").read_text(encoding="utf-8")
        self.assertNotIn("permissionMode", oc)

    def test_permission_mode_empty_omits(self):
        self.run_install()
        cfg = self.t / ".workflow" / "config.toml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            'claude_permission_mode = "auto"', 'claude_permission_mode = ""'), encoding="utf-8")
        self.run_install("--force")
        agent = (self.t / ".claude" / "agents" / "story-impl.md").read_text(encoding="utf-8")
        self.assertNotIn("permissionMode", agent)

    def test_prune_removes_only_files_the_kit_stopped_shipping(self):
        self.run_install()
        wf = self.t / ".workflow"
        man = json.loads((wf / ".kit-manifest.json").read_text(encoding="utf-8"))
        stale = ".workflow/scripts/retired_tool.py"
        (self.t / stale).write_text("# old\n", encoding="utf-8")
        (wf / "scripts" / "project_owned.py").write_text("# mine\n", encoding="utf-8")
        man["files"].append(stale)
        (wf / ".kit-manifest.json").write_text(json.dumps(man), encoding="utf-8")
        out = self.run_install().stdout
        self.assertIn("prune .workflow/scripts/retired_tool.py", out)
        self.assertFalse((self.t / stale).exists())
        self.assertTrue((wf / "scripts" / "project_owned.py").exists())
        self.assertTrue((wf / "config.toml").exists())
        self.assertTrue((wf / "AGENTS.local.md").exists())
        self.assertIn("__pycache__/", (wf / ".gitignore").read_text(encoding="utf-8"))

    def test_hook_fields_refreshed_for_existing_install(self):
        (self.t / ".claude").mkdir()
        cmd = 'uv run --no-project "$CLAUDE_PROJECT_DIR/.workflow/hooks/session/write_handoff.py" --tool claude'
        (self.t / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreCompact": [
            {"matcher": "auto", "hooks": [{"type": "command", "command": cmd}]}]}}), encoding="utf-8")
        self.run_install()
        entry = self.settings()["hooks"]["PreCompact"][0]["hooks"][0]
        self.assertEqual(200, entry.get("timeout"))

    def test_render_claude_fragment_unit(self):
        class WF:
            @staticmethod
            def get(key, default=None):
                return {"graph.tool": "none", "guards.mcp_readonly_servers": []}.get(key, default)
        out = install._render_claude_fragment({"hooks": {"PreToolUse": []}}, WF)
        self.assertEqual([], out["hooks"]["PreToolUse"])


if __name__ == "__main__":
    unittest.main()
