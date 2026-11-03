#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10.0"]
# ///
"""Rebuild the design intake's binary assets (renders and docs-page images), which are not committed.

    uv run --no-project <plugin-root>/scripts/design_assets.py [--design-only | --docs-only]

Writes under `config: design.intake_dir`:

    assets/<slug>.png                 one PNG render per frame of the design manifest
                                      (`design_source.frames`, from `design_source.screens_file_key`)
    assets/<group dir>/<slug>.<ext>   docs-page attachments per `attachments.groups`, downscaled
    assets-master/<group dir>/...     the full-resolution attachment originals

Keep both `assets/` and `assets-master/` out of version control; this script is what rebuilds them in
a fresh clone.

Manifest (`config: design.manifest`) — the `attachments` section is optional:

    "attachments": {"groups": [
      {"dir": "portraits", "mode": "newest", "pages": [{"slug": "ada", "page": "123"}],
       "max_edge": 768, "format": "jpeg", "quality": 85},
      {"dir": "backdrops", "mode": "all", "page": "456", "prefix": "backdrop",
       "max_edge": 1600, "format": "jpeg", "quality": 85}
    ]}

`newest` takes the most recently created attachment of each listed page; `all` takes every attachment
of one page in id order, named `<prefix>-NN`. `max_edge` null keeps the original size.

Credentials (environment variables only, never stored):
    design source   `config: design.token_env` (also read from the repo's `.env`)
    docs pages      `config: docs_mirror.user_env` / `.token_env` (falling back to `config: tracker.*`);
                    site `config: docs_mirror.site` (falling back to `config: tracker.site`)
A phase without credentials is SKIPPED with the variable names, not failed.

Pillow resizes and re-encodes; without it every file is written at its original size and the run
says so.

Exit codes: 0 at least one file written; 1 nothing written; 2 network or HTTP error;
3 SKIPPED (`config: design.intake_dir` or `config: design.manifest` unset).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

try:
    from PIL import Image  # type: ignore[import-not-found]
except ImportError:  # optional: originals are written unresized
    Image = None

DESIGN_API = "https://api.figma.com"
TIMEOUT_S = 120
SKIPPED = 3


class FetchError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def _cfg(key: str, default=""):
    val = wfconfig.get(key, default)
    return default if val is None else val


def _path(key: str) -> Path | None:
    raw = str(_cfg(key, "") or "")
    return wfconfig.repo_root() / raw if raw else None


def read_env_file(key: str) -> str | None:
    env_path = wfconfig.repo_root() / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _get(url: str, headers: dict, opener=None) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode("utf-8", "replace") if hasattr(exc, "read") else ""
        hint = ""
        if exc.code in (401, 403):
            hint = " — the token is rejected (expired or lacking access); mint a new one"
        raise FetchError(f"HTTP {exc.code} from {url}{hint}\n{body}") from None
    except urllib.error.URLError as exc:
        raise FetchError(f"network unreachable for {url}: {exc.reason}") from None


def save_image(blob: bytes, dest: Path, max_edge: int | None, fmt: str, quality: int = 85) -> str:
    """Write blob to dest, downscaling the long edge to max_edge when Pillow is present. Returns 'WxH'."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if Image is None:
        dest.write_bytes(blob)
        return "?x?"
    with Image.open(BytesIO(blob)) as img:
        if max_edge is None:
            dest.write_bytes(blob)
            return f"{img.width}x{img.height}"
        out = img.convert("RGB") if fmt == "jpeg" else img
        scale = min(1.0, max_edge / max(out.width, out.height))
        if scale < 1.0:
            out = out.resize((round(out.width * scale), round(out.height * scale)), Image.Resampling.LANCZOS)
        if fmt == "jpeg":
            out.save(dest, "JPEG", quality=quality, optimize=True)
        else:
            out.save(dest, fmt.upper())
        return f"{out.width}x{out.height}"


# ------------------------------------------------------------------ design source

def fetch_design(source: dict, assets: Path, token: str, opener=None) -> int:
    """Export every manifest frame as PNG through the design source's images endpoint."""
    file_key = source["screens_file_key"]
    by_node = {f["node"]: f["slug"] for f in source.get("frames", [])}
    if not by_node:
        log("SKIP design renders: the manifest lists no frames")
        return 0
    log(f"design source: exporting {len(by_node)} frame(s) from {file_key}")
    params = urllib.parse.urlencode({"ids": ",".join(by_node), "format": "png",
                                     "scale": source.get("render_scale", 1)})
    payload = json.loads(_get(f"{DESIGN_API}/v1/images/{file_key}?{params}", {"X-Figma-Token": token}, opener))
    if payload.get("err"):
        raise FetchError(f"design source export failed: {payload['err']}")
    written = 0
    for node, url in (payload.get("images") or {}).items():
        slug = by_node.get(node) or by_node.get(node.replace("-", ":"))
        if not slug:
            log(f"  ?  unexpected node in response: {node}")
            continue
        if not url:
            log(f"  !! {slug:38} no render URL (node {node} missing?)")
            continue
        blob = _get(url, {}, opener)
        dims = save_image(blob, assets / f"{slug}.png", None, "png")
        log(f"  ok {slug:38} {dims:>11}  {len(blob):>9,} B")
        written += 1
    return written


# ------------------------------------------------------------------ docs pages

def docs_auth() -> tuple[str, dict] | None:
    site = str(_cfg("docs_mirror.site", "") or _cfg("tracker.site", "")).rstrip("/")
    user_env = str(_cfg("docs_mirror.user_env", "") or _cfg("tracker.user_env", "TRACKER_USER"))
    token_env = str(_cfg("docs_mirror.token_env", "") or _cfg("tracker.token_env", "TRACKER_TOKEN"))
    user, token = os.environ.get(user_env, ""), os.environ.get(token_env, "")
    if not (site and user and token):
        log(f"SKIP docs-page images: set config docs_mirror.site (or tracker.site) and the environment "
            f"variables {user_env} and {token_env}")
        return None
    raw = base64.b64encode(f"{user}:{token}".encode()).decode()
    return site, {"Authorization": f"Basic {raw}", "Accept": "application/json"}


def _attachments(site: str, headers: dict, page: str, params: dict, opener=None) -> list[dict]:
    url = f"{site}/wiki/api/v2/pages/{page}/attachments?{urllib.parse.urlencode(params)}"
    return json.loads(_get(url, headers, opener)).get("results", [])


def _download(site: str, headers: dict, att: dict, opener=None) -> bytes:
    link = att["downloadLink"]
    return _get(link if link.startswith("http") else f"{site}/wiki{link}", headers, opener)


def _write_attachment(blob: bytes, group: dict, slug: str, root: Path) -> str:
    fmt = str(group.get("format", "jpeg"))
    ext = "jpg" if fmt == "jpeg" else fmt
    master = root / "assets-master" / group["dir"] / f"{slug}.{ext}"
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(blob)
    return save_image(blob, root / "assets" / group["dir"] / f"{slug}.{ext}",
                      group.get("max_edge"), fmt, int(group.get("quality", 85)))


def fetch_docs(groups: list[dict], root: Path, site: str, headers: dict, opener=None) -> int:
    written = 0
    for group in groups:
        mode = group.get("mode", "newest")
        if mode == "newest":
            pages = group.get("pages", [])
            log(f"docs pages: {len(pages)} image(s) for {group['dir']} (newest attachment per page)")
            for entry in pages:
                found = _attachments(site, headers, entry["page"], {"sort": "-created-date", "limit": 1}, opener)
                if not found:
                    log(f"  !! {entry['slug']:30} page {entry['page']} has no attachments")
                    continue
                blob = _download(site, headers, found[0], opener)
                dims = _write_attachment(blob, group, entry["slug"], root)
                log(f"  ok {entry['slug']:30} {dims:>10}  master {len(blob):>9,} B")
                written += 1
        elif mode == "all":
            atts = _attachments(site, headers, group["page"], {"limit": 250}, opener)
            log(f"docs pages: {len(atts)} image(s) for {group['dir']} from page {group['page']}")
            for i, att in enumerate(sorted(atts, key=lambda a: str(a["id"])), start=1):
                slug = f"{group.get('prefix', group['dir'])}-{i:02d}"
                blob = _download(site, headers, att, opener)
                dims = _write_attachment(blob, group, slug, root)
                log(f"  ok {slug:30} {dims:>10}  master {len(blob):>9,} B")
                written += 1
        else:
            log(f"  !! group {group.get('dir')!r}: unknown mode {mode!r} (newest | all) — skipped")
    return written


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None, opener=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    only = ap.add_mutually_exclusive_group()
    only.add_argument("--design-only", action="store_true", help="skip the docs-page phase")
    only.add_argument("--docs-only", action="store_true", help="skip the design-render phase")
    args = ap.parse_args(argv)

    root, manifest_path = _path("design.intake_dir"), _path("design.manifest")
    if root is None or manifest_path is None:
        log("SKIPPED — config design.intake_dir and design.manifest must both be set")
        return SKIPPED
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if Image is None:
        log("note: Pillow is not installed — files are written at their original size (run through uv to resize)")
    total = 0
    try:
        if not args.docs_only:
            env = str(_cfg("design.token_env", "FIGMA_ACCESS_TOKEN") or "FIGMA_ACCESS_TOKEN")
            token = os.environ.get(env) or read_env_file(env)
            source = manifest.get("design_source") or {}
            if not token:
                log(f"SKIP design renders: {env} is not set (environment or the repo's .env)")
            elif not source.get("screens_file_key"):
                log("SKIP design renders: the manifest has no design_source.screens_file_key")
            else:
                total += fetch_design(source, root / "assets", token, opener)
        if not args.design_only:
            groups = (manifest.get("attachments") or {}).get("groups") or []
            if not groups:
                log("SKIP docs-page images: the manifest has no attachments.groups")
            else:
                auth = docs_auth()
                if auth:
                    total += fetch_docs(groups, root, *auth, opener=opener)
    except FetchError as exc:
        log(f"\n{exc}")
        return 2
    log(f"\n{total} file(s) written under {root}")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
