#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Pre-compaction hook + CLI: mine the session journal into curation PROPOSALS.

Wiring
    Claude Code: `PreCompact` hook (matchers `auto` and `manual`).
    OpenCode:    the workflow-hooks plugin calls it on `experimental.session.compacting`.

Hook mode (JSON payload on stdin with `session_id`): when the session's journal holds at least
`config: curation.retro_threshold` NEW meaningful facts and the cooldown
(`config: curation.retro_cooldown_s`) has elapsed, launch a DETACHED, best-effort headless
analyst that reads the journal and writes proposals under .workflow/state/proposals/. It never
blocks compaction and always exits 0. If the launch is disabled or fails, the session stays
"unmined" and the session-start nudge (check_pending_proposals.py) points the owner at the
`retro` skill — the in-session, observed path, which calls `--mark` when done.

Analyst selection (`config: curation.analyst`):
    auto      Claude-shaped payload (has `transcript_path`) → `claude -p <prompt>`,
              otherwise → `opencode run <prompt>`
    claude    always `claude -p <prompt>`
    opencode  always `opencode run <prompt>`
    off       never launch (equivalent to env WORKFLOW_RETRO_AUTOSPAWN=0)

CLI
    session_retrospective.py --list                     # unmined sessions + journal paths
    session_retrospective.py --mark --session <id>      # record a session as analyzed
    session_retrospective.py --dry-run --session <id>   # go/no-go decision + the prompt
    session_retrospective.py --prompt --session <id>    # print only the analysis prompt

HARD guardrail: the analyst writes ONLY into .workflow/state/proposals/. Nothing reaches a live
skill / agent / rule / script / memory location without explicit approval in the `curate` skill.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _curation_state as cs  # noqa: E402

wfconfig = cs.wfconfig

SKILL_DIRS = (".opencode/skills", ".claude/skills")
AGENT_DIRS = (".opencode/agents", ".claude/agents")


def _threshold() -> int:
    return int(wfconfig.get("curation.retro_threshold", 8))


def _cooldown_s() -> int:
    return int(wfconfig.get("curation.retro_cooldown_s", 600))


def _last_retro_file() -> Path:
    return cs.journal_dir() / ".last_retro"


def _within_cooldown() -> bool:
    try:
        last = float(_last_retro_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (time.time() - last) < _cooldown_s()


def _touch_cooldown() -> None:
    try:
        cs.journal_dir().mkdir(parents=True, exist_ok=True)
        _last_retro_file().write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(wfconfig.repo_root()))
    except ValueError:
        return str(p)


def build_prompt(session_id: str, run_id: str) -> str:
    journals = "\n".join(f"  - {_rel(p)}" for p in cs.session_journals(session_id)) or "  (none)"
    pdir = _rel(cs.proposals_dir())
    learnings = _rel(cs.learnings_path())
    scripts_index = str(wfconfig.get("paths.scripts_index", "scripts/INDEX.md"))
    agent_src = str(wfconfig.get("curation.agent_source_dir", ".workflow/agents"))
    root = wfconfig.repo_root()
    skill_dirs = ", ".join(d for d in SKILL_DIRS if (root / d).is_dir()) or "(none installed)"
    agent_dirs = ", ".join([agent_src] + [d for d in AGENT_DIRS if (root / d).is_dir()])
    kinds = "|".join(cs.proposal_kinds())
    return f"""You are the session-retrospective analyst for this repository. Mine a session activity
journal for REUSABLE tooling opportunities and write PROPOSALS ONLY. Be conservative.

JOURNAL FILES (JSON lines, one per turn; mine each line's `facts` array — kinds: command_run,
script_written, script_edited, tool_error, interrupted, skill_invoked, agent_spawned, user_message):
{journals}

REFERENCE BEFORE PROPOSING (avoid duplicates and previously rejected ideas):
  - {scripts_index} (project scripts) and .workflow/scripts/INDEX.md (workflow kit scripts)
  - skills: {skill_dirs}
  - agents: {agent_dirs}
  - rules: .workflow/rules/
  - {learnings} (rejected candidates — may not exist; never re-propose anything listed there)

TASK:
1. Look across the session for:
   - command_run sequences repeated several times (an ad-hoc task that recurs)
   - script_written / script_edited that look reusable but are not registered in {scripts_index}
   - tool_error / interrupted clusters that signal a recurring mistake
   - skill_invoked / agent_spawned patterns suggesting a missing skill or agent
2. Classify each GENUINE candidate as exactly one kind: {kinds}.
   Require real recurrence (>=2-3x) or clear reuse. SKIP single occurrences, anything already
   covered by an existing artifact, and anything in the learnings file. Prefer 0 proposals over
   speculative ones. If nothing qualifies, print exactly NO_PROPOSALS and write nothing.
3. For each candidate write, directly inside {pdir}/:
   - <kind>-<short-kebab-slug>.md          the proposal (template below); if the name exists,
                                            append -2, -3, ...
   - <kind>-<short-kebab-slug>.draft.<ext> the draft: .draft.py|.draft.sh|.draft.ps1 (script),
     .draft.SKILL.md (skill), .draft.agent.md (agent), .draft.rule.md (rule), .draft.memory.md

PROPOSAL TEMPLATE (YAML frontmatter + body):
---
kind: <{kinds}>
title: <short title>
run_id: {run_id}
target: <where it lands on approval — "<scripts dir>/<name>.py + {scripts_index} row"
  | "skill <name> (installed skill dirs)" | "agent <name> ({agent_src}/<name>.md, rendered)"
  | ".workflow/rules/<name>.md + .workflow/rules/INDEX.md row" | "memory <name>">
recurrence: <count / evidence summary>
---
## What
<what the artifact does, one paragraph>
## Why (evidence)
<cite the specific journal facts: commands, errors, timestamps>
## Draft
<the draft file name>
## Risks / notes
<agent: which tool capabilities it needs; over-engineering check; anything the approver should weigh>

DRAFT FORMATS:
  - skill: SKILL.md with YAML frontmatter `name` + `description`; tool-neutral prose.
  - agent: kit agent source format — frontmatter `name`, `description`, `mode`, `tier`
    (deep|standard|fast), `effort`, `tools` (capability list: read, grep, glob, edit, write, bash,
    task, skill, web, graph, monitor), `readonly`; tool-neutral body. Never a concrete model name.
  - rule: same frontmatter as the existing files in .workflow/rules/; operative content only.
  - script: doc header per .workflow/rules/script-artifact-curation.md.

HARD CONSTRAINTS:
- Write ONLY inside {pdir}. Do NOT modify skill dirs, agent dirs, .workflow/rules, any scripts
  dir, AGENTS.md / CLAUDE.md, or any memory file. Reading them is fine.
- Be specific and conservative. Quality over quantity.
"""


def _analyst_cmd(payload: dict, prompt: str):
    choice = str(wfconfig.get("curation.analyst", "auto")).lower()
    if choice == "off":
        return None
    if choice == "auto":
        choice = "claude" if payload.get("transcript_path") else "opencode"
    if choice == "claude":
        exe = shutil.which("claude")
        return [exe, "-p", prompt] if exe else None
    if choice == "opencode":
        exe = shutil.which("opencode")
        return [exe, "run", prompt] if exe else None
    return None


def _launch(payload: dict, session_id: str, facts: int) -> None:
    cmd = _analyst_cmd(payload, build_prompt(session_id, _run_id()))
    if not cmd:
        return  # leave unmined → nudge points at the retro skill
    cs.proposals_dir().mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WORKFLOW_RETRO_RUNNING": "1"}
    kwargs: dict = dict(cwd=str(wfconfig.repo_root()), env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)  # fire-and-forget
    cs.write_marker(session_id, launched_facts=facts)  # only reached if Popen did not raise
    _touch_cooldown()


def _autospawn_enabled() -> bool:
    if str(wfconfig.get("curation.analyst", "auto")).lower() == "off":
        return False
    return os.environ.get("WORKFLOW_RETRO_AUTOSPAWN", "1").lower() not in ("0", "false", "no", "off")


def _hook_mode() -> int:
    if os.environ.get("WORKFLOW_RETRO_RUNNING"):
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().lstrip(b"\xef\xbb\xbf").decode("utf-8"))
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id")
    if not session_id:
        return 0
    try:
        facts = cs.session_fact_count(session_id)
        marker = cs.read_marker(session_id)
        covered = max(marker["analyzed_facts"], marker["launched_facts"])
        if (facts - covered) < _threshold() or not _autospawn_enabled() or _within_cooldown():
            return 0
        _launch(payload, session_id, facts)
    except Exception:  # noqa: BLE001 — markers untouched → nudge fires → owner runs retro
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__.splitlines()[0])
    ap.add_argument("--mark", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prompt", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--session", default=None)
    args, _ = ap.parse_known_args()

    if args.list:
        unmined = cs.unmined_sessions(1)
        if not unmined:
            print("no unmined sessions")
            return 0
        for sid, facts, covered in unmined:
            paths = ", ".join(_rel(p) for p in cs.session_journals(sid))
            print(f"{sid}\tnew={facts - covered}\tfacts={facts}\tcovered={covered}\t{paths}")
        return 0

    if args.mark:
        if not args.session:
            print("ERROR: --mark requires --session <id>", file=sys.stderr)
            return 2
        facts = cs.session_fact_count(args.session)
        cs.write_marker(args.session, analyzed_facts=facts)
        print(f"marked session {args.session} analyzed at {facts} facts")
        return 0

    if args.prompt or args.dry_run:
        sid = args.session
        if not sid:
            print("ERROR: --prompt/--dry-run require --session <id>", file=sys.stderr)
            return 2
        if args.dry_run:
            facts = cs.session_fact_count(sid)
            marker = cs.read_marker(sid)
            covered = max(marker["analyzed_facts"], marker["launched_facts"])
            cooling = _within_cooldown()
            go = (facts - covered) >= _threshold() and not cooling and _autospawn_enabled()
            print(f"session={sid} facts={facts} covered={covered} new={facts - covered} "
                  f"threshold={_threshold()} cooldown={cooling} autospawn={_autospawn_enabled()} "
                  f"-> {'LAUNCH' if go else 'SKIP'}")
        print(build_prompt(sid, _run_id()))
        return 0

    return _hook_mode()


if __name__ == "__main__":
    sys.exit(main())
