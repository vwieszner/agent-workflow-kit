#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Notification hook: record each waiting event of a session.

Wiring
    Claude Code: `Notification` hook, matchers `permission_prompt` and `idle_prompt`.
    OpenCode:    not wired (no equivalent event).

For every payload on stdin, appends one JSON line to `<state dir>/<session_id>.jsonl`: `received`
(UTC ISO, when the hook read the payload; the payload carries none) plus `session_id`, `cwd`,
`notification_type`, `message` and `transcript_path` copied from it (an absent field is null).
Readers (the story-state board's attention section) use these files to mark the sessions that wait
for the owner.

A Notification hook cannot block or modify a notification, so this one never gets in the way: it
always exits 0, prints nothing, and writes nothing when stdin is not a JSON object, `session_id` is
missing or holds a character outside `[0-9A-Za-z-]` (the value becomes a file name), the command
line is malformed or the write fails. Nothing prunes the files.

Usage
    uv run --no-project <plugin-root>/hooks/session/notification.py [--state-dir <path>]
    (default state dir: .workflow/state/attention/ of the main checkout; `--state-dir` is for tests)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

COPIED_FIELDS = ("session_id", "cwd", "notification_type", "message", "transcript_path")
SESSION_ID_RE = re.compile(r"[0-9A-Za-z-]+")


class _SilentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def default_state_dir() -> Path:
    import wfconfig

    return wfconfig.workflow_dir() / "state" / "attention"


def record(raw: bytes, state_dir: Path) -> None:
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return
    line = {"received": datetime.now(timezone.utc).isoformat(), **{f: payload.get(f) for f in COPIED_FIELDS}}
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / f"{session_id}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    try:
        parser = _SilentParser(add_help=False)
        parser.add_argument("--state-dir", type=Path, default=None)
        args = parser.parse_args(argv)
        record(sys.stdin.buffer.read(), args.state_dir or default_state_dir())
    except Exception:  # noqa: BLE001 -- a side-effect hook must never fail the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
