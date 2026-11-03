"""static_checks.py: config-driven tool selection, the exit-code and JSON parsers, and the
changed-file filter, against a throwaway git repo and fake tools."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


class StaticChecksTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wf-static-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        self.genv = env
        self.git("init", "-q", "-b", "main")
        (self.root / "README").write_text("x\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("checkout", "-qb", "feature")
        (self.root / "app").mkdir()
        (self.root / "app" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "app" / "ui.js").write_text("x\n", encoding="utf-8")
        self.fake = self.root / "fake_tool.py"
        self.fake.write_text(
            "import json, sys\n"
            "files = sys.argv[1:]\n"
            "if '--json' in files:\n"
            "    print(json.dumps({'generalDiagnostics': [{'file': 'app/mod.py', 'severity': 'warning',"
            " 'rule': 'reportUndefinedVariable', 'message': 'x undefined', 'range': {'start': {'line': 0}}}]}))\n"
            "    sys.exit(1)\n"
            "print('checked', files); sys.exit(3 if any(f.endswith('.js') for f in files) else 0)\n",
            encoding="utf-8")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, env=self.genv)

    def config(self, body: str):
        (self.root / ".workflow").mkdir(exist_ok=True)
        (self.root / ".workflow" / "config.toml").write_text('[git]\nbase_branch = "main"\n' + body,
                                                              encoding="utf-8")

    def run_it(self, *args):
        env = dict(os.environ, WORKFLOW_REPO_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "static_checks.py"), "--worktree", str(self.root),
                               "--project-name", "s", *args], capture_output=True, text=True, env=env)

    def test_no_tools_is_skipped(self):
        self.config("")
        p = self.run_it("--changed")
        self.assertEqual(0, p.returncode)
        self.assertIn("SKIPPED", p.stdout)

    def test_exit_code_tool_filters_changed_files(self):
        exe = sys.executable.replace("\\", "/")
        self.config(f'[[static_checks.tools]]\nname = "py"\ncmd = ["{exe}", "{self.fake.as_posix()}"]\n'
                    'suffixes = [".py"]\nformat = "exit-code"\n'
                    f'[[static_checks.tools]]\nname = "js"\ncmd = ["{exe}", "{self.fake.as_posix()}"]\n'
                    'prefix = "app/"\nsuffixes = [".js"]\nstrip = "app/"\nformat = "exit-code"\n')
        p = self.run_it("--changed")
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)
        self.assertIn("[STATIC] py PASS files=", p.stdout)
        self.assertIn("[STATIC] js FAIL files=1", p.stdout)
        self.assertIn("checked ['ui.js']", p.stdout)

    def test_pyright_json_blocking_rule(self):
        exe = sys.executable.replace("\\", "/")
        self.config(f'[[static_checks.tools]]\nname = "pyright"\ncmd = ["{exe}", "{self.fake.as_posix()}", "--json"]\n'
                    'suffixes = [".py"]\nformat = "pyright-json"\nblocking_rules = ["reportUndefinedVariable"]\n')
        p = self.run_it("--changed")
        self.assertEqual(1, p.returncode, p.stdout)
        self.assertIn("app/mod.py:1 reportUndefinedVariable x undefined", p.stdout)

    def test_missing_tool_is_infra(self):
        self.config('[[static_checks.tools]]\nname = "x"\ncmd = ["no-such-tool-xyz"]\nformat = "exit-code"\n')
        self.assertEqual(2, self.run_it("--all").returncode)


if __name__ == "__main__":
    unittest.main()
