#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Background bring-up helper for story_setup.py's async flow.

Runs as a detached background process started by story_setup.py. Does the slow
work — image build, container start, readiness poll, compose-watch start — while
the orchestrator proceeds to Checkpoint 1 (spec review). Updates a per-stack
JSON status file that wait_for_stack_ready.py polls before the stack preflight.

Status file lifecycle:  starting -> up      (happy path)
                        starting -> failed  (any compose / readiness error, or a failed seed)
                        starting -> retrying -> up | failed   (a failed seed on a FRESH stack)

Seed (`config: stack.seed_cmd`, story stacks only; "" → none): after the ready poll and before
compose watch (watch's sync+restart would end an exec'd seed), the seed command runs in the
stack. Outcomes: "ok"; "absent" (output matches `config: stack.seed_absent_regex` — the tree
predates the seed); "failed: <detail>" (it ran and failed or timed out); "error: <detail>" (it
could not be launched — an OSError). A failed seed still starts watch, then marks the stack
`failed` ("ui seed failed ..."). An `error` outcome starts no watch and fails at once.

Fresh-stack retry: before the first `up` the helper records whether any engine volume carries
the label `com.docker.compose.project=<stack>` (none = fresh). A failed seed on a fresh stack is
retried once: status `retrying` (with `retry_started_at`), `compose down -v` (watch has not
started), the up + ready poll again, the seed again. A stack that is not fresh, or whose volume
probe could not answer, is never wiped.

JSON shape on disk (rewritten whole on every update; carries no extra keys):
  {"status": "starting"|"retrying"|"up"|"failed", "stack": ..., "started_at": ...,
   "updated_at": ..., "log_path": ...,
   "retry_started_at": ...   # only when status=retrying
   "ready_at": ...   # only when status=up
   "error": ...}     # only when status=failed

Readiness: every service in `config: stack.services` is `running`, and each one
listed in `config: stack.require_healthy` also reports health `healthy`
(`_stack.ready_services` — the one definition, shared with
wait_for_stack_ready.py). Only a listed long-running service exiting is a
failure; one-shot init services must NOT be listed in `stack.services`.

Created-but-not-started recovery: if services sit in `created` (their
`depends_on: condition: service_healthy` never fired before `up` returned), one
idempotent re-`up -d` is issued.

The compose-watch child (when `config: stack.watch` is true) runs with its CWD
inside the worktree and a command line containing `compose` and a bare `watch`
token — cleanup_story_stack.py and stack_preflight.py find it by exactly that.
A watch that exits within a few seconds of launch fails the bring-up.

Ports: `--slot N` → wfconfig.compose_env(N) (ALL port vars). `--ports K=V,...`
(optional) overrides individual values on top.

`config: stack.runtime = "none"` → status `up` is written immediately with a
SKIPPED log line; exit 0.

Run (invoked by story_setup.py; manual form):
  uv run --no-project .workflow/scripts/bring_up_stack_async.py --stack story-<id> \
    --worktree <path> --status-file <path> --log-file <path> --slot <N>

Exit codes: 0 stack up, 1 failed (status file says why).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402
from _stack import compose_ps, ready_services  # noqa: E402,F401  (re-exported for importers)

WATCH_SETTLE_S = 5
SEED_TIMEOUT_S = 240
SEED_FAILED = ("failed", "error")  # run_ui_seed outcomes that fail a bring-up
PROJECT_LABEL = "com.docker.compose.project"


def expected_services() -> set[str]:
    return set(_stack.expected_services())


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class Reporter:
    def __init__(self, stack: str, status_file: Path, log_file: Path):
        self.stack = stack
        self.status_file = status_file
        self.log_file = log_file
        self.started_at = now_iso()

    def write_status(self, status: str, err: str = "") -> None:
        obj: dict = {
            "status": status, "stack": self.stack,
            "started_at": self.started_at, "updated_at": now_iso(),
            "log_path": str(self.log_file),
        }
        if status == "retrying":
            obj["retry_started_at"] = now_iso()
        if status == "up":
            obj["ready_at"] = now_iso()
        if err:
            obj["error"] = err
        try:
            self.status_file.write_text(json.dumps(obj), encoding="utf-8")
        except OSError:
            pass

    def log(self, line: str) -> None:
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(f"[{now_iso()}] {line}\n")
        except OSError:
            pass

    def fail(self, msg: str) -> NoReturn:
        self.log(f"FAILED: {msg}")
        self.write_status("failed", msg)
        sys.exit(1)


def launch_watch(stack: str, cwd: Path, env: dict, log_file: Path) -> subprocess.Popen:
    """Start `compose watch` detached from this process. OS difference isolated here."""
    argv = _stack.compose_argv(stack, "watch", with_file=True)
    if os.name == "nt":
        # Own hidden console: with no console (DEVNULL stdin, DETACHED_PROCESS) the
        # Windows compose CLI exits immediately.
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return subprocess.Popen(argv, cwd=cwd, env=env,
                                creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)
    logfh = log_file.open("a", encoding="utf-8")
    return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=logfh, stderr=subprocess.STDOUT, start_new_session=True)


def is_story_stack(stack: str) -> bool:
    return stack.startswith(str(wfconfig.get("slots.stack_prefix", "story-")))


def run_ui_seed(stack: str, cwd, env: dict, timeout_sec: int = SEED_TIMEOUT_S, emit=None) -> str:
    """Run `config: stack.seed_cmd` for a story stack. Shared with bring_up_story_stack.py.

    Returns "skipped" (no seed configured, or not a story stack), "absent", "ok",
    "failed: <detail>" or "error: <detail>" (see the module docstring)."""
    tpl = str(wfconfig.get("stack.seed_cmd", "") or "").strip()
    if not tpl or not is_story_stack(stack):
        return "skipped"
    cmd = wfconfig.render(tpl, stack=stack, story_id=stack[len(str(wfconfig.get("slots.stack_prefix", "story-"))):])
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, errors="replace",
                              cwd=cwd, env=env, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return f"failed: timed out after {timeout_sec} s"
    except OSError as exc:
        return f"error: {exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    if emit is not None and output.strip():
        emit(output.rstrip())
    absent = str(wfconfig.get("stack.seed_absent_regex", "") or "")
    if absent and re.search(absent, output):
        return "absent"
    if proc.returncode == 0:
        return "ok"
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return f"failed: {lines[-1] if lines else f'exit {proc.returncode}'}"


def stack_is_fresh(stack: str, env: dict) -> bool:
    """True when the engine holds no volume of the stack's compose project. A probe that cannot
    answer counts as not fresh: only a project proven empty is ever wiped."""
    try:
        probe = subprocess.run([*shlex.split(_stack.engine_cmd()), "volume", "ls", "-q", "--filter",
                                f"label={PROJECT_LABEL}={stack}"],
                               capture_output=True, text=True, errors="replace", env=env, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and not probe.stdout.strip()


def bring_up(stack: str, cwd: Path, env: dict, rep: "Reporter", timeout_sec: int) -> None:
    """`compose up -d --build`, then the ready poll; fails the run on an exited service or the deadline."""
    expected = expected_services()
    poll_s = int(wfconfig.get("stack.ready_poll_s", 5))
    up_cmd = _stack.compose_argv(stack, "up", "-d", "--build", with_file=True)
    rep.log(" ".join(up_cmd))
    with rep.log_file.open("a", encoding="utf-8") as logfh:
        up = subprocess.run(up_cmd, stdout=logfh, stderr=subprocess.STDOUT, cwd=cwd, env=env)
    if up.returncode != 0:
        rep.log(f"'up' exited {up.returncode} -- proceeding to ps poll (dependency-health race recovery)")
    deadline = time.monotonic() + timeout_sec
    nudged = False
    while True:
        services = compose_ps(stack, cwd, env)
        ready = sorted(ready_services(services))
        exited = [s for s in services if s.get("State") in ("exited", "dead") and s.get("Service") in expected]
        created = [s for s in services if s.get("State") == "created"]
        missing = expected - set(ready)
        if not missing:
            break
        if exited:
            detail = ", ".join(f"{s.get('Service')}={s.get('State')}" for s in exited)
            rep.fail(f"container(s) exited during bring-up: {detail}")
        if created and not nudged:
            rep.log(f"nudging {len(created)} created-but-not-started service(s) with a re-up")
            with rep.log_file.open("a", encoding="utf-8") as logfh:
                subprocess.run(_stack.compose_argv(stack, "up", "-d", with_file=True),
                               stdout=logfh, stderr=subprocess.STDOUT, cwd=cwd, env=env)
            nudged = True
            continue
        if time.monotonic() > deadline:
            state = ", ".join(f"{s.get('Service')}={s.get('State')}" for s in services)
            rep.fail(f"stack did not reach all-ready within {timeout_sec}s. State: {state}")
        rep.log(f"waiting ({len(set(ready) & expected)}/{len(expected)} ready; missing: "
                f"{', '.join(sorted(missing))})")
        time.sleep(poll_s)
    rep.log(f"all {len(expected)} services ready")


def seed_with_retry(stack: str, cwd: Path, env: dict, rep: "Reporter", fresh: bool,
                    timeout_sec: int) -> tuple[str, bool]:
    """Run the seed; (status-file error or "", launch_error). A failed seed on a fresh stack is
    retried once on clean volumes; an `error` outcome is never retried."""
    outcome = run_ui_seed(stack, cwd, env, SEED_TIMEOUT_S, emit=rep.log)
    rep.log(f"ui seed: {outcome}")
    if not outcome.startswith(SEED_FAILED):
        return "", False
    launch_error = outcome.startswith("error")
    if not fresh or launch_error:
        return f"ui seed {outcome}", launch_error
    first = outcome.removeprefix("failed: ")
    rep.log("ui seed failed on a fresh stack: wiping it and retrying the bring-up and the seed once")
    rep.write_status("retrying")
    rep.log(f"{' '.join(_stack.compose_argv(stack, 'down', '-v'))}")
    with rep.log_file.open("a", encoding="utf-8") as logfh:
        wipe = subprocess.run(_stack.compose_argv(stack, "down", "-v"), stdout=logfh,
                              stderr=subprocess.STDOUT, cwd=cwd, env=env).returncode
    if wipe != 0:
        rep.fail(f"ui seed failed: {first}; the wipe before the retry (down -v) exited {wipe}")
    bring_up(stack, cwd, env, rep, timeout_sec)
    retry = run_ui_seed(stack, cwd, env, SEED_TIMEOUT_S, emit=rep.log)
    rep.log(f"ui seed after the wipe: {retry}")
    if not retry.startswith(SEED_FAILED):
        return "", False
    launch_error = retry.startswith("error")
    second = "the seed could not be launched" if launch_error else "failed again"
    return (f"ui seed failed on a fresh stack: {first}; {second} after one wipe and retry: "
            f"{retry.partition(': ')[2]}"), launch_error


def main() -> int:
    ap = argparse.ArgumentParser(description="detached slot-stack bring-up")
    ap.add_argument("--stack", required=True)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--status-file", required=True)
    ap.add_argument("--log-file", required=True)
    ap.add_argument("--slot", type=int, default=None,
                    help="slot number; ALL port vars come from wfconfig.compose_env(slot)")
    ap.add_argument("--ports", default="",
                    help="optional comma-separated KEY=VAL overrides applied on top")
    ap.add_argument("--stack-timeout-sec", type=int,
                    default=int(wfconfig.get("stack.bring_up_timeout_s", 300)))
    args = ap.parse_args()

    worktree = Path(args.worktree)
    rep = Reporter(args.stack, Path(args.status_file), Path(args.log_file))
    rep.write_status("starting")
    rep.log(f"background bring-up starting for {args.stack} in {worktree}")

    if not wfconfig.stack_enabled():
        rep.log(f"SKIPPED: stack bring-up — {_stack.STACK_NONE_REASON}")
        rep.write_status("up")
        return 0

    if args.slot is None and not args.ports:
        rep.fail("neither --slot nor --ports given — refusing to run compose without the port vars")
    env = wfconfig.compose_env(args.slot) if args.slot is not None else dict(os.environ)
    for kv in args.ports.split(","):
        if "=" in kv:
            k, _, v = kv.partition("=")
            env[k.strip()] = v.strip()

    expected = expected_services()
    if not expected:
        rep.fail("config: stack.services is empty — readiness cannot be judged; "
                 "list the long-running services that must be running")
    cwd = _stack.compose_dir(worktree)

    try:
        fresh = stack_is_fresh(args.stack, env)
        rep.log("stack is fresh: no engine volume of its compose project exists" if fresh
                else "stack is not fresh (or the volume probe could not answer): it is never wiped")
        bring_up(args.stack, cwd, env, rep, args.stack_timeout_sec)
        seed_error, launch_error = seed_with_retry(args.stack, cwd, env, rep, fresh, args.stack_timeout_sec)
        if launch_error:
            rep.fail(seed_error)

        if wfconfig.get("stack.watch", True):
            rep.log("starting compose watch (detached)")
            watch = launch_watch(args.stack, cwd, env, rep.log_file)
            time.sleep(WATCH_SETTLE_S)
            rc = watch.poll()
            if rc is not None:
                rep.fail(f"compose watch exited {rc} within {WATCH_SETTLE_S}s of launch — "
                         "edits would not sync; see the log")
            rep.log(f"compose watch running (pid {watch.pid})")
        else:
            rep.log("SKIPPED: compose watch — config: stack.watch = false")

        if seed_error:
            rep.fail(seed_error)
        rep.write_status("up")
        rep.log("bring-up complete -- stack ready")
        return 0
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - any unhandled error must land in the status file
        rep.fail(f"unhandled exception: {e!r}")


if __name__ == "__main__":
    sys.exit(main())
