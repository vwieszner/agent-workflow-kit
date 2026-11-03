#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Post-session audit of the story an auto-dev-loop session drove.

`auto_dev_driver.py` snapshots the story worktrees and the story stacks' volumes before each
session and, once the session's AUTO-DEV-PHASE line names the story, audits only that story
(other sessions land and clean up their own stories meanwhile):
  * any outcome but `landed`: the worktree and the story branch exist, and every
    `<stack>_*` volume present before the session is still present;
  * `halted`: no in_use slot-registry row, no running container of the story's stack, and the
    halt's bookkeeping is committed (no uncommitted change to the story's sprint-status row, its
    story-history file or its failure-log entry).
Volume and container checks run only when `config: stack.runtime` is not `none`.

A problem stops the driver. Standalone, it audits one story now:
  uv run --no-project .workflow/scripts/auto_dev_audit.py <story-id> [--outcome halted]
prints one problem per line and exits 1 when there is any.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bookkeeping as bk  # noqa: E402
import wfconfig  # noqa: E402


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def engine() -> str:
    return str(wfconfig.get("stack.engine_cmd", "") or "docker")


def volumes() -> set[str] | None:
    if not wfconfig.stack_enabled():
        return set()
    prefix = str(wfconfig.get("slots.stack_prefix", "story-"))
    try:
        r = _run([engine(), "volume", "ls", "-q", "--filter", f"name={prefix}"])
    except OSError:
        return None
    return set(r.stdout.split()) if r.returncode == 0 else None


def snapshot() -> dict:
    root = wfconfig.worktrees_root()
    tree = sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    return {"worktrees": tree, "volumes": volumes()}


def uncommitted_bookkeeping(repo: Path, story_id: str) -> list[str]:
    """The story's halt bookkeeping still uncommitted on the main checkout."""
    got = _run([sys.executable, str(Path(__file__).resolve().parent / "sprint_status.py"), "get",
                bk.dashed(story_id)], cwd=repo)
    try:
        row = json.loads(got.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [f"sprint-status row for {story_id} unreadable"]
    if row.get("status") != "ok":
        return [f"sprint-status row for {story_id}: {row.get('message')}"]
    key = row["key"]
    left = []
    status = bk.sprint_status_rel()
    base, work = bk.head_text(repo, status), bk.work_text(repo, status)
    if base is not None and work is not None:
        try:
            if bk.replace_line(base, work, bk.row_matcher(key), key) is not None:
                left.append(f"{status.as_posix()} row {key}")
        except bk.StepError as exc:
            left.append(str(exc))
    history = bk.history_rel(key).as_posix()
    if _run(["git", "-C", str(repo), "status", "--porcelain", "--", history]).stdout.strip():
        left.append(history)
    log = bk.failure_log_rel()
    head = bk.head_text(repo, log)
    work = bk.work_text(repo, log) or ""
    if head is None:
        if work:
            left.append(f"{log.as_posix()} (untracked)")
    elif set(bk.entry_texts(work, story_id)) != set(bk.entry_texts(head, story_id)):
        left.append(f"{log.as_posix()} entry {story_id}")
    return left


def facts(repo: Path, story_id: str, outcome: str) -> dict:
    dash = bk.dashed(story_id)
    found = {
        "worktree": wfconfig.worktree_path(dash).is_dir(),
        "branch": _run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                        wfconfig.branch_name(dash)]).returncode == 0,
        "volumes": volumes(),
    }
    if outcome == "halted":
        found["slot_in_use"] = bk.registry_slot(repo, story_id) is not None
        if wfconfig.stack_enabled():
            try:
                ps = _run([engine(), "ps", "-q", "--filter",
                           f"label=com.docker.compose.project={wfconfig.stack_name(dash)}"])
                found["running"] = len(ps.stdout.split()) if ps.returncode == 0 else None
            except OSError:
                found["running"] = None
        else:
            found["running"] = 0
        found["uncommitted"] = uncommitted_bookkeeping(repo, story_id)
    return found


def problems(story_id: str, outcome: str, before: dict | None, found: dict) -> list[str]:
    """What the session left wrong for its story; [] when nothing."""
    if outcome == "landed":
        return []
    dash = bk.dashed(story_id)
    out = []
    if not found["worktree"]:
        out.append(f"worktree {wfconfig.worktree_path(dash).as_posix()} is missing")
    if not found["branch"]:
        out.append(f"branch {wfconfig.branch_name(dash)} is missing")
    stack = wfconfig.stack_name(dash)
    had = {v for v in ((before or {}).get("volumes") or ()) if v.startswith(f"{stack}_")}
    if had:
        if found["volumes"] is None:
            out.append("container volumes could not be listed")
        elif had - found["volumes"]:
            out.append(f"volumes removed: {', '.join(sorted(had - found['volumes']))}")
    if outcome == "halted":
        if found["slot_in_use"]:
            out.append(f"slot-registry row for {dash} is still in_use")
        if found["running"] is None:
            out.append("running containers could not be listed")
        elif found["running"]:
            out.append(f"{found['running']} container(s) of {stack} still running")
        out += [f"uncommitted: {u}" for u in found["uncommitted"]]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="audit a story an auto-dev-loop session drove")
    ap.add_argument("story_id")
    ap.add_argument("--outcome", default="halted", choices=("advanced", "halted", "waiting"))
    args = ap.parse_args(argv)
    repo = wfconfig.repo_root()
    found = problems(args.story_id, args.outcome, None, facts(repo, args.story_id, args.outcome))
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
