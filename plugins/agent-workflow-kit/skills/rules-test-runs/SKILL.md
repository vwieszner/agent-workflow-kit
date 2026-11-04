---
name: rules-test-runs
description: How story agents run and record scoped tests — one invocation for every leg, sync check first, batch-fix rounds, the self-recorded GREEN line, owned process kills only. Preloaded into story-impl, story-finalize and story-rebase.
user-invocable: false
---

- **One invocation, every leg the story touches.** Leaving out a leg the story touches is a false green.
  ```bash
  uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/run_scoped_tests.py --story-id <story_id> \
    --record <story_id> <phase> \
    <labels for the default leg> [--leg <name>=<value>]... [--scripts]
  ```
  Named legs are the entries of `config: tests.scoped_legs`; one value per `--leg`, repeat the flag. A story whose tests live in `config: tests.scripts_tests_dir` passes `--scripts` for the red check and the green runs alike: it takes no value and runs that whole directory on the host in the story's worktree. `--scripts-test <path>` runs single files, never with `--record`.
- **Sync check before a run.** After editing files the stack runs (not the host-run `--scripts` leg), from the worktree root: `uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/check_container_sync.py -p <stack> --wait 60 <each file edited since the last run>` (a deleted file: add `--absent`). Exit 0 → run. Exit 1 (drift after the wait) → compose watch stopped syncing: return an environment HALT with the script's lines. Never `--fix`. The runner's `--require-synced` checks only that a file exists, not its content. Skipped (and said) when `config: stack.runtime = "none"`.
- **Waiting on a background run.** Wait with the tool's monitor on an until-loop, or one until-loop call with a timestamped heartbeat and an end bound under the tool cap (`end=$((SECONDS+540))`). A foreground `sleep N` followed by a check is blocked. In Claude Code `Monitor` and `TaskStop` are deferred tools: load them with ToolSearch (`select:Monitor,TaskStop`) before the first call.
- **After stopping a background task.** Stopping a task can leave its children running (Windows does not cascade termination). List them with `uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/proc_scan.py --match <regex>` and stop your own with `uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/kill_owned.py --pid <pid>… --tree`; the process-kill guard adds your worktree and launch time. `kill`, `pkill`, `taskkill` and `Stop-Process` are denied. A `REFUSE` line is final: report it to the orchestrator, never work around it.
- **The green run records itself.** With `--record <story_id> <phase>` the script appends the `<phase> tests GREEN` line, carrying the HEAD and the tested-tree hashes (`code-tree`, plus `scripts-tree` when the scripts leg ran), only when every requested leg and every `config: tests.record_checks` check passed. Never append a GREEN line by hand: your return value dies with the orchestrator's conversation; the phase gate and Checkpoint 3 read this line.
- **Checks a recorded run adds.** With `--record` the runner adds `config: tests.record_invariant_labels` to the default leg and runs every `config: tests.record_checks` entry (lint, static checks on the changed files). Their failures belong to the same round's fix list as test failures. To check before the recorded run, run the same command on its own.
- **Batch-fix rounds.** On failures: capture the full failure list, find each root cause, fix all of them in one pass, then run the scoped tests once. A new round starts only if that run surfaces new or remaining failures.
- **No test weakening.** No timeout bumps (except a per-test timeout meeting `${CLAUDE_PLUGIN_ROOT}/rules/parallel-test-isolation.md`'s criterion), skips or relaxed assertions to make a failure go away.
- **Snapshot files.** Regenerate a snapshot only when the story intentionally changes that output and the test fails on the snapshot alone, and regenerate it where the tree under test is (a regeneration inside the container leaves the worktree's copy stale: copy it back). An unintended snapshot mismatch is a failure to root-cause.
- **Human judgment is a HALT.** An ambiguous root cause, a design call or a scope question: stop and report the question concretely.
