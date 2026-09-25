#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Per-turn session journal hook (NO LLM) — capture stage of the curation loop.

Wiring
    Claude Code: `Stop` and `SubagentStop` hooks.
    OpenCode:    the workflow-hooks plugin calls it on `session.idle` with a
                 `workflow_tool_calls` payload.

Accepted payloads (JSON on stdin)
    1. Claude Code Stop/SubagentStop payload: `session_id`, `transcript_path`, optional
       `agent_id`. The hook reads the transcript DELTA since a stored byte cursor
       (.workflow/state/journal/cursors/) — O(delta) per turn.
    2. Plugin payload: `session_id`, optional `agent_id`, and
       `workflow_tool_calls: [{"tool": str, "input": str|object, "ok": bool}, ...]`
       already summarized by the OpenCode plugin. Tool names may be OpenCode-native
       (`bash`, `write`, `task`, ...) or Claude-shaped (`Bash`, `Write`, `Agent`, ...).
       When `workflow_tool_calls` is present it wins; `transcript_path` is ignored.

Output (DESIGN §8): one JSON line per turn appended to
    .workflow/state/journal/<session_id>.jsonl              (main session)
    .workflow/state/journal/<session_id>.sub-<agent>.jsonl  (subagent)
    {"ts", "session_id", "source", ["agent_id"],
     "tool_calls": [{"tool", "input", "ok"}],
     "facts": [{"kind": script_written|script_edited|command_run|skill_invoked|
                agent_spawned|tool_error|interrupted|user_message, ...}]}
`facts` is derived from the tool calls (plus user messages / interrupts, transcript mode only)
and is what the retrospective mines.

Contract: always exits 0 and prints nothing — never blocks a turn, never injects context.
No-op when WORKFLOW_RETRO_RUNNING is set (inside the retrospective's own analyst run) or
WORKFLOW_HEADLESS is set (inside the pre-compaction handoff writer's headless run).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_EXTS = (".py", ".ps1", ".sh", ".bat")
CMD_TOOLS = {"Bash", "PowerShell"}
# OpenCode-native tool names → canonical (Claude-shaped) names.
TOOL_ALIASES = {
    "bash": "Bash", "shell": "Bash", "powershell": "PowerShell",
    "write": "Write", "edit": "Edit", "multiedit": "MultiEdit", "patch": "Edit",
    "task": "Agent", "agent": "Agent", "skill": "Skill",
    "read": "Read", "grep": "Grep", "glob": "Glob", "list": "LS",
    "webfetch": "WebFetch", "websearch": "WebSearch", "todowrite": "TodoWrite",
}
# User-message prefixes that are hook/system injections, not real user input.
INJECTION_PREFIXES = (
    "<command-message>",
    "<system-reminder>",
    "Call ToolSearch with query",
)
MAX_CMD = 300
MAX_TEXT = 500
MAX_ERR = 300
MAX_INPUT = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canon_tool(name) -> str:
    name = str(name or "?")
    if name.startswith("mcp__"):
        return name
    return TOOL_ALIASES.get(name, TOOL_ALIASES.get(name.lower(), name))


def _is_script(path) -> bool:
    return isinstance(path, str) and path.lower().endswith(SCRIPT_EXTS)


def _field(inp: dict, *keys: str) -> str:
    for k in keys:
        v = inp.get(k)
        if v:
            return str(v)
    return ""


def _normalize(tool: str, inp) -> dict:
    """→ {"tool", "input" (short summary), "ok", "_detail" (fields for fact derivation)}."""
    detail: dict = {}
    if isinstance(inp, dict):
        detail["path"] = _field(inp, "file_path", "filePath", "path")
        detail["command"] = _field(inp, "command", "cmd")
        detail["skill"] = _field(inp, "skill", "name")
        detail["agent_type"] = _field(inp, "subagent_type", "agent", "subagentType")
        detail["desc"] = _field(inp, "description")
    else:
        text = str(inp or "")
        if tool in CMD_TOOLS:
            detail["command"] = text
        elif tool in ("Write", "Edit", "MultiEdit"):
            detail["path"] = text
        elif tool == "Skill":
            detail["skill"] = text
        elif tool == "Agent":
            detail["agent_type"] = text.split(":", 1)[0].strip()
            detail["desc"] = text
        detail["raw"] = text

    if tool in CMD_TOOLS:
        summary = detail.get("command", "")
    elif tool in ("Write", "Edit", "MultiEdit", "Read"):
        summary = detail.get("path", "") or detail.get("raw", "")
    elif tool == "Skill":
        summary = detail.get("skill", "")
    elif tool == "Agent":
        summary = f"{detail.get('agent_type', '')}: {detail.get('desc', '')}".strip(": ")
    elif isinstance(inp, dict):
        summary = _field(inp, "pattern", "query", "url", "file_path", "filePath", "path") \
            or json.dumps(inp, ensure_ascii=False, default=str)
    else:
        summary = detail.get("raw", "")
    cap = MAX_CMD if tool in CMD_TOOLS else MAX_INPUT
    return {"tool": tool, "input": str(summary)[:cap], "ok": True, "_detail": detail}


def _facts_from_call(call: dict, ts) -> list:
    tool, d = call["tool"], call.get("_detail", {})
    facts = []
    if tool == "Write" and _is_script(d.get("path")):
        facts.append({"ts": ts, "kind": "script_written", "path": d["path"]})
    elif tool in ("Edit", "MultiEdit") and _is_script(d.get("path")):
        facts.append({"ts": ts, "kind": "script_edited", "path": d["path"]})
    elif tool in CMD_TOOLS:
        facts.append({"ts": ts, "kind": "command_run", "tool": tool,
                      "command": d.get("command", "")[:MAX_CMD]})
    elif tool == "Skill":
        facts.append({"ts": ts, "kind": "skill_invoked", "skill": d.get("skill")})
    elif tool == "Agent":
        facts.append({"ts": ts, "kind": "agent_spawned", "agent_type": d.get("agent_type"),
                      "desc": d.get("desc", "")[:MAX_TEXT]})
    if not call.get("ok", True):
        facts.append({"ts": ts, "kind": "tool_error", "tool": tool,
                      "snippet": str(call.get("_error", ""))[:MAX_ERR]})
    return facts


# --------------------------------------------------------------------------- plugin payload

def _from_plugin(raw_calls: list) -> tuple[list, list]:
    calls, facts = [], []
    ts = _now()
    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        tool = _canon_tool(item.get("tool"))
        call = _normalize(tool, item.get("input"))
        call["ok"] = bool(item.get("ok", True))
        if item.get("error"):
            call["_error"] = item.get("error")
        calls.append(call)
        facts.extend(_facts_from_call(call, ts))
    return calls, facts


# --------------------------------------------------------------------------- transcript payload

def _cursor_path(cdir: Path, transcript_path: str) -> Path:
    key = hashlib.sha1(transcript_path.encode("utf-8")).hexdigest()[:16]
    return cdir / f"{key}.cur"


def _read_cursor(p: Path) -> int:
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_cursor(p: Path, value: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(value), encoding="utf-8")


def _from_transcript_lines(lines: list) -> tuple[list, list]:
    calls: list = []
    extra: list = []           # user_message / interrupted / orphan tool_error facts
    by_id: dict = {}           # tool_use id → call
    ts_of: dict = {}           # id(call) → transcript timestamp
    for obj in lines:
        ts = obj.get("timestamp")
        t = obj.get("type")
        content = (obj.get("message") or {}).get("content")
        if t == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                call = _normalize(_canon_tool(block.get("name")), block.get("input") or {})
                calls.append(call)
                ts_of[id(call)] = ts
                if block.get("id"):
                    by_id[block["id"]] = call
        elif t == "user":
            if isinstance(content, str):
                text = content.strip()
                if text and not text.startswith(INJECTION_PREFIXES) and '"hook_event_name"' not in text:
                    extra.append({"ts": ts, "kind": "user_message", "text": text[:MAX_TEXT]})
            elif isinstance(content, list):
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"
                            and block.get("is_error")):
                        continue
                    snippet = block.get("content")
                    if isinstance(snippet, list):
                        snippet = " ".join(str(b.get("text", b)) for b in snippet
                                           if isinstance(b, dict)) or str(snippet)
                    call = by_id.get(block.get("tool_use_id"))
                    if call is not None:
                        call["ok"] = False
                        call["_error"] = snippet
                    else:  # tool_use was in an earlier delta
                        extra.append({"ts": ts, "kind": "tool_error", "tool": "?",
                                      "snippet": str(snippet)[:MAX_ERR]})
            tur = obj.get("toolUseResult")
            if isinstance(tur, dict) and tur.get("interrupted"):
                extra.append({"ts": ts, "kind": "interrupted"})
    facts: list = []
    for call in calls:
        facts.extend(_facts_from_call(call, ts_of.get(id(call))))
    return calls, facts + extra


def _read_delta(cs, transcript_path: str):
    """→ (parsed lines, cursor file, new cursor) or None when there is nothing complete."""
    cur_path = _cursor_path(cs.cursors_dir(), transcript_path)
    cursor = _read_cursor(cur_path)
    size = os.path.getsize(transcript_path)
    if cursor > size:  # transcript rotated / truncated
        cursor = 0
    with open(transcript_path, "rb") as fh:
        fh.seek(cursor)
        data = fh.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return None
    complete = data[: last_nl + 1]
    parsed = []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
    return parsed, cur_path, cursor + len(complete)


# --------------------------------------------------------------------------- main

def _append(cs, session_id: str, agent_id, source: str, calls: list, facts: list) -> bool:
    if not calls and not facts:
        return True
    jdir = cs.journal_dir()
    jdir.mkdir(parents=True, exist_ok=True)
    suffix = f".sub-{cs.safe_name(agent_id)}" if agent_id else ""
    entry = {"ts": _now(), "session_id": session_id, "source": source}
    if agent_id:
        entry["agent_id"] = agent_id
    entry["tool_calls"] = [{"tool": c["tool"], "input": c["input"], "ok": c["ok"]} for c in calls]
    entry["facts"] = facts
    try:
        with open(jdir / f"{cs.safe_name(session_id)}{suffix}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return False
    return True


def main() -> int:
    if os.environ.get("WORKFLOW_RETRO_RUNNING") or os.environ.get("WORKFLOW_HEADLESS"):
        return 0
    try:
        raw = sys.stdin.buffer.read().lstrip(b"\xef\xbb\xbf")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _curation_state as cs

        session_id = str(payload.get("session_id") or "unknown-session")
        agent_id = payload.get("agent_id")

        tool_calls = payload.get("workflow_tool_calls")
        if isinstance(tool_calls, list):
            calls, facts = _from_plugin(tool_calls)
            _append(cs, session_id, agent_id, "plugin", calls, facts)
            return 0

        transcript_path = payload.get("transcript_path")
        if not transcript_path or not os.path.isfile(transcript_path):
            return 0
        delta = _read_delta(cs, transcript_path)
        if delta is None:
            return 0
        parsed, cur_path, new_cursor = delta
        calls, facts = _from_transcript_lines(parsed)
        if _append(cs, session_id, agent_id, "transcript", calls, facts):
            _write_cursor(cur_path, new_cursor)  # only advance once persisted
    except Exception:  # noqa: BLE001 — a journal hook must never break a turn
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
