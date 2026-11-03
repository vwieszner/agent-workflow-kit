"""Tests for design_instances.py (config: design.library_dir / design.intake_dir).

Pins, on a temporary library build:
  * a set id or a component name covers every variant and the frames' LOCAL copies of the
    set; a variant id covers only frame-map rows naming that variant;
  * a top-level instance prints its frame-dump subtree; a nested instance prints the inner
    node's subtree from the containing component's value sheet; both print the codegen
    lines carrying the instance's id or a descendant's, or say there is none;
  * an override row says unshown values are not recorded, and `effects` gets its own note;
    a frame map listing fewer ids than its count says how many are unlisted;
  * the master section prints the variant trees in sheet order; `--frame` limits the frames;
    a worktree without a build exits 2.

Usage
-----
    python3 -m unittest scripts/tests/test_design_instances.py
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _state_testenv as te  # noqa: E402
import design_instances as fi  # noqa: E402

INDEX = """\
| Component | Node | Page | Variants | Variant list | Value sheet |
|---|---|---|---|---|---|
| Button - Primary | `3:5` | Core | 2 | `3:6` State=Default<br>`3:10` State=Hover | [sets/button-primary.md](sets/button-primary.md) |
| Left sidebar | `400:1` | Arch | 1 | `400:2` Screen=DM | [sets/left-sidebar.md](sets/left-sidebar.md) |
"""

BUTTON_SHEET = """\
# Button - Primary

### `3:6` State=Default

- `3:6` component **State=Default** — 118×44; fill #FF8C00; radius 8
  - `3:7` text **Button** — Inter 700 14/16

### `3:10` State=Hover

- `3:10` component **State=Hover** — 118×44; fill #FFA73B; radius 8
  - `3:11` text **Button** — Inter 700 14/16
"""

SIDEBAR_SHEET = """\
# Left sidebar

- `400:2` component **Screen=DM** — 300×900
  - `445:536` instance **Button - Primary** — 223×44; fill #FF8C00; radius 8
    - `I445:536;3:7` text **Button** — Inter 700 18/16; "Cast Roll"
  - `445:600` frame **Other** — 10×10
"""

FRAME_MAP = """\
# Frame map — mechanics

| Master component | Instances | Where (first 6) | Fields the instances override |
|---|---|---|---|
| **Button - Primary** → `State=Default` `3:6` | 3 | `I668:3208;445:536` in Left sidebar > Footer; `701:10` in Row 2 | — |
| LOCAL `State=Default` (set `Button - Primary`) — component `816:1348` | 1 | `815:4284` in Main > Container | cornerRadius, effects |
| **Left sidebar** → `Screen=DM` `400:2` | 1 | `668:3208` in Mechanics | — |
"""

FRAME_DUMP = """\
# mechanics

- `668:1` frame **Mechanics** — 1280×1024
  - `701:10` instance **Button - Primary** — 164×44; fill #FF8C00; radius 8
    - override `I701:10;3:7` text **Button** — "Send to Story"
  - `815:4284` instance **Button - Primary** — 177×44; fill #FF8C00; radius 8
  - `900:1` frame **Next** — 1×1
"""

CODEGEN = """\
<div data-node-id="668:1">
<button className="rounded-[8px]" data-node-id="I668:3208;445:536" data-name="Button - Primary">
<p className="text-[18px]" data-node-id="I668:3208;445:536;3:7">Cast Roll</p>
<button className="rounded-[4px]" data-node-id="815:4284" data-name="Button - Primary">
"""


def build(root: Path, codegen: bool = True) -> Path:
    ml = root / "design" / "library"
    for d in ("sets", "frame-map", "frames"):
        (ml / d).mkdir(parents=True)
    (ml / "INDEX.md").write_text(INDEX, encoding="utf-8")
    (ml / "sets" / "button-primary.md").write_text(BUTTON_SHEET, encoding="utf-8")
    (ml / "sets" / "left-sidebar.md").write_text(SIDEBAR_SHEET, encoding="utf-8")
    (ml / "frame-map" / "mechanics.md").write_text(FRAME_MAP, encoding="utf-8")
    (ml / "frame-map" / "other.md").write_text(FRAME_MAP, encoding="utf-8")
    (ml / "frames" / "mechanics.md").write_text(FRAME_DUMP, encoding="utf-8")
    if codegen:
        (root / "design" / "design-context").mkdir(parents=True)
        (root / "design" / "design-context" / "mechanics.md").write_text(CODEGEN, encoding="utf-8")
    return root


class DesignInstances(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        te.enter(self, '[design]\nlibrary_dir = "design/library"\nintake_dir = "design"\n')
        self.root = build(Path(self.tmp.name))

    def text(self, query: str, frames=("mechanics",)) -> str:
        return "\n".join(fi.report(query, self.root, set(frames)))

    def test_a_set_id_or_name_covers_the_variants_and_local_copies(self):
        for query in ("3:5", "Button - Primary"):
            with self.subTest(query=query):
                out = self.text(query)
                self.assertIn("instance `I668:3208;445:536` in Left sidebar > Footer", out)
                self.assertIn("instance `815:4284` in Main > Container", out)
                self.assertNotIn("instance `668:3208`", out)

    def test_a_variant_id_covers_only_rows_naming_it(self):
        out = self.text("3:6")
        self.assertIn("instance `701:10`", out)
        self.assertNotIn("instance `815:4284`", out)

    def test_top_level_and_nested_instances_print_their_subtrees_and_codegen(self):
        out = self.text("3:5")
        self.assertIn('frames/mechanics.md:4: - `701:10` instance **Button - Primary** — 164×44', out)
        self.assertIn('frames/mechanics.md:5: - override `I701:10;3:7` text **Button** — "Send to Story"', out)
        self.assertNotIn("`900:1`", out)
        self.assertIn('sets/left-sidebar.md:4: - `445:536` instance **Button - Primary**', out)
        self.assertIn('sets/left-sidebar.md:5: - `I445:536;3:7` text **Button** — Inter 700 18/16; "Cast Roll"', out)
        self.assertNotIn("`445:600`", out)
        self.assertIn('design-context/mechanics.md:2: <button className="rounded-[8px]"', out)
        self.assertIn('design-context/mechanics.md:3: <p className="text-[18px]"', out)
        self.assertIn("design-context/mechanics.md: no codegen line for this instance", out)

    def test_overrides_effects_and_unlisted_instances_are_named(self):
        out = self.text("3:5")
        self.assertIn("overridden: cornerRadius, effects. A value the lines above do not show is not recorded in the build.", out)
        self.assertIn("`effects`: the build keeps only visible effects", out)
        self.assertIn("1 more instance(s): the frame map lists the first 2 only", out)

    def test_master_trees_print_in_sheet_order(self):
        out = self.text("3:5")
        self.assertLess(out.index("sets/button-primary.md:5: - `3:6` component"),
                        out.index("sets/button-primary.md:10: - `3:10` component"))
        self.assertIn("sets/button-primary.md:6: - `3:7` text **Button**", out)

    def test_frame_filter_and_missing_codegen_file(self):
        self.assertNotIn("## other", self.text("3:5"))
        self.assertIn("## other", self.text("3:5", frames=()))
        bare = build(Path(self.tmp.name) / "bare", codegen=False)
        out = "\n".join(fi.report("3:5", bare, {"mechanics"}))
        self.assertIn("design-context/mechanics.md: no file", out)

    def test_a_worktree_without_a_build_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(fi.main(["3:5", "--worktree", str(Path(self.tmp.name) / "none")]), 2)


class NotConfigured(unittest.TestCase):
    def test_no_library_dir_is_skipped(self):
        te.enter(self, "")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(fi.main(["3:5"]), 3)
        self.assertIn("SKIPPED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
