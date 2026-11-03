#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""One-command halt-and-release for an auto-dev-loop story.

The auto-dev-loop workflow's "Halt-and-release procedure" runs this script. A halt keeps the
story's work and evidence and releases only the slot. Steps, in order; a failed step is reported
and the later steps still run:
  1. draft   for a `story-spec*` step: `story_record.py append <id> "spec-draft incomplete (<step>)"`.
  2. wip     `git add -A` and commit `wip: [Story <id>] auto-loop halted in <step>` in the worktree
             (skipped when nothing changed). Untracked files whose name matches
             `config: review.repro_test_name_regex` stay untracked: a review re-run removes them.
  3. note    the sprint-status row becomes `blocked`, and its note's leading
             `STARTED <date> slot <N> (<branch>)` (or an earlier `HALTED` clause) becomes
             `HALTED <date> at <step>; worktree kept`. It runs before the entry, so a resume racing
             the halt finds no entry on a row that still reads in-progress.
  4. entry   log_failure.py appends the failure entry, with the WIP hash.
  5. lease   `land_lease.py release <id>` when the kit's land lease script is installed
             (exit 1, held by another story, is not a failure); skipped otherwise.
  6. watch   kills the worktree's orphan `compose watch` processes (no-op with stack.runtime none).
  7. stop    `{compose} -p <stack> stop`: containers, their logs and data volumes kept.
  8. slot    release_slot.py frees the slot-registry row.
  9. tracker with `--external-ticket`: the ticket's row in the design-question tracker
             (`config: paths.design_questions`), unless the key already has one; skipped without a
             ticket or without a tracker file.
 10. commit  on the base branch in the main checkout: the story's sprint-status row, its
             story-history file, its failure-log entry, its slot-registry row and the tracker lines
             carrying `[<ticket>]`, each staged as the index copy plus only this halt's change
             (`_bookkeeping.commit_own`), committed only when nothing else was staged.

Usage:
  uv run --no-project <plugin-root>/scripts/halt_release.py --story-id 16-918 --failure-step spec-open-questions \
      --failure-summary "story-spec returned 6 open questions: ..." --asked-of owner|external|both \
      [--last-test-output "<tail 30>"] [--external-ticket KEY --external-item "<the tracker row's Item cell>"]

`--asked-of` is required. `external` and `both` need `--external-ticket`, and any non-empty
`--external-ticket` must be exactly one ticket key (`config: paths.design_ticket_regex`). A missing
`--asked-of`, a bad ticket, or a line break in `--story-id`, `--failure-step`, `--external-ticket` or
`--external-item` is refused (exit 2) before any step.

Output: one JSON line {"status": "halted"|"partial"|"refused", "story_id", "wip_commit",
"bookkeeping_commit", "steps": {<step>: "ok ..."|"skipped: ..."|"FAILED: ..."}}.
Exit 0 when no step failed, 1 otherwise, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import _bookkeeping as bk  # noqa: E402
import _design  # noqa: E402
import wfconfig  # noqa: E402
from log_failure import ASKED_OF, ticket_refusal  # noqa: E402

DEFAULT_REPRO_NAME = r"^(test_(?:repro|pbt)_r\d+_\d+\.py|repro_r\d+_\d+\.test\.[cm]?[jt]sx?|repro-r\d+-\d+\.spec\.[cm]?[jt]s)$"
# The step is read lazily, up to the first "; worktree kept", so a step that holds ";" is still stripped.
LEAD_CLAUSE = re.compile(r"^(?:STARTED \S+ slot \d+ \([^)]*\)|HALTED \S+ at .+?; worktree kept)")


def note_cap() -> int:
    return int(wfconfig.get("sprint_status.note_max_chars", 240))


def repro_name() -> re.Pattern[str]:
    return re.compile(str(wfconfig.get("review.repro_test_name_regex", DEFAULT_REPRO_NAME)))


def halted_note(note: str, step: str, today: str) -> str:
    """The row note with its lead clause replaced by the halt clause, within the note cap."""
    clause = f"HALTED {today} at {step}; worktree kept"
    rest = LEAD_CLAUSE.sub("", note, count=1).lstrip("; ").strip()
    new = f"{clause}; {rest}" if rest else clause
    cap = note_cap()
    return new if len(new) <= cap else new[: cap - 1] + "…"


def add_tracker_row(repo: Path, ticket: str, story_id: str, item: str, today: str, step: str) -> bool:
    """Append the ticket's row to the design-question tracker; False when it already has one."""
    rel = _design.tracker_rel()
    path = repo / rel
    if not path.is_file():
        raise bk.StepError(f"{rel.as_posix()} does not exist")
    try:
        new, added = _design.insert_row(path.read_text(encoding="utf-8"), ticket, item,
                                        f"{story_id} ships the interims, ⚠️", f"auto-dev, the {story_id} {step} halt",
                                        today)
    except ValueError as exc:
        raise bk.StepError(f"{rel.as_posix()}: {exc}") from None
    if added:
        path.write_text(new, encoding="utf-8", newline="")
    return added


def commit_bookkeeping(repo: Path, story_id: str, key: str, slot: int | None, step: str, ticket: str = "") -> str:
    """Commit the story's own bookkeeping changes on the base branch; the new HEAD, or ''."""
    status, registry, log = bk.sprint_status_rel(), bk.registry_rel(), bk.failure_log_rel()
    history = bk.history_rel(key)
    tracker = _design.tracker_rel() if ticket else None
    paths = [status, history, log, registry] + ([tracker] if tracker else [])

    def stage_own() -> None:
        if tracker is not None:
            work = bk.work_text(repo, tracker)
            if work is not None:
                new = _design.own_lines(bk.index_text(repo, tracker) or "", work, lambda ln: f"[{ticket}]" in ln)
                if new is not None:
                    bk.stage_text(repo, tracker, new)
        base = bk.index_text(repo, status)
        work = bk.work_text(repo, status)
        if base is not None and work is not None:
            new = bk.replace_line(base, work, bk.row_matcher(key), f"{status.as_posix()} row {key}")
            if new is not None:
                bk.stage_text(repo, status, new)
        if slot is not None:
            base, work = bk.index_text(repo, registry), bk.work_text(repo, registry)
            if base is not None and work is not None:
                new = bk.replace_line(base, work, bk.slot_matcher(slot), f"{registry.as_posix()} slot {slot}")
                if new is not None:
                    bk.stage_text(repo, registry, new)
        work = bk.work_text(repo, history)
        if work is not None and work != bk.index_text(repo, history):
            bk.stage_text(repo, history, work)
        work = bk.work_text(repo, log)
        if work is not None:
            base = bk.index_text(repo, log)
            if base is None:
                bk.stage_text(repo, log, work)
            else:
                have = set(bk.entry_texts(base, story_id))
                added = [t for t in bk.entry_texts(work, story_id) if t not in have]
                if added:
                    joined = base if base.endswith("\n") or not base else base + "\n"
                    bk.stage_text(repo, log, joined + "".join(added))

    message = f"chore(auto-dev): {story_id} halted at {step}; slot released, worktree kept"
    return bk.commit_own(repo, paths, message, stage_own)


def refusal_of(args: argparse.Namespace) -> str:
    if not args.asked_of:
        return "--asked-of is required (owner, external or both)"
    for flag, value in (("--story-id", args.story_id), ("--failure-step", args.failure_step),
                        ("--external-ticket", args.external_ticket), ("--external-item", args.external_item)):
        if "".join(value.splitlines()) != value:
            return f"{flag} must be one line"
    return ticket_refusal(args.asked_of, args.external_ticket)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="auto-dev-loop halt-and-release: keep the work, release the slot")
    ap.add_argument("--story-id", required=True)
    ap.add_argument("--failure-step", required=True)
    ap.add_argument("--failure-summary", required=True)
    ap.add_argument("--last-test-output", default="")
    ap.add_argument("--asked-of", choices=ASKED_OF, default="", help="whose answers the halt waits on")
    ap.add_argument("--external-ticket", default="", help="the story's design-owner question ticket")
    ap.add_argument("--external-item", default="", help="the tracker row's Item cell for a new ticket")
    ap.add_argument("--repo-root", type=Path, default=None, help="main checkout (default: config repo root)")
    args = ap.parse_args(argv)
    refusal = refusal_of(args)
    if refusal:
        print(json.dumps({"status": "refused", "message": refusal}))
        return 2
    repo = (args.repo_root or wfconfig.repo_root()).resolve()
    story_id, step = args.story_id, args.failure_step
    dash = bk.dashed(story_id)
    worktree = wfconfig.worktree_path(dash)
    stack = wfconfig.stack_name(dash)
    py = sys.executable
    steps: dict[str, str] = {}
    out = {"story_id": story_id, "wip_commit": "", "bookkeeping_commit": ""}
    slot = bk.registry_slot(repo, story_id)
    key = ""

    def attempt(name: str, body) -> None:
        try:
            steps[name] = body() or "ok"
        except bk.StepError as exc:
            steps[name] = f"FAILED: {exc}"
        except Exception as exc:  # noqa: BLE001 -- every step runs; the failure is reported, never swallowed
            steps[name] = f"FAILED: {type(exc).__name__}: {exc}"

    def script(name: str, *a: str):
        return bk.run([py, str(SCRIPTS / name), *a], cwd=repo)

    def draft():
        if not step.startswith("story-spec"):
            return "skipped: not a story-spec step"
        r = script("story_record.py", "append", story_id, f"spec-draft incomplete ({step})")
        if r.returncode != 0:
            raise bk.StepError((r.stdout + r.stderr).strip()[-300:])

    def wip():
        if not worktree.is_dir():
            raise bk.StepError(f"no worktree at {worktree}")
        listed = bk.run(["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"])
        if listed.returncode != 0:
            raise bk.StepError("git ls-files --others failed")
        rx = repro_name()
        repro = [p for p in listed.stdout.split("\0") if p and rx.match(p.rsplit("/", 1)[-1])]
        if bk.run(["git", "-C", str(worktree), "add", "-A"]).returncode != 0:
            raise bk.StepError("git add -A failed")
        if repro and bk.run(["git", "-C", str(worktree), "reset", "-q", "--", *repro]).returncode != 0:
            raise bk.StepError("git reset of the untracked repro tests failed")
        if bk.run(["git", "-C", str(worktree), "diff", "--cached", "--quiet"]).returncode == 0:
            return "skipped: nothing to commit"
        done = bk.run(["git", "-C", str(worktree), "commit", "-q", "-m",
                       f"wip: [Story {story_id}] auto-loop halted in {step}"])
        if done.returncode != 0:
            raise bk.StepError(f"git commit: {(done.stderr or done.stdout).strip()[-300:]}")
        out["wip_commit"] = bk.run(["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"]).stdout.strip()
        return f"ok {out['wip_commit']}"

    def note():
        nonlocal key
        got = script("sprint_status.py", "get", dash)
        try:
            row = json.loads(got.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise bk.StepError(f"sprint_status.py get {dash}: {(got.stdout + got.stderr).strip()[-300:]}")
        if row.get("status") != "ok":
            raise bk.StepError(f"sprint_status.py get {dash}: {row.get('message')}")
        key = row["key"]
        new = halted_note(row.get("note") or "", step, date.today().isoformat())
        r = script("sprint_status.py", "set", key, "blocked", f"--note={new}")
        if '"updated"' not in r.stdout:
            raise bk.StepError((r.stdout + r.stderr).strip()[-300:])

    def entry():
        r = script("log_failure.py", "--story-id", story_id, "--branch-name", dash, "--failure-step", step,
                   "--failure-summary", args.failure_summary, "--wip-commit", out["wip_commit"],
                   "--asked-of", args.asked_of, "--external-ticket", args.external_ticket,
                   "--last-test-output", args.last_test_output, "--log-path", str(repo / bk.failure_log_rel()))
        if r.returncode != 0 or '"logged"' not in r.stdout:
            raise bk.StepError((r.stdout + r.stderr).strip()[-300:])

    def lease():
        if not (SCRIPTS / "land_lease.py").is_file():
            return "skipped: no land_lease.py installed"
        r = script("land_lease.py", "release", story_id)
        if r.returncode == 1:
            return "skipped: not held by this story"
        if r.returncode != 0:
            raise bk.StepError((r.stdout + r.stderr).strip()[-300:])

    def watch():
        if not wfconfig.stack_enabled():
            return "skipped: stack.runtime is none"
        import _stack
        procs, err = _stack.find_compose_watch(worktree)
        if err:
            raise bk.StepError(err)
        left = [p["pid"] for p in procs if not _stack.kill_pid(int(p["pid"]))]
        if left:
            raise bk.StepError(f"could not kill compose watch pid(s) {left}")
        return f"ok {len(procs)} killed"

    def stop():
        if not wfconfig.stack_enabled():
            return "skipped: stack.runtime is none"
        if slot is None:
            raise bk.StepError(f"no in_use slot-registry row for {dash}, so the slot ports are unknown")
        import _stack
        r = _stack.run(_stack.compose_argv(stack, "stop"), timeout=300, cwd=str(_stack.compose_dir(worktree)),
                       env=wfconfig.compose_env(slot))
        if r.returncode != 0:
            raise bk.StepError((r.stdout + r.stderr).strip()[-300:])

    def release():
        r = script("release_slot.py", "--story-id", story_id)
        if '"released"' in r.stdout:
            return f"ok slot {slot}"
        if '"not_found"' in r.stdout:
            return "skipped: no in_use row"
        raise bk.StepError((r.stdout + r.stderr).strip()[-300:])

    def tracker():
        if not args.external_ticket:
            return "skipped: no external ticket"
        if not _design.configured():
            return "skipped: config: paths.design_questions is empty"
        item = args.external_item or f"questions of {story_id} (see the ticket)"
        if not add_tracker_row(repo, args.external_ticket, story_id, item, date.today().isoformat(), step):
            return f"skipped: {args.external_ticket} already in the tracker"

    def commit():
        if not key:
            raise bk.StepError("the sprint-status key is unknown (the note step failed)")
        ticket = args.external_ticket if _design.configured() else ""
        out["bookkeeping_commit"] = commit_bookkeeping(repo, story_id, key, slot, step, ticket)
        return f"ok {out['bookkeeping_commit']}" if out["bookkeeping_commit"] else "skipped: nothing to commit"

    for name, body in (("draft", draft), ("wip", wip), ("note", note), ("entry", entry), ("lease", lease),
                       ("watch", watch), ("stop", stop), ("slot", release), ("tracker", tracker),
                       ("commit", commit)):
        attempt(name, body)
    failed = any(v.startswith("FAILED") for v in steps.values())
    print(json.dumps({"status": "partial" if failed else "halted", **out, "steps": steps}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
