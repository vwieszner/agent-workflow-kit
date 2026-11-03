#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Reassemble the design tool's token export from a session transcript, and rebuild the app theme from it.

    uv run --no-project .workflow/scripts/design_tokens.py export --tag <tag> [--session <id>]
                        [--transcripts-dir <dir>] [--worktree <root>]
    uv run --no-project .workflow/scripts/design_tokens.py build --project-name <stack> [--worktree <root>]

``export`` reads the ``@@ML|styles|<tag>|<part>|<parts>|<total>`` chunks that ``design_extract.js``
``stylesDump`` left in a Claude Code session transcript (the design MCP calls are made by a main session),
reassembles them exactly as ``design_library.py --from-transcripts`` does (the length check counts
UTF-16 code units, as JavaScript does) and writes ``config: design.tokens_export`` with every list sorted
by name, so an unchanged token set rewrites an unchanged file. Transcripts are searched in this
project's transcript dirs (``_transcripts.project_dirs()``) unless ``--transcripts-dir`` is given.

``build`` runs the project's token build in the story stack (``config: design.tokens_build``:
``service``, ``inputs`` [[worktree path, container path], ...] copied in, ``cmd`` (argv list) run with
``exec -T``, ``copy_back`` [[worktree dir, container dir], ...] copied out), because a live-sync watch
syncs host to container only. Do not run it while the stack runs a suite.

Exit 0 ok; 2 a failed command or incomplete chunks (SystemExit message); 3 SKIPPED (not configured).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _stack  # noqa: E402
import _transcripts  # noqa: E402
import wfconfig  # noqa: E402

CHUNK_PREFIX = "@@ML|styles|"
LISTS = ("collections", "variables", "text", "effect", "paint", "grid")


def export_rel() -> Path | None:
    raw = str(wfconfig.get("design.tokens_export", "") or "")
    return Path(raw) if raw else None


def transcript_files(root: Path, session: str | None) -> list[tuple[str, Path]]:
    """(session id, file) pairs: a session's own transcript plus its subagents', or every transcript."""
    if session:
        pairs: list[tuple[str, Path]] = []
        main = root / f"{session}.jsonl"
        if main.exists():
            pairs.append((session, main))
        sub = root / session / "subagents"
        if sub.exists():
            pairs += [(session, p) for p in sorted(sub.glob("*.jsonl"))]
        return pairs
    pairs = [(p.stem, p) for p in sorted(root.glob("*.jsonl"))]
    pairs += [(p.parent.parent.name, p) for p in sorted(root.glob("*/subagents/*.jsonl"))]
    return pairs


def tool_texts(path: Path) -> list[str]:
    """The ``stylesDump`` chunk texts of one transcript, in file order."""
    texts: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "tool_result" not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if c.get("type") != "tool_result":
                    continue
                body = c.get("content")
                items = body if isinstance(body, list) else [{"type": "text", "text": body}]
                for it in items:
                    t = it.get("text") if isinstance(it, dict) else None
                    if not isinstance(t, str):
                        continue
                    if t.startswith('"'):
                        try:
                            t = json.loads(t)
                        except json.JSONDecodeError:
                            pass
                    if t.startswith(CHUNK_PREFIX):
                        texts.append(t)
    return texts


def collect_chunks(root: Path, session: str | None, tag: str) -> dict[str, dict]:
    """Per session: {"parts", "total", "got": {part: text}} for the styles chunks carrying ``tag``."""
    found: dict[str, dict] = {}
    for session_id, path in transcript_files(root, session):
        for text in tool_texts(path):
            head, _, body = text.partition("\n")
            fields = head.split("|")
            if len(fields) != 6 or fields[2] != tag:
                continue
            _, _, _, part, nparts, total = fields
            slot = found.setdefault(session_id, {"parts": int(nparts), "total": int(total), "got": {}})
            if (slot["parts"], slot["total"]) != (int(nparts), int(total)):
                raise SystemExit(
                    f"tag {tag}: session {session_id} carries chunks with different headers "
                    f"({slot['parts']} parts / {slot['total']} chars and {nparts} parts / {total} chars)"
                )
            slot["got"][int(part)] = body
    return found


def reassemble(tag: str, slot: dict) -> dict:
    missing = [i for i in range(slot["parts"]) if i not in slot["got"]]
    if missing:
        raise SystemExit(f"tag {tag} is incomplete: have {sorted(slot['got'])} of {slot['parts']} parts")
    joined = "".join(slot["got"][i] for i in range(slot["parts"]))
    # The chunks are cut on UTF-16 code units, so a cut can fall between the two halves of a surrogate pair:
    # the halves reach Python as lone surrogates and only a UTF-16 round-trip recombines them.
    utf16 = joined.encode("utf-16-le", "surrogatepass")
    units = len(utf16) // 2  # JS string length counts UTF-16 code units
    if units != slot["total"]:
        raise SystemExit(f"tag {tag}: {units} UTF-16 units reassembled, the header says {slot['total']}")
    return json.loads(utf16.decode("utf-16-le", "surrogatepass"))


def sort_lists(data: dict) -> dict:
    for key in LISTS:
        if key in data:
            data[key] = sorted(data[key], key=lambda row: (row[1], row[0]))
    return data


def render(data: dict) -> str:
    """Sorted keys; each list row on one line, so a changed variable is a one-line diff."""
    parts = []
    for key in sorted(data):
        value = data[key]
        if key in LISTS:
            rows = ",\n".join(
                "    " + json.dumps(row, ensure_ascii=False, separators=(", ", ": ")) for row in value
            )
            parts.append(f'  {json.dumps(key)}: [\n{rows}\n  ]' if rows else f'  {json.dumps(key)}: []')
        else:
            parts.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def cmd_export(args: argparse.Namespace) -> int:
    export = export_rel()
    if export is None:
        print("SKIPPED — config: design.tokens_export is empty")
        return 3
    roots = [Path(args.transcripts_dir)] if args.transcripts_dir else [Path(p) for p in _transcripts.project_dirs()]
    found: dict[str, dict] = {}
    for root in roots:
        found.update(collect_chunks(root, args.session, args.tag))
    if not found:
        where = f"session {args.session}" if args.session else ", ".join(str(r) for r in roots)
        raise SystemExit(f"tag {args.tag}: no @@ML|styles chunks found in {where}")
    if len(found) > 1:
        raise SystemExit(
            f"tag {args.tag} appears in {len(found)} sessions ({', '.join(sorted(found))}); pass --session"
        )
    ((session_id, slot),) = found.items()
    data = sort_lists(reassemble(args.tag, slot))
    text = render(data)
    # `write_text` truncates the file before it encodes, so an unpaired surrogate must fail before the open.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SystemExit(
            f"tag {args.tag}: the reassembled text holds an unpaired UTF-16 surrogate ({error.reason}); "
            "re-dump with a fresh tag"
        ) from None
    out = Path(args.worktree) / export
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(
        f"export: tag {args.tag}, session {session_id}, {slot['parts']} parts, {slot['total']} chars -> {out}"
    )
    return 0


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd)}")


def cmd_build(args: argparse.Namespace) -> int:
    spec = wfconfig.get("design.tokens_build", {}) or {}
    if not isinstance(spec, dict) or not spec.get("service") or not spec.get("cmd"):
        print("SKIPPED — config: design.tokens_build needs service and cmd")
        return 3
    if not wfconfig.stack_enabled():
        print("SKIPPED — stack.runtime is none: no stack to build the tokens in")
        return 3
    service = str(spec["service"])
    root = Path(args.worktree)
    for local, container in spec.get("inputs") or []:
        run(_stack.compose_argv(args.project_name, "cp", str(root / local), f"{service}:{container}"))
    cmd = spec["cmd"] if isinstance(spec["cmd"], list) else str(spec["cmd"]).split()
    run(_stack.compose_argv(args.project_name, "exec", "-T", service, *[str(c) for c in cmd]))
    for local, container in spec.get("copy_back") or []:
        target = root / local
        target.mkdir(parents=True, exist_ok=True)
        run(_stack.compose_argv(args.project_name, "cp", f"{service}:{str(container).rstrip('/')}/.", str(target)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="reassemble the token export from a session transcript")
    export.add_argument("--tag", required=True)
    export.add_argument("--session", help="the orchestrator's session id; required when the tag is in several")
    export.add_argument("--transcripts-dir", default=None)
    export.add_argument("--worktree", default=str(wfconfig.repo_root()))
    export.set_defaults(func=cmd_export)

    build = sub.add_parser("build", help="run the token build in the stack and copy the output back")
    build.add_argument("--project-name", required=True)
    build.add_argument("--worktree", default=str(wfconfig.repo_root()))
    build.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
