// JARVIS Markets: a Bloomberg-style recon terminal over True Markets data.
// Read-only: every call goes to the local console server, which only exposes market data
// and the strategy lab. Nothing here can place, cancel or transfer anything.
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };
  async function api(path, body) {
    const opts = body === undefined ? {} : { method: "POST", headers: { "X-JARVIS": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---------- number formats ----------
  function px(v) {
    if (v == null) return "—";
    const a = Math.abs(v);
    const d = a >= 1000 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 8;
    return v.toLocaleString("en-US", { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
  }
  function big(v) {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(Math.round(v));
  }
  const pct = (v, d = 2) => v == null ? `<span class="dim">—</span>` : `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${v.toFixed(d)}</span>`;

  // ---------- state ----------
  const WINDOWS = [["1H", "1h", "1m"], ["4H", "4h", "5m"], ["1D", "1d", "15m"], ["7D", "7d", "1h"], ["30D", "30d", "4h"], ["90D", "90d", "1d"], ["180D", "180d", "1d"], ["365D", "365d", "1d"]];
  const S = { rows: [], symbol: store.get("tm.symbol", "BTC"), win: store.get("tm.win", "1D"), last: {}, quote: null, hist: [], strategies: [], templates: [], current: null, results: null };

  // ---------- clock + status ----------
  function tick() { $("#clock").textContent = new Date().toLocaleTimeString("en-GB", { hour12: false }); }
  setInterval(tick, 1000); tick();
  async function status() {
    try {
      const s = await api("/api/markets/status");
      const f = $("#feed");
      f.textContent = s.configured ? `TM ${s.key_id || "LIVE"}` : "TM NO KEY";
      f.className = "tag " + (s.configured ? "ok" : "bad");
      f.title = s.configured ? `True Markets key "${s.label || ""}" · read-only` : "Run: jarvis markets setup <truemarkets-key.json>";
      return s.configured;
    } catch (e) { $("#feed").textContent = "TM OFFLINE"; $("#feed").className = "tag bad"; return false; }
  }

  // ---------- sparkline ----------
  function spark(points, w = 70, h = 16) {
    if (!points?.length) return "";
    const lo = Math.min(...points), hi = Math.max(...points), span = hi - lo || 1;
    const d = points.map((v, i) => `${(i / (points.length - 1)) * w},${h - 1 - ((v - lo) / span) * (h - 2)}`).join(" ");
    const color = points.at(-1) >= points[0] ? "var(--up)" : "var(--down)";
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline fill="none" stroke="${color}" stroke-width="1.2" points="${d}"/></svg>`;
  }

  // ---------- monitor ----------
  async function loadMonitor() {
    try {
      const m = await api("/api/markets/monitor?limit=40");
      S.rows = m.rows;
      renderMonitor();
      renderTape();
      $("#mon-meta").textContent = `${m.rows.length} assets · top by mkt cap · ${new Date(m.at * 1000).toLocaleTimeString("en-GB", { hour12: false })}`;
    } catch (e) { $("#mon tbody").innerHTML = `<tr><td colspan="8" class="down">${esc(e.message)}</td></tr>`; }
  }
  function renderMonitor() {
    $("#mon tbody").innerHTML = S.rows.map((r, i) => {
      const prev = S.last[r.symbol], flash = prev == null || prev === r.last ? "" : r.last > prev ? "flash-up" : "flash-down";
      S.last[r.symbol] = r.last;
      const pos = r.high > r.low ? ((r.last - r.low) / (r.high - r.low)) * 100 : 50;
      return `<tr data-sym="${esc(r.symbol)}" class="${r.symbol === S.symbol ? "sel" : ""}">
        <td class="dim">${i + 1}</td><td><b>${esc(r.symbol)}</b><i>${esc(r.name)}</i></td>
        <td class="r ${flash}">${px(r.last)}</td><td class="r">${pct(r.chg_1h)}</td><td class="r">${pct(r.chg_24h)}</td>
        <td title="L ${px(r.low)} · H ${px(r.high)}"><span class="range"><i style="left:${pos.toFixed(1)}%"></i></span></td>
        <td class="r">${big(r.market_cap)}</td><td>${spark(r.spark)}</td></tr>`;
    }).join("") || `<tr><td colspan="8" class="dim">No data.</td></tr>`;
  }
  $("#mon").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-sym]"); if (tr) select(tr.dataset.sym); });
  function renderTape() {
    const items = S.rows.filter((r) => r.chg_24h != null).map((r) => `<span><b>${esc(r.symbol)}</b>${px(r.last)} ${pct(r.chg_24h)}</span>`).join("");
    $("#tape").innerHTML = items + items; // doubled for a seamless loop
  }

  // ---------- chart (GP) ----------
  function renderWindows() {
    $("#win").innerHTML = WINDOWS.map(([k]) => `<button type="button" aria-pressed="${k === S.win}" data-win="${k}">${k}</button>`).join("");
  }
  $("#win").addEventListener("click", (e) => { const b = e.target.closest("button[data-win]"); if (b) { S.win = b.dataset.win; store.set("tm.win", S.win); renderWindows(); loadChart(); } });

  async function select(symbol, win) {
    S.symbol = symbol.toUpperCase(); store.set("tm.symbol", S.symbol);
    if (win) { S.win = win; store.set("tm.win", win); renderWindows(); }
    for (const tr of document.querySelectorAll("#mon tr[data-sym]")) tr.classList.toggle("sel", tr.dataset.sym === S.symbol);
    $("#sec-title").textContent = `${S.symbol} GP`;
    await Promise.all([loadQuote(), loadChart()]);
  }
  async function loadQuote() {
    const sym = S.symbol;
    try {
      const q = await api("/api/markets/quote?symbol=" + encodeURIComponent(sym));
      if (sym !== S.symbol) return;
      S.quote = q;
      const c = q.change || {}, a = q.asset || {}, d24 = q.candles?.["24h"] || {};
      $("#quote").innerHTML = `<span class="px">${px(q.last)}</span>
        <span><span class="k">1H</span>${pct(c["1h"])}</span><span><span class="k">4H</span>${pct(c["4h"])}</span><span><span class="k">24H</span>${pct(c["24h"])}</span>
        <span><span class="k">24H H/L</span>${px(d24.high)} / ${px(d24.low)}</span><span><span class="k">MCAP</span>${big(q.market_cap)}</span>`;
      const supply = a.circulating_supply ? `${big(a.circulating_supply)}${a.max_supply ? " / " + big(a.max_supply) : ""}` : "—";
      $("#des").innerHTML = `<span class="k">NAME</span><span>${esc(a.name || sym)}</span><span class="k">CHAIN</span><span>${esc(a.chain || "—")}</span>
        <span class="k">VENUE</span><span>${esc((a.venue || "—").toUpperCase())}</span><span class="k">SUPPLY</span><span>${supply}</span>
        ${a.website ? `<span class="k">WEB</span><span><a href="${esc(a.website)}" target="_blank" rel="noopener noreferrer">${esc(a.website.replace(/^https?:\/\//, "").replace(/\/$/, ""))}</a></span>` : ""}
        ${a.x ? `<span class="k">X</span><span>@${esc(a.x)}</span>` : ""}
        ${a.description ? `<p>${esc(a.description)}</p>` : ""}`;
    } catch (e) { $("#quote").innerHTML = `<span class="down">${esc(e.message)}</span>`; $("#des").innerHTML = ""; }
  }
  async function loadChart() {
    const sym = S.symbol, [, window_, res] = WINDOWS.find(([k]) => k === S.win) || WINDOWS[2];
    try {
      const pts = await api(`/api/markets/history?symbol=${encodeURIComponent(sym)}&window=${window_}&resolution=${res}`);
      if (sym !== S.symbol) return;
      S.hist = pts; drawChart();
    } catch (e) { S.hist = []; drawChart(e.message); }
  }

  function canvas(el) {
    const dpr = Math.min(devicePixelRatio || 1, 2), w = el.clientWidth, h = el.clientHeight;
    if (el.width !== Math.round(w * dpr) || el.height !== Math.round(h * dpr)) { el.width = Math.round(w * dpr); el.height = Math.round(h * dpr); }
    const g = el.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    return { g, w, h };
  }
  // Shared line chart: series = [{values, color, fill}], labels = times (ISO) or null.
  function plot(el, series, labels, opts = {}) {
    const { g, w, h } = canvas(el);
    const pad = { l: 8, r: 64, t: 10, b: 18 };
    const all = series.flatMap((s) => s.values).filter((v) => v != null);
    if (!all.length) { g.fillStyle = css("--ink-3"); g.font = `12px ${css("--mono")}`; g.fillText(opts.empty || "No data", 12, 22); return null; }
    let lo = Math.min(...all), hi = Math.max(...all);
    if (lo === hi) { lo -= 1; hi += 1; }
    const span = hi - lo; lo -= span * 0.04; hi += span * 0.04;
    const n = Math.max(...series.map((s) => s.values.length));
    const X = (i) => pad.l + (i / Math.max(1, n - 1)) * (w - pad.l - pad.r);
    const Y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
    g.font = `10.5px ${css("--mono")}`; g.lineWidth = 1;
    for (let k = 0; k <= 4; k++) { // grid + right axis
      const v = lo + ((hi - lo) * k) / 4, y = Y(v);
      g.strokeStyle = "#11161c"; g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
      g.fillStyle = css("--ink-3"); g.fillText(opts.fmt ? opts.fmt(v) : px(v), w - pad.r + 6, y + 3);
    }
    if (labels?.length) {
      g.fillStyle = css("--ink-3");
      for (let k = 0; k < 4; k++) {
        const i = Math.round((k / 3) * (labels.length - 1)), t = new Date(labels[i]);
        const text = opts.long ? t.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) : t.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
        g.fillText(text, Math.min(Math.max(X(i) - 18, pad.l), w - pad.r - 40), h - 4);
      }
    }
    for (const s of series) {
      g.beginPath(); let started = false;
      s.values.forEach((v, i) => { if (v == null) return; started ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)); started = true; });
      g.strokeStyle = s.color; g.lineWidth = s.width || 1.4; g.stroke();
      if (s.fill) {
        g.lineTo(X(s.values.length - 1), h - pad.b); g.lineTo(X(0), h - pad.b); g.closePath();
        const grad = g.createLinearGradient(0, pad.t, 0, h - pad.b); grad.addColorStop(0, s.fill); grad.addColorStop(1, "transparent");
        g.fillStyle = grad; g.fill();
      }
    }
    if (opts.lastTag) { // last price tag on the axis
      const v = series[0].values.at(-1), y = Y(v);
      g.fillStyle = series[0].color; g.fillRect(w - pad.r + 2, y - 7, pad.r - 4, 14);
      g.fillStyle = "#000"; g.fillText(px(v), w - pad.r + 5, y + 3.5);
    }
    return { X, Y, n, pad, w, h };
  }
  let chartGeo = null;
  function drawChart(error) {
    const values = S.hist.map((p) => p.price), up = values.length > 1 && values.at(-1) >= values[0];
    const color = up ? css("--up") : css("--down");
    const long = ["30D", "90D", "180D", "365D", "7D"].includes(S.win);
    chartGeo = plot($("#gp"), [{ values, color, fill: up ? "rgba(61,220,132,.16)" : "rgba(255,90,82,.16)" }], S.hist.map((p) => p.t), { lastTag: true, long, empty: error || "No history for this window." });
  }
  $("#gp").addEventListener("mousemove", (e) => {
    const hov = $("#gp-hover");
    if (!chartGeo || !S.hist.length) { hov.hidden = true; return; }
    const rect = e.target.getBoundingClientRect(), x = e.clientX - rect.left;
    const i = Math.round(((x - chartGeo.pad.l) / (chartGeo.w - chartGeo.pad.l - chartGeo.pad.r)) * (chartGeo.n - 1));
    const p = S.hist[Math.max(0, Math.min(S.hist.length - 1, i))];
    const chg = ((S.hist.at(-1).price / p.price) - 1) * 100;
    hov.hidden = false;
    hov.innerHTML = `${esc(new Date(p.t).toLocaleString("en-GB", { hour12: false }))} · <b>${px(p.price)}</b> · since ${pct(chg)}%`;
  });
  $("#gp").addEventListener("mouseleave", () => ($("#gp-hover").hidden = true));

  // ---------- intel ----------
  async function loadIntel() {
    try {
      const d = await api("/api/markets/intel");
      const list = (rows, key) => `<ol>${rows.map((r) => `<li data-sym="${esc(r.symbol)}"><b>${esc(r.symbol)}</b><i>${esc(r.name)}</i>${key === "vol" ? `<span>${r.vol?.toFixed(2) ?? "—"}σ</span>` : pct(r.chg_24h)}</li>`).join("")}</ol>`;
      const b = d.breadth, upPct = b.n ? (b.up / b.n) * 100 : 50;
      $("#intel").innerHTML = `
        <div class="ib"><h3>TAPE</h3>
          <div class="kv"><span>Cap-weighted 24h</span>${pct(d.cap_weighted_24h)}</div>
          <div class="kv"><span>BTC dominance</span><span>${d.btc_dominance ?? "—"}%</span></div>
          <div class="kv"><span>Tracked cap</span><span>${big(d.tracked_cap)}</span></div>
          <div class="kv"><span>Breadth ${b.up}↑ ${b.down}↓</span><span>${upPct.toFixed(0)}% up</span></div>
          <div class="breadth" title="${b.up} up · ${b.down} down"><i style="width:${upPct}%"></i></div></div>
        <div class="ib"><h3>TOP GAINERS 24H</h3>${list(d.gainers)}</div>
        <div class="ib"><h3>TOP LOSERS 24H</h3>${list(d.losers)}</div>
        <div class="ib"><h3>MOST VOLATILE · σ HOURLY</h3>${list(d.volatile, "vol")}</div>`;
      $("#intel-meta").textContent = `${b.n} non-stable assets`;
    } catch (e) { $("#intel").innerHTML = `<div class="ib down">${esc(e.message)}</div>`; }
  }
  $("#intel").addEventListener("click", (e) => { const li = e.target.closest("li[data-sym]"); if (li) select(li.dataset.sym); });

  // ---------- strategy lab ----------
  const form = $("#edit");
  form.window.innerHTML = WINDOWS.map(([k, w]) => `<option value="${w}">${k}</option>`).join("");
  form.resolution.innerHTML = ["1m", "5m", "15m", "1h", "4h", "1d"].map((r) => `<option value="${r}">${r.toUpperCase()}</option>`).join("");
  function fill(spec) {
    S.current = spec;
    form.name.value = spec.name || ""; form.symbols.value = (spec.symbols || [S.symbol]).join(", ");
    form.window.value = spec.window || "90d"; form.resolution.value = spec.resolution || "4h";
    form.stop_loss.value = spec.stop_loss ?? 0; form.take_profit.value = spec.take_profit ?? 0;
    form.entry.value = (spec.entry || []).join("\n"); form.exit.value = (spec.exit || []).join("\n");
    $("#del").disabled = !spec.id; msg(spec.about || spec.notes || "");
    renderStrategies();
  }
  function read() {
    const lines = (t) => t.split("\n").map((s) => s.trim()).filter(Boolean);
    return { ...(S.current?.id ? { id: S.current.id } : {}), name: form.name.value.trim(), symbols: form.symbols.value.split(/[\s,]+/).filter(Boolean),
      window: form.window.value, resolution: form.resolution.value, stop_loss: Number(form.stop_loss.value || 0), take_profit: Number(form.take_profit.value || 0),
      entry: lines(form.entry.value), exit: lines(form.exit.value), fee_bps: S.current?.fee_bps ?? 10, notes: S.current?.notes || "" };
  }
  const msg = (t, cls = "dim") => { const m = $("#edit-msg"); m.className = cls; m.textContent = t; };
  function renderStrategies() {
    const signals = S.results?.summary?.signals || {};
    const btn = (s, key) => `<button type="button" data-key="${esc(key)}" class="${S.current && (S.current.id ? S.current.id === s.id : S.current.name === s.name) ? "sel" : ""}">
      <b>${esc(s.name)}</b>${s.id && S.results?.strategy.id === s.id ? Object.entries(signals).slice(0, 1).map(([, v]) => `<em class="sig ${v}">${v}</em>`).join("") : ""}
      <span>${esc(s.kind ? s.kind + " · " + s.resolution : (s.symbols || []).join(" ") + " · " + s.resolution)}</span></button>`;
    $("#strats").innerHTML = `<h4>SAVED</h4>${S.strategies.map((s) => btn(s, "s:" + s.id)).join("") || `<button type="button" disabled><span>None yet — tweak a template and SAVE.</span></button>`}
      <h4>TEMPLATES</h4>${S.templates.map((t, i) => btn(t, "t:" + i)).join("")}`;
  }
  $("#strats").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-key]"); if (!b) return;
    const [kind, key] = b.dataset.key.split(":");
    if (kind === "s") fill(S.strategies.find((s) => s.id === key));
    else { const t = S.templates[+key]; fill({ ...t, symbols: ["BTC", "ETH", "SOL"] }); }
  });
  async function loadStrategies() {
    [S.strategies, S.templates] = await Promise.all([api("/api/strategies"), api("/api/strategies/templates")]);
    if (!S.current) fill(S.strategies[0] || { ...S.templates[0], symbols: ["BTC", "ETH", "SOL"] });
    else renderStrategies();
  }
  form.addEventListener("submit", (e) => { e.preventDefault(); backtest(); });
  async function backtest() {
    const go = form.querySelector(".go"); go.disabled = true; msg("Pulling history and simulating…");
    try {
      const r = await api("/api/strategies/backtest", { spec: read() });
      S.results = r; renderResults(); renderStrategies();
      const bad = r.results.filter((x) => x.error);
      msg(bad.length ? `${bad.length} ticker(s) failed: ${bad.map((x) => x.symbol + " " + x.error).join("; ")}` : `Tested ${r.results.length} ticker(s) · ${r.summary?.trades ?? 0} trades`, bad.length ? "down" : "dim");
    } catch (e) { msg(e.message, "down"); }
    go.disabled = false;
  }
  function renderResults() {
    const r = S.results, ok = r.results.filter((x) => x.metrics);
    const colors = [css("--amber"), css("--blue"), "#c792ea", "#4dd0e1", "#f78c6c", "#c3e88d"];
    const series = ok.slice(0, 6).map((x, i) => ({ values: x.equity.map((v) => (v - 1) * 100), color: colors[i], width: 1.5 }));
    if (ok[0]) series.push({ values: ok[0].hold.map((v) => (v - 1) * 100), color: css("--ink-3"), width: 1 });
    plot($("#eq"), series, ok[0]?.times, { fmt: (v) => v.toFixed(1) + "%", long: true, empty: "No results" });
    const head = `<tr><th>TICKER</th><th class="r">RETURN%</th><th class="r">HOLD%</th><th class="r">SHARPE</th><th class="r">MAX DD%</th><th class="r">TRADES</th><th class="r">WIN%</th><th class="r">EXPO%</th><th>NOW</th></tr>`;
    const rows = r.results.map((x, i) => x.metrics ? `<tr><td><b style="color:${colors[i] || "inherit"}">${esc(x.symbol)}</b></td>
      <td class="r">${pct(x.metrics.total_return)}</td><td class="r">${pct(x.metrics.buy_hold)}</td><td class="r">${x.metrics.sharpe.toFixed(2)}</td>
      <td class="r down">${x.metrics.max_drawdown.toFixed(1)}</td><td class="r">${x.metrics.trades}</td><td class="r">${x.metrics.win_rate ?? "—"}</td>
      <td class="r">${x.metrics.exposure}</td><td><span class="sig ${x.signal}">${x.signal}</span>${x.position.open ? ` <span class="dim">${pct(x.position.unrealized)}%</span>` : ""}</td></tr>`
      : `<tr><td><b>${esc(x.symbol)}</b></td><td colspan="8" class="down">${esc(x.error)}</td></tr>`).join("");
    const s = r.summary;
    const foot = s ? `<tr><td class="dim">AVG</td><td class="r">${pct(s.total_return)}</td><td class="r">${pct(s.buy_hold)}</td><td class="r">${s.sharpe.toFixed(2)}</td><td class="r down">${s.max_drawdown.toFixed(1)}</td><td class="r">${s.trades}</td><td colspan="3" class="dim">grey line = buy &amp; hold ${esc(ok[0]?.symbol || "")}</td></tr>` : "";
    $("#bt").innerHTML = `<thead>${head}</thead><tbody>${rows}${foot}</tbody>`;
  }
  $("#save").addEventListener("click", async () => {
    try { const s = await api("/api/strategies", read()); S.current = s; await loadStrategies(); fill(s); msg(`Saved “${s.name}”.`); }
    catch (e) { msg(e.message, "down"); }
  });
  $("#del").addEventListener("click", async () => {
    if (!S.current?.id) return;
    try { await api("/api/strategies/delete", { id: S.current.id }); S.current = null; await loadStrategies(); msg("Deleted."); }
    catch (e) { msg(e.message, "down"); }
  });
  $("#new").addEventListener("click", () => fill({ name: "", symbols: [S.symbol], window: "90d", resolution: "4h", entry: [], exit: [], stop_loss: 8 }));

  // ---------- command line ----------
  const cmdIn = $("#cmd-in");
  const focusPane = (sel) => { document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("focus", p.matches(sel))); $(sel)?.scrollIntoView({ block: "nearest" }); };
  $("#cmd").addEventListener("submit", (e) => {
    e.preventDefault();
    const words = cmdIn.value.trim().toUpperCase().split(/\s+/).filter(Boolean); cmdIn.value = "";
    if (!words.length) return;
    const [a, b, c] = words, win = WINDOWS.find(([k]) => k === c || k === b)?.[0];
    if (a === "HELP" || a === "?") return $("#help").showModal();
    if (a === "TOP" || a === "MOV" || a === "INTEL") { loadIntel(); return focusPane(".intel"); }
    if (a === "MON") return focusPane(".mon");
    if (a === "STRAT" || a === "LAB") return focusPane(".lab");
    if (a === "NEW") { $("#new").click(); focusPane(".lab"); return form.name.focus(); }
    if (a === "BT") { focusPane(".lab"); return backtest(); }
    if (a === "TPL" && S.templates[+b - 1]) { fill({ ...S.templates[+b - 1], symbols: [S.symbol] }); return focusPane(".lab"); }
    if (a === "DES" && b) { select(b); return focusPane(".chart"); }
    if (/^[A-Z0-9._-]{1,24}$/.test(a)) { select(a, win); focusPane(".chart"); }
  });
  addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
    const keys = { F1: () => $("#help").showModal(), F2: () => focusPane(".mon"), F3: () => focusPane(".chart"), F4: () => focusPane(".intel"), F5: () => focusPane(".lab") };
    if (keys[e.key]) { e.preventDefault(); return keys[e.key](); }
    if (typing) { if (e.key === "Escape") document.activeElement.blur(); return; }
    if (e.key === "/") { e.preventDefault(); cmdIn.focus(); }
    else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && S.rows.length) {
      e.preventDefault();
      const i = S.rows.findIndex((r) => r.symbol === S.symbol), next = S.rows[(i + (e.key === "ArrowDown" ? 1 : -1) + S.rows.length) % S.rows.length];
      select(next.symbol);
      document.querySelector(`#mon tr[data-sym="${CSS.escape(next.symbol)}"]`)?.scrollIntoView({ block: "nearest" });
    }
  });

  // ---------- boot ----------
  let resizeTimer;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { drawChart(); if (S.results) renderResults(); }, 120); });
  renderWindows();
  (async () => {
    const ok = await status();
    loadStrategies().catch((e) => msg(e.message, "down"));
    if (!ok) {
      $("#mon tbody").innerHTML = `<tr><td colspan="8" class="down">No True Markets key on this laptop. Run: jarvis markets setup &lt;truemarkets-key.json&gt;</td></tr>`;
      return;
    }
    loadMonitor(); loadIntel(); select(S.symbol);
    setTimeout(() => { if (!S.results && form.entry.value.trim()) backtest(); }, 600); // show the lab working
    setInterval(() => { if (!document.hidden) loadMonitor(); }, 30e3);
    setInterval(() => { if (!document.hidden) loadIntel(); }, 60e3);
    setInterval(() => { if (!document.hidden) { loadQuote(); if (["1H", "4H", "1D"].includes(S.win)) loadChart(); } }, 15e3);
  })();
})();
