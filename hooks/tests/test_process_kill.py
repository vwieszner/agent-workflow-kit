"""process_kill.py guard (ported corpus): raw process kills are denied, and a kill_owned.py call gets the
caller's facts and a hook token. The corpus runs through `gate()` for Bash and PowerShell payloads; the rewrite
tests use fake agent and session transcripts and a temporary token directory; one case runs the real hook entry as
a subprocess; the scan tests pin that the residues keep the command's length and that no scan raises."""
from __future__ import annotations

import base64
import json
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
GUARDS = HERE.parent / "guards"
SCRIPTS = HERE.parent.parent / "scripts"
sys.path.insert(0, str(GUARDS))
sys.path.insert(0, str(SCRIPTS))

import process_kill as pk  # noqa: E402
import kill_owned as ko  # noqa: E402
import kill_token  # noqa: E402

ENTRY = GUARDS / "process_kill.py"
# Runs the guard's real entry with the token directory pointed at a temporary one (argv: scripts dir, token dir,
# entry), so no test writes into a real state dir.
ENTRY_RUNNER = (
    "import runpy, sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import kill_token\n"
    "kill_token.token_dir = lambda: Path(sys.argv[2])\n"
    "runpy.run_path(sys.argv[3], run_name='__main__')\n")


def _run(check, p):
    try:
        return check(p)
    except Exception:
        return None


def gate(p):
    msg = pk.check(p)
    return ([msg] if msg else []), None


RAW = "a command line does not identify a stack"
HELPER = "uv run --no-project .workflow/scripts/kill_owned.py"
SANCTIONED = "run directly as " + HELPER
SCRIPT = ".workflow/scripts/kill_owned.py"
WORKTREE = "D:/work/proj-worktrees/tq-59"
STAMP = "2026-10-16T08:35:57.131Z"

DENIED = [
    "Stop-Process -Id 33652 -Force",
    "Get-Process -Id 1 | Stop-Process",
    "stop-process -id 1",
    "STOP-PROCESS -Name python",
    "TASKKILL /PID 1",
    "taskkill.exe //F //PID 1",
    "taskkill /F /T /PID 1",
    "(Get-Process -Id 1).kill()",
    "$p.Kill()",
    "kill 1234",
    "kill -9 1234",
    "kill -TERM 1234",
    "pkill -f run_scoped_tests",
    "killall python",
    "tskill 1",
    "spps -Id 1",
    "cd x && taskkill /F /PID 1",
    "echo done; Stop-Process -Name python",
    "echo a || kill 1",
    "pgrep foo | xargs kill -9",
    "pgrep foo | xargs -r kill",
    "Get-Process python | ForEach-Object { Stop-Process -Id $_.Id }",
    "Get-Process python | % { $_.Kill() }",
    "foreach ($p in Get-Process python) { Stop-Process -Id $p.Id }",
    "& taskkill /F /PID 1",
    '& "C:\\Windows\\System32\\taskkill.exe" /F /PID 1',
    ". Stop-Process -Id 1",
    "cmd /c taskkill /F /PID 1",
    'cmd /c "taskkill /F /PID 1"',
    "cmd.exe /k taskkill /PID 1",
    "bash -c 'kill 1234'",
    'sh -c "pkill foo"',
    "bash -lc 'pkill foo'",
    'powershell -NoProfile -Command "Stop-Process -Id 1"',
    "pwsh -c 'taskkill /PID 1'",
    'powershell -NoProfile -Comm "Stop-Process -Id 1"',
    "pwsh -Com 'taskkill /PID 1'",
    'powershell /command "Stop-Process -Id 1"',
    "powershell -co kill 1234",
    "powershell -ExecutionPolicy Bypass -c 'taskkill /PID 1'",
    "Invoke-CimMethod -InputObject $p -MethodName Terminate",
    "Get-CimInstance Win32_Process -Filter 'ProcessId=1' | Invoke-CimMethod -MethodName Terminate",
    "wmic process where processid=1 delete",
    "wmic process where processid=1 call terminate",
    "sudo kill 1",
    "FOO=1 kill 5",
    "if true; then kill 1; fi",
    "(kill 1)",
    "echo $(pgrep x | xargs kill)",
    "echo hi\nkill 1234",
    "KILL 1234",
    "Taskkill.EXE /PID 1",
    'x="$(taskkill /PID 1 /F)"',
    'echo "$(kill 1234)"',
    'echo "$(echo "$(kill 1)")"',
    'Write-Output "$(Stop-Process -Id 1)"',
    "pwsh -Command 'Get-ChildItem \"D:\\work\\\"; Stop-Process -Id 1'",
    "bash -c 'echo `kill 1`'",
]
BASH_ONLY_DENIED = [
    "bash <<'EOF'\nkill 1234\nEOF",
    "cat <<'EOF' | bash\ntaskkill /PID 1\nEOF",
    "echo `kill 1234`",
    "x=`taskkill /PID 1`",
    'echo "x `kill 1234`"',
    'echo "`kill 1234`"',
    'x="`taskkill /PID 1`"',
    r"echo It\'s; kill 1234",
    r'echo \"; taskkill /PID 1',
]
POWERSHELL_ONLY_DENIED = [
    "@'\nStop-Process -Id 1\n'@ | iex",
    "iex @'\nStop-Process -Id 1\n'@",
    r'Get-ChildItem "D:\work\"; Stop-Process -Id 1',
    r'echo "\$(kill 1)"',
]

ALLOWED = [
    'git commit -m "fix: Stop-Process in the cleanup"',
    "git commit -m 'taskkill removed'",
    "grep -n taskkill scripts/x.py",
    "Select-String -Pattern 'Stop-Process' -Path x.py",
    "rg kill scripts",
    "kill -0 1234",
    "kill -s 0 1234",
    "docker kill c1",
    "docker compose -p story-x kill backend",
    "ls .claude/skills",
    "echo killed",
    "echo kill",
    "uv run --no-project scripts/proc_scan.py --match kill",
    "command -v kill",
    "which taskkill",
    "Get-Process python",
    "Write-Output 'Stop-Process'",
    'echo "kill 1"',
    "git log --grep='taskkill'",
    "uv run --no-project scripts/proc_scan.py --match run_scoped_tests",
    "cat scripts/kill_owned.py",
    "grep -n kill_owned scripts/INDEX.md",
    "echo '$(kill 1)'",
    'echo "$(echo kill)"',
    'echo "$(kill -0 1)"',
    "git diff -- scripts/kill_owned.py",
    "git show HEAD:scripts/kill_owned.py",
    "git log --oneline -- scripts/kill_owned.py",
    "rg kill_owned scripts",
    "Get-Content scripts/kill_owned.py",
    "Select-String -Path scripts/kill_owned.py -Pattern x",
    "python -m py_compile scripts/kill_owned.py",
    "uv run --no-project scripts/proc_scan.py --match kill_owned",
    "uv run --no-project scripts/proc_scan.py --match kill_owned.py",
    "uv run ruff check scripts/kill_owned.py",
    "uv run --no-project -m unittest scripts.tests.test_kill_owned",
    "uv run --no-project -m scripts.tests.test_kill_owned",
    "uv run --no-project --with psutil --module http.server 8000",
    'uv run --no-project scripts/story_record.py append tq-59 "note on kill_owned.py ordering"',
    "cat scripts/kill_owned.py; echo done",
    "git log --oneline | grep kill_owned",
    "cd D:/work/proj-worktrees/tq-59 && git diff -- scripts/kill_owned.py",
    "git diff -- scripts/kill_owned.py 2>&1",
    "git diff -- scripts/kill_owned.py | head",
    "git add scripts/kill_owned.py scripts/INDEX.md 2>&1",
    "cat scripts/kill_owned.py | wc -l",
    "cat scripts/kill_owned.py | grep def",
    "rg kill_owned scripts | head",
    "cat scripts/kill_owned.py > scripts/k.py",
    "cat scripts/kill_owned.py >> scripts/k.py",
    "cat scripts/kill_owned.py>scripts/k.py",
    "git show HEAD:scripts/kill_owned.py > scripts/k.py",
    "cat scripts/kill_owned.py | tee scripts/k.py",
    "Get-Content scripts/kill_owned.py | Out-File scripts/k.py",
    "Get-Content scripts/kill_owned.py | Set-Content scripts/k.py",
    "Set-Content scripts/k.py (Get-Content scripts/kill_owned.py)",
    'echo "$(cat scripts/kill_owned.py)" > scripts/k.py',
    "echo 'print(1)' | python -",
    "python - < scripts/probe.py",
    'uv run --no-project scripts/story_record.py append tq-59 "$(cat note.txt)"',
    'python scripts/probe.py --out "$(pwd)"',
    'powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date"',
    "powershell -NoProfile -ex Bypass -c Get-Date",
    "powershell -NoProfile -Command Get-Help Get-Process -e",
    "powershell -NoProfile -File scripts/build_base.ps1 -Tag image:tag",
    "python --check-hash-based-pycs always scripts/probe.py --out x",
    "python --check-hash-based-pycs always -m unittest scripts.tests.test_kill_owned",
    "uv run --no-project python --check-hash-based-pycs never scripts/probe.py",
]
BASH_ONLY_ALLOWED = [
    "cat <<'EOF' > notes.txt\nuse taskkill /PID 1 here\nEOF",
    "uv run --no-project python - <<'PY'\nimport os\nos.kill(1, 0)\nPY",
    r'echo "\$(kill 1)"',
    "echo '`kill 1`'",
    r"echo a\; kill 1",
    'echo "a \\`kill 1\\` b"',
]
POWERSHELL_ONLY_ALLOWED = [
    "$t = @'\nStop-Process -Id 1\n'@\nSet-Content x.txt $t",
    'Write-Output "a`"b; Stop-Process -Id 1"',
    'Write-Output "x`$(Stop-Process -Id 1)"',
    "Write-Output 'it''s; Stop-Process -Id 1'",
    "python kill_ow\\ned.py --pid 1",
    "python scripts/kill_own\\ed.py --pid 1",
    "python `\n  scripts/probe.py --out x",
    "uv run --no-project `\n  scripts/probe.py --out x",
    "if (Test-Path scripts/kill_owned.py) { Get-Content scripts/kill_owned.py }",
]
# What a launch the caller controls hands kill_owned.py: another story's worktree, a launch time of 1970, a pid.
FORGED_ARGS = "--worktree D:/other --since 1970-01-01T00:00:00Z --pid 1"
PRELUDE = "echo \"__file__ = 'D:/work/proj/scripts/kill_owned.py'\""
# Launch routes only a deliberate evader would use. The hook does not read them (it neither denies nor rewrites them);
# each ends at kill_owned.py's main() with FORGED_ARGS and no hook token, which the helper refuses.
UNREAD_ROUTES = [
    f"cat scripts/kill_owned.py | python - {FORGED_ARGS}",
    f"cat scripts/kill_owned.py|python - {FORGED_ARGS}",
    f"({PRELUDE}; cat scripts/kill_owned.py) | python - {FORGED_ARGS}",
    f"{{ {PRELUDE}; cat scripts/kill_owned.py; }} | python - {FORGED_ARGS}",
    f"git show HEAD:scripts/kill_owned.py | python - {FORGED_ARGS}",
    f"Get-Content scripts/kill_owned.py | python - {FORGED_ARGS}",
    f"cat scripts/kill_owned.py | uv run --no-project python - {FORGED_ARGS}",
    f"python -<scripts/kill_owned.py {FORGED_ARGS}",
    f'python - {FORGED_ARGS} <<< "$(cat scripts/kill_owned.py)"',
    f"python <(cat scripts/kill_owned.py) {FORGED_ARGS}",
    f"cat scripts/kill_owned.py > scripts/k.py; python scripts/k.py {FORGED_ARGS}",
    f"bash -c 'cat scripts/kill_owned.py | python - {FORGED_ARGS}'",
    f'powershell -Command "Get-Content scripts/kill_owned.py | python - {FORGED_ARGS}"',
    f"python $K {FORGED_ARGS}",
    f"K=kill_; python ${{K}}owned.py {FORGED_ARGS}",
    f"K=kill_; python scripts/${{K}}owned.py {FORGED_ARGS}",
    f"python %K% {FORGED_ARGS}",
    f"uv run --no-project $K {FORGED_ARGS}",
    f"uv run --no-project python $K {FORGED_ARGS}",
    f"uv run --no-project scripts/kill_own*.py {FORGED_ARGS}",
    f"K=scripts/kill_owned.py; uv run --no-project $K {FORGED_ARGS}",
    f"python scripts/kill_ow`\nned.py {FORGED_ARGS}",
    f"uv run --no-project bash -c 'uv run --no-project \"$0\" {FORGED_ARGS}' {SCRIPT}",
]
BASH_ONLY_UNREAD_ROUTES = [f"python `echo scripts/kill_owned.py` {FORGED_ARGS}"]
# Commands that name the helper in their own argv, or redirect it into python: denied with the sanctioned form.
STILL_DENIED = [
    f"python - {FORGED_ARGS} <scripts/kill_owned.py",
    f"python - {FORGED_ARGS} < scripts/kill_owned.py",
    f"cp scripts/kill_owned.py scripts/k.py && python scripts/k.py {FORGED_ARGS}",
    "copy scripts/kill_owned.py scripts/k.py",
    "Copy-Item scripts/kill_owned.py scripts/k.py",
    f"python $(echo scripts/kill_owned.py) {FORGED_ARGS}",
]
POWERSHELL_ONLY_STILL_DENIED = [f"python `echo scripts/kill_owned.py` {FORGED_ARGS}"]
BASH_ESCAPED = [
    "python kill_ow\\ned.py --pid 1",
    "python scripts/kill_own\\ed.py --pid 1",
]
CONTINUED = ["python `\n  scripts/kill_owned.py --pid 1"]
UV_MODULE_RUNS = [
    "uv run --no-project -m kill_owned --worktree D:/other --since 1970-01-01T00:00:00Z --pid 1",
    "uv run --no-project --with psutil --directory D:/work/proj/scripts -m kill_owned --pid 1",
    "uv run --no-project --module kill_owned --pid 1",
    "uv run --no-project --module=kill_owned --pid 1",
    "uv run --no-project --with psutil -m scripts.kill_owned --pid 1",
    "uv run --no-project -mkill_owned --pid 1",
    "uv run --no-project -qm kill_owned --pid 1",
    "uv run --no-project -mq kill_owned --pid 1",
    "uv run --no-project -m kill_owned.py --pid 1",
    "bash -c 'uv run --no-project -m kill_owned --pid 1'",
]
PY_VALUE_OPTION_RUNS = [
    f"python --check-hash-based-pycs always {SCRIPT} --pid 1",
    "python --check-hash-based-pycs always -m kill_owned --pid 1",
    "python -X dev --check-hash-based-pycs never -W ignore -m kill_owned --pid 1",
    "python --check-hash-based-pycs default -m scripts.kill_owned --worktree D:/other --since 1970-01-01T00:00:00Z"
    " --pid 1",
    f"py -3 --check-hash-based-pycs always {SCRIPT} --pid 1",
    f"uv run --no-project python --check-hash-based-pycs always {SCRIPT} --pid 1",
    "uv run --no-project python --check-hash-based-pycs always -m kill_owned --pid 1",
    f"D:/work/proj/.venv/Scripts/python.exe --check-hash-based-pycs always {SCRIPT} --pid 1",
    "D:/work/proj/.venv/Scripts/python.exe --check-hash-based-pycs always -m kill_owned --pid 1",
]
# Every spelling powershell.exe takes: a `-` or `/` lead, any case, any prefix of the name (`-ec` aliases the encoded
# switch); a `--` lead and a glued `:value` are rejected by it.
POWERSHELL_COMMAND_SWITCHES = ([f"{lead}{'command'[:n]}" for lead in "-/" for n in range(1, 8)]
                               + ["-COMMAND", "/Command", "-cOmMaNd"])
POWERSHELL_ENCODED_SWITCHES = ([f"{lead}{'encodedcommand'[:n]}" for lead in "-/" for n in range(1, 15)]
                               + ["-ec", "/ec", "-EC", "-ENC", "-EncodedCommand"])
# Nested shells whose command text the hook cannot place (DENY_NESTED), in both outer shells. powershell.exe starts
# its command text at the first word it does not know, so `-1;Stop-Process -Id 1` runs; the spellings shorter than the
# shortest `-nop` / `-noni` / `-st` / `-mta` / `-config` and the pwsh switches that differ from powershell.exe's are
# not placed either.
NESTED_UNPLACED = [
    'powershell "Stop-Process -Id 1"',
    "powershell -NoProfile Stop-Process -Id 1",
    "powershell -NoProfile -ExecutionPolicy Bypass Stop-Process -Id 1",
    "powershell -1;Stop-Process -Id 1",
    "powershell -NoProfile -foo;Stop-Process -Id 1",
    "powershell -Command:Stop-Process -Id 1",
    "powershell -CommandWithArgs 'Stop-Process -Id 1'",
    "powershell -cwa 'Stop-Process -Id 1'",
    "powershell --command 'Stop-Process -Id 1'",
    "powershell -WorkingDirectory C:/x Stop-Process -Id 1",
    "powershell -args Stop-Process",
    "powershell -ExecutionPolicy -Command Stop-Process -Id 1",
    "powershell -Command -",
    "powershell -c -",
    "powershell -File -",
    "powershell -NoProfile -",
    "powershell -NoProfile -Command",
    "powershell -File",
    "powershell -n -Command Get-Date",
    "powershell -no -Command Get-Date",
    "powershell -non -Command Get-Date",
    "powershell -s -Command Get-Date",
    "powershell -m -Command Get-Date",
    "powershell -mt -Command Get-Date",
    "powershell -con x -Command Get-Date",
    "pwsh -CommandWithArgs 'Stop-Process -Id 1'",
    "pwsh Stop-Process -Id 1",
    "pwsh -i -Command Get-Date",
    "pwsh -Version 7 -Command Get-Date",
    "pwsh -InputFormat Text -Command Get-Date",
    "pwsh -PSConsoleFile x.psc1 -Command Get-Date",
    "pwsh -sta -Command Get-Date",
]
# A nested shell that reads its commands from a stdin a pipe, a `<` redirect or the enclosing shell feeds.
NESTED_FED = [
    "echo 'Stop-Process -Id 1' | powershell",
    "echo x | powershell -NoProfile",
    "echo x | powershell -NoProfile -NonInteractive -InputFormat Text",
    "powershell -NoProfile < script.ps1",
    "powershell -NoProfile <<< 'Stop-Process -Id 1'",
    "echo x | powershell -NoExit -Command Get-Date",
    "echo x | powershell -NoExit -File s.ps1",
    "echo x | pwsh",
    "echo x | pwsh -NoProfile -NoExit -File s.ps1",
    "echo 'taskkill /PID 1' | cmd",
    "cmd < script.bat",
    "echo x | cmd /q",
    "echo x | cmd /k",
    "echo x | cmd /d/k",
    "echo x | bash -c 'powershell -NoProfile'",
    "echo x | bash -c 'cmd'",
]
# cmd.exe runs the text after the first /c, /k or /r anywhere in its arguments (probed on this host).
CMD_RUNS = [
    "cmd /r taskkill /PID 1", "cmd /R taskkill /PID 1", "cmd /ctaskkill /PID 1", "cmd /Ctaskkill /PID 1",
    "cmd /ktaskkill /PID 1", "cmd /rtaskkill /PID 1", "cmd /q/c taskkill /PID 1", "cmd /d/r taskkill /PID 1",
    "cmd /q/k taskkill /PID 1", "cmd /e:on/c taskkill /PID 1", "cmd /t:0a/c taskkill /PID 1",
    "cmd /s /c taskkill /PID 1", "cmd /u /c taskkill /PID 1", "cmd /x /c taskkill /PID 1",
    "cmd foo/c taskkill /PID 1", "cmd echo x /c taskkill /PID 1", 'cmd "/c taskkill /PID 1"',
    "cmd //c taskkill //PID 1", "cmd /c=taskkill /PID 1", "cmd /c,taskkill /PID 1", "cmd /c;taskkill /PID 1",
    "cmd /r=taskkill /PID 1", "cmd /k=taskkill /PID 1", "cmd /c   taskkill /PID 1", "cmd /c\ttaskkill /PID 1",
    "CMD.EXE /R taskkill /PID 1", "cmd.exe /c(taskkill /PID 1)",
    'powershell -NoProfile -Command "cmd /r taskkill /PID 1"', "bash -c 'cmd /ctaskkill /PID 1'",
]
# Nested shells whose command text the hook can place, or that run nothing it reads.
NESTED_PLACED = [
    'powershell -NoProfile -Command "Get-Date"',
    "powershell -ExecutionPolicy Bypass -File s.ps1",
    "powershell -NoProfile", "powershell", "pwsh", "powershell -NoLogo -NoProfile",
    "powershell -File s.ps1 -e foo",
    "powershell -File s.ps1 -c Stop-Process",
    "powershell -NoProfile -Command Get-Help Get-Process -e",
    "powershell -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File s.ps1",
    "powershell -nop -noni -ep Bypass -f s.ps1 -Tag x",
    "powershell -InputFormat Text -OutputFormat Text -Command Get-Date",
    "powershell -if Text -of Text -Command Get-Date",
    "powershell -Version 2 -Command Get-Date",
    "powershell -sta -Command Get-Date", "powershell -mta -Command Get-Date",
    "powershell -config x -Command Get-Date", "powershell -psc x.psc1 -Command Get-Date",
    "powershell /NoProfile /File s.ps1", "powershell /nop /c Get-Date",
    "pwsh -NoProfile -Command Get-Date", "pwsh -nop -noni -ex Bypass -f s.ps1",
    "echo x | powershell -Command Get-Date", "echo x | powershell -File s.ps1",
    "false || powershell -NoProfile", "false || cmd",
    "cmd /c echo hi", "cmd", "cmd /?", "cmd /c", "cmd /d /c echo hi", "cmd /home/rob echo hi", "cmd /k echo hi",
    'cmd.exe /d /s /c "echo hi"', "echo x | cmd /c echo hi",
]
NESTED_PLACED_POWERSHELL_ONLY = [
    r"powershell -ExecutionPolicy Bypass -NoProfile -File .claude\skills\trace-port-traffic\trace_port.ps1 -Port 1",
    r"powershell.exe -ExecutionPolicy Bypass -NoProfile -File x.ps1",
    r"powershell -ExecutionPolicy Bypass -File scripts\build_base.ps1 -Tag image:tag",
]


def payload(command, tool="Bash", **extra):
    return {"tool_name": tool, "tool_input": {"command": command}, **extra}


def denies_raw(command, tool):
    blocks, _ = gate(payload(command, tool))
    return any(RAW in b for b in blocks)


class CorpusTests(unittest.TestCase):
    def test_raw_kills_are_denied_for_bash_and_powershell(self):
        for tool in ("Bash", "PowerShell"):
            extra = BASH_ONLY_DENIED if tool == "Bash" else POWERSHELL_ONLY_DENIED
            for command in DENIED + extra:
                with self.subTest(tool=tool, command=command):
                    self.assertTrue(denies_raw(command, tool))

    def test_text_that_is_not_run_and_liveness_probes_pass(self):
        for tool in ("Bash", "PowerShell"):
            extra = POWERSHELL_ONLY_ALLOWED if tool == "PowerShell" else BASH_ONLY_ALLOWED
            for command in ALLOWED + extra:
                with self.subTest(tool=tool, command=command):
                    self.assertFalse(denies_raw(command, tool))
                    self.assertIsNone(pk.check(payload(command, tool)))
                    self.assertIsNone(pk.updated_input(payload(command, tool)))

    def test_the_deny_message_names_the_helper_and_the_listing_step(self):
        blocks, _ = gate(payload("Stop-Process -Id 1"))
        message = next(b for b in blocks if RAW in b)
        self.assertIn("[process-kill] Stop-Process is denied", message)
        self.assertIn("proc_scan.py --match <regex>", message)
        self.assertIn("kill_owned.py --pid <pid>", message)
        self.assertIn("--watch", message)
        self.assertIn("A REFUSE line is final", message)

    def test_a_tool_that_is_not_a_shell_is_ignored(self):
        self.assertIsNone(pk.check({"tool_name": "Read", "tool_input": {"command": "kill 1"}}))
        self.assertIsNone(pk.check({"tool_name": "Bash", "tool_input": {}}))

    def test_a_kill_owned_call_is_not_a_raw_kill(self):
        blocks, _ = gate(payload(f"{HELPER} --worktree {WORKTREE} --pid 1 --tree"))
        self.assertFalse(any(RAW in b for b in blocks))

    def test_a_nested_shell_is_scanned_with_the_nested_shells_quoting(self):
        for outer, command, verb in (
            ("Bash", "pwsh -Command 'Get-ChildItem \"D:\\work\\\"; Stop-Process -Id 1'", "Stop-Process"),
            ("PowerShell", "bash -c 'echo `kill 1`'", "kill"),
        ):
            with self.subTest(outer=outer):
                self.assertEqual(pk._raw_kill(command, outer), verb)
        self.assertIsNone(pk._raw_kill('Get-ChildItem "D:\\work\\"; Stop-Process -Id 1', "Bash"))
        self.assertIsNone(pk._raw_kill("echo `kill 1`", "PowerShell"))

    def test_every_spelling_powershell_takes_for_its_command_parameter_is_scanned(self):
        for shell in ("powershell", "pwsh"):
            for switch in POWERSHELL_COMMAND_SWITCHES:
                for tool in ("Bash", "PowerShell"):
                    command = f'{shell} -NoProfile {switch} "Stop-Process -Id 1"'
                    with self.subTest(tool=tool, command=command):
                        self.assertTrue(denies_raw(command, tool))

    def test_the_gate_carries_the_encoded_command_deny(self):
        for tool in ("Bash", "PowerShell"):
            with self.subTest(tool=tool):
                blocks, _ = gate(payload("powershell -NoProfile -enc AAAA", tool))
                self.assertIn(pk.DENY_ENCODED, blocks)

    def test_the_gate_carries_the_nested_shell_deny(self):
        for tool in ("Bash", "PowerShell"):
            with self.subTest(tool=tool):
                blocks, _ = gate(payload("powershell -NoProfile Stop-Process -Id 1", tool))
                self.assertIn(pk.DENY_NESTED, blocks)

    def test_a_nested_shell_whose_command_text_cannot_be_placed_is_denied_naming_the_readable_form(self):
        for tool in ("Bash", "PowerShell"):
            for command in NESTED_UNPLACED + NESTED_FED:
                with self.subTest(tool=tool, command=command):
                    self.assertEqual(pk.check(payload(command, tool)), pk.DENY_NESTED)
                    self.assertIsNone(pk.updated_input(payload(command, tool)))

    def test_the_nested_shell_deny_names_the_forms_that_are_read(self):
        for needle in ('powershell -NoProfile -Command "<text>"', "powershell -File <script>", 'cmd /c "<text>"',
                       "stdin"):
            self.assertIn(needle, pk.DENY_NESTED)

    def test_cmd_runs_the_text_after_the_first_c_k_or_r_wherever_it_sits_and_that_text_is_scanned(self):
        for tool in ("Bash", "PowerShell"):
            for command in CMD_RUNS:
                with self.subTest(tool=tool, command=command):
                    self.assertTrue(denies_raw(command, tool))

    def test_a_nested_shell_whose_text_is_placed_or_that_runs_nothing_passes(self):
        for tool in ("Bash", "PowerShell"):
            extra = NESTED_PLACED_POWERSHELL_ONLY if tool == "PowerShell" else []
            for command in NESTED_PLACED + extra:
                with self.subTest(tool=tool, command=command):
                    self.assertIsNone(pk.check(payload(command, tool)))
                    self.assertIsNone(pk.updated_input(payload(command, tool)))

    def test_every_spelling_powershell_takes_for_a_switch_is_placed(self):
        for shell in ("powershell", "pwsh"):
            for name, (shortest, takes_value) in pk.PS_SWITCHES.items():
                if name in ("command", "file", "encodedcommand") or (shell == "pwsh" and name in pk.PWSH_UNPLACED):
                    continue
                value = " Bypass" if takes_value else ""
                for lead in "-/":
                    for n in range(len(shortest), len(name) + 1):
                        for spelling in (name[:n], name[:n].upper()):
                            command = f"{shell} {lead}{spelling}{value} -Command Get-Date"
                            with self.subTest(command=command):
                                self.assertIsNone(pk.check(payload(command)))

    def test_an_alias_and_a_nested_script_name_are_placed_and_the_command_text_after_either_is_scanned(self):
        for command in ("powershell -ep Bypass -File s.ps1", "powershell -if Text -of Text -Command Get-Date"):
            with self.subTest(command=command):
                self.assertIsNone(pk.check(payload(command)))
        for command in ("powershell -ep Bypass -c Stop-Process -Id 1", "powershell -nop -File s.ps1; kill 1",
                        "powershell -nonint -noprofile -ex Bypass -w Hidden -c taskkill /PID 1"):
            with self.subTest(command=command):
                self.assertTrue(denies_raw(command, "Bash"))

    def test_a_script_argument_after_file_is_not_the_encoded_switch_and_positional_text_that_is_one_is_denied(self):
        self.assertIsNone(pk.check(payload("powershell -NoProfile -File s.ps1 -e foo")))
        self.assertEqual(pk.check(payload("powershell -NoProfile Get-Help Get-Process -e")), pk.DENY_NESTED)
        self.assertEqual(pk.check(payload("powershell -NoProfile -e AAAA -File s.ps1")), pk.DENY_ENCODED)


class ScanTests(unittest.TestCase):
    ALPHABET = ["'", '"', "`", "$", "(", ")", "\\", ";", "|", "&", "#", "{", "}", "\n", " ", "kill", "$(", "''", '""',
                '\\"', "@'", "'@", "<<", "EOF", "a", ">", "<", "<<<", "python", "kill_owned.py", "cat", "-", "uv run",
                "powershell", "pwsh", "cmd", "/c", "/k", "/r", "-Command", "-File", "-ExecutionPolicy", "-e",
                "-NoExit", " -", "kill-tokens", "--worktree", "--hook-token"]
    WORDS = ["", "-", "--", "-c", "-Command", "-command:x", "-File", "-f", "-e", "-ec", "-EncodedCommand", "-NoExit",
             "-noe", "-ExecutionPolicy", "-ex", "-ep", "Bypass", "-i", "-v", "x y", "kill", "Stop-Process", "/c", "/k",
             "/r", "/cx", "foo/c", "=", ",", ";", "@", "\t", "-nop", "-mta", "-config", "/", "//c"]

    def test_the_residue_keeps_the_commands_length_and_no_scan_raises(self):
        rng = random.Random(59)
        for _ in range(2000):
            text = "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, 40)))
            for tool in ("Bash", "PowerShell"):
                try:
                    body, residue = pk._prepare(tool, text)
                    pk._raw_kill(text, tool)
                    pk._kill_owned_call(tool, text)
                    pk._is_whole_command(tool, text)
                except Exception as exc:
                    self.fail(f"{tool} {text!r}: {type(exc).__name__}: {exc}")
                self.assertEqual(len(body), len(residue), (tool, text))

    def test_the_nested_shell_walkers_never_raise_and_return_text_or_a_problem_never_both(self):
        rng = random.Random(61)
        for _ in range(3000):
            args = [rng.choice(self.WORDS) for _ in range(rng.randint(0, 7))]
            for name in ("powershell", "pwsh", "cmd", "bash"):
                for fed in (False, True):
                    try:
                        result = pk._nested_shell(name, args, fed)
                    except Exception as exc:
                        self.fail(f"{name} {args!r} fed={fed}: {type(exc).__name__}: {exc}")
                    if result is None:
                        continue
                    self.assertIsInstance(result, pk.Nested, (name, args, fed))
                    self.assertIn(result.problem, ("", pk.ENCODED, pk.UNPLACED), (name, args, fed))
                    self.assertFalse(result.problem and result.text, (name, args, fed))

    def test_the_powershell_scan_follows_powershell_quoting(self):
        for text, expected in (
            ("echo 'it''s' ; x", "echo " + " " * 7 + " ; x"),
            ('echo "a""b" ; x', "echo " + " " * 6 + " ; x"),
            ('echo "a`"b" ; x', "echo " + " " * 6 + " ; x"),
            (r'echo "a\" ; x', "echo " + " " * 4 + " ; x"),
            ("echo a`; b", "echo a   b"),
            ('echo "$(x)" ; y', "echo " + " " + " (" + "x" + "  " + " ; y"),
            ('echo "$((x) + 1)" ; y', "echo " + " " + " (" + "(x) + 1" + "  " + " ; y"),
        ):
            with self.subTest(text=text):
                self.assertEqual(pk._ps_unquoted_residue(text), expected)

    def test_the_bash_scan_marks_only_a_substitution_opened_inside_double_quotes(self):
        for text, expected in (
            ('x="$(kill 1)"', "x=  (kill 1  "),
            ("echo '$(kill 1)'", "echo " + " " * 11),
            (r'echo "\$(kill 1)"', "echo " + " " * 12),
            ('echo "$(echo "$(kill 1)")"', "echo " + "  (" + "echo " + "  (" + "kill 1" + " " * 4),
            ("echo $(kill 1)", "echo $(kill 1)"),
        ):
            with self.subTest(text=text):
                self.assertEqual(pk._bash_residue(text), expected)

    def test_the_bash_scan_exposes_a_backtick_body_inside_double_quotes_and_blanks_unquoted_escapes(self):
        escaped_ticks = 'echo "a \\`kill 1\\` b"'
        for text, expected in (
            ('echo "x `kill 1`"', "echo " + "   " + "`kill 1`" + " "),
            ("echo '`kill 1`'", "echo " + " " * 10),
            ('echo "a `kill 1"', "echo " + " " * 11),
            (escaped_ticks, "echo " + " " * (len(escaped_ticks) - 5)),
            (r"echo It\'s; kill 1", "echo It  s; kill 1"),
            (r"echo a\; kill 1", "echo a   kill 1"),
            ("echo a\\", "echo a "),
        ):
            with self.subTest(text=text):
                self.assertEqual(pk._bash_residue(text), expected)


class Transcripts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="process-kill-hook-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tokens = self.tmp / "tokens"
        patched = mock.patch.object(kill_token, "token_dir", return_value=self.tokens)
        patched.start()
        self.addCleanup(patched.stop)

    def token_files(self):
        return sorted(self.tokens.glob("*.json")) if self.tokens.exists() else []

    def split_token(self, command):
        """(the command without its trailing `--hook-token <nonce>`, the record of the token file that nonce names)."""
        head, sep, nonce = command.rpartition(" --hook-token ")
        self.assertTrue(sep, command)
        self.assertRegex(nonce, r"^[0-9a-f]{32}$")
        return head, json.loads((self.tokens / f"{nonce}.json").read_text(encoding="utf-8"))

    def write(self, name, records):
        path = self.tmp / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return str(path)

    def agent_transcript(self, prompt="story_id: tq-59\nworktree_path: " + WORKTREE + "\nspec_path: x", agent_id="a1"):
        return self.write(f"agent-{agent_id}.jsonl", [
            {"type": "user", "timestamp": STAMP, "message": {"role": "user", "content": prompt}},
            {"type": "assistant", "timestamp": "2026-10-16T08:36:00.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}])

    def subagent(self, command, transcript=None, **ti):
        transcript = transcript or self.agent_transcript()
        return {"tool_name": "Bash", "agent_type": "story-impl", "agent_id": "a1", "session_id": "s1",
                "transcript_path": transcript, "tool_input": {"command": command, **ti}}

    def session(self, command, records=None, tool="Bash"):
        records = records if records is not None else [
            {"type": "summary", "summary": "x"},
            {"type": "user", "timestamp": "2026-10-16T07:00:00.000Z", "message": {"role": "user", "content": "hi"}},
            {"type": "user", "timestamp": "2026-10-16T09:00:00.000Z", "message": {"role": "user", "content": "later"}}]
        return {"tool_name": tool, "session_id": "s1", "transcript_path": self.write("session.jsonl", records),
                "tool_input": {"command": command}}


class RewriteTests(Transcripts):
    def test_a_subagent_call_gets_its_dispatch_worktree_and_first_timestamp(self):
        p = self.subagent(f"{HELPER} --pid 33652 --tree", description="stop my orphans", timeout=5000)
        self.assertIsNone(pk.check(p))
        out = pk.updated_input(p)
        command, record = self.split_token(out["command"])
        self.assertEqual(command, f"{HELPER} --pid 33652 --tree --worktree {WORKTREE} --since {STAMP}")
        self.assertEqual((record["worktree"], record["since"]), (WORKTREE, STAMP))
        self.assertEqual(out["description"], "stop my orphans")
        self.assertEqual(out["timeout"], 5000)

    def test_the_rewrite_works_for_powershell_and_for_bold_or_backticked_dispatch_keys(self):
        for prompt in ("**worktree_path**: `" + WORKTREE + "`", "worktree_path = " + WORKTREE,
                       "- worktree_path: \"" + WORKTREE + "\""):
            with self.subTest(prompt=prompt):
                p = self.subagent(f"{HELPER} --pid 1", transcript=self.agent_transcript(prompt))
                p["tool_name"] = "PowerShell"
                command, record = self.split_token(pk.updated_input(p)["command"])
                self.assertTrue(command.endswith(f"--worktree {WORKTREE} --since {STAMP}"))
                self.assertEqual((record["worktree"], record["since"]), (WORKTREE, STAMP))

    def test_a_backslash_worktree_path_is_appended_with_forward_slashes(self):
        p = self.subagent(f"{HELPER} --pid 1", transcript=self.agent_transcript(
            "worktree_path: D:\\work\\proj-worktrees\\tq-59"))
        command, record = self.split_token(pk.updated_input(p)["command"])
        self.assertTrue(command.endswith(f"--worktree {WORKTREE} --since {STAMP}"))
        self.assertEqual(record["worktree"], WORKTREE)

    def test_a_uv_option_that_takes_a_value_does_not_hide_the_helper(self):
        command = f"uv run --no-project --with psutil {SCRIPT} --pid 1"
        p = self.subagent(command)
        self.assertIsNone(pk.check(p))
        rewritten, _ = self.split_token(pk.updated_input(p)["command"])
        self.assertEqual(rewritten, f"{command} --worktree {WORKTREE} --since {STAMP}")

    def test_gate_returns_no_block_for_a_valid_call_and_the_entry_emits_updated_input_only(self):
        p = self.subagent(f"{HELPER} --pid 1")
        self.assertEqual(gate(p), ([], None))
        self.assertEqual(self.token_files(), [])
        r = subprocess.run([sys.executable, "-c", ENTRY_RUNNER, str(SCRIPTS), str(self.tokens), str(ENTRY)],
                           input=json.dumps(p), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", out)
        command, record = self.split_token(out["updatedInput"]["command"])
        self.assertEqual(command, f"{HELPER} --pid 1 --worktree {WORKTREE} --since {STAMP}")
        self.assertEqual((record["worktree"], record["since"]), (WORKTREE, STAMP))
        self.assertEqual(len(self.token_files()), 1)

    def test_the_real_entry_denies_a_raw_stop_process_with_hook_specific_output(self):
        r = subprocess.run([sys.executable, str(ENTRY)], input=json.dumps(payload("Stop-Process -Id 33652 -Force")),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(RAW, out["permissionDecisionReason"])

    def test_the_real_entry_prints_nothing_for_an_ordinary_command(self):
        r = subprocess.run([sys.executable, str(ENTRY)], input=json.dumps(payload("git status")),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_a_main_session_call_with_a_worktree_gets_the_first_timestamp_appended(self):
        p = self.session(f"{HELPER} --worktree {WORKTREE} --pid 1")
        self.assertIsNone(pk.check(p))
        command, record = self.split_token(pk.updated_input(p)["command"])
        self.assertEqual(command, f"{HELPER} --worktree {WORKTREE} --pid 1 --since 2026-10-16T07:00:00.000Z")
        self.assertEqual((record["worktree"], record["since"]), (WORKTREE, "2026-10-16T07:00:00.000Z"))

    def test_a_main_session_worktree_in_the_equals_form_or_quoted_is_bound_into_the_token(self):
        for named in (f"--worktree={WORKTREE}", f'--worktree "{WORKTREE}"', f"--worktree '{WORKTREE}'"):
            with self.subTest(named=named):
                p = self.session(f"{HELPER} {named} --watch")
                command, record = self.split_token(pk.updated_input(p)["command"])
                self.assertTrue(command.endswith(" --since 2026-10-16T07:00:00.000Z"))
                self.assertEqual(record["worktree"], WORKTREE)

    def test_a_command_that_is_not_a_kill_owned_call_is_not_rewritten(self):
        self.assertIsNone(pk.updated_input(self.subagent("git status")))
        self.assertIsNone(pk.updated_input(self.subagent("uv run --no-project scripts/proc_scan.py --match x")))

    def test_a_command_that_only_names_kill_owned_is_not_a_call(self):
        for command in ("python -m py_compile scripts/kill_owned.py", "python -m unittest scripts.tests.test_kill_owned",
                        "cat scripts/kill_owned.py", "grep -n kill_owned scripts/INDEX.md",
                        "git diff -- scripts/kill_owned.py", "Select-String -Path scripts/kill_owned.py -Pattern x",
                        "git show HEAD:scripts/kill_owned.py", "git log --oneline -- scripts/kill_owned.py",
                        "rg kill_owned scripts", "Get-Content scripts/kill_owned.py",
                        "uv run --no-project scripts/proc_scan.py --match kill_owned",
                        "uv run --no-project scripts/proc_scan.py --match kill_owned.py",
                        "uv run ruff check scripts/kill_owned.py",
                        "python scripts/lint_on_commit.py scripts/kill_owned.py",
                        'uv run --no-project scripts/story_record.py append tq-59 "note on kill_owned.py ordering"'):
            with self.subTest(command=command):
                p = self.subagent(command)
                self.assertIsNone(pk.check(p))
                self.assertIsNone(pk.updated_input(p))

    def test_a_hash_inside_a_word_is_not_a_comment(self):
        p = self.session(f"{HELPER} --worktree {WORKTREE} --pid 1#1")
        self.assertIsNone(pk.check(p))
        command, record = self.split_token(pk.updated_input(p)["command"])
        self.assertTrue(command.endswith(" --since 2026-10-16T07:00:00.000Z"))
        self.assertEqual(record["worktree"], WORKTREE)

    def test_in_powershell_a_main_session_worktree_with_backslashes_is_rewritten_and_bound_as_typed(self):
        path = r"D:\work\proj-worktrees\tq-59"
        for named in (f"--worktree {path}", f'--worktree "{path}"', f"--worktree '{path}'"):
            with self.subTest(named=named):
                p = self.session(f"{HELPER} {named} --pid 1", tool="PowerShell")
                self.assertIsNone(pk.check(p))
                command, record = self.split_token(pk.updated_input(p)["command"])
                self.assertTrue(command.endswith(" --since 2026-10-16T07:00:00.000Z"))
                self.assertEqual(record["worktree"], path)

    def test_a_powershell_backtick_continuation_is_one_command(self):
        p = self.subagent(f"{HELPER} --pid 1 `\n--tree")
        p["tool_name"] = "PowerShell"
        self.assertIsNone(pk.check(p))
        command, _ = self.split_token(pk.updated_input(p)["command"])
        self.assertEqual(command, f"{HELPER} --pid 1 `\n--tree --worktree {WORKTREE} --since {STAMP}")


class TokenTests(Transcripts):
    """The hook writes one single-use token per rewritten call and kill_owned.py redeems it (AC1)."""

    def test_check_writes_no_token_and_each_rewrite_writes_exactly_one(self):
        p = self.subagent(f"{HELPER} --pid 1")
        self.assertIsNone(pk.check(p))
        self.assertEqual(gate(p), ([], None))
        self.assertEqual(self.token_files(), [])
        first = pk.updated_input(p)["command"].rpartition(" --hook-token ")[2]
        second = pk.updated_input(p)["command"].rpartition(" --hook-token ")[2]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.token_files()), 2)

    def test_a_call_that_is_ignored_or_denied_writes_no_token(self):
        for p in (self.subagent("git status"), self.subagent(f"python {SCRIPT} --pid 1"),
                  self.subagent(f"{HELPER} --pid 1 --since 2026-01-01T00:00:00Z"), self.subagent("Stop-Process -Id 1"),
                  self.subagent(f"{HELPER} --pid 1 --hook-token {'0' * 32}"), self.session(f"{HELPER} --pid 1"),
                  self.subagent(f"{HELPER} --pid 1 && echo done"), self.subagent("powershell -NoProfile kill 1")):
            with self.subTest(command=p["tool_input"]["command"]):
                self.assertIsNone(pk.updated_input(p))
        self.assertEqual(self.token_files(), [])

    def test_the_helper_redeems_exactly_the_token_the_hook_wrote_and_only_once(self):
        for p in (self.subagent(f"{HELPER} --pid 33652 --tree"),
                  self.session(f"{HELPER} --worktree {WORKTREE} --watch")):
            command = pk.updated_input(p)["command"]
            words = command.split()
            args = ko.parse_args(words[words.index(SCRIPT) + 1:])
            with self.subTest(command=command):
                self.assertEqual(args.worktree, WORKTREE)
                self.assertIsNone(ko.token_problem(args))
                self.assertIn("already used", ko.token_problem(args))

    def test_the_helper_refuses_a_token_for_other_facts_than_the_call_carries(self):
        for field, value in (("since", "2030-01-01T00:00:00Z"), ("worktree", "D:/work/proj-worktrees/tq-5")):
            words = pk.updated_input(self.subagent(f"{HELPER} --pid 1"))["command"].split()
            args = ko.parse_args(words[words.index(SCRIPT) + 1:])
            setattr(args, field, value)
            with self.subTest(field=field):
                self.assertIn("another --worktree or --since", ko.token_problem(args))

    def test_a_token_that_cannot_be_written_raises_and_the_gate_leaves_the_call_unrewritten(self):
        p = self.subagent(f"{HELPER} --pid 1")
        with mock.patch.object(kill_token, "issue", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                pk.updated_input(p)
            self.assertIsNone(_run(pk.updated_input, p))
            self.assertIsNone(pk.check(p))

    def test_the_real_entry_prints_nothing_when_the_token_directory_cannot_be_created(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where the directory must go", encoding="utf-8")
        r = subprocess.run([sys.executable, "-c", ENTRY_RUNNER, str(SCRIPTS), str(blocker / "tokens"), str(ENTRY)],
                           input=json.dumps(self.subagent(f"{HELPER} --pid 1")), capture_output=True, text=True,
                           timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


class DenyTests(Transcripts):
    def denied(self, p, needle):
        message = pk.check(p)
        self.assertIsNotNone(message, p["tool_input"]["command"])
        self.assertIn(needle, message)
        self.assertIsNone(pk.updated_input(p))

    def test_a_supplied_hook_token_is_denied_in_every_spelling_for_a_subagent_and_a_main_session(self):
        for extra in ("--hook-token abc", "--hook-token=abc", f"--hook-token {'0' * 32}"):
            for who, p in (("subagent", self.subagent(f"{HELPER} --pid 1 {extra}")),
                           ("session", self.session(f"{HELPER} --worktree {WORKTREE} --pid 1 {extra}"))):
                with self.subTest(extra=extra, who=who):
                    self.denied(p, "[process-kill]")
                    self.assertIn("--hook-token", pk.check(p))
        self.assertEqual(self.token_files(), [])

    def test_a_call_not_launched_by_uv_that_supplies_a_token_is_denied_for_both_reasons(self):
        message = pk.check(self.subagent(f"python {SCRIPT} --pid 1 --hook-token {'0' * 32}"))
        self.assertIn("the hook supplies --hook-token", message)
        self.assertIn(SANCTIONED, message)

    def test_a_main_session_call_that_names_worktree_twice_or_without_a_path_is_denied(self):
        for tail in (f"--worktree {WORKTREE} --worktree D:/other", f"--worktree={WORKTREE} --worktree D:/other",
                     f"--worktree D:/other --worktree={WORKTREE}"):
            with self.subTest(tail=tail):
                self.denied(self.session(f"{HELPER} --pid 1 {tail}"), "name --worktree once")
        self.denied(self.session(f"{HELPER} --pid 1 --worktree"), "needs a path")

    def test_a_main_session_worktree_that_is_not_a_plain_absolute_path_is_denied_naming_the_plain_form(self):
        both = ('--worktree "$WT"', "--worktree ~/x", f"--worktree {WORKTREE}#1")
        only = {"PowerShell": ("--worktree $env:WT",), "Bash": ("--worktree ~/proj-worktrees/x",)}
        for tool in ("Bash", "PowerShell"):
            for named in both + only[tool]:
                with self.subTest(tool=tool, named=named):
                    self.denied(self.session(f"{HELPER} {named} --pid 1", tool=tool), "plain absolute path")
        self.assertEqual(self.token_files(), [])

    def test_in_bash_a_main_session_worktree_with_a_backslash_is_denied_quoted_or_not(self):
        path = r"D:\work\proj-worktrees\tq-59"
        for named in (f"--worktree {path}", f'--worktree "{path}"', f"--worktree '{path}'", f'--worktree="{path}"'):
            with self.subTest(named=named):
                self.denied(self.session(f"{HELPER} {named} --pid 1"), "forward slashes")
        self.assertEqual(self.token_files(), [])

    def test_a_shell_command_naming_the_token_directory_is_denied_in_both_shells(self):
        for command in (
            "cp note.json D:/work/proj/.story-setup/kill-tokens/" + "0" * 32 + ".json",
            "echo '{}' > .story-setup/kill-tokens/a.json",
            "ls .story-setup/KILL-TOKENS",
            "python -c \"open('.story-setup/kill-tokens/a.json', 'w')\"",
            "bash -c 'echo > .story-setup/kill-tokens/a.json'",
            "Set-Content -Path .story-setup\\kill-tokens\\a.json -Value '{}'",
            "cat <<'EOF'\nkill-tokens\nEOF",
            "git status  # kill-tokens",
            f"{HELPER} --pid 1 --worktree {WORKTREE} --note kill-tokens",
        ):
            for tool in ("Bash", "PowerShell"):
                with self.subTest(command=command, tool=tool):
                    self.assertEqual(pk.check(payload(command, tool)), pk.DENY_TOKEN_DIR)
                    self.assertIn(pk.DENY_TOKEN_DIR, gate(payload(command, tool))[0])
                    self.assertIsNone(pk.updated_input(payload(command, tool)))

    def test_the_token_directory_deny_says_only_the_hook_writes_a_token(self):
        self.assertIn("only the process-kill hook writes a hook token", pk.DENY_TOKEN_DIR)

    def test_a_command_that_names_the_token_module_but_not_the_directory_passes(self):
        for command in ("uv run --no-project scripts/tests/test_kill_token.py", "git diff -- scripts/kill_token.py",
                        "cat scripts/kill_token.py", "python -m py_compile scripts/kill_token.py"):
            with self.subTest(command=command):
                self.assertIsNone(pk.check(payload(command)))

    def test_supplied_worktree_since_and_worktree_root_are_denied_in_every_spelling(self):
        for extra in ("--worktree D:/x", "--worktree=D:/x", "--since 2026-01-01T00:00:00Z", "--since=2026-01-01",
                      "--worktree-root D:/x", "--worktree-root=D:/x"):
            with self.subTest(extra=extra):
                self.denied(self.subagent(f"{HELPER} --pid 1 {extra}"), "[process-kill]")

    def test_the_hook_supplies_message_names_the_flags(self):
        self.denied(self.subagent(f"{HELPER} --pid 1 --worktree D:/x"), "the hook supplies")

    def test_a_compound_command_is_denied(self):
        for tail in ("&& echo done", "; echo done", "| tee out.txt", "> out.txt", "2>&1 | cat", "$(echo 1)",
                     "`echo 1`", "|| true"):
            with self.subTest(tail=tail):
                self.denied(self.subagent(f"{HELPER} --pid 1 {tail}"), "own command")
        self.denied(self.subagent(f"cd D:/x && {HELPER} --pid 1"), "own command")

    def test_a_substitution_inside_double_quotes_is_part_of_a_compound_command(self):
        for tool in ("Bash", "PowerShell"):
            p = self.subagent(f'{HELPER} --pid "$(echo 1)"')
            p["tool_name"] = tool
            with self.subTest(tool=tool):
                self.denied(p, "own command")

    def test_a_trailing_comment_is_denied_in_both_shells(self):
        for tool in ("Bash", "PowerShell"):
            p = self.subagent(f"{HELPER} --pid 1 --tree  # stop my orphans")
            p["tool_name"] = tool
            with self.subTest(tool=tool):
                self.denied(p, "own command")

    def test_a_trailing_backtick_that_would_swallow_the_appended_facts_is_denied(self):
        p = self.subagent(f"{HELPER} --pid 1 `\n")
        p["tool_name"] = "PowerShell"
        self.denied(p, "own command")

    def test_a_kill_owned_call_not_launched_by_uv_is_denied_naming_the_sanctioned_form(self):
        for call in (f"D:/work/proj/.venv/Scripts/python.exe {SCRIPT} --pid 1",
                     f"python {SCRIPT} --pid 1", f"python3 -u {SCRIPT} --pid 1", f"py -3 {SCRIPT} --pid 1",
                     f"{SCRIPT} --pid 1", f"cd D:/x && python {SCRIPT} --pid 1"):
            with self.subTest(call=call):
                self.denied(self.subagent(call), SANCTIONED)

    def test_a_kill_owned_call_nested_in_another_shell_is_denied_naming_the_sanctioned_form(self):
        for call in (f'cmd /c "{HELPER} --pid 1"', f"cmd.exe /k {HELPER} --pid 1", f"bash -c '{HELPER} --pid 1'",
                     f'powershell -NoProfile -Command "& {HELPER} --pid 1"',
                     f'powershell -NoProfile -Comm "& {HELPER} --pid 1"',
                     f"bash -c \"bash -c '{HELPER} --pid 1'\""):
            for tool in ("Bash", "PowerShell"):
                p = self.subagent(call)
                p["tool_name"] = tool
                with self.subTest(call=call, tool=tool):
                    self.denied(p, SANCTIONED)

    def test_a_kill_owned_call_nested_in_every_powershell_command_spelling_is_denied(self):
        transcript = self.agent_transcript()
        for shell in ("powershell", "pwsh"):
            for switch in POWERSHELL_COMMAND_SWITCHES:
                call = f'{shell} -NoProfile {switch} "& {HELPER} --pid 1"'
                for tool in ("Bash", "PowerShell"):
                    p = self.subagent(call, transcript=transcript)
                    p["tool_name"] = tool
                    with self.subTest(call=call, tool=tool):
                        self.denied(p, SANCTIONED)

    def test_powershell_encoded_command_is_denied_unread_in_every_spelling_and_shell(self):
        transcript = self.agent_transcript()
        for text in ("Stop-Process -Id 1", f"{HELPER} --pid 1 --worktree D:/other --since 1970-01-01T00:00:00Z"):
            blob = base64.b64encode(text.encode("utf-16-le")).decode()
            for shell in ("powershell", "pwsh"):
                for switch in POWERSHELL_ENCODED_SWITCHES:
                    for tool in ("Bash", "PowerShell"):
                        p = self.subagent(f"{shell} -NoProfile {switch} {blob}", transcript=transcript)
                        p["tool_name"] = tool
                        with self.subTest(text=text, shell=shell, switch=switch, tool=tool):
                            self.denied(p, pk.DENY_ENCODED)

    def test_an_encoded_command_is_denied_wherever_the_check_reads_a_command(self):
        blob = base64.b64encode("Stop-Process -Id 1".encode("utf-16-le")).decode()
        transcript = self.agent_transcript()
        for call, needle in (
            (f"powershell -ExecutionPolicy Bypass -enc {blob}", pk.DENY_ENCODED),
            (f"powershell -NoProfile -NonInteractive -e {blob}", pk.DENY_ENCODED),
            (f"& powershell -e {blob}", pk.DENY_ENCODED),
            (f"echo a; powershell -enc {blob}", pk.DENY_ENCODED),
            (f"bash -c 'powershell -enc {blob}'", pk.DENY_ENCODED),
            (f"cmd /c powershell -enc {blob}", pk.DENY_ENCODED),
            (f'powershell -Command "powershell -enc {blob}"', pk.DENY_ENCODED),
            (f"uv run --no-project powershell -enc {blob}", SANCTIONED),
            (f"uv run --no-project pwsh -ec {blob}", SANCTIONED),
        ):
            for tool in ("Bash", "PowerShell"):
                p = self.subagent(call, transcript=transcript)
                p["tool_name"] = tool
                with self.subTest(call=call, tool=tool):
                    self.denied(p, needle)

    def test_the_encoded_command_deny_names_the_plain_command_form(self):
        message = pk.check(self.subagent("powershell -enc AAAA"))
        self.assertIn("[process-kill] powershell -EncodedCommand is denied", message)
        self.assertIn("plain text with -Command", message)

    def test_a_python_option_that_takes_a_value_does_not_hide_the_helper(self):
        transcript = self.agent_transcript()
        for call in PY_VALUE_OPTION_RUNS:
            for tool in ("Bash", "PowerShell"):
                p = self.subagent(call, transcript=transcript)
                p["tool_name"] = tool
                with self.subTest(call=call, tool=tool):
                    self.denied(p, SANCTIONED)

    def test_every_other_route_that_can_launch_kill_owned_is_denied_naming_the_sanctioned_form(self):
        for call in (
            "python -m kill_owned --worktree D:/other --since 1970-01-01T00:00:00Z --pid 1",
            "python -m scripts.kill_owned --pid 1",
            "python3 -u -m kill_owned --pid 1",
            "python -um kill_owned --pid 1",
            f"python - --pid 1 < {SCRIPT}",
            f"python -c \"import runpy; runpy.run_path('{SCRIPT}')\"",
            f"python -m cProfile {SCRIPT} --pid 1",
            f"timeout 600 {HELPER} --pid 1",
            f"nice {HELPER} --pid 1",
            f"env -i {HELPER} --pid 1",
            f"sudo -u x python {SCRIPT} --pid 1",
            f'eval "{HELPER} --pid 1"',
            f'iex "{HELPER} --pid 1"',
            f"Start-Process uv -ArgumentList run,--no-project,{SCRIPT},--pid,1",
            "uv run --no-project python -m kill_owned --pid 1",
            "PYTHONPATH=D:/work/proj/scripts uv run --no-project --with psutil "
            "python -m kill_owned --pid 1",
            f'uv run --no-project bash -c "python {SCRIPT} --pid 1"',
            "uv run --no-project python -c \"import runpy,sys; sys.argv=['k','--worktree','D:/other',"
            f"'--since','1970-01-01T00:00:00Z','--pid','1']; runpy.run_path('{SCRIPT}', run_name='__main__')\" {SCRIPT}",
            f"uv run --no-project $SHELL -c 'x' {SCRIPT}",
            f"uv run --no-project python {SCRIPT} --pid 1",
            f"K={SCRIPT}; python $K --pid 1",
            "python D:/work/proj/scripts/kill_own*.py --pid 1",
            f"uv run --no-project powershell -NoProfile {SCRIPT} --pid 1",
            f"uv run --no-project powershell -File {SCRIPT} --pid 1",
        ):
            for tool in ("Bash", "PowerShell"):
                p = self.subagent(call)
                p["tool_name"] = tool
                with self.subTest(call=call, tool=tool):
                    self.denied(p, SANCTIONED)

    def test_uv_run_module_flag_naming_kill_owned_is_denied_in_every_spelling_and_shell(self):
        for call in UV_MODULE_RUNS:
            for tool in ("Bash", "PowerShell"):
                p = self.subagent(call)
                p["tool_name"] = tool
                with self.subTest(call=call, tool=tool):
                    self.denied(p, SANCTIONED)

    def test_a_command_that_names_the_helper_in_its_own_argv_or_redirects_it_into_python_is_denied(self):
        for tool, calls in (("Bash", STILL_DENIED), ("PowerShell", STILL_DENIED + POWERSHELL_ONLY_STILL_DENIED)):
            for call in calls:
                p = self.subagent(call)
                p["tool_name"] = tool
                with self.subTest(call=call, tool=tool):
                    self.denied(p, SANCTIONED)

    def test_an_expansion_operand_is_denied_only_in_a_command_that_names_the_helper(self):
        for tool in ("Bash", "PowerShell"):
            for call, expected in ((f"python $K {FORGED_ARGS}", None),
                                   (f"K=scripts/kill_owned.py; python $K {FORGED_ARGS}", SANCTIONED)):
                p = self.subagent(call)
                p["tool_name"] = tool
                with self.subTest(call=call, tool=tool):
                    if expected:
                        self.denied(p, expected)
                    else:
                        self.assertIsNone(pk.check(p))
                        self.assertIsNone(pk.updated_input(p))

    def test_routes_only_an_evader_would_use_are_not_read_and_the_helper_refuses_them_without_a_token(self):
        for tool, routes in (("Bash", UNREAD_ROUTES + BASH_ONLY_UNREAD_ROUTES), ("PowerShell", UNREAD_ROUTES)):
            for call in routes:
                p = self.subagent(call)
                p["tool_name"] = tool
                with self.subTest(call=call, tool=tool):
                    self.assertIn(FORGED_ARGS, call)
                    self.assertIsNone(pk.check(p))
                    self.assertIsNone(pk.updated_input(p))
        self.assertEqual(self.token_files(), [])
        self.assertIn("missing or malformed", ko.token_problem(ko.parse_args(FORGED_ARGS.split())))

    def test_in_bash_an_unquoted_backslash_escape_can_spell_the_helper_and_in_powershell_it_cannot(self):
        for call in BASH_ESCAPED:
            powershell = self.subagent(call)
            powershell["tool_name"] = "PowerShell"
            with self.subTest(call=call):
                self.denied(self.subagent(call), SANCTIONED)
                self.assertIsNone(pk.check(powershell))

    def test_a_powershell_line_continuation_before_the_helper_path_does_not_hide_it(self):
        for call in CONTINUED:
            p = self.subagent(call)
            p["tool_name"] = "PowerShell"
            with self.subTest(call=call):
                self.denied(p, SANCTIONED)

    def test_a_call_that_supplies_facts_and_is_not_launched_by_uv_is_denied_for_both_reasons(self):
        call = (f"D:/work/proj/.venv/Scripts/python.exe {SCRIPT} --worktree D:/other "
                "--since 1970-01-01T00:00:00Z --pid 1")
        message = pk.check(self.subagent(call))
        self.assertIn("the hook supplies --worktree and --since", message)
        self.assertIn(SANCTIONED, message)
        self.assertIn("the hook supplies --since", pk.check(self.session(call.replace("--worktree D:/other ", ""))))

    def test_a_main_session_call_not_launched_by_uv_is_denied(self):
        self.denied(self.session(f"python {SCRIPT} --worktree {WORKTREE} --pid 1"), SANCTIONED)

    def test_a_dispatch_without_a_worktree_path_is_denied(self):
        self.denied(self.subagent(f"{HELPER} --pid 1", transcript=self.agent_transcript("story_id: tq-59\ngo")),
                    "owns no worktree")

    def test_an_unreadable_transcript_is_denied(self):
        self.denied(self.subagent(f"{HELPER} --pid 1", transcript=str(self.tmp / "agent-a1.jsonl")), "[process-kill]")
        p = self.subagent(f"{HELPER} --pid 1")
        del p["transcript_path"]
        self.denied(p, "[process-kill]")

    def test_a_transcript_with_no_timestamp_is_denied(self):
        t = self.write("agent-a1.jsonl", [{"type": "user", "message": {"role": "user",
                                                                       "content": "worktree_path: " + WORKTREE}}])
        self.denied(self.subagent(f"{HELPER} --pid 1", transcript=t), "[process-kill]")

    def test_a_main_session_call_without_a_worktree_is_denied_naming_it(self):
        self.denied(self.session(f"{HELPER} --pid 1"), "--worktree")

    def test_a_main_session_call_with_a_since_is_denied(self):
        self.denied(self.session(f"{HELPER} --worktree {WORKTREE} --since 2026-01-01T00:00:00Z --pid 1"),
                    "the hook supplies")

    def test_a_main_session_with_an_unreadable_transcript_is_denied(self):
        p = self.session(f"{HELPER} --worktree {WORKTREE} --pid 1")
        p["transcript_path"] = str(self.tmp / "nope.jsonl")
        self.denied(p, "[process-kill]")

    def test_the_check_never_raises_on_a_malformed_payload(self):
        for p in ({"tool_name": "Bash", "tool_input": {"command": f"{HELPER} --pid 1"}, "agent_type": "x"},
                  {"tool_name": "Bash", "tool_input": {"command": f"{HELPER} --pid 1"}, "agent_type": "x",
                   "agent_id": 7, "transcript_path": 5}):
            with self.subTest(p=p):
                self.assertIn("[process-kill]", pk.check(p))



if __name__ == "__main__":
    unittest.main()
