#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Convert a design MCP "design context" tool-result JSON into a design-context markdown file.

The design MCP's design-context call returns tens of kilobytes of generated reference code per frame;
the harness persists it to a tool-result file instead of returning it inline. This script does the
conversion on disk: JSON in, `<config: design.intake_dir>/design-context/<slug>.md` out, with a
provenance header. It moves bytes and attaches provenance only: it does not summarise and writes no
"resolved values" section (those are written after reading the converted file, never invented).

Usage
-----
    uv run --no-project .workflow/scripts/design_context.py --json <tool-result.json> --slug <frame-slug> \
        --node <node id> [--file-key <key>] [--ticket <key>] [--note "<one-line caveat>"] [--stdout]

`--file-key` defaults to the screens file (`screens_file_key` of the design manifest,
`config: design.manifest`). The header's styling note is `config: design.context_note` (the project's
styling rule for adapting generated code, e.g. "use the component library's props, not utility classes").

Exit 0 written; 2 bad input (SystemExit message); 3 SKIPPED (`config: design.intake_dir` is empty and no
--out-dir / --stdout). Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

HEADER = """<!--
SOURCE: design context, file {file_key} ({file_label}), node {node}
Extracted: {date}
REFERENCE ONLY. This is the design tool's generated reference code, not code to paste.
{context_note}Use it for exact token values, spacing and structure.
Asset URLs inside expire. This frame draws ONE state: its components' other variants and exact
gradient values are in the design library build (frame-map/{slug}.md and sets/, written by
.workflow/scripts/design_library.py). Generated code may flatten gradient strokes to their first stop.
{extra}-->

# Design context - {slug}

Node `{node}`{ticket_line}
{note_line}
"""


def manifest() -> dict:
    raw = str(wfconfig.get("design.manifest", "") or "")
    path = wfconfig.repo_root() / raw if raw else None
    if path and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def extract_text(payload) -> str:
    """The generated-code text of a tool-result payload: the `text` field of its text blocks."""
    if isinstance(payload, dict):
        payload = payload.get("content", payload.get("data", [payload]))
    if not isinstance(payload, list):
        raise SystemExit(f"unexpected payload shape: {type(payload).__name__}")
    chunks = [b["text"] for b in payload if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    if not chunks:
        raise SystemExit("no text blocks found - was the screenshot the only content?")
    return "\n\n".join(chunks)


def main(argv: list[str] | None = None) -> int:
    src_cfg = (manifest().get("design_source") or {})
    screens, library = src_cfg.get("screens_file_key", ""), src_cfg.get("library_file_key", "")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--file-key", default=screens)
    ap.add_argument("--ticket", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args(argv)

    intake = str(wfconfig.get("design.intake_dir", "") or "")
    if not (args.stdout or args.out_dir or intake):
        print("SKIPPED — config: design.intake_dir is empty (pass --out-dir or --stdout)")
        return 3
    src = Path(args.json)
    if not src.is_file():
        raise SystemExit(f"not found: {src}")
    body = extract_text(json.loads(src.read_text(encoding="utf-8")))
    label = {screens: "screens file", library: "library file"}.get(args.file_key, "file") if args.file_key else "file"
    note = str(wfconfig.get("design.context_note", "") or "")
    text = HEADER.format(
        file_key=args.file_key or "?", file_label=label, node=args.node, date=args.date, slug=args.slug,
        context_note=(note.rstrip() + "\n") if note else "",
        extra=f"NOTE: {args.note}\n" if args.note else "",
        ticket_line=f". Ticket {args.ticket}." if args.ticket else "",
        note_line=f"\n**{args.note}**\n" if args.note else "",
    ) + "\n```tsx\n" + body.strip() + "\n```\n"
    if args.stdout:
        sys.stdout.write(text)
        return 0
    out_dir = Path(args.out_dir) if args.out_dir else wfconfig.repo_root() / intake / "design-context"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{args.slug}.md"
    dest.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {dest} ({len(text):,} chars, {text.count(chr(10)):,} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
