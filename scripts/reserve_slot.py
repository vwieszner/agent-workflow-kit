#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Atomic slot reservation for the parallel-dev-wave / auto-dev-loop pipelines.

Holds the `slot-registry` NamedMutex (.workflow/scripts/_named_mutex.py — an OS
file lock) for the whole read-modify-write, so concurrent terminals and agents
never claim the same slot.

This script ONLY touches the slot registry (`config: paths.slot_registry`).
Sprint-status is NOT in scope — the orchestrator edits it with valid status
values via sprint_status.py.

The registry must exist; create it once with
`uv run --no-project .workflow/scripts/slot_registry.py init`.

Usage:
  uv run --no-project .workflow/scripts/reserve_slot.py --story-id 7.5.8

Story ids may be dotted or dashed; the registry, branch, worktree and stack
names use the dashed form.

Output (single JSON line on stdout):
  {"status":"reserved","slot":1,"branch_name":"7-5-8","branch":"story/7-5-8",
   "worktree_path":"/abs/worktrees/7-5-8","stack_project_name":"story-7-5-8",
   "stack_enabled":true,"story_id":"7.5.8"}
  {"status":"already_reserved", ...same keys}   # story already holds a slot
  {"status":"no_free_slot"}                     # slots 1..config: slots.count all in_use
  {"status":"error","message":"..."}            # IO / parse / lock error

`branch_name` is the dashed story id (the Story ID column); `branch` is the git branch.

Exit codes: 0 for reserved / already_reserved / no_free_slot, 1 for error.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
from _named_mutex import NamedMutex  # noqa: E402
import slot_registry as reg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="mutex-protected slot reservation")
    ap.add_argument("--story-id", required=True)
    ap.add_argument("--registry-path", default=None,
                    help="default: config: paths.slot_registry")
    ap.add_argument("--mutex-timeout-ms", type=int, default=60000)
    args = ap.parse_args()
    registry = Path(args.registry_path) if args.registry_path else reg.registry_path()

    try:
        with NamedMutex(reg.MUTEX_NAME, args.mutex_timeout_ms):
            sid = reg.dashed(args.story_id)
            info = {
                "branch_name": sid,
                "branch": wfconfig.branch_name(sid),
                "worktree_path": wfconfig.worktree_path(sid).as_posix(),
                "stack_project_name": wfconfig.stack_name(sid),
                "stack_enabled": wfconfig.stack_enabled(),
                "story_id": args.story_id,
            }

            if not registry.is_file():
                print(json.dumps({"status": "error",
                                  "message": f"Registry file not found: {registry} "
                                             "(create it: uv run --no-project "
                                             ".workflow/scripts/slot_registry.py init)"}))
                return 1
            lines = reg.read_lines(registry)
            rows = reg.parse_rows(lines)

            # First pass: is this story already reserved somewhere?
            for row in rows:
                if row["status"] == "in_use" and row["story_id"] == sid:
                    print(json.dumps({"status": "already_reserved", "slot": row["slot"], **info}))
                    return 0

            # Second pass: claim the first free slot in 1..slots.count.
            allowed = set(reg.slot_numbers())
            reserved = None
            for row in rows:
                if row["status"] == "free" and row["slot"] in allowed:
                    reserved = row["slot"]
                    lines[row["index"]] = reg.in_use_row(
                        reserved, sid, info["branch"], info["worktree_path"],
                        date.today().isoformat())
                    break

            if reserved is None:
                print(json.dumps({"status": "no_free_slot"}))
                return 0

            reg.write_lines(registry, lines)
            print(json.dumps({"status": "reserved", "slot": reserved, **info}))
            return 0
    except (TimeoutError, OSError) as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
