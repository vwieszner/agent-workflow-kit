#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Report which files a story branch and its base BOTH touched since they forked.

The pre-merge "what might conflict?" check, as one script (shell process substitution is not portable to every
agent shell, and a failed `comm <(...) <(...)` prints an empty overlap that reads as "safe").

OVERLAP lists the files changed on both sides. CONFLICTS lists the subset whose three-way merge keeps conflict
markers, from the three-tree form `git merge-tree <merge-base> <base> <branch>` (git's trivial merge: no rename
detection, so a prediction -- the rebase decides). Read every OVERLAP diff before merging; resolve every
CONFLICTS file.

Usage
-----
    # files <branch> and the base branch (config: git.base_branch) both touched since they forked
    uv run --no-project .workflow/scripts/branch_overlap.py story/7-945

    # against another base, in a worktree
    uv run --no-project .workflow/scripts/branch_overlap.py story/7-945 --base main --repo <worktree>

    # also intersect with the repo's UNCOMMITTED files
    uv run --no-project .workflow/scripts/branch_overlap.py story/7-945 --dirty

    # for scripting: exit 1 when there is any overlap
    uv run --no-project .workflow/scripts/branch_overlap.py story/7-945 --exit-code

When to use
-----------
    Before every rebase dispatch (the story-rebase subagent's per-file rules come from it), and before
    a merge when the base branch has moved under the branch.

Exit codes
----------
    0  ran successfully (overlap may or may not be empty)
    1  ran successfully and found overlap  (only with --exit-code)
    2  git error / bad branch name
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

HEADER = re.compile(r"^(changed in both|added in both|removed in local|removed in remote|"
                    r"added in local|added in remote|removed in both|merged)$")
ENTRY = re.compile(r"^  (?:base|our|their|result)\s+\d{6} [0-9a-f]{40} (.+)$")
BOTH = ("changed in both", "added in both")


def git(repo: str, *args: str) -> str:
    """Run a git command in `repo`, returning stdout. Exits 2 on git failure.
    UTF-8 decoding: merge-tree output carries file content, which the locale codec
    (cp1252) cannot decode."""
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"git {' '.join(args)} failed (exit {proc.returncode}):\n{proc.stderr.strip()}\n"
        )
        sys.exit(2)
    return proc.stdout


def changed_files(repo: str, rev_range: str) -> set[str]:
    """File names changed over a revision range. -z keeps paths with spaces or
    non-ASCII characters intact (git otherwise quotes and escapes them)."""
    out = git(repo, "diff", "--name-only", "-z", rev_range)
    return {p for p in out.split("\0") if p}


def dirty_files(repo: str) -> set[str]:
    """Uncommitted paths (staged, unstaged, untracked). `--porcelain -z` emits
    'XY <path>\\0'; renames add a second NUL-terminated path, which we keep too."""
    out = git(repo, "status", "--porcelain", "-z")
    fields = [f for f in out.split("\0") if f]
    paths: set[str] = set()
    for field in fields:
        # 'XY path' — status codes are the first two columns, then a space.
        paths.add(field[3:] if len(field) > 3 and field[2] == " " else field)
    return paths


def predicted_conflicts(merge_tree_out: str) -> set[str]:
    """Paths of the `changed in both` / `added in both` entries whose merged content keeps a
    `+<<<<<<<` conflict-marker line. Other entry kinds are not scanned: an added file's content is
    printed as `+` lines and may contain a literal marker."""
    conflicts: set[str] = set()
    header = path = None
    for line in merge_tree_out.splitlines():
        if HEADER.match(line):
            header, path = line, None
        elif path is None and (m := ENTRY.match(line)):
            path = m.group(1)
        elif header in BOTH and path and line.startswith("+<<<<<<<"):
            conflicts.add(path)
    return conflicts


def section(title: str, files: set[str], *, limit: int | None = None) -> None:
    print(f"=== {title} ({len(files)}) ===")
    listing = sorted(files)
    shown = listing if limit is None else listing[:limit]
    for f in shown:
        print(f"  {f}")
    if limit is not None and len(listing) > limit:
        print(f"  ... and {len(listing) - limit} more")
    if not listing:
        print("  (none)")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Files a branch and its base both touched since they forked.",
    )
    ap.add_argument("branch", help="the story branch, e.g. story/7-945")
    ap.add_argument("--base", default=None, help="base branch (default: config git.base_branch)")
    ap.add_argument("--repo", default=".", help="repo or worktree to run in (default: cwd)")
    ap.add_argument(
        "--dirty",
        action="store_true",
        help="also intersect the branch's files with the repo's uncommitted files",
    )
    ap.add_argument(
        "--exit-code",
        action="store_true",
        help="exit 1 when the overlap is non-empty (for scripting)",
    )
    ap.add_argument("--limit", type=int, default=40, help="max files per list (default: 40)")
    args = ap.parse_args()
    if args.base is None:
        args.base = str(wfconfig.get("git.base_branch", "development"))

    base_sha = git(args.repo, "merge-base", args.base, args.branch).strip()
    if not base_sha:
        sys.stderr.write(f"no merge base between {args.base} and {args.branch}\n")
        return 2

    branch_files = changed_files(args.repo, f"{base_sha}..{args.branch}")
    base_files = changed_files(args.repo, f"{base_sha}..{args.base}")
    overlap = branch_files & base_files

    print(f"fork point: {base_sha[:8]}  ({args.base} <-> {args.branch})\n")
    section(f"{args.branch} changed", branch_files, limit=args.limit)
    section(f"{args.base} advanced by", base_files, limit=args.limit)
    section("OVERLAP -- both sides touched (review before merging)", overlap, limit=None)
    conflicts = predicted_conflicts(git(args.repo, "merge-tree", base_sha, args.base, args.branch))
    section("CONFLICTS -- git's three-way merge leaves markers (resolve before merging)",
            conflicts, limit=None)

    if args.dirty:
        dirty = dirty_files(args.repo)
        section(
            "ALSO DIRTY in this worktree (uncommitted)",
            branch_files & dirty,
            limit=None,
        )

    print(
        "CONFLICTS is a prediction (no rename detection); the other OVERLAP files\n"
        "merge clean but deserve a diff read."
    )
    return 1 if (args.exit_code and overlap) else 0


if __name__ == "__main__":
    sys.exit(main())
