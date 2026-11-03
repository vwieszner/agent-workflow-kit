#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Compare the design-token tables of the specification mirror with the design-tool token export.

Reads the token section of the specification document (`config: design.token_doc`, default
`config: docs_mirror.mirror`) — from the `config: design.token_doc_start` heading at a line start to the
first `config: design.token_doc_end` line after it — and the token export (`config: design.tokens_export`,
written by `design_tokens.py export`), and checks every row:

- token tables (`| `<token>` | `<alias>` | <value> |`): the token exists, its alias equals the alias
  cell (a leading `color/` is dropped: the export names primitives without it), and the alias chain
  resolves to the value cell (`8px` = 8, `#FFFFFF @ 5%` = `#FFFFFF@0.05`). A row is filed under the
  table its token prefix names in `config: design.token_table_prefixes` ([[prefix, table], ...]),
  else under `colour table`;
- note lines (`- **<token>:** … `<alias>` (`#RRGGBB`)`) and value bullets (`- `<token>`** (**`<v>`**`):
  the token exists and resolves to the value (the alias too, when the line names one);
- `space/<n> (<n>px)` primitives: each exists with that value;
- the text-style table (`| `<name>` | <family> | <weight> | `<size>px` | `<line>px` |`): family, weight
  name (spaces and case ignored; a trailing `(600)` optional), size and pixel line height;
- the effect-style table: one layer, type, X/Y/blur/spread, colour and opacity.

Prints one line per mismatch (`MISMATCH`, `MISSING in export`), the rows checked per table, then
`EXPORT-ONLY`: the variables of the `config: design.token_semantic_collection` collection, text styles
and effect styles that no row names.

Every table in `config: design.token_required_tables` must match at least one row: a table that
matches none has changed its format, so the run exits 2 instead of reading as "all match".

Usage
-----
    uv run --no-project .workflow/scripts/token_doc_check.py [--mirror <path>] [--export <path>] [--all]

Exit 0: every row matches and nothing is export-only. Exit 1: a mismatch, missing token or export-only
name. Exit 2: unreadable file, missing section heading, a required table with no row, an export
without `variables` / `collections` / `text` / `effect`, or a variable with more than one mode.
Exit 3: SKIPPED (no token doc or no export configured). Read-only, stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

EXPORT_KEYS = ("variables", "collections", "text", "effect")
DEFAULT_PREFIXES = [["radius/", "radius table"], ["space/", "spacing table"]]
DEFAULT_REQUIRED = ["colour table", "text style", "effect style"]


def cfg(key: str, default):
    v = wfconfig.get(f"design.{key}", default)
    return default if v is None else v


def _path(raw: str) -> Path | None:
    return (wfconfig.repo_root() / raw) if raw else None


def default_mirror() -> Path | None:
    return _path(str(cfg("token_doc", "") or wfconfig.get("docs_mirror.mirror", "") or ""))


def default_export() -> Path | None:
    return _path(str(cfg("tokens_export", "") or ""))


def prefixes() -> list[tuple[str, str]]:
    return [(str(p), str(t)) for p, t in (cfg("token_table_prefixes", DEFAULT_PREFIXES) or [])]


def tables() -> tuple[str, ...]:
    """Report order: the prefix tables, then the fixed kinds."""
    names = [t for _, t in prefixes()]
    for t in ("colour note", "colour table", "border-width bullet", "size bullet", "space primitive",
              "text style", "effect style"):
        if t not in names:
            names.append(t)
    return tuple(names)

TOKEN = r"[a-z][a-z\-]*/[a-z0-9\-/]+"
TABLE_ROW_RE = re.compile(rf"^\| `({TOKEN})` \| `([^`]+)` \| (.+?) \|", re.M)
NOTE_RE = re.compile(rf"^- \*\*({TOKEN}):\*\* .*?`({TOKEN})` \(`(#[0-9A-Fa-f]{{6}})`\)", re.M)
BULLET_RE = re.compile(rf"^- `({TOKEN})`\*\* \(\*\*`([^`]+)`", re.M)
PRIMITIVE_RE = re.compile(r"`(space/\d+)` \((\d+)px\)")
TEXT_ROW_RE = re.compile(
    r"^\| `([^`]+)` \| ([^|]+?) \| ([A-Za-z][A-Za-z ]*?)(?: \(\d+\))? \| `(\d+)px` \| `(\d+)px` \|", re.M)
EFFECT_ROW_RE = re.compile(
    r"^\| `([^`]+)` \| (Drop|Inner) shadow \| `X: (-?\d+), Y: (-?\d+), Blur: (\d+), Spread: (-?\d+)` \| "
    r"`(#[0-9A-Fa-f]{6})` @ `(\d+)%` \|", re.M)


class BadInput(Exception):
    pass


def norm_value(cell: str) -> str:
    """`8px` -> `8`; `#ffffff` @ `5%` -> `#FFFFFF@5%` (backticks and spaces dropped)."""
    v = cell.replace("`", "").replace(" ", "")
    if v.endswith("px"):
        v = v[:-2]
    return v.upper() if v.startswith("#") else v


def norm_export(value) -> str:
    """Export value in the same form: 8 -> `8`; `#FFFFFF@0.05` -> `#FFFFFF@5%`."""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    s = str(value)
    if s.startswith("#") and "@" in s:
        hexpart, alpha = s.split("@", 1)
        return f"{hexpart.upper()}@{round(float(alpha) * 100)}%"
    return s.upper() if s.startswith("#") else s


def norm_weight(name) -> str:
    return str(name).replace(" ", "").lower()


def pixel_line_height(lh) -> str:
    """`{unit: PIXELS, value: 20}` -> `20`; any other shape names itself, so it never equals a mirror cell."""
    value = lh.get("value") if isinstance(lh, dict) else None
    if (isinstance(lh, dict) and lh.get("unit") == "PIXELS" and isinstance(value, (int, float))
            and not isinstance(value, bool)):
        return f"{value:g}"
    return f"non-pixel line height {json.dumps(lh, ensure_ascii=False)}"


class Export:
    def __init__(self, doc):
        if not isinstance(doc, dict):
            raise BadInput("export root is not a JSON object")
        missing = [k for k in EXPORT_KEYS if not isinstance(doc.get(k), list)]
        if missing:
            raise BadInput(f"export lacks the list key(s): {', '.join(missing)}")
        self.vars = {v[1]: v for v in doc["variables"]}
        for name, v in self.vars.items():
            if len(v[4]) != 1:
                raise BadInput(f"variable {name} has {len(v[4])} modes")
        semantic_name = str(cfg("token_semantic_collection", "Semantic") or "Semantic")
        semantic = {c[0] for c in doc["collections"] if c[1] == semantic_name}
        self.semantic = sorted(n for n, v in self.vars.items() if v[3] in semantic)
        self.text = {t[1]: t for t in doc["text"]}
        self.effect = {e[1]: e for e in doc["effect"]}

    def raw(self, name: str):
        return next(iter(self.vars[name][4].values()))

    def alias(self, name: str) -> str | None:
        r = self.raw(name)
        return r[6:] if isinstance(r, str) and r.startswith("alias:") else None

    def resolve(self, name: str):
        seen = set()
        while (a := self.alias(name)) is not None:
            if a in seen or a not in self.vars:
                return f"<broken alias {a}>"
            seen.add(a)
            name = a
        return self.raw(name)


def section(md: str) -> str:
    """The text from the start heading line to the first end heading line after it (no start
    configured: the whole document)."""
    start_h = str(cfg("token_doc_start", "") or "")
    end_h = str(cfg("token_doc_end", "") or "")
    if not start_h:
        return md
    start = re.search(rf"^{re.escape(start_h)}", md, re.M)
    if start is None:
        raise BadInput(f"token doc lacks a `{start_h}` heading line")
    if not end_h:
        return md[start.start():]
    end = re.compile(rf"^{re.escape(end_h)}", re.M).search(md, start.end())
    if end is None:
        raise BadInput(f"token doc lacks a `{end_h}` heading line after `{start_h}`")
    return md[start.start():end.start()]


def token_table(tok: str) -> str:
    for prefix, table in prefixes():
        if tok.startswith(prefix):
            return table
    return "colour table"


def check(md: str, exp: Export, show_all: bool) -> int:
    sec = section(md)
    named: set[str] = set()
    checked: dict[str, int] = dict.fromkeys(tables(), 0)
    bad = 0

    def report(table: str, ok: bool, text: str) -> None:
        nonlocal bad
        bad += not ok
        checked[table] += 1
        if not ok or show_all:
            print(f"{'ok      ' if ok else 'MISMATCH'} [{table}] {text}")

    def token_row(table: str, tok: str, prim: str | None, val: str) -> None:
        named.add(tok)
        if tok not in exp.vars:
            report(table, False, f"{tok}: MISSING in export (doc {prim or '-'} = {val})")
            return
        a, r = exp.alias(tok), norm_export(exp.resolve(tok))
        want_alias = prim[6:] if prim and prim.startswith("color/") else prim
        ok = (prim is None or a == want_alias) and r == norm_value(val)
        report(table, ok, f"{tok}: doc {prim or '-'} = {norm_value(val)} | export {a or '-'} = {r}")

    for m in TABLE_ROW_RE.finditer(sec):
        token_row(token_table(m.group(1)), m.group(1), m.group(2), m.group(3))
    for m in NOTE_RE.finditer(sec):
        token_row("colour note", m.group(1), m.group(2), m.group(3))
    for m in BULLET_RE.finditer(sec):
        kind = "border-width bullet" if m.group(1).startswith("border-width/") else "size bullet"
        token_row(kind, m.group(1), None, m.group(2))
    for m in PRIMITIVE_RE.finditer(sec):
        token_row("space primitive", m.group(1), None, m.group(2))

    text_rows = TEXT_ROW_RE.findall(sec)
    effect_rows = EFFECT_ROW_RE.findall(sec)
    for name, family, weight, size, line in text_rows:
        t = exp.text.get(name)
        if t is None:
            report("text style", False, f"{name}: MISSING in export")
            continue
        got = (t[2], t[3], f"{t[4]:g}", pixel_line_height(t[5]))
        want = (family.strip(), weight, size, line)
        same = (got[0] == want[0] and norm_weight(got[1]) == norm_weight(want[1]) and got[2:] == want[2:])
        report("text style", same, f"{name}: doc {want} | export {got}")

    for name, kind, x, y, blur, spread, hexv, pct in effect_rows:
        e = exp.effect.get(name)
        if e is None:
            report("effect style", False, f"{name}: MISSING in export")
            continue
        want = [f"{kind.upper()}_SHADOW", f"{hexv.upper()}@{pct}%", x, y, blur, spread]
        layers = e[2]
        got = ([layers[0][0], norm_export(layers[0][1])] + [f"{n:g}" for n in layers[0][2:6]]
               if len(layers) == 1 else [f"{len(layers)} layers"])
        report("effect style", got == want, f"{name}: doc {want} | export {got}")

    only = ([n for n in exp.semantic if n not in named]
            + [f"text:{n}" for n in exp.text if n not in {r[0] for r in text_rows}]
            + [f"effect:{n}" for n in exp.effect if n not in {r[0] for r in effect_rows}])
    print("checked: " + ", ".join(f"{t} {n}" for t, n in checked.items()))
    required = set(str(t) for t in (cfg("token_required_tables", DEFAULT_REQUIRED) or []))
    empty = [t for t, n in checked.items() if n == 0 and t in required]
    if empty:
        raise BadInput(f"no row matched in: {', '.join(empty)} (has the table's format changed?)")
    print(f"EXPORT-ONLY ({len(only)}): {', '.join(only) or '-'}")
    print(f"{bad} mismatch(es), {len(only)} export-only")
    return 1 if bad or only else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare the spec's token tables with the token export")
    ap.add_argument("--mirror", type=Path, default=None, help="default: config design.token_doc")
    ap.add_argument("--export", type=Path, default=None, help="default: config design.tokens_export")
    ap.add_argument("--all", action="store_true", help="also print the matching rows")
    args = ap.parse_args(argv)
    args.mirror = args.mirror or default_mirror()
    args.export = args.export or default_export()
    if args.mirror is None or args.export is None:
        print("SKIPPED — config: design.token_doc (or docs_mirror.mirror) and design.tokens_export are both needed")
        return 3
    try:
        md = args.mirror.read_text(encoding="utf-8")
        exp = Export(json.loads(args.export.read_text(encoding="utf-8")))
        return check(md, exp, args.all)
    except (OSError, ValueError, KeyError, IndexError, BadInput) as e:
        print(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
