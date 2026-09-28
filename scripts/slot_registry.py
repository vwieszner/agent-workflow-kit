#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Slot registry — the one definition of its file format, plus `init`.

The registry (`config: paths.slot_registry`, committed) is a Markdown table with
one row per isolated slot 1..`config: slots.count` (the shared slot,
`config: slots.shared_slot`, is never a row):

    | Slot | Status | Story ID | Branch | Worktree | Since |
    |------|--------|----------|--------|----------|-------|
    | 1 | free | — | — | — | — |
    | 2 | in_use | 7-2 | story/7-2 | /abs/path/to/worktrees/7-2 | 2026-01-01 |

Empty cells are U+2014. Story IDs are stored dashed (`7.5.8` → `7-5-8`).
Rows are written only by reserve_slot.py / release_slot.py / this script, each
under the `slot-registry` NamedMutex. Read-only lookups use _registry_lookup.py.

CLI:
  uv run --no-project .workflow/scripts/slot_registry.py init [--registry-path P]
      Creates the registry from config if it does not exist (never overwrites).

Output (single JSON line on stdout):
  {"status":"created","path":"...","slots":6}
  {"status":"exists","path":"..."}
  {"status":"error","message":"..."}

Exit codes: 0 created or exists, 1 error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
from _named_mutex import NamedMutex  # noqa: E402

MUTEX_NAME = "slot-registry"
EM_DASH = "—"
TABLE_HEADER = "| Slot | Status | Story ID | Branch | Worktree | Since |"
TABLE_RULE = "|------|--------|----------|--------|----------|-------|"


def registry_path() -> Path:
    return wfconfig.path("paths.slot_registry", "docs/implementation-artifacts/slot-registry.md")


def dashed(story_id: str) -> str:
    return story_id.strip().replace(".", "-")


def slot_numbers() -> list[int]:
    shared = int(wfconfig.get("slots.shared_slot", 0))
    count = int(wfconfig.get("slots.count", 6))
    return [n for n in range(1, count + 1) if n != shared]


def port_formula_line() -> str:
    if not wfconfig.stack_enabled():
        return ('No isolated stack (`config: stack.runtime = "none"`): '
                "slots are git worktrees only.")
    ports = wfconfig.get("stack.ports", {}) or {}
    stride = int(wfconfig.get("stack.port_stride", 100))
    if not ports:
        return "No host ports declared (`config: stack.ports` is empty)."
    formula = " | ".join(f"{k}={v}+N*{stride}" for k, v in ports.items())
    return f"Port assignments for slot N (`config: stack.ports` + N * `stack.port_stride`):\n`{formula}`"


def header_lines() -> list[str]:
    shared = int(wfconfig.get("slots.shared_slot", 0))
    return [
        "# Slot Registry",
        "",
        "Tracks which isolation slots are currently in use.",
        f"Slot {shared} is the shared dev stack — never assigned to stories.",
        "Rows are written only by `.workflow/scripts/reserve_slot.py` and "
        "`.workflow/scripts/release_slot.py`; never edit a row by hand while a session is running.",
        "",
        port_formula_line(),
        "",
        TABLE_HEADER,
        TABLE_RULE,
    ]


def free_row(slot: int) -> str:
    return f"| {slot} | free | {EM_DASH} | {EM_DASH} | {EM_DASH} | {EM_DASH} |"


def in_use_row(slot: int, story_id: str, branch: str, worktree: str, since: str) -> str:
    return f"| {slot} | in_use | {story_id} | {branch} | {worktree} | {since} |"


def parse_rows(lines: list[str]) -> list[dict]:
    """Data rows with their line index: {index, slot, status, story_id, branch, worktree, since}.

    The one parser for the format (_registry_lookup reads through it). A missing Since
    cell is tolerated; fewer than five cells is not a data row.
    """
    out = []
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or not cells[0].isdigit():
            continue
        clean = ["" if c in ("", EM_DASH, "-", "--") else c for c in cells]
        out.append({"index": i, "slot": int(cells[0]), "status": clean[1].lower(),
                    "story_id": clean[2], "branch": clean[3], "worktree": clean[4],
                    "since": clean[5] if len(clean) > 5 else ""})
    return out


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines()


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")  # no trailing newline (row-writer parity)


def init_registry(path: Path) -> str:
    """Create the registry if absent. Caller holds the mutex. Returns created|exists."""
    if path.is_file():
        return "exists"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_lines(path, header_lines() + [free_row(n) for n in slot_numbers()])
    return "created"


def main() -> int:
    ap = argparse.ArgumentParser(description="slot registry maintenance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create the registry from config if absent")
    i.add_argument("--registry-path", default=None)
    i.add_argument("--mutex-timeout-ms", type=int, default=60000)
    args = ap.parse_args()

    path = Path(args.registry_path) if args.registry_path else registry_path()
    try:
        with NamedMutex(MUTEX_NAME, args.mutex_timeout_ms):
            status = init_registry(path)
    except (TimeoutError, OSError) as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1
    out = {"status": status, "path": str(path)}
    if status == "created":
        out["slots"] = len(slot_numbers())
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
