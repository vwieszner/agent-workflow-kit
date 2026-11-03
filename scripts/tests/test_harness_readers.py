"""retro_digest, journal_grep, transcript_calls, denial_samples, session_cost_report and
proposal_drift, run as subprocesses against a throwaway repo and a fake Claude transcript root."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import _transcripts  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wf-readers-")).resolve()
        self.cc = Path(tempfile.mkdtemp(prefix="wf-cc-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.cc, True)
        (self.root / ".workflow").mkdir()
        (self.root / ".workflow" / "config.toml").write_text(
            '[project]\nname = "demo"\n[paths]\nscripts_index = "scripts/INDEX.md"\n'
            f'[transcripts]\nclaude_root = "{self.cc.as_posix()}"\nclaude_project_globs = ["proj*"]\n',
            encoding="utf-8")
        self.jdir = self.root / ".workflow" / "state" / "journal"
        self.jdir.mkdir(parents=True)

    def run_script(self, name, *args):
        env = dict(os.environ, WORKFLOW_REPO_ROOT=str(self.root))
        p = subprocess.run([sys.executable, str(SCRIPTS / name), *args], capture_output=True, text=True,
                           encoding="utf-8", env=env, cwd=str(self.root))
        return p

    def journal(self, name, facts):
        with open(self.jdir / name, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "t", "facts": facts}) + "\n")


class HelperTests(unittest.TestCase):
    def test_slug(self):
        self.assertEqual("D--work-repo", _transcripts.slug("D:\\work\\repo"))
        self.assertEqual("-home-u-repo", _transcripts.slug("/home/u/repo"))

    def test_base_role_default(self):
        self.assertEqual("story-impl", _transcripts.base_role("story-impl-max"))
        self.assertEqual("other-max", _transcripts.base_role("other-max"))


class JournalReaderTests(Base):
    def setUp(self):
        super().setUp()
        err = {"ts": "2026-01-01T00:00:02", "kind": "tool_error", "tool": "Bash", "snippet": "Exit code 1\nboom 42"}
        cmd = {"ts": "2026-01-01T00:00:01", "kind": "command_run", "tool": "Bash", "command": "cd x && git status"}
        self.journal("s1.jsonl", [cmd, err, {"ts": "2026-01-01T00:00:03", "kind": "user_message", "text": "fix it"}])
        self.journal("s1.sub-a1.jsonl", [{"ts": "2026-01-01T00:00:00", "kind": "agent_started",
                                         "agent_id": "a1", "agent_type": "story-impl-xhigh"},
                                        {"ts": "2026-01-01T00:00:04", "kind": "command_run", "command": "pytest -q"},
                                        {"ts": "2026-01-01T00:00:05", "kind": "tool_error", "snippet": "boom 7"}])

    def test_journal_grep_error_with_prev_command_and_agent_filter(self):
        out = self.run_script("journal_grep.py", "error", "boom").stdout
        self.assertIn("prev cmd: 'cd x && git status'", out)
        self.assertIn("story-impl", out)
        out = self.run_script("journal_grep.py", "command", "pytest", "--agent-type", "story-impl").stdout
        self.assertIn("CMD 'pytest -q'", out)
        self.assertNotIn("git status", out)
        out = self.run_script("journal_grep.py", "command", ".", "--agent-type", "main").stdout
        self.assertNotIn("pytest", out)

    def test_retro_digest_clusters(self):
        p = self.run_script("retro_digest.py", "--min-errors", "2", "--min-commands", "1")
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn("1 unmined session(s), 4 new fact(s)", p.stdout)
        self.assertIn("git status", p.stdout)
        self.assertIn("'fix it'", p.stdout)
        markers = self.jdir / ".markers"
        markers.mkdir()
        (markers / "s1.json").write_text('{"analyzed_facts": 4, "launched_facts": 0}', encoding="utf-8")
        self.assertIn("no unmined facts", self.run_script("retro_digest.py").stdout)


class TranscriptFixture(Base):
    def setUp(self):
        super().setUp()
        proj = self.cc / "proj-a"
        sub = proj / "sess1" / "subagents"
        sub.mkdir(parents=True)
        main = [
            {"type": "assistant", "timestamp": "2026-02-01T10:00:00Z", "requestId": "r1",
             "message": {"model": "claude-x", "usage": {"input_tokens": 100, "output_tokens": 10},
                         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                      "input": {"command": "git push --force"}}]}},
            {"type": "user", "timestamp": "2026-02-01T10:00:01Z", "toolDenialKind": "permission-rule",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "denied by hook"}]}},
        ]
        (proj / "sess1.jsonl").write_text("\n".join(json.dumps(x) for x in main) + "\n", encoding="utf-8")
        (sub / "agent-a1.jsonl").write_text("\n".join(json.dumps(x) for x in main) + "\n", encoding="utf-8")
        (sub / "agent-a1.meta.json").write_text('{"agentType": "story-impl-max"}', encoding="utf-8")
        self.guard = self.root / "guard.py"
        self.guard.write_text("def check(p):\n    c = p['tool_input'].get('command', '')\n"
                              "    return 'no force' if '--force' in c else None\n", encoding="utf-8")
        self.allow = self.root / "allow.py"
        self.allow.write_text("import sys\ndef evaluate(p):\n    return 0\n", encoding="utf-8")

class TranscriptTests(TranscriptFixture):
    def test_grep_counts_by_agent(self):
        p = self.run_script("transcript_calls.py", "grep", "--tool", "Bash", "--since", "2026-01-01",
                            "--count-by", "agent", "--result", "denied")
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn("2 matching call(s)", p.stdout)
        self.assertIn("story-impl", p.stdout)

    def test_replay_against_baseline_and_history(self):
        p = self.run_script("transcript_calls.py", "replay", "--hook", str(self.allow), "--since", "2026-01-01")
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn("deny -> allow", p.stdout)
        p = self.run_script("transcript_calls.py", "replay", "--hook", str(self.guard), "--baseline",
                            str(self.allow), "--since", "2026-01-01")
        self.assertIn("allow -> deny", p.stdout)
        self.assertIn("CHANGED", p.stdout)

    def test_replay_refuses_live_state_module(self):
        live = self.root / "phase_dispatch.py"
        live.write_text("def check(p):\n    return None\n", encoding="utf-8")
        p = self.run_script("transcript_calls.py", "replay", "--hook", str(live), "--since", "2026-01-01")
        self.assertEqual(2, p.returncode)

    def test_denial_samples(self):
        out = self.run_script("denial_samples.py", "story-impl").stdout
        self.assertIn("DENY: denied by hook", out)

    def test_session_cost_report(self):
        out_dir = self.root / "report"
        p = self.run_script("session_cost_report.py", "--out", str(out_dir))
        self.assertEqual(0, p.returncode, p.stderr)
        rows = json.loads((out_dir / "transcripts.json").read_text(encoding="utf-8"))
        self.assertEqual({"main", "sub"}, {r["kind"] for r in rows})


class HarnessMetricsTests(TranscriptFixture):
    def test_dedicated_session_attributed(self):
        self.journal("sess1.jsonl", [{"ts": "2026-02-01T09:00:00Z", "kind": "command_run",
                                      "command": "story_ledger.py 1-2"}])
        p = self.run_script("harness_metrics.py", "--story", "1-2", "--json")
        self.assertEqual(0, p.returncode, p.stderr)
        rep = json.loads(p.stdout)[0]
        self.assertEqual("1-2", rep["story"])
        self.assertEqual("DEDICATED", rep["sessions"][0]["class"])
        self.assertGreaterEqual(rep["total"]["requests"], 1)

    def test_missing_dirs_exit_1(self):
        shutil.rmtree(self.cc / "proj-a")
        self.assertEqual(1, self.run_script("harness_metrics.py", "--story", "1-2").returncode)


class ProposalDriftTests(Base):
    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))

    def test_states(self):
        self.git("init", "-q")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
        self.git("add", "scripts/tool.py")
        self.git("commit", "-qm", "add")
        pdir = self.root / ".workflow" / "state" / "proposals"
        pdir.mkdir(parents=True)
        (pdir / "script-tool.md").write_text(
            "---\nkind: script\ntitle: T\nrun_id: 20000101-000000\ntarget: scripts/tool.py + scripts/INDEX.md row\n---\n",
            encoding="utf-8")
        (pdir / "script-tool.draft.py").write_text("print(1)\n", encoding="utf-8")
        (pdir / "script-new.md").write_text(
            "---\nkind: script\nrun_id: 29990101-000000\ntarget: scripts/new_tool.py\n---\n", encoding="utf-8")
        out = self.run_script("proposal_drift.py").stdout
        self.assertIn("scripts/tool.py", out)
        self.assertIn("ADDED-SINCE", out)
        self.assertIn("scripts/INDEX.md                         index (not checked)", out)
        self.assertIn("scripts/new_tool.py                      NEW-ABSENT", out)
        (pdir / "script-tool.md").write_text(
            "---\nkind: script\nrun_id: 29990101-000000\ntarget: scripts/tool.py\n---\n", encoding="utf-8")
        self.assertIn("LANDED", self.run_script("proposal_drift.py").stdout)

    def test_missing_dir_exits_2(self):
        self.assertEqual(2, self.run_script("proposal_drift.py", "--proposals", str(self.root / "nope")).returncode)


if __name__ == "__main__":
    unittest.main()
