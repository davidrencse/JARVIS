(function () {
  "use strict";

  // ---------- helpers ----------
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = window.JarvisEsc;
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
  // dashboard.js draws the dashboard from these events.
  const emit = (name, detail) => dispatchEvent(new CustomEvent("jarvis:" + name, { detail }));
  const QUERY = new URLSearchParams(location.search);
  const OVERLAY = QUERY.has("overlay"), OVERLAY_KEY = QUERY.get("key") || "Alt+1";
  const state = { overview: null, credits: null, tier: "", speak: store.get("jarvis.speak", false), cases: [],
                  case: store.get("jarvis.case", "") };

  // ---------- views ----------
  // Order is the rail's own order, so the number keys read top to bottom.
  const VIEWS = ["console", "graph", "system", "credits", "memory", "run", "files", "devices", "settings"];
  const loaded = {};
  async function show() {
    // The graph view gave way to the knowledge engine page; old links and key 2 go there.
    if (location.hash === "#graph") { location.replace("knowledge.html"); return; }
    const name = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "console";
    for (const v of VIEWS) $("#view-" + v).hidden = v !== name;
    $("#case-switch").hidden = name === "console";
    $$(".rail a[data-view]").forEach((a) => { if (a.dataset.view === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    $("#crumb-view").textContent = $("#view-" + name).dataset.title;
    // The overlay controller finds its window by this exact title, so it never changes there.
    document.title = OVERLAY ? "JARVIS Overlay" : `JARVIS · ${$("#view-" + name).dataset.title}`;
    emit("view", name);
    const load = { graph: loadGraph, credits: () => renderCreditTable(state.credits),
                   memory: loadMemory, devices: renderDevices, files: () => window.JarvisFiles?.open(),
                   run: () => window.JarvisRun?.open(), system: (a) => window.JarvisSystem?.open(a) }[name];
    if (load) { try { if ((await load(loaded[name])) !== false) loaded[name] = true; } catch (e) { toast(plainError(e.message)); } }
  }
  addEventListener("hashchange", show);
  $("#voice-btn").addEventListener("click", () => { location.hash = "settings"; });

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
    list.filter((r) => r.group !== "Model APIs").forEach((r) => (groups[r.group] = groups[r.group] || []).push(r));  // pay-per-token APIs are off-limits
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
    renderInputSettings();
    $("#top-status").innerHTML = [
      `<a class="chip" href="#memory">${icon("database")} ${o.memory.items} in memory</a>`,
      `<span class="chip">${icon("audio-lines")} ${o.voice.voiceprint ? "voiceprint enrolled" : "no voiceprint"}</span>`,
    ].join("");
    lastOverview = Date.now();
    const heardLines = (o.voice.recent || []).filter((l) => !/ignored another voice/.test(l)).slice(-5).map((l) => {
      const m = l.match(/^(\S+ \S+)\s+(.*)$/); const text = m ? m[2] : l;
      return `<div title="${esc(l)}"><b>${esc(m ? m[1].slice(11, 16) : "")}</b> ${esc(text)}</div>`;
    }).join("");
    const ign = o.voice.ignored || {};
    const ignLine = ign.count ? `<div class="muted" title="Voices that didn't match your voiceprint are dropped unread">${ign.count} other voice${ign.count === 1 ? "" : "s"} ignored${ign.last ? ` · last ${esc(ign.last.slice(11, 16))}` : ""}</div>` : "";
    $("#listener-log").innerHTML = heardLines + ignLine || (o.voice.listening ? "Listening. Nothing heard yet; start with “Jarvis…”." : "Listener is off.");
    const auto = $(".dock [data-tier='']");
    auto.title = o.routing.auto_escalate ? "Let the router pick from your words" : `Conversation: the ${o.brain?.name || "ChatGPT"} brain answers. Pick Tools or Plan for more.`;
    renderRouter(o); renderMachines(o); renderHeapFacts(o);
    if (!$("#view-devices").hidden) renderDevices();
    loaded.console = true;
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
    if (live.local) return !live.lan_mouse || live.lan_mouse.running ? `<span class="state-word ok">this device</span>` : `<span class="state-word warn">Lan Mouse off</span>`;
    if (!live.reachable) return `<span class="state-word warn" title="${esc(live.error || "")}">unreachable</span>`;
    if (live.ssh === false) return `<span class="state-word warn" title="${esc(live.error || "")}">ssh failed</span>`;
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
    } else if (live.via === "node") {
      rows.push(["Platform", `${live.platform || "—"} · v${live.version || "—"}`]);
      if (live.seen) rows.push(["Checked in", `${Math.max(0, Math.round(Date.now() / 1000 - live.seen))} s ago`]);
    } else if (live.error) rows.push(["Error", live.error]);
    return `<dl class="facts mono live-facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("")}</dl>`;
  }
  // Live Lan Mouse status moved to the Devices view; Settings > Input only keeps the peripheral list.
  function renderInputSettings() { renderPeripheralInventory(); }
  let peripheralInventory = store.get("jarvis.input.peripherals", []);
  if (!Array.isArray(peripheralInventory)) peripheralInventory = [];
  function renderPeripheralInventory() {
    const select = $("#input-peripheral-device"), list = $("#input-peripheral-list");
    if (!select || !list) return;
    const current = select.value;
    const devices = state.overview?.devices || [];
    select.innerHTML = `<option value="">Unassigned</option>` + devices.map((d) =>
      `<option value="${esc(d.id)}">${esc(d.hostname || d.label || words(d.id))}</option>`).join("");
    if (devices.some((d) => d.id === current)) select.value = current;
    list.innerHTML = peripheralInventory.length ? peripheralInventory.map((item) => {
      const device = devices.find((d) => d.id === item.device);
      return `<li><div><b>${esc(item.name)}</b><span>${esc(item.type)}${device ? ` · ${esc(device.hostname || device.label || device.id)}` : " · Unassigned"}</span></div>
        <button class="btn ghost peripheral-remove" type="button" data-id="${esc(item.id)}" aria-label="Remove ${esc(item.name)}">Remove</button></li>`;
    }).join("") : `<li class="peripheral-empty">No peripherals added yet.</li>`;
  }
  $("#input-peripheral-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget, data = new FormData(form), name = String(data.get("name") || "").trim();
    if (!name) return;
    const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    peripheralInventory.push({ id, name, type: String(data.get("type") || "Other"), device: String(data.get("device") || "") });
    store.set("jarvis.input.peripherals", peripheralInventory);
    form.reset(); renderPeripheralInventory(); announce(`${name} added to the peripheral list.`);
  });
  $("#input-peripheral-list").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-id]");
    if (!button) return;
    peripheralInventory = peripheralInventory.filter((item) => item.id !== button.dataset.id);
    store.set("jarvis.input.peripherals", peripheralInventory);
    renderPeripheralInventory(); announce("Peripheral removed.");
  });
  function renderProbe() {
    const p = state.probe;
    $("#probe-checked").textContent = p ? "checked " + ago(p.checked_at) : "checking…";
    renderInputSettings();
    if (!p) return;
    const [label, cls] = LEVEL[p.input_link.level] || LEVEL.warn;
    $("#link-status").innerHTML = `<div class="link-verdict lvl-${esc(p.input_link.level)}">${icon("keyboard", "i")}<b>Input sharing</b><span class="state-word ${cls}">${label}</span>
      <ul>${p.input_link.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>`;
    if (state.overview) { renderMachines(state.overview); if (!$("#view-devices").hidden) renderDevices(); }
  }
  let probing = false;
  async function refreshProbe(force) {
    if (probing) return;
    probing = true;
    const buttons = [$("#probe-refresh")].filter(Boolean);
    buttons.forEach((button) => { button.disabled = true; });
    if (force) $("#probe-checked").textContent = "checking…";
    try { state.probe = await api("/api/probe" + (force ? "?refresh=1" : "")); renderProbe(); emit("probe", state.probe); if (force) announce("Machines checked: " + state.probe.input_link.lines[0]); }
    catch (e) {
      $("#probe-checked").textContent = "check failed";
      $("#link-status").innerHTML = `<div class="empty"><b>Couldn't run the live check.</b>${esc(plainError(e.message))}</div>`;
    }
    finally { probing = false; buttons.forEach((button) => { button.disabled = false; }); }
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

  // ---------- devices: live connections ----------
  // Computers (node links from the broker) and this computer's listener and drop folder
  // come from /api/connections (~20 ms, no network); hardware from the overview's
  // inventory. Lan Mouse, clipboard and pairing are peripherals.js ("Connect & share"),
  // drawn between #devices-top and #devices-grid. Re-read every 8 s while visible.
  const conn = { data: null, error: "", busy: new Set(), timer: 0 };
  async function loadConnections() {
    try { conn.data = await api("/api/connections"); conn.error = ""; }
    catch (e) { conn.error = e.message; }
    if (!$("#view-devices").hidden) renderDevices();
  }
  function pollConnections() {
    clearTimeout(conn.timer);
    conn.timer = setTimeout(async () => {
      if ($("#view-devices").hidden) return;
      if (document.visibilityState === "visible") await loadConnections();
      pollConnections();
    }, 8000);
  }
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const pill = (text, cls = "", tip = "") => `<span class="cx-pill ${cls}"${tip ? ` title="${esc(tip)}"` : ""}>${esc(text)}</span>`;
  const dot = (on) => `<i class="cx-dot ${on === true ? "on" : on === false ? "off" : ""}" aria-hidden="true"></i>`;
  function silent(s) {
    if (s == null) return "never";
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }
  function btn(target, part, action, label, opts = {}) {
    const key = `${target}:${part}:${action}`;
    const busy = conn.busy.has(key);
    return `<button type="button" class="cx-btn${opts.strong ? " strong" : ""}" data-cx="${esc(key)}"${opts.off || busy ? " disabled" : ""}${opts.tip ? ` title="${esc(opts.tip)}"` : ""}>${busy ? `<i class="cx-spin" aria-hidden="true"></i>` : opts.icon ? icon(opts.icon) : ""}<span>${esc(label)}</span></button>`;
  }
  function hwLine(d) { return [d.model, d.cpu, d.ram, d.gpu].filter(Boolean).map(esc).join(" · "); }
  function hwDetails(d) {
    if (!d) return "";
    const tools = Object.entries(d.tools || {}).map(([k, v]) => `<span class="cx-tool ${v ? "ok" : ""}" title="${v ? "found on PATH" : "not on PATH when the inventory was taken"}">${esc(k)}</span>`).join("");
    return `<details class="cx-more"><summary>Hardware and tools</summary>
      <dl class="cx-facts"><dt>Model</dt><dd>${esc(d.model || "—")}</dd><dt>CPU</dt><dd>${esc(d.cpu || "—")}</dd><dt>Memory</dt><dd>${esc(d.ram || "—")}</dd><dt>GPU</dt><dd>${esc(d.gpu || "—")}</dd>
      <dt>Address</dt><dd>${esc(d.address || "—")}</dd><dt>Inventory</dt><dd>${esc(ago(d.collected_at))}</dd></dl>
      ${tools ? `<div class="cx-tools">${tools}</div>` : ""}</details>`;
  }

  function hubCard(c, here, inv) {
    const d = inv[c.local] || {};
    return `<article class="cx-node cx-hub" aria-label="${esc(c.local)}, the main PC">
      <header>${icon("laptop-minimal", "i")}<div><h3>${esc(c.local)}</h3><p>${esc(d.label || d.hostname || "")}</p></div>${pill(c.role === "main" ? "main · this PC" : "this PC", "ok")}</header>
      <p class="cx-line">${dot(true)}Hub running${c.code ? ` · code <span class="mono">${esc(c.code.slice(0, 8))}</span>` : ""}</p>
      ${hwLine(d) ? `<p class="cx-sub">${hwLine(d)}</p>` : ""}
      ${hwDetails(d)}</article>`;
  }

  function nodeCard(n, inv) {
    const d = inv[n.id] || {};
    const jobs = n.jobs_24h || {};
    const failed = (jobs.failed || 0) + (jobs.expired || 0) + (jobs.uncertain || 0);
    const jobLine = [jobs.done ? `${jobs.done} done` : "", failed ? `${failed} failed` : "", n.running ? `${n.running} running` : ""].filter(Boolean).join(" · ") || "no jobs";
    let hint = "";
    if (!n.online) hint = `Its node agent isn't polling the main. On ${esc(n.hostname || n.id)}, start JARVIS's node agent (or <code>jarvis cluster doctor</code>).`;
    else if (!n.code_ok) hint = "Runs older code. It pulls the main's code by itself on its next poll and restarts.";
    return `<article class="cx-node ${n.online ? "on" : "off"}" aria-label="${esc(n.id)}, node">
      <header>${icon("laptop-minimal", "i")}<div><h3>${esc(n.id)}</h3><p>${esc(n.label !== n.id ? n.label : n.hostname || "")}</p></div>
        ${pill(n.online ? "online" : "offline", n.online ? "ok" : "warn", n.seen ? "last heard " + new Date(n.seen * 1000).toLocaleString() : "")}</header>
      <p class="cx-line">${dot(n.online)}${n.online ? "Polling now" : "Last heard " + silent(n.silent_for)}${n.platform ? ` · ${esc(n.platform)}` : ""}${n.hostname ? ` · <span class="mono">${esc(n.hostname)}</span>` : ""}</p>
      <div class="cx-chips">${pill(n.code_ok ? "same code" : "older code", n.code_ok ? "ok" : "warn", "node code " + (n.code || "unknown"))}${pill("24 h: " + jobLine, failed ? "warn" : "")}</div>
      <div class="cx-caps" aria-label="What the main may ask it">${n.capabilities.map((c) => `<span title="${esc(c.does)}">${esc(c.id)}</span>`).join("")}</div>
      ${hint ? `<p class="cx-hint">${hint}</p>` : ""}
      <div class="cx-actions"><button type="button" class="cx-btn" data-cx-screen="${esc(n.id)}"${n.online ? "" : " disabled"}>${icon("monitor")}<span>Screen</span></button>
        <button type="button" class="cx-btn" data-cx-send="${esc(n.id)}">${icon("arrow-up-right")}<span>Send files</span></button></div>
      ${hwDetails(d)}</article>`;
  }

  function linksPanel(o, inv) {
    const c = conn.data?.cluster;
    if (!c) return `<section class="panel cx-panel cx-links"><p class="cx-empty">${conn.error ? "Couldn't read connections: " + esc(conn.error) : "Reading connections…"}</p></section>`;
    if (!c.enabled) return `<section class="panel cx-panel cx-links"><header class="cx-h"><h2>Computers</h2></header>
      <p class="cx-empty">The cluster isn't set up, so this computer works alone. On the main PC run <code>jarvis cluster init</code>, then <code>jarvis cluster invite NAME</code>.</p></section>`;
    const nodes = c.nodes || [];
    const online = nodes.filter((n) => n.online).length;
    return `<section class="panel cx-panel cx-links" aria-labelledby="cx-links-h">
      <header class="cx-h"><h2 id="cx-links-h">Computers</h2><span class="cx-meta">${plural(nodes.length, "node")} · ${online} online</span></header>
      <div class="cx-map">${hubCard(c, conn.data.here, inv)}
        <div class="cx-wires" aria-hidden="true">${nodes.map((n) => `<i class="${n.online ? "on" : "off"}"></i>`).join("")}</div>
        <div class="cx-nodes">${nodes.map((n) => nodeCard(n, inv)).join("") ||
          `<p class="cx-empty">No node PCs yet. Run <code>jarvis cluster invite NAME</code> here, then <code>jarvis cluster join CODE</code> on the other computer.</p>`}</div></div></section>`;
  }

  function tile(id, iconName, title, on, line, sub, buttons, extra = "") {
    return `<article class="cx-tile ${on === true ? "on" : on === false ? "off" : ""}" aria-labelledby="cx-t-${id}">
      <header>${icon(iconName, "i")}<h3 id="cx-t-${id}">${esc(title)}</h3>${on == null ? "" : pill(on ? "on" : "off", on ? "ok" : "")}</header>
      <p class="cx-line">${line}</p>${sub ? `<p class="cx-sub">${sub}</p>` : ""}${extra}
      <div class="cx-actions">${buttons}</div></article>`;
  }

  function herePanel() {
    const h = conn.data?.here;
    if (!h) return "";
    const v = h.voice || {}, drop = h.drop || {};
    const voice = v.error ? tile("voice", "mic", "Voice listener", null, esc(v.error), "", "") :
      tile("voice", "mic", "Voice listener", v.running,
        v.running ? `Listening · pid <span class="mono">${esc(v.pid)}</span>` : v.enrolled ? "Not listening" : "No voiceprint yet: set up voice in Settings",
        v.supervised ? "Restarts by itself if it crashes; starts at login." : "Not supervised. Turn on autostart to have it restarted after a crash.",
        v.running ? btn("here", "voice", "restart", "Restart", { icon: "rotate-ccw" }) + btn("here", "voice", "stop", "Stop")
                  : btn("here", "voice", "start", "Start", { strong: true, off: !v.enrolled }),
        (v.recent || []).length ? `<p class="cx-answer bad">${esc(v.recent.at(-1))}</p>` : "");
    const files = (drop.latest || []).map((f) => `<li><span>${esc(f.name)}${f.dir ? "/" : ""}</span><small class="mono">${f.dir ? "folder" : compact(f.bytes) + " B"} · ${esc(ago(f.at))}</small></li>`).join("");
    const inbox = drop.error ? tile("drop", "download", "Received files", null, esc(drop.error), "", "") :
      tile("drop", "download", "Received files", null,
        drop.count ? `${plural(drop.count, "item")} in <span class="mono">${esc(drop.folder)}</span>` : `Nothing yet in <span class="mono">${esc(drop.folder)}</span>`,
        "", "", files ? `<ul class="cx-files">${files}</ul>` : "");
    return `<section class="panel cx-panel cx-here" aria-labelledby="cx-here-h">
      <header class="cx-h"><h2 id="cx-here-h">On this computer${h.device ? ` <span class="cx-meta">${esc(h.device)}</span>` : ""}</h2></header>
      <div class="cx-tiles">${voice}${inbox}</div></section>`;
  }

  function sendPanel(o) {
    const targets = o.devices.filter((d) => d.send);
    const pick = targets.find((d) => d.id === store.get("jarvis.dropdevice", "")) || targets[0];
    return `<section class="panel cx-panel cx-send" aria-labelledby="cx-send-h"><header class="cx-h"><h2 id="cx-send-h">Send files</h2></header>
      ${pick ? `<select class="field" id="drop-device" aria-label="Send to">${targets.map((d) =>
        `<option value="${esc(d.id)}" ${d === pick ? "selected" : ""}>To ${esc(d.id)}${d.hostname ? " (" + esc(d.hostname) + ")" : ""}</option>`).join("")}</select>
      <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Drop files here or press Enter to choose">
        <p><b>Drop files or images here</b></p><p class="muted">or click to choose. They go over the cluster link and land in <span class="mono">~/JARVIS-Drop</span> on the chosen computer.</p>
        <input type="file" id="drop-input" multiple hidden></div>
      <ul class="drop-log mono" id="drop-log" aria-live="polite"></ul>`
      : `<p class="cx-empty">No other computer to send to. Add a node PC to the cluster first.</p>`}</section>`;
  }

  function agentsPanel(o) {
    const rows = Object.entries(o.agents || {});
    if (!rows.length) return "";
    return `<section class="panel cx-panel cx-agents" aria-labelledby="cx-agents-h"><header class="cx-h"><h2 id="cx-agents-h">Personal agents</h2><span class="cx-meta">from jarvis.config.json</span></header>
      <ul class="cx-list">${rows.map(([k, v]) => { const [label, cls] = linkState(v.status); return `<li><b>${esc(k)}</b><span>${esc(words(v.integration))}</span>${pill(label, cls, words(v.status))}</li>`; }).join("")}</ul></section>`;
  }

  function summary() {
    const d = conn.data; if (!d) return "";
    const nodes = d.cluster?.nodes || [], h = d.here || {};
    const parts = [
      [nodes.length ? `${nodes.filter((n) => n.online).length}/${nodes.length} nodes online` : "no nodes", nodes.length && nodes.every((n) => n.online)],
      ["voice listener " + (h.voice?.running ? "on" : "off"), h.voice?.running],
      [(h.drop?.count || 0) + " received", null]];
    return parts.map(([t, ok]) => `<span>${ok == null ? "" : dot(!!ok)}${esc(t)}</span>`).join("");
  }

  function renderDevices() {
    const o = state.overview; if (!o) return false;
    const inv = Object.fromEntries((o.devices || []).map((d) => [d.id, d]));
    const open = new Set($$("#view-devices details.cx-more[open]").map((el) => el.closest("[aria-label]")?.getAttribute("aria-label")));
    $("#devices-top").innerHTML = `
      <header class="cx-top"><p class="cx-summary" aria-live="polite">${summary()}</p>
        <div class="cx-top-actions"><span class="cx-meta">${conn.data ? "checked " + ago(conn.data.checked_at) : conn.error ? "not reachable" : "checking…"}</span>
        <button type="button" class="cx-btn" id="cx-refresh">${icon("refresh-cw")}<span>Check now</span></button></div></header>
      ${linksPanel(o, inv)}`;
    $("#devices-grid").innerHTML = `${herePanel()}
      <div class="cx-pair">${screenPanel(o)}${sendPanel(o)}</div>
      ${agentsPanel(o)}`;
    $$("#view-devices details.cx-more").forEach((el) => { if (open.has(el.closest("[aria-label]")?.getAttribute("aria-label"))) el.open = true; });
    wireDropzone(); wireScreen(); wireConnections();
    if (!conn.data && !conn.error && !conn.loading) { conn.loading = true; loadConnections().finally(() => (conn.loading = false)); }
    pollConnections();
  }

  function wireConnections() {
    $("#cx-refresh")?.addEventListener("click", () => { conn.data = null; renderDevices(); loadConnections(); });
    $$("#devices-grid [data-cx]").forEach((b) => b.addEventListener("click", async () => {
      const key = b.dataset.cx, [target, part, action] = key.split(":");
      conn.busy.add(key); renderDevices();
      try {
        const r = await api("/api/connections", { target, part, action });
        const note = r.result?.note; if (note) toast(note); else announce(`${words(part)}: ${action} done`);
      } catch (e) { toast(plainError(e.message)); } finally { conn.busy.delete(key); await loadConnections(); }
    }));
    $$("#view-devices [data-cx-screen]").forEach((b) => b.addEventListener("click", () => {
      screenView.device = b.dataset.cxScreen; store.set("jarvis.scrdevice", screenView.device);
      renderDevices(); $(".screen-panel")?.scrollIntoView({ behavior: "smooth", block: "start" }); snapScreen();
    }));
    $$("#view-devices [data-cx-send]").forEach((b) => b.addEventListener("click", () => {
      store.set("jarvis.dropdevice", b.dataset.cxSend);
      const sel = $("#drop-device"); if (sel) sel.value = b.dataset.cxSend;
      $("#dropzone")?.scrollIntoView({ behavior: "smooth", block: "center" }); $("#dropzone")?.focus({ preventScroll: true });
    }));
  }

  // ---------- screens ----------
  // Read-only snapshots through the `screen` desktop action. Each device keeps its last
  // picture; Live re-takes one every few seconds while Devices is open and the tab visible.
  const screenView = { device: store.get("jarvis.scrdevice", ""), live: false, shots: {}, busy: new Set(), error: "", timer: 0, seen: false };
  function reachable(id) {
    const c = conn.data?.cluster;
    if (!c) return !!state.overview?.devices?.find((d) => d.id === id)?.here; // not read yet
    if (!c.enabled || id === c.local) return true;
    return !!(c.nodes || []).find((n) => n.id === id)?.online;
  }
  function screenPanel(o) {
    const d = o.devices.find((x) => x.id === screenView.device) || o.devices.find((x) => !x.here && reachable(x.id))
      || o.devices.find((x) => x.here) || o.devices[0];
    if (!d) return "";
    screenView.device = d.id;
    return `<article class="panel ticks screen-panel"><header class="ph">${icon("monitor")}<h2>Screens</h2><span class="ph-meta mono">view only</span></header>
      <div class="scr-bar">
        <div class="scr-tabs" role="tablist" aria-label="Whose screen">${o.devices.map((x) =>
          `<button type="button" role="tab" data-dev="${esc(x.id)}" aria-selected="${x.id === d.id}">${icon("laptop-minimal")}<span>${esc(x.id)}</span>${x.here ? `<small>this device</small>` : ""}</button>`).join("")}</div>
        <div class="scr-actions">
          <button class="scr-btn" type="button" id="scr-live" aria-pressed="${screenView.live}"><i aria-hidden="true"></i>Live</button>
          <button class="scr-btn" type="button" id="scr-refresh">${icon("refresh-cw")}<span>Snapshot</span></button>
          <button class="scr-btn" type="button" id="scr-full" aria-label="Full screen">${icon("arrow-up-right")}</button></div></div>
      <div class="scr-frame" id="scr-frame"></div></article>`;
  }
  function paintScreen() {
    const frame = $("#scr-frame"); if (!frame) return;
    const shot = screenView.shots[screenView.device];
    const busy = screenView.busy.has(screenView.device);
    frame.classList.toggle("busy", busy);
    frame.innerHTML = (shot ? `<img src="${shot.url}" alt="Screen of ${esc(screenView.device)}">` : "") +
      (screenView.error ? `<div class="scr-empty"><b>Couldn't get ${esc(screenView.device)}'s screen</b><p>${esc(screenView.error)}</p><button class="scr-btn" type="button" data-scr-snap>Try again</button></div>`
        : !shot ? `<div class="scr-empty">${busy ? `<b>Capturing ${esc(screenView.device)}…</b>`
          : `<b>${esc(screenView.device)}</b><button class="scr-btn" type="button" data-scr-snap>${icon("monitor")}<span>Show its screen</span></button>`}</div>` : "") +
      (shot ? `<span class="scr-stamp mono">${screenView.live ? `<i aria-hidden="true"></i>` : ""}${esc(screenView.device)} · ${esc(shot.at)}${shot.w ? ` · ${shot.w}×${shot.h}` : ""}${busy ? " · updating…" : ""}</span>` : "");
    frame.querySelector("[data-scr-snap]")?.addEventListener("click", snapScreen);
  }
  async function snapScreen() {
    const device = screenView.device;
    if (screenView.busy.has(device)) return;
    screenView.busy.add(device); screenView.error = ""; paintScreen();
    try {
      const res = await fetch("/api/screen?device=" + encodeURIComponent(device));
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
      // A data: URL, not blob: — the console's CSP allows only 'self' and data: images.
      const blob = await res.blob();
      const url = await new Promise((ok, fail) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = () => fail(r.error); r.readAsDataURL(blob); });
      const img = new Image(); img.src = url;
      await img.decode().catch(() => { throw new Error("The picture that came back can't be shown."); });
      screenView.shots[device] = { url, at: new Date().toLocaleTimeString(), w: img.naturalWidth, h: img.naturalHeight };
    } catch (e) {
      if (device === screenView.device) screenView.error = e.message;
      setLive(false);
    } finally {
      screenView.busy.delete(device);
      if (device === screenView.device) paintScreen();
    }
  }
  function setLive(on) {
    screenView.live = on; clearTimeout(screenView.timer);
    const gen = screenView.gen = (screenView.gen || 0) + 1;  // only the newest chain may run
    $("#scr-live")?.setAttribute("aria-pressed", String(on));
    if (!on) return paintScreen();
    const tick = async () => {
      if (!screenView.live || gen !== screenView.gen) return;
      if (!$("#view-devices").hidden && document.visibilityState === "visible") await snapScreen();
      if (screenView.live && gen === screenView.gen) screenView.timer = setTimeout(tick, 3000);
    };
    tick();
  }
  function wireScreen() {
    if (!$("#scr-frame")) return;
    paintScreen();
    $$(".scr-tabs button").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.dev === screenView.device) return;
      screenView.device = b.dataset.dev; screenView.error = ""; store.set("jarvis.scrdevice", b.dataset.dev);
      $$(".scr-tabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      paintScreen(); snapScreen();
    }));
    $("#scr-refresh").addEventListener("click", snapScreen);
    $("#scr-live").addEventListener("click", () => setLive(!screenView.live));
    $("#scr-full").addEventListener("click", () => $("#scr-frame").requestFullscreen?.().catch(() => {}));
    // First visit to Devices: show the chosen screen without waiting for a click.
    // Only a reachable one: a capture of an offline node hangs until it times out and holds
    // one of the browser's few connections to the console meanwhile.
    if (!screenView.seen && !$("#view-devices").hidden && reachable(screenView.device)) { screenView.seen = true; snapScreen(); }
  }

  // Files go over the existing SSH link: a drag can't cross from one OS's desktop to another's.
  function wireDropzone() {
    const zone = $("#dropzone"), input = $("#drop-input"), log = $("#drop-log"), target = $("#drop-device");
    if (!zone) return;
    target.addEventListener("change", () => store.set("jarvis.dropdevice", target.value));
    const line = (text, cls) => { const li = document.createElement("li"); li.className = cls || ""; li.textContent = text; log.prepend(li); return li; };
    async function sendAll(files) {
      for (const file of files) {
        const li = line(`Sending ${file.name} …`, "pending");
        try {
          const r = await api("/api/send?name=" + encodeURIComponent(file.name) + "&device=" + encodeURIComponent(target.value), file, true);
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
  // What a reply cost (usage.py): tokens, API-equivalent dollars (notional: JARVIS runs on
  // subscriptions) and the share of the Codex 5-hour and weekly windows it used.
  const pct = (v) => v == null ? "?" : v === 0 ? "0" : v >= 1 ? v.toFixed(1) : v >= 0.1 ? v.toFixed(2) : v < 0.0001 ? "<0.0001" : String(+v.toPrecision(2));
  function usageChip(u) {
    if (!u) return "";
    if (!u.calls) return `<div class="msg-usage" title="Answered locally (parser, clock or memory): no model call, nothing spent."><b>free</b><span>0 tokens · local</span></div>`;
    const w = u.windows || {}, usd = u.usd < 0.0001 ? "<$0.0001" : "$" + u.usd.toFixed(u.usd < 0.01 ? 4 : 3);
    const tip = `${u.engine} · ${u.calls} call${u.calls === 1 ? "" : "s"} · ${(u.input || 0).toLocaleString()} new in, ${(u.cached || 0).toLocaleString()} cached, ${(u.output || 0).toLocaleString()} out. `
      + `${usd} is what these tokens would cost at API prices; your subscription is what you actually pay. Window shares are estimated from your Codex history.`;
    return `<div class="msg-usage" title="${esc(tip)}"><b>${pct(w["5h"])}%</b><span>of 5 h</span><b>${pct(w.week)}%</b><span>of week</span>`
      + `<span class="sep">·</span><span>${u.tokens.toLocaleString()} tok</span><span>≈ ${usd}</span></div>`;
  }
  const liveReply = (t, tier) => ({ who: "jarvis", text: t.done ? t.reply : t.sentences.join(" "), at: t.at + 1, tier: t.tier || tier, usage: t.usage,
    streaming: !t.done, error: t.error, meta: t.done ? [t.seconds != null ? t.seconds + "s" : null, t.plan || null].filter(Boolean) : [t.sentences.length ? "answering" : "thinking"] });
  function renderThread() {
    const el = $("#thread");
    const rows = [], known = new Set();
    for (const h of history) {
      known.add(h.id);
      rows.push({ who: "you", text: h.text, at: stamp(h.at), tier: h.tier, channel: channelOf(h), voice: live.turns.get(h.id)?.voice });
      if (h.reply) rows.push({ who: "jarvis", text: h.reply.body, at: stamp(h.reply.at), tier: h.tier, plan: h.reply.meta?.plan, usage: h.reply.meta?.usage,
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
        <p>Type below, or say “Jarvis, …”: the mic is always on, but only requests that start with “Jarvis” are sent (the mic button turns it off). Quick asks run one cheap model call; big jobs get a specialist plan you review first.</p>
        <div class="suggest">${["What did I decide about the clipboard?", "Summarize what's in memory about Lan Mouse", "Plan a recon of my home lab's exposed services"].map((s) => `<button type="button">${esc(s)}</button>`).join("")}</div></li>`;
      $$(".suggest button", el).forEach((b) => b.addEventListener("click", () => { prompt.value = b.textContent; autosize(); prompt.focus(); }));
      return;
    }
    const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    el.innerHTML = rows.slice(-60).map((m) => `<li class="msg ${m.who}${m.error ? " error" : ""}${m.streaming ? " streaming" : ""}"${m.streaming ? ' aria-busy="true"' : ""}>
      <div class="msg-head"><span class="who">${m.who === "you" ? "You" : "JARVIS"}</span>${m.who === "you" && m.channel ? `<span class="src-tag">${esc(m.voice ? m.channel + " · voice" : m.channel)}</span>` : ""}${m.tier ? `<span class="tier">${esc(tierName(m.tier))}</span>` : ""}<time>${dayTime(m.at)}</time></div>
      <div class="msg-body">${esc(m.text)}</div>${m.meta?.length ? `<div class="msg-meta">${m.meta.map((x) => `<span class="state-word">${esc(x)}</span>`).join("")}</div>` : ""}${m.who === "jarvis" && !m.streaming ? usageChip(m.usage) : ""}</li>`).join("");
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
  let handsFree = true; // always-on is the default for every console launch
  store.set("jarvis.handsFree", true);
  const narrow = matchMedia("(max-width: 640px)");
  const placeholder = () => (prompt.placeholder = narrow.matches ? "Ask JARVIS…" : state.case ? "Ask JARVIS (filed into this investigation)" : handsFree ? "Ask JARVIS, say “Jarvis, …”, or tap Space" : "Ask JARVIS, or tap Space to talk");
  narrow.addEventListener("change", placeholder); placeholder();
  $("#case-select").addEventListener("change", placeholder);
  prompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#dock").requestSubmit(); }
    // Space in an empty prompt is never a word: treat it as push-to-talk.
    else if (e.code === "Space" && !prompt.value && !e.repeat) { e.preventDefault(); prompt.blur(); spaceAt = Date.now(); startMic(false, true); }
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
  $("#dock").addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) { stopWaiting(); return; }
    if (!prompt.value.trim() && (mic.active || mic.starting)) return stopMic(); // Enter: send what it heard now
    ask(prompt.value);
  });
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
    if (!heard && (mic.active || mic.starting)) mic.stop(); // typed it instead; drop the half-heard clip
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
      if (done.state === "timeout") { trace([...pre, [routed, "done"], ["still running after 15 min · check the Run view", "fail"]]); return; }
      if (done.state === "lost") { trace([...pre, [routed, "done"], ["lost contact with the console server · the job may still be running", "fail"]]); return; }
      if (current.message) local = local.filter((m) => m !== pending); // memory has it now
      const secs = Math.round((done.finished - done.started) * 10) / 10;
      if (done.state === "done") {
        const res = done.result;
        (res.memory || []).slice(0, 1).forEach((id) => mine.add(id)); store.set("jarvis.mine", [...mine].slice(-300));
        local = local.filter((m) => m !== pending);
        await loadHistory();
        trace([...pre, [routed, "done"], [`answered in ${secs}s${res.voice?.source ? " · spoke via " + res.voice.source : ""}${res.plan ? " · plan ready in the Run view" : ""}`, "done"]]);
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
      setTimeout(() => { if (!busy) idleTrace(); }, 9000);
      relisten();
    }
  }

  // ---------- microphone ----------
  // Always-on by default: the mic idles until you talk and checks each pause (or Enter),
  // but only a request that starts with "Jarvis" is sent; the server drops everything
  // else. Push-to-talk (Space, or with always-on off) needs no wake word.
  const micBtn = $("#mic");
  let firstListen = true, spaceAt = 0, micRetryDelay = 1000, micRetryTimer = null;
  const LISTENING = [["listening for “Jarvis, …” · Esc cancels", "active"]];
  const idleTrace = () => trace(mic.active && handsFree ? LISTENING : []);
  const mic = new Mic({ wave: $("#mic-wave"), onLevel: (l) => (Terrain.level = l), onAutoStop: () => stopMic() });
  function syncMicBtn() {
    micBtn.setAttribute("aria-pressed", String(handsFree || mic.active));
    micBtn.title = handsFree ? "Always listening, but only acts on requests that start with “Jarvis”. Click to stop listening."
      : "Talk: click to listen all the time, or tap Space when you're not typing";
  }
  function relisten() {
    clearTimeout(micRetryTimer);
    micRetryTimer = setTimeout(() => {
      micRetryTimer = null;
      if (handsFree && !busy && !agent.live) startMic(true);
    }, 600);
  }
  async function startMic(auto, ptt) {
    if (agent.live) return;
    // Space while always-on is idling for "Jarvis": drop that wait and take this one as
    // push-to-talk, so it needs no wake word.
    if (ptt && mic.active && mic.wait) { clearTimeout(micRetryTimer); micRetryTimer = null; await mic.stop(); }
    if (mic.active || mic.starting) return;
    if (busy) return auto || toast("Still answering the last request. Talk again in a moment, or press Esc to stop waiting.");
    try {
      await mic.start({ wait: handsFree && !ptt });
      if (!mic.active) return;
      micRetryDelay = 1000;
      syncMicBtn();
      // Don't wipe an answer's trace when listening resumes; it clears to "listening" later.
      if (!auto || !$("#trace").children.length) trace(mic.wait ? LISTENING : [["listening · no “Jarvis” needed · stops when you pause · Esc cancels", "active"]]);
      if (!auto) announce("Listening.");
      Terrain.redraw();
    } catch (e) {
      if (handsFree) {
        syncMicBtn();
        if (e.name !== "NotAllowedError" && e.name !== "SecurityError") {
          const delay = micRetryDelay;
          micRetryDelay = Math.min(micRetryDelay * 2, 30000);
          clearTimeout(micRetryTimer);
          micRetryTimer = setTimeout(() => {
            micRetryTimer = null;
            if (handsFree && !busy && !agent.live) startMic(true);
          }, delay);
        }
      }
      toast(e.name === "NotAllowedError" ? "Microphone permission was denied. Allow it for this page, then try again." : plainError(e.message));
    }
  }
  async function stopMic(cancel) {
    if (!mic.active && !mic.starting) return;
    cancel = cancel || mic.starting;
    const ambient = mic.wait; // an always-on clip: it only counts if it starts with "Jarvis"
    const clip = await mic.stop();
    syncMicBtn();
    const fail = (label, ms, s = "fail") => { trace([[label, s]]); setTimeout(() => { if (!busy) idleTrace(); }, ms); relisten(); };
    if (cancel) return fail("cancelled", 2000);
    if (!clip || !clip.audio) return fail("didn't hear speech · check the mic input level", 3500);
    micBtn.classList.add("busy");
    trace([[firstListen ? "transcribing · loading Whisper on the GPU, first time only" : "checking it's you · transcribing", "active"]]);
    try {
      const r = await api("/api/listen" + (ambient ? "?wake=1" : ""), new Uint8Array(clip.audio.buffer), true);
      if (r.device) firstListen = false;
      // The background listener heard it too and answers "Jarvis, …" itself.
      if (r.listener) return r.addressed ? fail("the background listener is answering that", 2500, "done") : idleTrace();
      if (!r.owner) {
        // Always-on hears JARVIS's own voice and the room; only a push-to-talk miss is worth a toast.
        if (!handsFree) toast("That didn't sound like you, so JARVIS ignored it. Re-run `jarvis voice enroll` if this keeps happening.");
        return fail(`ignored · voice match ${r.score} is below your cutoff`, 2500);
      }
      if (ambient && !r.addressed) return fail("ignored · didn't start with “Jarvis”", 2500);
      if (!r.text) return fail(r.addressed ? "heard “Jarvis” but no request after it" : "heard you, but no words came through", 2500);
      prompt.value = r.text; autosize();
      await ask(r.text, r);
    } catch (e) { trace([["transcription failed", "fail"]]); toast(plainError(e.message)); }
    finally { micBtn.classList.remove("busy"); relisten(); }
  }
  micBtn.addEventListener("click", () => {
    handsFree = !handsFree; store.set("jarvis.handsFree", handsFree);
    if (handsFree) { micRetryDelay = 1000; announce("Always listening."); startMic(); }
    else {
      clearTimeout(micRetryTimer); micRetryTimer = null;
      if (mic.active || mic.starting) stopMic(true);
      announce("Stopped listening.");
    }
    syncMicBtn(); placeholder();
  });
  syncMicBtn();
  // Tap Space to talk (no wake word) anywhere except while typing, and except on a control,
  // where Space keeps its usual job of pressing the button, link or menu that has focus.
  const CONTROL = "button, a[href], summary, select, [role=button], [role=tab], [role=switch], [role=menuitem], [role=checkbox], [role=radio], input, textarea, [contenteditable]";
  const pageFocused = () => { const a = document.activeElement; return !a || a === document.body || !a.matches?.(CONTROL); };
  addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (mic.active || mic.starting) { e.preventDefault(); return stopMic(true); }
      if (busy) { stopWaiting(); return; }
      if (!$("#inspector").hidden && big) { big.select(null); return; }
      if (OVERLAY && !typing()) { hideOverlay(); return; }
    }
    if (typing() || e.metaKey || e.altKey) return;
    if (e.code === "Space" && !e.repeat && !e.ctrlKey && pageFocused()) { e.preventDefault(); spaceAt = Date.now(); startMic(false, true); }
    else if (e.key === "Enter" && (mic.active || mic.starting) && pageFocused()) { e.preventDefault(); stopMic(); }
    else if (!e.ctrlKey && /^[1-9]$/.test(e.key) && pageFocused()) location.hash = VIEWS[+e.key - 1];
    else if (e.key === "/" || (e.key.toLowerCase() === "k" && e.ctrlKey)) { e.preventDefault(); prompt.focus(); }
  });
  // Hold Space to talk; a quick tap leaves it listening until you pause.
  addEventListener("keyup", (e) => { if (e.code === "Space" && (mic.active || mic.starting) && !mic.wait && Date.now() - spaceAt > 400) stopMic(); });

  // ---------- live: ElevenLabs agent call (its speech in/out, JARVIS answers) ----------
  const callBtn = $("#agent-call"), voiceSel = $("#voice-select"), voiceBtn = $("#voice-btn");
  const tune = { speed: $("#voice-speed"), stability: $("#voice-stability") };
  const CALL_TITLE = { idle: "Live call: talk back and forth with JARVIS (ElevenLabs voice agent)",
    connecting: "Connecting to the ElevenLabs agent…", listening: "On a live call: just talk. Click or Esc to hang up.",
    speaking: "JARVIS is speaking; talk over it to interrupt. Click or Esc to hang up." };
  const clip = (t, n = 90) => (t.length > n ? t.slice(0, n - 1) + "…" : t);
  const caption = $("#agent-caption"), captionWho = $("#agent-caption-who"), captionText = $("#agent-caption-text");
  let captionRun = 0, streamedAgentText = "", agentTextStreaming = false;
  function showCaption(who, text, typewriter = false) {
    caption.hidden = false; captionWho.textContent = who; const run = ++captionRun;
    const value = String(text || "");
    if (!typewriter) { captionText.textContent = value; return; }
    captionText.textContent = "";
    let at = 0;
    const step = () => {
      if (run !== captionRun) return;
      at = Math.min(value.length, at + Math.max(1, Math.ceil(value.length / 90)));
      captionText.textContent = value.slice(0, at);
      if (at < value.length) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  const agent = new ElevenAgent({
    api,
    onState(s) {
      callBtn.dataset.state = s; callBtn.setAttribute("aria-pressed", String(s !== "idle")); callBtn.title = CALL_TITLE[s];
      if (s === "connecting") trace([["connecting to the voice agent", "active"]]);
      if (s === "idle") { caption.hidden = true; captionText.textContent = ""; captionRun++; streamedAgentText = ""; agentTextStreaming = false;
        trace([["call ended", "done"]]); setTimeout(() => { if (!agent.live && !busy) idleTrace(); }, 3000); relisten(); }
    },
    onLevel: (l) => (Terrain.level = l),
    onUser: (t) => { if (t) { showCaption("David", t, true); trace([[`you · ${clip(t)}`, "done"]]); announce("You said: " + t); } },
    onAgent: (t) => { if (t) { if (!agentTextStreaming && t !== streamedAgentText) showCaption("JARVIS", t, false); trace([[`jarvis · ${clip(t)}`, "done"]]); } },
    onAgentPart: (part) => {
      if (part.type === "start") { streamedAgentText = ""; agentTextStreaming = true; showCaption("JARVIS", "", false); }
      else if (part.type === "delta") { streamedAgentText += part.text || ""; showCaption("JARVIS", streamedAgentText, false); }
      else if (part.type === "stop") agentTextStreaming = false;
    },
    onTool: (req, phase) => trace([[`asking JARVIS · ${clip(req, 60)}`, phase === "start" ? "active" : phase]]),
    onVoice(r) {
      if (r.current && voiceSel.querySelector(`option[value="${CSS.escape(r.current)}"]`)) voiceSel.value = voiceSel.dataset.value = r.current;
      const extra = [r.speed != null && `speed ${r.speed}`, r.stability != null && `stability ${r.stability}`].filter(Boolean).join(", ");
      toast(`Voice: ${String(r.name || "unchanged").split(" - ")[0]}${extra ? ` (${extra})` : ""}. Reconnecting…`);
    },
    onMicSelected: (label) => { if (allMics.checked) $("#mic-settings-status").textContent = `Using ${label}`; },
    onError: (m) => toast(plainError(m)),
  });
  async function toggleCall() {
    if (agent.live) return agent.stop();
    if (mic.active || mic.starting) await stopMic(true);
    try { await agent.start(); announce("Live call started. Just talk."); }
    catch (e) { trace([]); toast(e.name === "NotAllowedError" ? "Microphone permission was denied. Allow it for this page, then try again." : plainError(e.message)); }
  }
  callBtn.addEventListener("click", toggleCall);
  if (handsFree) startMic(true); // always-on: listen from page load
  addEventListener("keydown", (e) => { if (e.key === "Escape" && agent.live) { e.preventDefault(); agent.stop(); } });
  const showTune = (k) => { $(`#voice-${k}-out`).textContent = Number(tune[k].value).toFixed(2); };
  const audioPrefs = store.get("jarvis.agentAudio", {});
  const deviceSelect = $("#agent-mic-device"), gainSelect = $("#agent-mic-gain");
  const allMics = $("#agent-mic-all");
  allMics.checked = typeof audioPrefs.allMicrophones === "boolean" ? audioPrefs.allMicrophones : !audioPrefs.deviceId;
  const audioToggles = { echoCancellation: $("#agent-echo"), noiseSuppression: $("#agent-noise"), autoGainControl: $("#agent-agc") };
  gainSelect.value = audioPrefs.gain || 1;
  for (const [key, input] of Object.entries(audioToggles)) input.checked = audioPrefs[key] !== false;
  const persistAudioPrefs = () => {
    const next = { deviceId: deviceSelect.value, gain: Number(gainSelect.value), allMicrophones: allMics.checked };
    for (const [key, input] of Object.entries(audioToggles)) next[key] = input.checked;
    store.set("jarvis.agentAudio", next); $("#agent-mic-gain-out").textContent = `${Math.round(next.gain * 100)}%`;
    agent.setAudioSettings?.(next);
  };
  const syncMicOptions = () => { deviceSelect.disabled = allMics.checked; };
  deviceSelect.value = audioPrefs.deviceId || "";
  syncMicOptions();
  deviceSelect.addEventListener("change", persistAudioPrefs);
  allMics.addEventListener("change", () => { syncMicOptions(); persistAudioPrefs(); });
  gainSelect.addEventListener("input", persistAudioPrefs);
  for (const input of Object.values(audioToggles)) input.addEventListener("change", persistAudioPrefs);
  $("#agent-mic-refresh").addEventListener("click", async () => {
    const status = $("#mic-settings-status");
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const inputs = devices.filter((d) => d.kind === "audioinput");
      const selectedDevice = store.get("jarvis.agentAudio", {}).deviceId;
      deviceSelect.innerHTML = '<option value="">System default</option>' + inputs.map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || `Microphone ${i + 1}`)}</option>`).join("");
      deviceSelect.value = selectedDevice && inputs.some((d) => d.deviceId === selectedDevice) ? selectedDevice : "";
      status.textContent = `${inputs.length} input${inputs.length === 1 ? "" : "s"} found`;
    } catch (e) { status.textContent = "Device list unavailable"; toast(plainError(e.message)); }
  });
  navigator.mediaDevices?.addEventListener?.("devicechange", () => $("#agent-mic-refresh").click());
  $("#agent-mic-gain-out").textContent = `${Math.round(Number(gainSelect.value) * 100)}%`;
  async function loadVoices() {
    let status;
    try { status = await api("/api/agent"); } catch (e) { return; }
    callBtn.hidden = !status.configured;
    if (!status.has_key) return;
    for (const k of ["speed", "stability"]) {
      const [lo, hi] = status.ranges?.[k] || [tune[k].min, tune[k].max];
      Object.assign(tune[k], { min: lo, max: hi });
      if (status.tuning?.[k] != null) tune[k].value = status.tuning[k];
      showTune(k);
    }
    $("#voice-note").textContent = status.tts_engine === "elevenlabs"
      ? "Used for live calls and every reply JARVIS reads aloud, here and from the listener."
      : "Used for live calls. Read-aloud replies use the local Kokoro voice.";
    try {
      const { current, voices } = await api("/api/agent/voices");
      voiceSel.innerHTML = (current && !voices.some((v) => v.voice_id === current) ? `<option value="${esc(current)}">Current voice</option>` : "") +
        voices.map((v) => `<option value="${esc(v.voice_id)}" title="${esc(v.about)}">${esc(v.name)}</option>`).join("");
      if (current) voiceSel.value = current;
      voiceBtn.hidden = !voices.length;
    } catch (e) { voiceBtn.hidden = true; }
  }
  async function saveVoice(body, said) {
    const r = await api("/api/agent/voice", body);
    if (agent.live) { toast(`${said(r)}. Reconnecting the call…`); await agent.reconnect("How's this?"); }
    else toast(`${said(r)}.`);
    return r;
  }
  voiceSel.addEventListener("change", async () => {
    const before = voiceSel.dataset.value;
    try { voiceSel.dataset.value = (await saveVoice({ voice_id: voiceSel.value }, (r) => `Voice set to ${r.name}`)).current; }
    catch (e) { if (before) voiceSel.value = before; toast(plainError(e.message)); }
  });
  for (const k of ["speed", "stability"]) {
    tune[k].addEventListener("input", () => showTune(k));
    tune[k].addEventListener("change", async () => {
      try { await saveVoice({ [k]: Number(tune[k].value) }, () => `${k === "speed" ? "Speed" : "Stability"} set to ${Number(tune[k].value).toFixed(2)}`); }
      catch (e) { toast(plainError(e.message)); }
    });
  }
  $("#voice-preview").addEventListener("click", async (e) => {
    const b = e.currentTarget; b.disabled = true;
    try {
      const r = await api("/api/agent/voice/preview", {});
      if (!String(r.source).startsWith("elevenlabs") && r.source !== "cache") toast(`Played with ${r.source}, not ElevenLabs.`);
    } catch (err) { toast(plainError(err.message)); }
    setTimeout(() => { b.disabled = false; }, 1500);
  });
  loadVoices().then(() => { voiceSel.dataset.value = voiceSel.value; });
  $("#agent-mic-refresh").click();

  // ---------- live: one event stream for console, voice listener and CLI ----------
  // turn.* events stream every request as it happens, whichever channel it came from;
  // memory/plan/voice events say what changed so only that part reloads.
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
    "connections.changed"() { if (!$("#view-devices").hidden) loadConnections(); },
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
      if (t) Object.assign(t, { done: true, reply: d.reply, seconds: d.seconds, plan: d.plan, tier: d.tier || t.tier, usage: d.usage });
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
    "voice.state"(d) {
      if (state.overview) state.overview.voice.listening = d.listening;
      // A listener killed mid-answer never sends turn.done.
      if (!d.listening) for (const t of live.turns.values()) if (t.channel === "voice" && !t.done)
        Object.assign(t, { done: true, error: true, reply: "The listener stopped before it finished answering." });
      drawThread();
      soonOverview();
    },
    "voice.ready"() { soonOverview(); },
    "voice.heard"() { soonOverview(); },
    "voice.ignored"() { soonOverview(); },
    // Grok Bot finished a handoff after the brain's turn had already ended.
    "grok.reply"(d) { toast(`Grok finished: ${String(d.answer || "").slice(0, 220)}`); soonHistory(); },
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

  window.Jarvis = { api, esc, icon, ago, tierName, words, toast, state };

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
