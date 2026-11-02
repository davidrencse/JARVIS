// Console layout: every panel under the wheel can be dragged to a new place, made wider or
// taller, removed, and added back from a widget library that also holds extra statistics.
// Panels keep their DOM position (app.js and friends find them by id); order and size are CSS
// `order` / grid spans on the shared grid, saved per browser. Built-in panels and the stat
// widgets defined here are treated the same.
(function () {
  "use strict";
  const J = window.Jarvis;
  const main = document.querySelector("#view-console .cx-main");
  const grid = document.querySelector("#view-console .cx-grid");
  if (!J || !main || !grid) return;
  const on = (name, fn) => addEventListener("jarvis:" + name, (e) => fn(e.detail));
  const esc = J.esc;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const KEY = "jarvis.console.layout.v1";
  const store = {
    get() { try { return JSON.parse(localStorage.getItem(KEY)) || null; } catch (e) { return null; } },
    set(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} },
  };

  // ---------- stat widgets (new) ----------
  const session = { latency: [], scores: [], heard: 0, ignored: 0 };
  const bars = (rows, unit = "") => {
    const max = Math.max(1, ...rows.map((r) => r[1]));
    return `<ul class="st-bars">${rows.map(([k, v]) => `<li><span class="k">${esc(k)}</span><i style="--w:${(v / max) * 100}%"></i><b>${v}${unit}</b></li>`).join("")}</ul>`;
  };
  const big = (n, sub) => `<p class="st-big">${n}</p><p class="st-sub">${esc(sub)}</p>`;
  const o = () => J.state.overview || {};
  const STATS = {
    heat: { title: "Stage activity", desc: "How often each pipeline stage ran since the console opened.", span: 2,
      render: () => {
        const st = window.JarvisWheel?.stages || [];
        const rows = st.filter((s) => s.total).map((s) => [s.label, s.total]);
        return rows.length ? bars(rows) : `<p class="st-empty">Runs light this up stage by stage.</p>`;
      } },
    latency: { title: "Response time", desc: "Seconds from request to reply, this session.", span: 1,
      render: () => {
        const L = session.latency;
        if (!L.length) return `<p class="st-empty">No replies yet this session.</p>`;
        const sorted = [...L].sort((a, b) => a - b), med = sorted[Math.floor(sorted.length / 2)];
        const max = Math.max(...L, 1), pts = L.slice(-30).map((v, i, a) => `${(i / Math.max(1, a.length - 1)) * 100},${30 - (v / max) * 28}`).join(" ");
        return big(med.toFixed(1) + " s", `median of ${L.length} · fastest ${sorted[0].toFixed(1)} s`) +
          `<svg class="st-spark" viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}"/></svg>`;
      } },
    voice: { title: "Voice gate", desc: "Speaker-check scores: accepted, near misses and other voices.", span: 1,
      render: () => {
        const S = session.scores.slice(-40);
        const dots = S.map((s, i) => `<i class="${s.ok ? "ok" : "no"}" style="left:${(i / Math.max(1, S.length - 1)) * 100}%;bottom:${Math.min(1, s.score) * 100}%"></i>`).join("");
        return big(`${session.heard} / ${session.ignored}`, "heard / ignored this session") +
          `<div class="st-gate" aria-hidden="true"><span class="th" style="bottom:36%"></span><span class="nm" style="bottom:10%"></span>${dots}</div>`;
      } },
    kinds: { title: "Memory mix", desc: "What the memory heap holds, by kind.", span: 1,
      render: () => bars(Object.entries(o().memory?.kinds || {}).sort((a, b) => b[1] - a[1])) },
    tasks: { title: "Agent runs", desc: "Task ledger runs by outcome.", span: 1,
      render: () => { const s = o().tasks?.states || {}; return Object.keys(s).length ? bars(Object.entries(s)) : `<p class="st-empty">No runs yet.</p>`; } },
    todo: { title: "Vault tasks", desc: "Your Obsidian tasks: overdue, today, in progress, upcoming.", span: 1,
      render: () => { const t = o().todo || {}; return bars([["overdue", t.overdue || 0], ["today", t.today || 0], ["in progress", t.in_progress || 0], ["upcoming", t.upcoming || 0]]); } },
    tools: { title: "Tools", desc: "Which local tools JARVIS can reach right now.", span: 1,
      render: () => `<ul class="st-tools">${Object.entries(o().tools || {}).map(([k, v]) => `<li class="${v ? "ok" : "no"}"><i></i>${esc(k)}<b>${v ? "ready" : "missing"}</b></li>`).join("")}</ul>` },
    budget: { title: "Voice budget", desc: "ElevenLabs characters used this month.", span: 1,
      render: () => { const v = o().voice || {}, used = v.chars_this_month || 0, cap = v.budget || 1;
        return big(`${Math.round((used / cap) * 100)}%`, `${used.toLocaleString()} of ${cap.toLocaleString()} characters`) + `<div class="st-meter"><i style="--w:${Math.min(100, (used / cap) * 100)}%"></i></div>`; } },
    memgrowth: { title: "Memory", desc: "Items and links in the memory heap.", span: 1,
      render: () => big((o().memory?.items ?? 0).toLocaleString(), `${(o().memory?.links ?? 0).toLocaleString()} links`) },
  };
  for (const [key, w] of Object.entries(STATS)) {
    const el = document.createElement("article");
    el.className = "card lab-stat"; el.id = "stat-" + key;
    el.innerHTML = `<header class="ph"><h2>${esc(w.title)}</h2></header><div class="st-body"></div>`;
    grid.appendChild(el);
    w.el = el;
  }
  let drawQueued = 0;
  function drawStats() {
    cancelAnimationFrame(drawQueued);
    drawQueued = requestAnimationFrame(() => {
      for (const w of Object.values(STATS)) if (!w.el.classList.contains("is-off")) w.el.querySelector(".st-body").innerHTML = w.render();
    });
  }
  on("overview", drawStats);
  on("live", (e) => {
    const d = e.data || {};
    if (e.kind === "turn.done" && typeof d.seconds === "number") session.latency.push(d.seconds);
    if (e.kind === "voice.heard") { session.heard++; session.scores.push({ score: d.score, ok: true }); }
    if (e.kind === "voice.ignored") { session.ignored++; session.scores.push({ score: d.score, ok: false }); }
    if (/^(turn|voice)\./.test(e.kind)) drawStats();
  });

  // ---------- the panels ----------
  const graphCard = document.getElementById("mini-graph")?.closest(".card");
  const PANELS = [
    ["trace", "#w-trace", "Trace", "Every step of the current request, as a tree.", 2, 2],
    ["agent", "#sig-agent", "Agent", "Listening state, face and voice waveform.", 1, 1],
    ["codex", "#sig-limit", "Codex window", "ChatGPT/Codex usage left in this window.", 1, 1],
    ["heat", "#stat-heat", null, null, 2, 1],
    ["recent", "#w-recent", "Recent memory", "The last things remembered.", 2, 1],
    ["latency", "#stat-latency", null, null, 1, 1],
    ["voice", "#stat-voice", null, null, 1, 1],
    ["machines", ".cx-machines", "Machines and links", "Each computer and the links between them.", 2, 1],
    ["claude", "#sig-claude", "Claude window", "Claude usage left in this window.", 1, 1],
    ["activity", "#w-activity", "Activity", "Requests, providers and memory, as a ledger.", 2, 1],
    ["link", "#sig-link", "Zenbook link", "Ping trace to the other laptop.", 1, 1],
    ["balance", "#w-balance", "DeepSeek balance", "Live API balance.", 1, 1],
    ["credits", ".cx-credits", "Credits", "Provider balances and limits.", 2, 1],
    ["graph", graphCard, "Memory graph", "A preview of the memory graph and router.", 2, 1],
    ["kinds", "#stat-kinds", null, null, 1, 1, true],
    ["tasks", "#stat-tasks", null, null, 1, 1, true],
    ["todo", "#stat-todo", null, null, 1, 1, true],
    ["tools", "#stat-tools", null, null, 1, 1, true],
    ["budget", "#stat-budget", null, null, 1, 1, true],
    ["memgrowth", "#stat-memgrowth", null, null, 1, 1, true],
  ].map(([key, sel, title, desc, span, rows, off]) => {
    const el = typeof sel === "string" ? document.querySelector(sel) : sel;
    const w = STATS[key];
    return el && { key, el, title: title || w.title, desc: desc || w.desc, span, rows, off: !!off };
  }).filter(Boolean);
  const byKey = Object.fromEntries(PANELS.map((p) => [p.key, p]));

  const fresh = () => ({ order: PANELS.map((p) => p.key), span: {}, rows: {}, off: PANELS.filter((p) => p.off).map((p) => p.key) });
  let state = store.get() || fresh();
  for (const p of PANELS) if (!state.order.includes(p.key)) { state.order.push(p.key); if (p.off) state.off.push(p.key); }

  function apply() {
    let n = 0;
    state.order.forEach((key, i) => {
      const p = byKey[key]; if (!p) return;
      const off = state.off.includes(key);
      p.el.classList.add("lab-panel");
      p.el.classList.toggle("is-off", off);
      p.el.style.order = String(i);
      p.el.dataset.span = String(state.span[key] || p.span);
      p.el.dataset.rows = String(state.rows[key] || p.rows);
      p.el.dataset.key = key;
      const label = p.el.querySelector("h2, .flow-title");
      if (label && !off) label.dataset.n = String(++n).padStart(2, "0");
      chrome(p);
    });
    drawStats();
    dispatchEvent(new Event("resize"));  // canvases inside panels re-measure
  }
  function save() { store.set(state); }

  // ---------- per-panel chrome: grip, width, height, remove ----------
  function chrome(p) {
    if (p.el.querySelector(":scope > .lab-chrome")) return;
    const c = document.createElement("div");
    c.className = "lab-chrome";
    c.innerHTML = `<button type="button" class="grip" aria-label="Move ${esc(p.title)} (arrow keys)" title="Drag to move">⠿</button>` +
      `<button type="button" data-act="narrow" aria-label="Narrower" title="Narrower">−</button>` +
      `<button type="button" data-act="wide" aria-label="Wider" title="Wider">+</button>` +
      `<button type="button" data-act="tall" aria-label="Taller or shorter" title="Taller / shorter">↕</button>` +
      `<button type="button" data-act="off" aria-label="Remove ${esc(p.title)}" title="Remove">×</button>`;
    p.el.appendChild(c);
    c.addEventListener("click", (e) => {
      const act = e.target.closest("button")?.dataset.act; if (!act) return;
      const span = +p.el.dataset.span, rows = +p.el.dataset.rows;
      flip(() => {
        if (act === "narrow") state.span[p.key] = Math.max(1, span - 1);
        if (act === "wide") state.span[p.key] = Math.min(4, span + 1);
        if (act === "tall") state.rows[p.key] = rows > 1 ? 1 : 2;
        if (act === "off") { state.off.push(p.key); J.toast?.(`${p.title} removed. Add it back from Add widget.`); }
        apply();
      });
      save(); library.render();
    });
    c.querySelector(".grip").addEventListener("keydown", (e) => {
      const d = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key]; if (!d) return;
      e.preventDefault(); move(p.key, d); c.querySelector(".grip").focus();
    });
  }
  function move(key, d) {
    const vis = state.order.filter((k) => !state.off.includes(k));
    const i = vis.indexOf(key), j = i + d; if (j < 0 || j >= vis.length) return;
    flip(() => { const a = state.order.indexOf(key), b = state.order.indexOf(vis[j]); [state.order[a], state.order[b]] = [state.order[b], state.order[a]]; apply(); });
    save();
  }

  // FLIP: record where every panel is, change the layout, then slide each from old to new.
  function flip(change, skip) {
    const before = new Map(PANELS.map((p) => [p, p.el.getBoundingClientRect()]));
    change();
    if (reduce.matches) return;
    for (const p of PANELS) {
      if (p.el === skip || p.el.classList.contains("is-off")) continue;
      const a = before.get(p), b = p.el.getBoundingClientRect();
      const dx = a.left - b.left, dy = a.top - b.top;
      if (!dx && !dy) continue;
      p.el.style.transition = "none"; p.el.style.transform = `translate(${dx}px, ${dy}px)`;
      requestAnimationFrame(() => { p.el.style.transition = "transform .32s cubic-bezier(.2,.8,.2,1)"; p.el.style.transform = ""; });
    }
  }

  // ---------- drag: lift the panel, follow the pointer, others make room ----------
  let drag = null;
  main.addEventListener("pointerdown", (e) => {
    const editing = main.closest(".lab").classList.contains("editing");
    const grip = e.target.closest(".lab-chrome .grip");
    const panel = e.target.closest(".lab-panel");
    if (!panel || (!grip && !(editing && !e.target.closest(".lab-chrome button")))) return;
    e.preventDefault();
    const r = panel.getBoundingClientRect();
    drag = { el: panel, key: panel.dataset.key, dx: e.clientX - r.left, dy: e.clientY - r.top, id: e.pointerId };
    panel.setPointerCapture(e.pointerId);
    panel.classList.add("lifted"); panel.style.transition = "none";
  });
  main.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const place = () => {
      const r = drag.el.getBoundingClientRect(), cur = new DOMMatrixReadOnly(getComputedStyle(drag.el).transform);
      drag.el.style.transform = `translate(${e.clientX - drag.dx - (r.left - cur.m41)}px, ${e.clientY - drag.dy - (r.top - cur.m42)}px) scale(1.015)`;
    };
    place();
    drag.el.style.pointerEvents = "none";
    const under = document.elementFromPoint(e.clientX, e.clientY)?.closest(".lab-panel");
    drag.el.style.pointerEvents = "";
    if (!under || under === drag.el || !under.dataset.key) return;
    const ur = under.getBoundingClientRect();
    const after = e.clientX > ur.left + ur.width / 2 || e.clientY > ur.top + ur.height * .75;
    const next = state.order.filter((k) => k !== drag.key);
    next.splice(next.indexOf(under.dataset.key) + (after ? 1 : 0), 0, drag.key);
    if (next.join() === state.order.join()) return;
    flip(() => { state.order = next; apply(); }, drag.el);
    place();
  });
  const drop = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const el = drag.el; drag = null;
    el.classList.remove("lifted");
    el.style.transition = reduce.matches ? "none" : "transform .28s cubic-bezier(.2,.8,.2,1)";
    el.style.transform = "";
    save(); library.render();
  };
  main.addEventListener("pointerup", drop); main.addEventListener("pointercancel", drop);

  // ---------- toolbar + widget library ----------
  const bar = document.createElement("div");
  bar.className = "lab-toolbar";
  bar.innerHTML = `<span class="lab-tb-label">Panels</span>
    <button type="button" class="lab-tb" data-tb="edit" aria-pressed="false">Edit layout</button>
    <button type="button" class="lab-tb" data-tb="add" aria-expanded="false" aria-controls="lab-library">Add widget</button>
    <button type="button" class="lab-tb ghost" data-tb="reset">Reset</button>
    <span class="lab-tb-hint">Drag a panel by ⠿ — or turn on Edit layout and drag anywhere.</span>
    <div class="lab-library" id="lab-library" hidden role="dialog" aria-label="Add a widget"></div>`;
  document.getElementById("lab-stage")?.after(bar);
  const libEl = bar.querySelector(".lab-library");
  const library = {
    render() {
      const off = PANELS.filter((p) => state.off.includes(p.key));
      libEl.innerHTML = `<header><b>Widgets</b><span>${off.length ? off.length + " available" : "Everything is on the console"}</span></header>` +
        (off.length ? `<ul>${off.map((p) => `<li><div><b>${esc(p.title)}</b><span>${esc(p.desc || "")}</span></div>
          <button type="button" class="lab-tb" data-add="${p.key}">Add</button></li>`).join("")}</ul>` : "");
    },
  };
  bar.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.tb === "edit") {
      const lab = main.closest(".lab"), onNow = !lab.classList.contains("editing");
      lab.classList.toggle("editing", onNow); b.setAttribute("aria-pressed", String(onNow));
      b.textContent = onNow ? "Done" : "Edit layout";
    } else if (b.dataset.tb === "add") {
      const open = libEl.hidden; library.render(); libEl.hidden = !open; b.setAttribute("aria-expanded", String(open));
    } else if (b.dataset.tb === "reset") {
      flip(() => { state = fresh(); apply(); }); save(); library.render(); J.toast?.("Layout reset.");
    } else if (b.dataset.add) {
      const key = b.dataset.add;
      flip(() => { state.off = state.off.filter((k) => k !== key); state.order = [...state.order.filter((k) => k !== key), key]; apply(); });
      save(); library.render();
      const el = byKey[key].el; el.classList.add("just-added"); setTimeout(() => el.classList.remove("just-added"), 1600);
      el.scrollIntoView({ behavior: reduce.matches ? "auto" : "smooth", block: "nearest" });
    }
  });
  document.addEventListener("pointerdown", (e) => {
    if (!libEl.hidden && !e.target.closest(".lab-library, [data-tb='add']")) { libEl.hidden = true; bar.querySelector("[data-tb='add']").setAttribute("aria-expanded", "false"); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !libEl.hidden) libEl.hidden = true; });

  apply();
})();
