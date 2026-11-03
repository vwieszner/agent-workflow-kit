# Auto-Dev Loop — Fully Autonomous Story Pipeline

**Role:** Drive a milestone to completion without human checkpoints. Pick the next startable
story, run the full story lifecycle, merge, clean up, move on. Repeat until no startable story
remains.

The `parallel-dev-wave` skill is the controlled equivalent with three human gates. **This skill
removes those gates.** Multiple review passes, verification and meta-evaluation are the mitigation.

All scripts below are invoked as `uv run --no-project .workflow/scripts/<name>.py ...` from the main
checkout (`python3 .workflow/scripts/<name>.py ...` works too). Steps that name a script the project
has not installed (`land_lease.py`, `merge_train.py`, `branch_overlap.py`) are skipped and say so.

---

## Pre-authorized operations (authorized in advance by the owner — DO NOT ASK)

Every operation below is **pre-authorized**. Do NOT ask for confirmation on any of these — asking
IS a bug in this mode. Anything this list does not name is not pre-authorized.

- The story's design-owner ticket (§"Design ticket"), at any halt and in Step 4's design filing:
  the tracker search, create and comment calls (`tracker.py`, or the tracker MCP tools of
  `config: tracker.mcp_tools`) for that story's `(<config: design.question_tag>)` questions only, and
  `design_ticket.py` (`tracker-row`, `commit-tracker`, `mark-spec`).
- `halt_release.py` (the halt-and-release procedure): the WIP commit in the story worktree, the
  sprint-status flip to `blocked`, the failure-log entry, the lease release, the kill of the
  worktree's orphan `compose watch`, `{compose} -p <stack> stop` (containers stopped; logs and data
  volumes kept), the slot release, and its bookkeeping commit on `config: git.base_branch`.
- `bring_up_story_stack.py --mode down` (wipes the isolated stack's volumes) of a story that is
  **not** halted, and every other compose operation (up / restart / exec / build / logs / watch /
  ps) against a `<config: slots.stack_prefix>*` project. A halted story's stack is never wiped. A
  bare `{compose} ... down` is never run.
- The worktree removal and branch delete inside `cleanup_story_stack.py`, run by `land-story` after
  the merge. In an auto-dev-loop session the `auto_dev_keep` guard denies `git worktree remove`, a
  `<config: git.branch_prefix>*` branch delete, a delete of a worktree root, a container volume
  `rm`/`prune`, a `cleanup_story_stack.py` teardown of an unmerged worktree and a
  `bring_up_story_stack.py --mode down` of a halted story; a deny there is a halt-and-release, never
  a workaround.
- The `git merge <config: git.merge_style>` of the story branch into `config: git.base_branch`, run
  by `land-story`.
- Recursive delete (`rm -rf`; Windows `Remove-Item -Recurse -Force`) of any path inside a story
  worktree (never a worktree root), under any `config: loop.cleanup_paths` entry, or under the
  session's scratch/temp directory.
- Running `reserve_slot.py`, `release_slot.py`, `story_setup.py`, `wait_for_stack_ready.py`,
  `stack_preflight.py`, `cleanup_story_stack.py`, `log_failure.py`, `sprint_status.py`,
  `story_record.py`, `story_ledger.py`, `milestone_map_check.py`, and invoking `land-story`,
  `post-merge-smoke` and `milestone-blockers` (with `AUTO_MODE`).
- Appending the auto-labelled `checkpoint-1 spec approved (auto-dev-loop …)` and
  `checkpoint-3 merge approved (auto-dev-loop …)` facts and the other record lines this file names.
- `findings-evaluator`'s patch/defer/dismiss classification for every finding (no owner triage gate
  in this mode). A finding assessed as quality-bar-blocking is never deferred or dismissed: it is
  patched, or the story halts (Step 9). Bar-severity assignment is never pre-authorized.
- Recommended phases and priorities (marked `(recommended)`) from `milestone-map-refresh` and
  `story-spec`. A value the owner set is never changed.
- The `milestone-blockers` `AUTO_MODE` refresh: the `milestone-map-refresh` agent's edits to the
  dependency map, row notes and story history, committed as its own changes, and a
  `log_failure.py --failure-step map-refresh` entry per question it returns.
- Sprint-status flips between `backlog` ↔ `ready-for-dev` ↔ `in-progress` ↔ `review` ↔ `done`; any
  status → `blocked` (halt-and-release); a hand-blocked row → `in-progress` (Step 3b).
- When installed: `land_lease.py acquire` / `release` / `holds`, `merge_train.py`
  (`wait`, `home`, `settle`), `branch_overlap.py`, the `story-rebase` dispatch onto
  `config: git.base_branch` with `validate: stack` together with the conflict resolutions it makes
  under the 11c.3 rules, and the pre-merge gate `run_full_suite.py --story-id <id> --record` (with
  `--profile-leg` when `profile_leg.py due` exits 1).
- The resume path's git operations in the story worktree: Step 3a′'s plain rebase and its
  `git rebase --abort`; Step 7's repro clean-up; Step 10's fold of `wip:` and docs commits into the
  impl (or close-out) commit (`git commit-tree`, `git reset --soft`).

**NOT pre-authorized** (requires the owner before action):

- A wipe of the shared stack (`config: slots.shared_stack_name`) or any bare `{compose} ... down`.
- Force push (`git push --force`, `--force-with-lease`); any push to a remote; fetching from or
  merging from a remote.
- `git commit --no-verify`.
- Anything on the tool's permission deny list (`.claude/settings.json` / `opencode.json`).

**On encountering an obstacle:** apply the halt-and-release procedure and move to the next story.
Do NOT pause to ask the owner.

---

## Override of the human-gate rules

This skill operates under the owner's explicit override of two working rules:

- **Code-review triage is NOT human-gated here.** The `findings-evaluator` agent makes the
  patch/defer/dismiss call on the verified findings of each review round (one 3-pass `layered-review`
  run per round, at most three rounds).
- **Merge approval is NOT human-gated here.** If the tests are green, the loop merges via
  `land-story`.

Every other rule (root cause before fix, no test-weakening, dispatch-prompt discipline, surgical
changes) applies unchanged. The phase-gate guard (`.workflow/hooks/guards/phase_dispatch.py`) stays
on: the loop satisfies it with durable records that name the autonomous authority; it never writes
a record claiming a human approved or triaged anything.

A halt never deletes work or evidence. Halt-and-release keeps the branch (with its WIP commit), the
worktree and the stack (containers stopped; logs and data volumes kept) and releases only the slot.
A halted story's worktree and stack data are deleted only by `land-story` or by the owner's discard.

---

## Pipeline overview

```
[main session — auto-dev-loop]
  └─ LOOP:
       ├─ Step 1: milestone-blockers AUTO_MODE (refreshes the map when due) → pick from STARTABLE
       │     ↳ none → TERMINATE
       ├─ Step 2: refuse a halted story; reserve_slot.py (lock-protected)
       ├─ Step 3: story_setup.py --reserved-slot (worktree, seed files, spec lookup, graph index,
       │          async stack bring-up); 3a′ resumed story: apply the halt's answers, rebase, re-up;
       │          3b sprint status → in-progress
       ├─ Step 4: spec (story-spec, auto mode) + open-question check + checkpoint-1 record
       ├─ Step 5: wait_for_stack_ready.py → stack_preflight.py (the gate)
       ├─ Step 6: story-impl (tests-first; max 5 batch-fix rounds; phase-2 GREEN record; impl commit)
       ├─ Step 7: layered-review (mode auto, 3 passes) → findings file
       ├─ Step 8: re-verify UNVERIFIED findings (findings-verifier)
       ├─ Step 9: findings-evaluator → patch / defer / dismiss
       ├─ Step 9b: ## Triage decisions in the findings file
       ├─ Step 10: story-finalize (patches → scoped tests, max 5 rounds → close-out commit);
       │           a close-out review round follows while the ledger says one is due
       ├─ Step 11: phase-3 GREEN check → gate decision → story-propagate propose (parked) →
       │           schema check → checkpoint-3 record → land window → land-story
       └─ Step 12: back to Step 1
```

Any failure in Steps 3–11 → halt-and-release → Step 1.

---

## Inputs

None. The skill picks its own work.

- `config: loop.milestone` — the milestone `milestone-blockers` reports on. Empty → Step 1 takes
  the first `ready-for-dev` story in sprint status (file order).
- `config: loop.max_stories` — stories per invocation (`0` = unlimited); `AUTO_DEV_LOOP_MAX_STORIES`
  overrides it. Counts every story started (landed or halted). Driver mode ignores both (the driver
  has `--max-stories`).
- Args `driver story=<id|none>`: driver mode, below.

---

## Driver mode — one phase per session

`.workflow/scripts/auto_dev_driver.py` runs this skill as `/auto-dev-loop driver story=<id>` or
`/auto-dev-loop driver story=none`, each time in a fresh headless session. In driver mode:

1. **One phase per session.** Run the one phase this section selects, then end the session. Never
   start the next phase, and never loop back to Step 1, in the same session; after a
   halt-and-release, end the session.
2. **`story=none`:** run Steps 1–4 for one new story. The phase ends at the Step 4b record line.
3. **`story=<id>`:** continues only a story that holds a slot-registry row; a halted story holds
   none (the resume command resumes it and Step 1 picks it). When the story's row reads `blocked`,
   apply Step 2's halted check first; a halted story ends the session with `outcome=infra-stop`.
   When the row reads `ready-for-dev`, or is hand-blocked, and the story holds a slot-registry row,
   run Step 3b's flip with the slot from that row first. Then run
   `story_ledger.py <id>` and select the first matching phase from its "Handoff state" block, the
   `seed ACs due` / `review re-run due` / `review round due` lines after it, and the record file:

   | Ledger | Phase | Ends at |
   |---|---|---|
   | `spec approved` not ok, the story holds a slot | Step 4 | the Step 4b record line |
   | `spec approved` ok, `phase-2 tests` not ok | Steps 5–6 | the `phase-2 tests GREEN` record line |
   | `phase-3 tests` ok and a `seed ACs due: AC…` line | halt-and-release, `failure_step="resume-seed-ac"` | the failure entry |
   | `phase-2 tests` ok, `triage recorded` not ok and a `seed ACs due: AC…` line | Steps 5, then Step 6's seed-AC run | the `seed ACs implemented` record line |
   | `phase-2 tests` ok, no `review round 1 recorded` line in the record | Step 7 | the `review round 1 recorded` line |
   | a `review re-run due: round <m>` line | Steps 5, the Step 7 repro clean-up, then Step 7 (round `<m>`) | the `review round <m> recorded` line |
   | a `review round <n> recorded` line, `triage recorded` not ok | Steps 8, 9, 9b | the `## Triage decisions` section |
   | `triage recorded` ok, `phase-3 tests` not ok | Step 10 | the `phase-3 tests GREEN` record line |
   | a `review round due: round <n>` line | Step 7 (a close-out round) | the `review round <n> recorded` line |
   | `phase-3 tests` ok | Step 11 | `land-story` done, or `waiting` |

   The `phase-3 tests` row counts only a GREEN line recorded after the latest
   `review round <n> recorded` line. In Step 11, a sub-step whose record line already exists for the
   branch's current HEAD is not run again; the 11c window always re-runs from `acquire`. A story with
   no slot-registry row and no record: end with `outcome=infra-stop`.
4. **Inputs come from disk, never from an earlier session:** `branch_name`, `worktree_path` and
   `slot` from the story's slot-registry row; `stack_project_name` = `config: slots.stack_prefix` +
   `branch_name`; `graph_project` from `.workflow/state/story-setup/<stack_project_name>.graph_project`;
   `spec_path` and `findings_file` from the ledger's "Artifacts on disk"; Steps 8–9 read only the
   `### Summary` and `### Findings for triage` parts of the findings file's latest `## Review round <n>`
   section. The halt's answers: the latest `resumed halt` block of
   `sprint_status.py history <story-key>` (Step 3a′). The first phase after a resume that runs the
   halted step puts the block's ruling into that step's dispatch prompt (or the evaluator's input) and
   appends `halt answers dispatched <entry id>` to the record once the dispatch has started; a later
   phase skips the injection when that line exists.
5. **Final line.** End the session's last message with exactly:
   `AUTO-DEV-PHASE story=<id|none> outcome=<outcome> step=<last step run>`
   `story=` names the story this session drove (from Step 2's reservation on, a halt included);
   `none` only when no story was reserved. `<outcome>` is one of `advanced` (the phase's end record
   exists), `landed` (Step 11c finished), `halted` (halt-and-release ran), `waiting` (11c.1 found the
   lease held or stories ahead in the line; nothing torn down), `terminated` (Step 1 found no
   story), `infra-stop` (an "Allowed stops" case). The driver checks the record, not this line: an
   outcome other than `waiting` with no new record fact or failure-log entry stops the driver.
6. Every other rule of this file applies unchanged.

---

## Halt-and-release procedure

Used by every failure path in Steps 3–11, including any dispatch that returns output marked partial
(it hit its turn limit; never resume or retry it): `failure_step="<agent>-max-turns"`.

1. **Questions.** Every question the failure leaves open goes into the failure summary, numbered,
   and is tagged `(owner)` or `(<tag>)` (`<tag>` = `config: design.question_tag`; with no design owner
   configured every question is `(owner)`). The design owner's: a design value, a
   documentation-vs-design contradiction, an undrawn state, routing, a click target or other screen
   behaviour. The owner's: a product or scope decision, anything irreversible or outward-facing, two
   source-of-truth docs that contradict each other. `(<tag>)` questions go on the story's ticket first
   (§"Design ticket").
2. **Run the halt** (the main checkout's script, never a story branch's copy):
   ```bash
   uv run --no-project .workflow/scripts/halt_release.py --story-id "<story_id>" --failure-step "<failure_step>" --failure-summary "<one-sentence summary, then the numbered questions>" --asked-of "<owner|external|both>" --last-test-output "<last 30 lines or empty>"
   ```
   `--asked-of`: whose answers the halt waits on (`owner` for a failure that asks nothing, too).
   `external` and `both` need `--external-ticket <key> --external-item "<the tracker row's Item cell>"`;
   any non-empty `--external-ticket` must be one ticket key (`config: paths.design_ticket_regex`).
   It keeps the worktree, branch and stack and releases only the slot: draft marker (for a
   `story-spec*` step), WIP commit, the sprint-status note's `HALTED <date> at <step>; worktree kept`
   clause (status `blocked`), the failure entry, lease release, orphan watch kill, stack stop, slot
   release, and one commit of the story's own bookkeeping on `config: git.base_branch`. Never run its
   steps by hand.
3. **Exit 0** (`"status": "halted"`): done. **Exit 1** (`"status": "partial"`): its JSON names each
   failed step; stop the loop and report them (driver mode: `outcome=infra-stop`). **Exit 2**
   (`"refused"`): a missing `--asked-of`, a bad `--external-ticket`, or a line break in an argument;
   fix the arguments and run again.
4. **Loop back to Step 1.** The failed story has a failure-log entry; `milestone-blockers` reports it
   as halted, never as startable.

**Resume** (the owner, once the questions are answered — the design owner's part when the ticket's
tracker row reads done):
`uv run --no-project .workflow/scripts/resume_halted.py <story_id> --ruling '<the ruling>'`
(`--no-ruling` when the halt asked nothing or only the design owner; the ruling goes in single quotes,
each single quote inside it written `'\''`). `resume_halted.py` refuses while the design owner's part
is open. It archives the entry with the ruling in the story history, removes it,
sets the row `ready-for-dev` and commits; Step 1 can then pick the story, Step 3 reuses the worktree,
Step 3a′ applies the ruling. **Discard** (the owner only):
`uv run --no-project .workflow/scripts/cleanup_story_stack.py <branch_name>`.

### Design ticket

With a design owner configured (`config: design.question_tag`) and a tracker (`config: tracker.backend`
not `none`), invoke the `design-ticket` skill: one ticket per story, only the questions it does not
already carry, the tracker row, the spec markers. In this loop its `origin` is
`auto-dev, the <story_id> <step>`.
- At a halt, pass the ticket to `halt_release.py --external-ticket <key> --external-item "<the tracker's
  Item cell>"` and skip the skill's tracker step: the halt writes and commits the row.
- A failed tracker call: halt without `--external-ticket` (`--asked-of owner`), the error quoted in the
  failure summary. The driver's audit then stops the run; the interactive loop reports it and goes on.
- No design owner or no tracker configured: `(<tag>)` questions are the owner's; halt with
  `--asked-of owner`.

---

## Workflow

### Step 1 — Pick the next story

**Milestone configured:** invoke the `milestone-blockers` skill with args `AUTO_MODE`. When one of its
refresh reasons holds, it refreshes the map before writing the report. Read its machine-readable
tail: take `STARTABLE`, drop stories whose slot-registry row is `in_use` (read the registry), and
pick `RECOMMENDED` if it survives, else the first remaining `STARTABLE` story. Never pick outside
`STARTABLE`.

**No milestone configured:** run
`uv run --no-project .workflow/scripts/sprint_status.py list --status ready-for-dev --exclude-epics --json`
and pick the first row (file order) that holds no slot and has no failure-log entry.

No candidate: emit `auto-dev-loop terminating — no startable stories left` and stop
(driver mode: `outcome=terminated`).

### Step 2 — Refuse a halted story, then reserve the slot

**Halted check** (before anything is reserved). Read the story's sprint-status row
(`sprint_status.py get <id>`) and the failure log (`config: paths.auto_dev_failure_log`; headings
`## <id> — <timestamp> — <step>`, dotted and dashed ids alike). A `blocked` row is **halted** when
the failure log holds an entry for the story (the resume command has not run) or its note carries a
`HALTED <date> at <step>` clause with no entry (a failed halt). A `blocked` row with neither is
**hand-blocked** and goes on like any other. A halted story: stop and tell the owner (to run
`resume_halted.py <story_id>` when the entry stands; a failed halt is theirs to rule on); in driver
mode end with `outcome=infra-stop`. An unreadable sprint-status file or failure log is an "Allowed
stops" case, never "not halted".

```bash
uv run --no-project .workflow/scripts/reserve_slot.py --story-id "<story_id>"
```

Parse the single-line JSON:
- `reserved` — take `slot`, `branch_name`, `worktree_path`, `stack_project_name`. Step 3.
- `already_reserved` — another terminal took it. Back to Step 1.
- `no_free_slot` — wait 60s with a timestamped heartbeat line, then re-run. After 5 retries emit
  `auto-dev-loop terminating — no free slot and 5min wait exhausted` and stop.
- `error` — infrastructure issue: `log_failure.py --story-id <id> --branch-name none --failure-step reserve-slot …`
  and stop the loop, surfacing the message.

### Step 3 — Setup

**3a — Run the setup script** (the one `parallel-dev-wave` uses):

```bash
uv run --no-project .workflow/scripts/story_setup.py <story_id> --reserved-slot <slot>
```

It creates the worktree off `config: git.base_branch` (reusing an existing one), seeds
`config: git.worktree_seed_files`, locates the spec, indexes the worktree into the code graph
(when `config: graph.tool` is not `none`), launches the async stack bring-up and returns in seconds.
Do not start `compose watch` yourself. Parse its `key: value` report for `worktree_path`,
`stack_project_name`, `graph_project`, `spec_path` and `spec_present`; pass `graph_project` to every
later dispatch that reads worktree code.

| Result | Action |
|---|---|
| exit 0, `graph_project` set (or graph disabled) | 3a′ |
| exit 0, `graph_project: <indexing failed>` | halt-and-release, `failure_step="setup-graph-index"` |
| exit 1 | halt-and-release, `failure_step="setup"`, quoting the failure line |
| exit 3 (several spec candidates) | halt-and-release, `failure_step="setup-spec-ambiguity"`, naming them |
| exit 4 (the slot is not the one Step 2 reserved) | another session owns the story: release nothing, touch nothing; back to Step 1 (driver: `outcome=infra-stop`) |

**3a′ — Resumed story: apply the halt's answers, rebase onto the base branch, re-up.**

When `merge_train.py` is installed, first run `merge_train.py home <story_id>` (a worktree left on a
train branch goes back to the story branch; exit 2: halt-and-release, `failure_step="resume-rebase"`).

Then apply the answers. Read the latest `resumed halt` block of
`sprint_status.py history <story-key>` (the halted entries verbatim after a `ruling:` line). Two kinds
of answer apply: the block's `ruling:` line (the owner's), and the design owner's rulings on the
`external_ticket` of the block's latest entry, read from the ticket (`tracker.py comments --key <key>`)
and the design-question tracker. An answer that lives in the spec lands there now: an `[OPEN-Q <n>]`
marker, or an `OPEN-Q <n> (<tag>, interim ⚠️, <key>)` marker, is replaced by its ruling. Any other
answer goes into the dispatch prompt (or the evaluator's input) of the halted step, built by the
session that runs it. Append `halt answers applied <entry id>` with `story_record.py append` and commit
the spec edit and the record line on the story branch (`docs: [Story <story_id>] halt answers applied`).
Skip the spec edit, the append and the commit when
`git -C <worktree_path> log <branch> -S"halt answers applied <entry id>" --oneline` lists a commit.
An answer that does not settle its question: halt-and-release with
`failure_step="halt-answer-unclear"`, quoting the question and the answer. No `resumed halt` block:
skip this paragraph.

Then `git -C <worktree_path> merge-base --is-ancestor <config: git.base_branch> <branch>`: exit 0 →
3b. Exit 1 → 1–5 below. Any other exit: halt-and-release, `failure_step="resume-rebase"`.

1. **Wait** for the bring-up: `wait_for_stack_ready.py <stack_project_name>` (non-zero: halt-and-release,
   `failure_step="stack-bring-up"`).
2. **Docs-only branch** (every path of `git diff --name-only <base>...HEAD` is under `docs/` or
   `config: paths.specs_dir`): `git -C <worktree_path> rebase <base>`; a conflict
   (`git rebase --abort`) or refusal: halt-and-release, `failure_step="resume-rebase"`, naming the files.
3. **Otherwise**: run `branch_overlap.py <branch> --base <base> --repo <worktree_path>` when installed,
   then dispatch `story-rebase` with `target: <base>`, `validate: stack`,
   `range_diff_path: <worktree_path>/.story-setup/range-diff-<story_id>-<n>.txt`, the OVERLAP and
   CONFLICTS lists, 11c.3's per-file rules, `slot`, `stack_project_name`, `graph_project`, and every
   commit of `<base>..HEAD` oldest first (`wip:` commits included). Resolved conflicts: append
   `resume rebase onto <sha12>: <n> conflicted files resolved — <file>: <resolution>; …`. A
   `story-rebase` HALT: halt-and-release, `failure_step="resume-rebase"`, with the file:line and options.
4. **Re-up** after either rebase: `bring_up_story_stack.py --slot <slot> --project-name <stack> --worktree <worktree_path> --mode up`
   (non-zero: halt-and-release, `failure_step="resume-re-up"`); append
   `resume rebase onto <sha12> (<plain | story-rebase>); re-up ok`.
5. **Re-index** the worktree in the code graph (when enabled); a failure: halt-and-release,
   `failure_step="setup-graph-index"`.

**3b — Sprint status → `in-progress`** (key-addressed; never a line-number edit):

```bash
uv run --no-project .workflow/scripts/sprint_status.py set <story-key> in-progress --note "STARTED <YYYY-MM-DD> slot <N> (<branch>); <phase/priority marker>; dep: <ids or none>; auto-dev-loop"
uv run --no-project .workflow/scripts/sprint_status.py touch --note "<story-id> started (slot <N>, auto-dev-loop)."
```

A key missing from sprint status: skip the flip and continue. A failed flip: halt-and-release,
`failure_step="setup"`.

### Step 4 — Spec + checkpoint-1 record

**4a — Spec.** With `spec_present: true`: when the project has the `ui-state-reach` skill and
`story_record.py reach <story_id>` exits 1 (`MISSING`: a frontend story whose spec has no "UI states
and how each is reached" table), invoke `ui-state-reach` with
`spec=<spec_path> worktree=<worktree_path> graph_project=<graph_project> mode=auto` and commit the spec
(`docs: [Story <story_id>] UI states reach table`). Then the open check:

```bash
uv run --no-project .workflow/scripts/story_record.py open <story_id>
```

- Exit 0 → 4b.
- Exit 1 with `[OPEN-Q` lines that are all `(<tag>)` (design owner configured): run the **design
  filing** below, then re-run the check. A marker the filing leaves bracketed (filed with the interim
  `none — the story waits for the design`: a missing layout or screen) halts with
  `failure_step="spec-open-questions"`, `--asked-of external` and the ticket.
- Exit 1 with other `[OPEN-Q` lines: halt-and-release, `failure_step="spec-open-questions"`, every
  marked question in the failure summary (`--asked-of owner`, or `both` with the ticket when `(<tag>)`
  markers stand too).
- Exit 1 with only a `spec-draft incomplete` record line: dispatch `story-spec` as below with
  `existing_draft: true`; then items 2–4.
- `no spec found` (exit 1 or 2): halt-and-release, `failure_step="spec-check"`.
- Exit 3: read each `CHECK` line with three lines either side in the spec. A line that leaves a
  question undecided and names no value for the story to build is an open question: halt-and-release,
  `failure_step="spec-open-questions"`, quoting it. A line that names what the story builds (an
  interim value, a deferral to another story, a settled history note) is not.

With `spec_present: false`:
1. When `story_research_pack.py` is installed, build the research pack:
   `story_research_pack.py <story_id> --worktree <worktree_path>`. Dispatch the `story-spec` subagent
   (on a failed dispatch retry once per the `parallel-dev-wave` model-fallback policy) with: story id
   + title, the epic AC block (`config: paths.epics_dir`), `worktree_path`, `graph_project`,
   `spec_target_path` = `<worktree_path>/<config: paths.specs_dir>/<story-key>.md`, `mode: auto`,
   `elicitation_rounds: 3`, `research_pack` (when built), and this line: `Run every
   advanced-elicitation round non-interactively with the critique-pass args — critique lists only, no
   method menu, no Apply/Reject pause; you revise the spec once with the union.` The agent records
   best-effort decisions (the recommended option) in the spec and marks a question that is the
   owner's call `[OPEN-Q <n>]`; it returns `spec_path`, `elicitation_rounds` and `open_questions`.
2. Set `spec_path`; record `spec-draft complete (story-spec auto mode, <N> elicitation rounds)`.
3. If `open_questions` holds any `(<tag>)` question, run the **design filing** first: a value or a
   state carries story-spec's derived interim; a missing layout or screen is filed with the interim
   `none — the story waits for the design`. Then, if it holds an owner question or a `(<tag>)` question
   filed with no interim: halt-and-release, `failure_step="spec-open-questions"`, every numbered
   question in the failure summary, `--asked-of` from who the remaining questions wait on. The WIP
   commit keeps the draft with its markers.
4. A failed dispatch after the retry: halt-and-release, `failure_step="story-spec"`.

**Design filing.** File every `(<tag>)` question the story's ticket does not already carry
(§"Design ticket"); a question already on it is not posted again. For a new ticket, add its row to the
design-question tracker and commit only that hunk (`design_ticket.py tracker-row` + `commit-tracker`),
then rewrite the spec's markers (`design_ticket.py mark-spec`) and commit the spec on the story branch
(`docs: [Story <story_id>] design questions filed (<key>)`). The interim ships ⚠️ until the design owner
rules; `design-sync` routes the ruling later.

**4b — Record checkpoint-1.** The loop stands in for the owner's spec approval. Append exactly one:

```bash
uv run --no-project .workflow/scripts/story_record.py append <story_id> "checkpoint-1 spec approved (auto-dev-loop, story-spec auto mode, <N> elicitation rounds complete)"
uv run --no-project .workflow/scripts/story_record.py append <story_id> "checkpoint-1 spec approved (auto-dev-loop standing authorisation, existing spec used as-is)"
```

The first after a `story-spec` dispatch, the second when the spec was present. A refusal (an
`[OPEN-Q]` marker, an incomplete draft): halt-and-release, `failure_step="spec-open-questions"`, with
the refusal's lines. The phase-gate guard blocks the Step 6 dispatch until this line exists.

### Step 5 — Stack gate

**5a** — `wait_for_stack_ready.py <stack_project_name>`. Non-zero (bring-up failed, timeout, stack
gone): halt-and-release, `failure_step="stack-bring-up"`, quoting the error and log tail. Skipped,
and said so, with `config: stack.runtime` = `none`.

**5b** — the gate:

```bash
uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack_project_name> --worktree <worktree_path>
```

Exit 0 → Step 6. Exit 1 → optionally dispatch `dev-preflight` (diagnosis only) with the FAIL lines,
put its diagnosis in the failure summary, then halt-and-release, `failure_step="stack-preflight"`.
The script is the gate; the agent never is.

### Step 6 — `story-impl`

Dispatch the `story-impl` subagent. Pass, each on its own line: `story_id: <story_id>`,
`branch_name`, `worktree_path`, `stack_project_name`, `slot`, `graph_project`, `spec_path`. Append:

> AUTO MODE: Maximum 5 batch-fix rounds. If round 5 still shows failing tests, STOP. Stage all current changes with `git add -A`, then commit with subject `wip: [Story <story_id>] auto-loop halted at test-fix round 5`. Report the failure with the last 30 lines of test output. Do NOT return success.

- **Success** (impl commit present, tests green, `phase-2 tests GREEN` record line): Step 7.
- **`story-impl handoff — context cap`**: record it as `parallel-dev-wave` Phase 2 "Context-cap
  hand-off" does, then dispatch a fresh story-impl (the variant `parallel-dev-wave` "story-impl
  escalation" names; the `agent_dispatch` guard enforces it) with the same inputs plus
  `handoff: <handoff_file>` and the cap extended with `Rounds already used by earlier runs: <rounds_used>.`
  A third hand-off is a halt.
- **WIP halt or any other halt**: halt-and-release, `failure_step="story-impl"`, with the test output.

**The seed-AC run.** A story implemented before its spec gained a seed AC gets the AC implemented by
a story-impl run scoped to it, when `story_ledger.py <story_id>` prints `seed ACs due: AC…` (a story
past phase-3 halts with `failure_step="resume-seed-ac"`):
1. `<worktree_path>/.story-setup/<story_id>-phase-2.command` must exist; missing: halt-and-release,
   `failure_step="resume-seed-ac"`.
2. Commit an uncommitted spec: `docs: [Story <story_id>] seed ACs AC<a>, AC<b>`.
3. Write `<worktree_path>/.story-setup/seed-acs-<story_id>.md` with `seed_acs: AC<a>, AC<b>`,
   `done: <every other AC id>`, `green_command: <the saved phase-2 command, verbatim>`.
4. Dispatch story-impl with Step 6's inputs plus `seed_acs: <that file>` and
   `seed_base: <git rev-parse HEAD>`, and the AUTO MODE cap with the WIP subject
   `wip: [Story <story_id>] auto-loop halted at seed-AC test-fix round 5`.
5. Success (a report with a `seed_acs:` line): re-up the stack (`bring_up_story_stack.py --mode up`)
   and append `seed ACs implemented: AC<a>, AC<b> (<impl sha12>); re-up ok`. Anything else:
   halt-and-release, `failure_step="resume-seed-ac"`, naming each seed AC and the output.

### Step 7 — `layered-review` (one invocation, 3 passes)

**Re-run repro clean-up.** A run for the ledger's `review re-run due: round <m>` first deletes the
untracked repro test files earlier rounds left (names matching `config: review.repro_test_name_regex`),
`git rm`s the repro-named files the story's halt WIP commits added (commit
`wip: [Story <story_id>] drop unadopted repro tests before review round <m>`; none, no commit), and
confirms each is gone from its container.

Invoke the `layered-review` skill once with:

```
story_id: <story_id>
mode: auto
passes: 3
worktree: <worktree_path>
spec_file: <spec_path>
graph_project: <graph_project>
stack_project_name: <stack_project_name>
```

The skill stages the round (its number, kind and diff: a branch round over `<merge-base>..HEAD`, or a
close-out round over its delta), runs every layer in 3 independent passes, dedupes in code, has
`findings-verifier` verify every deduped finding, has `repro-writer` write a failing test per
non-refuted runtime finding where the project has it, writes the findings file and returns
`failed_layers`, `skipped_layers`, the `ui_*` accounting lines (when a UI verifier ran), `unverified`,
`counts`, `findings_file`, the `triage` rows and the `refuted` block. REFUTED findings never reach the
evaluator. Findings of halted rounds are carried into the re-run round, marked `carried:<id>`.

**Coverage rule.** A layer missing in 1 of 3 passes: proceed (the summary names it). A layer missing
in 2 or more passes: halt-and-release, `failure_step="code-review-layer-missing-2-of-3-passes"`.

**UI rule** (when the review ran a UI verifier). No story merges with a UI state left unchecked: an
unreached state, an `ui_*` accounting line that lists anything or reads `not reported by this run`, a
failed UI-verifier layer, or one skipped for any reason other than `no changed frontend source` →
halt-and-release, `failure_step="ui-states-unchecked"`, naming each.

The skill stops without a result: halt-and-release, `failure_step="layered-review"`. It stops because
the project's review workflow tool is unavailable: stop the loop ("Allowed stops").

**The review preamble** — Steps 8 and 9 lead their dispatch prompts with
`config: review.required_preamble` verbatim, then: "In this run the findings-evaluator agent performs
triage; defer only when the fix needs scope outside this story or a design or spec decision."

### Step 8 — Re-verify UNVERIFIED findings

Empty `unverified`: skip. Otherwise dispatch `findings-verifier` once with every unverified finding
inline (the finding's section of the latest round, never the file path; no model fallback; retry a
failed dispatch once). Update the triage rows with the verdicts. A finding re-verified REFUTED goes to
Step 9b's `dismissed_findings`. Any finding still without a verdict: halt-and-release,
`failure_step="code-review-unverified-finding"`.

### Step 9 — `findings-evaluator`

No triage rows and no UI out-of-scope claims: go to Step 9b with all lists `none`. Otherwise dispatch
`findings-evaluator`: the preamble, the sentence "This dispatch comes from auto-dev-loop.", then
`story_id`, `round: <n>`, the round's Summary lines, `worktree_path`, `spec_path`, `graph_project`,
`findings_file`, the quality-bar doc (`config: paths.quality_bar_doc`, or "none configured") and the
triage rows inline (UI out-of-scope claims with the UI diff path, when present). A close-out round is
triaged under the `rules-defer` skill's auto mode when the project has it.

It returns patches as manifest rows (`finding_id | file | line | change | notes | repro_test`), defers
and dismissals, each with one-line reasoning. **There is no human triage step.**

- Evaluator fails (after the model-fallback retry): halt-and-release, `failure_step="findings-evaluator"`.
- A finding it flags as needing human review, or a defer that needs a new story or an owner decision:
  halt-and-release, `failure_step="findings-evaluator-needs-human"`, naming each id and question.
- A rejected or unruled UI out-of-scope claim: halt-and-release, `failure_step="ui-states-unchecked"`.
- **Bar-blocking findings.** With no quality bar configured this gate is skipped (say so in 9b).
  Otherwise the evaluator never defers or dismisses a finding it assesses as bar-blocking: it patches
  it, and returns it under `## Bar-blocking — needs human triage` only when the verifiers split, when
  no fix exists without scope the spec lacks, or when the fix contradicts the spec or a record. A
  non-empty list: halt-and-release, `failure_step="bar-blocking-finding"`, quoting each entry's bar
  and consequence. Severity is never pre-authorized.

### Step 9b — Persist the triage decisions

Write each defer's tracking row first, in the main checkout (never a story worktree): a row in the
project's backlog or the epic of a not-done story naming `<story_id> <finding_id>`, committed as your
own change; fill its `tracked in` into the defer. Then append to `findings_file`:

~~~markdown
## Triage decisions — <YYYY-MM-DD>

Triaged by findings-evaluator (auto-dev-loop); no human triage. Patched <X>, deferred <Y>, dismissed <Z>.
Bar gate: <applied against <quality bar doc> | skipped (no quality bar configured)>

### approved_patches (patch manifest)

```
<finding_id> | <file (repo-relative)> | <line> | <change — one imperative sentence> | <notes> | <repro_test or ->
```

### deferred_findings

- **<finding_id>** — <title> — bar impact: none — waits on: <scope or decision> — tracked in: `<path>` — `<key>` — reasoning: <one line>

### dismissed_findings

- **<finding_id>** — <title> — reasoning: <one line>

### ui_out_of_scope_rulings

- <state> — <kind> — accepted | rejected — reasoning: <one line>
~~~

Write all four headings every time (`none` under an empty one). The phase-gate guard blocks the
`story-finalize` dispatch until the section exists; `story-finalize` commits it with the patches.

### Step 10 — `story-finalize`

**Fold a resumed branch first.** When `git log --format=%s <base>..HEAD` lists a
`wip: [Story <story_id>]` commit, or more than one commit and one of them a docs commit, or more
than one commit in story-impl's format, fold the `wip:` and docs commits into the impl commit by
`parallel-dev-wave`'s "A story resumed after an auto-dev-loop halt", step 2 (once a close-out commit
exists, a later commit folds into it). A failed fold: halt-and-release, `failure_step="resume-fold"`.

Dispatch `story-finalize` with `story_id`, `branch_name`, `worktree_path`, `stack_project_name`,
`graph_project`, `approved_patches` (state the row count), `deferred_findings`, `dismissed_findings`,
`last_round: yes`, and append:

> AUTO MODE: Maximum 5 batch-fix rounds after applying patches. If round 5 still shows failing tests, STOP. Stage all current changes with `git add -A`, then commit with subject `wip: [Story <story_id>] auto-loop halted at finalize test-fix round 5`. Report the failure with the last 30 lines of test output. Do NOT return success.

- **Success**: when `story_ledger.py <story_id>` prints `review re-run due: round <m>` or
  `review round due: round <n>`, go to Step 7 for that round (driver mode: end the session);
  otherwise Step 11.
- **WIP halt or any failure**: halt-and-release, `failure_step="story-finalize"`. Never invoke
  `land-story`.

### Step 11 — Checkpoint-3 record, land window, `land-story`

**11a** — `story_record.py check <story_id> phase-3`. Exit 1: halt-and-release,
`failure_step="phase-3-no-green-record"`.

**11a′ — Gate decision.** When the working rules' "Pre-merge full gate" does not require the gate for
this change, record `pre-merge gate not required — <the clause that applies>`; when in doubt it is
required and nothing is recorded.

**11a″ — Propagation, parked for the owner.** Dispatch `story-propagate` with `mode: propose` (the
inputs of `parallel-dev-wave`'s Propagation section). Write its checklist to
`<worktree_path>/.story-setup/propagation-<story_id>.md`, never with a `## Decisions` section. For
each `Cascades:` entry, name its target story:
- **Target holds an `in_use` slot**: halt-and-release, `failure_step="propagation-cascade"`, with the entry.
- **Target holds no slot and has a spec** in its kept worktree: append
  `[OPEN-Q <n>] cascade from <story_id> (propagation P<k>): <the entry verbatim>` to that spec's open
  questions and commit it on the target's branch.
- **Target has no spec**: `sprint_status.py history <target> --append "Cascade from <story_id> (P<k>): <the entry>"`
  and commit only that change.

Pick no option. Record `propagation cascades moved: …` (when any) and
`propagation parked for the owner: <n> rows in propagation-<story_id>.md; NEXT: /propagate-story-changes story=<story_id>`.

**11a‴ — Schema check.** When `config: pipeline.schema_globs` is set and
`git diff --name-only <base>...HEAD` matches one: read the record and spec for the kind of change.
Additive and stated: go on. Structural (rename, type change, dropped column, key change) or unstated:
halt-and-release, `failure_step="migration-reset-structural"`. Never infer the kind from the diff.

**11b — Record checkpoint-3**: `checkpoint-3 merge approved (auto-dev-loop standing authorisation)`.
After a `checkpoint-3 approval withdrawn -- train gate exit <rc>` line no new authorisation is
recorded: 11c.1 halts instead.

**11c — Land window.** One story at a time holds the lease from `acquire` to `land-story`'s release.
Without `land_lease.py` installed, steps 1–3b are skipped (say so) and the gate (step 4) runs directly.

1. `land_lease.py acquire <story_id>` (one attempt, foreground). Exit 0: step 2. Exit 1 (no standing
   approval): a `withdrawn -- train gate exit` verdict → halt-and-release, `failure_step="train-gate"`;
   a `withdrawn -- pre-merge gate` verdict → `failure_step="pre-merge-gate"`; otherwise
   `failure_step="land-lease-no-approval"`. Exit 2: halt-and-release, `failure_step="land-lease-error"`.
   Exit 3 (lease held, or stories ahead):
   - Driver mode: end with `AUTO-DEV-PHASE story=<story_id> outcome=waiting step=11c.1`. Nothing is
     torn down; the driver waits in the line, builds and gates the train, and starts a fresh session
     at the story's turn.
   - Without the driver: run `merge_train.py wait <story_id>` in the background and wait for its exit.
     Exit 0: step 2. Exit 3 (a stalled holder or head of line): halt-and-release,
     `failure_step="land-lease-timeout"`. Exit 4 (a red train gate): `failure_step="train-gate"`.
   - Before step 2, every window: `merge_train.py home <story_id>` (exit 2: `failure_step="land-window-rebase"`).
   - Then `profile_leg.py due`. Exit 1: the gate in step 4 adds `--profile-leg`, and a story with a carried
     verdict (step 3b) or recorded `not required` gates anyway. Exit 0 or 3 (not configured): no
     `--profile-leg`. Exit 2: halt-and-release, `failure_step="profile-leg-due-unknown"`.
2. Fold a resumed branch as in Step 10 (a commit after the close-out folds into the close-out). A
   failed fold: `failure_step="resume-fold"`.
3. **Final rebase.** Skip when `merge-base --is-ancestor <base> <branch>` holds. Otherwise:
   1. `branch_overlap.py <branch> --base <base> --repo <worktree_path>` (exit 2:
      `failure_step="land-window-rebase"`).
   2. Dispatch `story-rebase` with `target: <base>`, `validate: stack`, `range_diff_path`, the OVERLAP
      and CONFLICTS lists, and these per-file rules:
      - Any per-file rule `story-rebase` or this dispatch names decides first.
      - For each listed file, the spec sections that govern it.
      - Always a HALT: a conflict in a dependency manifest or lockfile (`config: env.dependency_files`);
        they are never edited by hand.
      - Mechanical, resolve it: both sides' changes can be kept in full (independent additions, or
        edits to different lines git reported as one hunk).
      - A design or scope call, HALT with file:line and the options: keeping both sides is impossible,
        or the result would change behaviour neither side wrote.
   3. Resolved conflicts: append `land-window rebase onto <sha12>: <n> conflicted files resolved — <file>: <resolution>; …`.
      A HALT: halt-and-release, `failure_step="land-window-rebase"`.
3b. When the profile leg is not due and the latest pre-merge verdict is a train GREEN or `pre-merge gate not required — …`, run
   `merge_train.py settle <story_id>`: exit 0 skips step 4; exit 1 → step 4; exit 2:
   `failure_step="land-window-rebase"`.
4. **Gate** (unless exempt and not due): `run_full_suite.py --story-id <story_id> --record` (plus
   `--profile-leg` when due). All green records
   `pre-merge gate GREEN` itself. A non-zero exit withdraws the checkpoint-3 approval and releases the
   lease; when the run was killed before its exit handler (`land_lease.py holds <story_id>` exits 0),
   append `checkpoint-3 approval withdrawn -- pre-merge gate exit <rc>` and release the lease yourself.
   Then halt-and-release, `failure_step="pre-merge-gate"`, with the failing test ids. A run that could
   not start the gate (preflight failed) withdraws nothing: halt-and-release,
   `failure_step="pre-merge-gate-not-run"`, with the preflight FAIL lines.
5. Invoke the `land-story` skill with `<story_id>`: merge → `post-merge-smoke` → cleanup → sprint
   status `done` → lease release.

A `land-story` dispatch blocked because the base branch moved past the gated base: re-run 11c from
step 3 once, keeping the lease; a second block: halt-and-release, `failure_step="land-guard"`. Never
write a `pre-merge base accepted` override line.

`land-story` halts on a merge conflict or a smoke failure: never retry the merge, never resolve it.
Inspect what completed — if the merge landed but smoke failed, say in the failure summary that the
merge is already in the base branch. Then halt-and-release,
`failure_step="land-story-<merge-conflict|smoke-failure>"`.

On success the slot is `free` and sprint status reads `done`.

### Step 12 — Next iteration

Increment the started-story count, check the cap, and loop back to Step 1.

---

## Forbidden in this mode

- **Do NOT ask the owner for confirmation** at any step — except under "Allowed stops".
- **Do NOT skip the failure-log entry** when halting. It is the owner's only record of what went wrong.
- **Do NOT resolve a conflict that needs a design or scope call**, and never resolve a `land-story`
  merge conflict.
- **Do NOT delete the branch, worktree or stack data** of a halted story.
- **Do NOT write a record line or a `## Triage decisions` section the loop has not reached** to get
  past a phase-gate block: a blocked dispatch is a halt-and-release, `failure_step="<step>-phase-gate"`,
  naming the missing fact.
- **Do NOT remove an `[OPEN-Q]` marker's brackets, or write a `spec-draft complete` line,** to get past
  a checkpoint-1 refusal. Only a ruling replaces a marker.
- **Do NOT write a record line claiming a human approved or triaged anything.**
- **Do NOT disable or bypass a guard.** A guard block on a story is a halt-and-release,
  `failure_step="guard-blocked-<guard>"`.

---

## Allowed stops (surface to the owner)

These are infrastructure problems, not story decisions — stop the loop (driver mode:
`outcome=infra-stop`):

- `reserve_slot.py` returns `error`.
- Every slot stays `in_use` for more than 5 minutes.
- `milestone-blockers` itself errors (a missing milestone key, an unreadable map, an unreadable
  failure log or sprint status — not an empty `STARTABLE`, which is clean termination), or
  `sprint_status.py list` errors.
- Step 2 refuses the picked story as halted.
- `halt_release.py` exits 1 (`partial`).
- The review stops because the project's review workflow tool is unavailable in this session.
