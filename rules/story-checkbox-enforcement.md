---
name: story-checkbox-enforcement
description: MANDATORY. Mark a story's task/subtask checkboxes [x] as each task's code and tests are done — before any git commit. Never commit or mark a story done with unchecked completed tasks.
---
# Story Checkbox Enforcement

## Trigger
A task or subtask's code is written AND its tests pass.

## Mandatory sequence
1. Mark `[x]` on that task/subtask in the story spec's Tasks / Subtasks section
   (`config: paths.specs_dir`).
2. Update the spec's dev-agent record with a brief note of what was done.
3. Update the spec's file list with every new, modified, or deleted file.
4. Only then commit — the commit contains the code AND the updated story file.

## Forbidden
- ❌ `git commit` before the story file is updated.
- ❌ Batching all checkbox updates into a final documentation step.
- ❌ Setting story status to `done` while tasks still show `[ ]`.
- ❌ Implementing code + tests but leaving the checkboxes `[ ]`.

The story file is the source of truth for completion: `[ ]` means NOT DONE regardless of the code.
