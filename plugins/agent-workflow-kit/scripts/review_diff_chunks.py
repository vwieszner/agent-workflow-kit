#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
r"""Split a review run's unified diff into chunk files for the review workflow.

A review layer reads its diff with the Read tool, which refuses a call whose result exceeds
25,000 tokens; a layer that stops early reviews part of the change and still reports
"clean". Chunking bounds what one layer must read, and the line counts printed here
are what the workflow checks each layer's `diff_lines_read` against.

Each chunk also carries `pages`: the `[offset, limit]` Read calls that cover it in order,
each at most PAGE_CHARS characters as Read returns them (a line plus its line-number prefix).
Read checks its token limit on the whole line, so a line longer than LINE_CLIP characters
(its source line-number prefix included) is written to the chunk file clipped, ending in
`... [review_diff_chunks: line clipped at LINE_CLIP of <n> chars]`; line counts and line
numbers are unchanged, and `test_titles` / `test_viewports` read the unclipped diff. Diff
text runs 2.2-2.9 characters per token and minified JavaScript about 1.9, so a
40,000-character page stays under about 21,500 tokens even when it is all clipped minified
lines; a line-count limit cannot bound tokens, because one file's lines can be ten times
longer than another's.

Splits only at `diff --git` file boundaries, packing whole file diffs into chunks of
at most `--max-lines` lines; a single file diff longer than that gets a chunk of its
own. Every hunk line is prefixed with its source line number — `{new:>6} ` for an added or
context line, `o{old}` right-aligned in the same width for a removed line — because a layer
otherwise cites the line number the Read tool shows for the diff file. Header lines and
`\ No newline` markers carry a blank prefix of the same width; line counts are unchanged.

Each chunk also carries `test_titles`: the titles of the tests its test and spec files (`*.test.*`,
`*.spec.*`) add or retitle, each once, in the order the diff first shows them. A title is the string
literal that `it(` or `test(` takes first, on the opener's line or the next, after any of `.only`,
`.skip`, `.fixme`, `.fails`, `.concurrent`, `.todo` or `.each(<table>)`; escaped quotes are unescaped
and an `.each` title stays a template. A `describe` title is not a test title. A call counts when one
of its lines is an added line (a title line added under an unchanged `it(` counts, a test whose body
alone changed does not) and never spans two hunks. A member call (`re.test(`), `test.describe`, a hook,
`test.step` and a title built by concatenation are not listed. The review workflow takes the UI diff's
titles from `ui_chunks`, dispatches them to the ui-verifier and names each it did not map to a state.

It also carries `test_viewports`: the viewports the same files set, as `"<width>x<height>"`, each once,
in the order the diff first shows them. A viewport is the pair of integer literals `{ width, height }`
(either key order, on one line or several, nothing else in the object) that `setViewportSize(` takes
or a `viewport:` option takes (`test.use`, `newContext`, `newPage`). A computed or shorthand size, a
size held in a variable or member (`setViewportSize(size)`), a lone width, the getter `viewportSize()`
and a call whose name only ends in `setViewportSize` are not listed. A call counts under the rule the
titles follow: one of its lines is an added line, and it never spans two hunks. The workflow dispatches
the merged list to the ui-verifier and names each viewport it placed in no state.

Writes `diff-<k>.patch` (k from 1) next to the input and prints one JSON
object: {"total_lines": N, "chunks": [{"path": ..., "lines": ..., "files": [...], "pages": [[offset, limit], ...], "test_titles": [...], "test_viewports": [...]}]}.
An empty diff prints `{"total_lines": 0, "chunks": []}` and writes nothing.

Usage
-----
    uv run --no-project <plugin-root>/scripts/review_diff_chunks.py --diff <run_dir>/diff.patch [--max-lines 3000]

Stdlib only.
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
import os
import re
import sys
from collections.abc import Iterator

FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
WIDTH = 7
PAGE_CHARS = 40_000
LINE_CLIP = 2000
READ_PREFIX = 8
TEST_FILE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
# The quote classes exclude the backslash, so a string's escape pairs have one reading and a failed
# match backtracks linearly. The lookbehind keeps `re.test(`, `submit(` and `$it(` out.
TEST_CALL = re.compile(
    r"""(?<![\w.$])(?:it|test)
        (?:\.(?:only|skip|fixme|fails|concurrent|todo))*
        (?:\.each\s*\((?:[^()]|\([^()]*\))*\))?
        \s*\(\s*
        (?:'(?P<single>(?:\\.|[^'\\])*)'|"(?P<double>(?:\\.|[^"\\])*)"|`(?P<back>(?:\\.|[^`\\])*)`)
        \s*[,)]""",
    re.VERBOSE | re.DOTALL,
)
ESCAPED_QUOTE = re.compile(r"\\(['\"`\\$])")
# The object must hold exactly the two integer literals; the lookbehind keeps `resetViewportSize(` and
# `myviewport:` out, and `page.setViewportSize(` in.
VIEWPORT_SET = re.compile(
    r"""(?:(?<![\w$])setViewportSize\s*\(\s*|(?<![\w$])viewport\s*:\s*)
        \{\s*
        (?:width\s*:\s*(?P<w1>\d+)\s*,\s*height\s*:\s*(?P<h1>\d+)
          |height\s*:\s*(?P<h2>\d+)\s*,\s*width\s*:\s*(?P<w2>\d+))
        \s*,?\s*\}""",
    re.VERBOSE,
)


def number_lines(lines: list[str]) -> list[str]:
    """Prefix each hunk line with its source line number (see the module docstring)."""
    out, old, new, in_hunk = [], 0, 0, False
    for line in lines:
        m = HUNK.match(line)
        if m:
            old, new, in_hunk = int(m.group(1)), int(m.group(2)), True
            out.append(" " * WIDTH + line)
        elif FILE_HEADER.match(line.rstrip("\r\n")):
            in_hunk = False
            out.append(" " * WIDTH + line)
        elif in_hunk and line[:1] == "+":
            out.append(f"{new:>{WIDTH - 1}} " + line)
            new += 1
        elif in_hunk and line[:1] == "-":
            out.append(f"o{old}".rjust(WIDTH - 1) + " " + line)
            old += 1
        elif in_hunk and line[:1] == " ":
            out.append(f"{new:>{WIDTH - 1}} " + line)
            old, new = old + 1, new + 1
        else:
            out.append(" " * WIDTH + line)
    return out


def clip(line: str) -> str:
    """`line` cut to LINE_CLIP characters plus a marker naming its full length; line ending kept."""
    body = line.rstrip("\r\n")
    if len(body) <= LINE_CLIP:
        return line
    return f"{body[:LINE_CLIP]} ... [review_diff_chunks: line clipped at {LINE_CLIP} of {len(body)} chars]{line[len(body):]}"


def split_files(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Return (path, lines) per file diff; text before the first header stays with the first file."""
    files: list[tuple[str, list[str]]] = []
    pre: list[str] = []
    for line in lines:
        m = FILE_HEADER.match(line.rstrip("\r\n"))
        if m:
            files.append((m.group(2), [line]))
        elif files:
            files[-1][1].append(line)
        else:
            pre.append(line)
    if pre:
        if files:
            files[0] = (files[0][0], pre + files[0][1])
        else:
            files.append(("(no file header)", pre))
    return files


def added_matches(lines: list[str], pattern: re.Pattern[str]) -> Iterator[re.Match[str]]:
    """Matches of `pattern` in each hunk's new-file text of one file's diff, in diff order, keeping
    those with an added line among the lines they span; a match never spans two hunks."""
    hunks: list[list[tuple[str, bool]]] = []
    current: list[tuple[str, bool]] | None = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.startswith("@@"):
            current = []
            hunks.append(current)
        elif current is not None and line[:1] in ("+", " ", ""):
            current.append((line[1:], line[:1] == "+"))
    for hunk in hunks:
        text = "\n".join(t for t, _ in hunk)
        starts = list(itertools.accumulate((len(t) + 1 for t, _ in hunk), initial=0))
        for m in pattern.finditer(text):
            first = bisect.bisect_right(starts, m.start()) - 1
            last = bisect.bisect_right(starts, m.end() - 1) - 1
            if any(added for _, added in hunk[first:last + 1]):
                yield m


def file_test_titles(lines: list[str]) -> list[str]:
    """Titles of the tests one file's diff adds (see the module docstring), in diff order."""
    titles: list[str] = []
    for m in added_matches(lines, TEST_CALL):
        title = next(m.group(g) for g in ("single", "double", "back") if m.group(g) is not None)
        if title:
            titles.append(ESCAPED_QUOTE.sub(r"\1", title))
    return titles


def file_test_viewports(lines: list[str]) -> list[str]:
    """Viewports one file's diff sets (see the module docstring), as `<width>x<height>`, in diff order."""
    return [f"{m.group('w1') or m.group('w2')}x{m.group('h1') or m.group('h2')}"
            for m in added_matches(lines, VIEWPORT_SET)]


def chunk_test_titles(body: list[str]) -> list[str]:
    """`test_titles` of a chunk: the titles added by its test and spec files, each once, in order."""
    titles = [t for path, lines in split_files(body) if TEST_FILE.search(path) for t in file_test_titles(lines)]
    return list(dict.fromkeys(titles))


def chunk_test_viewports(body: list[str]) -> list[str]:
    """`test_viewports` of a chunk: the viewports set by its test and spec files, each once, in order."""
    viewports = [v for path, lines in split_files(body) if TEST_FILE.search(path) for v in file_test_viewports(lines)]
    return list(dict.fromkeys(viewports))


def pack(files: list[tuple[str, list[str]]], max_lines: int) -> list[tuple[list[str], list[str]]]:
    chunks: list[tuple[list[str], list[str]]] = []
    for path, body in files:
        if chunks and len(chunks[-1][1]) + len(body) <= max_lines:
            chunks[-1][0].append(path)
            chunks[-1][1].extend(body)
        else:
            chunks.append(([path], list(body)))
    return chunks


def page_plan(lines: list[str], budget: int = PAGE_CHARS) -> list[list[int]]:
    """`[offset, limit]` Read calls covering `lines` in order; a page holds at least one line."""
    pages: list[list[int]] = []
    start, size = 1, 0
    for i, line in enumerate(lines, start=1):
        cost = len(line.rstrip("\r\n")) + READ_PREFIX
        if size and size + cost > budget:
            pages.append([start, i - start])
            start, size = i, 0
        size += cost
    if lines:
        pages.append([start, len(lines) - start + 1])
    return pages


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--diff", required=True)
    ap.add_argument("--max-lines", type=int, default=3000)
    a = ap.parse_args()
    try:
        with open(a.diff, encoding="utf-8", errors="replace", newline="") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"review-diff-chunks: cannot read {a.diff}: {exc}", file=sys.stderr)
        return 1
    out_dir = os.path.dirname(os.path.abspath(a.diff))
    chunks = pack(split_files(lines), max(1, a.max_lines)) if lines else []
    report = {"total_lines": len(lines), "chunks": []}
    for k, (paths, body) in enumerate(chunks, start=1):
        path = os.path.join(out_dir, f"diff-{k}.patch").replace("\\", "/")
        numbered = [clip(line) for line in number_lines(body)]
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.writelines(numbered)
        report["chunks"].append({"path": path, "lines": len(body), "files": paths, "pages": page_plan(numbered),
                                 "test_titles": chunk_test_titles(body), "test_viewports": chunk_test_viewports(body)})
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
