#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Per-story harness cost / effort metrics from Claude Code transcripts (Phase 0.1 KPI tool).

For each `--story`, finds the sessions and subagent transcripts that worked on it and
reports, per phase and per agent type: input / output / cache-write 5m / cache-write 1h /
cache-read tokens, $ at current pricing (primary KPI), distinct agent spawns, wall-clock,
plus per-session main-thread human turns, API requests and peak context.

Pricing, requestId dedupe (last usage line wins) and the partial-usage output estimate
are imported from `session_cost_report.py`; this script adds attribution only.

WHERE THE DATA COMES FROM
-------------------------
* Journals `.workflow/state/journal/<session>.jsonl` (the session journal hook's facts). A fact
  names a story when its desc/command/path/text matches the story id with the boundary rule of
  `story_ledger.mentions` (dotted == dashed). Skill facts carry no arguments, so a skill
  invocation never names a story.
* Main transcripts `<project dir>/<session>.jsonl` (project dirs per `_transcripts.project_dirs()`),
  subagents under `<session>/subagents/agent-*.jsonl` + `.meta.json` (agentType, description,
  toolUseId, parentAgentId for depth-2 spawns).
* Saved-workflow runs (e.g. the review workflow): agents under
  `<session>/subagents/workflows/<runId>/agent-*.jsonl` + `.meta.json` (agentType), and the run
  file `<session>/workflows/<runId>.json`, whose `args.story_id` (or `result.story_id`) names the
  story the run worked on.
* Story worktree project dirs: the slug of the story's worktree path (exact, or followed by `-`):
  every session there ran with the story worktree as cwd.
* Denied tool calls: the user record carrying a `tool_result` has `toolDenialKind`.

ATTRIBUTION RULES (printed with every report; thresholds are flags)
---------------------------------------------------------------------
1. Candidate main sessions: a journal with >= 1 fact naming the story, the parent of a
   subagent whose description names it, or the parent of a workflow run whose story_id is it.
2. share = (weight of facts naming the story) / (weight of facts naming any story id
   found in sprint-status.yaml), where a fact naming k stories weighs 1/k for each.
   * DEDICATED  share >= --dedicated-share (default 0.75): main thread 100%.
   * SHARED     --incidental-share <= share < --dedicated-share: main thread x share.
   * INCIDENTAL share < --incidental-share: main thread excluded; its $ is printed as
     the excluded amount.
3. Subagents of a candidate session: description names the story -> 100% (any session
   class). Description names only other stories -> excluded. Description names none ->
   the parent's Agent prompt decides (the story must be the single most-mentioned id;
   a tie excludes); a prompt naming no story falls back to the session class
   (DEDICATED 100%, SHARED x share, INCIDENTAL excluded). Each subagent row records its
   basis. A workflow-run agent goes by its run file instead: story_id names the story ->
   100% (any session class); names another story -> excluded; no story_id -> rule 3 on its
   description, with no prompt. Its row records the run id.
4. Worktree-dir sessions: 100%, phase `security-review` when the first prompt is the
   headless "Review this change for security vulnerabilities" run, else `worktree-session`.
5. A requestId counted once across every file (global seen-set).
6. Window: for a merged story (`Merge <config: git.branch_prefix><id>` on `config: git.base_branch`), facts, main-thread
   requests, subagents and worktree sessions after merge time + --post-merge-hours
   (default 12) are ignored, so later sessions that only read about the story do not
   count. An unmerged story has no end bound.

PHASES
------
Subagents: by agentType through AGENT_PHASE (spec, impl, review, verify, finalize, rebase, gate;
anything else=other:<type>). Main thread of a DEDICATED session: each request takes the phase of
the latest earlier boundary in that transcript (Agent dispatch by agentType; Skill tool call or
typed slash command through SKILL_PHASE; a command containing a COMMAND_PHASE needle), labelled
`main@<phase>`. SHARED main threads are one row, `main (shared xN%)`.
`config: harness_metrics.agent_phase`, `.skill_phase` and `.command_phase` (tables name -> phase)
add to or replace the defaults.

DENIALS
-------
Every transcript counted above (same window, same attribution factor, deduped by tool_use_id)
also yields its denied tool calls, by `toolDenialKind`: permission-rule (a PreToolUse hook
deny, exit 2 or JSON, or a permission rule), automode-blocked (the auto-mode classifier),
user-rejected (a refused permission prompt, headless refusals included), automode-unavailable.
`interrupted` (session ended before the result) is not a denial. A denial is a REPEAT when
its call was issued in the first assistant message after an earlier denial's result, i.e.
the agent's answer to a refusal was refused too. Source: the hook script from the result
text, else its leading `[tag]`, else the classifier reason, else its first words.

CROSS-STORY SECTIONS (markdown, after the Summary)
--------------------------------------------------
* $ measured by phase: one line per story, its phases by $ measured, descending.
* Tool-call denials by agent type, all stories: every report's by-agent rows added up
  (spawns, denials per kind and source, repeats; weighted like $).
* With `--cut [TYPE=]WHEN` (repeatable): subagent denials per agent type before / after
  the cut. A subagent row is after when its first transcript line is at or after the cut
  of its agent type: the cut given for that exact TYPE, else for its base role
  (`agent_roles.base_role`), else the cut given without TYPE; a type with no cut is not
  listed. WHEN is an ISO time (UTC when it has no offset) or a commit, which stands for
  its committer time. Weighted like $; main threads are not split.
`--summary` prints the Summary and these sections only, without the per-story detail.

MEASURED vs ESTIMATED
---------------------
`$ measured` prices usage lines exactly. Requests whose final usage line never arrived
get an output top-up from historical means (`session_cost_report.MEAN_OUT`); that top-up
is the separate `$ est` column and is never folded into measured $.

Usage
-----
    uv run --no-project .workflow/scripts/harness_metrics.py --story 8-47 --story 8-30
    uv run --no-project .workflow/scripts/harness_metrics.py --story 6-20 --json > metrics.json
    uv run --no-project .workflow/scripts/harness_metrics.py --story 6-20 --dedicated-share 0.8
    uv run --no-project .workflow/scripts/harness_metrics.py --story 16-17 --story 3-2 --summary \
        --cut af181f31 --cut story-propagate=2026-10-07T19:56:39Z

Claude Code transcripts only. Read-only. Exit 0 on a report (even with no sessions found); 1 when
no project transcript dir or the journal dir is missing.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _transcripts as tr  # noqa: E402
import session_cost_report as scr  # noqa: E402
import story_ledger as sl  # noqa: E402
import wfconfig  # noqa: E402

base_role = tr.base_role
PROJECTS = tr.claude_root()


def project_glob(*parts: str) -> list[str]:
    """glob.glob of <project dir>/<parts...> over every project dir."""
    hits: list[str] = []
    for proj in tr.project_dirs():
        hits += glob.glob(os.path.join(proj, *parts))
    return hits

AGENT_PHASE = {
    "story-spec": "spec", "story-impl": "impl",
    "blind-hunter": "review", "edge-case-hunter": "review", "acceptance-auditor": "review",
    "verification-gap": "review", "ui-verifier": "review", "pbt-writer": "review", "test-adequacy": "review",
    "findings-verifier": "verify", "findings-evaluator": "verify", "repro-writer": "verify",
    "story-finalize": "finalize", "story-rebase": "rebase",
    "story-diagnose": "gate", "test-bat-runner": "gate",
    **(wfconfig.get("harness_metrics.agent_phase", {}) or {}),
}
SKILL_PHASE = {
    "parallel-dev-wave": "setup", "layered-review": "review", "code-review": "review",
    "land-story": "land", "post-merge-smoke": "land", "propagate-story-changes": "propagate",
    "create-story": "spec", "run-tests": "gate",
    **(wfconfig.get("harness_metrics.skill_phase", {}) or {}),
}
COMMAND_PHASE = tuple((wfconfig.get("harness_metrics.command_phase", {}) or {}).items()) + (
    ("run_full_suite.py", "gate"), ("story_setup.py", "setup"))
SECURITY_PROMPT = "Review this change for security vulnerabilities"
MEASURED_KEYS = ("in", "out", "cr", "cw5", "cw1", "advisor")
EST_KEYS = ("est_out", "est_advisor")
TOK_KEYS = ("in", "out", "cw5", "cw1", "cr")
DENIAL_KINDS = ("permission-rule", "automode-blocked", "user-rejected", "automode-unavailable")
NOT_A_DENIAL = {"interrupted"}


# ---------------------------------------------------------------- story ids

def short_id(key: str) -> str | None:
    """sprint-status key -> the id people type: 8-47-character-... -> 8-47, tq-21-gate-... -> tq-21."""
    toks = key.lower().split("-")
    out = []
    for i, t in enumerate(toks):
        if any(ch.isdigit() for ch in t):
            out.append(t)
        elif i == 0 and len(toks) > 1 and any(ch.isdigit() for ch in toks[1]):
            out.append(t)
        else:
            break
    return "-".join(out) if out and any(ch.isdigit() for ch in "".join(out)) else None


def known_ids(sprint_status: str, extra: list[str]) -> list[str]:
    ids = set(x.lower() for x in extra)
    try:
        with open(sprint_status, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"\s{2,}([A-Za-z0-9][\w.\-]*)\s*:\s*([a-z][a-z-]*)", line)
                if not m or m.group(1).startswith("epic-") or m.group(1).endswith("retrospective"):
                    continue
                sid = short_id(m.group(1))
                if sid:
                    ids.add(sid)
    except OSError:
        pass
    return sorted(ids)


class IdMatcher:
    """One alternation over every id variant, with the boundary rule of story_ledger.mentions."""

    def __init__(self, ids: list[str]):
        self.canon = {}
        for i in ids:
            for v in sl.variants(i):
                self.canon[v.lower()] = i
        alts = sorted(self.canon, key=len, reverse=True)
        self.rx = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(a) for a in alts) + r")(?!\w|[-.]\d)")

    def find(self, text: str) -> set[str]:
        return {self.canon[m.group(1)] for m in self.rx.finditer((text or "").lower())}

    def counts(self, text: str) -> collections.Counter:
        return collections.Counter(self.canon[m.group(1)] for m in self.rx.finditer((text or "").lower()))


# ---------------------------------------------------------------- transcripts

def ts_parse(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def find_main_transcript(sid: str) -> str | None:
    hits = sorted(project_glob(sid + ".jsonl"))
    return hits[0] if hits else None


def scan_main(path: str) -> dict:
    """Second pass over a main transcript: boundaries, Agent prompts, human turns, questions."""
    boundaries, prompts = [], {}
    human, asks = [], []
    first_user = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            ts = o.get("timestamp")
            if t == "user":
                c = (o.get("message") or {}).get("content")
                if first_user is None:
                    first_user = c if isinstance(c, str) else " ".join(
                        str(x.get("text", "")) for x in (c or []) if isinstance(x, dict))
                if (o.get("origin") or {}).get("kind") == "human" and not o.get("isMeta"):
                    human.append(ts)
                    if isinstance(c, str):
                        cm = re.search(r"<command-name>/?([\w:.-]+)</command-name>", c)
                        if cm and SKILL_PHASE.get(cm.group(1)):
                            boundaries.append((ts, SKILL_PHASE[cm.group(1)]))
            elif t == "queue-operation" and first_user is None and o.get("content"):
                first_user = str(o.get("content"))
            elif t == "assistant":
                for b in (o.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name, inp = b.get("name"), b.get("input") or {}
                    if name == "Agent":
                        prompts[b.get("id")] = str(inp.get("prompt", ""))
                        ph = AGENT_PHASE.get(base_role(str(inp.get("subagent_type") or "")))
                        if ph:
                            boundaries.append((ts, ph))
                    elif name == "Skill":
                        ph = SKILL_PHASE.get(str(inp.get("skill") or ""))
                        if ph:
                            boundaries.append((ts, ph))
                    elif name in ("Bash", "PowerShell"):
                        cmd = str(inp.get("command", ""))
                        for needle, ph in COMMAND_PHASE:
                            if needle in cmd:
                                boundaries.append((ts, ph))
                                break
                    elif name == "AskUserQuestion":
                        asks.append(ts)
    boundaries.sort(key=lambda x: x[0] or "")
    return {"boundaries": boundaries, "prompts": prompts, "human_ts": human,
            "ask_ts": asks, "first_user": (first_user or "")[:200]}


def agent_prompts(path: str) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"Agent"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                for b in (o.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent":
                        out[b.get("id")] = str((b.get("input") or {}).get("prompt", ""))
    except OSError:
        pass
    return out


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    return str(content or "")


def denial_source(kind: str, text: str) -> str:
    """What refused the call: hook script, leading [tag], classifier reason, or the first words."""
    body = text.strip()
    if kind == "automode-blocked":
        m = re.search(r"Reason: \[([^\]]+)\]", body)
        return f"classifier [{m.group(1)}]" if m else "classifier"
    body = re.sub(r"^PreToolUse:\w+ hook error:\s*", "", body)
    m = (re.match(r"\[[^\]]*?(\w+)\.py\]", body)
         or re.search(r"\(Hook: [^)]*?(\w+)\.py\)", body))
    if m:
        return m.group(1)
    m = re.match(r"\[([^\]]{1,40})\]", body)
    if m:
        return f"[{m.group(1)}]"
    return " ".join(body.split()[:5])[:40]


def denial_events(path: str) -> list[dict]:
    """Denied tool calls of one transcript, in order (see DENIALS in the module docstring)."""
    events, follows = [], set()
    armed, armed_msg = False, None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return events
    with fh:
        for line in fh:
            if '"tool_use"' not in line and "toolDenialKind" not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            msg = o.get("message") or {}
            if o.get("type") == "assistant":
                mid = msg.get("id") or o.get("requestId")
                for b in msg.get("content") or []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use") or not armed:
                        continue
                    if armed_msg is None:
                        armed_msg = mid
                    if mid == armed_msg:
                        follows.add(b.get("id"))
                    else:
                        armed = False
            elif o.get("type") == "user" and o.get("toolDenialKind"):
                kind = str(o["toolDenialKind"])
                if kind in NOT_A_DENIAL:
                    continue
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                        tid = b.get("tool_use_id")
                        events.append({"id": tid, "ts": o.get("timestamp"), "kind": kind,
                                       "source": denial_source(kind, result_text(b.get("content"))),
                                       "repeat": tid in follows})
                armed, armed_msg = True, None
    return events


def phase_at(ts, bounds: list) -> str:
    ph = "pre-dispatch"
    for bts, bph in bounds:
        if bts and ts and bts < ts:
            ph = bph
        else:
            break
    return ph


class Ledger:
    """Global requestId seen-set + accumulation buckets."""

    def __init__(self):
        self.seen: set = set()
        self.denials_seen: set = set()
        self.unpriced = collections.Counter()
        self.partial = 0

    def take_denials(self, events: list[dict]) -> list[dict]:
        kept = []
        for e in events:
            if e["id"] is not None:
                if e["id"] in self.denials_seen:
                    continue
                self.denials_seen.add(e["id"])
            kept.append(e)
        return kept

    def take(self, rows: list[dict], attributed: bool = True) -> list[dict]:
        kept = []
        for r in rows:
            rid = r.get("rid")
            if rid is not None:
                if rid in self.seen:
                    continue
                self.seen.add(rid)
            if attributed:
                for m in r.get("unpriced") or []:
                    self.unpriced[m] += 1
                if not r.get("final"):
                    self.partial += 1
            kept.append(r)
        return kept


def new_bucket() -> dict:
    return {"requests": 0, "tok": collections.Counter(), "adv_tok": collections.Counter(),
            "usd": collections.Counter(), "spawns": 0, "first": None, "last": None,
            "agent_seconds": 0.0, "denials": collections.Counter(), "denial_sources": collections.Counter(),
            "repeats": 0.0}


def add_rows(b: dict, rows: list[dict], factor: float) -> None:
    for r in rows:
        b["requests"] += 1
        for k, v in (r.get("tok") or {}).items():
            b["tok"][k] += v * factor
        for k, v in (r.get("adv_tok") or {}).items():
            b["adv_tok"][k] += v * factor
        for k, v in (r.get("cost") or {}).items():
            b["usd"][k] += v * factor
        t = r.get("ts")
        if t:
            b["first"] = min(b["first"] or t, t)
            b["last"] = max(b["last"] or t, t)


def add_denials(b: dict, events: list[dict], factor: float) -> None:
    for e in events:
        b["denials"][e["kind"]] += factor
        b["denial_sources"][e["source"]] += factor
        if e["repeat"]:
            b["repeats"] += factor


def span_s(first, last) -> float:
    a, b = ts_parse(first), ts_parse(last)
    return (b - a).total_seconds() if a and b else 0.0


def usd_measured(b) -> float:
    return sum(b["usd"].get(k, 0.0) for k in MEASURED_KEYS)


def usd_est(b) -> float:
    return sum(b["usd"].get(k, 0.0) for k in EST_KEYS)


# ---------------------------------------------------------------- per story

def merge_subject_re(story_id: str) -> re.Pattern:
    """The subject of the pipeline merge of a story: `Merge <prefix><id>` or `Merge branch '<prefix><id>'`."""
    vs = sl.variants(story_id)
    prefix = re.escape(str(wfconfig.get("git.branch_prefix", "story/")))
    return re.compile(r"^Merge\s+(branch\s+')?" + prefix + "(" + "|".join(re.escape(v) for v in vs)
                      + r")(?!\w|[-.]\d)", re.IGNORECASE)


def merge_time(target: str) -> tuple[str | None, str | None]:
    """(merge sha+subject, ISO commit time) of the story's pipeline merge on the base branch."""
    rx = merge_subject_re(target)
    for line in sl.git("log", str(wfconfig.get("git.base_branch", "main")), "-600", "--merges", "--format=%h\t%cI\t%s").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and rx.match(parts[2]):
            return f"{parts[0]} {parts[2][:80]}", parts[1]
    return None, None


def to_utc_iso(s: str | None) -> str | None:
    d = ts_parse(s) if s else None
    return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z") if d else None


def journal_shares(jdir: str, target: str, matcher: IdMatcher, window_end: str | None) -> dict:
    """session_id -> {'target': w, 'all': w, 'facts': n, 'target_facts': n}, facts up to window_end only."""
    vs = sl.variants(target)
    out = {}
    for fp in glob.glob(os.path.join(jdir, "*.jsonl")):
        sid = os.path.basename(fp)[:-6].split(".sub-")[0]
        tw = aw = 0.0
        nf = nt = 0
        for o in tr.journal_facts(fp):
            if window_end and str(o.get("ts") or "") > window_end:
                continue
            nf += 1
            blob = " ".join(str(o.get(k, "")) for k in ("desc", "command", "path", "text"))
            if not sl.mentions(blob, vs):
                ids = matcher.find(blob)
                if ids:
                    aw += 1.0
                continue
            ids = matcher.find(blob) | {target}
            nt += 1
            tw += 1.0 / len(ids)
            aw += 1.0
        if nt:
            d = out.setdefault(sid, {"target": 0.0, "all": 0.0, "facts": 0, "target_facts": 0})
            d["target"] += tw
            d["all"] += aw
            d["facts"] += nf
            d["target_facts"] += nt
    return out


def worktree_dirs(target: str) -> list[str]:
    dirs = []
    for v in sl.variants(target):
        key = tr.slug(wfconfig.worktree_path(v))
        for d in glob.glob(os.path.join(PROJECTS, key + "*")):
            base = os.path.basename(d)
            if base == key or base.startswith(key + "-"):
                dirs.append(d)
    return sorted(set(dirs))


def meta_named_sessions(target: str) -> set[str]:
    vs = sl.variants(target)
    sids = set()
    for mf in project_glob("*", "subagents", "*.meta.json"):
        try:
            meta = json.load(open(mf, encoding="utf-8"))
        except Exception:
            continue
        if sl.mentions(str(meta.get("description", "")), vs):
            sids.add(os.path.basename(os.path.dirname(os.path.dirname(mf))))
    return sids


def workflow_run_story(session_dir: str, run_id: str) -> str | None:
    """`story_id` of a saved-workflow run, from its run file `<session>/workflows/<runId>.json`."""
    try:
        with open(os.path.join(session_dir, "workflows", run_id + ".json"), encoding="utf-8") as fh:
            run = json.load(fh)
    except (OSError, ValueError):
        return None
    for part in ("args", "result"):
        block = run.get(part) if isinstance(run, dict) else None
        if isinstance(block, dict) and block.get("story_id"):
            return str(block["story_id"])
    return None


def workflow_named_sessions(target: str) -> set[str]:
    vs = sl.variants(target)
    sids = set()
    for rf in project_glob("*", "workflows", "*.json"):
        sess_dir = os.path.dirname(os.path.dirname(rf))
        story = workflow_run_story(sess_dir, os.path.basename(rf)[:-5])
        if story and sl.mentions(story, vs):
            sids.add(os.path.basename(sess_dir))
    return sids


def classify(share: float, dedicated: float, incidental: float) -> str:
    if share >= dedicated:
        return "DEDICATED"
    if share >= incidental:
        return "SHARED"
    return "INCIDENTAL"


def subagent_basis(meta: dict, prompt: str | None, target: str, matcher: IdMatcher,
                   klass: str, share: float) -> tuple[str, float]:
    desc_ids = matcher.find(str(meta.get("description", "")))
    if target in desc_ids:
        return ("description names story" + (" (+others)" if len(desc_ids) > 1 else ""), 1.0)
    if desc_ids:
        return ("description names other stories only", 0.0)
    if prompt:
        c = matcher.counts(prompt)
        if c:
            top = max(c.values())
            if c.get(target, 0) == top and list(c.values()).count(top) == 1:
                return ("prompt: story is the single most-mentioned id", 1.0)
            return ("prompt: another id mentioned as often or more", 0.0)
    if klass == "DEDICATED":
        return ("unnamed; dedicated session", 1.0)
    if klass == "SHARED":
        return ("unnamed; shared session x share", share)
    return ("unnamed; incidental session", 0.0)


def analyze_story(target: str, args, matcher: IdMatcher) -> dict:
    ledger = Ledger()
    merge, merged_at = merge_time(target)
    window_end = None
    if merged_at:
        window_end = to_utc_iso((ts_parse(merged_at) + datetime.timedelta(hours=args.post_merge_hours)).isoformat())
    shares = journal_shares(args.journal_dir, target, matcher, window_end)
    sessions = set(shares) | meta_named_sessions(target) | workflow_named_sessions(target)
    vs = sl.variants(target)
    by_phase = collections.defaultdict(new_bucket)
    by_agent = collections.defaultdict(new_bucket)
    session_rows, sub_rows = [], []
    ignored_after_window = []
    excluded_usd = 0.0
    all_first, all_last = None, None

    def in_window(rows: list[dict]) -> list[dict]:
        return [r for r in rows if not window_end or not r.get("ts") or r["ts"] <= window_end]

    def note_span(f, l):
        nonlocal all_first, all_last
        if f:
            all_first = min(all_first or f, f)
        if l:
            all_last = max(all_last or l, l)

    def load_meta(sp: str) -> dict:
        mf = sp[:-6] + ".meta.json"
        if os.path.exists(mf):
            try:
                with open(mf, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {}

    def count_agent(sid: str, sp: str, meta: dict, basis: str, f: float, run_id: str | None = None) -> None:
        a = scr.analyze_file(sp, is_sub=True)
        if window_end and a["first"] and a["first"] > window_end:
            return
        srows = ledger.take(a["req_rows"])
        dens = ledger.take_denials(in_window(denial_events(sp)))
        atype = meta.get("agentType", "?")
        phase = AGENT_PHASE.get(base_role(atype), f"other:{atype}")
        for bucket in (by_phase[phase], by_agent[atype]):
            add_rows(bucket, srows, f)
            add_denials(bucket, dens, f)
            bucket["spawns"] += 1
            bucket["agent_seconds"] += span_s(a["first"], a["last"]) * f
        note_span(a["first"], a["last"])
        sub_rows.append({"session": sid, "agent": os.path.basename(sp)[:-6], "agent_type": atype,
                         "phase": phase, "description": meta.get("description"),
                         "spawn_depth": meta.get("spawnDepth"), "basis": basis, "factor": round(f, 3),
                         "workflow_run": run_id,
                         "requests": len(srows), "peak_ctx": max((r["ctx"] for r in srows), default=0),
                         "denials": len(dens), "denial_repeats": sum(1 for e in dens if e["repeat"]),
                         "denial_sources": dict(collections.Counter(e["source"] for e in dens)),
                         "first": a["first"], "last": a["last"]})

    for sid in sorted(sessions):
        sh = shares.get(sid, {"target": 0.0, "all": 0.0, "facts": 0, "target_facts": 0})
        share = (sh["target"] / sh["all"]) if sh["all"] else 0.0
        klass = classify(share, args.dedicated_share, args.incidental_share)
        path = find_main_transcript(sid)
        row = {"session": sid, "class": klass, "share": round(share, 3),
               "target_fact_weight": round(sh["target"], 2), "story_fact_weight": round(sh["all"], 2),
               "journal_facts": sh["facts"], "transcript": path}
        if not path:
            row["note"] = "main transcript not found"
            session_rows.append(row)
            continue
        main = scr.analyze_file(path)
        if window_end and main["first"] and main["first"] > window_end:
            ignored_after_window.append(sid)
            continue
        scan = scan_main(path)
        rows = ledger.take(in_window(main["req_rows"]), attributed=klass != "INCIDENTAL")
        dens = ledger.take_denials(in_window(denial_events(path)))
        full = new_bucket()
        add_rows(full, rows, 1.0)
        factor = {"DEDICATED": 1.0, "SHARED": share}.get(klass, 0.0)
        row.update({
            "project_dir": os.path.basename(os.path.dirname(path)),
            "requests": len(rows),
            "denials": len(dens),
            "human_turns": sum(1 for t in scan["human_ts"] if not window_end or (t or "") <= window_end),
            "ask_user_question": sum(1 for t in scan["ask_ts"] if not window_end or (t or "") <= window_end),
            "peak_ctx": max((r["ctx"] for r in rows), default=0),
            "first": full["first"], "last": full["last"],
            "usd_full_measured": round(usd_measured(full), 4), "usd_full_est": round(usd_est(full), 4),
            "attributed_factor": round(factor, 3),
        })
        if klass == "INCIDENTAL":
            excluded_usd += usd_measured(full)
        elif klass == "DEDICATED":
            bounds = [b for b in scan["boundaries"] if b[0]]
            seg = collections.defaultdict(list)
            for r in rows:
                seg[phase_at(r["ts"], bounds)].append(r)
            for ph, rr in seg.items():
                add_rows(by_phase[f"main@{ph}"], rr, 1.0)
            for e in dens:
                add_denials(by_phase[f"main@{phase_at(e['ts'], bounds)}"], [e], 1.0)
            add_rows(by_agent["main (dedicated)"], rows, 1.0)
            add_denials(by_agent["main (dedicated)"], dens, 1.0)
            note_span(full["first"], full["last"])
        else:
            add_rows(by_phase[f"main (shared x{share:.0%})"], rows, factor)
            add_rows(by_agent["main (shared)"], rows, factor)
            add_denials(by_phase[f"main (shared x{share:.0%})"], dens, factor)
            add_denials(by_agent["main (shared)"], dens, factor)
            note_span(full["first"], full["last"])
        session_rows.append(row)

        # ---- subagents of this session
        sdir = os.path.join(os.path.dirname(path), sid, "subagents")
        sub_prompts = None
        for sp in sorted(glob.glob(os.path.join(sdir, "agent-*.jsonl"))):
            meta = load_meta(sp)
            tuid = meta.get("toolUseId")
            prompt = scan["prompts"].get(tuid)
            if prompt is None and meta.get("parentAgentId"):
                prompt = agent_prompts(os.path.join(sdir, f"agent-{meta['parentAgentId']}.jsonl")).get(tuid)
            if prompt is None and tuid:
                if sub_prompts is None:
                    sub_prompts = {}
                    for other in glob.glob(os.path.join(sdir, "agent-*.jsonl")):
                        sub_prompts.update(agent_prompts(other))
                prompt = sub_prompts.get(tuid)
            basis, f = subagent_basis(meta, prompt, target, matcher, klass, share)
            if f > 0:
                count_agent(sid, sp, meta, basis, f)

        # ---- agents of this session's saved-workflow runs
        for sp in sorted(glob.glob(os.path.join(sdir, "workflows", "*", "agent-*.jsonl"))):
            run_id = os.path.basename(os.path.dirname(sp))
            meta = load_meta(sp)
            run_story = workflow_run_story(os.path.join(os.path.dirname(path), sid), run_id)
            if run_story:
                basis, f = (("workflow run names story", 1.0) if sl.mentions(run_story, vs)
                            else ("workflow run names another story", 0.0))
            else:
                basis, f = subagent_basis(meta, None, target, matcher, klass, share)
            if f > 0:
                count_agent(sid, sp, meta, basis, f, run_id)

    # ---- story worktree project dirs (headless security reviews and other worktree-cwd sessions)
    for d in worktree_dirs(target):
        for mp in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            main = scr.analyze_file(mp)
            if window_end and main["first"] and main["first"] > window_end:
                ignored_after_window.append(os.path.basename(mp)[:-6])
                continue
            scan = scan_main(mp)
            rows = ledger.take(in_window(main["req_rows"]))
            dens = ledger.take_denials(in_window(denial_events(mp)))
            phase = "security-review" if scan["first_user"].startswith(SECURITY_PROMPT) else "worktree-session"
            for bucket in (by_phase[phase], by_agent[f"{phase} (headless)"]):
                add_rows(bucket, rows, 1.0)
                add_denials(bucket, dens, 1.0)
                bucket["agent_seconds"] += span_s(main["first"], main["last"])
            note_span(main["first"], main["last"])
            full = new_bucket()
            add_rows(full, rows, 1.0)
            session_rows.append({"session": os.path.basename(mp)[:-6], "class": "WORKTREE", "share": 1.0,
                                 "project_dir": os.path.basename(d), "requests": len(rows),
                                 "denials": len(dens),
                                 "human_turns": len(scan["human_ts"]), "ask_user_question": len(scan["ask_ts"]),
                                 "peak_ctx": max((r["ctx"] for r in rows), default=0),
                                 "first": main["first"], "last": main["last"],
                                 "usd_full_measured": round(usd_measured(full), 4),
                                 "usd_full_est": round(usd_est(full), 4), "attributed_factor": 1.0,
                                 "phase": phase, "transcript": mp})

    total = new_bucket()
    for b in by_phase.values():
        for k in ("tok", "adv_tok", "usd", "denials", "denial_sources"):
            total[k].update(b[k])
        total["requests"] += b["requests"]
        total["spawns"] += b["spawns"]
        total["repeats"] += b["repeats"]
    dedicated = [r for r in session_rows if r["class"] == "DEDICATED" and "requests" in r]
    return {
        "worktree_sessions": sum(1 for r in session_rows if r["class"] == "WORKTREE"),
        "story": target,
        "rules": {"dedicated_share": args.dedicated_share, "incidental_share": args.incidental_share,
                  "journal_dir": args.journal_dir, "projects_root": PROJECTS,
                  "merge": merge, "merged_at": merged_at, "post_merge_hours": args.post_merge_hours,
                  "window_end_utc": window_end},
        "ignored_after_window": ignored_after_window,
        "sessions": session_rows, "subagents": sub_rows,
        "by_phase": {k: export(v) for k, v in sorted(by_phase.items(), key=lambda kv: -usd_measured(kv[1]))},
        "by_agent": {k: export(v) for k, v in sorted(by_agent.items(), key=lambda kv: -usd_measured(kv[1]))},
        "total": export(total),
        "excluded_incidental_usd": round(excluded_usd, 4),
        "wall_clock": {"first": all_first, "last": all_last, "hours": round(span_s(all_first, all_last) / 3600, 2)},
        "main_dedicated": {
            "sessions": len(dedicated),
            "human_turns": sum(r["human_turns"] for r in dedicated),
            "ask_user_question": sum(r["ask_user_question"] for r in dedicated),
            "requests": sum(r["requests"] for r in dedicated),
            "peak_ctx_max": max((r["peak_ctx"] for r in dedicated), default=0),
        },
        "unpriced_requests": dict(ledger.unpriced),
        "partial_usage_requests": ledger.partial,
    }


def export(b: dict) -> dict:
    return {"requests": b["requests"], "spawns": b["spawns"],
            "tokens": {k: round(b["tok"].get(k, 0)) for k in TOK_KEYS},
            "advisor_tokens": {k: round(v) for k, v in b["adv_tok"].items() if v},
            "usd": {k: round(b["usd"].get(k, 0.0), 4) for k in MEASURED_KEYS},
            "usd_measured": round(usd_measured(b), 4), "usd_est_topup": round(usd_est(b), 4),
            "span_hours": round(span_s(b["first"], b["last"]) / 3600, 2),
            "agent_hours": round(b["agent_seconds"] / 3600, 2),
            "denials": {"total": round(sum(b["denials"].values()), 2), "repeats": round(b["repeats"], 2),
                        "by_kind": {k: round(v, 2) for k, v in b["denials"].items()},
                        "by_source": {k: round(v, 2) for k, v in b["denial_sources"].most_common()}},
            "first": b["first"], "last": b["last"]}


# ---------------------------------------------------------------- rendering

def fmt_tok(n) -> str:
    n = float(n)
    return f"{n / 1e6:.2f}M" if n >= 1e6 else (f"{n / 1e3:.0f}k" if n >= 1e3 else f"{n:.0f}")


def table(title: str, items: dict) -> list[str]:
    L = [f"### {title}", "",
         "| row | spawns | req | input | output | cw 5m | cw 1h | cache read | $ input | $ output | $ cw5m | $ cw1h "
         "| $ cache read | $ advisor | **$ measured** | $ est top-up | span h | agent h |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in items.items():
        t, u = v["tokens"], v["usd"]
        L.append(f"| {k} | {v['spawns']} | {v['requests']} | {fmt_tok(t['in'])} | {fmt_tok(t['out'])} | "
                 f"{fmt_tok(t['cw5'])} | {fmt_tok(t['cw1'])} | {fmt_tok(t['cr'])} | {u['in']:.2f} | {u['out']:.2f} | "
                 f"{u['cw5']:.2f} | {u['cw1']:.2f} | {u['cr']:.2f} | {u['advisor']:.2f} | **{v['usd_measured']:.2f}** | "
                 f"{v['usd_est_topup']:.2f} | {v['span_hours']} | {v['agent_hours']} |")
    return L + [""]


def fmt_n(x: float) -> str:
    return f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"


def denial_table(items: dict, title: str = "Tool-call denials by agent type") -> list[str]:
    L = [f"### {title}", "",
         "Kinds per `toolDenialKind`; repeat = the first call after a denial was denied too; weighted like $.", "",
         "| agent type | spawns | denials | per spawn | repeats | " + " | ".join(DENIAL_KINDS) + " | top sources |",
         "|---|---|---|---|---|" + "---|" * len(DENIAL_KINDS) + "---|"]
    for k, v in items.items():
        d = v["denials"]
        per = fmt_n(d["total"] / v["spawns"]) if v["spawns"] else "-"
        top = ", ".join(f"{s} {fmt_n(n)}" for s, n in list(d["by_source"].items())[:3])
        L.append(f"| {k} | {v['spawns']} | {fmt_n(d['total'])} | {per} | {fmt_n(d['repeats'])} | "
                 + " | ".join(fmt_n(d["by_kind"].get(kind, 0)) for kind in DENIAL_KINDS) + f" | {top} |")
    return L + [""]


def render(rep: dict) -> str:
    r = rep["rules"]
    tot = rep["total"]
    md = rep["main_dedicated"]
    L = [f"## Story {rep['story']}", "",
         f"**$ measured: {tot['usd_measured']:.2f}** (+ {tot['usd_est_topup']:.2f} estimated output top-up for "
         f"{rep['partial_usage_requests']} partial-usage requests) | requests {tot['requests']} | "
         f"agent spawns {tot['spawns']} | headless worktree sessions {rep['worktree_sessions']} | "
         f"wall-clock {rep['wall_clock']['first']} -> {rep['wall_clock']['last']} "
         f"({rep['wall_clock']['hours']} h) | excluded INCIDENTAL main threads: ${rep['excluded_incidental_usd']:.2f}",
         "",
         f"Main thread, DEDICATED sessions only: {md['sessions']} sessions, {md['human_turns']} human turns, "
         f"{md['ask_user_question']} AskUserQuestion calls, {md['requests']} API requests, peak context "
         f"{fmt_tok(md['peak_ctx_max'])} (per-session peak; a story spans several sessions because of /clear).",
         "",
         f"Rules: DEDICATED share >= {r['dedicated_share']}, SHARED >= {r['incidental_share']}, else INCIDENTAL "
         f"(share = fact weight naming the story / fact weight naming any story id; a fact naming k ids weighs 1/k). "
         f"Skill facts carry no arguments and never name a story. Window: facts, requests and subagents up to "
         f"{r['window_end_utc'] or 'now (story not merged)'} (merge {r['merge'] or 'none found'} at {r['merged_at']} "
         f"+ {r['post_merge_hours']} h). Ignored, started after the window: "
         f"{', '.join(x[:8] for x in rep['ignored_after_window']) or 'none'}.", ""]
    if rep["unpriced_requests"]:
        L += [f"**UNPRICED requests (counted at $0):** {rep['unpriced_requests']}", ""]
    L += ["### Sessions considered", "",
          "| session | dir | class | share | target/all fact weight | req | human turns | AskUserQuestion | "
          "peak ctx | $ full (measured) | factor | first | last |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in rep["sessions"]:
        if "requests" not in s:
            L.append(f"| {s['session'][:8]} | - | {s['class']} | {s['share']} | - | - | - | - | - | - | - | - | "
                     f"{s.get('note', '')} |")
            continue
        fw = (f"{s['target_fact_weight']}/{s['story_fact_weight']}" if "target_fact_weight" in s else "worktree cwd")
        L.append(f"| {s['session'][:8]} | {s.get('project_dir', '')} | {s['class']}"
                 f"{' (' + s['phase'] + ')' if s.get('phase') else ''} | {s['share']} | {fw} | {s['requests']} | "
                 f"{s['human_turns']} | {s['ask_user_question']} | {fmt_tok(s['peak_ctx'])} | "
                 f"{s['usd_full_measured']:.2f} | {s['attributed_factor']} | {(s['first'] or '')[:16]} | "
                 f"{(s['last'] or '')[:16]} |")
    L.append("")
    L += table("By phase", rep["by_phase"])
    L += table("By agent type", rep["by_agent"])
    L += denial_table(rep["by_agent"])
    basis = collections.Counter(x["basis"] for x in rep["subagents"])
    L += ["### Subagent attribution basis", ""] + [f"- {k}: {v}" for k, v in basis.most_common()] + [""]
    return "\n".join(L)


def summary(reps: list[dict]) -> str:
    L = ["## Summary", "",
         "| story | $ measured | $ est top-up | $ cache write (5m+1h) | $ cache read | $ input | $ output | $ advisor "
         "| spawns | denials (repeats) | dedicated main sessions | human turns | AskUserQuestion | peak main ctx "
         "| wall-clock h | excluded $ |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in reps:
        t, u, md = r["total"], r["total"]["usd"], r["main_dedicated"]
        L.append(f"| {r['story']} | **{t['usd_measured']:.2f}** | {t['usd_est_topup']:.2f} | {u['cw5'] + u['cw1']:.2f} | "
                 f"{u['cr']:.2f} | {u['in']:.2f} | {u['out']:.2f} | {u['advisor']:.2f} | {t['spawns']} | "
                 f"{fmt_n(t['denials']['total'])} ({fmt_n(t['denials']['repeats'])}) | "
                 f"{md['sessions']} | {md['human_turns']} | {md['ask_user_question']} | {fmt_tok(md['peak_ctx_max'])} | "
                 f"{r['wall_clock']['hours']} | {r['excluded_incidental_usd']:.2f} |")
    return "\n".join(L) + "\n"


def phase_lines(reps: list[dict]) -> list[str]:
    L = ["### $ measured by phase", "", "| story | phases, $ measured descending |", "|---|---|"]
    for r in reps:
        ph = ", ".join(f"{k} {v['usd_measured']:.2f}" for k, v in r["by_phase"].items() if v["usd_measured"] >= 0.005)
        L.append(f"| {r['story']} | {ph or '-'} |")
    return L + [""]


def aggregate_agents(reps: list[dict]) -> dict:
    """by_agent of every report added up, in the shape denial_table reads; most denials first."""
    agg: dict = {}
    for r in reps:
        for k, v in r["by_agent"].items():
            a = agg.setdefault(k, {"spawns": 0, "total": 0.0, "repeats": 0.0,
                                   "by_kind": collections.Counter(), "by_source": collections.Counter()})
            d = v["denials"]
            a["spawns"] += v["spawns"]
            a["total"] += d["total"]
            a["repeats"] += d["repeats"]
            a["by_kind"].update(d["by_kind"])
            a["by_source"].update(d["by_source"])
    return {k: {"spawns": a["spawns"],
                "denials": {"total": a["total"], "repeats": a["repeats"], "by_kind": dict(a["by_kind"]),
                            "by_source": dict(a["by_source"].most_common())}}
            for k, a in sorted(agg.items(), key=lambda kv: (-kv[1]["total"], kv[0]))}


def as_utc(t: datetime.datetime) -> datetime.datetime:
    return (t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)).astimezone(datetime.timezone.utc)


def parse_cut(value: str) -> tuple[str | None, datetime.datetime, str]:
    """`[TYPE=]WHEN` -> (agent type or None, UTC cut, label). WHEN: ISO time, else a commit's committer time."""
    typ, _, when = value.rpartition("=")
    when = when.strip()
    t = ts_parse(when) if when else None
    label = None
    if t is None and when and not when.startswith("-"):
        t = ts_parse(sl.git("log", "-1", "--format=%cI", when).strip())
        label = when
    if t is None:
        raise ValueError(f"--cut {value!r}: {when!r} is neither an ISO time nor a commit")
    t = as_utc(t)
    iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (typ.strip() or None), t, (f"{label} = {iso}" if label else iso)


def cut_for(atype: str, cuts: dict):
    return cuts.get(atype) or cuts.get(base_role(atype)) or cuts.get(None)


def split_lines(reps: list[dict], cuts: dict) -> list[str]:
    rows: dict = {}
    undated = 0
    for r in reps:
        for x in r["subagents"]:
            cut = cut_for(x["agent_type"], cuts)
            if cut is None:
                continue
            first = ts_parse(x["first"]) if x.get("first") else None
            if first is None:
                undated += 1
                continue
            row = rows.setdefault(x["agent_type"], {"cut": cut[1], "sides": {
                side: {"spawns": 0, "denials": 0.0, "repeats": 0.0, "sources": collections.Counter()}
                for side in ("before", "after")}})
            b = row["sides"]["after" if as_utc(first) >= cut[0] else "before"]
            f = x["factor"]
            b["spawns"] += 1
            b["denials"] += x["denials"] * f
            b["repeats"] += x["denial_repeats"] * f
            for src, n in (x.get("denial_sources") or {}).items():
                b["sources"][src] += n * f
    L = ["### Subagent denials before / after the cut", "",
         "A subagent is after when its first transcript line is at or after its agent type's cut. "
         "Weighted like $; main threads are not split; a type with no cut is not listed."
         + (f" {undated} subagent rows without a timestamp skipped." if undated else ""), "",
         "| agent type | cut | before: spawns | denials | per spawn | repeats | top sources "
         "| after: spawns | denials | per spawn | repeats | top sources |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, row in sorted(rows.items()):
        cells = []
        for side in ("before", "after"):
            b = row["sides"][side]
            per = fmt_n(b["denials"] / b["spawns"]) if b["spawns"] else "-"
            top = ", ".join(f"{s} {fmt_n(n)}" for s, n in b["sources"].most_common(2)) or "-"
            cells.append(f"{b['spawns']} | {fmt_n(b['denials'])} | {per} | {fmt_n(b['repeats'])} | {top}")
        L.append(f"| {k} | {row['cut']} | {cells[0]} | {cells[1]} |")
    return L + [""]


def cross_story(reps: list[dict], cuts: dict) -> str:
    L = phase_lines(reps) + denial_table(aggregate_agents(reps), "Tool-call denials by agent type, all stories")
    if cuts:
        L += split_lines(reps, cuts)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--story", action="append", required=True, help="story id (8-47, 7.5.11); repeatable")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="machine output instead of markdown")
    out.add_argument("--summary", action="store_true",
                     help="markdown with the Summary and the cross-story sections only")
    ap.add_argument("--cut", action="append", default=[], metavar="[TYPE=]WHEN",
                    help="before/after table of subagent denials at WHEN (ISO time, UTC when no offset, "
                         "or a commit); TYPE limits it to an agent type or base role; repeatable")
    ap.add_argument("--dedicated-share", type=float, default=0.75)
    ap.add_argument("--incidental-share", type=float, default=0.25)
    ap.add_argument("--post-merge-hours", type=float, default=12.0,
                    help="attribution window closes this long after the story's merge commit")
    ap.add_argument("--journal-dir", default=str(tr.journal_dir()))
    ap.add_argument("--sprint-status", default=str(wfconfig.path("paths.sprint_status",
                                                                  "docs/implementation-artifacts/sprint-status.yaml")))
    args = ap.parse_args()
    if args.cut and args.json:
        ap.error("--cut adds a markdown table; it does not apply to --json")
    cuts = {}
    for value in args.cut:
        try:
            typ, t, label = parse_cut(value)
        except ValueError as e:
            ap.error(str(e))
        cuts[typ] = (t, label)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    if not tr.project_dirs() or not os.path.isdir(args.journal_dir):
        sys.stderr.write(f"no transcript dir matches {tr.project_globs()} under {PROJECTS}, "
                         f"or journal dir {args.journal_dir} is missing\n")
        return 1
    stories = [s.strip().lower().replace(".", "-") for s in args.story]
    matcher = IdMatcher(known_ids(args.sprint_status, stories))
    reps = [analyze_story(s, args, matcher) for s in stories]
    if args.json:
        print(json.dumps(reps, indent=1, default=str))
    else:
        print("# Harness metrics\n")
        print(summary(reps))
        print(cross_story(reps, cuts))
        if not args.summary:
            for rep in reps:
                print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
