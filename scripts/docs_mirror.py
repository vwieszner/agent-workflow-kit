#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Keep a read-only repo mirror of the external design-specification page (`config: docs_mirror.*`).

The page lives in a docs system (Confluence today; `config: docs_mirror.backend`). The mirror
(`config: docs_mirror.mirror`) is a header comment plus the page body byte-for-byte. Two sources:

  * a result file: the JSON the docs MCP's page-content call (`config: docs_mirror.mcp_tool`) left in a
    session's `tool-results/` directory (`{"data": {"id", "title", "body": {"format", "value",
    "lossyConversion", "lostFeatures"}, "metadata": {"version": {"number", "createdAt"}, "authorId"}}}`),
    or `--session <id>` for the newest such file of that session;
  * `--fetch`: Confluence Cloud REST v2 (`GET /wiki/api/v2/pages/<page_id>?body-format=<body_format>`),
    credentials from the environment variables `config: docs_mirror.user_env` / `.token_env`.

The script compares the result's version with the mirror header's `Version:` line, writes the unified
diff of the bodies to a file, prints a section-level summary (headings added / removed / changed) and,
with `--write`, rewrites the mirror (LF line endings). Nothing is written unless the live version is
newer. A mirror whose `Format:` differs from the result's is never compared (exit 2): re-export it from
the same source.

Usage
-----
    uv run --no-project .workflow/scripts/docs_mirror.py <result file> [--write] [--mirror P] [--diff-out P]
    uv run --no-project .workflow/scripts/docs_mirror.py --session <session-id> [--write]
    uv run --no-project .workflow/scripts/docs_mirror.py --fetch [--write]
    uv run --no-project .workflow/scripts/docs_mirror.py --init <result file>      # first export, no mirror yet

Exit 0: mirror rewritten, current, or a dry run with a newer version. Exit 1: same or older version but
the body differs (hand edit, or converter drift); nothing written. Exit 2: bad input (unreadable result,
missing field, wrong page, format mismatch, no result in the session, mirror missing or without a
`Version:` line, docs backend not configured). Exit 3: SKIPPED (`config: docs_mirror.backend` is none).
Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _transcripts  # noqa: E402
import wfconfig  # noqa: E402

VERSION_RE = re.compile(r"^Version: (\d+), authored (\S+) by account (\S+)$", re.M)
FORMAT_RE = re.compile(r"^Format: (\S+)", re.M)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$|^<h[1-6][^>]*>(.*?)</h[1-6]>", re.I)
DEFAULT_GLOB = "mcp-*getConfluence*-*.txt"


class BadInput(Exception):
    pass


def cfg(key: str, default):
    v = wfconfig.get(f"docs_mirror.{key}", default)
    return default if v is None else v


def fail(msg: str) -> int:
    print(f"ERROR: {msg}")
    return 2


def page_id() -> str:
    return str(cfg("page_id", "") or "")


def mirror_default() -> Path | None:
    raw = str(cfg("mirror", "") or "")
    return (wfconfig.repo_root() / raw) if raw else None


def find_result(session: str) -> Path | None:
    glob = str(cfg("mcp_result_glob", DEFAULT_GLOB) or DEFAULT_GLOB)
    hits = sorted((p for project in _transcripts.project_dirs()
                   for p in (Path(project) / session / "tool-results").glob(glob)),
                  key=lambda p: p.stat().st_mtime)
    for p in reversed(hits):
        try:
            if str(json.loads(p.read_text(encoding="utf-8"))["data"].get("id")) == page_id():
                return p
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def from_mcp(doc: dict) -> dict:
    data = doc["data"]
    body, metadata = data["body"], data["metadata"]
    return {"page_id": str(data["id"]), "title": data["title"], "format": body["format"], "body": body["value"],
            "lossy": body.get("lossyConversion", False), "lost": body.get("lostFeatures", []),
            "number": int(metadata["version"]["number"]), "created": metadata["version"]["createdAt"],
            "author_id": metadata.get("authorId", "unknown"), "via": "the docs MCP"}


def from_rest(doc: dict, fmt: str) -> dict:
    body = (doc.get("body") or {}).get(fmt) or {}
    version = doc["version"]
    return {"page_id": str(doc["id"]), "title": doc["title"], "format": fmt, "body": body["value"],
            "lossy": False, "lost": [], "number": int(version["number"]), "created": version["createdAt"],
            "author_id": version.get("authorId", "unknown"), "via": "the REST API"}


def fetch_rest(opener=None) -> dict:
    site = str(cfg("site", "") or wfconfig.get("tracker.site", "") or "").rstrip("/")
    if not site or not page_id():
        raise BadInput("config: docs_mirror.site and docs_mirror.page_id are required for --fetch")
    user_env = str(cfg("user_env", "") or wfconfig.get("tracker.user_env", "TRACKER_USER"))
    token_env = str(cfg("token_env", "") or wfconfig.get("tracker.token_env", "TRACKER_TOKEN"))
    user, token = os.environ.get(user_env, ""), os.environ.get(token_env, "")
    if not user or not token:
        raise BadInput(f"set the environment variables {user_env} and {token_env}")
    fmt = str(cfg("body_format", "storage") or "storage")
    url = f"{site}/wiki/api/v2/pages/{urllib.parse.quote(page_id())}?body-format={urllib.parse.quote(fmt)}"
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode(),
        "Accept": "application/json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=60) as resp:
            return from_rest(json.loads(resp.read()), fmt)
    except urllib.error.HTTPError as exc:
        raise BadInput(f"GET {url}: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise BadInput(f"GET {url}: {exc.reason}") from None


def render_header(result: dict, export_date: str) -> str:
    space = str(cfg("space", "") or "")
    site = str(cfg("site", "") or wfconfig.get("tracker.site", "") or "").rstrip("/")
    lines = ["<!--",
             f'SOURCE OF TRUTH: page {result["page_id"]}{f" (space {space})" if space else ""} - "{result["title"]}"']
    if site:
        lines.append(f"URL: {site}/wiki/spaces/{space}/pages/{result['page_id']}" if space
                     else f"URL: {site}/wiki/pages/viewpage.action?pageId={result['page_id']}")
    lines += [
        f"Version: {result['number']}, authored {result['created']} by account {result['author_id']}",
        f"Format: {result['format']}",
        f"Exported: {export_date} via {result['via']}, copied byte-for-byte from the result",
        f"Lossy conversion: {result['lossy']} — features not representable: {result['lost']}",
        "READ-ONLY MIRROR. Do not hand-edit - re-export with .workflow/scripts/docs_mirror.py instead.",
        "-->", "", ""]
    return "\n".join(lines)


def split_mirror(text: str) -> tuple[str, str]:
    if not text.startswith("<!--") or "-->" not in text:
        return "", text
    header, _, body = text.partition("-->")
    return header + "-->", body.lstrip("\n")


def sections(text: str) -> dict[str, list[str]]:
    """Heading text (a repeated heading gets ` #<n>`) -> the lines under it."""
    out: dict[str, list[str]] = {}
    seen: Counter[str] = Counter()
    cur = "(before the first heading)"
    out[cur] = []
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            name = re.sub(r"<[^>]+>", "", (m.group(1) or m.group(2) or "")).replace("*", "").strip() or "(empty heading)"
            seen[name] += 1
            cur = name if seen[name] == 1 else f"{name} #{seen[name]}"
            out[cur] = []
        else:
            out[cur].append(line)
    return out


def write_mirror(path: Path, result: dict) -> None:
    body = result["body"]
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_header(result, dt.date.today().isoformat()) + body + ("" if body.endswith("\n") else "\n")
    path.write_text(text, encoding="utf-8", newline="")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-export the design-specification mirror")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("result", nargs="?", type=Path, help="page-content result file")
    src.add_argument("--session", help="the newest page-content result of this session")
    src.add_argument("--fetch", action="store_true", help="fetch the page through the REST API")
    src.add_argument("--init", type=Path, metavar="RESULT", help="write the first mirror from a result file")
    ap.add_argument("--write", action="store_true", help="rewrite the mirror when the live version is newer")
    ap.add_argument("--mirror", type=Path, default=None, help="mirror file (default: config docs_mirror.mirror)")
    ap.add_argument("--diff-out", type=Path, help="unified diff file (default: in the temp dir)")
    args = ap.parse_args(argv)

    if str(cfg("backend", "none") or "none") == "none":
        print("SKIPPED — config: docs_mirror.backend is none (no external specification page)")
        return 3
    mirror = args.mirror or mirror_default()
    if mirror is None:
        return fail("config: docs_mirror.mirror is empty")
    try:
        if args.fetch:
            result = fetch_rest()
        else:
            path = args.init or args.result or find_result(args.session)
            if path is None:
                return fail(f"no page-content result for page {page_id()} under <transcript dir>/{args.session}/tool-results")
            result = from_mcp(json.loads(path.read_text(encoding="utf-8")))
    except BadInput as exc:
        return fail(str(exc))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return fail(f"not a page-content result ({exc!r})")
    if page_id() and result["page_id"] != page_id():
        return fail(f"page {result['page_id']}, expected {page_id()} (config: docs_mirror.page_id)")
    if args.init:
        if mirror.exists():
            return fail(f"{mirror} exists; --init writes only a first mirror")
        write_mirror(mirror, result)
        print(f"mirror written: {mirror} (v{result['number']}, format {result['format']})")
        return 0
    if not mirror.is_file():
        return fail(f"mirror not found: {mirror} (first export: --init <result file>)")

    old_header, old_body = split_mirror(mirror.read_text(encoding="utf-8"))
    m = VERSION_RE.search(old_header)
    if m is None:
        return fail(f"{mirror}: no 'Version: <n>, authored <ts> by account <id>' line in the header")
    fm = FORMAT_RE.search(old_header)
    if fm and fm.group(1) != result["format"]:
        return fail(f"{mirror} holds format {fm.group(1)}, the result is {result['format']}: re-export from the same source")
    old_no, live_no, body = int(m.group(1)), result["number"], result["body"]

    diff = list(difflib.unified_diff(old_body.splitlines(), body.splitlines(),
                                     f"mirror-v{old_no}", f"live-v{live_no}", n=0, lineterm=""))
    diff_out = args.diff_out or Path(tempfile.gettempdir()) / f"docs-mirror-{result['page_id']}-v{old_no}-v{live_no}.diff"
    diff_out.write_text("\n".join(diff) + "\n", encoding="utf-8")

    print(f"page {result['page_id']}: live v{live_no} ({result['created']}), mirror v{old_no} ({mirror})")
    if live_no <= old_no:
        if not diff:
            print("mirror is current; body identical to the result")
            return 0
        print(f"mirror is not older but its body differs from the result: {len(diff)} diff lines in {diff_out}")
        print("not written: a hand edit of the read-only mirror, or a converter change at the same version")
        return 1

    a, b = sections(old_body), sections(body)
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    changed = [k for k in b if k in a and a[k] != b[k]]
    print(f"sections: +{len(added)} -{len(removed)} ~{len(changed)} (live has {len(b)})")
    for tag, keys in (("ADDED  ", added), ("REMOVED", removed), ("CHANGED", changed)):
        for k in keys:
            print(f"  {tag} {k}")
    print(f"full diff: {diff_out} ({len(diff)} lines) - read it whole")
    if not args.write:
        print("not written: pass --write to re-export the mirror")
        return 0
    write_mirror(mirror, result)
    print(f"mirror rewritten: {mirror} (v{old_no} -> v{live_no}, {len(body)} chars, LF)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
