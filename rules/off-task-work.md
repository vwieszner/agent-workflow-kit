---
name: off-task-work
description: MANDATORY. Work built off-task moves onto its own branch with a carved story, never reverted, and the assigned task resumes.
---
# Work built off-task moves to a branch

## Trigger
You find you have built something the current task did not ask for (you drifted from the task).

## Rule
1. Stop building it, and do not revert it.
2. Move it onto its own branch, your own hunks only — other sessions' uncommitted edits can sit in
   the same files:
   - `git worktree add -b <config: git.branch_prefix><slug> <config: git.worktrees_root>/<slug> <config: git.base_branch>`
   - per file: `uv run --no-project .workflow/scripts/stage_own_hunks.py list <file>`, then
     `uv run --no-project .workflow/scripts/stage_own_hunks.py pick <file> <patch> <your hunks>`;
   - apply each patch on the branch with `git -C <worktree> apply --unidiff-zero <patch>`;
   - take it back out where the work was made with `git apply -R --unidiff-zero <patch>`;
   - move each new file you created to the same path in the worktree.

   Never restore a whole file.
3. Carve a story to finish it: an epic entry plus a sprint-status row (`config: paths.sprint_status`),
   committed on its own.
4. Tell the owner in one line (branch + story key), then return to the assigned task and finish it.

## Forbidden
- ❌ Reverting the off-task work before it sits on its branch.
- ❌ Re-implementing it after a revert.
- ❌ Pausing the assigned task to finish the off-task work.

Discarding the work takes the owner's explicit discard.
