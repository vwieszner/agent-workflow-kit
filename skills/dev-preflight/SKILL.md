---
name: dev-preflight
description: Verify a dev stack (the shared slot or a story slot) is healthy and ready before any test run. Catches stale or exited services, a dead watch, code out of sync with the container, failing health checks, and wrong-branch state before they pollute the workflow. The gate is a deterministic script; the dev-preflight subagent is diagnosis-only.
---

## The gate is the script

If `config: stack.runtime = "none"`: there is no stack to check. State "preflight skipped — stack.runtime = none", confirm the worktree's branch with `git rev-parse --abbrev-ref HEAD`, and proceed.

Otherwise run it from the repo root (read-only):

```
uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack> --worktree <src-root>
```

- `--project-name` — the compose project to check (`config: slots.shared_stack_name` for the shared slot; `config: slots.stack_prefix` + story id for a story slot)
- `--worktree` — the source root the stack was brought up from (the main checkout for the shared slot; the slot's worktree otherwise)
- `--expected-branch` — optional; defaults to the story branch, or `config: git.base_branch` for the shared slot

It checks: every service in `config: stack.services` running; every `config: stack.health.checks` command; watch liveness when `config: stack.watch`; code sync when `config: stack.health.container_src` is set (read-only hash compare); and branch. Report its output verbatim.

**Exit 0** → proceed. **Exit 1** → surface the failing lines and HALT; never run tests against a broken stack.

The sync check samples a few recently modified files. When the branch's own changed files must be live, verify them explicitly:

```
uv run --no-project .workflow/scripts/check_container_sync.py -p <stack> <paths>
```

## Diagnosis when it fails

Only after the script reports red, and only for detail beyond its own FAIL lines: dispatch the `dev-preflight` subagent with `project_name` (required), `failing_checks` (the script's FAIL lines), and optionally `sample_file` / `expected_branch` / `worktree`.

The subagent is **diagnosis-only, report-only** — it never runs stack lifecycle commands and never modifies a file. **Never use the subagent as the gate.**
