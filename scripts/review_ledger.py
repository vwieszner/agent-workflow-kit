#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Review-loop state of one story, read from its record file and its findings files.

The `layered-review` pipeline mode writes two durable traces per review round:
  * the record line `review round <n> recorded — …; reviewed <kind> <base40>..<end40>`
    (`review_findings_render.py`, through `story_record.cmd_append`);
  * a `## Review round <n> — <date>` section in `<story>-code-review-findings-<date>.md`, which
    triage extends with a `## Triage decisions` section holding `### approved_patches`,
    `### deferred_findings` and `### dismissed_findings` lists.

This module reads them back: which rounds were recorded, which were triaged and finalized, whether
the review loop has ended (`review_loop`), and whether a re-run is owed (`review_rerun_due`).
`review_stage.py` stages the next round from it; `review_carry.py` and `review_findings_render.py`
read round triage from it. `story_ledger.py` may import it for the ledger's review rows.

Loop rules (`rules-defer`): a round is finalized when a `phase-3 … GREEN` record line follows its
record line, or when its triage approved no patch and deferred no finding. The loop ends on a
new-code round, a Loop exit, a round that approved no patch, or the close-out round cap
(`CLOSE_OUT_CAP` close-out rounds after the latest triaged branch round).

Importable only; stdlib only.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

_RECORDED_ROUND_RE = re.compile(r"\breview round (\d+) recorded\b", re.IGNORECASE)
_ROUND_HEADING_RE = re.compile(r"^## review round (\d+)\b", re.MULTILINE)  # lowercased text
_TRIAGE_HEADING_RE = re.compile(r"^## triage decisions", re.MULTILINE)       # lowercased text
_TRIAGE_LISTS = ("approved_patches", "deferred_findings", "dismissed_findings")
GREEN = ("green", "passed", "pass")
RED = ("red", "fail", "error", "pending", "not run")
_REVIEWED_RE = re.compile(r"; reviewed (branch|close-out|new-code) ([0-9a-f]{40})\.\.([0-9a-f]{40})")
_MODE_RE = re.compile(r"^- Diff: .*?; mode (\w+)", re.MULTILINE)
_EXIT_CANDIDATE_RE = re.compile(r"^- Loop exit: candidate", re.MULTILINE)
_LIST_HEADING_RE = re.compile(r"^###\s+(approved_patches|deferred_findings)\b.*$", re.MULTILINE | re.IGNORECASE)
_FINDING_ID = r"R\d+-\d+[a-z]?"
CLOSE_OUT_CAP = 2
LOOP_CAP_REASON = f"round cap: {CLOSE_OUT_CAP} close-out rounds after the branch review"
NO_PATCH_PREFIX = "no patch approved in round "
NEW_CODE_REASON = "new-code round: no further review round"
RERUN_STEP = "ui-states-unchecked"
HALT_SUBJECT = r"^wip: \[Story {id}\] auto-loop halted in (\S+)\s*$"


def variants(story_id: str) -> list[str]:
    """7-920b -> ['7-920b', '7.920b']; 10.97 -> ['10-97', '10.97']."""
    a = story_id.strip()
    return sorted({a, a.replace(".", "-"), a.replace("-", ".")})


def specs_rel() -> str:
    return str(wfconfig.get("paths.specs_dir", "docs/implementation-artifacts")).strip("/")


def base_branch() -> str:
    return str(wfconfig.get("git.base_branch", "main"))


def recorded_rounds(record: list[str]) -> set[int]:
    """Round numbers that have a `review round <n> recorded` record line."""
    return {int(m.group(1)) for line in record if (m := _RECORDED_ROUND_RE.search(line))}


def round_triaged(body: str, n: int) -> bool:
    """True if round n's own section of a LOWERCASED findings text holds triage.

    The section runs from the LAST `## Review round n` heading to the next `## Review round` heading or EOF;
    inside it a `## Triage decisions` heading must be followed by one of the three triage lists."""
    heads = list(_ROUND_HEADING_RE.finditer(body))
    own = [h for h in heads if int(h.group(1)) == n]
    if not own:
        return False
    start = own[-1].end()
    end = next((h.start() for h in heads if h.start() > start), len(body))
    section = body[start:end]
    t = _TRIAGE_HEADING_RE.search(section)
    return bool(t) and any(k in section[t.end():] for k in _TRIAGE_LISTS)


def line_has(line: str, keys: tuple[str, ...], positive: tuple[str, ...] = (),
             negative: tuple[str, ...] = ()) -> bool:
    """A record line carries every key, one positive word (when given) and no negative word (whole word,
    optionally inflected), in any case."""
    low = line.lower()
    if not all(k in low for k in keys):
        return False
    if any(re.search(rf"\b{re.escape(n)}(?:s|{re.escape(n[-1])}?(?:ed|ing)|ure|ures)?\b", low) for n in negative):
        return False
    return not positive or any(p in low for p in positive)


def record_verdict(record: list[str], keys: tuple[str, ...], positive: tuple[str, ...] = (),
                   negative: tuple[str, ...] = ()) -> str | None:
    for line in record:
        if line_has(line, keys, positive, negative):
            return line.lstrip("- ").strip()
    return None


def numbered_record_lines(path: str) -> list[tuple[int, str]]:
    """(line number, stripped line) of each line starting with `-`; raises OSError."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [(n, line.strip()) for n, line in enumerate(fh, 1) if line.startswith("-")]


def record_lines(arts: str, story_id: str) -> list[str]:
    for v in variants(story_id):
        p = os.path.join(arts, f"{v}-record.md")
        if os.path.isfile(p):
            try:
                return [line for _n, line in numbered_record_lines(p)]
            except OSError:
                return []
    return []


def round_section(body: str, n: int) -> str:
    """Round n's own section of the findings text (from its LAST heading to the next round heading or EOF)."""
    heads = list(re.finditer(r"^## Review round (\d+)\b", body, re.MULTILINE | re.IGNORECASE))
    own = [h for h in heads if int(h.group(1)) == n]
    if not own:
        return ""
    start = own[-1].start()
    end = next((h.start() for h in heads if h.start() > start), len(body))
    return body[start:end]


def _list_blocks(section: str) -> dict[str, list[list[str]]]:
    out: dict[str, list[list[str]]] = {"approved_patches": [], "deferred_findings": []}
    for m in _LIST_HEADING_RE.finditer(section):
        rest = section[m.end():]
        stop = re.search(r"^#", rest, re.MULTILINE)
        out[m.group(1).lower()].append((rest[:stop.start()] if stop else rest).splitlines())
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _placeholder(text: str) -> bool:
    return not text or text.startswith("```") or text.lower().strip("-*() ") == "none" or set(text) <= set("|-: ")


def triage_lists(section: str) -> tuple[list[str], list[str], bool, bool, bool]:
    """(approved rows, deferred rows, both lists present, every approved line parsed, every deferred line parsed)."""
    blocks = _list_blocks(section)
    approved: list[str] = []
    deferred: list[str] = []
    approved_parsed = deferred_parsed = True
    for line in (x for block in blocks["approved_patches"] for x in block):
        text = line.strip()
        head = re.sub(r"^[-*|\s`]+", "", text).lstrip("*")
        if re.match(rf"^{_FINDING_ID}\b", head) and len(_cells(text)) > 1:
            approved.append(text)
        elif not (_placeholder(text) or re.match(r"finding_id\b", head, re.IGNORECASE)):
            approved_parsed = False
    for line in (x for block in blocks["deferred_findings"] for x in block):
        text = line.strip()
        head = re.sub(r"^[-*|\s`]+", "", text).lstrip("*")
        if re.match(rf"^{_FINDING_ID}\b", head):
            deferred.append(text)
        elif not _placeholder(text):
            deferred_parsed = False
    both = bool(blocks["approved_patches"]) and bool(blocks["deferred_findings"])
    return approved, deferred, both, approved_parsed, deferred_parsed


def effective_kind(rnd: dict) -> str:
    return rnd["kind"] or ("branch" if rnd["n"] == 1 else "close-out")


def close_out_rounds(rounds: list[dict]) -> int:
    """Triaged close-out rounds after the latest triaged branch round (what the round cap counts)."""
    triaged = [r for r in rounds if r["triaged"]]
    last_branch = max((r["n"] for r in triaged if effective_kind(r) == "branch"), default=0)
    return sum(1 for r in triaged if effective_kind(r) == "close-out" and r["n"] > last_branch)


def review_loop(record: list[str], findings_text: str) -> dict:
    """`{rounds, due, ended}` of the review loop.

    `rounds`: one dict per recorded round, in number order: `{n, kind, base, end, mode, triaged, finalized,
    approved, files, exit_candidate}`. `ended` (set only when the latest round is triaged and finalized) is
    `NEW_CODE_REASON`, the Loop exit, the no-patch reason or `LOOP_CAP_REASON`, in that priority; `due` is the
    next round, in auto mode only, while the loop has not ended. An `approved_patches` line that cannot be read
    may hide a patch, so such a latest round never ends the loop for having approved none."""
    lowered = findings_text.lower()
    index: dict[int, int] = {}
    for i, line in enumerate(record):
        if m := _RECORDED_ROUND_RE.search(line):
            index[int(m.group(1))] = i
    rounds = []
    unreadable: set[int] = set()
    for n in sorted(index):
        line = record[index[n]]
        reviewed = _REVIEWED_RE.search(line)
        section = round_section(findings_text, n)
        triaged = round_triaged(lowered, n)
        approved, deferred, both, approved_parsed, deferred_parsed = triage_lists(section)
        if not approved_parsed:
            unreadable.add(n)
        green = record_verdict(record[index[n] + 1:], ("phase-3", "tests"), GREEN, RED)
        nothing = triaged and both and approved_parsed and deferred_parsed and not approved and not deferred
        mode = _MODE_RE.search(section)
        rounds.append({
            "n": n, "kind": reviewed.group(1) if reviewed else None,
            "base": reviewed.group(2) if reviewed else None, "end": reviewed.group(3) if reviewed else None,
            "mode": mode.group(1) if mode else None, "triaged": triaged,
            "finalized": bool(green) or nothing, "approved": approved,
            "files": sorted({c[1] for c in map(_cells, approved) if len(c) > 1}),
            "exit_candidate": bool(_EXIT_CANDIDATE_RE.search(section))})
    state = {"rounds": rounds, "due": None, "ended": None}
    latest = rounds[-1] if rounds else None
    if not latest or not (latest["triaged"] and latest["finalized"]):
        return state
    n = latest["n"]
    if effective_kind(latest) == "new-code":
        state["ended"] = NEW_CODE_REASON
    elif not latest["approved"] and n not in unreadable:
        state["ended"] = f"Loop exit at round {n}" if latest["exit_candidate"] else f"{NO_PATCH_PREFIX}{n}"
    elif close_out_rounds(rounds) >= CLOSE_OUT_CAP:
        state["ended"] = LOOP_CAP_REASON
    elif latest["mode"] == "auto":
        state["due"] = n + 1
    return state


def findings_paths(vs: list[str], arts: str, *extra: str) -> list[str]:
    """The story's findings files in reading order: every `<id>-code-review-findings*.md` of `arts` and each
    `extra` path, once."""
    unique: dict[str, str] = {}
    for p in [*(p for v in vs for p in glob.glob(os.path.join(arts, f"{v}-code-review-findings*.md"))), *extra]:
        unique.setdefault(os.path.normcase(os.path.abspath(p)), p)
    return [unique[key] for key in sorted(unique)]


def findings_text_of(vs: list[str], arts: str, *extra: str) -> str:
    out = []
    for p in findings_paths(vs, arts, *extra):
        try:
            with open(p, encoding="utf-8", errors="replace") as handle:
                out.append(handle.read())
        except OSError:
            continue
    return "\n".join(out)


def _git_out(worktree: str, *args: str) -> tuple[int, str]:
    try:
        res = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return res.returncode, res.stdout.strip()


def _latest_halt(worktree: str, story_id: str, step: str) -> tuple[str, str] | None:
    """(sha, step) of the newest `wip: [Story <id>] auto-loop halted in <step>` commit on the branch."""
    rc, root = _git_out(worktree, "merge-base", base_branch(), "HEAD")
    if rc != 0 or not root:
        return None
    rc, log = _git_out(worktree, "log", "--format=%H\t%s", f"{root}..HEAD")
    if rc != 0:
        return None
    patterns = [re.compile(HALT_SUBJECT.format(id=re.escape(v))) for v in variants(story_id)]
    for entry in log.splitlines():
        sha, _, subject = entry.partition("\t")
        for pattern in patterns:
            m = pattern.match(subject)
            if m and m.group(1) == step:
                return sha, m.group(1)
    return None


def _rerun_after_halt(worktree: str, story_id: str, record: list[str]) -> tuple[str, int] | None:
    halt = _latest_halt(worktree, story_id, RERUN_STEP)
    if halt is None:
        return None
    at_halt: set[int] = set()
    for v in variants(story_id):
        rc, text = _git_out(worktree, "show", f"{halt[0]}:{specs_rel()}/{v}-record.md")
        if rc == 0:
            at_halt = recorded_rounds(text.splitlines())
            break
    n = max(at_halt, default=0)
    if n == 0 or max(recorded_rounds(record), default=0) > n:
        return None
    return halt[0], n


def _phase_2_green(line: str) -> bool:
    return line_has(line, ("phase-2", "tests"), GREEN, RED)


def _rerun_after_green(record: list[str]) -> int | None:
    last_round = last_green = None
    for i, line in enumerate(record):
        if _RECORDED_ROUND_RE.search(line):
            last_round = i
        if _phase_2_green(line):
            last_green = i
    n = max(recorded_rounds(record), default=0)
    if n == 0 or last_round is None or last_green is None or last_green < last_round:
        return None
    return n


def review_rerun_due(worktree: str, story_id: str, record: list[str]) -> int | None:
    """The round a resumed story owes, or None: n + 1 (n = highest recorded round) when the newest
    `ui-states-unchecked` halt commit has no later recorded round, or a phase-2 GREEN line follows the last
    recorded round. The renderer refuses to re-record a round, so the re-run never reuses n."""
    halt = _rerun_after_halt(worktree, story_id, record)
    if halt:
        return halt[1] + 1
    n = _rerun_after_green(record)
    return None if n is None else n + 1


def rerun_owed_by_green(worktree: str, story_id: str, record: list[str]) -> int | None:
    """The round a phase-2 GREEN owes (a whole-branch re-run), or None; a halt re-run wins over it."""
    if _rerun_after_halt(worktree, story_id, record):
        return None
    n = _rerun_after_green(record)
    return None if n is None else n + 1
