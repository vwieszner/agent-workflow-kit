"""Guards for the sprint-status tracker and sprint_status.py.

1. The LIVE tracker (the installed project's `config: paths.sprint_status`): no row note
   and no last_updated note may exceed `config: sprint_status.note_max_chars`. Skipped
   when no `.workflow/config.toml` or no tracker exists (e.g. in the kit source tree).
2. sprint_status.py: `set` archives the note it replaces, refuses over-cap notes and
   unknown statuses, `--history` and `history --append` write the history file, `touch`
   archives the old last_updated note, `--dry-run` writes nothing, and the configured
   history dir / status set / cap are honoured.
"""

import contextlib
import io
import json
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import wfconfig  # noqa: E402
import sprint_status  # noqa: E402

# Resolved at import, before any test redirects WORKFLOW_REPO_ROOT.
REAL_ROOT = wfconfig.repo_root()

MINI = """generated: 2026-01-01
last_updated: 2026-01-02 # old last_updated note
development_status:
  epic-1: done
  1-1-alpha: ready-for-dev # T3 bar-memory; dep: none; alpha scope
  1-2-beta: backlog
"""


def _note(line: str) -> str:
    m = sprint_status.ROW_RE.match(line) or sprint_status.LAST_UPDATED_RE.match(line)
    return (m.group("rest") or "").lstrip().lstrip("#").strip() if m else ""


def _live_tracker() -> tuple[Path | None, int]:
    cfg_path = REAL_ROOT / ".workflow" / "config.toml"
    if not cfg_path.is_file():
        return None, 0
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    rel = cfg.get("paths", {}).get("sprint_status", "docs/implementation-artifacts/sprint-status.yaml")
    cap = int(cfg.get("sprint_status", {}).get("note_max_chars", sprint_status.DEFAULT_NOTE_CAP))
    p = REAL_ROOT / rel
    return (p if p.is_file() else None), cap


class LiveTrackerCapTests(unittest.TestCase):
    def setUp(self):
        self.live, self.cap = _live_tracker()
        if self.live is None:
            self.skipTest("no installed .workflow/config.toml or sprint-status tracker in this repo")

    def test_every_row_note_within_cap(self):
        over = []
        for i, line in enumerate(self.live.read_text(encoding="utf-8").splitlines(), 1):
            m = sprint_status.ROW_RE.match(line)
            if m and len(_note(line)) > self.cap:
                over.append(f"L{i} {m.group('key')} ({len(_note(line))} chars)")
        self.assertEqual(
            over, [],
            f"sprint-status notes over the cap ({self.cap}); move the long form to the "
            "story's history file (`sprint_status.py history <key> --append`):\n" + "\n".join(over),
        )

    def test_last_updated_note_within_cap(self):
        for line in self.live.read_text(encoding="utf-8").splitlines():
            if sprint_status.LAST_UPDATED_RE.match(line):
                self.assertLessEqual(len(_note(line)), self.cap, line[:120])
                return
        self.fail("last_updated line not found")


class ScriptBehaviourTests(unittest.TestCase):
    CONFIG = ""

    def setUp(self):
        self.root = te.enter(self, self.CONFIG)
        self.path = self.root / "sprint-status.yaml"
        self.path.write_text(MINI, encoding="utf-8")
        self.hist = self.root / sprint_status.DEFAULT_HISTORY_DIRNAME

    def _run(self, *argv, path=True):
        out = io.StringIO()
        full = (["--path", str(self.path)] if path else []) + list(argv)
        with contextlib.redirect_stdout(out):
            try:
                code = sprint_status.main(full)
            except SystemExit as e:  # _die raises SystemExit(2)
                code = e.code
        return code, out.getvalue()

    def test_set_replaces_note_and_archives_old_one(self):
        code, out = self._run("set", "1-1", "done", "--note", "MERGED (merge abc1234)")
        self.assertEqual(code, 0, out)
        self.assertIn("1-1-alpha: done  # MERGED (merge abc1234)", self.path.read_text(encoding="utf-8"))
        hist = self.hist / "1-1-alpha.md"
        self.assertTrue(hist.is_file(), out)
        body = hist.read_text(encoding="utf-8")
        self.assertIn("status: ready-for-dev", body)
        self.assertIn("T3 bar-memory; dep: none; alpha scope", body)
        self.assertEqual(json.loads(out)["history_file"], str(hist))

    def test_set_rejects_over_cap_note_and_writes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        code, out = self._run("set", "1-1", "done", "--note", "x" * (sprint_status.note_cap() + 1))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "error")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse(self.hist.exists())

    def test_set_rejects_unknown_status_unless_forced(self):
        code, out = self._run("set", "1-2", "shelved")
        self.assertEqual(code, 2, out)
        code, out = self._run("set", "1-2", "shelved", "--force")
        self.assertEqual(code, 0, out)
        self.assertIn("1-2-beta: shelved", self.path.read_text(encoding="utf-8"))

    def test_set_history_flag_records_detail(self):
        code, _ = self._run("set", "1-2", "in-progress", "--note", "STARTED slot 3",
                            "--history", "Checkpoint 1: long detail here.")
        self.assertEqual(code, 0)
        self.assertIn("Checkpoint 1: long detail here.", (self.hist / "1-2-beta.md").read_text(encoding="utf-8"))

    def test_history_append_then_print(self):
        code, _ = self._run("history", "1-2", "--append", "Round 2 decisions.")
        self.assertEqual(code, 0)
        code, out = self._run("history", "1-2")
        self.assertEqual(code, 0)
        self.assertIn("Round 2 decisions.", out)
        self.assertIn("status: backlog", out)

    def test_touch_caps_and_archives_old_note(self):
        code, _ = self._run("touch", "--note", "y" * (sprint_status.note_cap() + 1))
        self.assertEqual(code, 2)
        code, _ = self._run("touch", "--date", "2026-01-03", "--note", "short sentence.")
        self.assertEqual(code, 0)
        self.assertIn("last_updated: 2026-01-03 # short sentence.", self.path.read_text(encoding="utf-8"))
        archived = (self.hist / f"{sprint_status.LAST_UPDATED_HISTORY_KEY}.md").read_text(encoding="utf-8")
        self.assertIn("old last_updated note", archived)

    def test_dry_run_writes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        code, out = self._run("--dry-run", "set", "1-1", "done", "--note", "MERGED")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse(self.hist.exists())

    def test_ambiguous_prefix_is_an_error(self):
        code, out = self._run("get", "1-")
        self.assertEqual(code, 2)
        self.assertIn("ambiguous", json.loads(out)["message"])


class ConfiguredBehaviourTests(unittest.TestCase):
    CONFIG = """
[paths]
sprint_status = "track/status.yaml"
story_history_dir = "track/hist"

[sprint_status]
note_max_chars = 10
statuses = ["todo", "done"]
"""

    def setUp(self):
        self.root = te.enter(self, self.CONFIG)
        (self.root / "track").mkdir()
        (self.root / "track" / "status.yaml").write_text(MINI, encoding="utf-8")

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = sprint_status.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_config_paths_statuses_and_cap_are_used(self):
        code, out = self._run("set", "1-1", "backlog")
        self.assertEqual(code, 2, "a status outside config sprint_status.statuses was accepted")
        code, out = self._run("set", "1-1", "todo", "--note", "x" * 11)
        self.assertEqual(code, 2, "a note over config note_max_chars was accepted")
        code, out = self._run("set", "1-1", "todo", "--note", "ok note")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "track" / "hist" / "1-1-alpha.md").is_file(),
                        "the replaced note was not archived under config paths.story_history_dir")


if __name__ == "__main__":
    unittest.main()
