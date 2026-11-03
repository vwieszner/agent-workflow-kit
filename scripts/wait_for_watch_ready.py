#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Wait until a `docker compose watch` output file shows the watch is up, or name why it is not.

Usage
-----
    uv run --no-project .workflow/scripts/wait_for_watch_ready.py <output-file> [--timeout-sec 300] [--poll-sec 5]

    <output-file>  the file the watch process writes to: the output file of a
                   `bring_up_story_stack.py --mode watch` started in the background, or the log a
                   launch redirected into.

When to use
-----------
    After starting compose watch by hand and before stack_preflight.py or a scoped run: a story
    stack re-upped with `bring_up_story_stack.py --mode up` (that mode does not start watch), a
    stack_preflight `watch` FAIL, the shared stack after an engine restart. Replaces a hand-written
    `until grep -qi "watch enabled" <output> ...` loop.

Each poll reads the file from where the previous poll stopped and prints one heartbeat line
(time, bytes read, last line). Ready is a line containing `config: stack.watch_ready_text` (default
`Watch enabled`, printed by Compose after the watch's image rebuild). A failure is a line containing
`cannot take exclusive lock for project` (a stale compose-watch pid file whose PID was reused),
`compose watch exited with code` (the line bring_up_story_stack.py prints on stderr when the
compose child exits non-zero) or any `config: stack.watch_failure_texts` entry. Matching is
case-insensitive; the first matching line decides. Build output is not parsed for errors. A file
that shrinks (replaced or truncated) is read again from its start, and the lines kept from before are dropped.

Exit codes
----------
    0  ready: a `Watch enabled` line
    1  failed: a failure line, printed once, then the lines before it (the lock case with its remedy)
    2  timeout: neither within --timeout-sec (default 300, max 540: under the 600 s tool cap)
    3  bad arguments

Output: stdout (UTF-8). Stdlib only; reads the file, writes nothing.
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

READY = "watch enabled"
FAILURES = (
    ("cannot take exclusive lock for project",
     "stale compose-watch lock: confirm the PID it names is not a compose process "
     "(proc_scan.py --pid <pid> --chain), delete the compose pid file it names "
     "(Windows: %LOCALAPPDATA%\\docker-compose\\<project>.pid), then restart via "
     "bring_up_story_stack.py --mode watch; files written before the restart need a touch to sync"),
    ("compose watch exited with code", ""),
)


def _ready() -> str:
    return str(wfconfig.get("stack.watch_ready_text", "") or READY).lower()


def _failures() -> tuple:
    extra = tuple((str(t).lower(), "") for t in (wfconfig.get("stack.watch_failure_texts", []) or []))
    return FAILURES + extra


MAX_TIMEOUT_SEC = 540
LAST_LINES = 5


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(3)


def classify(line: str) -> tuple[int, str, str] | None:
    low = line.lower()
    if _ready() in low:
        return 0, line, ""
    for needle, remedy in _failures():
        if needle in low:
            return 1, line, remedy
    return None


class Tail:
    """Incremental reader: complete lines are consumed once, the trailing partial line is re-read.
    `last` holds the non-blank lines before the deciding one, never the deciding line itself."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.partial = b""
        self.exists = False
        self.last: collections.deque[str] = collections.deque(maxlen=LAST_LINES)

    def poll(self) -> tuple[int, str, str] | None:
        try:
            if self.path.stat().st_size < self.offset:  # replaced or truncated: start over
                self.offset, self.partial = 0, b""
                self.last.clear()
            with self.path.open("rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:  # not created yet, or locked by the writer this instant
            return None
        self.exists = True
        self.offset += len(data)
        *complete, self.partial = (self.partial + data).split(b"\n")
        for raw in complete:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            hit = classify(line)
            if hit:
                return hit
            if line.strip():
                self.last.append(line)
        return classify(self.partial.decode("utf-8", "replace")) if self.partial else None


def now() -> str:
    return time.strftime("%H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    ap = _Parser(description="wait for a compose-watch output file to show `Watch enabled`")
    ap.add_argument("output_file", type=Path)
    ap.add_argument("--timeout-sec", type=int, default=300)
    ap.add_argument("--poll-sec", type=int, default=5)
    a = ap.parse_args(argv)
    if not 1 <= a.timeout_sec <= MAX_TIMEOUT_SEC:
        ap.error(f"--timeout-sec must be 1..{MAX_TIMEOUT_SEC} (the wait stays under the 600 s tool cap)")
    if not 1 <= a.poll_sec <= 60:
        ap.error("--poll-sec must be 1..60")

    tail = Tail(a.output_file)
    deadline = time.monotonic() + a.timeout_sec
    while True:
        hit = tail.poll()
        if hit:
            code, line, remedy = hit
            print(f"[{now()}] {'READY' if code == 0 else 'FAILED'}: {line.strip()}")
            if code:
                for prior in tail.last:
                    print(f"  | {prior[:200]}")
                if remedy:
                    print(f"  remedy: {remedy}")
            return code
        left = deadline - time.monotonic()
        if left <= 0:
            break
        state = (f"{tail.offset} bytes, last: {tail.last[-1][:120] if tail.last else '-'}"
                 if tail.exists else "file not created yet")
        print(f"[{now()}] still waiting on compose watch ({state})")
        time.sleep(min(a.poll_sec, left))

    print(f"[{now()}] TIMEOUT after {a.timeout_sec}s: no `Watch enabled` and no failure line in {a.output_file}"
          + ("" if tail.exists else " (the file never appeared)"))
    for prior in tail.last:
        print(f"  | {prior[:200]}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
