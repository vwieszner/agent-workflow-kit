#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Agent role of an agent type: an effort variant counts as its base agent.

`story-impl-xhigh` and `story-impl-max` are generated copies of `story-impl` that differ only in
`effort` (`gen_agent_effort_variants.py`). Every check keyed on an agent type — the phase gate, the
nested-dispatch allowlist, the source-read and graph guards, the ledger's phase position, cost
attribution — maps the type through `base_role` first, so a variant is gated, guarded and counted
exactly as its base agent.

Stdlib only.
"""
from __future__ import annotations

import re

ESCALATED_ROLES = ("story-impl",)
EFFORT_SUFFIXES = ("-xhigh", "-max")


_SCOPED = re.compile(r"^[a-z0-9][a-z0-9-]*:([^:]+)$")


def unscope(name):
    """`story-impl` for a plugin-scoped `<plugin>:story-impl`; any other value unchanged. Installed as a
    Claude Code plugin, the kit's agents, skills and workflows carry the plugin name as a prefix."""
    if isinstance(name, str):
        m = _SCOPED.match(name.strip())
        if m:
            return m.group(1)
    return name


def normalize_payload(payload):
    """A hook payload with plugin-scoped agent, subagent and skill names unscoped, in place."""
    if not isinstance(payload, dict):
        return payload
    if "agent_type" in payload:
        payload["agent_type"] = unscope(payload["agent_type"])
    ti = payload.get("tool_input")
    if isinstance(ti, dict):
        for key in ("subagent_type", "skill"):
            if key in ti:
                ti[key] = unscope(ti[key])
    return payload


def base_role(agent_type: str | None) -> str | None:
    """`story-impl` for `story-impl-xhigh` / `story-impl-max` (also plugin-scoped); any other value unscoped."""
    if not agent_type:
        return agent_type
    agent_type = unscope(agent_type)
    for suffix in EFFORT_SUFFIXES:
        if agent_type.endswith(suffix) and agent_type[: -len(suffix)] in ESCALATED_ROLES:
            return agent_type[: -len(suffix)]
    return agent_type
