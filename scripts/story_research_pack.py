#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Collect the planning-doc lines that name one story into a capped research pack for story-spec.

Reads the story's checkout (its worktree) and writes one Markdown file of `path:line: text` lines, section by
section:
  * the story's sprint-status row and the tail of its story-history file;
  * every other spec, record or audit in `config: paths.specs_dir` naming the story (dependency hand-over and
    split notes, consumer ACs);
  * epic lines naming the story outside its own block (the orchestrator passes that block);
  * design-question tracker lines (`config: paths.design_questions`, optional) naming the story or a ticket
    (`config: paths.design_ticket_regex`) its epic block or tracker rows name;
  * deferred-work entries naming the story.
A story is named by its dashed id (`16-11`, also as the prefix of its key) or, for a numbered story, its dotted
id (`16.11`). Each section is capped; a capped section ends with the count of lines left out and the Grep that
lists them.

Usage
-----
    uv run --no-project .workflow/scripts/story_research_pack.py <story_id> --worktree <worktree_path> [--out <file>]

`--out` defaults to `.workflow/state/story-setup/research-<story_id>.md` (git-ignored). Prints the written path
and its size.

When to use
-----------
    The orchestrator runs it before dispatching story-spec (auto-dev-loop, parallel-dev-wave Checkpoint 1) and
    passes the path as `research_pack`.

Exit codes: 0 = pack written; 2 = bad arguments or no specs dir under the worktree.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402


def _rel(key: str, default: str) -> Path:
    return Path(str(wfconfig.get(key, default)).replace("\\", "/").strip("/"))


IMPL = _rel("paths.specs_dir", "docs/implementation-artifacts")
EPICS = _rel("paths.epics_dir", "docs/planning-artifacts/epics")
SPRINT = _rel("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml")
HISTORY = _rel("paths.story_history_dir", "docs/implementation-artifacts/story-history")
DEFERRED = _rel("paths.deferred_work_dir", "docs/implementation-artifacts/deferred-work")
_TRACKER_RAW = str(wfconfig.get("paths.design_questions", "") or "")
TRACKER = Path(_TRACKER_RAW.strip("/")) if _TRACKER_RAW else None
TICKET = re.compile(str(wfconfig.get("paths.design_ticket_regex", r"\b[A-Z][A-Z0-9]+-\d+\b")))
LINE_CAP = 500
PER_FILE = 6
CAPS = {"sprint": 1500, "history": 3000, "specs": 9000, "epics": 4000, "tracker": 5000, "deferred": 2500}


def id_pattern(story_id: str) -> re.Pattern:
    forms = [re.escape(story_id)]
    numbered = re.fullmatch(r"(\d+)-(\d+[a-z]?)", story_id)
    if numbered:
        forms.append(re.escape(f"{numbered[1]}.{numbered[2]}"))
    return re.compile(rf"(?<![\w.-])(?:{'|'.join(forms)})(?![0-9A-Za-z])")


def clip(s: str) -> str:
    s = s.strip()
    return s if len(s) <= LINE_CAP else s[:LINE_CAP] + " …"


def read_lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []


def capped(lines: list[str], cap: int, grep: str) -> list[str]:
    out, size = [], 0
    for i, line in enumerate(lines):
        if size + len(line) > cap:
            out.append(f"… {len(lines) - i} more line(s): {grep}")
            break
        out.append(line)
        size += len(line) + 1
    return out


def own_block(lines: list[str], dotted: str) -> range:
    """0-based line range of the epic's `Story <dotted>` heading and its body."""
    head = re.compile(rf"^(#{{2,4}}) Story {re.escape(dotted)}[:\s]")
    for i, line in enumerate(lines):
        m = head.match(line)
        if m:
            level = len(m[1])
            for j in range(i + 1, len(lines)):
                h = re.match(r"^(#+) ", lines[j])
                if h and len(h[1]) <= level:
                    return range(i, j)
            return range(i, len(lines))
    return range(0)


def grep_file(p: Path, rel: str, pat: re.Pattern, skip: range = range(0)) -> list[str]:
    hits = [f"{rel}:{n}: {clip(t)}" for n, t in enumerate(read_lines(p), 1) if n - 1 not in skip and pat.search(t)]
    if len(hits) > PER_FILE:
        hits = hits[:PER_FILE] + [f"{rel}: … {len(hits) - PER_FILE} more line(s) in this file"]
    return hits


def build(story_id: str, wt: Path) -> str:
    pat = id_pattern(story_id)
    dotted = story_id.replace("-", ".", 1) if re.fullmatch(r"\d+-\d+[a-z]?", story_id) else None
    grep = f"Grep -n '{pat.pattern}'"
    out = [f"# Research pack — {story_id}", "",
           f"Built by `.workflow/scripts/story_research_pack.py` from `{wt}`. Each line is `path:line: text` "
           "(paths relative to the worktree). A capped section ends with the Grep that lists the rest.", ""]

    sprint = wt / SPRINT
    key_line = re.compile(rf"^\s+{re.escape(story_id)}(?:-[a-z0-9-]+)?:")
    rows = [f"{SPRINT.as_posix()}:{n}: {clip(t)}" for n, t in enumerate(read_lines(sprint), 1) if key_line.match(t)]
    out += ["## Sprint-status row", *(capped(rows, CAPS["sprint"], grep) or ["none"]), ""]

    history = sorted((wt / HISTORY).glob(f"{story_id}-*.md")) + sorted((wt / HISTORY).glob(f"{story_id}.md"))
    out.append("## Story history (tail)")
    if history:
        text = history[0].read_text(encoding="utf-8", errors="replace")
        out += [f"`{HISTORY.as_posix()}/{history[0].name}`" + (" (last part)" if len(text) > CAPS["history"] else ""),
                text[-CAPS["history"]:].strip()]
    else:
        out.append("none")
    out.append("")

    own = re.compile(rf"^{re.escape(story_id)}(?:-|\.md$)")
    spec_hits = []
    for p in sorted((wt / IMPL).glob("*.md")):
        if not own.match(p.name):
            spec_hits += grep_file(p, f"{IMPL.as_posix()}/{p.name}", pat)
    out += [f"## Other specs, records and audits naming {story_id}", *(capped(spec_hits, CAPS["specs"], f"{grep} {IMPL.as_posix()}/*.md") or ["none"]), ""]

    epic_hits, tickets = [], set()
    for p in sorted((wt / EPICS).glob("*.md")):
        lines = read_lines(p)
        block = own_block(lines, dotted) if dotted else range(0)
        tickets.update(TICKET.findall("\n".join(lines[i] for i in block)))
        epic_hits += grep_file(p, f"{EPICS.as_posix()}/{p.name}", pat, block)
    out += [f"## Epic lines naming {story_id} outside its own block", *(capped(epic_hits, CAPS["epics"], f"{grep} {EPICS.as_posix()}/") or ["none"]), ""]

    if TRACKER is not None:
        tracker = read_lines(wt / TRACKER)
        tickets.update(t for line in tracker if pat.search(line) for t in TICKET.findall(line))
        named = re.compile("|".join([pat.pattern] + [rf"\b{re.escape(t)}\b" for t in sorted(tickets)]))
        tracker_hits = [f"{TRACKER.as_posix()}:{n}: {clip(t)}" for n, t in enumerate(tracker, 1) if named.search(t)]
        label = f"{story_id}{', ' + ', '.join(sorted(tickets)) if tickets else ''}"
        out += [f"## Design-question tracker lines naming {label}",
                *(capped(tracker_hits, CAPS["tracker"], f"Grep -n '{named.pattern}' {TRACKER.as_posix()}") or ["none"]),
                ""]
    else:
        out += ["## Design-question tracker", "SKIPPED — config: paths.design_questions is empty", ""]

    deferred = []
    for p in sorted((wt / DEFERRED).glob("*.md")):
        deferred += grep_file(p, f"{DEFERRED.as_posix()}/{p.name}", pat)
    out += [f"## Deferred work naming {story_id}",
            *(capped(deferred, CAPS["deferred"], f"{grep} {DEFERRED.as_posix()}/") or ["none"])]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("story_id", help="dashed story id, e.g. 1-2 or ab-40")
    ap.add_argument("--worktree", type=Path, required=True, help="the story's worktree (or checkout) root")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: .workflow/state/story-setup/research-<story_id>.md")
    args = ap.parse_args(argv)
    if not (args.worktree / IMPL).is_dir():
        sys.stderr.write(f"no {IMPL.as_posix()}/ under {args.worktree}\n")
        return 2
    out = args.out or wfconfig.state_dir("story-setup") / f"research-{args.story_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = build(args.story_id, args.worktree)
    out.write_text(text, encoding="utf-8")
    print(f"research_pack: {out.as_posix()} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
