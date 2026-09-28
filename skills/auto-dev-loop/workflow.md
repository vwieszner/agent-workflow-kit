# Auto-Dev Loop — Fully Autonomous Story Pipeline

**Role:** Drive a milestone to completion without human checkpoints. Pick the next startable
story, run the full story lifecycle, merge, clean up, move on. Repeat until no startable story
remains.

The `parallel-dev-wave` skill is the controlled equivalent with three human gates. **This skill
removes those gates.** Multiple review runs plus meta-evaluation are the mitigation.

All scripts below are invoked as `uv run --no-project .workflow/scripts/<name>.py ...` from the
repo root (`python3 .workflow/scripts/<name>.py ...` works too).

---

## Pre-authorized operations (authorized in advance by the owner — DO NOT ASK)

Every destructive operation invoked by this skill is **pre-authorized**. Do NOT ask for
confirmation on any of these — asking IS a bug in this mode.

**Pre-authorized:**

- `bring_up_story_stack.py --mode down` / `<config: stack.compose_cmd> -p <config: slots.stack_prefix>* down -v` (wipes the isolated stack's volumes)
- Every compose operation (up / down / restart / exec / build / logs / watch / ps) against a `<config: slots.stack_prefix>*` project
- `git worktree add` / `git worktree remove --force` of a path under `config: git.worktrees_root`
- `git add -A` + WIP commits on the story branch, inside the story worktree (halt-and-release)
- `git branch -D <config: git.branch_prefix><id>` — ONLY for stories closed via `land-story`; halted stories keep their branch (halt-and-release)
- `git merge <config: git.merge_style>` of the story branch into `config: git.base_branch`
- Recursive delete (`rm -rf`; Windows `Remove-Item -Recurse -Force`) of any path under `config: git.worktrees_root`, under any `config: loop.cleanup_paths` entry, or under the session's scratch/temp directory
- Running `reserve_slot.py`, `release_slot.py`, `bring_up_story_stack.py`, `stack_preflight.py`, `cleanup_story_stack.py`, `log_failure.py`, `sprint_status.py`, `story_record.py`
- Appending the auto-labelled `checkpoint-1 spec approved` and `checkpoint-3 merge approved` facts to the story record (Steps 5 and 11)
- Invocations of `land-story`, `post-merge-smoke`, and the halt-and-release procedure
- `findings-evaluator`'s patch/defer/dismiss classification for every finding (no owner triage gate in this mode). **Exception: a finding assessed as quality-bar-blocking is NOT auto-triageable** — it is patched, or halt-and-released for human triage (Step 9). Bar-severity assignment is the owner's call and is never pre-authorized.
- Sprint-status flips between `backlog` ↔ `ready-for-dev` ↔ `in-progress` ↔ `review` ↔ `done`

**NOT pre-authorized** (requires the owner before action):

- Force push (`git push --force`, `--force-with-lease`)
- Any push to a remote, including `config: git.main_branch` and `config: git.base_branch`
- Fetching from or merging from a remote
- `git commit --no-verify`
- Anything on the tool's permission deny list (`.claude/settings.json` / `opencode.json`)

**On encountering an obstacle:** apply the halt-and-release procedure (below) and move to the next
story. Do NOT pause to ask the owner.

---

## Override of the human-gate rules

This skill operates under the owner's explicit override of two working rules:

- **Code-review triage is NOT human-gated here.** The `findings-verifier` agent adversarially
  verifies the union of three `layered-review` runs; the `findings-evaluator` agent makes the
  patch/defer/dismiss call on the verified findings.
- **Merge approval is NOT human-gated here.** If scoped tests are green, the loop merges via
  `land-story`.

Every other rule (root cause before fix, no test-weakening, dispatch-prompt discipline, surgical
changes) still applies unchanged.

The pipeline's phase-gate guard (`.workflow/hooks/guards/phase_dispatch.py`) stays on. The loop
satisfies it with durable records that name the autonomous authority (Steps 5, 9b and 11) — it never
disables the guard and never writes a record claiming a human approved something.

Failure handling preserves the **git branch (with a WIP commit)** but releases the
slot / worktree / stack via halt-and-release, so the loop keeps working on other stories.

---

## Pipeline overview

```
[main session — auto-dev-loop]
  │
  └─ LOOP:
       ├─ Step 1: pick the next story
       │     ↳ milestone-blockers for config: loop.milestone  (or first ready-for-dev story)
       │     ↳ drop stories already in a slot or in the failure log
       │     ↳ none left → TERMINATE LOOP
       │
       ├─ Step 2: reserve_slot.py  (lock-protected, cross-terminal-safe)
       │     ↳ already_reserved → back to Step 1
       │     ↳ no_free_slot → wait 60s, retry 5×, then TERMINATE
       │
       ├─ Step 3: inline setup (orchestrator)
       │     ↳ worktree add → stack up → compose watch (bg) → sanity check
       │     ↳ graph index (optional) → spec discovery → sprint status in-progress
       │
       ├─ Step 4: auto-spec (only if spec_present: false)
       │     ↳ story-spec agent (create-story + 3 elicitation rounds, non-interactive)
       │
       ├─ Step 5: stack_preflight.py (the gate) + auto checkpoint-1 record
       │
       ├─ Step 6: story-impl agent  (tests-first; max 5 batch-fix rounds; impl commit)
       │
       ├─ Step 7: layered-review × 3 on <merge-base>..HEAD
       ├─ Step 8: findings_union (dedupe)
       ├─ Step 8b: findings-verifier → CONFIRMED / REFUTED / UNVERIFIABLE + bar impact
       ├─ Step 9: findings-evaluator → patch / defer / dismiss (+ bar-blocking list)
       ├─ Step 9b: write the findings file with every decision + triage lists
       │
       ├─ Step 10: story-finalize agent  (patches → scoped tests, max 5 rounds → close-out commit)
       │
       ├─ Step 11: auto checkpoint-3 record → land-story
       │     ↳ merge → post-merge-smoke → cleanup → sprint status done
       │
       └─ Step 12: back to Step 1
```

Any failure in Steps 3–11 → halt-and-release → Step 1.

---

## Inputs

None. The skill picks its own work.

- `config: loop.milestone` — milestone whose `milestone-blockers` report feeds Step 1. Empty → the
  first `ready-for-dev` story in sprint status.
- `config: loop.max_stories` — cap on stories per invocation (`0` = unlimited). The environment
  variable `AUTO_DEV_LOOP_MAX_STORIES`, when set, overrides it. Counts every story started
  (landed or halted). When the cap is reached, finish the current story and stop with
  `auto-dev-loop terminating — max_stories (<N>) reached`.

---

## Halt-and-release procedure

Used by every failure path in Steps 3–11. It preserves the branch (with its WIP commit) and
releases the slot / worktree / stack. The owner resumes a failed story later by `git worktree add`
from the preserved branch.

**Steps (orchestrator, in the main session):**

1. **Commit any uncommitted changes** as WIP, if there are any:
   ```bash
   cd <worktree_path>
   git add -A
   git diff --cached --quiet || git commit -m "wip: [Story <story_id>] auto-loop halted in <failure_step>"
   ```
   Capture the commit hash if a commit was made (empty string if not).

2. **Log the failure** (lock-protected append):
   ```bash
   uv run --no-project .workflow/scripts/log_failure.py \
       --story-id "<story_id>" \
       --branch-name "<branch_name>" \
       --failure-step "<failure_step>" \
       --failure-summary "<one-sentence summary>" \
       --wip-commit "<hash or empty>" \
       --last-test-output "<last 30 lines or empty>"
   ```
   The entry lands in the failure log (`config: paths.auto_dev_failure_log`) and records: story id,
   timestamp, failure step, the preserved branch, the WIP commit, the summary, the test-output
   tail, and the resume command
   (`git worktree add <config: git.worktrees_root>/<branch_name> <config: git.branch_prefix><branch_name>`).

3. **Stop the stack and wipe its volumes** (skip, and say so, when `config: stack.runtime` is `none`):
   ```bash
   uv run --no-project .workflow/scripts/bring_up_story_stack.py \
       --slot <slot> --project-name "<stack_project_name>" --worktree "<worktree_path>" --mode down
   ```

4. **Kill orphan compose-watch processes and remove the worktree, keeping the branch:**
   ```bash
   uv run --no-project .workflow/scripts/cleanup_story_stack.py "<story_id>" --skip-branch-delete
   ```
   `--skip-branch-delete` is the contract: the branch is preserved. If the script is unavailable,
   fall back to `git worktree remove --force <worktree_path>`; if that fails because a
   `compose watch` process still holds the directory, find the process whose working directory is
   inside the worktree, stop it, and retry.

5. **Release the slot** (lock-protected):
   ```bash
   uv run --no-project .workflow/scripts/release_slot.py --story-id "<story_id>"
   ```
   The script ONLY resets the slot row in `config: paths.slot_registry` to `free`. It does NOT touch
   sprint status.

6. **Do NOT flip sprint status on halt.** The story stays `in-progress`. The failure-log entry is
   the explicit halt marker; a status outside `config: sprint_status.statuses` (e.g. `failed`) would
   corrupt the status taxonomy. The owner triages later and decides whether to resume (stay
   `in-progress`) or revert to `backlog` / `ready-for-dev`.

7. **Loop back to Step 1.** Step 1's skip-set prevents re-picking the failed story.

---

## Workflow

### Step 1 — Pick the next story

**Skip-set** (applied to every candidate):
- stories whose row in `config: paths.slot_registry` is `in_use` (read the registry directly);
- stories with an entry in the failure log (`config: paths.auto_dev_failure_log`) — already failed once, the
  owner's inspection is pending. If the log does not exist, the skip-set from it is empty.

**Milestone configured** (`config: loop.milestone` non-empty): invoke the `milestone-blockers`
skill for that milestone in the main session. Read its machine-readable tail: take `STARTABLE`,
drop skip-set stories, and pick `RECOMMENDED` if it survives the skip-set, else the first remaining
`STARTABLE` story. Never pick a story outside `STARTABLE` (group 0).

**No milestone configured:** run
```bash
uv run --no-project .workflow/scripts/sprint_status.py list --status ready-for-dev --exclude-epics --json
```
and pick the first row (file order) not in the skip-set.

If no candidate remains: emit `auto-dev-loop terminating — no startable stories left` and stop.

### Step 2 — Reserve the slot (lock-protected)

```bash
uv run --no-project .workflow/scripts/reserve_slot.py --story-id "<story_id>"
```

Parse the single-line JSON result:
- `reserved` — take `slot`, `branch_name`, `worktree_path`, `stack_project_name`. The git branch is
  `config: git.branch_prefix` + `branch_name`. Proceed to Step 3.
- `already_reserved` — another terminal took it between Step 1 and Step 2. Back to Step 1.
- `no_free_slot` — wait 60s with a timestamped heartbeat line, then re-run the script. If still
  `no_free_slot` after 5 retries, emit
  `auto-dev-loop terminating — no free slot and 5min wait exhausted` and stop.
- `error` — infrastructure issue, not a story failure: log it via `log_failure.py`
  (`--failure-step reserve-slot`) and stop the loop, surfacing the message.

### Step 3 — Inline setup (orchestrator, in the main session)

The orchestrator does setup directly (both this skill and `parallel-dev-wave`'s `story_setup.py`
share `reserve_slot.py`; the rest runs inline so the orchestrator controls loop pacing). Use the
values from Step 2. Ports derive from `slot` via `config: stack.ports` + `slot × config: stack.port_stride`;
`bring_up_story_stack.py` passes all of them to every compose call.

**3a — Create the worktree** off the base branch without switching the main checkout:

```bash
git worktree add -b <config: git.branch_prefix><branch_name> <worktree_path> <config: git.base_branch>
```

If the worktree already exists (resumed run), skip this and use the existing directory.

Then seed each `config: git.worktree_seed_files` entry: copy it from the repo root into the same
relative path in the worktree when the worktree has none. Never overwrite a worktree copy; a missing
source file is a warning, not a failure.

**3b — Bring up the isolated stack** (foreground). Skip 3b–3d, and say so, when
`config: stack.runtime` is `none`.

```bash
uv run --no-project .workflow/scripts/bring_up_story_stack.py \
    --slot <slot> --project-name "<stack_project_name>" --worktree "<worktree_path>" --mode up
```

Verify exit code 0.

**3c — Start `compose watch` in the background** (only when `config: stack.watch` is true; Claude
Code: `run_in_background: true`; OpenCode: a background shell):

```bash
uv run --no-project .workflow/scripts/bring_up_story_stack.py \
    --slot <slot> --project-name "<stack_project_name>" --worktree "<worktree_path>" --mode watch
```

**3d — Sanity check:**

```bash
uv run --no-project .workflow/scripts/bring_up_story_stack.py \
    --slot <slot> --project-name "<stack_project_name>" --worktree "<worktree_path>" --mode ps
```

Every service in `config: stack.services` must show `Up` / `running`. Any `Exited` or missing:
halt-and-release with `failure_step="inline-setup-sanity-check"`. Go to Step 1.

**3e — Graph index** (skip, and say so, when `config: graph.tool` is `none`): index the worktree with
the graph CLI (`config: graph.cli`) exactly as `story_setup.py` does, capture the returned project
name as `graph_project`, and write it to `.workflow/state/story-setup/<stack_project_name>.graph_project`.
Every later dispatch that reads worktree code passes this `graph_project`; never the main checkout's
project (`config: graph.main_project`). An indexing failure is not a halt — continue with
`graph_project` unset and state that graph navigation falls back to grep/read for this story.

**3f — Spec discovery:**

```bash
ls <worktree_path>/<config: paths.specs_dir>/*.md
```

Look for a file whose name starts with the story id in dotted or dashed form (ignore
`*-record.md`, `*-code-review-findings-*.md`). If several match, prefer the shortest filename. Set
`spec_present` and `spec_path` accordingly.

**3g — Flip sprint status to `in-progress`** (orchestrator; key-addressed, never a line-number edit —
another session may be editing the file concurrently):

```bash
uv run --no-project .workflow/scripts/sprint_status.py set <story-key> in-progress --note "STARTED <YYYY-MM-DD> slot <N> (<branch>); <phase/priority marker>; dep: <ids or none>; auto-dev-loop"
uv run --no-project .workflow/scripts/sprint_status.py touch --note "<story-id> started (slot <N>, auto-dev-loop)."
```

Notes are capped at `config: sprint_status.note_max_chars`; `set` archives the replaced note to
`config: paths.story_history_dir`/`<key>.md`; `--history "..."` records long-form detail. If the
story key is not in sprint status, skip the flip and continue.

If any of 3a–3g fails (other than the 3e indexing case): halt-and-release with
`failure_step="inline-setup"`. Go to Step 1.

### Step 4 — Auto-spec (conditional)

If `spec_present: true`: skip this step.

Otherwise:
1. Dispatch the `story-spec` subagent. Pass: story id + title, the epic's AC block for the story
   (from `config: paths.epics_dir`), `worktree_path`, `graph_project`, `spec_target_path` =
   `<worktree_path>/<config: paths.specs_dir>/<story-key>.md`, `mode: auto`,
   `elicitation_rounds: 3`. The agent runs `create-story` non-interactively (best-effort decisions
   recorded in the spec), then the `advanced-elicitation` rounds, revises once with the union of
   critiques, and returns `spec_path`. Blocking ambiguity that survives the rounds is logged in the
   spec; the agent returns its best version.
2. Set `spec_path` from the agent's report.
3. On a failed dispatch, retry once per the model-fallback policy in the `parallel-dev-wave` skill.
   If it still fails: halt-and-release with `failure_step="story-spec"`. Go to Step 1.

### Step 5 — Preflight gate + auto checkpoint-1 record

```bash
uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack_project_name> --worktree <worktree_path>
```

The script is the gate. Exit 1: dispatch the `dev-preflight` subagent (diagnosis-only, report-only)
with the script's failing lines, put its diagnosis into the failure summary, and halt-and-release
with `failure_step="stack-preflight"`. Go to Step 1. With `config: stack.runtime` = `none` the
script runs its non-stack checks only.

On exit 0, record the autonomous spec approval (the phase-gate guard blocks the `story-impl`
dispatch without it):

```bash
uv run --no-project .workflow/scripts/story_record.py append <story_id> "checkpoint-1 spec approved — auto-dev-loop pre-authorization, no human review"
```

### Step 6 — `story-impl`

Dispatch the `story-impl` subagent. Pass `story_id`, `branch_name`, `worktree_path`,
`stack_project_name`, `slot`, `graph_project`, `spec_path`.

**Append this hard cap to the dispatch prompt:**

> AUTO MODE: Maximum 5 batch-fix rounds. If round 5 still shows failing tests, STOP. Stage all current changes with `git add -A`, then commit with subject `wip: [Story <story_id>] auto-loop halted at test-fix round 5`. Report the failure with the last 30 lines of test output. Do NOT return success.

When the agent returns:
- **Success** (impl commit hash present, tests green, `phase-2 tests` GREEN line in the story
  record): proceed to Step 7.
- **WIP halt or any other halt**: halt-and-release with `failure_step="story-impl"`, including the
  test output the agent surfaced. Go to Step 1.

### Step 7 — `layered-review` × 3

Invoke the `layered-review` skill three times in sequence from the main session, each on
`<merge-base>..HEAD` of the story branch. Pass in the args of every invocation:

- The review target: the diff range `<merge-base>..HEAD` and the absolute `spec_path`.

- The dispatch preamble (guard-enforced): every review sub-agent prompt MUST lead with
  `config: review.required_preamble` verbatim. Add, after it: "In this run the findings-evaluator
  agent performs triage."
- The slot's `graph_project`, with the note that review sub-agents reading worktree code must use
  that project and never `config: graph.main_project`.
- The dispatch directive: dispatch the layers as their typed agents (`blind-hunter`,
  `edge-case-hunter`, `acceptance-auditor`); on a failed dispatch retry that layer once per the
  `parallel-dev-wave` model-fallback policy. A layer that errors OR returns no report counts as
  FAILED — silence is a failure, not a pass.

Collect each run's findings list verbatim. Keep them as separate lists for now.

If a run fails (errors / returns empty): retry it once. If the retry also fails, proceed with the
successful runs and record, under a `## Partial coverage` heading in the Step 9b file, which run
failed and why. Do NOT halt the story for one missing run.

If fewer than 2 of the 3 runs succeed: halt-and-release with
`failure_step="layered-review-only-1-of-3-succeeded"` (or `-0-of-3-`). One review is below the bar
for autonomous triage.

### Step 8 — Build `findings_union`

Concatenate the findings from all successful runs. Deduplicate by
`(reviewer_source, file:line, description_fingerprint)`, where the fingerprint is the first ~80
characters of the description, lowercased. Findings with the same fingerprint at the same file:line
collapse into one entry that records how many runs flagged it. Keep every unique finding, including
ones only one run produced (union, not consensus).

### Step 8b — Findings verification (`findings-verifier`)

Same pass as `parallel-dev-wave` Post-Phase 2b. Dispatch the `findings-verifier` subagent (prompt
leads with `config: review.required_preamble`; names `graph_project`, or `none`) with every finding
in `findings_union`, keyed by a stable finding id, each carried verbatim (id, claim, cited file:line,
layer evidence) — never the findings-file path, which the verifier does not read. Split the union across several dispatches when it
is large; every finding goes to exactly one verifier.

- **Silence is a failure.** A dispatch counts as FAILED if it errors, returns no report, or covers
  fewer findings than assigned. Retry a failed dispatch once. `findings-verifier` has no model
  fallback. If the retry also fails: halt-and-release with `failure_step="findings-verifier"`.
  Never pass an unverified finding to Step 9.
- **REFUTED** → auto-dismissed with the verifier's evidence. Never sent to the evaluator; listed in
  the Step 9b file under `dismissed_findings` with reason `refuted by findings-verifier`.
- **CONFIRMED / UNVERIFIABLE** → `verified_findings`, each carrying its verdict, evidence and
  `bar impact:` line verbatim. A finding without a `bar impact:` line (verifier partial failure)
  counts as unverified: re-dispatch it; never invent the line.

### Step 9 — `findings-evaluator`

Dispatch the `findings-evaluator` subagent (its prompt leads with `config: review.required_preamble`,
followed by the sentence "This dispatch comes from auto-dev-loop." — the evaluator refuses any
dispatch that does not state it). Pass `story_id`, `worktree_path`, `spec_path`, `graph_project`, the quality-bar doc path
(`config: paths.quality_bar_doc`, or "none configured"), and `verified_findings` inline (Step 8b —
REFUTED findings are not passed).

It returns `patch_list`, `defer_list`, `dismiss_list` — each finding with a one-line reason — plus,
when a quality bar is configured, a `## Bar-blocking — needs human triage` list. **There is no human
triage step**: the evaluator's call is final, with one exception below.

If the evaluator fails: retry once per the model-fallback policy; then halt-and-release with
`failure_step="findings-evaluator"`. Go to Step 1.

**Quality-bar-blocking findings are not auto-triageable.** When `config: paths.quality_bar_doc` is
empty, this gate is skipped — state "bar gate skipped (no quality bar configured)" in the Step 9b
file. Otherwise a finding the verifier marked `bar impact: BLOCKING` is binding: the evaluator may
not defer or dismiss it, and may escalate a `none` finding it judges bar-blocking on the code. The
evaluator never defers or dismisses a finding it assesses as bar-blocking; it
patches it, or returns it under `## Bar-blocking — needs human triage`. If that list is non-empty:
halt-and-release with `failure_step="bar-blocking-finding"`, quoting each entry's bar id and
consequence in the failure summary. Go to Step 1. The log names the bar, never a severity tier —
severity is the owner's call.

### Step 9b — Write the findings file (every decision documented)

Every finding gets a machine-decided classification; the owner must be able to audit, after the
fact, why each one was patched, deferred or dismissed. Write this file in the worktree BEFORE Step 10
so `story-finalize` commits it with the patches:

```
<worktree_path>/<config: paths.specs_dir>/<story-key>-code-review-findings-<YYYY-MM-DD>.md
```

Format (every finding appears exactly once, with reasoning):

```markdown
# Code Review Findings — Story <story_id> (auto-dev-loop)

Generated by findings-evaluator over <R> layered-review runs (union of <N> deduplicated findings;
<V> verified by findings-verifier: <C> confirmed, <U> unverifiable, <F> refuted).
Bar gate: <applied against <quality bar doc> | skipped (no quality bar configured)>

## Raw review runs
<each successful run's findings verbatim, under "### Run 1" / "### Run 2" / "### Run 3">

## Partial coverage
<only if a run failed: which run and why>

## Verification
<per finding: id — CONFIRMED | REFUTED | UNVERIFIABLE — evidence — bar impact line (not for REFUTED)>

## Triage decisions — <YYYY-MM-DD> (auto-dev-loop: findings-evaluator, pre-authorized; no human triage)

- Patched: <X> · Deferred: <Y> · Dismissed: <Z>

### approved_patches
finding_id | file (repo-relative) | line | change (one imperative sentence) | notes
<one manifest row per patched finding>

<then per patched finding:>
#### P1. <one-line title>
- **Source:** <blind-hunter | edge-case-hunter | acceptance-auditor> (<k> of <R> runs)
- **Finding:** <verbatim description>
- **Verification:** <CONFIRMED | UNVERIFIABLE> — <evidence> — <bar impact line>
- **Reasoning:** <one line — why patch>

### deferred_findings
#### D1. <title>
- **Source / File:** <reviewer> · `<file:line>`
- **Finding:** <verbatim>
- **Logged in:** `config: paths.deferred_work_dir`/`story-<id>.md`
- **Reasoning:** <one line — why defer rather than patch/dismiss>

### dismissed_findings
#### X1. <title>
- **Source / File:** <reviewer> · `<file:line>`
- **Finding:** <verbatim>
- **Reasoning:** <one line — refuted by findings-verifier (evidence) / false positive / out of stage / redundant / ...>
```

The `## Triage decisions` section with its three lists is the durable triage record the phase-gate
guard requires before the `story-finalize` dispatch. With zero findings, still write the file with
the three lists each reading `none — clean review`.

### Step 10 — `story-finalize`

Dispatch the `story-finalize` subagent. Pass `story_id`, `branch_name`, `worktree_path`,
`stack_project_name`, `graph_project`, `approved_patches` (the manifest), `deferred_findings`,
`dismissed_findings`, `round: 1`, and the findings file path.

**Append this hard cap to the dispatch prompt:**

> AUTO MODE: Maximum 5 batch-fix rounds after applying patches. If round 5 still shows failing tests, STOP. Stage all current changes with `git add -A`, then commit with subject `wip: [Story <story_id>] auto-loop halted at finalize test-fix round 5`. Report the failure with the last 30 lines of test output. Do NOT return success.

When the agent returns:
- **Success** (close-out commit landed or none needed, `phase-3 tests` GREEN line recorded): Step 11.
- **WIP halt or any failure**: halt-and-release with `failure_step="story-finalize"`. Do NOT invoke
  `land-story`. Go to Step 1.

### Step 11 — Auto checkpoint-3 record + `land-story`

Record the autonomous merge approval (the phase-gate guard blocks `land-story` without it):

```bash
uv run --no-project .workflow/scripts/story_record.py append <story_id> "checkpoint-3 merge approved — auto-dev-loop pre-authorization, no human review"
```

Then invoke the `land-story` skill with `story_id`. It does: `git merge <config: git.merge_style>` →
`post-merge-smoke` → cleanup (orphan watch kill, stack down, worktree remove, branch delete, slot
reset) → sprint status `done`.

If `land-story` halts on a merge conflict or a smoke failure: do NOT retry the merge and do NOT
resolve conflicts. Inspect what completed first — if the merge landed but smoke failed, the
failure summary must say the merge is already in `config: git.base_branch`. Then halt-and-release
with `failure_step="land-story-<merge-conflict|smoke-failure>"`.

On success the slot is `free` (released by `land-story`'s cleanup) and sprint status shows `done`.

### Step 12 — Next iteration

Increment the started-story count, check `max_stories`, and loop back to Step 1.

---

## Forbidden in this mode

- **Do NOT ask the owner for confirmation** at any step. If you want to ask "is this OK?", make the
  best available decision and continue — except for the cases under "Allowed stops".
- **Do NOT skip the failure-log entry** when halting a story. It is the owner's only record of what
  went wrong.
- **Do NOT auto-resolve merge conflicts.** Log as WIP and move on.
- **Do NOT delete the branch** of a halted story. The branch preserves the WIP commit and is how the
  owner resumes it.
- **Do NOT write a record line claiming a human approved or triaged anything.** Every auto-written
  checkpoint or triage record names the auto-dev-loop pre-authorization.
- **Do NOT disable or bypass a guard** to get past a block. A guard block on a story is a
  halt-and-release with `failure_step="guard-blocked-<guard>"`.

---

## Allowed stops (surface to the owner)

These are infrastructure problems, not story decisions — stop the loop and surface them:

- `reserve_slot.py` returns `error` (lock misbehaved, file permission, registry parse).
- All `config: slots.count` slots stay `in_use` for more than 5 minutes.
- `milestone-blockers` itself errors (a missing milestone config key, an unreadable map — not an
  empty `STARTABLE`, which is clean termination), or `sprint_status.py list` errors.
