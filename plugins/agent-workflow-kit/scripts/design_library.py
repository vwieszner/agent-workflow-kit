#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10.0"]
# ///
"""Download the design source's component library and map every screen frame onto it.

    uv run --no-project <plugin-root>/scripts/design_library.py                 # REST fetch + build
    uv run --no-project <plugin-root>/scripts/design_library.py --from-transcripts [--session <id>]
    uv run --no-project <plugin-root>/scripts/design_library.py --build-only
    uv run --no-project <plugin-root>/scripts/design_library.py --changes          # 2 REST calls, no build
    uv run --no-project <plugin-root>/scripts/design_library.py --plan             # the MCP calls a refresh needs
    uv run --no-project <plugin-root>/scripts/design_library.py --from-transcripts --session <id> --accept-fingerprints

The design source is Figma (REST API and the Figma MCP). Story specs read the library's component
variants (hover, active, error, collapsed, ...), not only the flat screen frames. This script produces
those inputs under `config: design.library_dir`:

    INDEX.md                        tracked — every component set + standalone component, variants,
                                    node IDs, keys, last update, value-sheet and render paths
    sets/<slug>.md                  tracked — per set: every variant's node tree with exact values
                                    (size, auto-layout, fills incl. gradient stops, per-side strokes,
                                    radii, effects, named styles, typography, copy, instance targets),
                                    then a state diff: each non-default variant vs its default sibling
    frame-map/<frame-slug>.md       tracked — per screen frame of the design manifest: every component it
                                    instances, resolved to its library set/variant or flagged LOCAL (a copy
                                    that is not the library component — where a screen drifts from the
                                    library), the fields its instances override, and the frame's text
    frames/, flows.md, tokens.md    tracked — screen values, prototype wiring, styles and variables
    raw/                            git-ignored — node JSON (REST, or MCP dumps converted to REST shape)
    renders/                        git-ignored — _pages/ page renders; <set>/<variant>.png,
                                    _standalone/<name>.png and _screens/<frame>.png cropped from them

The design manifest (`config: design.manifest`, JSON; example: `.workflow/scripts/design_manifest.example.json`)
holds `design_source.library_file_key`, `design_source.screens_file_key`, `design_source.render_scale` and
`design_source.frames` ([{slug, node, ticket?}]).

Two sources, one builder:

* **REST** (default): needs the token in the environment variable `config: design.token_env` (or the
  repo's `.env`) on a seat with REST quota. A seat without quota answers 429 with a multi-day
  `Retry-After`; the run aborts.
* **MCP** (`--from-transcripts`): the calls are made by a main session through the design MCP's
  plugin-code tool with `.workflow/scripts/design_extract.js` (its header documents the call shapes), and page
  renders through the screenshot tool saved with `design_page_renders.py`. This mode reads the `@@ML`
  chunks back out of the Claude Code session transcripts (main + subagents) and reassembles them —
  values are never re-typed. Page bounds for cropping come from the `pageBounds` results.

`--changes` makes the two REST list calls (component sets, components), stores the lists in `raw/` and
prints every published set or component that is NEW, REMOVED or CHANGED against the stored lists, so an
MCP refresh re-dumps only those. A `--from-transcripts` import that does not re-dump every listed node is
a partial refresh: `snapshot.json` keeps its `extracted` date and records `updated` plus the `refreshed`
node ids, and page bounds are merged, not replaced.

**A refresh re-extracts only what changed.** `--plan` prints STEP 1: one `fingerprints` call per file.
After those are imported, `--plan` prints STEP 2: the dump, frame, flow, list and style calls and the page
renders for exactly the keys whose hash differs from `raw/hashes.json`, batched, with a call estimate (also
written to `raw/refresh_plan.json`). `--accept-fingerprints` takes the imported fingerprints as the
baseline for everything, once, right after a complete extraction.

Values are transcribed mechanically from node JSON; the design tool's generated code flattens gradient
strokes to their first stop, so it is not used. Pillow is needed only to crop renders (PEP 723 dependency);
without it the crops are skipped and said so. TLS uses Python's default context (set SSL_CERT_FILE on a
host with a TLS-inspecting proxy).

Exit 0 ok; 3 SKIPPED (no library dir or manifest); a fatal input or quota error exits with its message.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _transcripts  # noqa: E402
import wfconfig  # noqa: E402


def _cfg_path(key: str) -> Path | None:
    raw = str(wfconfig.get(key, "") or "")
    return wfconfig.repo_root() / raw if raw else None


OUT = _cfg_path("design.library_dir")
MANIFEST = _cfg_path("design.manifest") or Path("/nonexistent-design-manifest.json")
FIGMA_API = "https://api.figma.com"
PRECEDENCE = str(wfconfig.get("design.precedence", "") or
                 "the design owner's ticket answer > the master component > this frame > the intake extract > the specification page")
NODE_BATCH = 12
CROP_MARGIN = 16


def log(msg: str) -> None:
    print(msg, flush=True)


def read_env_file(key: str) -> str | None:
    env_path = wfconfig.repo_root() / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s or "unnamed"


def raw_name(node_id: str) -> str:
    return node_id.replace(":", "-") + ".json"


# ---------------------------------------------------------------- REST source

def rest_getter():
    import os
    import urllib.error
    import urllib.parse
    import urllib.request

    env = str(wfconfig.get("design.token_env", "FIGMA_ACCESS_TOKEN") or "FIGMA_ACCESS_TOKEN")
    token = os.environ.get(env) or read_env_file(env)
    if not token:
        raise SystemExit(f"{env} is not set (environment or the repo's .env); use --from-transcripts instead")

    def get(path: str, params: dict | None = None) -> dict:
        url = f"{FIGMA_API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        for _ in range(6):
            req = urllib.request.Request(url, headers={"X-Figma-Token": token})
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise SystemExit(f"design source REST {path}: HTTP {exc.code}") from None
                wait = int(exc.headers.get("Retry-After", "20"))
                if wait > 300:
                    # A per-seat monthly quota answers with a multi-day Retry-After.
                    raise SystemExit(f"design source quota exhausted on {path}: Retry-After {wait}s "
                                     f"(~{wait / 86400:.1f} days). Use a token with quota, or --from-transcripts.")
                log(f"  429, sleeping {wait}s")
                time.sleep(wait)
        raise SystemExit(f"rate-limited 6 times on {path}")

    return get


def rest_changes() -> None:
    get = rest_getter()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ds_key = manifest["design_source"]["library_file_key"]
    raw = OUT / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    def stored(name: str) -> list:
        p = raw / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    old_sets, old_comps = stored("component_sets.json"), stored("components.json")
    sets_meta = get(f"/v1/files/{ds_key}/component_sets")["meta"]["component_sets"]
    comps_meta = get(f"/v1/files/{ds_key}/components")["meta"]["components"]

    def diff(kind: str, old: list, new: list) -> None:
        o = {x["node_id"]: x for x in old}
        n = {x["node_id"]: x for x in new}

        def label(x: dict) -> str:
            parent = ((x.get("containing_frame") or {}).get("containingComponentSet") or {}).get("name")
            return f"{x['name']}" + (f" (in {parent})" if parent else "")

        for nid in sorted(n.keys() - o.keys()):
            log(f"NEW      {kind} {nid} {label(n[nid])} updated {n[nid].get('updated_at')}")
        for nid in sorted(o.keys() - n.keys()):
            log(f"REMOVED  {kind} {nid} {label(o[nid])}")
        for nid in sorted(n.keys() & o.keys()):
            if n[nid].get("updated_at") != o[nid].get("updated_at"):
                log(f"CHANGED  {kind} {nid} {label(n[nid])} {o[nid].get('updated_at')} -> {n[nid].get('updated_at')}")

    diff("set", old_sets, sets_meta)
    diff("component", old_comps, comps_meta)
    (raw / "component_sets.json").write_text(json.dumps(sets_meta), encoding="utf-8")
    (raw / "components.json").write_text(json.dumps(comps_meta), encoding="utf-8")
    log(f"{len(sets_meta)} published sets, {len(comps_meta)} published components; lists stored in raw/")


def rest_fetch(skip_frames: bool) -> None:
    get = rest_getter()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ds_key = manifest["design_source"]["library_file_key"]
    ws_key = manifest["design_source"]["screens_file_key"]
    raw = OUT / "raw"
    (raw / "frames").mkdir(parents=True, exist_ok=True)
    sets_meta = get(f"/v1/files/{ds_key}/component_sets")["meta"]["component_sets"]
    (raw / "snapshot.json").write_text(json.dumps(
        {"extracted": time.strftime("%Y-%m-%d"), "source": "design source REST API"}), encoding="utf-8")
    comps_meta = get(f"/v1/files/{ds_key}/components")["meta"]["components"]
    (raw / "component_sets.json").write_text(json.dumps(sets_meta), encoding="utf-8")
    (raw / "components.json").write_text(json.dumps(comps_meta), encoding="utf-8")
    standalone = [c for c in comps_meta if not (c.get("containing_frame") or {}).get("containingComponentSet")]
    ids = [s["node_id"] for s in sets_meta] + [c["node_id"] for c in standalone]
    todo = [i for i in ids if not (raw / raw_name(i)).exists()]
    for i in range(0, len(todo), NODE_BATCH):
        batch = todo[i:i + NODE_BATCH]
        log(f"  nodes {i + 1}-{i + len(batch)} of {len(todo)}")
        data = get(f"/v1/files/{ds_key}/nodes", {"ids": ",".join(batch)})
        for nid, payload in data["nodes"].items():
            if payload:
                (raw / raw_name(nid)).write_text(json.dumps(payload), encoding="utf-8")
    if not skip_frames:
        for f in manifest["design_source"]["frames"]:
            data = get(f"/v1/files/{ws_key}/nodes", {"ids": f["node"]})
            payload = data["nodes"].get(f["node"])
            if payload:
                (raw / "frames" / f"{f['slug']}.rest.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------- MCP source (transcripts)

def hex_to_color(s: str) -> dict:
    h, _, a = s.partition("@")
    return {"r": int(h[1:3], 16) / 255, "g": int(h[3:5], 16) / 255, "b": int(h[5:7], 16) / 255,
            "a": float(a) if a else 1.0}


def compact_paint(c) -> dict:
    if isinstance(c, str):
        if c.startswith("S#"):
            col = hex_to_color(c[1:])
            return {"type": "SOLID", "color": {**col, "a": 1.0}, "opacity": col["a"]}
        if c.startswith("I"):
            return {"type": "IMAGE", "scaleMode": c[1:]}
        return {"type": c}
    _, kind, stops, handles, op = c
    return {"type": f"GRADIENT_{kind}", "opacity": op,
            "gradientStops": [{"color": hex_to_color(h), "position": pos} for h, pos in stops],
            "gradientHandlePositions": ([{"x": handles[0], "y": handles[1]}, {"x": handles[2], "y": handles[3]}]
                                        if handles else [])}


def compact_node(c: dict, style_ids: dict) -> dict:
    """Expand one node of design_extract.js's compact format into the REST node shape."""
    n = {"id": c["i"], "name": c["n"], "type": c["t"], "visible": c.get("v", 1) != 0}
    if "o" in c:
        n["opacity"] = c["o"]
    if "w" in c:
        n["absoluteBoundingBox"] = {"x": c.get("x", 0), "y": c.get("y", 0), "width": c["w"], "height": c["h"]}
    if "L" in c:
        mode, gap, pt, pr, pb, pl, main, cross = c["L"]
        n.update({"layoutMode": mode, "itemSpacing": gap, "paddingTop": pt, "paddingRight": pr,
                  "paddingBottom": pb, "paddingLeft": pl, "primaryAxisAlignItems": main,
                  "counterAxisAlignItems": cross})
    if isinstance(c.get("f"), list):
        n["fills"] = [compact_paint(p) for p in c["f"]]
    if "s" in c:
        n["strokes"] = [compact_paint(p) for p in c["s"]]
        sw = c.get("sw")
        if isinstance(sw, list):
            n["individualStrokeWeights"] = dict(zip(("top", "right", "bottom", "left"), sw))
        else:
            n["strokeWeight"] = sw
        n["strokeAlign"] = c.get("sa", "")
        if c.get("sd"):
            n["strokeDashes"] = c["sd"]
    if "r" in c:
        if isinstance(c["r"], list):
            n["rectangleCornerRadii"] = c["r"]
        else:
            n["cornerRadius"] = c["r"]
    if "e" in c:
        eff = []
        for e in c["e"]:
            if len(e) == 6:
                eff.append({"type": e[0], "color": hex_to_color(e[1]), "offset": {"x": e[2], "y": e[3]},
                            "radius": e[4], "spread": e[5]})
            else:
                eff.append({"type": e[0], "radius": e[1]})
        n["effects"] = eff
    if "st" in c:
        n["styles"] = {}
        for k, name in c["st"].items():
            sid = f"name:{name}"
            style_ids[sid] = {"name": name}
            n["styles"][k] = sid
    if "T" in c:
        chars, fam, fstyle, weight, size, lh, ls, case, align, segs = c["T"]
        n["characters"] = chars
        n["style"] = {"fontFamily": fam, "fontStyle": fstyle, "fontWeight": weight, "fontSize": size,
                      "lineHeightPx": lh, "letterSpacing": ls, "textAlignHorizontal": align}
        if case:
            n["style"]["textCase"] = case
        if segs and segs > 1:
            n["style"]["mixedSegments"] = segs
    if "I" in c:
        mid, key, name, set_name, remote = c["I"]
        n["componentId"] = mid
        n["_main"] = {"id": mid, "key": key, "name": name, "set": set_name, "remote": bool(remote)}
    if "R" in c:
        # MCP style runs: [start, end, fills, family, style, weight, size, decoration]; only runs that
        # differ from the base (first) run are kept, matching what REST overrides express.
        base = c["R"][0] if c["R"] else None
        runs = []
        for r in c["R"][1:] if base else []:
            start, end, fills, fam, fstyle, weight, size, deco = r
            o = {}
            if fills != base[2]:
                o["fills"] = [compact_paint(x) for x in fills]
            if fam != base[3]:
                o["fontFamily"] = fam
            if fstyle != base[4]:
                o["fontStyle"] = fstyle
            if weight != base[5]:
                o["fontWeight"] = weight
            if size != base[6]:
                o["fontSize"] = size
            if deco != base[7]:
                o["textDecoration"] = deco
            if o:
                runs.append((c["T"][0][start:end], o))
        if runs:
            n["_runs"] = runs
    if "rx" in c:
        n["interactions"] = [
            {"trigger": {"type": t, "delay": d}, "actions": [
                {"type": "NODE", "destinationId": dest, "navigation": nav,
                 "transition": ({"type": tt, "easing": {"type": te}, "duration": td} if tt else None)}
                for dest, nav, tt, te, td in acts]}
            for t, d, acts in c["rx"]]
    if "ov" in c:
        n["overrides"] = [{"overriddenFields": c["ov"]}]
    if "oc" in c:
        # Shallow frame dump: snapshots of the descendants this screen overrides on the instance.
        n["_oc"] = [(x.get("of", []), compact_node(x, style_ids)) for x in c["oc"]]
    if "c" in c:
        n["children"] = [compact_node(ch, style_ids) for ch in c["c"]]
    return n


def transcript_files(session: str | None) -> list[Path]:
    files: list[Path] = []
    for root in (Path(p) for p in _transcripts.project_dirs()):
        if session:
            main = root / f"{session}.jsonl"
            if main.exists():
                files.append(main)
            sub = root / session / "subagents"
            if sub.exists():
                files += sorted(sub.glob("**/*.jsonl"))
        else:
            files += sorted(root.glob("*.jsonl")) + sorted(root.glob("*/subagents/**/*.jsonl"))
    return files


def tool_texts(path: Path):
    for line in path.open(encoding="utf-8", errors="replace"):
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
                yield d.get("timestamp") or "", t


def file_role(manifest: dict, key: str | None) -> str | None:
    if key == manifest["design_source"]["library_file_key"]:
        return "ds"
    if key == manifest["design_source"]["screens_file_key"]:
        return "ws"
    return None


def extraction_ts(seen: dict, key: str) -> str | None:
    """When the values a fingerprint key covers were last extracted in this import (a frame needs both dumps)."""
    role, kind, *rest = key.split(":", 2)
    if kind == "node":
        if role == "ds":
            return seen.get(f"ds:dump:{rest[0]}")
        a, b = seen.get(f"ws:frames:{rest[0]}"), seen.get(f"ws:shallow:{rest[0]}")
        return min(a, b) if a and b else None
    return seen.get(key) if kind in ("list", "top", "flow", "styles") else None


def update_baseline(raw: Path, seen: dict, accept: bool) -> None:
    """raw/hashes.json holds, per fingerprint key, the hash of the values in raw/ and when they were known current.

    A key takes the latest fingerprint's hash when its values were extracted at or after that fingerprint, or
    (``accept``) unconditionally — only right after a complete extraction. An unchanged hash only moves ``at``.
    A changed hash with no newer extraction is left alone, so ``--plan`` keeps listing it.
    """
    fps = json.loads((raw / "fingerprints.json").read_text(encoding="utf-8"))
    hp = raw / "hashes.json"
    base = json.loads(hp.read_text(encoding="utf-8")) if hp.exists() else {}
    for key, f in fps.items():
        if key.startswith("_") or ":bounds:" in key:
            continue
        ext = extraction_ts(seen, key)
        b = base.get(key)
        if accept:
            base[key] = {"hash": f["hash"], "page": f.get("page"), "at": f["ts"],
                         "dumped": ext or (b or {}).get("dumped") or f["ts"]}
        elif ext and ext >= f["ts"]:
            base[key] = {"hash": f["hash"], "page": f.get("page"), "at": ext, "dumped": ext}
        elif b and b["hash"] == f["hash"]:
            b["at"] = max(b["at"], f["ts"])
    hp.write_text(json.dumps(base, indent=1, sort_keys=True), encoding="utf-8")


def import_transcripts(session: str | None, accept_fingerprints: bool = False) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    parts: dict[tuple[str, str], dict] = {}
    ds_bounds: dict = {}
    ws_bounds: dict = {}
    bound_results: list[tuple[str, dict, dict]] = []
    for path in transcript_files(session):
        for ts, t in tool_texts(path):
            if t.startswith("@@ML|"):
                head, _, body = t.partition("\n")
                _, kind, tag, part, nparts, total = head.split("|")
                slot = parts.setdefault((kind, tag), {"parts": int(nparts), "total": int(total), "got": {}, "ts": ""})
                slot["got"][int(part)] = body
                slot["ts"] = max(slot["ts"], ts)
                continue
            if '"renderUnion"' in t and t.lstrip().startswith("{"):
                try:
                    obj = json.loads(t)
                except json.JSONDecodeError:
                    continue
                if "sizes" in obj:
                    bound_results.append((ts, ds_bounds, obj.get("pages") or {}))
                elif "bounds" in obj:
                    bound_results.append((ts, ws_bounds, obj.get("bounds") or {}))
    # Oldest first, so a page measured twice keeps its latest bounds.
    for _, target, pages in sorted(bound_results, key=lambda r: r[0]):
        target.update(pages)
    raw = OUT / "raw"
    (raw / "frames").mkdir(parents=True, exist_ok=True)
    complete, incomplete = 0, []
    lists: dict = {}
    refreshed: set[str] = set()
    seen: dict[str, str] = {}
    fps: dict = {}
    # Oldest first, so a node dumped twice keeps its latest values.
    for (kind, tag), slot in sorted(parts.items(), key=lambda kv: (kv[1]["ts"], kv[0])):
        if sorted(slot["got"]) != list(range(slot["parts"])):
            incomplete.append(f"{kind}/{tag}: have {sorted(slot['got'])} of {slot['parts']}")
            continue
        text = "".join(slot["got"][i] for i in range(slot["parts"]))
        if len(text.encode("utf-16-le")) // 2 != slot["total"]:  # JS length counts UTF-16 code units
            incomplete.append(f"{kind}/{tag}: {len(text)} chars, header says {slot['total']}")
            continue
        data = json.loads(text)
        complete += 1
        role, ts = file_role(manifest, data.get("file")), slot["ts"]

        def mark(key: str) -> None:
            seen[key] = max(seen.get(key, ""), ts)

        if kind == "dump":
            for nid, payload in data["nodes"].items():
                style_ids: dict = {}
                doc = compact_node(payload["document"], style_ids)
                out = {"document": doc, "styles": style_ids, "source": "mcp", "tag": tag}
                (raw / raw_name(nid)).write_text(json.dumps(out), encoding="utf-8")
                refreshed.add(nid)
                mark(f"{role}:dump:{nid}")
        elif kind == "list":
            lists[data["page"][0]] = {"name": data["page"][1], "rows": data["rows"]}
            mark(f"{role}:list:{data['page'][0]}")
        elif kind == "top":
            tp = raw / "ws_toplevel.json"
            top = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {}
            top.update(data["pages"])
            tp.write_text(json.dumps(top), encoding="utf-8")
            for pid in data["pages"]:
                mark(f"{role}:top:{pid}")
        elif kind == "flow":
            (raw / "flows").mkdir(exist_ok=True)
            (raw / "flows" / f"{data['page'][0].replace(':', '-')}.json").write_text(json.dumps(data), encoding="utf-8")
            mark(f"{role}:flow:{data['page'][0]}")
        elif kind == "styles":
            (raw / "styles.json").write_text(json.dumps(data), encoding="utf-8")
            mark(f"{role}:styles")
        elif kind == "shallow":
            for nid, payload in data["nodes"].items():
                style_ids = {}
                doc = compact_node(payload["document"], style_ids)
                (raw / "frames" / f"{nid.replace(':', '-')}.shallow.json").write_text(
                    json.dumps({"document": doc, "styles": style_ids, "tag": tag}), encoding="utf-8")
                mark(f"{role}:shallow:{nid}")
        elif kind == "frames":
            for fid, fr in data["frames"].items():
                if fr:
                    (raw / "frames" / f"{fid.replace(':', '-')}.mcp.json").write_text(json.dumps(fr), encoding="utf-8")
                    mark(f"{role}:frames:{fid}")
        elif kind == "fp":
            for nid, v in data["nodes"].items():
                if v:
                    fps[f"{role}:node:{nid}"] = {"hash": v[0], "page": v[1], "ts": ts}
            for pid, pv in data["pages"].items():
                if not pv:
                    continue
                for part_name in ("list", "top", "flow"):
                    if part_name in pv:
                        fps[f"{role}:{part_name}:{pid}"] = {"hash": pv[part_name], "ts": ts}
                fps[f"{role}:bounds:{pid}"] = {"renderUnion": pv["renderUnion"], "ts": ts}
                fps.setdefault("_pages", {})[f"{role}:{pid}"] = ts
            if data.get("styles"):
                fps[f"{role}:styles"] = {"hash": data["styles"], "ts": ts}
    if fps or accept_fingerprints:
        fp_path = raw / "fingerprints.json"
        merged_fp = json.loads(fp_path.read_text(encoding="utf-8")) if fp_path.exists() else {}
        pages_ts = {**merged_fp.pop("_pages", {}), **fps.pop("_pages", {})}
        for key, f in fps.items():
            if f["ts"] >= merged_fp.get(key, {}).get("ts", ""):
                merged_fp[key] = f
        merged_fp["_pages"] = pages_ts
        fp_path.write_text(json.dumps(merged_fp, indent=1, sort_keys=True), encoding="utf-8")
        update_baseline(raw, seen, accept_fingerprints)
    # Merged, so a partial refresh keeps the bounds of the pages it did not re-measure.
    pb_path = raw / "page_bounds.json"
    bounds = json.loads(pb_path.read_text(encoding="utf-8")) if pb_path.exists() else {"ds": {}, "ws": {}}
    bounds["ds"].update(ds_bounds)
    bounds["ws"].update(ws_bounds)
    pb_path.write_text(json.dumps(bounds), encoding="utf-8")
    if lists:
        allc = raw / "all_components.json"
        merged = json.loads(allc.read_text(encoding="utf-8")) if allc.exists() else {"pages": {}}
        merged["pages"].update(lists)
        allc.write_text(json.dumps(merged), encoding="utf-8")
    if refreshed:
        source = "Figma MCP (use_figma + get_screenshot)"
        today = time.strftime("%Y-%m-%d")
        snap = raw / "snapshot.json"
        prior = json.loads(snap.read_text(encoding="utf-8")) if snap.exists() else {}
        listed = {x["node_id"] for name in ("component_sets.json", "components.json") if (raw / name).exists()
                  for x in json.loads((raw / name).read_text(encoding="utf-8"))
                  if name == "component_sets.json"
                  or not (x.get("containing_frame") or {}).get("containingComponentSet")}
        if prior.get("extracted") and not listed <= refreshed:
            # A partial refresh: the base snapshot keeps its date; the re-dumped nodes are recorded.
            prior.update({"updated": today, "updated_source": source,
                          "refreshed": sorted(set(prior.get("refreshed", [])) | refreshed)})
            snap.write_text(json.dumps(prior), encoding="utf-8")
        else:
            snap.write_text(json.dumps({"extracted": today, "source": source}), encoding="utf-8")
    log(f"transcripts: {complete} complete chunk sets; {len(incomplete)} incomplete")
    for x in incomplete:
        log(f"  INCOMPLETE {x}")
    return {"incomplete": incomplete}


# ---------------------------------------------------------------- value transcription

def num(v) -> str:
    """One number format for both sources: 2 decimals max, no trailing zeros, no negative zero."""
    if not isinstance(v, (int, float)):
        return str(v)
    r = round(float(v), 2) + 0.0
    return f"{r:g}"


def hex_color(c: dict, opacity: float = 1.0) -> str:
    a = c.get("a", 1.0) * opacity
    h = "#%02X%02X%02X" % (round(c["r"] * 255), round(c["g"] * 255), round(c["b"] * 255))
    return h if a >= 0.995 else f"{h}@{a:.2f}"


def fmt_paint(p: dict) -> str | None:
    if not p.get("visible", True):
        return None
    op = p.get("opacity", 1.0)
    t = p["type"]
    if t == "SOLID":
        return hex_color(p["color"], op)
    if t.startswith("GRADIENT"):
        stops = ", ".join(f"{hex_color(s['color'])} {s['position']:.2f}" for s in p.get("gradientStops", []))
        h = p.get("gradientHandlePositions") or []
        where = f" from ({num(h[0]['x'])},{num(h[0]['y'])}) to ({num(h[1]['x'])},{num(h[1]['y'])})" if len(h) >= 2 else ""
        extra = f" opacity {op:.2f}" if op < 0.995 else ""
        return f"{t.replace('GRADIENT_', '').lower()}-gradient[{stops}]{where}{extra}"
    if t == "IMAGE":
        return f"image({p.get('scaleMode', '')})"
    return t.lower()


def fmt_effect(e: dict) -> str | None:
    if not e.get("visible", True):
        return None
    t = e["type"]
    if t in ("DROP_SHADOW", "INNER_SHADOW"):
        o = e.get("offset", {})
        return (f"{'inset ' if t == 'INNER_SHADOW' else ''}shadow {o.get('x', 0):g}px {o.get('y', 0):g}px "
                f"{e.get('radius', 0):g}px {e.get('spread', 0) or 0:g}px {hex_color(e['color'])}")
    # Figma blur radius is twice the CSS blur() value (CSS takes the Gaussian's standard deviation).
    r = e.get("radius", 0) or 0
    return f"{t.lower()} {r:g} (Figma radius) = CSS blur({num(r / 2)}px)"


NODE_NAMES: dict[str, str] = {}  # node id -> name, filled per payload so interactions can name their target
# component key -> design library node id. A screens file instance of a library component reports the id of
# the file's local copy of that component; the key is what identifies it in the library.
LIB_BY_KEY: dict[str, str] = {}


def index_names(n: dict) -> None:
    NODE_NAMES[n["id"]] = n.get("name", "")
    for ch in n.get("children", []):
        index_names(ch)


def run_props(o: dict) -> str:
    parts = []
    fills = [x for x in (fmt_paint(p) for p in o.get("fills", [])) if x]
    if fills:
        parts.append(" + ".join(fills))
    if o.get("fontFamily"):
        parts.append(o["fontFamily"])
    if o.get("fontWeight"):
        parts.append(num(o["fontWeight"]))
    if o.get("fontSize"):
        parts.append(num(o["fontSize"]))
    if o.get("italic") or "italic" in str(o.get("fontStyle", "")).lower():
        parts.append("italic")
    if o.get("textDecoration") and o["textDecoration"] != "NONE":
        parts.append(o["textDecoration"].lower())
    return " ".join(parts) or "(same as base)"


def text_runs(n: dict) -> list[str]:
    """Style runs that differ from the node's base style: REST characterStyleOverrides +
    styleOverrideTable, or the MCP converter's _runs."""
    chars = n.get("characters") or ""
    if n.get("_runs"):
        return [f'"{t[:60]}" = {run_props(o)}'.replace("\n", "⏎") for t, o in n["_runs"]]
    cso = n.get("characterStyleOverrides") or []
    table = n.get("styleOverrideTable") or {}
    out, i = [], 0
    while i < len(cso):
        sid, j = cso[i], i
        while j < len(cso) and cso[j] == sid:
            j += 1
        if sid and str(sid) in table:
            out.append(f'"{chars[i:j][:60]}" = {run_props(table[str(sid)])}'.replace("\n", "⏎"))
        i = j
    return out


def fmt_interactions(n: dict) -> str | None:
    items = []
    for it in n.get("interactions") or []:
        trig = ((it.get("trigger") or {}).get("type") or "?").lower()
        if (it.get("trigger") or {}).get("delay"):
            trig += f" after {num(it['trigger']['delay'] * 1000)}ms"
        for a in it.get("actions") or []:
            if not a:
                continue
            dest = a.get("destinationId")
            what = (a.get("navigation") or a.get("type") or "").lower()
            if dest:
                what += f" `{dest}`" + (f" ({NODE_NAMES[dest]})" if dest in NODE_NAMES else "")
            tr = a.get("transition") or {}
            if tr:
                what += f" {str(tr.get('type', '')).lower()}"
                if (tr.get("easing") or {}).get("type"):
                    what += f" {tr['easing']['type'].lower()}"
                if tr.get("duration") is not None:
                    what += f" {num(tr['duration'] * 1000)}ms"
            items.append(f"on {trig} → {what}")
    return "interactions " + "; ".join(items) if items else None


def node_props(n: dict, styles: dict, comps: dict) -> list[str]:
    out: list[str] = []
    bb = n.get("absoluteBoundingBox") or {}
    if bb:
        out.append(f"{num(bb.get('width', 0))}×{num(bb.get('height', 0))}")
    if n.get("visible", True) is False:
        out.append("HIDDEN")
    if n.get("opacity", 1.0) < 0.995:
        out.append(f"opacity {n['opacity']:.2f}")
    if n.get("layoutMode") and n["layoutMode"] != "NONE":
        pads = [n.get(k, 0) or 0 for k in ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft")]
        lay = f"{n['layoutMode'].lower()} gap {num(n.get('itemSpacing', 0) or 0)} pad {'/'.join(num(p) for p in pads)}"
        # MIN is the default alignment; REST omits it, so both sources print only non-default values.
        if n.get("primaryAxisAlignItems") not in (None, "MIN"):
            lay += f" main {n['primaryAxisAlignItems'].lower()}"
        if n.get("counterAxisAlignItems") not in (None, "MIN"):
            lay += f" cross {n['counterAxisAlignItems'].lower()}"
        out.append(lay)
    fills = [x for x in (fmt_paint(p) for p in n.get("fills", [])) if x]
    if fills:
        out.append("fill " + " + ".join(fills))
    strokes = [x for x in (fmt_paint(p) for p in n.get("strokes", [])) if x]
    if strokes:
        w = n.get("individualStrokeWeights")
        wtxt = f"t{w['top']:g} r{w['right']:g} b{w['bottom']:g} l{w['left']:g}" if w else f"{n.get('strokeWeight') or 0:g}px"
        dash = " dashed " + "/".join(f"{d:g}" for d in n["strokeDashes"]) if n.get("strokeDashes") else ""
        out.append(f"stroke {' + '.join(strokes)} {wtxt} {str(n.get('strokeAlign', '')).lower()}{dash}".rstrip())
    if n.get("rectangleCornerRadii"):
        out.append("radius " + "/".join(f"{r:g}" for r in n["rectangleCornerRadii"]))
    elif n.get("cornerRadius"):
        out.append(f"radius {n['cornerRadius']:g}")
    effects = [x for x in (fmt_effect(e) for e in n.get("effects", [])) if x]
    if effects:
        out.append("effects " + "; ".join(effects))
    for kind, sid in (n.get("styles") or {}).items():
        name = (styles.get(sid) or {}).get("name")
        if name:
            out.append(f"style.{kind}={name}")
    if n["type"] == "TEXT":
        st = n.get("style", {})
        lh = st.get("lineHeightPx")
        font = f"{st.get('fontFamily', '?')} {num(st.get('fontWeight', '?'))} {num(st.get('fontSize', '?'))}"
        if lh:
            font += f"/{num(lh)}"
        if st.get("letterSpacing"):
            font += f" tracking {num(st['letterSpacing'])}"
        if st.get("textCase"):
            font += f" {st['textCase'].lower()}"
        if st.get("textAlignHorizontal") and st["textAlignHorizontal"] != "LEFT":
            font += f" align {st['textAlignHorizontal'].lower()}"
        if st.get("italic") or "italic" in str(st.get("fontStyle", "")).lower():
            font += " italic"
        runs = text_runs(n)
        if st.get("mixedSegments") and not runs:
            font += f" (first of {st['mixedSegments']} style runs; runs not captured)"
        out.append(font)
        chars = (n.get("characters") or "").replace("\n", "⏎")
        out.append(f'"{chars[:160]}{"…" if len(chars) > 160 else ""}"')
        if runs:
            out.append("runs " + " | ".join(runs))
    rx = fmt_interactions(n)
    if rx:
        out.append(rx)
    if n["type"] == "INSTANCE":
        m = n.get("_main") or {}
        cid = n.get("componentId", "")
        name = m.get("name") or (comps.get(cid) or {}).get("name", "")
        set_name = m.get("set") or ""
        lib = LIB_BY_KEY.get(m.get("key", "")) if m.get("remote") else None
        ref = f"`{lib}` (local copy `{cid}`)" if lib and lib != cid else f"`{cid}`"
        out.append(f"instance-of {set_name + ' → ' if set_name else ''}{name} {ref}".strip())
        ov = sorted({f for o in n.get("overrides", []) for f in o.get("overriddenFields", [])})
        if ov:
            out.append("overrides " + ",".join(ov))
    return out


def outline(n: dict, styles: dict, comps: dict, depth: int = 0, lines: list[str] | None = None) -> list[str]:
    lines = [] if lines is None else lines
    lines.append(f"{'  ' * depth}- `{n['id']}` {n['type'].lower()} **{n['name']}** — {'; '.join(node_props(n, styles, comps))}")
    for fields, snap in n.get("_oc", []):
        lines.append(f"{'  ' * (depth + 1)}- override `{snap['id']}` {snap['type'].lower()} **{snap['name']}** "
                     f"[{','.join(fields)}] — {'; '.join(node_props(snap, styles, comps))}")
    for ch in n.get("children", []):
        outline(ch, styles, comps, depth + 1, lines)
    return lines


def flat_props(n: dict, styles: dict, comps: dict, path: str = "", acc: dict | None = None) -> dict:
    acc = {} if acc is None else acc
    key = f"{path}/{n['name']}" if path else n["name"]
    k, i = key, 2
    while k in acc:
        k, i = f"{key}#{i}", i + 1
    acc[k] = (n["id"], node_props(n, styles, comps))
    for ch in n.get("children", []):
        flat_props(ch, styles, comps, k, acc)
    return acc


def parse_variant(name: str) -> dict:
    out = {}
    for part in name.split(","):
        if "=" in part:
            a, b = part.split("=", 1)
            out[a.strip()] = b.strip()
    return out


STATE_KEYS = ("State", "Status", "Function", "Property 1")


def state_diffs(variants: list[dict], styles: dict, comps: dict) -> list[str]:
    """Diff each non-default variant against its sibling with the same other properties, preferring
    the sibling whose state is Default."""
    lines: list[str] = []
    parsed = [(v, parse_variant(v["name"])) for v in variants]
    for v, props in parsed:
        skey = next((k for k in STATE_KEYS if k in props), None)
        if skey is None or props[skey] == "Default":
            continue
        others = {k: x for k, x in props.items() if k != skey}
        siblings = [(b, bp) for b, bp in parsed if b is not v and {k: x for k, x in bp.items() if k != skey} == others]
        base = next((b for b, bp in siblings if bp.get(skey) == "Default"), None)
        if base is None and siblings:
            base = siblings[0][0]
        if base is None:
            base = parsed[0][0] if parsed[0][0] is not v else None
        if base is None:
            continue
        ra = {k.split("/", 1)[1] if "/" in k else "": val for k, val in flat_props(base, styles, comps).items()}
        rb = {k.split("/", 1)[1] if "/" in k else "": val for k, val in flat_props(v, styles, comps).items()}
        changed = [(k or "(root)", ra.get(k, (None, None))[1], rb.get(k, (None, None))[1])
                   for k in sorted(set(ra) | set(rb)) if ra.get(k, (None, None))[1] != rb.get(k, (None, None))[1]]
        lines.append(f"\n### `{v['name']}` vs `{base['name']}` — {len(changed)} node(s) differ\n")
        for k, pa, pb in changed[:60]:
            lines.append(f"- **{k}**")
            lines.append(f"  - base: {'; '.join(pa) if pa else '(absent)'}")
            lines.append(f"  - this: {'; '.join(pb) if pb else '(absent)'}")
        if len(changed) > 60:
            lines.append(f"- … {len(changed) - 60} more differing nodes (see raw JSON)")
    return lines


# ---------------------------------------------------------------- build

def build(manifest: dict) -> None:
    try:
        from PIL import Image
    except ImportError:  # crops need Pillow; the sheets and maps do not
        Image = None
        log("renders: SKIPPED — Pillow is not installed (run through `uv run`, which installs it from the "
            "script header), so no set, variant or screen crop is written")

    raw = OUT / "raw"
    sets_meta = json.loads((raw / "component_sets.json").read_text(encoding="utf-8"))
    snap = raw / "snapshot.json"
    snapshot = json.loads(snap.read_text(encoding="utf-8")) if snap.exists() else {}
    comps_meta = json.loads((raw / "components.json").read_text(encoding="utf-8"))
    ds_key = manifest["design_source"]["library_file_key"]
    ws_key = manifest["design_source"]["screens_file_key"]
    by_key = {c["key"]: c for c in comps_meta}
    standalone = [c for c in comps_meta if not (c.get("containing_frame") or {}).get("containingComponentSet")]
    bounds = json.loads((raw / "page_bounds.json").read_text(encoding="utf-8")) if (raw / "page_bounds.json").exists() else {"ds": {}, "ws": {}}
    # The REST lists hold PUBLISHED components only. all_components.json (MCP listComponents) holds every
    # component on every page; the rest are added here and marked unpublished.
    allc = raw / "all_components.json"
    if allc.exists():
        known = {x["node_id"] for x in sets_meta} | {x["node_id"] for x in comps_meta}
        for pid, pg in json.loads(allc.read_text(encoding="utf-8"))["pages"].items():
            for nid, typ, name, parent, key in pg["rows"]:
                if nid in known or (typ == "COMPONENT" and parent):
                    continue
                meta = {"node_id": nid, "name": name, "key": key, "unpublished": True,
                        "containing_frame": {"pageId": pid, "pageName": pg["name"]}}
                (sets_meta if typ == "COMPONENT_SET" else standalone).append(meta)
                known.add(nid)
    (OUT / "sets").mkdir(exist_ok=True)
    # Names like "Icon", "Icon +" and "Icon - ->" collapse to one slug; suffix the node id on a clash.
    base_slugs = [(x["node_id"], slug(x["name"])) for x in sets_meta + standalone]
    clash = {sl for _, sl in base_slugs if sum(1 for _, o in base_slugs if o == sl) > 1}
    slugs = {nid: f"{sl}-{nid.replace(':', '-')}" if sl in clash else sl for nid, sl in base_slugs}
    (OUT / "frame-map").mkdir(exist_ok=True)

    page_imgs: dict = {}

    def page_image(prefix: str, page_id: str):
        key = f"{prefix}-{page_id.replace(':', '-')}"
        if key not in page_imgs:
            hits = sorted((OUT / "renders" / "_pages").glob(f"{key}*.png"))
            page_imgs[key] = Image.open(hits[0]) if hits and Image is not None else None
        return page_imgs[key]

    def crop(prefix: str, page_id: str, page_bounds: dict, box: dict, dest: Path, margin: int) -> str | None:
        img = page_image(prefix, page_id)
        pb = (page_bounds or {}).get(page_id)
        if img is None or not pb or not box:
            return None
        x0, y0, x1, y1 = pb["renderUnion"]
        s = img.width / (x1 - x0)
        left = (box["x"] - x0) * s - margin
        top = (box["y"] - y0) * s - margin
        right = (box["x"] + box["width"] - x0) * s + margin
        bottom = (box["y"] + box["height"] - y0) * s + margin
        c = img.crop((max(0, round(left)), max(0, round(top)), min(img.width, round(right)), min(img.height, round(bottom))))
        dest.parent.mkdir(parents=True, exist_ok=True)
        c.save(dest)
        return dest.relative_to(OUT).as_posix()

    def load(nid: str) -> dict | None:
        p = raw / raw_name(nid)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    index_rows, missing, stale = [], [], []
    n_variants = 0

    hp = raw / "hashes.json"
    hashes = json.loads(hp.read_text(encoding="utf-8")) if hp.exists() else {}

    def freshness(meta: dict, nid: str) -> str:
        checked = (hashes.get(f"ds:node:{nid}") or {}).get("at")
        if checked:
            # A fingerprint matched these values at `checked`, so a republish with no value change is not stale.
            if (meta.get("updated_at") or "") > checked:
                stale.append(f"{meta['name']} `{nid}`")
                return (f"updated in Figma {meta.get('updated_at')} · values checked {checked[:10]} · "
                        "**⚠️ STALE: Figma changed this after the values below were checked; re-extract before relying on them**")
            return f"updated in Figma {meta.get('updated_at', '?')} · values checked {checked[:10]}"
        extracted = snapshot.get("updated") if nid in snapshot.get("refreshed", []) else snapshot.get("extracted")
        changed = (meta.get("updated_at") or "")[:10]
        if extracted and changed > extracted:
            stale.append(f"{meta['name']} `{nid}`")
            return (f"updated in Figma {meta.get('updated_at')} · values extracted {extracted} · "
                    "**⚠️ STALE: Figma changed this after the values below were extracted; re-extract before relying on them**")
        return f"updated in Figma {meta.get('updated_at', '?')} · values extracted {extracted or '?'}"
    for s in sorted(sets_meta, key=lambda z: ((z.get("containing_frame") or {}).get("pageName", ""), z["name"])):
        payload = load(s["node_id"])
        cf = s.get("containing_frame") or {}
        if not payload:
            missing.append(f"{s['name']} `{s['node_id']}`")
            continue
        doc, styles, comps = payload["document"], payload.get("styles", {}), payload.get("components", {})
        NODE_NAMES.clear()
        index_names(doc)
        variants = [v for v in doc.get("children", []) if v["type"] == "COMPONENT"]
        n_variants += len(variants)
        sl = slugs[s["node_id"]]
        renders = {}
        for v in variants:
            r = crop("ds", cf.get("pageId", ""), bounds.get("ds"), v.get("absoluteBoundingBox"),
                     OUT / "renders" / sl / f"{slug(v['name'])}.png", CROP_MARGIN)
            if r:
                renders[v["id"]] = r
        set_render = crop("ds", cf.get("pageId", ""), bounds.get("ds"), doc.get("absoluteBoundingBox"),
                          OUT / "renders" / sl / "_set.png", CROP_MARGIN)
        lines = [f"# {s['name']} — design library component set `{s['node_id']}`", "",
                 f"File `{ds_key}` · page {cf.get('pageName', '')} · key `{s['key']}` · {freshness(s, s['node_id'])} · "
                 f"source {payload.get('source', 'rest')}" + (" · **UNPUBLISHED in the library**" if s.get("unpublished") else "")
                 + (f" · whole-set render `{set_render}`" if set_render else ""),
                 "Generated by `.workflow/scripts/design_library.py` — values transcribed, not interpreted. Re-run it; do not edit.",
                 "", f"**{len(variants)} variants:**", ""]
        for v in variants:
            lines.append(f"- `{v['id']}` **{v['name']}**" + (f" — render `{renders[v['id']]}`" if v["id"] in renders else ""))
        if s.get("description"):
            lines += ["", f"Description: {s['description']}"]
        lines += ["", "## State diffs", ""]
        diffs = state_diffs(variants, styles, comps)
        lines += diffs if diffs else ["(no state property, or only default variants)"]
        lines += ["", "## Variant trees", ""]
        for v in variants:
            lines.append(f"\n### `{v['id']}` {v['name']}\n")
            lines += outline(v, styles, comps)
        (OUT / "sets" / f"{sl}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        index_rows.append(f"| {s['name']} | `{s['node_id']}` | {cf.get('pageName', '')} | {len(variants)} | "
                          + "<br>".join(f"`{v['id']}` {v['name']}" for v in variants) + f" | [sets/{sl}.md](sets/{sl}.md) |")
    for c in sorted(standalone, key=lambda z: z["name"]):
        payload = load(c["node_id"])
        cf = c.get("containing_frame") or {}
        if not payload:
            missing.append(f"{c['name']} `{c['node_id']}`")
            continue
        doc = payload["document"]
        NODE_NAMES.clear()
        index_names(doc)
        sl = slugs[c["node_id"]]
        r = crop("ds", cf.get("pageId", ""), bounds.get("ds"), doc.get("absoluteBoundingBox"),
                 OUT / "renders" / "_standalone" / f"{sl}.png", CROP_MARGIN)
        lines = [f"# {c['name']} — design library component `{c['node_id']}`", "",
                 f"File `{ds_key}` · page {cf.get('pageName', '')} · key `{c['key']}` · {freshness(c, c['node_id'])} · "
                 f"source {payload.get('source', 'rest')}" + (" · **UNPUBLISHED in the library**" if c.get("unpublished") else "")
                 + (f" · render `{r}`" if r else ""),
                 "Generated by `.workflow/scripts/design_library.py` — values transcribed, not interpreted.", "", "## Tree", ""]
        lines += outline(doc, payload.get("styles", {}), payload.get("components", {}))
        (OUT / "sets" / f"{sl}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        index_rows.append(f"| {c['name']} *(standalone)* | `{c['node_id']}` | {cf.get('pageName', '')} | 1 | — | [sets/{sl}.md](sets/{sl}.md) |")

    partial = (f"; {len(snapshot.get('refreshed', []))} nodes re-extracted since, latest {snapshot['updated']} "
               f"({snapshot.get('updated_source', '?')})") if snapshot.get("updated") else ""
    index = ["# design library — index", "",
             f"Library file `{ds_key}`. Snapshot extracted {snapshot.get('extracted', '?')} "
             f"({snapshot.get('source', '?')}){partial}; built by "
             "`.workflow/scripts/design_library.py` — re-run it rather than editing this file.", "",
             f"**{len(sets_meta)} component sets + {len(standalone)} standalone components; {n_variants} variants in the "
             f"sets built.** Value sheets under `sets/`, per-screen maps under `frame-map/`, renders (gitignored) "
             "under `renders/`.", ""]
    if missing:
        index += [f"**Not yet downloaded ({len(missing)}):** " + ", ".join(missing), ""]
    if stale:
        index += [f"**⚠️ Stale — changed in Figma after extraction ({len(stale)}):** " + ", ".join(stale), ""]
    index += ["| Component | Node | Page | Variants | Variant list | Value sheet |", "|---|---|---|---|---|---|"] + index_rows
    (OUT / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    log(f"sets built: {len(index_rows)}; missing: {len(missing)}; stale: {len(stale)}")

    built_frames = 0
    for f in manifest["design_source"]["frames"]:
        src = raw / "frames" / f"{f['node'].replace(':', '-')}.mcp.json"
        if not src.exists():
            continue
        fr = json.loads(src.read_text(encoding="utf-8"))
        page_id, page_name = (fr.get("page") or [None, None])
        shot = None
        if fr.get("b") and page_id:
            x, y, w, h = fr["b"]
            shot = crop("ws", page_id, bounds.get("ws"), {"x": x, "y": y, "width": w, "height": h},
                        OUT / "renders" / "_screens" / f"{f['slug']}.png", 0)
        rows, n_inst, n_local = [], 0, 0
        for key, g in sorted(fr["groups"].items(), key=lambda kv: -kv[1]["count"]):
            n_inst += g["count"]
            master = by_key.get(key)
            main = g.get("main") or [None, key, "", 0]
            if master:
                cs = (master.get("containing_frame") or {}).get("containingComponentSet") or {}
                where = (f"**{cs.get('name')}** → `{master['name']}`" if cs else f"**{master['name']}**") + f" `{master['node_id']}`"
            else:
                n_local += g["count"]
                where = f"LOCAL `{main[1]}`" + (f" (set `{main[2]}`)" if main[2] else "") + f" — component `{main[0]}`"
            at = "; ".join(f"`{i}` in {p}" for i, p in zip(g["ids"], g["at"]))
            rows.append(f"| {where} | {g['count']} | {at} | {', '.join(g['ov']) if g['ov'] else '—'} |")
        texts = []
        for t in fr.get("texts", []):
            t = " ".join(t.split())
            if t and t not in texts:
                texts.append(t)
        lines = [f"# Frame map — {f['slug']} (`{f['node']}`)", "",
                 f"screens file `{ws_key}` · page {page_name} · frame **{fr.get('name')}** · ticket {f.get('ticket', '—')} · "
                 "generated by `.workflow/scripts/design_library.py` (MCP source)" + (f" · render `{shot}`" if shot else ""), "",
                 f"**{len(fr['groups'])} distinct components, {n_inst} instances; {n_local} instances are LOCAL copies** "
                 "(not a design library component — compare them with the master before trusting the frame's values). "
                 "Read each master's value sheet for its variants (hover, active, error, collapsed...). "
                 "A library instance renders the component version the screens file file has accepted, so the "
                 "screen render can lag the master: where the render and the master's value sheet disagree, the "
                 "master is newer — record both and ask the design owner.", "",
                 "| Master component | Instances | Where (first 6) | Fields the instances override |", "|---|---|---|---|"]
        lines += rows
        dc = OUT.parent / "design-context" / f"{f['slug']}.md"
        if dc.exists():
            import html as _html
            extract = " ".join(_html.unescape(dc.read_text(encoding="utf-8")).split())
            drift = [t for t in texts if t not in extract]
            lines += ["", f"## Copy absent from the intake extract `design-context/{f['slug']}.md` ({len(drift)})", "",
                      "Candidates for copy the design owner changed since the intake. A string can also be absent because the "
                      "extract split it across spans or dropped a hidden layer — check the render before acting.", ""]
            lines += [f"- {t[:300]}" for t in drift] if drift else ["(none)"]
        lines += ["", f"## Text content ({len(texts)} distinct strings, document order)", ""]
        lines += [f"- {t[:300]}" for t in texts]
        (OUT / "frame-map" / f"{f['slug']}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        built_frames += 1
    log(f"frame maps built: {built_frames} of {len(manifest['design_source']['frames'])}")
    build_frames(manifest, ws_key)
    build_flows(manifest, ws_key)
    build_tokens(ds_key)


def build_frames(manifest: dict, ws_key: str) -> None:
    """Today's screen values (shallow dumps): frames/<slug>.md."""
    raw = OUT / "raw"
    out_dir = OUT / "frames"
    n = 0
    allc = raw / "all_components.json"
    if allc.exists():
        for pg in json.loads(allc.read_text(encoding="utf-8"))["pages"].values():
            for nid, _typ, _name, _parent, key in pg["rows"]:
                LIB_BY_KEY[key] = nid
    for f in manifest["design_source"]["frames"]:
        src = raw / "frames" / f"{f['node'].replace(':', '-')}.shallow.json"
        if not src.exists():
            continue
        payload = json.loads(src.read_text(encoding="utf-8"))
        doc = payload["document"]
        NODE_NAMES.clear()
        index_names(doc)
        out_dir.mkdir(exist_ok=True)
        lines = [f"# Screen values — {f['slug']} (`{f['node']}`)", "",
                 f"screens file `{ws_key}` · frame **{doc['name']}** · ticket {f.get('ticket', '—')} · shallow dump: "
                 "non-instance nodes in full; each instance is a reference to its master (see `../sets/`) plus the "
                 "descendants this screen overrides (`override` lines, with the overridden fields). Generated by "
                 "`.workflow/scripts/design_library.py` — values transcribed, not interpreted.", "",
                 f"Precedence: {PRECEDENCE}. An `override` value is this screen's intent; a non-overridden value "
                 "that differs from the master is a question for the design owner.", ""]
        lines += outline(doc, payload.get("styles", {}), {})
        (out_dir / f"{f['slug']}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        n += 1
    if n:
        log(f"screen value files built: {n}")


FLOW_KEEP = {"NAVIGATE", "OVERLAY", "SWAP", "SCROLL_TO"}


def build_flows(manifest: dict, ws_key: str) -> None:
    """Prototype wiring across screens file: flows.md."""
    raw = OUT / "raw"
    fdir = raw / "flows"
    top = json.loads((raw / "ws_toplevel.json").read_text(encoding="utf-8")) if (raw / "ws_toplevel.json").exists() else {}
    if not fdir.exists() and not top:
        return
    slugs = {f["node"]: f["slug"] for f in manifest["design_source"]["frames"]}
    lines = ["# Prototype flows — screens file", "",
             f"File `{ws_key}`. Every prototype link on every page except the Archive: navigate, open overlay, swap, "
             "scroll-to, back, close and URL actions. Variant `CHANGE_TO` actions (hover and similar) are component "
             "states and live in `sets/`. Generated by `.workflow/scripts/design_library.py`.", ""]
    if top:
        lines += ["## Top-level frames per page", "",
                  "Frames with a manifest slug are covered by the intake (frame map + screen values); the rest are "
                  "not yet mapped.", "", "| Page | Node | Name | Type | Size | Manifest slug |", "|---|---|---|---|---|---|"]
        for pid, pg in top.items():
            for row in pg.get("nodes", []):
                nid, name, typ = row[0], row[1], row[2]
                size = f"{num(row[5])}×{num(row[6])}" if len(row) >= 7 else ""
                lines.append(f"| {pg['name']} | `{nid}` | {name} | {typ.lower()} | {size} | {slugs.get(nid, '**not mapped**')} |")
            for fid, fname in pg.get("flows", []):
                lines.append(f"| {pg['name']} | `{fid}` | ▶ flow start: **{fname}** | flow | | {slugs.get(fid, '')} |")
        lines.append("")
    if fdir.exists():
        for src in sorted(fdir.glob("*.json")):
            d = json.loads(src.read_text(encoding="utf-8"))
            rows = d.get("rows", [])
            lines += [f"## {d['page'][1]} (`{d['page'][0]}`) — {len(rows)} links", ""]
            for fid, fname in d.get("flows", []):
                lines.append(f"- ▶ flow start `{fid}` **{fname}**")
            if d.get("flows"):
                lines.append("")
            if not rows:
                lines += ["(no prototype links)", ""]
                continue
            lines += ["| Source screen | Control | Label | Trigger | Action | Destination | Transition |",
                      "|---|---|---|---|---|---|---|"]
            for (fr_id, fr_name, nid, nname, label, trig, atype, nav, dest, dname, tt, te, td, url) in rows:
                action = (nav or atype or "").lower()
                target = (f"`{dest}` {dname or ''}" if dest else (url or "")).strip()
                trans = " ".join(x for x in [(tt or "").lower(), (te or "").lower(),
                                             f"{num(td * 1000)}ms" if isinstance(td, (int, float)) else ""] if x)
                lines.append(f"| {fr_name} `{fr_id}` | {nname} `{nid}` | {' '.join((label or '').split())[:50]} | "
                             f"{(trig or '').lower()} | {action} | {target} | {trans or '—'} |")
            lines.append("")
    (OUT / "flows.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("flows.md built")


def build_tokens(ds_key: str) -> None:
    """Design tokens — local styles and variables: tokens.md."""
    src = OUT / "raw" / "styles.json"
    if not src.exists():
        return
    d = json.loads(src.read_text(encoding="utf-8"))
    lines = ["# Design tokens — design library styles and variables", "",
             f"File `{ds_key}`. Local paint, text, effect and grid styles plus variables (collections, modes, values). "
             "Colours are `#RRGGBB` or `#RRGGBB@alpha`. Generated by `.workflow/scripts/design_library.py`.", ""]
    lines += [f"## Paint styles ({len(d.get('paint', []))})", "", "| Name | Value | Description |", "|---|---|---|"]
    for sid, name, paints, desc in d.get("paint", []):
        vals = " + ".join(fmt_paint(compact_paint(x)) or "" for x in paints)
        lines.append(f"| {name} | {vals} | {desc} |")
    lines += ["", f"## Text styles ({len(d.get('text', []))})", "",
              "| Name | Family | Style | Size | Line height | Letter spacing | Case | Decoration |", "|---|---|---|---|---|---|---|---|"]
    for sid, name, fam, style, size, lh, ls, case, deco, desc in d.get("text", []):
        lhv = (f"{num(lh.get('value'))}{'%' if lh.get('unit') == 'PERCENT' else 'px' if lh.get('unit') == 'PIXELS' else ''}"
               if isinstance(lh, dict) and lh.get("unit") != "AUTO" else "auto")
        lsv = (f"{num(ls.get('value'))}{'%' if ls.get('unit') == 'PERCENT' else 'px'}" if isinstance(ls, dict) else str(ls))
        lines.append(f"| {name} | {fam} | {style} | {num(size)} | {lhv} | {lsv} | {(case or '').lower()} | {(deco or '').lower()} |")
    lines += ["", f"## Effect styles ({len(d.get('effect', []))})", "", "| Name | Effects | Description |", "|---|---|---|"]
    for sid, name, effects, desc in d.get("effect", []):
        ev = []
        for e in effects:
            if len(e) == 6:
                ev.append(fmt_effect({"type": e[0], "color": hex_to_color(e[1]), "offset": {"x": e[2], "y": e[3]},
                                      "radius": e[4], "spread": e[5]}) or "")
            else:
                ev.append(f"{e[0].lower()} {num(e[1])}px")
        lines.append(f"| {name} | {'; '.join(ev)} | {desc} |")
    if d.get("grid"):
        lines += ["", f"## Grid styles ({len(d['grid'])})", ""]
        lines += [f"- {name}: `{json.dumps(grids)}`" for sid, name, grids in d["grid"]]
    cols = {c[0]: c for c in d.get("collections", [])}
    lines += ["", f"## Variables ({len(d.get('variables', []))} in {len(cols)} collections)", ""]
    by_col: dict = {}
    for v in d.get("variables", []):
        by_col.setdefault(v[3], []).append(v)
    for cid, vs in by_col.items():
        col = cols.get(cid, [cid, cid, [], None])
        modes = col[2]
        lines += [f"### {col[1]} — modes: {', '.join(m[1] for m in modes) or '—'}", "",
                  "| Variable | Type | " + " | ".join(m[1] for m in modes) + " | Scopes |",
                  "|---|---|" + "---|" * len(modes) + "---|"]
        for vid, name, typ, _, values, scopes, desc in sorted(vs, key=lambda x: x[1]):
            cells = [str(values.get(m[0], "")) for m in modes]
            lines.append(f"| {name} | {typ.lower()} | " + " | ".join(cells) + f" | {', '.join(scopes or [])} |")
        lines.append("")
    (OUT / "tokens.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("tokens.md built")


RAW_TO_CHARS = 0.32  # raw/ node JSON (REST shape) is ~3.1x the extractor's compact text for the same node
BATCH_CHARS = 60000  # extractor text per dump tag


def extractor_chunk() -> int:
    m = re.search(r"const CHUNK(?:_BYTES)? = (\d+);", (Path(__file__).resolve().parent / "design_extract.js").read_text(encoding="utf-8"))
    return int(m.group(1)) if m else 14000


def plan(manifest: dict) -> None:
    """Print the MCP calls a refresh needs: the fingerprints, then only what differs from raw/hashes.json."""
    raw = OUT / "raw"

    def load(name: str, default):
        p = raw / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    fps, base = load("fingerprints.json", {}), load("hashes.json", {})
    bounds = load("page_bounds.json", {"ds": {}, "ws": {}})
    ds_key = manifest["design_source"]["library_file_key"]
    ws_key = manifest["design_source"]["screens_file_key"]
    ds_pages = sorted(load("all_components.json", {"pages": {}})["pages"])
    ws_pages = sorted(bounds.get("ws", {}))
    frames = [f["node"] for f in manifest["design_source"]["frames"]]
    prefix = "r" + time.strftime("%m%d%H%M")
    chunk_chars = extractor_chunk()
    log(f"STEP 1 — fingerprints: one use_figma call per file (call again with part+1 until part == parts-1). "
        "Import them with --from-transcripts --session <id>, then run --plan again for STEP 2.")
    log(f'  {ds_key}: return await fingerprints("{prefix}-fp-ds", {{ dsPages: {json.dumps(ds_pages)}, styles: true }}, 0);')
    log(f'  {ws_key}: return await fingerprints("{prefix}-fp-ws", {{ frames: {json.dumps(frames)}, wsPages: {json.dumps(ws_pages)} }}, 0);')
    if not any(k.startswith("ds:node:") for k in fps) or not any(k.startswith("ws:node:") for k in fps):
        log("No fingerprints imported for one of the files yet: run STEP 1 first.")
        return

    def changed(key: str) -> bool:
        f, b = fps.get(key), base.get(key)
        return f is not None and (b is None or b["hash"] != f["hash"])

    fp_ts = max(f["ts"] for k, f in fps.items() if not k.startswith("_"))
    ds_dump = sorted(k.split(":", 2)[2] for k in fps if k.startswith("ds:node:") and changed(k))
    pages_ts = fps.get("_pages", {})
    removed = sorted(k.split(":", 2)[2] for k in base if k.startswith("ds:node:")
                     and (k not in fps or fps[k]["ts"] < pages_ts.get(f"ds:{fps[k].get('page')}", "")))
    ds_list = [p for p in ds_pages if changed(f"ds:list:{p}")]
    styles = changed("ds:styles")
    ws_frames = [f for f in frames if changed(f"ws:node:{f}")]
    unknown = [f for f in frames if f"ws:node:{f}" not in fps]
    ws_top = [p for p in ws_pages if changed(f"ws:top:{p}")]
    ws_flow = [p for p in ws_pages if changed(f"ws:flow:{p}")]

    def size(path: Path, ratio: float, default: int) -> int:
        return int(path.stat().st_size * ratio) if path.exists() else default

    def pack(ids: list[str], size_of) -> list[tuple[list[str], int]]:
        out, cur, tot = [], [], 0
        for i in ids:
            s = size_of(i)
            if cur and tot + s > BATCH_CHARS:
                out.append((cur, tot))
                cur, tot = [], 0
            cur.append(i)
            tot += s
        if cur:
            out.append((cur, tot))
        return out

    def calls(est: int) -> int:
        return max(1, -(-est // chunk_chars))

    def renders(role: str, node_ids: list[str]) -> list[dict]:
        rows = []
        content = {fps[f"{role}:node:{n}"]["page"] for n in node_ids if f"{role}:node:{n}" in fps}
        wanted = ds_pages if role == "ds" else sorted({fps[f"ws:node:{f}"]["page"] for f in frames if f"ws:node:{f}" in fps})
        for pid in wanted:
            fb = (fps.get(f"{role}:bounds:{pid}") or {}).get("renderUnion")
            if not fb or None in fb:
                continue
            why = []
            if fb != ((bounds.get(role) or {}).get(pid) or {}).get("renderUnion"):
                why.append("bounds moved")
            if pid in content:
                why.append("content changed")
            pngs = sorted((OUT / "renders" / "_pages").glob(f"{role}-{pid.replace(':', '-')}*.png"))
            last = max((b.get("dumped", "") for k, b in base.items()
                        if k.startswith(f"{role}:node:") and b.get("page") == pid), default="")
            if not pngs:
                why.append("no render")
            elif last and time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(pngs[0].stat().st_mtime)) < last[:19]:
                why.append("render older than its values")
            if why:
                name = pngs[0].name if pngs else f"{role}-{pid.replace(':', '-')}.png"
                rows.append({"page": pid, "file": f"renders/_pages/{name}", "maxDimension": round(max(fb[2] - fb[0], fb[3] - fb[1])),
                             "why": why})
        return rows

    total = 0
    out: dict = {"fingerprints_at": fp_ts, "prefix": prefix, "ds": {}, "ws": {}}
    log(f"\nSTEP 2 — from fingerprints taken {fp_ts} (prefix {prefix}):")
    log(f"\nlibrary file {ds_key}:")
    batches = pack(ds_dump, lambda i: size(raw / raw_name(i), RAW_TO_CHARS, 20000))
    for n, (ids, est) in enumerate(batches, 1):
        total += calls(est)
        log(f'  return await dumpNodes("{prefix}-ds-{n}", {json.dumps(ids)}, 0);   // ~{calls(est)} calls')
    for p in ds_list:
        total += 1
        log(f'  return await listComponents("{prefix}-dsl-{p.replace(":", "-")}", "{p}", 0);')
    if styles:
        total += 2
        log(f'  return await stylesDump("{prefix}-styles", 0);')
    ds_r = renders("ds", ds_dump)
    if ds_r:
        total += 1 + len(ds_r)
        log(f'  return {{ pages: await pageBounds({json.dumps([r["page"] for r in ds_r])}), sizes: {{}} }};')
    for r in ds_r:
        log(f'  get_screenshot nodeId {r["page"]} maxDimension {r["maxDimension"]} -> {r["file"]}   ({", ".join(r["why"])})')
    out["ds"] = {"dump": batches, "list": ds_list, "styles": styles, "renders": ds_r, "removed": removed}
    log(f"\nscreens file {ws_key}:")
    sh = pack(ws_frames, lambda i: size(raw / "frames" / f"{i.replace(':', '-')}.shallow.json", RAW_TO_CHARS, 30000))
    fm = pack(ws_frames, lambda i: size(raw / "frames" / f"{i.replace(':', '-')}.mcp.json", 1.0, 8000))
    for n, (ids, est) in enumerate(fm, 1):
        total += calls(est)
        log(f'  return await frameMap("{prefix}-fm-{n}", {json.dumps(ids)}, 0);   // ~{calls(est)} calls')
    for n, (ids, est) in enumerate(sh, 1):
        total += calls(est)
        log(f'  return await shallowFrames("{prefix}-sh-{n}", {json.dumps(ids)}, 0);   // ~{calls(est)} calls')
    if ws_top:
        total += 1
        log(f'  return await topLevel("{prefix}-top", {json.dumps(ws_top)}, 0);')
    for p in ws_flow:
        total += 1
        log(f'  return await flowMap("{prefix}-flow-{p.replace(":", "-")}", "{p}", 0);')
    ws_r = renders("ws", ws_frames)
    if ws_r:
        total += 1 + len(ws_r)
        log(f'  return {{ bounds: await pageBounds({json.dumps([r["page"] for r in ws_r])}) }};')
    for r in ws_r:
        log(f'  get_screenshot nodeId {r["page"]} maxDimension {r["maxDimension"]} -> {r["file"]}   ({", ".join(r["why"])})')
    out["ws"] = {"frameMap": fm, "shallow": sh, "top": ws_top, "flow": ws_flow, "renders": ws_r, "not_found": unknown}
    (raw / "refresh_plan.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    n_ds = sum(1 for k in fps if k.startswith("ds:node:"))
    log(f"\nChanged: {len(ds_dump)} of {n_ds} library file nodes, {len(ws_frames)} of {len(frames)} frames, "
        f"{len(ws_flow)} flow pages, {len(ds_r) + len(ws_r)} renders. About {total} MCP calls.")
    if removed:
        log(f"Gone from Figma since the last check: {', '.join(removed)}")
    if unknown:
        log(f"Manifest frames with no fingerprint (not found in Figma, or STEP 1 not run for them): {', '.join(unknown)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="design library download + frame maps")
    ap.add_argument("--from-transcripts", action="store_true", help="import MCP chunks from session transcripts")
    ap.add_argument("--session", help="limit transcript scan to one session id (main + its subagents)")
    ap.add_argument("--build-only", action="store_true", help="rebuild sheets/maps/crops from raw/ only")
    ap.add_argument("--skip-frames", action="store_true", help="REST mode: skip the screens file frames")
    ap.add_argument("--changes", action="store_true",
                    help="2 REST calls: store the published set/component lists and print what changed; no build")
    ap.add_argument("--plan", action="store_true",
                    help="print the MCP calls a refresh needs (fingerprints, then only what changed); no build")
    ap.add_argument("--accept-fingerprints", action="store_true",
                    help="with --from-transcripts: take the imported fingerprints as the baseline for everything. "
                         "Only right after a complete extraction")
    args = ap.parse_args(argv)
    if OUT is None or not MANIFEST.is_file():
        print("SKIPPED — config: design.library_dir is empty or the design manifest "
              "(config: design.manifest) does not exist")
        return 3
    if args.accept_fingerprints and not args.from_transcripts:
        ap.error("--accept-fingerprints needs --from-transcripts")
    if args.changes:
        rest_changes()
        return 0
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.plan:
        plan(manifest)
        return 0
    if args.from_transcripts:
        import_transcripts(args.session, args.accept_fingerprints)
    elif not args.build_only:
        rest_fetch(args.skip_frames)
    build(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
