#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic stack preflight — THE pre-test gate for a slot stack.

The `dev-preflight` agent is diagnosis-only, used when this script reports red;
never use the agent as the gate.

Checks (all read-only; never mutates the stack, the worktree, or any container):
  1. services  — every `config: stack.services` entry is running and not unhealthy
  2. health    — each `config: stack.health.checks` entry ({name, cmd, timeout_s});
                 cmd is a template run through the shell, exit 0 = healthy
  3. watch     — a compose-watch process anchored in the worktree (process CWD
                 readout; psutil when importable, POSIX `ps` + /proc|lsof fallback).
                 Skipped when `config: stack.watch = false`.
  4. code sync — sha256 of the N most-recently-modified files under the sync
                 mounts' host prefixes compared against the container copies, each
                 file routed to the services of the most specific mount that serves it
                 (`config: stack.health.sync_mounts`; empty → one mount
                 `stack.health.worktree_src` → `stack.health.container_src` in the app
                 service — check_container_sync.load_mounts). Read-only on purpose: a
                 WRITE probe would trigger a sync+restart watch action and bounce the
                 service. Skipped when no mount is configured.
  5. branch    — worktree is on the expected git branch

`config: stack.runtime = "none"` → checks 1–4 print SKIPPED; only the branch
check runs.

Usage:
  uv run --no-project .workflow/scripts/stack_preflight.py --project-name story-7-50 \
      --worktree <worktree path> [--expected-branch story/7-50] [--slot N]
  uv run --no-project .workflow/scripts/stack_preflight.py --project-name <config: slots.shared_stack_name>
      (shared stack: --worktree defaults to the repo root, branch to config: git.base_branch)

Windows: the watch check needs psutil (`uv run --with psutil --no-project ...`);
without it the check FAILS as "cannot verify" rather than passing.

Exit codes: 0 = all checks pass (warnings allowed), 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402

results: list[tuple[str, str, str]] = []  # (level, check, detail)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def record(level: str, check: str, detail: str) -> None:
    results.append((level, check, detail))
    print(f"[{level}] {check:<10} {detail}")


def skip(check: str, reason: str) -> None:
    record("SKIP", check, f"SKIPPED — {reason}")


def check_services(project: str) -> bool:
    expected = _stack.expected_services()
    if not expected:
        record("FAIL", "services", "config: stack.services is empty — nothing to verify; "
                                   "list the long-running services")
        return False
    proc = _stack.run(_stack.compose_argv(project, "ps", "--format",
                                          "{{.Service}}\t{{.State}}\t{{.Status}}"),
                      env=_stack.env_for_stack(project))
    if proc.returncode != 0:
        record("FAIL", "services", f"compose ps exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return False
    state: dict[str, tuple[str, str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            state[parts[0]] = (parts[1], parts[2])
    ok = True
    for svc in expected:
        if svc not in state:
            record("FAIL", "services", f"{svc}: NOT PRESENT in stack {project}")
            ok = False
            continue
        svc_state, status = state[svc]
        if svc_state != "running":
            record("FAIL", "services", f"{svc}: state={svc_state} ({status})")
            ok = False
        elif "unhealthy" in status:
            record("FAIL", "services", f"{svc}: {status}")
            ok = False
    if ok:
        record("PASS", "services", f"{len(expected)}/{len(expected)} running ({project})")
    return ok


def check_health(project: str, worktree: Path, slot: int | None) -> bool:
    checks = wfconfig.get("stack.health.checks", []) or []
    if not checks:
        skip("health", "config: stack.health.checks is empty")
        return True
    env = wfconfig.compose_env(slot) if slot is not None else _stack.env_for_stack(project)
    ok = True
    for chk in checks:
        name = str(chk.get("name", "check"))
        cmd = wfconfig.render(str(chk.get("cmd", "")), stack=project, worktree=str(worktree),
                              slot=slot, service=_stack.app_service())
        if not cmd.strip():
            record("FAIL", name, "health check has an empty cmd")
            ok = False
            continue
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  errors="replace", cwd=_stack.compose_dir(worktree), env=env,
                                  timeout=int(chk.get("timeout_s", 90)))
        except subprocess.TimeoutExpired:
            record("FAIL", name, f"timed out after {chk.get('timeout_s', 90)}s: {cmd}")
            ok = False
            continue
        if proc.returncode == 0:
            record("PASS", name, f"exit 0: {cmd}")
        else:
            record("FAIL", name, f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}")
            ok = False
    return ok


def check_watch(worktree: Path) -> bool:
    found, note = _stack.find_compose_watch(worktree)
    if note:
        record("FAIL", "watch", f"cannot verify compose watch — {note}")
        return False
    if found:
        record("PASS", "watch", f"compose watch running (pid {', '.join(str(p['pid']) for p in found)}) "
                                f"cwd={worktree}")
        return True
    record("FAIL", "watch", f"no compose-watch process anchored in {worktree} — edits will NOT sync; "
                            "restart via bring_up_story_stack.py --mode watch (carries ALL port vars)")
    return False


def _sync_candidates(src: Path, ignores: list[str], count: int) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in ignores]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = str(p.relative_to(src))
            if any(seg in rel for seg in ignores):
                continue
            files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:count]


def check_code_sync(project: str, worktree: Path) -> bool:
    import check_container_sync as ccs
    mounts = ccs.load_mounts()  # most-specific host prefix first
    if not mounts:
        skip("sync", 'no sync mount configured (config: stack.health.sync_mounts is empty and '
                     'stack.health.container_src = "")')
        return True
    root = worktree.resolve()
    ignores = [str(x) for x in (wfconfig.get("stack.health.sync_ignores",
                                             [".git", "__pycache__", ".pytest_cache", ".pyc",
                                              "node_modules"]) or [])]
    count = int(wfconfig.get("stack.health.sync_sample_count", 3))
    grace = int(wfconfig.get("stack.health.sync_grace_s", 30))
    ok = True
    candidates: set[Path] = set()
    for prefix, _services, _croot in mounts:
        src = (root / prefix).resolve() if prefix else root
        if not src.is_dir():
            record("FAIL", "sync", f"source dir missing: {src}")
            ok = False
            continue
        candidates.update(_sync_candidates(src, ignores, count))
    sample = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[:count]
    for path in sample:
        rel = path.relative_to(root).as_posix()
        # Route to the mount that SERVES the file: a nested mount's files also exist in the
        # outer mount's containers, where a match proves nothing.
        prefix, services, croot = next(m for m in mounts if rel.startswith(m[0]))
        crel = rel[len(prefix):]
        local_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        for service in services:
            proc = _stack.run(_stack.compose_argv(project, "exec", "-T", service, "sh", "-c",
                                                  f"sha256sum '{croot}/{crel}' 2>/dev/null || echo MISSING"),
                              timeout=45, env=_stack.env_for_stack(project))
            container_sha = proc.stdout.strip().split()[0] if proc.stdout.strip() else "ERROR"
            if container_sha == local_sha:
                record("PASS", "sync", f"{rel} [{service}] (sha match)")
                continue
            age = time.time() - path.stat().st_mtime
            if age < grace:
                record("WARN", "sync", f"{rel} [{service}] differs but was modified {age:.0f}s ago "
                                       "— sync may be in flight")
            else:
                record("FAIL", "sync", f"{rel} [{service}] STALE in container (local mtime "
                                       f"{age:.0f}s ago; container={container_sha[:12]})")
                ok = False
    return ok


def check_branch(worktree: Path, expected: str) -> bool:
    proc = _stack.run(["git", "-C", str(worktree), "branch", "--show-current"])
    actual = proc.stdout.strip()
    if actual == expected:
        record("PASS", "branch", actual)
        return True
    record("FAIL", "branch", f"on {actual!r}, expected {expected!r}")
    return False


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue]
    ap = argparse.ArgumentParser(description="deterministic stack preflight (read-only)")
    ap.add_argument("--project-name", required=True)
    ap.add_argument("--worktree", default=None, help="default: the repo root")
    ap.add_argument("--expected-branch", default=None,
                    help="default: config: git.base_branch for the shared stack, "
                         "else config: git.branch_prefix + <id> from the project name")
    ap.add_argument("--slot", type=int, default=None,
                    help="slot for port placeholders in health checks (default: from the registry)")
    args = ap.parse_args()

    worktree = Path(args.worktree) if args.worktree else wfconfig.repo_root()
    shared = str(wfconfig.get("slots.shared_stack_name", ""))
    prefix = str(wfconfig.get("slots.stack_prefix", "story-"))
    expected_branch = args.expected_branch or (
        str(wfconfig.get("git.base_branch", "development")) if args.project_name == shared
        else wfconfig.branch_name(args.project_name.removeprefix(prefix))
    )
    slot = args.slot if args.slot is not None else _stack.slot_for_stack(args.project_name)

    print(f"[{ts()}] stack_preflight: project={args.project_name} worktree={worktree}")
    ok = True
    if not wfconfig.stack_enabled():
        for chk in ("services", "health", "watch", "sync"):
            skip(chk, _stack.STACK_NONE_REASON)
    else:
        if slot is None:
            record("WARN", "slot", f"slot for {args.project_name} unknown — port placeholders in "
                                   "health checks stay unresolved; pass --slot")
        ok &= check_services(args.project_name)
        ok &= check_health(args.project_name, worktree, slot)
        if wfconfig.get("stack.watch", True):
            ok &= check_watch(worktree)
        else:
            skip("watch", "config: stack.watch = false")
        ok &= check_code_sync(args.project_name, worktree)
    ok &= check_branch(worktree, expected_branch)

    fails = [r for r in results if r[0] == "FAIL"]
    print(f"[{ts()}] verdict: {'PASS' if ok else 'FAIL'} "
          f"({len(results) - len(fails)} ok/warn/skip, {len(fails)} failing)")
    if not ok:
        print("preflight FAILED — for diagnosis beyond the details above, dispatch the dev-preflight "
              "subagent (report-only) or inspect logs: <compose> -p <project> logs --tail=30 <svc>")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
