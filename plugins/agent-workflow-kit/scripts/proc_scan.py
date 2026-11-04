#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["psutil"]
# ///
"""Find host processes by command line — read-only.

Usage (psutil is a PEP 723 dependency: `uv run --no-project` installs it into uv's cache, never the host):
  uv run --no-project <plugin-root>/scripts/proc_scan.py --watch <stack>
  uv run --no-project <plugin-root>/scripts/proc_scan.py --match run_full_suite --match <story-id>
  uv run --no-project <plugin-root>/scripts/proc_scan.py --pid 33148 --chain
  uv run --no-project <plugin-root>/scripts/proc_scan.py --pid 33148 --tree
  uv run --no-project <plugin-root>/scripts/proc_scan.py --wait-gone run_scoped_tests.py --timeout 1800

--watch      `<compose> -p <stack> ... watch` processes, one row per watch; exit 1 when more than one.
--match      rows whose command line matches every regex; the scan's own process tree is excluded.
--chain      parent chain of --pid, nearest first.
--tree       --pid and its live descendants, indented by depth; exit 1 when --pid is not running,
             2 when its descendants cannot be listed. Takes --pid alone.
--wait-gone  poll until nothing matches, a timestamped line per poll; exit 1 on --timeout.
Never kills: stop a listed pid you own with `.workflow/scripts/kill_owned.py`.
psutil is imported only where live processes are read, so the pure selection functions
(`select`, `watch_roots`, `tree_lines`) import without it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import time

CMD_WIDTH = 200


def live_rows() -> list[dict]:
    import psutil
    own = {os.getpid()} | {p.pid for p in psutil.Process().parents()}
    rows = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
        if proc.info["pid"] in own:
            continue
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if cmd:
            rows.append(proc.info | {"cmd": cmd})
    return rows


def select(rows: list[dict], patterns: list[str]) -> list[dict]:
    regexes = [re.compile(p) for p in patterns]
    return [r for r in rows if all(rx.search(r["cmd"]) for rx in regexes)]


def watch_roots(rows: list[dict], project: str) -> list[dict]:
    """One row per watch: a compose CLI wrapper spawns the compose plugin with the same arguments, so a row whose
    parent is also a match is the same watch, not a duplicate."""
    hits = select(rows, [rf"(?:^|\s)(?:-p|--project-name)[=\s]{re.escape(project)}(?:\s|$)",
                         r"(?:^|\s)watch(?:\s|$)"])
    pids = {r["pid"] for r in hits}
    return [r for r in hits if r["ppid"] not in pids]


def _fmt(r: dict) -> str:
    created = dt.datetime.fromtimestamp(r["create_time"]).strftime("%m-%d %H:%M:%S")
    return f"{r['pid']:>7} ppid={r['ppid']:<7} {created} {r['name']:<20} {r['cmd'][:CMD_WIDTH]}"


def chain(pid: int) -> list[str]:
    import psutil
    out = []
    try:
        proc = psutil.Process(pid)
        for p in [proc, *proc.parents()]:
            try:
                out.append(f"{p.pid:>7} {p.name():<20} {' '.join(p.cmdline())[:CMD_WIDTH]}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                out.append(f"{p.pid:>7} <no access>")
    except psutil.NoSuchProcess:
        out.append(f"{pid:>7} <not running>")
    return out


class TreeUnlisted(Exception):
    """The descendants of a live pid cannot be listed, so no count of them is known."""


def tree_lines(root: dict, descendants: list[dict]) -> list[str]:
    """`root` then each descendant under its parent, depth-first in creation order, two spaces per level.
    A descendant whose parent is not in the set (it exited between listings) hangs under `root`."""
    known = {root["pid"]} | {d["pid"] for d in descendants}
    kids: dict[int, list[dict]] = {}
    for d in descendants:
        parent = d["ppid"] if d["ppid"] in known and d["ppid"] != d["pid"] else root["pid"]
        kids.setdefault(parent, []).append(d)
    out: list[str] = []

    def walk(row: dict, depth: int) -> None:
        out.append("  " * depth + _fmt(row))
        for child in sorted(kids.get(row["pid"], []), key=lambda r: (r["create_time"], r["pid"])):
            walk(child, depth + 1)

    walk(root, 0)
    return out


def _row(proc) -> dict | None:
    import psutil
    try:
        with proc.oneshot():
            info = {"pid": proc.pid, "ppid": proc.ppid(), "name": proc.name(), "create_time": proc.create_time()}
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return {"pid": proc.pid, "ppid": -1, "name": "<no access>", "create_time": 0.0, "cmd": ""}
    try:
        info["cmd"] = " ".join(proc.cmdline())
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        info["cmd"] = "<no access>"
    return info


def tree(pid: int) -> list[str] | None:
    import psutil
    try:
        proc = psutil.Process(pid)
        descendants = proc.children(recursive=True)
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise TreeUnlisted(str(exc) or type(exc).__name__) from exc
    root = _row(proc)
    if root is None:
        return None
    return tree_lines(root, [r for r in map(_row, descendants) if r is not None])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="find host processes by command line (read-only)")
    ap.add_argument("--watch", metavar="STACK")
    ap.add_argument("--match", action="append", default=[], metavar="REGEX")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--chain", action="store_true")
    ap.add_argument("--tree", action="store_true")
    ap.add_argument("--wait-gone", metavar="REGEX")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args(argv)
    if a.tree and (a.chain or a.match or a.watch or a.wait_gone):
        ap.error("--tree takes --pid alone: not with --chain, --match, --watch or --wait-gone")
    if a.tree and a.pid is None:
        ap.error("--tree needs --pid")
    if a.watch:
        rows = watch_roots(live_rows(), a.watch)
        for r in rows:
            print(_fmt(r))
        print(f"{len(rows)} watch process(es) for {a.watch}" + (" - DUPLICATE" if len(rows) > 1 else ""))
        return 1 if len(rows) > 1 else 0
    if a.pid is not None and a.chain:
        print("\n".join(chain(a.pid)))
        return 0
    if a.pid is not None and a.tree:
        try:
            lines = tree(a.pid)
        except TreeUnlisted as exc:
            print(f"{a.pid:>7} descendants cannot be listed ({exc}): no count of them is known")
            return 2
        if lines is None:
            print(f"{a.pid:>7} <not running>")
            return 1
        print("\n".join(lines))
        print(f"{len(lines) - 1} live descendant(s) of {a.pid}")
        return 0
    if a.wait_gone:
        deadline = time.monotonic() + a.timeout
        while rows := select(live_rows(), [a.wait_gone]):
            if time.monotonic() >= deadline:
                print(f"[{dt.datetime.now():%H:%M:%S}] TIMEOUT after {a.timeout}s; still running: "
                      f"{', '.join(str(r['pid']) for r in rows)}")
                return 1
            print(f"[{dt.datetime.now():%H:%M:%S}] still waiting on {len(rows)} process(es) matching {a.wait_gone!r}",
                  flush=True)
            time.sleep(a.interval)
        print(f"[{dt.datetime.now():%H:%M:%S}] none matching {a.wait_gone!r}")
        return 0
    if a.match:
        rows = select(live_rows(), a.match)
        for r in rows:
            print(_fmt(r))
        print(f"{len(rows)} match(es)")
        return 0
    ap.error("one of --watch, --match, --pid --chain, --pid --tree, --wait-gone is required")
    return 2


if __name__ == "__main__":
    sys.exit(main())
