#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Find which story spec or epic owns each UI string, and that story's sprint-status value.

Usage
-----
    uv run --no-project .workflow/scripts/ui_gap_owner.py "Pending Applications" "Approved Roster"
    uv run --no-project .workflow/scripts/ui_gap_owner.py --gaps gaps.txt      # one UI string per line

When to use
-----------
    After a UI comparison (`ui-design-compare`) reports elements the app lacks or shows that the
    design does not have. Each string lists its owning story specs (key, status, file:line, matching
    line) from `config: paths.specs_dir` and the epics in `config: paths.epics_dir`. A hit in a `done`
    story's spec is marked REGRESSION-CANDIDATE. Whether the element is really missing from the app
    is a separate check this script does not make.

Skipped files: review findings (`*-code-review-findings*`), record files (`*-record`), and the
deferred-work and failure-log files. Reads docs only. Exit 0; 2 usage. Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprint_status  # noqa: E402  ROW_RE, the utf-8 stdout reconfigure
import wfconfig  # noqa: E402

SKIP_RE = re.compile(r"-code-review-findings(?:-.*)?$|-record$")
SHOW = 10


def skip_names() -> set[str]:
    names = {"deferred-work"}
    log = str(wfconfig.get("paths.auto_dev_failure_log", "") or "")
    if log:
        names.add(Path(log).stem)
    return names


def statuses(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    path = root / str(wfconfig.get("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml"))
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = sprint_status.ROW_RE.match(line)
        if m:
            out[m.group("key")] = m.group("status")
    return out


def corpus(root: Path) -> list[tuple[Path, list[str], bool]]:
    specs = root / str(wfconfig.get("paths.specs_dir", "docs/implementation-artifacts"))
    epics = root / str(wfconfig.get("paths.epics_dir", "docs/planning-artifacts/epics"))
    skip = skip_names()
    out = []
    for folder, is_epic in ((specs, False), (epics, True)):
        for p in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            if p.stem in skip or SKIP_RE.search(p.stem):
                continue
            out.append((p, p.read_text(encoding="utf-8").splitlines(), is_epic))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("strings", nargs="*")
    ap.add_argument("--gaps", type=Path)
    ap.add_argument("--root", type=Path, default=None, help="checkout to read (default: the repo root)")
    args = ap.parse_args(argv)
    needles = list(args.strings)
    if args.gaps:
        needles += [ln.strip() for ln in args.gaps.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not needles:
        ap.error("give UI strings or --gaps")
    root = (args.root or wfconfig.repo_root()).resolve()
    status = statuses(root)
    texts = corpus(root)
    for needle in needles:
        pat = re.compile(re.escape(needle), re.IGNORECASE)
        hits = [(p, no, line.strip(), epic) for p, lines, epic in texts
                for no, line in enumerate(lines, 1) if pat.search(line)]
        print(f"== {needle}  ({len(hits)} hits)")
        if not hits:
            print("  no spec or epic names this string")
        for p, no, text, epic in hits[:SHOW]:
            rel = p.relative_to(root).as_posix()
            if epic:
                print(f"  epic  {rel}:{no}  {text[:160]}")
                continue
            st = status.get(p.stem, "no sprint-status row")
            mark = "  REGRESSION-CANDIDATE" if st == "done" else ""
            print(f"  {p.stem}  [{st}]{mark}  {rel}:{no}  {text[:160]}")
        if len(hits) > SHOW:
            print(f"  ... {len(hits) - SHOW} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
