#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Identify what is generating traffic to a compose service port.

Captures the request fingerprint inside the target container with tcpdump, classifies the
source IP as host-origin or cross-container, then traces the source process:

    1. Resolve the service container (`<compose> -p <project> ps -q <service>`), confirm it is
       running, and make tcpdump available in it (installed on demand via apt-get / apk /
       dnf / microdnf / yum).
    2. Capture N push-flag packets on the internal port INSIDE the container
       (`<compose> -p <project> exec -T <service> timeout <s> tcpdump -i any -nn -A ...`).
       The filter is `tcp port <port> and tcp[tcpflags] & tcp-push != 0` — never a network-wide
       sniff.
    3. Parse the first inbound source IP and the HTTP fingerprint (method, path, User-Agent,
       Content-Length, first body line).
    4. Classify: source IP == a network gateway of the container → HOST-ORIGIN; source IP ==
       another container on one of its docker networks → CROSS-CONTAINER.
    5a. Host-origin: find the published host port (`docker port`), then poll the host's TCP
        table for client sockets whose remote port is that host port and print PID, executable,
        command line, parent and grandparent. Backends, isolated in `host_connections()` /
        `process_info()`: psutil when importable (all OSes); otherwise `lsof` + `ps`
        (Linux/macOS). Windows requires psutil.
    5b. Cross-container: run a /proc-based lookup inside the source container (socket inode in
        /proc/net/tcp{,6} → owning PID via /proc/*/fd) and print PID, cmdline, parent.

Usage
-----
    uv run --no-project .workflow/scripts/trace_port.py --service <svc> --internal-port <port>
        [--project-name <compose project>] [--capture-packets 10] [--capture-timeout 60]
        [--poll-interval-ms 30] [--poll-sec 15] [--exec-user 0]
    # psutil backend (required on Windows):
    uv run --no-project --with psutil .workflow/scripts/trace_port.py ...

    --project-name defaults to `config: slots.shared_stack_name`. The compose command is
    `config: stack.compose_cmd`, run from `config: stack.compose_dir`.

Exit codes
----------
    0 report printed · 2 no running container / bad args · 3 tcpdump unavailable ·
    4 no packets within the timeout · 5 no source IP parsed · 6 source IP matches neither a
    gateway nor a container · 7 internal port not published to the host (host-origin case) ·
    8 no host process backend available

When to use
-----------
    A service's logs show repeated unexpected requests of unknown origin and you need the
    spawning process and its command line. Dev stacks only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

try:
    import psutil  # type: ignore
except ImportError:  # optional backend
    psutil = None

HDR_RE = re.compile(
    r"^\s*\d\d:\d\d:\d\d\.\d+\s+(?:\S+\s+)?(?:(?:In|Out|P|M|B)\s+)?IP\s+"
    r"(\d+\.\d+\.\d+\.\d+)\.\d+\s+>\s+(\d+\.\d+\.\d+\.\d+)\.(\d+):")

PROC_LOOKUP = r"""#!/bin/sh
TARGET_PORT=$1
PATTERN=":$(printf '%04X' "$TARGET_PORT")"
INODE=$(awk -v p="$PATTERN" '$3 ~ p"$" {print $10; exit}' /proc/net/tcp 2>/dev/null)
[ -z "$INODE" ] && INODE=$(awk -v p="$PATTERN" '$3 ~ p"$" {print $10; exit}' /proc/net/tcp6 2>/dev/null)
if [ -z "$INODE" ] || [ "$INODE" = "0" ]; then echo "NO_SOCKET_FOUND_FOR_PORT_$TARGET_PORT"; exit 1; fi
echo "socket inode: $INODE"
for pid_dir in /proc/[0-9]*; do
  pid=$(basename "$pid_dir")
  for fd in "$pid_dir"/fd/*; do
    [ -L "$fd" ] || continue
    if [ "$(readlink "$fd" 2>/dev/null)" = "socket:[$INODE]" ]; then
      echo "PID=$pid"; printf '  CMD: '; tr '\0' ' ' < "$pid_dir/cmdline" 2>/dev/null; echo
      ppid=$(awk '/^PPid:/ {print $2}' "$pid_dir/status" 2>/dev/null)
      if [ -n "$ppid" ] && [ "$ppid" != "0" ]; then
        echo "PARENT_PID=$ppid"; printf '  PARENT_CMD: '; tr '\0' ' ' < "/proc/$ppid/cmdline" 2>/dev/null; echo
      fi
      exit 0
    fi
  done
done
echo "NO_PID_FOUND_FOR_INODE_$INODE"
exit 2
"""

INSTALL_TCPDUMP = (
    "if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq tcpdump; "
    "elif command -v apk >/dev/null 2>&1; then apk add --no-cache tcpdump; "
    "elif command -v dnf >/dev/null 2>&1; then dnf install -y -q tcpdump; "
    "elif command -v microdnf >/dev/null 2>&1; then microdnf install -y tcpdump; "
    "elif command -v yum >/dev/null 2>&1; then yum install -y -q tcpdump; "
    "else echo 'no supported package manager'; exit 1; fi"
)


def section(title: str) -> None:
    print("\n" + "=" * 72 + f"\n  {title}\n" + "=" * 72, flush=True)


def fail(msg: str, code: int) -> int:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    return code


def run(cmd: list, timeout: float | None = 60, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return subprocess.CompletedProcess(cmd, 124, out, "timeout")


# --------------------------------------------------------------------------- docker helpers

class Docker:
    def __init__(self, project: str, exec_user: str):
        self.compose = shlex.split(str(wfconfig.get("stack.compose_cmd", "docker compose")))
        first = Path(self.compose[0]).name
        self.docker = "podman" if first.startswith("podman") else "docker"
        self.project = project
        self.cwd = wfconfig.repo_root() / str(wfconfig.get("stack.compose_dir", "."))
        self.user = ["-u", exec_user] if exec_user else []

    def compose_run(self, *args: str, timeout: float | None = 60) -> subprocess.CompletedProcess:
        return run([*self.compose, "-p", self.project, *args], timeout=timeout, cwd=self.cwd)

    def svc_exec(self, service: str, *args: str, timeout: float | None = 60) -> subprocess.CompletedProcess:
        return self.compose_run("exec", "-T", *self.user, service, *args, timeout=timeout)

    def d(self, *args: str, timeout: float | None = 60) -> subprocess.CompletedProcess:
        return run([self.docker, *args], timeout=timeout)


def networks_of(dk: Docker, container_id: str) -> dict:
    """{network name: gateway ip} for the container."""
    r = dk.d("inspect", "--format", "{{json .NetworkSettings.Networks}}", container_id)
    try:
        nets = json.loads(r.stdout.strip() or "{}")
    except ValueError:
        return {}
    out = {}
    for name, info in (nets or {}).items():
        gw = (info or {}).get("Gateway") or ""
        if not gw:
            g = dk.d("network", "inspect", name, "--format", "{{range .IPAM.Config}}{{.Gateway}} {{end}}")
            gw = (g.stdout.split() or [""])[0]
        out[name] = gw
    return out


def ip_to_container(dk: Docker, network: str) -> dict:
    r = dk.d("network", "inspect", network, "--format", "{{json .Containers}}")
    try:
        conts = json.loads(r.stdout.strip() or "{}") or {}
    except ValueError:
        return {}
    out = {}
    for info in conts.values():
        ip = str(info.get("IPv4Address", "")).split("/")[0]
        if ip:
            out[ip] = info.get("Name", "?")
    return out


# --------------------------------------------------------------------------- host backends (OS-isolated)

def host_connections(host_port: int) -> set:
    """PIDs owning client TCP sockets whose REMOTE port is host_port (excluding TIME_WAIT)."""
    if psutil is not None:
        pids = set()
        try:
            conns = psutil.net_connections(kind="tcp")
        except (psutil.AccessDenied, PermissionError):
            conns = []
        for c in conns:
            if c.raddr and c.raddr.port == host_port and c.pid and c.status != "TIME_WAIT":
                pids.add(c.pid)
        return pids
    if os.name == "nt" or not shutil.which("lsof"):
        raise RuntimeError("no host backend")
    r = run(["lsof", "-nP", f"-iTCP:{host_port}", "-sTCP:ESTABLISHED", "-Fpn"], timeout=10)
    pids, pid = set(), None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid and re.search(rf"->.*:{host_port}$", line):
            pids.add(pid)
    return pids


def process_info(pid: int) -> dict | None:
    """{pid, name, exe, cmd, ppid} or None if the process is gone."""
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                try:
                    exe = p.exe()
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    exe = "?"
                try:
                    cmd = " ".join(p.cmdline())
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    cmd = "?"
                return {"pid": pid, "name": p.name(), "exe": exe, "cmd": cmd, "ppid": p.ppid()}
        except psutil.NoSuchProcess:
            return None
    r = run(["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)], timeout=5)
    c = run(["ps", "-o", "command=", "-p", str(pid)], timeout=5)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    ppid_s, _, comm = r.stdout.strip().partition(" ")
    return {"pid": pid, "name": Path(comm.strip()).name, "exe": comm.strip(),
            "cmd": c.stdout.strip(), "ppid": int(ppid_s) if ppid_s.isdigit() else 0}


def trace_host(dk: Docker, container_id: str, internal_port: int, poll_ms: int, poll_s: int) -> int:
    r = dk.d("port", container_id, f"{internal_port}/tcp")
    host_port = None
    for line in r.stdout.splitlines():
        m = re.search(r":(\d+)\s*$", line)
        if m:
            host_port = int(m.group(1))
            break
    if not host_port:
        return fail(f"`docker port` returned nothing usable for {internal_port}/tcp — service may "
                    f"not be published: {r.stdout.strip()!r}", 7)
    backend = "psutil" if psutil is not None else "lsof+ps"
    print(f"host port: {host_port} — polling host TCP table ({backend}) for {poll_s}s...", flush=True)

    seen: set = set()
    reports: list = []
    start = last_beat = time.monotonic()
    while time.monotonic() - start < poll_s:
        try:
            pids = host_connections(host_port)
        except RuntimeError:
            return fail("no host process backend: install psutil (uv run --no-project --with psutil "
                        "...) — required on Windows; on Linux/macOS `lsof` also works.", 8)
        for pid in sorted(pids - seen):
            seen.add(pid)
            info = process_info(pid)
            if not info:
                reports.append(f"[exited before lookup] PID={pid}")
                continue
            blk = [f"PID={pid}  Name={info['name']}", f"  EXE: {info['exe']}", f"  CMD: {info['cmd']}"]
            parent = process_info(info["ppid"]) if info["ppid"] else None
            if parent:
                blk += [f"  PARENT [{parent['pid']}] {parent['name']}", f"    CMD: {parent['cmd']}"]
                gp = process_info(parent["ppid"]) if parent["ppid"] else None
                if gp:
                    blk += [f"  GRANDPARENT [{gp['pid']}] {gp['name']}", f"    CMD: {gp['cmd']}"]
            reports.append("\n".join(blk))
        now = time.monotonic()
        if now - last_beat >= 2:
            print(f"[{datetime.now():%H:%M:%S}] still polling host port {host_port}... "
                  f"{len(seen)} PID(s) caught", flush=True)
            last_beat = now
        time.sleep(poll_ms / 1000)

    if not reports:
        print(f"host poll caught no live connections in {poll_s}s. The source process may close its "
              f"socket faster than the poll cadence — re-run with a smaller --poll-interval-ms "
              f"(e.g. 10) or a longer --poll-sec.")
    else:
        print(f"Caught {len(reports)} distinct PID(s):")
        for rep in reports:
            print("\n" + rep)
    return 0


def trace_container(dk: Docker, source_container: str, internal_port: int) -> int:
    b64 = base64.b64encode(PROC_LOOKUP.encode()).decode()
    print(f"running /proc-based lookup inside {source_container}...", flush=True)
    r = dk.d("exec", source_container, "sh", "-c", f"echo {b64} | base64 -d | sh -s -- {internal_port}")
    print((r.stdout + r.stderr).rstrip())
    if "NO_SOCKET_FOUND" in r.stdout:
        print(f"\nNo socket to dst port {internal_port} was open at the moment of lookup — likely a "
              f"short-lived connection. Re-run while traffic is firing, or repeat in quick succession.")
    return 0


# --------------------------------------------------------------------------- main

def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Trace the source of traffic to a compose service port.")
    ap.add_argument("--service", required=True)
    ap.add_argument("--internal-port", type=int, required=True)
    ap.add_argument("--project-name", default=None)
    ap.add_argument("--capture-packets", type=int, default=10)
    ap.add_argument("--capture-timeout", type=int, default=60)
    ap.add_argument("--poll-interval-ms", type=int, default=30)
    ap.add_argument("--poll-sec", type=int, default=15)
    ap.add_argument("--exec-user", default="",
                    help="user for in-container exec (e.g. 0 when the service runs non-root)")
    a = ap.parse_args(argv)

    project = a.project_name or str(wfconfig.get("slots.shared_stack_name", wfconfig.repo_root().name))
    dk = Docker(project, a.exec_user)

    section("Step 1: container & tcpdump")
    r = dk.compose_run("ps", "-q", a.service)
    cid = (r.stdout.split() or [""])[0]
    if not cid:
        return fail(f"no running container for service '{a.service}' in project '{project}'. "
                    f"Bring the stack up first.", 2)
    print(f"container id: {cid}")
    state = dk.d("inspect", "--format", "{{.State.Status}}", cid).stdout.strip()
    if state != "running":
        return fail(f"container {cid} is in state '{state}' (expected 'running').", 2)
    if dk.svc_exec(a.service, "sh", "-c", "command -v tcpdump").returncode != 0:
        print("tcpdump not present in container — installing...", flush=True)
        inst = dk.svc_exec(a.service, "sh", "-c", INSTALL_TCPDUMP, timeout=300)
        print("\n".join((inst.stdout + inst.stderr).splitlines()[-5:]))
        if dk.svc_exec(a.service, "sh", "-c", "command -v tcpdump").returncode != 0:
            return fail("tcpdump install failed — package repos unreachable, unsupported image, or "
                        "the exec user lacks root (try --exec-user 0).", 3)
        print("tcpdump installed.")
    else:
        print("tcpdump already present.")

    section(f"Step 2: capture {a.capture_packets} PSH packets on tcp:{a.internal_port} "
            f"(timeout {a.capture_timeout}s)")
    bpf = f"tcp port {a.internal_port} and (tcp[tcpflags] & tcp-push != 0)"
    cap = dk.svc_exec(a.service, "timeout", str(a.capture_timeout), "tcpdump", "-i", "any", "-nn",
                      "-A", "-s", "2048", "-c", str(a.capture_packets), bpf,
                      timeout=a.capture_timeout + 30)
    text = cap.stdout + cap.stderr
    print(text)
    if not re.search(r"IP\s+\d+\.\d+\.\d+\.\d+", text):
        return fail(f"tcpdump captured no packets within {a.capture_timeout}s. Traffic may have "
                    f"stopped — confirm the requests still appear in the service logs and re-run.", 4)

    section("Step 3: request fingerprint")
    src_ip = None
    for line in text.splitlines():
        m = HDR_RE.match(line)
        if m and int(m.group(3)) == a.internal_port:
            src_ip = m.group(1)
            break
    if not src_ip:
        return fail("could not parse a source IP from the capture (printed above); inspect manually.", 5)
    print(f"source IP: {src_ip}")
    m = re.search(r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)\s+HTTP/", text)
    print(f"first request: {m.group(1)} {m.group(2)}" if m
          else "first request: (HTTP method line not found — non-HTTP traffic?)")
    for label, rx in (("User-Agent", r"(?im)^.*?User-Agent:\s*(.+?)\s*$"),
                      ("Content-Length", r"(?im)^.*?Content-Length:\s*(\d+)\s*$")):
        mm = re.search(rx, text)
        if mm:
            print(f"{label}: {mm.group(1)}")
    body = re.search(r"\r?\n\r?\n([^\r\n]{1,500})", text)
    if body:
        print(f"body (first 500 chars): {body.group(1)}")

    section("Step 4: classify source IP")
    nets = networks_of(dk, cid)
    print("container networks: " + (", ".join(f"{n} (gateway {g or '?'})" for n, g in nets.items()) or "(none)"))
    origin, source_container = None, None
    if src_ip in {g for g in nets.values() if g}:
        origin = "host"
        print("classification: HOST-ORIGIN (source IP is a docker network gateway)")
    else:
        for net in nets:
            hit = ip_to_container(dk, net).get(src_ip)
            if hit:
                origin, source_container = "container", hit
                print(f"classification: CROSS-CONTAINER (source = {hit}, network {net})")
                break
    if not origin:
        print(f"classification: UNKNOWN — {src_ip} matches neither a gateway nor a container on "
              f"{', '.join(nets) or 'any network'}. Inspect `docker network ls` and each network.")
        return 6

    section("Step 5: source process trace")
    if origin == "host":
        rc = trace_host(dk, cid, a.internal_port, a.poll_interval_ms, a.poll_sec)
    else:
        rc = trace_container(dk, source_container, a.internal_port)
    section("done")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
