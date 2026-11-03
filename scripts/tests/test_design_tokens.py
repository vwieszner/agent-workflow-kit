"""Tests for design_tokens export: chunk reassembly from session transcripts, the failure cases and the
sorted, one-row-per-line output."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _state_testenv as te  # noqa: E402
import design_tokens  # noqa: E402

EXPORT = Path("web/tokens/export.json")
CONFIG = '''[stack]
runtime = "docker-compose"
compose_cmd = "docker compose"
[design]
tokens_export = "web/tokens/export.json"
tokens_build = { service = "frontend", cmd = ["npm", "run", "tokens:build"], inputs = [["web/tokens/export.json", "/app/tokens/export.json"], ["web/tokens/local.json", "/app/tokens/local.json"]], copy_back = [["web/tokens/generated", "/app/tokens/generated"]] }
'''

PAYLOAD = {
    "file": "LIBRARY-FILE",
    "collections": [["VariableCollectionId:1:1", "Primitives", [["1:0", "Mode 1"]], "1:0"]],
    "variables": [
        ["VariableID:1:2", "cyan/500", "COLOR", "VariableCollectionId:1:1", {"1:0": "#00F0FF"}, [], ""],
        ["VariableID:1:1", "charcoal/900", "COLOR", "VariableCollectionId:1:1", {"1:0": "#161B22"}, [], "café \U0001f3b2"],
    ],
    "text": [],
    "effect": [],
    "paint": [],
    "grid": [],
}


def chunk(tag, part, parts, total, body):
    return f"@@ML|styles|{tag}|{part}|{parts}|{total}\n{body}"


def tool_result_line(text, as_list=False):
    content = [{"type": "text", "text": text}] if as_list else text
    return json.dumps({"message": {"content": [{"type": "tool_result", "content": content}]}})


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def split(text, size):
    return [text[i : i + size] for i in range(0, len(text), size)]


def split_utf16(text, size):
    """Cut on UTF-16 code units, as the JS chunker does; a cut inside a pair leaves lone surrogates."""
    raw = text.encode("utf-16-le")
    return [raw[i : i + 2 * size].decode("utf-16-le", "surrogatepass") for i in range(0, len(raw), 2 * size)]


class ExportTests(unittest.TestCase):
    def setUp(self):
        te.enter(self, CONFIG)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()

    def write_transcript(self, session, lines, subagent=None):
        if subagent:
            path = self.transcripts / session / "subagents" / f"{subagent}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = self.transcripts / f"{session}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def chunk_lines(self, tag, payload=PAYLOAD, size=60, as_list=False, drop=None):
        text = json.dumps(payload, ensure_ascii=False)
        pieces = split(text, size)
        total = utf16_length(text)
        return [
            tool_result_line(chunk(tag, i, len(pieces), total, piece), as_list)
            for i, piece in enumerate(pieces)
            if i != drop
        ]

    def run_export(self, tag, session=None):
        args = ["export", "--tag", tag, "--transcripts-dir", str(self.transcripts), "--worktree", str(self.worktree)]
        if session:
            args += ["--session", session]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            design_tokens.main(args)
        return out.getvalue()

    def written(self):
        return (self.worktree / EXPORT).read_text(encoding="utf-8")

    def test_reassembles_chunks_and_writes_sorted_rows_one_per_line(self):
        self.write_transcript("s1", self.chunk_lines("t1"))
        self.run_export("t1", "s1")
        text = self.written()
        data = json.loads(text)
        self.assertEqual([row[1] for row in data["variables"]], ["charcoal/900", "cyan/500"])
        self.assertEqual(data["variables"][0][6], PAYLOAD["variables"][1][6])
        self.assertTrue(text.endswith("}\n"))
        row_lines = [line for line in text.splitlines() if line.startswith('    ["VariableID')]
        self.assertEqual(len(row_lines), 2)

    def test_unchanged_set_rewrites_identical_bytes_whatever_the_list_order(self):
        self.write_transcript("s1", self.chunk_lines("t1"))
        self.run_export("t1", "s1")
        first = self.written()
        shuffled = dict(PAYLOAD, variables=list(reversed(PAYLOAD["variables"])))
        self.write_transcript("s2", self.chunk_lines("t2", shuffled))
        self.run_export("t2", "s2")
        self.assertEqual(self.written(), first)

    def test_reads_text_item_lists_and_json_quoted_strings(self):
        quoted = [json.dumps(json.loads(line)["message"]["content"][0]["content"]) for line in self.chunk_lines("t1")]
        lines = [tool_result_line(text) for text in quoted]
        self.write_transcript("s1", lines)
        self.run_export("t1", "s1")
        self.assertEqual(json.loads(self.written())["file"], PAYLOAD["file"])

        self.write_transcript("s2", self.chunk_lines("t2", as_list=True))
        self.run_export("t2", "s2")
        self.assertEqual(json.loads(self.written())["file"], PAYLOAD["file"])

    def test_reads_a_subagent_transcript_of_the_session(self):
        self.write_transcript("s1", self.chunk_lines("t1"), subagent="agent-a")
        self.run_export("t1", "s1")
        self.assertEqual(json.loads(self.written())["file"], PAYLOAD["file"])

    def test_a_chunk_resent_under_the_same_tag_overwrites_its_part(self):
        lines = self.chunk_lines("t1")
        self.write_transcript("s1", lines + lines)
        self.run_export("t1", "s1")
        self.assertEqual(json.loads(self.written())["file"], PAYLOAD["file"])

    def test_other_tags_in_the_same_session_are_ignored(self):
        self.write_transcript("s1", self.chunk_lines("old", dict(PAYLOAD, file="stale")) + self.chunk_lines("t1"))
        self.run_export("t1", "s1")
        self.assertEqual(json.loads(self.written())["file"], PAYLOAD["file"])

    def test_an_incomplete_tag_fails_and_writes_nothing(self):
        self.write_transcript("s1", self.chunk_lines("t1", drop=1))
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.run_export("t1", "s1")
        self.assertFalse((self.worktree / EXPORT).exists())

    def test_a_utf16_length_mismatch_fails_and_writes_nothing(self):
        text = json.dumps(PAYLOAD, ensure_ascii=False)
        wrong = utf16_length(text) - 1  # a code-point count of a payload holding an astral character
        pieces = split(text, 80)
        lines = [tool_result_line(chunk("t1", i, len(pieces), wrong, piece)) for i, piece in enumerate(pieces)]
        self.write_transcript("s1", lines)
        with self.assertRaisesRegex(SystemExit, "UTF-16"):
            self.run_export("t1", "s1")
        self.assertFalse((self.worktree / EXPORT).exists())

    def test_a_tag_in_two_sessions_needs_a_session(self):
        self.write_transcript("s1", self.chunk_lines("t1"))
        self.write_transcript("s2", self.chunk_lines("t1"))
        with self.assertRaisesRegex(SystemExit, "pass --session"):
            self.run_export("t1")
        self.assertFalse((self.worktree / EXPORT).exists())
        self.run_export("t1", "s2")
        self.assertEqual(json.loads(self.written())["file"], PAYLOAD["file"])

    def test_a_tag_with_no_chunks_fails_naming_the_tag(self):
        self.write_transcript("s1", self.chunk_lines("other"))
        with self.assertRaisesRegex(SystemExit, "t1"):
            self.run_export("t1", "s1")
        self.assertFalse((self.worktree / EXPORT).exists())

    def test_chunks_of_one_session_with_different_headers_fail(self):
        text = json.dumps(PAYLOAD, ensure_ascii=False)
        half = len(text) // 2
        lines = [
            tool_result_line(chunk("t1", 0, 2, utf16_length(text), text[:half])),
            tool_result_line(chunk("t1", 1, 3, utf16_length(text), text[half:])),
        ]
        self.write_transcript("s1", lines)
        with self.assertRaisesRegex(SystemExit, "different headers"):
            self.run_export("t1", "s1")
        self.assertFalse((self.worktree / EXPORT).exists())

    def test_a_tag_found_only_in_a_subagent_transcript_maps_to_its_session_id(self):
        self.write_transcript("s1", self.chunk_lines("t1"), subagent="agent-a")
        out = self.run_export("t1")
        self.assertIn("session s1", out)
        self.assertEqual(json.loads(self.written())["file"], PAYLOAD["file"])

    def test_a_main_and_a_subagent_transcript_of_one_session_count_as_one_session(self):
        lines = self.chunk_lines("t1")
        self.write_transcript("s1", lines[::2])
        self.write_transcript("s1", lines[1::2], subagent="agent-a")
        out = self.run_export("t1")
        self.assertIn("session s1", out)
        self.assertEqual(json.loads(self.written())["file"], PAYLOAD["file"])

    def test_a_chunk_cut_between_the_halves_of_a_surrogate_pair_survives_the_export(self):
        text = json.dumps(PAYLOAD, ensure_ascii=False)
        size = utf16_length(text[: text.index("\U0001f3b2")]) + 1
        pieces = split_utf16(text, size)
        self.assertTrue(any(piece.endswith("\ud83c") for piece in pieces))
        lines = [tool_result_line(chunk("t1", i, len(pieces), utf16_length(text), piece)) for i, piece in enumerate(pieces)]
        self.write_transcript("s1", lines)
        self.run_export("t1", "s1")
        self.assertEqual(json.loads(self.written())["variables"][0][6], PAYLOAD["variables"][1][6])

    def test_an_unpaired_surrogate_fails_without_truncating_the_existing_export(self):
        lone = dict(PAYLOAD, variables=[PAYLOAD["variables"][0][:6] + ["broken \ud83c"]])
        text = json.dumps(lone, ensure_ascii=False)
        total = len(text.encode("utf-16-le", "surrogatepass")) // 2
        pieces = split(text, 60)
        self.write_transcript("s1", [tool_result_line(chunk("t1", i, len(pieces), total, piece)) for i, piece in enumerate(pieces)])
        existing = self.worktree / EXPORT
        existing.parent.mkdir(parents=True)
        existing.write_text("committed export\n", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "unpaired UTF-16 surrogate"):
            self.run_export("t1", "s1")
        self.assertEqual(existing.read_text(encoding="utf-8"), "committed export\n")


class BuildTests(unittest.TestCase):
    def test_build_copies_the_host_inputs_into_the_container_before_running_the_build(self):
        te.enter(self, CONFIG)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(design_tokens, "run") as run:
            self.assertEqual(design_tokens.main(["build", "--project-name", "story-x", "--worktree", tmp]), 0)
        commands = [call.args[0] for call in run.call_args_list]
        build = next(i for i, cmd in enumerate(commands) if "tokens:build" in cmd)
        copied_in = [cmd for cmd in commands[:build] if "cp" in cmd and cmd[-1].startswith("frontend:")]
        self.assertEqual([cmd[-1] for cmd in copied_in],
                         ["frontend:/app/tokens/export.json", "frontend:/app/tokens/local.json"])
        self.assertEqual([Path(cmd[-2]) for cmd in copied_in],
                         [Path(tmp) / EXPORT, Path(tmp) / "web/tokens/local.json"])
        self.assertEqual(commands[-1][-2:], ["frontend:/app/tokens/generated/.", str(Path(tmp) / "web/tokens/generated")])

    def test_unconfigured_build_and_export_are_skipped(self):
        te.enter(self, "")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(design_tokens.main(["build", "--project-name", "story-x"]), 3)
            self.assertEqual(design_tokens.main(["export", "--tag", "t"]), 3)
        self.assertIn("SKIPPED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
