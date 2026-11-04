#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Run the auto-dev-loop one phase per fresh headless agent session.

Each session runs `/auto-dev-loop driver story=<id|none>` headless, never resumed, so no phase
carries an earlier phase's context. The tool is `config: loop.driver_tool`:
  * `claude` (default): `claude -p "<prompt>" --model <M> --effort <E> --permission-mode
    <config: loop.driver_permission_mode> --output-format stream-json --verbose --name <name>`;
    `--model` / `--effort` come from `--orchestrator-model` / `--orchestrator-effort` (default
    `config: loop.orchestrator_model` / `loop.orchestrator_effort`).
  * `opencode`: `config: loop.opencode_cmd` (default `opencode run {prompt}`; `{prompt}` and
    `{model}` are filled, each one argument). Its output is kept as the session stream and its
    whole text is read for the phase line; there is no init check and no cost. UNTESTED against a
    live OpenCode.
The auto-dev-loop skill's "Driver mode" section says which phase a session runs and the
`AUTO-DEV-PHASE` line it ends with.

Per session:
  * name: `<run-id>-s<n>` while the driver's story is unset, `<story>-<run-id>-s<n>` once set.
  * environment: the launching session's own Claude Code variables are removed (`SESSION_VARS`);
    `auto_dev_guard.MARKER` carries the run id (the keep-halted guard and `cleanup_story_stack.py`
    then refuse to remove an unmerged story's worktree, branch or stack volumes);
    `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` keeps `claude -p` from stopping a background workflow
    (the review) after its last turn — `--session-timeout-min` is the bound instead. On Windows
    the driver needs Git Bash (`CLAUDE_CODE_GIT_BASH_PATH`): without it Claude Code runs hooks
    through PowerShell, where an exit-code block does not block.
  * startup check (claude): the stream's `init` event must list the tools in
    `config: loop.required_tools`, the `auto-dev-loop` skill and the agents in
    `config: loop.required_agents`; otherwise the session is killed and the driver stops.
  * outcome: the last `result` event. `is_error: true` stops the driver; it is never a story
    failure.
  * progress: decided from the record, not from the session's text. Afterwards the story
    ledger's commits, handoff state, sprint-status line and record-file lines
    (`ledger_fingerprint`) or the failure log must show a new fact, or the driver stops. A
    `waiting` session needs no new fact and does not count toward `--max-stories`.
  * config fingerprint: `sessions.jsonl` gets one line per session with `knobs_id` (hash of every
    agent's model / effort / maxTurns, the driver flags, the tool version and main model),
    `harness_id` (hash of every harness file), the main-checkout HEAD, and the session's story,
    outcome, step, minutes, cost and turns.
  * harness drift: files uncommitted at the session's start, changed since the previous session
    and changed while it ran are recorded and named in `driver.log`; drift never stops a run.
  * audit: `auto_dev_audit.py` checks the story the session drove; a problem stops the driver.

The land lease (when the kit's `land_lease.py` and `merge_train.py` are installed): a Step 11
session that finds the lease held, or stories ahead in its line, ends `outcome=waiting` with
nothing torn down. The driver joins the lease's first-come-first-served line and waits with no
session running: every poll it stops on a STOP request, applies the stall guard
(`--land-stall-min`), runs one merge-train step (`merge_train.advance`), takes the lease at the
story's turn (its merge approval checked first) and otherwise prints the holder, the position and
the time waited. A red train gate withdraws the approval and leaves the line; the next session
runs the story's recovery. Without those scripts a `waiting` outcome stops the driver.
Release rule: after every session that does not end `landed`, and on every driver exit, the
driver releases the lease the driven story holds and leaves the line.

The driver stops on: `terminated`, `infra-stop`, an error, a session timeout, no progress,
`--max-stories` stories landed or halted, `--max-cost-usd` spent, a stalled lease holder, or the
run's STOP file (`<run-dir>/STOP`, written by `stop <run-id>`), or at once on its STOP-NOW file
(`stop <run-id> --now`; see Watchdog). Run directories: `.workflow/state/auto-dev/<run-id>/`
(`driver.log`, `sessions.jsonl`, `session-<n>.jsonl`, `run.json`).

Run record (`run.json`): `pid`, `create_time` (psutil when installed), `started_utc`, `detached`,
`options`, `current` (`starting`, `session`, `waiting-in-line`, `train-gate`, `ending`) and
`ended` (`null` while the run lives, then `utc`, `exit_code`, `reason`). Written through a temp
file; the first write publishes exclusively. A run killed by force writes no `ended`; `list`
reads that as `dead`. An end record `run.json` cannot take goes to `run.json.end`.

Watchdog: a daemon thread judges the run stuck when no `driver.log` line and no session stream
line arrived for `--session-timeout-min` plus `STUCK_GRACE_SECONDS`; it kills the driver's own
descendants, leaves the line, releases the lease the driven story holds, writes `ended` (exit 1)
and exits. A train gate's silence and a host sleep are not counted. On the STOP-NOW file it does
the same as an immediate stop, giving the main thread `STUCK_GRACE_SECONDS` to end the run first.

Exit codes: 0 stopped as asked (terminated, a cap, STOP); 1 stopped on a problem, or after the
watchdog set its flag; 2 could not start (no CLI, a bad `--run-id`, a record published first).

Usage
-----
    uv run --no-project .workflow/scripts/auto_dev_driver.py [--story <id>] [--max-stories N]
        [--max-cost-usd X] [--session-timeout-min M] [--land-stall-min M]
        [--orchestrator-model M] [--orchestrator-effort E]
    uv run --no-project .workflow/scripts/auto_dev_driver.py start [the same options] [--json]
    uv run --no-project .workflow/scripts/auto_dev_driver.py list [--json]
    uv run --no-project .workflow/scripts/auto_dev_driver.py stop <run-id> [--now] [--json]

The bare form runs in the foreground. `start` runs the same run detached and prints
`run <id> pid <pid> started` once the run's record shows a live process (exit 0; 1 no record
within the bound; 2 refused or the run process exited first). `list` states every run: pid,
story, phase, spend against the cap and state (unknown, ended, dead, stuck, stopping, running).
`stop <run-id>` writes the STOP file (exit 0; 1 when the run is not live; 2 on a bad id); with
`--now` the STOP-NOW file. `--state-dir` (tests) replaces `.workflow/state/auto-dev`.

Stdlib only; psutil is used when installed (process identity and tree kill).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_dev_audit  # noqa: E402
import auto_dev_guard  # noqa: E402
import wfconfig  # noqa: E402

try:  # the land window's scripts: optional
    import land_lease  # type: ignore  # noqa: E402
    import merge_train  # type: ignore  # noqa: E402
except ImportError:
    land_lease = None
    merge_train = None

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None

REPO = wfconfig.repo_root()
STATE = wfconfig.workflow_dir() / "state" / "auto-dev"
SCRIPTS = Path(__file__).resolve().parent
LEDGER = SCRIPTS / "story_ledger.py"

SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID", "CLAUDE_CODE_EXECPATH", "CLAUDE_EFFORT",
)
DEFAULT_REQUIRED_TOOLS = ("Bash", "Agent", "Skill")
TOOL_NAMES = {"Agent": ("Agent", "Task")}
DEFAULT_REQUIRED_AGENTS = ("story-spec", "story-impl", "findings-evaluator", "findings-verifier",
                           "story-finalize", "story-rebase")
WAIT_POLL_SECONDS = getattr(land_lease, "HEARTBEAT_SECONDS", 30.0)
READER_JOIN_SECONDS = 30
STUCK_GRACE_SECONDS = READER_JOIN_SECONDS + WAIT_POLL_SECONDS
SUSPEND_GAP_POLLS = 2
START_BOUND_SECONDS = 10.0
RUN_ID_FORMAT = "%Y%m%d-%H%M%S"
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}$")
RUN_RECORD = "run.json"
RUN_END_MARKER = "run.json.end"
RUN_STOP = "STOP"
RUN_STOP_NOW = "STOP-NOW"
STOP_NOW_POLL_SECONDS = 1.0
ID_ATTEMPTS = 20
ID_WAIT_SECONDS = 0.25
RECORD_WRITE_TRIES = 20
EXIT_INTERRUPTED = 130
DEFAULT_STALL_MIN = getattr(merge_train, "STALL_BUDGET_MINUTES", 180)
STUCK = threading.Event()
IMMEDIATE_STOP = threading.Event()
STUCK_NOTE = ""
GATE_RUNNING = threading.Event()
_event_at = time.monotonic()
PHASE_LINE = re.compile(
    r"^AUTO-DEV-PHASE story=(?P<story>\S+) outcome=(?P<outcome>advanced|landed|halted|waiting|terminated|infra-stop)"
    r"(?: step=(?P<step>\S+))?\s*$", re.M)
RECORD_FILE = re.compile(r"^_record file: (.+)_$", re.M)
AGENT_KNOBS = ("model", "effort", "maxTurns")
HARNESS_FILES = ("CLAUDE.md", "AGENTS.md", ".claude/settings.json", "opencode.json", ".workflow/config.toml",
                 ".workflow/AGENTS.md", ".workflow/AGENTS.local.md")
HARNESS_GLOBS = ((".claude/agents", "*.md"), (".claude/skills", "**/*"), (".claude/workflows", "*.js"),
                 (".opencode/agents", "*.md"), (".opencode/skills", "**/*"), (".opencode/plugins", "*"),
                 (".workflow/rules", "**/*.md"), (".workflow/scripts", "*.py"), (".workflow/hooks", "**/*.py"),
                 (".workflow/agents", "*.md"))


# ------------------------------------------------------------------ configuration

def cfg(key: str, default):
    try:
        return wfconfig.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def driver_tool() -> str:
    return str(cfg("loop.driver_tool", "claude"))


def required_tools() -> tuple[str, ...]:
    return tuple(cfg("loop.required_tools", list(DEFAULT_REQUIRED_TOOLS)) or ())


def required_agents() -> tuple[str, ...]:
    return tuple(cfg("loop.required_agents", list(DEFAULT_REQUIRED_AGENTS)) or ())


def harness_globs() -> list[tuple[str, str]]:
    extra = [tuple(x) for x in (cfg("loop.extra_harness_globs", []) or []) if isinstance(x, (list, tuple)) and len(x) == 2]
    return list(HARNESS_GLOBS) + extra


# ------------------------------------------------------------------ fingerprints

def git_bash_path(env: dict[str, str]) -> Path | None:
    """Git Bash as Claude Code looks for it on Windows: the variable, the default installs, then
    the `bin\\bash.exe` of a git installation on PATH."""
    set_path = env.get("CLAUDE_CODE_GIT_BASH_PATH")
    if set_path and Path(set_path).is_file():
        return Path(set_path)
    for p in (Path(r"C:\Program Files\Git\bin\bash.exe"), Path(r"C:\Program Files (x86)\Git\bin\bash.exe")):
        if p.is_file():
            return p
    for d in env.get("PATH", "").split(os.pathsep):
        git = Path(d) / "git.exe"
        if not git.is_file():
            continue
        for root in list(git.parents)[1:4]:
            if (root / "bin" / "bash.exe").is_file():
                return root / "bin" / "bash.exe"
    return None


# The kit installed as a Claude Code plugin: its files live beside this scripts dir, outside the project.
KIT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_INSTALL = (KIT_ROOT / "hooks" / "hooks.json").is_file()
PLUGIN_GLOBS = (("agents", "*.md"), ("skills", "**/*"), ("workflows", "*.js"), ("rules", "**/*.md"),
                ("scripts", "*.py"), ("hooks", "**/*.py"), ("hooks", "hooks.json"), (".", "AGENTS.md"))


def agent_knobs(repo: Path | None = None) -> dict[str, dict[str, str]]:
    """Each installed agent's `model`, `effort` and `maxTurns` from its frontmatter."""
    repo = repo or REPO
    out = {}
    folders = [repo / ".claude" / "agents", repo / ".opencode" / "agents"]
    if PLUGIN_INSTALL:
        folders.append(KIT_ROOT / "agents")
    for folder in folders:
        for path in sorted(folder.glob("*.md")):
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines or lines[0].strip() != "---":
                continue
            knobs = {}
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                key, sep, value = line.partition(":")
                if sep and key.strip() in AGENT_KNOBS:
                    knobs[key.strip()] = value.strip()
            out.setdefault(path.stem, knobs)
    return out


def harness_paths(repo: Path | None = None) -> list[Path]:
    repo = repo or REPO
    paths = [repo / name for name in HARNESS_FILES]
    for folder, pattern in harness_globs():
        paths += sorted(p for p in (repo / folder).glob(pattern)
                        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return [p for p in dict.fromkeys(paths) if p.is_file()]


def plugin_paths() -> list[Path]:
    """The plugin's own harness files (empty outside a plugin install)."""
    if not PLUGIN_INSTALL:
        return []
    paths = []
    for folder, pattern in PLUGIN_GLOBS:
        paths += sorted(p for p in (KIT_ROOT / folder).glob(pattern)
                        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return list(dict.fromkeys(paths))


def harness_id(repo: Path | None = None) -> str:
    repo = repo or REPO
    digest = hashlib.sha256()
    for path in harness_paths(repo):
        digest.update(path.relative_to(repo).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    for path in plugin_paths():
        digest.update(b"plugin:" + path.relative_to(KIT_ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def harness_files(repo: Path | None = None) -> dict[str, str]:
    repo = repo or REPO
    return {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in harness_paths(repo)}


def harness_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def uncommitted_harness(files: dict[str, str], repo: Path | None = None) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo or REPO), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    changed, entries = set(), iter(out.split("\0"))
    for entry in entries:
        if len(entry) > 3:
            changed.add(entry[3:])
            if entry[0] in "RC":
                next(entries, None)
    return sorted(changed & files.keys())


def describe_changes(paths: list[str], uncommitted: list[str], limit: int = 8) -> str:
    named = [p + (" (uncommitted)" if p in uncommitted else "") for p in paths[:limit]]
    return ", ".join(named) + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def main_head(repo: Path | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo or REPO), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def knobs_id(knobs: dict) -> str:
    return hashlib.sha256(json.dumps(knobs, sort_keys=True).encode()).hexdigest()[:12]


def session_record(n: int, knobs: dict, harness: str, head: str, init: dict | None, result: dict,
                   phase: dict | None, minutes: float, drift: dict | None = None) -> dict:
    knobs = dict(knobs, tool_version=(init or {}).get("claude_code_version"), model=(init or {}).get("model"))
    return {
        "session": n, "knobs_id": knobs_id(knobs), "harness_id": harness, "head": head,
        "story": phase["story"] if phase else None, "outcome": phase["outcome"] if phase else None,
        "step": phase["step"] if phase else None, "minutes": round(minutes, 1),
        "cost_usd": result.get("total_cost_usd"), "turns": result.get("num_turns"),
        "is_error": result.get("is_error"), "knobs": knobs, **(drift or {}),
    }


def session_env(bash: Path | None, run_id: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}
    if bash:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = str(bash)
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    env[auto_dev_guard.MARKER] = run_id
    return env


def init_problem(init: dict) -> str | None:
    tools = set(init.get("tools") or [])
    missing = [t for t in required_tools() if tools.isdisjoint(TOOL_NAMES.get(t, (t,)))]
    if "auto-dev-loop" not in (init.get("skills") or []):
        missing.append("skill auto-dev-loop")
    from agent_roles import unscope
    have = {unscope(str(x)) for x in (init.get("agents") or [])}
    missing += [f"agent {a}" for a in required_agents() if a not in have]
    return f"session lacks {', '.join(missing)}" if missing else None


def phase_line(text: str) -> dict | None:
    found = list(PHASE_LINE.finditer(text or ""))
    return found[-1].groupdict() if found else None


def ledger_fingerprint(text: str) -> tuple[str, ...]:
    """The facts that change only when the story advances: commits, handoff state, sprint status
    and the record file's lines. Journal-projected lines (position, rounds) are left out."""
    keep, section = [], ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line
        if line.startswith("_sprint-status:"):
            keep.append(line)
        elif section.startswith("## Commits") and re.match(r"- [0-9a-f]{7,} ", line):
            keep.append(line)
        elif section.startswith("## Handoff state") and line.startswith("- "):
            keep.append(line)
    record = RECORD_FILE.search(text)
    if record:
        path = REPO / record.group(1).strip()
        if path.is_file():
            keep += [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
    return tuple(keep)


def ledger(story: str) -> str:
    r = subprocess.run([sys.executable, str(LEDGER), story], cwd=REPO, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)
    return r.stdout


def same_story(a: str, b: str) -> bool:
    return a.replace(".", "-") == b.replace(".", "-")


def failure_log() -> Path:
    return wfconfig.path("paths.auto_dev_failure_log", "docs/implementation-artifacts/auto-dev-failures.md")


def failure_entries(story: str) -> int:
    log = failure_log()
    if not log.is_file():
        return 0
    heads = re.findall(r"^## (\S+) — ", log.read_text(encoding="utf-8"), re.M)
    return sum(1 for head in heads if same_story(head, story))


def progressed(before: tuple[str, ...] | None, failures_before: int,
               after: tuple[str, ...], failures_after: int) -> bool:
    """A new failure-log entry, or (for a story known before the session) a changed ledger
    fingerprint, or (for a story the session picked) a recorded spec approval."""
    if failures_after > failures_before:
        return True
    if before is None:
        return any(line.startswith("- ok") and "spec approved" in line for line in after)
    return after != before


# ------------------------------------------------------------------ processes

def kill_tree(proc: subprocess.Popen) -> None:
    if psutil is not None:
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                with contextlib.suppress(psutil.Error):
                    child.kill()
            parent.kill()
            return
        except psutil.Error:
            return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_event(at: float | None = None) -> None:
    global _event_at
    _event_at = time.monotonic() if at is None else at


def last_event() -> float:
    return _event_at


def stuck_threshold_s(session_timeout_min: float) -> float:
    return session_timeout_min * 60 + STUCK_GRACE_SECONDS


def watchdog_tick(threshold_s: float, on_stuck, clock=time.monotonic) -> bool:
    if GATE_RUNNING.is_set():
        return False
    silence = clock() - _event_at
    if silence > threshold_s:
        on_stuck(silence)
        return True
    return False


@contextlib.contextmanager
def train_gate(rec: "RunRecord | None" = None, session: int | None = None, story: str | None = None):
    """Around `merge_train.advance`: the watchdog counts none of the call's silence."""
    before = rec.data["current"] if rec else None
    GATE_RUNNING.set()
    try:
        if rec:
            rec.activity("train-gate", session, story)
        yield
    finally:
        mark_event()
        GATE_RUNNING.clear()
        if rec:
            rec.update(current=before)


def own_create_time() -> float | None:
    return psutil.Process().create_time() if psutil is not None else None


def process_alive(pid: int, create_time: float | None) -> bool:
    if psutil is not None:
        try:
            return create_time is None or psutil.Process(pid).create_time() == create_time
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_own_descendants(timeout: float = 10.0) -> list[int]:
    """Kill every descendant of this process, deepest first. Needs psutil."""
    if psutil is None:
        raise OSError("psutil is not installed: descendants cannot be listed")
    children = psutil.Process().children(recursive=True)
    signalled, left = [], []
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            left.append(f"{child.pid} ({exc})")
            continue
        signalled.append(child)
    _, alive = psutil.wait_procs(signalled, timeout=timeout)
    left += [f"{child.pid} (alive {timeout:g} s after the kill)" for child in alive]
    if left:
        raise OSError(f"could not kill {', '.join(left)}")
    return [child.pid for child in children]


def exit_process(code: int) -> None:
    sys.stdout.flush()
    os._exit(code)


def end_cause() -> str:
    return "immediate stop" if IMMEDIATE_STOP.is_set() else "stuck"


class RunStuck(Exception):
    """The main thread reached a process spawn after the watchdog ended the run."""


def unless_stuck(call, *args):
    if STUCK.is_set():
        raise RunStuck(f"{end_cause()}: no new process started; stopping")
    return call(*args)


def run_session(cmd: list[str], env: dict[str, str], stream_path: Path, timeout_s: float,
                tool: str = "claude") -> dict:
    """Run one headless session; return {init, result, problem}. The stream goes to stream_path.
    For a non-claude tool the result is synthesised from the whole output text."""
    out = {"init": None, "result": None, "problem": None}
    if STUCK.is_set():
        out["problem"] = ("immediate stop: no session was started" if IMMEDIATE_STOP.is_set()
                          else "stuck: the run was declared stuck; no session was started")
        return out
    popen_kw = {"start_new_session": True} if os.name != "nt" else {}
    proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **popen_kw)
    session_over = threading.Event()
    text: list[str] = []

    def read() -> None:
        with open(stream_path, "w", encoding="utf-8") as log:
            for line in proc.stdout:
                if not session_over.is_set():
                    mark_event()
                log.write(line)
                log.flush()
                if tool != "claude":
                    text.append(line)
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init" and out["init"] is None:
                    out["init"] = event
                    problem = init_problem(event)
                    if problem:
                        out["problem"] = problem
                        kill_tree(proc)
                elif event.get("type") == "result":
                    out["result"] = event

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            out["problem"] = out["problem"] or f"session timed out after {timeout_s / 60:.0f} min"
            kill_tree(proc)
            proc.wait()
        except KeyboardInterrupt:
            kill_tree(proc)
            raise
        reader.join(timeout=READER_JOIN_SECONDS)
        if not reader.is_alive():
            proc.stdout.close()
        if tool != "claude":
            out["init"] = {"tool": tool}
            if not out["problem"]:
                out["result"] = {"result": "".join(text), "is_error": proc.returncode != 0,
                                 "total_cost_usd": None, "num_turns": None}
        elif not out["problem"] and out["init"] is None:
            out["problem"] = f"session exited (code {proc.returncode}) without an init event"
        return out
    finally:
        session_over.set()


# ------------------------------------------------------------------ land lease

def release_lease(lease_path: Path | None, story: str, note) -> bool:
    if land_lease is None or lease_path is None:
        return False
    outcome, _ = land_lease.release(lease_path, story)
    if outcome == "released":
        note(f"released the land lease held by {story}")
    return outcome == "released"


def wait_in_line(story: str, lease_path: Path, stop_file: Path, stall_min: float, note, gate=train_gate) -> int | None:
    """Wait in the land lease's line for the story's turn, with no session running.

    Returns None once the driver holds the lease for the story or a red train gate withdrew the
    story's approval, else the driver's exit code (0 on a STOP file, 1 on a stall, a stuck run, an
    approval that no longer stands, or a lease or train error). The story's entry leaves the line
    on every return that is not the turn."""
    if land_lease is None or merge_train is None:
        note("the land lease scripts (land_lease.py, merge_train.py) are not installed; stopping")
        return 1
    started = time.monotonic()
    joined = None
    free_head, free_since = None, 0.0

    def ended_by_watchdog() -> bool:
        if not STUCK.is_set():
            return False
        land_lease.leave(lease_path, story)
        note(f"{end_cause()}: left the land lease's line; stopping")
        return True

    try:
        merge_train.clear_gating(story, lease_path)
        while True:
            if ended_by_watchdog():
                return 1
            if stop_file.is_file():
                land_lease.leave(lease_path, story)
                note(f"stop requested ({stop_file}); left the land lease's line; stopping")
                return 0
            record = land_lease.read_record(lease_path)
            holder = {"story": record["story"], "acquired": record["acquired"]} if "story" in record else None
            line = record.get("line", [])
            if holder and same_story(holder["story"], story):
                return None
            if holder and merge_train.holder_stall_exit(record, lease_path, stall_min):
                land_lease.leave(lease_path, story)
                note(f"land lease stalled: {holder['story']} has held it for {land_lease.age(holder)}, past the"
                     f" --land-stall-min budget of {stall_min:g} min")
                note(f"{story} left the line; its slot, worktree and stack are untouched and nothing was written to"
                     " the failure log")
                note("recover: `uv run --no-project .workflow/scripts/land_lease.py status`; once the holder's session"
                     f" is known dead, and only on the owner's instruction, `land_lease.py release {holder['story']}`;"
                     f" then resume with `auto_dev_driver.py --story {story}`")
                return 1
            if holder is None and line and not same_story(line[0]["story"], story):
                head, now = line[0]["story"], time.monotonic()
                if free_head is None or not same_story(head, free_head):
                    free_head, free_since = head, now
                elif now - free_since > stall_min * 60:
                    land_lease.leave(lease_path, story)
                    with contextlib.suppress(Exception):
                        merge_train.home(story)
                    note(f"land lease stalled: it has stood free for {land_lease.duration(now - free_since)} behind"
                         f" {head}, the head of the line, past the --land-stall-min budget of {stall_min:g} min")
                    note(f"{story} left the line; nothing was written to the failure log; find and stop the waiter"
                         f" of {head}, then resume with `auto_dev_driver.py --story {story}`")
                    return 1
            else:
                free_head = None
            with gate():
                step = merge_train.advance(story, lease_path)
            if step.kind == "withdrawn":
                note(f"the train gate for {story} was red: {step.text}; the merge approval is withdrawn and {story} "
                     "left the line")
                return None
            if step.kind in merge_train.WORKED:
                note(f"merge train: {step.text}")
                continue
            if holder is None and (not line or same_story(line[0]["story"], story)):
                if ended_by_watchdog():
                    return 1
                approved, reason = land_lease.approval(story)
                if approved is None:
                    land_lease.leave(lease_path, story)
                    note(f"the merge approval for {story} no longer stands ({reason}); left the line; stopping")
                    return 1
                outcome, _ = land_lease.take(lease_path, story)
                if outcome in ("acquired", "already"):
                    note(f"took the land lease for {story} after {land_lease.duration(time.monotonic() - started)}"
                         " in the line")
                    return None
            if ended_by_watchdog():
                return 1
            position, record = land_lease.join(lease_path, story)
            entry = next((e for e in record.get("line", []) if same_story(e["story"], story)), None)
            if entry and joined and entry["joined"] != joined:
                note(f"{story}'s line entry was dropped as stale (the host slept?); joined again at the back")
            joined = entry["joined"] if entry else joined
            state = f"held by {holder['story']} for {land_lease.age(holder)}" if holder else "free"
            note(f"waiting for the land lease: {state}; {story} is position {position} in the line;"
                 f" {land_lease.duration(time.monotonic() - started)} waited"
                 + (f"; merge train: {step.text}" if step.text and step.kind in ("waiting", "refused", "failed", "not-run")
                    else ""))
            time.sleep(WAIT_POLL_SECONDS)
    except Exception as exc:  # noqa: BLE001 -- lease, train and OS errors all end the wait the same way
        with contextlib.suppress(Exception):
            land_lease.leave(lease_path, story)
        with contextlib.suppress(Exception):
            merge_train.home(story)
        note(f"land lease: {exc}; stopping")
        return 1


def exit_steps(lease_path: Path | None, story: str | None, note) -> None:
    if not story or land_lease is None or lease_path is None:
        return
    try:
        land_lease.leave(lease_path, story)
    except Exception as exc:  # noqa: BLE001
        note(f"land lease: could not leave the line: {exc}")
    try:
        release_lease(lease_path, story, note)
    except Exception as exc:  # noqa: BLE001
        note(f"land lease: could not release the lease: {exc}")


# ------------------------------------------------------------------ run record

class RunRecord:
    """`<run-dir>/run.json`: written atomically; the first write publishes exclusively."""

    def __init__(self, run_dir: Path, data: dict, log=None):
        self.path = run_dir / RUN_RECORD
        self.data = data
        self.log = log
        self.published = False
        self.lock = threading.Lock()

    def write(self) -> None:
        text = json.dumps(self.data, indent=2)
        temp = self.path.with_name(RUN_RECORD + ".tmp")
        for attempt in range(RECORD_WRITE_TRIES):
            try:
                temp.write_text(text, encoding="utf-8")
                if self.published:
                    os.replace(temp, self.path)
                else:
                    if self.path.exists():  # POSIX rename replaces silently: refuse a record published first
                        temp.unlink(missing_ok=True)
                        raise FileExistsError(str(self.path))
                    os.rename(temp, self.path)
                    self.published = True
                return
            except FileExistsError:
                with contextlib.suppress(OSError):
                    temp.unlink(missing_ok=True)
                raise
            except PermissionError:
                if attempt == RECORD_WRITE_TRIES - 1:
                    raise
                threading.Event().wait(0.05)

    def report(self, path: Path, exc: OSError) -> None:
        text = f"cannot write {path}: {exc}"
        print(f"auto-dev-driver: {text}", file=sys.stderr)
        if self.log is not None:
            try:
                self.log(text)
            except Exception as log_exc:  # noqa: BLE001
                print(f"auto-dev-driver: cannot log {text!r}: {log_exc}", file=sys.stderr)

    def update(self, **fields) -> None:
        with self.lock:
            self.data.update(fields)
            try:
                self.write()
            except OSError as exc:
                self.report(self.path, exc)

    def activity(self, activity: str, session: int | None, story: str | None) -> None:
        self.update(current={"activity": activity, "session": session, "story": story, "since": iso(utc_now())})

    def end(self, exit_code: int, reason: str) -> bool:
        with self.lock:
            if self.data["ended"] is not None:
                return False
            self.data["ended"] = {"utc": iso(utc_now()), "exit_code": exit_code, "reason": reason}
            try:
                self.write()
            except OSError as exc:
                self.report(self.path, exc)
                if self.published:
                    self.write_end_marker()
            return True

    def write_end_marker(self) -> None:
        marker = self.path.with_name(RUN_END_MARKER)
        try:
            marker.write_text(json.dumps(self.data["ended"], indent=2), encoding="utf-8")
        except OSError as exc:
            self.report(marker, exc)


def new_run_data(run_dir: Path, pid: int, create_time: float | None, detached: bool, options: dict) -> dict:
    return {
        "run_id": run_dir.name, "pid": pid, "create_time": create_time,
        "started_utc": iso(datetime.strptime(run_dir.name, RUN_ID_FORMAT)), "detached": detached,
        "options": options,
        "current": {"activity": "starting", "session": None, "story": options["story"], "since": iso(utc_now())},
        "ended": None}


def read_end_marker(run_dir: Path) -> dict | None:
    try:
        marker = json.loads((run_dir / RUN_END_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) and "exit_code" in marker else None


def read_run_record(run_dir: Path) -> dict:
    """The run's `run.json`: `ok` with its fields (a `run.json.end` merged in), `none`, or `error`."""
    path = run_dir / RUN_RECORD
    if not path.is_file():
        return {"status": "none", "source": str(path), "reason": "no run.json"}
    for attempt in range(5):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            if attempt == 4:
                return {"status": "error", "source": str(path), "error": f"cannot read run.json: {exc}"}
            threading.Event().wait(0.05)
            continue
        except ValueError as exc:
            return {"status": "error", "source": str(path), "error": f"run.json is not JSON: {exc}"}
        if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
            return {"status": "error", "source": str(path), "error": "run.json is not a run record (no integer pid)"}
        marker = None if data.get("ended") else read_end_marker(run_dir)
        if marker:
            data["ended"] = marker
        return {"status": "ok", "source": str(path), **data}
    raise AssertionError("unreachable")


def _say(note):
    def say(text: str) -> None:
        try:
            note(text)
        except Exception as exc:  # noqa: BLE001 -- a log write that fails must not keep the thread from its exit
            print(f"auto-dev-driver: cannot note {text!r}: {exc}", file=sys.stderr)
    return say


def declare_stuck(silence: float, session_timeout_min: float, rec: RunRecord, note, lease_path, get_story) -> None:
    global STUCK_NOTE
    say = _say(note)
    msg = (f"stuck: no new event for {silence / 60:.0f} min (threshold {session_timeout_min:g} min + "
           f"{STUCK_GRACE_SECONDS:g} s); killing this run's own process tree")
    STUCK_NOTE = msg
    STUCK.set()
    say(msg)
    try:
        kill_own_descendants()
    except Exception as exc:  # noqa: BLE001
        say(f"stuck: could not kill every descendant: {exc}")
    try:
        exit_steps(lease_path, get_story(), say)
    except Exception as exc:  # noqa: BLE001
        say(f"stuck: exit steps failed: {exc}")
    rec.end(1, msg)
    exit_process(1)


def declare_immediate_stop(stop_file: Path, rec: RunRecord, note, lease_path, get_story,
                           main_exited: threading.Event) -> None:
    global STUCK_NOTE
    say = _say(note)
    msg = f"immediate stop requested ({stop_file}); killing this run's own process tree"
    STUCK_NOTE = msg
    IMMEDIATE_STOP.set()
    STUCK.set()
    say(msg)
    if merge_train is not None and hasattr(merge_train, "GATE_ABORT"):
        merge_train.GATE_ABORT.set()
    try:
        killed = kill_own_descendants()
        say(f"immediate stop requested ({stop_file}): killed {len(killed)} processes")
    except Exception as exc:  # noqa: BLE001
        say(f"immediate stop requested ({stop_file}): could not kill every descendant: {exc}")
    if main_exited.wait(STUCK_GRACE_SECONDS):
        return
    try:
        exit_steps(lease_path, get_story(), say)
    except Exception as exc:  # noqa: BLE001
        say(f"immediate stop: exit steps failed: {exc}")
    try:
        rec.end(1, msg)
    except Exception as exc:  # noqa: BLE001
        print(f"auto-dev-driver: cannot write the end record: {exc}", file=sys.stderr)
    exit_process(1)


def watch(done: threading.Event, threshold_s: float, on_stuck, note, clock=time.monotonic, stop_now_file=None,
          on_stop_now=None) -> None:
    poll = min(STOP_NOW_POLL_SECONDS, WAIT_POLL_SECONDS) if stop_now_file else WAIT_POLL_SECONDS
    polled = judged = clock()
    while not done.wait(poll):
        now = clock()
        gap, polled = now - polled, now
        if stop_now_file and stop_now_file.is_file():
            on_stop_now()
            return
        if gap > SUSPEND_GAP_POLLS * WAIT_POLL_SECONDS:
            mark_event(now)
            judged = now
            _say(note)(f"watchdog: {gap:.0f} s between polls: the host slept; the silence clock restarts")
            continue
        if now - judged >= WAIT_POLL_SECONDS:
            judged = now
            if watchdog_tick(threshold_s, on_stuck, lambda: now):
                return


# ------------------------------------------------------------------ the run

def add_run_options(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--story", help="story id already in flight to resume")
    ap.add_argument("--max-stories", type=int, help="stop after this many stories landed or halted")
    ap.add_argument("--max-cost-usd", type=float, help="do not start a session once this much is spent")
    ap.add_argument("--session-timeout-min", type=float, default=float(cfg("loop.session_timeout_min", 300)))
    ap.add_argument("--land-stall-min", type=float, default=float(cfg("loop.land_stall_min", DEFAULT_STALL_MIN)),
                    help="stop waiting in the land lease's line once the holder has held it this long")
    ap.add_argument("--orchestrator-model", default=str(cfg("loop.orchestrator_model", "opus")))
    ap.add_argument("--orchestrator-effort", default=str(cfg("loop.orchestrator_effort", "medium")),
                    choices=("low", "medium", "high", "xhigh", "max"))
    ap.add_argument("--state-dir", help="run directories root (tests); default .workflow/state/auto-dev")


def run_options(args: argparse.Namespace) -> dict:
    return {"story": args.story, "max_stories": args.max_stories, "max_cost_usd": args.max_cost_usd,
            "session_timeout_min": args.session_timeout_min, "land_stall_min": args.land_stall_min,
            "orchestrator_model": args.orchestrator_model, "orchestrator_effort": args.orchestrator_effort}


def set_state_dir(state_dir: str | None) -> None:
    global STATE
    if state_dir:
        STATE = Path(state_dir).resolve()


def startup_checks() -> tuple[Path | None, str, Path | None] | str:
    """(Git Bash or None, the CLI path, the lease path or None), or the message of the first refusal."""
    tool = driver_tool()
    if tool == "claude":
        cli = shutil.which("claude")
        if not cli:
            return "auto-dev-driver: cannot start: no claude on PATH"
        bash = None
        if os.name == "nt":
            bash = git_bash_path(dict(os.environ))
            if not bash:
                return ("auto-dev-driver: cannot start: no Git Bash found (set CLAUDE_CODE_GIT_BASH_PATH to Git's "
                        "bin\\bash.exe)")
    elif tool == "opencode":
        argv = shlex.split(str(cfg("loop.opencode_cmd", "opencode run {prompt}")))
        cli = shutil.which(argv[0]) if argv else None
        if not cli:
            return f"auto-dev-driver: cannot start: {argv[0] if argv else 'loop.opencode_cmd'} not on PATH"
        bash = None
    else:
        return f"auto-dev-driver: cannot start: config loop.driver_tool {tool!r} is not claude or opencode"
    lease_path = None
    if land_lease is not None:
        try:
            lease_path = land_lease.default_path()
        except Exception as exc:  # noqa: BLE001
            return f"auto-dev-driver: cannot start: {exc}"
    return bash, cli, lease_path


def session_command(cli: str, prompt: str, name: str, args: argparse.Namespace) -> list[str]:
    if driver_tool() == "claude":
        return [cli, "-p", prompt, "--model", args.orchestrator_model, "--effort", args.orchestrator_effort,
                "--permission-mode", str(cfg("loop.driver_permission_mode", "auto")), "--output-format",
                "stream-json", "--verbose", "--name", name]
    argv = shlex.split(str(cfg("loop.opencode_cmd", "opencode run {prompt}")))
    return [a.replace("{prompt}", prompt).replace("{model}", args.orchestrator_model) for a in argv]


def allocate_run_dir(state: Path) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    for _ in range(ID_ATTEMPTS):
        run_dir = state / utc_now().strftime(RUN_ID_FORMAT)
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            time.sleep(ID_WAIT_SECONDS)
    raise OSError(f"no free run id under {state} after {ID_ATTEMPTS} tries")


def adopt_run_dir(state: Path, run_id: str) -> Path:
    if not RUN_ID_RE.match(run_id):
        raise ValueError(f"--run-id {run_id!r} is not a run id (YYYYMMDD-HHMMSS)")
    run_dir = state / run_id
    if not run_dir.is_dir():
        raise ValueError(f"--run-id {run_id}: {run_dir} does not exist")
    if (run_dir / RUN_RECORD).exists():
        raise ValueError(f"--run-id {run_id}: {run_dir} already holds {RUN_RECORD}")
    return run_dir


def run_driver(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_run_options(ap)
    ap.add_argument("--run-id", help="adopt this existing empty run directory (set by `start`)")
    args = ap.parse_args(argv)
    set_state_dir(args.state_dir)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    checks = startup_checks()
    if isinstance(checks, str):
        print(checks, file=sys.stderr)
        return 2
    bash, cli, lease_path = checks
    try:
        run_dir = adopt_run_dir(STATE, args.run_id) if args.run_id else allocate_run_dir(STATE)
    except (ValueError, OSError) as exc:
        print(f"auto-dev-driver: cannot start: {exc}", file=sys.stderr)
        return 2
    stop_file = run_dir / RUN_STOP
    stop_now_file = run_dir / RUN_STOP_NOW
    tool = driver_tool()

    global STUCK_NOTE
    STUCK.clear()
    IMMEDIATE_STOP.clear()
    if merge_train is not None and hasattr(merge_train, "GATE_ABORT"):
        merge_train.GATE_ABORT.clear()
    STUCK_NOTE = ""
    mark_event()
    exit_state = {"code": 1, "reason": "the driver ended without a result", "last_note": ""}

    def note(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        exit_state["last_note"] = msg
        mark_event()
        print(line, flush=True)
        with open(run_dir / "driver.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def stop(code: int) -> None:
        if code == 0 and STUCK.is_set():
            note(f"{end_cause()}: the run was ended before it stopped as asked; exit 1")
            code = 1
        exit_state["code"], exit_state["reason"] = code, exit_state["last_note"]

    rec = RunRecord(run_dir, new_run_data(run_dir, os.getpid(), None, args.run_id is not None, run_options(args)),
                    log=note)
    story, done, spent, n = args.story, 0, 0.0, 0
    previous_files: dict[str, str] | None = None
    watchdog_done = threading.Event()
    main_exited = threading.Event()
    refused = False
    try:
        rec.data["create_time"] = own_create_time()
        try:
            rec.write()
        except FileExistsError:
            refused = True
            print(f"auto-dev-driver: cannot start: {run_dir / RUN_RECORD} was published first; nothing was run",
                  file=sys.stderr)
            return 2
        threading.Thread(
            target=watch, daemon=True,
            args=(watchdog_done, stuck_threshold_s(args.session_timeout_min),
                  lambda silence: declare_stuck(silence, args.session_timeout_min, rec, note, lease_path,
                                                lambda: story), note),
            kwargs={"stop_now_file": stop_now_file,
                    "on_stop_now": lambda: declare_immediate_stop(stop_now_file, rec, note, lease_path,
                                                                  lambda: story, main_exited)}).start()
        note(f"run {run_dir.name}; tool {tool}; story {story or 'none (first session picks)'}")
        while True:
            if STUCK.is_set():
                note(f"{end_cause()}: no session started; stopping")
                stop(1)
                break
            if stop_file.is_file():
                note(f"stop requested ({stop_file}); stopping")
                stop(0)
                break
            if args.max_cost_usd is not None and spent >= args.max_cost_usd:
                note(f"spent ${spent:.2f} of the ${args.max_cost_usd:.2f} cap; stopping")
                stop(0)
                break
            n += 1
            before = ledger_fingerprint(unless_stuck(ledger, story)) if story else None
            failures_before = failure_entries(story) if story else 0
            snap = unless_stuck(auto_dev_audit.snapshot)
            knobs = {"agents": agent_knobs(), "driver": {**run_options(args), "story": None, "tool": tool}}
            harness = harness_id()
            files = harness_files()
            uncommitted = unless_stuck(uncommitted_harness, files)
            changed_before = harness_changes(previous_files, files) if previous_files is not None else []
            head = unless_stuck(main_head)
            prompt = f"/auto-dev-loop driver story={story or 'none'}"
            name = f"{story}-{run_dir.name}-s{n}" if story else f"{run_dir.name}-s{n}"
            if changed_before:
                note(f"session {n}: harness changed since session {n - 1} ended: "
                     f"{describe_changes(changed_before, uncommitted)}")
            rec.activity("session", n, story)
            note(f"session {n}: {prompt}")
            started = time.monotonic()
            res = run_session(session_command(cli, prompt, name, args), session_env(bash, run_dir.name),
                              run_dir / f"session-{n}.jsonl", args.session_timeout_min * 60, tool)
            result = res["result"] or {}
            spent += float(result.get("total_cost_usd") or 0)
            mins = (time.monotonic() - started) / 60
            failed = bool(res["problem"] or not result or result.get("is_error"))
            phase = phase_line(str(result.get("result"))) if result else None
            previous_files = harness_files()
            changed_during = harness_changes(files, previous_files)
            if changed_during:
                note(f"session {n}: harness changed while it ran: "
                     f"{describe_changes(changed_during, unless_stuck(uncommitted_harness, previous_files))}")
            record = session_record(n, knobs, harness, head, res["init"], result, phase, mins, drift={
                "harness_uncommitted": uncommitted, "harness_changed_before": changed_before,
                "harness_changed_during": changed_during})
            with open(run_dir / "sessions.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            note(f"session {n} config: knobs {record['knobs_id']}, harness {harness}, head {head}")
            drove = story or (phase["story"] if phase and phase["story"] != "none" else None)
            released = False
            if drove and not (phase and phase["outcome"] == "landed" and not failed):
                try:
                    released = release_lease(lease_path, drove, note)
                except Exception as exc:  # noqa: BLE001
                    note(f"land lease: {exc}; stopping")
                    stop(1)
                    break
            if failed:
                note(f"session {n} failed after {mins:.0f} min: {res['problem'] or str(result.get('result'))[:300]};"
                     f" stopping (spent ${spent:.2f})")
                stop(1)
                break
            if not phase:
                note(f"session {n} ended without an AUTO-DEV-PHASE line; stopping (spent ${spent:.2f})")
                stop(1)
                break
            sid, outcome = phase["story"], phase["outcome"]
            note(f"session {n} done in {mins:.0f} min, ${float(result.get('total_cost_usd') or 0):.2f}:"
                 f" story {sid} {outcome} (step {phase['step'] or '?'}); total ${spent:.2f}")
            if outcome == "terminated":
                note("no startable story left; stopping")
                stop(0)
                break
            if outcome == "infra-stop":
                note("the session stopped on an infrastructure problem; see its stream; stopping")
                stop(1)
                break
            if sid == "none" or (story and not same_story(sid, story)):
                note(f"session {n} reported story {sid} while driving {story or 'none'}; stopping")
                stop(1)
                break
            if outcome in ("advanced", "halted", "waiting"):
                facts = unless_stuck(auto_dev_audit.facts, REPO, sid, outcome)
                found = auto_dev_audit.problems(sid, outcome, snap, facts)
                if found:
                    note(f"session {n} audit of {sid} ({outcome}) failed: {'; '.join(found)}; stopping")
                    stop(1)
                    break
            if outcome == "waiting":
                if released:
                    note(f"session {n} ended waiting although {sid} held the land lease; released it; stopping")
                    stop(1)
                    break
                story = sid
                rec.activity("waiting-in-line", n, sid)
                code = wait_in_line(sid, lease_path, stop_file, args.land_stall_min, note,
                                    gate=lambda: train_gate(rec, n, sid))
                if code is not None:
                    stop(code)
                    break
                continue
            if not progressed(before, failures_before, ledger_fingerprint(unless_stuck(ledger, sid)),
                              failure_entries(sid)):
                note(f"session {n} reported {outcome} but the record shows no new fact for {sid}; stopping")
                stop(1)
                break
            if outcome in ("landed", "halted"):
                done += 1
                story = None
                if args.max_stories is not None and done >= args.max_stories:
                    note(f"{done} stories finished; stopping")
                    stop(0)
                    break
            else:
                story = sid
    except RunStuck as exc:
        note(str(exc))
        stop(1)
    except KeyboardInterrupt:
        exit_state["code"], exit_state["reason"] = EXIT_INTERRUPTED, "interrupted (Ctrl-C)"
        raise
    except Exception as exc:
        exit_state["code"], exit_state["reason"] = 1, f"{type(exc).__name__}: {exc}"
        raise
    finally:
        watchdog_done.set()
        if not refused:
            rec.activity("ending", n, story)
            try:
                exit_steps(lease_path, story, note)
            finally:
                try:
                    rec.end(*((1, STUCK_NOTE) if STUCK.is_set() else (exit_state["code"], exit_state["reason"])))
                finally:
                    main_exited.set()
    return rec.data["ended"]["exit_code"]


# ------------------------------------------------------------------ start / list / stop

CONSOLE_LOG = "console.log"
CONSOLE_TAIL_CHARS = 300
START_POLL_SECONDS = 0.05
LIVE_STATES = ("running", "stopping", "stuck")
RUN_OPTION_FLAGS = (("story", "--story"), ("max_stories", "--max-stories"), ("max_cost_usd", "--max-cost-usd"),
                    ("session_timeout_min", "--session-timeout-min"), ("land_stall_min", "--land-stall-min"),
                    ("orchestrator_model", "--orchestrator-model"), ("orchestrator_effort", "--orchestrator-effort"))


def emit(doc: dict) -> None:
    print(json.dumps(doc, indent=2))


def spawn_detached(argv: list[str], run_dir: Path) -> subprocess.Popen:
    """Start `argv` detached from this process's console and process group, output to console.log."""
    console = open(run_dir / CONSOLE_LOG, "ab")
    try:
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            return subprocess.Popen(argv, cwd=REPO, stdin=subprocess.DEVNULL, stdout=console, stderr=console,
                                    creationflags=flags)
        return subprocess.Popen(argv, cwd=REPO, stdin=subprocess.DEVNULL, stdout=console, stderr=console,
                                start_new_session=True)
    finally:
        console.close()


def console_tail(run_dir: Path) -> str:
    try:
        return (run_dir / CONSOLE_LOG).read_bytes().decode("utf-8", errors="replace")[-CONSOLE_TAIL_CHARS:]
    except OSError:
        return ""


class CommandParser(argparse.ArgumentParser):
    """With `--json`, a usage error is a JSON error object on stdout (exit 2)."""

    json_requested = False

    def parse_args(self, args=None, namespace=None):
        self.json_requested = "--json" in (sys.argv[1:] if args is None else args)
        return super().parse_args(args, namespace)

    def error(self, message):
        if self.json_requested:
            emit({"status": "error", "source": str(STATE), "error": message, "exit_code": 2})
            self.exit(2)
        super().error(message)


def record_started(record: dict) -> bool:
    if record["status"] != "ok":
        return False
    if record.get("ended"):
        return record.get("create_time") is not None or psutil is None
    return process_alive(record["pid"], record.get("create_time"))


def record_failed_start(run_dir: Path, options: dict, pid: int, exit_code: int, reason: str) -> None:
    data = new_run_data(run_dir, pid, None, True, options)
    data["ended"] = {"utc": iso(utc_now()), "exit_code": exit_code, "reason": reason}
    path = run_dir / RUN_RECORD
    if path.exists():
        raise FileExistsError(str(path))
    temp = path.with_name(RUN_RECORD + ".start-failed")
    try:
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.rename(temp, path)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


def start_command(argv: list[str]) -> int:
    ap = CommandParser(prog="auto_dev_driver.py start", description="Start a detached auto-dev run.")
    add_run_options(ap)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    set_state_dir(args.state_dir)

    def fail(message: str, code: int, source) -> int:
        if args.json:
            emit({"status": "error", "source": str(source), "error": message, "exit_code": code})
        else:
            print(message, file=sys.stderr)
        return code

    checks = startup_checks()
    if isinstance(checks, str):
        return fail(checks, 2, STATE)
    try:
        run_dir = allocate_run_dir(STATE)
    except OSError as exc:
        return fail(f"auto-dev-driver: cannot start: {exc}", 2, STATE)
    options = run_options(args)
    child = [sys.executable, str(Path(__file__).resolve())]
    for key, flag in RUN_OPTION_FLAGS:
        if options[key] is not None:
            child += [flag, str(options[key])]
    if args.state_dir:
        child += ["--state-dir", str(STATE)]
    child += ["--run-id", run_dir.name]

    def started(record: dict) -> int:
        message = f"run {run_dir.name} pid {record['pid']} started; log {run_dir / 'driver.log'}"
        if record.get("ended"):
            ended = record["ended"]
            message += f"; already ended (exit {ended.get('exit_code')}: {ended.get('reason')})"
        if args.json:
            emit({"status": "ok", "source": str(run_dir), "run_id": run_dir.name, "pid": record["pid"],
                  "run_dir": str(run_dir)})
        else:
            print(message)
        return 0

    def close(pid: int, code: int, message: str) -> int:
        try:
            record_failed_start(run_dir, options, pid, code, message)
        except FileExistsError:
            record = read_run_record(run_dir)
            if record_started(record):
                return started(record)
        except OSError as exc:
            return fail(f"{message}; the failed-start record could not be written ({exc})", code, run_dir)
        return fail(message, code, run_dir)

    try:
        proc = spawn_detached(child, run_dir)
    except OSError as exc:
        return close(os.getpid(), 2, f"auto-dev-driver: start: cannot spawn the run process: {exc}")
    deadline, pause = time.monotonic() + START_BOUND_SECONDS, threading.Event()
    while True:
        exit_code = proc.poll()
        record = read_run_record(run_dir)
        if record_started(record):
            return started(record)
        if exit_code is not None:
            return close(proc.pid, 2, (
                f"auto-dev-driver: start: the run process exited with exit code {exit_code} before its record "
                f"showed a live run; {run_dir / CONSOLE_LOG} ends: {console_tail(run_dir)!r}"))
        if time.monotonic() >= deadline:
            return close(proc.pid, 1, (f"auto-dev-driver: start: no run record from process {proc.pid} within "
                                       f"{START_BOUND_SECONDS:g} s (see {run_dir / CONSOLE_LOG})"))
        pause.wait(START_POLL_SECONDS)


def newest_event_mtime(run_dir: Path) -> float:
    paths = [run_dir / "driver.log", *run_dir.glob("session-*.jsonl")]
    times = [p.stat().st_mtime for p in paths if p.is_file()]
    return max(times) if times else (run_dir / RUN_RECORD).stat().st_mtime


def run_state(run_dir: Path, record: dict) -> dict:
    if record["status"] != "ok":
        return {"name": "unknown", "reason": record.get("reason") or record.get("error")}
    ended = record.get("ended")
    if ended:
        return {"name": "ended", "reason": f"exit {ended.get('exit_code')}: {ended.get('reason')}"}
    pid = record["pid"]
    if not process_alive(pid, record.get("create_time")):
        return {"name": "dead", "reason": f"pid {pid} is gone and the run wrote no end record"}
    if (run_dir / RUN_STOP_NOW).exists():
        stopping = {"name": "stopping", "reason": "STOP-NOW requested (immediate stop)"}
    elif (run_dir / RUN_STOP).exists():
        stopping = {"name": "stopping", "reason": "STOP requested; the run stops when its current phase session "
                                                  "ends, or at its next land-line poll"}
    else:
        stopping = None
    current = record.get("current") or {}
    if current.get("activity") == "train-gate":
        return stopping or {"name": "running", "reason": f"in the train gate; pid {pid} alive"}
    timeout_min = (record.get("options") or {}).get("session_timeout_min")
    threshold = stuck_threshold_s(300 if timeout_min is None else timeout_min)
    try:
        age = time.time() - newest_event_mtime(run_dir)
    except OSError as exc:
        return {"name": "unknown", "reason": f"the run's newest event time cannot be read: {exc}"}
    if age > threshold:
        return {"name": "stuck", "reason": f"no new event for {age / 60:.0f} min (threshold {threshold / 60:.1f} min); "
                                           f"pid {pid}"}
    return stopping or {"name": "running", "reason": f"pid {pid} alive; last event {age:.0f} s ago"}


def read_sessions(run_dir: Path) -> list[dict]:
    path = run_dir / "sessions.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def run_row(run_dir: Path) -> dict:
    record = read_run_record(run_dir)
    state = run_state(run_dir, record)
    sessions = read_sessions(run_dir)
    spent = round(sum(float(s.get("cost_usd") or 0) for s in sessions), 2)
    cap = (record.get("options") or {}).get("max_cost_usd") if record["status"] == "ok" else "unknown"
    current = record.get("current") or {} if record["status"] == "ok" else {}
    live = state["name"] in LIVE_STATES
    story = (current.get("story") or ("picking" if current.get("activity") == "session" else "-")) if live else "-"
    last = sessions[-1] if sessions else {}
    last_text = f"last: step {last.get('step') or '?'} {last.get('outcome') or '?'}" if sessions else None
    activity = current.get("activity")
    phase = "; ".join(filter(None, [str(activity) if live and activity else None, last_text])) or "-"
    return {"run_id": run_dir.name, "run_dir": str(run_dir), "record": record,
            "pid": record.get("pid") if record["status"] == "ok" else None, "story": story, "phase": phase,
            "spent_usd": spent, "cap_usd": "no cap" if cap is None else cap, "state": state}


def list_runs() -> dict:
    if not STATE.is_dir():
        return {"status": "none", "source": str(STATE), "reason": "no auto-dev runs yet", "runs": []}
    rows = []
    for d in sorted(p for p in STATE.iterdir() if p.is_dir() and RUN_ID_RE.match(p.name)):
        try:
            rows.append(run_row(d))
        except Exception as exc:  # noqa: BLE001 -- one unreadable run must not hide the others
            rows.append({"run_id": d.name, "run_dir": str(d), "pid": None, "story": "-", "phase": "-",
                         "spent_usd": None, "cap_usd": "unknown",
                         "state": {"name": "unknown", "reason": f"{type(exc).__name__}: {exc}"}})
    return {"status": "ok", "source": str(STATE), "runs": rows}


def run_line(row: dict) -> str:
    spent = f"${row['spent_usd']:.2f}" if isinstance(row.get("spent_usd"), (int, float)) else "$?"
    cap = row["cap_usd"]
    return (f"{row['run_id']}  pid {row['pid'] if row['pid'] is not None else '-'}  {row['state']['name']}  "
            f"story {row['story']}  phase {row['phase']}  {spent} of "
            f"{f'${cap:.2f}' if isinstance(cap, (int, float)) else cap}")


def list_command(argv: list[str]) -> int:
    ap = CommandParser(prog="auto_dev_driver.py list", description="List every auto-dev run.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--state-dir")
    args = ap.parse_args(argv)
    set_state_dir(args.state_dir)
    doc = list_runs()
    if args.json:
        emit(doc)
    elif doc["runs"]:
        for row in doc["runs"]:
            print(run_line(row))
    else:
        print(doc.get("reason") or "no runs")
    return 0


def stop_command(argv: list[str]) -> int:
    ap = CommandParser(prog="auto_dev_driver.py stop",
                       description="Ask one run to stop after its current phase, or with --now at once.")
    ap.add_argument("run_id")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--state-dir")
    args = ap.parse_args(argv)
    set_state_dir(args.state_dir)
    run_dir = STATE / args.run_id

    def answer(code: int, state: str | None, requested: bool, message: str) -> int:
        if args.json:
            emit({"status": "ok" if code == 0 else "error", "source": str(run_dir), "run_id": args.run_id,
                  "state": state, "requested": requested, "message": message})
        else:
            print(message, file=None if code == 0 else sys.stderr)
        return code

    if not RUN_ID_RE.match(args.run_id):
        return answer(2, None, False, f"{args.run_id!r} is not a run id (YYYYMMDD-HHMMSS)")
    if not run_dir.is_dir():
        return answer(2, None, False, f"no run directory {run_dir}")
    record = read_run_record(run_dir)
    state = run_state(run_dir, record)
    name = state["name"]
    if name in ("ended", "dead", "unknown"):
        return answer(1, name, False, f"run {args.run_id} is {name}: {state['reason']}; nothing written")
    if args.now:
        if (run_dir / RUN_STOP_NOW).exists():
            return answer(0, name, False, f"run {args.run_id}: immediate stop already requested")
        (run_dir / RUN_STOP_NOW).touch()
        return answer(0, name, True, f"immediate stop requested: run {args.run_id}'s watchdog kills its own process "
                                     f"tree within {STOP_NOW_POLL_SECONDS:g} s and the run ends with exit 1")
    if name == "stopping":
        return answer(0, name, False, f"run {args.run_id}: stop already requested")
    (run_dir / RUN_STOP).touch()
    return answer(0, name, True, f"stop requested: run {args.run_id} stops when its current phase session ends, "
                                 "or at its next land-line poll")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    commands = {"start": start_command, "list": list_command, "stop": stop_command}
    if argv and argv[0] in commands:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return commands[argv[0]](argv[1:])
    return run_driver(argv)


if __name__ == "__main__":
    sys.exit(main())
