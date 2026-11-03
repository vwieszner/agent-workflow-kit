#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(StructuredOutput) guard: a review agent submits its report only after reading every diff line.

Scope: `agent_type` in `config: guards.review_coverage_agents` (default the review layers and execution stages),
the agents whose report the review workflow accepts only with `diff_lines_read` covering their diff. Their
report is the StructuredOutput call, so this is the last point at which unread lines can still be read.

The assigned diff files are the absolute `.../diff-<k>.patch` paths in the agent's dispatch prompt (the first
user message of its own transcript). A line counts as read when a Read call on that file whose range covers it
returned without `is_error`. While any line is unread, the call is denied with the missing ranges as Read calls
sized by `review_diff_chunks.page_plan`. After CAP denials of one agent the call is allowed, so a stuck agent
cannot loop; the workflow's `diff_lines_read` check still applies to that report.

Fails open: an unknown agent type, no transcript (OpenCode, or a missing file), a diff file missing on disk, or
any error. Every run for a covered agent appends one line to `.workflow/state/review-coverage.jsonl` (allow /
deny / cap / no-transcript / error), and a StructuredOutput call whose payload names no `agent_type` logs
`no-agent-type`, so "never fired" is distinguishable from "fired and allowed". Switch:
`config: guards.review_coverage`. Deny: hookSpecificOutput JSON, exit 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _guardlib as g  # noqa: E402
from _transcript import message_text, transcript_file  # noqa: E402
from review_diff_chunks import page_plan  # noqa: E402  (scripts/ is on sys.path via _guardlib)

HOOK = "review_coverage.py"
DEFAULT_AGENTS = ["blind-hunter", "edge-case-hunter", "verification-gap", "acceptance-auditor", "ui-verifier",
                  "pbt-writer", "test-adequacy"]
CAP = 3
DEFAULT_LIMIT = 2000
DIFF_PATH = re.compile(r"(?:[A-Za-z]:[/\\]|/)[^\s,;()'\"]*?diff-\d+\.patch")


def norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def scan(transcript: str) -> tuple[str, list[tuple[str, int, int]]]:
    """(dispatch prompt, successful Read calls as (normalized path, offset, limit))."""
    prompt: str | None = None
    uses: dict[str, dict] = {}
    reads: list[tuple[str, int, int]] = []
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                msg = json.loads(raw).get("message")
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if prompt is None and msg.get("role") == "user":
                prompt = message_text(content)
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "Read":
                    uses[b.get("id")] = b.get("input") or {}
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                    inp = uses.pop(b["tool_use_id"])
                    if not b.get("is_error"):
                        reads.append((norm(str(inp.get("file_path") or "")),
                                      max(int(inp.get("offset") or 1), 1), int(inp.get("limit") or DEFAULT_LIMIT)))
    return prompt or "", reads


def unread_ranges(total: int, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Inclusive line ranges of 1..total that no (offset, limit) span covers."""
    gaps, nxt = [], 1
    for start, limit in sorted(spans):
        if start > nxt:
            gaps.append((nxt, min(start - 1, total)))
        nxt = max(nxt, start + limit)
        if nxt > total:
            break
    if nxt <= total:
        gaps.append((nxt, total))
    return [(a, b) for a, b in gaps if a <= b]


def unread(prompt: str, reads: list[tuple[str, int, int]]) -> dict[str, list[list[int]]]:
    """Per assigned diff file on disk: the Read calls (offset, limit) that would cover its unread lines."""
    out: dict[str, list[list[int]]] = {}
    for path in dict.fromkeys(DIFF_PATH.findall(prompt)):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            lines = fh.readlines()
        key = norm(path)
        gaps = unread_ranges(len(lines), [(o, n) for p, o, n in reads if p == key])
        calls = [[a + off - 1, lim] for a, b in gaps for off, lim in page_plan(lines[a - 1:b])]
        if calls:
            out[path] = calls
    return out


def reason(missing: dict[str, list[list[int]]]) -> str:
    rows = "\n".join(f"- {path}: " + ", ".join(f"offset {o} limit {n}" for o, n in calls)
                     for path, calls in missing.items())
    return (f"[workflow-hook] Report refused: these diff lines are unread (.workflow/hooks/guards/{HOOK}). Make "
            "exactly these Read calls, review what they show, then submit the report again with diff_lines_read "
            "counting every line read:\n" + rows)


def denials_so_far(agent_id: str) -> int:
    p = os.path.join(tempfile.gettempdir(), "workflow-review-coverage", f"{agent_id}.count")
    try:
        with open(p, encoding="utf-8") as fh:
            n = int(fh.read().strip())
    except (OSError, ValueError):
        n = 0
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(str(n + 1))
    return n


def log(entry: dict) -> None:
    try:
        with open(g.wfconfig.state_dir() / "review-coverage.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def evaluate(payload: dict) -> int:
    if payload.get("tool_name") != "StructuredOutput":
        return 0
    entry: dict = {"t": time.strftime("%Y-%m-%dT%H:%M:%S")}
    agent_type = payload.get("agent_type")
    if not agent_type:
        log(entry | {"event": "no-agent-type", "agent_id": payload.get("agent_id")})
        return 0
    if agent_type not in set(g.cfg_list("guards.review_coverage_agents", DEFAULT_AGENTS)):
        return 0
    entry |= {"agent_type": agent_type, "agent_id": payload.get("agent_id")}
    try:
        path = transcript_file(payload)
        if not path:
            log(entry | {"event": "no-transcript"})
            return 0
        missing = unread(*scan(path))
        summary = {os.path.basename(p): len(c) for p, c in missing.items()}
        if not missing:
            log(entry | {"event": "allow"})
            return 0
        if denials_so_far(str(payload.get("agent_id") or agent_type)) >= CAP:
            log(entry | {"event": "cap", "unread_pages": summary})
            return 0
        log(entry | {"event": "deny", "unread_pages": summary})
        return g.deny_json(reason(missing))
    except Exception as exc:  # fail open, but leave a trace
        log(entry | {"event": "error", "error": f"{type(exc).__name__}: {exc}"[:300]})
        return 0


if __name__ == "__main__":
    g.run("review_coverage", evaluate)
