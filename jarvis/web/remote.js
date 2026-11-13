// Remote: see a node computer's screen live in the console and drive its mouse and keyboard.
// Self-contained. Backend: /api/remote (+ /start /frame /input /stop). Frames are long-polled
// one at a time into an <img>; input is batched (<= one request per ~33 ms, one in flight,
// down/up/key/release go out at once). The session only streams while this view is on screen.
// Colour means state: live = --ok, starting/stale = --warn, ended with an error = --alert.
(() => {
  "use strict";
  const root = document.getElementById("rm-root"), section = document.getElementById("view-remote");
  if (!root || !section) return;

  const esc = window.JarvisEsc || ((s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
  const J = () => window.Jarvis;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // HIDDEN_STOP_MS stays under the hub's MAIN_IDLE (20 s): a background tab ends the session
  // itself, with the true reason, before the hub ends it for "the console stopped watching".
  const STALE_MS = 3000, FLUSH_MS = 33, HIDDEN_STOP_MS = 15000;
  const BG_REASON = "Stopped: the tab was in the background too long.";
  const GONE = "JARVIS on the main restarted, so the session ended. Press Connect to start again.";

  root.innerHTML = `
    <header class="rm-head">
      <h1 class="rm-sr">Remote control of a node computer</h1>
      <div class="rm-devs" id="rm-devs" role="radiogroup" aria-label="Computer to control"></div>
      <div class="rm-live">
        <span class="rm-state" id="rm-state" data-st="idle"><i class="rm-dot" aria-hidden="true"></i><b id="rm-st" aria-live="polite">Not connected</b></span>
        <span class="rm-stats"><span class="rm-m" id="rm-fps">– fps</span><span class="rm-m" id="rm-res">–</span><span class="rm-m" id="rm-age">–</span></span>
      </div>
      <div class="rm-tools">
        <label class="rm-q"><span>Quality</span>
          <select id="rm-quality" aria-label="Stream quality">
            <option value="low">Low (fastest)</option><option value="medium">Medium</option><option value="high">High (sharpest)</option>
          </select></label>
        <button type="button" class="cx-btn" id="rm-fs" aria-label="Fullscreen the remote screen">Fullscreen</button>
        <button type="button" class="cx-btn strong" id="rm-go">Connect</button>
      </div>
    </header>
    <div class="rm-note" id="rm-note" role="status" hidden></div>
    <div class="rm-stage" id="rm-stage">
      <div class="rm-view" id="rm-view" tabindex="0" role="application"
           aria-label="Remote screen. While this has focus, your keyboard and mouse are sent to the remote computer.">
        <img class="rm-img" id="rm-img" alt="Live screen of the remote computer" draggable="false" hidden>
        <div class="rm-ph" id="rm-ph"></div>
      </div>
      <div class="rm-fsedge" aria-hidden="true"></div>
      <button type="button" class="rm-fsx" id="rm-fsx">Exit fullscreen</button>
    </div>
    <footer class="rm-bar">
      <span class="rm-kb" id="rm-kb" data-on="0">Keyboard: not captured</span>
      <span class="rm-hint" id="rm-hint">Click outside the screen to stop sending keys.</span>
    </footer>`;

  const $ = (id) => document.getElementById(id);
  const el = { devs: $("rm-devs"), note: $("rm-note"), go: $("rm-go"), fs: $("rm-fs"), q: $("rm-quality"), stage: $("rm-stage"),
    view: $("rm-view"), img: $("rm-img"), ph: $("rm-ph"), state: $("rm-state"), st: $("rm-st"), fps: $("rm-fps"), res: $("rm-res"),
    age: $("rm-age"), kb: $("rm-kb"), hint: $("rm-hint") };

  // ---------- state ----------
  let devices = [], devKey = "", pick = store.get("jarvis.remote.device", ""), quality = store.get("jarvis.remote.quality", "medium");
  let sess = null;                 // the session we are attached to (frames + input)
  let other = null;                // a live session on the server we are not attached to (offer reattach)
  let ended = null;                // { reason, error } of the last session, shown until the next connect
  let gen = 0, loopCtl = null, pollTimer = 0, listTimer = 0, tick = 0, hiddenTimer = 0;
  let aliveAt = 0;  // the hub said the node checked in lately, though the screen didn't change
  let fw = 0, fh = 0, lastFrameAt = 0, attachedAt = 0, frames = [], prevUrl = "", gotFrame = false;
  let busy = false, refreshing = false, refreshSoon = false;
  const visible = () => !section.hidden;
  if (!["low", "medium", "high"].includes(quality)) quality = "medium";
  el.q.value = quality;

  // ---------- rendering ----------
  function setNote(msg, kind) {
    el.note.hidden = !msg;
    el.note.className = "rm-note" + (kind ? " " + kind : "");
    if (typeof msg === "string") el.note.textContent = msg; else if (msg) { el.note.textContent = ""; el.note.append(msg); }
  }
  function viewState() {
    if (!sess) return ended ? (ended.error ? "failed" : "ended") : "idle";
    if (sess.state === "ended") return ended?.error ? "failed" : "ended";
    if (!gotFrame) return Date.now() - attachedAt > 12000 ? "stale" : "starting";
    return Date.now() - Math.max(lastFrameAt, aliveAt) > STALE_MS ? "stale" : "live";
  }
  const LABEL = { idle: "Not connected", starting: "Starting…", live: "Live", stale: "No new frames", ended: "Disconnected", failed: "Ended" };
  function renderStatus() {
    const st = viewState();
    el.state.setAttribute("data-st", st);
    // The error text lives in the centre placeholder only; the status bar just says "Ended".
    let text = LABEL[st];
    if (st === "ended" && ended?.reason) text = ended.reason;
    if (el.st.textContent !== text) el.st.textContent = text;
    const now = performance.now();
    while (frames.length && now - frames[0] > 2000) frames.shift();
    const fps = frames.length > 1 ? (frames.length - 1) / ((frames[frames.length - 1] - frames[0]) / 1000) : 0;
    const fpsText = sess && gotFrame && st !== "stale" ? `${fps.toFixed(fps < 10 ? 1 : 0)} fps` : "– fps";
    const resText = fw && sess ? `${fw}×${fh}` : "–";
    const age = lastFrameAt && sess ? Date.now() - lastFrameAt : -1;
    const ageText = age < 0 ? "no frame yet" : `last frame ${age < 10000 ? Math.round(age) + " ms" : Math.round(age / 1000) + " s"} ago`;
    if (el.fps.textContent !== fpsText) el.fps.textContent = fpsText;
    if (el.res.textContent !== resText) el.res.textContent = resText;
    if (el.age.textContent !== ageText) el.age.textContent = ageText;
    el.age.classList.toggle("warn", st === "stale");
    renderPlaceholder(st);
  }
  function renderPlaceholder(st) {
    const showImg = !!sess && gotFrame;
    el.img.hidden = !showImg;
    el.ph.hidden = showImg;
    if (showImg) return;
    const dev = devices.find((d) => d.id === pick);
    let h, p;
    if (st === "starting" || st === "stale") { h = "Starting the stream…"; p = `Waiting for the first frame from ${esc(dev?.label || "the computer")}.`; }
    else if (st === "failed") { h = "The session ended"; p = esc(ended.error); }
    else if (st === "ended") { h = "Disconnected"; p = "Choose a computer and press Connect to start again."; }
    else if (!devices.some((d) => d.ready)) { h = "No computer is ready"; p = "The node must be online and running the current code. On the main computer, run <code>jarvis cluster update &lt;node&gt;</code>, then check again."; }
    else { h = "Not connected"; p = "Choose a computer and press Connect."; }
    const html = `<div><h2>${esc(h)}</h2><p>${p}</p></div>`;
    if (el.ph.innerHTML !== html) el.ph.innerHTML = html;
  }
  function renderControls() {
    const active = !!sess && sess.state !== "ended";
    el.go.textContent = active ? "Disconnect" : "Connect";
    el.go.classList.toggle("strong", !active);
    el.go.disabled = busy || (!active && !devices.some((d) => d.id === pick && d.ready));
    el.q.disabled = active || busy;
    el.fs.disabled = !active;
    for (const r of el.devs.querySelectorAll("input")) r.disabled = active || busy || r.getAttribute("data-ready") !== "1";
    el.devs.classList.toggle("locked", active);
    el.view.setAttribute("tabindex", active ? "0" : "-1");
  }
  function renderDevices() {
    const key = JSON.stringify(devices) + "|" + pick;
    if (key === devKey) return;
    devKey = key;
    if (!devices.length) {
      el.devs.innerHTML = `<p class="rm-empty">No node computers are linked yet. Pair one with <code>jarvis cluster invite</code> on this computer, then check again.</p>
        <button type="button" class="cx-btn" id="rm-recheck">Check again</button>`;
      $("rm-recheck").addEventListener("click", refresh);
      return;
    }
    el.devs.innerHTML = devices.map((d) => `
      <label class="rm-dev${d.ready ? "" : " off"}${d.id === pick ? " on" : ""}">
        <input type="radio" name="rm-dev" value="${esc(d.id)}" data-ready="${d.ready ? 1 : 0}"${d.id === pick ? " checked" : ""}${d.ready ? "" : " disabled"}>
        <span class="rm-dev-dot ${d.ready ? "ok" : d.online ? "warn" : ""}" aria-hidden="true"></span>
        <span class="rm-dev-t"><b>${esc(d.label || d.id)}</b>
          <small>${d.ready ? "Ready" : esc(d.why || (d.online ? "Not ready" : "Offline"))}</small></span>
      </label>`).join("");
    for (const r of el.devs.querySelectorAll("input")) r.addEventListener("change", () => { pick = r.value; store.set("jarvis.remote.device", pick); devKey = ""; renderDevices(); renderControls(); renderStatus(); });
  }
  function renderOther() {
    // A session already runs on the server (reload, or another tab): offer to take it over or stop it.
    if (sess || !other) { if (!sess && !ended) setNote(""); return; }
    const dev = devices.find((d) => d.id === other.device);
    const box = document.createElement("div"); box.className = "rm-other";
    const t = document.createElement("span");
    t.textContent = `A session on ${dev?.label || other.device} is already ${other.state === "live" ? "live" : "starting"}.`;
    const a = document.createElement("button"); a.type = "button"; a.className = "cx-btn strong"; a.textContent = "Reattach";
    const b = document.createElement("button"); b.type = "button"; b.className = "cx-btn"; b.textContent = "Stop it";
    a.addEventListener("click", () => { const s = other; other = null; attach(s); });
    b.addEventListener("click", async () => { const s = other; other = null; setNote(""); try { await J().api("/api/remote/stop", { session: s.id }); } catch (e) { setNote(e.message, "bad"); } refresh(); });
    box.append(t, a, b);
    setNote(box, "warn");
  }

  // ---------- server state ----------
  async function refresh() {
    if (!visible() || (sess && document.hidden)) return;  // a hidden tab's session is the hidden timer's
    if (refreshing) { refreshSoon = true; return; }       // one status read at a time; a skipped call runs once after
    refreshing = true;
    try {
      const d = await J().api("/api/remote");
      devices = d.devices || [];
      if (!devices.some((x) => x.id === pick)) pick = (devices.find((x) => x.ready) || devices[0])?.id || "";
      const s = d.session;
      if (sess) {
        if (s && s.id === sess.id) syncSession(s);
        else if (!s || s.state === "ended") finish(s?.error || null, s?.error ? "" : "The session is over.");
      } else {
        other = s && s.state !== "ended" ? s : null;
        if (s && s.state === "ended" && s.error && !ended) { ended = { error: s.error, reason: "" }; }
        renderOther();
      }
      renderDevices(); renderControls(); renderStatus();
    } catch (e) {
      if (!sess) setNote(e.message, "bad");
    } finally {
      refreshing = false;
      if (refreshSoon) { refreshSoon = false; refresh(); }
    }
  }
  function syncSession(s) {
    if (s.width) { fw = s.width; fh = s.height; }
    if (s.state === "ended") { finish(s.error || null, s.error ? "" : "The session is over."); return; }
    if (gotFrame) s.state = "live";
    sess = Object.assign(sess, s);
  }

  // ---------- connect / disconnect ----------
  async function connect() {
    if (busy || sess) return;
    const dev = devices.find((d) => d.id === pick);
    if (!dev || !dev.ready) return;
    busy = true; ended = null; setNote(""); renderControls();
    try {
      const s = await J().api("/api/remote/start", { device: pick, quality: el.q.value });
      store.set("jarvis.remote.quality", el.q.value);
      other = null;
      if (!visible()) {  // the view was left while Connect waited: never stream behind it
        ended = { error: null, reason: "Stopped when you left the Remote view." };
        try { await J().api("/api/remote/stop", { session: s.id }); } catch (e) { /* already gone */ }
        return;
      }
      attach(s);
    } catch (e) {
      // No session exists yet: show the failure in the centre placeholder (once), not the banner.
      ended = { error: e.message || "The computer did not start a session.", reason: "" };
    } finally { busy = false; renderControls(); renderStatus(); }
  }
  function attach(s) {
    detachLoop();
    sess = s; gotFrame = false; closing = false; lastFrameAt = 0; attachedAt = Date.now(); frames = []; ended = null; setNote("");
    fw = s.width || 0; fh = s.height || 0;
    quality = s.quality || quality;
    if (s.device && devices.some((d) => d.id === s.device)) { pick = s.device; devKey = ""; renderDevices(); }
    if (s.quality && ["low", "medium", "high"].includes(s.quality)) el.q.value = s.quality;
    inputErr = ""; inputFails = 0; releaseAfterStop = false; inputStopped = false;
    const g = ++gen;
    if (document.hidden) {
      // The tab went to the background while Connect waited: no frames until it's back, and
      // the same background stop as a session that was already running (visibilitychange
      // set hiddenAt; it restarts the frame loop on return). No status poll either.
      clearTimeout(hiddenTimer);
      const left = HIDDEN_STOP_MS - (hiddenAt ? Date.now() - hiddenAt : 0);
      hiddenTimer = setTimeout(() => { if (document.hidden && sess) backgroundStop(); }, Math.max(0, left));
    } else {
      frameLoop(s.id, g);
      clearInterval(pollTimer); pollTimer = setInterval(refresh, 3000);
      clearInterval(tick); tick = setInterval(renderStatus, 250);
    }
    renderControls(); renderStatus();
    try { el.view.focus({ preventScroll: true }); } catch (e) {}
    kbState();  // a view that already had focus fires no focus event
  }
  function detachLoop() { gen++; if (loopCtl) { loopCtl.abort(); loopCtl = null; } }
  // The session is over (by us, by the node, or by an error). reason is shown when there is no error.
  function finish(error, reason) {
    detachLoop();
    clearInterval(pollTimer); pollTimer = 0; clearInterval(tick); tick = 0;
    clearTimeout(flushTimer); flushTimer = 0;
    q = []; held.clear(); heldKeys.clear(); inputErr = ""; inputFails = 0; releaseAfterStop = false; inputStopped = false;
    sess = null; gotFrame = false; closing = false;
    ended = { error: error || null, reason: reason || "" };
    if (prevUrl) { el.img.removeAttribute("src"); URL.revokeObjectURL(prevUrl); prevUrl = ""; }
    if (document.fullscreenElement === el.stage) document.exitFullscreen?.().catch(() => {});
    // No toast here: the placeholder already shows the error, and repeating it is noise.
    renderControls(); renderStatus(); kbState();
  }
  async function disconnect(why) {
    const s = sess;
    if (!s) return;
    busy = true; renderControls();
    await drain(true);
    finish(null, why || "Disconnected.");
    try { await J().api("/api/remote/stop", { session: s.id }); } catch (e) { /* already gone */ }
    busy = false; renderControls(); renderStatus();
    refresh();
  }

  // ---------- frames ----------
  async function frameLoop(id, g) {
    let after = 0, fails = 0;
    while (g === gen) {
      const ctl = new AbortController(); loopCtl = ctl;
      try {
        const res = await fetch(`/api/remote/frame?session=${encodeURIComponent(id)}&after=${after}`,
          { signal: ctl.signal, credentials: "same-origin", headers: { "X-JARVIS": "1" }, cache: "no-store" });
        if (g !== gen) return;
        if (res.status === 204) {
          fails = 0;
          const ago = parseFloat(res.headers.get("X-Node-Ago"));
          if (Number.isFinite(ago) && ago < STALE_MS / 1000) { aliveAt = Date.now() - ago * 1000; if (gotFrame) renderStatus(); }
          continue;
        }
        if (res.status === 410) {
          const d = await res.json().catch(() => ({}));
          if (g === gen) finish(d.error || "The remote computer ended the session.", "");
          return;
        }
        if (res.status === 404) {  // the hub doesn't know the session: JARVIS on the main restarted
          if (g === gen) finish(GONE, "");
          return;
        }
        if (!res.ok) throw new Error("HTTP " + res.status);
        const seq = parseInt(res.headers.get("X-Frame-Seq"), 10);
        const w = parseInt(res.headers.get("X-Frame-Width"), 10), h = parseInt(res.headers.get("X-Frame-Height"), 10);
        const blob = await res.blob();
        if (g !== gen) return;
        after = Number.isFinite(seq) ? seq : after + 1;
        if (w > 0 && h > 0) { fw = w; fh = h; }
        const url = URL.createObjectURL(blob), old = prevUrl;
        prevUrl = url;
        el.img.src = url;
        if (old) setTimeout(() => URL.revokeObjectURL(old), 400);
        fails = 0; lastFrameAt = Date.now(); frames.push(performance.now());
        if (!gotFrame) { gotFrame = true; if (sess) sess.state = "live"; renderControls(); }
        renderStatus();
      } catch (e) {
        if (g !== gen || e.name === "AbortError") return;
        if (++fails >= 6) { finish("Lost contact with the console server.", ""); return; }
        await sleep(Math.min(3000, 300 * fails));
      }
    }
  }

  // ---------- input ----------
  // HTTP 4xx (CSRF, 400/401/403/404/409, …) is permanent: drop ordinary events, send one
  // release, and do not reschedule. Network errors and HTTP 5xx back off from 300 ms,
  // doubling to 5 s. A success starts that delay back at 300 ms.
  let q = [], closing = false, flushTimer = 0, lastSend = 0, inputErr = "", inputFails = 0, releaseAfterStop = false, inputStopped = false;
  const held = new Set(), heldKeys = new Set();
  // In-flight input requests per session id: one stalled request must not block another session.
  const inflight = new Set();
  const flying = () => !!sess && inflight.has(sess.id);
  // closing: Disconnect has queued its final release; nothing new may be accepted after it.
  const canSend = () => !!sess && sess.state !== "ended" && gotFrame && !closing;
  // ok / err / fails / events (the batch just sent) / queue (still waiting).
  // 4xx: stop. The first time, queue is exactly one release (releaseOnce) so the node lets go.
  // A lone release that itself 4xxs clears the queue and does not ask for another try.
  function inputPumpPlan(ok, err, fails, events, queue) {
    const q = Array.isArray(queue) ? queue.slice() : [];
    const sent = Array.isArray(events) ? events : [];
    if (ok) return { queue: q, fails: 0, delay: 0, stop: false, releaseOnce: false, clearNote: true };
    const status = err && typeof err.status === "number" ? err.status : 0;
    if (status >= 400 && status < 500) {
      const sentRelease = sent.length === 1 && sent[0] && sent[0].t === "release";
      if (sentRelease) return { queue: [], fails: 0, delay: 0, stop: true, releaseOnce: false, clearNote: false };
      return { queue: [{ t: "release" }], fails: 0, delay: 0, stop: true, releaseOnce: true, clearNote: false };
    }
    const n = fails > 0 ? Math.floor(fails) : 0;
    const shift = n > 16 ? 16 : n;
    let delay = 300 * Math.pow(2, shift);
    if (delay > 5000) delay = 5000;
    if (!q.length || !q[0] || q[0].t !== "release") q.unshift({ t: "release" });
    return { queue: q, fails: n + 1, delay, stop: false, releaseOnce: false, clearNote: false };
  }
  function push(ev, urgent) {
    if (!canSend()) return;
    if (inputStopped && ev.t !== "release") return;
    if (ev.t === "move" && q.length && q[q.length - 1].t === "move") q[q.length - 1] = ev; else q.push(ev);
    if (urgent) pump();
    else if (!flushTimer && !flying()) flushTimer = setTimeout(pump, Math.max(0, FLUSH_MS - (performance.now() - lastSend)));
  }
  async function pump() {
    clearTimeout(flushTimer); flushTimer = 0;
    if (!q.length || !sess || inflight.has(sess.id)) return;
    const events = q, id = sess.id; q = [];
    const preserveNote = releaseAfterStop && events.length === 1 && events[0] && events[0].t === "release";
    releaseAfterStop = false;
    inflight.add(id); lastSend = performance.now();
    let plan = null;
    try {
      await J().api("/api/remote/input", { session: id, events });
      plan = inputPumpPlan(true, null, inputFails, events, q);
      inputFails = plan.fails;
      q = plan.queue;
      if (!preserveNote) inputStopped = false;
      if (plan.clearNote && inputErr && !preserveNote) { inputErr = ""; setNote(""); }
    } catch (e) {
      if (/Unknown remote session/.test(e.message || "")) { if (sess && sess.id === id) finish(GONE, ""); return; }
      plan = inputPumpPlan(false, e, inputFails, events, q);
      inputFails = plan.fails;
      q = plan.queue;
      inputStopped = !!plan.stop;
      releaseAfterStop = !!plan.releaseOnce;
      const fresh = e.message !== inputErr;
      if (fresh) { inputErr = e.message; setNote("Input was not delivered: " + e.message, "bad"); }
      // The lost batch may have held a key-up or button-up: tell the node to let go of everything,
      // then count as held only what the events still queued after that release press again.
      heldFrom(q);
      if (fresh) refresh();  // the session may be over (410): let the server say so
    } finally {
      inflight.delete(id);
      const live = plan && sess && sess.id === id && sess.state !== "ended";
      if (live && plan.releaseOnce && q.length) pump();
      else if (live && !plan.stop && q.length) {
        if (plan.delay) flushTimer = setTimeout(pump, plan.delay);
        else if (q.some((ev) => ev.t !== "move")) pump(); else flushTimer = setTimeout(pump, FLUSH_MS);
      }
    }
  }
  // Wait (briefly) for queued input to leave; optionally send a release first.
  async function drain(release) {
    if (release) { held.clear(); heldKeys.clear(); if (canSend()) q.push({ t: "release" }); closing = true; pump(); }
    for (let i = 0; i < 30 && (flying() || q.length); i++) { await sleep(20); if (!flying() && q.length) pump(); }
  }
  // What stays down on the node once these events are pressed, starting from nothing held.
  function heldFrom(events) {
    held.clear(); heldKeys.clear();
    for (const ev of events) {
      if (ev.t === "release") { held.clear(); heldKeys.clear(); }
      else if (ev.t === "down") held.add(ev.b);
      else if (ev.t === "up") held.delete(ev.b);
      else if (ev.t === "key") { if (ev.down) heldKeys.add(ev.code); else heldKeys.delete(ev.code); }
    }
  }
  function releaseInput() {
    if (!held.size && !heldKeys.size) return;
    held.clear(); heldKeys.clear();
    push({ t: "release" }, true);
  }

  function content() {
    const r = el.img.getBoundingClientRect(), iw = fw || el.img.naturalWidth, ih = fh || el.img.naturalHeight;
    if (!iw || !ih || r.width < 1 || r.height < 1) return null;
    const s = Math.min(r.width / iw, r.height / ih), w = iw * s, h = ih * s;
    return { l: r.left + (r.width - w) / 2, t: r.top + (r.height - h) / 2, w, h };
  }
  const r5 = (n) => Math.round(n * 1e5) / 1e5;
  // Normalised point inside the drawn image; null in the letterbox (unless clamping, for releases).
  function norm(e, clamp) {
    const c = content();
    if (!c) return null;
    let x = (e.clientX - c.l) / c.w, y = (e.clientY - c.t) / c.h;
    if (x < 0 || x > 1 || y < 0 || y > 1) {
      if (!clamp) return null;
      x = Math.min(1, Math.max(0, x)); y = Math.min(1, Math.max(0, y));
    }
    return { x: r5(x), y: r5(y) };
  }
  const BTN = { 0: "left", 1: "middle", 2: "right" };
  const BIT = { left: 1, right: 2, middle: 4 };  // PointerEvent.buttons
  const NAV = (e) => e.button === 3 || e.button === 4;  // back/forward: never navigate the console
  const at = (p) => (p ? { x: p.x, y: p.y } : {});   // no point: the node presses where it is
  function press(b, down, p) {
    if (down === held.has(b)) return;              // already in that state: never sent twice
    if (down) held.add(b); else held.delete(b);
    push(Object.assign({ t: down ? "down" : "up", b }, at(p)), true);
  }
  // A press or release of a second button while one is held comes as a pointermove (Pointer
  // Events fire pointerdown/up only for the first down and the last up): send it from there.
  function chord(e, p) {
    const b = BTN[e.button];
    if (b && typeof e.buttons === "number") press(b, (e.buttons & BIT[b]) !== 0, p);
    if (e.buttons === 0) for (const h of [...held]) press(h, false, p);  // an up we never saw
  }

  el.view.addEventListener("contextmenu", (e) => e.preventDefault());
  el.view.addEventListener("dragstart", (e) => e.preventDefault());
  for (const k of ["mousedown", "mouseup", "auxclick"]) el.view.addEventListener(k, (e) => { if (NAV(e)) e.preventDefault(); });
  el.view.addEventListener("pointerdown", (e) => {
    try { el.view.focus({ preventScroll: true }); } catch (_) {}
    if (NAV(e)) e.preventDefault();
    const b = BTN[e.button];
    if (!b || !canSend()) return;
    const p = norm(e, false);
    if (!p) return;                              // letterbox: focus only
    e.preventDefault();
    try { el.view.setPointerCapture(e.pointerId); } catch (_) {}
    press(b, true, p);
  });
  el.view.addEventListener("pointermove", (e) => {
    if (!canSend()) return;
    const p = norm(e, held.size > 0);            // while dragging, the edge of the picture holds
    if (held.size && e.button >= 0) chord(e, p);
    else if (held.size && e.buttons === 0) chord(e, p);
    if (p) push({ t: "move", x: p.x, y: p.y }, false);
  });
  const up = (e) => {
    if (NAV(e)) e.preventDefault();
    if (!held.size) return;
    const p = norm(e, true);
    const b = BTN[e.button];
    if (b) press(b, false, p);
    chord(e, p);                                 // pointerup: nothing is down any more
    if (!held.size) try { el.view.releasePointerCapture(e.pointerId); } catch (_) {}
  };
  el.view.addEventListener("pointerup", up);
  el.view.addEventListener("pointercancel", () => { if (held.size) { held.clear(); push({ t: "release" }, true); } });

  let wheelAcc = 0;
  el.view.addEventListener("wheel", (e) => {
    if (!canSend()) return;  // no session: the placeholder and the page scroll as usual
    e.preventDefault();
    if (!norm(e, false)) return;
    wheelAcc += e.deltaMode === 1 ? e.deltaY / 3 : e.deltaMode === 2 ? Math.sign(e.deltaY) * 10 : e.deltaY / 100;
    wheelAcc = Math.max(-40, Math.min(40, wheelAcc));  // a fling's backlog stays short
    const n = Math.max(-20, Math.min(20, Math.trunc(wheelAcc)));  // the hub takes -20..20 per event
    if (n) { wheelAcc -= n; push({ t: "wheel", dy: n }, true); }
  }, { passive: false });

  el.view.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    // The way out for keyboard users, since Tab is sent to the remote too.
    if (e.code === "Escape" && e.ctrlKey && e.altKey && e.shiftKey) { e.preventDefault(); e.stopPropagation(); releaseInput(); el.view.blur(); if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {}); return; }
    // While a session runs, keys never reach the console's own shortcuts (1-9 switch views,
    // Space starts the mic), even before the first frame when they can't be sent yet.
    if (!sess || sess.state === "ended") return;
    e.preventDefault(); e.stopPropagation();
    if (!canSend()) return;
    heldKeys.add(e.code);
    push({ t: "key", code: e.code, down: true }, true);
  });
  el.view.addEventListener("keyup", (e) => {
    if (!sess || sess.state === "ended") return;
    e.preventDefault(); e.stopPropagation();
    if (!canSend() || !heldKeys.has(e.code)) return;
    heldKeys.delete(e.code);
    push({ t: "key", code: e.code, down: false }, true);
  });
  // Fullscreen + Keyboard Lock: Ctrl+W, Ctrl+T, Ctrl+Tab... reach the page (and the remote)
  // instead of closing or switching the console tab. Chrome only allows it in fullscreen.
  let kbLocked = false;
  function kbLock() {
    try {
      const k = navigator.keyboard;
      if (k && k.lock) k.lock().then(() => { kbLocked = true; kbState(); }, () => {});
    } catch (e) {}
  }
  function kbUnlock() {
    try { if (navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock(); } catch (e) {}
    kbLocked = false;
  }
  const kbState = () => {
    const on = document.activeElement === el.view && !!sess;
    el.kb.setAttribute("data-on", on ? "1" : "0");
    el.kb.textContent = on ? "Keyboard: captured" : "Keyboard: not captured";
    el.hint.textContent = !on ? "Click the screen to send keys." : kbLocked
      ? "Browser keys like Ctrl+W go to the remote too. Hold Esc, point at the top edge, or press Ctrl+Alt+Shift+Esc to leave fullscreen."
      : "Click outside the screen to stop sending keys. Keyboard users: Ctrl+Alt+Shift+Esc.";
  };
  el.view.addEventListener("focus", kbState);
  // Capture lost mid-drag (element removed, browser took it back): let go of what is held.
  el.view.addEventListener("lostpointercapture", () => { if (held.size) releaseInput(); });
  el.view.addEventListener("blur", () => { releaseInput(); wheelAcc = 0; kbState(); });
  kbState();

  // ---------- toolbar ----------
  el.go.addEventListener("click", () => { if (sess) disconnect(); else connect(); });
  el.q.addEventListener("change", () => { quality = el.q.value; store.set("jarvis.remote.quality", quality); });
  el.fs.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else if (el.stage.requestFullscreen) el.stage.requestFullscreen().then(() => { kbLock(); el.view.focus({ preventScroll: true }); }).catch((e) => setNote("Fullscreen was refused: " + e.message, "warn"));
  });
  $("rm-fsx").addEventListener("click", () => { releaseInput(); document.exitFullscreen?.().catch(() => {}); });
  document.addEventListener("fullscreenchange", () => {
    const full = document.fullscreenElement === el.stage;
    el.fs.textContent = full ? "Exit fullscreen" : "Fullscreen";
    if (!full) kbUnlock();
    kbState();
  });

  // ---------- visibility ----------
  // Streaming runs only while this view is on screen; hiding it (or closing the page) stops the session.
  let wasVisible = false;
  function onVisibility() {
    const v = visible();
    if (v === wasVisible) return;
    wasVisible = v;
    if (v) {
      refresh();
      clearInterval(listTimer); listTimer = setInterval(() => { if (!sess && !document.hidden) refresh(); }, 8000);
    } else {
      clearInterval(listTimer); listTimer = 0;
      other = null;
      if (sess) disconnect("Stopped when you left the Remote view.");
    }
  }
  new MutationObserver(onVisibility).observe(section, { attributes: true, attributeFilter: ["hidden"] });
  addEventListener("hashchange", onVisibility);

  function stopBeacon() {
    if (!sess) return;
    const id = sess.id, o = { method: "POST", keepalive: true, credentials: "same-origin", headers: { "X-JARVIS": "1", "Content-Type": "application/json" } };
    try {
      fetch("/api/remote/input", Object.assign({ body: JSON.stringify({ session: id, events: [{ t: "release" }] }) }, o)).catch(() => {});
      fetch("/api/remote/stop", Object.assign({ body: JSON.stringify({ session: id }) }, o)).catch(() => {});
    } catch (e) {}
  }
  addEventListener("pagehide", stopBeacon);
  // Closing or reloading the tab mid-session asks first (a stray Ctrl+W outside fullscreen).
  addEventListener("beforeunload", (e) => { if (sess && sess.state !== "ended") { e.preventDefault(); e.returnValue = ""; } });
  let hiddenAt = 0;
  const backgroundStop = () => { stopBeacon(); finish(null, BG_REASON); };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      releaseInput();  // held keys and buttons go first; the polls stopping must not drop this
      clearTimeout(hiddenTimer);
      hiddenAt = Date.now();
      clearInterval(listTimer); listTimer = 0;
      // Frames and the status/device polls pause with the tab. The hidden timer still ends
      // the session, and pagehide still beacons a release plus stop.
      if (sess) {
        detachLoop();
        clearInterval(pollTimer); pollTimer = 0; clearInterval(tick); tick = 0;
        hiddenTimer = setTimeout(() => { if (document.hidden && sess) backgroundStop(); }, HIDDEN_STOP_MS);
      }
    } else {
      clearTimeout(hiddenTimer);
      // A throttled background timer may not have fired yet: the hub has ended it by now.
      const aged = !!(sess && hiddenAt && Date.now() - hiddenAt >= HIDDEN_STOP_MS);
      if (aged) backgroundStop();
      else if (sess && sess.state !== "ended" && visible()) {
        const g = ++gen; frameLoop(sess.id, g);
        clearInterval(pollTimer); pollTimer = setInterval(refresh, 3000);
        clearInterval(tick); tick = setInterval(renderStatus, 250);
        renderStatus();
        refresh();
      }
      hiddenAt = 0;
      if (visible()) {
        clearInterval(listTimer); listTimer = setInterval(() => { if (!sess && !document.hidden) refresh(); }, 8000);
        // Not after a background stop: that stop is still in flight, and a status read can
        // still see the session and offer Reattach over the "background too long" message.
        if (!sess && !aged) refresh();
      }
    }
  });

  onVisibility();
  renderDevices(); renderControls(); renderStatus();
})();
