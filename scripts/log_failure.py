#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Atomic append to the autonomous loop's permanent-failure log
(`config: paths.auto_dev_failure_log`).

Serialised by its own named mutex (`workflow-failure-log`, separate from the
slot-registry mutex) so a failing terminal never blocks another terminal that is
mid-slot-reservation. Creates the file with its header on first use.

Usage:
  uv run --no-project .workflow/scripts/log_failure.py \
    --story-id 7-5 --branch-name 7-5 \
    --failure-step story-impl \
    --failure-summary "Tests failed after 5 batch-fix rounds." \
    [--wip-commit abc1234] [--last-test-output "...tail 30..."] \
    [--log-path <file>] [--mutex-timeout-ms 30000]

`--branch-name` is the branch suffix: the logged branch is
`config: git.branch_prefix` + branch-name.

Output (single JSON line on stdout):
  {"status":"logged","file":"...","entry_id":"<story_id>-<timestamp>"}   exit 0
  {"status":"error","message":"..."}                                     exit 1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
from _named_mutex import NamedMutex  # noqa: E402

MUTEX_NAME = "workflow-failure-log"
DEFAULT_LOG = "docs/implementation-artifacts/auto-dev-failures.md"

HEADER = (
    "# Auto-Dev Loop — Permanent Failures\n\n"
    "Each entry is a story the autonomous loop could not complete.\n"
    "The branch is preserved; worktree/stack/slot have been released.\n"
    "Human inspection required before resume.\n"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="append a failure record to the auto-dev failure log")
    ap.add_argument("--story-id", required=True)
    ap.add_argument("--branch-name", required=True)
    ap.add_argument("--failure-step", required=True)
    ap.add_argument("--failure-summary", required=True)
    ap.add_argument("--wip-commit", default="")
    ap.add_argument("--last-test-output", default="")
    ap.add_argument("--log-path", default=None,
                    help="default: config paths.auto_dev_failure_log")
    ap.add_argument("--mutex-timeout-ms", type=int, default=30000)
    args = ap.parse_args(argv)
    log_path = (Path(args.log_path) if args.log_path
                else wfconfig.path("paths.auto_dev_failure_log", DEFAULT_LOG))
    branch = wfconfig.branch_name(args.branch_name)
    worktree = (wfconfig.worktrees_root() / args.branch_name).as_posix()

    try:
        with NamedMutex(MUTEX_NAME, args.mutex_timeout_ms):
            timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            entry_id = f"{args.story_id}-{timestamp}"

            lines = [
                "",
                f"## {args.story_id} — {timestamp} — {args.failure_step}",
                "",
                f"- branch: {branch} (PRESERVED — checkout to inspect)",
            ]
            if args.wip_commit:
                lines.append(f"- wip_commit: {args.wip_commit}")
            lines += [
                f"- failure_step: {args.failure_step}",
                f"- failure_summary: {args.failure_summary}",
            ]
            if args.last_test_output:
                lines += ["", "Last test output (tail 30):", "```", args.last_test_output, "```"]
            lines += [
                "",
                "Worktree removed, stack down, slot released. To resume manually:",
                "```",
                f"git worktree add {worktree} {branch}",
                "```",
                "",
            ]
            entry = "\n".join(lines) + "\n"

            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not log_path.is_file():
                log_path.write_text(HEADER, encoding="utf-8")
            with log_path.open("a", encoding="utf-8") as f:
                f.write(entry)

        print(json.dumps({"status": "logged", "file": str(log_path), "entry_id": entry_id}))
        return 0
    except (TimeoutError, OSError) as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
