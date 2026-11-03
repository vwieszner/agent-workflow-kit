#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Search, tally or replay tool calls across every Claude Code transcript of this project.

Usage
-----
    uv run --no-project <plugin-root>/scripts/transcript_calls.py grep [FILTERS] [--result REGEX]
        [--show-result] [--full] [--count-by agent|tool|session|day] [--samples N]
    uv run --no-project <plugin-root>/scripts/transcript_calls.py replay --hook PATH [--baseline PATH]
        [FILTERS] [--transcript-prefix] [--samples N]

    FILTERS: --tool NAME[,NAME]   --agent-type TYPE[,TYPE] (`main` = main sessions; an effort
             variant matches its base role)   --input REGEX (searched in the call's JSON input)
             --since WHEN   --until WHEN   --session ID-PREFIX
    WHEN: a date YYYY-MM-DD; an ISO time YYYY-MM-DDTHH:MM[:SS[.fff]][Z|+HH:MM|-HH:MM] (UTC when
          it has no offset); or a commit (anything `git rev-parse --verify <WHEN>^{commit}`
          resolves; a file path is not one), which stands for its committer time. --since is
          inclusive, --until exclusive. Both bounds are cut to the whole second: a fractional
          --since and a fractional --until are each truncated to the second.
    Without --since every transcript is read (several GB, minutes); pass it.

When to use
-----------
    grep    A question about what agents called or got back: which story agents Read
            the rules files, which scoped test runs returned an encoding error, which URLs were
            fetched. Prints one line per call (plus the result around the --result match),
            or a tally with --count-by. journal_grep.py answers from the journal, which keeps
            only shell commands and error snippets, cut short; this reads the transcripts
            themselves: every tool's input, every result.
            --full prints each shown call's whole input, one field per line, string fields
            verbatim (a SubagentHandback `message`, an Agent `prompt`, a Bash `command`), and the
            whole result when --result or --show-result is given. Reading a prior subagent's
            final report:
                transcript_calls.py grep --tool SubagentHandback --agent-type story-diagnose
                    --input "<story id>" --since <date> --samples 1 --full
            Before / after a change: the same grep with `--until <commit>` and with
            `--since <commit>`. The dispatches each side holds:
            `grep --tool Agent --input '"subagent_type": "<type>' --since/--until ... --samples 0`
            (Agent-tool spawns; a workflow's agents have no Agent call). Denials per spawn for a
            set of stories: harness_metrics.py --cut.
    replay  The impact of a PreToolUse check change before it ships. Feeds each selected
            historical call through a hook module's `check(payload)` (or, for a kit guard,
            `evaluate(payload)`: exit 2 or a JSON `deny` on stdout is a deny) and reports the
            calls whose verdict changes: against --baseline (a draft against the live module), or
            else against what happened (a `toolDenialKind` result, from any hook or the classifier).

Replay limits
-------------
    * Only `check(payload) -> str | None` (None = allow) or `evaluate(payload) -> int` runs; a
      module's main() never does.
    * Modules named in `config: transcripts.replay_refused` (default: phase_dispatch,
      repro_paths) read git or record files, so a replayed verdict means nothing; they are
      refused. Do not replay a draft of one of them either.
    * A module whose source reads `transcript_path` or calls `transcript_file(` needs
      --transcript-prefix: each transcript is copied line by line into a temp dir and the check
      reads the copy as it stood at the call (up to and including the call's line). A subagent's
      copy keeps its `agent-<id>.jsonl` name, so `transcript_file()` resolves to it; a check that
      wants the main transcript for a subagent call is not modelled.
    * agent_type / agent_id come from the subagent's `.meta.json`; main-session calls carry none.

Inputs: <project dirs>/<session>.jsonl and <session>/subagents[/workflows/<runId>]/agent-*.jsonl
        (+ sibling .meta.json) — project dirs per `_transcripts.project_dirs()`;
        `git rev-parse` / `git log` for a commit WHEN.
Output: stdout (UTF-8). Writes nothing but the --transcript-prefix temp dir, removed as it goes.
Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import contextlib
import io

sys.dont_write_bytecode = True  # importing a draft must not leave __pycache__ beside it
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _transcripts as tr  # noqa: E402
import wfconfig  # noqa: E402

base_role = tr.base_role
LIVE_STATE = set(wfconfig.get("transcripts.replay_refused", ["phase_dispatch", "repro_paths"]) or [])
REPO = str(wfconfig.repo_root())
BOUND = "%Y-%m-%dT%H:%M:%S"  # a prefix of every transcript timestamp, so bounds compare as strings
DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}")
# extended form only: fromisoformat also reads basic-format digits, so an all-digit short SHA
# such as 20261008 would otherwise parse as a date
TIME_RX = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?")
GIT_TIMEOUT = 30


def die(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(2)


def git(*args: str) -> subprocess.CompletedProcess:
    """`git <args>` in the repo; exits 2 when git is missing or does not answer in GIT_TIMEOUT."""
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT)
    except FileNotFoundError as exc:
        die(f"git is not runnable, so a commit WHEN cannot be resolved: {exc}")
    except subprocess.TimeoutExpired:
        die(f"git {' '.join(args)} did not finish in {GIT_TIMEOUT}s")


def commit_time(rev: str) -> str | None:
    """Committer time of `rev` (ISO with offset), or None when it names no commit."""
    if rev.startswith("-"):
        return None
    p = git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if p.returncode != 0 or not p.stdout.strip():
        return None
    p = git("log", "-1", "--format=%cI", p.stdout.strip())
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def iso_time(text: str) -> datetime.datetime | None:
    try:
        t = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def parse_when(value: str) -> str:
    """WHEN -> UTC bound in BOUND form; ValueError when it is neither a time nor a commit."""
    if DATE_RX.fullmatch(value) or TIME_RX.fullmatch(value):
        t = iso_time(value)
        if t is None:
            raise ValueError(f"{value!r} is not a valid date or ISO time")
    else:
        committed = commit_time(value)
        t = iso_time(committed) if committed else None
    if t is None:
        raise ValueError(f"{value!r} is neither a date, an ISO time nor a commit")
    return t.astimezone(datetime.timezone.utc).strftime(BOUND)


def transcripts(since: str) -> list[tuple[str, str, str, str | None]]:
    """(path, session id, agent type or 'main', agent id) per transcript, oldest first."""
    floor = 0.0
    if since:  # a file last written before --since holds no call at or after it; a date or a BOUND, UTC
        floor = datetime.datetime.fromisoformat(since).replace(tzinfo=datetime.timezone.utc).timestamp()
    found = []
    for proj in tr.project_dirs():
        for f in glob.glob(os.path.join(proj, "*.jsonl")):
            found.append((f, os.path.basename(f)[:-6], "main", None))
        subs = glob.glob(os.path.join(proj, "*", "subagents", "agent-*.jsonl"))
        subs += glob.glob(os.path.join(proj, "*", "subagents", "workflows", "*", "agent-*.jsonl"))
        for f in subs:
            try:
                with open(f[:-6] + ".meta.json", encoding="utf-8") as fh:
                    atype = json.load(fh).get("agentType") or "?"
            except (OSError, ValueError):
                atype = "?"
            sid = os.path.relpath(f, proj).split(os.sep)[0]
            found.append((f, sid, atype, os.path.basename(f)[len("agent-"):-len(".jsonl")]))
    found = [t for t in found if os.path.getmtime(t[0]) >= floor]
    return sorted(found, key=lambda t: os.path.getmtime(t[0]))


def lines(path: str):
    """(raw line, entry) per line; only a line that can hold a tool_use / tool_result is parsed."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            entry = None
            if '"tool_use"' in raw or '"tool_result"' in raw:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    pass
            yield raw, entry


def blocks(entry: dict) -> list[dict]:
    content = (entry.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(x.get("text", "") for x in content or [] if isinstance(x, dict))


def one_line(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def around(text: str, rx: re.Pattern | None, n: int = 300) -> str:
    m = rx.search(text) if rx else None
    start = max(0, m.start() - n // 2) if m else 0
    return one_line(text[start:start + n], n)


def call_line(ts: str, sid: str, atype: str, use: dict) -> str:
    return (f"{ts[:19]}  {sid[:8]}  {atype:<20} {use.get('name', '?'):<12} "
            f"{one_line(json.dumps(use.get('input'), ensure_ascii=False), 220)}")


def full_block(ts: str, sid: str, atype: str, use: dict) -> str:
    """Header line, then each input field; a multi-line or long string field is printed verbatim
    on the lines after its `--- key:` header."""
    out = [f"=== {ts[:19]}  {sid}  {atype}  {use.get('name', '?')}"]
    inp = use.get("input")
    if not isinstance(inp, dict):
        out.append(json.dumps(inp, ensure_ascii=False, indent=2))
        return "\n".join(out)
    for key, value in inp.items():
        if isinstance(value, str) and ("\n" in value or len(value) > 120):
            out.append(f"--- {key}:")
            out.append(value)
        else:
            out.append(f"--- {key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(out)


class Select:
    def __init__(self, a: argparse.Namespace):
        self.tools = {t.strip() for t in (a.tool or "").split(",") if t.strip()}
        self.types = {base_role(t.strip()) for t in (a.agent_type or "").split(",") if t.strip()}
        self.input = re.compile(a.input) if a.input else None
        self.since = a.since or ""
        self.until = a.until or ""
        self.session = a.session or ""

    def transcript(self, sid: str, atype: str) -> bool:
        return sid.startswith(self.session) and (not self.types or base_role(atype) in self.types)

    def call(self, entry: dict, block: dict) -> bool:
        if block.get("type") != "tool_use":
            return False
        ts = str(entry.get("timestamp", ""))
        if self.since and ts < self.since:
            return False
        if self.until and ts >= self.until:
            return False
        if self.tools and block.get("name") not in self.tools:
            return False
        return not self.input or bool(self.input.search(json.dumps(block.get("input"), ensure_ascii=False)))


def cmd_grep(a: argparse.Namespace, sel: Select) -> int:
    rx = re.compile(a.result) if a.result else None
    tally: collections.Counter = collections.Counter()
    total = shown = 0

    def emit(sid: str, atype: str, ts: str, use: dict, text: str | None) -> None:
        """`text` is None for a call with no result in the transcript."""
        nonlocal total, shown
        total += 1
        key = {"agent": base_role(atype), "tool": use.get("name"), "session": sid[:8], "day": ts[:10]}
        if a.count_by:
            tally[key[a.count_by]] += 1
        if shown < a.samples:
            shown += 1
            if a.full:
                print(full_block(ts, sid, atype, use))
                if (rx or a.show_result) and text is not None:
                    print("--- result:")
                    print(text)
                print()
                return
            print(call_line(ts, sid, atype, use))
            if rx or a.show_result:
                print(f"    -> {around(text or '', rx)}")

    for path, sid, atype, _aid in transcripts(a.since):
        if not sel.transcript(sid, atype):
            continue
        pending: dict = {}
        for _raw, entry in lines(path):
            if not entry:
                continue
            for b in blocks(entry):
                if sel.call(entry, b):
                    pending[b.get("id")] = (str(entry.get("timestamp", "")), b)
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    ts, use = pending.pop(b["tool_use_id"])
                    text = result_text(b.get("content"))
                    if not rx or rx.search(text):
                        emit(sid, atype, ts, use, text)
        if not rx:
            for ts, use in pending.values():  # no result in the transcript: interrupted or cut off
                emit(sid, atype, ts, use, None)
    print(f"\n{total} matching call(s)" + (f", {shown} shown" if shown < total else ""))
    for key, n in tally.most_common():
        print(f"{n:6d}  {key}")
    return 0


def load_check(path: str, name: str):
    """(check function, whether its source reads the transcript)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem in LIVE_STATE:
        die(f"refused: {stem} reads git or record files, so a replayed verdict means nothing")
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        die(f"cannot read {path}: {exc}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    check = getattr(module, "check", None)
    if not callable(check):
        evaluate = getattr(module, "evaluate", None)
        if not callable(evaluate):
            die(f"refused: {path} has neither check(payload) nor evaluate(payload)")
        check = _from_evaluate(evaluate)
    return check, ("transcript_path" in source or "transcript_file(" in source)


def _from_evaluate(evaluate):
    """Adapt a kit guard's `evaluate(payload) -> exit code` to `check(payload) -> reason | None`."""
    def check(payload: dict):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = evaluate(payload)
        if code == 2:
            return err.getvalue().strip() or "denied (exit 2)"
        if '"deny"' in out.getvalue():
            try:
                return json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecisionReason"]
            except (ValueError, KeyError, TypeError):
                return out.getvalue().strip()
        return None
    return check


def verdict(check, payload: dict) -> tuple[str, str]:
    try:
        reason = check(payload)
    except Exception as exc:  # the live hooks fail open on this; a replay reports it
        return "error", f"{type(exc).__name__}: {exc}"
    return ("deny", str(reason)) if reason else ("allow", "")


def cmd_replay(a: argparse.Namespace, sel: Select) -> int:
    hook, hook_reads = load_check(a.hook, "replay_hook")
    base, base_reads = load_check(a.baseline, "replay_baseline") if a.baseline else (None, False)
    if (hook_reads or base_reads) and not a.transcript_prefix:
        die("this check reads the transcript: pass --transcript-prefix so it sees each transcript "
            "as it stood at the call")
    moves: collections.Counter = collections.Counter()
    by_type: collections.Counter = collections.Counter()
    samples: dict = collections.defaultdict(list)

    def record(sid, atype, ts, use, before, after) -> None:
        move = f"{before[0]} -> {after[0]}"
        moves[move] += 1
        if before[0] != after[0]:
            by_type[(move, base_role(atype))] += 1
            if len(samples[move]) < a.samples:
                samples[move].append((ts, sid, atype, use, before[1], after[1]))

    tmp = tempfile.mkdtemp(prefix="transcript-replay-") if a.transcript_prefix else None
    try:
        for path, sid, atype, aid in transcripts(a.since):
            if not sel.transcript(sid, atype):
                continue
            copy = os.path.join(tmp, os.path.basename(path)) if tmp else None
            out = open(copy, "w", encoding="utf-8") if copy else None
            pending: dict = {}
            try:
                for raw, entry in lines(path):
                    if out:
                        out.write(raw)
                    if not entry:
                        continue
                    for b in blocks(entry):
                        if sel.call(entry, b):
                            if out:
                                out.flush()
                            payload = {"hook_event_name": "PreToolUse", "session_id": sid,
                                       "tool_name": b.get("name"), "tool_input": b.get("input") or {},
                                       "tool_use_id": b.get("id"), "cwd": entry.get("cwd") or ""}
                            if copy:
                                payload["transcript_path"] = copy
                            if atype != "main":
                                payload["agent_type"], payload["agent_id"] = atype, aid
                            before = verdict(base, payload) if base else None
                            pending[b.get("id")] = (str(entry.get("timestamp", "")), b, before,
                                                    verdict(hook, payload))
                        elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                            ts, use, before, after = pending.pop(b["tool_use_id"])
                            if before is None:
                                kind = entry.get("toolDenialKind")
                                denied = kind and kind != "interrupted"
                                before = ("deny", f"[{kind}] {result_text(b.get('content'))}") if denied \
                                    else ("allow", "")
                            record(sid, atype, ts, use, before, after)
                for ts, use, before, after in pending.values():
                    record(sid, atype, ts, use, before or ("no-result", ""), after)
            finally:
                if out:
                    out.close()
                    os.remove(copy)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    against = f"baseline {a.baseline}" if a.baseline else "what happened (toolDenialKind, any source)"
    print(f"replay of {a.hook} against {against}")
    for move, n in sorted(moves.items()):
        old, new = move.split(" -> ")
        changed = old != new
        types = ", ".join(f"{t} {c}" for (m, t), c in by_type.most_common() if m == move)
        print(f"  {move:<22} {n:6d}" + ("  CHANGED" if changed else "") + (f"  ({types})" if types else ""))
    for move, rows in samples.items():
        print(f"\n=== {move}")
        for ts, sid, atype, use, why_before, why_after in rows:
            print(call_line(ts, sid, atype, use))
            if why_before:
                print(f"    before: {one_line(why_before, 300)}")
            if why_after:
                print(f"    after:  {one_line(why_after, 300)}")
    return 0


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)  # absent when sys.stdout is replaced (io.StringIO)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")  # a report's text is not cp1252-safe
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grep", help="list or tally matching calls")
    r = sub.add_parser("replay", help="replay matching calls through a hook module's check(payload)")
    for p in (g, r):
        p.add_argument("--tool", help="tool name(s), comma-separated (Bash,PowerShell,Agent,Read,...)")
        p.add_argument("--agent-type", help="agent type(s), comma-separated; main = main sessions")
        p.add_argument("--input", help="regex searched in the call's JSON input")
        p.add_argument("--since", help="WHEN: calls at or after it (YYYY-MM-DD, an ISO time - UTC without "
                                       "an offset, cut to the second - or a commit)")
        p.add_argument("--until", help="WHEN: calls before it (same forms as --since)")
        p.add_argument("--session", default="", help="session id prefix")
    g.add_argument("--result", help="regex the call's result must match; prints the text around it")
    g.add_argument("--show-result", action="store_true", help="print the start of every result")
    g.add_argument("--full", action="store_true",
                   help="print each shown call's whole input (string fields verbatim) and, with "
                        "--result/--show-result, its whole result")
    g.add_argument("--count-by", choices=("agent", "tool", "session", "day"))
    g.add_argument("--samples", type=int, default=20, help="calls printed (default 20; 0 = tally only)")
    r.add_argument("--hook", required=True, help="module whose check(payload) is replayed (draft or live)")
    r.add_argument("--baseline", help="module to compare against (usually the live hook); default: what happened")
    r.add_argument("--transcript-prefix", action="store_true",
                   help="give the check a copy of the transcript up to each call (required when it reads one)")
    r.add_argument("--samples", type=int, default=8, help="changed calls printed per verdict change (default 8)")
    a = ap.parse_args()
    for name in ("since", "until"):
        if getattr(a, name):
            try:
                setattr(a, name, parse_when(getattr(a, name)))
            except ValueError as e:
                ap.error(f"--{name}: {e}")
    if a.since and a.until and a.since >= a.until:
        ap.error(f"--since {a.since} is not before --until {a.until}")
    sel = Select(a)
    return cmd_grep(a, sel) if a.cmd == "grep" else cmd_replay(a, sel)


if __name__ == "__main__":
    sys.exit(main())
