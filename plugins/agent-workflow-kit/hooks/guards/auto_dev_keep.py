#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Guard (auto_dev_keep) — keep a halted story's worktree, branch and stack volumes in an auto-dev-loop session.
Switch: `config: guards.auto_dev_guard`.

Events:
  * PreToolUse(Skill) and UserPromptExpansion (matcher `auto-dev-loop`): a call that starts the
    `auto-dev-loop` skill marks the session (`.workflow/state/auto-dev/sessions/<session_id>`).
    Never blocks.
  * PreToolUse(Bash|PowerShell): in a marked session, or one the driver launched
    (`WORKFLOW_AUTO_DEV_RUN` set), denies the teardown rules of
    `.workflow/scripts/auto_dev_guard.py` (`shell_check`).

Deny = hookSpecificOutput JSON on stdout (`permissionDecision: deny`), exit 0. Fails open on an
internal error.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
import auto_dev_guard as rules  # noqa: E402  (the scripts dir, on sys.path via _guardlib)


def evaluate(payload: dict) -> int:
    if payload.get("tool_name") == "Skill" or payload.get("hook_event_name") == "UserPromptExpansion":
        try:
            rules.mark_from_payload(payload)
        except OSError:
            pass
        return 0
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return 0
    message = rules.shell_check(payload)
    return g.deny_json(message) if message else 0


if __name__ == "__main__":
    g.run("auto_dev_guard", evaluate)
