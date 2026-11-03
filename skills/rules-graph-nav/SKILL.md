---
name: rules-graph-nav
description: Code-navigation rule for story and review agents — the graph for structure, Read before editing or judging, the slot's graph project only. Preloaded through agent frontmatter skills on Claude Code; invoked first elsewhere.
user-invocable: false
---

- **The slot's project only.** Every graph call passes `project=<graph_project>` from the dispatch prompt. `config: graph.main_project` is the main checkout's graph and returns plausible-but-wrong results for a branch (`.workflow/hooks/guards/graph_query.py` denies it to slot agents). With no project in the prompt, or `config: graph.tool = "none"`, use grep/read only.
- **Structure through the graph.** Where a symbol is defined, who calls it, call chains and change impact go through the graph tools of `config: graph.mcp_server` (search, trace, snippet, query).
- **Read before editing or judging.** Read the code region itself before you edit it or reach a verdict on it; a graph snippet or a search hit is not a reading. Grep is for full-text search and non-code files.
- **Snapshot mode.** When the dispatch prompt says the worktree is being mutated, read only the snapshot paths it provides and do not use the graph.
- **Worktree layout.** Diff and finding paths are repo-relative: join them to the worktree root unchanged. A path quoted from a rule, spec or doc relative to an app subdirectory is resolved under that subdirectory of the worktree, never under the worktree root; when unsure, glob for it under the worktree before concluding it is missing.
