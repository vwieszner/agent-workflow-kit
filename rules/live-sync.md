---
name: live-sync
description: MANDATORY when config stack.watch = true. Live code sync into containers uses `compose watch` only; never add source-code volume mounts. How to verify sync and what to do when it breaks.
---
# Live Code Sync

Applies when `config: stack.runtime` is not `"none"` and `config: stack.watch` is `true`.

## Rule
Live sync from worktree to containers is done by `{compose} watch` (the compose file's
`develop.watch` blocks). It must be running for every stack you edit and test against.

- ❌ **Forbidden:** adding host-to-container source-code volume mounts to the compose file, or any
  other manual sync mechanism, to solve a sync problem.
- ✅ Respect the existing `develop.watch` configuration.

## When sync is suspect
1. Verify: `.workflow/scripts/check_container_sync.py`, or a host-vs-container checksum of one edited
   file (`config: stack.health.container_src` ↔ `config: stack.health.worktree_src`).
2. If out of sync, check that the watch process for that worktree is alive; restart it in the right
   worktree (or ask the owner to).
3. Do not trust any test result from a stack whose sync you have not verified.

## Code freeze
Do not modify files in a worktree while that worktree's stack is running a test suite — watch
resyncs mid-run and produces flaky failures with no clear cause. The freeze is per-stack: editing a
different slot's worktree is fine.
