#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Print the findings a review round carries from its story's halted review rounds.

A review re-run triages the findings of every round that halted before triage. Round `<m>` carries
the `### Findings for triage` blocks of each `## Review round <n>` section of the story's findings
files with `t < n < m`, where `t` is the highest round below `m` that `review_ledger.round_triaged`
reports triaged (0 when none is). The files are read in the order given and joined by a newline, as
`review_ledger.findings_text_of` joins them. CONFIRMED, UNVERIFIABLE and UNVERIFIED blocks are carried; a REFUTED row
is not. An id counts once, from its latest rendering:
  * a block in a later halted round replaces the earlier block (it was verified on a newer tree);
  * a REFUTED row in a later halted round drops the id;
  * an id that a later halted round's block names as a `carried:<id>` source is represented by that
    block and is not carried again;
  * an id that joined a group a later halted round REFUTED is dropped too: a REFUTED row renders no
    Sources, so the members are read from that round's Summary `joined round <n> findings` list
    (`<id> → <group id>`), which `review_findings_render.py` writes.

Output: one line of JSON, an array of `{id, round, title, location, kind, recommended, claim,
verdict, bar_impact}` ordered by round, then by id number; `round` is the section the block
was read from. `[]` when no file exists or no round is halted. Each field is read from one
line of the block (`####` heading, `Location`, `Kind / recommended`, `Claim`, `Verification`,
`bar impact`); a present line with an empty value (an empty Claim or bar impact the
harness rendered) is an empty field. A block of the carry set that lacks a line prints nothing,
names the id and round on stderr and exits 1 — a halted finding is never dropped in silence. A
malformed block that a later round replaced or dropped is not in the carry set and is never read.

Usage
-----
    uv run --no-project <plugin-root>/scripts/review_carry.py --findings-file <path> [<path> ...] --round <m>

Exit 0 on a result, 1 on a malformed carried block, 2 on bad arguments.

When to use
-----------
    review_stage.py (layered-review pipeline mode): its output is the workflow's `carried` argument.
    review_findings_render.py imports `carried_blocks` to re-derive the carried set and to read each
    carried finding's text and layer evidence from the story's findings files.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review_findings_show as show  # noqa: E402
from review_ledger import round_triaged  # noqa: E402

KEYS = ("id", "round", "title", "location", "kind", "recommended", "claim", "verdict", "bar_impact")
ROUND_HEADING = re.compile(r"^## Review round (\d+)\b", re.MULTILINE)
ID = re.compile(r"^R(\d+)-(\d+)$")
JOINED_PAIR = re.compile(r"(R\d+-\d+) → (R\d+-\d+)")


class CarryError(ValueError):
    """A carried block is malformed; nothing is printed."""


def _sections(text: str) -> dict[int, str]:
    """Round number -> its section text; a round that appears twice keeps its last section."""
    heads = list(ROUND_HEADING.finditer(text))
    return {int(h.group(1)): text[h.start():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
            for i, h in enumerate(heads)}


def _value(lines: list[str], field: str) -> str | None:
    prefix = show.FIELDS[field]
    line = next((x for x in lines if x.lower().startswith(prefix)), None)
    return None if line is None else line[len(prefix):].strip()


def _sources(lines: list[str]) -> list[str]:
    return [s.strip() for s in (_value(lines, "sources") or "").split(",") if s.strip()]


def _joined(section: str) -> list[tuple[str, str]]:
    """(carried id, round group id) pairs of a section's Summary `Carried from halted rounds` line."""
    line = next((x for x in section.splitlines() if x.startswith("- Carried from halted rounds:")), "")
    return JOINED_PAIR.findall(line)


def _entry(fid: str, title: str, rnd: int, lines: list[str]) -> dict:
    def need(field: str) -> str:
        value = _value(lines, field)
        if value is None:
            raise CarryError(f"{fid} (round {rnd}): the carried block has no {show.FIELDS[field].strip('- *:[')} line")
        return value

    kind, sep, recommended = need("kind").partition(" / ")
    if not sep:
        raise CarryError(f"{fid} (round {rnd}): the Kind / recommended line has no ' / ' separator")
    verdict = need("verification").split()
    if not verdict:
        raise CarryError(f"{fid} (round {rnd}): the Verification line names no verdict")
    return {"id": fid, "round": rnd, "title": title, "location": need("location").strip("`"), "kind": kind,
            "recommended": recommended, "claim": need("claim"), "verdict": verdict[0],
            "bar_impact": need("bar"), "sources": _sources(lines), "lines": lines}


def carried_blocks(text: str, rnd: int) -> list[dict]:
    """The carry set of round `rnd` with each block's `sources` and field `lines` besides the KEYS."""
    sections = _sections(text)
    lowered = text.lower()
    below = sorted(n for n in sections if n < rnd)
    triaged = max((n for n in below if round_triaged(lowered, n)), default=0)
    carry: dict[str, tuple[int, dict]] = {}
    for n in (n for n in below if n > triaged):
        blocks, dismissed = show.parse(sections[n])
        for fid in dismissed:
            carry.pop(fid, None)
        for cid, gid in _joined(sections[n]):
            if gid in dismissed:
                carry.pop(cid, None)
        for fid, b in blocks.items():
            for s in _sources(b["lines"]):
                if s.startswith("carried:") and s != f"carried:{fid}":
                    carry.pop(s[len("carried:"):], None)
        carry.update({fid: (n, b) for fid, b in blocks.items()})
    entries = [_entry(fid, b["title"], n, b["lines"]) for fid, (n, b) in carry.items()]

    def order(e: dict) -> tuple[int, int, int]:
        n, k = ID.match(e["id"]).groups()
        return e["round"], int(k), int(n)

    return sorted(entries, key=order)


def carried(text: str, rnd: int) -> list[dict]:
    """The carry set of round `rnd`, each entry holding exactly KEYS."""
    return [{k: e[k] for k in KEYS} for e in carried_blocks(text, rnd)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--findings-file", required=True, nargs="+",
                    help="the story's *-code-review-findings*.md files, read in the order given")
    ap.add_argument("--round", required=True, type=int, help="the round about to be reviewed")
    a = ap.parse_args(argv)
    texts = []
    for path in a.findings_file:
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                texts.append(fh.read())
    text = "\n".join(texts)
    try:
        result = carried(text, a.round)
    except CarryError as exc:
        print(f"review-carry: {exc}", file=sys.stderr)
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
