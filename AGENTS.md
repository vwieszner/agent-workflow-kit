# Working Rules (agent-workflow-kit)

These rules apply to every agent session and every subagent in this project. They are the short
form; each "Full rule" line points to the complete rule in `.workflow/rules/` (index:
`.workflow/rules/INDEX.md`). Project-specific rules live in `.workflow/AGENTS.local.md` and
`.workflow/rules/local/` (index: `.workflow/rules/local/INDEX.md`); they add to these rules and
win where they are more specific. This file and `.workflow/rules/*.md` are kit-managed — never
edit them in a project; put project rules in the local files.

- **Project values live in `.workflow/config.toml`.** Prose writes them as `config: <section>.<key>`.
  Read a value with `uv run --no-project .workflow/scripts/wfconfig.py get <section.key>`. Never
  hard-code or guess a value the config holds.
- The human who approves checkpoints is **the owner** (`config: project.owner`).
- `{compose}` in commands stands for `config: stack.compose_cmd`; `<stack>` for a compose project name
  (`config: slots.shared_stack_name`, or `config: slots.stack_prefix` + story id).
- Read the lessons file (`config: paths.lessons`) at session start.

## Tool vocabulary

Skills, agents and rules in this kit use three tool-neutral phrases:

| Phrase | Claude Code | OpenCode |
|---|---|---|
| "dispatch the `<name>` subagent" | Agent tool with `subagent_type: <name>` | `task` tool with agent `<name>` |
| "invoke the `<name>` skill" | Skill tool, or `/<name>` | `skill` tool with `<name>` |
| "ask the owner" | `AskUserQuestion` | `question` tool |

With no question tool available, "ask the owner" means: ask in plain text and stop.

---

## Communicating with the owner

**Never guess — ask when uncertain (HARD RULE).** If you do not know what the owner is asking, or are
not confident in your reading, ask one direct question. Applies to ambiguous instructions ("check X",
"fix that"), decisions without enough information, and every "I think you mean…" moment. Forbidden: acting on a guess and reporting it
as correct; offering a guess plus "let me know if I misunderstood" and proceeding; grepping or
re-reading docs to resolve ambiguity in the owner's instruction instead of asking; guessing twice in a
row after being wrong.

**Re-confirm terse replies to multi-option prompts (HARD RULE).** When your previous message offered
several options (a question tool call, numbered list, A/B/C, any "pick one") and the reply is too
short to identify one ("do", "go", "yes", a single letter/number, an emoji), restate the options in
one line and ask "did you mean option X?". Never pick by recency or prominence. Does not apply to a
terse reply to a single yes/no question.

**Batch questions.** Several pending questions go in one round-trip: one ask-the-owner call with up to
4 questions, or one message listing them all. A question that gates everything else stands alone.
Applies to skills and subagents too.

**End every reply with the answer or the blocking question (HARD RULE).** The final lines are
exactly one of: (1) a 1–3 sentence direct answer to what the owner asked, restated plainly (one line
per question if several); or (2) the blocking question with its options on one line, and nothing
after it. If both apply: answer, then question. Forbidden: ending on analysis, caveats, background, or
a next-steps list; burying the answer or the ask mid-paragraph; several diffuse questions when one
decision is the blocker.

**Answer before action.** A question is answered directly before any action or code.
Full rule: `.workflow/rules/answer-before-action.md`.

**State the goal and reasoning first (HARD RULE).** Before any consequential action (file
write/delete, git state change, migration/schema edit, stack or container operation, anything
destructive or outward-facing) and before any recommendation, verdict, or conclusion: state the goal
and the evidence (file + line read, command output, rule cited) in the same message, ahead of the
output; name rejected alternatives. Read-only exploration needs no preamble.
Full rule: `.workflow/rules/goal-and-reasoning.md`.

## Judgment

**Be critical; protect the project (HARD RULE).** Loyalty is to the project's correctness and
long-term health, not to agreement. When a directive (from anyone, including the owner and your own
earlier output) is wrong, ambiguous, contradictory, short-sighted, or risky, stop and challenge it
with concrete reasoning before executing. Challenge on:
- **Cascading scope impact** — a descope/rename/change that affects other stories, specs, or the plan:
  surface the cascade before executing.
- **Contradiction with a prior decision** — an epic, the quality bar (`config: paths.quality_bar_doc`),
  sprint status, the active plan, or an earlier owner call: ask which is authoritative.
- **Over-engineering smells**, **architecture-rule violations**, **quality-bar risks**,
  **tech-debt shortcuts** — name the rule or doc and offer the clean alternative.

Be direct, cite sources, offer alternatives; then accept the owner's override once they decided with
full information. Defer on product taste, aesthetics, naming, UX preference, and scope/timeline
preferences with no architectural cost. Not contrarianism, not slow-walking, not refusal.

**No quality reduction for convenience (HARD RULE).** Scope, coverage and correctness are set by the
requirement. Before any recommendation ask: does this reduce the quality of the product, the
functionality, or the code? If yes, it is a descope proposal — surface it with the cost named; the
owner decides. Never propose removing a rule, test, or guard because it is not being followed —
non-enforcement means it was never wired up. Full rule: `.workflow/rules/no-quality-reduction.md`.

**Root cause before fix (HARD RULE).** No fix until the mechanism is demonstrated: *this input → this
code path → this wrong state → this observed failure*. No unproved fix, no test-weakening, no silent
deferral, no symptom suppression. Mitigations ship only when labelled in the code comment and commit
message, with the investigation written down and the owner's agreement. If you cannot prove it, say so
and ask. Full rule: `.workflow/rules/root-cause-before-fix.md`.

**Flag over-engineering smells before implementing.** AC compliance is default-yes, but when an AC or
task smells over-engineered for the project's stage, stop and ask: "AC<N> asks for X. Smells over-engineered because <reason>. Proceed
as specified, drop, or reduce to <minimal alternative>?" Smells: a static lint duplicating a runtime
guard; feature flags/toggles with no current need; an abstraction (base class, factory, strategy) with
one implementation; retry/circuit-breaker/timeout on a local call that has never failed;
observability for a feature with no users; deprecation paths, `_v2` renames or dual-read fallbacks
before production; a shared helper with one caller; "for future extensibility"; defensive checks for
cases the type system or call boundary already rules out. Does NOT apply to tests, real security
boundaries, or tenant/data-isolation enforcement at the storage boundary.

**Check the ecosystem before implementing (HARD RULE).** Before implementing any infra-class or
general-purpose mechanism (fork safety, pooling, retries, presence, caching, rate limiting,
serialization, validation), check what installed libraries and the framework already provide (library
docs, a docs MCP if configured) and implement only the missing delta. Hand-rolling functionality a
dependency already ships is a defect, not a style choice. Applies at spec time too: an AC must not
specify a hand-rolled version of shipped functionality.

**Surgical changes (HARD RULE).** Every changed line traces to the request. No drive-by edits to
adjacent code, comments, formatting, or imports; match the file's style. Clean orphans your change
creates (unused imports/helpers, tests of removed behaviour, broken call-sites); leave pre-existing
ones (unused code, commented-out blocks, TODOs, "removed in vN" markers, inconsistent style) and
mention them in your report. Test before saving: can each changed line point to the request or an
AC? If not, revert it. "It was already wrong", "I was already there" and "it's a quick fix" are not
justifications. Exceptions: explicit cleanup/refactor tasks, mechanical renames/moves,
formatter output.

## Truthfulness

**Never invent values from unread files (HARD RULE).** A constant, enum value, range, required-field
list, or any value defined elsewhere is written only after reading its definition — never guessed,
inferred, or filled in with "what makes sense". If a read fails, retry once with a different path or
offset; if it still fails, stop, tell the owner which file you need and why, and wait. Never proceed
with an invented value. **Review findings:** when a finding's grounds are "X is missing" / "Y doesn't handle Z",
read the cited code before accepting the premise or proposing a redesign.

**Read full doc files (HARD RULE).** Before any conclusion about a documentation, spec, planning or
reference file (gaps, consistency, "covers X", a check or audit verdict), read the entire file, paging
with offset/limit. Targeted search is fine for locating something, never for a verdict. Source code is
navigated by symbol, not read end-to-end. Full rule: `.workflow/rules/read-full-docs.md`.

**No completion claims without fresh evidence (HARD RULE).** If you read part of a file, say so.
"Should work", "probably", "seems to", "I've verified" are banned without evidence; otherwise say
"I have NOT verified this". Check exit codes, not summary lines; treat a subagent report as a claim.
Full rule: `.workflow/rules/verify-ground-truth.md`.

## Source-of-truth discipline (HARD RULE)

Plans, specs, sprint status, epics, audits, and these rules are the record.

1. **Decision propagation.** When the owner makes a product or scope decision, propagate it in the
   same turn through every record it touches: sprint status (`config: paths.sprint_status`; notes are
   markers only, max `config: sprint_status.note_max_chars` chars; edit rows only with
   `.workflow/scripts/sprint_status.py`, never by line number — it archives replaced notes to
   `config: paths.story_history_dir`), the relevant epic (`config: paths.epics_dir`), in-flight specs
   (`config: paths.specs_dir`), audits (`config: paths.planning_dir`), the active plan
   (`config: paths.active_plan`), `.workflow/AGENTS.local.md` / `.workflow/rules/local/` if
   project-wide, memory if a recurring class of decision. Grep for stale references, update each, and
   tell the owner what was updated — never just acknowledge in chat.
   Make all of one decision's edits in a single pass — one write per file, or all the edits issued
   together in one message — never one edit per turn.
2. **Ambiguity/contradiction.** If a doc is ambiguous or contradicts another, stop, ask the owner,
   and propagate the answer — never paper over it with a guess. Subagents surface divergences to the orchestrator; they never auto-resolve.
3. **In-flight spec changes.** (a) When a story's spec changes during implementation, capture it in the
   spec in the same turn: driver → alternatives considered → choice + reasoning → fix applied →
   sources; edit the AC text itself. (b) Before merge, invoke the `propagate-story-changes` skill;
   cascading scope impact is flagged before the merge, not after.

## Artifacts hold operative content only (HARD RULE)

Everything written into skills, agents, `.workflow/rules/`, `.workflow/rules/local/`, `AGENTS.md`,
`.workflow/AGENTS.local.md`, or `CLAUDE.md` must instruct:
the rule, its trigger and scope, the forbidden patterns, the concrete how. No origin stories, incident
retellings, self-criticism, or justification prose — those go to memory or an audit doc, unreferenced.
Reference lines only when they point to more operative content. Examples only as minimal ✅/❌
pairs. Before saving, delete every sentence that does not change behaviour. Applies to `curate`
promotions.

## Environment

**App commands run in the container** (when `config: env.app_runs_in_container`). The app's
runtime, package managers, and test runners run only via `{compose} -p <stack> exec -T <service>`;
never on the host, never as a fallback when the container fails — fix the container. Host runs
tooling only: `config: env.host_tooling_runner` for stdlib/PEP 723 scripts and hooks,
`config: env.project_python` for scripts importing project code. Bare `python`/`python3` is
forbidden on the host except for kit scripts and other stdlib-only tooling. Guard-enforced: the
bare-python rule is built in while `config: env.app_runs_in_container` is true, plus
`config: guards.host_forbidden`. Full rule: `.workflow/rules/host-passive-environment.md`.
Dependencies: `.workflow/rules/dependency-sync.md`.

**Read source locally.** Inspect code in the worktree the stack runs from, not through `exec`. Use
`exec` only for runtime state (logs, health, processes) or to diagnose suspected sync drift.

**Live sync** (when `config: stack.runtime` is not `"none"` and `config: stack.watch` is true).
`compose watch` only; never add source volume mounts. Verify sync before trusting a
result. Full rule: `.workflow/rules/live-sync.md`.

**Code freeze — per stack.** Never modify files in a worktree while that worktree's stack is running a
suite. Other slots' worktrees are unaffected.

**Preflight before testing any stack.** Run
`uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack> --worktree <worktree-root>`
before scoped tests. Pass the worktree root (the main checkout for the shared slot), never its
`config: stack.health.worktree_src` subfolder — the script appends it. On any FAIL line, surface it; for deeper diagnosis dispatch the `dev-preflight`
subagent (report-only; it never runs compose commands). The script is the gate, never the agent.
`run_full_suite.py` runs preflight itself.

**Container health-check.** Before any `exec` in a verify pass, confirm the service is `Up`
(`{compose} -p <stack> ps -a <service>`). If `Exited`/`Restarting`, fetch
`{compose} -p <stack> logs --tail=15 <service>` and surface that first. Healthy-looking stacks can lie:
`.workflow/rules/container-truthfulness.md`.

**Polling heartbeat.** Every wait loop emits a timestamped line per iteration and has an explicit
timeout: `until <check>; do echo "[$(date +%H:%M:%S)] still waiting on <thing>..."; sleep N; done`.
If a tool failure is suspected, surface diagnosis options within 30 seconds.

**Code-symbol navigation (HARD RULE when `config: graph.tool` is not `"none"`).** Use the code-graph
MCP (`config: graph.mcp_server`) for symbol lookup, call chains, change impact, and exact symbol
source — not grep/read scanning. Grep/read are for full-text search, non-code files, and regions
already located. If a project is not indexed yet, index it first. Always read a file before
editing it. **In a story slot, query only the slot's graph project** (from
`.workflow/state/story-setup/<stack>.graph_project`), never `config: graph.main_project`. Enforced by
`.workflow/hooks/guards/graph_query.py`. With `graph.tool = "none"`, navigate with grep/read.

## Testing

- **Discipline and entry points:** `.workflow/rules/test-guide.md` — scoped runs via
  `.workflow/scripts/run_scoped_tests.py`, the full gate via `.workflow/scripts/run_full_suite.py`,
  required flags (`config: tests_policy.required_flags`), no real/paid LLM in tests (use the mock
  provider).
- **Real-signature tests at infra boundaries (HARD RULE).** Code talking to a real dependency has a
  live-dependency test or a contract-faithful fake; a bare auto-mock for that boundary is forbidden.
  Full rule: `.workflow/rules/real-signature-tests.md`.
- **Parallel test isolation (HARD RULE).** No import-time connections, no connection-holding module
  singletons, no cross-test module state; shared live infra keyed on per-test tokens; per-worker E2E
  shards. Full rule: `.workflow/rules/parallel-test-isolation.md`.
- **Pre-merge full gate.** Scoped green cannot catch parallel-isolation regressions. Run the full gate
  before merging when the change touches shared infra, adds a connection-holding singleton, touches
  seed/E2E worker plumbing, or ends a wave. When in doubt, run it. A failing leg on a story branch
  goes to the `story-diagnose` subagent, never to `story-finalize` or `story-impl`.
- **Batch-fix iteration.** Capture the failure list once, fix all, validate once per round; ask the
  owner when a failure needs judgment. Dispatch prompts must not ask for per-fix test runs
  (guard: `.workflow/hooks/guards/dispatch_prompt.py`). When several agents work on different stories
  in parallel, each runs its own tests as soon as it is ready — none waits for its siblings.
- **Live-infra tests run in the full gate's single parallel pass**; never exclude them by tag or
  marker. E2E runs have runner retries disabled — a retry hides the failure.
- **E2E and browser work:** `.workflow/rules/e2e-debugging-protocol.md`,
  `.workflow/rules/browser-interaction.md`.

## Story pipeline

**Mandatory lifecycle.** Implement stories through the `parallel-dev-wave` skill: slot setup → spec
approval → implementation → `layered-review` → owner triage → finalize → `land-story`. Several stories
may run at once in separate slots; only the merge step is one-at-a-time.
Forbidden: bringing up a stack without every port env var in `config: stack.ports`; auto-applying
review patches; merging without the owner's explicit confirmation; skipping the slot registry update
(`config: paths.slot_registry`); skipping branch/worktree/stack cleanup.

**Isolated slot per story.** Slots `1..config: slots.count`; slot `config: slots.shared_slot` is the
shared dev stack and is never assigned.

**Post-merge regression gate.** After every merge, invoke the `post-merge-smoke` skill. Every service
in `config: smoke.services` must be running with no fresh errors. Pre-existing baseline failures are
not exempt.

**Commit convention.** Impl commit: `config: git.impl_commit_template` (one per story, code + tests;
type `feat`, `fix`, `docs`, `refactor` or `test`).
Close-out commit: `config: git.closeout_commit_template` (zero if every finding was deferred or
dismissed, one if any patch was applied). At most `config: git.max_commits_per_story` commits per
story branch; never batch commits across stories. Mark task checkboxes before committing:
`.workflow/rules/story-checkbox-enforcement.md`.

**Definition of Done.** A story is done only when:
1. Code and tests are written in the story's worktree.
2. The `layered-review` skill has run and the owner has triaged its findings.
3. Approved patches are applied and stack logs are clean.
4. Tests pass in the story's isolated slot (in its container when the stack is enabled).
5. The impl commit (and close-out commit if patches were applied) has landed.
6. The branch is merged `config: git.merge_style` into `config: git.base_branch` with the owner's
   explicit confirmation.
7. Sprint status shows `done`.
8. Branch, worktree and stack are removed and the slot registry row is reset to `free`.

**Story size.** One user-visible capability per story, at most `config: pipeline.max_behaviour_acs`
behaviour ACs (0 = no cap; tokens/a11y, tests, flag flips and ownership notes do not count), sliced
vertically — UI, API, data and tests together. A separate backend story only when one backend change
feeds several stories. A spec that needs more is split before it is written; the split goes to the
owner as an open question.

**Structured LLM output** (when the project calls LLMs). Parse it through a declared schema and a
structured-output parser; never with regex or string matching.

**Pre-production schema** (when `config: schema.preprod`): no new migrations, no backward-compat
tests, reset stale databases after merge. Full rule: `.workflow/rules/preprod-schema.md`.

## Memory and curation

- **Memory** holds: an owner decision not yet in any record that may recur; a recurring pattern; an
  owner remark that changes how future work is approached. It does NOT hold what belongs in sprint
  status, the lessons file, an epic, or a spec, nor anything inferred without the owner confirming,
  nor everyday execution detail.
- **Scripts and learned artifacts.** Reusable host scripts are saved with a doc header and registered
  in `config: paths.scripts_index`. Recurring tasks and mistakes become proposals via the `retro`
  skill; only the `curate` skill, with the owner's per-item approval, moves a proposal into a skill,
  agent, rule, script, or memory. Nothing auto-writes into those locations.
  Full rule: `.workflow/rules/script-artifact-curation.md`.
