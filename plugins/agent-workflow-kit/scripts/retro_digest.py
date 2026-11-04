#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""One overview of every unmined session's NEW journal facts, clustered, for the `retro` skill.

Purpose: `retro` Step 2 has to find recurrence across every unmined session, and reading the
journals whole stops working past a few sessions. This prints, for the facts no `--mark` or launch
has covered yet:
  * the sessions and the new-fact window;
  * fact counts by kind;
  * tool_error clusters — (agent type, tool, normalised snippet) with count, session count and
    sample sessions;
  * command heads — the script + subcommand, or git/docker verb, with a leading `cd …` dropped;
  * script files written or edited outside the registered script homes (scratchpads, worktrees);
  * agents spawned, skills invoked, interrupts per session;
  * the owner's own messages (harness notices dropped).
Drill into a cluster with journal_grep.py (facts) or transcript_calls.py (full calls/results).

Usage:
    uv run --no-project <plugin-root>/scripts/retro_digest.py [--min-errors N] [--min-commands N]
        [--no-messages] [--session ID-PREFIX[,ID-PREFIX...]]

When to use: `retro` Step 2, before reading any journal; `harness-review` when it mines journals.

Inputs: .workflow/state/journal/<session>[.sub-<agent_id>].jsonl and its .markers/ via
        .workflow/hooks/session/_curation_state.py (unmined_sessions, new_facts); the agent type of
        a subagent journal is its `agent_started` fact, else the fact's `caller`.
Script homes: .workflow/scripts/ and the directory of `config: paths.scripts_index`.
Output: stdout only; writes nothing. Exit 0. Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks" / "session"))
import _transcripts as tr  # noqa: E402
import _curation_state as cs  # noqa: E402
import wfconfig  # noqa: E402

NOTICE_PREFIXES = ("<", "This session is being continued", "Caveat:", "[Cross-session")
SCRIPT_RE = re.compile(r"([\w./-]+\.(?:py|sh|ps1))")
CD_RE = re.compile(r"^\s*cd\s+(\"[^\"]*\"|'[^']*'|\S+)\s*(&&|;)\s*")
_journal_types: dict[str, str] = {}


def journal_agent_type(name: str) -> str:
    if name not in _journal_types:
        _journal_types[name] = tr.journal_agent_type(cs.journal_dir() / name)
    return _journal_types[name]


def fact_agent(fact: dict) -> str:
    at = journal_agent_type(str(fact.get("_journal", "")))
    return str(fact.get("caller") or at) if at == "sub" else at


def norm_error(snippet: str) -> str:
    s = re.sub(r"Exit code \d+\n?", "", snippet or "")
    s = re.sub(r"(?:[A-Za-z]:)?[\\/][^\s'\"]+[\\/][^\s'\"]+", "<PATH>", s)
    s = re.sub(r"'[^'\n]*('|$)", "'...'", s)  # a quoted path or token varies per call, not per cause
    s = re.sub(r"\d+", "N", s)
    return " ".join(s.split())[:110]


def command_head(command: str) -> str:
    c = CD_RE.sub("", command.strip())
    toks = c.split()
    if not toks:
        return ""
    m = SCRIPT_RE.search(c[:200])
    if m and c.index(m.group(1)) < 80:
        rest = c[m.end():].split()
        sub = " " + rest[0] if rest and re.match(r"^[a-z][\w-]*$", rest[0]) else ""
        return m.group(1) + sub
    if toks[0] == "git" and len(toks) > 3 and toks[1] == "-C":
        return "git -C <dir> " + toks[3]
    if toks[0] in ("git", "gh", "docker-compose", "npm", "uv") and len(toks) > 1:
        return " ".join(toks[:2])
    if toks[0] == "docker" and len(toks) > 2:
        return " ".join(toks[:3])
    return toks[0]


def script_homes() -> list[str]:
    root = wfconfig.repo_root()
    homes = [root / ".workflow" / "scripts",
             (root / str(wfconfig.get("paths.scripts_index", "scripts/INDEX.md"))).parent]
    return [str(h).lower().replace("\\", "/") for h in homes]


def collect(prefixes: list[str]) -> tuple[list, list, list]:
    """(sessions as (sid, facts, covered), their new meaningful facts, the owner's messages)."""
    sessions = [s for s in cs.unmined_sessions(1) if not prefixes or s[0].startswith(tuple(prefixes))]
    facts, messages = [], []
    for sid, _, covered in sessions:
        new = cs.new_facts(sid, covered)
        for fact in new:
            fact["_sid"] = sid
        facts.extend(new)
        if not new:
            continue
        t0 = str(new[0].get("ts", ""))
        for fact in tr.journal_facts(cs.journal_dir() / f"{cs.safe_name(sid)}.jsonl"):
            if fact.get("kind") == "user_message" and str(fact.get("ts", "")) >= t0:
                text = str(fact.get("text", "")).strip()
                if text and not text.startswith(NOTICE_PREFIXES):
                    messages.append((str(fact.get("ts", "")), sid, text))
    return sessions, facts, messages


def tally(rows) -> list:
    """[(key, count, sorted session ids)] most frequent first."""
    counts, sids = collections.Counter(), collections.defaultdict(set)
    for key, sid in rows:
        counts[key] += 1
        sids[key].add(sid)
    return [(k, n, sorted(sids[k])) for k, n in counts.most_common()]


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--min-errors", type=int, default=3, help="smallest tool_error cluster shown")
    ap.add_argument("--min-commands", type=int, default=5, help="smallest command-head count shown")
    ap.add_argument("--no-messages", action="store_true", help="omit the owner's messages")
    ap.add_argument("--session", default="", help="comma-separated session id prefixes")
    a = ap.parse_args(argv)
    prefixes = [p.strip() for p in a.session.split(",") if p.strip()]

    sessions, facts, messages = collect(prefixes)
    if not facts:
        print("no unmined facts")
        return 0
    ts = sorted(str(f.get("ts", "")) for f in facts)
    print(f"{len(sessions)} unmined session(s), {len(facts)} new fact(s), {ts[0][:16]} .. {ts[-1][:16]}")
    print("kinds:", dict(collections.Counter(f["kind"] for f in facts).most_common()))

    print(f"\n== tool_error clusters (>= {a.min_errors}): count sessions [agent] [tool] snippet")
    for (agent, tool, snip), n, sids in tally(((fact_agent(f), f.get("tool"), norm_error(f.get("snippet", ""))),
                                               f["_sid"]) for f in facts if f["kind"] == "tool_error"):
        if n >= a.min_errors:
            print(f"{n:4d} {len(sids):3d} [{agent}] [{tool}] {snip!r}  e.g. {' '.join(s[:8] for s in sids[:3])}")

    print(f"\n== command heads (>= {a.min_commands}): count sessions head")
    for head, n, sids in tally((command_head(str(f.get("command", ""))), f["_sid"])
                               for f in facts if f["kind"] == "command_run"):
        if n >= a.min_commands:
            print(f"{n:4d} {len(sids):3d} {head}")

    print("\n== script files written/edited outside the script homes: count sessions name e.g.")
    homes = script_homes()
    paths: dict[str, str] = {}
    rows = []
    for f in facts:
        if f["kind"] in ("script_written", "script_edited"):
            p = str(f.get("path", ""))
            norm = p.lower().replace("\\", "/")
            if any(norm.startswith(h) for h in homes):
                continue
            name = Path(p.replace("\\", "/")).name
            paths.setdefault(name, p)
            rows.append((name, f["_sid"]))
    for name, n, sids in tally(rows)[:40]:
        print(f"{n:4d} {len(sids):3d} {name}  {paths[name][:120]}")

    print("\n== agents spawned:", collections.Counter(f.get("agent_type") for f in facts
                                                     if f["kind"] == "agent_spawned").most_common())
    print("== skills invoked:", collections.Counter(f.get("skill") for f in facts
                                                    if f["kind"] == "skill_invoked").most_common())
    print("== interrupts per session:", collections.Counter(f["_sid"][:8] for f in facts
                                                            if f["kind"] == "interrupted").most_common())

    if not a.no_messages:
        print(f"\n== the owner's messages ({len(messages)})")
        for t, sid, text in sorted(messages):
            print(f"{t[:16]} {sid[:8]} {' '.join(text.split())[:200]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
