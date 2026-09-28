#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Run story-scoped tests and print a PARSED summary.

Keeps raw runner output out of the calling agent's context. Prints one verdict header,
the COMPLETE list of failing test ids (the batch-fix rule needs the full list), and
per-failure output tails (capped); the full raw log is saved to a temp file whose path
is printed.

The command is `config: tests.scoped_cmd`, rendered with `{labels}` (the positional
test labels, shell-quoted and space-joined), `{stack}`, `{worktree}`, `{slot}`,
`{story_id}`, `{branch}`, `{compose}` and every port var of the slot. It runs through
the shell with cwd = the worktree and every slot port var in the environment.
Results are parsed with `config: tests.summary_regex` (a result line — its absence
means the runner never produced a result) and `config: tests.fail_id_regex` (one match
per failing test; capture group `id` or group 1 is the test id, optional group `kind`).

Usage:
    uv run --no-project .workflow/scripts/run_scoped_tests.py --story-id <id> <label> [<label> ...]
    uv run --no-project .workflow/scripts/run_scoped_tests.py --project-name <stack> <label> [...]
        [--tail N] [--timeout SECONDS]

    --story-id resolves the worktree, branch and slot from the slot registry and the stack
    name from `config: slots.stack_prefix`; --project-name overrides the stack name. With
    neither, the shared stack (`config: slots.shared_stack_name`) and the main checkout are used.

Exit codes (fails CLOSED):
    0  all green
    1  test failures/errors
    2  infra/config error (scoped_cmd unset or missing a `config: tests_policy.required_flags`
       entry, story not in registry, runner produced no result line, timeout, shell could
       not launch)
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402
import _registry_lookup  # noqa: E402

DETAIL_CAP = 15  # max failure blocks printed in full; ids are always all printed
DEFAULT_FAIL_RE = r"^(?:FAIL|ERROR): (\S+)"
DEFAULT_SUMMARY_RE = r"^(?:Ran \d+ tests?|=+ .* in [\d.]+s)"


def fail_regex() -> re.Pattern:
    return re.compile(str(wfconfig.get("tests.fail_id_regex", DEFAULT_FAIL_RE)), re.MULTILINE)


def summary_regex() -> re.Pattern:
    return re.compile(str(wfconfig.get("tests.summary_regex", DEFAULT_SUMMARY_RE)), re.MULTILINE)


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


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description="run scoped tests; print a parsed summary")
    ap.add_argument("--story-id", default=None, help="resolve worktree/slot/stack from the slot registry")
    ap.add_argument("--project-name", default=None, help="stack (compose project) name override")
    ap.add_argument("--tail", type=int, default=25, help="output lines per failure block")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds before the run is killed")
    ap.add_argument("labels", nargs="+", help="test labels substituted into {labels}")
    a = ap.parse_args(argv)

    template = str(wfconfig.get("tests.scoped_cmd", "") or "")
    if not template.strip():
        print("[SCOPED] CONFIG-ERROR — tests.scoped_cmd is not set in .workflow/config.toml")
        return 2
    ctx, err = resolve_target(a.story_id, a.project_name)
    if err:
        print(f"[SCOPED] CONFIG-ERROR — {err}")
        return 2
    labels = quote_labels(a.labels)
    cmd = wfconfig.render(template, labels=labels, **ctx)
    missing = missing_required_flags(cmd)
    if missing:
        print(f"[SCOPED] CONFIG-ERROR — tests.scoped_cmd lacks required flag(s) "
              f"{', '.join(missing)} (config: tests_policy.required_flags)")
        return 2

    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=a.timeout, cwd=ctx["worktree"], env=run_env(ctx))
    except subprocess.TimeoutExpired:
        print(f"[SCOPED] INFRA-ERROR — run exceeded {a.timeout}s and was killed")
        return 2
    except OSError as exc:
        print(f"[SCOPED] INFRA-ERROR — cannot launch the scoped command: {exc}")
        return 2

    raw = (r.stdout or "") + "\n" + (r.stderr or "")
    with tempfile.NamedTemporaryFile("w", suffix=".log", prefix=f"scoped-{ctx['stack']}-",
                                     delete=False, encoding="utf-8") as fh:
        fh.write(f"$ {cmd}\n{raw}")
        log_path = fh.name

    parsed = parse_output(raw, r.returncode)
    if parsed["status"] == "infra":
        print(f"[SCOPED] INFRA-ERROR — no test-result line in runner output (exit {r.returncode}). "
              f"Raw log: {log_path}")
        tail = [ln for ln in raw.splitlines() if ln.strip()][-20:]
        print("\n".join(tail))
        return 2

    fails = parsed["failures"]
    verdict = "PASS" if parsed["status"] == "pass" else "FAIL"
    print(f"[SCOPED] {verdict} exit={r.returncode} failing={len(parsed['ids'])} log={log_path}")
    for ln in parsed["summary"][-3:]:
        print(f"  {ln.strip()}")

    if parsed["status"] == "pass":
        return 0
    if fails:
        print("\nAll failing tests:")
        for f in fails:
            print(f"  {f['kind']}: {f['id']}")
        for f in fails[:DETAIL_CAP]:
            blines = f["block"].splitlines()
            print(f"\n--- {f['kind']}: {f['id']}")
            print("\n".join(blines[-a.tail:]))
        if len(fails) > DETAIL_CAP:
            print(f"\n({len(fails) - DETAIL_CAP} more failure blocks in the raw log: {log_path})")
    else:
        print(f"\nnon-zero exit with no failing id matched by tests.fail_id_regex — "
              f"inspect the raw log: {log_path}")
        tail = [ln for ln in raw.splitlines() if ln.strip()][-20:]
        print("\n".join(tail))
    return 1


if __name__ == "__main__":
    sys.exit(main())
