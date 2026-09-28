#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard on the code-graph MCP server's tools (`mcp__<graph.mcp_server>__*`).
Switch: `config: guards.graph_query`. Skipped entirely when `config: graph.tool = "none"`.

Denies via JSON permissionDecision on stdout (exit 2 does not block MCP tools):

  G2 — a slot-only agent (`config: guards.slot_only_agents`, default story-impl,
       story-finalize, story-rebase, story-diagnose; identified by the payload's `agent_type`) targeting the main
       checkout's graph project (`config: graph.main_project`). Those agents work only in
       a worktree; the main graph gives plausible, wrong results for their branch.
  G1 — a query against a project with no index file in `config: graph.cache_dir`
       (`<project>.db`). Skipped when the cache dir does not exist.

Exempt tools (they fix or diagnose an unindexed state): `config: guards.graph_exempt_tools`.
Fail open: no `project` argument, unknown agent, any unexpected error -> allow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

HOOK = "graph_query.py"
DEFAULT_EXEMPT = ["index_repository", "list_projects", "delete_project", "index_status"]


def evaluate(payload: dict) -> int:
    if str(g.cfg("graph.tool", "none")) == "none":
        return 0
    server = str(g.cfg("graph.mcp_server", "codebase-memory-mcp"))
    prefix = f"mcp__{server}__"
    tool_name = payload.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name.startswith(prefix):
        return 0
    if tool_name[len(prefix):] in set(g.cfg_list("guards.graph_exempt_tools", DEFAULT_EXEMPT)):
        return 0
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return 0
    project = ti.get("project")
    if not isinstance(project, str) or not project.strip():
        return 0
    project = project.strip()

    main_project = str(g.cfg("graph.main_project", "") or "")
    slot_only = set(g.cfg_list("guards.slot_only_agents", ["story-impl", "story-finalize", "story-rebase", "story-diagnose"]))
    agent = payload.get("agent_type")
    if main_project and agent in slot_only and project == main_project:
        return g.deny_json(
            f"[workflow-hook] {agent} runs inside a story worktree; project '{main_project}' "
            f"is the main checkout's graph, not your branch's — results would be plausible "
            f"and wrong. Use the graph_project from your dispatch inputs (story_setup report / "
            f".workflow/state/story-setup/<stack>.graph_project). (Hook: .workflow/hooks/guards/{HOOK})"
        )

    cache_raw = str(g.cfg("graph.cache_dir", "~/.cache/codebase-memory-mcp") or "")
    if not cache_raw:
        return 0
    cache = Path(os.path.expanduser(cache_raw))
    if not cache.is_dir():
        return 0
    if (cache / f"{project}.db").is_file():
        return 0
    indexed = sorted(p.stem for p in cache.glob("*.db") if not p.stem.startswith("_"))
    return g.deny_json(
        f"[workflow-hook] project '{project}' is not indexed. Run index_repository(repo_path=...) "
        f"first. Indexed projects: {', '.join(indexed) if indexed else '(none)'}. In a story "
        f"slot use the slot's graph_project, never a guessed slug. (Hook: .workflow/hooks/guards/{HOOK})"
    )


if __name__ == "__main__":
    g.run("graph_query", evaluate)
