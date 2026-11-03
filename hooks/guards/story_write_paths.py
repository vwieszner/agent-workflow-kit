#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(Write|Edit|NotebookEdit|Bash|PowerShell) guard — a story agent writes into its own worktree.
Switch: `config: guards.story_write_paths`.

A subagent whose dispatch prompt (the first user message of its own transcript) holds a `worktree_path: <path>`
line (at column 0, or indented by a workflow harness) owns that worktree; its story files go there, never into the
main checkout (the repo root). Paths are compared by component after normalisation (`\\` and `/d/...` forms,
case on case-insensitive hosts, `..`), so a sibling worktrees root is not inside the main checkout. Denies:
  * `check`: a Write, Edit or NotebookEdit whose path lies in the main checkout;
  * `shell_check`: a Bash / PowerShell command whose write-shaped target lies in the main checkout: a `>` / `>>`
    redirect, `tee`, the last operand of `cp` / `mv`, the path of `Out-File` / `Set-Content` / `Add-Content` /
    `New-Item`, the `-Destination` of `Copy-Item` / `Move-Item`. A relative target counts when the command's own
    `cd` (or `Set-Location`) goes into the main checkout. Heredoc and here-string bodies are not scanned.
Shared files the pipeline edits on the base branch pass: `config: guards.main_checkout_shared_paths`.

Not checked (mistake-proofing, not a sandbox): the main session; a subagent whose dispatch names no worktree; what
a script writes by itself; a target held in a variable; a relative target with no `cd`; OpenCode (no subagent
transcript to read). Fails open with one stderr line. Deny: hookSpecificOutput JSON, exit 0.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
from _transcript import dispatch_prompt, transcript_file  # noqa: E402

WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "NotebookEdit": "notebook_path"}
WORKTREE_LINE = re.compile(r"^[ \t]*worktree_path:[ \t]*(\S+)[ \t]*$", re.MULTILINE)

_PATH = r"""(?:"[^"]+"|'[^']+'|[^\s'";|&()<>]+)"""
SHELL_TARGETS = (
    re.compile(r"(?<![<>&\d])\d?>{1,2}(?!&)\s*(" + _PATH + ")"),
    re.compile(r"\btee\s+(?:-a\s+|--append\s+)?(" + _PATH + ")"),
    re.compile(r"\b(?:cp|mv)\s+(?:-{1,2}[\w-]+\s+)*(?:" + _PATH + r"\s+)+?(" + _PATH + r")\s*(?=$|[;&|)])"),
    re.compile(r"\b(?:Out-File|Set-Content|Add-Content|New-Item)\b(?:\s+-(?:Literal)?(?:File)?Path)?\s+(" + _PATH + ")",
               re.IGNORECASE),
    re.compile(r"\b(?:Copy-Item|Move-Item)\b[^;|]*?\s-Destination\s+(" + _PATH + ")", re.IGNORECASE),
)
CD = re.compile(r"\s*\(?\s*(?:cd|Set-Location|pushd)\s+(?:-Path\s+)?(" + _PATH + ")", re.IGNORECASE)
# A heredoc or here-string body is data on stdin, not shell text: its `>` is no redirect.
BODIES = (re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?^[ \t]*\2[ \t]*$", re.DOTALL | re.MULTILINE),
          re.compile(r"@(['\"])[ \t]*\r?\n.*?^\1@", re.DOTALL | re.MULTILINE))
# Inside a quoted string `>`, `|`, `;` and `&` are text (`grep ">"`, an awk program), so they are masked first.
QUOTED = re.compile(r"""'[^']*'|"(?:[^"\\]|\\.)*\"""")


def agent_worktree(payload: dict, tag: str) -> str | None:
    """The worktree a subagent's dispatch names (forward slashes, no trailing slash), or None: the main session, no
    `worktree_path:` line, or no transcript (the last says so on stderr, prefixed with `tag`)."""
    if not payload.get("agent_type"):
        return None
    transcript = transcript_file(payload)
    if not transcript:
        print(f"[{tag}] check skipped: the agent's transcript was not found", file=sys.stderr)
        return None
    match = WORKTREE_LINE.search(dispatch_prompt(transcript))
    return match.group(1).replace("\\", "/").rstrip("/") if match else None


def normal(path: str, base: str | None = None) -> str | None:
    """`path` as an absolute forward-slash path with `..` collapsed (lower-cased on Windows-style drive paths and
    case-insensitive hosts); a relative one is joined to `base`. None when it is relative and there is no base, or
    is a `~` / `$VAR` / `container:/path` form."""
    p = path.strip().strip("'\"").replace("\\", "/")
    drive = re.match(r"^/([a-zA-Z])(/.*)?$", p)
    if drive and os.name == "nt":
        p = f"{drive.group(1)}:{drive.group(2) or '/'}"
    if re.match(r"^[a-zA-Z]:/", p):
        head, rest = p[:2], p[2:]
        return (head + posixpath.normpath(rest)).lower()
    if p.startswith("/"):
        out = posixpath.normpath(p)
        return out.lower() if _CASELESS else out
    if base is None or p.startswith(("~", "$")) or ":" in p:
        return None
    return normal(base.rstrip("/") + "/" + p)


_CASELESS = os.name == "nt" or sys.platform == "darwin"


def main_checkout() -> str:
    return normal(str(g.wfconfig.repo_root())) or ""


def in_main(path: str | None) -> bool:
    """`path` (from `normal`) is the main checkout or under it, and is not a shared file
    (`config: guards.main_checkout_shared_paths`)."""
    main = main_checkout()
    if path is None or not main:
        return False
    if path != main and not path.startswith(main + "/"):
        return False
    shared = {str(s).replace("\\", "/").strip("/") for s in g.cfg_list("guards.main_checkout_shared_paths", [])}
    rel = path[len(main) + 1:]
    return rel not in shared and rel not in {s.lower() for s in shared}


def reason(target: str, worktree: str) -> str:
    return (f"[story-write-paths] {target!r} is in the main checkout: a story's files go into its own worktree "
            f"{worktree}, never the main checkout. Write it under {worktree}/ (scratch files under "
            f"the system temp dir).")


def check(payload: dict) -> str | None:
    """The deny reason for a Write, Edit or NotebookEdit call, or None."""
    key = WRITE_TOOLS.get(str(payload.get("tool_name") or ""))
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if not key or not isinstance(args.get(key), str) or not in_main(normal(args[key])):
        return None
    worktree = agent_worktree(payload, "story-write-paths")
    return reason(args[key], worktree) if worktree else None


def shell_targets(command: str) -> list[str]:
    """Every write-shaped target of `command`, as `normal` paths; a relative one only after an in-command `cd`, which
    holds for the segments after it."""
    for body in BODIES:
        command = body.sub(" ", command)
    command = QUOTED.sub(lambda m: re.sub(r"[<>|;&]", "_", m.group(0)), command)
    found = []
    base = None
    for segment in re.split(r"&&|\|\||;|\n", command):
        cd = CD.match(segment)
        if cd:
            base = normal(cd.group(1), base)
        for rx in SHELL_TARGETS:
            for raw in rx.findall(segment):
                if raw.strip("'\"") in ("/dev/null", "$null", "NUL", "nul"):
                    continue
                target = normal(raw, base)
                if target:
                    found.append(target)
    return found


def shell_check(payload: dict) -> str | None:
    """The deny reason for a Bash or PowerShell command that writes into the main checkout, or None."""
    if str(payload.get("tool_name") or "") not in ("Bash", "PowerShell"):
        return None
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    command = args.get("command")
    if not isinstance(command, str):
        return None
    hits = [t for t in shell_targets(command) if in_main(t)]
    if not hits:
        return None
    worktree = agent_worktree(payload, "story-write-paths")
    return reason(hits[0], worktree) if worktree else None


def main() -> int:
    if not g.enabled("story_write_paths"):
        return 0
    try:
        payload = g.read_payload()
        if payload is None:
            raise ValueError("payload unreadable")
        message = check(payload) or shell_check(payload)
    except Exception as exc:  # fail open: a crashing hook must not block an edit
        print(f"[story-write-paths] check skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if message:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": message}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
