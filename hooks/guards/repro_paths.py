#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(Write|Edit|Bash) guard for repro-writer and pbt-writer: untracked repro / property test files and
the scoped test runner only.

Scope: callers whose `agent_type` is in `config: guards.repro_paths_agents` (default repro-writer, pbt-writer).

Bash: allowed only as ONE invocation of the main checkout's runner —
`<config: env.host_tooling_runner> <main checkout>/.workflow/scripts/run_scoped_tests.py <args>` (absolute path;
never a worktree's copy, which the story under review could change) — whose every `-` token names an option in
`config: review.repro_runner_options`, whose `=` values do not start with `-`, and which holds no shell operator,
quote, redirection or substitution (tokens match `[\\w./:=@,+-]`), so no shell write bypasses the file rule.

Write / Edit: allowed only when the target
  * lies in a git worktree root directly under `config: git.worktrees_root` (`git rev-parse --show-toplevel`
    equals it: a plain folder there tracks nothing, so the untracked check alone would pass every path);
  * has a repro test name (`config: review.repro_test_name_regex`, matched exact-case on the file name);
  * lies under one of `config: review.repro_roots` (worktree-relative; [] = anywhere in the worktree);
  * for a `.py` file, sits in a directory named in `config: review.repro_py_parent_dirs` ([] = any);
  * is not tracked in that worktree (`git ls-files --error-unmatch`, case-sensitive).
Everything else is denied. Switch: `config: guards.repro_paths`. Deny: hookSpecificOutput JSON, exit 0.

`REPRO_NAME_DEFAULT` is also how story-finalize and the ledger recognise the untracked repro files a review
round leaves.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
import _paths  # noqa: E402

HOOK = "repro_paths.py"
REPRO_NAME_DEFAULT = r"^(test_(?:repro|pbt)_r\d+_\d+\.py|repro_r\d+_\d+\.test\.[cm]?[jt]sx?|repro-r\d+-\d+\.spec\.[cm]?[jt]s)$"
RUNNER_OPTIONS_DEFAULT = ["--project-name", "--story-id", "--tail", "--timeout", "--require-synced"]
TOKEN = re.compile(r"^[\w./:=@,+-]+$")


def runner_check(cmd: str, script: str, options: set[str]) -> str | None:
    """None when `cmd` is one allowed invocation of `script` (absolute main-checkout path), else the reason."""
    prefix = shlex.split(str(g.cfg("env.host_tooling_runner", "uv run --no-project")))
    parts = cmd.split()
    if prefix and parts[:len(prefix)] == prefix:
        rest = parts[len(prefix):]
    elif parts[:1] == ["python3"]:
        rest = parts[1:]
    else:
        return "not started with the host tooling runner (config env.host_tooling_runner) or python3"
    if not rest or not _paths.is_absolute(rest[0]) or \
            _paths.canon(rest[0]) != _paths.canon(str(Path(g.wfconfig.repo_root()) / ".workflow" / "scripts" / script)):
        return f"not the main checkout's absolute .workflow/scripts/{script}"
    for tok in rest[1:]:
        if not TOKEN.match(tok):
            return f"token {tok!r} holds a shell character"
        if tok.startswith("-"):
            name, _, value = tok.partition("=")
            if name not in options:
                return f"option {name!r} is not allowed"
            if value.startswith("-"):
                return f"value of {name} starts with '-'"
    return None


def write_check(raw: str) -> str | None:
    if not raw or not _paths.is_absolute(raw):
        return f"{raw!r} is not an absolute path"
    wt_root = str(g.wfconfig.worktrees_root())
    if not _paths.under(raw, wt_root) or _paths.canon(raw) == _paths.canon(wt_root):
        return f"{raw!r} is outside the story worktrees ({wt_root})"
    path = os.path.normpath(raw).replace("\\", "/")
    rel_root = path[len(os.path.normpath(wt_root).replace("\\", "/")):].lstrip("/")
    parts = rel_root.split("/")
    if len(parts) < 2:
        return f"{raw!r} is not inside a worktree"
    worktree = str(Path(wt_root) / parts[0])
    inner = "/".join(parts[1:])
    name = parts[-1]
    if not re.search(str(g.cfg("review.repro_test_name_regex", REPRO_NAME_DEFAULT)), name):
        return (f"{name!r} is not a repro test name (config review.repro_test_name_regex: test_repro_r<round>_<n>.py, "
                f"test_pbt_r<round>_<n>.py, repro_r<round>_<n>.test.<ext>, repro-r<round>-<n>.spec.<ext>)")
    roots = [str(r).strip("/") for r in g.cfg_list("review.repro_roots", []) if str(r).strip("/")]
    if roots and not any(inner == r or inner.startswith(r + "/") for r in roots):
        return f"{inner!r} is not under a repro root ({', '.join(roots)})"
    py_dirs = g.cfg_list("review.repro_py_parent_dirs", ["tests", "test"])
    if name.endswith(".py") and py_dirs and (len(parts) < 3 or parts[-2] not in py_dirs):
        return f"{inner!r}: a Python repro test sits in a {'/'.join(py_dirs)} directory"
    top = subprocess.run(["git", "-C", worktree, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if top.returncode != 0 or _paths.canon(top.stdout.strip()) != _paths.canon(worktree):
        return f"{parts[0]!r} under the worktrees root is not a git worktree root"
    if subprocess.run(["git", "-C", worktree, "ls-files", "--error-unmatch", inner], capture_output=True).returncode == 0:
        return f"{raw!r} is tracked; repro and property tests are untracked files only"
    return None


def evaluate(payload: dict) -> int:
    if payload.get("agent_type") not in set(g.cfg_list("guards.repro_paths_agents", ["repro-writer", "pbt-writer"])):
        return 0
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool in ("Bash", "PowerShell"):
        cmd = str(ti.get("command") or "").strip()
        options = set(g.cfg_list("review.repro_runner_options", RUNNER_OPTIONS_DEFAULT))
        why = runner_check(cmd, "run_scoped_tests.py", options)
        if why:
            runner = g.cfg("env.host_tooling_runner", "uv run --no-project")
            return g.deny_json(
                f"[workflow-hook] {cmd[:120]!r}: {why}. This agent runs only `{runner} "
                f"{Path(g.wfconfig.repo_root()) / '.workflow' / 'scripts' / 'run_scoped_tests.py'} <args>` (options "
                f"{' '.join(sorted(options))}; no shell operators). Read logs and source with the read/grep tools. "
                f"(Hook: .workflow/hooks/guards/{HOOK})")
        return 0
    if tool not in ("Write", "Edit"):
        return 0
    why = write_check(str(ti.get("file_path") or ""))
    if why:
        return g.deny_json(f"[workflow-hook] {why} (Hook: .workflow/hooks/guards/{HOOK})")
    return 0


if __name__ == "__main__":
    g.run("repro_paths", evaluate)
