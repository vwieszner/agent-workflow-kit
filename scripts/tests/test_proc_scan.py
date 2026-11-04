"""proc_scan.py: process selection by command line, and the --pid --tree descendant listing.

Pins: `select` keeps only rows matching every regex; `watch_roots` returns one row per compose
watch of the named project — the `docker.exe` → `docker-compose.exe` wrapper pair counts once,
two independent watches count twice, another project's watch and a non-watch command never match.
`tree_lines` nests each descendant under its parent in creation order and hangs an orphan under the
root. `--pid N --tree` over a fake psutil: descendants that cannot be listed exit 2 with no count; a
descendant that exited before its row was read is dropped from the rows and the count; a pid that is not
running exits 1; `--tree` beside another mode, or without `--pid`, is a usage error.
"""
from __future__ import annotations

import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import proc_scan  # noqa: E402

WRAPPER = {"pid": 10, "ppid": 1, "cmd": "docker compose -p story-16-16 watch"}
PLUGIN = {"pid": 11, "ppid": 10,
          "cmd": r"C:\Program Files\Docker\cli-plugins\docker-compose.exe compose -p story-16-16 watch"}
SECOND = {"pid": 20, "ppid": 2, "cmd": "docker compose -p story-16-16 watch"}
OTHER_PROJECT = {"pid": 30, "ppid": 3, "cmd": "docker compose -p story-16-161 watch"}
NOT_WATCH = {"pid": 40, "ppid": 4, "cmd": "docker compose -p story-16-16 ps --watchdog"}

T0 = 1_760_600_000.0
ROOT = {"pid": 100, "ppid": 1, "name": "python.exe", "create_time": T0 + 1, "cmd": "uv run run_scoped_tests.py"}
LEG = {"pid": 101, "ppid": 100, "name": "python.exe", "create_time": T0 + 2, "cmd": "python -m unittest scripts.tests"}
CONHOST = {"pid": 102, "ppid": 100, "name": "conhost.exe", "create_time": T0 + 1.5, "cmd": "conhost.exe 0x4"}
GRANDCHILD = {"pid": 103, "ppid": 101, "name": "git.exe", "create_time": T0 + 3, "cmd": "git write-tree"}
ORPHAN = {"pid": 104, "ppid": 999, "name": "bash.exe", "create_time": T0 + 4, "cmd": "bash -c sleep"}


class ProcScan(unittest.TestCase):
    def test_select_requires_every_pattern(self):
        rows = [WRAPPER, NOT_WATCH, OTHER_PROJECT]
        self.assertEqual(proc_scan.select(rows, ["story-16-16", "watch"]), [WRAPPER, NOT_WATCH, OTHER_PROJECT])
        self.assertEqual(proc_scan.select(rows, ["story-16-16", r"\bps\b"]), [NOT_WATCH])

    def test_wrapper_pair_is_one_watch(self):
        rows = [WRAPPER, PLUGIN, OTHER_PROJECT, NOT_WATCH]
        self.assertEqual(proc_scan.watch_roots(rows, "story-16-16"), [WRAPPER])

    def test_independent_watches_are_duplicates(self):
        rows = [WRAPPER, PLUGIN, SECOND]
        self.assertEqual(proc_scan.watch_roots(rows, "story-16-16"), [WRAPPER, SECOND])

    def test_unknown_project_has_none(self):
        self.assertEqual(proc_scan.watch_roots([WRAPPER, PLUGIN], "story-nope"), [])

    def test_tree_nests_by_parent_in_creation_order(self):
        lines = proc_scan.tree_lines(ROOT, [GRANDCHILD, LEG, CONHOST])
        self.assertEqual(lines, [proc_scan._fmt(ROOT), "  " + proc_scan._fmt(CONHOST),
                                 "  " + proc_scan._fmt(LEG), "    " + proc_scan._fmt(GRANDCHILD)])

    def test_orphan_hangs_under_the_root(self):
        lines = proc_scan.tree_lines(ROOT, [ORPHAN])
        self.assertEqual(lines, [proc_scan._fmt(ROOT), "  " + proc_scan._fmt(ORPHAN)])

    def test_root_alone_has_one_line(self):
        self.assertEqual(proc_scan.tree_lines(ROOT, []), [proc_scan._fmt(ROOT)])


class PsError(Exception):
    """psutil.Error."""


class PsNoSuchProcess(PsError):
    """psutil.NoSuchProcess: the process has exited."""


class PsAccessDenied(PsError):
    """psutil.AccessDenied: the process is alive and the fact cannot be read."""


class FakeProc:
    """The slice of `psutil.Process` that `tree()` reads. `gone` makes every fact raise NoSuchProcess (it exited
    after `children()` listed it); `children_denied` makes `children()` raise AccessDenied; `cmdline_denied` makes
    `cmdline()` raise AccessDenied."""

    def __init__(self, row, parent=None, gone=False, children_denied=False, cmdline_denied=False):
        self.pid, self.row, self.kids = row["pid"], row, []
        self.gone, self.children_denied, self.cmdline_denied = gone, children_denied, cmdline_denied
        if parent is not None:
            parent.kids.append(self)

    def _fact(self, value):
        if self.gone:
            raise PsNoSuchProcess(self.pid)
        return value

    def oneshot(self):
        return contextlib.nullcontext()

    def ppid(self):
        return self._fact(self.row["ppid"])

    def name(self):
        return self._fact(self.row["name"])

    def create_time(self):
        return self._fact(self.row["create_time"])

    def cmdline(self):
        if self.cmdline_denied:
            raise PsAccessDenied(self.pid)
        return self._fact(self.row["cmd"].split())

    def children(self, recursive=False):
        if self.children_denied:
            raise PsAccessDenied(self.pid)
        found, queue = [], list(self.kids)
        while queue:
            kid = queue.pop(0)
            found.append(kid)
            if recursive:
                queue.extend(kid.kids)
        return found


def fake_psutil(*procs):
    by_pid = {p.pid: p for p in procs}

    def process(pid):
        if pid not in by_pid:
            raise PsNoSuchProcess(pid)
        return by_pid[pid]

    return types.SimpleNamespace(Error=PsError, NoSuchProcess=PsNoSuchProcess, AccessDenied=PsAccessDenied,
                                 Process=process)


class TreeTests(unittest.TestCase):
    """`--pid N --tree` through `main()` over a contract-faithful slice of psutil (the host venv has none)."""

    def run_main(self, *argv, procs=()):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"psutil": fake_psutil(*procs)}), \
                mock.patch.object(sys, "argv", ["proc_scan.py", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = proc_scan.main()
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_a_live_tree_lists_each_descendant_and_counts_them(self):
        root = FakeProc(ROOT)
        leg = FakeProc(LEG, parent=root)
        FakeProc(GRANDCHILD, parent=leg)
        FakeProc(CONHOST, parent=root)
        rc, out, _ = self.run_main("--pid", "100", "--tree", procs=[root])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines(), [proc_scan._fmt(ROOT), "  " + proc_scan._fmt(CONHOST),
                                            "  " + proc_scan._fmt(LEG), "    " + proc_scan._fmt(GRANDCHILD),
                                            "3 live descendant(s) of 100"])

    def test_descendants_that_cannot_be_listed_exit_2_with_no_count(self):
        root = FakeProc(ROOT, children_denied=True)
        rc, out, _ = self.run_main("--pid", "100", "--tree", procs=[root])
        self.assertEqual(rc, 2, out)
        self.assertIn("descendants cannot be listed", out)
        self.assertNotIn("live descendant(s)", out)
        with mock.patch.dict(sys.modules, {"psutil": fake_psutil(root)}):
            with self.assertRaises(proc_scan.TreeUnlisted):
                proc_scan.tree(100)

    def test_a_descendant_that_exited_before_its_row_was_read_is_dropped_from_rows_and_count(self):
        root = FakeProc(ROOT)
        FakeProc(LEG, parent=root)
        FakeProc(CONHOST, parent=root, gone=True)
        rc, out, _ = self.run_main("--pid", "100", "--tree", procs=[root])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines(), [proc_scan._fmt(ROOT), "  " + proc_scan._fmt(LEG),
                                            "1 live descendant(s) of 100"])
        self.assertNotIn("<no access>", out)

    def test_a_descendant_whose_command_line_is_denied_stays_listed(self):
        root = FakeProc(ROOT)
        FakeProc(LEG, parent=root, cmdline_denied=True)
        rc, out, _ = self.run_main("--pid", "100", "--tree", procs=[root])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[1], "  " + proc_scan._fmt({**LEG, "cmd": "<no access>"}))
        self.assertEqual(out.splitlines()[-1], "1 live descendant(s) of 100")

    def test_a_pid_that_is_not_running_exits_1(self):
        for label, procs in (("never listed", []), ("exited after lookup", [FakeProc(ROOT, gone=True)])):
            with self.subTest(label):
                rc, out, _ = self.run_main("--pid", "100", "--tree", procs=procs)
                self.assertEqual(rc, 1, out)
                self.assertEqual(out.splitlines(), ["    100 <not running>"])

    def test_tree_beside_another_mode_or_without_pid_is_a_usage_error(self):
        for argv in (("--pid", "100", "--tree", "--chain"), ("--pid", "100", "--tree", "--match", "x"),
                     ("--tree", "--match", "x"), ("--tree",)):
            with self.subTest(argv=argv):
                rc, out, err = self.run_main(*argv, procs=[FakeProc(ROOT)])
                self.assertEqual(rc, 2, out + err)
                self.assertIn("--tree", err)
                self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
