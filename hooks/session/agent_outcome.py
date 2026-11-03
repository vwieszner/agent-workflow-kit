#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Agent-scoped Stop hook (fires as SubagentStop): a story agent's completion claim must hold.

Wired with `--role` in a story agent's frontmatter `hooks: Stop:` (Claude Code), or in the
settings `SubagentStop` entry for `repro`:
  * `impl`     — when the final report starts `✅ story-impl complete`: the tracked tree of its
                 `worktree_path` is clean; exactly one story commit (`[Story <id>]` in the subject)
                 since the merge base with `config: git.base_branch`, and it is HEAD (with
                 `seed_base: <sha>` in the dispatch prompt — a seed-AC run — the commits counted are
                 `<seed_base>..HEAD`, and a seed_base that is no ancestor of HEAD is a problem); the
                 latest `phase-2 tests GREEN` record line passes the tested-tree check below.
  * `finalize` — when the final report starts `✅ story-finalize complete`: clean tracked tree; at
                 most two story commits, the second a `code review close-out` commit; every entry
                 in `<config: paths.deferred_work_dir>/story-<id>.md` carries a `Bar impact` line;
                 when the report's `deferred_logged:` is above 0, absent or not a number, that file's
                 last `## Round` section holds at least that many `### ` entries, each with a
                 ``**Tracked in.** `<path>` — `<key>` `` line naming an existing file outside every
                 story worktree that holds `<key>`, is not a deferred-work log and is not one of the
                 story's own spec / findings / record files, with `<key>` reading
                 `<story_id> <finding_id>`; no untracked repro test file is left; the latest
                 `phase-3 tests GREEN` line passes the tested-tree check.
  * `diagnose` — gate mode: the tracked tree of the dispatch's `worktree_path` is clean (`mode:
                 scoped` is not checked: its caller's uncommitted edits are there).
  * `repro`    — for agent_type `repro-writer` / `pbt-writer` only: the tracked tree of the
                 dispatch's `worktree_path` is clean and every untracked file is a repro test
                 (`config: review.repro_test_name_regex`).
  * `propagate` — apply mode, when the report starts `✅ story-propagate applied`: it lists at least
                 one `- updated: <absolute path>` line, and every listed file has an uncommitted
                 change in its checkout.

Tested-tree check: for each path in `config: story_record.tested_trees` (repo-relative dirs,
e.g. ["src", "scripts"]; [] disables the check) the GREEN line must carry
`<label>-tree <hash>` (label = the path with `/` as `-`, `.` as `repo`) and the hash must be
the prefix of `git rev-parse HEAD:<path>` — code changed after the green run is a problem.

A report that does not claim completion passes. A violated claim is answered with a block
decision naming each problem, which keeps the agent working. After CAP blocks of one agent the
stop is allowed and a record line marks its claim unverified. Needs the agent transcript
(`agent_transcript_path`) or `last_assistant_message`; an unreadable payload or a git failure
passes silently. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import wfconfig  # noqa: E402

DEFAULT_REPRO_NAME = r"^(test_(?:repro|pbt)_r\d+_\d+\.py|repro_r\d+_\d+\.test\.[cm]?[jt]sx?|repro-r\d+-\d+\.spec\.[cm]?[jt]s)$"
_TRACKED = re.compile(r"\*\*Tracked in[.:]\*\*\s*`([^`]+)`\s*—\s*`([^`]+)`")
CAP = 3
CLAIMS = {"impl": "✅ story-impl complete", "finalize": "✅ story-finalize complete",
          "propagate": "✅ story-propagate applied"}
_FIELD = r"^\s*{}:\s*(\S.*?)\s*$"


def field(text: str, name: str) -> str | None:
    m = re.search(_FIELD.format(name), text or "", re.MULTILINE)
    return m.group(1).strip("`'\"") if m else None


def variants(story_id: str) -> list[str]:
    d = story_id.replace(".", "-")
    return list(dict.fromkeys([d, story_id, d.replace("-", ".")]))


def repro_name() -> re.Pattern[str]:
    return re.compile(str(wfconfig.get("review.repro_test_name_regex", DEFAULT_REPRO_NAME)))


def specs_rel() -> str:
    return str(wfconfig.get("paths.specs_dir", "docs/implementation-artifacts")).strip("/")


def transcript_texts(path: str | None) -> tuple[str, str]:
    """(first user message, last assistant text) of an agent transcript."""
    first, last = "", ""
    try:
        with open(path or "", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                content = (o.get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(b.get("text", "") for b in content
                                     if isinstance(b, dict) and b.get("type") == "text")
                else:
                    continue
                if o.get("type") == "user" and not first and text:
                    first = text
                elif o.get("type") == "assistant" and text:
                    last = text
    except OSError:
        pass
    return first, last


def git(wt: str, *args: str) -> str:
    return subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True, timeout=30).stdout


def base_branch() -> str:
    return str(wfconfig.get("git.base_branch", "development"))


def story_commits(wt: str, story_id: str, since: str | None = None) -> list[str]:
    base = since or git(wt, "merge-base", "HEAD", base_branch()).strip()
    tags = {f"[Story {v}]" for v in variants(story_id)}
    subjects = git(wt, "log", "--reverse", "--format=%s", f"{base}..HEAD").splitlines()
    return [s for s in subjects if any(t in s for t in tags)]


def record_path(wt: str, story_id: str) -> str:
    return os.path.join(wt, specs_rel(), f"{variants(story_id)[0]}-record.md")


def green_line(wt: str, story_id: str, phase: str) -> str | None:
    try:
        with open(record_path(wt, story_id), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    hit = None
    for line in lines:
        if f"{phase} tests GREEN" in line:
            hit = line
    return hit


def tree_label(path: str) -> str:
    p = path.strip("/").strip()
    return "repo" if p in ("", ".") else p.replace("/", "-")


def tree_problem(wt: str, story_id: str, phase: str) -> str | None:
    trees = [str(t) for t in (wfconfig.get("story_record.tested_trees", []) or [])]
    if not trees:
        return None
    line = green_line(wt, story_id, phase)
    if line is None:
        return (f"no `{phase} tests GREEN` record line — run the scoped tests with their record option for "
                f"{story_id} {phase}")
    for path in trees:
        label = tree_label(path)
        m = re.search(rf"\b{re.escape(label)}-tree ([0-9a-f]{{7,40}})", line)
        if not m:
            return (f"the last {phase} GREEN record line carries no `{label}-tree <hash>` token — re-run the "
                    f"scoped tests with their record option")
        spec = "HEAD^{tree}" if label == "repo" else f"HEAD:{path.strip('/')}"
        head = git(wt, "rev-parse", spec).strip()
        if not head.startswith(m.group(1)):
            return (f"the last {phase} GREEN run tested {label}-tree {m.group(1)}, but {spec} is {head[:12]} — "
                    f"code changed after the green run; re-run the scoped tests")
    return None


def seed_base_problem(wt: str, seed_base: str) -> str | None:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", seed_base):
        return f"seed_base {seed_base!r} does not resolve to a commit"

    def rc(*args: str) -> int:
        return subprocess.run(["git", "-C", wt, *args], capture_output=True, timeout=30).returncode

    if rc("rev-parse", "--verify", "--quiet", f"{seed_base}^{{commit}}") != 0:
        return f"seed_base {seed_base} does not resolve to a commit"
    if rc("merge-base", "--is-ancestor", seed_base, "HEAD") != 0:
        return f"seed_base {seed_base} is not an ancestor of HEAD"
    return None


def comparable(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)).replace("\\", "/").lower()


def story_key(story_id: str) -> str | None:
    r = subprocess.run([sys.executable, str(SCRIPTS / "sprint_status.py"), "get", story_id.replace(".", "-")],
                       capture_output=True, text=True, timeout=60)
    try:
        row = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return row.get("key") if row.get("status") == "ok" else None


def tracked_in_problems(body: str, story_id: str, logged: int | None, wt: str, said: str | None = None) -> list[str]:
    said = said or ("absent" if logged is None else str(logged))
    starts = [m.start() for m in re.finditer(r"(?m)^## Round ", body)]
    if not starts:
        return [f"the deferred-work file has no `## Round N — <date>` section, but deferred_logged is {said} "
                f"— write each deferred entry under one"]
    section = body[starts[-1]:]
    end = re.search(r"(?m)^## ", section[3:])
    if end:
        section = section[:end.start() + 3]
    entries = re.split(r"(?m)^### ", section)[1:]
    need = logged or 1
    out = []
    if len(entries) < need:
        out.append(f"`### ` entries in the last `## Round` section: {len(entries)}; deferred_logged is {said} "
                   f"(needs at least {need}) — write one entry per logged defer")
    key = story_key(story_id)
    own = {f"{key}.md".lower()} if key else set()
    own_rx = re.compile(rf"(?:{'|'.join(re.escape(v) for v in variants(story_id))})-(?:record|code-review-findings-.+)\.md",
                        re.IGNORECASE)
    repo = str(wfconfig.repo_root())
    wt_path = comparable(wt)
    worktrees = comparable(str(wfconfig.worktrees_root()))
    deferred_dir = comparable(os.path.join(repo, str(wfconfig.get("paths.deferred_work_dir",
                                                                   f"{specs_rel()}/deferred-work"))))
    story_keys = tuple(f"{v} " for v in variants(story_id))
    for entry in entries:
        head = f"deferred entry '{entry.splitlines()[0]}' **Tracked in.**"
        m = _TRACKED.search(entry)
        if not m:
            out.append(f"{head} line is malformed — write it as ``- **Tracked in.** `<path>` — `<key>` `` "
                       if "**Tracked in" in entry else f"{head} line is missing")
            continue
        path, row_key = m.group(1), m.group(2)
        full = path if os.path.isabs(path) else os.path.join(repo, path)
        norm = comparable(full)
        name = norm.rpartition("/")[2]
        if not os.path.isfile(full):
            out.append(f"{head} path `{path}` does not exist")
        elif norm.startswith(deferred_dir + "/"):
            out.append(f"{head} path `{path}` is a deferred-work log, not a tracking row")
        elif name in own or own_rx.fullmatch(name):
            out.append(f"{head} path `{path}` is this story's own spec, findings file or record, not a tracking row")
        elif norm == wt_path or norm.startswith(wt_path + "/") or norm.startswith(worktrees + "/"):
            out.append(f"{head} path `{path}` is inside a story worktree; a tracking row lives in the main "
                       f"checkout, never on a story branch")
        elif not row_key.startswith(story_keys):
            out.append(f"{head} key `{row_key}` must read `<story_id> <finding_id>`, starting with "
                       f"`{variants(story_id)[0]}`")
        else:
            with open(full, encoding="utf-8", errors="replace") as fh:
                if not re.search(rf"(?<![\w-]){re.escape(row_key)}(?![\w-])", fh.read()):
                    out.append(f"{head} file `{path}` does not contain `{row_key}`")
    return out


def problems(role: str, report: str, prompt: str) -> list[str]:
    if role in ("diagnose", "repro"):
        wt = field(prompt, "worktree_path")
        if not wt or (role == "diagnose" and field(prompt, "mode") == "scoped"):
            return []
        dirty = git(wt, "status", "--porcelain", "--untracked-files=no").strip()
        out = [f"tracked files changed in {wt} (report-only agent): {dirty}"] if dirty else []
        if role == "repro":
            rx = repro_name()
            stray = [p for p in git(wt, "ls-files", "--others", "--exclude-standard").splitlines()
                     if not rx.match(p.rsplit("/", 1)[-1])]
            if stray:
                out.append(f"untracked files that are not repro tests: {', '.join(stray)} — delete them")
        return out

    if not re.search(rf"(?m)^\W*?{re.escape(CLAIMS[role])}", report or ""):
        return []
    if role == "propagate":
        if (field(prompt, "mode") or "").lower() != "apply":
            return []
        paths = re.findall(r"(?m)^\s*-\s*updated:\s*(\S+)", report or "")
        if not paths:
            return ["the report claims applied edits but lists no `- updated: <absolute path>` line"]
        return [f"{p}: no uncommitted change in its checkout" for p in paths
                if not git(os.path.dirname(p), "status", "--porcelain", "--", os.path.basename(p)).strip()]
    story_id, wt = field(report, "story_id"), field(report, "worktree_path")
    if not story_id or not wt:
        return ["the report claims completion but lacks its `story_id:` / `worktree_path:` lines"]
    out = []
    dirty = git(wt, "status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        out.append(f"uncommitted tracked changes in {wt}: {dirty}")
    seed_base = field(prompt, "seed_base") if role == "impl" else None
    seed_problem = seed_base_problem(wt, seed_base) if seed_base else None
    commits = [] if seed_problem else story_commits(wt, story_id, seed_base)
    if role == "impl":
        if seed_problem:
            out.append(seed_problem)
        head = git(wt, "log", "-1", "--format=%s").strip()
        if not seed_problem and (len(commits) != 1 or commits[-1] != head):
            out.append(f"expected exactly one story commit, at HEAD; found {len(commits)}: {commits}")
        problem = tree_problem(wt, story_id, "phase-2")
    else:
        if len(commits) > 2:
            out.append(f"more than two story commits: {commits}")
        elif len(commits) == 2 and "code review close-out" not in commits[1]:
            out.append(f"the second story commit is not a close-out commit: {commits[1]!r}")
        deferred_dir = str(wfconfig.get("paths.deferred_work_dir", f"{specs_rel()}/deferred-work"))
        dpath = os.path.join(wt, deferred_dir, f"story-{variants(story_id)[0]}.md")
        raw = field(report, "deferred_logged")
        logged = re.match(r"\d+", raw or "")
        count = int(logged.group()) if logged else None
        said = "absent" if raw is None else str(count) if logged else f"unreadable ({raw!r})"
        if os.path.isfile(dpath):
            with open(dpath, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            for entry in re.split(r"(?m)^### ", body)[1:]:
                if not re.search(r"bar impact", entry, re.IGNORECASE):
                    out.append(f"deferred entry '{entry.splitlines()[0]}' has no Bar impact line")
            if count is None or count > 0:
                out += tracked_in_problems(body, story_id, count, wt, said)
        elif count is None or count > 0:
            out.append(f"deferred_logged is {said}, but {deferred_dir}/story-{variants(story_id)[0]}.md does not "
                       f"exist — write the logged defers there, or report `deferred_logged: 0`")
        rx = repro_name()
        left = [p for p in git(wt, "ls-files", "--others", "--exclude-standard").splitlines()
                if rx.match(p.rsplit("/", 1)[-1])]
        if left:
            out.append(f"untracked repro tests left in {wt}: {', '.join(left)} — commit the adopted ones in the "
                       f"close-out and delete the rest")
        problem = tree_problem(wt, story_id, "phase-3")
    if problem:
        out.append(problem)
    return out


def blocks_so_far(agent_id: str) -> int:
    p = os.path.join(tempfile.gettempdir(), "workflow-outcome-gate", f"{agent_id}.count")
    try:
        with open(p, encoding="utf-8") as fh:
            n = int(fh.read().strip())
    except Exception:  # noqa: BLE001
        n = 0
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(str(n + 1))
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--role", required=True, choices=("impl", "finalize", "diagnose", "repro", "propagate"))
    a = ap.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.buffer.read().lstrip(b"\xef\xbb\xbf").decode("utf-8"))
        if a.role == "repro" and payload.get("agent_type") not in ("repro-writer", "pbt-writer"):
            return 0
        prompt, last = transcript_texts(payload.get("agent_transcript_path"))
        report = payload.get("last_assistant_message") or last
        found = problems(a.role, report, prompt)
    except Exception:  # noqa: BLE001 -- the hook never bricks an agent
        return 0
    if not found:
        return 0
    agent = payload.get("agent_type") or a.role
    if blocks_so_far(str(payload.get("agent_id") or agent)) >= CAP:
        story_id = field(report, "story_id") or field(prompt, "story_id")
        if story_id:
            subprocess.run([sys.executable, str(SCRIPTS / "story_record.py"), "append", story_id,
                            f"{agent} outcome gate unmet after {CAP} blocks; its completion claim is unverified "
                            "— check the agent report before the next phase"], capture_output=True, timeout=60)
        return 0
    print(json.dumps({"decision": "block", "reason": (
        f"Your report does not hold yet ({agent} outcome gate, .workflow/hooks/session/agent_outcome.py):\n- "
        + "\n- ".join(found)
        + "\nFix each one, or, if you cannot, replace the completion line with the failure and the question it "
          "raises.")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
