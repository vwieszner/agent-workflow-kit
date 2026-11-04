"""The Claude Code plugin build (install.py build-plugin) and the plugin-mode code paths:

- the committed plugins/agent-workflow-kit equals a fresh build (edit the kit, then rebuild);
- hooks.json runs only scripts the plugin ships, through ${CLAUDE_PLUGIN_ROOT}, with `--role auto` for
  the per-agent checks a plugin agent cannot carry;
- shipped code never holds `${...}` (a Python f-string or JS template literal would evaluate it);
- prose points at the plugin root, agents carry no plugin-ignored fields and their skills are scoped;
- plugin-scoped names are unscoped for guards and readers;
- `--role auto` passes agents it has no role for; session_start --plugin injects the working rules;
- plugin_setup.py creates project files once, merges deny rules and names kit hooks still wired.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
PLUGIN = KIT / "plugins" / "agent-workflow-kit"
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "scripts"))
import agent_roles  # noqa: E402
import install  # noqa: E402


def run(script: Path, args: list[str], payload: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *args], input=json.dumps(payload or {}),
                          capture_output=True, text=True, cwd=cwd, timeout=60)


class BuildTests(unittest.TestCase):
    def test_committed_plugin_matches_a_fresh_build(self):
        diffs = install.check_plugin(PLUGIN)
        self.assertEqual(diffs, [], "run `python3 install.py build-plugin`")

    def test_hooks_json_runs_shipped_scripts_through_the_plugin_root(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        cmds = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
        self.assertTrue(cmds)
        for cmd in cmds:
            self.assertNotIn(".workflow/", cmd)
            self.assertNotIn("$CLAUDE_PROJECT_DIR", cmd)
            for rel in re.findall(r'\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+\.py)"', cmd):
                self.assertTrue((PLUGIN / rel).is_file(), rel)
        joined = "\n".join(cmds)
        self.assertIn("agent_outcome.py\" --role auto", joined)
        self.assertIn("context_cap.py\" --role auto", joined)
        self.assertIn("inject_path_rules.py\" --auto", joined)
        self.assertTrue(all(c.endswith("--plugin") for c in cmds if "session_start.py" in c))

    def test_shipped_code_holds_no_brace_variable(self):
        allowed = {"scripts/check_settings_and_docs_integrity.py", "hooks/session/session_start.py"}
        for f in list((PLUGIN / "scripts").rglob("*")) + list((PLUGIN / "hooks").rglob("*")) + \
                list((PLUGIN / "workflows").rglob("*")):
            if f.suffix in (".py", ".js", ".mjs") and f.relative_to(PLUGIN).as_posix() not in allowed:
                self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", f.read_text(encoding="utf-8"), f)

    def test_prose_points_at_the_plugin_root(self):
        for f in list((PLUGIN / "skills").rglob("*.md")) + [PLUGIN / "AGENTS.md"]:
            text = f.read_text(encoding="utf-8")
            for gone in (".workflow/scripts/", ".workflow/hooks/", ".workflow/workflows/"):
                self.assertNotIn(gone, text, f)
        review = (PLUGIN / "skills" / "layered-review" / "workflow.md").read_text(encoding="utf-8")
        self.assertIn('scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/review.js"', review)
        self.assertIn(".workflow/rules/local/", (PLUGIN / "AGENTS.md").read_text(encoding="utf-8"))

    def test_agents_drop_plugin_ignored_fields_and_scope_their_skills(self):
        for f in (PLUGIN / "agents").glob("*.md"):
            head = f.read_text(encoding="utf-8").split("\n---\n", 1)[0]
            self.assertNotRegex(head, r"(?m)^(hooks|permissionMode|mcpServers):", f)
            for line in head.splitlines():
                if line.startswith("skills:"):
                    for name in line.split(":", 1)[1].strip("[] ").split(","):
                        self.assertTrue(name.strip().startswith("agent-workflow-kit:"), (f, name))

    def test_setup_skill_and_deny_rules_ship(self):
        self.assertTrue((PLUGIN / "skills" / "setup" / "SKILL.md").is_file())
        deny = json.loads((PLUGIN / "setup" / "permissions.deny.json").read_text(encoding="utf-8"))
        self.assertIn("Edit(.workflow/state/kill-tokens/**)", deny)


class NameTests(unittest.TestCase):
    def test_unscope_and_base_role(self):
        self.assertEqual(agent_roles.unscope("agent-workflow-kit:story-impl"), "story-impl")
        self.assertEqual(agent_roles.unscope("story-impl"), "story-impl")
        self.assertEqual(agent_roles.unscope("a:b:c"), "a:b:c")
        self.assertEqual(agent_roles.base_role("agent-workflow-kit:story-impl-max"), "story-impl")

    def test_normalize_payload(self):
        p = agent_roles.normalize_payload({"agent_type": "x-kit:blind-hunter",
                                           "tool_input": {"subagent_type": "x-kit:story-spec", "skill": "x-kit:land-story"}})
        self.assertEqual((p["agent_type"], p["tool_input"]["subagent_type"], p["tool_input"]["skill"]),
                         ("blind-hunter", "story-spec", "land-story"))


class PluginModeHookTests(unittest.TestCase):
    def test_role_auto_passes_agents_without_a_role(self):
        r = run(KIT / "hooks" / "session" / "agent_outcome.py", ["--role", "auto"],
                {"agent_type": "agent-workflow-kit:blind-hunter"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = run(KIT / "hooks" / "session" / "context_cap.py", ["--role", "auto"], {"tool_name": "Bash"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_session_start_plugin_injects_rules_and_names_the_setup_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            env = dict(os.environ, WORKFLOW_REPO_ROOT=tmp)
            r = subprocess.run([sys.executable, str(KIT / "hooks" / "session" / "session_start.py"), "startup",
                                "--plugin"], input="{}", capture_output=True, text=True, cwd=tmp, env=env, timeout=60)
        self.assertIn("agent-workflow-kit plugin root:", r.stdout)
        self.assertIn("agent-workflow-kit:setup", r.stdout)
        self.assertIn("# Working Rules", r.stdout)

    def test_session_start_without_plugin_flag_is_unchanged(self):
        r = run(KIT / "hooks" / "session" / "session_start.py", ["startup"])
        self.assertNotIn("plugin root", r.stdout)


class SetupTests(unittest.TestCase):
    def test_setup_creates_once_merges_deny_and_names_wired_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text(json.dumps({
                "permissions": {"deny": ["Bash(rm -rf /)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command",
                                               "command": 'uv run "$CLAUDE_PROJECT_DIR/.workflow/hooks/session/session_journal.py"'}]}]}}),
                encoding="utf-8")
            (root / ".workflow").mkdir()
            (root / ".workflow" / "AGENTS.local.md").write_text("mine\n", encoding="utf-8")
            r = subprocess.run([sys.executable, str(PLUGIN / "scripts" / "plugin_setup.py")], cwd=tmp,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((root / ".workflow" / "config.toml").is_file())
            self.assertEqual((root / ".workflow" / "AGENTS.local.md").read_text(encoding="utf-8"), "mine\n")
            deny = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["deny"]
            self.assertEqual(deny[0], "Bash(rm -rf /)")
            self.assertIn("Edit(.workflow/state/kill-tokens/**)", deny)
            self.assertIn("WARNING", r.stdout)
            again = subprocess.run([sys.executable, str(PLUGIN / "scripts" / "plugin_setup.py")], cwd=tmp,
                                   capture_output=True, text=True, timeout=120)
            self.assertIn("every kit deny rule present", again.stdout)


if __name__ == "__main__":
    unittest.main()
