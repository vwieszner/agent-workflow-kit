#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Render one review-workflow round into the story's findings file and record it.

Input: the workflow run, as one of
  * `--transcript-dir <dir>` — Claude Code: the `Transcript dir` the Workflow tool prints at launch
    (`<session>/subagents/workflows/<runId>`); the run file is `<session>/workflows/<runId>.json`;
  * `--run-dir <dir>` — the replay runner (`workflow_replay.mjs`, the no-Workflow fallback): `<dir>/run.json`
    and `<dir>/journal.jsonl`.
Two runner-written files are read; no agent copies any review text:
  * the run file — its `result` is the workflow's return value, whose `record` map holds structure only
    (raw ids per finding, the journal label of each accepted layer, of each verifier vote and of the repro call);
  * `journal.jsonl` — each agent's full result, joined to its label through the `started` line's agentId.
The findings document is rebuilt from the two. A finding's verdict is the majority of
its votes (`decide`, the mirror of the workflow's); a REPRODUCED repro result counts only
with an `assertion` failure, else it reads BLOCKED. Output:
  * appends a `## Review round <n> — <date>` section to the story's findings file
    (`<worktree>/<config: paths.specs_dir>/<story>-code-review-findings-*.md`),
    creating the file with its heading when absent;
  * appends a `review round <n> recorded …` line to the story's record file via
    `story_record.cmd_append`.

Prints `review-render: OK <findings file>` on success. Exits 1 without writing
anything when the run is not a completed `review` run, its story id or
round differs from `--story-id` / `--round`, a record label has no journal result or
more than one, a raw id, verdict or repro result cannot be resolved, a rebuilt
finding's title, location, recommendation, verdict, votes, bar impact or repro cell
differs from the run's triage rows, a ui-verifier `states`, `checked_ids`, `tests`, `viewports`, `acs` or entry
(`state_id`) is malformed, an `unreached` entry has no valid `methods_tried`, the run's `ui_test_titles`,
`ui_test_viewports` or `ui_spec_acs` is not a list of non-empty strings, the run's `ui_unaccounted` / `ui_double_placed` / `ui_unknown_ids` / `ui_untested_titles` /
`ui_unplaced_viewports` / `ui_unmapped_acs` differs from the one rebuilt from the ui-verifier's journal result, or the
findings file already holds a section for that round.

The ui-verifier's report names its own state list (`states`) and places each id once: in `checked_ids`, or as
the `state_id` of an `unreached` or `out_of_scope` entry. The round section and the caller's report list the ids
placed nowhere (`ui_unaccounted`), placed more than once, in one list or in several (`ui_double_placed`), and
placed without being in `states` (`ui_unknown_ids`); each reads `not reported by this run` when the report lacks
one of the four lists (`state_accounting`, the mirror of the workflow's `uiAccounting`).

The report also maps each test title the dispatch listed (the run's `ui_test_titles`) to the state that test
drives, as `tests` entries `{title, state_id}`. A `tests` entry is a mapping, not a placement: one whose `state_id`
is absent from `states` joins `ui_unknown_ids` (`<id> — <title> — placed in tests`), and the titles no entry names
are `ui_untested_titles` (`untested_titles`, the mirror of the workflow's), listed beside the other lists. That list
reads `not reported by this run` when the report has no `tests` or the run no `ui_test_titles`.

It maps each viewport the dispatch listed (the run's `ui_test_viewports`) to the layout states checked or placed at
it, as `viewports` entries `{viewport, state_ids}` naming at least one state, and each spec AC id the dispatch listed
(the run's `ui_spec_acs`) to the states that AC describes, as `acs` entries `{ac, state_ids[, reason]}`; an AC that
describes no state names none and gives a reason. Both are mappings, not placements: a state id absent from `states`
joins `ui_unknown_ids` (`<id> — <viewport> — placed in viewports`, `<id> — <ac> — placed in acs`), the viewports no
entry names are `ui_unplaced_viewports` (`unplaced_viewports`) and the AC ids no entry names are `ui_unmapped_acs`
(`unmapped_acs`). Each reads `not reported by this run` when the report lacks `viewports` / `acs` or the run the
dispatched list.

Each `unreached` entry names the methods the verifier tried to reach its state, as `methods_tried`: a non-empty list of
`{method, stopped_by}` whose `method` is one of `UNREACHED_METHODS` (the workflow's list) and whose `stopped_by` is text
with a non-blank character. An entry without a valid list is refused (`_unreached_list`, the mirror of the workflow's
`uiAccounting`), also in a report that lacks `states`; each unreached line, in the round section and the caller's report,
reads `<state> — <reason> — tried: <method> (<stopped_by>); …`, the methods in the order given.

A round that carries findings from halted rounds (`record.carried_input`, see `review_carry.py`)
lists each as a `carried:<id>` source. The carried set is re-derived from the story's findings files (the
run's `findings_file` and every `<id>-code-review-findings*.md` beside it, in `review_ledger.findings_paths` order)
before the round is appended; the renderer refuses, writing nothing, when `record.carried_input` differs.
A carried finding's text (and, for one heading its own group, its layer evidence, relabelled
`[R<n>:<raw id>]`; a label already prefixed `R<n>:` or `carried:` keeps its attribution) comes from
those files, never from the run. A halted `BLOCKING` impact survives the
round's verdict as `BLOCKING (minority, halted round <n>) …`; a REFUTED round verdict then reads
UNVERIFIABLE (`apply_halted_blocking`, the mirror of the workflow's rule).

The run's `delta` (scripts/review_stage.py, returned unchanged by the workflow) is required and validated; a run
without a valid one is refused, writing nothing. The record line ends with `; reviewed <kind> <base40>..<end40>`.
The round's Summary gains `- Reviewed:`, for a close-out or new-code round `- Delta:`, and `- Loop exit:` (`candidate`
when every triage finding lies outside the delta and none is bar-blocking; `n/a` for a branch or a new-code round,
which has no further round); each triage finding gets a `- **Delta:** inside|outside|n/a` line after its Location line
(`delta_side`, `n/a` on a branch or a new-code round; the caller's table has a `delta` column). A new-code round
(`delta.kind` `new-code`, `how` `since`) reviews the change an amend made to the close-out, from the close-out before it.

Finding text comes from agents reading untrusted code, so every value written outside
the raw-JSON block is put on one line (` ⏎ ` marks a line break): a quoted markdown line
can never start a heading, table row or round section. The raw JSON keeps the original.

Usage
-----
    uv run --no-project .workflow/scripts/review_findings_render.py --transcript-dir <dir> --story-id <id> --round <n>
    uv run --no-project .workflow/scripts/review_findings_render.py --run-dir <run_dir>/replay --story-id <id> --round <n>

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review_carry  # noqa: E402
import review_ledger  # noqa: E402
import wfconfig  # noqa: E402
from story_record import cmd_append  # noqa: E402

WORKFLOW_NAME = "review"
TRIAGE_VERDICTS = ("CONFIRMED", "UNVERIFIABLE", "UNVERIFIED")
DOC_KEYS = ("story_id", "round", "mode", "passes", "date", "diff_range", "delta", "findings_file")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
DELTA_HOWS = {"branch": ("branch",), "close-out": ("same-impl", "replayed", "whole"), "new-code": ("since",)}
LOCATION = re.compile(r"^(.+?):(\d+)(?:-(\d+))?")
BARE_RANGE = re.compile(r"^\d+(?:-\d+)?$")
DELTA_TOLERANCE = 3  # the workflow's dedupe tolerance for neighbouring locations


class RenderError(Exception):
    """The run cannot be rendered; nothing is written."""


def validate_delta(delta) -> None:
    """Refuse a run whose `delta` is absent or malformed — the shapes `review_stage.py` writes and the workflow checks."""
    def bad(why: str):
        raise RenderError(f"delta {why}")
    if not isinstance(delta, dict):
        bad("is missing or not an object")
    kind, how = delta.get("kind"), delta.get("how")
    if kind not in DELTA_HOWS:
        bad(f"kind {kind!r} is not branch, close-out or new-code")
    if how not in DELTA_HOWS[kind]:
        bad(f"how {how!r} does not fit a {kind} round")
    reviewed = delta.get("reviewed")
    if not isinstance(reviewed, dict) or not all(isinstance(reviewed.get(k), str) and SHA40.match(reviewed[k])
                                                 for k in ("base", "end")):
        bad("reviewed needs a 40-hex base and end")
    hunks = delta.get("hunks")
    if not isinstance(hunks, dict) or not all(
            isinstance(rs, list) and all(isinstance(r, list) and len(r) == 2 and all(
                isinstance(x, int) and not isinstance(x, bool) for x in r) for r in rs) for rs in hunks.values()):
        bad("hunks must map paths to [int, int] ranges")
    deleted = delta.get("deleted")
    if not isinstance(deleted, list) or not all(isinstance(p, str) for p in deleted):
        bad("deleted must be a list of paths")
    lines = delta.get("lines")
    if not isinstance(lines, int) or isinstance(lines, bool):
        bad("lines must be an integer")
    if how in ("same-impl", "replayed"):
        against = delta.get("against")
        if not (isinstance(against, dict) and isinstance(against.get("round"), int)
                and isinstance(against.get("end"), str) and SHA40.match(against["end"])):
            bad(f"{how} needs against {{round, end}}")
    if how == "replayed" and not (isinstance(delta.get("replay"), str) and SHA40.match(delta["replay"])):
        bad("replayed needs a 40-hex replay commit")
    if how == "whole" and not (isinstance(delta.get("reason"), str) and delta["reason"].strip()):
        bad("whole needs a reason")


def path_rewrites() -> list[tuple[str, str]]:
    """`config: review.path_rewrites` — [regex, replacement] pairs mapping a cited path (a container path) onto
    its repo-relative path; the workflow receives the same list."""
    out = []
    for pair in wfconfig.get("review.path_rewrites", []) or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            out.append((str(pair[0]), str(pair[1])))
    return out


def worktrees_dir() -> str:
    """The basename of the worktrees root; a cited path through it is cut to its worktree-relative part."""
    try:
        return wfconfig.worktrees_root().name
    except Exception:
        return ""


def _norm_path(path: str, rewrites: list[tuple[str, str]] | None = None, wt_dir: str | None = None) -> str:
    """The workflow's `normPath`."""
    rewrites = path_rewrites() if rewrites is None else rewrites
    wt_dir = worktrees_dir() if wt_dir is None else wt_dir
    p = re.sub(r"[`'\"]", "", str(path).strip()).replace("\\", "/")
    p = re.sub(r"^[ab]/", "", p)
    if wt_dir:
        p = re.sub(rf"^.*?{re.escape(wt_dir)}/[^/]+/", "", p)
    for rx, repl in rewrites:
        p = re.sub(rx, repl, p)
    return re.sub(r"^\.?/", "", p)


def _same_path(a: str, b: str) -> bool:
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def delta_side(location, delta: dict, worktree: str | None = None) -> str:
    """`inside`, `outside` or `n/a` for one finding location against the round's delta.

    A location naming several places (`a.py:10; b.py:20`, `a.py:10, b.py:20`) is inside when any one is. A place
    that is only a range (`a.py:10-12,66-67`) is a range of the file the place before it named; an empty place
    (a trailing or doubled `;` or `,`) is skipped.
    Anything the classifier cannot place (no `file:line`, a deleted file, a file unknown to the delta and the
    worktree, a location with no place at all) is inside, so it keeps the loop going.
    """
    if delta["kind"] in ("branch", "new-code"):
        return "n/a"
    sides: set[str] = set()
    last_file = None
    for place in re.split(r"[;,]", str(location or "")):
        place = re.sub(r"[`'\"]", "", place).strip()
        if not place:
            continue
        if last_file and BARE_RANGE.match(place):
            place = f"{last_file}:{place}"
        if m := LOCATION.match(place):
            last_file = m.group(1)
        sides.add(_place_side(place, delta, worktree))
    return "inside" if not sides or "inside" in sides else "outside"


def _place_side(place: str, delta: dict, worktree: str | None) -> str:
    m = LOCATION.match(re.sub(r"[`'\"]", "", place).strip())
    if not m:
        return "inside"
    path, start = _norm_path(m.group(1)), int(m.group(2))
    end = int(m.group(3) or m.group(2))
    if any(_same_path(path, _norm_path(d)) for d in delta["deleted"]):
        return "inside"
    matched = False
    for hunk_path, ranges in delta["hunks"].items():
        if not _same_path(path, _norm_path(hunk_path)):
            continue
        matched = True
        if any(a <= end + DELTA_TOLERANCE and b >= start - DELTA_TOLERANCE for a, b in ranges):
            return "inside"
    if matched:
        return "outside"
    if worktree and os.path.isfile(os.path.join(worktree, path)):
        return "outside"
    return "inside"


def _worktree_of(findings_file) -> str | None:
    """`<worktree>/<config: paths.specs_dir>/<file>` -> `<worktree>`."""
    if not findings_file:
        return None
    depth = len([p for p in review_ledger.specs_rel().split("/") if p])
    out = os.path.dirname(str(findings_file))
    for _ in range(depth):
        out = os.path.dirname(out)
    return out


def decide(votes: list[dict]) -> dict:
    """Majority of the votes cast — the workflow's `decide`, line for line."""
    if len(votes) == 1:
        v = votes[0]
        return {"verdict": v.get("verdict"), "evidence": v.get("evidence"),
                "bar_impact": v.get("bar_impact"), "votes": [v.get("verdict")]}
    n = lambda x: sum(1 for v in votes if v.get("verdict") == x)  # noqa: E731
    verdict = "CONFIRMED" if n("CONFIRMED") >= 2 else "REFUTED" if n("REFUTED") >= 2 else "UNVERIFIABLE"
    agree = [v for v in votes if v.get("verdict") == verdict]
    evidence = (agree[0].get("evidence") if agree else
                "split vote — " + " | ".join(f"{v.get('verdict')}: {v.get('evidence')}" for v in votes))
    blocking = [v for v in votes if str(v.get("bar_impact") or "").startswith("BLOCKING")]
    if verdict == "REFUTED":
        impact = "n/a"
    elif len(blocking) >= 2:
        impact = blocking[0]["bar_impact"]
    elif len(blocking) == 1:
        impact = (f"BLOCKING (minority, 1 of {len(votes)} votes)"
                  f"{blocking[0]['bar_impact'][len('BLOCKING'):]}")
    else:
        impact = (agree[0] if agree else votes[0]).get("bar_impact")
    return {"verdict": verdict, "evidence": evidence, "bar_impact": impact,
            "votes": [v.get("verdict") for v in votes]}


HALTED_BLOCKING_RX = re.compile(r"^BLOCKING(\s*\([^)]*\))?")


def apply_halted_blocking(decision: dict, members: list[dict], rnd) -> dict:
    """Keep a carried finding's halted BLOCKING visible after the round's verdict — the workflow's rule.

    The first carried member whose halted impact starts with BLOCKING supplies the round and the line.
    A REFUTED verdict reads UNVERIFIABLE with the split evidence; a CONFIRMED or UNVERIFIABLE verdict
    whose impact is not BLOCKING gets the minority line.
    """
    hb = next((c for c in members if str(c.get("bar_impact") or "").startswith("BLOCKING")), None)
    if hb is None:
        return decision
    line = (f"BLOCKING (minority, halted round {hb['round']})"
            f"{HALTED_BLOCKING_RX.sub('', hb['bar_impact'], count=1)}")
    if decision["verdict"] == "REFUTED":
        return dict(decision, verdict="UNVERIFIABLE", bar_impact=line,
                    evidence=(f"split across rounds — halted round {hb['round']}: {hb['verdict']} | "
                              f"round {rnd}: REFUTED — {decision['evidence']}"))
    if not str(decision["bar_impact"] or "").startswith("BLOCKING"):
        return dict(decision, bar_impact=line)
    return decision


def repro_status(r: dict) -> str:
    """REPRODUCED stands only on an assertion failure — the workflow's `reproStatus`."""
    return "BLOCKED" if r.get("status") == "REPRODUCED" and r.get("failure_kind") != "assertion" else r.get("status")


def repro_cell(f: dict) -> str:
    r = f.get("repro")
    if not r:
        return "—"
    return f"{r['status']} {r['test_path']}" if r.get("test_path") else r["status"]


def _one_line(value) -> str:
    s = str(value)
    return s if "\n" not in s else re.sub(r"\s*\n\s*", " ⏎ ", s.strip())


def _finding_block(f: dict, side: str = "n/a") -> list[str]:
    v = {k: _one_line(x) for k, x in (f.get("verification") or {}).items()}
    t = {k: _one_line(f.get(k, d)) for k, d in
         (("title", ""), ("location", "N/A"), ("kind", "?"), ("recommended", "?"), ("claim", ""))}
    out = [f"#### {f['id']} — {t['title']}", ""]
    out.append(f"- **Sources:** {', '.join(f.get('sources', []))}")
    if f.get("carried"):
        out.append("- **Carried from:** " + "; ".join(
            f"{c['id']} (round {c['round']}: {c['verdict']}; bar impact {_one_line(c['bar_impact'])})"
            for c in f["carried"]))
    out.append(f"- **Location:** `{t['location']}`")
    out.append(f"- **Delta:** {side}")
    out.append(f"- **Kind / recommended:** {t['kind']} / {t['recommended']}")
    out.append(f"- **Claim:** {t['claim']}")
    for e in f.get("evidence", []):
        out.append(f"- **Layer evidence [{e.get('source', '?')}]:** {_one_line(e.get('text', ''))}")
    out.append(f"- **Verification:** {v.get('verdict', 'UNVERIFIED')} — {v.get('evidence', 'no verifier verdict returned')}")
    votes = (f.get("verification") or {}).get("votes") or []
    if len(votes) > 1:
        out.append(f"- **Votes:** {', '.join(votes)}")
    r = {k: _one_line(x) for k, x in (f.get("repro") or {}).items()}
    if r:
        out.append(f"- **Repro:** {r.get('status')} ({r.get('failure_kind')}) — `{r.get('test_id') or '-'}` "
                   f"in `{r.get('test_path') or '-'}` — {r.get('tail', '')}")
    impact = (f.get("verification") or {}).get("bar_impact")
    out.append("- bar impact: " + ("MISSING — route back through verification" if impact is None
                                   else v["bar_impact"]))
    out.append("")
    return out


UNREACHED_METHODS = ("seeded", "created", "shared_data", "websocket_or_fault", "rule")


def ui_unreached(layers: list[dict]) -> tuple[str, list[str]] | None:
    """(count text, one line per state) the ui-verifier did not check, each ending in the methods it tried; None when it did not report."""
    ui = next((layer for layer in layers if layer["label"] == "ui-verifier" and not layer.get("failed")), None)
    if ui is None:
        return None
    if "unreached" not in ui:
        return "not reported by this run", []
    items = [f"{_one_line(g.get('state', '?'))} — {_one_line(g.get('reason', ''))} — tried: "
             + "; ".join(f"{_one_line(m['method'])} ({_one_line(m['stopped_by'])})" for m in g["methods_tried"])
             for g in ui["unreached"]]
    return (str(len(items)) if items else "none"), items


OUT_OF_SCOPE_KINDS = ("not_in_spec_or_diff", "destructive_to_seed")


def ui_out_of_scope(layers: list[dict]) -> tuple[str, list[str]] | None:
    """(count text, one line per state) the ui-verifier placed outside the story; None when it did not report."""
    ui = next((layer for layer in layers if layer["label"] == "ui-verifier" and not layer.get("failed")), None)
    if ui is None:
        return None
    if "out_of_scope" not in ui:
        return "not reported by this run", []
    items = [f"{_one_line(g.get('state', '?'))} — {_one_line(g.get('kind', '?'))} — {_one_line(g.get('reason', ''))}"
             for g in ui["out_of_scope"]]
    return (str(len(items)) if items else "none"), items


UI_ACCOUNTING_KEYS = ("states", "checked_ids", "unreached", "out_of_scope")
UI_ACCOUNTING_LISTS = ("ui_unaccounted", "ui_double_placed", "ui_unknown_ids")
UI_UNTESTED = "ui_untested_titles"
UI_UNPLACED = "ui_unplaced_viewports"
UI_UNMAPPED = "ui_unmapped_acs"


def state_accounting(ui: dict) -> tuple[list[str], list[str], list[str]]:
    """(placed nowhere, placed more than once, placed but not in `states`) lines of a ui-verifier report.

    The report places each id of `states` once: in `checked_ids`, or as the `state_id` of an `unreached` or
    `out_of_scope` entry; a duplicate within one list counts as a second placement. A `tests`, `viewports` or
    `acs` entry names states, places none, and joins the third list for each id absent from `states`. A line is
    `<id> — <state text>[ — placed in <list>, …]`, in the order of the workflow's `uiAccounting`, which this mirrors.
    """
    known = {s["id"]: s["state"] for s in ui["states"]}
    placed: dict[str, dict] = {}

    def place(sid: str, where: str, state=None) -> None:
        p = placed.setdefault(sid, {"where": [], "state": ""})
        p["where"].append(where)
        if not p["state"] and isinstance(state, str):
            p["state"] = state

    for sid in ui["checked_ids"]:
        place(sid, "checked_ids")
    for where in ("unreached", "out_of_scope"):
        for entry in ui[where]:
            place(entry["state_id"], where, entry.get("state"))

    def line(sid: str, text: str, where: list[str] | None = None) -> str:
        return " — ".join(part for part in (sid, text, f"placed in {', '.join(where)}" if where else "") if part)

    def text(sid: str) -> str:
        return known.get(sid) or placed[sid]["state"]

    unknown = [line(sid, text(sid), p["where"]) for sid, p in placed.items() if sid not in known]
    unknown += [line(t["state_id"], t["title"], ["tests"]) for t in ui.get("tests") or [] if t["state_id"] not in known]
    unknown += [line(sid, v["viewport"], ["viewports"]) for v in ui.get("viewports") or []
                for sid in v["state_ids"] if sid not in known]
    unknown += [line(sid, a["ac"], ["acs"]) for a in ui.get("acs") or [] for sid in a["state_ids"] if sid not in known]
    return ([line(sid, state) for sid, state in known.items() if sid not in placed],
            [line(sid, text(sid), p["where"]) for sid, p in placed.items() if len(p["where"]) > 1],
            unknown)


def untested_titles(ui: dict) -> list[str]:
    """The dispatched titles (`test_titles`) that no entry of the report's `tests` names, in dispatch order."""
    mapped = {t["title"] for t in ui["tests"]}
    return [title for title in ui["test_titles"] if title not in mapped]


def unplaced_viewports(ui: dict) -> list[str]:
    """The dispatched viewports (`test_viewports`) that no entry of the report's `viewports` names, in dispatch order."""
    mapped = {v["viewport"] for v in ui["viewports"]}
    return [viewport for viewport in ui["test_viewports"] if viewport not in mapped]


def unmapped_acs(ui: dict) -> list[str]:
    """The dispatched AC ids (`spec_acs`) that no entry of the report's `acs` names, in dispatch order."""
    mapped = {a["ac"] for a in ui["acs"]}
    return [ac for ac in ui["spec_acs"] if ac not in mapped]


def _ui_accounting(layers: list[dict], index: int) -> tuple[str, list[str]] | None:
    ui = next((layer for layer in layers if layer["label"] == "ui-verifier" and not layer.get("failed")), None)
    if ui is None:
        return None
    if not all(key in ui for key in UI_ACCOUNTING_KEYS):
        return "not reported by this run", []
    items = [_one_line(item) for item in state_accounting(ui)[index]]
    return (str(len(items)) if items else "none"), items


def ui_unaccounted(layers: list[dict]) -> tuple[str, list[str]] | None:
    """(count text, one line per id) the ui-verifier listed in `states` and placed nowhere; None when it did not report."""
    return _ui_accounting(layers, 0)


def ui_double_placed(layers: list[dict]) -> tuple[str, list[str]] | None:
    """(count text, one line per id) the ui-verifier placed more than once; None when it did not report."""
    return _ui_accounting(layers, 1)


def ui_unknown_ids(layers: list[dict]) -> tuple[str, list[str]] | None:
    """(count text, one line per id) the ui-verifier placed without listing it in `states`; None when it did not report."""
    return _ui_accounting(layers, 2)


def _ui_mapping(layers: list[dict], keys: tuple[str, str], missing_of) -> tuple[str, list[str]] | None:
    """(count text, one line per item) of `missing_of(report)`, the dispatched items no entry names; None when the
    ui-verifier did not report, and `not reported by this run` when the report or the run lacks one of `keys`."""
    ui = next((layer for layer in layers if layer["label"] == "ui-verifier" and not layer.get("failed")), None)
    if ui is None:
        return None
    if not all(key in ui for key in keys):
        return "not reported by this run", []
    items = [_one_line(item) for item in missing_of(ui)]
    return (str(len(items)) if items else "none"), items


def ui_untested_titles(layers: list[dict]) -> tuple[str, list[str]] | None:
    """(count text, one line per title) the dispatch listed and the ui-verifier mapped to no state; None when it did not report."""
    return _ui_mapping(layers, ("tests", "test_titles"), untested_titles)


def ui_unplaced_viewports(layers: list[dict]) -> tuple[str, list[str]] | None:
    """(count text, one line per viewport) the dispatch listed and the ui-verifier mapped to no state; None when it did not report."""
    return _ui_mapping(layers, ("viewports", "test_viewports"), unplaced_viewports)


def ui_unmapped_acs(layers: list[dict]) -> tuple[str, list[str]] | None:
    """(count text, one line per AC id) the dispatch listed and the ui-verifier mapped to no state; None when it did not report."""
    return _ui_mapping(layers, ("acs", "spec_acs"), unmapped_acs)


def _blocking(f: dict) -> bool:
    return str((f.get("verification") or {}).get("bar_impact") or "").startswith("BLOCKING")


def _delta_lines(delta: dict, triage: list[dict], sides: dict[str, str]) -> list[str]:
    """The Summary's `Reviewed:`, `Delta:` (close-out and new-code rounds) and `Loop exit:` lines."""
    reviewed = delta["reviewed"]
    out = [f"- Reviewed: {delta['kind']} {reviewed['base'][:12]}..{reviewed['end'][:12]}"]
    if delta["kind"] == "branch":
        return out + ["- Loop exit: n/a (branch review)"]
    how, lines = delta["how"], delta["lines"]
    if delta["kind"] == "new-code":
        return out + [f"- Delta: new code since close-out {reviewed['base'][:12]} — {lines} diff lines",
                      "- Loop exit: n/a (new-code round: no further round)"]
    if how == "whole":
        out.append(f"- Delta: whole close-out patch — {delta['reason']}, {lines} diff lines")
    else:
        head = f"- Delta: against round {delta['against']['round']}'s close-out {delta['against']['end'][:12]} — "
        out.append(head + (f"same impl, {lines} diff lines" if how == "same-impl" else
                           f"replayed onto {reviewed['base'][:12]} as {delta['replay'][:12]}, {lines} diff lines"))
    inside = [f["id"] for f in triage if sides[f["id"]] == "inside"]
    blocking = [f["id"] for f in triage if _blocking(f)]
    if not triage:
        out.append("- Loop exit: candidate — no findings for triage")
    elif not inside and not blocking:
        out.append(f"- Loop exit: candidate — all {len(triage)} findings lie outside the delta and none is "
                   "bar-blocking; the loop ends unless triage finds a never-deferred finding")
    else:
        line = f"- Loop exit: no — {len(inside)} inside the delta ({', '.join(inside) or 'none'})"
        out.append(line + (f"; bar-blocking {', '.join(blocking)}" if blocking else ""))
    return out


def render(doc: dict) -> str:
    rnd = doc["round"]
    findings = doc["findings"]
    triage = [f for f in findings if (f.get("verification") or {}).get("verdict", "UNVERIFIED") in TRIAGE_VERDICTS]
    refuted = [f for f in findings if (f.get("verification") or {}).get("verdict") == "REFUTED"]
    layers = doc["layers"]
    missing = [layer["label"] for layer in layers if layer.get("failed")]

    out = [f"## Review round {rnd} — {doc['date']}", ""]
    out.append("### Summary")
    out.append("")
    out.append(f"- Diff: `{doc.get('diff_range', '?')}`; mode {doc.get('mode', '?')}; passes {doc.get('passes', 1)}")
    delta = doc["delta"]
    worktree = _worktree_of(doc.get("findings_file"))
    sides = {f["id"]: delta_side(f.get("location", "N/A"), delta, worktree) for f in triage}
    out += _delta_lines(delta, triage, sides)
    out.append(f"- Layers run: {', '.join(layer['label'] for layer in layers)}")
    out.append(f"- Layers with no report after retry: {', '.join(missing) if missing else 'none'}")
    gaps = ui_unreached(layers)
    if gaps:
        out.append(f"- UI states not checked (ui-verifier): {gaps[0]}")
        out += [f"  - {line}" for line in gaps[1]]
    scope = ui_out_of_scope(layers)
    if scope:
        out.append(f"- UI states out of scope (ui-verifier): {scope[0]}")
        out += [f"  - {line}" for line in scope[1]]
    for title, found in (("UI states unaccounted (ui-verifier)", ui_unaccounted(layers)),
                         ("UI states placed more than once (ui-verifier)", ui_double_placed(layers)),
                         ("UI ids not in the state list (ui-verifier)", ui_unknown_ids(layers)),
                         ("UI test titles not mapped (ui-verifier)", ui_untested_titles(layers)),
                         ("UI test viewports not placed (ui-verifier)", ui_unplaced_viewports(layers)),
                         ("UI spec ACs not mapped (ui-verifier)", ui_unmapped_acs(layers))):
        if found:
            out.append(f"- {title}: {found[0]}")
            out += [f"  - {line}" for line in found[1]]
    out.append(f"- Raw findings {doc.get('raw_count', '?')}; after dedupe {len(findings)}; "
               f"for triage {len(triage)}; refuted {len(refuted)}")
    carried_input = doc.get("carried_input") or []
    if carried_input:
        joined = [f"{c['id']} → {f['id']}" for f in findings if f["id"].startswith(f"R{rnd}-")
                  for c in f.get("carried") or []]
        out.append(f"- Carried from halted rounds: {len(carried_input)} ({', '.join(c['id'] for c in carried_input)}); "
                   f"joined round {rnd} findings: {len(joined)} ({', '.join(joined) or 'none'})")
    unverified = doc.get("unverified") or []
    out.append(f"- Findings with no verifier verdict: {', '.join(unverified) if unverified else 'none'}")
    if doc.get("repro_skipped"):
        out.append(f"- Repro stage skipped: {doc['repro_skipped']}")
    else:
        statuses = [f["repro"]["status"] for f in findings if f.get("repro")]
        out.append("- Repro: " + (", ".join(f"{s} {statuses.count(s)}" for s in dict.fromkeys(statuses)) or "none run"))
    out.append("")
    out.append("### Findings for triage")
    out.append("")
    for f in triage:
        out += _finding_block(f, sides[f["id"]])
    if not triage:
        out += ["(none)", ""]
    out.append(f"### Dismissed as false positive — REFUTED ({len(refuted)})")
    out.append("")
    for f in refuted:
        ev = _one_line((f.get("verification") or {}).get("evidence") or "")
        out.append(f"- {f['id']} | {_one_line(f.get('title', ''))} | `{_one_line(f.get('location', 'N/A'))}` | {ev}")
    if not refuted:
        out.append("(none)")
    out.append("")
    out.append("### Raw layer reports")
    out.append("")
    for layer in layers:
        out.append(f"#### {layer['label']}")
        out.append("")
        out.append("```json")
        keys = ("failed", "checked", "findings") + tuple(
            k for k in ("states", "checked_ids", "tests", "viewports", "acs", "unreached", "out_of_scope") if k in layer)
        out.append(json.dumps({k: layer.get(k) for k in keys}, indent=1, ensure_ascii=False))
        out.append("```")
        out.append("")
    return "\n".join(out) + "\n"


def load_run(transcript_dir: str | None = None, run_dir: str | None = None) -> tuple[dict, dict]:
    """Return (the run's result, journal results keyed by agent label).

    `transcript_dir` is Claude Code's Workflow transcript dir; `run_dir` is the replay runner's output dir."""
    if run_dir:
        tdir = os.path.abspath(run_dir).rstrip("\\/")
        run_file = os.path.join(tdir, "run.json")
    else:
        tdir = os.path.abspath(str(transcript_dir)).rstrip("\\/")
        session = os.path.dirname(os.path.dirname(os.path.dirname(tdir)))
        run_file = os.path.join(session, "workflows", f"{os.path.basename(tdir)}.json")
    try:
        with open(run_file, encoding="utf-8") as fh:
            run = json.load(fh)
    except Exception as exc:
        raise RenderError(f"cannot load run file {run_file}: {exc}") from exc
    run_id = run.get("runId") if run_dir else os.path.basename(tdir)
    if run.get("workflowName") != WORKFLOW_NAME or not run_id or run.get("runId") != run_id:
        raise RenderError(f"{run_file} is not {WORKFLOW_NAME} run {run_id}")
    if run.get("status") != "completed":
        raise RenderError(f"run {run_id} status is {run.get('status')!r}, not 'completed'")
    result = run.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("record"), dict):
        raise RenderError(f"run {run_id} returned no record map")

    journal = os.path.join(tdir, "journal.jsonl")
    try:
        with open(journal, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except Exception as exc:
        raise RenderError(f"cannot load {journal}: {exc}") from exc
    agent_of: dict[str, str] = {}
    result_of: dict[str, object] = {}
    for row in rows:
        if row.get("type") == "started" and row.get("label"):
            if row["label"] in agent_of:
                raise RenderError(f"journal label {row['label']!r} started more than once")
            agent_of[row["label"]] = row.get("agentId")
        elif row.get("type") == "result":
            if row.get("agentId") in result_of:
                raise RenderError(f"journal holds two results for agent {row.get('agentId')}")
            result_of[row.get("agentId")] = row.get("result")
    return result, {label: result_of[aid] for label, aid in agent_of.items() if aid in result_of}


def _out_of_scope_list(label: str, claims) -> list[dict]:
    """The ui-verifier's out_of_scope list; anything but dicts with state, a known kind and reason is refused."""
    if not isinstance(claims, list):
        raise RenderError(f"journal result for {label!r} has a malformed out_of_scope list")
    for i, claim in enumerate(claims, 1):
        if not isinstance(claim, dict):
            raise RenderError(f"journal result for {label!r}: out_of_scope item {i} is not an object")
        if claim.get("kind") not in OUT_OF_SCOPE_KINDS:
            raise RenderError(f"journal result for {label!r}: out_of_scope item {i} has kind "
                              f"{claim.get('kind')!r} (expected one of {', '.join(OUT_OF_SCOPE_KINDS)})")
        for key in ("state", "reason"):
            if not isinstance(claim.get(key), str) or not claim[key]:
                raise RenderError(f"journal result for {label!r}: out_of_scope item {i} has no {key}")
    return claims


def _unreached_list(label: str, gaps) -> list[dict]:
    """The ui-verifier's unreached list; anything but dicts whose `methods_tried` is a non-empty list of `{method,
    stopped_by}` (a method of `UNREACHED_METHODS`, text with a non-blank character) is refused — the workflow's rule."""
    if not isinstance(gaps, list) or not all(isinstance(g, dict) for g in gaps):
        raise RenderError(f"journal result for {label!r} has a malformed unreached list")
    for i, gap in enumerate(gaps, 1):
        tried = gap.get("methods_tried")
        if not isinstance(tried, list) or not tried:
            raise RenderError(f"journal result for {label!r}: unreached item {i} has no methods_tried list")
        for item in tried:
            if not isinstance(item, dict):
                raise RenderError(f"journal result for {label!r}: unreached item {i} has a methods_tried item that is not an object")
            if item.get("method") not in UNREACHED_METHODS:
                raise RenderError(f"journal result for {label!r}: unreached item {i} has a methods_tried method "
                                  f"{item.get('method')!r} (expected one of {', '.join(UNREACHED_METHODS)})")
            if not isinstance(item.get("stopped_by"), str) or not item["stopped_by"].strip():
                raise RenderError(f"journal result for {label!r}: unreached item {i} has a methods_tried item with no stopped_by")
    return gaps


def _states_list(label: str, states) -> list[dict]:
    """The ui-verifier's states list; anything but dicts with a unique non-empty id and a non-empty state is refused."""
    if not isinstance(states, list):
        raise RenderError(f"journal result for {label!r} has a malformed states list")
    seen: set[str] = set()
    for i, item in enumerate(states, 1):
        if not isinstance(item, dict):
            raise RenderError(f"journal result for {label!r}: states item {i} is not an object")
        for key in ("id", "state"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise RenderError(f"journal result for {label!r}: states item {i} has no {key}")
        if item["id"] in seen:
            raise RenderError(f"journal result for {label!r}: states repeats the id {item['id']!r}")
        seen.add(item["id"])
    return states


def _checked_ids_list(label: str, ids) -> list[str]:
    """The ui-verifier's checked_ids list; anything but a list of strings is refused."""
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise RenderError(f"journal result for {label!r} has a malformed checked_ids list")
    return ids


def _tests_list(label: str, tests) -> list[dict]:
    """The ui-verifier's tests list; anything but dicts with a non-empty title and state_id is refused."""
    if not isinstance(tests, list):
        raise RenderError(f"journal result for {label!r} has a malformed tests list")
    for i, item in enumerate(tests, 1):
        if not isinstance(item, dict):
            raise RenderError(f"journal result for {label!r}: tests item {i} is not an object")
        for key in ("title", "state_id"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise RenderError(f"journal result for {label!r}: tests item {i} has no {key}")
    return tests


def _state_ids(ids) -> bool:
    """Whether `ids` is a list of non-empty strings."""
    return isinstance(ids, list) and all(isinstance(i, str) and i for i in ids)


def _viewports_list(label: str, viewports) -> list[dict]:
    """The ui-verifier's viewports list; anything but dicts with a non-empty viewport and a non-empty list of state ids is refused."""
    if not isinstance(viewports, list):
        raise RenderError(f"journal result for {label!r} has a malformed viewports list")
    for i, item in enumerate(viewports, 1):
        if not isinstance(item, dict):
            raise RenderError(f"journal result for {label!r}: viewports item {i} is not an object")
        if not isinstance(item.get("viewport"), str) or not item["viewport"]:
            raise RenderError(f"journal result for {label!r}: viewports item {i} has no viewport")
        if not _state_ids(item.get("state_ids")) or not item["state_ids"]:
            raise RenderError(f"journal result for {label!r}: viewports item {i} has no state_ids")
    return viewports


def _acs_list(label: str, acs) -> list[dict]:
    """The ui-verifier's acs list; anything but dicts with a non-empty ac, a list of state ids and, when the list is
    empty, a reason is refused."""
    if not isinstance(acs, list):
        raise RenderError(f"journal result for {label!r} has a malformed acs list")
    for i, item in enumerate(acs, 1):
        if not isinstance(item, dict):
            raise RenderError(f"journal result for {label!r}: acs item {i} is not an object")
        if not isinstance(item.get("ac"), str) or not item["ac"]:
            raise RenderError(f"journal result for {label!r}: acs item {i} has no ac")
        if not _state_ids(item.get("state_ids")):
            raise RenderError(f"journal result for {label!r}: acs item {i} has no state_ids list")
        if "reason" in item and not isinstance(item["reason"], str):
            raise RenderError(f"journal result for {label!r}: acs item {i} has a reason that is not text")
        if not item["state_ids"] and not (item.get("reason") or "").strip():
            raise RenderError(f"journal result for {label!r}: acs item {i} has no state_ids and no reason")
    return acs


def _dispatched_list(key: str, value) -> list[str]:
    """A list the run dispatched (`ui_test_titles`, `ui_test_viewports`, `ui_spec_acs`); anything but a list of
    non-empty strings is refused."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RenderError(f"the run's {key} is not a list of non-empty strings")
    return value


def _require_state_ids(label: str, entry: dict) -> None:
    """Every `unreached` and `out_of_scope` entry of a report that lists its states names a non-empty `state_id`."""
    for where in ("unreached", "out_of_scope"):
        for i, item in enumerate(entry[where], 1):
            if not isinstance(item.get("state_id"), str) or not item["state_id"]:
                raise RenderError(f"journal result for {label!r}: {where} item {i} has no state_id")


def _check_accounting(result: dict, layers: list[dict]) -> None:
    """The run's accounting lists, when it carries them, equal the ones rebuilt from the ui-verifier's journal result."""
    ui = next((layer for layer in layers if layer["label"] == "ui-verifier" and not layer.get("failed")), None)
    if ui is None:
        return
    rebuilt: dict[str, list[str]] = {}
    if all(key in ui for key in UI_ACCOUNTING_KEYS):
        rebuilt.update(zip(UI_ACCOUNTING_LISTS, state_accounting(ui)))
    for key, keys, missing_of in ((UI_UNTESTED, ("tests", "test_titles"), untested_titles),
                                  (UI_UNPLACED, ("viewports", "test_viewports"), unplaced_viewports),
                                  (UI_UNMAPPED, ("acs", "spec_acs"), unmapped_acs)):
        if all(k in ui for k in keys):
            rebuilt[key] = missing_of(ui)
    for key, lines in rebuilt.items():
        if key in result and result[key] != lines:
            raise RenderError(f"rebuilt {key} {lines} differs from the run's {result[key]}")


CARRIED = "carried:"
LAYER_EVIDENCE = re.compile(r"^- \*\*Layer evidence \[(.+?)\]:\*\* (.*)$")
ATTRIBUTED = re.compile(r"^(R\d+:|carried:)")


def _evidence_label(label: str, rnd: int) -> str:
    """A halted block's evidence label as round `rnd` raised it; one already round- or carry-labelled stands."""
    return label if ATTRIBUTED.match(label) else f"R{rnd}:{label}"


def _carried_blocks(result: dict, rec: dict) -> list[dict]:
    """The carry set re-derived from the story's findings files, as staging reads them; the run's `carried_input`
    must name exactly it."""
    path = result["findings_file"]
    vs = review_ledger.variants(str(result["story_id"])) if result.get("story_id") else []
    text = review_ledger.findings_text_of(vs, os.path.dirname(path), path)
    try:
        blocks = review_carry.carried_blocks(text, int(result["round"]))
    except review_carry.CarryError as exc:
        raise RenderError(str(exc)) from exc
    want = sorted((b["id"], b["round"]) for b in blocks)
    got = sorted((c["id"], c["round"]) for c in rec.get("carried_input") or [])
    if want != got:
        raise RenderError(f"record.carried_input differs from the halted findings in {path} and the story's other "
                          f"findings files: missing {sorted(set(want) - set(got))}, "
                          f"extra {sorted(set(got) - set(want))}")
    return blocks


def rebuild(result: dict, by_label: dict) -> dict:
    """The findings document, rebuilt from the run's record map and the journal."""
    rec = result["record"]
    validate_delta(result.get("delta"))

    def agent_result(label: str) -> dict:
        if label not in by_label:
            raise RenderError(f"no journal result for {label!r}")
        r = by_label[label]
        if not isinstance(r, dict):
            raise RenderError(f"journal result for {label!r} is not a structured report")
        return r

    dispatched = {entry_key: _dispatched_list(run_key, result[run_key])
                  for entry_key, run_key in (("test_titles", "ui_test_titles"), ("test_viewports", "ui_test_viewports"),
                                             ("spec_acs", "ui_spec_acs")) if run_key in result}
    layers, raw = [], {}
    for layer in rec["layers"]:
        if layer["failed"]:
            layers.append({"label": layer["label"], "failed": True, "reason": layer["reason"],
                           "checked": "", "findings": []})
            continue
        r = agent_result(layer["journal_label"])
        found = r.get("findings")
        if not isinstance(found, list):
            raise RenderError(f"journal result for {layer['journal_label']!r} has no findings list")
        entry = {"label": layer["label"], "failed": False, "reason": "",
                 "checked": r.get("checked", ""), "findings": found}
        if "unreached" in r:
            entry["unreached"] = _unreached_list(layer["journal_label"], r["unreached"])
        if "out_of_scope" in r:
            entry["out_of_scope"] = _out_of_scope_list(layer["journal_label"], r["out_of_scope"])
        if "states" in r:
            entry["states"] = _states_list(layer["journal_label"], r["states"])
        if "checked_ids" in r:
            entry["checked_ids"] = _checked_ids_list(layer["journal_label"], r["checked_ids"])
        if "tests" in r:
            entry["tests"] = _tests_list(layer["journal_label"], r["tests"])
        if "viewports" in r:
            entry["viewports"] = _viewports_list(layer["journal_label"], r["viewports"])
        if "acs" in r:
            entry["acs"] = _acs_list(layer["journal_label"], r["acs"])
        if layer["label"] == "ui-verifier":
            entry.update(dispatched)
        if all(key in entry for key in UI_ACCOUNTING_KEYS):
            _require_state_ids(layer["journal_label"], entry)
        layers.append(entry)
        for i, f in enumerate(found):
            raw[f"{layer['label']}.{i + 1}"] = f
    if len(raw) != rec["raw_count"]:
        raise RenderError(f"journal yields {len(raw)} raw findings; the run counted {rec['raw_count']}")
    _check_accounting(result, layers)

    blocks = _carried_blocks(result, rec)
    by_block = {b["id"]: b for b in blocks}
    used: set[str] = set()

    findings, unverified = [], []
    for entry in rec["findings"]:
        sources = entry["sources"]
        if not sources:
            raise RenderError(f"{entry['id']}: unresolved raw ids (none listed)")
        missing = [s for s in sources if s not in raw and not (s.startswith(CARRIED) and s[len(CARRIED):] in by_block)]
        if missing:
            raise RenderError(f"{entry['id']}: unresolved raw ids {missing}")
        members = [by_block[s[len(CARRIED):]] for s in sources if s.startswith(CARRIED)]
        used.update(b["id"] for b in members)
        if [(c["id"], c["round"]) for c in entry.get("carried") or []] != [(b["id"], b["round"]) for b in members]:
            raise RenderError(f"{entry['id']}: carried {entry.get('carried')} differs from its carried sources")
        evidence, recommended = [], []
        for n, sid in enumerate(sources):
            if sid.startswith(CARRIED):
                b = by_block[sid[len(CARRIED):]]
                recommended += b["recommended"].split(" / ")
                if n == 0:
                    evidence += [{"source": _evidence_label(m.group(1), b["round"]), "text": m.group(2)}
                                 for m in map(LAYER_EVIDENCE.match, b["lines"]) if m]
                else:
                    evidence.append({"source": sid, "text": f"{b['claim']} (round {b['round']})"})
            else:
                recommended.append(raw[sid].get("recommended"))
                evidence.append({"source": sid, "text": raw[sid].get("evidence", "") if n == 0 else
                                 f"{raw[sid].get('claim', '')} — {raw[sid].get('evidence', '')}"})
        first = by_block[sources[0][len(CARRIED):]] if sources[0].startswith(CARRIED) else raw[sources[0]]
        f = {"id": entry["id"], "title": first.get("title", ""), "location": first.get("location", ""),
             "claim": first.get("claim", ""), "kind": first.get("kind"), "confidence": first.get("confidence"),
             "sources": sources, "evidence": evidence,
             "recommended": " / ".join(dict.fromkeys(recommended))}
        if members:
            f["carried"] = [{k: b[k] for k in ("id", "round", "verdict", "bar_impact")} for b in members]
        if entry["verified_by"]:
            votes = []
            for label in entry["verified_by"]:
                verdicts = agent_result(label).get("verdicts") or []
                v = next((v for v in verdicts if isinstance(v, dict) and v.get("id") == entry["id"]), None)
                if v is None:
                    raise RenderError(f"{entry['id']}: no verdict in {label!r}")
                votes.append(v)
            f["verification"] = apply_halted_blocking(decide(votes), f.get("carried") or [], result["round"])
        else:
            unverified.append(entry["id"])
        if entry.get("repro_by"):
            results = agent_result(entry["repro_by"]).get("results") or []
            r = next((r for r in results if isinstance(r, dict) and r.get("id") == entry["id"]), None)
            if r is None:
                raise RenderError(f"{entry['id']}: no repro result in {entry['repro_by']!r}")
            f["repro"] = {k: r.get(k) for k in ("failure_kind", "test_path", "test_id", "tail")}
            f["repro"]["status"] = repro_status(r)
        elif entry.get("repro_source"):
            # A falsified property carries the test that falsified it; that test is the repro.
            r = raw.get(entry["repro_source"])
            if r is None:
                raise RenderError(f"{entry['id']}: repro source {entry['repro_source']!r} is not a raw finding")
            f["repro"] = {k: r.get(k) for k in ("failure_kind", "test_path", "test_id", "tail")}
            f["repro"]["status"] = repro_status({"status": "REPRODUCED", "failure_kind": r.get("failure_kind")})
        findings.append(f)

    triage_rows = {r["id"]: r for r in result.get("triage", [])}
    refuted_rows = {r["id"]: r for r in result.get("refuted", [])}
    for f in findings:
        verdict = (f.get("verification") or {}).get("verdict", "UNVERIFIED")
        if verdict == "REFUTED":
            row, expect = refuted_rows.get(f["id"]), {}
        else:
            row = triage_rows.get(f["id"])
            expect = {"verdict": verdict, "recommended": f["recommended"], "repro": repro_cell(f),
                      "votes": ", ".join((f.get("verification") or {}).get("votes") or []),
                      "bar_impact": (f.get("verification") or {}).get("bar_impact", "MISSING")}
        if not f["sources"][0].startswith(CARRIED):
            expect.update(title=f["title"], location=f["location"])
        if row is None or any(row.get(k) != v for k, v in expect.items()):
            raise RenderError(f"{f['id']}: rebuilt finding differs from the run's triage rows")
    if unverified != list(result.get("unverified", [])):
        raise RenderError(f"unverified ids {unverified} differ from the run's {result.get('unverified')}")
    if sorted(set(by_block) - used):
        raise RenderError(f"carried ids {sorted(set(by_block) - used)} appear in no finding's sources")

    doc = {k: result[k] for k in DOC_KEYS}
    doc.update(raw_count=len(raw), layers=layers, findings=findings, unverified=unverified,
               repro_skipped=result.get("repro_skipped") or "",
               carried_input=[{"id": c["id"], "round": c["round"]} for c in rec.get("carried_input") or []])
    return doc


def summary(result: dict, ui_gaps: tuple[str, list[str]] | None = None,
            ui_scope: tuple[str, list[str]] | None = None, texts: dict | None = None,
            ui_accounting: tuple | None = None) -> str:
    """The caller's report, from the run file: the Workflow completion message can be truncated.

    `texts` maps a carried-only finding's id to the (title, location) the findings file holds for it.
    `ui_accounting` holds `ui_unaccounted`, `ui_double_placed`, `ui_unknown_ids`, `ui_untested_titles`,
    `ui_unplaced_viewports` and `ui_unmapped_acs` of the layers, in that order.
    """
    texts = texts or {}
    def cell(v) -> str:
        return str(v if v not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")
    failed = result.get("failed_layers") or []
    reasons = result.get("failed_reasons") or {}
    out = [
        "failed_layers: " + (", ".join(f"{l} ({reasons.get(l, '')})" for l in failed) or "none"),
        "skipped_layers: " + (", ".join(map(str, result.get("skipped_layers") or [])) or "none"),
    ]
    if ui_gaps:
        out.append("ui_unreached: " + ("; ".join(ui_gaps[1]) or ui_gaps[0]))
    if ui_scope:
        out.append("ui_out_of_scope: " + ("; ".join(ui_scope[1]) or ui_scope[0]))
    for name, found in zip((*UI_ACCOUNTING_LISTS, UI_UNTESTED, UI_UNPLACED, UI_UNMAPPED), ui_accounting or ()):
        if found:
            out.append(f"{name}: " + ("; ".join(found[1]) or found[0]))
    out += [
        "unverified: " + (", ".join(result.get("unverified") or []) or "none"),
        "repro_skipped: " + (result.get("repro_skipped") or "no"),
        "counts: " + ", ".join(f"{k} {v}" for k, v in (result.get("counts") or {}).items()),
        f"findings_file: {result.get('findings_file')}",
        "",
        "| id | sources | location | delta | title | verdict | votes | bar impact | repro | recommended |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    delta, worktree = result.get("delta"), _worktree_of(result.get("findings_file"))
    def with_text(r: dict) -> dict:
        title, location = texts.get(r.get("id"), (r.get("title"), r.get("location")))
        return dict(r, title=title, location=location)
    for r in map(with_text, result.get("triage") or []):
        r = dict(r, delta=delta_side(r.get("location"), delta, worktree) if delta else "n/a")
        out.append("| " + " | ".join(cell(r.get(k)) for k in (
            "id", "sources", "location", "delta", "title", "verdict", "votes", "bar_impact", "repro",
            "recommended")) + " |")
    refuted = list(map(with_text, result.get("refuted") or []))
    out += ["", f"dismissed as false positive ({len(refuted)}): "
            + ("; ".join(f"{r['id']} {r['title']} ({r['location']})" for r in refuted) or "none")]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--transcript-dir", help="the 'Transcript dir' the Workflow tool printed for this round's run")
    src.add_argument("--run-dir", help="the replay runner's output dir (run.json + journal.jsonl)")
    ap.add_argument("--story-id", required=True)
    ap.add_argument("--round", required=True)
    a = ap.parse_args(argv)
    try:
        result, by_label = load_run(a.transcript_dir, a.run_dir)
        if str(result.get("story_id")) != a.story_id or str(result.get("round")) != a.round:
            raise RenderError(f"run is story {result.get('story_id')} round {result.get('round')}, "
                              f"not story {a.story_id} round {a.round}")
        doc = rebuild(result, by_label)
        path = doc["findings_file"]
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                if re.search(rf"^## Review round {re.escape(str(doc['round']))} ", fh.read(), re.MULTILINE):
                    raise RenderError(f"{path} already holds review round {doc['round']}")
    except (RenderError, KeyError, TypeError) as exc:
        print(f"review-render: {exc}; nothing written", file=sys.stderr)
        return 1

    findings = doc["findings"]
    counts = {}
    for f in findings:
        verdict = (f.get("verification") or {}).get("verdict", "UNVERIFIED")
        counts[verdict] = counts.get(verdict, 0) + 1
    cmd_append(
        str(doc["story_id"]),
        f"review round {doc['round']} recorded — {len(findings)} findings after dedupe "
        f"({counts.get('CONFIRMED', 0)} confirmed, {counts.get('UNVERIFIABLE', 0)} unverifiable, "
        f"{counts.get('REFUTED', 0)} refuted, {counts.get('UNVERIFIED', 0)} without verdict); "
        f"{os.path.basename(path)}; reviewed {doc['delta']['kind']} "
        f"{doc['delta']['reviewed']['base']}..{doc['delta']['reviewed']['end']}",
    )
    # The record line goes first: if the write below fails, a re-run renders the round
    # and story_record de-duplicates the identical record line.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    head = "" if os.path.isfile(path) else f"# Story {doc['story_id']} — code review findings\n\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(head + render(doc))
    print(f"review-render: OK {path}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    texts = {f["id"]: (_one_line(f["title"]), _one_line(f["location"])) for f in findings
             if f["sources"][0].startswith(CARRIED)}
    layers = doc["layers"]
    print(summary(result, ui_unreached(layers), ui_out_of_scope(layers), texts,
                  (ui_unaccounted(layers), ui_double_placed(layers), ui_unknown_ids(layers), ui_untested_titles(layers),
                   ui_unplaced_viewports(layers), ui_unmapped_acs(layers))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
