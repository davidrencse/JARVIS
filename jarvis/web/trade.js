// JARVIS paper trading: build a strategy, test it on real history, switch it on, and the
// console server trades it with practice money (paper.py). Nothing on this page, or behind
// it, can place a real order.
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  async function api(path, body) {
    const opts = body === undefined ? {} : { method: "POST", headers: { "X-JARVIS": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const usd = (v, d) => v == null ? "—" : (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d ?? (Math.abs(v) >= 100 ? 0 : 2), maximumFractionDigits: d ?? (Math.abs(v) >= 100 ? 0 : 2) });
  function price(v) {
    if (v == null) return "—";
    const a = Math.abs(v), d = a >= 1000 ? 0 : a >= 1 ? 2 : a >= 0.01 ? 4 : 8;
    return "$" + v.toLocaleString("en-US", { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
  }
  const signed = (v, d = 1) => v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(d) + "%";
  const signedUsd = (v) => v == null ? "—" : (v > 0 ? "+" : "") + usd(v, 2);
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`; };
  function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 3600); }
  const S = { rows: [], templates: [], style: 0, coins: new Set(["BTC", "ETH", "SOL"]), risk: 10, period: "365d|1d", editing: null,
    entry: [], exit: [], result: null, paper: null, strategies: [], side: "buy", drawer: null };

  // ---------- canvas ----------
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function prep(el) {
    const dpr = Math.min(devicePixelRatio || 1, 2), w = el.clientWidth, h = el.clientHeight;
    el.width = Math.round(w * dpr); el.height = Math.round(h * dpr);
    const g = el.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); return { g, w, h };
  }
  function lineChart(el, series, opts = {}) {
    const { g, w, h } = prep(el), pad = { l: 6, r: 6, t: 12, b: 14 };
    const all = series.flatMap((s) => s.values); if (!all.length) return;
    let lo = Math.min(...all, opts.base ?? Infinity), hi = Math.max(...all, opts.base ?? -Infinity); if (lo === hi) { lo -= 1; hi += 1; }
    const X = (i, n) => pad.l + (i / Math.max(1, n - 1)) * (w - pad.l - pad.r), Y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
    const t0 = performance.now(), dur = reduce || opts.instant ? 0 : 900;
    function draw(now) {
      const k = dur ? Math.max(0, Math.min(1, (now - t0) / dur)) : 1, ease = 1 - Math.pow(1 - k, 3);
      g.clearRect(0, 0, w, h);
      if (opts.base != null) { g.strokeStyle = css("--line-2"); g.lineWidth = 1; g.setLineDash([4, 6]); g.beginPath(); g.moveTo(pad.l, Y(opts.base)); g.lineTo(w - pad.r, Y(opts.base)); g.stroke(); g.setLineDash([]); }
      for (const s of series) {
        const n = s.values.length, upto = Math.max(2, Math.round(n * ease));
        g.beginPath(); s.values.slice(0, upto).forEach((v, i) => (i ? g.lineTo(X(i, n), Y(v)) : g.moveTo(X(i, n), Y(v))));
        g.strokeStyle = s.color; g.lineWidth = s.width || 2; g.lineJoin = "round"; g.setLineDash(s.dash || []); g.stroke(); g.setLineDash([]);
        if (s.dot && upto === n) { g.fillStyle = s.color; g.beginPath(); g.arc(X(n - 1, n), Y(s.values[n - 1]), 4, 0, 7); g.fill(); }
      }
      if (k < 1) requestAnimationFrame(draw);
    }
    requestAnimationFrame(draw);
    if (dur) setTimeout(() => draw(t0 + dur), dur + 150); // frames pause in hidden tabs: land the final one
  }

  // ---------- rules: grammar <-> plain words <-> editor rows ----------
  const TERMS = [["price", "Price", 0], ["sma", "Average", 20], ["ema", "Fast average", 12], ["rsi", "RSI", 14],
    ["roc", "% change", 12], ["high", "Recent high", 20], ["low", "Recent low", 10]];
  const OPS = [[">", "is above"], ["<", "is below"], ["crosses_above", "crosses above"], ["crosses_below", "crosses below"]];
  const UNIT = { "1h": "hour", "4h": "4-hour", "1d": "day" };
  function termWords(t, res) {
    const m = /^(\w+)\((\d+)\)$/.exec(t), u = UNIT[res] || "bar";
    if (!m) return t;
    return { sma: `the ${m[2]}-${u} average`, ema: `the ${m[2]}-${u} fast average`, rsi: "RSI", roc: `the ${m[2]}-${u} change`,
      zscore: `the ${m[2]}-${u} z-score`, high: `the ${m[2]}-${u} high`, low: `the ${m[2]}-${u} low`, vol: `${m[2]}-${u} volatility` }[m[1]] || t;
  }
  function plain(rule, res) {
    const [a, op, b] = rule.split(/\s+/);
    return `${termWords(a, res)} ${(OPS.find((o) => o[0] === op) || [, op])[1]} ${termWords(b, res)}`;
  }
  function parseTerm(t) { const m = /^(\w+)\((\d+)\)$/.exec(t); return m ? { k: m[1], n: +m[2] } : t === "price" ? { k: "price", n: 0 } : { k: "num", n: +t }; }
  const termStr = (t) => t.k === "price" ? "price" : t.k === "num" ? String(t.n) : `${t.k}(${Math.max(1, Math.min(400, t.n | 0))})`;
  function parseRule(r) { const [a, op, b] = r.split(/\s+/); return { l: parseTerm(a), op, r: parseTerm(b) }; }
  const ruleStr = (x) => `${termStr(x.l)} ${x.op} ${termStr(x.r)}`;
  function termEditor(t, side, allowNum) {
    const opts = (allowNum ? [["num", "Number", 0], ...TERMS] : TERMS).map(([k, label]) => `<option value="${k}"${t.k === k ? " selected" : ""}>${label}</option>`).join("");
    return `<select data-f="${side}k" aria-label="Indicator">${opts}</select><input data-f="${side}n" type="number" step="any" value="${t.n}" ${t.k === "price" ? "hidden" : ""} aria-label="${t.k === "num" ? "Value" : "Period"}" title="${t.k === "num" ? "value" : "bars"}">`;
  }
  function renderRules() {
    for (const kind of ["entry", "exit"]) {
      $(`#r-${kind}`).innerHTML = S[kind].map((x, i) => `<div class="rule" data-kind="${kind}" data-i="${i}">${termEditor(x.l, "l", false)}
        <select data-f="op" aria-label="Comparison">${OPS.map(([v, l]) => `<option value="${v}"${x.op === v ? " selected" : ""}>${l}</option>`).join("")}</select>
        ${termEditor(x.r, "r", true)}<button class="x-sm" data-del aria-label="Remove condition">×</button></div>`).join("")
        || `<p class="note">${kind === "exit" ? "No sell rule: only the safety net sells." : "Add at least one."}</p>`;
    }
  }
  $("#rules").addEventListener("change", (e) => {
    const row = e.target.closest(".rule"); if (!row) return;
    const x = S[row.dataset.kind][+row.dataset.i], f = e.target.dataset.f;
    if (f === "op") x.op = e.target.value;
    else if (f.endsWith("k")) { const t = x[f[0]]; t.k = e.target.value; t.n = (TERMS.find((d) => d[0] === t.k) || [, , t.k === "num" ? 30 : 0])[2]; renderRules(); }
    else x[f[0]].n = +e.target.value;
  });
  $("#rules").addEventListener("click", (e) => {
    if (e.target.dataset.add) { S[e.target.dataset.add].push(parseRule(e.target.dataset.add === "entry" ? "rsi(14) < 30" : "rsi(14) > 70")); renderRules(); }
    const del = e.target.closest("[data-del]"); if (del) { const row = del.closest(".rule"); S[row.dataset.kind].splice(+row.dataset.i, 1); renderRules(); }
  });

  // ---------- builder ----------
  const GLYPH = { "Golden cross": "M2 28 C14 26 18 30 26 20 S40 6 58 4", "EMA momentum": "M2 30 L14 26 L22 28 L34 16 L44 18 L58 4",
    "RSI reversion": "M2 8 C14 10 18 30 28 30 S44 10 58 8", "Z-score snapback": "M2 16 L18 16 L24 30 L30 14 L58 16",
    "Donchian breakout": "M2 22 L12 18 L22 22 L32 18 L40 20 L46 8 L58 4" };
  const title = (name) => (S.templates.find((t) => t.name === name) || {}).title || name;
  function renderStyles() {
    $("#styles").innerHTML = S.templates.map((t, i) => `<button class="style" role="radio" data-i="${i}" aria-checked="${i === S.style}"><svg viewBox="0 0 60 34" preserveAspectRatio="none" aria-hidden="true"><path d="${GLYPH[t.name] || "M2 20 L58 12"}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><b>${esc(t.title || t.name)}</b><small>${esc(t.about)}</small></button>`).join("")
      + `<button class="style own" role="radio" data-i="custom" aria-checked="${S.style === "custom"}"><svg viewBox="0 0 60 34" aria-hidden="true"><path d="M30 8v18M21 17h18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><b>My own rules</b><small>Pick the conditions yourself.</small></button>`;
    $("#rules").hidden = S.style !== "custom";
  }
  $("#styles").addEventListener("click", (e) => {
    const b = e.target.closest(".style"); if (!b) return;
    if (b.dataset.i === "custom") {
      if (S.style !== "custom") { const t = S.templates[S.style] || { entry: ["rsi(14) < 30"], exit: ["rsi(14) > 70"] }; S.entry = t.entry.map(parseRule); S.exit = t.exit.map(parseRule); }
      S.style = "custom"; renderStyles(); renderRules(); return;
    }
    S.style = +b.dataset.i; renderStyles();
    const t = S.templates[S.style]; S.period = t.window === "365d" || t.window === "180d" ? "365d|1d" : t.window === "30d" ? "30d|1h" : "90d|4h"; syncSeg("#period", S.period);
  });
  function renderCoins() {
    const list = S.rows.filter((r) => !r.stable).slice(0, 14);
    for (const c of S.coins) if (!list.some((r) => r.symbol === c)) list.push({ symbol: c });
    $("#coins").innerHTML = list.map((r) => `<button class="coin" data-s="${esc(r.symbol)}" aria-pressed="${S.coins.has(r.symbol)}">${esc(r.symbol)}${r.chg_24h != null ? `<small class="${cls(r.chg_24h)}">${signed(r.chg_24h, 1)}</small>` : ""}</button>`).join("");
  }
  $("#coins").addEventListener("click", (e) => {
    const b = e.target.closest(".coin"); if (!b) return; const s = b.dataset.s;
    if (S.coins.has(s)) { if (S.coins.size > 1) S.coins.delete(s); } else if (S.coins.size < 8) S.coins.add(s); else toast("Up to 8 coins per strategy.");
    renderCoins();
  });
  function syncSeg(sel, v) { for (const b of document.querySelectorAll(sel + " button")) b.setAttribute("aria-checked", String(b.dataset.v === String(v))); }
  $("#risk").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.risk = +b.dataset.v; $("#b-stop").value = S.risk; syncSeg("#risk", S.risk); } });
  $("#b-stop").addEventListener("input", () => { S.risk = Number($("#b-stop").value); syncSeg("#risk", S.risk); });
  $("#period").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.period = b.dataset.v; syncSeg("#period", S.period); } });

  function loadIntoBuilder(s, shared = false) {
    S.editing = shared ? null : s.id;
    S.coins = new Set(s.symbols || ["BTC", "ETH", "SOL"]);
    S.period = `${s.window || "90d"}|${s.resolution || "4h"}`;
    S.risk = Number(s.stop_loss || 0);
    $("#b-stop").value = s.stop_loss || 0;
    $("#b-take").value = s.take_profit || 0;
    $("#b-fee").value = s.fee_bps ?? 10;
    $("#b-name").value = s.name || "";
    S.entry = (s.entry || []).map(parseRule); S.exit = (s.exit || []).map(parseRule);
    S.style = "custom"; renderStyles(); renderRules(); renderCoins();
    syncSeg("#risk", S.risk); syncSeg("#period", S.period);
    $("#go").textContent = S.editing ? "Save changes" : "Save & paper trade";
    $("#build").scrollIntoView({ behavior: reduce ? "auto" : "smooth" });
    if (shared) toast("Shared strategy loaded. Review it, then save a copy.");
  }

  function spec() {
    const [window_, res] = S.period.split("|"), own = S.style === "custom", t = own ? null : S.templates[S.style];
    const entry = own ? S.entry.map(ruleStr) : t.entry, exit = own ? S.exit.map(ruleStr) : t.exit;
    const name = $("#b-name").value.trim() || `${own ? "My rules" : t.title || t.name} · ${[...S.coins].join(" ")}`;
    return { ...(S.editing ? { id: S.editing } : {}), name, symbols: [...S.coins], window: window_, resolution: res, entry, exit,
      stop_loss: Number($("#b-stop").value), take_profit: Number($("#b-take").value), fee_bps: Number($("#b-fee").value),
      notes: own ? "Built in the Markets page." : `From the “${t.name}” template.` };
  }
  async function busy(btn, label, fn) {
    const was = btn.textContent; btn.disabled = true; btn.textContent = label;
    try { await fn(); } catch (e) { toast(e.message); }
    btn.disabled = false; btn.textContent = was;
  }
  $("#test").addEventListener("click", () => busy($("#test"), "Testing…", async () => {
    const sp = spec(); const r = await api("/api/strategies/backtest", { spec: sp }); S.result = r; showResult(r, sp);
  }));
  $("#go").addEventListener("click", () => busy($("#go"), "Starting…", async () => {
    const saved = await api("/api/strategies", spec());
    if (S.editing) { await loadStrategies(); S.editing = null; $("#go").textContent = "Save & paper trade"; toast(`Updated “${saved.name}”.`); return; }
    const p = await api("/api/paper/start", { id: saved.id });
    renderPaper(p); await loadStrategies();
    const held = p.bots.find((b) => b.id === saved.id)?.holding || [];
    toast(`“${saved.name}” is paper trading with $1,000.${held.length ? ` Bought ${held.join(", ")}.` : " It'll buy when the rules fire."}`);
    $("#strategies").scrollIntoView({ behavior: reduce ? "auto" : "smooth" });
  }));
  function countUp(el, to) {
    const t0 = performance.now(), dur = reduce ? 0 : 900;
    (function step(now) { const k = dur ? Math.max(0, Math.min(1, (now - t0) / dur)) : 1; el.textContent = usd(1000 + (to - 1000) * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(step); })(t0);
    if (dur) setTimeout(() => (el.textContent = usd(to)), dur + 150);
  }
  function showResult(r, sp) {
    const ok = r.results.filter((x) => x.metrics);
    $("#result").hidden = false;
    if (!ok.length) { $("#res-to").textContent = "—"; $("#res-vs").textContent = r.results.map((x) => `${x.symbol}: ${x.error}`).join(" · "); return; }
    const s = r.summary, end = 1000 * (1 + s.total_return / 100), hold = 1000 * (1 + s.buy_hold / 100);
    const label = { "30d|1h": "the last month", "90d|4h": "the last 3 months", "365d|1d": "the last year" }[sp.window + "|" + sp.resolution] || sp.window;
    $("#res-title").textContent = `$1,000 run on this over ${label}`;
    countUp($("#res-to"), end); $("#res-to").className = cls(s.total_return);
    $("#res-vs").innerHTML = `Just holding: <b class="${cls(s.buy_hold)}">${usd(hold)}</b>. ${end >= hold ? "The strategy did better." : "Holding did better this time."} Worst dip: ${s.max_drawdown.toFixed(0)}%.`;
    $("#res-rows").innerHTML = ok.map((x) => `<div><b>${esc(x.symbol)}</b><span class="${cls(x.metrics.total_return)}">${signed(x.metrics.total_return)}</span><span class="dim">${x.metrics.trades} trades</span><span class="badge ${x.signal}">${x.signal === "HOLD" ? "IN NOW" : x.signal}</span></div>`).join("");
    const n = Math.min(...ok.map((x) => x.equity.length));
    const avg = (key) => Array.from({ length: n }, (_, i) => ok.reduce((a, x) => a + x[key][i], 0) / ok.length * 1000);
    lineChart($("#res-chart"), [{ values: avg("hold"), color: css("--ink-3"), width: 1.5, dash: [5, 5] }, { values: avg("equity"), color: css("--ink"), width: 2, dot: true }], { base: 1000 });
    $("#result").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "nearest" });
  }

  // ---------- your strategies ----------
  async function loadStrategies() { S.strategies = await api("/api/strategies"); renderStrategies(); }
  function renderStrategies() {
    const bots = Object.fromEntries((S.paper?.bots || []).map((b) => [b.id, b]));
    const list = [...S.strategies].sort((a, b) => (bots[b.id]?.on ? 1 : 0) - (bots[a.id]?.on ? 1 : 0) || (b.updated || 0) - (a.updated || 0));
    if (!list.length) { $("#strats").innerHTML = `<p class="note">No strategies yet. Make one below, or say “Jarvis, paper trade buy the dip on bitcoin”.</p>`; return; }
    $("#strats").innerHTML = list.map((s) => {
      const bot = bots[s.id], on = bot?.on, rules = `Buy when ${s.entry.map((r) => plain(r, s.resolution)).join(" and ")}` +
        (s.exit.length ? ` · sell when ${s.exit.map((r) => plain(r, s.resolution)).join(" or ")}` : "") + (s.stop_loss ? ` · safety net −${s.stop_loss}%` : "");
      return `<div class="srow${on ? " on" : ""}" data-id="${esc(s.id)}">
        <div class="s-main"><b>${esc(s.name)}</b><span class="s-rules">${esc(rules)}</span></div>
        <div class="s-coins">${s.symbols.map((c) => `<span class="badge${bot?.holding?.includes(c) ? " HOLD" : " FLAT"}" title="${bot?.holding?.includes(c) ? "holding" : "waiting"}">${esc(c)}</span>`).join("")}</div>
        <div class="s-pnl num">${bot ? `<b class="${cls(bot.pnl)}">${signedUsd(bot.pnl)}</b><small>${on ? `on · ${usd(bot.budget)}` : "off"}</small>` : `<small>not traded yet</small>`}</div>
        <div class="s-acts"><button class="btn sm${on ? " ghost" : ""}" data-act="${on ? "stop" : "start"}">${on ? "Stop" : "Paper trade"}</button>
          <button class="ph-btn" data-act="edit">Edit</button><button class="ph-btn" data-act="share">Share</button><button class="ph-btn" data-act="test">Test</button><button class="ph-btn ghost" data-act="delete" title="Delete">Delete</button></div></div>`;
    }).join("");
  }
  $("#strats").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const id = b.closest(".srow").dataset.id, s = S.strategies.find((x) => x.id === id), act = b.dataset.act;
    if (act === "delete" && b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = "Sure?"; setTimeout(() => { b.dataset.sure = ""; b.textContent = "Delete"; }, 3000); return; }
    await busy(b, "…", async () => {
      if (act === "start") { renderPaper(await api("/api/paper/start", { id })); toast(`“${s.name}” is paper trading.`); }
      else if (act === "stop") { renderPaper(await api("/api/paper/stop", { id })); toast(`Stopped “${s.name}” and sold what it held.`); }
      else if (act === "edit") { loadIntoBuilder(s); }
      else if (act === "share") {
        const copy = { name: s.name, symbols: s.symbols, window: s.window, resolution: s.resolution, entry: s.entry, exit: s.exit, stop_loss: s.stop_loss, take_profit: s.take_profit, fee_bps: s.fee_bps, notes: s.notes };
        const url = new URL(location.href); url.hash = "share=" + btoa(unescape(encodeURIComponent(JSON.stringify(copy)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(url.href);
        else { const input = document.createElement("textarea"); input.value = url.href; document.body.append(input); input.select(); document.execCommand("copy"); input.remove(); }
        toast("Share link copied.");
      }
      else if (act === "test") { const r = await api("/api/strategies/backtest", { id }); S.result = r; showResult(r, s); }
      else if (act === "delete") {
        if (S.paper?.bots.find((x) => x.id === id && x.on)) renderPaper(await api("/api/paper/stop", { id }));
        await api("/api/strategies/delete", { id }); await loadStrategies(); toast(`Deleted “${s.name}”.`);
      }
    });
    renderStrategies();
  });
  function loadShared() {
    const m = /^#share=(.+)$/.exec(location.hash); if (!m) return;
    try {
      const raw = m[1].replace(/-/g, "+").replace(/_/g, "/");
      loadIntoBuilder(JSON.parse(decodeURIComponent(escape(atob(raw + "=".repeat((4 - raw.length % 4) % 4))))), true);
      history.replaceState(null, "", location.pathname + location.search + "#build");
    } catch { toast("That strategy share link is invalid."); }
  }

  // ---------- account, holdings, trades ----------
  function renderPaper(p) {
    S.paper = p;
    $("#a-value").textContent = usd(p.value, 2);
    $("#a-pnl").innerHTML = `<span class="${cls(p.pnl)}">${signedUsd(p.pnl)} (${signed(p.pnl_pct, 2)})</span> since you started with ${usd(p.start)}`;
    $("#a-cash").textContent = usd(p.cash);
    $("#a-inv").textContent = usd(p.positions.reduce((a, x) => a + x.value, 0));
    $("#a-run").textContent = `${p.running} strateg${p.running === 1 ? "y" : "ies"}`;
    $("#ticked").textContent = p.ticked ? `checked ${ago(p.ticked)} · every ${Math.round(p.interval / 60)} min` : "";
    const eq = p.equity.map((x) => x[1]);
    $("#a-empty").hidden = eq.length > 1;
    if (eq.length > 1) lineChart($("#a-chart"), [{ values: eq, color: css("--ink"), width: 2, dot: true }], { base: p.start, instant: renderPaper.drawn });
    else prep($("#a-chart"));
    renderPaper.drawn = true;
    const names = Object.fromEntries(p.bots.map((b) => [b.id, b.name]));
    $("#h-count").textContent = p.positions.length ? `${p.positions.length} position${p.positions.length > 1 ? "s" : ""}` : "";
    $("#positions").innerHTML = p.positions.length ? p.positions.map((x) => `<div class="prow"><b>${esc(x.symbol)}</b>
        <span class="dim">${esc(x.key === "manual" ? "your order" : names[x.key] || "strategy")}</span>
        <span class="num">${usd(x.value, 2)}</span><span class="num ${cls(x.pnl)}">${signed(x.pnl_pct, 2)}</span></div>`).join("")
      : `<p class="note">Nothing held. Running strategies buy when their rules fire.</p>`;
    $("#trades").innerHTML = p.trades.length ? p.trades.slice(0, 12).map((t) => `<div class="prow"><span class="badge ${t.side === "buy" ? "BUY" : "SELL"}">${t.side.toUpperCase()}</span>
        <b>${esc(t.symbol)}</b><span class="dim">${usd(t.usd, 2)} at ${price(t.price)}${t.pnl != null ? ` · <span class="${cls(t.pnl)}">${signedUsd(t.pnl)}</span>` : ""} · ${esc(t.why)}</span>
        <span class="dim num">${ago(t.at)}</span></div>`).join("")
      : `<p class="note">No trades yet.</p>`;
    renderStrategies();
  }
  const loadPaper = (refresh) => api("/api/paper" + (refresh ? "?refresh=1" : "")).then(renderPaper);
  $("#a-refresh").addEventListener("click", (e) => busy(e.target, "Checking…", () => loadPaper(true)));
  $("#a-reset").addEventListener("click", (e) => {
    const b = e.target; if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = "Reset? Click again"; setTimeout(() => { b.dataset.sure = ""; b.textContent = "Reset to $10,000"; }, 3000); return; }
    busy(b, "Resetting…", async () => { renderPaper(await api("/api/paper/reset", {})); toast("Paper account reset. All strategies are off."); });
  });
  $("#o-side").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.side = b.dataset.v; syncSeg("#o-side", S.side); } });
  $("#order").addEventListener("submit", (e) => {
    e.preventDefault();
    const sym = $("#o-sym").value.trim().toUpperCase(), amt = parseFloat($("#o-usd").value);
    busy(e.submitter || $("#order .btn"), "Placing…", async () => {
      renderPaper(await api("/api/paper/order", { symbol: sym, side: S.side, usd: amt > 0 ? amt : null }));
      toast(`Paper ${S.side} of ${sym} filled.`); $("#o-usd").value = "";
    });
  });

  // ---------- market today ----------
  async function loadMarket() {
    const [mon, intel] = await Promise.all([api("/api/markets/monitor?limit=40"), api("/api/markets/intel")]);
    S.rows = mon.rows;
    const move = intel.cap_weighted_24h ?? 0, b = intel.breadth;
    const word = move > 2 ? "rallying" : move > 0.3 ? "up" : move < -2 ? "selling off" : move < -0.3 ? "down" : "flat";
    $("#headline").innerHTML = `Crypto is ${word} today <span class="${cls(move)}">${signed(move)}</span> · ${b.up} of ${b.n} big coins up`;
    $("#market").innerHTML = S.rows.filter((r) => !r.stable).slice(0, 12).map((r) => `<button class="coin" data-s="${esc(r.symbol)}" title="${esc(r.name)} · ${price(r.last)}">${esc(r.symbol)}<small class="${cls(r.chg_24h)}">${signed(r.chg_24h, 1)}</small></button>`).join("");
    renderCoins(); setLive(true);
  }
  $("#market").addEventListener("click", (e) => { const b = e.target.closest(".coin"); if (b) openDrawer(b.dataset.s); });

  // ---------- coin drawer ----------
  const WIN = [["1D", "1d", "15m"], ["7D", "7d", "1h"], ["30D", "30d", "4h"], ["1Y", "365d", "1d"]];
  async function openDrawer(sym, win = "7D") {
    S.drawer = sym; $("#drawer").hidden = false;
    $("#d-win").innerHTML = WIN.map(([k]) => `<button data-w="${k}" aria-pressed="${k === win}">${k}</button>`).join("");
    const [, w, r] = WIN.find(([k]) => k === win);
    try {
      const [q, hist] = await Promise.all([api("/api/markets/quote?symbol=" + encodeURIComponent(sym)), api(`/api/markets/history?symbol=${encodeURIComponent(sym)}&window=${w}&resolution=${r}`)]);
      if (S.drawer !== sym) return;
      const vals = hist.map((p) => p.price), chg = vals.length > 1 ? (vals.at(-1) / vals[0] - 1) * 100 : null;
      $("#drawer-body").innerHTML = `<h3>${esc(q.asset?.name || sym)}</h3><div class="px">${price(q.last)}</div><div class="${cls(chg)}">${signed(chg)} over ${win}</div>
        <p class="dim">${q.asset?.website ? `<a href="${esc(q.asset.website)}" target="_blank" rel="noopener noreferrer">website</a>` : ""}</p>
        <button class="btn sm" id="d-buy">Paper buy $500</button>`;
      $("#d-buy").addEventListener("click", (e) => busy(e.target, "Buying…", async () => { renderPaper(await api("/api/paper/order", { symbol: sym, side: "buy", usd: 500 })); toast(`Paper bought $500 of ${sym}.`); }));
      $("#d-des").textContent = (q.asset?.description || "").slice(0, 500);
      lineChart($("#d-chart"), [{ values: vals, color: css("--ink"), width: 2, dot: true }]);
    } catch (e) { $("#drawer-body").innerHTML = `<h3>${esc(sym)}</h3><p class="down">${esc(e.message)}</p>`; }
  }
  $("#d-win").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) openDrawer(S.drawer, b.dataset.w); });
  $("#drawer-x").addEventListener("click", () => ($("#drawer").hidden = true));
  addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").hidden = true; });

  // ---------- boot ----------
  function setLive(ok, msg) {
    const l = $("#live"); l.className = "status " + (ok ? "ok" : "bad"); l.lastChild.textContent = ok ? "Live" : (msg || "Offline");
    if (ok) $("#updated").textContent = "Updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  $("#theme").addEventListener("click", () => {
    const light = document.documentElement.dataset.theme !== "light";
    if (light) document.documentElement.dataset.theme = "light"; else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("trade-theme", light ? "light" : "dark"); } catch (e) {}
    if (S.paper) { renderPaper.drawn = false; renderPaper(S.paper); } if (S.result) showResult(S.result, S.result.strategy);
  });
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (S.paper) { renderPaper.drawn = false; renderPaper(S.paper); } }, 150); });
  (async () => {
    loadPaper().catch((e) => toast(e.message));
    loadStrategies().catch(() => {});
    api("/api/strategies/templates").then((t) => { S.templates = t; renderStyles(); }).catch(() => {});
    loadShared();
    try {
      const st = await api("/api/markets/status");
      if (!st.configured) { setLive(false, "No key"); $("#headline").textContent = "Connect True Markets for live prices: jarvis markets setup <your-key.json>"; renderCoins(); return; }
    } catch (e) { setLive(false); $("#headline").textContent = "The JARVIS console server isn't reachable."; return; }
    loadMarket().catch((e) => { setLive(false); $("#headline").textContent = e.message; });
    setInterval(() => { if (!document.hidden) loadMarket().catch(() => setLive(false)); }, 60e3);
    setInterval(() => { if (!document.hidden) { loadPaper().catch(() => {}); loadStrategies().catch(() => {}); } }, 20e3);
  })();
})();
