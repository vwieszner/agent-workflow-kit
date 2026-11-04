---
name: setup
description: Set up the current project for the agent-workflow-kit plugin — create .workflow/config.toml, the project rules files and the slot registry, and add the kit's permission deny rules to .claude/settings.json. Use once per project, when a session reports that .workflow/config.toml is missing, or when the owner says "set up the workflow kit".
---

# Setup

1. Run `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/plugin_setup.py" --dry-run` and show the owner its lines.
2. Ask the owner to confirm; then run it without `--dry-run`. Existing files are kept, never overwritten.
3. If it prints a WARNING about kit hooks still wired in `.claude/settings.json` (an earlier `install.py` install), tell the owner: each guard would run twice. Remove those entries only when the owner says so.
4. Tell the owner the next step: fill in `.workflow/config.toml` — at minimum `[project]`, `[git] base_branch`, `[paths]`, `[tests] scoped_cmd` / `full_cmd` with their regexes, and `[stack]` (or `runtime = "none"`). Every key is documented inline. Check a value with `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/wfconfig.py" get <section.key>`.
