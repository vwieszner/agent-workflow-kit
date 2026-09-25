#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard — the story pipeline's phase gate. Switch: `config: guards.phase_dispatch`.

Blocks a phase dispatch whose precondition has no durable record:

    dispatch story-impl        requires  checkpoint-1   (owner approved the spec)
    dispatch story-finalize    requires  phase-2 + triage
    invoke skill land-story    requires  checkpoint-3   (owner approved the merge)

It enforces that the fact was WRITTEN DOWN, not that it is true. Fact presence is
delegated to `.workflow/scripts/story_record.py check <story_id> <fact_key>` (exit 0 =
positively recorded) so the gate and the ledger never disagree; a `phase-2 tests RED`
line does NOT satisfy phase-2. A broken or missing checker keeps the gate CLOSED.

story_id extraction (dispatch prompt / skill args): `story_id: <id>`, `branch_name:
<prefix><id>`, `<git.branch_prefix><id>`. A gated subagent dispatch without a story_id is
blocked. land-story with no id, or with `--cleanup-only`, passes.

Exit 0 = allow; exit 2 + stderr = block.
Self-test: python3 .workflow/hooks/guards/phase_dispatch.py --selftest
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

HOOK = "phase_dispatch.py"
RECORD_SCRIPT = g.SCRIPTS_DIR / "story_record.py"

GATED_AGENTS = {
    "story-impl": ["checkpoint-1"],
    "story-finalize": ["phase-2", "triage"],
}
GATED_SKILLS = {
    "land-story": ["checkpoint-3"],
}

FACT_HELP = {
    "checkpoint-1": ("the owner's spec approval (Checkpoint 1)", "checkpoint-1 spec approved"),
    "phase-2": ("a GREEN phase-2 scoped-test verdict", "phase-2 tests GREEN -- <label> (<n> tests, exit 0)"),
    "triage": ("the owner's Checkpoint 2 triage decisions, persisted to the findings file",
               "(write the '## Triage decisions' section in the story's findings file)"),
    "phase-3": ("a GREEN phase-3 scoped-test verdict", "phase-3 tests GREEN -- <label> (<n> tests, exit 0)"),
    "checkpoint-3": ("the owner's merge approval (Checkpoint 3)", "checkpoint-3 merge approved"),
}


def _id_patterns() -> tuple[str, ...]:
    prefix = re.escape(str(g.cfg("git.branch_prefix", "story/")))
    return (
        r"story[_\s-]?id\s*[:=]\s*[\"'`*]*([A-Za-z0-9][\w.\-]*)",
        rf"branch[_\s-]?name\s*[:=]\s*[\"'`*]*{prefix}([A-Za-z0-9][\w.\-]*)",
        rf"(?<![\w/]){prefix}([A-Za-z0-9][\w.\-]*)",
    )


def extract_story_id(text: str) -> str | None:
    for pat in _id_patterns():
        m = re.search(pat, text or "", re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip(".,;:\"'`*")
    return None


def fact_recorded(story_id: str, fact_key: str) -> bool:
    """True only if the fact is POSITIVELY recorded. Any checker failure -> False."""
    try:
        r = subprocess.run(
            [sys.executable, str(RECORD_SCRIPT), "check", story_id, fact_key],
            capture_output=True, text=True, timeout=25,
        )
        return r.returncode == 0
    except Exception:
        return False


def block(what: str, story_id: str, missing: list[str]) -> int:
    sys.stderr.write(f"[workflow-hook] {what} BLOCKED -- the preceding phase has no durable record.\n\n")
    for key in missing:
        desc, example = FACT_HELP.get(key, (key, ""))
        sys.stderr.write(f"  - missing: {desc}\n")
        if example and not example.startswith("("):
            sys.stderr.write(
                f"      record it:  uv run --no-project .workflow/scripts/story_record.py "
                f'append {story_id} "{example}"\n'
            )
        elif example:
            sys.stderr.write(f"      {example}\n")
    sys.stderr.write(
        "\nThe fact exists only in this conversation until it is written down; a clear or\n"
        "compaction loses it. If it is true, record it and retry. If it is not true yet,\n"
        "the phase is not ready to start.\n"
        f"Inspect:  uv run --no-project .workflow/scripts/story_ledger.py {story_id}\n"
        f"(Hook: .workflow/hooks/guards/{HOOK})\n"
    )
    return 2


def evaluate(payload: dict) -> int:
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}

    if tool == "Agent":
        subtype = str(ti.get("subagent_type") or "").strip()
        required = GATED_AGENTS.get(subtype)
        if not required:
            return 0
        story_id = extract_story_id(str(ti.get("prompt") or ""))
        if not story_id:
            sys.stderr.write(
                f"[workflow-hook] {subtype} dispatch BLOCKED -- no story_id in the prompt.\n\n"
                f"  Every {subtype} dispatch must carry `story_id` and `worktree_path`.\n"
                f"  Without a story id this gate cannot check the handoff record.\n"
                f"(Hook: .workflow/hooks/guards/{HOOK})\n"
            )
            return 2
        missing = [k for k in required if not fact_recorded(story_id, k)]
        return block(f"{subtype} dispatch", story_id, missing) if missing else 0

    if tool == "Skill":
        skill = str(ti.get("skill") or ti.get("name") or "").strip().lstrip("/")
        required = GATED_SKILLS.get(skill)
        if not required:
            return 0
        args = str(ti.get("args") or "")
        story_id = extract_story_id(args) or (args.split()[0].strip() if args.split() else None)
        if not story_id or "--cleanup-only" in args:
            return 0
        missing = [k for k in required if not fact_recorded(story_id, k)]
        return block(f"/{skill} {story_id}", story_id, missing) if missing else 0

    return 0


def _selftest() -> int:
    """Routing + per-transition requirements against a FIXED fact map (record lookup stubbed)."""
    global fact_recorded
    recorded = {
        ("st-a", "checkpoint-1"): True,
        ("st-b", "checkpoint-1"): True, ("st-b", "phase-2"): True, ("st-b", "triage"): True,
        ("st-c", "checkpoint-1"): True, ("st-c", "phase-2"): True, ("st-c", "triage"): True,
        ("st-c", "phase-3"): True, ("st-c", "checkpoint-3"): True,
    }
    real = fact_recorded
    fact_recorded = lambda s, k: recorded.get((s, k), False)  # noqa: E731
    A, S = "Agent", "Skill"
    cases = [
        ("non-pipeline agent passes", A, {"subagent_type": "Explore", "prompt": "find x"}, 0),
        ("unrelated skill passes", S, {"skill": "run-tests", "args": "st-a"}, 0),
        ("story-impl without story_id blocked", A, {"subagent_type": "story-impl", "prompt": "go"}, 2),
        ("story-impl with checkpoint-1 passes", A, {"subagent_type": "story-impl", "prompt": "story_id: st-a"}, 0),
        ("story-impl without checkpoint-1 blocked", A, {"subagent_type": "story-impl", "prompt": "story_id: st-none"}, 2),
        ("story-finalize without phase-2+triage blocked", A, {"subagent_type": "story-finalize", "prompt": "story_id: st-a"}, 2),
        ("story-finalize with phase-2+triage passes", A, {"subagent_type": "story-finalize", "prompt": "story_id: st-b"}, 0),
        ("land-story without checkpoint-3 blocked", S, {"skill": "land-story", "args": "st-b"}, 2),
        ("land-story with checkpoint-3 passes", S, {"skill": "land-story", "args": "st-c"}, 0),
        ("land-story --cleanup-only passes", S, {"skill": "land-story", "args": "st-a --cleanup-only"}, 0),
    ]
    bad = 0
    try:
        for name, tool, ti, expect in cases:
            err = sys.stderr
            sys.stderr = open(os.devnull, "w")
            try:
                got = evaluate({"tool_name": tool, "tool_input": ti})
            finally:
                sys.stderr.close()
                sys.stderr = err
            bad += got != expect
            print(f"  [{'PASS' if got == expect else 'FAIL'}] {name}  (expect {expect}, got {got})")
    finally:
        fact_recorded = real
    print("\nALL PASS" if not bad else f"\n{bad} FAILURE(S)")
    return 0 if not bad else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    g.run("phase_dispatch", evaluate)
