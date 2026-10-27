/* Settings view: renders /api/settings fields generically, saves each change immediately. */
(() => {
  "use strict";
  const view = document.getElementById("view-settings");
  if (!view) return;
  const $ = (s, r = view) => r.querySelector(s);
  const SECS = ["machines", "voice", "intelligence", "memory", "input"];
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let state = { fields: [], machines: [], this_os: "" };
  let loading = false;
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
  const byPath = (p) => state.fields.find((f) => f.path === p);
  const rowOf = (p) => view.querySelector('.st-row[data-path="' + CSS.escape(p) + '"]');

  function control(f) {
    const dis = f.disabled ? " disabled" : "";
    const lab = esc(f.label);
    if (f.kind === "bool") return `<button type="button" class="st-switch" role="switch" aria-checked="${!!f.value}" aria-label="${lab}"${dis}></button>`;
    if (f.kind === "number") {
      const min = f.min ?? 0, max = f.max ?? 1, p = ((f.value - min) / ((max - min) || 1)) * 100;
      return `<div class="st-slider"><input type="range" min="${min}" max="${max}" step="${f.step || 1}" value="${f.value}" aria-label="${lab}" style="--p:${p}%"${dis}><output>${fmt(f, f.value)}</output></div>`;
    }
    const ch = (f.choices || []).map((c) => (c && typeof c === "object" ? c : { value: c, label: c }));
    if (ch.length && ch.length <= 3 && ch.every((c) => String(c.label).length < 14))
      return `<div class="st-seg" role="radiogroup" aria-label="${lab}">${ch.map((c) => { const on = String(c.value) === String(f.value); return `<button type="button" role="radio" data-v="${esc(c.value)}" aria-checked="${on}" tabindex="${on ? 0 : -1}"${dis}>${esc(c.label)}</button>`; }).join("")}</div>`;
    return `<select class="st-select" aria-label="${lab}"${dis}>${ch.map((c) => `<option value="${esc(c.value)}"${String(c.value) === String(f.value) ? " selected" : ""}>${esc(c.label)}</option>`).join("")}</select>`;
  }

  function rowHtml(f) {
    return `<div class="st-row" data-path="${esc(f.path)}" data-kind="${f.kind}"${f.disabled ? ' data-disabled="1"' : ""}>
      <div class="st-label"><b>${esc(f.label)}</b>${f.help ? `<small>${esc(f.help)}</small>` : ""}<span class="st-err" role="alert" hidden></span></div>
      <div class="st-ctl">${control(f)}</div></div>`;
  }

  function render() {
    for (const sec of SECS) {
      const box = $("#st-rows-" + sec);
      if (!box) continue;
      const fs = state.fields.filter((f) => f.section === sec);
      box.innerHTML = fs.length ? fs.map(rowHtml).join("") : '<p class="st-empty">Nothing to configure here yet.</p>';
    }
    renderMachines();
  }

  function renderMachines() {
    const box = $("#st-machines");
    if (!box) return;
    box.innerHTML = (state.machines || []).map((m) => {
      const main = m.role === "main";
      return `<div class="st-mc" data-role="${main ? "main" : "node"}">
        <span class="st-badge">${main ? "MAIN" : "NODE"}</span><b>${esc(m.label || m.id)}</b>
        ${main ? "<small>Runs the brain and memory. Only one computer can be main; choose another to hand over.</small>" : `<small>Connects to the main PC.</small><button type="button" data-make-main="${esc(m.id)}">Make main</button>`}</div>`;
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
      if (failed) fieldErr(path, failed);
      showNotice();
    }
  }

  function showNotice() {
    const n = $("#st-notice");
    if (!restart.size) { n.hidden = true; return; }
    const voice = [...restart].some((p) => p.startsWith("voice"));
    const cluster = [...restart].some((p) => !p.startsWith("voice"));
    n.hidden = false;
    n.innerHTML = `<span>Restart the ${voice ? "voice listener" : ""}${voice && cluster ? " / " : ""}${cluster ? "cluster" : ""} to apply.</span>` + (voice ? '<button type="button" id="st-restart-voice">Restart voice listener</button>' : "");
  }

  function pickSeg(btn) {
    const row = btn.closest(".st-row"), p = row.dataset.path, f = byPath(p);
    if (String(f.value) === btn.dataset.v) return;
    row.querySelectorAll(".st-seg button").forEach((b) => { const on = b === btn; b.setAttribute("aria-checked", on); b.tabIndex = on ? 0 : -1; });
    save(p, btn.dataset.v, f.value);
  }

  view.addEventListener("click", (e) => {
    const sw = e.target.closest(".st-switch");
    if (sw && !sw.disabled) { const p = sw.closest(".st-row").dataset.path, prev = byPath(p).value; sw.setAttribute("aria-checked", !prev); save(p, !prev, prev); return; }
    const sg = e.target.closest(".st-seg button");
    if (sg && !sg.disabled) { pickSeg(sg); return; }
    const mm = e.target.closest("[data-make-main]");
    if (mm) { mm.disabled = true; save("cluster.main_device", mm.dataset.makeMain, (byPath("cluster.main_device") || {}).value); return; }
    if (e.target.closest("#st-restart-voice")) restartVoice(e.target.closest("button"));
    if (e.target.closest("[data-retry]")) load();
  });
  view.addEventListener("keydown", (e) => {
    const seg = e.target.closest(".st-seg");
    if (seg && /^Arrow(Left|Right|Up|Down)$/.test(e.key)) {
      const bs = [...seg.querySelectorAll("button:not(:disabled)")], i = bs.indexOf(e.target);
      const n = bs[(i + (/Right|Down/.test(e.key) ? 1 : bs.length - 1)) % bs.length];
      n.focus(); pickSeg(n); e.preventDefault();
    }
  });
  view.addEventListener("input", (e) => {
    const r = e.target.closest(".st-slider input"); if (!r) return;
    const f = byPath(r.closest(".st-row").dataset.path);
    $("output", r.parentNode).textContent = fmt(f, r.value);
    r.style.setProperty("--p", ((r.value - r.min) / ((r.max - r.min) || 1)) * 100 + "%");
  });
  view.addEventListener("change", (e) => {
    const row = e.target.closest(".st-row"); if (!row) return;
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
    catch (e) { SECS.forEach((s) => { const b = $("#st-rows-" + s); if (b) b.innerHTML = '<p class="st-empty">Couldn’t load settings: ' + esc(e.message) + ' <button type="button" class="btn ghost" data-retry>Retry</button></p>'; }); }
    finally { loading = false; }
  }

  let saved = "machines"; try { saved = localStorage.getItem("jarvis.settings.sec") || saved; } catch (e) {}
  select(saved);
  const onShow = () => { if (!view.hidden && !pending.size) load(); };
  new MutationObserver(onShow).observe(view, { attributes: true, attributeFilter: ["hidden"] });
  onShow();
})();
