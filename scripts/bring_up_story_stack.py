#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Bring up / inspect / tear down an isolated slot stack with ALL port env vars.

One script instead of an env-var-modifying inline command: the slot's port vars
(`config: stack.ports` + slot * `config: stack.port_stride`, via
wfconfig.compose_env) are computed here and passed only to the compose child.
Never bring a slot stack up with a bare `compose up` — missing port vars fall
back to their defaults and collide with the shared slot.

Usage:
  uv run --no-project .workflow/scripts/bring_up_story_stack.py \
    --slot 1 --project-name story-7-28 --worktree <worktree path> \
    --mode up      # or watch | down | ps

Mode behaviors (run in <worktree>/<config: stack.compose_dir>):
  up    -> <compose> -p <name> [-f <compose_file>] up -d --build   (foreground)
  watch -> <compose> -p <name> [-f <compose_file>] watch           (foreground; caller backgrounds it)
  down  -> <compose> -p <name> down -v                             (foreground)
  ps    -> <compose> -p <name> ps                                  (foreground)

`config: stack.runtime = "none"` → prints a SKIPPED line and exits 0.

Exits with the compose exit code.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402

MODE_ARGS = {
    "up": (["up", "-d", "--build"], True),
    "watch": (["watch"], True),
    "down": (["down", "-v"], False),
    "ps": (["ps"], False),
}


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue] — keep progress ordered around child output
    ap = argparse.ArgumentParser(description="isolated slot stack compose wrapper")
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--project-name", required=True)
    ap.add_argument("--worktree", required=True,
                    help="the slot's worktree (compose runs in <worktree>/<stack.compose_dir>)")
    ap.add_argument("--mode", required=True, choices=sorted(MODE_ARGS))
    args = ap.parse_args()

    if not wfconfig.stack_enabled():
        _stack.skipped(f"compose {args.mode} for {args.project_name}", _stack.STACK_NONE_REASON)
        return 0

    mode_args, with_file = MODE_ARGS[args.mode]
    cmd = _stack.compose_argv(args.project_name, *mode_args, with_file=with_file)
    proc = subprocess.run(cmd, cwd=_stack.compose_dir(Path(args.worktree)),
                          env=wfconfig.compose_env(args.slot))
    if proc.returncode != 0:
        print(f"compose {args.mode} exited with code {proc.returncode}", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
