#!/usr/bin/env python3
"""Shared state for the script-artifact-curation loop.

Imported by session_journal.py (writer of journal lines), session_retrospective.py (marker
writer) and check_pending_proposals.py (reader). The journal-line schema, the marker schema and
the proposal naming below are the contract these hooks agree on, which is why they live here.

Layout (all under .workflow/state/, git-ignored):
    journal/<session>.jsonl                one JSON object per turn (DESIGN §8):
                                           {"ts", "session_id", "tool_calls": [{"tool","input","ok"}],
                                            "facts": [{"kind", ...}], "source", ["agent_id"]}
    journal/<session>.sub-<agent>.jsonl    same, for a subagent
    journal/cursors/<hash>.cur             byte cursor per transcript (Claude Code only)
    journal/.markers/<session>.json        {"analyzed_facts": A, "launched_facts": L}
    journal/.last_retro                    auto-retrospective cooldown timestamp
    proposals/<kind>-<slug>.md             pending proposal (frontmatter + rationale)
    proposals/<kind>-<slug>.draft.<ext>    its draft artifact
    proposals/archive/                     resolved proposals
    curation-learnings.md                  rejection reasons (never re-propose)

A session is "unmined" when its meaningful-fact count exceeds
max(analyzed_facts, launched_facts) by at least the caller's threshold.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import wfconfig  # noqa: E402

# Fact kinds counted toward the analysis threshold / nudge (user_message is context only).
MEANINGFUL = {
    "script_written", "script_edited", "command_run",
    "tool_error", "interrupted", "skill_invoked", "agent_spawned",
}
DEFAULT_KINDS = ("skill", "agent", "rule", "script", "memory")


def _state(*parts: str) -> Path:
    """.workflow/state/<parts> WITHOUT creating it (readers must not create dirs)."""
    return wfconfig.workflow_dir().joinpath("state", *parts)


def journal_dir() -> Path:
    return _state("journal")


def markers_dir() -> Path:
    return journal_dir() / ".markers"


def cursors_dir() -> Path:
    return journal_dir() / "cursors"


def proposals_dir() -> Path:
    return _state("proposals")


def archive_dir() -> Path:
    return proposals_dir() / "archive"


def learnings_path() -> Path:
    return _state("curation-learnings.md")


def proposal_kinds() -> tuple:
    kinds = wfconfig.get("curation.proposal_kinds", list(DEFAULT_KINDS)) or list(DEFAULT_KINDS)
    return tuple(str(k).lower() for k in kinds)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value)) or "unknown"


# --------------------------------------------------------------------------- journal

def session_journals(session_id: str) -> list:
    jdir = journal_dir()
    if not jdir.is_dir():
        return []
    sid = safe_name(session_id)
    return sorted(p for p in jdir.glob(f"{sid}*.jsonl")
                  if p.name == f"{sid}.jsonl" or p.name.startswith(f"{sid}.sub-"))


def _line_facts(obj: dict) -> list:
    facts = obj.get("facts")
    return facts if isinstance(facts, list) else []


def session_fact_count(session_id: str) -> int:
    """Count meaningful facts across every journal file of a session."""
    total = 0
    for jf in session_journals(session_id):
        try:
            with open(jf, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    total += sum(1 for f in _line_facts(obj)
                                 if isinstance(f, dict) and f.get("kind") in MEANINGFUL)
        except OSError:
            continue
    return total


def all_session_ids() -> list:
    """Session ids with at least one journal file (strip `.sub-<agent>` and `.jsonl`)."""
    jdir = journal_dir()
    if not jdir.is_dir():
        return []
    ids = set()
    for jf in jdir.glob("*.jsonl"):
        name = jf.name[: -len(".jsonl")]
        if ".sub-" in name:
            name = name.split(".sub-", 1)[0]
        ids.add(name)
    return sorted(ids)


def read_marker(session_id: str) -> dict:
    p = markers_dir() / f"{safe_name(session_id)}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {"analyzed_facts": int(data.get("analyzed_facts", 0)),
                    "launched_facts": int(data.get("launched_facts", 0))}
    except (OSError, ValueError, TypeError):
        pass
    return {"analyzed_facts": 0, "launched_facts": 0}


def write_marker(session_id: str, *, analyzed_facts=None, launched_facts=None) -> None:
    md = markers_dir()
    md.mkdir(parents=True, exist_ok=True)
    cur = read_marker(session_id)
    if analyzed_facts is not None:
        cur["analyzed_facts"] = int(analyzed_facts)
    if launched_facts is not None:
        cur["launched_facts"] = int(launched_facts)
    (md / f"{safe_name(session_id)}.json").write_text(json.dumps(cur), encoding="utf-8")


def unmined_sessions(threshold: int) -> list:
    """(session_id, facts, covered) for sessions whose new activity >= threshold."""
    out = []
    for sid in all_session_ids():
        facts = session_fact_count(sid)
        m = read_marker(sid)
        covered = max(m["analyzed_facts"], m["launched_facts"])
        if facts - covered >= threshold:
            out.append((sid, facts, covered))
    return out


def prune_journals(retention_days: int) -> int:
    """Delete journals, markers and cursors untouched for > retention_days. 0/neg disables."""
    if retention_days <= 0:
        return 0
    jdir = journal_dir()
    if not jdir.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    candidates = list(jdir.glob("*.jsonl"))
    for sub in (markers_dir(), cursors_dir()):
        if sub.is_dir():
            candidates.extend(p for p in sub.iterdir() if p.is_file())
    for p in candidates:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------- proposals

def is_proposal_file(p: Path) -> bool:
    """A pending proposal is `<kind>-<slug>.md` directly in proposals/ (not a draft, not README)."""
    if not p.is_file() or p.suffix != ".md" or ".draft." in p.name or p.name == "README.md":
        return False
    kind = p.name.split("-", 1)[0].lower()
    return kind in proposal_kinds() and "-" in p.name


def pending_proposals() -> list:
    pdir = proposals_dir()
    if not pdir.is_dir():
        return []
    return sorted(p for p in pdir.iterdir() if is_proposal_file(p))


def proposal_drafts(proposal: Path) -> list:
    """Draft files belonging to a proposal: `<stem>.draft.*` siblings."""
    return sorted(proposal.parent.glob(f"{proposal.stem}.draft.*"))
