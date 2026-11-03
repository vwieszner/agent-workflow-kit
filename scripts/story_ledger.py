#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Project a story's phase ledger from the session journal + git + disk.

The ledger records POSITION, not content: which phases ran, which review round the
story is in, and where each phase's output lives. It is DERIVED on demand rather
than written at each boundary, so it cannot go stale — regenerate and it is
current by construction.

Sources, in order of authority:
  * git                            — commits (ground truth; sha is verifiable)
  * <story>-record.md              — approvals and test verdicts (see below)
  * filesystem                     — spec / findings / deferred-work existence
  * .workflow/state/journal/*.jsonl — phase dispatches, tool errors, interrupts
                                     (DESIGN §8 turn lines written by the session journal)

The record file holds ONLY what no hook can capture: a human approval, and a test
verdict that otherwise lives in an agent's return value and dies with the
conversation. Append-only, one line per fact, committed with the story, written by
`story_record.py append`:

    <config: paths.specs_dir>/<story>-record.md
    - 2026-01-01 checkpoint-1 spec approved
    - 2026-01-01 phase-2 tests GREEN — <labels> (14 tests)
    - 2026-01-01 checkpoint-3 merge approved

ABSENCE IS REPORTED, never omitted. A story with no test line reads as "no durable
record", not as silence that could be mistaken for green: the ledger must make a
missing handoff visible.

Rounds observed are derived from the journal: each review-workflow launch (a `workflow_run`
fact whose `workflow` is `config: review.workflow_name` or a `<name>.js` script path) is one
round, counted once per distinct `round` arg so a relaunch is not a second round; subagent
review-layer dispatches within `config: story_ledger.round_gap_min` minutes also form one round.
A workflow round is COMPLETE only once the record carries its `review round <n> recorded` line
(written by the review renderer); a launch without that line is reported in progress. Triage is
checked per round: once any round is recorded, only a `## Triage decisions` section inside the
latest recorded round's own `## Review round <n>` section counts.

After the Handoff-state rows the ledger prints, for a story that is neither merged nor done:
  * `- seed ACs due: AC<a>, ...` for each `### AC<n> — Seed:` spec heading no `seed ACs
    implemented: AC<a>, ...` record line names, once the `phase-2 tests` row is ok;
  * `- review loop: ...`, `- review round due: ...`, `- review loop ended: ...` (`review_loop`);
  * `- review re-run due: round <n+1> (...)` when the newest `ui-states-unchecked` halt WIP commit
    has no higher recorded round after it, or a phase-2 GREEN line follows the last recorded round.
None of them is a Handoff-state row: RESUMABLE and `story_record.py check` ignore them.

The `pre-merge gate` row is the land guard (`premerge_verdict`): a GREEN for the branch's current
gate tree on a base the base branch still has, a train GREEN, or a recorded exemption.

Completeness caveat, printed with every report: journals are per-session, so a
crashed session can leave a gap. This reports what it FOUND. Never treat an absent
phase as proof the phase did not run.

Journal contract (DESIGN §8): one JSON object per turn —
  {"ts": "<iso>", "session_id": "...", "tool_calls": [{"tool": "...", "input": ..., "ok": true}],
   "interrupted": false}
A subagent dispatch is a tool call whose `tool` is `Agent` / `Task` / `task`; its
`input` is either a dict carrying `subagent_type` (or `agent_type`/`agent`) and
`description`, or a string containing `subagent_type=<name>` (a leading `<name>:` is
also accepted). `ok: false` is a tool error. `interrupted: true` on the turn is an
interrupt.

Usage
-----
    uv run --no-project .workflow/scripts/story_ledger.py <story-id>
    uv run --no-project .workflow/scripts/story_ledger.py <story-id> --write <path.md>

    <story-id> matches the slot-registry / sprint-status key. Dotted and dashed forms
    are matched interchangeably. Exit code: always 0 (report tool).

Importable: `build(story_id) -> str` (report text; its `**Position:**` and
`**RESUMABLE` lines are consumed by session injectors), `variants`, `mentions`,
`resolve_roots`, `read_record`, `handoff_state`, `premerge_verdict`, `merge_approval`,
`spec_open_items`, `review_loop` (used by story_record.py, land_lease.py, merge_train.py and
the phase-dispatch guard).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _registry_lookup  # noqa: E402
from agent_roles import base_role  # noqa: E402

REVIEW_LAYERS = {"blind-hunter", "edge-case-hunter", "verification-gap", "acceptance-auditor"}
AGENT_TOOLS = {"Agent", "Task", "task"}
WORKFLOW_TOOLS = {"Workflow"}


def review_workflow() -> str:
    return str(wfconfig.get("review.workflow_name", "review"))


PHASE_LABEL = {
    "@review-workflow": "post-phase-2 (review + verification workflow)",
    "story-spec": "checkpoint-1 (spec drafted)",
    "story-impl": "phase-2 (implementation)",
    "findings-verifier": "post-phase-2b (verification)",
    "story-finalize": "phase-3 (finalize)",
    "story-rebase": "rebase",
    "story-diagnose": "pre-merge gate diagnosis",
}

# Suffixes of OTHER per-story artifacts that live in the same directory as the
# spec and therefore match a bare `<id>*.md` glob. The spec is identified by
# elimination, so every new per-story artifact MUST be added here (and to the
# twin list in story_setup.py) or it will be mistaken for the spec.
NON_SPEC_SUFFIXES = ("-record", "-auto-triage", "-ledger")

# The review renderer writes the record line and opens each round's findings section with the
# heading; both carry the round number.
_RECORDED_ROUND_RE = re.compile(r"\breview round (\d+) recorded\b", re.IGNORECASE)
_ROUND_HEADING_RE = re.compile(r"^## review round (\d+)\b", re.MULTILINE)   # lowercased text
_TRIAGE_HEADING_RE = re.compile(r"^## triage decisions", re.MULTILINE)        # lowercased text
_TRIAGE_LISTS = ("approved_patches", "deferred_findings", "dismissed_findings")
# Untracked repro / property tests a review round leaves for story-finalize (adopted or deleted there);
# they are not a half-applied patch. `config: review.repro_test_name_regex`.
DEFAULT_REPRO_NAME = r"^(test_(?:repro|pbt)_r\d+_\d+\.py|repro_r\d+_\d+\.test\.[jt]sx?|repro-r\d+-\d+\.spec\.[jt]s)$"


def repro_test_name() -> re.Pattern[str]:
    return re.compile(str(wfconfig.get("review.repro_test_name_regex", DEFAULT_REPRO_NAME)))


def is_review_workflow(fact: dict) -> bool:
    """A `workflow_run` fact carries the Workflow tool's `name`, or its `scriptPath`."""
    wf = str(fact.get("workflow") or "")
    name = review_workflow()
    return wf == name or re.split(r"[\\/]", wf)[-1].lower() == f"{name}.js".lower()


def workflow_round(fact: dict) -> str | None:
    """The `round` arg of a review-workflow launch, read from its journaled args."""
    m = re.search(r'"?round"?\s*[:=]\s*"?(\d+)', str(fact.get("desc", "")) + " " + str(fact.get("text", "")))
    return m.group(1) if m else None

NON_SPEC_INFIXES = ("code-review-findings",)


# ------------------------------------------------------------------ config access

def repo() -> str:
    return str(wfconfig.repo_root())


def specs_rel() -> str:
    return str(wfconfig.get("paths.specs_dir", "docs/implementation-artifacts"))


def base_branch() -> str:
    return str(wfconfig.get("git.base_branch", "development"))


def round_gap_min() -> int:
    return int(wfconfig.get("story_ledger.round_gap_min", 45))


def journal_dir() -> str:
    return str(wfconfig.workflow_dir() / "state" / "journal")


def _rel(p: str) -> str:
    """Path relative to the repo root when possible (cross-drive → absolute)."""
    try:
        return os.path.relpath(p, repo())
    except ValueError:
        return os.path.abspath(p)


def _abs(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(repo(), p)


# ------------------------------------------------------------------ id matching

def variants(story_id: str) -> list[str]:
    """7-20b -> ['7-20b', '7.20b']; 10.7 -> ['10-7', '10.7']."""
    a = story_id.strip()
    out = {a, a.replace(".", "-"), a.replace("-", ".")}
    return sorted(out)


def mentions(text: str, vs: list[str]) -> bool:
    """Boundary-aware id match.

    The id must not be preceded or followed by another id character, where `-`/`.`
    count as a boundary only when no DIGIT follows them: a digit after `-`/`.`
    continues the id (`7-5` must not match `7-5-11`); a letter after `-` starts a slug
    (`7-21-session-summary`) and still matches. A character attached directly also
    continues the id, so `7-20` does not match `7-20b`.
    """
    t = (text or "").lower()
    for v in vs:
        if re.search(rf"(?<![\w.-]){re.escape(v.lower())}(?!\w|[-.]\d)", t):
            return True
    return False


# ------------------------------------------------------------------ roots

def resolve_roots(vs: list[str], create: bool = False) -> tuple[str, str | None]:
    """(artifacts_dir, branch) for this story.

    An IN-FLIGHT story's spec, findings, deferred-work and record file live in its
    WORKTREE, and its commits are on the story branch — neither is reachable from the
    main checkout's base branch. Both resolve from the slot registry. A merged story
    falls back to the main checkout, where its files now live.

    `create` (writers only): when the story's worktree exists but its specs dir does not
    yet, create it rather than fall back. Otherwise a fact written before the spec lands
    goes to the main checkout, and once the worktree's specs dir appears every reader
    resolves there and reports the fact missing.
    """
    row = _registry_lookup.find(vs)
    if row and row.get("worktree_path"):
        wt = str(row["worktree_path"])
        arts = os.path.join(wt, specs_rel())
        if create and os.path.isdir(wt):
            os.makedirs(arts, exist_ok=True)
        if os.path.isdir(arts):
            return arts, (row.get("branch") or None)
    return os.path.join(repo(), specs_rel()), None


def _root_of(arts: str) -> str:
    """The checkout (worktree or main) that holds `arts`."""
    a = os.path.abspath(arts)
    rel = os.path.normpath(specs_rel())
    if a.endswith(os.sep + rel) or a.endswith("/" + rel.replace(os.sep, "/")):
        return a[: -len(rel)].rstrip("/\\") or a
    return repo()


# ------------------------------------------------------------------ journal

def _agent_from_input(inp) -> tuple[str | None, str]:
    """(agent_type, description) from an Agent/Task tool-call input summary."""
    if isinstance(inp, str):
        s = inp.strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return _agent_from_input(parsed)
        except (ValueError, TypeError):
            pass
        m = re.search(r"(?:subagent_type|agent_type|agent)\s*[=:]\s*[\"']?([\w.-]+)", s)
        if m:
            return m.group(1), s
        m = re.match(r"([a-z][\w-]*)\s*:", s)
        if m:
            return m.group(1), s
        return None, s
    if isinstance(inp, dict):
        at = inp.get("subagent_type") or inp.get("agent_type") or inp.get("agent") or inp.get("subagent")
        desc = inp.get("description") or inp.get("desc") or inp.get("prompt") or ""
        return (str(at) if at else None), str(desc)
    return None, str(inp or "")


def _facts_from_turn(turn: dict) -> list[dict]:
    """Flatten one DESIGN §8 turn line into per-call facts."""
    ts = str(turn.get("ts", ""))
    out: list[dict] = []
    calls = turn.get("tool_calls") or []
    if not isinstance(calls, list):
        calls = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "?")
        inp = call.get("input")
        text = inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False, default=str)
        if tool in AGENT_TOOLS:
            at, desc = _agent_from_input(inp)
            out.append({"ts": ts, "kind": "agent_spawned", "tool": tool,
                        "agent_type": base_role(at), "desc": desc, "text": text})
        elif tool in WORKFLOW_TOOLS:
            wf = ""
            if isinstance(inp, dict):
                wf = str(inp.get("name") or inp.get("scriptPath") or "")
            else:
                m = re.search(r"(?:name|scriptPath)\W+([\w./\\-]+)", str(inp or ""))
                wf = m.group(1) if m else ""
            out.append({"ts": ts, "kind": "workflow_run", "tool": tool, "workflow": wf,
                        "desc": text, "text": text})
        else:
            out.append({"ts": ts, "kind": "tool_call", "tool": tool, "text": text})
        if call.get("ok") is False:
            out.append({"ts": ts, "kind": "tool_error", "tool": tool, "text": text})
    if turn.get("interrupted"):
        joined = " ".join(f.get("text", "") for f in out)
        out.append({"ts": ts, "kind": "interrupted", "tool": "", "text": joined})
    return out


# path -> (mtime, [(blob, fact)]): one parse per journal file per process, so a caller building several
# stories' ledgers (the session-start position report) does not re-read every journal once per story.
_JOURNAL_CACHE: dict[str, tuple[float, list[tuple[str, dict]]]] = {}


def _journal_rows(fp: str) -> list[tuple[str, dict]] | None:
    try:
        mtime = os.path.getmtime(fp)
    except OSError:
        return None
    hit = _JOURNAL_CACHE.get(fp)
    if hit and hit[0] == mtime:
        return hit[1]
    rows: list[tuple[str, dict]] = []
    try:
        fh = open(fp, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            for f in _facts_from_turn(o):
                rows.append((" ".join(str(f.get(k, "")) for k in ("desc", "text")), f))
    _JOURNAL_CACHE[fp] = (mtime, rows)
    return rows


def load_facts(vs: list[str]) -> tuple[list[dict], int, int]:
    """Return (facts for this story, total facts scanned, journal files seen).

    Facts from a subagent's own journal (`<session>.sub-<agent>.jsonl`) are marked `_nested`: a dispatch made
    inside a phase agent is listed on its own and never sets the position."""
    facts, scanned, nfiles = [], 0, 0
    for fp in sorted(glob.glob(os.path.join(journal_dir(), "*.jsonl"))):
        nfiles += 1
        rows = _journal_rows(fp)
        if rows is None:
            continue
        scanned += len(rows)
        name = os.path.basename(fp)
        nested = ".sub-" in name
        caller = name.split(".sub-", 1)[1].rsplit(".jsonl", 1)[0] if nested else None
        for blob, f in rows:
            if mentions(blob, vs):
                facts.append(dict(f, _session=name[:8], _nested=nested, caller=caller))
    facts.sort(key=lambda f: f.get("ts", ""))
    return facts, scanned, nfiles


def parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def derive_rounds(spawns: list[dict]) -> list[list[dict]]:
    """Cluster review-layer dispatches into rounds by time gap."""
    gap = timedelta(minutes=round_gap_min())
    rounds: list[list[dict]] = []
    cur: list[dict] = []
    prev = None
    for s in spawns:
        t = parse_ts(s.get("ts", ""))
        if cur and prev and t and (t - prev) > gap:
            rounds.append(cur)
            cur = []
        cur.append(s)
        if t:
            prev = t
    if cur:
        rounds.append(cur)
    return rounds


# ------------------------------------------------------------------ record + handoff

def numbered_record_lines(path: str) -> list[tuple[int, str]]:
    """(file line number, stripped line) of each line of a record file whose first character is `-`, so an
    indented sub-bullet is not a record line; raises OSError."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [(n, ln.strip()) for n, ln in enumerate(fh, 1) if ln.startswith("-")]


def read_record(vs: list[str], arts: str) -> tuple[str | None, list[str]]:
    """Return (path, lines) of the story's append-only record file, if any."""
    for v in vs:
        p = os.path.join(arts, f"{v}-record.md")
        if os.path.isfile(p):
            try:
                lines = [line for _n, line in numbered_record_lines(p)]
            except OSError:
                lines = []
            return _rel(p), lines
    return None, []


# ------------------------------------------------------------------ land guard (pre-merge verdict)

_PREMERGE_TREE = re.compile(r"\bcode-tree ([0-9a-f]{7,40})")
_PREMERGE_BASE = re.compile(r"\bbase ([0-9a-f]{7,40})\b")
_PREMERGE_HEAD = re.compile(r"\bHEAD ([0-9a-f]{7,40})\b")
_TRAIN_IDS = re.compile(r"\btrain ([^\s/]+)\s*$")
MOVED_COMMITS_SHOWN = 10
_RERUN = "re-run the gate in the land window"


def _base_override_re() -> re.Pattern[str]:
    return re.compile(r"^pre-merge base accepted\s*(?:—|--|-)\s*" + re.escape(base_branch())
                      + r" ([0-9a-f]{7,40})\b", re.IGNORECASE)


def train_prefix() -> str:
    return str(wfconfig.get("git.train_prefix", "train/"))


def current_code_tree(worktree: str) -> str | None:
    """The worktree's `code-tree` hash, computed exactly as the gate records it (`run_scoped_tests.tested_tree`:
    the working `config: tests.code_root` subtree, untracked files included); None when it cannot be computed."""
    try:
        import run_scoped_tests
        return run_scoped_tests.tested_tree(worktree)[1]
    except Exception:
        return None


def _gate_inert(path: str) -> bool:
    """True when the gate never reads `path` (`config: pipeline.gate_inert_prefixes`, or a top-level `*.md`), unless
    `config: pipeline.gate_input_exceptions` names it. Every other path, new ones included, is a gate input."""
    if path in list(wfconfig.get("pipeline.gate_input_exceptions", []) or []):
        return False
    prefixes = tuple(wfconfig.get("pipeline.gate_inert_prefixes",
                                  ["docs/", ".workflow/", ".claude/", ".opencode/"]) or [])
    return (bool(prefixes) and path.startswith(prefixes)) or ("/" not in path and path.endswith(".md"))


def _fact_of(line: str) -> str:
    """The fact of a `- <date> <fact>` record line."""
    parts = line.lstrip("- ").split(None, 1)
    return parts[1] if len(parts) > 1 else ""


def merge_approval(record: list[str]) -> tuple[str | None, str]:
    """(approving line, "") when the latest `checkpoint-3` verdict approves the merge, else (None, reason).

    A verdict line is a record line whose fact starts with `checkpoint-3`; the latest one decides. It approves
    when it contains `approved` and neither `pending` nor `withdrawn`. A line that only mentions checkpoint-3
    elsewhere (a NEXT note) is not a verdict.
    """
    latest = next((ln for ln in reversed(record) if _fact_of(ln).lower().startswith("checkpoint-3")), None)
    if latest is None:
        return None, "the record has no checkpoint-3 verdict line"
    text = latest.lstrip("- ").strip()
    low = text.lower()
    if "approved" in low and not re.search(r"\b(?:pending|withdrawn)\b", low):
        return text, ""
    return None, f"the latest checkpoint-3 verdict is not an approval: {text}"


def _git_out(worktree: str, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 128, ""
    return r.returncode, r.stdout.strip()


def _branch_block(worktree: str, story_id: str) -> str | None:
    """The reason when the worktree is not on the story's branch, else None.

    The branch comes from the slot registry; where the registry does not know the story, only a train branch
    blocks and the story branch is derived from it."""
    rc, current = _git_out(worktree, "branch", "--show-current")
    if rc != 0:
        return f"git could not read the checked-out branch in {worktree}; the land guard fails closed"
    expected = resolve_roots(variants(story_id))[1]
    tp = train_prefix()
    if not expected:
        if not current.startswith(tp):
            return None
        expected = str(wfconfig.get("git.branch_prefix", "story/")) + current[len(tp):]
    if current == expected:
        return None
    return f"the worktree is on {current or 'a detached HEAD'}, not {expected}; check out {expected}"


def _moved_base_block(worktree: str, base_full: str, tip: str) -> str | None:
    """None when the base branch's movement since `base_full` leaves HEAD's gate inputs alone, else the reason."""
    bb = base_branch()
    rc_moved, moved_out = _git_out(worktree, "diff", "--name-only", "--no-renames", "-z", base_full, bb)
    rc_own, own_out = _git_out(worktree, "diff", "--name-only", "--no-renames", "-z", base_full, "HEAD")
    if rc_moved != 0 or rc_own != 0:
        return f"git could not diff the gated base against {bb} or HEAD; the land guard fails closed"
    moved = [p for p in moved_out.split("\0") if p]
    own = {p for p in own_out.split("\0") if p}
    blocker = next((f"{p} is read by the gate" for p in moved if not _gate_inert(p)), None) \
        or next((f"{p} changed on both {bb} and the branch" for p in moved if p in own), None)
    if blocker is None:
        return None
    _, log = _git_out(worktree, "log", "--format=%h %s", f"{base_full}..{bb}")
    commits = log.splitlines()
    listed = "".join(f"\n    {c}" for c in commits[:MOVED_COMMITS_SHOWN])
    more = (f"\n    (+{len(commits) - MOVED_COMMITS_SHOWN} more; {len(commits)} commits in total)"
            if len(commits) > MOVED_COMMITS_SHOWN else "")
    return (
        f"{bb} moved past the gated base: {base_full[:12]} → {tip[:12]}; commits in "
        f"{base_full[:12]}..{bb}:{listed}{more}\n"
        f"  {blocker}\n"
        f"  Remedy: hold the lease, rebase onto {bb} and re-run the gate in the land window; "
        f"or the owner records: pre-merge base accepted — {bb} {tip[:12]} (<reason>)")


def _currency(worktree: str, record: list[str], text: str, story_id: str,
              base_full: str, tip: str) -> tuple[str | None, str]:
    """(text, "") when the base branch's tip is `base_full`, or the owner accepted the tip, or the base branch's
    movement since `base_full` is gate-inert and off every path HEAD changed; else (None, reason)."""
    if tip == base_full:
        return text, ""
    override_re = _base_override_re()
    for other in record:
        override = override_re.match(_fact_of(other))
        if override and tip.startswith(override.group(1).lower()):
            return text, ""
    rc, _ = _git_out(worktree, "merge-base", "--is-ancestor", "HEAD", base_branch())
    if rc == 0:
        return None, (f"the branch is already merged into {base_branch()}; resume with "
                      f"the land-story skill: {story_id} --cleanup-only")
    reason = _moved_base_block(worktree, base_full, tip)
    return (text, "") if reason is None else (None, reason)


def _gated_base(worktree: str, base: str) -> tuple[str | None, str, str]:
    """(reason, full base sha, base-branch tip) -- the reason is set when the base is not usable."""
    bb = base_branch()
    rc, _ = _git_out(worktree, "merge-base", "--is-ancestor", base, bb)
    if rc == 1:
        return f"the gated base {base} is not an ancestor of {bb}; {_RERUN}", "", ""
    if rc != 0:
        return f"git could not compare base {base} with {bb}; the land guard fails closed", "", ""
    rc_tip, tip = _git_out(worktree, "rev-parse", bb)
    rc_base, base_full = _git_out(worktree, "rev-parse", base)
    if rc_tip != 0 or rc_base != 0 or not tip or not base_full:
        return f"git could not resolve {bb} or the gated base; the land guard fails closed", "", ""
    return None, base_full, tip


def _non_train_commits(worktree: str, base: str, ids: list[str]) -> str:
    """Diagnostic text: the first-parent commits in `<base>..R` (R = merge-base HEAD <base branch>) that touch a
    gate input and are not the merge of a story the train held. Empty when there are none or git cannot say."""
    rc, root = _git_out(worktree, "merge-base", "HEAD", base_branch())
    rc_base, base_full = _git_out(worktree, "rev-parse", base)
    if rc != 0 or rc_base != 0 or not root or not base_full:
        return ""
    _, log = _git_out(worktree, "log", "--first-parent", "--format=%H\t%s", f"{base_full}..{root}")
    prefix = re.escape(str(wfconfig.get("git.branch_prefix", "story/")))
    commits = []
    for entry in log.splitlines():
        sha, _, subject = entry.partition("\t")
        if any(re.search(rf"{prefix}{re.escape(story)}(?!\w|[-.]\d)", subject) for story in ids):
            continue
        rc_diff, out = _git_out(worktree, "diff", "--name-only", "--no-renames", "-z", f"{sha}^", sha)
        if rc_diff == 0 and all(_gate_inert(p) for p in out.split("\0") if p):
            continue
        commits.append(f"{sha[:8]} {subject}")
    if not commits:
        return ""
    listed = "".join(f"\n    {c}" for c in commits[:MOVED_COMMITS_SHOWN])
    more = f"\n    (+{len(commits) - MOVED_COMMITS_SHOWN} more)" if len(commits) > MOVED_COMMITS_SHOWN else ""
    return f"\n  commits not from the train in {base_full[:12]}..{root[:12]}:{listed}{more}"


def _train_verdict(worktree: str, record: list[str], line: str, text: str, story_id: str) -> tuple[str | None, str]:
    """A train GREEN (` on <train prefix>` in the line) passes when the tested commit resolves, HEAD's gate inputs
    equal its own, the gated base is known, and HEAD is current against the base branch measured from
    `merge-base HEAD <base branch>`."""
    tested, base = _PREMERGE_HEAD.search(line), _PREMERGE_BASE.search(line)
    ids = (m.group(1).split(",") if (m := _TRAIN_IDS.search(line.strip())) else [])
    if not tested:
        return None, f"the train GREEN carries no HEAD sha; {_RERUN}"
    sha = tested.group(1)
    rc, tested_full = _git_out(worktree, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if rc != 0 or not tested_full:
        return None, f"the gated train commit {sha} no longer resolves; {_RERUN}"
    rc, out = _git_out(worktree, "diff", "--name-only", "--no-renames", "-z", tested_full, "HEAD")
    if rc != 0:
        return None, "git could not diff the gated train against HEAD; the land guard fails closed"
    differing = [p for p in out.split("\0") if p and not _gate_inert(p)]
    if differing:
        more = f" (+{len(differing) - 1} more gate inputs)" if len(differing) > 1 else ""
        return None, (f"HEAD differs from the gated train in {differing[0]}{more}; {_RERUN}"
                      + (_non_train_commits(worktree, base.group(1), ids) if base else ""))
    if base is None:
        return None, f"the GREEN carries no base; {_RERUN}"
    reason, _, tip = _gated_base(worktree, base.group(1))
    if reason:
        return None, reason
    rc, root = _git_out(worktree, "merge-base", "HEAD", base_branch())
    if rc != 0 or not root:
        return None, f"git could not find the merge base of HEAD and {base_branch()}; the land guard fails closed"
    passed, reason = _currency(worktree, record, text, story_id, root, tip)
    if passed is None:
        return None, reason + _non_train_commits(worktree, base.group(1), ids)
    return passed, ""


def premerge_verdict(record: list[str], arts: str, story_id: str = "<id>") -> tuple[str | None, str]:
    """(passing line, "") when the latest `pre-merge gate` verdict lets the branch land, else (None, reason).

    A verdict line is `- <date> pre-merge gate …`; a line that only mentions the gate (a NEXT note) is not one.
    Whatever the verdict, the worktree must be on the story branch: land-story merges that ref, and a worktree
    left on a train branch would be judged by commits the merge never sees.
    `not required — <clause>` passes. A train GREEN (` on <train prefix>` in the line, written by
    `run_full_suite.py --train`) passes by `_train_verdict`; any other GREEN line passes only when:
      * its `code-tree` hash (written by `run_full_suite.py --record`) equals the worktree's current code tree
        (`current_code_tree`) — a gate run before a later amend or rebase tested other code;
      * it carries `base <sha>`, the base-branch commit the tested tree was based on, and that base is an
        ancestor of the base branch;
      * the base branch's tip is still that base, or every path it changed since that base is gate-inert
        (`_gate_inert`) and none of them is a path the branch also changed, or the record holds the owner's
        `pre-merge base accepted — <base branch> <sha12> (<reason>)` for the current tip.
    Any other latest verdict (a failure, a hand-written GREEN) blocks; so does a git error.
    """
    worktree = _root_of(arts)
    for line in reversed(record):
        low = line.lower()
        parts = low.lstrip("- ").split(None, 1)
        if len(parts) < 2 or not parts[1].startswith("pre-merge gate"):
            continue
        text = line.lstrip("- ").strip()
        wrong_branch = _branch_block(worktree, story_id)
        if wrong_branch:
            return None, wrong_branch
        if "not required" in low:
            return text, ""
        if "green" not in low:
            return None, f"the latest pre-merge gate verdict is not GREEN: {text}"
        if f" on {train_prefix().lower()}" in low:
            return _train_verdict(worktree, record, line, text, story_id)
        tree = _PREMERGE_TREE.search(line)
        if not tree:
            return None, f"the latest pre-merge gate GREEN carries no code-tree hash; {_RERUN}"
        head_tree = current_code_tree(worktree)
        if not head_tree:
            return None, f"the code tree of {worktree} could not be computed; the land guard fails closed"
        if not (head_tree.startswith(tree.group(1)) or tree.group(1).startswith(head_tree)):
            return None, (f"the tested code-tree {tree.group(1)} differs from the worktree's {head_tree[:12]}: "
                          f"the branch changed after the gate; {_RERUN}")
        found = _PREMERGE_BASE.search(line)
        if not found:
            return None, f"the GREEN carries no base; {_RERUN}"
        reason, base_full, tip = _gated_base(worktree, found.group(1))
        if reason:
            return None, reason
        return _currency(worktree, record, text, story_id, base_full, tip)
    return None, "no pre-merge gate verdict line in the record"


def premerge_fact(record: list[str], arts: str) -> str | None:
    """The latest `pre-merge gate` verdict line, if it lets the branch land (see `premerge_verdict`)."""
    return premerge_verdict(record, arts)[0]


# ------------------------------------------------------------------ verdict lines

_GREEN = ("green", "passed", "pass")       # a `phase-<n> tests` row's positive words, matched in lowercase
_RED = ("red", "fail", "error", "pending", "not run")   # and its negative words, matched as whole words


def _line_has(line: str, keys: tuple[str, ...], positive: tuple[str, ...] = (),
              negative: tuple[str, ...] = ()) -> bool:
    """Whether a record line carries every one of `keys`, one of `positive` when given, and none of `negative`,
    in any case.

    A negative matches only as a whole word, optionally inflected (-s/-ed/-ing/-ure/-ures; the last letter may
    double before -ed/-ing, so `not running` counts): `FAILED` and `errors` count, but `red` inside
    `sharedRoster` or `rendered` and `error` inside `test_error_handling` do not."""
    low = line.lower()
    if not all(k in low for k in keys):
        return False
    if any(re.search(rf"\b{re.escape(n)}(?:s|{re.escape(n[-1])}?(?:ed|ing)|ure|ures)?\b", low) for n in negative):
        return False
    return not positive or any(p in low for p in positive)


def record_verdict(record: list[str], keys: tuple[str, ...], positive: tuple[str, ...] = (),
                   negative: tuple[str, ...] = ()) -> str | None:
    """The first record line carrying `keys` and a POSITIVE verdict (`_line_has`). Keyword presence alone is not
    a verdict: `phase-2 tests RED` never satisfies the row."""
    for line in record:
        if _line_has(line, keys, positive, negative):
            return line.lstrip("- ").strip()
    return None


# ------------------------------------------------------------------ review loop

def recorded_rounds(record: list[str]) -> set[int]:
    """Round numbers that have a `review round <n> recorded` record line."""
    return {int(m.group(1)) for line in record if (m := _RECORDED_ROUND_RE.search(line))}


def round_triaged(body: str, n: int) -> bool:
    """True if round n's own section of a lowercased findings file holds triage.

    The section runs from the LAST `## Review round n` heading (a relaunched round appends a fresh section,
    which needs fresh triage) to the next `## Review round` heading or EOF. Inside it, a `## Triage decisions`
    heading must be followed by one of the three lists Checkpoint 2 persists.
    """
    heads = list(_ROUND_HEADING_RE.finditer(body))
    own = [h for h in heads if int(h.group(1)) == n]
    if not own:
        return False
    start = own[-1].end()
    end = next((h.start() for h in heads if h.start() > start), len(body))
    section = body[start:end]
    t = _TRIAGE_HEADING_RE.search(section)
    return bool(t) and any(k in section[t.end():] for k in _TRIAGE_LISTS)


_REVIEWED_RE = re.compile(r"; reviewed (branch|close-out|new-code) ([0-9a-f]{40})\.\.([0-9a-f]{40})")
_MODE_RE = re.compile(r"^- Diff: .*?; mode (\w+)", re.MULTILINE)
_EXIT_CANDIDATE_RE = re.compile(r"^- Loop exit: candidate", re.MULTILINE)
_LIST_HEADING_RE = re.compile(r"^###\s+(approved_patches|deferred_findings)\b.*$", re.MULTILINE | re.IGNORECASE)
_FINDING_ID = r"R\d+-\d+[a-z]?"
NO_PATCH_PREFIX = "no patch approved in round "
NEW_CODE_REASON = "new-code round: no further review round"


def close_out_cap() -> int:
    return int(wfconfig.get("review.close_out_cap", 2))


def loop_cap_reason() -> str:
    return f"round cap: {close_out_cap()} close-out rounds after the branch review"


def round_section(body: str, n: int) -> str:
    """Round n's own section of the findings file (from its LAST heading to the next round heading or EOF)."""
    heads = list(re.finditer(r"^## Review round (\d+)\b", body, re.MULTILINE | re.IGNORECASE))
    own = [h for h in heads if int(h.group(1)) == n]
    if not own:
        return ""
    start = own[-1].start()
    end = next((h.start() for h in heads if h.start() > start), len(body))
    return body[start:end]


def _list_blocks(section: str) -> dict[str, list[list[str]]]:
    """The lines of every `### approved_patches` / `### deferred_findings` block of a round section."""
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


def _triage_lists(section: str) -> tuple[list[str], list[str], bool, bool, bool]:
    """(approved rows, deferred rows, both lists present, every approved line parsed, every deferred line
    parsed); a placeholder line (blank, `none`, a fence, a table rule) counts as parsed."""
    blocks = _list_blocks(section)
    approved: list[str] = []
    deferred: list[str] = []
    approved_parsed = deferred_parsed = True
    for line in (ln for block in blocks["approved_patches"] for ln in block):
        text = line.strip()
        head = re.sub(r"^[-*|\s`]+", "", text).lstrip("*")
        if re.match(rf"^{_FINDING_ID}\b", head) and len(_cells(text)) > 1:
            approved.append(text)
        elif not (_placeholder(text) or re.match(r"finding_id\b", head, re.IGNORECASE)):
            approved_parsed = False
    for line in (ln for block in blocks["deferred_findings"] for ln in block):
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
    """The triaged close-out rounds after the latest triaged branch round: what the round cap counts. A new-code
    round is no close-out round and no branch round, so it neither counts nor restarts the count."""
    triaged = [r for r in rounds if r["triaged"]]
    last_branch = max((r["n"] for r in triaged if effective_kind(r) == "branch"), default=0)
    return sum(1 for r in triaged if effective_kind(r) == "close-out" and r["n"] > last_branch)


def review_loop(record_lines: list[str], findings_text: str) -> dict:
    """The review loop's state from the record lines and the findings file: `{rounds, due, ended}`.

    `rounds` holds one dict per recorded round (the latest record line of each number), in number order:
    `{n, kind, base, end, mode, triaged, finalized, approved, files, exit_candidate}`. `kind` (`branch`,
    `close-out` or `new-code`), `base`, `end` come from the record line's `reviewed` range (None when absent; such a
    round counts as a branch round when it is round 1 and as a close-out round otherwise). A round is finalized
    when a `phase-3 … GREEN` line follows its record line, or when its triage approved no patch and deferred no
    finding (both lists present, every line read, both empty). `ended` is the new-code round, the Loop exit, the
    no-patch reason or the close-out round cap (`config: review.close_out_cap`), in that priority; `due` is the
    next round, in auto mode only and while the loop has not ended. Both are set only when the latest round is
    triaged and finalized. An unreadable `approved_patches` line may hide an approved patch, so a latest round with
    one never ends the loop for having approved none."""
    body = findings_text
    lowered = body.lower()
    index: dict[int, int] = {}
    for i, line in enumerate(record_lines):
        if m := _RECORDED_ROUND_RE.search(line):
            index[int(m.group(1))] = i
    rounds = []
    unreadable: set[int] = set()
    for n in sorted(index):
        line = record_lines[index[n]]
        reviewed = _REVIEWED_RE.search(line)
        section = round_section(body, n)
        triaged = round_triaged(lowered, n)
        approved, deferred, both, approved_parsed, deferred_parsed = _triage_lists(section)
        if not approved_parsed:
            unreadable.add(n)
        green = record_verdict(record_lines[index[n] + 1:], ("phase-3", "tests"), _GREEN, _RED)
        nothing = triaged and both and approved_parsed and deferred_parsed and not approved and not deferred
        mode = _MODE_RE.search(section)
        rounds.append({
            "n": n, "kind": reviewed.group(1) if reviewed else None,
            "base": reviewed.group(2) if reviewed else None, "end": reviewed.group(3) if reviewed else None,
            "mode": mode.group(1) if mode else None, "triaged": triaged,
            "finalized": bool(green) or nothing, "approved": approved,
            "files": sorted({c[1] for c in map(_cells, approved) if len(c) > 1}),
            "exit_candidate": bool(_EXIT_CANDIDATE_RE.search(section))})
    state: dict = {"rounds": rounds, "due": None, "ended": None}
    latest = rounds[-1] if rounds else None
    if not latest or not (latest["triaged"] and latest["finalized"]):
        return state
    n = latest["n"]
    if effective_kind(latest) == "new-code":
        state["ended"] = NEW_CODE_REASON
    elif not latest["approved"] and n not in unreadable:
        state["ended"] = f"Loop exit at round {n}" if latest["exit_candidate"] else f"{NO_PATCH_PREFIX}{n}"
    elif close_out_rounds(rounds) >= close_out_cap():
        state["ended"] = loop_cap_reason()
    elif latest["mode"] == "auto":
        state["due"] = n + 1
    return state


def findings_paths(vs: list[str], arts: str, *extra: str) -> list[str]:
    """The story's findings files in reading order: every `<id>-code-review-findings*.md` of `arts` and each
    `extra` path, once."""
    unique: dict[str, str] = {}
    for p in [*(p for v in vs for p in glob.glob(os.path.join(glob.escape(arts), f"{v}-code-review-findings*.md"))),
              *extra]:
        unique.setdefault(os.path.normcase(os.path.abspath(p)), p)
    return [unique[key] for key in sorted(unique)]


def findings_text_of(vs: list[str], arts: str, *extra: str) -> str:
    """The story's findings file(s), concatenated in `findings_paths` order."""
    out = []
    for p in findings_paths(vs, arts, *extra):
        try:
            with open(p, encoding="utf-8", errors="replace") as handle:
                out.append(handle.read())
        except OSError:
            continue
    return "\n".join(out)


# ------------------------------------------------------------------ handoff state

def handoff_state(vs: list[str], record: list[str], arts: str) -> list[tuple[str, bool, str]]:
    """The facts a cleared session needs. (label, present, detail)."""

    def has(*keys: str, positive: tuple[str, ...] = (), negative: tuple[str, ...] = ()) -> str | None:
        return record_verdict(record, keys, positive, negative)

    # Triage lives in the findings file, not the record. The section must carry actual decisions — an empty
    # `## Triage decisions` heading does not count. Once a review round is recorded, triage is per round: only
    # the latest RECORDED round's own `## Review round <n>` section counts, so an earlier round's decisions never
    # satisfy a later round. The whole-file rule applies only while no round is recorded.
    triage = None
    latest = max(recorded_rounds(record), default=None)
    for p in findings_paths(vs, arts):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                body = fh.read().lower()
        except OSError:
            continue
        if latest is not None:
            if _ROUND_HEADING_RE.search(body) and round_triaged(body, latest):
                triage = _rel(p)
            continue
        if _ROUND_HEADING_RE.search(body) or "## triage decisions" not in body:
            continue
        section = body.split("## triage decisions", 1)[1]
        if any(k in section for k in _TRIAGE_LISTS):
            triage = _rel(p)

    # With a round recorded, only the latest round's own finalize counts (a GREEN line after its record line, or a
    # triage with nothing to apply); before any round, any phase-3 GREEN line does.
    latest_round = (review_loop(record, findings_text_of(vs, arts))["rounds"] or [None])[-1]
    if latest_round is None:
        phase3 = has("phase-3", "tests", positive=_GREEN, negative=_RED)
    elif latest_round["finalized"]:
        at = max(i for i, line in enumerate(record) if (m := _RECORDED_ROUND_RE.search(line))
                 and int(m.group(1)) == latest_round["n"])
        phase3 = (record_verdict(record[at + 1:], ("phase-3", "tests"), _GREEN, _RED)
                  or f"round {latest_round['n']}: no patch approved and no finding deferred - nothing to finalize")
    else:
        phase3 = None

    rows: list[tuple[str, bool, str]] = []
    approval = has("checkpoint-1", positive=("approved",), negative=("pending",))
    spec_detail = approval or "NO DURABLE RECORD"
    if approval:
        _spec, still_open = spec_open_items(vs, record, arts)
        if still_open:
            approval = None
            spec_detail = f"approval recorded, but {len(still_open)} open item(s): {still_open[0]}"
    rows.append(("spec approved", bool(approval), spec_detail))
    for label, hit in (
        ("phase-2 tests", has("phase-2", "tests", positive=_GREEN, negative=_RED)),
        ("triage recorded", triage),
        ("phase-3 tests", phase3),
        ("pre-merge gate", premerge_verdict(record, arts, vs[0])[0]),
        ("merge approved", merge_approval(record)[0]),
    ):
        rows.append((label, bool(hit), hit or "NO DURABLE RECORD"))
    return rows


# ------------------------------------------------------------------ re-runs and seed ACs

RERUN_STEP = "ui-states-unchecked"
_HALT_SUBJECT = r"^wip: \[Story {id}\] auto-loop halted in (\S+)\s*$"


def _latest_halt(worktree: str, story_id: str, step: str) -> tuple[str, str] | None:
    """(sha, step) of the newest `wip: [Story <id>] auto-loop halted in <step>` commit naming `step` in
    `<merge-base with the base branch>..HEAD`, or None."""
    rc, root = _git_out(worktree, "merge-base", base_branch(), "HEAD")
    if rc != 0 or not root:
        return None
    rc, log = _git_out(worktree, "log", "--format=%H\t%s", f"{root}..HEAD")
    if rc != 0:
        return None
    patterns = [re.compile(_HALT_SUBJECT.format(id=re.escape(v))) for v in variants(story_id)]
    for entry in log.splitlines():
        sha, _, subject = entry.partition("\t")
        for pattern in patterns:
            m = pattern.match(subject)
            if m and m.group(1) == step:
                return sha, m.group(1)
    return None


def _rerun_after_halt(worktree: str, story_id: str, record: list[str]) -> tuple[str, int] | None:
    """(sha, n) of the newest `ui-states-unchecked` halt WIP commit, n being the highest `review round <n>
    recorded` line of the record at that commit, while the current record holds no higher round; else None."""
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
    """Whether a record line is a phase-2 GREEN line: one the `phase-2 tests` row accepts."""
    return _line_has(line, ("phase-2", "tests"), _GREEN, _RED)


def _rerun_after_green(record: list[str]) -> int | None:
    """n of the highest recorded round, when the record's last phase-2 GREEN line comes after its last
    `review round <k> recorded` line (the record is append-only: order is by position)."""
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
    """The review round a resumed story owes, or None: n + 1 (n the highest recorded round) when the newest
    `ui-states-unchecked` halt WIP commit has no later recorded round, or a phase-2 GREEN line was recorded after
    the last recorded round. The renderer refuses to re-record a round, so the re-run cannot reuse n."""
    halt = _rerun_after_halt(worktree, story_id, record)
    if halt:
        return halt[1] + 1
    n = _rerun_after_green(record)
    return None if n is None else n + 1


def rerun_owed_by_green(worktree: str, story_id: str, record: list[str]) -> int | None:
    """The review round a phase-2 GREEN owes (a whole-branch re-run), or None; a halt re-run wins over it."""
    if _rerun_after_halt(worktree, story_id, record):
        return None
    n = _rerun_after_green(record)
    return None if n is None else n + 1


SEED_HEADING = re.compile(r"^(### AC(\d+)[ \t]*(?:—|–|--|-)[ \t]*Seed:)", re.MULTILINE)
# The leading comma-separated list of `AC<n>` tokens after the phrase; `[\d.]*` keeps a dotted token such as
# `AC1.1` inside the list, so it cannot end it.
SEED_IMPLEMENTED = re.compile(r"seed acs implemented:\s*(ac\d[\d.]*(?:\s*,\s*ac\d[\d.]*)*)", re.IGNORECASE)
_SEED_TOKEN = re.compile(r"ac(\d+)\.?", re.IGNORECASE)


def _seed_acs_implemented(record: list[str]) -> set[int]:
    """The AC numbers named by `seed ACs implemented: AC<a>, AC<b> …` record lines. A line counts only when its
    fact starts with the phrase; a token names its AC only whole (`AC2`, or a sentence-final `AC2.`)."""
    named: set[int] = set()
    for line in record:
        m = SEED_IMPLEMENTED.match(_fact_of(line))
        if m:
            named.update(int(t.group(1)) for token in m.group(1).split(",")
                         if (t := _SEED_TOKEN.fullmatch(token.strip())))
    return named


def seed_headings(worktree: str, story_id: str) -> set[int]:
    """The numbers of the `### AC<n> — Seed:` headings in the story's spec under `worktree`; empty when the spec is
    absent or unreadable."""
    spec = _spec_path(variants(story_id), os.path.join(worktree, specs_rel()))
    if spec is None:
        return set()
    try:
        with open(spec, encoding="utf-8", errors="replace") as fh:
            return {int(m.group(2)) for m in SEED_HEADING.finditer(fh.read())}
    except OSError:
        return set()


def seed_acs_due(worktree: str, story_id: str, record: list[str]) -> list[int] | None:
    """The seed AC numbers a story owes, ascending (`[]` when none), or None when the spec holds no seed AC heading
    or `record` holds no phase-2 GREEN line. A heading is due exactly when no `seed ACs implemented:` line of
    `record` names its number. Only the spec and `record` are read — no git."""
    headings = seed_headings(worktree, story_id)
    if not headings or not any(_phase_2_green(line) for line in record):
        return None
    return sorted(headings - _seed_acs_implemented(record))


def corroborating_hints(vs: list[str], arts: str) -> list[str]:
    """Places a phase fact ALSO tends to be written, for the human to check.

    Advisory only: these never flip a RESUMABLE verdict or satisfy a dispatch gate —
    they stop the ledger from reporting "never happened" when it means "not in the
    record file".
    """
    hints: list[str] = []
    spec = next((p for lbl, p, ok in find_artifacts(vs, arts) if lbl == "spec" and ok), None)
    if spec:
        try:
            with open(_abs(spec), encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    low = line.lower()
                    if "checkpoint 1" in low and ("cleared" in low or "approved" in low):
                        hints.append(f"spec header says checkpoint-1 cleared — {spec}:{n}")
                        break
        except OSError:
            pass
    hist = str(wfconfig.path("paths.story_history_dir", "docs/implementation-artifacts/story-history"))
    if os.path.isdir(hist):
        for fn in sorted(os.listdir(hist)):
            if not fn.endswith(".md") or not mentions(os.path.splitext(fn)[0], vs):
                continue
            try:
                with open(os.path.join(hist, fn), encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
            except OSError:
                continue
            if "checkpoint 1" in txt.lower():
                hints.append(f"story-history mentions Checkpoint 1 — {_rel(os.path.join(hist, fn))}")
    return hints


# ------------------------------------------------------------------ git

def git(*args, cwd: str | None = None) -> str:
    try:
        return subprocess.run(["git", "-C", cwd or repo(), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _non_source_prefixes() -> list[str]:
    prefixes = list(wfconfig.get("story_ledger.non_source_prefixes", ["docs/"]))
    prefixes.append(specs_rel().replace("\\", "/").rstrip("/") + "/")
    return prefixes


def completion_state(vs: list[str], arts: str) -> tuple[str | None, str | None, str | None]:
    """(sprint_status, merge_commit, dirty_worktree) — ground truth that outranks dispatches.

    A dispatch is journaled; a completion, a merge and a sprint-status flip are not.
    These checks keep a merged story from reading as mid-review. `dirty_worktree`
    flags uncommitted SOURCE in the story worktree: a phase that died mid-patch
    leaves changes that a merge of committed history would silently drop.
    """
    status = None
    try:
        with open(wfconfig.path("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml"),
                  encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"\s{2,}([A-Za-z0-9][\w.\-]*)\s*:\s*([a-z][a-z-]*)", line)
                if m and mentions(m.group(1), vs):
                    status = m.group(2)
                    break
    except OSError:
        pass

    # Anchored on the pipeline's own merge-subject form, NOT a loose id mention: a
    # merge whose subject merely names the story must not mark it COMPLETE. Same
    # boundary rule as mentions() — a digit after -/. continues the id.
    merge = None
    prefix = str(wfconfig.get("git.branch_prefix", "story/"))
    merge_re = re.compile(
        r"^Merge\s+(branch\s+')?" + re.escape(prefix) + "(" + "|".join(re.escape(v) for v in vs)
        + r")(?!\w|[-.]\d)",
        re.IGNORECASE,
    )
    for line in git("log", base_branch(), "-400", "--merges", "--format=%h %s").splitlines():
        subject = line.split(" ", 1)[1] if " " in line else ""
        if merge_re.match(subject):
            merge = line
            break

    dirty = None
    wt = _root_of(arts)
    if os.path.exists(os.path.join(wt, ".git")) and os.path.abspath(wt) != os.path.abspath(repo()):
        # NOT .strip() on the whole blob — porcelain's first column is a space for
        # worktree-modified entries (" M path"); stripping would mangle line 1.
        out = git("status", "--porcelain", "--untracked-files=all", cwd=wt)
        # A mid-story worktree is always dirty in its docs (spec edits, the untracked
        # record file). Only uncommitted SOURCE is the half-applied-patch signal.
        paths = []
        for ln in out.splitlines():
            if len(ln) < 4:
                continue
            if ln.startswith("??") and repro_test_name().match(ln[3:].strip().strip('"').rsplit("/", 1)[-1]):
                continue
            p = ln[3:].strip().strip('"')
            if " -> " in p:          # rename: score the destination
                p = p.split(" -> ", 1)[1].strip().strip('"')
            if p:
                paths.append(p.replace("\\", "/"))
        skip = _non_source_prefixes()
        src = [p for p in paths if not any(p.startswith(s) for s in skip)]
        if src:
            dirty = (f"{wt} — {len(src)} uncommitted source path(s): "
                     f"{', '.join(src[:4])}{' …' if len(src) > 4 else ''}")
    return status, merge, dirty


def find_commits(vs: list[str], branch: str | None) -> tuple[list[str], str]:
    """(commits, how). Prefer the branch range — exact, and no false positives.

    `<base>..<branch>` is the story's own commits. A merged story has no branch, so
    the fallback greps commit SUBJECTS (never bodies) and marks every line unverified.
    """
    base = base_branch()
    if branch:
        out = [ln for ln in git("log", "--oneline", f"{base}..{branch}").splitlines() if ln]
        if out:
            return out, f"branch range {base}..{branch} (exact)"
    seen, out = set(), []
    for line in git("log", "-400", "--format=%h %s").splitlines():
        if not line or line[:8] in seen:
            continue
        subject = line.split(" ", 1)[1] if " " in line else ""
        if mentions(subject, vs):
            seen.add(line[:8])
            out.append(f"{line}   [? subject match — not proof this is the story's commit]")
    return out, (f"subject grep on {base} — NOT the story's branch range; each line is a "
                 "candidate, not a verified story commit")


def _is_spec(path: str) -> bool:
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    if any(x in stem for x in NON_SPEC_INFIXES):
        return False
    return not any(stem.endswith(sfx) for sfx in NON_SPEC_SUFFIXES)


def _spec_path(vs: list[str], arts: str) -> str | None:
    """The story's spec under `arts`: the first `<id>*.md` that is a spec (`_is_spec`) and names the story at an
    id boundary (`mentions`), so `6` never reads `60-*.md` and `9-3` never reads `9-3-1-*.md`."""
    for v in vs:
        for p in sorted(glob.glob(os.path.join(glob.escape(arts), f"{v}*.md"))):
            if _is_spec(p) and mentions(os.path.basename(p), vs):
                return p
    return None


def find_artifacts(vs: list[str], arts: str) -> list[tuple[str, str, bool]]:
    root = _root_of(arts)
    deferred = str(wfconfig.get("paths.deferred_work_dir", "docs/implementation-artifacts/deferred-work"))
    spec = _spec_path(vs, arts)
    rows = [("spec", _rel(spec) if spec else "(not found)", spec is not None)]
    for label, base, pattern in (
        ("findings", arts, "{v}*code-review-findings*.md"),
        ("deferred-work", os.path.join(root, deferred), "story-{v}.md"),
        ("record", arts, "{v}-record.md"),
        ("auto-triage", arts, "{v}-auto-triage.md"),
    ):
        hit = None
        for v in vs:
            for p in sorted(glob.glob(os.path.join(glob.escape(base), pattern.format(v=v)))):
                hit = _rel(p)
                break
            if hit:
                break
        rows.append((label, hit or "(not found)", bool(hit)))
    return rows


# An open question in a spec is a bracketed, numbered marker; a ruled one is written without brackets
# (`OPEN-Q 3 (a)`), and a mention of the convention (`[OPEN-Q <n>]`) carries no number. A story-spec run that
# stopped early leaves `spec-draft incomplete (<step>)`; a finished one `spec-draft complete (...)`.
OPEN_MARKER = re.compile(r"\[OPEN-Q (?P<num>\d+)(?P<rest>[^\]\n]*)\]")
DRAFT_KEY = "spec-draft"


def incomplete_spec_draft(record: list[tuple[int, str]]) -> tuple[int, str] | None:
    """The latest `spec-draft` line of `(n, line)` record pairs, unless it is `spec-draft complete`."""
    for n, line in reversed(record):
        parts = line.split(None, 2)
        words = parts[2].split() if len(parts) == 3 and line.startswith("- ") else []
        if words[:1] == [DRAFT_KEY]:
            return None if words[1:2] == ["complete"] else (n, line)
    return None


def spec_open_items(vs: list[str], record: list[str], arts: str) -> tuple[str | None, list[str]]:
    """(spec path, open items) that keep the spec from approval: a bracketed `[OPEN-Q …]` marker in the spec, or
    a latest `spec-draft` record line that is not `spec-draft complete`."""
    items: list[str] = []
    draft = incomplete_spec_draft(list(enumerate(record, 1)))
    if draft:
        items.append(f"record: {draft[1]}")
    spec = _spec_path(vs, arts)
    if spec is None:
        return None, items
    shown = _rel(spec)
    try:
        with open(spec, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if OPEN_MARKER.search(line):
                    items.append(f"{shown}:{n}: {line.strip()[:200]}")
    except OSError:
        items.append(f"{shown}: unreadable")
    return shown, items


# ------------------------------------------------------------------ report

def build(story_id: str) -> str:
    vs = variants(story_id)
    arts, branch = resolve_roots(vs)
    facts, scanned, nfiles = load_facts(vs)
    wf_name = review_workflow()
    nested_spawns = [f for f in facts if f.get("kind") == "agent_spawned" and f.get("_nested")]
    spawns = sorted(
        [f for f in facts if f.get("kind") == "agent_spawned" and not f.get("_nested")]
        + [dict(f, agent_type=wf_name) for f in facts
           if f.get("kind") == "workflow_run" and not f.get("_nested") and is_review_workflow(f)],
        key=lambda f: f.get("ts", ""),
    )
    review_spawns = [s for s in spawns if s.get("agent_type") in REVIEW_LAYERS]
    clusters = derive_rounds(review_spawns)
    # A round dispatches the layer set; a lone dispatch after a gap is a retry of one
    # failed layer, not a new round.
    rounds = [c for c in clusters if len({str(x.get("agent_type")) for x in c}) >= 2]
    retries = [c for c in clusters if c not in rounds]
    # One round per distinct `round` arg: relaunching a stopped run repeats its args.
    wf_rounds = {workflow_round(s) or s.get("ts", "") for s in spawns if s.get("agent_type") == wf_name}
    n_rounds = len(rounds) + len(wf_rounds)
    recorded = recorded_rounds(read_record(vs, arts)[1])
    finalizes = [s for s in spawns if s.get("agent_type") == "story-finalize"]
    errors = [f for f in facts if f.get("kind") in ("tool_error", "interrupted")]

    L = [f"# Story {story_id} — phase ledger", ""]

    # ---- position ----
    sprint, merge, dirty = completion_state(vs, arts)
    last = spawns[-1] if spawns else None
    if merge or (sprint in ("done", "merged")):
        bits = []
        if merge:
            bits.append(f"merged as {merge}")
        if sprint:
            bits.append(f"sprint-status: {sprint}")
        L.append(f"**Position:** COMPLETE — {' | '.join(bits)}. "
                 f"No phase remains; ignore any dispatch-derived position below.")
    elif last:
        last_type = str(last.get("agent_type") or "?")
        at = (f"review round {n_rounds}" if last_type in REVIEW_LAYERS or last_type == wf_name
              else PHASE_LABEL.get(last_type, last_type))
        nxt = {
            "story-spec": "checkpoint-1 approval, then phase-2",
            "story-impl": "layered-review (round 1)",
            wf_name: "checkpoint-2 triage of the latest round",
            "findings-verifier": "checkpoint-2 triage",
            "story-finalize": "the review loop line (a close-out round due, the loop ended, or the next-round "
                              "trigger check), then checkpoint-3",
            "story-rebase": "the step the rebase was for (record a NEXT: line when dispatching it)",
            "story-diagnose": "write the ## Gate fix section, then story-finalize applies it"
                              " (or relay the report to the owner)",
        }.get(last_type, "post-phase-2b verification, then checkpoint-2")
        # A record line carrying "NEXT:" is the human-authored next step and
        # outranks the dispatch-derived guess.
        recorded_next = next(
            (ln.split("NEXT:", 1)[1].strip() for ln in reversed(read_record(vs, arts)[1])
             if "NEXT:" in ln),
            None,
        )
        nxt_out = f"next (record): {recorded_next}" if recorded_next else f"next: {nxt}"
        # A launch is journaled when the workflow STARTS; the round is complete only once its
        # `review round <n> recorded` line exists.
        if last_type == wf_name:
            rnd = workflow_round(last)
            if rnd is None:
                at = f"review round {n_rounds} (round arg not in the journal; completion unverified)"
                nxt_out = "next: confirm the record's `review round <n> recorded` line, else relaunch layered-review"
            elif int(rnd) not in recorded:
                at = f"review round {rnd} in progress (launched, not recorded)"
                nxt_out = f"next: relaunch layered-review round {rnd}"
        retry_note = f" | layer retries: {len(retries)}" if retries else ""
        L.append(f"**Position:** {at} | rounds observed: {n_rounds}{retry_note} | "
                 f"story-finalize runs: {len(finalizes)} | {nxt_out}")
    else:
        L.append("**Position:** no phase dispatches found for this story in the journals")
    L += ["",
          f"_Projected from {len(facts)} matching facts ({scanned:,} scanned across {nfiles} session "
          f"journals) + git + disk. Journals are per-session, so a crashed session can leave a "
          f"gap: this reports what was FOUND, not a guarantee of completeness._",
          ""]

    # ---- phases ----
    L.append("## Phases observed")
    if not spawns:
        L.append("- (none)")
    else:
        emitted: set[int] = set()
        for s in spawns:
            at = str(s.get("agent_type") or "?")
            ts = (s.get("ts") or "")[:16].replace("T", " ")
            if at in REVIEW_LAYERS:
                for i, r in enumerate(clusters, 1):
                    if s in r and i not in emitted:
                        emitted.add(i)
                        layers = ", ".join(sorted({str(x.get("agent_type") or "?") for x in r}))
                        kind = (f"review round {rounds.index(r) + 1}" if r in rounds
                                else "layer retry")
                        L.append(f"- {ts}  {kind} — {layers} ({len(r)} dispatches)")
            else:
                label = PHASE_LABEL["@review-workflow"] if at == wf_name else PHASE_LABEL.get(at, at)
                L.append(f"- {ts}  {label} — {str(s.get('desc', ''))[:70]}")
    L.append("")
    if nested_spawns:
        L.append("## Nested dispatches (inside a phase agent)")
        for s in nested_spawns:
            ts = (s.get("ts") or "")[:16].replace("T", " ")
            L.append(f"- {ts}  {s.get('caller') or '?'} → {s.get('agent_type') or '?'} — "
                     f"{str(s.get('desc', ''))[:70]}")
        L.append("")

    # ---- commits ----
    commits, how = find_commits(vs, branch)
    L.append(f"## Commits (git — verifiable) — via {how}")
    L += [f"- {c}" for c in commits] or ["- (none found)"]
    L.append("")

    # ---- handoff state ----
    rec_path, rec_lines = read_record(vs, arts)
    rows = handoff_state(vs, rec_lines, arts)
    artifacts = find_artifacts(vs, arts)
    have = {label: ok for label, _path, ok in artifacts}

    # A fact is REQUIRED only once the pipeline has reached the phase that writes it.
    exact_branch = how.startswith("branch range")
    # Triage is demanded for the latest RECORDED round; when the only workflow rounds are
    # launched-but-unrecorded, no round has produced anything to triage.
    wf_launched = any(s.get("agent_type") == wf_name for s in spawns)
    required = {
        "spec approved": True,
        "phase-2 tests": exact_branch and len(commits) >= 1,
        "triage recorded": have.get("findings", False) and not (wf_launched and not recorded),
        "phase-3 tests": exact_branch and len(commits) >= 2,
        "pre-merge gate": False,  # follows the approval in the land window; land-story's gate enforces it
        "merge approved": bool(merge),
    }
    L.append("## Handoff state (what a cleared session needs)")
    for lbl, ok, detail in rows:
        if ok:
            mark, note = "ok ", detail
        elif required.get(lbl, True):
            mark, note = "!! ", detail
        else:
            mark, note = "-- ", "not yet reached (no artifact requires it yet)"
        L.append(f"- {mark} {lbl:<16} {note}")
    if not (merge or sprint in ("done", "merged")):
        wt = _root_of(arts)
        seed = seed_acs_due(wt, story_id, rec_lines)
        if seed:
            L.append(f"- seed ACs due: {', '.join(f'AC{n}' for n in seed)} "
                     f"(until a seed ACs implemented record line names them)")
        due = review_rerun_due(wt, story_id, rec_lines)
        halt = _rerun_after_halt(wt, story_id, rec_lines) if due else None
        loop_state = review_loop(rec_lines, findings_text_of(vs, arts))
        if loop_state["rounds"]:
            r = loop_state["rounds"][-1]
            rng = (f"{effective_kind(r)} {r['base'][:12]}..{r['end'][:12]}" if r["kind"]
                   else f"{effective_kind(r)} (no reviewed range)")
            L.append(f"- review loop: round {r['n']} {rng}; {'triaged' if r['triaged'] else 'not triaged'}; "
                     f"{'finalized' if r['finalized'] else 'not finalized'}; mode {r['mode']}")
            if loop_state["due"] and not due:
                L.append(f"- review round due: round {loop_state['due']} (close-out)")
            if loop_state["ended"]:
                L.append(f"- review loop ended: {loop_state['ended']}")
        if due:
            L.append(f"- review re-run due: round {due} (the latest halt, {halt[0][:12]}, was {RERUN_STEP} "
                     f"after review round {due - 1})" if halt else
                     f"- review re-run due: round {due} (a phase-2 GREEN was recorded after review round {due - 1})")
    L.append("")

    blocking = [lbl for lbl, ok, _ in rows if not ok and required.get(lbl, True)]
    if merge or sprint in ("done", "merged"):
        L.append("**RESUMABLE: n/a** — story is complete; handoff rows above are historical.")
    elif dirty:
        L.append(f"**RESUMABLE: NO — DIRTY WORKTREE.** {dirty}. A phase died mid-edit: "
                 f"the merge takes committed history only, so these changes would be "
                 f"silently dropped while the record still reads GREEN from the prior round. "
                 f"Inspect `git -C <worktree> status` before doing anything else.")
    elif blocking:
        L.append(f"**RESUMABLE: NO** — missing durable record for: {', '.join(blocking)}. "
                 f"Those facts exist only in the conversation; clearing loses them.")
    else:
        L.append("**RESUMABLE: YES** — every fact the pipeline has reached so far is recorded.")
    L.append("")
    if sprint:
        L.append(f"_sprint-status: **{sprint}**_")
    L.append("")
    if rec_path:
        L.append(f"_record file: {rec_path}_")
    else:
        L.append(f"_record file: none yet — create it with "
                 f"`uv run --no-project .workflow/scripts/story_record.py append {vs[0]} \"<fact>\"`._")
    # The verdict above stays single-source: it gates a dispatch. Corroboration is a
    # POINTER for the human, never a substitute for the record.
    hints = corroborating_hints(vs, arts)
    if hints and any(not ok for _lbl, ok, _d in rows):
        L.append("")
        L.append("_Corroboration found elsewhere (NOT a substitute for the record file — "
                 "backfill it with `story_record.py append`):_")
        L += [f"- {h}" for h in hints]
    L.append("")

    # ---- artifacts ----
    L.append("## Artifacts on disk")
    for label, path, ok in artifacts:
        L.append(f"- {'ok ' if ok else '-- '} {label:<14} {path}")
    L.append("")

    if errors:
        L.append(f"## Errors / interrupts touching this story ({len(errors)})")
        for e in errors[:10]:
            ts = (e.get("ts") or "")[:16].replace("T", " ")
            L.append(f"- {ts}  {e.get('kind')}  {str(e.get('tool', ''))} "
                     f"{str(e.get('text', ''))[:70]}")
        L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="project a story's phase ledger")
    ap.add_argument("story_id")
    ap.add_argument("--write", metavar="PATH", help="also write the ledger to PATH")
    args = ap.parse_args(argv)
    text = build(args.story_id)
    if args.write:
        os.makedirs(os.path.dirname(os.path.abspath(args.write)), exist_ok=True)
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"written: {args.write}\n")
    # Commit subjects may carry non-ASCII; a cp1252 console must not kill the report.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    try:
        print(text)
    except UnicodeEncodeError:
        enc = (getattr(sys.stdout, "encoding", None) or "utf-8")
        sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
