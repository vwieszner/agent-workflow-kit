#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Session-start nudge about pending curation work. Non-blocking; stdout = injected context.

Wiring
    Claude Code: called by session_start.py (`startup` and `clear`).
    OpenCode:    the workflow-hooks plugin calls it on `session.created`.

Behaviour
    1. Prunes journals / markers / cursors older than `config: curation.journal_retention_days`.
    2. Reports a failed last background analyst run (.workflow/state/retro-launches.jsonl) until a
       later run succeeds or a `--mark` moves its session past the run's facts.
    3. If proposals are waiting in .workflow/state/proposals/ → tells the agent to point the
       owner at the `curate` skill.
    4. Else, if prior sessions hold at least `config: curation.retro_threshold` meaningful facts
       no analysis has covered → tells the agent to point the owner at the `retro` skill.
    Prints nothing when there is nothing to do. `text()` returns the same output for
    session_start.py. Always exits 0.
"""
from __future__ import annotations

import sys
from pathlib import Path


def failed_launch_text(cs) -> str:
    """The failed-last-analyst-run line, or ""."""
    try:
        rec = cs.last_launch()
        if not rec or rec.get("ok"):
            return ""
        session = str(rec.get("session") or "")
        if cs.read_marker(session)["analyzed_facts"] >= int(rec.get("facts") or 0):
            return ""
        return (f"[curation] The last background retrospective (run {rec.get('run_id')}, session "
                f"{session[:8]}) failed: {rec.get('error')}. Its session is back in the `retro` "
                f"backlog; if the headless login expired, log in again. "
                f"Log: .workflow/state/retro-launches.jsonl\n")
    except Exception:  # noqa: BLE001
        return ""


def text() -> str:
    """The curation nudge, or "" when there is nothing to do."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _curation_state as cs
    except Exception:  # noqa: BLE001
        return ""
    failed = failed_launch_text(cs)
    try:
        wf = cs.wfconfig
        cs.prune_journals(int(wf.get("curation.journal_retention_days", 30)))
        pending = cs.pending_proposals()
        if pending:
            names = ", ".join(p.stem for p in pending[:5])
            extra = "" if len(pending) <= 5 else f" (+{len(pending) - 5} more)"
            return failed + (f"[curation] {len(pending)} proposal(s) awaiting review: {names}{extra}. "
                             f"Invoke the `curate` skill to approve/reject.\n")
        unmined = cs.unmined_sessions(int(wf.get("curation.retro_threshold", 8)))
        if unmined:
            total = sum(facts - covered for _, facts, covered in unmined)
            return failed + (f"[curation] {total} unmined activity fact(s) across {len(unmined)} prior "
                             f"session(s). Invoke the `retro` skill to harvest reusable scripts/skills, "
                             f"or ignore.\n")
    except Exception:  # noqa: BLE001
        return failed
    return failed


def main() -> int:
    try:  # never lose the nudge to a console-encoding error
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    sys.stdout.write(text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
