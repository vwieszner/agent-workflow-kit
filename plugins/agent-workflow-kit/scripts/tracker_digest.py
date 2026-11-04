#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Digest a tracker search result for the `design-sync` skill's tracker step.

Reads a search result too large to read inline: the file `tracker.py search --out` wrote, or the
tool-result file a tracker MCP search call left in a Claude Code session's `tool-results/`
directory. Both result shapes are accepted: `{"issues": {"nodes": [...]}}` and
`{"data": {"issues": [...]}}`. Prints one line per issue: key, status, updated, created, comment
count (`comment.total` / comments carried), the id, author and time of every comment newer than
`--since`, and the summary.

An issue is marked FETCH when the result has no `comment` field, a `comment.total` that is absent,
null or not an integer, or fewer comments than `comment.total`: read that issue's comments with
`tracker.py comments --key <key>` (or the MCP comments tool). `--comments-out <file>` writes the full
body of every comment newer than `--since` under a
`===== <key> comment <id> by <author> at <created> =====` header; a non-string body is written as JSON.

`--since` defaults to the date in the question tracker's `Status (<YYYY-MM-DD>)` header
(`config: paths.design_questions`), the last sync. Either way it must be a valid `YYYY-MM-DD` date.

`--session <id>` searches `<id>/tool-results/` under this project's Claude Code transcript dirs
(`_transcripts.project_dirs()`) for `config: tracker.mcp_search_result_glob` and takes the newest.

Usage
-----
    uv run --no-project <plugin-root>/scripts/tracker_digest.py <result file> [--since YYYY-MM-DD] [--comments-out <path>]
    uv run --no-project <plugin-root>/scripts/tracker_digest.py --session <session-id> [...]

Exit 0: digest printed. Exit 1: digest printed, but more pages follow (`MORE PAGES` line). Exit 2:
unreadable file, unrecognised shape, no search result in the session, no `--since` and no tracker
date, or an invalid date. Writes nothing but `--comments-out`. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _design as d  # noqa: E402
import _transcripts  # noqa: E402
import wfconfig  # noqa: E402

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DEFAULT_GLOB = "mcp-*search*Issues*-*.txt"


def fail(msg: str) -> int:
    print(f"ERROR: {msg}")
    return 2


def result_glob() -> str:
    return str(d.cfg("tracker.mcp_search_result_glob", DEFAULT_GLOB) or DEFAULT_GLOB)


def find_result(session: str) -> Path | None:
    hits = sorted((p for project in _transcripts.project_dirs()
                   for p in (Path(project) / session / "tool-results").glob(result_glob())),
                  key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def issues_of(doc: dict) -> tuple[list[dict], bool] | None:
    """The issue list and whether more pages follow, from either result shape; None when neither matches."""
    iss = doc.get("issues")
    if isinstance(iss, dict) and isinstance(iss.get("nodes"), list):
        return iss["nodes"], bool((iss.get("pageInfo") or {}).get("hasNextPage"))
    data = doc.get("data")
    if isinstance(data, dict) and isinstance(data.get("issues"), list):
        return data["issues"], data.get("isLast") is False
    return None


def tracker_since(path: Path | None = None) -> str | None:
    rel = d.tracker_rel()
    target = path or ((wfconfig.repo_root() / rel) if rel else None)
    if target is None:
        return None
    try:
        return d.last_sync(target.read_text(encoding="utf-8"))
    except OSError:
        return None


def valid_date(text: str) -> bool:
    if not DATE_RE.fullmatch(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def digest(issue: dict, since: str) -> tuple[str, list[dict], bool]:
    """One report line, the comments newer than `since` (ISO timestamps compare as strings), and FETCH."""
    f = issue.get("fields") or {}
    status = (f.get("status") or {}).get("name", "-")
    c = f.get("comment")
    if not isinstance(c, dict):
        count, new, fetch = "no comment field", [], True
    else:
        shown = c.get("comments") or []
        total = c.get("total")
        new = [x for x in shown if (x.get("created") or "") >= since]
        if isinstance(total, int) and not isinstance(total, bool):
            count, fetch = f"{total}/{len(shown)}", total > len(shown)
        else:
            count, fetch = f"?/{len(shown)} (total {json.dumps(total) if 'total' in c else 'absent'})", True
    marks = ", ".join(f"{x.get('id')} {(x.get('author') or {}).get('displayName', '?')} "
                      f"{(x.get('created') or '')[:16]}" for x in new)
    line = " | ".join([
        issue.get("key", "?"), status,
        f"updated {(f.get('updated') or '-')[:16]}",
        f"created {(f.get('created') or '-')[:10]}",
        f"comments {count}", f"new [{marks}]", (f.get("summary") or "")[:80],
    ])
    return (line + ("  FETCH" if fetch else "")), new, fetch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Digest a tracker search result")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("result", nargs="?", type=Path, help="the search result file")
    src.add_argument("--session", help="take the newest tracker MCP search result of this session")
    ap.add_argument("--since", help="YYYY-MM-DD; default: the question tracker's Status header date")
    ap.add_argument("--comments-out", type=Path, help="write the full body of every newer comment here")
    args = ap.parse_args(argv)

    path = args.result if args.result else find_result(args.session)
    if path is None:
        return fail(f"no {result_glob()} under <transcript dir>/{args.session}/tool-results")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return fail(f"{path}: {e}")
    found = issues_of(doc) if isinstance(doc, dict) else None
    if found is None:
        return fail(f"{path}: neither issues.nodes[] nor data.issues[]")
    issues, more = found
    since = args.since or tracker_since()
    if not since:
        return fail("no --since and no `Status (<date>)` header in the question tracker (config: paths.design_questions)")
    if not valid_date(since):
        return fail(f"since date {since!r} is not a valid YYYY-MM-DD date")

    print(f"{len(issues)} issues in {path.name}; new comments since {since}")
    out = []
    fetch = 0
    for issue in issues:
        line, new, needs_fetch = digest(issue, since)
        fetch += needs_fetch
        print(line)
        for x in new:
            body = x.get("body")
            out.append(f"\n===== {issue.get('key')} comment {x.get('id')} by "
                       f"{(x.get('author') or {}).get('displayName', '?')} at {x.get('created')} =====\n"
                       f"{body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)}\n")
    print(f"FETCH: {fetch} issue(s) need their comments read")
    if args.comments_out:
        args.comments_out.write_text("".join(out), encoding="utf-8", newline="\n")
        print(f"wrote {len(out)} comment(s) to {args.comments_out}")
    if more:
        print("MORE PAGES: this result is one page of the search; fetch the next page and digest it too")
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
