(function () {
  "use strict";

  // ---------- helpers ----------
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (name, cls = "i i-sm") => `<svg class="${cls}" aria-hidden="true"><use href="icons.svg?v=2#i-${name}"/></svg>`;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };
  async function api(path, body, raw) {
    const opts = body === undefined ? {} : {
      method: "POST", headers: { "X-JARVIS": "1", "Content-Type": raw ? "application/octet-stream" : "application/json" },
      body: raw ? body : JSON.stringify(body) };
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  const nf = new Intl.NumberFormat();
  function compact(n) {
    if (n == null || isNaN(n)) return "—";
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e4) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "K";
    return nf.format(Math.round(n * 100) / 100);
  }
  function money(v, unit) {
    if (unit === "USD") return "$" + Number(v).toFixed(2);
    if (unit === "CNY") return "¥" + Number(v).toFixed(2);
    if (unit === "%") return Math.round(v) + "%";
    return compact(v) + (unit ? " " + unit : "");
  }
  const stamp = (iso) => typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  function ago(iso) {
    if (!iso) return "—";
    const s = Math.round((Date.now() - stamp(iso)) / 1000);
    if (s < -60) return "in " + until(stamp(iso));
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }
  function until(t) {
    const s = Math.max(0, Math.round((t - Date.now()) / 1000));
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min";
    if (s < 86400) return Math.floor(s / 3600) + " h " + Math.round((s % 3600) / 60) + " min";
    return Math.floor(s / 86400) + " d " + Math.round((s % 86400) / 3600) + " h";
  }
  const hhmm = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const dayTime = (t) => { const d = new Date(t), today = new Date().toDateString() === d.toDateString();
    return today ? hhmm(d) : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + hhmm(d); };
  let toastTimer;
  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 4800);
  }
  // One quiet live region, set only when something changes state.
  function announce(msg) { const a = $("#announce"); a.textContent = ""; setTimeout(() => (a.textContent = msg), 30); }
  const words = (s) => String(s || "").replace(/_/g, " ");
  const TIER = { simple: "Quick", medium: "Tools", complex: "Plan" };
  const tierName = (t) => TIER[t] || t || "";
  function plainError(msg) {
    msg = String(msg || "");
    if (/run lanes? in this ledger are busy/i.test(msg)) return msg.replace(/^.*?All/, "All") + " Raise orchestration.lanes in jarvis.config.json to run more at once.";
    if (/not installed|not on PATH/i.test(msg)) return msg + " Install it or switch the provider in jarvis.config.json.";
    if (/Failed to fetch|NetworkError/i.test(msg)) return "Lost contact with the console server. Is `python -m jarvis ui` still running?";
    return msg.replace(/^(RuntimeError|ValueError|TypeError|OSError): /, "");
  }
  const typing = () => /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;

  // ---------- state ----------
  // dashboard.js draws gauges, charts and the task board from these events.
  const emit = (name, detail) => dispatchEvent(new CustomEvent("jarvis:" + name, { detail }));
  const QUERY = new URLSearchParams(location.search);
  const OVERLAY = QUERY.has("overlay"), OVERLAY_KEY = QUERY.get("key") || "Alt+1";
  const state = { overview: null, credits: null, tier: "", speak: store.get("jarvis.speak", false), cases: [],
                  case: store.get("jarvis.case", "") };

  // ---------- views ----------
  // Order is the rail's own order, so the number keys read top to bottom.
  const VIEWS = ["console", "graph", "system", "credits", "memory", "tasks", "run", "files", "devices"];
  const loaded = {};
  async function show() {
    const name = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "console";
    for (const v of VIEWS) $("#view-" + v).hidden = v !== name;
    $$(".rail a[data-view]").forEach((a) => { if (a.dataset.view === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    $("#crumb-view").textContent = $("#view-" + name).dataset.title;
    // The overlay controller finds its window by this exact title, so it never changes there.
    document.title = OVERLAY ? "JARVIS Overlay" : `JARVIS · ${$("#view-" + name).dataset.title}`;
    emit("view", name);
    const load = { graph: loadGraph, credits: () => renderCreditTable(state.credits),
                   memory: loadMemory, tasks: loadTasks, devices: renderDevices, files: () => window.JarvisFiles?.open(),
                   run: () => window.JarvisRun?.open(), system: (a) => window.JarvisSystem?.open(a) }[name];
    if (load) { try { if ((await load(loaded[name])) !== false) loaded[name] = true; } catch (e) { toast(plainError(e.message)); } }
  }
  addEventListener("hashchange", show);

  // ---------- theme: system → light → dark ----------
  const THEME_LABEL = { system: "Theme follows the system", light: "Light theme", dark: "Dark theme" };
  function syncTheme() { const b = $("#theme-toggle"); b.title = THEME_LABEL[JarvisTheme.pref] + " (click to change)"; b.setAttribute("aria-label", b.title); }
  $("#theme-toggle").addEventListener("click", () => {
    const order = ["system", "light", "dark"];
    JarvisTheme.pref = order[(order.indexOf(JarvisTheme.pref) + 1) % 3];
    try { localStorage.setItem("jarvis.theme", JarvisTheme.pref); } catch (e) {}
    JarvisTheme.apply(); syncTheme(); Terrain.redraw(); graphs.forEach((g) => g.draw());
    toast(THEME_LABEL[JarvisTheme.pref] + ".");
  });
  syncTheme();

  // ---------- clock ----------
  function isoWeek(d) {
    const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    const day = t.getUTCDay() || 7; t.setUTCDate(t.getUTCDate() + 4 - day);
    return Math.ceil(((t - Date.UTC(t.getUTCFullYear(), 0, 1)) / 86400000 + 1) / 7);
  }
  function tick() {
    const d = new Date();
    $("#top-clock").textContent = d.toLocaleTimeString([], { hour12: false });
    $("#clock-time").textContent = hhmm(d);
    $("#clock-sec").textContent = String(d.getSeconds()).padStart(2, "0");
    $("#clock-date").textContent = d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" });
    $("#clock-tz").textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const start = new Date(d.getFullYear(), 0, 0), doy = Math.floor((d - start) / 86400000);
    const days = Math.round((new Date(d.getFullYear(), 11, 31) - start) / 86400000);
    $("#clock-facts").innerHTML = [
      ["UTC", d.toISOString().slice(11, 16)], ["Week", "W" + String(isoWeek(d)).padStart(2, "0")],
      ["Day", `${doy} / ${days}`], ["Year", Math.round((doy / days) * 100) + "% done"],
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  }
  tick(); setInterval(tick, 1000);

  // ---------- weather (Open-Meteo, no key) ----------
  const WX = {
    0: ["Clear", "sun"], 1: ["Mostly clear", "sun"], 2: ["Partly cloudy", "cloud-sun"], 3: ["Overcast", "cloud"],
    45: ["Fog", "cloud-fog"], 48: ["Freezing fog", "cloud-fog"], 51: ["Light drizzle", "cloud-drizzle"], 53: ["Drizzle", "cloud-drizzle"],
    55: ["Heavy drizzle", "cloud-drizzle"], 56: ["Freezing drizzle", "cloud-drizzle"], 57: ["Freezing drizzle", "cloud-drizzle"],
    61: ["Light rain", "cloud-rain"], 63: ["Rain", "cloud-rain"], 65: ["Heavy rain", "cloud-rain"], 66: ["Freezing rain", "cloud-rain"],
    67: ["Freezing rain", "cloud-rain"], 71: ["Light snow", "cloud-snow"], 73: ["Snow", "cloud-snow"], 75: ["Heavy snow", "cloud-snow"],
    77: ["Snow grains", "cloud-snow"], 80: ["Showers", "cloud-rain"], 81: ["Showers", "cloud-rain"], 82: ["Heavy showers", "cloud-rain"],
    85: ["Snow showers", "cloud-snow"], 86: ["Snow showers", "cloud-snow"], 95: ["Thunderstorm", "cloud-lightning"],
    96: ["Thunderstorm, hail", "cloud-lightning"], 99: ["Thunderstorm, hail", "cloud-lightning"],
  };
  const wx = (code, day = 1) => { const [label, ic] = WX[code] || ["—", "cloud"]; return [label, !day && ic === "sun" ? "moon" : ic]; };
  let place = store.get("jarvis.place", null);
  let units = store.get("jarvis.units", "metric");
  async function loadWeather() {
    const body = $("#weather-body");
    if (!place) {
      $("#weather-place-name").textContent = "Set location";
      body.innerHTML = `<div class="empty" style="grid-column:1/-1"><b>Where are you?</b>Search a city or use this laptop’s location. Forecasts come from Open-Meteo; nothing else is sent.</div>`;
      $("#place-form").hidden = false; return;
    }
    $("#weather-place-name").textContent = place.name;
    const imperial = units === "imperial";
    const q = new URLSearchParams({ latitude: place.lat, longitude: place.lon, timezone: "auto", forecast_days: "6",
      current: "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day,precipitation",
      hourly: "temperature_2m,precipitation_probability",
      daily: "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max",
      temperature_unit: imperial ? "fahrenheit" : "celsius", wind_speed_unit: imperial ? "mph" : "kmh" });
    try {
      const res = await fetch("https://api.open-meteo.com/v1/forecast?" + q);
      if (!res.ok) throw new Error("HTTP " + res.status);
      renderWeather(await res.json(), imperial);
    } catch (e) {
      body.innerHTML = `<div class="empty" style="grid-column:1/-1"><b>Weather is unreachable.</b>${esc(e.message)}. It retries every 15 minutes.</div>`;
    }
  }
  function renderWeather(d, imperial) {
    if (window.JarvisDash) return JarvisDash.weather(d, imperial, wx, () => { units = units === "metric" ? "imperial" : "metric"; store.set("jarvis.units", units); loadWeather(); });
    const c = d.current, [label, ic] = wx(c.weather_code, c.is_day);
    const deg = imperial ? "°F" : "°C", wind = imperial ? "mph" : "km/h";
    const now = Date.parse(c.time), hours = [];
    for (let i = 0; i < d.hourly.time.length && hours.length < 24; i++) {
      if (Date.parse(d.hourly.time[i]) >= now - 3600e3) hours.push([d.hourly.time[i], d.hourly.temperature_2m[i], d.hourly.precipitation_probability[i] ?? 0]);
    }
    const temps = hours.map((h) => h[1]), lo = Math.min(...temps), hi = Math.max(...temps);
    const W = 300, H = 70, x = (i) => (i / (hours.length - 1)) * W, y = (t) => 8 + (1 - (t - lo) / ((hi - lo) || 1)) * (H - 26);
    const line = hours.map((h, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(h[1]).toFixed(1)}`).join("");
    const bars = hours.map((h, i) => h[2] > 0 ? `<rect x="${(x(i) - 3).toFixed(1)}" y="${(H - 12 - h[2] * 0.14).toFixed(1)}" width="6" height="${(h[2] * 0.14).toFixed(1)}" fill="var(--fill-2)"/>` : "").join("");
    const ticks = hours.map((h, i) => i % 6 === 0 ? `<text x="${x(i).toFixed(1)}" y="${H + 8}" text-anchor="${i ? "middle" : "start"}">${h[0].slice(11, 16)}</text><text x="${x(i).toFixed(1)}" y="${(y(h[1]) - 6).toFixed(1)}" text-anchor="${i ? "middle" : "start"}" style="fill:var(--ink)">${Math.round(h[1])}°</text>` : "").join("");
    const days = d.daily.time.slice(1, 6).map((t, i) => {
      const [dl, di] = wx(d.daily.weather_code[i + 1]);
      return `<div class="wx-day" title="${esc(dl)}"><b>${new Date(t + "T12:00").toLocaleDateString([], { weekday: "short" })}</b>${icon(di, "i")}<span class="mono">${Math.round(d.daily.temperature_2m_max[i + 1])}° <span>${Math.round(d.daily.temperature_2m_min[i + 1])}°</span></span></div>`;
    }).join("");
    $("#weather-body").innerHTML = `
      <div class="wx-now">
        <button class="wx-temp" type="button" id="wx-units" title="Switch to ${imperial ? "°C" : "°F"}" aria-label="${Math.round(c.temperature_2m)}${deg}. Switch to ${imperial ? "Celsius" : "Fahrenheit"}">${icon(ic, "i")}<span class="wx-deg">${Math.round(c.temperature_2m)}°</span></button>
        <div class="wx-cond">${esc(label)}</div>
        <div class="wx-sub">Feels ${Math.round(c.apparent_temperature)}${deg} · H ${Math.round(d.daily.temperature_2m_max[0])}° L ${Math.round(d.daily.temperature_2m_min[0])}°</div>
      </div>
      <div>
        <svg class="wx-chart" viewBox="0 -4 ${W} ${H + 14}" preserveAspectRatio="none" role="img" aria-label="Temperature from ${Math.round(lo)} to ${Math.round(hi)} degrees over the next 24 hours">
          ${bars}<path d="${line}" fill="none" stroke="var(--ink)" stroke-width="1.3" vector-effect="non-scaling-stroke"/>${ticks}
        </svg>
        <dl class="facts mono" style="margin-top:8px">
          <dt>Wind</dt><dd>${Math.round(c.wind_speed_10m)} ${wind}</dd>
          <dt>Humidity</dt><dd>${c.relative_humidity_2m}%</dd>
          <dt>Rain chance</dt><dd>${d.daily.precipitation_probability_max[0] ?? 0}%</dd>
          <dt>Sun</dt><dd>${d.daily.sunrise[0].slice(11, 16)} – ${d.daily.sunset[0].slice(11, 16)}</dd>
        </dl>
      </div>
      <div class="wx-days">${days}</div>`;
    $("#wx-units").addEventListener("click", () => { units = units === "metric" ? "imperial" : "metric"; store.set("jarvis.units", units); loadWeather(); });
  }
  $("#weather-place").addEventListener("click", () => { const f = $("#place-form"); f.hidden = !f.hidden; if (!f.hidden) $("#place-input").focus(); });
  let placeTimer;
  $("#place-input").addEventListener("input", (ev) => {
    clearTimeout(placeTimer);
    const q = ev.target.value.trim();
    if (q.length < 2) { $("#place-results").innerHTML = ""; return; }
    placeTimer = setTimeout(async () => {
      try {
        const res = await fetch("https://geocoding-api.open-meteo.com/v1/search?" + new URLSearchParams({ name: q, count: "5" }));
        const data = await res.json();
        $("#place-results").innerHTML = (data.results || []).map((r, i) =>
          `<li><button type="button" data-i="${i}">${esc(r.name)}<small>${esc([r.admin1, r.country].filter(Boolean).join(", "))}</small></button></li>`).join("") || `<li class="muted" style="padding:6px 8px;font-size:12.5px">No match.</li>`;
        $$("#place-results button").forEach((b) => b.addEventListener("click", () => {
          const r = data.results[b.dataset.i]; setPlace({ name: r.name, lat: r.latitude, lon: r.longitude });
        }));
      } catch (e) { $("#place-results").innerHTML = `<li class="muted">Search failed: ${esc(e.message)}</li>`; }
    }, 280);
  });
  $("#place-form").addEventListener("submit", (e) => { e.preventDefault(); $("#place-results button")?.click(); });
  $("#place-geo").addEventListener("click", () => {
    if (!navigator.geolocation) return toast("Location isn't available in this browser.");
    navigator.geolocation.getCurrentPosition(
      (p) => setPlace({ name: "Here", lat: +p.coords.latitude.toFixed(3), lon: +p.coords.longitude.toFixed(3) }),
      (e) => toast("Couldn't get location: " + e.message + ". Search a city instead."), { timeout: 10000 });
  });
  function setPlace(p) { place = p; store.set("jarvis.place", p); $("#place-form").hidden = true; $("#place-input").value = ""; $("#place-results").innerHTML = ""; loadWeather(); }
  loadWeather(); setInterval(loadWeather, 15 * 60 * 1000);

  // ---------- credits ----------
  const SRC_TEXT = { live: "Live from the provider", local: "Read from logs on this laptop", manual: "Entered by you", none: "Not set up", error: "Check failed" };
  const SRC_SHORT = { live: "live", local: "local logs", manual: "entered", none: "not set up", error: "failed" };
  const srcKind = (r) => (r.status === "error" ? "error" : r.source);
  const srcMark = (r) => { const k = srcKind(r); return `<span class="src src-${k}" title="${SRC_TEXT[k]}" role="img" aria-label="${SRC_TEXT[k]}"></span>`; };
  const legend = (kinds) => kinds.map((k) => `<span><span class="src src-${k}" aria-hidden="true"></span>${SRC_SHORT[k]}</span>`).join("");
  // Every meter reads as what's LEFT, with the reset time inline.
  function meters(r, max = 2) {
    return r.meters.slice(0, max).map((m) => {
      const pct = m.pct ?? 0, left = Math.max(0, 100 - pct);
      const num = m.unit === "%" ? `${Math.round(left)}% left` : `${compact(Math.max(0, m.limit - m.used))} of ${compact(m.limit)} left`;
      const when = m.resets_at ? `resets ${dayTime(Date.parse(m.resets_at))} · in ${until(Date.parse(m.resets_at))}` : "";
      return `<div class="meter"><span class="meter-label">${esc(m.label)}${when ? `<small>${esc(when)}</small>` : ""}</span>
        <span class="bar ${r.source === "local" ? "local" : ""}" role="img" aria-label="${esc(num)}"><i style="width:${left.toFixed(1)}%"></i></span>
        <span class="meter-num">${num}</span></div>`;
    }).join("");
  }
  function headline(r) {
    if (!r.headline) return `<span class="cr-val" style="color:var(--ink-3)">—<small>${r.status === "error" ? "check failed" : "not set up"}</small></span>`;
    return `<span class="cr-val">${money(r.headline.value, r.headline.unit)}<small>${esc(r.headline.label)}</small></span>`;
  }
  function renderCreditRows(list) {
    const ready = list.filter((r) => r.status !== "unconfigured"), idle = list.filter((r) => r.status === "unconfigured");
    $("#credit-rows").innerHTML = ready.map((r) => `<div class="cr">${srcMark(r)}
        <span class="cr-name">${esc(r.name)}<small>${esc(r.blurb)}</small></span>
        ${r.meters.length ? `<div class="meters">${meters(r)}</div>` : `<span class="cr-note">${esc(r.note)}</span>`}
        ${headline(r)}
        ${r.meters.length && r.status === "error" ? `<span class="cr-note cr-note-full">${esc(r.note)}</span>` : ""}</div>`).join("") +
      (idle.length ? `<div class="cr-more"><span class="src src-none" aria-hidden="true"></span>${idle.length} not set up: ${esc(idle.map((r) => r.name).join(", "))}<a href="#credits">Set up${icon("chevron-right")}</a></div>` : "");
    $("#credits-mini-legend").innerHTML = legend(["live", "local", "manual", "error"]);
    $("#credits-checked").textContent = "checked " + ago(list[0]?.checked_at);
  }
  function setupBox(r) {
    const s = r.setup || {}, dash = s.dashboard ? `<a href="${esc(s.dashboard)}" target="_blank" rel="noopener noreferrer">${esc(new URL(s.dashboard).host)}${icon("external-link")}</a>` : null;
    if (s.how === "key") return `<div class="setup"><ol>
        <li>Create or copy a key${dash ? ` at ${dash}` : ""}.</li>
        <li>Add it to <code>${esc(s.secrets_file)}</code>:<pre>"${esc(s.keys[0])}": "your-key"</pre>
          JARVIS reads that file on every check, so no restart. A <code>${esc(s.keys[0])}</code> environment variable also works, but the console must be restarted to see it.</li>
        <li>Press <b>Check now</b>.</li></ol></div>`;
    if (s.how === "local") return `<div class="setup">Quota is read using your existing ${r.id === "codex" ? "Codex" : "Claude Code"} sign-in. ${r.id === "claude" ? "JARVIS attempts to renew expired Claude sessions automatically. If renewal fails, run <code>claude auth login</code>, then check again." : "Open Codex to renew an expired sign-in, then check again."} Local logs are a fallback when live quota is unavailable.${dash ? ` Account usage: ${dash}.` : ""}</div>`;
    return `<div class="setup">${esc(r.note)} ${dash ? `Dashboard: ${dash}.` : ""} Use <b>Enter balance</b> to record what it shows; the entry is dated so you can tell when it’s stale.</div>`;
  }
  function renderCreditTable(list) {
    if (!list) return;
    if ($("#credit-table .ct-edit") || $("#credit-table").contains(document.activeElement)) return; // never wipe a half-typed entry
    $("#credits-legend").innerHTML = `<span class="legend">${legend(["live", "local", "manual", "none", "error"])}</span>`;
    const groups = {};
    list.forEach((r) => (groups[r.group] = groups[r.group] || []).push(r));
    $("#credit-table").innerHTML = Object.entries(groups).map(([g, rows]) => `<section class="ct-group"><h3>${esc(g)}</h3>${rows.map((r) => `
      <div class="ct-row" data-id="${r.id}">${srcMark(r)}
        <div><div class="cr-name">${esc(r.name)}<small>${esc(r.blurb)}</small></div></div>
        <div class="meters">${headline(r)}${meters(r, 4)}</div>
        <div class="ct-facts mono">${r.facts.map(([k, v]) => `<div><span>${esc(k)}</span><span>${/^\d{4}-\d\d-\d\dT/.test(v || "") ? ago(v) : esc(v ?? "—")}</span></div>`).join("")}</div>
        <span></span>
        ${r.note ? `<p class="ct-note">${esc(r.note)}</p>` : ""}
        <div class="ct-actions" style="grid-column:2/-1">
          ${r.status !== "ok" || r.source === "local" ? `<button class="btn ghost sm" type="button" data-setup="${r.id}" aria-expanded="false">${r.source === "local" ? "Where this comes from" : "How to set up"}</button>` : ""}
          ${r.setup?.how !== "local" ? `<button class="btn ghost sm" type="button" data-edit="${r.id}">${r.source === "manual" ? "Update balance" : "Enter balance"}</button>` : ""}
        </div>
      </div>`).join("")}</section>`).join("");
    $$("[data-edit]").forEach((b) => b.addEventListener("click", () => editCredit(b.closest(".ct-row"), b.dataset.edit)));
    $$("[data-setup]").forEach((b) => b.addEventListener("click", () => {
      const row = b.closest(".ct-row"), open = row.querySelector(".setup");
      if (open) { open.remove(); b.setAttribute("aria-expanded", "false"); return; }
      row.insertAdjacentHTML("beforeend", setupBox(list.find((r) => r.id === b.dataset.setup)));
      b.setAttribute("aria-expanded", "true");
    }));
  }
  function editCredit(row, id) {
    if (row.querySelector(".ct-edit")) return row.querySelector(".ct-edit").remove();
    const f = document.createElement("form"); f.className = "ct-edit";
    f.innerHTML = `<input class="field" name="remaining" inputmode="decimal" placeholder="Remaining" aria-label="Remaining" required>
      <input class="field" name="total" inputmode="decimal" placeholder="Total (optional)" aria-label="Total">
      <select class="field" name="unit" aria-label="Unit"><option>USD</option><option>CNY</option><option>credits</option><option>tokens</option><option>chars</option></select>
      <input class="field note" name="note" placeholder="Note, e.g. plan renews on the 1st" aria-label="Note">
      <button class="btn" type="submit">Save</button><button class="btn ghost" type="button" data-cancel>Cancel</button><button class="btn ghost" type="button" data-clear>Remove entry</button>`;
    row.appendChild(f); f.remaining.focus();
    const send = async (body) => {
      try { await api("/api/credits/manual", { provider: id, ...body }); f.remove(); await refreshCredits(true); toast(body.remaining === "" ? "Entry removed." : "Saved."); }
      catch (e) { toast(plainError(e.message)); }
    };
    f.addEventListener("submit", (e) => { e.preventDefault(); send({ remaining: f.remaining.value, total: f.total.value, unit: f.unit.value, note: f.note.value }); });
    f.querySelector("[data-cancel]").addEventListener("click", () => f.remove());
    f.querySelector("[data-clear]").addEventListener("click", () => { if (confirm("Remove the balance you entered?")) send({ remaining: "" }); });
  }
  async function refreshCredits(force) {
    try {
      state.credits = await api("/api/credits" + (force ? "?refresh=1" : ""));
      emit("credits", state.credits);
      renderCreditRows(state.credits);
      if (!$("#view-credits").hidden) renderCreditTable(state.credits);
    } catch (e) { $("#credit-rows").innerHTML = `<div class="empty"><b>Couldn't read credits.</b>${esc(plainError(e.message))}</div>`; }
  }
  $("#credits-refresh").addEventListener("click", async () => { const b = $("#credits-refresh"); b.disabled = true; await refreshCredits(true); b.disabled = false; announce("Credits checked."); });

  // ---------- investigations ----------
  async function loadCases() {
    try { state.cases = await api("/api/cases"); } catch (e) { state.cases = []; }
    if (state.case && !state.cases.some((c) => c.slug === state.case)) state.case = "";
    const sel = $("#case-select");
    sel.innerHTML = `<option value="">No investigation</option>` + state.cases.map((c) => `<option value="${esc(c.slug)}">${esc(c.title)} · ${c.items}</option>`).join("") + `<option value="__new">New investigation…</option>`;
    sel.value = state.case; syncCase();
  }
  function syncCase() {
    const on = !!state.case;
    $("#case-switch").classList.toggle("active", on);
    const exp = $("#case-export"); exp.hidden = !on; exp.href = on ? "/api/export?case=" + encodeURIComponent(state.case) : "#";
    $("#layer-case").disabled = !on; $("#layer-case").title = on ? "Only this investigation and what it links to" : "Pick an investigation in the header first";
    $("#mem-in-case").disabled = !on; $("#mem-in-case").checked = on && store.get("jarvis.memInCase", true);
    const c = state.cases.find((x) => x.slug === state.case);
    $("#mem-add-label").textContent = c ? `Capture a finding · ${c.title}` : "Capture a finding";
    if (!on && layer === "case") setLayer("heap");
  }
  $("#case-select").addEventListener("change", async (e) => {
    if (e.target.value === "__new") {
      const name = prompt("Name the investigation, e.g. “Home lab exposure” or “Acme public infrastructure”.");
      if (name && name.trim()) {
        try { const c = await api("/api/cases", { name }); state.case = c.slug; store.set("jarvis.case", c.slug); toast(`Investigation “${name.trim()}” started. New notes and asks are filed into it.`); }
        catch (err) { toast(plainError(err.message)); }
      }
      return loadCases();
    }
    state.case = e.target.value; store.set("jarvis.case", state.case); syncCase();
    announce(state.case ? "Investigation selected." : "No investigation selected.");
    if (!$("#view-memory").hidden) searchMemory();
    if (!$("#view-graph").hidden && layer === "case") applyLayer();
  });

  // ---------- overview ----------
  async function refreshOverview() {
    try { state.overview = await api("/api/overview"); emit("overview", state.overview); }
    catch (e) { $("#top-status").innerHTML = `<span class="chip">Console server unreachable</span>`; return; }
    const o = state.overview;
    const running = o.tasks.states.running || 0;
    $("#top-status").innerHTML = [
      `<a class="chip" href="#memory">${icon("database")} ${o.memory.items} in memory</a>`,
      `<a class="chip${running ? " on" : ""}" href="#tasks">${running ? '<span class="dot"></span>' : icon("list-checks")} ${running ? running + " running" : Object.values(o.tasks.states).reduce((a, b) => a + b, 0) + " tasks"}</a>`,
      `<span class="chip">${icon("audio-lines")} ${o.voice.voiceprint ? "voiceprint enrolled" : "no voiceprint"}</span>`,
    ].join("");
    lastOverview = Date.now();
    renderListener(o);
    $("#listener-log").innerHTML = (o.voice.recent || []).filter((l) => !/ignored another voice/.test(l)).slice(-5).map((l) => {
      const m = l.match(/^(\S+ \S+)\s+(.*)$/); const text = m ? m[2] : l;
      return `<div title="${esc(l)}"><b>${esc(m ? m[1].slice(11, 16) : "")}</b> ${esc(text)}</div>`;
    }).join("") || "Listener log is empty.";
    const auto = $(".dock [data-tier='']");
    auto.title = o.routing.auto_escalate ? "Let the router pick from your words" : `Conversation: the ${o.brain?.name || "ChatGPT"} brain answers. Pick Tools or Plan for more.`;
    renderRouter(o); renderMachines(o); renderHeapFacts(o);
    if (!$("#view-devices").hidden) renderDevices();
    if (!$("#view-tasks").hidden) loadTasks();
    loaded.console = true;
  }
  function renderListener(o) {
    const hf = $("#hands-free"), was = hf.classList.contains("on"), on = !!o.voice.listening;
    hf.classList.toggle("on", on);
    hf.querySelector(".chip-label").textContent = on ? "Listening" : "Listener off";
    hf.setAttribute("aria-pressed", on);
    hf.title = on ? "Always-on listener is running. Say “Jarvis, …”. Click to stop it." : "Start the always-on listener (say “Jarvis, …”).";
    if (was !== on && loaded.console) announce(on ? "Listener on." : "Listener off.");
  }
  function renderRouter(o) {
    const counts = { simple: 0, medium: 0, complex: 0 };
    let total = 0;
    for (const h of history) if (h.tier in counts) { counts[h.tier]++; total++; }
    const r = o.routing, m = o.models.claude || {}, b = state.brain || o.brain || {};
    $("#router-meta").textContent = r.brain ? `Quick → ${b.name || r.brain} brain${b.warm ? " · warm" : ""}` : `Quick → ${r.simple_provider || "claude"}, ${r.simple_power || "low"} effort`;
    $("#router-meta").title = b.error ? "Brain couldn't start: " + b.error + ". Quick asks fall back to a one-shot " + (r.simple_provider || "claude") + " run." : "";
    const desc = { simple: "one cheap model run", medium: "your APIs, agents, web", complex: "specialist plan" };
    $("#router-body").innerHTML = `<div class="route-bars">${Object.entries(counts).map(([k, v]) => `
      <div class="route-row" title="${desc[k]}"><b>${tierName(k)}</b><span class="bar" role="img" aria-label="${v} of ${total}"><i style="width:${total ? (v / total) * 100 : 0}%"></i></span><span class="mono">${v}</span></div>`).join("")}</div>
      <p class="muted" style="font-size:12px;margin-top:10px">${total ? `How your last ${total} requests were handled, from every channel.` : "No requests yet. Ask something below."}</p>
      <dl class="facts mono route-models">${["low", "mid", "high"].map((p) => `<dt>${p} effort</dt><dd>${esc((m[p] || {}).model || "—")} · ${esc((m[p] || {}).effort || "—")}</dd>`).join("")}</dl>`;
  }
  // Explicit status vocabulary: config words map to what they mean, never by substring.
  function linkState(s) {
    s = String(s || "");
    if (/^(verified|paired_verified.*|ssh_verified.*)$/.test(s)) return ["linked", "ok"];
    if (/^not_paired$|^not_configured$|^unconfigured$/.test(s)) return [s === "not_paired" ? "not paired" : "not set up", "warn"];
    if (/^planned$/.test(s)) return ["planned", "warn"];
    if (/^user_reports/.test(s)) return ["reported", ""];
    return [words(s).split(" ").slice(0, 2).join(" ") || "unknown", ""];
  }
  const stateWord = (s) => { const [label, cls] = linkState(s); return `<span class="state-word ${cls}" title="${esc(words(s))}">${esc(label)}</span>`; };
  // ---------- live link probe ----------
  const LEVEL = { up: ["daemons up", "ok"], warn: ["up · with warnings", ""], down: ["down", "warn"] };
  function liveWord(live) {
    if (!live) return `<span class="state-word" title="Not checked yet">checking</span>`;
    if (live.local) return live.lan_mouse?.running ? `<span class="state-word ok">this laptop</span>` : `<span class="state-word warn">Lan Mouse off</span>`;
    if (!live.reachable) return `<span class="state-word warn" title="${esc(live.error || "")}">unreachable</span>`;
    if (!live.ssh) return `<span class="state-word warn" title="${esc(live.error || "")}">ssh failed</span>`;
    return `<span class="state-word ok">online</span>`;
  }
  function liveFacts(live) {
    if (!live) return "";
    const lm = live.lan_mouse || {};
    const rows = [];
    if (live.local) {
      rows.push(["Lan Mouse", lm.running ? `pid ${lm.pid}${lm.listening ? " · UDP 4242" : " · no socket"}` : "not running"]);
      if (lm.peers?.length) rows.push(["Sending to", lm.peers.join(", ")]);
    } else if (live.ssh) {
      rows.push(["Lan Mouse", lm.running ? `${lm.fallback === "active" ? "fallback build" : "JARVIS build"}${lm.listening ? " · UDP 4242" : " · no socket"}` : `${lm.unit} / ${lm.fallback}`]);
      rows.push(["Ping · SSH", `${live.ping_ms ?? "—"} ms · ${live.ssh_ms ?? "—"} ms`]);
      if (live.mem_avail_pct != null) rows.push(["Memory free", `${live.mem_avail_pct}%${live.swap_used_pct != null ? ` · swap ${live.swap_used_pct}%` : ""}`]);
      rows.push(["Load · up", `${live.load} · ${Math.round(live.uptime_s / 3600)} h`]);
      if (live.wifi_power_save) rows.push(["Wi-Fi power save", live.wifi_power_save]);
      if (live.ip) rows.push(["IP now", live.ip]);
    } else if (live.error) rows.push(["Error", live.error]);
    return `<dl class="facts mono live-facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("")}</dl>`;
  }
  function renderProbe() {
    const p = state.probe;
    $("#probe-checked").textContent = p ? "checked " + ago(p.checked_at) : "checking…";
    if (!p) return;
    const [label, cls] = LEVEL[p.input_link.level] || LEVEL.warn;
    $("#link-status").innerHTML = `<div class="link-verdict lvl-${esc(p.input_link.level)}">${icon("keyboard", "i")}<b>Input sharing</b><span class="state-word ${cls}">${label}</span>
      <ul>${p.input_link.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>`;
    if (state.overview) { renderMachines(state.overview); if (!$("#view-devices").hidden) renderDevices(); }
  }
  let probing = false;
  async function refreshProbe(force) {
    if (probing) return;
    probing = true; const b = $("#probe-refresh"); b.disabled = true;
    if (force) $("#probe-checked").textContent = "checking…";
    try { state.probe = await api("/api/probe" + (force ? "?refresh=1" : "")); renderProbe(); emit("probe", state.probe); if (force) announce("Machines checked: " + state.probe.input_link.lines[0]); }
    catch (e) { $("#probe-checked").textContent = "check failed"; $("#link-status").innerHTML = `<div class="empty"><b>Couldn't run the live check.</b>${esc(plainError(e.message))}</div>`; }
    finally { probing = false; b.disabled = false; }
  }
  $("#probe-refresh").addEventListener("click", () => refreshProbe(true));

  function renderMachines(o) {
    const live = state.probe?.devices || {};
    const links = Object.entries(o.desktop).map(([k, v]) => `<div class="machine"><h3>${icon({ input: "keyboard", clipboard: "list-checks", screen_stream: "radio", extended_display: "monitor" }[k] || "activity", "i")}${esc(words(k).replace(/^\w/, (c) => c.toUpperCase()))} ${k === "input" && state.probe ? `<span class="state-word ${(LEVEL[state.probe.input_link.level] || LEVEL.warn)[1]}">${(LEVEL[state.probe.input_link.level] || LEVEL.warn)[0]}</span>` : stateWord(v.status)}</h3><p>${esc(v.candidate || "")}</p></div>`).join("");
    $("#machines").innerHTML = o.devices.map((d) => `<div class="machine"><h3>${icon("laptop-minimal", "i")}${esc(d.hostname || d.id)} ${liveWord(live[d.id])}</h3>
      <p>${esc([d.model, d.cpu, d.ram].filter(Boolean).join(" · "))}</p>${liveFacts(live[d.id])}</div>`).join("") + links +
      `<p class="machine mono" style="font-size:11px;color:var(--ink-3);grid-column:1/-1">Machines and input sharing are checked live every minute. Clipboard, screen stream and display come from jarvis.config.json.</p>`;
  }
  function renderHeapFacts(o) {
    $("#heap-facts").innerHTML = `<dt>Items</dt><dd>${o.memory.items}</dd><dt>Links</dt><dd>${o.memory.links}</dd>` +
      Object.entries(o.memory.kinds).slice(0, 4).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
  }
  $("#hands-free").addEventListener("click", async () => {
    const on = state.overview?.voice?.listening, chip = $("#hands-free");
    chip.querySelector(".chip-label").textContent = on ? "Stopping…" : "Starting…"; chip.disabled = true;
    try { await api("/api/voice", { action: on ? "stop" : "start" }); toast(on ? "Listener stopped." : "Listener is on. Say “Jarvis, …”."); }
    catch (e) { toast(plainError(e.message)); }
    chip.disabled = false; refreshOverview();
  });

  // ---------- graphs ----------
  const graphs = [];
  const mini = new ForceGraph($("#mini-graph"), { mini: true }); graphs.push(mini);
  async function loadMini() { try { mini.setData(await api("/api/graph/heap")); } catch (e) {} }
  let big, graphData = {}, layer = "heap";
  async function loadGraph(again) {
    if (!big) {
      big = new ForceGraph($("#graph-canvas"), { onSelect: inspect }); graphs.push(big);
      $("#graph-legend").innerHTML = Object.entries({ item: "Memory item (size = importance)", kind: "Kind or tag it shares", module: "Code module (size = lines)", provider: "External service, model or stored state", device: "Machine", specialist: "Specialist agent", "personal-agent": "Personal agent" })
        .map(([k, label]) => `<div>${legendShape(ForceGraph.SHAPES[k])}${label}</div>`).join("");
    }
    if (again && graphData.loadedLayer === layer) return;
    return applyLayer();
  }
  function legendShape(shape) {
    const s = { square: '<rect x="2" y="2" width="8" height="8" fill="currentColor"/>', diamond: '<path d="M6 1 11 6 6 11 1 6z" fill="none" stroke="currentColor"/>',
      frame: '<rect x="2" y="2" width="8" height="8" fill="none" stroke="currentColor" stroke-width="1.6"/>', dashed: '<circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" stroke-dasharray="1.6 1.6"/>',
      dot: '<circle cx="6" cy="6" r="2.5" fill="currentColor"/>', circle: '<circle cx="6" cy="6" r="4" fill="currentColor"/>', ring: '<circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor"/><circle cx="6" cy="6" r="1.6" fill="currentColor"/>' }[shape];
    return `<svg viewBox="0 0 12 12" aria-hidden="true">${s}</svg>`;
  }
  async function applyLayer() {
    try {
      const need = { heap: ["heap"], case: ["case"], system: ["system"], all: ["system", "heap"] }[layer];
      const got = await Promise.all(need.map((k) => api(k === "system" ? "/api/graph/system" : "/api/graph/heap" + (k === "case" ? "?case=" + encodeURIComponent(state.case) : ""))));
      const data = got.length === 1 ? got[0] : { nodes: [...got[0].nodes, ...got[1].nodes], edges: [...got[0].edges, ...got[1].edges,
        ...got[1].nodes.filter((n) => n.kind === "kind").map((n) => ({ src: n.id, dst: "ext:memdb", rel: "stored in" }))] };
      big.setData(data); graphData.loadedLayer = layer;
      if (!data.nodes.length) toast(layer === "case" ? "Nothing filed in this investigation yet." : "Nothing to draw yet.");
      updateMatches();
    } catch (e) { toast("Graph: " + plainError(e.message)); return false; }
  }
  function setLayer(name) {
    layer = name; $$(".graph-toolbar [data-layer]").forEach((x) => x.setAttribute("aria-pressed", x.dataset.layer === name));
    inspect(null); if (big) applyLayer();
  }
  $$(".graph-toolbar [data-layer]").forEach((b) => b.addEventListener("click", () => setLayer(b.dataset.layer)));
  function updateMatches() {
    const list = $("#graph-matches"), hits = big ? big.matching().slice(0, 12) : [];
    list.hidden = !hits.length;
    list.innerHTML = hits.map((n) => `<li><button type="button" data-id="${esc(n.id)}"><span>${esc(n.label)}</span><span class="mono">${esc(ForceGraph.LABELS[n.kind] || n.kind)}</span></button></li>`).join("");
    $$("button", list).forEach((b) => b.addEventListener("click", () => { big.select(b.dataset.id); list.hidden = true; }));
  }
  $("#graph-filter").addEventListener("input", (e) => { if (big) { big.setFilter(e.target.value); updateMatches(); } });
  $("#graph-filter").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); const hit = big?.matching()[0]; if (hit) { big.select(hit.id); $("#graph-matches").hidden = true; } }
    if (e.key === "ArrowDown") { e.preventDefault(); $("#graph-matches button")?.focus(); }
  });
  $("#graph-reset").addEventListener("click", () => big && big.reheat());
  let inspectSeq = 0;
  async function inspect(node, links = []) {
    const box = $("#inspector"), seq = ++inspectSeq;
    if (!node) { box.hidden = true; return; }
    let body = node.doc ? `<p>${esc(node.doc)}</p>` : "";
    if (node.kind === "module") body += `<dl class="facts mono"><dt>Lines</dt><dd>${node.loc}</dd><dt>Public</dt><dd>${node.defs.length}</dd></dl>` + (node.defs.length ? `<p class="mono" style="font-size:11.5px">${node.defs.map(esc).join(" · ")}</p>` : "");
    if (node.kind === "specialist") body += `<dl class="facts mono"><dt>Model</dt><dd>${esc(node.model || "—")}</dd><dt>Effort</dt><dd>${esc(node.power || "—")}</dd></dl>`;
    if (node.kind === "item") {
      try {
        const item = await api("/api/memory/item?id=" + encodeURIComponent(node.id));
        if (seq !== inspectSeq) return; // a newer selection won
        body += `<dl class="facts mono"><dt>Kind</dt><dd>${esc(item.kind)}</dd><dt>Source</dt><dd>${esc(item.source)}</dd><dt>Saved</dt><dd>${ago(item.created_at)}</dd><dt>Importance</dt><dd>${item.importance}</dd></dl><p style="white-space:pre-wrap;max-height:220px;overflow:auto">${esc(item.body.slice(0, 1600))}</p>
          <a class="link-btn" href="#memory" data-open="${esc(item.id)}">Open in Memory${icon("chevron-right")}</a>`;
      } catch (e) {}
    }
    if (seq !== inspectSeq) return;
    box.innerHTML = `<button class="icon-btn close" type="button" aria-label="Close details">${icon("x", "i")}</button>
      <span class="kind">${esc(ForceGraph.LABELS[node.kind] || node.kind)}</span><h3>${esc(node.label)}</h3>${body}
      ${links.length ? `<span class="kind">Connections · ${links.length}</span><ul>${links.slice(0, 40).map((l) => `<li><button type="button" data-id="${esc(l.other.id)}"><span>${esc(l.other.label)}</span><span class="mono">${l.out ? "" : "← "}${esc(l.rel)}</span></button></li>`).join("")}</ul>` : ""}`;
    box.hidden = false;
    box.querySelector(".close").addEventListener("click", () => { big.select(null); $("#graph-canvas").focus(); });
    $$("[data-id]", box).forEach((b) => b.addEventListener("click", () => big.select(b.dataset.id)));
    box.querySelector("[data-open]")?.addEventListener("click", (e) => { pendingOpen = e.currentTarget.dataset.open; });
    announce(`${ForceGraph.LABELS[node.kind] || node.kind}: ${node.label}. ${links.length} connections.`);
  }

  // ---------- memory ----------
  let pendingOpen = null;
  async function loadMemory() {
    const sel = $("#mem-kind"), keep = sel.value;
    sel.innerHTML = `<option value="">All kinds</option>` + Object.keys(state.overview?.memory?.kinds || {}).map((k) => `<option>${esc(k)}</option>`).join("");
    sel.value = keep;
    $("#memory-meta").textContent = `${state.overview?.memory?.items ?? "?"} items · ${state.overview?.memory?.links ?? "?"} links`;
    await searchMemory();
    if (pendingOpen) { openMemory(pendingOpen); pendingOpen = null; }
  }
  async function searchMemory() {
    const days = $("#mem-since").value;
    const params = { q: $("#mem-q").value, kind: $("#mem-kind").value, tag: $("#mem-tag").value.trim().replace(/^#/, ""), limit: 60,
                     min_importance: $("#mem-imp").value, since: days ? new Date(Date.now() - (days === "1" ? (Date.now() - new Date().setHours(0, 0, 0, 0)) : days * 86400e3)).toISOString() : "" };
    if (state.case && $("#mem-in-case").checked) params.case = state.case;
    try {
      const items = await api(`/api/memory?${new URLSearchParams(params)}`);
      const tags = new Set(); items.forEach((it) => it.tags.forEach((t) => tags.add(t)));
      $("#tag-list").innerHTML = [...tags].sort().map((t) => `<option value="${esc(t)}">`).join("");
      const filtered = params.q || params.kind || params.tag || params.since || +params.min_importance || params.case;
      $("#mem-list").innerHTML = items.length ? items.map((it) => `<li><button type="button" data-id="${esc(it.id)}"><b>${esc(it.title)}</b><span class="mono">${esc(it.kind)} · ${ago(it.created_at)}</span><p>${esc(it.preview)}</p></button></li>`).join("")
        : `<li class="empty"><b>${filtered ? "Nothing matches these filters." : "Memory is empty."}</b>${filtered ? "Loosen a filter or use fewer words; search matches word stems and prefixes." : "Every request, reply and plan lands here automatically, and anything you capture below."}</li>`;
      $$("#mem-list button").forEach((b) => b.addEventListener("click", () => openMemory(b.dataset.id)));
    } catch (e) { $("#mem-list").innerHTML = `<li class="empty"><b>Search failed.</b>${esc(plainError(e.message))}</li>`; }
  }
  async function openMemory(id) {
    $$("#mem-list button").forEach((b) => b.setAttribute("aria-current", b.dataset.id === id));
    try {
      const it = await api("/api/memory/item?id=" + encodeURIComponent(id));
      $("#mem-detail-title").textContent = it.kind + " · " + it.id.split(":").pop();
      const linkRow = (l) => { const out = l.src === it.id, other = out ? l.dst : l.src;
        return `<button class="link-btn mono" type="button" data-id="${esc(other)}">${out ? "" : "← "}${esc(l.rel)} · ${esc(other)}</button>`; };
      $("#mem-detail").innerHTML = `<h3 style="font-size:17px;font-weight:500;letter-spacing:-.015em">${esc(it.title)}</h3>
        <dl class="facts mono"><dt>ID</dt><dd>${esc(it.id)}</dd><dt>Source</dt><dd>${esc(it.source)}</dd><dt>Saved</dt><dd>${new Date(it.created_at).toLocaleString()}</dd><dt>Importance</dt><dd>${it.importance}</dd>
          ${it.meta?.source_url ? `<dt>URL</dt><dd><a href="${esc(it.meta.source_url)}" target="_blank" rel="noopener noreferrer">${esc(it.meta.source_url)}</a></dd>` : ""}</dl>
        ${it.tags.length ? `<div class="tags">${it.tags.map((t) => `<button type="button" class="state-word" data-tag="${esc(t)}" title="Filter by this tag">#${esc(t)}</button>`).join("")}</div>` : ""}
        <pre>${esc(it.body)}</pre>
        <div class="links-list">${it.links.length ? `<span class="ph"><h2>Links · ${it.links.length}</h2></span>${it.links.map(linkRow).join("")}` : ""}</div>
        <form class="link-form" id="link-form">
          <label class="ph" for="link-q" style="margin:0"><h2>Link to another item</h2></label>
          <div class="row"><select class="field" id="link-rel" aria-label="Relation">
            <option value="related">related to</option><option value="supports">supports</option><option value="contradicts">contradicts</option><option value="part-of">part of</option><option value="answers">answers</option><option value="follows">follows</option>
          </select><input class="field" id="link-q" type="search" placeholder="Search for the other item" autocomplete="off"></div>
          <ul class="link-hits" id="link-hits"></ul>
        </form>`;
      $$("#mem-detail [data-id]").forEach((b) => b.addEventListener("click", () => openMemory(b.dataset.id)));
      $$("#mem-detail [data-tag]").forEach((b) => b.addEventListener("click", () => { $("#mem-tag").value = b.dataset.tag; searchMemory(); }));
      let t;
      $("#link-q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(async () => {
        const q = e.target.value.trim(); if (q.length < 2) { $("#link-hits").innerHTML = ""; return; }
        const hits = (await api("/api/memory?" + new URLSearchParams({ q, limit: 8 }))).filter((h) => h.id !== it.id);
        $("#link-hits").innerHTML = hits.map((h) => `<li><button type="button" data-dst="${esc(h.id)}"><span>${esc(h.title)}</span><span class="mono">${esc(h.kind)}</span></button></li>`).join("") || `<li class="muted" style="font-size:12.5px;padding:6px 8px">No match.</li>`;
        $$("#link-hits [data-dst]").forEach((b) => b.addEventListener("click", async () => {
          try { await api("/api/memory/link", { src: it.id, dst: b.dataset.dst, rel: $("#link-rel").value }); toast("Linked."); openMemory(it.id); loadMini(); }
          catch (err) { toast(plainError(err.message)); }
        }));
      }, 220); });
      $("#link-form").addEventListener("submit", (e) => e.preventDefault());
    } catch (e) { toast(plainError(e.message)); }
  }
  let memTimer;
  const queueSearch = () => { clearTimeout(memTimer); memTimer = setTimeout(searchMemory, 220); };
  ["#mem-q", "#mem-tag"].forEach((s) => $(s).addEventListener("input", queueSearch));
  ["#mem-kind", "#mem-since", "#mem-imp"].forEach((s) => $(s).addEventListener("change", searchMemory));
  $("#mem-in-case").addEventListener("change", (e) => { store.set("jarvis.memInCase", e.target.checked); searchMemory(); });
  $("#mem-search").addEventListener("submit", (e) => { e.preventDefault(); searchMemory(); });
  $("#mem-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("#mem-note").value.trim(); if (!text) { $("#mem-note").focus(); return toast("Write the finding first."); }
    const tags = $("#mem-tags").value.split(",").map((t) => t.trim().replace(/^#/, "")).filter(Boolean);
    try {
      const it = await api("/api/memory", { text, tags, kind: $("#mem-new-kind").value, title: $("#mem-title").value.trim(),
        importance: +$("#mem-new-imp").value, source_url: $("#mem-url").value.trim(), case: state.case || null });
      ["#mem-note", "#mem-tags", "#mem-title", "#mem-url"].forEach((s) => ($(s).value = ""));
      toast("Saved as " + it.id + (state.case ? " in this investigation." : "."));
      await refreshOverview(); loadMemory(); openMemory(it.id); loadMini(); if (state.case) loadCases();
    } catch (err) { toast(plainError(err.message)); }
  });

  // ---------- tasks and plans ----------
  let planPoll = null, openPlanId = null, pendingPlan = null;
  function loadTasks() {
    const o = state.overview; if (!o) return false;
    const list = o.tasks.recent;
    $("#tasks-meta").textContent = Object.entries(o.tasks.states).map(([k, v]) => `${v} ${words(k)}`).join(" · ");
    $("#task-list").innerHTML = list.length ? list.map((t) => `<li><button type="button" data-id="${t.id}"><b>${esc(t.objective || t.id)}</b><span class="state-word ${t.state === "finished" ? "ok" : ""}">${esc(words(t.state))}</span><p class="mono">${esc([t.provider, t.power && t.power + " effort", t.role, t.mode].filter(Boolean).join(" · "))} · ${ago(t.created_at)}</p></button></li>`).join("")
      : `<li class="empty"><b>No tasks yet.</b>Quick and Tools requests create a task here as they run.</li>`;
    $("#plan-list").innerHTML = o.plans.length ? o.plans.map((p) => { const done = p.steps.filter((s) => s.state === "finished").length;
      return `<li><button type="button" data-plan="${esc(p.id)}"><b>${esc(p.goal)}</b><span class="state-word ${p.state === "finished" ? "ok" : ""}">${esc(p.state)}</span><p>${done}/${p.steps.length} steps done · ${esc(p.summary)}</p></button></li>`; }).join("")
      : `<li class="empty"><b>No plans yet.</b>Pick <b>Plan</b> in the prompt bar (or describe something big) and specialists draft one here for you to review and run.</li>`;
    $$("#task-list button").forEach((b) => b.addEventListener("click", () => openTask(b.dataset.id)));
    $$("#plan-list button").forEach((b) => b.addEventListener("click", () => openPlan(b.dataset.plan)));
    if (pendingPlan) { openPlan(pendingPlan); pendingPlan = null; }
  }
  async function openTask(id) {
    clearInterval(planPoll); openPlanId = null; emit("detail", { task: id });
    $$("#task-list button, #plan-list button").forEach((x) => x.setAttribute("aria-current", x.dataset.id === id));
    try {
      const t = await api("/api/tasks/" + id);
      $("#task-detail-title").textContent = "Task events";
      $("#task-detail").innerHTML = `<h3 style="font-size:16px;font-weight:500">${esc(t.spec.objective)}</h3>
        <dl class="facts mono"><dt>ID</dt><dd>${esc(t.id)}</dd><dt>State</dt><dd>${esc(t.state)}</dd><dt>Provider</dt><dd>${esc(t.spec.provider)} · ${esc(t.spec.power || "")} effort</dd><dt>Mode</dt><dd>${esc(t.spec.mode)}</dd></dl>
        <ol class="events">${t.events.map((ev) => { let d = {}; try { d = JSON.parse(ev.detail); } catch (x) {} const extra = Object.entries(d).filter(([k]) => !/prompt|stdout/.test(k)).slice(0, 3).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v).slice(0, 80) : String(v).slice(0, 80)}`).join(" · ");
          return `<li><b>${esc(words(ev.kind))}</b><span class="mono">${new Date(ev.at).toLocaleString()}${extra ? " · " + esc(extra) : ""}</span></li>`; }).join("")}</ol>`;
    } catch (e) { toast(plainError(e.message)); }
  }
  async function openPlan(id, quiet) {
    if (!quiet) emit("detail", { plan: id });
    $$("#task-list button, #plan-list button").forEach((x) => x.setAttribute("aria-current", x.dataset.plan === id));
    let p;
    try { p = await api("/api/plans/" + id); } catch (e) { return toast(plainError(e.message)); }
    $("#task-detail-title").textContent = "Plan"; openPlanId = id;
    const runnable = p.state === "planned";
    const left = p.steps.filter((s) => s.state === "pending").length;
    $("#task-detail").innerHTML = `<h3 style="font-size:16px;font-weight:500">${esc(p.goal)}</h3>
      <p class="muted" style="font-size:13px">${esc(p.summary)}</p>
      <div class="run-row">${runnable ? `<button class="btn" type="button" id="plan-run">${icon("play")} Run ${left} step${left === 1 ? "" : "s"}</button>
        <p>Each step is a real model run on your subscription, one at a time in order. It stops at the first failed step.</p>` :
        `<span class="state-word ${p.state === "finished" ? "ok" : ""}">${esc(p.state)}</span><p>${p.state === "running" ? "Running. Each step updates here as it finishes." : p.state === "blocked" ? "A step failed; open its task to see why." : "All steps finished. Results are in Memory."}</p>`}</div>
      <ol class="steps">${p.steps.map((s) => `<li><div class="step-head"><span class="mono">${esc(s.id)}</span>${esc(s.specialist)}<span class="mono">${esc(s.power || "")} effort${s.depends_on.length ? " · after " + esc(s.depends_on.join(", ")) : ""}</span><span class="state-word ${s.state === "finished" ? "ok" : s.state === "pending" ? "warn" : ""}">${esc(s.state)}</span></div>
        <p>${esc(s.objective)}</p>
        ${s.acceptance?.length ? `<details><summary>Done when…</summary><ul style="margin:4px 0 0;padding-left:18px;font-size:12.5px;color:var(--ink-2)">${s.acceptance.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></details>` : ""}
        ${s.answer ? `<details><summary>Result</summary><pre>${esc(s.answer)}</pre></details>` : ""}</li>`).join("")}</ol>`;
    $("#plan-run")?.addEventListener("click", async (e) => {
      const b = e.currentTarget;
      if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.innerHTML = `${icon("play")} Confirm: run ${left} step${left === 1 ? "" : "s"}`; setTimeout(() => { if (b.isConnected) { b.dataset.armed = ""; b.innerHTML = `${icon("play")} Run ${left} step${left === 1 ? "" : "s"}`; } }, 4000); return; }
      try { await api(`/api/plans/${id}/run`, {}); toast("Plan started."); announce("Plan started."); setTimeout(() => openPlan(id), 600); }
      catch (err) { toast(plainError(err.message)); }
    });
    clearInterval(planPoll);
    // The live stream pushes plan.updated; this poll only covers a dropped connection.
    if (p.state === "running" || !quiet) planPoll = setInterval(async () => {
      if ($("#view-tasks").hidden) return clearInterval(planPoll);
      if (live.connected) return;
      const q = await api("/api/plans/" + id).catch(() => null);
      if (q && JSON.stringify(q.steps.map((s) => s.state)) + q.state !== JSON.stringify(p.steps.map((s) => s.state)) + p.state) { openPlan(id, true); refreshOverview(); }
      if (q && q.state !== "running" && p.state !== "running") clearInterval(planPoll);
    }, 3000);
  }

  // ---------- devices ----------
  function renderDevices() {
    const o = state.overview; if (!o) return false;
    const tools = (t) => Object.entries(t || {}).map(([k, v]) => `<span class="state-word ${v ? "ok" : "warn"}" title="${v ? "found on PATH" : "not on PATH (may still be installed elsewhere)"}">${esc(k)}</span>`).join("");
    const live = state.probe?.devices || {};
    $("#devices-grid").innerHTML = o.devices.map((d) => `<article class="panel ticks">
        <header class="ph">${icon("laptop-minimal")}<h2>${esc(d.id)}</h2>${liveWord(live[d.id])}<span class="ph-meta mono">${state.probe ? "checked " + ago(state.probe.checked_at) : ""}</span></header>
        ${liveFacts(live[d.id])}
        <h3>${esc(d.model || d.hostname)}</h3><p class="lede">${esc(d.hostname)} · ${esc(d.platform || "")} · ${esc(d.transport)}</p>
        <dl class="facts mono"><dt>CPU</dt><dd>${esc(d.cpu || "—")}</dd><dt>Memory</dt><dd>${esc(d.ram || "—")}</dd><dt>GPU</dt><dd>${esc(d.gpu || "—")}</dd>
        <dt>Address</dt><dd>${esc(d.address || "—")}</dd><dt>Status</dt><dd>${esc(words(d.status))}</dd><dt>Inventory</dt><dd>${ago(d.collected_at)}</dd></dl>
        <p class="muted" style="font-size:12px;margin-top:14px">Commands on PATH when the inventory was taken:</p><div class="toolmarks">${tools(d.tools)}</div></article>`).join("") +
      `<article class="panel ticks"><header class="ph">${icon("radio")}<h2>Links between them</h2></header>
        <dl class="facts mono">${Object.entries(o.desktop).map(([k, v]) => `<dt>${esc(words(k))}</dt><dd title="${esc(v.status)}">${esc(v.candidate || "")} · ${esc(linkState(v.status)[0])}</dd>`).join("")}
        ${Object.entries(o.network).map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("")}</dl>
        <p class="muted" style="font-size:12px;margin-top:12px">From jarvis.config.json, not a live check.</p></article>` +
      `<article class="panel ticks"><header class="ph">${icon("arrow-up-right")}<h2>Send to the other laptop</h2></header>
        <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Drop files here or press Enter to choose">
          <p><b>Drop files or images here</b></p><p class="muted">or click to choose. They land in <span class="mono">~/JARVIS-Drop</span> on the Zenbook.</p>
          <input type="file" id="drop-input" multiple hidden></div>
        <ul class="drop-log mono" id="drop-log" aria-live="polite"></ul></article>` +
      `<article class="panel ticks"><header class="ph">${icon("activity")}<h2>Personal agents</h2></header>
        <dl class="facts mono">${Object.entries(o.agents).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(words(v.integration))} · ${esc(linkState(v.status)[0])}</dd>`).join("")}</dl>
        <p class="muted" style="font-size:12.5px;margin-top:12px">Muse, Grokbot and Jev get a JARVIS adapter once their interface (CLI, local API or extension) is known.</p></article>`;
    wireDropzone();
  }

  // Files go over the existing SSH link: a drag can't cross between Windows and Wayland.
  function wireDropzone() {
    const zone = $("#dropzone"), input = $("#drop-input"), log = $("#drop-log");
    if (!zone) return;
    const line = (text, cls) => { const li = document.createElement("li"); li.className = cls || ""; li.textContent = text; log.prepend(li); return li; };
    async function sendAll(files) {
      for (const file of files) {
        const li = line(`Sending ${file.name} …`, "pending");
        try {
          const r = await api("/api/send?name=" + encodeURIComponent(file.name), file, true);
          li.textContent = `Sent ${r.sent} (${compact(r.bytes)} bytes)`; li.className = "ok"; announce(li.textContent);
        } catch (e) { li.textContent = `${file.name}: ${e.message}`; li.className = "bad"; }
      }
    }
    zone.addEventListener("click", () => input.click());
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    input.addEventListener("change", () => { sendAll([...input.files]); input.value = ""; });
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("over"));
    zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); sendAll([...e.dataTransfer.files]); });
  }

  // ---------- session thread: every channel, from memory ----------
  let history = [];
  const mine = new Set(store.get("jarvis.mine", []));       // message IDs sent from this console
  let local = [];                                            // pending or failed asks not in memory
  let lastHistory = 0;
  async function loadHistory() {
    try { history = await api("/api/history?limit=40"); } catch (e) { return; }
    lastHistory = Date.now();
    // A live turn is retired once its reply is in memory.
    for (const h of history) if (h.reply && live.turns.get(h.id)?.done) live.turns.delete(h.id);
    // An interrupted CLI or listener can leave a turn with no end; give up after 20 min.
    for (const [id, t] of live.turns) if (!t.done && Date.now() - t.at > 20 * 60e3) live.turns.delete(id);
    renderThread(); if (state.overview) renderRouter(state.overview);
  }
  const CHANNEL = { console: "console", voice: "voice", cli: "CLI" };
  const channelOf = (h) => CHANNEL[h.channel] || (mine.has(h.id) ? "console" : "voice or CLI");
  // Streaming (or just-finished) reply for a request whose answer isn't in memory yet.
  const liveReply = (t, tier) => ({ who: "jarvis", text: t.done ? t.reply : t.sentences.join(" "), at: t.at + 1, tier: t.tier || tier,
    streaming: !t.done, error: t.error, meta: t.done ? [t.seconds != null ? t.seconds + "s" : null, t.plan || null].filter(Boolean) : [t.sentences.length ? "answering" : "thinking"] });
  function renderThread() {
    const el = $("#thread");
    const rows = [], known = new Set();
    for (const h of history) {
      known.add(h.id);
      rows.push({ who: "you", text: h.text, at: stamp(h.at), tier: h.tier, channel: channelOf(h), voice: live.turns.get(h.id)?.voice });
      if (h.reply) rows.push({ who: "jarvis", text: h.reply.body, at: stamp(h.reply.at), tier: h.tier, plan: h.reply.meta?.plan,
        meta: [h.reply.meta?.task ? "task " + h.reply.meta.task.slice(0, 6) : null, h.reply.meta?.adapter || null].filter(Boolean) });
      else if (live.turns.has(h.id)) rows.push(liveReply(live.turns.get(h.id), h.tier));
    }
    for (const [id, t] of live.turns) {
      if (known.has(id)) continue;
      rows.push({ who: "you", text: t.text, at: t.at, tier: t.tier, channel: CHANNEL[t.channel] || t.channel, voice: t.voice });
      rows.push(liveReply(t));
    }
    rows.push(...local);
    rows.sort((a, b) => a.at - b.at);
    if (!rows.length) {
      el.innerHTML = `<li class="thread-empty"><h3>What should I work on?</h3>
        <p>Type below, or click the mic (or hold Space when nothing is focused). Quick asks run one cheap model call; big jobs get a specialist plan you review first.</p>
        <div class="suggest">${["What did I decide about the clipboard?", "Summarize what's in memory about Lan Mouse", "Plan a recon of my home lab's exposed services"].map((s) => `<button type="button">${esc(s)}</button>`).join("")}</div></li>`;
      $$(".suggest button", el).forEach((b) => b.addEventListener("click", () => { prompt.value = b.textContent; autosize(); prompt.focus(); }));
      return;
    }
    const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    el.innerHTML = rows.slice(-60).map((m) => `<li class="msg ${m.who}${m.error ? " error" : ""}${m.streaming ? " streaming" : ""}"${m.streaming ? ' aria-busy="true"' : ""}>
      <div class="msg-head"><span class="who">${m.who === "you" ? "You" : "JARVIS"}</span>${m.who === "you" && m.channel ? `<span class="src-tag">${esc(m.voice ? m.channel + " · voice" : m.channel)}</span>` : ""}${m.tier ? `<span class="tier">${esc(tierName(m.tier))}</span>` : ""}<time>${dayTime(m.at)}</time></div>
      <div class="msg-body">${esc(m.text)}</div>
      ${m.meta?.length || m.plan ? `<div class="msg-meta">${(m.meta || []).map((x) => `<span class="state-word">${esc(x)}</span>`).join("")}${m.plan ? `<a class="state-word" href="#tasks" data-plan-open="${esc(m.plan)}" title="Review and run this plan">${icon("list-checks")} open ${esc(m.plan)}</a>` : ""}</div>` : ""}</li>`).join("");
    $$("[data-plan-open]", el).forEach((a) => a.addEventListener("click", () => { pendingPlan = a.dataset.planOpen; if (!$("#view-tasks").hidden) loadTasks(); }));
    if (stick || busy) el.scrollTop = el.scrollHeight;
  }
  $("#thread-clear").addEventListener("click", () => { local = []; renderThread(); toast("Cleared unsent and failed lines. Everything else stays in Memory."); });

  // ---------- trace ----------
  function trace(steps) {
    $("#trace").innerHTML = steps.map(([label, s]) => `<span class="step ${s}"><span class="dot"></span>${esc(label)}</span>`).join("");
  }

  // ---------- dock ----------
  const prompt = $("#prompt");
  function autosize() { prompt.style.height = "auto"; prompt.style.height = Math.min(160, prompt.scrollHeight) + "px"; }
  prompt.addEventListener("input", autosize);
  const narrow = matchMedia("(max-width: 640px)");
  const placeholder = () => (prompt.placeholder = narrow.matches ? "Ask JARVIS…" : state.case ? "Ask JARVIS (filed into this investigation)" : "Ask JARVIS, or hold Space to talk");
  narrow.addEventListener("change", placeholder); placeholder();
  $("#case-select").addEventListener("change", placeholder);
  prompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#dock").requestSubmit(); }
    // Space in an empty prompt is never a word: treat it as push-to-talk.
    else if (e.code === "Space" && !prompt.value && !e.repeat) { e.preventDefault(); spaceAt = Date.now(); startMic(); }
  });
  const tierBtns = $$(".dock [data-tier]");
  function setTier(t) {
    state.tier = t; $("#tier-select").value = t;
    tierBtns.forEach((x) => { x.setAttribute("aria-checked", x.dataset.tier === t); x.tabIndex = x.dataset.tier === t ? 0 : -1; });
  }
  tierBtns.forEach((b, i) => {
    b.addEventListener("click", () => setTier(b.dataset.tier));
    b.addEventListener("keydown", (e) => {
      const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key]; if (!d) return;
      e.preventDefault(); const n = tierBtns[(i + d + tierBtns.length) % tierBtns.length]; setTier(n.dataset.tier); n.focus();
    });
  });
  $("#tier-select").addEventListener("change", (e) => setTier(e.target.value));
  const speakBtn = $("#speak-toggle");
  function syncSpeak() { speakBtn.setAttribute("aria-pressed", state.speak); speakBtn.querySelector("use").setAttribute("href", `icons.svg?v=2#i-${state.speak ? "volume-2" : "volume-x"}`); speakBtn.title = state.speak ? "Replies are read aloud on this laptop" : "Read replies aloud on this laptop"; }
  speakBtn.addEventListener("click", () => { state.speak = !state.speak; store.set("jarvis.speak", state.speak); syncSpeak(); });
  syncSpeak();

  let busy = false, current = null; // current: the console ask being waited on
  const send = $("#send");
  function setBusy(on) {
    busy = on;
    send.setAttribute("aria-label", on ? "Stop waiting" : "Send");
    send.title = on ? "Stop waiting for this answer (Esc)" : "Send (Enter)";
    send.querySelector("use").setAttribute("href", `icons.svg?v=2#i-${on ? "square" : "arrow-up"}`);
  }
  $("#dock").addEventListener("submit", (e) => { e.preventDefault(); if (busy) { stopWaiting(); return; } ask(prompt.value); });
  const routeLabel = (r) => `${tierName(r.tier)}${r.handler ? " · " + r.handler : r.reasons?.length ? " · " + r.reasons.slice(0, 2).join(", ") : ""}`;
  // Resolves when the job ends: pushed by the live stream (turn.done/turn.error), with a
  // slow poll underneath in case the stream drops.
  function waitForJob(id, limit) {
    return new Promise((resolve) => {
      const w = { resolve, misses: 0, timer: null, limit: setTimeout(() => finishWait(id, { state: "timeout" }), limit) };
      live.waiters.set(id, w);
      const poll = async () => {
        if (!live.waiters.has(id)) return;
        try { const j = await api("/api/jobs/" + id); w.misses = 0; if (j.state !== "running") return finishWait(id, j); }
        catch (e) { if (++w.misses > 5) return finishWait(id, { state: "lost" }); }
        w.timer = setTimeout(poll, live.connected ? 5000 : 900);
      };
      w.timer = setTimeout(poll, live.connected ? 5000 : 900);
    });
  }
  function finishWait(id, job) {
    const w = live.waiters.get(id); if (!w) return;
    clearTimeout(w.timer); clearTimeout(w.limit); live.waiters.delete(id); w.resolve(job);
  }
  function adopt(mid, t) {
    current.message = mid; current.route = { ...current.route, tier: t.tier, reasons: t.reasons || current.route.reasons };
    t.voice = current.pending.voice;
    mine.add(mid); store.set("jarvis.mine", [...mine].slice(-300));
    local = local.filter((m) => m !== current.pending);
    drawThread();
  }
  function stopWaiting() { for (const id of [...live.waiters.keys()]) finishWait(id, { state: "stopped" }); }
  async function ask(text, heard) {
    text = (text || "").trim();
    if (!text) return;
    if (busy) return toast("JARVIS is still on the last request. Press Esc to stop waiting.");
    setBusy(true); clearTimeout(previewTimer);
    prompt.value = ""; autosize(); prompt.blur(); // so Space talks again right away
    const pending = { who: "you", text, at: Date.now(), voice: !!heard, channel: "console", tier: state.tier || null };
    local.push(pending); renderThread();
    const pre = heard ? [[`heard you · voice match ${heard.score ?? "not checked"}`, "done"]] : [];
    trace([...pre, ["routing", "active"]]); announce("Sent. Routing.");
    let ticker;
    try {
      const job = await api("/api/ask", { text, tier: state.tier || null, speak: state.speak, case: state.case || null });
      current = { id: job.id, pending, route: job.route, t0: Date.now(), message: null };
      pending.tier = job.route.tier;
      // The stream can beat this response: adopt a turn.start that already arrived.
      for (const [mid, t] of live.turns) if (t.job === job.id) adopt(mid, t);
      const draw = () => {
        if (!current) return;
        const t = current.message && live.turns.get(current.message), s = Math.round((Date.now() - current.t0) / 1000);
        if (t?.sentences.length) current.answering = true;
        trace([...pre, [routeLabel(current.route), "done"], [current.answering ? `answering · ${s}s` : `working · ${s}s · Esc to stop waiting`, "active"]]);
      };
      draw(); ticker = setInterval(draw, 500);
      const done = await waitForJob(job.id, 15 * 60e3);
      const routed = routeLabel(current.route);
      if (done.state === "stopped") { trace([...pre, [routed, "done"], ["stopped waiting · JARVIS may still finish; the reply will appear here", "fail"]]); announce("Stopped waiting."); return; }
      if (done.state === "timeout") { trace([...pre, [routed, "done"], ["still running after 15 min · check Tasks", "fail"]]); return; }
      if (done.state === "lost") { trace([...pre, [routed, "done"], ["lost contact with the console server · the job may still be running", "fail"]]); return; }
      if (current.message) local = local.filter((m) => m !== pending); // memory has it now
      const secs = Math.round((done.finished - done.started) * 10) / 10;
      if (done.state === "done") {
        const res = done.result;
        (res.memory || []).slice(0, 1).forEach((id) => mine.add(id)); store.set("jarvis.mine", [...mine].slice(-300));
        local = local.filter((m) => m !== pending);
        await loadHistory();
        trace([...pre, [routed, "done"], [`answered in ${secs}s${res.voice?.source ? " · spoke via " + res.voice.source : ""}${res.plan ? " · plan ready in Tasks" : ""}`, "done"]]);
        announce("JARVIS answered: " + String(res.reply).slice(0, 240));
        if (state.case) loadCases();
      } else {
        if (current.message) live.turns.delete(current.message);
        local.push({ who: "jarvis", text: plainError(done.error), error: true, at: Date.now(), meta: ["failed", secs + "s"] });
        trace([...pre, [routed, "done"], ["failed", "fail"]]); announce("That failed: " + plainError(done.error));
      }
    } catch (e) {
      local.push({ who: "jarvis", text: plainError(e.message), error: true, at: Date.now() });
      trace([...pre, ["couldn't start", "fail"]]); announce("Couldn't start: " + plainError(e.message));
    } finally {
      clearInterval(ticker); current = null;
      setBusy(false); renderThread();
      refreshOverview(); loadMini();
      setTimeout(() => { if (!busy) trace([]); }, 9000);
    }
  }

  // ---------- microphone ----------
  const micBtn = $("#mic");
  let firstListen = true, spaceAt = 0;
  const mic = new Mic({ wave: $("#mic-wave"), onLevel: (l) => (Terrain.level = l), onAutoStop: () => stopMic() });
  async function startMic() {
    if (mic.active || mic.starting) return;
    if (busy) return toast("Still answering the last request. Talk again in a moment, or press Esc to stop waiting.");
    try {
      await mic.start();
      if (!mic.active) return;
      micBtn.setAttribute("aria-pressed", "true");
      trace([["listening · stops when you pause · Esc cancels", "active"]]); announce("Listening."); Terrain.redraw();
    } catch (e) { toast(e.name === "NotAllowedError" ? "Microphone permission was denied. Allow it for this page, then try again." : plainError(e.message)); }
  }
  async function stopMic(cancel) {
    if (!mic.active && !mic.starting) return;
    cancel = cancel || mic.starting;
    micBtn.setAttribute("aria-pressed", "false");
    const clip = await mic.stop();
    if (cancel) { trace([["cancelled", "fail"]]); setTimeout(() => trace([]), 2000); return; }
    if (!clip || !clip.audio) { trace([["didn't hear speech · check the mic input level", "fail"]]); setTimeout(() => trace([]), 3500); return; }
    micBtn.classList.add("busy");
    trace([[firstListen ? "transcribing · loading Whisper on the GPU, first time only" : "checking it's you · transcribing", "active"]]);
    try {
      const r = await api("/api/listen", new Uint8Array(clip.audio.buffer), true);
      firstListen = false;
      if (!r.owner) { trace([[`ignored · voice match ${r.score} is below your cutoff`, "fail"]]); toast("That didn't sound like you, so JARVIS ignored it. Re-run `jarvis voice enroll` if this keeps happening."); return; }
      if (!r.text) { trace([["heard you, but no words came through", "fail"]]); return; }
      prompt.value = r.text; autosize();
      await ask(r.text, r);
    } catch (e) { trace([["transcription failed", "fail"]]); toast(plainError(e.message)); }
    finally { micBtn.classList.remove("busy"); }
  }
  micBtn.addEventListener("click", () => (mic.active || mic.starting ? stopMic() : startMic()));
  // Space talks only when focus is on the page itself, so it never hijacks a link, summary or control.
  const pageFocused = () => { const a = document.activeElement; return !a || a === document.body || a === $("#stage") || a.classList?.contains("view"); };
  addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (mic.active || mic.starting) { e.preventDefault(); return stopMic(true); }
      if (busy) { stopWaiting(); return; }
      if (!$("#inspector").hidden && big) { big.select(null); return; }
      if (OVERLAY && !typing()) { hideOverlay(); return; }
    }
    if (typing() || e.metaKey || e.altKey) return;
    if (e.code === "Space" && !e.repeat && pageFocused()) { e.preventDefault(); spaceAt = Date.now(); startMic(); }
    else if (!e.ctrlKey && /^[1-9]$/.test(e.key) && pageFocused()) location.hash = VIEWS[+e.key - 1];
    else if (e.key === "/" || (e.key.toLowerCase() === "k" && e.ctrlKey)) { e.preventDefault(); prompt.focus(); }
  });
  // Hold Space to talk; a quick tap leaves it listening until you pause.
  addEventListener("keyup", (e) => { if (e.code === "Space" && (mic.active || mic.starting) && Date.now() - spaceAt > 400) stopMic(); });

  // ---------- live: one event stream for console, voice listener and CLI ----------
  // turn.* events stream every request as it happens, whichever channel it came from;
  // memory/task/plan/voice events say what changed so only that part reloads.
  const live = { connected: false, turns: new Map(), waiters: new Map(), seq: 0, es: null };
  const soon = (fn, ms) => { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; };
  const soonHistory = soon(loadHistory, 200), soonOverview = soon(refreshOverview, 500), soonMini = soon(loadMini, 1500);
  const soonMemory = soon(() => { if (!$("#view-memory").hidden) searchMemory(); }, 500);
  let drawQueued = false;
  const drawThread = () => { if (!drawQueued) { drawQueued = true; requestAnimationFrame(() => { drawQueued = false; renderThread(); }); } };
  let lastOverview = 0;
  function syncLive() {
    const c = $("#live-chip");
    c.classList.toggle("on", live.connected);
    c.querySelector(".chip-label").textContent = live.connected ? "Live" : "Reconnecting";
    c.title = live.connected ? "Live: console, voice listener and CLI requests stream here as they happen." : "Lost the live stream; retrying. The console falls back to polling meanwhile.";
  }
  const HANDLERS = {
    hello(d, e) {
      if (e.seq < live.seq) { live.turns.clear(); } // the server restarted: resync everything
      live.seq = e.seq; state.brain = d.brain;
      loadHistory(); soonOverview();
      emit("vault", { resync: true }); // notes may have changed while disconnected
    },
    // A note changed, in Obsidian or by JARVIS; the to-do board refetches (dashboard.js).
    "vault.changed"(d) { emit("vault", d); },
    "brain.state"(d) { state.brain = d; if (state.overview) renderRouter(state.overview); },
    "turn.start"(d, e) {
      if (!d.message) return;
      const t = { channel: d.channel, job: d.job, text: d.text, tier: d.tier, reasons: d.reasons, sentences: [],
                  at: e.at * 1000, voice: d.channel === "voice", done: false };
      live.turns.set(d.message, t);
      if (current && d.job === current.id) adopt(d.message, t);
      else if (d.channel !== "console") announce(`${CHANNEL[d.channel] || d.channel}: ${d.text}`.slice(0, 200));
      drawThread();
    },
    "turn.sentence"(d) {
      const t = live.turns.get(d.message); if (t) { t.sentences.push(d.text); drawThread(); }
      if (current && d.job === current.id) current.answering = true;
    },
    "turn.done"(d) {
      const t = d.message && live.turns.get(d.message);
      if (t) Object.assign(t, { done: true, reply: d.reply, seconds: d.seconds, plan: d.plan, tier: d.tier || t.tier });
      if (d.job && live.waiters.has(d.job)) api("/api/jobs/" + d.job).then((j) => finishWait(d.job, j)).catch(() => {});
      drawThread(); soonHistory(); soonOverview(); soonMini();
    },
    "turn.error"(d) {
      const t = d.message && live.turns.get(d.message);
      if (t) Object.assign(t, { done: true, error: true, reply: plainError(d.error), seconds: d.seconds });
      if (d.job && live.waiters.has(d.job)) api("/api/jobs/" + d.job).then((j) => finishWait(d.job, j)).catch(() => {});
      drawThread();
    },
    "memory.added"(d) {
      if (d.kind === "message" || d.kind === "reply") soonHistory();
      soonOverview(); soonMini(); soonMemory();
      if (d.kind === "case") loadCases();
    },
    "task.event"() { soonOverview(); },
    "plan.updated"(d) { soonOverview(); if (openPlanId === d.id && !$("#view-tasks").hidden) openPlan(d.id, true); },
    "voice.state"(d) {
      if (state.overview) { state.overview.voice.listening = d.listening; renderListener(state.overview); }
      // A listener killed mid-answer never sends turn.done.
      if (!d.listening) for (const t of live.turns.values()) if (t.channel === "voice" && !t.done)
        Object.assign(t, { done: true, error: true, reply: "The listener stopped before it finished answering." });
      drawThread();
      soonOverview();
    },
    "voice.ready"() { soonOverview(); },
    "voice.heard"() { soonOverview(); },
    "voice.ignored"() { soonOverview(); },
  };
  function connectLive() {
    if (!window.EventSource) return;
    const es = live.es = new EventSource("/api/events");
    es.addEventListener("open", () => { live.connected = true; syncLive(); }); // "hello" follows and resyncs
    es.addEventListener("error", () => { live.connected = false; syncLive(); }); // EventSource retries by itself
    es.addEventListener("message", (ev) => {
      let e; try { e = JSON.parse(ev.data); } catch (x) { return; }
      if (e.kind !== "hello") live.seq = Math.max(live.seq, e.seq);
      try { HANDLERS[e.kind]?.(e.data || {}, e); } catch (x) { console.error("live event", e.kind, x); }
      emit("live", e); // dashboard.js: the agent tile follows turns and the listener
    });
  }

  // ---------- route preview: what will happen, decided the way the server will ----------
  let previewTimer, previewSeq = 0;
  function preview() {
    clearTimeout(previewTimer);
    const text = prompt.value.trim();
    if (busy || mic.active) return;
    if (text.length < 3) { if ($("#trace .step.preview")) trace([]); return; }
    previewTimer = setTimeout(async () => {
      const seq = ++previewSeq;
      try {
        const r = await api("/api/route?" + new URLSearchParams({ text, tier: state.tier || "" }));
        if (seq !== previewSeq || busy || mic.active || !prompt.value.trim()) return;
        trace([[`Enter sends to ${routeLabel(r)}`, "preview"]]);
      } catch (e) {}
    }, 300);
  }
  prompt.addEventListener("input", preview);
  tierBtns.forEach((b) => b.addEventListener("click", preview));
  $("#tier-select").addEventListener("change", preview);

  // ---------- overlay mode (python -m jarvis overlay) ----------
  function hideOverlay() { api("/api/overlay", { action: "hide" }).catch((e) => toast(plainError(e.message))); }
  if (OVERLAY) {
    document.documentElement.classList.add("is-overlay");
    const chip = document.createElement("button");
    chip.type = "button"; chip.className = "chip overlay-chip";
    chip.title = `Hide the overlay (${OVERLAY_KEY} or Esc). ${OVERLAY_KEY} brings it back from any app.`;
    chip.innerHTML = `<span class="kbd">${esc(OVERLAY_KEY)}</span> hide`;
    chip.addEventListener("click", hideOverlay);
    $(".topbar-right").prepend(chip);
  }

  window.Jarvis = { openTask, openPlan, api, esc, icon, ago, tierName, words, toast, state };

  // ---------- boot ----------
  renderThread(); show(); syncLive();
  loadCases();
  refreshOverview().then(() => { loadHistory(); show(); });
  refreshCredits(false); loadMini();
  connectLive();
  // The live stream carries changes; these are the safety net if it drops.
  setInterval(() => { if (!live.connected || Date.now() - lastOverview > 60000) refreshOverview(); }, 20000);
  setInterval(() => { if (!live.connected || Date.now() - lastHistory > 120000) loadHistory(); }, 30000);
  setInterval(() => refreshCredits(false), 120000);
  refreshProbe(false);
  setInterval(() => { if (!document.hidden) refreshProbe(false); }, 60000); // the server caches for 55 s
  setInterval(() => { if (state.probe) $("#probe-checked").textContent = "checked " + ago(state.probe.checked_at); }, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.probe && Date.now() / 1000 - state.probe.checked_at > 60) refreshProbe(false); });
})();
