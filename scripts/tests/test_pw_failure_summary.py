"""Tests for ``scripts/pw_failure_summary.py`` over synthetic trace.zip files built in a temp dir.

Pins:
  * one failing test is summarised: the ANSI-stripped error lines and top stack frame, the actions
    before the failure with the failed one marked (hook / fixture container steps left out), HTTP
    responses >= 400 deduplicated with a count, failed requests (a `_failureText`, or status -1
    with none) with their reason, console errors only, and the unparsed-line warning;
  * action titles come from `test.trace`; without it, from the library trace events;
  * the failed step is the leaf whose `after` fails first, not the enclosing step it propagates to;
  * an error thrown outside any Playwright step is reported as such, and the actions listed are the
    ones before the "After Hooks" teardown;
  * a truncated zip and a zip with a corrupt member are reported as `!! unreadable` and skipped
    while the next archive is still summarised (exit 0); exit 1 when every archive is unreadable,
    exit 2 when no trace.zip is found.

The script runs as a subprocess with piped stdout, as an agent runs it, so the UTF-8 output
handling is exercised too.

Usage
-----
    uv run --no-project scripts/tests/test_pw_failure_summary.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pw_failure_summary.py"

ANSI_ERROR = ("Error: \x1b[2mexpect(\x1b[22m\x1b[31mlocator\x1b[39m\x1b[2m).\x1b[22mtoBeVisible\x1b[2m()"
              "\x1b[22m failed\n\nLocator:  getByTestId('dnd5e-stat-card')\nExpected: visible\n")


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          encoding="utf-8", errors="strict", timeout=60)


def jsonl(events: list) -> str:
    return "\n".join(event if isinstance(event, str) else json.dumps(event) for event in events) + "\n"


def write_trace(folder: Path, members: dict[str, list], compression: int = zipfile.ZIP_DEFLATED) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "trace.zip"
    with zipfile.ZipFile(path, "w", compression) as archive:
        for name, events in members.items():
            archive.writestr(name, jsonl(events))
    return path


def step(call_id: str, title: str, method: str = "pw:api", parent: str | None = None) -> dict:
    event = {"type": "before", "callId": call_id, "stepId": call_id, "class": "Test", "method": method,
             "title": title, "params": {}, "stack": []}
    if parent:
        event["parentId"] = parent
    return event


def done(call_id: str, error: str | None = None) -> dict:
    event = {"type": "after", "callId": call_id, "annotations": []}
    if error:
        event["error"] = {"name": "", "message": error}
    return event


def resource(method: str, url: str, status: int, failure: str | None = None) -> dict:
    response = {"status": status, "statusText": "", "headers": []}
    if failure:
        response["_failureText"] = failure
    return {"type": "resource-snapshot", "snapshot": {"request": {"method": method, "url": url}, "response": response}}


def console(kind: str, text: str, url: str) -> dict:
    return {"type": "console", "messageType": kind, "text": text, "args": [],
            "location": {"url": url, "lineNumber": 0, "columnNumber": 0}}


def library_steps() -> list:
    return [
        {"type": "context-options", "origin": "library"},
        {"type": "before", "callId": "call@10", "startTime": 10.0, "class": "Frame", "method": "goto",
         "params": {"url": "http://localhost:4173/projects/111/dashboard"}},
        {"type": "after", "callId": "call@10", "endTime": 11.0},
        {"type": "before", "callId": "call@11", "startTime": 12.0, "class": "Frame", "method": "expect",
         "params": {"selector": "internal:testid=[data-testid=\"dnd5e-stat-card\"s]"}},
        {"type": "after", "callId": "call@11", "endTime": 17.0, "error": {"name": "Expect", "message": "Expect failed"}},
    ]


def failing_members() -> dict[str, list]:
    return {
        "test.trace": [
            {"type": "context-options", "origin": "testRunner"},
            step("hook@1", "Before Hooks", method="hook"),
            step("fixture@2", 'Fixture "page"', method="fixture", parent="hook@1"),
            step("pw:api@3", 'POST "/api/token/"', parent="hook@1"),
            done("pw:api@3"), done("fixture@2"), done("hook@1"),
            step("pw:api@4", 'Navigate to "/projects/111/dashboard"'), done("pw:api@4"),
            step("expect@5", 'Expect "toBeVisible"', method="expect"),
            done("expect@5", error=ANSI_ERROR),
            {"type": "error", "message": ANSI_ERROR,
             "stack": [{"file": "/app/tests/pages/PlayerViewPage.js", "line": 43, "column": 38},
                       {"file": "/app/tests/items.spec.js", "line": 60, "column": 22}]},
            step("hook@6", "After Hooks", method="hook"), done("hook@6"),
        ],
        "0-trace.trace": library_steps() + [
            console("error", "Failed to load resource: the server responded with a status of 500 → tasks",
                    "http://localhost:4173/api/projects/111/tasks/"),
            console("log", "routine log line", "http://localhost:4173/"),
            "{not json",
        ],
        "0-trace.network": [
            resource("GET", "http://localhost:4173/", 200),
            resource("GET", "http://localhost:4173/api/projects/111/tasks/", 500),
            resource("GET", "http://localhost:4173/api/projects/111/tasks/", 500),
            resource("GET", "http://localhost:4173/api/items/?page=1", -1, "net::ERR_ABORTED"),
            resource("GET", "http://localhost:4173/api/active-members/", -1),
        ],
    }


class PwFailureSummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_failing_test_is_summarised(self):
        write_trace(self.root / "items-Item-Stats-chromium", failing_members())
        proc = run(str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [
            "== items-Item-Stats-chromium",
            "   !! 1 trace line(s) did not parse: this summary is incomplete",
            "   error: Error: expect(locator).toBeVisible() failed",
            "   error: Locator:  getByTestId('dnd5e-stat-card')",
            "   error: Expected: visible",
            "   at /app/tests/pages/PlayerViewPage.js:43",
            '   action: POST "/api/token/"',
            '   action: Navigate to "/projects/111/dashboard"',
            '   FAILED action: Expect "toBeVisible"',
            "   http 500 GET http://localhost:4173/api/projects/111/tasks/ x2",
            "   failed GET http://localhost:4173/api/active-members/ — no response",
            "   failed GET http://localhost:4173/api/items/?page=1 — net::ERR_ABORTED",
            "   console: Failed to load resource: the server responded with a status of 500 → tasks"
            " @ http://localhost:4173/api/projects/111/tasks/",
        ])

    def test_action_titles_prefer_test_trace_and_fall_back_to_library_events(self):
        with_runner = failing_members()
        write_trace(self.root / "a-with-test-trace", with_runner)
        without_runner = {name: events for name, events in with_runner.items() if name != "test.trace"}
        write_trace(self.root / "b-library-only", without_runner)
        lines = run(str(self.root), "--max-actions", "3").stdout.splitlines()
        first, second = lines.index("== a-with-test-trace"), lines.index("== b-library-only")
        self.assertIn('   FAILED action: Expect "toBeVisible"', lines[first:second])
        self.assertNotIn("Frame.goto", "\n".join(lines[first:second]))
        self.assertIn("   action: Frame.goto http://localhost:4173/projects/111/dashboard", lines[second:])
        self.assertIn('   FAILED action: Frame.expect internal:testid=[data-testid="dnd5e-stat-card"s]', lines[second:])

    def test_failed_step_is_the_leaf_not_its_enclosing_step(self):
        members = {"test.trace": [
            step("pw:api@1", 'Navigate to "/"'), done("pw:api@1"),
            step("step@2", "verify the stat card", method="test.step"),
            step("expect@3", 'Expect "toBeVisible"', method="expect", parent="step@2"),
            done("expect@3", error="Error: failed"),
            done("step@2", error="Error: failed"),
            {"type": "error", "message": "Error: failed", "stack": []},
        ]}
        write_trace(self.root / "nested-chromium", members)
        lines = run(str(self.root)).stdout.splitlines()
        self.assertIn("   action: verify the stat card", lines)
        self.assertIn('   FAILED action: Expect "toBeVisible"', lines)
        self.assertNotIn("   FAILED action: verify the stat card", lines)

    def test_error_outside_a_step_lists_the_actions_before_teardown(self):
        members = {
            "test.trace": [
                step("pw:api@1", 'Navigate to "/projects/21"'), done("pw:api@1"),
                step("pw:api@2", 'POST "/api/game-sessions/21/roll/cast/"'), done("pw:api@2"),
                {"type": "error", "message": "Error: Failed to cast a DM roll: 500 <!DOCTYPE html>",
                 "stack": [{"file": "/app/tests/helpers.js", "line": 660}]},
                step("hook@3", "After Hooks", method="hook"),
                step("pw:api@4", 'DELETE "/api/projects/21/"', parent="hook@3"), done("pw:api@4"),
                done("hook@3"),
                step("hook@5", "Worker Cleanup", method="hook"),
                step("fixture@6", 'Fixture "browser"', method="fixture", parent="hook@5"),
                done("fixture@6"), done("hook@5"),
            ],
            "1-trace.network": [resource("POST", "http://backend:8001/api/game-sessions/21/roll/cast/", 500)],
        }
        write_trace(self.root / "dm-roll-cast-chromium", members)
        proc = run(str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [
            "== dm-roll-cast-chromium",
            "   error: Error: Failed to cast a DM roll: 500 <!DOCTYPE html>",
            "   at /app/tests/helpers.js:660",
            "   no failed step recorded: the error was thrown outside a Playwright action",
            '   action: Navigate to "/projects/21"',
            '   action: POST "/api/game-sessions/21/roll/cast/"',
            "   http 500 POST http://backend:8001/api/game-sessions/21/roll/cast/",
        ])

    def test_unreadable_archives_are_reported_and_skipped(self):
        truncated = write_trace(self.root / "a-truncated-chromium", failing_members())
        truncated.write_bytes(truncated.read_bytes()[: truncated.stat().st_size // 2])
        corrupt = write_trace(self.root / "b-corrupt-member-chromium", failing_members(), zipfile.ZIP_STORED)
        data = corrupt.read_bytes()
        corrupt.write_bytes(data.replace(b"Before Hooks", b"Before Hookz", 1))
        write_trace(self.root / "c-readable-chromium", failing_members())

        proc = run(str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertTrue(lines[0].startswith(f"!! unreadable {truncated} — "), lines[0])
        self.assertTrue(lines[1].startswith(f"!! unreadable {corrupt} — "), lines[1])
        self.assertIn("Bad CRC-32", lines[1])
        self.assertEqual(lines[2], "== c-readable-chromium")
        self.assertIn('   FAILED action: Expect "toBeVisible"', lines[3:])

    def test_every_archive_unreadable_exits_1(self):
        broken = self.root / "x-chromium"
        broken.mkdir()
        (broken / "trace.zip").write_bytes(b"PK\x03\x04 not a zip")
        proc = run(str(self.root))
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stdout.startswith("!! unreadable "), proc.stdout)

    def test_no_trace_found_exits_2(self):
        (self.root / "empty-chromium").mkdir()
        proc = run(str(self.root))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("no trace.zip found", proc.stderr)


if __name__ == "__main__":
    unittest.main()
