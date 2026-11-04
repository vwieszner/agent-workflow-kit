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

ONE full suite runs per stack at a time (`config: tests.gate_lock_scope = "stack"`, the
default; "host" → one per host). The whole run — preflight, every phase, the parse — holds a
NamedMutex (`_named_mutex.py`, lock file `<tempdir>/agent-workflow-full-suite-<stack>.lock`),
so a second session queues instead of colliding. Waiters are served first-come-first-served
through a line file next to the lock (`<lock>.line.json`): a waiter refreshes its entry every
poll and an entry not refreshed for 120 s (or stamped that far ahead) is dead. The wait is
announced every 30 s. Exhausting the budget (`--lock-timeout-minutes`, default
`config: tests.full_lock_timeout_min`) FAILS the run — it never proceeds unlocked — and an
unreadable line file fails it the same way. A holder that dies releases the lock.

--record (with --story-id): the HEAD and the tested `code-tree` (run_scoped_tests.tested_tree)
and the merge-base with `config: git.base_branch` are fixed before the run; on an all-green
gate `pre-merge gate GREEN -- run_full_suite all legs (exit 0) HEAD <sha12> code-tree <sha12>
base <sha12>` is appended to the story's record. On ANY non-zero exit while the story holds the
land lease (land_lease.py, when installed) the run appends `checkpoint-3 approval withdrawn --
pre-merge gate exit <rc>` and releases the lease. A failing preflight is re-run every 15 s for
up to `config: tests.preflight_wait_s`; still failing, the gate ran no test: it prints
`pre-merge gate not run: preflight failed (...)`, exits 75, withdraws nothing, and releases a
lease the story holds (the approval stands). Without --record a failing preflight exits 1.

Serving profiles (`config: profiles`, gate_profile.py): with `config: profiles.gate` set, the legs
named in `config: profiles.gate_legs` ([] = every leg) run under that profile. gate_profile reads
the running profile first and switches only on a mismatch; a profile that cannot be entered fails
the legs that need it (NO RESULT) and nothing else. `--profile-leg` adds one more leg after the
concurrent ones, whatever they returned: `config: profile_leg.cmd` run alone under
`config: profile_leg.profile`. Afterwards — also after a crash — the stack is returned to its
resting profile (`profiles.story_resting` / `profiles.shared_resting`); a failed return fails the
gate (`RESTORE: FAILED`). A green `--profile-leg` run appends a line to the profile-leg state file
(profile_leg.py), which the land window's `profile_leg.py due` counts merges against. With
`profiles.gate` empty every profile step is SKIPPED (announced) and `--profile-leg` is a config error.

--hold-lock-for ACTION runs one fixed operation under the shared stack's gate lock instead of
the suite and exits with its code (1 when the lock is not taken):
  reset-test-dbs   `config: schema.test_db_reset_cmd` (drop reused test databases)
  land-merge       --story-id + --merge-summary: merge the story branch into the main
                   checkout (`config: git.merge_style`); refused unless the story holds the land
                   lease and the checkout is on `config: git.base_branch` with no merge,
                   cherry-pick or revert in progress. Exit 0 merged, 1 refused, 3 merge failed
                   and left no merge state, 4 merge failed and the abort failed (mid-merge).
A shared-stack gate refuses to run while the main checkout has MERGE_HEAD.

Usage:
  uv run --no-project <plugin-root>/scripts/run_full_suite.py                  # shared stack, main checkout
  uv run --no-project <plugin-root>/scripts/run_full_suite.py --story-id <id>  # the story's slot + worktree
      [--expected-branch <b>] [--lock-timeout-minutes N] [--record] [--profile-leg | --train]
  uv run --no-project <plugin-root>/scripts/run_full_suite.py --hold-lock-for land-merge --story-id <id> --merge-summary "<line>"

Exit codes:
  0  all green
  1  test failure, preflight failure, pre/post-phase failure, or gate lock not acquired
  75 a --record gate that ran no test (preflight still failing after the readiness wait)
  2  configuration error (tests.full_cmd unset, or missing a `config: tests_policy.required_flags`
     entry) or story not resolvable from the slot registry
  n  a pre-phase's own non-zero exit code when it aborts the gate
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import wfconfig  # noqa: E402
from _named_mutex import NamedMutex, lock_file_for  # noqa: E402
from run_scoped_tests import (  # noqa: E402
    fail_regex, missing_required_flags, parse_output, resolve_target, run_env, summary_regex,
)

# One gate lock per stack by default (`config: tests.gate_lock_scope`): a stack is the scope of
# the state two gates would share (reused test databases, seeded fixtures); gates on different
# stacks run concurrently. "host" makes it one lock per host. Tests override LOCK_DIR.
LOCK_DIR: Path | None = None
LOCK_NAME = "agent-workflow-full-suite"
DEFAULT_LOCK_BUDGET_MIN = 60
HEARTBEAT_SECONDS = 30.0
# The line's poll, and how long an entry may go unrefreshed before it is dead: long enough that
# a waiter slowed by host load keeps its place, short enough that a killed waiter blocks the line
# for at most two minutes.
POLL_SECONDS = 1.0
LINE_STALE_SECONDS = 120
LINE_LOCK_TIMEOUT_S = 2.0
LINE_ENTRY_KEYS = ("token", "joined", "seen")
PREFLIGHT_RETRY_SECONDS = 15
NOT_RUN_EXIT = 75  # a gate that ran no test
FAILING_CHECK = re.compile(r"^\[FAIL\]\s+(\S+)", re.MULTILINE)

LEGS = (
    ("FULL", "tests.full_cmd"),
    ("UNIT-FRONTEND", "tests.unit_frontend_cmd"),
    ("E2E", "tests.e2e_cmd"),
)


def _hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


# --------------------------------------------------------------------------- lock

class LockTimeout(TimeoutError):
    """The gate lock could not be taken within the budget (or the line file is unusable)."""

    def __init__(self, path: str, cause: object = None):
        super().__init__(path)
        self.cause = cause


class _LineBusy(Exception):
    """The line file's own lock stayed held past LINE_LOCK_TIMEOUT_S: the poll is skipped."""


def _lock_dir() -> Path:
    return LOCK_DIR if LOCK_DIR is not None else Path(tempfile.gettempdir())


def lock_name(stack: str | None = None) -> str:
    """The gate lock's name: per stack (`config: tests.gate_lock_scope = "stack"`, the default)
    or host-global ("host"). No stack → the host-global name."""
    if stack and str(wfconfig.get("tests.gate_lock_scope", "stack")) == "stack":
        return f"{LOCK_NAME}-{stack}"
    return LOCK_NAME


def _lock_path(stack: str | None = None) -> Path:
    return lock_file_for(lock_name(stack), _lock_dir())


def _line_path(stack: str | None = None) -> Path:
    return _lock_path(stack).with_suffix(".line.json")


def _try_acquire(name: str, timeout_s: float) -> NamedMutex | None:
    """One NamedMutex acquire attempt bounded by `timeout_s`; the held mutex or None."""
    m = NamedMutex(name, int(max(0.0, timeout_s) * 1000), lock_dir=_lock_dir())
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


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(text: str) -> datetime:
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("no timezone")
    return stamp


def _entry_alive(entry: dict) -> bool:
    """An entry is alive while its `seen` is within LINE_STALE_SECONDS of now on either side
    (a stamp far in the future means the clock stepped back: dead, its owner re-queues)."""
    age = (datetime.now(timezone.utc) - _parse_stamp(entry["seen"])).total_seconds()
    return abs(age) <= LINE_STALE_SECONDS


@contextlib.contextmanager
def _line_guard(stack: str | None):
    """Hold the line file's own lock for one access, never across a wait."""
    try:
        m = NamedMutex(lock_name(stack) + ".line", int(LINE_LOCK_TIMEOUT_S * 1000), lock_dir=_lock_dir())
        m.__enter__()
    except TimeoutError:
        raise _LineBusy from None
    except OSError as exc:
        raise LockTimeout(str(_line_path(stack)), exc) from exc
    try:
        yield
    finally:
        m.__exit__(None, None, None)


def _read_line(stack: str | None) -> list[dict]:
    """The stack's line in arrival order; no file is an empty line. Raises LockTimeout
    (with a cause) when the file is unreadable or malformed — the run then fails closed."""
    path = _line_path(stack)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LockTimeout(str(path), exc) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LockTimeout(str(path), f"not valid JSON: {exc}") from exc
    line = data.get("line") if isinstance(data, dict) else None
    if not (isinstance(line, list) and all(
            isinstance(e, dict) and all(isinstance(e.get(k), str) and e[k] for k in LINE_ENTRY_KEYS)
            for e in line)):
        raise LockTimeout(str(path), f"malformed (expected {{\"line\": [{{token, joined, seen}}]}}): {raw[:200]!r}")
    entries = [{k: e[k] for k in LINE_ENTRY_KEYS} for e in line]
    for i, e in enumerate(entries):
        for field in ("joined", "seen"):
            try:
                _parse_stamp(e[field])
            except ValueError as exc:
                raise LockTimeout(str(path), f"entry {i} has an unusable `{field}` stamp {e[field]!r}: {exc}") from exc
    return entries


def _write_line(stack: str | None, line: list[dict]) -> None:
    """Replace the line file atomically; remove it when the line is empty."""
    path = _line_path(stack)
    try:
        if not line:
            path.unlink(missing_ok=True)
            return
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gate-line-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps({"line": line}) + "\n")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
    except OSError as exc:
        raise LockTimeout(str(path), exc) from exc


def _take_turn(stack: str | None, token: str) -> tuple[NamedMutex | None, int | None]:
    """One poll of the turn rule, under the line lock: (the held gate lock or None, the
    position when this run joined the line now). Dead entries are dropped. The gate lock is
    tried only when the line is empty or this run heads it, so a run never overtakes a live
    waiter ahead of it; on success its entry leaves in the same locked access."""
    with _line_guard(stack):
        line = [e for e in _read_line(stack) if _entry_alive(e)]
        mine = next((e for e in line if e["token"] == token), None)
        if not line or line[0]["token"] == token:
            held = _try_acquire(lock_name(stack), 0)
            if held is not None:
                try:
                    if mine:
                        _write_line(stack, [e for e in line if e is not mine])
                except BaseException:
                    held.__exit__(None, None, None)
                    raise
                return held, None
        now = _utc_stamp()
        position = None
        if mine:
            mine["seen"] = now
        else:
            line.append({"token": token, "joined": now, "seen": now})
            position = len(line)
        _write_line(stack, line)
        return None, position


def _leave_line(stack: str | None, token: str) -> None:
    try:
        with _line_guard(stack):
            line = _read_line(stack)
            if any(e["token"] == token for e in line):
                _write_line(stack, [e for e in line if e["token"] != token and _entry_alive(e)])
    except _LineBusy:
        print(f"WARNING: this run's place in the gate line was not removed (the line file stayed locked); "
              f"it is dropped once it has gone {LINE_STALE_SECONDS}s without a refresh.", flush=True)
    except LockTimeout as exc:
        print(f"WARNING: this run's place in the gate line was not removed ({exc.cause}); it stays until "
              f"the line file is repaired or deleted.", flush=True)


def _acquire_suite_lock(budget_minutes: float, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                        stack: str | None = None) -> GateLock:
    """Take the gate lock in the stack's first-come-first-served line, announcing the wait.

    Waiters poll every POLL_SECONDS and refresh their line entry; a heartbeat line prints every
    `heartbeat_seconds`. Raises LockTimeout on budget exhaustion or an unusable line file; the
    caller must NOT fall through to running the suite.
    """
    token = f"{os.getpid()}-{time.time_ns()}"
    start = time.monotonic()
    budget_s = budget_minutes * 60
    last_beat = start
    joined = acquired = False
    try:
        while True:
            try:
                held, position = _take_turn(stack, token)
            except _LineBusy:
                held, position = None, None
            if held is not None:
                acquired = True
                if joined:
                    print(f"[{_hms()}] gate lock acquired after {int(time.monotonic() - start)}s", flush=True)
                return GateLock(held)
            if position is not None:
                joined = True
                print(f"[{_hms()}] another full suite holds the gate lock{' on ' + stack if stack else ''} "
                      f"— queueing (position {position}, budget {budget_minutes}m)", flush=True)
                print(f"           lock: {_lock_path(stack)}", flush=True)
            remaining = budget_s - (time.monotonic() - start)
            if remaining <= 0:
                raise LockTimeout(str(_lock_path(stack)))
            if time.monotonic() - last_beat >= heartbeat_seconds:
                last_beat = time.monotonic()
                print(f"[{_hms()}] still waiting on the gate lock... {int(last_beat - start)}s elapsed", flush=True)
            time.sleep(min(POLL_SECONDS, remaining))
    finally:
        if joined and not acquired:
            _leave_line(stack, token)


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
              log_tail: list[str] | None = None, fail_re=None, summary_re=None,
              restore_failure: str = "") -> tuple[int, str]:
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
    green = all_legs and phases_exit == 0 and not failed_phase and not restore_failure
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
    elif all_legs and restore_failure and not failed_phase:
        out.append(f"failed — all legs passed but RESTORE: FAILED ({restore_failure}){wall_note}")
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
    if restore_failure:
        out.append(f"RESTORE: FAILED ({restore_failure})")
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


def _run_preflight(cmd: list[str], cwd: str, env: dict, wait_seconds: float) -> int:
    """stack_preflight.py's exit code, re-run every PREFLIGHT_RETRY_SECONDS while it fails until
    `wait_seconds` have passed since its first failure (0: one run, no retry). Its output is
    printed in full for the first, the last and a passing run; a retry in between prints one
    `preflight not ready:` line naming the failing checks."""
    first_failed, runs = None, 0
    while True:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace")
        runs += 1
        output = (proc.stdout or "").rstrip()
        left = 0.0
        if proc.returncode != 0:
            first_failed = time.monotonic() if first_failed is None else first_failed
            left = wait_seconds - (time.monotonic() - first_failed)
        if output and (proc.returncode == 0 or runs == 1 or left <= 0):
            print(output, flush=True)
        if proc.returncode == 0 or left <= 0:
            return proc.returncode
        delay = min(PREFLIGHT_RETRY_SECONDS, left)
        names = ", ".join(dict.fromkeys(FAILING_CHECK.findall(output))) or "unnamed check"
        print(f"[{_hms()}] preflight not ready: {names}; retrying in {delay:g} s "
              f"({left:.0f} s of {wait_seconds:g} s left)", flush=True)
        time.sleep(delay)


def _gate_profile_module():
    """gate_profile when serving profiles are configured (`config: profiles.gate`), else None."""
    try:
        import gate_profile  # type: ignore[import-not-found]
    except ImportError:
        return None
    return gate_profile if gate_profile.enabled() else None


def _run_profiled(ctx: dict, legs: list[tuple[str, str]], profile_leg: bool, env: dict, log_fh,
                  log_lock) -> tuple[dict, str]:
    """Run the concurrent legs (the gate-profile legs under `config: profiles.gate`), then the
    profile leg alone when asked for, then return the stack to its resting profile.
    Returns (results, restore failure or "")."""
    gp = _gate_profile_module()
    results: dict = {}
    if gp is None:
        print("profiles: SKIPPED — config: profiles.gate is empty (no serving-profile switch)", flush=True)
        return run_legs(legs, ctx["worktree"], env, log_fh, log_lock), ""
    stack = gp.stack_from_ctx(ctx)
    in_profile = None
    restore_failure = ""
    ran_to_completion = False
    try:
        try:
            target = gp.gate_profile()
            entered, why = gp.ensure(stack, target)
        except gp.ProfileError as exc:
            target, entered, why = None, False, str(exc)
        in_profile = target if entered else None
        run_now = legs
        if not entered:
            needs = {str(x) for x in (wfconfig.get("profiles.gate_legs", []) or [])} or {lbl for lbl, _ in legs}
            for label, _ in legs:
                if label in needs:
                    results[label] = {"exit": None, "text": f"profile not entered: {why}"}
            run_now = [(lbl, cmd) for lbl, cmd in legs if lbl not in needs]
        results.update(run_legs(run_now, ctx["worktree"], env, log_fh, log_lock))
        if profile_leg:
            import profile_leg as pl
            label = pl.label()
            try:
                target = gp.load(str(wfconfig.get("profile_leg.profile", "")))
                entered, why = gp.ensure(stack, target)
            except gp.ProfileError as exc:
                target, entered, why = None, False, str(exc)
            in_profile = target if entered else None
            if entered:
                results.update(run_legs([(label, pl.command(ctx))], ctx["worktree"], env, log_fh, log_lock))
            else:
                results[label] = {"exit": None, "text": f"profile not entered: {why}"}
        ran_to_completion = True
    finally:
        # Every stack ends in its resting profile, also after a crash or a failed switch.
        try:
            resting = gp.resting_profile(stack)
        except gp.ProfileError as exc:
            resting, restore_failure = None, f"resting profile not resolvable: {exc}"
        if resting is not None and (in_profile is None or in_profile.name != resting.name):
            restored, why = gp.ensure(stack, resting)
            if not restored:
                restore_failure = f"stack not returned to its resting profile ({resting.name}): {why}"
        if restore_failure and not ran_to_completion:
            print(f"RESTORE: FAILED ({restore_failure})", flush=True)
    return results, restore_failure


def _run_gate(ctx: dict, expected_branch: str, env: dict, not_run: list[int] | None = None,
              profile_leg: bool = False) -> int:
    """Run the gate. `not_run` (a `--record` gate) receives a preflight exit code that still
    failed after the readiness wait (`config: tests.preflight_wait_s`); the caller reports
    that as a gate that ran no test."""
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
        wait_s = float(wfconfig.get("tests.preflight_wait_s", 300)) if not_run is not None else 0.0
        rc = _run_preflight(pf_cmd, worktree, env, wait_s)
        if rc != 0:
            if not_run is None:
                print("preflight FAILED — suite not run.")
            else:
                not_run.append(rc)
            return 1
    else:
        print("preflight skipped — stack.runtime = \"none\" (no isolated stack to check)")

    legs = _configured_legs(ctx)
    expected = [label for label, _ in legs]
    if profile_leg:
        import profile_leg as pl
        expected.append(pl.label())
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
    restore_failure = ""
    log_lock = threading.Lock()
    start = time.monotonic()
    with log.open("a", encoding="utf-8", errors="replace") as fh:
        for name, cmd in pre:
            rc = _run_sequential(name, cmd, worktree, env, fh, log_lock)
            if rc != 0:
                phases_exit, failed_phase = rc, name
                break
        if not failed_phase:
            results, restore_failure = _run_profiled(ctx, legs, profile_leg, env, fh, log_lock)
            legs_ok = all((results.get(lbl) or {}).get("exit") == 0 for lbl in expected)
            if legs_ok and not restore_failure:
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
                           log_path=str(log), wall=wall, log_tail=tail, restore_failure=restore_failure)
    print()
    print(text, flush=True)
    if failed_phase and not results and phases_exit not in (0, 1):
        return phases_exit  # a pre-phase aborts with its own exit code
    return code


# --------------------------------------------------------------------------- record / lease

def _git_out(worktree: str, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True, errors="replace")
    return r.returncode, r.stdout.strip()


def base_of(worktree: str) -> str:
    """The merge-base of HEAD with `config: git.base_branch` — the base the gate tested on.
    Raises RuntimeError when it cannot be determined."""
    base = str(wfconfig.get("git.base_branch", "development"))
    rc, out = _git_out(worktree, "merge-base", "HEAD", base)
    if rc != 0 or not out:
        raise RuntimeError(f"git merge-base HEAD {base} failed in {worktree}")
    return out


def _land_lease():
    """The land_lease module when the kit ships it, else None (lease steps are then skipped)."""
    try:
        import land_lease  # type: ignore[import-not-found]
    except ImportError:
        return None
    return land_lease


def _lease_holder_is(ll, story_id: str) -> bool:
    lease = ll.read(ll.default_path())
    return lease is not None and ll.dashed(lease["story"]) == ll.dashed(story_id)


def _withdraw_if_holding(story_id: str, rc: int, head: str | None) -> None:
    """A non-zero `--record` run whose story holds the land lease: withdraw its checkpoint-3
    approval and release the lease. A story not holding the lease records nothing; no
    land_lease module → nothing to withdraw (announced)."""
    ll = _land_lease()
    if ll is None:
        print("land lease: SKIPPED — land_lease.py is not installed; no approval withdrawn", flush=True)
        return
    try:
        if not _lease_holder_is(ll, story_id):
            return
        from story_record import cmd_append
        try:
            cmd_append(story_id, f"checkpoint-3 approval withdrawn -- pre-merge gate exit {rc}"
                                 + (f" HEAD {head}" if head else ""))
            print(f"checkpoint-3 approval for {story_id} withdrawn (pre-merge gate exit {rc}).", flush=True)
        except OSError as exc:
            print(f"WARNING: the gate exited {rc} but the checkpoint-3 approval for {story_id} was NOT "
                  f"withdrawn: {exc}", flush=True)
        ll.release(ll.default_path(), story_id)
        print(f"land lease released by {story_id}.", flush=True)
    except (getattr(ll, "LeaseError", OSError), OSError) as exc:
        print(f"WARNING: the gate exited {rc} but the land lease/approval for {story_id} could not be "
              f"updated: {exc}", flush=True)


def _preflight_not_run(story_id: str, rc: int) -> int:
    """A `--record` gate whose preflight still failed after the readiness wait ran no test:
    exit NOT_RUN_EXIT, nothing recorded or withdrawn; the land lease the story holds is
    released (its checkpoint-3 approval stands)."""
    print(f"pre-merge gate not run: preflight failed (stack_preflight.py exit {rc})", flush=True)
    ll = _land_lease()
    if ll is None:
        return NOT_RUN_EXIT
    try:
        if _lease_holder_is(ll, story_id):
            ll.release(ll.default_path(), story_id)
            print(f"land lease released by {story_id}; the checkpoint-3 approval stands.", flush=True)
    except (getattr(ll, "LeaseError", OSError), OSError) as exc:
        print(f"WARNING: the gate did not run and the land lease held by {story_id} could not be read or "
              f"released: {exc}; run `land_lease.py holds {story_id}` (exit 0: `land_lease.py release "
              f"{story_id}`; exit 1: nothing to do; exit 2: halt with its stderr line)", flush=True)
    return NOT_RUN_EXIT


# --------------------------------------------------------------------------- locked actions

def _merge_train():
    """The merge_train module when the kit ships it, else None (train steps are then skipped)."""
    try:
        import merge_train  # type: ignore[import-not-found]
    except ImportError:
        return None
    return merge_train


def _withdraw_train(story_id: str, rc: int, head: str | None) -> None:
    """A red train gate: withdraw the checkpoint-3 approval and remove the story from the line."""
    mt, ll = _merge_train(), _land_lease()
    try:
        mt.withdraw(story_id, rc, head, ll.default_path())
    except (mt.TrainError, ll.LeaseError, OSError) as exc:
        print(f"WARNING: the train gate exited {rc} but the land lease/approval for {story_id} could not be "
              f"updated: {exc}", flush=True)


def _gate_heartbeat(args: argparse.Namespace):
    """Keep a recorded gate alive in the lease record for the whole run: a train gate's line entry and the
    `gating` stamp of a train or of the holder's window gate. Without merge_train, or with a lease record that
    cannot be resolved here, nothing beats (the train checks report the latter as `train gate not run`)."""
    mt, ll = _merge_train(), _land_lease()
    if not (args.story_id and args.record) or mt is None or ll is None:
        return contextlib.nullcontext()
    try:
        return mt.heartbeat(args.story_id, ll.default_path())
    except (ll.LeaseError, OSError):
        return contextlib.nullcontext()


def _land_merge(story_id: str, branch: str, summary: str) -> int:
    """Merge the story branch into the main checkout under the shared stack's gate lock (the
    base branch is the shared stack's source, so the merge waits while its gate runs). Exit:
    0 merged; 1 nothing attempted (story does not hold the land lease, lease unreadable, main
    checkout off the base branch, merge/cherry-pick/revert in progress); 3 the merge failed
    and left no merge state; 4 the merge failed and `git merge --abort` failed (mid-merge)."""
    ll = _land_lease()
    if ll is None:
        print("land-merge refused: land_lease.py is not installed")
        return 1
    try:
        if not _lease_holder_is(ll, story_id):
            print(f"land-merge refused: {story_id} does not hold the land lease")
            return 1
    except (getattr(ll, "LeaseError", OSError), OSError) as exc:
        print(f"land-merge refused: the land lease record cannot be read: {exc}")
        return 1
    repo = str(wfconfig.repo_root())
    base = str(wfconfig.get("git.base_branch", "development"))
    rc, current = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or current != base:
        print(f"land-merge refused: the main checkout is on '{current or '?'}', not {base}")
        return 1
    for head in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if _git_out(repo, "rev-parse", "-q", "--verify", head)[0] == 0:
            print(f"land-merge refused: {head} present in {repo} (an operation is in progress)")
            return 1
    style = str(wfconfig.get("git.merge_style", "--no-ff"))
    rc = subprocess.run(["git", "-C", repo, "merge", style, branch, "-m", f"Merge {branch}: {summary}"]).returncode
    if rc == 0:
        return 0
    if _git_out(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
        if subprocess.run(["git", "-C", repo, "merge", "--abort"]).returncode != 0:
            print("land-merge: git merge --abort failed — the main checkout is mid-merge; resolve it by hand")
            return 4
    print(f"land-merge: the merge exited {rc} and left no merge state; git's output above names the cause.")
    return 3


def _reset_test_dbs() -> int:
    """Run `config: schema.test_db_reset_cmd` (drop reused test databases)."""
    tpl = str(wfconfig.get("schema.test_db_reset_cmd", "") or "").strip()
    if not tpl:
        print("reset-test-dbs: config: schema.test_db_reset_cmd is unset — nothing to run")
        return 2
    shared = str(wfconfig.get("slots.shared_stack_name", wfconfig.get("project.name", "project")))
    return subprocess.run(wfconfig.render(tpl, stack=shared, slot=int(wfconfig.get("slots.shared_slot", 0))),
                          shell=True, cwd=str(wfconfig.repo_root())).returncode


# Fixed operations only: an allow-listed run_full_suite.py must not carry an arbitrary command.
LOCKED_ACTIONS = {"reset-test-dbs": _reset_test_dbs, "land-merge": _land_merge}
ACTION_ARGS = {"reset-test-dbs": (), "land-merge": ("--story-id", "--merge-summary")}


def _locked_action_main(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`--hold-lock-for`: run the named action under the shared stack's gate lock."""
    action = args.hold_lock_for
    takes = ACTION_ARGS[action]
    for flag, value in (("--story-id", args.story_id or None), ("--merge-summary", args.merge_summary)):
        if flag in takes and value is None:
            ap.error(f"--hold-lock-for {action} needs {flag}")
        if flag not in takes and value is not None:
            ap.error(f"--hold-lock-for {action} does not take {flag}")
    if args.record or args.expected_branch:
        ap.error(f"--hold-lock-for {action} does not take --record or --expected-branch")
    call = LOCKED_ACTIONS[action]
    if action == "land-merge":
        summary = args.merge_summary
        if not summary.strip() or "\n" in summary or "\r" in summary:
            ap.error("--merge-summary must be one non-empty line")
        ctx, err = resolve_target(args.story_id, None)
        if err:
            print(f"ERROR: {err}")
            return 2
        call = lambda: _land_merge(args.story_id, str(ctx["branch"]), summary)  # noqa: E731
    shared = str(wfconfig.get("slots.shared_stack_name", wfconfig.get("project.name", "project")))
    try:
        lock = _acquire_suite_lock(args.lock_timeout_minutes, stack=shared)
    except LockTimeout as exc:
        print(f"gate lock NOT acquired within {args.lock_timeout_minutes}m — {action} not run.")
        print(f"  lock: {_lock_path(shared)}")
        if exc.cause:
            print(f"  the gate line file cannot be used: {exc.cause}")
        return 1
    try:
        return call()
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description="preflight + gate phases + concurrent legs + parse",
                                 allow_abbrev=False)
    ap.add_argument("--story-id", default="")
    ap.add_argument("--expected-branch", default="",
                    help="override the branch stack_preflight expects (default: the slot "
                         "registry's Branch column for --story-id; none for the shared stack)")
    ap.add_argument("--lock-timeout-minutes", type=float,
                    default=float(wfconfig.get("tests.full_lock_timeout_min", DEFAULT_LOCK_BUDGET_MIN)),
                    help="how long to queue behind another full suite before failing")
    ap.add_argument("--record", action="store_true",
                    help="with --story-id: on an all-green gate append `pre-merge gate GREEN` (HEAD, "
                         "code-tree, base) to the story's record; on any non-zero exit while the story "
                         "holds the land lease, withdraw its checkpoint-3 approval and release the lease; "
                         "a preflight still failing after the readiness wait ran no test: exit 75")
    ap.add_argument("--profile-leg", action="store_true",
                    help="also run the profile leg (config: profile_leg) alone under its serving profile after "
                         "the concurrent legs; a green run is appended to the profile-leg state file "
                         "(profile_leg.py due)")
    ap.add_argument("--train", action="store_true",
                    help="with --story-id and --record: gate the story's train branch while it waits in the "
                         "land lease's line (merge_train.py); exit 75 with `train gate not run: <reason>` when "
                         "the train checks fail; a red gate withdraws the approval and leaves the line")
    ap.add_argument("--hold-lock-for", choices=sorted(LOCKED_ACTIONS), metavar="ACTION",
                    help=f"run one named operation under the shared stack's gate lock instead of the "
                         f"suite; one of {sorted(LOCKED_ACTIONS)}")
    ap.add_argument("--merge-summary", default=None,
                    help="with --hold-lock-for land-merge: the one-line summary in the merge message")
    args = ap.parse_args(argv)
    if args.merge_summary is not None and not args.hold_lock_for:
        ap.error("--merge-summary needs --hold-lock-for land-merge")
    if args.hold_lock_for:
        return _locked_action_main(ap, args)
    if args.record and not args.story_id:
        print("ERROR: --record needs --story-id")
        return 2
    if args.train and not (args.story_id and args.record):
        ap.error("--train needs --story-id and --record")
    if args.train and args.profile_leg:
        ap.error("--train cannot be combined with --profile-leg")
    if args.profile_leg:
        import profile_leg as pl
        if _gate_profile_module() is None or not pl.configured():
            print("ERROR: --profile-leg needs config: profiles.gate, profile_leg.profile and profile_leg.cmd "
                  "— the gate was not run")
            return 2
    if args.train and (_merge_train() is None or _land_lease() is None):
        print("ERROR: --train needs merge_train.py and land_lease.py — the train gate was not run")
        return 2

    head: list[str] = []
    withdraw = _withdraw_train if args.train else _withdraw_if_holding
    try:
        with _gate_heartbeat(args):
            rc, not_run = _gate_main(args, head)
    except BaseException:
        if args.record:
            withdraw(args.story_id, 1, head[0] if head else None)
        raise
    if rc != 0 and args.record and not not_run:
        withdraw(args.story_id, rc, head[0] if head else None)
    return rc


def _gate_main(args: argparse.Namespace, known_head: list[str]) -> tuple[int, bool]:
    """(exit code, ran-no-test)."""
    if not str(wfconfig.get("tests.full_cmd", "") or "").strip():
        print("ERROR: tests.full_cmd is not set in .workflow/config.toml — nothing to run.")
        return 2, False
    ctx, err = resolve_target(args.story_id or None, None)
    if err:
        print(f"ERROR: {err}")
        return 2, False
    missing = missing_required_flags(wfconfig.render(str(wfconfig.get("tests.full_cmd")), **ctx))
    if missing:
        print(f"ERROR: tests.full_cmd lacks required flag(s) {', '.join(missing)} "
              f"(config: tests_policy.required_flags) — suite not run.")
        return 2, False
    expected_branch = args.expected_branch or (ctx.get("branch", "") if args.story_id else "")
    mt, ll = _merge_train(), _land_lease()
    if args.train:
        prefix = str(wfconfig.get("git.branch_prefix", "story/"))
        branch = str(ctx.get("branch", ""))
        suffix = branch[len(prefix):] if prefix and branch.startswith(prefix) else branch
        expected_branch = str(wfconfig.get("git.train_prefix", "train/")) + suffix
    env = run_env(ctx)
    stack = str(ctx["stack"])
    print(f"stack: {stack}  cwd: {ctx['worktree']}")

    try:
        lock = _acquire_suite_lock(args.lock_timeout_minutes, stack=stack)
    except LockTimeout as exc:
        # Fails CLOSED: running unlocked is the collision the lock exists to prevent.
        print(f"gate lock NOT acquired within {args.lock_timeout_minutes}m — suite not run.")
        print(f"  lock: {_lock_path(stack)}")
        print(f"  the gate line file cannot be used: {exc.cause}" if exc.cause else
              "  another full suite is still holding it; retry, or raise --lock-timeout-minutes.")
        if args.train:
            # No test ran: the same "not run" as a failed check, so nothing is withdrawn.
            print(f"{mt.NOT_RUN_MARKER} the gate lock was not acquired within {args.lock_timeout_minutes}m",
                  flush=True)
            return mt.NOT_RUN_EXIT, True
        return 1, False

    try:
        # Under the shared stack's lock: a landing merge holds it while MERGE_HEAD exists, so a
        # merge state seen here was stranded, and a shared-stack gate would test conflict markers.
        if not args.story_id and _git_out(str(ctx["worktree"]), "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
            print("gate refused: the main checkout is mid-merge (MERGE_HEAD present) — resolve it first")
            return 1, False
        tree = base = ""
        ahead: list[str] = []
        lease_path = ll.default_path() if ll is not None else None
        if args.train:
            try:
                reason = mt.not_run_reason(args.story_id, str(ctx["worktree"]), expected_branch)
            except (mt.TrainError, ll.LeaseError, OSError) as exc:
                reason = f"the train checks could not run: {exc}"
            if reason:
                print(f"{mt.NOT_RUN_MARKER} {reason}", flush=True)
                return mt.NOT_RUN_EXIT, True
        if args.record or args.profile_leg:
            # Hashed before the run: the code freeze holds the tree for the whole gate.
            from run_scoped_tests import tested_tree
            try:
                head, tree = tested_tree(str(ctx["worktree"]))
                known_head.append(head)
                base = base_of(str(ctx["worktree"]))
            except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
                print(f"ERROR: the gate was not started: its tested tree or base cannot be determined: {exc}")
                return 2, False
        if args.train:
            ahead = mt.begin_gate(args.story_id, lease_path)
        elif args.record and mt is not None:
            mt.mark_holder(args.story_id, str(ctx["worktree"]), "gating")
        not_run: list[int] = []
        rc = _run_gate(ctx, expected_branch, env, not_run=not_run if args.record else None,
                       profile_leg=args.profile_leg)
        if not_run:
            if args.train:
                print(f"{mt.NOT_RUN_MARKER} preflight failed (stack_preflight.py exit {not_run[0]})", flush=True)
                try:
                    mt.reset_train(args.story_id, lease_path)
                except (mt.TrainError, ll.LeaseError, OSError) as exc:
                    print(f"WARNING: the train gate did not run but {args.story_id}'s train could not be "
                          f"reset: {exc}", flush=True)
                return mt.NOT_RUN_EXIT, True
            return _preflight_not_run(args.story_id, not_run[0]), True
        if args.profile_leg and rc == 0:
            # Before the pre-merge GREEN line: the record never reads green over an uncounted run.
            import profile_leg as pl
            state = pl.state_path()
            try:
                pl.record_green(state, story=args.story_id or str(ctx["stack"]), head=known_head[0],
                                tree=tree, base=base)
            except OSError as exc:
                print(f"ERROR: the green profile-leg run was NOT recorded in {state}: {exc}")
                return 1, False
        if args.record and rc == 0:
            from story_record import cmd_append
            if args.train:
                cmd_append(args.story_id, f"pre-merge gate GREEN -- run_full_suite all legs on {expected_branch} "
                                          f"(exit 0) HEAD {known_head[0]} code-tree {tree} base {base[:12]} "
                                          f"train {','.join(ahead)}")
                try:
                    mt.finish_green(args.story_id, lease_path)
                except (mt.TrainError, ll.LeaseError, OSError) as exc:
                    print(f"WARNING: the train gate passed but {args.story_id}'s train could not be marked "
                          f"green: {exc}", flush=True)
                    # `gating` still carries the gate's fresh stamp, which would hold every poll off a rebuild.
                    with contextlib.suppress(ll.LeaseError, OSError):
                        mt.reset_train(args.story_id, lease_path)
            else:
                cmd_append(args.story_id, f"pre-merge gate GREEN -- run_full_suite all legs (exit 0) "
                                          f"HEAD {known_head[0]} code-tree {tree} base {base[:12]}")
                if mt is not None:
                    mt.mark_holder(args.story_id, str(ctx["worktree"]), "green")
        return rc, False
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
