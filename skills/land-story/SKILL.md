---
name: land-story
description: Take a story from "ready to merge" through the merge into the base branch, post-merge smoke check, slot cleanup, and sprint-status update. Invoke after parallel-dev-wave reaches Checkpoint 3 with the owner's explicit merge confirmation. Also invokable as `land-story <id> --cleanup-only` for orphan recovery when an earlier session merged but did not finish cleanup. Cleanup runs .workflow/scripts/cleanup_story_stack.py, which also kills orphan watcher processes that would otherwise pin the worktree directory.
---

Follow the instructions in [workflow.md](workflow.md).
