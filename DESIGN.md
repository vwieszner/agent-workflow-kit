# agent-workflow-kit — design contract

Every file in this kit follows this contract. It is the single source for layout, install
paths, config keys, agent frontmatter, the tool vocabulary, and the hook protocol.

## 1. What the kit is

A portable, project-agnostic packaging of a spec-driven, human-gated agentic development
workflow:

- **Story pipeline** — spec → isolated slot (git worktree + optional isolated service stack)
  → tests-first implementation → layered adversarial code review → verification of findings
  → human triage → patch close-out → human-approved merge → post-merge smoke → cleanup.
- **Review** — parallel blind / edge-case / acceptance review layers, adversarial findings
  verification, autonomous findings evaluation (for the unattended loop only).
- **Guards** — hooks that block unsafe commands, enforce dispatch-prompt discipline, and
  enforce pipeline phase ordering.
- **Learning loop** — session journal → retrospective proposals → human-approved curation
  into skills / agents / rules / scripts / memory.
- **Debug skills** — flaky-test root-causing, hang diagnosis, mystery-port-traffic tracing.
- **Working rules** — `AGENTS.md` + `rules/`.

It runs under **OpenCode** and **Claude Code** from one source tree.

## 2. Kit layout (source)

```
agent-workflow-kit/
  README.md                     install + usage
  DESIGN.md                     this contract
  AGENTS.md                     working rules (always-loaded instructions)
  rules/*.md, rules/INDEX.md    detailed rules referenced from AGENTS.md
  workflow.config.example.toml  every project-specific value lives here
  skills/<name>/SKILL.md        shared Agent-Skills format (+ workflow.md, steps/, helpers)
  agents/<name>.md              tool-neutral agent source (see §5); install renders per tool
  hooks/guards/*.py             PreToolUse/PostToolUse guard scripts (Claude hook protocol, §7)
  hooks/session/*.py            session lifecycle hooks (journal, retrospective, injectors)
  scripts/*.py                  pipeline helper scripts (stdlib-only, read the config)
  scripts/wfconfig.py           the ONLY config loader — every script/hook imports it
  scripts/INDEX.md              registry of the kit scripts
  vendor/bmad/                  vendored BMad pieces the pipeline calls, packaged as skills
  adapters/opencode/            plugin (.ts) + opencode.json fragment
  adapters/claude/              settings.json hooks fragment
  install.py                    installs the kit into a target project
```

## 3. Installed layout (in a target project)

`install.py --target <repo> --tool opencode|claude|both` produces:

| Kit source | Installed at |
|---|---|
| `scripts/`, `hooks/` | `.workflow/scripts/`, `.workflow/hooks/` |
| `workflow.config.example.toml` | `.workflow/config.toml` (only if absent) |
| runtime state (journal, proposals, handoff, story-setup, locks) | `.workflow/state/` (git-ignored) |
| `rules/` | `.workflow/rules/` |
| `AGENTS.md` | `.workflow/AGENTS.md`, listed in `opencode.json` `instructions` and/or `@`-imported from `CLAUDE.md` |
| — (project-owned, created once, never overwritten) | `.workflow/AGENTS.local.md`, `.workflow/rules/local/INDEX.md` — `curate` promotes rules here |
| `skills/<n>/`, `vendor/bmad/skills/<n>/` | `.claude/skills/<n>/` for `claude`/`both` (OpenCode reads it too), `.opencode/skills/<n>/` for `opencode` |
| `agents/<n>.md` | rendered to `.opencode/agents/<n>.md` and/or `.claude/agents/<n>.md` |
| `adapters/opencode/plugins/*.ts` | `.opencode/plugins/` |
| `adapters/claude/settings.hooks.json` | merged into `.claude/settings.json` |

**Every path written in skill/agent/rule prose uses the INSTALLED path**:
`.workflow/scripts/story_setup.py`, `.workflow/config.toml`, `.workflow/rules/<x>.md`,
`.workflow/state/...`. Never a kit-source path, never an absolute path.

Scripts are invoked as `uv run --no-project .workflow/scripts/<name>.py ...` (Python ≥ 3.11,
stdlib only — `tomllib` for config). Scripts must also run as `python3 <path>`; `uv` is the
documented default, not a requirement.

## 4. Configuration — `.workflow/config.toml`

All project-specific values come from the config. Prose refers to them as
`config: <section>.<key>` (e.g. "the base branch (`config: git.base_branch`)").
Scripts read them via `wfconfig`. The schema is `workflow.config.example.toml`; every key
is documented there with its default. Adding a key: add it to the example file with a
comment and a default, and use `wfconfig.get("section.key", default)`.

Core sections (see the example file for the full list):
`project` (name, owner), `git` (base/main branch, branch prefix, worktrees root, commit
conventions), `paths` (specs, sprint status, slot registry, deferred work, epics, lessons,
quality-bar doc, scripts index), `slots` (count, shared slot, stack name prefix),
`stack` (isolated-environment runtime, compose file, required services, port env vars,
health checks, watch), `tests` (scoped / full / lint / unit / e2e command templates),
`smoke`, `review` (round-2 thresholds), `models` (tier → model per tool), `graph` (optional
code-graph MCP), `guards` (per-guard enable + pattern lists), `curation`.

Command templates use `{placeholders}` filled by `wfconfig.render()`:
`{repo}`, `{worktree}`, `{stack}`, `{slot}`, `{story_id}`, `{branch}`, `{labels}`,
`{service}`, plus every port env var name (`{BACKEND_PORT}`...).

**Optional capabilities degrade explicitly.** `stack.runtime = "none"` → no isolated
stack: slots are worktrees only, stack steps are skipped and say so. `graph.tool = "none"` →
graph steps skipped, code navigation falls back to grep/read. `paths.quality_bar_doc = ""`
→ the release-bar gate in triage is skipped. A skipped step always prints/states that it
was skipped and why — never a silent pass.

## 5. Agent source format (`agents/<name>.md`)

```markdown
---
name: story-impl
description: <one paragraph; when to dispatch; "do not invoke directly" if pipeline-only>
mode: subagent            # subagent | primary | all
tier: standard            # deep | standard | fast  → config: models.<tool>.<tier>
effort: high              # Claude Code only (low|medium|high|xhigh|max); ignored by OpenCode
tools: [read, grep, glob, edit, write, bash, skill, graph]
readonly: false           # true → edit/write stripped (OpenCode edit: deny); see note below
---
<body — tool-neutral prose>
```

Capability vocabulary for `tools` and its rendering:

| capability | Claude Code tool(s) | OpenCode (absent capability → deny/false) |
|---|---|---|
| read / grep / glob | Read / Grep / Glob | `permission.read` / `.grep` / `.glob` (+ `list`) |
| edit / write | Edit / Write | `permission.edit: allow` |
| bash | Bash (+ PowerShell) | `permission.bash: allow` |
| task | Agent | `permission.task: allow` |
| skill | Skill | `permission.skill: allow` |
| web | WebFetch, WebSearch | `permission.webfetch` / `.websearch: allow` |
| graph | `mcp__<config: graph.mcp_server>` + ToolSearch | `tools: {"<server>*": true}` (legacy tools map; the only MCP gate) |
| docs | `mcp__<config: docs.mcp_server>` + ToolSearch | `tools: {"<server>*": true}` |
| monitor | Monitor, TaskStop, SendMessage | (no equivalent — omitted) |

`readonly: true` removes the edit/write capabilities at render time. It does not restrict `bash`:
an agent that is readonly and keeps `bash` (e.g. `dev-preflight`, `story-diagnose`) is held to
no-mutation by its own prose — its hard rules forbid lifecycle and git-state commands.

Optional per-tool overrides in the source frontmatter: `claude: {permissionMode: auto}` and
`opencode: {temperature: 0.1}` are passed through verbatim.
`install.py render-agent <file> --tool claude|opencode` performs the rendering.

## 6. Tool-neutral prose conventions

Skills, agents, rules and AGENTS.md are written once for both tools:

- "dispatch the `<name>` subagent" — Claude Code: Agent tool with `subagent_type`; OpenCode:
  task tool with that agent. AGENTS.md carries this mapping table once; files don't repeat it.
- "invoke the `<name>` skill" — Claude Code: Skill tool / `/<name>`; OpenCode: `skill` tool.
- "ask the owner" — Claude Code `AskUserQuestion`; OpenCode `question` tool; else plain text.
- The human is **the owner** (`config: project.owner` holds a display name). Never a real name.
- Shell examples are POSIX `bash`; Windows specifics appear only where behaviour genuinely
  differs, in a clearly marked "Windows" note.
- Model names are never hard-coded in prose: say "tier `deep`" etc.

## 7. Hook protocol

All hook logic is Python, written against the **Claude Code hook protocol**: JSON on stdin
(`tool_name`, `tool_input`, `hook_event_name`, `session_id`, `cwd`, `transcript_path`,
`agent_type`...). Deny = exit code 2 with the reason on stderr, or for MCP tools a JSON
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
"permissionDecisionReason": "..."}}` on stdout. Session injectors print context to stdout.

The OpenCode plugin (`adapters/opencode/plugins/workflow-hooks.ts`) adapts OpenCode events
to that protocol: it builds the Claude-shaped payload (tool name mapping `bash→Bash`,
`task→Agent`, `skill→Skill`, `write→Write`, `edit→Edit`, `<server>_<tool>→mcp__<server>__<tool>`),
spawns the same Python script, and throws an `Error(reason)` on a deny. Session hooks map to
`event` (`session.created`, `session.idle`, `session.compacted`) and
`experimental.session.compacting`. Where OpenCode has no transcript file, the plugin writes
the journal entries itself in the journal format (§8).

Guards are **fail-open on internal error** (a crashing guard must not brick the session) and
**fail-closed on a matched rule**.

## 8. State formats

- Journal: `.workflow/state/journal/<session_id>.jsonl`, one JSON object per turn
  (`ts`, `session_id`, `tool_calls[]` with `tool`, short `input` summary, `ok`; plus `facts[]`
  derived from the calls, which the retrospective mines). Subagent turns go to
  `<session_id>.sub-<agent>.jsonl`.
- Proposals: `.workflow/state/proposals/<kind>-<slug>.md` (kind ∈ skill|agent|rule|script|memory),
  with the draft beside it as `<kind>-<slug>.draft.<ext>`.
- Handoff: `.workflow/state/handoff.md`.
- Story setup: `.workflow/state/story-setup/<stack>.{json,log,graph_project}`.
- Locks: `.workflow/state/locks/`.
- Slot registry / sprint status / specs: tracked docs at `config: paths.*` (committed).

## 9. Generalization rules (how sources were ported)

1. Remove every project-domain fact (product names, services, frameworks, ports, tables,
   people, dates, story ids, incident retellings). Replace with a config reference or a
   generic phrasing.
2. Keep every *mechanism* (gates, checkpoints, invariants, forbidden lists, exit-code
   contracts). A generalization never weakens a rule — if a rule was specific to one stack,
   generalize its trigger, do not drop it.
3. Operative content only: the rule, trigger, scope, forbidden patterns, the concrete how.
   No origin stories, "added after incident X", or justification narratives.
4. Cross-platform: POSIX first; Windows-only mechanisms (PEB CWD readout, named mutex via
   ctypes, PowerShell) become cross-platform (psutil when available / `fcntl`/`msvcrt`
   file locks) with the OS difference isolated in one function.
5. Kit-internal cross-references use installed paths (§3).
