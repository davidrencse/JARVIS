// Porcelain dashboard layer: sidebar pill, header pills, runs timeline, usage trend,
// recent memory, limits with the silk panel, the task board and its drawer. app.js owns
// data and behaviour and announces changes as "jarvis:*" events; this file only draws.
// Sliding-pill and rolling-number motion are ported from Watermelon UI's Continuous
// Tabs and Run Widget (MIT, ui.watermelon.sh) to plain DOM.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const { esc, icon } = J;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const on = (name, fn) => addEventListener("jarvis:" + name, (e) => fn(e.detail));
  const compact = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K" : String(Math.round(n)));

  // ---------- sliding pill (Continuous Tabs), horizontal or vertical ----------
  function slide(pill, target) {
    if (!pill) return;
    if (!target || !target.offsetParent) { pill.style.opacity = 0; return; }
    const box = target.getBoundingClientRect(), host = pill.parentElement.getBoundingClientRect();
    const vertical = getComputedStyle(pill.parentElement).flexDirection === "column";
    // The first placement jumps; only later moves spring, so nothing slides in on load.
    if (!pill.dataset.placed) { pill.style.transition = "none"; requestAnimationFrame(() => requestAnimationFrame(() => { pill.style.transition = ""; })); pill.dataset.placed = "1"; }
    pill.style.opacity = 1;
    pill.style.width = box.width + "px";
    pill.style.height = vertical ? box.height + "px" : "";
    pill.style.transform = vertical ? `translateY(${box.top - host.top}px)` : `translateX(${box.left - host.left}px)`;
  }
  const navPill = () => slide($(".rail-pill"), $('.rail a[aria-current="page"]'));
  const rangePill = () => slide($(".range .tabs-pill"), $('.range [aria-checked="true"]'));
  addEventListener("resize", () => { navPill(); rangePill(); boardPill(); });
  // Fonts and wrapping change the pills' targets after first paint; re-measure then too.
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => { navPill(); rangePill(); boardPill(); });
    $$(".rail, .range, #view-tasks .tabs").forEach((el) => ro.observe(el));
  }
  document.fonts?.ready.then(() => { navPill(); rangePill(); });

  // ---------- header title follows the view; the prompt lives in the assistant ----------
  const dock = $("#dock"), shell = $(".shell"), home = $("#dock-home");
  let view = location.hash.slice(1) || "console";
  function placeDock() {
    const inPanel = view === "console" && innerWidth > 860;
    if (inPanel && dock.parentElement !== home) home.appendChild(dock);
    if (!inPanel && dock.parentElement !== shell) shell.appendChild(dock);
    document.documentElement.classList.toggle("dock-in-panel", inPanel);
  }
  function onView(name) {
    view = name;
    $("#page-title").textContent = $("#view-" + name)?.dataset.title?.replace(" and plans", "") || "Console";
    document.documentElement.dataset.view = name;
    placeDock(); requestAnimationFrame(() => { navPill(); rangePill(); });
    if (name === "tasks") { setPane(pane); requestAnimationFrame(boardPill); }
    if (name !== "tasks" && !drawer.hidden) closeDrawer();
  }
  on("view", onView);
  addEventListener("resize", placeDock);

  // ---------- header pills ----------
  $("#hero-search").addEventListener("submit", (e) => {
    e.preventDefault();
    $("#mem-q").value = $("#hero-q").value.trim();
    location.hash = "memory";
    setTimeout(() => $("#mem-q").dispatchEvent(new Event("input")), 50);
  });
  $("#hero-refresh").addEventListener("click", () => { $("#probe-refresh").click(); $("#credits-refresh").click(); loadUsage(); });

  // ---------- rolling numbers (Run Widget) ----------
  function roll(el, to, fmt) {
    if (!el) return;
    const from = parseFloat(el.dataset.value || "0");
    el.dataset.value = to;
    if (reduce.matches || !isFinite(from) || from === to) { el.innerHTML = fmt(to); return; }
    const t0 = performance.now(), dur = 900;
    const step = (now) => {
      const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 4);
      el.innerHTML = fmt(from + (to - from) * e);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ---------- smooth lines ----------
  // Catmull-Rom through the points, as cubic Béziers.
  // Straight segments between the real points: a curve would invent values between hours.
  function smooth(pts) {
    return pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  }
  function lineChart({ values, ghost, labels, marker, unit = "", ariaLabel, axes = true, H = 170 }) {
    const W = 520, L = axes ? 34 : 4, T = 10, B = axes ? 24 : 6, n = values.length;
    const max = Math.max(...values, ...(ghost || []), 1), min = Math.min(0, ...values);
    const x = (i) => L + (i / Math.max(1, n - 1)) * (W - L - 6);
    const y = (v) => T + (1 - (v - min) / (max - min || 1)) * (H - T - B);
    const pts = values.map((v, i) => [x(i), y(v)]);
    const grid = axes ? [0.25, 0.5, 0.75, 1].map((f) => { const v = min + (max - min) * f, yy = y(v);
      return `<line x1="${L}" x2="${W - 6}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" class="c-grid"/><text x="${L - 6}" y="${(yy + 3).toFixed(1)}" text-anchor="end" class="c-axis">${esc(compact(v))}${unit}</text>`; }).join("") : "";
    const xl = (labels || []).map(([i, t]) => `<text x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="c-axis">${esc(t)}</text>`).join("");
    const ghostPath = ghost ? `<path d="${smooth(ghost.map((v, i) => [x(i), y(v)]))}" class="c-ghost"/>` : "";
    let mark = "";
    if (marker != null && pts[marker]) {
      const [mx, my] = pts[marker];
      mark = `<line x1="${mx.toFixed(1)}" x2="${mx.toFixed(1)}" y1="${my.toFixed(1)}" y2="${H - B}" class="c-drop"/><circle cx="${mx.toFixed(1)}" cy="${my.toFixed(1)}" r="5" class="c-dot"/>`;
    }
    return `<svg class="line-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(ariaLabel)}">${grid}${xl}${ghostPath}<path d="${smooth(pts)}" class="c-line"/>${mark}</svg>`;
  }

  // ---------- Claude usage: trend card + silk bars ----------
  let usage = null;
  // The card's range tabs (1D, 3D, 7D): the window shown, against the window before it
  // when the logs reach back that far (they keep a week, so 7D has nothing to compare).
  let usageDays = 1;
  async function loadUsage() {
    if (!$("#usage-body")) return; // the console's ridge (console.js) shows usage now
    const span = usageDays * 24, ask = Math.min(span * 2, 168);
    try { usage = await J.api("/api/usage?hours=" + ask); } catch (e) {
      $("#usage-body").innerHTML = `<div class="empty"><b>Usage isn't readable right now.</b>${esc(e.message)}</div>`; return;
    }
    const n = usage.output.length, cur = usage.output.slice(n - span), curReplies = usage.replies.slice(n - span);
    const prev = n >= span * 2 ? usage.output.slice(n - span * 2, n - span) : null;
    const total = cur.reduce((a, b) => a + b, 0), before = prev ? prev.reduce((a, b) => a + b, 0) : 0;
    const change = prev && before ? ((total - before) / before) * 100 : null;
    const words = usageDays === 1 ? "24 hours" : `${usageDays} days`;
    const body = $("#usage-body");
    if (!body.querySelector(".trend-big")) {
      body.innerHTML = `
      <div class="u-tabs range-tabs" role="radiogroup" aria-label="Usage range">
        ${[1, 3, 7].map((d) => `<button type="button" role="radio" data-days="${d}" aria-checked="${d === usageDays}">${d}D</button>`).join("")}</div>
      <p class="trend-sub" id="usage-sub"></p>
      <div class="trend-big"><span id="usage-num"></span><span class="trend-delta" id="usage-delta"></span></div>
      <p class="trend-note" id="usage-note"></p>
      <div class="trend-chart"></div>
      <ul class="trend-legend" id="usage-legend"></ul>`;
      $$("#usage-body [data-days]").forEach((b) => b.addEventListener("click", () => {
        usageDays = +b.dataset.days;
        $$("#usage-body [data-days]").forEach((x) => x.setAttribute("aria-checked", x === b));
        loadUsage();
      }));
    }
    $("#usage-sub").textContent = `Output tokens, last ${words}`;
    roll($("#usage-num"), total, (v) => compact(v));
    // Less output is not "worse" here, so the change is a signed figure in ink, not red or green.
    $("#usage-delta").textContent = change == null ? "" : `${change >= 0 ? "+" : "−"}${Math.abs(change).toFixed(0)}%`;
    $("#usage-delta").className = "trend-delta";
    $("#usage-note").textContent = change == null ? `Claude Code keeps a week of logs, so there's nothing earlier to compare.` : `Compared with the ${words} before`;
    body.querySelector(".trend-chart").innerHTML = lineChart({ values: cur, ghost: prev, axes: false, H: 96,
      ariaLabel: `Claude output per hour over the last ${words}${prev ? " (bold) against the period before (dashed)" : ""}` });
    const peak = Math.max(...cur), peakAt = new Date((usage.start + (n - span + cur.indexOf(peak)) * 3600) * 1000);
    $("#usage-legend").innerHTML = [
      ["Last 5 hours", compact(cur.slice(-5).reduce((a, b) => a + b, 0))],
      ["Replies", String(curReplies.reduce((a, b) => a + b, 0))],
      [`Peak, ${usageDays > 1 ? peakAt.toLocaleDateString([], { weekday: "short" }) + " " : ""}${peakAt.toLocaleTimeString([], { hour: "numeric" })}`, compact(peak)],
    ].map(([k, v]) => `<li><i aria-hidden="true"></i><span>${esc(k)}</span><b>${esc(v)}</b></li>`).join("");
    bars();
  }
  function bars() {
    if (!usage || !$("#silk-bars")) return;
    const last = usage.output.slice(-12), max = Math.max(...last, 1);
    const hour = (i) => new Date((usage.start + (usage.hours - 12 + i) * 3600) * 1000).toLocaleTimeString([], { hour: "numeric" });
    $("#silk-bars").innerHTML = `<h3>Claude by hour</h3><div class="bars">${last.map((v, i) =>
      `<span class="bar-col" title="${esc(hour(i))}: ${compact(v)} tokens"><i style="height:${Math.max(3, (v / max) * 100).toFixed(1)}%"></i></span>`).join("")}</div>
      <div class="bars-axis"><span>${esc(hour(0))}</span><span>now</span></div>`;
  }
  loadUsage(); setInterval(() => { if (!document.hidden) loadUsage(); }, 5 * 60 * 1000);

  // ---------- limits: stats + silk tiles ----------
  let credits = J.state.credits, probe = J.state.probe;
  const until = (iso) => { const s = Math.max(0, (Date.parse(iso) - Date.now()) / 1000);
    return s < 3600 ? Math.max(1, Math.round(s / 60)) + " min" : s < 86400 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " d"; };
  function limits() {
    if (!$("#limit-stats")) return;
    const codex = credits?.find((r) => r.id === "codex");
    const current = (codex?.meters || []).filter((m) => Number.isFinite(m.pct) &&
      (!m.resets_at || Date.parse(m.resets_at) > Date.now()));
    const five = current.find((m) => m.label === "5-hour window");
    const week = current.find((m) => m.label === "Weekly window");
    const left = (m) => (m ? Math.round(100 - m.pct) : null);
    const zen = probe?.devices?.zenbook, dell = probe?.devices?.dell;
    const stat = (ic, label, value, sub, tone) => `<div class="stat ${tone || ""}"><span class="stat-ic" aria-hidden="true">${icon(ic)}</span>
      <div><span class="stat-label">${esc(label)}</span><b>${value == null ? "—" : value}</b><span class="stat-sub">${esc(sub || "")}</span></div></div>`;
    $("#limit-stats").innerHTML =
      stat("gauge", "Codex, 5 h", left(five) == null ? null : left(five) + "%", five ? (five.resets_at ? "resets in " + until(five.resets_at) : "left") : "no data") +
      stat("calendar", "Codex, week", left(week) == null ? null : left(week) + "%", week?.resets_at ? "resets in " + until(week.resets_at) : "left") +
      stat("laptop-minimal", "Zenbook memory", zen?.mem_avail_pct == null ? null : zen.mem_avail_pct + "%", "free", zen?.mem_avail_pct != null && zen.mem_avail_pct < 10 ? "alert" : "");
    const tile = (title, sub, bits, prog, progLabel, tone) => `<div class="silk-tile">
      <h3>${esc(title)}</h3><p>${esc(sub)}</p>
      <div class="tile-bits">${bits.map((b) => `<span>${esc(b)}</span>`).join("")}</div>
      <div class="tile-prog ${tone || ""}"><span>${esc(progLabel)}</span><span>${prog == null ? "—" : Math.round(prog) + "%"}</span>
        <i aria-hidden="true"><b style="width:${prog == null ? 0 : Math.max(2, Math.min(100, prog))}%"></b></i></div></div>`;
    const lm = probe?.input_link;
    $("#silk-tiles").innerHTML =
      tile("WinLaptop", "Dell G15 · this laptop",
        [dell?.lan_mouse?.running ? "Lan Mouse up" : "Lan Mouse off", lm ? (lm.level === "down" ? "link down" : "link up") : "checking"],
        five ? 100 - (five.pct ?? 0) : null, "Codex window left") +
      tile("archmain", zen?.ssh ? `Zenbook · ${zen.ip || "online"}` : zen ? "Zenbook · unreachable" : "Zenbook · checking",
        [zen?.ping_ms != null ? zen.ping_ms + " ms" : "—", zen?.wifi_power_save === "on" ? "power save on" : "power save off"],
        zen?.mem_avail_pct ?? null, "Memory free", zen?.mem_avail_pct != null && zen.mem_avail_pct < 10 ? "alert" : "");
    const done = credits && probe;
    $("#limits-count").textContent = done ? String([five, week, zen].filter(Boolean).length) : "…";
  }
  on("credits", (d) => { credits = d; limits(); });
  on("probe", (d) => { probe = d; limits(); });
  limits();

  // Silk for the black panel: soft folds shaded once on a small canvas, then reused as
  // a background image. Drawn, not downloaded, and redrawn only on theme change.
  function silk() {
    if (!$("#silk")) return;
    const w = 360, h = 180, c = document.createElement("canvas"); c.width = w; c.height = h;
    const g = c.getContext("2d"), img = g.createImageData(w, h);
    const dark = document.documentElement.dataset.theme !== "light";
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const u = x / w, v = y / h;
      const fold = Math.sin(u * 7.0 + Math.sin(v * 3.2 + u * 2.1) * 2.4 + v * 1.4);
      const fold2 = Math.sin(u * 13.0 - v * 4.0 + Math.sin(u * 3.0) * 1.2);
      let l = 0.5 + 0.38 * fold + 0.12 * fold2;
      l = Math.pow(Math.max(0, Math.min(1, l)), 2.2) * (0.35 + 0.65 * (1 - Math.abs(v - 0.45)));
      const base = 0.045, val = Math.round(255 * (base + l * 0.16));
      const i = (y * w + x) * 4;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = val; img.data[i + 3] = 255;
    }
    g.putImageData(img, 0, 0);
    $("#silk").style.setProperty("--silk", `url(${c.toDataURL("image/png")})`);
    return dark;
  }
  silk();

  // ---------- recent memory ----------
  const KIND_ICON = { message: "message-square", reply: "audio-lines", note: "file-text", finding: "flag", plan: "list-checks", result: "activity", case: "folder-search", url: "link", output: "terminal" };
  function recent(o) {
    const items = (o?.memory?.recent || []).slice(0, 5);
    $("#recent-list").innerHTML = items.length ? items.map((it) => `<li><a href="#memory" data-id="${esc(it.id)}">
        <span class="pl-ic" aria-hidden="true">${icon(KIND_ICON[it.kind] || "file-text")}</span><span class="pl-t">${esc(it.title)}</span><span class="pl-k">${esc(it.kind)}</span></a></li>`).join("")
      : `<li class="empty"><b>Nothing remembered yet.</b>Requests, replies and notes land here.</li>`;
    $$("#recent-list a").forEach((a) => a.addEventListener("click", () => {
      $("#mem-q").value = ""; setTimeout(() => $(`#mem-list [data-id="${CSS.escape(a.dataset.id)}"]`)?.click(), 400);
    }));
  }
  on("overview", (o) => { recent(o); timeline(); if (view === "tasks") renderBoard(); });

  // ---------- runs timeline (the reference's schedule) ----------
  let range = "day";
  function timeline() {
    const o = J.state.overview, host = $("#timeline");
    if (!o || !host) return;
    const now = Date.now(), span = range === "day" ? 24 * 3600e3 : 7 * 24 * 3600e3;
    const t0 = now - span, ticks = range === "day" ? 8 : 7;
    const runs = [];
    for (const t of o.tasks?.recent || []) {
      const a = Date.parse(t.created_at), b = Date.parse(t.updated_at || t.created_at);
      if (b < t0) continue;
      runs.push({ row: `${t.provider || "agent"} · ${t.role || "task"}`, a: Math.max(a, t0), b: Math.max(b, a), title: t.objective, state: t.state, who: (t.provider || "?")[0].toUpperCase(), id: t.id, kind: "task" });
    }
    for (const p of o.plans || []) {
      const b = (p.updated_at || 0) * 1000;
      if (b && b >= t0) runs.push({ row: "plans", a: b - 60e3, b, title: p.goal, state: p.state, who: "P", id: p.id, kind: "plan" });
    }
    const rows = [...new Set(runs.map((r) => r.row))].slice(0, 6);
    $("#timeline-meta").textContent = runs.length ? `${runs.length} run${runs.length === 1 ? "" : "s"} in the last ${range === "day" ? "24 hours" : "7 days"}` : "";
    if (!rows.length) {
      host.innerHTML = `<div class="empty"><b>No runs in the last ${range === "day" ? "24 hours" : "week"}.</b>Ask something in the assistant and its run appears here.
        ${range === "day" ? `<button type="button" class="btn ghost sm" id="tl-week">Show the past week</button>` : ""}</div>`;
      $("#tl-week")?.addEventListener("click", () => $('.range [data-range="week"]').click());
      return;
    }
    const pos = (t) => ((t - t0) / span) * 100;
    const dur = (ms) => ms < 60e3 ? Math.max(1, Math.round(ms / 1000)) + " s" : ms < 3600e3 ? Math.round(ms / 60e3) + " min" : (ms / 3600e3).toFixed(1) + " h";
    const tickLabel = (i) => { const d = new Date(t0 + (span * i) / ticks);
      return range === "day" ? d.toLocaleTimeString([], { hour: "numeric" }) : d.toLocaleDateString([], { weekday: "short" }); };
    host.innerHTML = `<div class="tl-grid" style="--rows:${rows.length}">
      <div class="tl-labels">${rows.map((r) => `<span>${esc(r)}</span>`).join("")}</div>
      <div class="tl-track">
        ${Array.from({ length: ticks + 1 }, (_, i) => `<i class="tl-line" style="left:${(i / ticks) * 100}%"></i>`).join("")}
        <i class="tl-now" style="left:100%" title="now"></i>
        ${runs.filter((r) => rows.includes(r.row)).map((r) => {
          const left = pos(r.a), width = Math.max(pos(r.b) - left, 0);
          return `<button type="button" class="tl-run ${r.state === "finished" || r.state === "done" ? "" : "alt"}" data-kind="${r.kind}" data-id="${esc(r.id)}"
            style="--row:${rows.indexOf(r.row)};left:min(${left.toFixed(2)}%, calc(100% - 128px));min-width:max(${width.toFixed(2)}%, 118px)"
            title="${esc(r.title)} · ${esc(r.state)}"><span>${esc(dur(r.b - r.a))} · ${esc(J.words(r.state))}</span><span class="tl-who" aria-hidden="true">${esc(r.who)}</span></button>`;
        }).join("")}
      </div>
      <div class="tl-axis">${Array.from({ length: ticks + 1 }, (_, i) => `<span style="left:${(i / ticks) * 100}%">${esc(tickLabel(i))}</span>`).join("")}</div>
    </div>`;
    $$(".tl-run", host).forEach((b) => b.addEventListener("click", () => {
      location.hash = "tasks";
      setTimeout(() => (b.dataset.kind === "plan" ? J.openPlan(b.dataset.id) : J.openTask(b.dataset.id)), 60);
    }));
  }
  $$(".range [data-range]").forEach((b) => b.addEventListener("click", () => {
    range = b.dataset.range;
    $$(".range [data-range]").forEach((x) => x.setAttribute("aria-checked", x === b));
    rangePill(); timeline();
  }));
  setInterval(timeline, 60000);

  // ---------- agent runs board (the second tab) ----------
  const COLUMNS = [
    { id: "queued", title: "Queued", states: ["queued", "planned"] },
    { id: "running", title: "Running", states: ["running"] },
    { id: "done", title: "Done", states: ["finished"] },
    { id: "attention", title: "Needs attention", states: null },
  ];
  const PRIORITY = { high: ["High", "p-high"], mid: ["Medium", "p-mid"], low: ["Low", "p-low"] };
  function cardsFrom(o) {
    const tasks = (o.tasks?.recent || []).map((t) => ({
      kind: "task", id: t.id, state: t.state, title: t.objective || t.id, at: t.created_at,
      prio: PRIORITY[t.power] || ["Auto", "p-low"],
      tags: [t.provider, t.role, t.mode === "workspace-write" ? "can edit" : "read-only"].filter(Boolean),
      desc: `${J.words(t.state)} ${J.ago(t.updated_at || t.created_at)}`, foot: t.id.slice(0, 6) }));
    const plans = (o.plans || []).map((p) => { const done = p.steps.filter((s) => s.state === "finished").length;
      return { kind: "plan", id: p.id, state: p.state, title: p.goal, at: p.updated_at ? p.updated_at * 1000 : null,
        prio: ["Plan", "p-plan"], tags: [...new Set(p.steps.map((s) => s.specialist))].slice(0, 3),
        desc: p.summary, foot: `${done}/${p.steps.length} steps` }; });
    return [...plans, ...tasks];
  }
  function card(c) {
    const date = c.at ? new Date(typeof c.at === "number" ? c.at : Date.parse(c.at)).toLocaleDateString([], { month: "short", day: "numeric" }) : "—";
    return `<li><button type="button" class="t-card" data-kind="${c.kind}" data-id="${esc(c.id)}">
      <span class="t-top"><span class="prio ${c.prio[1]}">${esc(c.prio[0])}</span></span>
      ${c.tags.length ? `<span class="t-tags">${c.tags.map((t) => `<span>${esc(t)}</span>`).join("")}</span>` : ""}
      <b class="t-title">${esc(c.title)}</b>
      ${c.desc ? `<span class="t-desc">${esc(c.desc)}</span>` : ""}
      <span class="t-foot"><span class="t-chip">${icon("flag")} ${esc(date)}</span><span class="t-chip">${icon(c.kind === "plan" ? "list-checks" : "terminal")} ${esc(c.foot)}</span></span>
    </button></li>`;
  }
  function renderBoard() {
    const o = J.state.overview, host = $("#task-board");
    if (!o || !host) return;
    const q = ($("#board-q").value || "").trim().toLowerCase();
    const all = cardsFrom(o).filter((c) => !q || (c.title + " " + c.tags.join(" ") + " " + (c.desc || "")).toLowerCase().includes(q));
    const known = COLUMNS.flatMap((col) => col.states || []);
    host.innerHTML = COLUMNS.map((col) => {
      const items = all.filter((c) => (col.states ? col.states.includes(c.state) : !known.includes(c.state)));
      return `<section class="col" aria-label="${esc(col.title)}">
        <header class="col-head"><div><h2>${esc(col.title)}</h2><span>${items.length} ${items.length === 1 ? "run" : "runs"}</span></div>
          ${col.id === "queued" ? `<button type="button" class="round-btn sm" data-new aria-label="Ask JARVIS to do something">${icon("plus")}</button>` : ""}</header>
        <ol class="col-cards">${items.map(card).join("") || `<li class="col-empty">${col.id === "attention" ? "Nothing failed." : col.id === "running" ? "Nothing running." : "Empty."}</li>`}</ol>
      </section>`;
    }).join("");
    $$(".t-card", host).forEach((b) => b.addEventListener("click", () => (b.dataset.kind === "plan" ? J.openPlan(b.dataset.id) : J.openTask(b.dataset.id))));
    $("[data-new]", host)?.addEventListener("click", () => { location.hash = "console"; setTimeout(() => $("#prompt").focus(), 80); });
  }

  // ---------- tabs: To-dos (the vault) | Agent runs ----------
  let pane = "todos";
  const tabsPill = $("#view-tasks .tabs-pill"), tabs = $$("#view-tasks [data-pane]");
  function boardPill() { slide(tabsPill, $('#view-tasks [data-pane][aria-selected="true"]')); }
  function setPane(name, focus) {
    pane = name;
    tabs.forEach((t) => { const on = t.dataset.pane === name; t.setAttribute("aria-selected", on); t.tabIndex = on ? 0 : -1; if (on && focus) t.focus(); });
    $("#todo-pane").hidden = name !== "todos"; $("#runs-pane").hidden = name !== "runs";
    $("#board-q").placeholder = name === "todos" ? "Filter to-dos" : "Filter agent runs";
    boardPill();
    if (name === "runs") renderBoard(); else loadTodos();
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => setPane(t.dataset.pane));
    t.addEventListener("keydown", (e) => { const d = { ArrowRight: 1, ArrowLeft: -1 }[e.key]; if (d) { e.preventDefault(); setPane(tabs[(i + d + tabs.length) % tabs.length].dataset.pane, true); } });
  });
  let qTimer;
  $("#board-q").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => (pane === "runs" ? renderBoard() : loadTodos()), 220); });

  // ---------- to-dos from the vault ----------
  // Every action is followed by a fresh fetch: a task's id comes from its note and words,
  // so an edit (or Obsidian changing the note) can give it a new one.
  const TODO_COLUMNS = [
    { id: "overdue", title: "Overdue", from: ["overdue"], empty: "Nothing overdue." },
    { id: "today", title: "Today", from: ["today", "in_progress"], empty: "Nothing due today." },
    { id: "upcoming", title: "Upcoming", from: ["upcoming", "later"], empty: "Nothing scheduled ahead." },
    { id: "anytime", title: "Anytime", from: ["anytime"], empty: "No undated to-dos." },
    { id: "done", title: "Done this week", from: ["done"], empty: "Nothing finished this week yet." },
  ];
  const PRIO = { highest: ["Highest", "p-high"], high: ["High", "p-high"], medium: ["Medium", "p-mid"], low: ["Low", "p-low"], lowest: ["Lowest", "p-low"] };
  let agenda = null, todoSeq = 0, todoBusy = false;
  const byId = new Map();
  const fmtDay = (iso) => { if (!iso) return ""; const d = new Date(iso + "T12:00"), t = new Date(); t.setHours(12, 0, 0, 0);
    const diff = Math.round((d - t) / 86400e3);
    return diff === 0 ? "Today" : diff === 1 ? "Tomorrow" : diff === -1 ? "Yesterday" : d.toLocaleDateString([], { month: "short", day: "numeric", ...(d.getFullYear() !== t.getFullYear() ? { year: "numeric" } : {}) }); };
  const obsidian = (path) => `obsidian://open?vault=${encodeURIComponent(agenda?.vault || "Vault")}&file=${encodeURIComponent(path.replace(/\.md$/i, ""))}`;

  async function loadTodos() {
    const seq = ++todoSeq, q = ($("#board-q").value || "").trim();
    try {
      const a = await J.api("/api/vault/tasks" + (q ? "?q=" + encodeURIComponent(q) : ""));
      if (seq !== todoSeq) return; // a newer fetch is on its way
      agenda = a; drawTodos();
    } catch (e) {
      if (seq !== todoSeq) return;
      $("#todo-board").innerHTML = `<div class="empty card"><b>Your vault's to-dos can't be read right now.</b>${esc(e.message)}${/No vault/i.test(e.message) ? "" : " If the console was started before the to-do board existed, restart it."}</div>`;
      $("#study-plans").innerHTML = ""; $("#todo-count").textContent = "";
    }
  }
  function todoCard(t, col) {
    const done = t.status === "done", prio = PRIO[t.priority];
    const when = t.due ? `due ${fmtDay(t.due)}` : t.scheduled ? `scheduled ${fmtDay(t.scheduled)}` : done && t.done ? `done ${fmtDay(t.done)}` : "";
    const whenTip = t.due_from_text ? "Due date read from the task's words, not a 📅 field" : "";
    return `<li class="todo ${done ? "is-done" : ""} ${t.status === "in_progress" ? "is-started" : ""}">
      <button type="button" class="tick" data-tick="${esc(t.id)}" aria-label="${done ? "Reopen" : "Mark done"}: ${esc(t.title)}" ${agenda.writable ? "" : "disabled"}>${icon(done ? "check" : "circle", "i")}</button>
      <button type="button" class="todo-body" data-open="${esc(t.id)}">
        ${prio || t.status === "in_progress" || t.recur ? `<span class="t-top">${prio ? `<span class="prio ${prio[1]}">${prio[0]}</span>` : ""}${t.status === "in_progress" ? `<span class="prio p-plan">Started</span>` : ""}${t.recur ? `<span class="t-chip sm">${icon("rotate-ccw")} ${esc(t.recur)}</span>` : ""}</span>` : ""}
        <b class="t-title">${esc(t.title)}</b>
        <span class="t-foot">${when ? `<span class="t-chip ${col === "overdue" ? "late" : ""}" ${whenTip ? `title="${esc(whenTip)}"` : ""}>${icon("calendar")} ${esc(when)}</span>` : "<span></span>"}<span class="t-note" title="${esc(t.path)}">${icon("book-open")} ${esc(t.note)}</span></span>
      </button></li>`;
  }
  function drawTodos() {
    const a = agenda; if (!a) return;
    byId.clear();
    TODO_COLUMNS.forEach((c) => c.from.forEach((b) => (a[b] || []).forEach((t) => byId.set(t.id, t))));
    const open = (a.counts.overdue || 0) + (a.counts.today || 0) + (a.counts.in_progress || 0);
    $("#todo-count").textContent = open ? String(open) : "";
    $("#tasks-meta").textContent = `${a.open} open in ${a.vault}${a.writable ? "" : ", read-only"}`;
    $("#quick-add").hidden = !a.writable;
    $("#study-plans").innerHTML = (a.checklists || []).map((c) => {
      const pct = c.total ? Math.round((c.done / c.total) * 100) : 0;
      return `<a class="plan-bar card" href="${esc(obsidian(c.path))}" title="Open ${esc(c.note)} in Obsidian">
        <span class="pb-head"><b>${esc(c.note)}</b><span>${c.done} of ${c.total}</span></span>
        <span class="pb-track" role="img" aria-label="${pct}% done"><i style="width:${pct}%"></i></span>
        <span class="pb-next">${c.next ? `Next: ${esc(c.next)}` : "All done."}</span></a>`;
    }).join("");
    $("#todo-board").innerHTML = TODO_COLUMNS.map((col) => {
      const items = col.from.flatMap((b) => a[b] || []);
      return `<section class="col todo-col col-${col.id}" aria-label="${esc(col.title)}">
        <header class="col-head"><div><h2>${esc(col.title)}</h2><span>${items.length} ${items.length === 1 ? "to-do" : "to-dos"}</span></div></header>
        <ol class="col-cards">${items.map((t) => todoCard(t, col.id)).join("") || `<li class="col-empty">${esc(col.empty)}</li>`}</ol>
      </section>`;
    }).join("");
  }
  async function act(id, action, value) {
    if (todoBusy) return;
    const t = byId.get(id); todoBusy = true;
    document.documentElement.classList.add("todo-busy");
    try {
      const r = await J.api(`/api/vault/tasks/${encodeURIComponent(id)}`, { action, value: value ?? null });
      J.toast({ done: "Done. Ticked in " + (t?.note || r.path) + ".", reopen: "Reopened.", start: "Marked started.", cancel: "Cancelled.",
                due: value ? "Due date set." : "Due date cleared.", priority: "Priority set." }[action] + (r.next ? " The next one is scheduled." : ""));
      await loadTodos();
      if (!drawer.hidden && drawer.dataset.todo) { const fresh = [...byId.values()].find((x) => x.path === t?.path && x.line === t?.line) || byId.get(id); fresh ? openTodo(fresh.id) : closeDrawer(); }
    } catch (e) {
      J.toast(/changed since/i.test(e.message) ? "That note changed since the board loaded. It's refreshed now; try again." : e.message);
      await loadTodos();
    } finally { todoBusy = false; document.documentElement.classList.remove("todo-busy"); }
  }
  function openTodo(id) {
    const t = byId.get(id); if (!t) return;
    const done = t.status === "done", w = agenda.writable;
    drawer.dataset.todo = id;
    $("#task-detail-title").textContent = "To-do";
    const fact = (k, v) => (v ? `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>` : "");
    $("#task-detail").innerHTML = `<h3 class="todo-h">${esc(t.title)}</h3>
      <dl class="facts">${fact("Note", t.note)}${fact("Section", t.section)}${fact("Due", t.due ? fmtDay(t.due) + (t.due_from_text ? " (read from the text)" : "") : "")}${fact("Scheduled", t.scheduled && fmtDay(t.scheduled))}${fact("Starts", t.start && fmtDay(t.start))}${fact("Repeats", t.recur)}${fact("Created", t.created && fmtDay(t.created))}${fact("Done", t.done && fmtDay(t.done))}${fact("Priority", t.priority)}</dl>
      <a class="btn ghost obsidian" href="${esc(obsidian(t.path))}">${icon("book-open")} Open in Obsidian</a>
      <p class="todo-path">${esc(t.path)}, line ${t.line}</p>
      ${w ? `<div class="todo-actions">
        ${done ? `<button class="btn" type="button" data-act="reopen">${icon("rotate-ccw")} Reopen</button>`
               : `<button class="btn" type="button" data-act="done">${icon("check")} Mark done</button>
                  ${t.status === "in_progress" ? "" : `<button class="btn ghost" type="button" data-act="start">Start</button>`}
                  <button class="btn ghost" type="button" data-act="cancel">${icon("ban")} Cancel</button>`}
      </div>
      ${done ? "" : `<form class="todo-edit" id="todo-due"><label for="td-due">Due date</label><input class="field" id="td-due" type="date" value="${esc(t.due || "")}">
        <button class="btn ghost sm" type="submit">Set</button>${t.due ? `<button class="btn ghost sm" type="button" data-clear-due>Clear</button>` : ""}</form>
      <form class="todo-edit" id="todo-prio"><label for="td-prio">Priority</label><select class="field" id="td-prio">
        ${["", "highest", "high", "medium", "low", "lowest"].map((p) => `<option value="${p}" ${(t.priority || "") === p ? "selected" : ""}>${p ? p[0].toUpperCase() + p.slice(1) : "None"}</option>`).join("")}</select>
        <button class="btn ghost sm" type="submit">Set</button></form>`}` : `<p class="muted">Vault writes are off (vault.write in jarvis.config.json), so this to-do can only be changed in Obsidian.</p>`}`;
    $$("[data-act]", $("#task-detail")).forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.act === "cancel" && b.dataset.armed !== "1") { b.dataset.armed = "1"; b.lastChild.textContent = " Confirm cancel"; return; }
      act(id, b.dataset.act);
    }));
    $("#todo-due")?.addEventListener("submit", (e) => { e.preventDefault(); act(id, "due", $("#td-due").value || null); });
    $("[data-clear-due]")?.addEventListener("click", () => act(id, "due", null));
    $("#todo-prio")?.addEventListener("submit", (e) => { e.preventDefault(); act(id, "priority", $("#td-prio").value || null); });
    drawer.hidden = false; requestAnimationFrame(() => drawer.classList.add("open"));
  }
  $("#todo-board").addEventListener("click", (e) => {
    const tick = e.target.closest("[data-tick]"), open = e.target.closest("[data-open]");
    if (tick) { const t = byId.get(tick.dataset.tick); act(tick.dataset.tick, t?.status === "done" ? "reopen" : "done"); }
    else if (open) openTodo(open.dataset.open);
  });
  $("#quick-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("#qa-text").value.trim(); if (!text) return;
    const btn = $("#qa-add"); btn.disabled = true;
    try {
      await J.api("/api/vault/tasks", { text, due: $("#qa-due").value || null, priority: $("#qa-prio").value || null });
      $("#qa-text").value = ""; $("#qa-due").value = ""; $("#qa-prio").value = "";
      J.toast("Added to the Inbox in tasks.md.");
      await loadTodos();
    } catch (err) { J.toast(err.message); }
    finally { btn.disabled = false; $("#qa-text").focus(); }
  });
  // Obsidian edits arrive through the console's file watcher, JARVIS's own through its
  // writes; both are "vault.changed". Coalesce bursts (a save touches a note twice).
  let vaultTimer;
  on("vault", () => { clearTimeout(vaultTimer); vaultTimer = setTimeout(() => { if (view === "tasks" && pane === "todos" && !todoBusy) loadTodos(); }, 400); });

  // ---------- detail drawer ----------
  const drawer = $("#task-drawer");
  on("detail", () => {
    delete drawer.dataset.todo;
    if (pane !== "runs") setPane("runs");
    drawer.hidden = false; requestAnimationFrame(() => drawer.classList.add("open"));
  });
  function closeDrawer() { drawer.classList.remove("open"); delete drawer.dataset.todo; setTimeout(() => { if (!drawer.classList.contains("open")) drawer.hidden = true; }, reduce.matches ? 0 : 220); }
  $("#drawer-close").addEventListener("click", closeDrawer);
  addEventListener("keydown", (e) => { if (e.key === "Escape" && !drawer.hidden) { e.stopPropagation(); closeDrawer(); } }, true);

  // ---------- boot from whatever app.js already has ----------
  $("#theme-toggle").addEventListener("click", () => setTimeout(silk, 30));
  onView(location.hash.slice(1) || "console");
  if (J.state.overview) { recent(J.state.overview); timeline(); }
})();
