#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Orchestrator-side poll on the per-stack status file written by bring_up_stack_async.py.

story_setup.py launches the stack bring-up in the background and returns
immediately. Before the stack preflight (Pre-Phase 2), the orchestrator MUST
confirm the bring-up reached `up` — otherwise preflight fails on half-started
containers.

Polls `.workflow/state/story-setup/<stack>.json` until the status reaches `up`
(or `failed`, or the timeout fires), printing a timestamped heartbeat each
iteration. Idempotent: an `up` status file returns immediately.

Staleness: the status file records that bring-up FINISHED, not that the stack is
running NOW. So an `up` status is confirmed against the container engine: the
count of `config: stack.services` that are ready right now (the readiness
predicate is `_stack.ready_services`, shared with the bring-up). A partially-dead
stack keeps some containers up, so the check is a count, not "anything running".
If the engine cannot be queried at all, the status file stays the signal (the
line says liveness was not confirmed).

`config: stack.runtime = "none"` → prints a SKIPPED line and exits 0.

Budget: `--timeout-sec` counts from the waiter's start. While the helper reports `retrying` (a
failed seed on a fresh stack, wiped and retried once) its `retry_started_at` is a second anchor:
the deadline is the later of (waiter start + timeout) and (retry start + timeout), re-evaluated
every poll. A retry that starts after the waiter gets a full timeout from its own start; one that
started before never shortens the waiter's. The TIMEOUT line names the anchor that ran out.

Exit codes:
  0 -- stack reached `up` AND its services are running now (or stack runtime is none)
  1 -- stack reported `failed` (log tail printed), or no status file
  2 -- timeout (log tail printed; orchestrator decides retry / abort)
  3 -- STALE `up`: bring-up completed, but the stack is not running any more
       (re-run bring_up_story_stack.py --mode up; do NOT proceed to preflight)

Run: uv run --no-project <plugin-root>/scripts/wait_for_stack_ready.py <stack> [--timeout-sec 300]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def retry_started(status: dict) -> datetime | None:
    """When the helper began the fresh-stack retry pass: a `retrying` record's `retry_started_at`."""
    if status.get("status") != "retrying":
        return None
    try:
        return datetime.fromisoformat(str(status["retry_started_at"]))
    except (KeyError, ValueError):
        return None


def spent_anchor(waited: float, since_retry: float | None, timeout_sec: int) -> str | None:
    """The anchor whose timeout has run out, or None while the deadline has not passed. The anchor
    that began last decides; a retry that began before the waiter never shortens its budget."""
    if since_retry is not None and since_retry < waited:
        return "the retry start" if since_retry > timeout_sec else None
    return "the waiter start" if waited > timeout_sec else None


def live_missing(stack: str, repo_root: Path) -> set[str] | None:
    """Expected services NOT ready right now, or None if the engine could not be
    queried (never fail the caller on an engine hiccup — this is a confirmation).

    `compose -p <stack> ps` resolves the project by label (no compose file needed);
    it still receives the slot's port vars.
    """
    try:
        services = _stack.compose_ps(stack, repo_root, _stack.env_for_stack(stack))
    except Exception as e:  # engine not running, CLI missing, etc.
        print(f"[{ts()}] {stack}: could not query the container engine to confirm liveness ({e})")
        return None
    return set(_stack.expected_services()) - _stack.ready_services(services)


def tail(path: Path, n: int = 20) -> None:
    if path.is_file():
        print("  log tail:")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
            print(f"    {line}")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue] — heartbeats must reach the orchestrator live
    ap = argparse.ArgumentParser(description="poll a slot stack's async bring-up status")
    ap.add_argument("stack", help="compose project name (e.g. story-7-14)")
    ap.add_argument("--repo-root", default=None, help="default: wfconfig repo root")
    ap.add_argument("--timeout-sec", type=int, default=None,
                    help="default: config: stack.ready_wait_timeout_s (300)")
    ap.add_argument("--poll-interval-sec", type=int, default=None,
                    help="default: config: stack.ready_poll_s (5)")
    args = ap.parse_args()
    repo_root = _stack.set_repo_root(args.repo_root)
    if args.timeout_sec is None:
        args.timeout_sec = int(wfconfig.get("stack.ready_wait_timeout_s", 300))
    if args.poll_interval_sec is None:
        args.poll_interval_sec = int(wfconfig.get("stack.ready_poll_s", 5))

    if not wfconfig.stack_enabled():
        _stack.skipped(f"wait for stack {args.stack}", _stack.STACK_NONE_REASON)
        return 0

    state = wfconfig.state_dir("story-setup")
    status_file = state / f"{args.stack}.json"
    log_file = state / f"{args.stack}.log"

    if not status_file.is_file():
        print(f"wait_for_stack_ready: no status file at {status_file}")
        print("  (story_setup.py was not run, or the stack name is wrong)")
        return 1

    started = time.monotonic()
    retry_at: datetime | None = None
    last_status = ""
    expected = _stack.expected_services()

    while True:
        status = None
        for attempt in (1, 2):  # file may be mid-write -- back off once and retry
            try:
                status = json.loads(status_file.read_text(encoding="utf-8-sig"))
                break
            except (OSError, json.JSONDecodeError) as e:
                if attempt == 1:
                    time.sleep(0.2)
                else:
                    print(f"[{ts()}] status file unreadable: {e}")

        if status:
            if status.get("status") == "up":
                ready_at = status.get("ready_at")
                missing = live_missing(args.stack, repo_root)
                if missing:
                    slot = _stack.slot_for_stack(args.stack)
                    wt = _stack.worktree_for_stack(args.stack)
                    print(f"[{ts()}] {args.stack}: STALE 'up' -- bring-up completed at {ready_at}, "
                          f"but {len(missing)}/{len(expected)} services are not running now: "
                          f"{', '.join(sorted(missing))}")
                    print("  The stack died since bring-up (engine restart, host reboot, manual down).")
                    print("  The status file records that bring-up FINISHED, not that the stack is UP.")
                    print(f"  Fix: uv run --no-project <plugin-root>/scripts/bring_up_story_stack.py "
                          f"--slot {slot if slot is not None else '<N>'} --project-name {args.stack} "
                          f"--worktree {wt if wt else '<worktree>'} --mode up")
                    print("  (that script passes ALL port env vars -- do NOT use a bare "
                          "`compose up`, which silently remaps onto the shared slot's ports)")
                    return 3
                if missing is None:
                    note = " -- liveness NOT confirmed (engine query failed)"
                elif not expected:
                    note = (" -- liveness NOT verified (config: stack.services is empty; "
                            "nothing to count)")
                else:
                    note = " -- liveness confirmed"
                print(f"[{ts()}] {args.stack}: up (ready_at={ready_at}){note}")
                return 0
            if status.get("status") == "failed":
                print(f"[{ts()}] {args.stack}: FAILED -- {status.get('error')}")
                tail(log_file)
                return 1
            retry_at = retry_started(status) or retry_at
            if status.get("status") != last_status:
                print(f"[{ts()}] {args.stack}: {status.get('status')}")
                last_status = status.get("status") or ""

        since_retry = ((datetime.now().astimezone() - retry_at).total_seconds()
                       if retry_at is not None else None)
        anchor = spent_anchor(time.monotonic() - started, since_retry, args.timeout_sec)
        if anchor:
            print(f"[{ts()}] {args.stack}: TIMEOUT {args.timeout_sec}s after {anchor} (last status={last_status})")
            tail(log_file)
            return 2

        print(f"[{ts()}] {args.stack}: still {last_status} ...")
        time.sleep(args.poll_interval_sec)


if __name__ == "__main__":
    sys.exit(main())
