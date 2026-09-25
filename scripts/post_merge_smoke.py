#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Post-merge smoke check on the shared stack (slot `config: slots.shared_slot`).

Confirms the shared dev stack (`config: slots.shared_stack_name`) is healthy
after a merge lands on `config: git.base_branch`:
  1. every smoke service (`config: smoke.services`, else `config: stack.services`)
     is `running`;
  2. none of their recent log lines (`--tail config: smoke.log_tail`, `--since
     config: smoke.log_since`) match `config: smoke.log_error_patterns`;
  3. source-sync gate — the RUNNING code is the MERGED code: the files the most
     recent merge brought in (its first-parent diff, limited to the sync mounts
     and `config: smoke.sync_exts`) are hash-compared against the containers via
     check_container_sync.py. A container reports `running` with clean logs
     while executing stale code when `compose watch` has died silently; state
     and log checks cannot see that.

On failure, prints diagnoses: E (stale source), the configured pattern
diagnoses (`config: smoke.diagnoses`), A2 (the merge touched migrations —
`config: smoke.migration_*`), D (no pattern recognized: verbatim log dump).
Single-shot, deterministic. Never auto-applies destructive fixes (rebuild,
recreate, `down -v`) — the owner runs those after reviewing the diagnosis.

`config: stack.runtime = "none"` → prints SKIPPED and exits 0.

Run:  uv run --no-project .workflow/scripts/post_merge_smoke.py
Exit codes: 0 on green, 1 on any failure (with diagnosis printed).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402

DEFAULT_SYNC_EXTS = [".py", ".js", ".jsx", ".ts", ".tsx", ".json"]
DEFAULT_DIAGNOSES = [
    {"id": "B", "title": "Package restructure / file deletion",
     "pattern": r"ModuleNotFoundError|ImportError|cannot import name|Cannot find module",
     "cause": "compose watch syncs new/modified files but does NOT delete removed files; "
              "a deleted module still exists in the container, shadowing the new layout.",
     "fix": "{ports_env} {compose} -p {stack} up -d --build {services}"},
    {"id": "C", "title": "Possible env-var change",
     "pattern": r"KeyError|environment variable|env var",
     "cause": "env vars are baked at container creation; neither compose watch nor a code "
              "sync picks up env-file changes.",
     "fix": "{ports_env} {compose} -p {stack} up -d --force-recreate {services}"},
]


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Every call gets the shared slot's port vars (compose calls must carry ALL of them)."""
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          cwd=cwd or wfconfig.repo_root(),
                          env=wfconfig.compose_env(int(wfconfig.get("slots.shared_slot", 0))))


def compose_logs(project: str, service: str, tail: int, since: str) -> str:
    args = ["logs", f"--tail={tail}"]
    if since:
        args.append(f"--since={since}")
    out = run(_stack.compose_argv(project, *args, service))
    return (out.stdout or "") + (out.stderr or "")


def sync_pathspecs() -> list[str]:
    explicit = [str(p) for p in (wfconfig.get("smoke.sync_paths", []) or [])]
    if explicit:
        return explicit
    import check_container_sync as ccs
    return [p or "." for p, _, _ in ccs.load_mounts()]


def merged_source_files(repo: Path) -> tuple[list[str], str]:
    """Tree-relative source paths the most recent merge brought onto the base branch.

    The MERGE's own first-parent diff, not HEAD's: /land-story lands a bookkeeping
    commit after the merge, so `HEAD~1..HEAD` would narrow the set to docs.
    """
    merge_sha = run(["git", "-C", str(repo), "rev-list", "--merges", "-1", "HEAD"]).stdout.strip()
    if merge_sha:
        rng, label = [f"{merge_sha}^1", merge_sha], f"merge {merge_sha[:7]}"
    else:
        rng, label = ["HEAD~1", "HEAD"], "HEAD~1..HEAD (no merge commit found)"
    exts = tuple(str(e) for e in (wfconfig.get("smoke.sync_exts", DEFAULT_SYNC_EXTS) or []))
    out = run(["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=d", *rng, "--",
               *sync_pathspecs()])
    files = [f.strip() for f in out.stdout.splitlines()
             if f.strip() and (not exts or f.strip().endswith(exts))]
    return files, label


def source_sync_gate(project: str, repo: Path) -> tuple[bool, list[str]]:
    import check_container_sync as ccs
    if not ccs.load_mounts():
        return True, ["source sync: SKIPPED — no sync mount configured "
                      '(config: stack.health.sync_mounts empty, stack.health.container_src = "")']
    files, label = merged_source_files(repo)
    if not files:
        return True, [f"source sync: skipped — {label} touched no container source files"]
    checker = _stack.scripts_dir() / "check_container_sync.py"
    if not checker.exists():
        return False, [f"source sync: CANNOT VERIFY — {checker} is missing",
                       "  Without it this gate cannot tell a fresh container from a stale one.",
                       "  Restore the script or verify by hand before trusting any green."]
    cap = int(wfconfig.get("smoke.max_sync_files", 40))
    probe = files[:cap]
    dropped = len(files) - len(probe)
    res = run([sys.executable, str(checker), "-p", project, *probe], cwd=repo)
    lines = [ln for ln in ((res.stdout or "") + (res.stderr or "")).splitlines() if ln.strip()]
    head = [f"source sync: {len(probe)} file(s) from {label}"
            + (f" (capped; {dropped} more not checked)" if dropped else "")]
    return res.returncode == 0, head + [f"  {ln}" for ln in lines]


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue] — keep output live when piped
    if not wfconfig.stack_enabled():
        _stack.skipped("post-merge smoke", _stack.STACK_NONE_REASON)
        return 0

    repo = wfconfig.repo_root()
    project = str(wfconfig.get("slots.shared_stack_name", ""))
    slot = int(wfconfig.get("slots.shared_slot", 0))
    services = [str(s) for s in (wfconfig.get("smoke.services", []) or [])] or _stack.expected_services()
    if not project:
        print("FAIL: config: slots.shared_stack_name is empty — no shared stack to smoke-check")
        return 1
    if not services:
        print("FAIL: no services to check — set config: smoke.services (or stack.services)")
        return 1
    patterns = [str(p) for p in (wfconfig.get("smoke.log_error_patterns", []) or [])]
    error_re = re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None
    tail_n = int(wfconfig.get("smoke.log_tail", 20))
    since = str(wfconfig.get("smoke.log_since", "") or "")
    fill = {"stack": project, "services": " ".join(services), "service": _stack.app_service(),
            "ports_env": _stack.ports_env_prefix(slot)}

    # ---------------------------------------------------------- service state
    ps = run(_stack.compose_argv(project, "ps", "--format", "json"))
    if ps.returncode != 0:
        print(f"FAIL: compose ps exited {ps.returncode}")
        print("  Likely cause: the container engine is not running. Start it and re-try.")
        return 1
    state = {s.get("Service"): s.get("State") for s in _stack.parse_ps_json(ps.stdout)}

    # ---------------------------------------------------------------- log tails
    logs = {svc: compose_logs(project, svc, tail_n, since) for svc in services}
    svc_ok = {svc: state.get(svc, "missing") == "running"
              and not (error_re and error_re.search(logs[svc])) for svc in services}
    all_svc_ok = all(svc_ok.values())

    # ------------------------------------------------------------ source sync
    sync_ok, sync_lines = source_sync_gate(project, repo)

    states_line = " ".join(f"{svc}={state.get(svc, 'missing')}" for svc in services)
    if all_svc_ok and sync_ok:
        print(f"smoke green: {states_line}, no fresh error lines in the last {tail_n} log lines")
        for line in sync_lines:
            print(line)
        print(f"{wfconfig.get('git.base_branch', 'main')} baseline clean.")
        return 0

    # ----------------------------------------------------------- failure path
    print(f"smoke FAIL: {states_line} source_sync={'ok' if sync_ok else 'STALE'}")
    print()

    if not sync_ok:
        print("[Diagnosis E] Containers are NOT running the merged code")
        for line in sync_lines:
            print(line)
        print("  Root cause: `compose watch` for this project is dead or was never started.")
        print("              It dies silently — no error, no log line — and the containers keep")
        print("              serving whatever they last synced. State and log checks cannot see it.")
        print(f"  Fix:        uv run --no-project .workflow/scripts/bring_up_story_stack.py --slot {slot} "
              f"--project-name {project} --worktree {repo} --mode up")
        print("              then restart watch (same script, --mode watch), else the next edit goes stale.")
        print(f"  Verify:     uv run --no-project .workflow/scripts/check_container_sync.py -p {project} <paths>")
        print()
        if all_svc_ok:
            print("  NOTE: services are healthy and logs are clean — this failure is ONLY the")
            print("        stale-source gate.")
            print()
            return 1

    combined = "\n".join(logs.values())
    any_match = False

    for diag in (wfconfig.get("smoke.diagnoses", DEFAULT_DIAGNOSES) or []):
        try:
            m = re.search(str(diag.get("pattern", "")), combined) if diag.get("pattern") else None
        except re.error as e:
            print(f"[Diagnosis {diag.get('id', '?')}] invalid pattern in config: {e}")
            continue
        if not m:
            continue
        any_match = True
        print(f"[Diagnosis {diag.get('id', '?')}] {diag.get('title', '')}")
        print(f"  Signal: {m.group(0)}")
        if diag.get("cause"):
            print(f"  Root cause: {diag['cause']}")
        if diag.get("fix"):
            print(f"  Fix:        {wfconfig.render(str(diag['fix']), **fill)}")
        else:
            print("  Action: surface the log lines; do NOT auto-fix — needs human judgment.")
        print()

    mig_re = str(wfconfig.get("smoke.migration_paths_regex", "") or "")
    touched = []
    if mig_re:
        touched = [f for f in run(["git", "-C", str(repo), "log", "-1", "--name-only",
                                   "--pretty=format:"]).stdout.splitlines()
                   if f.strip() and re.search(mig_re, f)]
    if touched:
        any_match = True
        print("[Diagnosis A2] Last commit touched migrations - checking live-DB drift")
        plan_cmd = str(wfconfig.get("smoke.migration_plan_cmd", "") or "")
        if not plan_cmd:
            print("  SKIPPED: config: smoke.migration_plan_cmd is empty — inspect the live DB by hand.")
        else:
            cmd = wfconfig.render(plan_cmd, **fill)
            plan = subprocess.run(cmd, shell=True, capture_output=True, text=True, errors="replace",
                                  cwd=repo, env=wfconfig.compose_env(slot))
            plan_out = (plan.stdout or "") + (plan.stderr or "")
            clean_re = str(wfconfig.get("smoke.migration_plan_clean_regex", "") or "")
            if plan.returncode != 0:
                print(f"  migration plan failed (exit {plan.returncode}):")
                for line in plan_out.splitlines()[:10]:
                    print(f"    {line}")
            elif clean_re and re.search(clean_re, plan_out):
                print("  migration plan: clean (live DB applied state matches on-disk migrations)")
                print("  NOTE: a plan is blind to in-place edits of already-applied migrations.")
                print("        If the merge edited an applied migration, the live DB is silently")
                print("        stale. Inspect the diff manually.")
            else:
                print("  migration plan reports pending operations - live DB is BEHIND migrations:")
                for line in plan_out.splitlines()[:15]:
                    print(f"    {line}")
                print("  Reset: follow the project's schema-reset rule (additive vs structural change).")
        print("  Touched migration files:")
        for f in touched:
            print(f"    {f}")
        print()

    if not any_match:
        print("[Diagnosis D] Pattern not recognized - dumping recent logs verbatim")
        for svc in services:
            print()
            print(f"--- {svc} last 30 lines ---")
            for line in compose_logs(project, svc, 30, "").splitlines():
                print(f"  {line}")

    for svc in services:
        print()
        print(f"--- {svc} tail (last {tail_n}) ---")
        for line in logs[svc].splitlines():
            print(f"  {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
