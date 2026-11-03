#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Run story-scoped tests, print a PARSED summary, and record a green run.

Keeps raw runner output out of the calling agent's context and makes the phase GREEN
record line a product of the run itself, not of the agent. Per leg it prints one verdict
header, the COMPLETE list of failing test ids (the batch-fix rule needs the full list) and
per-failure detail (capped); the full raw log of each leg is saved under
`.workflow/state/logs/scoped/` and its path printed.

Legs (at least one; run in this order):
  default    positional labels → `config: tests.scoped_cmd` ({labels} = the labels,
             shell-quoted), parsed by `config: tests.scoped_parser`.
  named      `--leg NAME=VALUE` (repeatable; values of one leg accumulate) → the entry of
             `config: tests.scoped_legs` named NAME: {name, cmd, parser, strip_prefix?,
             fail_id_regex?, summary_regex?}. `strip_prefix` is removed from each value (a
             worktree-relative path given where the runner's root is a subdirectory).
  scripts    `--scripts` → the host script-tests leg: `config: tests.scripts_cmd` run on the
             HOST in the worktree (no container exec), parsed as unittest; zero tests ran is an
             infra error. `--scripts-test <dir>/test_x.py` (repeatable) runs only those files
             (as dotted modules) with the same parsing; refused with --scripts and --record (a
             partial leg is never a GREEN line); a file absent from the worktree is NOT RUN.

Parsers (`parser` key): "regex" (`fail_id_regex` / `summary_regex` of the leg, else
`config: tests.*`), "unittest" (`Ran N tests` + OK/FAILED, blocks split on `=====`),
"vitest" (per-file lines, `Test Files`/`Tests` summary, ` FAIL ` blocks) and "playwright"
(list reporter: `Running N tests`, numbered failure blocks, footer counts and listed ids).
For vitest/playwright the verdict is the runner's exit code; the parse supplies the detail.

--require-synced <path>: a worktree-relative file that must exist in the container serving
    it (routing: `config: stack.health.sync_mounts`, as check_container_sync.py) before any
    leg runs. Polled `config: tests.sync_wait_attempts` times every `tests.sync_wait_poll_s`
    with a heartbeat; still absent → NOT SYNCED, exit 2, no leg run. A file write often
    restarts the service under compose watch; the same poll waits that out.
--record <story_id> <phase>: phase ∈ phase-2 | phase-3. Refused (exit 2, nothing run) unless
    the story holds a slot and (with a stack) the stack is the story's stack. Adds
    `config: tests.record_invariant_labels` to the default leg (when it runs) and runs
    `config: tests.record_checks` ([{name, cmd}], e.g. lint and static checks on the changed
    files) after the legs; a non-zero check fails the run like a failing leg. Only when
    EVERY leg and check passed, appends via story_record.py:
      "<phase> tests GREEN -- run_scoped_tests <legs> (exit 0) HEAD <sha12> code-tree <sha12>
       [scripts-tree <sha12>]"
    `code-tree` hashes the working `config: tests.code_root` subtree (untracked files
    included, .gitignore respected, the real index untouched; with root "." the
    `config: paths.specs_dir` subtree is excluded so record/spec edits never change it); it
    equals `git rev-parse HEAD:<root>` once committed. `scripts-tree` (only when --scripts
    ran) hashes `config: tests.scripts_root` the same way. The line carries fixed vocabulary
    only (no runner text, id or path). The invocation is then saved, shell-quoted, to
    `.workflow/state/story-setup/<story_id>-<phase>.command` (the last green run of a phase
    defines its scope); a failed save prints `command not saved` and keeps exit 0.

Usage:
    uv run --no-project .workflow/scripts/run_scoped_tests.py --story-id <id> <label> [...]
        [--leg NAME=VALUE]... [--scripts | --scripts-test PATH...] [--require-synced PATH]...
        [--record <id> <phase>] [--tail N] [--timeout SECONDS]
    --story-id resolves worktree/branch/slot from the slot registry and the stack name from
    `config: slots.stack_prefix`; --project-name overrides the stack name. With neither, the
    shared stack (`config: slots.shared_stack_name`) and the main checkout are used.
    --tail N: lines per failure block — the last N of a traceback-style block, the first N
    of a vitest/playwright failure (its assertion comes first).

Exit codes (fails CLOSED):
    0  all green (and recorded, when --record was given)
    1  test failures/errors, or a failing record check
    2  infra/config error (command unset or missing a `config: tests_policy.required_flags`
       entry, story not in registry, no result line, timeout, not synced, refused record)
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _registry_lookup  # noqa: E402

DETAIL_CAP = 15  # max failure blocks printed in full; ids are always all printed
FILE_CAP = 60    # max vitest per-file lines printed
DEFAULT_FAIL_RE = r"^(?:FAIL|ERROR): (\S+)"
DEFAULT_SUMMARY_RE = r"^(?:Ran \d+ tests?|=+ .* in [\d.]+s)"
RECORD_PHASES = ("phase-2", "phase-3")
KILL_DRAIN_SECONDS = 10

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# unittest / Django-style runner
_BLOCK_SEP = re.compile(r"^={10,}\s*$", re.MULTILINE)
_UT_RESULT_RE = re.compile(r"Ran (\d+) tests? in ([\d.]+)s")
_UT_VERDICT_RE = re.compile(r"^(OK|FAILED)\b(.*)$", re.MULTILINE)
_UT_HEAD_RE = re.compile(r"^(FAIL|ERROR): (\S+(?: \([^)]+\))?)")
# vitest default reporter
_VITEST_FILE_RE = re.compile(r"^ [✓❯×↓] \S+ \(\d+ tests?\b")
_VITEST_SUMMARY_RE = re.compile(r"^\s*(?:Test Files|Tests|Snapshots|Errors)  \S")
_VITEST_FAIL_RE = re.compile(r"^ FAIL  (.+)$")
_VITEST_RULE_RE = re.compile(r"^⎯")
# Playwright list reporter (ANSI stripped)
_PW_RUNNING_RE = re.compile(r"^Running \d+ tests? using \d+ workers?")
_PW_BLOCK_RE = re.compile(r"^  \d+\) (\[[\w-]+\] › .+?)\s*$")
_PW_FOOTER_HEAD_RE = re.compile(r"^  (\d+) (failed|interrupted|flaky|skipped|did not run)\s*$")
_PW_FOOTER_ITEM_RE = re.compile(r"^    (\[[\w-]+\] › .+?)\s*$")
_PW_PASSED_RE = re.compile(r"^  (\d+) passed \(([^)]+)\)\s*$")
_PW_FATAL_RE = re.compile(r"^  (\d+) errors? (?:was|were) not a part of any test\b")
PW_LISTED = ("failed", "interrupted", "flaky")
PW_COUNTS = ("passed", "failed", "interrupted", "flaky", "skipped", "did not run", "fatal")


# --------------------------------------------------------------------------- regex parser

def fail_regex(pattern: str | None = None) -> re.Pattern:
    return re.compile(pattern or str(wfconfig.get("tests.fail_id_regex", DEFAULT_FAIL_RE)), re.MULTILINE)


def summary_regex(pattern: str | None = None) -> re.Pattern:
    return re.compile(pattern or str(wfconfig.get("tests.summary_regex", DEFAULT_SUMMARY_RE)), re.MULTILINE)


def _fail_id(m: re.Match) -> tuple[str, str]:
    """(kind, id) from a fail_id_regex match."""
    gd = m.groupdict()
    kind = gd.get("kind") or m.group(0).split(":", 1)[0].strip() or "FAIL"
    if gd.get("id"):
        tid = gd["id"]
    elif m.re.groups >= 1 and m.group(1):
        tid = m.group(1)
    else:
        tid = m.group(0).strip()
    return kind, tid


def parse_output(text: str, returncode: int,
                 fail_re: re.Pattern | None = None,
                 summary_re: re.Pattern | None = None) -> dict:
    """Parse runner output into {"status", "summary", "failures", "ids"}.

    status: "infra" — no summary line (the runner never produced a result);
            "pass"  — exit 0 and no failing id;
            "fail"  — otherwise (a non-zero exit with no parsed id still fails).
    failures: [{"kind", "id", "block"}] in output order; block = the output from the
    failing-id line up to the next failing-id or summary line.
    """
    fail_re = fail_re or fail_regex()
    summary_re = summary_re or summary_regex()
    lines = text.splitlines()
    summary = [ln for ln in lines if summary_re.search(ln)]
    if not summary:
        return {"status": "infra", "summary": [], "failures": [], "ids": []}

    starts: list[tuple[int, re.Match]] = []
    for i, ln in enumerate(lines):
        m = fail_re.search(ln)
        if m:
            starts.append((i, m))
    stops = {i for i, _ in starts} | {i for i, ln in enumerate(lines) if summary_re.search(ln)}
    failures = []
    for i, m in starts:
        end = next((j for j in sorted(stops) if j > i), len(lines))
        block = "\n".join(lines[i:end]).rstrip()
        block = re.sub(r"\n[=\-]{10,}\s*$", "", block)
        kind, tid = _fail_id(m)
        failures.append({"kind": kind, "id": tid, "block": block})
    ids: list[str] = []
    for f in failures:
        if f["id"] not in ids:
            ids.append(f["id"])
    status = "pass" if returncode == 0 and not failures else "fail"
    return {"status": status, "summary": summary, "failures": failures, "ids": ids}


# --------------------------------------------------------------------------- runner parsers

def parse_unittest_output(text: str) -> dict:
    """unittest / Django runner output → {status, ran, time, failures}. The runner's own
    summary is the LAST `Ran N tests` line (test output may quote earlier ones)."""
    matches = list(_UT_RESULT_RE.finditer(text))
    if not matches:
        return {"status": "infra", "ran": 0, "time": 0.0, "failures": []}
    m = matches[-1]
    ran, secs = int(m.group(1)), float(m.group(2))
    verdict = _UT_VERDICT_RE.search(text, m.end())
    ok = bool(verdict and verdict.group(1) == "OK")
    failures = []
    for block in _BLOCK_SEP.split(text[:m.start()]):
        head = _UT_HEAD_RE.match(block.strip())
        if head:
            failures.append({"kind": head.group(1), "id": head.group(2), "block": block.strip()})
    return {"status": "pass" if ok and not failures else "fail", "ran": ran, "time": secs,
            "failures": failures}


def parse_vitest_output(text: str) -> dict:
    """vitest default-reporter output (ANSI stripped): per-file lines, summary lines and every
    ` FAIL ` header (failed tests and suites that failed to load) with its detail block."""
    files, summary, failures, current = [], [], [], None
    for ln in text.splitlines():
        if _VITEST_RULE_RE.match(ln):
            current = None
            continue
        head = _VITEST_FAIL_RE.match(ln)
        if head:
            current = {"id": head.group(1).strip(), "block": []}
            failures.append(current)
        elif current is not None:
            current["block"].append(ln)
        elif _VITEST_FILE_RE.match(ln):
            files.append(ln.strip())
        elif _VITEST_SUMMARY_RE.match(ln):
            summary.append(ln.strip())
    return {"files": files, "summary": summary, "failures": failures,
            "has_result": any(s.startswith("Test Files") for s in summary),
            "no_files": "No test files found" in text}


def parse_playwright_output(text: str) -> dict:
    """Playwright list-reporter output (ANSI stripped): the `Running` line, footer counts and
    duration, the ids under each footer heading, and the numbered failure blocks."""
    running, duration, counts, listed, blocks = None, None, {}, {}, []
    block, head = None, None
    for ln in text.splitlines():
        if _PW_RUNNING_RE.match(ln):
            running = ln.strip()
            continue
        m = _PW_BLOCK_RE.match(ln)
        if m:
            block, head = {"id": m.group(1), "block": []}, None
            blocks.append(block)
            continue
        m = _PW_FOOTER_HEAD_RE.match(ln)
        if m:
            block, head = None, m.group(2)
            counts[head] = int(m.group(1))
            listed[head] = []
            continue
        m = _PW_PASSED_RE.match(ln)
        if m:
            block, head = None, None
            counts["passed"], duration = int(m.group(1)), m.group(2)
            continue
        m = _PW_FATAL_RE.match(ln)
        if m:
            block, head = None, None
            counts["fatal"] = int(m.group(1))
            continue
        if head is not None:
            item = _PW_FOOTER_ITEM_RE.match(ln)
            if item:
                listed[head].append(item.group(1))
                continue
            head = None
        if block is not None:
            block["block"].append(ln)
    return {"running": running, "duration": duration, "counts": counts, "listed": listed,
            "blocks": blocks, "has_result": bool(counts)}


# --------------------------------------------------------------------------- config / target

def missing_required_flags(cmd: str) -> list[str]:
    """`config: tests_policy.required_flags` entries absent from a rendered test command."""
    return [str(f) for f in (wfconfig.get("tests_policy.required_flags", []) or [])
            if str(f) not in cmd]


def quote_labels(labels: list[str]) -> str:
    """Shell-quote labels for the platform shell (POSIX sh vs Windows cmd)."""
    if os.name == "nt":
        return subprocess.list2cmdline(labels)
    return " ".join(shlex.quote(x) for x in labels)


def resolve_target(story_id: str | None, project_name: str | None) -> tuple[dict, str | None]:
    """(render context, error). Context keys: stack, worktree, slot, story_id, branch."""
    if story_id:
        row = _registry_lookup.find(story_id)
        if not row:
            return {}, (f"story-id '{story_id}' not found in slot registry: "
                        f"{_registry_lookup.registry_path()}")
        if not row.get("worktree_path"):
            return {}, f"story-id '{story_id}' has no worktree path in the slot registry"
        return {
            "stack": project_name or wfconfig.stack_name(story_id),
            "worktree": str(row["worktree_path"]),
            "slot": row["slot"],
            "story_id": story_id,
            "branch": row.get("branch") or wfconfig.branch_name(story_id),
        }, None
    return {
        "stack": project_name or str(wfconfig.get("slots.shared_stack_name", wfconfig.get("project.name", "project"))),
        "worktree": str(wfconfig.repo_root()),
        "slot": int(wfconfig.get("slots.shared_slot", 0)),
        "story_id": None,
        "branch": str(wfconfig.get("git.base_branch", "development")),
    }, None


def run_env(ctx: dict) -> dict[str, str]:
    env = wfconfig.compose_env(int(ctx["slot"]))
    if wfconfig.stack_enabled():
        env["COMPOSE_PROJECT_NAME"] = str(ctx["stack"])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def named_legs() -> dict[str, dict]:
    out = {}
    for leg in wfconfig.get("tests.scoped_legs", []) or []:
        if isinstance(leg, dict) and leg.get("name") and str(leg.get("cmd", "")).strip():
            out[str(leg["name"])] = leg
    return out


def scripts_tests_dir() -> str:
    return str(wfconfig.get("tests.scripts_tests_dir", "scripts/tests")).strip("/")


def scripts_command(modules: list[str] | None = None) -> str:
    """The --scripts leg's shell command; `modules` (dotted names) limits it to those."""
    py = str(wfconfig.get("tests.scripts_python", "") or "") or shlex.quote(sys.executable)
    if modules:
        return f"{py} -m unittest {' '.join(shlex.quote(m) for m in modules)}"
    tpl = str(wfconfig.get("tests.scripts_cmd", "") or "")
    if tpl.strip():
        return wfconfig.render(tpl, python=py, scripts_tests_dir=scripts_tests_dir())
    return (f"{py} -m unittest discover -s {shlex.quote(scripts_tests_dir())} "
            f"-p 'test_*.py' -t .")


def scripts_test_module(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".")


def scripts_test_present(worktree: str, path: str) -> bool:
    """True when `worktree` holds the file under exactly this spelling (a case-insensitive
    filesystem's `isfile` accepts a case variant that unittest then cannot import)."""
    folder, name = os.path.split(os.path.join(worktree, path))
    try:
        return name in os.listdir(folder) and os.path.isfile(os.path.join(folder, name))
    except OSError:
        return False


# --------------------------------------------------------------------------- sync wait

def container_target(path: str) -> tuple[str, str] | None:
    """(service, container path) serving a worktree-relative file, or None when unmapped."""
    import check_container_sync as ccs
    if not ccs.MOUNTS:
        ccs.MOUNTS[:] = ccs.load_mounts()
    crel, services, root = ccs._route(path)
    if crel is None or not services:
        return None
    return services[0], f"{root}/{crel}"


def wait_synced(stack: str, paths: list[str], env: dict, attempts: int, poll: float) -> list[str]:
    """The paths still absent from their container after `attempts` checks."""
    compose = shlex.split(str(wfconfig.get("stack.compose_cmd", "docker compose")))
    pending = list(paths)
    for i in range(attempts):
        still = []
        for p in pending:
            tgt = container_target(p)
            r = subprocess.run([*compose, "-p", stack, "exec", "-T", tgt[0], "test", "-f", tgt[1]],
                               capture_output=True, env=env) if tgt else None
            if r is None or r.returncode != 0:
                still.append(p)
        pending = still
        if not pending or i == attempts - 1:
            break
        print(f"[SCOPED] [{time.strftime('%H:%M:%S')}] waiting for {len(pending)} file(s) to reach "
              f"the container: {', '.join(pending)}", flush=True)
        time.sleep(poll)
    return pending


# --------------------------------------------------------------------------- record

def green_line(phase: str, summaries: list[str], head: str, tree: str,
               scripts_tree: str | None = None) -> str:
    """The record line for a green run. Fixed vocabulary only: the ledger rejects a verdict
    line carrying red/fail/error/pending/not run as whole words, so no runner text, test id or
    path is ever echoed into it."""
    line = (f"{phase} tests GREEN -- run_scoped_tests {', '.join(s for s in summaries if s)} (exit 0) "
            f"HEAD {head} code-tree {tree}")
    return f"{line} scripts-tree {scripts_tree}" if scripts_tree else line


def _git(worktree: str, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                          check=True, env=env, timeout=120).stdout.strip()


def tested_tree(worktree: str, root: str | None = None) -> tuple[str, str]:
    """(HEAD sha12, tree sha12 of the working `root` subtree incl. untracked files), via a
    throwaway index. root "." hashes the whole tree minus `config: paths.specs_dir`."""
    root = (root if root is not None else str(wfconfig.get("tests.code_root", ".") or ".")).strip("/") or "."
    head = _git(worktree, "rev-parse", "--short=12", "HEAD")
    index = os.path.join(worktree, _git(worktree, "rev-parse", "--git-path", "index"))
    with tempfile.TemporaryDirectory(prefix="scoped-index-") as tmp:
        tmp_index = os.path.join(tmp, "index")
        if os.path.isfile(index):
            shutil.copyfile(index, tmp_index)
        env = dict(os.environ, GIT_INDEX_FILE=tmp_index)
        _git(worktree, "add", "-A", "--", root, env=env)
        if root == ".":
            specs = str(wfconfig.get("paths.specs_dir", "") or "").strip("/")
            excluded = [p for p in (specs, ".workflow/state") if p]
            for p in excluded:
                _git(worktree, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", p, env=env)
            tree = _git(worktree, "write-tree", env=env)[:12]
        else:
            tree = _git(worktree, "write-tree", f"--prefix={root}/", env=env)[:12]
    return head, tree


def record_target(story_id: str, ctx: dict) -> tuple[str | None, str]:
    """(worktree, "") when this run may be recorded for the story, else (None, reason)."""
    row = _registry_lookup.find(story_id)
    if not row or not row.get("worktree_path"):
        return None, f"story {story_id} holds no slot in the slot registry"
    if wfconfig.stack_enabled() and str(ctx["stack"]).lower() != wfconfig.stack_name(story_id).lower():
        return None, f"stack '{ctx['stack']}' is not story {story_id}'s stack ({wfconfig.stack_name(story_id)})"
    return str(row["worktree_path"]), ""


def write_run_command(story_id: str, phase: str, argv: list[str]) -> Path:
    path = wfconfig.state_dir("story-setup") / f"{story_id}-{phase}.command"
    runner = str(wfconfig.get("env.host_tooling_runner", "uv run --no-project"))
    path.write_text(f"{runner} {shlex.join([sys.argv[0], *argv])}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- running

def _save_log(stack: str, leg: str, raw: str) -> str:
    log_dir = wfconfig.state_dir("logs", "scoped")
    with tempfile.NamedTemporaryFile("w", suffix=".log", prefix=f"scoped-{stack}-{leg or 'default'}-",
                                     dir=log_dir, delete=False, encoding="utf-8") as fh:
        fh.write(raw)
        return fh.name


def _kill_tree(proc: subprocess.Popen) -> str:
    """Kill `proc` and its descendants. Returns a note when the tree kill failed, else ''."""
    note = ""
    try:
        if os.name == "nt":
            r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, text=True)
            msgs = re.split(r"(?m)^(?=ERROR:)", r.stderr or "")[1:]
            if r.returncode != 0 and not (msgs and all("no running instance" in m.lower() for m in msgs)):
                note = f"taskkill exited {r.returncode}"
        else:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
    except OSError as exc:
        note = f"tree kill could not run: {exc}"
    finally:
        try:
            proc.kill()
        except OSError:
            pass
    return note


def run_tree(cmd: str, cwd: str, env: dict, timeout: int) -> tuple[int | None, str, str]:
    """(exit code | None on timeout, combined output, note). On timeout the child's whole
    process tree is killed (a plain kill leaves grandchildren holding the pipes, which blocks
    the read); the post-kill drain is bounded by KILL_DRAIN_SECONDS."""
    kw: dict = {}
    if os.name != "nt":
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kw)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", ""
    except subprocess.TimeoutExpired:
        note = _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=KILL_DRAIN_SECONDS)
            drained = "its process tree was killed"
        except subprocess.TimeoutExpired as still:
            out = still.stdout if isinstance(still.stdout, str) else (still.stdout or b"").decode("utf-8", "replace")
            drained = f"its output pipes were still open {KILL_DRAIN_SECONDS}s after the kill"
        return None, out or "", drained + (f" ({note})" if note else "")
    except BaseException:
        _kill_tree(proc)
        raise


def _tail(raw: str, n: int) -> str:
    return "\n".join([ln for ln in raw.splitlines() if ln.strip()][-n:])


def _print_failures(fails: list[dict], tail: int, log_path: str, head_first: bool = False) -> None:
    print("\nAll failing tests:")
    for f in fails:
        print(f"  {f['kind']}: {f['id']}")
    for f in fails[:DETAIL_CAP]:
        lines = f["block"].strip("\n").splitlines()
        print(f"\n--- {f['kind']}: {f['id']}")
        print("\n".join(lines[:tail] if head_first else lines[-tail:]))
    if len(fails) > DETAIL_CAP:
        print(f"\n({len(fails) - DETAIL_CAP} more failure blocks in the raw log: {log_path})")


def _hdr(leg: str) -> str:
    return f"[SCOPED] {leg} " if leg else "[SCOPED] "


def report_regex(leg: str, raw: str, rc: int, log_path: str, tail: int, spec: dict) -> tuple[int, str]:
    parsed = parse_output(raw, rc, fail_regex(spec.get("fail_id_regex")), summary_regex(spec.get("summary_regex")))
    if parsed["status"] == "infra":
        print(f"{_hdr(leg)}INFRA-ERROR — no test-result line in runner output (exit {rc}). Raw log: {log_path}")
        print(_tail(raw, 20))
        return 2, ""
    print(f"{_hdr(leg)}{'PASS' if parsed['status'] == 'pass' else 'FAIL'} exit={rc} "
          f"failing={len(parsed['ids'])} log={log_path}")
    for ln in parsed["summary"][-3:]:
        print(f"  {ln.strip()}")
    if parsed["status"] == "pass":
        return 0, leg or "tests"
    if parsed["failures"]:
        _print_failures(parsed["failures"], tail, log_path)
    else:
        print(f"\nnon-zero exit with no failing id matched by the fail-id regex — inspect the raw log: {log_path}")
        print(_tail(raw, 20))
    return 1, ""


def report_unittest(leg: str, raw: str, rc: int, log_path: str, tail: int, spec: dict) -> tuple[int, str]:
    parsed = parse_unittest_output(raw)
    if parsed["status"] == "infra":
        print(f"{_hdr(leg)}INFRA-ERROR — no `Ran N tests` line in runner output (exit {rc}). Raw log: {log_path}")
        print(_tail(raw, 20))
        return 2, ""
    if spec.get("require_tests") and (parsed["ran"] == 0 or rc == 5):
        print(f"{_hdr(leg)}INFRA-ERROR — no tests ran (ran={parsed['ran']}, exit {rc}). Raw log: {log_path}")
        return 2, ""
    if parsed["status"] == "pass" and rc != 0:
        print(f"{_hdr(leg)}INFRA-ERROR — the runner reported OK but exited {rc}. Raw log: {log_path}")
        return 2, ""
    fails = parsed["failures"]
    print(f"{_hdr(leg)}{'PASS' if parsed['status'] == 'pass' else 'FAIL'} ran={parsed['ran']} "
          f"failures={sum(1 for f in fails if f['kind'] == 'FAIL')} "
          f"errors={sum(1 for f in fails if f['kind'] == 'ERROR')} time={parsed['time']:.1f}s log={log_path}")
    if fails:
        _print_failures(fails, tail, log_path)
    if parsed["status"] != "pass":
        if not fails:
            print(_tail(raw, 20))
        return 1, ""
    return 0, f"{leg or 'tests'} {parsed['ran']} tests"


def report_vitest(leg: str, raw: str, rc: int, log_path: str, tail: int, spec: dict) -> tuple[int, str]:
    parsed = parse_vitest_output(_ANSI_RE.sub("", raw))
    if rc != 0 and not parsed["has_result"]:
        if parsed["no_files"]:
            print(f"{_hdr(leg)}FAIL exit={rc} log={log_path} — no test file matched the filters")
            return 1, ""
        print(f"{_hdr(leg)}INFRA-ERROR — no `Test Files` line in runner output (exit {rc}). Raw log: {log_path}")
        print(_tail(raw, 20))
        return 2, ""
    print(f"{_hdr(leg)}{'PASS' if rc == 0 else 'FAIL'} exit={rc} log={log_path}")
    for line in parsed["files"][:FILE_CAP]:
        print(f"  {line}")
    if len(parsed["files"]) > FILE_CAP:
        print(f"  ({len(parsed['files']) - FILE_CAP} more test-file lines in the raw log)")
    for line in parsed["summary"]:
        print(f"  {line}")
    if rc == 0:
        return 0, leg or "tests"
    fails = [{"kind": "FAIL", "id": f["id"], "block": "\n".join(f["block"])} for f in parsed["failures"]]
    if not fails:
        print("\nNo failing test id in the runner output; its tail:")
        print(_tail(raw, 40))
        return 1, ""
    _print_failures(fails, tail, log_path, head_first=True)
    return 1, ""


def report_playwright(leg: str, raw: str, rc: int, log_path: str, tail: int, spec: dict) -> tuple[int, str]:
    verdict = "PASS" if rc == 0 else "FAIL"
    raw_tail = _tail(raw, 40)
    parsed = parse_playwright_output(_ANSI_RE.sub("", raw))
    if not parsed["has_result"]:
        print(f"{_hdr(leg)}{verdict} exit={rc} log={log_path}")
        if rc != 0:
            print(raw_tail)
            return 1, ""
        return 0, leg or "tests"
    c = parsed["counts"]
    totals = ", ".join(f"{c[k]} {'error(s) outside any test' if k == 'fatal' else k}"
                       for k in PW_COUNTS if k in c)
    duration = f" ({parsed['duration']})" if parsed["duration"] else ""
    print(f"{_hdr(leg)}{verdict} exit={rc} {totals}{duration} log={log_path}")
    if parsed["running"]:
        print(f"  {parsed['running']}")
    kind: dict[str, str] = {}
    for head in PW_LISTED:
        for name in parsed["listed"].get(head, []):
            kind.setdefault(name, head)
            print(f"  {head.upper()}: {name}")
    if rc == 0:
        return 0, leg or "tests"
    blocks = parsed["blocks"]
    if not blocks:
        print("\nNo failure block in the runner output; its tail:")
        print(raw_tail)
        return 1, ""
    for b in blocks[:DETAIL_CAP]:
        print(f"\n--- {kind.get(b['id'], 'failed').upper()}: {b['id']}")
        print("\n".join("\n".join(b["block"]).strip("\n").splitlines()[:tail]))
    if len(blocks) > DETAIL_CAP:
        print(f"\n({len(blocks) - DETAIL_CAP} more failure blocks in the raw log: {log_path})")
    if "fatal" in c:
        print("\nError(s) outside any test; runner tail:")
        print(raw_tail)
    return 1, ""


REPORTERS = {"regex": report_regex, "unittest": report_unittest, "vitest": report_vitest,
             "playwright": report_playwright}


def run_leg(leg: str, cmd: str, spec: dict, ctx: dict, env: dict, cwd: str,
            timeout: int, tail: int) -> tuple[int, str]:
    print(f"[SCOPED] [{time.strftime('%H:%M:%S')}] {leg or 'default'} started", flush=True)
    try:
        rc, raw, note = run_tree(cmd, cwd, env, timeout)
    except OSError as exc:
        print(f"{_hdr(leg)}INFRA-ERROR — cannot launch the command: {exc}")
        return 2, ""
    log_path = _save_log(str(ctx["stack"]), leg, f"$ {cmd}\n{raw}")
    if rc is None:
        print(f"{_hdr(leg)}INFRA-ERROR — run exceeded {timeout}s and {note}. Partial log: {log_path}")
        return 2, ""
    reporter = REPORTERS.get(str(spec.get("parser", "regex")), report_regex)
    return reporter(leg, raw, rc, log_path, tail, spec)


# --------------------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="run scoped tests; print a parsed summary",
                                 allow_abbrev=False)
    ap.add_argument("--story-id", default=None, help="resolve worktree/slot/stack from the slot registry")
    ap.add_argument("--project-name", default=None, help="stack (compose project) name override")
    ap.add_argument("--tail", type=int, default=25, help="lines per failure block")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds before a leg is killed")
    ap.add_argument("--leg", action="append", default=[], metavar="NAME=VALUE",
                    help="one value for the named leg of config tests.scoped_legs; repeat for more")
    ap.add_argument("--scripts", action="store_true", help="run the host script-tests leg")
    ap.add_argument("--scripts-test", action="append", default=[], metavar="PATH",
                    help="one worktree-relative script test file; repeat for more")
    ap.add_argument("--require-synced", action="append", default=[], metavar="PATH",
                    help="worktree-relative file that must exist in its container before any leg runs")
    ap.add_argument("--record", nargs=2, metavar=("STORY_ID", "PHASE"),
                    help="on an all-green run, record '<phase> tests GREEN' for the story")
    ap.add_argument("labels", nargs="*", help="labels for the default leg ({labels} of tests.scoped_cmd)")
    a = ap.parse_intermixed_args(argv)
    legs: dict[str, list[str]] = {}
    known = named_legs()
    for item in a.leg:
        name, sep, value = item.partition("=")
        if not sep or not value:
            ap.error(f"--leg takes NAME=VALUE, got {item!r}")
        if name not in known:
            ap.error(f"--leg {name!r} is not a leg of config tests.scoped_legs "
                     f"(known: {', '.join(sorted(known)) or 'none'})")
        prefix = str(known[name].get("strip_prefix", "") or "")
        value = value.replace("\\", "/")
        legs.setdefault(name, []).append(value.removeprefix(prefix) if prefix else value)
    a.named = legs
    tdir = scripts_tests_dir()
    pat = re.compile(re.escape(tdir) + r"/test_[A-Za-z0-9_]*\.py")
    a.scripts_test = list(dict.fromkeys(p.replace("\\", "/").removeprefix("./") for p in a.scripts_test))
    bad = [p for p in a.scripts_test if not pat.fullmatch(p)]
    if bad:
        ap.error(f"--scripts-test takes a worktree-relative {tdir}/test_<name>.py file; got: {' '.join(bad)}")
    if a.scripts_test and a.scripts:
        ap.error("--scripts already runs the whole script-tests directory: give --scripts or --scripts-test")
    if a.scripts_test and a.record:
        ap.error("--scripts-test runs a partial leg, which is never recorded as GREEN: drop --record, or run --scripts")
    if not (a.labels or a.named or a.scripts or a.scripts_test):
        ap.error("no leg requested: give labels, --leg, --scripts or --scripts-test")
    if a.record and a.record[1] not in RECORD_PHASES:
        ap.error(f"--record phase must be one of {', '.join(RECORD_PHASES)}")
    return a


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        a = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    story_id = a.story_id or (a.record[0] if a.record else None)
    ctx, err = resolve_target(story_id, a.project_name)
    if err:
        print(f"[SCOPED] CONFIG-ERROR — {err}")
        return 2
    env = run_env(ctx)
    worktree = str(ctx["worktree"])

    # ---- build the legs (fail closed on config before anything runs)
    plan: list[tuple[str, str, dict, str]] = []  # (leg, cmd, spec, cwd)
    labels = list(a.labels)
    if a.record and labels:
        labels += [str(x) for x in (wfconfig.get("tests.record_invariant_labels", []) or []) if str(x) not in labels]
    if labels:
        template = str(wfconfig.get("tests.scoped_cmd", "") or "")
        if not template.strip():
            print("[SCOPED] CONFIG-ERROR — tests.scoped_cmd is not set in .workflow/config.toml")
            return 2
        cmd = wfconfig.render(template, labels=quote_labels(labels), **ctx)
        missing = missing_required_flags(cmd)
        if missing:
            print(f"[SCOPED] CONFIG-ERROR — tests.scoped_cmd lacks required flag(s) "
                  f"{', '.join(missing)} (config: tests_policy.required_flags)")
            return 2
        plan.append(("", cmd, {"parser": str(wfconfig.get("tests.scoped_parser", "regex"))}, worktree))
    known = named_legs()
    for name, values in a.named.items():
        spec = known[name]
        plan.append((name, wfconfig.render(str(spec["cmd"]), labels=quote_labels(values), **ctx), spec, worktree))

    if a.record:
        rec_wt, reason = record_target(a.record[0], ctx)
        if rec_wt is None:
            print(f"[SCOPED] NOT RECORDED — {reason}; no leg was run")
            return 2
    if a.scripts_test:
        absent = [p for p in a.scripts_test if not scripts_test_present(worktree, p)]
        if absent:
            print(f"[SCOPED] NOT RUN — script test file absent from {worktree}: {', '.join(absent)}; no leg was run")
            return 2
    if a.scripts or a.scripts_test:
        senv = dict(env, PYTHONIOENCODING="utf-8", PYTHON_COLORS="0", NO_COLOR="1")
        senv.update({str(k): str(v) for k, v in (wfconfig.get("tests.scripts_env", {}) or {}).items()})
        plan.append(("scripts", scripts_command([scripts_test_module(p) for p in a.scripts_test]),
                     {"parser": "unittest", "require_tests": True, "env": senv}, worktree))

    if a.require_synced:
        if not wfconfig.stack_enabled():
            print("[SCOPED] --require-synced SKIPPED — stack.runtime = \"none\" (nothing to sync into)")
        else:
            unmapped = [p for p in a.require_synced if container_target(p) is None]
            if unmapped:
                print(f"[SCOPED] CONFIG-ERROR — no sync mount serves: {', '.join(unmapped)} "
                      f"(config: stack.health.sync_mounts)")
                return 2
            attempts = int(wfconfig.get("tests.sync_wait_attempts", 36))
            poll = float(wfconfig.get("tests.sync_wait_poll_s", 5))
            missing = wait_synced(str(ctx["stack"]), a.require_synced, env, attempts, poll)
            if missing:
                print(f"[SCOPED] NOT SYNCED — absent from the container after {attempts * poll:g} s: "
                      f"{', '.join(missing)}; no leg was run")
                return 2

    worst, summaries = 0, []
    for leg, cmd, spec, cwd in plan:
        rc, summary = run_leg(leg, cmd, spec, ctx, spec.get("env", env), cwd, a.timeout, a.tail)
        worst = max(worst, rc)
        summaries.append(summary)
    if a.record:
        for i, chk in enumerate(wfconfig.get("tests.record_checks", []) or []):
            if not (isinstance(chk, dict) and str(chk.get("cmd", "")).strip()):
                continue
            name = str(chk.get("name") or f"check-{i + 1}")
            cmd = wfconfig.render(str(chk["cmd"]), **ctx)
            print(f"[SCOPED] [{time.strftime('%H:%M:%S')}] {name} started", flush=True)
            rc, raw, note = run_tree(cmd, worktree, env, a.timeout)
            log_path = _save_log(str(ctx["stack"]), name, f"$ {cmd}\n{raw}")
            ok = rc == 0
            print(f"[SCOPED] {name} {'PASS' if ok else 'FAIL'} exit={rc if rc is not None else 'timeout'} log={log_path}")
            if not ok:
                print(_tail(raw, 20))
            worst = max(worst, 0 if ok else (2 if rc is None else 1))
            summaries.append(name if ok else "")
    if worst or not a.record:
        return worst

    story, phase = a.record
    try:
        head, tree = tested_tree(worktree)
        scripts_tree = (tested_tree(worktree, str(wfconfig.get("tests.scripts_root", "scripts")))[1]
                        if a.scripts else None)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[SCOPED] NOT RECORDED — could not hash the tested tree: {exc}")
        return 2
    from story_record import cmd_append
    rc = cmd_append(story, green_line(phase, summaries, head, tree, scripts_tree))
    if rc == 0:
        try:
            write_run_command(story, phase, argv)
        except OSError as exc:
            print(f"[SCOPED] command not saved — {exc}")
    return 0 if rc == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
