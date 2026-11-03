"""Review tooling: the diff chunker (`review_diff_chunks.py`: file-boundary splitting, line numbering, clipping,
page plans, test titles and viewports) and the spec AC reader (`review_spec_acs.py`), plus the deterministic
helpers of the review pipeline: `review_ledger.review_loop`, `review_findings_render` (`decide`,
`apply_halted_blocking`, `delta_side`, path normalisation), `review_carry` (malformed blocks) and
`run_mutation`'s pure functions."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402


def file_diff(path: str, body_lines: int) -> str:
    head = f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n"
    return head + "".join(f"+line {i}\n" for i in range(body_lines))


UNIT_TEST = "web/src/pages/Ledger.test.jsx"
E2E_SPEC = "web/tests/player-ledger.spec.js"


def hunk_diff(path: str, body: list[str]) -> str:
    """One file's diff whose hunk lines are `body`, each starting `+`, `-`, a space or `@@`."""
    head = f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
    return head + "".join(line + "\n" for line in body)


class DiffChunks(unittest.TestCase):
    def chunk(self, text: str, max_lines: int) -> tuple[dict, Path]:
        d = Path(tempfile.mkdtemp())
        (d / "diff.patch").write_text(text, encoding="utf-8", newline="")
        r = subprocess.run([sys.executable, str(SCRIPTS / "review_diff_chunks.py"),
                            "--diff", str(d / "diff.patch"), "--max-lines", str(max_lines)],
                           capture_output=True, text=True, timeout=60, check=True)
        return json.loads(r.stdout), d

    def test_split_at_file_boundaries_lossless_for_lines_up_to_line_clip(self):
        """Chunks hold every byte of a diff whose numbered lines are at most LINE_CLIP characters long."""
        sys.path.insert(0, str(SCRIPTS))
        import review_diff_chunks as rdc
        widest = "+" + "w" * (rdc.LINE_CLIP - rdc.WIDTH - 1) + "\n"
        text = file_diff("a.py", 10) + file_diff("b.py", 9) + widest + file_diff("c.py", 10)
        rep, d = self.chunk(text, 31)
        self.assertEqual([c["files"] for c in rep["chunks"]], [["a.py", "b.py"], ["c.py"]])
        joined = "".join(Path(c["path"]).read_text(encoding="utf-8") for c in rep["chunks"])
        self.assertEqual("".join(l[7:] for l in joined.splitlines(keepends=True)), text)
        self.assertEqual(sum(c["lines"] for c in rep["chunks"]), rep["total_lines"])

    def test_oversized_file_gets_own_chunk(self):
        text = file_diff("small.py", 2) + file_diff("big.py", 50) + file_diff("tail.py", 2)
        rep, _ = self.chunk(text, 20)
        self.assertEqual([c["files"] for c in rep["chunks"]], [["small.py"], ["big.py"], ["tail.py"]])

    def test_line_numbers(self):
        text = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -10,3 +20,3 @@ def f():\n"
                " keep\n-gone\n+new\n keep2\n")
        rep, _ = self.chunk(text, 100)
        lines = Path(rep["chunks"][0]["path"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[4:], ["    20  keep", "   o11 -gone", "    21 +new", "    22  keep2"])
        self.assertTrue(lines[3].startswith("       @@"))

    def test_pages_cover_each_chunk_within_budget(self):
        sys.path.insert(0, str(SCRIPTS))
        import review_diff_chunks as rdc
        text = file_diff("short.py", 3000) + file_diff("wide.json", 40).replace("+line", "+" + "x" * 1900)
        rep, _ = self.chunk(text, 100_000)
        lines = Path(rep["chunks"][0]["path"]).read_text(encoding="utf-8").splitlines(keepends=True)
        pages = rep["chunks"][0]["pages"]
        self.assertEqual(pages[0][0], 1)
        self.assertEqual([o for o, _ in pages[1:]], [o + l for o, l in pages[:-1]])
        self.assertEqual(sum(l for _, l in pages), len(lines))
        for o, l in pages:
            cost = sum(len(x.rstrip("\r\n")) + rdc.READ_PREFIX for x in lines[o - 1:o - 1 + l])
            self.assertLessEqual(cost, rdc.PAGE_CHARS, (o, l, cost))
        self.assertGreater(len(pages), 2)

    def test_clip(self):
        sys.path.insert(0, str(SCRIPTS))
        import review_diff_chunks as rdc
        self.assertEqual(rdc.clip("short\n"), "short\n")
        self.assertEqual(rdc.clip("z" * 2000 + "\n"), "z" * 2000 + "\n")
        out = rdc.clip("y" * 3000 + "\r\n")
        self.assertEqual(out, "y" * 2000 + " ... [review_diff_chunks: line clipped at 2000 of 3000 chars]\r\n")
        self.assertEqual(rdc.clip("z" * 2001), "z" * 2000 + " ... [review_diff_chunks: line clipped at 2000 of 2001 chars]")

    def test_long_line_clipped_and_paged_within_budget(self):
        sys.path.insert(0, str(SCRIPTS))
        import review_diff_chunks as rdc
        text = (file_diff("a.css", 600)
                + "diff --git a/v.min.js b/v.min.js\nnew file mode 100644\n--- /dev/null\n+++ b/v.min.js\n"
                + "@@ -0,0 +1 @@\n+" + "x" * 50_925 + "\n\\ No newline at end of file\n"
                + file_diff("b.py", 600))
        rep, _ = self.chunk(text, 100_000)
        chunk = rep["chunks"][0]
        lines = Path(chunk["path"]).read_text(encoding="utf-8").splitlines(keepends=True)
        self.assertEqual(len(lines), chunk["lines"])
        self.assertEqual(len(text.splitlines()), chunk["lines"])
        long = [x for x in lines if "line clipped" in x]
        self.assertEqual(len(long), 1)
        self.assertEqual("     1 +" + "x" * (rdc.LINE_CLIP - 8)
                         + " ... [review_diff_chunks: line clipped at 2000 of 50933 chars]\n", long[0])
        self.assertEqual(sum(l for _, l in chunk["pages"]), len(lines))
        for o, l in chunk["pages"]:
            cost = sum(len(x.rstrip("\r\n")) + rdc.READ_PREFIX for x in lines[o - 1:o - 1 + l])
            self.assertLessEqual(cost, rdc.PAGE_CHARS, (o, l, cost))

    def test_page_holds_at_least_one_line(self):
        sys.path.insert(0, str(SCRIPTS))
        import review_diff_chunks as rdc
        self.assertEqual(rdc.page_plan(["x" * 50 + "\n"] * 3, budget=10), [[1, 1], [2, 1], [3, 1]])
        self.assertEqual(rdc.page_plan([]), [])

    def test_empty_diff(self):
        rep, d = self.chunk("", 10)
        self.assertEqual(rep, {"total_lines": 0, "chunks": []})
        self.assertFalse(list(d.glob("diff-*.patch")))

    def titles(self, files: dict[str, list[str]], max_lines: int = 100_000) -> list[str]:
        """The `test_titles` of every chunk of a diff holding `files` (path -> hunk lines), in chunk order."""
        rep, _ = self.chunk("".join(hunk_diff(path, body) for path, body in files.items()), max_lines)
        return [title for c in rep["chunks"] for title in c["test_titles"]]

    def test_it_and_test_titles_are_listed_whether_or_not_describe_blocks_nest_them(self):
        body = ["@@ -0,0 +1,7 @@",
                "+describe('Zone C — the private block (AC1)', () => {",
                "+  it('renders the given component for each private entry', () => {});",
                "+  describe('a nested group', () => {",
                '+    test("a double-quoted title", async () => {});',
                "+    it(`a template title`, () => {});",
                "+  });",
                "+});"]
        self.assertEqual(self.titles({UNIT_TEST: body}), [
            "renders the given component for each private entry", "a double-quoted title", "a template title"])

    def test_modifier_and_each_forms_are_listed_with_the_title_template_as_written(self):
        body = ["@@ -0,0 +1,8 @@",
                "+it.only('an only title', () => {});",
                "+it.skip('a skipped title', () => {});",
                "+test.fixme('a fixme title', async () => {});",
                "+it.each([403, 409])('a %i from end_session never navigates', async (status) => {});",
                "+it.each([['inbox'], ['archive']])('marks %s as the only current row', (item) => {});",
                "+it.each([",
                "+  [1, 'a'],",
                "+])('a title after a multi-line table %i', (n) => {});"]
        self.assertEqual(self.titles({UNIT_TEST: body}), [
            "an only title", "a skipped title", "a fixme title", "a %i from end_session never navigates",
            "marks %s as the only current row", "a title after a multi-line table %i"])

    def test_a_title_on_the_next_line_is_listed_and_an_escaped_quote_is_unescaped(self):
        body = ["@@ -0,0 +1,5 @@",
                "+it(",
                "+  'a title on the next line',",
                "+  async () => {},",
                "+);",
                "+it('shows the error line when the viewer\\'s own profile cannot be loaded', async () => {});"]
        self.assertEqual(self.titles({UNIT_TEST: body}), [
            "a title on the next line", "shows the error line when the viewer's own profile cannot be loaded"])

    def test_only_added_lines_open_a_title_and_a_title_added_under_an_unchanged_opener_counts(self):
        body = ["@@ -10,6 +10,7 @@",
                " it('a context title is not an added line', () => {",
                "-  it('a removed title', () => {",
                "+  it('an added title replacing it', () => {",
                " it(",
                "+  'a title added under an unchanged opener',",
                " () => {});"]
        self.assertEqual(self.titles({UNIT_TEST: body}), [
            "an added title replacing it", "a title added under an unchanged opener"])

    def test_calls_that_declare_no_test_are_not_listed(self):
        body = ["@@ -0,0 +1,9 @@",
                "+test.describe('A playwright group', () => {",
                "+  test.beforeEach(async ({ page }) => {});",
                "+  test.step('a step is not a test', async () => {});",
                "+  test.setTimeout(30000);",
                "+  test.use({ viewport: { width: 1280 } });",
                "+  const matched = new RegExp(`/api/${id}/$`).test(response.url());",
                "+  submit('a call whose name ends in it', () => {});",
                "+  it('a ' + 'concatenated title', () => {});",
                "+});"]
        self.assertEqual(self.titles({E2E_SPEC: body}), [])

    def test_a_call_does_not_span_two_hunks(self):
        body = ["@@ -1,2 +1,2 @@", "+it(", "@@ -80,2 +80,2 @@", "+  'a title from another hunk',"]
        self.assertEqual(self.titles({UNIT_TEST: body}), [])

    def test_only_test_and_spec_files_are_read(self):
        def one(title: str) -> list[str]:
            return ["@@ -0,0 +1,1 @@", f"+it('{title}', () => {{}});"]
        files = {
            "web/src/pages/Ledger.jsx": one("a source file"),
            "web/tests/pages/PlayerLedgerPage.js": one("a page object"),
            "web/tests/helpers.js": one("a helper"),
            "web/src/pages/Ledger.test.js": one("a js test"),
            "web/src/pages/Ledger.test.jsx": one("a jsx test"),
            "web/tests/chronicle.spec.js": one("a spec"),
            "web/src/data.test.json": one("a json file"),
        }
        self.assertEqual(self.titles(files), ["a js test", "a jsx test", "a spec"])

    def test_a_title_is_listed_once_in_first_seen_order_across_hunks_and_files(self):
        first = ["@@ -1 +1 @@", "+it('first title', () => {});", "@@ -50 +50 @@",
                 "+it('second title', () => {});", "+it('first title', () => {});"]
        second = ["@@ -1 +1 @@", "+test('second title', () => {});", "+test('third title', () => {});"]
        self.assertEqual(self.titles({UNIT_TEST: first, E2E_SPEC: second}), ["first title", "second title", "third title"])

    def test_each_chunk_lists_the_titles_of_its_own_files(self):
        text = (hunk_diff(UNIT_TEST, ["@@ -1 +1 @@", "+it('unit title', () => {});", *[f"+line {i}" for i in range(30)]])
                + hunk_diff(E2E_SPEC, ["@@ -1 +1 @@", "+test('e2e title', () => {});"]))
        rep, _ = self.chunk(text, 20)
        self.assertEqual([(c["files"], c["test_titles"]) for c in rep["chunks"]],
                         [([UNIT_TEST], ["unit title"]), ([E2E_SPEC], ["e2e title"])])

    def test_a_diff_without_test_files_lists_no_titles(self):
        rep, _ = self.chunk(file_diff("web/src/a.jsx", 5), 100)
        self.assertEqual([c["test_titles"] for c in rep["chunks"]], [[]])

    def test_titles_are_read_from_a_crlf_diff(self):
        body = ["@@ -0,0 +1,2 @@", "+it('a title', () => {});", "+test('another title', () => {});"]
        text = hunk_diff(UNIT_TEST, body).replace("\n", "\r\n")
        rep, _ = self.chunk(text, 100)
        self.assertEqual(rep["chunks"][0]["test_titles"], ["a title", "another title"])

    def viewports(self, files: dict[str, list[str]], max_lines: int = 100_000) -> list[str]:
        """The `test_viewports` of every chunk of a diff holding `files` (path -> hunk lines), in chunk order."""
        rep, _ = self.chunk("".join(hunk_diff(path, body) for path, body in files.items()), max_lines)
        return [viewport for c in rep["chunks"] for viewport in c["test_viewports"]]

    def test_literal_sizes_set_by_a_call_are_listed_as_width_x_height_in_first_seen_order(self):
        body = ["@@ -0,0 +1,7 @@",
                "+  test('draws the block on the frame width', async ({ page }) => {",
                "+    await page.setViewportSize({ width: 1280, height: 1024 });",
                "+  });",
                "+  test('spans the content area on the summary screen', async ({ page }) => {",
                "+    await page.setViewportSize({ width: 2560, height: 1440 });",
                "+  });"]
        self.assertEqual(self.viewports({E2E_SPEC: body}), ["1280x1024", "2560x1440"])

    def test_a_viewport_option_a_multi_line_call_and_a_reordered_pair_are_listed(self):
        body = ["@@ -0,0 +1,8 @@",
                "+test.use({ viewport: { width: 1024, height: 768 } });",
                "+await page.setViewportSize({",
                "+  width: 800,",
                "+  height: 600,",
                "+});",
                "+const other = await browser.newPage({ viewport: { height: 700, width: 900 } });",
                "+await page.setViewportSize({width:640,height:480});"]
        self.assertEqual(self.viewports({E2E_SPEC: body}), ["1024x768", "800x600", "900x700", "640x480"])

    def test_computed_sizes_and_calls_that_set_no_literal_pair_are_not_listed(self):
        body = ["@@ -0,0 +1,11 @@",
                "+await page.setViewportSize({ width: size.width, height: 1024 });",
                "+await page.setViewportSize({ width: 1280 * 2, height: 1024 });",
                "+await page.setViewportSize({ width, height });",
                "+await page.setViewportSize({ width: getWidth(), height: 1024 });",
                "+await page.setViewportSize(viewports.wide);",
                "+await page.setViewportSize(size);",
                "+await page.setViewportSize({ width: 1280 });",
                "+await resetViewportSize({ width: 1, height: 2 });",
                "+const current = page.viewportSize();",
                "+const options = { viewport: viewports.wide };",
                "+const lone = { viewport: { height: 1024 } };"]
        self.assertEqual(self.viewports({E2E_SPEC: body}), [])

    def test_only_added_lines_open_a_viewport_and_a_call_does_not_span_two_hunks(self):
        body = ["@@ -10,7 +10,9 @@",
                " await page.setViewportSize({ width: 1111, height: 222 });",
                "-await page.setViewportSize({ width: 1112, height: 223 });",
                "+await page.setViewportSize({ width: 1113, height: 224 });",
                " await page.setViewportSize({",
                "+  width: 1114,",
                "+  height: 225,",
                " });",
                "@@ -80,2 +80,2 @@",
                "+await page.setViewportSize({",
                "@@ -90,2 +92,2 @@",
                "+  width: 1115, height: 226 });"]
        self.assertEqual(self.viewports({E2E_SPEC: body}), ["1113x224", "1114x225"])

    def test_only_test_and_spec_files_are_read_for_viewports(self):
        def one(width: int, height: int) -> list[str]:
            return ["@@ -0,0 +1,1 @@", f"+await page.setViewportSize({{ width: {width}, height: {height} }});"]
        files = {
            "web/playwright.config.js": one(1, 1),
            "web/tests/helpers.js": one(2, 2),
            "web/src/pages/Ledger.jsx": one(3, 3),
            "web/src/pages/Ledger.test.jsx": one(4, 4),
            "web/tests/chronicle.spec.js": one(5, 5),
            "web/src/data.test.json": one(6, 6),
        }
        self.assertEqual(self.viewports(files), ["4x4", "5x5"])

    def test_a_viewport_is_listed_once_in_first_seen_order_across_hunks_and_files(self):
        first = ["@@ -1 +1 @@", "+await page.setViewportSize({ width: 1280, height: 1024 });", "@@ -50 +50 @@",
                 "+await page.setViewportSize({ width: 2560, height: 1440 });",
                 "+await page.setViewportSize({ width: 1280, height: 1024 });"]
        second = ["@@ -1 +1 @@", "+await page.setViewportSize({ width: 2560, height: 1440 });",
                  "+await page.setViewportSize({ width: 800, height: 600 });"]
        self.assertEqual(self.viewports({UNIT_TEST: first, E2E_SPEC: second}), ["1280x1024", "2560x1440", "800x600"])

    def test_each_chunk_lists_the_viewports_of_its_own_files(self):
        text = (hunk_diff(UNIT_TEST, ["@@ -1 +1 @@", "+await page.setViewportSize({ width: 100, height: 200 });",
                                      *[f"+line {i}" for i in range(30)]])
                + hunk_diff(E2E_SPEC, ["@@ -1 +1 @@", "+await page.setViewportSize({ width: 300, height: 400 });"]))
        rep, _ = self.chunk(text, 20)
        self.assertEqual([(c["files"], c["test_viewports"]) for c in rep["chunks"]],
                         [([UNIT_TEST], ["100x200"]), ([E2E_SPEC], ["300x400"])])

    def test_a_diff_without_test_files_lists_no_viewports_and_a_crlf_diff_lists_the_same_ones(self):
        rep, _ = self.chunk(file_diff("web/src/a.jsx", 5), 100)
        self.assertEqual([c["test_viewports"] for c in rep["chunks"]], [[]])
        body = ["@@ -0,0 +1,5 @@", "+await page.setViewportSize({ width: 1280, height: 1024 });",
                "+await page.setViewportSize({", "+  width: 800,", "+  height: 600,", "+});"]
        rep, _ = self.chunk(hunk_diff(E2E_SPEC, body).replace("\n", "\r\n"), 100)
        self.assertEqual(rep["chunks"][0]["test_viewports"], ["1280x1024", "800x600"])


def spec_ac_ids(spec: str, newline: str = "\n") -> list[str]:
    """The `spec_acs` `review_spec_acs.py` prints for a spec file holding `spec`."""
    path = Path(tempfile.mkdtemp()) / "spec.md"
    path.write_text(spec, encoding="utf-8", newline=newline)
    r = subprocess.run([sys.executable, str(SCRIPTS / "review_spec_acs.py"), "--spec", str(path)],
                       capture_output=True, text=True, timeout=60, check=True)
    assert r.stdout.count("\n") == 1, r.stdout
    return json.loads(r.stdout)["spec_acs"]


class SpecAcIds(unittest.TestCase):
    def test_ids_are_read_from_headings_and_bold_line_starts_of_every_form(self):
        spec = "\n".join([
            "# Story zz-1: A title", "", "## Story", "",
            "As a player I read AC9 in the notes; AC1 is described below.", "",
            "## Acceptance Criteria", "",
            "### AC1: A colon heading", "Prose that names AC1, AC2 and **AC3** inline.", "",
            "### AC2 — A dash heading",
            "#### AC2b: A lettered sub-heading",
            "##### AC2.1: A dotted sub-heading",
            "**AC3 — A bold dash heading.**",
            "**AC4:** A bold colon heading",
            "**AC5** A closed bold heading",
            "**AC5a.** A closed bold heading with a full stop",
            "### AC6/AC7: Two ids in one heading",
            "### AC8, AC9 and AC10 — Three ids in one heading", "",
            "## AC11", "Text.", "",
            "## Tasks", "", "- [ ] Task 1 (AC1)", ""])
        self.assertEqual(spec_ac_ids(spec), ["AC1", "AC2", "AC2b", "AC2.1", "AC3", "AC4", "AC5", "AC5a",
                                             "AC6", "AC7", "AC8", "AC9", "AC10", "AC11"])

    def test_prose_mentions_list_items_table_rows_quotes_and_bold_notes_declare_no_id(self):
        spec = "\n".join([
            "# Story zz-1", "", "## Acceptance Criteria", "",
            "1. The panel renders (AC1 of the epic).",
            "- AC2: a list item that is not a heading",
            "- **AC2b** a bold list item",
            "| AC3 Reuse the layout | AC3 |",
            "> ### AC4: a quoted heading",
            "**ACs verified satisfied** and **AC5 verified** in prose",
            "**AC6-2 pre-fix evidence** of a bold note",
            "**AC7.3b made non-vacuous.** a bold note",
            "**AC8, AC9 verified** in prose",
            "### ACs in prose", "### AC compliance",
            "Story 8.934b AC1 sets the width.", ""])
        self.assertEqual(spec_ac_ids(spec), [])

    def test_the_acceptance_criteria_section_decides_when_it_holds_an_id(self):
        spec = "\n".join([
            "## Acceptance Criteria", "", "### AC1: One", "### AC2: Two", "",
            "## Dev Notes", "", "**AC2.1 — a note about AC2.**", "### AC9: a heading in the notes", ""])
        self.assertEqual(spec_ac_ids(spec), ["AC1", "AC2"])

    def test_a_level_two_ac_heading_does_not_end_the_acceptance_criteria_section(self):
        spec = "\n".join(["## Acceptance Criteria", "", "### AC1: One", "", "## AC2", "Text.", "", "### AC3: Three", "",
                          "## Dev Notes", "", "### AC4: a heading in the notes", ""])
        self.assertEqual(spec_ac_ids(spec), ["AC1", "AC2", "AC3"])

    def test_a_spec_without_an_acceptance_criteria_section_or_with_no_id_in_it_is_read_whole(self):
        no_section = "\n".join(["# Story zz-1", "", "## Context", "", "#### AC1: Under a group heading",
                                "**AC2 — A bold definition.**", "", "## Dev Notes", "", "See AC3.", ""])
        self.assertEqual(spec_ac_ids(no_section), ["AC1", "AC2"])
        empty_section = "\n".join(["## Acceptance Criteria", "", "1. An unnumbered criterion.", "",
                                   "## Detailed criteria", "", "### AC1: Here", ""])
        self.assertEqual(spec_ac_ids(empty_section), ["AC1"])
        self.assertEqual(spec_ac_ids("# Story zz-1\n\nNo criteria here.\n"), [])

    def test_a_fenced_code_block_declares_no_id(self):
        spec = "\n".join(["## Acceptance Criteria", "", "```markdown", "### AC1: an example heading in a code block", "```",
                          "### AC2: A real heading", "~~~", "**AC3 — in a tilde fence**", "~~~", "### AC4: Another", ""])
        self.assertEqual(spec_ac_ids(spec), ["AC2", "AC4"])

    def test_an_id_is_listed_once_in_order_of_first_appearance_and_crlf_and_bom_change_nothing(self):
        spec = "﻿" + "\n".join(["## Acceptance Criteria", "", "### AC2: Two", "### AC1: One", "**AC2 — Two again.**", ""])
        self.assertEqual(spec_ac_ids(spec, newline="\r\n"), ["AC2", "AC1"])
        self.assertEqual(spec_ac_ids(spec), ["AC2", "AC1"])

    def test_an_unreadable_spec_exits_1_naming_the_file(self):
        missing = Path(tempfile.mkdtemp()) / "missing.md"
        r = subprocess.run([sys.executable, str(SCRIPTS / "review_spec_acs.py"), "--spec", str(missing)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn(str(missing), r.stderr)




class LedgerLoop(unittest.TestCase):
    def setUp(self):
        te.enter(self, "")
        import review_ledger
        self.L = review_ledger

    def section(self, n: int, approved: str, mode: str = "controlled", exit_line: str = "- Loop exit: n/a") -> str:
        return (f"## Review round {n} — 2026-01-01\n\n### Summary\n\n- Diff: `a..b`; mode {mode}; passes 1\n"
                f"{exit_line}\n\n## Triage decisions\n\n### approved_patches\n{approved}\n\n"
                "### deferred_findings\nnone\n\n### dismissed_findings\nnone\n")

    def rec(self, n: int, kind: str = "branch") -> str:
        return f"- 2026-01-01 review round {n} recorded — 1 findings; f.md; reviewed {kind} {'a' * 40}..{'b' * 40}"

    def test_round_with_no_patch_ends_the_loop(self):
        state = self.L.review_loop([self.rec(1)], self.section(1, "none"))
        self.assertEqual("no patch approved in round 1", state["ended"])

    def test_patch_round_needs_phase3_green_to_finalize(self):
        text = self.section(1, "R1-1 | src/a.py | 3 | fix | - | -")
        state = self.L.review_loop([self.rec(1)], text)
        self.assertFalse(state["rounds"][0]["finalized"])
        self.assertIsNone(state["ended"])
        state = self.L.review_loop([self.rec(1), "- 2026-01-02 phase-3 tests GREEN — x (3 tests)"], text)
        self.assertTrue(state["rounds"][0]["finalized"])
        self.assertEqual(["src/a.py"], state["rounds"][0]["files"])

    def test_close_out_cap_ends_the_loop(self):
        green = "- 2026-01-02 phase-3 tests GREEN — x (3 tests)"
        text = "".join(self.section(n, "R%d-1 | src/a.py | 3 | fix | - | -" % n) for n in (1, 2, 3))
        record = [self.rec(1), green, self.rec(2, "close-out"), green, self.rec(3, "close-out"), green]
        self.assertEqual(self.L.LOOP_CAP_REASON, self.L.review_loop(record, text)["ended"])

    def test_auto_mode_owes_the_next_round(self):
        text = self.section(1, "R1-1 | src/a.py | 3 | fix | - | -", mode="auto")
        state = self.L.review_loop([self.rec(1), "- phase-3 tests GREEN"], text)
        self.assertEqual(2, state["due"])

    def test_a_red_phase3_line_is_not_a_verdict(self):
        text = self.section(1, "R1-1 | src/a.py | 3 | fix | - | -")
        state = self.L.review_loop([self.rec(1), "- phase-3 tests RED — 2 failed"], text)
        self.assertFalse(state["rounds"][0]["finalized"])


class RenderHelpers(unittest.TestCase):
    def setUp(self):
        te.enter(self, '[git]\nworktrees_root = "../demo-worktrees"\n[review]\npath_rewrites = [["^/app/", "src/"]]\n')
        import review_findings_render
        self.R = review_findings_render

    def test_decide_needs_two_votes_and_keeps_a_minority_blocking(self):
        v = lambda verdict, impact="none": {"verdict": verdict, "evidence": verdict.lower(), "bar_impact": impact}
        self.assertEqual("CONFIRMED", self.R.decide([v("CONFIRMED"), v("CONFIRMED"), v("REFUTED")])["verdict"])
        split = self.R.decide([v("CONFIRMED"), v("REFUTED")])
        self.assertEqual("UNVERIFIABLE", split["verdict"])
        self.assertTrue(split["evidence"].startswith("split vote"))
        minority = self.R.decide([v("CONFIRMED", "BLOCKING — bar-1 — breaks"), v("CONFIRMED"), v("CONFIRMED")])
        self.assertEqual("BLOCKING (minority, 1 of 3 votes) — bar-1 — breaks", minority["bar_impact"])
        self.assertEqual("n/a", self.R.decide([v("REFUTED"), v("REFUTED"), v("CONFIRMED")])["bar_impact"])

    def test_halted_blocking_survives_a_refuted_round(self):
        d = {"verdict": "REFUTED", "evidence": "no", "bar_impact": "n/a", "votes": ["REFUTED"]}
        kept = self.R.apply_halted_blocking(d, [{"round": 1, "verdict": "CONFIRMED",
                                                 "bar_impact": "BLOCKING — bar-2 — x"}], 2)
        self.assertEqual("UNVERIFIABLE", kept["verdict"])
        self.assertEqual("BLOCKING (minority, halted round 1) — bar-2 — x", kept["bar_impact"])

    def test_norm_path_cuts_the_worktree_and_applies_rewrites(self):
        self.assertEqual("src/x.py", self.R._norm_path("/home/u/demo-worktrees/1-1/src/x.py"))
        self.assertEqual("src/svc.py", self.R._norm_path("/app/svc.py"))
        self.assertEqual("a.py", self.R._norm_path("b/a.py"))

    def test_delta_side(self):
        delta = {"kind": "close-out", "hunks": {"src/a.py": [[10, 12]]}, "deleted": ["src/gone.py"]}
        self.assertEqual("inside", self.R.delta_side("src/a.py:14", delta))
        self.assertEqual("outside", self.R.delta_side("src/a.py:40", delta))
        self.assertEqual("inside", self.R.delta_side("src/gone.py:1", delta))
        self.assertEqual("inside", self.R.delta_side("src/a.py:40; src/a.py:11", delta))
        self.assertEqual("inside", self.R.delta_side("src/a.py:40,11-912", delta), "a bare range keeps its file")
        self.assertEqual("inside", self.R.delta_side("N/A", delta), "an unplaceable location keeps the loop going")
        self.assertEqual("n/a", self.R.delta_side("src/a.py:40", dict(delta, kind="branch")))

    def test_validate_delta_refuses_a_malformed_delta(self):
        with self.assertRaises(self.R.RenderError):
            self.R.validate_delta({"kind": "close-out", "how": "branch"})


class CarryRefusals(unittest.TestCase):
    def setUp(self):
        te.enter(self, "")

    def test_a_carried_block_missing_a_line_is_refused(self):
        text = ("## Review round 1 — d\n\n### Findings for triage\n\n#### R1-1 — t\n\n- **Sources:** blind-hunter.1\n"
                "- **Location:** `a.py:1`\n- **Claim:** c\n- **Verification:** CONFIRMED — e\n- bar impact: none\n")
        d = Path(tempfile.mkdtemp())
        (d / "1-1-code-review-findings-x.md").write_text(text, encoding="utf-8")
        r = subprocess.run([sys.executable, str(SCRIPTS / "review_carry.py"), "--findings-file",
                            str(d / "1-1-code-review-findings-x.md"), "--round", "2"], capture_output=True, text=True)
        self.assertEqual(1, r.returncode)
        self.assertIn("R1-1", r.stderr)


class MutationHelpers(unittest.TestCase):
    def setUp(self):
        te.enter(self, '[review.mutation]\nsrc_prefix = "src"\n')
        import run_mutation
        self.M = run_mutation

    def test_paths(self):
        self.assertEqual("pkg/x.py", self.M.container_path("src/pkg/x.py"))
        self.assertEqual("pkg.x", self.M.dotted_module("src/pkg/x.py"))
        self.assertEqual("src/pkg/x.py", self.M.module_file("pkg.x.x_f__mutmut_3"))
        with self.assertRaises(ValueError):
            self.M.container_path("lib/x.py")
        with self.assertRaises(ValueError):
            self.M.container_path("src/../x.py")

    def test_changed_functions_and_lines(self):
        src = "X = 1\n\ndef f(a):\n    return a\n\nclass C:\n    @staticmethod\n    def g():\n        return 2\n"
        found, outside = self.M.changed_functions(src, {1, 4, 7}, "pkg.x")
        self.assertEqual({"pkg.x.x_f": 3, "pkg.x.x\u01c1C\u01c1g": 8}, found)
        self.assertEqual([1], outside)
        self.assertEqual({5, 6}, self.M.changed_lines("@@ -4,0 +5,2 @@\n+a\n+b\n"))
        self.assertEqual({2}, self.M.mutated_lines("--- a\n+++ b\n@@ -1,3 +1,3 @@\n x\n-y\n+z\n w\n"))

    def test_disabled_stage_is_blocked(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "run_mutation.py"), "--project-name", "story-1",
                            "--worktree", "/tmp/x", "--diff-range", "a..b", "--module", "src/a.py", "--test",
                            "src/tests/test_a.py"], capture_output=True, text=True,
                           env={**__import__("os").environ})
        self.assertEqual(2, r.returncode)
        self.assertIn("disabled", r.stdout)


if __name__ == "__main__":
    unittest.main()
