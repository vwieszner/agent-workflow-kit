#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""SessionStart injector (startup / clear / compact / resume): position of in-flight stories.

Reads the slot registry (`config: paths.slot_registry`, a markdown table whose rows start
`| <slot> | <status> | <story_id> | ...`). For every numbered slot whose status is not
`free`, loads `.workflow/scripts/story_ledger.py` and calls `build(story_id)`; from the
returned markdown it prints only the `**Position:**` line and the `**RESUMABLE...` line —
never the full ledger. At most `config: slots.count` stories.

Prints NOTHING when every slot is free. Fail-silent: any error exits 0 with no output.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def in_flight(registry: Path, limit: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        text = registry.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        slot, status, story = cells[0], cells[1].lower(), cells[2]
        if not slot.isdigit() or status in ("free", "status", ""):
            continue
        if story and story not in ("—", "-", ""):
            out.append((slot, story))
    return out[:limit]


def load_ledger():
    spec = importlib.util.spec_from_file_location("story_ledger", SCRIPTS_DIR / "story_ledger.py")
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    import wfconfig

    registry = wfconfig.path("paths.slot_registry", "docs/implementation-artifacts/slot-registry.md")
    stories = in_flight(registry, int(wfconfig.get("slots.count", 6)))
    if not stories:
        return 0
    mod = load_ledger()
    if mod is None:
        return 0
    lines: list[str] = []
    for slot, story in stories:
        try:
            report = mod.build(story)
        except Exception:
            continue
        pos = next((l for l in report.splitlines() if l.startswith("**Position:**")), "")
        res = next((l for l in report.splitlines() if l.startswith("**RESUMABLE")), "")
        if pos or res:
            lines.append(f"  slot {slot} — {story}")
            if pos:
                lines.append(f"    {pos.replace('**Position:**', 'position:').strip()}")
            if res:
                lines.append(f"    {res.replace('**', '').strip()[:200]}")
    if not lines:
        return 0
    print("=== In-flight stories (position only) ===")
    print("\n".join(lines))
    print("  Full ledger: uv run --no-project .workflow/scripts/story_ledger.py <story-id>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
