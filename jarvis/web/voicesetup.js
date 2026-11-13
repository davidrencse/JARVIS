/* Voice setup: a guided mic check + enrollment. The server records through the same mic
   path the always-on listener uses (onboard.py); this page only shows the steps, the live
   level and the line to read, and polls /api/voice/setup. Opens by itself on a machine
   with no voiceprint; Settings > Voice opens it again. */
(() => {
  "use strict";
  const STEPS = [["quiet", "Room"], ["level", "Mic"], ["enroll", "Your voice"], ["test", "Check"], ["done", "Ready"]];
  const ORDER = { starting: 0, quiet: 0, level: 1, enroll: 2, learning: 3, test: 3, done: 4 };
  const LATER = "jarvis.voicesetup.later";
  const esc = window.JarvisEsc;
  const icon = (n) => `<svg class="i" aria-hidden="true"><use href="icons.svg?v=2#i-${n}"/></svg>`;

  async function api(path, body) {
    const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-JARVIS": "1" }, body: JSON.stringify(body) };
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({ error: "HTTP " + res.status }));
    if (!res.ok || data.error) throw new Error(data.error || "HTTP " + res.status);
    return data;
  }

  const dlg = document.createElement("dialog");
  dlg.className = "vs";
  dlg.setAttribute("aria-labelledby", "vs-title");
  dlg.innerHTML = `
    <div class="vs-card">
      <header class="vs-top">
        <span class="vs-kicker">${icon("audio-lines")}Voice setup</span>
        <ol class="vs-steps" aria-label="Progress">${STEPS.map(([k, l], i) => `<li data-i="${i}"><span class="vs-dot"></span><span class="vs-step">${l}</span></li>`).join("")}</ol>
        <button type="button" class="vs-x" data-act="close" aria-label="Close">${icon("x")}</button>
      </header>
      <div class="vs-body" aria-live="polite"></div>
      <footer class="vs-foot"></footer>
    </div>`;
  document.body.appendChild(dlg);
  const body = dlg.querySelector(".vs-body"), foot = dlg.querySelector(".vs-foot");

  let view = null, devices = null, device = "", timer = 0, screen = "", lastKey = "", polling = false;
  // A finished or failed run from before this opening is old news: show the start screen.
  let stale = true;

  function orb(level) {
    return `<div class="vs-orb" style="--lvl:${level || 0}" aria-hidden="true"><i></i><i></i><i></i><b>${icon("mic")}</b></div>`;
  }

  function deviceSelect() {
    if (!devices) return `<p class="vs-note">Looking for microphones…</p>`;
    const opts = [`<option value="">System default</option>`].concat(devices.map((d) => `<option value="${esc(d)}"${d === device ? " selected" : ""}>${esc(d)}</option>`));
    return `<label class="vs-field"><span>Microphone</span><select class="st-select vs-select" data-role="device">${opts.join("")}</select></label>`;
  }

  function intro() {
    const again = view?.enrolled;
    body.innerHTML = `
      ${orb(0)}
      <h2 id="vs-title">${again ? "Retrain JARVIS on your voice" : "Teach JARVIS your voice"}</h2>
      <p class="vs-lede">About a minute. JARVIS tells you what to say; just talk normally from where you usually sit.</p>
      <details class="vs-more"${device ? " open" : ""}><summary>Different mic?</summary>${deviceSelect()}</details>`;
    foot.innerHTML = `
      <button type="button" class="st-btn" data-act="later">${again ? "Close" : "Not now"}</button>
      <button type="button" class="st-btn st-btn-strong" data-act="start">${icon("mic")}${again ? "Start again" : "Start"}</button>`;
  }

  function running(v) {
    const enroll = v.step === "enroll", prog = enroll ? v.clips.length : 0;
    const bar = enroll ? `<div class="vs-bar" role="progressbar" aria-valuemin="0" aria-valuemax="${v.total}" aria-valuenow="${prog}" aria-label="Lines recorded">${Array.from({ length: v.total }, (_, i) => `<span class="${i < prog ? "on" : i === prog ? "now" : ""}"></span>`).join("")}</div><p class="vs-count">Line ${Math.min(v.index, v.total)} of ${v.total}</p>` : "";
    const say = v.phrase ? `<blockquote class="vs-phrase">“${esc(v.phrase)}”</blockquote>` : "";
    const quiet = v.step === "quiet" || v.step === "starting" || v.step === "learning";
    body.innerHTML = `
      ${orb(v.level)}
      <p class="vs-msg${quiet ? " vs-wait" : ""}" id="vs-title">${esc(v.message || "Working")}</p>
      ${say}
      ${bar}
      <p class="vs-warn"${v.warning ? "" : " hidden"}>${icon("flag")}<span>${esc(v.warning)}</span></p>`;
    foot.innerHTML = `<span class="vs-hint">${v.phrase ? "Speak when you're ready. It moves on by itself." : ""}</span><button type="button" class="st-btn" data-act="cancel">Cancel</button>`;
  }

  function done(v) {
    const r = v.result || {}, m = v.mic || {};
    const passed = r.test_passed, score = r.test_score;
    const verdict = passed === true ? "JARVIS recognised you." : passed === false ? "That check was a near miss; JARVIS keeps learning your voice as you use it." : "Skipped the final check.";
    const rows = [
      ["Voice match", score == null ? "—" : `${Number(score).toFixed(2)} <small>needs ${Number(r.threshold).toFixed(2)}</small>`],
      ["Mic", esc(m.device || "System default")],
      ["Signal over room noise", m.snr_db == null ? "—" : `${m.snr_db} dB`],
      ["Listener", (r.listening ?? v.listening) ? "On, listening for “Jarvis”" : "Off"],
    ];
    body.innerHTML = `
      <div class="vs-ok${passed === false ? " vs-meh" : ""}" aria-hidden="true">${icon(passed === false ? "audio-lines" : "check")}</div>
      <h2 id="vs-title">You're set up</h2>
      <p class="vs-lede">${verdict} Say “Jarvis, …” any time.</p>
      <dl class="vs-facts">${rows.map(([k, val]) => `<div><dt>${k}</dt><dd>${val}</dd></div>`).join("")}</dl>
      ${v.warning ? `<p class="vs-warn">${icon("flag")}<span>${esc(v.warning)}</span></p>` : ""}
      <label class="vs-toggle"><span><b>Start listening at login</b><small>So JARVIS is ready whenever this computer is.</small></span>
        <button type="button" class="st-switch" role="switch" aria-checked="${!!v.autostart}" data-act="autostart" aria-label="Start listening at login"></button></label>`;
    foot.innerHTML = `<button type="button" class="st-btn" data-act="restart">Redo</button><button type="button" class="st-btn st-btn-strong" data-act="close">Done</button>`;
  }

  function failed(v) {
    body.innerHTML = `
      <div class="vs-ok vs-bad" aria-hidden="true">${icon("mic-off")}</div>
      <h2 id="vs-title">That didn't work</h2>
      <p class="vs-lede">${esc(v.error)}</p>
      ${deviceSelect()}
      <p class="vs-note">${v.enrolled ? "Your previous voice setup still works." : "Nothing was changed."}</p>`;
    foot.innerHTML = `<button type="button" class="st-btn" data-act="later">Close</button><button type="button" class="st-btn st-btn-strong" data-act="start">Try again</button>`;
  }

  function paintSteps(v) {
    const at = v && v.running || v?.step === "done" ? ORDER[v.step] ?? -1 : -1;
    dlg.querySelectorAll(".vs-steps li").forEach((li) => {
      const i = Number(li.dataset.i);
      li.className = i < at ? "done" : i === at ? "now" : "";
      if (i === at) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    });
  }

  function render() {
    const v = view || {};
    const next = v.running ? "run" : stale ? "intro" : v.step === "done" ? "done" : v.step === "error" ? "error" : "intro";
    paintSteps(v);
    if (next === "run") {
      // Re-render text only when it changes; the level just moves the orb.
      const key = [v.step, v.message, v.phrase, v.warning, v.clips.length, v.index].join("|");
      if (screen !== "run" || key !== lastKey) { running(v); lastKey = key; }
      else body.querySelector(".vs-orb")?.style.setProperty("--lvl", levelOf(v.level));
    } else if (next !== screen) {
      ({ done, error: failed, intro }[next])(v);
      // Land on the main action, not the close button the dialog would pick first.
      foot.querySelector(".st-btn-strong")?.focus();
    }
    dlg.dataset.screen = screen = next;
    const orbEl = body.querySelector(".vs-orb");
    if (orbEl && next === "run") orbEl.style.setProperty("--lvl", levelOf(v.level));
  }
  // RMS of speech sits around 0.01–0.1: a compressed scale makes both ends visible.
  const levelOf = (rms) => Math.min(1, Math.pow(Math.max(0, rms || 0) * 14, 0.6)).toFixed(3);

  function armPoll(ms) {
    clearTimeout(timer);
    timer = 0;
    if (!dlg.open || document.hidden) return;
    timer = setTimeout(poll, ms);
  }
  async function poll() {
    clearTimeout(timer);
    timer = 0;
    if (!dlg.open || document.hidden) return;
    if (polling) return;
    polling = true;
    try {
      try { view = await api("/api/voice/setup"); render(); }
      catch (e) { view = { ...(view || {}), running: false, step: "error", error: e.message }; render(); }
    } finally { polling = false; }
    armPoll(view?.running ? 120 : 1500);
  }

  async function loadDevices() {
    try { devices = (await api("/api/voice/devices")).devices || []; }
    catch (e) { devices = []; }
    if (!view?.running && (screen === "intro" || screen === "error")) {
      const box = body.querySelector(".vs-field, .vs-note");
      if (box) box.outerHTML = deviceSelect();
    }
  }

  async function open(preset) {
    if (!dlg.open) dlg.showModal();
    screen = "";
    if (preset) view = preset;
    else try { view = await api("/api/voice/setup"); } catch (e) { view = { step: "error", error: e.message }; }
    if (view.mic?.device) device = view.mic.device;
    stale = !view.running;
    render();
    if (!devices) loadDevices();
    // The view just loaded; the loop's next read is the live level, not a second copy of this one.
    armPoll(view?.running ? 120 : 1500);
  }

  function close() {
    if (view?.running) return; // cancel first; Esc is caught below
    clearTimeout(timer);
    if (dlg.open) dlg.close();
    document.dispatchEvent(new CustomEvent("voicesetup:closed", { detail: view }));
  }

  dlg.addEventListener("cancel", (e) => { if (view?.running) e.preventDefault(); });
  dlg.addEventListener("change", (e) => { if (e.target.matches("[data-role=device]")) device = e.target.value; });
  dlg.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const act = b.dataset.act;
    if (act === "close") { if (view?.running) return; close(); return; }
    if (act === "later") { try { localStorage.setItem(LATER, String(Date.now())); } catch (_) {} close(); return; }
    if (act === "restart") { stale = true; screen = ""; render(); if (!devices) loadDevices(); return; }
    b.disabled = true;
    try {
      if (act === "start") { stale = false; view = await api("/api/voice/setup", { action: "start", device }); screen = ""; render(); armPoll(120); }
      else if (act === "cancel") { b.textContent = "Cancelling"; await api("/api/voice/setup", { action: "cancel" }); }
      else if (act === "autostart") {
        const on = b.getAttribute("aria-checked") !== "true";
        b.setAttribute("aria-checked", on);
        view = await api("/api/voice/setup", { action: "autostart", on });
        b.setAttribute("aria-checked", !!view.autostart);
      }
    } catch (err) {
      view = { ...(view || {}), running: false, step: "error", error: err.message }; screen = ""; render();
    } finally { b.disabled = false; }
  });

  // Settings > Voice: what is set up on this machine, and the button that opens setup.
  let entryFlight = null;
  async function paintEntry(s) {
    const box = document.getElementById("vs-entry");
    if (!box) return;
    if (s === undefined) {
      if (entryFlight) return entryFlight;
      entryFlight = api("/api/voice/setup").then((v) => v, () => null).then((v) => { entryFlight = null; return paintEntry(v); });
      return entryFlight;
    }
    const mini = box.querySelector(".vs-mini"), line = box.querySelector("small"), btn = box.querySelector("[data-voicesetup]");
    const enrolled = !!s?.enrolled, m = s?.mic || {};
    mini.classList.toggle("off", !enrolled);
    line.textContent = !s ? "Voice input isn't available on this computer."
      : !enrolled ? "Not set up yet. JARVIS can't tell your voice from anyone else's."
      : [s.listening ? "Listening for “Jarvis”" : "Listener off", m.device ? m.device : "System default mic", m.measured ? "tuned " + m.measured.split(" ")[0] : "mic not tuned yet"].join(" · ");
    btn.textContent = enrolled ? "Redo setup" : "Set up voice";
    btn.disabled = !s;
  }
  document.addEventListener("click", (e) => { if (e.target.closest("[data-voicesetup]")) open(); });
  document.addEventListener("voicesetup:closed", () => paintEntry());
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearTimeout(timer); timer = 0; return; }
    if (dlg.open) poll();
  });
  const settingsView = document.getElementById("view-settings");
  if (settingsView) new MutationObserver(() => { if (!settingsView.hidden) paintEntry(); }).observe(settingsView, { attributes: true, attributeFilter: ["hidden"] });

  // First run: no voiceprint on this machine, and not put off in the last three days.
  // ?voicesetup=1 opens it regardless (links from elsewhere). One read feeds the settings line and the dialog.
  (async () => {
    const forced = new URLSearchParams(location.search).get("voicesetup") === "1";
    let later = 0;
    try { later = Number(localStorage.getItem(LATER)) || 0; } catch (_) {}
    const recent = !forced && Date.now() - later < 3 * 864e5;
    let s = null;
    try { s = await api("/api/voice/setup"); } catch (_) { s = null; }
    paintEntry(s);
    if (recent || !s) return;
    if (forced || s.running || !s.enrolled) open(s);
  })();
})();
