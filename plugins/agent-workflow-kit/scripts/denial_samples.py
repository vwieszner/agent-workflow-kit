#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Print sample denied tool calls for given agent types, with the denial text and the next call.

Purpose: drill-down (Claude Code transcripts) after harness_metrics.py flags an agent type's denial rate. For each
non-interrupted denial (`toolDenialKind`) in the subagent transcripts it prints the denied
call, the denial text, and the agent's next call (did it adapt, retry, or escape?), then a
ranked table of distinct denial texts.

Usage:
    uv run --no-project <plugin-root>/scripts/denial_samples.py <type[,type...]> [--since YYYY-MM-DD] [--samples N]

    Effort variants map to their base role (see _transcripts.base_role).

Inputs: <project dirs>/*/subagents/agent-*.jsonl and .../subagents/workflows/*/agent-*.jsonl
        (project dirs per _transcripts.project_dirs()), role from the sibling .meta.json agentType.
Output: stdout only; writes nothing. Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _transcripts as tr  # noqa: E402

base_role = tr.base_role


def transcripts() -> list[str]:
    files: list[str] = []
    for proj in tr.project_dirs():
        files += glob.glob(os.path.join(proj, "*", "subagents", "agent-*.jsonl"))
        files += glob.glob(os.path.join(proj, "*", "subagents", "workflows", "*", "agent-*.jsonl"))
    return sorted(files, key=os.path.getmtime)


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(x.get("text", "") for x in content or [] if isinstance(x, dict))


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("types", help="comma-separated agent types")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD; transcripts modified on/after")
    ap.add_argument("--samples", type=int, default=12)
    args = ap.parse_args(argv)

    wanted = {base_role(t.strip()) for t in args.types.split(",") if t.strip()}
    since = time.mktime(time.strptime(args.since, "%Y-%m-%d")) if args.since else 0.0
    reasons: collections.Counter = collections.Counter()
    shown = 0

    for f in transcripts():
        if os.path.getmtime(f) < since:
            continue
        try:
            with open(f[:-6] + ".meta.json", encoding="utf-8") as mh:
                atype = json.load(mh).get("agentType")
        except (OSError, ValueError):
            continue
        if not atype or base_role(atype) not in wanted:
            continue
        calls, order, denied = {}, [], []
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                kind = o.get("toolDenialKind")
                for b in (o.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        calls[b["id"]] = (b["name"], json.dumps(b.get("input"))[:230])
                        order.append(b["id"])
                    elif b.get("type") == "tool_result" and kind and kind != "interrupted":
                        text = result_text(b.get("content")).replace("\n", " ")
                        reasons[text[:150]] += 1
                        denied.append((b.get("tool_use_id"), kind, text))
        # The next call follows the denial later in the file, so resolve it after the full read.
        for tid, kind, text in denied:
            if shown >= args.samples:
                break
            shown += 1
            i = order.index(tid) if tid in order else -1
            nxt = calls.get(order[i + 1]) if 0 <= i < len(order) - 1 else None
            print(f"--- {atype} [{kind}] {os.path.basename(f)[:20]}")
            print("  CALL:", calls.get(tid))
            print("  DENY:", text[:700])
            print("  NEXT:", nxt)

    print("\n=== distinct deny texts")
    for k, n in reasons.most_common(25):
        print(n, k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
