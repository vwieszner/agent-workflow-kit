#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(Bash|PowerShell) guard for test-adequacy: the mutation runner only.

Scope: callers whose `agent_type` is in `config: guards.mutation_command_agents` (default test-adequacy).
Allows a shell command only when it is ONE invocation of the main checkout's runner —
`<config: env.host_tooling_runner> <main checkout>/.workflow/scripts/run_mutation.py <args>` (absolute path,
never a worktree's copy) — whose every `-` token is one of `--project-name --worktree --diff-range --module --test
--timeout`, whose `=` values do not start with `-`, with no shell operator, quote, redirection or substitution.
The agent has no write tool, so the shell is its only way to change anything. Switch:
`config: guards.mutation_command`. Deny: hookSpecificOutput JSON, exit 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
from repro_paths import runner_check  # noqa: E402

HOOK = "mutation_command.py"
OPTIONS = {"--project-name", "--worktree", "--diff-range", "--module", "--test", "--timeout"}


def evaluate(payload: dict) -> int:
    if payload.get("agent_type") not in set(g.cfg_list("guards.mutation_command_agents", ["test-adequacy"])):
        return 0
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return 0
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cmd = str(ti.get("command") or "").strip()
    why = runner_check(cmd, "run_mutation.py", OPTIONS)
    if why:
        runner = g.cfg("env.host_tooling_runner", "uv run --no-project")
        return g.deny_json(
            f"[workflow-hook] {cmd[:120]!r}: {why}. test-adequacy runs only `{runner} "
            f"{Path(g.wfconfig.repo_root()) / '.workflow' / 'scripts' / 'run_mutation.py'} <args>` (options "
            f"{' '.join(sorted(OPTIONS))}; values match [\\w./:=@,+-]). Read sources with the read/grep tools. "
            f"(Hook: .workflow/hooks/guards/{HOOK})")
    return 0


if __name__ == "__main__":
    g.run("mutation_command", evaluate)
