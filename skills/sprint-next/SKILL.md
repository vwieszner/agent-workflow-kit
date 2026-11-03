---
name: sprint-next
description: Show what story to work on next. Reads the slot registry, the sprint-status tracker, the milestone dependency map and the auto-dev failure log in one shot and produces a concise situational report — critical path status, what is in-flight in slots, what is halted and waiting on whom, what is ready to start, and a concrete slot recommendation. Use whenever someone asks "what's next?", "what should I work on?", "what can I start?", "what's in progress?", or anything about sprint priority or picking the next work item.
---

# Sprint Next — Priority Snapshot

Answer "what should we work on next?" from the source-of-truth files in one concise report. Always read the slot registry first: a story recommended without that check may already be in flight.

## Step 1 — Read the sources in parallel

1. The slot registry (`config: paths.slot_registry`) — which slots are `in_use`, which are `free`.
2. The tracker: `uv run --no-project .workflow/scripts/sprint_status.py list --exclude-status done --exclude-epics --notes` (row notes are marker sets; long form in `config: paths.story_history_dir`).
3. The milestone in `config: loop.milestone` (when set): its dependency map (`config: milestones.<name>.map_file` / `map_key`) and its priority markers; the quality-bar doc (`config: paths.quality_bar_doc`) for tier definitions when set.
4. The failure log (`config: paths.auto_dev_failure_log`) — input only: its `## <story-id> — <date> — <failure step>` headings and each entry's `- asked_of:` and ticket lines.

## Step 2 — Extract the key facts

- Slots `in_use` and their stories; the count of `free` slots.
- The critical path (the milestone map's order, else the tracker's file order of not-done stories) and each story's status.
- `ready-for-dev` stories NOT assigned to a slot, with their priority markers and wave/group.
- Halted stories: a `blocked` row, or a failure-log entry for a story whose row is not `done`/`superseded`.

## Step 3 — Produce the report

### Critical Path

Each critical-path story with its status — ✅ `done`, 🔄 in-flight (slot N), 🟡 `ready-for-dev`, 🔴 `blocked` (halted, listed under Halted unless hand-blocked), ⬜ `backlog`. Call out the **first non-done story** — the current blocker.

### In-Flight

Slot number, story id, branch for each occupied slot. Then: "N slot(s) free."

### Halted

Per halted story: id, failure step, date, the hard deps of its map row that are not `done`, and who it waits on — **waiting on <external owner> <ticket>** while the entry's `asked_of` names that owner and the ticket's row in `config: paths.design_questions` is not resolved; otherwise **resume decision due** (these lead the section). For a resume decision due: the owner rules, then `uv run --no-project .workflow/scripts/resume_halted.py <story_id> --ruling '<ruling>'` (an entry that asked the owner nothing takes `--no-ruling`). A `blocked` row with no failure entry is hand-blocked unless its note carries a `HALTED <date> at <step>` clause (a failed halt: the owner rules on it; print no command). A tracker or failure log that cannot be read is reported as such, and no story is listed under Ready to Start or Recommendation: an unread source is never "not halted". None halted: say so in one line.

### Ready to Start

`ready-for-dev` stories not in any slot, ordered by the milestone's priority markers (in `config: milestones.<name>.priority_markers` order), then the map's group/wave, then rank if present. For each: story id · priority marker (if any) · group · one-line rationale.

### Recommendation

Name which stories to start and in which slots: "Start X in slot 2, Y in slot 3." Only stories that are not already in a slot and not halted.

## What not to do

- Never recommend a story without checking the slot registry first.
- Never recommend a story that has a failure-log entry; report it under Halted.
- Never invent statuses. Read what is in the files.
- Never add, remove, or re-tier priority markers — surface only what the files say.
