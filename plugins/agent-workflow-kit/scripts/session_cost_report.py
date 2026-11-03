#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Session cost / context forensics over this project's Claude Code transcripts.

Walks every main transcript (`*.jsonl`) plus its subagent transcripts
(`<session>/subagents/agent-*.jsonl` + sibling `.meta.json`) and emits one row per
transcript: agent type, model, request count, peak/median context, cost split into
input / output / cache-read / cache-write(5m) / cache-write(1h) / advisor, and a
per-step context-growth attribution (assistant output vs. tool results vs. injected
text). Writes a single `transcripts.json` — one row per main/subagent transcript,
plus per-step detail for each.

ACCOUNTING RULES
----------------
  * Usage lines DUPLICATE per content block. Dedupe by requestId GLOBALLY (subagent files can
    also be linked into the main session's tree) and keep the LAST line — subagent lines stream
    partial usage, so an early line for a requestId is not final.
  * Advisor usage is priced at the ADVISOR model, not the caller's model.
  * A request whose final usage line never arrived (process killed, early hand-back) gets an output
    estimate from historical mean output tokens (`MEAN_OUT`), reported separately, never dropped.

PRICE TABLE
-----------
`P` ($/MTok: input, output, cache_read, cache_write_5m, cache_write_1h) is a default table keyed by
model id. `config: cost.prices` (a table of model id -> [in, out, cache_read, cw5m, cw1h]) replaces
entries or adds models; `config: cost.mean_output_tokens` (a table "main:<model>" / "sub:<model>"
-> tokens) replaces `MEAN_OUT` entries. Check the provider's current price list before trusting a
report; a model missing from the table is priced at $0 (its tokens are still counted) and listed
under `unpriced`.

Usage
-----
    uv run --no-project <plugin-root>/scripts/session_cost_report.py
    uv run --no-project <plugin-root>/scripts/session_cost_report.py --since 2026-09-20
    uv run --no-project <plugin-root>/scripts/session_cost_report.py --since 2026-09-20 --out /tmp/cost-report

`--out` defaults to a new directory under the system temp dir (never inside a scripts dir —
this is a manual reporting tool, not a build artifact). Project dirs: `_transcripts.project_dirs()`.

Manual tool only — never wire this into a hook or the gate. Over the full
transcript tree it reads several GB, which is far too slow for anything
latency-sensitive.

Exit codes: 0 = wrote a report (even if 0 transcripts matched); 1 = no project transcript
dir exists.
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
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _transcripts as tr  # noqa: E402
import wfconfig  # noqa: E402

# $/MTok: input, output, cache_read, cache_write_5m, cache_write_1h (defaults; see PRICE TABLE).
P = {
    "claude-opus-5-5": (4, 20, 0.20, 5.0, 8.0),
    "claude-opus-5": (5, 25, 0.50, 6.25, 10.0),
    "claude-opus-4-8": (5, 25, 0.50, 6.25, 10.0),
    "claude-opus-4-7": (5, 25, 0.50, 6.25, 10.0),
    "claude-opus-4-6": (5, 25, 0.50, 6.25, 10.0),
    "claude-fable-5-1": (10, 50, 0.25, 12.5, 20.0),
    "claude-mythos-5-1": (10, 50, 0.25, 12.5, 20.0),
    "claude-fable-5": (10, 50, 1.0, 12.5, 20.0),
    "claude-mythos-5": (10, 50, 1.0, 12.5, 20.0),
    "claude-sonnet-5-5": (2, 10, 0.20, 2.5, 4.0),
    "claude-sonnet-5": (2, 10, 0.20, 2.5, 4.0),
    "claude-sonnet-4-6": (3, 15, 0.30, 3.75, 6.0),
    "claude-haiku-4-5": (1, 5, 0.10, 1.25, 2.0),
}

# Historical mean output tokens per (is_subagent, model), used only to estimate
# cost for requests whose final usage line never arrived (see module docstring).
MEAN_OUT = {
    (False, "claude-opus-5-5"): 1348, (False, "claude-opus-5"): 1045, (True, "claude-opus-5-5"): 1045,
    (True, "claude-sonnet-5"): 1847, (True, "claude-fable-5-1"): 4219, (True, "claude-opus-5"): 1157,
    (False, "claude-fable-5-1"): 2540, (False, "claude-opus-4-7"): 446,
}


def _apply_config() -> None:
    for model, row in (wfconfig.get("cost.prices", {}) or {}).items():
        if isinstance(row, (list, tuple)) and len(row) == 5:
            P[str(model)] = tuple(float(x) for x in row)
    for key, tokens in (wfconfig.get("cost.mean_output_tokens", {}) or {}).items():
        kind, _, model = str(key).partition(":")
        if kind in ("main", "sub") and model:
            MEAN_OUT[(kind == "sub", model)] = int(tokens)


_apply_config()


def norm_model(m):
    if not m:
        return "?"
    m = m.replace("[1m]", "")
    m = re.sub(r"-\d{8}$", "", m)
    return m


def price(model, it):
    p = P.get(norm_model(model))
    if not p:
        return None
    cc = it.get("cache_creation") or {}
    c5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
    c1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
    ctot = it.get("cache_creation_input_tokens", 0) or 0
    if c5 + c1 < ctot:
        c5 += ctot - c5 - c1
    return {
        "in": (it.get("input_tokens", 0) or 0) * p[0] / 1e6,
        "out": (it.get("output_tokens", 0) or 0) * p[1] / 1e6,
        "cr": (it.get("cache_read_input_tokens", 0) or 0) * p[2] / 1e6,
        "cw5": c5 * p[3] / 1e6,
        "cw1": c1 * p[4] / 1e6,
    }


def ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def content_len(c):
    if c is None:
        return 0
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        n = 0
        for x in c:
            if isinstance(x, dict):
                if x.get("type") == "text":
                    n += len(x.get("text", ""))
                elif x.get("type") == "image":
                    n += 6000  # ~1.5k tokens
                elif x.get("type") == "tool_result":
                    n += content_len(x.get("content"))
                else:
                    n += len(json.dumps(x))
            else:
                n += len(str(x))
        return n
    if isinstance(c, dict):
        return len(json.dumps(c))
    return len(str(c))


def tool_label(name, inp):
    inp = inp or {}
    if name == "Bash" or name == "PowerShell":
        cmd = (inp.get("command") or "").strip().replace("\n", " ")
        return f"{name}:{cmd[:90]}"
    if name == "Read":
        return f"Read:{inp.get('file_path','')}"
    if name == "Skill":
        return f"Skill:{inp.get('skill','')}"
    if name == "Agent":
        return f"Agent:{inp.get('subagent_type','')}:{(inp.get('description') or '')[:50]}"
    if name.startswith("mcp__"):
        return name
    return name


def analyze_file(path, keep_steps=False, is_sub=False):
    reqs = collections.OrderedDict()  # requestId -> dict; last-write-wins (see module docstring)
    tool_uses = {}  # id -> (name, label, req_id)
    results = []  # (ts, tool_use_id, chars)
    injected = []  # (ts, kind, chars)
    compacts = 0
    title = None
    entry = None
    event_ts = []
    effort = collections.Counter()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("type")
            if t == "custom-title":
                title = r.get("customTitle")
            if r.get("entrypoint") and not entry:
                entry = r.get("entrypoint")
            tstamp = r.get("timestamp")
            if tstamp:
                event_ts.append(tstamp)
            if t == "system" and r.get("subtype") == "compact_boundary":
                compacts += 1
            if t == "assistant":
                m = r.get("message", {})
                rid = r.get("requestId") or m.get("id")
                d = reqs.get(rid)
                if d is None:
                    d = reqs[rid] = {
                        "ts": tstamp,
                        "model": m.get("model"),
                        "usage": m.get("usage") or {},
                        "tools": [],
                        "effort": r.get("effort"),
                        "advisorModel": r.get("advisorModel"),
                        "text_chars": 0,
                    }
                    effort[r.get("effort")] += 1
                else:
                    # dedupe by requestId: usage lines duplicate per content block;
                    # subagent transcripts stream partial usage, so keep the LAST line.
                    d["usage"] = m.get("usage") or d["usage"]
                for c in m.get("content") or []:
                    if c.get("type") == "server_tool_use" and c.get("name") == "advisor":
                        d["adv_blocks"] = d.get("adv_blocks", 0) + 1
                    if c.get("type") == "tool_use":
                        lab = tool_label(c.get("name", ""), c.get("input"))
                        tool_uses[c.get("id")] = (c.get("name"), lab, rid)
                        d["tools"].append(lab)
                    elif c.get("type") == "text":
                        d["text_chars"] += len(c.get("text", ""))
            elif t == "user":
                m = r.get("message", {})
                c = m.get("content")
                if isinstance(c, list):
                    for x in c:
                        if isinstance(x, dict) and x.get("type") == "tool_result":
                            results.append((tstamp, x.get("tool_use_id"), content_len(x.get("content"))))
                        elif isinstance(x, dict):
                            injected.append((tstamp, "user-text" if not r.get("isMeta") else "meta-text", content_len([x])))
                else:
                    injected.append((tstamp, "user-text" if not r.get("isMeta") else "meta-text", content_len(c)))
            elif t == "attachment":
                a = r.get("attachment", {})
                n = sum(len(x.get("content", "")) for x in (r.get("rendered") or []) if isinstance(x, dict))
                injected.append((tstamp, "att:" + str(a.get("type")), n))
    # per request pricing & context
    rows = []
    for rid, d in reqs.items():
        u = d["usage"]
        its = u.get("iterations") or [dict(u, type="message")]
        cost = collections.Counter()
        # Per-request token split from the same iterations that are priced:
        # tok = executor iterations, adv_tok = advisor iterations. cw5 absorbs any
        # cache-creation total not broken down by TTL, exactly as price() does.
        tok = collections.Counter()
        adv_tok = collections.Counter()
        unpriced = set()
        exec_ctx = []
        adv_calls = 0
        adv_in = 0
        out_tok = 0
        think = (u.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0
        for it in its:
            mdl = it.get("model") or d["model"]
            itc = it.get("cache_creation") or {}
            i5 = itc.get("ephemeral_5m_input_tokens", 0) or 0
            i1 = itc.get("ephemeral_1h_input_tokens", 0) or 0
            itot = it.get("cache_creation_input_tokens", 0) or 0
            if i5 + i1 < itot:
                i5 += itot - i5 - i1
            split = {"in": it.get("input_tokens", 0) or 0, "out": it.get("output_tokens", 0) or 0,
                     "cr": it.get("cache_read_input_tokens", 0) or 0, "cw5": i5, "cw1": i1}
            if it.get("type") == "advisor_message":
                # advisor usage is priced at the ADVISOR model, not the caller's.
                adv_calls += 1
                adv_in += (it.get("input_tokens", 0) or 0) + (it.get("cache_read_input_tokens", 0) or 0) + (it.get("cache_creation_input_tokens", 0) or 0)
                adv_tok.update(split)
                pr = price(mdl, it)
                if pr is None:
                    unpriced.add(norm_model(mdl))
                cost["advisor"] += sum((pr or {}).values())
                continue
            tok.update(split)
            pr = price(mdl, it)
            if pr is None:
                unpriced.add(norm_model(mdl))
                cost["unpriced"] += 0
                continue
            for k, v in pr.items():
                cost[k] += v
            exec_ctx.append((it.get("input_tokens", 0) or 0) + (it.get("cache_read_input_tokens", 0) or 0) + (it.get("cache_creation_input_tokens", 0) or 0))
            out_tok += it.get("output_tokens", 0) or 0
        # partial-usage requests (no final iterations recorded): estimate output
        # and missing advisor calls from historical means rather than dropping them.
        final = bool(u.get("iterations"))
        if not final:
            p = P.get(norm_model(d["model"]))
            if p:
                mean_out = MEAN_OUT.get((is_sub, norm_model(d["model"])), 1200)
                missing = max(0, mean_out - out_tok)
                cost["est_out"] += missing * p[1] / 1e6
            nb = d.get("adv_blocks", 0) - adv_calls
            if nb > 0 and exec_ctx:
                # priced at the advisor model the transcript records, as a measured advisor call is.
                ap = P.get(norm_model(d["advisorModel"]))
                if ap is None:
                    unpriced.add(norm_model(d["advisorModel"]))
                else:
                    cost["est_advisor"] += nb * (exec_ctx[0] * ap[0] + 11000 * ap[1]) / 1e6
                adv_calls += nb
                adv_in += nb * exec_ctx[0]
        cc = u.get("cache_creation") or {}
        rows.append({
            "rid": rid, "ts": d["ts"], "model": norm_model(d["model"]), "ctx": max(exec_ctx) if exec_ctx else 0,
            "ctx_first": exec_ctx[0] if exec_ctx else 0,
            "out": out_tok, "think": think, "cost": dict(cost), "adv_calls": adv_calls, "adv_in": adv_in,
            "cw": (u.get("cache_creation_input_tokens", 0) or 0), "cr": (u.get("cache_read_input_tokens", 0) or 0),
            "cw1h": cc.get("ephemeral_1h_input_tokens", 0) or 0, "cw5m": cc.get("ephemeral_5m_input_tokens", 0) or 0,
            "tools": d["tools"], "effort": d["effort"], "advisorModel": d["advisorModel"], "final": final,
            "tok": dict(tok), "adv_tok": dict(adv_tok), "unpriced": sorted(unpriced),
        })
    rows.sort(key=lambda x: x["ts"] or "")
    # growth attribution: assign each tool_result to the request that produced its tool_use
    res_by_req = collections.defaultdict(list)
    for tstamp, tid, n in results:
        tu = tool_uses.get(tid)
        if tu:
            res_by_req[tu[2]].append((tu[0], tu[1], n))
    inj_sorted = sorted([x for x in injected if x[0]], key=lambda x: x[0])
    growth = collections.Counter()  # category -> tokens attributed
    growth_detail = collections.Counter()
    steps = []
    ii = 0
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        delta = b["ctx_first"] - a["ctx"]
        inj = []
        while ii < len(inj_sorted) and inj_sorted[ii][0] <= (b["ts"] or ""):
            if inj_sorted[ii][0] > (a["ts"] or ""):
                inj.append(inj_sorted[ii])
            ii += 1
        if delta < 0:
            growth["reset(compact/new)"] += 0
            steps.append({"i": i, "delta": delta, "reset": True})
            continue
        outpart = min(a["out"], delta)
        growth["assistant_output(incl thinking)"] += outpart
        rem = delta - outpart
        parts = [("tool:" + nm, lab, n) for nm, lab, n in res_by_req.get(a["rid"], [])] + [(k, k, n) for _, k, n in inj]
        tot = sum(p[2] for p in parts)
        if tot > 0:
            for cat, lab, n in parts:
                share = rem * n / tot
                growth[cat] += share
                growth_detail[lab] += share
        else:
            growth["unattributed"] += rem
        if keep_steps:
            steps.append({"i": i, "ts": a["ts"], "ctx": a["ctx"], "delta": delta, "out": a["out"], "think": a["think"],
                          "tools": a["tools"], "res_chars": sum(p[2] for p in parts)})
    # timing: sum inter-event gaps under 10 minutes as "active" wall time
    evs = sorted(event_ts)
    active = 0.0
    for x, y in zip(evs, evs[1:]):
        g = (ts(y) - ts(x)).total_seconds()
        if g < 600:
            active += g
    tot_cost = collections.Counter()
    for r in rows:
        for k, v in r["cost"].items():
            tot_cost[k] += v
    models = collections.Counter(r["model"] for r in rows)
    return {
        "path": path, "title": title, "entry": entry,
        "first": evs[0] if evs else None, "last": evs[-1] if evs else None,
        "active_s": active, "n_req": len(rows), "models": dict(models), "effort": dict(effort),
        "peak_ctx": max((r["ctx"] for r in rows), default=0),
        "final_ctx": rows[-1]["ctx"] if rows else 0,
        "median_ctx": sorted(r["ctx"] for r in rows)[len(rows) // 2] if rows else 0,
        "compacts": compacts, "cost": dict(tot_cost), "cost_total": sum(tot_cost.values()),
        "adv_calls": sum(r["adv_calls"] for r in rows), "adv_in": sum(r["adv_in"] for r in rows),
        "out_tok": sum(r["out"] for r in rows), "think_tok": sum(r["think"] for r in rows),
        "cr_tok": sum(r["cr"] for r in rows), "cw_tok": sum(r["cw"] for r in rows),
        "cw1h_tok": sum(r["cw1h"] for r in rows),
        "growth": {k: round(v) for k, v in growth.items()},
        "growth_top": [(k, round(v)) for k, v in growth_detail.most_common(25)],
        "ctx_series": [(r["ts"], r["ctx"]) for r in rows][:: max(1, len(rows) // 60)],
        "steps": steps if keep_steps else None,
        "req_rows": [{k: r[k] for k in ("rid", "ts", "model", "ctx", "out", "think", "cw", "cr", "cw1h", "adv_calls", "adv_in", "effort", "final", "tok", "adv_tok", "unpriced")} | {"c": round(sum(r["cost"].values()), 4), "cost": {k: round(v, 5) for k, v in r["cost"].items()}} for r in rows],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--since", default=None, help="only include transcripts modified on/after this date (YYYY-MM-DD); default: all")
    ap.add_argument("--out", default=None, help="output directory; default: a new dir under the system temp dir (never scripts/)")
    args = ap.parse_args(argv)

    roots = tr.project_dirs()
    if not roots:
        sys.stderr.write(f"no transcript dir matches {tr.project_globs()} under {tr.claude_root()}\n")
        return 1

    out_dir = args.out or tempfile.mkdtemp(prefix="session-cost-report-")
    os.makedirs(out_dir, exist_ok=True)

    out = []
    mains = [(root, mp) for root in roots for mp in glob.glob(os.path.join(root, "*.jsonl"))]
    for ROOT, mp in mains:
        sid = os.path.basename(mp)[:-6]
        if args.since and datetime.datetime.fromtimestamp(os.path.getmtime(mp)).strftime("%Y-%m-%d") < args.since:
            continue
        m = analyze_file(mp, keep_steps=True)
        m.update({"sid": sid, "kind": "main", "agentType": "main", "meta": None})
        out.append(m)
        for sp in glob.glob(os.path.join(ROOT, sid, "subagents", "*.jsonl")):
            meta = {}
            mf = sp[:-6] + ".meta.json"
            if os.path.exists(mf):
                try:
                    meta = json.load(open(mf, encoding="utf-8"))
                except Exception:
                    pass
            s = analyze_file(sp, keep_steps=True, is_sub=True)
            s.update({"sid": sid, "kind": "sub", "agentType": meta.get("agentType", "?"), "meta": meta})
            out.append(s)

    out_path = os.path.join(out_dir, "transcripts.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    print(f"transcripts: {len(out)}")
    print(f"wrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
