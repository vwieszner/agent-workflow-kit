#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Issue-tracker calls for the design-owner workflow (`design-ticket`, `design-sync`), behind one interface.

`config: tracker.backend` picks the backend:
  "jira"  Jira Cloud REST (stdlib urllib). Site `config: tracker.site`; credentials from the environment
          variables named by `config: tracker.user_env` and `config: tracker.token_env` (basic auth,
          email + API token). Credentials are never written anywhere.
  "mcp"   the session makes the calls with the tracker's MCP tools (`config: tracker.mcp_tools`); this
          script then prints which tool to call and exits 3. `tracker_digest.py` reads that tool's result.
  "none"  no tracker: every subcommand prints SKIPPED and exits 3.
A new backend is a subclass of `Backend` registered in `BACKENDS`.

Subcommands (JSON on stdout):
  search --query Q [--fields f1,f2] [--max N] [--out FILE]
      Runs the query (JQL for jira). Writes the result in the shape tracker_digest.py reads
      ({"data": {"issues": [...], "isLast": bool}}) to --out (default stdout).
  create --summary S --description-file F [--label L ...] [--issue-type T] [--priority P] [--assignee A]
      Creates an issue in `config: tracker.project_key`; defaults from `config: tracker.issue_type`,
      `.priority`, `.assignee`. Prints {"key", "url"}.
  comment --key K --body-file F
      Adds a comment. Prints {"id"}.
  comments --key K
      Every comment of the issue: {"comments": [{id, author, created, body}]}.

Exit codes: 0 ok; 1 the tracker answered with an error (message printed, nothing retried);
2 usage / missing configuration or credentials; 3 SKIPPED (backend "none" or "mcp").
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _design as d  # noqa: E402

TIMEOUT = 60


class TrackerError(RuntimeError):
    pass


class ConfigError(RuntimeError):
    pass


class Backend:
    name = "base"

    def search(self, query: str, fields: list[str], max_results: int) -> dict:
        raise NotImplementedError

    def create(self, summary: str, description: str, labels: list[str], issue_type: str, priority: str,
               assignee: str) -> dict:
        raise NotImplementedError

    def comment(self, key: str, body: str) -> dict:
        raise NotImplementedError

    def comments(self, key: str) -> dict:
        raise NotImplementedError


class JiraRest(Backend):
    """Jira Cloud REST: v3 search (`/rest/api/3/search/jql`), v2 issue and comment (plain-text bodies)."""

    name = "jira"

    def __init__(self, opener=None):
        self.site = str(d.cfg("tracker.site", "") or "").rstrip("/")
        if not self.site:
            raise ConfigError("config: tracker.site is empty")
        user_env = str(d.cfg("tracker.user_env", "TRACKER_USER"))
        token_env = str(d.cfg("tracker.token_env", "TRACKER_TOKEN"))
        user, token = os.environ.get(user_env, ""), os.environ.get(token_env, "")
        if not user or not token:
            raise ConfigError(f"set the environment variables {user_env} and {token_env} (tracker credentials)")
        self.auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
        self.project = str(d.cfg("tracker.project_key", "") or "")
        self.open = opener or urllib.request.urlopen

    def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        url = self.site + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with self.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise TrackerError(f"{method} {path}: HTTP {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise TrackerError(f"{method} {path}: {exc.reason}") from None
        return json.loads(raw) if raw else {}

    def search(self, query, fields, max_results):
        issues: list[dict] = []
        token = None
        while True:
            params = {"jql": query, "fields": ",".join(fields), "maxResults": str(max_results)}
            if token:
                params["nextPageToken"] = token
            page = self._call("GET", "/rest/api/3/search/jql", params=params)
            issues += page.get("issues") or []
            token = page.get("nextPageToken")
            if not token or page.get("isLast", True) or len(issues) >= max_results:
                return {"data": {"issues": issues, "isLast": not token or bool(page.get("isLast", True))}}

    def create(self, summary, description, labels, issue_type, priority, assignee):
        if not self.project:
            raise ConfigError("config: tracker.project_key is empty")
        fields: dict = {"project": {"key": self.project}, "summary": summary, "description": description,
                        "issuetype": {"name": issue_type}, "labels": labels}
        if priority:
            fields["priority"] = {"name": priority}
        if assignee:
            fields["assignee"] = {"accountId": assignee}
        out = self._call("POST", "/rest/api/2/issue", {"fields": fields})
        return {"key": out.get("key"), "url": d.ticket_url(out.get("key") or "")}

    def comment(self, key, body):
        out = self._call("POST", f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment", {"body": body})
        return {"id": out.get("id")}

    def comments(self, key):
        out = self._call("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment",
                         params={"maxResults": "1000"})
        return {"comments": [{"id": c.get("id"), "author": (c.get("author") or {}).get("displayName"),
                              "created": c.get("created"), "body": c.get("body")}
                             for c in out.get("comments") or []]}


BACKENDS: dict[str, type[Backend]] = {"jira": JiraRest}
MCP_TOOL_OF = {"search": "search", "create": "create", "comment": "comment", "comments": "comments"}


def backend_name() -> str:
    return str(d.cfg("tracker.backend", "none") or "none")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="issue-tracker calls (config: tracker.backend)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search")
    p.add_argument("--query", required=True)
    p.add_argument("--fields", default="summary,status,updated,created,comment")
    p.add_argument("--max", type=int, default=200)
    p.add_argument("--out", type=Path)
    p = sub.add_parser("create")
    p.add_argument("--summary", required=True)
    p.add_argument("--description-file", type=Path, required=True)
    p.add_argument("--label", action="append", default=[])
    p.add_argument("--issue-type", default=None)
    p.add_argument("--priority", default=None)
    p.add_argument("--assignee", default=None)
    p = sub.add_parser("comment")
    p.add_argument("--key", required=True)
    p.add_argument("--body-file", type=Path, required=True)
    p = sub.add_parser("comments")
    p.add_argument("--key", required=True)
    args = ap.parse_args(argv)

    name = backend_name()
    if name == "none":
        print(json.dumps({"skipped": "config: tracker.backend is none — no issue tracker configured"}))
        return 3
    if name == "mcp":
        tools = d.cfg("tracker.mcp_tools", {}) or {}
        tool = tools.get(MCP_TOOL_OF[args.cmd], "") if isinstance(tools, dict) else ""
        print(json.dumps({"skipped": "config: tracker.backend is mcp — make this call with the tracker's MCP tool",
                          "tool": tool or f"(config: tracker.mcp_tools.{MCP_TOOL_OF[args.cmd]} is not set)"}))
        return 3
    cls = BACKENDS.get(name)
    if cls is None:
        print(json.dumps({"error": f"unknown tracker backend {name!r}; known: {sorted(BACKENDS)}, mcp, none"}))
        return 2
    try:
        backend = cls()
        if args.cmd == "search":
            out = backend.search(args.query, [f for f in args.fields.split(",") if f], args.max)
            if args.out:
                args.out.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
                out = {"written": str(args.out), "issues": len(out["data"]["issues"])}
        elif args.cmd == "create":
            out = backend.create(
                args.summary, args.description_file.read_text(encoding="utf-8"),
                args.label or [str(x) for x in (d.cfg("tracker.labels", []) or [])],
                args.issue_type or str(d.cfg("tracker.issue_type", "Task")),
                args.priority if args.priority is not None else str(d.cfg("tracker.priority", "") or ""),
                args.assignee if args.assignee is not None else str(d.cfg("tracker.assignee", "") or ""))
        elif args.cmd == "comment":
            out = backend.comment(args.key, args.body_file.read_text(encoding="utf-8"))
        else:
            out = backend.comments(args.key)
    except ConfigError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except TrackerError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
