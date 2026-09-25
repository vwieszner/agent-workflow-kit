#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Append a durable phase fact to a story's append-only record file.

The record file is the only place a human approval or a test verdict survives a
context clear: both are conversational acts no hook can capture. This is its writer;
`story_ledger.py` is its reader.

Usage
-----
    uv run --no-project .workflow/scripts/story_record.py append <story-id> "<fact>"
    uv run --no-project .workflow/scripts/story_record.py append 7-2 "checkpoint-1 spec approved"
    uv run --no-project .workflow/scripts/story_record.py check  <story-id> <fact-key>

`append` resolves the story's worktree from the slot registry (`config: paths.slot_registry`),
writes `<worktree>/<config: paths.specs_dir>/<story-id>-record.md` (main checkout when the
story holds no slot), creates the file with its heading if absent, prepends today's date,
and refuses an exact duplicate.

Canonical fact lines (the ledger's verdict rules match these):
    checkpoint-1 spec approved
    phase-2 tests GREEN — <labels> (<N> tests)       | phase-2 tests RED — ...
    phase-3 tests GREEN — <labels> (<N> tests)       | phase-3 tests RED — ...
    checkpoint-3 merge approved
    NEXT: <human-authored next step>                 (overrides the derived next step)

Recognised fact keys for `check` (the five the pipeline hands off on):
    checkpoint-1 | phase-2 | triage | phase-3 | checkpoint-3

Exit codes:
    append: 0 appended or already present; 2 empty fact.
    check:  0 a POSITIVE record exists; 1 missing (or only a negative verdict); 2 unknown key.

When to use: at every pipeline phase boundary, immediately after the fact becomes
true (spec approved, scoped suite green, merge approved). Never batch these; an
unwritten fact is lost at the next clear.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Single source of truth for id variants, worktree resolution and the positive
# verdict rules — the writer and the reader must not drift.
from story_ledger import (  # noqa: E402
    handoff_state,
    read_record,
    repo,
    resolve_roots,
    variants,
)

HEADING = "# Story {sid} — decision & evidence record"

# fact-key -> the handoff_state row it satisfies
FACT_ROWS = {
    "checkpoint-1": "spec approved",
    "phase-2": "phase-2 tests",
    "triage": "triage recorded",
    "phase-3": "phase-3 tests",
    "checkpoint-3": "merge approved",
}


def record_path(story_id: str) -> str:
    vs = variants(story_id)
    arts, _branch = resolve_roots(vs)
    return os.path.join(arts, f"{vs[0]}-record.md")


def cmd_append(story_id: str, fact: str) -> int:
    fact = fact.strip().lstrip("-").strip()
    if not fact:
        print("story-record: refusing to append an empty fact", file=sys.stderr)
        return 2
    path = record_path(story_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = f"- {date.today().isoformat()} {fact}"

    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    else:
        existing = HEADING.format(sid=story_id) + "\n\n"

    if line in existing.splitlines():
        print(f"story-record: already present, not duplicated\n  {path}\n  {line}")
        return 0

    if not existing.endswith("\n"):
        existing += "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(existing + line + "\n")

    try:
        shown = os.path.relpath(path, repo())
    except ValueError:
        shown = path
    print(f"story-record: appended\n  {shown}\n  {line}")
    return 0


def cmd_check(story_id: str, fact_key: str) -> int:
    row = FACT_ROWS.get(fact_key)
    if row is None:
        print(f"story-record: unknown fact key '{fact_key}'; "
              f"expected one of {', '.join(FACT_ROWS)}", file=sys.stderr)
        return 2
    vs = variants(story_id)
    arts, _branch = resolve_roots(vs)
    _path, lines = read_record(vs, arts)
    for label, ok, detail in handoff_state(vs, lines, arts):
        if label == row:
            if ok:
                print(f"PRESENT  {row}: {detail}")
                return 0
            print(f"MISSING  {row}", file=sys.stderr)
            return 1
    print(f"MISSING  {row}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("append", help="append a dated fact line to the record file")
    a.add_argument("story_id")
    a.add_argument("fact", help='e.g. "checkpoint-1 spec approved"')

    c = sub.add_parser("check", help="exit 0 if the fact is positively recorded")
    c.add_argument("story_id")
    c.add_argument("fact_key", choices=sorted(FACT_ROWS))

    args = ap.parse_args(argv)
    if args.cmd == "append":
        return cmd_append(args.story_id, args.fact)
    return cmd_check(args.story_id, args.fact_key)


if __name__ == "__main__":
    sys.exit(main())
