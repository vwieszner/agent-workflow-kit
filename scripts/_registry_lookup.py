#!/usr/bin/env python3
"""Read-only lookup of one story's row in the slot registry (`config: paths.slot_registry`).

Registry format (markdown table, one row per slot):

    | Slot | Status | Story ID | Branch | Worktree | Since |
    |------|--------|----------|--------|----------|-------|
    | 1    | free   | —        | —      | —        | —     |
    | 2    | in_use | 7-2      | story/7-2 | ../proj-worktrees/7-2 | 2026-01-01 |

Empty cells are `—` (U+2014), `-` or blank. A relative Worktree cell resolves against the
repo root. Free rows never match.

Used by story_ledger.py, story_record.py, run_full_suite.py and run_scoped_tests.py.
Writing the registry is NOT done here (slot reservation owns that, under its mutex).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

EMPTY_CELLS = ("", "—", "-", "--")


def _clean(cell: str) -> str:
    c = cell.strip()
    return "" if c in EMPTY_CELLS else c


def registry_path() -> Path:
    return wfconfig.path("paths.slot_registry", "docs/implementation-artifacts/slot-registry.md")


def rows() -> list[dict]:
    """Every data row of the registry table as a dict. Missing registry → []."""
    try:
        text = registry_path().read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        slot = cells[0]
        if not slot.isdigit():  # header or separator row
            continue
        out.append({
            "slot": int(slot),
            "status": _clean(cells[1]).lower(),
            "story_id": _clean(cells[2]),
            "branch": _clean(cells[3]),
            "worktree": _clean(cells[4]),
            "since": _clean(cells[5]) if len(cells) > 5 else "",
        })
    return out


def find(story_ids: list[str] | str) -> dict | None:
    """The non-free row whose Story ID equals one of `story_ids` (case-insensitive).

    Adds `worktree_path` (a resolved Path, or None when the cell is empty).
    """
    ids = [story_ids] if isinstance(story_ids, str) else list(story_ids)
    wanted = {i.strip().lower() for i in ids if i.strip()}
    for row in rows():
        if row["status"] in ("free", "") or not row["story_id"]:
            continue
        if row["story_id"].lower() in wanted:
            wt = row["worktree"]
            if wt:
                p = Path(wt)
                if not p.is_absolute():
                    p = wfconfig.repo_root() / p
                row["worktree_path"] = p.resolve()
            else:
                row["worktree_path"] = None
            return row
    return None
