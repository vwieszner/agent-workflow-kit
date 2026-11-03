#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(mcp__<browser server>__.*) guard: a story agent's browser file outputs go into its own worktree.

A browser MCP tool that saves a file resolves a relative `filename` against the MCP's workspace root, the main
checkout; a story's files belong in the story's worktree. For a subagent whose dispatch prompt holds a
`worktree_path: <path>` line (found through its transcript, `_transcript.agent_worktree`) this denies:
  * `browser_take_screenshot` without a `filename`;
  * a `filename` on `browser_take_screenshot`, `browser_snapshot`, `browser_console_messages` or
    `browser_network_requests` that is not an absolute path under `<worktree_path>/.playwright-mcp/`;
  * a `browser_run_code_unsafe` `code` whose `uivShot(` / `uivSecondShot(` call or `.screenshot({ path })` names a
    string-literal path not under `<worktree_path>/.playwright-mcp/`.
Not checked: the main session; a subagent whose dispatch names no worktree; a tool with no transcript file
(OpenCode); a path `code` builds at run time (a template literal holding `${`). The bar is mistake-proofing, not
a sandbox. The server name is `config: browser.mcp_server` (default playwright). Switch:
`config: guards.playwright_paths`. Fails open. Deny: hookSpecificOutput JSON, exit 0.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
import _paths  # noqa: E402
from _transcript import agent_worktree  # noqa: E402

HOOK = "playwright_paths.py"
WRITE_TOOLS = {"browser_take_screenshot", "browser_snapshot", "browser_console_messages", "browser_network_requests"}
CODE_PATHS = re.compile(
    r"""(?:uivShot|uivSecondShot)\s*\(\s*(['"`])(?P<a>[^'"`]*)\1"""
    r"""|\.screenshot\s*\(\s*\{[^}]*?\bpath\s*:\s*(['"`])(?P<b>[^'"`]*)\3""")


def inside(path: str, root: str) -> bool:
    return _paths.is_absolute(path) and not _paths.has_dot_segment(path) and _paths.under(path, root)


def check(payload: dict) -> str | None:
    name = str(payload.get("tool_name") or "")
    prefix = f"mcp__{g.cfg('browser.mcp_server', 'playwright')}__"
    if not name.startswith(prefix) or not payload.get("agent_type"):
        return None
    tool = name[len(prefix):]
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool not in WRITE_TOOLS and not (tool == "browser_run_code_unsafe" and isinstance(args.get("code"), str)):
        return None
    worktree = agent_worktree(payload, "playwright-paths")
    if not worktree:
        return None
    root = worktree + "/.playwright-mcp/"
    form = f"{root}round-<round>/<screen>-<state>.png"
    if tool in WRITE_TOOLS:
        filename = args.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            if tool != "browser_take_screenshot":
                return None
            return (f"[playwright-paths] {tool} without a filename saves into the MCP's output dir, outside your "
                    f"worktree. Pass the absolute filename {form}.")
        if not inside(filename, root):
            return (f"[playwright-paths] {filename!r} is not inside your worktree's {root}: a story's files go "
                    f"into its own worktree, never the main checkout. Pass the absolute filename {form}.")
        return None
    for m in CODE_PATHS.finditer(args["code"]):
        target = m.group("a") if m.group("a") is not None else m.group("b")
        if "${" in target:
            continue
        if not inside(target, root):
            return (f"[playwright-paths] the code saves {target!r}, which is not inside your worktree's {root}: a "
                    f"story's files go into its own worktree. Use the absolute path {form}.")
    return None


def evaluate(payload: dict) -> int:
    reason = check(payload)
    return g.deny_json(f"{reason} (Hook: .workflow/hooks/guards/{HOOK})") if reason else 0


if __name__ == "__main__":
    g.run("playwright_paths", evaluate)
