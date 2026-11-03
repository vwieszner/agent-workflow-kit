"""Shared helpers for the autonomous loop's halt / resume / audit scripts.

* The failure log (`config: paths.auto_dev_failure_log`): `failure_blocks` parses its entries
  (`## <story_id> — <timestamp> — <step>` headings and their leading `- key: value` field group).
* Own-change commits on the base branch: the main checkout's tracked bookkeeping files (sprint
  status, story history, failure log, slot registry) are shared by every session. A halt or a
  resume commits only ITS change to them: the index copy of each file is rebuilt from the
  index's own text plus exactly that change (`stage_text`), so another session's uncommitted
  edit in the same file never enters the commit. `commit_own` wraps it: base-branch check,
  empty-index wait, staged-paths check, unstage on failure.

Importable only. Stdlib only.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

FAILURE_HEAD_RE = re.compile(r"^## (\S+) — (\S+) — (.*)$")
FIELD_RE = re.compile(r"^- (\w+): ?(.*)$")
INDEX_WAIT_SEC = 60


class StepError(RuntimeError):
    pass


def run(cmd: list[str], cwd: Path | None = None, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    if input_bytes is not None:
        return subprocess.run(cmd, cwd=cwd, input=input_bytes, capture_output=True)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def dashed(story_id: str) -> str:
    return story_id.replace(".", "-")


def same_story(a: str, b: str) -> bool:
    """Story ids match in dotted and dashed form alike (7.5.8 == 7-5-8)."""
    return dashed(a) == dashed(b)


def rel(key: str, default: str) -> Path:
    """A `config: paths.*` value as a repo-relative path."""
    return Path(str(wfconfig.get(key, default)))


def sprint_status_rel() -> Path:
    return rel("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml")


def registry_rel() -> Path:
    return rel("paths.slot_registry", "docs/implementation-artifacts/slot-registry.md")


def failure_log_rel() -> Path:
    return rel("paths.auto_dev_failure_log", "docs/implementation-artifacts/auto-dev-failures.md")


def history_rel(key: str) -> Path:
    return rel("paths.story_history_dir", "docs/implementation-artifacts/story-history") / f"{key}.md"


# ------------------------------------------------------------------ failure log

def failure_blocks(lines: list[str]) -> list[dict]:
    """Every entry: story_id, stamp, step, start (heading line index), end (exclusive), fields."""
    heads = [i for i, line in enumerate(lines) if FAILURE_HEAD_RE.match(line.rstrip("\r\n"))]
    out = []
    for n, start in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        m = FAILURE_HEAD_RE.match(lines[start].rstrip("\r\n"))
        fields: dict[str, str] = {}
        i = start + 1
        while i < end and not lines[i].strip():
            i += 1
        last = None
        while i < end:
            text = lines[i].rstrip("\r\n")
            f = FIELD_RE.match(text)
            if f:
                last = f.group(1)
                fields[last] = f.group(2)
            elif last and text.startswith("  ") and text.strip():
                fields[last] += "\n" + text[2:]
            elif last and not text.strip() and i + 1 < end and lines[i + 1].startswith("  "):
                fields[last] += "\n"
            else:
                break
            i += 1
        out.append({"story_id": m.group(1), "stamp": m.group(2), "step": m.group(3).strip(),
                    "start": start, "end": end, "fields": fields})
    return out


def block_span(lines: list[str], block: dict) -> range:
    """The block's lines plus its own leading blank line, minus the next entry's leading blank line."""
    start, end = block["start"], block["end"]
    if start > 0 and not lines[start - 1].strip():
        start -= 1
    if end < len(lines) and end > start and not lines[end - 1].strip():
        end -= 1
    return range(start, end)


def story_entries(text: str, story_id: str) -> list[dict]:
    lines = text.splitlines(keepends=True)
    return [b for b in failure_blocks(lines) if same_story(b["story_id"], story_id)]


def without_story(text: str, story_id: str) -> str:
    """`text` with every entry of `story_id` (and its leading blank line) removed."""
    lines = text.splitlines(keepends=True)
    drop = {i for b in failure_blocks(lines) if same_story(b["story_id"], story_id) for i in block_span(lines, b)}
    return "".join(line for i, line in enumerate(lines) if i not in drop)


def entry_texts(text: str, story_id: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    return ["".join(lines[i] for i in block_span(lines, b)) for b in failure_blocks(lines)
            if same_story(b["story_id"], story_id)]


# ------------------------------------------------------------------ index staging

def index_text(repo: Path, path: Path) -> str | None:
    """The index copy of `path` (None when the index does not hold it)."""
    r = run(["git", "-C", str(repo), "show", f":{path.as_posix()}"], input_bytes=b"")
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "surrogateescape")


def head_text(repo: Path, path: Path) -> str | None:
    r = run(["git", "-C", str(repo), "show", f"HEAD:{path.as_posix()}"], input_bytes=b"")
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "surrogateescape")


def work_text(repo: Path, path: Path) -> str | None:
    p = repo / path
    return p.read_bytes().decode("utf-8", "surrogateescape") if p.is_file() else None


def stage_text(repo: Path, path: Path, text: str) -> None:
    """Make `text` the index copy of `path` (keeps the file mode; 100644 for a new path)."""
    blob = run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
               input_bytes=text.encode("utf-8", "surrogateescape"))
    if blob.returncode != 0:
        raise StepError(f"git hash-object {path.as_posix()}: {blob.stderr.decode('utf-8', 'replace').strip()}")
    sha = blob.stdout.decode().strip()
    listed = run(["git", "-C", str(repo), "ls-files", "-s", "--", path.as_posix()]).stdout.split()
    mode = listed[0] if listed else "100644"
    r = run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo", f"{mode},{sha},{path.as_posix()}"])
    if r.returncode != 0:
        raise StepError(f"git update-index {path.as_posix()}: {(r.stderr or r.stdout).strip()}")


def replace_line(base: str, work: str, match: Callable[[str], bool], what: str) -> str | None:
    """`base` with its one line matching `match` replaced by `work`'s one matching line.

    None when the two lines are equal (nothing to stage). Raises when either text holds no
    matching line or several."""
    b = [i for i, line in enumerate(base.splitlines(keepends=True)) if match(line.rstrip("\r\n"))]
    w = [line for line in work.splitlines(keepends=True) if match(line.rstrip("\r\n"))]
    if len(b) != 1 or len(w) != 1:
        raise StepError(f"{what}: expected one line in the index and one in the working tree, "
                        f"found {len(b)} and {len(w)}")
    lines = base.splitlines(keepends=True)
    old = lines[b[0]]
    new = w[0]
    if old.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    if old == new:
        return None
    lines[b[0]] = new
    return "".join(lines)


def check_branch(repo: Path) -> None:
    base = str(wfconfig.get("git.base_branch", "development"))
    branch = run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"]).stdout.strip()
    if branch != base:
        raise StepError(f"main checkout is on {branch!r}, not {base}")


def commit_own(repo: Path, paths: list[Path], message: str, stage_own: Callable[[], None],
               wait_s: float | None = None) -> str:
    """Commit on the base branch what `stage_own` stages in `paths`; the new short HEAD, or ''
    when nothing was staged. The index must hold nothing else (waits up to INDEX_WAIT_SEC) and
    the staged paths must be within `paths`. A failure unstages `paths` and re-raises."""
    check_branch(repo)
    deadline = time.monotonic() + (INDEX_WAIT_SEC if wait_s is None else wait_s)
    while run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode != 0:
        if time.monotonic() > deadline:
            raise StepError(f"another session's changes stayed staged for {INDEX_WAIT_SEC}s")
        time.sleep(2)
    try:
        stage_own()
        staged = set(run(["git", "-C", str(repo), "diff", "--cached", "--name-only"]).stdout.split())
        allowed = {p.as_posix() for p in paths}
        if not staged <= allowed:
            raise StepError(f"unexpected staged paths: {sorted(staged - allowed)}")
        if not staged:
            return ""
        done = run(["git", "-C", str(repo), "commit", "-q", "-m", message])
        if done.returncode != 0:
            raise StepError(f"git commit: {(done.stderr or done.stdout).strip()[-300:]}")
        return run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"]).stdout.strip()
    except Exception:
        run(["git", "-C", str(repo), "reset", "-q", "--", *[p.as_posix() for p in paths]])
        raise


def row_matcher(key: str) -> Callable[[str], bool]:
    """Matches the sprint-status row of `key` (`  <key>: <status>`)."""
    rx = re.compile(rf"^\s+{re.escape(key)}:\s")
    return lambda line: bool(rx.match(line))


def registry_slot(repo: Path, story_id: str) -> int | None:
    """The slot whose registry row is `in_use` by the story (dotted or dashed), else None."""
    path = repo / registry_rel()
    if not path.is_file():
        return None
    want = dashed(story_id)
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].isdigit() and cells[1] == "in_use" and dashed(cells[2]) == want:
            return int(cells[0])
    return None


def slot_matcher(slot: int) -> Callable[[str], bool]:
    rx = re.compile(rf"^\|\s*{slot}\s*\|")
    return lambda line: bool(rx.match(line))
