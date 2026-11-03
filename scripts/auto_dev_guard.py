#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Keep a halted story's work in an auto-dev-loop session, by mechanism rather than instruction.

A session is an auto-dev-loop session when either holds:
  * `MARKER` (`WORKFLOW_AUTO_DEV_RUN`) is in its environment: `auto_dev_driver.py` sets it (the
    run id) for every session it launches; shell calls, their hooks and the scripts they run
    inherit it;
  * `.workflow/state/auto-dev/sessions/<session_id>` exists: written when the session starts the
    `auto-dev-loop` skill (`mark_from_payload`, run by the `auto_dev_guard` hook on the Skill call
    and on the UserPromptExpansion event of a typed `/auto-dev-loop`).
In such a session `shell_check` denies, for every shell call (subagents' included):
  * `git worktree remove`, a recursive delete of a worktree root under `config: git.worktrees_root`
    (or of that directory itself), a `<config: git.branch_prefix>*` branch delete, a container
    volume `rm` / `prune`, and any command naming `MARKER` or the session-marker dir;
  * `cleanup_story_stack.py <id>` without `--keep-worktree` while that
    worktree's HEAD is not merged into `config: git.base_branch`;
  * `bring_up_story_stack.py --mode down` of a story that has a failure-log entry.
`merge_refusal` is the same unmerged-teardown check for `cleanup_story_stack.py` to call itself.
Outside an auto-dev-loop session everything is allowed. `land-story` removes a merged story's
worktree and branch through `cleanup_story_stack.py`.

The kit's data-only scripts (`halt_release.py`, `log_failure.py`, `resume_halted.py`) carry free
text (failure summaries, test output, a ruling) that may name a worktree path: a whole command
that is exactly one such call with nothing that can run or chain is not checked.

Importable; the hook is `.workflow/hooks/guards/auto_dev_guard.py`. Stdlib only.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

MARKER = "WORKFLOW_AUTO_DEV_RUN"
SKILL = "auto-dev-loop"

_DATA_ARG_SCRIPTS = {
    "halt_release.py": {"--story-id", "--failure-step", "--failure-summary", "--last-test-output", "--repo-root",
                        "--asked-of", "--external-ticket", "--external-item"},
    "log_failure.py": {"--story-id", "--branch-name", "--failure-step", "--failure-summary", "--wip-commit",
                       "--last-test-output", "--log-path", "--mutex-timeout-ms", "--asked-of", "--external-ticket"},
    "resume_halted.py": {"--ruling", "--no-ruling", "--repo-root"},
}
_STORY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_WORKTREE_REMOVE = re.compile(r"\bgit\b.*\bworktree\s+remove\b", re.I)
_VOLUME_DELETE = re.compile(r"\b(?:docker|podman)\b.*\bvolume\s+(?:rm|remove|prune)\b", re.I)
_DELETE_VERB = re.compile(
    r"(?<![\w-])(?:rm|rmdir|rd|del|erase|ri|Remove-Item)(?![\w-])|rmtree|Directory\]::Delete", re.I)
_SESSIONS_DIR = re.compile(r"auto-dev[\\/]+sessions", re.I)
_CLEANUP = re.compile(r"cleanup_story_stack\.py['\"]?\s+['\"]?([^\s'\"]+)", re.I)
_BRING_UP_DOWN = re.compile(r"bring_up_story_stack\.py\b.*--mode[=\s]+['\"]?down\b", re.I)

DENY = (
    "[auto-dev-keep-halted] An auto-dev-loop session never removes a story's worktree, branch or "
    "stack volumes: a halt keeps them and releases only the slot (auto-dev-loop workflow.md, "
    "halt-and-release). land-story removes them after the merge through cleanup_story_stack.py. "
    "A discard is the owner's, outside auto-dev-loop."
)


def sessions_dir() -> Path:
    return wfconfig.workflow_dir() / "state" / "auto-dev" / "sessions"


def session_marked(session_id: str | None) -> bool:
    return bool(session_id) and (sessions_dir() / str(session_id)).is_file()


def active(env: dict[str, str] | None = None, payload: dict | None = None) -> bool:
    if (os.environ if env is None else env).get(MARKER):
        return True
    return session_marked((payload or {}).get("session_id"))


def mark_from_payload(payload: dict) -> bool:
    """Write the session marker when the payload starts the auto-dev-loop skill; True when written."""
    skill = ""
    if payload.get("tool_name") == "Skill":
        ti = payload.get("tool_input") or {}
        skill = str(ti.get("skill") or ti.get("name") or "")
    elif payload.get("hook_event_name") == "UserPromptExpansion":
        skill = str(payload.get("command_name") or "")
    session_id = payload.get("session_id")
    if skill.strip().lstrip("/") != SKILL or not session_id:
        return False
    d = sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / str(session_id)).write_text(skill, encoding="utf-8")
    return True


def segments(command: str) -> list[str]:
    """The command split on `;`, `&`, `|` and newlines outside quotes and backslash escapes.

    Empty (nothing to check) only when the whole command is one data-only script call in which
    nothing can run or chain: no `$(`, `${`, `$'` or backtick outside single quotes, no unquoted
    `&`, `<` or `>`, no carriage return, no unclosed quote."""
    out, cur, quote, unsafe, i = [], [], None, "\r" in command, 0
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'":
            cur.append(command[i:i + 2])
            i += 2
            continue
        if quote != "'" and (c == "`" or command.startswith(("$(", "${", "$'"), i)):
            unsafe = True
        if quote is None and c in "&<>":
            unsafe = True
        if quote:
            cur.append(c)
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in ";|&\n":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    parts = [seg for seg in out if seg.strip()]
    if len(parts) == 1 and not unsafe and quote is None and inert_data_call(parts[0]):
        return []
    return out


def _kit_script(arg: str) -> Path | None:
    p = Path(arg)
    if not p.is_absolute():
        p = wfconfig.repo_root() / p
    scripts = wfconfig.workflow_dir() / "scripts"
    try:
        if os.path.normcase(str(p.resolve().parent)) == os.path.normcase(str(scripts.resolve())):
            return p
    except OSError:
        return None
    return None


def inert_data_call(segment: str) -> bool:
    """True only for `[uv run --no-project | python3] .workflow/scripts/<data-arg script> --option value ...`
    with every option one the script takes; for resume_halted.py, exactly one story id plus
    `--ruling`, `--no-ruling` and `--repo-root` at most once each."""
    try:
        argv = shlex.split(segment, posix=True)
    except ValueError:
        return False
    if argv[:3] == ["uv", "run", "--no-project"]:
        rest = argv[3:]
    elif argv and re.fullmatch(r"python3?(?:\.\d+)?", argv[0]):
        rest = argv[1:]
    else:
        return False
    if not rest:
        return False
    script = _kit_script(rest[0])
    options = _DATA_ARG_SCRIPTS.get(Path(rest[0]).name)
    if script is None or options is None:
        return False
    rest = rest[1:]
    if script.name == "resume_halted.py":
        ids, seen, k = [], [], 0
        while k < len(rest):
            if rest[k] == "--no-ruling" and rest[k] not in seen:
                seen.append(rest[k])
                k += 1
            elif rest[k] in options:
                if rest[k] in seen or k + 1 >= len(rest):
                    return False
                seen.append(rest[k])
                k += 2
            else:
                ids.append(rest[k])
                k += 1
        return len(ids) == 1 and bool(_STORY_ID.fullmatch(ids[0]))
    return len(rest) % 2 == 0 and all(rest[k] in options for k in range(0, len(rest), 2))


def _worktree_root_re() -> re.Pattern[str]:
    """A worktree root (or the worktrees directory itself, or a glob over it): the path ends at the
    worktree name, optionally with a trailing separator, before a quote, space or end."""
    name = re.escape(wfconfig.worktrees_root().name)
    return re.compile(rf"{name}(?:[\\/]+[^\\/\s'\"`;|&)]*)?[\\/]*(?=$|[\s'\"`;|&)])", re.I)


def _branch_delete_re() -> re.Pattern[str]:
    prefix = re.escape(str(wfconfig.get("git.branch_prefix", "story/")))
    return re.compile(rf"\bgit\b.*\bbranch\b.*(?:\s-[A-Za-z]*[dD][A-Za-z]*\b|\s--delete\b).*\b{prefix}", re.I)


def violation(command: str) -> str | None:
    """The command-text rule a shell command breaks in an auto-dev-loop session, or None."""
    if MARKER.lower() in command.lower() or _SESSIONS_DIR.search(command):
        return "touches the auto-dev session marker"
    root_re, branch_re = _worktree_root_re(), _branch_delete_re()
    for segment in segments(command):
        if _WORKTREE_REMOVE.search(segment):
            return "git worktree remove"
        if branch_re.search(segment):
            return "deletes a story branch"
        if _VOLUME_DELETE.search(segment):
            return "deletes container volumes"
        if _DELETE_VERB.search(segment) and root_re.search(segment):
            return "deletes a story worktree root directory"
    return None


def halted_stories(log: Path | None = None) -> set[str]:
    """Dashed story ids that have a failure-log entry."""
    log = log or wfconfig.path("paths.auto_dev_failure_log", "docs/implementation-artifacts/auto-dev-failures.md")
    try:
        text = log.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {m.replace(".", "-") for m in re.findall(r"^## (\S+) — ", text, re.M)}


def state_violation(command: str, repo: Path | None = None, log: Path | None = None) -> str | None:
    """The rule a teardown script call breaks given the story's state, or None."""
    repo = repo or wfconfig.repo_root()
    prefix = re.escape(str(wfconfig.get("slots.stack_prefix", "story-")))
    project = re.compile(rf"--project-name[=\s]+['\"]?{prefix}([^\s'\"]+)", re.I)
    for segment in segments(command):
        cleanup = _CLEANUP.search(segment)
        if cleanup and "--keep-worktree" not in segment and not cleanup.group(1).startswith("-"):
            refusal = merge_refusal(wfconfig.worktree_path(cleanup.group(1).replace(".", "-")), repo,
                                    env={MARKER: "hook"})
            if refusal:
                return f"tears down an unmerged worktree ({refusal.split('Refused: ', 1)[-1]})"
        p = project.search(segment)
        if _BRING_UP_DOWN.search(segment) and p and p.group(1).replace(".", "-") in halted_stories(log):
            return f"wipes the stack of halted story {p.group(1)}"
    return None


def shell_check(payload: dict, env: dict[str, str] | None = None) -> str | None:
    """Deny message for a shell call, or None. Allows everything outside an auto-dev-loop session."""
    if not active(env, payload):
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    rule = violation(command) or state_violation(command)
    return f"{DENY} Blocked: {rule}." if rule else None


def merge_refusal(worktree: Path, repo_root: Path, base: str | None = None,
                  env: dict[str, str] | None = None) -> str | None:
    """Why a full teardown of `worktree` is refused while `MARKER` is set, or None.

    Allowed without the marker, when the worktree is absent, and when its HEAD is an ancestor
    of the base branch (the story merged)."""
    if not (os.environ if env is None else env).get(MARKER) or not worktree.exists():
        return None
    base = base or str(wfconfig.get("git.base_branch", "development"))
    head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode != 0:
        return f"{DENY} Refused: the HEAD of {worktree} cannot be read, so its merge is unproven."
    merged = subprocess.run(["git", "-C", str(repo_root), "merge-base", "--is-ancestor", head.stdout.strip(), base])
    if merged.returncode != 0:
        return f"{DENY} Refused: {worktree} HEAD {head.stdout.strip()[:8]} is not merged into {base}."
    return None
