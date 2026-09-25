#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""SessionStart injector (compact / resume): print the prior-session handoff, if any.

Reads `.workflow/state/handoff.md` (written by write_handoff.py at pre-compaction).
Prints a header plus its contents; prints nothing when the file is absent. Stdout is
injected into the session. Always exits 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

# UTF-8 stdout: a non-cp1252 character would otherwise raise mid-print on Windows and
# silently truncate the injection.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))


def main() -> int:
    import wfconfig

    handoff = wfconfig.workflow_dir() / "state" / "handoff.md"
    if handoff.is_file():
        print("=== Resuming from prior state ===")
        print(handoff.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
