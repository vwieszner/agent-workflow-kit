#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""SessionStart entry: every session-start injector in one process.

Usage
    session_start.py <startup|clear|compact|resume> [--tool claude|opencode] [--plugin]   (default claude)

Wiring
    Claude Code: one `SessionStart` matcher group per source, each running this script with
    that source as its argument.

Emits, in this order:
  * compact / resume — the code-navigation directive (`inject_symbol_nav_directive`; first, so
    loading the deferred tools is the literal first action), then the in-flight story positions
    (`inject_story_ledger`);
  * startup / clear  — the curation nudge (`check_pending_proposals`), then the in-flight story
    positions.
  * `--plugin` (the kit installed as a Claude Code plugin, which cannot load CLAUDE.md): first, on
    every source, the plugin's install path (what `${CLAUDE_PLUGIN_ROOT}` means in kit files read with
    Read), the working rules (`AGENTS.md` at the plugin root) and the project's
    `.workflow/AGENTS.local.md`; without `.workflow/config.toml` a line naming the setup skill.
Each part comes from its module's `text()`; each module's own `main()` still works on its own.
A part that raises is skipped. Never breaks a session start; always exits 0.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARTS = {
    "compact": ("inject_symbol_nav_directive", "inject_story_ledger"),
    "resume": ("inject_symbol_nav_directive", "inject_story_ledger"),
    "startup": ("check_pending_proposals", "inject_story_ledger"),
    "clear": ("check_pending_proposals", "inject_story_ledger"),
}


def part(name: str, tool: str) -> str:
    try:
        spec = importlib.util.spec_from_file_location(f"_ss_{name}", HERE / f"{name}.py")
        if not spec or not spec.loader:
            return ""
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = mod.text(tool) if name == "inject_symbol_nav_directive" else mod.text()
    except Exception:  # noqa: BLE001
        return ""
    return out if not out or out.endswith("\n") else out + "\n"


def plugin_rules() -> str:
    """The working rules a plugin install injects in place of CLAUDE.md imports."""
    root = HERE.parent.parent
    out = [f"agent-workflow-kit plugin root: {root}\n"
           f"In kit files and kit messages (skills' step files, rules, hook and script output), "
           f"`${{CLAUDE_PLUGIN_ROOT}}` and `<plugin-root>` mean {root}; shell commands must spell that "
           f"path out (the variable is not set in Bash).\n"]
    try:
        import wfconfig
        project = wfconfig.repo_root()
    except Exception:  # noqa: BLE001
        project = Path.cwd()
    if not (project / ".workflow" / "config.toml").is_file():
        out.append("This project has no .workflow/config.toml: invoke the `agent-workflow-kit:setup` skill "
                   "before any pipeline skill.\n")
    for path in (root / "AGENTS.md", project / ".workflow" / "AGENTS.local.md"):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            out.append(f"\n<!-- {path.name} -->\n{text}\n")
    return "".join(out)


def compose(source: str, tool: str = "claude", plugin: bool = False) -> str:
    head = plugin_rules() if plugin and source in PARTS else ""
    return head + "".join(part(name, tool) for name in PARTS.get(source, ()))


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    tool = "claude"
    if "--tool" in argv:
        i = argv.index("--tool")
        tool = argv[i + 1] if i + 1 < len(argv) else tool
        argv = argv[:i] + argv[i + 2:]
    plugin = "--plugin" in argv
    argv = [a for a in argv if a != "--plugin"]
    source = argv[0] if argv else ""
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent.parent / "scripts"))
    sys.stdout.write(compose(source, tool, plugin))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # noqa: BLE001
        sys.exit(0)
