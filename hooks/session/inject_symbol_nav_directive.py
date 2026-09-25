#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""SessionStart injector (compact / resume): re-assert the code-navigation and
library-docs directive.

Usage: inject_symbol_nav_directive.py [--tool claude|opencode]   (default claude)

Content is built from config:
  * graph part — only when `config: graph.tool != "none"`; names `config: graph.mcp_server`.
  * docs part  — only when `config: session.docs_mcp_tools` is non-empty (fully-qualified
    Claude tool names, e.g. "mcp__context7__resolve-library-id").
  * `--tool claude` adds a FIRST-ACTION line telling the agent to load the deferred tool
    schemas with ToolSearch (Claude Code defers MCP tool schemas; OpenCode does not).
Prints nothing when neither part applies. Always exits 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

GRAPH_TOOLS = ("search_graph", "trace_path", "get_code_snippet")


def build(tool: str) -> str:
    import wfconfig

    graph_on = str(wfconfig.get("graph.tool", "none")) != "none"
    server = str(wfconfig.get("graph.mcp_server", "codebase-memory-mcp"))
    docs_tools = [str(t) for t in (wfconfig.get("session.docs_mcp_tools", []) or [])]
    if not graph_on and not docs_tools:
        return ""

    preload = ([f"mcp__{server}__{t}" for t in GRAPH_TOOLS] if graph_on else []) + docs_tools
    parts: list[str] = []
    if tool == "claude":
        parts.append(
            "=== MUST-DO: FIRST ACTION THIS SESSION ===\n"
            f'Call ToolSearch with query "select:{",".join(preload)}" NOW, before any other '
            "tool call. These are deferred tools and are NOT usable until their schemas are loaded."
        )
    else:
        parts.append("=== Navigation directive (re-asserted after compaction / resume) ===")
    if graph_on:
        parts.append(
            f"Use the {server} code graph for ALL code-symbol navigation this session — finding "
            "symbols, tracing call chains (who-calls-what), change-impact analysis, and reading a "
            "symbol's exact source — instead of grep/read scanning for symbols.\n"
            "If the project is not indexed yet, run index_repository first (list_projects to "
            "check); symbol queries against an unindexed project return nothing. In a story slot "
            "use the slot's graph_project, never the main checkout's.\n"
            "Grep/read stay correct for full-text search, non-code files (docs, YAML, configs) "
            "and reading a region already located — and always read a file before editing it."
        )
    if docs_tools:
        names = ", ".join(t.split("__")[-1] for t in docs_tools)
        parts.append(
            f"Use the library-docs tools ({names}) BEFORE hand-rolling any infra-class behaviour "
            "(fork-safety, pooling, retries, presence, caching): check what the installed "
            "library already provides and implement only the missing delta — and whenever a "
            "library's runtime behaviour is load-bearing for an answer or a spec (spec creation "
            "runs in the main session and uses these same tools)."
        )
    return "\n".join(parts)


def main(argv: list[str]) -> int:
    tool = "claude"
    if "--tool" in argv:
        i = argv.index("--tool")
        if i + 1 < len(argv):
            tool = argv[i + 1]
    text = build(tool)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(0)
