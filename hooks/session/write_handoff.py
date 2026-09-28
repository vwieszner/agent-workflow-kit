#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Pre-compaction hook: have a headless agent write `.workflow/state/handoff.md`.

Usage (hook payload JSON on stdin):
    write_handoff.py --tool claude|opencode

Claude Code: wired as PreCompact (matcher "auto"). OpenCode: called by the plugin from
`experimental.session.compacting`, with `transcript_path` pointing at a text export of the
session's recent messages (OpenCode keeps no transcript file).

Runs the headless command SYNCHRONOUSLY (so the handoff exists before the post-compaction
SessionStart injector reads it), bounded by `config: session.handoff_timeout_s`.
Command: `config: session.handoff_cmd` (a template; the token `{prompt}` is replaced by the
prompt as ONE argument), else the per-tool default:
    claude   -> claude -p {prompt}
    opencode -> opencode run {prompt}
Switch: `config: session.handoff_on_compact` (default true).

The child runs with WORKFLOW_HEADLESS=1; this script no-ops when that is already set, so a
headless run never recursively spawns another, and session_journal.py no-ops under it, so the
child's own turns are never journaled. Also a no-op when WORKFLOW_RETRO_RUNNING is set (inside
the retrospective's headless analyst).
Always exits 0 — compaction is never blocked or failed by this hook.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

HANDOFF_PROMPT_PREFIX = "Write current session state to "
DEFAULT_CMDS = {
    "claude": "claude -p {prompt}",
    "opencode": "opencode run {prompt}",
}


def build_prompt(handoff: Path, transcript_path: str | None) -> str:
    source = ""
    if transcript_path and Path(transcript_path).is_file():
        source = (f" Reconstruct the state from the session transcript at {transcript_path} "
                  f"(read its most recent part) and from `git status` / `git diff --stat`.")
    else:
        source = " Reconstruct the state from `git status` / `git diff --stat` and recent commits."
    return (
        f"{HANDOFF_PROMPT_PREFIX}{handoff} (create the directory if needed). Include: file "
        f"currently being edited, what change is in progress, last 2-3 decisions made, intended "
        f"next step. Be concise, single page max.{source} Write ONLY that file."
    )


def build_argv(template: str, prompt: str) -> list[str]:
    tokens = shlex.split(template, posix=(os.name != "nt"))
    argv = [prompt if t == "{prompt}" else t for t in tokens]
    if "{prompt}" not in tokens:
        argv.append(prompt)
    return argv


def main(argv: list[str]) -> int:
    if os.environ.get("WORKFLOW_HEADLESS") or os.environ.get("WORKFLOW_RETRO_RUNNING"):
        return 0
    import wfconfig

    if not wfconfig.get("session.handoff_on_compact", True):
        return 0
    tool = "claude"
    if "--tool" in argv:
        i = argv.index("--tool")
        if i + 1 < len(argv):
            tool = argv[i + 1]
    try:
        payload = json.loads(sys.stdin.buffer.read().lstrip(b"\xef\xbb\xbf").decode("utf-8") or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    handoff = wfconfig.state_dir() / "handoff.md"
    prompt = build_prompt(handoff, payload.get("transcript_path"))
    template = str(wfconfig.get("session.handoff_cmd", "") or DEFAULT_CMDS.get(tool, DEFAULT_CMDS["claude"]))
    cmd = build_argv(template, prompt)
    timeout = int(wfconfig.get("session.handoff_timeout_s", 180))
    env = {**os.environ, "WORKFLOW_HEADLESS": "1"}
    subprocess.run(cmd, cwd=str(wfconfig.repo_root()), env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(0)
