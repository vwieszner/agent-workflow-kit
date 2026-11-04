"""End-to-end test of the layered-review pipeline mode without a Workflow runtime:
review_stage.py stages a round on a real git worktree, workflow_replay.mjs runs workflows/review.js with every
agent call answered from fixtures, review_findings_render.py renders the round and records it, and
review_findings_show / review_carry / review_precision read the result back. Needs node on PATH."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent
KIT = SCRIPTS.parent
WORKFLOW = KIT / "workflows" / "review.js"
REPLAY = SCRIPTS / "workflow_replay.mjs"
SPECS = "docs/implementation-artifacts"
NODE = shutil.which("node")

CONFIG = f"""
[project]
name = "demo"
[git]
base_branch = "main"
branch_prefix = "story/"
worktrees_root = "wts"
[paths]
specs_dir = "{SPECS}"
slot_registry = "{SPECS}/slot-registry.md"
quality_bar_doc = ""
[slots]
stack_prefix = "story-"
[stack]
runtime = "none"
[review]
required_preamble = "Report findings only — do NOT auto-apply patches — do NOT edit any files."
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout


def finding(kind: str = "general") -> dict:
    return {"title": "divide by zero on empty list", "location": "src/calc.py:2",
            "claim": "mean() divides by len(xs) without guarding an empty list",
            "evidence": "return sum(xs) / len(xs)", "kind": kind, "recommended": "patch"}


def answer(p: dict) -> dict | None:
    """A fixture report for one pending agent call."""
    t = p["agentType"]
    if t == "findings-verifier":
        ids = [line[4:].split(" — ")[0] for line in p["prompt"].splitlines() if line.startswith("### ")]
        return {"verdicts": [{"id": i, "verdict": "CONFIRMED", "evidence": "read src/calc.py:2",
                              "bar_impact": "n/a (no quality bar configured)"} for i in ids]}
    lines = int(p["prompt"].split(" lines)")[0].rsplit("(", 1)[-1].split(", ")[-1].split()[0]) \
        if " lines)" in p["prompt"] else 10**6
    return {"checked": "the whole chunk", "diff_lines_read": lines,
            "findings": [finding("edge" if t == "edge-case-hunter" else "general")] if t in
            ("blind-hunter", "edge-case-hunter") else []}


@unittest.skipUnless(NODE, "node is not installed")
class ReviewPipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)
        self.env = dict(os.environ, WORKFLOW_REPO_ROOT=str(self.root))
        git(self.root, "init", "-q", "-b", "main")
        (self.root / ".gitignore").write_text("wts/\n.workflow/state/\n", encoding="utf-8")
        (self.root / SPECS).mkdir(parents=True)
        (self.root / SPECS / "slot-registry.md").write_text(
            "| Slot | Status | Story ID | Branch | Worktree | Since |\n|---|---|---|---|---|---|\n"
            "| 1 | in_use | 1-1 | story/1-1 | wts/1-1 | 2026-01-01 |\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")
        self.wt = self.root / "wts" / "1-1"
        git(self.root, "worktree", "add", "-q", "-b", "story/1-1", str(self.wt))
        (self.wt / "src").mkdir()
        (self.wt / "src" / "calc.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n", encoding="utf-8")
        (self.wt / SPECS / "1-1.md").write_text("# Story 1-1\n\n## Acceptance Criteria\n\n### AC1 — mean\n",
                                                encoding="utf-8")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "feat: [Story 1-1] mean")

    def run_py(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True,
                              env=self.env, cwd=str(self.root))

    def stage(self) -> dict:
        p = self.run_py("review_stage.py", "--story-id", "1-1", "--mode", "controlled", "--worktree", str(self.wt),
                        "--spec-file", str(self.wt / SPECS / "1-1.md"), "--graph-project", "none",
                        "--stack-project-name", "none", "--date", "2026-01-02")
        self.assertEqual(0, p.returncode, p.stderr)
        return json.loads(p.stdout)

    def replay(self, out: Path) -> dict:
        for _ in range(20):
            p = subprocess.run([NODE, str(REPLAY), "run", str(WORKFLOW), str(out)], capture_output=True, text=True)
            if p.returncode == 0:
                return json.loads((out / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(10, p.returncode, p.stdout + p.stderr)
            for call in json.loads((out / "pending.json").read_text(encoding="utf-8")):
                self.assertTrue(call["dispatch_prompt"].startswith(call["prompt"]))
                f = out / "answer.json"
                f.write_text(json.dumps(answer(call)), encoding="utf-8")
                r = subprocess.run([NODE, str(REPLAY), "record", str(out), call["label"], str(f)],
                                   capture_output=True, text=True)
                self.assertEqual(0, r.returncode, r.stderr)
        self.fail("replay did not complete")

    def test_stage_replay_render_round_trip(self):
        staged = self.stage()
        args = staged["args"]
        self.assertEqual(1, args["round"])
        self.assertEqual("branch", args["delta"]["kind"])
        self.assertIn("src/calc.py", args["changed_files"])
        self.assertTrue(args["preamble"].startswith("Report findings only"))
        self.assertFalse(staged["seed_required"])
        out = Path(args["run_dir"]) / "replay"
        run = self.replay(out)
        self.assertEqual("review", run["workflowName"])
        result = run["result"]
        self.assertEqual(2, result["counts"]["raw"])
        self.assertEqual(1, result["counts"]["deduped"], "the two layers' findings on one line dedupe to one")
        self.assertEqual(1, result["counts"]["confirmed"])
        self.assertIn("ui-verifier (no ui_base_url)", result["skipped_layers"])
        self.assertEqual("no stack_project_name", result["repro_skipped"])

        p = self.run_py("review_findings_render.py", "--run-dir", str(out), "--story-id", "1-1", "--round", "1")
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn("review-render: OK", p.stdout)
        self.assertIn("| bar impact |", p.stdout)
        ffile = Path(result["findings_file"])
        text = ffile.read_text(encoding="utf-8")
        self.assertIn("## Review round 1 — 2026-01-02", text)
        self.assertIn("- bar impact: n/a (no quality bar configured)", text)
        record = (self.wt / SPECS / "1-1-record.md").read_text(encoding="utf-8")
        self.assertRegex(record, r"review round 1 recorded .*; reviewed branch [0-9a-f]{40}\.\.[0-9a-f]{40}")

        again = self.run_py("review_findings_render.py", "--run-dir", str(out), "--story-id", "1-1", "--round", "1")
        self.assertEqual(1, again.returncode, "a round is never rendered twice")

        show = self.run_py("review_findings_show.py", str(ffile), "R1-1", "--fields", "verification,bar")
        self.assertEqual(0, show.returncode, show.stderr)
        self.assertIn("CONFIRMED", show.stdout)
        self.assertIn("- bar impact:", show.stdout)

        carry = self.run_py("review_carry.py", "--findings-file", str(ffile), "--round", "2")
        self.assertEqual(0, carry.returncode, carry.stderr)
        carried = json.loads(carry.stdout)
        self.assertEqual(["R1-1"], [c["id"] for c in carried], "an untriaged round is carried into the next")
        self.assertEqual("n/a (no quality bar configured)", carried[0]["bar_impact"])

        precision = self.run_py("review_precision.py", str(self.wt / SPECS), "--json")
        self.assertEqual(0, precision.returncode, precision.stderr)
        rep = json.loads(precision.stdout)
        self.assertEqual("PARSED", rep["files"][0]["status"], rep["files"][0])
        self.assertEqual(1.0, rep["stories"]["1-1"]["all"]["precision_deduped"])

        staged2 = self.stage()
        self.assertEqual(2, staged2["args"]["round"])
        self.assertEqual(["R1-1"], [c["id"] for c in staged2["args"]["carried"]])

    def test_failed_layer_is_named_never_clean(self):
        args = self.stage()["args"]
        out = Path(args["run_dir"]) / "replay"
        for _ in range(20):
            p = subprocess.run([NODE, str(REPLAY), "run", str(WORKFLOW), str(out)], capture_output=True, text=True)
            if p.returncode == 0:
                break
            for call in json.loads((out / "pending.json").read_text(encoding="utf-8")):
                if call["agentType"] == "blind-hunter":
                    cmd = [NODE, str(REPLAY), "record", str(out), call["label"], "--error", "boom"]
                else:
                    f = out / "answer.json"
                    f.write_text(json.dumps(answer(call)), encoding="utf-8")
                    cmd = [NODE, str(REPLAY), "record", str(out), call["label"], str(f)]
                subprocess.run(cmd, check=True, capture_output=True)
        result = json.loads((out / "run.json").read_text(encoding="utf-8"))["result"]
        self.assertEqual(["blind-hunter"], result["failed_layers"])
        self.assertIn("agent error: boom", result["failed_reasons"]["blind-hunter"])

    def test_record_refuses_a_report_missing_a_required_key(self):
        args = self.stage()["args"]
        out = Path(args["run_dir"]) / "replay"
        p = subprocess.run([NODE, str(REPLAY), "run", str(WORKFLOW), str(out)], capture_output=True, text=True)
        self.assertEqual(10, p.returncode)
        call = json.loads((out / "pending.json").read_text(encoding="utf-8"))[0]
        f = out / "bad.json"
        f.write_text(json.dumps({"findings": []}), encoding="utf-8")
        r = subprocess.run([NODE, str(REPLAY), "record", str(out), call["label"], str(f)], capture_output=True,
                           text=True)
        self.assertEqual(2, r.returncode)
        self.assertIn("lacks required", r.stderr)


if __name__ == "__main__":
    unittest.main()
