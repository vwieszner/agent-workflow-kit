#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Install agent-workflow-kit into a target project, or render one agent.

Usage:
  python3 install.py install --target <repo> --tool opencode|claude|both [--force] [--dry-run]
  python3 install.py render-agent <agent.md> --tool claude|opencode [--out <file>] [--target <repo>]

`install` (run from the kit checkout):
  .workflow/scripts, .workflow/hooks, .workflow/rules  ← kit-managed, always refreshed
  .workflow/config.toml                               ← only if absent
  .workflow/AGENTS.md                                 ← kit-managed; wired into opencode.json
                                                        `instructions` and/or a CLAUDE.md @import
  .workflow/AGENTS.local.md, .workflow/rules/local/   ← project-owned; created once, never touched
  skills   → .claude/skills (claude|both — OpenCode reads .claude/skills too)
             .opencode/skills (opencode only)
  agents   → rendered into .claude/agents and/or .opencode/agents
  hooks    → .claude/settings.json hooks merged (claude|both)
             .opencode/plugins/*.ts + .opencode/package.json deps (opencode|both)
  Existing skills/agents with the same name are SKIPPED (reported) unless --force.

`render-agent` (also available installed as .workflow/scripts/install.py): renders a kit-source
agent (DESIGN.md §5) for one tool, using the target's .workflow/config.toml for models and MCP
server names. Prints to stdout unless --out is given.

Exit codes: 0 ok, 1 error, 2 bad agent source.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parent
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
# Always-loaded instruction files (OpenCode `instructions`, Claude CLAUDE.md @imports).
INSTRUCTION_FILES = [".workflow/AGENTS.md", ".workflow/AGENTS.local.md"]
LOCAL_FILES = {
    "AGENTS.local.md": "# Project rules\n\nProject-specific additions to .workflow/AGENTS.md. "
                       "Owned by this project; the kit installer never overwrites it.\n",
    "rules/local/INDEX.md": "# Project rules index\n\n| Rule | Scope |\n|------|-------|\n",
}


# ------------------------------------------------------------------ config access

def _load_wfconfig(target: Path | None):
    if target is not None:
        os.environ["WORKFLOW_REPO_ROOT"] = str(target.resolve())
    for cand in (KIT / "scripts", KIT):  # kit checkout, or installed .workflow/scripts
        if (cand / "wfconfig.py").is_file():
            sys.path.insert(0, str(cand))
            break
    import wfconfig  # noqa: E402

    wfconfig.repo_root.cache_clear()
    wfconfig.load.cache_clear()
    return wfconfig


def _defaults() -> dict[str, Any]:
    """Example-config values, used when the target has no config yet."""
    import tomllib

    ex = KIT / "workflow.config.example.toml"
    if ex.is_file():
        return tomllib.loads(ex.read_text(encoding="utf-8"))
    return {}


def _cfg(wf, key: str, default: Any) -> Any:
    val = wf.get(key, None)
    if val is not None:
        return val
    node: Any = _defaults()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# ------------------------------------------------------------------ frontmatter

def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'" and raw[-1] == raw[0] and len(raw) >= 2:
        body = raw[1:-1]
        return json.loads(raw) if raw[0] == '"' else body.replace("''", "'")
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    if raw.startswith("{") and raw.endswith("}"):
        out: dict[str, Any] = {}
        inner = raw[1:-1].strip()
        for pair in filter(None, (p.strip() for p in inner.split(","))):
            k, _, v = pair.partition(":")
            out[k.strip().strip("\"'")] = _scalar(v)
        return out
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw


def parse_agent(text: str) -> tuple[dict[str, Any], str]:
    """Parse the restricted frontmatter of DESIGN.md §5 (scalars, inline lists/maps,
    one level of indented maps)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing frontmatter opening '---'")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("missing frontmatter closing '---'") from None
    meta: dict[str, Any] = {}
    current: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t" and current is not None:
            k, _, v = line.strip().partition(":")
            if not isinstance(meta.get(current), dict):
                meta[current] = {}
            meta[current][k.strip().strip("\"'")] = _scalar(v)
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        if v.strip() == "":
            meta[k] = {}
            current = k
        else:
            meta[k] = _scalar(v)
            current = None
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def _y(v: Any) -> str:
    """YAML scalar via JSON (a JSON string/number/bool is valid YAML)."""
    return json.dumps(v, ensure_ascii=False)


# ------------------------------------------------------------------ rendering

CLAUDE_TOOLS = {
    "read": ["Read"], "grep": ["Grep"], "glob": ["Glob"], "edit": ["Edit"], "write": ["Write"],
    "bash": ["Bash"], "task": ["Agent"], "skill": ["Skill"], "web": ["WebFetch", "WebSearch"],
    "monitor": ["Monitor", "TaskStop", "SendMessage"],
}


def render_agent(src: Path, tool: str, wf) -> str:
    meta, body = parse_agent(src.read_text(encoding="utf-8"))
    for req in ("name", "description", "tools"):
        if req not in meta:
            raise ValueError(f"{src.name}: frontmatter lacks '{req}'")
    caps = [str(c) for c in (meta.get("tools") or [])]
    unknown = set(caps) - set(CLAUDE_TOOLS) - {"graph", "docs"}
    if unknown:
        raise ValueError(f"{src.name}: unknown capabilities {sorted(unknown)}")
    readonly = bool(meta.get("readonly", False))
    if readonly:
        caps = [c for c in caps if c not in ("edit", "write")]
    tier = str(meta.get("tier", "standard"))
    graph_on = "graph" in caps and _cfg(wf, "graph.tool", "none") != "none"
    graph_srv = str(_cfg(wf, "graph.mcp_server", "codebase-memory-mcp"))
    docs_srv = str(_cfg(wf, "docs.mcp_server", "") or "")
    docs_on = "docs" in caps and bool(docs_srv)

    out = ["---"]
    if tool == "claude":
        names: list[str] = []
        for c in caps:
            names += CLAUDE_TOOLS.get(c, [])
            if c == "bash" and sys.platform == "win32":
                names.append("PowerShell")
        if graph_on:
            names += ["ToolSearch", f"mcp__{graph_srv}"]
        if docs_on:
            names += ["ToolSearch", f"mcp__{docs_srv}"]
        names = list(dict.fromkeys(names))
        if not names:
            raise ValueError(f"{src.name}: renders to an empty Claude tool list "
                             "(an omitted list would grant ALL tools)")
        out += [f"name: {meta['name']}", f"description: {_y(str(meta['description']))}",
                f"model: {_cfg(wf, f'models.claude.{tier}', 'sonnet')}"]
        if meta.get("effort"):
            out.append(f"effort: {meta['effort']}")
        out.append(f"tools: {', '.join(names)}")
        for k, v in (meta.get("claude") or {}).items():
            out.append(f"{k}: {v if not isinstance(v, str) else v}")
    elif tool == "opencode":
        def perm(on: bool) -> str:
            return "allow" if on else "deny"
        out += [f"description: {_y(str(meta['description']))}",
                f"mode: {meta.get('mode', 'subagent')}",
                f"model: {_cfg(wf, f'models.opencode.{tier}', 'anthropic/claude-sonnet-5')}",
                "permission:",
                f"  read: {perm('read' in caps)}",
                f"  list: {perm('read' in caps or 'glob' in caps)}",
                f"  grep: {perm('grep' in caps)}",
                f"  glob: {perm('glob' in caps)}",
                f"  edit: {perm(('edit' in caps or 'write' in caps) and not readonly)}",
                f"  bash: {perm('bash' in caps)}",
                f"  task: {perm('task' in caps)}",
                f"  skill: {perm('skill' in caps)}",
                f"  webfetch: {perm('web' in caps)}",
                f"  websearch: {perm('web' in caps)}",
                "tools:",
                f"  {_y(graph_srv + '*')}: {str(graph_on).lower()}"]
        if docs_srv:
            out.append(f"  {_y(docs_srv + '*')}: {str(docs_on).lower()}")
        for k, v in (meta.get("opencode") or {}).items():
            out.append(f"{k}: {v}")
    else:
        raise ValueError(f"unknown tool {tool!r}")
    out.append("---")
    return "\n".join(out) + "\n\n" + body.rstrip() + "\n"


# ------------------------------------------------------------------ install helpers

class Installer:
    def __init__(self, target: Path, tool: str, force: bool, dry: bool):
        self.t, self.tool, self.force, self.dry = target, tool, force, dry
        self.log: list[str] = []
        self.skipped: list[str] = []

    def note(self, msg: str) -> None:
        self.log.append(msg)
        print(("DRY  " if self.dry else "") + msg)

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.t)) if p.is_relative_to(self.t) else str(p)

    def copytree(self, src: Path, dst: Path) -> None:
        if not src.is_dir():
            return
        self.note(f"sync  {self.rel(dst)}/")
        if not self.dry:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=IGNORE)

    def write(self, dst: Path, text: str, *, owned: bool) -> None:
        if dst.exists() and not owned and not self.force:
            self.skipped.append(self.rel(dst))
            self.note(f"SKIP  {self.rel(dst)} (exists; --force to overwrite)")
            return
        self.note(f"write {self.rel(dst)}")
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")

    def json_merge(self, dst: Path, patch: dict[str, Any], merge) -> None:
        cur: dict[str, Any] = {}
        if dst.is_file():
            try:
                cur = json.loads(dst.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise SystemExit(f"cannot merge into {dst}: invalid JSON ({e})")
        new = merge(cur, patch)
        if new != cur:
            self.write(dst, json.dumps(new, indent=2, ensure_ascii=False) + "\n", owned=True)


def _merge_missing(cur: dict, patch: dict) -> dict:
    """Deep merge adding keys the target lacks; lists get missing items appended."""
    out = dict(cur)
    for k, v in patch.items():
        if k not in out:
            out[k] = v
        elif isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge_missing(out[k], v)
        elif isinstance(out[k], list) and isinstance(v, list):
            out[k] = out[k] + [x for x in v if x not in out[k]]
    return out


def _merge_claude_hooks(cur: dict, patch: dict) -> dict:
    """Append each hook entry whose command is not already wired for that event+matcher."""
    out = dict(cur)
    hooks = dict(out.get("hooks", {}))
    for event, groups in patch.get("hooks", {}).items():
        existing = list(hooks.get(event, []))
        for grp in groups:
            match = next((g for g in existing if g.get("matcher") == grp.get("matcher")), None)
            if match is None:
                existing.append(grp)
                continue
            have = {h.get("command") for h in match.get("hooks", [])}
            match["hooks"] = match.get("hooks", []) + [
                h for h in grp.get("hooks", []) if h.get("command") not in have]
        hooks[event] = existing
    out["hooks"] = hooks
    return out


def install(target: Path, tool: str, force: bool, dry: bool) -> int:
    if not (KIT / "skills").is_dir():
        print("install must run from the kit checkout (skills/ not found next to install.py)")
        return 1
    if not target.is_dir():
        print(f"target {target} is not a directory")
        return 1
    ins = Installer(target.resolve(), tool, force, dry)
    t = ins.t
    use_claude, use_oc = tool in ("claude", "both"), tool in ("opencode", "both")

    # 1. kit-managed runtime
    wfd = t / ".workflow"
    ins.copytree(KIT / "scripts", wfd / "scripts")
    ins.copytree(KIT / "hooks", wfd / "hooks")
    ins.copytree(KIT / "rules", wfd / "rules")
    if not dry:
        shutil.copy2(KIT / "install.py", wfd / "scripts" / "install.py")
    ins.write(wfd / "AGENTS.md", (KIT / "AGENTS.md").read_text(encoding="utf-8"), owned=True)
    ins.write(wfd / ".gitignore", "state/\n", owned=True)
    # project-owned: created once, never overwritten (curate promotes into these)
    for rel, text in LOCAL_FILES.items():
        if not (wfd / rel).exists():
            ins.write(wfd / rel, text, owned=True)
    cfg = wfd / "config.toml"
    if cfg.exists():
        ins.note("keep  .workflow/config.toml (exists)")
    else:
        ins.write(cfg, (KIT / "workflow.config.example.toml").read_text(encoding="utf-8"),
                  owned=True)

    wf = _load_wfconfig(t)

    # 2. always-loaded instructions
    if use_oc:
        ins.json_merge(t / "opencode.json",
                       {"$schema": "https://opencode.ai/config.json",
                        "instructions": INSTRUCTION_FILES + [".workflow/memory/*.md"]},
                       _merge_missing)
        frag = KIT / "adapters" / "opencode" / "opencode.json.fragment.json"
        if frag.is_file():
            ins.json_merge(t / "opencode.json", json.loads(frag.read_text(encoding="utf-8")),
                           _merge_missing)
    if use_claude:
        cm = t / "CLAUDE.md"
        cur = cm.read_text(encoding="utf-8") if cm.is_file() else ""
        add = [f"@{f}" for f in INSTRUCTION_FILES if f"@{f}" not in cur]
        if add:
            ins.write(cm, cur.rstrip() + ("\n\n" if cur else "") + "\n".join(add) + "\n",
                      owned=True)

    # 3. skills
    skill_root = t / (".claude/skills" if use_claude else ".opencode/skills")
    for base in (KIT / "skills", KIT / "vendor" / "bmad" / "skills"):
        for sk in sorted(p for p in base.glob("*") if p.is_dir()):
            dst = skill_root / sk.name
            if dst.exists() and not force:
                ins.skipped.append(ins.rel(dst))
                ins.note(f"SKIP  {ins.rel(dst)}/ (exists; --force to overwrite)")
                continue
            ins.copytree(sk, dst)

    # 4. agents
    for ag in sorted((KIT / "agents").glob("*.md")):
        for tl, d in (("claude", ".claude/agents"), ("opencode", ".opencode/agents")):
            if (tl == "claude" and use_claude) or (tl == "opencode" and use_oc):
                ins.write(t / d / ag.name, render_agent(ag, tl, wf), owned=False)

    # 5. hooks
    if use_claude:
        frag = KIT / "adapters" / "claude" / "settings.hooks.json"
        if frag.is_file():
            ins.json_merge(t / ".claude" / "settings.json",
                           json.loads(frag.read_text(encoding="utf-8")), _merge_claude_hooks)
    if use_oc:
        ins.copytree(KIT / "adapters" / "opencode" / "plugins", t / ".opencode" / "plugins")
        pkg = KIT / "adapters" / "opencode" / "package.json"
        if pkg.is_file():
            ins.json_merge(t / ".opencode" / "package.json",
                           json.loads(pkg.read_text(encoding="utf-8")), _merge_missing)

    print("\nDone." if not dry else "\nDry run — nothing written.")
    if ins.skipped:
        print(f"{len(ins.skipped)} existing item(s) skipped — review them or rerun with --force.")
    print("Next: edit .workflow/config.toml (see the kit README 'Configure' section), then run\n"
          "  uv run --no-project .workflow/scripts/slot_registry.py init   # slot registry")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--target", required=True, type=Path)
    i.add_argument("--tool", required=True, choices=["opencode", "claude", "both"])
    i.add_argument("--force", action="store_true")
    i.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("render-agent")
    r.add_argument("agent", type=Path)
    r.add_argument("--tool", required=True, choices=["claude", "opencode"])
    r.add_argument("--out", type=Path)
    r.add_argument("--target", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "install":
        return install(a.target, a.tool, a.force, a.dry_run)
    wf = _load_wfconfig(a.target)
    try:
        text = render_agent(a.agent, a.tool, wf)
    except (ValueError, OSError) as e:
        print(f"render-agent: {e}", file=sys.stderr)
        return 2
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
