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

ESCALATED_ROLES = ("story-impl",)
EFFORT_SUFFIXES = ("-xhigh", "-max")


def base_role(agent_type: str | None) -> str | None:
    """`story-impl` for `story-impl-xhigh` / `story-impl-max`; any other value unchanged."""
    if not agent_type:
        return agent_type
    for suffix in EFFORT_SUFFIXES:
        if agent_type.endswith(suffix) and agent_type[: -len(suffix)] in ESCALATED_ROLES:
            return agent_type[: -len(suffix)]
    return agent_type
