#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Resume one halted story whose questions are answered: archive its failure entries with the
owner's ruling, remove them and set the row `ready-for-dev`.

Usage (the main checkout, on `config: git.base_branch`):
  uv run --no-project <plugin-root>/scripts/resume_halted.py <story-id> --ruling '<text>' [--repo-root P]

Pass the ruling in single quotes and write each single quote inside it as `'\\''`: the shell
runs a backtick or a `$(...)` inside double quotes.

`<story-id>` is any form of the id that names one sprint-status key (`16-918`, `16.918`, a unique
key prefix, the full key). The story's entries are the failure-log entries whose heading id is
the same story (dotted and dashed forms alike); the latest is the last in file order.

Refusals (exit 1, `{"status": "refused", "message": ...}`, nothing written):
  * the main checkout is not on the base branch;
  * the id names no sprint-status key (sprint_status.py resolves it);
  * the story has no failure entry, or its row reads `done` or `superseded`;
  * `--ruling` is absent or blank (`--no-ruling` resumes an entry that asked nothing, e.g. a
    test-fix exhaustion or a stack failure the owner has fixed); an entry whose `asked_of` is
    `external` asked only the external design owner and needs no ruling;
  * the latest entry's `asked_of` is `external` or `both` and its `external_ticket` is not done in
    the design-question tracker (`config: paths.design_questions`: the ticket's row Status starts
    with `config: design.done_marker`), or has no row there: the design owner's part is open;
  * the failure-log mutex is not acquired within its wait;
  * the row's status, read again once the mutex is held, differs from the first read.

Under the failure-log mutex (`log_failure.LOG_MUTEX`), three steps run in order:
  archive  `sprint_status.py history <key> --append`: `resumed halt <entry id>`, an empty line,
           then a `~~~~text` fence holding `ruling: <first line>` (later lines indented two
           spaces) and every entry of the story verbatim. The fence is one tilde longer than any
           tilde run inside, so nothing closes it early.
  row      `sprint_status.py set <key> ready-for-dev`, only when the row reads `blocked`; a note
           leading with `HALTED <date> at <step>; worktree kept` loses that clause.
  remove   the story's entries leave the failure log; every other line keeps its ending.
A failed step stops the command: `{"status": "failed", "step", "message", "written": [...]}`.

The mutex is released before the commit: one commit on the base branch of only this command's
changes (`_bookkeeping.commit_own`), message `chore(auto-dev): <key> resumed; halt <entry id>
archived`. Success prints `{"status": "resumed", "story", "entries", "row", "commit"}` (exit 0).
A failed commit prints `{"status": "failed", "step": "commit", ...}`: the edits stay in the
working tree.

Exit codes: 0 resumed; 1 refused or failed; 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import _bookkeeping as bk  # noqa: E402
import _design  # noqa: E402
import halt_release as hr  # noqa: E402
import log_failure  # noqa: E402
import wfconfig  # noqa: E402
from _named_mutex import NamedMutex  # noqa: E402

LOG_MUTEX = log_failure.LOG_MUTEX
MUTEX_TIMEOUT_MS = log_failure.MUTEX_TIMEOUT_MS
FINISHED = ("done", "superseded")


class Refusal(RuntimeError):
    """The command refuses before it writes anything."""


class StepFailure(RuntimeError):
    def __init__(self, step: str, message: str, written: list[str]):
        super().__init__(message)
        self.step, self.written = step, list(written)


def entry_id(block: dict) -> str:
    return f"{block['story_id']}-{block['stamp']}"


def archive_text(entries: list[str], latest: dict, ruling: str) -> str:
    first, *rest = (ruling or "none").split("\n")
    shown = "\n".join([f"ruling: {first}"] + [f"  {line}" for line in rest])
    body = "\n\n".join(e.strip("\r\n") for e in entries)
    fence = "~" * max(4, 1 + max(map(len, re.findall(r"~+", f"{shown}\n{body}")), default=0))
    return f"resumed halt {entry_id(latest)}\n\n{fence}text\n{shown}\n\n{body}\n{fence}"


def sprint_status(repo: Path, *argv: str):
    return bk.run([sys.executable, str(SCRIPTS / "sprint_status.py"), "--path", str(repo / bk.sprint_status_rel()),
                   "--history-dir", str(repo / bk.history_rel("x").parent), *argv], cwd=repo)


def read_row(repo: Path, story: str) -> dict:
    got = sprint_status(repo, "get", story)
    try:
        row = json.loads(got.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Refusal(f"sprint_status.py get {story}: {(got.stdout + got.stderr).strip()[-300:]}") from None
    if row.get("status") != "ok":
        raise Refusal(f"{story} names no sprint-status key: {row.get('message')}")
    return row


def release(repo: Path, key: str, status: str, log_text: str, blocks: list[dict], entries: list[str],
            ruling: str) -> list[str]:
    written: list[str] = []
    try:
        done = sprint_status(repo, "history", key, "--append", archive_text(entries, blocks[-1], ruling))
    except OSError as exc:  # the archive is one argv element; an OS limit on its length is this step's failure
        raise StepFailure("archive", f"cannot run sprint_status.py: {exc}", written) from None
    if done.returncode != 0 or '"appended"' not in done.stdout:
        raise StepFailure("archive", (done.stdout + done.stderr).strip()[-300:], written)
    written.append("archive")
    if status == "blocked":
        note = read_row(repo, key).get("note") or ""
        halted = hr.LEAD_CLAUSE.match(note)
        flip = ["set", key, "ready-for-dev"]
        if halted and halted.group().startswith("HALTED"):
            rest = note[halted.end():].lstrip("; ").strip() or f"resumed after halt {entry_id(blocks[-1])}"
            flip.append(f"--note={rest}")  # one argv word: argparse reads a separate `-...` word as an option
        done = sprint_status(repo, *flip)
        if done.returncode != 0 or '"updated"' not in done.stdout:
            raise StepFailure("row", (done.stdout + done.stderr).strip()[-300:], written)
        written.append("row")
    try:
        (repo / bk.failure_log_rel()).write_bytes(
            bk.without_story(log_text, blocks[-1]["story_id"]).encode("utf-8", "surrogateescape"))
    except OSError as exc:
        raise StepFailure("remove", f"cannot edit the failure log: {exc}", written) from None
    written.append("remove")
    return written


def resume(repo: Path, given_id: str, ruling: str, no_ruling: bool) -> tuple[dict, int]:
    bk.check_branch(repo)
    row = read_row(repo, bk.dashed(given_id))
    key, status = row["key"], row["value"]
    if status in FINISHED:
        raise Refusal(f"{key} reads {status}; a finished story has nothing to resume")

    log_path = repo / bk.failure_log_rel()
    try:
        mutex = NamedMutex(LOG_MUTEX, MUTEX_TIMEOUT_MS)
        mutex.__enter__()
    except (TimeoutError, OSError) as exc:
        raise Refusal(f"the failure-log mutex was not acquired: {exc}") from None
    try:
        now = read_row(repo, key)["value"]
        if now != status:
            raise Refusal(f"{key} changed from {status} to {now} while the command waited for the failure-log "
                          "mutex; run it again")
        log_text = bk.work_text(repo, bk.failure_log_rel()) or ""
        lines = log_text.splitlines(keepends=True)
        blocks = [b for b in bk.failure_blocks(lines) if bk.same_story(b["story_id"], given_id)]
        if not blocks:
            raise Refusal(f"no failure entry for {given_id} ({key}) in {bk.failure_log_rel().as_posix()}")
        fields = blocks[-1]["fields"]
        asked = (fields.get("asked_of") or "").strip()
        if asked in ("external", "both"):
            ticket = (fields.get("external_ticket") or "").strip()
            state = _design.ticket_status(repo, ticket) if ticket else None
            if not _design.is_done(state):
                raise Refusal(f"the design owner's part is open: {ticket or 'the entry names no ticket'} "
                              + (f"reads {state}" if state else "has no row in the design-question tracker"))
        if not ruling and not no_ruling and asked != "external":
            raise Refusal("the owner's answer is owed: pass it as --ruling (or --no-ruling when the halt asked nothing)")
        entries = bk.entry_texts(log_text, given_id)
        try:
            written = release(repo, key, status, log_text, blocks, entries, ruling)
        except StepFailure as exc:
            return {"status": "failed", "step": exc.step, "message": str(exc), "written": exc.written}, 1
    finally:
        mutex.__exit__(None, None, None)

    status_rel, log_rel, history = bk.sprint_status_rel(), bk.failure_log_rel(), bk.history_rel(key)
    paths = [status_rel, history, log_rel]

    def stage_own() -> None:
        if "row" in written:
            base, work = bk.index_text(repo, status_rel), bk.work_text(repo, status_rel)
            if base is not None and work is not None:
                new = bk.replace_line(base, work, bk.row_matcher(key), f"{status_rel.as_posix()} row {key}")
                if new is not None:
                    bk.stage_text(repo, status_rel, new)
        work = bk.work_text(repo, history)
        if work is not None and work != bk.index_text(repo, history):
            bk.stage_text(repo, history, work)
        base = bk.index_text(repo, log_rel)
        if base is not None and bk.story_entries(base, given_id):
            bk.stage_text(repo, log_rel, bk.without_story(base, given_id))

    message = f"chore(auto-dev): {key} resumed; halt {entry_id(blocks[-1])} archived"
    try:
        sha = bk.commit_own(repo, paths, message, stage_own)
    except Exception as exc:  # noqa: BLE001 -- the edits stay in the working tree for the session to commit
        edited = [p.as_posix() for p in paths if p != status_rel or "row" in written]
        return {"status": "failed", "step": "commit", "message": str(exc), "uncommitted": edited}, 1
    after = "ready-for-dev" if "row" in written else status
    return {"status": "resumed", "story": key, "entries": [entry_id(b) for b in blocks], "row": after,
            "commit": sha}, 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="resume one halted story: archive its failure entries with the "
                                             "ruling, remove them and set the row ready-for-dev")
    ap.add_argument("story_id")
    ap.add_argument("--ruling", default="", help="the owner's answer, archived with the entries")
    ap.add_argument("--no-ruling", action="store_true", help="the halt asked no question")
    ap.add_argument("--repo-root", type=Path, default=None, help="main checkout (default: config repo root)")
    args = ap.parse_args(argv)
    repo = (args.repo_root or wfconfig.repo_root()).resolve()
    ruling = args.ruling.replace("\r\n", "\n").replace("\r", "\n").strip()
    try:
        doc, code = resume(repo, args.story_id, ruling, args.no_ruling)
    except (Refusal, bk.StepError) as exc:
        doc, code = {"status": "refused", "message": str(exc)}, 1
    except Exception as exc:  # noqa: BLE001 -- one JSON line, whatever went wrong
        doc, code = {"status": "failed", "message": f"{type(exc).__name__}: {exc}"}, 1
    print(json.dumps(doc))
    return code


if __name__ == "__main__":
    sys.exit(main())
