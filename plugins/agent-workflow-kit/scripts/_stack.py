"""Internal helpers shared by the slot/stack lifecycle scripts (not a CLI).

- repo-root override (`--repo-root` flags)
- compose argv / cwd / env built from config (`stack.*`) — every compose call a
  lifecycle script makes goes through `compose_argv` + `wfconfig.compose_env`
- the ONE definition of "service is ready" (`ready_services`)
- slot / worktree lookup for a stack name via the slot registry
- cross-platform discovery and killing of `compose watch` processes anchored in a
  directory (psutil when importable; POSIX fallback `ps` + /proc or `lsof`;
  Windows without psutil: detection unavailable, reported as such)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

try:  # optional accelerator; every caller has a fallback
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - depends on the environment
    psutil = None

STACK_NONE_REASON = 'config: stack.runtime = "none" (slots are worktrees only)'
WATCH_TOKEN = re.compile(r"(?<![-\w])watch(?![-\w])")   # bare subcommand; excludes `--watch`
COMPOSE_TOKEN = re.compile(r"\bcompose\b")


# ------------------------------------------------------------------ general

def set_repo_root(value: str | None) -> Path:
    """Apply a `--repo-root` override for this process and its children."""
    if value:
        os.environ["WORKFLOW_REPO_ROOT"] = str(Path(value).resolve())
        wfconfig.repo_root.cache_clear()
        wfconfig.load.cache_clear()
    return wfconfig.repo_root()


def skipped(step: str, reason: str) -> None:
    print(f"SKIPPED: {step} — {reason}")


def scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def run(cmd: list[str], timeout: int | None = 60, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, timeout=timeout, **kw)


# ------------------------------------------------------------------ compose

def compose_prefix() -> list[str]:
    return shlex.split(str(wfconfig.get("stack.compose_cmd", "docker compose")))


def engine_cmd() -> str:
    """Container-engine CLI for non-compose calls (volume/network listing)."""
    return str(wfconfig.get("stack.engine_cmd", "") or "docker")


def compose_argv(stack: str, *args: str, with_file: bool = False) -> list[str]:
    """`<compose_cmd> -p <stack> [-f <compose_file>] <args>`.

    with_file=True for commands that need the project model (up, watch, start);
    label-addressed commands (ps, logs, exec, cp, down) work from any cwd without it.
    """
    argv = compose_prefix() + ["-p", stack]
    if with_file:
        f = str(wfconfig.get("stack.compose_file", "") or "")
        if f:
            argv += ["-f", f]
    return argv + list(args)


def compose_dir(worktree: Path) -> Path:
    return (Path(worktree) / str(wfconfig.get("stack.compose_dir", ".") or ".")).resolve()


def expected_services() -> list[str]:
    return [str(s) for s in (wfconfig.get("stack.services", []) or [])]


def app_service() -> str:
    return str(wfconfig.get("stack.app_service", "backend"))


def parse_ps_json(stdout: str) -> list[dict]:
    """`compose ps --format json` — NDJSON (current compose) or one JSON array (older)."""
    text = stdout.strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
            return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def compose_ps(stack: str, cwd: Path, env: dict) -> list[dict]:
    out = subprocess.run(compose_argv(stack, "ps", "-a", "--format", "json"),
                         capture_output=True, text=True, errors="replace", cwd=cwd, env=env)
    return parse_ps_json(out.stdout)


def ready_services(services: list[dict]) -> set[str]:
    """Names of services that count as READY: container `running` AND, for every
    service listed in `config: stack.require_healthy`, health `healthy`.

    Single definition shared by bring_up_stack_async.py and wait_for_stack_ready.py.
    """
    need_healthy = {str(s) for s in (wfconfig.get("stack.require_healthy", []) or [])}
    return {
        name for s in services
        if (name := s.get("Service"))
        and s.get("State") == "running"
        and (name not in need_healthy or s.get("Health") == "healthy")
    }


def ports_env_prefix(slot: int) -> str:
    """POSIX `VAR=val ...` prefix for printed remedy commands."""
    return " ".join(f"{k}={v}" for k, v in wfconfig.slot_ports(slot).items())


# ------------------------------------------------------------------ registry lookups

def _registry_find(story_id: str) -> dict | None:
    try:
        import _registry_lookup
    except ImportError:
        return None
    ids = [story_id, story_id.replace(".", "-")]
    return _registry_lookup.find(ids)


def story_id_for_stack(stack: str) -> str | None:
    prefix = str(wfconfig.get("slots.stack_prefix", "story-"))
    if prefix and stack.startswith(prefix):
        return stack[len(prefix):]
    return None


def slot_for_stack(stack: str) -> int | None:
    """Shared stack → shared slot; `<prefix><id>` → the registry row's slot; else None."""
    if stack == str(wfconfig.get("slots.shared_stack_name", "")):
        return int(wfconfig.get("slots.shared_slot", 0))
    sid = story_id_for_stack(stack)
    if sid is None:
        return None
    row = _registry_find(sid)
    return int(row["slot"]) if row else None


def env_for_stack(stack: str) -> dict[str, str]:
    """compose_env for the stack's slot (ALL port vars). Unknown slot → the shared
    slot's ports: label-addressed commands (ps/logs/exec/cp/down) bind no ports, but
    every compose call still carries a complete port set."""
    slot = slot_for_stack(stack)
    if slot is None:
        slot = int(wfconfig.get("slots.shared_slot", 0))
    return wfconfig.compose_env(slot)


def worktree_for_stack(stack: str) -> Path | None:
    """Shared stack → repo root; `<prefix><id>` → registry worktree (fallback: the
    conventional worktree path if it exists); else None."""
    if stack == str(wfconfig.get("slots.shared_stack_name", "")):
        return wfconfig.repo_root()
    sid = story_id_for_stack(stack)
    if sid is None:
        return None
    row = _registry_find(sid)
    if row and row.get("worktree_path"):
        return Path(row["worktree_path"])
    fallback = wfconfig.worktree_path(sid)
    return fallback if fallback.is_dir() else None


# ------------------------------------------------------------------ processes

def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(p)).rstrip("\\/")


def path_inside(child: str, root: Path) -> bool:
    c, r = _norm(child), _norm(str(root))
    return c == r or c.startswith(r + os.sep) or c.startswith(r + "/")


def _engine_names() -> set[str]:
    names = {"docker", "docker-compose", "podman", "podman-compose", "com.docker.cli"}
    first = Path(compose_prefix()[0]).name.lower() if compose_prefix() else ""
    if first:
        names.add(first)
    return names


def is_compose_process(name: str, cmdline: str) -> bool:
    base = name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return (base in _engine_names() or "docker" in base or "compose" in base) \
        and bool(COMPOSE_TOKEN.search(cmdline))


def is_compose_watch(name: str, cmdline: str) -> bool:
    return is_compose_process(name, cmdline) and bool(WATCH_TOKEN.search(cmdline))


def _posix_cwd(pid: int) -> str | None:
    proc_link = f"/proc/{pid}/cwd"
    if os.path.exists(f"/proc/{pid}"):
        try:
            return os.readlink(proc_link)
        except OSError:
            return None
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def _posix_process_table() -> list[dict]:
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=", "-o", "ppid=", "-o", "args="],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        args = parts[2]
        name = Path(args.split()[0]).name if args.split() else ""
        rows.append({"pid": int(parts[0]), "ppid": int(parts[1]) if parts[1].isdigit() else 0,
                     "name": name, "cmdline": args})
    return rows


def process_table() -> tuple[list[dict], str | None]:
    """[{pid, ppid, name, cmdline}] plus a note when detection is degraded/unavailable."""
    if psutil is not None:
        rows = []
        for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            info = p.info
            rows.append({"pid": info["pid"], "ppid": info.get("ppid") or 0,
                         "name": info.get("name") or "",
                         "cmdline": " ".join(info.get("cmdline") or [])})
        return rows, None
    if os.name == "nt":
        return [], ("psutil is not installed — on Windows a process's working directory cannot "
                    "be read without it. Re-run with `uv run --with psutil --no-project ...` "
                    "(or `pip install psutil` in the interpreter you use).")
    return _posix_process_table(), None


def process_cwd(pid: int) -> str | None:
    if psutil is not None:
        try:
            return psutil.Process(pid).cwd()
        except (psutil.Error, OSError):
            return None
    if os.name == "nt":
        return None
    return _posix_cwd(pid)


def find_compose_watch(root: Path, verbose: bool = False) -> tuple[list[dict], str | None]:
    """compose-watch processes whose CWD is `root` or inside it.

    Returns (matches [{pid, ppid, name, cmdline, cwd}], note). A non-None note means
    detection was unavailable — callers must say so, never treat it as "none found".
    The CWD-prefix filter is the safety net: a broad command-line match cannot reach
    a process anchored in another slot's directory.
    """
    table, note = process_table()
    if note:
        return [], note
    found = []
    for row in table:
        if not is_compose_watch(row["name"], row["cmdline"]):
            continue
        cwd = process_cwd(row["pid"])
        if verbose:
            print(f"  candidate pid={row['pid']:<6} cwd={cwd or '<unreadable>'}")
        if cwd and path_inside(cwd, root):
            found.append({**row, "cwd": cwd})
    return found, None


def process_name(pid: int) -> str:
    if psutil is not None:
        try:
            return psutil.Process(pid).name() or ""
        except (psutil.Error, OSError):
            return ""
    for row in (_posix_process_table() if os.name != "nt" else []):
        if row["pid"] == pid:
            return row["name"]
    return ""


def kill_pid(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        if psutil is not None:
            psutil.Process(pid).kill()
        elif os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=30)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except Exception:  # noqa: BLE001 - already gone / no permission: the summary shows what died
        return False
