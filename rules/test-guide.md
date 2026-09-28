---
name: test-guide
description: MANDATORY for ALL test execution. Which entry point to use (scoped vs full gate), required non-interactive/test-isolation flags, sequential execution, code freeze, batch-fix iteration, and failure-analysis discipline.
---
# Test Execution Guide

Command templates live in `config: tests.*`; required flags in `config: tests_policy.required_flags`.
When `config: env.app_runs_in_container` is true, every test command runs in the container
(`host-passive-environment.md`).

## 1. Entry points
| Purpose | Command |
|---|---|
| Scoped iteration / agent validation loop | `uv run --no-project .workflow/scripts/run_scoped_tests.py --project-name <stack> <labels...>` — verdict, exit code, full failing-id list and capped tails reach the context; the raw log goes to a temp file |
| Full gate (all legs: backend + unit + e2e) | `uv run --no-project .workflow/scripts/run_full_suite.py` (shared stack) or `... --story-id <id>` (isolated stack) |

- The full gate owns preflight, slot resolution and output parsing — do not run preflight separately
  ahead of it. Any one leg failing fails the gate.
- **Concurrent full-gate invocations queue** on a host-global lock (announced periodically). Exhausting
  the lock timeout (`config: tests.full_lock_timeout_min`) FAILS the run; it never proceeds unlocked.
- Scoped runs are NOT covered by the gate's preflight: run
  `uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack> --worktree <worktree-root>`
  yourself first — the worktree root, never its `config: stack.health.worktree_src` subfolder (the
  script appends it).
- If the owner asks to "run tests", "check the app", or "verify the build", the job is done only when
  the full gate passes.

## 2. Required flags — non-interactive and test-isolated
- Every test invocation MUST carry every flag in `config: tests_policy.required_flags` (typically: a
  non-interactive flag so setup prompts cannot hang the runner, and the test settings/profile so the
  run cannot reach live brokers, queues or databases). Wrappers may not add them for you — check.
- E2E runners run in CI/non-interactive mode so the HTML reporter does not start a server and hang
  the process, with runner retries disabled (e.g. `--retries=0`) — a retry turns a failure into a pass. Never try to open an HTML report from the CLI — tell the owner its path.
- A wrong test label may not error clearly (some runners report a synthetic 1-test failure). Before
  treating a 1-test failure as real, confirm the label resolves to what you intended (labels are
  relative to the in-container source root, not the repo root).

## 3. Execution discipline
- **Sequential:** never start a suite while another is running against the same stack.
- **Parallel stories:** when several agents work on different stories, each runs its own tests as soon
  as it is ready — none waits for its siblings.
- **Code freeze (per stack):** do not modify any file in a worktree while that worktree's stack runs a
  suite. Editing another slot's worktree is fine.
- **Exit code, not summary:** `verify-ground-truth.md`.
- **Parallel safety:** new tests must be safe under the parallel gate — `parallel-test-isolation.md`.
- **Adapter tests:** logic moved into an infrastructure adapter gets a dedicated test for that adapter,
  with a real signature (`real-signature-tests.md`).
- **LLM-needing tests** use the deterministic mock provider — never a real/paid LLM.

## 4. Batch-fix iteration
When fixing N failures within one agent's scope:
1. Capture the failure list ONCE.
2. Fix ALL N failures. Do not run any test between individual fixes — the captured list is the safety net.
3. Validate ONCE, scoped to the tests covering the list.
4. New or remaining failures → a new round: capture once, fix all, validate once. Repeat until green.

If a failure needs human judgment (ambiguous root cause, design decision, scope question, competing
directions), STOP and ask the owner with file, line and options.

Dispatch prompts must not instruct per-fix test runs (no "after each", "between fixes", "confirm green
before moving on", "per fix", or "iterate" near "test"/"run"/"verify"/"patch"). Enforced by
`.workflow/hooks/guards/dispatch_prompt.py`.

## 5. Failure analysis
- Root cause first (`root-cause-before-fix.md`). Do not revert code to make tests pass; do not raise
  timeouts before every logical cause is ruled out.
- Enable traces and screenshots on failure; read the trace before guessing.
- E2E failures follow `e2e-debugging-protocol.md`.
- Tests are not done until the docs that describe the tested behaviour match the code.
