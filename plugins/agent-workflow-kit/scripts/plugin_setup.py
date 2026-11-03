#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Set up a project for the agent-workflow-kit Claude Code plugin (the plugin's `setup` skill runs this).

    uv run --no-project <plugin root>/scripts/plugin_setup.py [--dry-run]

A plugin cannot write into a project or ship permission rules, so this script creates what the
`install.py` route would have created, in the current git checkout:

  .workflow/config.toml               from the plugin's workflow.config.example.toml — only if absent
  .workflow/AGENTS.local.md           project rules — only if absent
  .workflow/rules/local/INDEX.md      project rules index — only if absent
  .workflow/.gitignore                `state/` and `__pycache__/` ignored
  .claude/settings.json               the kit's `permissions.deny` rules appended (existing rules kept)
  slot registry                       `slot_registry.py init` (config: paths.slot_registry)

Existing files are never overwritten. When `.claude/settings.json` still wires the kit's hooks from an
`install.py` install (`/.workflow/hooks/` commands), every guard would run twice: the script names those
entries and leaves them for the owner to remove.

Exit codes: 0 done (or nothing to do); 1 an error (a malformed settings file, no git checkout).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
DENY_FILE = KIT_ROOT / "setup" / "permissions.deny.json"
LOCAL_FILES = {
    "AGENTS.local.md": "# Project rules\n\nProject-specific additions to the kit's working rules. "
                       "Owned by this project; the kit never overwrites it.\n",
    "rules/local/INDEX.md": "# Project rules index\n\n| Rule | Scope |\n|------|-------|\n",
    ".gitignore": "state/\n__pycache__/\n",
}


def project_root() -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out).resolve() if out else None


def write_once(path: Path, text: str, dry: bool, log: list[str]) -> None:
    if path.exists():
        log.append(f"keep   {path} (exists)")
        return
    log.append(f"create {path}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def merge_deny(settings: Path, deny: list[str], dry: bool, log: list[str]) -> list[str]:
    """Append the missing deny rules; return the kit hook commands an install.py install left wired."""
    cur: dict = {}
    if settings.is_file():
        cur = json.loads(settings.read_text(encoding="utf-8"))
    perms = dict(cur.get("permissions") or {})
    have = list(perms.get("deny") or [])
    missing = [d for d in deny if d not in have]
    if missing:
        log.append(f"update {settings}: {len(missing)} permissions.deny rule(s) added")
        perms["deny"] = have + missing
        cur["permissions"] = perms
        if not dry:
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        log.append(f"keep   {settings} (every kit deny rule present)")
    wired = []
    for groups in (cur.get("hooks") or {}).values():
        for grp in groups or []:
            for h in grp.get("hooks", []) or []:
                cmd = str(h.get("command", ""))
                if "/.workflow/hooks/" in cmd:
                    wired.append(cmd)
    return wired


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    root = project_root()
    if root is None:
        print("ERROR: not inside a git checkout — run from the project's repository")
        return 1
    log: list[str] = []
    wf = root / ".workflow"
    example = KIT_ROOT / "workflow.config.example.toml"
    write_once(wf / "config.toml", example.read_text(encoding="utf-8"), a.dry_run, log)
    for rel, text in LOCAL_FILES.items():
        write_once(wf / rel, text, a.dry_run, log)
    try:
        deny = json.loads(DENY_FILE.read_text(encoding="utf-8")) if DENY_FILE.is_file() else []
        wired = merge_deny(root / ".claude" / "settings.json", deny, a.dry_run, log)
    except (OSError, ValueError) as exc:
        print(f"ERROR: .claude/settings.json could not be updated: {exc}")
        return 1
    if not a.dry_run:
        rc = subprocess.run([sys.executable, str(KIT_ROOT / "scripts" / "slot_registry.py"), "init"],
                            cwd=root).returncode
        log.append(f"slot registry init: exit {rc}")
    print(("DRY RUN — nothing written\n" if a.dry_run else "") + "\n".join(log))
    if wired:
        print(f"\nWARNING: .claude/settings.json still runs {len(wired)} kit hook command(s) from an install.py "
              "install (`.workflow/hooks/...`). With the plugin enabled every guard would run twice: remove "
              "those entries, or use one of the two install routes.")
    print("\nNext: edit .workflow/config.toml (project, git, paths, tests, stack); see the plugin README.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
