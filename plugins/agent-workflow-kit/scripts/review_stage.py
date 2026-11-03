#!/usr/bin/env python3
"""Stage one review round and print the review workflow's `args` JSON (layered-review pipeline mode, Step 1).

Computes the round's number, kind and diff, and stages every input the workflow needs. A branch round reviews
`<merge-base>..HEAD`, as does the re-run a phase-2 GREEN owes (also past an ended loop). A close-out round reviews
its delta: the close-out patch against the close-out the latest triaged round reviewed (same impl: `<prev end>..HEAD`;
after a rebase: the previous close-out patch replayed onto the new impl in a temporary index; otherwise the whole
close-out patch, with the reason). A new-code round (`--new-code-since`, controlled mode only) reviews
`<close-out before the change>..HEAD`. The story's findings files and record are pipeline output: no staged diff,
replay patch or `changed_files` holds them. The round, its ledger state and the carried findings come from the record
file and every findings file of the story; the loop's refusals come from `review_ledger.review_loop`.

Usage:
    uv run --no-project <plugin-root>/scripts/review_stage.py --story-id <id> --mode controlled|auto --worktree <abs>
        --spec-file <abs>|no-spec --graph-project <name>|none --stack-project-name <stack>|none
        [--passes N] [--snapshot-dir <abs>] [--date YYYY-MM-DD] [--new-code-since <close-out sha>]

Config: git.base_branch, git.branch_prefix, paths.specs_dir, paths.quality_bar_doc, review.* (ui_paths,
ui_test_regex, ui_profile, ui_base_url, ui_seed_cmd, backend_source_regex, backend_test_regex,
source_exclude_regex, pbt_source_regex, mutation.enabled, path_rewrites, chunk_max_lines, required_preamble),
graph.main_project.

Inputs: git in the worktree on a `<config: git.branch_prefix><id>` branch; review_diff_chunks.py, review_carry.py and
        review_spec_acs.py (run as subprocesses with this interpreter).
Output: stdout is one JSON line `{"args": {...}, "seed_required": bool, "seed_cmd": str, "review": {round, kind,
        reviewed, diff_range, how, reason}}`. Writes `<worktree>/.workflow/state/review-runs/round-<n>/` (diff.patch,
        chunks.json, diff-<k>.patch, ui/, claims.txt, args.json, replay/args.json for workflow_replay.mjs) and pins `refs/review/<branch>/round-<n>` at HEAD
        (plus `round-<n>-base` at the replay commit). `delete_review_refs` is imported by story cleanup.
Exit: 0 staged; 1 a step failed (its message on stderr); 3 nothing to review; 4 no round is owed (one stderr line).
Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import _registry_lookup  # noqa: E402
import review_ledger as ledger  # noqa: E402
import wfconfig  # noqa: E402

DEFAULT_UI_TEST = r"\.(?:test|spec)\.[cm]?[jt]sx?$"
DEFAULT_TEST = r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$"
IDENTITY = ("-c", "user.name=review-stage", "-c", "user.email=review-stage@localhost")
WHOLE_FILE = [1, 10**9]  # the new-side range of a changed file that git prints no `@@` hunk for
C_ESCAPES = {b"a": b"\a", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r", b"t": b"\t", b"v": b"\v",
             b"\\": b"\\", b'"': b'"'}
PREFIXES = ("--src-prefix=a/", "--dst-prefix=b/")  # overrides diff.noprefix: diff_hunks and `git apply` read a/ and b/
PLAIN = ("--no-ext-diff", "--no-color")  # keeps a host diff.external or color.diff=always from changing the diff text
UNQUOTED = ("-c", "core.quotepath=false")  # a non-ASCII path comes out as itself, not as git's quoted octal form


class StageError(Exception):
    pass


class Refusal(Exception):
    """No round is owed (exit 4)."""


def _run(cmd: list[str], env: dict | None = None, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, input=data, env=env)


def git(worktree: Path, *args: str, env: dict | None = None) -> str:
    res = _run(["git", "-C", str(worktree), *args], env)
    if res.returncode != 0:
        raise StageError(f"git {' '.join(args)}: {res.stderr.decode('utf-8', 'replace').strip()}")
    return res.stdout.decode("utf-8", "replace")


def git_ok(worktree: Path, *args: str) -> bool:
    return _run(["git", "-C", str(worktree), *args]).returncode == 0


def run_script(name: str, *args: str) -> dict | list:
    res = subprocess.run([sys.executable, str(SCRIPTS / name), *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise StageError(f"{name} exit {res.returncode}: {(res.stdout + res.stderr).strip()}")
    return json.loads(res.stdout)


def posix(path) -> str:
    return str(path).replace("\\", "/")


def cfg(key: str, default):
    try:
        return wfconfig.get(key, default)
    except Exception:
        return default


def run_dir_for(worktree: Path, n: int) -> Path:
    return Path(worktree) / ".workflow" / "state" / "review-runs" / f"round-{n}"


def chunk(diff: Path, keys: tuple[str, ...]) -> tuple[int, list[dict]]:
    plan = run_script("review_diff_chunks.py", "--diff", str(diff), "--max-lines",
                      str(int(cfg("review.chunk_max_lines", 3000))))
    (diff.parent / "chunks.json").write_text(json.dumps(plan), encoding="utf-8")
    return plan["total_lines"], [{k: c[k] for k in keys} for c in plan["chunks"]]


def git_path(token: str) -> str:
    """A path as a git diff header spells it: ended by a TAB when it holds a space, and C-quoted (octal escapes)
    when it holds a special character or, with core.quotepath on, a non-ASCII one."""
    token = token.removesuffix("\t")
    if not (len(token) > 1 and token[0] == token[-1] == '"'):
        return token

    def byte(m: re.Match) -> bytes:
        esc = m.group(1)
        return bytes([int(esc, 8)]) if esc[:1].isdigit() else C_ESCAPES[esc]

    return re.sub(rb'\\([0-3][0-7]{2}|[abfnrtv\\"])', byte, token[1:-1].encode("utf-8")).decode("utf-8", "replace")


def header_path(line: str) -> str | None:
    """The path a `diff --git a/<path> b/<path>` header names, or None when its two sides differ (a rename or a
    copy, which prints the path on a `rename to` / `copy to` line). A path holding ` b/` splits the header at
    more than one place; only the split with equal sides is right."""
    rest = line[len("diff --git "):]
    for m in re.finditer(r' (?="?b/)', rest):
        old, new = git_path(rest[:m.start()]), git_path(rest[m.end():])
        if old.startswith("a/") and new.startswith("b/") and old[2:] == new[2:]:
            return new[2:]
    return None


def diff_hunks(text: str) -> tuple[dict[str, list[list[int]]], list[str]]:
    """({new-side path: [[start, end], ...]}, [paths whose new side is /dev/null]) of a unified git diff.

    A changed file with no `@@` hunk (binary, rename-only, copy-only, mode-only, empty) is changed whole:
    `WHOLE_FILE`."""
    hunks: dict[str, list[list[int]]] = {}
    deleted: list[str] = []
    old = new = None
    in_hunk = False
    bare: dict | None = None  # the open file until its `---` line; a file with no hunk prints none

    def settle() -> None:
        if bare and bare["path"]:
            if bare["gone"]:
                deleted.append(bare["path"])
            else:
                hunks.setdefault(bare["path"], []).append(list(WHOLE_FILE))

    for line in text.splitlines():
        if line.startswith("diff --git "):
            settle()
            bare = {"path": header_path(line), "gone": False}
            old = new = None
            in_hunk = False
        elif in_hunk and not line.startswith("@@"):
            continue
        elif bare and line.startswith(("rename to ", "copy to ")):
            bare["path"] = git_path(line.partition(" to ")[2])
        elif bare and line.startswith("deleted file mode "):
            bare["gone"] = True
        elif line.startswith("--- "):
            bare = None
            token = git_path(line[4:])
            old = None if token == "/dev/null" else re.sub(r"^a/", "", token)
        elif line.startswith("+++ "):
            token = git_path(line[4:])
            if token == "/dev/null":
                if old:
                    deleted.append(old)
                new = None
            else:
                new = re.sub(r"^b/", "", token)
        elif line.startswith("@@") and new:
            in_hunk = True
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                hunks.setdefault(new, []).append([start, start + max(count, 1) - 1])
    settle()
    return hunks, deleted


def arts_of(worktree: Path) -> Path:
    return Path(worktree) / ledger.specs_rel()


def findings_file(worktree: Path, story_id: str, today: str) -> Path:
    arts = arts_of(worktree)
    found = sorted(arts.glob(f"{story_id}-code-review-findings-*.md"))
    return found[0] if found else arts / f"{story_id}-code-review-findings-{today}.md"


def ui_checked_before(text: str) -> bool:
    """An earlier round of the story's findings text lists ui-verifier under "Layers run" and not under "no
    report after retry"."""
    for section in re.split(r"^## Review round ", text, flags=re.MULTILINE)[1:]:
        run = re.search(r"^- Layers run: (.*)$", section, re.MULTILINE)
        missing = re.search(r"^- Layers with no report after retry: (.*)$", section, re.MULTILINE)
        ran = run and re.search(r"(^|, )ui-verifier\b", run.group(1))
        failed = missing and re.search(r"(^|, )ui-verifier\b", missing.group(1))
        if ran and not failed:
            return True
    return False


def slot_of(story_id: str) -> int | None:
    row = _registry_lookup.find(ledger.variants(story_id))
    try:
        return int(str(row.get("slot"))) if row else None
    except (TypeError, ValueError):
        return None


def ui_base_url(stack: str, story_id: str) -> str:
    """`config: review.ui_base_url` rendered with the story slot's ports; "" when unset or no stack runs."""
    template = str(cfg("review.ui_base_url", "") or "")
    if stack == "none" or not template:
        return ""
    url = wfconfig.render(template, story_id=story_id, stack=stack, slot=slot_of(story_id))
    return "" if "{" in url else url.rstrip("/")


def seed_cmd(stack: str, story_id: str) -> str:
    template = str(cfg("review.ui_seed_cmd", "") or "")
    return wfconfig.render(template, story_id=story_id, stack=stack, slot=slot_of(story_id)) if template else ""


def prefix() -> str:
    return str(cfg("git.branch_prefix", "story/"))


def branch_name(head_branch: str) -> str:
    p = prefix()
    return head_branch[len(p):] if p and head_branch.startswith(p) else head_branch


def prompts_dir() -> Path:
    """The installed layered-review skill's review-prompts dir in the main checkout."""
    root = wfconfig.repo_root()
    for tool_dir in (".claude", ".opencode"):
        d = root / tool_dir / "skills" / "layered-review" / "review-prompts"
        if d.is_dir():
            return d
    return SCRIPTS.parent.parent / "skills" / "layered-review" / "review-prompts"


def _rx(key: str, default: str) -> re.Pattern | None:
    raw = str(cfg(key, default) or "")
    return re.compile(raw) if raw else None


def ui_paths() -> list[str]:
    return [str(p).strip("/") for p in (cfg("review.ui_paths", []) or []) if str(p).strip("/")]


def ui_source(path: str) -> bool:
    """A changed file the ui-verifier checks on screen: under `review.ui_paths` and not a test file."""
    test = _rx("review.ui_test_regex", DEFAULT_UI_TEST)
    return any(path == p or path.startswith(p + "/") for p in ui_paths()) and not (test and test.search(path))


def classify(changed: list[str]) -> dict[str, list[str]]:
    """The changed files each execution stage reads: backend sources and tests (test-adequacy), pure-function
    modules (pbt-writer). An empty regex in config turns the class (and its stage) off."""
    source = _rx("review.backend_source_regex", "")
    test = _rx("review.backend_test_regex", DEFAULT_TEST)
    exclude = _rx("review.source_exclude_regex", r"/(migrations|settings)/")
    pbt = _rx("review.pbt_source_regex", "")
    tests = [p for p in changed if source and source.search(p) and test and test.search(p)]
    sources = [p for p in changed if source and source.search(p) and p not in tests
               and not (exclude and exclude.search(p))]
    return {"backend_sources": sources if cfg("review.mutation.enabled", False) else [],
            "backend_tests": tests, "pbt_sources": [p for p in sources if pbt and pbt.search(p)] if pbt else []}


def delete_review_refs(repo: Path, branch: str) -> list[str]:
    """Delete every `refs/review/<branch>/` ref (`<branch>` with or without `story/`) and return them."""
    name = branch_name(branch)
    listing = git(Path(repo), "for-each-ref", "--format=%(refname)", f"refs/review/{name}/")
    refs = [r for r in listing.splitlines() if r]
    for ref in refs:
        git(Path(repo), "update-ref", "-d", ref)
    return refs


def close_out_marks(story_id: str) -> list[str]:
    return [f"[Story {v}] code review close-out" for v in ledger.variants(story_id)]


def close_out_commit(worktree: Path, rng: str, story_id: str) -> str | None:
    """The newest commit of `rng` whose subject is the story's close-out commit."""
    log = git(worktree, "log", "--format=%H%x09%s", rng)
    marks = close_out_marks(story_id)
    for entry in log.splitlines():
        sha, _, subject = entry.partition("\t")
        if any(mark in subject for mark in marks):
            return sha
    return None


def pipeline_pathspec(story_id: str) -> list[str]:
    """The pathspec that leaves the story's own pipeline output out of a diff: its findings files and its record,
    under every spelling of the story id. Machine-written, they are never reviewed."""
    spec = ["--", "."]
    for v in ledger.variants(story_id):
        arts = ledger.specs_rel()
        spec += [f":(exclude){arts}/{v}-code-review-findings*.md", f":(exclude){arts}/{v}-record.md"]
    return spec


def replay(worktree: Path, run_dir: Path, parent_before: str, prev_end: str, base: str, k: int,
           pathspec: list[str]) -> str | None:
    """Replay the close-out patch `parent_before..prev_end` onto `base` in a temporary index; the commit, or None."""
    index = run_dir / "replay.index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index.resolve()))
    try:
        git(worktree, "read-tree", base, env=env)
        patch = _run(["git", "-C", str(worktree), "diff", "--binary", "--full-index", *PREFIXES, *PLAIN,
                      parent_before, prev_end, *pathspec])
        if patch.returncode != 0:
            return None
        if patch.stdout.strip():
            cmd = ["git", "-C", str(worktree), "apply", "--cached"]
            res = _run([*cmd, "--3way"], env, patch.stdout)
            if res.returncode != 0:
                # a git that rejects `--cached --3way`, or a conflict, leaves the index as it was or half-patched
                git(worktree, "read-tree", base, env=env)
                res = _run(cmd, env, patch.stdout)
            if res.returncode != 0 or git(worktree, "ls-files", "-u", env=env).strip():
                return None
        tree = git(worktree, "write-tree", env=env).strip()
        return git(worktree, *IDENTITY, "commit-tree", tree, "-p", base, "-m",
                   f"review delta base: round {k} close-out replayed onto {base[:12]}").strip()
    finally:
        index.unlink(missing_ok=True)


def loop_refusal(state: dict, mode: str) -> str | None:
    """Why the ended review loop owes no round, or None. The round cap, a Loop exit and a new-code round end it in both
    modes; `no patch approved` ends it in auto mode only: a controlled round the owner forces below the cap stages, and
    a forced round at the cap is refused as the cap."""
    ended = state["ended"]
    if not ended:
        return None
    if mode == "auto" or not ended.startswith(ledger.NO_PATCH_PREFIX):
        return ended
    if ledger.close_out_rounds(state["rounds"]) >= ledger.CLOSE_OUT_CAP:
        return ledger.LOOP_CAP_REASON
    return None


def new_code_refusal(state: dict, rerun: int | None) -> str | None:
    """Why a new-code round is not owed, or None: no triaged round precedes it, a triaged new-code round already follows
    the latest triaged branch round, or a re-run is owed (it reviews the whole branch). The round cap and `ended` are not
    asked: `rules-defer` allows the round after any last round, and the ledger cannot see a skipped one."""
    triaged = [r for r in state["rounds"] if r["triaged"]]
    if not triaged:
        return "--new-code-since needs a triaged round before it"
    last_branch = max((r["n"] for r in triaged if ledger.effective_kind(r) == "branch"), default=0)
    if any(ledger.effective_kind(r) == "new-code" and r["n"] > last_branch for r in triaged):
        return "a new-code round already follows the latest branch review, and the rule allows one"
    if rerun:
        return f"a review re-run is due (round {rerun}), and a re-run reviews the whole branch"
    return None


def new_code_start(worktree: Path, story_id: str, since: str, close_out: str | None) -> str:
    """The full sha of `--new-code-since`: a commit carrying the story's close-out subject, with the same parent as
    the current close-out and not the current close-out. The close-out an amend replaced is a sibling of the amended
    one, not its ancestor; the same-parent test also refuses a start on another impl (a rebase between). Raises Refusal."""
    def refuse(why: str) -> Refusal:
        return Refusal(f"no review round is owed: --new-code-since {since!r} {why}")

    rev = None if since.startswith("-") else _run(["git", "-C", str(worktree), "rev-parse", "--verify", "--quiet",
                                                   f"{since}^{{commit}}"])
    if rev is None or rev.returncode != 0:
        raise refuse("is not a commit of this repository")
    sha = rev.stdout.decode("utf-8", "replace").strip()
    if not any(mark in git(worktree, "log", "-1", "--format=%s", sha) for mark in close_out_marks(story_id)):
        raise refuse("is not a close-out commit of this story")
    if close_out is None or sha == close_out:
        raise refuse("is the current close-out commit, or the branch holds none to follow it")
    if git(worktree, "rev-list", "--parents", "-n", "1", sha).split()[1:] != \
            git(worktree, "rev-list", "--parents", "-n", "1", close_out).split()[1:]:
        raise refuse("has another parent than the current close-out: the impl changed between them")
    return sha


def plan_round(a: argparse.Namespace, worktree: Path, mb: str) -> dict:
    """The round's number, kind and ledger state, from disk and git only; raises Refusal."""
    since = a.new_code_since
    if since and a.mode != "controlled":
        raise Refusal("no review round is owed: a new-code round (--new-code-since) is staged in controlled mode only")
    arts = arts_of(worktree)
    record: list[str] = []
    for v in ledger.variants(a.story_id):
        path = arts / f"{v}-record.md"
        if path.is_file():
            record = [line for _n, line in ledger.numbered_record_lines(str(path))]
            break
    ffile = findings_file(worktree, a.story_id, a.date or dt.date.today().isoformat())
    files = ledger.findings_paths(ledger.variants(a.story_id), str(arts))
    text = ledger.findings_text_of(ledger.variants(a.story_id), str(arts))
    headed ={int(m.group(1)) for m in re.finditer(r"^## Review round (\d+)\b", text, re.MULTILINE)}
    n = 1 + max(ledger.recorded_rounds(record) | headed, default=0)
    state = ledger.review_loop(record, text)
    triaged = [r for r in state["rounds"] if r["triaged"]]
    prev = triaged[-1] if triaged else None
    if prev and not prev["finalized"]:
        raise Refusal(f"no review round is owed: round {prev['n']}'s finalize has not run "
                      f"(no phase-3 GREEN line after its review round {prev['n']} recorded line)")
    close_out = close_out_commit(worktree, f"{mb}..HEAD", a.story_id) if prev else None
    start = ""
    if since:
        why = new_code_refusal(state, ledger.review_rerun_due(str(worktree), a.story_id, record))
        if why:
            raise Refusal(f"no review round is owed: {why}")
        kind, start = "new-code", new_code_start(worktree, a.story_id, since, close_out)
    else:
        by_green = ledger.rerun_owed_by_green(str(worktree), a.story_id, record)
        latest = state["rounds"][-1] if state["rounds"] else None
        # A re-run repeats the halted round's kind, and a new-code round's start comes only from --new-code-since. A
        # phase-2 GREEN owes a whole-branch re-run instead, which the flag path refuses (new_code_refusal).
        if latest and not latest["triaged"] and ledger.effective_kind(latest) == "new-code" and not by_green:
            raise Refusal(f"no review round is owed: round {latest['n']} is a new-code round that halted, "
                          "and staging it again needs --new-code-since")
        why = None if by_green else loop_refusal(state, a.mode)
        if why:
            raise Refusal(f"no review round is owed: the review loop ended: {why}")
        kind = "close-out" if prev and close_out and not by_green else "branch"
    return {"n": n, "kind": kind, "prev": prev, "close_out": close_out, "since": start, "findings": ffile,
            "files": files, "text": text}


def stage_diff(worktree: Path, run_dir: Path, plan: dict, mb: str, head: str) -> dict:
    """The reviewed range and the staged range, with the delta's `how`; nothing is written outside `run_dir`."""
    if plan["kind"] == "branch":
        rng = f"{mb}..{head}"
        return {"reviewed": (mb, head), "diff_range": rng, "how": "branch", "reason": "", "against": None,
                "replay": None}
    if plan["kind"] == "new-code":
        return {"reviewed": (plan["since"], head), "diff_range": f"{plan['since']}..{head}", "how": "since",
                "reason": "", "against": None, "replay": None}
    base = git(worktree, "rev-parse", f"{plan['close_out']}^").strip()
    reviewed = (base, head)
    whole = {"reviewed": reviewed, "diff_range": f"{base}..{head}", "how": "whole", "against": None, "replay": None}
    prev = plan["prev"]
    k = prev["n"]
    if not (prev["kind"] and prev["base"] and prev["end"]):
        return {**whole, "reason": f"round {k} recorded no reviewed range"}
    if not git_ok(worktree, "cat-file", "-e", f"{prev['end']}^{{commit}}"):
        return {**whole, "reason": f"round {k}'s reviewed end {prev['end'][:12]} is not in the repository"}
    p_commit = close_out_commit(worktree, f"{prev['base']}..{prev['end']}", plan["story_id"])
    if p_commit is None:
        return {**whole, "reason": f"round {k} reviewed no close-out"}
    p_parent = git(worktree, "rev-parse", f"{p_commit}^").strip()
    against = {"round": k, "end": prev["end"]}
    if git(worktree, "rev-parse", f"{p_parent}^{{tree}}").strip() == git(worktree, "rev-parse", f"{base}^{{tree}}").strip():
        return {"reviewed": reviewed, "diff_range": f"{prev['end']}..{head}", "how": "same-impl", "reason": "",
                "against": against, "replay": None}
    replayed = replay(worktree, run_dir, p_parent, prev["end"], base, k, pipeline_pathspec(plan["story_id"]))
    if replayed is None:
        return {**whole, "reason": f"round {k}'s close-out patch does not apply onto {base[:12]}"}
    return {"reviewed": reviewed, "diff_range": f"{replayed}..{head}", "how": "replayed", "reason": "",
            "against": against, "replay": replayed}


def stage(a: argparse.Namespace) -> tuple[int, dict]:
    worktree = Path(a.worktree)
    today = a.date or dt.date.today().isoformat()
    head_branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not head_branch.startswith(prefix()):
        raise StageError(f"HEAD is on {head_branch!r}; review_stage.py stages a {prefix()}<id> branch")
    mb = git(worktree, "merge-base", str(cfg("git.base_branch", "main")), "HEAD").strip()
    head = git(worktree, "rev-parse", "HEAD").strip()
    plan = plan_round(a, worktree, mb)
    plan["story_id"] = a.story_id
    n, ffile = plan["n"], plan["findings"]

    run_dir = run_dir_for(worktree, n)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "ui").mkdir(parents=True)
    staged = stage_diff(worktree, run_dir, plan, mb, head)
    diff_range = staged["diff_range"]
    pathspec = pipeline_pathspec(a.story_id)
    git(worktree, *UNQUOTED, "diff", *PREFIXES, *PLAIN, f"--output={run_dir / 'diff.patch'}", diff_range, *pathspec)
    total, diff_chunks = chunk(run_dir / "diff.patch", ("path", "lines", "pages"))
    if total == 0:
        return 3, {}

    branch_files = [git_path(ln) for ln in git(worktree, *UNQUOTED, "diff", *PLAIN, "--name-only",
                                               f"{mb}..{head}").splitlines() if ln]
    profile = str(cfg("review.ui_profile", "") or "").strip("/")
    if ui_checked_before(plan["text"]) and profile not in branch_files:
        ui_range = diff_range
    else:
        ui_range = f"{mb}..{head}"
    ui_chunks: list[dict] = []
    if ui_paths() and profile:
        git(worktree, *UNQUOTED, "diff", *PREFIXES, *PLAIN, f"--output={run_dir / 'ui' / 'diff.patch'}", ui_range,
            "--", *ui_paths())
        ui_total, ui_chunks = chunk(run_dir / "ui" / "diff.patch",
                                    ("path", "lines", "pages", "files", "test_titles", "test_viewports"))
        if ui_total == 0:
            ui_chunks = []

    base, end = staged["reviewed"]
    claims = run_dir / "claims.txt"
    log = git(worktree, "log", "--format=%B", f"{base}..{end}")
    if log.strip():
        claims.write_text(log, encoding="utf-8")
    changed = [git_path(ln) for ln in git(worktree, *UNQUOTED, "diff", *PLAIN, "--name-only",
                                          diff_range, *pathspec).splitlines() if ln]
    carried = run_script("review_carry.py", "--findings-file", *plan["files"], "--round", str(n)) \
        if plan["files"] else []
    spec_acs = [] if a.spec_file == "no-spec" else run_script("review_spec_acs.py", "--spec", a.spec_file)["spec_acs"]
    hunks, deleted = diff_hunks((run_dir / "diff.patch").read_text(encoding="utf-8", errors="replace"))
    delta = {"kind": plan["kind"], "reviewed": {"base": base, "end": end}, "against": staged["against"],
             "how": staged["how"], "replay": staged["replay"], "reason": staged["reason"], "lines": total,
             "hunks": hunks, "deleted": deleted}
    url = ui_base_url(a.stack_project_name, a.story_id) if ui_chunks else ""
    bar_doc = str(cfg("paths.quality_bar_doc", "") or "").strip("/")
    preamble = str(cfg("review.required_preamble", "") or "")
    if not preamble:
        raise StageError("config review.required_preamble is empty; every review dispatch must lead with it")

    args = {
        "story_id": a.story_id, "round": n, "mode": a.mode, "passes": a.passes, "date": today,
        "diff_range": diff_range, "worktree": posix(worktree), "run_dir": posix(run_dir),
        "diff_chunks": diff_chunks, "claims_file": posix(claims) if log.strip() else "",
        "spec_file": "" if a.spec_file == "no-spec" else a.spec_file,
        "graph_project": "" if a.graph_project == "none" else a.graph_project,
        "snapshot_dir": posix(a.snapshot_dir),
        "prompts_dir": posix(prompts_dir()),
        "repo_root": posix(wfconfig.repo_root()), "findings_file": posix(ffile),
        "stack_project_name": "" if a.stack_project_name == "none" else a.stack_project_name,
        "changed_files": changed, "branch_files": branch_files, "ui_base_url": url, "ui_chunks": ui_chunks,
        "spec_acs": spec_acs, "carried": carried, "delta": delta,
        **classify(changed),
        "ui_sources": sorted({f for c in ui_chunks for f in c["files"] if ui_source(f)}),
        "ui_profile": posix(worktree / profile if profile in branch_files else wfconfig.repo_root() / profile)
        if profile else "",
        "ui_profile_from_branch": bool(profile) and profile in branch_files,
        "preamble": preamble,
        "quality_bar_doc": posix(worktree / bar_doc) if bar_doc else "",
        "graph_main_project": str(cfg("graph.main_project", "") or ""),
        "path_rewrites": [list(map(str, p)) for p in (cfg("review.path_rewrites", []) or [])
                          if isinstance(p, (list, tuple)) and len(p) == 2],
        "worktrees_dir": wfconfig.worktrees_root().name,
    }
    (run_dir / "args.json").write_text(json.dumps(args, indent=1), encoding="utf-8")
    (run_dir / "replay").mkdir()
    (run_dir / "replay" / "args.json").write_text(json.dumps(args, indent=1), encoding="utf-8")

    pin = f"refs/review/{branch_name(head_branch)}/round-{n}"
    git(worktree, "update-ref", pin, head)
    if staged["replay"]:
        git(worktree, "update-ref", f"{pin}-base", staged["replay"])
    seed = bool(url) and a.spec_file != "no-spec" and not a.snapshot_dir and bool(args["ui_sources"]) \
        and bool(cfg("review.ui_seed_cmd", ""))
    review = {"round": n, "kind": plan["kind"], "reviewed": f"{base}..{end}", "diff_range": diff_range,
              "how": staged["how"], "reason": staged["reason"]}
    return 0, {"args": args, "seed_required": seed, "seed_cmd": seed_cmd(a.stack_project_name, a.story_id) if seed
               else "", "review": review}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--story-id", required=True)
    ap.add_argument("--mode", required=True, choices=("controlled", "auto"))
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--spec-file", required=True)
    ap.add_argument("--graph-project", required=True)
    ap.add_argument("--stack-project-name", required=True)
    ap.add_argument("--snapshot-dir", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--new-code-since", default="")
    a = ap.parse_args(argv)
    try:
        code, out = stage(a)
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except (StageError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if code == 3:
        print("total_lines 0: nothing to review", file=sys.stderr)
        return 3
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
