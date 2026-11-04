#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Every drawn instance of one design-library component, with the values the library build records.

For each screen frame map that lists the component, prints per instance:
  * the frame-map row (`<library>/frame-map/<frame>.md`): where the instance sits and which fields
    the frame's instances override;
  * the instance's subtree in the frame dump (`<library>/frames/<frame>.md`), for an instance placed
    directly on the screen;
  * for an instance nested in another component (`I<outer>;...;<inner>`), the inner node's subtree in
    the containing component's value sheet (`<library>/sets/*.md`);
  * the design tool's codegen lines carrying its node id or a descendant's
    (`<intake>/design-context/<frame>.md`, written by `design_context.py`): the drawn label, icons and classes;
then each requested variant's tree from the component's own value sheet. An overridden field whose
value none of those lines shows is not recorded in the build; the report says so.

`<library>` is `config: design.library_dir`, `<intake>` is `config: design.intake_dir`; the build is
written by `design_library.py`.

Usage
-----
    uv run --no-project .workflow/scripts/design_instances.py <component> [--frame <slug>]... [--worktree <path>]

`<component>` is a component set node id (`3:5`), a variant node id (`3:6`), a LOCAL component id
or the component name (`"Button - Primary"`). A set id or a name covers every variant and the frames'
LOCAL copies of the set. `--frame` limits the report to the named frame maps (repeatable).
`--worktree` reads the build in that checkout (default: the repo root).

When to use
-----------
    A spec, review or implementation needs the values a component is drawn with on each screen
    (which instances override what) before writing or checking ACs that keep or change them.

Exit codes: 0 = report printed (also when no instance matched); 2 = bad arguments or no library
build under the worktree; 3 = SKIPPED (`config: design.library_dir` is empty).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

NODE = re.compile(r"^\d+:\d+$")
WHERE = re.compile(r"`(I?[\d:;]+)`(?: in (.*?))?(?=; `|$)")
LINE_CAP = 360
SUBTREE_CAP = 10
SHEET_HITS = 3
CODEGEN_HITS = 10


def library_rel() -> Path | None:
    raw = str(wfconfig.get("design.library_dir", "") or "")
    return Path(raw) if raw else None


def intake_rel() -> Path:
    raw = str(wfconfig.get("design.intake_dir", "") or "")
    lib = library_rel()
    return Path(raw) if raw else (lib.parent if lib else Path("."))


def clip(s: str) -> str:
    s = s.rstrip()
    return s if len(s) <= LINE_CAP else s[:LINE_CAP] + " …"


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split(" | ")]


def table_rows(lines: list[str]):
    for n, line in enumerate(lines, 1):
        if line.startswith("| ") and not line.startswith("|---"):
            yield n, cells(line)


def read_lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines() if p.is_file() else []


def indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def subtree(lines: list[str], i: int) -> list[tuple[int, str]]:
    """Line `i` (0-based) and the deeper-indented lines under it, capped."""
    out = [(i + 1, lines[i])]
    for j in range(i + 1, len(lines)):
        if not lines[j].strip() or indent(lines[j]) <= indent(lines[i]):
            break
        if len(out) == SUBTREE_CAP:
            out.append((j + 1, " " * indent(lines[j]) + "… (subtree continues)"))
            break
        out.append((j + 1, lines[j]))
    return out


def index_components(ml: Path) -> list[dict]:
    rows = []
    for _, c in table_rows(read_lines(ml / "INDEX.md")):
        if len(c) < 6 or c[0] == "Component":
            continue
        sheet = re.search(r"\((sets/[^)]+)\)", c[5])
        rows.append({"name": c[0], "node": c[1].strip("`"), "variants": re.findall(r"`(\d+:\d+)`", c[4]),
                     "sheet": sheet.group(1) if sheet else None})
    return rows


def resolve(query: str, components: list[dict]) -> tuple[set[str], set[str], list[str]]:
    """(node ids a frame-map master cell may name, component names, value sheets)."""
    ids, names, sheets = set(), set(), []
    for r in components:
        whole = query in (r["name"], r["node"])
        if whole or query in r["variants"]:
            names.add(r["name"])
            ids.update(r["variants"] if whole else [query])
            if r["sheet"]:
                sheets.append(r["sheet"])
    if not names and NODE.match(query):
        ids.add(query)
    return ids, names, sheets


def report(query: str, worktree: Path, frames: set[str]) -> list[str]:
    ml = worktree / library_rel()
    components = index_components(ml)
    ids, names, sheets = resolve(query, components)
    # A variant id names one variant; a set id or a name also takes the frames' LOCAL copies.
    whole_set = bool(names) and not any(query in r["variants"] for r in components)
    sets = {p: read_lines(p) for p in sorted((ml / "sets").glob("*.md"))}
    out = [f"# `{query}` — {', '.join(sorted(names)) or 'no library set'}; node ids {', '.join(sorted(ids))}",
           f"Build: {ml}"]
    found = 0
    for fm in sorted((ml / "frame-map").glob("*.md")):
        slug = fm.stem
        if frames and slug not in frames:
            continue
        hits = []
        for n, c in table_rows(read_lines(fm)):
            if len(c) < 4 or c[0] == "Master component":
                continue
            master = c[0]
            if any(f"`{i}`" in master for i in ids) or (whole_set and any(
                    f"**{nm}**" in master or f"(set `{nm}`)" in master for nm in names)):
                hits.append((n, c))
        if not hits:
            continue
        dump = read_lines(ml / "frames" / f"{slug}.md")
        codegen = read_lines(worktree / intake_rel() / "design-context" / f"{slug}.md")
        out.append(f"\n## {slug}")
        for n, (master, count, where, overrides) in ((n, c[:4]) for n, c in hits):
            listed = [(m.group(1), (m.group(2) or "").strip()) for m in WHERE.finditer(where)]
            found += len(listed)
            out.append(f"- frame-map/{slug}.md:{n} — {clip(master)} · {count} instance(s) · overrides: {overrides}")
            if count.isdigit() and int(count) > len(listed):
                out.append(f"  - {int(count) - len(listed)} more instance(s): the frame map lists the first {len(listed)} only")
            for iid, at in listed:
                out.append(f"  - instance `{iid}`{' in ' + at if at else ''}")
                tag = f"`{iid}`"
                for i, line in enumerate(dump):
                    if tag in line:
                        out += [f"    frames/{slug}.md:{k}: {clip(t.strip())}" for k, t in subtree(dump, i)]
                        break
                if ";" in iid:
                    inner, shown = f"`{iid.split(';')[-1]}`", 0
                    for p, lines in sets.items():
                        for i, line in enumerate(lines):
                            if inner in line and line.lstrip().startswith(f"- {inner}") and shown < SHEET_HITS:
                                shown += 1
                                out += [f"    sets/{p.name}:{k}: {clip(t.strip())}" for k, t in subtree(lines, i)]
                codegen_hits = [(k, t) for k, t in enumerate(codegen, 1)
                                if f'data-node-id="{iid}"' in t or f'data-node-id="{iid};' in t]
                out += [f"    design-context/{slug}.md:{k}: {clip(t.strip())}" for k, t in codegen_hits[:CODEGEN_HITS]]
                if not codegen_hits:
                    out.append(f"    design-context/{slug}.md: {'no codegen line for this instance' if codegen else 'no file'}")
                elif len(codegen_hits) > CODEGEN_HITS:
                    out.append(f"    design-context/{slug}.md: {len(codegen_hits) - CODEGEN_HITS} more codegen line(s)")
            if overrides not in ("", "—"):
                out.append(f"  - overridden: {overrides}. A value the lines above do not show is not recorded in the build.")
                if "effects" in overrides:
                    out.append("  - `effects`: the build keeps only visible effects, so a removed or hidden effect reads as none.")
    out.append(f"\n{found} instance(s) listed across the frame maps.")
    if names or sheets:
        out.append("\n## Master value sheets")
        for sheet in dict.fromkeys(sheets):
            lines = read_lines(ml / sheet)
            for i, line in enumerate(lines):
                if any(line.lstrip().startswith(f"- `{vid}` component") for vid in ids):
                    out += [f"{sheet}:{k}: {clip(t.strip())}" for k, t in subtree(lines, i)]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("component", help="set id, variant id, LOCAL component id or component name")
    ap.add_argument("--frame", action="append", default=[], help="frame-map file name without .md (repeatable)")
    ap.add_argument("--worktree", type=Path, default=None, help="checkout whose build to read (default: repo root)")
    args = ap.parse_args(argv)
    if library_rel() is None:
        print("SKIPPED — config: design.library_dir is empty")
        return 3
    args.worktree = args.worktree or wfconfig.repo_root()
    if not (args.worktree / library_rel() / "INDEX.md").is_file():
        sys.stderr.write(f"no library build under {args.worktree / library_rel()}\n")
        return 2
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(report(args.component, args.worktree, set(args.frame))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
