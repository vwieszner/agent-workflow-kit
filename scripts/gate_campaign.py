#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Run a K-consecutive full-suite campaign against a story stack and stop at the first bad run.

Each run is one `.workflow/scripts/run_full_suite.py --story-id <id>`. After every run the script
keeps the console output, the gate's raw log (the `log:` / `raw log:` path the run printed) and
every `config: tests.artifact_dirs` directory (worktree-relative; the next run would overwrite
them) in the campaign directory, scans the stack's `config: smoke.services` container logs since
the run started for each `--forbid` pattern, and appends one JSON record to `campaign.jsonl`. A log
fetch that times out or exits non-zero counts as a hit, so an unread log never passes as clean.

Stop rules, checked in this order (exit code in brackets):
  red gate (run_full_suite exit != 0) [1]; wall above --max-wall-s [2]; a --forbid hit [3].
All K runs good: exit 0. A usage or setup error (bad arguments, invalid --forbid regex, story not
in the slot registry) exits 4 before any run. Campaign-level judgements (median walls, leg counts)
are read from campaign.jsonl by the caller.

Usage
-----
    uv run --no-project .workflow/scripts/gate_campaign.py run --story-id <id> --runs 5 \
        [--max-wall-s 800] [--forbid "Traceback"] [--out <dir>] [-- <extra run_full_suite.py args>]
    uv run --no-project .workflow/scripts/gate_campaign.py status --out <dir>

`run` blocks for K full gates: launch it in the background and wait for its completion. `status` is
a one-shot read of the campaign directory: the finished records plus the run in progress.

When to use: a story or spec asks for K consecutive green gates, or a gate-timing measurement needs
several full runs with per-run walls and preserved logs. Stack logs need `config: stack.runtime`
!= "none"; without a stack, --forbid is refused.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _registry_lookup  # noqa: E402
import _stack  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
LOG_PATH = re.compile(r"(?:raw log|log):\s+(\S+\.log)")
SETUP_ERROR = 4  # distinct from the stop-rule codes 1-3, so a setup error never reads as a red gate


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse's own exit 2 would read as "wall over budget"
        self.print_usage(sys.stderr)
        self.exit(SETUP_ERROR, f"{self.prog}: error: {message}\n")


def _worktree(story_id: str) -> Path:
    row = _registry_lookup.find(story_id)
    if not row or not row.get("worktree_path"):
        print(f"ERROR: story-id '{story_id}' has no worktree in {_registry_lookup.registry_path()}", file=sys.stderr)
        sys.exit(SETUP_ERROR)
    return Path(row["worktree_path"])


def forbid_hits(project: str, since_iso: str, patterns: list[str]) -> dict[str, int]:
    if not patterns:
        return {}
    services = [str(s) for s in (wfconfig.get("smoke.services", []) or [])] or _stack.expected_services()
    try:
        proc = subprocess.run(_stack.compose_argv(project, "logs", "--no-color", "--since", since_iso, *services),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                              env=_stack.env_for_stack(project))
    except subprocess.TimeoutExpired:
        return {"<compose logs timed out>": 1}  # a frozen log stream must not pass as "no hits"
    except OSError as exc:
        return {f"<compose logs could not run: {exc}>": 1}
    if proc.returncode != 0:
        return {f"<compose logs exit {proc.returncode}>": 1}
    return {p: len(re.findall(p, proc.stdout)) for p in patterns}


def copy_artifacts(out_dir: Path, label: str, console: str, worktree: Path) -> list[str]:
    kept = []
    for src in dict.fromkeys(LOG_PATH.findall(console)):
        path = Path(src)
        if path.is_file():
            dst = out_dir / f"{label}.{path.name}"
            shutil.copy2(path, dst)
            kept.append(dst.name)
    for rel in wfconfig.get("tests.artifact_dirs", []) or []:
        src = worktree / str(rel)
        if src.is_dir():
            name = f"art-{label}-{str(rel).strip('/').replace('/', '_')}"
            shutil.copytree(src, out_dir / name, dirs_exist_ok=True)
            kept.append(name + "/")
    return kept


def cmd_run(args: argparse.Namespace) -> int:
    if args.runs < 1:
        print(f"ERROR: --runs must be >= 1, got {args.runs}", file=sys.stderr)
        return SETUP_ERROR
    for p in args.forbid:
        try:
            re.compile(p)
        except re.error as exc:
            print(f"ERROR: --forbid {p!r} is not a valid regex: {exc}", file=sys.stderr)
            return SETUP_ERROR
    if args.forbid and not wfconfig.stack_enabled():
        print("ERROR: --forbid scans stack logs, and config: stack.runtime is \"none\"", file=sys.stderr)
        return SETUP_ERROR
    worktree = _worktree(args.story_id)
    project = wfconfig.stack_name(args.story_id)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out or Path(tempfile.gettempdir()) / f"gate-campaign-{args.story_id}-{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = out_dir / "campaign.jsonl"
    running = out_dir / "running.json"
    extra = [a for a in args.extra if a != "--"]
    code_root = str(wfconfig.get("tests.code_root", ".") or ".").strip("/")
    print(f"campaign dir: {out_dir}", flush=True)

    for i in range(1, args.runs + 1):
        label = f"run{i}"
        started = datetime.now(timezone.utc)
        running.write_text(json.dumps({"label": label, "started": started.isoformat()}), encoding="utf-8")
        print(f"[{datetime.now():%H:%M:%S}] {label}/{args.runs} start", flush=True)
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "run_full_suite.py"), "--story-id", args.story_id,
                               *extra], cwd=worktree, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        wall = round(time.monotonic() - t0)
        console = proc.stdout + proc.stderr
        (out_dir / f"{label}.out").write_text(console, encoding="utf-8")
        hits = forbid_hits(project, started.isoformat(), args.forbid)
        ref = "HEAD" if code_root in ("", ".") else f"HEAD:{code_root}"
        tree = subprocess.run(["git", "-C", str(worktree), "rev-parse", ref], capture_output=True, text=True)
        record = {"label": label, "started": started.isoformat(), "rc": proc.returncode, "wall_s": wall,
                  "tree": tree.stdout.strip()[:12], "forbid_hits": hits,
                  "kept": copy_artifacts(out_dir, label, console, worktree)}
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        running.unlink(missing_ok=True)

        stop = None
        if proc.returncode != 0:
            stop = (1, f"red gate rc={proc.returncode}")
        elif args.max_wall_s and wall > args.max_wall_s:
            stop = (2, f"wall {wall}s > --max-wall-s {args.max_wall_s}")
        elif any(hits.values()):
            stop = (3, f"forbidden log lines {hits}")
        print(f"[{datetime.now():%H:%M:%S}] {label} rc={proc.returncode} wall={wall}s "
              f"{'STOP: ' + stop[1] if stop else 'ok'}", flush=True)
        if stop:
            return stop[0]
    print(f"ALLDONE {args.runs}/{args.runs} good -> {ledger}", flush=True)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    ledger = out_dir / "campaign.jsonl"
    for line in (ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []):
        r = json.loads(line)
        print(f"{r['label']} rc={r['rc']} wall={r['wall_s']}s tree={r['tree']} forbid={r['forbid_hits']}")
    running = out_dir / "running.json"
    if running.exists():
        r = json.loads(running.read_text(encoding="utf-8"))
        age = int((datetime.now(timezone.utc) - datetime.fromisoformat(r["started"])).total_seconds())
        print(f"[{datetime.now():%H:%M:%S}] in progress: {r['label']} for {age}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    ap = _Parser(description="K-consecutive full-suite campaign")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--story-id", required=True)
    r.add_argument("--runs", type=int, required=True)
    r.add_argument("--max-wall-s", type=int, default=0)
    r.add_argument("--forbid", action="append", default=[], help="regex; repeatable")
    r.add_argument("--out", default="")
    r.add_argument("extra", nargs=argparse.REMAINDER, help="after --: passed to run_full_suite.py")
    s = sub.add_parser("status")
    s.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    return cmd_run(args) if args.cmd == "run" else cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
