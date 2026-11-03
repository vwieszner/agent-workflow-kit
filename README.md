# agent-workflow-kit

A portable, spec-driven, human-gated development workflow for coding agents. It runs under
**OpenCode** and **Claude Code** from one source tree.

| Part | What you get |
|---|---|
| Story pipeline | `parallel-dev-wave` → `land-story`: one isolated slot per story (a git worktree, plus an isolated compose stack if you want one), tests-first implementation with effort-escalated `story-impl` variants and context-cap hand-offs, layered review, owner triage, a close-out commit, `story-rebase`, `story-diagnose` for a failing pre-merge gate, `story-propagate` (propose before the merge checkpoint, apply after the merge), owner-approved merge in a land window (land lease + serial merge train, the train branch gated while waiting), an optional profile leg (an extra gate leg under another serving profile, due every N merges), post-merge smoke, cleanup. `sprint-next` answers "what's next?". |
| Unattended loop | `auto-dev-loop`: the same pipeline without human checkpoints, run one phase per fresh headless session by `auto_dev_driver.py` (`start` / `list` / `stop`) and watched with `auto-dev-run`. The next story comes from `milestone-blockers` (map refresh via `milestone-map-refresh`). A halt keeps the worktree and stack; `resume_halted.py` resumes it. The control room (`control_room.py`, localhost) shows the board, the attention queue and the driver runs, and starts or stops runs; `auto_dev_metrics.py compare` compares driver configurations. Every operation it is pre-authorized for is listed explicitly. |
| Review | `layered-review`: typed layers (`blind-hunter`, `edge-case-hunter`, `verification-gap`, `acceptance-auditor`), execution stages (`ui-verifier` on a UI profile, `pbt-writer` property tests, `test-adequacy` mutation testing), `findings-verifier` verification (3-vote majority in auto mode), `repro-writer` failing tests, rendered findings files with halted-round carry-over and a close-out loop; `findings-evaluator` (auto-dev-loop only). Runs as a Claude Code Workflow (`workflows/review.js`) or, elsewhere, through `workflow_replay.mjs` (Node ≥ 18). |
| Guards | Hooks that block force-push, `--no-verify`, host installs, configured forbidden commands, raw process kills (`kill_owned.py` with a hook-issued token instead) and PowerShell commits. They also enforce the review preamble and the no-iterate dispatch phrasing, the phase gates (spec approved → impl; merge approved + pre-merge gate + land lease → land), nested-dispatch edges, story agents writing only in their worktree, review path confinement (the blind layer reads only its diff; review agents read the story tree, not the main checkout or library source; repro writers write only untracked repro tests; reports wait until every diff line is read), graph-query hygiene and read-only database MCPs. |
| Learning loop | A per-turn session journal (with subagent liveness facts) feeds `retro` (`retro_digest.py` first), which writes proposals; `curate` checks drift (`proposal_drift.py`) and promotes them with owner approval into skills, agents, rules, scripts or memory. A failed background analyst run is logged to `.workflow/state/retro-launches.jsonl` and reported at the next session start. |
| Design owner | Optional: `design-ticket`, `design-sync`, `ui-design-compare`, `ui-profile`, `ui-seed`; `tracker.py` (Jira REST or a tracker MCP), `docs_mirror.py` (Confluence REST or MCP), `design_library.py` + `design_extract.js` + `design_assets.py` (Figma REST or MCP), `token_doc_check.py`, `design_tokens.py`, `snapshot_update.py`, `ui_gap_owner.py`. Every step says SKIPPED when unconfigured. |
| Harness ops | `harness-review` with `story_state.py` (the read model), `auto_dev_metrics.py`, `harness_metrics.py`, `session_cost_report.py`, `denial_samples.py`, `transcript_calls.py`, `journal_grep.py`, `review_precision.py`. |
| Debugging | `flaky-test-hunter`, `hang-diagnoser`, `trace-port-traffic`, `run-tests`; `proc_scan.py` / `kill_owned.py` (owned process kills), `gate_campaign.py`, `gate_sampler.py`, `host_cpu_load.py`, `pw_failure_summary.py`. |
| Working rules | `AGENTS.md` plus `rules/` and the `rules-*` skills: never guess, root cause before fix, no quality reduction, surgical changes, off-task work, source-of-truth discipline, verification evidence, harness readers, and the rest. |
| Vendored BMad | `create-story`, `advanced-elicitation` (MIT — see `vendor/bmad/NOTICE.md`); the edge-case-hunter and verification-gap review prompts (MIT — `skills/layered-review/review-prompts/NOTICE.md`). |

`DESIGN.md` is the contract every file follows: layout, config, agent format, tool mapping and hook protocol.

## Requirements

- git, and Python ≥ 3.11 on the host. Scripts use only the standard library. `uv` is the
  documented runner; `python3` works too. `psutil` is optional: it improves watcher-process
  detection and is required for that on Windows.
- Optional: Docker Compose (for isolated per-slot stacks), a code-graph MCP
  (`codebase-memory-mcp`), and a library-docs MCP (`context7`).

## Install

Two routes: the **Claude Code plugin** (Claude Code only, nothing copied into the project but its
config), or **`install.py`** (Claude Code and/or OpenCode, everything copied into the project).

### Claude Code plugin

```
/plugin marketplace add vwieszner/agent-workflow-kit
/plugin install agent-workflow-kit@agent-workflow-kit
```

Then, in each project, invoke the `agent-workflow-kit:setup` skill: it creates `.workflow/config.toml`,
the project rules files and the slot registry, and adds the kit's deny rules to `.claude/settings.json`.
Skills, agents and the review workflow are named `agent-workflow-kit:<name>`. The plugin's
SessionStart hook injects the working rules (`AGENTS.md`) and `.workflow/AGENTS.local.md`, since a
plugin cannot load CLAUDE.md. Agents use the example config's models and list the graph, docs and
browser MCP tools; a project that needs other models uses the `install.py` route. Do not combine the
two routes in one project: every guard would run twice (setup warns when it finds both).

The plugin under `plugins/agent-workflow-kit/` is generated: edit the kit, then run
`python3 install.py build-plugin` (`--check` reports drift; the tests fail on it) and
`claude plugin validate ./plugins/agent-workflow-kit`.

### install.py

```bash
python3 agent-workflow-kit/install.py install --target /path/to/project --tool both      # or opencode | claude
python3 agent-workflow-kit/install.py install --target /path/to/project --tool both --dry-run
```

What lands in the project:

| Path | Owner | Notes |
|---|---|---|
| `.workflow/scripts/`, `.workflow/hooks/`, `.workflow/rules/`, `.workflow/AGENTS.md` | kit | refreshed on every install |
| `.workflow/config.toml` | project | created once from `workflow.config.example.toml` |
| `.workflow/AGENTS.local.md`, `.workflow/rules/local/` | project | created once; your project rules; `curate` promotes rules here |
| `.workflow/state/` | — | git-ignored runtime state (journal, proposals, story setup, locks, land lease, auto-dev runs) |
| `.claude/skills/` (claude, both) or `.opencode/skills/` (opencode) | kit | OpenCode also reads `.claude/skills/`, so `both` installs skills once |
| `.claude/agents/`, `.opencode/agents/` | kit | rendered from `agents/*.md` with the models set in your config |
| `.workflow/workflows/`, `.claude/workflows/` (claude, both) | kit | the `review` workflow script |
| `.claude/settings.json` `hooks` | merged | entries are appended; existing hooks are kept |
| `.opencode/plugins/workflow-hooks.ts`, `.opencode/package.json` | kit | the OpenCode adapter for the same Python hooks |
| `opencode.json` | merged | adds `instructions` (AGENTS + local + `.workflow/memory/*.md`) and permission defaults, only where you have none |
| `CLAUDE.md` | appended | `@.workflow/AGENTS.md` and `@.workflow/AGENTS.local.md` imports |

If a skill or agent with the same name already exists, it is **skipped** and reported. Rerun with
`--force` to overwrite it, which is how you pick up kit updates or re-render agents after a
`models.*` change. Commit `.workflow/`; `state/` is ignored.

## Configure

Edit `.workflow/config.toml`. Every key is documented inline. The minimum for a real project:

1. `[project]` `name`, `owner`; `[git]` `base_branch`.
2. `[paths]`: where your specs, sprint status and slot registry live.
3. `[tests]` `scoped_cmd` and `full_cmd`, with `fail_id_regex` and `summary_regex` matching your runner's output.
4. Isolated stacks: `[stack]` `runtime = "docker-compose"`, `services`, `[stack.ports]` (every
   host port in your compose file must read `${VAR:-default}` from one of these vars), and
   `[stack.health]` checks. Without a stack: `runtime = "none"`, which makes slots worktree-only;
   every stack step prints SKIPPED.
5. `[guards]` `host_forbidden` / `command_deny`: until you fill these in, the host-passive rule is
   prose only.
6. Optional: `graph.tool`, `paths.quality_bar_doc` (enables the triage bar gate), `[milestones]`
   for `milestone-blockers` / `auto-dev-loop`, `[review]` UI/mutation/property stages and
   `browser.mcp_server` for the ui-verifier, `[tests] scoped_legs`, `[path_rules]`, `[static_checks]`, `[profiles]` / `[profile_leg]` / `[sampler]` for a gate that switches serving profiles, `[design]` / `[tracker]` / `[docs_mirror]` for a design owner, `[control_room]`.

Then initialize the slot registry and check the config:

```bash
uv run --no-project .workflow/scripts/slot_registry.py init
uv run --no-project .workflow/scripts/wfconfig.py get git.base_branch
uv run --no-project .workflow/scripts/wfconfig.py ports 1
```

## Use

| You say | What runs |
|---|---|
| "implement story 1-2" | `parallel-dev-wave` (3 owner checkpoints: spec, triage, merge) |
| "land story 1-2" (after approving the merge) | `land-story` |
| "what's next?" | `sprint-next` |
| "what's blocking release-1?" | `milestone-blockers` |
| "run the autonomous loop" | `auto-dev-loop` |
| "start a driver run" | `auto-dev-run` |
| "harness review" | `harness-review` |
| "file the design questions for story 1-2" | `design-ticket` |
| "the designer answered" / "sync the design" | `design-sync` |
| open the control room | `uv run --no-project .workflow/scripts/control_room.py` |
| "review this diff" | `layered-review` |
| `/retro`, then `/curate` | the learning loop |

To resume a story after `/clear`, run `uv run --no-project .workflow/scripts/story_ledger.py <story-id>`.

## Tests

```bash
python3 -m unittest discover -s agent-workflow-kit/scripts/tests -p 'test_*.py'
python3 -m unittest discover -s agent-workflow-kit/hooks/tests -p 'test_*.py'
```

## Known limitations

- **The OpenCode plugin is type-checked, not run.** `adapters/opencode/plugins/workflow-hooks.ts`
  compiles under strict `tsc` against `@opencode-ai/plugin` 1.18.33 and its hooks object is checked
  with `satisfies Hooks` (`npm run typecheck` in `adapters/opencode/` after installing its dev
  dependencies). It has not been exercised inside a running OpenCode. Session-context injection
  (`experimental.chat.system.transform`) and compaction (`experimental.session.compacting`) use
  experimental APIs.
- **OpenCode's skill tool carries no arguments.** The plugin passes the session's latest user
  message as the skill args, so the phase guard sees a story id only when that message names it
  ("land story 1-2"). `land-story` also checks the merge approval itself
  (`story_record.py check <id> checkpoint-3`) as its first step.
- **OpenCode exposes every shell as the `bash` tool.** The plugin follows OpenCode's own shell
  choice — config `shell`, else `$SHELL`, else PowerShell on Windows — and applies the PowerShell
  guard for pwsh/powershell and the Bash guard otherwise. `WORKFLOW_OPENCODE_SHELL` overrides it.
  A `cmd` shell gets no shell guard.
- **OpenCode journaling** records tool calls (including `apply_patch` file writes), user
  messages, and interrupts (`session.error` with `MessageAbortedError`). Subagent turns go to
  `<parent>.sub-<agent>.jsonl` only when OpenCode reports the child session's parent.
- **OpenCode has no per-dispatch model override**, so `models.fallback.opencode.deep` only applies
  where an adapter supports it; the retry otherwise runs on the agent's configured model.
- **Process-kill rewrite under OpenCode.** `process_kill.py` adds a token to `kill_owned.py` calls via
  `updatedInput`; the plugin applies it to the shell command. Without that, `kill_owned.py` refuses
  (no kill happens). `kill_owned.py` and `proc_scan.py` need psutil (PEP 723, via `uv`).
- **Claude-only hooks.** The transcript readers and metrics read Claude Code transcripts only.
  Notification, InstructionsLoaded, SubagentStart path-rule injection, the per-agent completion
  checks and context cap, `review_coverage`, `land_lease_wait` and `clear_offer` need Claude Code's
  hook events or transcript files; under OpenCode they are not wired or fail open.
- **The auto-dev driver's OpenCode path** (`loop.driver_tool = "opencode"`) has not run against a live
  OpenCode: no init check, no cost, the phase line read from the whole output. Its Claude path was
  tested with a fake `claude` CLI only. Without psutil the watchdog cannot kill a stuck session's
  descendants.
- **Live services and backends not exercised:** the Jira REST backend (`tracker.py`), the Confluence
  REST fetch (`docs_mirror.py --fetch`, `design_assets.py`), the Figma REST mode and the MCP flows of
  `design_library.py` / `design_page_renders.py` / `design_assets.py` ran only against fakes.
  `docs_mirror.py --fetch` mirrors the REST `storage` body (XHTML), not markdown; a mirror is never
  compared across formats.
- **Serving profiles:** `gate_profile.py` and `gate_sampler.py` were tested with a fake compose/engine;
  the profile switch and the container `/proc` reading have not run against a live stack.
- **Control room:** the launch backends ran only against fakes (no live Windows Terminal, tmux,
  `claude` or `opencode` start). Under OpenCode the read model's `sessions` section reads
  "unavailable" unless `config: story_state.sessions_cmd` names a command printing the documented
  JSON; `opencode --session <id>` resume is untested; running-agent attribution reads Claude Code
  subagent metadata, under OpenCode only the journal's agent facts.
- `trace_port.py` and the Docker paths of the lifecycle scripts have not been run against a live
  stack. They were tested with `stack.runtime = "none"` and a fake compose CLI, on macOS without
  psutil.
- The default `opencode.json` permissions allow `bash *` (with push/force/no-verify denied or
  asked) to match an auto-approve working style. Tighten them in your `opencode.json`; the
  installer never overwrites keys you already have.

## License

MIT — see `LICENSE`. Vendored BMad files keep their own MIT notices (`vendor/bmad/`).
