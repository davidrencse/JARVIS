// Connection tree: while JARVIS works on a request, a floating panel draws the request's
// path as a live tree — where it came from, what it recalled, how it was routed, which
// brain or run took it, every tool that brain called, and the reply going back out.
//
// Built only from the live stream (turn.start / turn.step / turn.sentence / turn.done /
// turn.error, see events.py), so console, voice and CLI requests all show up. It sits
// over every view, appears on its own when a turn starts and fades a few seconds after
// it ends unless pinned.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const { esc } = J;
  const on = (name, fn) => addEventListener("jarvis:" + name, (e) => fn(e.detail));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };

  const CHANNEL = { console: "Console", voice: "Voice", cli: "Terminal" };
  const LINGER = 8000;          // ms the finished tree stays up when not pinned
  const W = 156, H = 40, GX = 26, GY = 6;   // node box and gaps, px (flow.css matches)

  const turns = new Map();       // message id -> turn
  let shown = null, pinned = store.get("jarvis.flow.pin", false), hidden = false, fadeTimer = 0, tick = 0;

  // ---------- panel ----------
  const panel = document.createElement("aside");
  panel.className = "flow";
  panel.hidden = true;
  panel.setAttribute("aria-live", "polite");
  panel.innerHTML = `
    <header class="flow-head">
      <span class="flow-dot"></span>
      <b class="flow-title">Connection tree</b>
      <span class="flow-meta"></span>
      <button type="button" class="flow-btn flow-prev" title="Previous request" aria-label="Previous request">‹</button>
      <button type="button" class="flow-btn flow-next" title="Next request" aria-label="Next request">›</button>
      <button type="button" class="flow-btn flow-pin" title="Keep this panel open" aria-pressed="false">Pin</button>
      <button type="button" class="flow-btn flow-close" title="Hide until the next request" aria-label="Hide">×</button>
    </header>
    <p class="flow-ask"></p>
    <div class="flow-body"><div class="flow-canvas"><svg class="flow-edges" aria-hidden="true"></svg></div></div>`;
  document.body.appendChild(panel);
  const $ = (s) => panel.querySelector(s);
  const canvas = $(".flow-canvas"), svg = $(".flow-edges");

  $(".flow-pin").addEventListener("click", () => { pinned = !pinned; store.set("jarvis.flow.pin", pinned); sync(); });
  $(".flow-close").addEventListener("click", () => { hidden = true; pinned = false; store.set("jarvis.flow.pin", false); sync(); });
  $(".flow-prev").addEventListener("click", () => step(-1));
  $(".flow-next").addEventListener("click", () => step(1));
  function step(dir) {
    const keys = [...turns.keys()], i = keys.indexOf(shown);
    const next = keys[i + dir];
    if (next) { shown = next; draw(); }
  }

  // ---------- model ----------
  function node(t, id, parent, label, detail, state) {
    const n = t.nodes.get(id);
    if (n) Object.assign(n, { parent: parent ?? n.parent, label: label ?? n.label, detail: detail ?? n.detail, state: state ?? n.state });
    else { t.nodes.set(id, { id, parent, label, detail: detail || "", state: state || "done", order: tick++ }); }
  }

  function begin(d, at) {
    const t = { id: d.message, channel: d.channel, text: d.text || "", tier: d.tier, at, done: false, nodes: new Map(), sentences: 0 };
    node(t, "root", null, CHANNEL[d.channel] || d.channel || "Request", (d.text || "").slice(0, 90), "run");
    turns.set(d.message, t);
    while (turns.size > 12) turns.delete(turns.keys().next().value);
    return t;
  }

  on("live", (e) => {
    const d = e.data || {};
    if (e.kind === "hello") { if (!turns.size) return; turns.clear(); shown = null; sync(); return; }
    if (!e.kind.startsWith("turn.") || !d.message) return;
    let t = turns.get(d.message);
    if (e.kind === "turn.start") {
      t = begin(d, e.at * 1000);
      shown = d.message; hidden = false;
    }
    if (!t) return;
    if (e.kind === "turn.step") {
      // "playwright.browser_click" reads better as browser_click, from playwright.
      const m = /^(approve |decline )?([\w-]+)\.([\w-]+)(.*)$/.exec(d.label || "");
      const label = m ? `${m[1] || ""}${m[3]}` : d.label;
      node(t, d.id, d.parent, label, d.detail || (m ? m[2] + (m[4] || "") : ""), d.state);
    }
    if (e.kind === "turn.sentence") {
      t.sentences += 1;
      node(t, "reply", "root", "Reply", `${t.sentences} sentence${t.sentences === 1 ? "" : "s"} streaming`, "run");
    }
    if (e.kind === "turn.done" || e.kind === "turn.error") {
      t.done = true; t.seconds = d.seconds;
      const failed = e.kind === "turn.error";
      for (const n of t.nodes.values()) if (n.state === "run") n.state = failed ? "fail" : "done";
      if (failed) node(t, "error", "root", "Failed", String(d.error || "").replace(/^\w+(Error|Exception): /, ""), "fail");
      else node(t, "reply", "root", "Reply", (d.reply || "").replace(/\s+/g, " ").slice(0, 90), "done");
      node(t, "root", null, null, null, failed ? "fail" : "done");
    }
    if (d.message === shown) draw();
    sync();
  });

  // ---------- layout: columns by depth, rows by leaf order ----------
  function layout(t) {
    const kids = new Map();
    for (const n of t.nodes.values()) {
      if (n.id === "root") continue;
      const p = t.nodes.has(n.parent) ? n.parent : "root";
      if (!kids.has(p)) kids.set(p, []);
      kids.get(p).push(n);
    }
    for (const list of kids.values()) list.sort((a, b) => a.order - b.order);
    const pos = new Map();
    let row = 0, depth = 0;
    (function place(n, d) {
      depth = Math.max(depth, d);
      const ch = kids.get(n.id) || [];
      if (!ch.length) { pos.set(n.id, { x: d, y: row++ }); return; }
      ch.forEach((c) => place(c, d + 1));
      const ys = ch.map((c) => pos.get(c.id).y);
      pos.set(n.id, { x: d, y: (Math.min(...ys) + Math.max(...ys)) / 2 });
    })(t.nodes.get("root"), 0);
    return { pos, rows: Math.max(row, 1), cols: depth + 1 };
  }

  function draw() {
    const t = turns.get(shown);
    if (!t) return;
    const { pos, rows, cols } = layout(t);
    const width = cols * W + (cols - 1) * GX, height = rows * H + (rows - 1) * GY;
    // Shrink to fit the panel's width (down to 70%), then scroll.
    const room = $(".flow-body").clientWidth - 28;
    const k = room > 0 ? Math.max(0.7, Math.min(1, room / width)) : 1;
    canvas.style.width = width + "px"; canvas.style.height = height + "px";
    canvas.style.transform = k < 1 ? `scale(${k})` : "";
    canvas.style.marginBottom = k < 1 ? `${-height * (1 - k)}px` : "";
    canvas.style.marginRight = k < 1 ? `${-width * (1 - k)}px` : "";
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", width); svg.setAttribute("height", height);
    const at = (id) => { const p = pos.get(id); return { x: p.x * (W + GX), y: p.y * (H + GY) }; };

    let edges = "";
    for (const n of t.nodes.values()) {
      if (n.id === "root" || !pos.has(n.id)) continue;
      const parent = t.nodes.has(n.parent) ? n.parent : "root";
      const a = at(parent), b = at(n.id);
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
      edges += `<path class="flow-edge" data-state="${esc(n.state)}" d="M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}"/>`;
    }
    svg.innerHTML = edges;

    // Nodes are kept and moved, not rebuilt, so the tree grows instead of flickering.
    if (canvas.dataset.turn !== t.id) { canvas.querySelectorAll(".flow-node").forEach((el) => el.remove()); canvas.dataset.turn = t.id; }
    const els = new Map([...canvas.querySelectorAll(".flow-node")].map((el) => [el.dataset.id, el]));
    for (const n of t.nodes.values()) {
      if (!pos.has(n.id)) continue;
      let el = els.get(n.id);
      if (!el) {
        el = document.createElement("div");
        el.className = "flow-node"; el.dataset.id = n.id;
        el.innerHTML = `<span class="flow-node-label"><i></i><span></span></span><span class="flow-node-detail"></span>`;
        canvas.appendChild(el);
      }
      els.delete(n.id);
      const p = at(n.id);
      el.dataset.state = n.state;
      el.style.transform = `translate(${p.x}px, ${p.y}px)`;
      el.title = [n.label, n.detail].filter(Boolean).join("\n");
      el.querySelector(".flow-node-label span").textContent = n.label || n.id;
      el.querySelector(".flow-node-detail").textContent = n.detail || "";
    }
    for (const el of els.values()) el.remove();
    // Keep the newest working node in view.
    const busy = [...t.nodes.values()].filter((n) => n.state === "run" && n.id !== "root" && pos.has(n.id)).pop();
    if (busy) {
      const body = $(".flow-body"), y = at(busy.id).y * k;
      if (y < body.scrollTop || y + H * k > body.scrollTop + body.clientHeight) body.scrollTop = y - body.clientHeight / 2;
    }

    const keys = [...turns.keys()], i = keys.indexOf(shown);
    $(".flow-ask").textContent = t.text;
    $(".flow-meta").textContent = [t.tier, t.done ? (t.seconds != null ? `${t.seconds}s` : "done") : "working", keys.length > 1 ? `${i + 1}/${keys.length}` : ""].filter(Boolean).join(" · ");
    $(".flow-prev").disabled = i <= 0;
    $(".flow-next").disabled = i >= keys.length - 1;
    panel.dataset.state = t.done ? (t.nodes.get("root").state === "fail" ? "fail" : "done") : "run";
  }

  // ---------- visibility ----------
  function sync() {
    clearTimeout(fadeTimer);
    const t = turns.get(shown);
    const working = [...turns.values()].some((x) => !x.done);
    $(".flow-pin").setAttribute("aria-pressed", String(pinned));
    $(".flow-pin").textContent = pinned ? "Pinned" : "Pin";
    const want = !!t && !hidden && (pinned || working || !t.done);
    if (want) { panel.hidden = false; requestAnimationFrame(() => panel.classList.add("on")); }
    else if (t && !hidden) fadeTimer = setTimeout(hide, LINGER);  // finished: linger, then fade
    else hide();
  }
  function hide() {
    panel.classList.remove("on");
    setTimeout(() => { if (!panel.classList.contains("on")) panel.hidden = true; }, 220);
  }
})();
