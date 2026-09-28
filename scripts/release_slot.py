#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Atomic slot release for /land-story's success path AND auto-dev-loop's
halt-and-release path.

Same `slot-registry` NamedMutex as reserve_slot.py, opposite direction — flips
the story's row from in_use back to free and clears its cells.

Does NOT delete the git branch (the branch is the durable artifact preserving a
failed story's WIP and a merged story's history). This script ONLY touches the
slot registry (`config: paths.slot_registry`); sprint-status stays with the
orchestrator.

Usage:
  uv run --no-project .workflow/scripts/release_slot.py --story-id 7.5.8

Output (single JSON line on stdout):
  {"status":"released","slot":1,"branch_name":"7-5-8","branch":"story/7-5-8","story_id":"7.5.8"}
  {"status":"not_found","story_id":"7.5.8","branch_name":"7-5-8","branch":"story/7-5-8"}  # already free
  (`branch_name` is the dashed id, as reserve_slot.py emits it; `branch` is the git branch.)
  {"status":"error","message":"..."}

Exit codes: 0 for released / not_found, 1 for error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _named_mutex import NamedMutex  # noqa: E402
import slot_registry as reg  # noqa: E402
import wfconfig  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="mutex-protected slot release")
    ap.add_argument("--story-id", required=True)
    ap.add_argument("--registry-path", default=None,
                    help="default: config: paths.slot_registry")
    ap.add_argument("--mutex-timeout-ms", type=int, default=60000)
    args = ap.parse_args()
    registry = Path(args.registry_path) if args.registry_path else reg.registry_path()

    try:
        with NamedMutex(reg.MUTEX_NAME, args.mutex_timeout_ms):
            sid = reg.dashed(args.story_id)

            if not registry.is_file():
                print(json.dumps({"status": "error",
                                  "message": f"Registry file not found: {registry}"}))
                return 1
            lines = reg.read_lines(registry)

            released = None
            for row in reg.parse_rows(lines):
                if row["status"] == "in_use" and row["story_id"] == sid:
                    released = row["slot"]
                    lines[row["index"]] = reg.free_row(released)
                    break

            branch = wfconfig.branch_name(sid)
            if released is None:
                print(json.dumps({"status": "not_found", "story_id": args.story_id,
                                  "branch_name": sid, "branch": branch}))
                return 0

            reg.write_lines(registry, lines)
            print(json.dumps({"status": "released", "slot": released, "branch_name": sid,
                              "branch": branch, "story_id": args.story_id}))
            return 0
    except (TimeoutError, OSError) as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
