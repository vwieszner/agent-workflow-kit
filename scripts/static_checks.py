#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Static checks for app code (type checker, linter), driven by `config: static_checks.tools`.

Usage:
    uv run --no-project .workflow/scripts/static_checks.py --worktree <root> --project-name <stack> --changed [--base <branch>]
    uv run --no-project .workflow/scripts/static_checks.py --worktree <root> --project-name <stack> --all

--changed  files that differ from the merge-base of HEAD and `--base` (default `config: git.base_branch`)
           — committed, staged, unstaged and untracked; deleted files dropped — filtered per tool.
--all      the whole tree: each tool's `all_args`.

Config — one `[[static_checks.tools]]` table per tool:
    name        label in the report
    cmd         argv template list; `{worktree}`, `{stack}`, `{compose}` are filled, and the file
                arguments are appended (the changed files, or `all_args` for --all)
    prefix      only changed files under this repo-relative prefix (default "")
    suffixes    only changed files with one of these suffixes (e.g. [".py"])
    strip       prefix removed from each file argument (a tool that runs inside a subdirectory or a
                container working dir); default ""
    all_args    file arguments for --all (default ["."])
    format      "pyright-json" | "eslint-json" | "exit-code"
    blocking_rules  (pyright-json) rules that block even at warning severity
    cwd         "worktree" (default) or "none"
A tool with no matching changed file is skipped. Without any configured tool the script prints
`[STATIC] no tools configured` and exits 0.

Blocking diagnostics:
  * pyright-json: severity error, or a rule in `blocking_rules`;
  * eslint-json:  severity 2 (error); warnings are counted, never block;
  * exit-code:    a non-zero exit; the output tail is shown.

Output: one `[STATIC] <tool> PASS|FAIL|INFRA-ERROR ...` line per tool, then each blocking diagnostic
as `<path>:<line> <rule> <message>`. Importable: `check(worktree, project, base)`.

Exit codes: 0 clean, 1 blocking diagnostics, 2 infra (tool missing, container down, unparsable output).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

SHOW = 30


def _git(worktree: str, *args: str) -> list[str]:
    out = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", check=True, timeout=120).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def changed_files(worktree: str, base: str) -> list[str]:
    mb = _git(worktree, "merge-base", "HEAD", base)[0]
    files = set(_git(worktree, "diff", "--name-only", "--diff-filter=d", mb))
    files |= set(_git(worktree, "ls-files", "--others", "--exclude-standard"))
    return sorted(files)


def tools() -> list[dict]:
    return [t for t in (wfconfig.get("static_checks.tools", []) or []) if isinstance(t, dict) and t.get("cmd")]


def select(tool: dict, files: list[str]) -> list[str]:
    prefix = str(tool.get("prefix", ""))
    suffixes = tuple(tool.get("suffixes", []) or [])
    strip = str(tool.get("strip", ""))
    out = []
    for f in files:
        if prefix and not f.startswith(prefix):
            continue
        if suffixes and not f.endswith(suffixes):
            continue
        out.append(f[len(strip):] if strip and f.startswith(strip) else f)
    return out


def _parse_pyright(stdout: str, rc: int, tool: dict) -> tuple[int, list[str], int]:
    try:
        diags = json.loads(stdout).get("generalDiagnostics", [])
    except (ValueError, AttributeError):
        return 2, [f"output unparsable (exit {rc})"], 0
    rules = set(tool.get("blocking_rules", []) or [])
    blocking = [d for d in diags if d.get("severity") == "error" or d.get("rule") in rules]
    return (1 if blocking else 0), [
        f"{d['file']}:{d['range']['start']['line'] + 1} {d.get('rule', d['severity'])} "
        f"{' '.join(d['message'].split())}" for d in blocking], len(diags) - len(blocking)


def _parse_eslint(stdout: str, rc: int, stderr: str, tool: dict) -> tuple[int, list[str], int]:
    try:
        results = json.loads(stdout)
    except ValueError:
        return 2, [f"output unparsable (exit {rc}): {stderr.strip()[-300:]}"], 0
    errors, warnings = [], 0
    strip = str(tool.get("strip", ""))
    for res in results:
        for m in res.get("messages", []):
            if m.get("severity") == 2:
                errors.append(f"{strip}{res.get('filePath', '?')}:{m.get('line', 0)} "
                              f"{m.get('ruleId')} {m.get('message')}")
            else:
                warnings += 1
    return (1 if errors else 0), errors, warnings


def run_tool(tool: dict, worktree: str, project: str, files: list[str] | None) -> tuple[int, list[str], int]:
    values = {"worktree": worktree, "stack": project,
              "compose": str(wfconfig.get("stack.compose_cmd", "docker compose"))}
    argv: list[str] = []
    for part in tool["cmd"]:
        rendered = str(part).format_map(values)
        argv.extend(rendered.split() if part == "{compose}" else [rendered])
    argv += files if files is not None else [str(a) for a in (tool.get("all_args", ["."]) or [])]
    if not shutil.which(argv[0]):
        return 2, [f"{argv[0]} not found on PATH"], 0
    r = subprocess.run(argv, cwd=worktree if tool.get("cwd", "worktree") == "worktree" else None,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    fmt = tool.get("format", "exit-code")
    if fmt == "pyright-json":
        return _parse_pyright(r.stdout, r.returncode, tool)
    if fmt == "eslint-json":
        return _parse_eslint(r.stdout, r.returncode, r.stderr, tool)
    if r.returncode == 0:
        return 0, [], 0
    tail = (r.stdout + r.stderr).strip().splitlines()[-SHOW:]
    return 1, tail, 0


def check(worktree: str, project: str, base: str | None) -> tuple[int, str]:
    """(exit code, summary) — prints the report. base None = whole tree."""
    configured = tools()
    if not configured:
        print("[STATIC] no tools configured (config: static_checks.tools) — SKIPPED")
        return 0, ""
    changed = None if base is None else changed_files(worktree, base)
    worst, parts = 0, []
    for tool in configured:
        name = str(tool.get("name", tool["cmd"][0]))
        files = None if changed is None else select(tool, changed)
        if files is not None and not files:
            continue
        rc, lines, warns = run_tool(tool, worktree, project, files)
        verdict = "PASS" if rc == 0 else "FAIL" if rc == 1 else "INFRA-ERROR"
        print(f"[STATIC] {name} {verdict} files={'all' if files is None else len(files)} "
              f"blocking={len(lines) if rc == 1 else 0} warnings={warns}")
        if lines:
            print("\n".join(lines[:SHOW]))
        worst = max(worst, rc)
        parts.append(name)
    if not parts:
        print("[STATIC] no changed file matches a configured tool — nothing to check")
    return worst, ("static " + "+".join(parts)) if parts else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0], allow_abbrev=False)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--project-name", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--changed", action="store_true")
    mode.add_argument("--all", action="store_true")
    ap.add_argument("--base", default=None)
    a = ap.parse_args(argv)
    base = None if a.all else (a.base or str(wfconfig.get("git.base_branch", "main")))
    try:
        rc, _ = check(a.worktree, a.project_name, base)
    except (subprocess.SubprocessError, OSError, KeyError, IndexError) as exc:
        print(f"[STATIC] INFRA-ERROR — {exc}")
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
