"""Tests for guards/powershell_command.py and guards/script_registration.py."""
from __future__ import annotations

import json

from _hookenv import GUARDS, HookTestCase

PS = GUARDS / "powershell_command.py"
REG = GUARDS / "script_registration.py"


class PowerShellTests(HookTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(
            "[guards]\n"
            'host_forbidden = ["^python3?(\\\\.exe)?(\\\\s|$)"]\n'
            f'recursive_delete_allowed = [{json.dumps("C:/work/demo")}, "/tmp/scratch"]\n'
        )

    def ps(self, cmd):
        return self.exit_code(PS, {"tool_name": "PowerShell", "tool_input": {"command": cmd}})

    def test_native_2_and_1(self):
        self.assertEqual(2, self.ps("cmd /c run-tests.bat 2>&1"))
        self.assertEqual(2, self.ps('& "C:\\tools\\x.exe" 2>&1'))
        self.assertEqual(0, self.ps("Get-ChildItem 2>&1"))

    def test_host_forbidden_with_call_operator(self):
        self.assertEqual(2, self.ps("python foo.py"))
        self.assertEqual(2, self.ps("& python foo.py"))
        self.assertEqual(0, self.ps("docker compose exec -T backend python foo.py"))

    def test_host_installs(self):
        self.assertEqual(2, self.ps("pip install x"))
        self.assertEqual(0, self.ps("uv add --no-sync x"))

    def test_recursive_delete(self):
        self.assertEqual(0, self.ps("Remove-Item -Recurse -Force C:\\work\\demo\\test-results"))
        self.assertEqual(2, self.ps("Remove-Item -Recurse -Force C:\\work\\demo"))
        self.assertEqual(2, self.ps("Remove-Item -Recurse -Force C:\\Users\\someone"))
        self.assertEqual(0, self.ps("rm -rf /tmp/scratch/x"))
        self.assertEqual(2, self.ps("rm -rf /etc"))
        self.assertEqual(0, self.ps("Remove-Item -Recurse -Force $worktree"))
        self.assertEqual(0, self.ps("Remove-Item C:\\Users\\someone\\file.txt"))

    def test_recursive_delete_forms(self):
        self.assertEqual(2, self.ps("Remove-Item -Recurse -Force C:\\"))
        self.assertEqual(2, self.ps("Remove-Item -Path:C:\\Windows -Recurse"))
        self.assertEqual(2, self.ps("Remove-Item -LiteralPath 'C:\\Windows' -Recurse -Force"))
        self.assertEqual(2, self.ps("Remove-Item -Recurse ~"))
        self.assertEqual(2, self.ps("Remove-Item -Recurse -Force $env:USERPROFILE"))
        self.assertEqual(2, self.ps("rd -Recurse C:\\work"))
        self.assertEqual(2, self.ps("rm -rf /"))
        self.assertEqual(0, self.ps("Remove-Item -Path:C:\\work\\demo\\out -Recurse"))

    def test_git_rules(self):
        self.assertEqual(2, self.ps("git push --force origin story/x"))
        self.assertEqual(2, self.ps("git push origin main"))
        self.assertEqual(2, self.ps("git commit --no-verify -m x"))
        self.assertEqual(0, self.ps("git push origin story/x"))

    def test_destructive(self):
        self.assertEqual(2, self.ps("git reset --hard HEAD~1"))
        self.assertEqual(2, self.ps("Format-Volume -DriveLetter D"))
        self.assertEqual(2, self.ps("Get-Disk 1 | Clear-Disk -RemoveData"))
        self.assertEqual(2, self.ps("git update-ref -d refs/heads/x"))

    def test_bare_python_builtin(self):
        self.write_config("")
        self.assertEqual(2, self.ps("& python foo.py"))
        self.assertEqual(0, self.ps("uv run --no-project .workflow/scripts/wfconfig.py get x"))

    def test_bash_payload_ignored(self):
        self.assertEqual(0, self.exit_code(PS, {"tool_name": "Bash", "tool_input": {"command": "pip install x"}}))


class ScriptRegistrationTests(HookTestCase):
    def setUp(self):
        super().setUp()
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "INDEX.md").write_text("| known.py | does things |\n", encoding="utf-8")

    def write(self, path):
        return self.run_hook(REG, {"tool_name": "Write", "cwd": str(self.root),
                                   "tool_input": {"file_path": str(self.root / path)}})

    def test_unregistered_script_warns(self):
        p = self.write("scripts/new_tool.py")
        self.assertEqual(0, p.returncode)
        self.assertIn(b"not registered", p.stderr)
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual("PostToolUse", out["hookEventName"])
        self.assertIn("not registered", out["additionalContext"])

    def test_kit_scripts_ignored(self):
        self.assertEqual(b"", self.write(".workflow/scripts/new_kit_tool.py").stdout)

    def test_missing_index_says_skipped(self):
        (self.root / "scripts" / "INDEX.md").unlink()
        self.assertIn(b"SKIPPED", self.write("scripts/new_tool.py").stdout)

    def test_registered_script_silent(self):
        self.assertEqual(b"", self.write("scripts/known.py").stderr)

    def test_tests_subdir_exempt(self):
        self.assertEqual(b"", self.write("scripts/tests/test_x.py").stderr)

    def test_non_script_ignored(self):
        self.assertEqual(b"", self.write("scripts/notes.md").stderr)
        self.assertEqual(b"", self.write("app/module.py").stderr)

    def test_opencode_filepath_key(self):
        p = self.run_hook(REG, {"tool_name": "Write", "cwd": str(self.root),
                                "tool_input": {"filePath": str(self.root / "scripts/other.sh")}})
        self.assertIn(b"not registered", p.stderr)
