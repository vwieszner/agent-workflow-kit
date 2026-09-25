#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Session-start nudge about pending curation work. Non-blocking; stdout = injected context.

Wiring
    Claude Code: `SessionStart` hook (matchers `startup` and `clear`).
    OpenCode:    the workflow-hooks plugin calls it on `session.created`.

Behaviour
    1. Prunes journals / markers / cursors older than `config: curation.journal_retention_days`.
    2. If proposals are waiting in .workflow/state/proposals/ → tells the agent to point the
       owner at the `curate` skill.
    3. Else, if prior sessions hold at least `config: curation.retro_threshold` meaningful facts
       no analysis has covered → tells the agent to point the owner at the `retro` skill.
    Prints nothing when there is nothing to do. Always exits 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:  # never lose the nudge to a console-encoding error
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def main() -> int:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _curation_state as cs
    except Exception:  # noqa: BLE001
        return 0
    try:
        wf = cs.wfconfig
        cs.prune_journals(int(wf.get("curation.journal_retention_days", 30)))
        pending = cs.pending_proposals()
        if pending:
            names = ", ".join(p.stem for p in pending[:5])
            extra = "" if len(pending) <= 5 else f" (+{len(pending) - 5} more)"
            print(f"[curation] {len(pending)} proposal(s) awaiting review: {names}{extra}. "
                  f"Invoke the `curate` skill to approve/reject.")
            return 0
        unmined = cs.unmined_sessions(int(wf.get("curation.retro_threshold", 8)))
        if unmined:
            total = sum(facts - covered for _, facts, covered in unmined)
            print(f"[curation] {total} unmined activity fact(s) across {len(unmined)} prior "
                  f"session(s). Invoke the `retro` skill to harvest reusable scripts/skills, "
                  f"or ignore.")
    except Exception:  # noqa: BLE001
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
