#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse(Write|Edit|NotebookEdit|Bash|PowerShell) guard for the story-propagate subagent.
Switch: `config: guards.propagate_paths`. Acts only when the payload's `agent_type` is story-propagate.

The dispatch prompt (the first user message of the agent's own transcript) sets the mode:
  * `mode: propose` (or no mode line) — read-only: every Write and Edit is denied.
  * `mode: apply` — a Write or Edit is allowed only on a path listed under the prompt's `approved:` block
    (`- <absolute path> — <note>` lines); paths are normalised and compared case-insensitively.

A shell command is allowed only when it is:
  * one read-only git command, checked on its shell-split argv: only the characters in GIT_CHARS (double quotes
    only around a `-C` path); `git`, optionally `-C <path>`, then a subcommand in GIT_OPTIONS; a subcommand in
    GIT_ACTIONS must be followed by one of its listed actions and no other positional argument (`worktree list`,
    never `worktree add`); every later argument starting with `-` is `--`, `-<n>`, or an option GIT_OPTIONS lists
    for that subcommand (exact name before any `=`). Any other option is denied;
  * one `uv run --no-project <plugin-root>/scripts/sprint_status.py [--dry-run] [--path <file>] <get|list|body|history>
    ...` (in apply mode also `set` and `history --append`, without `--path`; any prefix argparse expands to
    `--append` counts as the write) with no shell operator, substitution, escape, brace, glob or leading `~` outside
    a closed single-quoted argument; an unclosed `'` is denied, and a command holding any `"` gets no single-quote
    stripping.

Everything else is denied via hookSpecificOutput JSON on stdout, exit 0, with a reason naming the accepted forms;
an unreadable transcript is denied (fails closed for story-propagate). OpenCode carries no subagent transcript, so
under OpenCode the agent's prose is the only guard.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402
from _transcript import dispatch_prompt, transcript_file  # noqa: E402
from agent_roles import base_role  # noqa: E402

# Shell operators, substitution, escapes, and brace/glob expansion (bash expands them
# after this check reads the command); a word starting with `~` is tilde-expanded.
SHELL_META = re.compile(r"[;&|<>`$\n\\{}*?\[\]]|(?:^|\s)~")
# A closed single-quoted span is literal to bash: no expansion, no escape, no operator.
SINGLE_QUOTED = re.compile(r"'[^']*'")
GIT_CHARS = re.compile(r'^[A-Za-z0-9 ._/:=@^~+,%"-]+$')
_COMMON = {"--stat", "--name-only", "--name-status", "--shortstat", "--numstat", "--no-color"}
GIT_OPTIONS = {
    "diff": _COMMON | {"--unified", "--word-diff", "--cached", "--staged"},
    "log": _COMMON | {"--oneline", "--format", "--pretty", "--max-count", "-n", "-p", "--patch",
                      "--reverse", "--follow", "--since", "--until", "--author", "--no-merges",
                      "--first-parent", "--date"},
    "show": _COMMON | {"--format", "--pretty", "--oneline", "--no-patch", "-s"},
    "status": {"--porcelain", "--short", "-s", "--untracked-files", "--branch", "-b"},
    "rev-parse": {"--short", "--abbrev-ref", "--verify", "--show-toplevel", "--git-path"},
    "merge-base": {"--is-ancestor"},
    "ls-files": {"--others", "--exclude-standard", "--error-unmatch", "--modified", "-o", "-m"},
    "worktree": {"--porcelain", "-v", "--verbose"},
}
GIT_ACTIONS = {"worktree": {"list"}}
_NUMERIC = re.compile(r"^-(?:\d+|n\d+|U\d+)$")


def git_allowed(cmd: str) -> bool:
    """True for one read-only git command whose every option is listed for its subcommand."""
    if not GIT_CHARS.match(cmd) or re.search(r"(?:^|\s)~", cmd):
        return False
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    if len(argv) < 2 or argv[0] != "git":
        return False
    i = 1
    if argv[1] == "-C":
        if len(argv) < 4 or argv[2].startswith("-"):
            return False
        i = 3
    sub = argv[i]
    if sub not in GIT_OPTIONS:
        return False
    if '"' in cmd and not (i == 3 and cmd.count('"') == 2 and f'-C "{argv[2]}" ' in cmd):
        return False
    if sub in GIT_ACTIONS:
        if len(argv) < i + 2 or argv[i + 1] not in GIT_ACTIONS[sub]:
            return False
        if any(not tok.startswith("-") for tok in argv[i + 2:]):
            return False
    for tok in argv[i + 1:]:
        if not tok.startswith("-") or tok == "--" or _NUMERIC.match(tok):
            continue
        if tok.split("=", 1)[0] not in GIT_OPTIONS[sub]:
            return False
    return True
# `.workflow/scripts/` (install.py route) or the kit's own scripts dir (plugin route), quoted or not.
_KIT_SCRIPTS = re.escape((Path(__file__).resolve().parent.parent.parent / "scripts").as_posix())
SPRINT = re.compile(r"^(?:uv run --no-project|python3?) [\"']?(?:(?:[A-Za-z]:)?(?:[\w./-]+/)?\.workflow/scripts|"
                    + _KIT_SCRIPTS + r")/sprint_status\.py[\"']?"
                    r"((?: --dry-run| --path[ =][^\s'\"]+)*) (\w+)(?: |$)")
SPRINT_READ = {"get", "list", "body", "history"}
ACCEPTED = ("story-propagate's Bash runs one command per call, no shell operators: "
            "`git [-C <path>] <diff|log|show|status|rev-parse|merge-base|ls-files|worktree list> ...` with "
            "the options GIT_OPTIONS lists in .workflow/hooks/guards/propagate_paths.py, or "
            "`uv run --no-project <plugin-root>/scripts/sprint_status.py [--dry-run] [--path <file>] "
            "<get|list|body|history> ...` (apply mode adds `set` and `history --append`, without --path). "
            "Read files with Read, search with Grep, list files with Glob.")


def appends(cmd: str) -> bool:
    """True when an argument is `--append` or a prefix argparse expands to it."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return True
    return any(len(name) > 2 and "--append".startswith(name) for name in (tok.split("=", 1)[0] for tok in argv))


def norm(path: str) -> str:
    return os.path.normpath(path.strip().strip('"')).replace("\\", "/").lower()


def dispatch(payload: dict) -> tuple[str, set[str]]:
    """(mode, approved paths) from the agent's dispatch prompt."""
    path = transcript_file(payload)
    if not path:
        raise ValueError("no agent transcript")
    prompt = dispatch_prompt(path)
    m = re.search(r"(?m)^\s*mode:\s*(\w+)", prompt)
    mode = m.group(1).lower() if m else "propose"
    approved: set[str] = set()
    lines = prompt.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "approved:"), None)
    for line in (lines[start + 1:] if start is not None else []):
        line = line.strip()
        if not line.startswith("- "):
            break
        approved.add(norm(re.split(r"\s+—\s+|\s+-\s+", line[2:], maxsplit=1)[0]))
    return mode, approved


def check(payload: dict) -> str | None:
    """The deny reason, or None to allow. Acts only for story-propagate (`agent_type`)."""
    if base_role(payload.get("agent_type")) != "story-propagate":
        return None
    tool = payload.get("tool_name")
    if tool not in ("Bash", "PowerShell", "Write", "Edit", "NotebookEdit"):
        return None
    mode, approved = dispatch(payload)
    if tool in ("Bash", "PowerShell"):
        cmd = str((payload.get("tool_input") or {}).get("command") or "").strip()
        if git_allowed(cmd):
            return None
        # A `"` changes what a `'` means (`"a'" ; rm x ; "'b"`), so single-quoted spans are
        # stripped only from a command with no double quote.
        if '"' in cmd:
            unsafe = SHELL_META.search(cmd)
        else:
            unquoted = SINGLE_QUOTED.sub("Q", cmd)
            unsafe = "'" in unquoted or SHELL_META.search(unquoted)
        if unsafe:
            return f"{cmd[:120]!r}: no shell operators, redirection, substitution or expansion. {ACCEPTED}"
        s = SPRINT.match(cmd)
        if s:
            read = s.group(2) in SPRINT_READ and not appends(cmd)
            if read or (mode == "apply" and "--path" not in s.group(1)):
                return None
        return f"{cmd[:120]!r}: not an accepted command. {ACCEPTED}"
    ti = payload.get("tool_input") or {}
    raw = str(ti.get("file_path") or ti.get("notebook_path") or "")
    if mode != "apply":
        return "propose mode is read-only; return the proposed edit in the checklist instead"
    if norm(raw) not in approved:
        return f"{raw!r} is not in the dispatch's approved: list"
    return None


def main() -> int:
    if not g.enabled("propagate_paths"):
        return 0
    payload = g.read_payload()
    if payload is None:
        return 0
    try:
        reason = check(payload)
    except Exception as exc:
        reason = f"unreadable hook payload or dispatch ({type(exc).__name__}); denied"
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"[story-propagate] {reason} (.workflow/hooks/guards/propagate_paths.py)",
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
