#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Switch a gate stack's services between named serving profiles (`config: profiles`).

A serving profile is a set of settings the services read at start (a fake vs a real model, a test vs
a production-like backend). The gate's per-merge legs run under `config: profiles.gate`; the optional
profile leg (`config: profile_leg`, `run_full_suite.py --profile-leg`) runs under its own profile. A
story stack rests in `config: profiles.story_resting`, the shared stack in
`config: profiles.shared_resting` (each "" → the gate profile), so a gate switches a stack only when
its resting profile differs from the profile a leg needs. With `config: profiles.gate = ""` nothing
here runs: every profile step is SKIPPED (announced by the caller).

Profiles are `[profiles.defs.<name>]` tables:

    value     = "<value>"          # what `config: profiles.env_var` is set to for the services
    probe_cmd = "<template>"       # run once per service ({service} filled in); prints one line
                                   # `PROFILE {json}` describing the running settings
    expect    = { KEY = value }    # every key must be in that JSON with this value (strings compare
                                   # case-insensitively)

A container's environment is fixed at creation, so a switch recreates the services. `prepare()` runs
one switch end to end, each step printing a timestamped line with its cost and a progress line per poll:

  1. drain: poll `config: profiles.drain_cmd` until it exits 0 (no queued or running background work
     that would execute under the next profile); "" → no drain;
  2. recreate `config: profiles.services` (default `config: stack.services`) with the profile's value
     in `config: profiles.env_var`, every slot port var, `--build --force-recreate --no-deps`, from the
     stack's own tree;
  3. wait until those services are ready (`_stack.ready_services`);
  4. probe every service and compare with the profile's `expect`. A mismatch fails the leg that follows.

`ensure()` reads the running profile first and switches only on a mismatch.

CLI (manual use; the gate calls the functions in-process):
    uv run --no-project <plugin-root>/scripts/gate_profile.py status [--story-id <id>]
    uv run --no-project <plugin-root>/scripts/gate_profile.py ensure <profile> [--story-id <id>]
    uv run --no-project <plugin-root>/scripts/gate_profile.py rest [--story-id <id>]

Exit codes: 0 ok / in the profile, 1 not in the profile or the switch failed, 2 config or usage error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import wfconfig  # noqa: E402
import _stack  # noqa: E402

MARKER = "PROFILE"


class ProfileError(RuntimeError):
    """A profile name or stack that cannot be resolved from the config."""


@dataclass(frozen=True)
class Profile:
    name: str
    value: str
    probe_cmd: str
    expect: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Stack:
    name: str          # compose project
    slot: int
    worktree: str
    shared: bool       # the shared dev stack (no story)


def _cfg(key: str, default):
    return wfconfig.get(f"profiles.{key}", default)


def enabled() -> bool:
    return bool(str(_cfg("gate", "") or "").strip())


def load(name: str) -> Profile:
    defs = _cfg("defs", {}) or {}
    d = defs.get(name)
    if not isinstance(d, dict):
        raise ProfileError(f"profile {name!r} is not defined (config: profiles.defs.{name})")
    probe = str(d.get("probe_cmd", "") or "").strip()
    if not probe:
        raise ProfileError(f"profile {name!r} has no probe_cmd (config: profiles.defs.{name}.probe_cmd)")
    return Profile(name, str(d.get("value", "") or ""), probe, dict(d.get("expect", {}) or {}))


def gate_profile() -> Profile:
    return load(str(_cfg("gate", "")))


def resting_profile(stack: Stack) -> Profile:
    key = "shared_resting" if stack.shared else "story_resting"
    return load(str(_cfg(key, "") or _cfg("gate", "")))


def services() -> list[str]:
    return [str(s) for s in (_cfg("services", []) or [])] or _stack.expected_services()


def stack_from_ctx(ctx: dict) -> Stack:
    """The stack a run_scoped_tests.resolve_target context names."""
    return Stack(str(ctx["stack"]), int(ctx.get("slot", 0) or 0), str(ctx["worktree"]),
                 not ctx.get("story_id"))


# ------------------------------------------------------------------ plumbing (patched in tests)

def _run(cmd, *, env=None, cwd=None, timeout=None, shell=False):
    try:
        return subprocess.run(cmd, env=env, cwd=cwd, timeout=timeout, shell=shell,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {exc.timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _sleep(seconds):
    time.sleep(seconds)


def _monotonic():
    return time.monotonic()


def _say(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def compose_env(stack: Stack) -> dict[str, str]:
    """Every slot port var plus COMPOSE_PROJECT_NAME: a compose command never lands on another slot's ports."""
    env = wfconfig.compose_env(stack.slot)
    env["COMPOSE_PROJECT_NAME"] = stack.name
    return env


def switch_env(stack: Stack, profile: Profile) -> dict[str, str]:
    env = compose_env(stack)
    var = str(_cfg("env_var", "") or "")
    if var:
        env[var] = profile.value
    return env


def switch_cmd(stack: Stack) -> list[str]:
    return _stack.compose_argv(stack.name, "up", "-d", "--build", "--force-recreate", "--no-deps",
                               *services(), with_file=True)


def _render(template: str, stack: Stack, **extra) -> str:
    return wfconfig.render(template, stack=stack.name, slot=stack.slot, worktree=stack.worktree, **extra)


# ------------------------------------------------------------------ steps

def switch(stack: Stack, profile: Profile) -> int:
    result = _run(switch_cmd(stack), env=switch_env(stack, profile),
                  cwd=str(_stack.compose_dir(Path(stack.worktree))),
                  timeout=float(_cfg("switch_timeout_s", 1200)))
    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-15:])
        _say(f"recreate failed (exit {result.returncode}):\n{tail}")
    return result.returncode


def parse_marked(result, marker: str = MARKER):
    """The JSON object after `marker` in a probe's stdout, or None for a failed or garbled probe."""
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        if line.startswith(marker + " "):
            try:
                value = json.loads(line[len(marker) + 1:])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def drain(stack: Stack) -> bool:
    template = str(_cfg("drain_cmd", "") or "").strip()
    if not template:
        return True
    timeout_s, poll_s = float(_cfg("drain_timeout_s", 180)), float(_cfg("poll_s", 5))
    cmd = _render(template, stack)
    deadline = _monotonic() + timeout_s
    while True:
        result = _run(cmd, env=compose_env(stack), cwd=stack.worktree, shell=True,
                      timeout=float(_cfg("probe_timeout_s", 180)))
        if result.returncode == 0:
            return True
        if _monotonic() >= deadline:
            return False
        last = (result.stdout or result.stderr or "").strip().splitlines()[-1:] or [f"exit {result.returncode}"]
        _say(f"drain: waiting ({last[0][:160]})")
        _sleep(poll_s)


def wait_ready(stack: Stack) -> bool:
    timeout_s, poll_s = float(_cfg("ready_timeout_s", 480)), float(_cfg("poll_s", 5))
    wanted = set(services())
    deadline = _monotonic() + timeout_s
    while True:
        ready = _ready(stack)
        if wanted <= ready:
            return True
        if _monotonic() >= deadline:
            return False
        _say("ready: waiting for " + ", ".join(sorted(wanted - ready)))
        _sleep(poll_s)


def _ready(stack: Stack) -> set[str]:
    try:
        return _stack.ready_services(_stack.compose_ps(stack.name, Path(stack.worktree), compose_env(stack)))
    except (OSError, subprocess.SubprocessError):
        return set()


def _same(got, want) -> bool:
    if isinstance(got, str) and isinstance(want, str):
        return got.lower() == want.lower()
    return got == want


def problems_in(service: str, profile: Profile, reply) -> list[str]:
    if reply is None:
        return [f"{service}: the running profile could not be read (probe failed or printed no `{MARKER} {{json}}` line)"]
    return [f"{service}: {key} is {reply.get(key)!r}, the {profile.name} profile expects {want!r}"
            for key, want in profile.expect.items() if not _same(reply.get(key), want)]


def _probe(stack: Stack, profile: Profile, service: str):
    return _run(_render(profile.probe_cmd, stack, service=service), env=compose_env(stack),
                cwd=stack.worktree, shell=True, timeout=float(_cfg("probe_timeout_s", 180)))


def verify(stack: Stack, profile: Profile) -> list[str]:
    svcs = services()
    with ThreadPoolExecutor(max_workers=max(1, len(svcs))) as pool:
        futures = {s: pool.submit(_probe, stack, profile, s) for s in svcs}
        replies = {s: parse_marked(f.result()) for s, f in futures.items()}
    out: list[str] = []
    for s in svcs:
        out.extend(problems_in(s, profile, replies[s]))
    return out


def _timed(label: str, step):
    start = _monotonic()
    value = step()
    return value, f"{label} {round(_monotonic() - start)}s"


def prepare(stack: Stack, profile: Profile) -> tuple[bool, str]:
    """Put the stack in `profile`; (True, "") when the running services confirm it."""
    drained, cost = _timed("drain", lambda: drain(stack))
    _say(f"{cost}: {'drained' if drained else 'NOT drained'} before the switch to {profile.name}")
    if not drained:
        return False, f"drain timed out after {_cfg('drain_timeout_s', 180)}s; the profile was not switched"
    rc, cost = _timed("recreate", lambda: switch(stack, profile))
    _say(f"{cost}: {', '.join(services())} recreated in the {profile.name} profile (exit {rc})")
    if rc != 0:
        return False, f"recreate for the {profile.name} profile exited {rc}"
    ready, cost = _timed("ready", lambda: wait_ready(stack))
    _say(f"{cost}: services {'ready' if ready else 'NOT ready'}")
    if not ready:
        return False, f"services not ready within {_cfg('ready_timeout_s', 480)}s of the {profile.name} recreate"
    problems, cost = _timed("verify", lambda: verify(stack, profile))
    _say(f"{cost}: {profile.name} profile {'confirmed from the running services' if not problems else 'MISMATCH'}")
    if problems:
        return False, "; ".join(problems)
    return True, ""


def ensure(stack: Stack, profile: Profile) -> tuple[bool, str]:
    """Put the stack in `profile`, reading the running profile first; a stack already in it is left alone."""
    problems, cost = _timed("probe", lambda: verify(stack, profile))
    if not problems:
        _say(f"{cost}: the stack already runs the {profile.name} profile; no switch")
        return True, ""
    _say(f"{cost}: the stack is not in the {profile.name} profile ({'; '.join(problems)}); switching")
    return prepare(stack, profile)


# ------------------------------------------------------------------ CLI

def _stack_for(story_id: str | None) -> Stack:
    from run_scoped_tests import resolve_target
    ctx, err = resolve_target(story_id or None, None)
    if err:
        raise ProfileError(err)
    return stack_from_ctx(ctx)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "ensure", "rest"):
        p = sub.add_parser(name)
        if name == "ensure":
            p.add_argument("profile")
        p.add_argument("--story-id", default="")
    a = ap.parse_args(argv)
    if not enabled():
        print("profiles: SKIPPED — config: profiles.gate is empty (no serving profiles configured)")
        return 0 if a.cmd == "status" else 2
    try:
        stack = _stack_for(a.story_id)
        if a.cmd == "status":
            profile = resting_profile(stack)
            problems = verify(stack, profile)
            print(f"{stack.name}: {'in' if not problems else 'NOT in'} its resting profile {profile.name}"
                  + (f" ({'; '.join(problems)})" if problems else ""))
            return 0 if not problems else 1
        profile = load(a.profile) if a.cmd == "ensure" else resting_profile(stack)
    except ProfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    ok, why = ensure(stack, profile)
    if not ok:
        print(f"FAILED: {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
