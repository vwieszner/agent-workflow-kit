#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Stop and PreToolUse(AskUserQuestion) guard — a land-window session that waits on the owner does not hold
the land lease. Switch: `config: guards.land_lease_wait`.

Rule (parallel-dev-wave workflow.md "Land window"): the window contains no human wait. A question to the owner
ends the window; the lease is released first and the window restarts from `land_lease.py acquire <id>` after
the answer. A session that only waits on a background task it started (the gate, `story-rebase`,
`merge_train.py wait`) keeps the lease. Stop fires at every turn end, so the check stops at the first step that
settles it:

  1. Stop only: the payload's `background_tasks` is a non-empty list -> allow, no IO.
  2. The land lease is free -> allow.
  3. The holder is not this session's story -> allow. The holder is this session's only when its transcript
     holds a shell `tool_use` running `land_lease.py acquire <holder>`.
  4. The holder is merged (its slot-registry branch is gone, or `git merge-base --is-ancestor <branch>
     <config: git.base_branch>` exits 0) -> allow: after the merge the lease is held to land-story's last step.
  5. Otherwise the session waits on the owner:
       * Stop, first firing -> `{"decision": "block", "reason": ...}`: continue the window, or release first.
       * Stop, re-entered (`stop_hook_active`) -> the hook releases the lease itself and says so through
         `systemMessage`; it never blocks twice in a row.
       * AskUserQuestion -> deny; the hook never releases there.
       * A Stop payload without a `background_tasks` key cannot be classified: the lease is kept and a
         `systemMessage` names the release command.

Fails toward keeping the lease: an unparseable payload, a missing or unreadable transcript (OpenCode has none),
a lease error, a git call that exits other than 0 or 1 and any unexpected exception allow with nothing on stdout
and one stderr line. Output is JSON on stdout with exit 0, never exit 2. Every release goes through
`land_lease.release`, which keeps the line.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

GIT_TIMEOUT_SECONDS = 15
RULE = 'parallel-dev-wave workflow.md "Land window"'
HOOK = ".workflow/hooks/guards/land_lease_wait.py"
RELEASED_PREFIX = "[land lease] released for"
SHELL_TOOLS = {"Bash", "PowerShell"}
ACQUIRE = re.compile(r"land_lease\.py[\"']?\s+acquire\s+[\"']?([A-Za-z0-9][\w.\-]*)")


class _Skip(Exception):
    """The check cannot settle the case; the lease is kept."""


def _release_command(story: str) -> str:
    return f"uv run --no-project .workflow/scripts/land_lease.py release {story}"


def _blocks(entry: dict) -> list[dict]:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _acquired_in(path: str | None, story: str) -> bool:
    """True when the transcript holds a shell `land_lease.py acquire` call for the story (a cheap raw-bytes
    pre-scan answers most transcripts without the JSON parse)."""
    import land_lease
    if not path:
        raise _Skip("transcript path missing")
    wanted = land_lease.dashed(story)
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise _Skip("transcript unreadable") from None
    holder = rb"[-.]".join(re.escape(part.encode("utf-8")) for part in wanted.split("-"))
    if b"land_lease.py" not in raw or b"acquire" not in raw or not re.search(holder, raw):
        return False
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        for block in _blocks(entry):
            if block.get("type") != "tool_use" or block.get("name") not in SHELL_TOOLS:
                continue
            command = str((block.get("input") or {}).get("command", ""))
            if any(land_lease.dashed(m.group(1)) == wanted for m in ACQUIRE.finditer(command)):
                return True
    return False


def _merged(story: str) -> bool:
    import story_ledger
    _arts, branch = story_ledger.resolve_roots(story_ledger.variants(story))
    if branch is None:
        return True
    proc = subprocess.run(["git", "-C", str(g.wfconfig.repo_root()), "merge-base", "--is-ancestor", branch,
                           story_ledger.base_branch()], capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    if proc.returncode not in (0, 1):
        raise _Skip(f"git merge-base exited {proc.returncode}")
    return proc.returncode == 0


def _block_reason(story: str) -> str:
    return (
        f"[workflow-hook] {story} holds the land lease and this turn ends waiting on the owner with no background "
        f"task running. The land window holds no human wait.\n"
        f"  If the window has a next step, continue with it.\n"
        f"  Otherwise release first: {_release_command(story)}\n"
        f"  then say in your message that the lease is released and that the window restarts from `acquire` "
        f"after the owner's answer, and end the turn.\n"
        f"  In an unattended (auto-dev-loop) session never ask: run the halt-and-release procedure.\n"
        f"(Hook: {HOOK}; rule: {RULE})\n"
    )


def _ask_reason(story: str) -> str:
    return (
        f"[workflow-hook] {story} holds the land lease and the land window holds no human wait: release first, "
        f"then ask.\n  Release: {_release_command(story)}\n"
        f"  Say in your message that the window restarts from `acquire` after the owner's answer.\n"
        f"  If a background task of the window is still running (the gate, story-rebase, merge_train.py wait), "
        f"wait for its notification before asking.\n"
        f"(Hook: {HOOK}; rule: {RULE})"
    )


def _decide(payload: dict) -> dict | None:
    is_ask = payload.get("tool_name") in ("AskUserQuestion", "question")
    if not is_ask and payload.get("hook_event_name") != "Stop":
        return None
    tasks = payload.get("background_tasks")
    if not is_ask and isinstance(tasks, list) and tasks:
        return None
    import land_lease
    lease = land_lease.read(land_lease.default_path())
    if lease is None:
        return None
    story = lease["story"]
    if not _acquired_in(payload.get("transcript_path"), story) or _merged(story):
        return None
    if is_ask:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": _ask_reason(story)}}
    if not isinstance(tasks, list):
        return {"systemMessage": (
            f"[land lease] {story} holds the land lease and this turn end carries no background_tasks list, so the "
            f"wait cannot be classified and the lease is kept. If the session now waits on you, release it: "
            f"{_release_command(story)}")}
    if not payload.get("stop_hook_active"):
        return {"decision": "block", "reason": _block_reason(story)}
    outcome, _holder = land_lease.release(land_lease.default_path(), story)
    if outcome != "released":
        return None
    return {"systemMessage": (
        f"{RELEASED_PREFIX} {story}: the turn ended waiting on you with no background task running. "
        f"After your answer the window restarts from land_lease.py acquire {story}.")}


def check(payload: dict) -> dict | None:
    """The JSON to print for this Stop / AskUserQuestion payload, or None. Never raises."""
    try:
        return _decide(payload)
    except _Skip as exc:
        cause = str(exc)
    except Exception as exc:
        cause = f"{type(exc).__name__}: {exc}"
    sys.stderr.write(f"[workflow-hook] land-lease wait check skipped: {cause}\n")
    return None


def main() -> int:
    if not g.enabled("land_lease_wait"):
        return 0
    payload = g.read_payload()
    if payload is None:
        sys.stderr.write("[workflow-hook] land-lease wait check skipped: unparseable payload\n")
        return 0
    result = check(payload)
    if result is None:
        return 0
    try:
        print(json.dumps(result))
        sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"[workflow-hook] land-lease wait check: output failed ({type(exc).__name__}: {exc})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
