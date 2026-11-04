"""Tests for design_library's fingerprint import and --plan.

raw/hashes.json decides what a refresh re-extracts, and its ``at`` time decides whether build() marks a value sheet
STALE, so a wrong baseline move hides a Figma change without any error. Pinned here: --accept-fingerprints takes
every fingerprint as the baseline; a changed key is planned and an unchanged one is not; an extraction clears a key
only when it is at or after the fingerprint, and a frame needs both its frame map and its shallow dump; a changed
hash with no newer extraction keeps its old hash and ``at``; a node missing from its page's latest fingerprint is
reported removed; a changed component list is planned; page bounds measured twice keep the newest result, whatever
the transcript-file order. The selection is asserted on raw/refresh_plan.json; the
STEP 1 and removed-node messages are checked as text. Synthetic manifest, transcripts and raw/ in a temp dir: the
real library build, the manifest and design_extract.js are never read or written.

Usage
-----
    python3 -m unittest scripts/tests/test_design_library.py

When to use
-----------
    After any edit to import_transcripts, update_baseline, extraction_ts or plan in design_library.py.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import design_library as fml  # noqa: E402

DS, WS = "DS-FILE", "WS-FILE"
DS_PAGE, WS_PAGE = "2:2", "0:1"
FRAME = "5:5"
MANIFEST = {"design_source": {"library_file_key": DS, "screens_file_key": WS,
                              "frames": [{"slug": "frame", "node": FRAME}]}}
T0 = "2026-10-10T10:00:00.000Z"
T_EARLY = "2026-10-10T10:30:00.000Z"
T1 = "2026-10-10T11:00:00.000Z"
T2 = "2026-10-10T11:05:00.000Z"


def planned(plan, role, key):
    return [i for ids, _ in plan[role][key] for i in ids]


class PlanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.tx = root / "tx"
        self.tx.mkdir()
        self.raw = root / "ml" / "raw"
        (self.raw / "frames").mkdir(parents=True)
        (self.raw / "all_components.json").write_text(
            json.dumps({"pages": {DS_PAGE: {"name": "Components", "rows": []}}}), encoding="utf-8")
        (self.raw / "page_bounds.json").write_text(
            json.dumps({"ds": {}, "ws": {WS_PAGE: {"renderUnion": [0, 0, 10, 10]}}}), encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
        for patcher in (mock.patch.object(fml, "OUT", root / "ml"),
                        mock.patch.object(fml._transcripts, "project_dirs", return_value=[str(self.tx)]),
                        mock.patch.object(fml, "MANIFEST", manifest),
                        mock.patch.object(fml, "extractor_chunk", return_value=14000)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.lines = []

    def chunk(self, kind, tag, payload, ts):
        text = json.dumps(payload)
        body = f"@@ML|{kind}|{tag}|0|1|{len(text)}\n{text}"
        self.lines.append({"timestamp": ts, "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": body}]}]}})

    def fp_ds(self, tag, ts, nodes, list_hash="L1"):
        self.chunk("fp", tag, {"file": DS, "nodes": {n: [h, DS_PAGE] for n, h in nodes.items()},
                               "pages": {DS_PAGE: {"list": list_hash, "renderUnion": [0, 0, 10, 10]}},
                               "styles": "S1"}, ts)

    def fp_ws(self, tag, ts, frame_hash):
        self.chunk("fp", tag, {"file": WS, "nodes": {FRAME: [frame_hash, WS_PAGE]},
                               "pages": {WS_PAGE: {"top": "T1", "flow": "F1", "renderUnion": [0, 0, 10, 10]}},
                               "styles": None}, ts)

    def dump_ds(self, tag, ts, nid):
        self.chunk("dump", tag, {"file": DS, "nodes": {nid: {"document": {"i": nid, "n": "Set", "t": "COMPONENT_SET"}}}},
                   ts)

    def frame_map(self, tag, ts):
        self.chunk("frames", tag, {"file": WS, "frames": {FRAME: {"name": "F", "page": [WS_PAGE, "P"], "b": None,
                                                                  "groups": {}, "texts": []}}}, ts)

    def shallow(self, tag, ts):
        self.chunk("shallow", tag, {"file": WS, "nodes": {FRAME: {"document": {"i": FRAME, "n": "F", "t": "FRAME"}}}},
                   ts)

    def import_chunks(self, accept=False):
        (self.tx / "s.jsonl").write_text("\n".join(json.dumps(x) for x in self.lines) + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            fml.import_transcripts("s", accept)

    def plan(self):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            fml.plan(MANIFEST)
        path = self.raw / "refresh_plan.json"
        return (json.loads(path.read_text(encoding="utf-8")) if path.exists() else None), printed.getvalue()

    def baseline(self):
        return json.loads((self.raw / "hashes.json").read_text(encoding="utf-8"))

    def bootstrap(self):
        self.fp_ds("a-fp-ds", T0, {"1:1": "aaaa", "1:2": "bbbb"})
        self.fp_ws("a-fp-ws", T0, "ffff")
        self.import_chunks(accept=True)

    def test_no_fingerprints_prints_step_one_only(self):
        plan, printed = self.plan()
        self.assertIsNone(plan)
        self.assertIn("run STEP 1 first", printed)

    def test_accept_takes_every_fingerprint_as_the_baseline(self):
        self.bootstrap()
        base = self.baseline()
        self.assertEqual((base["ds:node:1:1"]["hash"], base["ds:node:1:2"]["hash"], base[f"ws:node:{FRAME}"]["hash"]),
                         ("aaaa", "bbbb", "ffff"))
        plan, _ = self.plan()
        self.assertEqual(planned(plan, "ds", "dump"), [])
        self.assertEqual(planned(plan, "ws", "frameMap"), [])
        self.assertEqual(planned(plan, "ws", "shallow"), [])
        self.assertEqual((plan["ds"]["list"], plan["ds"]["styles"], plan["ws"]["top"], plan["ws"]["flow"]),
                         ([], False, [], []))

    def test_changed_keys_are_planned_and_unchanged_ones_are_not(self):
        self.bootstrap()
        self.fp_ds("b-fp-ds", T1, {"1:1": "a2a2", "1:2": "bbbb"}, list_hash="L2")
        self.fp_ws("b-fp-ws", T1, "f2f2")
        self.import_chunks()
        plan, _ = self.plan()
        self.assertEqual(planned(plan, "ds", "dump"), ["1:1"])
        self.assertEqual(planned(plan, "ws", "frameMap"), [FRAME])
        self.assertEqual(planned(plan, "ws", "shallow"), [FRAME])
        self.assertEqual(plan["ds"]["list"], [DS_PAGE])
        self.assertEqual(plan["ws"]["flow"], [])

    def test_changed_hash_without_newer_extraction_keeps_its_baseline(self):
        self.bootstrap()
        self.fp_ds("b-fp-ds", T1, {"1:1": "a2a2", "1:2": "bbbb"})
        self.fp_ws("b-fp-ws", T1, "ffff")
        self.import_chunks()
        base = self.baseline()
        self.assertEqual((base["ds:node:1:1"]["hash"], base["ds:node:1:1"]["at"]), ("aaaa", T0))
        self.assertEqual((base["ds:node:1:2"]["hash"], base["ds:node:1:2"]["at"]), ("bbbb", T1))

    def test_extraction_at_or_after_the_fingerprint_clears_a_key(self):
        self.bootstrap()
        self.fp_ds("b-fp-ds", T1, {"1:1": "a2a2", "1:2": "bbbb"})
        self.fp_ws("b-fp-ws", T1, "f2f2")
        self.dump_ds("c-ds-1", T2, "1:1")
        self.frame_map("c-fm-1", T2)
        self.import_chunks()
        plan, _ = self.plan()
        self.assertEqual(planned(plan, "ds", "dump"), [])
        self.assertEqual(self.baseline()["ds:node:1:1"]["hash"], "a2a2")
        self.assertEqual(planned(plan, "ws", "frameMap"), [FRAME])
        self.shallow("c-sh-1", T2)
        self.import_chunks()
        plan, _ = self.plan()
        self.assertEqual(planned(plan, "ws", "frameMap"), [])
        self.assertEqual(planned(plan, "ws", "shallow"), [])

    def test_extraction_older_than_the_fingerprint_does_not_clear_a_key(self):
        self.bootstrap()
        self.dump_ds("b-ds-1", T_EARLY, "1:1")
        self.fp_ds("c-fp-ds", T1, {"1:1": "a2a2", "1:2": "bbbb"})
        self.fp_ws("c-fp-ws", T1, "ffff")
        self.import_chunks()
        plan, _ = self.plan()
        self.assertEqual(planned(plan, "ds", "dump"), ["1:1"])
        self.assertEqual(self.baseline()["ds:node:1:1"]["hash"], "aaaa")

    def test_node_missing_from_its_page_fingerprint_is_reported_removed(self):
        self.bootstrap()
        self.fp_ds("b-fp-ds", T1, {"1:1": "aaaa"})
        self.fp_ws("b-fp-ws", T1, "ffff")
        self.import_chunks()
        plan, printed = self.plan()
        self.assertEqual(plan["ds"]["removed"], ["1:2"])
        self.assertIn("Gone from Figma since the last check: 1:2", printed)
        self.assertEqual(planned(plan, "ds", "dump"), [])

    def test_page_bounds_keep_the_newest_result_whatever_the_transcript_order(self):
        sub = self.tx / "s" / "subagents"
        sub.mkdir(parents=True)
        for name, ts, union in (("agent-a.jsonl", T2, [-20, 0, 10, 10]), ("agent-b.jsonl", T1, [-38, -13, 10, 10])):
            text = json.dumps({"bounds": {WS_PAGE: {"renderUnion": union}}})
            (sub / name).write_text(json.dumps({"timestamp": ts, "message": {"content": [
                {"type": "tool_result", "content": [{"type": "text", "text": text}]}]}}) + "\n", encoding="utf-8")
        self.bootstrap()
        bounds = json.loads((self.raw / "page_bounds.json").read_text(encoding="utf-8"))
        self.assertEqual(bounds["ws"][WS_PAGE]["renderUnion"], [-20, 0, 10, 10])


if __name__ == "__main__":
    unittest.main()
