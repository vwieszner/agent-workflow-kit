"""profile_leg.py: the merge counter behind `due` (first-parent merges on the base branch after the
last green run's base), the state file (append-only, unreadable lines skipped), the exit codes
(0 not due, 1 due, 2 cannot tell, 3 not configured)."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import profile_leg as pl  # noqa: E402

CONFIGURED = ('[git]\nbase_branch = "main"\n[profile_leg]\nprofile = "real"\n'
              'cmd = "run {specs}"\nspecs = ["a", "b"]\nevery = 3\n')


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.root = te.enter(self, CONFIGURED)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.invalid")
        git(self.root, "config", "user.name", "t")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "root")
        self.base = git(self.root, "rev-parse", "HEAD")

    def merge(self, name: str, inner_merge: bool = False) -> None:
        git(self.root, "checkout", "-q", "-b", name)
        git(self.root, "commit", "-q", "--allow-empty", "-m", f"{name} work")
        if inner_merge:
            git(self.root, "checkout", "-q", "-b", f"{name}-sub")
            git(self.root, "commit", "-q", "--allow-empty", "-m", "sub")
            git(self.root, "checkout", "-q", name)
            git(self.root, "merge", "-q", "--no-ff", "-m", "inner", f"{name}-sub")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--no-ff", "-m", f"merge {name}", name)

    def due(self, every=3):
        return pl.due(pl.state_path(), self.root, every, "main")


class CounterTests(RepoCase):
    def test_no_record_means_due(self):
        is_due, line = self.due()
        self.assertTrue(is_due)
        self.assertIn("no green profile-leg run recorded", line)

    def test_only_first_parent_merges_after_the_base_count(self):
        self.merge("a", inner_merge=True)   # the inner merge is not a merge on main
        self.assertEqual(pl.merges_since(self.base, self.root, "main"), 1)

    def test_below_the_threshold_is_not_due_and_at_it_is(self):
        pl.record_green(pl.state_path(), story="1-1", head="h", tree="t", base=self.base)
        self.merge("a")
        self.merge("b")
        self.assertFalse(self.due()[0])
        self.merge("c")
        is_due, line = self.due()
        self.assertTrue(is_due)
        self.assertIn("3 merges to main", line)

    def test_a_later_green_run_resets_the_count(self):
        pl.record_green(pl.state_path(), story="1-1", head="h", tree="t", base=self.base)
        for n in "abc":
            self.merge(n)
        pl.record_green(pl.state_path(), story="1-2", head="h", tree="t", base=git(self.root, "rev-parse", "HEAD"))
        self.assertFalse(self.due()[0])

    def test_unreadable_lines_are_skipped(self):
        path = pl.state_path()
        pl.record_green(path, story="1-1", head="h", tree="t", base=self.base)
        with path.open("a") as fh:
            fh.write("{not json\n" + json.dumps({"story": "x"}) + "\n")
        self.assertEqual(pl.last_green(path)["story"], "1-1")

    def test_an_unknown_base_is_an_error_not_a_due_run(self):
        pl.record_green(pl.state_path(), story="1-1", head="h", tree="t", base="f" * 40)
        with self.assertRaises(pl.ProfileLegError):
            self.due()

    def test_record_green_appends_one_line_per_run(self):
        path = pl.state_path()
        pl.record_green(path, story="1-1", head="h1", tree="t1", base="b1")
        pl.record_green(path, story="1-2", head="h2", tree="t2", base="b2")
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        self.assertEqual([x["story"] for x in lines], ["1-1", "1-2"])
        self.assertEqual(set(lines[0]), {"date", "story", "head", "tree", "base"})


class CliTests(RepoCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = pl.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_exit_codes_due_not_due_error(self):
        rc, out, _ = self.run_main("due")
        self.assertEqual(rc, pl.EXIT_DUE)
        self.assertIn("--profile-leg --record", out)
        pl.record_green(pl.state_path(), story="1-1", head="h", tree="t", base=self.base)
        self.assertEqual(self.run_main("due")[0], pl.EXIT_NOT_DUE)
        self.merge("a")
        self.assertEqual(self.run_main("due", "--every", "1")[0], pl.EXIT_DUE)
        pl.record_green(pl.state_path(), story="1-2", head="h", tree="t", base="e" * 40)
        rc, _, err = self.run_main("due")
        self.assertEqual(rc, pl.EXIT_ERROR)
        self.assertIn("cannot tell", err)

    def test_unconfigured_is_skipped_never_due(self):
        (self.root / ".workflow" / "config.toml").write_text('[git]\nbase_branch = "main"\n')
        te.clear_caches()
        rc, out, _ = self.run_main("due")
        self.assertEqual(rc, pl.EXIT_NOT_CONFIGURED)
        self.assertIn("SKIPPED", out)

    def test_command_fills_specs(self):
        self.assertEqual(pl.command({"stack": "s"}), "run a b")


if __name__ == "__main__":
    unittest.main()
