"""Corpus tests for guards/bash_command.py. Add an ALLOWED row before touching a matcher:
this guard sees every shell call in every session, so a false positive stops unrelated work."""
from __future__ import annotations

from _hookenv import GUARDS, HookTestCase

HOOK = GUARDS / "bash_command.py"
ALLOW, DENY = 0, 2

# Project-style host_forbidden rule used by the corpus (bare host python).
EXTRA = r'''
[guards]
host_forbidden = ["^python3?(\\.exe)?(\\s|$)"]
host_forbidden_hint = "Use uv run --no-project <script>, the project venv interpreter, or the container."
command_deny = [ { pattern = "manage\\.py\\s+makemigrations(?![^|;&\\n]*--(check|dry-run))", reason = "never create new migration files" } ]
'''

ALLOWED = [
    ("container python via compose -p", "docker compose -p demo exec -T backend python manage.py test x"),
    ("container python via docker-compose", "docker-compose exec -T backend python manage.py migrate"),
    ("venv interpreter, absolute path", 'C:/work/demo/.venv/Scripts/python.exe -c "print(1)"'),
    ("venv interpreter after a cd", 'cd "/work/demo" && /work/demo/.venv/bin/python scripts/x.py'),
    ("uv run tooling script", "uv run --no-project .workflow/scripts/story_setup.py wa-1"),
    ("word python inside a commit message", 'git commit -m "fix python import ordering"'),
    ("grepping for python", 'grep -rn "python" docs/ | head'),
    ("python inside a heredoc fed to a container",
     "docker compose -p demo exec -T backend bash <<'EOF'\npython manage.py test\nEOF"),
    ("echo mentioning python", 'echo "run python later"'),
    ("listing .py files", "ls scripts/*.py"),
    ("container pip install", "docker compose exec -T backend pip install requests"),
    ("container pip install with -p flag", "docker compose -p story-1 exec -T backend pip install x"),
    ("uv add --no-sync", "uv add --no-sync httpx"),
    ("uv remove --no-sync", "uv remove --no-sync httpx"),
    ("uv lock", "uv lock"),
    ("pip list (read-only)", "pip list"),
    ("push to a story branch", "git push origin story/7-44"),
    ("git status", "git status"),
    ("quoted backslash path", 'ls "C:\\work\\demo"'),
    ("forward-slash windows path", "ls C:/work/demo"),
    ("find -exec escape", "find . -name '*.tmp' -exec rm {} \\;"),
    ("makemigrations --check", "docker compose exec -T backend python manage.py makemigrations --check"),
    ("git reset --soft", "git reset --soft HEAD~1"),
    ("reset --hard inside a commit message", 'git commit -m "document git reset --hard"'),
    ("recursive delete inside the repo", "rm -rf build/tmp"),
    ("recursive delete of a variable path", 'rm -rf "$WORKDIR"'),
    ("non-recursive rm of a system file", "rm /etc/hosts.bak"),
    ("recursive delete in a temp scratch dir", "rm -rf /tmp/claude-501/scratch/x"),
    ("rm inside a container", "docker compose exec -T backend rm -rf /app/tmp"),
]

FORBIDDEN = [
    ("bare python heredoc", "python - <<'PY'\nprint(1)\nPY"),
    ("bare python script", "python scripts/foo.py"),
    ("bare python3 -c", 'python3 -c "print(1)"'),
    ("bare python after a cd", 'cd "/work/demo" && python -c "import yaml"'),
    ("bare python after a pipe", "echo hi | python"),
    ("bare python.exe", "python.exe foo.py"),
    ("bare python after ;", "ls; python foo.py"),
    ("sudo python", "sudo python foo.py"),
    ("host pip install", "pip install requests"),
    ("host python -m pip install", "python -m pip install requests"),
    ("venv pip install", "C:/work/demo/.venv/Scripts/pip.exe install requests"),
    ("host uv pip install", "uv pip install requests"),
    ("host uv sync", "uv sync"),
    ("uv add without --no-sync", "uv add requests"),
    ("uv remove without --no-sync", "uv remove requests"),
    ("force push", "git push --force origin story/x"),
    ("force push short flag", "git push -f origin story/x"),
    ("push to base branch", "git push origin development"),
    ("push to main", "git push origin main"),
    ("push to master", "git push upstream master"),
    ("generic git push", "git push"),
    ("commit --no-verify", 'git commit --no-verify -m "x"'),
    ("commit --no-gpg-sign", 'git commit --no-gpg-sign -m "x"'),
    ("unquoted drive path", "ls C:\\work\\demo"),
    ("unquoted seg\\seg path", "cat .claude\\hooks\\x.cmd"),
    ("project command_deny rule", "python3 manage.py makemigrations"),
    ("git reset --hard", "git reset --hard HEAD~3"),
    ("git -C reset --hard", "git -C ../wt reset --hard origin/development"),
    ("git filter-branch", "git filter-branch --tree-filter x HEAD"),
    ("git update-ref -d", "git update-ref -d refs/heads/story/x"),
    ("rm -rf /", "rm -rf /"),
    ("rm -fr root glob", "rm -fr /*"),
    ("rm -r home", "rm -r ~"),
    ("rm --recursive $HOME", "rm --recursive --force $HOME/projects"),
    ("sudo rm -rf system dir", "sudo rm -rf /usr/local"),
    ("rm -rf the repo root", "rm -rf ."),
    ("rm -rf escaping the repo via ..", "rm -rf ../../other"),
    ("rm -rf a drive root", "rm -rf C:/"),
    ("rm -rf the whole temp dir", "rm -rf /tmp"),
]


class BashCommandCorpus(HookTestCase):
    extra_config = EXTRA

    def test_allowed(self):
        for name, cmd in ALLOWED:
            with self.subTest(shape=name):
                self.assertEqual(ALLOW, self.exit_code(HOOK, {"tool_name": "Bash", "tool_input": {"command": cmd}}))

    def test_forbidden(self):
        for name, cmd in FORBIDDEN:
            with self.subTest(shape=name):
                self.assertEqual(DENY, self.exit_code(HOOK, {"tool_name": "Bash", "tool_input": {"command": cmd}}))

    def test_other_tool_ignored(self):
        self.assertEqual(ALLOW, self.exit_code(HOOK, {"tool_name": "Read", "tool_input": {"command": "git push"}}))


class BashCommandDefaults(HookTestCase):
    """No project lists configured: built-in rules still hold."""

    def run_cmd(self, cmd):
        return self.exit_code(HOOK, {"tool_name": "Bash", "tool_input": {"command": cmd}})

    def test_bare_python_denied_when_app_runs_in_container(self):
        self.assertEqual(DENY, self.run_cmd("python x.py"))
        self.assertEqual(DENY, self.run_cmd("python3 -c 'print(1)'"))
        self.assertEqual(ALLOW, self.run_cmd("uv run --no-project scripts/x.py"))
        self.assertEqual(ALLOW, self.run_cmd("python3 .workflow/hooks/guards/bash_command.py"))

    def test_bare_python_allowed_when_app_runs_on_host(self):
        self.write_config("[env]\napp_runs_in_container = false\n")
        self.assertEqual(ALLOW, self.run_cmd("python x.py"))

    def test_project_python_allowed(self):
        self.write_config('[env]\nproject_python = "python3.12"\n')
        self.assertEqual(ALLOW, self.run_cmd("python3.12 -m mytool"))
        self.assertEqual(DENY, self.run_cmd("python3 -m mytool"))

    def test_recursive_delete_allowlist_from_config(self):
        self.write_config('[guards]\nrecursive_delete_allowed = ["/data/scratch"]\n')
        self.assertEqual(ALLOW, self.run_cmd("rm -rf /data/scratch/run1"))
        self.assertEqual(DENY, self.run_cmd("rm -rf /data/scratch"))
        self.assertEqual(DENY, self.run_cmd("rm -rf /tmp/claude-1/x"))

    def test_builtin_rules_hold(self):
        for cmd in ("git push --force origin story/x", "pip install x", "git commit --no-verify -m x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(DENY, self.exit_code(HOOK, {"tool_name": "Bash", "tool_input": {"command": cmd}}))

    def test_protected_branches_follow_config(self):
        self.write_config('[guards]\nprotected_branches = ["release"]\n')
        run = lambda c: self.exit_code(HOOK, {"tool_name": "Bash", "tool_input": {"command": c}})  # noqa: E731
        self.assertEqual(DENY, run("git push origin release"))
        self.assertEqual(ALLOW, run("git push origin story/x"))

    def test_host_installs_switch(self):
        self.write_config("[guards]\nblock_host_installs = false\n")
        self.assertEqual(ALLOW, self.exit_code(HOOK, {"tool_name": "Bash", "tool_input": {"command": "pip install x"}}))

    def test_guard_switch_off(self):
        self.write_config("[guards]\nbash_command = false\n")
        self.assertEqual(ALLOW, self.exit_code(HOOK, {"tool_name": "Bash", "tool_input": {"command": "git push --force"}}))

    def test_fail_open_on_garbage(self):
        self.assertEqual(ALLOW, self.exit_code(HOOK, "not json"))

    def test_broken_config_falls_back_to_defaults(self):
        (self.root / ".workflow" / "config.toml").write_text("this is [not toml", encoding="utf-8")
        self.assertEqual(DENY, self.exit_code(HOOK, {"tool_name": "Bash", "tool_input": {"command": "git push --force"}}))
