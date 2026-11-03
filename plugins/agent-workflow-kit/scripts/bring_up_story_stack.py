#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Bring up / inspect / stop / tear down an isolated slot stack with ALL port env vars.

One script instead of an env-var-modifying inline command: the slot's port vars
(`config: stack.ports` + slot * `config: stack.port_stride`, via
wfconfig.compose_env) are computed here and passed only to the compose child.
Never bring a slot stack up with a bare `compose up` — missing port vars fall
back to their defaults and collide with the shared slot.

Usage:
  uv run --no-project <plugin-root>/scripts/bring_up_story_stack.py \
    --slot 1 --project-name story-7-28 --worktree <worktree path> \
    --mode up      # or watch | stop | down | ps
  uv run --no-project <plugin-root>/scripts/bring_up_story_stack.py --project-name story-7-28 --mode seed

Mode behaviors (run in <worktree>/<config: stack.compose_dir>):
  up    -> <compose> -p <name> [-f <compose_file>] up -d --build (foreground), then, on a zero
           exit, the seed (`config: stack.seed_cmd`, story stacks only); prints `ui seed: <outcome>`
  watch -> <compose> -p <name> [-f <compose_file>] watch   (foreground; run it in the background,
           then `wait_for_watch_ready.py <its output file>`)
  stop  -> <compose> -p <name> stop: containers, logs and data volumes stay, ports are released
           (a halted story keeps its stack this way)
  down  -> <compose> -p <name> down -v
  ps    -> <compose> -p <name> ps
  seed  -> no compose call: the seed in the running stack. Takes --project-name only: the slot and
           worktree come from the slot registry; --slot and --worktree are refused.
Every other mode requires --slot and --worktree. `stop`, `down` and `seed` are refused (exit 2,
before any compose call) for the shared stack (`config: slots.shared_stack_name`): the owner
stops or wipes it by hand.

Resting env: `config: stack.story_env` ({VAR = value}) is passed to every compose child of a story
stack, and `up` / `watch` append any missing `VAR=value` line to the `.env` beside the compose file
(append-only, existing keys untouched), so a later bare compose command in the worktree resolves
the same values.

`config: stack.runtime = "none"` → prints a SKIPPED line and exits 0.

Exits with the compose exit code; `up` with a zero compose exit and a failed seed exits 1; `seed`
exits 1 on a failed seed and 2 when the slot registry names no usable stack for --project-name.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402
from bring_up_stack_async import SEED_FAILED, run_ui_seed  # noqa: E402

MODE_ARGS = {
    "up": (["up", "-d", "--build"], True),
    "watch": (["watch"], True),
    "stop": (["stop"], False),
    "down": (["down", "-v"], False),
    "ps": (["ps"], False),
}
SEED_MODE = "seed"
SEEDING_MODES = ("up", "watch")
SHARED_REFUSED = ("stop", "down", SEED_MODE)


def shared_stack() -> str:
    return str(wfconfig.get("slots.shared_stack_name", wfconfig.get("project.name", "project")))


def story_env() -> dict[str, str]:
    return {str(k): str(v) for k, v in (wfconfig.get("stack.story_env", {}) or {}).items()}


def seed_env_file(env_file: Path, values: dict[str, str]) -> list[str]:
    """Append each `VAR=value` whose VAR the file does not set; return the keys added."""
    try:
        text = env_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    have = {ln.split("=", 1)[0].strip() for ln in text.splitlines() if "=" in ln and not ln.lstrip().startswith("#")}
    add = [k for k in values if k not in have]
    if add:
        sep = "" if not text or text.endswith("\n") else "\n"
        env_file.write_text(text + sep + "".join(f"{k}={values[k]}\n" for k in add), encoding="utf-8")
    return add


def report_ui_seed(project: str, cwd: Path, env: dict[str, str]) -> int:
    outcome = run_ui_seed(project, cwd, env, emit=print)
    if outcome.startswith(SEED_FAILED):
        print(f"ui seed: {outcome}", file=sys.stderr)
        return 1
    print(f"ui seed: {outcome}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue] — keep progress ordered around child output
    ap = argparse.ArgumentParser(description="isolated slot stack compose wrapper")
    ap.add_argument("--slot", type=int)
    ap.add_argument("--project-name", required=True)
    ap.add_argument("--worktree", help="the slot's worktree (compose runs in <worktree>/<stack.compose_dir>)")
    ap.add_argument("--mode", required=True, choices=sorted([*MODE_ARGS, SEED_MODE]))
    args = ap.parse_args(argv)

    given = {"--slot": args.slot, "--worktree": args.worktree}
    if args.mode == SEED_MODE:
        extra = [f for f, v in given.items() if v is not None]
        if extra:
            ap.error(f"--mode seed takes the slot and the worktree from the slot registry; drop {', '.join(extra)}")
    else:
        missing = [f for f, v in given.items() if v is None]
        if missing:
            ap.error(f"--mode {args.mode} requires {', '.join(missing)}")

    if args.mode in SHARED_REFUSED and args.project_name == shared_stack():
        print(f"bring_up_story_stack: refused: --mode {args.mode} on the shared stack {shared_stack()} is "
              f"run only by the owner, by hand", file=sys.stderr)
        return 2

    if not wfconfig.stack_enabled():
        _stack.skipped(f"compose {args.mode} for {args.project_name}", _stack.STACK_NONE_REASON)
        return 0

    slot, worktree = args.slot, Path(args.worktree) if args.worktree else None
    if args.mode == SEED_MODE:
        slot, worktree = _stack.slot_for_stack(args.project_name), _stack.worktree_for_stack(args.project_name)
        if slot is None or worktree is None or not worktree.is_dir():
            print(f"bring_up_story_stack: the slot registry names no usable stack for {args.project_name}",
                  file=sys.stderr)
            return 2
    assert worktree is not None and slot is not None
    cwd = _stack.compose_dir(worktree)
    env = wfconfig.compose_env(slot)
    is_story = args.project_name != shared_stack()
    values = story_env() if is_story else {}
    env.update(values)

    if args.mode == SEED_MODE:
        return report_ui_seed(args.project_name, cwd, env)
    if args.mode in SEEDING_MODES and values:
        added = seed_env_file(cwd / ".env", values)
        if added:
            print(f"bring_up_story_stack: {cwd / '.env'} now sets {', '.join(added)} for {args.project_name}")

    mode_args, with_file = MODE_ARGS[args.mode]
    proc = subprocess.run(_stack.compose_argv(args.project_name, *mode_args, with_file=with_file), cwd=cwd, env=env)
    if proc.returncode != 0:
        print(f"compose {args.mode} exited with code {proc.returncode}", file=sys.stderr)
        return proc.returncode
    if args.mode != "up":
        return 0
    return report_ui_seed(args.project_name, cwd, env)


if __name__ == "__main__":
    sys.exit(main())
