// Porcelain dashboard layer: sidebar pill, header pills, runs timeline, usage trend,
// recent memory and limits with the silk panel. app.js owns
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
  addEventListener("resize", () => { navPill(); rangePill(); });
  // Fonts and wrapping change the pills' targets after first paint; re-measure then too.
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => { navPill(); rangePill(); });
    $$(".rail, .range").forEach((el) => ro.observe(el));
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
    $("#page-title").textContent = $("#view-" + name)?.dataset.title || "Console";
    document.documentElement.dataset.view = name;
    placeDock(); requestAnimationFrame(() => { navPill(); rangePill(); });
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
  on("overview", (o) => { recent(o); timeline();  });

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
          return `<div class="tl-run ${r.state === "finished" || r.state === "done" ? "" : "alt"}"
            style="--row:${rows.indexOf(r.row)};left:min(${left.toFixed(2)}%, calc(100% - 128px));min-width:max(${width.toFixed(2)}%, 118px)"
            title="${esc(r.title)} · ${esc(r.state)}"><span>${esc(dur(r.b - r.a))} · ${esc(J.words(r.state))}</span><span class="tl-who" aria-hidden="true">${esc(r.who)}</span></div>`;
        }).join("")}
      </div>
      <div class="tl-axis">${Array.from({ length: ticks + 1 }, (_, i) => `<span style="left:${(i / ticks) * 100}%">${esc(tickLabel(i))}</span>`).join("")}</div>
    </div>`;
  }
  $$(".range [data-range]").forEach((b) => b.addEventListener("click", () => {
    range = b.dataset.range;
    $$(".range [data-range]").forEach((x) => x.setAttribute("aria-checked", x === b));
    rangePill(); timeline();
  }));
  setInterval(timeline, 60000);

  // ---------- boot from whatever app.js already has ----------
  $("#theme-toggle").addEventListener("click", () => setTimeout(silk, 30));
  onView(location.hash.slice(1) || "console");
  if (J.state.overview) { recent(J.state.overview); timeline(); }
})();
