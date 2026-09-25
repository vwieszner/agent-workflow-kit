#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PostToolUse guard on Write: warn when a new script lands unregistered.
Switch: `config: guards.script_registration`.

Rule: a reusable host script (`config: guards.script_suffixes`, default .py/.ps1/.sh)
saved directly under a script dir (`config: guards.script_dirs`, default ["scripts"]) must
carry a doc header and a row in the project's script registry (`config: paths.scripts_index`).
Files in subdirectories (e.g. `scripts/tests/`) are exempt — they are registered as groups.

WARN, never deny: one-off throwaways are legitimately unregistered, and PostToolUse fires
after the write. Warning goes to stderr with exit 0; the agent registers the script in the
same turn or states that it is a deliberate throwaway. Fail open on anything unexpected.
"""
from __future__ import annotations

import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402


def evaluate(payload: dict) -> int:
    ti = payload.get("tool_input") or {}
    raw_path = ti.get("file_path") or ti.get("filePath") or ""
    if not raw_path:
        return 0
    suffixes = {s.lower() for s in g.cfg_list("guards.script_suffixes", [".py", ".ps1", ".sh"])}
    script_dirs = [str(d).strip("/\\") for d in g.cfg_list("guards.script_dirs", ["scripts"])]
    index_rel = str(g.cfg("paths.scripts_index", "scripts/INDEX.md"))

    path = pathlib.PurePosixPath(str(raw_path).replace("\\", "/"))
    if path.suffix.lower() not in suffixes:
        return 0
    parent = str(path.parent)
    hit = next((d for d in script_dirs if parent == d or parent.endswith("/" + d)), None)
    if hit is None:
        return 0
    name = path.name

    base = Path(payload.get("cwd") or g.wfconfig.repo_root())
    index_path = base / index_rel
    if not index_path.is_file():
        return 0
    if name in index_path.read_text(encoding="utf-8", errors="replace"):
        return 0
    sys.stderr.write(
        f"[workflow-hook] {hit}/{name} is not registered in {index_rel}.\n"
        "A REUSABLE host script needs a doc header and a registry row in the same turn. Add "
        "the row now, or, if this is a one-off throwaway, leave it unregistered deliberately "
        "and say so.\n"
    )
    return 0


if __name__ == "__main__":
    g.run("script_registration", evaluate)
