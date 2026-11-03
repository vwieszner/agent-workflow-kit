"""Shared helpers for guards that read a subagent's own transcript (Claude Code).

`transcript_file(payload)` — the subagent's transcript: the payload's `transcript_path` when it is
`agent-<agent_id>.jsonl`, else `<project>/<session_id>/subagents/**/agent-<agent_id>.jsonl`. None for the main
session (no `agent_id`) or when nothing is found. OpenCode payloads carry no transcript: callers treat None as
"cannot check" and say so.

`dispatch_prompt(path)` — the text of the transcript's first user record (the dispatch prompt).
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

MAX_TRANSCRIPT_LINES = 2000


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
    hits = glob.glob(os.path.join(glob.escape(str(project)), sid, "subagents", "**", f"agent-{aid}.jsonl"),
                     recursive=True)
    return hits[0] if hits else None


def dispatch_prompt(path: str) -> str:
    """The text of the transcript's first user record."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            if n >= MAX_TRANSCRIPT_LINES:
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(b.get("text", "") for b in content
                                 if isinstance(b, dict) and isinstance(b.get("text"), str))
            return ""
    return ""
