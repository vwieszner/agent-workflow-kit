# Land Story

**Goal:** Take a story from "ready to merge" through merge, post-merge smoke, cleanup, and sprint-status update — the back half of the lifecycle that `parallel-dev-wave` hands off after Checkpoint 3.

**Precondition:** the story's record file carries `checkpoint-3 merge approved`. Before anything else (skip only with `--cleanup-only`), run:

```
uv run --no-project .workflow/scripts/story_record.py check <story_id> checkpoint-3
```

Exit 1 → STOP: go back to the owner; never write the approval on their behalf. `.workflow/hooks/guards/phase_dispatch.py` also blocks the skill invocation when the tool passes the story id with it (Claude Code); this check is the gate wherever it does not (OpenCode's skill tool carries no arguments).

All commands run from the repo root (`uv run --no-project .workflow/scripts/wfconfig.py root`).

## Inputs

- **`story_id`** — required. The id matching the slot registry (`config: paths.slot_registry`).
- **`--cleanup-only`** — optional. Skip merge + smoke; only run cleanup. Use when a previous session merged but did not finish cleanup.

## Step 1 — Confirm preconditions

Read these in parallel:

- `git worktree list` — the story's worktree must be present (unless `--cleanup-only` and the dir is the orphan being recovered).
- `git log --oneline <config: git.base_branch> -5` — the merge target's tip.
- The slot registry — confirm the story id matches an `in_use` row.

If `--cleanup-only`: skip to **Step 4**.

## Step 2 — Merge into the base branch

```bash
git checkout <config: git.base_branch>
git merge <config: git.merge_style> <config: git.branch_prefix><story_id> -m "Merge <branch>: <one-line summary>"
```

The merge style is mandatory as configured (default `--no-ff` — preserves the branch shape in history).

If the merge reports a conflict: HALT, surface the conflicting files, do NOT auto-resolve. The owner decides. Once resolved and committed, proceed.

## Step 3 — Post-merge smoke check

Invoke the `post-merge-smoke` skill — it runs `.workflow/scripts/post_merge_smoke.py` against the shared slot (`config: slots.shared_slot`): service state, the log scan, and the source-sync gate (E), which hash-checks that the containers serve the merge's own files. There is no post-merge-smoke agent; never dispatch one.

If the smoke surfaces a failure clearly caused by the merge (e.g. a new env var not picked up by a running container, a new dependency missing from an image): apply its recommended fix (recreate / rebuild) before proceeding. Destructive fixes are run by hand after reading the diagnosis — the script never applies them.

When `config: stack.runtime = "none"` the smoke reports the stack checks skipped; say so in the completion report.

## Step 4 — Cleanup

Invoke the cleanup script:

```bash
uv run --no-project --with psutil .workflow/scripts/cleanup_story_stack.py <id>
```

For `--cleanup-only` (orphan recovery), add the skip flags:

```bash
uv run --no-project --with psutil .workflow/scripts/cleanup_story_stack.py <id> --skip-docker-down --skip-branch-delete
```

(`--with psutil` supplies the optional process-inspection dependency the orphan detection uses; the script runs without it but reports reduced detection.)

What the script does:

1. Resolves the worktree path from the slot registry (falls back to a unique `<id>` / `<id>-*` directory under `config: git.worktrees_root` if the row is already `free`; `--worktree-root` overrides the root).
2. Finds orphan watcher processes: compose processes whose command line carries a bare `watch` subcommand token (the `--watch` flag form used on the shared slot is excluded) and whose working directory is inside the target worktree. The working-directory prefix match is the safety net — it cannot reach another slot's watcher.
3. Kills each orphan and its parent compose process, then pauses briefly so the OS releases directory handles.
4. Brings the slot stack down with volumes (`-p <stack> down -v`) — skipped under `--skip-docker-down`, auto-skipped when nothing remains, and skipped when `config: stack.runtime = "none"`.
5. Copies `<story-id>-record.md` out of the worktree into `config: paths.specs_dir` in the main checkout (prints `story record preserved: ... -> ...`, or `story record: none found`). The `checkpoint-3 merge approved` line was appended after the close-out commit, so the merge did not move it; this copy is the only thing that keeps it.
6. `git worktree remove --force <path>`, with a recursive-delete + `git worktree prune` fallback.
7. Deletes the story branch (skipped under `--skip-branch-delete`).
8. Frees the slot-registry row via `release_slot.py` (lock-protected; `not_found` = already free, idempotent). It runs after the worktree and branch are gone, so a cleanup that dies earlier leaves `in_use` as the evidence.
9. When `config: graph.tool` is not `none`: deletes the worktree's graph project, named by the `.workflow/state/story-setup/<stack>.graph_project` sidecar. Skipped with a warning if the sidecar is missing or empty — it never guesses a name.
10. Removes `.workflow/state/story-setup/<stack>.{json,log,graph_project}` and prints a summary (killed PIDs, worktree gone, branch gone, slot released, graph project).

**Run with `--dry-run` first** when uncertain. Misclassifying the shared slot's watcher as an orphan would silently stop file sync for shared dev work; `--dry-run` prints every candidate (pid + cwd) and the kill list before anything is touched.

Check the summary's `Slot released :` line. `released` or `already free` is done; `FAILED` means the row is still `in_use` — run `uv run --no-project .workflow/scripts/release_slot.py --story-id <id>` by hand and report it.

## Step 5 — Sprint status + bookkeeping commit

Use `.workflow/scripts/sprint_status.py` — never a hand-rolled `sed -i` / `awk` / throwaway-script edit. The file is addressed **by story key, never by line number**.

```bash
uv run --no-project .workflow/scripts/sprint_status.py --help            # exact flags
uv run --no-project .workflow/scripts/sprint_status.py get <story-key>    # the row
```

Step 5 must achieve:

- `development_status[<story-key>]` → `done`
- a one-line note (max `config: sprint_status.note_max_chars`) carrying `MERGED <date> (merge <sha>; impl <sha>; close-out <sha>)`, the slot freed, and the story's phase marker. `set` archives the replaced note to `config: paths.story_history_dir`/`<key>.md`; put the long close-out narrative (review rounds, gate results, carves) there with `--history "..."`, never in the note
- `last_updated` refreshed with `touch --note "<one sentence>"` (the old one is archived)

If the script genuinely cannot express the edit, say so and edit by **key match**, never by line number — then check the diff size before committing: a few-line change that produces a whole-file diff means line endings were rewritten.

Make a single bookkeeping commit on the base branch:

```
chore: mark <story-id> done in sprint-status, free slot <N> in registry
```

(plus any attribution trailer the project or tool requires). Include in this same commit:

- the `<story-id>-record.md` that Step 4 copied into `config: paths.specs_dir` — it is the story's only durable record of the owner's approvals and test verdicts;
- the slot-registry change.

If Step 4 printed `Slot released : FAILED`, the row is still `in_use`: run `release_slot.py --story-id <id>` by hand and say so — a stuck `in_use` row makes `reserve_slot.py` treat the slot as taken and sends `story_ledger.py` to a dead worktree.

## Step 6 — Recommend a session clear

The story is complete: branch merged and deleted, worktree removed, stack down, slot `free`, sprint status `done`. Every artifact is on disk.

Report completion, then **recommend clearing the session context before the next story** as the reply's last line. One story per session keeps per-call context near the floor. Never clear it yourself — state the recommendation and let the owner decide.

A story interrupted before this point is resumed with `uv run --no-project .workflow/scripts/story_ledger.py <story-id>`.

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `git worktree remove` fails with "busy" / "permission denied" on a freshly-downed stack | An orphan watcher process is pinning the worktree directory | `cleanup_story_stack.py` handles it. If it still fails, re-run with `--dry-run` to see every candidate (pid + cwd) and confirm the command-line + working-directory match catches the orphan |
| Merge reports a conflict | The story drifted from the base branch | HALT, surface to the owner. Resolve manually; never auto-resolve |
| A shared-slot service exited after the merge | New env var or dependency; the container needs recreate/rebuild | Follow the `post-merge-smoke` diagnosis and run its recommended command by hand |
| `Cannot resolve worktree for story id ...` | Registry row already `free` AND no `<id>` / `<id>-*` directory under the worktrees root | Likely already cleaned up — check `git worktree list`; if truly orphaned elsewhere, pass `--worktree-root` or remove by hand |

## When NOT to use

- During implementation — parallel-dev-wave phases are still in flight. This skill expects the branch ready-to-merge or already merged.
- For the shared slot (`config: slots.shared_slot`) — it is never assigned to a story; if it is the target, there is a workflow violation upstream.

## When TO use

- Immediately after parallel-dev-wave's Checkpoint 3, once the owner confirmed and `checkpoint-3 merge approved` is recorded.
- For orphan recovery when an earlier session left a worktree directory that will not delete — `--cleanup-only`.
- After resuming a session where parallel-dev-wave was interrupted between Checkpoint 3 and cleanup.
