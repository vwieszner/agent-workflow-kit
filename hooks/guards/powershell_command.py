#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard on the PowerShell tool (Claude Code on Windows).
Switch: `config: guards.powershell_command`.

Rules (exit 2 + stderr on any match):
  1. `2>&1` together with a native command (cmd /c, .bat, .exe, & "...exe").
     PowerShell 5.1 wraps native stderr in NativeCommandError and kills the wrapping
     script the moment the native command writes to stderr. The tool captures stderr
     already — drop the `2>&1`.
  2. Host-forbidden commands (`config: guards.host_forbidden`), after stripping a
     leading `&` call operator. Container segments exempt.
  3. Host package installs (`config: guards.block_host_installs`) — same rules as the
     Bash guard.
  4. Recursive delete (`Remove-Item`/`ri`/`rm`/`rd`/`del` with `-Recurse`, incl.
     `-Path:`/`-LiteralPath:` forms, and `rm -r` variants) whose target is not strictly
     inside an allowed root (`config: guards.recursive_delete_allowed`; default: the repo
     root, the worktrees root, `claude*`/`workflow*` dirs of the system temp dir). Drive
     roots, `/`, `~`/`$HOME`/`$env:USERPROFILE` and `..` escapes are caught; other
     variable-based paths (`$dir`) are trusted.
  5. git push / commit rules and destructive commands — same as the Bash guard.
  6. Bare host `python` — same as the Bash guard.
  7. Project rules (`config: guards.command_deny`): [{pattern, reason}].
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

HOOK = "powershell_command.py"

NATIVE_COMMAND_MARKERS = re.compile(
    r"(?:cmd\s+/c\b|\.bat\b|\.exe\b|&\s+['\"][^'\"]+\.(?:exe|bat))", re.IGNORECASE,
)
HAS_2_AND_1 = re.compile(r"\b2>&1\b")


def evaluate(payload: dict) -> int:
    if payload.get("tool_name") != "PowerShell":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str) or not cmd:
        return 0

    violations: list[str] = []
    if HAS_2_AND_1.search(cmd) and NATIVE_COMMAND_MARKERS.search(cmd):
        violations.append(
            "[ps-2>&1] '2>&1' paired with a native command (cmd /c, .bat, .exe). PowerShell "
            "5.1 wraps native stderr in NativeCommandError and kills the wrapping script as "
            "soon as the native command writes to stderr. Drop the '2>&1' — the PowerShell "
            "tool captures stderr already."
        )
    violations += g.git_violations(cmd)
    violations += g.destructive_violations(cmd)
    violations += g.host_python_violations(cmd, strip_call_operator=True)
    violations += g.host_forbidden_violations(cmd, strip_call_operator=True)
    violations += g.host_install_violations(cmd)
    violations += g.recursive_delete_violations(cmd, str(payload.get("cwd") or os.getcwd()),
                                                powershell=True)
    violations += g.command_deny_violations(cmd)

    if violations:
        return g.block("PowerShell command violates one or more discipline rules.",
                       violations, HOOK, "Rewrite the command and try again.")
    return 0


if __name__ == "__main__":
    g.run("powershell_command", evaluate)
