---
name: run-tests
description: Run the full test gate (every configured leg) with preflight and parse in one script call. Use whenever someone says "run the suite", "run all tests", "run the full suite", or asks for a full regression run. Takes story_id (optional) to target a story's isolated slot stack; otherwise targets the shared stack.
---

## Execution

One tool call. The script owns the lock, the preflight, every gate phase and the parse — no subagent dispatch.

Shared stack:

```bash
uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/run_full_suite.py
```

A story's slot stack:

```bash
uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/run_full_suite.py --story-id <id>
```

The script:
1. Takes the stack's gate lock (`config: tests.gate_lock_scope`, default per stack). **One full suite runs per stack at a time**: a second session on the same stack QUEUES, first-come-first-served, with a heartbeat every 30 s; gates on other stacks run concurrently. Exhausting `--lock-timeout-minutes` (default `config: tests.full_lock_timeout_min`) fails the run; it never proceeds unlocked.
2. Runs `stack_preflight.py` (skipped and announced when `config: stack.runtime = "none"`).
3. Runs the schema-drift check only when a `config: pipeline.schema_globs` file changed, then `config: tests.full_pre_phases`, the concurrent legs (`config: tests.full_cmd`, `unit_frontend_cmd`, `e2e_cmd`) and `config: tests.full_post_phases`.
4. Parses the legs and prints a structured per-leg summary with the COMPLETE failing-id list; the raw log goes to a temp file whose path is printed.

**A queued run is not a hung run.** `queueing (position <k>, ...)` and `still waiting on the gate lock... Ns elapsed` mean another session holds this stack's lock — wait; do not kill it and do not start a second gate.

## Output contract

Report the script's stdout verbatim. No reinterpretation, no diagnosis — the parser produces the canonical summary. Report the wall time the script emitted; never estimate a duration.
