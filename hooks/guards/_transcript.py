#!/usr/bin/env python3
"""Shared transcript helpers for the review guards (Claude Code transcripts).

A subagent's own transcript is `<session dir>/subagents/**/agent-<agent_id>.jsonl`; the hook payload carries
`transcript_path` (the session's, or the agent's own), `session_id` and `agent_id`. The first user message of the
agent's transcript is its dispatch prompt (Agent tool or Workflow agent() call). Tools without transcript files
(OpenCode) get None from every function here, and the guards that need a transcript fail open.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

WORKTREE_LINE = re.compile(r"^[ \t]*worktree_path:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
MAX_LINES = 2000


def transcript_file(payload: dict) -> str | None:
    tp = payload.get("transcript_path") or ""
    aid, sid = payload.get("agent_id"), payload.get("session_id")
    if not (tp and aid):
        return None
    if os.path.basename(tp) == f"agent-{aid}.jsonl" and os.path.isfile(tp):
        return tp
    if not sid:
        return None
    path = Path(tp)
    project = next((a.parent for a in path.parents if a.name == sid), path.parent)
    hits = glob.glob(os.path.join(project, sid, "subagents", "**", f"agent-{aid}.jsonl"), recursive=True)
    return hits[0] if hits else None


def message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def dispatch_prompt(transcript: str) -> str:
    """The first user message of a transcript, or ''."""
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh):
            if n >= MAX_LINES:
                break
            try:
                msg = json.loads(raw).get("message")
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("role") == "user":
                return message_text(msg.get("content"))
    return ""


def agent_worktree(payload: dict, tag: str) -> str | None:
    """The worktree a subagent's dispatch names (`worktree_path: <path>` line; forward slashes, no trailing
    slash), or None: the main session, no such line, or no transcript (said on stderr with `tag`)."""
    if not payload.get("agent_type"):
        return None
    transcript = transcript_file(payload)
    if not transcript:
        print(f"[{tag}] check skipped: the agent's transcript was not found", file=sys.stderr)
        return None
    match = WORKTREE_LINE.search(dispatch_prompt(transcript))
    return match.group(1).replace("\\", "/").rstrip("/") if match else None
