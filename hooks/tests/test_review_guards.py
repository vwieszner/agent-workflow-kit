"""Review-pipeline guards, each run as a subprocess fed a JSON payload:
review_run_paths (blind-hunter reads only run diff files), review_code_paths (review agents read the story's
tree, not the main checkout or library source), repro_paths (repro/pbt writers: untracked repro files and the
scoped runner only), mutation_command (test-adequacy: the mutation runner only), playwright_paths (browser
outputs go into the agent's worktree) and review_coverage (a report waits for every diff line to be read)."""
from __future__ import annotations

import json
import subprocess
import unittest
import uuid
from pathlib import Path

from _hookenv import GUARDS, HookTestCase


class ReviewGuardBase(HookTestCase):
    extra_config = '[env]\nhost_tooling_runner = "uv run --no-project"\n'

    def setUp(self) -> None:
        super().setUp()
        cfg = (self.root / ".workflow" / "config.toml").read_text(encoding="utf-8")
        cfg = cfg.replace('worktrees_root = "../demo-worktrees"', 'worktrees_root = "wts"')
        cfg = cfg.replace("[review]\n", '[review]\nrepro_runner_options = ["--project-name", "--require-synced", "--tail"]\n')
        (self.root / ".workflow" / "config.toml").write_text(cfg, encoding="utf-8")
        self.wts = self.root / "wts"
        self.wt = self.wts / "1-1"
        self.wt.mkdir(parents=True)

    def denied(self, guard: str, agent: str | None, tool: str, tool_input: dict, **extra) -> bool:
        payload = {"tool_name": tool, "tool_input": tool_input, **extra}
        if agent:
            payload["agent_type"] = agent
        return self.json_denied(GUARDS / guard, payload)


class RunPaths(ReviewGuardBase):
    def test_blind_hunter_reads_only_run_diff_files(self):
        run = self.wt / ".workflow" / "state" / "review-runs" / "round-2"
        for name in ("diff.patch", "diff-3.patch"):
            self.assertFalse(self.denied("review_run_paths.py", "blind-hunter", "Read", {"file_path": str(run / name)}))
        main_run = self.root / ".workflow" / "state" / "review-runs" / "direct-1"
        self.assertFalse(self.denied("review_run_paths.py", "blind-hunter", "Read", {"file_path": str(main_run / "diff.patch")}))
        for path in (run / "args.json", self.wt / "src" / "a.py", run / ".." / "round-2" / "diff.patch",
                     Path("/tmp/x/.workflow/state/review-runs/r/diff.patch")):
            self.assertTrue(self.denied("review_run_paths.py", "blind-hunter", "Read", {"file_path": str(path)}), path)

    def test_other_agents_and_tools_pass(self):
        self.assertFalse(self.denied("review_run_paths.py", "edge-case-hunter", "Read", {"file_path": "/etc/hosts"}))
        self.assertFalse(self.denied("review_run_paths.py", "blind-hunter", "Grep", {"pattern": "x"}))


class CodePaths(ReviewGuardBase):
    def test_worktree_passes_main_checkout_denied_except_review_prompts(self):
        a = "edge-case-hunter"
        self.assertFalse(self.denied("review_code_paths.py", a, "Read", {"file_path": str(self.wt / "src" / "a.py")}))
        self.assertFalse(self.denied("review_code_paths.py", a, "Grep", {"pattern": "x", "path": str(self.wt)}))
        self.assertTrue(self.denied("review_code_paths.py", a, "Read", {"file_path": str(self.root / "src" / "a.py")}))
        prompts = self.root / ".claude" / "skills" / "layered-review" / "review-prompts" / "edge-case-hunter.md"
        self.assertFalse(self.denied("review_code_paths.py", a, "Read", {"file_path": str(prompts)}))
        self.assertTrue(self.denied("review_code_paths.py", a, "Read",
                                    {"file_path": str(self.wts) + "/../src/a.py"}), "traversal into the main checkout")

    def test_missing_or_relative_path_and_library_source_denied(self):
        a = "findings-verifier"
        self.assertTrue(self.denied("review_code_paths.py", a, "Grep", {"pattern": "x"}))
        self.assertTrue(self.denied("review_code_paths.py", a, "Glob", {"pattern": "*.py", "path": "src"}))
        self.assertTrue(self.denied("review_code_paths.py", a, "Read",
                                    {"file_path": str(self.wt / "node_modules" / "lib" / "i.js")}))
        self.assertTrue(self.denied("review_code_paths.py", a, "Glob",
                                    {"pattern": "**/site-packages/**", "path": str(self.wt)}))
        self.assertFalse(self.denied("review_code_paths.py", a, "Read", {"file_path": str(self.wt / "package-lock.json")}))

    def test_unscoped_agents_pass(self):
        self.assertFalse(self.denied("review_code_paths.py", "story-impl", "Grep", {"pattern": "x"}))
        self.assertFalse(self.denied("review_code_paths.py", None, "Grep", {"pattern": "x"}))


class ReproPaths(ReviewGuardBase):
    def setUp(self) -> None:
        super().setUp()
        for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", "-C", str(self.wt), *args], check=True, capture_output=True)
        (self.wt / "src" / "tests").mkdir(parents=True)
        tracked = self.wt / "src" / "tests" / "test_repro_r1_1.py"
        tracked.write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.wt), "add", "-A"], check=True, capture_output=True)

    def runner(self, args: str, path: str | None = None) -> str:
        return f"uv run --no-project {path or self.root / '.workflow' / 'scripts' / 'run_scoped_tests.py'} {args}"

    def test_runner_only(self):
        a = "repro-writer"
        self.assertFalse(self.denied("repro_paths.py", a, "Bash", {"command": self.runner("--project-name story-1-1 t.x")}))
        self.assertTrue(self.denied("repro_paths.py", a, "Bash", {"command": self.runner("--record x")}))
        self.assertTrue(self.denied("repro_paths.py", a, "Bash", {"command": self.runner("x; rm -rf /")}))
        self.assertTrue(self.denied("repro_paths.py", a, "Bash", {"command": self.runner("--tail=-1 x")}))
        self.assertTrue(self.denied("repro_paths.py", a, "Bash",
                                    {"command": self.runner("x", str(self.wt / ".workflow" / "scripts" / "run_scoped_tests.py"))}),
                        "a worktree copy of the runner")
        self.assertTrue(self.denied("repro_paths.py", a, "Bash", {"command": "cat /etc/passwd"}))

    def test_untracked_repro_files_only(self):
        a = "pbt-writer"
        ok = self.wt / "src" / "tests" / "test_pbt_r1_2.py"
        self.assertFalse(self.denied("repro_paths.py", a, "Write", {"file_path": str(ok)}))
        self.assertTrue(self.denied("repro_paths.py", a, "Write", {"file_path": str(self.wt / "src" / "tests" / "test_repro_r1_1.py")}),
                        "a tracked file")
        self.assertTrue(self.denied("repro_paths.py", a, "Write", {"file_path": str(self.wt / "src" / "test_pbt_r1_2.py")}),
                        "a Python repro outside a tests directory")
        self.assertTrue(self.denied("repro_paths.py", a, "Edit", {"file_path": str(self.wt / "src" / "app.py")}))
        self.assertTrue(self.denied("repro_paths.py", a, "Write", {"file_path": str(self.root / "src" / "tests" / "test_pbt_r1_2.py")}))
        plain = self.wts / "notes"
        (plain / "tests").mkdir(parents=True)
        self.assertTrue(self.denied("repro_paths.py", a, "Write", {"file_path": str(plain / "tests" / "test_pbt_r1_2.py")}),
                        "a folder that is no git worktree root")
        self.assertFalse(self.denied("repro_paths.py", a, "Write", {"file_path": str(self.wt / "web" / "repro_r1_3.test.jsx")}))


class MutationCommand(ReviewGuardBase):
    def test_mutation_runner_only(self):
        runner = f"uv run --no-project {self.root / '.workflow' / 'scripts' / 'run_mutation.py'}"
        self.assertFalse(self.denied("mutation_command.py", "test-adequacy", "Bash",
                                     {"command": f"{runner} --project-name story-1 --worktree /w --diff-range a..b --module src/a.py --test src/t.py"}))
        self.assertTrue(self.denied("mutation_command.py", "test-adequacy", "Bash", {"command": f"{runner} --help"}))
        self.assertTrue(self.denied("mutation_command.py", "test-adequacy", "Bash", {"command": "ls"}))
        self.assertFalse(self.denied("mutation_command.py", "story-impl", "Bash", {"command": "ls"}))


class TranscriptGuards(ReviewGuardBase):
    def transcript(self, prompt: str, reads: list[tuple[str, int, int]] = ()) -> dict:
        aid = uuid.uuid4().hex
        t = self.root / "session" / "subagents" / f"agent-{aid}.jsonl"
        t.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"message": {"role": "user", "content": prompt}}]
        for i, (path, off, lim) in enumerate(reads):
            rows.append({"message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"u{i}", "name": "Read", "input": {"file_path": path, "offset": off, "limit": lim}}]}})
            rows.append({"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{i}"}]}})
        t.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return {"transcript_path": str(t), "agent_id": aid, "session_id": "s"}

    def test_playwright_outputs_stay_in_the_worktree(self):
        extra = self.transcript(f"story_id: 1-1\nworktree_path: {self.wt}\n")
        tool = "mcp__playwright__browser_take_screenshot"
        good = f"{self.wt}/.playwright-mcp/round-1/home-empty.png"
        self.assertFalse(self.denied("playwright_paths.py", "ui-verifier", tool, {"filename": good}, **extra))
        self.assertTrue(self.denied("playwright_paths.py", "ui-verifier", tool, {"filename": "shot.png"}, **extra))
        self.assertTrue(self.denied("playwright_paths.py", "ui-verifier", tool, {}, **extra))
        code = "async (page) => page.screenshot({ path: '/tmp/x.png' })"
        self.assertTrue(self.denied("playwright_paths.py", "ui-verifier", "mcp__playwright__browser_run_code_unsafe",
                                    {"code": code}, **extra))
        self.assertFalse(self.denied("playwright_paths.py", None, tool, {"filename": "shot.png"}, **extra))

    def test_coverage_denies_until_every_line_is_read(self):
        diff = self.wt / ".workflow" / "state" / "review-runs" / "round-1" / "diff-1.patch"
        diff.parent.mkdir(parents=True)
        diff.write_text("".join(f"line {i}\n" for i in range(50)), encoding="utf-8")
        prompt = f"Diff file: {diff} (chunk 1 of 1, 50 lines)."
        partial = self.transcript(prompt, [(str(diff), 1, 20)])
        p = self.run_hook(GUARDS / "review_coverage.py",
                          {"tool_name": "StructuredOutput", "agent_type": "blind-hunter", **partial})
        self.assertIn(b'"deny"', p.stdout)
        self.assertIn(b"offset 21", p.stdout)
        full = self.transcript(prompt, [(str(diff), 1, 20), (str(diff), 21, 30)])
        self.assertFalse(self.denied("review_coverage.py", "blind-hunter", "StructuredOutput", {}, **full))
        log = (self.root / ".workflow" / "state" / "review-coverage.jsonl").read_text(encoding="utf-8")
        self.assertIn('"deny"', log)
        self.assertIn('"allow"', log)


if __name__ == "__main__":
    unittest.main()
