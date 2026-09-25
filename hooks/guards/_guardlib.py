#!/usr/bin/env python3
"""Shared plumbing for the guard hooks in .workflow/hooks/guards/.

Every guard speaks the Claude Code hook protocol (DESIGN §7):
  * JSON payload on stdin (`tool_name`, `tool_input`, `agent_type`, `cwd`, ...).
  * Deny a built-in tool: exit 2 with the reason on stderr  -> `block()`.
  * Deny an MCP tool: JSON permissionDecision on stdout, exit 0 -> `deny_json()`
    (exit 2 does not block MCP tools).
  * Fail OPEN on internal error (`run()` turns any exception into exit 0);
    fail CLOSED on a matched rule.

Each guard is switched by `config: guards.<name>` (default true). `run()` checks it.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import wfconfig  # noqa: E402


# ------------------------------------------------------------------ payload / config

def read_payload() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.buffer.read().lstrip(b"\xef\xbb\xbf")  # strip UTF-8 BOM(s)
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def cfg(key: str, default: Any) -> Any:
    try:
        return wfconfig.get(key, default)
    except Exception:
        return default


def enabled(guard: str) -> bool:
    return bool(cfg(f"guards.{guard}", True))


def cfg_list(key: str, default: list | None = None) -> list:
    v = cfg(key, default if default is not None else [])
    return list(v) if isinstance(v, (list, tuple)) else []


# ------------------------------------------------------------------ decisions

def block(header: str, violations: list[str], hook: str, footer: str = "") -> int:
    sys.stderr.write(f"[workflow-hook] {header}\n\nDetected violations:\n")
    for v in violations:
        sys.stderr.write(f"  - {v}\n")
    if footer:
        sys.stderr.write(f"\n{footer}\n")
    sys.stderr.write(f"(Hook: .workflow/hooks/guards/{hook})\n")
    return 2


def deny_json(reason: str) -> int:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


def run(guard: str, fn: Callable[[dict[str, Any]], int]) -> None:
    """Entry point: config switch, payload parse, fail-open wrapper."""
    try:
        if not enabled(guard):
            sys.exit(0)
        payload = read_payload()
        if payload is None:
            sys.exit(0)
        code = fn(payload)
    except SystemExit:
        raise
    except Exception:
        code = 0  # a crashing guard must never brick the session
    sys.exit(code)


# ------------------------------------------------------------------ shell parsing

SEGMENT_SPLIT = re.compile(r"(?:;|&&|\|\||\n)")
# Separators for "first token" checks — NOT newlines, so a heredoc body line fed to a
# container is never mistaken for a host command.
SEGMENT_SPLIT_NO_NL = re.compile(r"(?:\|\||&&|[;|&])")

# docker compose exec / docker exec — tolerates compose flags between `compose` and
# `exec` (e.g. `-p <stack>`, `-f file.yml`, `--project-name X`, `--env-file .env`).
DOCKER_EXEC_COMPOSE = re.compile(
    r"\bdocker(?:[- ]compose)?(?:\s+(?:-[a-zA-Z]\S*|--[a-zA-Z][\w-]*(?:=\S+)?)\s+\S+)*\s+exec\b",
    re.IGNORECASE,
)
DOCKER_EXEC_SIMPLE = re.compile(r"\bdocker\s+exec\b", re.IGNORECASE)


def is_container_segment(seg: str) -> bool:
    return bool(DOCKER_EXEC_COMPOSE.search(seg) or DOCKER_EXEC_SIMPLE.search(seg))


def unquoted_residue(cmd: str) -> str:
    """Blank out single- and double-quoted spans (length-preserving) so only
    shell-UNQUOTED text remains."""
    out: list[str] = []
    state: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if state is None:
            if c in ("'", '"'):
                state = c
                out.append(" ")
            else:
                out.append(c)
        elif state == "'":
            if c == "'":
                state = None
            out.append(" ")
        else:
            if c == "\\" and i + 1 < n and cmd[i + 1] in ('"', "\\", "$", "`"):
                out.append("  ")
                i += 2
                continue
            if c == '"':
                state = None
            out.append(" ")
        i += 1
    return "".join(out)


# ------------------------------------------------------------------ shared command rules

PIP_INSTALL_PATTERNS = [
    r"(?:^|\s)pip3?(?:\.exe)?\s+install\b",
    r"(?:^|\s)python3?(?:\.exe)?\s+-m\s+pip\s+install\b",
    r"\.venv[\\/](?:Scripts|bin)[\\/]python3?(?:\.exe)?\s+-m\s+pip\s+install\b",
    r"\.venv[\\/](?:Scripts|bin)[\\/]pip3?(?:\.exe)?\s+install\b",
    r"(?:^|\s)uv\s+pip\s+install\b",
    r"(?:^|\s)uv\s+sync\b",
    r"\.venv[\\/](?:Scripts|bin)[\\/]uv(?:\.exe)?\s+(?:pip\s+install|sync)\b",
]


def host_install_violations(cmd: str) -> list[str]:
    """`config: guards.block_host_installs` — package installs on the host are
    forbidden; installs inside a container (docker exec segments) are fine."""
    if not cfg("guards.block_host_installs", True):
        return []
    out: list[str] = []
    for raw in SEGMENT_SPLIT.split(cmd):
        seg = raw.strip()
        if not seg or is_container_segment(seg):
            continue
        m = re.search(r"(?:^|\s)uv\s+(add|remove)\b", seg, re.IGNORECASE)
        if m and not re.search(r"--no-sync\b", seg):
            out.append(
                f"[host-uv-no-sync] uv {m.group(1)} on host without --no-sync: '{seg[:120]}'. "
                f"It would install into the host environment. Use 'uv {m.group(1)} --no-sync "
                f"<pkg>' (edits the manifest + lockfile only), then rebuild the container image."
            )
            continue
        for pattern in PIP_INSTALL_PATTERNS:
            if re.search(pattern, seg, re.IGNORECASE):
                out.append(
                    f"[host-pip] package install on the host: '{seg[:120]}'. Forbidden — it "
                    "pollutes the host environment. Edit the manifest without installing "
                    "(e.g. 'uv add --no-sync <pkg>'), then rebuild the container image; or "
                    "install inside the container via 'docker compose exec'."
                )
                break
    return out


def host_forbidden_violations(cmd: str, strip_call_operator: bool = False) -> list[str]:
    """`config: guards.host_forbidden` — regexes matched against the start of every
    host command segment (container segments skipped). First match reported."""
    patterns = cfg_list("guards.host_forbidden")
    if not patterns:
        return []
    hint = str(cfg("guards.host_forbidden_hint", "") or "")
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(str(p), re.IGNORECASE))
        except re.error:
            continue
    for seg in SEGMENT_SPLIT_NO_NL.split(unquoted_residue(cmd)):
        s = seg.strip()
        if not s or is_container_segment(s):
            continue
        s = re.sub(r"^sudo\s+", "", s)
        if strip_call_operator:
            s = re.sub(r"^&\s+", "", s)
        for rx in compiled:
            if rx.search(s):
                msg = (f"[host-forbidden] '{s[:120]}' matches config guards.host_forbidden "
                       f"('{rx.pattern}') — this command must not run on the host.")
                if hint:
                    msg += f" {hint}"
                return [msg]
    return []


def command_deny_violations(cmd: str) -> list[str]:
    """`config: guards.command_deny` — project rules: [{pattern, reason}, ...]."""
    out: list[str] = []
    for entry in cfg_list("guards.command_deny"):
        if not isinstance(entry, dict) or not entry.get("pattern"):
            continue
        try:
            if re.search(str(entry["pattern"]), cmd, re.IGNORECASE | re.MULTILINE):
                out.append(f"[command-deny] {entry.get('reason') or entry['pattern']}")
        except re.error:
            continue
    return out
