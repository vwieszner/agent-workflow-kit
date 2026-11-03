#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Every doc line that names a story — dashed and dotted forms, one boundary rule, own files skipped.

Usage
-----
    uv run --no-project .workflow/scripts/story_refs.py 8-34a                       # specs scope
    uv run --no-project .workflow/scripts/story_refs.py 7-39 8-36a --in specs --in epics --in tracker
    uv run --no-project .workflow/scripts/story_refs.py 16-18 16-20 8-37 --among     # hits inside another listed story's own files
    uv run --no-project .workflow/scripts/story_refs.py 10-10 --files                # one line per file: hits, kind, path
    uv run --no-project .workflow/scripts/story_refs.py ab-17 --json
    uv run --no-project .workflow/scripts/story_refs.py 8-34a --root <worktree>      # a worktree's docs

When to use
-----------
    milestone-blockers dependency checks ("which other specs name story X"), story-propagate's sweep,
    and any manual "who references this story" sweep. Reports hits; it never judges whether a hit is a
    dependency — read each line and classify it as the caller's step says.

Keys
----
    Each KEY resolves against the main checkout's sprint-status rows (`sprint_status.ROW_RE`): an exact key,
    or the one key that starts with KEY + "-" where the next character is not a digit (16-1 never resolves to
    16-10). An all-numeric id resolves in either spelling (7-5-6 finds 7.5.6-...). A full key is searched by
    its short id (8-34a-some-slug -> 8-34a); a named key with no numeric id is searched as given. An unknown
    or ambiguous KEY prints its error line, the other keys still run, exit 2.

Match rule (per line, case-insensitive)
---------------------------------------
    Forms: the short id dashed, plus its dotted spelling when all-numeric (8-34a + 8.34a; ab-17 dashed only).
    A form matches when
      - it is not preceded by a letter, digit or "_", by "<digit>-" / "<digit>." (dates, versions, 18-34a),
        or by a path's line-range colon (file.py:10-11). A colon after a word stays a match (dep:16.1);
      - it is not followed by a letter, digit or "_", or by "-<digit>" / ".<digit>" (7-5 never matches 7-5-6).
    A trailing "-<letter>" is allowed, so a slug or full key matches its id. A two-segment all-numeric dotted
    form (8.1) also reads as a decimal; a line whose every match is that form with no "Story"/"Stories"/"dep"
    word before it is kept and marked "#" (number_like in --json). Nothing is dropped for looking numeric.

Own files and kinds
-------------------
    Own files are skipped unless --include-own and counted in each key's header: the stem equals the full key,
    starts with "<id>-" / "<dotted id>-" not followed by a digit, or is "story-<id>" (deferred work). Every hit
    carries its kind — spec, findings, record, registry, history, deferred, tracker, epic, planning.

Scopes (--in, repeatable; default specs)
----------------------------------------
    specs     <config: paths.specs_dir>/*.md (top level)
    history   <config: paths.story_history_dir>/*.md
    deferred  <config: paths.deferred_work_dir>/*.md
    tracker   <config: paths.sprint_status>
    epics     <config: paths.epics_dir>/*.md
    planning  <config: paths.planning_dir>/**/*.md
    all       every scope above

    Docs are read from the main checkout; --root <checkout> reads another. Keys always resolve against the
    main checkout's tracker. Exit 0 hits found, 1 none, 2 a key error. Read-only. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprint_status  # noqa: E402
import wfconfig  # noqa: E402


def _rel_cfg(key: str, default: str) -> str:
    return str(wfconfig.get(key, default)).replace("\\", "/").strip("/")


def scopes_table() -> dict[str, tuple[str, str]]:
    tracker = _rel_cfg("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml")
    tdir, _, tname = tracker.rpartition("/")
    return {
        "specs": (_rel_cfg("paths.specs_dir", "docs/implementation-artifacts"), "*.md"),
        "history": (_rel_cfg("paths.story_history_dir", "docs/implementation-artifacts/story-history"), "*.md"),
        "deferred": (_rel_cfg("paths.deferred_work_dir", "docs/implementation-artifacts/deferred-work"), "*.md"),
        "tracker": (tdir or ".", tname),
        "epics": (_rel_cfg("paths.epics_dir", "docs/planning-artifacts/epics"), "*.md"),
        "planning": (_rel_cfg("paths.planning_dir", "docs/planning-artifacts"), "**/*.md"),
    }


SCOPES = scopes_table()
NUMERIC_ID = re.compile(r"\d+(?:[.-]\d+)+[a-z]?", re.IGNORECASE)
# A full key's leading id: an optional letter prefix (tq-, cr-) then the dashed numeric segments, each
# with an optional letter suffix (8-34a-dm-seals-... -> 8-34a, tq-17-... -> tq-17).
SHORT_RE = re.compile(r"^((?:[a-z]+\d*-)?\d+[a-z]?(?:-\d+[a-z]?)*)(?:-|$)")
STORY_WORD = re.compile(r"(?:stor(?:y|ies)|deps?)\W{0,3}$", re.IGNORECASE)
# Left edge of a match: not inside a longer token, a date, a version or a longer id (18-34a, 2026-10-10),
# and not a path's line range (views.py:10-11, `:647-648`). A word's colon stays a match: dep:16.1 is a dep.
LEFT = (r"(?<![A-Za-z0-9_])(?<!\d[.-])(?<![^A-Za-z]:)"
        + "".join(rf"(?<!\.[A-Za-z]{{{n}}}:)" for n in range(1, 6)))


class Story:
    def __init__(self, arg: str, full: str):
        self.arg = arg
        self.full = full
        sid = arg.replace(".", "-") if NUMERIC_ID.fullmatch(arg) else arg
        if sid == full:
            m = SHORT_RE.match(full)
            if m:
                sid = m.group(1)
        self.sid = sid.lower()
        self.forms = [self.sid]
        if NUMERIC_ID.fullmatch(self.sid):
            self.forms.append(self.sid.replace("-", "."))
        alt = "|".join(re.escape(f) for f in sorted(self.forms, key=len, reverse=True))
        self.pattern = re.compile(LEFT + r"(?:" + alt + r")(?![A-Za-z0-9_])(?![.-]\d)", re.IGNORECASE)
        # 8.1, 16.1, 7.20 also read as decimals ("8.1 s", "8.1:1", "-8.1 %"); 8.34a and 7.5.6 do not.
        self.decimal_form = self.forms[1] if len(self.forms) > 1 and re.fullmatch(r"\d+\.\d+", self.forms[1]) else None

    def number_like(self, line: str) -> bool:
        """Every match on the line is the decimal-shaped dotted form with no "Story"/"Stories"/"dep" word before it."""
        if self.decimal_form is None:
            return False
        for m in self.pattern.finditer(line):
            if m.group(0).lower() != self.decimal_form or STORY_WORD.search(line[:m.start()]):
                return False
        return True

    def owns(self, path: Path) -> bool:
        stem = path.stem.lower()
        if stem == self.full.lower():
            return True
        for f in self.forms:
            if stem == f"story-{f}":
                return True
            if stem.startswith(f + "-") and not stem[len(f) + 1:len(f) + 2].isdigit():
                return True
        return False


def tracker_keys() -> list[str]:
    path = sprint_status.default_path()
    if not path.is_file():
        sprint_status._die(f"sprint-status file not found: {path}")
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = sprint_status.ROW_RE.match(line)
        if m:
            keys.append(m.group("key"))
    return keys


def resolve(arg: str, keys: list[str], keyset: set[str]) -> tuple[str | None, str]:
    """(full key, "") or (None, reason): an exact key in either spelling, else the one key that starts
    with "<id>-" where the next character is not a digit; none or several such keys is the reason."""
    forms = {arg}
    if NUMERIC_ID.fullmatch(arg):
        forms |= {arg.replace(".", "-"), arg.replace("-", ".")}
    for f in forms:
        if f in keyset:
            return f, ""
    hits = sorted({k for f in forms for k in keys
                   if k.startswith(f + "-") and not k[len(f) + 1:len(f) + 2].isdigit()})
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, f"no story row matching {arg!r}"
    return None, f"{arg!r} is ambiguous - matches {len(hits)}: {', '.join(hits[:8])}"


def scan_files(root: Path, scopes: list[str]) -> list[Path]:
    seen: dict[Path, None] = {}
    for scope in scopes:
        base, pattern = SCOPES[scope]
        for f in sorted((root / base).glob(pattern)):
            if f.is_file():
                seen[f] = None
    return list(seen)


def kind_of(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    if (SCOPES["tracker"][0].rstrip("/") + "/" + SCOPES["tracker"][1]).lstrip("./") == rel:
        return "tracker"
    if rel.startswith(SCOPES["history"][0] + "/"):
        return "history"
    if rel.startswith(SCOPES["deferred"][0] + "/"):
        return "deferred"
    if rel.startswith(SCOPES["epics"][0] + "/"):
        return "epic"
    if rel.startswith(SCOPES["planning"][0] + "/"):
        return "planning"
    if "code-review-findings" in name:
        return "findings"
    if name.endswith("-record.md"):
        return "record"
    if rel == _rel_cfg("paths.slot_registry", "docs/implementation-artifacts/slot-registry.md"):
        return "registry"
    return "spec"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        epilog="Full rules: the module docstring (.workflow/scripts/story_refs.py).",
    )
    parser.add_argument("keys", nargs="+", metavar="KEY")
    parser.add_argument("--in", dest="scopes", action="append", choices=[*SCOPES, "all"],
                        help="scope to read (repeatable; default specs)")
    parser.add_argument("--among", action="store_true",
                        help="keep only hits inside another listed key's own files")
    parser.add_argument("--include-own", action="store_true", help="also report hits in the key's own files")
    parser.add_argument("--files", action="store_true", help="one line per file instead of per line")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--width", type=int, default=220, help="line text cut (default 220)")
    parser.add_argument("--root", type=Path, help="checkout whose docs to read (default: the main checkout)")
    args = parser.parse_args()

    if not args.scopes:
        scopes = ["specs"]
    elif "all" in args.scopes:
        scopes = list(SCOPES)
    else:
        scopes = list(dict.fromkeys(args.scopes))
    root = (args.root or wfconfig.repo_root()).resolve()

    keys = tracker_keys()
    keyset = set(keys)
    stories: list[Story] = []
    errors: list[dict] = []
    for arg in args.keys:
        full, reason = resolve(arg, keys, keyset)
        if full is None:
            errors.append({"key": arg, "error": reason})
        else:
            stories.append(Story(arg, full))

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    files = [(f, f.relative_to(root).as_posix(), f.read_text(encoding="utf-8", errors="replace"))
             for f in scan_files(root, scopes)]
    report = []
    for s in stories:
        hits, own_skipped = [], []
        for f, rel, text in files:
            own = s.owns(f)
            others = [o.sid for o in stories if o is not s and o.owns(f)]
            if args.among and not others:
                continue
            if own and not args.include_own:
                if s.pattern.search(text):
                    own_skipped.append(rel)
                continue
            kind = ("own-" if own else "") + kind_of(rel)
            for n, line in enumerate(text.splitlines(), 1):
                if s.pattern.search(line):
                    hit = {"path": rel, "line": n, "kind": kind, "number_like": s.number_like(line),
                           "text": line.strip()[:args.width]}
                    if args.among:
                        hit["in_own_of"] = others
                    hits.append(hit)
        report.append({"key": s.arg, "full": s.full, "forms": s.forms, "scopes": scopes,
                       "own_skipped": own_skipped, "hits": hits})

    if args.json:
        print(json.dumps({"root": root.as_posix(), "keys": report, "errors": errors}, ensure_ascii=False, indent=1))
    else:
        for e in errors:
            print(json.dumps({"status": "error", "key": e["key"], "message": e["error"]}))
        for r in report:
            nfiles = len({h["path"] for h in r["hits"]})
            print(f"== {r['key']}  ({r['full']}; forms {', '.join(r['forms'])}; {'+'.join(r['scopes'])}"
                  f"{'; among' if args.among else ''}): {len(r['hits'])} hits in {nfiles} files;"
                  f" own files skipped: {len(r['own_skipped'])}")
            if args.files:
                per: dict[str, list] = {}
                for h in r["hits"]:
                    entry = per.setdefault(h["path"], [0, 0, h["kind"]])
                    entry[0] += 1
                    entry[1] += h["number_like"]
                for path, (count, numeric, kind) in per.items():
                    note = "  (all number-like)" if numeric == count else ""
                    print(f"  {count:>3}  {kind:<12} {path}{note}")
            else:
                for h in r["hits"]:
                    among = f" [in {', '.join(h['in_own_of'])}]" if args.among else ""
                    mark = "#" if h["number_like"] else " "
                    print(f" {mark}{h['kind']:<12} {h['path']}:{h['line']}{among}: {h['text']}")
    if errors:
        return 2
    return 0 if any(r["hits"] for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
