/* JARVIS system design: the request path in plain words, live health with fixes,
   where data lives, and the real module map underneath. Everything is read-only. */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const bytes = (n) => (n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n >= 1e3 ? Math.round(n / 1e3) + " KB" : n + " B");
const RANK = { fail: 2, warn: 1, ok: 0 };
const MARK = { ok: "✓", warn: "!", fail: "✕" };
const TIER_WORDS = {
  simple: (r) => `A quick answer from the <b>${esc(r.handler || "brain")}</b>, with your notes and wiki as background. Clock and calendar questions skip the AI entirely.`,
  medium: (r) => `Needs a website, so it goes to <b>${esc(r.handler || "a web agent")}</b>: a read-only run that can browse but not log in.`,
  complex: () => `A big job. A planner splits it into specialist steps and <b>waits for you to press Run</b> before anything executes.`,
};
const REASONS = {  // the router's reasons, said plainly
  "conversation (auto_escalate off)": "you're talking to it and auto-escalate is off, so it stays a quick answer",
  "multi-part build or production work": "it sounds like a multi-part build",
  "long brief": "it's a long brief",
  "needs a website or logged-in account": "it needs a website or an account",
  "single self-contained request": "it's one self-contained request",
  "owner override": "you picked the tier yourself",
};
let data = null, open = null, lastPath = null;

async function api(path) {
  const res = await fetch(path);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

function worst(checks) {
  return checks.reduce((w, c) => (RANK[c.state] > RANK[w] ? c.state : w), "ok");
}

/* ---------- 01: the flow ---------- */
function renderFlow() {
  const main = data.stations.filter((s) => !s.side), side = data.stations.filter((s) => s.side);
  const card = (s, i) => {
    const mine = data.checks.filter((c) => c.station === s.id), state = mine.length ? worst(mine) : "ok";
    const problem = mine.filter((c) => c.state !== "ok").sort((a, b) => RANK[b.state] - RANK[a.state])[0];
    return `<li class="station" data-id="${s.id}" tabindex="0" role="button" aria-expanded="${open === s.id}"
        aria-label="${esc(s.title)}: ${esc(s.plain)}">
      <div class="num"><span>${s.side ? "ALSO" : String(i + 1).padStart(2, "0")} · ${esc((s.verb || "").toUpperCase())}</span><span class="dot ${state}" title="${state}"></span></div>
      <h3>${esc(s.title)}</h3>
      <p class="plain">${esc(s.plain)}</p>
      ${problem ? `<p class="issue ${problem.state}">${MARK[problem.state]} ${esc(problem.label)}</p>` : ""}
      <p class="meta">${s.modules.length} modules · ${s.lines.toLocaleString()} lines</p>
    </li>`;
  };
  $("flow").innerHTML = main.map(card).join("") + `<span class="packet" id="packet" aria-hidden="true"></span>`;
  $("side").innerHTML = side.map((s) => card(s, 0).replace("<li", "<div").replace("</li>", "</div>")).join("");
  document.querySelectorAll(".station").forEach((el) => {
    const go = () => toggle(el.dataset.id);
    el.addEventListener("click", go);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  });
}

function toggle(id) {
  open = open === id ? null : id;
  document.querySelectorAll(".station").forEach((el) => el.setAttribute("aria-expanded", String(el.dataset.id === open)));
  const drawer = $("drawer");
  if (!open) { drawer.hidden = true; return; }
  const s = data.stations.find((x) => x.id === open);
  const mods = data.modules.filter((m) => m.station === open).sort((a, b) => b.lines - a.lines);
  const checks = data.checks.filter((c) => c.station === open);
  drawer.innerHTML = `<h3>${esc(s.title)}</h3><p class="plain">${esc(s.plain)}</p>
    <div class="cols">
      <div><h4>THE CODE</h4><ul class="mods">${mods.map((m) => `<li><b>${esc(m.name)}.py</b><i>${m.lines} ln</i><span>${esc(m.doc || "—")}</span></li>`).join("")}</ul></div>
      <div><h4>WHAT IT NEEDS</h4>${checks.length ? `<ul class="checks" style="grid-template-columns:1fr">${checks.map(checkItem).join("")}</ul>` : `<p class="plain">Nothing outside the code.</p>`}</div>
    </div>`;
  drawer.hidden = false;
  wireCopy(drawer);
  drawer.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
}

/* the route tester lights up the stations a request would pass through */
let packetTimer = 0;
async function trace(text) {
  try {
    const r = await api("/api/route?text=" + encodeURIComponent(text));
    const path = data.paths[r.tier] || [];
    $("verdict").innerHTML = `<span class="tier">${esc(r.tier.toUpperCase())}</span>${(TIER_WORDS[r.tier] || (() => ""))(r)}
      <span class="dim">· because ${esc((r.reasons || []).map((x) => REASONS[x] || x).join(", "))}.</span>`;
    light(path);
    runPacket(path);
  } catch (err) {
    $("verdict").textContent = "Couldn't trace that: " + err.message;
  }
}
function light(path) {
  lastPath = path;
  $("flow").classList.add("tracing");
  document.querySelectorAll("#flow .station").forEach((el) => el.classList.toggle("lit", path.includes(el.dataset.id)));
}
function runPacket(path) {
  clearInterval(packetTimer);
  const packet = $("packet"), cards = [...document.querySelectorAll("#flow .station")];
  if (!packet || !cards.length || getComputedStyle(packet).display === "none") return;
  const stops = cards.filter((c) => path.includes(c.dataset.id));
  const flowBox = $("flow").getBoundingClientRect();
  let i = 0;
  packet.style.opacity = "1";
  const step = () => {
    if (i >= stops.length) { clearInterval(packetTimer); packet.style.opacity = "0"; return; }
    const box = stops[i].getBoundingClientRect();
    packet.style.transition = i ? "left .55s ease" : "none";
    packet.style.left = `${box.left - flowBox.left + box.width / 2 - 4}px`;
    i++;
  };
  step();
  packetTimer = setInterval(step, 600);
}

/* ---------- 02: health ---------- */
function checkItem(c) {
  const station = data.stations.find((s) => s.id === c.station);
  return `<li class="check ${c.state}"><span class="mark" aria-label="${c.state}">${MARK[c.state]}</span>
    <div><b>${esc(c.label)}</b><span class="where">${esc((station?.title || c.station).toUpperCase())}</span></div>
    <span class="detail">${esc(c.detail)}</span>
    ${c.fix ? `<span class="fix"><code>${esc(c.fix)}</code><button type="button" class="copy" data-copy="${esc(c.fix)}">COPY</button></span>` : ""}</li>`;
}
function wireCopy(root) {
  root.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "COPIED"; }
    catch { b.textContent = "SELECT IT"; }
    setTimeout(() => (b.textContent = "COPY"), 1600);
  }));
}
function renderChecks() {
  const list = data.checks.slice().sort((a, b) => RANK[b.state] - RANK[a.state]);
  $("checks").innerHTML = list.map(checkItem).join("");
  wireCopy($("checks"));
  const n = (s) => data.checks.filter((c) => c.state === s).length;
  $("score").innerHTML = `<span class="ok">${n("ok")} WORKING</span><span class="warn">${n("warn")} TO IMPROVE</span><span class="fail">${n("fail")} BROKEN</span>`;
}

/* ---------- 03: stores ---------- */
function renderStores() {
  $("stores").innerHTML = data.stores.map((s) => `<div class="store"><h3>${esc(s.name)}<span>${bytes(s.bytes)}</span></h3>
    <p>${esc(s.holds)}</p>${s.count ? `<p class="count">${esc(s.count)}</p>` : ""}<p class="path" title="${esc(s.path)}">${esc(s.path)}</p></div>`).join("");
}

/* ---------- 04: code map ---------- */
function renderSetup() {
  const s = data.setup;
  const claude = Object.entries(s.claude || {}).map(([k, v]) => `${k} ${v}`).join(" / ");
  $("setup").innerHTML = [["brain", `${s.brain} · ${s.chat_model}`], ["claude tiers", claude], ["voice", `${s.voice} (${s.kokoro_voice})`],
    ["speech-to-text", s.stt], ["python", s.python], ["platform", s.platform]]
    .map(([k, v]) => `<span>${esc(k)} <b>${esc(v)}</b></span>`).join("");
}
function renderCodemap() {
  const cols = data.stations, BW = 132, BH = 34, GX = 22, GY = 10, TOP = 30;
  const pos = new Map();
  let H = 0;
  cols.forEach((s, ci) => {
    const mods = data.modules.filter((m) => m.station === s.id).sort((a, b) => b.lines - a.lines);
    mods.forEach((m, ri) => pos.set(m.name, { x: 12 + ci * (BW + GX), y: TOP + ri * (BH + GY), m }));
    H = Math.max(H, TOP + mods.length * (BH + GY));
  });
  const W = 24 + cols.length * (BW + GX) - GX;
  const svg = $("codemap");
  svg.setAttribute("viewBox", `0 0 ${W} ${H + 10}`);
  svg.style.minWidth = Math.min(W, 1400) + "px";
  const heads = cols.map((s, ci) => `<text class="col" x="${12 + ci * (BW + GX)}" y="16">${esc(s.title.toUpperCase())}</text>`).join("");
  const edges = [];
  for (const m of data.modules) for (const t of m.imports) {
    const a = pos.get(m.name), b = pos.get(t);
    if (!a || !b) continue;
    const x1 = a.x + BW, y1 = a.y + BH / 2, x2 = b.x, y2 = b.y + BH / 2;
    const back = x2 <= a.x;  // an import pointing left loops around the bottom
    const d = back ? `M${a.x + BW / 2},${a.y + BH} C${a.x + BW / 2},${H + 6} ${b.x + BW / 2},${H + 6} ${b.x + BW / 2},${b.y + BH}`
      : `M${x1},${y1} C${x1 + 40},${y1} ${x2 - 40},${y2} ${x2},${y2}`;
    edges.push(`<path class="edge" data-s="${esc(m.name)}" data-t="${esc(t)}" d="${d}"/>`);
  }
  const boxes = [...pos.values()].map(({ x, y, m }, i) => `<g class="mod" data-id="${esc(m.name)}" transform="translate(${x},${y})" tabindex="${i ? -1 : 0}"
      aria-label="${esc(m.name)}.py, ${m.lines} lines">
      <rect width="${BW}" height="${BH}" rx="2"/><text x="8" y="14">${esc(m.name)}.py</text><text class="sub" x="8" y="27">${m.lines} lines · ${m.imports.length}→ ←${m.used_by.length}</text></g>`).join("");
  svg.innerHTML = heads + `<g>${edges.join("")}</g>` + boxes;
  const els = [...svg.querySelectorAll(".mod")];
  const focus = (name) => {
    svg.classList.toggle("focus", !!name);
    const near = new Set();
    svg.querySelectorAll(".edge").forEach((p) => {
      const hot = p.dataset.s === name || p.dataset.t === name;
      p.classList.toggle("hot", hot);
      if (hot) { near.add(p.dataset.s); near.add(p.dataset.t); }
    });
    els.forEach((el) => { el.classList.toggle("hot", el.dataset.id === name); el.classList.toggle("near", near.has(el.dataset.id)); });
    if (!name) return;
    const m = data.modules.find((x) => x.name === name);
    $("module-note").innerHTML = `<b>${esc(m.name)}.py</b> — ${esc(m.doc || "no description")}<br>
      uses: ${esc(m.imports.join(", ") || "nothing else in JARVIS")} · used by: ${esc(m.used_by.join(", ") || "nothing")}
      ${m.public.length ? `<br>offers: ${esc(m.public.slice(0, 8).join(", "))}` : ""}`;
  };
  els.forEach((el, i) => {
    el.addEventListener("mouseenter", () => focus(el.dataset.id));
    el.addEventListener("focus", () => focus(el.dataset.id));
    el.addEventListener("mouseleave", () => focus(null));
    el.addEventListener("keydown", (e) => {
      const step = { ArrowDown: 1, ArrowUp: -1, ArrowRight: 4, ArrowLeft: -4 }[e.key];
      if (step === undefined) return;
      e.preventDefault();
      const next = els[Math.max(0, Math.min(els.length - 1, i + step))];
      el.tabIndex = -1; next.tabIndex = 0; next.focus();
    });
  });
}

async function load() {
  try {
    data = await api("/api/system/design");
  } catch (err) {
    $("score").textContent = "Couldn't read the system: " + err.message;
    return;
  }
  renderFlow(); renderChecks(); renderStores(); renderSetup(); renderCodemap();
  if (lastPath) light(lastPath);
  if (open) { const was = open; open = null; toggle(was); }
}

$("tester").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("try").value.trim();
  if (text) trace(text);
});
load();
setInterval(load, 30000);  // health is live: start the listener and watch it go green
