#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Shared helpers for the external design owner's question tracker and the design tooling.

The design owner is an outside role (a designer, a UX lead) whose questions the project owner does
not rule: `config: design.owner_role` names the role in prose, `config: design.question_tag` is the
tag of their spec markers (`[OPEN-Q n] (<tag>)`), which must also be listed in
`config: spec.external_question_tags`. Their questions live as tickets in an issue tracker
(`config: tracker.*`) and as rows of the question tracker file (`config: paths.design_questions`).

The question tracker file holds one ticket table whose header row starts
`| Ticket | Item | Priority | Blocks | Status` (the Status header may carry the last sync date:
`Status (<YYYY-MM-DD>)`). The table may sit inside a blockquote (every line prefixed `> `); a row's
first cell is the ticket key, bare, bold or as a `[KEY](url)` link; its last cell is the status,
done when it starts with `config: design.done_marker`.

Every function degrades explicitly: `configured()` is False with no tracker file configured, and
callers print SKIPPED. Imported by design_ticket, tracker, tracker_digest, halt_release,
log_failure and resume_halted. Not run on its own. Stdlib only.
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

HEADER_RE = re.compile(r"^(?:>\s?)?\|\s*Ticket\s*\|\s*Item\s*\|")
STATUS_DATE_RE = re.compile(r"Status \((\d{4}-\d{2}-\d{2})\)")
DEFAULT_TICKET_RE = r"\b[A-Z][A-Z0-9]+-\d+\b"


def cfg(key: str, default):
    value = wfconfig.get(key, default)
    return default if value is None else value


def tracker_rel() -> Path | None:
    raw = str(cfg("paths.design_questions", "") or "").strip()
    return Path(raw) if raw else None


def configured() -> bool:
    return tracker_rel() is not None


def owner_role() -> str:
    return str(cfg("design.owner_role", "") or "the design owner")


def question_tag() -> str:
    """The marker tag of the design owner's questions; '' when no external tag is configured."""
    tag = str(cfg("design.question_tag", "") or "").strip()
    if tag:
        return tag
    tags = [str(t) for t in (cfg("spec.external_question_tags", []) or []) if str(t).strip()]
    return tags[0] if tags else ""


def done_marker() -> str:
    return str(cfg("design.done_marker", "✅") or "✅")


def ticket_re() -> re.Pattern[str]:
    return re.compile(str(cfg("paths.design_ticket_regex", DEFAULT_TICKET_RE) or DEFAULT_TICKET_RE))


def is_ticket_key(text: str) -> bool:
    """`text` is exactly one ticket key."""
    m = ticket_re().search(text)
    return bool(m) and m.group(0) == text


def ticket_url(key: str) -> str:
    site = str(cfg("tracker.site", "") or "").rstrip("/")
    template = str(cfg("tracker.browse_url", "") or "")
    if template:
        return wfconfig.render(template, key=key, site=site)
    return f"{site}/browse/{key}" if site else ""


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith(">"):
        body = body[1:].strip()
    return [c.strip() for c in body.strip("|").split("|")]


def _row_key(line: str) -> str | None:
    if not re.match(r"^(?:>\s?)?\|", line.strip()):
        return None
    first = _cells(line)[0] if _cells(line) else ""
    m = ticket_re().search(first)
    return m.group(0) if m else None


def table_span(lines: list[str]) -> tuple[int, int] | None:
    """(header index, index after the last table line) of the ticket table, or None."""
    head = next((i for i, ln in enumerate(lines) if HEADER_RE.match(ln.strip())), None)
    if head is None:
        return None
    end = head + 1
    while end < len(lines) and re.match(r"^(?:>\s?)?\|", lines[end].strip()):
        end += 1
    return head, end


def ticket_rows(text: str) -> dict[str, dict]:
    """Ticket key -> {"line", "item", "status"} for every row of the ticket table."""
    lines = text.splitlines()
    span = table_span(lines)
    if span is None:
        return {}
    out: dict[str, dict] = {}
    for line in lines[span[0] + 2: span[1]]:
        key = _row_key(line)
        if key:
            cells = _cells(line)
            out[key] = {"line": line, "item": cells[1] if len(cells) > 1 else "",
                        "status": cells[-1] if cells else ""}
    return out


def last_sync(text: str) -> str | None:
    m = STATUS_DATE_RE.search(text)
    return m.group(1) if m else None


def insert_row(text: str, key: str, item: str, blocks: str, origin: str, today: str,
               priority: str | None = None) -> tuple[str, bool]:
    """`text` with the ticket's row appended to the ticket table; (text, False) when the key has a row.
    Raises ValueError when the file has no ticket table."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    span = table_span(lines)
    if span is None:
        raise ValueError("no ticket table (a `| Ticket | Item | Priority | Blocks | Status` header row)")
    if key in ticket_rows(text):
        return text, False
    prefix = "> " if lines[span[0]].lstrip().startswith(">") else ""
    url = ticket_url(key)
    cell = f"[{key}]({url})" if url else f"[{key}]"
    prio = priority or str(cfg("tracker.priority", "Medium") or "Medium")
    lines.insert(span[1], f"{prefix}| {cell} | {item} | {prio} | {blocks} | ⏳ To Do — filed {today} ({origin}) |")
    return newline.join(lines), True


def ticket_status(repo: Path, key: str) -> str | None:
    """The Status cell of the ticket's row in the tracker file, or None (no tracker, no row)."""
    rel = tracker_rel()
    if rel is None or not (repo / rel).is_file():
        return None
    row = ticket_rows((repo / rel).read_text(encoding="utf-8")).get(key)
    return row["status"] if row else None


def is_done(status: str | None) -> bool:
    return bool(status) and status.strip().startswith(done_marker())


def own_lines(base: str, work: str, mine) -> str | None:
    """`base` plus only the changes of `work` whose added and removed lines all satisfy `mine`.

    None when no such change exists. Changes touching other lines (another session's edits in the
    same file) are left out."""
    a, b = base.splitlines(keepends=True), work.splitlines(keepends=True)
    out: list[str] = []
    taken = False
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            out += a[i1:i2]
            continue
        old, new = a[i1:i2], b[j1:j2]
        if all(mine(x.rstrip("\r\n")) for x in old + new):
            out += new
            taken = True
        else:
            out += old
    if not taken:
        return None
    text = "".join(out)
    if base.endswith("\n") and text and not text.endswith("\n"):
        text += "\n"
    return text
