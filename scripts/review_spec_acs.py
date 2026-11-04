#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""List the AC ids a story spec declares, for the review workflow's ui-verifier dispatch.

The workflow script has no file access, so `review_stage.py` runs this on the spec and passes the
list as `spec_acs`. The workflow dispatches the ids to the ui-verifier and names each one it mapped to no state
(`ui_unmapped_acs`).

An AC heading declares an id:
  * a markdown heading of level 2 to 6 that opens with the id: `## AC1`, `### AC2: Title`, `#### AC3b — Title`,
    `##### AC4.1: Title`;
  * a line that opens with the id in bold and then a separator: `**AC5 — Title.**`, `**AC6:** text`, `**AC7**`,
    `**AC7.** text`.
An id is `AC<n>` with an optional letter or dotted number (`AC3b`, `AC4.1`); one heading may list several
(`AC6/AC7`, `AC8, AC9 and AC10`). A prose mention, a list item, a table row, a quote, a heading with no number
(`### ACs`) and a bold note that goes on in prose (`**AC7 verified**`) declare none, and nothing inside a fenced
code block does.

The ids are read from the spec's `## Acceptance Criteria` section when it holds an id (the section ends at the
next heading of level 1 or 2 that is not itself an AC heading), else from the whole file. Each id is listed once,
in order of first appearance. A spec whose ACs are not headed in one of these forms lists none, and the workflow
then dispatches no AC.

Prints one JSON line, {"spec_acs": ["AC1", ...]}. Exits 1, printing nothing on stdout, when the file cannot be read.

Usage
-----
    uv run --no-project .workflow/scripts/review_spec_acs.py --spec <spec file>

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

ID = r"AC\d+(?:\.\d+)*[a-z]?(?![0-9A-Za-z])"
ID_LIST = rf"{ID}(?:\s*(?:,|/|&|\band\b)\s*{ID})*"
HEADING = re.compile(rf"^ {{0,3}}#{{2,6}}[ \t]+(?P<ids>{ID_LIST})")
BOLD = re.compile(rf"^ {{0,3}}\*\*(?P<ids>{ID_LIST})(?:\.?\*\*|\s*[:—–]|\s+-\s)")
TOP_HEADING = re.compile(r"^ {0,3}#{1,2}[ \t]+(?P<text>.*)$")
ACCEPTANCE = re.compile(r"^acceptance criteria\b", re.IGNORECASE)
FENCE = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})(?P<rest>.*)$")


def heading_ids(line: str) -> list[str]:
    """The ids `line` declares as an AC heading; [] for any other line."""
    m = HEADING.match(line) or BOLD.match(line)
    return re.findall(ID, m.group("ids")) if m else []


def spec_ac_ids(text: str) -> list[str]:
    """The AC ids `text` declares (see the module docstring), each once, in order of first appearance."""
    inside: list[str] = []
    anywhere: list[str] = []
    fence = ""
    in_section = False
    for line in text.splitlines():
        m = FENCE.match(line)
        if fence:
            if m and m.group("mark")[0] == fence[0] and len(m.group("mark")) >= len(fence) and not m.group("rest").strip():
                fence = ""
            continue
        if m:
            fence = m.group("mark")
            continue
        ids = heading_ids(line)
        top = TOP_HEADING.match(line)
        if top:
            if ACCEPTANCE.match(top.group("text").strip()):
                in_section = True
            elif not ids:
                in_section = False
        anywhere += ids
        if in_section:
            inside += ids
    return list(dict.fromkeys(inside or anywhere))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--spec", required=True, help="the story spec file")
    a = ap.parse_args(argv)
    try:
        with open(a.spec, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"review-spec-acs: cannot read {a.spec}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"spec_acs": spec_ac_ids(text)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
