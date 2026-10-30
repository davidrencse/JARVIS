// The pipeline wheel: every stage a request passes through, drawn as one ring segment that
// carries that stage's latest lines. Live turn.* and voice.* events light the stage that is
// working, fire rays out of it and pull a bundle in from the memory tangle; the trace panel
// and the status line say the same thing in words. app.js owns the data; this file only draws.
(function () {
  "use strict";
  const J = window.Jarvis;
  const canvas = document.getElementById("lab-wheel");
  if (!J || !canvas) return;
  const $ = (s) => document.querySelector(s);
  const on = (name, fn) => addEventListener("jarvis:" + name, (e) => fn(e.detail));
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const ctx = canvas.getContext("2d");
  // The memory tangle runs on the GPU when it can (field.js); the 2D canvas then draws only
  // the ring, rays and labels on a transparent layer above it.
  const field = window.JarvisField ? window.JarvisField(canvas.parentElement, canvas) : null;
  let fieldBusy = false, inkRGB = [1, 1, 1], bgRGB = [0, 0, 0], dark = true;
  function rgb(color) {
    const probe = document.createElement("canvas").getContext("2d");
    probe.fillStyle = color; const hex = probe.fillStyle;
    return hex.startsWith("#") ? [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255) : [1, 1, 1];
  }

  // id, label, what it does (seed lines until live lines replace them)
  const STAGES = [
    ["voice", "Hear", ["listen.py", "silero vad", "speaker id · eres2net", "whisper", "wake word: jarvis"]],
    ["intake", "Intake", ["turn.start", "channel · voice/console", "remember inbound"]],
    ["recall", "Recall", ["vault · wiki · heap", "bm25 + vectors", "rrf fusion"]],
    ["route", "Route", ["router.py", "tier: instant/simple/web", "reasons"]],
    ["instant", "Instant", ["instant.py", "clock · date · vault", "0 tokens"]],
    ["parse", "Parse", ["parse.py", "strict grammar", "typed intents", "0 tokens"]],
    ["gate", "Gates", ["gates.py", "target exists", "risk level", "u-gates"]],
    ["hist", "History", ["history.py", "h1 duplicate", "h3 contradiction"]],
    ["confirm", "Confirm", ["confirm.py", "risk ratchet", "ask david first"]],
    ["run", "Execute", ["remote.call", "run here / node", "cluster hub"]],
    ["obs", "Observe", ["observe.py", "read screen back", "verified / uncertain"]],
    ["brain", "Brain", ["chatgpt lane", "claude failover", "tools"]],
    ["adapter", "Agents", ["adapters", "browser agents", "muse"]],
    ["grok", "Grok", ["grok preflight", "ten checks", "grok bot"]],
    ["plan", "Plan", ["orchestrator.py", "specialists", "subtasks"]],
    ["task", "Task", ["runner.py", "codex / claude cli", "ledger"]],
    ["memory", "Remember", ["memory.sqlite3", "history.sqlite3", "events.jsonl"]],
    ["speak", "Speak", ["kokoro", "sentence stream", "on it."]],
  ].map(([id, label, seed], i) => ({ id, label, i, lines: seed.slice(), heat: 0, state: "", total: 0, last: "" }));
  const byId = Object.fromEntries(STAGES.map((s) => [s.id, s]));

  function stageOf(id, parent) {
    const head = String(id || "").split(":")[0];
    if (head.startsWith("grok")) return byId.grok;
    if (head === "hist") return byId.hist;
    if (byId[head]) return byId[head];
    if (parent === "brain") return byId.brain; // tool calls inside a brain turn
    if (head.startsWith("plan")) return byId.plan;
    return byId.route;
  }

  // ---------- geometry and the static memory tangle (redrawn only on resize/theme) ----------
  let W = 0, H = 0, dpr = 1, G = null, tangle = null, ink = "#ededed", bg = "#000";
  function rng(seed) { return () => ((seed = (seed * 16807) % 2147483647) / 2147483647); }
  function layout() {
    const box = canvas.getBoundingClientRect();
    dpr = Math.min(2, devicePixelRatio || 1);
    W = Math.max(1, box.width); H = Math.max(1, box.height);
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    const css = getComputedStyle(document.documentElement);
    ink = css.getPropertyValue("--ink").trim() || "#ededed";
    bg = css.getPropertyValue("--bg").trim() || "#000";
    const narrow = W < 640;
    const R = Math.max(H * 0.92, W * (narrow ? 0.7 : 0.5));
    const cx = W * (narrow ? 0.7 : 0.6) - R, cy = H / 2;
    const band = Math.min(R * 0.3, W * (narrow ? 0.26 : 0.2));
    const span = Math.asin(Math.min(0.97, (H * 0.53) / R));
    G = { R, r: R - band, cx, cy, band, span, step: (2 * span) / STAGES.length };
    if (!view.touched) home();
    inkRGB = rgb(ink); bgRGB = rgb(bg); dark = bgRGB[0] + bgRGB[1] + bgRGB[2] < 1.5;
    field?.layout(W, H, dpr, G);
    zoomLabel?.();
    drawTangle();
  }
  function drawTangle() {
    tangle = document.createElement("canvas");
    tangle.width = canvas.width; tangle.height = canvas.height;
    const t = tangle.getContext("2d"), rand = rng(7 + Math.round(W));
    t.scale(dpr, dpr);
    const items = (J.state.overview?.memory?.items) || 400;
    const n = Math.min(900, 160 + Math.sqrt(items) * 14), pts = [];
    for (let i = 0; i < 140; i++) {
      const a = (rand() - 0.5) * Math.PI * 1.2, d = G.r * (0.15 + 0.82 * Math.sqrt(rand()));
      pts.push([G.cx + Math.cos(a) * d, G.cy + Math.sin(a) * d * 1.05]);
    }
    t.strokeStyle = ink; t.lineWidth = 0.5;
    for (let i = 0; i < n; i++) {
      const a = pts[(rand() * pts.length) | 0], b = pts[(rand() * pts.length) | 0];
      t.globalAlpha = 0.05 + rand() * 0.1;
      t.beginPath(); t.moveTo(a[0], a[1]);
      t.quadraticCurveTo((a[0] + b[0]) / 2 + (rand() - 0.5) * 160, (a[1] + b[1]) / 2 + (rand() - 0.5) * 160, b[0], b[1]);
      t.stroke();
    }
    t.globalAlpha = 0.5; t.fillStyle = ink;
    for (let i = 0; i < 18; i++) { const p = pts[(rand() * pts.length) | 0]; t.beginPath(); t.arc(p[0], p[1], 2.2, 0, 7); t.stroke(); }
    tangle.pts = pts;
  }

  const angleOf = (s) => -G.span + G.step * (s.i + 0.5);
  const polar = (a, d) => [G.cx + Math.cos(a) * d, G.cy + Math.sin(a) * d];

  // ---------- live state ----------
  const bundles = []; // memory → stage, fading
  let active = null, hover = null, busy = false, runSteps = 0, t0 = performance.now();
  // Steps can land within milliseconds of each other; the wheel replays them at a pace
  // the eye can follow (the trace panel and status line stay exact and immediate).
  const queue = []; let pumping = 0, path = [], pathAt = 0;
  function fire(stage, text, state = "run") {
    queue.push([stage, text, state]);
    if (!pumping) pump();
  }
  function pump() {
    const next = queue.shift();
    if (!next) { pumping = 0; return; }
    show(...next);
    pumping = setTimeout(pump, reduce.matches ? 0 : 280);
  }
  function show(stage, text, state) {
    if (path[path.length - 1] !== stage) path.push(stage);
    pathAt = performance.now();
    stage.heat = 1; stage.state = state; stage.total++;
    if (text) { stage.last = text; stage.lines.unshift(text.toLowerCase().slice(0, 48)); stage.lines.length = Math.min(stage.lines.length, 14); }
    active = stage;
    if (field && !reduce.matches) field.burst(...polar(angleOf(stage), G.r - 4), performance.now() / 1000);
    else if (tangle?.pts && !reduce.matches) {
      const p = tangle.pts[(Math.random() * tangle.pts.length) | 0];
      bundles.push({ from: p, stage, born: performance.now() });
      if (bundles.length > 12) bundles.shift();
    }
    tag(stage);
    wake();
  }

  // ---------- drawing ----------
  function frame(now) {
    pending = false;
    // A hidden or collapsed canvas lays out with no room for the ring: arc() would throw.
    if (!G || G.r < 4) return;
    const c = ctx, time = (now - t0) / 1000;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.globalAlpha = 1;
    if (field) c.clearRect(0, 0, W, H); else { c.fillStyle = bg; c.fillRect(0, 0, W, H); }
    c.setTransform(dpr * view.k, 0, 0, dpr * view.k, dpr * view.x, dpr * view.y);
    if (field) fieldBusy = field.draw({ view, time, now: now / 1000, ink: inkRGB, bg: bgRGB, dark });
    else if (tangle) { c.drawImage(tangle, 0, 0, W, H); }

    // rays
    for (const s of STAGES) {
      const a0 = angleOf(s), base = 0.16 + Math.min(0.2, Math.log1p(s.total) * 0.04);
      const len = W * (base + s.heat * 0.4), count = 44 + Math.round(s.heat * 50);
      for (let k = 0; k < count; k++) {
        const jit = ((k * 9301 + s.i * 49297) % 233280) / 233280;
        const a = a0 + (jit - 0.5) * G.step * 0.9;
        const wob = reduce.matches ? 0 : Math.sin(time * 1.3 + k * 1.7 + s.i) * 0.08;
        const L = len * (0.35 + 0.65 * ((k * 7 % 13) / 13) + wob);
        const [x1, y1] = polar(a, G.R + 6), [x2, y2] = polar(a, G.R + 6 + L);
        c.globalAlpha = 0.13 + ((k * 31) % 7) / 60 + s.heat * 0.45 * ((k % 5) / 5 + 0.2);
        c.strokeStyle = ink; c.lineWidth = s.heat > 0.4 && k % 9 === 0 ? 1.4 : 0.6;
        c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
      }
    }

    // ring band: dark cells with mono text
    for (const s of STAGES) {
      const a = angleOf(s), half = G.step / 2 - 0.004;
      c.globalAlpha = 1;
      c.fillStyle = s === active || s === hover ? mix(0.16) : mix(0.05);
      c.beginPath(); c.arc(G.cx, G.cy, G.R, a - half, a + half); c.arc(G.cx, G.cy, G.r, a + half, a - half, true); c.closePath(); c.fill();
      if (s.heat > 0.02 || s === hover) {
        c.globalAlpha = Math.max(s.heat, s === hover ? 0.6 : 0); c.strokeStyle = ink; c.lineWidth = 1; c.stroke();
      }
      // text: lines run radially, stacked across the segment
      c.save();
      c.translate(...polar(a, G.r + 8)); c.rotate(a);
      const fs = Math.max(6.5, Math.min(9, G.band / 18));
      c.font = `400 ${fs}px "Geist Mono", ui-monospace, monospace`;
      c.textBaseline = "middle"; c.fillStyle = ink;
      const arcLen = G.step * (G.r + G.band / 2), rows = Math.max(2, Math.floor(arcLen / (fs + 2.5)) - 1);
      c.globalAlpha = 0.95; c.font = `500 ${fs + 0.5}px "Geist Mono", ui-monospace, monospace`;
      c.fillText(String(s.i + 1).padStart(2, "0") + " " + s.label.toUpperCase() + (s.total ? "  ·" + s.total : ""), 0, -((rows - 1) / 2) * (fs + 2.5));
      c.font = `400 ${fs}px "Geist Mono", ui-monospace, monospace`;
      for (let r = 1; r < rows; r++) {
        const line = s.lines[(r - 1 + (reduce.matches || s.heat < 0.05 ? 0 : Math.floor(time * 2))) % s.lines.length];
        if (!line) continue;
        c.globalAlpha = (r === 1 && s.heat > 0.1 ? 0.95 : 0.42);
        c.fillText(clip(c, line, G.band - 18), 0, (r - (rows - 1) / 2) * (fs + 2.5));
      }
      c.restore();
    }
    // outer ticks
    c.globalAlpha = 0.35; c.strokeStyle = ink; c.lineWidth = 0.6;
    for (let k = 0; k <= 120; k++) {
      const a = -G.span + (2 * G.span * k) / 120, [x1, y1] = polar(a, G.R + 1), [x2, y2] = polar(a, G.R + (k % 10 ? 4 : 9));
      c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
    }
    c.beginPath(); c.arc(G.cx, G.cy, G.r - 3, -G.span, G.span); c.stroke();

    // bundles from memory into the active stage's inner edge
    for (const b of bundles) {
      const age = (now - b.born) / 1600; if (age > 1) continue;
      const a = angleOf(b.stage), [tx, ty] = polar(a, G.r - 4);
      c.strokeStyle = ink; c.lineWidth = 0.7;
      for (let k = 0; k < 22; k++) {
        c.globalAlpha = (1 - age) * 0.55;
        const off = (k - 11) * 1.4;
        c.beginPath(); c.moveTo(b.from[0], b.from[1] + off * 0.4);
        c.quadraticCurveTo((b.from[0] + tx) / 2 + off * 3, (b.from[1] + ty) / 2 - 40 + off * 2, tx, ty + off * 0.6);
        c.stroke();
      }
    }

    // the run's path: stage to stage along the ring's inner edge, fading after the run
    const pathAge = (now - pathAt) / 1000;
    if (path.length > 1 && (busy || pathAge < 6)) {
      c.strokeStyle = ink; c.lineWidth = 1.4; c.globalAlpha = busy ? 0.9 : Math.max(0, 0.9 - pathAge / 6.5);
      c.beginPath();
      path.forEach((s, k) => {
        const [x, y] = polar(angleOf(s), G.r - 14 - (k % 2) * 10);
        if (k) c.lineTo(x, y); else c.moveTo(x, y);
      });
      c.stroke();
      for (const [k, s] of path.entries()) {
        const [x, y] = polar(angleOf(s), G.r - 14 - (k % 2) * 10);
        c.beginPath(); c.arc(x, y, k === path.length - 1 ? 3.5 : 2, 0, 7); c.fillStyle = ink; c.fill();
      }
    }

    // decay
    let hot = path.length > 1 && pathAge < 6;
    for (const s of STAGES) { s.heat *= busy || queue.length ? 0.993 : 0.982; if (s.heat < 0.01) s.heat = 0; else hot = true; }
    if (active && active.heat === 0 && !busy) { active = null; hideTag(); }
    placeTag();
    const moving = (hot || busy || fieldBusy || bundles.some((b) => now - b.born < 1600)) && !reduce.matches;
    schedule(moving ? 0 : field ? 33 : 120);  // the GPU field idles at ~30 fps, the 2D fallback breathes
  }
  function mix(alpha) { return `color-mix(in srgb, ${ink} ${Math.round(alpha * 100)}%, ${bg})`; }
  function clip(c, text, max) {
    if (c.measureText(text).width <= max) return text;
    while (text.length > 3 && c.measureText(text + "…").width > max) text = text.slice(0, -1);
    return text + "…";
  }

  // Idle: a slow breath (one frame every 120 ms) instead of full-rate animation.
  // Busy: every frame. Idle: a slow breath (one frame per 120 ms). Reduced motion: only on change.
  let pending = false, timer = 0;
  function schedule(delay) {
    if (pending) return;
    if (reduce.matches && delay) return;
    pending = true;
    if (delay) timer = setTimeout(() => { timer = 0; requestAnimationFrame(frame); }, delay); else requestAnimationFrame(frame);
  }
  function wake() { if (pending && timer) { clearTimeout(timer); timer = 0; pending = false; } schedule(0); }

  // ---------- tag (hover or active stage) ----------
  const tagEl = $("#lab-tag");
  function tag(s) {
    if (!tagEl) return;
    tagEl.hidden = false; tagEl._s = s; tagEl._node = null;
    tagEl.querySelector("b").textContent = `${String(s.i + 1).padStart(2, "0")} ${s.label}${s.state ? " · " + s.state : ""}`;
    tagEl.querySelector("span").textContent = s.last || s.lines.slice(0, 3).join(" · ");
    placeTag();
  }
  function placeTag() {
    const s = tagEl && !tagEl.hidden && tagEl._s; if (!s || !G) return;
    let [x, y] = toScreen(...polar(angleOf(s), G.R + Math.min(W * 0.18, 150) / view.k));
    x = Math.min(W - tagEl.offsetWidth - 8, Math.max(8, x)); y = Math.min(H - tagEl.offsetHeight - 30, Math.max(8, y - 20));
    tagEl.style.left = x + "px"; tagEl.style.top = y + "px";
  }
  function hideTag() { if (tagEl && !hover) tagEl.hidden = true; }

  // ---------- explore: wheel/pinch to zoom at the cursor, drag to pan, double-click to reset ----------
  const view = { k: 1, x: 0, y: 0, touched: false };
  const K_MIN = 0.5, K_MAX = 8;
  const toScreen = (wx, wy) => [view.x + wx * view.k, view.y + wy * view.k];
  function home() {  // a little zoomed in, the ring's inner edge just left of centre
    view.k = W < 640 ? 1.15 : 1.35;
    view.x = W * 0.42 - view.k * (G.cx + G.r);
    view.y = H / 2 - view.k * G.cy;
  }
  function zoomAt(sx, sy, factor) {
    const k = Math.min(K_MAX, Math.max(K_MIN, view.k * factor)), f = k / view.k;
    view.x = sx - (sx - view.x) * f; view.y = sy - (sy - view.y) * f; view.k = k;
    view.touched = true; zoomLabel(); wake();
  }
  const stage = canvas.parentElement;
  const local = (e) => { const b = canvas.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; };
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const [sx, sy] = local(e);
    zoomAt(sx, sy, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)));
  }, { passive: false });
  const pointers = new Map(); let drag = null;
  stage.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".lab-zoom")) return;
    stage.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, local(e));
    drag = { at: local(e), moved: 0, pinch: pointers.size === 2 ? dist() : 0 };
    stage.classList.add("dragging");
  });
  function dist() { const [a, b] = [...pointers.values()]; return Math.hypot(a[0] - b[0], a[1] - b[1]); }
  stage.addEventListener("pointerup", end); stage.addEventListener("pointercancel", end);
  function end(e) { pointers.delete(e.pointerId); if (!pointers.size) { drag = null; stage.classList.remove("dragging"); } }
  stage.addEventListener("dblclick", (e) => { if (!e.target.closest(".lab-zoom")) reset(); });
  function reset() { view.touched = false; home(); zoomLabel(); wake(); }
  stage.tabIndex = 0;
  stage.addEventListener("keydown", (e) => {
    const step = 40, c = [W / 2, H / 2];
    const act = { "+": () => zoomAt(...c, 1.25), "=": () => zoomAt(...c, 1.25), "-": () => zoomAt(...c, 0.8), "0": reset,
      ArrowLeft: () => pan(step, 0), ArrowRight: () => pan(-step, 0), ArrowUp: () => pan(0, step), ArrowDown: () => pan(0, -step) }[e.key];
    if (act) { e.preventDefault(); act(); }
  });
  function pan(dx, dy) { view.x += dx; view.y += dy; view.touched = true; wake(); }
  const zoomEl = document.createElement("div");
  zoomEl.className = "lab-zoom";
  zoomEl.innerHTML = '<button type="button" data-z="out" aria-label="Zoom out">−</button><output aria-live="polite"></output>'
    + '<button type="button" data-z="in" aria-label="Zoom in">+</button><button type="button" data-z="home" aria-label="Reset view">Reset</button>';
  stage.appendChild(zoomEl);
  zoomEl.addEventListener("click", (e) => {
    const z = e.target.closest("button")?.dataset.z; if (!z) return;
    if (z === "home") reset(); else zoomAt(W / 2, H / 2, z === "in" ? 1.3 : 1 / 1.3);
  });
  function zoomLabel() { zoomEl.querySelector("output").textContent = Math.round(view.k * 100) + "%"; }

  stage.addEventListener("pointermove", (e) => {
    if (!G) return;
    if (drag && pointers.has(e.pointerId)) {
      const p = local(e), prev = pointers.get(e.pointerId);
      pointers.set(e.pointerId, p);
      if (pointers.size === 2 && drag.pinch) {
        const d = dist(), [a, b] = [...pointers.values()];
        zoomAt((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, d / drag.pinch); drag.pinch = d;
      } else { pan(p[0] - prev[0], p[1] - prev[1]); }
      drag.moved += Math.abs(p[0] - prev[0]) + Math.abs(p[1] - prev[1]);
      return;
    }
    const [sx, sy] = local(e);
    const x = (sx - view.x) / view.k - G.cx, y = (sy - view.y) / view.k - G.cy;
    const d = Math.hypot(x, y), a = Math.atan2(y, x);
    const s = d > G.r - 10 && d < G.R + W ? STAGES[Math.floor((a + G.span) / G.step)] : null;
    const hit = !s && field ? field.pick(sx, sy, view) : (field?.unhover(), null);
    if (hit) { nodeTag(hit); hover = null; return; }
    if (s !== hover || tagEl?._node) { hover = s || null; if (hover) tag(hover); else if (active) tag(active); else hideTag(); wake(); }
  });
  canvas.parentElement.addEventListener("pointerleave", () => { hover = null; field?.unhover(); if (active) tag(active); else hideTag(); });
  // Hovering a memory: what it is, where it came from, how connected it is.
  function nodeTag({ node, x, y, links }) {
    if (!tagEl) return;
    tagEl.hidden = false; tagEl._s = null; tagEl._node = node;
    tagEl.querySelector("b").textContent = `Memory · ${node.group || node.kind}${links ? " · " + links + " links" : ""}`;
    tagEl.querySelector("span").textContent = node.label || node.id;
    tagEl.style.left = Math.min(W - tagEl.offsetWidth - 8, x + 14) + "px";
    tagEl.style.top = Math.max(8, Math.min(H - tagEl.offsetHeight - 30, y - 10)) + "px";
    wake();
  }

  // ---------- words: status line, trace panel, header strip ----------
  const stateEl = $("#lab-state"), stepsEl = $("#lab-steps"), metaEl = $("#lab-trace-meta"), bar = $("#lab-progress");
  const steps = new Map();
  function status(text, isBusy) { if (stateEl) { stateEl.textContent = text; stateEl.classList.toggle("busy", !!isBusy); } }
  function renderSteps() {
    if (!stepsEl) return;
    stepsEl.innerHTML = [...steps.values()].map((s, i) =>
      `<li class="${s.state}"><span class="n">${String(i + 1).padStart(2, "0")}</span><span class="k">${J.esc(s.stage)}</span>` +
      `<span class="d">${J.esc(s.label)}${s.detail ? " — " + J.esc(s.detail) : ""}</span><span class="s">${J.esc(s.state)}</span></li>`).join("");
  }
  function step(key, stage, label, detail, state) {
    steps.set(key, { stage: stage.label, label, detail, state }); renderSteps();
    fire(stage, detail ? `${label} · ${detail}` : label, state);
    runSteps++; if (bar) { bar.classList.remove("idle"); bar.style.width = Math.min(92, 8 + runSteps * 9) + "%"; }
    status(`${stage.label} · ${label}${detail ? " — " + detail : ""}`, true);
  }

  let runsToday = 0;
  on("live", (e) => {
    const d = e.data || {};
    switch (e.kind) {
      case "voice.heard": fire(byId.voice, `heard (${d.score}) ${d.text || ""}`, "done"); break;
      case "voice.ignored": byId.voice.total++; byId.voice.heat = Math.max(byId.voice.heat, 0.15); wake(); break;
      case "turn.start":
        busy = true; runSteps = 0; steps.clear();
        path = path[path.length - 1] === byId.voice && performance.now() - pathAt < 4000 ? [byId.voice] : [];  // a spoken turn starts at Hear
        if (metaEl) metaEl.textContent = `${d.channel || "console"} · ${d.tier || "routing"}`;
        step("intake", byId.intake, `“${String(d.text || "").slice(0, 80)}”`, (d.reasons || []).join("; "), "run");
        break;
      case "turn.step": step(d.id, stageOf(d.id, d.parent), d.label || d.id, d.detail, d.state || "done"); break;
      case "turn.confirm": step("confirm", byId.confirm, "Waiting for your yes", d.text || d.summary || "", "run"); break;
      case "turn.sentence": fire(byId.speak, d.text, "run"); break;
      case "turn.done": {
        busy = false; runsToday++;
        for (const s of steps.values()) if (s.state === "run") s.state = "done";
        step("done", byId.memory, "Done", `${d.seconds ?? "?"} s · ${d.tier || ""}`.trim(), "done");
        if (bar) { bar.style.width = "100%"; setTimeout(() => { bar.style.width = ""; bar.classList.add("idle"); }, 900); }
        status(`Done in ${d.seconds ?? "?"} s · ${String(d.reply || "").slice(0, 90)}`, false);
        setTimeout(() => { if (!busy) status("Idle · waiting for “Jarvis, …”", false); }, 12000);
        header();
        break;
      }
    }
  });

  function header(o = J.state.overview) {
    if (!o) return;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    const tasks = o.tasks?.states || {};
    set("lab-s-runs", runsToday ? `${runsToday} today` : `${Object.values(tasks).reduce((a, b) => a + (+b || 0), 0)} total`);
    set("lab-s-memory", (o.memory?.items ?? "—").toLocaleString());
    const devs = o.devices || [], ok = devs.filter((x) => /verified|online|ok/i.test(x.status || "")).length;
    set("lab-s-machines", `${ok}/${devs.length} online`);
    const here = devs.find((x) => x.id === o.cluster?.local_device);
    if (here) set("profile-sub", `${(here.label || here.id).split(",")[0]} · ${o.cluster?.role || "main"}`);
    set("lab-s-voice", o.voice?.listening ? "listening" : "off");
    set("lab-s-brain", `${o.brain?.name || "—"}${o.brain?.warm ? " · warm" : ""}`);
    if (tangle && Math.abs((tangle.items || 0) - (o.memory?.items || 0)) > 50) { tangle.items = o.memory?.items; drawTangle(); }
  }
  on("overview", header);

  // ---------- boot ----------
  if (bar) bar.classList.add("idle");
  const ro = new ResizeObserver(() => { layout(); wake(); });
  ro.observe(canvas.parentElement);
  new MutationObserver(() => { layout(); wake(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) wake(); });
  document.fonts?.ready.then(() => { layout(); wake(); });
  let graphItems = -1;
  async function loadGraph() {
    if (!field) return;
    try { field.setGraph(await J.api("/api/graph/heap")); graphItems = J.state.overview?.memory?.items ?? 0; wake(); }
    catch (e) { /* the 2D ring still works; the next overview retries */ }
  }
  on("overview", (o) => { if (Math.abs((o.memory?.items ?? 0) - graphItems) >= 5) loadGraph(); });
  layout(); header(); wake(); loadGraph();
  window.JarvisWheel = { fire: (id, text, state) => byId[id] && fire(byId[id], text, state), stages: STAGES };
})();
