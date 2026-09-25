#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard on Bash / PowerShell: slot agents read source through the code graph,
not the shell. Switch: `config: guards.agent_source_read`. Skipped entirely when
`config: graph.tool = "none"` (without a graph there is nothing to redirect to).

Deny (JSON permissionDecision) only when ALL hold:
  * the caller's `agent_type` is in `config: guards.source_read_gated_agents`
    (default story-impl, story-finalize);
  * a pipeline segment's FIRST word is a read verb (cat head tail sed more less);
  * one of that segment's own arguments ends in a source suffix
    (`config: guards.source_suffixes`).

Never denied: the Read tool (the escape hatch for a just-written file or a located region);
`<cmd> | tail -40` (tail's segment has no file arg); `cat > out.py <<EOF` (a redirect makes
it a write); docs / YAML / JSON / logs. Fail open on anything unexpected.
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

HOOK = "agent_source_read.py"
READ_VERBS = {"cat", "head", "tail", "sed", "more", "less"}
DEFAULT_SUFFIXES = [".py", ".js", ".jsx", ".ts", ".tsx"]
SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;|\n")


def offending_path(segment: str, suffixes: tuple[str, ...]) -> str | None:
    seg = segment.strip()
    if not seg or ">" in seg:
        return None
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    verb = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if verb not in READ_VERBS:
        return None
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        if tok.lower().endswith(suffixes):
            return tok
    return None


def evaluate(payload: dict) -> int:
    if str(g.cfg("graph.tool", "none")) == "none":
        return 0
    gated = set(g.cfg_list("guards.source_read_gated_agents", ["story-impl", "story-finalize"]))
    agent = payload.get("agent_type")
    if agent not in gated:
        return 0
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return 0
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return 0
    command = ti.get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    suffixes = tuple(s.lower() for s in g.cfg_list("guards.source_suffixes", DEFAULT_SUFFIXES))
    server = str(g.cfg("graph.mcp_server", "codebase-memory-mcp"))
    for segment in SEGMENT_SPLIT.split(command):
        path = offending_path(segment, suffixes)
        if path:
            return g.deny_json(
                f"[workflow-hook] {agent} may not read source via the shell: '{path}'. Reading "
                f"a symbol's source, locating a file:line and tracing call chains go through the "
                f"code graph ({server}: get_code_snippet / search_graph / trace_path with "
                f"project=<your graph_project>). If the region is already located, or the file "
                f"was just written and the index is stale, use the Read tool — it is not gated. "
                f"Docs, YAML, JSON and logs are not gated either. (Hook: .workflow/hooks/guards/{HOOK})"
            )
    return 0


if __name__ == "__main__":
    g.run("agent_source_read", evaluate)
