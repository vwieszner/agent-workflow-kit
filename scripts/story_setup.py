#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Phase 1 of the parallel-dev-wave pipeline -- async story setup.

Does the fast deterministic prep (slot reservation, worktree creation, spec
lookup, graph indexing) synchronously, then launches the slow stack bring-up as
a detached background process (bring_up_stack_async.py) and returns
immediately, so the orchestrator can overlap the bring-up with Checkpoint 1
spec review.

Background bring-up status is tracked in `.workflow/state/story-setup/`:
  <stack>.json          -- {status: starting|up|failed, ...} (REWRITTEN with a
                           fixed key set by bring_up_stack_async.py on every
                           update -- carries no extra keys)
  <stack>.log           -- compose stdout+stderr + heartbeat lines
  <stack>.graph_project -- code-graph project name for the worktree (single
                           line; written once here, read by cleanup at teardown
                           -- a sidecar BECAUSE the status JSON gets rewritten)

Steps (all synchronous, all fast):
  1. Reserve a slot via reserve_slot.py (mutex-protected).
  2. Compute the slot's host ports (wfconfig.slot_ports).
  3. Create the worktree off `config: git.base_branch` on branch
     `config: git.branch_prefix`<id> (reused if it already exists), then seed
     each `config: git.worktree_seed_files` entry from the repo root. Those files
     are gitignored (e.g. `.env`), so a fresh worktree has none, and compose
     resolves `.env` from its CWD -- the worktree -- so every `${VAR:-default}`
     would otherwise silently take its default in the slot stack. An existing
     copy in the worktree is left alone.
  4. Locate the story spec under <worktree>/`config: paths.specs_dir` (exit 3 on
     ambiguity -- before any graph/status artifacts are created).
  5. Index the worktree into the code graph and capture the returned project
     name -- never compute the slug. Skipped when `config: graph.tool = "none"`.
  6. Initialize per-stack status + log + graph_project files.
  7. Launch bring_up_stack_async.py as a detached background process.
     Skipped when `config: stack.runtime = "none"` (worktree-only slot).
  8. Emit a structured `key: value` report on stdout and exit 0.

Exit codes:
  0 -- success (reservation + worktree + background bring-up launched or skipped)
  1 -- error (reserve / worktree / launch failure)
  2 -- no free slot
  3 -- spec ambiguity (multiple candidate spec files)

Invoked by the parallel-dev-wave skill (Phase 1), from the repo root:
  uv run --no-project .workflow/scripts/story_setup.py <story_id>

Pairs with:
  .workflow/scripts/bring_up_stack_async.py   -- runs in the background
  .workflow/scripts/wait_for_stack_ready.py   -- orchestrator-side poll before preflight
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402

# Pipeline files that share the story-id prefix in the specs dir but are not the
# spec. Keep in sync with the NON_SPEC_* lists in story_ledger.py.
NON_SPEC_SUFFIXES = ("-record", "-auto-triage", "-ledger")
NON_SPEC_INFIXES = ("code-review-findings",)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def fail(msg: str, code: int = 1) -> NoReturn:
    print(f"\nstory-setup FAILED: {msg}")
    sys.exit(code)


def graph_cli() -> Path | None:
    raw = str(wfconfig.get("graph.cli", "") or "")
    return Path(os.path.expanduser(raw)) if raw else None


def index_worktree(worktree: Path) -> str:
    """Index the worktree; return the authoritative project name ('' on failure/skip)."""
    tool = str(wfconfig.get("graph.tool", "none"))
    if tool == "none":
        _stack.skipped("graph indexing", 'config: graph.tool = "none" (navigation uses grep/read)')
        return ""
    if tool != "codebase-memory-mcp":
        print(f"  WARNING: graph.tool = {tool!r} has no indexing adapter in story_setup.py -- "
              "worktree not indexed")
        return ""
    cli = graph_cli()
    if cli is None or not cli.is_file():
        print(f"  WARNING: graph CLI not found at {cli or '<config: graph.cli is empty>'} -- "
              "worktree not indexed")
        return ""
    try:
        idx = subprocess.run(
            [str(cli), "cli", "index_repository", "--repo-path", worktree.as_posix()],
            capture_output=True, text=True, errors="replace", timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  WARNING: index_repository failed to run: {e}")
        return ""
    for line in reversed(idx.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                out = json.loads(line)
            except json.JSONDecodeError:
                continue
            if out.get("project"):
                print(f"  graph_project={out['project']} "
                      f"({out.get('nodes', '?')} nodes / {out.get('edges', '?')} edges)")
                return out["project"]
            break
    print("  WARNING: indexing did not return a project name -- the preflight graph "
          "assertion will fail until index_repository is run manually for "
          f"{worktree}. stderr: {idx.stderr.strip()[:300]}")
    return ""


def _ps_quote(arg: str) -> str:
    """One Start-Process ArgumentList element: double-quoted for the child's argv,
    single-quoted (with '' escaping) for PowerShell itself."""
    return "'" + ('"' + arg + '"').replace("'", "''") + "'"


def launch_detached(argv: list[str], cwd: Path, env: dict) -> str:
    """Start argv detached from this process; return its PID ('' on failure).

    POSIX: new session, no stdio. Windows: PowerShell Start-Process (a plain
    Popen with DETACHED_PROCESS does not reliably survive the caller there).
    """
    if os.name == "nt":
        launcher = (
            "$bgArgs = @(" + ",".join(_ps_quote(a) for a in argv[1:]) + "); "
            f"$p = Start-Process -FilePath {_ps_quote(argv[0])} -ArgumentList $bgArgs "
            f"-WorkingDirectory {_ps_quote(str(cwd))} -WindowStyle Hidden -PassThru; "
            "Write-Output $p.Id"
        )
        lp = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-Command", launcher], capture_output=True, text=True, env=env)
        pid = lp.stdout.strip().splitlines()[-1] if lp.stdout.strip() else ""
        if lp.returncode != 0 or not pid.isdigit():
            print(f"  launcher rc={lp.returncode} out={lp.stdout!r} err={lp.stderr!r}")
            return ""
        return pid
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    except OSError as e:
        print(f"  launch failed: {e}")
        return ""
    return str(p.pid)


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue] — keep progress ordered around child output
    ap = argparse.ArgumentParser(description="parallel-dev-wave Phase 1 story setup")
    ap.add_argument("story_id", help="story id -- dotted (7.5.8) or dashed (21-3)")
    ap.add_argument("--repo-root", default=None, help="default: wfconfig repo root")
    args = ap.parse_args()
    repo_root = _stack.set_repo_root(args.repo_root)
    child_env = dict(os.environ, WORKFLOW_REPO_ROOT=str(repo_root))
    here = _stack.scripts_dir()

    # ------------------------------------------------------------------ 1) slot
    print(f"story-setup: reserving slot for {args.story_id} ...")
    reserve = here / "reserve_slot.py"
    if not reserve.is_file():
        fail(f"reserve_slot.py not found at {reserve}")
    proc = subprocess.run([sys.executable, str(reserve), "--story-id", args.story_id],
                          capture_output=True, text=True, env=child_env)
    json_lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    if not json_lines:
        fail(f"reserve_slot.py produced no JSON. Raw output: {proc.stdout!r} {proc.stderr!r}")
    try:
        r = json.loads(json_lines[-1])
    except json.JSONDecodeError:
        fail(f"could not parse reserve_slot.py output: {json_lines[-1]!r}")

    status = r.get("status")
    if status == "reserved":
        pass
    elif status == "already_reserved":
        print(f"  story already reserved in slot {r['slot']} -- reusing")
    elif status == "no_free_slot":
        fail(f"all {wfconfig.get('slots.count', 6)} slots are in_use", 2)
    else:
        fail(f"reserve_slot.py error: {r.get('message')}")

    slot = int(r["slot"])
    sid = r["branch_name"]
    branch = r["branch"]
    worktree = Path(r["worktree_path"])
    stack = r["stack_project_name"]
    already_reserved = status == "already_reserved"
    print(f"  slot={slot} branch={branch} stack={stack}")

    # ----------------------------------------------------------------- 2) ports
    ports = wfconfig.slot_ports(slot)
    ports_line = " ".join(f"{k}={v}" for k, v in ports.items()) or "<none declared>"

    # -------------------------------------------------------------- 3) worktree
    base = str(wfconfig.get("git.base_branch", "development"))
    if worktree.exists():
        print(f"story-setup: worktree already exists at {worktree} -- reusing")
    else:
        print(f"story-setup: creating worktree at {worktree} off {base} ...")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        wt = subprocess.run(["git", "-C", str(repo_root), "worktree", "add",
                             "-b", branch, str(worktree), base])
        if wt.returncode != 0 or not worktree.exists():
            fail(f"git worktree add failed (exit {wt.returncode})")

    for rel in wfconfig.get("git.worktree_seed_files", [".env"]) or []:
        src, dst = repo_root / rel, worktree / rel
        if dst.exists():
            print(f"story-setup: worktree {rel} already present -- left as-is")
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            print(f"story-setup: seeded worktree {rel} from {src}")
        else:
            print(f"  WARNING: no {rel} at {src} -- the slot runs without it")

    # ------------------------------------------------------------------ 4) spec
    dashed, dotted = sid, sid.replace("-", ".")
    spec_dir = worktree / str(wfconfig.get("paths.specs_dir", "docs/implementation-artifacts"))
    spec_path, spec_present = "", False
    pattern = re.compile(rf"^({re.escape(dashed)}|{re.escape(dotted)})(-|\.|$)")

    def is_spec(p: Path) -> bool:
        stem = p.stem.lower()
        if any(x in stem for x in NON_SPEC_INFIXES):
            return False
        return not any(stem.endswith(sfx) for sfx in NON_SPEC_SUFFIXES)

    candidates = (
        sorted(p for p in spec_dir.glob("*.md")
               if p.is_file() and pattern.match(p.stem) and is_spec(p))
        if spec_dir.is_dir() else []
    )
    if len(candidates) == 1:
        spec_path, spec_present = str(candidates[0]), True
    elif len(candidates) > 1:
        print("\nstory-setup: MULTIPLE spec candidates -- orchestrator must disambiguate:")
        for c in candidates:
            print(f"  {c}")
        sys.exit(3)

    # ----------------------------------------------------------- 5) graph index
    print("story-setup: indexing worktree into the code graph ...")
    graph_project = index_worktree(worktree)

    # ----------------------------------------------------- 6) status/log files
    status_dir = wfconfig.state_dir("story-setup")
    status_file = status_dir / f"{stack}.json"
    log_file = status_dir / f"{stack}.log"
    graph_file = status_dir / f"{stack}.graph_project"
    graph_file.write_text(graph_project + "\n", encoding="utf-8")

    # ------------------------------------------------------ 7) detached launch
    bg_pid = ""
    if not wfconfig.stack_enabled():
        _stack.skipped("stack bring-up", _stack.STACK_NONE_REASON)
        stack_status = "skipped (stack.runtime = none)"
    else:
        init_status = {
            "status": "starting", "stack": stack,
            "started_at": now_iso(), "updated_at": now_iso(),
            "log_path": str(log_file),
        }
        status_file.write_text(json.dumps(init_status), encoding="utf-8")
        log_file.write_text(
            f"[{now_iso()}] story-setup queued background bring-up for {stack}\n",
            encoding="utf-8")
        print(f"story-setup: launching background bring-up (project {stack}) ...")
        async_script = here / "bring_up_stack_async.py"
        if not async_script.is_file():
            fail(f"bring_up_stack_async.py not found at {async_script}")
        bg_pid = launch_detached(
            [sys.executable, str(async_script),
             "--stack", stack, "--worktree", str(worktree),
             "--status-file", str(status_file), "--log-file", str(log_file),
             "--slot", str(slot)],
            cwd=repo_root, env=child_env)
        if not bg_pid:
            fail("failed to launch bring_up_stack_async.py")
        print(f"  background PID={bg_pid} -- log: {log_file}")
        stack_status = "starting (background)"

    # ---------------------------------------------------------------- 8) report
    print()
    print("===== story-setup launched (async bring-up running) ====="
          if bg_pid else "===== story-setup complete (no stack bring-up) =====")
    print(f"slot:               {slot}")
    print(f"story_id:           {args.story_id}")
    print(f"branch:             {branch}")
    print(f"worktree_path:      {worktree}")
    print(f"stack_project_name: {stack}")
    print(f"graph_project:      {graph_project if graph_project else '<indexing failed or skipped>'}")
    print(f"ports:              {ports_line}")
    print(f"stack_status:       {stack_status}")
    print(f"stack_status_file:  {status_file if bg_pid else '<none>'}")
    print(f"stack_log_file:     {log_file if bg_pid else '<none>'}")
    print(f"background_pid:     {bg_pid or '<none>'}")
    print(f"spec_path:          {spec_path if spec_present else '<not found>'}")
    print(f"spec_present:       {str(spec_present).lower()}")
    print(f"already_reserved:   {str(already_reserved).lower()}")
    print()
    print("==> Orchestrator: proceed to Checkpoint 1 (spec review).")
    print(f"==> Before Phase 2, run: uv run --no-project .workflow/scripts/wait_for_stack_ready.py {stack}")
    sys.exit(0)


if __name__ == "__main__":
    main()
