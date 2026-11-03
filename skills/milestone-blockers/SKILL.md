---
name: milestone-blockers
description: Show every story needed for a named milestone in wave/dependency order, with full dependency-chain verification against sprint status, halted stories and what unblocks them, a coverage check for stories the dependency map never listed, a cross-reference check for couplings only written in other specs, and a refresh check that offers (or, with AUTO_MODE, runs) the milestone-map-refresh agent. Use whenever someone asks "what's left for <milestone>?", "what's blocking <milestone>?", "what should start next?", or anything about milestone readiness. Also the story picker for the auto-dev-loop skill (args AUTO_MODE).
---

# Milestone Blockers — Dependency-Group View

Show every story of one milestone organized by execution group, with actual status verified
against sprint status.

**Mode.** First argument exactly `AUTO_MODE` (auto-dev-loop and its driver pass it): Step 2e runs the
map refresh itself and applies its recommendations. Any other invocation reports and offers the
refresh; it writes nothing.

## Inputs

- **Milestone name** — the argument after any `AUTO_MODE` (`milestone-blockers <name>`). If none is
  given, use `config: loop.milestone`. If that is empty too, and exactly one `[milestones.<name>]`
  table is configured, use it; otherwise ask the owner which milestone.
- **Milestone definition** — `config: milestones.<name>.*`:
  - `map_file` — file holding the dependency map (default `config: paths.sprint_status`).
  - `map_key` — dotted key of the map inside `map_file` (default `<name>.dependency_map`).
  - `member_marker` — regex on sprint-status row notes; a match marks a row as belonging to the
    milestone. Used ONLY by the coverage and refresh checks.
  - `non_member_marker` — regex marking a row as explicitly outside the milestone (refresh check).
  - `untriaged_marker` — regex marking a row as waiting for triage, e.g. `UNPHASED` (refresh check).
  - `priority_markers` — ordered list of note markers ranking group-0 candidates (highest first).

If `config: milestones.<name>` is absent, or `map_key` does not resolve inside `map_file`, stop and
report which key is missing. Do not reconstruct a map from other sources.

### Dependency map schema (the authoritative source; written only by `milestone-map-refresh`)

```yaml
<map_key>:
  refreshed: "<ISO date> @ <short sha of config: git.base_branch read at the refresh>"
  coupling_checked: <short sha of the last spec-coupling sweep>   # optional
  deps:                          # story key -> HARD upstream story keys (cannot build/test before they merge)
    <story-key>: [<dep-key>, ...]
  soft_deps:                     # optional: ordering preferences with no build need
    <story-key>: [<dep-key>, ...]
  not_deps:                      # optional: couplings checked and judged no dep, with the evidence
    <story-key>: ["<key> (<file:line>)", ...]
  blocked: [<story-key>, ...]    # optional: not startable whatever its deps (external block, superseded)
  pending: [<story-key>, ...]    # optional: deps not yet known
  parallel_groups:               # ordered execution groups; group N unblocks after group N-1 merges
    group_0_startable_now: [<story-key>, ...]
    group_1_after_group_0_merges: [...]
    # ... further groups in order; the last one is the final group
  excluded:                      # optional named lists of member stories deliberately outside the groups
    <list name>: [...]
  critical_path_floor:
    chain: "<a> -> <b> -> <c>"
    length: <number of serial merges>
  waves:                         # optional: story key -> wave label, shown as context only
    <story-key>: <label>
```

Story keys may appear dotted or dashed (`7.5.8` / `7-5-8`), or as a short id that is the leading part
of exactly one sprint-status key; all name the same story.

## Step 1 — Read the sources

1. `config: paths.slot_registry` — which slots are `in_use`, and which story is in each.
2. `map_file` at `map_key`, and `config: paths.sprint_status` for the actual status of every story.
   Row notes are one-line marker sets; a story's history is in `config: paths.story_history_dir`/`<key>.md` —
   open it only when a marker needs its backstory.
3. The failure log (`config: paths.auto_dev_failure_log`).

## Step 2 — Build the picture

Run `uv run --no-project .workflow/scripts/milestone_map_check.py <name>` first and build Steps 2,
2b and 2d from its lists: STARTABLE, HALTED, PENDING, BLOCKED, NO-ROW, UNMAPPED, UNTRIAGED, DEAD-DEP
and `REFRESH empty`. Use `--why <key>` for any membership you doubt. A script error (exit 2, or an
unreadable failure log) is a `milestone-blockers` error: report it and recommend nothing.

**Startable Now is derived, not read from a bucket.** A story is Startable Now when it sits in any
`parallel_groups` group, is not `done`, holds no slot, is not halted, is not listed under `blocked` or
`pending`, and every hard dep (`deps`) is `done` in sprint status. `group_0_startable_now` is a hint
only: a story it lists with an open hard dep is not Startable Now (report the dep), and a story from a
later group that meets the rule is (the script marks it `(bucket stale)`; say in one line that the
groups need a recompute).

**The halted clause.** A story with a failure-log entry is halted, whatever its failure step, until the
owner's resume command removes the entry. A `blocked` sprint-status row whose note carries
`HALTED <date> at <step>` and has no entry is a failed halt — halted too. A `blocked` row with neither
is hand-blocked and follows its deps like any other. A halted story is never Startable Now.

Cross-check the map against sprint status: the map was written at refresh time and may be stale.

## Step 2b — Coverage check

Step 2 catches stories whose **status** went stale. It cannot catch stories the map **never listed**.
UNMAPPED (the script's list) = open rows matching `member_marker`, named in no group and no `excluded`
list. With no `member_marker`, state: "Coverage check skipped — no `member_marker` configured for
<name>; the groups may be incomplete." Quote the map's `refreshed` value: it is the staleness evidence.

Markers are read here **only to detect omissions**. They never place a story into a group, never drive
the recommendation, and never substitute for the map.

## Step 2c — Cross-reference check (before recommending)

Step 2b catches stories the map never listed. It cannot catch **couplings the map never recorded** —
dependencies written in prose inside another story's spec that never reached `deps`.

For every story you are about to recommend, run:

```bash
grep -rlE "(^|[^[:alnum:].-])(<dotted-id>|<dashed-id>)([^[:alnum:]]|$)" <config: paths.specs_dir> \
  | grep -vE "/(<dashed-id>|<dotted-id>)[-.]"
```

(Escape the dots of `<dotted-id>` in both patterns.) For each hit, read the matching line and classify:

| What you find | What it means |
|---|---|
| Another spec's AC or context names this story as a dep, prerequisite, or "coordinate with" | **Blocking or ordering constraint.** Report it; do not recommend the pair as parallel. |
| Another spec builds on a section / field / interface this story introduces | **Serial coupling.** Recommend only the upstream one. |
| A merged story, a deferred-work file, or a historical note | Mention only if it changes the picture. |

A coupling the map lists under `not_deps` for that story is already judged. Report what the grep found
beside the map's deps, and say plainly when the two disagree. This grep informs the recommendation and
its caveats only; a coupling the map lacks is a refresh reason (Step 2d), not a group to invent.

## Step 2d — Refresh check

The map is refreshed only for one of these reasons, never on a schedule and never because a story
merged:

1. **`empty`** — the script's `REFRESH empty` reads `due`: no grouped story meets the Startable rule
   (a story in a slot or halted counts as startable), an open member row exists, and the map's
   `refreshed` value has no `@ <sha>` or a merge reached `config: git.base_branch` since that sha.
2. **`untriaged: <keys>`** — the script's UNTRIAGED list, plus the UNMAPPED list.
3. **`deps: <key ← dep, …>`** — the script's DEAD-DEP list (a hard dep whose row is `superseded` or
   missing), and every Step 2c coupling the map lacks that the story's `not_deps` does not list.

A story with a `map-refresh` failure-log entry awaits the owner's ruling: it counts toward no reason.
Write one line into the report, above Group 0: `Refresh: not due`, or `Refresh: due — <reasons>`.

## Step 2e — Run the refresh (when due)

- **Without `AUTO_MODE`:** report the reasons and offer the refresh. On the owner's yes, dispatch the
  `milestone-map-refresh` subagent with `mode: propose`, `milestone: <name>`, the reasons and the
  `refreshed` value as `stamp`; show its rows and questions; on the owner's rulings, dispatch it with
  `mode: apply` and the approved rows.
- **With `AUTO_MODE`:** split the `untriaged` keys into batches of at most 15 (one batch when there are
  none). Dispatch one `milestone-map-refresh` run per batch, in parallel, with `mode: propose`, the
  stamp and the batch's keys; the first batch also gets the `empty` and `deps` reasons and
  `coupling: yes`, the others `coupling: no`. When every run has returned, dispatch one run with
  `mode: auto`, the stamp, the reasons, and every returned row with its run's `coupling:` line as
  `rows`; it alone writes and commits. For each question any run returned, run
  `uv run --no-project .workflow/scripts/log_failure.py --story-id <story id> --branch-name none --failure-step map-refresh --failure-summary "<the question>"`.
  Outcome `superseded`: re-run Steps 1–2d once. Then rebuild Steps 2–2c from the refreshed map and
  write the report from it.
- A failed dispatch is a `milestone-blockers` error.

## Step 3 — Produce the report

### Group 0 — Startable Now

Every Startable Now story with actual status, its group, `(bucket stale)` where it applies, and its
wave label if `waves` has one; in-flight stories follow under their own line (`in-flight — Slot N`).
List each `group_0_startable_now` story with an open hard dep, naming the dep.

### Halted — Awaiting Resume

Every story the halted clause drops: key, the entry's failure step and date (a failed halt shows the
`HALTED` clause of its note), and its hard deps that are not `done`. Each with an entry waits on the
owner: **resume decision due** — the owner rules, then runs
`uv run --no-project .workflow/scripts/resume_halted.py <story_id> --ruling '<the ruling>'`
(`--no-ruling` when the halt asked nothing). A failed halt has no entry: print no command; it is the
owner's to rule on. A halted story is never recommended. Empty: say so in one line.

### Group 1 … Group N

One section per remaining group, in map order. For each story: the dep(s) that must merge first. If
all of a story's deps are already `done`, note that it has effectively moved to group 0. The last
group is headed **Final**.

### Unmapped — In Sprint Status, Absent From The Dependency Map

The Step 2b result: key, sprint-status status, and the deps its own note declares. State plainly that
these are **not** in any group and that the groups above are therefore incomplete. Quote `refreshed`.
Empty: say so in one line. Skipped: say that instead.

### Critical Path Floor

Quote `critical_path_floor.chain` and `critical_path_floor.length` verbatim. If UNMAPPED is non-empty,
flag that the floor was derived from an incomplete story set and may be understated.

### Recommendation

Free slots from the slot registry (slots `1..config: slots.count`; never `config: slots.shared_slot`).
Name which Startable Now stories to start and in which slots. Order candidates by `priority_markers`
(first marker found in the note ranks it; unmarked last; a `(recommended)` value orders like any
other, show the suffix); within a rank, chain-head stories (ones that unblock later-group chains)
before leaf stories. Be concrete: "Start <A> (chain head) in Slot 1, <B> in Slot 3."

**Recommend only from Startable Now** — never from UNMAPPED, never a halted story. If UNMAPPED is
non-empty, say in the same breath that the recommendation is only as sound as the map, and offer the
refresh (Step 2e).

## Machine-readable tail (for auto-dev-loop)

End the report with this block, exactly:

```
MILESTONE: <name>
STARTABLE: <story-key>, <story-key>, ...      # Startable Now stories, in recommendation order; "none" if empty
RECOMMENDED: <story-key>                       # the first recommendation, or "none"
HALTED: <story-key>, ...                       # "none" if empty
UNMAPPED: <count | skipped>
```

## What not to do

- Never use `member_marker` or `priority_markers` to define the story list or to place a story in a
  group. Two sanctioned uses only: priority markers order candidates, and the markers detect what the
  map omits.
- Never recommend a slot assignment without running Step 2c's grep on every story you name.
- Never recommend a story that is not Startable Now, or that has a failure-log entry.
- Never re-invent wave ordering when `parallel_groups` already encodes execution order.
- Never trust the map's status without verifying against sprint status.
- Never invent deps — the report uses only what the map declares; a refresh adds a dep only with
  file:line evidence.
- Never read the implementation plan as a source — sprint status is the source of record.
- Never edit `map_file` or sprint status in this skill. Only the `milestone-map-refresh` agent writes
  the map: with `AUTO_MODE`, its own recommendations; otherwise only the rows the owner approved.
