#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tear down a story slot: orphan compose-watch kill, `compose down -v`, story-record
preservation, worktree removal, branch delete, slot release, graph-project delete,
per-stack setup artifacts.

Orphan watch: a `compose watch` started in the background can outlive the
session that started it and keep the worktree directory busy through its CWD
anchor (on Windows, `git worktree remove` and recursive deletes then fail with
"Device or resource busy"). Detection:
  1. Candidates: container-engine processes (docker / docker-compose / podman /
     the `config: stack.compose_cmd` binary) whose command line contains
     `compose` and a bare `watch` token. The lookaround (?<![-\\w])watch(?![-\\w])
     excludes the `up --watch` FLAG form.
  2. Read each candidate's CWD — psutil when importable; POSIX fallback: `ps` for
     the table, /proc/<pid>/cwd or `lsof -a -p <pid> -d cwd` for the CWD.
     Windows without psutil: CWD cannot be read (WMI does not expose it), so
     detection is reported UNAVAILABLE and nothing is killed — run with
     `uv run --with psutil --no-project ...` or stop the watch by hand.
  3. Orphan = CWD equal to or inside the worktree (path-separator-aware prefix;
     the real safety net — a broad command-line match cannot reach another
     slot's watch).
  4. Kill the orphan, and its parent only when the parent is itself a
     container-engine process; then wait 500ms (Windows releases directory
     handles asynchronously).

Safety:
  - Refuses to run when the resolved worktree is the main repo root.
  - Never guesses a graph project name: the name comes only from the
    `.workflow/state/story-setup/<stack>.graph_project` sidecar written by
    story_setup.py; missing/empty sidecar → graph delete SKIPPED with a warning.
  - The story record (`<specs_dir>/<id>-record.md` in the worktree) is copied
    into the main repo's specs dir BEFORE the worktree is deleted.
  - The slot-registry row is released LAST (via release_slot.py — never
    reimplemented here), after the worktree and branch are gone, so a cleanup
    that dies part-way leaves the row `in_use` as the signal it did not finish.
  - Idempotent: re-running on a half-cleaned story finishes the job.

`config: stack.runtime = "none"` → the compose step prints SKIPPED; everything
else runs. `config: graph.tool = "none"` → the graph step prints SKIPPED.

Run:
  uv run --no-project .workflow/scripts/cleanup_story_stack.py <story-id>
  uv run --no-project .workflow/scripts/cleanup_story_stack.py <story-id> --dry-run
  uv run --no-project .workflow/scripts/cleanup_story_stack.py <story-id> --skip-docker-down --skip-branch-delete

Exit codes: 0 on completion (warnings possible); 1 when the worktree cannot be
resolved, the safety refusal fires, or an unexpected error aborts the teardown.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _stack  # noqa: E402
import slot_registry as reg  # noqa: E402


def _registered_worktrees(repo_root: Path) -> dict[Path, str]:
    """{resolved worktree path: branch ref} from `git worktree list --porcelain`."""
    out = subprocess.run(["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
                         capture_output=True, text=True, errors="replace").stdout
    found: dict[Path, str] = {}
    path: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):]).resolve()
            found[path] = ""
        elif line.startswith("branch ") and path is not None:
            found[path] = line[len("branch "):].strip()
    return found


def resolve_worktree(story_id: str, worktree_root: Path):
    """Return (slot_num | None, branch, worktree_path). Raises LookupError on failure."""
    registry = reg.registry_path()
    rows = reg.parse_rows(reg.read_lines(registry)) if registry.is_file() else []
    for row in rows:
        if row["status"] == "in_use" and row["story_id"] == story_id:
            wt = Path(row["worktree"])
            if not wt.is_absolute():
                wt = wfconfig.repo_root() / wt
            return row["slot"], row["branch"] or wfconfig.branch_name(story_id), wt
    # Fallback (registry row already free): ONLY the dir named exactly <id>, and only when
    # git registers it on this story's branch. A prefix match would reach a sibling story
    # (`5` → `5-1`) and delete its worktree and branch.
    branch = wfconfig.branch_name(story_id)
    cand = worktree_root / story_id
    if not cand.exists():
        return None, branch, cand  # already removed; the branch step stays idempotent
    for row in rows:
        if row["status"] == "in_use" and row["worktree"]:
            owned = Path(row["worktree"])
            if not owned.is_absolute():
                owned = wfconfig.repo_root() / owned
            if owned.resolve() == cand.resolve():
                raise LookupError(
                    f"{cand} is assigned to story '{row['story_id']}' in the slot registry -- "
                    f"refusing to tear it down for '{story_id}'")
    ref = _registered_worktrees(wfconfig.repo_root()).get(cand.resolve())
    if ref != f"refs/heads/{branch}":
        raise LookupError(
            f"Cannot resolve worktree for story id '{story_id}' -- registry has no in_use row "
            f"and {cand} is not a git worktree on {branch} "
            f"({'not registered' if ref is None else ref or 'detached HEAD'})")
    return None, branch, cand


def find_orphans(worktree: Path) -> tuple[list[dict], str | None]:
    """Compose-watch processes whose CWD is anchored inside the worktree."""
    return _stack.find_compose_watch(worktree, verbose=True)


def preserve_story_record(story_id: str, worktree: Path, repo_root: Path) -> None:
    """Copy the story's record file out of the worktree BEFORE the worktree is deleted.

    `<id>-record.md` holds the approvals and test verdicts a cleared session needs.
    Its last line (checkpoint-3 merge approved) is appended after the close-out
    commit, so no commit carries it and the merge does not move it. The copy lands
    in the main repo for /land-story's bookkeeping commit to pick up.
    """
    specs = str(wfconfig.get("paths.specs_dir", "docs/implementation-artifacts"))
    arts = worktree / specs
    candidates = sorted(arts.glob("*-record.md")) if arts.is_dir() else []
    variants = {story_id, story_id.replace(".", "-"), story_id.replace("-", ".")}
    hits = [p for p in candidates if p.stem[: -len("-record")] in variants]
    if not hits:
        print(f"\nstory record: none found under {arts} -- this story ran without a "
              f"durable handoff record (nothing to preserve)")
        return
    dest_dir = repo_root / specs
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in hits:
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        print(f"\nstory record preserved: {src} -> {dest}")
        print("  (stage it in /land-story's bookkeeping commit; it is the only durable "
              "evidence of the merge approval)")


def release_slot_row(story_id: str) -> str:
    """Free the story's registry row via release_slot.py; return a status.

    `not_found` (row already free) is success -- re-running cleanup must be idempotent.
    """
    script = _stack.scripts_dir() / "release_slot.py"
    if not script.is_file():
        print(f"\nWARNING: {script} not found -- slot row NOT released; free it by hand.")
        return "FAILED (release_slot.py missing)"
    print(f"\n{Path(sys.executable).name} {script} --story-id {story_id}")
    try:
        # Pin the main repo: run from inside the worktree being deleted, wfconfig would
        # otherwise resolve the root from a vanished cwd and the release would crash.
        repo_root = wfconfig.repo_root()
        r = subprocess.run([sys.executable, str(script), "--story-id", story_id],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=90, cwd=repo_root,
                           env=dict(os.environ, WORKFLOW_REPO_ROOT=str(repo_root)))
    except Exception as e:  # noqa: BLE001 -- any failure must be visible, never swallowed
        print(f"WARNING: release_slot.py could not run -- {e}")
        return f"FAILED ({e})"
    out = (r.stdout or "").strip()
    print(out or r.stderr.strip())
    try:
        status = json.loads(out.splitlines()[-1]).get("status", "?")
    except Exception:  # noqa: BLE001
        status = "?"
    if r.returncode != 0 or status not in ("released", "not_found"):
        print("WARNING: slot row NOT released -- registry still shows in_use; free it by hand.")
        return f"FAILED ({status})"
    return "released" if status == "released" else "already free"


def delete_graph_project(sidecar: Path) -> str:
    if str(wfconfig.get("graph.tool", "none")) == "none":
        _stack.skipped("graph project delete", 'config: graph.tool = "none"')
        return "skipped (graph.tool = none)"
    graph_project = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else ""
    if not graph_project:
        print(f"\nWARNING: no graph_project sidecar at {sidecar} -- skipping graph "
              "delete (never guess a project name).")
        return "skipped (no sidecar)"
    raw = str(wfconfig.get("graph.cli", "") or "")
    cli = Path(os.path.expanduser(raw)) if raw else None
    if cli is None or not cli.is_file():
        print(f"\nWARNING: graph CLI not found at {cli or '<config: graph.cli is empty>'} -- "
              f"graph project '{graph_project}' NOT deleted.")
        return "FAILED (cli missing)"
    print(f"\n{cli} cli delete_project --project {graph_project}")
    try:
        out = subprocess.run([str(cli), "cli", "delete_project", "--project", graph_project],
                             capture_output=True, text=True, errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        print("  TIMEOUT after 120s -- graph project NOT deleted. Delete it by hand: "
              f"{cli} cli delete_project --project {graph_project}")
        return "FAILED (timeout)"
    # The CLI writes its result JSON to stdout on success but to stderr on failure;
    # scan both streams and surface the reason.
    blobs = [ln.strip() for ln in (out.stdout + "\n" + out.stderr).splitlines()
             if ln.strip().startswith("{")]
    verdict = blobs[-1] if blobs else ""
    print(f"  {verdict or '(no JSON result)'}  [exit {out.returncode}]")
    if '"deleted"' in verdict:
        return graph_project
    if '"not_found"' in verdict:
        return f"{graph_project} (already absent)"
    detail = (out.stderr or out.stdout).strip()
    if detail:
        print("  CLI said:")
        for ln in detail.splitlines()[-8:]:
            print(f"    {ln}")
    print(f"  Graph project '{graph_project}' still exists -- retry by hand, or use the "
          "graph MCP server's delete_project tool.")
    return "FAILED"


def run_stream(cmd: list[str], **kw) -> int:
    """Run a child with output flowing to the console; return its exit code."""
    return subprocess.run(cmd, **kw).returncode


def capture(cmd: list[str], **kw) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              **kw).stdout.strip()
    except OSError:
        return ""


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue] — keep progress ordered around child output
    ap = argparse.ArgumentParser(description="Tear down an isolated story slot")
    ap.add_argument("story_id", help="story id as in the slot registry (dotted ids are dashed)")
    ap.add_argument("--repo-root", default=None, help="default: wfconfig repo root")
    ap.add_argument("--worktree-root", default=None,
                    help="default: config: git.worktrees_root")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the kill list and planned actions without executing")
    ap.add_argument("--skip-docker-down", action="store_true",
                    help="skip 'compose down -v' (orphan recovery)")
    ap.add_argument("--skip-branch-delete", action="store_true",
                    help="skip 'git branch -D' (orphan recovery / preserve WIP)")
    args = ap.parse_args()
    repo_root = _stack.set_repo_root(args.repo_root)
    worktree_root = Path(args.worktree_root) if args.worktree_root else wfconfig.worktrees_root()
    sid = reg.dashed(args.story_id)
    stack = wfconfig.stack_name(sid)
    slot_release = "<dryrun>"
    graph_deleted = "<dryrun>"

    try:
        slot_num, branch, worktree = resolve_worktree(sid, worktree_root)
    except LookupError as e:
        print(f"ERROR: {e}")
        return 1

    if _stack.path_inside(str(repo_root), worktree):
        print(f"ERROR: resolved worktree {worktree} is (or contains) the main repo {repo_root} "
              "-- refusing to tear it down. Fix the registry row.")
        return 1

    print(f"Story id    : {sid}")
    print(f"Worktree    : {worktree}")
    print(f"Branch      : {branch}")
    print(f"Stack       : {stack}")
    print(f"Slot number : {slot_num if slot_num is not None else '<unknown -- registry already free>'}")
    print(f"DryRun      : {args.dry_run}")
    print()

    # ---------------------------------------------- orphan compose-watch kill
    orphans, note = find_orphans(worktree)
    victims: list[int] = []
    if note:
        print(f"WARNING: orphan compose-watch detection UNAVAILABLE -- {note}")
        print("         Nothing was killed. If worktree removal fails with 'resource busy', stop "
              "the watch process by hand and re-run.")
    elif not orphans:
        print(f"No orphan compose-watch processes anchored in {worktree}.")
    else:
        print("\nOrphan compose-watch processes detected:")
        for o in orphans:
            print(f"  pid={o['pid']}  parent={o['ppid']}  cmd={o['cmdline']}")
        pids = {o["pid"] for o in orphans}
        for o in orphans:
            ppid = o.get("ppid") or 0
            if ppid > 1 and _stack.is_compose_process(_stack.process_name(ppid), "compose"):
                pids.add(ppid)
        victims = sorted(pids)

    if victims:
        if args.dry_run:
            print(f"\nDRYRUN: would kill PIDs {','.join(map(str, victims))}")
        else:
            print(f"\nStopping PIDs: {','.join(map(str, victims))}")
            for pid in victims:
                _stack.kill_pid(pid)
            time.sleep(0.5)

    # -------------------------------------------------------- standard teardown
    if not args.dry_run:
        if args.skip_docker_down:
            print("compose down -v: skipped (--skip-docker-down)")
        elif not wfconfig.stack_enabled():
            _stack.skipped("compose down -v", _stack.STACK_NONE_REASON)
        else:
            print()
            engine = _stack.engine_cmd()
            env = wfconfig.compose_env(slot_num if slot_num is not None
                                       else int(wfconfig.get("slots.shared_slot", 0)))
            # Probe containers (-a includes stopped), volumes and networks so a
            # merely-stopped stack still gets a full down -v; skip only when truly
            # nothing is left.
            has_leftovers = (
                capture(_stack.compose_argv(stack, "ps", "-aq"), env=env)
                or capture([engine, "volume", "ls", "-q", "--filter", f"name=^{stack}"])
                or capture([engine, "network", "ls", "-q", "--filter", f"name=^{stack}"])
            )
            down = _stack.compose_argv(stack, "down", "-v")
            if has_leftovers:
                print(" ".join(down))
                run_stream(down, env=env)
            else:
                print(f"{' '.join(down[:-2])} -- no containers/volumes/network present, skipping down -v")

        if worktree.exists():
            preserve_story_record(sid, worktree, repo_root)
            print(f"\ngit worktree remove --force {worktree}")
            # git may exit non-zero if the worktree was already pruned from its
            # registry (typical for orphan recovery); fall through either way.
            run_stream(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree)])
            if worktree.exists():
                print("\ngit did not remove dir; falling back to recursive delete")
                try:
                    shutil.rmtree(worktree)
                    print("Filesystem removal succeeded.")
                except OSError as e:
                    print(f"WARNING: Failed to remove {worktree} -- {e}")
                    print("WARNING: If no compose-watch or other process is anchored here, this "
                          "is likely a container-engine / VM file-share stale handle. Wait for it "
                          "to release, or restart the engine (disruptive to other slots).")
            run_stream(["git", "-C", str(repo_root), "worktree", "prune"])
        else:
            print(f"Worktree {worktree} already absent.")

        if not args.skip_branch_delete and branch:
            if capture(["git", "-C", str(repo_root), "branch", "--list", branch]):
                print(f"\ngit branch -D {branch}")
                run_stream(["git", "-C", str(repo_root), "branch", "-D", branch])
            else:
                print(f"Branch {branch} already deleted.")

        # Last registry step: ordered after worktree + branch removal so a cleanup
        # that fails above leaves `in_use` behind as evidence.
        slot_release = release_slot_row(sid)

        # ------------------------------------------------------ graph project
        setup_dir = wfconfig.state_dir("story-setup")
        sidecar = setup_dir / f"{stack}.graph_project"
        graph_deleted = delete_graph_project(sidecar)

        # Per-stack async-setup artifacts (git-ignored, accumulate otherwise).
        for f in (setup_dir / f"{stack}.json", setup_dir / f"{stack}.log", sidecar):
            if f.is_file():
                print(f"Removing async-setup artifact: {f}")
                try:
                    f.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------- summary
    branch_gone = not capture(["git", "-C", str(repo_root), "branch", "--list", branch])
    print()
    print("===== summary =====")
    print(f"Killed PIDs   : {'<dryrun>' if args.dry_run else ','.join(map(str, victims)) or '<none>'}")
    print(f"Watch detect  : {'UNAVAILABLE' if note else 'ok'}")
    print(f"Worktree gone : {not worktree.exists()}")
    print(f"Branch gone   : {branch_gone}")
    print(f"Slot released : {slot_release}")
    print(f"Graph project : {graph_deleted}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - surface, exit 1 per the contract
        print(f"ERROR: unexpected failure aborted the teardown: {e!r}")
        sys.exit(1)
