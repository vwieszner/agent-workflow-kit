#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(Read|Grep|Glob) guard: review agents read the story's tree, not the main checkout.

A review layer or verifier starts in the session's working directory, the main checkout on the base branch; a
grep without a path there judges the code under review against a different branch. For callers whose
`agent_type` is in `config: guards.review_code_paths_agents` (default edge-case-hunter, verification-gap,
acceptance-auditor, findings-verifier) this denies:
  * Grep / Glob with no `path`, or a relative one;
  * Read / Grep / Glob of third-party package source — a path (or Glob pattern) segment in
    `config: review.library_segments` (default node_modules, site-packages): no tree these agents reach holds it
    at the story's locked versions; the deny says how to settle a library premise instead;
  * Read / Grep / Glob of any other path inside the main checkout, except the installed `layered-review`
    skill's `review-prompts/` (`.claude/skills/...` or `.opencode/skills/...`).
Story worktrees, review run dirs and snapshot dirs lie outside the main checkout and pass. Paths are normalised
(`.`/`..` resolved) first. Switch: `config: guards.review_code_paths`. Deny: hookSpecificOutput JSON, exit 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
import _paths  # noqa: E402

HOOK = "review_code_paths.py"
PATH_KEY = {"Read": "file_path", "Grep": "path", "Glob": "path"}
DEFAULT_AGENTS = ["edge-case-hunter", "verification-gap", "acceptance-auditor", "findings-verifier"]
LIBRARY_REASON = (
    "Third-party package source is not readable at the story's locked versions in any tree you can reach: story "
    "worktrees and snapshots hold no installed packages, and the main checkout's copy is a host install that does "
    "not track the lockfiles. Do not search for it. Settle a premise that rests on library behaviour from project "
    "code, tests or the spec read this run, or from the library's documentation at the locked version (read the "
    "version from the worktree's lockfile; with a docs MCP configured, query the id for exactly that version). "
    "Documentation is not source: a verdict resting on it says \"documented behaviour at <version>, source not "
    "read\". When neither settles it, say the library source was not read: a verifier reports UNVERIFIABLE "
    "(library source unreadable), a review layer states the library behaviour as an unverified assumption.")


def _library(text: str, segments: set[str]) -> bool:
    return not segments.isdisjoint(text.replace("\\", "/").lower().split("/"))


def evaluate(payload: dict) -> int:
    if payload.get("agent_type") not in set(g.cfg_list("guards.review_code_paths_agents", DEFAULT_AGENTS)):
        return 0
    tool = payload.get("tool_name")
    if tool not in PATH_KEY:
        return 0
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    raw = str(ti.get(PATH_KEY[tool]) or "").strip()
    tail = f" (Hook: .workflow/hooks/guards/{HOOK})"
    if not raw:
        return g.deny_json(f"[workflow-hook] {tool} needs an explicit absolute path under the story worktree; "
                           "without one it searches the main checkout (a different branch)." + tail)
    if not _paths.is_absolute(raw):
        return g.deny_json(f"[workflow-hook] {tool} path must be absolute (under the story worktree); "
                           f"'{raw}' would resolve against the main checkout." + tail)
    segments = {s.lower() for s in g.cfg_list("review.library_segments", ["node_modules", "site-packages"])}
    if _library(_paths.canon(raw), segments):
        return g.deny_json(f"[workflow-hook] {LIBRARY_REASON} Denied: {raw}" + tail)
    pattern = str(ti.get("pattern") or "") if tool == "Glob" else ""
    if pattern and _library(pattern, segments):
        return g.deny_json(f"[workflow-hook] {LIBRARY_REASON} Denied pattern: {pattern}" + tail)
    main = str(g.wfconfig.repo_root())
    if _paths.under(raw, main) and not _paths.under(raw, str(g.wfconfig.worktrees_root())):
        prompts = [str(Path(main) / d / "skills" / "layered-review" / "review-prompts") for d in (".claude", ".opencode")]
        if not any(_paths.under(raw, p) for p in prompts):
            return g.deny_json(f"[workflow-hook] {tool} of the main checkout is denied: it holds the base branch, "
                               f"not the code under review. Use the story worktree path. Denied: {raw}" + tail)
    return 0


if __name__ == "__main__":
    g.run("review_code_paths", evaluate)
