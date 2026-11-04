"""kill_owned.py: a process is killed only by the caller that owns it, and a refusal names the reason.

Pure tests (host venv, fake process rows, no psutil) pin the ownership rules, the component-wise path
containment, the watch selection, the argument checks, the refusal of a call without the hook's single-use token
(also when the helper is launched as a copy, as a module or from stdin), the output lines and the `--tree` console
host that is awaited (`FOLLOWS`), never killed. The live tests run the
real helper through `uv run` (its psutil dependency) from a runner that points the token directory at a temporary
one, each call carrying a token issued there, against throwaway fake worktrees and a real orphan process.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv  # noqa: E402,F401
sys.path.insert(0, str(SCRIPTS))

import kill_owned as ko  # noqa: E402
import kill_token  # noqa: E402

KILL = SCRIPTS / "kill_owned.py"
ROOT = "/work/proj-worktrees"
MAIN = "/work/proj"
WT = f"{ROOT}/cr-7"
T0 = 1_760_600_000.0
SINCE = datetime.fromtimestamp(T0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
GATE_CMD = "uv run --no-project scripts/run_full_suite.py --story-id 8-934a --record"
DISCOVER = "python.exe -m unittest discover -s scripts/tests -p test_*.py -t ."
WATCH_CMD = r"C:\Program Files\Docker\docker.exe compose -p story-cr-7 watch"


def row(pid, cwd, created=T0 + 10, cmdline="python.exe worker.py", name="python.exe", ppid=999_999):
    return {"pid": pid, "ppid": ppid, "name": name, "cmdline": cmdline, "cwd": cwd, "create_time": created}


def owner(**kwargs):
    base = dict(worktree=WT, since=T0, since_text=SINCE, stack="story-cr-7", worktree_root=ROOT, main_root=MAIN)
    return ko.Owner(**{**base, **kwargs})


def reasons(r, own=None, ancestors=(), me=frozenset()):
    return ko.refusals(r, own or owner(), list(ancestors), me)


class ContainmentTests(unittest.TestCase):
    def test_a_cwd_equal_to_or_under_the_worktree_is_inside(self):
        self.assertTrue(ko.inside(WT, WT))
        self.assertTrue(ko.inside(f"{WT}/src", WT))
        if os.name == "nt":
            self.assertTrue(ko.inside(WT.replace("/", "\\") + "\\src\\frontend", WT))

    def test_a_longer_sibling_name_is_not_inside(self):
        self.assertFalse(ko.inside(f"{ROOT}/ab-59/src", f"{ROOT}/ab-5"))
        self.assertFalse(ko.inside(f"{ROOT}/8-934a", f"{ROOT}/8-934"))

    def test_the_worktree_root_is_not_inside_the_main_checkout(self):
        self.assertFalse(ko.inside(f"{ROOT}/x", MAIN))
        self.assertTrue(ko.inside(f"{MAIN}/scripts", MAIN))

    def test_a_cwd_in_another_worktree_is_named_by_that_worktree_and_not_as_the_main_checkout(self):
        self.assertEqual(ko.where(f"{ROOT}/x/src", owner()), "worktree `x`")
        self.assertEqual(ko.where(f"{MAIN}/scripts", owner()), "the main checkout")
        self.assertEqual(ko.where("E:/elsewhere", owner()), "E:/elsewhere")


class OwnershipRuleTests(unittest.TestCase):
    def test_the_callers_own_orphan_is_owned(self):
        self.assertEqual(reasons(row(100, f"{WT}/src")), [])

    def test_another_worktree_is_refused_and_named(self):
        text = "; ".join(reasons(row(100, f"{ROOT}/ab-59/src")))
        self.assertIn("worktree `ab-59`", text)

    def test_the_main_checkout_is_refused_and_named(self):
        text = "; ".join(reasons(row(100, f"{MAIN}")))
        self.assertIn("the main checkout", text)

    def test_a_process_created_before_since_is_refused_naming_both_times(self):
        text = "; ".join(reasons(row(100, WT, created=T0 - 3600)))
        self.assertIn("--since", text)
        stamps = [p for p in text.replace(",", " ").split() if p.count(":") == 2]
        self.assertGreaterEqual(len(stamps), 2, text)

    def test_a_process_under_another_stacks_gate_is_refused_even_inside_the_callers_worktree(self):
        gate = row(35344, MAIN, cmdline=GATE_CMD, ppid=1)
        text = "; ".join(reasons(row(100, WT, cmdline=DISCOVER, ppid=35344), ancestors=[gate]))
        self.assertIn("story-8-934a", text)
        self.assertIn("run_full_suite.py", text)
        self.assertIn("35344", text)

    def test_a_gate_without_a_story_id_is_slot_0s(self):
        gate = row(40, MAIN, cmdline="uv run scripts/run_full_suite.py", ppid=1)
        self.assertIn("project", "; ".join(reasons(row(100, WT, ppid=40), ancestors=[gate])))

    def test_an_ancestor_gate_of_the_callers_own_stack_still_refuses_when_no_registry_row_matches(self):
        gate = row(40, MAIN, cmdline="run_full_suite.py --story-id cr-7", ppid=1)
        self.assertEqual(reasons(row(100, WT, ppid=40), owner(stack="story-cr-7"), [gate]), [])
        text = "; ".join(reasons(row(100, WT, ppid=40), owner(stack=None), [gate]))
        self.assertIn("run_full_suite.py", text)

    def test_an_ancestor_compose_watch_with_an_unknown_stack_refuses(self):
        watch = row(41, WT, cmdline=r"C:\docker.exe compose watch", name="docker.exe", ppid=1)
        self.assertIn("compose watch", "; ".join(reasons(row(100, WT, ppid=41), ancestors=[watch])))

    def test_an_ancestor_that_is_neither_a_gate_nor_a_watch_does_not_refuse(self):
        shell = row(42, WT, cmdline="bash.exe", name="bash.exe", ppid=1)
        self.assertEqual(reasons(row(100, WT, ppid=42), ancestors=[shell]), [])

    def test_an_unreadable_ancestor_alone_refuses_with_that_reason(self):
        hidden = {"pid": 50, "unreadable": True}
        self.assertEqual(reasons(row(100, WT, ppid=50), ancestors=[hidden]), ["ancestor 50 unreadable"])

    def test_an_unreadable_ancestor_refuses_only_when_its_name_could_host_a_gate(self):
        for name, refused in (("python.exe", True), ("pythonw.exe", True), ("py.exe", True), ("uv.exe", True),
                              ("docker.exe", True), ("docker-compose.exe", True), (None, True),
                              ("svchost.exe", False), ("services.exe", False), ("wininit.exe", False),
                              ("WmiPrvSE.exe", False), ("bash.exe", False)):
            with self.subTest(name=name):
                hidden = {"pid": 50, "unreadable": True, "name": name}
                expected = ["ancestor 50 unreadable"] if refused else []
                self.assertEqual(reasons(row(100, WT, ppid=50), ancestors=[hidden]), expected)

    def test_an_ancestor_whose_pid_is_unknown_is_named_by_its_child(self):
        hidden = {"pid": None, "of": 100, "unreadable": True, "name": None}
        self.assertEqual(reasons(row(100, WT), ancestors=[hidden]), ["parent of 100 unreadable"])

    def test_an_unreadable_ancestor_below_a_foreign_gate_still_refuses_for_the_gate(self):
        hidden = {"pid": 50, "unreadable": True}
        gate = row(35344, MAIN, cmdline=GATE_CMD, ppid=1)
        text = "; ".join(reasons(row(100, WT, ppid=50), ancestors=[hidden, gate]))
        self.assertIn("ancestor 50 unreadable", text)
        self.assertIn("under story-8-934a's run_full_suite.py pid 35344", text)

    def test_a_compose_watch_is_refused_in_pid_mode_pointing_at_watch_mode(self):
        text = "; ".join(reasons(row(100, f"{WT}/src", cmdline=WATCH_CMD, name="docker.exe")))
        self.assertIn("a compose watch", text)
        self.assertIn("--watch", text)

    def test_an_unreadable_cwd_is_refused(self):
        self.assertIn("cwd unreadable: ownership unproven", "; ".join(reasons(row(100, None))))

    def test_the_helpers_own_tree_is_refused(self):
        self.assertIn("own", "; ".join(reasons(row(7, WT), me=frozenset({7}))))

    def test_every_failing_rule_is_named_joined_by_semicolons(self):
        found = reasons(row(100, f"{ROOT}/ab-59", created=T0 - 5, cmdline=WATCH_CMD, name="docker.exe"))
        self.assertGreaterEqual(len(found), 3)

    def test_prefix_traps(self):
        self.assertNotEqual(reasons(row(100, f"{ROOT}/ab-59/src"), owner(worktree=f"{ROOT}/ab-5")), [])
        self.assertEqual(reasons(row(100, f"{ROOT}/ab-5/src"), owner(worktree=f"{ROOT}/ab-5")), [])


class CrossStackReplayTests(unittest.TestCase):
    """A caller naming another stack's gate and its test children: nothing may be killed."""

    def test_stopping_another_stacks_gate_is_refused_whole(self):
        gate = row(35344, MAIN, cmdline=GATE_CMD, name="python.exe", ppid=1, created=T0 - 300)
        pair_a = row(33652, f"{ROOT}/8-934a", cmdline=DISCOVER, ppid=35344, created=T0 - 290)
        pair_b = row(33932, f"{ROOT}/8-934a", cmdline=DISCOVER, ppid=33652, created=T0 - 290)
        ancestors = {33652: [gate], 33932: [pair_a, gate], 35344: []}
        verdicts = ko.decide([pair_a, pair_b, gate], owner(since=T0 - 600), ancestors, frozenset())
        self.assertTrue(all(not v.owned for v in verdicts), [v.reasons for v in verdicts])
        text = {v.row["pid"]: "; ".join(v.reasons) for v in verdicts}
        self.assertIn("worktree `8-934a`", text[33652])
        self.assertIn("worktree `8-934a`", text[33932])
        self.assertIn("run_full_suite.py", text[33652])
        self.assertIn("the main checkout", text[35344])
        self.assertEqual(ko.exit_code(verdicts, alive=[]), 1)


class WatchSelectionTests(unittest.TestCase):
    PARENT = row(10, f"{WT}/src", cmdline=WATCH_CMD, name="docker.exe", ppid=1)
    CHILD = row(11, f"{WT}/src", cmdline=r"C:\docker-compose.exe compose -p story-cr-7 watch",
                name="docker-compose.exe", ppid=10)

    def test_the_wrapper_pair_in_the_worktree_is_selected_and_nothing_else(self):
        listed_parent = row(1, MAIN, cmdline="cmd.exe", name="cmd.exe", ppid=0)
        slot_zero = row(20, f"{MAIN}/src", cmdline=r"docker.exe compose up --build --watch", name="docker.exe")
        flag_form_in_wt = row(21, f"{WT}/src", cmdline=r"docker.exe compose up --build --watch",
                              name="docker.exe")
        other = row(22, f"{ROOT}/16-916/src", cmdline=WATCH_CMD, name="docker.exe")
        not_docker = row(23, f"{WT}/src", cmdline="python.exe watch compose", name="python.exe")
        rows = [listed_parent, slot_zero, flag_form_in_wt, other, not_docker, self.PARENT, self.CHILD]
        self.assertEqual([r["pid"] for r in ko.select_watch(rows, WT)], [10, 11])

    def test_an_older_watch_form_with_no_project_is_selected_by_cwd_alone(self):
        old = row(30, f"{WT}/src", cmdline=r"C:\docker-compose.exe compose watch", name="docker-compose.exe")
        self.assertEqual([r["pid"] for r in ko.select_watch([old], WT)], [30])

    def test_the_no_up_form_is_selected(self):
        form = row(31, WT, cmdline=r"docker.exe compose watch --no-up -p story-cr-7", name="docker.exe")
        self.assertEqual([r["pid"] for r in ko.select_watch([form], WT)], [31])

    def test_cleanup_of_16_1_never_reaches_16_16(self):
        other = row(22, f"{ROOT}/16-916/src", cmdline=WATCH_CMD, name="docker.exe")
        mine = row(23, f"{ROOT}/16-91/src", cmdline=WATCH_CMD, name="docker.exe")
        self.assertEqual([r["pid"] for r in ko.select_watch([other, mine], f"{ROOT}/16-91")], [23])

    def test_a_watch_with_an_unreadable_cwd_is_not_selected(self):
        self.assertEqual(ko.select_watch([row(24, None, cmdline=WATCH_CMD, name="docker.exe")], WT), [])


class ArgumentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kill-owned-args-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "worktrees"
        (self.root / "ok").mkdir(parents=True)
        (self.root / "ok" / ".git").write_text("gitdir: x\n", encoding="utf-8")
        (self.root / "plain").mkdir()
        (self.root / ".review-runs").mkdir()

    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = ko.main(list(argv))
        return rc, err.getvalue()


class ArgumentTests(ArgumentCase):
    def test_a_real_story_worktree_is_accepted(self):
        self.assertIsNone(ko.worktree_error(str(self.root / "ok"), str(self.root)))

    def test_the_main_checkout_the_root_and_review_runs_and_a_plain_dir_are_rejected(self):
        for name, path in (("main", MAIN), ("root", str(self.root)), ("review-runs", str(self.root / ".review-runs")),
                           ("plain", str(self.root / "plain")), ("absent", str(self.root / "nope")),
                           ("nested", str(self.root / "ok" / "src"))):
            with self.subTest(name=name):
                self.assertIsNotNone(ko.worktree_error(path, str(self.root)))

    def test_exit_2_for_each_bad_argument_before_any_process_is_read(self):
        wt = str(self.root / "ok")
        for argv in (
            ("--worktree", wt, "--worktree-root", str(self.root), "--pid", "1"),                       # no --since
            ("--worktree", wt, "--worktree-root", str(self.root), "--pid", "1", "--since", "yesterday"),
            ("--worktree", wt, "--worktree-root", str(self.root), "--watch", "--since", "yesterday"),
            ("--worktree", MAIN, "--pid", "1", "--since", SINCE),
            ("--worktree", str(self.root), "--worktree-root", str(self.root), "--pid", "1", "--since", SINCE),
            ("--worktree", str(self.root / ".review-runs"), "--worktree-root", str(self.root), "--pid", "1",
             "--since", SINCE),
            ("--pid", "1", "--since", SINCE),                                                            # no --worktree
            ("--worktree", wt, "--worktree-root", str(self.root), "--since", SINCE),                    # no mode
        ):
            with self.subTest(argv=argv):
                rc, _ = self.main(*argv)
                self.assertEqual(rc, 2)

    def test_watch_mode_needs_no_since(self):
        args = ko.parse_args(["--worktree", str(self.root / "ok"), "--worktree-root", str(self.root), "--watch"])
        self.assertIsNone(ko.validate(args))

    def test_an_abbreviated_flag_is_not_accepted(self):
        rc, _ = self.main("--worktree", str(self.root / "ok"), "--worktree-root", str(self.root), "--pid", "1",
                          "--sin", SINCE)
        self.assertEqual(rc, 2)

    def test_since_parses_a_z_suffix_and_an_offset(self):
        self.assertEqual(ko.parse_since("2026-10-16T08:00:00.000Z"), ko.parse_since("2026-10-16T10:00:00+02:00"))
        with self.assertRaises(ValueError):
            ko.parse_since("not a time")


class TokenTests(ArgumentCase):
    """A call is refused before any process is read unless it carries the hook's single-use token for its own
    `--worktree` and `--since`."""

    def setUp(self):
        super().setUp()
        self.tokens = self.tmp / "tokens"
        self.wt = str(self.root / "ok")
        patched = mock.patch.object(kill_token, "token_dir", return_value=self.tokens)
        patched.start()
        self.addCleanup(patched.stop)

    def argv(self, *extra, since=SINCE, watch=False):
        mode = ("--watch",) if watch else ("--pid", "1")
        base = ("--worktree", self.wt, "--worktree-root", str(self.root), *mode)
        return (*base, *(("--since", since) if since else ()), *extra)

    @contextlib.contextmanager
    def no_process_reads(self):
        refuse = AssertionError("a process was read before the token was checked")
        with contextlib.ExitStack() as stack:
            for name in ("_build_owner", "_own_pids", "scan_pids", "scan_watch"):
                stack.enter_context(mock.patch.object(ko, name, side_effect=refuse))
            yield

    def test_a_call_without_a_valid_token_exits_2_before_any_process_is_read(self):
        expired = kill_token.issue(self.tokens, self.wt, SINCE, now=time.time() - 600)
        other_since = kill_token.issue(self.tokens, self.wt, "2026-01-01T00:00:00Z")
        other_worktree = kill_token.issue(self.tokens, str(self.root / "plain"), SINCE)
        with self.no_process_reads():
            for label, extra, reason in (
                ("no token", (), "missing or malformed"),
                ("malformed", ("--hook-token", "../x"), "missing or malformed"),
                ("unknown", ("--hook-token", "0" * 32), "does not exist or was already used"),
                ("expired", ("--hook-token", expired), "expired"),
                ("other since", ("--hook-token", other_since), "another --worktree or --since"),
                ("other worktree", ("--hook-token", other_worktree), "another --worktree or --since"),
            ):
                with self.subTest(label=label):
                    rc, err = self.main(*self.argv(*extra))
                    self.assertEqual(rc, 2)
                    self.assertIn("no hook token: call it through the sanctioned form", err)
                    self.assertIn(reason, err)

    def test_a_watch_call_needs_a_token_too(self):
        with self.no_process_reads():
            rc, err = self.main(*self.argv(since=None, watch=True))
        self.assertEqual(rc, 2)
        self.assertIn("no hook token", err)

    def test_a_valid_token_lets_the_call_through_once_and_a_second_use_is_refused(self):
        nonce = kill_token.issue(self.tokens, self.wt, SINCE)
        with mock.patch.object(ko, "_own_pids", return_value=frozenset()), \
                mock.patch.object(ko, "scan_pids", return_value=([], [])), \
                mock.patch.object(ko, "stop", return_value=([], [])):
            self.assertEqual(self.main(*self.argv("--hook-token", nonce)), (0, ""))
            rc, err = self.main(*self.argv("--hook-token", nonce))
        self.assertEqual(rc, 2)
        self.assertIn("already used", err)

    def test_a_watch_token_carries_no_since(self):
        nonce = kill_token.issue(self.tokens, self.wt, None)
        with mock.patch.object(ko, "_own_pids", return_value=frozenset()), \
                mock.patch.object(ko, "scan_watch", return_value=[]), \
                mock.patch.object(ko, "stop", return_value=([], [])):
            self.assertEqual(self.main(*self.argv("--hook-token", nonce, since=None, watch=True)), (0, ""))

    def test_the_arguments_are_validated_before_the_token_is_redeemed(self):
        nonce = kill_token.issue(self.tokens, self.wt, SINCE)
        rc, err = self.main(*self.argv("--hook-token", nonce, since="yesterday"))
        self.assertEqual(rc, 2)
        self.assertNotIn("hook token", err)
        self.assertTrue((self.tokens / f"{nonce}.json").exists())

    def test_the_token_is_read_from_the_directory_the_hook_writes(self):
        nonce = kill_token.issue(self.tokens, self.wt, SINCE)
        args = ko.parse_args(["--worktree", self.wt, "--since", SINCE, "--pid", "1", "--hook-token", nonce])
        self.assertIsNone(ko.token_problem(args))
        self.assertIn("already used", ko.token_problem(args))

    def test_no_option_redirects_where_the_token_is_read_from(self):
        nonce = kill_token.issue(self.tokens, self.wt, SINCE)
        with self.no_process_reads():
            rc, err = self.main(*self.argv("--hook-token", nonce, "--token-dir", str(self.tokens)))
        self.assertEqual(rc, 2)
        self.assertIn("unrecognized arguments: --token-dir", err)
        self.assertTrue((self.tokens / f"{nonce}.json").exists())

    def test_an_unlocatable_token_directory_refuses(self):
        args = ko.parse_args(["--worktree", self.wt, "--since", SINCE, "--pid", "1", "--hook-token", "0" * 32])
        with mock.patch.object(kill_token, "token_dir", side_effect=OSError("no git")):
            self.assertIn("cannot be located", ko.token_problem(args))


class UnreadRouteTests(ArgumentCase):
    """A launch route the shell hook does not read (a copy of the script, the module, the script run from stdin) still
    ends at `main()`, which refuses a call without a hook token before it reads a process."""

    def route(self, argv, stdin=None, cwd=None):
        return subprocess.run([sys.executable, *argv], input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, cwd=cwd, env={**os.environ, "PYTHONPATH": str(SCRIPTS)})

    def test_each_route_is_refused_without_a_token_before_any_process_is_read(self):
        copy = self.tmp / "k.py"
        shutil.copyfile(KILL, copy)
        args = ["--worktree", str(self.root / "ok"), "--worktree-root", str(self.root), "--since", SINCE, "--pid", "1"]
        from_stdin = ("import runpy, sys\n"
                      "sys.argv = ['kill_owned.py', *sys.argv[1:]]\n"
                      f"runpy.run_path({str(KILL)!r}, run_name='__main__')\n")
        for label, (argv, stdin, cwd) in {
            "a copy of the script": ([str(copy), *args], None, None),
            "the module": (["-m", "kill_owned", *args], None, str(SCRIPTS)),
            "the script run from stdin": (["-", *args], from_stdin, None),
        }.items():
            with self.subTest(route=label):
                r = self.route(argv, stdin, cwd)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("no hook token: call it through the sanctioned form", r.stderr)
                self.assertEqual(r.stdout, "")


class CallerStackTests(unittest.TestCase):
    ROWS = [(f"{ROOT}/ab-59", "ab-59"), (f"{ROOT}/ab-5", "ab-5")]

    def test_the_stack_is_story_id_of_the_row_whose_worktree_is_the_callers(self):
        self.assertEqual(ko.caller_stack(f"{ROOT}/ab-59", self.ROWS), "story-ab-59")
        self.assertEqual(ko.caller_stack(f"{ROOT}/ab-5", self.ROWS), "story-ab-5")

    def test_no_matching_row_means_no_stack(self):
        self.assertIsNone(ko.caller_stack(f"{ROOT}/cr-7", self.ROWS))
        self.assertIsNone(ko.caller_stack(f"{ROOT}/ab-59", []))


class OutputTests(unittest.TestCase):
    def test_the_owner_line_names_the_facts_judged_by(self):
        self.assertEqual(ko.owner_line(owner()), f"owner: worktree={WT} since={SINCE} stack=story-cr-7")
        self.assertIn("stack=none", ko.owner_line(owner(stack=None)))

    def test_kill_and_would_kill_and_refuse_lines(self):
        ok = ko.Verdict(row(100, f"{WT}/src"), [])
        no = ko.Verdict(row(101, MAIN), ["cwd is in the main checkout", "created before --since"])
        self.assertTrue(ko.render(ok, dry_run=False).startswith("KILL 100 python.exe cwd="))
        self.assertIn("created=", ko.render(ok, dry_run=False))
        self.assertTrue(ko.render(ok, dry_run=True).startswith("WOULD-KILL 100 "))
        self.assertTrue(ko.render(no, dry_run=False).startswith("REFUSE 101 python.exe"))
        self.assertIn("cwd is in the main checkout; created before --since", ko.render(no, dry_run=False))

    def test_exit_codes(self):
        ok = ko.Verdict(row(100, WT), [])
        no = ko.Verdict(row(101, MAIN), ["x"])
        self.assertEqual(ko.exit_code([ok], alive=[]), 0)
        self.assertEqual(ko.exit_code([ok, no], alive=[]), 1)
        self.assertEqual(ko.exit_code([ok], alive=[100]), 1)


class PsError(Exception):
    """psutil.Error."""


class PsNoSuchProcess(PsError):
    """psutil.NoSuchProcess: the process has exited."""


class PsAccessDenied(PsError):
    """psutil.AccessDenied: the process is alive and the fact cannot be read."""


class FakeProc:
    """The slice of `psutil.Process` that kill_owned.py reads. A fact named in `denied` raises AccessDenied when it
    is read; a process with `gone` set raises NoSuchProcess for every read; `parent_denied`, `ppid_denied` and
    `children_denied` make `parent()`, `ppid()` and `children()` raise AccessDenied."""

    def __init__(self, pid, parent=None, cwd=f"{WT}/src", cmdline="python.exe worker.py", name="python.exe",
                 created=T0 + 10, denied=(), gone=False, parent_denied=False, ppid_denied=False,
                 children_denied=False):
        self.pid, self.parent_proc, self.kids, self.killed = pid, parent, [], False
        self.facts = {"cwd": cwd, "cmdline": cmdline.split(), "name": name, "create_time": created}
        self.denied, self.gone = set(denied), gone
        self.parent_denied, self.ppid_denied, self.children_denied = parent_denied, ppid_denied, children_denied
        if parent is not None:
            parent.kids.append(self)

    def read(self, fact):
        if self.gone:
            raise PsNoSuchProcess(self.pid)
        if fact in self.denied:
            raise PsAccessDenied(self.pid)
        return self.facts[fact]

    def cwd(self):
        return self.read("cwd")

    def cmdline(self):
        return self.read("cmdline")

    def name(self):
        return self.read("name")

    def create_time(self):
        return self.read("create_time")

    def ppid(self):
        if self.ppid_denied:
            raise PsAccessDenied(self.pid)
        return self.parent_proc.pid if self.parent_proc else 0

    def oneshot(self):
        return contextlib.nullcontext()

    def parent(self):
        if self.parent_denied:
            raise PsAccessDenied(self.pid)
        return self.parent_proc

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

    def kill(self):
        if self.gone:
            raise PsNoSuchProcess(self.pid)
        self.killed = True


def _all_end(procs, timeout=None):
    return list(procs), []


def fake_psutil(*procs, wait_procs=_all_end):
    by_pid = {p.pid: p for p in procs}

    def process(pid):
        proc = by_pid.get(pid)
        if proc is None or proc.gone:
            raise PsNoSuchProcess(pid)
        return proc

    return types.SimpleNamespace(Error=PsError, NoSuchProcess=PsNoSuchProcess, AccessDenied=PsAccessDenied,
                                 Process=process, process_iter=lambda: list(by_pid.values()), wait_procs=wait_procs)


class UnreadableProcessTests(unittest.TestCase):
    """The live layer over a contract-faithful slice of psutil (the host venv has none): a process that is alive but
    cannot be read is refused and never reported GONE, and one that has exited is skipped."""

    def psutil(self, *procs):
        patch = mock.patch.dict(sys.modules, {"psutil": fake_psutil(*procs)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_ancestors_keep_every_readable_row_and_mark_an_unreadable_one(self):
        gate = FakeProc(40, cwd=MAIN, cmdline=GATE_CMD)
        hidden = FakeProc(50, parent=gate, denied={"cmdline"})
        target = FakeProc(100, parent=hidden)
        self.psutil(gate, hidden, target)
        rows = ko._ancestors(target)
        self.assertEqual([r["pid"] for r in rows], [50, 40])
        self.assertTrue(rows[0]["unreadable"])
        self.assertEqual(rows[0]["name"], "python.exe")
        self.assertEqual(rows[1]["cmdline"], GATE_CMD)

    def test_an_unreadable_ancestor_refuses_a_target_with_no_gate_above_it(self):
        hidden = FakeProc(50, denied={"cmdline"})
        target = FakeProc(100, parent=hidden)
        self.psutil(hidden, target)
        entries, gone = ko.scan_pids([100], False, owner(), frozenset())
        self.assertEqual([(v.row["pid"], v.reasons) for v, _ in entries], [(100, ["ancestor 50 unreadable"])])
        self.assertEqual(gone, [])

    def test_an_unreadable_ancestor_that_cannot_host_a_gate_does_not_refuse(self):
        for name in ("svchost.exe", "services.exe", "wininit.exe", "WmiPrvSE.exe"):
            with self.subTest(name=name):
                hidden = FakeProc(50, name=name, denied={"cmdline"})
                target = FakeProc(100, parent=hidden)
                self.psutil(hidden, target)
                entries, _ = ko.scan_pids([100], False, owner(), frozenset())
                self.assertEqual(entries[0][0].reasons, [])

    def test_an_unreadable_ancestor_whose_name_cannot_be_read_either_refuses(self):
        hidden = FakeProc(50, denied={"cmdline", "name"})
        target = FakeProc(100, parent=hidden)
        self.psutil(hidden, target)
        self.assertEqual(ko._ancestors(target), [{"pid": 50, "unreadable": True, "name": None}])
        entries, _ = ko.scan_pids([100], False, owner(), frozenset())
        self.assertEqual(entries[0][0].reasons, ["ancestor 50 unreadable"])

    def test_a_chain_through_unreadable_service_hosts_refuses_nothing_and_a_gate_above_them_still_refuses(self):
        gate = FakeProc(2, cwd=MAIN, cmdline=GATE_CMD)
        wininit = FakeProc(4, parent=gate, name="wininit.exe", denied={"cmdline"})
        services = FakeProc(8, parent=wininit, name="services.exe", denied={"cmdline"})
        svchost = FakeProc(12, parent=services, name="svchost.exe", denied={"cmdline"})
        wmi = FakeProc(16, parent=svchost, name="WmiPrvSE.exe", denied={"cmdline"})
        shell = FakeProc(20, parent=wmi, name="bash.exe", cmdline="bash.exe -c work")
        target = FakeProc(100, parent=shell)
        self.psutil(gate, wininit, services, svchost, wmi, shell, target)
        self.assertEqual([r["pid"] for r in ko._ancestors(target)], [20, 16, 12, 8, 4, 2])
        entries, _ = ko.scan_pids([100], False, owner(), frozenset())
        self.assertEqual(entries[0][0].reasons, ["under story-8-934a's run_full_suite.py pid 2"])
        wininit.parent_proc = None
        entries, _ = ko.scan_pids([100], False, owner(), frozenset())
        self.assertEqual(entries[0][0].reasons, [])

    def test_a_parent_link_that_cannot_be_read_refuses_naming_the_parent_not_the_target(self):
        target = FakeProc(100, parent=FakeProc(60), parent_denied=True)
        self.psutil(target)
        self.assertEqual(ko._ancestors(target), [{"pid": 60, "unreadable": True, "name": None}])
        entries, _ = ko.scan_pids([100], False, owner(), frozenset())
        self.assertEqual(entries[0][0].reasons, ["ancestor 60 unreadable"])

    def test_a_parent_link_and_ppid_that_cannot_be_read_refuse_naming_the_child(self):
        target = FakeProc(100, parent=FakeProc(60), parent_denied=True, ppid_denied=True)
        self.psutil(target)
        self.assertEqual(ko._ancestors(target), [{"pid": None, "of": 100, "unreadable": True, "name": None}])
        self.assertEqual(reasons(row(100, WT), ancestors=ko._ancestors(target)), ["parent of 100 unreadable"])

    def test_a_readable_ancestor_whose_parent_link_cannot_be_read_refuses(self):
        shell = FakeProc(50, parent=FakeProc(51), parent_denied=True)
        target = FakeProc(100, parent=shell)
        self.psutil(shell, target)
        found = ko._ancestors(target)
        self.assertEqual([r["pid"] for r in found], [50, 51])
        self.assertEqual(reasons(row(100, WT), ancestors=found), ["ancestor 51 unreadable"])

    def test_an_unreadable_ancestor_that_refuses_or_is_a_system_root_is_listed_once_when_its_link_fails(self):
        for name, expected in (("python.exe", ["ancestor 50 unreadable"]), ("svchost.exe", []), ("services.exe", []),
                               ("wininit.exe", []), ("SVCHOST.EXE", []), ("wininit", [])):
            with self.subTest(name=name):
                hidden = FakeProc(50, parent=FakeProc(51), name=name, denied={"cmdline"}, parent_denied=True)
                target = FakeProc(100, parent=hidden)
                self.psutil(hidden, target)
                self.assertEqual([r["pid"] for r in ko._ancestors(target)], [50])
                entries, _ = ko.scan_pids([100], False, owner(), frozenset())
                self.assertEqual(entries[0][0].reasons, expected)

    def test_an_unreadable_ancestor_outside_the_system_roots_whose_own_link_fails_hides_a_gate_and_refuses(self):
        for name in ("WmiPrvSE.exe", "bash.exe", "cmd.exe"):
            with self.subTest(name=name):
                gate = FakeProc(40, cwd=MAIN, cmdline=GATE_CMD)
                hidden = FakeProc(50, parent=gate, name=name, denied={"cmdline"}, parent_denied=True)
                target = FakeProc(100, parent=hidden)
                self.psutil(gate, hidden, target)
                found = ko._ancestors(target)
                self.assertEqual([r["pid"] for r in found], [50, 40])
                self.assertEqual(found[1], {"pid": 40, "unreadable": True, "name": None})
                entries, _ = ko.scan_pids([100], False, owner(), frozenset())
                self.assertEqual(entries[0][0].reasons, ["ancestor 40 unreadable"])

    def test_that_refusal_names_the_child_when_ppid_cannot_be_read_either(self):
        hidden = FakeProc(50, parent=FakeProc(51), name="WmiPrvSE.exe", denied={"cmdline"}, parent_denied=True,
                          ppid_denied=True)
        target = FakeProc(100, parent=hidden)
        self.psutil(hidden, target)
        self.assertEqual(ko._ancestors(target)[1:], [{"pid": None, "of": 50, "unreadable": True, "name": None}])
        entries, _ = ko.scan_pids([100], False, owner(), frozenset())
        self.assertEqual(entries[0][0].reasons, ["parent of 50 unreadable"])

    def test_a_readable_gate_above_an_unreadable_ancestor_still_refuses(self):
        gate = FakeProc(40, cwd=MAIN, cmdline=GATE_CMD)
        hidden = FakeProc(50, parent=gate, denied={"cmdline"})
        target = FakeProc(100, parent=hidden)
        self.psutil(gate, hidden, target)
        entries, _ = ko.scan_pids([100], False, owner(), frozenset())
        self.assertEqual(entries[0][0].reasons,
                         ["ancestor 50 unreadable", "under story-8-934a's run_full_suite.py pid 40"])

    def test_an_ancestor_that_exited_during_the_walk_ends_it_without_a_refusal(self):
        vanished = FakeProc(60, gone=True)
        target = FakeProc(100, parent=vanished)
        self.psutil(vanished, target)
        self.assertEqual(ko._ancestors(target), [])
        entries, _ = ko.scan_pids([100], False, owner(), frozenset())
        self.assertEqual(entries[0][0].reasons, [])

    def test_a_descendant_that_cannot_be_inspected_is_refused_and_one_that_exited_is_gone(self):
        top = FakeProc(100)
        readable = FakeProc(101, parent=top)
        hidden = FakeProc(102, parent=top, denied={"cmdline"})
        vanished = FakeProc(103, parent=top, gone=True)
        self.psutil(top, readable, hidden, vanished)
        entries, gone = ko.scan_pids([100], True, owner(), frozenset())
        verdicts = {v.row["pid"]: v for v, _ in entries}
        self.assertEqual(list(verdicts), [102, 101, 100])
        self.assertEqual(gone, [103])
        self.assertTrue(verdicts[101].owned and verdicts[100].owned)
        self.assertFalse(verdicts[102].owned)
        self.assertTrue(ko.render(verdicts[102], dry_run=False).startswith("REFUSE 102 ? — cannot be inspected"))
        self.assertEqual(ko.exit_code([v for v, _ in entries], alive=[]), 1)

    def test_a_target_whose_only_descendant_exited_is_clean(self):
        top = FakeProc(100)
        vanished = FakeProc(103, parent=top, gone=True)
        self.psutil(top, vanished)
        entries, gone = ko.scan_pids([100], True, owner(), frozenset())
        self.assertEqual(gone, [103])
        self.assertEqual(ko.exit_code([v for v, _ in entries], alive=[]), 0)

    def test_a_watch_member_that_cannot_be_inspected_is_refused_and_one_that_exited_is_skipped(self):
        root = FakeProc(10, cmdline=WATCH_CMD, name="docker.exe")
        readable = FakeProc(11, parent=root)
        hidden = FakeProc(12, parent=root, denied={"cmdline"})
        vanished = FakeProc(13, parent=root, gone=True)
        elsewhere = FakeProc(20, cwd=f"{ROOT}/16-916/src", cmdline=WATCH_CMD, name="docker.exe")
        self.psutil(root, readable, hidden, vanished, elsewhere)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            entries = ko.scan_watch(WT, own=frozenset())
        verdicts = {v.row["pid"]: v for v, _ in entries}
        self.assertEqual(list(verdicts), [12, 11, 10])
        self.assertFalse(verdicts[12].owned)
        self.assertTrue(verdicts[11].owned and verdicts[10].owned)
        self.assertIn("candidate pid=10", out.getvalue())
        self.assertIn("candidate pid=20", out.getvalue())
        self.assertEqual(ko.exit_code([v for v, _ in entries], alive=[]), 1)

    def test_a_target_whose_descendants_cannot_be_listed_is_refused_whole_under_tree_only(self):
        top = FakeProc(100, children_denied=True)
        self.psutil(top)
        entries, gone = ko.scan_pids([100], True, owner(), frozenset())
        self.assertEqual((len(entries), gone), (1, []))
        verdict = entries[0][0]
        self.assertFalse(verdict.owned)
        self.assertTrue(ko.render(verdict, dry_run=False).startswith(
            "REFUSE 100 python.exe — descendants cannot be enumerated"))
        self.assertEqual(ko.exit_code([verdict], alive=[]), 1)
        entries, _ = ko.scan_pids([100], False, owner(), frozenset())
        self.assertTrue(entries[0][0].owned)

    def test_a_watch_whose_descendants_cannot_be_listed_is_refused_and_another_watch_is_still_judged(self):
        blind = FakeProc(10, cmdline=WATCH_CMD, name="docker.exe", children_denied=True)
        sighted = FakeProc(30, cmdline=WATCH_CMD, name="docker.exe")
        child = FakeProc(31, parent=sighted)
        self.psutil(blind, sighted, child)
        with contextlib.redirect_stdout(io.StringIO()):
            entries = ko.scan_watch(WT, own=frozenset())
        verdicts = {v.row["pid"]: v for v, _ in entries}
        self.assertEqual(list(verdicts), [10, 31, 30])
        self.assertFalse(verdicts[10].owned)
        self.assertIn("descendants cannot be enumerated", verdicts[10].reasons[0])
        self.assertTrue(verdicts[31].owned and verdicts[30].owned)
        self.assertEqual(ko.exit_code([v for v, _ in entries], alive=[]), 1)


class ConsoleHostTests(unittest.TestCase):
    """Under `--tree`, a `conhost.exe` whose parent is an owned member of the same tree and whose only failing rule is
    the CWD rule is FOLLOWS: never killed, awaited with the kills, GONE when it ends within the wait and ALIVE (exit 1)
    when it does not. Any other console host is judged as before."""

    def psutil(self, *procs, wait_procs=_all_end):
        patch = mock.patch.dict(sys.modules, {"psutil": fake_psutil(*procs, wait_procs=wait_procs)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_console_host_of_an_owned_tree_member_follows_and_does_not_fail_the_exit_code(self):
        top = FakeProc(100, name="bash.exe")
        host = FakeProc(101, parent=top, name="conhost.exe", cwd="C:/Windows")
        self.psutil(top, host)
        entries, _ = ko.scan_pids([100], True, owner(), frozenset())
        verdicts = {v.row["pid"]: v for v, _ in entries}
        self.assertEqual(verdicts[101].follows, 100)
        self.assertFalse(verdicts[101].owned)
        self.assertEqual(ko.render(verdicts[101], dry_run=False), "FOLLOWS 101 conhost.exe — console host of 100")
        self.assertEqual(ko.exit_code([v for v, _ in entries], alive=[]), 0)
        self.assertEqual(ko.exit_code([v for v, _ in entries], alive=[101]), 1)

    def test_a_console_host_under_a_refused_member_stays_refused(self):
        top = FakeProc(100, name="bash.exe")
        shell = FakeProc(101, parent=top, name="bash.exe", cwd="C:/Users/x/scratchpad")
        host = FakeProc(102, parent=shell, name="conhost.exe", cwd="C:/Windows")
        self.psutil(top, shell, host)
        entries, _ = ko.scan_pids([100], True, owner(), frozenset())
        verdicts = {v.row["pid"]: v for v, _ in entries}
        self.assertIsNone(verdicts[102].follows)
        self.assertTrue(ko.render(verdicts[102], dry_run=False).startswith("REFUSE 102 conhost.exe — cwd is in"))

    def test_a_console_host_failing_another_rule_too_stays_refused(self):
        top = FakeProc(100, name="bash.exe")
        host = FakeProc(101, parent=top, name="conhost.exe", cwd="C:/Windows", created=T0 - 10)
        self.psutil(top, host)
        entries, _ = ko.scan_pids([100], True, owner(), frozenset())
        verdict = {v.row["pid"]: v for v, _ in entries}[101]
        self.assertIsNone(verdict.follows)
        self.assertEqual(ko.exit_code([verdict], alive=[]), 1)

    def test_a_console_host_named_with_pid_is_judged_as_before(self):
        for tree in (False, True):
            with self.subTest(tree=tree):
                top = FakeProc(100, name="bash.exe")
                host = FakeProc(101, parent=top, name="conhost.exe", cwd="C:/Windows")
                self.psutil(top, host)
                entries, _ = ko.scan_pids([101], tree, owner(), frozenset())
                self.assertEqual([v.row["pid"] for v, _ in entries], [101])
                self.assertIsNone(entries[0][0].follows)
                self.assertFalse(entries[0][0].owned)

    def stop_tree(self, ends_within_wait: set):
        """`--pid 100 --tree` over a bash target with its console host, through `stop()`; the fake `wait_procs`
        ends the PIDs in `ends_within_wait` and records what it was asked to wait on and for how long."""
        top = FakeProc(100, name="bash.exe")
        host = FakeProc(101, parent=top, name="conhost.exe", cwd="C:/Windows")
        waited = []

        def wait_procs(procs, timeout=None):
            waited.append(([p.pid for p in procs], timeout))
            return ([p for p in procs if p.pid in ends_within_wait],
                    [p for p in procs if p.pid not in ends_within_wait])

        self.psutil(top, host, wait_procs=wait_procs)
        entries, gone = ko.scan_pids([100], True, owner(), frozenset())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            killed, alive = ko.stop(entries, False, gone)
        self.assertTrue(top.killed)
        self.assertFalse(host.killed)
        self.assertEqual(waited, [([100, 101], ko.WAIT_SECONDS)])
        self.assertEqual(killed, [100])
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "FOLLOWS 101 conhost.exe — console host of 100")
        self.assertTrue(lines[1].startswith("KILL 100 bash.exe "), lines)
        return lines[2:], alive, [v for v, _ in entries]

    def test_stop_awaits_a_following_console_host_and_reports_it_gone(self):
        tail, alive, verdicts = self.stop_tree({100, 101})
        self.assertEqual(tail, ["GONE 101"])
        self.assertEqual(alive, [])
        self.assertEqual(ko.exit_code(verdicts, alive), 0)

    def test_stop_reports_a_following_console_host_that_outlives_the_wait_alive_and_exits_1(self):
        tail, alive, verdicts = self.stop_tree({100})
        self.assertEqual(tail, ["ALIVE 101"])
        self.assertEqual(alive, [101])
        self.assertEqual(ko.exit_code(verdicts, alive), 1)


SLEEPER = (
    "import os, sys, time\n"
    "end = time.time() + 120\n"
    "while time.time() < end and not os.path.exists(sys.argv[1]):\n"
    "    time.sleep(0.2)\n")
SPAWNING_SLEEPER = (
    "import subprocess, sys\n"
    "stop, child_wt, childfile = sys.argv[1:4]\n"
    f"kid = subprocess.Popen([sys.executable, '-c', {SLEEPER!r}, stop], cwd=child_wt, stdin=subprocess.DEVNULL,\n"
    "                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "open(childfile, 'w').write(str(kid.pid))\n" + SLEEPER)
TREE_LAUNCHER = """
import subprocess, sys
wt, child_wt, pidfile, childfile, stopfile, source = sys.argv[1:7]
p = subprocess.Popen([sys.executable, "-c", source, stopfile, child_wt, childfile], cwd=wt,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(pidfile, "w").write(str(p.pid))
"""
WATCH_LAUNCHER = """
import os, shutil, subprocess, sys
wt, fake, pidfile, childfile, stopfile, source = sys.argv[1:7]
os.makedirs(os.path.dirname(fake), exist_ok=True)
shutil.copy2(sys._base_executable, fake)
env = {**os.environ, "PYTHONHOME": sys.base_prefix,
       "PATH": os.path.dirname(sys._base_executable) + os.pathsep + os.environ.get("PATH", "")}
p = subprocess.Popen([fake, "-c", source, stopfile, wt, childfile, "compose", "-p", "story-x", "watch"], cwd=wt,
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(pidfile, "w").write(str(p.pid))
"""

LAUNCHER = """
import subprocess, sys
wt, pidfile, stopfile = sys.argv[1:4]
sleeper = ("import os, sys, time\\n"
           "end = time.time() + 120\\n"
           "while time.time() < end and not os.path.exists(sys.argv[1]):\\n"
           "    time.sleep(0.2)\\n")
p = subprocess.Popen([sys.executable, "-c", sleeper, stopfile], cwd=wt, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(pidfile, "w").write(str(p.pid))
"""


# Runs kill_owned.py's real entry with the hook-token directory pointed at a temporary one (argv: scripts dir, token
# dir, then the helper's own arguments), so no live test reads the main repo's .story-setup. Written to a file with
# the helper's PEP 723 header so `uv run` resolves psutil the way it does for the helper itself.
KILL_RUNNER = '''# /// script
# requires-python = ">=3.11"
# dependencies = ["psutil"]
# ///
import runpy
import sys
from pathlib import Path

scripts, tokens = sys.argv[1:3]
sys.path.insert(0, scripts)
import kill_token

kill_token.token_dir = lambda: Path(tokens)
sys.argv = [str(Path(scripts) / "kill_owned.py"), *sys.argv[3:]]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    return f'"{pid}"' in out


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("psutil") and shutil.which("uv"),
                     "live kill tests need Windows (tasklist), psutil and uv")
class LiveTests(unittest.TestCase):
    """The real helper against a real orphan: the launcher starts a sleeper in a fake worktree and exits, so the
    sleeper's parent PID is dead."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="kill-owned-live-"))
        cls.tokens = cls.tmp / "tokens"
        cls.runner = cls.tmp / "kill_runner.py"
        cls.runner.write_text(KILL_RUNNER, encoding="utf-8")
        cls.root = cls.tmp / "worktrees"
        for name in ("mine", "other"):
            (cls.root / name).mkdir(parents=True)
            (cls.root / name / ".git").write_text("gitdir: x\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.stopfiles, self.pids, self.children = [], [], []
        self.addCleanup(lambda: [child.wait(timeout=30) for child in self.children])
        self.addCleanup(lambda: [self.gone(pid) for pid in self.pids])
        self.addCleanup(lambda: [Path(f).write_text("stop") for f in self.stopfiles])

    def child(self, worktree: str) -> int:
        """A sleeper in `worktree` started by this process, so its parent chain is alive up to here."""
        stopfile = self.tmp / f"stop-{uuid.uuid4().hex[:8]}-{worktree}"
        self.stopfiles.append(str(stopfile))
        proc = subprocess.Popen([sys.executable, "-c", SLEEPER, str(stopfile)], cwd=str(self.root / worktree),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(proc)
        deadline = time.monotonic() + 10
        while not alive(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(alive(proc.pid))
        return proc.pid

    def orphan(self, worktree: str) -> int:
        tag = uuid.uuid4().hex[:8]
        pidfile, stopfile = self.tmp / f"pid-{tag}-{worktree}", self.tmp / f"stop-{tag}-{worktree}"
        self.stopfiles.append(str(stopfile))
        subprocess.run([sys.executable, "-c", LAUNCHER, str(self.root / worktree), str(pidfile), str(stopfile)],
                       check=True, timeout=60)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 10
        while not alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(alive(pid))
        return pid

    def token_args(self, worktree: str, since: str | None, nonce: str | None = None) -> list[str]:
        """The hook's argument: a fresh token for exactly this call's facts (or `nonce`), in the runner's directory."""
        nonce = nonce or kill_token.issue(self.tokens, str(self.root / worktree), since)
        return ["--hook-token", nonce]

    def run_helper(self, *args: str):
        return subprocess.run(
            ["uv", "run", "--system-certs", "--no-project", str(self.runner), str(SCRIPTS), str(self.tokens), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)

    def helper(self, pid: int, *extra: str, worktree="mine", since=None, token=True, nonce=None):
        since = since or (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.run_helper("--worktree", str(self.root / worktree), "--worktree-root", str(self.root),
                               "--since", since, "--pid", str(pid), *extra,
                               *(self.token_args(worktree, since, nonce) if token else []))

    def gone(self, pid: int) -> bool:
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        return not alive(pid)

    def test_the_callers_own_orphan_is_killed(self):
        pid = self.orphan("mine")
        r = self.helper(pid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"KILL {pid} ", r.stdout)
        self.assertTrue(r.stdout.startswith("owner: worktree="), r.stdout)
        self.assertTrue(self.gone(pid))

    def test_a_target_whose_parent_chain_is_alive_up_to_this_process_is_killed_unless_a_gate_is_above_it(self):
        """The helper walks this process's real ancestors, the SYSTEM ones above every Windows chain included, and an
        unreadable one must not refuse the target. Under `run_full_suite.py` the chain holds a gate, which refuses a
        descendant by design (the fake worktree has no registry row), so there the refusal names the gate alone."""
        pid = self.child("mine")
        probe = self.helper(pid, "--dry-run")
        self.assertNotIn("unreadable", probe.stdout)
        if "run_full_suite.py" in probe.stdout:
            self.assertEqual(probe.returncode, 1, probe.stdout + probe.stderr)
            self.assertIn(f"REFUSE {pid} ", probe.stdout)
            self.assertTrue(alive(pid))
            return
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        self.assertIn(f"WOULD-KILL {pid} ", probe.stdout)
        r = self.helper(pid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"KILL {pid} ", r.stdout)
        self.assertTrue(self.gone(pid))

    def test_an_orphan_in_the_other_worktree_is_refused_and_survives(self):
        pid = self.orphan("other")
        r = self.helper(pid)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"REFUSE {pid} ", r.stdout)
        self.assertIn("worktree `other`", r.stdout)
        self.assertTrue(alive(pid))

    def test_a_process_created_before_since_is_refused_and_survives(self):
        pid = self.orphan("mine")
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self.helper(pid, since=future)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"REFUSE {pid} ", r.stdout)
        self.assertTrue(alive(pid))

    def test_dry_run_kills_nothing(self):
        pid = self.orphan("mine")
        r = self.helper(pid, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"WOULD-KILL {pid} ", r.stdout)
        self.assertTrue(alive(pid))

    def test_a_call_without_a_hook_token_is_refused_before_any_process_is_read_and_the_orphan_survives(self):
        pid = self.orphan("mine")
        r = self.helper(pid, token=False)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("no hook token: call it through the sanctioned form", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertTrue(alive(pid))

    def test_a_used_token_is_refused_on_the_second_call_and_the_orphan_survives(self):
        pid = self.orphan("mine")
        since = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = kill_token.issue(self.tokens, str(self.root / "mine"), since)
        first = self.helper(pid, "--dry-run", since=since, nonce=nonce)
        second = self.helper(pid, since=since, nonce=nonce)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
        self.assertIn("already used", second.stderr)
        self.assertTrue(alive(pid))

    def test_a_pid_that_is_gone_is_reported_gone(self):
        pid = self.orphan("mine")
        Path(self.stopfiles[-1]).write_text("stop")
        self.assertTrue(self.gone(pid))
        r = self.helper(pid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"GONE {pid}", r.stdout)

    def pair(self, launcher: str, worktree: str, second: str) -> tuple[int, int]:
        """Start a sleeper in `worktree` that spawns one sleeping child, through a launcher that exits; (the
        sleeper's PID, the child's PID)."""
        tag = uuid.uuid4().hex[:8]
        pidfile, childfile, stopfile = (self.tmp / f"{kind}-{tag}" for kind in ("pid", "child", "stop"))
        self.stopfiles.append(str(stopfile))
        subprocess.run([sys.executable, "-c", launcher, str(self.root / worktree), second, str(pidfile),
                        str(childfile), str(stopfile), SPAWNING_SLEEPER], check=True, timeout=60)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (childfile.exists() and childfile.read_text().strip()):
            time.sleep(0.1)
        pid, child = int(pidfile.read_text()), int(childfile.read_text())
        self.pids += [pid, child]
        self.assertTrue(alive(pid) and alive(child))
        return pid, child

    def fake_watch(self, worktree: str) -> tuple[int, int]:
        """A `docker.exe compose -p story-x watch` (a copy of the interpreter under that name) with one child."""
        fake = self.tmp / f"fake-{uuid.uuid4().hex[:8]}" / "docker.exe"
        return self.pair(WATCH_LAUNCHER, worktree, str(fake))

    def assertKilledBefore(self, stdout: str, first: int, second: int):
        for pid in (first, second):
            self.assertIn(f"KILL {pid} ", stdout)
        self.assertLess(stdout.index(f"KILL {first} "), stdout.index(f"KILL {second} "), stdout)

    def watch_helper(self, worktree: str = "mine"):
        return self.run_helper("--worktree", str(self.root / worktree), "--worktree-root", str(self.root), "--watch",
                               *self.token_args(worktree, None))

    def test_tree_kills_an_owned_targets_descendants_bottom_up(self):
        parent, child = self.pair(TREE_LAUNCHER, "mine", str(self.root / "mine"))
        r = self.helper(parent, "--tree")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertKilledBefore(r.stdout, child, parent)
        self.assertTrue(self.gone(parent) and self.gone(child))

    def test_tree_refuses_a_descendant_in_another_worktree_and_still_kills_its_owned_parent(self):
        parent, child = self.pair(TREE_LAUNCHER, "mine", str(self.root / "other"))
        r = self.helper(parent, "--tree")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"KILL {parent} ", r.stdout)
        self.assertIn(f"REFUSE {child} ", r.stdout)
        self.assertIn("worktree `other`", r.stdout)
        self.assertTrue(self.gone(parent))
        self.assertTrue(alive(child))

    def test_watch_mode_kills_a_compose_watch_in_the_worktree_with_its_child_and_spares_another_worktrees(self):
        mine, mine_child = self.fake_watch("mine")
        other, other_child = self.fake_watch("other")
        r = self.watch_helper("mine")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertKilledBefore(r.stdout, mine_child, mine)
        self.assertIn(f"candidate pid={other}", r.stdout)
        self.assertNotIn(f"KILL {other} ", r.stdout)
        self.assertTrue(self.gone(mine) and self.gone(mine_child))
        self.assertTrue(alive(other) and alive(other_child))


if __name__ == "__main__":
    unittest.main()
