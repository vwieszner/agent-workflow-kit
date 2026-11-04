---
name: land-story
description: Take a story from "ready to merge" through the merge into the base branch (under the shared slot's gate lock, while the story holds the land lease), post-merge smoke check, slot cleanup, propagation apply, and sprint-status update. Invoke as the last step of parallel-dev-wave's land window, after the owner's explicit merge confirmation. Also invokable as `land-story <id> --cleanup-only` for orphan recovery when an earlier session merged but did not finish. Cleanup runs .workflow/scripts/cleanup_story_stack.py, which also kills orphan watcher processes that would otherwise pin the worktree directory.
---

Follow the instructions in [workflow.md](workflow.md).
