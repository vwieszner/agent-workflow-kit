#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Quality signals per auto-dev run and a comparison across configuration fingerprints.

Purpose
-------
`run` prints one driver run (the `auto_dev_run_report.py` document) plus, per story the run drove: where its
artifacts were read, its halts with the matching failure-log entry (`config: paths.auto_dev_failure_log`), review
findings by bar impact, fix rounds, pre-merge gate verdicts, the post-merge smoke result, later fixes to its landed code and
its cost. `compare` groups every fingerprinted run's sessions by configuration and sets quality, cost and time
beside each group. Both print one JSON document (`--json`, the form the control room calls) or markdown.

Sources, each read by the tool that already owns it: `sessions.jsonl` and the session streams
(`auto_dev_run_report.py`), the story's record and findings files (its worktree, else the main checkout, else
the preserved story branch (`config: git.branch_prefix` + id); `story_ledger.read_record`, `review_precision.summarize`), the journals
(`story_ledger.load_facts`), the failure log, git (`story_state.run`, timed), and
`harness_metrics.py --json` for story cost. Stdlib only; read-only apart from the cost cache.

Every named field is a section with `status` and `source`, as in `story_state.py`:
  "ok"    data under the section's keys;
  "none"  `reason`: read, and nothing exists (what was looked for);
  "error" `error`: the read failed (the function's name, then the exception or the command's exit and stderr).
One failing reader becomes its own section's error and never ends the report. Sections built from the cost
report carry `"format": "internal"`.

Cost cache. `<main>/.workflow/state/auto-dev-metrics/cost/<story>.json` holds `{computed_at, argv, report}`, the
raw `harness_metrics.py` report. Claude Code deletes session transcripts after its cleanup period, and the report is
built from them, so a report is kept: a *final* report (its `window_end_utc` is set and
earlier than `computed_at`; nothing after the window is read) is never recomputed. `--cost cached` (default)
reads the cache and computes only stories without an entry; `--cost live` recomputes every non-final story. A
live read that lost a transcript the cached report priced keeps the cached entry (`transcripts_expired`); any
other live read replaces it (`previous_usd_measured` when the new total is lower).

Grouping. `config_id` is `auto_dev_driver.knobs_id` over a session's `knobs` without `driver.max_stories` and
`driver.max_cost_usd` (they set how long a run lasts, not how a session works); each group lists the stored
`knobs_id`s it covers. The unit is the session. A story is counted as landed or halted in the group of its
outcome session (the last session whose phase line says `landed` or `halted`), and listed in every group it
touched; per-session figures count in each session's own group.

Later fixes. For a landed story: its merge on the base branch (`config: git.base_branch`;
`harness_metrics.merge_subject_re`), its files outside the documentation prefixes (`config:
pipeline.gate_inert_prefixes`), and every later `fix:` or `Revert "` commit on the base branch that changes or removes a line
`git blame` attributes to one of the story's commits.

Usage
-----
    uv run --no-project <plugin-root>/scripts/auto_dev_metrics.py run [<run-id> | latest] [--cost cached|live] [--json] [--state-dir DIR]
    uv run --no-project <plugin-root>/scripts/auto_dev_metrics.py compare [--cost cached|live] [--json] [--state-dir DIR]

When to use
-----------
Tuning the auto-dev configuration: find the configuration that keeps or raises quality and speed and keeps or
lowers cost. A first `--cost cached` run reads every landed story's cost live (minutes); later runs read the
cache.

Exit 0 whenever the report is printed, source errors included. Exit 2 (one JSON error line under `--json`, a
stderr line otherwise) when the main checkout cannot be located, on a usage error, or when the run does not exist.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import glob
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import auto_dev_driver  # noqa: E402
import auto_dev_run_report as adr  # noqa: E402
import harness_metrics as hm  # noqa: E402
import review_precision as rp  # noqa: E402
import story_ledger as sl  # noqa: E402
import story_state as ss  # noqa: E402
import wfconfig  # noqa: E402

MAIN_REPO: Path | None = None
STATE_DIR: Path | None = None
ARTS: Path | None = None
FAILURE_LOG: Path | None = None
CACHE_DIR: Path | None = None

# A hang detector for one harness_metrics run over every story needing a live read.
COST_TIMEOUT_S = 1800
CAPS = ("max_stories", "max_cost_usd")
GROUP_KEY = {"key": "config_id", "excluded": ["driver.max_stories", "driver.max_cost_usd"]}
ARTS_REL = "docs/implementation-artifacts"
FIX_SUBJECT_RX = re.compile(r"^fix(\([^)]*\))?!?:")
HUNK_RX = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.M)
BLAME_HEADER_RX = re.compile(r"^([0-9a-f]{40}) \d+ \d+(?: \d+)?$")
FAILURE_HEADING_RX = re.compile(r"^## (.+?) — (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) — (.*)$")
LOG_LINE_RX = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] (.*)$")
SCOPED_RECORD_RX = re.compile(r"--record\s+(\S+)\s+(phase-\d+)")
MAX_CMD = 300
GATE_RED_RX = re.compile(r"^checkpoint-3 approval withdrawn\s*(?:--|—|-)\s*(?:pre-merge|train) gate exit (\d+)", re.I)
HANDOFF_RX = re.compile(r"^impl handoff (\d+)\b")
ROUNDS_USED_RX = re.compile(r"\brounds_used (\d+)")
COST_SCOPE = ("every session harness_metrics attributes to the story: driver sessions of any run, "
              "interactive and worktree sessions")
LANDED_OUTSIDE = ("landed outside a driver session; post_merge_smoke.py prints its verdict and records it nowhere")
FIX_DEFINITION = ("a later `fix:` or `Revert \"` commit on the base branch that changes or removes a line `git blame` "
                  "attributes to one of the story's commits; a fix inside another story's `feat:` commit is not seen")


class GitError(RuntimeError):
    pass


class RunNotFound(Exception):
    pass


def configure(main_repo) -> None:
    """Point every path the script reads at `main_repo` (the main checkout)."""
    global MAIN_REPO, STATE_DIR, ARTS, FAILURE_LOG, CACHE_DIR
    MAIN_REPO = Path(main_repo)
    ss.configure(MAIN_REPO)
    global ARTS_REL
    ARTS_REL = ss.ARTS_REL
    STATE_DIR = MAIN_REPO / ".workflow" / "state" / "auto-dev"
    ARTS = MAIN_REPO / ARTS_REL
    FAILURE_LOG = ss.FAILURES_PATH
    CACHE_DIR = MAIN_REPO / ".workflow" / "state" / "auto-dev-metrics" / "cost"
    adr.configure(MAIN_REPO, STATE_DIR)


def now_iso() -> str:
    return ss.now_iso()


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fact_of(line: str) -> tuple[str, str]:
    """(date, fact) of a `- <date> <fact>` record line."""
    parts = line.lstrip("- ").split(None, 1)
    return (parts[0], parts[1]) if len(parts) > 1 else ("", "")


def config_blob(knobs: dict) -> dict:
    blob = copy.deepcopy(knobs or {})
    for cap in CAPS:
        (blob.get("driver") or {}).pop(cap, None)
    return blob


def config_id(knobs: dict) -> str:
    return auto_dev_driver.knobs_id(config_blob(knobs))


def git(*args) -> str:
    argv = ["git", "-C", MAIN_REPO, *args]
    rc, out, stderr = ss.run(argv, timeout=ss.GIT_TIMEOUT_S)
    if rc != 0:
        raise GitError(ss.cmd_error(argv, rc, stderr))
    return out


# ---------------------------------------------------------------- context

class StoryRef:
    def __init__(self, sid: str):
        self.sid = sid
        self.vs = sl.variants(sid)


class Ctx:
    """What one report shares between its stories: memoised reads (a failed read is re-raised, not retried)."""

    def __init__(self, cost_mode: str = "cached"):
        self.cost_mode = cost_mode
        self.run = None
        self._stories: dict[str, StoryRef] = {}
        self._memo: dict = {}
        self.unreadable: dict[str, str] = {}  # run id -> the read error; that run's facts came from its streams

    def story(self, sid: str) -> StoryRef:
        return self._stories.setdefault(sid, StoryRef(sid))

    def cached(self, key, fn):
        if key not in self._memo:
            try:
                self._memo[key] = ("ok", fn())
            except Exception as exc:  # noqa: BLE001 -- re-raised to every caller of the key
                self._memo[key] = ("raised", exc)
        state, value = self._memo[key]
        if state == "raised":
            raise value
        return value

    def merges(self) -> list[tuple[str, str, str]]:
        def read():
            out = git("log", ss.BASE_BRANCH, "--merges", "--format=%H%x09%cI%x09%s")
            return [tuple(line.split("\t", 2)) for line in out.splitlines() if line.count("\t") >= 2]
        return self.cached("merges", read)

    def failure_entries(self) -> list[dict] | None:
        return self.cached("failures", read_failure_log)

    def run_facts(self, run_id: str) -> list[dict]:
        def read():
            d = STATE_DIR / run_id
            sessions = d / "sessions.jsonl"
            if sessions.is_file():
                try:
                    records = ss.read_jsonl(sessions, strict=True)
                except (OSError, ValueError) as exc:
                    self.unreadable[run_id] = str(exc)  # landing() reports it; the stream phase lines stand in
                    return adr.session_facts(adr.load_run(d))
                return adr.session_facts({"records": records, "reports": []})
            return adr.session_facts(adr.load_run(d))
        return self.cached(("facts", run_id), read)

    def landing(self, st: StoryRef) -> tuple[tuple[Path, int] | None, list[str]]:
        """((run dir, session) of the newest driver session whose phase line says this story landed, one
        `run <id> (<error>)` for each run on the way, the landing run included, whose `sessions.jsonl` could not
        be read)."""
        wanted = {v.lower() for v in st.vs}
        unreadable: list[str] = []
        for run_id in reversed(adr.run_ids(STATE_DIR)):
            facts = self.run_facts(run_id)
            if run_id in self.unreadable:
                unreadable.append(f"run {run_id} ({self.unreadable[run_id]})")
            for f in reversed(facts):
                if f["outcome"] == "landed" and str(f["story"]).lower() in wanted:
                    return (STATE_DIR / run_id, f["session"]), unreadable
        return None, unreadable

    def roots(self, st: StoryRef) -> dict:
        return self.cached(("roots", st.sid), lambda: locate(self, st))

    def findings(self, st: StoryRef) -> dict | None:
        return self.cached(("findings", st.sid), lambda: read_findings(self, st))


# ---------------------------------------------------------------- artifact roots

def branch_listing(st: StoryRef) -> tuple[str | None, list[str]]:
    """(the preserved story branch of a variant, the file names it holds under the specs dir)."""
    branch = next((ss.story_branch(v) for v in st.vs if ss.branch_exists(ss.story_branch(v))), None)
    if branch is None:
        return None, []
    out = git("ls-tree", "--name-only", branch, "--", f"{ARTS_REL}/")
    return branch, [Path(line).name for line in out.splitlines() if line.strip()]


def findings_names(names, vs) -> list[str]:
    return sorted(n for n in names if any(fnmatch.fnmatch(n, f"{v}*code-review-findings*.md") for v in vs)
                  and sl.mentions(n, vs))


def locate(ctx: Ctx, st: StoryRef) -> dict:
    """Where the record and the findings files are read: the worktree, else the main checkout, else the branch."""
    vs = st.vs
    registry = ctx.cached("registry", ss.read_registry)[0]
    worktrees, worktrees_err = ctx.cached("worktrees", ss.git_worktrees)
    wt_arts, wt_text = None, "no worktree"
    try:
        row = ss.find_registry_row(registry, vs, None, None)
        sec, path = ss.worktree_section(st.sid, vs, row, worktrees, worktrees_err)
        if sec["status"] == "ok" and sec["exists"]:
            wt_arts = Path(path) / "docs" / "implementation-artifacts"
            wt_text = str(wt_arts)
        elif sec["status"] == "ok":
            wt_text = f"no worktree: {path} does not exist"
        elif sec["status"] == "error":
            wt_text = f"worktree unreadable: {sec['error']}"
        else:
            wt_text = sec["reason"]
    except Exception as exc:  # noqa: BLE001 -- the main checkout and the branch are still read
        wt_text = f"worktree lookup failed: {type(exc).__name__}: {exc}"

    record = findings = None
    for root, arts in (("worktree", wt_arts), ("main checkout", ARTS)):
        if arts is None:
            continue
        if record is None:
            rel, lines = sl.read_record(vs, str(arts))
            if rel is not None:
                record = {"root": root, "location": str(arts / Path(rel).name), "lines": lines}
        if findings is None:
            paths = sorted({p for v in vs for p in glob.glob(os.path.join(glob.escape(str(arts)),
                                                                          f"{v}*code-review-findings*.md"))
                            if sl.mentions(os.path.basename(p), vs)})
            if paths:
                findings = {"root": root, "location": str(arts), "paths": paths, "names": [], "branch": None}
    branch_text = f"{ss.story_branch(st.sid)} does not exist"
    record_failure = ""
    if record is None or findings is None:
        try:
            branch, names = branch_listing(st)
            if branch is not None:
                branch_text = f"{branch} holds none"
                if record is None:
                    for v in vs:
                        if f"{v}-record.md" in names:
                            try:
                                text = git("show", f"{branch}:{ARTS_REL}/{v}-record.md")
                            except GitError as exc:
                                record_failure = f"; {branch}:{ARTS_REL}/{v}-record.md is listed but could not be read: {exc}"
                                continue
                            lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("-")]
                            record = {"root": "branch", "location": branch, "lines": lines}
                            break
                if findings is None:
                    found = findings_names(names, vs)
                    if found:
                        findings = {"root": "branch", "location": branch, "paths": [], "names": found,
                                    "branch": branch}
        except (GitError, RuntimeError) as exc:
            branch_text = f"branch lookup failed: {exc}"
    where = f"worktree: {wt_text}; main checkout: {ARTS}; branch: {branch_text}"
    return {"record": record, "findings": findings,
            "record_reason": f"no {st.sid}-record.md found ({where}){record_failure}",
            "findings_reason": f"no {st.sid}*code-review-findings*.md found ({where})"}


def root_section(kind: str, roots: dict) -> dict:
    src = roots[kind]
    if src is None:
        return ss.none(ARTS, roots[f"{kind}_reason"])
    return ss.ok(src["location"], root=src["root"], location=src["location"])


def artifact_roots_section(ctx: Ctx, st: StoryRef) -> dict:
    roots = ctx.roots(st)
    return {"record": root_section("record", roots), "findings": root_section("findings", roots)}


# ---------------------------------------------------------------- findings and fix rounds

def merge_counters(blocks: list[dict], key: str) -> dict:
    total: Counter = Counter()
    for b in blocks:
        total.update(b[key])
    return dict(total)


def read_findings(ctx: Ctx, st: StoryRef) -> dict | None:
    """The parsed findings of the story, or None when no root holds a findings file."""
    src = ctx.roots(st)["findings"]
    if src is None:
        return None
    tmp = None
    try:
        paths = list(src["paths"])
        if src["branch"]:
            CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix="findings-", dir=CACHE_DIR.parent))
            for name in src["names"]:
                (tmp / name).write_text(git("show", f"{src['branch']}:{ARTS_REL}/{name}"), encoding="utf-8")
                paths.append(str(tmp / name))
        rep = rp.summarize(paths)
        rounds = set()
        for p in paths:
            body = Path(p).read_text(encoding="utf-8", errors="replace").lower()
            rounds |= {int(m.group(1)) for m in ss.ROUND_HEADING_RE.finditer(body)}
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    blocks = [s["all"] for s in rep["stories"].values()]
    by_impact: dict = {}
    for b in blocks:
        for impact, counts in b["triage_by_impact"].items():
            by_impact.setdefault(impact, Counter()).update(counts)
    return {"src": src, "rounds": sorted(rounds),
            "files": [{"file": f["file"], "parse_status": f["status"], "reason": f["reason"]} for f in rep["files"]],
            "deduped": sum(b["deduped"] for b in blocks), "verdicts": merge_counters(blocks, "verdicts"),
            "triage": merge_counters(blocks, "triage"), "auto_mode": any(f.get("auto_mode") for f in rep["files"]),
            "bar_impact": merge_counters(blocks, "bar_impact"), "kind": merge_counters(blocks, "kind"),
            "triage_by_impact": {k: dict(v) for k, v in by_impact.items()}}


def findings_section(ctx: Ctx, st: StoryRef) -> dict:
    data = ctx.findings(st)
    if data is None:
        return ss.none(ARTS, ctx.roots(st)["findings_reason"])
    keys = ("files", "deduped", "verdicts", "triage", "auto_mode", "rounds", "bar_impact", "kind",
            "triage_by_impact")
    return ss.ok(data["src"]["location"], root=data["src"]["root"], **{k: data[k] for k in keys})


def review_rounds_section(ctx: Ctx, st: StoryRef) -> dict:
    data = ctx.findings(st)
    if data is None:
        return ss.none(ARTS, ctx.roots(st)["findings_reason"])
    return ss.ok(data["src"]["location"], rounds=data["rounds"])


SCOPED_NOTE = ("Every run is counted, red ones included: phase-2's first run is the red check, and an environment "
               "re-run counts too (a red run is recorded nowhere else). A journaled command is cut at 300 characters "
               f"(MAX_CMD={MAX_CMD}, session_journal.py), so a run whose story id falls past the cut is not counted; "
               "commands_cut_at_300 counts this story's run_scoped_tests.py facts that reach that length.")


def scoped_test_runs_section(ctx: Ctx, st: StoryRef) -> dict:
    jdir = ss.journal_dir()
    if not os.path.isdir(jdir):
        return ss.err(jdir, f"journal dir {jdir} does not exist")
    facts, _, _ = sl.load_facts(st.vs)
    record = ctx.roots(st)["record"]
    greens = [fact_of(ln)[1] for ln in record["lines"]] if record else None
    wanted = {v.lower() for v in st.vs}
    phases = {p: {"runs": 0, "ts": [], "green_lines": None if greens is None else
                  sum(1 for g in greens if g.startswith(f"{p} tests GREEN"))} for p in ("phase-2", "phase-3")}
    cut = 0
    for f in facts:
        cmd = f.get("command") or ""
        if f.get("kind") != "command_run" or "run_scoped_tests.py" not in cmd:
            continue
        if len(cmd) >= MAX_CMD:
            cut += 1
        m = SCOPED_RECORD_RX.search(cmd)
        if m and m.group(1).lower() in wanted and m.group(2) in phases:
            phases[m.group(2)]["runs"] += 1
            phases[m.group(2)]["ts"].append(f.get("ts"))
    return ss.ok(jdir, phases=phases, commands_cut_at_300=cut, note=SCOPED_NOTE)


def impl_handoffs_section(ctx: Ctx, st: StoryRef) -> dict:
    record = ctx.roots(st)["record"]
    if record is None:
        return ss.none(ARTS, ctx.roots(st)["record_reason"])
    handoffs = []
    for ln in record["lines"]:
        date, fact = fact_of(ln)
        m = HANDOFF_RX.match(fact)
        if m:
            used = ROUNDS_USED_RX.search(fact)
            handoffs.append({"n": int(m.group(1)), "rounds_used": int(used.group(1)) if used else None,
                             "line": ln.lstrip("- ").strip()})
    return ss.ok(record["location"], handoffs=handoffs)


def fix_rounds_section(ctx: Ctx, st: StoryRef) -> dict:
    return ss.ok("findings files, journals and the record",
                 review_rounds=ss.guard("findings files", "review_rounds_section", lambda: review_rounds_section(ctx, st)),
                 scoped_test_runs=ss.guard(ss.journal_dir(), "scoped_test_runs_section",
                                           lambda: scoped_test_runs_section(ctx, st)),
                 impl_handoffs=ss.guard("record", "impl_handoffs_section", lambda: impl_handoffs_section(ctx, st)))


# ---------------------------------------------------------------- gates and smoke

def pre_merge_gate_section(ctx: Ctx, st: StoryRef) -> dict:
    record = ctx.roots(st)["record"]
    if record is None:
        return ss.none(ARTS, f"no pre-merge gate line: {ctx.roots(st)['record_reason']}")
    gates = []
    for ln in record["lines"]:
        date, fact = fact_of(ln)
        low = fact.lower()
        red = GATE_RED_RX.match(fact)
        if low.startswith("pre-merge gate green"):
            verdict, code = "green", None
        elif low.startswith("pre-merge gate not required"):
            verdict, code = "not-required", None
        elif red:
            verdict, code = "red", int(red.group(1))
        else:
            continue
        gates.append({"date": date, "verdict": verdict, "exit_code": code, "line": ln.lstrip("- ").strip()})
    if not gates:
        return ss.none(record["location"], f"no pre-merge gate line in {record['location']}")
    return ss.ok(record["location"], gates=gates, latest=gates[-1])


def post_merge_smoke_section(ctx: Ctx, st: StoryRef) -> dict:
    landing, unreadable = ctx.landing(st)
    tail = ("; sessions.jsonl unreadable, so the run's stream phase lines were read instead: "
            + "; ".join(unreadable)) if unreadable else ""
    if landing is None:
        return ss.none(STATE_DIR, LANDED_OUTSIDE + tail)
    d, n = landing
    path = d / f"session-{n}.jsonl"
    if not path.is_file():
        return ss.none(path, f"the landing session {n} of run {d.name} has no stream file {path.name}{tail}")
    uses: list[str] = []
    results: dict[str, str] = {}
    for ev in ss.read_jsonl(path, strict=False):
        for b in adr.blocks(ev):
            if ev.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("name") == "Bash" \
                    and "post_merge_smoke.py" in str((b.get("input") or {}).get("command", "")):
                uses.append(b.get("id", ""))
            elif ev.get("type") == "user" and b.get("type") == "tool_result":
                results[b.get("tool_use_id", "")] = adr.result_text(b)
    calls = []
    for tid in uses:
        text = results.get(tid, "")
        lines = text.splitlines()
        if any(ln.startswith("smoke FAIL") for ln in lines):
            verdict = "fail"
        elif any(ln.startswith("smoke green") for ln in lines):
            verdict = "green"
        else:
            verdict = "unrecognised"
        calls.append({"tool_use_id": tid, "verdict": verdict, "first_line": lines[0] if lines else ""})
    if not calls:
        return ss.none(path, f"{path.name} (run {d.name}) holds no Bash call to post_merge_smoke.py{tail}")
    return ss.ok(f"{path}{tail}", run_id=d.name, session=n, calls=calls, verdict=calls[-1]["verdict"])


# ---------------------------------------------------------------- halts

def read_failure_log() -> list[dict] | None:
    if not FAILURE_LOG.is_file():
        return None
    entries: list[dict] = []
    for line in FAILURE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = FAILURE_HEADING_RX.match(line)
        if m:
            entries.append({"story": m.group(1), "timestamp": m.group(2), "ts": datetime.fromisoformat(m.group(2)),
                            "failure_step": m.group(3).strip(), "summary": ""})
        elif entries and line.startswith("- failure_step:"):
            entries[-1]["failure_step"] = line.split(":", 1)[1].strip()
        elif entries and line.startswith("- failure_summary:") and not entries[-1]["summary"]:
            entries[-1]["summary"] = line.split(":", 1)[1].strip()
    return entries


def run_start_local(run_id: str) -> datetime:
    return adr.to_local(adr.run_started(run_id)).replace(tzinfo=None)


def session_end(lines: list[str], start: datetime, n: int) -> datetime | None:
    """The local time of the last driver.log line naming `session <n>`; the log carries a time of day only, so
    the date is walked from the run's start, one day on whenever a line's time is earlier than the line before."""
    day, previous, end = start.date(), start.time(), None
    pattern = re.compile(rf"\bsession {n}\b")
    for line in lines:
        m = LOG_LINE_RX.match(line)
        if not m:
            continue
        t = time(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if t < previous:
            day += timedelta(days=1)
        previous = t
        if pattern.search(m.group(4)):
            end = datetime.combine(day, t)
    return end


def failure_entry(ctx: Ctx, vs: list[str], run_id: str, log: list[str] | None, n: int,
                  story_sessions: list[int]) -> dict:
    """The story's last `auto-dev-failures.md` entry after the end of its latest earlier session in the run that
    `driver.log` dates (the run's start when none is dated) and at or before the end of session `n`;
    `story_sessions` are the story's sessions of the run."""
    if log is None:
        return ss.none(FAILURE_LOG, f"session {n} of run {run_id} cannot be dated: the run has no driver.log")
    start = run_start_local(run_id)
    end = session_end(log, start, n)
    if end is None:
        return ss.none(FAILURE_LOG, f"driver.log of run {run_id} names no line for session {n}")
    entries = ctx.failure_entries()
    if entries is None:
        return ss.none(FAILURE_LOG, f"no failure log at {FAILURE_LOG}")
    lower, since = start, "the run's start"
    undated = []
    for earlier in sorted((s for s in story_sessions if s < n), reverse=True):
        earlier_end = session_end(log, start, earlier)
        if earlier_end is not None:
            lower = earlier_end
            since = f"the end of session {earlier}, the story's latest earlier session in this run that driver.log dates"
            break
        undated.append(earlier)
    since += "".join(f"; driver.log names no line for session {s}" for s in undated)
    window = f"after {lower.isoformat(timespec='seconds')} ({since}) up to {end.isoformat(timespec='seconds')}"
    hits = [e for e in entries if sl.mentions(e["story"], vs) and lower < e["ts"] <= end]
    if not hits:
        return ss.none(FAILURE_LOG, f"no failure-log entry for {vs[0]} {window}")
    e = max(hits, key=lambda x: x["ts"])
    return ss.ok(FAILURE_LOG, failure_step=e["failure_step"], timestamp=e["timestamp"], summary=e["summary"][:300])


def halts_section(ctx: Ctx, st: StoryRef) -> dict:
    data = ctx.run
    facts = [f for f in adr.session_facts(data) if f["story"] == st.sid]
    mine = [f["session"] for f in facts]
    sessions = [{"session": f["session"], "step": f["step"],
                 "failure_entry": ss.guard(FAILURE_LOG, "failure_entry",
                                           lambda f=f: failure_entry(ctx, st.vs, data["run_id"], data["log"],
                                                                     f["session"], mine))}
                for f in facts if f["outcome"] == "halted"]
    return ss.ok(data["dir"], sessions=sessions,
                 driver_stops=[f["session"] for f in facts if f["is_error"] or f["outcome"] is None])


# ---------------------------------------------------------------- later fixes

def later_fixes_section(ctx: Ctx, st: StoryRef) -> dict:
    source = f"git log {ss.BASE_BRANCH} --merges"
    try:
        return build_later_fixes(ctx, st, source)
    except GitError as exc:
        return ss.err(source, str(exc))


def doc_prefixes() -> tuple[str, ...]:
    """Paths whose later changes are not code fixes: `config: pipeline.gate_inert_prefixes` (default docs/)."""
    return tuple(str(p) for p in (wfconfig.get("pipeline.gate_inert_prefixes", ["docs/"]) or ["docs/"]))


def story_lines_changed(commit: str, files: list[str], story_commits: set[str]) -> tuple[int, list[str]]:
    """How many lines `commit` changed or removed that blame (on its parent) attributes to the story's commits."""
    total, touched = 0, []
    for path in files:
        count = 0
        for m in HUNK_RX.finditer(git("diff", "-U0", f"{commit}^", commit, "--", path)):
            start = int(m.group(1))
            length = int(m.group(2)) if m.group(2) is not None else 1
            if length == 0:
                continue
            blame = git("blame", "--porcelain", "-L", f"{start},{start + length - 1}", f"{commit}^", "--", path)
            count += sum(1 for line in blame.splitlines()
                         if (h := BLAME_HEADER_RX.match(line)) and h.group(1) in story_commits)
        if count:
            total += count
            touched.append(path)
    return total, touched


def build_later_fixes(ctx: Ctx, st: StoryRef, source: str) -> dict:
    rx = hm.merge_subject_re(st.sid)
    merge = next((m for m in ctx.merges() if rx.match(m[2])), None)
    if merge is None:
        return ss.none(source, f"no `Merge {ss.story_branch(st.sid)}` merge on {ss.BASE_BRANCH}")
    sha, date, subject = merge
    merged_at = datetime.fromisoformat(date)
    story_commits = set(git("rev-list", f"{sha}^1..{sha}^2").split())
    files = [f for f in git("diff", "--name-only", "-z", f"{sha}^1", sha).split("\0")
             if f and not f.startswith(doc_prefixes())]
    until = git("log", "-1", "--format=%cI", ss.BASE_BRANCH).strip()
    fixes = []
    if files:
        log = git("log", "--no-merges", "--reverse", "--format=%H%x09%cI%x09%s", f"{sha}..{ss.BASE_BRANCH}", "--", *files)
        for line in log.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not (FIX_SUBJECT_RX.match(parts[2]) or parts[2].startswith('Revert "')):
                continue
            changed, touched = story_lines_changed(parts[0], files, story_commits)
            if changed:
                fixes.append({"sha": parts[0], "date": parts[1], "subject": parts[2],
                              "days_after_merge": round((datetime.fromisoformat(parts[1]) - merged_at).total_seconds() / 86400, 2),
                              "files": touched, "story_lines_changed": changed})
    return ss.ok(source, merge={"sha": sha, "date": date, "subject": subject}, files=files, definition=FIX_DEFINITION,
                 observed_until=until, days_observed=round((datetime.fromisoformat(until) - merged_at).total_seconds() / 86400, 2),
                 fixes=fixes)


# ---------------------------------------------------------------- cost

def cost_key(sid: str) -> str:
    return sid.strip().lower().replace(".", "-")


def is_final(entry: dict) -> bool:
    end = ss.parse_ts(((entry["report"].get("rules") or {}).get("window_end_utc")))
    computed = ss.parse_ts(entry["computed_at"])
    return end is not None and computed is not None and end < computed


def read_cache(key: str) -> tuple[dict | None, str | None]:
    path = CACHE_DIR / f"{key}.json"
    if not path.is_file():
        return None, None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if not (isinstance(entry, dict) and isinstance(entry.get("report"), dict) and entry.get("computed_at")
                and isinstance(entry.get("argv"), list) and isinstance(entry["report"].get("rules") or {}, dict)):
            raise ValueError("not a {computed_at, argv, report} entry whose report.rules is a dict")
    except (OSError, ValueError) as exc:
        return None, f"{path.name}: unusable cache file ({exc}); read live"
    return entry, None


def write_cache(key: str, entry: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_DIR / f"{key}.json")
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def lost_sessions(cached: dict, live: dict) -> list[str]:
    """Sessions the cached report priced that the live report can no longer read: a priced session whose
    main transcript is now not found, or a WORKTREE session (found by globbing transcripts) that is gone."""
    live_rows = {r.get("session"): r for r in live.get("sessions", [])}
    lost = []
    for row in cached.get("sessions", []):
        sid = row.get("session")
        if row.get("class") == "WORKTREE" and sid not in live_rows:
            lost.append(sid)
        elif is_number(row.get("usd_full_measured")) and sid in live_rows \
                and live_rows[sid].get("note") == "main transcript not found":
            lost.append(sid)
    return lost


def bucket_map(buckets: dict) -> dict:
    return {k: {"usd_measured": b["usd_measured"], "usd_est_topup": b["usd_est_topup"], "agent_hours": b["agent_hours"],
                "span_hours": b["span_hours"], "spawns": b["spawns"], "requests": b["requests"]}
            for k, b in buckets.items()}


def story_section(argv, rep: dict, cache: str, **extra) -> dict:
    source = ss.argv_text(argv)

    def build():
        rules = rep.get("rules") or {}
        return ss.ok(source, usd_measured=rep["total"]["usd_measured"], usd_est_topup=rep["total"]["usd_est_topup"],
                     wall_clock=rep["wall_clock"],
                     sessions_by_class=dict(Counter(s.get("class") for s in rep.get("sessions", []))),
                     excluded_incidental_usd=rep["excluded_incidental_usd"], unpriced_requests=rep["unpriced_requests"],
                     merge=rules.get("merge"), window_end_utc=rules.get("window_end_utc"),
                     by_phase=bucket_map(rep["by_phase"]), by_agent=bucket_map(rep["by_agent"]),
                     scope=COST_SCOPE, basis="list-price estimate", format="internal", cache=cache, **extra)
    return ss.guard(source, "harness_metrics report", build)


def story_costs(story_ids, mode: str) -> dict[str, dict]:
    """story id -> its cost section: one `harness_metrics.py --json` for every story needing a live read."""
    if not story_ids:
        return {}
    keys = {sid: cost_key(sid) for sid in story_ids}
    cache = {key: read_cache(key) for key in dict.fromkeys(keys.values())}
    live_keys = [k for k, (entry, _) in cache.items() if entry is None or not (is_final(entry) or mode == "cached")]
    argv = live_error = None
    live: dict[str, dict] = {}
    if live_keys:
        argv = [sys.executable, SCRIPTS_DIR / "harness_metrics.py"]
        for key in live_keys:
            argv += ["--story", key]
        argv.append("--json")
        rc, out, stderr = ss.run(argv, timeout=COST_TIMEOUT_S)
        if rc != 0:
            live_error = ss.cmd_error(argv, rc, stderr)
        else:
            try:
                live = {r["story"]: r for r in json.loads(out)}
            except (ValueError, KeyError, TypeError) as exc:
                live_error = f"{ss.argv_text(argv)}: unparsable harness_metrics output: {exc}"
    sections: dict[str, dict] = {}
    for key, (entry, cache_error) in cache.items():
        if key not in live_keys:
            sections[key] = story_section(entry["argv"], entry["report"],
                                          "final" if is_final(entry) else f"cached {entry['computed_at']}")
            continue
        extra: dict = {"cache_error": cache_error} if cache_error else {}
        if live_error:
            sections[key] = {**ss.err(ss.argv_text(argv), live_error), **extra}
            continue
        rep = live.get(key)
        if rep is None:
            sections[key] = {**ss.err(ss.argv_text(argv), f"story {key} is missing from the harness_metrics report"),
                             **extra}
            continue
        if entry is not None:
            lost = lost_sessions(entry["report"], rep)
            if lost:
                sections[key] = story_section(
                    entry["argv"], entry["report"], f"cached {entry['computed_at']}", transcripts_expired=True,
                    sessions=lost, live_usd_measured=(rep.get("total") or {}).get("usd_measured"),
                    cached_at=entry["computed_at"], **extra)
                continue
            old = (entry["report"].get("total") or {}).get("usd_measured")
            new = (rep.get("total") or {}).get("usd_measured")
            if is_number(old) and is_number(new) and new < old:
                extra["previous_usd_measured"] = old
        try:
            write_cache(key, {"computed_at": now_iso(), "argv": [str(a) for a in argv], "report": rep})
        except OSError as exc:
            extra["cache_write_error"] = f"{CACHE_DIR / (key + '.json')}: {exc}"
        sections[key] = story_section(argv, rep, "live", **extra)
    return {sid: sections[key] for sid, key in keys.items()}


def driver_cost_section(ctx: Ctx, st: StoryRef) -> dict:
    data = ctx.run
    if data["records_error"]:
        return ss.err(data["records_path"], data["records_error"])
    if data["records"] is None:
        return ss.none(data["records_path"], f"no sessions.jsonl in {data['dir']}: cost_usd and minutes are recorded there")
    recs = [r for r in data["records"] if r.get("story") == st.sid]
    reports = {r["session"]: r for r in data["reports"]}
    by_step: dict = {}
    by_model: dict = {}
    for r in recs:
        row = by_step.setdefault(str(r.get("step")), {"usd": 0.0, "minutes": 0.0, "sessions": 0})
        row["sessions"] += 1
        if is_number(r.get("cost_usd")):
            row["usd"] += r["cost_usd"]
        if is_number(r.get("minutes")):
            row["minutes"] += r["minutes"]
        mu = (reports.get(r.get("session")) or {}).get("model_usage")
        if mu and mu["status"] == "ok":
            for model, usage in mu["models"].items():
                if is_number(usage["cost_usd"]):
                    by_model[model] = by_model.get(model, 0.0) + usage["cost_usd"]
    return ss.ok(data["records_path"], usd=round(sum(r["cost_usd"] for r in recs if is_number(r.get("cost_usd"))), 6),
                 minutes=round(sum(r["minutes"] for r in recs if is_number(r.get("minutes"))), 2), sessions=len(recs),
                 by_step={k: {"usd": round(v["usd"], 6), "minutes": round(v["minutes"], 2), "sessions": v["sessions"]}
                          for k, v in by_step.items()},
                 by_model={m: round(v, 6) for m, v in by_model.items()},
                 unpriced_sessions=[r.get("session") for r in recs if not is_number(r.get("cost_usd"))],
                 basis="list-price estimate (the result event's total_cost_usd, subagents included)")


# ---------------------------------------------------------------- the run document

def call(name: str, ctx: Ctx, st: StoryRef, source) -> dict:
    """Build one section inside `story_state.guard`; the function is looked up at call time."""
    return ss.guard(source, name, lambda: globals()[name](ctx, st))


def story_document(ctx: Ctx, sid: str, costs: dict) -> dict:
    st = ctx.story(sid)
    return {"story_id": sid,
            "artifact_roots": call("artifact_roots_section", ctx, st, ARTS),
            "halts": call("halts_section", ctx, st, ctx.run["dir"]),
            "findings": call("findings_section", ctx, st, ARTS),
            "fix_rounds": call("fix_rounds_section", ctx, st, ARTS),
            "pre_merge_gate": call("pre_merge_gate_section", ctx, st, ARTS),
            "post_merge_smoke": call("post_merge_smoke_section", ctx, st, STATE_DIR),
            "later_fixes": call("later_fixes_section", ctx, st, f"git log {ss.BASE_BRANCH} --merges"),
            "cost": {"driver": call("driver_cost_section", ctx, st, ctx.run["dir"]), "story": costs[sid]}}


def safe_costs(ids: list[str], mode: str) -> dict[str, dict]:
    try:
        return story_costs(ids, mode)
    except Exception as exc:  # noqa: BLE001 -- every story's cost section names the failure
        return {sid: ss.err("harness_metrics.py", f"story_costs: {type(exc).__name__}: {exc}") for sid in ids}


def build_run_document(run_arg: str, cost_mode: str) -> dict:
    d = adr.run_dir(run_arg, STATE_DIR)
    if d is None:
        raise RunNotFound(f"no run directory for {run_arg!r} under {STATE_DIR}")
    data = adr.load_run(d)
    ctx = Ctx(cost_mode)
    ctx.run = data
    run = adr.build_run(d, 6, None, data)
    ids = list(dict.fromkeys(f["story"] for f in adr.session_facts(data) if f["story"] not in (None, "none")))
    costs = safe_costs(ids, cost_mode)
    return {"generated_at": now_iso(), "main_repo": str(MAIN_REPO), "run": run,
            "stories": [story_document(ctx, sid, costs) for sid in ids]}


# ---------------------------------------------------------------- the comparison

def usd_minutes(units: list[dict]) -> tuple[float, float]:
    usd = sum(u["rec"]["cost_usd"] for u in units if is_number(u["rec"].get("cost_usd")))
    minutes = sum(u["rec"]["minutes"] for u in units if is_number(u["rec"].get("minutes")))
    return round(usd, 6), round(minutes, 2)


def harness_pathspecs() -> list[str]:
    return list(auto_dev_driver.HARNESS_FILES) + [f":(glob){folder}/{pattern}" for folder, pattern in
                                                  auto_dev_driver.HARNESS_GLOBS]


def harness_section(units: list[dict], run_records: dict) -> dict:
    def point(u: dict) -> dict:
        return {"harness_id": u["rec"].get("harness_id"), "run": u["run"], "session": u["n"],
                "head": u["rec"].get("head")}

    ids: dict = {}
    for u in units:
        ids.setdefault(u["rec"].get("harness_id"), point(u))
    order = list(ids.values())
    transitions = []
    for before, after in zip(units, units[1:]):
        if before["rec"].get("harness_id") == after["rec"].get("harness_id"):
            continue
        a, b = point(before), point(after)
        records = run_records[after["run"]]
        index = next(i for i, r in enumerate(records) if r.get("session") == after["n"])
        # the drift keys bridge two sessions only when they are neighbours in one run
        prev = records[index - 1] if index > 0 and records[index - 1] is before["rec"] else None
        t: dict = {"from": a, "to": b}
        keys_ok = prev is not None and "harness_changed_during" in prev \
            and "harness_changed_before" in after["rec"] and "harness_uncommitted" in after["rec"]
        with_keys = [rec for rec in (prev, after["rec"]) if rec is not None and any(k in rec for k in adr.DRIFT_KEYS)]
        if keys_ok:
            t["changed"] = sorted(set(prev["harness_changed_during"]) | set(after["rec"]["harness_changed_before"]))
            t["uncommitted"] = list(after["rec"]["harness_uncommitted"])
            t["note"] = "from the drift keys the records carry"
        elif a["head"] and a["head"] == b["head"] and not with_keys:
            t["note"] = "changed files not recorded (the records predate the drift fields)"
        elif a["head"] and b["head"]:
            t["note"] = "committed changes only; the id hashes the working copy"
            try:
                out = git("diff", "--name-only", a["head"], b["head"], "--", *harness_pathspecs())
                t["committed_diff"] = sorted(line for line in out.splitlines() if line.strip())
            except GitError as exc:
                t["error"] = str(exc)
        else:
            t["note"] = "changed files not recorded and a head is missing"
        transitions.append(t)
    return ss.ok(f"sessions.jsonl under {STATE_DIR}", ids=order, transitions=transitions)


def build_compare_document(cost_mode: str) -> dict:
    ctx = Ctx(cost_mode)
    run_records: dict[str, list[dict]] = {}
    unfingerprinted: list[dict] = []
    units: list[dict] = []
    for run_id in adr.run_ids(STATE_DIR):
        d = STATE_DIR / run_id
        sessions = d / "sessions.jsonl"
        reason = None
        if sessions.is_file():
            try:
                run_records[run_id] = ss.read_jsonl(sessions, strict=True)
            except (OSError, ValueError) as exc:
                reason = f"cannot read sessions.jsonl: {exc}"
        else:
            reason = "no sessions.jsonl"
        if reason is None:
            for rec in run_records[run_id]:
                units.append({"run": run_id, "n": rec.get("session"), "rec": rec, "config": config_id(rec.get("knobs") or {})})
            continue
        data = adr.load_run(d)
        holding = sum(1 for r in data["reports"] if r["state"] == "ended")
        unfingerprinted.append({
            "run_id": run_id, "reason": f"{reason}; {holding} of {len(data['reports'])} session streams hold a result event",
            "stories": [{"story": f["story"], "outcome": f["outcome"], "step": f["step"]}
                        for f in adr.session_facts(data) if f["story"] not in (None, "none")]})
    order = list(dict.fromkeys(u["config"] for u in units))
    for u in units:
        u["group"] = order.index(u["config"])
    logs: dict = {}

    def log_of(run_id):
        if run_id not in logs:
            p = STATE_DIR / run_id / "driver.log"
            logs[run_id] = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else None
        return logs[run_id]

    def failure_of(u) -> dict:
        mine = [x["n"] for x in units if x["run"] == u["run"] and x["rec"].get("story") == u["rec"].get("story")]
        return ss.guard(FAILURE_LOG, "failure_entry", lambda: failure_entry(
            ctx, sl.variants(u["rec"]["story"]), u["run"], log_of(u["run"]), u["n"], mine))

    by_story: dict[str, list[dict]] = {}
    for u in units:
        story = u["rec"].get("story")
        if story not in (None, "none"):
            by_story.setdefault(story, []).append(u)
    outcome_unit = {sid: next((u for u in reversed(us) if u["rec"].get("outcome") in ("landed", "halted")), us[-1])
                    for sid, us in by_story.items()}
    landed_ids = [sid for sid, u in outcome_unit.items() if u["rec"].get("outcome") == "landed"]
    costs = safe_costs(landed_ids, cost_mode)

    groups = []
    for gi, config in enumerate(order):
        gunits = [u for u in units if u["group"] == gi]
        first_knobs = gunits[0]["rec"].get("knobs") or {}
        by_run: dict = {}
        for u in gunits:
            by_run.setdefault(u["run"], []).append(u["n"])
        knobs_ids = list(dict.fromkeys(u["rec"].get("knobs_id") for u in gunits))
        if gi == 0:
            diff = ss.none(STATE_DIR, "the first group has no previous group")
        else:
            prev_first = next(u for u in units if u["group"] == gi - 1)["rec"].get("knobs") or {}
            diff = ss.ok(STATE_DIR, changes=adr.knobs_diff(config_blob(prev_first), config_blob(first_knobs)))
        lists: dict = {"landed": [], "halted": [], "in_progress": []}
        counted_ids = []
        for sid, us in by_story.items():
            mine = [u for u in us if u["group"] == gi]
            if not mine:
                continue
            touched = sorted({u["group"] for u in us})
            entry: dict = {"story_id": sid}
            if len(touched) > 1:
                entry["spans_groups"] = [order[g] for g in touched]
            out = outcome_unit[sid]
            if out["group"] == gi:
                outcome = out["rec"].get("outcome")
                entry.update(counted=True, outcome=outcome, step=out["rec"].get("step"))
                if outcome == "halted":
                    fe = failure_of(out)
                    entry["failure_step"] = fe.get("failure_step") if fe["status"] == "ok" else None
                lists[outcome if outcome in ("landed", "halted") else "in_progress"].append(entry)
                counted_ids.append(sid)
            else:
                entry.update(counted=False, outcome_group=order[out["group"]], outcome=mine[-1]["rec"].get("outcome"),
                             step=mine[-1]["rec"].get("step"))
                lists["in_progress"].append(entry)
        stories = ss.ok(STATE_DIR, **lists)

        halted_units = [u for u in gunits if u["rec"].get("outcome") == "halted"]
        failure_steps = Counter()
        for u in halted_units:
            fe = failure_of(u)
            failure_steps[fe["failure_step"] if fe["status"] == "ok" else "none"] += 1
        per_story = {}
        for sid in counted_ids:
            st = ctx.story(sid)
            per_story[sid] = {
                "findings": call("findings_section", ctx, st, ARTS), "fix_rounds": call("fix_rounds_section", ctx, st, ARTS),
                "pre_merge_gate": call("pre_merge_gate_section", ctx, st, ARTS),
                "post_merge_smoke": call("post_merge_smoke_section", ctx, st, STATE_DIR),
                "later_fixes": call("later_fixes_section", ctx, st, f"git log {ss.BASE_BRANCH} --merges")}
        blocking = sum(s["findings"]["bar_impact"].get(k, 0) for s in per_story.values()
                       if s["findings"]["status"] == "ok" for k in ("blocking", "blocking-minority"))
        quality = ss.ok(STATE_DIR, halts_by_step=dict(Counter(str(u["rec"].get("step")) for u in halted_units)),
                        halts_by_failure_step=dict(failure_steps),
                        driver_stops=[{"run": u["run"], "session": u["n"]} for u in gunits
                                      if u["rec"].get("is_error") or u["rec"].get("outcome") is None],
                        stories=per_story,
                        totals={"blocking_findings": blocking,
                                "findings_deduped": sum(s["findings"]["deduped"] for s in per_story.values()
                                                        if s["findings"]["status"] == "ok"),
                                "later_fixes": sum(len(s["later_fixes"]["fixes"]) for s in per_story.values()
                                                   if s["later_fixes"]["status"] == "ok"),
                                "review_rounds": sum(len(s["fix_rounds"]["review_rounds"]["rounds"])
                                                     for s in per_story.values()
                                                     if s["fix_rounds"]["status"] == "ok"
                                                     and s["fix_rounds"]["review_rounds"]["status"] == "ok")})

        usd, minutes = usd_minutes(gunits)
        landed = []
        for entry in lists["landed"]:
            sid = entry["story_id"]
            u_in, m_in = usd_minutes([u for u in by_story[sid] if u["group"] == gi])
            u_all, m_all = usd_minutes(by_story[sid])
            cost = costs.get(sid, {})
            row = {"story_id": sid, "usd_in_group": u_in, "minutes_in_group": m_in, "usd_all_groups": u_all,
                   "minutes_all_groups": m_all,
                   "story_usd_measured": cost.get("usd_measured") if cost.get("status") == "ok" else None}
            if cost.get("status") != "ok":
                row["story_cost_error"] = cost.get("error") or cost.get("reason")
            landed.append(row)
        n_landed = len(landed)
        cost_and_time = ss.ok(
            f"sessions.jsonl under {STATE_DIR}", driver_usd=usd, driver_minutes=minutes,
            unpriced_sessions=[{"run": u["run"], "session": u["n"]} for u in gunits if not is_number(u["rec"].get("cost_usd"))],
            usd_per_landed=round(usd / n_landed, 6) if n_landed else "n/a (0 landed)",
            minutes_per_landed=round(minutes / n_landed, 2) if n_landed else "n/a (0 landed)", landed=landed,
            note="Minutes are session minutes: the time a story waits in the land line runs no session "
                 "(see the auto_dev_driver.py header) and is not counted. A session without a numeric cost_usd is named in "
                 "unpriced_sessions and never summed as 0.")
        groups.append({
            "config_id": config, "knobs_ids": knobs_ids,
            "knobs": ss.ok(STATE_DIR, knobs=first_knobs), "diff_from_previous": diff,
            "runs": [{"run_id": r, "sessions": ns} for r, ns in by_run.items()],
            "first": {"run": gunits[0]["run"], "session": gunits[0]["n"]},
            "last": {"run": gunits[-1]["run"], "session": gunits[-1]["n"]},
            "harness": ss.guard(STATE_DIR, "harness_section", lambda gu=gunits: harness_section(gu, run_records)),
            "stories": stories, "quality": quality, "cost_and_time": cost_and_time})
    return {"generated_at": now_iso(), "main_repo": str(MAIN_REPO), "grouping": GROUP_KEY, "groups": groups,
            "unfingerprinted_runs": unfingerprinted, "cost_mode": cost_mode}


# ---------------------------------------------------------------- markdown

def note(sec: dict) -> str:
    return sec.get("reason") or sec.get("error") or ""


def print_run(doc: dict) -> None:
    adr.print_report(doc["run"])
    print("\n# stories")
    for s in doc["stories"]:
        print(f"\n## {s['story_id']}")
        roots = s["artifact_roots"]
        if "status" in roots:
            print(f"- artifact roots: {roots['status']} {note(roots)}")
        else:
            for kind, sec in roots.items():
                print(f"- {kind} read from: " + (sec["root"] if sec["status"] == "ok" else f"{sec['status']} {note(sec)}"))
        h = s["halts"]
        if h["status"] == "ok":
            for x in h["sessions"]:
                fe = x["failure_entry"]
                print(f"- halted in session {x['session']} (step {x['step']}): "
                      + (f"{fe['failure_step']} at {fe['timestamp']}" if fe["status"] == "ok" else note(fe)))
            print(f"- driver stops: {h['driver_stops']}")
        f = s["findings"]
        print("- findings: " + (f"{f['deduped']} deduped; impact {f['bar_impact']}; verdicts {f['verdicts']}; "
                                 f"triage {f['triage']}" if f["status"] == "ok" else f"{f['status']} {note(f)}"))
        g = s["pre_merge_gate"]
        print("- pre-merge gate: " + (g["latest"]["verdict"] if g["status"] == "ok" else f"{g['status']} {note(g)}"))
        m = s["post_merge_smoke"]
        print("- post-merge smoke: " + (m["verdict"] if m["status"] == "ok" else f"{m['status']} {note(m)}"))
        lf = s["later_fixes"]
        print("- later fixes: " + (f"{len(lf['fixes'])} in {lf['days_observed']} days" if lf["status"] == "ok"
                                   else f"{lf['status']} {note(lf)}"))
        c = s["cost"]["story"]
        print("- story cost: " + (f"${c['usd_measured']} measured ({c['cache']})" if c["status"] == "ok"
                                  else f"{c['status']} {note(c)}"))


def print_compare(doc: dict) -> None:
    print(f"# auto-dev configuration comparison ({doc['cost_mode']} cost)\n")
    print("| config | runs | sessions | landed/halted | driver usd | driver minutes | usd per landed | "
          "minutes per landed | blocking findings | later fixes |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for g in doc["groups"]:
        s, q, c = g["stories"], g["quality"], g["cost_and_time"]
        sessions = sum(len(r["sessions"]) for r in g["runs"])
        print(f"| {g['config_id']} | {len(g['runs'])} | {sessions} | {len(s['landed'])}/{len(s['halted'])} | "
              f"{c['driver_usd']} | {c['driver_minutes']} | {c['usd_per_landed']} | {c['minutes_per_landed']} | "
              f"{q['totals']['blocking_findings']} | {q['totals']['later_fixes']} |")
    for g in doc["groups"]:
        print(f"\n## {g['config_id']} (knobs ids {', '.join(map(str, g['knobs_ids']))})")
        d = g["diff_from_previous"]
        print("- knobs diff from the previous group: " + ("" if d["status"] == "ok" else note(d)))
        for line in d.get("changes", []):
            print(f"    - {line}")
        h = g["harness"]
        if h["status"] == "ok":
            for t in h["transitions"]:
                detail = t.get("changed") or t.get("committed_diff") or t.get("error") or ""
                print(f"- harness {t['from']['harness_id']} → {t['to']['harness_id']}: {t['note']} {detail}")
    for u in doc["unfingerprinted_runs"]:
        print(f"\n- unfingerprinted run {u['run_id']}: {u['reason']}")


# ---------------------------------------------------------------- command line

def use_state_dir(path: Path) -> None:
    global STATE_DIR
    STATE_DIR = path
    adr.configure(MAIN_REPO, path)


def fail(message: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"status": "error", "message": message}))
    else:
        print(f"auto-dev-metrics: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = adr.JsonArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("run", "compare"):
        p = sub.add_parser(name)
        if name == "run":
            p.add_argument("run", nargs="?", default="latest", help="run id, or latest")
        p.add_argument("--cost", choices=("cached", "live"), default="cached")
        p.add_argument("--json", action="store_true")
        p.add_argument("--state-dir", type=Path)
    try:
        args = ap.parse_args(argv)
    except adr.UsageError as exc:
        return fail(str(exc), "--json" in argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        main_repo = wfconfig.repo_root()
        if not (main_repo / ".git").exists():
            raise RuntimeError(f"{main_repo} is not the main checkout (no .git)")
        configure(main_repo)
    except Exception as exc:  # noqa: BLE001 -- reported as the usage-level error
        return fail(str(exc), args.json)
    if args.state_dir:
        use_state_dir(args.state_dir)
    try:
        doc = build_run_document(args.run, args.cost) if args.command == "run" else build_compare_document(args.cost)
    except RunNotFound as exc:
        return fail(str(exc), args.json)
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1, default=str))
    elif args.command == "run":
        print_run(doc)
    else:
        print_compare(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
