"""design_ticket.py (+ _design): the story label, the rendered ticket body and comment, the tracker row, the
tracker-only commit beside another session's edit, and the spec marker rewrite.

The commit tests run real git on a throwaway repo laid out like the main checkout.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _state_testenv as te  # noqa: E402
import design_ticket as nt  # noqa: E402

TRACKER = Path("docs/design/design-questions.md")
CONFIG = """[git]
base_branch = "development"
[paths]
design_questions = "docs/design/design-questions.md"
design_ticket_regex = '\\bUX-\\d+\\b'
[design]
question_tag = "Designer"
[tracker]
project_key = "UX"
site = "https://example.atlassian.net"
labels = ["design-needed", "ux-design"]
"""

TRACKER_HEAD = (
    "# Design questions\n"
    "> | Ticket | Item | Priority | Blocks | Status (2026-10-10) |\n"
    "> |---|---|---|---|---|\n"
    "> | [UX-140](u) | Tab strip | Medium | — | ⏳ To Do |\n"
    ">\n"
    "> More text.\n"
)
SPEC = {
    "story_id": "8-936a", "topic": "Generating Chronicle overlay",
    "builds": "the Chronicle generation progress overlay", "phase": None,
    "what_this_is": "The overlay, from `401:1001`.",
    "questions": [
        {"title": "Stalled generation", "body": "The overlay cannot be dismissed.",
         "nodes": ["401:1001"], "options": ["(a) stay", "(b) close"], "ask": "Which?",
         "interim": "(a), it stays until a page reload."},
        {"title": "Glow", "body": "No variable carries it.", "sources": [{"source": "master", "value": "#3366CC@50%"}],
         "interim": "the drawn value."},
    ],
}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


class LabelTests(unittest.TestCase):
    def test_dots_become_dashes(self):
        te.enter(self, CONFIG)
        self.assertEqual(nt.story_label("8.936a"), "story-8-936a")
        self.assertEqual(nt.query("8-936a"), 'project = UX AND labels = "story-8-936a"')


class RenderTests(unittest.TestCase):
    def setUp(self):
        te.enter(self, CONFIG)

    def test_new_ticket_has_the_required_sections_in_order(self):
        out = nt.render_ticket(SPEC)
        self.assertEqual(out["summary"], "[UX] Generating Chronicle overlay — 2 questions from the Chronicle "
                                         "generation progress overlay (Story 8-936a)")
        self.assertEqual(out["labels"], ["design-needed", "ux-design", "story-8-936a"])
        desc = out["description"]
        self.assertTrue(desc.startswith("Story: 8-936a\n\n## What this is"))
        order = [desc.index(h) for h in ("## What this is", "## Questions", "## Done when")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("## Notes", desc)
        self.assertIn("1. **Stalled generation.** The overlay cannot be dismissed.", desc)
        self.assertIn("   - Options: (a) stay; (b) close", desc)
        self.assertIn("2. **Glow.**", desc)
        self.assertIn("   - Sources: master: #3366CC@50%", desc)
        self.assertIn("   - Interim ⚠️: the drawn value.", desc)

    def test_phase_label_and_notes(self):
        out = nt.render_ticket({**SPEC, "phase": 1, "notes": ["Radius is radius/card 8 by the published rule."]})
        self.assertIn("phase-1", out["labels"])
        self.assertIn("## Notes\n\n- Radius is radius/card 8 by the published rule.", out["description"])

    def test_a_comment_numbers_from_start(self):
        comment = nt.render_comment(SPEC, 5)["comment"]
        self.assertIn("5. **Stalled generation.**", comment)
        self.assertIn("6. **Glow.**", comment)
        self.assertNotIn("## Done when", comment)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        te.enter(self, CONFIG)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / TRACKER).parent.mkdir(parents=True)
        (self.repo / TRACKER).write_text(TRACKER_HEAD, encoding="utf-8")

    def add(self, ticket="UX-148"):
        return nt.insert_tracker_row(self.repo, ticket, "Overlay: Q1 stall", "8-936a ships the interims, ⚠️",
                                     "the owner, 8-936a Checkpoint 2", "2026-10-11")

    def test_the_row_closes_the_ticket_table(self):
        self.assertTrue(self.add())
        lines = (self.repo / TRACKER).read_text(encoding="utf-8").splitlines()
        row = lines.index(next(ln for ln in lines if "[UX-148]" in ln))
        self.assertEqual(lines[row + 1], ">")
        self.assertEqual(lines[row], "> | [UX-148](https://example.atlassian.net/browse/UX-148) | Overlay: Q1 stall | "
                                     "Medium | 8-936a ships the interims, ⚠️ | ⏳ To Do — filed 2026-10-11 "
                                     "(the owner, 8-936a Checkpoint 2) |")

    def test_an_existing_key_adds_nothing(self):
        self.assertFalse(self.add("UX-140"))

    def test_crlf_is_kept(self):
        (self.repo / TRACKER).write_bytes(TRACKER_HEAD.replace("\n", "\r\n").encode("utf-8"))
        self.add()
        raw = (self.repo / TRACKER).read_bytes().decode("utf-8")
        self.assertNotIn("\n", raw.replace("\r\n", ""))


class CommitTrackerTests(unittest.TestCase):
    def setUp(self):
        te.enter(self, CONFIG)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = self.repo = Path(tmp.name)
        git(repo, "init", "-q", "-b", "development")
        git(repo, "config", "user.name", "t")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "core.autocrlf", "false")
        (repo / TRACKER).parent.mkdir(parents=True)
        (repo / TRACKER).write_text(TRACKER_HEAD, encoding="utf-8")
        (repo / "other.md").write_text("x\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        (repo / TRACKER).write_text(TRACKER_HEAD.replace("> More text.", "> More text, edited elsewhere."),
                                       encoding="utf-8")
        nt.insert_tracker_row(repo, "UX-148", "Q1", "8-936a ships the interims, ⚠️", "x", "2026-10-11")

    def test_only_the_tickets_row_is_committed(self):
        self.assertTrue(nt.commit_tracker(self.repo, "UX-148", "docs: UX-148"))
        committed = git(self.repo, "show", f"HEAD:{TRACKER.as_posix()}")
        self.assertIn("[UX-148]", committed)
        self.assertNotIn("edited elsewhere", committed)
        self.assertIn("edited elsewhere", (self.repo / TRACKER).read_text(encoding="utf-8"))
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")

    def test_a_busy_index_refuses_and_commits_nothing(self):
        (self.repo / "other.md").write_text("y\n", encoding="utf-8")
        git(self.repo, "add", "other.md")
        head = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaises(nt.Refusal):
            nt.commit_tracker(self.repo, "UX-148", "docs: UX-148")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only").split(), ["other.md"])

    def test_no_uncommitted_row_refuses(self):
        with self.assertRaises(nt.Refusal):
            nt.commit_tracker(self.repo, "UX-999", "docs: none")


class MarkSpecTests(unittest.TestCase):
    def setUp(self):
        te.enter(self, CONFIG)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.spec = Path(tmp.name) / "spec.md"
        self.spec.write_text("A [OPEN-Q 1] (Designer) x.\nB [OPEN-Q 2] here.\nC [OPEN-Q 3] (Designer) y.\n", encoding="utf-8")

    def test_listed_markers_are_rewritten_and_others_kept(self):
        self.assertEqual(nt.mark_spec(self.spec, "UX-148", {3}), [3])
        text = self.spec.read_text(encoding="utf-8")
        self.assertIn("C OPEN-Q 3 (Designer, interim ⚠️, UX-148) y.", text)
        self.assertIn("A [OPEN-Q 1] (Designer) x.", text)
        self.assertIn("B [OPEN-Q 2] here.", text)

    def test_all_rewrites_every_bracketed_designer_marker_only(self):
        self.assertEqual(nt.mark_spec(self.spec, "UX-148", None), [1, 3])
        self.assertIn("B [OPEN-Q 2] here.", self.spec.read_text(encoding="utf-8"))

    def test_a_listed_marker_that_is_not_a_designer_marker_refuses_and_writes_nothing(self):
        before = self.spec.read_text(encoding="utf-8")
        with self.assertRaises(nt.Refusal):
            nt.mark_spec(self.spec, "UX-148", {1, 2})
        self.assertEqual(self.spec.read_text(encoding="utf-8"), before)


class StatusAndCliTests(unittest.TestCase):
    def test_ticket_status_and_done(self):
        root = te.enter(self, CONFIG)
        (root / TRACKER).parent.mkdir(parents=True)
        (root / TRACKER).write_text(TRACKER_HEAD.replace("⏳ To Do |", "✅ Done — answered |"), encoding="utf-8")
        import _design
        self.assertTrue(_design.is_done(_design.ticket_status(root, "UX-140")))
        self.assertIsNone(_design.ticket_status(root, "UX-999"))
        self.assertEqual(_design.last_sync(TRACKER_HEAD), "2026-10-10")
        self.assertTrue(_design.is_ticket_key("UX-12"))
        self.assertFalse(_design.is_ticket_key("UX-12 x"))

    def test_plain_table_without_blockquote(self):
        import _design
        te.enter(self, CONFIG)
        text = "| Ticket | Item | Priority | Blocks | Status |\n|---|---|---|---|---|\n| UX-1 | a | Low | — | ⏳ |\n\nafter\n"
        new, added = _design.insert_row(text, "UX-2", "b", "c", "o", "2026-01-01")
        self.assertTrue(added)
        self.assertIn("| [UX-2](https://example.atlassian.net/browse/UX-2) | b |", new.splitlines()[3])
        self.assertEqual(new.splitlines()[4], "")

    def test_unconfigured_tracker_steps_are_skipped(self):
        te.enter(self, "")
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nt.main(["tracker-row", "--ticket", "UX-1", "--item", "i", "--blocks", "b",
                                      "--origin", "o"]), 0)
        self.assertIn("skipped", out.getvalue())

    def test_a_bad_ticket_key_is_refused(self):
        te.enter(self, CONFIG)
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nt.main(["mark-spec", "--spec", "x.md", "--ticket", "none", "--all"]), 1)
        self.assertIn("not a ticket key", out.getvalue())


if __name__ == "__main__":
    unittest.main()
