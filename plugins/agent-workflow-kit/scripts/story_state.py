#!/usr/bin/env python3
"""One JSON snapshot of every in-flight story and every auto-dev driver run.

Purpose
-------
The read model of the control room (`control_room.py`) and of `sprint-next` / `auto-dev-run`, built
from files the harness already writes. Per in-flight story: slot, worktree, stack, pipeline
position, record and artifacts, commits and files in progress, agents, cost. Per driver run: log,
finished sessions, spend, the session still streaming. Per listed agent session (`sessions`): its
id, name, cwd and state, and the story it belongs to: by driver-run name (`<run-id>-s<n>`,
`<story>-<run-id>-s<n>`), by story-id name or by worktree cwd; never guessed, and `story: null`
with an `unlinked_reason` when none applies. Each story lists its `session_ids`. The `attention`
section is one queue of everything that waits on the owner or on an external owner (the
design-question tracker): sessions marked `waiting` / `failed` / `unknown` from the listing and the
Notification events, and the questions, halts and undecided rulings the harness wrote to files.
Each entry names its story (or null), its source (file and line, or session), what is asked and
whom it waits on (`asked_of`: `owner` or `external`); `sources` names the state of each source.

Sources, each read by the tool that already owns it:
  the slot registry (`config: paths.slot_registry`), sprint status (`sprint_status.py` as a
  subprocess), `.workflow/state/story-setup/*`, `{compose} ps` (the stack's port variables set;
  SKIPPED when `config: stack.runtime = "none"`), git, `story_ledger.py` (position as a
  subprocess; handoff, record, artifacts in-process), `.workflow/state/journal/*.jsonl`
  (dispatches; the agent_started/agent_stopped facts of `hooks/session/session_journal.py`),
  Claude Code subagent `.meta.json` files under `config: transcripts.claude_root` (this
  project's dirs only), `harness_metrics.py --json` (cost), `.workflow/state/auto-dev/` (driver
  runs), the session-listing probe (`config: story_state.sessions_cmd`; default
  `claude agents --json --all` when `config: loop.driver_tool` is claude, "unavailable" otherwise),
  `.workflow/state/attention/<session>.jsonl` (the Notification events `notification.py` writes),
  the failure log (`config: paths.auto_dev_failure_log`), the propagation checklists
  (`.workflow/state/story-setup/propagation-*.md`, then `<story history>/<id>/propagation-*.md`),
  the optional design-question tracker (`config: paths.design_questions`), and each story's spec
  and record (the `attention` section). An optional source that is not configured is a `none`
  section that says so ("n/a").

Every named field of the output is a section: an object with `status` and `source`.
  "ok"    data under the section's keys;
  "none"  `reason`: read, and nothing exists for this story (what was looked for);
  "error" `error`: the read failed (exception text; exit code plus a stderr tail of at most
          300 characters; "timed out after <n> s").
A section never holds a bare empty value; `[]` appears only inside an "ok" section as a real
zero. Sections built from Claude Code's internal storage (transcripts, `.meta.json`, the cost
report) carry `"format": "internal"`.

Usage
-----
    uv run --no-project <plugin-root>/scripts/story_state.py                  # every in-flight story
    uv run --no-project <plugin-root>/scripts/story_state.py --story 1-2      # replaces the set; repeatable
    uv run --no-project <plugin-root>/scripts/story_state.py --attention      # only the sessions and the attention queue
    uv run --no-project <plugin-root>/scripts/story_state.py --no-cost        # no cost read

`--no-cost` skips the `harness_metrics.py --json` subprocess (the slowest read): each story's `cost` reads
`{"status": "none", "source": "harness_metrics.py", "reason": "not read: --no-cost"}`. It combines with
`--story` and is a usage error with `--attention`.

`--attention` prints `{generated_at, main_repo, sources, sessions, attention}` and builds no ledger,
compose, cost or commit data. It cannot be combined with `--story`.

Prints one JSON document to stdout (UTF-8) and exits 0 whenever it is printed, source errors
included; exits 2 with a JSON error line when the main checkout cannot be located or on a usage
error. Stdlib only; read-only.

Importable: `build_snapshot(story_ids, with_cost)`, `build_attention()`, `waits_on(fields, rows)`
(the one waits-on rule; `resume_halted.py` may share it), `failure_blocks(lines)`.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import _design  # noqa: E402
import _stack  # noqa: E402
import _transcripts  # noqa: E402
import harness_metrics  # noqa: E402
import story_ledger as sl  # noqa: E402
import wfconfig  # noqa: E402

MAIN_REPO: Path | None = None
REGISTRY_PATH: Path | None = None
TRACKER_PATH: Path | None = None
HISTORY_DIR: Path | None = None
SETUP_DIR: Path | None = None
ARTS_REL = "docs/implementation-artifacts"
DEFERRED_REL = "docs/implementation-artifacts/deferred-work"
AUTO_DEV_DIR: Path | None = None
ATTENTION_DIR: Path | None = None
EXTERNAL_PATH: Path | None = None   # the design-question tracker; None when not configured
FAILURES_PATH: Path | None = None
BASE_BRANCH = "development"
BRANCH_PREFIX = "story/"
EXTERNAL_TAGS: list[str] = []
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
OPEN_Q_EXTERNAL_RE: re.Pattern | None = None

GIT_TIMEOUT_S = 30
COMPOSE_TIMEOUT_S = 60
TRACKER_TIMEOUT_S = 30
# Hang detectors: the ledger bound is per story, the cost bound covers one harness_metrics run over the set.
LEDGER_TIMEOUT_S = 60
COST_TIMEOUT_S = 900
SESSIONS_TIMEOUT_S = 45
CLAUDE_SESSIONS_ARGV = ["claude", "agents", "--json", "--all"]
SESSIONS_SOURCE = "session listing"
LISTING_FIELDS = ("id", "sessionId", "name", "kind", "cwd", "startedAt", "state", "status", "waitingFor", "pid")

MAX_ERR = 300
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}$")
ROUND_HEADING_RE = re.compile(r"^## review round (\d+)\b", re.MULTILINE)  # lowercased text
# `auto_dev_driver.py` names its sessions `<run-id>-s<n>` or `<story>-<run-id>-s<n>`.
DRIVER_NAME_RE = re.compile(r"^(?:(?P<story>.+)-)?(?P<run>\d{8}-\d{6})-s(?P<n>\d+)$")
# Claude Code's variant of a name another live session uses: the name plus two lowercase words.
DUPLICATE_NAME_RE = re.compile(r"^(?P<base>.+)-[a-z]+-[a-z]+$")
SESSIONS_NOTE = ("The listing cannot show a session whose process has ended; a headless (-p) session is listed "
                 "only sometimes; a stopped background session may still read `done`, and only the missing pid "
                 "tells it from a live one. Nothing is inferred beyond the listing.")
RUNNING_NOTE = ("Running = the agent's latest start/stop fact is agent_started (SubagentStart/SubagentStop "
                "hook facts), or it is agent_stopped and the agent's own transcript holds an assistant record "
                "after that stop (resumed_after_stop: true; this is how an agent past a blocked stop is "
                "listed). A session killed mid-agent never writes agent_stopped, so such an agent stays "
                "listed; last_activity shows how stale it is. An agent with no agent_started fact (no SubagentStart hook) "
                "is not listed.")
DISPATCH_NOTE = ("Dispatch facts land when the dispatching turn ends, so a dispatch made in the current turn "
                 "is not listed yet.")


def _rel(key: str, default: str) -> str:
    return str(wfconfig.get(key, default) or default)


def configure(main_repo: Path) -> None:
    """Point every path the script reads at `main_repo` (the main checkout), from the config."""
    global MAIN_REPO, REGISTRY_PATH, TRACKER_PATH, HISTORY_DIR, SETUP_DIR, ARTS_REL, DEFERRED_REL, AUTO_DEV_DIR
    global ATTENTION_DIR, EXTERNAL_PATH, FAILURES_PATH, BASE_BRANCH, BRANCH_PREFIX, EXTERNAL_TAGS, TICKET_RE
    global OPEN_Q_EXTERNAL_RE
    MAIN_REPO = Path(main_repo)
    REGISTRY_PATH = MAIN_REPO / _rel("paths.slot_registry", "docs/implementation-artifacts/slot-registry.md")
    TRACKER_PATH = MAIN_REPO / _rel("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml")
    HISTORY_DIR = MAIN_REPO / _rel("paths.story_history_dir", "docs/implementation-artifacts/story-history")
    ARTS_REL = _rel("paths.specs_dir", "docs/implementation-artifacts")
    DEFERRED_REL = _rel("paths.deferred_work_dir", f"{ARTS_REL}/deferred-work")
    state = MAIN_REPO / ".workflow" / "state"
    SETUP_DIR = state / "story-setup"
    AUTO_DEV_DIR = state / "auto-dev"
    ATTENTION_DIR = state / "attention"
    tracker = str(wfconfig.get("paths.design_questions", "") or "")
    EXTERNAL_PATH = MAIN_REPO / tracker if tracker else None
    FAILURES_PATH = MAIN_REPO / _rel("paths.auto_dev_failure_log", "docs/implementation-artifacts/auto-dev-failures.md")
    BASE_BRANCH = _rel("git.base_branch", "development")
    BRANCH_PREFIX = str(wfconfig.get("git.branch_prefix", "story/") or "")
    EXTERNAL_TAGS = [str(t) for t in (wfconfig.get("spec.external_question_tags", []) or []) if str(t).strip()]
    TICKET_RE = _design.ticket_re()
    # An `[OPEN-Q <n> ...]` marker tagged for an external owner: `(<tag>` inside the brackets or right after them
    # (the same rule as `story_record.py`).
    OPEN_Q_EXTERNAL_RE = (re.compile(r"\[OPEN-Q \d+[^\]\n]*?\((?:%s)\b[^\]\n]*\]|\[OPEN-Q \d+[^\]\n]*\][`*]*\s*\((?:%s)\b"
                                     % ("|".join(map(re.escape, EXTERNAL_TAGS)), "|".join(map(re.escape, EXTERNAL_TAGS))))
                          if EXTERNAL_TAGS else None)


def arts_of(root) -> str:
    return str(Path(root) / ARTS_REL)


def root_of_arts(arts) -> Path:
    """The checkout an artifacts dir (`arts_of`) belongs to."""
    root = Path(arts)
    for _ in Path(ARTS_REL).parts:
        root = root.parent
    return root


def story_branch(story_id: str) -> str:
    return f"{BRANCH_PREFIX}{story_id}"


def is_epic_key(key: str) -> bool:
    """Sprint-status rows that are no story (an epic row or its retrospective)."""
    return key.startswith("epic-") or key.endswith("-retrospective")


def run(argv, *, cwd=None, env=None, timeout):
    """(returncode, stdout, stderr); a timeout or an unstartable command has returncode None."""
    try:
        r = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout:g} s"
    except OSError as exc:
        return None, "", f"{type(exc).__name__}: {exc}"
    return r.returncode, r.stdout, r.stderr


# ---------------------------------------------------------------- sections

def ok(source, **data) -> dict:
    return {"status": "ok", "source": str(source), **data}


def none(source, reason: str, **data) -> dict:
    return {"status": "none", "source": str(source), "reason": reason, **data}


def err(source, error: str) -> dict:
    return {"status": "error", "source": str(source), "error": error}


def argv_text(argv) -> str:
    return " ".join(str(a) for a in argv)


def cmd_error(argv, rc, stderr: str) -> str:
    if rc is None:
        return f"{argv_text(argv)}: {stderr}"
    return f"{argv_text(argv)} exit {rc}: {stderr.strip()[-MAX_ERR:]}"


def guard(source, name: str, fn) -> dict:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- a failing reader becomes its section's error
        return err(source, f"{name}: {type(exc).__name__}: {exc}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mtime_iso(path) -> str | None:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat()
    except OSError:
        return None


def local_stamp_utc(stamp: str) -> str | None:
    """A timestamp with no offset (`log_failure.py` writes `datetime.now()`) read as local time and given
    in UTC; None when it does not parse."""
    try:
        return datetime.fromisoformat(stamp).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path))).replace("\\", "/")


def within(path: str, root: str) -> bool:
    """`path` and `root` are `norm()` output: equal, or `path` lies under `root` by path component."""
    return path == root or path.startswith(root + "/")


def read_jsonl(path, *, strict: bool) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                if strict:
                    raise ValueError(f"{path} line {n} is not JSON")
                continue
            if isinstance(obj, dict):
                rows.append(obj)
            elif strict:
                raise ValueError(f"{path} line {n} is not a JSON object")
    return rows


# ---------------------------------------------------------------- global sources

def read_registry() -> tuple[list[dict] | None, str, dict]:
    """(rows, raw text, `sources.slot_registry` section)."""
    try:
        text = REGISTRY_PATH.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return None, "", err(REGISTRY_PATH, f"cannot read the slot registry: {exc}")
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and cells[0].isdigit():
            rows.append({"slot": int(cells[0]), "status": cells[1], "story_id": cells[2], "branch": cells[3],
                         "worktree": cells[4], "since": cells[5]})
    in_use = sum(1 for r in rows if r["status"] == "in_use")
    return rows, text, ok(REGISTRY_PATH, rows=len(rows), in_use=in_use)


def read_tracker() -> tuple[list[dict] | None, dict]:
    """(every sprint-status row, `sources.sprint_status` section) via `sprint_status.py list --json`."""
    argv = [sys.executable, SCRIPTS_DIR / "sprint_status.py", "--path", TRACKER_PATH, "list", "--json"]
    rc, out, stderr = run(argv, timeout=TRACKER_TIMEOUT_S)
    if rc is None:
        return None, err(TRACKER_PATH, cmd_error(argv, rc, stderr))
    try:
        doc = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except ValueError:
        doc = {}
    if rc != 0 or doc.get("status") != "ok":
        return None, err(TRACKER_PATH, doc.get("message") or cmd_error(argv, rc, stderr))
    return doc["rows"], ok(TRACKER_PATH, rows=len(doc["rows"]))


def read_tracker_details(keys: list[str]) -> dict[str, dict]:
    """key -> the `sprint_status.py get` line (an `ok` line, or an `error` line / a synthetic one)."""
    if not keys:
        return {}
    argv = [sys.executable, SCRIPTS_DIR / "sprint_status.py", "--path", TRACKER_PATH, "get", *keys]
    rc, out, stderr = run(argv, timeout=TRACKER_TIMEOUT_S)
    details: dict[str, dict] = {}
    for line in out.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("key"):
            details[obj["key"]] = obj
    for key in keys:
        details.setdefault(key, {"status": "error", "key": key, "message": cmd_error(argv, rc, stderr)})
    return details


def git_worktrees() -> tuple[list[dict] | None, str]:
    argv = ["git", "-C", MAIN_REPO, "worktree", "list", "--porcelain"]
    rc, out, stderr = run(argv, timeout=GIT_TIMEOUT_S)
    if rc != 0:
        return None, cmd_error(argv, rc, stderr)
    rows, cur = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):], "branch": None}
            rows.append(cur)
        elif line.startswith("branch ") and cur is not None:
            cur["branch"] = line[len("branch "):]
    return rows, ""


def parse_ts(value) -> datetime | None:
    """A journal (`+00:00`) or transcript (`Z`) timestamp as an aware datetime; None when it does not parse."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def assistant_after(transcript: str, stop: datetime) -> bool:
    """True when the transcript holds an `assistant` record timestamped after `stop`."""
    try:
        if os.path.getmtime(transcript) < stop.timestamp():
            return False
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "assistant":
                    ts = parse_ts(obj.get("timestamp"))
                    if ts is not None and ts > stop:
                        return True
    except OSError:
        return False
    return False


def journal_dir() -> str:
    return sl.journal_dir()


def scan_running() -> tuple[list[dict] | None, str, list[str]]:
    """(agents, error, unreadable): the running agents from the `.sub-` journal files.

    An agent runs when its latest start/stop fact is `agent_started`, or is `agent_stopped` with an
    `assistant` record in the agent's own transcript after that stop. `unreadable` names each journal
    file that could not be read; its agents are not listed.
    """
    jdir = journal_dir()
    if not os.path.isdir(jdir):
        return None, f"journal dir {jdir} does not exist", []
    agents, unreadable = [], []
    meta_index: dict[tuple[str, str], str] = {}
    for project in _transcripts.project_dirs():
        for mp in sorted(glob.glob(os.path.join(glob.escape(project), "*", "subagents", "**",
                                                "agent-*.meta.json"), recursive=True)):
            parts = Path(mp).relative_to(project).parts
            meta_index.setdefault((parts[0], parts[-1][len("agent-"):-len(".meta.json")]), mp)
    for fp in sorted(glob.glob(os.path.join(glob.escape(jdir), "*.sub-*.jsonl"))):
        session, _, agent_id = os.path.basename(fp)[:-len(".jsonl")].partition(".sub-")
        try:
            events = [f for f in read_jsonl(fp, strict=False) if f.get("kind") in ("agent_started", "agent_stopped")]
        except (OSError, ValueError) as exc:
            unreadable.append(f"{os.path.basename(fp)}: {type(exc).__name__}: {exc}")
            continue
        started = [e for e in events if e["kind"] == "agent_started"]
        if not started:
            continue
        last = started[-1]
        meta_path = meta_index.get((session, agent_id))
        meta, run_id, session_dir, transcript = {}, None, None, None
        if meta_path:
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                meta = {}
            folder = Path(meta_path).parent
            if folder.parent.name == "workflows":
                run_id = folder.name
            session_dir = Path(meta_path).parents[1 if run_id is None else 3]
            transcript = meta_path[:-len(".meta.json")] + ".jsonl"
        resumed = False
        if events[-1]["kind"] == "agent_stopped":
            stop = parse_ts(events[-1].get("ts"))
            if not (transcript and stop and assistant_after(transcript, stop)):
                continue
            resumed = True
        agents.append({
            "agent_id": agent_id, "agent_type": last.get("agent_type"), "session_id": session,
            "started": last.get("ts"), "cwd": last.get("cwd"),
            "description": meta.get("description"), "workflow_run": run_id,
            "last_activity": mtime_iso(transcript) if transcript else None,
            "meta_found": bool(meta_path), "resumed_after_stop": resumed, "format": "internal",
            "_session_dir": str(session_dir) if session_dir else None,
        })
    return agents, "", unreadable


# ---------------------------------------------------------------- the story set

def variants_of(story_id: str, key: str | None) -> list[str]:
    out = set(sl.variants(story_id))
    short = harness_metrics.short_id(key) if key else None
    if short:
        out.update(sl.variants(short))
    return sorted(out)


def join_key(story_id: str, tracker_keys: list[str] | None) -> tuple[str, str | None, str]:
    """(state, key, text): `ok` + key, `none` + reason, or `error` + message."""
    if tracker_keys is None:
        return "error", None, "the tracker could not be read"
    exact = [k for k in tracker_keys if k.lower() == story_id.lower()]
    if exact:
        return "ok", exact[0], ""
    vs = sl.variants(story_id)
    hits = [k for k in tracker_keys if sl.mentions(k, vs)]
    if len(hits) == 1:
        return "ok", hits[0], ""
    if not hits:
        return "none", None, f"no sprint-status key names story {story_id}"
    return "error", None, f"story {story_id} matches {len(hits)} sprint-status keys: {', '.join(hits)}"


def story_set(story_ids, registry, tracker, tracker_keys) -> list[dict]:
    entries: dict[str, dict] = {}
    if story_ids:
        for sid in story_ids:
            entries.setdefault(sid, {"story_id": sid, "because": ["--story"]})
    else:
        for row in registry or []:
            if row["status"] == "in_use":
                entries.setdefault(row["story_id"], {"story_id": row["story_id"], "because": [f"slot {row['slot']} in_use"]})
        by_key = {}
        for entry in entries.values():
            state, key, _ = join_key(entry["story_id"], tracker_keys)
            if state == "ok":
                by_key[key] = entry
        for row in tracker or []:
            key = row["key"]
            if row["status"] not in ("in-progress", "review", "blocked") or is_epic_key(key):
                continue
            sid = harness_metrics.short_id(key) or key
            entry = by_key.get(key) or next((e for e in entries.values() if e["story_id"].lower() == sid.lower()), None)
            if entry is None:
                entry = entries.setdefault(sid, {"story_id": sid, "because": []})
            entry["because"].append(f"sprint-status {row['status']}")
    return [entries[k] for k in sorted(entries)]


# ---------------------------------------------------------------- per-story readers

def find_registry_row(registry, vs: list[str], key: str | None, tracker_keys) -> dict | None:
    wanted = {v.lower() for v in vs}
    for row in registry or []:
        if row["status"] != "in_use":
            continue
        if row["story_id"].lower() in wanted:
            return row
        if key and join_key(row["story_id"], tracker_keys)[1] == key:
            return row
    return None


def slot_section(sid, row, registry_text, registry_err) -> dict:
    if registry_err:
        return registry_err
    project = wfconfig.stack_name(row['story_id'] if row else sid)
    if row is None:
        return none(REGISTRY_PATH, f"no in_use row for {sid} in {REGISTRY_PATH}")
    return ok(REGISTRY_PATH, slot=row["slot"], branch=row["branch"], worktree=row["worktree"],
              since=row["since"], project=project)


def worktree_section(sid, vs, row, worktrees, worktrees_err) -> tuple[dict, str | None]:
    """(section, usable worktree path or None)."""
    if row is not None:
        path = row["worktree"]
        return ok(REGISTRY_PATH, path=path, found_by="slot-registry", exists=os.path.isdir(path)), path
    if worktrees is None:
        return err("git worktree list", worktrees_err), None
    wanted = {f"refs/heads/{story_branch(v)}" for v in vs}
    for wt in worktrees:
        if wt["branch"] in wanted:
            return ok("git worktree list", path=wt["path"], found_by="git worktree list",
                      exists=os.path.isdir(wt["path"])), wt["path"]
    branch = next((story_branch(v) for v in vs if branch_exists(story_branch(v))), None)
    reason = (f"no worktree; branch {branch} exists" if branch
              else f"no worktree; no branch {story_branch(sid)} (looked for refs/heads/{BRANCH_PREFIX}{{{','.join(vs)}}})")
    return none("git worktree list", reason, branch=branch, branch_exists=bool(branch)), None


def branch_exists(branch: str) -> bool:
    """True when `refs/heads/<branch>` exists, False when git says it does not; any other outcome raises."""
    argv = ["git", "-C", MAIN_REPO, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
    rc, _, stderr = run(argv, timeout=GIT_TIMEOUT_S)
    if rc == 0:
        return True
    if rc == 1:
        return False
    raise RuntimeError(cmd_error(argv, rc, stderr))


def bring_up_section(project, worktree) -> dict:
    path = SETUP_DIR / f"{project}.json"
    if not path.is_file():
        return none(path, f"no {path.name} in {path.parent}")
    try:
        return ok(path, state=json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        return err(path, f"cannot parse {path.name}: {exc}")


def graph_section(project, worktree) -> dict:
    if str(wfconfig.get("graph.tool", "none")) == "none":
        return none("config graph.tool", "n/a: config graph.tool is \"none\"")
    path = SETUP_DIR / f"{project}.graph_project"
    if not path.is_file():
        return none(path, f"no {path.name} in {path.parent}")
    try:
        line = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        return err(path, f"cannot read {path.name}: {exc}")
    if not line:
        return err(path, "indexing failed (empty sidecar)")
    return ok(path, graph_project=line)


def parse_json_lines(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return [o for o in json.loads(text) if isinstance(o, dict)]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def services_section(project, slot_info, worktree_path, worktree) -> dict:
    """`worktree_path` is the story's checked worktree (None when it has none); `worktree` is its section."""
    if not wfconfig.stack_enabled():
        return none("config stack.runtime", "n/a: config stack.runtime is \"none\" (no isolated stack)")
    if slot_info is not None and worktree_path:
        argv = _stack.compose_argv(project, "ps", "-a", "--format", "json")
        env = wfconfig.compose_env(int(slot_info["slot"]))
        rc, out, stderr = run(argv, cwd=_stack.compose_dir(Path(worktree_path)), env=env, timeout=COMPOSE_TIMEOUT_S)
    else:
        argv = [_stack.engine_cmd(), "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                "--format", "{{json .}}"]
        rc, out, stderr = run(argv, timeout=COMPOSE_TIMEOUT_S)
    if rc != 0:
        return err(argv_text(argv), cmd_error(argv, rc, stderr))
    try:
        rows = parse_json_lines(out)
    except ValueError as exc:
        return err(argv_text(argv), f"unparsable container-engine output: {exc}")
    if not (slot_info is not None and worktree_path):
        containers = [{"name": r.get("Names"), "state": r.get("State"), "status": r.get("Status")} for r in rows]
        if slot_info is None:
            why = "the story has no slot, so no port variables to judge a stack by"
        elif worktree["status"] == "ok":
            why = (f"the story's worktree directory is missing ({worktree['path']}), so compose has no "
                   f"project directory to run in")
        elif worktree["status"] == "error":
            why = (f"the story's worktree could not be read ({worktree['error']}), so compose has no "
                   f"project directory to run in")
        else:
            why = "the story has no worktree, so compose has no project directory to run in"
        return ok(argv_text(argv), services=containers, ready=f"not computed: {why}")
    listed = {r.get("Service") for r in rows if r.get("Service")}
    return ok(argv_text(argv),
              services=[{"service": r.get("Service"), "state": r.get("State"), "health": r.get("Health"),
                         "exit_code": r.get("ExitCode")} for r in rows],
              ready=sorted(_stack.ready_services(rows)),
              missing=sorted(set(_stack.expected_services()) - listed))


def ledger_abs(rel: str) -> str:
    return os.path.normpath(os.path.join(str(wfconfig.repo_root()), rel))


def record_section(vs, arts) -> tuple[dict, list[str] | None]:
    source = f"{arts}/<{'|'.join(vs)}>-record.md"

    def read():
        rel, lines = sl.read_record(vs, arts)
        if rel is None:
            return none(source, f"no {vs[0]}-record.md in {arts}"), []
        if not lines:
            return none(ledger_abs(rel), f"record has no entries: {ledger_abs(rel)} holds no '- ' lines"), []
        return ok(ledger_abs(rel), path=ledger_abs(rel), lines=lines, last=lines[-1]), lines

    try:
        return read()
    except Exception as exc:  # noqa: BLE001
        return err(source, f"read_record: {type(exc).__name__}: {exc}"), None


def position_section(sid, vs, has_row, record_lines, arts) -> dict:
    argv = [sys.executable, SCRIPTS_DIR / "story_ledger.py", sid]
    rc, out, stderr = run(argv, timeout=LEDGER_TIMEOUT_S)
    if rc != 0:
        return err(argv_text(argv), cmd_error(argv, rc, stderr))
    lines = out.splitlines()
    pos = next((l for l in lines if l.startswith("**Position:**")), None)
    if pos is None:
        return err(argv_text(argv), "no **Position:** line in the ledger output")
    res = next((l for l in lines if l.startswith("**RESUMABLE")), "")
    line = pos.replace("**Position:**", "", 1).strip()
    m = re.search(r"rounds observed: (\d+)", line)
    basis = ("the ledger resolves the story's record from its slot-registry worktree" if has_row else
             "the ledger's record-derived parts (`next (record)`, RESUMABLE) read the main checkout's artifacts; "
             f"the record and handoff below read {arts}")
    data = {"line": line, "resumable": res.replace("**", "").strip(), "rounds_observed": int(m.group(1)) if m else None,
            "recorded_rounds": sorted(sl.recorded_rounds(record_lines or [])), "basis": basis}
    if not m:
        data["rounds_observed_reason"] = f"the position line has no 'rounds observed: <n>': {line}"
    if record_lines is None:
        data["recorded_rounds_note"] = "the record could not be read; see the record section"
    return ok(argv_text(argv), **data)


def handoff_section(vs, record_lines, arts) -> dict:
    if record_lines is None:
        return err("story_ledger.handoff_state", "the record could not be read; see the record section")
    rows = sl.handoff_state(vs, record_lines, arts)
    return ok("story_ledger.handoff_state",
              rows=[{"label": label, "present": present, "detail": detail} for label, present, detail in rows])


def spec_section(vs, arts) -> dict:
    """The first `<id>*.md` that is a spec (`find_artifacts`' filter) and names the story at an id boundary."""
    rejected: list[str] = []
    for v in vs:
        for p in sorted(glob.glob(os.path.join(glob.escape(arts), f"{v}*.md"))):
            name = os.path.basename(p)
            if not sl._is_spec(p):
                continue
            if sl.mentions(name, vs):
                return ok(p, path=os.path.normpath(p))
            if name not in rejected:
                rejected.append(name)
    reason = f"no spec matching {{{','.join(vs)}}}*.md in {arts}"
    if rejected:
        reason += (f"; rejected at the id boundary (the id must not continue into a digit after `-`/`.` or "
                   f"into attached characters): {', '.join(rejected)}")
    return none(arts, reason)


def findings_section(vs, arts) -> dict:
    paths = sorted({p for v in vs for p in glob.glob(os.path.join(glob.escape(arts), f"{v}*code-review-findings*.md"))
                    if sl.mentions(os.path.basename(p), vs)})
    if not paths:
        return none(arts, f"no {{{','.join(vs)}}}*code-review-findings*.md in {arts}")
    files = []
    for p in paths:
        body = Path(p).read_text(encoding="utf-8", errors="replace").lower()
        rounds = sorted({int(m.group(1)) for m in ROUND_HEADING_RE.finditer(body)})
        files.append({"path": p, "rounds": rounds,
                      "triaged_rounds": [n for n in rounds if sl.round_triaged(body, n)]})
    return ok(arts, files=files)


def deferred_section(vs, arts) -> dict:
    deferred = root_of_arts(arts) / DEFERRED_REL
    for v in vs:
        path = deferred / f"story-{v}.md"
        if not path.is_file():
            continue
        sections = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## "):
                sections.append({"heading": line[3:].strip(), "entries": 0})
            elif line.startswith("### ") and sections:
                sections[-1]["entries"] += 1
        return ok(path, sections=sections)
    return none(deferred, f"no story-{{{','.join(vs)}}}.md in {deferred}")


PROPAGATION_ROW_RE = re.compile(r"^\|\s*P\d+\b")
PROPAGATION_CASCADE_RE = re.compile(r"^- P\d+\b")


def parse_propagation(lines: list[str]) -> dict:
    """`rows` (line number, cells of each table line whose first cell starts `P<n>`, so `P10 (optional)`
    counts), `cascades` (line number, text of each `- P<n>` bullet of the list a `Cascades` line, with or
    without a colon, opens; the list ends at the next unindented line that is no such bullet) and `decided`
    (a `## Decisions` heading)."""
    rows, cascades, in_list = [], [], False
    for n, line in enumerate(lines, 1):
        if PROPAGATION_ROW_RE.match(line):
            rows.append((n, [c.strip() for c in line.strip().strip("|").split("|")]))
        if re.fullmatch(r"Cascades:?", line.strip()):
            in_list = True
        elif in_list and PROPAGATION_CASCADE_RE.match(line):
            cascades.append((n, line.strip()))
        elif in_list and line.strip() and not line[0].isspace():
            in_list = False
    return {"rows": rows, "cascades": cascades, "decided": any(l.startswith("## Decisions") for l in lines)}


def propagation_section(vs, worktree) -> dict:
    """The story's checklist: in the setup state dir before the land, in <story history>/<id>/ after it."""
    dirs = [SETUP_DIR] + [HISTORY_DIR / v for v in vs]
    for path in (d / f"propagation-{v}.md" for d in dirs for v in vs):
        if not path.is_file():
            continue
        parsed = parse_propagation(path.read_text(encoding="utf-8", errors="replace").splitlines())
        return ok(path, rows=len(parsed["rows"]), cascades=len(parsed["cascades"]), decided=parsed["decided"])
    return none(HISTORY_DIR, f"no propagation-{{{','.join(vs)}}}.md in {SETUP_DIR} or {HISTORY_DIR}")


# ---------------------------------------------------------------- git

def parse_name_status(out: str) -> dict[str, dict]:
    tokens = out.split("\0")
    files, i = {}, 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i]
        if status[0] in "RC":
            old, new = tokens[i + 1], tokens[i + 2]
            files[new] = {"change": status[0], "old_path": old}
            i += 3
        else:
            files[tokens[i + 1]] = {"change": status[0]}
            i += 2
    return files


def parse_porcelain(out: str) -> dict[str, dict]:
    tokens = out.split("\0")
    files, i = {}, 0
    while i < len(tokens) and tokens[i]:
        entry = tokens[i]
        xy, path = entry[:2], entry[3:]
        info = {"xy": xy}
        if "R" in xy or "C" in xy:
            info["old_path"] = tokens[i + 1]
            i += 2
        else:
            i += 1
        files[path] = info
    return files


def git_section(cwd, head_ref, with_status: bool) -> dict:
    """Commits and files since the merge-base with the base branch, read from `cwd` against `head_ref`."""
    def g(*args):
        argv = ["git", "-C", cwd, *args]
        rc, out, stderr = run(argv, timeout=GIT_TIMEOUT_S)
        if rc != 0:
            raise RuntimeError(cmd_error(argv, rc, stderr))
        return out

    source = f"git -C {cwd} (base {BASE_BRANCH}, head {head_ref})"
    try:
        branch = g("rev-parse", "--abbrev-ref", head_ref).strip()
        head = g("rev-parse", head_ref).strip()
        merge_base = g("merge-base", head_ref, BASE_BRANCH).strip()
        commits = []
        for line in g("log", "--format=%h%x09%cI%x09%s", f"{merge_base}..{head_ref}").splitlines():
            sha, date, subject = (line.split("\t", 2) + ["", ""])[:3]
            commits.append({"sha": sha, "date": date, "subject": subject})
        committed = parse_name_status(g("diff", "--name-status", "-z", merge_base, head_ref))
        dirty = parse_porcelain(g("status", "--porcelain=v1", "-z", "--untracked-files=all")) if with_status else {}
    except RuntimeError as exc:
        return err(source, str(exc))
    files = []
    for path in sorted(set(committed) | set(dirty)):
        c, d = committed.get(path), dirty.get(path)
        entry = {"path": path}
        if d and d["xy"] == "??":
            entry.update(change="?", where="untracked")
        elif c and d:
            entry.update(change=f"{c['change']}/{d['xy'].replace(' ', '.')}", where="both")
        elif c:
            entry.update(change=c["change"], where="committed")
        else:
            entry.update(change=d["xy"].replace(" ", "."), where="uncommitted")
        old = (c or d or {}).get("old_path")
        if old:
            entry["old_path"] = old
        files.append(entry)
    return ok(source, branch=branch, head=head, merge_base=merge_base, base=BASE_BRANCH, commits=commits,
              files=files, uncommitted="applicable" if with_status else "not applicable: no worktree")


# ---------------------------------------------------------------- agents

def dispatched_section(vs) -> dict:
    jdir = journal_dir()
    if not os.path.isdir(jdir):
        return err(jdir, f"journal dir {jdir} does not exist")
    facts, scanned, nfiles = sl.load_facts(vs)
    out = []
    for f in facts:
        if f.get("kind") == "agent_spawned":
            agent_type = f.get("agent_type")
        elif f.get("kind") == "workflow_run" and sl.is_review_workflow(f):
            agent_type = str(wfconfig.get("review.workflow_name", "review") or "review")
        else:
            continue
        row = {"ts": f.get("ts"), "agent_type": agent_type, "desc": f.get("desc"), "session": f.get("_session"),
               "nested": f.get("_nested"), "caller": f.get("caller")}
        if f.get("story_id"):
            row["story_id"] = f["story_id"]
        if f.get("workflow"):
            row["workflow"] = f["workflow"]
        out.append(row)
    if not out:
        return none(jdir, f"no agent_spawned or review workflow_run fact names {{{','.join(vs)}}} "
                          f"({scanned} facts scanned in {nfiles} journals)")
    return ok(jdir, facts=out, note=DISPATCH_NOTE)


def attribute_running(agents, stories) -> dict[str, list[dict]]:
    """story_id -> its running agents; the key None holds the unattributed ones."""
    result: dict = {None: []}
    for s in stories:
        result[s["story_id"]] = []
    matcher_ids = sorted({i for s in stories for i in (s["story_id"], *( [s["_short"]] if s["_short"] else []))})
    matcher = harness_metrics.IdMatcher(matcher_ids) if matcher_ids else None
    owner = {}
    for s in stories:
        for i in (s["story_id"], s["_short"]):
            if i:
                owner[i.lower()] = s
    for a in agents:
        row = {k: v for k, v in a.items() if not k.startswith("_")}
        story, how = None, None
        cwd = norm(a["cwd"]) if a.get("cwd") else None
        if cwd:
            story = next((s for s in stories if s["_worktree"] and within(cwd, norm(s["_worktree"]))), None)
            how = "worktree cwd"
        if story is None and a.get("workflow_run") and a.get("_session_dir"):
            value = harness_metrics.workflow_run_story(a["_session_dir"], a["workflow_run"])
            if value:
                story = next((s for s in stories if sl.mentions(value, s["_vs"])), None)
                how = "workflow run file"
        if story is None and matcher and a.get("description"):
            found = {owner[i.lower()]["story_id"] for i in matcher.find(a["description"])}
            if len(found) == 1:
                story = next(s for s in stories if s["story_id"] in found)
                how = "meta description"
        row["attributed_by"] = how if story else None
        result[story["story_id"] if story else None].append(row)
    return result


# ---------------------------------------------------------------- cost

def cost_args(stories) -> dict[str, str]:
    """story_id -> the id `harness_metrics.py` receives (the id journals name, normalised as it does)."""
    out = {}
    for s in stories:
        arg = s["_short"] or s["story_id"]
        out[s["story_id"]] = arg.strip().lower().replace(".", "-")
    return out


def cost_sections(stories) -> dict[str, dict]:
    if not stories:
        return {}
    args = cost_args(stories)
    uniq = list(dict.fromkeys(args.values()))
    argv = [sys.executable, SCRIPTS_DIR / "harness_metrics.py"]
    for a in uniq:
        argv += ["--story", a]
    argv.append("--json")
    rc, out, stderr = run(argv, timeout=COST_TIMEOUT_S)
    if rc != 0:
        return {s["story_id"]: err(argv_text(argv), cmd_error(argv, rc, stderr)) for s in stories}
    try:
        reports = {r["story"]: r for r in json.loads(out)}
    except (ValueError, KeyError, TypeError) as exc:
        return {s["story_id"]: err(argv_text(argv), f"unparsable harness_metrics output: {exc}") for s in stories}
    sections = {}
    for s in stories:
        rep = reports.get(args[s["story_id"]])
        if rep is None:
            sections[s["story_id"]] = err(argv_text(argv), f"story {args[s['story_id']]} is missing from the "
                                                          f"harness_metrics report")
            continue
        sections[s["story_id"]] = guard(argv_text(argv), "harness_metrics report", lambda rep=rep: report_section(argv, rep))
    return sections


def report_section(argv, rep) -> dict:
    classes: dict[str, int] = {}
    for sess in rep.get("sessions", []):
        classes[sess.get("class")] = classes.get(sess.get("class"), 0) + 1
    return ok(
        argv_text(argv), usd_measured=rep["total"]["usd_measured"], usd_est_topup=rep["total"]["usd_est_topup"],
        by_phase={k: v["usd_measured"] for k, v in rep["by_phase"].items()},
        by_agent={k: v["usd_measured"] for k, v in rep["by_agent"].items()},
        sessions_by_class=classes, excluded_incidental_usd=rep["excluded_incidental_usd"],
        wall_clock=rep["wall_clock"], unpriced_requests=rep["unpriced_requests"],
        basis="list-price estimate", format="internal")


# ---------------------------------------------------------------- driver runs

def run_sections(run_dir: Path) -> dict:
    run_id = run_dir.name
    log_path, sessions_path = run_dir / "driver.log", run_dir / "sessions.jsonl"
    # driver.log
    if not log_path.is_file():
        log = none(log_path, f"no driver.log in {run_dir}")
    else:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            log = ok(log_path, first=lines[0], last=lines[-1], lines=len(lines)) if lines else \
                none(log_path, "driver.log is empty")
        except OSError as exc:
            log = err(log_path, f"cannot read driver.log: {exc}")
    # sessions.jsonl
    records = None
    if not sessions_path.is_file():
        sessions = none(sessions_path, "no sessions.jsonl: no session has finished")
    else:
        try:
            records = read_jsonl(sessions_path, strict=True)
            sessions = ok(sessions_path, records=[{k: v for k, v in r.items() if k != "knobs"} for r in records])
        except (OSError, ValueError) as exc:
            sessions = err(sessions_path, f"cannot read sessions.jsonl: {exc}")

    def derived(fn):
        if sessions["status"] == "ok":
            return fn(records)
        return {k: v for k, v in sessions.items() if k != "records"}

    spent = derived(lambda recs: ok(sessions_path, usd=round(sum(
        r["cost_usd"] for r in recs if isinstance(r.get("cost_usd"), (int, float))), 6), note="finished sessions only"))
    stories = derived(lambda recs: ok(sessions_path, ids=list(dict.fromkeys(
        r["story"] for r in recs if r.get("story") not in (None, "none")))))
    # the highest-numbered stream without a record line
    finished = {r.get("session") for r in records or []}
    streams = sorted(int(m.group(1)) for p in run_dir.glob("session-*.jsonl")
                     if (m := re.fullmatch(r"session-(\d+)\.jsonl", p.name)))
    if sessions["status"] == "error":
        in_progress = err(run_dir, "cannot tell which session is streaming: sessions.jsonl is unreadable")
    else:
        open_streams = [n for n in streams if n not in finished]
        if not open_streams:
            in_progress = none(run_dir, "every session stream has a record line" if streams
                               else "no session-<n>.jsonl in the run directory")
        else:
            n = open_streams[-1]
            path = run_dir / f"session-{n}.jsonl"
            in_progress = guard(path, "session stream", lambda: stream_state(path, n))
    return {"status": "ok", "source": str(run_dir), "run_id": run_id,
            "started_utc": datetime.strptime(run_id, "%Y%m%d-%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ"),
            "alive": "not determined here: `auto_dev_driver.py list` reads the run record and the process table",
            "stop_requested": (run_dir / "STOP").exists(),
            "driver_log": log, "sessions": sessions, "spent_usd": spent, "stories": stories,
            "in_progress": in_progress}


def stream_state(path: Path, n: int) -> dict:
    init, has_result = None, False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not (("init" in line and "subtype" in line) or '"result"' in line):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "system" and event.get("subtype") == "init" and init is None:
                init = event
            elif event.get("type") == "result":
                has_result = True
    if init is None:
        return ok(path, session=n, init_found=False, has_result=has_result, last_event_at=mtime_iso(path),
                  init_reason="the stream holds no system/init event yet", format="internal")
    return ok(path, session=n, init_found=True, session_id=init.get("session_id"), model=init.get("model"),
              claude_code_version=init.get("claude_code_version"), has_result=has_result,
              last_event_at=mtime_iso(path), format="internal")


def run_or_error(run_dir: Path) -> dict:
    """`run_sections`, or an `error` section that still names the run when reading it raises."""
    result = guard(run_dir, "run_sections", lambda: run_sections(run_dir))
    if result.get("status") == "error":
        result["run_id"] = run_dir.name
    return result


def driver_runs_section() -> dict:
    if not AUTO_DEV_DIR.is_dir():
        return none(AUTO_DEV_DIR, f"no auto-dev state directory at {AUTO_DEV_DIR}")
    try:
        names = sorted((p.name for p in AUTO_DEV_DIR.iterdir() if p.is_dir() and RUN_ID_RE.match(p.name)), reverse=True)
        return ok(AUTO_DEV_DIR, runs=[run_or_error(AUTO_DEV_DIR / n) for n in names])
    except OSError as exc:
        return err(AUTO_DEV_DIR, f"cannot read {AUTO_DEV_DIR}: {exc}")


# ---------------------------------------------------------------- sessions

def story_identity(story_id: str, tracker_keys) -> str:
    """The sprint-status key a story id joins to, else the id itself."""
    state, key, _ = join_key(story_id, tracker_keys)
    return key if state == "ok" else story_id


def story_forms(registry, tracker, tracker_keys) -> dict[str, set[str]]:
    """Lowercase id form -> the identities carrying it, over every slot-registry Story ID and every
    sprint-status story row. The key, its short id and the dotted and dashed forms of both name one story."""
    forms: dict[str, set[str]] = {}

    def add(identity: str, *names) -> None:
        for name in names:
            for v in sl.variants(name) if name else []:
                forms.setdefault(v.lower(), set()).add(identity)

    for row in tracker or []:
        key = row["key"]
        if not is_epic_key(key):
            add(key, key, harness_metrics.short_id(key))
    for row in registry or []:
        if row["status"] == "in_use":
            identity = story_identity(row["story_id"], tracker_keys)
            add(identity, row["story_id"], harness_metrics.short_id(identity))
    return forms


def worktree_trees(registry, worktrees, tracker_keys) -> list[tuple[str, str]]:
    """(normalised worktree path, identity), longest path first: the registry's in_use rows, then
    the story branches (`config: git.branch_prefix`) of `git worktree list`. The main checkout is never one."""
    trees: dict[str, str] = {}
    for row in registry or []:
        if row["status"] == "in_use" and row["worktree"]:
            trees[norm(row["worktree"])] = story_identity(row["story_id"], tracker_keys)
    prefix = f"refs/heads/{BRANCH_PREFIX}"
    for wt in worktrees or []:
        if (wt["branch"] or "").startswith(prefix):
            trees.setdefault(norm(wt["path"]), story_identity(wt["branch"][len(prefix):], tracker_keys))
    trees.pop(norm(MAIN_REPO), None)
    return sorted(trees.items(), key=lambda kv: len(kv[0]), reverse=True)


def lookup_name(name: str, forms) -> set[str]:
    """The identities a session name equals: the id in any form, else an id followed by the two-word
    suffix a session listing adds to a duplicate name."""
    hits = forms.get(name.lower())
    if hits:
        return hits
    m = DUPLICATE_NAME_RE.match(name)
    return forms.get(m["base"].lower(), set()) if m else set()


def unreadable_reasons(unreadable: dict, *sources) -> list[str]:
    """`<source> unreadable: <error>` for each of `sources` whose read failed."""
    return [f"{source} unreadable: {unreadable[source]}" for source in sources if source in unreadable]


def link_driver(entry: dict, match, run: dict, forms, shown) -> str | None:
    """Rule 1: a driver session's name carries its run and number, and perhaps its story."""
    run_id, n = match["run"], int(match["n"])
    entry["driver_run"] = {"run_id": run_id, "session": n}
    identity, how = None, "name"
    sessions = run["sessions"]
    texts = [(match["story"], "name", "the name's story part")]
    if sessions["status"] == "ok":
        record = next((r for r in sessions["records"] if r.get("session") == n), None)
        texts.append((record.get("story") if record else None, "phase line", "the phase line's story"))
    ambiguous = []
    for text, source, label in texts:
        if text in (None, "", "none"):
            continue
        hits = forms.get(str(text).lower(), set())
        if len(hits) > 1:
            if source == "name":
                entry["name_candidates"] = sorted(hits)
            ambiguous.append((source, f"{label} {str(text)!r} matched {len(hits)} stories"))
            continue
        identity, how = next(iter(hits), str(text)), source
        break
    if identity is None:
        if ambiguous:
            parts = [text for _, text in ambiguous]
            if sessions["status"] == "ok" and all(source != "phase line" for source, _ in ambiguous):
                parts.append("the phase line names no story")
            elif sessions["status"] == "none":
                parts.append(sessions["reason"])
            reason = f"driver run {run_id} session {n}: " + "; ".join(parts)
        else:
            reason = f"driver run {run_id} session {n} has not named its story"
        if sessions["status"] == "error":
            reason += f"; {sessions['error']}"
        entry.update(story=None, linked_by=None, unlinked_reason=reason)
        return None
    entry.update(story=shown.get(identity, identity), linked_by="driver-run", story_from=how)
    return identity


def link_session(entry: dict, forms, trees, run_map, shown, unreadable) -> str | None:
    """Set `story` and `linked_by` (and the rule's own fields) on `entry`; return the story's identity.
    The first rule that resolves wins; nothing but the listing's name and cwd is read. `unreadable` maps
    the name of each failed source to its error, which an unlinked reason that relied on it appends."""
    name, cwd = entry.get("name"), entry.get("cwd")
    tried = []
    driver = DRIVER_NAME_RE.match(name) if isinstance(name, str) else None
    if driver:
        run = run_map.get(driver["run"])
        if run is not None and run.get("status") != "error":
            return link_driver(entry, driver, run, forms, shown)
        if run is not None:
            tried.append(f"driver run {driver['run']} unreadable: {run['error']}")
        else:
            tried.extend(unreadable_reasons(unreadable, "driver runs")
                         or [f"driver-shaped name {name!r} has no run directory {driver['run']}"])
    if isinstance(name, str):
        hits = lookup_name(name, forms)
        if len(hits) == 1:
            identity = next(iter(hits))
            entry.update(story=shown.get(identity, identity), linked_by="name")
            return identity
        if hits:
            entry["name_candidates"] = sorted(hits)
            tried.append(f"name {name!r} matches {len(hits)} stories")
        else:
            tried.append(f"name {name!r} matches no story")
            tried.extend(unreadable_reasons(unreadable, "slot registry", "sprint-status tracker"))
    else:
        tried.append("the session has no name")
    if isinstance(cwd, str) and cwd:
        here = norm(cwd)
        for tree, identity in trees:
            if within(here, tree):
                entry.update(story=shown.get(identity, identity), linked_by="cwd")
                return identity
        if within(here, norm(MAIN_REPO)):
            tried.append(f"cwd {cwd} is in the main checkout")
        else:
            tried.append(f"cwd {cwd} is under no story worktree")
            tried.extend(unreadable_reasons(unreadable, "slot registry", "git worktree list"))
    else:
        tried.append("the session has no cwd")
    entry.update(story=None, linked_by=None, unlinked_reason="; ".join(tried))
    return None


def session_entry(item: dict, forms, trees, run_map, shown, unreadable) -> dict:
    entry = {f: item[f] for f in LISTING_FIELDS if f in item}
    started = item.get("startedAt")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        entry["started"] = datetime.fromtimestamp(started / 1000, timezone.utc).isoformat()
    entry["key"] = item.get("id") or item.get("sessionId") or f"pid:{item.get('pid')}"
    entry["_identity"] = link_session(entry, forms, trees, run_map, shown, unreadable)
    return entry


def sessions_argv() -> tuple[list[str] | None, str]:
    """(the session-listing command, or None with the reason it is unavailable).

    `config: story_state.sessions_cmd` (a list, or a string split on whitespace) wins; "" → by
    `config: loop.driver_tool`: claude lists with `claude agents --json --all`; opencode has no listing
    command whose output this reader knows, so the probe is unavailable. The command must print a JSON
    array of objects carrying the fields of `LISTING_FIELDS`."""
    configured = wfconfig.get("story_state.sessions_cmd", "") or ""
    if isinstance(configured, list) and configured:
        argv = [str(a) for a in configured]
    elif isinstance(configured, str) and configured.strip():
        argv = configured.split()
    elif str(wfconfig.get("loop.driver_tool", "claude")) == "claude":
        argv = list(CLAUDE_SESSIONS_ARGV)
    else:
        return None, ("unavailable: config loop.driver_tool is not claude and config story_state.sessions_cmd "
                      "is empty, so no session listing is read")
    return [shutil.which(argv[0]) or argv[0], *argv[1:]], ""


def sessions_section(forms, trees, runs, shown, unreadable) -> dict:
    """Every session the session-listing probe lists, each with the story it belongs to. Entries keep a
    private `_identity` for `take_identities`. `unreadable` maps each failed source feeding `forms` and
    `trees` to its error."""
    argv, why = sessions_argv()
    if argv is None:
        return none(SESSIONS_SOURCE, why)
    rc, out, stderr = run(argv, cwd=MAIN_REPO, timeout=SESSIONS_TIMEOUT_S)
    if rc != 0:
        return err(SESSIONS_SOURCE, cmd_error(argv, rc, stderr))
    try:
        listed = json.loads(out)
    except ValueError as exc:
        return err(SESSIONS_SOURCE, f"{SESSIONS_SOURCE} printed no JSON: {exc}")
    if not isinstance(listed, list):
        return err(SESSIONS_SOURCE, f"{SESSIONS_SOURCE} printed JSON that is not an array")
    if not all(isinstance(item, dict) for item in listed):
        return err(SESSIONS_SOURCE, f"{SESSIONS_SOURCE} printed an array holding a non-object item")
    run_map = {r["run_id"]: r for r in runs["runs"]} if runs["status"] == "ok" else {}
    if runs["status"] == "error":
        unreadable = {**unreadable, "driver runs": runs["error"]}
    entries = [session_entry(item, forms, trees, run_map, shown, unreadable) for item in listed]
    entries.sort(key=lambda e: e["startedAt"] if isinstance(e.get("startedAt"), (int, float)) else float("-inf"),
                 reverse=True)
    return ok(SESSIONS_SOURCE, entries=entries, note=SESSIONS_NOTE)


def take_identities(sessions: dict) -> list[str | None]:
    """Remove each entry's private `_identity` and return them in entry order ([] unless the section is ok)."""
    if sessions["status"] != "ok":
        return []
    return [e.pop("_identity") for e in sessions["entries"]]


def session_ids_section(sessions: dict, identities: list, identity: str) -> dict:
    if sessions["status"] == "none":
        return none(SESSIONS_SOURCE, sessions["reason"])
    if sessions["status"] != "ok":
        return err(SESSIONS_SOURCE, f"the session listing failed: {sessions['error']}")
    return ok(SESSIONS_SOURCE, ids=[e["key"] for e, who in zip(sessions["entries"], identities) if who == identity])


# ---------------------------------------------------------------- the attention queue

ATTENTION_SOURCE = "attention queue"
ATTENTION_NOTE = (
    SESSIONS_NOTE + " A session started in a checkout without the Notification hook records no event: its waits "
    "show from the listing alone (a background session reading status idle with state working stays unknown, an "
    "interactive idle session is never marked). The record-halt entries (a record's latest dated line holding an "
    "upper-case HALT and no ruling word outside parentheses) and the external-interim entries (a design-question "
    "tracker row whose Blocks cell holds a warning sign or `interim`) are text rules over free-form files: a halt "
    "or an interim written another way is not seen, and a later decision line that mentions an earlier HALT "
    "outside parentheses does get an entry. The waits-on rule of a halt reads the failure entry's `asked_of` line "
    "(`owner`, `external` or `both`) and `external_ticket` line: `external` while the external owner owes (`asked_of` "
    "`external` or `both`, or no `asked_of` line and a ticket key) and the ticket's Status cell in the tracker "
    "does not start with the done marker (`config: design.done_marker`), `owner` otherwise; `asked_of` "
    "is never null. A story with several failure entries is judged by its latest entry: every `driver-halt` line "
    "of the story carries that entry's `asked_of`. A `blocked-row` entry's `halt_asked_of` is the failure "
    "entry's own `asked_of` line (`null` when there is none), and its `asked` is the row's note only when its "
    "`note_status` reads `ok`.")
KIND_RANK = {"open-question": 0, "spec-draft": 1, "record-halt": 2, "driver-halt": 3, "propagation-row": 4,
             "propagation-cascade": 4, "external-interim": 5, "blocked-row": 6}
MARK_RANK = {"waiting": 0, "failed": 1, "unknown": 2}
# A story with one of these statuses queues nothing in O1-O5. O6 alone keeps entries for finished stories.
FINISHED = {"done", "superseded"}
EVENT_TYPES = ("permission_prompt", "idle_prompt")
SESSION_ID_RE = re.compile(r"[0-9A-Za-z-]+")
OPEN_Q_NUMBER_RE = re.compile(r"\[OPEN-Q (\d+)")
RECORD_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2})\s+(.*)$")
HALT_RE = re.compile(r"\bHALT(?:ED)?\b")
RULED_RE = re.compile(r"\brul(?:ings|ing|ed)\b", re.IGNORECASE)
FAILURE_HEADING_RE = re.compile(r"^## (\S+) — (\S+) — (.+)$")
# A story id carries a separator: numeric parts joined by `-` or `.`, or a letter prefix, a dash and a number.
# A bare number, a decimal, a percentage or a ratio is not one; a colon and a space after an id is a label.
STORY_TOKEN_RE = re.compile(
    r"(?<![\w.@-])(?:[A-Za-z]{1,3}-\d+(?:[-.]\d+)*[a-z]?(?!\w|[-.]\d)"
    r"|(?!0\.)\d+(?:[-.]\d+)+[a-z]?(?![\w%]|:\d|[-.]\d))")
B8_UNKNOWN_ASKED = ("the listing reads status idle with state working, which a session waiting for input after an "
                    "interrupt also shows; no Notification since the session last wrote its transcript")
B8_UNREADABLE_ASKED = ("the listing reads status idle with state working; its Notification event file could not be "
                       "read (`{file}`)")
B2_ASKED = "a question asked in prose (state blocked, status idle)"
B3_ASKED = ("blocked with no live process: a first-run dialog or a retired session; attaching by id resumes it")


def cap(text) -> str:
    text = str(text)
    return text if len(text) <= MAX_ERR else text[:MAX_ERR - 1] + "…"


def attention_shown(registry, tracker, tracker_keys) -> dict[str, str]:
    """identity -> displayed story id for the whole in-flight set, whatever `--story` narrowed."""
    shown = {}
    for entry in story_set(None, registry, tracker, tracker_keys):
        shown[join_key(entry["story_id"], tracker_keys)[1] or entry["story_id"]] = entry["story_id"]
    return shown


def entry_asked(fields: dict) -> str | None:
    """The entry's `asked_of` value; None when the line is absent or reads anything but owner, external or both."""
    asked = fields.get("asked_of")
    return asked if asked in ("owner", "external", "both") else None


def owner_owes(fields: dict | None) -> bool:
    """True when the owner owes the halt a ruling: `asked_of` is `owner`, `both` or absent, whatever the external
    part reads. The owner's part stays owed while the entry stands: `resume_halted.py` takes the ruling and removes
    the entry in one run. False for None (the story has no entry)."""
    return fields is not None and entry_asked(fields) in ("owner", "both", None)


def entry_ticket(fields: dict | None) -> str | None:
    """The first ticket key (`config: paths.design_ticket_regex`) in the entry's `external_ticket` value
    (`log_failure.py --external-ticket`); None when the line is absent or holds none."""
    found = TICKET_RE.search((fields or {}).get("external_ticket") or "")
    return found.group() if found else None


def ticket_done(status: str | None) -> bool:
    """The tracker's done rule (`_design.is_done`: the Status starts with `config: design.done_marker`)."""
    return _design.is_done(status)


def waits_on(fields: dict | None, external_rows: list[dict] | None) -> tuple[str, str | None]:
    """(asked_of, the ticket's Status cell) for a story's latest failure entry, from its parsed `- key: value`
    `fields` (None: the story has no entry) and the design-question tracker's ticket rows (None: unreadable or
    not configured). The one implementation of the waits-on rule.

    The external owner owes when `asked_of` is `external` or `both`, or the entry has no `asked_of` line and a
    ticket (`entry_ticket`). That part is open until the ticket's Status reads done (`ticket_done`); a ticket with
    no row, no ticket or no readable tracker count as open. The result is `external` while that part is open, else
    `owner`: the owner owes a ruling (`owner_owes`), or the halt asked only the external owner, the ticket reads
    done and the resume is due. Never None. A story with no entry is asked of `owner`."""
    if fields is None:
        return "owner", None
    asked = entry_asked(fields)
    ticket = entry_ticket(fields)
    status = next((r["status"] for r in external_rows or [] if r["ticket"] == ticket), None) if ticket else None
    external_open = (asked in ("external", "both") or (asked is None and ticket is not None)) \
        and not ticket_done(status)
    return ("external" if external_open else "owner"), status


FIELD_RE = re.compile(r"^- (\w+): (.*)$")
LINE_BREAK_RE = re.compile(r"(\r\n|\r|\n)")


def failure_lines(text: str) -> list[str]:
    """The failure log's lines: split on `\\r\\n`, `\\r` and `\\n` only. `str.splitlines` also breaks at `\\x0b`,
    `\\x0c`, `\\x1c`-`\\x1e`, `\\x85`, U+2028 and U+2029, which would give a reader a column-0 field that another
    reader of the same file does not see; `resume_halted.py` splits with `LINE_BREAK_RE` too."""
    lines = LINE_BREAK_RE.split(text)[0::2]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def failure_blocks(lines: list[str]) -> list[dict]:
    """One dict per `## <id> — <timestamp> — <step>` entry of auto-dev-failures.md, in file order: its heading
    parts, `start` (the heading's index) and `end` (the next heading's index, exclusive) in `lines`, `group_end`
    and `fields`.

    After the heading, blank and whitespace-only lines are skipped. The entry's leading group is then the run of
    column-0 lines starting with `- ` and their continuation lines, which start with a space: `log_failure.py`
    indents a multi-line `failure_summary` that way (an empty line as `  `). A `- ` line that is no `- key: value`
    field (`- wip_commit:` with no value, a hyphenated key) stays in the group and sets nothing. The first other
    line ends the group (an empty line, `Last test output`, a fence, prose), so no line of a summary ends it
    early, and the test output and the prose below it never set a field. `group_end` is the index just past the
    group (`start + 1` when the entry has none). `fields` holds the first `- key: value` bullet of each key; a
    continuation line is never read as a field."""
    heads = [i for i, l in enumerate(lines) if FAILURE_HEADING_RE.match(l)]
    blocks = []
    for at, start in enumerate(heads):
        story_id, stamp, step = FAILURE_HEADING_RE.match(lines[start]).groups()
        end = heads[at + 1] if at + 1 < len(heads) else len(lines)
        first = start + 1
        while first < end and not lines[first].strip():
            first += 1
        group_end = first
        while group_end < end and (lines[group_end].startswith("- ")
                                   or (group_end > first and lines[group_end].startswith(" "))):
            group_end += 1
        fields = {}
        for l in lines[first:group_end]:
            field = FIELD_RE.match(l)
            if field:
                fields.setdefault(field.group(1), field.group(2).strip())
        blocks.append({"story_id": story_id, "stamp": stamp, "step": step, "start": start, "end": end,
                       "group_end": group_end if group_end > first else start + 1, "fields": fields})
    return blocks


def story_branches() -> tuple[set[str] | None, str]:
    """(every local story branch (`config: git.branch_prefix`), error text): one `git for-each-ref`."""
    argv = ["git", "-C", MAIN_REPO, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}"]
    rc, out, stderr = run(argv, timeout=GIT_TIMEOUT_S)
    if rc != 0:
        return None, cmd_error(argv, rc, stderr)
    return {l.strip() for l in out.splitlines() if l.strip()}, ""


class AttentionContext:
    """What every attention source resolves a story against."""

    def __init__(self, registry, tracker, tracker_keys, worktrees, worktrees_err, shown):
        self.registry, self.tracker, self.tracker_keys = registry, tracker, tracker_keys
        self.worktrees, self.worktrees_err, self.shown = worktrees, worktrees_err, shown
        self.status_of = {r["key"]: r["status"] for r in tracker or []}
        self.forms = story_forms(registry, tracker, tracker_keys)
        # O4 stores each blocked story's failure entries, in file order, for O7: one entry per halted story.
        self.halts: dict[str, list[dict]] = {}
        # O4 also stores the identities a heading that names several stories could mean; its `driver-halt` is then
        # their one entry, so O7 emits no `blocked-row` for them.
        self.covered: set[str] = set()
        self._external_rows: tuple[list[dict] | None, Exception | None] | None = None

    def external_rows(self) -> list[dict] | None:
        """The design-question tracker's ticket rows, parsed once per attention run (the failure, tracker and
        blocked-row readers share them); None when no tracker is configured; a failed read raises the same
        exception on every call."""
        if EXTERNAL_PATH is None:
            return None
        if self._external_rows is None:
            try:
                self._external_rows = (read_external_rows(), None)
            except Exception as exc:  # noqa: BLE001 -- re-raised to each caller
                self._external_rows = (None, exc)
        rows, exc = self._external_rows
        if exc is not None:
            raise exc
        return rows

    def waits_on(self, fields: dict | None) -> tuple[str | None, str | None]:
        """`waits_on(fields, rows)` over this run's one parse of the design-question tracker."""
        try:
            rows = self.external_rows()
        except Exception:  # noqa: BLE001 -- the `external tracker` source reports the failed read
            rows = None
        return waits_on(fields, rows)

    def who(self, identity, fallback=None) -> tuple[str | None, str | None]:
        """(displayed story, tracker status) of an identity; the fallback text when it has none."""
        if identity is None:
            return fallback, None
        return self.shown.get(identity, identity), self.status_of.get(identity)

    def resolve(self, name: str) -> str | None:
        """The one identity a story id (any form) names, else None. An id that no form carries joins through
        the tracker keys instead."""
        hits = lookup_name(name, self.forms)
        if hits:
            return next(iter(hits)) if len(hits) == 1 else None
        state, key, _ = join_key(name, self.tracker_keys)
        return key if state == "ok" else None

    def candidates(self, name: str) -> set[str]:
        """Every identity a story id (any form) could name, by the two lookups `resolve` makes: the identities
        carrying it as an id form, else the tracker keys that mention it. `resolve` gives the one of them or None."""
        hits = lookup_name(name, self.forms)
        if hits:
            return set(hits)
        vs = sl.variants(name)
        return {key for key in self.tracker_keys or [] if sl.mentions(key, vs)}

    def entry(self, identity, kind, asked_of, source, asked, since=None, *, fallback=None, **extra) -> dict:
        story, status = self.who(identity, fallback)
        return {"story": story, "story_status": status, "kind": kind, "asked_of": asked_of, "source": source,
                "asked": cap(asked), "since": since, **extra}


# ---- sessions

def event_relation(event: dict | None) -> str:
    """How the event's `received` compares with the transcript's last write: `newer`, `older`, `missing`
    (the transcript cannot be stat-ed) or `none` (no event)."""
    if event is None:
        return "none"
    path = event.get("transcript_path")
    try:
        mtime = os.path.getmtime(path)
    except (OSError, TypeError, ValueError):
        return "missing"
    return "newer" if parse_ts(event["received"]).timestamp() > mtime else "older"


def read_events(items: list[dict]) -> tuple[dict[str, dict], dict[str, str], dict]:
    """(session id -> its latest `permission_prompt` / `idle_prompt` line, session id -> the name of its event
    file when that file cannot be read, `sources.events`). Only the files of listed sessions are opened; a
    file that cannot be read is named under `unreadable` and its session has no event."""
    if not ATTENTION_DIR.is_dir():
        return {}, {}, none(ATTENTION_DIR, f"no Notification event has been recorded: {ATTENTION_DIR} does not exist")
    latest, unreadable_files, files, unreadable = {}, {}, 0, []
    for sid in dict.fromkeys(i.get("sessionId") for i in items):
        if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
            continue
        path = ATTENTION_DIR / f"{sid}.jsonl"
        if not path.is_file():
            continue
        files += 1
        try:
            rows = read_jsonl(path, strict=False)
        except OSError as exc:
            unreadable.append(f"{path.name}: {exc}")
            unreadable_files[sid] = path.name
            continue
        usable = [r for r in rows if r.get("notification_type") in EVENT_TYPES and parse_ts(r.get("received"))]
        if usable:
            latest[sid] = usable[-1]
    extra = {"unreadable": unreadable} if unreadable else {}
    return latest, unreadable_files, ok(ATTENTION_DIR, files=files, events=len(latest), **extra)


def reading_of(item: dict) -> str:
    return f"the listing reads state {item.get('state')!r}, status {item.get('status')!r}"


def mark_session(item: dict, event: dict | None, relation: str,
                 event_file: str | None = None) -> tuple[str, str, str, bool, bool]:
    """(mark, asked, basis, event supports the mark, mark compared the transcript's last write). An event
    whose transcript cannot be stat-ed (`missing`) confirms nothing. `event_file` names the session's event
    file when that file cannot be read. The first matching row of the session tables wins."""
    state, status, waiting_for = item.get("state"), item.get("status"), item.get("waitingFor")
    confirms = event is not None and relation == "newer"
    compared = relation in ("newer", "older")
    message = (event or {}).get("message") or "the Notification event carries no message"

    def by_listing(asked):
        return "waiting", asked, "listing + event" if confirms else "listing", confirms, compared

    if item.get("kind") == "background":
        if status == "waiting":
            return by_listing(waiting_for or reading_of(item))
        if state == "blocked":
            if status == "idle" and not waiting_for:
                return by_listing(B2_ASKED)
            if item.get("pid") is None:
                return by_listing(B3_ASKED)
            return by_listing(waiting_for or reading_of(item))
        if state == "working" and status == "busy" or state in ("done", "stopped"):
            return "none", "", "listing", False, False
        if state == "failed":
            return "failed", "the session ended with an error", "listing", False, False
        if state == "working" and status == "idle":
            if confirms:
                return "waiting", message, "listing + event", True, True
            if relation == "missing":
                return "unknown", message, "listing + event (transcript not found)", False, False
            if event_file:
                return "unknown", B8_UNREADABLE_ASKED.format(file=event_file), "listing", False, False
            return "unknown", B8_UNKNOWN_ASKED, "listing", False, compared
        if state == "working" and status is None:
            return "unknown", "working with no live process", "listing", False, False
        return "unknown", reading_of(item), "listing", False, False
    if item.get("kind") == "interactive":
        if status == "busy":
            return "none", "", "listing", False, False
        if status == "waiting":
            return by_listing(waiting_for or reading_of(item))
        if status in ("idle", None):
            if confirms:
                return "waiting", message, "event", True, True
            if relation == "missing":
                return "unknown", message, "event (transcript not found)", False, False
            return "none", "", "listing", False, False
    return "unknown", reading_of(item), "listing", False, False


def session_queue(ctx: AttentionContext, items: list[dict], identities: list, events: dict,
                  unreadable_events: dict) -> list[dict]:
    out = []
    for item, identity in zip(items, identities):
        sid = item.get("sessionId")
        event = events.get(sid) if isinstance(sid, str) else None
        event_file = unreadable_events.get(sid) if isinstance(sid, str) else None
        relation = event_relation(event)
        mark, asked, basis, supports, compared = mark_session(item, event, relation, event_file)
        if mark == "none":
            continue
        source = {"session": item["key"], "session_id": sid, "name": item.get("name"),
                  "session_kind": item.get("kind"), "cwd": item.get("cwd"), "linked_by": item.get("linked_by")}
        extra = {"mark": mark,
                 "listing": {"state": item.get("state"), "status": item.get("status"),
                             "waitingFor": item.get("waitingFor"), "pid_present": item.get("pid") is not None},
                 "event": ({"notification_type": event["notification_type"], "message": event.get("message"),
                            "received": event["received"]} if event else None),
                 "basis": basis}
        if compared:
            extra["format"] = "internal"
        if identity is None:
            extra["unlinked_reason"] = item.get("unlinked_reason")
        out.append(ctx.entry(identity, "session", "owner", source, asked, event["received"] if supports else None,
                             **extra))
    return out


# ---- the stories O1-O3 read

class StoryScope:
    def __init__(self, ctx: AttentionContext, identity: str, key: str | None):
        self.identity = identity
        self.display = ctx.who(identity)[0]
        self.vs = variants_of(self.display, key)
        row = find_registry_row(ctx.registry, self.vs, key, ctx.tracker_keys)
        self.worktree = None
        if row and os.path.isdir(row["worktree"]):
            self.worktree = row["worktree"]
        else:
            wanted = {f"refs/heads/{story_branch(v)}" for v in self.vs}
            self.worktree = next((wt["path"] for wt in ctx.worktrees or []
                                  if wt["branch"] in wanted and os.path.isdir(wt["path"])), None)
        root = Path(self.worktree) if self.worktree else MAIN_REPO
        self.arts = arts_of(root)


def story_scopes(ctx: AttentionContext) -> list[StoryScope]:
    """Every sprint-status story row whose status is not `done` or `superseded` (`FINISHED`), plus every
    slot-registry `in_use` row whose story is not finished (a story the tracker does not know is kept)."""
    if ctx.tracker is None:
        raise RuntimeError("the tracker could not be read")
    keys: dict[str, str | None] = {}
    for row in ctx.tracker:
        key = row["key"]
        if row["status"] not in FINISHED and not is_epic_key(key):
            keys.setdefault(key, key)
    for row in ctx.registry or []:
        if row["status"] == "in_use":
            identity = story_identity(row["story_id"], ctx.tracker_keys)
            if ctx.status_of.get(identity) in FINISHED:
                continue
            keys.setdefault(identity, identity if identity in ctx.status_of else None)
    return [StoryScope(ctx, identity, key) for identity, key in keys.items()]


def branch_spec(branch: str, vs: list[str]) -> tuple[str, str] | None:
    """(main-checkout path, text) of the spec on `branch`, else None."""
    rel_dir = ARTS_REL.rstrip("/") + "/"
    argv = ["git", "-C", MAIN_REPO, "ls-tree", "--name-only", branch, rel_dir]
    rc, out, stderr = run(argv, timeout=GIT_TIMEOUT_S)
    if rc != 0:
        raise RuntimeError(cmd_error(argv, rc, stderr))
    names = sorted(p.strip() for p in out.splitlines()
                   if p.strip().endswith(".md") and sl._is_spec(p)
                   and any(os.path.basename(p).lower().startswith(v.lower()) for v in vs)
                   and sl.mentions(os.path.basename(p), vs))
    if not names:
        return None
    show = ["git", "-C", MAIN_REPO, "show", f"{branch}:{names[0]}"]
    rc, out, stderr = run(show, timeout=GIT_TIMEOUT_S)
    if rc != 0:
        raise RuntimeError(cmd_error(show, rc, stderr))
    return str(MAIN_REPO / names[0]), out


def open_question_entries(ctx, scope: StoryScope, file: str, ref: str | None, text: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for n, line in enumerate(text.splitlines(), 1):
        external = ({OPEN_Q_NUMBER_RE.search(m.group()).group(1) for m in OPEN_Q_EXTERNAL_RE.finditer(line)}
                    if OPEN_Q_EXTERNAL_RE is not None else set())
        for m in sl.OPEN_MARKER.finditer(line):
            number = OPEN_Q_NUMBER_RE.match(m.group()).group(1)
            g = groups.setdefault(number, {"lines": [], "external": False})
            if not g["lines"] or g["lines"][-1] != n:
                g["lines"].append(n)
            g.update(marker=m.group(), text=line.strip())
            g["external"] = g["external"] or number in external
    out = []
    for g in groups.values():
        source = {"file": file, "line": g["lines"][-1], **({"ref": ref} if ref else {})}
        out.append(ctx.entry(scope.identity, "open-question", "external" if g["external"] else "owner", source, g["text"],
                             marker=g["marker"], lines=g["lines"]))
    return out


def read_specs(ctx, scopes, branches, branches_err) -> tuple[dict, list[dict]]:
    if branches is None and any(s.worktree is None for s in scopes):
        raise RuntimeError(f"cannot list the story branches: {branches_err}")
    entries, unreadable = [], []
    for scope in scopes:
        try:
            found = None
            if scope.worktree is None:
                branch = next((story_branch(v) for v in scope.vs if story_branch(v) in branches), None)
                spec = branch_spec(branch, scope.vs) if branch else None
                found = (spec[0], branch, spec[1]) if spec else None
            if found is None:
                located = spec_section(scope.vs, scope.arts)
                if located["status"] == "ok":
                    found = (located["path"], None, Path(located["path"]).read_text(encoding="utf-8", errors="replace"))
            if found:
                entries += open_question_entries(ctx, scope, *found)
        except (OSError, RuntimeError) as exc:
            unreadable.append(f"{scope.display}: {exc}")
    extra = {"unreadable": unreadable} if unreadable else {}
    return ok("story specs", stories=len(scopes), entries=len(entries), **extra), entries


def record_lines(vs, arts) -> tuple[str | None, list[tuple[int, str]]]:
    """(path, [(file line number, stripped line)] of the lines starting with `-`) of the story's record."""
    for v in vs:
        path = os.path.join(arts, f"{v}-record.md")
        if os.path.isfile(path):
            return path, sl.numbered_record_lines(path)
    return None, []


def strip_parentheses(text: str) -> str:
    previous = None
    while previous != text:
        previous, text = text, re.sub(r"\([^()]*\)", "", text)
    return text


def read_records(ctx, scopes) -> tuple[dict, list[dict]]:
    entries = []
    for scope in scopes:
        path, lines = record_lines(scope.vs, scope.arts)
        if not lines:
            continue
        draft = sl.incomplete_spec_draft(lines)
        if draft:
            n, line = draft
            date = RECORD_LINE_RE.match(line)
            entries.append(ctx.entry(scope.identity, "spec-draft", "owner", {"file": path, "line": n}, line,
                                     date.group(1) if date else None))
        dated_lines = [(n, RECORD_LINE_RE.match(line)) for n, line in lines]
        dated_lines = [(n, dated) for n, dated in dated_lines if dated]
        if not dated_lines:
            continue
        n, dated = dated_lines[-1]
        text = dated.group(2)
        outside = strip_parentheses(text)
        if HALT_RE.search(outside) and not RULED_RE.search(outside):
            entries.append(ctx.entry(scope.identity, "record-halt", "owner", {"file": path, "line": n}, text,
                                     dated.group(1)))
    return ok("story records", stories=len(scopes), entries=len(entries)), entries


# ---- the out-of-session files

def halted_branch_re() -> re.Pattern:
    """The `- branch: <prefix><id> (PRESERVED` line `log_failure.py` writes for a kept story branch."""
    return re.compile(rf"^- branch: {re.escape(BRANCH_PREFIX)}(\S+) \(PRESERVED")


def read_failures(ctx, branches, branches_err) -> tuple[dict, list[dict]]:
    if not FAILURES_PATH.is_file():
        return none(FAILURES_PATH, f"no failure log at {FAILURES_PATH}"), []
    # Split on line breaks only (`failure_lines`): str.splitlines also breaks at \x0b, \x0c, \x1c-\x1e, \x85,
    # U+2028 and U+2029, which would give this reader a column-0 field another reader does not see.
    lines = failure_lines(FAILURES_PATH.read_text(encoding="utf-8", errors="replace"))
    entries, stale, halts, covered = [], [], {}, set()
    parsed_entries = failure_blocks(lines)
    # The story's latest entry (the last in file order) decides for each of its entries.
    latest = {ctx.resolve(p["story_id"]) or p["story_id"]: p for p in parsed_entries}
    for parsed in parsed_entries:
        story_id, stamp, step, start, fields = (parsed[k] for k in ("story_id", "stamp", "step", "start", "fields"))
        branch = None
        for l in lines[start + 1:parsed["end"]]:
            kept = halted_branch_re().search(l)
            if kept and branch is None:
                if branches is None:
                    raise RuntimeError(f"cannot list the story branches: {branches_err}")
                name = story_branch(kept.group(1))
                branch = {"name": name, "exists": name in branches,
                          "worktree": next((wt["path"] for wt in ctx.worktrees or []
                                            if wt["branch"] == f"refs/heads/{name}"), None)}
        identity = ctx.resolve(story_id)
        if ctx.who(identity)[1] in FINISHED:
            stale.append(f"{story_id} — {stamp}")
            continue
        failure_step = fields.get("failure_step") or step
        summary = fields.get("failure_summary")
        since = local_stamp_utc(stamp)
        if ctx.who(identity)[1] == "blocked":
            halts.setdefault(identity, []).append({
                "failure_step": failure_step, "external_ticket": entry_ticket(fields), "branch": branch,
                "wip_commit": fields.get("wip_commit"), "since": since, "fields": fields})
            continue
        if identity is None:
            covered |= ctx.candidates(story_id)
        decider = latest[identity or story_id]
        asked_of, _ = ctx.waits_on(decider["fields"])
        _, ticket_status = ctx.waits_on(fields)
        entries.append(ctx.entry(
            identity, "driver-halt", asked_of, {"file": str(FAILURES_PATH), "line": start + 1},
            f"{failure_step}: {summary}" if summary else failure_step, since, fallback=story_id,
            failure_step=failure_step, branch=branch, wip_commit=fields.get("wip_commit"),
            external_ticket=entry_ticket(fields), ticket_status=ticket_status))
    # A heading that names several stories covers their blocked keys, except one with a halt of its own.
    ctx.halts, ctx.covered = halts, covered - set(halts)
    return ok(FAILURES_PATH, entries=len(entries), stale_entries=stale), entries


def propagation_files(ctx) -> list[Path]:
    """Every checklist: the setup state dir's `propagation-*.md` (stories not landed yet), then each landed
    story's `<story history>/<id>/propagation-*.md`."""
    found = sorted(SETUP_DIR.glob("propagation-*.md")) if SETUP_DIR.is_dir() else []
    return found + (sorted(HISTORY_DIR.glob("*/propagation-*.md")) if HISTORY_DIR.is_dir() else [])


def read_propagation(ctx) -> tuple[dict, list[dict]]:
    paths = propagation_files(ctx)
    if not paths:
        return none(HISTORY_DIR, f"no propagation-*.md in {SETUP_DIR} or {HISTORY_DIR}"), []
    entries, decided, stale, unreadable = [], 0, [], []
    for path in paths:
        story_id = path.name[len("propagation-"):-len(".md")]
        identity = ctx.resolve(story_id)
        if ctx.who(identity)[1] in FINISHED:
            stale.append(path.name)
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            unreadable.append(f"{path.name}: {exc}")
            continue
        parsed = parse_propagation(body.splitlines())
        if parsed["decided"]:
            decided += 1
            continue
        since = mtime_iso(path)
        for n, cells in parsed["rows"]:
            cell = lambda i: cells[i] if i < len(cells) else ""  # noqa: E731
            row = {"id": re.match(r"P\d+", cells[0]).group(), "file": cell(1), "lines": cell(2), "impact": cell(4),
                   "proposed_edit": cell(5)}
            entries.append(ctx.entry(identity, "propagation-row", "owner", {"file": str(path), "line": n},
                                     f"rule {row['id']} ({row['impact']}): {row['file']} {row['lines']}", since,
                                     fallback=story_id, row=row))
        for n, text in parsed["cascades"]:
            entries.append(ctx.entry(identity, "propagation-cascade", "owner", {"file": str(path), "line": n}, text,
                                     since, fallback=story_id, row={"id": re.match(r"- (P\d+)", text).group(1)}))
    extra = {"unreadable": unreadable} if unreadable else {}
    return ok(HISTORY_DIR, files=len(paths), decided_files=decided, entries=len(entries), stale_files=stale,
              **extra), entries


def read_external_rows() -> list[dict]:
    """The ticket table of the design-question tracker (`config: paths.design_questions`, the format `_design.py`
    documents): the first table whose header row has a `Ticket` and a `Status` cell (the Status header may carry
    a sync date; a `Blocks` cell is optional), up to the first line that is not a table line. One row per ticket
    key (`config: paths.design_ticket_regex`) in the Ticket cell."""
    rows, cols = [], None
    for n, line in enumerate(EXTERNAL_PATH.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        text = line.strip().lstrip(">").strip()
        if cols is None:
            if text.startswith("|"):
                head = [c.strip().lower() for c in text.strip("|").split("|")]
                found = {name: next((i for i, h in enumerate(head) if h == name or h.startswith(name + " ")), None)
                         for name in ("ticket", "blocks", "status")}
                if found["ticket"] is not None and found["status"] is not None:
                    cols = {name: i for name, i in found.items() if i is not None}
            continue
        if not text.startswith("|"):
            break
        cells = [c.strip() for c in text.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue  # the header's separator row
        cell = lambda name: cells[cols[name]] if name in cols and cols[name] < len(cells) else ""  # noqa: E731
        ticket = TICKET_RE.search(cell("ticket"))
        if ticket:
            rows.append({"ticket": ticket.group(), "blocks": cell("blocks"), "status": cell("status"), "line": n})
    if cols is None:
        raise RuntimeError(f"no ticket table (a header row with Ticket and Status cells) in {EXTERNAL_PATH}")
    return rows


def read_external(ctx) -> tuple[dict, list[dict]]:
    if EXTERNAL_PATH is None:
        return none("config paths.design_questions", "n/a: config paths.design_questions is empty"), []
    if not EXTERNAL_PATH.is_file():
        return none(EXTERNAL_PATH, f"no design-question tracker at {EXTERNAL_PATH}"), []
    rows = ctx.external_rows()
    entries = []
    for row in rows:
        blocks = row["blocks"]
        if ticket_done(row["status"]) or not ("⚠" in blocks or re.search(r"\binterims?\b", blocks, re.IGNORECASE)):
            continue
        identities = list(dict.fromkeys(
            i for i in (ctx.resolve(m.group()) for m in STORY_TOKEN_RE.finditer(blocks)) if i)) or [None]
        for identity in identities:
            entries.append(ctx.entry(identity, "external-interim", "external",
                                     {"file": str(EXTERNAL_PATH), "line": row["line"]},
                                     f"{row['ticket']}: {blocks}", ticket=row["ticket"]))
    return ok(EXTERNAL_PATH, rows=len(rows), entries=len(entries)), entries


def read_blocked(ctx) -> tuple[dict, list[dict]]:
    """One `blocked-row` entry per `blocked` row. `asked` is the row's note only when `note_status` is `ok`: a failed
    `sprint_status.py get` gives `note_status` `error`, the last 300 characters of its message in `note_error` and a
    fixed `asked`, never the error text. The halt fields come from the story's latest failure entry (`null` when it
    has none). `asked_of` is the `waits_on` result, which reads `owner` once the external ticket reads done;
    `halt_asked_of` is that entry's own `asked_of` line (`entry_asked`)."""
    if ctx.tracker is None:
        raise RuntimeError("the tracker could not be read")
    blocked = [r for r in ctx.tracker if r["status"] == "blocked" and r["key"] not in ctx.covered
               and not is_epic_key(r["key"])]
    details = read_tracker_details([r["key"] for r in blocked])
    entries = []
    for row in blocked:
        d = details[row["key"]]
        note_read = d.get("status") == "ok"
        if note_read:
            note = d.get("note") or "marked blocked in sprint-status with no note"
        else:
            note = "marked blocked in sprint-status; its note could not be read"
        halt = (ctx.halts.get(row["key"]) or [{}])[-1]
        asked_of, ticket_status = ctx.waits_on(halt.get("fields"))
        entries.append(ctx.entry(
            row["key"], "blocked-row", asked_of, {"file": str(TRACKER_PATH), "line": row.get("line")},
            note, halt.get("since"),
            failure_step=halt.get("failure_step"), external_ticket=halt.get("external_ticket"), ticket_status=ticket_status,
            branch=halt.get("branch"), wip_commit=halt.get("wip_commit"),
            halt_asked_of=entry_asked(halt.get("fields") or {}),
            note_status="ok" if note_read else "error",
            note_error=None if note_read else (d.get("message") or "")[-MAX_ERR:]))
    return ok(TRACKER_PATH, rows=len(ctx.tracker), entries=len(entries)), entries


def sort_entries(found: list[dict]) -> list[dict]:
    sessions = [e for e in found if e["kind"] == "session"]
    rest = [e for e in found if e["kind"] != "session"]
    sessions.sort(key=lambda e: (MARK_RANK[e["mark"]], e["since"] is None,
                                 -parse_ts(e["since"]).timestamp() if e["since"] else 0))
    rest.sort(key=lambda e: (e["story"] is None, e["story"] or "", KIND_RANK[e["kind"]],
                             str(e["source"].get("file")), e["source"].get("line") or 0))
    return sessions + rest


def attention_section(sessions, identities, registry, tracker, tracker_src, tracker_keys, worktrees, worktrees_err,
                      shown) -> dict:
    """Everything that waits on the owner or an external owner. A source that fails is its own `error` and adds no
    entries."""
    ctx = AttentionContext(registry, tracker, tracker_keys, worktrees, worktrees_err, shown)
    branches, branches_err = story_branches()
    parts: dict[str, dict] = {}
    found: list[dict] = []

    def collect(name, source, fn):
        try:
            parts[name], more = fn()
        except Exception as exc:  # noqa: BLE001 -- a failing source becomes its own error
            parts[name], more = err(source, f"{type(exc).__name__}: {exc}"), []
        found.extend(more)

    events, unreadable_events, parts["events"] = {}, {}, none(
        ATTENTION_DIR, "the session listing failed, so no event file was read")
    if sessions["status"] == "none":
        parts["events"] = none(ATTENTION_DIR, f"no session listing, so no event file was read: {sessions['reason']}")
    if sessions["status"] == "ok":
        try:
            events, unreadable_events, parts["events"] = read_events(sessions["entries"])
        except Exception as exc:  # noqa: BLE001
            events, unreadable_events, parts["events"] = {}, {}, err(ATTENTION_DIR, f"{type(exc).__name__}: {exc}")

    def session_source():
        if sessions["status"] == "none":
            return none(SESSIONS_SOURCE, sessions["reason"]), []
        if sessions["status"] != "ok":
            return err(SESSIONS_SOURCE, sessions["error"]), []
        queued = session_queue(ctx, sessions["entries"], identities, events, unreadable_events)
        return ok(SESSIONS_SOURCE, listed=len(sessions["entries"]), entries=len(queued)), queued

    scopes: list[StoryScope] = []

    def scoped(reader):
        nonlocal scopes
        if not scopes:
            scopes = story_scopes(ctx)
        return reader(ctx, scopes)

    collect("sessions", SESSIONS_SOURCE, session_source)
    collect("specs", "story specs", lambda: scoped(lambda c, s: read_specs(c, s, branches, branches_err)))
    collect("records", "story records", lambda: scoped(read_records))
    collect("auto-dev failures", FAILURES_PATH, lambda: read_failures(ctx, branches, branches_err))
    collect("propagation", HISTORY_DIR, lambda: read_propagation(ctx))
    collect("external tracker", EXTERNAL_PATH or "config paths.design_questions", lambda: read_external(ctx))
    collect("sprint-status", TRACKER_PATH, lambda: read_blocked(ctx))
    order = ("sessions", "events", "specs", "records", "auto-dev failures", "propagation", "external tracker",
             "sprint-status")
    return ok(ATTENTION_SOURCE, entries=sort_entries(found), sources={k: parts[k] for k in order},
              note=ATTENTION_NOTE)


# ---------------------------------------------------------------- the snapshot

def tracker_section(tracker, tracker_src, join, key, details) -> dict:
    state, text = join
    if tracker is None:
        return err(TRACKER_PATH, f"the tracker could not be read: {tracker_src['error']}")
    if state == "ok":
        d = details[key]
        if d.get("status") != "ok":
            return err(TRACKER_PATH, d.get("message", "sprint_status get failed"))
        return ok(TRACKER_PATH, key=key, story_status=d["value"], note=d["note"], line=d["line"])
    if state == "none":
        return none(TRACKER_PATH, text)
    return err(TRACKER_PATH, text)


def story_git_section(sid, vs, row, worktree_path, worktree) -> dict:
    """The git section for a story: its worktree, else its branch read from the main checkout, else none."""
    if worktree_path:
        return git_section(worktree_path, "HEAD", True)
    if worktree["status"] == "ok":  # the registered worktree directory is gone
        candidates = list(dict.fromkeys(([row["branch"]] if row and row.get("branch") else [])
                                        + [story_branch(v) for v in vs]))
        branch = next((b for b in candidates if branch_exists(b)), None)
        if branch:
            return git_section(str(MAIN_REPO), branch, False)
        return none("git", f"worktree {worktree['path']} does not exist and no branch among "
                           f"{', '.join(candidates)} exists: nothing to read")
    branch_ref = worktree.get("branch") if worktree["status"] == "none" else None
    if branch_ref:
        return git_section(str(MAIN_REPO), branch_ref, False)
    if worktree["status"] == "error":
        return err("git worktree list", worktree["error"])
    return none("git", f"no worktree and no branch {story_branch(sid)}: nothing to read")


def read_globals() -> tuple:
    """(registry, registry text, registry source, registry error or None, tracker rows, tracker source,
    tracker keys, worktrees, worktrees error): the sources every story and session join against."""
    try:
        registry, registry_text, registry_src = read_registry()
    except Exception as exc:  # noqa: BLE001 -- a failing global reader becomes its source's error
        registry, registry_text = None, ""
        registry_src = err(REGISTRY_PATH, f"read_registry: {type(exc).__name__}: {exc}")
    registry_err = registry_src if registry_src["status"] == "error" else None
    tracker, tracker_src = read_tracker()
    tracker_keys = [r["key"] for r in tracker] if tracker is not None else None
    worktrees, worktrees_err = git_worktrees()
    return (registry, registry_text, registry_src, registry_err, tracker, tracker_src, tracker_keys, worktrees,
            worktrees_err)


def build_sessions(registry, registry_err, tracker, tracker_src, tracker_keys, worktrees, worktrees_err, runs,
                   shown) -> tuple[dict, list]:
    """(the `sessions` section, the identity of each of its entries)."""
    unreadable_sources = {}
    if registry_err:
        unreadable_sources["slot registry"] = registry_err["error"]
    if tracker is None:
        unreadable_sources["sprint-status tracker"] = tracker_src["error"]
    if worktrees is None:
        unreadable_sources["git worktree list"] = worktrees_err
    sessions = guard(SESSIONS_SOURCE, "sessions_section", lambda: sessions_section(
        story_forms(registry, tracker, tracker_keys), worktree_trees(registry, worktrees, tracker_keys), runs,
        shown, unreadable_sources))
    return sessions, take_identities(sessions)


def build_snapshot(story_ids: list[str] | None = None, with_cost: bool = True) -> dict:
    (registry, registry_text, registry_src, registry_err, tracker, tracker_src, tracker_keys, worktrees,
     worktrees_err) = read_globals()
    try:
        running, running_err, unreadable = scan_running()
    except Exception as exc:  # noqa: BLE001
        running, running_err, unreadable = None, f"scan_running: {type(exc).__name__}: {exc}", []
    jdir = journal_dir()
    if not os.path.isdir(jdir):
        journals = err(jdir, f"journal dir {jdir} does not exist")
    elif running is None:
        journals = err(jdir, running_err)
    elif unreadable:
        journals = err(jdir, f"cannot read {len(unreadable)} journal file(s), their agents are not listed: "
                             + "; ".join(unreadable))
    else:
        journals = ok(jdir, files=len(glob.glob(os.path.join(glob.escape(jdir), "*.jsonl"))))

    entries = story_set(story_ids, registry, tracker, tracker_keys)
    stories = []
    for entry in entries:
        sid = entry["story_id"]
        state, key, text = join_key(sid, tracker_keys)
        vs = variants_of(sid, key)
        row = find_registry_row(registry, vs, key, tracker_keys)
        stories.append({"story_id": sid, "_because": entry["because"], "_key": key, "_join": (state, text),
                        "_vs": vs, "_row": row, "_short": harness_metrics.short_id(key) if key else None})

    details = read_tracker_details(sorted({s["_key"] for s in stories if s["_key"]}))
    runs = guard(AUTO_DEV_DIR, "driver_runs_section", driver_runs_section)
    sessions, session_identities = build_sessions(
        registry, registry_err, tracker, tracker_src, tracker_keys, worktrees, worktrees_err, runs,
        {(s["_key"] or s["story_id"]): s["story_id"] for s in stories})
    attention = guard(ATTENTION_SOURCE, "attention_section", lambda: attention_section(
        sessions, session_identities, registry, tracker, tracker_src, tracker_keys, worktrees, worktrees_err,
        attention_shown(registry, tracker, tracker_keys)))
    if not with_cost:
        costs = {s["story_id"]: none("harness_metrics.py", "not read: --no-cost") for s in stories}
    else:
        try:
            costs = cost_sections(stories)
        except Exception as exc:  # noqa: BLE001
            costs = {s["story_id"]: err("harness_metrics.py", f"cost_sections: {type(exc).__name__}: {exc}")
                     for s in stories}
    out_stories = []
    for s in stories:
        sid, vs, row = s["story_id"], s["_vs"], s["_row"]
        project = wfconfig.stack_name(row['story_id'] if row else sid)
        slot = guard(REGISTRY_PATH, "slot_section", lambda: slot_section(sid, row, registry_text, registry_err))
        slot_info = slot if slot["status"] == "ok" else None
        try:
            worktree, wt_path = worktree_section(sid, vs, row, worktrees, worktrees_err)
        except Exception as exc:  # noqa: BLE001 -- the story keeps its other sections
            worktree, wt_path = err("git worktree list", f"worktree_section: {type(exc).__name__}: {exc}"), None
        s["_worktree"] = wt_path if worktree["status"] == "ok" and worktree["exists"] else None
        setup_src = SETUP_DIR

        tracker_sec = guard(TRACKER_PATH, "tracker_section",
                            lambda: tracker_section(tracker, tracker_src, s["_join"], s["_key"], details))

        if s["_worktree"]:
            arts, found_by = arts_of(s["_worktree"]), "worktree"
        else:
            arts, found_by = arts_of(MAIN_REPO), "main checkout"
        record, record_lines = record_section(vs, arts)
        git_sec = guard("git", "git_section", lambda: story_git_section(sid, vs, row, s["_worktree"], worktree))

        if running is None:
            running_sec = err(jdir, running_err)
        else:
            running_sec = None  # filled after attribution

        if runs["status"] == "ok":
            unreadable_runs = [r["run_id"] for r in runs["runs"]
                               if r.get("status") == "error" or r["stories"]["status"] == "error"]
            if unreadable_runs:
                run_ids = err(AUTO_DEV_DIR, "cannot tell which runs name this story: unreadable run(s) "
                                            + ", ".join(unreadable_runs))
            else:
                ids = [r["run_id"] for r in runs["runs"] if r["stories"]["status"] == "ok"
                       and any(sl.mentions(v, vs) for v in r["stories"]["ids"])]
                run_ids = ok(AUTO_DEV_DIR, ids=ids)
        else:
            run_ids = {k: v for k, v in runs.items() if k != "runs"}

        out_stories.append({
            "story_id": sid, "in_flight_because": s["_because"], "slot": slot, "tracker": tracker_sec,
            "worktree": worktree,
            "stack": {"bring_up": guard(setup_src, "bring_up_section", lambda: bring_up_section(project, s["_worktree"])),
                      "graph_project": guard(setup_src, "graph_section", lambda: graph_section(project, s["_worktree"])),
                      "services": guard(f"compose (project {project})", "services_section",
                                        lambda: services_section(project, slot_info, s["_worktree"], worktree))},
            "artifacts_root": ok(arts, path=arts, found_by=found_by),
            "position": guard(SCRIPTS_DIR / "story_ledger.py", "position_section",
                              lambda: position_section(sid, vs, row is not None, record_lines, arts)),
            "handoff": guard("story_ledger.handoff_state", "handoff_state", lambda: handoff_section(vs, record_lines, arts)),
            "record": record,
            "artifacts": {
                "spec": guard(arts, "spec_section", lambda: spec_section(vs, arts)),
                "findings": guard(arts, "findings", lambda: findings_section(vs, arts)),
                "deferred": guard(arts, "deferred", lambda: deferred_section(vs, arts)),
                "propagation": guard(setup_src, "propagation", lambda: propagation_section(vs, s["_worktree"])),
            },
            "git": git_sec,
            "agents": {"dispatched": guard(jdir, "load_facts", lambda: dispatched_section(vs)), "running": running_sec},
            "cost": costs[sid],
            "driver_run_ids": run_ids,
            "session_ids": session_ids_section(sessions, session_identities, s["_key"] or sid),
        })

    if running is None:
        unattributed = err(jdir, running_err)
    else:
        try:
            grouped = attribute_running(running, stories)
        except Exception as exc:  # noqa: BLE001
            failed = err(jdir, f"attribute_running: {type(exc).__name__}: {exc}")
            for story in out_stories:
                story["agents"]["running"] = dict(failed)
            unattributed = dict(failed)
        else:
            for story in out_stories:
                story["agents"]["running"] = ok(jdir, agents=grouped[story["story_id"]], note=RUNNING_NOTE,
                                                format="internal")
            unattributed = ok(jdir, agents=grouped[None], format="internal",
                              note="Agents whose cwd is no story worktree and whose workflow run or "
                                   "description names no single story in the set.")
    return {"generated_at": now_iso(), "main_repo": str(MAIN_REPO),
            "sources": {"slot_registry": registry_src, "sprint_status": tracker_src, "journals": journals},
            "stories": out_stories, "driver_runs": runs, "sessions": sessions, "attention": attention,
            "unattributed_agents": unattributed}


def build_attention() -> dict:
    """The fast read: the sessions and the attention queue, with none of the per-story sections."""
    (registry, _text, registry_src, registry_err, tracker, tracker_src, tracker_keys, worktrees,
     worktrees_err) = read_globals()
    runs = guard(AUTO_DEV_DIR, "driver_runs_section", driver_runs_section)
    shown = attention_shown(registry, tracker, tracker_keys)
    sessions, identities = build_sessions(registry, registry_err, tracker, tracker_src, tracker_keys, worktrees,
                                          worktrees_err, runs, shown)
    attention = guard(ATTENTION_SOURCE, "attention_section", lambda: attention_section(
        sessions, identities, registry, tracker, tracker_src, tracker_keys, worktrees, worktrees_err, shown))
    return {"generated_at": now_iso(), "main_repo": str(MAIN_REPO),
            "sources": {"slot_registry": registry_src, "sprint_status": tracker_src},
            "sessions": sessions, "attention": attention}


class UsageError(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def main(argv=None) -> int:
    ap = JsonArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--story", action="append", default=None, metavar="ID",
                    help="story id; repeatable; replaces the in-flight set")
    ap.add_argument("--attention", action="store_true",
                    help="print only the sessions and the attention queue")
    ap.add_argument("--no-cost", action="store_true",
                    help="skip the harness_metrics.py read: every story's cost is a none section")
    try:
        args = ap.parse_args(argv)
        if any(not sid.strip() for sid in args.story or []):
            raise UsageError("--story needs a non-empty story id")
        if args.attention and args.story:
            raise UsageError("--attention cannot be combined with --story: the queue always covers every story")
        if args.attention and args.no_cost:
            raise UsageError("--attention cannot be combined with --no-cost: it builds no cost data")
    except UsageError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        main_repo = wfconfig.repo_root()
        if not (main_repo / ".git").exists():
            raise RuntimeError(f"{main_repo} is not the main checkout (no .git)")
        configure(main_repo)
    except Exception as exc:  # noqa: BLE001 -- reported as the usage-level error line
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 2
    document = build_attention() if args.attention else build_snapshot(args.story, with_cost=not args.no_cost)
    print(json.dumps(document, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
