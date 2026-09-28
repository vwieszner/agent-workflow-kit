"""Tests for the slot-registry parser shared by the writers and _registry_lookup, the
cleanup fallback's exact-match worktree resolution (a sibling story's worktree is never
reachable), post_merge_smoke's merge range (the merge, not the bookkeeping commit after
it), and check_settings_and_docs_integrity.py (hook wiring + quoted passages, zero checks
is a failure)."""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import _registry_lookup  # noqa: E402
import check_settings_and_docs_integrity as integrity  # noqa: E402
import cleanup_story_stack  # noqa: E402
import post_merge_smoke  # noqa: E402

SPECS = "docs/implementation-artifacts"
HEADER = ("| Slot | Status | Story ID | Branch | Worktree | Since |\n"
          "|------|--------|----------|--------|----------|-------|\n")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], check=True, capture_output=True, text=True).stdout


def write_registry(root: Path, rows: str) -> None:
    reg = root / SPECS / "slot-registry.md"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(HEADER + rows, encoding="utf-8")


class RegistryParserTests(unittest.TestCase):
    def test_lookup_reads_a_row_without_since_and_a_bom(self):
        root = te.enter(self, "")
        reg = root / SPECS / "slot-registry.md"
        reg.parent.mkdir(parents=True)
        reg.write_text("﻿" + HEADER + "| 2 | in_use | 7-2 | story/7-2 | wt/7-2 |\n",
                       encoding="utf-8")
        rows = _registry_lookup.rows()
        self.assertEqual([(r["slot"], r["story_id"], r["since"]) for r in rows], [(2, "7-2", "")])
        self.assertEqual(_registry_lookup.find("7-2")["worktree_path"], (root / "wt" / "7-2").resolve())


class CleanupResolveTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, '[stack]\nruntime = "none"\n[git]\nworktrees_root = "wts"\n')
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "f.txt").write_text("x\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        self.wts = self.root / "wts"
        git(self.root, "worktree", "add", "-q", "-b", "story/5-1", str(self.wts / "5-1"), "main")
        write_registry(self.root, "| 1 | free | — | — | — | — |\n")

    def test_a_sibling_story_worktree_is_never_resolved(self):
        _slot, branch, wt = cleanup_story_stack.resolve_worktree("5", self.wts)
        self.assertNotEqual(wt, self.wts / "5-1")
        self.assertNotEqual(branch, "story/5-1")

    def test_absent_dir_resolves_to_the_exact_path_for_an_idempotent_rerun(self):
        slot, branch, wt = cleanup_story_stack.resolve_worktree("5", self.wts)
        # "5" has no dir of its own: the resolution must not be 5-1's.
        self.assertIsNone(slot)
        self.assertEqual(branch, "story/5")
        self.assertEqual(wt, self.wts / "5")

    def test_exact_registered_worktree_on_its_branch_resolves(self):
        git(self.root, "worktree", "add", "-q", "-b", "story/5", str(self.wts / "5"), "main")
        slot, branch, wt = cleanup_story_stack.resolve_worktree("5", self.wts)
        self.assertEqual((slot, branch, wt), (None, "story/5", self.wts / "5"))

    def test_a_plain_dir_that_git_does_not_register_is_refused(self):
        (self.wts / "9").mkdir(parents=True)
        with self.assertRaises(LookupError):
            cleanup_story_stack.resolve_worktree("9", self.wts)

    def test_a_dir_the_registry_assigns_to_another_story_is_refused(self):
        git(self.root, "worktree", "add", "-q", "-b", "story/5", str(self.wts / "5"), "main")
        write_registry(self.root, f"| 1 | in_use | 7-7 | story/7-7 | {self.wts / '5'} | 2026-01-01 |\n")
        with self.assertRaises(LookupError) as cm:
            cleanup_story_stack.resolve_worktree("5", self.wts)
        self.assertIn("7-7", str(cm.exception))


class MergeRangeTests(unittest.TestCase):
    def test_range_is_the_merge_not_the_bookkeeping_commit_after_it(self):
        root = te.enter(self, "")
        git(root, "init", "-q", "-b", "main")
        (root / "a.txt").write_text("a\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        git(root, "checkout", "-qb", "story/1")
        (root / "migrations").mkdir()
        (root / "migrations" / "0001_initial.py").write_text("x = 1\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "schema")
        git(root, "checkout", "-q", "main")
        git(root, "merge", "-q", "--no-ff", "-m", "merge story/1", "story/1")
        (root / "docs.md").write_text("bookkeeping\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "bookkeeping")
        rng, label = post_merge_smoke.merge_range(root)
        self.assertTrue(label.startswith("merge "), label)
        files = git(root, "diff", "--name-only", *rng).split()
        self.assertEqual(files, ["migrations/0001_initial.py"])


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, "")

    def _main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = integrity.main([])
        return code, out.getvalue()

    def _settings(self, commands):
        hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": c} for c in commands]}]}
        path = self.root / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    def _hook(self, rel):
        path = self.root / ".workflow" / "hooks" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hook\n", encoding="utf-8")

    def test_no_adapter_at_all_fails(self):
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn("zero-check", out)

    def test_adapters_with_zero_references_fail(self):
        self._settings([])
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn("ZERO", out)

    def test_a_dangling_settings_hook_fails_and_a_present_one_passes(self):
        self._settings(['uv run --no-project "$CLAUDE_PROJECT_DIR/.workflow/hooks/guards/bash_command.py"'])
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn(".workflow/hooks/guards/bash_command.py", out)
        self._hook("guards/bash_command.py")
        code, out = self._main()
        self.assertEqual(code, 0, out)

    def test_plugin_hook_paths_resolve_under_workflow_hooks(self):
        plugin = self.root / ".opencode" / "plugins" / "workflow-hooks.ts"
        plugin.parent.mkdir(parents=True)
        plugin.write_text('const G = { Bash: ["guards/bash_command.py"] }\n'
                          'await runHook("session/session_journal.py", p)\n', encoding="utf-8")
        self._hook("guards/bash_command.py")
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn(".workflow/hooks/session/session_journal.py", out)

    def test_an_untracked_hook_script_fails_in_a_git_checkout(self):
        git(self.root, "init", "-q", "-b", "main")
        self._settings(['uv run "$CLAUDE_PROJECT_DIR/.workflow/hooks/guards/bash_command.py"'])
        self._hook("guards/bash_command.py")
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn("NOT git-tracked", out)
        git(self.root, "add", "-A")
        code, out = self._main()
        self.assertEqual(code, 0, out)

    def test_a_quoted_passage_that_was_removed_fails(self):
        self._settings(['uv run "$CLAUDE_PROJECT_DIR/.workflow/hooks/guards/bash_command.py"'])
        self._hook("guards/bash_command.py")
        (self.root / "RULES.md").write_text("## Test-fix iteration\n", encoding="utf-8")
        cfg = self.root / ".workflow" / "config.toml"
        cfg.write_text('[integrity]\nquoted_passages = [\n'
                       '  { file = "RULES.md", text = "Test-fix iteration" },\n'
                       '  { file = "RULES.md", text = "Gone section" },\n]\n', encoding="utf-8")
        te.clear_caches()
        code, out = self._main()
        self.assertEqual(code, 1, out)
        self.assertIn("Gone section", out)
        self.assertNotIn("Test-fix iteration'", out)


if __name__ == "__main__":
    unittest.main()
