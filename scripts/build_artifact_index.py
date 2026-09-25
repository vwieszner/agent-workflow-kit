#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Generate the unified artifact catalog (skills + agents + rules + hooks + scripts).

Part of the script-artifact-curation loop (.workflow/rules/script-artifact-curation.md).
Scans the installed artifact homes and writes one "find it fast" index to
`config: paths.artifact_catalog` (default .workflow/state/artifact-catalog.md):

    skills   .opencode/skills/*/SKILL.md and .claude/skills/*/SKILL.md (union, by name)
    agents   `config: curation.agent_source_dir` sources + .opencode/agents, .claude/agents
    rules    .workflow/rules/*.md
    hooks    .workflow/hooks/**/*.py
    scripts  .workflow/scripts/ (kit) and the directory of `config: paths.scripts_index`

Usage
-----
    uv run --no-project .workflow/scripts/build_artifact_index.py [--out PATH]

When to use
-----------
    After adding/removing a skill, agent, rule, hook or script (the `curate` skill runs it after
    each approval), or any time the catalog looks stale.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

SCRIPT_EXTS = (".py", ".ps1", ".sh", ".bat")
SKILL_DIRS = (".opencode/skills", ".claude/skills")
AGENT_DIRS = (".opencode/agents", ".claude/agents")


def _frontmatter_value(text: str, key: str) -> str:
    """First `key: value` of a leading YAML frontmatter block, else ''."""
    text = text.lstrip("﻿").lstrip()
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", block, re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
        val = val[1:-1]
    return val.strip()


def _docstring_first_line(text: str) -> str:
    m = re.search(r'"""(.*?)(?:\n|""")', text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _truncate(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _table(rows: list, cols=("Name", "Where", "Description")) -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for name, where, desc in rows:
        out.append(f"| `{name}` | {where} | {_truncate(desc).replace('|', chr(92) + '|')} |")
    return "\n".join(out)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def skills(root: Path) -> list:
    found: dict = {}
    for rel in SKILL_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            sk = d / "SKILL.md"
            if not sk.is_file():
                continue
            text = _read(sk)
            name = _frontmatter_value(text, "name") or d.name
            entry = found.setdefault(name, [[], _frontmatter_value(text, "description")])
            entry[0].append(rel)
    return [(n, ", ".join(w), desc) for n, (w, desc) in sorted(found.items())]


def agents(root: Path) -> list:
    found: dict = {}
    src = str(wfconfig.get("curation.agent_source_dir", ".workflow/agents"))
    for rel in (src, *AGENT_DIRS):
        base = root / rel
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*.md")):
            text = _read(f)
            name = _frontmatter_value(text, "name") or f.stem
            entry = found.setdefault(name, [[], _frontmatter_value(text, "description")])
            entry[0].append(rel + (" (source)" if rel == src else ""))
    return [(n, ", ".join(w), desc) for n, (w, desc) in sorted(found.items())]


def rules(root: Path) -> list:
    base = root / ".workflow" / "rules"
    if not base.is_dir():
        return []
    return [(f.name, ".workflow/rules", _frontmatter_value(_read(f), "description"))
            for f in sorted(base.glob("*.md")) if f.name != "INDEX.md"]


def hooks(root: Path) -> list:
    base = root / ".workflow" / "hooks"
    if not base.is_dir():
        return []
    return [(f.name, str(f.parent.relative_to(root)), _docstring_first_line(_read(f)))
            for f in sorted(base.rglob("*.py"))]


def _count_scripts(d: Path) -> int:
    return len([p for p in d.glob("*") if p.suffix in SCRIPT_EXTS]) if d.is_dir() else 0


def _link(out: Path, target: Path) -> str:
    return os.path.relpath(target, out.parent).replace(os.sep, "/")


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Regenerate the unified artifact catalog.")
    ap.add_argument("--out", default=None, help="output path (default: config paths.artifact_catalog)")
    a = ap.parse_args(argv)

    root = wfconfig.repo_root()
    out = Path(a.out).resolve() if a.out else wfconfig.path(
        "paths.artifact_catalog", ".workflow/state/artifact-catalog.md")
    scripts_index = wfconfig.path("paths.scripts_index", "scripts/INDEX.md")
    kit_index = root / ".workflow" / "scripts" / "INDEX.md"

    sk, ag, ru, ho = skills(root), agents(root), rules(root), hooks(root)
    n_kit = _count_scripts(root / ".workflow" / "scripts")
    n_proj = _count_scripts(scripts_index.parent)
    rule_doc = root / ".workflow" / "rules" / "script-artifact-curation.md"
    parts = [
        "# Artifact Catalog",
        "",
        "_Generated by `.workflow/scripts/build_artifact_index.py` — do not edit by hand._ Unified "
        "index of curated tooling: skills, subagents, rules, hooks, and scripts. New artifacts "
        "arrive via the curation loop (`retro` → `curate`); see "
        f"[`script-artifact-curation.md`]({_link(out, rule_doc)}).",
        "",
        f"Counts: {len(sk)} skills · {len(ag)} agents · {len(ru)} rules · {len(ho)} hooks · "
        f"{n_kit} workflow scripts · {n_proj} project scripts.",
        "",
        "## Skills", "", _table(sk) if sk else "_none_", "",
        "## Subagents", "", _table(ag) if ag else "_none_", "",
        "## Rules", "", _table(ru) if ru else "_none_", "",
        "## Hooks", "", _table(ho) if ho else "_none_", "",
        "## Scripts", "",
        f"- Workflow kit scripts: [`.workflow/scripts/INDEX.md`]({_link(out, kit_index)})",
        f"- Project scripts (authoritative registry): "
        f"[`{scripts_index.relative_to(root) if scripts_index.is_relative_to(root) else scripts_index}`]"
        f"({_link(out, scripts_index)})",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {out} ({len(sk)} skills, {len(ag)} agents, {len(ru)} rules, {len(ho)} hooks)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
