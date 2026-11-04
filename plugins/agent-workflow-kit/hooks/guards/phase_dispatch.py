#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard — the story pipeline's phase gate. Switch: `config: guards.phase_dispatch`.

Blocks a phase dispatch whose precondition has no durable record:

    dispatch story-impl        requires  checkpoint-1   (owner approved the spec; no open item left)
    dispatch story-finalize    requires  phase-2 + triage
    dispatch story-rebase      requires  no fact
    invoke skill land-story    requires  phase-3 + pre-merge + checkpoint-3
                               and       the story holds the land lease (`land_lease.py holds <id>`;
                                         exit 1 blocks naming the holder or `free`, exit 2 / a timeout
                                         blocks as "could not be checked"). `--cleanup-only` stays ungated.

Branch check: a story-impl, story-finalize, story-rebase or land-story (not `--cleanup-only`)
dispatch is also blocked while the story's worktree has a train branch (`config: git.train_prefix`)
checked out: an amend or rebase there never reaches the story branch, the ref land-story merges.
A story with no slot-registry branch passes; a git error or timeout blocks as "could not be checked".

It enforces that the fact was WRITTEN DOWN, not that it is true. Fact presence is delegated to
`.workflow/scripts/story_record.py check <story_id> <fact_key>` (exit 0 = positively recorded)
so the gate and the ledger never disagree; a `phase-2 tests RED` line does NOT satisfy phase-2.
When a record check prints a reason (`MISSING  pre-merge gate: <reason>`, the land guard's), the
block text carries it.

Fails CLOSED: an unreadable payload, a record check that times out or crashes, and the
whole-dispatch deadline (`BUDGET_S`, below the hook's timeout, so a slow check denies instead of
being killed into an allow) all block. An effort variant counts as its base agent.

story_id extraction (dispatch prompt / skill args): `story_id: <id>`, `branch_name:
<prefix><id>`, `<git.branch_prefix><id>`. A gated subagent dispatch without a story_id is
blocked. land-story with no id passes (it resolves its own).

`check(payload)` returns the block message (or None) for in-process callers.
Exit 0 = allow; exit 2 + stderr = block.
Self-test: python3 <plugin-root>/hooks/guards/phase_dispatch.py --selftest
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
from agent_roles import base_role  # noqa: E402

HOOK = "phase_dispatch.py"
RECORD_SCRIPT = g.SCRIPTS_DIR / "story_record.py"
LEASE_SCRIPT = g.SCRIPTS_DIR / "land_lease.py"
CHECK_TIMEOUT_S = 25   # one story_record check
BUDGET_S = 60          # every check of one dispatch; the hook entry's timeout is 90 s

GATED_AGENTS = {
    "story-impl": ["checkpoint-1"],
    "story-finalize": ["phase-2", "triage"],
    "story-rebase": [],
}
GATED_SKILLS = {
    "land-story": ["phase-3", "pre-merge", "checkpoint-3"],
}

_SCOPED = ("(run .workflow/scripts/run_scoped_tests.py with --record <story_id> {phase}; "
           "it records an all-green run)")
FACT_HELP = {
    "checkpoint-1": ("the owner's spec approval (Checkpoint 1)", "checkpoint-1 spec approved"),
    "phase-2": ("a GREEN phase-2 scoped-test verdict", _SCOPED.format(phase="phase-2")),
    "triage": ("the owner's Checkpoint 2 triage decisions, persisted to the findings file",
               "(write the '## Triage decisions' section in the story's findings file)"),
    "phase-3": ("a GREEN phase-3 scoped-test verdict", _SCOPED.format(phase="phase-3")),
    "pre-merge": ("a full-gate GREEN for the branch's current gate tree on a base the base branch still has, "
                  "run in the land window, or a recorded exemption",
                  "(in the land window - after land_lease.py acquire <story_id> and the final rebase - run "
                  ".workflow/scripts/run_full_suite.py --story-id <story_id> --record; or append "
                  "'pre-merge gate not required — <AGENTS.md clause>'); a train GREEN (run_full_suite.py "
                  "--train, driven by merge_train.py) counts while it passes the land guard)"),
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


def fact_recorded(story_id: str, fact_key: str, timeout: float = CHECK_TIMEOUT_S) -> tuple[bool | None, str]:
    """(status, reason): True when the fact is POSITIVELY recorded, False when the record lacks it, None when
    the check could not answer (timeout, crash); reason is the text after `MISSING  <row>:`."""
    try:
        r = subprocess.run([sys.executable, str(RECORD_SCRIPT), "check", story_id, fact_key],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None, ""
    if r.returncode == 0:
        return True, ""
    if r.returncode == 1 and (r.stderr or "").startswith("MISSING"):
        prefix = re.match(r"MISSING  [^:\n]+:\s*", r.stderr)
        return False, r.stderr[prefix.end():].strip() if prefix else ""
    return None, ""


def lease_held(story_id: str, timeout: float = CHECK_TIMEOUT_S) -> tuple[bool | None, str]:
    """(status, text): True when the story holds the land lease; False with the holder (or `free`); None with the
    cause when the check could not answer."""
    try:
        r = subprocess.run([sys.executable, str(LEASE_SCRIPT), "holds", story_id],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return None, type(exc).__name__
    if r.returncode == 0:
        return True, ""
    if r.returncode == 1:
        return False, (r.stdout or "").strip() or "free"
    return None, (r.stderr or "").strip()


def worktree_branch(story_id: str, timeout: float = CHECK_TIMEOUT_S) -> tuple[bool | None, str]:
    """(status, text): True when the story's worktree is not on a train branch (or the story has no slot-registry
    branch); False with the instruction when it is; None with the cause when git could not answer."""
    import story_ledger
    arts, branch = story_ledger.resolve_roots(story_ledger.variants(story_id))
    if not branch:
        return True, ""
    worktree = story_ledger._root_of(arts)
    try:
        r = subprocess.run(["git", "-C", worktree, "branch", "--show-current"],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return None, type(exc).__name__
    if r.returncode != 0:
        return None, (r.stderr or "").strip() or f"git exited {r.returncode}"
    current = r.stdout.strip()
    if current.startswith(str(g.cfg("git.train_prefix", "train/"))):
        return False, (f"the worktree is on {current}; check out {branch} first: an amend or rebase on "
                       f"{current} never reaches {branch}")
    return True, ""


def block(what: str, story_id: str, missing: list[str], unknown: list[str] | None = None,
          reasons: dict[str, str] | None = None, lease: tuple[bool | None, str] | None = None,
          branch: tuple[bool | None, str] | None = None) -> str:
    only_branch = not (missing or unknown or (lease is not None and lease[0] is not True))
    if not only_branch:
        header = "the preceding phase has no durable record."
    elif branch is not None and branch[0] is None:
        header = "the story's worktree branch could not be checked."
    else:
        header = "the story's worktree is not on its story branch."
    out = [f"[workflow-hook] {what} BLOCKED -- {header}\n\n"]
    for key in missing:
        desc, example = FACT_HELP.get(key, (key, ""))
        out.append(f"  - missing: {desc}\n")
        if (reasons or {}).get(key):
            out.append(f"      reason: {reasons[key]}\n")
        if example and not example.startswith("("):
            out.append(f"      record it:  uv run --no-project <plugin-root>/scripts/story_record.py "
                       f'append {story_id} "{example}"\n')
        elif example:
            out.append(f"      {example}\n")
    for key in unknown or ():
        out.append(f"  - could not be checked (story_record check timed out or failed): "
                   f"{FACT_HELP.get(key, (key, ''))[0]}\n"
                   f"      the gate fails closed; do not append a duplicate, retry the dispatch\n")
    if lease is not None and lease[0] is False:
        out.append(f"  - missing: the land lease -- {lease[1]}\n"
                   f"      acquire it:  uv run --no-project <plugin-root>/scripts/land_lease.py acquire {story_id}\n")
    elif lease is not None and lease[0] is None:
        out.append(f"  - could not be checked (land_lease.py holds timed out or failed): the land lease"
                   f"{' -- ' + lease[1] if lease[1] else ''}\n      the gate fails closed; retry the dispatch\n")
    if branch is not None and branch[0] is False:
        out.append(f"  - blocked: {branch[1]}\n")
    elif branch is not None and branch[0] is None:
        out.append(f"  - could not be checked (git branch --show-current in the story's worktree failed): "
                   f"the worktree branch{' -- ' + branch[1] if branch[1] else ''}\n"
                   f"      the gate fails closed; retry the dispatch\n")
    if only_branch:
        out.append(f"\n(Hook: <plugin-root>/hooks/guards/{HOOK})\n")
        return "".join(out)
    out.append(
        "\nThe fact exists only in this conversation until it is written down; a clear or\n"
        "compaction loses it. If it is true, record it and retry. If it is not true yet,\n"
        "the phase is not ready to start.\n"
        f"Inspect:  uv run --no-project <plugin-root>/scripts/story_ledger.py {story_id}\n"
        f"(Hook: <plugin-root>/hooks/guards/{HOOK})\n"
    )
    return "".join(out)


def _facts(story_id: str, required: list[str], deadline: float) -> tuple[list[str], list[str], dict[str, str]]:
    """(missing, unknown, reasons) for the required facts; a fact checked past the deadline is unknown."""
    missing, unknown, reasons = [], [], {}
    for key in required:
        remaining = deadline - time.monotonic()
        status, reason = (fact_recorded(story_id, key, timeout=min(CHECK_TIMEOUT_S, remaining))
                          if remaining >= 1 else (None, ""))
        if status is False:
            missing.append(key)
            if reason:
                reasons[key] = reason
        elif status is None:
            unknown.append(key)
    return missing, unknown, reasons


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def check(payload: dict, budget_s: float = BUDGET_S) -> str | None:
    """The block message for an unrecorded phase dispatch, else None."""
    deadline = time.monotonic() + budget_s
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}

    if tool == "Agent":
        subtype = str(ti.get("subagent_type") or "").strip()
        required = GATED_AGENTS.get(base_role(subtype) or "")
        if required is None:
            return None
        story_id = extract_story_id(str(ti.get("prompt") or ""))
        if not story_id:
            return (f"[workflow-hook] {subtype} dispatch BLOCKED -- no story_id in the prompt.\n\n"
                    f"  Every {subtype} dispatch must carry `story_id` and `worktree_path`.\n"
                    f"  Without a story id this gate cannot check the handoff record.\n"
                    f"(Hook: <plugin-root>/hooks/guards/{HOOK})\n")
        missing, unknown, reasons = _facts(story_id, required, deadline)
        rem = _remaining(deadline)
        branch = worktree_branch(story_id, timeout=min(CHECK_TIMEOUT_S, rem)) if rem >= 1 else (None, "")
        if missing or unknown or branch[0] is not True:
            return block(f"{subtype} dispatch", story_id, missing, unknown, reasons, branch=branch)
        return None

    if tool == "Skill":
        skill = str(ti.get("skill") or ti.get("name") or "").strip().lstrip("/")
        required = GATED_SKILLS.get(skill)
        if not required:
            return None
        args = str(ti.get("args") or "")
        story_id = extract_story_id(args) or (args.split()[0].strip() if args.split() else None)
        if not story_id or "--cleanup-only" in args:
            return None
        missing, unknown, reasons = _facts(story_id, required, deadline)
        rem = _remaining(deadline)
        lease = lease_held(story_id, timeout=min(CHECK_TIMEOUT_S, rem)) if rem >= 1 else (None, "")
        rem = _remaining(deadline)
        branch = worktree_branch(story_id, timeout=min(CHECK_TIMEOUT_S, rem)) if rem >= 1 else (None, "")
        if missing or unknown or lease[0] is not True or branch[0] is not True:
            return block(f"/{skill} {story_id}", story_id, missing, unknown, reasons, lease, branch)
        return None

    return None


UNREADABLE = ("[workflow-hook] dispatch BLOCKED -- the hook payload could not be read, so the phase gate "
              f"cannot run; it fails closed.\n(Hook: <plugin-root>/hooks/guards/{HOOK})\n")


def evaluate(payload: dict) -> int:
    message = check(payload)
    if message:
        sys.stderr.write(message)
        return 2
    return 0


def _selftest() -> int:
    """Routing + per-transition requirements against FIXED fact / lease / branch maps."""
    global fact_recorded, lease_held, worktree_branch
    branches = {"st-t": (False, "the worktree is on train/st-t; check out story/st-t first"),
                "st-x": (None, "git exited 128")}
    leases = {"st-t": (True, ""), "st-c": (True, ""), "st-e": (False, "free"),
              "st-f": (False, "st-other has held the lease for 3m"), "st-v": (None, "malformed record"),
              "st-x": (True, "")}
    recorded = {
        ("st-a", "checkpoint-1"): True,
        ("st-b", "checkpoint-1"): True, ("st-b", "phase-2"): True, ("st-b", "triage"): True,
        ("st-c", "checkpoint-1"): True, ("st-c", "phase-2"): True, ("st-c", "triage"): True,
        ("st-c", "phase-3"): True, ("st-c", "pre-merge"): True, ("st-c", "checkpoint-3"): True,
        ("st-d", "checkpoint-3"): True,
        ("st-g", "phase-3"): True, ("st-g", "checkpoint-3"): True,
        ("st-u", "checkpoint-1"): None,
    }
    for sid in ("st-e", "st-f", "st-v", "st-t", "st-x"):
        for key in GATED_SKILLS["land-story"] + GATED_AGENTS["story-impl"]:
            recorded[(sid, key)] = True
    real = (fact_recorded, lease_held, worktree_branch)
    fact_recorded = lambda s, k, timeout=None: (recorded.get((s, k), False), "")  # noqa: E731
    lease_held = lambda s, timeout=None: leases.get(s, (False, "free"))  # noqa: E731
    worktree_branch = lambda s, timeout=None: branches.get(s, (True, ""))  # noqa: E731
    A, S = "Agent", "Skill"
    cases = [
        ("non-pipeline agent passes", A, {"subagent_type": "Explore", "prompt": "find x"}, 0),
        ("unrelated skill passes", S, {"skill": "run-tests", "args": "st-a"}, 0),
        ("story-impl without story_id blocked", A, {"subagent_type": "story-impl", "prompt": "go"}, 2),
        ("story-impl with checkpoint-1 passes", A, {"subagent_type": "story-impl", "prompt": "story_id: st-a"}, 0),
        ("story-impl-max counts as story-impl", A, {"subagent_type": "story-impl-max", "prompt": "story_id: st-none"}, 2),
        ("story-impl without checkpoint-1 blocked", A, {"subagent_type": "story-impl", "prompt": "story_id: st-none"}, 2),
        ("story-impl blocked when the check cannot answer", A, {"subagent_type": "story-impl", "prompt": "story_id: st-u"}, 2),
        ("story-finalize without phase-2+triage blocked", A, {"subagent_type": "story-finalize", "prompt": "story_id: st-a"}, 2),
        ("story-finalize with phase-2+triage passes", A, {"subagent_type": "story-finalize", "prompt": "story_id: st-b"}, 0),
        ("land-story without checkpoint-3 blocked", S, {"skill": "land-story", "args": "st-b"}, 2),
        ("land-story without phase-3 blocked", S, {"skill": "land-story", "args": "st-d"}, 2),
        ("land-story without pre-merge blocked", S, {"skill": "land-story", "args": "st-g"}, 2),
        ("land-story with every fact and the lease passes", S, {"skill": "land-story", "args": "st-c"}, 0),
        ("land-story with a free lease blocked", S, {"skill": "land-story", "args": "st-e"}, 2),
        ("land-story with another holder blocked", S, {"skill": "land-story", "args": "st-f"}, 2),
        ("land-story with an unanswered lease check blocked", S, {"skill": "land-story", "args": "st-v"}, 2),
        ("land-story --cleanup-only passes", S, {"skill": "land-story", "args": "st-a --cleanup-only"}, 0),
        ("story-impl on a train branch blocked", A, {"subagent_type": "story-impl", "prompt": "story_id: st-t"}, 2),
        ("story-rebase on a train branch blocked", A, {"subagent_type": "story-rebase", "prompt": "story_id: st-t"}, 2),
        ("story-rebase on a story branch passes", A, {"subagent_type": "story-rebase", "prompt": "story_id: st-a"}, 0),
        ("land-story on a train branch blocked", S, {"skill": "land-story", "args": "st-t"}, 2),
        ("land-story --cleanup-only on a train branch passes", S, {"skill": "land-story", "args": "st-t --cleanup-only"}, 0),
        ("story-impl blocked when the branch check cannot answer", A, {"subagent_type": "story-impl", "prompt": "story_id: st-x"}, 2),
    ]
    bad = 0
    try:
        for name, tool, ti, expect in cases:
            got = 2 if check({"tool_name": tool, "tool_input": ti}) else 0
            bad += got != expect
            print(f"  [{'PASS' if got == expect else 'FAIL'}] {name}  (expect {expect}, got {got})")
        first = (check({"tool_name": A, "tool_input": {"subagent_type": "story-impl",
                                                       "prompt": "story_id: st-x"}}) or "").splitlines()[:1]
        ok = bool(first) and "could not be checked" in first[0]
        bad += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] an unanswered branch check is headed as unchecked")
        spent = check({"tool_name": A, "tool_input": {"subagent_type": "story-impl", "prompt": "story_id: st-a"}},
                      budget_s=0)
        ok = bool(spent) and "could not be checked" in spent
        bad += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] an exhausted deadline blocks instead of allowing")
    finally:
        fact_recorded, lease_held, worktree_branch = real
    print("\nALL PASS" if not bad else f"\n{bad} FAILURE(S)")
    return 0 if not bad else 1


def main() -> int:
    if not g.enabled("phase_dispatch"):
        return 0
    payload = g.read_payload()
    if payload is None:
        sys.stderr.write(UNREADABLE)
        return 2
    try:
        return evaluate(payload)
    except Exception as exc:  # a crashing gate stays CLOSED for gated dispatches
        tool = payload.get("tool_name")
        ti = payload.get("tool_input") or {}
        gated = (tool == "Agent" and base_role(str(ti.get("subagent_type") or "")) in GATED_AGENTS) or \
                (tool == "Skill" and str(ti.get("skill") or "").lstrip("/") in GATED_SKILLS
                 and "--cleanup-only" not in str(ti.get("args") or ""))
        if gated:
            sys.stderr.write(f"[workflow-hook] phase gate error ({type(exc).__name__}: {exc}); it fails closed.\n"
                             f"(Hook: <plugin-root>/hooks/guards/{HOOK})\n")
            return 2
        return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(main())
