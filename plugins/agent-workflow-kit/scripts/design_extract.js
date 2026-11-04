// Figma Plugin API extractor for the Figma MCP `use_figma` tool — read-only.
//
// Prefixes: `ds` = the library file (`design_source.library_file_key` of the design manifest),
// `ws` = the screens file (`design_source.screens_file_key`).
//
// Used when the REST API quota is exhausted (see .workflow/scripts/design_library.py). Paste this whole
// file as the `code` of a `use_figma` call, followed by ONE main line:
//
//   return await sizeNodes(["21:43", "65:87"]);                 // chars per node, to plan chunks
//   return await dumpNodes("ds-b1", ["21:43"], 0);              // part 0 of a compact node dump
//   return await frameMap("ws-p1", ["12:34", "56:78"], 0);  // part 0 of instance -> master maps
//   return await fingerprints("fp-ds", { dsPages: ["7:7"], styles: true }, 0);  // content hashes
//
// A refresh starts with one `fingerprints` call per file and re-extracts only what changed:
// `design_library.py --plan` prints the exact calls (fingerprints first, then the changed nodes).
//
// dumpNodes / frameMap return a string "@@ML|<kind>|<tag>|<part>|<parts>|<total>\n<slice>". Each slice
// is at most CHUNK_BYTES of UTF-8, under the MCP's 20KB output cap; call again with part+1 until
// part == parts-1. The same inputs always serialize and split identically, so parts concatenate.
// `design_library.py --from-transcripts` reassembles the parts from the session transcripts,
// so values are never re-typed by hand. Colours are "#RRGGBB" or "#RRGGBB@alpha"; paint opacity is
// folded into alpha.

const CHUNK_BYTES = 19000;
const MIXED = figma.mixed;
const styleCache = {};

const r3 = (v) => (typeof v === "number" ? +v.toFixed(2) : v);
function hx(c, alpha) {
  const h = (x) => Math.round(x * 255).toString(16).padStart(2, "0").toUpperCase();
  const a = alpha === undefined ? (c.a === undefined ? 1 : c.a) : alpha * (c.a === undefined ? 1 : c.a);
  const s = "#" + h(c.r) + h(c.g) + h(c.b);
  return a < 0.995 ? s + "@" + +a.toFixed(3) : s;
}
function inv(t) {
  const [[a, b, c], [d, e, f]] = t;
  const det = a * e - b * d;
  if (!det) return null;
  return [[e / det, -b / det, (b * f - c * e) / det], [-d / det, a / det, (c * d - a * f) / det]];
}
const ap = (m, x, y) => [m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2]];

function paint(p) {
  if (p.visible === false) return null;
  const op = p.opacity === undefined ? 1 : p.opacity;
  if (p.type === "SOLID") return "S" + hx(p.color, op);
  if (p.type.startsWith("GRADIENT")) {
    const m = inv(p.gradientTransform);
    const handles = m ? [...ap(m, 0, 0.5), ...ap(m, 1, 0.5)].map(r3) : null;
    return ["G", p.type.replace("GRADIENT_", ""), p.gradientStops.map((s) => [hx(s.color), r3(s.position)]), handles, r3(op)];
  }
  if (p.type === "IMAGE") return "I" + (p.scaleMode || "");
  return p.type;
}

async function styleName(id) {
  if (!id || typeof id !== "string") return null;
  if (!(id in styleCache)) {
    try {
      const s = await figma.getStyleByIdAsync(id);
      styleCache[id] = s ? s.name : null;
    } catch (e) {
      styleCache[id] = null;
    }
  }
  return styleCache[id];
}

async function ser(n, depth, shallow, noKids) {
  const o = { i: n.id, n: n.name, t: n.type };
  if (n.visible === false) o.v = 0;
  if ("opacity" in n && n.opacity !== 1) o.o = r3(n.opacity);
  const bb = n.absoluteBoundingBox;
  if (bb) {
    o.w = r3(bb.width);
    o.h = r3(bb.height);
    if (depth <= 1) {
      o.x = r3(bb.x);
      o.y = r3(bb.y);
    }
  }
  if ("layoutMode" in n && n.layoutMode !== "NONE") {
    o.L = [n.layoutMode, r3(n.itemSpacing), n.paddingTop, n.paddingRight, n.paddingBottom, n.paddingLeft,
      n.primaryAxisAlignItems, n.counterAxisAlignItems];
  }
  if ("fills" in n) {
    if (n.fills === MIXED) o.f = "mixed";
    else {
      const f = n.fills.map(paint).filter((x) => x !== null);
      if (f.length) o.f = f;
    }
  }
  if ("strokes" in n && n.strokes.length) {
    const s = n.strokes.map(paint).filter((x) => x !== null);
    if (s.length) {
      o.s = s;
      // Reading a property a node type lacks throws (it does not return undefined), so guard with `in`.
      const four = "strokeTopWeight" in n
        ? [n.strokeTopWeight, n.strokeRightWeight, n.strokeBottomWeight, n.strokeLeftWeight] : null;
      o.sw = n.strokeWeight === MIXED || (four && new Set(four).size > 1) ? four : n.strokeWeight;
      o.sa = n.strokeAlign;
      if ("dashPattern" in n && n.dashPattern.length) o.sd = n.dashPattern;
    }
  }
  if ("cornerRadius" in n) {
    if (n.cornerRadius === MIXED) o.r = [n.topLeftRadius, n.topRightRadius, n.bottomRightRadius, n.bottomLeftRadius];
    else if (n.cornerRadius) o.r = n.cornerRadius;
  }
  if ("effects" in n && n.effects.length) {
    const e = n.effects.filter((x) => x.visible !== false).map((x) =>
      x.type.endsWith("SHADOW")
        ? [x.type, hx(x.color), x.offset.x, x.offset.y, x.radius, x.spread || 0]
        : [x.type, x.radius]);
    if (e.length) o.e = e;
  }
  const st = {};
  for (const [k, prop] of [["fill", "fillStyleId"], ["stroke", "strokeStyleId"], ["effect", "effectStyleId"], ["text", "textStyleId"]]) {
    if (prop in n && typeof n[prop] === "string" && n[prop]) {
      const nm = await styleName(n[prop]);
      if (nm) st[k] = nm;
    }
  }
  if (Object.keys(st).length) o.st = st;
  if (n.type === "TEXT") {
    const segs = n.getStyledTextSegments(["fontName", "fontSize", "fontWeight", "lineHeight", "letterSpacing", "textCase", "fills"]);
    const g = segs[0] || {};
    const lh = g.lineHeight;
    const ls = g.letterSpacing;
    const lhPx = lh && lh.unit === "PIXELS" ? lh.value : lh && lh.unit === "PERCENT" ? (g.fontSize * lh.value) / 100 : null;
    const lsPx = ls ? (ls.unit === "PIXELS" ? ls.value : (g.fontSize * ls.value) / 100) : 0;
    o.T = [n.characters, g.fontName ? g.fontName.family : null, g.fontName ? g.fontName.style : null, g.fontWeight,
      g.fontSize, r3(lhPx), r3(lsPx), g.textCase && g.textCase !== "ORIGINAL" ? g.textCase : "", n.textAlignHorizontal, segs.length];
    if (o.f === "mixed" && g.fills) o.f = g.fills.map(paint).filter((x) => x !== null);
  }
  if (n.type === "TEXT") {
    const all = n.getStyledTextSegments(["fontName", "fontSize", "fontWeight", "fills", "textDecoration"]);
    if (all.length > 1) {
      o.R = all.map((g) => [g.start, g.end, g.fills.map(paint).filter((x) => x !== null),
        g.fontName.family, g.fontName.style, g.fontWeight, g.fontSize, g.textDecoration]);
    }
  }
  if ("reactions" in n && n.reactions && n.reactions.length) {
    o.rx = n.reactions.map((r) => [r.trigger ? r.trigger.type : null, r.trigger && r.trigger.delay ? r.trigger.delay : 0,
      (r.actions || (r.action ? [r.action] : [])).filter((a) => a).map((a) => [a.destinationId || null, a.navigation || a.type,
        a.transition ? a.transition.type : null, a.transition && a.transition.easing ? a.transition.easing.type : null,
        a.transition ? a.transition.duration : null])]);
  }
  if (n.type === "INSTANCE") {
    const mc = await n.getMainComponentAsync();
    if (mc) {
      const setName = mc.parent && mc.parent.type === "COMPONENT_SET" ? mc.parent.name : "";
      o.I = [mc.id, mc.key, mc.name, setName, mc.remote ? 1 : 0];
    }
    const ov = new Set();
    for (const x of n.overrides) for (const fld of x.overriddenFields) ov.add(fld);
    if (ov.size) o.ov = [...ov].sort();
    if (shallow) {
      // Shallow mode: an instance is a reference to its master plus a snapshot of each descendant this
      // screen overrides (`of` = the overridden fields). Its unmodified internals live in the master sheet.
      const oc = [];
      for (const x of n.overrides) {
        if (x.id === n.id) continue;
        const d = await figma.getNodeByIdAsync(x.id);
        if (!d) continue;
        const s = await ser(d, depth + 1, true, true);
        s.of = x.overriddenFields;
        oc.push(s);
      }
      if (oc.length) o.oc = oc;
      return o;
    }
  }
  if ("children" in n && !noKids) {
    o.c = [];
    for (const ch of n.children) o.c.push(await ser(ch, depth + 1, shallow, false));
  }
  return o;
}

// Today's screen values: non-instance nodes in full, instances as master references plus their overrides.
// return await shallowFrames("wsv-<n>", ["12:34"], 0);   sizes first: return await sizeShallow([...ids]);
async function shallowFrames(tag, ids, part) {
  const nodes = {};
  for (const id of ids) {
    const n = await figma.getNodeByIdAsync(id);
    if (n) nodes[id] = { document: await ser(n, 0, true, false) };
  }
  return chunk("shallow", tag, JSON.stringify({ file: figma.fileKey, nodes }), part);
}

async function sizeShallow(ids) {
  const out = [];
  for (const id of ids) {
    const n = await figma.getNodeByIdAsync(id);
    out.push([id, n ? n.name : null, n ? JSON.stringify(await ser(n, 0, true, false)).length : 0]);
  }
  return out;
}

// Top-level nodes of each page plus the page's prototype flow starting points.
// return await topLevel("wst", ["0:1", "7:7"], 0);
async function topEntry(p) {
  await p.loadAsync();
  return {
    name: p.name,
    flows: (p.flowStartingPoints || []).map((f) => [f.nodeId, f.name]),
    nodes: p.children.map((c) => {
      const b = c.absoluteBoundingBox;
      return [c.id, c.name, c.type].concat(b ? [b.x, b.y, b.width, b.height].map(r3) : []);
    }),
  };
}

async function topLevel(tag, pageIds, part) {
  const pages = {};
  for (const pid of pageIds) {
    const p = await figma.getNodeByIdAsync(pid);
    if (!p) continue;
    pages[pid] = await topEntry(p);
  }
  return chunk("top", tag, JSON.stringify({ file: figma.fileKey, pages }), part);
}

// Prototype wiring on one page: every navigate / overlay / swap / back / close / url / scroll action.
// Variant CHANGE_TO actions (hover and similar) are skipped — they are component states, in the sheets.
// return await flowMap("wsf-<page>", "<pageId>", 0);
async function flowMap(tag, pageId, part) {
  return chunk("flow", tag, JSON.stringify(await flowPayload(pageId)), part);
}

async function flowPayload(pageId) {
  const p = await figma.getNodeByIdAsync(pageId);
  await p.loadAsync();
  const names = {};
  async function nameOf(id) {
    if (!(id in names)) {
      const d = await figma.getNodeByIdAsync(id);
      names[id] = d ? d.name : null;
    }
    return names[id];
  }
  function frameOf(n) {
    let x = n;
    while (x.parent && x.parent.type !== "PAGE") x = x.parent;
    return x;
  }
  function firstText(n) {
    if (n.type === "TEXT") return n.characters;
    if ("findOne" in n) {
      const t = n.findOne((x) => x.type === "TEXT");
      return t ? t.characters : "";
    }
    return "";
  }
  const rows = [];
  const withReactions = p.findAll((n) => "reactions" in n && n.reactions && n.reactions.length > 0);
  for (const n of withReactions) {
    for (const r of n.reactions) {
      const acts = (r.actions || (r.action ? [r.action] : [])).filter((a) => a);
      for (const a of acts) {
        if (a.type === "NODE" && a.navigation === "CHANGE_TO") continue;
        const fr = frameOf(n);
        const t = a.transition || null;
        rows.push([fr.id, fr.name, n.id, n.name, firstText(n).slice(0, 60), r.trigger ? r.trigger.type : null,
          a.type, a.navigation || null, a.destinationId || null, a.destinationId ? await nameOf(a.destinationId) : null,
          t ? t.type : null, t && t.easing ? t.easing.type : null, t ? t.duration : null, a.url || null]);
      }
    }
  }
  return { file: figma.fileKey, page: [p.id, p.name], flows: (p.flowStartingPoints || []).map((f) => [f.nodeId, f.name]), rows };
}

// Design tokens: local paint / text / effect / grid styles and variables (collections, modes, values).
// return await stylesDump("dss", 0);
async function stylesDump(tag, part) {
  return chunk("styles", tag, JSON.stringify(await stylesPayload()), part);
}

async function stylesPayload() {
  const ps = await figma.getLocalPaintStylesAsync();
  const ts = await figma.getLocalTextStylesAsync();
  const es = await figma.getLocalEffectStylesAsync();
  const gs = await figma.getLocalGridStylesAsync();
  const cols = await figma.variables.getLocalVariableCollectionsAsync();
  const vars = await figma.variables.getLocalVariablesAsync();
  const vname = {};
  for (const v of vars) vname[v.id] = v.name;
  const val = (x) => {
    if (x && typeof x === "object" && x.type === "VARIABLE_ALIAS") return "alias:" + (vname[x.id] || x.id);
    if (x && typeof x === "object" && "r" in x) return hx(x);
    return x;
  };
  const out = {
    file: figma.fileKey,
    paint: ps.map((s) => [s.id, s.name, s.paints.map(paint).filter((x) => x !== null), s.description || ""]),
    text: ts.map((s) => [s.id, s.name, s.fontName.family, s.fontName.style, s.fontSize, s.lineHeight, s.letterSpacing,
      s.textCase, s.textDecoration, s.description || ""]),
    effect: es.map((s) => [s.id, s.name, s.effects.filter((e) => e.visible !== false).map((e) =>
      e.type.endsWith("SHADOW") ? [e.type, hx(e.color), e.offset.x, e.offset.y, e.radius, e.spread || 0] : [e.type, e.radius]),
      s.description || ""]),
    grid: gs.map((s) => [s.id, s.name, s.layoutGrids]),
    collections: cols.map((c) => [c.id, c.name, c.modes.map((m) => [m.modeId, m.name]), c.defaultModeId]),
    variables: vars.map((v) => [v.id, v.name, v.resolvedType, v.variableCollectionId,
      Object.fromEntries(Object.entries(v.valuesByMode).map(([m, x]) => [m, val(x)])), v.scopes, v.description || ""]),
  };
  return out;
}

// Cuts fall on UTF-8 byte counts (the cap is on bytes; a non-ASCII character takes 2-4) and never inside
// a surrogate pair. `total` stays the UTF-16 length the importer checks.
function chunk(kind, tag, text, part) {
  const cuts = [0];
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    const pair = c >= 0xd800 && c <= 0xdbff && i + 1 < text.length;
    const w = pair ? 4 : c >= 0x800 ? 3 : c >= 0x80 ? 2 : 1;
    if (bytes + w > CHUNK_BYTES) {
      cuts.push(i);
      bytes = 0;
    }
    bytes += w;
    if (pair) i++;
  }
  cuts.push(text.length);
  return "@@ML|" + kind + "|" + tag + "|" + part + "|" + (cuts.length - 1) + "|" + text.length + "\n" +
    text.slice(cuts[part], cuts[part + 1]);
}

// The union of each page's top-level bounds (layout and render), so a page-level get_screenshot can be
// cropped per component or frame using their absolute boxes: return await pageBounds(["7:8"]);
async function pageBounds(pageIds) {
  const out = {};
  for (const pid of pageIds) {
    const p = await figma.getNodeByIdAsync(pid);
    out[pid] = p ? await boundsOf(p) : null;
  }
  return out;
}

async function boundsOf(p) {
  await p.loadAsync();
  const kids = [];
  const u = [Infinity, Infinity, -Infinity, -Infinity];
  const ur = [Infinity, Infinity, -Infinity, -Infinity];
  for (const c of p.children) {
    const b = c.absoluteBoundingBox;
    const rb = c.absoluteRenderBounds || b;
    if (!b) continue;
    kids.push([c.id, c.name, c.type, r3(b.x), r3(b.y), r3(b.width), r3(b.height)]);
    u[0] = Math.min(u[0], b.x); u[1] = Math.min(u[1], b.y); u[2] = Math.max(u[2], b.x + b.width); u[3] = Math.max(u[3], b.y + b.height);
    ur[0] = Math.min(ur[0], rb.x); ur[1] = Math.min(ur[1], rb.y); ur[2] = Math.max(ur[2], rb.x + rb.width); ur[3] = Math.max(ur[3], rb.y + rb.height);
  }
  return { name: p.name, union: u.map(r3), renderUnion: ur.map(r3), nKids: kids.length };
}

// Every COMPONENT_SET and COMPONENT on one page, published or not (the REST component lists return
// published components only): return await listComponents("dsl-<page>", "<pageId>", 0);
async function listComponents(tag, pageId, part) {
  const p = await figma.getNodeByIdAsync(pageId);
  await p.loadAsync();
  return chunk("list", tag, JSON.stringify({ file: figma.fileKey, page: [p.id, p.name], rows: listRows(p) }), part);
}

function listRows(p) {
  return p.findAllWithCriteria({ types: ["COMPONENT_SET", "COMPONENT"] }).map((n) =>
    [n.id, n.type, n.name, n.parent && n.parent.type === "COMPONENT_SET" ? n.parent.id : null, n.key]);
}

async function sizeNodes(ids) {
  const out = [];
  for (const id of ids) {
    const n = await figma.getNodeByIdAsync(id);
    if (!n) { out.push([id, null, 0]); continue; }
    out.push([id, n.name, JSON.stringify(await ser(n, 0)).length]);
  }
  return out;
}

async function dumpNodes(tag, ids, part) {
  const nodes = {};
  for (const id of ids) {
    const n = await figma.getNodeByIdAsync(id);
    if (n) nodes[id] = { document: await ser(n, 0) };
  }
  return chunk("dump", tag, JSON.stringify({ file: figma.fileKey, nodes }), part);
}

async function frameMap(tag, frameIds, part) {
  const frames = {};
  for (const fid of frameIds) {
    const f = await figma.getNodeByIdAsync(fid);
    if (!f) { frames[fid] = null; continue; }
    const groups = {};
    const texts = [];
    async function walk(n, path) {
      if (n.type === "TEXT") texts.push(n.characters);
      if (n.type === "INSTANCE") {
        const mc = await n.getMainComponentAsync();
        const key = mc ? mc.key : "none:" + n.name;
        if (!groups[key]) {
          const setName = mc && mc.parent && mc.parent.type === "COMPONENT_SET" ? mc.parent.name : "";
          groups[key] = { main: mc ? [mc.id, mc.name, setName, mc.remote ? 1 : 0] : null, count: 0, ids: [], at: [], ov: [] };
        }
        const g = groups[key];
        g.count += 1;
        if (g.ids.length < 6) { g.ids.push(n.id); g.at.push(path.slice(-2).join(" > ")); }
        for (const x of n.overrides) for (const fld of x.overriddenFields) if (!g.ov.includes(fld)) g.ov.push(fld);
      }
      if ("children" in n) for (const ch of n.children) await walk(ch, path.concat([n.name]));
    }
    await walk(f, []);
    const fb = f.absoluteBoundingBox;
    frames[fid] = { name: f.name, page: f.parent && f.parent.type === "PAGE" ? [f.parent.id, f.parent.name] : null,
      b: fb ? [fb.x, fb.y, fb.width, fb.height].map(r3) : null, groups, texts };
  }
  return chunk("frames", tag, JSON.stringify({ file: figma.fileKey, frames }), part);
}

// Change detection: two 32-bit FNV-1a variants over the same JSON the dumps carry. Not cryptographic.
function h64(str) {
  let a = 0x811c9dc5;
  let b = 0x9e3779b1;
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i);
    a = Math.imul(a ^ c, 0x01000193);
    b = Math.imul(b ^ c, 0x01000197);
  }
  return (a >>> 0).toString(16).padStart(8, "0") + (b >>> 0).toString(16).padStart(8, "0");
}

// One hash per dump target, frame and page part, over the serializations dumpNodes and frameMap /
// shallowFrames (the full `ser` tree), listComponents, topLevel, flowMap and stylesDump read, plus each
// page's renderUnion. Node values are [hash, pageId]. `design_library.py --plan` prints both calls.
//   library file:   return await fingerprints("fp-ds", { dsPages: [...], styles: true }, 0);
//   screens file:   return await fingerprints("fp-ws", { frames: [...], wsPages: [...] }, 0);
async function fingerprints(tag, spec, part) {
  const out = { file: figma.fileKey, nodes: {}, pages: {}, styles: null };
  // Bounds come first, as pageBounds measures them: resolving instance masters (ser) grows a frame's
  // absoluteRenderBounds within the same call.
  const ru = {};
  for (const pid of [...(spec.dsPages || []), ...(spec.wsPages || [])]) {
    const p = await figma.getNodeByIdAsync(pid);
    if (p) ru[pid] = (await boundsOf(p)).renderUnion;
  }
  for (const pid of spec.dsPages || []) {
    const p = await figma.getNodeByIdAsync(pid);
    if (!p) { out.pages[pid] = null; continue; }
    await p.loadAsync();
    const rows = listRows(p);
    for (const [id, type, , parent] of rows) {
      if (type === "COMPONENT" && parent) continue;
      const n = await figma.getNodeByIdAsync(id);
      out.nodes[id] = [h64(JSON.stringify(await ser(n, 0))), pid];
    }
    out.pages[pid] = { list: h64(JSON.stringify(rows)), renderUnion: ru[pid] };
  }
  for (const fid of spec.frames || []) {
    const n = await figma.getNodeByIdAsync(fid);
    let pg = n;
    while (pg && pg.type !== "PAGE") pg = pg.parent;
    out.nodes[fid] = n ? [h64(JSON.stringify(await ser(n, 0))), pg ? pg.id : null] : null;
  }
  for (const pid of spec.wsPages || []) {
    const p = await figma.getNodeByIdAsync(pid);
    if (!p) { out.pages[pid] = null; continue; }
    out.pages[pid] = { top: h64(JSON.stringify(await topEntry(p))), flow: h64(JSON.stringify(await flowPayload(pid))),
      renderUnion: ru[pid] };
  }
  if (spec.styles) out.styles = h64(JSON.stringify(await stylesPayload()));
  return chunk("fp", tag, JSON.stringify(out), part);
}
