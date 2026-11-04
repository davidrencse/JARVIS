/* JARVIS as an engine: a 3D V8 beside the console's pipeline wheel (three.js, local copy).
   Original design, built from bolt-on modules (engine/modules.js), each a real piece of JARVIS:

     01 intake     requests coming in        07 ECU         desktop control + status lights
     02 block      8 cylinders = 8 stages    08 radiator    health (runs hot on warn/fail)
     03 crank      the gates                 09 alternator  the cluster
     04 turbo      the brain, spools on think 10 fuel       model quota (L3 budget left)
     05 exhaust    replies going out         11 gearbox     actions (drives while a tool runs)
     06 sump       memory (oil level)        12 chassis     the machines it runs on

   This file is the frame around them: renderer and camera, the live state the modules read,
   the data feeds, numbered callout tags, hover, and placement in the wheel's world.
   The engine running = JARVIS working: off and still when idle, revs with activity, winds down
   when the request is done. Monochrome metal, colour means state (amber = activity, green = ok,
   red = error). Renders only while visible, idles at 30 fps, holds still with reduced motion.
   The circuit-board version lives on in board3d.js (the System design page uses it). */
import * as THREE from "./vendor/three-0.170.0.module.min.js";
import { makeKit } from "./engine/kit.js";
import { MODULES } from "./engine/modules.js";

// The wheel's home view puts the ring's inner edge here (fraction of the stage width); wheel.js
// home() uses the same number. The engine fills the open area left of it.
const HOME_EDGE = 0.58;

const host = document.getElementById("board3d");
if (host) start(host);

function start(host) {
  const canvas = host.querySelector("canvas");
  const tip = host.querySelector(".board-tip");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const esc = (v) => (window.JarvisEsc ? window.JarvisEsc(String(v)) : String(v).replace(/[&<>"]/g, ""));
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
  } catch (err) {
    host.querySelector(".board-fallback")?.removeAttribute("hidden");
    return;
  }
  renderer.setPixelRatio(Math.min(2, devicePixelRatio || 1));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.25;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x060606);
  // Isometric, orthographic: a flat 2.5D drawing, framed every draw from the wheel's view so it
  // zooms and pans with the ring instead of sitting on the glass.
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 200);
  const ISO = { az: Math.PI / 4, el: Math.atan(1 / Math.SQRT2) };
  const EYE = new THREE.Vector3(Math.sin(ISO.az) * Math.cos(ISO.el), Math.sin(ISO.el), Math.cos(ISO.az) * Math.cos(ISO.el));
  scene.add(new THREE.HemisphereLight(0xdfe6ee, 0x0b0b0b, 0.7));
  const key = new THREE.DirectionalLight(0xffffff, 2.6); key.position.set(-5, 9, 6); scene.add(key);
  const fill = new THREE.DirectionalLight(0x9fb4c8, 0.8); fill.position.set(6, 3, 5); scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffe2b8, 0.9); rim.position.set(2, 5, -8); scene.add(rim);

  // ---------- live state every module reads ----------
  const state = {
    rpm: 0, rpmTo: 0, until: 0, angle: 0, thinking: 0, boost: 0, reply: 0, intake: 0, error: 0, toolSpin: 0,
    explode: 0, explodeTo: 0, lastStep: "idle", lastTool: "none yet", turns: 0, replies: 0, tools: 0,
    kinds: {}, items: 0, links: 0, brain: "brain", warm: false, listening: false, health: "ok", devices: [], reduced,
  };
  const cards = {};
  const ctx = {
    shared: {},
    rows: (id) => (cards[id] ? cards[id].rows : [["status", "loading"]]),
    card: (id) => (cards[id] ? cards[id].state : "off"),
    notOk: () => Object.values(cards).filter((c) => c.state !== "ok").map((c) => [c.id, c.state]),
  };

  // ---------- build: every module bolts onto one engine group ----------
  const kit = makeKit();
  const engine = new THREE.Group(); scene.add(engine);
  const mods = MODULES.map((def) => {
    const root = new THREE.Group(); root.name = def.id; engine.add(root);
    const built = def.build(kit, root, ctx);
    return { def, root, ...built, focus: kit.outline(root), anchor: new THREE.Vector3(...def.anchor),
             explode: new THREE.Vector3(...(def.explode || [0, 0, 0])), st: "" };
  });

  // Frame the camera on the engine's real bounds, so adding a module never needs new numbers here.
  // Measured twice: assembled, and exploded (every module slid out along its own vector).
  const right = new THREE.Vector3(Math.cos(ISO.az), 0, -Math.sin(ISO.az));
  const upv = new THREE.Vector3().crossVectors(EYE, right).normalize();
  function measure() {
    engine.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(engine);
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let i = 0; i < 8; i++) {
      const p = new THREE.Vector3(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z);
      const u = p.dot(right), v = p.dot(upv);
      x0 = Math.min(x0, u); x1 = Math.max(x1, u); y0 = Math.min(y0, v); y1 = Math.max(y1, v);
    }
    return { x: x1 - x0, y: y1 - y0, centre: new THREE.Vector3().addScaledVector(right, (x0 + x1) / 2).addScaledVector(upv, (y0 + y1) / 2) };
  }
  const span = measure();
  for (const m of mods) m.root.position.copy(m.explode);
  const spread = measure();
  for (const m of mods) m.root.position.set(0, 0, 0);
  const spreadFit = Math.sqrt(Math.min(1, span.x / spread.x, span.y / spread.y));  // shrink part-way: exploded still mostly fits, parts stay readable
  camera.position.copy(EYE).multiplyScalar(40).add(span.centre); camera.lookAt(span.centre);
  const blend = new THREE.Vector3();
  function applyExplode(t) {  // t: 0 assembled .. 1 exploded
    const k = 1 + (spreadFit - 1) * t;
    for (const m of mods) m.root.position.copy(m.explode).multiplyScalar(t);
    engine.scale.setScalar(k);
    blend.copy(span.centre).lerp(spread.centre, t);
    engine.position.copy(span.centre).addScaledVector(blend, -k);
    engine.updateMatrixWorld(true);
  }

  // ---------- gauges strip + callout tags (crisp HTML over the canvas) ----------
  const strip = document.createElement("div"); strip.className = "engine-gauges"; host.appendChild(strip);
  const overlay = document.createElement("canvas"); overlay.className = "engine-strands"; host.appendChild(overlay);
  const octx = overlay.getContext("2d");
  const tagLayer = document.createElement("div"); tagLayer.className = "engine-tags"; host.appendChild(tagLayer);
  for (const m of mods) {
    m.tagEl = document.createElement("span"); m.tagEl.className = "et";
    m.tagEl.innerHTML = `<b>${String(m.def.no).padStart(2, "0")}</b><em>${esc(m.def.name)}</em><i>${esc(m.def.role)}</i>`;
    tagLayer.appendChild(m.tagEl);
    m.tagAt = null;
  }
  function lights() {  // each tag's status light, from its module
    for (const m of mods) {
      let st = "idle";
      try { st = m.def.status ? m.def.status(state, ctx) || "idle" : "idle"; } catch (e) {}
      if (st !== m.st) { m.st = st; m.tagEl.setAttribute("data-st", st); }
    }
  }
  // engine controls sit on the stage, above the wheel's canvas: pull apart, and fill the stage
  const ctlBox = document.createElement("div"); ctlBox.className = "engine-ctl"; ctlBox.setAttribute("role", "group"); ctlBox.setAttribute("aria-label", "Engine view");
  ctlBox.innerHTML = '<button type="button" data-e="explode" aria-pressed="false" title="Pull the engine apart into its modules (E)">Explode</button>'
    + '<button type="button" data-e="fit" title="Zoom so the engine fills the stage (F); Reset or double-click to go back">Fit engine</button>';
  host.parentElement.appendChild(ctlBox);
  const ctl = ctlBox.querySelector('[data-e="explode"]');
  const toggleExplode = () => {
    state.explodeTo = state.explodeTo ? 0 : 1;
    ctl.setAttribute("aria-pressed", String(!!state.explodeTo)); ctl.textContent = state.explodeTo ? "Assemble" : "Explode";
    if (reduced) { state.explode = state.explodeTo; applyExplode(state.explode); frameCamera(); renderNow(); }
    wake(1600);
  };
  const fitEngine = () => {  // the engine's on-screen box (plus its gauges), handed to the wheel to zoom onto
    const wheel = window.JarvisWheel;
    if (!wheel || !wheel.fitRect) return;
    const t = state.explode, [sx, sy] = toScreen(world.ax, world.ay);
    const hw = (ppuNow * (span.x + (spread.x * spreadFit - span.x) * t)) / 2, hh = (ppuNow * (span.y + (spread.y * spreadFit - span.y) * t)) / 2;
    wheel.fitRect(sx - hw, sy - hh, sx + hw, sy + hh + 40);
  };
  ctlBox.addEventListener("pointerdown", (e) => e.stopPropagation());  // not a pan of the wheel
  ctlBox.addEventListener("dblclick", (e) => e.stopPropagation());
  ctlBox.addEventListener("click", (e) => {
    e.stopPropagation();
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.e === "explode") toggleExplode(); else fitEngine();
  });
  host.parentElement.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    if (e.key === "e" || e.key === "E") toggleExplode();
    if (e.key === "f" || e.key === "F") fitEngine();
  });
  function gauges() {
    const fuel = (ctx.rows("brain").find(([k]) => /L3 left/.test(k)) || [, "-"])[1];
    const temp = { ok: "normal", warn: "check", fail: "hot" }[state.health];
    const gear = state.toolSpin > 0.15 ? "drive" : state.rpm > 3 ? "neutral" : "park";
    strip.innerHTML = [["tach", `${Math.round(state.rpm)} rpm`, ""], ["boost", state.thinking ? "spooled" : "off", ""], ["gear", gear, ""],
      ["fuel", `L3 ${fuel}`, ""], ["oil", `${state.items} mem`, ""], ["temp", temp, state.health]]
      .map(([k, v, cls]) => `<span class="${cls}"><i>${k}</i><b>${esc(v)}</b></span>`).join("");
    lights();
  }
  function health() {
    const states = Object.values(cards).map((c) => c.state);
    state.health = states.includes("fail") ? "fail" : states.includes("warn") ? "warn" : "ok";
    gauges(); wake(300);
  }

  // ---------- data feeds ----------
  function setOverview(o) {
    if (!o) return;
    const mem = o.memory || {};
    state.kinds = mem.kinds || {}; state.items = mem.items || 0; state.links = mem.links || 0;
    state.brain = (o.brain && o.brain.name) || "brain"; state.warm = !!(o.brain && o.brain.warm);
    state.listening = !!(o.voice && o.voice.listening);
    const nodes = Object.fromEntries(((o.cluster && o.cluster.nodes) || []).map((n) => [n.id, n]));
    state.devices = (o.devices || []).map((d) => ({ name: d.hostname || d.id, here: !!d.here, online: d.here || !!(nodes[d.id] && nodes[d.id].online) }));
    health();
  }
  const api = (path) => (window.Jarvis && window.Jarvis.api ? window.Jarvis.api(path) : fetch(path).then((r) => r.json()));
  const loadCards = async () => { try { for (const c of await api("/api/system/components")) cards[c.id] = c; health(); } catch (e) {} };
  (async () => { try { setOverview(await api("/api/overview")); } catch (e) {} })();
  loadCards(); setInterval(() => { if (!document.hidden) loadCards(); }, 30000);
  addEventListener("jarvis:overview", (ev) => setOverview(ev.detail));

  const STAGE_OF = [[/^(hear|intake|voice)/, 0], [/^(route|instant|parse|judge)/, 1], [/^(recall|memory)/, 2], [/^(gate|hist|confirm)/, 3],
                    [/^(brain|plan|combine|step)/, 4], [/^(tool|run|adapter|grok|task)/, 5], [/^(obs)/, 6], [/^(remember|speak)/, 7]];
  const fire = (i) => ctx.shared.fire && ctx.shared.fire(i);
  addEventListener("jarvis:live", (ev) => {
    const e = ev.detail || {}, d = e.data || {};
    if (e.kind === "turn.start") {
      state.turns++; state.rpmTo = Math.max(state.rpmTo, 1400); state.until = performance.now() + 60000;
      state.thinking = 1; state.intake = 1; fire(0);
    }
    if (e.kind === "turn.step") {
      const id = String(d.id || ""), lbl = String(d.label || "");
      state.lastStep = lbl || id;
      const hit = STAGE_OF.find(([re]) => re.test(id));
      if (hit) fire(hit[1]);
      const tool = /^(tool|adapter|grok)/.test(id);
      state.rpmTo = Math.min(5200, Math.max(state.rpmTo, 900) + (tool ? 900 : 250));
      state.until = Math.max(state.until, performance.now() + 15000);
      if (tool) { state.lastTool = lbl || id; state.tools++; state.toolSpin = 1; }
      if (/^(brain|plan)/.test(id)) state.boost = 1;
      if (d.state === "fail") state.error = 1;
    }
    if (e.kind === "turn.done") {
      state.replies++; state.reply = 1; state.thinking = 0; state.until = performance.now() + 2200; fire(7);
      if (d.error) state.error = 1;
    }
    wake(3000);
  });

  // ---------- hover: 3D parts and the callout tags ----------
  const hitObjs = [], owner = new Map();
  for (const m of mods) for (const o of m.hit) o.traverse((x) => { if ((x.isMesh || x.isInstancedMesh) && !owner.has(x)) { hitObjs.push(x); owner.set(x, m); } });
  const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
  let hover = null;
  function focus(m) {
    if (m === hover) return;
    if (hover) { hover.focus(false); hover.tagEl.classList.remove("on"); }
    hover = m;
    if (m) { m.focus(true); m.tagEl.classList.add("on"); }
    if (!running()) renderNow();
  }
  function hoverAt(x, y) {
    const r = canvas.getBoundingClientRect();
    let m = mods.find((q) => q.tagAt && x >= q.tagAt[0] && x <= q.tagAt[2] && y >= q.tagAt[1] && y <= q.tagAt[3]) || null;
    if (!m) {
      ptr.set((x / r.width) * 2 - 1, -(y / r.height) * 2 + 1);
      ray.setFromCamera(ptr, camera);
      const hit = ray.intersectObjects(hitObjs, false)[0];
      m = hit ? owner.get(hit.object) : null;
    }
    focus(m);
    canvas.style.cursor = m && m.def.href ? "pointer" : "";
    if (m) {
      const head = `<div class="tt"><b>${String(m.def.no).padStart(2, "0")} ${esc(m.def.name)}</b><span>${esc(m.def.role)}</span></div>`;
      tip.innerHTML = head + m.info(state).map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
      tip.hidden = false;
      // keep the tip on the engine's side of the ring: flip it left of the pointer when it would cross
      const tw = tip.offsetWidth || 200, th = tip.offsetHeight || 100, limit = ring ? ringEdge(y) : r.width - 4;
      const tx = x + 14 + tw > limit ? x - 14 - tw : x + 14;
      tip.style.left = Math.max(4, Math.min(r.width - tw - 4, tx)) + "px"; tip.style.top = Math.max(4, Math.min(r.height - th - 4, y - 10)) + "px";
    } else tip.hidden = true;
    wake(600);
  }
  function leave() { focus(null); tip.hidden = true; wake(600); }
  canvas.addEventListener("pointermove", (e) => { const r = canvas.getBoundingClientRect(); hoverAt(e.clientX - r.left, e.clientY - r.top); });
  canvas.addEventListener("pointerleave", leave);
  host.parentElement.addEventListener("click", () => { if (hover && hover.def.href) location.href = hover.def.href; });

  // ---------- anchored in the wheel's world ----------
  // The wheel draws in world coordinates (screen = view.x + world * view.k). The engine gets a
  // fixed spot in that world, left of the ring, so zooming or panning the wheel moves and scales
  // it with everything else. world.scale = world px per engine unit.
  const world = { ax: 0, ay: 0, scale: 40 };
  let view = { k: 1, x: 0, y: 0 }, W = 1, H = 1;
  const GAUGE_H = 0.13;  // share of the stage height kept under the engine for the gauges
  let ring = null;  // the wheel's geometry: tags and the tip stay clear of the ring
  function ringEdge(y) {  // screen x of the ring's inner edge at screen height y
    if (!ring) return W - 4;
    const [cx, cy] = toScreen(ring.cx, ring.cy), rr = (ring.r - 16) * view.k;
    return Math.abs(y - cy) < rr ? Math.min(W - 4, cx + Math.sqrt(rr * rr - (y - cy) ** 2)) : W - 4;
  }
  function place(G, w, h) {
    W = w; H = h; ring = G;
    const kh = w < 640 ? 1.15 : 1.35;  // the wheel's home zoom (wheel.js home())
    const left = 0.02 * w, rightEdge = HOME_EDGE * w - 0.02 * w;
    const availW = rightEdge - left, availH = h * (1 - GAUGE_H) - 0.06 * h;
    const ppu = Math.max(12, Math.min(availW / span.x, availH / span.y));
    world.scale = ppu / kh;
    const sx = (left + rightEdge) / 2, sy = 0.03 * h + availH / 2;
    const homeX = HOME_EDGE * w - kh * (G.cx + G.r), homeY = h / 2 - kh * G.cy;
    world.ax = (sx - homeX) / kh; world.ay = (sy - homeY) / kh;
    size();
  }
  // Called from inside the wheel's own frame: render right here, in the same frame, so the engine
  // and the ring move as one (a separate rAF lagged a frame behind and shivered while panning).
  function setView(v) {
    if (!v || (v.k === view.k && v.x === view.x && v.y === view.y)) return;
    view = { k: v.k, x: v.x, y: v.y };
    frameCamera();
    renderNow();
  }
  const toScreen = (wx, wy) => [view.x + wx * view.k, view.y + wy * view.k];
  const proj = new THREE.Vector3();
  const screenOf = (v) => { proj.copy(v).project(camera); return [(proj.x * 0.5 + 0.5) * W, (-proj.y * 0.5 + 0.5) * H]; };
  let ppuNow = 1;
  function frameCamera() {
    const ppu = (ppuNow = world.scale * view.k), [sx, sy] = toScreen(world.ax, world.ay);
    camera.left = -sx / ppu; camera.right = (W - sx) / ppu; camera.top = sy / ppu; camera.bottom = -(H - sy) / ppu;
    camera.updateProjectionMatrix(); camera.updateMatrixWorld();
    // gauges: centred under the engine, scaled with it (360 px wide at its natural size)
    const ew = ppu * span.x, gs = Math.max(0.45, Math.min(1.25, (ew * 0.62) / 360, (W * (HOME_EDGE - 0.04) * view.k) / 1.35 / 360));
    const gx = Math.max(4 - 180 * (1 - gs), sx - 180);  // keep the strip's left edge on the stage
    strip.style.transformOrigin = "50% 0";
    strip.style.transform = `translate3d(${gx.toFixed(2)}px, ${(sy + (ppu * span.y) / 2 + 6).toFixed(2)}px, 0) scale(${gs.toFixed(4)})`;
    strip.style.display = sx < -300 || sx > W + 300 || sy < -300 || sy > H + 300 ? "none" : "";
    placeTags(ew);
  }
  function placeTags(ew) {
    // full tags once the engine is big enough to read them, numbers only when small, none when tiny
    const mode = ew > 640 || (state.explode > 0.5 && ew > 300) ? "full" : ew > 150 ? "num" : "off";
    tagLayer.setAttribute("data-mode", mode);
    if (mode === "off") { for (const m of mods) m.tagAt = null; return; }
    if (mode !== tagMode || !mods[0].tagSize) for (const m of mods) m.tagSize = [m.tagEl.offsetWidth || 60, m.tagEl.offsetHeight || 16];
    tagMode = mode;
    const reach = Math.max(0.55, Math.min(1.6, ew / 520)), floor = H - 30;
    const boxes = mods.map((m) => {
      const [ax, ay] = screenOf(m.root.localToWorld(aw.copy(m.anchor))), [dx, dy] = m.def.tag, [tw, th] = m.tagSize;
      const tx = ax + dx * reach, ty = ay + dy * reach;
      return { m, ax, ay, tw, th, l: dx < 0 ? tx - tw : tx, t: ty - th / 2 };
    });
    const edge = ringEdge;
    const [ctlR, ctlB] = ctlRect;
    const clamp = (b) => {
      b.t = Math.max(b.l < ctlR ? ctlB : 4, Math.min(floor - b.th, b.t));
      b.l = Math.max(4, Math.min(Math.min(W - 4, edge(b.t), edge(b.t + b.th)) - b.tw, b.l));
    };
    boxes.forEach(clamp);
    // push overlapping tags apart vertically, a few passes, so the callouts stay readable
    for (let pass = 0; pass < 6; pass++) {
      let moved = false;
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        if (a.l >= b.l + b.tw + 2 || b.l >= a.l + a.tw + 2) continue;
        const over = Math.min(a.t + a.th, b.t + b.th) - Math.max(a.t, b.t) + 2;
        if (over <= 0) continue;
        const [hi, lo] = a.t <= b.t ? [a, b] : [b, a];
        hi.t -= over / 2; lo.t += over / 2; clamp(hi); clamp(lo); moved = true;
      }
      if (!moved) break;
    }
    for (const b of boxes) {
      b.m.tagEl.style.transform = `translate3d(${b.l.toFixed(1)}px, ${b.t.toFixed(1)}px, 0)`;
      const ex = Math.max(b.l, Math.min(b.l + b.tw, b.ax)), ey = Math.max(b.t, Math.min(b.t + b.th, b.ay));
      b.m.tagAt = [b.l, b.t, b.l + b.tw, b.t + b.th, b.ax, b.ay, ex, ey];
    }
  }
  let tagMode = "";
  const aw = new THREE.Vector3();

  // a stage fired on the ring: a strand runs from the firing cylinder to that stage
  const strands = [];
  function burst(wx, wy) { strands.push({ wx, wy, born: performance.now(), ci: ctx.shared.lastFired ?? 4 }); if (strands.length > 12) strands.shift(); wake(1400); }
  window.JarvisBoard = { place, hoverAt, leave, setView, burst };
  setTimeout(() => { const hook = window.JarvisBoardHook; if (hook) { hook.board = window.JarvisBoard; if (hook.G) place(hook.G, hook.size.W, hook.size.H); if (hook.view) setView(hook.view); } }, 0);
  let ctlRect = [0, 0];
  function size() {
    W = Math.max(1, host.clientWidth); H = Math.max(1, host.clientHeight);
    ctlRect = [ctlBox.offsetLeft + ctlBox.offsetWidth + 6, ctlBox.offsetTop + ctlBox.offsetHeight + 6];
    renderer.setSize(W, H, false);
    overlay.width = Math.round(W * Math.min(2, devicePixelRatio || 1)); overlay.height = Math.round(H * Math.min(2, devicePixelRatio || 1));
    frameCamera(); wake(300);
  }
  new ResizeObserver(size).observe(host);

  // ---------- loop ----------
  let visible = true, awakeUntil = 0, last = 0, raf = 0, lastGauge = 0;
  const running = () => state.rpm > 3 || state.rpmTo > 3;
  const shown = () => visible && !document.hidden && host.offsetParent !== null;
  function renderNow() { if (shown()) { renderer.render(scene, camera); drawOverlay(performance.now()); } }
  new IntersectionObserver((es) => { visible = es.some((x) => x.isIntersecting); if (visible) wake(800); }).observe(host);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) wake(800); });
  function wake(ms) { awakeUntil = Math.max(awakeUntil, performance.now() + ms); if (!raf) raf = requestAnimationFrame(loop); }
  function loop(now) {
    raf = 0;
    if (!shown()) return;
    const run = running();
    if (now - last >= (run ? 1000 / 60 : 1000 / 30) - 1) {
      const dt = Math.min(0.05, (now - last) / 1000 || 0.016); last = now;
      step(dt, now); renderer.render(scene, camera); drawOverlay(now);
    }
    if ((run || now < awakeUntil) && (!reduced || run)) raf = requestAnimationFrame(loop);
  }
  function step(dt, now) {
    if (now > state.until) state.rpmTo = 0;  // nothing happening: the engine shuts off
    state.rpm += (state.rpmTo - state.rpm) * Math.min(1, (state.rpmTo > state.rpm ? 1.6 : 0.8) * dt);
    if (state.rpm < 3 && state.rpmTo === 0) state.rpm = 0;
    state.angle += (reduced ? 0 : (state.rpm / 60) * Math.PI * 2 * 0.12) * dt;  // shown slowed so the motion reads
    if (state.explode !== state.explodeTo) {
      state.explode += (state.explodeTo - state.explode) * Math.min(1, dt * 5);
      if (Math.abs(state.explode - state.explodeTo) < 0.002) state.explode = state.explodeTo;
      applyExplode(state.explode); frameCamera(); wake(200);
    }
    for (const m of mods) m.update(dt, state, now);
    state.error *= Math.pow(0.5, dt);
    if (now - lastGauge > 250) { lastGauge = now; gauges(); }
  }
  const pw = new THREE.Vector3();
  function drawOverlay(now) {
    const dpr = overlay.width / Math.max(1, W);
    octx.setTransform(dpr, 0, 0, dpr, 0, 0); octx.clearRect(0, 0, W, H);
    // callout leaders: anchor dot, elbow to the tag
    if (tagLayer.getAttribute("data-mode") !== "off") {
      for (const m of mods) {
        if (!m.tagAt) continue;
        const [, , , , ax, ay, ex, ey] = m.tagAt, on = m === hover;
        octx.strokeStyle = on ? "rgba(240,180,41,.9)" : "rgba(184,190,198,.38)"; octx.lineWidth = 1;
        octx.beginPath(); octx.moveTo(ax, ay); octx.lineTo(ex, ey); octx.stroke();
        octx.fillStyle = on ? "#f0b429" : "rgba(220,226,232,.75)";
        octx.beginPath(); octx.arc(ax, ay, on ? 2.6 : 1.8, 0, 6.3); octx.fill();
      }
    }
    const cyl = ctx.shared.cylinders || [];
    for (let i = strands.length - 1; i >= 0; i--) {
      const st = strands[i], age = (now - st.born) / 1200;
      if (age >= 1) { strands.splice(i, 1); continue; }
      const c = cyl[st.ci] || cyl[4];
      if (!c) continue;
      c.piston.getWorldPosition(pw);
      const [x1, y1] = screenOf(pw), [x2, y2] = toScreen(st.wx, st.wy);
      const a = Math.sin(Math.PI * Math.min(1, age * 1.3)) * (1 - age);
      octx.strokeStyle = `rgba(240,180,41,${0.75 * a})`; octx.lineWidth = 1.4;
      octx.beginPath(); octx.moveTo(x1, y1); octx.quadraticCurveTo((x1 + x2) / 2, Math.min(y1, y2) - 60, x2, y2); octx.stroke();
      const t2 = Math.min(1, age * 1.6), u = 1 - t2;  // a bright bead running out to the ring
      const bx = u * u * x1 + 2 * u * t2 * (x1 + x2) / 2 + t2 * t2 * x2, by = u * u * y1 + 2 * u * t2 * (Math.min(y1, y2) - 60) + t2 * t2 * y2;
      octx.fillStyle = `rgba(255,220,150,${a})`; octx.beginPath(); octx.arc(bx, by, 2.4, 0, 6.3); octx.fill();
    }
  }
  for (const m of mods) m.update(0, state, 0);
  size(); gauges(); wake(1500);
}
