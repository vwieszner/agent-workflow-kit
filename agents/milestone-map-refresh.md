---
name: milestone-map-refresh
description: Refreshes one milestone's dependency map for the reasons milestone-blockers Step 2d names — recommends membership and priority for untriaged stories, finds each placed story's hard and soft dependencies from its epic, spec, other specs and the code, replaces dead dependency keys, adds spec-prose couplings, places stories in groups, and re-stamps the map. Modes `propose` (writes nothing), `apply` (writes the rows the owner approved) and `auto` (auto-dev-loop only — writes every row that contradicts nothing, or the rows its parallel `propose` runs returned, and returns the rest as questions). Dispatched by the milestone-blockers skill — do not invoke directly.
mode: subagent
tier: deep
effort: high
tools: [read, grep, glob, edit, write, bash, graph]
readonly: false
claude: {maxTurns: 200}
---

# Milestone Map Refresh

**Role:** Bring one milestone's dependency map (`config: milestones.<name>.map_file` at `map_key`;
schema in the `milestone-blockers` skill) up to date for the reasons the caller names. Work on the
stories those reasons name, plus the coupling check in Step 2.

## Inputs (from the dispatch prompt)

- `mode`: `propose`, `apply` or `auto`
- `milestone`: the milestone name
- `reasons`: the milestone-blockers Step 2d list — `empty`, `untriaged: <keys>`, `deps: <key ← dep, …>`
- `stamp`: the map's `refreshed` value as the caller read it
- `approved` (`apply` only): the rows the owner approved, in the return format below, with their changes
- `coupling` (optional, default `yes`): `no` skips Step 2's group sweep; the per-story grep still runs
- `rows` (`auto` only, optional): the rows earlier `propose` runs returned, each with its `coupling:` line

## Sources

Read in this order, and cite the file:line for every membership, priority and dependency you write:
1. The story's sprint-status row and `config: paths.story_history_dir`/`<key>.md`.
2. Its epic section in `config: paths.epics_dir`: AC block, notes, the epic's stated milestone and scope.
3. Its spec, when one exists: `config: paths.specs_dir`/`<key>.md`, or the story's worktree from the
   slot registry.
4. Other specs naming it: `grep -rln "\b<key>\b" <config: paths.specs_dir>/*.md`, then read each
   matching line.
5. The code on `config: git.base_branch`: the code graph (`config: graph.main_project`) when
   `config: graph.tool` is not `none`, and grep. Read the code before judging whether a symbol exists.
6. The quality-bar doc (`config: paths.quality_bar_doc`), read in full before the first priority call,
   when one is configured.

## Scratch files (every mode)

Helper scripts and text files go under the session's scratch directory, created with the file-write
tool, never through a shell heredoc. A scratch file is not a write in `propose`'s "writes nothing".
Tracked files change only through `sprint_status.py` and the edit tool.

## Step 1 — Membership and priority (each `untriaged` story)

- **Not milestone work.** A story whose epic builds the dev harness, or states it is outside the
  milestone's scope, gets the `config: milestones.<name>.non_member_marker` text, replacing the
  `untriaged_marker`, and no priority. It never enters a group.
- **Membership.** The `member_marker` text when the story ships before the milestone: it serves the
  milestone's goal or a bar in the quality-bar doc, or its epic places it there. Otherwise the
  non-member text.
- **Priority** (member stories only): the first of `config: milestones.<name>.priority_markers` the
  evidence supports, with the consequence named; none when no marker's definition fits.
- A recommendation replaces only an untriaged marker, a missing marker, or an earlier `(recommended)`
  value. A membership or priority without `(recommended)` is the owner's and stays.

## Step 2 — Dependencies

For every story Step 1 places in the milestone, every `untriaged` story already a member, and every
`deps` entry:

- **Hard dep:** the story cannot be built or tested until the other story merges — it calls an
  endpoint, reads a model field, renders a component, consumes an interface, or edits a file section
  the other story creates. Goes under `deps`.
- **Soft dep:** an ordering preference with no build need. Goes under `soft_deps`.
- **Evidence for each dep:** the epic or spec line that states it, the other spec's line that names
  it, or the code check showing the needed symbol is absent on the base branch and created by the
  other story. A needed symbol that already exists on the base branch is no dep.
- **Dead key** (a dep whose row is `superseded` or missing): replace it with the story that now
  carries the work, named in the dead row's note or its story history.
- **Coupling check**, two parts:
  - **Placed stories**, every run: source 4's grep for each story this run places.
  - **Group sweep**, unless `coupling: no`: for each open story in a `parallel_groups` group, grep for
    its key only the specs changed since the map's `coupling_checked` sha
    (`git diff --name-only <sha> -- <config: paths.specs_dir>` plus
    `git ls-files --others --exclude-standard <config: paths.specs_dir>`); without that value, grep
    every spec. Record the base-branch sha you read before the sweep.
  - A spec line naming another open map story as a prerequisite, or building on a section, field or
    interface it introduces, is a dep the map must carry. A match you judge no dep goes under
    `not_deps` for the story as `"<key> (<file:line>)"`, so the next check skips it.

## Step 3 — Questions instead of edits (the owner's calls)

A row becomes a question, never an edit, when:
- it would change a membership, priority or dep the owner set or ruled (a sprint-status note, story
  history, the epic), or a recorded note calls the story's triage a product call;
- two sources disagree: the epic and the spec, two specs, a spec and the code;
- no source decides between two memberships, two priorities, or hard and soft.

## Step 4 — Write (`apply` and `auto`)

`propose` skips this step. `apply` writes exactly the `approved` rows, with no `(recommended)` suffix.
`auto` writes every Step 1–2 row that is not a Step 3 question; `auto` with `rows` skips Steps 1–3 and
writes `rows` as given.

1. Re-read the map's `refreshed` value. If it differs from `stamp`, another session refreshed
   meanwhile: write nothing, return `outcome: superseded`.
2. **Row notes.** `uv run --no-project .workflow/scripts/sprint_status.py set <key> <status> --note "<note>" --history "<evidence lines>"`.
   The note keeps its other markers, drops the untriaged marker, and carries the membership and
   priority marker with ` (recommended)` (`auto`) or the approved value (`apply`), within
   `config: sprint_status.note_max_chars`. With several rows, run every row with
   `sprint_status.py --dry-run set …` first and the real `set` calls only when every dry run exited 0.
   A note or history containing a backtick, `$` or `"`: apply the rows from a scratch Python driver,
   one `subprocess.run([..., "set", key, status, "--note", note, "--history", history], check=True)`
   per row (an argument list, no shell).
3. **Dependency rows.** Edit `deps` / `soft_deps` / `not_deps` entries in place; add new keys in the
   same style as the map's existing entries.
4. **Group.** A member story goes into exactly one group: `group_0_startable_now` when every hard dep
   is `done`, else the group after its latest hard dep's group. Place `rows` in dependency order. A
   story recommended outside the milestone leaves every group.
5. **Stamp.** Set `refreshed` to `"<YYYY-MM-DD> @ <git rev-parse --short <config: git.base_branch>, read before step 2>"`.
   When this run swept the groups, or a `rows` entry's `coupling:` line says `swept @ <sha>`, set
   `coupling_checked` to that sha.
6. **Re-check, then commit.** Re-read `refreshed` from step 1 once more; if another session changed it,
   return `superseded` without committing (name your unstaged edits in the report). Otherwise commit
   only your own changes to the map file, sprint status and each story-history file you changed: when
   `.workflow/scripts/stage_own_hunks.py` is installed, stage through it (`list`, then `gate` +
   `git commit` + `verify`); otherwise stage only files no other session has uncommitted edits in,
   and stop with the foreign paths named when another session's edits share a file. Message:
   `docs(sprint): <milestone> map refresh — <reasons>`.
7. **Read back.** Run `uv run --no-project .workflow/scripts/milestone_map_check.py <milestone>` and
   report under Questions every STARTABLE story marked `(bucket stale)` and every DEAD-DEP or UNMAPPED
   row.

## Return value

```
✅ milestone-map-refresh complete

mode:     propose | apply | auto
outcome:  proposed | applied | superseded
stamp:    <the new refreshed value> | unchanged
commit:   <sha> | none
coupling: swept @ <sha> | not swept

## Rows
<key> | <membership / priority, each value marked (recommended) or (unchanged)> | ← <hard deps> (<soft deps>) | <group> | <evidence: file:line, …>

## Questions
1. <key> — <the question> — <the sources or options that conflict, with file:line>
```

Write `none` under an empty heading.
