#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Key-addressed query and edit for the sprint-status tracker (`config: paths.sprint_status`).

Every edit is addressed by KEY, never by line number (line numbers go stale on every
insertion). `body` streams the file with the long `last_updated:` note elided, so grep
output over the file stays readable.

Editing is line-wise on purpose: a YAML round-trip would delete every inline `#` note and
reorder the keys.

Row notes are capped at `config: sprint_status.note_max_chars` and carry markers only
(tier, phase, declared deps with state, merge/impl SHAs, slot, flag words). The long form
lives in `config: paths.story_history_dir`/<key>.md: `set` appends the note it replaces
there, `set --history` / `history --append` record detail, `history` prints it.

Valid statuses: `config: sprint_status.statuses` (`--force` bypasses validation).

Usage:
  uv run --no-project .workflow/scripts/sprint_status.py get <key-or-unique-prefix>
  uv run --no-project .workflow/scripts/sprint_status.py set <key> done --note "MERGED (abc1234); slot 2 freed."
  uv run --no-project .workflow/scripts/sprint_status.py set <key> done --note "..." --history "Full close-out ..."
  uv run --no-project .workflow/scripts/sprint_status.py history <key>             # print note history
  uv run --no-project .workflow/scripts/sprint_status.py history <key> --append "Checkpoint 1 decisions: ..."
  uv run --no-project .workflow/scripts/sprint_status.py list --status ready-for-dev in-progress [--json]
  uv run --no-project .workflow/scripts/sprint_status.py touch --note "<key> started (slot 2)."
  uv run --no-project .workflow/scripts/sprint_status.py body | grep -n "<key>"

Global options (before the subcommand): --path <file> (default: config), --history-dir <dir>
(default: config when --path is not given, else a `story-history/` directory beside --path),
--dry-run (print the JSON without writing).

Mutating subcommands print one JSON line on stdout and leave the file untouched on error.
Exit codes: 0 ok; 2 error (JSON {"status":"error","message":...} on stdout).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

# The tracker carries non-ASCII (arrows, em dashes); a cp1252 console would crash `body`.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

DEFAULT_STATUSES = ("backlog", "ready-for-dev", "in-progress", "review", "done", "blocked",
                    "deferred", "optional")
DEFAULT_NOTE_CAP = 240
DEFAULT_HISTORY_DIRNAME = "story-history"
LAST_UPDATED_HISTORY_KEY = "_last-updated"

# Story rows live under `development_status:` at 2-space indent: "  <key>: <status> # <note>"
ROW_RE = re.compile(
    r"^(?P<indent>  )(?P<key>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r":\s*(?P<status>[A-Za-z-]+)"
    r"(?P<rest>\s*#.*)?$"
)
# Anchored so a commented "# last_updated:" line cannot match.
LAST_UPDATED_RE = re.compile(r"^last_updated:\s*(?P<date>\S+)(?P<rest>\s*#.*)?$")


def note_cap() -> int:
    return int(wfconfig.get("sprint_status.note_max_chars", DEFAULT_NOTE_CAP))


def known_statuses() -> tuple[str, ...]:
    return tuple(wfconfig.get("sprint_status.statuses", list(DEFAULT_STATUSES)))


def default_path() -> Path:
    return wfconfig.path("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml")


def default_history_dir() -> Path:
    return wfconfig.path("paths.story_history_dir", "docs/implementation-artifacts/story-history")


def _die(msg: str) -> None:
    print(json.dumps({"status": "error", "message": msg}))
    raise SystemExit(2)


def _read(path: Path) -> list[str]:
    if not path.is_file():
        _die(f"sprint-status file not found: {path}")
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _find_row(lines: list[str], key: str):
    for i, line in enumerate(lines):
        m = ROW_RE.match(line.rstrip("\n"))
        if m and m.group("key") == key:
            return i, m
    return None, None


def _resolve_key(lines: list[str], key: str) -> str:
    """Allow a unique prefix (e.g. '21-15') to stand in for the full story key."""
    exact, _ = _find_row(lines, key)
    if exact is not None:
        return key
    hits = []
    for line in lines:
        m = ROW_RE.match(line.rstrip("\n"))
        if m and m.group("key").startswith(key):
            hits.append(m.group("key"))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        _die(f"no story row matching {key!r}")
    _die(f"{key!r} is ambiguous - matches {len(hits)}: {', '.join(sorted(hits)[:8])}")
    raise AssertionError("unreachable")


def _write(path: Path, lines: list[str], dry_run: bool) -> None:
    if not dry_run:
        path.write_text("".join(lines), encoding="utf-8")


def _history_path(hist_dir: Path, key: str) -> Path:
    return hist_dir / f"{key}.md"


def _check_cap(note: str, what: str) -> None:
    n = len(note.strip())
    cap = note_cap()
    if n > cap:
        _die(
            f"{what} is {n} chars; cap is {cap}. The one-line note carries markers only - "
            f"put the long form in the history file (`set --history` / `history <key> --append`)."
        )


def _archive_note(hist_dir: Path, key: str, status: str, note: str, title: str, dry_run: bool) -> str | None:
    """Append `note` to the key's history file. Returns the file path, or None when there is nothing to archive."""
    if not note.strip():
        return None
    hp = _history_path(hist_dir, key)
    if not dry_run:
        hp.parent.mkdir(parents=True, exist_ok=True)
        if not hp.exists():
            hp.write_text(
                f"# {key} — sprint-status note history\n\n"
                "Full text of every sprint-status note this row has carried, oldest first. "
                "The live one-line note is in the sprint-status file; this file holds what it replaced.\n",
                encoding="utf-8",
            )
        with hp.open("a", encoding="utf-8") as f:
            f.write(f"\n## {_dt.date.today().isoformat()} · status: {status} · {title}\n\n{note.strip()}\n")
    return str(hp)


def cmd_get(args, path: Path) -> int:
    lines = _read(path)
    key = _resolve_key(lines, args.key)
    idx, m = _find_row(lines, key)
    assert idx is not None and m is not None  # _resolve_key already proved the row exists
    note = (m.group("rest") or "").lstrip().lstrip("#").strip()
    print(json.dumps({
        "status": "ok",
        "key": key,
        "value": m.group("status"),
        "line": idx + 1,
        "note": note,
    }))
    return 0


def cmd_set(args, path: Path) -> int:
    lines = _read(path)
    key = _resolve_key(lines, args.key)
    idx, m = _find_row(lines, key)
    assert idx is not None and m is not None  # _resolve_key already proved the row exists
    statuses = known_statuses()
    if args.status not in statuses and not args.force:
        _die(
            f"unknown status {args.status!r}; expected one of "
            f"{', '.join(statuses)} (--force to override)"
        )
    old = m.group("status")
    old_note = (m.group("rest") or "").lstrip().lstrip("#").strip()
    history_file = None
    if args.note is not None:
        _check_cap(args.note, "--note")
        if old_note and old_note != args.note.strip():
            history_file = _archive_note(
                args.history_dir, key, old, old_note, "note replaced by `sprint_status.py set`", args.dry_run
            )
    if args.history:
        history_file = _archive_note(
            args.history_dir, key, args.status, args.history,
            "detail recorded by `sprint_status.py set --history`", args.dry_run
        )
    rest = f"  # {args.note.strip()}" if args.note is not None else (m.group("rest") or "")
    lines[idx] = f"{m.group('indent')}{key}: {args.status}{rest}\n"
    _write(path, lines, args.dry_run)
    print(json.dumps({
        "status": "updated",
        "key": key,
        "from": old,
        "to": args.status,
        "line": idx + 1,
        "history_file": history_file,
        "dry_run": args.dry_run,
    }))
    return 0


def cmd_history(args, path: Path) -> int:
    lines = _read(path)
    key = _resolve_key(lines, args.key)
    _idx, m = _find_row(lines, key)
    assert m is not None
    if args.append:
        hp = _archive_note(
            args.history_dir, key, m.group("status"), args.append,
            "appended via `sprint_status.py history --append`", args.dry_run
        )
        print(json.dumps({"status": "appended", "key": key, "history_file": hp, "dry_run": args.dry_run}))
        return 0
    hp = _history_path(args.history_dir, key)
    if not hp.is_file():
        print(json.dumps({"status": "ok", "key": key, "history_file": None}))
        return 0
    sys.stdout.write(hp.read_text(encoding="utf-8"))
    return 0


def cmd_touch(args, path: Path) -> int:
    lines = _read(path)
    for i, line in enumerate(lines):
        m = LAST_UPDATED_RE.match(line.rstrip("\n"))
        if not m:
            continue
        date = args.date or _dt.date.today().isoformat()
        history_file = None
        if args.note:
            _check_cap(args.note, "--note")
            old_note = (m.group("rest") or "").lstrip().lstrip("#").strip()
            if old_note and old_note != args.note.strip():
                history_file = _archive_note(
                    args.history_dir, LAST_UPDATED_HISTORY_KEY, "n/a",
                    f"last_updated: {m.group('date')} — {old_note}",
                    "replaced by `sprint_status.py touch`", args.dry_run,
                )
        rest = f" # {args.note.strip()}" if args.note else (m.group("rest") or "")
        lines[i] = f"last_updated: {date}{rest}\n"
        _write(path, lines, args.dry_run)
        print(json.dumps({
            "status": "updated",
            "field": "last_updated",
            "from": m.group("date"),
            "to": date,
            "line": i + 1,
            "history_file": history_file,
            "dry_run": args.dry_run,
        }))
        return 0
    _die("last_updated line not found")
    return 2


def cmd_list(args, path: Path) -> int:
    lines = _read(path)
    wanted = set(args.status or [])
    rows = []
    for i, line in enumerate(lines):
        m = ROW_RE.match(line.rstrip("\n"))
        if not m:
            continue
        if wanted and m.group("status") not in wanted:
            continue
        if args.exclude_epics and m.group("key").startswith("epic-"):
            continue
        rows.append({"key": m.group("key"), "status": m.group("status"), "line": i + 1})
    if args.json:
        print(json.dumps({"status": "ok", "count": len(rows), "rows": rows}))
    else:
        for r in rows:
            print(f"{r['line']:>4}  {r['status']:<13} {r['key']}")
        print(f"--- {len(rows)} row(s)", file=sys.stderr)
    return 0


def cmd_body(_args, path: Path) -> int:
    """Stream the file with the last_updated note elided - makes grep output readable."""
    for line in _read(path):
        m = LAST_UPDATED_RE.match(line.rstrip("\n"))
        if m:
            sys.stdout.write(f"last_updated: {m.group('date')}  # <note elided>\n")
        else:
            sys.stdout.write(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    cap = note_cap()
    ap = argparse.ArgumentParser(description="key-addressed sprint-status query/edit")
    ap.add_argument("--path", type=Path, default=None, help="tracker file (default: config paths.sprint_status)")
    ap.add_argument("--history-dir", type=Path, default=None,
                    help="note-history dir (default: config paths.story_history_dir, or "
                         "story-history/ beside --path when --path is given)")
    ap.add_argument("--dry-run", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", help="print one story row")
    g.add_argument("key")
    g.set_defaults(fn=cmd_get)

    s = sub.add_parser("set", help="set a story's status (and optionally replace its note)")
    s.add_argument("key")
    s.add_argument("status")
    s.add_argument("--note", default=None, help=f"replace the trailing # note (max {cap} chars)")
    s.add_argument("--history", default=None, help="long-form detail appended to the story's history file")
    s.add_argument("--force", action="store_true", help="allow a status outside the configured set")
    s.set_defaults(fn=cmd_set)

    h = sub.add_parser("history", help="print a story's note history, or --append to it")
    h.add_argument("key")
    h.add_argument("--append", default=None, help="text to append under a dated heading")
    h.set_defaults(fn=cmd_history)

    t = sub.add_parser("touch", help="rewrite last_updated by key, not by line number")
    t.add_argument("--note", default=None, help=f"one sentence, max {cap} chars; the old note is archived")
    t.add_argument("--date", default=None, help="ISO date; defaults to today")
    t.set_defaults(fn=cmd_touch)

    ls = sub.add_parser("list", help="list story rows, optionally filtered by status")
    ls.add_argument("--status", nargs="*", default=None)
    ls.add_argument("--exclude-epics", action="store_true")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)

    b = sub.add_parser("body", help="stream the file with the last_updated note elided")
    b.set_defaults(fn=cmd_body)

    args = ap.parse_args(argv)
    if args.path is None:
        args.path = default_path()
        if args.history_dir is None:
            args.history_dir = default_history_dir()
    elif args.history_dir is None:
        args.history_dir = args.path.parent / DEFAULT_HISTORY_DIRNAME
    return args.fn(args, args.path)


if __name__ == "__main__":
    raise SystemExit(main())
