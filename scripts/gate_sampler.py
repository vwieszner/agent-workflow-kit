#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
r"""Load and probe sampler for a full-gate run: VM CPU, per-container cost, and config-defined probes.

Standalone: not part of the gate, not wired into its summary. Start it just before a gate and stop it
after (`--stop-file`, `--max-seconds`, Ctrl-C); `summarize` then windows the JSONL to the gate's
`[hh:mm:ss] <LEG>: exit` lines and checks the budgets.

Usage
-----
    uv run --no-project .workflow/scripts/gate_sampler.py sample --out run1.jsonl \
        [--stack <stack>] [--story-id <id>] [--interval 10] [--max-seconds 3600] [--stop-file stop.flag] \
        [--no-stats] [--no-probes] [--local]
    uv run --no-project .workflow/scripts/gate_sampler.py summarize run1.jsonl \
        [--from 21:01:10 --to 21:09:40] [--stack <stack>] [--check]

What `sample` records (one JSONL line each)
-------------------------------------------
    sample  every --interval s (<= 15), from the main loop: 1/5/15-min loadavg, the CPU busy /
            iowait / idle split from `/proc/stat` deltas, procs_running / procs_blocked, CPU count.
            Read inside a container of the stack (`/proc/loadavg` and `/proc/stat` are not
            namespaced: the reading is the container VM's), preferring
            `config: sampler.reference_services` — pick quiet services, not the busiest one, whose
            starved `exec` would blur the reading. `--local` (or no container engine / no running
            container) reads the host's own `/proc` on Linux, else psutil (optional).
            Loadavg counts runnable AND uninterruptible-I/O tasks; the split and the two counters
            separate CPU starvation from queued I/O.
    ctr     back to back, at least --stats-interval s apart, from a second thread: `<engine> stats
            --no-stream` for every running container (CPU %, memory, cumulative block I/O), plus the
            host's total CPU (psutil, else Windows typeperf; best effort). It runs off the main loop:
            the call takes seconds and stretches under load.
    probe   one line per run of each `config: sampler.probes` entry ({ name, cmd, interval_s,
            timeout_s, slow_s }), each on its own thread. `cmd` is a template ({stack} {slot}
            {worktree} and the slot port vars); its exit code, duration and output tail are recorded.
            A probe measures what the gate's tests wait on (a database's open transactions, a queue
            depth, a health endpoint's latency); a probe that fails or times out is itself evidence.

`summarize` reports peak/mean load, the CPU split, per-stack and per-container cores and block I/O,
and per probe: runs, failures, slow runs (duration above `slow_s`), p95 and max duration. Budgets:
load1 above `config: sampler.load_budget` x CPU count is an over-budget sample; any probe failure or
slow run is over budget. `--check` exits 1 when anything is over budget.

Exit codes: sample 0 ok, 2 usage; summarize 0 ok, 1 no samples / over budget with --check.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import wfconfig  # noqa: E402

POLLER_JOIN_S = 20.0
VM_CMD = ("cat /proc/loadavg; "
          "grep -E '^(cpu |procs_running|procs_blocked)' /proc/stat; "
          "grep -c '^processor' /proc/cpuinfo")
_SIZE = re.compile(r"^\s*([\d.]+)\s*([A-Za-z]+)\s*$")
_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
          "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}
_CONTAINER = re.compile(r"^(?P<stack>.+)-(?P<svc>[a-z0-9_.-]+?)-\d+$")


def _hms(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else time.time()).strftime("%H:%M:%S")


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def to_bytes(text: str | None) -> int | None:
    m = _SIZE.match(text or "")
    if not m or m.group(2) not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def stack_of(container_name: str) -> str:
    m = _CONTAINER.match(container_name)
    return m.group("stack") if m else container_name


def engine() -> list[str]:
    return shlex.split(str(wfconfig.get("stack.engine_cmd", "") or "docker"))


def _engine(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([*engine(), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


class NoReferenceContainer(RuntimeError):
    """--stack names a stack with no running container."""


def pick_reference(stack: str | None, timeout: float = 20.0) -> str | None:
    """A running container of `stack` to read the VM from, preferring `config: sampler.reference_services`."""
    ps = _engine("ps", "--format", "{{.Names}}", timeout=timeout)
    if ps.returncode != 0:
        # A daemon that is down is not a stack with no containers: keep the in-loop retry.
        raise RuntimeError((ps.stderr or f"ps exit {ps.returncode}").strip())
    names = ps.stdout.split()
    if not stack:
        return names[0] if names else None
    for svc in wfconfig.get("sampler.reference_services", []) or []:
        if f"{stack}-{svc}-1" in names:
            return f"{stack}-{svc}-1"
    own = [n for n in names if stack_of(n) == stack]
    if not own:
        raise NoReferenceContainer(f"no running container of stack {stack}")
    return own[0]


def parse_vm(stdout: str) -> dict:
    lines = stdout.splitlines()
    load = lines[0].split()
    running, _, threads = load[3].partition("/")
    cpu = [int(x) for x in lines[1].split()[1:9]]  # user nice system idle iowait irq softirq steal
    counters = {ln.split()[0]: int(ln.split()[1]) for ln in lines[2:4]}
    return {"load1": float(load[0]), "load5": float(load[1]), "load15": float(load[2]),
            "runnable": int(running), "threads": int(threads), "cpu": cpu,
            "procs_running": counters.get("procs_running"),
            "procs_blocked": counters.get("procs_blocked"), "ncpu": int(lines[4])}


def read_vm(ref: str | None, timeout: float = 12.0) -> dict:
    """The VM reading through `ref`; with no reference, the host's own `/proc` (Linux) or psutil."""
    if ref:
        r = _engine("exec", ref, "sh", "-c", VM_CMD, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or f"exec exit {r.returncode}").strip())
        return parse_vm(r.stdout)
    if Path("/proc/loadavg").exists():
        r = subprocess.run(["sh", "-c", VM_CMD], capture_output=True, text=True, timeout=timeout)
        return parse_vm(r.stdout)
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("no /proc and no psutil: no VM reading on this host") from exc
    l1, l5, l15 = os.getloadavg() if hasattr(os, "getloadavg") else psutil.getloadavg()
    t = psutil.cpu_times()
    cpu = [int(getattr(t, k, 0) * 100) for k in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")]
    return {"load1": l1, "load5": l5, "load15": l15, "runnable": None, "threads": None, "cpu": cpu,
            "procs_running": None, "procs_blocked": None, "ncpu": psutil.cpu_count() or 1}


def cpu_split(prev: list[int], cur: list[int], ncpu: int) -> dict:
    user, nice, system, idle, iowait, irq, softirq, steal = (c - p for c, p in zip(cur, prev))
    busy = user + nice + system + irq + softirq + steal
    total = busy + idle + iowait
    if total <= 0:
        return {}
    return {"cpu_busy_pct": round(busy * 100 / total, 2),
            "cpu_iowait_pct": round(iowait * 100 / total, 2),
            "cpu_idle_pct": round(idle * 100 / total, 2),
            "busy_cores": round(busy / total * ncpu, 2)}


def read_containers() -> list[dict]:
    r = _engine("stats", "--no-stream", "--format", "{{json .}}", timeout=120)
    out = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        blk_r, _, blk_w = (d.get("BlockIO") or "").partition("/")
        cpu = (d.get("CPUPerc") or "").rstrip("%")
        out.append({"n": d.get("Name", "?"), "cpu": float(cpu) if re.match(r"^[\d.]+$", cpu) else None,
                    "mem": to_bytes((d.get("MemUsage") or "").split("/")[0]),
                    "br": to_bytes(blk_r), "bw": to_bytes(blk_w),
                    "pids": int(d["PIDs"]) if str(d.get("PIDs", "")).isdigit() else None})
    return out


def read_host_cpu() -> float | None:
    """Host total CPU %: psutil when installed, else Windows typeperf; None when neither answers."""
    try:
        import psutil  # type: ignore[import-not-found]
        return float(psutil.cpu_percent(interval=1.0))
    except ImportError:
        pass
    if sys.platform != "win32":
        return None
    try:
        r = subprocess.run(["typeperf", r"\Processor(_Total)\% Processor Time", "-sc", "2"],
                           capture_output=True, text=True, timeout=15)
        values = []
        for row in csv.reader(io.StringIO(r.stdout)):
            if len(row) == 2:
                try:
                    values.append(float(row[1]))
                except ValueError:
                    pass  # header and trailing status lines
        return values[-1] if values else None
    except Exception:  # noqa: BLE001 - best effort, never blocks a sample
        return None


class Sink:
    def __init__(self, path: Path):
        # A run killed mid-write leaves a line with no newline; the next record must not join it.
        unterminated = False
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as existing:
                existing.seek(-1, 2)
                unterminated = existing.read(1) != b"\n"
        self._fh = path.open("a", encoding="utf-8")
        if unterminated:
            self._fh.write("\n")
            self._fh.flush()
        self._lock = threading.Lock()
        self._closed = False

    def write(self, rec: dict) -> None:
        line = json.dumps(rec, separators=(",", ":"))
        with self._lock:
            if self._closed:
                return
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._fh.close()


class StatsPoller(threading.Thread):
    """`<engine> stats --no-stream` (every container) plus the host CPU, off the main loop."""

    def __init__(self, min_interval: float, sink: Sink, halt: threading.Event):
        super().__init__(daemon=True)
        self.min_interval, self.sink, self.halt = min_interval, sink, halt
        self.snapshots = 0
        self.last_top: tuple[str, float] | None = None
        self.last_call_s: float | None = None

    def run(self) -> None:
        while not self.halt.is_set():
            t0 = time.monotonic()
            rec: dict = {"kind": "ctr"}
            try:
                rec["containers"] = read_containers()
            except Exception as exc:  # noqa: BLE001
                rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
            rec["call_s"] = round(time.monotonic() - t0, 2)
            now = time.time()
            rec.update(epoch=now, hms=_hms(now), host_cpu_pct=read_host_cpu())
            self.sink.write(rec)
            self.snapshots += 1
            self.last_call_s = rec["call_s"]
            cpus = [(c["n"], c["cpu"]) for c in rec.get("containers", []) if c["cpu"] is not None]
            self.last_top = max(cpus, key=lambda kv: kv[1]) if cpus else None
            self.halt.wait(max(0.0, self.min_interval - (time.monotonic() - t0)))


class ProbePoller(threading.Thread):
    """Runs one configured probe command at its interval; one `probe` record per run."""

    def __init__(self, name: str, cmd: str, interval: float, timeout: float, slow_s: float,
                 env: dict, cwd: str | None, sink: Sink, halt: threading.Event):
        super().__init__(daemon=True)
        self.name, self.cmd, self.interval, self.timeout, self.slow_s = name, cmd, interval, timeout, slow_s
        self.env, self.cwd, self.sink, self.halt = env, cwd, sink, halt
        self.runs = self.failures = self.slow = 0

    def run(self) -> None:
        while not self.halt.is_set():
            t0 = time.monotonic()
            rec: dict = {"kind": "probe", "name": self.name}
            try:
                r = subprocess.run(self.cmd, shell=True, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=self.timeout, env=self.env, cwd=self.cwd)
                rec.update(exit=r.returncode, out=((r.stdout or "") + (r.stderr or "")).strip()[-300:])
            except subprocess.TimeoutExpired:
                rec.update(exit=None, error=f"timed out after {self.timeout:g}s")
            except OSError as exc:
                rec.update(exit=None, error=str(exc)[:200])
            dur = round(time.monotonic() - t0, 3)
            now = time.time()
            ok = rec.get("exit") == 0
            slow = ok and dur > self.slow_s
            rec.update(epoch=now, hms=_hms(now), dur_s=dur, ok=ok, slow=slow, slow_s=self.slow_s)
            self.sink.write(rec)
            self.runs += 1
            self.failures += 0 if ok else 1
            self.slow += 1 if slow else 0
            self.halt.wait(max(0.0, self.interval - (time.monotonic() - t0)))


def configured_probes(ctx: dict) -> list[dict]:
    out = []
    for i, p in enumerate(wfconfig.get("sampler.probes", []) or []):
        if not isinstance(p, dict) or not str(p.get("cmd", "")).strip():
            continue
        out.append({"name": str(p.get("name") or f"probe-{i + 1}"),
                    "cmd": wfconfig.render(str(p["cmd"]), **ctx),
                    "interval": float(p.get("interval_s", 1.0) or 1.0),
                    "timeout": float(p.get("timeout_s", 10.0) or 10.0),
                    "slow_s": float(p.get("slow_s", 1.0) or 1.0)})
    return out


def _ctx(stack: str | None, story_id: str | None) -> tuple[dict, dict, str | None]:
    """(render context, env, cwd) for probe commands."""
    if story_id or stack:
        try:
            from run_scoped_tests import resolve_target, run_env
            ctx, err = resolve_target(story_id or None, stack)
            if not err:
                return ctx, run_env(ctx), str(ctx.get("worktree") or "") or None
        except Exception:  # noqa: BLE001 - probes then render without slot values
            pass
    return ({"stack": stack or "", "slot": 0, "worktree": str(wfconfig.repo_root())},
            dict(os.environ), str(wfconfig.repo_root()))


def cmd_sample(a: argparse.Namespace) -> int:
    for flag, value in (("--interval", a.interval), ("--stats-interval", a.stats_interval),
                        ("--max-seconds", a.max_seconds)):
        if value <= 0:
            print(f"{flag} must be > 0 (got {value})", file=sys.stderr)
            return 2
    if a.interval > 15:
        print("--interval must be <= 15 s", file=sys.stderr)
        return 2
    if a.stop_file and Path(a.stop_file).exists():
        print(f"--stop-file {a.stop_file} already exists: the sampler would stop at once with no "
              "samples; remove it first", file=sys.stderr)
        return 2
    ctx, env, cwd = _ctx(a.stack, a.story_id)
    stack = a.stack or (str(ctx.get("stack") or "") if a.story_id else "") or None
    ref = None
    if not a.local:
        try:
            ref = pick_reference(stack)
        except NoReferenceContainer as exc:
            print(f"--stack {stack}: {exc}", file=sys.stderr)
            return 2
        except Exception:  # noqa: BLE001 - no engine: read the host itself
            ref = None
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sink = Sink(out)
    halt = threading.Event()
    stats = None if a.no_stats else StatsPoller(a.stats_interval, sink, halt)
    probes = [] if a.no_probes else [
        ProbePoller(p["name"], p["cmd"], p["interval"], p["timeout"], p["slow_s"], env, cwd, sink, halt)
        for p in configured_probes(ctx)]
    sink.write({"kind": "start", "epoch": time.time(), "hms": _hms(), "label": a.label, "stack": stack,
                "interval": a.interval, "stats_interval": a.stats_interval, "ref": ref or "local",
                "probes": [p.name for p in probes]})
    print(f"[{_hms()}] sampler start: out={out} ref={ref or 'local'} interval={a.interval}s "
          f"stats={'off' if stats is None else f'>={a.stats_interval}s'} "
          f"probes={','.join(p.name for p in probes) or 'none'} hard-stop={a.max_seconds}s", flush=True)
    for t in ([stats] if stats else []) + probes:
        t.start()

    deadline = time.monotonic() + a.max_seconds
    prev_cpu, n, reason = None, 0, "max-seconds"
    try:
        while True:
            if a.stop_file and Path(a.stop_file).exists():
                reason = "stop-file"
                break
            if time.monotonic() >= deadline:
                break
            t0 = time.monotonic()
            remaining = max(1.0, deadline - t0)
            epoch = time.time()
            rec: dict = {"kind": "sample", "epoch": epoch, "hms": _hms(epoch)}
            try:
                if ref is None and not a.local and stack:
                    ref = pick_reference(stack, timeout=min(20.0, remaining))
                vm = read_vm(ref, timeout=min(12.0, remaining))
                cpu = vm.pop("cpu")
                rec.update(vm, ref=ref or "local")
                if prev_cpu is not None:
                    rec.update(cpu_split(prev_cpu, cpu, vm["ncpu"]))
                prev_cpu = cpu
            except Exception as exc:  # noqa: BLE001 - the reference container may have been recreated
                rec["vm_error"] = f"{type(exc).__name__}: {exc}"[:200]
                ref, prev_cpu = None, None
            rec["probe_runs"] = {p.name: [p.runs, p.failures, p.slow] for p in probes}
            rec["ctr_snapshots"] = stats.snapshots if stats else 0
            rec["iter_s"] = round(time.monotonic() - t0, 2)
            sink.write(rec)
            n += 1
            top = stats.last_top if stats else None
            print(f"[{_hms()}] #{n} load1={rec.get('load1')} busy={rec.get('busy_cores')} cores "
                  f"iowait={rec.get('cpu_iowait_pct')}% run={rec.get('procs_running')} "
                  f"blocked={rec.get('procs_blocked')} "
                  f"top={top[0] + ':' + str(top[1]) + '%' if top else '-'} "
                  + " ".join(f"{p.name}={p.runs}/{p.failures}f/{p.slow}s" for p in probes)
                  + f" iter={rec['iter_s']}s", flush=True)
            now = time.monotonic()
            if now >= deadline:
                break
            time.sleep(max(0.0, min(a.interval - (now - t0), deadline - now)))
    except KeyboardInterrupt:
        reason = "interrupt"
    finally:
        halt.set()
        # A poller mid-call writes its record when the call returns; joining keeps it ahead of `stop`.
        for t in ([stats] if stats else []) + probes:
            t.join(timeout=POLLER_JOIN_S)
        sink.write({"kind": "stop", "epoch": time.time(), "hms": _hms(), "reason": reason, "samples": n})
        print(f"[{_hms()}] sampler stop ({reason}): {n} samples", flush=True)
        sink.close()
    return 0


def _p95(values: list[float]) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))] if s else 0.0


def cmd_summarize(a: argparse.Namespace) -> int:
    lines = [ln for ln in Path(a.file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    recs: list[dict] = []
    skipped = run = 0
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            rec = None
        if not isinstance(rec, dict) or "kind" not in rec:
            skipped += 1
            continue
        if rec["kind"] == "start":
            run += 1
        rec["_run"] = run
        recs.append(rec)
    if skipped:
        print(f"skipped {skipped} unparseable line(s): a sampler still writing, or killed mid-write")
    samples = [r for r in recs if r["kind"] == "sample"]
    if not samples:
        print("no samples")
        return 1
    # Runs appended to one file are separate; the default window covers the last one only.
    starts = [r["epoch"] for r in recs if r["kind"] == "start"]
    run_lo = starts[-1] if starts else samples[0]["epoch"]
    last = recs[-1]["epoch"]

    def at(hms: str, on) -> float:
        return datetime.combine(on, datetime.strptime(hms, "%H:%M:%S").time()).timestamp()

    # Clock times carry no date: --from is its latest occurrence at or before the last record, and a
    # --to earlier than --from is the next day.
    last_day = datetime.fromtimestamp(last).date()
    lo = run_lo
    if a.from_:
        lo = at(a.from_, last_day)
        if lo > last:
            lo = at(a.from_, last_day - timedelta(days=1))
    lo_day = datetime.fromtimestamp(lo).date()
    hi = at(a.to, lo_day) if a.to else max(r["epoch"] for r in recs)
    if a.to and hi < lo:
        hi = at(a.to, lo_day + timedelta(days=1))

    def inside(r: dict) -> bool:
        return lo <= r["epoch"] <= hi

    win = [s for s in samples if inside(s)]
    ctrs = [r for r in recs if r["kind"] == "ctr" and inside(r) and r.get("containers")]
    probes = [r for r in recs if r["kind"] == "probe" and inside(r)]
    if not win:
        print("no samples inside the window")
        return 1
    over = False
    gaps = [b["epoch"] - x["epoch"] for x, b in zip(win, win[1:])]
    print(f"== {a.file}  window {_stamp(lo)} .. {_stamp(hi)}  {len(win)} samples "
          f"(median interval {statistics.median(gaps) if gaps else 0:.1f}s, max gap {max(gaps, default=0):.1f}s), "
          f"{len(ctrs)} container snapshots, {len(probes)} probe runs")

    budget = float(wfconfig.get("sampler.load_budget", 1.0) or 1.0)
    loads = [(s["load1"], s["hms"], s.get("ncpu") or 1) for s in win if s.get("load1") is not None]
    if loads:
        peak = max(loads)
        above = sum(v > budget * nc for v, _, nc in loads)
        over |= above > 0
        print(f"VM loadavg1   peak {peak[0]:.2f} @ {peak[1]}   mean {statistics.mean(v for v, _, _ in loads):.2f}   "
              f"over budget ({budget:g} x {loads[-1][2]} CPUs): {above} of {len(loads)}"
              f"{'  <-- OVER BUDGET' if above else ''}")
    busy = [s["busy_cores"] for s in win if s.get("busy_cores") is not None]
    iow = [s["cpu_iowait_pct"] for s in win if s.get("cpu_iowait_pct") is not None]
    if busy:
        print(f"VM CPU        busy mean {statistics.mean(busy):.1f} cores of {win[-1].get('ncpu')} "
              f"(peak {max(busy):.1f});  iowait mean {statistics.mean(iow):.2f}% (peak {max(iow):.2f}%)")
    pr = [s["procs_running"] for s in win if s.get("procs_running") is not None]
    pb = [s["procs_blocked"] for s in win if s.get("procs_blocked") is not None]
    if pr:
        print(f"tasks         procs_running mean {statistics.mean(pr):.1f} peak {max(pr)};  "
              f"procs_blocked mean {statistics.mean(pb):.1f} peak {max(pb)}")
    host = [r["host_cpu_pct"] for r in ctrs if r.get("host_cpu_pct") is not None]
    if host:
        print(f"host          total CPU mean {statistics.mean(host):.0f}% peak {max(host):.0f}%")

    per_ctr: dict[str, list[float]] = defaultdict(list)
    blk: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    blk_prev: dict[tuple[str, int], int] = {}
    agg_by_snapshot: list[dict[str, float]] = []
    for r in ctrs:
        agg: dict[str, float] = defaultdict(float)
        for c in r["containers"]:
            if c["cpu"] is None:
                continue
            agg[stack_of(c["n"])] += c["cpu"] / 100.0
            per_ctr[c["n"]].append(c["cpu"] / 100.0)
        agg_by_snapshot.append(agg)
    # Block I/O is cumulative since container start: a drop is a restart (the new reading is then
    # everything since it); a container first seen mid-window only sets a baseline.
    for r in ctrs:
        for c in r["containers"]:
            for slot, key in enumerate(("br", "bw")):
                cur = c.get(key)
                if cur is None:
                    continue
                prev = blk_prev.get((c["n"], slot))
                if prev is not None:
                    blk[c["n"]][slot] += cur - prev if cur >= prev else cur
                blk_prev[(c["n"], slot)] = cur
    if ctrs:
        stacks = sorted({k for agg in agg_by_snapshot for k in agg})
        per_stack = {k: [agg.get(k, 0.0) for agg in agg_by_snapshot] for k in stacks}
        print("per stack (cores = sum of container CPU%/100 per snapshot):")
        for k, v in sorted(per_stack.items(), key=lambda kv: -statistics.mean(kv[1])):
            tag = "  <-- this stack" if a.stack == k else ""
            print(f"    {k:24s} mean {statistics.mean(v):6.2f}  peak {max(v):6.2f}{tag}")
        if a.stack:
            others = [sum(v for k, v in agg.items() if k != a.stack) for agg in agg_by_snapshot]
            print(f"other stacks combined: mean {statistics.mean(others):.2f} cores, peak {max(others):.2f}")
        print("top containers by mean cores (block I/O over the window, MB read / written):")
        for name, v in sorted(per_ctr.items(), key=lambda kv: -statistics.mean(kv[1]))[:12]:
            b = blk[name]
            print(f"    {name:32s} mean {statistics.mean(v):6.2f}  peak {max(v):6.2f}   "
                  f"blk r {b[0] / 1e6:9.1f}  w {b[1] / 1e6:9.1f}")

    by_probe: dict[str, list[dict]] = defaultdict(list)
    for p in probes:
        by_probe[p["name"]].append(p)
    for name, rows in sorted(by_probe.items()):
        durs = [r["dur_s"] for r in rows if r.get("ok")]
        fails = [r for r in rows if not r.get("ok")]
        slow = [r for r in rows if r.get("slow")]
        over |= bool(fails or slow)
        print(f"probe {name:16s} runs {len(rows)}  failures {len(fails)}  slow {len(slow)} "
              f"(> {rows[0].get('slow_s', '?')}s)  p95 {_p95(durs):.3f}s  max {max(durs, default=0):.3f}s"
              f"{'  <-- OVER BUDGET' if fails or slow else ''}")
        if fails:
            f = fails[0]
            why = f.get("error") or f.get("out") or f"exit {f.get('exit')}"
            print(f"    first failure {f['hms']}: {why[:160]}")
        if slow:
            s = max(slow, key=lambda r: r["dur_s"])
            print(f"    slowest {s['hms']}: {s['dur_s']:.3f}s {(s.get('out') or '')[:120]!r}")
    print(f"budget: {'OVER' if over else 'within'}")
    return 1 if (a.check and over) else 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="record samples until --max-seconds, --stop-file or Ctrl-C")
    s.add_argument("--out", required=True, help="JSONL file (appended)")
    s.add_argument("--stack", help="compose project whose container is the VM-read reference")
    s.add_argument("--story-id", help="resolve the stack (and probe template values) from the slot registry")
    s.add_argument("--label", default="")
    s.add_argument("--interval", type=float, default=10.0, help="seconds between VM samples (<= 15)")
    s.add_argument("--stats-interval", type=float, default=10.0,
                   help="minimum seconds between container-stats snapshots")
    s.add_argument("--max-seconds", type=float, default=3600.0, help="hard stop")
    s.add_argument("--stop-file", help="exit cleanly once this file exists")
    s.add_argument("--no-stats", action="store_true", help="no container-stats thread")
    s.add_argument("--no-probes", action="store_true", help="no config: sampler.probes threads (cost comparison)")
    s.add_argument("--local", action="store_true", help="read the host's own /proc (or psutil), not a container")
    s.set_defaults(fn=cmd_sample)
    m = sub.add_parser("summarize", help="load, CPU split, per-stack cores, probe budgets")
    m.add_argument("file")
    m.add_argument("--from", dest="from_", help="window start, local HH:MM:SS")
    m.add_argument("--to", help="window end, local HH:MM:SS")
    m.add_argument("--stack", help="stack to single out; the rest are reported as 'other stacks'")
    m.add_argument("--check", action="store_true", help="exit 1 when any budget is exceeded")
    m.set_defaults(fn=cmd_summarize)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
