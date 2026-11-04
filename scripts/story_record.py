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
    uv run --no-project .workflow/scripts/story_record.py append 7-92 "checkpoint-1 spec approved"
    uv run --no-project .workflow/scripts/story_record.py check  <story-id> <fact-key>
    uv run --no-project .workflow/scripts/story_record.py open   <story-id>
    uv run --no-project .workflow/scripts/story_record.py reach  <story-id>
    uv run --no-project .workflow/scripts/story_record.py rule   <story-id> <n>=<ruling> [<n>=<ruling> ...]

`append` resolves the story's worktree from the slot registry (`config: paths.slot_registry`),
writes `<worktree>/<config: paths.specs_dir>/<story-id>-record.md` (creating that dir when the
worktree exists; main checkout only when the story holds no slot), creates the file with its
heading if absent, prepends today's date, and refuses a line identical to the latest record line
of the same fact key (the fact's first word): a retried append is a no-op, but
`checkpoint-3 merge approved` after a withdrawal on the same day is written. An append of a
`checkpoint-1 … approved` line runs `open`'s check and refuses (exit 1) while an open item remains
or no spec is found.

`open` lists what keeps the spec from approval. Exit 1: an open item, printed `OPEN` — a bracketed
`[OPEN-Q …]` marker (a ruled one is written without brackets, `OPEN-Q 3 (a)`), or a latest
`spec-draft` record line that is not `spec-draft complete`. Exit 3: no open item, but prose lines
that may leave a question open, printed `CHECK`, for the caller to judge. Exit 0: neither. No spec
found is exit 1. An approval recorded earlier stops counting while an open item remains.

`reach` tells whether the spec still needs the `ui-state-reach` step: exit 1, printed `MISSING`,
when the spec has no `## UI states and how each is reached` section and the story changes
frontend source (a path under `config: ui.frontend_src_prefixes` not matching
`config: ui.frontend_test_regex`), read from the story branch's diff against the base branch and
from the paths the spec names. Exit 0: the section exists, no frontend source, or no prefixes
configured (SKIPPED). Exit 2: no spec found.

`rule` writes Checkpoint-1 rulings into the spec: every bracketed `[OPEN-Q <n><rest>]` marker of a
ruled number becomes `OPEN-Q <n><rest> (<ruling>)` (a recommendation tail in `<rest>` is dropped).
It refuses, exit 2 and spec unchanged: a malformed or repeated `<n>=<ruling>`; a ruled number with
no marker; a marker tagged for an external owner (`(<tag>` with a tag in
`config: spec.external_question_tags`), which that owner's tracker answers (the design owner's:
`design_ticket.py mark-spec`); a marker naming several
numbers (`1, 2`, `1-3`, `1/2`, `1 and 2`). A rerun whose numbers are all already ruled writes
nothing and exits 0. Line endings are preserved. It ends with `open`'s output and exit code.

Canonical fact lines (the ledger's verdict rules match these):
    checkpoint-1 spec approved
    phase-2 tests GREEN — <labels> (<N> tests)       | phase-2 tests RED — ...
    phase-3 tests GREEN — <labels> (<N> tests)       | phase-3 tests RED — ...
    pre-merge gate GREEN … HEAD <sha12> code-tree <hash12> base <sha12>   (run_full_suite.py --record)
    pre-merge gate not required — <AGENTS.md pre-merge gate clause>
    pre-merge base accepted — <base branch> <sha12> (<reason>)    (only on the owner's instruction)
    checkpoint-3 merge approved                      | checkpoint-3 approval withdrawn -- <cause>
    NEXT: <human-authored next step>                 (overrides the derived next step)

Recognised fact keys for `check`:
    checkpoint-1 | phase-2 | triage | phase-3 | pre-merge | checkpoint-3
`pre-merge` is the land guard (`story_ledger.premerge_verdict`); its MISSING line on stderr carries
the reason: `MISSING  pre-merge gate: <reason>`. `checkpoint-3` is the latest checkpoint-3 verdict
line: a withdrawal un-satisfies it until a later approval.

Exit codes:
    append: 0 appended or already present; 1 checkpoint-1 refused (open items); 2 empty fact.
    check:  0 a POSITIVE record exists; 1 missing (or only a negative verdict); 2 unknown key.

When to use: at every pipeline phase boundary, immediately after the fact becomes
true (spec approved, scoped suite green, merge approved). Never batch these; an
unwritten fact is lost at the next clear.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Single source of truth for id variants, worktree resolution and the positive
# verdict rules — the writer and the reader must not drift.
import story_ledger  # noqa: E402
import wfconfig  # noqa: E402
from story_ledger import (  # noqa: E402
    OPEN_MARKER,
    handoff_state,
    premerge_verdict,
    read_record,
    repo,
    resolve_roots,
    spec_open_items,
    variants,
)

HEADING = "# Story {sid} — decision & evidence record"

# fact-key -> the handoff_state row it satisfies
FACT_ROWS = {
    "checkpoint-1": "spec approved",
    "phase-2": "phase-2 tests",
    "triage": "triage recorded",
    "phase-3": "phase-3 tests",
    "pre-merge": "pre-merge gate",
    "checkpoint-3": "merge approved",
}


def _tags() -> list[str]:
    return [str(t) for t in (wfconfig.get("spec.external_question_tags", []) or []) if str(t).strip()]


def _who() -> str:
    names = ["the owner", "owner", *[t.lower() for t in _tags()]]
    return "|".join(re.escape(n) for n in names)


def _open_prose() -> re.Pattern[str]:
    who = _who()
    return re.compile(r"open question|not an implementer'?s choice|do not pick one|settle it at checkpoint"
                      rf"|halt for (?:{who})|(?:halt|stop) and ask (?:{who})|until (?:{who}) rules"
                      rf"|(?:{who}) to (?:rule|decide)", re.I)


SETTLED_PROSE = re.compile(r"was an open question|no open questions|open questions? (?:for|in) story \d"
                           r"|open questions? (?:were|are) (?:all )?(?:ruled|answered)|open question \d"
                           r"|open questions batched", re.I)
MULTI_PART = re.compile(r"(?:\s*(,|/|&|\band\b)\s*|(-|–|\.\.))(\d+)\b")
REC_TAIL = re.compile(r"\s*(?:\([^()\n]*\)\s*)?,?\s*\(?recommend(?:ed|ation)?\b.*$", re.I)
REACH_HEADING = re.compile(r"^## UI states and how each is reached\s*$", re.M)


def _latest_line_of_key(lines: list[str], key: str) -> str | None:
    """The latest `- <date> <fact>` line whose fact starts with `key`."""
    for ln in reversed(lines):
        parts = ln.split(None, 2)
        if ln.startswith("- ") and len(parts) == 3 and parts[2].split()[0] == key:
            return ln
    return None


def open_items(story_id: str) -> tuple[str | None, list[str], list[str]]:
    """(spec path, open items, prose lines to judge) for the story's spec and record."""
    vs = variants(story_id)
    arts, _branch = resolve_roots(vs)
    _path, lines = read_record(vs, arts)
    spec, items = spec_open_items(vs, lines, arts)
    prose: list[str] = []
    if spec is None:
        return None, items, prose
    with open(os.path.join(repo(), spec), encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    pattern = _open_prose()
    for n, line in enumerate(text.splitlines(), 1):
        if not OPEN_MARKER.search(line) and pattern.search(line) and not SETTLED_PROSE.search(line):
            prose.append(f"{spec}:{n}: {line.strip()[:200]}")
    return spec, items, prose


def cmd_open(story_id: str) -> int:
    spec, items, prose = open_items(story_id)
    if spec is None:
        print(f"OPEN   no spec found for {story_id}", file=sys.stderr)
    for item in items:
        print(f"OPEN   {item}")
    for line in prose:
        print(f"CHECK  {line}")
    if spec is None or items:
        return 1
    if prose:
        return 3
    print(f"CLEAR  {spec}: no open question")
    return 0


def marker_numbers(num: int, rest: str) -> set[int]:
    """Every question number a marker names: its own, plus `, 2` / `/2` / `and 2`, or every number of `-3`."""
    nums, prev, pos = {num}, num, 0
    while m := MULTI_PART.match(rest, pos):
        n = int(m.group(3))
        nums |= set(range(prev, n + 1)) if m.group(2) and n >= prev else {n}
        prev, pos = n, m.end()
    return nums


def _external_tag(text: str, rest: str, end: int) -> str | None:
    """The external-owner tag of a marker: `(<tag>` inside its brackets or right after them."""
    for tag in _tags():
        inside = re.compile(rf"\({re.escape(tag)}\b")
        after = re.compile(rf"[`*]*\s*\({re.escape(tag)}\b")
        if inside.search(rest) or after.match(text, end):
            return tag
    return None


def cmd_rule(story_id: str, pairs: list[str]) -> int:
    rulings: dict[int, str] = {}
    for pair in pairs:
        num, sep, ruling = pair.partition("=")
        num = num.strip()
        if not sep or not num.isdigit() or not ruling.strip() or int(num) in rulings:
            print(f"story-record rule: '{pair}' is not a single <n>=<ruling> (each number once)", file=sys.stderr)
            return 2
        rulings[int(num)] = ruling.strip()
    spec, _items, _prose = open_items(story_id)
    if spec is None:
        print(f"story-record rule: no spec found for {story_id}", file=sys.stderr)
        return 2
    path = os.path.join(repo(), spec)
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()

    problems: list[str] = []
    found: set[int] = set()
    for m in OPEN_MARKER.finditer(text):
        named = marker_numbers(int(m.group("num")), m.group("rest"))
        if not named & rulings.keys():
            continue
        found |= named & rulings.keys()
        if len(named) > 1:
            problems.append(f"'{m.group(0)}' names several questions ({', '.join(map(str, sorted(named)))}): "
                            "rule each one in the spec's prose")
        elif (tag := _external_tag(text, m.group("rest"), m.end())):
            how = ("file it with the design-ticket skill; design_ticket.py mark-spec rewrites the marker"
                   if tag == str(wfconfig.get("design.question_tag", "") or "") else
                   "that owner's question tracker answers it")
            problems.append(f"'{m.group(0)}' is a ({tag}) marker: {how}")
    unbracketed = {n for n in set(rulings) - found if re.search(rf"(?<!\[)\bOPEN-Q {n}(?!\d)", text)}
    problems += [f"OPEN-Q {n}: no marker in {spec}" for n in sorted(set(rulings) - found - unbracketed)]
    if problems:
        print("story-record rule: refusing, spec unchanged", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 2
    if not found:
        print(f"story-record rule: {spec}: OPEN-Q {', '.join(map(str, sorted(unbracketed)))} already ruled, "
              "nothing to rewrite; spec unchanged")
        cmd_open(story_id)
        return 0

    rewritten = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal rewritten
        n = int(m.group("num"))
        if n not in rulings:
            return m.group(0)
        rewritten += 1
        tail = REC_TAIL.search(m.group("rest"))
        rest = m.group("rest")[:tail.start()] if tail else m.group("rest")
        return f"OPEN-Q {n}{rest} ({rulings[n]})"

    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(OPEN_MARKER.sub(_sub, text))
    print(f"story-record rule: {spec}: rewrote {rewritten} marker(s) for OPEN-Q "
          f"{', '.join(map(str, sorted(found)))}"
          + (f"; already ruled: {', '.join(map(str, sorted(unbracketed)))}" if unbracketed else ""))
    return cmd_open(story_id)


def frontend_source(paths) -> list[str]:
    prefixes = tuple(str(p) for p in (wfconfig.get("ui.frontend_src_prefixes", []) or []))
    test_re = re.compile(str(wfconfig.get("ui.frontend_test_regex", r"\.(?:test|spec)\.[jt]sx?$")))
    if not prefixes:
        return []
    return sorted({p for p in paths if p.startswith(prefixes) and not test_re.search(p)})


def cmd_reach(story_id: str) -> int:
    vs = variants(story_id)
    arts, branch = resolve_roots(vs)
    _path, lines = read_record(vs, arts)
    spec, _items = spec_open_items(vs, lines, arts)
    if spec is None:
        print(f"OPEN   no spec found for {story_id}", file=sys.stderr)
        return 2
    prefixes = list(wfconfig.get("ui.frontend_src_prefixes", []) or [])
    if not prefixes:
        print("CLEAR  reach check SKIPPED — config: ui.frontend_src_prefixes is empty")
        return 0
    with open(os.path.join(repo(), spec), encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if REACH_HEADING.search(text):
        print(f"CLEAR  {spec}: reach table present")
        return 0
    base = story_ledger.base_branch()
    if branch:
        changed = story_ledger.git("diff", "--name-only", f"{base}...HEAD", cwd=story_ledger._root_of(arts))
    else:
        changed = story_ledger.git("diff", "--name-only", f"{base}...{wfconfig.branch_name(vs[0])}")
    named = re.findall(r"(?:" + "|".join(re.escape(p) for p in prefixes) + r")[\w./-]*\w", text.replace("\\", "/"))
    sources = frontend_source([p.strip() for p in changed.splitlines()] + named)
    if not sources:
        print(f"CLEAR  {spec}: no frontend source")
        return 0
    print(f"MISSING  {spec}: no reach table; frontend source: {', '.join(sources[:8])}"
          + (f" (+{len(sources) - 8} more)" if len(sources) > 8 else ""))
    return 1


def record_path(story_id: str, create: bool = False) -> str:
    vs = variants(story_id)
    arts, _branch = resolve_roots(vs, create=create)
    return os.path.join(arts, f"{vs[0]}-record.md")


def cmd_append(story_id: str, fact: str) -> int:
    fact = fact.strip().lstrip("-").strip()
    if not fact:
        print("story-record: refusing to append an empty fact", file=sys.stderr)
        return 2
    if fact.split()[0] == "checkpoint-1" and "approved" in fact.split():
        spec, items, _prose = open_items(story_id)
        if spec is None or items:
            print("story-record: refusing checkpoint-1 — the spec has open items"
                  + ("" if spec else f" (no spec found for {story_id})")
                  + "".join(f"\n  {i}" for i in items), file=sys.stderr)
            return 1
    path = record_path(story_id, create=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = f"- {date.today().isoformat()} {fact}"

    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    else:
        existing = HEADING.format(sid=story_id) + "\n\n"

    if _latest_line_of_key(existing.splitlines(), fact.split()[0]) == line:
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
            reason = premerge_verdict(lines, arts, vs[0])[1] if fact_key == "pre-merge" else ""
            print(f"MISSING  {row}" + (f": {reason}" if reason else ""), file=sys.stderr)
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

    o = sub.add_parser("open", help="list the spec's open items (exit 1) or prose lines to judge (exit 3)")
    o.add_argument("story_id")
    r = sub.add_parser("reach", help="exit 1 if the spec needs the ui-state-reach step")
    r.add_argument("story_id")
    ru = sub.add_parser("rule", help="write Checkpoint-1 rulings into the spec's bracketed [OPEN-Q n] markers")
    ru.add_argument("story_id")
    ru.add_argument("rulings", nargs="+", help='<n>=<ruling>, e.g. 1=a "2=a, b as recommended"')

    args = ap.parse_args(argv)
    if args.cmd == "append":
        return cmd_append(args.story_id, args.fact)
    if args.cmd == "rule":
        return cmd_rule(args.story_id, args.rulings)
    if args.cmd == "open":
        return cmd_open(args.story_id)
    if args.cmd == "reach":
        return cmd_reach(args.story_id)
    return cmd_check(args.story_id, args.fact_key)


if __name__ == "__main__":
    sys.exit(main())
