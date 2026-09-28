# Propagate Story Changes — Pre-Merge Propagation Pass

**Goal:** before a story branch merges, ensure every doc that references the story's changed
concepts is updated (or explicitly noted as unaffected).

**Your role:** you are the forcing function for the source-of-truth rule's "in-flight spec change
capture + end-of-impl propagation" clause (AGENTS.md). You read the changed story, identify
dependent docs, present each impact to the owner, and apply updates per the owner's call.

## Triage authority belongs to the owner

Each potential downstream impact you surface is a **propose-to-the-owner** moment, not an
auto-apply. The owner decides whether the impact is real, whether to update the dependent doc now,
or whether to flag it for follow-up. Same discipline as `layered-review` triage.

## When invoked

Near the end of a story's implementation, AFTER all spec changes from in-flight triage have landed
in the story file, BEFORE asking for merge confirmation. Typical invocation:
- `propagate-story-changes` (no args — auto-detect from the current worktree branch)
- `propagate-story-changes story=<id>` (explicit story id)

## Inputs

- **Story id** — from `story=<id>`, else parsed from the current branch name
  (`config: git.branch_prefix` + `<id>`).
- **Story spec file** — under `config: paths.specs_dir` (any subdirectory), file name starting with
  the story id in dotted or dashed form.
- **Base branch** — `config: git.base_branch`.

## Steps

### Step 1 — Resolve inputs and load context

1. Resolve the story id. If `story=<id>` is provided, use it. Else run
   `git rev-parse --abbrev-ref HEAD` and strip `config: git.branch_prefix`. If neither yields an id,
   halt and ask the owner.
2. Find the spec file: search `config: paths.specs_dir` recursively for `<dotted-id>-*.md` and
   `<dashed-id>-*.md` (excluding generated companions such as `*-record.md`,
   `*-code-review-findings-*.md`, `*-auto-triage.md`). If more than one candidate remains, ask the
   owner which one. Read it fully.
3. From the spec, extract:
   - **The story's own AC numbers and titles** (AC1/AC2…, Group A/B/C…, whichever the spec uses).
   - **The "Depends on" line** in the header (declares upstream stories).
   - **Any "References" / "Sources" sections** that cite related stories, epics, audits.
4. Run `git diff <base>...HEAD -- <spec path>` to see what spec text changed on this branch. Note
   the AC numbers / sections most heavily edited.

### Step 2 — Identify dependent docs

For each location below, run a targeted grep for the story id (both dotted and dashed forms) and for
the AC numbers that were edited heavily:

| Location | What you're looking for |
|---|---|
| `config: paths.epics_dir` — the story's epic file | Does the epic's story summary reflect what landed? Does any epic open question relate? |
| Other specs under `config: paths.specs_dir` that are not done | Forward-dependents — stories that reference this story's ACs by number, or that declare "Depends on: <id>" |
| Specs of done stories under `config: paths.specs_dir` | Reverse impact — done stories whose AC contracts assume an interface this story may have changed |
| `config: paths.planning_dir` — `*audit*.md` | Audit findings now stale or partially resolved |
| `config: paths.sprint_status` + `config: paths.story_history_dir`/`<key>.md` | Row note (max `config: sprint_status.note_max_chars` chars; markers only) matches the latest spec state? Long-form history current? |
| `config: paths.deferred_work_dir` (per-story files) | Items closed by what landed? New defers needed? |
| `config: paths.active_plan` (skip, and say so, when empty) | Step list reflects landed scope? |
| The project instruction file (`AGENTS.md` / `CLAUDE.md`) and `.workflow/rules/` | The story codified a project-wide rule |
| The agent's persistent memory, if the tool keeps one | A recurring class of decision worth saving |

For each location, collect the file:line hits. Don't open every file yet — just collect the
targeted hit list.

### Step 3 — Present the propagation checklist to the owner

Output a structured table:

```
## Pre-merge propagation checklist for Story <id>

### Spec changes detected (highest-impact ACs)
- AC<N>: <one-line summary of what changed>
- AC<M>: ...

### Dependent docs (potential impacts)
| File | Line(s) | What it references | Likely impact |
|------|---------|---------------------|---------------|
| <epic file> | 142, 188 | "Story <id> produces <X>" | None — interface unchanged. |
| <other spec> | 28 | "<id> path uses <old behaviour>" | **STALE** — this story replaced <old> with <new>. |
| <other spec> | 47 | "<mitigation> in <id>" | Verify still aligned. |
| <sprint status file> | 61 | row note | Update to match spec state. |
| <deferred-work>/story-<id>.md | — | (no hits) | New entries needed for <finding>. |
```

Halt here. Wait for the owner to walk through each row.

### Step 4 — Walk through each impact

For each row in the checklist, present full context (read the referenced file at the line, show the
surrounding context), then ask the owner:
- **Update now** — apply an edit to the dependent doc; the owner confirms the wording.
- **Flag for follow-up** — add a `[propagation-followup-<story-id>]` entry to
  `config: paths.deferred_work_dir`/`story-<story-id>.md`, under a
  `## Propagation followups — <YYYY-MM-DD>` heading, with the impact description.
- **Skip** — false positive, no impact.

Do NOT auto-decide. Per the source-of-truth rule, the owner owns the call.

Edits to the sprint-status file go through `.workflow/scripts/sprint_status.py` (`set` /
`history --append`), never a line-number edit.

### Step 5 — Surface cascading scope impact

If any row reveals that a downstream story's dependency has been broken by this story's changes
(AC reference no longer exists, contract changed, function renamed), this is a **cascading scope
impact** under the "challenge when needed" rule (AGENTS.md):

1. Highlight it explicitly: "Cascade impact: Story X.Y's AC<N> assumes <old behaviour>, which <id>
   changed to <new behaviour>. X.Y will need spec revision OR re-implementation if already done."
2. Wait for the owner's call: revise X.Y now, file a follow-up story, or flag for sprint replan.
3. Do not proceed to merge while a cascade is unresolved.

### Step 6 — Final summary + merge readiness

Output:

```
## Propagation pass complete for Story <id>

### Updated docs (N)
- file:line — <one-line description>

### Flagged for follow-up (M)
- <deferred-work>/story-<id>.md entry [propagation-followup-<id>] — <description>

### Skipped (K)
- file:line — false positive

### Cascade impacts (C)
- (none) | <story id> — <description>; status: <revised | follow-up filed | unresolved>

**Merge readiness:** READY / UNRESOLVED CASCADES / UPDATES NEEDED BEFORE MERGE
```

If the verdict is "unresolved cascades" or "updates needed", do NOT recommend merge. The owner
resolves first.

## Constraints

- MUST NOT apply edits to dependent docs without the owner's confirmation per row.
- Read-only during Steps 1–3; writes only in Step 4, after the owner approves each row.
- Don't broaden scope: the pass is bounded to docs that REFERENCE the changed concepts. Targeted grep
  matches drive the checklist; speculative searches don't.
- Never move or rename the spec file (e.g. into a "done" folder).

## Exit conditions

- All checklist rows resolved (updated / flagged / skipped).
- All cascade impacts surfaced and each called by the owner.
- Final summary printed.

The owner then proceeds to merge via the `land-story` skill.
