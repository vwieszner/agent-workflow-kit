#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard on the Bash tool (OpenCode: `bash`). Switch: `config: guards.bash_command`.

Rules (exit 2 + stderr on any match):
  1. git push: any force push; a push to a protected branch
     (`config: guards.protected_branches`, default [git.main_branch, git.base_branch,
     "master"]); a generic `git push` without an explicit `<remote> <git.branch_prefix>...`
     target.
  2. git commit --no-verify / --no-gpg-sign.
  2a. Destructive commands, always: git reset --hard, git filter-branch,
      git update-ref -d, Format-Volume, Clear-Disk.
  2b. Recursive delete (rm -r/-rf/-fr/--recursive) whose target is not strictly inside
      an allowed root (`config: guards.recursive_delete_allowed`; default: the repo root,
      the worktrees root, `claude*`/`workflow*` dirs of the system temp dir). `/`, drive
      roots, `~`/`$HOME` and relative targets escaping via `..` are caught; other
      variable-based targets (`$dir`) are trusted.
  3. Host package installs (`config: guards.block_host_installs`): pip install,
     python -m pip install, uv pip install, uv sync, uv add/remove without --no-sync.
     Segments that `docker exec` / `docker compose exec` into a container are exempt.
  4. Unquoted backslash paths (`X:\\...` or `seg\\seg`): bash strips unquoted
     backslashes, so the command does not run as written.
  4a. Bare host `python`/`python3` when `config: env.app_runs_in_container` is true
      (default). Allowed: container segments, `uv run ...`, `config: env.project_python`,
      and `.workflow/` kit scripts.
  5. Host-forbidden commands (`config: guards.host_forbidden`): a regex list matched
     against the start of every host command segment. Newlines do not split segments, so
     a heredoc body fed to a container is never flagged.
  6. Project rules (`config: guards.command_deny`): [{pattern, reason}].

Remedy for a legitimate exception: the owner runs the command from their own shell.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

HOOK = "bash_command.py"

# Drive-letter prefix (`C:\`) or two path segments joined by a backslash (`seg\seg`).
# `\;` (find -exec), `\b`/`\t` escapes lack a path char on both sides and do not match.
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\|[\w.\-]\\[\w.\-]")


def evaluate(payload: dict) -> int:
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str) or not cmd:
        return 0

    violations = g.git_violations(cmd)
    violations += g.destructive_violations(cmd)
    violations += g.recursive_delete_violations(cmd, str(payload.get("cwd") or os.getcwd()))
    violations += g.host_install_violations(cmd)
    if _WIN_PATH_RE.search(g.unquoted_residue(cmd)):
        violations.append(
            "[bash-winpath] unquoted backslash path. Bash strips unquoted backslashes "
            "('dir\\sub\\x.cmd' becomes 'dirsubx.cmd'), so the command will not run as "
            "written. Quote the path, use forward slashes, or (Windows) use the PowerShell tool."
        )
    violations += g.host_python_violations(cmd)
    violations += g.host_forbidden_violations(cmd)
    violations += g.command_deny_violations(cmd)

    if violations:
        return g.block(
            "Bash command violates one or more discipline rules.", violations, HOOK,
            "Rewrite the command and try again. A legitimate exception the owner authorized "
            "is run by the owner from their own shell.",
        )
    return 0


if __name__ == "__main__":
    g.run("bash_command", evaluate)
