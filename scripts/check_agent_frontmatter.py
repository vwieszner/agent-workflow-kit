#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Agent model/effort policy: each agent's frontmatter is the one source.

Checks one checkout:
  * every rendered Claude Code agent (`.claude/agents/*.md`) sets a non-empty `model` and, when
    it sets `effort`, one of EFFORTS;
  * every kit-format agent source (`config: curation.agent_source_dir`, default
    `.workflow/agents/*.md`) sets `tier` to deep | standard | fast and, when set, a valid `effort`;
  * every `.claude/workflows/*.js` that declares `const EFFORT = {...}` names only existing agents
    and gives each exactly its frontmatter `effort` (a workflow passes effort per `agent()` call,
    and this check keeps the two equal);
  * when `gen_agent_effort_variants.py` is installed, the effort variants are the copies it
    generates (`drift(root)`).

Modes
-----
    uv run --no-project .workflow/scripts/check_agent_frontmatter.py [--root <checkout>]
        Print each violation; exit 1 if any, else 0. Default root: the config repo root.
    uv run --no-project .workflow/scripts/check_agent_frontmatter.py --hook
        PostToolUse(Write|Edit) entry. Reads the payload on stdin; when the written file is an
        agent or workflow file of a checkout, checks that checkout and reports violations as a
        PostToolUse block decision. Any other file, or any unexpected condition: silent, exit 0.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

EFFORTS = ("low", "medium", "high", "xhigh", "max")
TIERS = ("deep", "standard", "fast")

_FIELD = re.compile(r"^(model|effort|tier):\s*(\S+)\s*(?:#.*)?$", re.MULTILINE)
_EFFORT_MAP = re.compile(r"const EFFORT = \{(.*?)\}", re.DOTALL)
_PAIR = re.compile(r"""['"]([\w-]+)['"]\s*:\s*['"](\w+)['"]""")


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def source_dir(root: Path) -> Path:
    try:
        rel = str(wfconfig.get("curation.agent_source_dir", ".workflow/agents"))
    except Exception:  # noqa: BLE001
        rel = ".workflow/agents"
    return root / rel


def check(root: Path) -> list[str]:
    problems: list[str] = []
    efforts: dict[str, str] = {}
    for f in sorted((root / ".claude" / "agents").glob("*.md")):
        fields = dict(_FIELD.findall(frontmatter(f.read_text(encoding="utf-8"))))
        if not fields.get("model"):
            problems.append(f".claude/agents/{f.name}: no model")
        if "effort" in fields and fields["effort"] not in EFFORTS:
            problems.append(f".claude/agents/{f.name}: effort {fields['effort']!r} is not one of {', '.join(EFFORTS)}")
        efforts[f.stem] = fields.get("effort", "")
    src = source_dir(root)
    for f in sorted(src.glob("*.md")) if src.is_dir() else []:
        fields = dict(_FIELD.findall(frontmatter(f.read_text(encoding="utf-8"))))
        where = f.relative_to(root).as_posix()
        if fields.get("tier") not in TIERS:
            problems.append(f"{where}: tier {fields.get('tier')!r} is not one of {', '.join(TIERS)}")
        if "effort" in fields and fields["effort"] not in EFFORTS:
            problems.append(f"{where}: effort {fields['effort']!r} is not one of {', '.join(EFFORTS)}")
    for f in sorted((root / ".claude" / "workflows").glob("*.js")):
        m = _EFFORT_MAP.search(f.read_text(encoding="utf-8"))
        if not m:
            continue
        for agent, effort in _PAIR.findall(m.group(1)):
            if agent not in efforts:
                problems.append(f"{f.name}: EFFORT names {agent!r}, which has no .claude/agents/{agent}.md")
            elif effort != efforts[agent]:
                problems.append(f"{f.name}: EFFORT[{agent!r}] is {effort!r}; {agent}.md frontmatter says "
                                f"{efforts[agent]!r} (change both together)")
    try:
        import gen_agent_effort_variants as variants  # installed by the kit when the variants exist
    except ImportError:
        variants = None
    if variants is not None and hasattr(variants, "drift"):
        try:
            problems.extend(variants.drift(root))
        except Exception as exc:  # noqa: BLE001 -- a broken generator is itself a finding
            problems.append(f"gen_agent_effort_variants.drift failed: {type(exc).__name__}: {exc}")
    return problems


def _root_for(path: Path) -> Path | None:
    parts = [p.lower() for p in path.parts]
    if len(parts) >= 3 and parts[-3] == ".claude" and (parts[-2], path.suffix) in (("agents", ".md"),
                                                                                   ("workflows", ".js")):
        return path.parents[2]
    src = source_dir(wfconfig.repo_root())
    try:
        if path.suffix == ".md" and path.resolve().parent == src.resolve():
            return wfconfig.repo_root()
    except OSError:
        return None
    return None


def hook() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().lstrip(b"\xef\xbb\xbf").decode("utf-8") or "{}")
        path = Path(str((payload.get("tool_input") or {}).get("file_path") or ""))
        root = _root_for(path)
        if root is None:
            return 0
        problems = check(root)
    except Exception:  # noqa: BLE001
        return 0
    if problems:
        print(json.dumps({"decision": "block", "reason": (
            "Agent model/effort policy violated (.workflow/scripts/check_agent_frontmatter.py):\n- "
            + "\n- ".join(problems))}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--hook", action="store_true", help="run as the PostToolUse hook")
    ap.add_argument("--root", type=Path, default=None)
    a = ap.parse_args(argv)
    if a.hook:
        return hook()
    problems = check(a.root or wfconfig.repo_root())
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
