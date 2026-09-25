#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard: keep diagnostic database MCP servers read-only.
Switch: `config: guards.mcp_readonly`. Servers: `config: guards.mcp_readonly_servers`,
a list of tables `{ server = "<mcp server name>", dialect = "cypher" | "sql" | "none",
extra_deny = ["<regex>", ...] }`. Empty list -> the guard does nothing.

A database MCP server bypasses the application's data-access layer, so a write through it
carries none of the isolation guarantees the application enforces. It is a diagnostics
tool, never a write path. This guard is the second layer behind the server's own
read-only mode (whose query classification can misclassify custom procedures).

For a tool `mcp__<server>__<tool>` of a listed server, deny (JSON permissionDecision):
  M1 — the tool name contains "write" (any upstream spelling).
  M2 — any string argument contains a write clause of the dialect, in clause position
       (not after `:` `.` or a backtick). String literals and comments are blanked first,
       so `WHERE n.name = 'DELETE ME'` stays allowed.
  M3 — any string argument calls a known-mutating procedure family of the dialect.
  M4 — any string argument matches an `extra_deny` regex.
Fail open on malformed payloads and unexpected errors.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

HOOK = "mcp_readonly.py"

DIALECTS: dict[str, dict[str, list[str]]] = {
    "cypher": {
        "clauses": ["CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH",
                    r"LOAD\s+CSV"],
        "procs": [r"\b(?:apoc\.(?:create|merge|refactor|periodic|nodes\.delete|"
                  r"relationships\.delete)|db\.create)\w*"],
    },
    "sql": {
        "clauses": ["INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "CREATE", "ALTER",
                    "DROP", "TRUNCATE", "GRANT", "REVOKE", "COPY", "VACUUM", "REINDEX", "CLUSTER",
                    "CALL", "DO", r"SELECT\s+[^;]*\bINTO\b"],
        "procs": [r"\bpg_(?:terminate_backend|cancel_backend|reload_conf|rotate_logfile)\b",
                  r"\bset_config\s*\("],
    },
    "none": {"clauses": [], "procs": []},
}

_QUOTED = r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`(?:[^`\\]|\\.)*`|/\*.*?\*/"
# Line comments differ per dialect: `//` in Cypher, `--` in SQL.
_LITERALS_RE = {
    "cypher": re.compile(_QUOTED + r"|//[^\n]*", re.DOTALL),
    "sql": re.compile(_QUOTED + r"|--[^\n]*", re.DOTALL),
    "none": re.compile(_QUOTED, re.DOTALL),
}


def strip_literals(text: str, dialect: str = "none") -> str:
    rx = _LITERALS_RE.get(dialect, _LITERALS_RE["none"])
    return rx.sub(lambda m: " " * len(m.group(0)), text)


def iter_strings(value: object, depth: int = 0):
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_strings(item, depth + 1)


def _server_entry(tool_name: str) -> tuple[dict, str] | None:
    for entry in g.cfg_list("guards.mcp_readonly_servers"):
        if not isinstance(entry, dict) or not entry.get("server"):
            continue
        prefix = f"mcp__{entry['server']}__"
        if tool_name.startswith(prefix):
            return entry, tool_name[len(prefix):]
    return None


def evaluate(payload: dict) -> int:
    tool_name = payload.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return 0
    found = _server_entry(tool_name)
    if not found:
        return 0
    entry, suffix = found
    tail = (f"This MCP server is read-only diagnostics; write through the application layer "
            f"instead. (Hook: .workflow/hooks/guards/{HOOK})")

    if "write" in suffix.lower():
        return g.deny_json(f"[workflow-hook] '{tool_name}' is a write tool. {tail}")

    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return 0
    dialect_name = str(entry.get("dialect", "none")).lower()
    if dialect_name not in DIALECTS:
        dialect_name = "none"
    dialect = DIALECTS[dialect_name]
    clause_re = (re.compile(r"(?<![:.`\w])(?:" + "|".join(dialect["clauses"]) + r")(?![\w`])",
                            re.IGNORECASE) if dialect["clauses"] else None)
    proc_res = [re.compile(p, re.IGNORECASE) for p in dialect["procs"]]
    extra = []
    for p in entry.get("extra_deny", []) or []:
        try:
            extra.append(re.compile(str(p), re.IGNORECASE))
        except re.error:
            continue

    for text in iter_strings(ti):
        scanned = strip_literals(text, dialect_name)
        if clause_re:
            m = clause_re.search(scanned)
            if m:
                return g.deny_json(f"[workflow-hook] '{tool_name}' argument contains the write "
                                   f"clause '{m.group(0).upper()}'. {tail}")
        for rx in proc_res:
            m = rx.search(scanned)
            if m:
                return g.deny_json(f"[workflow-hook] '{tool_name}' argument calls the mutating "
                                   f"procedure '{m.group(0)}'. {tail}")
        for rx in extra:
            m = rx.search(scanned)
            if m:
                return g.deny_json(f"[workflow-hook] '{tool_name}' argument matches the "
                                   f"extra_deny pattern '{rx.pattern}'. {tail}")
    return 0


if __name__ == "__main__":
    g.run("mcp_readonly", evaluate)
