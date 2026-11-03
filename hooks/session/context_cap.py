#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PostToolUse hook (agent-scoped, Claude Code) — context cap.

Wiring: a `PostToolUse` hook in the frontmatter of the agents that hand off at a context cap
(story-impl and its effort variants with `--role impl`, story-diagnose with `--role diagnose`).
OpenCode: not wired (no per-agent hooks, no transcript usage lines).

Once the agent's context reaches `config: context_cap.tokens` (default 300000), every later tool
result carries an `additionalContext` reminder: start no new work, hand off per the agent's
"Hand-off at the context cap" section, return the hand-off report. Soft cap — nothing is denied;
the tool's auto-compaction stays the backstop.

CONTEXT
-------
The latest request in the agent's own transcript: the largest executor iteration's
input + cache read + cache write (the `ctx` rule of `session_cost_report.py`). A request's
top-level usage sums its executor iterations — an advisor call splits one request into two —
so summing the top level overstates context; `advisor_message` iterations are excluded.
A streaming-partial line (no `iterations`) is one iteration: its top level is used.

TRANSCRIPT
----------
`transcript_path` when its basename is `agent-<agent_id>.jsonl`, else found by agent id under
`<project dir>/<session_id>/subagents/**` (a parent agent's path is never measured). The read grows
back from EOF until a line with `message.usage` parses (one line can exceed the first window).

Fails open (no output), but appends to `.workflow/state/context-cap.jsonl` every fire and every
run that found no transcript or no usage, so "never fired" is distinguishable from "never measured".

Usage (agent frontmatter, PostToolUse):
    uv run --no-project "$CLAUDE_PROJECT_DIR/.workflow/hooks/session/context_cap.py" --role impl|diagnose
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import wfconfig  # noqa: E402

CAP_TOKENS = int(wfconfig.get("context_cap.tokens", 300_000))
TAIL_START = 256 * 1024
STATE = wfconfig.workflow_dir() / "state"
CTX_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

MESSAGES = {
    "impl": ("Context cap reached: your context is {k}k tokens (cap {cap}k). Start no new step or edit. "
             "Let a running command finish or stop it, then follow your \"Hand-off at the "
             "context cap\" section: write the hand-off file and return the hand-off report."),
    "diagnose": ("Context cap reached: your context is {k}k tokens (cap {cap}k). Start no new experiment. "
                 "Let a running command finish or stop it, then follow your \"Hand-off at the "
                 "context cap\" section: return the hand-off report with the failures you finished and the "
                 "rest under `remaining:`."),
}


def context_tokens(usage: dict) -> int:
    parts = [i for i in usage.get("iterations") or [] if i.get("type") != "advisor_message"] or [usage]
    return max(sum(int(p.get(k) or 0) for k in CTX_FIELDS) for p in parts)


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


def last_context(path: str) -> int | None:
    size = os.path.getsize(path)
    window = TAIL_START
    with open(path, "rb") as fh:
        while True:
            start = max(0, size - window)
            fh.seek(start)
            lines = fh.read().split(b"\n")
            if start:
                lines = lines[1:]
            for raw in reversed(lines):
                if b'"usage"' not in raw:
                    continue
                try:
                    msg = json.loads(raw).get("message")
                except ValueError:
                    continue
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if usage:
                    return context_tokens(usage)
            if not start:
                return None
            window *= 4


def log(entry: dict) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "context-cap.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--role", choices=sorted(MESSAGES), required=True)
    role = ap.parse_args(argv).role
    entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "role": role}
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        log(entry | {"event": "unreadable-payload"})
        return 0
    entry["agent_id"] = payload.get("agent_id")
    path = transcript_file(payload)
    try:
        ctx = last_context(path) if path else None
    except OSError:
        ctx = None
    if ctx is None:
        log(entry | {"event": "no-usage" if path else "no-transcript",
                     "transcript_path": payload.get("transcript_path")})
        return 0
    if ctx < CAP_TOKENS:
        return 0
    log(entry | {"event": "cap", "context": ctx})
    text = MESSAGES[role].format(k=ctx // 1000, cap=CAP_TOKENS // 1000)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
