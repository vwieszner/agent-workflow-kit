#!/usr/bin/env python3
"""The HTML the control room renders: pages, partials and result fragments, as pure functions of documents.

Purpose
-------
`control_room.py` reads the harness through `story_state.py`, `auto_dev_driver.py`, `auto_dev_run_report.py` and
`auto_dev_metrics.py` and hands the parsed documents to this module. Nothing here reads a file, runs a process or
reads the clock: the caller passes `now`. Every value that reaches the page goes through `html.escape`; there is no raw
interpolation of a document value. A section (`status` ok / none / error, the states of `story_state.py`) is shown by
its state, so nothing renders blank; a section or row with `format: "internal"` carries an "internal format" marker.

Usage
-----
Imported by `control_room.py` and `control_room_launch.py`; not run on its own. `section_html(section)` and
`value_html(value)` render any document value generically (a list of objects as one table over the union of its
keys), so a section a later story adds shows without a change here.

When to use
-----------
Changing what the control room shows. The routes, cadence and commands are in `control_room.py`.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import auto_dev_driver
import story_state

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHORT_SESSION_RE = re.compile(r"[0-9a-f]{8}")
LIVE_STATES = auto_dev_driver.LIVE_STATES
EFFORTS = ("low", "medium", "high", "xhigh", "max")
STATES = ("ok", "none", "error")
HTMX_CONFIG = ('{"responseHandling":[{"code":"204","swap":false},{"code":"[23]..","swap":true},'
               '{"code":"[45]..","swap":true,"error":false}]}')
MARKER = '<span class="marker">internal format</span>'
DASH = "—"
RESULT_TARGET = 'hx-target="#action-result" hx-swap="innerHTML"'
DOC_NAMES = {"attention": "attention document", "board": "board document", "cost": "cost document",
             "runs": "runs document", "story": "story document", "run": "run report document",
             "stream": "phase stream document", "metrics-run": "run metrics document",
             "metrics-compare": "metrics comparison document"}


def esc(value) -> str:
    return html.escape(str(value))


@dataclass
class DocState:
    key: str
    doc: object = None
    fetched_at: float | None = None
    running_since: float | None = None
    failure: dict | None = None
    requested: bool = False
    refresh_path: str | None = None
    live_at_fetch: bool = False


# ------------------------------------------------------------------ generic values and sections

def is_section(value) -> bool:
    return isinstance(value, dict) and value.get("status") in STATES and "source" in value


def _empty(text: str) -> str:
    return f'<span class="empty">{esc(text)}</span>'


def value_html(value) -> str:
    if isinstance(value, dict):
        if is_section(value):
            return section_html(value)
        if not value:
            return _empty("(empty object)")
        return _kv_table(value)
    if isinstance(value, list):
        if not value:
            return _empty("none (empty list)")
        if all(isinstance(item, dict) for item in value):
            return _rows_table(value)
        return '<ul class="list">' + "".join(f"<li>{value_html(item)}</li>" for item in value) + "</ul>"
    if value is None:
        return _empty(DASH)
    if value is True or value is False:
        return "true" if value else "false"
    if isinstance(value, str):
        return esc(value) if value else _empty("(empty text)")
    return esc(value)


def _kv_table(mapping: dict) -> str:
    marker = MARKER if mapping.get("format") == "internal" else ""
    rows = "".join(f"<tr><th>{esc(key)}</th><td>{value_html(item)}</td></tr>" for key, item in mapping.items())
    return f'{marker}<table class="kv">{rows}</table>'


def _rows_table(rows: list) -> str:
    columns: list = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    head = "<tr>" + "".join(f"<th>{esc(key)}</th>" for key in columns) + "</tr>"
    body = []
    for row in rows:
        marker = MARKER if row.get("format") == "internal" else ""
        cells = []
        for index, key in enumerate(columns):
            cells.append(f"<td>{marker if index == 0 else ''}{value_html(row.get(key))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table class="rows">{head}{"".join(body)}</table>'


def section_html(section: dict) -> str:
    status = section.get("status")
    if status not in STATES:
        return _kv_table(section)
    marker = MARKER if section.get("format") == "internal" else ""
    label = {"ok": "ok", "none": DASH, "error": "error"}[status]
    detail = ""
    if status == "none":
        detail = f' <span class="reason">{esc(section.get("reason") or "no reason given")}</span>'
    elif status == "error":
        detail = f' <span class="error-text">{esc(section.get("error") or "no error text")}</span>'
    data = {k: v for k, v in section.items() if k not in ("status", "source", "reason", "error", "format")}
    table = _kv_table(data) if data else ""
    return (f'<div class="section {status}"><span class="state state-{status}">{label}</span>{detail} '
            f'<span class="src">{esc(section.get("source"))}</span>{marker}{table}</div>')


def state_text(section) -> str:
    """One escaped line for a section that is not shown as data."""
    if not isinstance(section, dict):
        return esc(DASH)
    marker = f" {MARKER}" if section.get("format") == "internal" else ""
    status = section.get("status")
    if status == "ok":
        return f"ok{marker}"
    if status == "none":
        return f'{DASH} {esc(section.get("reason") or "no reason given")}{marker}'
    if status == "error":
        return f'error: {esc(section.get("error") or "no error text")}{marker}'
    return esc(DASH)


# ------------------------------------------------------------------ time and document headers

def age_text(seconds: float) -> str:
    whole = int(max(0, seconds))
    if whole < 120:
        return f"{whole} s"
    if whole < 7200:
        return f"{whole // 60} min"
    return f"{whole // 3600} h"


def clock_text(timestamp: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(timestamp))


def iso_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def doc_head(state: DocState, now: float, note: str = "", target: str = "closest .doc-head") -> str:
    """`target`: what the 'Refresh now' button swaps, the head itself unless a block that polls holds it."""
    name = DOC_NAMES.get(state.key.split(":", 1)[0], state.key)
    parts = [f'<div class="doc-head" data-doc="{esc(state.key)}"><strong>{esc(name)}</strong>']
    if state.doc is not None and state.fetched_at is not None:
        generated = state.doc.get("generated_at") if isinstance(state.doc, dict) else None
        generated = generated or iso_text(state.fetched_at)
        parts.append(f' <span class="age">generated {esc(generated)} (age {age_text(now - state.fetched_at)})</span>')
    if state.running_since is not None:
        what = "refresh running since" if state.doc is not None else "first read running since"
        parts.append(f' <span class="running">{what} {clock_text(state.running_since)}</span>')
    elif state.doc is None and state.failure is None:
        parts.append(' <span class="unread">not read yet</span>')
    if state.refresh_path:
        parts.append(f' <button type="button" hx-post="{esc(state.refresh_path)}" hx-target="{esc(target)}" '
                     f'hx-swap="outerHTML">Refresh now</button>')
    if note:
        parts.append(f' <span class="note">{esc(note)}</span>')
    if state.failure:
        failure = state.failure
        tails = "\n".join(text for text in (failure.get("stderr_tail"), failure.get("stdout_tail")) if text)
        parts.append(f'<div class="banner" role="alert">refresh failed: {esc(failure.get("command"))} - '
                     f'{esc(failure.get("reason"))}<pre>{esc(tails)}</pre></div>')
    parts.append("</div>")
    return "".join(parts)


# ------------------------------------------------------------------ layout and fragments

def layout(title: str, body: str, active: str = "") -> str:
    links = (("board", "/", "Board"), ("attention", "/attention", "Attention"), ("runs", "/runs", "Runs"),
             ("metrics", "/metrics/compare", "Metrics"))
    nav = "".join('<a href="%s"%s>%s</a>' % (href, ' class="active"' if key == active else "", label)
                  for key, href, label in links)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{esc(title)} - control room</title>"
            f"<meta name=\"htmx-config\" content='{HTMX_CONFIG}'>"
            "<link rel=\"stylesheet\" href=\"/static/app.css\">"
            "<script src=\"/static/htmx.min.js\"></script></head><body>"
            f"<nav>{nav}</nav><main><h1>{esc(title)}</h1>"
            "<div id=\"action-result\" class=\"action-result\" aria-live=\"polite\"></div>"
            f"{body}</main></body></html>")


def result_html(kind: str, message_html: str) -> str:
    return f'<div class="result {esc(kind)}" data-result="{esc(kind)}">{message_html}</div>'


def error_page(status: int, message: str, as_page: bool = True) -> str:
    body = result_html("error", f"{status}: {esc(message)}")
    return layout(f"Error {status}", body) if as_page else body


def td(col: str, inner: str) -> str:
    return f'<td data-col="{col}">{inner}</td>'


def thead(*labels: str) -> str:
    # th cells without a tr: the data rows are the only tr of these tables, which the tests' table reader relies on;
    # the HTML parser opens the missing row itself.
    return "<thead>" + "".join(f"<th>{esc(label)}</th>" for label in labels) + "</thead>"


def hidden(**fields) -> str:
    return "".join(f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">' for name, value in fields.items())


def post_button(path: str, label: str, confirm: str = "") -> str:
    attr = f' hx-confirm="{esc(confirm)}"' if confirm else ""
    return (f'<button type="button" hx-post="{esc(path)}" {RESULT_TARGET}{attr}>{esc(label)}</button>')


def start_session_form(story_id: str = "") -> str:
    if story_id:
        field = hidden(story=story_id)
        button = "Start story session"
    else:
        field = ('<label>Story id <input type="text" name="story" required maxlength="64" '
                 'pattern="[A-Za-z0-9][A-Za-z0-9.\\-]*"></label> ')
        button = "Start story session"
    return (f'<form class="inline" hx-post="/sessions/start" {RESULT_TARGET}>{field}'
            f'<button type="submit">{button}</button></form>')


def start_anyway_form(story_id: str) -> str:
    """The launch-record refusal's escape: the same start with `force=1`."""
    return (f'<form class="inline" hx-post="/sessions/start" {RESULT_TARGET}>{hidden(story=story_id, force="1")}'
            '<button type="submit">Start anyway</button></form>')


def number(value, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return esc(value) if value not in (None, "") else DASH
    return f"{value:.{digits}f}"


def dollars(value) -> str:
    text = number(value)
    return f"${text}" if text != DASH and text[0].isdigit() else text


# ------------------------------------------------------------------ session controls 

def open_form(label: str, **fields) -> str:
    return (f'<form class="inline" hx-post="/sessions/open" {RESULT_TARGET}>{hidden(**fields)}'
            f'<button type="submit">{esc(label)}</button></form>')


def listing_control(kind, session_id, has_pid: bool, pid=None) -> str:
    """The control for a session entry: an open form, or the text saying why there is none."""
    if kind == "interactive":
        where = f"pid {esc(pid)}" if pid else "no pid in the listing"
        return f"interactive session in another terminal ({where})"
    if kind == "background":
        if not (isinstance(session_id, str) and SHORT_SESSION_RE.fullmatch(session_id)):
            return "no session id in the listing"
        label = "Open in a terminal window" if has_pid else "Open — restarts the stopped session"
        return open_form(label, kind="attach", id=session_id)
    return f"{esc(kind or 'unknown')} session: cannot be opened"


def resume_decision(run_state: str, session: dict):
    """('resume', uuid) | ('headless', None) | ('no-id', None) | ('undecided', None) for a driver phase session."""
    live = run_state in LIVE_STATES
    state = session.get("state")
    if live and state == "running":
        return "headless", None
    if not (state == "ended" or run_state in ("ended", "dead")):
        return "undecided", None
    init = session.get("init") if isinstance(session.get("init"), dict) else {}
    session_id = init.get("session_id")
    if isinstance(session_id, str) and UUID_RE.fullmatch(session_id):
        return "resume", session_id
    return "no-id", None


def resume_control(run_id: str, run_state: str, session: dict) -> str:
    decision, _ = resume_decision(run_state, session)
    if decision == "resume":
        return open_form("Resume in a terminal window", kind="resume", run=run_id, session=session.get("session"))
    if decision == "headless":
        return "running headless: cannot be entered"
    if decision == "no-id":
        return "no session id (init.session_id is missing or not a UUID): cannot be resumed"
    return "run state unknown: cannot tell whether the session is still running"


# ------------------------------------------------------------------ shared lookups

def doc_of(state: DocState) -> dict:
    return state.doc if isinstance(state.doc, dict) else {}


def sessions_section(attention: DocState, board: DocState):
    """The freshest `sessions` section (by `generated_at`), that document's `generated_at` and the earliest
    `fetched_at` of the cached attention and board documents (the start of the oldest refresh a listing in use may
    have been read in), or (None, None, None)."""
    found = []
    for order, state in enumerate((attention, board)):
        doc = doc_of(state)
        section = doc.get("sessions")
        if is_section(section) and section.get("status") == "ok":
            found.append((_parse_iso(doc.get("generated_at")), -order, section, doc.get("generated_at")))
    if not found:
        return None, None, None
    best = max(found, key=lambda item: (item[0], item[1]))
    starts = [state.fetched_at for state in (attention, board) if state.fetched_at is not None]
    return best[2], best[3], min(starts, default=None)


def _parse_iso(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def story_by_id(doc, story_id: str):
    for item in doc.get("stories", []) if isinstance(doc, dict) else []:
        if isinstance(item, dict) and item.get("story_id") == story_id:
            return item
    return None


def nested(mapping, *path):
    for key in path:
        if not isinstance(mapping, dict):
            return None
        mapping = mapping.get(key)
    return mapping


def section_value(section, render) -> str:
    """`render(section)` for an ok section, the section's state text otherwise."""
    if is_section(section) and section.get("status") == "ok":
        return render(section)
    return state_text(section)


# ------------------------------------------------------------------ AC2 the board

def needs_cell(attention: DocState, story_id: str) -> str:
    doc = attention.doc
    if not isinstance(doc, dict):
        why = "refresh failed" if attention.failure else "first read running" if attention.running_since else "no read yet"
        return f"attention document not read ({why})"
    section = doc.get("attention")
    if not (is_section(section) and section.get("status") == "ok"):
        return f"attention queue {state_text(section)}"
    entries = [e for e in section.get("entries", []) if isinstance(e, dict) and e.get("story") == story_id]
    if not entries:
        return "nothing waiting"
    counts = {"owner": 0, "external": 0}
    for entry in entries:
        counts[entry.get("asked_of")] = counts.get(entry.get("asked_of"), 0) + 1
    text = " · ".join(f"{who.capitalize()} {count}" for who, count in counts.items() if count)
    marks = [e["mark"] for e in entries if e.get("mark") in story_state.MARK_RANK]
    if marks:
        text += f" ({min(marks, key=story_state.MARK_RANK.get)})"
    href = "/attention?" + urlencode({"asked_of": "all", "story": story_id})
    return f'<a href="{esc(href)}">{esc(text)}</a>'


def cost_cell(cost: DocState, story_id: str, now: float) -> str:
    doc = cost.doc
    if not isinstance(doc, dict):
        return "cost document not read yet"
    age = f" (age {age_text(now - cost.fetched_at)})" if cost.fetched_at is not None else ""
    story = story_by_id(doc, story_id)
    if story is None:
        return esc(f"story {story_id} is not in the cost document") + age
    section = story.get("cost")
    if not (is_section(section) and section.get("status") == "ok"):
        return state_text(section) + age
    text = (f"{dollars(section.get('usd_measured'))} measured + {dollars(section.get('usd_est_topup'))} "
            f"est. top-up{age}")
    return esc(text) + (f" {MARKER}" if section.get("format") == "internal" else "")


def _stack_text(story: dict) -> str:
    def render(section):
        ready, missing = section.get("ready"), section.get("missing") or []
        if isinstance(ready, str):
            return esc(ready)
        text = f"{len(ready or [])} ready"
        return esc(text + (", missing: " + ", ".join(str(m) for m in missing) if missing else ""))
    return section_value(nested(story, "stack", "services"), render)


def _agents_text(story: dict) -> str:
    running = nested(story, "agents", "running")

    def render(section):
        agents = [a for a in section.get("agents", []) if isinstance(a, dict)]
        kinds = ", ".join(str(a.get("agent_type")) for a in agents)
        internal = section.get("format") == "internal" or any(a.get("format") == "internal" for a in agents)
        return esc(f"{len(agents)}" + (f" ({kinds})" if kinds else "")) + (f" {MARKER}" if internal else "")
    return section_value(running, render)


def _files_text(story: dict) -> str:
    def render(section):
        files = [f for f in section.get("files", []) if isinstance(f, dict)]
        counts = [(where, sum(1 for f in files if f.get("where") == where))
                  for where in ("committed", "uncommitted", "both", "untracked")]
        parts = ", ".join(f"{where} {count}" for where, count in counts if count)
        return esc(f"{len(files)}" + (f" ({parts})" if parts else ""))
    return section_value(story.get("git"), render)


def board_row(story: dict, attention: DocState, cost: DocState, now: float) -> str:
    sid = str(story.get("story_id"))
    tracker, slot, position, record = story.get("tracker"), story.get("slot"), story.get("position"), story.get("record")
    because = story.get("in_flight_because")
    because_text = ", ".join(str(item) for item in because) if isinstance(because, list) and because else DASH
    cells = [
        td("story", f'<a href="/story/{quote(sid, safe="")}">{esc(sid)}</a>'),
        td("status", section_value(tracker, lambda s: esc(s.get("story_status")))),
        td("because", esc(because_text)),
        td("slot", section_value(slot, lambda s: esc(f"{s.get('slot')} {s.get('branch')}"))),
        td("phase", section_value(position, lambda s: esc(s.get("line")))),
        td("stack", _stack_text(story)),
        td("agents", _agents_text(story)),
        td("files", _files_text(story)),
        td("record", section_value(record, lambda s: esc(s.get("last") or DASH))),
        td("needs", needs_cell(attention, sid)),
        td("cost", cost_cell(cost, sid, now)),
        td("actions", start_session_form(sid)),
    ]
    return f'<tr data-story="{esc(sid)}">{"".join(cells)}</tr>'


def board_inner(board: DocState, attention: DocState, cost: DocState, now: float) -> str:
    parts = [doc_head(board, now), doc_head(attention, now), doc_head(cost, now)]
    doc = doc_of(board)
    sources = doc.get("sources")
    if isinstance(sources, dict):
        parts.append('<ul class="sources">' + "".join(
            f'<li data-source="{esc(name)}">{esc(name)}: {state_text(section)}</li>'
            for name, section in sources.items()) + "</ul>")
    unattributed = doc.get("unattributed_agents")
    if unattributed is not None:
        parts.append('<p class="unattributed">unattributed agents: ' + section_value(
            unattributed, lambda s: esc(len([a for a in s.get("agents", [])]))
            + (f" {MARKER}" if s.get("format") == "internal" else "")) + "</p>")
    stories = [s for s in doc.get("stories", []) if isinstance(s, dict)]
    if board.doc is None:
        parts.append('<p class="empty">No board document yet.</p>')
    elif not stories:
        parts.append('<p class="empty">No story is in flight.</p>')
    else:
        rows = "".join(board_row(s, attention, cost, now) for s in stories)
        parts.append('<table class="board">' + thead(
            "Story", "Status", "In flight because", "Slot", "Phase", "Stack", "Running agents", "Files in progress",
            "Last record line", "Needs input", "Cost", "") + f"<tbody>{rows}</tbody></table>")
    return "".join(parts)


def board_page(board: DocState, attention: DocState, cost: DocState, now: float) -> str:
    body = (f'<div class="start-bar">{start_session_form()}</div>'
            '<div id="board" hx-get="/partials/board" hx-trigger="every 15s" hx-swap="innerHTML">'
            f'{board_inner(board, attention, cost, now)}</div>')
    return layout("Board", body, "board")


# ------------------------------------------------------------------ AC3 story detail

def pick_story(story_id: str, board: DocState, story: DocState):
    """(story object, 'story' | 'board' | None): the newer document that holds the story; a tie goes to the board."""
    from_board, from_story = story_by_id(board.doc, story_id), story_by_id(story.doc, story_id)
    if from_story is not None and (from_board is None or (story.fetched_at or 0) > (board.fetched_at or 0)):
        return from_story, "story"
    if from_board is not None:
        return from_board, "board"
    return None, None


def session_rows(entries, story_id: str) -> str:
    rows = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("story") != story_id:
            continue
        has_pid = entry.get("pid") is not None
        control = listing_control(entry.get("kind"), entry.get("id"), has_pid, entry.get("pid"))
        cells = [td("key", esc(entry.get("key"))), td("name", esc(entry.get("name"))),
                 td("kind", esc(entry.get("kind"))), td("state", esc(entry.get("state") or DASH)),
                 td("status", esc(entry.get("status") or DASH)), td("waiting_for", esc(entry.get("waitingFor") or DASH)),
                 td("pid", "yes" if has_pid else "no"), td("linked_by", esc(entry.get("linked_by") or DASH)),
                 td("started", esc(entry.get("started") or DASH)), td("open", control)]
        rows.append(f'<tr data-session-key="{esc(entry.get("key"))}">{"".join(cells)}</tr>')
    return "".join(rows)


def story_summary(story: dict) -> str:
    blocks = []
    record = nested(story, "record")
    if is_section(record) and record.get("status") == "ok":
        lines = "".join(f"<li>{esc(line)}</li>" for line in record.get("lines", []))
        blocks.append(f"<h3>record lines</h3><ol class=\"record\">{lines}</ol>")
    findings = nested(story, "artifacts", "findings")
    if is_section(findings) and findings.get("status") == "ok":
        items = "".join(f"<li>{esc(f.get('path'))}: rounds {esc(f.get('rounds'))}, "
                        f"triaged_rounds {esc(f.get('triaged_rounds'))}</li>" for f in findings.get("files", []))
        blocks.append(f"<h3>findings files</h3><ul>{items or '<li>none</li>'}</ul>")
    deferred = nested(story, "artifacts", "deferred")
    if is_section(deferred) and deferred.get("status") == "ok":
        items = "".join(f"<li>{esc(s.get('heading'))}: {esc(s.get('entries'))} entries</li>"
                        for s in deferred.get("sections", []))
        blocks.append(f"<h3>deferred-work sections</h3><ul>{items or '<li>none</li>'}</ul>")
    spec = nested(story, "artifacts", "spec")
    if is_section(spec) and spec.get("status") == "ok":
        blocks.append(f"<h3>spec</h3><p>{esc(spec.get('path'))}</p>")
    git = story.get("git")
    if is_section(git) and git.get("status") == "ok":
        blocks.append(f"<h3>commits</h3>{value_html(git.get('commits') or [])}"
                      f"<h3>files in progress</h3>{value_html(git.get('files') or [])}")
    agents = story.get("agents")
    if isinstance(agents, dict):
        dispatched, running = agents.get("dispatched"), agents.get("running")
        if is_section(dispatched) and dispatched.get("status") == "ok":
            blocks.append(f"<h3>dispatched agents</h3>{value_html(dispatched.get('facts') or [])}")
        if is_section(running) and running.get("status") == "ok":
            blocks.append(f"<h3>running agents</h3>{MARKER}{value_html(running.get('agents') or [])}")
    runs = story.get("driver_run_ids")
    if is_section(runs) and runs.get("status") == "ok":
        links = "".join(f'<li><a href="/runs/{quote(str(r), safe="")}">{esc(r)}</a></li>' for r in runs.get("ids", []))
        blocks.append(f"<h3>driver runs</h3><ul>{links or '<li>none</li>'}</ul>")
    return "".join(blocks)


def story_inner(story_id: str, board: DocState, story: DocState, cost: DocState, attention: DocState,
                now: float) -> str:
    chosen, label = pick_story(story_id, board, story)
    parts = [doc_head(board, now), doc_head(story, now), doc_head(cost, now), doc_head(attention, now),
             f'<p class="which">showing the {label} document</p>' if label else ""]
    if chosen is None:
        return "".join(parts) + result_html("error", esc(f"story {story_id} is not in a document yet"))
    parts.append(story_summary(chosen))
    cost_story = story_by_id(cost.doc, story_id)
    age = f" (age {age_text(now - cost.fetched_at)})" if cost.fetched_at is not None else ""
    parts.append(f"<h3>cost</h3><p>from the cost document{age}</p>")
    parts.append(value_html(cost_story.get("cost")) if cost_story is not None
                 else _empty(f"story {story_id} is not in the cost document"))
    section, _, _ = sessions_section(attention, board)
    rows = session_rows(section.get("entries", []), story_id) if section else ""
    parts.append("<h3>sessions linked to the story</h3>")
    if section is None:
        parts.append(_empty("no session listing has been read"))
    else:
        parts.append('<table class="sessions">' + thead(
            "Key", "Name", "Kind", "State", "Status", "Waiting for", "pid", "Linked by", "Started", "")
            + f"<tbody>{rows}</tbody></table>" if rows else _empty("no session is linked to this story"))
    parts.append("<h3>all sections</h3>")
    for key, value in chosen.items():
        if key in ("cost", "story_id"):
            continue
        parts.append(f"<h4>{esc(key)}</h4>{value_html(value)}")
    return "".join(parts)


def story_page(story_id: str, board: DocState, story: DocState, cost: DocState, attention: DocState,
               now: float) -> str:
    body = (f'<div class="start-bar">{start_session_form(story_id)}</div>'
            f'<div id="story" hx-get="/partials/story/{quote(story_id, safe="")}" hx-trigger="every 15s" '
            f'hx-swap="innerHTML">{story_inner(story_id, board, story, cost, attention, now)}</div>')
    return layout(f"Story {story_id}", body, "board")


# ------------------------------------------------------------------ AC4 the attention queue

def _entry_source(entry: dict) -> str:
    source = entry.get("source")
    if not isinstance(source, dict):
        return esc(source)
    if "session" in source:
        return esc(f"{source.get('name')} {source.get('session')} {source.get('session_kind')}")
    if "file" in source:
        return esc(f"{source['file']}:{source['line']}" if source.get("line") else source["file"])
    return esc(", ".join(f"{k}={v}" for k, v in source.items()))


def _entry_control(entry: dict) -> str:
    source = entry.get("source")
    if entry.get("kind") != "session" or not isinstance(source, dict):
        return DASH
    listing = entry.get("listing") if isinstance(entry.get("listing"), dict) else {}
    return listing_control(source.get("session_kind"), source.get("session"), bool(listing.get("pid_present")))


def attention_row(entry: dict) -> str:
    story = entry.get("story")
    story_cell = (f'<a href="/story/{quote(str(story), safe="")}">{esc(story)}</a>' if story
                  else esc(f"unlinked ({entry.get('unlinked_reason')})" if entry.get("unlinked_reason") else "unlinked"))
    marker = f"{MARKER} " if entry.get("format") == "internal" else ""
    cells = [td("mark", marker + esc(entry.get("mark") or DASH)), td("kind", esc(entry.get("kind"))),
             td("story", story_cell),
             td("story_status", esc(entry.get("story_status") or DASH)), td("asked_of", esc(entry.get("asked_of"))),
             td("source", _entry_source(entry)), td("asked", esc(entry.get("asked"))),
             td("since", esc(entry.get("since") or DASH)), td("open", _entry_control(entry))]
    return f"<tr data-entry=\"{esc(entry.get('kind'))}\">{''.join(cells)}</tr>"


def attention_inner(attention: DocState, asked_of: str, story: str | None, now: float) -> str:
    parts = [doc_head(attention, now)]
    section = doc_of(attention).get("attention")
    if attention.doc is None:
        parts.append('<p class="empty">No attention document yet.</p>')
        return "".join(parts)
    if not (is_section(section) and section.get("status") == "ok"):
        parts.append(f'<p class="empty">attention queue {state_text(section)}</p>')
        return "".join(parts)
    entries = [e for e in section.get("entries", []) if isinstance(e, dict)
               and (asked_of == "all" or e.get("asked_of") == asked_of) and (story is None or e.get("story") == story)]
    if entries:
        parts.append('<table class="queue">' + thead(
            "Mark", "Kind", "Story", "Story status", "Asked of", "Source", "Asked", "Since", "")
            + "<tbody>" + "".join(attention_row(e) for e in entries) + "</tbody></table>")
    else:
        parts.append('<p class="empty">Nothing matches this filter.</p>')
    sources = section.get("sources")
    if isinstance(sources, dict):
        rows = "".join(f'<tr data-source-row="{esc(name)}">{td("name", esc(name))}{td("state", state_text(src))}</tr>'
                       for name, src in sources.items())
        parts.append(f'<h3>sources</h3><table class="sources">{thead("Source", "State")}<tbody>{rows}</tbody></table>')
    return "".join(parts)


def attention_page(attention: DocState, asked_of: str, story: str | None, now: float) -> str:
    def link(value: str) -> str:
        query = {"asked_of": value, **({"story": story} if story else {})}
        mark = ' class="active"' if value == asked_of else ""
        return f'<a href="/attention?{esc(urlencode(query))}"{mark}>{esc(value)}</a>'

    query = {"asked_of": asked_of, **({"story": story} if story else {})}
    body = (f'<p class="filter">asked of: {" | ".join(link(v) for v in ("owner", "external", "all"))}'
            f'{f" - story {esc(story)}" if story else ""}</p>'
            f'<div id="queue" hx-get="/partials/attention?{esc(urlencode(query))}" hx-trigger="every 15s" '
            f'hx-swap="innerHTML">{attention_inner(attention, asked_of, story, now)}</div>')
    return layout("Attention", body, "attention")


# ------------------------------------------------------------------ AC5-AC7 runs

def stop_controls(run_id: str, state_name: str) -> str:
    quoted = quote(run_id, safe="")
    controls = []
    if state_name in ("running", "stuck"):
        controls.append(post_button(f"/runs/{quoted}/stop", "Stop"))
    if state_name in LIVE_STATES:
        controls.append(post_button(
            f"/runs/{quoted}/stop-now", "Stop now",
            f"Stop run {run_id} now? Its phase session is killed and the run ends with exit 1."))
    return "".join(controls)


def _run_state(row: dict) -> tuple[str, str]:
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    return str(state.get("name") or "unknown"), str(state.get("reason") or "")


def run_row_html(row: dict) -> str:
    run_id = str(row.get("run_id"))
    name, reason = _run_state(row)
    state_cell = esc(f"{name}: {reason}" if reason else name)
    if row.get("status") == "error":
        state_cell += esc(f" - error: {row.get('error')}")
    pid = row.get("pid")
    pid_cell = state_text(pid) if isinstance(pid, dict) else esc(pid if pid is not None else DASH)
    phase = row.get("phase") if isinstance(row.get("phase"), dict) else {}
    spend = row.get("spend") if isinstance(row.get("spend"), dict) else {}
    if spend.get("status") == "ok":
        cap = spend.get("cap_usd")
        spent = dollars(spend.get("spent_usd"))
        spend_cell = esc(f"{spent} of {dollars(cap)}" if isinstance(cap, (int, float)) else f"{spent} ({cap})")
    else:
        spend_cell = state_text(spend) if spend else esc(DASH)
    stop = []
    if row.get("stop_requested"):
        stop.append("stop requested")
    controls = stop_controls(run_id, name)
    stop_cell = esc(" ".join(stop)) + controls if (stop or controls) else esc(DASH)
    cells = [td("run_id", f'<a href="/runs/{quote(run_id, safe="")}">{esc(run_id)}</a>'), td("state", state_cell),
             td("pid", pid_cell), td("story", esc(row.get("story") if row.get("story") is not None else DASH)),
             td("phase", esc(phase.get("text") or DASH)), td("spend", spend_cell),
             td("started", esc(row.get("started_utc") or DASH)), td("stop", stop_cell)]
    return f'<tr data-run="{esc(run_id)}">{"".join(cells)}</tr>'


def runs_of(state: DocState) -> list:
    doc = state.doc if isinstance(state.doc, dict) else {}
    return [row for row in doc.get("runs", []) if isinstance(row, dict)]


def runs_inner(runs: DocState, now: float) -> str:
    parts = [doc_head(runs, now)]
    doc = runs.doc
    if doc is None:
        parts.append('<p class="empty">No runs document yet.</p>')
    elif not is_section(doc) or doc.get("status") != "ok":
        parts.append(f'<p class="empty">runs {state_text(doc)}</p>')
    elif not runs_of(runs):
        parts.append('<p class="empty">No driver run exists.</p>')
    else:
        parts.append('<table class="runs">' + thead("Run", "State", "pid", "Story", "Phase", "Spend", "Started", "Stop")
                     + "<tbody>" + "".join(run_row_html(r) for r in runs_of(runs)) + "</tbody></table>")
    return "".join(parts)


def _run_option_fields() -> str:
    import argparse
    parser = argparse.ArgumentParser()
    auto_dev_driver.add_run_options(parser)

    def default(dest: str, fallback: str) -> str:
        value = parser.get_default(dest)
        return f"default {value}" if value is not None else fallback

    def field(label: str, dest: str, hint: str, widget: str | None = None) -> str:
        control = widget or f'<input type="text" name="{dest}" autocomplete="off">'
        return f'<div class="field"><label>{esc(label)} {control}</label> <span class="hint">{esc(hint)}</span></div>'

    effort = '<select name="orchestrator_effort"><option value="">(driver default)</option>' + "".join(
        f'<option value="{e}">{e}</option>' for e in EFFORTS) + "</select>"
    return "".join([
        field("Story", "story", "blank: the first session picks one"),
        field("Max stories", "max_stories", "blank: no limit"),
        field("Max cost (USD)", "max_cost_usd", "blank: no cap"),
        field("Session timeout (min)", "session_timeout_min", default("session_timeout_min", "")),
        field("Land stall (min)", "land_stall_min", default("land_stall_min", "")),
        field("Orchestrator model", "orchestrator_model", default("orchestrator_model", "")),
        field("Orchestrator effort", "orchestrator_effort", default("orchestrator_effort", ""), effort)])


def runs_page(runs: DocState, now: float) -> str:
    body = ('<p><a href="/metrics/compare">Compare runs</a></p><h3>Start a run</h3>'
            f'<form class="start-run" hx-post="/runs/start" {RESULT_TARGET}>{_run_option_fields()}'
            '<button type="submit">Start run</button></form>'
            '<div id="runs" hx-get="/partials/runs" hx-trigger="every 15s" hx-swap="innerHTML">'
            f'{runs_inner(runs, now)}</div>')
    return layout("Runs", body, "runs")


def stream_session_id(row: dict):
    progress = row.get("in_progress")
    if is_section(progress) and progress.get("status") == "ok" and isinstance(progress.get("session"), int):
        return progress["session"]
    return None


def stream_inner(row: dict, stream: DocState | None, session_number, now: float) -> str:
    if session_number is None or stream is None:
        progress = row.get("in_progress")
        shown = section_html(progress) if is_section(progress) else _empty("no session is in progress")
        return f'<div class="stream">{shown}</div>'
    parts = [doc_head(stream, now)]
    sessions = nested(doc_of(stream), "run", "sessions") or []
    session = next((s for s in sessions if isinstance(s, dict) and s.get("session") == session_number), None)
    if session is None:
        if stream.doc is not None:
            parts.append(_empty(f"session {session_number} is not in the report"))
    else:
        fields = {key: session.get(key) for key in ("state", "idle_seconds", "init", "dispatches", "errors",
                                                      "last_actions") if key in session}
        parts.append(_kv_table(fields))
    return '<div class="stream">' + "".join(parts) + "</div>"


def run_sessions_table(run_id: str, run_state: str, report: DocState) -> str:
    run = nested(doc_of(report), "run")
    if report.doc is None:
        return ""
    if not isinstance(run, dict) or not isinstance(run.get("sessions"), list):
        return result_html("error", f"run report: {state_text(run)}")
    rows = []
    for session in run["sessions"]:
        if not isinstance(session, dict):
            continue
        result = session.get("result") if isinstance(session.get("result"), dict) else {}
        init = session.get("init") if isinstance(session.get("init"), dict) else {}
        cells = [td("session", esc(session.get("session"))), td("state", esc(session.get("state"))),
                 td("story", esc(session.get("story") or DASH)),
                 td("outcome", esc(session.get("outcome") or DASH)), td("step", esc(session.get("step") or DASH)),
                 td("minutes", esc(result.get("minutes", DASH))), td("cost", esc(result.get("total_cost_usd", DASH))),
                 td("session_id", esc(init.get("session_id") or DASH)),
                 td("resume", resume_control(run_id, run_state, session))]
        rows.append(f'<tr data-session="{esc(session.get("session"))}">{"".join(cells)}</tr>')
    if not rows:
        return _empty("the run has no session stream yet")
    return ('<table class="sessions">' + thead("Session", "State", "Story", "Outcome", "Step", "Minutes", "Cost (USD)",
                                              "Session id", "") + "<tbody>" + "".join(rows) + "</tbody></table>")


def sessions_polling(run_state: str, report: DocState) -> bool:
    """Whether the Sessions block polls its partial: while the run is live, while a fetch of its report runs, and
    after the run ended until the report fetched after the end is cached."""
    return (run_state in LIVE_STATES or report.running_since is not None or report.doc is None
            or report.live_at_fetch)


def run_sessions_inner(run_id: str, run_state: str, report: DocState, now: float, note: str = "") -> str:
    return doc_head(report, now, note, "closest #run-sessions") + run_sessions_table(run_id, run_state, report)


def run_sessions_block(run_id: str, run_state: str, report: DocState, now: float, note: str = "") -> str:
    """The Sessions block. It polls its partial while `sessions_polling` holds; the partial answers 286 once it does
    not. 'Refresh now' swaps the whole block, so the fetch it starts polls again after a 286 stopped the poll."""
    poll = (f' hx-get="/partials/runs/{quote(run_id, safe="")}/sessions" hx-trigger="every 2s" hx-swap="innerHTML"'
            if sessions_polling(run_state, report) else "")
    return f'<div id="run-sessions"{poll}>{run_sessions_inner(run_id, run_state, report, now, note)}</div>'


def run_page(row: dict, runs: DocState, report: DocState, now: float) -> str:
    run_id = str(row.get("run_id"))
    name, reason = _run_state(row)
    quoted = quote(run_id, safe="")
    live = name in LIVE_STATES
    spend = row.get("spend") if isinstance(row.get("spend"), dict) else {}
    phase = row.get("phase") if isinstance(row.get("phase"), dict) else {}
    header = _kv_table({"run_id": run_id, "state": f"{name}: {reason}" if reason else name,
                        "pid": row.get("pid") if row.get("pid") is not None else DASH,
                        "story": row.get("story"), "phase": phase.get("text"),
                        "spend": spend if spend else None, "started_utc": row.get("started_utc"),
                        "stop_requested": row.get("stop_requested"), "record": row.get("record")})
    log_trigger = "load, every 2s" if live else "load"
    stream_trigger = "load, every 5s" if live else "load"
    body = (f'<p><a href="/runs">All runs</a> | <a href="/metrics/run/{quoted}">Metrics for this run</a></p>'
            f'{doc_head(runs, now)}{header}<div class="controls">{stop_controls(run_id, name)}</div>'
            '<h3>driver.log</h3>'
            f'<pre id="driver-log" class="log" hx-get="/partials/runs/{quoted}/log" hx-trigger="{log_trigger}" '
            'hx-swap="innerHTML scroll:bottom"></pre>'
            '<h3>Phase stream</h3>'
            f'<div id="stream" hx-get="/partials/runs/{quoted}/stream" hx-trigger="{stream_trigger}" '
            'hx-swap="innerHTML"></div>'
            f'<h3>Sessions</h3>{run_sessions_block(run_id, name, report, now)}')
    return layout(f"Run {run_id}", body, "runs")


# ------------------------------------------------------------------ AC8 metrics

def metrics_inner(state: DocState, now: float, poll_path: str, render, note: str = "") -> str:
    computing = state.running_since is not None
    attrs = f' hx-get="{esc(poll_path)}" hx-trigger="every 5s" hx-swap="outerHTML"' if computing else ""
    parts = [f'<div class="metrics-body"{attrs}>', doc_head(state, now, note)]
    if computing:
        parts.append(f'<p class="computing">computing, started {age_text(now - state.running_since)} ago</p>')
    if state.doc is not None:
        parts.append(render(state.doc))
    parts.append("</div>")
    return "".join(parts)


def render_metrics_run(doc: dict) -> str:
    run = doc.get("run") if isinstance(doc, dict) else None
    parts = []
    if isinstance(run, dict):
        for key in ("totals", "knobs", "knobs_changes", "harness_ids", "fingerprint"):
            if key in run:
                parts.append(f"<h3>{esc(key)}</h3>{value_html(run[key])}")
    else:
        parts.append(f"<h3>run</h3>{value_html(run)}")
    for item in doc.get("stories", []) if isinstance(doc, dict) else []:
        if isinstance(item, dict):
            parts.append(f"<h3>story {esc(item.get('story_id'))}</h3>")
            parts.extend(f"<h4>{esc(key)}</h4>{value_html(value)}" for key, value in item.items() if key != "story_id")
    return "".join(parts)


def group_blocks(groups) -> str:
    """One block per `groups[]` item, headed by its `config_id`, as the run view does per `stories[]` item."""
    if not (isinstance(groups, list) and groups and all(isinstance(item, dict) for item in groups)):
        return value_html(groups)
    return "".join(f"<h4>group {esc(item.get('config_id'))}</h4>"
                   f"{value_html({key: value for key, value in item.items() if key != 'config_id'})}"
                   for item in groups)


def render_metrics_compare(doc: dict) -> str:
    parts = []
    for key in ("grouping", "cost_mode", "groups", "unfingerprinted_runs"):
        if isinstance(doc, dict) and key in doc:
            body = group_blocks(doc[key]) if key == "groups" else value_html(doc[key])
            parts.append(f"<h3>{esc(key)}</h3>{body}")
    return "".join(parts)


def metrics_page(title: str, refresh_path: str, state: DocState, now: float, poll_path: str, render) -> str:
    def form(cost: str, label: str) -> str:
        return (f'<form class="inline" hx-post="{esc(refresh_path)}" hx-target="#metrics-body" hx-swap="innerHTML">'
                f'{hidden(cost=cost)}<button type="submit">{esc(label)}</button></form>')

    body = (f'<p><a href="/runs">Runs</a> | <a href="/metrics/compare">Comparison</a></p>'
            f'{form("cached", "Refresh")}'
            f'{form("live", "Recompute live cost (about 25 s per non-final story)")}'
            f'<div id="metrics-body">{metrics_inner(state, now, poll_path, render)}</div>')
    return layout(title, body, "metrics")


# ------------------------------------------------------------------ result fragments

def run_start_result(rc, doc, stdout: str, stderr_tail: str) -> str:
    if rc is None:
        return result_html("error", "The start did not answer in time; the runs list shows whether a run began.")
    if isinstance(doc, dict) and doc.get("status") == "ok":
        run_id = str(doc.get("run_id"))
        return result_html("ok", f'Run <a href="/runs/{quote(run_id, safe="")}">{esc(run_id)}</a> started '
                                 f'(pid {esc(doc.get("pid"))}).')
    if isinstance(doc, dict) and doc.get("status") == "error":
        return result_html("error", f'The driver refused the start: {esc(doc.get("error"))} '
                                    f'(exit code {esc(doc.get("exit_code", rc))}).')
    return result_html("error", f"The driver answered with exit code {esc(rc)} and no JSON. "
                                f"stderr: <pre>{esc(stderr_tail)}</pre>")


def stop_result(rc, doc, stderr_tail: str) -> str:
    if isinstance(doc, dict) and "message" in doc:
        kind = "ok" if doc.get("status") == "ok" else "refused"
        return result_html(kind, f'{esc(doc.get("message"))} (state: {esc(doc.get("state"))})')
    why = "did not run" if rc is None else f"exit code {rc}"
    return result_html("error", f"The driver stop command gave no JSON answer ({esc(why)}). "
                                f"stderr: <pre>{esc(stderr_tail)}</pre>")


def launch_result(outcome: dict) -> str:
    kind = outcome.get("status", "error")
    message = esc(outcome.get("message", ""))
    existing = outcome.get("existing")
    extra = ""
    if isinstance(existing, dict):
        extra = (f' Existing session {esc(existing.get("id") or existing.get("key"))} ({esc(existing.get("name"))}): '
                 + listing_control(existing.get("kind"), existing.get("id"), existing.get("pid") is not None,
                                   existing.get("pid")))
    if outcome.get("launch_ids"):
        extra += " " + start_anyway_form(outcome["story"])
    if outcome.get("session_id"):
        extra += " " + open_form("Open in a terminal window", kind="attach", id=outcome["session_id"])
    return result_html(kind, message + extra)
