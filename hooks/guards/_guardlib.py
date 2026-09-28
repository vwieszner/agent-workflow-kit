#!/usr/bin/env python3
"""Shared plumbing for the guard hooks in .workflow/hooks/guards/.

Every guard speaks the Claude Code hook protocol (DESIGN §7):
  * JSON payload on stdin (`tool_name`, `tool_input`, `agent_type`, `cwd`, ...).
  * Deny a built-in tool: exit 2 with the reason on stderr  -> `block()`.
  * Deny an MCP tool: JSON permissionDecision on stdout, exit 0 -> `deny_json()`
    (exit 2 does not block MCP tools).
  * Fail OPEN on internal error (`run()` turns any exception into exit 0);
    fail CLOSED on a matched rule.

Each guard is switched by `config: guards.<name>` (default true). `run()` checks it.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import wfconfig  # noqa: E402


# ------------------------------------------------------------------ payload / config

def read_payload() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.buffer.read().lstrip(b"\xef\xbb\xbf")  # strip UTF-8 BOM(s)
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def cfg(key: str, default: Any) -> Any:
    try:
        return wfconfig.get(key, default)
    except Exception:
        return default


def enabled(guard: str) -> bool:
    return bool(cfg(f"guards.{guard}", True))


def cfg_list(key: str, default: list | None = None) -> list:
    v = cfg(key, default if default is not None else [])
    return list(v) if isinstance(v, (list, tuple)) else []


# ------------------------------------------------------------------ decisions

def block(header: str, violations: list[str], hook: str, footer: str = "") -> int:
    sys.stderr.write(f"[workflow-hook] {header}\n\nDetected violations:\n")
    for v in violations:
        sys.stderr.write(f"  - {v}\n")
    if footer:
        sys.stderr.write(f"\n{footer}\n")
    sys.stderr.write(f"(Hook: .workflow/hooks/guards/{hook})\n")
    return 2


def deny_json(reason: str) -> int:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


def run(guard: str, fn: Callable[[dict[str, Any]], int]) -> None:
    """Entry point: config switch, payload parse, fail-open wrapper."""
    try:
        if not enabled(guard):
            sys.exit(0)
        payload = read_payload()
        if payload is None:
            sys.exit(0)
        code = fn(payload)
    except SystemExit:
        raise
    except Exception:
        code = 0  # a crashing guard must never brick the session
    sys.exit(code)


# ------------------------------------------------------------------ shell parsing

SEGMENT_SPLIT = re.compile(r"(?:;|&&|\|\||\n)")
# Separators for "first token" checks — NOT newlines, so a heredoc body line fed to a
# container is never mistaken for a host command.
SEGMENT_SPLIT_NO_NL = re.compile(r"(?:\|\||&&|[;|&])")

# docker compose exec / docker exec — tolerates compose flags between `compose` and
# `exec` (e.g. `-p <stack>`, `-f file.yml`, `--project-name X`, `--env-file .env`).
DOCKER_EXEC_COMPOSE = re.compile(
    r"\bdocker(?:[- ]compose)?(?:\s+(?:-[a-zA-Z]\S*|--[a-zA-Z][\w-]*(?:=\S+)?)\s+\S+)*\s+exec\b",
    re.IGNORECASE,
)
DOCKER_EXEC_SIMPLE = re.compile(r"\bdocker\s+exec\b", re.IGNORECASE)


def is_container_segment(seg: str) -> bool:
    return bool(DOCKER_EXEC_COMPOSE.search(seg) or DOCKER_EXEC_SIMPLE.search(seg))


def unquoted_residue(cmd: str) -> str:
    """Blank out single- and double-quoted spans (length-preserving) so only
    shell-UNQUOTED text remains."""
    out: list[str] = []
    state: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if state is None:
            if c in ("'", '"'):
                state = c
                out.append(" ")
            else:
                out.append(c)
        elif state == "'":
            if c == "'":
                state = None
            out.append(" ")
        else:
            if c == "\\" and i + 1 < n and cmd[i + 1] in ('"', "\\", "$", "`"):
                out.append("  ")
                i += 2
                continue
            if c == '"':
                state = None
            out.append(" ")
        i += 1
    return "".join(out)


# ------------------------------------------------------------------ shared command rules

PIP_INSTALL_PATTERNS = [
    r"(?:^|\s)pip3?(?:\.exe)?\s+install\b",
    r"(?:^|\s)python3?(?:\.exe)?\s+-m\s+pip\s+install\b",
    r"\.venv[\\/](?:Scripts|bin)[\\/]python3?(?:\.exe)?\s+-m\s+pip\s+install\b",
    r"\.venv[\\/](?:Scripts|bin)[\\/]pip3?(?:\.exe)?\s+install\b",
    r"(?:^|\s)uv\s+pip\s+install\b",
    r"(?:^|\s)uv\s+sync\b",
    r"\.venv[\\/](?:Scripts|bin)[\\/]uv(?:\.exe)?\s+(?:pip\s+install|sync)\b",
]


def host_install_violations(cmd: str) -> list[str]:
    """`config: guards.block_host_installs` — package installs on the host are
    forbidden; installs inside a container (docker exec segments) are fine."""
    if not cfg("guards.block_host_installs", True):
        return []
    out: list[str] = []
    for raw in SEGMENT_SPLIT.split(cmd):
        seg = raw.strip()
        if not seg or is_container_segment(seg):
            continue
        m = re.search(r"(?:^|\s)uv\s+(add|remove)\b", seg, re.IGNORECASE)
        if m and not re.search(r"--no-sync\b", seg):
            out.append(
                f"[host-uv-no-sync] uv {m.group(1)} on host without --no-sync: '{seg[:120]}'. "
                f"It would install into the host environment. Use 'uv {m.group(1)} --no-sync "
                f"<pkg>' (edits the manifest + lockfile only), then rebuild the container image."
            )
            continue
        for pattern in PIP_INSTALL_PATTERNS:
            if re.search(pattern, seg, re.IGNORECASE):
                out.append(
                    f"[host-pip] package install on the host: '{seg[:120]}'. Forbidden — it "
                    "pollutes the host environment. Edit the manifest without installing "
                    "(e.g. 'uv add --no-sync <pkg>'), then rebuild the container image; or "
                    "install inside the container via 'docker compose exec'."
                )
                break
    return out


def host_forbidden_violations(cmd: str, strip_call_operator: bool = False) -> list[str]:
    """`config: guards.host_forbidden` — regexes matched against the start of every
    host command segment (container segments skipped). First match reported."""
    patterns = cfg_list("guards.host_forbidden")
    if not patterns:
        return []
    hint = str(cfg("guards.host_forbidden_hint", "") or "")
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(str(p), re.IGNORECASE))
        except re.error:
            continue
    for seg in SEGMENT_SPLIT_NO_NL.split(unquoted_residue(cmd)):
        s = seg.strip()
        if not s or is_container_segment(s):
            continue
        s = re.sub(r"^sudo\s+", "", s)
        if strip_call_operator:
            s = re.sub(r"^&\s+", "", s)
        for rx in compiled:
            if rx.search(s):
                msg = (f"[host-forbidden] '{s[:120]}' matches config guards.host_forbidden "
                       f"('{rx.pattern}') — this command must not run on the host.")
                if hint:
                    msg += f" {hint}"
                return [msg]
    return []


def command_deny_violations(cmd: str) -> list[str]:
    """`config: guards.command_deny` — project rules: [{pattern, reason}, ...]."""
    out: list[str] = []
    for entry in cfg_list("guards.command_deny"):
        if not isinstance(entry, dict) or not entry.get("pattern"):
            continue
        try:
            if re.search(str(entry["pattern"]), cmd, re.IGNORECASE | re.MULTILINE):
                out.append(f"[command-deny] {entry.get('reason') or entry['pattern']}")
        except re.error:
            continue
    return out


def git_violations(cmd: str) -> list[str]:
    """git push / commit discipline — shared by the Bash and PowerShell guards."""
    out: list[str] = []
    main = str(cfg("git.main_branch", "main"))
    base = str(cfg("git.base_branch", "development"))
    protected = cfg_list("guards.protected_branches") or [main, base, "master"]
    prefix = str(cfg("git.branch_prefix", "story/"))
    prot_alt = "|".join(re.escape(b) for b in dict.fromkeys(protected) if b)

    if re.search(r"git\s+push[^|;&]*(--force|--force-with-lease|-f\s|-f$)", cmd, re.IGNORECASE):
        out.append("[git-push] force-push detected. Forbidden unless the owner explicitly "
                   "authorizes it. Use a non-force push.")

    if prot_alt and re.search(
        rf"git\s+push[^|;&]*\s(origin|upstream)\s+({prot_alt})(\s|$|:)", cmd, re.IGNORECASE,
    ):
        out.append(f"[git-push] push to a protected branch ({', '.join(protected)}). Forbidden "
                   "unless the owner explicitly authorizes the push.")

    if re.search(r"(^|[;&|]\s*)git\s+push\s*($|[;&|]|--[a-z])", cmd, re.IGNORECASE | re.MULTILINE):
        if not re.search(rf"git\s+push[^|;&]*\s(origin|upstream)\s+{re.escape(prefix)}",
                         cmd, re.IGNORECASE):
            out.append(f"[git-push] generic 'git push' without an explicit {prefix}<branch> "
                       f"target. Do not push unless the owner authorizes it; to push a story "
                       f"branch write 'git push origin {prefix}<id>' explicitly.")

    if re.search(r"git\s+commit[^|;&]*--no-verify", cmd, re.IGNORECASE):
        out.append("[git-commit] '--no-verify' skips pre-commit hooks. Forbidden unless the "
                   "owner explicitly authorizes it. Fix the underlying hook failure instead.")
    if re.search(r"git\s+commit[^|;&]*--no-gpg-sign", cmd, re.IGNORECASE):
        out.append("[git-commit] '--no-gpg-sign' bypasses commit signing. Forbidden unless "
                   "the owner explicitly authorizes it.")
    return out


# `git` plus any global options (`-C <dir>`, `-c k=v`, `--no-pager`) before the subcommand.
_GIT = r"\bgit(?:\s+(?:-[Cc]\s+\S+|--[\w-]+(?:=\S+)?))*\s+"
DESTRUCTIVE_RULES = [
    (re.compile(_GIT + r"reset\b[^|;&\n]*\s--hard\b", re.IGNORECASE),
     "[git-reset-hard] 'git reset --hard' discards uncommitted work irrecoverably."),
    (re.compile(_GIT + r"filter-branch\b", re.IGNORECASE),
     "[git-filter-branch] 'git filter-branch' rewrites history."),
    (re.compile(_GIT + r"update-ref\b[^|;&\n]*\s-d\b", re.IGNORECASE),
     "[git-update-ref] 'git update-ref -d' deletes a ref outside the normal branch commands."),
    (re.compile(r"\bFormat-Volume\b", re.IGNORECASE),
     "[format-volume] 'Format-Volume' erases a volume."),
    (re.compile(r"\bClear-Disk\b", re.IGNORECASE),
     "[clear-disk] 'Clear-Disk' erases a disk."),
]


def destructive_violations(cmd: str) -> list[str]:
    """Built-in never-run commands (both shells). Quoted text is ignored."""
    residue = unquoted_residue(cmd)
    return [f"{msg} Forbidden for agents; if intended, the owner runs it from their own shell."
            for rx, msg in DESTRUCTIVE_RULES if rx.search(residue)]


def _segments(cmd: str) -> list[str]:
    """Host command segments of `cmd`, split on unquoted `;` `&&` `||` `|` `&` only
    (not newlines), returned as ORIGINAL text so quoted arguments survive."""
    residue = unquoted_residue(cmd)
    out, start = [], 0
    for m in SEGMENT_SPLIT_NO_NL.finditer(residue):
        out.append(cmd[start:m.start()])
        start = m.end()
    out.append(cmd[start:])
    return [s.strip() for s in out if s.strip()]


_PY = r"python3?(?:\.\d+)?(?:\.exe)?"
BARE_PYTHON = re.compile(rf"^{_PY}(?=\s|$)", re.IGNORECASE)
# A stdlib-only kit script (`python3 .workflow/scripts/x.py`) is sanctioned tooling.
KIT_SCRIPT = re.compile(rf"^{_PY}\s+(?:-\S+\s+)*[\"']?(?:\./)?\.workflow[\\/](?:scripts|hooks)[\\/]",
                        re.IGNORECASE)


def host_python_violations(cmd: str, strip_call_operator: bool = False) -> list[str]:
    """Bare `python`/`python3` on the host when `config: env.app_runs_in_container` is true.
    Allowed: container segments, `uv run ...`, the configured `env.project_python`, and
    `.workflow/` kit scripts (stdlib-only tooling)."""
    if not cfg("env.app_runs_in_container", True):
        return []
    project_python = str(cfg("env.project_python", "") or "").strip()
    for seg in _segments(cmd):
        if is_container_segment(seg):
            continue
        s = re.sub(r"^sudo\s+", "", seg)
        if strip_call_operator:
            s = re.sub(r"^&\s+", "", s)
        if project_python and s.startswith(project_python):
            continue
        if BARE_PYTHON.search(s) and not KIT_SCRIPT.search(s):
            runner = str(cfg("env.host_tooling_runner", "uv run --no-project") or "")
            where = (f"the project interpreter '{project_python}'" if project_python
                     else "the app container (`<compose> exec -T <service> python ...`)")
            return [f"[host-python] bare '{s.split()[0]}' on the host: '{s[:120]}'. The app runs "
                    f"in a container (config env.app_runs_in_container). Run standalone tooling "
                    f"with '{runner} <script>', project-importing code with {where}."]
    return []


# ------------------------------------------------------------------ recursive delete

RM_CMD = re.compile(r"^(?:sudo\s+)?rm(?=\s|$)", re.IGNORECASE)
RM_RECURSIVE_FLAG = re.compile(r"^(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)$")
PS_DELETE_CMD = re.compile(r"^(?:&\s+)?(?:Remove-Item|ri|rm|rmdir|rd|del|erase)(?=\s|$)",
                           re.IGNORECASE)
PS_RECURSE_FLAG = re.compile(r"^-Rec(?:u(?:r(?:s(?:e)?)?)?)?(?::\$true)?$", re.IGNORECASE)
PS_PATH_OPT = re.compile(r"^-(?:LiteralPath|Path|LP|PSPath)(?::(.+))?$", re.IGNORECASE)
_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
_HOME_VARS = re.compile(r"^(?:\$HOME|\$\{HOME\}|\$env:(?:USERPROFILE|HOMEPATH|HOME))(?=$|[\\/])",
                        re.IGNORECASE)


def _norm(p: str) -> str:
    """Lexically normalized, lower-cased, forward-slashed path with a trailing '/'."""
    p = p.replace("\\", "/")
    drive = ""
    m = re.match(r"^([A-Za-z]:)(.*)$", p)
    if m:
        drive, p = m.group(1), m.group(2) or "/"
    p = posixpath.normpath(p) if p else "/"
    return (drive + p).rstrip("/").lower() + "/"


def _is_absolute(p: str) -> bool:
    return bool(re.match(r"^(?:/|\\\\|[A-Za-z]:(?:[\\/]|$))", p))


def _temp_bases() -> list[str]:
    bases = {_norm(tempfile.gettempdir())}
    if os.name != "nt":
        bases.update({"/tmp/", "/private/tmp/"})
    return sorted(bases)


def delete_allowed_roots() -> list[str]:
    """Roots a recursive delete must stay STRICTLY inside. Configured, or by default the
    repo root, the worktrees root, and `claude*`/`workflow*` dirs of the system temp dir."""
    roots = cfg_list("guards.recursive_delete_allowed")
    if roots:
        return [_norm(str(wfconfig.render(str(r)))) for r in roots]
    return [_norm(str(wfconfig.repo_root())), _norm(str(wfconfig.worktrees_root()))]


def _under_temp_scratch(norm: str) -> bool:
    for base in _temp_bases():
        if norm.startswith(base):
            first = norm[len(base):].split("/", 1)[0]
            if first.startswith(("claude", "workflow")) and len(norm) > len(base) + len(first) + 1:
                return True
    return False


def _delete_targets(seg: str, powershell: bool) -> list[str] | None:
    """Target arguments of a recursive delete in `seg`, or None if `seg` is not one."""
    toks = [t.strip("\"'") for t in _TOKEN.findall(seg)]
    if not toks:
        return None
    head = " ".join(toks[:2])
    is_rm = bool(RM_CMD.match(head))
    is_ps = powershell and bool(PS_DELETE_CMD.match(head))
    if not (is_rm or is_ps):
        return None
    args = toks[1:]
    if toks[0].lower() in ("sudo", "&"):
        args = toks[2:]
    recursive, targets = False, []
    for a in args:
        if is_rm and RM_RECURSIVE_FLAG.match(a):
            recursive = True
        elif is_ps and PS_RECURSE_FLAG.match(a):
            recursive = True
        elif is_ps and PS_PATH_OPT.match(a):
            val = PS_PATH_OPT.match(a).group(1)
            if val:
                targets.append(val.strip("\"'"))
        elif a.startswith("-") or a == "--":
            continue
        elif is_ps:
            targets.extend(t.strip() for t in a.split(",") if t.strip())
        else:
            targets.append(a)
    return targets if recursive else None


def recursive_delete_violations(cmd: str, cwd: str, powershell: bool = False) -> list[str]:
    """Recursive delete whose target is not strictly inside an allowed root. Checked:
    `/`, drive roots, `~`/`$HOME`, relative targets resolved against `cwd` (so `..`
    escapes are caught), absolute literals. Other variable-based targets are trusted."""
    roots: list[str] | None = None
    home = _norm(os.path.expanduser("~"))
    for seg in _segments(cmd):
        if is_container_segment(seg):
            continue
        targets = _delete_targets(seg, powershell)
        if not targets:
            continue
        roots = roots if roots is not None else delete_allowed_roots()
        for t in targets:
            if t == "~" or t.startswith(("~/", "~\\")) or _HOME_VARS.match(t):
                rest = re.sub(r"^(?:~|\$HOME|\$\{HOME\}|\$env:\w+)", "", t, flags=re.IGNORECASE)
                norm = _norm(home.rstrip("/") + "/" + rest.lstrip("\\/"))
            elif t.startswith(("$", "%")):
                continue  # other variable-based path: trusted
            elif _is_absolute(t):
                norm = _norm(t)
            else:
                norm = _norm(cwd.replace("\\", "/").rstrip("/") + "/" + t)
            ok = any(norm.startswith(r) and len(norm) > len(r) for r in roots)
            if not ok and not cfg_list("guards.recursive_delete_allowed"):
                ok = _under_temp_scratch(norm)
            if not ok:
                shown = ", ".join(r.rstrip("/") for r in roots)
                return [f"[recursive-delete] recursive delete of '{t}' (resolves to "
                        f"'{norm.rstrip('/') or '/'}') outside the allowed roots "
                        f"(config guards.recursive_delete_allowed): {shown}"
                        + ("" if cfg_list("guards.recursive_delete_allowed")
                           else ", <temp>/claude*, <temp>/workflow*")
                        + ". If intentional, the owner runs it from their own shell."]
    return []
