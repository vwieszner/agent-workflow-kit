"""halt_release.py and resume_halted.py against a real git repo with a story worktree
(stack.runtime = "none"): the halt keeps the worktree and branch, WIP-commits, flips the row to
blocked, logs the entry, frees the slot and commits only its own bookkeeping; the resume archives
the entry with the ruling, removes it, sets the row ready-for-dev and commits only its change."""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import _bookkeeping as bk  # noqa: E402
import halt_release  # noqa: E402
import resume_halted  # noqa: E402

CONFIG = """
[project]
name = "demo"
[git]
base_branch = "development"
branch_prefix = "story/"
worktrees_root = "wt"
[paths]
specs_dir = "docs/arts"
sprint_status = "docs/arts/sprint-status.yaml"
story_history_dir = "docs/arts/story-history"
slot_registry = "docs/arts/slot-registry.md"
auto_dev_failure_log = "docs/arts/auto-dev-failures.md"
[stack]
runtime = "none"
"""

STATUS = """development_status:
  1-2-widget: in-progress  # STARTED 2026-01-01 slot 1 (story/1-2); dep: none
  1-3-other: backlog  # untouched
last_updated: 2026-01-01
"""

REGISTRY = """| Slot | Status | Story ID | Branch | Worktree | Since |
|------|--------|----------|--------|----------|-------|
| 1 | in_use | 1-2 | story/1-2 | {wt} | 2026-01-01 |
| 2 | free | — | — | — | — |
"""


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


class HaltResumeTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIG)
        git(self.root, "init", "-q", "-b", "development")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        (self.root / ".gitignore").write_text(".workflow/state/\nwt/\n", encoding="utf-8")
        arts = self.root / "docs" / "arts"
        arts.mkdir(parents=True)
        (arts / "sprint-status.yaml").write_text(STATUS, encoding="utf-8")
        self.wt = self.root / "wt" / "1-2"
        (arts / "slot-registry.md").write_text(REGISTRY.format(wt=self.wt.as_posix()), encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        git(self.root, "worktree", "add", "-q", "-b", "story/1-2", str(self.wt), "development")
        (self.wt / "src.txt").write_text("work in progress\n", encoding="utf-8")
        (self.wt / "test_repro_r1_1.py").write_text("# repro\n", encoding="utf-8")

    def halt(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = halt_release.main(["--story-id", "1-2", "--failure-step", "story-impl",
                                      "--failure-summary", "tests red after 5 rounds", *extra])
        return code, json.loads(out.getvalue())

    def resume(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = resume_halted.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_halt_then_resume(self):
        # another session's uncommitted edit in the same file must stay out of both commits
        status = self.root / "docs" / "arts" / "sprint-status.yaml"
        status.write_text(status.read_text().replace("# untouched", "# someone else's edit"), encoding="utf-8")

        code, res = self.halt()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["status"], "halted")
        self.assertTrue(self.wt.is_dir())
        self.assertIn("wip: [Story 1-2] auto-loop halted in story-impl", git(self.wt, "log", "-1", "--format=%s"))
        self.assertIn("test_repro_r1_1.py", git(self.wt, "status", "--porcelain"))  # repro test stays untracked
        self.assertTrue(res["steps"]["stop"].startswith("skipped"))
        committed = git(self.root, "show", "HEAD:docs/arts/sprint-status.yaml")
        self.assertIn("1-2-widget: blocked  # HALTED", committed)
        self.assertIn("# untouched", committed)
        self.assertIn("someone else's edit", status.read_text())
        self.assertIn("| 1 | free |", git(self.root, "show", "HEAD:docs/arts/slot-registry.md"))
        log = git(self.root, "show", "HEAD:docs/arts/auto-dev-failures.md")
        self.assertIn("## 1-2 — ", log)
        self.assertIn("- failure_step: story-impl", log)

        code, res = self.resume("1-2")
        self.assertEqual(code, 1)
        self.assertIn("--ruling", res["message"])

        code, res = self.resume("1-2", "--ruling", "use the fake clock")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["row"], "ready-for-dev")
        committed = git(self.root, "show", "HEAD:docs/arts/sprint-status.yaml")
        self.assertIn("1-2-widget: ready-for-dev  # dep: none", committed)
        self.assertIn("# untouched", committed)
        self.assertNotIn("## 1-2 — ", git(self.root, "show", "HEAD:docs/arts/auto-dev-failures.md"))
        history = git(self.root, "show", "HEAD:docs/arts/story-history/1-2-widget.md")
        self.assertIn("resumed halt 1-2-", history)
        self.assertIn("ruling: use the fake clock", history)
        self.assertIn("~~~~text", history)
        self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))

    def test_line_break_refused_before_any_step(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = halt_release.main(["--story-id", "1-2", "--failure-step", "a\nb", "--failure-summary", "x"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "refused")
        self.assertNotIn("wip:", git(self.wt, "log", "-1", "--format=%s"))

    def test_resume_without_entry_refuses(self):
        code, res = self.resume("1-2", "--ruling", "x")
        self.assertEqual(code, 1)
        self.assertIn("no failure entry", res["message"])


class BookkeepingUnitTests(unittest.TestCase):
    def test_failure_blocks_and_without_story(self):
        text = ("# head\n\n## 1-2 — 2026-01-01T00:00:00 — story-impl\n\n- branch: story/1-2\n- failure_summary: a\n"
                "  b\n\nKept: x\n\n## 1.3 — 2026-01-02T00:00:00 — setup\n\n- failure_step: setup\n")
        blocks = bk.failure_blocks(text.splitlines(keepends=True))
        self.assertEqual([b["story_id"] for b in blocks], ["1-2", "1.3"])
        self.assertEqual(blocks[0]["fields"]["failure_summary"], "a\nb")
        self.assertEqual(bk.without_story(text, "1-3"),
                         "# head\n\n## 1-2 — 2026-01-01T00:00:00 — story-impl\n\n- branch: story/1-2\n"
                         "- failure_summary: a\n  b\n\nKept: x\n")

    def test_halted_note_replaces_lead_clause(self):
        with te.IsolatedRepo(""):
            self.assertEqual(halt_release.halted_note("STARTED 2026-01-01 slot 2 (story/1-2); dep: none", "a;b",
                                                      "2026-02-02"), "HALTED 2026-02-02 at a;b; worktree kept; dep: none")
            again = halt_release.halted_note("HALTED 2026-02-02 at a;b; worktree kept; dep: none", "c", "2026-03-03")
            self.assertEqual(again, "HALTED 2026-03-03 at c; worktree kept; dep: none")

    def test_archive_fence_outgrows_inner_tildes(self):
        text = resume_halted.archive_text(["## 1-2 — t — s\n~~~~~ inner\n"], {"story_id": "1-2", "stamp": "t"},
                                          "line one\nline two")
        self.assertTrue(text.startswith("resumed halt 1-2-t\n\n~~~~~~text\nruling: line one\n  line two\n"))
        self.assertTrue(text.endswith("\n~~~~~~"))


if __name__ == "__main__":
    unittest.main()
