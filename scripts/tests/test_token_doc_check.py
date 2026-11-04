"""Tests for token_doc_check.py (config: design.token_doc_* and design.token_*).

Pins, on a mini mirror section and a mini token export in a temp dir:
  * one row of every form (radius / spacing / colour table rows, colour note, border-width and size
    bullets, space primitive, text style, effect style) matching the export exits 0;
  * each form with a changed value gives exactly one MISMATCH and exit 1, an alias that differs too;
  * the `color/` prefix, `#X @ 5%` against `#X@0.05`, the broken-backtick row, `SemiBold` against
    `Semi Bold`, a two-word `Semi Bold (600)` and a bare `Extra Bold` weight all match;
  * a token missing from the export is `MISSING in export`; an unnamed Semantic variable, text style or
    effect is listed under EXPORT-ONLY and exits 1; an unnamed Primitive is not listed;
  * a broken alias chain is a `<broken alias …>` mismatch; a 2-layer effect is a `2 layers` mismatch;
  * a non-pixel or non-object line height is a MISMATCH line, not a traceback;
  * exit 2: a 2-mode variable, a missing START or END heading, an END heading only before START,
    unreadable JSON, a missing export file, a missing `collections` / `text` / `effect` / `variables`
    key, a non-object root, and an expected table whose format no longer matches any row; an END line
    before START is skipped, and colour notes may be absent.

Usage
-----
    python3 -m unittest scripts/tests/test_token_doc_check.py
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _state_testenv as te  # noqa: E402
import token_doc_check as ctc  # noqa: E402

CONFIG = '''[design]
token_doc_start = "# Token Reference"
token_doc_end = "### Accessibility Requirements"
token_required_tables = ["radius table", "spacing table", "colour table", "border-width bullet", "size bullet", "space primitive", "text style", "effect style"]
'''

MIRROR = """\
# Intro

Some text.

# Token Reference (Variables & Styles)

## 2. Corner Radius Tokens

| Token | Alias | Value | Usage |
| --- | --- | --- | --- |
| `radius/control` | `radius/8` | `8px` | Interactive controls. |

## 3. Spacing Tokens

`space/0` (0px), `space/8` (8px).

| Token | Alias | Value | Notes |
| --- | --- | --- | --- |
| `space/control-y` | `space/8` | `8px` | Vertical padding. |

## 4. Color Tokens

- **text/nav-active:** Add token role: `blue/500` (`#3366CC`) — Active tab labels.

| Token | Base | Value | Purpose |
| --- | --- | --- | --- |
| **Surfaces** |  |  |  |
| `surface/hover-neutral` | `color/alpha/white-5` | `#FFFFFF` @ `5%` | Translucent hover overlay. |
| `surface/hover-danger` | `color/alpha/crimson-5` | `#`B91C1C@ `5%` | Crimson hover overlay. |
| `border/focus` | `color/blue/500` | `#3366CC` | Active input focus rings. |

## 5. Sizes & Border Widths

- `border-width/default`** (**`1px`**):** Standard borders.
- `size/icon-sm`** (**`16px`**):** Micro status icons.

## 6. Typography Scale (Text Styles)

| Style | Family | Weight | Size | Leading | Usage |
| --- | --- | --- | --- | --- | --- |
| `control/label` | Inter | SemiBold (600) | `14px` | `20px` | Standard button labels. |
| `control/label-sm` | Inter | Semi Bold (600) | `12px` | `16px` | Compact labels. |
| `display/heavy` | Newsreader | Extra Bold | `48px` | `56px` | Heavy hero. |

## 7. Shadows (Effect Styles)

| Effect | Kind | Offsets and blur | Colour | Usage |
| --- | --- | --- | --- | --- |
| `Elevation/Standard` | Drop shadow | `X: 0, Y: 4, Blur: 24, Spread: 0` | `#000000` @ `50%` | Resting shadow. |

### Accessibility Requirements

- Contrast.
"""

P, S = "C:prim", "C:sem"


def var(name: str, coll: str, value) -> list:
    mode = "m:p" if coll == P else "m:s"
    return [f"VariableID:{name}", name, "FLOAT", coll, {mode: value}, ["ALL_SCOPES"], ""]


def text_style(name: str, family: str, weight: str, size, line_height) -> list:
    return [f"S:{name}", name, family, weight, size, line_height, {"unit": "PERCENT", "value": 0}, "NONE", "NONE", ""]


EXPORT = {
    "file": "mini",
    "collections": [[P, "Primitives", [["m:p", "Mode 1"]], "m:p"], [S, "Semantic", [["m:s", "Mode 1"]], "m:s"]],
    "variables": [
        var("radius/8", P, 8), var("radius/control", S, "alias:radius/8"),
        var("space/0", P, 0), var("space/8", P, 8), var("space/control-y", S, "alias:space/8"),
        var("blue/500", P, "#3366CC"), var("text/nav-active", S, "alias:blue/500"),
        var("alpha/white-5", P, "#FFFFFF@0.05"), var("surface/hover-neutral", S, "alias:alpha/white-5"),
        var("alpha/crimson-5", P, "#b91c1c@0.05"), var("surface/hover-danger", S, "alias:alpha/crimson-5"),
        var("border/focus", S, "alias:blue/500"),
        var("border-width/default", S, 1), var("size/icon-sm", S, 16),
        var("orange/900", P, "#331100"),
    ],
    "text": [
        text_style("control/label", "Inter", "Semi Bold", 14, {"unit": "PIXELS", "value": 20}),
        text_style("control/label-sm", "Inter", "SemiBold", 12, {"unit": "PIXELS", "value": 16}),
        text_style("display/heavy", "Newsreader", "ExtraBold", 48, {"unit": "PIXELS", "value": 56}),
    ],
    "effect": [["S:e1", "Elevation/Standard", [["DROP_SHADOW", "#000000@0.5", 0, 4, 24, 0]], "Resting."]],
    "grid": [], "paint": [],
}


class TokenCheck(unittest.TestCase):
    def setUp(self):
        te.enter(self, CONFIG)
        self.tmp = Path(tempfile.mkdtemp(prefix="token-check-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_check(self, mirror: str = MIRROR, export=None, *, raw_export: str | None = None,
                  show_all: bool = False) -> tuple[int, str]:
        m, e = self.tmp / "mirror.md", self.tmp / "figma-export.json"
        m.write_text(mirror, encoding="utf-8")
        e.write_text(raw_export if raw_export is not None else json.dumps(EXPORT if export is None else export),
                     encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ctc.main(["--mirror", str(m), "--export", str(e)] + (["--all"] if show_all else []))
        return rc, out.getvalue()

    @staticmethod
    def export_with(**changes) -> dict:
        doc = copy.deepcopy(EXPORT)
        doc.update(changes)
        return doc

    @staticmethod
    def replace(old: str, new: str) -> str:
        assert MIRROR.count(old) == 1, old
        return MIRROR.replace(old, new)

    def test_every_form_matching_exits_0_with_counts_per_table(self):
        rc, out = self.run_check()
        self.assertEqual(0, rc, out)
        self.assertNotIn("MISMATCH", out)
        self.assertIn("checked: radius table 1, spacing table 1, colour note 1, colour table 3, "
                      "border-width bullet 1, size bullet 1, space primitive 2, text style 3, effect style 1", out)
        self.assertIn("EXPORT-ONLY (0): -", out)
        self.assertIn("0 mismatch(es), 0 export-only", out)

    def test_normalised_forms_match(self):
        rc, out = self.run_check(show_all=True)
        self.assertEqual(0, rc, out)
        for row in ("ok       [colour table] surface/hover-neutral: doc color/alpha/white-5 = #FFFFFF@5% "
                    "| export alpha/white-5 = #FFFFFF@5%",
                    "ok       [colour table] surface/hover-danger: doc color/alpha/crimson-5 = #B91C1C@5% "
                    "| export alpha/crimson-5 = #B91C1C@5%",
                    "ok       [colour table] border/focus: doc color/blue/500 = #3366CC",
                    "ok       [text style] control/label: doc ('Inter', 'SemiBold', '14', '20') "
                    "| export ('Inter', 'Semi Bold', '14', '20')",
                    "ok       [text style] control/label-sm: doc ('Inter', 'Semi Bold', '12', '16')",
                    "ok       [text style] display/heavy: doc ('Newsreader', 'Extra Bold', '48', '56')"):
            self.assertIn(row, out)

    def test_each_changed_form_is_one_mismatch_and_exit_1(self):
        cases = {
            "radius table": ("| `radius/8` | `8px` |", "| `radius/8` | `9px` |"),
            "spacing table": ("| `space/8` | `8px` | Vertical", "| `space/8` | `10px` | Vertical"),
            "colour note": ("(`#3366CC`) — Active", "(`#3366CD`) — Active"),
            "colour table": ("`#FFFFFF` @ `5%`", "`#FFFFFF` @ `6%`"),
            "border-width bullet": ("`1px`", "`2px`"),
            "size bullet": ("`16px`**", "`18px`**"),
            "space primitive": ("`space/0` (0px)", "`space/0` (1px)"),
            "text style": ("`14px` | `20px`", "`14px` | `22px`"),
            "effect style": ("Blur: 24", "Blur: 25"),
        }
        for table, (old, new) in cases.items():
            with self.subTest(table=table):
                rc, out = self.run_check(self.replace(old, new))
                self.assertEqual(1, rc, out)
                mismatches = [x for x in out.splitlines() if x.startswith("MISMATCH")]
                self.assertEqual(1, len(mismatches), out)
                self.assertIn(f"[{table}]", mismatches[0])

    def test_a_differing_alias_with_the_same_value_is_a_mismatch(self):
        doc = copy.deepcopy(EXPORT)
        doc["variables"].append(var("blue/main", P, "#3366CC"))
        doc["variables"] = [var("border/focus", S, "alias:blue/main") if v[1] == "border/focus" else v
                            for v in doc["variables"]]
        rc, out = self.run_check(export=doc)
        self.assertEqual(1, rc, out)
        self.assertIn("MISMATCH [colour table] border/focus: doc color/blue/500 = #3366CC "
                      "| export blue/main = #3366CC", out)

    def test_token_missing_from_export(self):
        doc = self.export_with(variables=[v for v in EXPORT["variables"] if v[1] != "border/focus"])
        rc, out = self.run_check(export=doc)
        self.assertEqual(1, rc, out)
        self.assertIn("MISMATCH [colour table] border/focus: MISSING in export (doc color/blue/500 = `#3366CC`)",
                      out)

    def test_unnamed_semantic_text_and_effect_are_export_only_and_exit_1(self):
        doc = copy.deepcopy(EXPORT)
        doc["variables"].append(var("surface/new", S, "alias:orange/900"))
        doc["text"].append(text_style("body/new", "Inter", "Regular", 14, {"unit": "PIXELS", "value": 20}))
        doc["effect"].append(["S:e9", "Glow/New", [["DROP_SHADOW", "#000000@0.5", 0, 0, 8, 0]], ""])
        rc, out = self.run_check(export=doc)
        self.assertEqual(1, rc, out)
        self.assertNotIn("MISMATCH", out)
        self.assertIn("EXPORT-ONLY (3): surface/new, text:body/new, effect:Glow/New", out)
        self.assertIn("0 mismatch(es), 3 export-only", out)
        self.assertNotIn("orange/900", out)

    def test_broken_alias_chain_is_a_mismatch(self):
        doc = copy.deepcopy(EXPORT)
        doc["variables"] = [var("radius/control", S, "alias:radius/99") if v[1] == "radius/control" else v
                            for v in doc["variables"]]
        rc, out = self.run_check(export=doc)
        self.assertEqual(1, rc, out)
        self.assertIn("MISMATCH [radius table] radius/control:", out)
        self.assertIn("<broken alias radius/99>", out)

    def test_two_layer_effect_is_a_mismatch(self):
        doc = copy.deepcopy(EXPORT)
        doc["effect"][0][2].append(["DROP_SHADOW", "#000000@0.2", 0, 1, 2, 0])
        rc, out = self.run_check(export=doc)
        self.assertEqual(1, rc, out)
        self.assertIn("MISMATCH [effect style] Elevation/Standard:", out)
        self.assertIn("['2 layers']", out)

    def test_non_pixel_or_non_object_line_height_is_a_mismatch_line(self):
        for line_height in ({"unit": "AUTO"}, {"unit": "PERCENT", "value": 140}, 20, None, {"unit": "PIXELS"}):
            with self.subTest(line_height=line_height):
                doc = copy.deepcopy(EXPORT)
                doc["text"][0] = text_style("control/label", "Inter", "Semi Bold", 14, line_height)
                rc, out = self.run_check(export=doc)
                self.assertEqual(1, rc, out)
                mismatches = [x for x in out.splitlines() if x.startswith("MISMATCH")]
                self.assertEqual(1, len(mismatches), out)
                self.assertIn("[text style] control/label:", mismatches[0])
                self.assertIn("non-pixel line height", mismatches[0])

    def test_end_heading_before_start_is_skipped(self):
        rc, out = self.run_check("### Accessibility Requirements\n\nEarly.\n\n" + MIRROR)
        self.assertEqual(0, rc, out)
        self.assertIn("effect style 1", out)

    def test_colour_notes_may_be_absent(self):
        rc, out = self.run_check(self.replace(
            "- **text/nav-active:** Add token role: `blue/500` (`#3366CC`) — Active tab labels.\n", ""))
        self.assertEqual(1, rc, out)
        self.assertIn("colour note 0", out)
        self.assertIn("EXPORT-ONLY (1): text/nav-active", out)

    def test_bad_input_exits_2(self):
        two_modes = copy.deepcopy(EXPORT)
        two_modes["variables"][0][4]["m:x"] = 9
        cases = {
            "two modes": (MIRROR, json.dumps(two_modes), "has 2 modes"),
            "no start": (self.replace("# Token Reference", "# Design Tokens"), None, "lacks"),
            "start not at a line start": (self.replace("# Token Reference", "x # Token Reference"),
                                          None, "lacks"),
            "no end": (self.replace("### Accessibility Requirements", "### Accessibility"), None, "lacks"),
            "end only before start": (
                "### Accessibility Requirements\n" + self.replace("### Accessibility Requirements", "### Other"),
                None, "lacks"),
            "unreadable json": (MIRROR, "{not json", "ERROR"),
            "non-object root": (MIRROR, "[1, 2]", "not a JSON object"),
        }
        for key in ctc.EXPORT_KEYS:
            doc = copy.deepcopy(EXPORT)
            del doc[key]
            cases[f"no {key}"] = (MIRROR, json.dumps(doc), f"lacks the list key(s): {key}")
        for name, (mirror, raw, needle) in cases.items():
            with self.subTest(name):
                rc, out = self.run_check(mirror, raw_export=raw if raw is not None else json.dumps(EXPORT))
                self.assertEqual(2, rc, out)
                self.assertIn(needle, out)
                self.assertNotIn("EXPORT-ONLY", out)

    def test_missing_export_file_exits_2(self):
        m = self.tmp / "mirror.md"
        m.write_text(MIRROR, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ctc.main(["--mirror", str(m), "--export", str(self.tmp / "absent.json")])
        self.assertEqual(2, rc)
        self.assertIn("ERROR", out.getvalue())

    def test_an_expected_table_matching_no_row_exits_2(self):
        def unquoted(*cells: str) -> list[tuple[str, str]]:
            return [(c, c.replace("`", "")) for c in cells]

        cases = {
            "radius table": unquoted("| `radius/control` |"),
            "spacing table": unquoted("| `space/control-y` |"),
            "colour table": unquoted("| `surface/hover-neutral` |", "| `surface/hover-danger` |", "| `border/focus` |"),
            "border-width bullet": unquoted("- `border-width/default`** (**`1px`**):**"),
            "size bullet": unquoted("- `size/icon-sm`** (**`16px`**):**"),
            "space primitive": [("`space/0` (0px), `space/8` (8px).", "space/0 = 0px, space/8 = 8px.")],
            "text style": unquoted("`14px` | `20px`", "`12px` | `16px` | Compact", "`48px` | `56px`"),
            "effect style": [("| Drop shadow |", "| Drop-shadow |")],
        }
        for table, changes in cases.items():
            with self.subTest(table=table):
                mirror = MIRROR
                for old, new in changes:
                    self.assertEqual(1, mirror.count(old), old)
                    mirror = mirror.replace(old, new)
                rc, out = self.run_check(mirror)
                self.assertEqual(2, rc, out)
                self.assertIn(f"{table} 0", out)
                self.assertIn(f"no row matched in: {table}", out)


if __name__ == "__main__":
    unittest.main()
