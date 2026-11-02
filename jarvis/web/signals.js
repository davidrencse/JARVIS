// Signal tiles and wallet widgets: dot-matrix figures drawn on canvas from JARVIS's own
// data (turns, Claude's hourly output, the Codex window, Zenbook pings), a balance card
// and a tabbed activity table. app.js owns the data and announces it as "jarvis:*"
// events; this file only draws. Monochrome: lit dots are the ink, unlit dots a faint step.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const { esc } = J;
  const on = (name, fn) => addEventListener("jarvis:" + name, (e) => fn(e.detail));
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const fmt = new Intl.NumberFormat();
  const compact = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K" : String(Math.round(n)));

  // ---------- dot-matrix canvas ----------
  // Every figure is dots on a grid: lit at the ink, unlit at a faint step of it.
  function surface(canvas) {
    const dpr = Math.min(devicePixelRatio || 1, 2), w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return null;
    if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    return { g, w, h, ink: css("--ink"), off: css("--dot-off") || "rgba(255,255,255,.12)" };
  }
  const dot = (g, x, y, r, color) => { g.fillStyle = color; g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill(); };

  function bars(canvas, values) {
    const s = surface(canvas); if (!s) return;
    const { g, w, h, ink, off } = s, cols = values.length, step = 6, rows = Math.floor(h / step);
    const pitch = w / cols, max = Math.max(...values, 1);
    values.forEach((v, i) => {
      const lit = Math.round((v / max) * rows), x = i * pitch + pitch / 2;
      for (let r = 0; r < rows; r++) dot(g, x, h - 3 - r * step, 1.6, r < lit ? (i >= cols - 3 ? ink : css("--ink-2")) : off);
    });
  }
  function ring(canvas, pct) {
    const s = surface(canvas); if (!s) return;
    const { g, w, h, ink, off } = s, cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 4, n = 72;
    for (let k = 0; k < 3; k++) { // three concentric rows of dots, like a dial's track
      const r = R - k * 6, count = Math.round(n * (r / R));
      for (let i = 0; i < count; i++) {
        const a = -Math.PI / 2 + (i / count) * Math.PI * 2;
        dot(g, cx + r * Math.cos(a), cy + r * Math.sin(a), k === 0 ? 1.7 : 1.3, pct != null && i / count < pct / 100 ? (k === 0 ? ink : css("--ink-2")) : off);
      }
    }
  }
  function trace(canvas, samples) {
    const s = surface(canvas); if (!s) return;
    const { g, w, h, ink, off } = s, mid = h / 2, step = 4, n = Math.floor(w / step);
    for (let x = 2; x < w; x += step) dot(g, x, mid, 1, off);
    if (!samples.length) return;
    const max = Math.max(...samples, 40), recent = samples.slice(-12);
    // Each ping is a heartbeat-shaped blip whose height is the round trip.
    recent.forEach((ms, i) => {
      const cx = ((i + 1) / (recent.length + 1)) * w, amp = Math.min(1, ms / max) * (mid - 6);
      const shape = [0, -0.25, 1, -0.6, 0.15, 0];
      shape.forEach((k, j) => {
        const x = cx + (j - 2.5) * step, y = mid - k * amp;
        const pts = Math.max(1, Math.round(Math.abs(y - mid) / step));
        for (let p = 0; p <= pts; p++) dot(g, x, mid + ((y - mid) * p) / pts, 1.4, i === recent.length - 1 ? ink : css("--ink-2"));
      });
    });
  }
  // A dot face: two eyes and a mouth; the eyes blink and the mouth opens while busy.
  function face(canvas, mood, t) {
    const s = surface(canvas); if (!s) return;
    const { g, w, h, ink, off } = s, cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 3;
    for (let i = 0; i < 40; i++) { const a = (i / 40) * Math.PI * 2; dot(g, cx + R * Math.cos(a), cy + R * Math.sin(a), 1.2, mood === "offline" ? off : css("--ink-2")); }
    const blink = mood !== "offline" && Math.sin(t / 900) > 0.97;
    for (const ex of [-0.34, 0.34]) {
      if (blink) for (let k = -1; k <= 1; k++) dot(g, cx + ex * R + k * 3, cy - 0.12 * R, 1.3, ink);
      else dot(g, cx + ex * R, cy - 0.14 * R, 2.4, mood === "offline" ? off : ink);
    }
    const open = mood === "thinking" ? (Math.sin(t / 180) + 1) / 2 : mood === "listening" ? 0.35 : 0;
    for (let k = -3; k <= 3; k++) {
      const x = cx + k * 0.1 * R, y = cy + 0.34 * R + (mood === "idle" ? -Math.abs(k) * 0.025 * R : 0);
      dot(g, x, y, 1.3, ink);
      if (open > 0.1 && Math.abs(k) < 3) dot(g, x, y + open * 0.16 * R, 1.3, css("--ink-2"));
    }
  }
  function wave(canvas, energy, t) {
    const s = surface(canvas); if (!s) return;
    const { g, w, h, ink, off } = s, mid = h / 2, step = 5;
    for (let x = 3, i = 0; x < w; x += step, i++) {
      const env = Math.exp(-Math.pow((x - w / 2) / (w / 3.2), 2));
      const a = energy * env * (0.55 + 0.45 * Math.sin(i * 1.7 + t / 140) * Math.cos(i * 0.6 - t / 260));
      const n = Math.round(Math.abs(a) * (mid - 3) / 3);
      dot(g, x, mid, 1.2, energy > 0.02 ? ink : off);
      for (let k = 1; k <= n; k++) { dot(g, x, mid - k * 3, 1.1, css("--ink-2")); dot(g, x, mid + k * 3, 1.1, css("--ink-2")); }
    }
  }

  // ---------- 01 Agent ----------
  const agent = { mood: "idle", active: new Set(), listening: false, live: true, today: 0 };
  const WORD = { idle: "Idle", thinking: "Thinking", listening: "Listening", offline: "Offline" };
  function mood() {
    agent.mood = !agent.live ? "offline" : agent.active.size ? "thinking" : agent.listening ? "listening" : "idle";
    $("#sig-state").textContent = WORD[agent.mood] + (agent.mood === "thinking" ? "…" : "");
    $("#sig-agent-flag").textContent = agent.active.size > 1 ? `${agent.active.size} turns` : "";
    $("#sig-agent").dataset.mood = agent.mood;
  }
  function agentStats() {
    const o = J.state.overview;
    const rows = [["Today", String(agent.today)], ["Memory", o ? fmt.format(o.memory.items) : "—"], ["Listener", agent.listening ? "On" : "Off"]];
    $("#sig-agent-stats").innerHTML = rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("");
  }
  on("live", (e) => {
    const d = e.data || {};
    if (e.kind === "turn.start") agent.active.add(d.message || d.job || Math.random());
    if (e.kind === "turn.done" || e.kind === "turn.error") { agent.active.delete(d.message); agent.active.delete(d.job); if (!d.message && !d.job) agent.active.clear(); loadToday(); }
    if (e.kind === "voice.state") agent.listening = !!d.listening;
    if (e.kind === "hello") { agent.live = true; agent.active.clear(); }
    mood(); agentStats();
  });
  on("overview", (o) => { agent.listening = !!o?.voice?.listening; mood(); agentStats(); });
  async function loadToday() {
    try {
      const h = await J.api("/api/history?limit=60"), start = new Date(); start.setHours(0, 0, 0, 0);
      agent.today = h.filter((x) => Date.parse(x.at) >= start).length; agentStats();
      activityRequests = h; if (actTab === "requests") drawActivity();
    } catch (e) {}
  }
  // The live chip already tracks the stream; mirror it so "Offline" means the console lost it.
  new MutationObserver(() => { const c = $("#live-chip"); agent.live = !c || !/reconnect|connecting|offline/i.test(c.textContent); mood(); })
    .observe($("#live-chip") || document.body, { subtree: true, childList: true, characterData: true });

  let raf = 0;
  function animate(t) {
    raf = 0;
    if (document.hidden || $("#view-console")?.hidden) return;
    face($("#dm-face"), agent.mood, t);
    wave($("#dm-wave"), agent.mood === "thinking" ? 0.9 : agent.mood === "listening" ? 0.35 + (window.Terrain?.level || 0) : 0, t);
    // Moves only while something is happening; idle draws one still frame. The listener is
    // on most of the day, so the motion runs at ~15 fps rather than every frame.
    if (!reduce.matches && (agent.mood === "thinking" || agent.mood === "listening")) raf = setTimeout(() => requestAnimationFrame(animate), 66);
  }
  const kick = () => { if (!raf) raf = requestAnimationFrame(animate); };
  on("live", kick); on("view", kick); addEventListener("resize", kick);
  setInterval(kick, 4000); // the occasional blink while idle

  // ---------- 02 Claude tokens ----------
  async function loadTokens() {
    let u; try { u = await J.api("/api/usage?hours=48"); } catch (e) { if ($("#sig-tok")) $("#sig-tok").textContent = "—"; return; }
    const cur = u.output.slice(-24), prev = u.output.slice(0, -24);
    const total = cur.reduce((a, b) => a + b, 0), before = prev.reduce((a, b) => a + b, 0);
    if (claudeTokens !== total) { claudeTokens = total; limit(); }
    if (!$("#sig-tok")) return; // the old tokens tile is gone from the console
    const replies = u.replies.slice(-24).reduce((a, b) => a + b, 0);
    $("#sig-tok").textContent = compact(total);
    bars($("#dm-bars"), cur);
    $("#sig-tok-stats").innerHTML = `<div><dt>Replies</dt><dd>${fmt.format(replies)}</dd></div>`;
    const d = $("#sig-tok-delta");
    if (!before) { d.textContent = "—"; d.title = "No output in the 24 hours before to compare"; }
    else { const ch = Math.round(((total - before) / before) * 100); d.textContent = `${ch >= 0 ? "+" : "−"}${Math.abs(ch)}%`; d.title = "Compared with the 24 hours before"; }
  }

  // ---------- 03 Codex window ----------
  let credits = J.state.credits;
  const until = (iso) => { const s = Math.max(0, (Date.parse(iso) - Date.now()) / 1000);
    return s < 3600 ? Math.max(1, Math.round(s / 60)) + " min" : s < 86400 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " d"; };
  // One ring per subscription window (Codex is the brain, Claude the failover): the same
  // picture for both, so the two quotas read side by side.
  const row = (k, v, tone) => `<li class="${tone || ""}"><i aria-hidden="true"></i><span>${esc(k)}</span><b>${esc(v)}</b></li>`;
  function windowTile(id, ids, extra) {
    const prov = credits?.find((r) => r.id === id), meters = prov?.meters || [];
    const five = meters.find((m) => /5.?h/i.test(m.label || "")) || meters[0];
    const week = meters.find((m) => /week/i.test(m.label || "")) || meters[1];
    const left = five ? Math.max(0, 100 - (five.pct ?? 0)) : null;
    const name = id === "claude" ? "Claude" : "Codex";
    const canvas = $(ids.ring);
    ring(canvas, left);
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", left == null ? `${name}: no window data` : `${name}: ${Math.round(left)}% of the 5-hour window left`);
    $(ids.num).textContent = left == null ? "—" : Math.round(left) + "%";
    $(ids.tile).classList.toggle("low", left != null && left < 15);
    $(ids.flag).textContent = five?.resets_at ? "resets in " + until(five.resets_at) : "";
    $(ids.legend).innerHTML = prov && meters.length
      ? row("5 h used", five ? Math.round(five.pct ?? 0) + "%" : "—", "lit") +
        row("Week left", week ? Math.round(100 - (week.pct ?? 0)) + "%" : "—", "half") +
        row("Week resets", week?.resets_at ? until(week.resets_at) : "—", "off") + (extra || "")
      : row(prov?.status === "error" ? `${name} usage can't be read` : `No ${name} data yet`, "—", "off") + (extra || "");
  }
  let claudeTokens = null;
  function limit() {
    windowTile("codex", { ring: "#dm-ring", num: "#sig-ring", tile: "#sig-limit", flag: "#sig-limit-flag", legend: "#sig-ring-legend" });
    windowTile("claude", { ring: "#dm-ring-claude", num: "#sig-ring-claude", tile: "#sig-claude", flag: "#sig-claude-flag", legend: "#sig-ring-claude-legend" },
      claudeTokens == null ? "" : row("Output, 24 h", compact(claudeTokens) + " tok", "off"));
    drawBalance();
  }

  // ---------- 04 Zenbook link ----------
  const pings = [];
  function link(probe) {
    const z = probe?.devices?.zenbook;
    if (z?.ping_ms != null) { pings.push(z.ping_ms); if (pings.length > 30) pings.shift(); }
    trace($("#dm-trace"), pings);
    const stat = (k, v, u) => `<div><dt>${k}</dt><dd>${v == null ? "—" : esc(String(v))}${v == null || !u ? "" : `<small>${u}</small>`}</dd></div>`;
    $("#sig-link-stats").innerHTML = stat("Ping", z?.ping_ms != null ? Math.round(z.ping_ms) : null, "ms") +
      stat("Free", z?.mem_avail_pct, "%") + stat("Load", z?.load);
    const level = probe?.input_link?.level, pill = $("#sig-link-pill");
    pill.textContent = !z ? "Checking" : !z.reachable ? "Unreachable" : level === "down" ? "Link down" : level === "warn" ? "Up, with warnings" : "Stable";
    pill.dataset.tone = !z ? "" : !z.reachable || level === "down" ? "bad" : level === "warn" ? "warn" : "ok";
    $("#sig-link-flag").textContent = z?.ip || "";
  }
  on("probe", link);

  // ---------- balance card (reference 2) ----------
  function drawBalance() {
    const ds = credits?.find((r) => r.id === "deepseek");
    const num = $("#bal-num"), sub = $("#bal-sub");
    if (!ds) { num.textContent = "—"; sub.textContent = "Checking the provider…"; return; }
    if (ds.headline) {
      const v = Number(ds.headline.value);
      num.innerHTML = `${ds.headline.unit === "USD" ? "$" : ""}${fmt.format(Math.floor(v))}<small>.${v.toFixed(2).split(".")[1]}${ds.headline.unit === "USD" ? "" : " " + esc(ds.headline.unit)}</small>`;
      const granted = ds.facts.find((f) => f[0] === "Granted")?.[1], topped = ds.facts.find((f) => f[0] === "Topped up")?.[1];
      sub.textContent = `Topped up ${topped ?? "—"}, granted ${granted ?? "—"}, checked ${J.ago(ds.checked_at)}`;
    } else { num.textContent = "—"; sub.textContent = ds.status === "error" ? ds.note : "No key yet: run jarvis credits key deepseek"; }
  }
  on("credits", (d) => { credits = d; limit(); if (actTab === "providers") drawActivity(); });
  $("#bal-check").addEventListener("click", async () => {
    const b = $("#bal-check"); b.disabled = true;
    try { credits = await J.api("/api/credits?refresh=1"); limit(); J.toast("Balances checked."); } catch (e) { J.toast(e.message); }
    b.disabled = false;
  });

  // ---------- activity table (the reference's order book) ----------
  let actTab = "requests", activityRequests = [];
  const hhmm = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  function drawActivity() {
    const t = $("#act-table");
    let head = "", rows = [];
    if (actTab === "requests") {
      head = `<tr><th>Asked</th><th class="num">Tier</th><th class="num">Took</th><th class="num">At</th></tr>`;
      rows = activityRequests.slice(-6).reverse().map((h) => {
        const secs = h.reply ? Math.max(0, (Date.parse(h.reply.at) - Date.parse(h.at)) / 1000) : null;
        return `<tr><td class="ident" title="${esc(h.text)}">${esc(h.text)}</td><td class="num">${esc(J.tierName(h.tier) || "—")}</td><td class="num">${secs == null ? "—" : secs < 60 ? secs.toFixed(1) + " s" : Math.round(secs / 60) + " min"}</td><td class="num">${hhmm(h.at)}</td></tr>`;
      });
    } else if (actTab === "providers") {
      head = `<tr><th>Provider</th><th class="num">Left</th><th class="num">Source</th></tr>`;
      rows = (credits || []).filter((r) => r.status !== "unconfigured").map((r) => {
        const v = r.headline ? (r.headline.unit === "USD" ? "$" + Number(r.headline.value).toFixed(2) : r.headline.unit === "%" ? Math.round(r.headline.value) + "%" : compact(r.headline.value) + " " + r.headline.unit) : "—";
        return `<tr><td class="ident"><span class="mono-avatar sm">${esc(r.name[0])}</span>${esc(r.name)}</td><td class="num">${esc(v)}</td><td class="num muted">${esc(r.status === "error" ? "check failed" : r.source)}</td></tr>`;
      });
    } else {
      head = `<tr><th>Remembered</th><th class="num">Kind</th><th class="num">When</th></tr>`;
      rows = (J.state.overview?.memory?.recent || []).slice(0, 6).map((m) =>
        `<tr><td class="ident" title="${esc(m.title)}">${esc(m.title)}</td><td class="num">${esc(m.kind)}</td><td class="num">${esc(J.ago(m.created_at))}</td></tr>`);
    }
    t.innerHTML = `<thead>${head}</thead><tbody>${rows.join("") || `<tr><td class="muted" colspan="4">Nothing here yet.</td></tr>`}</tbody>`;
  }
  const tabs = $$("#w-activity [data-act-tab]");
  tabs.forEach((b, i) => {
    b.addEventListener("click", () => { actTab = b.dataset.actTab; tabs.forEach((x) => { x.setAttribute("aria-selected", x === b); x.tabIndex = x === b ? 0 : -1; }); drawActivity(); });
    b.addEventListener("keydown", (e) => { const d = { ArrowRight: 1, ArrowLeft: -1 }[e.key]; if (d) { e.preventDefault(); const n = tabs[(i + d + tabs.length) % tabs.length]; n.click(); n.focus(); } });
  });
  on("overview", () => { if (actTab === "memory") drawActivity(); });

  // ---------- boot ----------
  const redrawAll = () => { limit(); link(window.Jarvis.state.probe); loadTokens(); kick(); };
  addEventListener("resize", () => { clearTimeout(redrawAll.t); redrawAll.t = setTimeout(() => { limit(); trace($("#dm-trace"), pings); loadTokens(); }, 200); });
  $("#theme-toggle").addEventListener("click", () => setTimeout(() => { limit(); trace($("#dm-trace"), pings); loadTokens(); kick(); }, 40));
  document.fonts?.ready.then(() => kick());
  mood(); agentStats(); redrawAll(); loadToday(); drawActivity();
  setInterval(() => { if (!document.hidden) loadTokens(); }, 5 * 60 * 1000);
})();
