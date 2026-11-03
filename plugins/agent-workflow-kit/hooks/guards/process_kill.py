#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard on Bash / PowerShell: raw process kills are denied, and a `kill_owned.py` call gets the
caller's facts and a single-use token. Switch: `config: guards.process_kill`.

The bar is mistake-proofing, not a sandbox: an agent that makes an ordinary mistake, or follows a deny message,
cannot kill another stack's process. Raw kill commands are denied; `kill_owned.py` judges every target by the
caller's own worktree and launch time and by the gate or watch above it; this guard supplies those two facts and a
token so the caller cannot choose them. Documented residuals (code the agent writes and runs itself): a script
file, a launcher or evaluator that hides a kill (`uv run <exe>`, `uv run $K`, `Start-Process`, `timeout`, `nice`,
`env`, `eval`, `Invoke-Expression`), commands a `bash|sh` reads from stdin, a cmd command starting with `@`, the
agent's own psutil kill code, an import of `kill_owned`, and a token directory spelled without `kill-tokens`.

  * Raw kills are denied for every caller: `Stop-Process`, `spps`, `kill`, `taskkill`, `tskill`, `pkill`,
    `killall`, `.Kill()` on any object, `Invoke-CimMethod ... Terminate` and `wmic process ... delete|terminate`,
    at a command position (after `;`, `&&`, `||`, `|`, `&`, a newline, `(`, `{`, `xargs`, the call operators, a
    Bash backtick, and the `$(` or backtick opening a substitution inside double quotes) and inside `bash|sh -c`,
    `powershell|pwsh -Command` (any spelling PowerShell takes) and `cmd /c|/k|/r`, each read with its own shell's
    quoting. Text no shell runs (quoted arguments, heredoc and here-string bodies) is blanked first.
    `kill -0 <pid>` and `kill -s 0 <pid>` send nothing and pass.
  * A nested PowerShell or cmd is read only in the forms the check can place; any other form (positional text, an
    unknown switch, `-Command -`, a shell fed its commands on stdin) is denied with a message naming the readable
    form. `-EncodedCommand` (any spelling) is denied unread.
  * A `uv run ... kill_owned.py` call must be the whole command (no comment either) and must not carry
    `--worktree-root`, `--since`, `--hook-token` or (a subagent) `--worktree`. A subagent's call gets
    `--worktree <dispatch worktree_path> --since <first timestamp of its own transcript>`; a main session names
    `--worktree` itself, once, as a plain absolute path (forward slashes in Bash), and gets `--since <first
    timestamp of its session transcript>`. Both also get `--hook-token <nonce>`: `updated_input` writes
    `.workflow/state/kill-tokens/<nonce>.json` (`kill_token.py`) for exactly those values; `kill_owned.py` refuses
    a call whose token is missing, used, expired or issued for other values.
  * A shell command that names `kill-tokens` is denied: only this guard writes a token.
  * Any other route to `kill_owned.py` (the script as the command word, a python interpreter given it, `-m
    kill_owned`, `-c` code naming it, a nested shell, a wrapper or evaluator whose argv names it) is denied with
    the sanctioned form. A read-only command word (`cat`, `grep`, `rg`, `git`, `Get-Content`, ...) passes.
  * Fails closed for `kill_owned.py` calls, `-EncodedCommand` and a nested shell it cannot place; fails open for
    the rest of raw-kill detection. Nested shells are read `MAX_DEPTH` (3) levels deep.

Output: a deny is hookSpecificOutput JSON (`permissionDecision: deny`) with exit 0; a rewrite is
`hookSpecificOutput.updatedInput` (the whole `tool_input` with the extended `command`) with no
`permissionDecision`, so the normal permission evaluation applies to the rewritten command. Stdlib only.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

sys.path.insert(0, str(g.SCRIPTS_DIR))
import kill_token  # noqa: E402

VERBS = {"stop-process", "spps", "kill", "taskkill", "tskill", "pkill", "killall"}
PREFIX = {"sudo", "nohup", "exec", "time", "then", "do", "else", "elif", "if", "while", "until", "!", "env",
          "&", ".", "call"}
XARGS_WITH_ARG = {"-n", "-i", "-p", "-l", "-s", "-d", "-e", "-a"}
BASH_SHELLS = {"bash", "sh", "zsh", "dash"}
POWERSHELLS = {"powershell", "pwsh"}
READ_ONLY = {"cat", "type", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "findstr",
             "select-string", "sls", "get-content", "gc", "ls", "dir", "get-childitem", "gci", "test-path", "wc",
             "diff", "echo", "printf", "write-output", "write-host", "git", "ruff", "proc_scan.py", "kill_token.py"}
INSPECTORS = {"py_compile", "compileall", "unittest", "pytest", "pyflakes", "flake8", "ruff", "pyright", "mypy"}
EVALUATORS = {"eval", "iex", "invoke-expression", "invoke-command", "icm", "start-process", "saps"}
UV_VALUE_OPTIONS = {"--with", "--with-editable", "--with-requirements", "--env-file", "--extra", "--group",
                    "--package", "--python", "-p", "--directory", "--project", "--cache-dir", "--config-file",
                    "--index", "--default-index", "--index-url", "-i", "--extra-index-url", "--find-links", "-f",
                    "--index-strategy", "--keyring-provider", "--resolution", "--prerelease", "--exclude-newer",
                    "--link-mode", "--color"}
PY_VALUE_OPTIONS = {"-W", "-X", "--check-hash-based-pycs"}   # the next word is the option's value, not the operand
ENCODED = "-EncodedCommand"   # what `_raw_kill` reports for a `powershell` / `pwsh` payload it cannot read
UNPLACED = "-NestedShell"     # what `_raw_kill` reports for a nested shell whose command text it cannot place
# powershell.exe 5.1's switches, probed on this host: name -> (shortest spelling it accepts, takes a value). A `-` or
# `/` lead, any case, the shortest spelling or any longer prefix of the name, or an alias. A word that is none of
# these is not a switch: PowerShell starts its command text at the first word it does not know.
PS_SWITCHES = {
    "command": ("c", False), "file": ("f", False), "encodedcommand": ("e", False),
    "noprofile": ("nop", False), "nologo": ("nol", False), "noexit": ("noe", False),
    "noninteractive": ("noni", False), "sta": ("st", False), "mta": ("mta", False),
    "executionpolicy": ("ex", True), "windowstyle": ("w", True), "inputformat": ("i", True),
    "outputformat": ("o", True), "version": ("v", True), "configurationname": ("config", True),
    "psconsolefile": ("p", True),
}
PS_ALIASES = {"ec": "encodedcommand", "ep": "executionpolicy", "if": "inputformat", "of": "outputformat"}
# pwsh is not installed on this host and was not probed: it gets the switches above that mean the same in both
# shells (pwsh reads `-i` as -Interactive and has no -Version or -PSConsoleFile).
PWSH_UNPLACED = {"inputformat", "version", "psconsolefile", "configurationname", "sta", "mta"}
CMD_SWITCH = re.compile(r"/([ckr])", re.IGNORECASE)   # cmd.exe finds the first one anywhere in its arguments
CMD_SKIPPED = " \t=,;"   # what cmd.exe skips between the switch and the command text
FEEDS_STDIN = re.compile(r"<(?!\()")   # `<file`, `<<` and `<<<` give a command its stdin; `<(` is a substitution
TOKEN_DIR_NAME = kill_token.TOKEN_DIR_NAME
SEPARATORS = ";|&\n\r({"
BASH_SEPARATORS = SEPARATORS + "`"
NOT_CLEAN = set(";|&<>()`{}\n\r")
COMMENT = re.compile(r"(?:^|\s)#")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTHON = re.compile(r"^(?:py|pythonw?[\d.]*)$")
PY_MODULE = re.compile(r"^-[A-Za-z]*m(.*)$")
PY_CODE = re.compile(r"^-[A-Za-z]*c(.*)$")
UV_MODULE = re.compile(r"^(?:--module(?:=|$)|-[A-Za-z]*m)(.*)$")
KILL_OWNED = re.compile(r"(?<![A-Za-z0-9_])kill_owned(?![A-Za-z0-9_])")
EXPANSION = "$%`"
DOT_KILL = re.compile(r"\.\s*kill\s*\(", re.IGNORECASE)
WMIC_ACTION = re.compile(r"\b(?:delete|terminate)\b")
SHELL_C = re.compile(r"^-[a-z]*c$")
HERE_OPEN = re.compile(r"@(['\"])(?=[ \t]*\r?\n)")
HERE_CLOSE = re.compile(r"(?m)^(['\"])@")
WORKTREE_KEY = re.compile(r"worktree[_\s-]?path[\"'`*]*\s*[:=]\s*[\"'`*]*([^\s\"'`*]+)", re.IGNORECASE)
PLAIN_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/)[\w.\-\\/]*$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?$")
MAX_TRANSCRIPT_LINES = 2000
MAX_DEPTH = 3

HELPER_ONLY = ("[process-kill] run kill_owned.py as its own command: no other command, pipe, redirection, "
               "substitution or comment.")
NOT_SANCTIONED = (
    "[process-kill] this kill_owned.py call is denied: the hook supplies --worktree and --since only to a call "
    "run directly as uv run --no-project <plugin-root>/scripts/kill_owned.py <args>, with no other shell, "
    "interpreter or script path in between. Run it that way.")
DENY_RAW = (
    "[process-kill] {verb} is denied: a command line does not identify a stack. Kill only processes you own: "
    "list candidates with uv run --no-project <plugin-root>/scripts/proc_scan.py --match <regex>, then "
    "uv run --no-project <plugin-root>/scripts/kill_owned.py --pid <pid>\u2026 [--tree] (a compose watch: --watch; "
    "a main session adds --worktree <story worktree>). A REFUSE line is final: ask the owner, do not work around it.")
DENY_ENCODED = (
    "[process-kill] powershell -EncodedCommand is denied: the hook cannot read a base64 payload, so it cannot tell "
    "whether the command kills a process or runs kill_owned.py. Pass the command as plain text with -Command.")
DENY_NESTED = (
    "[process-kill] this nested powershell, pwsh or cmd call is denied: the hook cannot place the command text it "
    "runs. Write it as powershell -NoProfile -Command \"<text>\" (or powershell -File <script>), or as "
    "cmd /c \"<text>\". Positional text, a shell that reads its commands from stdin (a pipe, a < redirect or a "
    "heredoc feeds the shell directly, or feeds the nested bash|sh -c, powershell -Command or cmd /c text that "
    "holds it), -Command -, -CommandWithArgs and any switch the hook does not know are denied.")
DENY_TOKEN_DIR = (
    "[process-kill] a command that names kill-tokens is denied: only the process-kill hook writes a hook token, "
    "and kill_owned.py reads it. Run kill_owned.py as its sanctioned form instead; do not create, copy or list "
    "token files.")


class Deny(Exception):
    """A `kill_owned.py` call that cannot be given its facts."""


class Rewrite(NamedTuple):
    """A valid `kill_owned.py` call: the command with the facts appended and the `--worktree` / `--since` it will
    carry, which `updated_input` binds into the hook token."""
    command: str
    worktree: str
    since: str


class Nested(NamedTuple):
    """What a nested shell runs: the command text and the tool whose quoting it follows, or `problem` (`ENCODED` or
    `UNPLACED`) when the text cannot be read."""
    text: str = ""
    tool: str = ""
    problem: str = ""


# ---------------------------------------------------------------- command text
def _tokens(text: str) -> list[str]:
    out, cur, quote, started = [], [], None, False
    for c in text:
        if quote:
            if c == quote:
                quote = None
            else:
                cur.append(c)
        elif c in "'\"":
            quote, started = c, True
        elif c.isspace():
            if cur or started:
                out.append("".join(cur))
                cur, started = [], False
        else:
            cur.append(c)
    if cur or started:
        out.append("".join(cur))
    return out


def _name(token: str) -> str:
    base = re.split(r"[\\/]", token.strip())[-1].rstrip(")};")
    return base[:-4] if base.lower().endswith(".exe") else base


def _ps_unquoted_residue(cmd: str) -> str:
    """`bash_command._unquoted_residue` for PowerShell: quoted text becomes spaces and the length is kept.

    A backslash is literal, the backtick escapes the next character, `''` inside '...' and `""` inside "..." are
    literal quotes, and a `$(` inside "..." opens a subexpression PowerShell runs: its unquoted text stays
    visible, the opener is returned as ` (` (a command position) and the subexpression ends at the `)`
    balancing it."""
    out: list[str] = []
    stack: list[str] = []   # open contexts: "'", '"', "$("; empty = top level
    depth: list[int] = []   # unbalanced "(" inside each open "$(" context
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        state = stack[-1] if stack else None
        if c == "`" and state != "'" and i + 1 < n:
            out.append("  ")
            i += 2
            continue
        if state == "'":
            if cmd.startswith("''", i):
                out.append("  ")
                i += 2
                continue
            if c == "'":
                stack.pop()
            out.append(" ")
        elif state == '"':
            if cmd.startswith('""', i):
                out.append("  ")
                i += 2
                continue
            if cmd.startswith("$(", i):
                stack.append("$(")
                depth.append(0)
                out.append(" (")
                i += 2
                continue
            if c == '"':
                stack.pop()
            out.append(" ")
        else:
            if c in ("'", '"'):
                stack.append(c)
                out.append(" ")
            elif state == "$(" and c == ")" and depth[-1] == 0:
                stack.pop()
                depth.pop()
                out.append(" ")
            else:
                if state == "$(" and c == "(":
                    depth[-1] += 1
                elif state == "$(" and c == ")":
                    depth[-1] -= 1
                out.append(c)
        i += 1
    return "".join(out)


def _inside_double_quotes(text: str, at: int) -> bool:
    """Whether `text[at]` sits inside a double-quoted span. A `"` appended to the text before it closes that span,
    so the `x` after it is visible only then (a character in single quotes or after a backslash is not)."""
    return g.unquoted_residue(text[:at] + '"x', track_substitutions=True)[-1] == "x"


def _bash_residue(text: str) -> str:
    """The bash unquoted residue with the body of every `$(` and backtick substitution opened inside double quotes
    visible and its opener marked a command position (`(` and a backtick). `track_substitutions` exposes a `$(`
    body but blanks the opener, and blanks a backtick body whole, so without the marks `"$(kill 1)"` stays one
    segment with the text before it and `"`kill 1`"` hides its kill. An unquoted backslash and the character it
    escapes are blanked first, so `It\\'s` opens no quoted span."""
    plain = text
    while True:
        residue = list(g.unquoted_residue(plain, track_substitutions=True))
        if "\\" not in residue:
            break
        at = residue.index("\\")
        plain = plain[:at] + " " * min(2, len(plain) - at) + plain[at + 2:]
    at = plain.find("$(")
    while at != -1:
        if residue[at] == " " and _inside_double_quotes(plain, at):
            residue[at + 1] = "("
        at = plain.find("$(", at + 1)
    ticks = [i for i, c in enumerate(plain) if c == "`" and residue[i] == " " and _inside_double_quotes(plain, i)]
    for opener, closer in zip(ticks[::2], ticks[1::2]):
        residue[opener] = residue[closer] = "`"
        residue[opener + 1:closer] = _bash_residue(plain[opener + 1:closer])
    return "".join(residue)


def _residue(tool: str, text: str) -> str:
    return _ps_unquoted_residue(text) if tool == "PowerShell" else _bash_residue(text)


def _prepare(tool: str, cmd: str) -> tuple[str, str]:
    """(the command with text no shell runs blanked, its unquoted residue); both keep the command's length."""
    if tool == "PowerShell":
        body = g.blank_here_string_bodies(cmd)
        body = HERE_CLOSE.sub("  ", HERE_OPEN.sub("  ", body))
    else:
        body = g.blank_heredoc_bodies(cmd, keep_host_shell_bodies=True)
    return body, _residue(tool, body)


class Piece(NamedTuple):
    """A segment of a command with its unquoted residue (same length) and whether a pipe feeds its stdin (the
    separator before it is a `|` that is not half of `||`)."""
    text: str
    residue: str
    fed: bool


def _pieces(body: str, residue: str, tool: str) -> list[Piece]:
    separators = SEPARATORS if tool == "PowerShell" else BASH_SEPARATORS
    out: list[Piece] = []
    start, fed = 0, False
    for i, c in enumerate(residue):
        if c not in separators:
            continue
        if body[start:i].strip():
            out.append(Piece(body[start:i], residue[start:i], fed))
            fed = c == "|" and residue[i + 1:i + 2] != "|"
        start = i + 1
    if body[start:].strip():
        out.append(Piece(body[start:], residue[start:], fed))
    return out


def _segments(body: str, residue: str, tool: str) -> list[str]:
    return [piece.text for piece in _pieces(body, residue, tool)]


def _command_word(tokens: list[str]) -> tuple[int, str] | None:
    """(index, name) of the segment's command word after the prefix words, or None when a prefix ends it."""
    i = 0
    while i < len(tokens):
        raw = tokens[i].lstrip(")}")
        low = raw.lower()
        if not raw:
            i += 1
        elif low in PREFIX or ENV_ASSIGN.match(raw):
            i += 1
        elif low == "xargs":
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i].lower() in XARGS_WITH_ARG else 1
        elif low == "command":
            if i + 1 < len(tokens) and tokens[i + 1].startswith("-"):
                return None
            i += 1
        else:
            return i, _name(raw)
    return None


def _is_probe(args: list[str]) -> bool:
    return args[:1] == ["-0"] or args[:2] == ["-s", "0"]


def _raw_kill(text: str, tool: str, depth: int = 0, fed: bool = False) -> str | None:
    """The first process-terminating verb at a command position in `text`, `ENCODED` for a `powershell` / `pwsh`
    `-EncodedCommand` call whose payload cannot be read, `UNPLACED` for a nested shell whose command text cannot be
    placed (`_nested_shell`), else None. `fed`: the enclosing shell gives `text` its stdin."""
    if depth > MAX_DEPTH or not text.strip():
        return None
    body, residue = _prepare(tool, text) if depth == 0 else (text, _residue(tool, text))
    if DOT_KILL.search(residue):
        return ".Kill()"
    for piece in _pieces(body, residue, tool):
        segment = piece.text
        tokens = _tokens(segment)
        found = _command_word(tokens)
        if found is None:
            continue
        index, name = found
        low, args = name.lower(), tokens[index + 1:]
        if low in VERBS:
            if low == "kill" and _is_probe(args):
                continue
            return name
        if low == "invoke-cimmethod" and "terminate" in segment.lower():
            return "Invoke-CimMethod Terminate"
        if low == "wmic" and "process" in segment.lower() and WMIC_ACTION.search(segment.lower()):
            return "wmic process"
        stdin = fed or piece.fed or bool(FEEDS_STDIN.search(piece.residue))
        nested = _nested_shell(low, args, stdin)
        if nested is None:
            continue
        if nested.problem:
            return nested.problem
        verb = _raw_kill(nested.text, nested.tool, depth + 1, stdin)
        if verb:
            return verb
    return None


def _ps_switch(arg: str, pwsh: bool) -> str | None:
    """The `PS_SWITCHES` name `arg` spells, else None: a `--` lead, a glued `-command:x`, a word that is not a prefix
    (`-cx`) and a prefix shorter than the shortest spelling (`-no`, `-s`) are not switches."""
    low = arg[1:].lower() if len(arg) > 1 and arg[0] in "-/" else ""
    if not low:
        return None
    name = PS_ALIASES.get(low) or next(
        (n for n, (shortest, _) in PS_SWITCHES.items() if n.startswith(low) and len(low) >= len(shortest)), None)
    return None if name is None or (pwsh and name in PWSH_UNPLACED) else name


def _powershell_nested(pwsh: bool, args: list[str], fed: bool) -> Nested | None:
    """Walk the words of a `powershell` / `pwsh` call: switches (a value-taking one swallows the next word), then
    `-Command <text>`, whose text is everything after it, or `-File <script>`, whose script is not read and whose
    following words are its arguments. PowerShell starts its command text at the first word it does not know, so
    positional text and any other word are `UNPLACED`; so are `-Command -` and `-File -` (they read stdin), a missing
    text or script, and `-NoExit` or no command at all while stdin is fed (the shell then runs its stdin).
    `-EncodedCommand` is `ENCODED`. A call that only holds switches and has no stdin runs nothing."""
    noexit = False
    k = 0
    while k < len(args):
        switch = _ps_switch(args[k], pwsh)
        if switch is None:
            return Nested(problem=UNPLACED)
        if switch == "encodedcommand":
            return Nested(problem=ENCODED)
        if switch in ("command", "file"):
            rest = args[k + 1:]
            text = " ".join(rest)
            if not text.strip() or text.strip() == "-" or rest[0] == "-" or (noexit and fed):
                return Nested(problem=UNPLACED)
            return Nested(text, "PowerShell") if switch == "command" else None
        noexit = noexit or switch == "noexit"
        k += 2 if PS_SWITCHES[switch][1] else 1
    return Nested(problem=UNPLACED) if fed else None


def _cmd_nested(args: list[str], fed: bool) -> Nested | None:
    """cmd.exe runs the text after the first `/c`, `/k` or `/r` anywhere in its arguments, glued to it or not (`/q/c`,
    `x/c`, `/cecho`), after skipping blanks and `=`, `,` and `;`; that text is read with the bash rules. A call with
    no such switch, or with `/k`, reads its commands from stdin while stdin is fed, which is `UNPLACED`."""
    line = " ".join(args)
    found = CMD_SWITCH.search(line)
    if found is None or (found.group(1).lower() == "k" and fed):
        return Nested(problem=UNPLACED) if fed else None
    text = line[found.end():].lstrip(CMD_SKIPPED)
    return Nested(text, "Bash") if text.strip() else None


def _nested_shell(name: str, args: list[str], fed: bool = False) -> Nested | None:
    """What the shell `name` run with `args` would execute (`fed`: a pipe or a `<` redirect gives it its stdin), else
    None when `name` is no shell, or it runs nothing the check reads. Bash and its kin take the word after `-c`."""
    if name in BASH_SHELLS:
        for k, arg in enumerate(args):
            if SHELL_C.match(arg) and k + 1 < len(args):
                return Nested(args[k + 1], "Bash")
        return None
    if name in POWERSHELLS:
        return _powershell_nested(name == "pwsh", args, fed)
    if name == "cmd":
        return _cmd_nested(args, fed)
    return None


# ---------------------------------------------------------------- kill_owned calls
def _uv_program(args: list[str]) -> int | None:
    """Index in `args` (the words after `uv` / `uvx`) of the program `uv run` launches: the first word that is not
    an option, its value (for the options below) or the subcommand. `-m` / `--module` take no value, so after one
    that word is a module name, not a program (`_uv_modules`). An option missing from the table makes its value
    the program, whose own argv is then judged, so the mistake fails closed."""
    k = 0
    while k < len(args):
        if args[k] in UV_VALUE_OPTIONS:
            k += 2
        elif args[k].startswith("-") or args[k] in ("run", "tool"):
            k += 1
        else:
            return k
    return None


def _uv_modules(args: list[str], program: int | None) -> list[str] | None:
    """The module names `uv run` is told to run, or None when no `-m` / `--module` flag precedes `program`. uv takes
    the flag with no value, alone or in a short cluster such as `-qm`, so the module is the word at `program`. A name
    glued to the flag (`--module=X`, `-mX`) is a form uv rejects; it is judged as a module all the same."""
    end = len(args) if program is None else program
    flags = [match for match in map(UV_MODULE.match, args[:end]) if match]
    if not flags:
        return None
    glued = [match.group(1) for match in flags if match.group(1)]
    return glued + ([] if program is None else [args[program]])


class _Ctx(NamedTuple):
    """What `_launch_tokens` judges beside a segment's words."""
    bash: bool          # an unquoted backslash escapes the next character
    mentioned: bool     # the command names the helper somewhere (quoted text included)


def _is_helper_operand(operand: str, bash: bool, mentioned: bool) -> bool:
    """Whether an interpreter's script operand can be `kill_owned.py`: the name, a glob that matches it, or an
    expansion (`$K`, a substitution) in a command that names the helper somewhere. Bash reads an unquoted backslash
    as an escape, so there the name is also compared with the backslashes removed; PowerShell reads a backslash as
    a path separator."""
    names = [_name(text).lower() for text in ((operand, operand.replace("\\", "")) if bash else (operand,))]
    if "kill_owned.py" in names:
        return True
    if any(c in operand for c in EXPANSION):
        return mentioned
    return any(fnmatch.fnmatchcase("kill_owned.py", name) for name in names)


def _python_launches(args: list[str], ctx: _Ctx) -> bool:
    """Whether a Python interpreter given `args` runs `kill_owned.py`: as its script operand, as `-m kill_owned` (or
    `-m <module>` over a file naming it, unless the module only reads the file), as `-` with a later word that names
    the helper, or inside `-c` code. The options of `PY_VALUE_OPTIONS` take the next word as their value (CPython
    rejects `--check-hash-based-pycs=mode`), so that word is not the operand."""
    skip = False
    for k, arg in enumerate(args):
        rest = args[k + 1:]
        if skip:
            skip = False
        elif arg in PY_VALUE_OPTIONS:
            skip = True
        elif arg == "-":
            return any(KILL_OWNED.search(t) for t in rest)
        elif (option := PY_MODULE.match(arg)):
            module, after = (option.group(1), rest) if option.group(1) else (rest[0] if rest else "", rest[1:])
            return (module.rsplit(".", 1)[-1].lower() == "kill_owned"
                    or (module.lower() not in INSPECTORS and any(KILL_OWNED.search(t) for t in after)))
        elif (option := PY_CODE.match(arg)):
            return any(KILL_OWNED.search(t) for t in [option.group(1), *rest])
        elif not arg.startswith("-"):
            return _is_helper_operand(arg, ctx.bash, ctx.mentioned)
    return False


def _launch_tokens(low: str, args: list[str], tokens: list[str], depth: int, ctx: _Ctx) -> list[str] | None:
    """The tokens to report when the command (word `low`, argv `args`, in the segment `tokens`) can run
    `kill_owned.py` by a route the hook cannot rewrite, else None. A read-only command word never launches it. Any
    other word that is not an interpreter is judged by its argv: a word that names the helper launches it, and an
    evaluator's quoted text counts as argv words. `uv run -m` / `--module` launches the module it names, which is the
    helper when that module's last dotted part is `kill_owned`. A `powershell` / `pwsh` `-EncodedCommand` call is
    reported as well: its payload may hold a helper call and is not read."""
    if low == "kill_owned.py":
        return tokens
    nested = _nested_shell(low, args)
    if nested and nested.problem == ENCODED:
        return tokens
    if nested and nested.text:
        inner = _kill_owned_call(nested.tool, nested.text, depth + 1)
        return inner[0] if inner else None
    if PYTHON.match(low):
        return tokens if _python_launches(args, ctx) else None
    if low in READ_ONLY:
        return None
    if low in ("uv", "uvx"):
        k = _uv_program(args)
        if any(module.rsplit(".", 1)[-1].lower() == "kill_owned" for module in _uv_modules(args, k) or []):
            return tokens
        return None if k is None else _launch_tokens(_name(args[k]).lower(), args[k + 1:], tokens, depth, ctx)
    quoted = low in EVALUATORS
    named = any(KILL_OWNED.search(t) and (quoted or not any(c.isspace() for c in t)) for t in args)
    return tokens if named else None


def _kill_owned_call(tool: str, cmd: str, depth: int = 0) -> tuple[list[str], bool] | None:
    """(the tokens of the segment that can run `kill_owned.py`, whether the hook can rewrite that call in place),
    else None. Only a top-level `uv` segment whose program (`_uv_program`, with no `-m` / `--module` before it, which
    would make it a module) is `kill_owned.py` is rewritable; every other route that can launch the helper
    (`_launch_tokens`), a `uv` segment holding the name as a later word and a call nested in `bash -c`,
    `powershell -Command` or `cmd /c` included, is not."""
    if depth > MAX_DEPTH:
        return ([], False) if KILL_OWNED.search(cmd) else None
    body, residue = _prepare(tool, cmd) if depth == 0 else (cmd, _residue(tool, cmd))
    ctx = _Ctx(bash=tool != "PowerShell", mentioned=bool(KILL_OWNED.search(body)))
    for piece in _pieces(body, residue, tool):
        # A PowerShell backtick before a line break joins the lines (the residue blanks the pair) and leaves a lone
        # "`" token: whitespace to the shell, but the first non-option word to `_uv_program` and the script operand
        # to `_python_launches`, which would judge the backtick instead of the script on the next line.
        tokens = [t for t in _tokens(piece.text) if t != "`"]
        found = _command_word(tokens)
        if found is None:
            continue
        index, name = found
        low, args = name.lower(), tokens[index + 1:]
        if low == "uv":
            program = _uv_program(args)
            if (program is not None and _uv_modules(args, program) is None
                    and _name(args[program]).lower() == "kill_owned.py"):
                return tokens, depth == 0
        launched = _launch_tokens(low, args, tokens, depth, ctx)
        if launched is not None:
            return launched, False
    return None


def _is_whole_command(tool: str, cmd: str) -> bool:
    body, residue = _prepare(tool, cmd)
    return (len(_segments(body, residue, tool)) == 1 and not (set(residue.strip()) & NOT_CLEAN)
            and "$(" not in residue and not COMMENT.search(residue))


def _supplied(tokens: list[str], flag: str) -> bool:
    return any(t == flag or t.startswith(flag + "=") for t in tokens)


def _transcript_facts(path: str, want_dispatch: bool) -> tuple[str | None, str | None]:
    """(first timestamp, first user record's text); stops once it has what was asked for."""
    stamp = dispatch = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            if n >= MAX_TRANSCRIPT_LINES:
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if stamp is None and isinstance(record.get("timestamp"), str) and record["timestamp"]:
                stamp = record["timestamp"]
            if want_dispatch and dispatch is None and _is_user(record):
                dispatch = _text(record)
            if stamp is not None and (dispatch is not None or not want_dispatch):
                break
    return stamp, dispatch


def _is_user(record: dict) -> bool:
    message = record.get("message")
    return record.get("type") == "user" or (isinstance(message, dict) and message.get("role") == "user")


def _text(record: dict) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else record.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _check_supplied(payload: dict, tokens: list[str]) -> bool:
    """Raises Deny when the call carries a fact the hook supplies; returns whether the caller is a subagent."""
    if _supplied(tokens, "--worktree-root"):
        raise Deny("[process-kill] --worktree-root is for tests and is denied in a Claude call.")
    if _supplied(tokens, "--hook-token"):
        raise Deny("[process-kill] the hook supplies --hook-token; remove it from the call.")
    subagent = bool(payload.get("agent_type"))
    if _supplied(tokens, "--since") or (subagent and _supplied(tokens, "--worktree")):
        names = "--worktree and --since" if subagent else "--since"
        raise Deny(f"[process-kill] the hook supplies {names}; remove "
                   f"{'them' if subagent else 'it'} from the call.")
    return subagent


def _named_worktree(tokens: list[str], bash: bool) -> str:
    """The one `--worktree` value a main session's call names; raises Deny when it names none, names it more than
    once (argparse keeps the last, so the token would be bound to another value), leaves it without a value or
    writes it as anything but a plain absolute path. The shell rewrites a variable, `~`, a `/d/...` path and, in
    Bash, an unquoted backslash before `kill_owned.py` reads the argument, so the token would hold text the helper
    never receives. `_tokens` drops quotes, so a Bash value with a backslash is refused quoted or not."""
    values = []
    for k, token in enumerate(tokens):
        if token == "--worktree":
            if k + 1 >= len(tokens):
                raise Deny("[process-kill] --worktree needs a path.")
            values.append(tokens[k + 1])
        elif token.startswith("--worktree="):
            values.append(token[len("--worktree="):])
    if not values:
        raise Deny("[process-kill] a main session names its story worktree: add --worktree <path>.")
    if len(values) > 1:
        raise Deny("[process-kill] name --worktree once.")
    if not PLAIN_PATH.match(values[0]):
        raise Deny(f"[process-kill] --worktree {values[0]!r} is not a plain absolute path: write it as "
                   "an absolute path (<worktrees root>/<story id>), with no variable, ~ or /d/... form, because the shell "
                   "rewrites those before kill_owned.py reads the argument.")
    if bash and "\\" in values[0]:
        raise Deny("[process-kill] a --worktree path with a backslash is denied in Bash: it drops an unquoted one "
                   "before kill_owned.py reads the argument. Write it with forward slashes.")
    return values[0]


def _facts_suffix(payload: dict, tokens: list[str]) -> tuple[str, str, str]:
    """(the arguments the hook appends to a valid call, its --worktree, its --since); raises Deny when the facts
    cannot be resolved."""
    subagent = _check_supplied(payload, tokens)
    if subagent:
        path = transcript_file(payload)
        if not path:
            raise Deny("[process-kill] the agent's transcript was not found, so its dispatch worktree is unknown; "
                       "ask the orchestrator.")
        stamp, dispatch = _transcript_facts(path, want_dispatch=True)
        match = WORKTREE_KEY.search(dispatch or "")
        if not match:
            raise Deny("[process-kill] this agent owns no worktree; ask the orchestrator.")
        worktree = match.group(1).rstrip(",;").replace("\\", "/")
        if not PLAIN_PATH.match(worktree):
            raise Deny(f"[process-kill] the dispatch's worktree_path {worktree!r} is not a plain absolute path; "
                       "ask the orchestrator.")
    else:
        worktree = _named_worktree(tokens, payload.get("tool_name") == "Bash")
        path = payload.get("transcript_path")
        if not isinstance(path, str) or not os.path.isfile(path):
            raise Deny("[process-kill] the session transcript was not found, so --since cannot be supplied.")
        stamp, _ = _transcript_facts(path, want_dispatch=False)
    if not stamp or not TIMESTAMP.match(stamp):
        raise Deny("[process-kill] the transcript has no readable first timestamp, so --since cannot be supplied.")
    suffix = f" --worktree {worktree} --since {stamp}" if subagent else f" --since {stamp}"
    return suffix, worktree, stamp


def _unsanctioned(payload: dict, tokens: list[str]) -> str:
    """The deny for a `kill_owned.py` call the hook cannot rewrite in place; it also names a fact the call supplies."""
    try:
        _check_supplied(payload, tokens)
    except Deny as exc:
        return f"{exc} {NOT_SANCTIONED}"
    return NOT_SANCTIONED


def _raw_deny(verb: str) -> str:
    if verb == ENCODED:
        return DENY_ENCODED
    return DENY_NESTED if verb == UNPLACED else DENY_RAW.format(verb=verb)


def _analyze(payload) -> tuple[str | None, Rewrite | None]:
    """(deny message, rewrite): at most one is set; both None for a command this check ignores."""
    if not isinstance(payload, dict):
        return None, None
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if tool not in ("Bash", "PowerShell") or not isinstance(cmd, str) or not cmd.strip():
        return None, None
    if TOKEN_DIR_NAME in cmd.lower():
        return DENY_TOKEN_DIR, None
    try:
        verb = _raw_kill(cmd, tool)
    except Exception:
        verb = None
    if verb:
        return _raw_deny(verb), None
    try:
        call = _kill_owned_call(tool, cmd)
        if call is None:
            return None, None
        tokens, rewritable = call
        if not rewritable:
            return _unsanctioned(payload, tokens), None
        if not _is_whole_command(tool, cmd.rstrip()):
            return HELPER_ONLY, None
        suffix, worktree, since = _facts_suffix(payload, tokens)
        return None, Rewrite(cmd.rstrip() + suffix, worktree, since)
    except Deny as exc:
        return str(exc), None
    except Exception as exc:
        return f"[process-kill] the kill_owned.py call could not be checked ({type(exc).__name__}); denied.", None


def check(payload: dict) -> str | None:
    return _analyze(payload)[0]


def updated_input(payload: dict) -> dict | None:
    """The `tool_input` of a valid `kill_owned.py` call with its facts appended and a hook token issued for them, else
    None. The token file is written here and nowhere else (`check` writes nothing), once per call; a failure to write
    it raises, and the call then runs unrewritten and is refused by the helper for lacking a token."""
    deny, rewrite = _analyze(payload)
    if deny or rewrite is None:
        return None
    nonce = kill_token.issue(kill_token.token_dir(), rewrite.worktree, rewrite.since)
    return {**payload["tool_input"], "command": f"{rewrite.command} --hook-token {nonce}"}


def transcript_file(payload: dict) -> str | None:
    """The subagent's own transcript: `transcript_path` when it is `agent-<agent_id>.jsonl`, else that file under
    `<session>/subagents/` next to the session transcript."""
    tp = payload.get("transcript_path") or ""
    aid, sid = payload.get("agent_id"), payload.get("session_id")
    if not (tp and aid):
        return None
    if os.path.basename(tp) == f"agent-{aid}.jsonl" and os.path.isfile(tp):
        return tp
    if not sid:
        return None
    path = Path(tp)
    project = next((a.parent for a in path.parents if a.name == sid), path.parent)
    hits = glob.glob(os.path.join(project, sid, "subagents", "**", f"agent-{aid}.jsonl"), recursive=True)
    if not hits:
        hits = glob.glob(os.path.join(path.parent, "**", f"agent-{aid}.jsonl"), recursive=True)
    return hits[0] if hits else None


def evaluate(payload: dict) -> int:
    """Deny (JSON, exit 0 — survives a hook run through PowerShell) or rewrite the call via `updatedInput`."""
    message = check(payload)
    if message:
        return g.deny_json(message)
    try:
        rewritten = updated_input(payload)
    except OSError:
        rewritten = None
    if isinstance(rewritten, dict):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": rewritten}}))
    return 0


if __name__ == "__main__":
    g.run("process_kill", evaluate)
