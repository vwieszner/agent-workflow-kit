#!/usr/bin/env python3
"""Summarise the failed tests in a Playwright test-results folder: one block per trace.zip.

Per test: the failing assertion's first lines and top stack frame; the actions run before the
failure with the failed one marked (step titles from the test runner's `test.trace`, else the
browser-library trace events); HTTP responses with status >= 400 and failed requests (status
<= 0 or a failure text, e.g. net::ERR_ABORTED); browser console errors. A trace line that does
not parse is counted and reported, never skipped silently. A trace.zip that cannot be read is
reported as `!! unreadable <path> — <reason>` and the next one is still summarised.
Read-only; stdlib only.

Usage
-----
    uv run --no-project .workflow/scripts/pw_failure_summary.py <test-results-dir | trace.zip> [...] [--max-actions 5]

    Point it at the Playwright `test-results` output directory (with a container stack, its host
    bind mount). The next gate or E2E run on that stack clears it, so copy it out first when the
    stack may run again.

When to use
-----------
    An E2E leg failed (gate or scoped run) and the diagnosis needs the failing action, the stack line and
    the failing HTTP call, instead of unzipping and parsing each trace by hand. Playwright keeps a trace
    only for a failed test (`trace: 'retain-on-failure'`); a `-retry1` folder is its own block.

Exit 0 when at least one trace was summarised, 1 when trace.zip files were found but none could be
read, 2 when no trace.zip was found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import zlib
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
MAX_HTTP = 10
MAX_FAILED_REQUESTS = 10
MAX_CONSOLE = 10
# test.trace steps that only group other steps; listing them would crowd out the real actions.
CONTAINER_STEPS = {"hook", "fixture"}
# What a truncated or corrupted archive raises from ZipFile() or ZipFile.read(); UnicodeDecodeError
# comes from a damaged member-name entry, NotImplementedError from a damaged compression field.
UNREADABLE = (zipfile.BadZipFile, zlib.error, EOFError, OSError, UnicodeDecodeError, NotImplementedError)


def trace_archives(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(path.rglob("trace.zip")))
        elif path.is_file() and path.suffix == ".zip":
            found.append(path)
        else:
            print(f"skipped (not a trace.zip or a directory): {path}", file=sys.stderr)
    return found


def read_members(archive: zipfile.ZipFile) -> tuple[dict[str, list[dict]], int]:
    """Events per `.trace` / `.network` member, keyed by the member's file name."""
    members: dict[str, list[dict]] = {}
    unparsed = 0
    for name in sorted(archive.namelist()):
        if not name.endswith((".trace", ".network")):
            continue
        events = members.setdefault(Path(name).name, [])
        for line in archive.read(name).decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                unparsed += 1
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                unparsed += 1
    return members, unparsed


def step_label(event: dict) -> str:
    """A test.trace step's title; a library call's class.method plus its url or selector."""
    if event.get("title"):
        return event["title"]
    params = event.get("params") or {}
    target = params.get("url") or params.get("selector") or ""
    name = event.get("apiName") or f"{event.get('class')}.{event.get('method')}"
    return f"{name} {target}".strip()


def action_lines(members: dict[str, list[dict]], max_actions: int) -> list[str]:
    """The steps before the failure, the failed one marked. Steps and the failed id come from one
    source: test.trace when it has steps, else the library traces merged in time order."""
    steps = [event for event in members.get("test.trace", []) if event.get("type") in ("before", "after")]
    if not any(event.get("type") == "before" for event in steps):
        library = [event for name, events in members.items()
                   if name.endswith(".trace") and name != "test.trace" for event in events
                   if event.get("type") in ("before", "after")]
        steps = sorted(library, key=lambda event: event.get("startTime", event.get("endTime", 0)) or 0)

    befores = [event for event in steps if event.get("type") == "before"]
    # The first failing `after` is the leaf: a failure propagates to the enclosing steps, whose
    # `after` events close later.
    failed_id = next((event.get("callId") for event in steps
                      if event.get("type") == "after" and event.get("error")), None)
    failed_at = next((i for i, event in enumerate(befores) if event.get("callId") == failed_id), None)

    out: list[str] = []
    if failed_at is None:
        out.append("   no failed step recorded: the error was thrown outside a Playwright action")
        cut = next((i for i, event in enumerate(befores)
                    if event.get("title") == "After Hooks" and not event.get("parentId")), len(befores))
    else:
        cut = failed_at
    shown = [event for event in befores[:cut]
             if not (event.get("class") == "Test" and event.get("method") in CONTAINER_STEPS)]
    out.extend(f"   action: {step_label(event)}" for event in (shown[-max_actions:] if max_actions > 0 else []))
    if failed_at is not None:
        out.append(f"   FAILED action: {step_label(befores[failed_at])}")
    return out


def summarise(path: Path, max_actions: int) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        members, unparsed = read_members(archive)
    out = [f"== {path.parent.name if path.name == 'trace.zip' else path.name}"]
    if unparsed:
        out.append(f"   !! {unparsed} trace line(s) did not parse: this summary is incomplete")

    events = members.get("test.trace", []) + [event for name, member in members.items()
                                              if name != "test.trace" for event in member]
    for event in events:
        if event.get("type") == "error":
            message = [line for line in ANSI.sub("", event.get("message") or "").splitlines() if line.strip()][:3]
            out.extend(f"   error: {line[:300]}" for line in message)
            frames = event.get("stack") or []
            if frames:
                out.append(f"   at {frames[0].get('file')}:{frames[0].get('line')}")
            break

    out.extend(action_lines(members, max_actions))

    http: dict[tuple[str, str, int], int] = {}
    failed: dict[tuple[str, str, str], int] = {}
    for event in events:
        if event.get("type") != "resource-snapshot":
            continue
        snapshot = event.get("snapshot") or {}
        request = snapshot.get("request") or {}
        response = snapshot.get("response") or {}
        method, url = request.get("method", "?"), request.get("url", "?")
        status = response.get("status") or 0
        failure = response.get("_failureText")
        if failure or status <= 0:
            key = (method, url, failure or "no response")
            failed[key] = failed.get(key, 0) + 1
        elif status >= 400:
            key = (method, url, status)
            http[key] = http.get(key, 0) + 1
    for (method, url, status), count in sorted(http.items())[:MAX_HTTP]:
        out.append(f"   http {status} {method} {url}" + (f" x{count}" if count > 1 else ""))
    for (method, url, reason), count in sorted(failed.items())[:MAX_FAILED_REQUESTS]:
        out.append(f"   failed {method} {url} — {reason}" + (f" x{count}" if count > 1 else ""))

    console = [event for event in events if event.get("type") == "console" and event.get("messageType") == "error"]
    for event in console[:MAX_CONSOLE]:
        where = (event.get("location") or {}).get("url", "")
        out.append(f"   console: {(event.get('text') or '')[:200]} @ {where}")
    if len(console) > MAX_CONSOLE:
        out.append(f"   ... {len(console) - MAX_CONSOLE} more console errors")
    return out


def main(argv: list[str] | None = None) -> int:
    # Trace text holds characters the Windows console code page cannot encode (→, ›).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Summarise failed Playwright tests from trace.zip files.")
    parser.add_argument("paths", nargs="+", help="a test-results directory (searched recursively) or trace.zip files")
    parser.add_argument("--max-actions", type=int, default=5, help="actions shown before the failure (default 5)")
    args = parser.parse_args(argv)
    archives = trace_archives(args.paths)
    if not archives:
        print("no trace.zip found", file=sys.stderr)
        return 2
    summarised = 0
    for path in archives:
        try:
            block = summarise(path, args.max_actions)
        except UNREADABLE as exc:
            print(f"!! unreadable {path} — {exc}")
            continue
        print("\n".join(block))
        summarised += 1
    return 0 if summarised else 1


if __name__ == "__main__":
    raise SystemExit(main())
