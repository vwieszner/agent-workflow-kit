#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["psutil"]
# ///
"""Kill host processes only when the caller provably owns them, and say why anything else is refused.

Usage (psutil is a PEP 723 dependency). An option marked (hook) is appended by the process-kill guard, so a
caller passes none of them (`--worktree` for a subagent only: a main session passes it):
  uv run --no-project <plugin-root>/scripts/kill_owned.py --worktree <wt> (hook, subagent) --since <ISO-8601> (hook) --hook-token <nonce> (hook) --pid N [--pid N ...] [--tree] [--dry-run]
  uv run --no-project <plugin-root>/scripts/kill_owned.py --worktree <wt> (hook, subagent) --hook-token <nonce> (hook) --watch [--dry-run]

The guard (`.workflow/hooks/guards/process_kill.py`) appends `--worktree` and `--since` for a subagent (its
dispatch's `worktree_path`, its first transcript timestamp) and `--since` for a main session, which names
`--worktree` itself; a call that supplies them is denied. It also appends `--hook-token`, a single-use token it
wrote for exactly those values (`kill_token.py`).

A call without a valid token is refused with exit 2 before any process is read: the file
`.workflow/state/kill-tokens/<nonce>.json` must exist, is deleted as it is redeemed, and must be unexpired and
hold this call's `--worktree` and `--since`. It does not cover code that never goes through `main()` (an agent's
own psutil kill code, an in-process import of this module). A human running this CLI in their own terminal has
no token and is refused. `cleanup_story_stack.py` may import `scan_watch` and `stop`.

PID mode kills a target only when every rule passes; each failing rule is named on its REFUSE line:
  1. it is not this helper's own process or one of its ancestors;
  2. its CWD is readable;
  3. its CWD is inside `--worktree` (component-wise; the main checkout, another worktree or any other path
     refuses and is named);
  4. it was created at or after `--since`;
  5. it is not a compose watch (use `--watch`);
  6. no live ancestor is another stack's `run_full_suite.py` gate or compose watch, nor any when the caller's
     stack is unknown (the `in_use` slot-registry row whose worktree is `--worktree`), and no ancestor that could
     be one cannot be read: an unreadable ancestor refuses only when its name is a python, uv or container-engine
     executable or cannot be read either; Windows SYSTEM roots (`wininit`, `services`, `svchost`) end the walk.
Ancestry only ever refuses; it never grants ownership. `--tree` also judges and kills an owned target's live
descendants, bottom-up; a Windows `conhost.exe` whose parent is an owned member and whose only failing rule is
rule 3 is printed `FOLLOWS` and awaited, not killed; a descendant that cannot be inspected is refused, and a
target whose descendants cannot be listed is refused whole. Owned targets are killed through the judged
`psutil.Process` objects (PID-plus-creation-time check closes the judge-to-kill window).

`--watch` selects every compose-watch process whose CWD is inside `--worktree`, plus their live descendants.

Output: `owner: worktree=<path> since=<ISO|none> stack=<stack|none>`, then `KILL|WOULD-KILL <pid> <name>
cwd=<cwd> created=<HH:MM:SS>`, `REFUSE <pid> <name> — <reasons>`, `FOLLOWS <pid> conhost.exe — console host of
<ppid>`, `GONE <pid>`, `ALIVE <pid>`. Exit 0 nothing refused and nothing alive; 1 any REFUSE or ALIVE; 2 an
argument error, a missing psutil, or a missing, used, expired or mismatched hook token (before any process is
read). A REFUSE line is final: ask the owner, do not work around it.
`--worktree` must be an existing direct child of the worktree root (`config: git.worktrees_root`) holding a
`.git` file; `--worktree-root` exists for tests and is denied by the guard.

psutil is imported only where live processes are read, so the rules import and run without it.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wfconfig  # noqa: E402
import _registry_lookup  # noqa: E402
import _stack  # noqa: E402
import kill_token  # noqa: E402

GATE_EXECUTABLE = re.compile(r"^(?:py|pythonw?[\d.]*|uvx?|docker(?:-compose)?|podman(?:-compose)?)(?:\.exe)?$",
                             re.IGNORECASE)
SYSTEM_ROOT = re.compile(r"^(?:wininit|services|svchost)(?:\.exe)?$", re.IGNORECASE)
_STORY_ID = re.compile(r"--story-id[=\s]+['\"]?([^\s'\"]+)")
_PROJECT = re.compile(r"(?:^|\s)(?:-p|--project-name)[=\s]+['\"]?([^\s'\"]+)")
WAIT_SECONDS = 5
CONSOLE_HOST = "conhost.exe"
_CWD_RULE = "cwd is in "


@dataclass(frozen=True)
class Owner:
    worktree: str
    since: float | None
    since_text: str | None
    stack: str | None
    worktree_root: str
    main_root: str | None


@dataclass(frozen=True)
class Verdict:
    row: dict
    reasons: list
    follows: int | None = None  # the owned parent pid a console host is awaited with; never killed

    @property
    def owned(self) -> bool:
        return not self.reasons and self.follows is None


# ---------------------------------------------------------------- pure rules
def norm(path) -> PurePath:
    return PurePath(os.path.normcase(os.path.normpath(str(path))))


def inside(path, root) -> bool:
    """True when `path` is `root` or lies under it, compared as path components."""
    return norm(path).is_relative_to(norm(root))


def where(cwd: str, owner: Owner) -> str:
    """Where a CWD outside the caller's worktree is: another worktree by name, the main checkout, else the path."""
    root = norm(owner.worktree_root)
    if norm(cwd) != root and inside(cwd, root):
        return f"worktree `{norm(cwd).parts[len(root.parts)]}`"
    if owner.main_root is not None and inside(cwd, owner.main_root):
        return "the main checkout"
    return str(cwd)


def is_compose_watch(row: dict) -> bool:
    return _stack.is_compose_watch(str(row["name"]), row["cmdline"])


def ancestor_stack(row: dict) -> tuple[bool, str | None]:
    """(counts, stack): a `run_full_suite.py` gate (the story's stack from `--story-id`, else the shared stack) or a
    compose watch (its `-p` / `--project-name` value, else unknown) is another stack's unless it is the caller's."""
    cmd = row["cmdline"]
    if "run_full_suite.py" in cmd:
        match = _STORY_ID.search(cmd)
        shared = str(wfconfig.get("slots.shared_stack_name", wfconfig.get("project.name", "project")))
        return True, wfconfig.stack_name(match.group(1)) if match else shared
    if is_compose_watch(row):
        match = _PROJECT.search(cmd)
        return True, match.group(1) if match else None
    return False, None


def may_host_gate(name) -> bool:
    """Whether an unreadable ancestor named `name` could be a gate or compose watch."""
    return name is None or bool(GATE_EXECUTABLE.match(str(name)))


def ends_walk(ancestor: dict) -> bool:
    """Whether the ancestor walk may end silently at an unreadable link whose own parent cannot be looked up."""
    name = ancestor.get("name")
    return bool(ancestor.get("unreadable")) and (may_host_gate(name) or bool(SYSTEM_ROOT.match(str(name))))


def _unreadable_text(ancestor: dict) -> str:
    if ancestor["pid"] is None:
        return f"parent of {ancestor['of']} unreadable"
    return f"ancestor {ancestor['pid']} unreadable"


def _hms(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def refusals(row: dict, owner: Owner, ancestors: list, own_pids: frozenset) -> list:
    """Every ownership rule the target fails, as text; empty when the caller owns it."""
    out = []
    if row["pid"] in own_pids:
        out.append("the helper's own process tree is never killed")
    cwd = row["cwd"]
    if cwd is None:
        out.append("cwd unreadable: ownership unproven")
    elif not inside(cwd, owner.worktree):
        out.append(f"cwd is in {where(cwd, owner)}")
    if owner.since is not None and row["create_time"] < owner.since:
        out.append(f"created {_hms(row['create_time'])}, before --since {_hms(owner.since)}")
    if is_compose_watch(row):
        out.append("a compose watch: stop it with --watch")
    for ancestor in ancestors:
        if ancestor.get("unreadable"):
            if may_host_gate(ancestor.get("name")):
                out.append(_unreadable_text(ancestor))
            continue
        counts, stack = ancestor_stack(ancestor)
        if counts and (owner.stack is None or stack != owner.stack):
            kind = "run_full_suite.py" if "run_full_suite.py" in ancestor["cmdline"] else "compose watch"
            out.append(f"under {stack or 'unknown'}'s {kind} pid {ancestor['pid']}")
    return out


def decide(rows: list, owner: Owner, ancestors: dict, own_pids: frozenset) -> list:
    return [Verdict(r, refusals(r, owner, ancestors.get(r["pid"], []), own_pids)) for r in rows]


def select_watch(rows: list, worktree: str) -> list:
    """The compose-watch rows whose CWD is inside the worktree; an unreadable CWD selects nothing."""
    return [r for r in rows if is_compose_watch(r) and r["cwd"] is not None and inside(r["cwd"], worktree)]


def parse_since(text: str) -> float:
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def worktree_error(worktree: str, worktree_root: str) -> str | None:
    """Why `worktree` is not a story worktree (an existing direct child of the root holding a `.git` file)."""
    wt = norm(worktree)
    if wt.parent != norm(worktree_root) or wt == norm(worktree_root):
        return f"--worktree {worktree} is not a direct child of the worktree root {worktree_root}"
    path = Path(worktree)
    if not path.is_dir():
        return f"--worktree {worktree} does not exist"
    if not (path / ".git").is_file():
        return f"--worktree {worktree} is not a git worktree (no .git file)"
    return None


def caller_stack(worktree: str, in_use: list[tuple[str, str]]) -> str | None:
    """The stack of the `in_use` registry row (worktree, story id) whose worktree is the caller's, else None."""
    for wt, story in in_use:
        if norm(wt) == norm(worktree):
            return wfconfig.stack_name(story)
    return None


def owner_line(owner: Owner) -> str:
    return f"owner: worktree={owner.worktree} since={owner.since_text or 'none'} stack={owner.stack or 'none'}"


def render(verdict: Verdict, dry_run: bool) -> str:
    row = verdict.row
    if verdict.follows is not None:
        return f"FOLLOWS {row['pid']} {row['name']} — console host of {verdict.follows}"
    if verdict.owned:
        created = _hms(row["create_time"])
        return f"{'WOULD-KILL' if dry_run else 'KILL'} {row['pid']} {row['name']} cwd={row['cwd']} created={created}"
    return f"REFUSE {row['pid']} {row['name']} — {'; '.join(verdict.reasons)}"


def exit_code(verdicts: list, alive: list) -> int:
    return 1 if alive or any(not v.owned and v.follows is None for v in verdicts) else 0


def follower(verdict: Verdict, owned_pids: set) -> Verdict:
    """`verdict` as a FOLLOWS when it is a console host of an owned tree member refused by the CWD rule alone."""
    row = verdict.row
    if (str(row.get("name", "")).lower() == CONSOLE_HOST and row.get("ppid") in owned_pids
            and len(verdict.reasons) == 1 and verdict.reasons[0].startswith(_CWD_RULE)):
        return Verdict(row, [], follows=row["ppid"])
    return verdict


# ---------------------------------------------------------------- arguments
def parse_args(argv):
    ap = argparse.ArgumentParser(description="kill host processes only when the caller owns them",
                                 allow_abbrev=False)
    ap.add_argument("--worktree")
    ap.add_argument("--since")
    ap.add_argument("--pid", type=int, action="append", default=[])
    ap.add_argument("--tree", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--worktree-root", default=str(wfconfig.worktrees_root()))
    ap.add_argument("--hook-token")
    return ap.parse_args(argv)


def validate(args) -> str | None:
    """The reason the arguments are refused (exit 2), else None."""
    if not args.worktree:
        return "--worktree is required"
    if args.watch and args.pid:
        return "--watch and --pid are separate modes"
    if not args.watch and not args.pid:
        return "name the targets with --pid, or use --watch"
    if args.since is not None:
        try:
            parse_since(args.since)
        except ValueError:
            return f"--since {args.since!r} is not an ISO-8601 timestamp"
    elif not args.watch:
        return "--since is required with --pid"
    return worktree_error(args.worktree, args.worktree_root)


def token_problem(args) -> str | None:
    """Why the hook token is refused (exit 2), else None. Redeeming deletes the token."""
    try:
        directory = kill_token.token_dir()
    except OSError as exc:
        return f"the token directory cannot be located: {exc}"
    return kill_token.redeem(directory, args.hook_token, args.worktree, args.since)


# ---------------------------------------------------------------- live layer
def _row(proc) -> dict:
    import psutil
    try:
        cwd = proc.cwd()
    except (psutil.Error, OSError):
        cwd = None
    with proc.oneshot():
        return {"pid": proc.pid, "ppid": proc.ppid(), "name": proc.name(),
                "cmdline": " ".join(proc.cmdline() or []), "cwd": cwd, "create_time": proc.create_time()}


def _own_pids() -> frozenset:
    import psutil
    return frozenset({os.getpid()} | {p.pid for p in psutil.Process().parents()})


def _name_or_none(proc):
    import psutil
    try:
        return proc.name()
    except (psutil.Error, OSError):
        return None


def _unknown_parent(node) -> dict:
    """The marker for the parent that `node.parent()` cannot look up: its pid when `ppid()` reads it."""
    import psutil
    try:
        return {"pid": node.ppid(), "unreadable": True, "name": None}
    except (psutil.Error, OSError):
        return {"pid": None, "of": node.pid, "unreadable": True, "name": None}


def _ancestors(proc) -> list:
    """The ancestors of `proc`, nearest first, as rows. One whose facts cannot be read is `{"pid": ..., "unreadable":
    True, "name": ...}` (`name` is None when that cannot be read either), so a readable gate or watch above it still
    counts. The walk ends at the first ancestor that has exited. A parent link that cannot be looked up ends it with
    a marker for the parent, except at an unreadable ancestor that `ends_walk` accepts (a SYSTEM root, or one that
    already refuses by name)."""
    import psutil
    rows, node = [], proc
    while True:
        try:
            parent = node.parent()
        except psutil.NoSuchProcess:
            return rows
        except (psutil.Error, OSError):
            return rows if rows and ends_walk(rows[-1]) else [*rows, _unknown_parent(node)]
        if parent is None:
            return rows
        try:
            rows.append(_row(parent))
        except psutil.NoSuchProcess:
            return rows
        except (psutil.Error, OSError):
            rows.append({"pid": parent.pid, "unreadable": True, "name": _name_or_none(parent)})
        node = parent


def _uninspectable(pid: int, exc: Exception) -> Verdict:
    """The refusal for a process that is alive but whose facts cannot be read: its ownership is unproven."""
    return Verdict({"pid": pid, "name": "?", "cwd": None, "create_time": 0.0}, [f"cannot be inspected: {exc}"])


def _judge(proc, owner: Owner, own: frozenset) -> tuple[Verdict, object]:
    row = _row(proc)
    return Verdict(row, refusals(row, owner, _ancestors(proc), own)), proc


def _unenumerable(row: dict, exc: Exception) -> Verdict:
    """The refusal for an owned target whose descendants cannot be listed: they would outlive it unjudged."""
    return Verdict(row, [f"descendants cannot be enumerated: {exc}"])


def _bottom_up(proc) -> tuple[list, Exception | None]:
    """(the live descendants of `proc`, deepest first, then `proc` itself (the psutil `kill_proc_tree` order), the
    error that stopped the listing else None). A `proc` that has exited has no descendants to list."""
    import psutil
    try:
        children = proc.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    except (psutil.Error, OSError) as exc:
        return [proc], exc
    return [*reversed(children), proc], None


def scan_pids(pids: list, tree: bool, owner: Owner, own: frozenset) -> tuple[list, list]:
    """([(Verdict, Process)] in kill order, the PIDs that are already gone). A refused target is never expanded."""
    import psutil
    entries, gone, seen = [], [], set()
    for pid in pids:
        try:
            verdict, proc = _judge(psutil.Process(pid), owner, own)
        except psutil.NoSuchProcess:
            gone.append(pid)
            continue
        except (psutil.Error, OSError) as exc:
            entries.append((_uninspectable(pid, exc), None))
            continue
        group = [proc]
        if tree and verdict.owned:
            group, failure = _bottom_up(proc)
            if failure is not None:
                seen.add(proc.pid)
                entries.append((_unenumerable(verdict.row, failure), proc))
                continue
        judged = []
        for member in group:
            if member.pid in seen:
                continue
            seen.add(member.pid)
            try:
                judged.append((verdict, proc) if member is proc else _judge(member, owner, own))
            except psutil.NoSuchProcess:
                gone.append(member.pid)
            except (psutil.Error, OSError) as exc:
                judged.append((_uninspectable(member.pid, exc), None))
        # Bottom-up order judges a console host before its parent, so ownership is resolved after the whole group.
        owned_pids = {v.row["pid"] for v, _ in judged if v.owned}
        entries.extend((follower(v, owned_pids) if tree else v, p) for v, p in judged)
    return entries, gone


def scan_watch(worktree: str, own: frozenset | None = None) -> list:
    """[(Verdict, Process)] for the compose-watch processes anchored inside `worktree` and their live descendants,
    in kill order. Each docker candidate is printed with its CWD before the verdict."""
    import psutil
    own = _own_pids() if own is None else own
    rows = {}
    for proc in psutil.process_iter():
        try:
            row = _row(proc)
        except (psutil.Error, OSError):
            continue
        if is_compose_watch(row):
            rows[row["pid"]] = (row, proc)
            print(f"  candidate pid={row['pid']:<6} cwd={row['cwd'] or '<unreadable>'}")
    chosen = select_watch([r for r, _ in rows.values()], worktree)
    entries, seen = [], set()
    for root in chosen:
        members, failure = _bottom_up(rows[root["pid"]][1])
        if failure is not None:
            seen.add(root["pid"])
            entries.append((_unenumerable(root, failure), rows[root["pid"]][1]))
            continue
        for member in members:
            if member.pid in seen:
                continue
            seen.add(member.pid)
            try:
                row = rows[member.pid][0] if member.pid in rows else _row(member)
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError) as exc:
                entries.append((_uninspectable(member.pid, exc), None))
                continue
            reasons = ["the helper's own process tree is never killed"] if member.pid in own else []
            entries.append((Verdict(row, reasons), member))
    return entries


def stop(entries: list, dry_run: bool, gone: list = ()) -> tuple[list, list]:
    """Print each verdict, kill the owned ones (unless `dry_run`) and wait on them and on their FOLLOWS console hosts.
    (the PIDs killed, the PIDs still alive)."""
    import psutil
    for pid in gone:
        print(f"GONE {pid}")
    for verdict, _proc in entries:
        print(render(verdict, dry_run), flush=True)
    if dry_run:
        return [], []
    killed = []
    followers = [proc for verdict, proc in entries if verdict.follows is not None and proc is not None]
    for verdict, proc in entries:
        if not verdict.owned:
            continue
        try:
            proc.kill()
            killed.append(proc)
        except psutil.NoSuchProcess:
            print(f"GONE {proc.pid}")
        except (psutil.Error, OSError):
            killed.append(proc)
    ended, alive = psutil.wait_procs([*killed, *followers], timeout=WAIT_SECONDS)
    for proc in ended:
        if proc in followers:
            print(f"GONE {proc.pid}")
    for proc in alive:
        print(f"ALIVE {proc.pid}")
    return [p.pid for p in killed], [p.pid for p in alive]


def _build_owner(args) -> Owner:
    rows = []
    for row in _registry_lookup.rows():
        if row.get("status") == "in_use" and row.get("story_id") and row.get("worktree"):
            p = Path(row["worktree"])
            rows.append((str(p if p.is_absolute() else wfconfig.repo_root() / p), row["story_id"]))
    return Owner(worktree=args.worktree, since=parse_since(args.since) if args.since else None,
                 since_text=args.since, stack=caller_stack(args.worktree, rows),
                 worktree_root=args.worktree_root, main_root=str(wfconfig.repo_root()))


def main(argv=None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    problem = validate(args)
    if problem:
        print(f"kill_owned: {problem}", file=sys.stderr)
        return 2
    refusal = token_problem(args)
    if refusal:
        print(f"kill_owned: no hook token: call it through the sanctioned form ({refusal})", file=sys.stderr)
        return 2
    owner = _build_owner(args)
    print(owner_line(owner), flush=True)
    try:
        own = _own_pids()
        if args.watch:
            entries, gone = scan_watch(args.worktree, own), []
        else:
            entries, gone = scan_pids(args.pid, args.tree, owner, own)
        _killed, alive = stop(entries, args.dry_run, gone)
    except ImportError:
        print("kill_owned: psutil is required: run `uv run --no-project <plugin-root>/scripts/kill_owned.py ...` "
              "(PEP 723 installs it into uv's cache)", file=sys.stderr)
        return 2
    return exit_code([v for v, _ in entries], alive)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.exit(main())
