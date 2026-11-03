# agent-workflow-kit (Claude Code plugin)

Generated from the kit source by `python3 install.py build-plugin` — do not edit files here; edit the
kit and rebuild. The kit's README covers the workflow itself.

## Install

```
/plugin marketplace add vwieszner/agent-workflow-kit
/plugin install agent-workflow-kit@agent-workflow-kit
```

Then, once per project, invoke the `agent-workflow-kit:setup` skill (it creates `.workflow/config.toml`,
the project rules files and the slot registry, and adds the kit's deny rules to `.claude/settings.json`),
and fill in `.workflow/config.toml`.

## Differences from the `install.py` route

- Skills, agents and the review workflow are named `agent-workflow-kit:<name>`; the guards accept both
  forms.
- The working rules (`AGENTS.md`) and the project's `.workflow/AGENTS.local.md` are injected at session
  start by the plugin's SessionStart hook (a plugin cannot load CLAUDE.md).
- Plugin agents cannot carry their own hooks or permission mode: the completion checks and the context
  cap run from the plugin's hooks with `--role auto`, and agents run under the session's permission mode.
- Agents are rendered with the example config's models; the graph, docs and browser MCP tools are listed
  for every agent that declares them (absent servers are simply unavailable). A project that needs other
  models or MCP servers uses the `install.py` route.
- OpenCode is not covered by the plugin; use `install.py --tool opencode`.
