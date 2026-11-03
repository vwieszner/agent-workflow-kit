#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""SubagentStart hook: inject one path-scoped rule into the code-writing story agents.

Claude Code loads a path rule (a `.claude/rules/*.md` file with a `paths:` frontmatter list) only
when Claude touches a file under the project root that matches its `paths:`. Story agents edit
files in their slot worktree, outside the project root, so no path rule ever loads for them. This
hook returns the rule as `additionalContext`, which Claude Code places in the subagent's context
before its first turn.

Config
    `config: path_rules.dir`    directory holding the rule files (default `.claude/rules`)
    `config: path_rules.names`  rule names (file stems) to inject; the installer registers one
                                SubagentStart command per name
    `config: path_rules.agents` SubagentStart matcher regex (default
                                `^story-(impl|impl-xhigh|impl-max|finalize|rebase)$`)

One rule per invocation: Claude Code persists a large hook output to a file and shows the agent
only a short preview, so each rule gets its own registered command. The rule is read from the main
checkout; an agent whose branch edits a rule file reads its worktree copy.

Fails open: a missing rule, a name not in `path_rules.names`, a bad argument or any error emits
nothing and exits 0.

Usage: inject_path_rules.py <rule-name>     (e.g. `testing` for <dir>/testing.md)
Hook protocol: SubagentStart JSON payload on stdin; stdout
`{"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ...}}`.
OpenCode: not wired (no SubagentStart event).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))


def rule_text(name: str) -> str:
    import wfconfig

    if not re.fullmatch(r"[\w-]+", name):
        return ""
    if name not in [str(n) for n in (wfconfig.get("path_rules.names", []) or [])]:
        return ""
    rel_dir = str(wfconfig.get("path_rules.dir", ".claude/rules")).rstrip("/")
    path = wfconfig.repo_root() / rel_dir / f"{name}.md"
    if not path.is_file():
        return ""
    body = path.read_text(encoding="utf-8").strip()
    return (f"Path rule `{rel_dir}/{name}.md` — applies to the files its `paths:` names, matched "
            f"relative to your worktree root:\n{body}\n")


def auto_text(payload: dict) -> str:
    """`--auto` (one registration, e.g. a plugin's): every `config: path_rules.names` rule, for an agent
    whose type (plugin scope removed) matches `config: path_rules.agents`."""
    import wfconfig
    from agent_roles import unscope

    pattern = str(wfconfig.get("path_rules.agents", r"^story-(impl|impl-xhigh|impl-max|finalize|rebase)$"))
    if not re.search(pattern, str(unscope(payload.get("agent_type")) or "")):
        return ""
    return "".join(rule_text(str(n)) for n in (wfconfig.get("path_rules.names", []) or []))


def main(argv: list[str]) -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        if argv == ["--auto"]:
            text = auto_text(payload if isinstance(payload, dict) else {})
        else:
            text = rule_text(argv[0]) if len(argv) == 1 else ""
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart",
                                                     "additionalContext": text}}))
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
