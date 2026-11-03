#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""InstructionsLoaded hook: log which instruction files load, per session and subagent.

Wiring
    Claude Code: `InstructionsLoaded` hook (no matcher). OpenCode: not wired (no equivalent event).

Appends one JSON line per event to `.workflow/state/instructions-loaded.jsonl` of the main checkout:

    {"ts", "session_id", "agent_type", "agent_id", "file_path", "memory_type", "load_reason"}

`agent_type` / `agent_id` are null on the main thread; Claude Code sets them only inside a subagent
(or a session started with `--agent`). The repo root is resolved by wfconfig (`$WORKFLOW_REPO_ROOT`,
else git from `$CLAUDE_PROJECT_DIR`, else the payload's `cwd`), never the shell's current dir, so a
session that changed into a watched source tree never writes the log there.

Payload: `file_path`, `memory_type` (User/Project/Local/Managed), `load_reason` (session_start /
nested_traversal / path_glob_match / include / compact), plus `session_id`, `cwd`, `agent_id`,
`agent_type`. The event cannot block; Claude Code ignores the exit code and output.

Fail-open: any error (unreadable stdin, malformed JSON, unwritable path) exits 0 silently.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().lstrip(b"\xef\xbb\xbf")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return 0
        if not os.environ.get("WORKFLOW_REPO_ROOT"):
            start = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
            if start and os.path.isdir(start):
                os.chdir(start)
        import wfconfig

        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": payload.get("session_id"),
            "agent_type": payload.get("agent_type"),
            "agent_id": payload.get("agent_id"),
            "file_path": payload.get("file_path"),
            "memory_type": payload.get("memory_type"),
            "load_reason": payload.get("load_reason"),
        }
        state = wfconfig.workflow_dir() / "state"
        state.mkdir(parents=True, exist_ok=True)
        with open(state / "instructions-loaded.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
