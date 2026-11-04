# Layered Code Review

**Goal:** Review a change adversarially through independent, typed layers; verify every finding's premise against the code; hand the owner (or, in `auto-dev-loop` only, `findings-evaluator`) a triage table whose silences are named, never presented as clean.

## Triage authority belongs to the owner

Each finding is classified **patch**, **defer** or **dismiss** under the `rules-defer` skill. **The classification call is the owner's.** This skill produces findings, verdicts and recommended classifications; it never applies a patch, never edits code, the spec, sprint status or a deferred-work file, and never changes story status. The single exception is `auto-dev-loop`, where `findings-evaluator` substitutes for the owner's triage — nowhere else.

## Invocation modes

Determine the mode first and keep it for the whole run.

- **Pipeline mode** — invoked by the `parallel-dev-wave` or `auto-dev-loop` orchestrator with the arguments below. Runs "Pipeline mode" (this file). Every argument comes from the orchestrator: a missing required argument → stop and name it; do not search for a target or ask which diff to review.
- **Direct mode** — the owner invoked this skill on an arbitrary diff. Runs `./steps/step-01-gather-context.md` → `step-04-present.md`, halting at each checkpoint.

## Required dispatch preamble (guard-enforced)

Every review-layer dispatch begins with the verbatim text of `config: review.required_preamble` (`uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/wfconfig.py get review.required_preamble`). The guard `${CLAUDE_PLUGIN_ROOT}/hooks/guards/dispatch_prompt.py` blocks review-agent dispatches without it. In pipeline mode the workflow builds every prompt from it (`review_stage.py` passes it as `args.preamble`); in `auto-dev-loop` the workflow follows it with "In this run the findings-evaluator agent performs triage." The repro-writer and pbt-writer write test files, so they carry no "do NOT edit any files" preamble.

## Silence is a failure, not a pass

A layer whose dispatch errors, returns no structured report, or reports `diff_lines_read` below its diff's line count is retried once; after the retry it is listed in `failed_layers` with its reason. A finding with no verifier verdict is `UNVERIFIED`. Neither is ever presented as clean. With `config: models.fallback.<tool>.deep` set, a direct-mode retry of a `deep`-tier layer passes that model as a dispatch-time override where the tool supports one.

---

## Pipeline mode

### Arguments

| Name | Required | Value |
|---|---|---|
| `story_id` | yes | the sprint-status key |
| `mode` | yes | `controlled` (parallel-dev-wave) or `auto` (auto-dev-loop) |
| `passes` | no | independent review passes merged before verification; default `1`, auto-dev-loop passes `3` |
| `worktree` | yes | absolute story worktree root |
| `spec_file` | yes | absolute spec path, or `no-spec` (the acceptance auditor is then skipped) |
| `graph_project` | yes | the slot's `graph_project`; `none` without a graph or in snapshot mode |
| `snapshot_dir` | no | absolute scratch dir holding `git show <commit>:<path>` copies, set when another agent is mutating the worktree |
| `stack_project_name` | yes | the story stack the repro and execution stages run in; `none` when no stack runs (those stages are then skipped, and say so) |
| `new_code_since` | no | `controlled` only: the close-out commit before a last-round change that adds new code (`rules-defer`, New code after the last round); the round then reviews `<that close-out>..HEAD` |

### Step 1 — Stage the inputs

From the main checkout run:

```bash
uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/review_stage.py --story-id <story_id> --mode <mode> [--passes <passes>] --worktree <worktree> --spec-file <spec_file> --graph-project <graph_project> --stack-project-name <stack_project_name> [--snapshot-dir <snapshot_dir>] [--new-code-since <new_code_since>]
```

It computes the round number and kind, stages the round's diff and every workflow input under `<worktree>/.workflow/state/review-runs/round-<n>/` (reset on every run), pins the reviewed commit under `refs/review/<branch>/round-<n>`, and prints one line of JSON: `{"args": {...}, "seed_required": <bool>, "seed_cmd": "<cmd>", "review": {"round", "kind", "reviewed", "diff_range", "how", "reason"}}`. Exit codes: `0` staged; `1` a step failed (stop and report its stderr; a halted finding is never dropped); `3` nothing to review (stop); `4` no round is owed (stop and report its one line to the caller).

- A **branch round** reviews `<merge-base>..HEAD`; a **close-out round** reviews its delta (the close-out patch against the close-out the latest triaged round reviewed; the whole close-out patch, with the reason, when none can be derived); a **new-code round** reviews `<new_code_since>..HEAD`, in controlled mode only, at most once after the latest triaged branch round. `review_ledger.review_loop` refuses a round the loop does not owe.
- The diff, replay patch and `changed_files` leave out the story's findings files and record (pipeline output is never reviewed); `branch_files` and the UI diff do not.
- `carried` is `review_carry.py`'s output: the triage findings of halted rounds this round carries. `spec_acs` is `[]` for `no-spec`.
- The execution stages read the changed-file classes `review_stage.py` computes from config: `ui_sources` (`config: review.ui_paths` minus test files), `pbt_sources` (`config: review.pbt_source_regex`), `backend_sources` / `backend_tests` (`config: review.backend_source_regex`, `review.backend_test_regex`, and `review.mutation.enabled`). An empty class skips its stage, and the workflow names the skip.

**Seed the stack when the ui-verifier will run** (`seed_required` true): run `seed_cmd` exactly as printed (`config: review.ui_seed_cmd`) from the main checkout. Continue only when it exits 0. Any other result stops the round: report its output to the caller — no review runs on an unseeded or partly seeded stack.

**Snapshot mode** (`snapshot_dir` set): once Step 1 printed the staged review and before Step 2 starts, fill `snapshot_dir` with `git show <args.delta.reviewed.end>:<path>` for each path in `args.changed_files` and `args.branch_files` that exists at that commit, plus the spec. The workflow then reads code only from that dir and skips the repro and execution stages.

### Step 2 — Run the review workflow

**With a Workflow runtime (Claude Code `Workflow` tool):** call it with `scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/review.js"` (the plugin's review workflow) and `args` = the `args` object Step 1 printed, unchanged. Note the `Transcript dir` it prints at launch; Step 3 passes it to the renderer. Wait for its completion notification. Do not clear the session while it runs: a relaunch from a fresh session starts it over.

**Without one (OpenCode, or no Workflow tool):** play the runtime's part with the replay runner (Node ≥ 18 on the host); the workflow script stays the single source of the review logic:

1. `node ${CLAUDE_PLUGIN_ROOT}/scripts/workflow_replay.mjs run ${CLAUDE_PLUGIN_ROOT}/workflows/review.js <run_dir>/replay` — `<run_dir>` is `args.run_dir`. Exit 10 prints `PENDING <n> <path>`: `pending.json` lists each agent call the script waits on, `{label, agentType, effort, phase, dispatch_prompt}`.
2. Dispatch every listed call — those listed together in parallel — as the typed subagent `agentType`, its prompt exactly `dispatch_prompt`. Save each agent's JSON report to a file and record it: `node ${CLAUDE_PLUGIN_ROOT}/scripts/workflow_replay.mjs record <run_dir>/replay "<label>" <file>`. An agent that errors or returns no JSON is recorded with `--error "<why>"`; a report the recorder refuses for a missing required key is re-dispatched once, then recorded with `--error`.
3. Run step 1 again. Repeat until it exits 0 (`COMPLETE <run_dir>/replay/run.json`). Exit 1 (`FAILED`) stops the round: report the message.

Never answer a call yourself, edit a recorded result, or skip a pending call: each label is the agent's own report, and the renderer resolves every finding against it.

### Step 3 — Record and return the result

1. **Render.** Run `uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/review_findings_render.py --transcript-dir <Transcript dir> --story-id <story_id> --round <review.round>` (Workflow runtime) or `... --run-dir <run_dir>/replay ...` (replay). It rebuilds the round from the run file and the run's journal, appends it to `findings_file`, and writes the `review round <n> recorded` record line. If it exits non-zero, stop and report its output to the owner — an unrecorded review must not reach triage. Never write a findings section by hand.
2. **Return** to the caller the renderer's printed summary (read from the run file; never build the report from a completion message, which can be truncated), in this order:
   - `failed_layers` (with reasons), `skipped_layers`, `ui_unreached` (each state with the methods it tried), `ui_out_of_scope`, `ui_unaccounted`, `ui_double_placed`, `ui_unknown_ids`, `ui_untested_titles`, `ui_unplaced_viewports` and `ui_unmapped_acs` (when the ui-verifier ran), `unverified` and `repro_skipped` — named first, every time. Labels are `<layer>[#<pass>][@<chunk>]`.
   - `counts`, the diff line count and chunk count, and the `findings_file` path.
   - The triage rows (CONFIRMED, UNVERIFIABLE and UNVERIFIED) as a compact table: `id | sources | location | delta | title | verdict | votes | bar impact | repro | recommended`. `delta` is `inside` / `outside` the close-out round's delta, `n/a` on a branch or new-code round. `votes` lists the verifier votes (three in auto mode for patch and bar-blocking candidates; the verdict is their majority). `repro` is the repro-writer status and test path: REPRODUCED stands only on an assertion failure in the test body. A `carried:<id>` source marks a halted round's finding carried into this round.
   - The refuted rows as one compact "dismissed as false positive (N)" block.
   - The untracked repro and property test files the round left in the worktree stay there until story-finalize adopts or deletes them.
3. A finding's full text is its latest rendering in `findings_file`; print one with `uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/review_findings_show.py <findings_file> <id>` when the caller or the owner asks for its detail. Do not read the whole file.

---

## Direct mode

### Initialization

Load `.workflow/config.toml` and resolve `specs_dir` (`config: paths.specs_dir`), `sprint_status`, `deferred_work_dir`, `graph_tool` / `graph_main_project`, `quality_bar_doc`, today's date, and `communication_language` (`config: project.communication_language`; speak to the owner in it). Load `config: paths.lessons` and the project-context doc (`config: paths.project_context`, empty → `**/project-context.md`) when present.

### Step-file architecture

Read each step file completely before acting; follow the steps in order; HALT at each checkpoint for the owner; load the next step file only when directed.

Read fully and follow `./steps/step-01-gather-context.md`.
