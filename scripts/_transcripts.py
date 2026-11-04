#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Shared helpers for the harness readers: where Claude Code keeps this project's transcripts,
journal-file iteration, and the agent role of an agent type.

Claude Code stores a session transcript at `~/.claude/projects/<slug>/<session>.jsonl`, its
subagents at `<slug>/<session>/subagents[/workflows/<runId>]/agent-<id>.jsonl` with a sibling
`.meta.json` (`agentType`, `description`, ...). `<slug>` is the session's start directory with
every character outside `[A-Za-z0-9]` replaced by `-`.

Project dirs searched (`project_dirs()`):
    `config: transcripts.claude_project_globs` when set (globs under `config: transcripts.claude_root`,
    default `~/.claude/projects`), else `<slug of the repo root>*` plus `<slug of the worktrees
    root>*` (story slots run with their worktree as cwd).

Agent role (`base_role()`): an effort variant counts as its base agent —
`<role><suffix>` → `<role>` for each suffix in `config: agents.effort_variant_suffixes`
(default ["-xhigh", "-max"]) when `<role>` is in `config: agents.escalated_roles`
(default ["story-impl"]). Uses `.workflow/scripts/agent_roles.py` when that module exists.

Imported by retro_digest, journal_grep, transcript_calls, denial_samples, session_cost_report and
harness_metrics. Not run on its own. Stdlib only.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

try:  # the kit's agent-role module, when installed
    from agent_roles import base_role as _kit_base_role  # type: ignore  # noqa: E402
except Exception:  # noqa: BLE001
    _kit_base_role = None


def base_role(agent_type: str | None) -> str | None:
    if _kit_base_role is not None:
        return _kit_base_role(agent_type)
    if not agent_type:
        return agent_type
    roles = wfconfig.get("agents.escalated_roles", ["story-impl"]) or []
    for suffix in wfconfig.get("agents.effort_variant_suffixes", ["-xhigh", "-max"]) or []:
        if agent_type.endswith(suffix) and agent_type[: -len(suffix)] in roles:
            return agent_type[: -len(suffix)]
    return agent_type


def slug(path: Path | str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_root() -> str:
    return os.path.expanduser(str(wfconfig.get("transcripts.claude_root", "~/.claude/projects")))


def project_globs() -> list[str]:
    configured = wfconfig.get("transcripts.claude_project_globs", []) or []
    if configured:
        return [str(g) for g in configured]
    return [slug(wfconfig.repo_root()) + "*", slug(wfconfig.worktrees_root()) + "*"]


def project_dirs() -> list[str]:
    root = claude_root()
    found: list[str] = []
    for pattern in project_globs():
        for d in glob.glob(os.path.join(root, pattern)):
            if os.path.isdir(d) and d not in found:
                found.append(d)
    return sorted(found)


def subagent_meta(transcript: str) -> dict:
    try:
        with open(transcript[: -len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def subagent_type(session_id: str, agent_id: str) -> str:
    """agentType from the subagent's `.meta.json` in any project dir, else "?"."""
    for proj in project_dirs():
        pattern = os.path.join(proj, session_id, "subagents", "**", f"agent-{agent_id}.meta.json")
        for mf in glob.glob(pattern, recursive=True):
            try:
                with open(mf, encoding="utf-8") as fh:
                    return str(json.load(fh).get("agentType") or "?")
            except (OSError, ValueError):
                continue
    return "?"


def journal_dir() -> Path:
    return wfconfig.workflow_dir() / "state" / "journal"


def journal_facts(path: Path | str):
    """Yield every fact of a kit journal file (entries carry a `facts` list), in file order."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                facts = obj.get("facts")
                if isinstance(facts, list):
                    for fact in facts:
                        if isinstance(fact, dict):
                            yield fact
                elif "kind" in obj:  # a bare-fact line
                    yield obj
    except OSError:
        return


def journal_agent_type(path: Path | str) -> str:
    """`main` for a session journal; a subagent journal's `agent_started` agent_type, else the
    Claude `.meta.json` agentType, else `sub`."""
    name = os.path.basename(str(path))
    if ".sub-" not in name:
        return "main"
    for fact in journal_facts(path):
        if fact.get("kind") == "agent_started":
            return str(fact.get("agent_type") or "sub")
    sid, _, aid = name[: -len(".jsonl")].partition(".sub-")
    found = subagent_type(sid, aid)
    return found if found != "?" else "sub"
