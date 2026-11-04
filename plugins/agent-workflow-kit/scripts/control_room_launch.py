#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The control room's two side effects on the machine: opening a terminal window on a session and starting a
story session with the configured agent tool.

Purpose
-------
`control_room.py` hands a validated request to this module; it builds the command, writes the launch record and runs
the command. Both effects go through module-level seams (`run_starter`, `spawn_window`, `process_created`,
`process_alive`, `which`), so the tests replace them and no test starts an agent, a terminal or tmux.

Window backend (`config: control_room.launcher`):
  * `windows-terminal` -- `wt -w new new-tab -d <dir> powershell -NoLogo -NoProfile -EncodedCommand <script>`; the
    script drops `WT_SESSION` and the agent session variables, sets `CLAUDE_CODE_GIT_BASH_PATH` for Claude Code and
    runs the session command. Windows only.
  * `tmux` -- `tmux new-window -d -c <dir> -n <name> <command>` when the control room runs inside tmux (`$TMUX`),
    else `tmux new-session -d -s <name> -c <dir> <command>`; the result names the `tmux attach -t <name>` command.
  * `background` -- no terminal: an attach or resume needs one, so it is refused with the command to run by hand.
  * `auto` (default) -- `windows-terminal` on Windows when `wt` is on PATH, else `tmux` when it is on PATH, else
    `background`.

Session tool (`config: loop.driver_tool`):
  * `claude` -- attach `claude attach <id>`, resume `claude --resume <uuid>`, start
    `claude --bg --name <story> [--settings <file>] <prompt>` (detached by the CLI itself; `--settings` carries
    `enabledMcpjsonServers` when the project has a `.mcp.json`).
  * `opencode` -- resume `opencode --session <id>`; attach is refused (no background-session attach); start runs
    `config: control_room.opencode_start_cmd` (default `opencode run {prompt}`) in a window of the backend, or as a
    detached process with log files under the state dir with `background`. Untested against a live OpenCode.
The start prompt is `config: control_room.start_prompt` (`{story}` filled; default `/parallel-dev-wave {story}`).

Launch record `.workflow/state/control-room/launches.jsonl` -- one JSON line per event (`open`, `launch-override`,
`launch-attempt`, `launch-started`, `launch-result`, `launch-timeout`, `launch-exit`), flushed and fsynced before
the next step; a `launch-attempt` line is on disk before the CLI runs and a `launch-started` line (pid and creation
time) right after its process starts, written again once the starter returns when the first write failed. A launch
that started a process and has no `launch-result` has not ended while that process runs, whichever app instance
started it; the app kills nothing. A `launch-override` line precedes the attempt of a start the owner forced past a
launch-record refusal.

Process probing: Windows through kernel32 (`_probe_windows`), elsewhere psutil when installed, else `/proc/<pid>/stat`
and `os.kill(pid, 0)` (`_probe_posix`).

Usage
-----
Imported by `control_room.py`; not run on its own.
"""
from __future__ import annotations

import base64
import contextlib
import functools
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_dev_driver  # noqa: E402
import auto_dev_guard  # noqa: E402
import story_state  # noqa: E402
import wfconfig  # noqa: E402

LAUNCH_TIMEOUT_SECONDS = 120
BACKGROUNDED = re.compile(r"backgrounded\s+\S+\s+([0-9a-f]{8})\s+\S+")
RECORD_NAME = "launches.jsonl"
SETTINGS_NAME = "launch-settings.json"
WORKING_STATES = ("working", "blocked")
BACKENDS = ("windows-terminal", "tmux", "background")
TMUX_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_INVALID_PARAMETER = 87
FILETIME_UNIX_EPOCH = 116444736000000000  # 100 ns ticks from 1601-01-01 to 1970-01-01

which = shutil.which


def tool() -> str:
    return str(wfconfig.get("loop.driver_tool", "claude") or "claude")


def backend() -> str:
    """The window backend: `config: control_room.launcher`, `auto` resolved by platform and PATH."""
    chosen = str(wfconfig.get("control_room.launcher", "auto") or "auto")
    if chosen in BACKENDS:
        return chosen
    if os.name == "nt" and which("wt"):
        return "windows-terminal"
    if which("tmux"):
        return "tmux"
    return "background"


def start_prompt(story_id: str) -> str:
    template = str(wfconfig.get("control_room.start_prompt", "/parallel-dev-wave {story}") or "")
    return template.replace("{story}", story_id)


# ---------------------------------------------------------------- seams

def run_starter(argv, *, cwd, env, timeout, out_prefix, on_start=None):
    """(returncode, stdout, stderr) of a command whose output goes to `<out_prefix>.out` / `.err`; `on_start(proc)`
    runs right after the process starts. A timeout is returncode None and leaves the child running: a CLI that
    hands its session to a supervisor could orphan the session if killed mid-hand-off. A failure before the process
    starts is returncode None with the error as stderr and no `on_start` call. An exception from `on_start` does not
    end the call: the process is running, so the wait goes on and the exception leads the stderr returned."""
    prefix = Path(out_prefix)
    out_path, err_path = Path(f"{prefix}.out"), Path(f"{prefix}.err")
    timed_out = False
    hook_error = ""
    with contextlib.ExitStack() as files:
        try:
            prefix.parent.mkdir(parents=True, exist_ok=True)
            out_file = files.enter_context(open(out_path, "w", encoding="utf-8"))
            err_file = files.enter_context(open(err_path, "w", encoding="utf-8"))
            proc = subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out_file, stderr=err_file)
        except OSError as exc:
            return None, "", f"{type(exc).__name__}: {exc}"
        if on_start is not None:
            try:
                on_start(proc)
            except Exception as exc:  # noqa: BLE001 -- the process runs on whatever the hook did
                hook_error = f"on_start raised {type(exc).__name__}: {exc}\n"
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            returncode, timed_out = None, True
    out = out_path.read_text(encoding="utf-8", errors="replace")
    err = err_path.read_text(encoding="utf-8", errors="replace")
    if timed_out:
        err = f"timed out after {timeout} s; the command was left running\n{err}"
    return returncode, out, hook_error + err


def spawn_window(argv, *, cwd=None, env=None, log_prefix=None):
    """Starts `argv` detached from the control room and does not wait for it. With `log_prefix` its output goes
    to `<log_prefix>.out` / `.err` (the `background` backend), else nowhere."""
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kw["start_new_session"] = True
    if log_prefix is None:
        subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        return None
    prefix = Path(log_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{prefix}.out", "w", encoding="utf-8") as out, open(f"{prefix}.err", "w", encoding="utf-8") as err:
        return subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, **kw)


@functools.cache
def _kernel32():
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _probe_windows(pid: int):
    """None when no process has the id, else (running, creation time in epoch seconds or None). Not
    `os.kill(pid, 0)`: signal 0 is `CTRL_C_EVENT` on Windows."""
    import ctypes
    from ctypes import wintypes
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else (True, None)
    try:
        code = wintypes.DWORD()
        running = not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == STILL_ACTIVE
        times = [wintypes.FILETIME() for _ in range(4)]
        created = None
        if kernel32.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            created = (ticks - FILETIME_UNIX_EPOCH) / 1e7
        return running, created
    finally:
        kernel32.CloseHandle(handle)


def _proc_created(pid: int) -> float | None:
    """Creation time from /proc/<pid>/stat (Linux): boot time + start ticks; None when unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _probe_posix(pid: int):
    """None when no process has the id, else (running, creation time or None): psutil when installed, else
    `os.kill(pid, 0)` plus `/proc`. A zombie counts as gone."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return None
            return True, proc.create_time()
        except psutil.NoSuchProcess:
            return None
        except psutil.Error:
            return True, None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return True, None
    except OSError:
        return True, None
    return True, _proc_created(pid)


def _probe(pid):
    if not isinstance(pid, int) or pid <= 0:
        return None
    return _probe_windows(pid) if os.name == "nt" else _probe_posix(pid)


def process_created(pid: int):
    """The creation time (epoch seconds) of the process with id `pid`, or None when it cannot be read."""
    state = _probe(pid)
    return None if state is None else state[1]


def process_alive(pid: int, created) -> bool:
    """False when the process is gone: no process has the id, it has exited, or the id belongs to a process created
    at another time than `created` (the id was reused; a 1 s tolerance covers clock-tick rounding). A process whose
    creation time cannot be read counts as alive, so a launch is refused rather than let through."""
    state = _probe(pid)
    if state is None:
        return False
    running, started = state
    return running and (created is None or started is None or abs(started - created) < 1.0)


# ---------------------------------------------------------------- the launch record

def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def parse_iso(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def record_path(state_dir: Path) -> Path:
    return Path(state_dir) / RECORD_NAME


def append_record(state_dir: Path, line: dict) -> None:
    path = record_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_records(state_dir: Path) -> list[dict]:
    path = record_path(state_dir)
    if not path.is_file():
        return []
    out = []
    for text in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def refused(message: str, **extra) -> dict:
    return {"status": "refused", "message": message, "session_id": None, **extra}


def tail(text: str) -> str:
    return text.strip()[-story_state.MAX_ERR:]


def session_env() -> dict:
    """The environment of a launched session: the launching agent session's variables and the auto-dev marker
    removed, so the new session is neither a child nor an auto-dev session."""
    removable = {*auto_dev_driver.SESSION_VARS, auto_dev_guard.MARKER}
    return {k: v for k, v in os.environ.items() if k not in removable}


# ---------------------------------------------------------------- open a terminal window

def window_command(kind: str, ident: str) -> tuple[list[str] | None, str]:
    """(argv of the session command, refusal text) for an attach or a resume with the configured tool."""
    if tool() == "claude":
        return (["claude", "attach", ident] if kind == "attach" else ["claude", "--resume", ident]), ""
    if tool() == "opencode":
        if kind == "attach":
            return None, "OpenCode has no background-session attach; resume the session by id instead."
        return ["opencode", "--session", ident], ""
    return None, f"config loop.driver_tool {tool()!r} is not claude or opencode."


def ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def window_script(command: str, bash: Path | None) -> str:
    removed = ", ".join(f"Env:{name}" for name in ("WT_SESSION", *auto_dev_driver.SESSION_VARS))
    lines = [f"Remove-Item {removed} -ErrorAction SilentlyContinue"]
    if bash is not None:
        lines.append(f"$env:CLAUDE_CODE_GIT_BASH_PATH = {ps_quote(str(bash))}")
    lines += [command,
              # `$?` is false when the command cannot be run, which leaves `$LASTEXITCODE` unset
              'if (-not $? -or $LASTEXITCODE) '
              '{ Read-Host "the session command failed (exit code $LASTEXITCODE) - press Enter to close" }']
    return "\n".join(lines)


def windows_terminal_argv(directory: str, command: list[str]) -> tuple[list[str] | None, str, str]:
    """(argv, script, refusal) for a Windows Terminal tab running `command` in `directory`."""
    wt = which("wt")
    if wt is None:
        return None, "", "Windows Terminal (`wt`) was not found on PATH."
    bash = None
    if tool() == "claude":
        bash = auto_dev_driver.git_bash_path(dict(os.environ))
        if bash is None:
            return None, "", "Git Bash was not found; Claude Code needs it on Windows."
    # `wt` parses `;` as its own command separator.
    if ";" in directory:
        return None, "", f"The directory {directory} holds a semicolon, which Windows Terminal reads as a separator."
    script = window_script(" ".join(command), bash)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [str(wt), "-w", "new", "new-tab", "-d", directory, "powershell", "-NoLogo", "-NoProfile",
            "-EncodedCommand", encoded], script, ""


def tmux_argv(directory: str, command: list[str], name: str) -> tuple[list[str] | None, str, str]:
    """(argv, attach hint, refusal) for a tmux window running `command` in `directory`."""
    tmux = which("tmux")
    if tmux is None:
        return None, "", "tmux was not found on PATH."
    safe = TMUX_NAME_RE.sub("-", name)[:60] or "session"
    unset = " ".join(f"-u {v}" for v in (*auto_dev_driver.SESSION_VARS, auto_dev_guard.MARKER))
    shell = f"env {unset} {shlex.join(command)}; echo; echo '[session ended: press Enter]'; read _"
    if os.environ.get("TMUX"):
        return [tmux, "new-window", "-d", "-c", directory, "-n", safe, shell], f"tmux window {safe}", ""
    return ([tmux, "new-session", "-d", "-s", safe, "-c", directory, shell],
            f"tmux session {safe} (attach: tmux attach -t {safe})", "")


def open_window(state_dir: Path, clock, *, kind: str, ident: str, directory: str) -> dict:
    """Opens a window on the session `ident` (kind `attach` or `resume`) in `directory` through the configured
    backend. `ident` and `directory` are validated by the caller; the directory is re-checked here."""
    if not directory:
        return refused("The session has no directory to open in.")
    if not Path(directory).is_absolute() or not Path(directory).is_dir():
        return refused(f"The session's directory {directory} is not an absolute path to an existing directory.")
    command, why = window_command(kind, ident)
    if command is None:
        return refused(why)
    chosen = backend()
    if chosen == "background":
        return refused("No terminal backend (config control_room.launcher resolves to `background`): "
                       f"run `{shlex.join(command)}` in {directory} yourself.")
    if chosen == "windows-terminal":
        argv, extra, why = windows_terminal_argv(directory, command)
        record = {"script": extra}
        where = "a terminal window"
    else:
        argv, extra, why = tmux_argv(directory, command, f"cr-{ident}")
        record = {}
        where = extra
    if argv is None:
        return refused(why)
    append_record(state_dir, {"event": "open", "ts": iso(clock()), "backend": chosen, "argv": argv,
                              "dir": directory, **record})
    try:
        spawn_window(argv, env=session_env() if chosen == "tmux" else None)
    except OSError as exc:
        return {"status": "error", "message": f"The window did not open: {type(exc).__name__}: {exc}",
                "session_id": None}
    return {"status": "ok", "message": f"Opened {where} on {ident}.", "session_id": None}


# ---------------------------------------------------------------- start a story session

def find_duplicate(entries, story_id: str, sprint_key):
    """The first listing entry that is alive (a pid, or working / blocked) and belongs to the story: linked by its
    id or its sprint-status key, or named by the story id."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        alive = entry.get("pid") is not None or entry.get("state") in WORKING_STATES
        mine = entry.get("story") in (story_id, sprint_key) or entry.get("name") == story_id
        if alive and mine:
            return entry
    return None


def story_launches(records, story_id: str, sprint_key=None) -> dict:
    """{launch_id: {"attempt", "started", "result", "exit"}} for every launch of the story, in record order: the
    `ts` of its attempt, result and exit lines (None when absent) and its `launch-started` line. An attempt line is
    the story's when its `story` or its `sprint_key` is the id or the sprint-status key given here."""
    names = tuple(name for name in (story_id, sprint_key) if name)
    launches: dict = {}
    for line in records:
        event, launch_id = line.get("event"), line.get("launch_id")
        if event == "launch-attempt" and (line.get("story") in names or line.get("sprint_key") in names):
            launches[launch_id] = {"attempt": parse_iso(line.get("ts")), "started": None, "result": None, "exit": None}
        elif launch_id in launches:
            launch = launches[launch_id]
            if event == "launch-started":
                launch["started"] = line
            elif event == "launch-result":
                launch["result"] = parse_iso(line.get("ts"))
            elif event == "launch-exit":
                launch["exit"] = parse_iso(line.get("ts"))
    return launches


def launch_end(launch: dict):
    """When the launch ended, or None while it has not: at its result line, else at its exit line. A launch that
    started a process and has neither is still tracked by that process; one that never started a process ended at
    its attempt."""
    if launch["result"] is not None:
        return launch["result"]
    if launch["exit"] is not None:
        return launch["exit"]
    return None if launch["started"] is not None else launch["attempt"]


def settle_exited_launches(state_dir: Path, clock, story_id: str, sprint_key=None) -> None:
    """Appends a `launch-exit` line, stamped now, for each launch of the story that started a process, has no result
    or exit line and whose process (that pid with that creation time) is gone."""
    for launch_id, launch in story_launches(read_records(state_dir), story_id, sprint_key).items():
        started = launch["started"]
        if started is None or launch["result"] is not None or launch["exit"] is not None:
            continue
        if not process_alive(started.get("pid"), started.get("created")):
            append_record(state_dir, {"event": "launch-exit", "launch_id": launch_id, "ts": iso(clock())})


def blocking_launches(state_dir: Path, story_id: str, since: float, sprint_key=None) -> list[dict]:
    """Every launch of the story that blocks a new start, in record order: a launch that has not ended, or that ended
    at or after `since` (the start of a listing refresh that may have been read before the launch's session
    registered). Each is {"launch_id", "ended" (None while it has not), "pid"}."""
    blockers = []
    for launch_id, launch in story_launches(read_records(state_dir), story_id, sprint_key).items():
        ended = launch_end(launch)
        if ended is None or ended >= since:
            started = launch["started"] or {}
            blockers.append({"launch_id": launch_id, "ended": ended, "pid": started.get("pid")})
    return blockers


def mcp_servers(main: Path):
    """(sorted server names of `<main>/.mcp.json` or None when the project has none, refusal message)."""
    path = Path(main) / ".mcp.json"
    if not path.exists():
        return None, ""
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers")
    except (OSError, ValueError, AttributeError) as exc:
        return None, f"{path} could not be read ({type(exc).__name__}: {exc}); the session would lose its MCP servers."
    if not isinstance(servers, dict):
        return None, f"{path} has no mcpServers object."
    return sorted(servers), ""


def start_argv(main: Path, state_dir: Path, story_id: str) -> tuple[list[str] | None, dict, dict, str]:
    """(argv, env additions, record extras, refusal) of a story-session start with the configured tool."""
    prompt = start_prompt(story_id)
    if tool() == "claude":
        servers, why = mcp_servers(main)
        if why:
            return None, {}, {}, why
        claude = which("claude")
        if claude is None:
            return None, {}, {}, "`claude` was not found on PATH."
        env_set: dict = {}
        if os.name == "nt":
            bash = auto_dev_driver.git_bash_path(dict(os.environ))
            if bash is None:
                return None, {}, {}, "Git Bash was not found; Claude Code needs it on Windows."
            env_set["CLAUDE_CODE_GIT_BASH_PATH"] = str(bash)
        argv = [str(claude), "--bg", "--name", story_id]
        extras: dict = {}
        if servers is not None:
            settings = {"enabledMcpjsonServers": servers}
            settings_file = Path(state_dir) / SETTINGS_NAME
            settings_file.write_text(json.dumps(settings), encoding="utf-8")
            argv += ["--settings", str(settings_file)]
            extras["settings"] = settings
        return argv + [prompt], env_set, extras, ""
    if tool() == "opencode":
        template = shlex.split(str(wfconfig.get("control_room.opencode_start_cmd", "opencode run {prompt}")
                                   or "opencode run {prompt}"))
        if not template or which(template[0]) is None:
            return None, {}, {}, f"`{template[0] if template else 'opencode'}` was not found on PATH."
        return [a.replace("{prompt}", prompt).replace("{story}", story_id) for a in template], {}, {}, ""
    return None, {}, {}, f"config loop.driver_tool {tool()!r} is not claude or opencode."


def start_session(main: Path, state_dir: Path, clock, story_id: str, sprint_key=None, overridden=()) -> dict:
    """Start a session for the story after the caller's checks; returns the outcome dict `launch_result` renders.
    Claude Code backgrounds itself (`claude --bg`, its output read for the session id); OpenCode runs in a window of
    the backend, or detached with log files under the state dir. `sprint_key` is kept on the attempt line, so the
    launch is found under the id or the key; `overridden` lists the launch ids a forced start skipped. A process the
    starter started is always recorded by pid."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    argv, env_set, extras, why = start_argv(Path(main), state_dir, story_id)
    if argv is None:
        return refused(why)
    env = {**session_env(), **env_set}
    removable = {*auto_dev_driver.SESSION_VARS, auto_dev_guard.MARKER}
    launch_id = f"{datetime.fromtimestamp(clock(), timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    if overridden:
        append_record(state_dir, {"event": "launch-override", "ts": iso(clock()), "story": story_id,
                                  "launch_ids": list(overridden)})
    chosen = backend() if tool() != "claude" else "cli"
    append_record(state_dir, {
        "event": "launch-attempt", "launch_id": launch_id, "ts": iso(clock()), "story": story_id,
        "sprint_key": sprint_key, "tool": tool(), "backend": chosen, "argv": argv, "cwd": str(main),
        "env_removed": sorted(k for k in os.environ if k in removable), "env_set": env_set, **extras})
    started, written = [], []

    def write_started(proc, created):
        append_record(state_dir, {"event": "launch-started", "launch_id": launch_id, "ts": iso(clock()),
                                  "pid": proc.pid, "created": created})
        written.append(proc)

    def record_started(proc):
        started.append(proc)
        try:
            created = process_created(proc.pid)
        except Exception:  # noqa: BLE001 -- a creation time that cannot be read is recorded as null
            created = None
        try:
            write_started(proc, created)
        except OSError:
            pass  # written again once the starter returns

    if chosen in ("tmux", "windows-terminal"):
        return _start_in_window(state_dir, clock, launch_id, story_id, chosen, argv, str(main))
    if chosen == "background":
        try:
            proc = spawn_window(argv, cwd=str(main), env=env, log_prefix=state_dir / f"launch-{launch_id}")
        except OSError as exc:
            append_record(state_dir, {"event": "launch-result", "launch_id": launch_id, "ts": iso(clock()),
                                      "exit_code": None, "session_id": None, "stderr_tail": str(exc)})
            return {"status": "error", "session_id": None, "message": f"The session did not start: {exc}"}
        record_started(proc)
        return {"status": "ok", "session_id": None,
                "message": f"Started a detached {tool()} session for {story_id} (pid {proc.pid}); its output is in "
                           f"{state_dir / f'launch-{launch_id}'}.out/.err."}

    returncode, out, err = run_starter(argv, cwd=main, env=env, timeout=LAUNCH_TIMEOUT_SECONDS,
                                       out_prefix=state_dir / f"launch-{launch_id}", on_start=record_started)
    if started and not written:
        try:
            write_started(started[0], None)
        except OSError as exc:
            return {"status": "error", "session_id": None,
                    "message": f"Process {started[0].pid} started, but its launch-started line could not be written "
                               f"({type(exc).__name__}: {exc}); the launch guard does not know it."}
    if returncode is None and started:
        append_record(state_dir, {"event": "launch-timeout", "launch_id": launch_id, "ts": iso(clock())})
        return {"status": "error", "session_id": None,
                "message": f"The launch timed out after {LAUNCH_TIMEOUT_SECONDS} s ({tail(err)}); a session that "
                           f"started appears in the listing under the name {story_id}."}
    match = BACKGROUNDED.search(out)
    session_id = match.group(1) if match else None
    append_record(state_dir, {"event": "launch-result", "launch_id": launch_id, "ts": iso(clock()),
                              "exit_code": returncode, "session_id": session_id, "stdout_tail": tail(out),
                              "stderr_tail": tail(err)})
    if returncode is None:
        return {"status": "error", "session_id": None, "message": f"`{Path(argv[0]).name}` did not start: {tail(err)}"}
    if returncode != 0:
        return {"status": "error", "session_id": session_id,
                "message": f"`{Path(argv[0]).name}` exited {returncode}: {tail(err) or tail(out)}"}
    if session_id is None:
        return {"status": "ok", "session_id": None,
                "message": f"Launched {story_id}; id not read: a session that started appears in the listing under "
                           f"the name {story_id}."}
    return {"status": "ok", "session_id": session_id, "message": f"Started session {session_id} for {story_id}."}


def _start_in_window(state_dir: Path, clock, launch_id: str, story_id: str, chosen: str, argv: list[str],
                     directory: str) -> dict:
    """An interactive session start in a terminal window: the window owns the session, so the launch ends at its
    result line (the listing, not a pid, shows the session)."""
    if chosen == "windows-terminal":
        window, _script, why = windows_terminal_argv(directory, [shlex.join(argv)] if os.name != "nt"
                                                     else [subprocess.list2cmdline(argv)])
        where = "a terminal window"
    else:
        window, where, why = tmux_argv(directory, argv, f"{story_id}-{launch_id[-6:]}")
    if window is None:
        append_record(state_dir, {"event": "launch-result", "launch_id": launch_id, "ts": iso(clock()),
                                  "exit_code": None, "session_id": None, "stderr_tail": why})
        return refused(why)
    try:
        spawn_window(window, env=session_env())
    except OSError as exc:
        append_record(state_dir, {"event": "launch-result", "launch_id": launch_id, "ts": iso(clock()),
                                  "exit_code": None, "session_id": None, "stderr_tail": str(exc)})
        return {"status": "error", "session_id": None, "message": f"The window did not open: {exc}"}
    append_record(state_dir, {"event": "launch-result", "launch_id": launch_id, "ts": iso(clock()),
                              "exit_code": 0, "session_id": None, "window": window})
    return {"status": "ok", "session_id": None, "message": f"Started {story_id} in {where}."}
