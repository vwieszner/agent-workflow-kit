#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""proposal_drift.py — for each pending curation proposal, report whether its draft still fits the live target.

Purpose: `curate` Step 2 needs, per proposal, the answer to "does this draft still apply?" — has the
target file changed since the proposal was written, did the change already land, does a patch draft
still apply. This prints that answer for every pending proposal in one read-only pass, instead of a
hand-written `git log --since` / `diff <live> <draft>` / `git apply --check` loop per proposal.

When to use: `curate` Step 2, before presenting proposals; or a reviewer subagent checking a batch.

Usage:
    uv run --no-project <plugin-root>/scripts/proposal_drift.py [--proposals DIR] [--only SLUG_SUBSTR ...]

Per proposal (`<kind>-<slug>.md` in `.workflow/state/proposals/`), one header line plus one line per
repo path named in its `target` frontmatter (a relative path with a directory part that exists or
whose top directory exists, or a file that exists at the repo root):
    NEW-ABSENT      target file does not exist yet (a new artifact; nothing to drift against)
    ADDED-SINCE     target's first commit is after the proposal — a duplicate, or the change already landed
    UNCHANGED       target has no commits since the proposal's run_id
    CHANGED n       target has n commits since the proposal (listed, newest first, max 5)
    LANDED          a whole-file draft is byte-identical to the live target (after LF normalisation)
    PATCH-APPLIES / PATCH-FAILS   `git apply --check` of a `<stem>.draft.patch` against the working tree
    OUTSIDE-REPO    target is a memory file; not checked
    index           an index file (a script registry, a rules INDEX, the artifact catalog); not checked —
                    nearly every harness commit touches it
A trailing `dirty` marks a target with uncommitted working-tree changes.

The proposal's timestamp is its `run_id` frontmatter (`YYYYMMDD-HHMMSS`, UTC).

Read-only: runs only `git log`, `git status --porcelain`, `git apply --check`. Exit 0 always
(a report, not a gate); exit 2 on a missing proposals dir, or when git is missing or does not answer.
Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks" / "session"))
import wfconfig  # noqa: E402

REPO = wfconfig.repo_root()
RUN_ID = re.compile(r"^(\d{8})-(\d{6})")
FRONT_KEY = re.compile(r"^(kind|title|target|run_id):\s*(.*)$")
REL_PATH = re.compile(r"(?<![\w/.:\\-])((?:\.?[\w-]+/)+[\w.-]+\.\w+)")
ROOT_FILE = re.compile(r"(?<![\w/.\\-])([\w-]+(?:\.[\w-]+)*\.\w+)(?![\w/\\-])")
MAX_COMMITS = 5
GIT_TIMEOUT = 60
INDEX_NAMES = re.compile(r"(?:^|/)(?:INDEX\.md|artifact[_-]?(?:index|catalog)[^/]*)$", re.IGNORECASE)
KINDS = ("skill", "agent", "rule", "script", "memory")


class GitError(Exception):
    pass


def git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT)
    except FileNotFoundError as exc:
        raise GitError(f"git is not runnable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} did not finish in {GIT_TIMEOUT}s") from exc


def frontmatter(text: str) -> dict:
    out: dict = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = FRONT_KEY.match(line)
        if m:
            key = m.group(1)
            out[key] = m.group(2).strip()
        elif key and line.startswith((" ", "\t")):
            out[key] += " " + line.strip()
    return out


def since_of(run_id: str) -> datetime | None:
    m = RUN_ID.match(run_id or "")
    if not m:
        return None
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def target_paths(tp: str) -> list[str]:
    paths = [p for p in REL_PATH.findall(tp)
             if (REPO / p).exists() or (REPO / p.split("/", 1)[0]).is_dir()]
    paths += [n for n in ROOT_FILE.findall(tp) if (REPO / n).is_file()]
    return list(dict.fromkeys(paths))


def norm(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n")


def target_lines(prop: Path, fm: dict) -> list[str]:
    tp = fm.get("target", "")
    since = since_of(fm.get("run_id", ""))
    lines = []
    if re.search(r"\bmemory\b", tp) and fm.get("kind") == "memory":
        lines.append("  memory target             OUTSIDE-REPO")
    drafts = sorted(p for p in prop.parent.glob(f"{prop.stem}.draft.*") if p.is_file())
    for rel in target_paths(tp):
        if INDEX_NAMES.search(rel):
            lines.append(f"  {rel:<40} index (not checked)")
            continue
        live = REPO / rel
        dirty = " dirty" if git("status", "--porcelain", "--", rel).stdout.strip() else ""
        if not live.exists():
            lines.append(f"  {rel:<40} NEW-ABSENT")
            continue
        if since is not None:
            added = [a for a in git("log", "--diff-filter=A", "--format=%ct %cs", "--", rel).stdout.split("\n")
                     if a.strip()]
            if added and int(added[-1].split()[0]) > since.timestamp():
                lines.append(f"  {rel:<40} ADDED-SINCE {added[-1].split()[1]}{dirty}")
                continue
        whole = [d for d in drafts if d.suffix == live.suffix and d.suffix != ".patch"]
        if live.is_file() and whole and any(norm(d.read_bytes()) == norm(live.read_bytes()) for d in whole):
            lines.append(f"  {rel:<40} LANDED{dirty}")
            continue
        if since is None:
            lines.append(f"  {rel:<40} ? (no run_id timestamp){dirty}")
            continue
        log = git("log", f"--since={since.isoformat()}", "--format=%cs %h %s", "--", rel).stdout
        log = [c for c in log.split("\n") if c.strip()]
        if not log:
            lines.append(f"  {rel:<40} UNCHANGED{dirty}")
            continue
        lines.append(f"  {rel:<40} CHANGED {len(log)}{dirty}")
        lines.extend(f"      {c[:110]}" for c in log[:MAX_COMMITS])
    for patch in (p for p in drafts if p.suffix == ".patch"):
        ok = git("apply", "--check", str(patch)).returncode == 0
        lines.append(f"  {patch.name:<40} {'PATCH-APPLIES' if ok else 'PATCH-FAILS'}")
    if not lines:
        lines.append("  (no repo path in target)")
    return lines


def is_proposal(p: Path) -> bool:
    return (p.is_file() and p.suffix == ".md" and ".draft." not in p.name and p.name != "README.md"
            and p.name.split("-", 1)[0].lower() in KINDS and "-" in p.name)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)  # absent when the stream is replaced
        if reconfigure:
            reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--proposals", type=Path, default=wfconfig.workflow_dir() / "state" / "proposals")
    ap.add_argument("--only", nargs="*", default=[], help="keep proposals whose file name contains one of these")
    a = ap.parse_args(argv)
    if not a.proposals.is_dir():
        print(f"no proposals dir: {a.proposals}", file=sys.stderr)
        return 2
    try:
        for prop in sorted(p for p in a.proposals.iterdir() if is_proposal(p)):
            if a.only and not any(s in prop.name for s in a.only):
                continue
            fm = frontmatter(prop.read_text(encoding="utf-8", errors="replace"))
            print(f"== {prop.name} | {fm.get('kind', '?')}")
            for line in target_lines(prop, fm):
                print(line)
    except GitError as exc:
        print(f"proposal_drift: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
