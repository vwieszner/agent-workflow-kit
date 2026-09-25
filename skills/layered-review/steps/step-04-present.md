---
deferred_work_file: '' # set at runtime: <config: paths.deferred_work_dir>/story-<story_key>.md, or empty when no story key
---

# Step 4: Present and Act (direct mode only)

## RULES

- Pipeline mode never reaches this step — the caller owns triage, patching and status.
- When `{spec_file}` is set, write findings to the story file before offering action choices.
- `decision_needed` findings are resolved before `patch` findings are handled.
- No patch is applied without the owner's explicit numbered choice below.

## INSTRUCTIONS

### 1. Clean review shortcut

If zero findings remain after triage (none raised, or all dismissed): say so and go to section 6.

### 2. Write findings to the story file

Set `{deferred_work_file}` = `<config: paths.deferred_work_dir>/story-<{story_key}>.md` when `{story_key}` is set; otherwise leave it empty.

If `{spec_file}` exists and has a Tasks/Subtasks section, append a `### Review Findings` subsection, in this order:

1. `decision_needed` (unchecked): `- [ ] [Review][Decision] <Title> — <Detail>`
2. `patch` (unchecked): `- [ ] [Review][Patch] <Title> [<file>:<line>]`
3. `defer` (checked, marked deferred): `- [x] [Review][Defer] <Title> [<file>:<line>] — deferred, pre-existing`

If `{deferred_work_file}` is set, append each `defer` finding to it (create the file if absent; one file per story, never shared across stories) under `## Code review — <date>`, one `### <id> — <title>` block per finding with: **Location**, **Why deferred**, **Suggested follow-up**. If `{deferred_work_file}` is empty, do not invent a location — list the deferred findings in the summary and state they were not persisted.

### 3. Present summary

> **Code review complete.** <D> `decision_needed`, <P> `patch`, <W> `defer`, <R> dismissed.

If `{spec_file}` is set add: `Findings written to the review findings section in {spec_file}.` Otherwise: `Findings are listed above. No story file was provided, so nothing was persisted.`

### 4. Resolve decision_needed findings

If any exist, present each with its detail and the available options. The owner decides — the correct fix is ambiguous without their input. Walk through each (or batch related ones). Each resolves to `patch`, `defer`, or dismissed.

If the owner defers one, ask for a one-line reason and append it to both the story-file bullet and the `{deferred_work_file}` entry.

**HALT** — wait for the owner's choice on each. Do not proceed until every one is resolved.

### 5. Handle `patch` findings

If `patch` findings exist (including those resolved from section 4), HALT and ask the owner.

With `{spec_file}` set (show option 0 only when more than 3 patches exist):

> **How would you like to handle the <Z> `patch` findings?**
> 0. **Batch-apply all** — fix every non-controversial patch
> 1. **Fix them automatically** — apply fixes now
> 2. **Leave as action items** — they are already in the story file
> 3. **Walk through each** — show details before deciding

Without `{spec_file}` (option 0 only when more than 3 patches exist):

> **How would you like to handle the <Z> `patch` findings?**
> 0. **Batch-apply all** — fix every non-controversial patch
> 1. **Fix them automatically** — apply fixes now
> 2. **Walk through each** — show details before deciding

**HALT** — wait for the owner's numbered choice. Do not proceed until one is selected.

- **Option 0:** apply all non-controversial patches without per-finding confirmation; skip any that needs judgment. Summarize changes and skipped findings.
- **Fix automatically:** apply each fix, then summarize. If `{spec_file}` is set, check off the items in the story file.
- **Leave as action items** (spec set only): done — findings are already in the story.
- **Walk through each:** present each finding with detail, diff context and suggested fix; afterwards re-offer the applicable options and HALT again.

Report:

- Decision-needed resolved: <D>
- Patches handled: <P>
- Deferred: <W>
- Dismissed: <R>

### 6. Update story status and sync sprint tracking

Skip this section if `{spec_file}` is not set.

- All `decision_needed` and `patch` findings resolved (fixed or dismissed) and no unresolved HIGH/MEDIUM issues remain → `{new_status}` = `done`.
- Patches left as action items, or unresolved issues remain → `{new_status}` = `in-progress`.

Update the story file's Status section to `{new_status}` and save it.

If `{story_key}` is not set, skip the sync and state that sprint status was not synced (no story key). Otherwise, if `config: paths.sprint_status` exists, update the story's row by key — never by line number, preserving all comments and structure:

```bash
uv run --no-project .workflow/scripts/sprint_status.py set <story_key> <new_status>
```

If the key is not found, warn that the story file was updated but the sprint-status sync failed. If the sprint-status file does not exist, state that status was updated in the story file only.

> **Review complete.** Story status: `{new_status}` · Issues fixed: <n> · Action items: <n> · Deferred: <W> · Dismissed: <R>

### 7. Next steps

> **What next?**
> 1. **Start the next story** — invoke the `parallel-dev-wave` skill for the next `ready-for-dev` story
> 2. **Re-run code review** — address findings and review again
> 3. **Done** — end the workflow

**HALT** — wait for the owner's choice.
