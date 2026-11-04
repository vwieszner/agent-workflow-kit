"""Tests for the design-owner backends and helpers:

- tracker.py: backend none / mcp are SKIPPED (exit 3); the jira backend pages a search into the
  digest shape, creates and comments through REST with basic auth, refuses without credentials, and an
  HTTP error is exit 1;
- tracker_digest.py: one line per issue, FETCH when comments are missing, new comments after --since,
  MORE PAGES exit 1, --since from the question tracker's Status header, bad dates exit 2;
- docs_mirror.py: none backend SKIPPED, --init writes the first mirror, a newer version is rewritten
  with --write (dry run writes nothing), the same version with a different body exits 1, a format
  mismatch exits 2, a wrong page exits 2;
- snapshot_update.py: not configured SKIPPED, refusals, the compose exec + cp sequence;
- ui_gap_owner.py: hits per spec and epic, REGRESSION-CANDIDATE for a done story;
- design_page_renders.py: a URL outside the allowed form is refused, a non-PNG download writes nothing;
- design_context.py: text blocks extracted under the provenance header.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _state_testenv as te  # noqa: E402
import design_context  # noqa: E402
import design_page_renders  # noqa: E402
import docs_mirror  # noqa: E402
import snapshot_update  # noqa: E402
import tracker  # noqa: E402
import tracker_digest  # noqa: E402
import ui_gap_owner  # noqa: E402

JIRA = """[tracker]
backend = "jira"
site = "https://example.atlassian.net"
project_key = "UX"
issue_type = "Task"
priority = "Medium"
assignee = "acc-1"
user_env = "T_USER"
token_env = "T_TOKEN"
"""


def run(fn, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fn(argv)
    return code, out.getvalue()


class FakeResponse:
    def __init__(self, body: dict, status: int = 200):
        self.body, self.status = json.dumps(body).encode(), status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TrackerTests(unittest.TestCase):
    def test_none_and_mcp_are_skipped(self):
        te.enter(self, "")
        code, out = run(tracker.main, ["search", "--query", "x"])
        self.assertEqual(code, 3)
        self.assertIn("SKIPPED".lower(), out.lower())
        te.enter(self, '[tracker]\nbackend = "mcp"\nmcp_tools = { search = "mcp__atl__search" }\n')
        code, out = run(tracker.main, ["search", "--query", "x"])
        self.assertEqual(code, 3)
        self.assertIn("mcp__atl__search", out)

    def test_missing_credentials_is_a_config_error(self):
        te.enter(self, JIRA)
        with mock.patch.dict("os.environ", {"T_USER": "", "T_TOKEN": ""}):
            code, out = run(tracker.main, ["search", "--query", "x"])
        self.assertEqual(code, 2)
        self.assertIn("T_USER", out)

    def test_jira_search_pages_create_and_comment(self):
        te.enter(self, JIRA)
        calls = []
        pages = [{"issues": [{"key": "UX-1"}], "nextPageToken": "p2", "isLast": False},
                 {"issues": [{"key": "UX-2"}], "isLast": True}]

        def opener(req, timeout=None):
            calls.append(req)
            if "search/jql" in req.full_url:
                return FakeResponse(pages[len([c for c in calls if "search/jql" in c.full_url]) - 1])
            if req.full_url.endswith("/rest/api/2/issue"):
                return FakeResponse({"key": "UX-9"})
            return FakeResponse({"id": "100"})

        with mock.patch.dict("os.environ", {"T_USER": "me@example.com", "T_TOKEN": "tok"}):
            backend = tracker.JiraRest(opener=opener)
            found = backend.search('project = UX', ["summary"], 50)
            self.assertEqual([i["key"] for i in found["data"]["issues"]], ["UX-1", "UX-2"])
            self.assertTrue(found["data"]["isLast"])
            self.assertIn("nextPageToken=p2", calls[1].full_url)
            made = backend.create("S", "D", ["l"], "Task", "Medium", "acc-1")
            self.assertEqual(made, {"key": "UX-9", "url": "https://example.atlassian.net/browse/UX-9"})
            body = json.loads(calls[2].data)
            self.assertEqual(body["fields"]["project"], {"key": "UX"})
            self.assertEqual(body["fields"]["assignee"], {"accountId": "acc-1"})
            self.assertEqual(backend.comment("UX-9", "hello"), {"id": "100"})
            self.assertIn("/rest/api/2/issue/UX-9/comment", calls[3].full_url)
        auth = calls[0].get_header("Authorization")
        self.assertEqual(base64.b64decode(auth.split()[1]).decode(), "me@example.com:tok")

    def test_http_error_is_exit_1(self):
        te.enter(self, JIRA)

        def opener(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"denied"))

        with mock.patch.dict("os.environ", {"T_USER": "u", "T_TOKEN": "t"}), \
                mock.patch.object(tracker.urllib.request, "urlopen", opener):
            code, out = run(tracker.main, ["search", "--query", "x"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", out)


def issue(key, status="To Do", comments=None, total=None):
    fields = {"summary": f"s {key}", "status": {"name": status}, "updated": "2026-10-12T10:00:00.000+0000",
              "created": "2026-10-01T10:00:00.000+0000"}
    if comments is not None:
        fields["comment"] = {"comments": comments, "total": len(comments) if total is None else total}
    return {"key": key, "fields": fields}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, '[paths]\ndesign_questions = "q.md"\n')
        (self.root / "q.md").write_text("| Ticket | Item | Priority | Blocks | Status (2026-10-10) |\n", encoding="utf-8")

    def write(self, doc):
        p = self.root / "r.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def test_lines_fetch_and_new_comments(self):
        new = {"id": "7", "author": {"displayName": "D"}, "created": "2026-10-11T09:00:00.000+0000", "body": "answer"}
        old = {"id": "6", "author": {"displayName": "D"}, "created": "2026-10-01T09:00:00.000+0000", "body": "x"}
        doc = {"data": {"issues": [issue("UX-1", comments=[old, new]), issue("UX-2"),
                                   issue("UX-3", comments=[old], total=3)], "isLast": True}}
        out_file = self.root / "c.txt"
        code, out = run(tracker_digest.main, [str(self.write(doc)), "--comments-out", str(out_file)])
        self.assertEqual(code, 0, out)
        self.assertIn("new comments since 2026-10-10", out)
        lines = out.splitlines()
        self.assertTrue(next(l for l in lines if l.startswith("UX-1")).count("7 D 2026-10-11"))
        self.assertTrue(next(l for l in lines if l.startswith("UX-2")).endswith("FETCH"))
        self.assertTrue(next(l for l in lines if l.startswith("UX-3")).endswith("FETCH"))
        self.assertIn("===== UX-1 comment 7 by D", out_file.read_text(encoding="utf-8"))

    def test_more_pages_and_bad_date(self):
        doc = {"issues": {"nodes": [issue("UX-1", comments=[])], "pageInfo": {"hasNextPage": True}}}
        code, out = run(tracker_digest.main, [str(self.write(doc))])
        self.assertEqual(code, 1)
        self.assertIn("MORE PAGES", out)
        code, out = run(tracker_digest.main, [str(self.write(doc)), "--since", "2026-13-01"])
        self.assertEqual(code, 2)


BODY = "# Intro\n\nLine a\n\n## Colours\n\ncyan\n"


def mcp_result(number, body=BODY, page="42", fmt="markdown"):
    return {"data": {"id": page, "title": "Spec", "body": {"format": fmt, "value": body, "lossyConversion": False,
                                                          "lostFeatures": []},
                     "metadata": {"authorId": "acc", "version": {"number": number, "createdAt": "2026-10-01T00:00:00Z"}}}}


class DocsMirrorTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, '[docs_mirror]\nbackend = "confluence"\npage_id = "42"\nmirror = "spec.md"\n')

    def result(self, *a, **k):
        p = self.root / f"r{len(list(self.root.glob('r*.json')))}.json"
        p.write_text(json.dumps(mcp_result(*a, **k)), encoding="utf-8")
        return str(p)

    def test_none_backend_is_skipped(self):
        te.enter(self, "")
        self.assertEqual(run(docs_mirror.main, ["x.json"])[0], 3)

    def test_init_then_newer_then_same_version_drift(self):
        code, out = run(docs_mirror.main, ["--init", self.result(1)])
        self.assertEqual(code, 0, out)
        mirror = self.root / "spec.md"
        text = mirror.read_text(encoding="utf-8")
        self.assertIn("Version: 1, authored 2026-10-01T00:00:00Z by account acc", text)
        self.assertIn("Format: markdown", text)
        self.assertTrue(text.endswith(BODY))
        newer = BODY.replace("cyan", "teal") + "\n## Added\n\nx\n"
        diff = self.root / "d.diff"
        code, out = run(docs_mirror.main, [self.result(2, body=newer), "--diff-out", str(diff)])
        self.assertEqual(code, 0, out)
        self.assertIn("not written", out)
        self.assertEqual(mirror.read_text(encoding="utf-8"), text)
        self.assertIn("ADDED   Added", out)
        self.assertIn("CHANGED Colours", out)
        code, out = run(docs_mirror.main, [self.result(2, body=newer), "--write", "--diff-out", str(diff)])
        self.assertEqual(code, 0, out)
        self.assertTrue(mirror.read_text(encoding="utf-8").endswith(newer))
        code, out = run(docs_mirror.main, [self.result(2, body=newer + "hand edit\n"), "--diff-out", str(diff)])
        self.assertEqual(code, 1, out)

    def test_format_and_page_mismatch_exit_2(self):
        run(docs_mirror.main, ["--init", self.result(1)])
        self.assertEqual(run(docs_mirror.main, [self.result(2, fmt="storage")])[0], 2)
        self.assertEqual(run(docs_mirror.main, [self.result(2, page="99")])[0], 2)


class SnapshotTests(unittest.TestCase):
    CONFIG = """[stack]
runtime = "docker-compose"
compose_cmd = "docker compose"
[slots]
stack_prefix = "story-"
[design]
snapshots = [{ name = "theme", service = "web", test = "web/theme.test.js", update_cmd = ["npm", "test", "--", "-u"], container_path = "/app/__snapshots__/t.snap", worktree_path = "web/__snapshots__/t.snap" }]
"""

    def test_unconfigured_is_skipped(self):
        te.enter(self, '[stack]\nruntime = "docker-compose"\n')
        self.assertEqual(run(snapshot_update.main, ["--project-name", "story-1", "--worktree", "."])[0], 3)

    def test_refusals(self):
        te.enter(self, self.CONFIG)
        snap = snapshot_update.pick(None)
        self.assertIn("not a story stack", snapshot_update.refusal("shared", ".", snap))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("no web/theme.test.js", snapshot_update.refusal("story-1", tmp, snap))

    def test_update_then_copy(self):
        te.enter(self, self.CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def fake_run(cmd, **kw):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "Snapshots  1 updated\n", "")

            with mock.patch.object(snapshot_update, "refusal", return_value=None), \
                    mock.patch.object(snapshot_update.subprocess, "run", fake_run), \
                    mock.patch.object(snapshot_update, "_git", return_value=""):
                code, out = run(snapshot_update.main, ["--project-name", "story-1", "--worktree", tmp])
        self.assertEqual(code, 0, out)
        self.assertEqual(calls[0], ["docker", "compose", "-p", "story-1", "exec", "-T", "web", "npm", "test", "--", "-u"])
        self.assertEqual(calls[1][:6], ["docker", "compose", "-p", "story-1", "cp", "web:/app/__snapshots__/t.snap"])
        self.assertIn("snapshot current", out)
        self.assertIn("Snapshots  1 updated", out)


class GapOwnerTests(unittest.TestCase):
    def test_hits_and_regression_candidate(self):
        root = te.enter(self, '[paths]\nspecs_dir = "arts"\nepics_dir = "epics"\nsprint_status = "arts/sprint-status.yaml"\n')
        (root / "arts").mkdir()
        (root / "epics").mkdir()
        (root / "arts" / "sprint-status.yaml").write_text("development_status:\n  1-2-roster: done\n  1-3-apps: ready-for-dev\n",
                                                          encoding="utf-8")
        (root / "arts" / "1-2-roster.md").write_text("Shows Pending Applications.\n", encoding="utf-8")
        (root / "arts" / "1-3-apps.md").write_text("pending applications tab\n", encoding="utf-8")
        (root / "arts" / "1-3-apps-code-review-findings-2026-01-01.md").write_text("Pending Applications\n", encoding="utf-8")
        (root / "epics" / "epic-1.md").write_text("Pending Applications list\n", encoding="utf-8")
        code, out = run(ui_gap_owner.main, ["Pending Applications"])
        self.assertEqual(code, 0)
        self.assertIn("(3 hits)", out)
        self.assertIn("1-2-roster  [done]  REGRESSION-CANDIDATE  arts/1-2-roster.md:1", out)
        self.assertIn("1-3-apps  [ready-for-dev]  arts/1-3-apps.md:1", out)
        self.assertIn("epic  epics/epic-1.md:1", out)


class PageRenderTests(unittest.TestCase):
    def test_refused_url_and_non_png(self):
        root = te.enter(self, '[design]\nlibrary_dir = "lib"\n')
        good = "https://www.figma.com/api/mcp/asset/0a1b-2c.png"
        m = root / "m.json"
        m.write_text(json.dumps({"a.png": "https://evil.example/x.png"}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(design_page_renders.main(["--map", str(m)]), 1)
        m.write_text(json.dumps({"a.png": good}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = design_page_renders.main(["--map", str(m)], opener=lambda url, timeout=None: FakeResponse({"x": 1}))
        self.assertEqual(code, 1)
        self.assertFalse((root / "lib" / "renders" / "_pages" / "a.png").exists())

    def test_png_is_saved(self):
        root = te.enter(self, '[design]\nlibrary_dir = "lib"\n')
        m = root / "m.json"
        m.write_text(json.dumps({"a.png": "https://www.figma.com/api/mcp/asset/0a1b.png"}), encoding="utf-8")
        png = FakeResponse({})
        png.body = b"\x89PNG\r\n\x1a\nrest"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(design_page_renders.main(["--map", str(m)], opener=lambda url, timeout=None: png), 0)
        self.assertEqual((root / "lib" / "renders" / "_pages" / "a.png").read_bytes(), png.body)


class DesignContextTests(unittest.TestCase):
    def test_text_blocks_under_header(self):
        root = te.enter(self, '[design]\nintake_dir = "design"\ncontext_note = "Use the component props."\n')
        src = root / "dc.json"
        src.write_text(json.dumps([{"type": "image", "data": "..."}, {"type": "text", "text": "<div/>"}]), encoding="utf-8")
        code, out = run(design_context.main, ["--json", str(src), "--slug", "home", "--node", "1:2", "--date", "2026-01-01"])
        self.assertEqual(code, 0, out)
        text = (root / "design" / "design-context" / "home.md").read_text(encoding="utf-8")
        self.assertIn("node 1:2", text)
        self.assertIn("Use the component props.", text)
        self.assertIn("```tsx\n<div/>\n```", text)


if __name__ == "__main__":
    unittest.main()
