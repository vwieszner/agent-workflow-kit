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
