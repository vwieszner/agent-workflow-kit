#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Shared config loader for agent-workflow-kit scripts and hooks.

Every kit script and hook reads project-specific values through this module —
never hard-codes them. Config file: <repo>/.workflow/config.toml.

Repo root resolution (first hit wins):
  1. $WORKFLOW_REPO_ROOT
  2. `git rev-parse --show-toplevel` from the cwd — for a linked worktree this
     resolves to the MAIN checkout (via --git-common-dir) so every slot shares one
     config, registry and state dir.
  3. the cwd

Usage from a sibling script:
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  import wfconfig
  cfg = wfconfig.load()
  base = wfconfig.get("git.base_branch", "main")

CLI (for skills/prose that need a value):
  uv run --no-project .workflow/scripts/wfconfig.py get git.base_branch
  uv run --no-project .workflow/scripts/wfconfig.py ports 3
  uv run --no-project .workflow/scripts/wfconfig.py render "{compose} -p {stack} ps" --story-id 7-2
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

_MISSING = object()


@lru_cache(maxsize=1)
def repo_root() -> Path:
    env = os.environ.get("WORKFLOW_REPO_ROOT")
    if env:
        return Path(env).resolve()
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        if common:
            p = Path(common)
            # <main>/.git → <main>; bare-ish layouts fall back to show-toplevel
            if p.name == ".git":
                return p.parent.resolve()
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        if top:
            return Path(top).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd().resolve()


def workflow_dir() -> Path:
    return repo_root() / ".workflow"


def config_path() -> Path:
    return workflow_dir() / "config.toml"


def state_dir(*parts: str) -> Path:
    """.workflow/state/<parts...> — created on demand."""
    p = workflow_dir().joinpath("state", *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def get(key: str, default: Any = _MISSING) -> Any:
    """Dotted lookup: get("stack.ports") → dict. Raises KeyError if absent and no default."""
    node: Any = load()
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            if default is _MISSING:
                raise KeyError(f"config key '{key}' not set in {config_path()}")
            return default
    return node


def path(key: str, default: str = "") -> Path:
    """A config path value resolved against the repo root."""
    return repo_root() / str(get(key, default))


# --------------------------------------------------------------------- slots/stack

def stack_enabled() -> bool:
    return get("stack.runtime", "none") != "none"


def stack_name(story_id: str) -> str:
    return f"{get('slots.stack_prefix', 'story-')}{story_id}"


def branch_name(story_id: str) -> str:
    return f"{get('git.branch_prefix', 'story/')}{story_id}"


def worktrees_root() -> Path:
    raw = str(get("git.worktrees_root", "../{project}-worktrees"))
    raw = raw.replace("{project}", str(get("project.name", repo_root().name)))
    return (repo_root() / raw).resolve()


def worktree_path(story_id: str) -> Path:
    return worktrees_root() / story_id


def slot_ports(slot: int) -> dict[str, str]:
    """Port env vars for slot N: base + N * port_stride. Slot 0 → the bases."""
    stride = int(get("stack.port_stride", 100))
    ports = get("stack.ports", {}) or {}
    return {name: str(int(base) + slot * stride) for name, base in ports.items()}


def compose_env(slot: int) -> dict[str, str]:
    """os.environ + every port var for the slot — pass to EVERY compose call."""
    env = dict(os.environ)
    env.update(slot_ports(slot))
    return env


def render(template: str, **values: Any) -> str:
    """Fill {placeholders}. Adds {repo}, {compose} and, when slot given, port vars.

    Unknown placeholders are left intact (so a template can be rendered in stages).
    """
    ctx: dict[str, Any] = {
        "repo": str(repo_root()),
        "compose": str(get("stack.compose_cmd", "docker compose")),
        "project": str(get("project.name", repo_root().name)),
    }
    sid = values.get("story_id")
    if sid:
        ctx.setdefault("stack", stack_name(str(sid)))
        ctx.setdefault("branch", branch_name(str(sid)))
        ctx.setdefault("worktree", str(worktree_path(str(sid))))
    if "slot" in values and values["slot"] is not None:
        ctx.update(slot_ports(int(values["slot"])))
    ctx.update({k: v for k, v in values.items() if v is not None})

    class _Keep(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return template.format_map(_Keep({k: str(v) for k, v in ctx.items()}))


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="agent-workflow-kit config helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("key")
    p = sub.add_parser("ports")
    p.add_argument("slot", type=int)
    r = sub.add_parser("render")
    r.add_argument("template")
    r.add_argument("--story-id")
    r.add_argument("--slot", type=int)
    sub.add_parser("root")
    a = ap.parse_args(argv)

    if a.cmd == "get":
        try:
            v = get(a.key)
        except KeyError as e:
            print(e, file=sys.stderr)
            return 1
        print(v if not isinstance(v, (dict, list)) else repr(v))
    elif a.cmd == "ports":
        for k, v in slot_ports(a.slot).items():
            print(f"{k}={v}")
    elif a.cmd == "render":
        print(render(a.template, story_id=a.story_id, slot=a.slot))
    elif a.cmd == "root":
        print(repo_root())
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
