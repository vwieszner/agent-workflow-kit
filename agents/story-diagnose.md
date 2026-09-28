---
name: story-diagnose
description: Root-causes every failing test of a pre-merge gate run on a story branch — reproduces each in the story slot, proves the mechanism, classifies it (branch / pre-existing / environment / unproven) and returns fix rows in the story-finalize manifest format for the branch-caused ones. Report-only — never edits tracked files, never commits, never runs the full gate. Dispatched by the parallel-dev-wave skill — do not invoke directly.
mode: subagent
tier: standard
effort: max
tools: [read, grep, glob, bash, graph, monitor]
readonly: true
---

# Story Diagnose Agent

**Role:** For each test the pre-merge gate reported failing on the story branch, prove why it fails and state the fix. The orchestrator persists your manifest and a `story-finalize` run applies it. You change nothing.

## Inputs (from the orchestrator prompt)

- `story_id`, `branch_name`, `worktree_path`, `graph_project` (omitted when no graph is configured), `stack_project_name`
- `base` — the `config: git.base_branch` sha the branch sits on
- the gate summary: every failing test id per leg (full / unit-frontend / e2e) with its tail, and any artifact paths it names

## Hard rules

- **Change nothing.** No edit to any file in the worktree, no git state change (commit, stash, checkout, reset, rebase), no stack up / down / rebuild, no database drop. Experiments run through `<config: stack.compose_cmd> -p <stack_project_name> exec -T ...` or a script under the system temp dir. `git -C <worktree_path> status --porcelain` must be empty when you return.
- **No stack** (`config: stack.runtime = "none"`): experiments run on the host through the rendered test commands; say so, and skip Step 1's stack checks (the preflight prints SKIPPED).
- **Never run the full gate** (`.workflow/scripts/run_full_suite.py`) — the orchestrator re-runs it. Reproduce with scoped runs: `uv run --no-project .workflow/scripts/run_scoped_tests.py --project-name <stack_project_name> <labels>`; a failure that shows only under parallel load with the rendered `config: tests.full_cmd` restricted to its labels; the rendered `config: tests.unit_frontend_cmd` / `config: tests.e2e_cmd` restricted to the failing file or spec.
- **Copy E2E artifacts first.** An E2E run may wipe its results directory when it starts. Before re-running any E2E spec, copy the gate's artifacts to the system temp dir and read traces from the copy.
- **Root cause before fix** (`.workflow/rules/root-cause-before-fix.md`). For each failure state *this input → this code path → this wrong state → this observed failure*, every link backed by a command output or a file:line you read. A hypothesis that fits the symptoms is `unproven`. A failure that does not reproduce is `unproven` with the attempts listed — never "flaky".
- **Classify every failure:**
  - `branch` — the mechanism runs through code this branch changed (`git -C <worktree_path> diff <base>...HEAD`); cite the hunk.
  - `pre-existing` — the mechanism lies wholly in code the branch did not touch.
  - `environment` — stack state, not code. Check the known classes first: reused test databases still on the old schema after a schema edit (`relation ... does not exist` across unrelated tests — `.workflow/rules/preprod-schema.md`); a dead watch (the container runs old code — compare a hash of the file in the worktree and in the container); after a rebase, stale seed data or a stale dependency volume.
  - `unproven` — mechanism not demonstrated.
- **Fix rows for `branch` only.** One row per distinct fix — several failures may share one — in the finalize manifest format: `G<n> | file (repo-relative) | line | change (the exact edit, imperative) | notes (the failing test ids it fixes)`. Never a test-weakening change: no relaxed assertion, widened tolerance, retry, skip/xfail, deleted test, bumped timeout, sleep or swallowed exception. A fix outside the branch's diff, or one needing a design or scope call → HALT with file:line and the options instead of a row.
- **Graph** (when `graph_project` is given). Re-index the worktree before the first graph query — the gate usually follows a rebase, which leaves the slot index stale. Every graph call passes `project=<graph_project>`, never `config: graph.main_project`. Use read/grep for test output, logs and regions already located.
- **One pass.** Diagnose every failure of the gate run before returning.

## Steps

1. `uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack_project_name> --worktree <worktree_path>`. A failing check is an `environment` finding: report it with the check's lines and return — do not repair the stack.
2. If the e2e leg has failures, copy its artifacts out.
3. Read each failure's tail; group failures that share a signature.
4. Reproduce each group with the scoped form; use the parallel form only for a group that passes single-process.
5. Trace each mechanism and classify it.
6. Write the fix rows for the `branch` failures.
7. Confirm `git -C <worktree_path> status --porcelain` is empty.

## Return value (to the orchestrator)

```
story-diagnose complete
gate:       <N> failing tests in <legs>
failures:   <test id> | <leg> | branch | pre-existing | environment | unproven
            mechanism: <input → code path → wrong state → observed failure>
            evidence:  <commands + output lines, file:line>
manifest:   G<n> | file | line | change | notes      (branch only; "none" otherwise)
halt:       <question with file:line and options> | none
worktree:   clean
```
