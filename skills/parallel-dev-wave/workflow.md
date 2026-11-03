# Parallel Dev Wave — Orchestrator

**Role:** This skill orchestrates one story through three phases — a setup script, then two implementation subagents — with three human checkpoints between them. The orchestrator (main session) owns all human interaction. Phase 1 is a deterministic script (its work is purely mechanical); Phases 2–3 each run inside a dedicated subagent with its own focused instructions.

"Parallel" means multiple story slots can be active at once. Within one story the phases run sequentially.

Config keys are cited as `config: <section>.<key>` and live in `.workflow/config.toml`. Read a value with `uv run --no-project .workflow/scripts/wfconfig.py get <key>`.

---

## Pipeline overview

```
[main session]
  │
  ├─ Phase 1: run  .workflow/scripts/story_setup.py   (async — returns immediately)
  │     ↳ slot reservation → ports → worktree → spec lookup → graph indexing (if configured)
  │     ↳ launches  bring_up_stack_async.py  in the background — skipped when stack.runtime = "none"
  │     ↳ writes  .workflow/state/story-setup/<stack>.json  (starting → up | failed) + .graph_project sidecar
  │     ↳ orchestrator then flips sprint status to in-progress
  │
  ├─ Checkpoint 1: SPEC REVIEW (human) — overlaps with the background bring-up
  │     ↳ spec missing → dispatch story-spec (research pack first); co-draft on request
  │     ↳ rulings written with story_record.py rule; approval recorded (refused while an OPEN-Q stands)
  │
  ├─ Pre-Phase 2a: wait_for_stack_ready.py   Pre-Phase 2b: stack_preflight.py (the gate; exit 1 → HALT)
  │
  ├─ Phase 2: story-impl (tests-first / Batch TDD) → records phase-2 GREEN → IMPL COMMIT
  │     ↳ context-cap hand-off → fresh story-impl (effort variant by rounds_used)
  │
  ├─ Post-Phase 2: orchestrator runs layered-review (pipeline mode) on the committed diff
  │     ↳ review_stage.py stages the round → workflow: layers → dedupe → findings-verifier
  │       → repro / execution stages → findings file + `review round <n> recorded`
  │
  ├─ Checkpoint 2: CODE REVIEW TRIAGE (human — HARD RULE)
  │     ↳ main-session call per finding under rules-defer; the owner rules each row
  │     ↳ tracking rows for defers written; decisions persisted before Phase 3
  │
  ├─ Phase 3: story-finalize — apply manifest (adopt repro tests) → logs → scoped tests → CLOSE-OUT COMMIT
  │
  ├─ Round <next> (conditional + human gate) — close-out round → Checkpoint 2b → finalize AMENDS
  │
  ├─ Gate-exemption decision → Propagation (story-propagate, propose) → Checkpoint 3: MERGE APPROVAL
  │
  ├─ Land window (one story at a time across all slots)
  │     ↳ merge_train.py wait (train gated while waiting) → land_lease.py acquire → merge_train.py home
  │       → profile_leg.py due → final rebase (story-rebase, validate: stack) → merge_train.py settle
  │       → pre-merge gate (when needed; plus --profile-leg when due)
  │     ↳ a failing leg → story-diagnose → story-finalize (gate fix) → fresh Checkpoint 3 → new window
  │
  └─ land-story (needs the lease; releases it after its bookkeeping commit)
        ↳ merge → post-merge-smoke → cleanup → propagation apply → sprint-status flip
```

Multiple stories can run concurrently in different slots. Only the land window (the stretch from the final rebase to land-story's release of the lease) is one-at-a-time across all stories.

**Resuming a story in a fresh session** (after a context clear, a crash, or a handover):

```
uv run --no-project .workflow/scripts/story_ledger.py <story-id>
```

It reports the position — phases observed, review rounds, the review loop's state, `story-finalize` runs, commits, and which artifacts exist — projected from the session journal (`.workflow/state/journal/`) plus git plus disk. Nothing writes it, so it cannot be stale. Read the position, then follow its pointers to ground truth (the findings file for verdicts and triage decisions, git for commits) rather than reconstructing state from the conversation. A missing phase is not proof the phase did not run. A session that resumes a land window runs `uv run --no-project .workflow/scripts/land_lease.py acquire <id>` before anything else in the window: `.workflow/hooks/guards/land_lease_wait.py` ties the window to a session through that call in its transcript.

**A story resumed after an auto-dev-loop halt.** Its branch carries `wip: [Story <id>] auto-loop halted in <step>` commits and docs commits besides impl and close-out (`git log --reverse --format='%h %s' <merge-base>..HEAD`). `story-rebase` and the land window assume exactly impl + close-out, so three things run first — after Prerequisites item 2 has passed for the story, so a refused story's worktree is never touched:

0. **Halt answers.** `uv run --no-project .workflow/scripts/merge_train.py home <id>` (a worktree left on a train branch goes back to the story branch; exit 2: stop and tell the owner). Then read the latest `resumed halt` block of `uv run --no-project .workflow/scripts/sprint_status.py history <story-key>` and apply its `ruling:` line (and each external owner's answer to its ticket, when the entry carries one) as the auto-dev-loop workflow states; then `story_record.py append <id> "halt answers applied <entry id>"` and commit the spec edit and the record line on the story branch (`git -C <worktree_path> add <spec_path> <record_path>`, `git -C <worktree_path> commit -m "docs: [Story <id>] halt answers applied"`). A block whose `halt answers applied <entry id>` line a commit on the story branch already holds (`git -C <worktree_path> log <branch> -S"halt answers applied <entry id>" --oneline -- <record_path>`) is not applied twice; a line in the file with no such commit gets the commit alone. A story with no `resumed halt` block skips this.
1. **Repro clean-up, before the story's review round** (round 1, or the next round when the findings file already holds one): the auto-dev-loop workflow's "Review re-run: repro clean-up" steps. A halt WIP commit's committed repro files would otherwise reach the gate. The round carries the halted rounds' findings (`layered-review` Step 1); Checkpoint 2 walks each carried row, marked by a `carried:<id>` source, with the round's own.
2. **Fold, before the close-out review round (when one runs) and before the land window:** the halt's `wip:` commits (the clean-up's `drop unadopted repro tests` commit included) and the other docs commits (subject starting `docs:` or `docs(`) fold into the impl commit and the close-out commit, so the branch lands as impl + close-out. The impl commit is the first commit of `<merge-base>..HEAD` whose subject matches story-impl's format, `(feat|fix|docs|refactor|test): [Story <id>] …`, and does not name the code review close-out; a `docs: [Story <id>] …` commit is the impl commit only when the branch has no non-docs commit in that format and no `wip:` commit. Every other docs commit is a side commit. A later commit in that format before the close-out (a seed-AC run's `feat: [Story <id>] seed ACs …`) joins the impl commit. The close-out commit is the one whose subject names it. With no non-docs commit in story-impl's format, the oldest `wip:` commit is the impl anchor and its message is replaced by `feat: [Story <id>] <spec title>`. With no commit in that format and no `wip:` commit, the fold halts: report to the owner (an unattended driver halt-and-releases with `failure_step="resume-fold"`). Each `wip:`, side docs or seed-AC commit joins the nearest earlier of the two (one before the impl commit joins it). Trees are cumulative, so write the folded commits with `git commit-tree`, taking each anchor's message from `git log -1 --format=%B <sha> > <scratch file>`:
   - new impl = `git commit-tree <tree of the last commit before the close-out commit, or HEAD without one> -p <merge-base> -F <impl message>`;
   - new close-out (when one exists) = `git commit-tree <HEAD tree> -p <new impl> -F <close-out message>`;
   - `git reset --soft <new tip>`: index and worktree already hold that tree, so the recorded GREEN lines stay valid.

**Fresh session at a heavy human checkpoint.** At Checkpoints 1, 2, 2b and 3, once the owner's decision is on disk (the `story_record.py` line, or the `## Triage decisions` section of the findings file) and the main session's context is at least `config: guards.clear_offer_context_tokens` (default 300k), end the message with `State saved — clear the context, then resume parallel-dev-wave <story-id>` before the next subagent is dispatched; while a subagent this session dispatched is still running, add that the clear waits for it. Below the threshold, dispatch in the same session. At Checkpoint 3 the offer is made before the window's `land_lease.py acquire`: the land window contains no human wait, so the offer cannot come later. `.workflow/hooks/guards/clear_offer.py` holds the next story-impl / story-finalize dispatch, and a `land_lease.py acquire <id>` command, once, until the offer is made and the owner has replied. The resumed session runs `story_ledger.py <story-id>`, reads only the section the next phase needs (for Phase 3: the `## Triage decisions` section, not the whole findings file), and dispatches. A story session idle for more than an hour resumes the same way — never by continuing the old conversation.

---

## Slot registry

All active slot assignments are tracked in the slot registry (`config: paths.slot_registry`).

- Slots `1..config: slots.count` are assignable. Slot `config: slots.shared_slot` is the shared dev environment — never assigned to a story.
- `story_setup.py` reserves the slot at the start of Phase 1 via the lock-protected `reserve_slot.py`: it picks the first `status=free` row and updates it in place (`status=in_use`, `story_id`, `branch`, `worktree`, `since`).
- land-story resets the row to `free` after cleanup.

---

## Prerequisites — verify before starting

1. **Free slot available** in the registry.
2. **Refuse a halted story.** When the story's row reads `blocked`, check it before Phase 1 runs, so a refused story touches no slot, worktree, stack or rebase. A failure entry for the story in the failure log (`config: paths.auto_dev_failure_log`, any heading form of the id) means the halt's resume has not run: stop and tell the owner to run `uv run --no-project .workflow/scripts/resume_halted.py <story id>` first. A `blocked` row with no entry whose note carries a `HALTED <date> at <step>` clause is a failed halt: the owner rules on it; stop. A failure log or tracker that cannot be read leaves the row unclassifiable: stop and report the read failure — never treat an unread source as "not halted". A hand-blocked row (a `blocked` row meeting none of these) goes on to Phase 1 and flips like any other. The classification comes from `uv run --no-project .workflow/scripts/story_state.py --attention`: the story's `blocked-row` entry in `attention.entries` (every form of the id resolves to it; never match a literal `## <id> — ` heading). `failure_step` set: the failure entry stands. `failure_step` null, `note_status` `ok` and a `HALTED <date> at <step>` clause in `asked`: a failed halt. `note_status` `error`: the note read failed (`note_error`) — report the read failure. A `blocked` row with no `blocked-row` entry, or an `auto-dev failures` / `sprint-status` source reading `error`, cannot be told from a halted one and refuses too; a source `none` (no failure log) holds no entry.
3. When `config: stack.runtime` is not `none`: every host port mapping in the compose file reads one of the env vars in `config: stack.ports` (`${VAR:-default}`), and no service a slot brings up has a hard-coded `container_name:` (concurrent stacks collide on both). When it is `none`, say "stack prerequisites skipped — stack.runtime = none".

---

## Phase 1 — Run `story_setup.py`

Phase 1 is a deterministic script, not an agent. Run it from the repo root as a single call:

```
uv run --no-project .workflow/scripts/story_setup.py <story_id>
```

The script does only the **fast, deterministic prep**: reserves a slot (lock-protected `reserve_slot.py`); computes the slot's port env vars (every var in `config: stack.ports`, offset by `config: stack.port_stride`); creates the worktree off `config: git.base_branch` (skips if it exists) on branch `config: git.branch_prefix` + story id, seeding `config: git.worktree_seed_files`; locates the spec under `config: paths.specs_dir`; indexes the worktree into the code graph when `config: graph.tool` is not `none` (the name goes to the `.workflow/state/story-setup/<stack>.graph_project` sidecar); seeds `.workflow/state/story-setup/<stack>.json` with `status: starting`; launches `bring_up_stack_async.py` in the background when the stack is enabled (build + up, readiness poll over `config: stack.services`, seed, watch start; status `up` or `failed`; output in `<stack>.log`); prints a `key: value` report. It returns in seconds — proceed to Checkpoint 1 while the bring-up runs.

| Exit | Meaning | Orchestrator action |
|---|---|---|
| 0 | Slot reserved, worktree created, async bring-up launched (or stack skipped) | Parse the report, proceed to Checkpoint 1 |
| 1 | Error — reserve / worktree / launch failure | Surface the failure to the owner, halt |
| 2 | No free slot | Halt — the owner decides whether to wait |
| 3 | Multiple candidate spec files | Surface the candidates; the owner disambiguates, then continue |
| 4 | The story already holds a slot: another session (or an unattended driver) owns it | Halt — tell the owner the slot and owner; never re-run with `--reserved-slot` (only the session that reserved the slot passes it) |

**After exit 0**, flip the sprint status — key-addressed, never a line-number edit, because another session may be editing the file concurrently. The flip runs for a resumed halted story and a hand-blocked row too:

```
uv run --no-project .workflow/scripts/sprint_status.py set <story-key> in-progress --note "STARTED <date> slot <N> (<branch>); <phase marker>; dep: <...>"
uv run --no-project .workflow/scripts/sprint_status.py touch --note "<one sentence>"
```

Notes are capped at `config: sprint_status.note_max_chars`; `set` archives the replaced note to the story's history file; use `--history "..."` for long-form detail. The setup script does NOT touch the sprint-status file.

The report carries `slot`, `branch`, `worktree_path`, `stack_project_name`, `graph_project`, the slot `ports`, `stack_status_file`, `stack_log_file`, `background_pid`, `spec_path`, and `spec_present`.

---

## Model policy (pipeline-wide)

Each agent declares a **tier**, an **effort** and (Claude Code) a `maxTurns` cap in its frontmatter (`agents/<name>.md`); the concrete model per tool comes from `config: models.<tool>.<tier>`. Read them there; never restate them in a skill or prompt, and change an agent's frontmatter and any workflow `EFFORT` map together.

**story-impl escalation.** A fresh story-impl dispatched with `handoff:` runs as `story-impl-xhigh` when the hand-off file's `rounds_used` is 2 and as `story-impl-max` when it is 3 or more; every other dispatch is `story-impl`. The variants are generated copies of `story-impl.md` (`gen_agent_effort_variants.py`). `.workflow/hooks/guards/agent_dispatch.py` denies any other `subagent_type` and names the right one: re-dispatch with that name and the same prompt.

**Effort trials.** `story-rebase` and `story-diagnose` run at `high`, reverted to `max` only after asking the owner, on a second batch-fix round caused by that agent's own error (a wrong edit, a wrong conflict resolution, a wrong diagnosis). `findings-verifier`: a finding it marked REFUTED that is later patched. Do not lower an effort except via a trial with a stated revert trigger, recorded here.

**`maxTurns`.** A dispatch that returns output marked partial hit the cap: do not resume it and do not retry it — HALT to the owner with the agent, story and last reported step. Inside the review workflow a capped agent returns nothing and is reported as a failed layer or an unverified finding.

**A failed dispatch is retried once** (with `config: models.fallback.<tool>.<tier>` as a dispatch-time override where one is set and the tool supports it). Only after both attempts fail does it count as failed. **Never override `findings-verifier` to another model.** It has no fallback.

**`story-finalize` — if a Phase 3 run needs a second batch-fix round:** re-check every patch against the manifest before accepting it, and report the round with its cause.

**Post-merge smoke has no agent** — `.workflow/scripts/post_merge_smoke.py` via the `post-merge-smoke` skill.

---

## Checkpoint 1 — Spec review (human)

**If `spec_present: false`** — dispatch the `story-spec` subagent (retry once on a failed dispatch). Pass in the prompt:

- The story ID and title (from the epic under `config: paths.epics_dir`)
- The full epic AC block for the story
- The worktree path
- The slot's `graph_project` (omit when `config: graph.tool = "none"`)
- `spec_target_path`: `<worktree_path>/<config: paths.specs_dir>/<story-key>.md`
- `mode: checkpoint`
- `research_pack`: the path `uv run --no-project .workflow/scripts/story_research_pack.py <story_id> --worktree <worktree_path>` prints, run just before the dispatch

The spec MUST be written to the worktree (not the main checkout) — it is committed as part of the story branch.

**Interactive override:** when the owner asks to co-draft the spec, skip the dispatch and invoke the `create-story` skill in the main session. Then, when `story_record.py reach <id>` exits 1, invoke the `ui-state-reach` skill as story-spec's step 3 does.

**Then** present for review: the spec's **path**, a short summary (story goal, AC count with one-line AC titles, the files it will touch), the subagent's batched `open_questions` list, and its `missing_states` list (each MISSING row of the spec's "UI states and how each is reached" table, with the seed AC or `[OPEN-Q <n>]` that resolves it). **Do not paste the spec body into the conversation** — every pasted line is re-read on every subsequent call. The owner reviews the file in an editor and may edit it directly in the worktree; paste a specific section only if asked. Collect all answers in one round-trip, apply them to the spec, and wait for explicit confirmation before Phase 2.

Applying a ruling replaces its `[OPEN-Q <n>]` marker with the decision, without brackets (`OPEN-Q <n> (<option>)`): write it with `uv run --no-project .workflow/scripts/story_record.py rule <story-id> <n>=<ruling> ...`, never a hand edit. A question tagged for an external owner (`config: spec.external_question_tags`) is not the owner's to rule: for the design owner (`config: design.question_tag`), invoke the `design-ticket` skill (one ticket per story; it files the questions, adds a new ticket's row to `config: paths.design_questions` with the Blocks cell "the story ships the interims, ⚠️", and rewrites each marker without brackets as `OPEN-Q <n> (<tag>, interim ⚠️, <ticket key>)` through `design_ticket.py mark-spec`); for another external owner, file it on that owner's tracker and rewrite the markers the same way. With no tracker configured (`config: tracker.backend = "none"`), the skill says SKIPPED and the question goes to the owner. The story proceeds with the spec's interims; the external answer replaces a value in a follow-up change. An external question the spec gives no interim for goes to the owner for the interim only.

**When the owner confirms, record it durably:**

```
uv run --no-project .workflow/scripts/story_record.py append <story-id> "checkpoint-1 spec approved"
```

The script resolves the worktree from the slot registry, creates the record file with its heading, dates the line, and refuses duplicates. It refuses the approval while the spec has a bracketed `[OPEN-Q]` marker or an incomplete `spec-draft` record line (`story_record.py open <id>` lists them). Do not hand-write the markdown. **`.workflow/hooks/guards/phase_dispatch.py` blocks the Phase 2 `story-impl` dispatch until this line exists** and while an open item remains.

---

## Pre-Phase 2a — Wait for the background bring-up (orchestrator)

**If `config: stack.runtime = "none"`:** skip and state "stack wait skipped — stack.runtime = none". Still run Pre-Phase 2b.

```
uv run --no-project .workflow/scripts/wait_for_stack_ready.py <stack_project_name>
```

The poller emits one timestamped heartbeat line per iteration. It returns:

- **Exit 0** — status reached `up` **and the services are confirmed running now**. Proceed. Idempotent — returns instantly if the status file already says `up`.
- **Exit 1** — status `failed`. Surface the error + log tail (the script prints both) and halt. A seed failure on a fresh stack comes after the helper's one automatic wipe-and-retry and names both outcomes; a stack that already held data is never wiped. After the fix, run `bring_up_story_stack.py --slot <N> --project-name <stack> --worktree <worktree_path> --mode up` (its exit 0 replaces the waiter), then `--mode watch` in the background when the helper failed before starting the watch, and continue at Pre-Phase 2b.
- **Exit 2** — timeout. Surface diagnosis options (stuck build / container runtime hung / mismatched ports) and decide whether to retry.
- **Exit 3** — **stale `up`**: the bring-up finished, but the stack is not running any more. Re-run `bring_up_story_stack.py ... --mode up` (it passes every port var), start `--mode watch` in the background and run `uv run --no-project .workflow/scripts/wait_for_watch_ready.py <its output file>`, then re-run the poller. Never bring the stack up with a bare compose `up`.

## Pre-Phase 2b — Stack preflight (orchestrator, script)

```
uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack_project_name> --worktree <worktree_path>
```

It checks the services, every `config: stack.health.checks` command, watch liveness, worktree-to-container code sync (read-only hash compare), and branch. **Exit 0** → Phase 2. **Exit 1** → surface the failing checks to the owner and HALT. For diagnosis beyond the script's lines, dispatch the `dev-preflight` subagent (diagnosis-only — it never runs stack lifecycle commands).

---

## Phase 2 — Dispatch the `story-impl` subagent

Pass in the prompt: `story_id`, `branch_name`, `worktree_path`, `stack_project_name`, `slot`, `graph_project` (omit without a graph), `spec_path` (absolute, in the worktree).

The subagent implements all ACs **tests-first (Batch TDD)**: every test written before any implementation code, one scoped run to confirm a batched RED, implement, then the GREEN validation under the batch-fix rule, recorded by the runner (`--record <id> phase-2`). It then runs the schema-impact check, makes the impl commit, and returns. It does NOT run code review — the review must not share the author's context.

Phrase the prompt with the batched shape ("write all tests → one red run → implement → one green run → batch-fix") and avoid the forbidden iterate phrases ("after each", "per fix", "iterate", "between fixes", "confirm green before moving on") — `.workflow/hooks/guards/dispatch_prompt.py` blocks them.

**Context-cap hand-off.** A `story-impl handoff — context cap` report means the subagent stopped at the cap, its work uncommitted in the worktree. Record `uv run --no-project .workflow/scripts/story_record.py append <story-id> "impl handoff <n> at <context>; rounds_used <N>; file <handoff_file>; NEXT: dispatch a fresh story-impl with handoff: <handoff_file>"`, then dispatch a fresh story-impl (`subagent_type` per "story-impl escalation") with the same inputs plus `handoff: <handoff_file>`. A third hand-off in the same phase is a HALT: give the owner the hand-off file path and its `next:` line.

---

## Post-Phase 2 — Invoke `layered-review` (orchestrator, main session)

After `story-impl` returns, invoke the `layered-review` skill in pipeline mode with: `story_id`, `mode: controlled`, `worktree`, `spec_file` (absolute spec path), `graph_project` (the slot's, or `none`), `stack_project_name` (or `none` without a stack). `review_stage.py`, run by the skill, computes the round number and the diff range. **Snapshot mode** — another agent is mutating the worktree (e.g. `story-finalize` applying patches): name a scratch dir and pass it as `snapshot_dir`, with `graph_project: none`; fill it as the skill's Step 1 snapshot paragraph says; the workflow then skips the repro and execution stages.

The skill stages the diff and runs the review workflow. The layers and the verifier are typed read-only subagents. The workflow: runs the layers (blind-hunter, edge-case-hunter, verification-gap, and acceptance-auditor when there is a spec) with one retry each; dedupes in code; has `findings-verifier` verify every deduped finding (retrying any finding without a verdict once); runs the repro and execution stages; renders the findings file (`<config: paths.specs_dir>/<story>-code-review-findings-<date>.md` in the worktree: raw layer reports, verdicts, evidence, bar-impact lines); and appends a `review round <n> recorded` line to the story record.

**Silence is a failure, not a pass.** Never present as clean: a layer in `failed_layers`; a finding in `unverified`; a UI state in `ui_unreached` (with the methods it tried) or `ui_out_of_scope`; an entry in `ui_unaccounted`, `ui_double_placed`, `ui_unknown_ids`, `ui_untested_titles`, `ui_unplaced_viewports` or `ui_unmapped_acs`. A layer whose model is known-exhausted for the billing period counts as failed; tell the owner.

- For an unverified finding, dispatch `findings-verifier`, its prompt led by `config: review.required_preamble` and carrying that finding's section (`uv run --no-project .workflow/scripts/review_findings_show.py <findings-file> <id>`), before it can be deferred or dismissed.
- If that also fails, either HALT, or verify inline with cited evidence and tell the owner the pass was a deviation, naming the findings it covered. Bar impact is never assessed inline.

REFUTED findings never reach the triage walkthrough: they are auto-dismissed with evidence in the findings file and appear at Checkpoint 2 only as the compact "dismissed as false positive (N)" block.

---

## Checkpoint 2 — Code review triage (HARD RULE)

**Triage authority belongs to the owner. Never auto-apply patches based on recommended classifications.**

Each finding is classified under the `rules-defer` skill as:

- **patch** — apply now (close-out commit)
- **defer** — the fix needs scope outside the story or a design or spec decision, named and tracked; `story-finalize` writes it to `<config: paths.deferred_work_dir>/story-<id>.md`
- **dismiss** — a false positive or another reason on the rule's closed dismiss list, no action

**Quality-bar gate (HARD).** The bar assessment is produced by `findings-verifier` (it read each finding's cited code). **The orchestrator does NOT assess bar impact.** It relays the verifier's `bar impact:` line verbatim and enforces the rule mechanically:

- A finding marked `BLOCKING` is **patched, always** — `defer` and `dismiss` are not offered for it.
- If it cannot be patched surgically within the close-out, HALT and surface it, quoting the verifier's `<bar-id>` and consequence.
- REFUTED findings carry no bar line and need none.
- When `config: paths.quality_bar_doc` is empty the gate is skipped; say so once at the top of the triage.

A finding that arrives with no `bar impact:` line is routed back through verification before it can be deferred or dismissed — never invent one.

**UI states first.** Before the findings walk, present `ui_unreached` (each state with the methods it tried), `ui_unaccounted`, `ui_double_placed`, `ui_unknown_ids`, `ui_untested_titles`, `ui_unplaced_viewports`, `ui_unmapped_acs` and each `ui_out_of_scope` claim (`<state> — <kind> — <reason>`). The owner accepts or rejects each claim. A rejected claim, an unaccounted or double-placed state, an untested title, an unplaced viewport or an unmapped AC id is ruled like an unreached state: the story does not proceed with an in-scope UI state unchecked.

**The main session's call.** Read `rules-defer`. Read every triage row's claim, verification evidence and bar line in one call: `uv run --no-project .workflow/scripts/review_findings_show.py <findings-file> <id>... --fields claim,verification,bar`. Read the cited file:line of a row whose call turns on scope. For each row make the main session's call — `patch`, `defer` or `dismiss` — with a one-line reason:

- a `defer` call names the scope or decision it waits on and its tracking row: an existing one (`` `<path>` — `<key>` ``), or the exact row to add (kind, file, text naming `<story_id> <finding_id>`);
- a `dismiss` call names its reason from the rule's list;
- a call that differs from the layers' column names the rejected reason (✅ `patch — the layers' defer is test-only`);
- a `BLOCKING` row is called `patch`.

The call is a proposal: the owner rules every row, and nothing is applied on the call alone.

Before the walk, present the round's Summary `Reviewed:`, `Delta:` and `Loop exit:` lines (the round's `### Summary`). In a `Loop exit: candidate` round, the call for a finding that is not never-deferred is `defer` under the Loop exit, naming its tracking row.

Present the compact triage table — `id | sources | location | delta | title | verdict | bar impact | layers recommend | main-session call` — and the refuted block. A finding's full text is read from the findings file when the owner asks for it (`review_findings_show.py`). Walk through the findings one at a time; wait for the owner's call on each. Do NOT apply any patch or write any defer entry until the full list is settled.

**Enforcement.** The review layers and `findings-verifier` are read-only by their tool lists, and every workflow prompt leads with the preamble. `repro-writer` writes only untracked repro test files (`.workflow/hooks/guards/repro_paths.py`). `.workflow/hooks/guards/dispatch_prompt.py` blocks any review dispatch whose prompt lacks the no-auto-apply instruction.

Record the decisions into `approved_patches`, `deferred_findings`, `dismissed_findings`, and the rulings on the `ui_out_of_scope` claims as `ui_out_of_scope_rulings`.

**Every `deferred_findings` entry reads** `` <finding_id> — <title> — waits on: <scope or decision> — tracked in: `<path>` — `<key>` `` (plus its bar-impact and reasoning parts).

**Tracking rows, before the decisions are persisted.** For each defer whose tracking row does not exist, write the row the owner approved, in the main checkout (never in a story worktree): a line in a not-done story's epic block; a carve (epic block plus `sprint_status.py` row, the owner's ruling); an open row in a backlog doc (`config: paths.product_backlog`, `config: paths.harness_backlog`); a design-owner question through the `design-ticket` skill (its ticket key is the `<key>`); or an open-question line in the owning story's epic block. Every row except an external tracker's names `<story_id> <finding_id>`. Commit only your own hunks (`.workflow/scripts/stage_own_hunks.py`).

**`approved_patches` MUST be a patch manifest, not prose.** One entry per approved finding:

```
finding_id | file (repo-relative) | line (from the verification evidence) | change (one imperative sentence describing the exact edit) | notes (optional constraints) | repro_test
```

`repro_test` — the finding's repro test path when its triage row's `repro` cell reads `REPRODUCED <path>`, else `-`. story-finalize adopts each named repro test as the regression test (the patch must turn it green) and deletes every other repro file the round left.

**Persist the decisions to disk BEFORE dispatching Phase 3.** Append a `## Triage decisions — <YYYY-MM-DD>` section to the round's section of the findings file carrying the three lists verbatim (`### approved_patches` — the full manifest, `### deferred_findings`, `### dismissed_findings` with reasons), then `### ui_out_of_scope_rulings` (`<state> — <kind> — accepted | rejected — reasoning: <one line>`; `none` when the review listed no claim). **`.workflow/hooks/guards/phase_dispatch.py` blocks the Phase 3 `story-finalize` dispatch until this section exists in the latest recorded round and a phase-2 GREEN line exists.** Once persisted, Phase 3 can be dispatched from a fresh session — a safe context-clear boundary.

---

## Phase 3 — Dispatch the `story-finalize` subagent

Pass in the prompt: `story_id`, `branch_name`, `worktree_path`, `stack_project_name`, `graph_project`, `approved_patches` (the manifest, inline or as the named findings-file section: path + heading), `deferred_findings` (each with `waits on` and `tracked in`), `dismissed_findings`, and `last_round` — `yes` when no review round follows this finalize (the round cap, the owner's skip, or a patch count below the next-round trigger), else `no`.

The subagent applies patches in one pass, checks stack logs, re-runs scoped tests (batch-fix, recorded as phase-3), writes deferred items to the per-story file, and adds AT MOST ONE close-out commit (only if patches OR defers landed). The impl commit was already made by `story-impl`. It does NOT merge. When a later round or a gate fix triggers, Phase 3 runs again with that round's lists only and **amends** the close-out commit — the commit cap counts commits, not amends.

---

## Round `<next>` (conditional code review; 2 in the normal case)

After Phase 3 lands the close-out commit, inspect the latest review round's patch count (the highest `## Review round <n>` section of the findings file).

**Trigger** (either): `approved_patches >= config: review.round2_min_patches`, OR `unique_files(approved_patches) >= config: review.round2_min_files`. The owner may override either direction ("skip round `<next>`" / "force round `<next>`"). Below threshold and without an override, go to Checkpoint 3.

**Before the trigger check,** run `story_ledger.py <story_id>`. Read a `review re-run due: round <n>` line first, ahead of every `review loop ended` line: a phase-2 GREEN re-run owed beside an ended loop runs now (items 1–3 below; `review_stage.py` stages it as a branch round). A `review loop ended: Loop exit at round <n>` or `review loop ended: round cap …` line goes straight to Checkpoint 3 with no override (`rules-defer` Loop exit / Round cap; `config: review.close_out_cap`), except item 4; a `review loop ended: new-code round: no further review round` line goes straight to Checkpoint 3 with no further round. A `review loop ended: no patch approved in round <n>` line is below the trigger, so only the owner's "force round `<next>`" runs a round; `review_stage.py` refuses a forced round at the cap.

When triggered:

1. **Re-invoke `layered-review`** (`mode: controlled`); `review_stage.py` stages the close-out round (the close-out patch against the close-out the previous triaged round reviewed) and its number. The findings file and the record are pipeline output and are never reviewed.
2. **Checkpoint 2b** (human, HARD RULE). Same triage discipline and main-session call column as Checkpoint 2.
3. **Second Phase 3 run** with round `<next>`'s lists only; new defers go under a `## Round <next> — <YYYY-MM-DD>` heading; the close-out commit is **amended**.
4. **New code after the last round** (`rules-defer`). When a last-round patch or ruling adds code beyond its finding's fix, the owner rules at that triage which outcome applies: one review round of that change alone, or a carve. For the review: note the close-out hash before the `story-finalize` that applies the change amends it, then re-invoke `layered-review` with `new_code_since: <that close-out>`, triage it at Checkpoint 2b and finalize it with `last_round: yes`. The loop ends after it. For the carve: defer the finding to the new story's row.

---

## Rebase / re-stack — `story-rebase` subagent

Dispatch `story-rebase` whenever the branch must move onto a newer base: onto `config: git.base_branch` (before a review round, or when a file-coupled story merged first), or onto another story's branch (stacking). Never hand a rebase to `story-finalize` or `story-impl`.

Re-check the target tip immediately before dispatching. Record the dispatch with a `NEXT:` line (`story_record.py append <story-id> "rebase dispatched onto <target>; NEXT: <step>"`). Pass:

- `story_id`, `branch_name`, `worktree_path`, `graph_project`
- `slot`, `stack_project_name` and its state (up / parked / down)
- `target` — the ref to rebase onto; for a stack also the old base (`git rebase --onto <target> <old-base>`)
- the branch's current commits (impl + close-out hashes, base; a resumed branch: every commit)
- per-file conflict rules for the files both sides changed, and the spec sections that govern them: `uv run --no-project .workflow/scripts/branch_overlap.py <branch_name> --base <target>` lists them (CONFLICTS: git's three-way merge leaves markers; OVERLAP: both sides changed); pass both lists
- `validate: yes | no | stack` — `no` when the stack is parked/down or the orchestrator rebuilds it itself; `stack` for the land window's final rebase (brings the stack in line and runs preflight, no scoped tests: the window's gate validates)
- `range_diff_path` — `.workflow/state/story-setup/range-diff-<story_id>-<n>.txt`, `<n>` one more than the story's highest there (land-story's cleanup copies every range diff into the story-history dir)

The subagent returns the new hashes, every conflict with its resolution, and the stack-affecting files the rebase brought in. A resolution needing a design or scope call comes back as a HALT with file:line and options — relay it to the owner. Relay its `strays:` too: folding a stray's content into the main checkout's copy is the owner's call.

---

## Pre-merge gate failure — `story-diagnose` subagent

When `run_full_suite.py --story-id <id>` reports a failing leg on a story branch: inside the land window the run has already withdrawn the `checkpoint-3` approval and released the lease. A gate killed before its exit handler ran leaves the lease held: run `uv run --no-project .workflow/scripts/land_lease.py holds <id>` first; exit 0 → append `checkpoint-3 approval withdrawn -- pre-merge gate exit <rc>` and run `land_lease.py release <id>`; exit 1 → nothing to add; exit 2 → release nothing and HALT to the owner with its stderr line. Then dispatch `story-diagnose` with every failing test of that run in one prompt (never hand a gate failure to `story-finalize` or `story-impl`):

- `story_id`, `branch_name`, `worktree_path`, `graph_project`, `stack_project_name`
- `base` — `git merge-base <config: git.base_branch> <branch_name>`
- the gate summary: each failing test id per leg with its tail, and any artifact paths it names

Record at dispatch: `story_record.py append <story-id> "gate diagnosis dispatched: <N> tests in <legs>; NEXT: write the ## Gate fix section, then story-finalize applies it"`.

On return:

- **`story-diagnose handoff — context cap`:** record `gate diagnosis handoff <n>: <k> of <N> diagnosed; NEXT: dispatch story-diagnose for the remaining <m>`, then dispatch a fresh `story-diagnose` with the same inputs, the gate summary restricted to the `remaining` tests, `prior_notes` = its `remaining:` rows and `first_row` = the next free `G<n>`. Apply the rules below to the union of the reports. A third hand-off for the same gate run is a HALT.
- **A manifest row whose file is a harness path** (`.workflow/`, the tool's agent/skill dirs) the story's ACs do not cover: not a gate-fix row. It goes to a backlog row under `rules-defer`, and the gate failure it would fix is relayed to the owner.
- **Every failure `branch`, no HALT:** append the failures and manifest verbatim to the findings file as `## Gate fix — <YYYY-MM-DD>`. Then dispatch `story-finalize` with `approved_patches` = the manifest rows (or that named section; state the row count), empty defers and dismissals, the failing test labels as `scoped_labels`, and the instruction to amend the close-out commit. Record `gate-fix finalize dispatched: G1–G<n>; NEXT: fresh Checkpoint 3 approval, then a new land window`. When finalize returns, take a fresh Checkpoint 3 approval and open a new window; no gate runs in between.
- **Any `pre-existing`, `environment` or `unproven` failure, or a HALT:** relay the whole report to the owner and wait. Dispatch nothing for that gate run until the owner rules.

Gate-fix patches do not count toward the next-round trigger; the next window's gate verifies them.

**Gate-exemption decision.** After Phase 3 (+ Round `<next>`), and before Propagation: when AGENTS.md's pre-merge full-gate triggers do not require the gate, record `story_record.py append <story-id> "pre-merge gate not required — <the AGENTS.md clause that applies>"`; when in doubt, it does require the gate.

**The gate runs inside the land window**, after the final rebase: `uv run --no-project .workflow/scripts/run_full_suite.py --story-id <id> --record`, plus `--profile-leg` when `profile_leg.py due` exited 1 (the profile leg runs alone under `config: profile_leg.profile` after the per-merge legs; the stack is returned to its resting profile; a green run resets the due count). On an all-green run it appends `pre-merge gate GREEN … HEAD <sha12> code-tree <hash12> base <sha12>` itself; never write that line by hand. Any other non-zero exit while `<id>` holds the lease appends `checkpoint-3 approval withdrawn -- pre-merge gate exit <rc>` and releases the lease automatically. A failing preflight is re-run for `config: tests.preflight_wait_s`; still failing, the run prints `pre-merge gate not run: preflight failed (...)`, exits 75, withdraws nothing and releases the lease unless it printed a lease WARNING (then run Land window step 4's lease check); the approval stands.

**A story waiting in the line gates its train.** `merge_train.py wait <id>` (Land window step 1) builds the train branch (`config: git.train_prefix`) in the story worktree and runs `run_full_suite.py --story-id <id> --train --record` on it while the stories ahead land; its GREEN line ends `on <train branch> (exit 0) HEAD … code-tree … base … train <ids>`. A red train gate appends `checkpoint-3 approval withdrawn -- train gate exit <rc>` and removes the story from the line. A train gate whose preflight still fails ran no test: exit 75, nothing withdrawn, the story stays in the line and the next poll rebuilds and gates it. A conflicting or stack-affecting train is not gated: the story gates in its window.

`.workflow/hooks/guards/phase_dispatch.py` blocks land-story unless the story holds the lease and the latest pre-merge verdict passes the land guard (`story_ledger.premerge_verdict`): the `not required` line; a train GREEN whose tested train differs from the rebased `HEAD` in no gate input (`config: pipeline.gate_inert_prefixes`) and whose base the base branch still has; or a GREEN whose `code-tree` equals the worktree's current code tree, whose `base` is an ancestor of the base branch, and whose base equals the base branch's tip (or whose movement since is gate-inert and off the branch's paths). The block text names the reason. The only alternative to a re-run is an override line `pre-merge base accepted — <base branch> <sha12> (<reason>)` that the owner instructs; append it only on that explicit instruction, quoting the reason; it passes only while the base branch's tip equals that sha.

---

## Propagation — `story-propagate` subagent (propose)

After Phase 3 / Round `<next>` and the gate-exemption decision, dispatch `story-propagate` with:

```
mode: propose
story_id: <story-id>
worktree_path: <worktree_path>
spec_path: <spec path in the worktree>
base: <config: git.base_branch>
docs_root: <the main checkout>
```

Write its checklist verbatim to `.workflow/state/story-setup/propagation-<story-id>.md` (git-ignored; land-story's cleanup copies it into the story-history dir). Present it at Checkpoint 3.

---

## Checkpoint 3 — Merge approval (human)

Present to the owner:

- Branch name
- Commit(s) — hash + subject
- Test results — from the story's record (`phase-2 tests` / `phase-3 tests` lines the runner wrote). If the record carries no test line, tests have no durable evidence: say so and re-run the scoped suite rather than presenting an unevidenced "green".
- Deferred items appended this round (count + summary)
- That the full pre-merge gate follows in the land window, and that a red gate withdraws this approval
- The propagation checklist: each row to decide **update** (as proposed or reworded) / **follow-up** / **skip**; each cascade **revise now** / **follow-up story** / **sprint replan**; each memory candidate keep or drop

Write the rulings under `## Decisions` in `propagation-<story-id>.md`, then `story_record.py append <story-id> "propagation decided: <k> update, <m> follow-up, <s> skip; NEXT: story-propagate apply in land-story"`. Save the memory candidates the owner keeps. **An undecided cascade blocks the merge.** A checklist with no rows records `propagation decided: no rows`.

Ask explicitly: **"Ready to merge `<branch>` to `<config: git.base_branch>`?"** Do not merge until the owner confirms. When the owner confirms:

```
uv run --no-project .workflow/scripts/story_record.py append <story-id> "checkpoint-3 merge approved"
```

This line is written after the close-out commit, so no branch commit carries it; land-story's cleanup copies the record into the main checkout and its bookkeeping commit lands it.

---

## Land window

Once the owner confirms at Checkpoint 3 (approval recorded; at the context threshold the clear offer is made and answered here, before `acquire`), one story at a time holds the stretch from the final rebase to land-story's release of the lease. The lease record is `.workflow/state/land-lease.json` in the main checkout, guarded by a named lock, with no expiry. Run from the main checkout:

1. `uv run --no-project .workflow/scripts/land_lease.py acquire <id>` (refused, exit 1, without a standing `checkpoint-3` approval). Exit 3: another story holds it or is ahead in the line — tell the owner who, then run `uv run --no-project .workflow/scripts/merge_train.py wait <id>` in the background and wait for it (no total budget): it joins the first-come-first-served line, builds and gates the story's train while it waits, recovers a holder whose gate died, takes the lease at the story's turn, and leaves the line on its own exit 3 (the holder held the lease longer than `--holder-stall-min`, or the lease stood free that long behind a stalled head of the line), which you report. Its exit 0: step 2. Its exit 4: a red train gate withdrew the approval — run the gate-failure path on the train tree (`base` = `git merge-base <base branch> <train branch>`, summary from `.workflow/state/story-setup/train-gate-<id>.txt`), run `merge_train.py home <id>` before `story-finalize`, take a fresh Checkpoint 3 approval, open a new window. Exit 2 on either call (lease record, lock, git or OS error): run `land_lease.py holds <id>`; on exit 0 release the lease; then HALT with the stderr line. Re-running `acquire` as the holder is a no-op.
   **Before step 2, every window (a resumed one included):** `uv run --no-project .workflow/scripts/merge_train.py home <id>` (checks out the story branch when the worktree is on another; the record file's uncommitted lines are carried). Exit 2: release the lease and report; the approval stands.
   **Profile-leg due check, every window (a resumed one included), after `home`:** `uv run --no-project .workflow/scripts/profile_leg.py due` (after the lease is held, because a queued wait can let another story merge). Exit 1: the window's gate adds `--profile-leg` (one gate: the per-merge legs plus the profile leg), and steps 2–4 run as for a story with no carried verdict. Exit 0 or 3 (no profile leg configured — SKIPPED): no `--profile-leg`. Exit 2: end the window — release the lease, report the script's cause to the owner; the approval stands.
2. Final rebase onto the base branch, skipped when `git -C <worktree_path> merge-base --is-ancestor <base branch> <branch>` holds. Otherwise run `branch_overlap.py <branch> --base <base branch>`, then dispatch `story-rebase` with `target: <base branch>`, `validate: stack`, the OVERLAP and CONFLICTS lists, and the per-file conflict rules. `branch_overlap.py` exit 2 ends the window: release the lease and HALT with its stderr line; the approval stands. The uncommitted `checkpoint-3` record line takes the autostash path. A story recorded `pre-merge gate not required` is not rebased when the base branch is still an ancestor, unless the profile leg is due.
3. When the profile leg is not due: `uv run --no-project .workflow/scripts/merge_train.py settle <id>` when the latest pre-merge verdict is a train GREEN or `pre-merge gate not required — …`. Exit 0: the verdict carries over, the holder is marked `green` for `HEAD`, and step 4 is skipped. Exit 1: step 4. Exit 2: end the window — release the lease, report; the approval stands.
4. Gate: `uv run --no-project .workflow/scripts/run_full_suite.py --story-id <id> --record` (plus `--profile-leg` when due — also for a story recorded `not required`); it marks the holder's train `gating`, then `green` for `HEAD`. Exit 75 (no test ran): the approval stands; the lease is released unless the run printed a lease WARNING — then `land_lease.py holds <id>`: exit 0 → `land_lease.py release <id>` (a failing release: halt with its stderr line), no withdrawal line; exit 1 → nothing to do; exit 2 → release nothing and halt. Then read the preflight FAIL lines, bring the stack up, and reopen the window from step 1. A story recorded `not required` that is not due runs no gate: its window is `acquire` → `settle` → land-story.
5. Invoke the `land-story` skill with the story id; it releases the lease after its bookkeeping commit.

**A question to the owner inside the window ends the window.** Release the lease first (`land_lease.py release <id>`), say in the message that it is released and that the window restarts from `acquire` after the answer, then ask. A turn end that waits on a background task the window started (the gate, `story-rebase`, `merge_train.py wait`) keeps the lease. After the answer the window re-runs from step 1: `settle` carries a still-valid train GREEN over, otherwise the gate runs again (a window GREEN recorded before the wait is not reused). `.workflow/hooks/guards/land_lease_wait.py` enforces this on Claude Code: at Stop it blocks the turn end once, then releases the lease itself; it denies a question tool call while the story holds the lease before its merge. After the land-story merge the lease is kept to its bookkeeping step.

**A land-guard block inside the window:** the lease is still held. Re-run from step 2 (rebase, `settle`, then gate) and tell the owner the commits that moved the base branch. Write the override line instead only when the owner instructs it.

**The window ends early — lease released first, then reported — on:** a red gate (automatic; a killed gate: the lease check in the gate-failure path); a gate that ran no test (exit 75); `branch_overlap.py` exit 2; a `story-rebase` HALT; the owner ending the window; a question to the owner; and, before the merge, any land-story step that stops for the owner. After the land-story merge, a halt keeps the lease until its bookkeeping step completes; resume with `land-story <id> --cleanup-only`. A lease left by a dead session is released with `land_lease.py release <holder-id>` on the owner's instruction (a dead window gate's lease is released by a waiting `merge_train.py wait` itself). After a withdrawal, fix the cause, take a fresh Checkpoint 3 approval, and open a new window. After a preflight not-run no new approval is needed.

---

## Definition of Done

A story is complete only when:

1. Code + tests written in the worktree (`story-impl`)
2. `layered-review` run and findings triaged by the owner across **all rounds** that fired
3. Approved patches applied + stack logs clean (`story-finalize`, all rounds)
4. Scoped tests pass in the story's slot (`story-finalize`, after the final round)
5. Impl commit landed (`story-impl`); close-out commit landed if patches OR defers — possibly amended by later rounds or a gate fix. The branch ends with at most `config: git.max_commits_per_story` commits.
6. Pre-merge gate GREEN for the landed tree (or a recorded exemption)
7. Merged to `config: git.base_branch` with the owner's explicit confirmation, inside the land window (land-story)
8. Propagation applied, sprint status `done`, branch + worktree + stack removed, slot `free` (land-story)

---

## Forbidden

- Bringing up a slot stack without ALL port env vars from `config: stack.ports` (use `bring_up_story_stack.py`, never a bare compose `up`)
- Auto-applying code review patches (HARD RULE — Checkpoint 2)
- Deferring or dismissing a finding the verifier marked `bar impact: BLOCKING`
- Merging without the owner's explicit confirmation, or outside the land window (HARD RULE — Checkpoint 3)
- A human wait while holding the land lease
- Writing a `pre-merge gate GREEN` or `phase-<n> tests GREEN` line by hand
- Skipping the slot registry update, or branch/worktree/stack cleanup
- Letting a subagent return success while a service is exited/restarting or logs show fresh errors
- Dispatching a review agent without the required preamble — hook-blocked

---

## Running multiple stories concurrently

Each story occupies its own slot. Stories can be in different phases at once — e.g. story A waiting on Checkpoint 2 triage while story B is in Phase 2. Only the land window (`land_lease.py acquire` → land-story's release) is strictly one-at-a-time; a second story waiting to land queues on `merge_train.py wait` (first-come-first-served, unattended drivers in the same line) and gates its train while it waits.

To switch between in-progress stories: note the current story's phase and slot, switch, and pick up the other story from its current checkpoint (`story_ledger.py <story-id>`). Each story's state is durable on disk (worktree + stack + slot registry + record + findings file).

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `port is already allocated` | Slot collision, or a port var not passed | Check the registry; bring the stack up only via `bring_up_story_stack.py` |
| `container name already in use` | Hard-coded `container_name:` in the compose file | Remove the `container_name:` line |
| Service exited after `up` | App crashed on startup | `<config: stack.compose_cmd> -p <stack> logs --tail=30 <service>` |
| `git worktree remove` fails with "busy" after stack down | An orphan watcher process is pinning the worktree directory | Hand off to land-story, or run `uv run --no-project --with psutil .workflow/scripts/cleanup_story_stack.py <id>` |
| Subagent dispatch blocked by a guard | Review prompt missing the preamble, iterate-and-test phrasing, an unrecorded phase fact, a missing land lease, a train branch checked out, or the wrong effort variant | Read the guard's message; fix the prompt, record the fact, run `merge_train.py home <id>`, or re-dispatch with the variant it names |
