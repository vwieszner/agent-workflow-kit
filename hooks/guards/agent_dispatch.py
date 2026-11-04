#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(Agent) guard — nested-dispatch edge allowlist and story-impl escalation.
Switch: `config: guards.agent_dispatch`.

1. Edge allowlist. A dispatch made inside a subagent (payload `agent_type` set) may spawn only
   the callees `config: guards.dispatch_edges` lists for that caller (an effort variant counts as
   its base agent, `agent_roles.base_role`), and only with `run_in_background: false`: a subagent
   that ends its turn to wait for a background agent hands back unfinished. When
   `config: guards.dispatch_edge_models` names a model for the `caller->callee` edge, the
   dispatch must pass that `model`. A main-session dispatch (no `agent_type`) is not checked.
   Fails CLOSED: a crash inside the check denies.
2. Escalation. The `subagent_type` of a story-impl dispatch must be the variant its hand-off file
   (the prompt's `handoff: <path>` line) calls for: `rounds_used` 2 → `story-impl-xhigh`, 3 or
   more → `story-impl-max`, otherwise (no `handoff:` line, an unreadable file, no number)
   `story-impl`. Skipped, and allowed, when the variant agents are not installed. Fails open.

Deny = hookSpecificOutput JSON on stdout, exit 0.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
from agent_roles import base_role  # noqa: E402

HOOK = "agent_dispatch.py"
DEFAULT_EDGES = {
    "story-impl": ["story-diagnose", "dev-preflight", "Explore"],
    "story-finalize": ["story-diagnose", "dev-preflight"],
    "story-rebase": ["story-diagnose", "dev-preflight"],
    "story-diagnose": ["dev-preflight"],
    "story-spec": ["Explore"],
}
LADDER = ((3, "story-impl-max"), (2, "story-impl-xhigh"))
HANDOFF_LINE = re.compile(r"^\s*[-*]?\s*\**handoff\**\s*[:=]\s*[\"'`]?([^\s\"'`]+)", re.MULTILINE | re.IGNORECASE)
ROUNDS_LINE = re.compile(r"^rounds_used:\s*(\d+)", re.MULTILINE)


def edges() -> dict[str, set[str]]:
    raw = g.cfg("guards.dispatch_edges", DEFAULT_EDGES)
    return {str(k): {str(v) for v in (vs or [])} for k, vs in (raw or {}).items()} if isinstance(raw, dict) else {}


def edge_check(payload: dict) -> str | None:
    if payload.get("tool_name") != "Agent" or not payload.get("agent_type"):
        return None
    caller = str(payload["agent_type"])
    ti = payload.get("tool_input") or {}
    callee = ti.get("subagent_type") or "general-purpose"
    allowed = edges().get(base_role(caller) or "", set())
    tag = f"(Hook: .workflow/hooks/guards/{HOOK}, edge allowlist)"
    if callee not in allowed:
        return (f"[workflow-hook] nested dispatch BLOCKED -- {caller} may not spawn {callee} "
                f"(allowed: {', '.join(sorted(allowed)) or 'none'}). {tag}")
    background = ti.get("run_in_background")
    if background is not False:
        return (f"[workflow-hook] nested dispatch BLOCKED -- {caller} must start {callee} with "
                f"run_in_background: false. A subagent cannot wait for an agent it started in the background. "
                f"Received run_in_background: {json.dumps(background)}. Re-issue the same call with the JSON "
                f"boolean false. {tag}")
    models = g.cfg("guards.dispatch_edge_models", {}) or {}
    model = models.get(f"{base_role(caller)}->{callee}") if isinstance(models, dict) else None
    if model and ti.get("model") != model:
        return (f"[workflow-hook] nested dispatch BLOCKED -- {caller} must start {callee} with "
                f"model: \"{model}\". Re-issue the same call with that model. {tag}")
    return None


def required_impl_variant(prompt: str) -> str:
    m = HANDOFF_LINE.search(prompt or "")
    if not m:
        return "story-impl"
    try:
        with open(m.group(1), encoding="utf-8", errors="replace") as fh:
            r = ROUNDS_LINE.search(fh.read())
    except OSError:
        return "story-impl"
    rounds = int(r.group(1)) if r else 0
    return next((name for floor, name in LADDER if rounds >= floor), "story-impl")


def variants_installed() -> bool:
    agents = g.wfconfig.repo_root() / ".claude" / "agents"
    oc = g.wfconfig.repo_root() / ".opencode" / "agents"
    return any((d / f"{name}.md").is_file() for d in (agents, oc) for _, name in LADDER)


def escalation_check(payload: dict) -> str | None:
    ti = payload.get("tool_input") or {}
    requested = str(ti.get("subagent_type") or "")
    if payload.get("tool_name") != "Agent" or base_role(requested) != "story-impl":
        return None
    if not variants_installed():
        return None
    required = required_impl_variant(str(ti.get("prompt") or ""))
    if requested == required:
        return None
    return (f"[workflow-hook] story-impl escalation -- this dispatch must use subagent_type={required} "
            f"(requested {requested}): the hand-off file's rounds_used sets the effort (2 -> story-impl-xhigh, "
            f"3+ -> story-impl-max, otherwise story-impl). Re-dispatch with that subagent_type and the same "
            f"prompt. (Hook: .workflow/hooks/guards/{HOOK})")


def evaluate(payload: dict) -> int:
    messages = []
    try:
        m = edge_check(payload)
    except Exception as exc:  # noqa: BLE001 -- the edge check fails closed
        m = f"[workflow-hook] nested dispatch BLOCKED -- the edge check raised {type(exc).__name__}: {exc}."
    if m:
        messages.append(m)
    try:
        m = escalation_check(payload)
    except Exception:  # noqa: BLE001 -- the escalation check fails open
        m = None
    if m:
        messages.append(m)
    return g.deny_json("\n".join(messages)) if messages else 0


if __name__ == "__main__":
    g.run("agent_dispatch", evaluate)
