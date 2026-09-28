#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The full test gate: preflight + pre-phases + concurrent legs + post-phases + parse.

One host script, no sub-agents. Phases, in order:

  1. preflight   `.workflow/scripts/stack_preflight.py --project-name <stack> --worktree <wt>
                 [--expected-branch <b>]` when `config: stack.runtime` != "none"; skipped (and
                 announced) otherwise. A red preflight aborts: suite not run.
  2. pre-phases  `schema-drift` first — `config: tests.schema_drift_cmd`, run only when a file
                 changed against `config: git.base_branch` (committed or not) matches
                 `config: pipeline.schema_globs`; announced as SKIPPED otherwise. Then
                 `config: tests.full_pre_phases` ([{name, cmd}]), sequential; the first
                 non-zero exit aborts the gate with that exit code.
  3. legs        run CONCURRENTLY, each streamed into the raw log with a `[LABEL]` prefix;
                 one leg failing never stops the others:
                   FULL          `config: tests.full_cmd` (required)
                   UNIT-FRONTEND `config: tests.unit_frontend_cmd` (optional)
                   E2E           `config: tests.e2e_cmd` (optional)
  4. post-phases `config: tests.full_post_phases`, sequential, only after every leg
                 passed; a non-zero exit fails the gate even though every leg passed.
  5. parse       only the structured summary reaches stdout (plus one line per phase):
                 per-leg PASSED/FAILED, result lines matched by `config: tests.summary_regex`,
                 and the COMPLETE list of failing ids matched by `config: tests.fail_id_regex`.
                 The raw log goes to a temp file whose path is printed.

Command templates are rendered by wfconfig.render with {stack} {worktree} {slot} {story_id}
{branch} {compose} {repo} and every port var of the slot; they run through the shell with
cwd = the worktree and every slot port var (plus COMPOSE_PROJECT_NAME) in the environment.

Verdict rules (fail closed): a leg PASSES only when it exited 0 AND no failing id was
parsed from its output. A configured leg with no result (launch failure) is not green.
A leg with no summary line is reported "counts unavailable" — check the log.

ONE full suite runs on this host at a time. The whole run — preflight, every phase, the
parse — holds a host-global NamedMutex (`_named_mutex.py`, lock file
`<tempdir>/agent-workflow-full-suite.lock`), so a second session queues instead of colliding (two gates on one
host oversubscribe it and produce timing failures that read as code regressions). The
wait is announced every 30s. Exhausting the budget (`--lock-timeout-minutes`, default
`config: tests.full_lock_timeout_min`) FAILS the run — it never proceeds unlocked. A holder
that dies releases the lock (the OS drops it when the fd closes).

Usage:
  uv run --no-project .workflow/scripts/run_full_suite.py                  # shared stack, main checkout
  uv run --no-project .workflow/scripts/run_full_suite.py --story-id <id>  # the story's slot + worktree
      [--expected-branch <b>] [--lock-timeout-minutes N]

Exit codes:
  0  all green
  1  test failure, preflight failure, pre/post-phase failure, or gate lock not acquired
  2  configuration error (tests.full_cmd unset, or missing a `config: tests_policy.required_flags`
     entry) or story not resolvable from the slot registry
  n  a pre-phase's own non-zero exit code when it aborts the gate
"""
from __future__ import annotations

import argparse
import fnmatch
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import wfconfig  # noqa: E402
from _named_mutex import NamedMutex, lock_file_for  # noqa: E402
from run_scoped_tests import (  # noqa: E402
    fail_regex, missing_required_flags, parse_output, resolve_target, run_env, summary_regex,
)

# Host-global on purpose: the collision this prevents is host oversubscription across
# stacks and projects as much as same-stack state sharing. Tests override LOCK_DIR.
LOCK_DIR: Path | None = None
LOCK_NAME = "agent-workflow-full-suite"
DEFAULT_LOCK_BUDGET_MIN = 60
HEARTBEAT_SECONDS = 30.0

LEGS = (
    ("FULL", "tests.full_cmd"),
    ("UNIT-FRONTEND", "tests.unit_frontend_cmd"),
    ("E2E", "tests.e2e_cmd"),
)


def _hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


# --------------------------------------------------------------------------- lock

class LockTimeout(TimeoutError):
    """The gate lock could not be taken within the budget."""


def _lock_dir() -> Path:
    return LOCK_DIR if LOCK_DIR is not None else Path(tempfile.gettempdir())


def _lock_path() -> Path:
    return lock_file_for(LOCK_NAME, _lock_dir())


def _try_acquire(timeout_s: float) -> NamedMutex | None:
    """One NamedMutex acquire attempt bounded by `timeout_s`; the held mutex or None."""
    m = NamedMutex(LOCK_NAME, int(max(0.0, timeout_s) * 1000), lock_dir=_lock_dir())
    try:
        m.__enter__()
    except TimeoutError:
        return None
    return m


class GateLock:
    """The held gate lock; `release()` drops it."""

    def __init__(self, mutex: NamedMutex):
        self._mutex: NamedMutex | None = mutex

    def release(self) -> None:
        if self._mutex is not None:
            self._mutex.__exit__(None, None, None)
            self._mutex = None


def _acquire_suite_lock(budget_minutes: float,
                        heartbeat_seconds: float = HEARTBEAT_SECONDS) -> GateLock:
    """Take the host-global gate lock, announcing the wait every heartbeat.

    Each iteration is one NamedMutex acquire with a heartbeat-sized timeout, so a
    queued session prints progress instead of going silent. Raises LockTimeout on
    budget exhaustion; the caller must NOT fall through to running the suite.
    """
    held = _try_acquire(0)
    if held is not None:
        return GateLock(held)

    print(f"[{_hms()}] another full suite holds the gate lock — queueing "
          f"(budget {budget_minutes}m)", flush=True)
    print(f"           lock: {_lock_path()}", flush=True)
    start = time.monotonic()
    budget_s = budget_minutes * 60
    while True:
        remaining = budget_s - (time.monotonic() - start)
        if remaining <= 0:
            raise LockTimeout(str(_lock_path()))
        held = _try_acquire(min(heartbeat_seconds, remaining))
        if held is not None:
            print(f"[{_hms()}] gate lock acquired after "
                  f"{int(time.monotonic() - start)}s", flush=True)
            return GateLock(held)
        print(f"[{_hms()}] still waiting on the gate lock... "
              f"{int(time.monotonic() - start)}s elapsed", flush=True)


# --------------------------------------------------------------------------- running

def _run_sequential(label: str, cmd: str, cwd: str, env: dict, log_fh, log_lock) -> int:
    with log_lock:
        log_fh.write(f"\n----------------------------------------\n[{label}]\n$ {cmd}\n"
                     f"----------------------------------------\n")
        log_fh.flush()
    try:
        rc = subprocess.run(cmd, shell=True, stdout=log_fh, stderr=subprocess.STDOUT,
                            cwd=cwd, env=env).returncode
    except OSError as exc:
        with log_lock:
            log_fh.write(f"[{label}] launch failed: {exc}\n")
        rc = 1
    with log_lock:
        log_fh.write(f"\n[{label}] > exit {rc}\n")
        log_fh.flush()
    print(f"[{_hms()}] {label}: exit {rc}", flush=True)
    return rc


def run_legs(legs: list[tuple[str, str]], cwd: str, env: dict, log_fh, log_lock) -> dict:
    """Run every leg concurrently. Returns {label: {"exit": int|None, "text": str}}."""
    results: dict[str, dict] = {}
    procs: dict[str, subprocess.Popen] = {}
    buffers: dict[str, list[str]] = {}
    threads: dict[str, threading.Thread] = {}

    def _stream(label: str, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:  # always drain — never let a child block on a full pipe
            buffers[label].append(line)
            with log_lock:
                log_fh.write(f"[{label}] {line}")

    for label, cmd in legs:
        buffers[label] = []
        with log_lock:
            log_fh.write(f"[{label}] $ {cmd}\n")
        try:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env)
        except OSError as exc:
            results[label] = {"exit": None, "text": f"launch failed: {exc}"}
            continue
        procs[label] = p
        t = threading.Thread(target=_stream, args=(label, p), daemon=True)
        t.start()
        threads[label] = t

    for label, p in procs.items():
        p.wait()
        threads[label].join()
        if p.stdout is not None:
            p.stdout.close()
        results[label] = {"exit": p.returncode, "text": "".join(buffers[label])}
        print(f"[{_hms()}] {label}: exit {p.returncode}", flush=True)
    with log_lock:
        log_fh.flush()
    return results


# --------------------------------------------------------------------------- parse

def _fmt_wall(total_seconds: float | None) -> str:
    if total_seconds is None:
        return ""
    m, s = divmod(int(total_seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def summarize(expected: list[str], results: dict, *, phases_exit: int = 0,
              failed_phase: str = "", log_path: str = "", wall: float | None = None,
              log_tail: list[str] | None = None, fail_re=None, summary_re=None) -> tuple[int, str]:
    """(exit code, summary text). Fails closed on every leg that is not positively PASSED."""
    fail_re = fail_re or fail_regex()
    summary_re = summary_re or summary_regex()
    wall_s = _fmt_wall(wall)
    legs: dict[str, dict] = {}
    for label in expected:
        r = results.get(label)
        if r is None or r.get("exit") is None:
            legs[label] = {"state": "UNKNOWN", "exit": None, "parsed": None,
                           "note": (r or {}).get("text", "no result")}
            continue
        parsed = parse_output(r.get("text", ""), int(r["exit"]), fail_re, summary_re)
        passed = r["exit"] == 0 and not parsed["ids"]
        legs[label] = {"state": "PASSED" if passed else "FAILED", "exit": r["exit"], "parsed": parsed}

    all_legs = bool(expected) and all(v["state"] == "PASSED" for v in legs.values())
    green = all_legs and phases_exit == 0 and not failed_phase
    out: list[str] = []

    def _short(v: dict) -> str:
        if v["state"] == "PASSED":
            return "P"
        if v["state"] == "FAILED":
            return f"F(exit {v['exit']})"
        return "?"

    if green:
        out.append(f"all green{' in ' + wall_s if wall_s else ''}")
        for label in expected:
            summ = legs[label]["parsed"]["summary"]
            out.append(f"  {label}: {summ[-1].strip() if summ else 'PASSED (counts unavailable)'}")
        if log_path:
            out.append(f"  log: {log_path}")
        return 0, "\n".join(out)

    wall_note = f" · wall {wall_s}" if wall_s else ""
    if failed_phase and not results:
        out.append(f"failed — gate aborted in phase {failed_phase} (exit {phases_exit}) "
                   f"before the legs ran{wall_note}")
    elif all_legs:
        out.append(f"failed — all legs passed but the gate exited non-zero (exit {phases_exit}); "
                   f"phase {failed_phase or '?'} failed after the legs{wall_note}")
    else:
        out.append("failed — " + " · ".join(f"{lbl} {_short(legs[lbl])}" for lbl in expected) + wall_note)
    out.append("")
    for label in expected:
        v = legs[label]
        if v["state"] == "UNKNOWN":
            out.append(f"{label}: NO RESULT — {str(v['note'])[:200]}")
            continue
        p = v["parsed"]
        summ = p["summary"][-1].strip() if p["summary"] else "(no result line — counts unavailable)"
        out.append(f"{label}: {v['state']} (exit {v['exit']}) — {summ}")
        if p["ids"]:
            out.append(f"  failing ({len(p['ids'])}):")
            out += [f"    {tid}" for tid in p["ids"]]
        elif v["state"] == "FAILED":
            out.append("  non-zero exit with no failing id parsed — inspect the log")
    if log_tail:
        out.append("")
        out.append(f"Last {len(log_tail)} log lines:")
        out += [f"  | {ln}" for ln in log_tail]
    if log_path:
        out.append("")
        out.append(f"log: {log_path}")
    return 1, "\n".join(out)


# --------------------------------------------------------------------------- gate

def _configured_legs(ctx: dict) -> list[tuple[str, str]]:
    legs = []
    for label, key in LEGS:
        tpl = str(wfconfig.get(key, "") or "").strip()
        if tpl:
            legs.append((label, wfconfig.render(tpl, **ctx)))
    return legs


def _phases(key: str, ctx: dict) -> list[tuple[str, str]]:
    out = []
    for i, ph in enumerate(wfconfig.get(key, []) or []):
        if isinstance(ph, dict) and str(ph.get("cmd", "")).strip():
            out.append((str(ph.get("name") or f"phase-{i + 1}"), wfconfig.render(str(ph["cmd"]), **ctx)))
    return out


def _glob_match(path: str, pattern: str) -> bool:
    """fnmatch, plus a leading `**/` also matching at the tree root (`migrations/x.py`)."""
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))


def _changed_files(worktree: str, base: str) -> list[str]:
    """Files changed against `base`: committed on the branch plus uncommitted edits."""
    out: set[str] = set()
    for argv in (["diff", "--name-only", f"{base}...HEAD"], ["diff", "--name-only", "HEAD"]):
        r = subprocess.run(["git", "-C", worktree, *argv], capture_output=True, text=True,
                           errors="replace")
        out.update(ln.strip() for ln in r.stdout.splitlines() if ln.strip())
    return sorted(out)


def _schema_drift_phase(ctx: dict) -> list[tuple[str, str]]:
    """[("schema-drift", cmd)] when a schema file changed and the check is configured."""
    tpl = str(wfconfig.get("tests.schema_drift_cmd", "") or "").strip()
    if not tpl:
        print("schema-drift: SKIPPED — config: tests.schema_drift_cmd is unset")
        return []
    globs = [str(g) for g in (wfconfig.get("pipeline.schema_globs", []) or [])]
    if not globs:
        print("schema-drift: SKIPPED — config: pipeline.schema_globs is empty")
        return []
    base = str(wfconfig.get("git.base_branch", "development"))
    hits = [f for f in _changed_files(ctx["worktree"], base)
            if any(_glob_match(f, g) for g in globs)]
    if not hits:
        print(f"schema-drift: SKIPPED — no file matching config: pipeline.schema_globs "
              f"changed against {base}")
        return []
    print(f"schema-drift: {len(hits)} schema file(s) changed against {base} — running the check")
    return [("schema-drift", wfconfig.render(tpl, **ctx))]


def _preflight_launcher(pf: Path) -> list[str]:
    """The watch check needs psutil on Windows; uv supplies it without a host install."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--no-project", "--with", "psutil", str(pf)]
    return [sys.executable, str(pf)]


def _run_gate(ctx: dict, expected_branch: str, env: dict) -> int:
    worktree = ctx["worktree"]
    # ------------------------------------------------------------- preflight
    if wfconfig.stack_enabled():
        pf = SCRIPTS_DIR / "stack_preflight.py"
        if not pf.is_file():
            print(f"preflight FAILED — {pf} not found; suite not run.")
            return 1
        print("preflight...", flush=True)
        pf_cmd = [*_preflight_launcher(pf), "--project-name", str(ctx["stack"]), "--worktree", worktree]
        if expected_branch:
            pf_cmd += ["--expected-branch", expected_branch]
        if subprocess.run(pf_cmd, cwd=worktree, env=env).returncode != 0:
            print("preflight FAILED — suite not run.")
            return 1
    else:
        print("preflight skipped — stack.runtime = \"none\" (no isolated stack to check)")

    legs = _configured_legs(ctx)
    expected = [label for label, _ in legs]
    skipped = [label for label, key in LEGS if label not in expected]
    pre = _schema_drift_phase(ctx) + _phases("tests.full_pre_phases", ctx)
    post = _phases("tests.full_post_phases", ctx)
    if skipped:
        print(f"legs skipped (not configured): {', '.join(skipped)}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = Path(tempfile.gettempdir()) / f"full-suite-{ctx['stack']}-{stamp}.log"
    print(f"running {len(pre)} pre-phase(s), {len(legs)} concurrent leg(s), "
          f"{len(post)} post-phase(s)... raw log: {log}", flush=True)

    phases_exit, failed_phase = 0, ""
    results: dict = {}
    log_lock = threading.Lock()
    start = time.monotonic()
    with log.open("a", encoding="utf-8", errors="replace") as fh:
        for name, cmd in pre:
            rc = _run_sequential(name, cmd, worktree, env, fh, log_lock)
            if rc != 0:
                phases_exit, failed_phase = rc, name
                break
        if not failed_phase:
            results = run_legs(legs, worktree, env, fh, log_lock)
            legs_ok = all((results.get(lbl) or {}).get("exit") == 0 for lbl in expected)
            if legs_ok:
                for name, cmd in post:
                    rc = _run_sequential(name, cmd, worktree, env, fh, log_lock)
                    if rc != 0:
                        phases_exit, failed_phase = 1, name
                        break
    wall = time.monotonic() - start

    tail = None
    if failed_phase:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
    code, text = summarize(expected, results, phases_exit=phases_exit, failed_phase=failed_phase,
                           log_path=str(log), wall=wall, log_tail=tail)
    print()
    print(text, flush=True)
    if failed_phase and not results and phases_exit not in (0, 1):
        return phases_exit  # a pre-phase aborts with its own exit code
    return code


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description="preflight + gate phases + concurrent legs + parse")
    ap.add_argument("--story-id", default="")
    ap.add_argument("--expected-branch", default="",
                    help="override the branch stack_preflight expects (default: the slot "
                         "registry's Branch column for --story-id; none for the shared stack)")
    ap.add_argument("--lock-timeout-minutes", type=float,
                    default=float(wfconfig.get("tests.full_lock_timeout_min", DEFAULT_LOCK_BUDGET_MIN)),
                    help="how long to queue behind another full suite before failing")
    args = ap.parse_args(argv)

    if not str(wfconfig.get("tests.full_cmd", "") or "").strip():
        print("ERROR: tests.full_cmd is not set in .workflow/config.toml — nothing to run.")
        return 2
    ctx, err = resolve_target(args.story_id or None, None)
    if err:
        print(f"ERROR: {err}")
        return 2
    missing = missing_required_flags(wfconfig.render(str(wfconfig.get("tests.full_cmd")), **ctx))
    if missing:
        print(f"ERROR: tests.full_cmd lacks required flag(s) {', '.join(missing)} "
              f"(config: tests_policy.required_flags) — suite not run.")
        return 2
    expected_branch = args.expected_branch or (ctx.get("branch", "") if args.story_id else "")
    env = run_env(ctx)
    print(f"stack: {ctx['stack']}  cwd: {ctx['worktree']}")

    try:
        lock = _acquire_suite_lock(args.lock_timeout_minutes)
    except LockTimeout:
        # Fails CLOSED: running unlocked is the collision the lock exists to prevent.
        print(f"gate lock NOT acquired within {args.lock_timeout_minutes}m — suite not run.")
        print(f"  lock: {_lock_path()}")
        print("  another full suite is still holding it; retry, or raise --lock-timeout-minutes.")
        return 1

    try:
        return _run_gate(ctx, expected_branch, env)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
