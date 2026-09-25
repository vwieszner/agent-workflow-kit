# Kit script registry

Scripts installed to `.workflow/scripts/` and `.workflow/hooks/`. Kit-managed: a reinstall
overwrites them. Project scripts are registered in the project's own `config: paths.scripts_index`.
Run with `uv run --no-project .workflow/scripts/<name>.py` (or `python3`; Python ≥ 3.11, stdlib only;
psutil optional where noted in the script header). Each script's header documents its CLI and
exit codes.

## Config

| Script | Purpose |
|---|---|
| `wfconfig.py` | The only config loader (`.workflow/config.toml`). CLI: `get <key>`, `ports <slot>`, `render <template>`, `root`. |

## Slot and stack lifecycle

| Script | Purpose | Exit codes |
|---|---|---|
| `slot_registry.py` | Slot-registry file format; `init` creates it from config if absent. | 0 created/exists, 1 error |
| `reserve_slot.py` | Atomic slot reservation (JSON out). | 0 (any status but error), 1 error |
| `release_slot.py` | Atomic slot release (JSON out). | 0 (any status but error), 1 error |
| `story_setup.py` | Phase 1: reserve slot, worktree, spec lookup, graph index, launch async bring-up. | 0 ok, 1 error, 2 no free slot, 3 spec ambiguity |
| `bring_up_story_stack.py` | Compose wrapper for a slot with ALL port vars (`--mode up\|watch\|down\|ps`). | compose exit code |
| `bring_up_stack_async.py` | Background bring-up; writes `.workflow/state/story-setup/<stack>.json`. | — (background) |
| `wait_for_stack_ready.py` | Poll the bring-up status file; confirms services are running now. | 0 up, 1 failed, 2 timeout, 3 stale up |
| `stack_preflight.py` | THE pre-test gate: services, health checks, watch, code sync, branch. | 0 pass, 1 fail |
| `check_container_sync.py` | Prove compose watch synced files into containers; repair if not. | 0 in sync, 1 drift, 2 usage |
| `post_merge_smoke.py` | Post-merge smoke on the shared stack, with diagnoses. | 0 green, 1 fail |
| `start_slot0.py` | Bring the shared dev stack up and wait until usable. | 0 running, 1 compose error, 2 timeout/partial |
| `cleanup_story_stack.py` | Slot teardown: orphan watcher kill, down -v, worktree + branch removal, slot release. | 0 done, 1 unresolvable/refused/error |
| `_stack.py`, `_registry_lookup.py`, `_named_mutex.py` | Internal helpers (compose, registry lookup, cross-platform lock). | — |

## Story state and tests

| Script | Purpose |
|---|---|
| `sprint_status.py` | Key-addressed query/edit of the sprint-status tracker; archives replaced notes to story history. |
| `story_record.py` | Append/check durable phase facts in a story's record file (read by the phase guard). |
| `story_ledger.py` | Project a story's position from journal + git + disk (resume after `/clear`). |
| `log_failure.py` | Atomic append to the autonomous loop's permanent-failure log. |
| `run_scoped_tests.py` | Run `config: tests.scoped_cmd` and print a parsed summary. |
| `run_full_suite.py` | The full gate: preflight, pre-phases, concurrent legs, post-phases; host-global lock. |

## Learning loop and debugging

| Script | Purpose |
|---|---|
| `build_artifact_index.py` | Generate the artifact catalog (skills, agents, rules, hooks, scripts). |
| `trace_port.py` | Identify what generates traffic to a compose service port (tcpdump + process lookup). |
| `install.py` | Installed copy of the kit installer; `render-agent <file>` renders a kit-source agent. |

## Hooks (`.workflow/hooks/`)

| Hook | Event | Purpose |
|---|---|---|
| `guards/bash_command.py` | PreToolUse Bash | Block force-push, `--no-verify`, host installs, `host_forbidden`, `command_deny`. |
| `guards/powershell_command.py` | PreToolUse PowerShell | Same policy for PowerShell, plus recursive-delete allowlist. |
| `guards/dispatch_prompt.py` | PreToolUse Agent/task | Required review preamble; no iterate-and-test phrasing. |
| `guards/phase_dispatch.py` | PreToolUse Agent/Skill | Pipeline phase gate (spec approved before impl; merge approved before land-story). |
| `guards/graph_query.py` | PreToolUse graph MCP | Deny unindexed projects and main-repo queries from slot agents. |
| `guards/mcp_readonly.py` | PreToolUse DB MCPs | Keep configured database MCP servers read-only. |
| `guards/agent_source_read.py` | PreToolUse Bash | Slot agents read source through the code graph, not `cat`/`sed`. |
| `guards/script_registration.py` | PostToolUse Write | Warn when a new project script is unregistered. |
| `session/session_journal.py` | Stop / SubagentStop / `session.idle` | Per-turn journal (no LLM). |
| `session/session_retrospective.py` | PreCompact / compacting | Mine the journal into curation proposals. |
| `session/write_handoff.py` | PreCompact / compacting | Headless handoff writer → `.workflow/state/handoff.md`. |
| `session/check_pending_proposals.py` | SessionStart startup/clear | Nudge about pending curation work. |
| `session/inject_handoff.py` | SessionStart compact/resume | Inject the prior-session handoff. |
| `session/inject_story_ledger.py` | SessionStart all | Position line of each in-flight story. |
| `session/inject_symbol_nav_directive.py` | SessionStart compact/resume | Re-assert code-navigation / docs-MCP directives. |
