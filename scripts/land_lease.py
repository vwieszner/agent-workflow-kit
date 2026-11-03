#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The land lease: one story at a time holds the stretch from its final rebase onto the base branch to its merge.

The lease is one host-global record, `.workflow/state/land-lease.json` in the main checkout (git-ignored),
shared by every slot and worktree:

    {"story": "<id in dashed form>", "acquired": "<UTC ISO-8601>",
     "line": [{"story": "<id>", "joined": "<UTC ISO-8601>", "seen": "<UTC ISO-8601>"}, ...]}

`story` and `acquired` are the holder, present while the lease is held; `line` holds the waiting stories in
arrival order and is present only while non-empty. No file means free with an empty line; the lease has no
expiry. Every read and every read-modify-write of the record runs under the named lock `land-lease`
(`_named_mutex.py`), held for that one access only and never across a queue wait; writes go to a temp file in
the same directory and are moved into place with `os.replace`. An unreadable or malformed record, a lock
timeout or an OSError is exit 2 with the cause on stderr -- never "free".

The line serves waiters first-come-first-served. A waiter refreshes its entry's `seen` on every poll
(`HEARTBEAT_SECONDS`); an entry whose `seen` is older than `STALE_SECONDS` (ten polls) is dead, and every
locked decision ignores dead entries (every locked write drops them). A refresher that does not own the entry
(the train gate's heartbeat) calls `refresh_line_entry`: it rewrites `seen` of an entry still in the line and
never adds one, so it cannot undo a `leave`. The turn rule, in one locked attempt (`take`): the story holds the
lease (`already`); another story holds it (`held`); the lease is free but another live story heads the line
(`behind`); the lease is free and the line is empty or headed by this story (`acquired`, its entry leaving the
line in the same write). `release` clears the holder and keeps the line, and never hands the lease over: the
head takes it on its own next poll.

The holder and every line entry may carry one `train` object, the merge train's state for the story
(`merge_train.py`): `{"tip": <40 hex>, "state": "gating" | "green" | "none", "inputs": <non-empty string>,
"tested": <40 hex>, "gating_seen": <non-empty string>}`, `inputs`, `tested` and `gating_seen` optional.
`join`, `leave` and `release` keep the `train` of every story they leave in the record, `take` composes a fresh
holder with none, and `set_train` is the one writer of the key. `refresh_gating` stamps `gating_seen` only while
the state is still `gating`, so a late beat never overwrites a `green`. `release_dead_gate` frees the lease only
while the holder is still that story with a `gating` train carrying the stamp the caller judged dead, and keeps
the line. A malformed `train` is exit 2. `read()` returns `{story, acquired}` only; `read_record()` also returns
`train`.

    uv run --no-project .workflow/scripts/land_lease.py acquire <story-id> [--wait-minutes N]
    uv run --no-project .workflow/scripts/land_lease.py release <story-id>
    uv run --no-project .workflow/scripts/land_lease.py holds   <story-id>
    uv run --no-project .workflow/scripts/land_lease.py status

  acquire   refused (exit 1) unless the story's latest `checkpoint-3` verdict is an approval
            (`story_ledger.merge_approval`). Its turn -> taken (0); already held by the story -> 0; held by
            another story -> exit 3 naming the holder and its age; free but other stories ahead in the line ->
            exit 3 naming the head and the count. By default (`--wait-minutes 0`) one attempt, never joining
            the line. With `--wait-minutes N` > 0 it joins the line, prints a timestamped heartbeat every 30 s,
            takes the lease at its turn, and leaves the line when the budget is spent (exit 3) and on every
            other exit. It never proceeds without the lease.
  release   holder -> free (0), the line kept; already free -> 0; held by another story -> 1, unchanged.
            A lease left by a dead session is released with the holder's id, on the owner's instruction
            (a dead window gate's lease is released by a waiter in the line: `release_dead_gate`).
  holds     0 when the story holds the lease; 1 otherwise (prints the holder, or `free`); 2 on error.
  status    the holder and its age, or `free`, then one line per waiting story; 2 on error.

Importable: `run_full_suite.py` withdraws and releases in-process, the land-lease wait hook reads and releases
the lease, the phase-dispatch guard runs `holds` as a subprocess, `merge_train.py` queues, takes and releases.
`--lease-path` overrides the default record path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
from _named_mutex import NamedMutex  # noqa: E402

MUTEX_NAME = "land-lease"
MUTEX_TIMEOUT_MS = 20_000
HEARTBEAT_SECONDS = 30.0
STALE_SECONDS = 300
LEASE_FILE = "land-lease.json"
EXIT_OK, EXIT_REFUSED, EXIT_ERROR, EXIT_HELD = 0, 1, 2, 3
ENTRY_KEYS = ("story", "joined", "seen")
TRAIN_STATES = ("gating", "green", "none")
_SHA40 = re.compile(r"[0-9a-f]{40}")


class LeaseError(RuntimeError):
    """The lease record cannot be read, written or locked."""


def dashed(story_id: str) -> str:
    return story_id.strip().replace(".", "-")


def default_path() -> Path:
    try:
        return wfconfig.state_dir() / LEASE_FILE
    except OSError as exc:
        raise LeaseError(str(exc)) from exc


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_since(timestamp) -> float | None:
    """Seconds since a record timestamp, or None when it cannot be read."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return None


def duration(seconds: float | None) -> str:
    """`12s`, `3m05s`, `1h02m`."""
    if seconds is None:
        return "an unknown time"
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def age(lease: dict) -> str:
    """How long the lease has been held, e.g. `12s`, `3m05s`, `1h02m`."""
    return duration(seconds_since(lease.get("acquired")))


def _guarded(timeout_ms: int | None, body):
    """Run `body` under the lease mutex; a timeout or an OS error becomes a LeaseError."""
    try:
        with NamedMutex(MUTEX_NAME, MUTEX_TIMEOUT_MS if timeout_ms is None else timeout_ms):
            return body()
    except TimeoutError as exc:
        raise LeaseError(f"land lease mutex: {exc}") from exc
    except OSError as exc:
        raise LeaseError(f"land lease record: {exc}") from exc


def _check_train(value) -> dict:
    """The validated `train` object (known keys only); ValueError names the first defect."""
    if not isinstance(value, dict):
        raise ValueError("train to be an object")
    if value.get("state") not in TRAIN_STATES:
        raise ValueError(f"train state to be one of {', '.join(TRAIN_STATES)}")
    for key in ("tip", "tested"):
        if (key == "tip" or key in value) and not (isinstance(value.get(key), str) and _SHA40.fullmatch(value[key])):
            raise ValueError(f"train {key} to be 40 hex characters")
    if "inputs" in value and not (isinstance(value["inputs"], str) and value["inputs"]):
        raise ValueError("train inputs to be a non-empty string")
    if "gating_seen" in value and not (isinstance(value["gating_seen"], str) and value["gating_seen"]):
        raise ValueError("train gating_seen to be a non-empty string")
    return {k: value[k] for k in ("tip", "state", "inputs", "tested", "gating_seen") if k in value}


def _load_unlocked(path) -> dict:
    """The record as stored: `story` and `acquired` when held, `line` when non-empty; {} when there is no file."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LeaseError(f"land lease record {path} is not valid JSON: {exc}") from exc

    def malformed(expected: str) -> LeaseError:
        return LeaseError(f"land lease record {path} is malformed (expected {expected}): {raw[:200]!r}")

    if not isinstance(data, dict):
        raise malformed("a JSON object")
    def train_of(owner: dict) -> dict:
        try:
            return {"train": _check_train(owner["train"])} if "train" in owner else {}
        except ValueError as exc:
            raise malformed(str(exc)) from exc

    record: dict = {}
    if "story" in data or "acquired" in data:
        if not (isinstance(data.get("story"), str) and data["story"] and isinstance(data.get("acquired"), str)):
            raise malformed("story and acquired strings")
        record["story"], record["acquired"] = data["story"], data["acquired"]
        record.update(train_of(data))
    if "line" in data:
        line = data["line"]
        if not (isinstance(line, list) and line):
            raise malformed("line to be a non-empty list")
        for entry in line:
            if not (isinstance(entry, dict) and all(isinstance(entry.get(k), str) and entry[k] for k in ENTRY_KEYS)):
                raise malformed("every line entry to carry story, joined and seen strings")
        record["line"] = [{**{k: entry[k] for k in ENTRY_KEYS}, **train_of(entry)} for entry in line]
    if not record:
        raise malformed("a holder or a line")
    return record


def _holder(record: dict) -> dict | None:
    """The holder keys of a record, `train` included when it carries one."""
    if "story" not in record:
        return None
    return {k: record[k] for k in ("story", "acquired", "train") if k in record}


def _alive(entry: dict) -> bool:
    seconds = seconds_since(entry["seen"])
    return seconds is not None and seconds <= STALE_SECONDS


def _compose(holder: dict | None, line: list) -> dict:
    record = dict(holder) if holder else {}
    if line:
        record["line"] = line
    return record


def _view_unlocked(path) -> dict:
    """The record with the dead entries left out of the line (nothing is written)."""
    stored = _load_unlocked(path)
    return _compose(_holder(stored), [e for e in stored.get("line", []) if _alive(e)])


def _write_unlocked(path, lease: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".land-lease-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(lease) + "\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _store_unlocked(path, record: dict) -> None:
    """Write the record, or remove the file when the lease is free with an empty line."""
    if record:
        _write_unlocked(path, record)
    else:
        os.remove(path)


def read(path, timeout_ms: int | None = None) -> dict | None:
    """The holder `{story, acquired}`, or None when free. Raises LeaseError on a malformed record or a lock failure."""
    def body():
        holder = _holder(_load_unlocked(path))
        return {"story": holder["story"], "acquired": holder["acquired"]} if holder else None

    return _guarded(timeout_ms, body)


def read_record(path, timeout_ms: int | None = None) -> dict:
    """The whole record: the holder keys when held and the live `line` when non-empty; {} when free with no line."""
    return _guarded(timeout_ms, lambda: _view_unlocked(path))


def take(path, story_id: str, timeout_ms: int | None = None) -> tuple[str, dict]:
    """One locked attempt: ("acquired" | "already" | "held" | "behind", the record now on disk)."""
    story = dashed(story_id)

    def body():
        record = _view_unlocked(path)
        holder, line = _holder(record), record.get("line", [])
        if holder:
            return ("already" if dashed(holder["story"]) == story else "held"), record
        if line and dashed(line[0]["story"]) != story:
            return "behind", record
        record = _compose({"story": story, "acquired": _now()}, [e for e in line if dashed(e["story"]) != story])
        _write_unlocked(path, record)
        return "acquired", record

    return _guarded(timeout_ms, body)


def join(path, story_id: str, timeout_ms: int | None = None) -> tuple[int, dict]:
    """Add the story at the back of the line, or refresh `seen` of its entry (it keeps its place).

    Returns (the story's 1-based position, the record now on disk); position 0 and no write when the story
    holds the lease."""
    story = dashed(story_id)

    def body():
        record = _view_unlocked(path)
        holder, line = _holder(record), record.get("line", [])
        if holder and dashed(holder["story"]) == story:
            return 0, record
        now = _now()
        for entry in line:
            if dashed(entry["story"]) == story:
                entry["seen"] = now
                break
        else:
            line.append({"story": story, "joined": now, "seen": now})
        record = _compose(holder, line)
        _write_unlocked(path, record)
        return next(i for i, e in enumerate(line, 1) if dashed(e["story"]) == story), record

    return _guarded(timeout_ms, body)


def leave(path, story_id: str, timeout_ms: int | None = None) -> bool:
    """Remove the story's entry from the line; True when there was one."""
    story = dashed(story_id)

    def body():
        stored = _load_unlocked(path)
        if not any(dashed(e["story"]) == story for e in stored.get("line", [])):
            return False
        view = _view_unlocked(path)
        _store_unlocked(path, _compose(_holder(view), [e for e in view.get("line", []) if dashed(e["story"]) != story]))
        return True

    return _guarded(timeout_ms, body)


def release(path, story_id: str, timeout_ms: int | None = None) -> tuple[str, dict | None]:
    """One locked release: ("released" | "free" | "held", the holder that was on disk). The line is kept."""
    story = dashed(story_id)

    def body():
        record = _view_unlocked(path)
        holder = _holder(record)
        if holder is None:
            return "free", None
        if dashed(holder["story"]) != story:
            return "held", holder
        _store_unlocked(path, _compose(None, record.get("line", [])))
        return "released", holder

    return _guarded(timeout_ms, body)


def set_train(path, story_id: str, train: dict | None, timeout_ms: int | None = None) -> bool:
    """Set (None: remove) the `train` of the holder or of the story's line entry; True when it wrote.

    False, with no write, for a story that is neither. A malformed `train` raises LeaseError before anything is written."""
    story = dashed(story_id)
    try:
        checked = None if train is None else _check_train(train)
    except ValueError as exc:
        raise LeaseError(f"land lease train for {story} is malformed (expected {exc})") from exc

    def put(owner: dict) -> dict:
        owner = {k: v for k, v in owner.items() if k != "train"}
        return owner if checked is None else {**owner, "train": checked}

    def body():
        record = _view_unlocked(path)
        holder, line = _holder(record), record.get("line", [])
        if holder and dashed(holder["story"]) == story:
            holder = put(holder)
        elif any(dashed(e["story"]) == story for e in line):
            line = [put(e) if dashed(e["story"]) == story else e for e in line]
        else:
            return False
        _write_unlocked(path, _compose(holder, line))
        return True

    return _guarded(timeout_ms, body)


def refresh_gating(path, story_id: str, timeout_ms: int | None = None) -> bool:
    """Stamp `gating_seen` with now on the `train` of the holder or of the story's line entry, only while its
    state is still `gating`; True when it wrote.

    The state check and the write share one mutex hold, so a stamp never overwrites a `green` or `none` written
    in between. False, with no write, for a story that is neither or whose train is not `gating`."""
    story = dashed(story_id)

    def body():
        record = _view_unlocked(path)
        holder, line = _holder(record), record.get("line", [])
        owner = holder if holder and dashed(holder["story"]) == story else next(
            (e for e in line if dashed(e["story"]) == story), None)
        train = owner.get("train") if owner else None
        if not train or train["state"] != "gating":
            return False
        train["gating_seen"] = _now()
        _write_unlocked(path, _compose(holder, line))
        return True

    return _guarded(timeout_ms, body)


def release_dead_gate(path, story_id: str, gating_seen: str | None, timeout_ms: int | None = None) -> bool:
    """Free the lease of a holder whose gate a waiter judged dead; True when it wrote. The line is kept.

    The judgement and the write share one mutex hold: the story must still hold the lease with a `gating` train
    whose `gating_seen` equals the value the caller judged (None for a missing stamp). A gate that restamped, a
    holder that moved on and a second detector that lost the race all return False with nothing written."""
    story = dashed(story_id)

    def body():
        record = _view_unlocked(path)
        holder = _holder(record)
        if holder is None or dashed(holder["story"]) != story:
            return False
        train = holder.get("train")
        if not train or train["state"] != "gating" or train.get("gating_seen") != gating_seen:
            return False
        _store_unlocked(path, _compose(None, record.get("line", [])))
        return True

    return _guarded(timeout_ms, body)


def refresh_line_entry(path, story_id: str, timeout_ms: int | None = None) -> bool:
    """Stamp `seen` with now on the story's line entry; True when it wrote.

    The presence check and the write share one mutex hold, so a refresh never adds back an entry that a `leave`
    removed in between (`join` would). False, with no write, for a story that holds the lease, is not in the line,
    or whose entry is dead."""
    story = dashed(story_id)

    def body():
        record = _view_unlocked(path)
        holder, line = _holder(record), record.get("line", [])
        entry = next((e for e in line if dashed(e["story"]) == story), None)
        if entry is None:
            return False
        entry["seen"] = _now()
        _write_unlocked(path, _compose(holder, line))
        return True

    return _guarded(timeout_ms, body)


def approval(story_id: str) -> tuple[str | None, str]:
    """(approving line, "") when the story's latest checkpoint-3 verdict approves the merge, else (None, reason)."""
    import story_ledger
    vs = story_ledger.variants(story_id)
    arts, _branch = story_ledger.resolve_roots(vs)
    _path, lines = story_ledger.read_record(vs, arts)
    return story_ledger.merge_approval(lines)


def _fail(exc: Exception) -> int:
    print(f"land lease: {exc}", file=sys.stderr)
    return EXIT_ERROR


def _held_text(lease: dict) -> str:
    return f"land lease held by {lease['story']} for {age(lease)}"


def _ahead(record: dict, story: str) -> int:
    """How many live stories are in the line ahead of the story (all of them when it is not in the line)."""
    line = record.get("line", [])
    return next((i for i, e in enumerate(line) if dashed(e["story"]) == story), len(line))


def _refusal_text(record: dict, story: str) -> str:
    holder = _holder(record)
    if holder:
        return f"{_held_text(holder)}; {story} did not acquire it"
    return (f"land lease free but {_ahead(record, story)} stories ahead in the line, "
            f"{record['line'][0]['story']} first")


def _status_text(record: dict) -> str:
    holder = _holder(record)
    return f"held by {holder['story']} for {age(holder)}" if holder else "free"


def cmd_acquire(story_id: str, wait_minutes: float = 0.0, *, path=None,
                heartbeat_seconds: float | None = None) -> int:
    story = dashed(story_id)
    heartbeat = HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    queued = False
    try:
        record_path = Path(path) if path else default_path()
        approved, reason = approval(story_id)
        if approved is None:
            print(f"land lease: {story} not acquired -- missing fact: checkpoint-3 merge approved ({reason})",
                  file=sys.stderr)
            return EXIT_REFUSED
        started = time.monotonic()
        budget = wait_minutes * 60
        while True:
            outcome, record = take(record_path, story)
            if outcome == "acquired":
                queued = False
                print(f"land lease: acquired by {story}")
                return EXIT_OK
            if outcome == "already":
                queued = False
                print(f"land lease: already held by {story}")
                return EXIT_OK
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                print(_refusal_text(record, story), file=sys.stderr)
                return EXIT_HELD
            queued = True
            position, record = join(record_path, story)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] still waiting on the land lease: {_status_text(record)}; "
                  f"position {position} in the line; {int(time.monotonic() - started)}s waited", flush=True)
            time.sleep(min(heartbeat, remaining))
    except (LeaseError, OSError) as exc:
        return _fail(exc)
    finally:
        if queued:
            try:
                leave(record_path, story)
            except (LeaseError, OSError) as exc:
                print(f"land lease: {story} could not leave the line: {exc}", file=sys.stderr)


def cmd_release(story_id: str, *, path=None) -> int:
    story = dashed(story_id)
    try:
        outcome, lease = release(Path(path) if path else default_path(), story)
    except LeaseError as exc:
        return _fail(exc)
    if outcome == "released":
        print(f"land lease: released by {story}")
        return EXIT_OK
    if outcome == "free":
        print("land lease: already free")
        return EXIT_OK
    print(f"land lease: not released -- {_held_text(lease)}", file=sys.stderr)
    return EXIT_REFUSED


def cmd_holds(story_id: str, *, path=None) -> int:
    story = dashed(story_id)
    try:
        lease = read(Path(path) if path else default_path())
    except LeaseError as exc:
        return _fail(exc)
    if lease is None:
        print("free")
        return EXIT_REFUSED
    if dashed(lease["story"]) == story:
        print(f"{story} holds the land lease (held {age(lease)})")
        return EXIT_OK
    print(_held_text(lease))
    return EXIT_REFUSED


def cmd_status(*, path=None) -> int:
    try:
        record = read_record(Path(path) if path else default_path())
    except LeaseError as exc:
        return _fail(exc)
    holder = _holder(record)
    print("free" if holder is None else f"{_held_text(holder)} (acquired {holder['acquired']})")
    for position, entry in enumerate(record.get("line", []), 1):
        print(f"  position {position}: {entry['story']}, in line for {duration(seconds_since(entry['joined']))}")
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--lease-path", help="the lease record (default: .workflow/state/land-lease.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acquire", help="take the lease; exit 3 when another story holds it or is ahead in the line")
    a.add_argument("story_id")
    a.add_argument("--wait-minutes", type=float, default=0.0,
                   help="join the line and wait for the lease up to this many minutes (default 0: one attempt)")
    for name, text in (("release", "free the lease"), ("holds", "exit 0 when the story holds the lease")):
        sub.add_parser(name, help=text).add_argument("story_id")
    sub.add_parser("status", help="print the holder and its age, or free, then the waiters")
    args = ap.parse_args(argv)

    # Exit 1 means "refused" / "not held" to every caller, so an unexpected exception must exit 2.
    try:
        if args.cmd == "acquire":
            return cmd_acquire(args.story_id, args.wait_minutes, path=args.lease_path)
        if args.cmd == "release":
            return cmd_release(args.story_id, path=args.lease_path)
        if args.cmd == "holds":
            return cmd_holds(args.story_id, path=args.lease_path)
        return cmd_status(path=args.lease_path)
    except Exception as exc:
        return _fail(RuntimeError(f"unexpected {type(exc).__name__}: {exc}"))


if __name__ == "__main__":
    sys.exit(main())
