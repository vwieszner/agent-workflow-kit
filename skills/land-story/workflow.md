# Land Story

**Goal:** Take a story from "ready to merge" through merge, post-merge smoke, cleanup, propagation and sprint-status update — the back half of the lifecycle that `parallel-dev-wave` hands off as the land window's last step.

**Precondition:** the story's record file carries `checkpoint-3 merge approved`. Before anything else (skip only with `--cleanup-only`), run:

```
uv run --no-project .workflow/scripts/story_record.py check <story_id> checkpoint-3
```

Exit 1 → STOP: go back to the owner; never write the approval on their behalf. `.workflow/hooks/guards/phase_dispatch.py` also blocks the skill invocation, when the tool passes the story id with it, unless `phase-3`, `pre-merge` and `checkpoint-3` are recorded and the story holds the land lease; this check is the gate wherever the tool passes no arguments (OpenCode's skill tool).

All commands run from the main checkout (`uv run --no-project .workflow/scripts/wfconfig.py root`).

## Inputs

- **`story_id`** — required. The id matching the slot registry (`config: paths.slot_registry`).
- **`--cleanup-only`** — optional. Skip merge + smoke; only run cleanup, propagation and bookkeeping. Use when a previous session merged but did not finish.

## Step 1 — Confirm preconditions

Read these in parallel:

- `git worktree list` — the story's worktree must be present (unless `--cleanup-only` and the dir is the orphan being recovered).
- `git log --oneline <config: git.base_branch> -5` — the merge target's tip.
- The slot registry — confirm the story id matches an `in_use` row.

Land lease (not for `--cleanup-only`): `uv run --no-project .workflow/scripts/land_lease.py holds <story_id>` must exit 0 — the land window holds the lease from `acquire` to this skill's Step 5 release. Exit 1 or 2: stop and report the script's line; never acquire the lease from here.

If `--cleanup-only`: skip to **Step 4**.

## Step 2 — Merge into the base branch

```bash
uv run --no-project .workflow/scripts/run_full_suite.py --hold-lock-for land-merge --story-id <story_id> --merge-summary "<one-line summary>"
```

Run it in the background and continue on its completion: the wait for a running shared-slot gate (bounded by `--lock-timeout-minutes`) can outlast a foreground tool call. That is a wait on this session's own background task, so the lease stays held. The merge waits while a shared-slot gate runs, so no landing changes the code a shared gate is testing.

The script takes the shared slot's gate lock, checks that `<story_id>` holds the lease and that the main checkout is on `config: git.base_branch` with no merge, cherry-pick or revert in progress (never `git checkout` the base branch yourself: a checkout outside the lock would change the shared slot's code mid-gate), then merges the story branch with `config: git.merge_style` from a fixed argv.

| Exit | Meaning | Do |
|---|---|---|
| 0 | merged | continue to Step 2b |
| 1 | nothing attempted: the lock was not taken in time, the story does not hold the lease, the lease record is unreadable, the main checkout is off the base branch, or a merge, cherry-pick or revert is in progress there | release the lease (`uv run --no-project .workflow/scripts/land_lease.py release <story_id>`), HALT and report; nothing merged (auto mode: halt-and-release with `failure_step="land-story-merge"`) |
| 2 | an argument or slot-registry error | the same release and HALT |
| 3 | the merge failed and left no merge state | release the lease, HALT and surface git's output. "Local changes would be overwritten" means the main checkout's own uncommitted changes: surface those files; do not rebase the story branch. Otherwise (a conflict) surface the conflicting files: the conflict is resolved on the story branch (`story-rebase` onto the base branch) and the story re-lands through a fresh land window from `acquire` — never commit a resolution on the base branch (auto mode: halt-and-release with `failure_step="land-story-merge"`) |
| 4 | the merge failed and its abort failed: the main checkout is mid-merge | release the lease and HALT to the owner, naming the mid-merge state. Releasing is safe: the action refuses every further merge while a merge is in progress, and shared-slot gates refuse a mid-merge checkout |

## Step 2b — Schema reset (only when the merge touches a schema file)

If `config: pipeline.schema_globs` is empty, skip and say so. Otherwise, when `git diff --name-only HEAD^1 HEAD` lists a path matching it, apply `.workflow/rules/preprod-schema.md` to the shared slot now, before the smoke:

1. Drop the shared slot's reused test databases under its gate lock, so no full gate runs meanwhile: `uv run --no-project .workflow/scripts/run_full_suite.py --hold-lock-for reset-test-dbs` (runs `config: schema.test_db_reset_cmd`). Exit 1: the lock was not taken and nothing was dropped; never drop them unlocked: stop and report (auto mode: halt-and-release with `failure_step="land-story-migration-reset"`).
2. Reset the shared slot's live data store: an additive change → the statement the story's schema edit calls for; a structural change → `config: schema.stack_reset_cmd` for the shared stack, which wipes its data: ask the owner first.

If the story's record or spec does not say which kind of change it made, ask the owner; never infer it from the diff.

**Auto mode.** When the story's record holds `checkpoint-3 merge approved (auto-dev-loop standing authorisation)` exactly, the additive reset (the test-database drop and the additive statement) is pre-authorized: do not ask. A structural change, or one the record and spec leave unstated, is never pre-authorized: auto-dev-loop halt-and-releases that story before the merge.

## Step 3 — Post-merge smoke check

Invoke the `post-merge-smoke` skill — it runs `.workflow/scripts/post_merge_smoke.py` against the shared slot (`config: slots.shared_slot`): service state, the log scan, and the source-sync gate, which hash-checks that the containers serve the merge's own files. There is no post-merge-smoke agent; never dispatch one.

If the smoke surfaces a failure clearly caused by the merge (a new env var not picked up by a running container, a new dependency missing from an image): apply its recommended fix (recreate / rebuild) before proceeding. Destructive fixes are run by hand after reading the diagnosis — the script never applies them. When `config: stack.runtime = "none"` the smoke reports the stack checks skipped; say so in the completion report.

## Step 4 — Cleanup

```bash
uv run --no-project --with psutil .workflow/scripts/cleanup_story_stack.py <id>
```

For `--cleanup-only` (orphan recovery), add `--skip-docker-down --skip-branch-delete`. (`--with psutil` supplies the optional process-inspection dependency the orphan detection uses.)

What the script does:

1. Resolves the worktree path from the slot registry (falls back to a unique `<id>` / `<id>-*` directory under `config: git.worktrees_root` if the row is already `free`).
2. Inside an unattended driver session, refuses (exit 2, before any action) a full teardown of a worktree whose HEAD is not merged into the base branch.
3. Finds orphan watcher processes (compose processes with a bare `watch` subcommand token whose working directory is inside the worktree — the prefix match is the safety net: it cannot reach another slot's watcher), kills them and their compose parent, waits, and re-scans: a watcher still alive ends the run with exit 1 before anything is removed.
4. Brings the slot stack down with volumes — skipped under `--skip-docker-down`, when nothing remains, and when `config: stack.runtime = "none"`.
5. Copies the story's propagation checklist and range diffs (`.workflow/state/story-setup/propagation-<id>.md`, `range-diff-<id>-*.txt`) into `config: paths.story_history_dir`/`<id>/` (prints `story file preserved: ...`), and `<id>-record.md` out of the worktree into `config: paths.specs_dir` in the main checkout (prints `story record preserved: ...` or `story record: none found`). The `checkpoint-3 merge approved` line was appended after the close-out commit, so the merge did not move it; this copy keeps it.
6. `git worktree remove --force <path>`, with a recursive-delete + `git worktree prune` fallback.
7. Deletes the story branch, its train branch and `refs/train-tested/` ref, and its `refs/review/` refs (skipped under `--skip-branch-delete`).
8. Frees the slot-registry row via `release_slot.py` (after the worktree and branch are gone, so a cleanup that dies earlier leaves `in_use` as the evidence).
9. When `config: graph.tool` is not `none`: deletes the worktree's graph project named by the `.workflow/state/story-setup/<stack>.graph_project` sidecar (never guesses a name).
10. Removes the stack's setup artifacts and prints a summary.

**Run with `--dry-run` first** when uncertain: it prints every watcher candidate (pid + cwd) and the kill list before anything is touched.

Check the summary's `Slot released :` line. `released` or `already free` is done; `FAILED` means the row is still `in_use` — run `uv run --no-project .workflow/scripts/release_slot.py --story-id <id>` by hand and report it.

Exit 1 with `ERROR: compose-watch processes still running: <pids>`: nothing was removed. Report the PIDs to the owner and stop before Step 4b; rerun the cleanup once they are cleared. Never kill them with another command.

## Step 4b — Propagation apply

When `<config: paths.story_history_dir>/<id>/propagation-<id>.md` (Step 4's copy) has `## Decisions` with update or follow-up rows, dispatch the `story-propagate` subagent:

```
mode: apply
story_id: <id>
docs_root: <the main checkout>
approved:
- <absolute path> — <row ids and decision>
rows: <the approved rows with their final wording, verbatim from the file>
```

List every file an approved row edits under `approved:` (follow-up rows edit `<config: paths.deferred_work_dir>/story-<id>.md`). Stage each `- updated:` path from its report in Step 5's commit; relay its `Not applied` rows to the owner. Keep the propagation file: it is the story's tracked record of the rulings, committed in Step 5. No file or no approved rows: skip this step.

## Step 5 — Sprint status + bookkeeping commit

Use `.workflow/scripts/sprint_status.py` — never a hand-rolled `sed -i` / `awk` / throwaway-script edit. The file is addressed **by story key, never by line number**.

Step 5 must achieve:

- the story's row → `done`
- a one-line note (max `config: sprint_status.note_max_chars`) carrying `MERGED <date> (merge <sha>; impl <sha>; close-out <sha>)`, the slot freed, and the story's phase marker. `set` archives the replaced note; put the long close-out narrative (review rounds, gate results, carves) there with `--history "..."`, never in the note
- `last_updated` refreshed with `touch --note "<one sentence>"`

If the script genuinely cannot express the edit, say so and edit by **key match** — then check the diff size before committing: a few-line change that produces a whole-file diff means line endings were rewritten.

One bookkeeping commit on the base branch: `chore: mark <story-id> done in sprint-status, free slot <N> in registry` (plus any attribution trailer the project or tool requires), holding the sprint-status flip, the slot-registry change, the `<id>-record.md` Step 4 copied, the files it copied into the story-history dir, and the Step 4b `- updated:` paths.

**Stage only this session's hunks.** The main checkout's index is shared, and the tracker, the registry, the epics and the story history often carry other sessions' uncommitted edits. For each file `git diff <file>` shows another session's hunks in: `uv run --no-project .workflow/scripts/stage_own_hunks.py list <file>`, then pass only the hunks this session wrote as `--hunks <file> <spec>`; a file holding only this session's changes: `--add <file>`. Commit in one shell command: `T=$(uv run --no-project .workflow/scripts/stage_own_hunks.py gate --hunks <file> <spec>... --add <file>...) && git commit -F <msgfile> && uv run --no-project .workflow/scripts/stage_own_hunks.py verify "$T"` — `git commit` with no paths (never `--only`). `gate` exit 3: another session's changes are in the index and nothing was committed; wait and retry, never unstage them. `verify` exit 4: the commit took foreign paths; stop and report them.

After the bookkeeping commit, release the land lease — Step 5 moves the base branch after the merge, so an earlier release lets the next story's land guard trip on this story's own commits:

```bash
uv run --no-project .workflow/scripts/land_lease.py release <story_id>
```

Exit 0 (released, or `already free`) is done. Exit 1 (held by another story) is reported, not fatal. `--cleanup-only` runs this release too. A halt after the Step 2 merge keeps the lease until this step completes; resume with `land-story <id> --cleanup-only`.

## Step 6 — Recommend a session clear

The story is complete: branch merged and deleted, worktree removed, stack down, slot `free`, sprint status `done`. Report completion, then **recommend clearing the session context before the next story** as the reply's last line. Never clear it yourself — state the recommendation and let the owner decide.

A story interrupted before this point is resumed with `uv run --no-project .workflow/scripts/story_ledger.py <story-id>`.

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `git worktree remove` fails with "busy" / "permission denied" on a freshly-downed stack | An orphan watcher process is pinning the worktree directory | `cleanup_story_stack.py` handles it; re-run with `--dry-run` to see every candidate (pid + cwd) |
| `land-merge` exits 3 | A conflict (the story drifted from the base branch), or the main checkout's own uncommitted changes | See Step 2's exit table; never auto-resolve on the base branch |
| A shared-slot service exited after the merge | New env var or dependency; the container needs recreate/rebuild | Follow the `post-merge-smoke` diagnosis and run its recommended command by hand |
| `Cannot resolve worktree for story id ...` | Registry row already `free` AND no `<id>` / `<id>-*` directory under the worktrees root | Likely already cleaned up — check `git worktree list`; pass `--worktree-root` or remove by hand |

## When NOT to use

- During implementation — parallel-dev-wave phases are still in flight.
- For the shared slot — it is never assigned to a story.

## When TO use

- As the land window's last step, once the owner approved the merge and the story holds the lease.
- For orphan recovery when an earlier session left a worktree directory behind — `--cleanup-only`.
