---
name: milestone-blockers
description: Show every story needed for a named milestone in wave/dependency order, with full dependency-chain verification against sprint status, a coverage check for stories the dependency map never listed, and a cross-reference check for couplings only written in other specs. Use whenever someone asks "what's left for <milestone>?", "what's blocking <milestone>?", "what should start next?", or anything about milestone readiness. Also the story picker for the auto-dev-loop skill.
---

# Milestone Blockers — Dependency-Group View

Show every story of one milestone organized by execution group, with actual status verified
against sprint status. Read-only: this skill never edits any file.

## Inputs

- **Milestone name** — from the invocation (`milestone-blockers <name>`). If none is given, use
  `config: loop.milestone`. If that is empty too, and exactly one `[milestones.<name>]` table is
  configured, use it; otherwise ask the owner which milestone.
- **Milestone definition** — `config: milestones.<name>.*`:
  - `map_file` — file holding the milestone's dependency map (default `config: paths.sprint_status`).
  - `map_key` — dotted key of the dependency map inside `map_file` (default `<name>.dependency_map`).
  - `member_marker` — regex matched against sprint-status row notes; a match marks a row as
    belonging to the milestone. Used ONLY by the Step 2b coverage check.
  - `priority_markers` — ordered list of note markers ranking group-0 candidates (highest first).
    Empty → map order.

If `config: milestones.<name>` is absent, or `map_key` does not resolve inside `map_file`, stop
and report which key is missing. Do not reconstruct a map from other sources.

### Dependency map schema (the authoritative source; hand-maintained, refreshed by approved passes)

```yaml
<map_key>:
  refreshed: <ISO date of the last refresh pass>
  deps:                          # story key -> declared upstream story keys
    <story-key>: [<dep-key>, ...]
  parallel_groups:               # ordered execution groups; group N unblocks after group N-1 merges
    group_0_startable_now: [<story-key>, ...]
    group_1_after_group_0_merges: [...]
    group_2_after_group_1_merges: [...]
    # ... any further groups, in order; the last one is the final group
  excluded:                      # optional named lists of member stories deliberately outside the groups
    blocked_external: [...]      # e.g. waiting on something outside the project
    # <other list name>: [...]
  critical_path_floor:
    chain: "<a> -> <b> -> <c>"
    length: <number of serial merges>
  waves:                         # optional: story key -> wave label, shown as context only
    <story-key>: <label>
```

Story keys may appear dotted or dashed (`7.5.8` / `7-5-8`); treat both forms as the same story.

## Step 1 — Read both sources in parallel

1. `config: paths.slot_registry` — which slots are `in_use`, and which story/branch is in each.
2. `map_file` — the dependency map at `map_key`, and `config: paths.sprint_status` for the actual
   status of every story. Sprint-status row notes are one-line marker sets; a story's full history
   is in `config: paths.story_history_dir`/`<key>.md` — open it only when a marker needs its
   backstory.

## Step 2 — Build the picture

**Primary source: `parallel_groups`.** The groups are pre-computed execution order. Also read
`critical_path_floor` for the schedule floor.

For every story in every group:

1. Look up its actual status in sprint status. If it occupies a slot in the registry → `in-flight —
   Slot N`. If `done` → drop it from the active report.
2. Cross-check each declared dep (from `deps`) against its actual sprint status — the map was
   written at refresh time and may be stale. If every dep of a group-N story is now `done`, the story
   has effectively moved to group 0; say so next to it.

## Step 2b — Coverage check (MANDATORY)

Step 2 catches stories whose **status** went stale. It cannot catch stories the map **never listed**.

1. **Set A** — every sprint-status story that is NOT `done` and whose note matches
   `member_marker`. If `member_marker` is empty, skip this check and state in the report:
   "Coverage check skipped — no `member_marker` configured for <name>; the groups may be incomplete."
2. **Set B** — every story key named anywhere under `parallel_groups` and under `excluded`.
3. **A minus B = UNMAPPED.** Report it under its own heading (Step 3).

Quote the map's `refreshed` date. If UNMAPPED is non-empty, that date is the staleness evidence.

Markers are read here **only to detect omissions**. They never place a story into a group, never
drive the recommendation, and never substitute for the map.

## Step 2c — Cross-reference check (MANDATORY before recommending)

Step 2b catches stories the map never listed. It cannot catch **couplings the map never recorded** —
dependencies written in prose inside another story's spec (a context table, an AC's "verify no clash
with X" clause, a dev note) that never reached `deps`.

**For every story you are about to recommend**, run:

```bash
grep -rlE "(^|[^[:alnum:].-])(<dotted-id>|<dashed-id>)([^[:alnum:]]|$)" <config: paths.specs_dir> \
  | grep -vE "/(<dashed-id>|<dotted-id>)[-.]"
```

(Escape the dots of `<dotted-id>` in both patterns — `7\.5`, not `7.5`. The `grep -vE` drops the
story's own spec and per-story artifacts under either id form.)

For each hit, **read the matching line** and classify it:

| What you find | What it means |
|---|---|
| Another spec's AC or context names this story as a dep, prerequisite, or "coordinate with" | **Blocking or ordering constraint.** Report it; do not recommend the pair as parallel. |
| Another spec builds on a section / filter / field / interface this story introduces | **Serial coupling.** Recommend only the upstream one. |
| A merged story, a deferred-work file, or a historical note | Mention only if it changes the picture. |

Report what the grep found alongside the map's declared deps, and say plainly when the two disagree.

**Scope.** This grep informs the **recommendation and its caveats only**. It never places a story in
a group, never overrides `deps`, and never licenses recommending off-map. A coupling the map lacks is
a refresh trigger to offer, not a group to invent.

## Step 3 — Produce the report

### Group 0 — Startable Now

Every `group_0_startable_now` story that is not `done`, with actual status (in-flight — Slot N /
backlog / ready-for-dev) and its wave label if `waves` has one.

### Group 1 … Group N

One section per remaining group, in map order. For each story: the dep(s) that must merge first. If
all of a story's deps are already `done`, note that it has effectively moved to group 0. The last
group is headed **Final**.

### Unmapped — In Sprint Status, Absent From The Dependency Map

The Step 2b result. For each: story key, its sprint-status status, and the deps its own note
declares. State plainly that these are **not** in any group and that the groups above are therefore
incomplete. Quote the map's `refreshed` date. If UNMAPPED is empty, say so in one line. If the
check was skipped, say that instead.

### Critical Path Floor

Quote `critical_path_floor.chain` and `critical_path_floor.length` verbatim. This is the minimum
schedule regardless of slot count. If UNMAPPED is non-empty, flag that the floor was derived from an
incomplete story set and may be understated.

### Recommendation

Free slots from the slot registry (slots `1..config: slots.count`; never the shared slot
`config: slots.shared_slot`). Name which group-0 stories to start and in which slots. Order group-0
candidates by `priority_markers` (first marker found in the story's note ranks it; unmarked stories
last); within a rank, prioritise chain-head stories (ones that unblock later-group chains) over leaf
stories. Be concrete: "Start <A> (chain head) in Slot 1, <B> (chain head) in Slot 3."

**Recommend only from group 0** — never from the UNMAPPED set. If UNMAPPED is non-empty, say in the
same breath that the recommendation is only as sound as the map, and offer the refresh as a separate,
explicitly approved action: fold the unmapped stories into `deps` with their declared deps, recompute
`parallel_groups`, re-derive `critical_path_floor`, update `refreshed`.

## Machine-readable tail (for auto-dev-loop)

End the report with this block, exactly:

```
MILESTONE: <name>
STARTABLE: <story-key>, <story-key>, ...      # group-0 stories not done and not in a slot, in recommendation order; "none" if empty
RECOMMENDED: <story-key>                       # the first recommendation, or "none"
UNMAPPED: <count>
```

## What not to do

- Never use `member_marker` or `priority_markers` to define the story list or to place a story in a
  group. Two sanctioned uses only: priority markers order candidates within group 0, and the member
  marker detects what the map omits, for reporting under "Unmapped".
- Never recommend a slot assignment without running Step 2c's cross-reference grep on every story
  you name. When the map and the grep disagree, report both — do not silently prefer either.
- Never recommend a story that is not in group 0. Urgent work outside the map is a refresh trigger,
  not a licence to recommend off-map.
- Never re-invent wave ordering when `parallel_groups` already encodes execution order.
- Never trust the map's status notes without verifying against sprint status.
- Never invent deps — use only what `deps` declares.
- Never read the implementation plan as a source — sprint status is the source of record.
- Never edit `map_file` or the sprint-status file as a side effect of running this skill. Report the
  gap, offer the refresh, wait for the owner's approval.
