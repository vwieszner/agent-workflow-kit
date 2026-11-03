#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Report one auto-dev driver run from its own files: driver.log, sessions.jsonl and each phase session's stream.

Purpose
-------
Reads `.workflow/state/auto-dev/<run-id>/driver.log`, `sessions.jsonl` and `session-<n>.jsonl` (the
`claude -p --output-format stream-json` streams `auto_dev_driver.py` writes). Read-only, stdlib-only.
The state directory defaults to the main checkout's (`wfconfig.repo_root()`), so the report works from any
worktree.

Per session:
  * init: tool version, permission mode, model, session id (what `claude --resume` takes);
  * state: `running` (no `result` event yet; age of the last write) or the `result` event's
    subtype / is_error, duration, turns, `total_cost_usd` (includes subagents);
  * the `AUTO-DEV-PHASE` line of the final text, parsed into story / outcome / step;
  * `model_usage`: the result's `modelUsage` per model (cost, input, output, cache-read, cache-creation);
  * `fingerprint`: the session's `sessions.jsonl` record without its `knobs`;
  * `permission_denials` from the result, and every tool result with `is_error: true` (main session and
    subagents, tagged), first line only, with the `input` of the tool call that caused it;
  * the Agent / Task / Skill / Workflow dispatches of the main session;
  * main-session tool-call counts, and the last N main-session actions;
  * `unparsable_lines`: how many stream lines were not JSON, and the first such line number.
A non-claude session stream (OpenCode) is plain text: it reports no init, result or dispatches.

Per run: `driver_log`; `knobs` and `knobs_changes` (the field-level diff between consecutive sessions whose
`knobs_id` differs); `harness_ids`; `totals` (spend, minutes, stories by outcome, driver stops, unpriced
sessions, spend by model); `fingerprint`.

Every named field is a section: `status` ok / none / error, `source`, and `reason` / `error`.

Usage
-----
    uv run --no-project .workflow/scripts/auto_dev_run_report.py [<run-id> | latest] [--session N] [--last N]
        [--json] [--state-dir DIR]

When to use: watching a run in flight (the `auto-dev-run` skill), or reading what a finished run did and under
which configuration.

Exit 0 when the run directory exists, 2 when it does not or on a usage error (one JSON error line under
`--json`, a stderr line otherwise).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import auto_dev_driver  # noqa: E402
import wfconfig  # noqa: E402

class _Sections:
    """Section constructors: `ok`, `none`, `err`; `guard` turns a raising reader into an error section."""

    @staticmethod
    def ok(source, **fields) -> dict:
        return {"status": "ok", "source": str(source), **fields}

    @staticmethod
    def none(source, reason: str) -> dict:
        return {"status": "none", "source": str(source), "reason": reason}

    @staticmethod
    def err(source, error: str) -> dict:
        return {"status": "error", "source": str(source), "error": error}

    @staticmethod
    def guard(source, name: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- one failing reader becomes its section's error
            return {"status": "error", "source": str(source), "error": f"{name}: {type(exc).__name__}: {exc}"}

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def read_jsonl(path: Path, strict: bool = False) -> list:
        out = []
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                if strict:
                    raise ValueError(f"line {n}: {exc}") from None
        return out

    RUN_ID_RE = re.compile(r"^\d{8}-\d{6}$")


ss = _Sections


MAIN_REPO: Path | None = None
STATE: Path | None = None
LOCAL_TZ = None  # None: the host's zone; tests pin one
PHASE_LINE = re.compile(r"^AUTO-DEV-PHASE .*$", re.M)
DISPATCH_TOOLS = ("Agent", "Task", "Skill", "Workflow")
DRIFT_KEYS = ("harness_uncommitted", "harness_changed_before", "harness_changed_during")
ABSENT = "(absent)"
USAGE_KEYS = (("cost_usd", "costUSD"), ("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
              ("cache_read_tokens", "cacheReadInputTokens"), ("cache_creation_tokens", "cacheCreationInputTokens"))


class UsageError(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def default_state_dir() -> Path:
    return wfconfig.workflow_dir() / "state" / "auto-dev"


def configure(main_repo, state_dir=None) -> None:
    """Point the module at `main_repo` (the main checkout) and, unless `state_dir` is given, its run directory."""
    global MAIN_REPO, STATE
    MAIN_REPO = Path(main_repo)
    STATE = Path(state_dir) if state_dir else MAIN_REPO / ".workflow" / "state" / "auto-dev"


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def run_started(run_id: str) -> datetime:
    """The run id (named in UTC by the driver) as an aware datetime."""
    return datetime.strptime(run_id, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


def run_ids(state: Path) -> list[str]:
    """Every run directory name under `state` matching `RUN_ID_RE`, oldest first."""
    if not state.is_dir():
        return []
    return sorted(p.name for p in state.iterdir() if p.is_dir() and ss.RUN_ID_RE.match(p.name))


def run_dir(arg: str, state: Path | None = None) -> Path | None:
    state = state or STATE
    if arg == "latest":
        ids = run_ids(state)
        return state / ids[-1] if ids else None
    if not ss.RUN_ID_RE.match(arg):
        return None
    d = state / arg
    return d if d.is_dir() else None


def blocks(event: dict) -> list[dict]:
    content = (event.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def model_usage_section(path: Path, usage) -> dict:
    if not isinstance(usage, dict) or not usage:
        return ss.none(path, "the result event carries no modelUsage" if usage is None else
                       "the result event's modelUsage is empty")
    models = {m: {out: (row or {}).get(key) for out, key in USAGE_KEYS} for m, row in usage.items()}
    return ss.ok(path, models=models, format="internal")


def session_report(path: Path, last: int) -> dict:
    n = int(path.stem.split("-")[1])
    rep: dict = {"session": n, "events": 0, "state": "running", "dispatches": [],
                 "errors": [], "tool_counts": {}, "last_actions": []}
    calls: Counter = Counter()
    actions: list[str] = []
    names: dict[str, tuple[str, str]] = {}
    final_text = ""
    usage = None
    unparsable, first_bad = 0, None
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            unparsable += 1
            first_bad = first_bad or number
            continue
        if not isinstance(ev, dict):
            unparsable += 1
            first_bad = first_bad or number
            continue
        rep["events"] += 1
        kind, sub = ev.get("type"), ev.get("subtype")
        nested = bool(ev.get("parent_tool_use_id"))
        if kind == "system" and sub == "init":
            rep["init"] = {k: ev.get(k) for k in ("claude_code_version", "permissionMode", "model", "session_id")}
        elif kind == "assistant":
            for b in blocks(ev):
                if b.get("type") == "tool_use":
                    names[b.get("id", "")] = (b.get("name", ""),
                                              json.dumps(b.get("input") or {}, ensure_ascii=False)[:300])
                    if nested:
                        continue
                    name, inp = b.get("name", ""), b.get("input") or {}
                    calls[name] += 1
                    if name in DISPATCH_TOOLS:
                        rep["dispatches"].append({"tool": name, "type": inp.get("subagent_type") or inp.get("skill"),
                                                  "what": inp.get("description") or inp.get("args") or ""})
                    actions.append(f"{name} {json.dumps(inp, ensure_ascii=False)[:140]}")
                elif b.get("type") == "text" and not nested:
                    final_text = b.get("text", "")
                    actions.append("TEXT " + final_text[:140].replace("\n", " | "))
        elif kind == "user":
            for b in blocks(ev):
                if b.get("type") == "tool_result" and b.get("is_error"):
                    lines = result_text(b).strip().splitlines()
                    # A hook denial's first line is generic; its rule is the first "- [rule]" line.
                    rule = next((ln.strip() for ln in lines if re.match(r"\s*- \[", ln)), "")
                    text = " ".join(x for x in (lines[:1] + [rule]) if x)
                    name, inp = names.get(b.get("tool_use_id", ""), ("?", ""))
                    rep["errors"].append({"where": "subagent" if nested else "main",
                                          "parent": ev.get("parent_tool_use_id"),
                                          "tool": name, "input": inp,
                                          "text": text[:260]})
        elif kind == "result":
            rep["state"] = "ended"
            rep["result"] = {k: ev.get(k) for k in ("subtype", "is_error", "num_turns", "total_cost_usd")}
            rep["result"]["minutes"] = round((ev.get("duration_ms") or 0) / 60000, 1)
            rep["permission_denials"] = ev.get("permission_denials") or []
            final_text = ev.get("result") or final_text
            usage = ev.get("modelUsage")
    phase = PHASE_LINE.findall(final_text or "")
    rep["phase_line"] = phase[-1] if phase else None
    parsed = auto_dev_driver.phase_line(rep["phase_line"]) if rep["phase_line"] else None
    rep.update(story=parsed["story"] if parsed else None, outcome=parsed["outcome"] if parsed else None,
               step=parsed["step"] if parsed else None)
    rep["unparsable_lines"] = {"count": unparsable, "first_line": first_bad}
    rep["tool_counts"] = dict(calls.most_common())
    rep["last_actions"] = actions[-last:]
    if rep["state"] == "ended":
        rep["model_usage"] = model_usage_section(path, usage)
    else:
        rep["model_usage"] = ss.none(path, "the session is running: no result event, so no modelUsage yet")
        rep["idle_seconds"] = round(time.time() - path.stat().st_mtime)
    return rep


# ---------------------------------------------------------------- knobs and fingerprint

def flatten(blob, prefix: str = "") -> dict:
    """Dotted leaf paths of a nested blob; an empty dict is a leaf."""
    if isinstance(blob, dict) and blob:
        out: dict = {}
        for key, value in blob.items():
            out.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: blob}


def inner_dicts(blob, prefix: str = "") -> dict:
    """Dotted path -> value of every non-empty dict inside a nested blob (`flatten` keeps only the leaves)."""
    if not (isinstance(blob, dict) and blob):
        return {}
    out: dict = {prefix: blob} if prefix else {}
    for key, value in blob.items():
        out.update(inner_dicts(value, f"{prefix}.{key}" if prefix else str(key)))
    return out


def knobs_diff(before: dict, after: dict) -> list[str]:
    """`path: <json before> → <json after>` per differing leaf of two knob blobs, never the root path `''` that an
    empty blob flattens to; `(absent)` only for a path a blob does not hold: a path that is a dict in one blob and
    a leaf in the other shows the dict."""
    a, b = flatten(before), flatten(after)
    inner_a, inner_b = inner_dicts(before), inner_dicts(after)

    def show(leaves, inner, path):
        if path in leaves:
            return json.dumps(leaves[path], ensure_ascii=False)
        return json.dumps(inner[path], ensure_ascii=False) if path in inner else ABSENT

    return [f"{path}: {show(a, inner_a, path)} → {show(b, inner_b, path)}" for path in sorted(a.keys() | b.keys())
            if path and (path not in a or path not in b or a[path] != b[path])]


def load_run(d: Path, last: int = 6) -> dict:
    """Everything a run directory holds: records (or why none), driver.log lines, one report per stream."""
    sessions_path, log_path = d / "sessions.jsonl", d / "driver.log"
    data: dict = {"dir": d, "run_id": d.name, "records": None, "records_error": None,
                  "records_path": sessions_path, "log": None, "log_path": log_path, "log_error": None}
    if sessions_path.is_file():
        try:
            data["records"] = ss.read_jsonl(sessions_path, strict=True)
        except (OSError, ValueError) as exc:
            data["records_error"] = f"cannot read sessions.jsonl: {exc}"
    if log_path.is_file():
        try:
            data["log"] = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            data["log_error"] = f"cannot read driver.log: {exc}"
    streams = sorted((p for p in d.glob("session-*.jsonl") if re.fullmatch(r"session-\d+\.jsonl", p.name)),
                     key=lambda p: int(p.stem.split("-")[1]))
    data["reports"] = [session_report(p, last) for p in streams]
    return data


def session_facts(data: dict) -> list[dict]:
    """One fact per session, from `sessions.jsonl` when the run has it, else from the streams' phase lines."""
    if data["records"] is not None:
        return [{"session": r.get("session"), "story": r.get("story"), "outcome": r.get("outcome"),
                 "step": r.get("step"), "is_error": bool(r.get("is_error")), "cost_usd": r.get("cost_usd"),
                 "minutes": r.get("minutes"), "knobs_id": r.get("knobs_id")} for r in data["records"]]
    return [{"session": rep["session"], "story": rep["story"], "outcome": rep["outcome"], "step": rep["step"],
             "is_error": bool((rep.get("result") or {}).get("is_error")),
             "cost_usd": (rep.get("result") or {}).get("total_cost_usd"),
             "minutes": (rep.get("result") or {}).get("minutes"), "knobs_id": None}
            for rep in data["reports"] if rep["state"] == "ended" or rep["phase_line"]]


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fingerprint_of(rec: dict, source) -> dict:
    return ss.ok(source, **{k: v for k, v in rec.items() if k != "knobs"},
                 not_recorded=[k for k in DRIFT_KEYS if k not in rec])


def run_sections(data: dict) -> dict:
    """The run-level sections (`driver_log` … `fingerprint`)."""
    d, records, sp = data["dir"], data["records"], data["records_path"]
    log_path = data["log_path"]
    if data["log"] is None:
        driver_log = (ss.err(log_path, data["log_error"]) if data["log_error"]
                      else ss.none(log_path, f"no driver.log in {d}"))
    elif not data["log"]:
        driver_log = ss.none(log_path, "driver.log is empty")
    else:
        driver_log = ss.ok(log_path, lines=data["log"])
    reports = data["reports"]
    holding = sum(1 for r in reports if r["state"] == "ended")
    if records is None:
        if data["records_error"]:
            bad = ss.err(sp, data["records_error"])
            return {"driver_log": driver_log, "knobs": bad, "knobs_changes": bad, "harness_ids": bad,
                    "totals": bad, "fingerprint": bad}
        reason = f"no sessions.jsonl; {holding} of {len(reports)} session streams hold a result event"
        missing = ss.none(sp, f"no sessions.jsonl in {d}")
        return {"driver_log": driver_log, "knobs": missing, "knobs_changes": missing, "harness_ids": missing,
                "totals": missing, "fingerprint": ss.none(sp, reason)}
    if not records:
        empty = ss.none(sp, "sessions.jsonl holds no record")
        return {"driver_log": driver_log, "knobs": empty, "knobs_changes": empty, "harness_ids": empty,
                "totals": empty, "fingerprint": empty}

    changes = []
    for prev, cur in zip(records, records[1:]):
        if prev.get("knobs_id") != cur.get("knobs_id"):
            changes.append({"session": cur.get("session"), "previous_session": prev.get("session"),
                            "knobs_id_before": prev.get("knobs_id"), "knobs_id_after": cur.get("knobs_id"),
                            "changes": knobs_diff(prev.get("knobs") or {}, cur.get("knobs") or {})})
    seen: dict = {}
    for rec in records:
        seen.setdefault(rec.get("harness_id"), {"harness_id": rec.get("harness_id"),
                                                 "first_session": rec.get("session"), "head": rec.get("head")})
    facts = session_facts(data)
    last_outcome: dict = {}
    for f in facts:
        if f["story"] not in (None, "none") and f["outcome"]:
            last_outcome[f["story"]] = f["outcome"]
    by_model: dict = {}
    for rep in reports:
        mu = rep["model_usage"]
        if mu["status"] == "ok":
            for model, row in mu["models"].items():
                if is_number(row["cost_usd"]):
                    by_model[model] = by_model.get(model, 0.0) + row["cost_usd"]
    totals = ss.ok(sp, usd=round(sum(f["cost_usd"] for f in facts if is_number(f["cost_usd"])), 6),
                   minutes=round(sum(f["minutes"] for f in facts if is_number(f["minutes"])), 2),
                   stories_by_outcome=dict(Counter(last_outcome.values())),
                   driver_stops=[f["session"] for f in facts if f["is_error"] or f["outcome"] is None],
                   unpriced_sessions=[f["session"] for f in facts if not is_number(f["cost_usd"])],
                   usd_by_model={m: round(v, 6) for m, v in by_model.items()})
    return {
        "driver_log": driver_log,
        "knobs": ss.ok(sp, knobs=records[0].get("knobs"), knobs_id=records[0].get("knobs_id")),
        "knobs_changes": ss.ok(sp, changes=changes),
        "harness_ids": ss.ok(sp, ids=list(seen.values())),
        "totals": totals,
        "fingerprint": ss.ok(sp, records=len(records),
                             knobs_ids=list(dict.fromkeys(r.get("knobs_id") for r in records))),
    }


def build_run(d: Path, last: int = 6, only_session: int | None = None, data: dict | None = None) -> dict:
    data = data or load_run(d, last)
    by_session = {r.get("session"): r for r in data["records"] or []}
    sections = ss.guard(d, "run_sections", lambda: run_sections(data))
    if sections.get("status") == "error":
        sections = {k: sections for k in ("driver_log", "knobs", "knobs_changes", "harness_ids", "totals", "fingerprint")}
    sessions = []
    for rep in data["reports"]:
        if only_session is not None and rep["session"] != only_session:
            continue
        rec = by_session.get(rep["session"])
        if rec is not None:
            rep["fingerprint"] = fingerprint_of(rec, data["records_path"])
        elif data["records_error"]:
            rep["fingerprint"] = ss.err(data["records_path"], data["records_error"])
        else:
            rep["fingerprint"] = ss.none(data["records_path"], f"no sessions.jsonl line for session {rep['session']}")
        sessions.append(rep)
    return {"status": "ok", "source": str(d), "run_id": d.name,
            "started_utc": run_started(d.name).strftime("%Y-%m-%dT%H:%M:%SZ"), **sections, "sessions": sessions}


def build_document(d: Path, state: Path, last: int = 6, only_session: int | None = None) -> dict:
    return {"generated_at": ss.now_iso(), "state_dir": str(state), "run": build_run(d, last, only_session)}


# ---------------------------------------------------------------- markdown

def section_note(sec: dict) -> str:
    return sec.get("reason") or sec.get("error") or ""


def print_report(run: dict) -> None:
    print(f"# auto-dev run {run['run_id']}\n\n## driver.log")
    log = run["driver_log"]
    print("\n".join(f"    {line}" for line in log["lines"]) if log["status"] == "ok" else f"    ({section_note(log)})")
    for s in run["sessions"]:
        print(f"\n## session-{s['session']} — {s['state']}, {s['events']} events")
        if "init" in s:
            i = s["init"]
            print(f"- init: {i['claude_code_version']}, mode {i['permissionMode']}, {i['model']}")
        if s["state"] == "ended":
            r = s["result"]
            print(f"- result: {r['subtype']}, is_error {r['is_error']}, {r['minutes']} min, "
                  f"{r['num_turns']} turns, ${float(r['total_cost_usd'] or 0):.2f}")
        else:
            print(f"- last write {s['idle_seconds']} s ago")
        fp = s["fingerprint"]
        if fp["status"] == "ok":
            print(f"- fingerprint: knobs {fp['knobs_id']}, harness {fp['harness_id']}, head {fp['head']}"
                  + (f" (not recorded: {', '.join(fp['not_recorded'])})" if fp["not_recorded"] else ""))
        else:
            print(f"- fingerprint ({fp['status']}): {section_note(fp)}")
        mu = s["model_usage"]
        if mu["status"] == "ok":
            print("- model usage: " + ", ".join(f"{m} ${row['cost_usd']}" for m, row in mu["models"].items()))
        print(f"- phase line: {s['phase_line'] or '(none yet)'}")
        if s["unparsable_lines"]["count"]:
            print(f"- unparsable lines: {s['unparsable_lines']['count']} (first at line {s['unparsable_lines']['first_line']})")
        print(f"- permission denials: {len(s.get('permission_denials', []))}")
        for den in s.get("permission_denials", []):
            print(f"    - {json.dumps(den, ensure_ascii=False)[:200]}")
        print(f"- tool errors: {len(s['errors'])}")
        for e in s["errors"]:
            print(f"    - [{e['where']}] {e['tool']}: {e['text']}")
            print(f"      input: {e['input']}" + (f" (parent {e['parent']})" if e["parent"] else ""))
        print("- dispatches:")
        for x in s["dispatches"] or [{"tool": "(none)", "type": "", "what": ""}]:
            print(f"    - {x['tool']} {x['type'] or ''} — {str(x['what'])[:120]}")
        print("- main tool calls: " + ", ".join(f"{k} {v}" for k, v in s["tool_counts"].items()))
        print("- last actions:")
        for a in s["last_actions"]:
            print(f"    - {a}")
    print("\n## knobs changes")
    kc = run["knobs_changes"]
    if kc["status"] != "ok":
        print(f"    ({kc['status']}: {section_note(kc)})")
    elif not kc["changes"]:
        print("    (none: every session ran the same knobs)")
    for c in kc.get("changes", []):
        print(f"- session {c['session']} (after {c['previous_session']}): knobs {c['knobs_id_before']} → {c['knobs_id_after']}")
        for line in c["changes"]:
            print(f"    - {line}")
    ids = run["harness_ids"]
    if ids["status"] == "ok":
        print("\n## harness ids")
        for h in ids["ids"]:
            print(f"- {h['harness_id']} from session {h['first_session']}, head {h['head']}")
    totals = run["totals"]
    if totals["status"] == "ok":
        print(f"\n## totals\n- ${totals['usd']} over {totals['minutes']} min; stories by outcome {totals['stories_by_outcome']}"
              f"; driver stops {totals['driver_stops']}; unpriced sessions {totals['unpriced_sessions']}")


def fail(message: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"status": "error", "message": message}))
    else:
        print(f"auto-dev-run-report: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = JsonArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", nargs="?", default="latest", help="run id under .workflow/state/auto-dev, or latest")
    ap.add_argument("--session", type=int, help="report only session N")
    ap.add_argument("--last", type=int, default=6, help="last N main-session actions per session")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--state-dir", type=Path, help="auto-dev state directory (default: the main checkout's)")
    try:
        args = ap.parse_args(argv)
    except UsageError as exc:
        return fail(str(exc), "--json" in argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    state = args.state_dir or default_state_dir()
    d = run_dir(args.run, state)
    if d is None:
        return fail(f"no run directory for {args.run!r} under {state}", args.json)
    doc = build_document(d, state, args.last, args.session)
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1, default=str))
    else:
        print_report(doc["run"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
