#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Save design MCP page renders into the design library's `renders/_pages/` folder.

    uv run --no-project .workflow/scripts/design_page_renders.py --map <names-to-urls.json> [--dir <folder>]

`--map` is a JSON object `{"<name>.png": "<asset url>"}`: the asset URLs the design MCP's screenshot
call returned (they expire soon after; save them right after the calls). Every URL must match
`config: design.render_url_regex` (default: the Figma MCP asset URL form) and every download must
carry the PNG signature before anything is written; one failure exits 1 and writes nothing.
`design_library.py` reads these files back (`renders/_pages/<key>*.png`) to crop set and variant
renders. Default folder: `<config: design.library_dir>/renders/_pages`.

TLS uses the system trust store through Python's default SSL context; on a host whose TLS-inspecting
proxy root is not in it, set SSL_CERT_FILE to the proxy's CA bundle.

Exit 0 saved; 1 refused or a failed download; 3 SKIPPED (no folder configured). Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.png$")
DEFAULT_URL_RE = r"^https://www\.figma\.com/api/mcp/asset/[0-9a-f-]+\.png$"
TIMEOUT = 60


def default_dir() -> Path | None:
    lib = str(wfconfig.get("design.library_dir", "") or "")
    return wfconfig.repo_root() / lib / "renders" / "_pages" if lib else None


def download(url: str, opener=None) -> tuple[int, bytes]:
    try:
        with (opener or urllib.request.urlopen)(url, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except urllib.error.URLError:
        return 0, b""


def main(argv: list[str] | None = None, opener=None) -> int:
    ap = argparse.ArgumentParser(description="Save design MCP page renders into renders/_pages/.")
    ap.add_argument("--map", required=True, type=Path, help="JSON object {name.png: asset url}")
    ap.add_argument("--dir", type=Path, default=None)
    args = ap.parse_args(argv)
    out_dir = args.dir or default_dir()
    if out_dir is None:
        print("SKIPPED — config: design.library_dir is empty (pass --dir)")
        return 3
    url_re = re.compile(str(wfconfig.get("design.render_url_regex", DEFAULT_URL_RE) or DEFAULT_URL_RE))
    pairs = json.loads(args.map.read_text(encoding="utf-8"))
    if not isinstance(pairs, dict) or not pairs:
        print("refused: --map must be a non-empty JSON object", file=sys.stderr)
        return 1
    refused = [n for n, u in pairs.items() if not NAME_RE.match(n) or not isinstance(u, str) or not url_re.match(u)]
    if refused:
        print(f"refused: {len(refused)} entries fail the name or URL check: {refused}", file=sys.stderr)
        return 1
    blobs: dict[str, bytes] = {}
    for name, url in pairs.items():
        status, data = download(url, opener)
        if status != 200 or not data.startswith(PNG_SIGNATURE):
            print(f"FAIL {name}: HTTP {status}, {len(data)} bytes, not written", file=sys.stderr)
            return 1
        blobs[name] = data
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in blobs.items():
        (out_dir / name).write_bytes(data)
        print(f"{name}  {len(data)} bytes")
    print(f"saved {len(blobs)} renders to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
