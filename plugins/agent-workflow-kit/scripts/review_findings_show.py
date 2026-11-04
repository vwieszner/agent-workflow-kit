#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Print code-review findings by id from a story's findings file.

Reads the findings file `review_findings_render.py` writes
(`<worktree>/<config: paths.specs_dir>/<story>-code-review-findings-<date>.md`). A finding for triage is a
`#### R<round>-<n> — <title>` section of `- ` field lines; the renderer writes every value on one line
(` ⏎ ` marks a break), so a field is one line. A section ends at the next `##`, `###` or `####` heading, so
the `### Dismissed as false positive` block, the raw layer reports and an appended `## Triage decisions`
section never run into it. A REFUTED finding has no section: its `- <id> | title | location | evidence` row
in the Dismissed block is printed instead. An id rendered in several rounds (a halted round's finding
carried into a later round) prints its latest rendering.

Usage
-----
    uv run --no-project <plugin-root>/scripts/review_findings_show.py <findings-file> R1-24 R1-37 [--fields claim,verification,bar] [--width 900]

    --fields    comma list of sources, carried, location, delta, kind, claim, evidence, verification, votes,
                repro, bar (default: every field line, in file order)
    --width     cut each printed line to N characters (default: no cut)

Exit 0 when every id asked for was found; 1 when one was not (the found ones still print);
2 on bad arguments or a missing findings file.

When to use: triage (walking the owner through one finding at a time), story-finalize reading an
approved finding's section, findings-evaluator reading one finding. Never read the whole file.

Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# Field-line prefixes, lower-cased, as review_findings_render._finding_block writes them.
FIELDS = {
    "sources": "- **sources:**",
    "carried": "- **carried from:**",
    "location": "- **location:**",
    "delta": "- **delta:**",
    "kind": "- **kind / recommended:**",
    "claim": "- **claim:**",
    "evidence": "- **layer evidence [",
    "verification": "- **verification:**",
    "votes": "- **votes:**",
    "repro": "- **repro:**",
    "bar": "- bar impact:",
}
HEADING = re.compile(r"^#{2,4} ")
FINDING = re.compile(r"^#### (R\d+-\d+) — (.*)$")
DISMISSED = "### Dismissed as false positive"


def parse(text: str) -> tuple[dict, dict]:
    """(finding sections by id, dismissed rows by id); an id rendered twice keeps its latest rendering only."""
    sections, dismissed = {}, {}
    current, in_dismissed = None, False
    for line in text.splitlines():
        if HEADING.match(line):
            in_dismissed = line.startswith(DISMISSED)
            m = FINDING.match(line)
            current = None
            if m:
                dismissed.pop(m.group(1), None)
                current = sections[m.group(1)] = {"title": m.group(2), "lines": []}
            continue
        if not line.startswith("- "):
            continue
        if current is not None:
            current["lines"].append(line)
        elif in_dismissed:
            fid = line[2:].split(" | ", 1)[0].strip()
            sections.pop(fid, None)
            dismissed[fid] = line
    return sections, dismissed


def pick(lines: list[str], names: list[str]) -> list[str]:
    if not names:
        return lines
    prefixes = tuple(FIELDS[n] for n in names)
    return [line for line in lines if line.lower().startswith(prefixes)]


def cut(line: str, width: int) -> str:
    return line if not width or len(line) <= width else line[:width] + " …"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("path", help="the story's *-code-review-findings-*.md file")
    ap.add_argument("ids", nargs="+", help="finding ids, e.g. R1-24 R2-3")
    ap.add_argument("--fields", default="", help="comma list of: " + ", ".join(FIELDS))
    ap.add_argument("--width", type=int, default=0, help="cut each printed line to N characters")
    a = ap.parse_args(argv)
    names = [n.strip() for n in a.fields.split(",") if n.strip()]
    unknown = [n for n in names if n not in FIELDS]
    if unknown:
        ap.error(f"unknown field(s) {', '.join(unknown)}; choose from {', '.join(FIELDS)}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not os.path.isfile(a.path):
        print(f"review-findings-show: no findings file at {a.path!r}", file=sys.stderr)
        return 2
    with open(a.path, encoding="utf-8", errors="replace") as fh:
        sections, dismissed = parse(fh.read())

    missing = []
    for fid in a.ids:
        if fid in sections:
            s = sections[fid]
            print(f"== {fid} — {s['title']}")
            for line in pick(s["lines"], names):
                print(cut(line, a.width))
        elif fid in dismissed:
            print(f"== {fid} (REFUTED)")
            print(cut(dismissed[fid], a.width))
        else:
            missing.append(fid)
    if missing:
        print(f"review-findings-show: not in {a.path}: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
