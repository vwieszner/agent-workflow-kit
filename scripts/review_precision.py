#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Code-review precision per story and per layer, from the rendered `*-code-review-findings*.md` files.

Reads every `## Review round N — <date>` section `review_findings_render.py` wrote (one holding
`### Findings for triage` and `### Raw layer reports`) and the triage lists that follow it, and reports,
per story (all of its files and rounds together) and per layer:

    deduped findings -> CONFIRMED / REFUTED / UNVERIFIABLE / no verdict
                     -> triage patch / defer / dismiss / untriaged

DEFINITIONS (printed with every report)
---------------------------------------
* Findings: each `#### <id> — <title>` block under `### Findings for triage`, and each
  `- <id> | <title> | `<location>` | <evidence>` row under `### Dismissed as false positive — REFUTED (N)`.
* Verdict: the token on the block's `- **Verification:**` line (UNVERIFIED = no verdict); REFUTED for the
  dismissed rows. Words in the evidence text are never read as a verdict.
* Layers and raw items: the raw ids `<layer>[#pass][@chunk].<n>` on the block's `- **Sources:**` line. A REFUTED
  row lists no sources; its layer is that of the one raw item, named by no Sources line, whose title and location
  the row repeats.
* Unnamed raw items: raw items in the round's raw layer reports that no Sources line names and no REFUTED row
  repeats. Each belongs to a REFUTED finding the file does not name: it counts as a REFUTED raw item of its
  layer and enters no deduped count. Shown as `(N unnamed REFUTED)` in the raw-items cell.
* Carried findings: a `carried:<id>` source names no raw item. A block or REFUTED row whose id's round differs
  from its section's round was carried in from a halted round: it counts toward the Summary checks only, so
  each id counts once per story, in the round that raised it.
* precision (deduped) = CONFIRMED / deduped findings with a verdict.
* precision (raw)     = raw items whose finding is CONFIRMED / raw items with a verdict.
* Triage: ids in the first cell of each table row, or at the start of each bullet, under an `approved_patches` /
  `deferred_findings` / `dismissed_findings` heading. A finding in several buckets is reported in the highest of
  patch > defer > dismiss and counted in `split`.
* The Summary line (`Raw findings X; after dedupe Y; for triage Z; refuted W`) and the REFUTED heading count are
  checked against what was parsed; any mismatch makes the file PARTIAL.

FILE STATUS: PARSED (every finding has a verdict, every count matches), PARTIAL (reasons listed; the story's rows
are labelled `(not fully parsed)`), NO-VERIFIER (findings, no verdict at all: precision undefined, not zero),
UNPARSED (no rendered round; listed with the reason, never guessed).

Usage
-----
    uv run --no-project .workflow/scripts/review_precision.py [<dir>] [--story <id> ...] [--json]

Default directory: `config: paths.specs_dir` of the main checkout (merged history). Read-only.
Exit 0 on a report; 1 when the directory is missing.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
from review_findings_render import _one_line as one_line  # noqa: E402

LAYERS = ("blind-hunter", "edge-case-hunter", "acceptance-auditor", "verification-gap", "ui-verifier",
          "pbt-writer", "test-adequacy")
ROUND_RX = re.compile(r"^## Review round (\d+) — ")
SUMMARY_RX = re.compile(r"^- Raw findings (\d+); after dedupe (\d+); for triage (\d+); refuted (\d+)\s*$")
FINDING_RX = re.compile(r"^#### (R\d+-\d+) — ")
SOURCES_RX = re.compile(r"^- \*\*Sources:\*\* (.*)$")
VERIFICATION_RX = re.compile(r"^- \*\*Verification:\*\* (CONFIRMED|REFUTED|UNVERIFIABLE|UNVERIFIED)\b")
REFUTED_HEAD_RX = re.compile(r"^### Dismissed as false positive — REFUTED \((\d+)\)\s*$")
REFUTED_ROW_RX = re.compile(r"^- (R\d+-\d+) \| ")
KIND_RX = re.compile(r"^- \*\*Kind / recommended:\*\* (.+?) / ")
IMPACT_RX = re.compile(r"^- bar impact: (.*)$")
TRIAGE_KEYS = (("approved_patches", "patch"), ("deferred_findings", "defer"), ("dismissed_findings", "dismiss"))
BUCKET_RANK = {"patch": 3, "defer": 2, "dismiss": 1}
ID_RX = re.compile(r"(?<![\w-])(R\d+-\d+)(?:[a-z])?(?!\d)")
CARRIED_SOURCE = "carried:"


def rendered_rounds(lines: list[str]) -> list[tuple[int, int]]:
    out = []
    for s, line in enumerate(lines):
        if ROUND_RX.match(line):
            e = next((j for j in range(s + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[j])), len(lines))
            body = lines[s + 1:e]
            if "### Findings for triage" in body and "### Raw layer reports" in body:
                out.append((s, e))
    return out


def rendered_layer(raw_id: str) -> str:
    """The layer of a raw id `<layer>[#pass][@chunk].<n>`."""
    return re.split(r"[#@]", raw_id.rsplit(".", 1)[0], maxsplit=1)[0]


def classify_impact(text) -> str:
    t = (text or "").strip()
    if not t or t == "None" or t.startswith("MISSING"):
        return "unknown"
    if t.startswith("BLOCKING"):
        return "blocking-minority" if t.startswith("BLOCKING (minority") else "blocking"
    if t.lower().startswith("n/a"):
        return "n/a"
    return "not-blocking"


def carried_from_halted_round(fid: str, rnd: str) -> bool:
    m = re.match(r"R(\d+)-", fid)
    return bool(m) and m.group(1) != rnd


def parse_rendered_round(lines: list[str], start: int, end: int) -> dict:
    """Findings, verdicts, unnamed raw items and count mismatches of one rendered round."""
    rnd = ROUND_RX.match(lines[start]).group(1)
    section = cur = label = buf = declared = refuted_head = None
    blocks: dict = {}
    refuted: list = []
    raw: dict = {}
    problems: list = []
    unreadable = False
    for ln in range(start + 1, end):
        line = lines[ln]
        if line.startswith("### "):
            section, cur = line[4:].strip(), None
            m = REFUTED_HEAD_RX.match(line)
            if m:
                section, refuted_head = "refuted", int(m.group(1))
            continue
        if section == "Summary":
            m = SUMMARY_RX.match(line)
            if m:
                declared = [int(x) for x in m.groups()]
        elif section == "Findings for triage":
            m = FINDING_RX.match(line)
            if m:
                cur = m.group(1)
                blocks[cur] = {"sources": None, "verdict": None, "line": ln + 1, "kind": None, "impact": None}
            elif cur and SOURCES_RX.match(line):
                blocks[cur]["sources"] = [x.strip() for x in SOURCES_RX.match(line).group(1).split(",") if x.strip()]
            elif cur and VERIFICATION_RX.match(line):
                blocks[cur]["verdict"] = VERIFICATION_RX.match(line).group(1)
            elif cur and KIND_RX.match(line):
                blocks[cur]["kind"] = KIND_RX.match(line).group(1)
            elif cur and IMPACT_RX.match(line):
                blocks[cur]["impact"] = IMPACT_RX.match(line).group(1)
        elif section == "refuted":
            m = REFUTED_ROW_RX.match(line)
            if m:
                refuted.append((m.group(1), line, ln + 1))
        elif section == "Raw layer reports":
            if line.startswith("#### "):
                label = line[5:].strip()
            elif line.strip() == "```json" and buf is None:
                buf = []
            elif line.strip() == "```" and buf is not None:
                try:
                    found = json.loads("\n".join(buf)).get("findings") or []
                except (ValueError, AttributeError) as e:
                    problems.append(f"round {rnd}: raw layer report {label} unreadable ({e})")
                    unreadable, found = True, []
                for i, f in enumerate(found):
                    raw[f"{label}.{i + 1}"] = f
                buf = None
            elif buf is not None:
                buf.append(line)

    if declared is None:
        problems.append(f"round {rnd}: no Summary counts line")
    else:
        n_raw, n_dedupe, n_triage, n_refuted = declared
        if len(raw) != n_raw:
            problems.append(f"round {rnd}: Summary says {n_raw} raw findings, the raw layer reports hold {len(raw)}")
        if len(blocks) != n_triage:
            problems.append(f"round {rnd}: Summary says {n_triage} findings for triage, {len(blocks)} parsed")
        if len(refuted) != n_refuted:
            problems.append(f"round {rnd}: Summary says {n_refuted} refuted, {len(refuted)} REFUTED rows parsed")
        if n_triage + n_refuted != n_dedupe:
            problems.append(f"round {rnd}: Summary says {n_dedupe} after dedupe, {n_triage} for triage + "
                            f"{n_refuted} refuted")
    if refuted_head is not None and refuted_head != len(refuted):
        problems.append(f"round {rnd}: REFUTED heading says {refuted_head}, {len(refuted)} rows parsed")
    raw_ok = not unreadable and declared is not None and len(raw) == declared[0]

    inv: dict = {}
    verdicts: dict = {}
    named: set = set()
    for fid, b in blocks.items():
        if b["sources"] is None:
            problems.append(f"round {rnd}: {fid} has no Sources line")
        srcs = [x for x in b["sources"] or [] if not x.startswith(CARRIED_SOURCE)]
        named.update(srcs)
        absent = [x for x in srcs if x not in raw]
        if raw_ok and absent:
            problems.append(f"round {rnd}: {fid} names raw ids absent from the raw layer reports ({', '.join(absent)})")
        if b["verdict"] is None:
            problems.append(f"round {rnd}: {fid} has no Verification line")
        if carried_from_halted_round(fid, rnd):
            continue
        inv[fid] = {"layers": dict(collections.Counter(rendered_layer(x) for x in srcs)), "kind": b["kind"],
                    "bar_impact": classify_impact(b["impact"])}
        if b["verdict"] not in (None, "UNVERIFIED"):
            verdicts[fid] = b["verdict"]
    first: set = set()
    carried_refuted = 0
    for fid, row, _ln in refuted:
        if carried_from_halted_round(fid, rnd):
            carried_refuted += 1
            continue
        hits = [rid for rid, r in raw.items() if rid not in named and row.startswith(
            f"- {fid} | {one_line(r.get('title', ''))} | `{one_line(r.get('location', ''))}` | ")]
        entry = {"layers": {}, "kind": None, "bar_impact": "n/a"}
        if len(hits) == 1:
            first.add(hits[0])
            entry.update(layers={rendered_layer(hits[0]): 1}, kind=raw[hits[0]].get("kind"))
        else:
            problems.append(f"round {rnd}: REFUTED row {fid} matches {len(hits)} raw items by title and location; "
                            "its layer is unknown")
            if raw_ok:
                entry["raw_n"] = 0
        inv[fid] = entry
        verdicts[fid] = "REFUTED"
    unnamed = [rendered_layer(rid) for rid in raw if rid not in named and rid not in first] if raw_ok else []
    return {"inv": inv, "verdicts": verdicts, "unnamed": unnamed, "problems": problems,
            "carried_refuted": carried_refuted}


def triage_of(lines: list[str]) -> dict[str, set[str]]:
    """Ids under each triage heading: first table cell or bullet head."""
    triage: dict[str, set[str]] = collections.defaultdict(set)
    mode, level, fence = None, 0, False
    for line in lines:
        if line.strip().startswith("```"):
            fence = not fence
            continue
        h = None if fence else re.match(r"^(#{1,6})\s+(.*)", line)
        low = line.lower()
        hit = next((b for k, b in TRIAGE_KEYS if k in low), None)
        if h:
            if hit:
                mode, level = hit, len(h.group(1))
            elif mode and len(h.group(1)) <= level:
                mode = None
            continue
        if not mode:
            continue
        if "|" in line and not re.match(r"^\s*[-*]\s+", line):
            cell = line.strip().strip("|").split("|")[0]
        else:
            cell = re.split(r"\s[—–:|]\s|\s—", re.sub(r"^\s*([-*]|\d+\.)\s+", "", line), maxsplit=1)[0]
        for m in ID_RX.finditer(cell):
            triage[m.group(1)].add(mode)
    return triage


def parse_file(path: str) -> dict:
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    inv: dict = {}
    verdicts: dict = {}
    problems: list = []
    unnamed: list = []
    carried_refuted = 0
    segments = rendered_rounds(lines)
    for s, e in segments:
        r = parse_rendered_round(lines, s, e)
        carried_refuted += r["carried_refuted"]
        inv.update(r["inv"])
        verdicts.update(r["verdicts"])
        problems += r["problems"]
        unnamed += r["unnamed"]
    auto = any(re.match(r"^- Diff: .*; mode auto;", line) for line in lines)
    return {"inv": inv, "verdicts": verdicts, "triage": {k: sorted(v) for k, v in triage_of(lines).items()},
            "auto": auto, "problems": problems, "unnamed": unnamed, "rounds": len(segments)}


def story_of(fname: str) -> str:
    return re.split(r"-code-review-findings", fname, maxsplit=1)[0]


def aggregate(findings: list[dict], unnamed: list[str] = ()) -> dict:
    def n_raw(f):
        return f["raw_n"] if f.get("raw_n") is not None else (sum(f["layers"].values()) or 1)

    def block(fs, layer=None):
        c = collections.Counter(f["verdict"] for f in fs)
        t = collections.Counter(f["triage"] for f in fs)
        tc = collections.Counter(f["triage"] for f in fs if f["verdict"] == "CONFIRMED")
        verified = [f for f in fs if f["verdict"] != "NONE"]
        conf = [f for f in verified if f["verdict"] == "CONFIRMED"]
        extra = sum(1 for u in unnamed if layer is None or u == layer)
        if layer:
            raw = sum(f["layers"].get(layer, 0) for f in fs) + extra
            raw_ver = sum(f["layers"].get(layer, 0) for f in verified) + extra
            raw_conf = sum(f["layers"].get(layer, 0) for f in conf)
        else:
            raw = sum(n_raw(f) for f in fs) + extra
            raw_ver = sum(n_raw(f) for f in verified) + extra
            raw_conf = sum(n_raw(f) for f in conf)
        return {
            "deduped": len(fs), "raw_items": raw, "raw_unattributed": extra, "verdicts": dict(c), "triage": dict(t),
            "triage_of_confirmed": dict(tc), "split_triage": sum(1 for f in fs if f["split"]),
            "precision_deduped": round(len(conf) / len(verified), 3) if verified else None,
            "precision_raw": round(raw_conf / raw_ver, 3) if raw_ver else None,
            "bar_impact": dict(collections.Counter(f["bar_impact"] for f in fs)),
            "kind": dict(collections.Counter(f["kind"] for f in fs)),
        }
    res = {"all": block(findings), "layers": {}}
    seen = {layer for f in findings for layer in f["layers"]} | set(unnamed)
    for layer in list(LAYERS) + sorted(seen - set(LAYERS)):
        fs = [f for f in findings if f["layers"].get(layer)]
        if fs or layer in unnamed:
            res["layers"][layer] = block(fs, layer)
    return res


def summarize(files: list[str]) -> dict:
    per_story: dict = collections.OrderedDict()
    statuses = []
    for path in files:
        fname = os.path.basename(path)
        try:
            p = parse_file(path)
        except Exception as e:  # report, never guess
            statuses.append({"file": fname, "status": "UNPARSED", "reason": f"read/parse error: {e}"})
            continue
        inv, ver, tri = p["inv"], p["verdicts"], p["triage"]
        if not p["rounds"]:
            statuses.append({"file": fname, "status": "UNPARSED", "reason": "no rendered review round"})
            continue
        missing = sorted(i for i in inv if i not in ver)
        if inv and not ver:
            status, reason = "NO-VERIFIER", "; ".join(["findings found, no verdicts"] + p["problems"])
        else:
            problems = list(p["problems"])
            if missing:
                problems.append(f"{len(missing)} findings without verdict ({','.join(missing[:10])})")
            status, reason = ("PARTIAL", "; ".join(problems)) if problems else ("PARSED", "")
        if p["unnamed"]:
            by_layer = ", ".join(f"{k} {v}" for k, v in sorted(collections.Counter(p["unnamed"]).items()))
            reason = "; ".join(x for x in (reason, f"note: {len(p['unnamed'])} raw item(s) ({by_layer}) belong to "
                                                   "REFUTED findings the file does not name") if x)
        statuses.append({"file": fname, "status": status, "reason": reason, "findings": len(inv),
                         "verdicts": len(ver), "triaged": len(tri), "auto_mode": p["auto"]})
        st = per_story.setdefault(story_of(fname), {"files": [], "findings": [], "unnamed": []})
        st["files"].append(f"{fname} [{status}]")
        st["unnamed"] += p["unnamed"]
        for fid, f in inv.items():
            tb = tri.get(fid, [])
            st["findings"].append({"file": fname, "id": fid, "layers": f["layers"], "raw_n": f.get("raw_n"),
                                   "verdict": ver.get(fid, "NONE"),
                                   "triage": max(tb, key=lambda x: BUCKET_RANK[x]) if tb else "untriaged",
                                   "split": len(tb) > 1, "bar_impact": f.get("bar_impact", "unknown"),
                                   "kind": f.get("kind") or "unknown"})
    return {"stories": {k: {"files": v["files"], **aggregate(v["findings"], v["unnamed"])}
                        for k, v in per_story.items()},
            "files": statuses}


def pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def render(rep: dict, only: list[str] | None) -> str:
    L = ["# Review precision", "",
         "Definitions: precision (deduped) = CONFIRMED / deduped findings with a verdict; precision (raw) = raw items "
         "whose finding is CONFIRMED / raw items with a verdict (raw items = the raw ids on each finding's Sources "
         "line). Raw items no finding names belong to REFUTED findings the file does not name (`N unnamed REFUTED`).",
         ""]
    autos = [f["file"] for f in rep["files"] if f.get("auto_mode")]
    L += [f"Auto-mode files: {', '.join(autos) if autos else 'none'}", ""]
    L += ["## Per story", "",
          "| story | layer | deduped | raw items | CONFIRMED | REFUTED | UNVERIFIABLE | no verdict | "
          "precision (deduped) | precision (raw) | patch | defer | dismiss | untriaged | confirmed->patch |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for story, s in rep["stories"].items():
        if only and story not in only:
            continue
        label = story if all(f.endswith("[PARSED]") for f in s["files"]) else f"{story} (not fully parsed)"
        for layer, b in [("all", s["all"])] + list(s["layers"].items()):
            v, t = b["verdicts"], b["triage"]
            raw = f"{b['raw_items']} ({b['raw_unattributed']} unnamed REFUTED)" if b["raw_unattributed"] \
                else b["raw_items"]
            L.append(f"| {label} | {layer} | {b['deduped']} | {raw} | {v.get('CONFIRMED', 0)} | {v.get('REFUTED', 0)} | "
                     f"{v.get('UNVERIFIABLE', 0)} | {v.get('NONE', 0)} | {pct(b['precision_deduped'])} | "
                     f"{pct(b['precision_raw'])} | {t.get('patch', 0)} | {t.get('defer', 0)} | {t.get('dismiss', 0)} | "
                     f"{t.get('untriaged', 0)} | {b['triage_of_confirmed'].get('patch', 0)} |")
    L += ["", "## File status", "", "| file | status | findings | verdicts | triaged | reason |",
          "|---|---|---|---|---|---|"]
    for f in rep["files"]:
        if only and story_of(f["file"]) not in only:
            continue
        L.append(f"| {f['file']} | {f['status']} | {f.get('findings', '-')} | {f.get('verdicts', '-')} | "
                 f"{f.get('triaged', '-')} | {f.get('reason', '')} |")
    c = collections.Counter(f["status"] for f in rep["files"] if not only or story_of(f["file"]) in only)
    L += ["", f"Files: {dict(c)}", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("directory", nargs="?", default=None)
    ap.add_argument("--story", action="append", help="limit the report to these stories (file-name prefix)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    directory = a.directory or str(wfconfig.path("paths.specs_dir", "docs/implementation-artifacts"))
    if not os.path.isdir(directory):
        sys.stderr.write(f"directory not found: {directory}\n")
        return 1
    rep = summarize(sorted(glob.glob(os.path.join(directory, "*-code-review-findings*.md"))))
    if a.json:
        if a.story:
            rep = {"stories": {k: v for k, v in rep["stories"].items() if k in a.story},
                   "files": [f for f in rep["files"] if story_of(f["file"]) in a.story]}
        print(json.dumps(rep, indent=1))
    else:
        print(render(rep, a.story))
    return 0


if __name__ == "__main__":
    sys.exit(main())
