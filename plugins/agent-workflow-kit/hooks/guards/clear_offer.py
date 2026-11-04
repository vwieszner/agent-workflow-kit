#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(Agent|Skill|Bash|PowerShell) guard — offer a context clear once after a heavy
human checkpoint. Switch: `config: guards.clear_offer`.

parallel-dev-wave "Fresh session at a heavy human checkpoint": at Checkpoints 1, 2 and 3, once
the decision is on disk and the main session's context is at least
`config: guards.clear_offer_context_tokens` tokens, the orchestrator ends its message with
`State saved — /clear, then /parallel-dev-wave <story-id>` before dispatching the next phase.
This guard holds that dispatch ONCE until the offer was made and a user turn answered it.

Holds a dispatch only when ALL hold:
  * it is Agent(story-impl / story-finalize, effort variants included), Skill(land-story)
    without `--cleanup-only`, or a shell command running `land_lease.py acquire <id>` (the start
    of the land window), with a story id. A `land-story <id>` dispatch is not held while <id>
    holds the land lease (the hold happened at `acquire`); an unreadable lease fails open;
  * this session's transcript records a checkpoint for that story: a
    `story_record.py append <id> "checkpoint-..."` call, or a `## Triage decisions` section
    WRITTEN to the story's `code-review-findings` file (Edit/Write/MultiEdit, or a shell
    redirect / Add-Content / Set-Content / Out-File);
  * the tool call that wrote the latest such checkpoint does not contain `(auto-dev-loop`;
  * no assistant text naming `State saved — /clear` and the story, followed by a user turn,
    comes after that checkpoint;
  * the current context (the last assistant request's input + cache-read + cache-write
    tokens) is at least the threshold.

Reads the Claude Code transcript (`transcript_path`); with none (OpenCode) it allows. Fails
open on any error. Deny = hookSpecificOutput JSON on stdout, exit 0.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
from agent_roles import base_role  # noqa: E402

HOOK = "clear_offer.py"
GATED_AGENTS = {"story-impl", "story-finalize"}
GATED_SKILLS = {"land-story"}
SHELL_TOOLS = {"Bash", "PowerShell"}
ACQUIRE = re.compile(r"land_lease\.py[\"']?\s+acquire\s+[\"']?([A-Za-z0-9][\w.\-]*)")
OFFER = "State saved — /clear"
DEFAULT_THRESHOLD = 300_000
TRIAGE = "## Triage decisions"
AUTO_MARKER = "(auto-dev-loop"
SHELL_WRITE = re.compile(r">>|>\s|Add-Content|Set-Content|Out-File")
STORY_ID = re.compile(r"story[_\s-]?id\s*[:=]\s*[\"'`*]*([A-Za-z0-9][\w.\-]*)", re.IGNORECASE)


def extract_story_id(text: str) -> str | None:
    m = STORY_ID.search(text or "")
    if m:
        return m.group(1)
    prefix = re.escape(str(g.cfg("git.branch_prefix", "story/")))
    m = re.search(rf"{prefix}([A-Za-z0-9][\w.\-]*)", text or "")
    return m.group(1) if m else None


def _blocks(entry: dict) -> list[dict]:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _is_user_turn(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    blocks = _blocks(entry)
    return bool(blocks) and all(b.get("type") == "text" for b in blocks)


def _writes_checkpoint(entry: dict, story_id: str) -> str | None:
    if entry.get("type") != "assistant":
        return None
    for b in _blocks(entry):
        if b.get("type") != "tool_use":
            continue
        inp = b.get("input") or {}
        raw = json.dumps(inp)
        if re.search(rf"story_record\.py append {re.escape(story_id)} \\?[\"']checkpoint-", raw):
            return raw
        if TRIAGE not in raw or story_id not in raw:
            continue
        if b.get("name") in ("Edit", "Write", "MultiEdit") and "code-review-findings" in str(inp.get("file_path", "")):
            return raw
        command = str(inp.get("command", ""))
        if b.get("name") in SHELL_TOOLS and "code-review-findings" in command and SHELL_WRITE.search(command):
            return raw
    return None


def _offers_clear(entry: dict, story_id: str) -> bool:
    if entry.get("type") != "assistant":
        return False
    return any(b.get("type") == "text" and OFFER in (b.get("text") or "") and story_id in (b.get("text") or "")
               for b in _blocks(entry))


def needs_offer(entries: list[dict], story_id: str) -> bool:
    last_write = max((i for i, e in enumerate(entries) if _writes_checkpoint(e, story_id)), default=None)
    if last_write is None or AUTO_MARKER in (_writes_checkpoint(entries[last_write], story_id) or ""):
        return False
    offered = False
    for e in entries[last_write + 1:]:
        if _offers_clear(e, story_id):
            offered = True
        elif offered and _is_user_turn(e):
            return False
    return True


def current_context(entries: list[dict]) -> int:
    for e in reversed(entries):
        usage = (e.get("message") or {}).get("usage") if e.get("type") == "assistant" else None
        if usage:
            return sum(int(usage.get(k, 0) or 0) for k in
                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return 0


def _load(path: str) -> list[dict] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except Exception:  # noqa: BLE001
        return None


def _holds_lease(story_id: str) -> bool:
    """True when the land lease names `story_id`. Raises when unreadable or not installed."""
    import land_lease  # the kit's land-lease script, when installed
    lease = land_lease.read(land_lease.default_path())
    return lease is not None and land_lease.dashed(lease["story"]) == land_lease.dashed(story_id)


def check(payload: dict) -> str | None:
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    what = "dispatch"
    if tool == "Agent" and base_role(str(ti.get("subagent_type") or "").strip()) in GATED_AGENTS:
        story_id = extract_story_id(str(ti.get("prompt") or ""))
    elif tool == "Skill" and str(ti.get("skill") or ti.get("name") or "").strip().lstrip("/") in GATED_SKILLS:
        args = str(ti.get("args") or "")
        if "--cleanup-only" in args:
            return None
        story_id = extract_story_id(args) or (args.split()[0] if args.split() else None)
        try:
            if story_id and _holds_lease(story_id):
                return None
        except Exception:  # noqa: BLE001 -- an unreadable lease fails open
            return None
    elif tool in SHELL_TOOLS:
        opens = ACQUIRE.search(str(ti.get("command") or ""))
        if not opens:
            return None
        story_id, what = opens.group(1), "land window start (land_lease.py acquire)"
    else:
        return None
    path = payload.get("transcript_path")
    if not story_id or not path:
        return None
    entries = _load(path)
    if entries is None or not needs_offer(entries, story_id):
        return None
    threshold = int(g.cfg("guards.clear_offer_context_tokens", DEFAULT_THRESHOLD))
    context = current_context(entries)
    if context < threshold:
        return None
    return (
        f"[workflow-hook] {what} held once -- a checkpoint for {story_id} was recorded in this session, "
        f"the context is {context:,} tokens (>= {threshold:,}) and a clear has not been offered yet.\n"
        f"  End this turn with:  {OFFER}, then /parallel-dev-wave {story_id}\n"
        f"  If an agent you dispatched is still running, say that the clear waits for it.\n"
        f"  The owner's reply (a /clear, or 'continue') releases the dispatch.\n"
        f"(Hook: <plugin-root>/hooks/guards/{HOOK}; rule: parallel-dev-wave \"Fresh session at a heavy human checkpoint\")"
    )


def evaluate(payload: dict) -> int:
    message = check(payload)
    return g.deny_json(message) if message else 0


if __name__ == "__main__":
    g.run("clear_offer", evaluate)
