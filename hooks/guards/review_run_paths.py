#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(Read) guard: the blind review layer reads only its review run's diff files.

Scope: callers whose `agent_type` is in `config: guards.review_run_paths_agents` (default `blind-hunter`).
Allows a Read only of `<root>/.workflow/state/review-runs/<run>/diff.patch` or `.../diff-<k>.patch`, where
`<root>` is the main checkout or a worktree directly under `config: git.worktrees_root`. Every other Read is
denied — blindness to code, spec and docs is the layer's mechanism. A path with a `.` or `..` segment is
denied, so no traversal satisfies the match. Other tools pass. Switch: `config: guards.review_run_paths`.

Deny: hookSpecificOutput JSON on stdout, exit 0. Fails open on an unreadable payload (DESIGN §7).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
import _paths  # noqa: E402

HOOK = "review_run_paths.py"
RUN_FILE = re.compile(r"/\.workflow/state/review-runs/[\w.-]+/diff(-\d+)?\.patch$")


def allowed(path: str) -> bool:
    if not path or not _paths.is_absolute(path) or _paths.has_dot_segment(path):
        return False
    c = _paths.canon(path)
    if not RUN_FILE.search(c):
        return False
    root = c[:c.index("/.workflow/state/review-runs/")]
    if _paths.canon(str(g.wfconfig.repo_root())) == root:
        return True
    wt_root = _paths.canon(str(g.wfconfig.worktrees_root()))
    return root.startswith(wt_root + "/") and "/" not in root[len(wt_root) + 1:]


def evaluate(payload: dict) -> int:
    if payload.get("agent_type") not in set(g.cfg_list("guards.review_run_paths_agents", ["blind-hunter"])):
        return 0
    if payload.get("tool_name") != "Read":
        return 0
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    path = str(ti.get("file_path") or "")
    if allowed(path):
        return 0
    return g.deny_json(
        "[workflow-hook] Read is confined to the review run's diff.patch / diff-<k>.patch "
        f"(<worktree>/.workflow/state/review-runs/round-<n>/). Denied: {path or '(no path)'} "
        f"(Hook: .workflow/hooks/guards/{HOOK})")


if __name__ == "__main__":
    g.run("review_run_paths", evaluate)
