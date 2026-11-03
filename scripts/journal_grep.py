#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Search the session activity journals for facts matching a regex, across every session.

Purpose: cross-session drill-down over `.workflow/state/journal/*.jsonl` (the session journal
hook's output). For each matching `tool_error` it prints the timestamp, session, agent type and
the error snippet, plus the `command_run` that preceded it in the same journal file (what was the
agent doing when it failed?). For each matching `command_run` it prints the command.

Usage:
    uv run --no-project .workflow/scripts/journal_grep.py error   <regex> [--since YYYY-MM-DD] [--agent-type T[,T...]] [--journal-dir DIR]
    uv run --no-project .workflow/scripts/journal_grep.py command <regex> [--since YYYY-MM-DD] [--agent-type T[,T...]] [--journal-dir DIR]

    `error` matches the `tool_error` snippet; `command` matches the `command_run` command.
    `--agent-type main` selects main-session journals; other values match the subagent journal's
    agent type (its `agent_started` fact, else the Claude `.meta.json`); effort variants map to
    the base role on both sides. `--journal-dir` defaults to `.workflow/state/journal`.

When to use: `retro` or `harness-review` needs to know which agent types hit an error, or ran a
command, and in which sessions — instead of reading whole journal files.

Output: stdout only; writes nothing. Exit 0. Stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _transcripts as tr  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("kind", choices=("error", "command"))
    ap.add_argument("regex")
    ap.add_argument("--since", default="", help="ISO date; facts with an earlier ts are skipped")
    ap.add_argument("--agent-type", default="", help="comma-separated; 'main' = main session")
    ap.add_argument("--journal-dir", default=str(tr.journal_dir()))
    a = ap.parse_args(argv)
    rx = re.compile(a.regex)
    wanted = {tr.base_role(t.strip()) for t in a.agent_type.split(",") if t.strip()}

    for fp in sorted(glob.glob(os.path.join(a.journal_dir, "*.jsonl"))):
        sid = os.path.basename(fp)[: -len(".jsonl")].partition(".sub-")[0]
        at = tr.base_role(tr.journal_agent_type(fp))
        if wanted and at not in wanted:
            continue
        prev = None
        for o in tr.journal_facts(fp):
            if str(o.get("ts") or "") < a.since:
                continue
            if o.get("kind") == "command_run":
                cmd = str(o.get("command", ""))
                if a.kind == "command" and rx.search(cmd):
                    print(f"{str(o.get('ts'))[:16]} {sid[:8]} {at:18s} CMD {cmd[:300]!r}")
                prev = o
            elif o.get("kind") == "tool_error" and a.kind == "error":
                snip = str(o.get("snippet", ""))
                if rx.search(snip):
                    pc = str(prev.get("command", ""))[:260] if prev else "-"
                    print(f"{str(o.get('ts'))[:16]} {sid[:8]} {at:18s} ERR {snip[:160]!r}\n"
                          f"      prev cmd: {pc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
