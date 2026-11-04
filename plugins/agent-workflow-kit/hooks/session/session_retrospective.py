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
(`config: curation.retro_cooldown_s`) has elapsed, launch a DETACHED copy of this script in
`--analyst` mode and exit 0 at once, so compaction is never blocked. The launch sets the
session's launched_facts. If the launch is disabled or the analyst CLI is missing, the session
stays "unmined" and the session-start nudge (check_pending_proposals.py) points the owner at the
`retro` skill — the in-session, observed path, which calls `--mark` when done.

Analyst mode runs the headless analyst synchronously (bounded by
`config: curation.analyst_timeout_s`), which reads the journal and writes proposals under
.workflow/state/proposals/, and appends its outcome to .workflow/state/retro-launches.jsonl.
    claude    `claude --name retro-<run_id> --permission-mode <config: curation.analyst_permission_mode>
               --output-format json -p <prompt>` — a run fails on a non-zero exit, `is_error` (an
               expired login arrives that way), a timeout, or permission denials when it neither
               wrote a proposal carrying its run_id nor returned exactly NO_PROPOSALS.
    opencode  `opencode run <prompt>` — a run fails on a non-zero exit or a timeout.
A failed run resets launched_facts, so the session is unmined again and the session-start nudge
reports the failure.

The analyst prompt names the facts an earlier run already mined (the first `covered` by ts) and
lists every pending and archived proposal, so a run neither re-mines old facts nor re-proposes an
idea already waiting: it appends new evidence to the pending proposal instead.

Analyst selection (`config: curation.analyst`):
    auto      the calling tool: `--tool claude|opencode` (each adapter passes it) →
              `claude -p <prompt>` / `opencode run <prompt>`; without `--tool`, a payload with
              `transcript_path` → claude, otherwise → opencode
    claude    always `claude -p <prompt>`
    opencode  always `opencode run <prompt>`
    off       never launch (equivalent to env WORKFLOW_RETRO_AUTOSPAWN=0)

CLI
    session_retrospective.py --list                     # unmined sessions + journal paths
    session_retrospective.py --mark --session <id>      # record a session as analyzed
    session_retrospective.py --dry-run --session <id>   # go/no-go decision + the prompt
    session_retrospective.py --prompt --session <id>    # print only the analysis prompt
    session_retrospective.py --analyst --session <id> --run-id <id> --facts <n> --prior <n>
                             --covered <n> --tool claude|opencode   # one background run

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

NO_PROPOSALS = "NO_PROPOSALS"  # the analyst's whole final result when nothing qualifies
CREATE_NO_WINDOW = 0x08000000  # Windows: the detached analyst has no console and must not open one
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


def _analyst_timeout_s() -> int:
    return int(wfconfig.get("curation.analyst_timeout_s", 1800))


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(wfconfig.repo_root()))
    except ValueError:
        return str(p)


def _new_facts_line(session_id: str, covered: int) -> str:
    if covered <= 0:
        return "Every fact in these journals is new."
    new = cs.new_facts(session_id, covered)
    if not new:
        return f"The first {covered} meaningful facts were mined by an earlier run; none is new."
    return (f"The first {covered} meaningful facts (ts before {new[0].get('ts', '?')}) were mined by "
            f"an earlier run: read them for context only. A candidate needs evidence at or after that ts.")


def _pending_block() -> str:
    lines = []
    for p in cs.pending_proposals():
        kind, title = cs.proposal_heading(p)
        lines.append(f"  - {p.name} | {kind} | {title}")
    return "\n".join(lines) or "  (none)"


def build_prompt(session_id: str, run_id: str, covered: int = 0) -> str:
    journals = "\n".join(f"  - {_rel(p)}" for p in cs.session_journals(session_id)) or "  (none)"
    pdir = _rel(cs.proposals_dir())
    learnings = _rel(cs.learnings_path())
    scripts_index = str(wfconfig.get("paths.scripts_index", "scripts/INDEX.md"))
    agent_src = str(wfconfig.get("curation.agent_source_dir", ".workflow/agents"))
    root = wfconfig.repo_root()
    skill_dirs = ", ".join(d for d in SKILL_DIRS if (root / d).is_dir()) or "(none installed)"
    agent_dirs = ", ".join([agent_src] + [d for d in AGENT_DIRS if (root / d).is_dir()])
    kinds = "|".join(cs.proposal_kinds())
    archived = ", ".join(p.name for p in cs.archived_proposals()) or "(none)"
    return f"""You are the session-retrospective analyst for this repository. Mine a session activity
journal for REUSABLE tooling opportunities and write PROPOSALS ONLY. Be conservative.

JOURNAL FILES (JSON lines, one per turn; mine each line's `facts` array — kinds: command_run,
script_written, script_edited, tool_error, interrupted, skill_invoked, agent_spawned, user_message):
{journals}
{_new_facts_line(session_id, covered)}

REFERENCE BEFORE PROPOSING (avoid duplicates and previously rejected ideas):
  - {scripts_index} (project scripts) and .workflow/scripts/INDEX.md (workflow kit scripts)
  - skills: {skill_dirs}
  - agents: {agent_dirs}
  - rules: .workflow/rules/
  - {learnings} (rejected candidates — may not exist; never re-propose anything listed there)

PENDING PROPOSALS (awaiting `curate`; <file> | <kind> | <title>):
{_pending_block()}
A candidate one of these already proposes is NOT a new proposal. Append its new evidence (facts,
counts, sessions) to that proposal file as one section `## Additional evidence ({run_id})` and
change nothing else in it or its draft.

ARCHIVED PROPOSALS (resolved by `curate`, under {pdir}/archive/): {archived}
Skip a candidate an archived proposal already carried, unless this journal shows its shipped
artifact failing; read that archived proposal before deciding.

TASK:
1. Look across the session for:
   - command_run sequences repeated several times (an ad-hoc task that recurs)
   - script_written / script_edited that look reusable but are not registered in {scripts_index}
   - tool_error / interrupted clusters that signal a recurring mistake
   - skill_invoked / agent_spawned patterns suggesting a missing skill or agent
2. Classify each GENUINE candidate as exactly one kind: {kinds}.
   Require real recurrence (>=2-3x) or clear reuse. SKIP single occurrences, anything already
   covered by an existing artifact, and anything in the learnings file. Prefer 0 proposals over
   speculative ones. If nothing qualifies, or every candidate only added evidence to a pending
   proposal, print exactly {NO_PROPOSALS} and create no new file.
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
## Impact
- Quality: <raises | keeps | lowers> — <figure from the journals, with scope | unmeasured>
- Speed: <raises | keeps | lowers> — <figure from the journals, with scope | unmeasured>
- Token cost: <lowers | keeps | raises> — <figure from the journals, with scope | unmeasured>
(Measure by reading the journal files only; an axis that needs a shell command to measure reads
"unmeasured" — `curate` measures it.)
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


def _analyst_choice(payload: dict, tool: str | None = None) -> str | None:
    """`claude` / `opencode` when that CLI is on PATH, else None (also for `off`)."""
    choice = str(wfconfig.get("curation.analyst", "auto")).lower()
    if choice == "off":
        return None
    if choice == "auto":
        choice = tool or ("claude" if payload.get("transcript_path") else "opencode")
    if choice not in ("claude", "opencode") or not shutil.which(choice):
        return None
    return choice


def _analyst_argv(choice: str, run_id: str, prompt: str) -> list:
    exe = shutil.which(choice) or choice
    if choice == "claude":
        # A headless run in the default permission mode denies every write under the state dir.
        mode = str(wfconfig.get("curation.analyst_permission_mode", "auto"))
        return [exe, "--name", f"retro-{run_id}", "--permission-mode", mode,
                "--output-format", "json", "-p", prompt]
    return [exe, "run", prompt]


def _launch(payload: dict, session_id: str, facts: int, prior: int, covered: int,
            tool: str | None = None) -> None:
    choice = _analyst_choice(payload, tool)
    if not choice:
        return  # leave unmined → nudge points at the retro skill
    cs.proposals_dir().mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WORKFLOW_RETRO_RUNNING": "1"}
    kwargs: dict = dict(cwd=str(wfconfig.repo_root()), env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--analyst", "--session", session_id,
                      "--run-id", _run_id(), "--facts", str(facts), "--prior", str(prior),
                      "--covered", str(covered), "--tool", choice], **kwargs)  # fire-and-forget
    cs.write_marker(session_id, launched_facts=facts)  # only reached if Popen did not raise
    _touch_cooldown()


def _wrote_proposal(run_id: str) -> bool:
    """True when a pending proposal file carries this run's run_id."""
    try:
        return any(cs.proposal_run_id(p) == run_id for p in cs.pending_proposals())
    except Exception:  # noqa: BLE001
        return False


def _outcome(exit_code, stdout: str, wrote_proposal: bool) -> dict:
    """Record fields of one finished `claude -p --output-format json` run. Permission denials fail
    it only when it neither wrote a proposal nor returned exactly NO_PROPOSALS."""
    try:
        res = json.loads(stdout)
    except (ValueError, TypeError):
        res = None
    if not isinstance(res, dict):
        return {"ok": False, "exit_code": exit_code, "subtype": None, "total_cost_usd": None,
                "duration_ms": None, "error": f"exit {exit_code}, no JSON result"}
    denials = res.get("permission_denials") or []
    denied = None
    if denials:
        tools = sorted({str(d.get("tool_name")) for d in denials if isinstance(d, dict)})
        denied = f"{len(denials)} permission denial(s): {', '.join(tools)}"
    error = None
    if res.get("is_error") or exit_code != 0:
        error = str(res.get("result") or f"exit {exit_code}")[:300]
    elif denied and not (wrote_proposal or str(res.get("result") or "").strip() == NO_PROPOSALS):
        error = f"{denied}, and no proposal written or {NO_PROPOSALS} returned"
    return {"ok": error is None, "exit_code": exit_code, "subtype": res.get("subtype"),
            "total_cost_usd": res.get("total_cost_usd"), "duration_ms": res.get("duration_ms"),
            "error": error, "denials": denied, "analyst_session": res.get("session_id")}


def _plain_outcome(exit_code) -> dict:
    """Record fields of a run whose CLI prints no JSON result (opencode)."""
    return {"ok": exit_code == 0, "exit_code": exit_code, "subtype": None, "total_cost_usd": None,
            "duration_ms": None, "error": None if exit_code == 0 else f"exit {exit_code}"}


def _failed(error: str) -> dict:
    return {"ok": False, "exit_code": None, "subtype": None, "total_cost_usd": None,
            "duration_ms": None, "error": error[:300]}


def _analyst(session_id: str, run_id: str, facts: int, prior: int, covered: int, tool: str) -> int:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not shutil.which(tool):
        out = _failed(f"{tool} not found on PATH")
    else:
        try:
            proc = subprocess.run(_analyst_argv(tool, run_id, build_prompt(session_id, run_id, covered)),
                                  cwd=str(wfconfig.repo_root()), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=_analyst_timeout_s(),
                                  creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            out = (_outcome(proc.returncode, proc.stdout, _wrote_proposal(run_id)) if tool == "claude"
                   else _plain_outcome(proc.returncode))
        except subprocess.TimeoutExpired:
            out = _failed(f"timed out after {_analyst_timeout_s()} s")
        except Exception as e:  # noqa: BLE001
            out = _failed(f"{type(e).__name__}: {e}")
    # A later launch for the same session owns the marker once it has moved past this run's facts.
    if not out["ok"] and cs.read_marker(session_id)["launched_facts"] == facts:
        cs.write_marker(session_id, launched_facts=prior)
    cs.append_launch({"run_id": run_id, "session": session_id, "facts": facts, "started": started,
                      "ended": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "tool": tool, **out})
    return 0


def _autospawn_enabled() -> bool:
    if str(wfconfig.get("curation.analyst", "auto")).lower() == "off":
        return False
    return os.environ.get("WORKFLOW_RETRO_AUTOSPAWN", "1").lower() not in ("0", "false", "no", "off")


def _hook_mode(tool: str | None = None) -> int:
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
        _launch(payload, session_id, facts, marker["launched_facts"], covered, tool)
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
    ap.add_argument("--tool", choices=("claude", "opencode"), default=None)
    ap.add_argument("--analyst", action="store_true")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--facts", type=int, default=0)
    ap.add_argument("--prior", type=int, default=0)
    ap.add_argument("--covered", type=int, default=0)
    args, _ = ap.parse_known_args()

    if args.analyst:
        if not (args.session and args.run_id and args.tool):
            print("ERROR: --analyst requires --session, --run-id and --tool", file=sys.stderr)
            return 2
        return _analyst(args.session, args.run_id, args.facts, args.prior, args.covered, args.tool)

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
        facts = cs.session_fact_count(sid)
        marker = cs.read_marker(sid)
        covered = max(marker["analyzed_facts"], marker["launched_facts"])
        if args.dry_run:
            cooling = _within_cooldown()
            go = (facts - covered) >= _threshold() and not cooling and _autospawn_enabled()
            print(f"session={sid} facts={facts} covered={covered} new={facts - covered} "
                  f"threshold={_threshold()} cooldown={cooling} autospawn={_autospawn_enabled()} "
                  f"-> {'LAUNCH' if go else 'SKIP'}")
        print(build_prompt(sid, _run_id(), covered))
        return 0

    return _hook_mode(args.tool)


if __name__ == "__main__":
    sys.exit(main())
