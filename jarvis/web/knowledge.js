/* JARVIS knowledge engine console: raw/ -> wiki/ as a turning cluster graph.
   Pages are lit spheres, their claims a cloud of red points around them, raw sources
   gold. Reader/librarian/checker events from the server pulse the node they touched. */
"use strict";

const $ = (id) => document.getElementById(id);
const COLORS = { red: "#ff4b5c", pink: "#ff8f9b", gold: "#f4c64e", dim: "#7d7a78", text: "#e9e6e3" };
const TAGS = { ingest: "LOCKED", duplicate: "DUPLICATE", create: "NEW", patch: "PATCH",
  conflict: "CONFLICT", flag: "CONFLICT", answer: "SAVED", audit: "AUDIT", commit: "COMMIT",
  resolve: "RESOLVED", merge: "MERGED" };
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

const view = {
  nodes: [], edges: [], byId: new Map(), pages: [],
  az: -0.13, el: 0.38, zoom: 1, zoomTo: 1, spin: 0, drag: null, hover: null, selected: null,
  pulses: [], snap: null, started: performance.now(), last: "reader :: idle",
  notes: [], flow: [], signals: [], stars: null, claimsOf: new Map(), vault: null,
  layers: { wiki: true, vault: true, claims: true },
};

/* ---------- small helpers ---------- */
function hash(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function rng(seed) {  // mulberry32: stable positions for the same node across reloads
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const pad = (n, w = 3) => String(n).padStart(w, "0");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json", "X-JARVIS": "1" },
    body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

/* ---------- layout: one scene, two constellations ----------
   The wiki (pages, their claim clouds, raw sources) is the core; the Obsidian vault
   orbits it, one cluster per folder. A note the reader has synced is tied to the wiki
   page it became and to its raw source, so you can watch your notes feed the wiki. */
const FOLDERS = { notes: "#ff8f9b", projects: "#ffffff", courses: "#ff4b5c", leetcode: "#f4c64e",
  reference: "#e0a96d", people: "#ffd6da", daily: "#8a3440", templates: "#5a5552", archive: "#4a4644" };
const folderColor = (f) => FOLDERS[f] || "#cfc8c4";
const LINK_STYLE = { has: "rgba(255,75,92,0.08)", related: "rgba(255,143,155,0.22)", link: "rgba(255,214,218,0.13)",
  mirrors: "rgba(244,198,78,0.42)", cites: "rgba(244,198,78,0.05)", ingested: "rgba(244,198,78,0.22)" };

function layout(graph, vault) {
  const old = view.byId;
  const keep = (id) => (old.has(id) ? { x: old.get(id).x, y: old.get(id).y, z: old.get(id).z } : {});
  const nodes = graph.nodes.map((n) => ({ ...n, layer: "wiki", ...keep(n.id) }));
  const edges = graph.edges.slice();
  if (vault) {
    for (const n of vault.nodes) {
      const id = "v:" + n.id;
      nodes.push({ id, kind: "note", layer: "vault", label: n.label, path: n.id, folder: n.folder || "",
        type: n.group, degree: n.degree || 0, updated: n.updated, uri: n.uri, ...keep(id) });
    }
    for (const e of vault.edges) edges.push({ src: "v:" + e.src, dst: "v:" + e.dst, rel: "link" });
  }
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const notes = nodes.filter((n) => n.kind === "note");
  for (const n of notes) {  // bridges into the wiki
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
  for (const e of edges) if (e.rel === "has" && claimsOf.has(e.src)) claimsOf.get(e.src).push(byId.get(e.dst));
  const bySource = new Map();
  for (const e of edges) {
    if (e.rel !== "cites") continue;
    const page = byId.get("p:" + byId.get(e.src).page);
    if (!bySource.has(e.dst)) bySource.set(e.dst, new Set());
    bySource.get(e.dst).add(page);
  }
  const springs = [];
  for (const e of edges) {
    const k = { related: 1, link: 0.5, mirrors: 2.2 }[e.rel];
    if (k) springs.push([byId.get(e.src), byId.get(e.dst), k, e.rel === "mirrors" ? 0.35 : 1.6]);
  }
  for (const set of bySource.values()) {
    const list = [...set];
    for (let i = 1; i < list.length && i < 8; i++) springs.push([list[0], list[i], 0.35, 1.6]);
  }

  // Folder anchors on a ring around the wiki core, biggest folder first.
  const Rw = 120 + 26 * Math.sqrt(pages.length || 1);
  const folders = [...new Set(notes.map((n) => n.folder))]
    .sort((a, b) => notes.filter((n) => n.folder === b).length - notes.filter((n) => n.folder === a).length);
  const ring = Rw + 230 + 10 * Math.sqrt(notes.length);
  const anchor = new Map(folders.map((f, i) => {
    const a = (i / Math.max(1, folders.length)) * Math.PI * 2;
    return [f, { x: Math.cos(a) * ring, y: Math.sin(a * 2) * 50, z: Math.sin(a) * ring }];
  }));

  const sim = pages.concat(notes);
  sim.forEach((p, i) => {
    if (p.x !== undefined) return;
    const r = rng(hash(p.id));
    if (p.kind === "note") {
      const c = anchor.get(p.folder);
      p.x = c.x + (r() - 0.5) * 120; p.y = c.y + (r() - 0.5) * 120; p.z = c.z + (r() - 0.5) * 120;
      return;
    }
    const phi = Math.acos(1 - 2 * (i + 0.5) / Math.max(1, pages.length)), th = Math.PI * (1 + Math.sqrt(5)) * i;
    const rad = Rw * (0.6 + 0.4 * r());
    p.x = rad * Math.sin(phi) * Math.cos(th); p.y = rad * Math.cos(phi) * 0.8; p.z = rad * Math.sin(phi) * Math.sin(th);
  });
  const size = (p) => (p.kind === "note" ? 9 + 3 * Math.sqrt(p.degree || 0) : 16 + 7 * Math.sqrt((claimsOf.get(p.id) || []).length));
  const steps = sim.length > 600 ? 120 : 220;
  for (let step = 0; step < steps; step++) {
    const cool = 1 - step / steps;
    for (let i = 0; i < sim.length; i++) {
      const a = sim[i];
      a.vx = a.vy = a.vz = 0;
      for (let j = 0; j < sim.length; j++) {
        if (i === j) continue;
        const b = sim[j];
        const dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
        const d2 = dx * dx + dy * dy + dz * dz + 1;
        if (d2 > 160000) continue;  // far apart: no push worth computing
        const d = Math.sqrt(d2), want = size(a) + size(b), push = (want * want * 2.2) / d2;
        a.vx += (dx / d) * push; a.vy += (dy / d) * push; a.vz += (dz / d) * push;
      }
      const home = a.kind === "note" ? anchor.get(a.folder) : { x: 0, y: 0, z: 0 };
      const pull = a.kind === "note" ? (a.mirror ? 0.004 : 0.02) : 0.012;
      a.vx += (home.x - a.x) * pull; a.vy += (home.y - a.y) * pull * 1.5; a.vz += (home.z - a.z) * pull;
    }
    for (const [a, b, k, rest] of springs) {
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z, d = Math.hypot(dx, dy, dz) || 1;
      const f = (d - (size(a) + size(b)) * rest) * 0.04 * k;
      a.vx += (dx / d) * f; a.vy += (dy / d) * f; a.vz += (dz / d) * f;
      b.vx -= (dx / d) * f; b.vy -= (dy / d) * f; b.vz -= (dz / d) * f;
    }
    for (const p of sim) {
      const m = Math.min(1, 12 / (Math.hypot(p.vx, p.vy, p.vz) || 1)) * cool;
      p.x += p.vx * m; p.y += p.vy * m; p.z += p.vz * m;
    }
  }
  for (const p of pages) {
    const spread = size(p) * 1.15;
    for (const c of claimsOf.get(p.id) || []) {
      if (c.x !== undefined && old.has(c.id)) continue;
      const r = rng(hash(c.id));
      const u = r() * 2 - 1, t = r() * Math.PI * 2, s = Math.sqrt(1 - u * u), rad = spread * (0.25 + 0.75 * Math.cbrt(r()));
      c.x = p.x + rad * s * Math.cos(t); c.y = p.y + rad * u * 0.85; c.z = p.z + rad * s * Math.sin(t);
    }
  }
  for (const n of nodes) {
    if (n.kind !== "source") continue;
    const users = [...(bySource.get(n.id) || [])].filter(Boolean);
    const r = rng(hash(n.id));
    if (!users.length) { n.x = (r() - 0.5) * Rw; n.y = (r() - 0.5) * Rw; n.z = (r() - 0.5) * Rw; continue; }
    let x = 0, y = 0, z = 0;
    for (const p of users) { x += p.x; y += p.y; z += p.z; }
    x /= users.length; y /= users.length; z /= users.length;
    const len = Math.hypot(x, y, z) || 1, out = 30 + 25 * r();
    n.x = x + (x / len) * out; n.y = y + (y / len) * out; n.z = z + (z / len) * out;
  }
  view.nodes = nodes; view.byId = byId; view.pages = pages; view.notes = notes; view.extent = 0;
  view.edges = edges.map((e) => [byId.get(e.src), byId.get(e.dst), e.rel]).filter(([a, b]) => a && b);
  view.flow = view.edges.filter(([, , r]) => r === "related" || r === "link" || r === "mirrors" || r === "ingested");
  view.claimsOf = claimsOf;
  view.signals = [];
  if (!view.stars) {  // a far shell of dust: it turns with the scene, so depth reads at a glance
    const r = rng(7);
    view.stars = Array.from({ length: 420 }, () => {
      const u = r() * 2 - 1, t = r() * Math.PI * 2, s = Math.sqrt(1 - u * u), rad = 2.4 + r() * 1.6;
      return { ux: s * Math.cos(t) * rad, uy: u * rad, uz: s * Math.sin(t) * rad, b: r() };
    });
  }
  if (view.selected && !byId.has(view.selected.id)) view.selected = null;
  else if (view.selected) view.selected = byId.get(view.selected.id);
}

/* ---------- drawing ---------- */
const canvas = $("graph"), ctx = canvas.getContext("2d");
let W = 0, H = 0, DPR = 1;
function resize() {
  DPR = Math.min(2, window.devicePixelRatio || 1);
  const box = canvas.getBoundingClientRect();
  W = box.width; H = box.height;
  canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}
new ResizeObserver(resize).observe(canvas);

const visible = (n) => view.layers[n.layer] && (n.kind !== "claim" || view.layers.claims);

let cam = null;
function project() {
  const ca = Math.cos(view.az), sa = Math.sin(view.az), ce = Math.cos(view.el), se = Math.sin(view.el);
  if (!view.extent) {  // fit every node once per layout, not per frame
    const d = view.nodes.map((n) => Math.hypot(n.x, n.y, n.z)).sort((a, b) => a - b);
    view.extent = Math.max(120, d[Math.floor(d.length * 0.9)] || 0);
  }
  const extent = view.extent, scale = (Math.min(W * 0.92, H) / (extent * 2.05)) * view.zoom, F = extent * 3;
  cam = (x, y, z, out) => {
    const x1 = x * ca - z * sa, z1 = x * sa + z * ca;
    const y2 = y * ce - z1 * se, z2 = y * se + z1 * ce;
    const p = F / Math.max(F * 0.15, F + z2);
    out.sx = W / 2 + x1 * scale * p; out.sy = H / 2 + y2 * scale * p; out.depth = z2; out.persp = p * view.zoom;
  };
  for (const n of view.nodes) cam(n.x, n.y, n.z, n);
  for (const s of view.stars || []) cam(s.ux * extent, s.uy * extent, s.uz * extent, s);
}

const sprites = new Map();
function hexA(hex, a) {
  const v = parseInt(hex.slice(1), 16);
  return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${a})`;
}
function glow(color, r) {  // pre-rendered halo, drawn additively: cheap bloom
  const key = color + Math.round(r);
  if (!sprites.has(key)) {
    const size = Math.ceil(r * 4) || 1, c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d"), grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, hexA(color, 0.32)); grad.addColorStop(0.22, hexA(color, 0.09)); grad.addColorStop(1, hexA(color, 0));
    g.fillStyle = grad; g.fillRect(0, 0, size, size);
    sprites.set(key, c);
  }
  return sprites.get(key);
}

function claimColor(n) {
  if (n.flag === "conflict") return COLORS.gold;
  if (n.flag === "unconfirmed") return "#6b6866";
  if (n.flag === "superseded") return "#34312f";
  const h = hash(n.id) % 3;
  return h === 0 ? "#ff4b5c" : h === 1 ? "#ff7884" : "#c93a4b";
}
function nodeColor(n) {
  if (n.kind === "note") return folderColor(n.folder);
  if (n.kind === "source") return COLORS.gold;
  if (n.kind === "claim") return claimColor(n);
  return n.status === "review" ? COLORS.gold : n.kind === "answer" ? "#ffffff" : "#ffd6da";
}
function radius(n) {
  if (n.kind === "claim") return Math.max(0.8, 1.9 * n.persp);
  if (n.kind === "source") return Math.max(1.6, 3.2 * n.persp);
  if (n.kind === "note") return Math.max(1.3, (n.folder === "daily" ? 1.4 : 2.2 + Math.sqrt(n.degree) * 0.9) * n.persp);
  return Math.max(3, (3.5 + Math.sqrt(n.claims || 1) * 1.7) * n.persp);
}

function draw(now) {
  project();
  // ground: near-black with a faint warm core
  ctx.globalCompositeOperation = "source-over";
  ctx.fillStyle = "#09090a"; ctx.fillRect(0, 0, W, H);
  const core = ctx.createRadialGradient(W / 2, H / 2, 0, W / 2, H / 2, Math.max(W, H) * 0.6);
  core.addColorStop(0, "rgba(90,24,32,0.22)"); core.addColorStop(1, "rgba(9,9,10,0)");
  ctx.fillStyle = core; ctx.fillRect(0, 0, W, H);
  for (const s of view.stars) {
    ctx.fillStyle = `rgba(233,230,227,${(0.08 + s.b * 0.22) * Math.min(1, s.persp)})`;
    ctx.fillRect(s.sx, s.sy, s.b > 0.85 ? 1.6 : 1, s.b > 0.85 ? 1.6 : 1);
  }
  // links
  ctx.lineWidth = 0.6;
  const focus = view.selected || view.hover;
  for (const rel of ["has", "link", "related", "ingested", "mirrors"]) {
    ctx.strokeStyle = LINK_STYLE[rel]; ctx.beginPath();
    for (const [a, b, r] of view.edges) {
      if (r !== rel || !visible(a) || !visible(b)) continue;
      ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy);
    }
    ctx.stroke();
  }
  if (focus) {  // trace everything the focused node touches
    const pageId = focus.kind === "claim" ? "p:" + focus.page : focus.id;
    ctx.strokeStyle = "rgba(244,198,78,0.6)"; ctx.lineWidth = 0.9; ctx.beginPath();
    for (const [a, b] of view.edges) {
      if (!visible(a) || !visible(b)) continue;
      if (a === focus || b === focus || a.id === pageId || b.id === pageId) { ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); }
    }
    ctx.stroke();
  }
  // signals: sparks running along links, so the graph reads as alive
  ctx.globalCompositeOperation = "lighter";
  if (!reduced && view.flow.length) {
    while (view.signals.length < Math.min(50, view.flow.length)) {
      view.signals.push({ e: view.flow[Math.floor(Math.random() * view.flow.length)], t: 0, v: 0.004 + Math.random() * 0.01 });
    }
    for (const s of view.signals) {
      s.t += s.v;
      const [a, b, rel] = s.e;
      if (s.t >= 1 || !visible(a) || !visible(b)) { s.t = 2; continue; }
      const x = a.sx + (b.sx - a.sx) * s.t, y = a.sy + (b.sy - a.sy) * s.t;
      const tx = a.sx + (b.sx - a.sx) * Math.max(0, s.t - 0.12), ty = a.sy + (b.sy - a.sy) * Math.max(0, s.t - 0.12);
      const hot = rel === "mirrors" || rel === "ingested";
      ctx.strokeStyle = hot ? "rgba(244,198,78,0.55)" : "rgba(255,143,155,0.45)"; ctx.lineWidth = 1.1;
      ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(x, y); ctx.stroke();
      ctx.drawImage(glow(hot ? COLORS.gold : COLORS.pink, 3), x - 6, y - 6, 12, 12);
    }
    view.signals = view.signals.filter((s) => s.t < 1);
  }
  // halos, additively, for everything bigger than a claim
  const order = view.nodes.filter(visible).sort((a, b) => b.depth - a.depth);
  for (const n of order) {
    if (n.kind === "claim" || n.kind === "source" || (n.kind === "note" && n.folder === "daily")) continue;
    const r = radius(n) * (n.kind === "note" ? 2.6 : 2.1);
    ctx.globalAlpha = Math.max(0.15, Math.min(0.8, 1 - n.depth / 1400));
    ctx.drawImage(glow(nodeColor(n), r), n.sx - r * 2, n.sy - r * 2, r * 4, r * 4);
  }
  ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
  // cores, back to front
  for (const n of order) {
    ctx.globalAlpha = Math.max(0.25, Math.min(1, 1.1 - n.depth / 1100));
    const r = radius(n);
    if (n.kind === "page" || n.kind === "answer") {
      const g = ctx.createRadialGradient(n.sx - r * 0.35, n.sy - r * 0.35, r * 0.1, n.sx, n.sy, r);
      g.addColorStop(0, "#ffffff"); g.addColorStop(0.45, nodeColor(n)); g.addColorStop(1, n.status === "unconfirmed" ? "#3a3a3d" : "#7a2832");
      ctx.fillStyle = g;
    } else ctx.fillStyle = nodeColor(n);
    ctx.beginPath(); ctx.arc(n.sx, n.sy, r, 0, Math.PI * 2); ctx.fill();
    if (n.kind === "note" && n.mirror) {  // synced into the wiki: a thin gold ring
      ctx.strokeStyle = "rgba(244,198,78,0.7)"; ctx.lineWidth = 0.8;
      ctx.beginPath(); ctx.arc(n.sx, n.sy, r + 2, 0, Math.PI * 2); ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  ctx.font = "600 34px 'Geist Mono', monospace"; ctx.fillStyle = "rgba(233,230,227,0.08)"; ctx.textAlign = "center";
  ctx.fillText("J A R V I S", W / 2, H - 46);
  // labels: biggest pages and best-linked notes, never on top of each other
  ctx.textAlign = "left"; ctx.font = "10px 'Geist Mono', monospace";
  const many = W < 700 ? 5 : 10;
  const candidates = [
    ...(view.layers.wiki ? view.pages.slice().sort((a, b) => (b.claims || 0) - (a.claims || 0)).slice(0, many) : []),
    ...(view.layers.vault ? view.notes.filter((n) => n.folder !== "daily").sort((a, b) => b.degree - a.degree).slice(0, many) : []),
  ].sort((a, b) => a.depth - b.depth);
  const boxes = [];
  for (const n of candidates) {
    const text = n.kind === "note" ? clip(n.label, 26) : n.label;
    const w = ctx.measureText(text).width + 8, box = [n.sx + 8, n.sy - 7, n.sx + 8 + w, n.sy + 7];
    if (boxes.some((b) => box[0] < b[2] + 4 && box[2] > b[0] - 4 && box[1] < b[3] + 2 && box[3] > b[1] - 2)) continue;
    boxes.push(box);
    label(n, text, n.kind === "note");
  }
  for (const p of view.pulses) {
    const n = view.byId.get(p.id); if (!n) continue;
    const age = (now - p.t) / 1000, warn = p.tag === "CONFLICT";
    for (let k = 0; k < 3; k++) {
      const r = 10 + ((age * 26 + k * 14) % 44);
      ctx.strokeStyle = warn ? `rgba(244,198,78,${0.55 - r / 100})` : `rgba(255,75,92,${0.55 - r / 100})`;
      ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(n.sx, n.sy, r, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.font = "600 11.5px 'Geist Mono', monospace"; ctx.fillStyle = COLORS.gold;
    ctx.fillText(`${n.label} / ${p.tag}`, n.sx + 14, n.sy + 4);
    ctx.font = "10px 'Geist Mono', monospace";
  }
  view.pulses = view.pulses.filter((p) => now - p.t < 9000);
  const card = focus || (view.pulses.length && view.byId.get(view.pulses[view.pulses.length - 1].id));
  if (card && visible(card)) tooltip(card);
}
const clip = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));

function label(n, text, note) {
  const w = ctx.measureText(text).width + 8;
  ctx.fillStyle = "rgba(9,9,10,0.82)"; ctx.fillRect(n.sx + 8, n.sy - 7, w, 14);
  ctx.strokeStyle = note ? hexA(folderColor(n.folder), 0.45) : "rgba(255,255,255,0.12)"; ctx.strokeRect(n.sx + 8, n.sy - 7, w, 14);
  ctx.fillStyle = COLORS.text; ctx.fillText(text, n.sx + 12, n.sy + 3);
}

function tooltip(n) {
  let title, rows;
  if (n.kind === "note") {
    title = n.path;
    rows = [["NOTE", n.label], ["FOLDER · TYPE", `${n.folder || "root"} · ${n.type || "—"}`],
      ["LINKS", String(n.degree)], ["UPDATED", n.updated || "—"],
      ["IN THE WIKI", n.mirror ? "wiki/" + n.mirror.slice(2) + ".md" : n.folder === "daily" ? "daily notes stay out" : "not synced yet"]];
  } else if (n.kind === "source") {
    title = n.label;
    rows = [["ACCESS / RAW ORIGINAL", "READ ONLY · 0 MUTATIONS"], ["SOURCE", n.title], ["LAST CONFIRMED", n.added]];
  } else {
    const page = n.kind === "claim" ? view.byId.get("p:" + n.page) : n;
    let claim = n.kind === "claim" ? n : null;
    if (!claim && page && view.claimsOf.get(page.id)) {
      const list = view.claimsOf.get(page.id);
      claim = list.find((c) => c.flag === "conflict") || list[0];
    }
    title = `wiki/${(page || n).label}`;
    rows = [["CLAIM", claim ? claim.text : page.title], ["SOURCE", claim ? (claim.sources || []).join(" ⇔ ") : "—"],
      ["LAST CONFIRMED", (claim ? claim.confirmed : page.confirmed) + (claim && claim.flag === "conflict" ? " — REVIEW REQUIRED" : claim && claim.flag === "unconfirmed" ? " — UNCONFIRMED" : "")]];
  }
  const width = Math.min(290, W - 24);
  ctx.font = "10px 'Geist Mono', monospace";
  const lines = [];
  for (const [k, v] of rows) {
    lines.push({ k: true, t: k });
    let line = "";
    for (const word of String(v || "").split(" ")) {
      if (ctx.measureText(line + word).width > width - 20 && line) { lines.push({ t: line }); line = ""; }
      line += word + " ";
    }
    if (line.trim()) lines.push({ t: line.trim() });
  }
  lines.splice(14);
  const h = 28 + lines.length * 13;
  let x = n.sx - width - 24, y = n.sy - h - 14;
  if (x < 8) x = Math.min(W - width - 8, n.sx + 20);
  if (y < 26) y = n.sy + 16;
  ctx.fillStyle = "rgba(10,10,11,0.94)"; ctx.fillRect(x, y, width, h);
  ctx.strokeStyle = "rgba(255,255,255,0.18)"; ctx.strokeRect(x, y, width, h);
  ctx.fillStyle = COLORS.text; ctx.font = "600 11px 'Geist Mono', monospace"; ctx.fillText(clip(title, 40), x + 10, y + 17);
  ctx.font = "10px 'Geist Mono', monospace";
  lines.forEach((l, i) => {
    ctx.fillStyle = l.k ? COLORS.dim : l.t.includes("REVIEW REQUIRED") ? COLORS.gold : COLORS.text;
    ctx.fillText(l.t, x + 10, y + 33 + i * 13);
  });
  ctx.strokeStyle = "rgba(244,198,78,0.6)"; ctx.beginPath(); ctx.moveTo(n.sx, n.sy);
  ctx.lineTo(x + (x > n.sx ? 0 : width), y + h / 2); ctx.stroke();
}

/* ---------- loop ---------- */
let frames = 0, fpsAt = performance.now();
function frame(now) {
  if (!view.drag) {  // drift, plus whatever spin a flick left behind, easing out
    view.az += (reduced ? 0 : 0.0011) + view.spin;
    view.spin *= 0.94; if (Math.abs(view.spin) < 1e-5) view.spin = 0;
  }
  view.zoom += (view.zoomTo - view.zoom) * (reduced ? 1 : 0.18);
  if (W && view.nodes.length) draw(now);
  else if (W) { ctx.clearRect(0, 0, W, H); ctx.fillStyle = COLORS.dim; ctx.textAlign = "center";
    ctx.font = "11px 'Geist Mono', monospace"; ctx.fillText("Empty. Type sync to read your Obsidian vault, or ingest https://…", W / 2, H / 2); }
  frames++;
  if (now - fpsAt > 1000) { $("fps").textContent = `${frames}FPS`; frames = 0; fpsAt = now; }
  const deg = (((view.az * 180) / Math.PI + 180) % 360) - 180;
  $("graph-angle").textContent = `AZ ${deg.toFixed(1)}° Z ${Math.round(view.zoom * 100)}`;
  $("ticker").textContent = `${((now - view.started) / 1000).toFixed(2)}   ${view.last}`;
  $("clock").textContent = new Date().toTimeString().slice(0, 8);
  requestAnimationFrame(frame);
}

/* ---------- interaction ---------- */
function nearest(x, y) {
  let best = null, dist = 14;
  for (const n of view.nodes) {
    if (!visible(n)) continue;
    const d = Math.hypot(n.sx - x, n.sy - y) - (n.kind === "claim" ? 0 : 6);
    if (d < dist) { dist = d; best = n; }
  }
  return best;
}
canvas.addEventListener("pointerdown", (e) => { view.spin = 0; view.drag = { x: e.clientX, y: e.clientY, az: view.az, el: view.el, moved: false, vx: 0, t: e.timeStamp }; canvas.setPointerCapture(e.pointerId); });
canvas.addEventListener("pointermove", (e) => {
  const box = canvas.getBoundingClientRect();
  if (view.drag) {
    const dx = e.clientX - view.drag.x, dy = e.clientY - view.drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) view.drag.moved = true;
    const az = view.drag.az + dx * 0.006, dt = Math.max(1, e.timeStamp - view.drag.t);
    view.drag.vx = ((az - view.az) / dt) * 16; view.drag.t = e.timeStamp;
    view.az = az; view.el = Math.max(-1.2, Math.min(1.2, view.drag.el + dy * 0.004));
  } else view.hover = nearest(e.clientX - box.left, e.clientY - box.top);
});
canvas.addEventListener("pointerup", (e) => {
  const box = canvas.getBoundingClientRect();
  if (view.drag && !view.drag.moved) select(nearest(e.clientX - box.left, e.clientY - box.top));
  else if (view.drag && !reduced && e.timeStamp - view.drag.t < 80) view.spin = Math.max(-0.08, Math.min(0.08, view.drag.vx));
  view.drag = null;
});
canvas.addEventListener("pointerleave", () => { view.hover = null; });
const zoomBy = (f) => { view.zoomTo = Math.max(0.4, Math.min(4, view.zoomTo * f)); };
canvas.addEventListener("wheel", (e) => { e.preventDefault(); zoomBy(Math.exp(-e.deltaY * 0.0012)); }, { passive: false });
canvas.addEventListener("keydown", (e) => {
  const k = { ArrowLeft: () => (view.az -= 0.1), ArrowRight: () => (view.az += 0.1), ArrowUp: () => (view.el -= 0.08),
    ArrowDown: () => (view.el += 0.08), "+": () => zoomBy(1.15), "=": () => zoomBy(1.15), "-": () => zoomBy(1 / 1.15),
    Escape: () => select(null) }[e.key];
  if (k) { e.preventDefault(); k(); }
});

function select(n) {
  view.selected = n;
  renderInspector();
  renderIndex();
}

/* ---------- panels ---------- */
function renderAll() {
  const s = view.snap; if (!s) return;
  const st = s.stats;
  const nodes = view.nodes.length, links = view.edges.length;
  $("counts").textContent = `SRC ${pad(st.sources)} / ${st.pages + st.answers} DOCS / VAULT ${view.notes.length} / ${nodes} NODES / ${links} LINKS`;
  $("graph-count").textContent = `GRAPH / ${nodes.toLocaleString()} RECORDS · ${links.toLocaleString()} LINKS`;
  const commit = s.commit;
  $("role-librarian").querySelector("span").textContent = commit ? `${pad(commit.writes.length, 2)}/${pad(commit.writes.length, 2)} COMMITTED` : "00/00 COMMITTED";
  const sched = s.checker.schedule;
  $("role-checker").querySelector("span").textContent = s.checker.last ? `LAST ${s.checker.last.slice(5, 16).replace("T", " ")}` : `QUEUED ${sched.at}`;
  renderChat(); renderQueue(); renderInspector(); renderIndex(); renderRaw(); renderDiff(); renderMatrix(); renderChecker();
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
  $("chat").innerHTML = rows.slice(-9).map(({ e, n }) => `<li class="who-${esc(e.bot)}"><b>${who(e.bot)}</b><span title="${esc(e.message)}">${esc(e.target ? e.target + (n ? ` +${n} more` : "") + " · " : "")}${esc(e.message)}</span></li>`).join("");
  const log = view.snap.log;
  const last = log[log.length - 1];
  $("chat-foot").textContent = last ? `${last.at.slice(11)} · ${last.bot} :: ${last.action}` : "quiet";
}

function renderQueue() {
  const writes = view.snap.writes || [];
  const list = writes.length ? writes : view.snap.pages.slice(0, 12).map((p) => p.slug);
  $("queue").innerHTML = list.slice(0, 11).map((slug, i) => `<li><span class="n">${pad(i + 1, 2)}</span><span>wiki/${esc(slug)}.md</span><span class="bar"></span><span class="ok">OK</span></li>`).join("");
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
  if (!n) { $("inspect").innerHTML = `<p class="dim">Click a node.</p>`; return; }
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
  const claims = (view.claimsOf && view.claimsOf.get(page.id)) || [];
  const claim = n.kind === "claim" ? n : claims.find((c) => c.flag === "conflict") || claims[0];
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

const mini = $("minimap"), mctx = mini.getContext("2d");
function renderIndex() {
  const s = view.snap; if (!s) return;
  $("idx-pages").textContent = s.stats.pages + s.stats.answers;
  $("idx-links").textContent = s.stats.links.toLocaleString();
  const box = mini.getBoundingClientRect(), dpr = Math.min(2, devicePixelRatio || 1);
  mini.width = box.width * dpr; mini.height = box.height * dpr; mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const pages = view.pages;
  const all = pages.concat(view.layers.vault ? view.notes : []); if (!all.length) return;
  const ext = all.reduce((m, p) => Math.max(m, Math.abs(p.x), Math.abs(p.z)), 1);
  const pt = (p) => [box.width / 2 + (p.x / ext) * box.width * 0.45, box.height / 2 + (p.z / ext) * box.height * 0.45];
  mctx.strokeStyle = "rgba(255,143,155,0.25)"; mctx.lineWidth = 0.6; mctx.beginPath();
  for (const [a, b, r] of view.edges) if (r === "related") { const [x1, y1] = pt(a), [x2, y2] = pt(b); mctx.moveTo(x1, y1); mctx.lineTo(x2, y2); }
  mctx.stroke();
  if (view.layers.vault) for (const n of view.notes) {
    const [x, y] = pt(n);
    mctx.fillStyle = folderColor(n.folder); mctx.globalAlpha = 0.7; mctx.fillRect(x - 0.7, y - 0.7, 1.4, 1.4);
  }
  mctx.globalAlpha = 1;
  const sel = view.selected && (view.selected.kind === "claim" ? view.byId.get("p:" + view.selected.page) : view.selected);
  for (const p of pages) {
    const [x, y] = pt(p);
    mctx.fillStyle = p === sel ? COLORS.gold : p.status === "review" ? COLORS.gold : "#d9d4d1";
    mctx.beginPath(); mctx.arc(x, y, p === sel ? 3.2 : 1.8, 0, Math.PI * 2); mctx.fill();
  }
  const i = sel ? pages.indexOf(sel) + 1 : 0;
  $("idx-foot").textContent = `SELECT ${pad(i, 2)}/${pad(pages.length, 2)}${sel ? " · wiki/" + sel.label : ""}`;
}

function renderRaw() {
  const src = view.snap.sources;
  $("raw").innerHTML = src.slice(-7).reverse().map((x) => `<tr><td title="${esc(x.title)}">${esc(x.file)}</td><td class="sha">${esc(x.sha.slice(0, 10))}</td><td class="${x.ok && x.readonly ? "ro" : "bad"}">${x.ok ? (x.readonly ? "RO" : "RW") : "!!"}</td></tr>`).join("");
  const bad = src.filter((x) => !x.ok).length;
  $("raw-mut").textContent = `${bad} MUTATIONS`;
  $("raw-verify").textContent = src.length ? (bad ? `VERIFY FAIL ${bad}` : `VERIFY OK ${pad(src.length)}`) : "VERIFY —";
}

function renderDiff() {
  const d = view.snap.diff;
  if (!d) { $("diff").textContent = ""; $("diff-foot").textContent = "no writes yet"; return; }
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
  el.style.gridTemplateColumns = `70px repeat(${Math.max(1, cols)}, minmax(0, 1fr))`;
  let html = "";
  m.rows.slice(0, 9).forEach((row, i) => {
    html += `<span class="lab" title="${esc(row)}">${esc(row)}</span>`;
    html += m.cells[i].map((v, j) => `<span class="c c${v}" title="${esc(row)} × ${esc(m.cols[j])}"></span>`).join("");
  });
  el.innerHTML = html;
  $("matrix-size").textContent = `${m.rows.length}×${cols}`;
  $("matrix-foot").textContent = `${m.rows.length} PAGES + ${cols} RAW SOURCES`;
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

/* ---------- live data ---------- */
let stateTimer = 0, graphTimer = 0;
async function refresh(withGraph) {
  try {
    const [snap, graph, vault] = await Promise.all([api("/api/kb/state"), withGraph ? api("/api/kb/graph") : null,
      withGraph ? api("/api/vault/graph").catch(() => null) : null]);
    if (graph) layout(graph, vault);
    view.snap = snap;
    renderAll();
  } catch (err) {
    view.last = `jarvis :: ${err.message}`;
  }
}
function soon(withGraph) {
  clearTimeout(stateTimer);
  if (withGraph) { clearTimeout(graphTimer); graphTimer = setTimeout(() => refresh(true), 350); }
  else stateTimer = setTimeout(() => refresh(false), 250);
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
  renderIndex();
}));

resize();
refresh(true);
connect();
setInterval(() => refresh(true), 60000);  // the nightly checker may run in another process
requestAnimationFrame(frame);
