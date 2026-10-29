/* Settings view: renders /api/settings fields generically, saves each change immediately.
   The Calls and Input panels are static markup wired by app.js and sharedinput.js. */
(() => {
  "use strict";
  const view = document.getElementById("view-settings");
  if (!view) return;
  const $ = (s, r = view) => r.querySelector(s);
  const SECS = ["machines", "voice", "calls", "intelligence", "memory", "input"];
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Fields shown elsewhere (the machine list picks the main) stay out of the generic rows.
  const HIDDEN = new Set(["cluster.main_device"]);
  // Sub-headings inside a section, in order; fields not listed go under the first.
  const GROUPS = {
    voice: [["Who it answers", ["voiceprint.threshold", "voice.near_miss"]],
            ["How it talks", ["voice.kokoro_voice", "voice.kokoro_speed", "voice.max_spoken_chars"]]],
  };
  const NAMES = {
    chatgpt: "ChatGPT", claude: "Claude", reverse: "Reverse", forward: "Forward",
    bm_lewis: "Lewis · British", bm_george: "George · British", am_michael: "Michael · American",
    am_adam: "Adam · American", af_heart: "Heart · American", af_bella: "Bella · American", bf_emma: "Emma · British",
  };
  let state = { fields: [], machines: [], this_os: "" };
  let loading = false, confirmFor = "", confirmT;
  const pending = new Set();
  const restart = new Set();

  async function api(path, body) {
    const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-JARVIS": "1" }, body: JSON.stringify(body) };
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({ error: "HTTP " + res.status }));
    if (!res.ok || data.error) throw new Error(data.error || "HTTP " + res.status);
    return data;
  }

  let toastEl, toastT;
  function toast(msg) {
    if (!toastEl) { toastEl = document.createElement("div"); toastEl.className = "st-toast"; toastEl.setAttribute("role", "status"); document.body.appendChild(toastEl); }
    toastEl.innerHTML = '<svg class="i i-sm" aria-hidden="true"><use href="icons.svg?v=2#i-check"/></svg>' + esc(msg);
    toastEl.classList.add("on"); clearTimeout(toastT); toastT = setTimeout(() => toastEl.classList.remove("on"), 1800);
  }

  const fmt = (f, v) => {
    const dec = (String(f.step || 1).split(".")[1] || "").length;
    return Number(v).toFixed(dec);
  };
  const ago = (t) => {
    const s = Math.max(0, Date.now() / 1000 - t);
    return s < 60 ? Math.round(s) + " s ago" : s < 3600 ? Math.round(s / 60) + " min ago" : s < 86400 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago";
  };
  const byPath = (p) => state.fields.find((f) => f.path === p);
  const rowOf = (p) => view.querySelector('.st-row[data-path="' + CSS.escape(p) + '"]');

  function control(f) {
    const dis = f.disabled ? " disabled" : "";
    const lab = esc(f.label);
    if (f.kind === "bool") return `<button type="button" class="st-switch" role="switch" aria-checked="${!!f.value}" aria-label="${lab}"${dis}></button>`;
    if (f.kind === "number") {
      const min = f.min ?? 0, max = f.max ?? 1, v = f.value ?? min, p = ((v - min) / ((max - min) || 1)) * 100;
      return `<div class="st-slider"><input type="range" min="${min}" max="${max}" step="${f.step || 1}" value="${v}" aria-label="${lab}" style="--p:${p}%"${dis}><output>${f.value == null ? "—" : fmt(f, v)}</output></div>`;
    }
    const ch = (f.choices || []).map((c) => (c && typeof c === "object" ? c : { value: c, label: NAMES[c] || c }));
    if (ch.length && ch.length <= 3 && ch.every((c) => String(c.label).length < 14))
      return `<div class="st-seg" role="radiogroup" aria-label="${lab}">${ch.map((c) => { const on = String(c.value) === String(f.value); return `<button type="button" role="radio" data-v="${esc(c.value)}" aria-checked="${on}" tabindex="${on ? 0 : -1}"${dis}>${esc(c.label)}</button>`; }).join("")}</div>`;
    return `<select class="st-select" aria-label="${lab}"${dis}>${ch.map((c) => `<option value="${esc(c.value)}"${String(c.value) === String(f.value) ? " selected" : ""}>${esc(c.label)}</option>`).join("")}</select>`;
  }

  function rowHtml(f) {
    const help = f.disabled && f.path === "voiceprint.threshold" ? "Enroll your voice first: jarvis voice enroll." : f.help;
    return `<div class="st-row" data-path="${esc(f.path)}" data-kind="${f.kind}"${f.disabled ? ' data-disabled="1"' : ""}>
      <div class="st-label"><b>${esc(f.label)}</b>${help ? `<small>${esc(help)}</small>` : ""}<span class="st-err" role="alert" hidden></span></div>
      <div class="st-ctl">${control(f)}</div></div>`;
  }

  function sectionHtml(sec, fs) {
    if (!fs.length) return '<p class="st-empty">Nothing to configure here yet.</p>';
    const groups = GROUPS[sec];
    if (!groups) return fs.map(rowHtml).join("");
    const placed = new Set(groups.flatMap(([, paths]) => paths));
    return groups.map(([title, paths], i) => {
      const rows = fs.filter((f) => paths.includes(f.path) || (i === 0 && !placed.has(f.path)));
      return rows.length ? `<div class="st-sub"><h3>${esc(title)}</h3></div><div class="st-group">${rows.map(rowHtml).join("")}</div>` : "";
    }).join("");
  }

  function render() {
    for (const sec of SECS) {
      const box = $("#st-rows-" + sec);
      if (!box) continue;
      const fs = state.fields.filter((f) => f.section === sec && !HIDDEN.has(f.path));
      if (GROUPS[sec]) box.classList.add("st-flat");
      box.innerHTML = sectionHtml(sec, fs);
    }
    renderMachines();
  }

  function machineStatus(m) {
    if (m.role === "main") return ["ok", m.this ? "Runs the brain, memory and vault. This PC." : "Runs the brain, memory and vault."];
    if (m.paired === undefined) return ["", m.this ? "This PC. Connects to the main." : "Connects to the main."];
    if (!m.paired) return ["warn", "Not paired. On the main: jarvis cluster invite " + m.id];
    if (!m.online) return ["down", m.seen ? "Offline · last seen " + ago(m.seen) : "Offline · never connected"];
    return m.same_code ? ["ok", "Online · checked in " + ago(m.seen)] : ["warn", "Online · updating to the main's code"];
  }

  function renderMachines() {
    const box = $("#st-machines");
    if (!box) return;
    const list = (state.machines || []).slice().sort((a, b) => (b.role === "main") - (a.role === "main"));
    if (!list.length) { box.innerHTML = '<p class="st-empty">No computers configured. Add them under devices in jarvis.config.json.</p>'; return; }
    box.innerHTML = list.map((m) => {
      const main = m.role === "main", [tone, line] = machineStatus(m);
      const action = main ? "" : confirmFor === m.id
        ? `<div class="st-confirm"><span>Hand the brain to ${esc(m.label || m.id)}?</span><button type="button" class="st-btn st-btn-strong" data-make-main="${esc(m.id)}">Make main</button><button type="button" class="st-btn" data-cancel>Cancel</button></div>`
        : `<button type="button" class="st-btn" data-ask-main="${esc(m.id)}">Make main</button>`;
      return `<div class="st-row st-mc" data-role="${main ? "main" : "node"}">
        <div class="st-label"><b>${esc(m.label || m.id)}<span class="st-badge">${main ? "Main" : "Node"}</span>${m.this ? '<span class="st-badge st-badge-soft">This PC</span>' : ""}</b>
        <small class="st-status" data-tone="${tone}"><i aria-hidden="true"></i>${esc(line)}</small></div>
        <div class="st-ctl">${action}</div></div>`;
    }).join("");
  }

  function applyState(d) {
    state = { fields: d.fields || state.fields, machines: d.machines || state.machines, this_os: d.this_os || state.this_os };
  }

  function fieldErr(path, msg) {
    const row = path && rowOf(path); if (!row) return;
    const e = $(".st-err", row); e.textContent = msg || ""; e.hidden = !msg;
  }

  async function save(path, value, prev) {
    if (pending.has(path)) return;
    pending.add(path);
    const row = rowOf(path); if (row) row.dataset.busy = "1";
    fieldErr(path, "");
    const f = byPath(path); if (f) f.value = value;
    let failed = "";
    try {
      const d = await api("/api/settings", { changes: { [path]: value } });
      applyState(d);
      (d.restart || []).forEach((p) => restart.add(p));
      toast("Saved");
    } catch (e) {
      failed = "Not saved: " + e.message;
      if (f) f.value = prev;
    } finally {
      pending.delete(path);
      render();
      if (failed) { fieldErr(path, failed); if (path === "cluster.main_device") toast(failed); }
      showNotice();
    }
  }

  function showNotice() {
    const n = $("#st-notice");
    if (!restart.size) { n.hidden = true; return; }
    const voice = [...restart].some((p) => p.startsWith("voice"));
    const cluster = [...restart].some((p) => !p.startsWith("voice"));
    const what = [voice && "the voice listener", cluster && "JARVIS on every computer"].filter(Boolean).join(" and ");
    n.hidden = false;
    n.innerHTML = `<span>Restart ${what} to apply.</span>` + (voice ? '<button type="button" class="st-btn st-btn-strong" id="st-restart-voice">Restart voice listener</button>' : "");
  }

  function pickSeg(btn) {
    const row = btn.closest(".st-row"), p = row.dataset.path, f = byPath(p);
    if (String(f.value) === btn.dataset.v) return;
    row.querySelectorAll(".st-seg button").forEach((b) => { const on = b === btn; b.setAttribute("aria-checked", on); b.tabIndex = on ? 0 : -1; });
    save(p, btn.dataset.v, f.value);
  }

  function askMain(id) {
    confirmFor = id; clearTimeout(confirmT);
    if (id) confirmT = setTimeout(() => askMain(""), 8000);
    renderMachines();
    if (id) $(`[data-make-main="${CSS.escape(id)}"]`)?.focus();
  }

  view.addEventListener("click", (e) => {
    const sw = e.target.closest(".st-switch");
    if (sw && !sw.disabled) { const p = sw.closest(".st-row").dataset.path, prev = byPath(p).value; sw.setAttribute("aria-checked", !prev); save(p, !prev, prev); return; }
    const sg = e.target.closest(".st-seg button");
    if (sg && !sg.disabled) { pickSeg(sg); return; }
    const ask = e.target.closest("[data-ask-main]");
    if (ask) { askMain(ask.dataset.askMain); return; }
    if (e.target.closest("[data-cancel]")) { askMain(""); return; }
    const mm = e.target.closest("[data-make-main]");
    if (mm) { mm.disabled = true; clearTimeout(confirmT); confirmFor = ""; save("cluster.main_device", mm.dataset.makeMain, (byPath("cluster.main_device") || {}).value); return; }
    if (e.target.closest("#st-restart-voice")) restartVoice(e.target.closest("button"));
    if (e.target.closest("[data-retry]")) load();
  });
  view.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && confirmFor) { askMain(""); return; }
    const seg = e.target.closest(".st-seg");
    if (seg && /^Arrow(Left|Right|Up|Down)$/.test(e.key)) {
      const bs = [...seg.querySelectorAll("button:not(:disabled)")], i = bs.indexOf(e.target);
      const n = bs[(i + (/Right|Down/.test(e.key) ? 1 : bs.length - 1)) % bs.length];
      n.focus(); pickSeg(n); e.preventDefault();
    }
  });
  // Every slider (generic rows and the static Calls rows) fills its track up to the thumb.
  const fill = (r) => r.style.setProperty("--p", ((r.value - r.min) / ((r.max - r.min) || 1)) * 100 + "%");
  view.addEventListener("input", (e) => {
    const r = e.target.closest(".st-slider input"); if (!r) return;
    fill(r);
    const row = r.closest(".st-row[data-path]"); if (!row) return;
    $("output", r.parentNode).textContent = fmt(byPath(row.dataset.path), r.value);
  });
  view.addEventListener("change", (e) => {
    const row = e.target.closest(".st-row[data-path]"); if (!row) return;
    const p = row.dataset.path, f = byPath(p); if (!f) return;
    if (e.target.matches(".st-slider input")) save(p, Number(e.target.value), f.value);
    else if (e.target.matches(".st-select")) save(p, e.target.value, f.value);
  });

  async function restartVoice(btn) {
    btn.disabled = true; btn.textContent = "Restarting";
    try {
      await api("/api/voice", { action: "stop" });
      await api("/api/voice", { action: "start" });
      [...restart].filter((p) => p.startsWith("voice")).forEach((p) => restart.delete(p));
      toast("Voice listener restarted"); showNotice();
    } catch (err) { btn.disabled = false; btn.textContent = "Restart voice listener"; toast("Restart failed: " + err.message); }
  }

  const tabs = [...view.querySelectorAll('[role="tab"]')];
  function select(sec, focus) {
    if (!SECS.includes(sec)) sec = "machines";
    tabs.forEach((t) => { const on = t.dataset.sec === sec; t.setAttribute("aria-selected", on); t.tabIndex = on ? 0 : -1; if (on && focus) t.focus(); });
    view.querySelectorAll(".st-panel").forEach((p) => { p.hidden = p.dataset.sec !== sec; });
    view.querySelectorAll(".st-slider input").forEach(fill);
    try { localStorage.setItem("jarvis.settings.sec", sec); } catch (e) {}
  }
  tabs.forEach((t) => t.addEventListener("click", () => select(t.dataset.sec)));
  $("#st-tabs").addEventListener("keydown", (e) => {
    const i = tabs.findIndex((t) => t.getAttribute("aria-selected") === "true");
    let n = -1;
    if (/ArrowDown|ArrowRight/.test(e.key)) n = (i + 1) % tabs.length;
    else if (/ArrowUp|ArrowLeft/.test(e.key)) n = (i + tabs.length - 1) % tabs.length;
    else if (e.key === "Home") n = 0; else if (e.key === "End") n = tabs.length - 1;
    if (n >= 0) { e.preventDefault(); select(tabs[n].dataset.sec, true); }
  });

  async function load() {
    if (loading) return; loading = true;
    try { applyState(await api("/api/settings")); render(); }
    catch (e) {
      const msg = '<p class="st-empty">Couldn’t load settings: ' + esc(e.message) + ' <button type="button" class="st-btn" data-retry>Retry</button></p>';
      SECS.forEach((s) => { const b = $("#st-rows-" + s); if (b) b.innerHTML = msg; });
      $("#st-machines").innerHTML = msg;
    }
    finally { loading = false; }
  }

  // Machine status is live: refresh just that list while it is on screen.
  setInterval(async () => {
    if (view.hidden || $("#st-panel-machines").hidden || loading || pending.size || confirmFor || document.hidden) return;
    try { state.machines = (await api("/api/settings")).machines || state.machines; renderMachines(); } catch (e) {}
  }, 10000);

  // ?st=<section> opens a section directly (links from elsewhere); else the last one used.
  let saved = new URLSearchParams(location.search).get("st") || "machines";
  if (!location.search.includes("st=")) try { saved = localStorage.getItem("jarvis.settings.sec") || saved; } catch (e) {}
  select(saved);
  const onShow = () => { if (!view.hidden && !pending.size) load(); };
  new MutationObserver(onShow).observe(view, { attributes: true, attributeFilter: ["hidden"] });
  onShow();
})();
