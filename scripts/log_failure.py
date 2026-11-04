#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Atomic append to the autonomous loop's permanent-failure log
(`config: paths.auto_dev_failure_log`).

Serialised by its own named mutex (`LOG_MUTEX`, separate from the slot-registry mutex) so a
failing terminal never blocks another terminal that is mid-slot-reservation; `resume_halted.py`
holds the same mutex. Creates the file with its header on first use.

Usage:
  uv run --no-project .workflow/scripts/log_failure.py \
    --story-id 7-95 --branch-name 7-95 \
    --failure-step story-impl \
    --failure-summary "Tests failed after 5 batch-fix rounds." \
    [--wip-commit abc1234] [--last-test-output "...tail 30..."] \
    [--asked-of owner|external|both] [--external-ticket KEY] \
    [--log-path <file>] [--mutex-timeout-ms 30000]

`--asked-of` records whose answers the halt waits on: `owner` (the project owner), `external` (the
external design owner, `config: design.owner_role`, whose questions sit on a tracker ticket) or `both`.
`external` and `both` need `--external-ticket`, and any non-empty `--external-ticket` must be exactly one
ticket key (`config: paths.design_ticket_regex`) whatever `--asked-of` says: an entry whose ticket names
no key could never be resumed. Each is refused (exit 2) before anything is written.

`--branch-name` is the branch suffix: the logged branch is `config: git.branch_prefix` +
branch-name. `--branch-name none` logs a question that has no story branch (a milestone-map
refresh question): the entry then carries no Kept/Resume lines.

A line break in `--story-id`, `--branch-name`, `--failure-step`, `--wip-commit` or `--external-ticket` would split
the entry's heading or end its leading field group; each is refused (exit 2, naming the flag)
before anything is written. A multi-line `--failure-summary` has `\\r\\n` and bare `\\r` turned
into line breaks and continues on lines indented two spaces, so a blank line or a `- key:` line
inside it neither ends the field group nor sets a field.

Entry format:
  ## <story_id> — <YYYY-MM-DDTHH:MM:SS> — <failure_step>

  - branch: <prefix><branch> (PRESERVED — checkout to inspect)
  - wip_commit: <hash>                (only when given)
  - failure_step: <step>
  - asked_of: <owner|external|both>   (only when given)
  - external_ticket: <key>            (only when given)
  - failure_summary: <first line>
    <further lines, indented>

A story-branch entry ends with the Kept line and the resume command
(`resume_halted.py <story_id> --ruling '<the owner's ruling>'`).

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
import _design  # noqa: E402
import wfconfig  # noqa: E402
from _named_mutex import NamedMutex  # noqa: E402

LOG_MUTEX = "workflow-failure-log"
MUTEX_TIMEOUT_MS = 30000
DEFAULT_LOG = "docs/implementation-artifacts/auto-dev-failures.md"
ASKED_OF = ("owner", "external", "both")


def ticket_refusal(asked_of: str, ticket: str) -> str:
    """Why an asked-of / ticket pair is refused, or ''. Shared with halt_release.py."""
    if (asked_of in ("external", "both") or ticket) and not _design.is_ticket_key(ticket):
        return ("--external-ticket must be one ticket key (config: paths.design_ticket_regex) when given; "
                "--asked-of external or both needs one")
    return ""

HEADER = (
    "# Auto-Dev Loop — Permanent Failures\n\n"
    "Each entry is a story the autonomous loop could not complete.\n"
    "Its branch, worktree and stack data are kept and its containers stopped; only the slot is released.\n"
    "Human inspection required before resume.\n"
)


def log_path_default() -> Path:
    return wfconfig.path("paths.auto_dev_failure_log", DEFAULT_LOG)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="append a failure record to the auto-dev failure log")
    ap.add_argument("--story-id", required=True)
    ap.add_argument("--branch-name", required=True)
    ap.add_argument("--failure-step", required=True)
    ap.add_argument("--failure-summary", required=True)
    ap.add_argument("--wip-commit", default="")
    ap.add_argument("--last-test-output", default="")
    ap.add_argument("--asked-of", choices=ASKED_OF, default="", help="whose answers the halt waits on")
    ap.add_argument("--external-ticket", default="", help="the story's design-owner question ticket")
    ap.add_argument("--log-path", default=None, help="default: config paths.auto_dev_failure_log")
    ap.add_argument("--mutex-timeout-ms", type=int, default=MUTEX_TIMEOUT_MS)
    args = ap.parse_args(argv)
    for flag, value in (("--story-id", args.story_id), ("--branch-name", args.branch_name),
                        ("--failure-step", args.failure_step), ("--wip-commit", args.wip_commit),
                        ("--external-ticket", args.external_ticket)):
        if "".join(value.splitlines()) != value:
            ap.error(f"{flag} must be one line")
    refusal = ticket_refusal(args.asked_of, args.external_ticket)
    if refusal:
        ap.error(refusal)
    log_path = Path(args.log_path) if args.log_path else log_path_default()
    has_branch = args.branch_name != "none"
    branch = wfconfig.branch_name(args.branch_name)
    worktree = (wfconfig.worktrees_root() / args.branch_name).as_posix()
    stack = wfconfig.stack_name(args.branch_name)

    try:
        with NamedMutex(LOG_MUTEX, args.mutex_timeout_ms):
            timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            entry_id = f"{args.story_id}-{timestamp}"
            lines = [
                "",
                f"## {args.story_id} — {timestamp} — {args.failure_step}",
                "",
                f"- branch: {branch} (PRESERVED — checkout to inspect)" if has_branch
                else "- branch: none (no story branch; awaiting the owner's ruling)",
            ]
            if args.wip_commit:
                lines.append(f"- wip_commit: {args.wip_commit}")
            lines.append(f"- failure_step: {args.failure_step}")
            if args.asked_of:
                lines.append(f"- asked_of: {args.asked_of}")
            if args.external_ticket:
                lines.append(f"- external_ticket: {args.external_ticket}")
            first, *rest = args.failure_summary.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            lines.append(f"- failure_summary: {first}")
            lines += [f"  {line}" for line in rest]
            if args.last_test_output:
                lines += ["", "Last test output (tail 30):", "```", args.last_test_output, "```"]
            if has_branch:
                lines += [
                    "",
                    f"Kept: worktree `{worktree}`, branch `{branch}`, stack `{stack}` (containers stopped, "
                    "logs and data volumes kept). Slot released.",
                    "Resume: once the halt's questions are answered (the design owner's part when the "
                    "ticket's tracker row reads done), the owner runs "
                    f"`uv run --no-project .workflow/scripts/resume_halted.py {args.story_id} "
                    "--ruling '<the ruling>'` (`--no-ruling` when the halt asked only the design owner); it archives this entry with the ruling, removes it and sets the "
                    "row `ready-for-dev`; the loop can then pick the story again and reuses the worktree. "
                    "Discard (deletes worktree, stack data and branch):",
                    "```",
                    f"uv run --no-project .workflow/scripts/cleanup_story_stack.py {args.branch_name}",
                    "```",
                ]
            lines.append("")
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
