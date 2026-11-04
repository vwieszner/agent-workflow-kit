#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Regenerate a test snapshot in a story stack and copy it back to the story worktree.

Usage
-----
    uv run --no-project .workflow/scripts/snapshot_update.py --project-name <stack> \
        --worktree <worktree-root> [--name <snapshot>] [--diff-lines N] [--timeout S]

The snapshots are `config: design.snapshots`, a list of tables:
    { name = "theme", service = "frontend", test = "web/src/theme/theme.test.js",
      update_cmd = ["npm", "run", "test:unit", "--", "--run", "-u", "src/theme/theme.test.js"],
      container_path = "/app/src/theme/__snapshots__/theme.test.js.snap",
      worktree_path = "web/src/theme/__snapshots__/theme.test.js.snap",
      summary_regex = "^\\s*(?:Snapshots|Tests)  \\S" }
`--name` picks one (default: the only one). `test` and `worktree_path` are worktree-relative.

When to use
-----------
    The story intentionally changes the output a snapshot pins and its test fails only on the
    snapshot, or story-rebase regenerates the snapshot under its generated-files rule. Regenerating
    accepts whatever the code renders now: a mismatch the story did not intend is a failure to
    root-cause, not to regenerate (no test weakening). Report the printed diff line.

What it does
------------
    1. `{compose} -p <stack> exec -T <service> <update_cmd>` from the worktree root. A non-zero exit
       means an assertion other than the snapshot still fails: its tail is printed, nothing is copied.
    2. `{compose} -p <stack> cp <service>:<container_path> <worktree>/<worktree_path>`: a live-sync
       watch syncs host to container only, so without this copy the commit carries the stale snapshot.
    3. Prints the file's state against HEAD (`snapshot current`, or `+A -R` with the changed lines,
       capped at --diff-lines) and the runner's summary lines. Nothing is staged or committed.

Refused (exit 2, nothing run): a stack outside `config: slots.stack_prefix`, the main checkout as the
worktree, a worktree without the snapshot's `test` file. Do not run it while the stack runs a suite.

Exit codes: 0 regenerated (changed or current); 1 the test still fails after the update;
2 refused, compose failure or timeout; 3 SKIPPED (no snapshot configured, or stack.runtime none).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _stack  # noqa: E402
import wfconfig  # noqa: E402

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEFAULT_SUMMARY = r"^\s*(?:Snapshots|Tests)  \S"


def snapshots() -> list[dict]:
    raw = wfconfig.get("design.snapshots", []) or []
    return [s for s in raw if isinstance(s, dict)]


def pick(name: str | None) -> dict | None:
    snaps = snapshots()
    if name:
        return next((s for s in snaps if s.get("name") == name), None)
    return snaps[0] if len(snaps) == 1 else None


def is_main_checkout(worktree: str) -> bool | None:
    """True for the main working tree, False for a linked worktree, None outside git."""
    r = subprocess.run(["git", "-C", worktree, "rev-parse", "--git-dir", "--git-common-dir"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    dirs = r.stdout.splitlines()
    if r.returncode != 0 or len(dirs) != 2:
        return None
    git_dir, common = (os.path.normcase(os.path.abspath(os.path.join(worktree, x))) for x in dirs)
    return git_dir == common


def refusal(project: str, worktree: str, snap: dict) -> str | None:
    prefix = str(wfconfig.get("slots.stack_prefix", "story-"))
    if not project.startswith(prefix):
        return f"stack '{project}' is not a story stack ({prefix}<id>)"
    test = str(snap.get("test", ""))
    if test and not os.path.isfile(os.path.join(worktree, *test.split("/"))):
        return f"no {test} under {worktree} (pass the worktree root)"
    main = is_main_checkout(worktree)
    if main is None:
        return f"{worktree} is not a git working tree"
    if main:
        return "the worktree is the main checkout; pass the story worktree root"
    return None


def changed_lines(diff: str, cap: int) -> list[str]:
    lines = [ln for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    return lines[:cap] + ([f"... {len(lines) - cap} more changed lines"] if len(lines) > cap else [])


def _git(worktree: str, *args: str) -> str:
    return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True, timeout=60).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="regenerate a snapshot in a story stack", allow_abbrev=False)
    ap.add_argument("--project-name", required=True)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--diff-lines", type=int, default=80)
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args(argv)

    if not wfconfig.stack_enabled():
        print("[SNAPSHOT] SKIPPED — stack.runtime is none")
        return 3
    snap = pick(a.name)
    if snap is None:
        names = [s.get("name") for s in snapshots()]
        print(f"[SNAPSHOT] SKIPPED — no snapshot {'named ' + a.name if a.name else 'selected'} "
              f"(config: design.snapshots has {names or 'none'}; pass --name when there are several)")
        return 3
    reason = refusal(a.project_name, a.worktree, snap)
    if reason:
        print(f"[SNAPSHOT] REFUSED — {reason}")
        return 2
    service = str(snap["service"])
    update = [str(x) for x in (snap.get("update_cmd") or [])]
    try:
        r = subprocess.run(_stack.compose_argv(a.project_name, "exec", "-T", service, *update), cwd=a.worktree,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=a.timeout)
    except subprocess.TimeoutExpired:
        print(f"[SNAPSHOT] INFRA-ERROR — the update exceeded {a.timeout}s and was killed")
        return 2
    except FileNotFoundError as exc:
        print(f"[SNAPSHOT] INFRA-ERROR — cannot invoke the compose command: {exc}")
        return 2
    out = _ANSI_RE.sub("", (r.stdout or "") + "\n" + (r.stderr or ""))
    if r.returncode != 0:
        print(f"[SNAPSHOT] FAIL — {snap.get('test') or snap.get('name')} still fails after the update "
              f"(exit {r.returncode}); nothing copied")
        print("\n".join([ln for ln in out.splitlines() if ln.strip()][-30:]))
        return 1
    rel = str(snap["worktree_path"])
    target = os.path.join(a.worktree, *rel.split("/"))
    c = subprocess.run(_stack.compose_argv(a.project_name, "cp", f"{service}:{snap['container_path']}", target),
                       cwd=a.worktree, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120)
    if c.returncode != 0:
        print(f"[SNAPSHOT] INFRA-ERROR — compose cp failed: {((c.stderr or '') + (c.stdout or '')).strip()}")
        return 2
    if not _git(a.worktree, "status", "--porcelain", "--", rel).strip():
        print(f"[SNAPSHOT] snapshot current — {rel} matches HEAD")
    else:
        numstat = _git(a.worktree, "diff", "HEAD", "--numstat", "--", rel).split("\t")
        added, removed = (numstat[0], numstat[1]) if len(numstat) > 2 else ("?", "?")
        print(f"[SNAPSHOT] snapshot regenerated — {rel} +{added} -{removed} vs HEAD (not staged)")
        for ln in changed_lines(_git(a.worktree, "diff", "HEAD", "--unified=0", "--", rel), a.diff_lines):
            print(f"  {ln}")
    summary = re.compile(str(snap.get("summary_regex") or DEFAULT_SUMMARY))
    for ln in out.splitlines():
        if summary.match(ln):
            print(f"  {ln.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
