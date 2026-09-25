#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Bring the shared dev stack back up and wait until it is usable.

For projects whose compose services use `restart: "no"` (so ephemeral slot
stacks do not resurrect after a host restart), the shared stack
(`config: slots.shared_stack_name`, slot `config: slots.shared_slot`) does not
come back by itself either. This script restarts it — shared stack only.

What it does:
  1. `<compose> -p <shared> start <config: stack.services>` — `start` (not `up`)
     never recreates a container, so it cannot remap ports. It still receives
     ALL port vars of the shared slot. One-shot init services must not be listed
     in `stack.services`; their `Exited (0)` is their healthy state.
  2. Polls until `config: stack.app_service` reports `running`, with a
     timestamped heartbeat.
  3. Prints the final per-service state so a partial recovery is visible.

What it does not do:
  - Slot stacks. Bring one back with bring_up_story_stack.py (`--mode up`).
  - Start `compose watch`. Watch does not survive a host restart either and is
    a foreground process — start it in the tree you are working in, or container
    code silently goes stale (verify with check_container_sync.py).

`config: stack.runtime = "none"` → prints SKIPPED and exits 0.

Usage:
    uv run --no-project .workflow/scripts/start_slot0.py [--timeout 300]

Exit codes: 0 all services running; 1 compose error; 2 timeout / partial.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _states(project: str) -> dict[str, str]:
    proc = subprocess.run(
        _stack.compose_argv(project, "ps", "--format", "{{.Service}}={{.State}}"),
        capture_output=True, text=True, errors="replace",
        env=wfconfig.compose_env(int(wfconfig.get("slots.shared_slot", 0))),
    )
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            svc, _, state = line.partition("=")
            out[svc.strip()] = state.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the shared dev stack.")
    ap.add_argument("--timeout", type=int, default=240,
                    help="seconds to wait for the app service to reach running (default 240)")
    args = ap.parse_args()

    if not wfconfig.stack_enabled():
        _stack.skipped("start shared stack", _stack.STACK_NONE_REASON)
        return 0

    project = str(wfconfig.get("slots.shared_stack_name", ""))
    slot = int(wfconfig.get("slots.shared_slot", 0))
    services = _stack.expected_services()
    app = _stack.app_service()
    if not project or not services:
        print(f"[{_stamp()}] config error: slots.shared_stack_name and stack.services must be set")
        return 1

    print(f"[{_stamp()}] starting shared slot {slot} ({project}) ...")
    proc = subprocess.run(
        _stack.compose_argv(project, "start", *services, with_file=True),
        capture_output=True, text=True, errors="replace",
        cwd=_stack.compose_dir(wfconfig.repo_root()), env=wfconfig.compose_env(slot),
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        print(f"[{_stamp()}] compose start failed (exit {proc.returncode})")
        return 1

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        states = _states(project)
        if states.get(app) == "running":
            break
        running = sum(1 for v in states.values() if v == "running")
        print(f"[{_stamp()}] waiting on {app} ... {running}/{len(services)} running")
        time.sleep(5)
    else:
        print(f"[{_stamp()}] TIMEOUT after {args.timeout}s waiting for {app}")
        for svc, state in sorted(_states(project).items()):
            print(f"  {svc:<10} {state}")
        return 2

    states = _states(project)
    for svc, state in sorted(states.items()):
        print(f"  {svc:<10} {state}")

    not_running = [s for s in services if states.get(s) != "running"]
    if not_running:
        print(f"[{_stamp()}] PARTIAL: not running -> {', '.join(not_running)}")
        return 2

    print(f"[{_stamp()}] shared slot up ({len(services)}/{len(services)} running)")
    print("NOTE: `compose watch` is NOT started by this script; "
          "start it in the tree you are working in, or container code goes stale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
