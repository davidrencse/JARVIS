// JARVIS Trading: the simple face of the markets pipeline. Market data -> strategies ->
// signals -> you decide -> (real trading off). Everything comes from the local console
// server; nothing on this page can place an order.
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
  const usd = (v, d) => v == null ? "—" : "$" + v.toLocaleString("en-US", { minimumFractionDigits: d ?? (v >= 100 ? 0 : 2), maximumFractionDigits: d ?? (v >= 100 ? 0 : v >= 1 ? 2 : 6) });
  function price(v) {
    if (v == null) return "—";
    const a = Math.abs(v), d = a >= 1000 ? 0 : a >= 1 ? 2 : a >= 0.01 ? 4 : 8;
    return "$" + v.toLocaleString("en-US", { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
  }
  function big(v) {
    if (v == null) return "—";
    const a = Math.abs(v);
    return a >= 1e12 ? "$" + (v / 1e12).toFixed(2) + "T" : a >= 1e9 ? "$" + (v / 1e9).toFixed(1) + "B" : a >= 1e6 ? "$" + (v / 1e6).toFixed(0) + "M" : "$" + Math.round(v);
  }
  const signed = (v, d = 1) => v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(d) + "%";
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 3200); }
  const S = { rows: [], intel: null, pipe: null, templates: [], style: 0, coins: new Set(["BTC", "ETH", "SOL"]), risk: 10, period: "365d|1d", result: null, drawer: null };

  // ---------- canvas helpers ----------
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function prep(el) {
    const dpr = Math.min(devicePixelRatio || 1, 2), w = el.clientWidth, h = el.clientHeight;
    el.width = Math.round(w * dpr); el.height = Math.round(h * dpr);
    const g = el.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); return { g, w, h };
  }
  function lineChart(el, series, opts = {}) {
    const { g, w, h } = prep(el), pad = { l: 6, r: 6, t: 12, b: 14 };
    const all = series.flatMap((s) => s.values); if (!all.length) return;
    let lo = Math.min(...all), hi = Math.max(...all); if (lo === hi) { lo -= 1; hi += 1; }
    const X = (i, n) => pad.l + (i / Math.max(1, n - 1)) * (w - pad.l - pad.r), Y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
    if (opts.base != null && opts.base > lo && opts.base < hi) { g.strokeStyle = css("--line-2"); g.setLineDash([4, 6]); g.beginPath(); g.moveTo(pad.l, Y(opts.base)); g.lineTo(w - pad.r, Y(opts.base)); g.stroke(); g.setLineDash([]); }
    const t0 = performance.now(), dur = reduce || opts.instant ? 0 : 1100;
    function draw(now) {
      const k = dur ? Math.max(0, Math.min(1, (now - t0) / dur)) : 1, ease = 1 - Math.pow(1 - k, 3);
      g.clearRect(0, pad.t - 2, w, h); // keep it simple: redraw all series to the eased length
      if (opts.base != null && opts.base > lo && opts.base < hi) { g.strokeStyle = css("--line-2"); g.setLineDash([4, 6]); g.beginPath(); g.moveTo(pad.l, Y(opts.base)); g.lineTo(w - pad.r, Y(opts.base)); g.stroke(); g.setLineDash([]); }
      for (const s of series) {
        const n = s.values.length, upto = Math.max(2, Math.round(n * ease));
        g.beginPath(); s.values.slice(0, upto).forEach((v, i) => (i ? g.lineTo(X(i, n), Y(v)) : g.moveTo(X(i, n), Y(v))));
        g.strokeStyle = s.color; g.lineWidth = s.width || 2; g.lineJoin = "round"; g.setLineDash(s.dash || []); g.stroke(); g.setLineDash([]);
        if (s.fill) { g.lineTo(X(upto - 1, n), h - pad.b); g.lineTo(X(0, n), h - pad.b); g.closePath(); const gr = g.createLinearGradient(0, pad.t, 0, h); gr.addColorStop(0, s.fill); gr.addColorStop(1, "transparent"); g.fillStyle = gr; g.fill(); }
        if (s.dot && upto === n) { const x = X(n - 1, n), y = Y(s.values[n - 1]); g.fillStyle = s.color; g.beginPath(); g.arc(x, y, 4, 0, 7); g.fill(); g.strokeStyle = css("--line-2"); g.lineWidth = 6; g.beginPath(); g.arc(x, y, 7, 0, 7); g.stroke(); }
      }
      if (k < 1) requestAnimationFrame(draw);
    }
    requestAnimationFrame(draw);
    if (dur) setTimeout(() => draw(t0 + dur), dur + 150); // frames pause in hidden tabs: land the final one
  }

  // ---------- pulse: headline, gauge, heatmap ----------
  async function loadPulse() {
    const [mon, intel] = await Promise.all([api("/api/markets/monitor?limit=40"), api("/api/markets/intel")]);
    S.rows = mon.rows; S.intel = intel;
    const move = intel.cap_weighted_24h ?? 0, b = intel.breadth;
    const word = move > 2 ? "rallying" : move > 0.3 ? "up" : move < -2 ? "selling off" : move < -0.3 ? "down" : "flat";
    $("#headline").textContent = `Crypto is ${word} today`;
    $("#subline").textContent = `${b.up} of ${b.n} big coins are up. Bitcoin is ${intel.btc_dominance == null ? "—" : Math.round(intel.btc_dominance)}% of the value tracked.`;
    $("#k-move").textContent = signed(move); $("#k-move").className = cls(move);
    $("#k-move-f").innerHTML = `value-weighted · <span>${esc(word)}</span>`;
    $("#k-breadth").textContent = `${b.up}/${b.n}`;
    $("#k-breadth-f").textContent = `${b.n - b.up} down today`;
    $("#k-cap").textContent = big(intel.tracked_cap);
    // mini bars: the 14 biggest coins' moves, today's movers in full ink
    const movers = S.rows.filter((r) => !r.stable).slice(0, 14), mx = Math.max(1, ...movers.map((r) => Math.abs(r.chg_24h || 0)));
    $("#k-bars").innerHTML = movers.map((r) => `<i class="${Math.abs(r.chg_24h || 0) >= mx * .6 ? "hi" : ""}" style="height:${Math.max(8, Math.abs(r.chg_24h || 0) / mx * 100)}%" title="${esc(r.symbol)} ${signed(r.chg_24h)}"></i>`).join("");
    $("#k-breadth-bars").innerHTML = movers.map((r) => `<i class="${r.chg_24h > 0 ? "hi" : ""}" style="height:${r.chg_24h > 0 ? 100 : 35}%"></i>`).join("");
    // mood 0..100 from breadth (60%) and the cap-weighted move (40%)
    const mood = Math.max(0, Math.min(100, (b.n ? (b.up / b.n) * 60 : 30) + (Math.max(-5, Math.min(5, move)) + 5) * 4));
    $("#needle").style.left = `calc(${mood}% - 1px)`;
    $("#mood").textContent = mood < 20 ? "Fearful" : mood < 40 ? "Nervous" : mood < 60 ? "Neutral" : mood < 80 ? "Confident" : "Greedy";
    const top = intel.gainers[0], worst = intel.losers[0];
    $("#facts").innerHTML = `<dt>Best</dt><dd>${esc(top?.symbol)} <span class="${cls(top?.chg_24h)}">${signed(top?.chg_24h)}</span></dd>
      <dt>Worst</dt><dd>${esc(worst?.symbol)} <span class="${cls(worst?.chg_24h)}">${signed(worst?.chg_24h)}</span></dd>
      `;
    drawHeat(true);
    renderCoins();
    setLive(true);
  }

  // Squarified treemap (Bruls et al.), sized by market cap.
  function squarify(items, x, y, w, h) {
    const out = [], total = items.reduce((a, b) => a + b.v, 0); if (!total) return out;
    const scale = (w * h) / total; let rest = items.map((i) => ({ ...i, a: i.v * scale }));
    while (rest.length) {
      const short = Math.min(w, h); let row = [rest[0]], best = Infinity;
      const worst = (r) => { const s = r.reduce((a, b) => a + b.a, 0), mx = Math.max(...r.map((i) => i.a)), mn = Math.min(...r.map((i) => i.a)); return Math.max((short * short * mx) / (s * s), (s * s) / (short * short * mn)); };
      best = worst(row);
      for (let i = 1; i < rest.length; i++) { const next = worst([...row, rest[i]]); if (next > best) break; row.push(rest[i]); best = next; }
      const sum = row.reduce((a, b) => a + b.a, 0), thick = sum / short; let off = 0;
      for (const it of row) { const len = it.a / thick; out.push(w >= h ? { ...it, x, y: y + off, w: thick, h: len } : { ...it, x: x + off, y, w: len, h: thick }); off += len; }
      if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
      rest = rest.slice(row.length);
    }
    return out;
  }
  // Shade by size of the move, not its direction: the sign is in the coloured label.
  function heatColor(chg) {
    const k = Math.min(1, Math.abs(chg ?? 0) / 8) * .8 + .08, lo = css("--heat-lo").split(",").map(Number), hi = css("--heat-hi").split(",").map(Number);
    return `rgb(${lo.map((b, i) => Math.round(b + (hi[i] - b) * k)).join(",")})`;
  }
  let tiles = [];
  function drawHeat(animate) {
    const el = $("#heat"), { g, w, h } = prep(el);
    // Stablecoins don't move, so they only crowd out real coins. sqrt so small caps stay visible;
    // sorted biggest first (squarify needs it) and capped so every tile is big enough to label.
    const items = S.rows.filter((r) => r.market_cap && !r.stable).sort((a, b) => b.market_cap - a.market_cap)
      .slice(0, w < 600 ? 16 : 28).map((r) => ({ v: Math.sqrt(r.market_cap), r }));
    tiles = squarify(items, 0, 0, w, h);
    const t0 = performance.now(), dur = animate && !reduce ? 900 : 0;
    function draw(now) {
      g.clearRect(0, 0, w, h);
      tiles.forEach((t, i) => {
        const k = dur ? Math.max(0, Math.min(1, (now - t0 - i * 14) / (dur - 300))) : 1, e = 1 - Math.pow(1 - k, 3);
        const cw = t.w * e, ch = t.h * e, cx = t.x + (t.w - cw) / 2, cy = t.y + (t.h - ch) / 2;
        g.fillStyle = heatColor(t.r.chg_24h); g.globalAlpha = .25 + .75 * e;
        g.beginPath(); g.roundRect ? g.roundRect(cx + 1.5, cy + 1.5, Math.max(0, cw - 3), Math.max(0, ch - 3), 4) : g.rect(cx + 1.5, cy + 1.5, cw - 3, ch - 3); g.fill();
        g.globalAlpha = e;
        if (t.w > 34 && t.h > 22) {
          let fs = Math.min(26, Math.sqrt(t.w * t.h) / 5.5); g.font = `650 ${fs}px Geist, sans-serif`;
          while (fs > 8 && g.measureText(t.r.symbol).width > t.w - 14) g.font = `650 ${--fs}px Geist, sans-serif`;
          g.fillStyle = css("--ink"); g.fillText(t.r.symbol, t.x + 8, t.y + 6 + fs);
          if (t.h > fs * 2.4 && t.w > 44) { g.font = `500 ${Math.max(10.5, fs * .62)}px "Geist Mono", monospace`; g.fillStyle = css(t.r.chg_24h > 0 ? "--up" : t.r.chg_24h < 0 ? "--down" : "--ink-2"); g.fillText(signed(t.r.chg_24h), t.x + 8, t.y + 10 + fs * 1.85); }
        }
        g.globalAlpha = 1;
      });
      if (dur && now - t0 < dur + tiles.length * 14) requestAnimationFrame(draw);
    }
    requestAnimationFrame(draw);
    if (dur) setTimeout(() => draw(t0 + dur + tiles.length * 14 + 300), dur + tiles.length * 14 + 200);
  }
  const heat = $("#heat"), tip = $("#tip");
  function tileAt(e) { const r = heat.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top; return { t: tiles.find((t) => x >= t.x && x < t.x + t.w && y >= t.y && y < t.y + t.h), x, y }; }
  heat.addEventListener("mousemove", (e) => {
    const { t, x, y } = tileAt(e); if (!t) { tip.hidden = true; return; }
    tip.hidden = false; tip.innerHTML = `<b>${esc(t.r.name)}</b> · ${esc(t.r.symbol)}<br>${price(t.r.last)} · <span class="${cls(t.r.chg_24h)}">${signed(t.r.chg_24h)}</span> today<br><span style="color:var(--ink-3)">value ${big(t.r.market_cap)} · click for chart</span>`;
    tip.style.left = Math.min(x + 14, heat.clientWidth - tip.offsetWidth - 6) + "px"; tip.style.top = Math.min(y + 14, heat.clientHeight - tip.offsetHeight - 6) + "px";
  });
  heat.addEventListener("mouseleave", () => (tip.hidden = true));
  heat.addEventListener("click", (e) => { const { t } = tileAt(e); if (t) openDrawer(t.r.symbol); });

  // ---------- pipeline ----------
  async function loadPipeline(refresh) {
    const p = await api("/api/markets/pipeline" + (refresh ? "?refresh=1" : ""));
    S.pipe = p;
    const f = p.feed, s = p.signals;
    $("#n0").textContent = f.ok ? `${f.priced} coins` : "offline";
    $("#n1").textContent = s.ok ? `${s.strategies} running` : "—";
    $("#n2").textContent = s.ok ? `${s.counts.BUY + s.counts.SELL} new` : "—";
    document.querySelector('.node[data-n="2"] small').textContent = s.ok ? `${s.counts.HOLD} positions open · ${s.counts.FLAT} waiting` : "buy · sell · hold";
    $("#n3").textContent = s.ok ? (s.actionable.length ? `${s.actionable.length} to review` : "all clear") : "—";
    document.querySelector('.node[data-n="0"]').classList.toggle("live", !!f.ok);
    document.querySelector('.node[data-n="1"]').classList.toggle("live", !!s.ok);
    document.querySelector('.node[data-n="2"]').classList.toggle("live", !!s.ok);
    document.querySelector(".node.gate").classList.toggle("waiting", !!s.actionable?.length);
    $("#funnel").textContent = s.ok ? `${f.assets} coins → ${f.priced} priced → ${s.strategies} strategies → ${s.checks} checks → ${s.actionable.length} signals → 0 trades` : (f.error || s.error || "");
    // one row per coin: what needs a decision first, then what's being held and by whom
    const coins = {};
    for (const x of s.ok ? [...s.actionable, ...s.holding] : []) {
      const c = (coins[x.symbol] ||= { symbol: x.symbol, price: x.price, act: [], hold: [] });
      (x.signal === "HOLD" ? c.hold : c.act).push(x);
    }
    const list = Object.values(coins).sort((a, b) => b.act.length - a.act.length);
    const names = (xs) => xs.map((x) => `<b>${esc(friendly(x.strategy).title)}</b>${x.position?.unrealized ? ` <span class="${cls(x.position.unrealized)}">${signed(x.position.unrealized)}</span>` : ""}`).join(", ");
    $("#review").innerHTML = list.length ? list.map((c) => `<div class="row">${c.act.map((x) => `<span class="badge ${x.signal}">${x.signal}</span>`).join("") || `<span class="badge HOLD">HOLD</span>`}<b>${esc(c.symbol)}</b>
        <span>${c.act.length ? `${c.act[0].signal === "BUY" ? "Buy" : "Sell"} signal from ${names(c.act)}` : `Held by ${names(c.hold)}`}</span>
        <span class="note" style="margin-left:auto">${price(c.price)}</span></div>`).join("")
      + `<p class="note">Signals are suggestions for you to look at. Practice mode: JARVIS won't trade on them.</p>`
      : `<p class="note">${s.ok ? "No strategy is asking for action right now. JARVIS rechecks every minute." : esc(f.error || s.error || "Loading…")}</p>`;
    renderStrategies();
    if (f.stale) toast("Showing the last good data; True Markets didn't answer just now.");
  }
  // ---------- strategy cards ----------
  function renderStrategies() {
    const s = S.pipe?.signals; if (!s?.ok) return;
    const bySym = {}; for (const x of [...s.actionable, ...s.holding]) (bySym[x.id] ||= []).push(x);
    const rows = s.leaderboard.map((b) => {
      const max = Math.max(Math.abs(b.total_return), Math.abs(b.buy_hold), 1), w = (v) => `width:${Math.abs(v) / max * 100}%`;
      const chips = (bySym[b.id] || []).map((x) => `<span class="badge ${x.signal}">${esc(x.symbol)} ${x.signal}</span>`).join("") || `<span class="badge FLAT">waiting</span>`;
      return `<tr><td><b>${esc(friendly(b.name).title)}</b></td>
        <td class="num ret ${cls(b.total_return)}">${signed(b.total_return)}</td>
        <td class="num">${usd(1000 * (1 + b.total_return / 100))}</td>
        <td class="num muted">${usd(1000 * (1 + b.buy_hold / 100))}</td>
        <td><div class="vs" title="strategy vs holding"><span><i style="${w(b.total_return)}"></i></span><span class="h"><i style="${w(b.buy_hold)}"></i></span></div></td>
        <td class="num">${b.max_drawdown?.toFixed(0)}%</td><td class="num">${b.trades}</td>
        <td><div class="chips">${chips}</div></td></tr>`;
    }).join("");
    $("#strats").innerHTML = (s.templates ? `<p class="note">Starter ideas on BTC, ETH and SOL, tested over the last year. Save your own below and they'll replace these.</p>` : "") +
      `<table class="st"><thead><tr><th>Strategy</th><th class="num">Return</th><th class="num">$1,000 →</th><th class="num">Holding</th><th>vs holding</th><th class="num">Worst dip</th><th class="num">Trades</th><th>Now</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  // ---------- builder ----------
  const FRIENDLY = {
    "Golden cross": { title: "Ride big trends", desc: "Buy when the long trend turns up, sell when it turns down.", glyph: "M2 28 C14 26 18 30 26 20 S40 6 58 4", },
    "EMA momentum": { title: "Follow momentum", desc: "Jump in when prices climb fast; step out when they cool.", glyph: "M2 30 L14 26 L22 28 L34 16 L44 18 L58 4" },
    "RSI reversion": { title: "Buy the dip", desc: "Buy after a sharp drop, sell on the bounce.", glyph: "M2 8 C14 10 18 30 28 30 S44 10 58 8" },
    "Z-score snapback": { title: "Quick snapbacks", desc: "Catch unusually big short drops that snap back.", glyph: "M2 16 L18 16 L24 30 L30 14 L58 16" },
    "Donchian breakout": { title: "Catch breakouts", desc: "Buy when price breaks to a new high.", glyph: "M2 22 L12 18 L22 22 L32 18 L40 20 L46 8 L58 4" },
  };
  const friendly = (name) => FRIENDLY[name] || { title: name, desc: "", glyph: "M2 20 L58 12" };
  function renderStyles() {
    $("#styles").innerHTML = S.templates.map((t, i) => { const f = friendly(t.name);
      return `<button class="style" role="radio" data-i="${i}" aria-checked="${i === S.style}"><svg viewBox="0 0 60 34" preserveAspectRatio="none" aria-hidden="true"><path d="${f.glyph}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><b>${esc(f.title)}</b><small>${esc(f.desc)}</small></button>`; }).join("");
  }
  $("#styles").addEventListener("click", (e) => { const b = e.target.closest(".style"); if (!b) return; S.style = +b.dataset.i; renderStyles();
    const t = S.templates[S.style]; S.period = t.window === "365d" || t.window === "180d" ? "365d|1d" : t.window === "30d" ? "30d|1h" : "90d|4h"; syncSeg("#period", S.period); });
  function renderCoins() {
    const list = S.rows.filter((r) => !r.stable).slice(0, 14);
    $("#coins").innerHTML = list.map((r) => `<button class="coin" data-s="${esc(r.symbol)}" aria-pressed="${S.coins.has(r.symbol)}">${esc(r.symbol)}<small class="${cls(r.chg_24h)}">${signed(r.chg_24h, 1)}</small></button>`).join("");
  }
  $("#coins").addEventListener("click", (e) => { const b = e.target.closest(".coin"); if (!b) return; const s = b.dataset.s;
    if (S.coins.has(s)) { if (S.coins.size > 1) S.coins.delete(s); } else if (S.coins.size < 8) S.coins.add(s); else toast("Up to 8 coins per strategy.");
    renderCoins(); });
  function syncSeg(sel, v) { for (const b of document.querySelectorAll(sel + " button")) b.setAttribute("aria-checked", String(b.dataset.v === String(v))); }
  $("#risk").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.risk = +b.dataset.v; syncSeg("#risk", S.risk); } });
  $("#period").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.period = b.dataset.v; syncSeg("#period", S.period); } });
  $("#to-build").addEventListener("click", () => $("#build").scrollIntoView({ behavior: reduce ? "auto" : "smooth" }));

  function spec() {
    const t = S.templates[S.style], [window_, res] = S.period.split("|");
    return { name: `${friendly(t.name).title} · ${[...S.coins].join(" ")}`, symbols: [...S.coins], window: window_, resolution: res,
      entry: t.entry, exit: t.exit, stop_loss: S.risk, take_profit: t.take_profit || 0, fee_bps: 10, notes: `From the “${t.name}” template.` };
  }
  $("#test").addEventListener("click", async () => {
    const btn = $("#test"), sp = spec(); btn.disabled = true; btn.textContent = "Testing on real history…";
    try {
      const r = await api("/api/strategies/backtest", { spec: sp }); S.result = r; showResult(r, sp);
    } catch (e) { toast(e.message); }
    btn.disabled = false; btn.textContent = "Test it";
  });
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
    const label = { "30d|1h": "the last month", "90d|4h": "the last 3 months", "365d|1d": "the last year" }[S.period];
    $("#res-title").textContent = `If you'd run this over ${label}`;
    countUp($("#res-to"), end); $("#res-to").className = cls(s.total_return);
    $("#res-vs").innerHTML = `Just holding the same coins: <b class="${cls(s.buy_hold)}">${usd(hold)}</b>. ${end >= hold ? "The strategy did better." : "Holding did better this time."} Worst dip along the way: ${s.max_drawdown.toFixed(0)}%.`;
    $("#res-rows").innerHTML = ok.map((x) => `<div><b>${esc(x.symbol)}</b><span class="${cls(x.metrics.total_return)}">${signed(x.metrics.total_return)}</span><span style="color:var(--ink-3)">${x.metrics.trades} trades</span><span class="badge ${x.signal}">${x.signal === "HOLD" ? "IN NOW" : x.signal}</span></div>`).join("");
    $("#res-name").value = sp.name;
    $("#res-rules").textContent = `Buy when: ${sp.entry.join(" AND ")}\nSell when: ${sp.exit.join(" OR ") || "—"} (or down ${sp.stop_loss}%)\nCoins: ${sp.symbols.join(", ")} · bars: ${sp.resolution} · period: ${sp.window}`;
    // equal-weight portfolio curve vs holding
    const n = Math.min(...ok.map((x) => x.equity.length));
    const avg = (key) => Array.from({ length: n }, (_, i) => ok.reduce((a, x) => a + x[key][i], 0) / ok.length * 1000);
    lineChart($("#res-chart"), [{ values: avg("hold"), color: css("--ink-3"), width: 1.5, dash: [5, 5] }, { values: avg("equity"), color: css("--ink"), width: 2, dot: true }], { base: 1000 });
    $("#result").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "nearest" });
  }
  $("#res-save").addEventListener("click", async () => {
    try { const s = await api("/api/strategies", { ...spec(), name: $("#res-name").value.trim() || spec().name }); toast(`Saved “${s.name}”. JARVIS will watch it.`); loadPipeline(true); }
    catch (e) { toast(e.message); }
  });

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
        <p style="color:var(--ink-3);font-size:13.5px">Value ${big(q.market_cap)} · ${esc(q.asset?.chain || "")}${q.asset?.website ? ` · <a href="${esc(q.asset.website)}" target="_blank" rel="noopener noreferrer">website</a>` : ""}</p>`;
      $("#d-des").textContent = (q.asset?.description || "").slice(0, 600);
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
    drawHeat(false); if (S.result) showResult(S.result, spec()); if (S.drawer && !$("#drawer").hidden) openDrawer(S.drawer);
  });
  $("#menu").addEventListener("click", () => document.body.classList.toggle("nav-open"));
  $(".side").addEventListener("click", (e) => { if (e.target.closest("a")) document.body.classList.remove("nav-open"); });
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { drawHeat(false); if (S.result) showResult(S.result, spec()); }, 150); });
  (async () => {
    try {
      const st = await api("/api/markets/status");
      if (!st.configured) { setLive(false, "No key"); $("#headline").textContent = "Connect True Markets to start"; $("#subline").textContent = "Run: jarvis markets setup <your-key.json>, then reload."; return; }
    } catch (e) { setLive(false); $("#headline").textContent = "The JARVIS console server isn't reachable."; return; }
    api("/api/strategies/templates").then((t) => { S.templates = t; renderStyles(); if (location.hash === "#demo") setTimeout(() => $("#test").click(), 1500); }).catch(() => {});
    loadPulse().catch((e) => { setLive(false); $("#headline").textContent = e.message; });
    loadPipeline().catch((e) => ($("#funnel").textContent = e.message));
    setInterval(() => { if (!document.hidden) loadPulse().catch(() => setLive(false)); }, 60e3);
    setInterval(() => { if (!document.hidden) loadPipeline().catch(() => {}); }, 65e3);
  })();
})();
