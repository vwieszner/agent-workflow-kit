#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Prove `compose watch` actually synced files into the containers — and repair it if not.

`compose watch` can stop syncing mid-session with NO error. Tests then run
against stale container files and the failure looks like a code bug. The tell is
a test COUNT that does not change after editing a test file.

Rules this check enforces:
  * PER FILE, not all-or-nothing — watch can sync one file and miss its sibling.
  * Repair reaches EVERY service that mounts the path (a service left stale keeps
    running old code).
  * Each path is checked against the services that SERVE it. When one mount is
    nested inside another (e.g. a frontend dir inside the backend's synced tree),
    the file also exists in the outer mount's containers — checking it there is a
    true-but-useless OK. Routing is longest-host-prefix-first.

Mounts come from `config: stack.health.sync_mounts`
([{host_prefix, services, container_root}]); when that is empty, one mount is
derived: `stack.health.worktree_src` → `stack.health.container_src` in
`stack.app_service`. No mount configured, or `stack.runtime = "none"` → prints
SKIPPED and exits 0.

Paths are given relative to the source tree root (the cwd). Container paths are
emitted with a leading `//` (Linux reads it as `/`; Git Bash on Windows leaves
it alone).

Usage:
  # did my edits land on the shared stack?
  uv run --no-project .workflow/scripts/check_container_sync.py src/app/tasks.py

  # a slot stack, several files, wait up to 2 minutes for watch to catch up
  uv run --no-project .workflow/scripts/check_container_sync.py -p story-7-42 --wait 120 \
      src/app/tasks.py src/app/schemas.py

  # detected drift -> repair it into every drifted service
  uv run --no-project .workflow/scripts/check_container_sync.py -p story-7-42 --fix src/app/x.py

Exit codes: 0 = all in sync (or SKIPPED), 1 = drift remains, 2 = usage/environment error.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def _norm_prefix(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p in (".", ""):
        return ""
    return p.rstrip("/") + "/"


def load_mounts() -> list[tuple[str, tuple[str, ...], str]]:
    """[(host_prefix, services, container_root)], most-specific host prefix FIRST."""
    raw = wfconfig.get("stack.health.sync_mounts", []) or []
    mounts = []
    for m in raw:
        services = tuple(str(s) for s in (m.get("services") or []))
        root = str(m.get("container_root", "")).rstrip("/")
        if services and root:
            mounts.append((_norm_prefix(str(m.get("host_prefix", ""))), services, root))
    if not mounts:
        container_src = str(wfconfig.get("stack.health.container_src", "") or "").rstrip("/")
        if container_src:
            mounts.append((_norm_prefix(str(wfconfig.get("stack.health.worktree_src", ".") or ".")),
                           (_stack.app_service(),), container_src))
    mounts.sort(key=lambda m: len(m[0]), reverse=True)
    return mounts


MOUNTS: list[tuple[str, tuple[str, ...], str]] = []


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _die(msg: str) -> int:
    print(f"[ERROR] {msg}", file=sys.stderr)
    return 2


def _route(rel: str) -> tuple[str, tuple[str, ...], str] | tuple[None, None, None]:
    """Map a tree-relative path to (path under the container root, services, container root)."""
    norm = rel.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    for prefix, services, root in MOUNTS:  # most-specific first
        if norm.startswith(prefix):
            return norm[len(prefix):], services, root
    return None, None, None


def _exec_path(root: str, crel: str) -> str:
    """Path for `exec ... md5sum` — double-slashed so a copy-paste into Git Bash stays correct."""
    return f"/{root}/{crel}"


def _cp_dest(service: str, root: str, crel: str) -> str:
    """Destination for `compose cp`: single slash, since it follows `service:`."""
    return f"{service}:{root}/{crel}"


def _host_md5(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.md5(path.read_bytes()).hexdigest()


def _container_md5(project: str, service: str, cpath: str) -> str | None:
    try:
        out = _stack.run(_stack.compose_argv(project, "exec", "-T", service, "md5sum", cpath),
                         timeout=60, env=_stack.env_for_stack(project))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    parts = out.stdout.strip().split()
    return parts[0] if parts else None


def _copy_in(project: str, service: str, host: Path, root: str, crel: str) -> bool:
    res = _stack.run(_stack.compose_argv(project, "cp", str(host), _cp_dest(service, root, crel)),
                     timeout=120, env=_stack.env_for_stack(project))
    if res.returncode != 0:
        print(f"        cp failed -> {service}: {res.stderr.strip()[:200]}")
        return False
    return True


def survey(project: str, files: list[str], override: tuple[str, ...] | None) -> list[dict]:
    """Hash each file on the host and in the services that serve it (or in `override`)."""
    rows = []
    for rel in files:
        host = Path(rel)
        crel, routed, root = _route(rel)
        services = override if override else routed
        h = _host_md5(host)
        row = {"rel": rel, "host": h, "crel": crel, "root": root, "services": services,
               "containers": {}}
        if crel is not None and services and h is not None:
            for svc in services:
                row["containers"][svc] = _container_md5(project, svc, _exec_path(root, crel))
        rows.append(row)
    return rows


def report(rows: list[dict]) -> tuple[list[tuple[str, str]], int, int]:
    """Print a per-file / per-service table.

    Returns (drifted (rel, service) pairs, files actually checked, files skipped).
    """
    drift: list[tuple[str, str]] = []
    checked = skipped = 0
    for row in rows:
        rel, h = row["rel"], row["host"]
        if row["crel"] is None:
            mounts = ", ".join((p or "<tree root>") for p, _, _ in MOUNTS)
            print(f"[SKIP] {rel} - outside every watch mount ({mounts}), not checked")
            skipped += 1
            continue
        if h is None:
            print(f"[SKIP] {rel} - no such file on the host, not checked")
            skipped += 1
            continue
        checked += 1
        for svc in row["services"]:
            c = row["containers"].get(svc)
            if c is None:
                print(f"[FAIL] {rel:<62} {svc:<8} not readable in container")
                drift.append((rel, svc))
            elif c == h:
                print(f"[ OK ] {rel:<62} {svc:<8} {h[:12]}")
            else:
                print(f"[DRIFT]{rel:<62} {svc:<8} host={h[:12]} container={c[:12]}")
                drift.append((rel, svc))
    return drift, checked, skipped


def _assert_cwd_matches_project(project: str, *, fix: bool, allow_foreign: bool = False) -> None:
    """Refuse --fix (warn on a read-only check) when cwd is not the project's source tree.

    Tree-relative paths resolve against cwd. Run from the main checkout against a
    slot's project, `--fix` copies the BASE BRANCH's files into that slot's
    containers, silently replacing the story's code — and every line then reports
    [ OK ] because the copy makes the check pass.

    Fails OPEN: an unreadable registry or unknown project never blocks a legitimate run.
    """
    if allow_foreign:
        return
    try:
        expected = _stack.worktree_for_stack(project)
    except Exception:  # noqa: BLE001 - fail open
        return
    if expected is None:
        return
    try:
        cwd, exp = Path.cwd().resolve(), expected.resolve()
    except OSError:
        return
    if cwd == exp or exp in cwd.parents:
        return

    banner = (f"\n  cwd     : {cwd}\n"
              f"  expected: {exp}   (source tree for project '{project}')\n"
              "  Tree-relative paths resolve against cwd, so this run compares the WRONG tree's\n"
              f"  files against '{project}'s containers.\n")
    if fix:
        sys.stderr.write(
            "REFUSING --fix: cwd does not match the target project's source tree.\n" + banner +
            "  With --fix that OVERWRITES the containers with the wrong tree's code, and every\n"
            "  line then reports [ OK ] because the copy makes the check pass.\n"
            "  Re-run from the expected directory, or pass --allow-foreign-cwd if deliberate.\n")
        raise SystemExit(2)
    sys.stderr.write("WARNING: cwd does not match the target project's source tree.\n" + banner +
                     "  Results below describe the wrong tree. Watch for '[SKIP] ... no such file\n"
                     "  on the host' on files you know exist - that is this mistake's signature.\n\n")


def main() -> int:
    global MOUNTS
    ap = argparse.ArgumentParser(
        description="verify (and optionally repair) host->container file sync for a compose stack"
    )
    ap.add_argument("files", nargs="+", help="source-tree-relative paths")
    ap.add_argument("-p", "--project", default=None,
                    help="compose project name (default: config: slots.shared_stack_name)")
    # Comma-separated, NOT nargs="*": a multi-value option in front of a nargs="+" positional
    # swallows the file list.
    ap.add_argument("--services", metavar="A,B", default=None,
                    help="override the services to check, comma-separated. By default each "
                         "path is routed to the services that serve it (config mounts)")
    ap.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                    help="poll until in sync or timeout, instead of checking once")
    ap.add_argument("--fix", action="store_true",
                    help="repair drift with `compose cp` into every drifted service")
    ap.add_argument("--allow-foreign-cwd", action="store_true",
                    help="skip the cwd/project guard (deliberately checking another tree)")
    args = ap.parse_args()
    project = args.project or str(wfconfig.get("slots.shared_stack_name", ""))
    if not project:
        return _die("no --project and config: slots.shared_stack_name is empty")

    if not wfconfig.stack_enabled():
        _stack.skipped("container sync check", _stack.STACK_NONE_REASON)
        return 0
    MOUNTS = load_mounts()
    if not MOUNTS:
        _stack.skipped("container sync check",
                       'no sync mount configured (config: stack.health.sync_mounts is empty and '
                       'stack.health.container_src = "")')
        return 0

    # Guard BEFORE any hashing: a cwd/project mismatch silently compares — and with
    # --fix, overwrites — the wrong tree.
    _assert_cwd_matches_project(project, fix=args.fix, allow_foreign=args.allow_foreign_cwd)

    override = None
    if args.services is not None:
        override = tuple(s.strip() for s in args.services.split(",") if s.strip())
        if not override:
            return _die("--services was given with no service names")

    deadline = time.monotonic() + args.wait
    while True:
        rows = survey(project, args.files, override)
        drift, checked, skipped = report(rows)
        if not drift:
            tail = f" ({skipped} skipped)" if skipped else ""
            hit = sorted({s for r in rows if r["crel"] is not None and r["host"] for s in r["services"]})
            print(f"[{_stamp()}] {checked} file(s) in sync across {', '.join(hit) or 'no services'}{tail}")
            return 0 if checked else 2
        if args.wait and time.monotonic() < deadline:
            remaining = int(deadline - time.monotonic())
            print(f"[{_stamp()}] still waiting on compose watch - {len(drift)} drifted, {remaining}s left...")
            time.sleep(5)
            continue
        break

    if not args.fix:
        print(f"\n{len(drift)} drift(s). compose watch is not syncing - check it is still running.")
        print("Re-run with --fix to copy the host files into the drifted services.")
        return 1

    print(f"\n[{_stamp()}] repairing {len(drift)} drift(s) with `compose cp`...")
    ok = True
    repaired_services = set()
    for rel, svc in drift:
        host = Path(rel)
        crel, _, root = _route(rel)
        if crel is None or not host.is_file():
            ok = False
            continue
        if _copy_in(project, svc, host, root, crel):
            print(f"        copied {rel} -> {svc}")
            repaired_services.add(svc)
        else:
            ok = False

    rows = survey(project, args.files, override)
    remaining, _checked, _skipped = report(rows)
    if remaining or not ok:
        print(f"\n{len(remaining)} drift(s) REMAIN after repair.")
        return 1
    print("\nAll files repaired. NOTE: a service whose watch action is `sync+restart` may need")
    print(f"      `{' '.join(_stack.compose_prefix())} -p {project} restart "
          f"{' '.join(sorted(repaired_services))}` for the copy to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
