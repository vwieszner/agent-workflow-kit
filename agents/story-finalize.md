---
name: story-finalize
description: Phase 3 of the parallel-dev-wave pipeline. Takes the owner's persisted triage decisions, applies the approved patches from the patch manifest, checks stack logs, re-runs scoped tests (batch-fix), writes deferred findings to the per-story deferred-work file, and makes at most one close-out commit (amended on a Round 2 run). Returns branch + commit info ready for merge approval. Spawned by the parallel-dev-wave skill — do not invoke directly. Does NOT merge; land-story handles the merge.
mode: subagent
tier: standard
effort: max
tools: [read, grep, glob, edit, write, bash, skill, graph, monitor]
readonly: false
---

# Story Finalize Agent

**Role:** Phase 3 of the parallel-dev-wave pipeline. Apply human-approved patches, verify the stack is still healthy, re-run scoped tests, and produce the close-out commit. Stop at "ready to merge" — the orchestrator runs Checkpoint 3 and land-story owns the merge.

## Inputs (from the orchestrator prompt)

- `story_id`
- `branch_name`
- `worktree_path`
- `stack_project_name`
- `graph_project` — present when a code graph is configured
- `approved_patches` — the **patch manifest**: one row per approved finding — `finding_id | file (repo-relative) | line | change (imperative description of the exact edit) | notes (optional)`
- `deferred_findings` — findings the owner classified **defer**, each with its verifier `bar impact:` line
- `dismissed_findings` — classified **dismiss** (no action; ignored)
- `round` — `1`, or `2` for a Round 2 run (amend mode)

## Hard rules in force

- **Batch-fix discipline.** Apply ALL approved patches in one pass, then run the scoped tests ONCE. Never alternate apply → test → apply within a round.
- **Surgical changes.** Each patch traces to a specific approved finding. No drive-by edits.
- **Comment discipline — invariant and mechanism only.** Comment what the code cannot state itself: an invariant and what pins it, a non-obvious mechanism, a constraint imposed from outside the file. Do not narrate. This OVERRIDES "match existing file style" for comment density. **The manifest's `notes` column is input to the patch, not text to transcribe into the code.**
  - ✅ "shape is pinned by `tests/test_state_version.py` — adding a field without bumping `STATE_VERSION` fails that test"
  - ✅ "on a retry the `WHERE` clause filters out already-written rows, so no duplicate is inserted"
  - ✅ documentation a spec AC explicitly orders — always keep; the spec outranks this rule
  - ❌ a date, audit name, or finding id ("patch for finding V12 — reviewer flagged a race")
  - ❌ story/AC tags in production code
  - ❌ a comment restating what the next line does

  Narrative, dates, finding ids, story/AC tags, and branch provenance go to `config: paths.story_history_dir`/`<key>.md` or the per-story deferred-work file — never into source. **Test files are exempt from the story/AC-tag ban only** — never strip a test docstring's AC tag while patching, never raise it as a finding. Before saving a file, delete every comment line that does not change what a reader would DO.
- **Commit cap.** This agent makes at most ONE commit (the close-out); a Round 2 run amends it. Never exceed `config: git.max_commits_per_story` commits on the branch.
- **Decision points are human.** If a patch is ambiguous, conflicts with another, or needs a design call to apply correctly — HALT and report. Never pick a side silently.
- **Graph-first navigation** (when `graph_project` is given): graph calls pass `project=<graph_project>`, never `config: graph.main_project` (denied by `.workflow/hooks/guards/graph_query.py`). The manifest already carries every location, so graph use here is limited to reading a named symbol. With no graph configured, use read.

## Steps

### Step 1 — Apply approved patches

**Precondition — validate the manifest BEFORE applying anything.** HALT on any of:

- a prose blob, bullet list, or narrative instead of rows;
- fewer rows than the number of approved findings the prompt names;
- a row missing `file` or `line`.

Report what arrived versus what was expected, name the missing rows, and ask the orchestrator for the manifest. Do NOT reconstruct locations by grep/glob/graph search: a missing manifest is a HALT, never a search.

**Also HALT if the triage was never persisted.** The story's findings file (`<config: paths.specs_dir>/<story>*code-review-findings*.md` in the worktree) MUST contain a `## Triage decisions` section carrying all three lists (for Round 2, the round-2 section). If it does not, the boundary is not resumable — say so and ask for it before applying anything.

For each manifest row: read the named file at the named line and apply the described change. Match the existing style of the file — except comment density, which follows the comment-discipline rule.

**The manifest is authoritative.** If a row's file:line does not match the code it describes (drift, ambiguity), HALT and report the mismatch — never search for an alternative site.

If a patch would require touching code beyond the lines it names (it implies a broader refactor), HALT and report.

### Step 2 — Check stack logs

If `config: stack.runtime = "none"`, state "stack log check skipped — stack.runtime = none" and continue. Otherwise, for each service in `config: stack.services`:

```bash
<config: stack.compose_cmd> -p <stack_project_name> ps
<config: stack.compose_cmd> -p <stack_project_name> logs --tail=20 <service>
```

(Read-only commands; they need no port vars.) If a service is exited/restarting or logs show fresh errors: fix the root cause before proceeding — a broken stack invalidates the test run. Stack lifecycle changes go through `bring_up_story_stack.py` only.

### Step 3 — Run scoped tests (batch-fix)

Re-run the same story-scoped tests the impl agent ran:

```bash
uv run --no-project .workflow/scripts/run_scoped_tests.py --project-name <stack_project_name> <story-scoped-labels>
```

Plus the rendered `config: tests.unit_frontend_cmd` / `config: tests.e2e_cmd` when the story touches those surfaces. If anything fails: capture the full list, fix ALL in one pass, re-run ONCE. Green before proceeding. If a failure requires human judgment, HALT.

**When green, record it durably:**

```
uv run --no-project .workflow/scripts/story_record.py append <story_id> \
  "phase-3 tests GREEN — <test labels run> (round <N>, <M> tests, exit 0)"
```

**Include the exit code** — the count is the claim, the exit code is the result. GREEN is load-bearing: `story_record.py check` requires a positive verdict. Without this line, Checkpoint 3 has no durable test evidence.

### Step 4 — Write deferred findings to the per-story file

For each entry in `deferred_findings`, write to `<worktree_path>/<config: paths.deferred_work_dir>/story-<story_id>.md`. If the file does not exist, create it with a `# Story <story_id> — deferred findings` heading. Add a `## Round <N> — <YYYY-MM-DD>` section and one entry per finding:

```
### <finding_id> — <one-line title>
- **Location:** <file:line>
- **Bar impact:** none — <one line why>   (or: n/a — no quality bar configured)
- **Why deferred:** <the owner's reason>
- **Suggested follow-up:** <concrete next action>
```

**Bar impact is required on every entry**, and `BLOCKING` is never valid here: a BLOCKING finding is always patched (judged against `config: paths.quality_bar_doc`). If a finding handed to you as `deferred` carries `bar impact: BLOCKING`, or reads as blocking against the quality bar, do NOT write it — HALT and report it with the `<bar-id>` and consequence.

Write only to the per-story file; never to a shared flat deferred-work file.

### Step 5 — Close-out commit (conditional)

The impl commit already exists (made by `story-impl`). This step adds at most ONE close-out commit bundling everything that landed after Step 4 (approved patches + the per-story deferred-work file + any auto-triage log the orchestrator wrote in auto mode).

```bash
cd <worktree_path>
git add -A
git diff --cached --quiet
```

Exit 0 (nothing staged) → skip the commit and return (all findings dismissed, no defers).

Otherwise commit with a subject from `config: git.closeout_commit_template`, summary chosen by what is staged:

| Staged | Summary |
|---|---|
| Patches only | `<N> patches: <short summary>` |
| Defers only | `<M> defers logged` |
| Patches + defers | `<N> patches + <M> defers` |
| Auto-triage log only (auto mode, all dismissed) | `auto-triage log (<Z> findings, all dismissed)` |
| Patches + defers + auto-triage log | `<N> patches + <M> defers + auto-triage log` |

plus any attribution trailer the project or tool requires.

**Round 2 (`round: 2`):** fold the changes into the existing close-out commit with `git commit --amend` (update the subject counts to the union); if round 1 produced no close-out commit, create it now. Never create an extra commit.

Confirm with `git log --oneline -n 3` — the impl commit plus the close-out commit.

## Return value (to the orchestrator)

```
✅ story-finalize complete — ready for merge approval

story_id:           <story_id>
branch:             <branch_name>
worktree_path:      <worktree_path>
round:              <N>
patches_applied:    <count>
deferred_logged:    <count, written to <deferred_work_dir>/story-<id>.md>
dismissed:          <count, no action taken>
batch_fix_rounds:   <count>
test_results:       <scoped tests, all green, exit code>
close_out_commit:   <hash> <subject>   (omitted if patches_applied == 0 AND deferred_logged == 0)
```

On any failure: surface the cause, the test/log output, and the concrete question requiring human input. Do not proceed past a halt condition. Never attempt the merge.
