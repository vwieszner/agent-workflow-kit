#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Hook-wiring and quoted-passage integrity — dev-preflight Step 0.

1 — hook scripts: every script a hook adapter references exists on disk AND is
    tracked by git (when the repo is a git checkout). Sources, each read when present:
      * `.claude/settings.json` — every `hooks.<event>[].hooks[].command` of type
        `command`; `$CLAUDE_PROJECT_DIR` / `${CLAUDE_PROJECT_DIR}` resolve to the repo root.
      * `.opencode/plugins/workflow-hooks.ts` — every quoted `*.py` path handed to
        `runHook`, relative to `.workflow/hooks/`.
    A dangling hook fails open inside the tool (the call just runs unguarded), so this
    is the only place a missing guard shows up.
2 — quoted passages: each `config: integrity.quoted_passages` entry
    `{file, text, quoted_by?}` — `text` must still be a substring of `file`, and
    `quoted_by` (the hook/script quoting it), when given, must still exist. Catches a
    hook citing a rule section that was renamed or removed. Empty list → SKIPPED.

Zero checks never read as success: no adapter file found, or zero script references
extracted from the adapters present, is a FAIL (extraction is broken or nothing is wired).

Run:  uv run --no-project .workflow/scripts/check_settings_and_docs_integrity.py
Exit 0 all green; 1 with one `FAIL` line per failure.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

SETTINGS_REL = ".claude/settings.json"
PLUGIN_REL = ".opencode/plugins/workflow-hooks.ts"
PLUGIN_HOOKS_REL = ".workflow/hooks"
SCRIPT_TOKEN = re.compile(r"(?:[A-Za-z]:)?[\w./\\-]*[\w-]\.(?:py|ps1|sh|bat)\b")
PLUGIN_SCRIPT = re.compile(r"""["'`]([\w./-]+\.py)["'`]""")
PROJECT_DIR_VARS = ("${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR")


def checkout_roots(root: Path) -> tuple[Path, ...]:
    """`root`, plus the main checkout's root when `root` is a linked worktree: hooks may name
    scripts by an absolute main-checkout path, and every worktree tracks the same relative paths."""
    common = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, check=False).stdout.strip()
    main_root = (root / common).resolve().parent if common else root
    return tuple(dict.fromkeys((root.resolve(), main_root)))


def repo_relative(token: str, roots: tuple[Path, ...]) -> str:
    """A hook's script token as `git ls-files` prints it: `/` separators, and a path under any of
    `roots` made repo-relative."""
    path = token.replace("\\", "/").removeprefix("./")
    for root in roots:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            continue
    return path.lstrip("/")


def settings_refs(repo: Path, failures: list[str]) -> list[str] | None:
    """Repo-relative scripts referenced by settings.json hooks; None when the file is absent."""
    path = repo / SETTINGS_REL
    if not path.is_file():
        return None
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        failures.append(f"[hooks] cannot parse {SETTINGS_REL}: {e}")
        return []
    refs: list[str] = []
    roots = checkout_roots(repo)
    for entries in (settings.get("hooks") or {}).values():
        for entry in entries or []:
            for hook in entry.get("hooks", []) or []:
                cmd = hook.get("command") if hook.get("type") == "command" else None
                if not cmd:
                    continue
                for var in PROJECT_DIR_VARS:
                    cmd = cmd.replace(var, "")
                for token in SCRIPT_TOKEN.findall(cmd):
                    rel = repo_relative(token, roots)
                    if "/" in rel or (repo / rel).exists():
                        refs.append(rel)
    return refs


def plugin_refs(repo: Path) -> list[str] | None:
    """Repo-relative hook scripts the OpenCode plugin runs; None when the plugin is absent."""
    path = repo / PLUGIN_REL
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return [f"{PLUGIN_HOOKS_REL}/{rel.removeprefix('./')}" for rel in PLUGIN_SCRIPT.findall(text)]


def tracked_files(repo: Path) -> set[str] | None:
    """git-tracked paths, or None when the repo is not a git checkout."""
    r = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True,
                       errors="replace")
    return set(r.stdout.splitlines()) if r.returncode == 0 else None


# Running from a Claude Code plugin install: the plugin's own hooks/hooks.json beside this scripts dir.
KIT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_HOOKS_JSON = KIT_ROOT / "hooks" / "hooks.json"


def plugin_hooks_refs(failures: list[str]) -> list[str] | None:
    """Kit-root-relative scripts the plugin's hooks.json runs; None outside a plugin install."""
    if not PLUGIN_HOOKS_JSON.is_file():
        return None
    try:
        data = json.loads(PLUGIN_HOOKS_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        failures.append(f"[hooks] cannot parse {PLUGIN_HOOKS_JSON}: {e}")
        return []
    refs = []
    for entries in (data.get("hooks") or {}).values():
        for entry in entries or []:
            for hook in entry.get("hooks", []) or []:
                cmd = str(hook.get("command") or "")
                for token in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}\"?/([\w./-]+\.py)", cmd):
                    refs.append(token)
    return refs


def check_plugin_hooks(failures: list[str]) -> int:
    refs = plugin_hooks_refs(failures)
    if refs is None:
        return 0
    if not refs:
        failures.append(f"[hooks] extracted ZERO script references from {PLUGIN_HOOKS_JSON}")
        return 0
    for rel in sorted(set(refs)):
        if not (KIT_ROOT / rel).is_file():
            failures.append(f"[hooks] the plugin's hooks.json references a missing script: {rel}")
    return len(set(refs))


def check_hooks(repo: Path, failures: list[str]) -> int:
    sources = {SETTINGS_REL: settings_refs(repo, failures), PLUGIN_REL: plugin_refs(repo)}
    present = {k: v for k, v in sources.items() if v is not None}
    if PLUGIN_HOOKS_JSON.is_file():
        # A plugin install: the plugin's hooks are the adapter; project settings may hold none of the kit's.
        n = check_plugin_hooks(failures)
        present = {k: [r for r in v if ".workflow/hooks/" in r] for k, v in present.items()}
        return n + sum(len(v) for v in present.values())
    if not present:
        failures.append(f"[hooks] neither {SETTINGS_REL} nor {PLUGIN_REL} exists — "
                        "no hook adapter to check; a zero-check pass is not a pass")
        return 0
    referenced = sorted({rel for refs in present.values() for rel in refs})
    if not referenced:
        failures.append(f"[hooks] extracted ZERO script references from {', '.join(present)} — "
                        "extraction is broken or nothing is wired; a zero-check pass is not a pass")
        return 0
    tracked = tracked_files(repo)
    if tracked is None:
        print("[hooks] git-tracked check SKIPPED — not a git checkout")
    for rel in referenced:
        if not (repo / rel).is_file():
            failures.append(f"[hooks] a hook references a missing script: {rel}")
        elif tracked is not None and rel not in tracked:
            failures.append(f"[hooks] hook script exists but is NOT git-tracked: {rel}")
    return len(referenced)


def check_passages(repo: Path, failures: list[str]) -> int:
    entries = wfconfig.get("integrity.quoted_passages", []) or []
    if not entries:
        print("[passages] SKIPPED — config: integrity.quoted_passages is empty")
        return 0
    checked = 0
    for i, e in enumerate(entries):
        rel, text = str(e.get("file", "")), str(e.get("text", ""))
        if not rel or not text:
            failures.append(f"[passages] entry {i} needs both `file` and `text`")
            continue
        quoted_by = str(e.get("quoted_by", "") or "")
        if quoted_by and not (repo / quoted_by).is_file():
            failures.append(f"[passages] {quoted_by} no longer exists — update "
                            "config: integrity.quoted_passages")
            continue
        path = repo / rel
        if not path.is_file():
            failures.append(f"[passages] {rel} does not exist (quoted by {quoted_by or '?'})")
            continue
        checked += 1
        if text not in path.read_text(encoding="utf-8", errors="replace"):
            failures.append(f"[passages] {quoted_by or 'a hook'} quotes a passage no longer in "
                            f"{rel}: {text!r}")
    return checked


def main(argv: list[str] | None = None) -> int:
    del argv
    repo = wfconfig.repo_root()
    failures: list[str] = []
    n_hooks = check_hooks(repo, failures)
    n_passages = check_passages(repo, failures)
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print(f"OK — hooks: {n_hooks} referenced script(s) exist"
          f"{' and are tracked' if tracked_files(repo) is not None else ''}; "
          f"passages: {n_passages} quoted passage(s) present")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
