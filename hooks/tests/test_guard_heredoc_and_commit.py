"""Shell guards: heredoc bodies are content (except a body a host shell runs), a `$(...)` inside
double quotes is its own quoting context, and PowerShell commits are routed to Bash."""
from __future__ import annotations

import sys

from _hookenv import GUARDS, HookTestCase

sys.path.insert(0, str(GUARDS))
import _guardlib as g  # noqa: E402

BASH = GUARDS / "bash_command.py"
PS = GUARDS / "powershell_command.py"
ALLOW, DENY = 0, 2


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def ps(cmd):
    return {"tool_name": "PowerShell", "tool_input": {"command": cmd}}


class HeredocTests(HookTestCase):
    def test_a_content_heredoc_is_not_read_as_commands(self):
        for cmd in ("cat > notes.md <<'EOF'\npip install requests\ngit reset --hard\nEOF",
                    "git commit -F - <<EOF\nmention git push --force here\nEOF",
                    "cat > note.md <<'EOF'\nsee C:\\work\\x for details\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertEqual(ALLOW, self.exit_code(BASH, bash(cmd)))

    def test_a_body_a_host_shell_runs_is_checked(self):
        for cmd in ("bash <<EOF\npip install requests\nEOF",
                    "VAR=1 sh <<'EOF'\ngit reset --hard\nEOF",
                    "cat <<'EOF' | bash\npip install x\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertEqual(DENY, self.exit_code(BASH, bash(cmd)))

    def test_substitution_inside_double_quotes_is_its_own_context(self):
        self.assertEqual(ALLOW, self.exit_code(BASH, bash('echo "$(grep -E "\\bx\\b" f)"')))
        self.assertEqual(DENY, self.exit_code(BASH, bash('echo "$(ls D:\\work\\x)"')))

    def test_residue_keeps_length(self):
        for text in ('a "$(b "c" d)" e', "x 'y' \"z\"", 'echo "$((1+2))"'):
            self.assertEqual(len(g.unquoted_residue(text, track_substitutions=True)), len(text))


class PowerShellCommitTests(HookTestCase):
    def test_a_powershell_commit_is_denied(self):
        for cmd in ('git commit -m "x"', 'git -C "D:/w" commit -am x', "cd x; git commit"):
            with self.subTest(cmd=cmd):
                self.assertEqual(DENY, self.exit_code(PS, ps(cmd)))

    def test_text_naming_a_commit_is_not_a_commit(self):
        for cmd in ("git commit-tree abc", "@'\ngit commit -m x\n'@ | Set-Content t.txt"):
            with self.subTest(cmd=cmd):
                self.assertEqual(ALLOW, self.exit_code(PS, ps(cmd)))

    def test_a_here_string_an_evaluator_runs_is_checked(self):
        self.assertEqual(DENY, self.exit_code(PS, ps("@'\ngit commit -m x\n'@ | iex")))

    def test_the_switch_turns_it_off(self):
        self.write_config("[guards]\ncommit_via_bash = false\n")
        self.assertEqual(ALLOW, self.exit_code(PS, ps('git commit -m "x"')))
