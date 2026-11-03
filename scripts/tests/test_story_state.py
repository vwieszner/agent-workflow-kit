"""Tests for story_state.py: the per-story read model and its attention queue.

Pins:
  * the in-flight set (sprint-status in-progress/review/blocked + slot-registry in_use, `--story` replacing it);
  * every section carries `status` and `source`; optional sources that are not configured read `none` ("n/a");
  * the stack section is a `none` when `config: stack.runtime = "none"`;
  * the session probe: `config: story_state.sessions_cmd` is run and each entry is linked by name or worktree cwd;
    with an OpenCode driver and no command the probe is "unavailable" (a `none` section);
  * the attention queue: open questions (owner vs external-tagged), spec-draft and record-halt, driver-halt with
    the waits-on rule over the design-question tracker, external-interim rows, propagation rows, blocked rows;
  * `waits_on` and `failure_blocks` as pure functions;
  * the CLI contract (exit 0 with JSON; exit 2 on a usage error).

Git runs for real on a throwaway main checkout; the ledger and cost subprocesses are faked through `story_state.run`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import story_state as ss  # noqa: E402

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(cwd, *args) -> str:
    r = subprocess.run(["git", *GIT_ID, "-C", str(cwd), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout


CONFIG = """
[project]
name = "proj"
[git]
base_branch = "main"
branch_prefix = "story/"
worktrees_root = "../proj-worktrees"
[stack]
runtime = "none"
[paths]
design_questions = "docs/design-questions.md"
design_ticket_regex = "\\\\bDQ-\\\\d+\\\\b"
[spec]
external_question_tags = ["Designer"]
[loop]
driver_tool = "claude"
"""


class World(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "README.md").write_text("x\n")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-q", "-m", "init")
        self.arts = self.root / "docs" / "implementation-artifacts"
        self.arts.mkdir(parents=True)
        (self.root / ".workflow" / "state" / "journal").mkdir(parents=True)
        self.registry([])
        self.tracker({"1-1-login": "in-progress", "1-2-signup": "blocked", "1-3-done": "done", "epic-1": "in-progress"})
        self.fake_out: dict[str, tuple] = {}
        real_run = ss.run

        def fake_run(argv, *, cwd=None, env=None, timeout):
            text = " ".join(str(a) for a in argv)
            for needle, answer in self.fake_out.items():
                if needle in text:
                    return answer
            if "story_ledger.py" in text:
                return 0, "**Position:** story-impl | rounds observed: 1 | next: review\n**RESUMABLE: YES**\n", ""
            if "harness_metrics.py" in text:
                return 1, "", "no transcripts"
            return real_run(argv, cwd=cwd, env=env, timeout=timeout)

        patcher = mock.patch.object(ss, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        ss.configure(self.root)

    def registry(self, rows):
        lines = ["# Slot registry", "", "| Slot | Status | Story ID | Branch | Worktree | Since |",
                 "|---|---|---|---|---|---|"]
        lines += [f"| {s} | {st} | {sid} | {b} | {w} | 2026-01-01 |" for s, st, sid, b, w in rows]
        (self.root / "docs" / "implementation-artifacts").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / "implementation-artifacts" / "slot-registry.md").write_text("\n".join(lines) + "\n")

    def tracker(self, rows: dict):
        body = "development_status:\n" + "".join(f"  {k}: {v}\n" for k, v in rows.items())
        (self.root / "docs" / "implementation-artifacts").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / "implementation-artifacts" / "sprint-status.yaml").write_text(body)

    def worktree(self, story: str) -> Path:
        path = self.root.parent / f"{self.root.name}-wt-{story}"
        git(self.root, "worktree", "add", "-q", "-b", f"story/{story}", str(path))
        self.addCleanup(lambda: subprocess.run(["git", "-C", str(self.root), "worktree", "remove", "--force",
                                                str(path)], capture_output=True))
        return path

    def snapshot(self, **kw) -> dict:
        return ss.build_snapshot(**{"with_cost": False, **kw})

    def story(self, doc, sid):
        return next(s for s in doc["stories"] if s["story_id"] == sid)


def every_section_named(test, value, path="$"):
    """Every dict carrying `status` also carries `source`; no section holds a bare empty value."""
    if isinstance(value, dict):
        if "status" in value and value["status"] in ("ok", "none", "error"):
            test.assertIn("source", value, path)
            if value["status"] == "none":
                test.assertTrue(value.get("reason"), path)
            if value["status"] == "error":
                test.assertTrue(value.get("error"), path)
        for k, v in value.items():
            every_section_named(test, v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            every_section_named(test, v, f"{path}[{i}]")


class SnapshotTests(World):
    def test_in_flight_set_is_tracker_and_registry_without_epics_or_done(self):
        wt = self.worktree("1-4")
        self.registry([(1, "in_use", "1-4", "story/1-4", str(wt)), (2, "free", "", "", "")])
        doc = self.snapshot()
        ids = sorted(s["story_id"] for s in doc["stories"])
        self.assertEqual(ids, ["1-1", "1-2", "1-4"])
        s = self.story(doc, "1-4")
        self.assertEqual(s["slot"]["status"], "ok")
        self.assertEqual(s["slot"]["project"], "story-1-4")
        self.assertEqual(s["worktree"]["found_by"], "slot-registry")
        self.assertEqual(s["git"]["status"], "ok")
        self.assertEqual(s["git"]["base"], "main")
        every_section_named(self, doc)

    def test_story_flag_replaces_the_set(self):
        doc = self.snapshot(story_ids=["1-1"])
        self.assertEqual([s["story_id"] for s in doc["stories"]], ["1-1"])

    def test_no_stack_and_no_graph_read_as_not_applicable(self):
        s = self.story(self.snapshot(), "1-1")
        self.assertEqual(s["stack"]["services"]["status"], "none")
        self.assertIn("n/a", s["stack"]["services"]["reason"])
        self.assertEqual(s["stack"]["graph_project"]["status"], "none")
        self.assertEqual(s["cost"]["status"], "none")

    def test_position_comes_from_the_ledger_and_record_lines_are_read(self):
        (self.arts / "1-1-record.md").write_text("- 2026-01-02 checkpoint-1 spec approved\n"
                                                 "- 2026-01-03 review round 1 recorded; reviewed branch x\n")
        s = self.story(self.snapshot(), "1-1")
        self.assertEqual(s["position"]["status"], "ok")
        self.assertEqual(s["position"]["rounds_observed"], 1)
        self.assertEqual(s["record"]["status"], "ok")
        self.assertEqual(s["position"]["recorded_rounds"], [1])

    def test_spec_and_deferred_are_found_under_the_configured_dirs(self):
        (self.arts / "1-1-login.md").write_text("# Story 1.1\n")
        (self.arts / "deferred-work").mkdir()
        (self.arts / "deferred-work" / "story-1-1.md").write_text("## Round 1\n### D1\n### D2\n")
        s = self.story(self.snapshot(), "1-1")
        self.assertEqual(s["artifacts"]["spec"]["status"], "ok")
        self.assertEqual(s["artifacts"]["deferred"]["sections"], [{"heading": "Round 1", "entries": 2}])

    def test_running_agents_come_from_the_journal_liveness_facts(self):
        wt = self.worktree("1-4")
        self.registry([(1, "in_use", "1-4", "story/1-4", str(wt))])
        j = self.root / ".workflow" / "state" / "journal" / "s1.sub-a1.jsonl"
        j.write_text(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "kind": "agent_started", "agent_id": "a1",
                                 "agent_type": "story-impl", "cwd": str(wt)}) + "\n")
        doc = self.snapshot()
        running = self.story(doc, "1-4")["agents"]["running"]
        self.assertEqual(running["status"], "ok")
        self.assertEqual([a["agent_type"] for a in running["agents"]], ["story-impl"])
        self.assertEqual(running["agents"][0]["attributed_by"], "worktree cwd")

    def test_driver_runs_are_read_from_the_state_dir(self):
        run = self.root / ".workflow" / "state" / "auto-dev" / "20260101-120000"
        run.mkdir(parents=True)
        (run / "driver.log").write_text("[12:00:00] start\n[12:01:00] session 1\n")
        (run / "sessions.jsonl").write_text(json.dumps({"session": 1, "story": "1-1", "cost_usd": 1.5}) + "\n")
        doc = self.snapshot()
        runs = doc["driver_runs"]
        self.assertEqual(runs["status"], "ok")
        self.assertEqual(runs["runs"][0]["spent_usd"]["usd"], 1.5)
        self.assertEqual(self.story(doc, "1-1")["driver_run_ids"]["ids"], ["20260101-120000"])


class SessionProbeTests(World):
    def listing(self, items):
        te_root = self.root
        script = te_root / "list_sessions.py"
        script.write_text("import json\nprint(json.dumps(%r))\n" % items)
        cfg = (te_root / ".workflow" / "config.toml")
        cfg.write_text(cfg.read_text().replace('[loop]\ndriver_tool = "claude"',
                                               f'[loop]\ndriver_tool = "claude"\n[story_state]\n'
                                               f'sessions_cmd = ["{sys.executable}", "{script}"]'))
        te.clear_caches()

    def test_sessions_are_linked_by_name_and_cwd(self):
        wt = self.worktree("1-4")
        self.registry([(1, "in_use", "1-4", "story/1-4", str(wt))])
        self.listing([{"id": "aaaaaaaa", "name": "1-1", "kind": "background", "cwd": str(self.root),
                       "state": "working", "status": "busy", "pid": 1},
                      {"id": "bbbbbbbb", "name": "x", "kind": "interactive", "cwd": str(wt), "status": "busy"}])
        doc = self.snapshot()
        entries = doc["sessions"]["entries"]
        by_id = {e["id"]: e for e in entries}
        self.assertEqual(by_id["aaaaaaaa"]["linked_by"], "name")
        self.assertEqual(by_id["bbbbbbbb"]["linked_by"], "cwd")
        self.assertEqual(self.story(doc, "1-4")["session_ids"]["ids"], ["bbbbbbbb"])

    def test_opencode_without_a_command_is_unavailable(self):
        cfg = self.root / ".workflow" / "config.toml"
        cfg.write_text(cfg.read_text().replace('driver_tool = "claude"', 'driver_tool = "opencode"'))
        te.clear_caches()
        doc = ss.build_attention()
        self.assertEqual(doc["sessions"]["status"], "none")
        self.assertIn("unavailable", doc["sessions"]["reason"])
        self.assertEqual(doc["attention"]["sources"]["sessions"]["status"], "none")
        every_section_named(self, doc)

    def test_a_waiting_background_session_is_queued_for_the_owner(self):
        self.listing([{"id": "cccccccc", "sessionId": "s-1", "name": "1-1", "kind": "background",
                       "cwd": str(self.root), "state": "blocked", "status": "waiting", "waitingFor": "permission",
                       "pid": 9}])
        entries = ss.build_attention()["attention"]["entries"]
        session = [e for e in entries if e["kind"] == "session"]
        self.assertEqual(len(session), 1)
        self.assertEqual(session[0]["asked_of"], "owner")
        self.assertEqual(session[0]["mark"], "waiting")


class AttentionTests(World):
    def queue(self) -> list[dict]:
        cfg = self.root / ".workflow" / "config.toml"
        cfg.write_text(cfg.read_text().replace('driver_tool = "claude"', 'driver_tool = "none-tool"'))
        te.clear_caches()
        ss.configure(self.root)
        doc = ss.build_attention()
        every_section_named(self, doc)
        return doc["attention"]["entries"]

    def test_open_questions_split_owner_and_external(self):
        (self.arts / "1-1-login.md").write_text(
            "# Spec\n- [OPEN-Q 1] which flow? (recommend A)\n- [OPEN-Q 2 (Designer)] which icon?\n")
        found = [e for e in self.queue() if e["kind"] == "open-question"]
        self.assertEqual(sorted((e["marker"], e["asked_of"]) for e in found),
                         [("[OPEN-Q 1]", "owner"), ("[OPEN-Q 2 (Designer)]", "external")])

    def test_record_halt_and_spec_draft(self):
        (self.arts / "1-1-record.md").write_text("- 2026-01-02 spec-draft written\n"
                                                 "- 2026-01-03 HALT: needs a decision on the API\n")
        kinds = {e["kind"] for e in self.queue() if e["story"] == "1-1"}
        self.assertIn("record-halt", kinds)

    def test_a_ruled_halt_is_not_queued(self):
        (self.arts / "1-1-record.md").write_text("- 2026-01-03 HALT resolved: owner ruling A\n")
        self.assertFalse([e for e in self.queue() if e["kind"] == "record-halt"])

    def failure(self, story, extra=""):
        log = self.arts / "auto-dev-failures.md"
        log.write_text("# Failures\n\n" + textwrap.dedent(f"""\
            ## {story} — 2026-01-05T10:00:00 — story-spec

            - branch: story/{story} (PRESERVED — checkout to inspect)
            - failure_step: spec-open-questions
            - failure_summary: two questions
            {extra}
            """))

    def tracker_file(self, rows):
        (self.root / "docs" / "design-questions.md").write_text(
            "# Questions\n\n| Ticket | Item | Blocks | Status |\n|---|---|---|---|\n"
            + "".join(f"| {t} | item | {b} | {s} |\n" for t, b, s in rows))

    def test_blocked_row_waits_on_external_until_the_ticket_is_done(self):
        self.failure("1-2", "- asked_of: external\n- ticket: DQ-7")
        self.tracker_file([("DQ-7", "1-2", "open")])
        row = next(e for e in self.queue() if e["kind"] == "blocked-row")
        self.assertEqual((row["asked_of"], row["ticket"], row["ticket_status"]), ("external", "DQ-7", "open"))
        self.tracker_file([("DQ-7", "1-2", "✅ answered")])
        row = next(e for e in self.queue() if e["kind"] == "blocked-row")
        self.assertEqual(row["asked_of"], "owner")

    def test_driver_halt_for_an_in_progress_story_names_the_kept_branch(self):
        self.failure("1-1")
        git(self.root, "branch", "story/1-1")
        halt = next(e for e in self.queue() if e["kind"] == "driver-halt")
        self.assertEqual(halt["asked_of"], "owner")
        self.assertEqual(halt["branch"]["name"], "story/1-1")
        self.assertTrue(halt["branch"]["exists"])

    def test_external_interim_rows_and_unconfigured_tracker(self):
        self.tracker_file([("DQ-3", "⚠ 1-1 ships an interim", "open"), ("DQ-4", "1-1", "open")])
        found = [e for e in self.queue() if e["kind"] == "external-interim"]
        self.assertEqual([(e["story"], e["ticket"]) for e in found], [("1-1", "DQ-3")])
        cfg = self.root / ".workflow" / "config.toml"
        cfg.write_text(cfg.read_text().replace('design_questions = "docs/design-questions.md"', 'design_questions = ""'))
        te.clear_caches()
        ss.configure(self.root)
        doc = ss.build_attention()
        source = doc["attention"]["sources"]["external tracker"]
        self.assertEqual(source["status"], "none")
        self.assertIn("n/a", source["reason"])

    def test_propagation_rows_are_queued_until_decided(self):
        setup = self.root / ".workflow" / "state" / "story-setup"
        setup.mkdir(parents=True)
        (setup / "propagation-1-1.md").write_text("| P1 | docs/a.md | 3-5 | x | spec | edit |\n")
        rows = [e for e in self.queue() if e["kind"] == "propagation-row"]
        self.assertEqual(len(rows), 1)
        (setup / "propagation-1-1.md").write_text("| P1 | docs/a.md | 3-5 | x | spec | edit |\n## Decisions\n")
        self.assertFalse([e for e in self.queue() if e["kind"] == "propagation-row"])


class PureRuleTests(unittest.TestCase):
    def test_waits_on(self):
        self.assertEqual(ss.waits_on(None, None), ("owner", None))
        self.assertEqual(ss.waits_on({"asked_of": "owner"}, None), ("owner", None))
        old = ss.TICKET_RE
        ss.TICKET_RE = __import__("re").compile(r"\bDQ-\d+\b")
        try:
            self.assertEqual(ss.waits_on({"ticket": "DQ-1"}, None), ("external", None))
            rows = [{"ticket": "DQ-1", "status": "Done"}]
            self.assertEqual(ss.waits_on({"asked_of": "both", "ticket": "DQ-1"}, rows), ("owner", "Done"))
            self.assertEqual(ss.waits_on({"asked_of": "external", "ticket": "DQ-2"}, rows), ("external", None))
        finally:
            ss.TICKET_RE = old

    def test_failure_blocks_reads_the_leading_field_group_only(self):
        lines = ss.failure_lines("## 1-1 — 2026-01-01T00:00:00 — x\n\n- failure_step: s\n- failure_summary: a\n"
                                 "  - asked_of: external\n\nprose\n- asked_of: both\n")
        block = ss.failure_blocks(lines)[0]
        self.assertEqual(block["fields"], {"failure_step": "s", "failure_summary": "a"})


class CliTests(World):
    def test_cli_prints_json_and_rejects_bad_usage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ss.main(["--attention", "--story", "1-1"]), 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "error")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ss.main(["--no-cost", "--story", "1-1"]), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["main_repo"], str(self.root))


if __name__ == "__main__":
    unittest.main()
