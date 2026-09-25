---
name: post-merge-smoke
description: Run the mandatory post-merge regression gate on the shared dev slot after any story branch merges into the base branch. Checks that the configured smoke services are running, scans their recent logs for errors, diagnoses the failure class, and confirms the baseline is clean before the next story starts. Use immediately after every story merge — if someone says "just merged", "merge is done", "story X landed", or asks to run the smoke check, invoke this skill.
---

## Execution

Single tool call. The script handles service-state probing, log scanning, diagnosis, and read-only drift detection — no subagent, no model latency.

```
uv run --no-project .workflow/scripts/post_merge_smoke.py
```

The script:

1. Probes the shared stack (`config: slots.shared_stack_name`) and verifies every service in `config: smoke.services` is running.
2. Pulls recent log lines from each (window `config: smoke.log_since`) and scans them for `config: smoke.log_error_patterns`.
3. Runs the read-only `config: stack.health.checks` and, when `config: stack.health.container_src` is set, confirms the containers serve the merged files (source-sync gate over the merge's own file set).
4. On any failure, classifies the diagnosis (schema/migration drift, dependency or import breakage, env/config change, unknown) with the verbatim log evidence.
5. Prints the recommended fix command; never auto-applies destructive operations (volume wipes, rebuilds, forced recreates) — the owner or orchestrator runs those by hand after reading the diagnosis.

Exit code 0 on green, 1 on any failure.

When `config: stack.runtime = "none"` there is no shared stack: the script reports the stack checks as skipped. Report that skip explicitly — it is not a pass.

A red baseline is never exempt: a broken shared slot bleeds into every subsequent story. Fix it before the next story starts.

## Output contract

Report the script's stdout verbatim.

There is no post-merge-smoke agent. This skill is the script and nothing else — never dispatch an agent for it.
