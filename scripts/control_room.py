#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The localhost control room: a read-mostly web view of the story pipeline and the auto-dev driver.

Purpose
-------
Serves the board (`story_state.py --no-cost`), the story detail, the attention queue (`story_state.py --attention`),
the driver runs (`auto_dev_driver.py list --json`, the run reports, `driver.log`, the metrics views) and five
actions: start a driver run, stop a run, open a terminal window on a session, resume a driver session and start a
story session. It holds no state of its own beyond the cached documents: every page is a function of the last good
copy of the documents the tools printed (`control_room_views.py` renders them), each document is refreshed by one
single-flight background run, and a page request never waits on a refresh.

The server binds 127.0.0.1 only. Every request needs a `Host` of `127.0.0.1:<port>` or `localhost:<port>`; every
POST also needs `HX-Request: true` and an `Origin` of one of those two origins. Ids are matched with `re.fullmatch`
before they reach a command, a path or a script.

Documents and cadence (background documents start `INTERVALS[key]` seconds after their previous start, one run per
key at a time; on-request documents start when a page asks and are kept as stated in `Documents.ensure`):

    attention  story_state.py --attention           every 60 s
    board      story_state.py --no-cost             every 180 s
    cost       story_state.py                       every 1800 s
    runs       auto_dev_driver.py list --json       every 15 s
    story:<id> story_state.py --story <id> --no-cost    on request, kept 60 s
    run:<id>   auto_dev_run_report.py <id> --json       on request, kept 60 s while the run is live
    stream:<id>:<n>  auto_dev_run_report.py <id> --session <n> --last 30 --json   on request, kept 5 s
    metrics-run:<id>, metrics-compare   auto_dev_metrics.py ...    on request, kept until refreshed

Usage
-----
    uv run --no-project .workflow/scripts/control_room.py [--port N]

`--port` defaults to `config: control_room.port` (4747). Prints `control room on http://127.0.0.1:<port>/` and serves
until interrupted; exit 2 when the port is taken or the main checkout is not found. State (the launch record, the
start outputs) lives in `.workflow/state/control-room/`.

When to use
-----------
To watch the pipeline, answer what needs input, and start or stop runs without a terminal. The terminal-window and
session-start effects, and their per-platform backends, are in `control_room_launch.py`.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import control_room_launch as launch  # noqa: E402
import control_room_views as views  # noqa: E402
import run_scoped_tests  # noqa: E402
import story_state as ss  # noqa: E402
import wfconfig  # noqa: E402

DEFAULT_PORT = 4747
STATIC_DIR = SCRIPTS_DIR / "control_room_static"
STATIC_FILES = {"htmx.min.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}
INTERVALS = {"attention": 60, "board": 180, "cost": 1800, "runs": 15}
# Seconds per command: hang detectors well above a normal run (the live-cost metrics path recomputes every non-final
# story; start covers the driver's own start bound).
TIMEOUTS = {"attention": 100, "board": 770, "cost": 1800, "runs": 5, "story": 165, "run": 5, "stream": 5,
            "metrics-run": 600, "metrics-compare": 600, "sprint": 5, "stop": 10, "start": 25}
STORY_TTL, RUN_TTL, STREAM_TTL = 60, 60, 5
BODY_CAP = 64 * 1024
LOG_BYTES, LOG_LINES = 64 * 1024, 200

STORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}")
RUN_RE = re.compile(r"\d{8}-\d{6}")
DIGITS_RE = re.compile(r"[0-9]{1,6}")
POSITIVE_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}")
ASKED_OF = ("owner", "external", "all")


def run_reader(argv, *, cwd=None, env=None, timeout):
    """(returncode, stdout, stderr) of a read command; returncode None when it timed out (its process tree is
    killed) or could not start."""
    kw: dict = {} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen([str(a) for a in argv], cwd=str(cwd) if cwd else None, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", **kw)
    except Exception as exc:  # noqa: BLE001 -- an unstartable command is a failed document, not a crash
        return None, "", f"{type(exc).__name__}: {exc}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        note = run_scoped_tests._kill_tree(proc)
        text = f"timed out after {timeout:g} s"
        if note:
            text += f"; {note}"
        try:
            proc.communicate(timeout=run_scoped_tests.KILL_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            text += f"; its output pipes were still open {run_scoped_tests.KILL_DRAIN_SECONDS} s after the kill"
        return None, "", text
    return proc.returncode, out, err


clock = time.time


def child_env() -> dict:
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def command_text(argv) -> str:
    """The script's basename and the arguments after it: how a failed refresh names its command."""
    items = [str(a) for a in argv]
    for index, item in enumerate(items):
        if item.endswith(".py"):
            return " ".join([Path(item).name, *items[index + 1:]])
    return " ".join(items)


def tail(text: str) -> str:
    return (text or "").strip()[-ss.MAX_ERR:]


def parse_json(text: str):
    """The JSON value of `text`, or None when it holds none."""
    try:
        return json.loads(text) if text and text.strip() else None
    except ValueError:
        return None


# ---------------------------------------------------------------- documents

class Documents:
    """The cached documents: one entry per key, one refresh thread per key at a time."""

    BACKGROUND = ("attention", "board", "cost", "runs")

    def __init__(self, app: "App"):
        self.app = app
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        self.threads: list[threading.Thread] = []

    @staticmethod
    def blank() -> dict:
        return {"doc": None, "fetched_at": None, "running_since": None, "failure": None, "requested": False,
                "last_start": None, "live_at_fetch": False}

    def entry(self, key: str) -> dict:
        return self.entries.setdefault(key, self.blank())

    def driver(self, *args) -> list[str]:
        """The driver through `uv run --no-project` when uv is on PATH (its PEP 723 header), else this interpreter."""
        script = str(SCRIPTS_DIR / "auto_dev_driver.py")
        uv = shutil.which("uv")
        return [uv, "run", "--no-project", script, *args] if uv else [sys.executable, script, *args]

    def command(self, key: str, cost: str | None) -> list[str]:
        kind, _, rest = key.partition(":")

        def tool(script: str, *args: str) -> list[str]:
            return [sys.executable, str(SCRIPTS_DIR / script), *args]

        mode = cost or "cached"
        if kind == "attention":
            return tool("story_state.py", "--attention")
        if kind == "board":
            return tool("story_state.py", "--no-cost")
        if kind == "cost":
            return tool("story_state.py")
        if kind == "runs":
            return self.driver("list", "--json")
        if kind == "story":
            return tool("story_state.py", "--story", rest, "--no-cost")
        if kind == "run":
            return tool("auto_dev_run_report.py", rest, "--json")
        if kind == "stream":
            run_id, _, number = rest.partition(":")
            return tool("auto_dev_run_report.py", run_id, "--session", number, "--last", "30", "--json")
        if kind == "metrics-run":
            return tool("auto_dev_metrics.py", "run", rest, "--cost", mode, "--json")
        return tool("auto_dev_metrics.py", "compare", "--cost", mode, "--json")

    def refresh(self, key: str, *, cost: str | None = None, live: bool = False) -> bool:
        """Starts one background refresh of `key`; False when one is already running."""
        with self.lock:
            entry = self.entry(key)
            if entry["running_since"] is not None:
                return False
            started = clock()
            entry["running_since"] = entry["last_start"] = started
            entry["requested"] = True
            argv = self.command(key, cost)
            thread = threading.Thread(target=self.work, args=(key, argv, started, live), daemon=True)
            self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        thread.start()
        return True

    def work(self, key: str, argv: list[str], started: float, live: bool) -> None:
        timeout = TIMEOUTS[key.partition(":")[0]]
        failure, doc = None, None
        try:
            rc, out, err = run_reader(argv, cwd=self.app.main, env=child_env(), timeout=timeout)
            if rc is None:
                failure = {"reason": (err.strip().splitlines() or ["did not run"])[0], "stderr_tail": tail(err)}
            elif rc != 0:
                failure = {"reason": f"exit {rc}", "stderr_tail": tail(err), "stdout_tail": tail(out)}
            else:
                try:
                    doc = json.loads(out)
                except ValueError as exc:
                    failure = {"reason": f"unparsable output: {exc}", "stderr_tail": tail(err or out)}
        except Exception as exc:  # noqa: BLE001 -- the key must leave the running state whatever happens
            failure = {"reason": f"{type(exc).__name__}: {exc}", "stderr_tail": ""}
        with self.lock:
            entry = self.entry(key)
            if failure is None:
                entry.update(doc=doc, fetched_at=started, failure=None, live_at_fetch=live)
            else:
                entry["failure"] = {"command": command_text(argv), **failure}
            entry["running_since"] = None

    def join(self, timeout: float) -> None:
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(timeout)

    def tick(self) -> None:
        now = clock()
        for key in self.BACKGROUND:
            with self.lock:
                entry = self.entry(key)
                due = entry["running_since"] is None and (
                    entry["last_start"] is None or now - entry["last_start"] >= INTERVALS[key])
            if due:
                self.refresh(key)

    def state(self, key: str) -> "views.DocState":
        kind, _, rest = key.partition(":")
        if kind in self.BACKGROUND:
            path = f"/refresh/{kind}"
        elif kind in ("story", "run"):
            path = f"/refresh/{kind}/{rest}"
        elif kind == "stream":
            path = "/refresh/stream/" + rest.replace(":", "/")
        else:
            path = None
        with self.lock:
            entry = dict(self.entries.get(key) or self.blank())
        return views.DocState(key=key, doc=entry["doc"], fetched_at=entry["fetched_at"],
                              running_since=entry["running_since"], failure=entry["failure"],
                              requested=entry["requested"], refresh_path=path, live_at_fetch=entry["live_at_fetch"])

    def ensure(self, key: str, live: bool = False) -> None:
        """Starts a refresh of an on-request document when its copy is missing or past its keep time."""
        kind = key.partition(":")[0]
        with self.lock:
            entry = dict(self.entry(key))
        if entry["running_since"] is not None:
            return
        age = None if entry["last_start"] is None else clock() - entry["last_start"]
        if kind.startswith("metrics"):
            due = age is None
        elif kind == "run" and entry["doc"] is not None and not live:
            due = entry["live_at_fetch"] and (entry["failure"] is None or age >= RUN_TTL)
        else:
            ttl = {"story": STORY_TTL, "run": RUN_TTL, "stream": STREAM_TTL}[kind]
            due = age is None or age >= ttl
        if due:
            self.refresh(key, live=live)


class App:
    def __init__(self, main_repo, port: int):
        self.main = Path(main_repo)
        self.port = port
        self.state_dir = self.main / ".workflow" / "state" / "control-room"
        self.launch_lock = threading.Lock()
        self.docs = Documents(self)

    def start_background(self) -> None:
        def loop():
            while True:
                self.docs.tick()
                time.sleep(1)

        threading.Thread(target=loop, daemon=True, name="control-room-documents").start()


# ---------------------------------------------------------------- requests

class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Request:
    def __init__(self, app: App, method: str, path: str, query: dict, form: dict, hx: bool):
        self.app, self.method, self.path, self.query, self.form, self.hx = app, method, path, query, form, hx

    @property
    def docs(self) -> Documents:
        return self.app.docs

    def state(self, key: str):
        return self.app.docs.state(key)


def page(body: str, status: int = 200):
    return status, body.encode("utf-8"), "text/html; charset=utf-8"


def require(pattern: re.Pattern, value: str, what: str) -> str:
    if not pattern.fullmatch(value or ""):
        raise HttpError(400, f"invalid {what}: {value!r}")
    return value


def story_id_of(req: Request, value: str) -> str:
    return require(STORY_RE, value, "story id")


def known_story(req: Request, story_id: str) -> None:
    for key in ("board", f"story:{story_id}"):
        if views.story_by_id(req.state(key).doc, story_id) is not None:
            return
    raise HttpError(404, f"story {story_id} is not in the latest board document")


def run_row_of(req: Request, run_id: str) -> dict:
    require(RUN_RE, run_id, "run id")
    for row in views.runs_of(req.state("runs")):
        if row.get("run_id") == run_id:
            return row
    raise HttpError(404, f"run {run_id} is not in the latest runs document")


def run_state_name(row: dict) -> str:
    state = row.get("state")
    return state.get("name", "unknown") if isinstance(state, dict) else "unknown"


def get_board(req: Request):
    return page(views.board_page(req.state("board"), req.state("attention"), req.state("cost"), clock()))


def get_board_partial(req: Request):
    return page(views.board_inner(req.state("board"), req.state("attention"), req.state("cost"), clock()))


def story_view(req: Request, value: str, inner: bool):
    story_id = story_id_of(req, value)
    known_story(req, story_id)
    req.docs.ensure(f"story:{story_id}")
    render = views.story_inner if inner else views.story_page
    return page(render(story_id, req.state("board"), req.state(f"story:{story_id}"), req.state("cost"),
                       req.state("attention"), clock()))


def get_story(req: Request, story_id: str):
    return story_view(req, story_id, False)


def get_story_partial(req: Request, story_id: str):
    return story_view(req, story_id, True)


def attention_view(req: Request, inner: bool):
    asked_of = req.query.get("asked_of", "owner")
    if asked_of not in ASKED_OF:
        raise HttpError(400, f"invalid asked_of: {asked_of!r}")
    story = req.query.get("story")
    if story is not None:
        story = story_id_of(req, story)
    render = views.attention_inner if inner else views.attention_page
    return page(render(req.state("attention"), asked_of, story, clock()))


def get_attention(req: Request):
    return attention_view(req, False)


def get_attention_partial(req: Request):
    return attention_view(req, True)


def get_runs(req: Request):
    return page(views.runs_page(req.state("runs"), clock()))


def get_runs_partial(req: Request):
    return page(views.runs_inner(req.state("runs"), clock()))


def get_run(req: Request, run_id: str):
    row = run_row_of(req, run_id)
    req.docs.ensure(f"run:{run_id}", live=run_state_name(row) in views.LIVE_STATES)
    return page(views.run_page(row, req.state("runs"), req.state(f"run:{run_id}"), clock()))


def read_log(row: dict) -> str:
    path = Path(str(row.get("run_dir"))) / "driver.log"
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - LOG_BYTES))
            data = handle.read()
    except OSError:
        return f"none: {views.esc(path)}"
    lines = data.decode("utf-8", "replace").splitlines()[-LOG_LINES:]
    return views.esc("\n".join(lines))


def get_run_log(req: Request, run_id: str):
    row = run_row_of(req, run_id)
    status = 200 if run_state_name(row) in views.LIVE_STATES else 286
    return page(read_log(row), status)


def get_run_sessions(req: Request, run_id: str):
    row = run_row_of(req, run_id)
    key = f"run:{run_id}"
    req.docs.ensure(key, live=run_state_name(row) in views.LIVE_STATES)
    report = req.state(key)
    status = 200 if views.sessions_polling(run_state_name(row), report) else 286
    return page(views.run_sessions_inner(run_id, run_state_name(row), report, clock()), status)


def get_run_stream(req: Request, run_id: str):
    row = run_row_of(req, run_id)
    status = 200 if run_state_name(row) in views.LIVE_STATES else 286
    number = views.stream_session_id(row)
    if number is None:
        return page(views.stream_inner(row, None, None, clock()), status)
    key = f"stream:{run_id}:{number}"
    req.docs.ensure(key)
    return page(views.stream_inner(row, req.state(key), number, clock()), status)


def metrics_state(req: Request, key: str):
    req.docs.ensure(key)
    return req.state(key)


def metrics_run_args(req: Request, run_id: str):
    run_row_of(req, run_id)
    return f"metrics-run:{run_id}", f"/metrics/run/{run_id}/refresh", f"/partials/metrics/run-{run_id}"


def get_metrics_run(req: Request, run_id: str):
    key, refresh, poll = metrics_run_args(req, run_id)
    return page(views.metrics_page(f"Metrics {run_id}", refresh, metrics_state(req, key), clock(), poll,
                                   views.render_metrics_run))


def get_metrics_run_partial(req: Request, run_id: str):
    key, _, poll = metrics_run_args(req, run_id)
    return page(views.metrics_inner(metrics_state(req, key), clock(), poll, views.render_metrics_run))


def get_metrics_compare(req: Request):
    return page(views.metrics_page("Run comparison", "/metrics/compare/refresh",
                                   metrics_state(req, "metrics-compare"), clock(), "/partials/metrics/compare",
                                   views.render_metrics_compare))


def get_metrics_compare_partial(req: Request):
    return page(views.metrics_inner(metrics_state(req, "metrics-compare"), clock(), "/partials/metrics/compare",
                                    views.render_metrics_compare))


def cost_mode(req: Request) -> str:
    mode = req.form.get("cost")
    if mode not in ("cached", "live"):
        raise HttpError(400, f"cost must be cached or live, not {mode!r}")
    return mode


def not_started_note(started: bool, mode: str) -> str:
    return "" if started else f"a refresh is already running; the {mode}-cost request was not started"


def post_metrics_run_refresh(req: Request, run_id: str):
    key, _, poll = metrics_run_args(req, run_id)
    mode = cost_mode(req)
    started = req.docs.refresh(key, cost=mode)
    return page(views.metrics_inner(req.state(key), clock(), poll, views.render_metrics_run,
                                    not_started_note(started, mode)))


def post_metrics_compare_refresh(req: Request):
    mode = cost_mode(req)
    started = req.docs.refresh("metrics-compare", cost=mode)
    return page(views.metrics_inner(req.state("metrics-compare"), clock(), "/partials/metrics/compare",
                                    views.render_metrics_compare, not_started_note(started, mode)))


def post_refresh(req: Request, name: str):
    parts = name.split("/")
    live = False
    run_row = None
    if len(parts) == 1 and parts[0] in Documents.BACKGROUND:
        key = parts[0]
    elif len(parts) == 2 and parts[0] == "story":
        story_id = story_id_of(req, parts[1])
        known_story(req, story_id)
        key = f"story:{story_id}"
    elif len(parts) == 2 and parts[0] == "run":
        run_row = run_row_of(req, parts[1])
        key = f"run:{parts[1]}"
        live = run_state_name(run_row) in views.LIVE_STATES
    elif len(parts) == 3 and parts[0] == "stream":
        run_row_of(req, parts[1])
        require(DIGITS_RE, parts[2], "session number")
        key = f"stream:{parts[1]}:{parts[2]}"
    else:
        raise HttpError(404, f"no document named {name!r}")
    started = req.docs.refresh(key, live=live)
    note = "" if started else "already running"
    if run_row is not None:
        return page(views.run_sessions_block(parts[1], run_state_name(run_row), req.state(key), clock(), note))
    return page(views.doc_head(req.state(key), clock(), note))


def positive(value: str, name: str) -> str:
    if not POSITIVE_RE.fullmatch(value) or not math.isfinite(float(value)) or float(value) <= 0:
        raise HttpError(400, f"{name} must be a positive number, not {value!r}")
    return value


def start_flags(form: dict) -> list[str]:
    def field(name: str) -> str:
        return (form.get(name) or "").strip()

    flags: list[str] = []
    if field("story"):
        flags += ["--story", require(STORY_RE, field("story"), "story id")]
    if field("max_stories"):
        value = require(DIGITS_RE, field("max_stories"), "max_stories")
        if int(value) <= 0:
            raise HttpError(400, f"max_stories must be a positive integer, not {value!r}")
        flags += ["--max-stories", value]
    for name, flag in (("max_cost_usd", "--max-cost-usd"), ("session_timeout_min", "--session-timeout-min"),
                       ("land_stall_min", "--land-stall-min")):
        if field(name):
            flags += [flag, positive(field(name), name)]
    if field("orchestrator_model"):
        flags += ["--orchestrator-model", require(MODEL_RE, field("orchestrator_model"), "orchestrator_model")]
    if field("orchestrator_effort"):
        if field("orchestrator_effort") not in views.EFFORTS:
            raise HttpError(400, f"orchestrator_effort must be one of {', '.join(views.EFFORTS)}")
        flags += ["--orchestrator-effort", field("orchestrator_effort")]
    return flags


def post_run_start(req: Request):
    flags = start_flags(req.form)
    argv = req.docs.driver("start", *flags, "--json")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))
    rc, out, err = launch.run_starter(argv, cwd=req.app.main, env=dict(os.environ), timeout=TIMEOUTS["start"],
                                      out_prefix=req.app.state_dir / f"start-{stamp}-{secrets.token_hex(3)}")
    req.docs.refresh("runs")
    return page(views.run_start_result(rc, parse_json(out), out, tail(err)))


def post_run_stop(req: Request, run_id: str, now: bool):
    run_row_of(req, run_id)
    argv = req.docs.driver("stop", run_id, *(["--now"] if now else []), "--json")
    rc, out, err = run_reader(argv, cwd=req.app.main, timeout=TIMEOUTS["stop"])
    req.docs.refresh("runs")
    return page(views.stop_result(rc, parse_json(out), tail(err)))


def post_run_stop_graceful(req: Request, run_id: str):
    return post_run_stop(req, run_id, False)


def post_run_stop_now(req: Request, run_id: str):
    return post_run_stop(req, run_id, True)


def post_sessions_open(req: Request):
    kind = req.form.get("kind")
    directory = str(req.app.main)
    if kind == "attach":
        ident = require(views.SHORT_SESSION_RE, req.form.get("id", ""), "session id")
        section, _, _ = views.sessions_section(req.state("attention"), req.state("board"))
        entries = section.get("entries", []) if section else []
        entry = next((e for e in entries if isinstance(e, dict) and e.get("id") == ident
                      and e.get("kind") == "background"), None)
        if entry is None:
            raise HttpError(404, f"session {ident} is not a background session in the latest listing")
        directory = str(entry.get("cwd") or "")
        if not directory:
            return page(views.launch_result(launch.refused(f"Session {ident} has no cwd in the listing, so its "
                                                           "window has no directory to open in.")))
        if not Path(directory).is_absolute():
            return page(views.launch_result(launch.refused(f"Session {ident} lists the relative cwd {directory}, so "
                                                           "its window has no directory to open in.")))
    elif kind == "resume":
        run_id = req.form.get("run", "")
        require(RUN_RE, run_id, "run id")
        number = require(DIGITS_RE, req.form.get("session", ""), "session number")
        row = run_row_of(req, run_id)
        report = req.state(f"run:{run_id}")
        if report.doc is None:
            return page(views.launch_result(launch.refused("The run report has not been read yet; open the run page "
                                                           "first.")))
        sessions = views.nested(report.doc, "run", "sessions") or []
        session = next((s for s in sessions if isinstance(s, dict) and str(s.get("session")) == number), None)
        if session is None:
            raise HttpError(404, f"session {number} is not in the report of run {run_id}")
        decision, ident = views.resume_decision(run_state_name(row), session)
        if decision != "resume":
            reasons = {"headless": f"Session {number} is running headless: it cannot be entered.",
                       "no-id": f"Session {number} has no session id (init.session_id): it cannot be resumed.",
                       "undecided": f"The state of session {number} is unknown: it cannot be resumed."}
            return page(views.launch_result(launch.refused(reasons[decision])))
    else:
        raise HttpError(400, f"kind must be attach or resume, not {kind!r}")
    outcome = launch.open_window(req.app.state_dir, lambda: clock(), kind=kind, ident=ident, directory=directory)
    return page(views.launch_result(outcome))


def sprint_lookup(req: Request, story_id: str):
    """(key, refusal outcome or None) for the story's sprint-status row."""
    path = req.app.main / str(wfconfig.get("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml"))
    argv = [sys.executable, str(SCRIPTS_DIR / "sprint_status.py"), "--path", str(path), "get", story_id]
    rc, out, err = run_reader(argv, cwd=req.app.main, timeout=TIMEOUTS["sprint"])
    doc = parse_json(out)
    if rc is None:
        return None, launch.refused(f"The sprint-status lookup did not answer ({tail(err)}); nothing was started.")
    if not isinstance(doc, dict):
        return None, launch.refused(f"The sprint-status lookup exited {rc} with no JSON: {tail(err)}")
    if doc.get("status") != "ok":
        return None, launch.refused(str(doc.get("message") or "sprint-status has no such row"))
    if doc.get("value") in ("done", "superseded"):
        return None, launch.refused(f"{story_id} is {doc.get('value')} in sprint-status; nothing to start.")
    return doc.get("key"), None


def post_sessions_start(req: Request):
    story_id = story_id_of(req, req.form.get("story", ""))
    force = req.form.get("force") == "1"
    with req.app.launch_lock:
        key, refusal = sprint_lookup(req, story_id)
        if refusal is None:
            refusal = check_launch(req, story_id, key)
        overridden = []
        if refusal is not None and force and refusal.get("launch_ids"):
            overridden, refusal = refusal["launch_ids"], None
        if refusal is not None:
            return page(views.launch_result(refusal))
        outcome = launch.start_session(req.app.main, req.app.state_dir, lambda: clock(), story_id, key, overridden)
    if outcome.get("status") != "refused":
        req.docs.refresh("attention")
    return page(views.launch_result(outcome))


def check_launch(req: Request, story_id: str, sprint_key):
    section, generated_at, fetched_at = views.sessions_section(req.state("attention"), req.state("board"))
    if section is None:
        return launch.refused("No session listing has been read, so an existing session of the story cannot be "
                              "ruled out; refresh the attention document first.")
    existing = launch.find_duplicate(section.get("entries", []), story_id, sprint_key)
    if existing is not None:
        return launch.refused(f"{story_id} already has a session in the listing.", existing=existing)
    launch.settle_exited_launches(req.app.state_dir, lambda: clock(), story_id, sprint_key)
    blockers = launch.blocking_launches(req.app.state_dir, story_id, fetched_at, sprint_key)
    if not blockers:
        return None
    blocker = blockers[0]
    skippable = {"launch_ids": [b["launch_id"] for b in blockers], "story": story_id}
    if blocker["ended"] is None:
        return launch.refused(f"A launch of {story_id} ({blocker['launch_id']}) has not ended: its start "
                              f"process (pid {blocker['pid']}) is still running, so its session may not be in the "
                              "listing yet; wait for the process to exit, then for the next listing.", **skippable)
    return launch.refused(f"A launch of {story_id} ended after the refresh that produced the session listing began "
                          f"({launch.iso(fetched_at)}, the earliest refresh start among the cached documents; "
                          f"listing generated {generated_at}), so the listing may not show its session; wait for the "
                          "next listing before starting again.", **skippable)


def get_static(req: Request, name: str):
    content_type = STATIC_FILES.get(name)
    if content_type is None:
        raise HttpError(404, f"no static file {name!r}")
    return 200, (STATIC_DIR / name).read_bytes(), content_type


LITERAL = {
    "/": {"GET": get_board},
    "/partials/board": {"GET": get_board_partial},
    "/attention": {"GET": get_attention},
    "/partials/attention": {"GET": get_attention_partial},
    "/runs": {"GET": get_runs},
    "/partials/runs": {"GET": get_runs_partial},
    "/runs/start": {"POST": post_run_start},
    "/metrics/compare": {"GET": get_metrics_compare},
    "/partials/metrics/compare": {"GET": get_metrics_compare_partial},
    "/metrics/compare/refresh": {"POST": post_metrics_compare_refresh},
    "/sessions/open": {"POST": post_sessions_open},
    "/sessions/start": {"POST": post_sessions_start},
}
DYNAMIC = [
    (re.compile(r"/story/([^/]*)"), {"GET": get_story}),
    (re.compile(r"/partials/story/([^/]*)"), {"GET": get_story_partial}),
    (re.compile(r"/runs/([^/]*)"), {"GET": get_run}),
    (re.compile(r"/runs/([^/]*)/stop"), {"POST": post_run_stop_graceful}),
    (re.compile(r"/runs/([^/]*)/stop-now"), {"POST": post_run_stop_now}),
    (re.compile(r"/partials/runs/([^/]*)/log"), {"GET": get_run_log}),
    (re.compile(r"/partials/runs/([^/]*)/sessions"), {"GET": get_run_sessions}),
    (re.compile(r"/partials/runs/([^/]*)/stream"), {"GET": get_run_stream}),
    (re.compile(r"/metrics/run/([^/]*)"), {"GET": get_metrics_run}),
    (re.compile(r"/metrics/run/([^/]*)/refresh"), {"POST": post_metrics_run_refresh}),
    (re.compile(r"/partials/metrics/run-([^/]*)"), {"GET": get_metrics_run_partial}),
    (re.compile(r"/refresh/(.+)"), {"POST": post_refresh}),
    (re.compile(r"/static/(.*)"), {"GET": get_static}),
]


def resolve(method: str, path: str):
    """(handler, groups) for a request, or the status 404 / 405."""
    literal = LITERAL.get(path)
    if literal is not None:
        handler = literal.get(method)
        return (handler, ()) if handler else 405
    path_matched = False
    for pattern, handlers in DYNAMIC:
        match = pattern.fullmatch(path)
        if match:
            path_matched = True
            if method in handlers:
                return handlers[method], match.groups()
    return 405 if path_matched else 404


class Handler(BaseHTTPRequestHandler):
    server_version = "ControlRoom"

    def log_message(self, format, *args):  # noqa: A002 -- the signature is the base class's
        return

    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")

    def do_PUT(self):
        self.serve("PUT")

    def do_DELETE(self):
        self.serve("DELETE")

    def do_PATCH(self):
        self.serve("PATCH")

    def send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status: int, message: str, method: str) -> None:
        as_page = method == "GET" and self.headers.get("HX-Request") != "true"
        self.send(status, views.error_page(status, message, as_page).encode("utf-8"), "text/html; charset=utf-8")

    def serve(self, method: str) -> None:
        app: App = self.server.app
        try:
            self.send_checked(app, method)
        except HttpError as exc:
            self.fail(exc.status, exc.message, method)
        except Exception as exc:  # noqa: BLE001 -- a handler bug answers 500 instead of dropping the connection
            self.fail(500, f"{type(exc).__name__}: {exc}", method)

    def send_checked(self, app: App, method: str) -> None:
        origins = (f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}")
        if self.headers.get("Host") not in tuple(o.split("//", 1)[1] for o in origins):
            raise HttpError(403, "Host header is not this server")
        form: dict = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > BODY_CAP:
                raise HttpError(413, "request body too large")
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            if self.headers.get("Origin") not in origins:
                raise HttpError(403, "Origin is not this server")
            if self.headers.get("HX-Request") != "true":
                raise HttpError(403, "POST needs HX-Request: true")
            form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        split = urlsplit(self.path)
        resolved = resolve(method, split.path)
        if resolved == 404:
            raise HttpError(404, f"no route for {split.path}")
        if resolved == 405:
            raise HttpError(405, f"{method} is not allowed on {split.path}")
        handler, groups = resolved
        query = {k: v[0] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
        request = Request(app, method, split.path, query, form, self.headers.get("HX-Request") == "true")
        status, body, content_type = handler(request, *groups)
        self.send(status, body, content_type)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def create_server(app: App, port: int) -> Server:
    server = Server(("127.0.0.1", port), Handler)
    server.app = app
    app.port = server.server_address[1]
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve the localhost control room.")
    parser.add_argument("--port", type=int, default=int(wfconfig.get("control_room.port", DEFAULT_PORT) or DEFAULT_PORT))
    args = parser.parse_args(argv)
    main_repo = wfconfig.repo_root()
    if not (main_repo / ".git").exists():
        print(f"control room: {main_repo} is not the main checkout (no .git)", file=sys.stderr)
        return 2
    app = App(main_repo, args.port)
    try:
        server = create_server(app, args.port)
    except OSError as exc:
        print(f"control room: cannot bind 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 2
    try:
        print(f"control room on http://127.0.0.1:{server.server_address[1]}/", flush=True)
        app.start_background()
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
