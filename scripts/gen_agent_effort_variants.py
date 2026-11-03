#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Generate the effort variants of story-impl from its agent source file, and detect drift.

`story-impl-xhigh.md` and `story-impl-max.md` are `story-impl.md` with three frontmatter lines changed:
`name`, `effort`, and `description` (prefixed with when the variant is dispatched). Body, tier, tools and
per-tool overrides are copied verbatim, so the variant behaves as story-impl at a higher effort. The dispatch
gate (`.workflow/hooks/guards/agent_dispatch.py`) picks the variant from the hand-off file's `rounds_used`;
every other check maps a variant to its base role (`agent_roles.base_role`).

Modes
-----
    uv run --no-project scripts/gen_agent_effort_variants.py                 write both variants
    uv run --no-project scripts/gen_agent_effort_variants.py --check         exit 1 if a variant differs
    ... --agents-dir <dir>   the agent source dir (default: the kit's `agents/` beside this script's parent,
                             else `config: curation.agent_source_dir`)

Edit `story-impl.md`, then run this script; the kit's tests fail on drift.

Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE = "story-impl"
VARIANTS = {
    "xhigh": "Escalated story-impl (effort xhigh): dispatched only when the hand-off file named by the "
             "dispatch has rounds_used 2; the dispatch gate denies it otherwise.",
    "max": "Escalated story-impl (effort max): dispatched only when the hand-off file named by the "
           "dispatch has rounds_used 3 or more; the dispatch gate denies it otherwise.",
}


def default_agents_dir() -> Path:
    kit = Path(__file__).resolve().parent.parent / "agents"
    if (kit / f"{BASE}.md").is_file():
        return kit
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import wfconfig
    return wfconfig.repo_root() / str(wfconfig.get("curation.agent_source_dir", ".workflow/agents"))


def render(base_text: str, effort: str) -> str:
    """The variant file for `effort`, built from the base agent file's text."""
    end = base_text.find("\n---", 3)
    if not base_text.startswith("---") or end == -1:
        raise ValueError(f"{BASE}.md has no frontmatter")
    head, body = base_text[:end], base_text[end:]
    subs = [(r"^name:[^\r\n]*", f"name: {BASE}-{effort}"),
            (r"^effort:[^\r\n]*", f"effort: {effort}"),
            (r"^description:\s*", f"description: {VARIANTS[effort]} ")]
    for pattern, repl in subs:
        head, n = re.subn(pattern, lambda _m, r=repl: r, head, count=1, flags=re.MULTILINE)
        if n != 1:
            raise ValueError(f"{BASE}.md frontmatter has no line matching {pattern!r}")
    return head + body


def drift(agents_dir: Path) -> list[str]:
    """One line per variant file that is missing or differs from what `render` produces."""
    base = (agents_dir / f"{BASE}.md").read_text(encoding="utf-8")
    out = []
    for effort in VARIANTS:
        path = agents_dir / f"{BASE}-{effort}.md"
        if not path.exists() or path.read_text(encoding="utf-8") != render(base, effort):
            out.append(f"{path.name} is not the generated copy of {BASE}.md (run gen_agent_effort_variants.py)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--agents-dir", type=Path, default=None)
    a = ap.parse_args(argv)
    agents_dir = a.agents_dir or default_agents_dir()
    if a.check:
        problems = drift(agents_dir)
        for p in problems:
            print(p)
        return 1 if problems else 0
    base = (agents_dir / f"{BASE}.md").read_text(encoding="utf-8")
    for effort in VARIANTS:
        path = agents_dir / f"{BASE}-{effort}.md"
        path.write_text(render(base, effort), encoding="utf-8", newline="")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
