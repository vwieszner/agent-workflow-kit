# agent-workflow-kit

A portable, spec-driven, human-gated development workflow for coding agents. It runs under
**OpenCode** and **Claude Code** from one source tree.

| Part | What you get |
|---|---|
| Story pipeline | `parallel-dev-wave` → `land-story`: one isolated slot per story (a git worktree, plus an isolated compose stack if you want one), tests-first implementation, layered review, verified findings, owner triage, a close-out commit, `story-rebase` for moving a branch onto a newer base, `story-diagnose` for root-causing a failing pre-merge gate, owner-approved merge, post-merge smoke, cleanup. |
| Unattended loop | `auto-dev-loop`: the same pipeline without human checkpoints. The next story comes from `milestone-blockers`. Every operation it is pre-authorized for is listed explicitly. |
| Review | `layered-review` with the `blind-hunter`, `edge-case-hunter` and `acceptance-auditor` layers; `findings-verifier` for adversarial verification; `findings-evaluator` (auto-dev-loop only). |
| Guards | Hooks that block force-push, `--no-verify`, host installs and configured forbidden commands. They also enforce the review preamble and the no-iterate dispatch phrasing, the phase gates (spec approved → impl; merge approved → land), graph-query hygiene and read-only database MCPs. |
| Learning loop | A per-turn session journal feeds `retro`, which writes proposals; `curate` promotes them with owner approval into skills, agents, rules, scripts or memory. |
| Debugging | `flaky-test-hunter`, `hang-diagnoser`, `trace-port-traffic`. |
| Working rules | `AGENTS.md` plus `rules/`: never guess, root cause before fix, no quality reduction, surgical changes, source-of-truth discipline, verification evidence, and the rest. |
| Vendored BMad | `create-story`, `advanced-elicitation` (MIT — see `vendor/bmad/NOTICE.md`). |

`DESIGN.md` is the contract every file follows: layout, config, agent format, tool mapping and hook protocol.

## Requirements

- git, and Python ≥ 3.11 on the host. Scripts use only the standard library. `uv` is the
  documented runner; `python3` works too. `psutil` is optional: it improves watcher-process
  detection and is required for that on Windows.
- Optional: Docker Compose (for isolated per-slot stacks), a code-graph MCP
  (`codebase-memory-mcp`), and a library-docs MCP (`context7`).

## Install

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
| `.workflow/state/` | — | git-ignored runtime state (journal, proposals, handoff, locks) |
| `.claude/skills/` (claude, both) or `.opencode/skills/` (opencode) | kit | OpenCode also reads `.claude/skills/`, so `both` installs skills once |
| `.claude/agents/`, `.opencode/agents/` | kit | rendered from `agents/*.md` with the models set in your config |
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
   for `milestone-blockers` / `auto-dev-loop`.

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
| "what's blocking release-1?" | `milestone-blockers` |
| "run the autonomous loop" | `auto-dev-loop` |
| "review this diff" | `layered-review` |
| `/retro`, then `/curate` | the learning loop |

To resume a story after `/clear`, run `uv run --no-project .workflow/scripts/story_ledger.py <story-id>`.

## Tests

```bash
python3 -m unittest discover -s agent-workflow-kit/scripts/tests -p 'test_*.py'
python3 -m unittest discover -s agent-workflow-kit/hooks/tests -p 'test_*.py'
```

## Known limitations

- **OpenCode plugin is not type-checked or run.** It was written against the OpenCode `dev`
  plugin/SDK sources (hook names and payload shapes are cited in its header), but neither Bun nor
  tsc has compiled it. Session-context injection uses the `experimental.chat.system.transform`
  hook, and compaction uses `experimental.session.compacting`; both are experimental APIs.
- **OpenCode's skill tool carries no arguments**, so the phase guard cannot see the story id when
  `land-story` is invoked. The skill checks the merge approval itself
  (`story_record.py check <id> checkpoint-3`) as its first step.
- **OpenCode exposes every shell as `bash`.** Set `WORKFLOW_OPENCODE_SHELL=powershell` to apply the
  PowerShell guard instead.
- **OpenCode journaling** captures tool calls and user text from plugin events. It does not
  capture interrupts.
- **OpenCode has no per-dispatch model override**, so `models.fallback.opencode.deep` only applies
  where an adapter supports it; the retry otherwise runs on the agent's configured model.
- `trace_port.py` and the Docker paths of the lifecycle scripts have not been run against a live
  stack. They were tested with `stack.runtime = "none"` and a fake compose CLI, on macOS without
  psutil.
- The default `opencode.json` permissions allow `bash *` (with push/force/no-verify denied or
  asked) to match an auto-approve working style. Tighten them in your `opencode.json`; the
  installer never overwrites keys you already have.
