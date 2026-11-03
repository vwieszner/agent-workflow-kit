#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Mechanical half of milestone-blockers Steps 2, 2b and 2d over one milestone's dependency map.
Read-only.

Reads the milestone's map (`config: milestones.<name>.map_file` at `map_key`; schema in the
milestone-blockers skill), the sprint-status rows (`config: paths.sprint_status`), the slot
registry and the failure log, and prints:

  STARTABLE  an open story named in a `parallel_groups` group, not in a slot, not HALTED, not
             listed under `blocked` or `pending`, whose every hard dep (`deps`) is `done`.
             `(bucket stale)` = not in the first group.
  HALTED     an open grouped story with a failure-log entry (the latest entry's step and date), or a
             `blocked` row whose note carries `HALTED <date> at <step>` with no entry (a failed halt,
             `failed_halt: true`); with the hard deps that are not `done`.
  PENDING    an open grouped story listed under `pending` (its deps are not yet known).
  BLOCKED    an open grouped story listed under `blocked`.
  NO-ROW     a grouped story with no sprint-status row.
  UNMAPPED   Step 2b: open rows whose note matches `member_marker`, named in no group and no
             `excluded` list. Skipped (and said so) when `member_marker` is empty.
  UNTRIAGED  Step 2d: backlog / ready-for-dev rows whose note matches neither `member_marker`
             nor `non_member_marker`, or matches `untriaged_marker`. Skipped without `member_marker`.
  DEAD-DEP   Step 2d: a hard dep of an open grouped story whose row is `superseded` or missing.
  REFRESH    Step 2d `empty`: due when no grouped story meets the STARTABLE rule with its slot and
             halted clauses dropped, an open member row exists, and the map's `refreshed` value
             has no `@ <sha>` or `git log --merges <sha>..<config: git.base_branch>` lists a merge.

A story with a `map-refresh` failure-log entry awaits the owner's ruling: it counts toward no
Step 2d reason (left out of UNMAPPED, UNTRIAGED, DEAD-DEP and REFRESH's member count).
Story ids match in dotted and dashed form, and a short id matches a row key that starts with
`<id>-` when exactly one row does.

It never classifies a Step 2c coupling, never places a story in a group, never recommends,
never writes.

Usage:
  uv run --no-project .workflow/scripts/milestone_map_check.py [<milestone>] [--json] [--why KEY]...

`<milestone>` defaults to `config: loop.milestone`, else the only `[milestones.<name>]` table.
Exit 0 with the report; 2 with a JSON error line when the milestone, map or tracker is not found.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bookkeeping as bk  # noqa: E402
import sprint_status  # noqa: E402
import wfconfig  # noqa: E402

OPEN_EXCLUDED = ("done", "superseded")
MAP_REFRESH_STEP = "map-refresh"
STAMP_SHA_RE = re.compile(r"@\s*([0-9a-f]{7,40})\b")
HALTED_NOTE_RE = re.compile(r"HALTED (\S+) at (.+?); worktree kept")
SLOT_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*in_use\s*\|\s*([^|\s]+)\s*\|")


def _die(msg: str) -> None:
    print(json.dumps({"status": "error", "message": msg}))
    raise SystemExit(2)


# ------------------------------------------------------------------ minimal YAML (block subset)

def _scalar(text: str):
    t = text.strip()
    if not t or t in ("~", "null"):
        return None
    if t[0] in "\"'" and t[-1] == t[0] and len(t) >= 2:
        return t[1:-1]
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        return [_scalar(x) for x in _split_flow(inner)] if inner else []
    if t.startswith("{") and t.endswith("}"):
        out = {}
        for part in _split_flow(t[1:-1]):
            k, _, v = part.partition(":")
            out[_scalar(k)] = _scalar(v)
        return out
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return t


def _split_flow(text: str) -> list[str]:
    parts, depth, cur, quote = [], 0, [], None
    for c in text:
        if quote:
            cur.append(c)
            quote = None if c == quote else quote
        elif c in "\"'":
            quote = c
            cur.append(c)
        elif c in "[{":
            depth += 1
            cur.append(c)
        elif c in "]}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _strip_comment(line: str) -> str:
    quote = None
    for i, c in enumerate(line):
        if quote:
            quote = None if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def parse_yaml(text: str):
    """Block mappings, block sequences of scalars, flow lists/maps, quoted and plain scalars,
    `#` comments and `|`/`|-` literal blocks (kept as text). Enough for the dependency-map schema."""
    raw = text.splitlines()
    lines = []
    i = 0
    while i < len(raw):
        s = _strip_comment(raw[i])
        if s.strip():
            lines.append((len(s) - len(s.lstrip(" ")), s.strip(), i))
        i += 1

    def block(pos: int, indent: int):
        if pos >= len(lines):
            return None, pos
        if lines[pos][1].startswith("- ") or lines[pos][1] == "-":
            seq = []
            while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("-"):
                seq.append(_scalar(lines[pos][1][1:]))
                pos += 1
            return seq, pos
        mapping: dict = {}
        while pos < len(lines) and lines[pos][0] == indent:
            content = lines[pos][1]
            key, sep, rest = content.partition(":")
            if not sep:
                pos += 1
                continue
            key = _scalar(key)
            rest = rest.strip()
            pos += 1
            if rest in ("|", "|-", ">", ">-"):
                start_raw = lines[pos][2] if pos < len(lines) else len(raw)
                body = []
                while pos < len(lines) and lines[pos][0] > indent:
                    pos += 1
                end_raw = lines[pos][2] if pos < len(lines) else len(raw)
                body = raw[start_raw:end_raw]
                mapping[key] = "\n".join(b.strip() for b in body)
            elif rest:
                mapping[key] = _scalar(rest)
            elif pos < len(lines) and lines[pos][0] > indent:
                mapping[key], pos = block(pos, lines[pos][0])
            else:
                mapping[key] = None
        return mapping, pos

    if not lines:
        return {}
    value, _ = block(0, lines[0][0])
    return value


def dig(tree, dotted: str):
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# ------------------------------------------------------------------ the check

def milestone_name(given: str | None) -> str:
    if given:
        return given
    name = str(wfconfig.get("loop.milestone", "") or "")
    if name:
        return name
    tables = wfconfig.get("milestones", {}) or {}
    if isinstance(tables, dict) and len(tables) == 1:
        return next(iter(tables))
    _die("no milestone named: pass one, set config loop.milestone, or configure exactly one [milestones.<name>]")
    return ""


def listed(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, str):
        return [v for v in re.split(r"[\s,]+", value) if v]
    return []


class Check:
    def __init__(self, name: str, repo: Path):
        self.name = name
        self.repo = repo
        spec = wfconfig.get(f"milestones.{name}", None)
        if not isinstance(spec, dict):
            _die(f"config milestones.{name} is not set")
        self.spec = spec
        status_path = wfconfig.path("paths.sprint_status", "docs/implementation-artifacts/sprint-status.yaml")
        map_path = repo / str(spec.get("map_file") or "") if spec.get("map_file") else status_path
        if not status_path.is_file():
            _die(f"sprint-status file not found: {status_path}")
        if not map_path.is_file():
            _die(f"map file not found: {map_path}")
        self.rows: dict[str, tuple[str, str]] = {}
        for line in status_path.read_text(encoding="utf-8").splitlines():
            m = sprint_status.ROW_RE.match(line)
            if m:
                note = (m.group("rest") or "").lstrip().lstrip("#").strip()
                self.rows[m.group("key")] = (m.group("status"), note)
        self.ambiguous: dict[str, list[str]] = {}
        tree = parse_yaml(map_path.read_text(encoding="utf-8"))
        key = str(spec.get("map_key") or f"{name}.dependency_map")
        dmap = dig(tree, key)
        if not isinstance(dmap, dict):
            _die(f"map key {key!r} not found in {map_path}")
        self.map = dmap
        self.map_source = f"{map_path}:{key}"
        self.groups: list[tuple[str, list[str]]] = [
            (str(g), listed(v)) for g, v in (dmap.get("parallel_groups") or {}).items()]
        self.excluded = {str(g): listed(v) for g, v in (dmap.get("excluded") or {}).items()} \
            if isinstance(dmap.get("excluded"), dict) else {}
        self.deps = {str(k): listed(v) for k, v in (dmap.get("deps") or {}).items()} \
            if isinstance(dmap.get("deps"), dict) else {}
        self.blocked = set(self._resolve_all(listed(dmap.get("blocked"))))
        self.pending = set(self._resolve_all(listed(dmap.get("pending"))))
        self.slots = self._slots()
        self.halts = self._halts()

    # ---- key resolution
    def resolve(self, tok: str) -> str | None:
        forms = {tok, tok.replace(".", "-"), tok.replace("-", ".")}
        for f in forms:
            if f in self.rows:
                return f
        hits = sorted({k for f in forms for k in self.rows
                       if k.startswith(f + "-") and not k[len(f) + 1:len(f) + 2].isdigit()})
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            self.ambiguous[tok] = hits
        return None

    def _resolve_all(self, toks: list[str]) -> list[str]:
        return [self.resolve(t) or t for t in toks]

    def _slots(self) -> dict[str, int]:
        path = self.repo / bk.registry_rel()
        if not path.is_file():
            return {}
        out = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            m = SLOT_ROW_RE.match(line)
            if m:
                out[self.resolve(m.group(2)) or m.group(2)] = int(m.group(1))
        return out

    def _halts(self) -> dict[str, dict]:
        path = self.repo / bk.failure_log_rel()
        if not path.is_file():
            return {}
        out: dict[str, dict] = {}
        for b in bk.failure_blocks(path.read_text(encoding="utf-8").splitlines()):
            k = self.resolve(b["story_id"]) or b["story_id"]
            steps = out.get(k, {}).get("steps", [])
            out[k] = {"since": b["stamp"], "step": b["step"], "steps": sorted(set(steps) | {b["step"]})}
        return out

    def is_open(self, key: str) -> bool:
        return key in self.rows and self.rows[key][0] not in OPEN_EXCLUDED and not key.startswith("epic-")

    def marker(self, name: str) -> re.Pattern[str] | None:
        raw = str(self.spec.get(name) or "")
        return re.compile(raw) if raw else None

    def _empty(self, startable_any: int, map_refresh: set[str]) -> dict:
        member = self.marker("member_marker")
        open_members = [k for k in self.rows if self.is_open(k) and k not in map_refresh
                        and (member.search(self.rows[k][1]) if member else True)]
        m = STAMP_SHA_RE.search(str(self.map.get("refreshed") or ""))
        out = {"due": False, "startable_incl_slot_halted": startable_any, "open_members": len(open_members),
               "sha": m.group(1) if m else None, "merges": None, "error": None}
        if startable_any or not open_members:
            return out
        if out["sha"] is None:
            out["due"] = True
            return out
        base = str(wfconfig.get("git.base_branch", "development"))
        proc = subprocess.run(["git", "-C", str(self.repo), "log", "--merges", "--oneline", f"{out['sha']}..{base}"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if proc.returncode != 0:
            out["due"], out["error"] = None, f"git: {proc.stderr.strip() or proc.returncode}"
            return out
        out["merges"] = [line for line in proc.stdout.splitlines() if line.strip()]
        out["due"] = bool(out["merges"])
        return out

    def report(self) -> dict:
        first = self.groups[0][0] if self.groups else None
        grouped: dict[str, str] = {}
        for g, members in self.groups:
            for tok in members:
                grouped.setdefault(self.resolve(tok) or tok, g)
        excluded = {self.resolve(t) or t for vs in self.excluded.values() for t in vs}
        map_refresh = {k for k, h in self.halts.items() if MAP_REFRESH_STEP in h["steps"]}
        deps_of = {(self.resolve(k) or k): v for k, v in self.deps.items()}
        startable, halted, pending, blocked, no_row, dead = [], [], [], [], [], []
        startable_any = 0
        for key, group in grouped.items():
            if key not in self.rows:
                no_row.append(key)
                continue
            if not self.is_open(key):
                continue
            deps = deps_of.get(key, [])
            states = []
            for d in deps:
                k = self.resolve(d)
                st = self.rows[k][0] if k else None
                states.append(st)
                if (st is None or st == "superseded") and d not in self.ambiguous and key not in map_refresh:
                    dead.append({"story": key, "dep": d, "dep_key": k, "dep_status": st})
            ready = all(st == "done" for st in states)
            if key not in self.blocked and key not in self.pending and ready:
                startable_any += 1
            if key in self.slots:
                continue
            failed_halt = HALTED_NOTE_RE.search(self.rows[key][1]) if self.rows[key][0] == "blocked" else None
            if key in self.halts or failed_halt:
                h = self.halts.get(key) or {"since": failed_halt.group(1), "step": failed_halt.group(2),
                                            "steps": [], "failed_halt": True}
                halted.append({"story": key, **h,
                               "open_deps": [d for d, st in zip(deps, states) if st != "done"]})
                continue
            if key in self.pending:
                pending.append(key)
                continue
            if key in self.blocked:
                blocked.append(key)
                continue
            if not ready:
                continue
            startable.append({"story": key, "status": self.rows[key][0], "group": group,
                              "bucket_stale": group != first})
        member, non_member = self.marker("member_marker"), self.marker("non_member_marker")
        untriaged_marker = self.marker("untriaged_marker")
        if member:
            set_b = set(grouped) | excluded
            unmapped = sorted(k for k in self.rows if self.is_open(k) and member.search(self.rows[k][1])
                              and k not in set_b and k not in map_refresh)
            untriaged = sorted(
                k for k, (st, note) in self.rows.items()
                if not k.startswith("epic-") and st in ("backlog", "ready-for-dev") and k not in map_refresh
                and ((untriaged_marker and untriaged_marker.search(note))
                     or not (member.search(note) or (non_member and non_member.search(note)))))
        else:
            unmapped = untriaged = None
        return {
            "status": "ok", "milestone": self.name, "source": self.map_source,
            "refreshed": self.map.get("refreshed"), "slots": self.slots,
            "startable": startable, "halted": halted, "pending": sorted(pending), "blocked": sorted(blocked),
            "no_row": sorted(no_row), "unmapped": unmapped, "untriaged": untriaged, "dead_dep": dead,
            "map_refresh": sorted(map_refresh), "empty": self._empty(startable_any, map_refresh),
            "ambiguous": self.ambiguous,
        }

    def why(self, tok: str) -> list[str]:
        key = self.resolve(tok) or tok
        out = [f"{tok} → {key if key in self.rows else 'no row'}"]
        for g, members in self.groups:
            if any((self.resolve(m) or m) == key for m in members):
                out.append(f"  group {g}")
        for g, members in self.excluded.items():
            if any((self.resolve(m) or m) == key for m in members):
                out.append(f"  excluded {g}")
        deps = {(self.resolve(k) or k): v for k, v in self.deps.items()}.get(key)
        if deps is not None:
            out.append(f"  deps ← {', '.join(deps) or '(none)'}")
        return out


def empty_line(e: dict) -> str:
    if e["due"] is None:
        return f"unknown ({e['error']})"
    if e["startable_incl_slot_halted"]:
        return f"not due ({e['startable_incl_slot_halted']} startable, slot and halted counted)"
    if not e["open_members"]:
        return "not due (no open member row)"
    if e["sha"] is None:
        return "due (no `@ <sha>` in the map's refreshed value)"
    if e["due"]:
        return f"due ({len(e['merges'])} merge(s) since {e['sha']})"
    return f"not due (no merge since {e['sha']})"


def print_text(r: dict) -> None:
    print(f"milestone: {r['milestone']} ({r['source']}); refreshed: {r['refreshed']}")
    print(f"slots in use: {', '.join(f'{s}={k}' for k, s in sorted(r['slots'].items(), key=lambda x: x[1])) or 'none'}")
    print(f"\nSTARTABLE ({len(r['startable'])})")
    for s in r["startable"]:
        print(f"  {s['story']:<24} {s['status']:<13} {s['group']}{'  (bucket stale)' if s['bucket_stale'] else ''}")
    print(f"\nHALTED ({len(r['halted'])})")
    for s in r["halted"]:
        due = ("open: " + ", ".join(s["open_deps"])) if s["open_deps"] else "resume decision due"
        print(f"  {s['story']:<24} {s['step']}  since {s['since']}  {due}")
    for title, keys in (("PENDING", r["pending"]), ("BLOCKED", r["blocked"]), ("NO-ROW", r["no_row"])):
        print(f"\n{title} ({len(keys)}): {', '.join(keys) or 'none'}")
    for title, keys in (("UNMAPPED", r["unmapped"]), ("UNTRIAGED", r["untriaged"])):
        if keys is None:
            print(f"\n{title}: skipped (no member_marker configured for {r['milestone']})")
        else:
            print(f"\n{title} ({len(keys)}): {', '.join(keys) or 'none'}")
    print(f"\nDEAD-DEP ({len(r['dead_dep'])})")
    for d in r["dead_dep"]:
        print(f"  {d['story']:<24} ← {d['dep']} ({d['dep_status'] or 'no row'})")
    print(f"\nMAP-REFRESH (excluded from Step 2d) ({len(r['map_refresh'])}): {', '.join(r['map_refresh']) or 'none'}")
    print(f"\nREFRESH empty: {empty_line(r['empty'])}")
    print(f"\nAMBIGUOUS ({len(r['ambiguous'])})")
    for tok, hits in sorted(r["ambiguous"].items()):
        print(f"  {tok} → {', '.join(hits[:6])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="read-only milestone-map check (milestone-blockers Steps 2, 2b, 2d)")
    ap.add_argument("milestone", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--why", action="append", default=[], metavar="KEY")
    args = ap.parse_args(argv)
    check = Check(milestone_name(args.milestone), wfconfig.repo_root())
    if args.why:
        for w in args.why:
            print("\n".join(check.why(w)))
        return 0
    r = check.report()
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    else:
        print_text(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
