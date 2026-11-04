#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The record of the last green profile-leg run, and how many merges have landed since.

The per-merge gate runs its legs under `config: profiles.gate` only. The profile leg
(`config: profile_leg`) is the same gate plus one more leg run alone under its own serving profile
(`config: profile_leg.profile`, e.g. the real models a deterministic gate fakes), asked for with
`run_full_suite.py --profile-leg`; it is the only run that exercises that profile. Every green one
appends a line to `.workflow/state/<config: profile_leg.state_file>` of the main checkout (one file
for every stack and worktree):

    {"date": ..., "story": ..., "head": ..., "tree": ..., "base": <base-branch commit the tested
     tree was based on: `git merge-base <config: git.base_branch> HEAD`>}

The land window and land-story ask, before each merge:

    uv run --no-project .workflow/scripts/profile_leg.py due [--every N]

which counts the merges on `config: git.base_branch`'s first-parent line after the last line's
`base`, prints the count, and exits 1 when it has reached N (default `config: profile_leg.every`)
— also when no green run is recorded. Exit 0: not due. Exit 2 (cause on stderr): it cannot be told
(the record is unreadable, or the recorded base is not in the repository). Exit 3: no profile leg
is configured (`config: profile_leg.profile` or `.cmd` empty) — the due check is SKIPPED, never due.
Only a green run appends, so a red or partial run never resets the count.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import wfconfig  # noqa: E402

DEFAULT_EVERY = 10
DEFAULT_STATE_FILE = "profile-leg-runs.jsonl"
EXIT_NOT_DUE, EXIT_DUE, EXIT_ERROR, EXIT_NOT_CONFIGURED = 0, 1, 2, 3
RUN_COMMAND = "uv run --no-project .workflow/scripts/run_full_suite.py --story-id <id> --profile-leg --record"


class ProfileLegError(RuntimeError):
    """A git question the counter asked could not be answered."""


def configured() -> bool:
    return bool(str(wfconfig.get("profile_leg.profile", "") or "").strip()
                and str(wfconfig.get("profile_leg.cmd", "") or "").strip())


def label() -> str:
    return str(wfconfig.get("profile_leg.label", "") or "PROFILE-LEG")


def every() -> int:
    return int(wfconfig.get("profile_leg.every", DEFAULT_EVERY) or DEFAULT_EVERY)


def base_branch() -> str:
    return str(wfconfig.get("git.base_branch", "development"))


def state_path() -> Path:
    return wfconfig.state_dir() / str(wfconfig.get("profile_leg.state_file", "") or DEFAULT_STATE_FILE)


def command(ctx: dict) -> str:
    """The rendered leg command; `{specs}` is `config: profile_leg.specs` joined by spaces."""
    specs = " ".join(str(s) for s in (wfconfig.get("profile_leg.specs", []) or []))
    return wfconfig.render(str(wfconfig.get("profile_leg.cmd", "")), specs=specs, **ctx)


def _git(repo, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise ProfileLegError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def record_green(path: Path, *, story: str, head: str, tree: str, base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"date": date.today().isoformat(), "story": story, "head": head, "tree": tree, "base": base}
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry) + "\n")


def last_green(path: Path) -> dict | None:
    """The last readable line of the state file, or None; a line that is not JSON or has no base is skipped."""
    if not path.exists():
        return None
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("base"), str) and entry["base"]:
            last = entry
    return last


def merges_since(base: str, repo, branch: str) -> int:
    return int(_git(repo, "rev-list", "--count", "--first-parent", "--merges", f"{base}..{branch}"))


def due(path: Path, repo, n: int, branch: str) -> tuple[bool, str]:
    """(due, one-line status): due when `n` merges have landed on `branch` since the last green run."""
    entry = last_green(path)
    if entry is None:
        return True, f"profile leg: no green profile-leg run recorded -- DUE ({RUN_COMMAND})"
    count = merges_since(entry["base"], repo, branch)
    since = (f"{count} {'merge' if count == 1 else 'merges'} to {branch} since the last green "
             f"profile-leg run ({entry.get('date', '?')}, {entry.get('story', '?')})")
    if count >= n:
        return True, f"profile leg: {since}; DUE at {n} ({RUN_COMMAND})"
    return False, f"profile leg: {since}; due at {n}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("due", help=f"exit {EXIT_NOT_DUE} not due, {EXIT_DUE} due, {EXIT_ERROR} cannot tell, "
                                   f"{EXIT_NOT_CONFIGURED} no profile leg configured")
    d.add_argument("--every", type=int, default=None, help="merges between runs (default config: profile_leg.every)")
    args = ap.parse_args(argv)
    if not configured():
        print("profile leg: SKIPPED — config: profile_leg.profile / profile_leg.cmd are not set; never due")
        return EXIT_NOT_CONFIGURED
    try:
        is_due, message = due(state_path(), wfconfig.repo_root(), args.every or every(), base_branch())
    except (ProfileLegError, OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"profile leg: cannot tell whether it is due -- {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(message)
    return EXIT_DUE if is_due else EXIT_NOT_DUE


if __name__ == "__main__":
    sys.exit(main())
