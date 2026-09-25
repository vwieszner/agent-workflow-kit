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
  4. Recursive delete (`Remove-Item ... -Recurse`, `rm -rf` variants) whose literal
     absolute target is not strictly inside an allowed root
     (`config: guards.recursive_delete_allowed`; default: the repo root, the worktrees
     root, the system temp dir). Variable-based paths (`$dir`) are trusted.
  5. Project rules (`config: guards.command_deny`): [{pattern, reason}].
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

HOOK = "powershell_command.py"

NATIVE_COMMAND_MARKERS = re.compile(
    r"(?:cmd\s+/c\b|\.bat\b|\.exe\b|&\s+['\"][^'\"]+\.(?:exe|bat))", re.IGNORECASE,
)
HAS_2_AND_1 = re.compile(r"\b2>&1\b")

# `\b` before `-` needs a word boundary that does not exist between two non-word
# chars, so -Recurse is matched with a lookbehind on whitespace.
RE_REMOVE_ITEM = re.compile(r"\bRemove-Item\b", re.IGNORECASE)
RE_DASH_RECURSE = re.compile(r"(?<=\s)-Recurse\b", re.IGNORECASE)
RM_RF = re.compile(
    r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*[fF][a-zA-Z]*|-[a-zA-Z]*[fF][a-zA-Z]*[rR][a-zA-Z]*"
    r"|-[rR]\s+-[fF]|-[fF]\s+-[rR])\b",
    re.IGNORECASE,
)
# Literal absolute paths: drive-anchored Windows paths, or POSIX paths starting with /.
LITERAL_PATH = re.compile(r"(?:(?<=\s)|(?<=^)|(?<=[\"']))([A-Za-z]:\\[^\s\"';|&]+|/[^\s\"';|&]+)")


def _norm(p: str) -> str:
    return p.strip("\"'").replace("\\", "/").rstrip("/").lower() + "/"


def allowed_roots() -> list[str]:
    roots = g.cfg_list("guards.recursive_delete_allowed")
    if not roots:
        roots = [str(g.wfconfig.repo_root()), str(g.wfconfig.worktrees_root()),
                 tempfile.gettempdir()]
    else:
        roots = [str(g.wfconfig.render(str(r))) for r in roots]
    return [_norm(r) for r in roots]


def path_is_under_allowlist(path: str, roots: list[str]) -> bool:
    """Strictly under a root: the root itself is NOT allowed."""
    norm = _norm(path)
    return any(norm.startswith(r) and len(norm) > len(r) for r in roots)


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
    violations += g.host_forbidden_violations(cmd, strip_call_operator=True)
    violations += g.host_install_violations(cmd)

    roots = None
    for raw in g.SEGMENT_SPLIT.split(cmd):
        seg = raw.strip()
        if not seg or g.is_container_segment(seg):
            continue
        if (RE_REMOVE_ITEM.search(seg) and RE_DASH_RECURSE.search(seg)) or RM_RF.search(seg):
            roots = roots if roots is not None else allowed_roots()
            for p in LITERAL_PATH.findall(seg):
                if not path_is_under_allowlist(p, roots):
                    violations.append(
                        f"[recursive-delete] recursive delete of '{p}' outside the allowed "
                        f"roots (config guards.recursive_delete_allowed): "
                        f"{', '.join(r.rstrip('/') for r in roots)}. If intentional, the "
                        f"owner runs it from their own shell."
                    )
                    break

    violations += g.command_deny_violations(cmd)

    if violations:
        return g.block("PowerShell command violates one or more discipline rules.",
                       violations, HOOK, "Rewrite the command and try again.")
    return 0


if __name__ == "__main__":
    g.run("powershell_command", evaluate)
