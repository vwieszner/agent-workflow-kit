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

Round number is derived, not recorded: consecutive review-layer dispatches within
`config: story_ledger.round_gap_min` minutes form one round.

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
`resolve_roots`, `read_record`, `handoff_state` (used by story_record.py).
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

REVIEW_LAYERS = {"blind-hunter", "edge-case-hunter", "acceptance-auditor"}
AGENT_TOOLS = {"Agent", "Task", "task"}
PHASE_LABEL = {
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
                        "agent_type": at, "desc": desc, "text": text})
        else:
            out.append({"ts": ts, "kind": "tool_call", "tool": tool, "text": text})
        if call.get("ok") is False:
            out.append({"ts": ts, "kind": "tool_error", "tool": tool, "text": text})
    if turn.get("interrupted"):
        joined = " ".join(f.get("text", "") for f in out)
        out.append({"ts": ts, "kind": "interrupted", "tool": "", "text": joined})
    return out


def load_facts(vs: list[str]) -> tuple[list[dict], int, int]:
    """Return (facts for this story, total facts scanned, journal files seen)."""
    facts, scanned, nfiles = [], 0, 0
    for fp in sorted(glob.glob(os.path.join(journal_dir(), "*.jsonl"))):
        nfiles += 1
        try:
            fh = open(fp, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
                for f in _facts_from_turn(o):
                    scanned += 1
                    blob = " ".join(str(f.get(k, "")) for k in ("desc", "text"))
                    if mentions(blob, vs):
                        f["_session"] = os.path.basename(fp)[:8]
                        facts.append(f)
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

def read_record(vs: list[str], arts: str) -> tuple[str | None, list[str]]:
    """Return (path, lines) of the story's append-only record file, if any."""
    for v in vs:
        p = os.path.join(arts, f"{v}-record.md")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    lines = [ln.strip() for ln in fh if ln.strip().startswith("-")]
            except OSError:
                lines = []
            return _rel(p), lines
    return None, []


def handoff_state(vs: list[str], record: list[str], arts: str) -> list[tuple[str, bool, str]]:
    """The facts a cleared session needs. (label, present, detail)."""

    def has(*keys: str, positive: tuple[str, ...] = (), negative: tuple[str, ...] = ()
            ) -> str | None:
        """Match a record line on `keys`, accepting only a POSITIVE verdict.

        Keyword presence alone is not a verdict: a line must carry one of `positive`
        and none of `negative` to satisfy the row (`phase-2 tests RED` never counts).

        A negative matches only as a whole word, optionally inflected
        (-s/-ed/-ing/-ure/-ures): `FAILED` and `errors` count, but `red` inside
        `sharedRoster` or `rendered` and `error` inside `test_error_handling` do not.
        """
        for line in record:
            low = line.lower()
            if not all(k in low for k in keys):
                continue
            if any(re.search(rf"\b{re.escape(n)}(?:s|ed|ing|ure|ures)?\b", low)
                   for n in negative):
                continue
            if positive and not any(p in low for p in positive):
                continue
            return line.lstrip("- ").strip()
        return None

    # Triage lives in the findings file, not the record. The section must carry
    # actual decisions — an empty `## Triage decisions` heading does not count.
    triage = None
    for v in vs:
        for p in glob.glob(os.path.join(arts, f"{v}*code-review-findings*.md")):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    body = fh.read().lower()
            except OSError:
                continue
            if "## triage decisions" not in body:
                continue
            section = body.split("## triage decisions", 1)[1]
            if any(k in section for k in ("approved_patches", "deferred_findings",
                                          "dismissed_findings")):
                triage = _rel(p)
    rows = []
    _GREEN = ("green", "passed", "pass")
    _RED = ("red", "fail", "error", "pending", "not run")
    for label, hit in (
        ("spec approved", has("checkpoint-1", positive=("approved",), negative=("pending",))),
        ("phase-2 tests", has("phase-2", "tests", positive=_GREEN, negative=_RED)),
        ("triage recorded", triage),
        ("phase-3 tests", has("phase-3", "tests", positive=_GREEN, negative=_RED)),
        ("merge approved", has("checkpoint-3", positive=("approved",), negative=("pending",))),
    ):
        rows.append((label, bool(hit), hit or "NO DURABLE RECORD"))
    return rows


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
        out = git("status", "--porcelain", cwd=wt)
        # A mid-story worktree is always dirty in its docs (spec edits, the untracked
        # record file). Only uncommitted SOURCE is the half-applied-patch signal.
        paths = []
        for ln in out.splitlines():
            if len(ln) < 4:
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


def find_artifacts(vs: list[str], arts: str) -> list[tuple[str, str, bool]]:
    root = _root_of(arts)
    deferred = str(wfconfig.get("paths.deferred_work_dir", "docs/implementation-artifacts/deferred-work"))
    rows = []
    for label, base, pattern in (
        ("spec", arts, "{v}*.md"),
        ("findings", arts, "{v}*code-review-findings*.md"),
        ("deferred-work", os.path.join(root, deferred), "story-{v}.md"),
        ("record", arts, "{v}-record.md"),
        ("auto-triage", arts, "{v}-auto-triage.md"),
    ):
        hit = None
        for v in vs:
            for p in sorted(glob.glob(os.path.join(base, pattern.format(v=v)))):
                if label == "spec" and not _is_spec(p):
                    continue
                hit = _rel(p)
                break
            if hit:
                break
        rows.append((label, hit or "(not found)", bool(hit)))
    return rows


# ------------------------------------------------------------------ report

def build(story_id: str) -> str:
    vs = variants(story_id)
    arts, branch = resolve_roots(vs)
    facts, scanned, nfiles = load_facts(vs)
    spawns = [f for f in facts if f.get("kind") == "agent_spawned"]
    review_spawns = [s for s in spawns if s.get("agent_type") in REVIEW_LAYERS]
    clusters = derive_rounds(review_spawns)
    # A round dispatches the layer set; a lone dispatch after a gap is a retry of one
    # failed layer, not a new round.
    rounds = [c for c in clusters if len({str(x.get("agent_type")) for x in c}) >= 2]
    retries = [c for c in clusters if c not in rounds]
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
        at = PHASE_LABEL.get(
            last_type,
            f"review round {len(rounds)}" if last_type in REVIEW_LAYERS else last_type,
        )
        nxt = {
            "story-spec": "checkpoint-1 approval, then phase-2",
            "story-impl": "layered-review (round 1)",
            "findings-verifier": "checkpoint-2 triage",
            "story-finalize": "round-2 trigger check, else checkpoint-3 (merge approval)",
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
        retry_note = f" | layer retries: {len(retries)}" if retries else ""
        L.append(f"**Position:** {at} | rounds observed: {len(rounds)}{retry_note} | "
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
                L.append(f"- {ts}  {PHASE_LABEL.get(at, at)} — {str(s.get('desc', ''))[:70]}")
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
    required = {
        "spec approved": True,
        "phase-2 tests": exact_branch and len(commits) >= 1,
        "triage recorded": have.get("findings", False),
        "phase-3 tests": exact_branch and len(commits) >= 2,
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
