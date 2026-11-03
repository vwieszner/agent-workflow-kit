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
| `install.py` | Installed copy of the kit installer; `render-agent <file>` renders a kit-source agent. |
| `plugin_setup.py` | Set up a project for the Claude Code plugin (the `setup` skill): `.workflow/config.toml`, local rules files, `.gitignore`, slot registry, the kit's `permissions.deny` rules; names kit hooks an `install.py` install still wires. `--dry-run`. 0 done, 1 error. |
| `check_agent_frontmatter.py` | Agent model/effort policy check; `--hook` runs it as PostToolUse(Write\|Edit) with a block decision. |
| `gen_agent_effort_variants.py` | Writes/checks `story-impl-xhigh` / `story-impl-max` from `story-impl.md` (`--check` exits 1 on drift). Kit-maintainer script. |
| `agent_roles.py` | Effort variant → base agent role (`base_role`); imported by guards, ledger and readers. |

## Slot and stack lifecycle

| Script | Purpose | Exit codes |
|---|---|---|
| `slot_registry.py` | Slot-registry file format; `init` creates it from config if absent. | 0 created/exists, 1 error |
| `reserve_slot.py` | Atomic slot reservation (JSON out). | 0 (any status but error), 1 error |
| `release_slot.py` | Atomic slot release (JSON out). | 0 (any status but error), 1 error |
| `story_setup.py` | Phase 1: reserve slot, worktree, spec lookup, graph index, launch async bring-up; `--reserved-slot`. | 0 ok, 1 error, 2 no free slot, 3 spec ambiguity, 4 reservation refused |
| `bring_up_story_stack.py` | Compose wrapper for a slot with ALL port vars (`--mode up\|watch\|down\|stop\|seed\|ps`); `stop`/`down`/`seed` refused on the shared stack. | compose exit code |
| `bring_up_stack_async.py` | Background bring-up (+ seed step); writes `.workflow/state/story-setup/<stack>.json`. | — (background) |
| `wait_for_stack_ready.py` | Poll the bring-up status file; confirms services are running now. | 0 up, 1 failed, 2 timeout, 3 stale up |
| `wait_for_watch_ready.py` | Wait until a compose-watch output file shows ready, or name why not (stale lock, watch exited). | 0 ready, 1 failed, 2 timeout, 3 args |
| `stack_preflight.py` | THE pre-test gate: services, health checks, watch, code sync (routed per sync mount), branch. | 0 pass, 1 fail |
| `check_settings_and_docs_integrity.py` | dev-preflight Step 0: every hook script the Claude settings / OpenCode plugin reference exists and is git-tracked; every `config: integrity.quoted_passages` entry still exists. Zero checks is a FAIL. | 0 ok, 1 fail |
| `check_container_sync.py` | Prove compose watch synced files into containers (`--absent`: deleted files gone); repair if not. | 0 in sync, 1 drift, 2 usage |
| `post_merge_smoke.py` | Post-merge smoke on the shared stack: service state, log scan, source-sync gate; diagnoses S, E, A, configured, A2, D. | 0 green, 1 fail |
| `start_slot0.py` | Bring the shared dev stack up and wait until usable. | 0 running, 1 compose error, 2 timeout/partial |
| `cleanup_story_stack.py` | Slot teardown: owned watcher stop, down -v, worktree + branch + train/review refs removal, slot release; `--keep-worktree`; keeps the propagation checklist and range diffs in story history. | 0 done, 1 unresolvable/refused/watcher survived, 2 unmerged teardown inside a driver session |
| `kill_owned.py` | Kill host processes only when the caller provably owns them (CWD in its worktree, created after its start, no other stack's gate/watch above); `--pid N [--tree]` or `--watch`; needs the process-kill guard's token. PEP 723 psutil. | 0, 1 REFUSE/ALIVE, 2 args/token |
| `kill_token.py` | Single-use token issue/redeem shared by the process-kill guard and `kill_owned.py` (`.workflow/state/kill-tokens/`). Library. | — |
| `proc_scan.py` | Read-only process finder: `--watch <stack>`, `--match <regex>`, `--pid N --chain\|--tree`, `--wait-gone <regex>`. PEP 723 psutil. | 0 |
| `_stack.py`, `_registry_lookup.py`, `_named_mutex.py` | Internal helpers (compose, registry lookup, cross-platform lock). | — |

## Story state, land window and tests

| Script | Purpose |
|---|---|
| `sprint_status.py` | Key-addressed query/edit of the sprint-status tracker (`get` many keys, `--brief`; `list --exclude-status --match --notes`; `set --note ''` clears); archives replaced notes to story history. |
| `story_record.py` | Append/check durable phase facts in a story's record file (read by the phase guard); `open`, `reach`, `rule`; checkpoint-1 refused while an open question stands. |
| `story_ledger.py` | Project a story's position from journal + git + disk (resume after `/clear`): land guard, review loop, seed ACs due, review re-run due, nested dispatches. |
| `story_refs.py` | Every doc line naming a story (scopes from `config: paths.*`). |
| `story_research_pack.py` | Capped research pack for story-spec (`<id> --worktree W [--out F]`). |
| `stage_own_hunks.py` | Stage only this session's hunks in the shared index (`list \| pick \| stage \| gate \| verify`). |
| `branch_overlap.py` | Files a branch and its base both touched (OVERLAP) and predicted conflicts; before every story-rebase dispatch. |
| `land_lease.py` | The land lease (`acquire [--wait-minutes N] \| release \| holds \| status`). |
| `merge_train.py` | Serial merge train (`wait \| build \| settle \| home \| status`); gates the train branch while a story waits in the lease line. |
| `log_failure.py` | Atomic append to the autonomous loop's permanent-failure log (`--asked-of`, `--external-ticket`). |
| `run_scoped_tests.py` | Scoped run: default leg (`config: tests.scoped_cmd`), named legs (`--leg NAME=VALUE`), host script tests (`--scripts`), `--require-synced`; `--record <id> <phase>` writes the GREEN line with its `code-tree`. |
| `run_full_suite.py` | The full gate: preflight, schema-drift + pre-phases, concurrent legs, post-phases; one gate per stack (first-come-first-served line); `--record` (GREEN line, approval withdrawn on red while holding the lease, exit 75 = not run); `--train` gates the train branch; `--profile-leg` runs the profile leg alone under its profile (the stack returned to its resting profile; `RESTORE: FAILED` fails the gate; a green run resets the due count); `--hold-lock-for reset-test-dbs\|land-merge`. |
| `static_checks.py` | Config-driven static checks (`config: static_checks.tools`) on changed or all files. 0 clean, 1 blocking, 2 infra. |
| `gate_campaign.py` | K-consecutive full-gate campaign on a story stack; stops at the first red gate (1), over-budget wall (2), forbidden log line (3). |
| `host_cpu_load.py` | Host CPU load generator (N busy processes, hard stop + stop-file) for proving timing budgets under load. |
| `pw_failure_summary.py` | Summarise Playwright trace.zip failures: assertion, actions, failing HTTP, console errors. |
| `profile_leg.py` | The profile leg's merge counter: `due [--every N]` — 0 not due, 1 due (prints the `--profile-leg` command), 2 cannot tell, 3 not configured (SKIPPED); green runs recorded in `.workflow/state/<config: profile_leg.state_file>`. |
| `gate_profile.py` | Switch a gate stack between named serving profiles (`config: profiles`): drain → recreate → ready → verify; `ensure` switches only on a mismatch. `status \| ensure <profile> \| rest [--story-id]`; 0 ok, 1 not in profile / switch failed, 2 config. |
| `gate_sampler.py` | Load and probe sampler for a gate run (`sample` / `summarize [--check]`): VM load + CPU split, per-container cores and block I/O, `config: sampler.probes`; budgets from `config: sampler.load_budget`. |

## Review pipeline

| Script | Purpose |
|---|---|
| `review_stage.py` | Stage one review round (diff, chunks, UI diff, claims, carried findings, spec ACs, file classes) and print the review workflow's `args` JSON; pins `refs/review/<branch>/round-<n>`. |
| `review_diff_chunks.py` | Split a review diff into numbered chunk files sized for the Read tool. |
| `review_spec_acs.py` | List the AC ids a spec declares (ui-verifier). |
| `review_carry.py` | Print the findings a review round carries from the story's halted rounds. |
| `review_findings_render.py` | Rebuild a review round from the workflow run + journal, append it to the findings file, record `review round <n> recorded`. |
| `review_findings_show.py` | Print one or more findings by id from a findings file. |
| `review_ledger.py` | Review-loop state (recorded/triaged/finalized rounds, loop end, re-run due). Library. |
| `review_precision.py` | Review precision per story and layer from rendered findings files. |
| `run_mutation.py` | Mutation-test a story's changed Python functions against its tests (mutmut 3; test-adequacy). |
| `workflow_replay.mjs` | Node (≥ 18) replay runner for a Workflow script where no Workflow runtime exists (OpenCode fallback of layered-review). |

## Autonomous loop

| Script | Purpose |
|---|---|
| `auto_dev_driver.py` | Run auto-dev-loop one phase per fresh headless session (`start` / `list` / `stop`); run records under `.workflow/state/auto-dev/`. |
| `auto_dev_guard.py` | Keep-halted rules for auto-dev-loop sessions (`shell_check`, `merge_refusal`, `mark_from_payload`). Library. |
| `auto_dev_audit.py` | Post-session audit of the story a driver session drove. |
| `auto_dev_run_report.py` | Report one driver run from its log, sessions file and session streams. |
| `halt_release.py` | One-command halt-and-release: keep the work, release the slot, commit only its own bookkeeping; `--asked-of owner\|external\|both` (required), `--external-ticket`, `--external-item` (tracker step). |
| `resume_halted.py` | Resume a halted story: archive its failure entries with the ruling, remove them, row → ready-for-dev; refuses while the design owner's ticket is open. |
| `milestone_map_check.py` | Mechanical lists of milestone-blockers Steps 2/2b/2d over the dependency map (read-only). |
| `_bookkeeping.py` | Internal: failure-log parser and own-change commit on the base branch. |
| `auto_dev_metrics.py` | Quality, cost and time per driver run (`run [<id>\|latest]`) and across configuration fingerprints (`compare`); `--cost cached\|live`, `--json`. 0 printed, 2 usage/no run. |

## Control room and read model

| Script | Purpose |
|---|---|
| `story_state.py` | One JSON snapshot of every in-flight story (slot, worktree, stack, position, record, artifacts, git, agents, cost, driver runs, sessions) and the attention queue (what waits on the owner or the design owner); `--story`, `--attention`, `--no-cost`. Read-only. 0 printed, 2 usage/no main checkout. |
| `control_room.py` | Localhost web control room (127.0.0.1 only): board, story detail, attention queue, driver runs, metrics; start/stop a driver run, open a session in a terminal, start a story session. `--port` (default `config: control_room.port`). |
| `control_room_launch.py` | The control room's side effects: window backends (`config: control_room.launcher`: Windows Terminal, tmux, background) and story-session starts with `config: loop.driver_tool`. Library. |
| `control_room_views.py` | The control room's HTML, pure functions of the documents. Library. |
| `control_room_static/` | Vendored htmx 2.0.4 (0BSD, see its VENDOR.md) and the control room's stylesheet. |

## Design owner and design source (optional; each step SKIPPED when unconfigured)

| Script | Purpose |
|---|---|
| `design_ticket.py` | Deterministic parts of filing a story's design-owner questions: `label`, `render`, `tracker-row`, `commit-tracker`, `mark-spec`. 0 ok, 1 refusal, 2 usage. |
| `tracker.py` | Issue-tracker calls behind one interface (`config: tracker.backend`: jira REST, mcp, none): `search`, `create`, `comment`, `comments`. 0, 1 tracker error, 2 config/credentials, 3 SKIPPED. |
| `tracker_digest.py` | Digest a tracker search result (REST file or MCP result, `--session`): one line per issue, new comments since the last sync. 0, 1 more pages, 2 bad input. |
| `docs_mirror.py` | Keep the read-only mirror of the external specification page (MCP result, `--session`, or `--fetch` REST; `--init`): version gate, section summary, diff file. 0, 1 drift at same version, 2 bad input, 3 SKIPPED. |
| `token_doc_check.py` | Compare the specification's token tables with the design token export (MISMATCH / MISSING / EXPORT-ONLY). 0, 1 differences, 2 format changed, 3 SKIPPED. |
| `design_library.py` | Build the design library (REST or MCP transcripts): INDEX, value sheets, frame maps, screen values, flows, tokens, crops; `--changes`, `--plan`, `--accept-fingerprints`. PEP 723 pillow (crops only). 0, 3 SKIPPED. |
| `design_assets.py` | Rebuild the design intake's uncommitted binary assets: one PNG render per manifest frame and the docs-page image groups (`attachments.groups`, masters kept). PEP 723 pillow (resizing). 0 written, 1 nothing, 2 network/HTTP, 3 SKIPPED. |
| `design_extract.js` | Design-tool plugin-API extractor pasted into the design MCP's plugin-code call (`sizeNodes`, `dumpNodes`, `frameMap`, `fingerprints`, `stylesDump`); not run by Python. |
| `design_manifest.example.json` | Example design manifest (`config: design.manifest`). |
| `design_page_renders.py` | Save the design MCP's page-render asset URLs into `<library>/renders/_pages/`. 0, 1 refused/failed, 3 SKIPPED. |
| `design_instances.py` | Every drawn instance of one library component across the frame maps. 0, 2 no build, 3 SKIPPED. |
| `design_context.py` | Convert a design-context tool result into `<intake>/design-context/<slug>.md` with a provenance header. 0, 3 SKIPPED. |
| `design_tokens.py` | `export`: reassemble the token export from transcript chunks; `build`: run the project's token build in a story stack and copy the output back. 0, 3 SKIPPED. |
| `snapshot_update.py` | Regenerate a configured test snapshot in a story stack and copy it back (`config: design.snapshots`). 0, 1 still failing, 2 refused/infra, 3 SKIPPED. |
| `ui_gap_owner.py` | Which story spec or epic owns each UI string, with sprint status; REGRESSION-CANDIDATE for done stories. |
| `_design.py` | Internal: design-question tracker table, ticket key regex, design-owner tag. |

## Learning loop and harness ops

| Script | Purpose |
|---|---|
| `build_artifact_index.py` | Generate the artifact catalog (skills, agents, rules, hooks, scripts). |
| `retro_digest.py` | Clustered overview of every unmined session's new journal facts — `retro` Step 2. |
| `journal_grep.py` | Cross-session journal search: `error <regex>` / `command <regex>`; `--since`, `--agent-type`. |
| `transcript_calls.py` | Claude Code transcripts: `grep` calls/results and `replay` a hook draft over history. 0, 2 usage/refused. |
| `denial_samples.py` | Sample denied tool calls per agent type with the denial text and the agent's next call. |
| `session_cost_report.py` | Per-transcript cost/context forensics. 0, 1 no transcript dir. |
| `harness_metrics.py` | Per-story cost/effort/denial attribution by phase and agent type; `--summary`, `--json`, `--cut`. |
| `proposal_drift.py` | Per pending proposal and target: NEW-ABSENT / ADDED-SINCE / UNCHANGED / CHANGED / LANDED / PATCH-APPLIES/FAILS — `curate` Step 2. |
| `trace_port.py` | Identify what generates traffic to a compose service port (tcpdump + process lookup). |
| `_transcripts.py` | Internal: Claude project-dir resolution, journal fact iteration, `base_role`. |

## Hooks (`.workflow/hooks/`)

| Hook | Event | Purpose |
|---|---|---|
| `guards/bash_command.py` | PreToolUse Bash | Block force-push, `--no-verify`, host installs, `host_forbidden`, `command_deny`; heredoc bodies are text. |
| `guards/powershell_command.py` | PreToolUse PowerShell | Same policy for PowerShell, plus recursive-delete allowlist; `git commit` routed to Bash. |
| `guards/process_kill.py` | PreToolUse Bash/PowerShell | Deny raw kills; rewrite `kill_owned.py` calls with caller worktree, start time and a token (`updatedInput`). |
| `guards/agent_source_read.py` | PreToolUse Bash/PowerShell | Slot agents read source through the code graph, not `cat`/`sed`. |
| `guards/auto_dev_keep.py` | PreToolUse Skill/Bash/PowerShell, UserPromptExpansion | In an auto-dev session, deny removing a halted story's worktree, branch or volumes. |
| `guards/clear_offer.py` | PreToolUse Agent/Skill/Bash/PowerShell | Hold a phase dispatch once after a heavy checkpoint until `/clear` is offered. |
| `guards/dispatch_prompt.py` | PreToolUse Agent/task | Required review preamble; no iterate-and-test phrasing. |
| `guards/phase_dispatch.py` | PreToolUse Agent/Skill | Pipeline phase gate (spec approved → impl; merge approved + pre-merge + land lease → land-story); fails closed for gated dispatches. |
| `guards/agent_dispatch.py` | PreToolUse Agent | Nested-dispatch allowlist (`config: guards.dispatch_edges`) and story-impl effort escalation. |
| `guards/story_write_paths.py` | PreToolUse Write/Edit/shell | Story subagents write only inside their own worktree. |
| `guards/propagate_paths.py` | PreToolUse Write/Edit/shell | story-propagate writes only the files its dispatch lists. |
| `guards/land_lease_wait.py` | PreToolUse AskUserQuestion, Stop | No owner wait while holding the land lease. |
| `guards/review_run_paths.py` | PreToolUse Read/Grep/Glob | The blind layer reads only its review run's diff files. |
| `guards/review_code_paths.py` | PreToolUse Read/Grep/Glob | Review agents read the story tree, never the main checkout or library source. |
| `guards/repro_paths.py` | PreToolUse Write/Edit/shell | Repro/pbt writers write only untracked repro tests. |
| `guards/mutation_command.py` | PreToolUse Bash/PowerShell | test-adequacy runs only the mutation runner. |
| `guards/playwright_paths.py` | PreToolUse browser MCP | Browser file outputs stay in the agent's worktree. |
| `guards/review_coverage.py` | PreToolUse StructuredOutput | A review layer reports only after reading every diff line. |
| `guards/graph_query.py` | PreToolUse graph MCP | Deny unindexed projects and main-repo queries from slot agents. |
| `guards/mcp_readonly.py` | PreToolUse DB MCPs | Keep configured database MCP servers read-only. |
| `guards/script_registration.py` | PostToolUse Write | Warn when a new project script is unregistered. |
| `guards/_guardlib.py`, `guards/_paths.py`, `guards/_transcript.py` | — | Internal helpers (rules, path checks, subagent transcript lookup). |
| `session/session_start.py` | SessionStart startup/clear/compact/resume | Combined injector: pending proposals, story ledger, navigation directives. |
| `session/session_journal.py` | Stop / SubagentStart / SubagentStop / `session.idle` | Per-turn journal and subagent liveness facts (no LLM). |
| `session/session_retrospective.py` | PreCompact / compacting | Mine the journal; launches the background analyst (outcome in `retro-launches.jsonl`). |
| `session/agent_outcome.py` | Agent Stop (`--role impl\|finalize\|diagnose\|propagate`), SubagentStop (`--role repro`) | Block a completion claim the worktree contradicts. |
| `session/context_cap.py` | Agent PostToolUse (`--role impl\|diagnose`) | Soft context cap: tell the agent to hand off. |
| `session/inject_path_rules.py` | SubagentStart (story agents) | Inject `config: path_rules.names` rule files into slot-worktree agents. |
| `session/notification.py` | Notification permission_prompt/idle_prompt | Attention log. |
| `session/instructions_loaded.py` | InstructionsLoaded | Instruction-load log. |
| `session/check_pending_proposals.py`, `session/inject_story_ledger.py`, `session/inject_symbol_nav_directive.py`, `session/_curation_state.py` | via `session_start.py` | Injector parts and curation state. |
