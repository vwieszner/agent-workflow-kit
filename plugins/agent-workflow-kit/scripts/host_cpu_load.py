#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
r"""Host-side CPU load generator: N CPU-bound host processes outside the container VM.

Reproduces the load a shared host puts on a gate when other stacks and host processes take CPU
from the container VM — for proving a timing-sensitive test or a gate budget under load.
Standalone tooling, stdlib only, no project imports. Not part of the gate: start it beside a
measured run and stop it after.

Usage
-----
    uv run --no-project <plugin-root>/scripts/host_cpu_load.py --workers N --max-seconds S \
        [--stop-file load.stop] [--interval 10]

    --workers N      number of busy processes; each keeps one logical CPU busy (required).
    --max-seconds S  hard stop, a positive number of seconds (required).
    --stop-file F    stop as soon as this path exists. A path that already exists at start is
                     refused with exit 2: the load would stop at once having done nothing.
    --interval I     heartbeat period in seconds (default 10).

Stopping
--------
Every worker process checks the absolute deadline and the stop-file itself, about four times a
second, so a killed parent leaves no orphan: Windows does not cascade process termination to the
children of a killed process. The parent waits for the workers after the stop condition and
terminates any that outlive the wait.

Output
------
    [hh:mm:ss] host_cpu_load start: workers=N max-seconds=S stop-file=F
    [hh:mm:ss] host_cpu_load: workers=N alive=M remaining=Rs      (every --interval)
    [hh:mm:ss] host_cpu_load stop: reason=max-seconds|stop-file|interrupt|worker-lost
    [hh:mm:ss] host_cpu_load: WORKER LOST workers=N alive=M       (stderr, when a worker exits early)

A worker that exits before a stop condition holds ends the run: the load is below the calibrated
worker count, and a measurement taken under it is not comparable.

Exit codes: 0 stopped normally, 2 argument error, 3 worker lost.
"""
from __future__ import annotations

import argparse
import math
import multiprocessing
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

CHECK_EVERY_S = 0.25
JOIN_GRACE_S = 5.0
SPIN_ITERATIONS = 20_000


def _hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def should_stop(deadline_epoch: float, stop_file: str | None, now: float | None = None) -> str | None:
    """The stop reason, or None while the load should keep running."""
    if (now if now is not None else time.time()) >= deadline_epoch:
        return "max-seconds"
    if stop_file and Path(stop_file).exists():
        return "stop-file"
    return None


def burn(deadline_epoch: float, stop_file: str | None) -> str:
    """Keep one logical CPU busy until a stop condition holds; return the reason."""
    while True:
        reason = should_stop(deadline_epoch, stop_file)
        if reason:
            return reason
        check_at = time.monotonic() + CHECK_EVERY_S
        acc = 0
        while time.monotonic() < check_at:
            for i in range(SPIN_ITERATIONS):
                acc += i * i


def worker_main(deadline_epoch: float, stop_file: str) -> None:
    burn(deadline_epoch, stop_file or None)


def _spawn_workers(n: int, deadline_epoch: float, stop_file: str) -> Iterator:
    """Yield each worker after it has started, so the caller holds every one already running."""
    ctx = multiprocessing.get_context("spawn")
    for _ in range(n):
        proc = ctx.Process(target=worker_main, args=(deadline_epoch, stop_file), daemon=False)
        proc.start()
        yield proc


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Host-side CPU load generator (outside the Docker VM).")
    ap.add_argument("--workers", type=int, required=True, help="number of busy processes")
    ap.add_argument("--max-seconds", type=float, required=True, help="hard stop in seconds")
    ap.add_argument("--stop-file", default="", help="stop when this path appears")
    ap.add_argument("--interval", type=float, default=10.0, help="heartbeat period in seconds")
    return ap


def _validate(a: argparse.Namespace) -> str | None:
    for flag, value in (("--workers", a.workers), ("--max-seconds", a.max_seconds),
                        ("--interval", a.interval)):
        if not math.isfinite(value):
            return f"{flag} must be a finite number (got {value})"
        if value <= 0:
            return f"{flag} must be > 0 (got {value})"
    if a.stop_file and Path(a.stop_file).exists():
        return (f"--stop-file {a.stop_file} already exists: the load would stop at once having "
                "done nothing; remove it first")
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        a = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    problem = _validate(a)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    deadline = time.time() + a.max_seconds
    print(f"[{_hms()}] host_cpu_load start: workers={a.workers} max-seconds={a.max_seconds:g} "
          f"stop-file={a.stop_file or '-'}", flush=True)
    procs: list = []
    reason = "max-seconds"
    next_beat = time.monotonic()
    try:
        for proc in _spawn_workers(a.workers, deadline, a.stop_file):
            procs.append(proc)
        while True:
            reason = should_stop(deadline, a.stop_file or None)
            if reason:
                break
            alive = sum(1 for p in procs if p.is_alive())
            if alive < a.workers:
                # a worker that exits because a stop condition just came true is not a loss
                reason = should_stop(deadline, a.stop_file or None)
                if reason:
                    break
                reason = "worker-lost"
                print(f"[{_hms()}] host_cpu_load: WORKER LOST workers={a.workers} alive={alive}",
                      file=sys.stderr, flush=True)
                break
            if time.monotonic() >= next_beat:
                print(f"[{_hms()}] host_cpu_load: workers={a.workers} alive={alive} "
                      f"remaining={max(0.0, deadline - time.time()):.0f}s", flush=True)
                next_beat += a.interval
            time.sleep(min(CHECK_EVERY_S, a.interval))
    except KeyboardInterrupt:
        reason = "interrupt"
    finally:
        wait_until = time.monotonic() + JOIN_GRACE_S
        for proc in procs:
            proc.join(timeout=max(0.0, wait_until - time.monotonic()))
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
    print(f"[{_hms()}] host_cpu_load stop: reason={reason}", flush=True)
    return 3 if reason == "worker-lost" else 0


if __name__ == "__main__":
    sys.exit(main())
