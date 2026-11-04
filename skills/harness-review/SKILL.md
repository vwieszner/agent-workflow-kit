---
name: harness-review
description: Periodic improvement review of the development-lifecycle harness (hooks, agents, skills, workflows, scripts, settings). Runs the health checks, measures the stories merged since the last review against the harness baseline, mines the session journals through the retro skill, updates the ranked harness backlog, and presents the open items to the owner. Use when the owner says "harness review", "improve the harness", "what should we improve", or after a few stories have merged.
---

# harness-review — one harness improvement review

Every recommendation names its effect on quality (keep or raise), speed (keep or raise) and token
cost (keep or lower), with evidence or "unmeasured".

## Boundaries
- Harness only. Never start `parallel-dev-wave`, `auto-dev-loop`, the auto-dev driver or a story
  agent. Work that needs app code becomes a carved story (Step 7).
- Never edit the baseline doc (`config: paths.harness_baseline`); it is the comparison point.
- Never disable or weaken a guard to make a check pass. A failing check is a backlog item with its
  mechanism.

## Step 1 — Read the backlog
Read all of the backlog (`config: paths.harness_backlog`; create it from the template at the end of
this file when absent). Its `covered_through:` line names the newest merge the previous review
measured.

## Step 2 — Health checks
Report each as one pass/fail line with the command's evidence:

1. `uv run --no-project .workflow/scripts/check_settings_and_docs_integrity.py` exits 0.
2. Headless login (when `config: curation.analyst` is not `off`): Claude Code —
   `claude -p "Reply with the single word OK. Use no tools." --output-format json --name login-check`
   returns `is_error: false`; OpenCode — `opencode run "Reply with the single word OK."` exits 0. A
   failure means every background analyst launch and the auto-dev driver fail: tell the owner to log
   in again.
3. Unmined sessions: count the lines of
   `uv run --no-project .workflow/hooks/session/session_retrospective.py --list`.
4. Background retrospective runs: the lines of `.workflow/state/retro-launches.jsonl` since the date
   of the `covered_through` merge. Any `ok: false` fails this check; report each one's `error` and
   the summed `total_cost_usd`.
5. Review read-coverage gate (when `.workflow/state/review-coverage.jsonl` exists): since the date of
   the `covered_through` merge, count the events per `agent_type`: `deny` (a report refused with
   pages unread), `cap` (a report let through with pages still unread after the refusal limit), and
   `error` / `no-transcript` / `no-agent-type` (the gate failed open — no check ran). Any fail-open
   event fails this check.
6. Kit and project script tests pass:
   `python3 -m unittest discover -s .workflow/scripts/tests -p "test_*.py"` (when installed) and the
   project's script tests.

## Step 3 — Measure
1. Stories merged since the last review: the merge lines of
   `git log <config: git.base_branch> --merges --first-parent --format="%h %s" <covered_through>..<config: git.base_branch>`
   whose subject names `<config: git.branch_prefix><id>`.
2. One run for all of them (Claude Code transcripts):
   `uv run --no-project .workflow/scripts/harness_metrics.py --story <id> [--story <id> ...] --summary`,
   with `--cut <agent-type>=<sha>` for each harness fix since the last review that targets one agent
   type (`--cut <sha>` alone: every type), and, when installed,
   `uv run --no-project .workflow/scripts/review_precision.py --story <id> [...]`. Drill into one
   story with the full markdown (no `--summary`) or `--json`.
3. Compare each story with the baseline's KPI table: $ per story measured, the phase split, peak
   main-session context, checkpoints, review precision. Name every regression with its number. Flag a
   story's `excluded_incidental_usd` when it is larger than its attributed total (a multi-story
   orchestrator session).
4. Denied tool calls: the `Tool-call denials by agent type, all stories` table (per agent type:
   denials, per spawn, repeats, top sources). Compare with the denial figures the backlog records.
   Name every agent type whose rate or repeats rose, with its top source. For each `--cut`, the
   `before / after the cut` row of the fix's agent type is that fix's measured effect. Drill into one
   agent type with `.workflow/scripts/denial_samples.py <type> --since <date>`.
5. Autonomous-loop configurations (when `.workflow/state/auto-dev/` holds runs):
   `uv run --no-project .workflow/scripts/auto_dev_metrics.py compare` groups driver sessions by
   configuration fingerprint (model, effort, harness hash) and sets quality, cost and time beside each
   group. Name the configuration that keeps or raises quality and speed at equal or lower cost, and every
   group whose blocking findings, halts or later fixes rose.

## Step 4 — Mine the journals
Invoke the `retro` skill. List the proposals it writes; promotion stays with `curate`.

## Step 5 — Update the backlog
- Add one row per failed check, KPI regression, recurring journal error, or new owner request:
  severity (High / Medium / Low), effect on quality / speed / tokens, evidence (command output,
  `file:line`, commit, transcript count), next action.
- An item whose fix landed: status `done <sha>`. An item turned into a story: `carved <story-key>`.
  An item the owner rejected: `rejected: <reason>`.
- Re-rank the open rows: severity first, then gain on the three axes per unit of effort.
- Set `covered_through:` to the newest merge Step 3 measured.

## Step 6 — Present
Show the owner the failed checks, the KPI regressions, the new rows, and the top open rows with a
recommendation for each. Ask every pending decision in one round-trip.

## Step 7 — Act on the approved items
- **Harness file** (hooks, agents, skills, scripts, settings): prove the mechanism first
  (`.workflow/rules/root-cause-before-fix.md`), change it, run the touched script tests, and commit
  the exact paths. A kit-managed file under `.workflow/` is overwritten by the next kit install:
  change it in the kit source, or record the item for the kit.
- **App code:** carve a story — a section in the right epic plus a sprint-status row
  (`.workflow/scripts/sprint_status.py`). Never start it in this session.
- **New skill, agent, rule or script:** write a proposal under `.workflow/state/proposals/` for
  `curate`.
- Commit the backlog.

## Shared files
The sprint status, the epics and the story history often carry other sessions' uncommitted edits.
Commit only this session's hunks:
1. `uv run --no-project .workflow/scripts/stage_own_hunks.py list <path>` numbers the file's hunks.
2. In one shell command:
   `T=$(uv run --no-project .workflow/scripts/stage_own_hunks.py gate --hunks <path> <hunks>... [--add <path>]...) && git commit -F <msgfile> && uv run --no-project .workflow/scripts/stage_own_hunks.py verify "$T"`
   (`N:k` / `N:+a-b` picks part of a hunk that interleaves another session's edit; `--add` takes a
   file holding only your changes). `gate` exit 3: another session's changes are in the index and
   nothing was committed; wait and retry, never unstage them. `verify` exit 4: the commit took
   foreign paths; it lists them; stop and report them.

## Backlog template
```markdown
# Harness backlog

covered_through: <sha of the newest merge measured>

| # | Severity | Item | Quality / Speed / Tokens | Evidence | Next action | Status |
|---|---|---|---|---|---|---|
```
