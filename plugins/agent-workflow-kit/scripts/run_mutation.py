#!/usr/bin/env python3
"""Mutation-test a story's changed Python modules against the story's own tests (test-adequacy stage).

Only the functions and methods the story's diff touches are mutated: the module at the diff's end revision is
parsed, and every top-level function or class method whose lines (decorators included) overlap a line the diff
added or changed becomes a mutmut name pattern (`<module>.x_<func>__mutmut_*`, `<module>.xǁ<Class>ǁ<method>__mutmut_*`).
A changed line outside any function (a constant, a class-level field) is not mutable and is listed in
`unmutable_changes`.

Runs mutmut 3 in a scratch copy (a fresh `mktemp -d` per run) of the app root, so the synced source and the
project's `pyproject.toml` are never written:
  * container mode (`config: review.mutation.service` set): inside that service of the story stack, app root
    `config: review.mutation.app_root`;
  * host mode (service ""): on the host, app root = the worktree (POSIX `sh` required).
Steps: copy every top-level entry of the app root except `config: review.mutation.skip_dirs`; append a
`[tool.mutmut]` table (`source_paths` = the modules, test selection = the tests, `also_copy` = the tree, pytest
args `config: review.mutation.pytest_args`); `mutmut run --max-children 1 <patterns>` under a wall-clock timeout;
`mutmut results --all true` and `mutmut show` per survivor; delete the scratch copy.

Paths: a repo-relative module path minus `config: review.mutation.src_prefix` is its path under the app root.

The default budget (`--timeout`, default `config: review.mutation.timeout_s` = 420; the outer call is capped at
`--timeout + 120`) keeps one invocation inside a 600 s tool limit.

Survivors are split by the story's diff: a survivor whose mutated lines overlap a line the diff added or changed
in that module (`git diff -U0 <diff-range>` in the worktree) is reported with its diff; the rest are counted only.
Mutants left unchecked by the timeout are counted as `not checked`, never dropped.

Prints one JSON object: status (`ok` / `timeout` / `nothing-to-mutate` / `blocked`), reason, `functions`,
`unmutable_changes`, counts by mutmut status, `survivors` (in the diff: name, file, lines, diff) and
`survivors_outside_diff`. Exit 0 for ok/timeout/nothing-to-mutate, 2 for blocked (tests did not run under pytest,
mutmut missing, the stack is down, or the stage is disabled).

Usage
-----
    uv run --no-project <plugin-root>/scripts/run_mutation.py --project-name <stack> --worktree <abs worktree>
        --diff-range <base>..<end> --module <repo path>.py [--module ...] --test <repo path>.py [--test ...]
        [--timeout 420]
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

DEFAULT_SKIP = [".venv", "mutants", "node_modules", "logs", ".git"]
STATUSES = ("killed", "survived", "timeout", "suspicious", "no tests", "skipped", "not checked",
            "segfault", "caught by type check", "check was interrupted by user")
_RESULT = re.compile(r"^\s*(\S+): (.+?)\s*$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_RANGE = re.compile(r"^[0-9A-Za-z_][\w./^~-]*\.\.[0-9A-Za-z_][\w./^~-]*$")


def cfg(key: str, default):
    try:
        return wfconfig.get(key, default)
    except Exception:
        return default


def src_prefix() -> str:
    p = str(cfg("review.mutation.src_prefix", "") or "").replace("\\", "/").strip("/")
    return p + "/" if p else ""


def container_path(repo_path: str) -> str:
    """`<src_prefix>pkg/x.py` -> `pkg/x.py`, the path under the app root."""
    p = repo_path.replace("\\", "/")
    pre = src_prefix()
    if not p.startswith(pre) or ".." in p.split("/") or not re.fullmatch(r"[\w./-]+\.py", p):
        raise ValueError(f"{repo_path!r} is not a Python path under {pre or 'the repo root'}")
    return p[len(pre):]


def module_file(mutant: str) -> str:
    """`pkg.mod.x_fn__mutmut_4` -> `<src_prefix>pkg/mod.py`.

    mutmut 3 names a mutant `<dotted module>.<x_function | xǁClassǁmethod>__mutmut_<n>`."""
    return src_prefix() + "/".join(mutant.split(".")[:-1]) + ".py"


def parse_results(text: str) -> dict[str, str]:
    """`mutmut results --all true` output -> {mutant name: status}."""
    out = {}
    for line in text.splitlines():
        m = _RESULT.match(line)
        if m and m.group(2) in STATUSES:
            out[m.group(1)] = m.group(2)
    return out


def changed_lines(diff_u0: str) -> set[int]:
    """New-file line numbers a `git diff -U0` adds or changes."""
    lines: set[int] = set()
    for line in diff_u0.splitlines():
        m = _HUNK.match(line)
        if m:
            start, count = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
            lines.update(range(start, start + count))
    return lines


def mutated_lines(show: str) -> set[int]:
    """Current-file lines a `mutmut show` diff removes or replaces (its `-` lines, not context)."""
    lines: set[int] = set()
    old = None
    for line in show.splitlines():
        m = _HUNK.match(line)
        if m:
            old = int(m.group(1))
        elif old is None or line.startswith(("---", "+++")):
            continue
        elif line.startswith("-"):
            lines.add(old)
            old += 1
        elif not line.startswith("+"):
            old += 1
    return lines


def dotted_module(repo_path: str) -> str:
    """`<src_prefix>pkg/x.py` -> `pkg.x` (mutmut's module name under the app root)."""
    return container_path(repo_path)[:-3].replace("/", ".")


def changed_functions(source: str, lines: set[int], module: str) -> tuple[dict[str, int], list[int]]:
    """{mutmut name prefix: file line of its `def`} for the top-level functions and class methods
    `lines` touch, and the changed lines that fall in no function. A prefix plus
    `__mutmut_*` is the pattern `mutmut run` selects."""
    found: dict[str, int] = {}
    covered: set[int] = set()

    def span(node: ast.AST) -> set[int]:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        return set(range(start, node.end_lineno + 1))

    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.parse(source).body:
        if isinstance(node, funcs):
            hit = span(node) & lines
            if hit:
                found[f"{module}.x_{node.name}"] = node.lineno
                covered |= hit
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, funcs):
                    hit = span(item) & lines
                    if hit:
                        found[f"{module}.x\u01c1{node.name}\u01c1{item.name}"] = item.lineno
                        covered |= hit
    return found, sorted(lines - covered)


def container_script(modules: list[str], tests: list[str], patterns: list[str], timeout: int, app_root: str,
                     mutmut: str) -> str:
    toml = "\n".join([
        "", "[tool.mutmut]",
        "source_paths = " + json.dumps([container_path(m) for m in modules]),
        "pytest_add_cli_args_test_selection = " + json.dumps([container_path(t) for t in tests]),
        "pytest_add_cli_args = " + json.dumps([str(x) for x in cfg("review.mutation.pytest_args", []) or []]),
        "also_copy = ALSO_COPY", "",
    ])
    names = " ".join("'" + p + "'" for p in patterns)
    skip = " ".join(sorted(str(x) for x in (cfg("review.mutation.skip_dirs", DEFAULT_SKIP) or [])))
    return f"""set -u
W=$(mktemp -d "${{TMPDIR:-/tmp}}/mutmut-run.XXXXXX")
mkdir -p "$W/logs"
trap 'rm -rf "$W"' EXIT
cd '{app_root}'
command -v '{mutmut}' >/dev/null 2>&1 || [ -x '{mutmut}' ] || {{ echo "@@MUTMUT-MISSING@@"; exit 0; }}
ALSO=""
for e in *; do
  case " {skip} " in *" $e "*) continue;; esac
  cp -r "$e" "$W/"
  if [ -d "$e" ]; then ALSO="$ALSO\\"$e/\\", "; else ALSO="$ALSO\\"$e\\", "; fi
done
ALSO="[$ALSO\\"logs/\\"]"
cat >> "$W/pyproject.toml" <<'TOML'
{toml}
TOML
sed -i "s|^also_copy = ALSO_COPY$|also_copy = $ALSO|" "$W/pyproject.toml"
cd "$W"
timeout {int(timeout)} '{mutmut}' run --max-children 1 {names} > run.log 2>&1
echo "@@RUN-EXIT@@ $?"
tail -c 3000 run.log
echo "@@RESULTS@@"
'{mutmut}' results --all true 2>&1
for n in $('{mutmut}' results 2>/dev/null | sed -n 's/^ *\\([^ ]*\\): survived$/\\1/p'); do
  echo "@@SHOW@@ $n"
  '{mutmut}' show "$n" 2>&1
done
echo "@@END@@"
"""


def validate(a: argparse.Namespace) -> None:
    """Refuse any value that could reach the engine or git as an option or outside a story."""
    if not cfg("review.mutation.enabled", False):
        raise ValueError("mutation testing is disabled (config review.mutation.enabled = false)")
    stack_prefix = str(cfg("slots.stack_prefix", "story-"))
    if not re.fullmatch(re.escape(stack_prefix) + r"[A-Za-z0-9][\w.-]*", a.project_name):
        raise ValueError(f"--project-name {a.project_name!r} is not a story stack ({stack_prefix}<id>)")
    wt = os.path.normcase(os.path.abspath(a.worktree))
    root = os.path.normcase(str(wfconfig.worktrees_root()))
    if ".." in a.worktree.replace("\\", "/").split("/") or not wt.startswith(root + os.sep):
        raise ValueError(f"--worktree {a.worktree!r} is not a story worktree under {root}")
    if not _RANGE.match(a.diff_range):
        raise ValueError(f"--diff-range {a.diff_range!r} is not <rev>..<rev>")
    for p in a.module + a.test:
        container_path(p)


def git(a: argparse.Namespace, *args: str) -> str:
    return subprocess.run(["git", "-C", a.worktree, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def run(a: argparse.Namespace) -> tuple[dict, int]:
    end = a.diff_range.split("..", 1)[1]
    diff_lines: dict[str, set[int]] = {}
    starts: dict[str, int] = {}
    unmutable: dict[str, list[int]] = {}
    for module in a.module:
        diff_lines[module] = changed_lines(git(a, "diff", "-U0", "--end-of-options", a.diff_range, "--", module))
        source = git(a, "show", "--end-of-options", f"{end}:{module}")
        found, outside = changed_functions(source, diff_lines[module], dotted_module(module))
        starts.update(found)
        if outside:
            unmutable[module] = outside
    patterns = [prefix + "__mutmut_*" for prefix in starts]
    base = {"modules": a.module, "tests": a.test, "functions": patterns, "unmutable_changes": unmutable}
    if not patterns:
        return {"status": "nothing-to-mutate", "reason": "the diff changes no function or method in these modules",
                **base, "counts": {}, "survivors": [], "survivors_outside_diff": 0}, 0
    service = str(cfg("review.mutation.service", "") or "")
    mutmut = str(cfg("review.mutation.mutmut", "mutmut") or "mutmut")
    if service:
        app_root = str(cfg("review.mutation.app_root", "/app"))
        compose = shlex.split(str(cfg("stack.compose_cmd", "docker compose")))
        cmd = [*compose, "-p", a.project_name, "exec", "-T", service, "sh", "-c"]
    else:
        app_root = a.worktree.replace("\\", "/")
        cmd = ["sh", "-c"]
    script = container_script(a.module, a.test, patterns, a.timeout, app_root, mutmut)
    proc = subprocess.run([*cmd, script], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=a.timeout + 120)
    out = proc.stdout
    if proc.returncode != 0 and "@@END@@" not in out:
        return {"status": "blocked", "reason": f"mutation run exit {proc.returncode}: {(proc.stderr or out)[-800:]}"}, 2
    if "@@MUTMUT-MISSING@@" in out:
        return {"status": "blocked", "reason": f"mutmut ({mutmut}) is not installed where the run executes"}, 2
    m = re.search(r"@@RUN-EXIT@@ (\d+)", out)
    run_exit = int(m.group(1)) if m else -1
    log_tail = out.split("@@RUN-EXIT@@", 1)[-1].split("@@RESULTS@@", 1)[0]
    if "nothing matches" in log_tail:
        return {"status": "nothing-to-mutate", "reason": "mutmut generated no mutants for the changed functions",
                **base, "counts": {}, "survivors": [], "survivors_outside_diff": 0}, 0
    results = {n: s for n, s in parse_results(
        out.split("@@RESULTS@@", 1)[-1].split("@@SHOW@@", 1)[0].split("@@END@@", 1)[0]).items()
        if any(fnmatch.fnmatchcase(n, p) for p in patterns)}
    if not results or (run_exit not in (0, 124) and not any(s in ("killed", "survived") for s in results.values())):
        return {"status": "blocked", "reason": "mutmut produced no checked mutants; the story's tests may not run "
                                                f"under pytest. mutmut log tail: {log_tail[-1500:]}"}, 2
    counts: dict[str, int] = {}
    for status in results.values():
        counts[status] = counts.get(status, 0) + 1
    shows = {}
    for block in out.split("@@SHOW@@ ")[1:]:
        name, _, body = block.partition("\n")
        shows[name.strip()] = body.split("@@END@@", 1)[0].strip()
    survivors, outside = [], 0
    for name, status in sorted(results.items()):
        if status != "survived":
            continue
        show = shows.get(name, "")
        offset = starts.get(name.rsplit("__mutmut_", 1)[0], 1) - 1
        mutated = {line + offset for line in mutated_lines(show)}
        file = module_file(name)
        if mutated and mutated & diff_lines.get(file, set()):
            survivors.append({"name": name, "file": file, "lines": f"{min(mutated)}-{max(mutated)}", "diff": show[:3000]})
        else:
            outside += 1
    status = "timeout" if run_exit == 124 else "ok"
    reason = "wall-clock timeout; unchecked mutants are counted as 'not checked'" if status == "timeout" else ""
    return {"status": status, "reason": reason, **base, "counts": counts,
            "survivors": survivors, "survivors_outside_diff": outside}, 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0], allow_abbrev=False)
    ap.add_argument("--project-name", required=True)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--diff-range", required=True)
    ap.add_argument("--module", action="append", required=True)
    ap.add_argument("--test", action="append", required=True)
    ap.add_argument("--timeout", type=int, default=int(cfg("review.mutation.timeout_s", 420)))
    a = ap.parse_args(argv)
    try:
        validate(a)
        report, rc = run(a)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        report, rc = {"status": "blocked", "reason": str(exc)}, 2
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(json.dumps(report, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
