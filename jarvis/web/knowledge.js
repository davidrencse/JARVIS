/* JARVIS knowledge engine console: a recon-style cluster graph with instrument panels.
   The graph is built from BOTH the knowledge engine (/api/kb/graph: sources, pages, claims,
   vault notes) and JARVIS's own memory (/api/graph/heap: replies, messages, notes, plans, tags).
   One cluster per group: its records are packed into a fuzzy 3D blob drawn as a dense
   wireframe, clusters are tied by strands for the real links between them, and a scenario
   tab recolours the slice it names. Raw WebGL2 (instanced screen-space ribbons, additive
   blending, a 2D canvas on top for labels); a 2D canvas takes over when WebGL2 is missing.
   Reader/librarian/checker events from the server pulse the node they touched. */
"use strict";

const $ = (id) => document.getElementById(id);
const TAGS = { ingest: "LOCKED", duplicate: "DUPLICATE", create: "NEW", patch: "PATCH",
  conflict: "CONFLICT", flag: "CONFLICT", answer: "SAVED", audit: "AUDIT", commit: "COMMIT",
  resolve: "RESOLVED", merge: "MERGED" };
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

/* colours as 0..1 triples for the shaders; hex for the 2D layers */
const PINKS = [[0.88, 0.33, 0.60], [0.82, 0.30, 0.72], [0.92, 0.40, 0.52], [0.72, 0.36, 0.86], [0.90, 0.46, 0.66]];
const CYAN = [0.37, 0.83, 0.90], AMBER = [0.94, 0.71, 0.16], RED = [1, 0.30, 0.37], WHITE = [0.93, 0.93, 0.93];
const HEX = { amber: "#f0b429", red: "#ff4d5e", cyan: "#5fd3e6", pink: "#e0559a", ink: "#ededed", dim: "#8a8a8a" };
const rgba = (c, a) => `rgba(${Math.round(c[0] * 255)},${Math.round(c[1] * 255)},${Math.round(c[2] * 255)},${a})`;
const MAXC = 32;

const view = {
  kbNodes: [], kbEdges: [], memNodes: [], memEdges: [], heapSig: "",
  nodes: [], edges: [], byId: new Map(), pages: [], notes: [], claimsOf: new Map(),
  layers: { wiki: true, vault: true, claims: true },
  selected: null, hover: null, pulses: [], snap: null, started: performance.now(), last: "reader :: idle",
  scn: "all", activeUntil: 0, dirty: true,
};
const cam = { az: -0.55, el: 0.26, zoom: 1, zoomTo: 1, spin: 0, drag: null,
  px: 0, py: 0, panTo: null, anchor: null, pan: null };  // px/py: screen-space pan in CSS px
const ZOOM_MIN = 0.35, ZOOM_MAX = 60;
// adaptive quality: glow and resolution step down when frames get slow, back up when fast
const quality = { scale: 1, bloom: true, ema: 16, slowSince: 0, fastSince: 0 };
let scene = { clusters: [], nodes: [], mesh: new Float32Array(0), meshN: 0, strands: new Float32Array(0), strandN: 0,
  bundles: [], extent: 1, stats: {} };

/* ---------- small helpers ---------- */
function hash(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function sigOf(obj) {
  if (obj == null) return "0";
  let s;
  try { s = JSON.stringify(obj); } catch (e) { return String(Math.random()); }
  return hash(s) + ":" + s.length;
}
function rng(seed) {  // mulberry32: stable positions for the same record across reloads
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const pad = (n, w = 3) => String(n).padStart(w, "0");
const esc = window.JarvisEsc;
const clip = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json", "X-JARVIS": "1" },
    body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
function ago(iso) {
  const t = Date.parse(iso); if (!t) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 129600 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
}
const clock8 = (iso) => { const d = new Date(iso); return isNaN(d) ? "--:--:--" : d.toTimeString().slice(0, 8); };

/* ---------- data -> records ---------- */
const FOLDER_KEEP = 5;
function ingestKB(graph, vault) {
  const nodes = graph.nodes.map((n) => ({ ...n, layer: "wiki" }));
  const edges = graph.edges.map((e) => ({ ...e }));
  if (vault) {
    for (const n of vault.nodes) {
      nodes.push({ id: "v:" + n.id, kind: "note", layer: "vault", label: n.label, path: n.id, folder: n.folder || "",
        type: n.group, degree: n.degree || 0, updated: n.updated, uri: n.uri });
    }
    for (const e of vault.edges) edges.push({ src: "v:" + e.src, dst: "v:" + e.dst, rel: "link" });
  }
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const notes = nodes.filter((n) => n.kind === "note");
  for (const n of notes) {  // bridges from a synced note into the wiki page it became
    const base = n.path.split("/").pop().replace(/\.md$/, "");
    if (byId.has("p:" + base)) { edges.push({ src: n.id, dst: "p:" + base, rel: "mirrors" }); n.mirror = "p:" + base; }
  }
  for (const s of nodes) {
    if (s.kind !== "source" || !s.origin) continue;
    const note = notes.find((n) => s.origin.endsWith("/" + n.path));
    if (note) edges.push({ src: note.id, dst: s.id, rel: "ingested" });
  }
  const pages = nodes.filter((n) => n.kind === "page" || n.kind === "answer");
  const claimsOf = new Map(pages.map((p) => [p.id, []]));
  for (const e of edges) if (e.rel === "has" && claimsOf.has(e.src) && byId.has(e.dst)) claimsOf.get(e.src).push(byId.get(e.dst));
  view.kbNodes = nodes; view.kbEdges = edges; view.pages = pages; view.notes = notes; view.claimsOf = claimsOf;
}

function ingestHeap(heap) {
  const nodes = (heap.nodes || []).map((n) => ({
    id: n.id, kind: "mem", mkind: n.kind, group: n.group || (n.kind === "kind" ? n.label : n.kind), layer: "mem",
    label: n.label || n.id, created_at: n.created_at, importance: n.importance, source: n.source }));
  view.memNodes = nodes;
  view.memEdges = (heap.edges || []).map((e) => ({ ...e }));
  view.heapSig = `${nodes.length}:${view.memEdges.length}:${nodes[0] ? nodes[0].id : ""}`;
}

/* ---------- scene: clusters, blobs, meshes, strands ---------- */
const clusterR = (n) => 0.16 + 0.07 * Math.cbrt(Math.max(1, n));
const MEM_NAME = { reply: "REPLY", message: "MESSAGE", note: "NOTE", plan: "PLAN", tags: "TAGS" };

function blobPoint(r, sh) {  // a noisy, lumpy ball: unit-ish radius, fuzzy edge
  const u = r() * 2 - 1, t = r() * 6.2832, s = Math.sqrt(1 - u * u);
  const dx = s * Math.cos(t), dy = u, dz = s * Math.sin(t);
  const lump = 1 + 0.26 * Math.sin(dx * 2.7 + sh.p1) * Math.cos(dy * 2.1 + sh.p2) + 0.2 * Math.sin(dz * 3.3 + sh.p3 + dx * 1.5);
  const rad = (0.42 + 0.58 * Math.cbrt(r())) * lump * 0.78;
  return [dx * rad * sh.ax, dy * rad * sh.ay, dz * rad * sh.az];
}

function kNearest(V, M, cell, k, out) {  // short wireframe edges between near neighbours
  const grid = new Map(), keyOf = (a, b, c) => a + "," + b + "," + c;
  for (let i = 0; i < M; i++) {
    const key = keyOf(Math.floor(V[3 * i] / cell), Math.floor(V[3 * i + 1] / cell), Math.floor(V[3 * i + 2] / cell));
    if (!grid.has(key)) grid.set(key, []);
    grid.get(key).push(i);
  }
  const seen = new Set();
  for (let i = 0; i < M; i++) {
    const cx = Math.floor(V[3 * i] / cell), cy = Math.floor(V[3 * i + 1] / cell), cz = Math.floor(V[3 * i + 2] / cell);
    const cand = [];
    for (let a = -1; a <= 1; a++) for (let b = -1; b <= 1; b++) for (let c = -1; c <= 1; c++) {
      const list = grid.get(keyOf(cx + a, cy + b, cz + c)); if (!list) continue;
      for (const j of list) {
        if (j === i) continue;
        const dx = V[3 * i] - V[3 * j], dy = V[3 * i + 1] - V[3 * j + 1], dz = V[3 * i + 2] - V[3 * j + 2];
        cand.push([dx * dx + dy * dy + dz * dz, j]);
      }
    }
    cand.sort((p, q) => p[0] - q[0]);
    for (let n = 0; n < Math.min(k, cand.length); n++) {
      const j = cand[n][1], lo = Math.min(i, j), hi = Math.max(i, j), id = lo * 100003 + hi;
      if (!seen.has(id)) { seen.add(id); out.push(lo, hi); }
    }
  }
}

function buildScene() {
  const prev = new Map(scene.clusters.map((c) => [c.key, c.c0 || c.c]));
  const vis = (n) => view.layers[n.layer] && (n.kind !== "claim" || view.layers.claims);
  const nodes = view.kbNodes.filter(vis).concat(view.memNodes);
  const ids = new Map(nodes.map((n) => [n.id, n]));
  const edges = [];
  for (const e of view.kbEdges.concat(view.memEdges)) {
    const a = ids.get(e.src), b = ids.get(e.dst);
    if (a && b) edges.push([a, b, e.rel]);
  }
  // which cluster does each record live in
  const folderCount = new Map();
  for (const n of nodes) if (n.kind === "note") folderCount.set(n.folder || "root", (folderCount.get(n.folder || "root") || 0) + 1);
  const topFolders = new Set([...folderCount].sort((a, b) => b[1] - a[1]).slice(0, FOLDER_KEEP).map(([f]) => f));
  const defs = new Map();
  const place = (n, key, label, group) => {
    if (!defs.has(key)) defs.set(key, { key, label, group, nodes: [] });
    defs.get(key).nodes.push(n);
  };
  for (const n of nodes) {
    if (n.kind === "mem") {
      const g = n.mkind === "tag" ? "tags" : n.group;
      place(n, "mem:" + g, "MEMORY · " + (MEM_NAME[g] || String(g).toUpperCase()), "mem");
    } else if (n.kind === "source") place(n, "kb:sources", "SOURCES", "kb");
    else if (n.kind === "claim") place(n, "kb:claims", "CLAIMS", "kb");
    else if (n.kind === "note") {
      const f = n.folder || "root", fk = topFolders.has(f) ? f : "other";
      place(n, "v:" + fk, "VAULT · " + fk.toUpperCase(), "vault");
    } else place(n, "kb:pages", "PAGES", "kb");
  }
  const order = { mem: 0, kb: 1, vault: 2 };
  let clusters = [...defs.values()].sort((a, b) => order[a.group] - order[b.group] || b.nodes.length - a.nodes.length);
  clusters = clusters.slice(0, MAXC);
  const keep = new Set(clusters.map((c) => c.key));
  clusters.forEach((c, i) => {
    c.i = i; c.n = c.nodes.length; c.R = clusterR(c.n);
    c.base = PINKS[hash(c.key) % PINKS.length];
    c.recent = 0; c.conflicts = 0; c.running = 0;
    const r = rng(hash(c.key + "shape"));
    c.shape = { ax: 0.85 + r() * 0.5, ay: 0.7 + r() * 0.35, az: 0.85 + r() * 0.5, p1: r() * 6, p2: r() * 6, p3: r() * 6 };
  });
  const cOf = new Map();
  for (const c of clusters) for (const n of c.nodes) { cOf.set(n.id, c); n.ci = c.i; }
  for (const n of nodes) if (!cOf.has(n.id)) n.ci = -1;

  // real strands: edges between records of different clusters, bundled per cluster pair
  const bundles = new Map();
  const degree = new Map();
  for (const [a, b, rel] of edges) {
    degree.set(a.id, (degree.get(a.id) || 0) + 1); degree.set(b.id, (degree.get(b.id) || 0) + 1);
    const ca = cOf.get(a.id), cb = cOf.get(b.id);
    if (!ca || !cb || ca === cb) continue;
    const key = Math.min(ca.i, cb.i) + "|" + Math.max(ca.i, cb.i);
    if (!bundles.has(key)) bundles.set(key, { a: Math.min(ca.i, cb.i), b: Math.max(ca.i, cb.i), edges: [], rels: new Map() });
    const bd = bundles.get(key);
    bd.edges.push([a, b]); bd.rels.set(rel, (bd.rels.get(rel) || 0) + 1);
  }

  // cluster centres: a small 3D force layout, seeded from the previous layout so it stays put
  const K = clusters.length;
  clusters.forEach((c, i) => {
    if (prev.has(c.key)) { c.c = prev.get(c.key).slice(); return; }
    const phi = Math.acos(1 - 2 * (i + 0.5) / Math.max(1, K)), th = Math.PI * (1 + Math.sqrt(5)) * i;
    const rad = 0.5 + 0.35 * K ** 0.5;
    c.c = [rad * Math.sin(phi) * Math.cos(th) * 1.25, rad * Math.cos(phi) * 0.95, rad * Math.sin(phi) * Math.sin(th)];
  });
  for (let it = 0; it < 220; it++) {
    const cool = 1 - it / 220;
    for (let i = 0; i < K; i++) for (let j = i + 1; j < K; j++) {
      const a = clusters[i], b = clusters[j];
      const dx = b.c[0] - a.c[0], dy = b.c[1] - a.c[1], dz = b.c[2] - a.c[2], d = Math.hypot(dx, dy, dz) || 1e-3;
      const want = (a.R + b.R) * 1.3 + 0.1, bd = bundles.get(i + "|" + j);
      let f = d < want ? (want - d) * 0.35 : 0;   // push apart
      if (bd) f -= Math.max(0, d - want * 1.3) * 0.05 * Math.min(1, Math.log1p(bd.edges.length) / 4);  // real links pull together
      const ux = dx / d, uy = dy / d, uz = dz / d;
      a.c[0] -= ux * f * cool; a.c[1] -= uy * f * cool; a.c[2] -= uz * f * cool;
      b.c[0] += ux * f * cool; b.c[1] += uy * f * cool; b.c[2] += uz * f * cool;
    }
    for (const c of clusters) { c.c[0] *= 0.995; c.c[1] *= 0.985; c.c[2] *= 0.99; }
  }
  let cx = 0, cy = 0, cz = 0;
  for (const c of clusters) { cx += c.c[0]; cy += c.c[1]; cz += c.c[2]; }
  for (const c of clusters) { c.c[0] -= cx / (K || 1); c.c[1] -= cy / (K || 1); c.c[2] -= cz / (K || 1); }
  tall = H > W;   // a wide scene suits a wide stage, a tall one a phone
  for (const c of clusters) { c.c0 = c.c.slice(); c.c[1] *= tall ? 1.7 : 0.62; }
  let extent = 0.4;
  let extXZ = 0.4, extY = 0.2;
  for (const c of clusters) {
    extent = Math.max(extent, Math.hypot(...c.c) + c.R * 1.15);
    extXZ = Math.max(extXZ, Math.hypot(c.c[0], c.c[2]) + c.R * 1.1); extY = Math.max(extY, Math.abs(c.c[1]) + c.R * 1.1);
  }

  // blobs: real records first, then fuzz, joined to their near neighbours
  const mesh = [], gNodes = [];
  let gi = 0;
  for (const c of clusters) {
    const M = clamp(Math.round(c.n * 4) + 220, 260, 1100);
    const V = new Float32Array(M * 3), seeds = new Float32Array(M), gidx = new Int32Array(M).fill(-1);
    for (let v = 0; v < M; v++) {
      const real = v < c.n ? c.nodes[v] : null;
      const r = rng(hash(real ? real.id : c.key + "#" + v));
      const p = blobPoint(r, c.shape);
      V[3 * v] = c.c[0] + p[0] * c.R; V[3 * v + 1] = c.c[1] + p[1] * c.R; V[3 * v + 2] = c.c[2] + p[2] * c.R;
      seeds[v] = r();
    }
    c.M = M; c.V = V; c.seeds = seeds;
    c.nodes.forEach((n, v) => {
      const r = rng(hash(n.id + "x")), p = v < M ? null : blobPoint(r, c.shape);
      n.vi = v < M ? v : -1;
      if (p) { n.x = c.c[0] + p[0] * c.R; n.y = c.c[1] + p[1] * c.R; n.z = c.c[2] + p[2] * c.R; n.seed = r(); }
      else { n.x = V[3 * v]; n.y = V[3 * v + 1]; n.z = V[3 * v + 2]; n.seed = seeds[v]; }
      n.gi = gi++; if (v < M) gidx[v] = n.gi;
      gNodes.push(n);
    });
    const spacing = c.R * Math.cbrt(2.9 / M), pairs = [];
    kNearest(V, M, spacing * 1.7, 4, pairs);
    const wr = rng(hash(c.key + "w"));
    for (let e = 0; e < pairs.length; e += 2) {
      const i = pairs[e], j = pairs[e + 1];
      mesh.push(V[3 * i], V[3 * i + 1], V[3 * i + 2], seeds[i], V[3 * j], V[3 * j + 1], V[3 * j + 2], seeds[j],
        c.i, c.i, 0, wr(), gidx[i], gidx[j]);
    }
  }

  // strands: every real link (capped per bundle), plus a faint ambient tangle between all clusters
  const strands = [], bundleList = [];
  const sr = rng(7);
  const strand = (a, b, kind, w) => strands.push(a.x, a.y, a.z, a.seed, b.x, b.y, b.z, b.seed, a.ci, b.ci, kind, w, a.gi, b.gi);
  let links = 0;
  for (const bd of bundles.values()) {
    links += bd.edges.length;
    const step = Math.max(1, Math.ceil(bd.edges.length / 150));
    for (let e = 0; e < bd.edges.length; e += step) strand(bd.edges[e][0], bd.edges[e][1], 1, 0.55 + sr() * 0.45);
    const top = [...bd.rels].sort((p, q) => q[1] - p[1])[0];
    bundleList.push({ a: bd.a, b: bd.b, n: bd.edges.length, rel: top[0], rels: bd.rels, seed: sr() });
  }
  const pseudo = (c, v) => ({ x: c.V[3 * v], y: c.V[3 * v + 1], z: c.V[3 * v + 2], seed: c.seeds[v], ci: c.i, gi: -1 });
  for (let i = 0; i < K; i++) for (let j = i + 1; j < K; j++) {
    const a = clusters[i], b = clusters[j];
    const count = clamp(Math.round(0.5 * Math.sqrt(a.M * b.M)), 6, 70);
    for (let s = 0; s < count; s++) strand(pseudo(a, Math.floor(sr() * a.M)), pseudo(b, Math.floor(sr() * b.M)), 2, 0.2 + sr() * 0.4);
  }

  // per-record stats the scenarios read
  const now = Date.now(), cutoff = now - 36 * 3600 * 1000;
  for (const n of gNodes) {
    n.recent = isRecent(n, now, cutoff);
    if (n.recent) clusters[n.ci].recent++;
    if (n.flag === "conflict") clusters[n.ci].conflicts++;
  }
  const stats = { total: gNodes.length, mem: gNodes.filter((n) => n.kind === "mem").length,
    sources: gNodes.filter((n) => n.kind === "source").length, pages: gNodes.filter((n) => n.kind === "page" || n.kind === "answer").length,
    claims: gNodes.filter((n) => n.kind === "claim").length, conflicts: gNodes.filter((n) => n.flag === "conflict").length,
    recent: gNodes.filter((n) => n.recent).length, links, dialogue: (bundleList.find((b) => clusters[b.a].key === "mem:message" && clusters[b.b].key === "mem:reply" || clusters[b.a].key === "mem:reply" && clusters[b.b].key === "mem:message") || { rels: new Map() }).rels.get("answers") || 0 };

  scene = { clusters, nodes: gNodes, mesh: new Float32Array(mesh), meshN: mesh.length / 14, strands: new Float32Array(strands),
    strandN: strands.length / 14, bundles: bundleList, extent, extXZ, extY, stats, edges, degree };
  view.nodes = gNodes; view.edges = edges;
  view.byId = ids;
  if (view.selected) view.selected = ids.get(view.selected.id) || null;
  applyScenario(view.scn, true);
  renderer.upload(); view.dirty = true;
}

function isRecent(n, now, cutoff) {
  if (n.kind === "mem") { const t = Date.parse(n.created_at); return t > now - 24 * 3600 * 1000; }
  const d = n.added || n.confirmed || n.updated;
  if (!d) return false;
  const t = Date.parse(String(d).slice(0, 10) + "T00:00:00");
  return t >= cutoff - 24 * 3600 * 1000;
}

/* ---------- scenarios: highlight filters over the graph ---------- */
const SCENARIOS = [
  { id: "all", label: "ALL", count: (s) => s.total },
  { id: "memory", label: "MEMORY", count: (s) => s.mem },
  { id: "dialogue", label: "DIALOGUE", count: (s) => s.dialogue },
  { id: "sources", label: "SOURCES", count: (s) => s.sources },
  { id: "pages", label: "PAGES", count: (s) => s.pages },
  { id: "claims", label: "CLAIMS", count: (s) => s.claims },
  { id: "conflicts", label: "CONFLICTS", count: (s) => s.conflicts },
  { id: "recent", label: "LAST 24 H", count: (s) => s.recent },
  { id: "links", label: "LINKS", count: (s) => s.links },
];

function specFor(id) {
  const dimmed = (d) => ({ dim: d, amt: 0, lit: 0, red: 0, tc: WHITE });
  const focus = (tc, amt, red = 0) => ({ dim: 1, amt, lit: 1, red, tc });
  const keyIn = (c, list) => list.includes(c.key);
  const spec = { cl: () => dimmed(1), node: () => ({ hot: 0, dim: 1 }), mode: 0, ambient: 1, note: "" };
  const st = scene.stats;
  if (id === "memory") {
    spec.cl = (c) => (c.group === "mem" ? focus(CYAN, 0.42) : dimmed(0.22));
    spec.node = (n) => ({ hot: 0, dim: n.layer === "mem" ? 1 : 0.3 });
    spec.note = `MEMORY · ${scene.clusters.filter((c) => c.group === "mem").length} clusters · ${st.mem} items`;
  } else if (id === "dialogue") {
    const hot = new Set();
    for (const [a, b, rel] of scene.edges || []) if (rel === "answers") { hot.add(a.id); hot.add(b.id); }
    spec.cl = (c) => (keyIn(c, ["mem:message", "mem:reply"]) ? focus(WHITE, 0.3) : dimmed(0.22));
    spec.node = (n) => ({ hot: hot.has(n.id) ? 1 : 0, dim: hot.has(n.id) ? 1 : 0.35 });
    spec.note = `DIALOGUE · ${st.dialogue} owner → reply links`;
  } else if (id === "sources" || id === "pages" || id === "claims") {
    const key = "kb:" + id, have = scene.clusters.some((c) => c.key === key);
    const kinds = { sources: ["source"], pages: ["page", "answer"], claims: ["claim"] }[id];
    spec.cl = (c) => (!have ? dimmed(0.45) : c.key === key ? focus(CYAN, 0.5) : dimmed(0.3));
    spec.node = (n) => (!have ? { hot: 0, dim: 0.6 } : { hot: 0, dim: kinds.includes(n.kind) ? 1 : 0.35 });
    spec.mode = 1;
    spec.note = have ? `${id.toUpperCase()} · ${scene.clusters.find((c) => c.key === key).n} records · strands touching them lit`
      : `${id.toUpperCase()} · none yet — the knowledge engine is empty (type ingest https://… or sync)`;
  } else if (id === "conflicts") {
    spec.cl = (c) => (c.conflicts ? focus(RED, 0.8, 1) : dimmed(0.25));
    spec.node = (n) => ({ hot: n.flag === "conflict" ? 1 : 0, dim: n.flag === "conflict" ? 1 : 0.35 });
    spec.mode = 1;
    spec.note = st.conflicts ? `CONFLICTS · ${st.conflicts} claims disagree — review in the inspector` : "CONFLICTS · none — nothing in the graph disagrees";
  } else if (id === "recent") {
    spec.cl = (c) => (c.recent ? focus(AMBER, clamp(0.25 + 0.6 * c.recent / c.n, 0.25, 0.7)) : dimmed(0.25));
    spec.node = (n) => ({ hot: n.recent ? 1 : 0, dim: n.recent ? 1 : 0.35 });
    spec.note = `LAST 24 H · ${st.recent} records touched · ${scene.clusters.filter((c) => c.recent).length} clusters`;
  } else if (id === "links") {
    spec.cl = () => ({ dim: 0.4, amt: 0, lit: 1, red: 0, tc: WHITE });
    spec.node = () => ({ hot: 0, dim: 0.55 });
    spec.ambient = 0.08;
    spec.note = `LINKS · ${st.links} real links between ${scene.bundles.length} cluster pairs`;
  } else if (id.startsWith("c:")) {
    const key = id.slice(2), c0 = scene.clusters.find((c) => c.key === key);
    spec.cl = (c) => (c.key === key ? focus(CYAN, 0.5) : dimmed(0.28));
    spec.node = (n) => ({ hot: 0, dim: scene.clusters[n.ci] && scene.clusters[n.ci].key === key ? 1 : 0.35 });
    spec.mode = 1;
    spec.note = c0 ? `${c0.label} · ${c0.n} records` : "";
  } else if (view.layers && !scene.clusters.some((c) => c.group === "kb")) {
    spec.note = "KNOWLEDGE ENGINE EMPTY · showing JARVIS memory · type sync, ingest https://… or note …";
  }
  return spec;
}

// animated state: what the GPU reads, easing toward the scenario's targets
const ST = { cur: new Float32Array(MAXC * 4), tgt: new Float32Array(MAXC * 4), tc: new Float32Array(MAXC * 3), tct: new Float32Array(MAXC * 3),
  base: new Float32Array(MAXC * 3), cc: new Float32Array(MAXC * 4), mode: 0, ambient: 1, ambientT: 1, nodeHot: new Float32Array(0), nodeDim: new Float32Array(0),
  nodeHotT: new Float32Array(0), nodeDimT: new Float32Array(0), busy: true };

function applyScenario(id, snap) {
  view.scn = id;
  const spec = specFor(id);
  for (const c of scene.clusters) {
    const s = spec.cl(c), i = c.i;
    ST.tgt.set([s.dim, s.amt, s.lit, s.red], i * 4); ST.tct.set(s.tc, i * 3);
    ST.base.set(c.base, i * 3); ST.cc.set([c.c[0], c.c[1], c.c[2], c.R], i * 4);
    if (snap) { ST.cur.set([s.dim, s.amt, s.lit, s.red], i * 4); ST.tc.set(s.tc, i * 3); }
  }
  for (let i = scene.clusters.length; i < MAXC; i++) { ST.tgt.set([1, 0, 0, 0], i * 4); ST.cc.set([0, 0, 0, 1], i * 4); }
  const N = scene.nodes.length;
  if (ST.nodeHot.length !== N) { ST.nodeHot = new Float32Array(N); ST.nodeDim = new Float32Array(N).fill(1); snap = true; }
  ST.nodeHotT = new Float32Array(N); ST.nodeDimT = new Float32Array(N);
  scene.nodes.forEach((n, k) => { const s = spec.node(n); ST.nodeHotT[k] = s.hot; ST.nodeDimT[k] = s.dim; });
  if (snap) { ST.nodeHot.set(ST.nodeHotT); ST.nodeDim.set(ST.nodeDimT); ST.ambient = spec.ambient; }
  ST.mode = spec.mode; ST.ambientT = spec.ambient; ST.busy = true;
  view.activeUntil = performance.now() + 1500; view.dirty = true;
  renderer.uploadDyn();
  $("scn-note").textContent = spec.note + (id !== "all" ? "  ·  Esc clears" : "");
  $("scn-note").classList.toggle("on", id !== "all");
  document.querySelectorAll("#scenarios button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.scn === id)));
  renderMix();
}

function easeState(dt) {
  const k = reduced ? 1 : 1 - Math.exp(-dt * 8);
  let busy = false;
  const mix = (a, b) => { const d = b - a; if (Math.abs(d) > 0.002) { busy = true; return a + d * k; } return b; };
  for (let i = 0; i < MAXC * 4; i++) ST.cur[i] = mix(ST.cur[i], ST.tgt[i]);
  for (let i = 0; i < MAXC * 3; i++) ST.tc[i] = mix(ST.tc[i], ST.tct[i]);
  ST.ambient = mix(ST.ambient, ST.ambientT);
  let nodeChanged = false;
  for (let i = 0; i < ST.nodeHot.length; i++) {
    const h = mix(ST.nodeHot[i], ST.nodeHotT[i]), d = mix(ST.nodeDim[i], ST.nodeDimT[i]);
    if (h !== ST.nodeHot[i] || d !== ST.nodeDim[i]) nodeChanged = true;
    ST.nodeHot[i] = h; ST.nodeDim[i] = d;
  }
  ST.busy = busy;
  return nodeChanged;
}

function renderTabs() {
  const s = scene.stats;
  $("scenarios").innerHTML = SCENARIOS.map((sc) => {
    const n = sc.count(s) || 0;
    return `<button type="button" data-scn="${sc.id}" aria-pressed="${view.scn === sc.id}" class="${n ? "" : "zero"}">${sc.label}<em>${n.toLocaleString()}</em></button>`;
  }).join("");
}
$("scenarios").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-scn]");
  if (!b) return;
  applyScenario(view.scn === b.dataset.scn && b.dataset.scn !== "all" ? "all" : b.dataset.scn);
});

/* ---------- camera ---------- */
let W = 1, H = 1, DPR = 1, tall = false;
const camS = { ca: 1, sa: 0, ce: 1, se: 0, S: 1, D: 1, rot: new Float32Array(9) };
function camUpdate() {
  const c = camS;
  c.ca = Math.cos(cam.az); c.sa = Math.sin(cam.az); c.ce = Math.cos(cam.el); c.se = Math.sin(cam.el);
  c.rot.set([c.ca, -c.se * c.sa, c.ce * c.sa, 0, c.ce, c.se, -c.sa, -c.se * c.ca, c.ce * c.ca]);
  c.S = Math.min((W * 0.46) / (scene.extXZ || 1), (H * 0.5) / ((scene.extY || 1) + 0.15 * (scene.extXZ || 1))) * cam.zoom * (tall ? 1.2 : 1); c.D = scene.extent * 3.2;
}
function project(x, y, z, o) {  // same maths as the shader
  const c = camS;
  const x1 = x * c.ca - z * c.sa, z1 = x * c.sa + z * c.ca;
  const y2 = y * c.ce - z1 * c.se, z2 = y * c.se + z1 * c.ce;
  const f = c.D / Math.max(c.D - z2, 0.15 * c.D);
  o.sx = W / 2 + cam.px + x1 * c.S * f; o.sy = H / 2 + cam.py + y2 * c.S * f; o.depth = z2; o.f = f;
  return o;
}
let timeNow = 0;
// one scratch for the per-node breathe: the hot loop must not allocate a point per note per frame
const wobPt = { x: 0, y: 0, z: 0 };
const ovPt = { sx: 0, sy: 0, depth: 0, f: 1 };
function wobJ(n, R) {  // the shader's breathing, so labels and picks sit on the dots
  const k = reduced ? 0 : R * 0.035, s = n.seed;
  wobPt.x = n.x + k * Math.sin(timeNow * 0.5 + s * 40);
  wobPt.y = n.y + k * Math.sin(timeNow * 0.43 + s * 71 + 1);
  wobPt.z = n.z + k * Math.sin(timeNow * 0.37 + s * 23 + 2);
  return wobPt;
}
function projectAll() {
  camUpdate();
  for (const c of scene.clusters) project(c.c[0], c.c[1], c.c[2], c);
  for (const n of scene.nodes) {
    const p = wobJ(n, scene.clusters[n.ci].R);
    project(p.x, p.y, p.z, n);
  }
}

/* ---------- WebGL2 renderer ---------- */
const canvas = $("graph"), labels = $("graph-labels"), octx = labels.getContext("2d");
const COMMON = `#version 300 es
precision highp float;
uniform mat3 u_rot; uniform vec2 u_res, u_pan; uniform float u_S, u_D, u_time, u_breath, u_zoom;
uniform vec4 u_cc[${MAXC}]; uniform vec4 u_st[${MAXC}]; uniform vec3 u_tc[${MAXC}]; uniform vec3 u_base[${MAXC}];
vec3 wob(vec3 p, float seed, float R) {
  float k = R * .035 * u_breath;
  return p + k * vec3(sin(u_time * .5 + seed * 40.), sin(u_time * .43 + seed * 71. + 1.), sin(u_time * .37 + seed * 23. + 2.));
}
vec4 proj(vec3 p) {
  vec3 r = u_rot * p;
  float f = u_D / max(u_D - r.z, .15 * u_D);
  return vec4(u_res * .5 + u_pan + r.xy * u_S * f, r.z, f);
}
vec3 bez3(vec3 a, vec3 c, vec3 b, float t) { float u = 1. - t; return u * u * a + 2. * u * t * c + t * t * b; }
`;
const EDGE_VS = COMMON + `
layout(location=0) in vec4 a_p0; layout(location=1) in vec4 a_p1; layout(location=2) in vec4 a_m; layout(location=3) in vec2 a_id;
uniform float u_width, u_segs, u_hover, u_sel, u_mode, u_ambient, u_alpha;
out vec4 v_col; out vec3 v_x; out float v_side;
void main() {
  int seg = gl_VertexID / 6, k = gl_VertexID % 6;
  float cx = (k == 1 || k == 4 || k == 5) ? 1. : 0.;
  float cy = (k == 2 || k == 3 || k == 5) ? 1. : -1.;
  float t = (float(seg) + cx) / u_segs;
  int cA = int(a_m.x + .5), cB = int(a_m.y + .5);
  float kind = a_m.z, w = a_m.w;
  vec3 A = wob(a_p0.xyz, a_p0.w, u_cc[cA].w), B = wob(a_p1.xyz, a_p1.w, u_cc[cB].w);
  vec3 c = (A + B) * .5;
  if (kind > .5) {
    vec3 d = B - A, ref = vec3(sin(a_p0.w * 91.), 1., cos(a_p1.w * 53.));
    vec3 n = normalize(cross(d, ref) + 1e-5);
    c += n * length(d) * (fract(a_p0.w * 7.31) - .5) * .5;
  }
  vec4 P = proj(bez3(A, c, B, t));
  vec2 tang = normalize(proj(bez3(A, c, B, min(1., t + .02))).xy - proj(bez3(A, c, B, max(0., t - .02))).xy + 1e-5);
  float hot = (u_hover >= 0. && (a_id.x == u_hover || a_id.y == u_hover)) || (u_sel >= 0. && (a_id.x == u_sel || a_id.y == u_sel)) ? 1. : 0.;
  float wpx = u_width * (kind < .5 ? .55 : .7 + w * .7) * (1. + hot * .9);
  vec2 s = P.xy + vec2(-tang.y, tang.x) * cy * wpx;
  gl_Position = vec4(s / u_res * 2. - 1., 0., 1.); gl_Position.y = -gl_Position.y;
  vec4 sA = u_st[cA], sB = u_st[cB];
  vec3 col; float a;
  if (kind < .5) {
    vec3 rm = u_rot * ((A + B) * .5 - u_cc[cA].xyz);
    float R = u_cc[cA].w, rim = smoothstep(.5, .98, length(rm.xy) / R), dz = rm.z / R;
    col = mix(u_base[cA], vec3(.37, .83, .9), rim * .8);
    col = mix(col, u_tc[cA], sA.y);
    a = u_alpha * (.3 + .95 * w) * sA.x * (.4 + .6 * smoothstep(-1.2, 1.2, dz)) * (1. + rim * .7);
  } else {
    float lit = u_mode > .5 ? max(sA.z, sB.z) : min(sA.z, sB.z);
    vec3 tint = (sA.w + sB.w > .5) ? vec3(1., .3, .37) : vec3(.94, .71, .16);
    col = mix(vec3(.9), tint, lit);
    float base = kind < 1.5 ? .12 + .14 * w : (.03 + .035 * w) * u_ambient;
    a = base * mix(min(sA.x, sB.x), 3.4, lit);
  }
  a *= 1. + hot * 2.4; col = mix(col, vec3(1.), hot * .6);
  v_col = vec4(col, a); v_x = vec3(t, a_p0.w, kind); v_side = cy;
}`;
const EDGE_FS = `#version 300 es
precision highp float;
in vec4 v_col; in vec3 v_x; in float v_side; uniform float u_time; out vec4 o;
void main() {
  float e = 1. - abs(v_side); e *= e;
  float a = v_col.a * e;
  if (v_x.z > .5 && v_x.z < 1.5) {   // a pulse running along a real link
    float ph = fract(u_time * (.03 + .07 * fract(v_x.y * 3.7)) + v_x.y);
    a += v_col.a * exp(-pow((v_x.x - ph) * 14., 2.)) * 2.2 * e * step(.55, fract(v_x.y * 11.3));
  }
  o = vec4(v_col.rgb * a, a);
}`;
const NODE_VS = COMMON + `
layout(location=0) in vec4 a_n; layout(location=1) in vec4 a_nm; layout(location=2) in vec2 a_dyn;
uniform float u_hover, u_sel;
out vec2 v_uv; out vec4 v_col; out float v_hub, v_hot;
void main() {
  int k = gl_VertexID % 6;
  vec2 q = vec2((k == 1 || k == 4 || k == 5) ? 1. : -1., (k == 2 || k == 3 || k == 5) ? 1. : -1.);
  int cl = int(a_nm.x + .5);
  vec4 P = proj(wob(a_n.xyz, a_n.w, u_cc[cl].w));
  float id = float(gl_InstanceID);
  float hot = (id == u_hover || id == u_sel) ? 1. : 0.;
  float r = a_nm.y * clamp(P.w * pow(u_zoom, .35), .6, 3.2) * (1. + hot * 1.4) * (1. + .12 * sin(u_time * 1.7 + a_n.w * 30.));
  vec2 s = P.xy + q * r * 2.2;
  gl_Position = vec4(s / u_res * 2. - 1., 0., 1.); gl_Position.y = -gl_Position.y;
  float code = a_nm.w;
  vec3 col = vec3(.8, .93, 1.);
  if (code > .5 && code < 1.5) col = vec3(1.);
  else if (code < 2.5 && code > 1.5) col = vec3(.37, .83, .9);
  else if (code < 3.5 && code > 2.5) col = vec3(.96, .78, .3);
  else if (code < 4.5 && code > 3.5) col = vec3(.97);
  else if (code < 5.5 && code > 4.5) col = vec3(.78, .84, .95);
  else if (code < 6.5 && code > 5.5) col = vec3(.7, .8, 1.);
  else if (code > 6.5) col = vec3(1., .3, .37);
  col = mix(col, vec3(.94, .71, .16), a_dyn.x * .85);
  col = mix(col, vec3(1.), hot);
  float dim = u_st[cl].x * a_dyn.y;
  float depth = .45 + .55 * smoothstep(-1.2, 1.2, P.z / max(u_cc[cl].w, .1) * .5);
  v_col = vec4(col, dim * depth * (1. + a_dyn.x * .6));
  v_uv = q * 1.; v_hub = a_nm.z; v_hot = hot;
}`;
const NODE_FS = `#version 300 es
precision highp float;
in vec2 v_uv; in vec4 v_col; in float v_hub, v_hot; out vec4 o;
void main() {
  float d = length(v_uv) * 2.2;
  float core = smoothstep(1., .35, d);
  float glow = exp(-d * d * .9) * .3;
  float ring = v_hub * smoothstep(.12, 0., abs(d - 1.9)) * .8;
  float a = (core * .8 + glow + ring) * v_col.a * step(d, 2.2);
  o = vec4(v_col.rgb * a, a);
}`;

const FULL_VS = `#version 300 es
out vec2 v; void main() { vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2); v = p; gl_Position = vec4(p * 2. - 1., 0., 1.); }`;
const BRIGHT_FS = `#version 300 es
precision highp float; in vec2 v; uniform sampler2D u_tex; uniform vec2 u_px; out vec4 o;
void main() {  // 4-tap downsample, keep only what is bright enough to glow
  vec3 c = .25 * (texture(u_tex, v + u_px * vec2(-1, -1)).rgb + texture(u_tex, v + u_px * vec2(1, -1)).rgb
                + texture(u_tex, v + u_px * vec2(-1, 1)).rgb + texture(u_tex, v + u_px * vec2(1, 1)).rgb);
  float l = max(c.r, max(c.g, c.b));
  o = vec4(c * smoothstep(.18, .9, l), 1.);
}`;
const BLUR_FS = `#version 300 es
precision highp float; in vec2 v; uniform sampler2D u_tex; uniform vec2 u_dir; out vec4 o;
void main() {
  vec3 c = texture(u_tex, v).rgb * .227;
  c += (texture(u_tex, v + u_dir * 1.385).rgb + texture(u_tex, v - u_dir * 1.385).rgb) * .316;
  c += (texture(u_tex, v + u_dir * 3.231).rgb + texture(u_tex, v - u_dir * 3.231).rgb) * .070;
  o = vec4(c, 1.);
}`;
const COMP_FS = `#version 300 es
precision highp float; in vec2 v; uniform sampler2D u_scene, u_bloom; uniform float u_amt; out vec4 o;
void main() { o = vec4(texture(u_scene, v).rgb + texture(u_bloom, v).rgb * u_amt, 1.); }`;

const renderer = (() => {
  let gl = null, progs = null, bufs = [], mesh = null, strand = null, nodesVao = null, dynBuf = null, ok = false;
  let post = null;  // {ms, msRb, scene, sceneTex, a, aTex, b, bTex, w, h, hw, hh}
  const endA = {}, endB = {}, tintCol = [0, 0, 0];  // 2D fallback scratches: reused across every edge
  const tex = (w, h) => {
    const t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  };
  const fbo = (t) => { const f = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, f); gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0); return f; };
  function setupPost(w, h) {
    if (!ok) return;
    if (post) {
      for (const k of ["sceneTex", "aTex", "bTex"]) gl.deleteTexture(post[k]);
      for (const k of ["ms", "scene", "a", "b"]) gl.deleteFramebuffer(post[k]);
      gl.deleteRenderbuffer(post.msRb);
    }
    const hw = Math.max(1, w >> 1), hh = Math.max(1, h >> 1);
    const msRb = gl.createRenderbuffer(); gl.bindRenderbuffer(gl.RENDERBUFFER, msRb);
    gl.renderbufferStorageMultisample(gl.RENDERBUFFER, Math.min(4, gl.getParameter(gl.MAX_SAMPLES)), gl.RGBA8, w, h);
    const ms = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, ms);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, msRb);
    const sceneTex = tex(w, h), scene = fbo(sceneTex), aTex = tex(hw, hh), a = fbo(aTex), bTex = tex(hw, hh), b = fbo(bTex);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    post = { ms, msRb, scene, sceneTex, a, aTex, b, bTex, w, h, hw, hh };
  }
  const program = (vs, fs) => {
    const p = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
      const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      gl.attachShader(p, s);
    }
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const u = new Proxy({}, { get: (t, name) => (name in t ? t[name] : (t[name] = gl.getUniformLocation(p, name))) });
    return { p, u };
  };
  const buf = (data, usage = gl.STATIC_DRAW) => { const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, usage); bufs.push(b); return b; };
  const attr = (loc, size, stride, off, div = 1) => { gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, off); gl.vertexAttribDivisor(loc, div); };
  const edgeVao = (data) => {
    const v = gl.createVertexArray(); gl.bindVertexArray(v); buf(data);
    attr(0, 4, 56, 0); attr(1, 4, 56, 16); attr(2, 4, 56, 32); attr(3, 2, 56, 48);
    gl.bindVertexArray(null); return v;
  };

  try {
    gl = canvas.getContext("webgl2", { alpha: false, antialias: true, premultipliedAlpha: true });
    if (gl) {
      progs = { edge: program(EDGE_VS, EDGE_FS), node: program(NODE_VS, NODE_FS),
                bright: program(FULL_VS, BRIGHT_FS), blur: program(FULL_VS, BLUR_FS), comp: program(FULL_VS, COMP_FS) };
      ok = true;
    }
  } catch (err) { console.error("knowledge graph: WebGL2 unavailable, using 2D", err); gl = null; ok = false; }
  const ctx2 = ok ? null : canvas.getContext("2d");

  const KIND = { mem: 0, source: 3, page: 4, answer: 4, claim: 5, note: 6 };
  function upload() {
    if (!ok) return;
    for (const b of bufs) gl.deleteBuffer(b);
    bufs = [];
    mesh = scene.meshN ? edgeVao(scene.mesh) : null;
    strand = scene.strandN ? edgeVao(scene.strands) : null;
    const N = scene.nodes.length, data = new Float32Array(N * 8);
    scene.nodes.forEach((n, i) => {
      let size = 1.7, code = KIND[n.kind] ?? 4, hub = 0;
      if (n.kind === "mem") {
        if (n.mkind === "kind") { size = 6.5; hub = 1; code = 1; }
        else if (n.mkind === "tag") { size = 3.6; code = 2; }
        else size = 1.1 + Math.min(1.6, (n.importance || 0.3) * 2);
      } else if (n.kind === "source") size = 3.6;
      else if (n.kind === "page" || n.kind === "answer") { size = 3.5 + Math.sqrt(n.claims || 1) * 0.6; hub = 1; }
      else if (n.kind === "note") size = 1.6 + Math.sqrt(n.degree || 0) * 0.5;
      else if (n.kind === "claim") { size = 1.3; if (n.flag === "conflict") code = 7; }
      data.set([n.x, n.y, n.z, n.seed, n.ci, size, hub, code], i * 8);
      n.size = size;
    });
    nodesVao = gl.createVertexArray(); gl.bindVertexArray(nodesVao);
    buf(data); attr(0, 4, 32, 0); attr(1, 4, 32, 16);
    dynBuf = buf(new Float32Array(N * 2), gl.DYNAMIC_DRAW); attr(2, 2, 8, 0);
    gl.bindVertexArray(null);
    uploadDyn();
  }
  function uploadDyn() {
    if (!ok || !dynBuf) return;
    const N = ST.nodeHot.length, d = new Float32Array(N * 2);
    for (let i = 0; i < N; i++) { d[2 * i] = ST.nodeHot[i]; d[2 * i + 1] = ST.nodeDim[i]; }
    gl.bindBuffer(gl.ARRAY_BUFFER, dynBuf); gl.bufferData(gl.ARRAY_BUFFER, d, gl.DYNAMIC_DRAW);
  }

  function setCommon(p, t) {
    const u = p.u;
    gl.useProgram(p.p);
    gl.uniformMatrix3fv(u.u_rot, false, camS.rot);
    gl.uniform2f(u.u_res, W, H); gl.uniform2f(u.u_pan, cam.px, cam.py); gl.uniform1f(u.u_S, camS.S); gl.uniform1f(u.u_D, camS.D);
    gl.uniform1f(u.u_time, t); gl.uniform1f(u.u_breath, reduced ? 0 : 1); gl.uniform1f(u.u_zoom, cam.zoom);
    gl.uniform4fv(u["u_cc[0]"], ST.cc); gl.uniform4fv(u["u_st[0]"], ST.cur);
    gl.uniform3fv(u["u_tc[0]"], ST.tc); gl.uniform3fv(u["u_base[0]"], ST.base);
  }
  function draw(t) {
    const glow = quality.bloom && post && post.w === canvas.width && post.h === canvas.height;
    gl.bindFramebuffer(gl.FRAMEBUFFER, glow ? post.ms : null);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    if (!scene.nodes.length) { if (glow) present(false); return; }
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
    const hov = view.hover ? view.hover.gi : -1, sel = view.selected && view.selected.gi !== undefined ? view.selected.gi : -1;
    const e = progs.edge; setCommon(e, t);
    gl.uniform1f(e.u.u_hover, hov); gl.uniform1f(e.u.u_sel, sel); gl.uniform1f(e.u.u_mode, ST.mode); gl.uniform1f(e.u.u_ambient, ST.ambient);
    gl.uniform1f(e.u.u_width, clamp(0.62 * Math.sqrt(cam.zoom), 0.5, 1.5));
    if (strand) { gl.uniform1f(e.u.u_segs, 10); gl.uniform1f(e.u.u_alpha, 0.34); gl.bindVertexArray(strand); gl.drawArraysInstanced(gl.TRIANGLES, 0, 60, scene.strandN); }
    if (mesh) { gl.uniform1f(e.u.u_segs, 1); gl.uniform1f(e.u.u_alpha, 1.05); gl.bindVertexArray(mesh); gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, scene.meshN); }
    const n = progs.node; setCommon(n, t);
    gl.uniform1f(n.u.u_hover, hov); gl.uniform1f(n.u.u_sel, sel);
    gl.bindVertexArray(nodesVao); gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, scene.nodes.length);
    gl.bindVertexArray(null);
    if (glow) present(true);
  }
  // multisampled scene -> texture -> bright pass (half res) -> blur H, V -> scene + glow on screen
  function present(withGlow) {
    gl.disable(gl.BLEND);
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, post.ms); gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, post.scene);
    gl.blitFramebuffer(0, 0, post.w, post.h, 0, 0, post.w, post.h, gl.COLOR_BUFFER_BIT, gl.NEAREST);
    const pass = (prog, target, w, h, bind) => {
      gl.bindFramebuffer(gl.FRAMEBUFFER, target); gl.viewport(0, 0, w, h); gl.useProgram(prog.p); bind(prog.u);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    };
    const unit = (i, t, loc) => { gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, t); gl.uniform1i(loc, i); };
    if (withGlow) {
      pass(progs.bright, post.a, post.hw, post.hh, (u) => { unit(0, post.sceneTex, u.u_tex); gl.uniform2f(u.u_px, 1 / post.w, 1 / post.h); });
      for (let i = 0; i < 2; i++) {  // two blur rounds: a wide soft halo
        pass(progs.blur, post.b, post.hw, post.hh, (u) => { unit(0, post.aTex, u.u_tex); gl.uniform2f(u.u_dir, (1.6 + i) / post.hw, 0); });
        pass(progs.blur, post.a, post.hw, post.hh, (u) => { unit(0, post.bTex, u.u_tex); gl.uniform2f(u.u_dir, 0, (1.6 + i) / post.hh); });
      }
    }
    pass(progs.comp, null, post.w, post.h, (u) => {
      unit(0, post.sceneTex, u.u_scene); unit(1, post.aTex, u.u_bloom);
      gl.uniform1f(u.u_amt, withGlow ? clamp(0.85 - 0.12 * Math.log2(Math.max(1, cam.zoom)), 0.35, 0.85) : 0);
    });
  }

  // the fallback: the same scene on a plain 2D canvas
  function draw2D(t) {
    const c = ctx2;
    c.setTransform(DPR, 0, 0, DPR, 0, 0); c.globalCompositeOperation = "source-over"; c.fillStyle = "#000"; c.fillRect(0, 0, W, H);
    if (!scene.nodes.length) return;
    c.globalCompositeOperation = "lighter"; c.lineWidth = 0.6;
    const cl = scene.clusters;
    const run = (arr, N, strands) => {
      for (let i = 0; i < N; i++) {
        const o = i * 14, cA = arr[o + 8], cB = arr[o + 9], kind = arr[o + 10], w = arr[o + 11];
        const dA = ST.cur[cA * 4], dB = ST.cur[cB * 4], tA = ST.cur[cA * 4 + 1], lA = ST.cur[cA * 4 + 2], lB = ST.cur[cB * 4 + 2], rd = ST.cur[cA * 4 + 3] + ST.cur[cB * 4 + 3];
        project(arr[o], arr[o + 1], arr[o + 2], endA); project(arr[o + 4], arr[o + 5], arr[o + 6], endB);
        let col, alpha;
        if (!strands) {
          col = w > 0.8 ? CYAN : cl[cA].base; alpha = (0.1 + 0.2 * w) * dA;
          if (tA > 0.05) { tintCol[0] = ST.tc[cA * 3]; tintCol[1] = ST.tc[cA * 3 + 1]; tintCol[2] = ST.tc[cA * 3 + 2]; col = tintCol; }
        } else {
          const lit = ST.mode ? Math.max(lA, lB) : Math.min(lA, lB);
          col = lit > 0.3 ? (rd > 0.5 ? RED : AMBER) : WHITE;
          alpha = (kind < 1.5 ? 0.12 + 0.1 * w : (0.04 + 0.03 * w) * ST.ambient) * (lit > 0.3 ? 2.6 : Math.min(dA, dB));
        }
        c.strokeStyle = rgba(col, alpha); c.beginPath(); c.moveTo(endA.sx, endA.sy); c.lineTo(endB.sx, endB.sy); c.stroke();
      }
    };
    run(scene.strands, scene.strandN, true); run(scene.mesh, scene.meshN, false);
    for (const n of scene.nodes) {
      const dim = ST.cur[n.ci * 4] * (ST.nodeDim[n.gi] ?? 1), hot = ST.nodeHot[n.gi] || 0;
      c.fillStyle = hot > 0.5 ? HEX.amber : "#cfeaff"; c.globalAlpha = clamp(dim, 0.1, 1);
      c.beginPath(); c.arc(n.sx, n.sy, Math.max(1, (n.size || 2) * 0.5 * n.f), 0, 6.2832); c.fill();
    }
    c.globalAlpha = 1;
  }

  return {
    ok, upload, uploadDyn,
    resize(w, h, ratio) {
      canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
      try { setupPost(canvas.width, canvas.height); } catch (err) { quality.bloom = false; console.warn("knowledge graph: no glow pass", err); }
    },
    draw(t) { if (ok) draw(t); else draw2D(t); },
  };
})();

/* ---------- overlay: labels, tags, pulses, tooltip ---------- */
function resize() {
  const device = window.devicePixelRatio || 1;
  // sharp at any zoom: up to 3x on high-DPI screens, 1.5x supersampling on ordinary ones
  DPR = Math.min(3, device < 1.5 ? device * 1.5 : device) * quality.scale;
  const box = canvas.getBoundingClientRect();
  W = Math.max(1, box.width); H = Math.max(1, box.height);
  renderer.resize(W, H, DPR);
  if (scene.clusters.length && (H > W) !== tall) { buildScene(); return; }
  labels.width = Math.round(W * DPR); labels.height = Math.round(H * DPR);
  view.dirty = true;
}
new ResizeObserver(resize).observe(canvas);

const MONO = "'Geist Mono', ui-monospace, monospace";
function halo(text, x, y) { octx.strokeStyle = "rgba(0,0,0,.85)"; octx.lineWidth = 3; octx.lineJoin = "round"; octx.strokeText(text, x, y); octx.fillText(text, x, y); }

function drawOverlay(now) {
  const c = octx;
  c.setTransform(DPR, 0, 0, DPR, 0, 0); c.clearRect(0, 0, W, H);
  c.textBaseline = "alphabetic";
  if (!scene.clusters.length) {
    c.fillStyle = HEX.dim; c.textAlign = "center"; c.font = `500 12px ${MONO}`;
    const lines = !view.loaded ? ["READING MEMORY AND KNOWLEDGE…"] : W < 560 ? ["NO RECORDS YET", "memory and knowledge are empty", "type sync, ingest https://…", "or note …"]
      : ["NO RECORDS YET  ·  memory and knowledge are both empty", "type sync, ingest https://… or note … below"];
    lines.forEach((t, i) => c.fillText(t, W / 2, H / 2 + (i - (lines.length - 1) / 2) * 18));
    return;
  }
  // a faint orbit ring round the whole scene, with a slow marker on it
  const ring = scene.extent * 1.02;
  c.strokeStyle = "rgba(255,255,255,.07)"; c.lineWidth = 1; c.setLineDash([2, 7]); c.beginPath();
  for (let i = 0; i <= 120; i++) {
    const a = (i / 120) * 6.2832; project(Math.cos(a) * ring, 0, Math.sin(a) * ring, ovPt);
    i ? c.lineTo(ovPt.sx, ovPt.sy) : c.moveTo(ovPt.sx, ovPt.sy);
  }
  c.stroke(); c.setLineDash([]);
  if (!reduced) {
    const a = timeNow * 0.12; project(Math.cos(a) * ring, 0, Math.sin(a) * ring, ovPt);
    c.fillStyle = rgba(CYAN, 0.7); c.beginPath(); c.arc(ovPt.sx, ovPt.sy, 2, 0, 6.2832); c.fill();
  }
  const placed = [];
  const free = (x, y, w, h) => {
    for (const p of placed) if (x < p[2] && x + w > p[0] && y < p[3] && y + h > p[1]) return false;
    placed.push([x, y, x + w, y + h]); return true;
  };
  // strand tags: small amber labels riding the bundles between clusters
  c.font = `500 10px ${MONO}`; c.textAlign = "left";
  for (const b of scene.bundles) {
    const A = scene.clusters[b.a], B = scene.clusters[b.b];
    const lit = ST.mode ? Math.max(ST.cur[b.a * 4 + 2], ST.cur[b.b * 4 + 2]) : Math.min(ST.cur[b.a * 4 + 2], ST.cur[b.b * 4 + 2]);
    const dim = Math.min(ST.cur[b.a * 4], ST.cur[b.b * 4]), alpha = 0.35 * dim + 0.65 * lit;
    if (alpha < 0.12) continue;
    const t = 0.38 + b.seed * 0.24;
    project(A.c[0] + (B.c[0] - A.c[0]) * t, A.c[1] + (B.c[1] - A.c[1]) * t, A.c[2] + (B.c[2] - A.c[2]) * t, ovPt);
    const text = `${b.rel} ×${b.n}`, w = c.measureText(text).width;
    if (ovPt.sx < 4 || ovPt.sx + w > W - 4 || !free(ovPt.sx, ovPt.sy - 10, w + 4, 12)) continue;
    c.fillStyle = (ST.cur[b.a * 4 + 3] + ST.cur[b.b * 4 + 3]) > 0.5 && lit > 0.3 ? rgba(RED, alpha) : rgba(AMBER, alpha);
    halo(text, ovPt.sx, ovPt.sy);
  }
  // cluster labels
  for (const cl of scene.clusters.slice().sort((p, q) => p.depth - q.depth)) {
    const dim = ST.cur[cl.i * 4], a = 0.3 + 0.7 * dim, rpx = cl.R * camS.S * cl.f;
    // zoomed in: a cluster far off screen keeps its label off screen too (no pile-up at the edges)
    if (cam.zoom > 1.4 && (cl.sx < -rpx - 40 || cl.sx > W + rpx + 40 || cl.sy < -rpx - 40 || cl.sy > H + rpx + 40)) continue;
    if (cam.zoom > 1.4 && (cl.sx < 0 || cl.sx > W || cl.sy < 0 || cl.sy > H)) continue;
    const name = cl.label, sub = `${cl.n.toLocaleString()} ${cl.group === "mem" || cl.n !== 1 ? "items" : "item"} · ${cl.running > 0 ? "running" : "live"}`;
    c.font = `600 11px ${MONO}`; const w = Math.max(c.measureText(name).width, 80);
    const x = clamp(cl.sx - w / 2, 6, W - w - 6);
    let y = clamp(cl.sy + rpx * 0.04, 20, H - 16);
    for (const dy of [0, 28, -28, 56, -56, 84]) {
      const yy = clamp(y + dy, 20, H - 16);
      if (free(x, yy - 12, w, 26)) { y = yy; break; }
    }
    c.textAlign = "left"; c.fillStyle = `rgba(237,237,237,${a})`; halo(name, x, y);
    c.font = `400 9.5px ${MONO}`; c.fillStyle = `rgba(138,138,138,${a})`; halo(sub, x, y + 12);
  }
  // up close: label individual records, biggest first, never overlapping (at most 48)
  const near = clamp((cam.zoom - 2.2) / 1.6, 0, 1);
  if (near > 0) {
    c.font = `500 10px ${MONO}`; c.textAlign = "left";
    const seen = scene.nodes.filter((n) => n.sx > -20 && n.sx < W + 20 && n.sy > -20 && n.sy < H + 20
      && ST.cur[n.ci * 4] * (ST.nodeDim[n.gi] ?? 1) > 0.3 && n.label && !(n.kind === "mem" && n.mkind === "kind"))
      .sort((a, b) => (b.size || 1) * b.f - (a.size || 1) * a.f);
    let shown = 0;
    for (const n of seen) {
      if (shown >= 48) break;
      const text = clip(n.label, 30), w = c.measureText(text).width, x = n.sx + 6, y = n.sy + 3;
      if (x + w > W - 4 || !free(x, y - 10, w + 4, 13)) continue;
      c.fillStyle = `rgba(237,237,237,${(0.35 + 0.5 * near) * clamp(n.f, 0.6, 1)})`; halo(text, x, y); shown++;
    }
  }
  // pulses from the knowledge bots
  for (const p of view.pulses) {
    const n = view.byId.get(p.id); if (!n || n.ci < 0 || n.sx === undefined) continue;
    const age = (now - p.t) / 1000, warn = p.tag === "CONFLICT", col = warn ? AMBER : CYAN;
    for (let k = 0; k < 3; k++) {
      const r = 10 + ((age * 26 + k * 14) % 44);
      c.strokeStyle = rgba(col, Math.max(0, 0.6 - r / 90)); c.lineWidth = 1.2; c.beginPath(); c.arc(n.sx, n.sy, r, 0, 6.2832); c.stroke();
    }
    c.font = `600 11px ${MONO}`; c.textAlign = "left"; c.fillStyle = HEX.amber; halo(`${clip(n.label, 28)} / ${p.tag}`, n.sx + 14, n.sy + 4);
  }
  view.pulses = view.pulses.filter((p) => now - p.t < 9000);
  for (const cl of scene.clusters) cl.running = 0;
  for (const p of view.pulses) { const n = view.byId.get(p.id); if (n && n.ci >= 0) scene.clusters[n.ci].running++; }
  // selection and hover
  const sel = view.selected && view.selected.ci >= 0 ? view.selected : null;
  if (sel && sel.sx !== undefined) {
    c.strokeStyle = HEX.amber; c.lineWidth = 1.2; c.beginPath(); c.arc(sel.sx, sel.sy, 9, 0, 6.2832); c.stroke();
    c.beginPath(); c.moveTo(sel.sx - 14, sel.sy); c.lineTo(sel.sx - 11, sel.sy); c.moveTo(sel.sx + 11, sel.sy); c.lineTo(sel.sx + 14, sel.sy); c.stroke();
  }
  const card = view.hover || sel;
  if (card && card.sx !== undefined) tooltip(card);
}

function tipRows(n) {
  if (n.kind === "mem") {
    const cl = scene.clusters[n.ci];
    return [n.mkind === "item" ? clip(n.label, 44) : (n.mkind === "tag" ? "tag · " : "hub · ") + n.label,
      [["CLUSTER", cl ? cl.label : "—"], ["CREATED", n.created_at ? `${clock8(n.created_at)} · ${ago(n.created_at)}` : "—"],
        ["LINKS · IMPORTANCE", `${(scene.degree && scene.degree.get(n.id)) || 0} · ${n.importance ?? "—"}`]]];
  }
  if (n.kind === "note") return [n.path, [["NOTE", n.label], ["FOLDER · TYPE", `${n.folder || "root"} · ${n.type || "—"}`], ["LINKS", String(n.degree)],
    ["IN THE WIKI", n.mirror ? "wiki/" + n.mirror.slice(2) + ".md" : n.folder === "daily" ? "daily notes stay out" : "not synced yet"]]];
  if (n.kind === "source") return [n.label, [["ACCESS / RAW ORIGINAL", "READ ONLY · 0 MUTATIONS"], ["SOURCE", n.title], ["LAST CONFIRMED", n.added]]];
  const page = n.kind === "claim" ? view.byId.get("p:" + n.page) : n;
  let claim = n.kind === "claim" ? n : null;
  if (!claim && page && view.claimsOf.get(page.id)) { const list = view.claimsOf.get(page.id); claim = list.find((c) => c.flag === "conflict") || list[0]; }
  return [`wiki/${(page || n).label}`, [["CLAIM", claim ? claim.text : page.title], ["SOURCE", claim ? (claim.sources || []).join(" ⇔ ") : "—"],
    ["LAST CONFIRMED", (claim ? claim.confirmed : page.confirmed) + (claim && claim.flag === "conflict" ? " — REVIEW REQUIRED" : claim && claim.flag === "unconfirmed" ? " — UNCONFIRMED" : "")]]];
}

function tooltip(n) {
  const c = octx, [title, rows] = tipRows(n), width = Math.min(280, W - 16);
  c.font = `400 10px ${MONO}`;
  const lines = [];
  for (const [k, v] of rows) {
    lines.push({ k: true, t: k });
    let line = "";
    for (const word of String(v ?? "").split(" ")) {
      if (c.measureText(line + word).width > width - 20 && line) { lines.push({ t: line }); line = ""; }
      line += word + " ";
    }
    if (line.trim()) lines.push({ t: line.trim() });
  }
  lines.splice(12);
  const h = 28 + lines.length * 13;
  let x = n.sx + 16, y = n.sy - h - 10;
  if (x + width > W - 6) x = n.sx - width - 16;
  if (x < 6) x = 6;
  if (y < 6) y = Math.min(H - h - 6, n.sy + 14);
  c.fillStyle = "rgba(0,0,0,.92)"; c.fillRect(x, y, width, h);
  c.strokeStyle = "rgba(255,255,255,.2)"; c.lineWidth = 1; c.strokeRect(x + 0.5, y + 0.5, width, h);
  c.textAlign = "left"; c.fillStyle = HEX.ink; c.font = `600 11px ${MONO}`; c.fillText(clip(title, 38), x + 10, y + 17);
  c.font = `400 10px ${MONO}`;
  lines.forEach((l, i) => { c.fillStyle = l.k ? HEX.dim : l.t.includes("REVIEW REQUIRED") ? HEX.amber : HEX.ink; c.fillText(l.t, x + 10, y + 33 + i * 13); });
}

/* ---------- loop ---------- */
let frames = 0, fpsAt = performance.now(), lastDraw = 0, lastT = performance.now();
let lastClock = "", lastTickAt = -1e9, lastTickLast = "", lastAngle = "", frameOn = false, frameId = 0;
let graphSeen = true, graphIo = null;  // graphSeen stays true when IntersectionObserver is missing
function graphShown() { return graphSeen && !document.hidden; }
function stopFrame() {
  frameOn = false;
  if (frameId) { cancelAnimationFrame(frameId); frameId = 0; }
}
function kickFrame() {  // resume paints one frame immediately, then the loop carries on
  if (!graphShown() || frameOn) return;
  view.dirty = true; frameOn = true; lastT = performance.now();
  frameId = requestAnimationFrame(frame);
}
function frame(now) {
  frameId = 0;
  if (!graphShown()) { frameOn = false; return; }
  frameId = requestAnimationFrame(frame);
  const active = cam.drag || cam.pan || cam.panTo || cam.anchor || now < view.activeUntil || ST.busy || Math.abs(cam.spin) > 1e-5 || Math.abs(cam.zoomTo - cam.zoom) > 0.002 || view.pulses.length;
  const clk = new Date().toTimeString().slice(0, 8);
  if (clk !== lastClock) { lastClock = clk; $("clock").textContent = clk; }
  if (now - lastTickAt >= 1000 || view.last !== lastTickLast) {  // the ticker needs no more than 1 Hz
    lastTickAt = now; lastTickLast = view.last;
    $("ticker").textContent = `${((now - view.started) / 1000).toFixed(2)}   ${view.last}`;
  }
  if (reduced) { if (!view.dirty && !active) return; }
  else if (!active && !view.dirty && now - lastDraw < 32) return;
  const dt = Math.min(0.1, (now - lastT) / 1000); lastT = now; lastDraw = now;
  if (!cam.drag) {
    const exploring = cam.zoom > 1.6 || Math.hypot(cam.px, cam.py) > 4;  // hold still while you look closely
    cam.az += (reduced || exploring ? 0 : 0.045 * dt) + cam.spin;
    cam.spin *= 0.94; if (Math.abs(cam.spin) < 1e-5) cam.spin = 0;
  }
  const z0 = cam.zoom;
  cam.zoom += (cam.zoomTo - cam.zoom) * (reduced ? 1 : 0.18);
  if (cam.anchor && z0 > 0) {  // the point under the cursor stays under the cursor
    const k = cam.zoom / z0, ax = cam.anchor.x - W / 2, ay = cam.anchor.y - H / 2;
    cam.px = ax - (ax - cam.px) * k; cam.py = ay - (ay - cam.py) * k;
    if (Math.abs(cam.zoomTo - cam.zoom) < 0.002) cam.anchor = null;
  }
  if (cam.panTo) {
    cam.px += (cam.panTo.x - cam.px) * (reduced ? 1 : 0.16); cam.py += (cam.panTo.y - cam.py) * (reduced ? 1 : 0.16);
    if (Math.hypot(cam.panTo.x - cam.px, cam.panTo.y - cam.py) < 0.5) { cam.px = cam.panTo.x; cam.py = cam.panTo.y; cam.panTo = null; }
  }
  tuneQuality(now, dt);
  timeNow = reduced ? 0 : (now - view.started) / 1000;
  const nodesChanged = easeState(dt);
  if (nodesChanged) renderer.uploadDyn();
  projectAll();
  renderer.draw(timeNow);
  drawOverlay(now);
  view.dirty = false;
  frames++;
  if (now - fpsAt > 1000) { $("fps").textContent = `${frames}FPS`; frames = 0; fpsAt = now; }
  const deg = (((cam.az * 180) / Math.PI + 180) % 360) - 180;
  const angle = `AZ ${deg.toFixed(1)}° · ZOOM ${cam.zoom < 10 ? cam.zoom.toFixed(1) : Math.round(cam.zoom)}× · ${quality.bloom ? "GLOW" : "FAST"} ${Math.round(DPR * 10) / 10}x`;
  if (angle !== lastAngle) { lastAngle = angle; $("graph-angle").textContent = angle; }
}
function tuneQuality(now, dt) {
  // Only frames that really animate count (the loop idles otherwise).
  quality.ema = quality.ema * 0.9 + dt * 1000 * 0.1;
  if (quality.ema > 26) {           // under ~38 fps: give up glow first, then resolution
    quality.fastSince = 0;
    if (!quality.slowSince) quality.slowSince = now;
    if (now - quality.slowSince > 1500) {
      quality.slowSince = now;
      if (quality.bloom) quality.bloom = false;
      else if (quality.scale > 0.6) { quality.scale = Math.max(0.6, quality.scale * 0.8); resize(); }
    }
  } else if (quality.ema < 17) {    // ~60 fps with room to spare: earn it back slowly
    quality.slowSince = 0;
    if (!quality.fastSince) quality.fastSince = now;
    if (now - quality.fastSince > 5000) {
      quality.fastSince = now;
      if (quality.scale < 1) { quality.scale = Math.min(1, quality.scale / 0.8); resize(); }
      else if (!quality.bloom) quality.bloom = true;
    }
  } else { quality.slowSince = 0; quality.fastSince = 0; }
}

/* ---------- interaction ---------- */
function nearest(x, y) {
  let best = null, dist = 13;
  for (const n of scene.nodes) {
    if (ST.cur[n.ci * 4] < 0.12) continue;
    const d = Math.hypot(n.sx - x, n.sy - y) - Math.min(5, (n.size || 2) * 0.6 * n.f) - n.depth * 0.5;
    if (d < dist) { dist = d; best = n; }
  }
  return best;
}
const pointers = new Map();
let pinch = 0;
const poke = () => { view.activeUntil = performance.now() + 700; view.dirty = true; };
canvas.addEventListener("pointerdown", (e) => {
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  canvas.setPointerCapture(e.pointerId);
  if (pointers.size === 2) {
    const [p, q] = [...pointers.values()]; pinch = Math.hypot(p.x - q.x, p.y - q.y);
    cam.drag = null; cam.pan = { x: (p.x + q.x) / 2, y: (p.y + q.y) / 2, px: cam.px, py: cam.py }; return;
  }
  cam.spin = 0; cam.panTo = null;
  if (e.button === 1 || e.button === 2 || e.shiftKey) {  // shift, right or middle drag: pan
    cam.pan = { x: e.clientX, y: e.clientY, px: cam.px, py: cam.py, moved: false };
    canvas.style.cursor = "move"; poke(); return;
  }
  cam.drag = { x: e.clientX, y: e.clientY, az: cam.az, el: cam.el, moved: false, vx: 0, t: e.timeStamp };
  poke();
});
canvas.addEventListener("pointermove", (e) => {
  const box = canvas.getBoundingClientRect();
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 2) {
    const [p, q] = [...pointers.values()], d = Math.hypot(p.x - q.x, p.y - q.y), mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2;
    if (cam.pan) { cam.px = cam.pan.px + (mx - cam.pan.x); cam.py = cam.pan.py + (my - cam.pan.y); }
    if (pinch) zoomBy(d / pinch, mx - box.left, my - box.top);
    pinch = d; if (cam.pan) { cam.pan.x = mx; cam.pan.y = my; cam.pan.px = cam.px; cam.pan.py = cam.py; }
    poke(); return;
  }
  if (cam.pan) {
    cam.px = cam.pan.px + (e.clientX - cam.pan.x); cam.py = cam.pan.py + (e.clientY - cam.pan.y);
    view.hover = null; poke(); return;
  }
  if (cam.drag) {
    const dx = e.clientX - cam.drag.x, dy = e.clientY - cam.drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) cam.drag.moved = true;
    const az = cam.drag.az + dx * 0.006, dt = Math.max(1, e.timeStamp - cam.drag.t);
    cam.drag.vx = ((az - cam.az) / dt) * 16; cam.drag.t = e.timeStamp;
    cam.az = az; cam.el = clamp(cam.drag.el + dy * 0.004, -1.25, 1.25);
    view.hover = null;
  } else {
    const h = nearest(e.clientX - box.left, e.clientY - box.top);
    if (h !== view.hover) { view.hover = h; canvas.style.cursor = h ? "pointer" : ""; }
  }
  poke();
});
canvas.addEventListener("pointerup", (e) => {
  const box = canvas.getBoundingClientRect();
  pointers.delete(e.pointerId); pinch = 0;
  if (cam.pan) { cam.pan = null; canvas.style.cursor = ""; poke(); return; }
  if (cam.drag && !cam.drag.moved) select(nearest(e.clientX - box.left, e.clientY - box.top));
  else if (cam.drag && !reduced && e.timeStamp - cam.drag.t < 80) cam.spin = clamp(cam.drag.vx, -0.08, 0.08);
  cam.drag = null; poke();
});
canvas.addEventListener("pointercancel", (e) => { pointers.delete(e.pointerId); pinch = 0; cam.drag = null; cam.pan = null; });
canvas.addEventListener("contextmenu", (e) => e.preventDefault());  // right-drag pans
canvas.addEventListener("pointerleave", () => { view.hover = null; poke(); });
// Zoom toward a screen point (the cursor, the fingers); no point: toward the centre.
const zoomBy = (f, x, y) => {
  cam.zoomTo = clamp(cam.zoomTo * f, ZOOM_MIN, ZOOM_MAX);
  cam.anchor = x === undefined ? { x: W / 2, y: H / 2 } : { x, y }; cam.panTo = null; poke();
};
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const box = canvas.getBoundingClientRect(), speed = e.ctrlKey ? 0.01 : 0.0015;  // ctrlKey: a trackpad pinch
  zoomBy(Math.exp(-e.deltaY * speed), e.clientX - box.left, e.clientY - box.top);
}, { passive: false });
function home() { cam.az = -0.55; cam.el = 0.26; cam.zoomTo = 1; cam.spin = 0; cam.anchor = null; cam.panTo = { x: 0, y: 0 }; poke(); }
function flyTo(n) {  // double-click a record: bring it to the centre and lean in
  const z = Math.max(cam.zoomTo, Math.min(ZOOM_MAX, cam.zoomTo * 2.6, 8));
  const k = z / cam.zoom;
  cam.anchor = null; cam.zoomTo = z;
  // the node's offset from the centre scales with zoom: aim the pan so it ends at the centre
  cam.panTo = { x: (cam.px - (n.sx - W / 2)) * k, y: (cam.py - (n.sy - H / 2)) * k }; poke();
}
canvas.addEventListener("dblclick", (e) => {
  const box = canvas.getBoundingClientRect(), n = nearest(e.clientX - box.left, e.clientY - box.top);
  if (n) { select(n); flyTo(n); } else home();
});
canvas.addEventListener("keydown", (e) => {
  const k = { ArrowLeft: () => (cam.az -= 0.1), ArrowRight: () => (cam.az += 0.1), ArrowUp: () => (cam.el = clamp(cam.el - 0.08, -1.25, 1.25)),
    ArrowDown: () => (cam.el = clamp(cam.el + 0.08, -1.25, 1.25)), "+": () => zoomBy(1.25), "=": () => zoomBy(1.25), "-": () => zoomBy(1 / 1.25),
    "0": () => home() }[e.key];
  if (e.shiftKey && e.key.startsWith("Arrow")) {  // shift+arrows pan
    e.preventDefault(); cam.panTo = null;
    cam.px += { ArrowLeft: 40, ArrowRight: -40 }[e.key] || 0; cam.py += { ArrowUp: 40, ArrowDown: -40 }[e.key] || 0; poke(); return;
  }
  if (k) { e.preventDefault(); k(); poke(); }
});
addEventListener("keydown", (e) => {  // Esc: back to ALL, nothing selected
  if (e.key !== "Escape" || e.target.closest("input, textarea")) return;
  if (view.scn !== "all") applyScenario("all");
  if (view.selected) select(null);
});

function select(n) {
  view.selected = n;
  renderInspector();
  renderIndex();
  poke();
}

/* ---------- panels ---------- */
function renderCounts() {
  const s = scene.stats || {}, st = view.snap ? view.snap.stats : {};
  const seg = (k, v, cls = "") => `<span class="${cls}"><i>${k}</i><b>${v}</b></span>`;
  $("counts").innerHTML = [seg("MEMORY", (s.mem || 0).toLocaleString()), seg("SRC", pad(st.sources || 0)), seg("DOCS", (st.pages || 0) + (st.answers || 0)),
    seg("CLAIMS", st.claims || 0), seg("CONFLICTS", st.conflicts || 0, st.conflicts ? "warn" : ""), seg("LINKS", (s.links || 0).toLocaleString()),
    seg("NODES", (s.total || 0).toLocaleString())].join("");
  $("graph-count").textContent = `GRAPH / ${(s.total || 0).toLocaleString()} RECORDS · ${(s.links || 0).toLocaleString()} LINKS · ${scene.clusters.length} CLUSTERS`;
}

function renderAll() {
  const s = view.snap; if (!s) return;
  renderCounts();
  const commit = s.commit;
  $("role-librarian").querySelector("span").textContent = commit ? `${pad(commit.writes.length, 2)}/${pad(commit.writes.length, 2)} COMMITTED` : "00/00 COMMITTED";
  const sched = s.checker.schedule;
  $("role-checker").querySelector("span").textContent = s.checker.last ? `LAST ${s.checker.last.slice(5, 16).replace("T", " ")}` : `QUEUED ${sched.at}`;
  renderChat(); renderQueue(); renderInspector(); renderIndex(); renderRaw(); renderDiff(); renderMatrix(); renderChecker(); renderMix(); renderAge();
}

function who(bot) { return { you: "YOU", reader: "READER", librarian: "LIBRARIAN", checker: "CHECKER" }[bot] || String(bot || "JARVIS").toUpperCase(); }

function renderChat() {
  // A run of page writes from one bot reads as one line: "wiki/a.md +8 more · --source …".
  const rows = [];
  for (const e of view.snap.log) {
    const prev = rows[rows.length - 1];
    if (prev && ["patch", "create"].includes(e.action) && prev.e.bot === e.bot && prev.e.action === e.action && prev.e.message === e.message) prev.n++;
    else rows.push({ e, n: 0 });
  }
  // The same line again (the nightly "start / audit" pair, every night): one row, newest time, ×N.
  const seen = new Map();
  for (const row of rows) {
    const key = [row.e.bot, row.e.action, row.e.target, row.e.message].join("|");
    const had = seen.get(key);
    if (had) { had.times++; had.e = row.e; seen.delete(key); seen.set(key, had); }
    else seen.set(key, { ...row, times: 1 });
  }
  const shown = [...seen.values()];
  if (shown.length) {
    $("chat").innerHTML = shown.slice(-14).map(({ e, n, times }) => `<li class="who-${esc(e.bot)}"><time>${esc(String(e.at || "").slice(11, 19))}</time><b class="verb v-${esc(e.action)}">${esc(String(e.action || who(e.bot)).toLowerCase())}</b><span title="${esc(e.message)}">${esc(e.target ? e.target + (n ? ` +${n} more` : "") + " · " : "")}${esc(e.message)}${times > 1 ? `<em class="times">×${times}</em>` : ""}</span></li>`).join("");
    const last = view.snap.log[view.snap.log.length - 1];
    $("chat-foot").textContent = last ? `${last.at.slice(11)} · ${last.bot} :: ${last.action}` : "quiet";
    return;
  }
  // The engine is quiet: show what JARVIS itself just did, straight from its memory.
  const mem = view.memNodes.filter((n) => n.mkind === "item" && n.created_at).sort((a, b) => (a.created_at < b.created_at ? 1 : -1)).slice(0, 14).reverse();
  $("chat").innerHTML = mem.length ? mem.map((n) => `<li class="who-mem"><time>${esc(clock8(n.created_at))}</time><b class="verb m-${esc(n.group)}">${esc(n.group)}</b><span title="${esc(n.label)}">${esc(n.label)}</span></li>`).join("")
    : `<li class="empty"><span>no bot activity yet · type sync, ingest https://… or note …</span></li>`;
  $("chat-foot").textContent = mem.length ? `engine quiet · showing JARVIS memory (${view.memNodes.length} records)` : "quiet";
}

function renderQueue() {
  const writes = view.snap.writes || [];
  const list = writes.length ? writes : view.snap.pages.slice(0, 12).map((p) => p.slug);
  $("queue").innerHTML = list.length ? list.slice(0, 9).map((slug, i) => `<li><span class="n">${pad(i + 1, 2)}</span><span>wiki/${esc(slug)}.md</span><span class="bar"></span><span class="ok">OK</span></li>`).join("")
    : `<li class="empty"><span>queue empty · nothing waiting to be written</span></li>`;
  $("queue-count").textContent = `${list.length} DOC`;
  $("queue-foot").textContent = `${pad(writes.length, 2)}/${pad(writes.length, 2)} ATOMIC WRITES`;
}

function renderInspector() {
  const s = view.snap; if (!s) return;
  let n = view.selected;
  // Nothing picked and the checker is waiting on you: show the first open contradiction.
  const open = s.open_conflicts || [];
  const pick = n && n.kind === "claim" ? open.find((c) => c.claim === n.label || c.rival === n.label)
    : !n ? open[0] : null;
  if (pick) return renderRuling(pick, open.length);
  if (!n && s.diff) n = view.byId.get("p:" + s.diff.target.replace(/^wiki\//, "").replace(/\.md$/, ""));
  if (!n) {
    $("inspect-tag").textContent = "TRACE";
    $("inspect").innerHTML = `<p class="dim">Click a node.</p><p class="hint">${scene.nodes.length ? `${scene.nodes.length.toLocaleString()} records in ${scene.clusters.length} clusters. Hover a dot to read it, click to pin it here. Drag to orbit, wheel to zoom, double-click to reset.` : "Nothing to inspect yet."}</p>`;
    return;
  }
  if (n.kind === "mem") {
    const cl = scene.clusters[n.ci], deg = (scene.degree && scene.degree.get(n.id)) || 0;
    $("inspect-tag").textContent = n.mkind === "kind" ? "HUB" : n.mkind === "tag" ? "TAG" : "MEMORY";
    $("inspect").innerHTML = `<h3>${esc(clip(n.label, 90))}</h3><div class="k">ID</div><div class="v mono">${esc(n.id)}</div>
      <div class="k">KIND · GROUP · CLUSTER</div><div class="v">${esc(n.mkind)} · ${esc(n.group || "—")} · ${esc(cl ? cl.label : "—")}</div>
      <div class="k">CREATED</div><div class="v">${n.created_at ? esc(new Date(n.created_at).toLocaleString()) + " · " + esc(ago(n.created_at)) : "—"}</div>
      <div class="k">LINKS · IMPORTANCE · SOURCE</div><div class="v">${deg} · ${esc(n.importance ?? "—")} · ${esc(n.source || "—")}</div>`;
    return;
  }
  if (n.kind === "note") {
    $("inspect-tag").textContent = "VAULT";
    $("inspect").innerHTML = `<h3>${esc(n.label)}</h3><div class="k">PATH</div><div class="v">${esc(n.path)}</div>
      <div class="k">FOLDER · TYPE · LINKS</div><div class="v">${esc(n.folder || "root")} · ${esc(n.type || "—")} · ${n.degree}</div>
      <div class="k">IN THE WIKI</div><div class="v">${n.mirror ? "wiki/" + esc(n.mirror.slice(2)) + ".md" : n.folder === "daily" ? "daily notes stay out" : "not synced yet — type sync"}</div>
      ${n.uri ? `<div class="rule"><a class="btn" href="${esc(n.uri)}">OPEN IN OBSIDIAN</a></div>` : ""}`;
    return;
  }
  if (n.kind === "source") {
    const src = s.sources.find((x) => "s:" + x.id === n.id) || {};
    $("inspect-tag").textContent = "RAW";
    $("inspect").innerHTML = `<h3>${esc(n.label)}</h3><div class="k">TITLE</div><div class="v">${esc(n.title)}</div>
      <div class="k">SHA-256 · ${src.ok ? "VERIFIED" : "CHANGED"}</div><div class="v">${esc(src.sha || "")}…</div>
      <div class="k">ADDED</div><div class="v">${esc(n.added)}</div>`;
    return;
  }
  const page = n.kind === "claim" ? view.byId.get("p:" + n.page) : n;
  const claims = (view.claimsOf && page && view.claimsOf.get(page.id)) || [];
  const claim = n.kind === "claim" ? n : claims.find((c) => c.flag === "conflict") || claims[0];
  if (!page) { $("inspect").innerHTML = `<p class="dim">Page not in the graph.</p>`; return; }
  $("inspect-tag").textContent = claim && claim.flag === "conflict" ? "FLAG" : page.kind === "answer" ? "ANSWER" : "TRACE";
  $("inspect").innerHTML = `<h3>wiki/${esc(page.label)}</h3>
    <div class="k">CLAIM · ${claims.length} ON PAGE</div><div class="v ${claim && claim.flag ? "flag" : ""}">${esc(claim ? claim.text : page.title)}</div>
    <div class="k">SOURCE</div><div class="v">${esc(claim ? claim.sources.join(" ⇔ ") : "—")}</div>
    <div class="k">LAST CONFIRMED</div><div class="v ${claim && claim.flag ? "flag" : ""}">${esc(claim ? claim.confirmed : page.confirmed)}${claim && claim.flag === "conflict" ? " — REVIEW REQUIRED" : claim && claim.flag === "unconfirmed" ? " — UNCONFIRMED" : ""}</div>`;
}

function renderRuling(c, count) {
  $("inspect-tag").textContent = `FLAG ${count > 1 ? "1/" + count : ""}`.trim();
  $("inspect").innerHTML = `<h3>wiki/${esc(c.page)}.md</h3>
    <div class="k">CONFLICT · ${esc(c.why)}</div>
    <div class="v flag">A: ${esc(c.text)} <span class="dim">(${esc(c.sources.join(", "))} · ${esc(c.date)})</span></div>
    <div class="v flag">B: ${esc(c.rival_text)} <span class="dim">(${esc(c.rival_sources.join(", "))} · ${esc(c.rival_date)})</span></div>
    <div class="rule">
      <button type="button" data-claim="${esc(c.claim)}" data-against="${esc(c.rival)}" data-decision="keep">KEEP A</button>
      <button type="button" data-claim="${esc(c.rival)}" data-against="${esc(c.claim)}" data-decision="keep">KEEP B</button>
      <button type="button" data-claim="${esc(c.claim)}" data-against="${esc(c.rival)}" data-decision="both">BOTH TRUE</button>
    </div>`;
}

$("inspect").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-decision]");
  if (!b) return;
  b.disabled = true;
  try {
    await api("/api/kb/resolve", { claim: b.dataset.claim, decision: b.dataset.decision, against: b.dataset.against });
    view.selected = null;
  } catch (err) { view.last = `jarvis :: ${err.message}`; b.disabled = false; }
});

/* ---------- drop a file anywhere: it goes to the reader ---------- */
async function upload(files) {
  for (const file of files) {
    $("last-cmd").textContent = `> reader ingest ${file.name} --read-only`;
    try {
      const res = await fetch(`/api/kb/upload?name=${encodeURIComponent(file.name)}`,
        { method: "POST", headers: { "X-JARVIS": "1", "Content-Type": "application/octet-stream" }, body: file });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
    } catch (err) { view.last = `reader :: ${file.name}: ${err.message}`; $("last-cmd").textContent = "! " + err.message; }
  }
}
let dragDepth = 0;
addEventListener("dragenter", (e) => { if (e.dataTransfer.types.includes("Files")) { dragDepth++; document.body.classList.add("dropping"); } });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dropping"); } });
addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); });
addEventListener("drop", (e) => {
  if (!e.dataTransfer.files.length) return;
  e.preventDefault(); dragDepth = 0; document.body.classList.remove("dropping");
  upload([...e.dataTransfer.files]);
});
$("command-file").addEventListener("change", (e) => { upload([...e.target.files]); e.target.value = ""; });

// graph index: the scene seen from above, every cluster a disc, every record a dot
const mini = $("minimap"), mctx = mini.getContext("2d");
function renderIndex() {
  $("idx-pages").textContent = scene.nodes.length.toLocaleString();
  $("idx-links").textContent = (scene.stats.links || 0).toLocaleString();
  const box = mini.getBoundingClientRect(), dpr = Math.min(2, devicePixelRatio || 1);
  mini.width = box.width * dpr; mini.height = box.height * dpr; mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const sel = view.selected && view.selected.ci >= 0 ? view.selected : null;
  if (scene.clusters.length) {
    const ext = scene.extent, sc = Math.min(box.width, box.height) * 0.46 / ext, cx = box.width / 2, cy = box.height / 2;
    mctx.strokeStyle = "rgba(255,255,255,.08)"; mctx.beginPath(); mctx.arc(cx, cy, ext * 1.02 * sc, 0, 6.2832); mctx.stroke();
    mctx.strokeStyle = "rgba(240,180,41,.22)"; mctx.beginPath();
    for (const b of scene.bundles) { const A = scene.clusters[b.a], B = scene.clusters[b.b]; mctx.moveTo(cx + A.c[0] * sc, cy + A.c[2] * sc); mctx.lineTo(cx + B.c[0] * sc, cy + B.c[2] * sc); }
    mctx.stroke();
    for (const c of scene.clusters) {
      mctx.fillStyle = rgba(c.base, 0.16); mctx.beginPath(); mctx.arc(cx + c.c[0] * sc, cy + c.c[2] * sc, c.R * sc, 0, 6.2832); mctx.fill();
    }
    mctx.fillStyle = "rgba(237,237,237,.75)";
    for (const n of scene.nodes) mctx.fillRect(cx + n.x * sc - 0.5, cy + n.z * sc - 0.5, 1, 1);
    if (sel) { mctx.strokeStyle = HEX.amber; mctx.beginPath(); mctx.arc(cx + sel.x * sc, cy + sel.z * sc, 4, 0, 6.2832); mctx.stroke(); }
  }
  const i = sel ? sel.gi + 1 : 0;
  $("idx-foot").textContent = `SELECT ${pad(i, 2)}/${pad(scene.nodes.length, 2)}${sel ? " · " + clip(sel.kind === "mem" ? sel.label : sel.label || sel.id, 24) : ""}`;
}

function renderRaw() {
  const src = view.snap.sources;
  $("raw").innerHTML = src.length ? src.slice(-8).reverse().map((x) => `<tr><td title="${esc(x.title)}">${esc(x.file)}</td><td class="sha">${esc(x.sha.slice(0, 10))}</td><td class="${x.ok && x.readonly ? "ro" : "bad"}">${x.ok ? (x.readonly ? "RO" : "RW") : "!!"}</td></tr>`).join("")
    : `<tr class="empty"><td colspan="3">no raw sources · type ingest https://… or drop a file</td></tr>`;
  const bad = src.filter((x) => !x.ok).length;
  $("raw-mut").textContent = `${bad} MUTATIONS`;
  $("raw-verify").textContent = src.length ? (bad ? `VERIFY FAIL ${bad}` : `VERIFY OK ${pad(src.length)}`) : "VERIFY —";
}

function renderDiff() {
  const d = view.snap.diff;
  if (!d) { $("diff").innerHTML = `<span class="empty">no writes yet</span>`; $("diff-foot").textContent = "no writes yet"; return; }
  const lines = (d.diff || []).slice(0, 14);
  $("diff").innerHTML = lines.map((l) => {
    const cls = l.startsWith("+++") || l.startsWith("---") ? "head" : l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
    return `<span class="${cls}">${esc(l.length > 90 ? l.slice(0, 89) + "…" : l)}</span>`;
  }).join("\n");
  $("diff-tag").textContent = d.action === "create" ? "NEW" : "PATCH";
  $("diff-foot").textContent = `+${d.added || 0} / -${d.removed || 0} · ${d.target} · ${d.bot}`;
}

function renderMatrix() {
  const m = view.snap.matrix, cols = m.cols.length;
  const el = $("matrix");
  if (!m.rows.length) {  // an empty heat grid, so the instrument still reads as one
    el.style.gridTemplateColumns = `repeat(12, minmax(0, 1fr))`;
    el.innerHTML = Array.from({ length: 12 * 8 }, () => `<span class="c"></span>`).join("");
    el.classList.add("idle");
  } else {
    el.classList.remove("idle");
    el.style.gridTemplateColumns = `70px repeat(${Math.max(1, cols)}, minmax(0, 1fr))`;
    let html = "";
    m.rows.slice(0, 9).forEach((row, i) => {
      html += `<span class="lab" title="${esc(row)}">${esc(row)}</span>`;
      html += m.cells[i].map((v, j) => `<span class="c c${v}" title="${esc(row)} × ${esc(m.cols[j])}"></span>`).join("");
    });
    el.innerHTML = html;
  }
  $("matrix-size").textContent = `${m.rows.length}×${cols}`;
  $("matrix-foot").textContent = m.rows.length ? `${m.rows.length} PAGES + ${cols} RAW SOURCES` : "no citations yet";
}

function renderChecker() {
  const c = view.snap.checker, r = c.report;
  $("check-when").textContent = `NIGHTLY ${c.schedule.at} / ${c.schedule.installed || c.schedule.kind === "schtasks" ? "SCHEDULED" : "MANUAL"}`;
  $("check-scan").textContent = r ? `SCANNED ${pad(r.scanned)} / ${pad(view.snap.stats.claims)}` : "SCANNED 000 / " + pad(view.snap.stats.claims);
  const ages = view.snap.ages, max = Math.max(1, ...ages);
  $("bars").innerHTML = ages.map((n) => `<i style="height:${Math.round((n / max) * 100)}%"></i>`).join("");
  const flags = r ? r.new_conflicts.length + r.stale.length + r.mutated.length + r.broken.length : 0;
  $("check-foot").textContent = r ? `${pad(r.scanned)} CHECKS · ${pad(flags, 2)} FLAGS · ${r.orphans.length} ORPHANS` : "NOT RUN YET";
  $("check-state").textContent = r ? (flags ? "FLAGS" : "CLEAN") : "WAIT";
}

// knowledge mix: how many records sit in each cluster
function renderMix() {
  const cl = scene.clusters, max = Math.max(1, ...cl.map((c) => c.n));
  const total = cl.reduce((s, c) => s + c.n, 0);
  $("mix").innerHTML = cl.length ? cl.slice().sort((a, b) => b.n - a.n).slice(0, 9).map((c) => {
    const on = view.scn === "c:" + c.key;
    return `<li data-key="${esc(c.key)}" class="${on ? "on" : ""}" tabindex="0" role="button" title="Focus ${esc(c.label)}"><span class="lab">${esc(c.label)}</span><span class="bar"><i style="width:${Math.max(3, Math.round((c.n / max) * 100))}%;background:${rgba(c.base, 0.9)}"></i></span><span class="n">${c.n.toLocaleString()}</span></li>`;
  }).join("") : `<li class="empty"><span>no clusters yet</span></li>`;
  $("mix-tag").textContent = `${total.toLocaleString()} ITEMS`;
  $("mix-foot").textContent = cl.length ? `${cl.length} CLUSTERS · click a row to focus it` : "empty";
}
function mixPick(e) {
  const li = e.target.closest("li[data-key]");
  if (!li) return;
  const id = "c:" + li.dataset.key;
  applyScenario(view.scn === id ? "all" : id);
}
$("mix").addEventListener("click", mixPick);
$("mix").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); mixPick(e); } });

// age: claim age buckets (knowledge) and the last 24 hours of memory, with a freshness gauge
function renderAge() {
  const st = view.snap ? view.snap.stats : {}, ages = view.snap ? view.snap.ages : new Array(24).fill(0);
  const hours = new Array(24).fill(0), now = Date.now();
  for (const n of view.memNodes) {
    if (n.mkind !== "item" || !n.created_at) continue;
    const h = Math.floor((now - Date.parse(n.created_at)) / 3.6e6);
    if (h >= 0 && h < 24) hours[23 - h]++;
  }
  const memTotal = view.memNodes.filter((n) => n.mkind === "item").length, mem24 = hours.reduce((a, b) => a + b, 0);
  const claims = st.claims || 0;
  const fresh = claims ? Math.max(0, claims - (st.conflicts || 0) - (st.unconfirmed || 0)) / claims : memTotal ? mem24 / memTotal : null;
  const label = claims ? "CONFIRMED" : "MEMORY ≤24 H";
  const p = fresh === null ? 0 : clamp(fresh, 0, 1);
  const ex = (50 - 40 * Math.cos(Math.PI * p)).toFixed(2), ey = (55 - 40 * Math.sin(Math.PI * p)).toFixed(2);
  const gauge = `<svg viewBox="0 0 100 62" class="gauge" aria-hidden="true"><path d="M10 55 A40 40 0 0 1 90 55" class="trk"/>${p > 0.005 ? `<path d="M10 55 A40 40 0 0 1 ${ex} ${ey}" class="val"/>` : ""}
    <text x="50" y="50" text-anchor="middle" class="num">${fresh === null ? "—" : Math.round(p * 100) + "%"}</text></svg>`;
  const maxH = Math.max(1, ...hours), maxA = Math.max(1, ...ages);
  const line = hours.map((v, i) => `${(i / 23) * 118 + 1},${(26 - (v / maxH) * 24).toFixed(1)}`).join(" ");
  const bars = ages.map((v, i) => `<rect x="${(i * 5).toFixed(1)}" y="${(26 - (v / maxA) * 24).toFixed(1)}" width="3.4" height="${Math.max(0.8, (v / maxA) * 24).toFixed(1)}"/>`).join("");
  $("age").innerHTML = `<div class="age-top"><div class="g">${gauge}<small>${label}</small></div>
    <div class="sp"><small>MEMORY / HOUR · 24 H · NOW RIGHT</small><svg viewBox="0 0 120 28" preserveAspectRatio="none" class="spark"><polyline points="${line}"/></svg>
    <small>CLAIM AGE · 2-DAY BUCKETS · NEWEST LEFT</small><svg viewBox="0 0 120 28" preserveAspectRatio="none" class="abars">${bars}</svg></div></div>
    <dl class="age-stats"><div><dt>PAGES</dt><dd>${(st.pages || 0) + (st.answers || 0)}</dd></div><div><dt>CLAIMS</dt><dd>${claims}</dd></div>
    <div><dt>CONFLICTS</dt><dd class="${st.conflicts ? "warn" : ""}">${st.conflicts || 0}</dd></div><div><dt>UNCONF.</dt><dd>${st.unconfirmed || 0}</dd></div></dl>`;
  $("age-tag").textContent = `${mem24} IN 24 H`;
  $("age-foot").textContent = claims ? `${claims} claims · ${st.conflicts || 0} conflicts · ${st.unconfirmed || 0} unconfirmed` : `0 claims · memory ${memTotal.toLocaleString()} items · ${mem24} new in 24 h`;
}

/* ---------- live data ---------- */
let stateTimer = 0, graphTimer = 0, seenScene = "", seenPanel = "";
// No vault configured makes the endpoint answer 400: ask again only now and then, not every refresh.
const vaultWanted = () => !view.vaultFailed || Date.now() - view.vaultFailed > 600000;
let refreshBusy = false, refreshQueued = 0;
async function refresh(withGraph) {
  if (refreshBusy) { refreshQueued = Math.max(refreshQueued, withGraph ? 2 : 1); return; }
  refreshBusy = true;
  try {
    const [snap, graph, vault, heap] = await Promise.all([api("/api/kb/state"), withGraph ? api("/api/kb/graph") : null,
      withGraph && vaultWanted() ? api("/api/vault/graph").catch(() => { view.vaultFailed = Date.now(); return null; }) : null, withGraph ? api("/api/graph/heap").catch(() => null) : null]);
    if (graph) {
      const sceneKey = sigOf(graph) + "|" + sigOf(vault) + "|" + sigOf(heap);
      if (sceneKey !== seenScene) {
        seenScene = sceneKey;
        ingestKB(graph, vault); if (heap) ingestHeap(heap);
        buildScene(); renderTabs();
      }
    }
    view.snap = snap; view.loaded = true;
    const panelKey = sigOf(snap) + "|" + seenScene + "|" + Math.floor(Date.now() / 3600000);
    if (panelKey !== seenPanel) { seenPanel = panelKey; renderAll(); }
  } catch (err) {
    view.last = `jarvis :: ${err.message}`;
  } finally {
    refreshBusy = false;
    if (refreshQueued) { const again = refreshQueued > 1; refreshQueued = 0; refresh(again); }
  }
}
function soon(withGraph) {
  clearTimeout(stateTimer);
  if (withGraph) { clearTimeout(graphTimer); graphTimer = setTimeout(() => refresh(true), 350); }
  else stateTimer = setTimeout(() => refresh(false), 250);
}
let memBusy = false, memAgain = false;
async function refreshMemory() {  // JARVIS's own memory moves on its own: redraw when it does
  if (memBusy) { memAgain = true; return; }
  memBusy = true;
  try {
    const heap = await api("/api/graph/heap");
    const sig = `${(heap.nodes || []).length}:${(heap.edges || []).length}:${heap.nodes && heap.nodes[0] ? heap.nodes[0].id : ""}`;
    if (sig === view.heapSig) return;
    ingestHeap(heap); buildScene(); renderTabs(); renderAll();
    seenPanel = "";
  } catch { /* the console server may be restarting */ }
  finally {
    memBusy = false;
    if (memAgain) { memAgain = false; refreshMemory(); }
  }
}

function onEvent(kind, data) {
  const action = kind.slice(3);
  const target = String(data.target || "");
  view.last = `${data.bot || "jarvis"} :: ${action}${target ? " · " + target : ""}`;
  for (const id of ["reader", "librarian", "checker"]) $("role-" + id).classList.toggle("on", data.bot === id);
  if (data.bot && data.bot !== "you") $("command-who").textContent = who(data.bot);
  const page = target.match(/^wiki\/(.+)\.md$/), raw = target.match(/^raw\/(source-\d+)/);
  const id = page ? "p:" + page[1] : raw ? "s:" + raw[1] : null;
  const tag = TAGS[action];
  const pulse = () => {
    if (!id || !tag || !view.byId.has(id)) return;
    const prior = view.pulses.find((p) => p.id === id);
    if (prior && prior.tag === "CONFLICT" && tag !== "CONFLICT") return;  // a flag outranks a patch
    view.pulses = view.pulses.filter((p) => p.id !== id).concat({ id, tag, t: performance.now() }).slice(-4);
    poke();
  };
  if (["ingest", "create", "patch", "commit", "answer", "audit", "conflict", "flag", "resolve", "merge", "sync"].includes(action)) {
    soon(true);
    setTimeout(pulse, 450);
  } else soon(false);
  if (action === "error") view.last = `jarvis :: ${data.message}`;
}

function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (msg) => {
    try {
      const ev = JSON.parse(msg.data);
      if (typeof ev.kind === "string" && ev.kind.startsWith("kb.")) onEvent(ev.kind, ev.data || {});
    } catch { /* keep-alive or a malformed line */ }
  };
  es.onerror = () => { $("mode").textContent = "OFFLINE"; };
  es.onopen = () => { $("mode").textContent = "LIVE"; };
}

$("command").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("command-input"), text = input.value.trim();
  if (!text) return;
  $("last-cmd").textContent = "> " + text;
  input.value = "";
  try { await api("/api/kb/command", { text }); }
  catch (err) { view.last = `jarvis :: ${err.message}`; $("last-cmd").textContent = "! " + err.message; }
});

document.querySelectorAll("[data-layer]").forEach((b) => b.addEventListener("click", () => {
  const key = b.dataset.layer;
  view.layers[key] = !view.layers[key];
  b.setAttribute("aria-pressed", String(view.layers[key]));
  buildScene(); renderTabs(); renderAll();
}));

resize();
renderTabs();
refresh(true);
connect();
let graphPoll = 0, memPoll = 0;
function markVis() {
  const root = document.documentElement;
  if (!root || !root.setAttribute) return;
  if (document.hidden) root.setAttribute("data-vis", "hidden");
  else root.removeAttribute("data-vis");
}
function armKnowledgePolls() {
  if (graphPoll) clearInterval(graphPoll);
  if (memPoll) clearInterval(memPoll);
  graphPoll = memPoll = 0;
  if (document.hidden) return;
  graphPoll = setInterval(() => refresh(true), 60000);  // the nightly checker may run in another process
  memPoll = setInterval(() => refreshMemory(), 15000);
}
document.addEventListener("visibilitychange", () => {
  markVis();
  armKnowledgePolls();
  if (!document.hidden) { refresh(true); refreshMemory(); }  // catch up once on return
});
markVis();
armKnowledgePolls();
function armGraphLoop() {
  if (!graphIo && typeof IntersectionObserver !== "undefined") {
    const host = canvas.parentElement || canvas;
    graphIo = new IntersectionObserver((entries) => {
      graphSeen = entries.some((e) => e.isIntersecting);
      if (graphSeen) kickFrame(); else stopFrame();
    });
    graphIo.observe(host);
  }
  kickFrame();
}
function disarmGraphLoop() {  // page teardown: drop the observer and the frame
  stopFrame();
  if (graphIo) { graphIo.disconnect(); graphIo = null; }
}
armGraphLoop();
addEventListener("pagehide", disarmGraphLoop);
addEventListener("pageshow", (ev) => { if (ev.persisted) armGraphLoop(); });
document.addEventListener("visibilitychange", () => { if (document.hidden) stopFrame(); else kickFrame(); });
