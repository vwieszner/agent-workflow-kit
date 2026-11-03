#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic parts of filing a story's questions for the external design owner on its one ticket.

The `design-ticket` skill runs this. One ticket per story in the issue tracker (`config: tracker.*`);
the tracker calls themselves go through `.workflow/scripts/tracker.py` (REST backend) or the
tracker's MCP tools (`config: tracker.backend = "mcp"`).

Subcommands:
  label <story_id>
      JSON {"label", "query"}: the story's ticket label (`config: tracker.story_label_prefix` + id,
      `.` -> `-`) and the tracker search query (JQL for the jira backend).
  render <questions.json> [--start N] [--comment]
      JSON {"summary", "labels", "description"} for a new ticket, or {"comment"} with --comment (the
      questions numbered from --start, default 1). Input shape: see QUESTIONS FILE below.
  tracker-row --ticket KEY --item TEXT --blocks TEXT --origin TEXT [--today YYYY-MM-DD]
      Appends the ticket's row to the ticket table of `config: paths.design_questions`.
      JSON {"added": true|false}; false when the key already has a row.
  commit-tracker --ticket KEY --message TEXT
      On `config: git.base_branch` in the main checkout, with an empty index: stages only the
      tracker changes whose lines carry `[KEY]` and commits them. JSON {"commit": "<sha>"}.
  mark-spec --spec PATH --ticket KEY (--q 1,3 | --all)
      Rewrites `[OPEN-Q <n>] (<tag>)` as `OPEN-Q <n> (<tag>, interim ⚠️, KEY)` for the listed n (or
      every bracketed marker of the tag with --all); tag = `config: design.question_tag`.
      JSON {"rewritten": [n, ...]}; exit 1 when a listed n has no bracketed marker.

QUESTIONS FILE (UTF-8 JSON):
  {"story_id": "3-4", "topic": "Upload progress overlay", "builds": "the upload progress overlay",
   "phase": null, "what_this_is": "<markdown: what the story builds, from which design nodes>",
   "questions": [{"title": "Stalled upload", "body": "<markdown>", "nodes": ["12:34"],
                  "sources": [{"source": "Spec doc §X", "value": "..."}],
                  "options": ["(a) ...", "(b) ..."], "ask": "Which, and after how long?",
                  "interim": "(a), it stays until a page reload"}],
   "notes": ["<a question the owner's published rules already answer: the rule and the value>"]}
  `nodes`, `sources`, `options`, `ask` and `notes` are optional.

With no `config: paths.design_questions`, `tracker-row` and `commit-tracker` print
`{"skipped": "..."}` and exit 0; with no `config: design.question_tag`, `mark-spec` exits 1.
Exit 0 ok, 1 refusal (`{"error": ...}`), 2 usage. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bookkeeping as bk  # noqa: E402
import _design as d  # noqa: E402
import wfconfig  # noqa: E402


class Refusal(RuntimeError):
    pass


def story_label(story_id: str) -> str:
    return str(d.cfg("tracker.story_label_prefix", "story-")) + story_id.replace(".", "-")


def query(story_id: str) -> str:
    template = str(d.cfg("tracker.story_query", 'project = {project_key} AND labels = "{label}"'))
    return wfconfig.render(template, project_key=str(d.cfg("tracker.project_key", "")),
                           label=story_label(story_id))


def _question_block(n: int, q: dict) -> str:
    lines = [f"{n}. **{q['title']}.** {q['body']}".rstrip()]
    if q.get("nodes"):
        lines.append("   - Nodes: " + ", ".join(f"`{node}`" for node in q["nodes"]))
    if q.get("sources"):
        lines.append("   - Sources: " + "; ".join(f"{s['source']}: {s['value']}" for s in q["sources"]))
    if q.get("options"):
        lines.append("   - Options: " + "; ".join(q["options"]))
    if q.get("ask"):
        lines.append(f"   - {q['ask']}")
    lines.append(f"   - Interim ⚠️: {q['interim']}")
    return "\n".join(lines)


def questions_section(spec: dict, start: int) -> str:
    return "\n\n".join(_question_block(start + i, q) for i, q in enumerate(spec["questions"]))


def render_ticket(spec: dict) -> dict:
    n = len(spec["questions"])
    labels = [str(x) for x in (d.cfg("tracker.labels", []) or [])] + [story_label(spec["story_id"])]
    if spec.get("phase"):
        labels.append(f"phase-{spec['phase']}")
    parts = [f"Story: {spec['story_id']}", "## What this is", spec["what_this_is"].strip(),
             "## Questions", questions_section(spec, 1)]
    if spec.get("notes"):
        parts += ["## Notes", "\n".join(f"- {note}" for note in spec["notes"])]
    done_when = str(d.cfg("tracker.done_when", "") or "Each question is answered on this ticket.")
    parts += ["## Done when", done_when]
    plural = "question" if n == 1 else "questions"
    prefix = str(d.cfg("tracker.summary_prefix", "[UX]") or "").strip()
    summary = f"{spec['topic']} — {n} {plural} from {spec['builds']} (Story {spec['story_id']})"
    return {"summary": f"{prefix} {summary}".strip(), "labels": labels, "description": "\n\n".join(parts)}


def render_comment(spec: dict, start: int) -> dict:
    parts = [f"Story: {spec['story_id']}", "## Questions", questions_section(spec, start)]
    if spec.get("notes"):
        parts += ["## Notes", "\n".join(f"- {note}" for note in spec["notes"])]
    return {"comment": "\n\n".join(parts)}


def insert_tracker_row(repo: Path, ticket: str, item: str, blocks: str, origin: str, today: str) -> bool:
    rel = d.tracker_rel()
    if rel is None:
        raise Refusal("config: paths.design_questions is empty")
    path = repo / rel
    if not path.is_file():
        raise Refusal(f"{rel.as_posix()} does not exist")
    raw = path.read_bytes().decode("utf-8")
    try:
        new, added = d.insert_row(raw, ticket, item, blocks, origin, today)
    except ValueError as exc:
        raise Refusal(f"{rel.as_posix()}: {exc}") from None
    if added:
        path.write_bytes(new.encode("utf-8"))
    return added


def commit_tracker(repo: Path, ticket: str, message: str) -> str:
    rel = d.tracker_rel()
    if rel is None:
        raise Refusal("config: paths.design_questions is empty")
    marker = f"[{ticket}]"

    def stage_own() -> None:
        base = bk.index_text(repo, rel)
        work = bk.work_text(repo, rel)
        if work is None:
            raise bk.StepError(f"{rel.as_posix()} does not exist")
        new = d.own_lines(base or "", work, lambda ln: marker in ln)
        if new is None:
            raise bk.StepError(f"no uncommitted tracker line carries {marker}")
        bk.stage_text(repo, rel, new)

    try:
        sha = bk.commit_own(repo, [rel], message, stage_own, wait_s=0)
    except bk.StepError as exc:
        raise Refusal(str(exc)) from None
    if not sha:
        raise Refusal(f"no uncommitted tracker line carries {marker}")
    return sha


def mark_spec(spec_path: Path, ticket: str, wanted: set[int] | None) -> list[int]:
    tag = d.question_tag()
    if not tag:
        raise Refusal("no design question tag: set config: design.question_tag (or spec.external_question_tags)")
    bracketed = re.compile(rf"\[OPEN-Q (\d+)\] \({re.escape(tag)}\)")
    raw = spec_path.read_bytes().decode("utf-8")
    found = {int(m.group(1)) for m in bracketed.finditer(raw)}
    targets = found if wanted is None else wanted
    missing = sorted(targets - found)
    if missing:
        raise Refusal(f"{spec_path.name}: no bracketed `[OPEN-Q <n>] ({tag})` marker for {missing}")
    out = bracketed.sub(lambda m: (f"OPEN-Q {m.group(1)} ({tag}, interim ⚠️, {ticket})"
                                   if int(m.group(1)) in targets else m.group(0)), raw)
    spec_path.write_bytes(out.encode("utf-8"))
    return sorted(targets)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="design-owner ticket filing helpers")
    ap.add_argument("--repo-root", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("label")
    p.add_argument("story_id")
    p = sub.add_parser("render")
    p.add_argument("questions")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--comment", action="store_true")
    p = sub.add_parser("tracker-row")
    for flag in ("--ticket", "--item", "--blocks", "--origin"):
        p.add_argument(flag, required=True)
    p.add_argument("--today", default=date.today().isoformat())
    p = sub.add_parser("commit-tracker")
    p.add_argument("--ticket", required=True)
    p.add_argument("--message", required=True)
    p = sub.add_parser("mark-spec")
    p.add_argument("--spec", required=True)
    p.add_argument("--ticket", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--q")
    group.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    repo = (args.repo_root or wfconfig.repo_root()).resolve()
    try:
        if args.cmd in ("tracker-row", "commit-tracker") and not d.configured():
            out = {"skipped": "config: paths.design_questions is empty — no question tracker file"}
        elif args.cmd in ("tracker-row", "commit-tracker", "mark-spec") and not d.is_ticket_key(args.ticket):
            raise Refusal(f"--ticket {args.ticket!r} is not a ticket key (config: paths.design_ticket_regex)")
        elif args.cmd == "label":
            out = {"label": story_label(args.story_id), "query": query(args.story_id)}
        elif args.cmd == "render":
            spec = json.loads(Path(args.questions).read_text(encoding="utf-8"))
            out = render_comment(spec, args.start) if args.comment else render_ticket(spec)
        elif args.cmd == "tracker-row":
            out = {"added": insert_tracker_row(repo, args.ticket, args.item, args.blocks, args.origin, args.today)}
        elif args.cmd == "commit-tracker":
            out = {"commit": commit_tracker(repo, args.ticket, args.message)}
        else:
            wanted = None if args.all else {int(x) for x in args.q.split(",")}
            out = {"rewritten": mark_spec(Path(args.spec), args.ticket, wanted)}
    except Refusal as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
