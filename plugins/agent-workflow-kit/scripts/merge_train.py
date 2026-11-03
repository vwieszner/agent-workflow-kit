#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The serial merge train: a story waiting in the land lease's line gates the tree it will land with.

While the stories ahead land, a waiting story builds its train branch (`config: git.train_prefix` + branch
suffix) in its own worktree -- the story branch plus `config: git.base_branch` plus every story ahead of it (the
lease holder first, then the line entries in order) -- and runs the pre-merge gate on it
(`run_full_suite.py --story-id <id> --train --record`). Its rebased tree at the land window is then the tree the
gate tested, and `story_ledger.premerge_verdict` lets it merge without a second gate. The train state lives in
the lease record (`land_lease.py`, the `train` key of the holder and of every line entry); the story branch never
changes.

    uv run --no-project <plugin-root>/scripts/merge_train.py [--lease-path P] wait   <story-id> [--holder-stall-min M]
    uv run --no-project <plugin-root>/scripts/merge_train.py [--lease-path P] build  <story-id>
    uv run --no-project <plugin-root>/scripts/merge_train.py [--lease-path P] settle <story-id>
    uv run --no-project <plugin-root>/scripts/merge_train.py [--lease-path P] home   <story-id>
    uv run --no-project <plugin-root>/scripts/merge_train.py [--lease-path P] status

  wait    replaces `land_lease.py acquire <id> --wait-minutes N`, with no total budget. Each poll: one `take`
          (acquired or already -> the worktree on the story branch, exit 0), otherwise `join` and `advance`, then
          one timestamped line naming the holder (or `free`), the position, the train state and the story waited
          for. The train gate runs inside the wait, so the story takes the lease only after its own gate ended.
          Exit 1 no standing checkpoint-3 approval; 2 a lease or git error; 3 the holder has held the lease
          longer than `--holder-stall-min` (default `config: land.holder_stall_min`, 180), or the lease has stood
          free that long behind the same head of the line; 4 a red train gate withdrew the approval (its output
          is `.workflow/state/story-setup/train-gate-<id>.txt`). Exits 2, 3 and 4 leave the line and check out the
          story branch; the failed gate's train branch stays.
  build   one train build: exit 0 with what was built, 1 with the refusal (nothing built). The worktree ends on
          the story branch; a built train is recorded without inputs, so the next `advance` rebuilds and gates it.
  settle  in the land window, after the rebase: when `story_ledger.premerge_verdict` passes (a train GREEN that
          carries over, or a `not required` exemption), mark the holder `green` for `HEAD`. Exit 0 marked; 1 the
          guard blocks (its reason is printed; run the gate); 2 the story does not hold the lease, or an error.
  home    check out the story branch in the story worktree when it is on another branch. Exit 0; 2 on a git error.
  status  each story's train state.

`advance(story, lease_path)` is one poll step. First a dead gate holder is recovered (`recovered`): the holder's
train is `gating`, its `gating_seen` stamp is missing, unreadable or older than `land_lease.STALE_SECONDS`, its
latest `pre-merge gate` record line is not a GREEN for the train's tip, and this process saw the same stale stamp
at least two heartbeats earlier. The detector frees the lease with `land_lease.release_dead_gate` (conditional on
that stamp, so one detector wins) and withdraws the holder's checkpoint-3 approval. The gate turn: every story
ahead is `green` for its current branch tip, so at most one gate of the line is pending and a red story leaves
the line before any follower gates on it. A story whose own state is `gating` is running its gate; only a stamp
older than two heartbeats (`gating_stale`) marks a dead run, which is cleared so the story rebuilds. Then, unless
the story's recorded `inputs` (a fingerprint of the base-branch tip, each story ahead with its tip, and the own
tip) equal the live ones, it builds. A merge conflict or a stack-affecting path (`config: env.dependency_files`,
`config: pipeline.schema_globs`, `config: stack.compose_file`, `Dockerfile*`, `config:
pipeline.stack_affecting_globs`) in the train sets state `none` and the story gates in its land window. When the
story has a tested train and the new one differs from it in no gate input (`story_ledger._gate_inert`), the
verdict carries over with no gate. A story whose latest pre-merge verdict is `not required` is marked `green`.

The train gate child has a wall-clock bound (`config: land.train_gate_bound_min`, 55). At the bound its whole
process tree is killed (the gate's own handlers do not run and withdraw nothing), the slot stack's
`config: land.restart_services` are restarted and waited for, and the result is `train gate not run: ...`
(`NOT_RUN_EXIT`): the train is reset and the next poll gates it again. `GATE_ABORT` (set by an unattended
driver's immediate stop) starts no gate and makes a child that ended non-zero a not-run. The holder's window gate
is not bounded here.

The worktree is on the train branch only while a build or a train gate runs; the record file's uncommitted
lines survive every path. Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _registry_lookup  # noqa: E402
import land_lease  # noqa: E402
import run_scoped_tests  # noqa: E402
import story_ledger  # noqa: E402
import story_record  # noqa: E402
import wfconfig  # noqa: E402


def stack_affecting_patterns() -> tuple[str, ...]:
    pats = list(wfconfig.get("env.dependency_files", []) or [])
    pats += list(wfconfig.get("pipeline.schema_globs", []) or [])
    pats += list(wfconfig.get("pipeline.stack_affecting_globs", []) or [])
    compose = str(wfconfig.get("stack.compose_file", "") or "")
    if compose:
        pats.append(compose)
    pats.append("Dockerfile*")
    return tuple(p.replace("**/", "*/") for p in pats)


def _story_prefix() -> str:
    return str(wfconfig.get("git.branch_prefix", "story/"))


def _train_prefix() -> str:
    return str(wfconfig.get("git.train_prefix", "train/"))


NOT_RUN_MARKER = "train gate not run:"
NOT_RUN_ABORTED = f"{NOT_RUN_MARKER} stopped by the auto-dev driver's immediate stop"
NOT_RUN_EXIT = 75
EXIT_REFUSED, EXIT_ERROR, EXIT_HELD, EXIT_WITHDRAWN = 1, 2, 3, 4
WORKED = ("green", "carried", "fallback", "exempt", "recovered")
GIT_TIMEOUT_SECONDS = 600
# Wall-clock bound of one train gate child; keep it below STALL_BUDGET_MINUTES.
TRAIN_GATE_BOUND_MINUTES = int(wfconfig.get("land.train_gate_bound_min", 55))
# The longest a lease holder may hold the lease, or the lease may stand free behind the same head of the line,
# before a waiter takes the stall exit.
STALL_BUDGET_MINUTES = float(wfconfig.get("land.holder_stall_min", 180))
# Bound of the compose restart after a killed gate.
RESTART_BOUND_SECONDS = int(wfconfig.get("land.restart_bound_s", 60))
# Bound of the compose `up --wait` health wait after that restart.
HEALTH_WAIT_SECONDS = int(wfconfig.get("land.health_wait_s", 263))
# (lease path, holder, gating_seen) -> time.monotonic() of this process's first poll that saw the holder's gate
# stale. One key at a time: a changed holder or stamp starts the sighting over.
_SIGHTINGS: dict[tuple, float] = {}
# (lease path, holder) -> time.monotonic() of this process's first poll that found that holder past the stall budget
# with a dead-looking gate. One key at a time.
_STALL_WINDOWS: dict[tuple, float] = {}
# Set by an unattended driver's immediate stop: no gate starts, and a gate child that then ends non-zero was killed,
# not red.
GATE_ABORT = threading.Event()


class TrainError(RuntimeError):
    """A git or registry problem that stops a train step."""


class Refused(Exception):
    """A build that is refused; the message names the reason and nothing was built."""


@dataclass(frozen=True)
class Built:
    kind: str  # built | fallback | exempt | carried
    text: str


@dataclass(frozen=True)
class Step:
    kind: str  # absent | holder | idle | waiting | refused | gating | current | fallback | carried | exempt | green | recovered | not-run | failed | withdrawn
    text: str = ""


def _hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- git and the registry
def _git(worktree, *args: str, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrainError(f"git {' '.join(args)} could not run in {worktree}: {exc}") from exc
    if r.returncode not in ok:
        raise TrainError(f"git {' '.join(args)} failed in {worktree} (exit {r.returncode}): "
                         f"{(r.stderr or r.stdout).strip()}")
    return r


def _out(worktree, *args: str) -> str:
    return _git(worktree, *args).stdout.strip()


def _same(a: str, b: str) -> bool:
    return land_lease.dashed(a) == land_lease.dashed(b)


@dataclass(frozen=True)
class Roots:
    worktree: str
    branch: str   # the story branch
    train: str    # the train branch
    arts: str
    variants: list


def roots(story: str) -> Roots:
    vs = story_ledger.variants(story)
    arts, branch = story_ledger.resolve_roots(vs)
    if not branch:
        raise TrainError(f"{story} has no worktree and branch in the slot registry")
    prefix = _story_prefix()
    suffix = branch[len(prefix):] if branch.startswith(prefix) else branch
    return Roots(story_ledger._root_of(arts), branch, f"{_train_prefix()}{suffix}", arts, vs)


def development_tip(worktree) -> str:
    """The base branch's tip (`config: git.base_branch`)."""
    return _out(worktree, "rev-parse", f"refs/heads/{story_ledger.base_branch()}")


def story_tip(story: str) -> str:
    r = roots(story)
    return _out(r.worktree, "rev-parse", "--verify", f"refs/heads/{r.branch}")


def worktree_branch(worktree) -> str:
    """The checked-out branch of the worktree; empty when HEAD is detached."""
    return _out(worktree, "branch", "--show-current")


def dirty_gate_inputs(worktree) -> list[str]:
    """Dirty or untracked paths that are not gate-inert (the record file is under `docs/`, so it is not one)."""
    tokens = _git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split("\0")
    paths, i = [], 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            if i < len(tokens) and tokens[i]:
                paths.append(tokens[i])
            i += 1
    return [p for p in paths if not story_ledger._gate_inert(p)]


def _paths(out: str) -> list[str]:
    return [p for p in out.split("\0") if p]


def _gate_input_changes(worktree, a: str, b: str) -> list[str]:
    out = _git(worktree, "diff", "--name-only", "--no-renames", "-z", a, b).stdout
    return [p for p in _paths(out) if not story_ledger._gate_inert(p)]


def _stack_affecting(paths: list[str]) -> str | None:
    def hit(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatch(path, pat) or ("/" not in pat and fnmatch.fnmatch(name, pat))
                   for pat in stack_affecting_patterns())

    return next((p for p in paths if hit(p)), None)


# ---------------------------------------------------------------- the lease record
def _train_of(record: dict, story: str) -> dict | None:
    if record.get("story") and _same(record["story"], story):
        return record.get("train")
    return next((e.get("train") for e in record.get("line", []) if _same(e["story"], story)), None)


def ahead_of(story: str, record: dict) -> list[str] | None:
    """The stories ahead: the holder first, then the line entries before `story`; None when it is not in the line."""
    line = [land_lease.dashed(e["story"]) for e in record.get("line", [])]
    story = land_lease.dashed(story)
    if story not in line:
        return None
    holder = [land_lease.dashed(record["story"])] if record.get("story") else []
    return holder + line[:line.index(story)]


def live_inputs(story: str, record: dict) -> tuple[str, list[str]]:
    """(fingerprint of the train's inputs, the stories ahead): the `development` tip, each story ahead with its
    tip, and the own tip."""
    ahead = ahead_of(story, record) or []
    own = roots(story)
    parts = [f"{story_ledger.base_branch()} {development_tip(own.worktree)}"]
    parts += [f"{other} {story_tip(other)}" for other in ahead]
    parts.append(f"{land_lease.dashed(story)} {story_tip(story)}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest(), ahead


def _set(lease_path, story: str, train: dict | None) -> None:
    land_lease.set_train(lease_path, story, train)


def _train_dict(tip: str, state: str, inputs: str | None = None, tested: str | None = None,
                gating_seen: str | None = None) -> dict:
    train = {"tip": tip, "state": state}
    if inputs:
        train["inputs"] = inputs
    if tested:
        train["tested"] = tested
    if gating_seen:
        train["gating_seen"] = gating_seen
    return train


def gating_stale(train: dict) -> bool:
    """True when no gate is keeping a `gating` train alive: its `gating_seen` stamp is missing, unreadable or
    older than two heartbeats."""
    seen = land_lease.seconds_since(train.get("gating_seen"))
    return seen is None or seen > 2 * land_lease.HEARTBEAT_SECONDS


def reset_train(story: str, lease_path) -> None:
    """Rewrite the story's train as `none` with no `inputs` (its `tested` kept), so the next `advance` rebuilds it."""
    train = _train_of(land_lease.read_record(lease_path), story)
    if train:
        _set(lease_path, story, _train_dict(train["tip"], "none", tested=train.get("tested")))


def clear_gating(story: str, lease_path) -> None:
    """Reset the story's `gating` state when its stamp is stale (a dead run's); a fresh stamp is a gate that is
    still running, and is left alone."""
    train = _train_of(land_lease.read_record(lease_path), story)
    if train and train["state"] == "gating" and gating_stale(train):
        reset_train(story, lease_path)


def begin_gate(story: str, lease_path) -> list[str]:
    """Mark the story `gating` for its tip and live inputs, with the first `gating_seen` stamp; the stories ahead."""
    record = land_lease.read_record(lease_path)
    inputs, ahead = live_inputs(story, record)
    train = _train_of(record, story) or {}
    _set(lease_path, story, _train_dict(story_tip(story), "gating", inputs, train.get("tested"),
                                        land_lease._now()))
    return ahead


def finish_green(story: str, lease_path) -> None:
    """The train gate passed: keep the tested commit resolvable and mark the story `green` with it."""
    r = roots(story)
    head = _out(r.worktree, "rev-parse", "HEAD")
    _git(r.worktree, "update-ref", f"refs/train-tested/{r.train[len(_train_prefix()):]}", head)
    record = land_lease.read_record(lease_path)
    train = _train_of(record, story) or {}
    inputs = train.get("inputs") or live_inputs(story, record)[0]
    _set(lease_path, story, _train_dict(story_tip(story), "green", inputs, head))


def mark_holder(story: str, worktree, state: str) -> None:
    """The window's own gate marks the lease holder `gating` / `green` for `HEAD`; `gating` carries the first
    `gating_seen` stamp (the heartbeat's first beat comes one interval later), `green` none. A story that is not
    the holder, or a record that cannot be written, is left alone (the gate's exit code decides the rest)."""
    try:
        path = land_lease.default_path()
        holder = land_lease.read(path)
        if holder is None or not _same(holder["story"], story):
            return
        _set(path, story, _train_dict(_out(worktree, "rev-parse", "HEAD"), state,
                                      gating_seen=land_lease._now() if state == "gating" else None))
    except (TrainError, land_lease.LeaseError, OSError) as exc:
        print(f"WARNING: the land lease holder {story} could not be marked {state}: {exc}", flush=True)


def withdraw(story: str, rc: int, head: str | None, lease_path) -> bool:
    """A red train gate: withdraw the checkpoint-3 approval and leave the line (release the lease when the
    story holds it). Only a story in the line or holding the lease withdraws; True when it did."""
    record = land_lease.read_record(lease_path)
    holding = bool(record.get("story")) and _same(record["story"], story)
    if not (holding or ahead_of(story, record) is not None):
        return False
    try:
        story_record.cmd_append(story, f"checkpoint-3 approval withdrawn -- train gate exit {rc}"
                                       + (f" HEAD {head}" if head else ""))
        print(f"checkpoint-3 approval for {story} withdrawn (train gate exit {rc}).", flush=True)
    except OSError as exc:
        print(f"WARNING: the train gate exited {rc} but the checkpoint-3 approval for {story} was NOT "
              f"withdrawn: {exc}", flush=True)
    land_lease.leave(lease_path, story)
    if holding:
        land_lease.release(lease_path, story)
    print(f"{story} left the land lease's line.", flush=True)
    return True


@contextlib.contextmanager
def heartbeat(story: str, lease_path):
    """Refresh the story's line entry and its `gating` stamp every `land_lease.HEARTBEAT_SECONDS` while a gate
    runs (a train gate, or the window gate of the lease holder), so the entry (dead after `STALE_SECONDS`
    without a refresh) outlives a gate of any length and a waiter can tell a running gate from a dead one. The
    entry is refreshed, never re-added (`land_lease.refresh_line_entry`): a story that left the line stays out.
    For the holder the beat writes only while its train is `gating`."""
    interval = land_lease.HEARTBEAT_SECONDS
    stop = threading.Event()

    def beat():
        while not stop.wait(interval):
            try:
                land_lease.refresh_line_entry(lease_path, story)
                land_lease.refresh_gating(lease_path, story)
            except (land_lease.LeaseError, OSError):
                pass

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)


# ---------------------------------------------------------------- the gate turn and the train checks
def _turn_blocker(record: dict, ahead: list[str]) -> str | None:
    for other in ahead:
        train = _train_of(record, other)
        if not train or train["state"] != "green":
            return f"{other} has no green train yet (train {train['state'] if train else 'absent'})"
        try:
            tip = story_tip(other)
        except TrainError as exc:
            return f"{other}'s branch cannot be read: {exc}"
        if train["tip"] != tip:
            return f"{other}'s green train is for an older tip of its branch"
    return None


def not_run_reason(story: str, worktree, expected_branch: str) -> str | None:
    """Why a `--train` gate must not run now (the caller exits 75 with `train gate not run: <reason>`), else None."""
    current = worktree_branch(worktree)
    if current != expected_branch:
        return f"the worktree is on {current or 'a detached HEAD'}, not {expected_branch}"
    dirty = dirty_gate_inputs(worktree)
    if dirty:
        return f"{dirty[0]} is dirty or untracked" + (f" (+{len(dirty) - 1} more)" if len(dirty) > 1 else "")
    record = land_lease.read_record(land_lease.default_path())
    ahead = ahead_of(story, record)
    if ahead is None:
        return f"{story} is not in the land lease's line"
    blocker = _turn_blocker(record, ahead)
    if blocker:
        return f"it is not {story}'s gate turn: {blocker}"
    train = _train_of(record, story)
    if not train or train.get("inputs") != live_inputs(story, record)[0]:
        return ("the train is stale: its recorded inputs differ from the live inputs "
                "(the base branch or a story ahead moved)")
    return None


# ---------------------------------------------------------------- the build
def _exempt(r: Roots) -> bool:
    _path, lines = story_ledger.read_record(r.variants, r.arts)
    for line in reversed(lines):
        fact = story_ledger._fact_of(line).lower()
        if fact.startswith("pre-merge gate"):
            return "not required" in fact
    return False


def _record_file(r: Roots) -> str | None:
    for v in r.variants:
        candidate = os.path.join(r.arts, f"{v}-record.md")
        if os.path.isfile(candidate):
            return os.path.relpath(candidate, r.worktree).replace(os.sep, "/")
    return None


def _checkout_story(r: Roots) -> None:
    _git(r.worktree, "checkout", "-q", r.branch)


def build_train(story: str, lease_path) -> Built:
    """Build the train branch for a story waiting in the line, or refuse (Refused) and build nothing."""
    story = land_lease.dashed(story)
    record = land_lease.read_record(lease_path)
    if record.get("story") and _same(record["story"], story):
        raise Refused(f"{story} holds the land lease; a lease holder builds no train")
    ahead = ahead_of(story, record)
    if ahead is None:
        raise Refused(f"{story} is not in the land lease's line")
    approved, reason = land_lease.approval(story)
    if approved is None:
        raise Refused(f"the checkpoint-3 approval for {story} does not stand ({reason})")
    r = roots(story)
    dirty = dirty_gate_inputs(r.worktree)
    if dirty:
        raise Refused(f"the worktree has a dirty or untracked path that is not gate-inert: {', '.join(dirty[:5])}")
    own = _train_of(record, story) or {}
    if own.get("state") == "gating":
        raise Refused(f"{story}'s own train state is gating; a gate is running or was left by a dead run")
    tips = {}
    for other in ahead:
        try:
            tips[other] = (roots(other), story_tip(other))
        except TrainError as exc:
            raise Refused(f"the branch of {other}, ahead of {story}, cannot be resolved: {exc}") from exc
    tip = story_tip(story)
    if _exempt(r):
        _set(lease_path, story, _train_dict(tip, "green"))
        return Built("exempt", f"{story}'s pre-merge verdict is `not required`: no train, marked green")
    inputs, _ = live_inputs(story, record)
    tested = own.get("tested")
    dev = development_tip(r.worktree)

    def fallback(why: str) -> Built:
        _checkout_story(r)
        _set(lease_path, story, _train_dict(tip, "none", inputs, tested))
        return Built("fallback", f"no train for {story} this round: {why}; it gates in its land window")

    try:
        rel = _record_file(r)
        if rel and rel in _paths(_git(r.worktree, "diff", "--cached", "--name-only", "-z").stdout):
            _git(r.worktree, "restore", "--staged", "--", rel)
        _git(r.worktree, "checkout", "-q", "-B", r.train, r.branch)
        bb = story_ledger.base_branch()
        steps = [(f"train {story}: merge {bb} {dev[:12]}", bb)]
        steps += [(f"train {story}: merge {roots(other).branch} {sha[:12]}", sha) for other, (_, sha) in tips.items()]
        for message, target in steps:
            merged = _git(r.worktree, "merge", "--no-ff", "-m", message, target, ok=(0, 1, 128))
            if merged.returncode != 0:
                conflicted = _paths(_git(r.worktree, "diff", "--name-only", "--diff-filter=U", "-z").stdout)
                _git(r.worktree, "merge", "--abort", ok=(0, 1, 128))
                detail = ", ".join(conflicted) or (merged.stderr or merged.stdout).strip()
                return fallback(f"the merge of {target[:12]} stopped: {detail}")
        stack = _stack_affecting(_gate_input_changes(r.worktree, r.branch, r.train))
        if stack:
            return fallback(f"the train changes the stack-affecting path {stack}")
        if tested and _git(r.worktree, "rev-parse", "--verify", "-q", f"{tested}^{{commit}}", ok=(0, 1)).returncode == 0 \
                and not _gate_input_changes(r.worktree, tested, r.train):
            _checkout_story(r)
            _set(lease_path, story, _train_dict(tip, "green", inputs, tested))
            return Built("carried", f"the train for {story} differs from its tested train in no gate input: carried over")
        _set(lease_path, story, _train_dict(tip, "none", inputs, tested))
        return Built("built", f"built {r.train} for {story} on {', '.join(ahead)} and "
                              f"{story_ledger.base_branch()} {dev[:12]}")
    except TrainError:
        with contextlib.suppress(TrainError):
            _git(r.worktree, "merge", "--abort", ok=(0, 1, 128))
        with contextlib.suppress(TrainError):
            _checkout_story(r)
        raise


def _append_marker(out_path: Path, line: str) -> str:
    """Append `line` as its own line of the gate's output file; the file's text."""
    text = out_path.read_text(encoding="utf-8", errors="replace")
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(("\n" if text and not text.endswith("\n") else "") + line + "\n")
    return out_path.read_text(encoding="utf-8", errors="replace")


def _kill_and_drain(proc) -> str:
    """Kill `proc`'s process tree, then wait `KILL_DRAIN_SECONDS` for it to end. The notes for the marker line: what
    `_kill_tree` reported, then that the child was still alive after the wait, joined by '; '; '' when neither."""
    notes = run_scoped_tests._kill_tree(proc)
    try:
        proc.wait(timeout=run_scoped_tests.KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        notes += ("; " if notes else "") + (
            f"the child was still alive {run_scoped_tests.KILL_DRAIN_SECONDS} s after the kill")
    return notes


def run_gate(story: str, out_path) -> tuple[int, str]:
    """Run the train gate as a child process; its stdout goes to `out_path` for diagnosis. (exit code, output).

    The child has `TRAIN_GATE_BOUND_MINUTES` to end. Past it the whole process tree is killed, the story stack's
    `backend` and `frontend` containers are restarted and waited for (`restart_stack`), and the result is
    `(NOT_RUN_EXIT, <output + a `train gate not run:` line>)`. A child that ended non-zero while `GATE_ABORT` is
    set gets the same result after a bare restart; one that exited 0 is green whatever the event says. With
    `GATE_ABORT` already set no child starts and nothing is restarted: the result is `NOT_RUN_EXIT` and the marker
    alone. A child spawned as it is set is killed at once and ends as one that exited non-zero; its marker line
    also names a failed tree kill and a child still alive after the drain, as the bound's does. An interrupt
    kills the tree before it propagates."""
    main_repo = wfconfig.repo_root()
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "run_full_suite.py"),
           "--story-id", story, "--train", "--record"]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if GATE_ABORT.is_set():
        text = NOT_RUN_ABORTED + "\n"
        out_path.write_text(text, encoding="utf-8")
        return NOT_RUN_EXIT, text
    expired, abort_kill = None, ""
    with out_path.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=main_repo,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            if GATE_ABORT.is_set():  # the brake's one kill pass cannot see a child spawned after its snapshot
                abort_kill = _kill_and_drain(proc)
                rc = NOT_RUN_EXIT
            else:
                rc = proc.wait(timeout=TRAIN_GATE_BOUND_MINUTES * 60)
        except subprocess.TimeoutExpired:
            expired = _kill_and_drain(proc)
            rc = NOT_RUN_EXIT
        except BaseException:
            run_scoped_tests._kill_tree(proc)
            raise
    if expired is not None:
        line = (f"{NOT_RUN_MARKER} the train gate passed its {TRAIN_GATE_BOUND_MINUTES}-minute wall-clock bound; "
                "its process tree was killed" + (f"; {expired}" if expired else ""))
        cause = restart_stack(story, True)
    elif rc != 0 and GATE_ABORT.is_set():
        line = NOT_RUN_ABORTED + (f"; {abort_kill}" if abort_kill else "")
        cause = restart_stack(story, False)
    else:
        return rc, out_path.read_text(encoding="utf-8", errors="replace")
    return NOT_RUN_EXIT, _append_marker(out_path, line + (f"; {cause}" if cause else ""))


def _compose(cmd: list[str], label: str, bound: float, env: dict, cwd: str) -> str:
    """Run one compose command within `bound` seconds; '' on exit 0, else the cause."""
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out:
        try:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=cwd, env=env)
        except OSError as exc:
            return f"{label} could not run: {exc}"
        try:
            rc = proc.wait(timeout=bound)
        except subprocess.TimeoutExpired:
            run_scoped_tests._kill_tree(proc)
            return f"{label} did not finish within {bound:g} s"
        except BaseException:
            run_scoped_tests._kill_tree(proc)
            raise
        if rc == 0:
            return ""
        out.seek(0)
        tail = " | ".join(ln.strip() for ln in out.read().splitlines()[-5:] if ln.strip())
        return f"{label} exited {rc}" + (f": {tail}" if tail else "")


def restart_stack(story: str, health_wait: bool) -> str:
    """Restart the slot stack's `config: land.restart_services` (default `config: stack.services`), which ends the
    test commands a killed gate left running in them, and with `health_wait` wait until they are healthy again.
    '' on success or when the stack is disabled (`stack.runtime = "none"`), else the cause."""
    if not wfconfig.stack_enabled():
        return ""
    services = [str(s) for s in (wfconfig.get("land.restart_services", []) or wfconfig.get("stack.services", []) or [])]
    if not services:
        return ""
    row = _registry_lookup.find(story_ledger.variants(story))
    if not row or not str(row.get("slot") or "").strip().isdigit():
        return f"cannot restart the containers of {story}: no slot in the slot registry"
    project = wfconfig.stack_name(land_lease.dashed(story))
    env = wfconfig.compose_env(int(str(row["slot"]).strip()))
    tree = str(Path(str(row.get("worktree_path") or wfconfig.worktree_path(story))) /
               str(wfconfig.get("stack.compose_dir", ".")))
    base = [*str(wfconfig.get("stack.compose_cmd", "docker compose")).split(), "-p", project]
    cause = _compose(base + ["restart", *services], "compose restart", RESTART_BOUND_SECONDS, env, tree)
    if cause or not health_wait:
        return cause
    return _compose(base + ["up", "-d", "--no-recreate", "--no-deps", "--wait", "--wait-timeout",
                            str(HEALTH_WAIT_SECONDS), *services], "compose up --wait",
                    HEALTH_WAIT_SECONDS + run_scoped_tests.KILL_DRAIN_SECONDS, env, tree)


def holder_stall_exit(record: dict, lease_path, budget_minutes: float) -> bool:
    """True when a waiter that found `record`'s holder should take the stall exit: the holder has held the lease
    longer than `budget_minutes`, or its `acquired` cannot be read. A holder whose gate looks dead (its train is
    `gating` with a `gating_stale` stamp) is given the recovery window first, so `advance`'s dead-holder step
    can free the lease: `land_lease.STALE_SECONDS + 3 * land_lease.HEARTBEAT_SECONDS` on this process's
    `time.monotonic()`, one window per holder, started at its first poll as a candidate past the budget and never
    restarted by a stamp change (a live gate that beats irregularly still reaches the exit)."""
    key = (str(lease_path), land_lease.dashed(record["story"]))
    if key not in _STALL_WINDOWS:
        _STALL_WINDOWS.clear()
    held = land_lease.seconds_since(record.get("acquired"))
    if held is not None and held <= budget_minutes * 60:
        return False
    train = record.get("train")
    if not (train and train.get("state") == "gating" and gating_stale(train)):
        return True
    window = land_lease.STALE_SECONDS + 3 * land_lease.HEARTBEAT_SECONDS
    return time.monotonic() - _STALL_WINDOWS.setdefault(key, time.monotonic()) >= window


def _home(story: str) -> None:
    r = roots(story)
    if worktree_branch(r.worktree) != r.branch:
        _checkout_story(r)
        print(f"{story}: checked out {r.branch}")


def home(story: str) -> int:
    """Check out the story branch in the story worktree when it is on another branch (0), 2 on an error."""
    try:
        _home(story)
        return 0
    except TrainError as exc:
        print(f"merge train: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _gate_passed(holder: str, tip: str) -> bool:
    """True when the holder's latest `pre-merge gate` record line is a GREEN whose `HEAD <sha>` is a prefix of
    `tip`. A holder whose record cannot be resolved is not provably dead: TrainError propagates to the caller."""
    r = roots(holder)
    _path, lines = story_ledger.read_record(r.variants, r.arts)
    latest = next((ln for ln in reversed(lines)
                   if story_ledger._fact_of(ln).lower().startswith("pre-merge gate")), None)
    if latest is None or "green" not in latest.lower():
        return False
    match = story_ledger._PREMERGE_HEAD.search(latest)
    return bool(match) and tip.startswith(match.group(1))


def _recover_dead_holder(record: dict, lease_path) -> Step | None:
    """Free the lease of a holder whose gate died. The holder's train is `gating`, its `gating_seen` stamp is
    missing, unreadable or older than `land_lease.STALE_SECONDS`, its gate did not pass for the train's tip, and
    this process saw the same stale stamp at least two heartbeats earlier (a host that has just woken holds
    stamps of any age while every gate is alive). The release is conditional on that stamp, so only one
    detector writes; that detector withdraws the approval."""
    holder, train = record.get("story"), record.get("train")
    if not holder or not train or train["state"] != "gating":
        return None
    seen = train.get("gating_seen")
    age = land_lease.seconds_since(seen)
    if age is not None and age <= land_lease.STALE_SECONDS:
        return None
    key = (str(lease_path), land_lease.dashed(holder), seen)
    now = time.monotonic()
    if key not in _SIGHTINGS:
        _SIGHTINGS.clear()
        _SIGHTINGS[key] = now
        return None
    if now - _SIGHTINGS[key] < 2 * land_lease.HEARTBEAT_SECONDS:
        return None
    try:
        if _gate_passed(holder, train["tip"]):
            return None
    except TrainError:
        return None
    if not land_lease.release_dead_gate(lease_path, holder, seen):
        return None
    _SIGHTINGS.clear()
    since = seen or "none"
    line = f"checkpoint-3 approval withdrawn -- pre-merge gate dead: no heartbeat since {since} HEAD {train['tip'][:12]}"
    withdrawn = False
    try:
        if land_lease.approval(holder)[0] is not None:
            if story_record.cmd_append(holder, line) == 0:
                withdrawn = True
            else:
                print(f"WARNING: {holder}'s dead gate was recovered but the withdrawal was NOT recorded; "
                      f"append by hand: {line}", flush=True)
    except OSError as exc:
        print(f"WARNING: {holder}'s dead gate was recovered but the withdrawal was NOT recorded ({exc}); "
              f"append by hand: {line}", flush=True)
    return Step("recovered", f"recovered {holder}: its gate died (no heartbeat since {since}); lease released"
                             + ("; checkpoint-3 approval withdrawn" if withdrawn else ""))


def _join_line(lease_path, story: str) -> None:
    """`land_lease.join`, skipped once `GATE_ABORT` is set: after the driver's immediate stop its run joins no line."""
    if not GATE_ABORT.is_set():
        land_lease.join(lease_path, story)


def advance(story: str, lease_path) -> Step:
    """One poll step of a story waiting in the line: a dead gate holder's recovery, the gate turn, then a build
    and, when the build needs one, the train gate. See the module docstring."""
    story = land_lease.dashed(story)
    record = land_lease.read_record(lease_path)
    if record.get("story") and _same(record["story"], story):
        return Step("holder", f"{story} holds the lease")
    ahead = ahead_of(story, record)
    if ahead is None:
        return Step("absent", f"{story} is not in the line")
    recovered = _recover_dead_holder(record, lease_path)
    if recovered:
        return recovered
    if not ahead:
        return Step("idle", "nothing ahead: no train")
    blocker = _turn_blocker(record, ahead)
    if blocker:
        return Step("waiting", f"waiting for {blocker}")
    own = _train_of(record, story) or {}
    if own.get("state") == "gating":
        if not gating_stale(own):
            return Step("gating", f"{story}'s own train gate is running")
        clear_gating(story, lease_path)
        own = _train_of(land_lease.read_record(lease_path), story) or {}
    inputs, _ = live_inputs(story, record)
    if own.get("inputs") == inputs or (own.get("state") == "green" and not own.get("inputs")):
        return Step("current", f"{story}'s train is current (train {own.get('state')})")
    _join_line(lease_path, story)
    try:
        built = build_train(story, lease_path)
    except Refused as exc:
        return Step("refused", f"no train built: {exc}")
    _join_line(lease_path, story)
    if built.kind != "built":
        return Step(built.kind, built.text)
    try:
        step = _train_gate(story, lease_path)
    except BaseException:
        with contextlib.suppress(TrainError):
            _home(story)
        raise
    _home(story)
    return step


def _withdrawn_by_gate(story: str) -> bool:
    """True when the story's latest checkpoint-3 verdict is a train-gate withdrawal (the gate process wrote it)."""
    approved, reason = land_lease.approval(story)
    return approved is None and "approval withdrawn -- train gate exit" in reason


def _train_gate(story: str, lease_path) -> Step:
    """Run the train gate of the story whose train was just built and read its outcome."""
    r = roots(story)
    print(f"{_hms()} running the train gate for {story} on {r.train}", flush=True)
    out = wfconfig.state_dir("story-setup") / f"train-gate-{land_lease.dashed(story)}.txt"
    rc, text = run_gate(story, out)
    head = _out(r.worktree, "rev-parse", "--short=12", "HEAD")
    if rc == 0:
        _join_line(lease_path, story)
        return Step("green", f"train gate green for {story}")
    if rc == NOT_RUN_EXIT and NOT_RUN_MARKER in text:
        if _withdrawn_by_gate(story):
            land_lease.leave(lease_path, story)
            return Step("withdrawn", f"train gate for {story} withdrew the approval before it was stopped")
        reason = next((ln.strip() for ln in text.splitlines() if NOT_RUN_MARKER in ln), NOT_RUN_MARKER)
        _set(lease_path, story, _train_dict(story_tip(story), "none", tested=(_train_of(
            land_lease.read_record(lease_path), story) or {}).get("tested")))
        return Step("not-run", reason)
    if withdraw(story, rc, head, lease_path) or _withdrawn_by_gate(story):
        return Step("withdrawn", f"train gate exit {rc} withdrew the approval of {story}")
    return Step("failed", f"train gate exit {rc} withdrew nothing: {story} was not in the line and its approval stands")


# ---------------------------------------------------------------- the commands
def wait(story: str, holder_stall_min: float, lease_path=None, heartbeat_seconds: float | None = None) -> int:
    story = land_lease.dashed(story)
    interval = land_lease.HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    queued, code = False, EXIT_ERROR
    free_head, free_since = None, 0.0
    try:
        path = Path(lease_path) if lease_path else land_lease.default_path()
        approved, reason = land_lease.approval(story)
        if approved is None:
            print(f"merge train: {story} not waiting -- missing fact: checkpoint-3 merge approved ({reason})",
                  file=sys.stderr)
            code = EXIT_REFUSED
            return EXIT_REFUSED
        clear_gating(story, path)
        started = time.monotonic()
        while True:
            outcome, record = land_lease.take(path, story)
            if outcome in ("acquired", "already"):
                code = 0
                print(f"merge train: {story} {'took' if outcome == 'acquired' else 'already holds'} the land lease")
                return 0
            queued = True  # an entry left by an earlier run may still be live: every exit from here leaves the line
            if record.get("story"):
                free_head = None
                if holder_stall_exit(record, path, holder_stall_min):
                    held_for = land_lease.seconds_since(record["acquired"])
                    code = EXIT_HELD
                    print(f"merge train: the land lease holder {record['story']} has held it for "
                          f"{land_lease.age(record)}, past the {holder_stall_min:g}-minute budget"
                          f"{' (its acquired timestamp cannot be read)' if held_for is None else ''}; "
                          f"{story} leaves the line", file=sys.stderr)
                    return EXIT_HELD
            else:
                head = land_lease.dashed(record["line"][0]["story"])
                now = time.monotonic()
                if head != free_head:
                    free_head, free_since = head, now
                elif now - free_since > holder_stall_min * 60:
                    code = EXIT_HELD
                    print(f"merge train: the land lease has stood free for {land_lease.duration(now - free_since)} "
                          f"behind {head}, the head of the line, past the {holder_stall_min:g}-minute budget; "
                          f"{story} leaves the line", file=sys.stderr)
                    return EXIT_HELD
            position, record = land_lease.join(path, story)
            holder = record.get("story")
            step = advance(story, path)
            if step.kind == "withdrawn":
                queued, code = False, EXIT_WITHDRAWN
                print(step.text, file=sys.stderr)
                return EXIT_WITHDRAWN
            state = (_train_of(land_lease.read_record(path), story) or {}).get("state", "absent")
            held = f"held by {holder} for {land_lease.age(record)}" if holder else "free"
            print(f"[{_hms()}] waiting for the land lease: {held}; {story} is position {position} in the line; "
                  f"train {state}; {step.text}; {land_lease.duration(time.monotonic() - started)} waited", flush=True)
            if step.kind not in WORKED:
                time.sleep(interval)
    except (land_lease.LeaseError, TrainError, OSError) as exc:
        print(f"merge train: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if queued:
            with contextlib.suppress(land_lease.LeaseError, OSError):
                land_lease.leave(path, story)
        if code != EXIT_REFUSED:
            home(story)


def settle(story: str, lease_path=None) -> int:
    """Mark the lease holder `green` for `HEAD` when the land guard passes its verdict (0); 1 when it blocks."""
    story = land_lease.dashed(story)
    try:
        path = Path(lease_path) if lease_path else land_lease.default_path()
        holder = land_lease.read(path)
        if holder is None or not _same(holder["story"], story):
            print(f"merge train: {story} does not hold the land lease; settle runs in its land window",
                  file=sys.stderr)
            return EXIT_ERROR
        r = roots(story)
        _path, lines = story_ledger.read_record(r.variants, r.arts)
        passed, reason = story_ledger.premerge_verdict(lines, r.arts, r.variants[0])
        if passed is None:
            print(f"merge train: the pre-merge verdict does not carry over: {reason}")
            return EXIT_REFUSED
        _set(path, story, _train_dict(_out(r.worktree, "rev-parse", "HEAD"), "green"))
        print(f"merge train: {story} is green for HEAD: {passed}")
        return 0
    except (land_lease.LeaseError, TrainError, OSError) as exc:
        print(f"merge train: {exc}", file=sys.stderr)
        return EXIT_ERROR


def delete_train(repo, branch: str) -> list[str]:
    """Delete the train branch and `refs/train-tested/<suffix>` of `branch` (with or without the story prefix);
    each absent one is a no-op. Returns the refs deleted."""
    prefix, tp = _story_prefix(), _train_prefix()
    suffix = branch[len(prefix):] if branch.startswith(prefix) else branch
    deleted = []
    for ref, delete in ((f"refs/heads/{tp}{suffix}", ("branch", "-D", f"{tp}{suffix}")),
                        (f"refs/train-tested/{suffix}", ("update-ref", "-d", f"refs/train-tested/{suffix}"))):
        if _git(repo, "show-ref", "--verify", "--quiet", ref, ok=(0, 1)).returncode == 0:
            _git(repo, *delete)
            deleted.append(ref)
    return deleted


def status(lease_path) -> int:
    record = land_lease.read_record(lease_path)
    entries = ([(record["story"], record.get("train"), "holder")] if record.get("story") else [])
    entries += [(e["story"], e.get("train"), f"position {i}") for i, e in enumerate(record.get("line", []), 1)]
    if not entries:
        print("free; the line is empty")
    for story, train, where in entries:
        detail = "no train" if not train else (
            f"train {train['state']}, tip {train['tip'][:12]}"
            + (f", tested {train['tested'][:12]}" if train.get("tested") else ""))
        print(f"{story} ({where}): {detail}")
    return 0


def build_command(story: str, lease_path) -> int:
    try:
        built = build_train(story, lease_path)
    except Refused as exc:
        print(f"merge train: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if built.kind == "built":
        tested = (_train_of(land_lease.read_record(lease_path), story) or {}).get("tested")
        _set(lease_path, story, _train_dict(story_tip(story), "none", tested=tested))
        _checkout_story(roots(story))
    print(f"merge train: {built.kind}: {built.text}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--lease-path", help="the lease record (default: .workflow/state/land-lease.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wait", help="wait in the line for the lease, building and gating the train meanwhile")
    w.add_argument("story_id")
    w.add_argument("--holder-stall-min", type=float, default=STALL_BUDGET_MINUTES,
                   help="exit 3 once the lease holder has held it, or the lease has stood free behind the same "
                        f"head of the line, this long (default {STALL_BUDGET_MINUTES})")
    for name, text in (("build", "build the story's train once"), ("settle", "mark the holder green for HEAD"),
                       ("home", "check out the story branch in the story worktree")):
        sub.add_parser(name, help=text).add_argument("story_id")
    sub.add_parser("status", help="print each story's train state")
    args = ap.parse_args(argv)
    try:
        path = Path(args.lease_path) if args.lease_path else land_lease.default_path()
        if args.cmd == "wait":
            return wait(args.story_id, args.holder_stall_min, path)
        if args.cmd == "build":
            return build_command(args.story_id, path)
        if args.cmd == "settle":
            return settle(args.story_id, path)
        if args.cmd == "home":
            return home(args.story_id)
        return status(path)
    except (land_lease.LeaseError, TrainError, OSError) as exc:
        print(f"merge train: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        print(f"merge train: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
