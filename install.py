#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Install agent-workflow-kit into a target project, or render one agent.

Usage:
  python3 install.py install --target <repo> --tool opencode|claude|both [--force] [--dry-run]
  python3 install.py render-agent <agent.md> --tool claude|opencode [--out <file>] [--target <repo>]
  python3 install.py build-plugin [--out <dir>] [--check]   (the Claude Code plugin, plugins/agent-workflow-kit)

`install` (run from the kit checkout):
  .workflow/scripts, .workflow/hooks, .workflow/rules  ← kit-managed, always refreshed
  .workflow/config.toml                               ← only if absent
  .workflow/AGENTS.md                                 ← kit-managed; wired into opencode.json
                                                        `instructions` and/or a CLAUDE.md @import
  .workflow/AGENTS.local.md, .workflow/rules/local/   ← project-owned; created once, never touched
  skills   → .claude/skills (claude|both — OpenCode reads .claude/skills too)
             .opencode/skills (opencode only)
  agents   → rendered into .claude/agents and/or .opencode/agents
  workflows→ .workflow/workflows (always; node replay fallback) and .claude/workflows
             (claude|both; the Claude Code Workflow tool)
  hooks    → .claude/settings.json hooks + permissions.deny merged (claude|both); the MCP
             guard matchers are rendered from graph.mcp_server / guards.mcp_readonly_servers
             .opencode/plugins/*.ts + .opencode/package.json deps (opencode|both)
  Existing skills/agents with the same name are SKIPPED (reported) unless --force.
  Files a previous install wrote (.workflow/.kit-manifest.json) that the kit no longer
  ships are removed; project-owned files are never in the manifest.

`render-agent` (also available installed as .workflow/scripts/install.py): renders a kit-source
agent (DESIGN.md §5) for one tool, using the target's .workflow/config.toml for models and MCP
server names. Prints to stdout unless --out is given.

Exit codes: 0 ok, 1 error, 2 bad agent source.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parent
IGNORE_NAMES = ("__pycache__", "*.pyc", ".DS_Store")
IGNORE = shutil.ignore_patterns(*IGNORE_NAMES)
MANIFEST = ".workflow/.kit-manifest.json"
# Kit-owned hook scripts whose Claude matchers install.py renders from config.
MCP_GUARDS = ("graph_query.py", "mcp_readonly.py", "playwright_paths.py")
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

def _split_top(inner: str) -> list[str]:
    """Split an inline list/map body on the commas outside nested [] / {} and quotes."""
    parts, depth, quote, cur = [], 0, "", []
    for ch in inner:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return [x for x in parts if x.strip()]


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'" and raw[-1] == raw[0] and len(raw) >= 2:
        body = raw[1:-1]
        return json.loads(raw) if raw[0] == '"' else body.replace("''", "'")
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_scalar(x) for x in _split_top(inner)] if inner else []
    if raw.startswith("{") and raw.endswith("}"):
        out: dict[str, Any] = {}
        inner = raw[1:-1].strip()
        for pair in filter(None, (p.strip() for p in _split_top(inner))):
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
    raw_blocks: dict[str, list[str]] = {}
    current: str | None = None
    child_indent: int | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t" and current is not None:
            raw_blocks.setdefault(current, []).append(line)
            indent = len(line) - len(line.lstrip())
            if child_indent is None:
                child_indent = indent
            if indent > child_indent:
                continue   # deeper YAML (e.g. agent hooks): kept raw, rendered verbatim
            k, _, v = line.strip().partition(":")
            if not isinstance(meta.get(current), dict):
                meta[current] = {}
            meta[current][k.strip().strip("\"'")] = _scalar(v) if v.strip() else {}
            continue
        child_indent = None
        k, _, v = line.partition(":")
        k = k.strip()
        if v.strip() == "":
            meta[k] = {}
            current = k
        else:
            meta[k] = _scalar(v)
            current = None
    if raw_blocks:
        meta["__raw__"] = raw_blocks
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
    unknown = set(caps) - set(CLAUDE_TOOLS) - {"graph", "docs", "browser"}
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
    browser_srv = str(_cfg(wf, "browser.mcp_server", "") or "")
    browser_on = "browser" in caps and bool(browser_srv)

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
        if browser_on:
            names += ["ToolSearch", f"mcp__{browser_srv}"]
        names = list(dict.fromkeys(names))
        if not names:
            raise ValueError(f"{src.name}: renders to an empty Claude tool list "
                             "(an omitted list would grant ALL tools)")
        out += [f"name: {meta['name']}", f"description: {_y(str(meta['description']))}",
                f"model: {_cfg(wf, f'models.claude.{tier}', 'sonnet')}"]
        if meta.get("effort"):
            out.append(f"effort: {meta['effort']}")
        out.append(f"tools: {', '.join(names)}")
        overrides = meta.get("claude") or {}
        mode = str(_cfg(wf, "models.claude_permission_mode", "") or "")
        if mode and "permissionMode" not in overrides:
            out.append(f"permissionMode: {mode}")
        raw = (meta.get("__raw__") or {}).get("claude")
        if raw:   # block form: passed through verbatim (nested YAML such as agent hooks)
            cut = min(len(x) - len(x.lstrip()) for x in raw)
            out += [x[cut:] for x in raw]
        else:
            for k, v in overrides.items():
                out.append(f"{k}: {v if isinstance(v, str) else _y(v)}")
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
        if browser_srv:
            out.append(f"  {_y(browser_srv + '*')}: {str(browser_on).lower()}")
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
        self.kit_files: set[str] = set()   # every file the kit ships into the target

    def record_tree(self, src: Path, dst: Path) -> None:
        for f in src.rglob("*"):
            rel = f.relative_to(src)
            if (f.is_file() and "__pycache__" not in rel.parts
                    and not any(rel.match(pat) for pat in IGNORE_NAMES)):
                self.kit_files.add(self.rel(dst / rel).replace("\\", "/"))

    def note(self, msg: str) -> None:
        self.log.append(msg)
        print(("DRY  " if self.dry else "") + msg)

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.t)) if p.is_relative_to(self.t) else str(p)

    def copytree(self, src: Path, dst: Path) -> None:
        if not src.is_dir():
            return
        self.note(f"sync  {self.rel(dst)}/")
        self.record_tree(src, dst)
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

    def prune(self) -> None:
        """Delete files an earlier install shipped that the kit no longer ships."""
        man = self.t / MANIFEST
        old: set[str] = set()
        if man.is_file():
            try:
                old = set(json.loads(man.read_text(encoding="utf-8")).get("files", []))
            except (json.JSONDecodeError, AttributeError):
                old = set()
        for rel in sorted(old - self.kit_files):
            f = self.t / rel
            if not f.is_file() or not f.resolve().is_relative_to(self.t):
                continue
            self.note(f"prune {rel} (no longer shipped by the kit)")
            if not self.dry:
                f.unlink()
                d = f.parent
                while d != self.t and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
        self.write(man, json.dumps({"files": sorted(self.kit_files)}, indent=1) + "\n", owned=True)

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


def _is_kit_mcp_hook(h: dict) -> bool:
    cmd = str(h.get("command", ""))
    return "/.workflow/hooks/guards/" in cmd and cmd.rstrip('"').endswith(MCP_GUARDS)


def _is_kit_command(h: dict) -> bool:
    cmd = str(h.get("command", ""))
    return "/.workflow/hooks/" in cmd or "/.workflow/scripts/" in cmd


def _merge_claude_settings(cur: dict, patch: dict) -> dict:
    """Hooks: append each entry whose command is not already wired for that event+matcher;
    refresh the fields of kit-owned entries already wired; replace the kit's MCP guard
    entries with the freshly rendered ones. permissions.deny: append missing rules, never
    remove the project's."""
    out = json.loads(json.dumps(cur))
    hooks = dict(out.get("hooks", {}))
    if "PreToolUse" in hooks:
        kept = []
        for grp in hooks["PreToolUse"]:
            rest = [h for h in grp.get("hooks", []) if not _is_kit_mcp_hook(h)]
            if rest:
                kept.append(dict(grp, hooks=rest))
            elif not grp.get("hooks"):
                kept.append(grp)
        hooks["PreToolUse"] = kept
    wanted = {str(h.get("command")) for groups in patch.get("hooks", {}).values()
              for grp in groups for h in grp.get("hooks", [])}
    for event in list(hooks):
        kept = []
        for grp in hooks[event]:
            rest = [h for h in grp.get("hooks", [])
                    if not (_is_kit_command(h) and str(h.get("command")) not in wanted)]
            if rest or not grp.get("hooks"):
                kept.append(dict(grp, hooks=rest))
        hooks[event] = kept
    for event, groups in patch.get("hooks", {}).items():
        existing = list(hooks.get(event, []))
        for grp in groups:
            match = next((g for g in existing if g.get("matcher") == grp.get("matcher")), None)
            if match is None:
                existing.append(grp)
                continue
            by_cmd = {h.get("command"): h for h in match.get("hooks", [])}
            for h in grp.get("hooks", []):
                have = by_cmd.get(h.get("command"))
                if have is None:
                    match["hooks"] = match.get("hooks", []) + [h]
                elif _is_kit_command(h):
                    have.update(h)
        hooks[event] = existing
    out["hooks"] = hooks
    deny = (patch.get("permissions") or {}).get("deny") or []
    if deny:
        perms = dict(out.get("permissions") or {})
        cur_deny = list(perms.get("deny") or [])
        perms["deny"] = cur_deny + [d for d in deny if d not in cur_deny]
        out["permissions"] = perms
    return out


def _render_claude_fragment(frag: dict, wf) -> dict:
    """Add one PreToolUse group per configured MCP server: the code-graph server gets
    graph_query.py, each `guards.mcp_readonly_servers` server gets mcp_readonly.py. No
    configured server → no MCP hook (a catch-all matcher would spawn two processes on
    every MCP call)."""
    out = json.loads(json.dumps(frag))
    cmd = 'uv run --no-project "$CLAUDE_PROJECT_DIR/.workflow/hooks/guards/{}"'
    servers: dict[str, list[str]] = {}
    if _cfg(wf, "graph.tool", "none") != "none":
        srv = str(_cfg(wf, "graph.mcp_server", "") or "")
        if srv:
            servers.setdefault(srv, []).append("graph_query.py")
    for entry in _cfg(wf, "guards.mcp_readonly_servers", []) or []:
        srv = str((entry or {}).get("server", "")) if isinstance(entry, dict) else ""
        if srv:
            servers.setdefault(srv, []).append("mcp_readonly.py")
    browser = str(_cfg(wf, "browser.mcp_server", "") or "")
    if browser:
        servers.setdefault(browser, []).append("playwright_paths.py")
    groups = out.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for srv, scripts in servers.items():
        groups.append({"matcher": f"mcp__{srv}__.*",
                       "hooks": [{"type": "command", "command": cmd.format(s), "timeout": 30}
                                 for s in scripts]})
    # Path-scoped rules injected into story agents at start (one command per rule name).
    names = [str(n) for n in (_cfg(wf, "path_rules.names", []) or []) if str(n)]
    if names:
        rule = 'uv run --no-project "$CLAUDE_PROJECT_DIR/.workflow/hooks/session/inject_path_rules.py" {}'
        out["hooks"].setdefault("SubagentStart", []).insert(0, {
            "matcher": str(_cfg(wf, "path_rules.agents", "^story-(impl|impl-xhigh|impl-max|finalize|rebase)$")),
            "hooks": [{"type": "command", "command": rule.format(n), "timeout": 15} for n in names]})
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
    ins.kit_files.add(".workflow/scripts/install.py")
    ins.write(wfd / "AGENTS.md", (KIT / "AGENTS.md").read_text(encoding="utf-8"), owned=True)
    ins.kit_files.add(".workflow/AGENTS.md")
    ins.write(wfd / ".gitignore", "state/\n__pycache__/\n", owned=True)
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
                ins.record_tree(sk, dst)
                ins.skipped.append(ins.rel(dst))
                ins.note(f"SKIP  {ins.rel(dst)}/ (exists; --force to overwrite)")
                continue
            ins.copytree(sk, dst)

    # 4. agents
    for ag in sorted((KIT / "agents").glob("*.md")):
        for tl, d in (("claude", ".claude/agents"), ("opencode", ".opencode/agents")):
            if (tl == "claude" and use_claude) or (tl == "opencode" and use_oc):
                ins.write(t / d / ag.name, render_agent(ag, tl, wf), owned=False)
                ins.kit_files.add(f"{d}/{ag.name}")

    # 4b. Workflow scripts: always under .workflow/workflows (the replay fallback runs them with
    # node), and for Claude Code also under .claude/workflows (the Workflow tool).
    ins.copytree(KIT / "workflows", wfd / "workflows")
    if use_claude and (KIT / "workflows").is_dir():
        for wfs in sorted((KIT / "workflows").glob("*.js")):
            ins.write(t / ".claude" / "workflows" / wfs.name,
                      wfs.read_text(encoding="utf-8"), owned=False)
            ins.kit_files.add(f".claude/workflows/{wfs.name}")

    # 5. hooks
    if use_claude:
        frag = KIT / "adapters" / "claude" / "settings.hooks.json"
        if frag.is_file():
            ins.json_merge(t / ".claude" / "settings.json",
                           _render_claude_fragment(json.loads(frag.read_text(encoding="utf-8")), wf),
                           _merge_claude_settings)
    if use_oc:
        ins.copytree(KIT / "adapters" / "opencode" / "plugins", t / ".opencode" / "plugins")
        pkg = KIT / "adapters" / "opencode" / "package.json"
        if pkg.is_file():
            # Only the plugin SDK is installed; typescript, @types/node and the typecheck
            # script exist for type-checking the kit's own plugin source.
            dev = json.loads(pkg.read_text(encoding="utf-8")).get("devDependencies", {})
            sdk = {"devDependencies": {k: v for k, v in dev.items() if k == "@opencode-ai/plugin"}}
            ins.json_merge(t / ".opencode" / "package.json", sdk, _merge_missing)

    ins.prune()

    print("\nDone." if not dry else "\nDry run — nothing written.")
    if ins.skipped:
        print(f"{len(ins.skipped)} existing item(s) skipped — review them or rerun with --force.")
    print("Next: edit .workflow/config.toml (see the kit README 'Configure' section), then run\n"
          "  uv run --no-project .workflow/scripts/slot_registry.py init   # slot registry")
    return 0



# ------------------------------------------------------------------ Claude Code plugin build

PLUGIN_NAME = "agent-workflow-kit"
PLUGIN_VERSION = "0.2.0"
PLUGIN_DIR = KIT / "plugins" / PLUGIN_NAME
ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
# Plugin render of agents: every optional MCP capability on (a server the project lacks is simply
# absent), models from the example config, no permissionMode (plugins ignore it).
PLUGIN_RENDER = {"graph.tool": "codebase-memory-mcp", "graph.mcp_server": "codebase-memory-mcp",
                 "docs.mcp_server": "context7", "browser.mcp_server": "playwright",
                 "models.claude_permission_mode": ""}
PROSE_REWRITES = [
    (".workflow/rules/local/", "\0LOCAL_RULES\0"),
    (".workflow/scripts/", ROOT_VAR + "/scripts/"),
    (".workflow/hooks/", ROOT_VAR + "/hooks/"),
    (".workflow/rules/", ROOT_VAR + "/rules/"),
    (".workflow/workflows/", ROOT_VAR + "/workflows/"),
    (".workflow/AGENTS.md", ROOT_VAR + "/AGENTS.md"),
    ("\0LOCAL_RULES\0", ".workflow/rules/local/"),
    ('call it with `name: "review"` (the installed `.claude/workflows/review.js`)',
     'call it with `scriptPath: "' + ROOT_VAR + '/workflows/review.js"` (the plugin\'s review workflow)'),
]
TEXT_SUFFIXES = {".md", ".txt", ".csv", ".xml", ".yaml", ".yml", ".json", ".js", ".mjs"}
# In shipped code, only command examples printed to the model are rewritten (never a path the code opens).
# The token has no braces: `${...}` inside a Python f-string or a JS template literal would be evaluated.
CODE_ROOT = "<plugin-root>"
CODE_REWRITES = [
    (re.compile(r"(uv run --no-project |python3? |node )\.workflow/(scripts|hooks|workflows)/"),
     r"\1" + CODE_ROOT + r"/\2/"),
    (re.compile(r"\(Hook: \.workflow/hooks/"), "(Hook: " + CODE_ROOT + "/hooks/"),
]


def _code(text: str) -> str:
    for rx, rep in CODE_REWRITES:
        text = rx.sub(lambda m, rep=rep: m.expand(rep), text)
    return text


class _PluginWF:
    """A wfconfig stand-in for rendering plugin agents (no project config exists at build time)."""

    @staticmethod
    def get(key: str, default: Any = None) -> Any:
        return PLUGIN_RENDER.get(key)


def _prose(text: str) -> str:
    for a, b in PROSE_REWRITES:
        text = text.replace(a, b)
    return text


def _plugin_agent(src: Path) -> str:
    text = render_agent(src, "claude", _PluginWF())
    head, _, body = text.partition("\n---\n")
    lines, out, skip = head.split("\n"), [], False
    for ln in lines:
        if skip and (ln.startswith((" ", "\t")) or not ln.strip()):
            continue
        skip = False
        if ln.startswith("hooks:"):          # per-agent hooks: ignored in plugins; hooks.json --role auto
            skip = True
            continue
        if ln.startswith("permissionMode:"):  # ignored in plugins
            continue
        if ln.startswith("description:"):     # frontmatter is not substituted: name scripts, not paths
            ln = ln.replace(".workflow/scripts/", "").replace(".workflow/hooks/", "")
        if ln.startswith("tools:") and "Bash" in ln and "PowerShell" not in ln:
            ln = ln.replace("Bash", "Bash, PowerShell", 1)
        if ln.startswith("skills:"):
            names = [n.strip().strip('"') for n in ln.split(":", 1)[1].strip().strip("[]").split(",") if n.strip()]
            ln = "skills: [" + ", ".join(f"{PLUGIN_NAME}:{n}" for n in names) + "]"
        out.append(ln)
    return "\n".join(out) + "\n---\n" + _prose(body)


def _plugin_hooks() -> tuple[dict, list[str]]:
    frag = json.loads((KIT / "adapters" / "claude" / "settings.hooks.json").read_text(encoding="utf-8"))
    text = json.dumps(frag["hooks"])
    text = text.replace('\\"$CLAUDE_PROJECT_DIR/.workflow/hooks/', '\\"' + ROOT_VAR + '/hooks/')
    text = text.replace('\\"$CLAUDE_PROJECT_DIR/.workflow/scripts/', '\\"' + ROOT_VAR + '/scripts/')
    text = text.replace("agent_outcome.py\\\" --role repro", "agent_outcome.py\\\" --role auto")
    hooks = json.loads(text)
    def cmd(rel: str) -> str:
        return 'uv run --no-project "' + ROOT_VAR + '/hooks/' + rel + '"'
    for grp in hooks.get("SessionStart", []):
        for h in grp["hooks"]:
            if "session_start.py" in h["command"]:
                h["command"] += " --plugin"
    hooks.setdefault("PostToolUse", []).append(
        {"matcher": "*", "hooks": [{"type": "command", "command": cmd("session/context_cap.py") + " --role auto",
                                    "timeout": 30}]})
    hooks.setdefault("SubagentStart", []).append(
        {"hooks": [{"type": "command", "command": cmd("session/inject_path_rules.py") + " --auto",
                    "timeout": 15}]})
    hooks.setdefault("PreToolUse", []).append(
        {"matcher": "mcp__.*", "hooks": [{"type": "command", "command": cmd(f"guards/{g}"), "timeout": 30}
                                         for g in MCP_GUARDS]})
    deny = list((frag.get("permissions") or {}).get("deny") or [])
    return {"description": "agent-workflow-kit guards and session hooks", "hooks": hooks}, deny


PLUGIN_README = """# agent-workflow-kit (Claude Code plugin)

Generated from the kit source by `python3 install.py build-plugin` — do not edit files here; edit the
kit and rebuild. The kit's README covers the workflow itself.

## Install

```
/plugin marketplace add vwieszner/agent-workflow-kit
/plugin install agent-workflow-kit@agent-workflow-kit
```

Then, once per project, invoke the `agent-workflow-kit:setup` skill (it creates `.workflow/config.toml`,
the project rules files and the slot registry, and adds the kit's deny rules to `.claude/settings.json`),
and fill in `.workflow/config.toml`.

## Differences from the `install.py` route

- Skills, agents and the review workflow are named `agent-workflow-kit:<name>`; the guards accept both
  forms.
- The working rules (`AGENTS.md`) and the project's `.workflow/AGENTS.local.md` are injected at session
  start by the plugin's SessionStart hook (a plugin cannot load CLAUDE.md).
- Plugin agents cannot carry their own hooks or permission mode: the completion checks and the context
  cap run from the plugin's hooks with `--role auto`, and agents run under the session's permission mode.
- Agents are rendered with the example config's models; the graph, docs and browser MCP tools are listed
  for every agent that declares them (absent servers are simply unavailable). A project that needs other
  models or MCP servers uses the `install.py` route.
- OpenCode is not covered by the plugin; use `install.py --tool opencode`.
"""


def build_plugin(out: Path) -> list[str]:
    """Write the plugin tree to `out` (replaced); return the files written, plugin-relative."""
    if out.exists():
        shutil.rmtree(out)
    files: list[str] = []

    def put(rel: str, data: str | bytes) -> None:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            dst.write_bytes(data)
        else:
            dst.write_text(data, encoding="utf-8")
        files.append(rel)

    def copy_tree(src: Path, rel: str, prose: bool, skip_tests: bool = True) -> None:
        for f in sorted(src.rglob("*")):
            r = f.relative_to(src)
            if not f.is_file() or "__pycache__" in r.parts or f.suffix == ".pyc" or f.name == ".DS_Store":
                continue
            if skip_tests and "tests" in r.parts:
                continue
            target = f"{rel}/{r.as_posix()}"
            if prose and f.suffix in TEXT_SUFFIXES:
                put(target, _prose(f.read_text(encoding="utf-8")))
            elif f.suffix == ".md":
                put(target, _prose(f.read_text(encoding="utf-8")))
            elif f.suffix in (".py", ".mjs", ".js"):
                put(target, _code(f.read_text(encoding="utf-8")))
            else:
                put(target, f.read_bytes())

    manifest = {"name": PLUGIN_NAME, "version": PLUGIN_VERSION,
                "description": "Spec-driven, human-gated development workflow: isolated story slots, "
                               "tests-first implementation, layered review, guarded merges, an autonomous "
                               "loop and a learning loop.",
                "repository": "https://github.com/vwieszner/agent-workflow-kit", "license": "MIT",
                "keywords": ["workflow", "code-review", "agents", "tdd", "git-worktree"]}
    put(".claude-plugin/plugin.json", json.dumps(manifest, indent=2) + "\n")
    for base in (KIT / "skills", KIT / "vendor" / "bmad" / "skills", KIT / "plugin" / "skills"):
        for sk in sorted(p for p in base.glob("*") if p.is_dir()):
            copy_tree(sk, f"skills/{sk.name}", prose=True)
    for ag in sorted((KIT / "agents").glob("*.md")):
        put(f"agents/{ag.name}", _plugin_agent(ag))
    hooks, deny = _plugin_hooks()
    put("hooks/hooks.json", json.dumps(hooks, indent=2) + "\n")
    put("setup/permissions.deny.json", json.dumps(deny, indent=2) + "\n")
    copy_tree(KIT / "hooks", "hooks", prose=False)
    copy_tree(KIT / "scripts", "scripts", prose=False)
    copy_tree(KIT / "rules", "rules", prose=True)
    copy_tree(KIT / "workflows", "workflows", prose=False)
    put("AGENTS.md", _prose((KIT / "AGENTS.md").read_text(encoding="utf-8")))
    put("workflow.config.example.toml", (KIT / "workflow.config.example.toml").read_text(encoding="utf-8"))
    put("LICENSE", (KIT / "LICENSE").read_text(encoding="utf-8"))
    put("README.md", PLUGIN_README)
    return files


def check_plugin(out: Path) -> list[str]:
    """Differences between the committed plugin tree and a fresh build (empty when in sync)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / PLUGIN_NAME
        build_plugin(fresh)
        want = {p.relative_to(fresh).as_posix(): p.read_bytes() for p in fresh.rglob("*") if p.is_file()}
    have = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts} if out.is_dir() else {}
    diffs = [f"missing  {r}" for r in sorted(set(want) - set(have))]
    diffs += [f"extra    {r}" for r in sorted(set(have) - set(want))]
    diffs += [f"differs  {r}" for r in sorted(set(want) & set(have)) if want[r] != have[r]]
    return diffs

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--target", required=True, type=Path)
    i.add_argument("--tool", required=True, choices=["opencode", "claude", "both"])
    i.add_argument("--force", action="store_true")
    i.add_argument("--dry-run", action="store_true")
    b = sub.add_parser("build-plugin")
    b.add_argument("--out", type=Path, default=None)
    b.add_argument("--check", action="store_true", help="exit 1 when the committed plugin differs from a build")
    r = sub.add_parser("render-agent")
    r.add_argument("agent", type=Path)
    r.add_argument("--tool", required=True, choices=["claude", "opencode"])
    r.add_argument("--out", type=Path)
    r.add_argument("--target", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "install":
        return install(a.target, a.tool, a.force, a.dry_run)
    if a.cmd == "build-plugin":
        out = a.out or PLUGIN_DIR
        if a.check:
            diffs = check_plugin(out)
            for d in diffs[:50]:
                print(d)
            print("plugin in sync" if not diffs else f"{len(diffs)} difference(s): run `python3 install.py build-plugin`")
            return 1 if diffs else 0
        files = build_plugin(out)
        print(f"wrote {len(files)} file(s) to {out}")
        return 0
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
