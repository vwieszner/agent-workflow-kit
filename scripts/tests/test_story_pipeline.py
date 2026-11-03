"""Tests for the story-pipeline scripts ported in the land-window / review-loop sync:
story_record (open / rule / reach / latest-line duplicates / checkpoint-1 refusal), story_ledger
(merge approval, review loop, seed ACs, re-run due, the pre-merge land guard against real git),
land_lease, merge_train helpers, sprint_status (multi-get, list filters, history swap, note clear),
stage_own_hunks (gate + verify), branch_overlap, story_refs and the effort-variant generator."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402

import branch_overlap  # noqa: E402
import gen_agent_effort_variants as gen  # noqa: E402
import land_lease  # noqa: E402
import merge_train  # noqa: E402
import sprint_status  # noqa: E402
import story_ledger  # noqa: E402
import story_record  # noqa: E402
import story_refs  # noqa: E402

SPECS = "docs/implementation-artifacts"


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def git(cwd: Path, *args: str) -> str:
    return run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args], cwd)


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args)
    return code, out.getvalue() + err.getvalue()


class _WorktreeCase(unittest.TestCase):
    """A main checkout with a story worktree registered in the slot registry."""

    config = '[stack]\nruntime = "none"\n'

    def setUp(self):
        self.root = te.enter(self, self.config)
        self.wt = self.root / "worktrees" / "7-2"
        (self.wt / SPECS).mkdir(parents=True)
        (self.wt / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")
        (self.root / SPECS).mkdir(parents=True)
        (self.root / SPECS / "slot-registry.md").write_text(
            "| Slot | Status | Story ID | Branch | Worktree | Since |\n"
            "|------|--------|----------|--------|----------|-------|\n"
            f"| 3 | in_use | 7-2 | story/7-2 | {self.wt} | 2026-01-01 |\n", encoding="utf-8")
        self.spec = self.wt / SPECS / "7-2-some-feature.md"
        self.spec.write_text("# spec\n", encoding="utf-8")

    def rec(self, *argv):
        return quiet(story_record.main, list(argv))


class StoryRecordTests(_WorktreeCase):
    config = '[stack]\nruntime = "none"\n[spec]\nexternal_question_tags = ["Designer"]\n'

    def test_checkpoint1_refused_while_an_open_question_stands(self):
        self.spec.write_text("# spec\nAC1 [OPEN-Q 1 which colour? (a), recommended]\n", encoding="utf-8")
        code, out = self.rec("append", "7-2", "checkpoint-1 spec approved")
        self.assertEqual(code, 1, out)
        self.assertIn("open items", out)
        self.assertEqual(self.rec("open", "7-2")[0], 1)
        code, out = self.rec("rule", "7-2", "1=b")
        self.assertEqual(code, 0, out)
        self.assertIn("OPEN-Q 1 which colour? (b)", self.spec.read_text(encoding="utf-8"))
        self.assertEqual(self.rec("append", "7-2", "checkpoint-1 spec approved")[0], 0)
        self.assertEqual(self.rec("check", "7-2", "checkpoint-1")[0], 0)

    def test_rule_refuses_external_owner_multi_number_and_missing_markers(self):
        self.spec.write_text("# spec\n[OPEN-Q 1] (Designer) colour\n[OPEN-Q 2, 3] both\n", encoding="utf-8")
        before = self.spec.read_text(encoding="utf-8")
        self.assertEqual(self.rec("rule", "7-2", "1=a")[0], 2)
        self.assertEqual(self.rec("rule", "7-2", "2=a")[0], 2)
        self.assertEqual(self.rec("rule", "7-2", "9=a")[0], 2)
        self.assertEqual(self.rec("rule", "7-2", "1=a", "1=b")[0], 2)
        self.assertEqual(before, self.spec.read_text(encoding="utf-8"), "a refused rule changed the spec")

    def test_incomplete_spec_draft_is_an_open_item(self):
        self.rec("append", "7-2", "spec-draft incomplete (step-3)")
        self.assertEqual(self.rec("open", "7-2")[0], 1)
        self.rec("append", "7-2", "spec-draft complete (story-spec)")
        self.assertEqual(self.rec("open", "7-2")[0], 0)

    def test_duplicate_is_judged_against_the_latest_line_of_the_key(self):
        self.rec("append", "7-2", "checkpoint-3 merge approved")
        self.rec("append", "7-2", "checkpoint-3 approval withdrawn -- pre-merge gate exit 1")
        self.assertEqual(self.rec("check", "7-2", "checkpoint-3")[0], 1, "a withdrawal did not un-satisfy")
        self.rec("append", "7-2", "checkpoint-3 merge approved")
        self.assertEqual(self.rec("check", "7-2", "checkpoint-3")[0], 0, "a same-day re-approval was dropped")
        text = (self.wt / SPECS / "7-2-record.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("checkpoint-3 merge approved"), 2)

    def test_reach_is_skipped_without_frontend_prefixes(self):
        code, out = self.rec("reach", "7-2")
        self.assertEqual(code, 0)
        self.assertIn("SKIPPED", out)


class ReachTests(_WorktreeCase):
    config = '[stack]\nruntime = "none"\n[ui]\nfrontend_src_prefixes = ["web/src/"]\n'

    def test_reach_missing_when_the_spec_names_frontend_source(self):
        self.spec.write_text("# spec\nTouches web/src/pages/Home.jsx and web/src/pages/Home.test.jsx\n",
                             encoding="utf-8")
        code, out = self.rec("reach", "7-2")
        self.assertEqual(code, 1, out)
        self.assertIn("web/src/pages/Home.jsx", out)
        self.assertNotIn("Home.test.jsx", out)
        self.spec.write_text("# spec\nweb/src/pages/Home.jsx\n\n## UI states and how each is reached\n",
                             encoding="utf-8")
        self.assertEqual(self.rec("reach", "7-2")[0], 0)


class LedgerUnitTests(unittest.TestCase):
    def test_merge_approval_reads_the_latest_verdict(self):
        rec = ["- 2026-01-01 checkpoint-3 merge approved",
               "- 2026-01-02 NEXT: checkpoint-3 again after the gate",
               "- 2026-01-02 checkpoint-3 approval withdrawn -- pre-merge gate exit 1"]
        self.assertIsNone(story_ledger.merge_approval(rec)[0])
        self.assertIsNotNone(story_ledger.merge_approval(rec + ["- 2026-01-03 checkpoint-3 merge approved"])[0])
        self.assertIsNone(story_ledger.merge_approval([])[0])

    def test_negative_words_match_inflected_whole_words(self):
        self.assertFalse(story_ledger._line_has("phase-2 tests GREEN then not running", ("phase-2",),
                                                story_ledger._GREEN, story_ledger._RED))
        self.assertTrue(story_ledger._line_has("phase-2 tests GREEN rendered", ("phase-2",),
                                               story_ledger._GREEN, story_ledger._RED))

    def test_rerun_after_green_and_seed_tokens(self):
        rec = ["- 2026-01-01 phase-2 tests GREEN -- a (exit 0)",
               "- 2026-01-01 review round 1 recorded",
               "- 2026-01-02 phase-2 tests GREEN -- a (exit 0)"]
        self.assertEqual(story_ledger._rerun_after_green(rec), 1)
        self.assertIsNone(story_ledger._rerun_after_green(rec[:2]))
        named = story_ledger._seed_acs_implemented(
            ["- 2026-01-01 seed ACs implemented: AC2, AC1.1, AC20. (phase-2)"])
        self.assertEqual(named, {2, 20})

    def test_review_loop_ends_on_a_round_with_no_patch(self):
        rec = ["- 2026-01-01 review round 1 recorded",
               "- 2026-01-01 phase-3 tests GREEN -- a (exit 0)"]
        findings = ("## Review round 1\n- Diff: x; mode controlled\n## Triage decisions — 2026-01-01\n"
                    "### approved_patches\nnone\n### deferred_findings\nnone\n### dismissed_findings\n- R1-1 x\n")
        state = story_ledger.review_loop(rec, findings)
        self.assertEqual(state["ended"], "no patch approved in round 1")
        findings2 = findings.replace("### approved_patches\nnone",
                                     "### approved_patches\nR1-1 | a.py | 3 | fix | - | -")
        state = story_ledger.review_loop(rec, findings2)
        self.assertIsNone(state["ended"])
        self.assertEqual(state["rounds"][0]["files"], ["a.py"])

    def test_close_out_cap_ends_the_loop(self):
        rec, body = [], ""
        for n, kind in ((1, "branch"), (2, "close-out"), (3, "close-out")):
            rec += [f"- 2026-01-0{n} review round {n} recorded; reviewed {kind} {'a' * 40}..{'b' * 40}",
                    f"- 2026-01-0{n} phase-3 tests GREEN -- a (exit 0)"]
            body += (f"## Review round {n}\n- Diff: x; mode auto\n## Triage decisions\n"
                     f"### approved_patches\nR{n}-1 | f{n}.py | 1 | fix | - | -\n### deferred_findings\nnone\n")
        state = story_ledger.review_loop(rec, body)
        self.assertEqual(state["ended"], story_ledger.loop_cap_reason())


class SeedAndHandoffTests(_WorktreeCase):
    def test_seed_acs_due_until_named(self):
        self.spec.write_text("# spec\n### AC1 — Seed: a\n### AC2 — Seed: b\n", encoding="utf-8")
        rec = ["- 2026-01-01 phase-2 tests GREEN -- a (exit 0)"]
        self.assertEqual(story_ledger.seed_acs_due(str(self.wt), "7-2", rec), [1, 2])
        rec.append("- 2026-01-01 seed ACs implemented: AC1, AC2 (phase-2)")
        self.assertEqual(story_ledger.seed_acs_due(str(self.wt), "7-2", rec), [])
        self.assertIsNone(story_ledger.seed_acs_due(str(self.wt), "7-2", []))

    def test_triage_is_per_latest_recorded_round(self):
        findings = self.wt / SPECS / "7-2-code-review-findings-2026-01-01.md"
        findings.write_text("## Review round 1\n## Triage decisions\n### approved_patches\nnone\n"
                            "## Review round 2\n(no triage yet)\n", encoding="utf-8")
        self.rec("append", "7-2", "review round 1 recorded")
        rows = dict((r[0], r[1]) for r in story_ledger.handoff_state(
            ["7-2"], story_ledger.read_record(["7-2"], str(self.wt / SPECS))[1], str(self.wt / SPECS)))
        self.assertTrue(rows["triage recorded"])
        self.rec("append", "7-2", "review round 2 recorded")
        rows = dict((r[0], r[1]) for r in story_ledger.handoff_state(
            ["7-2"], story_ledger.read_record(["7-2"], str(self.wt / SPECS))[1], str(self.wt / SPECS)))
        self.assertFalse(rows["triage recorded"], "round 1's triage satisfied round 2")


class LandGuardGitTests(unittest.TestCase):
    """premerge_verdict against a real main checkout + story worktree."""

    def setUp(self):
        self.root = te.enter(self, '[stack]\nruntime = "none"\n[tests]\ncode_root = "."\n')
        git(self.root, "init", "-q", "-b", "development")
        (self.root / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "notes.md").write_text("n\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".workflow/\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.wt = self.root.parent / f"{self.root.name}-wt"
        git(self.root, "worktree", "add", "-q", "-b", "story/7-2", str(self.wt))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.wt)]))
        (self.wt / "app.py").write_text("x = 2\n", encoding="utf-8")
        git(self.wt, "commit", "-qam", "feat: [Story 7-2] change")
        (self.root / SPECS).mkdir(parents=True)
        (self.wt / SPECS).mkdir(parents=True)
        (self.root / SPECS / "slot-registry.md").write_text(
            "| Slot | Status | Story ID | Branch | Worktree | Since |\n|---|---|---|---|---|---|\n"
            f"| 1 | in_use | 7-2 | story/7-2 | {self.wt} | 2026-01-01 |\n", encoding="utf-8")
        self.arts = str(self.wt / SPECS)

    def green(self, tree: str, base: str) -> list[str]:
        return [f"- 2026-01-01 pre-merge gate GREEN -- run_full_suite all legs (exit 0) HEAD abc code-tree {tree} "
                f"base {base}"]

    def test_green_passes_on_the_tested_tree_and_current_base(self):
        import run_scoped_tests
        tree = run_scoped_tests.tested_tree(str(self.wt))[1]
        base = git(self.root, "rev-parse", "development")[:12]
        ok, why = story_ledger.premerge_verdict(self.green(tree, base), self.arts, "7-2")
        self.assertIsNotNone(ok, why)

    def test_a_later_change_or_a_gate_input_moving_on_the_base_blocks(self):
        import run_scoped_tests
        tree = run_scoped_tests.tested_tree(str(self.wt))[1]
        base = git(self.root, "rev-parse", "development")[:12]
        (self.root / "docs" / "notes.md").write_text("n2\n", encoding="utf-8")
        git(self.root, "commit", "-qam", "docs only")
        self.assertIsNotNone(story_ledger.premerge_verdict(self.green(tree, base), self.arts, "7-2")[0],
                             "a gate-inert base move blocked the land")
        (self.root / "lib.py").write_text("y = 1\n", encoding="utf-8")
        git(self.root, "add", "lib.py")
        git(self.root, "commit", "-qm", "code")
        ok, why = story_ledger.premerge_verdict(self.green(tree, base), self.arts, "7-2")
        self.assertIsNone(ok)
        self.assertIn("lib.py is read by the gate", why)
        tip = git(self.root, "rev-parse", "development")[:12]
        rec = self.green(tree, base) + [f"- 2026-01-02 pre-merge base accepted — development {tip} (owner)"]
        self.assertIsNotNone(story_ledger.premerge_verdict(rec, self.arts, "7-2")[0])
        (self.wt / "app.py").write_text("x = 3\n", encoding="utf-8")
        ok, why = story_ledger.premerge_verdict(self.green(tree, base), self.arts, "7-2")
        self.assertIsNone(ok)
        self.assertIn("differs", why)

    def test_not_required_passes_and_a_train_branch_blocks(self):
        rec = ["- 2026-01-01 pre-merge gate not required — AGENTS.md: docs only"]
        self.assertIsNotNone(story_ledger.premerge_verdict(rec, self.arts, "7-2")[0])
        git(self.wt, "checkout", "-q", "-b", "train/7-2")
        ok, why = story_ledger.premerge_verdict(rec, self.arts, "7-2")
        self.assertIsNone(ok)
        self.assertIn("check out story/7-2", why)


class LandLeaseTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, "")
        self.path = self.root / "lease.json"

    def test_line_is_first_come_first_served_and_release_keeps_the_line(self):
        self.assertEqual(land_lease.take(self.path, "1-1")[0], "acquired")
        self.assertEqual(land_lease.take(self.path, "1.1")[0], "already")
        self.assertEqual(land_lease.take(self.path, "2-2")[0], "held")
        self.assertEqual(land_lease.join(self.path, "2-2")[0], 1)
        self.assertEqual(land_lease.join(self.path, "3-3")[0], 2)
        self.assertEqual(land_lease.release(self.path, "1-1")[0], "released")
        self.assertEqual(land_lease.take(self.path, "3-3")[0], "behind")
        self.assertEqual(land_lease.take(self.path, "2-2")[0], "acquired")
        record = land_lease.read_record(self.path)
        self.assertEqual([e["story"] for e in record["line"]], ["3-3"])
        self.assertEqual(land_lease.release(self.path, "9-9")[0], "held")

    def test_train_state_and_dead_gate_release(self):
        land_lease.take(self.path, "1-1")
        train = {"tip": "a" * 40, "state": "gating", "gating_seen": "2000-01-01T00:00:00Z"}
        self.assertTrue(land_lease.set_train(self.path, "1-1", train))
        self.assertFalse(land_lease.release_dead_gate(self.path, "1-1", "other"))
        self.assertTrue(land_lease.release_dead_gate(self.path, "1-1", "2000-01-01T00:00:00Z"))
        self.assertIsNone(land_lease.read(self.path))
        with self.assertRaises(land_lease.LeaseError):
            land_lease.set_train(self.path, "1-1", {"tip": "short", "state": "green"})

    def test_malformed_record_is_an_error_never_free(self):
        self.path.write_text('{"story": "1-1"}', encoding="utf-8")
        with self.assertRaises(land_lease.LeaseError):
            land_lease.read(self.path)
        self.assertEqual(quiet(lambda: land_lease.cmd_holds("1-1", path=self.path))[0], 2)


class MergeTrainUnitTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, '[env]\ndependency_files = ["uv.lock"]\n[pipeline]\nschema_globs = ["**/migrations/*.py"]\n'
                                   '[stack]\ncompose_file = "compose.yml"\n')

    def test_ahead_of_and_stack_affecting(self):
        record = {"story": "1-1", "acquired": "x", "line": [{"story": "2-2"}, {"story": "3-3"}]}
        self.assertEqual(merge_train.ahead_of("3-3", record), ["1-1", "2-2"])
        self.assertIsNone(merge_train.ahead_of("9-9", record))
        self.assertEqual(merge_train._stack_affecting(["src/a.py", "pkg/migrations/0001.py"]), "pkg/migrations/0001.py")
        self.assertEqual(merge_train._stack_affecting(["uv.lock"]), "uv.lock")
        self.assertEqual(merge_train._stack_affecting(["Dockerfile.base"]), "Dockerfile.base")
        self.assertIsNone(merge_train._stack_affecting(["src/a.py"]))

    def test_delete_train_removes_the_refs(self):
        git(self.root, "init", "-q", "-b", "development")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "base")
        git(self.root, "branch", "train/7-2")
        git(self.root, "update-ref", "refs/train-tested/7-2", "HEAD")
        deleted = merge_train.delete_train(self.root, "story/7-2")
        self.assertEqual(sorted(deleted), ["refs/heads/train/7-2", "refs/train-tested/7-2"])
        self.assertEqual(merge_train.delete_train(self.root, "story/7-2"), [])


class SprintStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, "")
        self.path = self.root / "ss.yaml"
        self.path.write_text("last_updated: 2026-01-01  # n\ndevelopment_status:\n"
                             "  7-1-alpha: done  # MERGED\n  7-2-beta: in-progress  # STARTED slot 1\n"
                             "  epic-7: in-progress\n", encoding="utf-8")

    def ss(self, *argv):
        return quiet(sprint_status.main, ["--path", str(self.path), *argv])

    def test_get_many_brief_and_unknown_key(self):
        code, out = self.ss("get", "7-1", "7-9", "7-2", "--brief")
        self.assertEqual(code, 2)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(lines[0], "7-1-alpha: done | MERGED")
        self.assertTrue(lines[1].startswith("7-9: ERROR"))
        self.assertEqual(lines[2], "7-2-beta: in-progress | STARTED slot 1")

    def test_list_filters_and_empty_note_clears(self):
        code, out = self.ss("list", "--exclude-status", "done", "--exclude-epics", "--match", "slot", "--notes")
        self.assertEqual(code, 0)
        self.assertIn("7-2-beta: in-progress | STARTED slot 1", out)
        self.assertNotIn("7-1-alpha", out)
        self.assertEqual(self.ss("set", "7-2", "review", "--note", "")[0], 0)
        self.assertIn("  7-2-beta: review\n", self.path.read_text(encoding="utf-8"))

    def test_history_append_with_swapped_arguments(self):
        code, out = self.ss("history", "some text", "--append", "7-2")
        self.assertEqual(code, 0, out)
        self.assertIn('"argument_order": "swapped"', out)


class StageOwnHunksTests(unittest.TestCase):
    def test_gate_refuses_a_foreign_staged_file_and_verify_checks_the_commit(self):
        root = te.enter(self, "")
        git(root, "init", "-q", "-b", "development")
        (root / "a.txt").write_text("1\n2\n3\n", encoding="utf-8")
        (root / "b.txt").write_text("x\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "base")
        (root / "a.txt").write_text("1\nTWO\n3\n", encoding="utf-8")
        script = str(Path(__file__).resolve().parent.parent / "stage_own_hunks.py")
        p = subprocess.run([sys.executable, script, "gate", "--hunks", "a.txt", "1", "--repo", str(root)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        tree = p.stdout.strip()
        git(root, "commit", "-q", "-m", "own")
        v = subprocess.run([sys.executable, script, "verify", tree, "--repo", str(root)], capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        (root / "b.txt").write_text("y\n", encoding="utf-8")
        git(root, "add", "b.txt")
        (root / "a.txt").write_text("ONE\nTWO\n3\n", encoding="utf-8")
        p = subprocess.run([sys.executable, script, "gate", "--hunks", "a.txt", "1", "--repo", str(root)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 3, "a non-empty shared index was not refused")


class BranchOverlapAndRefsTests(unittest.TestCase):
    def test_predicted_conflicts_only_from_both_sides(self):
        out = ("changed in both\n  base   100644 " + "a" * 40 + " f.py\n  our    100644 " + "b" * 40 +
               " f.py\n  their  100644 " + "c" * 40 + " f.py\n@@ -1 +1 @@\n+<<<<<<< .our\n"
               "added in remote\n  their  100644 " + "d" * 40 + " g.py\n+<<<<<<< literal\n")
        self.assertEqual(branch_overlap.predicted_conflicts(out), {"f.py"})

    def test_story_ref_boundaries(self):
        s = story_refs.Story("7-5", "7-5-some-slug")
        self.assertTrue(s.pattern.search("see 7-5 and 7.5"))
        self.assertTrue(s.pattern.search("7-5-some-slug"))
        self.assertFalse(s.pattern.search("7-5-6"))
        self.assertFalse(s.pattern.search("2026-07-5"))
        self.assertFalse(s.pattern.search("file.py:7-5"))
        self.assertTrue(s.owns(Path("story-7-5.md")))
        self.assertFalse(s.owns(Path("7-5-6-other.md")))


class EffortVariantTests(unittest.TestCase):
    def test_kit_variants_are_current(self):
        agents = Path(__file__).resolve().parents[2] / "agents"
        if not (agents / "story-impl.md").is_file():
            self.skipTest("not running from the kit source tree")
        self.assertEqual(gen.drift(agents), [], "run scripts/gen_agent_effort_variants.py")

    def test_render_changes_three_lines_only(self):
        base = "---\nname: story-impl\ndescription: Phase 2.\neffort: high\ntools: [read]\n---\nbody\n"
        out = gen.render(base, "max")
        self.assertIn("name: story-impl-max\n", out)
        self.assertIn("effort: max\n", out)
        self.assertIn("description: Escalated story-impl (effort max)", out)
        self.assertTrue(out.endswith("\n---\nbody\n"))


if __name__ == "__main__":
    unittest.main()
