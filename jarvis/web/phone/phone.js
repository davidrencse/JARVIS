// JARVIS Phone (jarvis/web/phone/phone.js): the PWA on David's phone. A remote-control dashboard for
// JARVIS, not a second JARVIS: it lists his computers, drives a screen, shows a webcam, finds the
// machines and asks JARVIS through the normal pipeline. The server decides; this draws.
// - Every server or node string is set with textContent, never parsed as HTML.
// - Only a 401 {pair:true} shows Pair. A failed fetch means the phone can't reach the tailnet: it
//   says so and keeps the cookie and the shell.
// - A 403 whose code is csrf means the cookie is not enough. The app stays closed and offers
//   Pair again. Ordinary network failures and other 403s keep the saved check.
// - One frame long-poll and one input batch in flight. Held keys and buttons are let go on every
//   way out (blur, cancel, hidden, failed batch, end); the server's quiet and idle timers are the
//   real guarantee when the phone simply vanishes.
(() => {
  "use strict";

  // ---------- constants ----------
  const POLL_MS = 5000, PAIR_POLL_MS = 2000, PAIR_GIVE_UP_MS = 5 * 60000, ASK_POLL_MS = 700, DOWN_RETRY_MS = 15000;
  // HIDDEN_STOP_MS stays under the hub's MAIN_IDLE (20 s): a backgrounded app ends its own
  // session with the true reason before the hub ends it for silence.
  // STALE_MS outlasts a still screen's rhythm: the node's keep-alive every 2 s, reported by a
  // 204 that the hub holds up to 2.5 s, with margin. Any shorter and "Live" flickers.
  const FLUSH_MS = 33, HIDDEN_STOP_MS = 15000, STALE_MS = 6000, NO_FRAME_MS = 12000, CAMERA_MAX_MS = 15 * 60000;
  const LONG_PRESS_MS = 500, SLOP_PX = 8, PINCH_PX = 24, WHEEL_STEP_PX = 24, TRACK_GAIN = 1.6, MAX_ZOOM = 4;
  const MAX_EVENTS = 200, MAX_TEXT = 1000, MAX_ASK = 2000, MAX_NODES = 80;
  const ASK_KEY = "jarvis.phone.ask", ASK_STALE_MS = 30 * 60000, ASK_GIVE_UP_MS = 20 * 60000;
  const UNREACHABLE = "Can't reach JARVIS — is Tailscale on?";
  const LOST = "Lost contact with JARVIS — is Tailscale on?";
  const GONE = "This session is gone. JARVIS on the main may have restarted.";
  const BG_REASON = "Ended: the app was in the background too long.";
  const WAITING = /waiting for your ok/i;
  const SVGNS = "http://www.w3.org/2000/svg";
  // Icons the console's /icons.svg lacks live in index.html's own sprite.
  const LOCAL_ICONS = new Set(["camera", "smartphone", "mouse-pointer", "pointer", "chevron-left",
    "chevron-down", "lock", "power", "log-out", "wifi-off", "scan", "share", "square-plus", "arrow-left", "arrow-right",
    "arrow-down", "delete"]);
  const CAPS = [["remote", "monitor", "screen"], ["camera", "camera", "camera"]];
  // [code, label, sticky modifier, spoken name, icon]. Order fills 6 columns (2 rows) or 4 (3 rows).
  const KEYS = [["Escape", "Esc"], ["Tab", "Tab"], ["ControlLeft", "Ctrl", true], ["AltLeft", "Alt", true],
    ["ShiftLeft", "Shift", true], ["MetaLeft", "Super", true, "Super key"], ["Backspace", "⌫", false, "Backspace", "delete"],
    ["Enter", "Enter"], ["ArrowLeft", "←", false, "Left arrow", "arrow-left"], ["ArrowUp", "↑", false, "Up arrow", "arrow-up"],
    ["ArrowDown", "↓", false, "Down arrow", "arrow-down"], ["ArrowRight", "→", false, "Right arrow", "arrow-right"]];
  const MODS = new Set(KEYS.filter((k) => k[2]).map((k) => k[0]));
  const SCREENS = { devices: "s-devices", ask: "s-ask", phone: "s-phone", device: "s-device", control: "s-control", camera: "s-camera", fleet: "s-fleet" };
  const HEADS = { devices: "dv-h", ask: "ak-h", phone: "pn-h", device: "d-h", control: "c-h", camera: "v-h", fleet: "fl-h" };
  const TAB_OF = { devices: "devices", device: "devices", control: "devices", camera: "devices", fleet: "devices", ask: "ask", phone: "phone" };

  // ---------- small helpers ----------
  const $ = (id) => document.getElementById(id);
  const now = () => Date.now();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const r5 = (n) => Math.round(n * 1e5) / 1e5;
  const str = (v, max = 400) => (typeof v === "string" ? v.slice(0, max) : "");
  const pad = (n) => String(n).padStart(2, "0");
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); return true; } catch (e) { return false; } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { try { localStorage.setItem(k, ""); } catch (e2) { /* private mode */ } } },
  };
  const media = (q) => { try { return !!(window.matchMedia && window.matchMedia(q).matches); } catch (e) { return false; } };
  const reduced = () => media("(prefers-reduced-motion: reduce)");
  const agent = () => String((window.navigator && navigator.userAgent) || "");
  const isIOS = () => /iPhone|iPad|iPod/.test(agent()) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isAndroid = () => /Android/i.test(agent());
  const standalone = () => media("(display-mode: standalone)") || navigator.standalone === true;
  function text(node, s) { const v = s == null ? "" : String(s); if (node.textContent !== v) node.textContent = v; }
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    if (attrs) for (const k of Object.keys(attrs)) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "text") e.textContent = String(v); else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of kids) if (c != null && c !== false) e.append(c);
    return e;
  }
  function icon(name, small) {
    const s = document.createElementNS(SVGNS, "svg"), u = document.createElementNS(SVGNS, "use");
    s.setAttribute("class", small ? "i i-sm" : "i"); s.setAttribute("aria-hidden", "true");
    u.setAttribute("href", (LOCAL_ICONS.has(name) ? "" : "/icons.svg") + "#i-" + name);
    s.append(u);
    return s;
  }
  // Server times may be epoch seconds, epoch milliseconds or ISO strings; NaN when none of those.
  function msOf(t) {
    if (typeof t === "number" && isFinite(t)) return t < 1e11 ? t * 1000 : t;
    if (typeof t === "string" && t) { const v = Date.parse(t); return isFinite(v) ? v : NaN; }
    return NaN;
  }
  function ago(t) {
    const v = msOf(t);
    if (!isFinite(v)) return "never";
    const s = Math.max(0, Math.round((now() - v) / 1000));
    if (s < 5) return "now";
    if (s < 60) return s + " s ago";
    const m = Math.round(s / 60);
    if (m < 60) return m + " min ago";
    const hr = Math.round(m / 60);
    return hr < 48 ? hr + " h ago" : Math.round(hr / 24) + " d ago";
  }
  const mmss = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return pad(Math.floor(s / 60)) + ":" + pad(s % 60); };
  function day(t) { const v = msOf(t); if (!isFinite(v)) return "–"; const d = new Date(v); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  // Control characters (except tab and newline) and lone surrogates are refused by the node.
  function clean(s) {
    let v = String(s || "").replace(/[\u0000-\u0008\u000B-\u001F\u007F]/g, "");
    if (typeof v.toWellFormed === "function") v = v.toWellFormed().replace(/�/g, "");
    return v;
  }
  function defaultName() { return isIOS() ? "iPhone" : isAndroid() ? "Android phone" : "Phone"; }

  // ---------- elements ----------
  const el = {};
  for (const id of ["boot", "down", "down-h", "down-p", "down-retry", "down-when", "pair", "pair-ios", "pair-form", "p-code", "p-name",
    "p-go", "p-msg", "pair-wait", "pw-time", "pw-bar", "pw-cancel", "pw-msg", "app", "main-label", "link", "link-dot", "link-ms", "hb", "net",
    "net-retry", "main", "dv-meta", "st-online", "st-sess", "dv-list", "dv-empty", "dv-sess", "dv-sess-list", "fl-meta", "fl-note", "fl-auth", "fl-main-status", "fl-transition", "fl-promote", "fl-candidate", "fl-promote-go", "fl-promote-msg", "fl-limit", "fl-state", "fl-retry-row", "fl-retry", "fl-list", "fl-empty", "fl-guide-wrap", "fl-guide", "ak-meta", "a-thread", "a-empty", "a-text", "a-send", "a-count", "a-note", "pn-name", "pn-paired", "pn-host",
    "pn-main", "th-system", "th-light", "th-dark", "pn-sess", "pn-sess-empty", "pn-forget", "pn-forget-t", "pn-msg", "d-dot", "d-h",
    "d-sub", "d-sess", "d-sess-t", "d-sess-open", "d-sess-end", "d-status", "d-role", "d-platform", "d-r-ssid", "d-ssid", "d-r-lan",
    "d-lan", "d-r-ts", "d-ts", "d-q-low", "d-q-medium", "d-q-high", "d-control", "d-control-why",
    "d-camera", "d-camera-why", "d-msg", "d-meters", "c-end", "c-stage",
    "c-layer", "c-cursor", "c-ring", "c-fit", "c-direct", "c-track", "c-type", "c-keys-btn", "c-panel", "c-text", "c-send", "c-keys",
    "c-note", "v-stage", "v-chrome", "live", "tab-devices", "tab-ask", "tab-phone"]) {
    el[id.replace(/-(\w)/g, (_, c) => c.toUpperCase())] = $(id);
  }
  const scr = {};
  for (const k of Object.keys(SCREENS)) scr[k] = $(SCREENS[k]);
  // The two session screens share one engine; each has its own picture, placeholder and readouts.
  function sessionView(p) {
    return { h: $(p + "-h"), state: $(p + "-state"), st: $(p + "-st"), img: $(p + "-img"), ph: $(p + "-ph"), phH: $(p + "-ph-h"),
      phP: $(p + "-ph-p"), phActs: $(p + "-ph-acts"), again: $(p + "-again"), back: $(p + "-back"), fps: $(p + "-fps"),
      res: $(p + "-res"), rtt: $(p + "-rtt"), end: $(p + "-end") };
  }
  const VIEWS = { screen: sessionView("c"), camera: sessionView("v") };
  Object.assign(VIEWS.camera, { clock: $("v-clock"), left: $("v-left"), q: $("v-q") });

  // ---------- network ----------
  // kind: "net" (no answer: Tailscale off, no signal, timeout), "auth" (401 with pair:true),
  // "http" (JARVIS answered and said no; its message is shown as text).
  class Fail extends Error {
    constructor(message, kind, status, data) { super(message); this.kind = kind; this.status = status || 0; this.data = data || {}; }
  }
  // The pair response hands the CSRF token over once. It lives in this origin's storage and is
  // sent only as a header on same-origin calls. Forget and a revoke (401 pair) clear it.
  const CSRF_KEY = "jarvis.phone.csrf";
  const CSRF_RE = /^[A-Za-z0-9_-]{16,128}$/;
  const OP_RE = /^[A-Za-z0-9_-]{8,64}$/;
  let csrfToken = "", csrfUnsaved = false, csrfHold = null, repairing = false;
  function readCsrf() {
    const v = store.get(CSRF_KEY, "");
    csrfToken = CSRF_RE.test(v) ? v : "";
    if (v && !csrfToken) store.del(CSRF_KEY);
  }
  function saveCsrf(token) {
    if (!CSRF_RE.test(token)) return;
    csrfToken = token;
    const wrote = store.set(CSRF_KEY, token);
    csrfUnsaved = !(wrote && store.get(CSRF_KEY, "") === token);
    if (!csrfUnsaved && csrfHold) csrfHold.hidden = true;
  }
  function clearCsrf() { csrfToken = ""; csrfUnsaved = false; if (csrfHold) csrfHold.hidden = true; store.del(CSRF_KEY); }
  function isCsrf(e) {
    return !!(e && e.kind === "http" && e.status === 403 && e.data && e.data.code === "csrf");
  }
  readCsrf();
  function operationId() {
    try {
      const c = window.crypto;
      if (c && typeof c.randomUUID === "function") {
        const id = String(c.randomUUID()).replace(/-/g, "");
        if (OP_RE.test(id)) return id;
      }
      if (c && typeof c.getRandomValues === "function") {
        const bytes = new Uint8Array(16);
        c.getRandomValues(bytes);
        let id = "";
        for (let i = 0; i < bytes.length; i++) id += bytes[i].toString(16).padStart(2, "0");
        if (OP_RE.test(id)) return id;
      }
    } catch (e) { /* a private webview still gets the fallback below */ }
    let id = "";
    while (id.length < 16) id += Math.random().toString(36).slice(2);
    return id.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 32);
  }
  function sessionStopId(id) {
    const raw = String(id || "");
    if (/^[0-9a-f]{32}$/.test(raw)) return "stop-" + raw;
    const cleaned = ("stop-" + raw.replace(/[^A-Za-z0-9_-]/g, "")).slice(0, 64);
    return OP_RE.test(cleaned) ? cleaned : operationId();
  }
  function stampBody(path, body, fixedId) {
    const next = Object.assign({}, body);
    let id = fixedId || next.operation_id || "";
    if (path === "/phone/api/ask") id = next.key || id;
    if (path === "/phone/api/session/stop" && typeof next.session === "string") id = sessionStopId(next.session);
    if (!OP_RE.test(id)) id = operationId();
    next.operation_id = id;
    if (path === "/phone/api/ask") next.key = id;
    return next;
  }
  function phoneHeaders(json) {
    const headers = { "X-JARVIS-Phone": "1" };
    if (json) headers["Content-Type"] = "application/json";
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
    return headers;
  }
  // The remote-view session id is X-JARVIS-Session on the request, never a query parameter.
  function frameUrl(after) {
    return "/phone/api/session/frame?after=" + encodeURIComponent(String(after || 0));
  }
  function b64urlToBytes(s) {
    const clean = String(s || "").replace(/-/g, "+").replace(/_/g, "/");
    if (!clean || /[^A-Za-z0-9+/=]/.test(clean)) throw new Error("bad");
    const pad = clean.length % 4 === 0 ? "" : "=".repeat(4 - (clean.length % 4));
    const bin = atob(clean + pad);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function bytesToB64url(buf) {
    const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf || []);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }
  function passkeyOptions(preview) {
    const src = preview && typeof preview === "object" ? (preview.passkey && typeof preview.passkey === "object" ? preview.passkey : preview) : null;
    if (!src) return null;
    const rp = typeof src.rp_id === "string" ? src.rp_id : "";
    const ids = Array.isArray(src.credential_ids) ? src.credential_ids : null;
    const challenge = typeof src.challenge === "string" ? src.challenge : "";
    if (!rp || !challenge || !ids || !ids.length || ids.some((id) => typeof id !== "string" || !id)) return null;
    return { rp_id: rp, credential_ids: ids, challenge };
  }
  function packProof(cred, challenge) {
    const response = cred && cred.response;
    if (!cred || !response || cred.rawId == null || response.clientDataJSON == null || response.authenticatorData == null || response.signature == null) return null;
    try {
      const rawId = bytesToB64url(cred.rawId);
      // The adapter reads `passkey_proof`. The verifier reads a WebAuthn assertion: id, type, and response.
      return {
        type: "public-key",
        id: rawId,
        rawId,
        challenge,
        response: {
          clientDataJSON: bytesToB64url(response.clientDataJSON),
          authenticatorData: bytesToB64url(response.authenticatorData),
          signature: bytesToB64url(response.signature),
        },
      };
    } catch (e) { return null; }
  }
  async function readPasskey(opt) {
    let challenge;
    try { challenge = b64urlToBytes(opt.challenge); } catch (e) { return null; }
    if (!challenge.length) return null;
    const creds = navigator.credentials;
    if (!creds || typeof creds.get !== "function") return null;
    let allow;
    try { allow = opt.credential_ids.map((id) => ({ type: "public-key", id: b64urlToBytes(id) })); }
    catch (e) { return null; }
    let cred;
    try {
      cred = await creds.get({ publicKey: { challenge, rpId: opt.rp_id, allowCredentials: allow, userVerification: "required" } });
    } catch (e) { return null; }
    return packProof(cred, opt.challenge);
  }
  // call() and the frame loop share this. max caps an error body so it cannot be treated as a document.
  async function readJson(res, max) {
    if (!res || res.status === 204 || typeof res.text !== "function") return {};
    const t = await res.text();
    let raw = typeof t === "string" ? t : "";
    if (max > 0 && raw.length > max) raw = raw.slice(0, max);
    let data = {};
    try { data = raw ? JSON.parse(raw) : {}; } catch (e) { data = {}; }
    if (!data || typeof data !== "object" || Array.isArray(data)) data = {};
    return data;
  }
  async function call(path, body, opts = {}) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), opts.timeout || 20000);
    const payload = body === undefined ? undefined : stampBody(path, body, opts.operationId);
    const init = { method: payload === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
      signal: ctl.signal, headers: phoneHeaders(payload !== undefined) };
    if (payload !== undefined) init.body = JSON.stringify(payload);
    let res, data = {};
    try {
      res = await fetch(path, init);
      data = await readJson(res);
    } catch (e) {
      throw new Fail(UNREACHABLE, "net");
    } finally { clearTimeout(timer); }
    if (!data || typeof data !== "object" || Array.isArray(data)) data = {};
    if (res.status === 401 && data.pair) {
      clearCsrf();
      throw new Fail(str(data.error) || "This phone isn't paired.", "auth", 401, data);
    }
    if (res.ok && (path === "/phone/pair" || path === "/phone/pair/status") && CSRF_RE.test(data.csrf_token)) saveCsrf(data.csrf_token);
    if (!res.ok) throw new Fail(str(data.error) || `JARVIS refused that (HTTP ${res.status}).`, "http", res.status, data);
    return { status: res.status, data };
  }
  // call() for the app: tracks reachability for the banner, and a lost pairing goes back to Pair.
  async function api(path, body, opts) {
    try { const r = await call(path, body, opts); reachable(true); return r.data; }
    catch (e) {
      reachable(e.kind !== "net");
      if (e.kind === "auth") toPair();
      else if (isCsrf(e)) showCsrfRecovery();
      throw e;
    }
  }

  // ---------- top-level screens ----------
  let me = null, loaded = false, downTimer = 0, pollTimer = 0, reach = true;
  let ov = { devices: [], sessions: [], ask: { enabled: false } };
  function stageTo(name) { for (const k of ["boot", "down", "pair", "app"]) el[k].hidden = k !== name; }

  async function boot() {
    clearTimeout(downTimer);
    if (el.pair.hidden && el.app.hidden) stageTo("boot");
    let d;
    try { d = await api("/phone/api/me", undefined, { timeout: 15000 }); }
    catch (e) { if (e.kind !== "auth") showDown(e); return; }
    me = d && typeof d === "object" ? d : {};
    enterApp();
  }
  let repairBtn = null;
  function repairButton() {
    if (repairBtn) return repairBtn;
    repairBtn = h("button", { type: "button", class: "btn strong wide", id: "csrf-repair" }, "Pair again");
    repairBtn.hidden = true;
    repairBtn.addEventListener("click", () => repairPair());
    const host = (el.downRetry && (el.downRetry.parentNode || el.downRetry.parent)) || el.down;
    if (host) host.append(repairBtn);
    return repairBtn;
  }
  // The user chooses this. It is the only path from a csrf refusal back to Pair.
  function repairPair() {
    pauseForRepair();
    toPair("Pair this phone again. On the main computer, run jarvis phone pair and enter the code it prints.");
  }
  function pauseForRepair() {
    repairing = true;
    clearInterval(pollTimer); pollTimer = 0;
    clearTimeout(downTimer); downTimer = 0;
    detachLoop();
  }
  function showCsrfRecovery() {
    repairing = true;
    showDown(new Fail("", "http", 403, { code: "csrf" }));
  }
  function renderCsrfHold() {
    if (!el.app) return;
    if (!csrfUnsaved) { if (csrfHold) csrfHold.hidden = true; return; }
    if (!csrfHold) {
      csrfHold = h("div", { class: "notice", id: "csrf-hold", role: "status" });
      const msg = h("span", { id: "csrf-hold-msg" });
      const go = h("button", { type: "button", class: "mini", id: "csrf-hold-repair" }, "Pair again");
      csrfHold.append(msg, go);
      csrfHold.msg = msg;
      go.addEventListener("click", () => repairPair());
      if (typeof el.app.prepend === "function") el.app.prepend(csrfHold);
      else el.app.append(csrfHold);
    }
    text(csrfHold.msg, "This tab can keep working. The site check could not be saved on this phone, so the next time you open JARVIS you will need to pair again.");
    csrfHold.hidden = false;
  }
  function showDown(e) {
    stageTo("down");
    const csrf = isCsrf(e);
    const net = !csrf && e.kind === "net";
    // Tailscale serve answers 502 with no message while JARVIS restarts or isn't listening.
    const silent = !csrf && !net && e.status >= 502 && e.status <= 504 && !str(e.data && e.data.error);
    if (el.downRetry) el.downRetry.hidden = csrf;
    if (csrf) repairButton().hidden = false;
    else if (repairBtn) repairBtn.hidden = true;
    text(el.downH, csrf ? "Pair this phone again" : net ? UNREACHABLE : silent ? "JARVIS isn't answering" : "JARVIS didn't let this phone in");
    text(el.downP, csrf ? "This phone is still paired, but the check for this site is missing. Pair again from the main computer. This screen does not open the app on the saved sign-in."
      : net ? "Your phone reaches JARVIS through Tailscale. Turn it on, then try again."
      : silent ? "Tailscale reached the PC, but JARVIS there didn't answer. It may be restarting." : e.message);
    text(el.downWhen, csrf ? "" : "Trying again every 15 s.");
    clearTimeout(downTimer); downTimer = 0;
    if (csrf) { pauseForRepair(); return; }
    downTimer = setTimeout(() => { if (!repairing && !document.hidden && !el.down.hidden) boot(); }, DOWN_RETRY_MS);
  }
  function enterApp() {
    repairing = false;
    stageTo("app");
    reachable(true);
    renderCsrfHold();
    text(el.mainLabel, (me.main && str(me.main.label, 60)) || "");
    renderPhone();
    route(true);
    refresh();
    resumeAsk();
    clearInterval(pollTimer);
    pollTimer = setInterval(() => { if (!repairing && !document.hidden && !sess && !starting) refresh(); }, POLL_MS);
  }
  // Everything that talks to the server stops; the shell stays.
  function quiesce() {
    clearInterval(pollTimer); pollTimer = 0;
    retireStart();
    if (sess) { stopBeacon(); finish(null, ""); }
  }

  // ---------- pairing ----------
  let pairing = null;
  function toPair(msg) {
    clearCsrf();
    quiesce();
    me = null;
    stageTo("pair");
    const ios = isIOS() && !standalone();
    el.pairIos.hidden = !ios; el.pairForm.hidden = ios; el.pairWait.hidden = true;
    if (pairing) { clearTimeout(pairing.timer); clearInterval(pairing.tick); pairing = null; }
    el.pGo.disabled = false;
    pairNote(msg || "", msg ? "ok" : "");
    if (!el.pName.value) el.pName.value = defaultName();
  }
  function pairNote(s, st) { text(el.pMsg, s); el.pMsg.setAttribute("data-st", st || ""); }
  const codeOf = (v) => String(v || "").toUpperCase().replace(/[\s-]/g, "");
  function pairGo() {
    if (pairing) return;
    const code = codeOf(el.pCode.value), name = clean(el.pName.value).trim().slice(0, 40) || defaultName();
    if (!/^[A-Z0-9]{8}$/.test(code)) {
      pairNote("The code is the 8 letters and digits that jarvis phone pair prints.", "bad");
      try { el.pCode.focus(); } catch (e) { /* not focusable yet */ }
      return;
    }
    el.pGo.disabled = true;
    pairNote("Checking the code…", "");
    pairing = { code, operationId: operationId(), t0: now(), timer: 0, tick: 0 };
    pairStep("/phone/pair", { code, name, operation_id: pairing.operationId });
  }
  async function pairStep(path, body) {
    const p = pairing;
    if (!p) return;
    let r;
    try { r = await call(path, body, { timeout: 20000 }); }
    catch (e) {
      if (pairing !== p) return;
      // While waiting, a dropped request is retried: the approval on the main still counts.
      if (e.kind === "net" && p.tick) { p.timer = setTimeout(() => pairStep("/phone/pair/status", { code: p.code, operation_id: p.operationId }), PAIR_POLL_MS); return; }
      pairFail(e.message);
      return;
    }
    if (pairing !== p) return;
    if (r.data.ok) { clearInterval(p.tick); pairing = null; el.pCode.value = ""; boot(); return; }
    if (r.status === 202 || r.data.pending) {
      if (now() - p.t0 > PAIR_GIVE_UP_MS) { pairFail("Nobody allowed it in time. Run jarvis phone pair again for a new code."); return; }
      pairWaiting(p);
      p.timer = setTimeout(() => pairStep("/phone/pair/status", { code: p.code, operation_id: p.operationId }), PAIR_POLL_MS);
      return;
    }
    pairFail("JARVIS didn't pair this phone. Run jarvis phone pair again.");
  }
  function pairWaiting(p) {
    if (p.tick) return;
    el.pairForm.hidden = true; el.pairWait.hidden = false;
    text(el.pwMsg, "");
    // The console gate's countdown: an amber bar that runs out when the wait gives up.
    const tick = () => {
      text(el.pwTime, "waiting " + mmss(now() - p.t0).replace(/^0/, ""));
      el.pwBar.style.width = (100 * clamp(1 - (now() - p.t0) / PAIR_GIVE_UP_MS, 0, 1)).toFixed(1) + "%";
    };
    tick(); p.tick = setInterval(tick, 1000);
    announce("Waiting for your OK on the main computer.");
  }
  function pairFail(msg) {
    if (pairing) { clearTimeout(pairing.timer); clearInterval(pairing.tick); }
    pairing = null;
    el.pairWait.hidden = true; el.pairForm.hidden = false; el.pGo.disabled = false;
    pairNote(msg, "bad");
  }
  el.pGo.addEventListener("click", pairGo);
  el.pCode.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); try { el.pName.focus(); } catch (_) { /* fine */ } } });
  el.pName.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); pairGo(); } });
  el.pwCancel.addEventListener("click", () => {
    if (pairing) { clearTimeout(pairing.timer); clearInterval(pairing.tick); }
    pairing = null;
    el.pairWait.hidden = true; el.pairForm.hidden = false; el.pGo.disabled = false;
    pairNote("Stopped waiting. Answer N in the terminal on the main so the code can't be used.", "");
  });
  el.downRetry.addEventListener("click", () => { text(el.downWhen, "Trying…"); boot(); });

  // ---------- reachability, heartbeat ----------
  const beats = [];
  function reachable(ok) {
    reach = ok;
    if (!el.app.hidden) el.net.hidden = ok;
    renderLink();
  }
  function beat(ms) { beats.push(ms); if (beats.length > 24) beats.shift(); renderLink(); }
  function renderLink() {
    const last = beats.length ? beats[beats.length - 1] : 0;
    const st = !reach ? "bad" : last == null ? "stale" : "live";
    el.link.setAttribute("data-st", st);
    el.linkDot.setAttribute("data-st", st);
    text(el.linkMs, !reach ? "offline" : last ? Math.round(last) + " ms" : "– ms");
    el.link.setAttribute("aria-label", !reach ? "Link to JARVIS: unreachable" : last ? `Link to JARVIS: live, ${Math.round(last)} ms` : "Link to JARVIS");
    drawTrace(el.hb, beats);
  }
  el.netRetry.addEventListener("click", () => refresh());

  // ---------- overview ----------
  let refreshing = false;
  const hist = new Map();  // device id -> recent online readings, for its heartbeat
  const okDevice = (d) => d && typeof d === "object" && typeof d.id === "string" && d.id.length > 0;
  async function refresh() {
    if (refreshing || !me) return;
    refreshing = true;
    const t0 = now();
    try {
      const d = await api("/phone/api/overview", undefined, { timeout: 15000 });
      beat(Math.max(1, now() - t0));
      ov = {
        devices: Array.isArray(d.devices) ? d.devices.filter(okDevice) : [],
        sessions: Array.isArray(d.sessions) ? d.sessions.filter((s) => s && typeof s.id === "string") : [],
        ask: d.ask && typeof d.ask === "object" ? d.ask : { enabled: false },
      };
      loaded = true;
      for (const dv of ov.devices) {
        const a = hist.get(dv.id) || [];
        a.push(!!dv.online); if (a.length > 12) a.shift();
        hist.set(dv.id, a);
      }
      renderAll();
    } catch (e) {
      if (e.kind !== "auth") beat(null);
    } finally { refreshing = false; }
  }
  const dev = (id) => ov.devices.find((d) => d.id === id) || null;
  const label = (id) => { const d = dev(id); return (d && str(d.label, 80)) || id || "the computer"; };
  const mySession = (id, kind) => ov.sessions.find((s) => s.device === id && (!kind || s.kind === kind) && s.state !== "ended") || null;
  function sorted(list) {
    return list.slice().sort((a, b) => (b.role === "main") - (a.role === "main") || String(a.label || a.id).localeCompare(String(b.label || b.id)));
  }
  function renderAll() {
    renderDevices(); renderSessions(); renderDetail(); renderAskMeta(); renderPhone();
  }

  // Keep rows by key and only reorder when the order changed, so a focused row keeps focus.
  function keyed(list, cache, items, key, make, fill) {
    const ids = items.map(key);
    for (const it of items) { const k = key(it); let r = cache.get(k); if (!r) { r = make(it); cache.set(k, r); } fill(r, it); }
    for (const k of [...cache.keys()]) if (!ids.includes(k)) cache.delete(k);
    const want = ids.map((k) => cache.get(k).li), have = Array.from(list.children || []);
    if (want.length !== have.length || want.some((n, i) => n !== have[i])) list.replaceChildren(...want);
  }

  // ---------- devices ----------
  const devRows = new Map();
  function renderDevices() {
    const list = sorted(ov.devices), online = list.filter((d) => d.online).length;
    const d = new Date(now());
    text(el.dvMeta, loaded ? `read ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : "");
    text(el.stOnline, loaded ? `${online}/${list.length}` : "–");
    text(el.stSess, loaded ? String(ov.sessions.filter((s) => s.state !== "ended").length) : "–");
    el.dvEmpty.hidden = !(loaded && !list.length);
    keyed(el.dvList, devRows, list, (d) => d.id, makeDevRow, fillDevRow);
  }
  function makeDevRow(d) {
    const r = { dot: h("i", { class: "ld", "aria-hidden": "true" }), name: h("b", { class: "dev-name" }), sr: h("span", { class: "sr" }),
      meta: h("span", { class: "dev-meta mono" }), beat: h("canvas", { class: "beat", width: "56", height: "16", "aria-hidden": "true" }),
      caps: h("span", { class: "caps" }), cap: {} };
    for (const [k, ic] of CAPS) { const c = h("span", { class: "cap" }, icon(ic, true), h("span", { class: "sr" })); r.cap[k] = c; r.caps.append(c); }
    r.a = h("a", { class: "dev", href: "#/device/" + encodeURIComponent(d.id) }, r.dot,
      h("span", { class: "dev-t" }, r.name, r.sr, r.meta), h("span", { class: "dev-r" }, r.beat, r.caps), icon("chevron-right", true));
    r.li = h("li", null, r.a);
    return r;
  }
  function fillDevRow(r, d) {
    text(r.name, str(d.label, 80) || d.id);
    r.dot.setAttribute("data-st", d.online ? "live" : "off");
    const parts = [d.role === "main" ? "main" : "node", str(d.platform, 40), d.presence === "busy" ? "busy, awaiting contact" : d.online ? "seen " + ago(d.seen) : "last seen " + ago(d.seen)];
    text(r.meta, parts.filter(Boolean).join(" · "));
    const capsText = [];
    for (const [k, , word] of CAPS) {
      const on = !!(d.caps && d.caps[k]);
      r.cap[k].setAttribute("data-on", on ? "1" : "0");
      capsText.push((on ? "" : "no ") + word);
    }
    text(r.sr, `, ${d.presence === "busy" ? "busy, awaiting contact" : d.online ? "online" : "offline"}, ${capsText.join(", ")}`);
    drawPulse(r.beat, hist.get(d.id) || []);
  }

  // ---------- manage computers (read-only legacy fleet; GET /phone/api/fleet for the list) ----------
  // Invite, revoke and permission changes stay on the main PC. A promote control is shown only when
  // this phone's server record says manage_main. A stored flag or can_write does not grant it.
  const fleetRows = new Map();
  let fleet = null, fleetErr = "", fleetBusy = false, fleetAt = 0;
  let promote = null, promoteBusy = false, promoteView = null, candidateKey = "";
  const FLEET_LOOK = "This phone can look, not change. Adding, changing or removing a computer is finished on the main PC.";
  const FLEET_OWNER = "This phone may ask to promote a computer. JARVIS still checks the owner grant and a passkey before anything changes.";
  const FLEET_LIMIT = "Pairing this phone doesn't give it admin rights, so the steps that change the fleet stay on the main PC. Moving the main to another computer isn't available yet.";
  const FLEET_LIMIT_OWNER = "Pairing alone does not change MAIN. A passkey proof is sent to the server, and the server decides.";
  const FLEET_STATUS = { main: "MAIN", online: "Online", offline: "Offline", busy: "Busy, awaiting contact", waiting_for_first_contact: "Waiting to connect", invite_pending: "Invite pending", invite_expired: "Invite expired", not_enrolled: "Not connected", unconfigured: "Not set up" };
  const PENDING_STATES = new Set(["pending", "requested", "validating", "draining", "fencing", "granted_not_ready", "sending", "uncertain", "open"]);
  const REFUSED_STATES = new Set(["refused", "failed", "cancelled", "forbidden"]);
  const DONE_STATES = new Set(["succeeded", "completed", "committed", "done"]);
  function publicFleet(v, max) {
    return str(v, max).replace(/jarvis-invite-1:\S+/gi, "").replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, "").replace(/[ \t]{2,}/g, " ").trim();
  }
  function serverOwner() { return !!(me && me.manage_main === true); }
  function authorityView() {
    if (me && me.authority && typeof me.authority === "object" && !Array.isArray(me.authority)) return me.authority;
    if (fleet && fleet.authority && typeof fleet.authority === "object" && !Array.isArray(fleet.authority)) return fleet.authority;
    return null;
  }
  function transitionObject() {
    const a = authorityView();
    if (a && a.transition && typeof a.transition === "object") return a.transition;
    if (fleet && fleet.transition && typeof fleet.transition === "object") return fleet.transition;
    return null;
  }
  function classifyState(state) {
    const s = String(state || "").toLowerCase();
    if (PENDING_STATES.has(s)) return "pending";
    if (REFUSED_STATES.has(s)) return "refused";
    if (s === "stale") return "stale";
    return "";
  }
  function transitionKind(code, message) {
    const c = String(code || "").toLowerCase();
    const m = String(message || "").toLowerCase();
    if (c === "stale" || m.includes("moved on") || /\bstale\b/.test(m)) return "stale";
    if (c === "uncertain" || c === "pending") return "pending";
    if (c === "forbidden" || c === "owner_proof_required") return "refused";
    return "";
  }
  function authoritativeMainId() {
    const a = authorityView();
    return a && typeof a.main === "string" ? a.main : "";
  }
  function mainStatusLine() {
    const a = authorityView();
    if (!a) return "No authoritative MAIN status on this link.";
    if (a.main_ready === false || typeof a.main !== "string" || !a.main) return "No authoritative MAIN.";
    const id = str(a.main, 64);
    const row = (fleet && Array.isArray(fleet.devices) ? fleet.devices : []).find((d) => d && d.id === id);
    const fromMe = me && me.main && me.main.id === id ? str(me.main.label, 80) : "";
    const label = (row && publicFleet(row.label, 80)) || publicFleet(fromMe, 80) || id;
    return "MAIN is " + label + ".";
  }
  function mainReadyId() {
    const a = authorityView();
    if (!a || a.main_ready === false || typeof a.main !== "string" || !a.main) return "";
    return a.main;
  }
  // A local pending request settles only when the authoritative MAIN is that candidate,
  // or the server's terminal transition names that same candidate. Any other MAIN stays pending.
  function promotionBound(server, view) {
    if (!view || view.kind !== "pending") return true;
    const cand = server && typeof server.candidate === "string" ? server.candidate : "";
    return !!(view.candidate && cand && cand === view.candidate);
  }
  function promotionOutcome() {
    const server = transitionObject();
    const raw = String(server && (server.state || server.status) || "").toLowerCase();
    const serverKind = classifyState(raw);
    const reported = String(server && server.outcome || "").toLowerCase();
    const terminal = serverKind === "refused" || serverKind === "stale" ? serverKind
      : (DONE_STATES.has(raw) || reported === "new_main" ? "done" : "");
    const main = mainReadyId();
    const view = promoteView;
    if (view && view.candidate && main && main === view.candidate) return "done";
    if (terminal && promotionBound(server, view)) return terminal;
    if (view && view.kind === "pending" && view.candidate && main && main !== view.candidate && main !== view.source) return "uncertain";
    if (serverKind === "pending" || (view && view.kind === "pending")) return "pending";
    return "";
  }
  function applyPromoteOutcome(outcome) {
    if (!promoteView || promoteView.kind !== "pending") return;
    if (outcome !== "done" && outcome !== "refused" && outcome !== "stale") return;
    const detail = promoteView.detail || "";
    const candidate = promoteView.candidate;
    const source = promoteView.source;
    promoteView = outcome === "done" ? null : { kind: outcome, detail, candidate, source };
    if (outcome === "done" && el.flPromoteMsg && el.flPromoteMsg.textContent === "Promotion pending.") {
      text(el.flPromoteMsg, "JARVIS finished that request.");
    }
  }
  function transitionLine() {
    const outcome = promotionOutcome();
    applyPromoteOutcome(outcome);
    if (outcome === "done") return "";
    if (outcome === "uncertain") return "Promotion pending. This request is uncertain.";
    const server = transitionObject();
    const kind = outcome || (promoteView && promoteView.kind) || "";
    if (kind === "pending") return "Promotion pending.";
    if (kind === "stale") return "Promotion stale. Nothing was changed.";
    if (kind === "refused") {
      const why = server && (typeof server.outcome === "string" ? server.outcome : typeof server.error === "string" ? server.error : "");
      const bit = publicFleet(why || (promoteView && promoteView.detail) || "", 160);
      return bit ? "Promotion refused. " + bit : "Promotion refused.";
    }
    return "";
  }
  function fleetCandidates() {
    const list = fleet && Array.isArray(fleet.devices) ? fleet.devices.filter((d) => d && typeof d.id === "string") : [];
    const current = authoritativeMainId();
    return list.filter((d) => {
      if (current && d.id === current) return false;
      if (d.role === "main" || d.status === "main") return false;
      const profile = typeof d.profile === "string" ? d.profile : "";
      if (profile && profile !== "pc" && profile !== "control") return false;
      return true;
    }).sort((a, b) => str(a.label || a.id).localeCompare(str(b.label || b.id)));
  }
  function promoteAction() {
    const a = authorityView();
    return a && a.main_ready === false ? "recover" : "make_main_now";
  }
  function renderPromote() {
    if (!el.flMainStatus) return;
    text(el.flMainStatus, mainStatusLine());
    text(el.flTransition, transitionLine());
    if (el.flNote) text(el.flNote, serverOwner() ? FLEET_OWNER : FLEET_LOOK);
    if (el.flLimit) text(el.flLimit, serverOwner() ? FLEET_LIMIT_OWNER : FLEET_LIMIT);
    const owner = serverOwner();
    if (el.flPromote) el.flPromote.hidden = !owner;
    if (!owner || !el.flCandidate) return;
    const list = fleetCandidates();
    const key = list.map((d) => d.id + "\n" + str(d.label, 80)).join("|");
    if (key !== candidateKey) {
      candidateKey = key;
      const selected = el.flCandidate.value || (promote && promote.candidate) || "";
      el.flCandidate.replaceChildren(...list.map((d) => h("option", { value: d.id }, publicFleet(d.label, 80) || str(d.id, 64))));
      el.flCandidate.value = list.some((d) => d.id === selected) ? selected : (list[0] ? list[0].id : "");
    }
    if (el.flPromoteGo) {
      text(el.flPromoteGo, promote && promote.previewId ? "Try again" : "Promote to main");
      el.flPromoteGo.disabled = promoteBusy || !el.flCandidate.value;
    }
  }
  async function loadFleet() {
    if (fleetBusy) return;
    fleetBusy = true; renderFleet();
    try {
      fleet = await api("/phone/api/fleet");
      fleetErr = ""; fleetAt = now();
      try {
        const who = await api("/phone/api/me");
        if (who && typeof who === "object") me = who;
      } catch (e) { /* the list stands; the MAIN line keeps the last server record */ }
    }
    catch (e) { fleetErr = e && e.kind === "net" ? UNREACHABLE : str(e && e.message) || "Couldn't load the computers."; }
    fleetBusy = false; renderFleet();
  }
  async function promoteGo() {
    if (!serverOwner() || promoteBusy) return;
    const candidate = el.flCandidate ? el.flCandidate.value : "";
    if (!candidate) return;
    if (!promote || promote.candidate !== candidate) promote = { candidate, previewOp: operationId(), confirmOp: "", previewId: "", proof: null };
    promoteBusy = true;
    text(el.flPromoteMsg, "Asking JARVIS…");
    renderFleet();
    try {
      if (!promote.previewId) {
        const preview = await api("/phone/api/main/preview", {
          operation_id: promote.previewOp, action: promoteAction(), candidate, profile: "control",
        });
        const opt = passkeyOptions(preview);
        if (!opt) {
          promote = null;
          text(el.flPromoteMsg, "Passkey checks are not configured. Nothing was changed.");
          return;
        }
        const proof = await readPasskey(opt);
        if (!proof) {
          promote = null;
          text(el.flPromoteMsg, "This phone couldn't use a passkey. Nothing was changed.");
          return;
        }
        promote.previewId = str(preview && preview.preview_id, 80);
        promote.proof = proof;
        if (!promote.confirmOp) promote.confirmOp = operationId();
      }
      const done = await api("/phone/api/main/confirm", {
        operation_id: promote.confirmOp, preview_id: promote.previewId, passkey_proof: promote.proof,
      });
      const chosen = promote.candidate;
      const source = authoritativeMainId();
      const reported = String(done && (done.state || done.status) || "").toLowerCase();
      const reportedKind = classifyState(reported);
      const reportedDone = DONE_STATES.has(reported) || String(done && done.outcome || "").toLowerCase() === "new_main";
      promote = null;
      promoteView = { kind: reportedKind || (reportedDone ? "done" : "pending"), detail: "", candidate: chosen, source };
      text(el.flPromoteMsg, promoteView.kind === "pending" ? "Promotion pending." : "JARVIS accepted the request.");
    } catch (e) {
      if (e && e.kind === "auth") return;
      const code = e && e.data && e.data.code;
      const kind = transitionKind(code, e && e.message);
      const keep = e && (e.kind === "net" || code === "uncertain");
      if (kind === "stale" || code === "expired") {
        promote = null;
        promoteView = kind === "stale" ? { kind: "stale", detail: "" } : null;
      } else if (kind === "refused") {
        promote = null;
        promoteView = { kind: "refused", detail: str(e && e.message, 180) };
      } else if (keep || kind === "pending") {
        promoteView = { kind: "pending", detail: "" };
      }
      text(el.flPromoteMsg, str(e && e.message, 200) || "JARVIS didn't change MAIN.");
    } finally {
      promoteBusy = false;
      renderFleet();
    }
  }
  function fleetCaps(d) {
    const titles = new Map();
    for (const c of Array.isArray(fleet && fleet.capabilities) ? fleet.capabilities : []) if (c && typeof c.id === "string" && c.id !== "locate") titles.set(c.id, publicFleet(c.title, 60) || c.id);
    return (Array.isArray(d.capabilities) ? d.capabilities : []).map((c) => (typeof c === "string" ? c : c && c.id)).filter((id) => typeof id === "string" && id !== "locate").map((id) => titles.get(id) || id);
  }
  function makeFleetRow() {
    const r = { dot: h("i", { class: "ld", "aria-hidden": "true" }), name: h("b", { class: "dev-name" }), meta: h("span", { class: "dev-meta mono" }), caps: h("span", { class: "fl-caps" }), next: h("span", { class: "fl-next" }) };
    r.li = h("li", null, h("div", { class: "fl-row" }, r.dot, h("span", { class: "dev-t" }, r.name, r.meta, r.next, r.caps)));
    return r;
  }
  function fillFleetRow(r, d) {
    text(r.name, publicFleet(d.label, 160) || str(d.id, 64));
    r.dot.setAttribute("data-st", d.status === "main" || d.online ? "live" : "off");
    const kind = d.role === "main" || d.status === "main" ? "Main" : "Node";
    const state = d.presence === "busy" ? "busy" : d.status;
    const known = state != null && Object.prototype.hasOwnProperty.call(FLEET_STATUS, state);
    text(r.meta, [kind, publicFleet(d.profile_label, 40) || publicFleet(d.profile, 20), known ? FLEET_STATUS[state] : "Unknown"].filter(Boolean).join(" · "));
    const main = d.role === "main" || d.status === "main";
    const caps = main ? [] : fleetCaps(d);
    text(r.caps, main ? "" : caps.length ? "May: " + caps.join(", ") : "No permissions granted");
    const step = publicFleet(d.step, 180);
    text(r.next, main || d.status === "online" || !step ? "" : "Next: " + step);
  }
  function renderFleet() {
    if (!el.flList) return;
    const list = fleet && Array.isArray(fleet.devices) ? fleet.devices.filter((d) => d && typeof d.id === "string") : [];
    el.flList.hidden = !list.length;
    keyed(el.flList, fleetRows, list.slice().sort((a, b) => (a.status === "main" ? 0 : 1) - (b.status === "main" ? 0 : 1) || str(a.label || a.id).localeCompare(str(b.label || b.id))), (d) => d.id, makeFleetRow, fillFleetRow);
    el.flEmpty.hidden = !(fleet && !list.length);
    if (fleet && !list.length) text(el.flEmpty, "No computers are set up yet. Add one on the main PC, in Connections.");
    text(el.flState, fleetBusy && !fleet ? "Loading…" : fleetErr ? (fleet ? "Couldn't refresh, showing the last reading. " : "") + fleetErr : "");
    el.flRetryRow.hidden = !fleetErr;
    const age = fleet && fleetAt ? Math.max(0, now() - fleetAt) : 0;
    text(el.flMeta, fleet && fleetAt ? "read " + ago(fleetAt) + (fleetErr || age > 60000 ? " · stale" : "") : "");
    const steps = Array.isArray(fleet && fleet.guide) ? fleet.guide.filter((s) => s && typeof s.title === "string") : [];
    el.flGuideWrap.hidden = !steps.length;
    if (steps.length) el.flGuide.replaceChildren(...steps.map((s) => h("li", null,
      h("b", { text: publicFleet(s.title, 80) }),
      s.who ? h("span", { class: "fl-who", text: "Who: " + publicFleet(s.who, 40) }) : null,
      h("span", { class: "fl-step", text: publicFleet(s.body, 400) }))));
    renderPromote();
  }
  el.flRetry.addEventListener("click", () => loadFleet());
  if (el.flPromoteGo) el.flPromoteGo.addEventListener("click", () => { promoteGo(); });
  setInterval(() => { if (!repairing && me && cur.name === "fleet" && !document.hidden && !fleetBusy) loadFleet(); }, 20000);

  // ---------- this phone's sessions ----------
  const sessRowsA = new Map(), sessRowsB = new Map();
  const kindWord = (k) => (k === "camera" ? "Camera" : "Screen");
  function renderSessions() {
    const list = ov.sessions.filter((s) => s.state !== "ended");
    el.dvSess.hidden = !list.length;
    keyed(el.dvSessList, sessRowsA, list, (s) => s.id, makeSessRow, fillSessRow);
    keyed(el.pnSess, sessRowsB, list, (s) => s.id, makeSessRow, fillSessRow);
    el.pnSessEmpty.hidden = list.length > 0;
  }
  function makeSessRow(s) {
    const r = { t: h("span", { class: "srow-t" }), m: h("span", { class: "srow-m mono" }) };
    const open = h("button", { type: "button", class: "mini" }, "Open"), end = h("button", { type: "button", class: "mini" }, "End");
    open.addEventListener("click", () => reattach(r.s));
    end.addEventListener("click", () => stopSession(r.s, end));
    r.li = h("li", { class: "srow" }, h("i", { class: "ld", "data-st": "live", "aria-hidden": "true" }), h("span", { class: "srow-tt" }, r.t, r.m), open, end);
    return r;
  }
  function fillSessRow(r, s) {
    r.s = s;
    text(r.t, `${kindWord(s.kind)} · ${label(s.device)}`);
    text(r.m, `${str(s.state, 20) || "live"} · ${str(s.quality, 10) || "low"} · since ${ago(s.started)}`.replace("since now", "just now"));
  }
  function reattach(s) {
    if (!s) return;
    want = { device: s.device, kind: s.kind === "camera" ? "camera" : "screen", session: s };
    go(`#/${s.kind === "camera" ? "camera" : "control"}/${encodeURIComponent(s.device)}`);
  }
  async function stopSession(s, btn) {
    if (!s) return;
    if (btn) btn.disabled = true;
    try { await api("/phone/api/session/stop", { session: s.id }, { timeout: 10000 }); } catch (e) { /* refresh shows what's left */ }
    refresh();
  }

  // ---------- routing ----------
  let cur = { name: "" }, want = null;
  function parse() {
    const m = /^#\/(devices|ask|phone|device|control|camera|fleet)(?:\/(.+))?$/.exec(location.hash || "");
    if (!m) return { name: "devices", id: "" };
    let id = "";
    try { id = m[2] ? decodeURIComponent(m[2]) : ""; } catch (e) { return { name: "devices", id: "" }; }
    if (["device", "control", "camera"].includes(m[1]) && !id) return { name: "devices", id: "" };
    return { name: m[1], id };
  }
  function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
  function route(first) {
    if (!me) return;
    const r = parse();
    const kind = r.name === "control" ? "screen" : r.name === "camera" ? "camera" : "";
    if (sess && !(kind === sess.kind && r.id === sess.device)) disconnect("Ended when you left the screen.");
    if (starting && !(kind === sessKind && r.id === sessDevice)) retireStart();  // its late reply is stopped
    const asked = want && want.kind === kind && want.device === r.id;
    if (kind && !sess && !asked && !(kind === sessKind && r.id === sessDevice && (starting || ended))) {
      // A screen or camera never starts from a link, a reload or Back: only from a tap.
      go("#/device/" + encodeURIComponent(r.id));
      return;
    }
    const changed = r.name !== cur.name || r.id !== cur.id;
    cur = r;
    for (const k of Object.keys(scr)) scr[k].hidden = k !== r.name;
    el.app.setAttribute("data-screen", r.name);
    for (const t of ["devices", "ask", "phone"]) {
      const a = el["tab" + t[0].toUpperCase() + t.slice(1)];
      if (TAB_OF[r.name] === t) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    if (r.name === "device") renderDetail();
    if (r.name === "phone") renderPhone();
    if (r.name === "fleet") loadFleet();
    if (kind) {
      const w = asked ? want : null;
      want = null;
      if (w && w.session) attachTo(w.session, kind);
      else if (w) startSession(r.id, kind);
      else renderSess();
    }
    if (changed && !first) {
      try { el.main.scrollTop = 0; } catch (e) { /* not scrollable */ }
      const head = $(HEADS[r.name]);
      try { if (head) head.focus({ preventScroll: true }); } catch (e) { /* fine */ }
    }
    if (!kind && me && changed && !first) refresh();
  }
  addEventListener("hashchange", () => route());

  // ---------- device detail ----------
  const netOf = (d) => d.network && typeof d.network === "object" ? d.network : {};
  function factRow(row, dd, v) { const s = v == null || v === "" ? "" : String(v).slice(0, 120); row.hidden = !s; text(dd, s); }
  function renderDetail() {
    if (cur.name !== "device") return;
    const d = dev(cur.id);
    el.dSess.hidden = true;
    if (!d) {
      text(el.dH, cur.id); text(el.dSub, ""); el.dDot.setAttribute("data-st", "off");
      text(el.dStatus, loaded ? "Not in the cluster any more" : "Loading…");
      for (const b of [el.dControl, el.dCamera]) b.disabled = true;
      try { if (window.JarvisMeters && el.dMeters) window.JarvisMeters.clear(el.dMeters); } catch (e) { /* leave the screen usable */ }
      return;
    }
    text(el.dH, str(d.label, 80) || d.id);
    el.dDot.setAttribute("data-st", d.online ? "live" : "off");
    text(el.dSub, `${d.role === "main" ? "main" : "node"}${d.platform ? " · " + str(d.platform, 40) : ""}`);
    text(el.dStatus, d.presence === "busy" ? "Busy, awaiting contact · last seen " + ago(d.seen) : d.online ? "Online · seen " + ago(d.seen) : "Offline · last seen " + ago(d.seen));
    text(el.dRole, d.role === "main" ? "Main computer" : "Node");
    text(el.dPlatform, str(d.platform, 40) || "–");
    const n = netOf(d);
    factRow(el.dRSsid, el.dSsid, str(n.ssid, 64));
    factRow(el.dRLan, el.dLan, str(n.lan_ip, 64));
    factRow(el.dRTs, el.dTs, str(n.tailscale_ip, 64));
    const why = d.why && typeof d.why === "object" ? d.why : {};
    const caps = d.caps && typeof d.caps === "object" ? d.caps : {};
    const live = (k) => mySession(d.id, k);
    el.dControl.disabled = !caps.remote && !live("screen");
    text(el.dControlWhy, live("screen") ? "Your session is running: open it" : caps.remote ? "See the screen, drive its mouse and keyboard" : str(why.remote) || "Not available");
    el.dCamera.disabled = !caps.camera && !live("camera");
    text(el.dCameraWhy, live("camera") ? "Your camera session is running: open it" : caps.camera ? "See through its webcam; it shows a notice there" : str(why.camera) || "Not available");
    const s = mySession(d.id);
    if (s) { el.dSess.hidden = false; el.dSess.s = s; text(el.dSessT, `Your ${kindWord(s.kind).toLowerCase()} session here is ${str(s.state, 20) || "running"}.`); }
    try { if (window.JarvisMeters && el.dMeters) window.JarvisMeters.render(el.dMeters, d.meters || null); } catch (e) { /* a reading must not blank the device screen */ }
    renderQuality();
  }
  function openSession(kind) {
    const id = cur.id, s = mySession(id, kind);
    want = s ? { device: id, kind, session: s } : { device: id, kind };
    go(`#/${kind === "camera" ? "camera" : "control"}/${encodeURIComponent(id)}`);
  }
  el.dControl.addEventListener("click", () => openSession("screen"));
  el.dCamera.addEventListener("click", () => openSession("camera"));
  el.dSessOpen.addEventListener("click", () => reattach(el.dSess.s));
  el.dSessEnd.addEventListener("click", () => stopSession(el.dSess.s, el.dSessEnd));
  const QUALITY = ["low", "medium", "high"];
  const prefQuality = () => { const q = store.get("jarvis.phone.quality", "low"); return QUALITY.includes(q) ? q : "low"; };
  function renderQuality() { const q = prefQuality(); for (const k of QUALITY) el["dQ" + k[0].toUpperCase() + k.slice(1)].setAttribute("aria-pressed", String(k === q)); }
  for (const k of QUALITY) el["dQ" + k[0].toUpperCase() + k.slice(1)].addEventListener("click", () => { store.set("jarvis.phone.quality", k); renderQuality(); });

  // ---------- canvas: dot matrix ----------
  // Every figure is dots on a grid, like the console's signal tiles: lit at the ink, unlit faint.
  let pal = null, palKey = "";
  function palette() {
    const key = document.documentElement.getAttribute("data-theme") || "";
    if (pal && key === palKey) return pal;
    let cs = null;
    try { cs = getComputedStyle(document.documentElement); } catch (e) { cs = null; }
    const v = (n, d) => ((cs && cs.getPropertyValue(n)) || "").trim() || d;
    palKey = key;
    pal = { ink: v("--ink", "#ededed"), ink2: v("--ink-2", "#a8a8a8"), ink3: v("--ink-3", "#8a8a8a"), off: v("--dot-off", "rgba(255,255,255,.1)"),
      bad: v("--bad", "#ef7a6c"), bg: v("--bg", "#000") };
    return pal;
  }
  function surface(c) {
    if (!c || typeof c.getContext !== "function") return null;
    const w = c.clientWidth, hh = c.clientHeight;
    if (!w || !hh) return null;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(hh * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(hh * dpr); }
    const g = c.getContext("2d");
    if (!g) return null;
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, hh); g.globalAlpha = 1;
    return { g, w, h: hh, dpr };
  }
  function dot(g, x, y, r, color, a) { g.globalAlpha = a == null ? 1 : a; g.fillStyle = color; g.beginPath(); g.arc(x, y, r, 0, 6.2832); g.fill(); }
  // The header's link trace: one heartbeat blip per overview round trip, height by its time; a
  // failed poll is a red dot on the baseline.
  function drawTrace(c, samples) {
    const s = surface(c);
    if (!s) return;
    const { g, w, h: hh } = s, P = palette(), mid = hh / 2, step = 3;
    for (let x = 1.5; x < w; x += step) dot(g, x, mid, 0.7, P.off);
    const span = step * 6, n = Math.max(1, Math.floor(w / span)), rec = samples.slice(-n);
    rec.forEach((ms, i) => {
      const cx = w - (rec.length - i) * span + span / 2;
      if (ms == null) { dot(g, cx, mid, 1.3, P.bad); return; }
      const amp = clamp(ms / 300, 0.25, 1) * (mid - 1.5);
      [0, -0.3, 1, -0.55, 0.15, 0].forEach((k, j) => {
        const x = cx + (j - 2.5) * step, y = mid - k * amp, pts = Math.max(1, Math.round(Math.abs(y - mid) / 2.2));
        for (let p = 0; p <= pts; p++) dot(g, x, mid + ((y - mid) * p) / pts, 0.9, i === rec.length - 1 ? P.ink : P.ink2);
      });
    });
    g.globalAlpha = 1;
  }
  // A device row's pulse: twelve slots, newest on the right; a lit beat for each poll where it was
  // online, one unlit dot where it was not (or before the app was watching).
  function drawPulse(c, samples) {
    const s = surface(c);
    if (!s) return;
    const { g, w, h: hh } = s, P = palette(), mid = hh / 2, slots = 12, span = w / slots;
    for (let i = 0; i < slots; i++) {
      const k = samples.length - slots + i, x = span * i + span / 2;
      if (k < 0 || !samples[k]) { dot(g, x, mid, 0.8, P.off); continue; }
      const col = k === samples.length - 1 ? P.ink : P.ink3;
      for (const dy of [-6, -3, 0, 3, 6]) dot(g, x, mid + dy, 0.85, col, Math.abs(dy) === 6 ? 0.45 : 1);
    }
    g.globalAlpha = 1;
  }

  // ---------- ask ----------
  let asking = false;
  const askTarget = $("a-target");
  let askTargetKey = "";
  function askTargetChoice(d) {
    // The option stays disabled while the node is not proved online, including when it is busy.
    const name = str(d.label, 80) || d.id;
    if (d.online) return { disabled: false, label: name };
    if (d.presence === "busy") return { disabled: true, label: name + " (busy, awaiting contact)" };
    return { disabled: true, label: name + " (offline)" };
  }
  function renderAskTargets() {
    const list = sorted(ov.devices);
    const key = JSON.stringify(list.map((d) => [d.id, d.online, d.presence, d.label]));
    if (key === askTargetKey) return;
    askTargetKey = key;
    const selected = askTarget.value || store.get("jarvis.phone.target", "");
    askTarget.replaceChildren(h("option", { value: "" }, "Main computer"), ...list.map((d) => {
      const choice = askTargetChoice(d);
      return h("option", { value: d.id, disabled: choice.disabled }, choice.label);
    }));
    askTarget.value = list.some((d) => d.id === selected) ? selected : "";
  }

  askTarget.addEventListener("change", () => { store.set("jarvis.phone.target", askTarget.value || ""); renderAskMeta(); });
  function renderAskMeta() {
    renderAskTargets();
    askTarget.disabled = asking;
    const on = !!(ov.ask && ov.ask.enabled);
    text(el.akMeta, !loaded ? "" : on ? "to " + (askTarget.value ? label(askTarget.value) : ((me && me.main && str(me.main.label, 60)) || "the main")) : "off");
    el.aText.disabled = loaded && !on;
    el.aText.setAttribute("placeholder", loaded && !on ? (str(ov.ask && ov.ask.why) || "Ask is off on the main computer") : "Ask JARVIS…");
    el.aSend.disabled = asking || (loaded && !on) || !el.aText.value.trim();
  }
  function addMsg(who, body, st) {
    const head = h("span", { class: "msg-h mono" }, h("b", null, who === "you" ? "You" : "JARVIS"), h("time", null, timeNow()));
    const b = h("p", { class: "msg-b" });
    const li = h("li", { class: "msg " + who }, head, b);
    const m = { li, b, flow: null, act: null };
    setMsg(m, body, st);
    el.aThread.append(li);
    el.aEmpty.hidden = true;
    keepInView();
    return m;
  }
  function setMsg(m, body, st) { text(m.b, body); m.li.setAttribute("data-st", st || ""); }
  function timeNow() { const d = new Date(now()); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function nearBottom() {
    try { return el.main.scrollHeight - el.main.scrollTop - (el.main.clientHeight || 0) < 160; } catch (e) { return true; }
  }
  function keepInView() { try { el.main.scrollTop = el.main.scrollHeight; } catch (e) { /* fine */ } }

  // The decision tree: the steps the main reports for this request (recall, route, brain, tools, plan).
  // The server sends only labels and short summaries, and this draws them as text. Nothing is made up
  // here: no steps from the server means no tree, only "Working…".
  function cleanSteps(list) {
    const out = [];
    if (!Array.isArray(list)) return out;
    for (const s of list) {
      if (out.length >= MAX_NODES) break;
      if (!s || typeof s !== "object" || Array.isArray(s)) continue;
      const id = str(s.id, 48);
      if (!id) continue;
      out.push({ id, parent: str(s.parent, 48), label: clean(str(s.label, 80)) || "Step", detail: clean(str(s.detail, 120)),
        state: s.state === "run" || s.state === "fail" ? s.state : "done", kind: s.kind === "tool" ? "tool" : "step" });
    }
    return out;
  }
  const STEP_WORD = { run: "running", done: "done", fail: "failed" };
  function activeStage(steps) {
    for (let i = steps.length - 1; i >= 0; i--) if (steps[i].state === "run") return steps[i].label;
    return "";
  }
  function flowFor(m) {
    if (m.flow) return m.flow;
    const title = h("span", { class: "flow-t" }), meta = h("span", { class: "flow-n mono" });
    const tree = h("ol", { class: "tree" });
    const box = h("details", { class: "flow", "aria-live": "off", open: true }, h("summary", { class: "flow-s" }, title, meta), tree);
    m.li.append(box);
    m.flow = { box, title, meta, tree, nodes: new Map(), final: false };
    return m.flow;
  }
  function nodeFor(f, s) {
    let n = f.nodes.get(s.id);
    if (n) return n;
    n = { li: h("li", { class: "node" }), pip: h("i", { class: "pip", "aria-hidden": "true" }), nm: h("span", { class: "nm" }),
      sr: h("span", { class: "sr" }), dt: h("span", { class: "dt" }), sub: null };
    n.li.append(n.pip, h("div", { class: "nb" }, n.nm, n.sr, n.dt));
    f.nodes.set(s.id, n);
    return n;
  }
  function drawNode(f, s) {
    const n = nodeFor(f, s);
    n.li.setAttribute("data-st", s.state); n.li.setAttribute("data-kind", s.kind);
    text(n.nm, s.label); text(n.sr, " (" + STEP_WORD[s.state] + ")"); text(n.dt, s.detail);
    return n;
  }
  function fillTree(f, ol, kids, parent, depth, seen) {
    const want = [];
    for (const s of kids.get(parent) || []) {
      if (seen.has(s.id)) continue;
      seen.add(s.id);
      const n = drawNode(f, s);
      if (depth < 5 && (kids.get(s.id) || []).length) {
        if (!n.sub) { n.sub = h("ol", { class: "tree sub" }); n.li.append(n.sub); }
        const inner = fillTree(f, n.sub, kids, s.id, depth + 1, seen);
        if (inner.length !== n.sub.children.length || !inner.every((li, i) => n.sub.children[i] === li)) n.sub.replaceChildren(...inner);
      } else if (n.sub) { n.sub.remove(); n.sub = null; }
      want.push(n.li);
    }
    return want;
  }
  function renderFlow(m, steps, o) {
    if (!steps.length) return;
    const f = flowFor(m);
    const ids = new Set(steps.map((s) => s.id)), kids = new Map();
    for (const s of steps) {
      const p = s.parent !== s.id && ids.has(s.parent) ? s.parent : "";
      if (!kids.has(p)) kids.set(p, []);
      kids.get(p).push(s);
    }
    const seen = new Set(), stick = nearBottom();
    const want = fillTree(f, f.tree, kids, "", 0, seen);
    for (const s of steps) if (!seen.has(s.id)) { seen.add(s.id); const n = drawNode(f, s); if (n.sub) { n.sub.remove(); n.sub = null; } want.push(n.li); }
    const same = want.length === f.tree.children.length && want.every((li, i) => f.tree.children[i] === li);
    if (!same) f.tree.replaceChildren(...want);
    const failed = steps.some((s) => s.state === "fail");
    text(f.title, o.working ? "Decision path" : failed ? "Where it stopped" : "How it got there");
    text(f.meta, steps.length + (steps.length === 1 ? " step" : " steps") + (isFinite(o.secs) ? " · " + o.secs + " s" : ""));
    if (!o.working && !f.final) {  // finished: fold it away so the answer leads, unless something failed
      f.final = true;
      if (!failed) f.box.removeAttribute("open");
    }
    if (stick) keepInView();
  }
  function setAct(m, label, fn) {
    if (m.act) m.act.remove();
    m.act = null;
    if (!label) return;
    m.act = h("button", { type: "button", class: "msg-act" }, label);
    m.act.addEventListener("click", () => { if (!asking) fn(); });
    m.li.append(m.act);
    keepInView();
  }
  let heldAsk = null;
  function saveAsk(job, t, at, target) { store.set(ASK_KEY, job ? JSON.stringify({ job, text: t, at, target: target || "" }) : ""); }
  function rememberAsk(key, text, target) {
    const rec = { key: String(key || ""), text: str(text, 2000), at: now(), target: str(target, 64), held: true };
    if (!OP_RE.test(rec.key)) return;
    heldAsk = rec;
    store.set(ASK_KEY, JSON.stringify(rec));
    if (rec.target) store.set("jarvis.phone.target", rec.target);
  }

  function paintAsk(m, d, t0) {
    renderUsage(m, d && d.usage, true);
    const tr = d && d.trace && typeof d.trace === "object" ? d.trace : {};
    const steps = cleanSteps(tr.steps), stage = activeStage(steps), secs = Math.max(0, Math.round((now() - t0) / 1000));
    setMsg(m, stage ? "Working · " + stage : tr.streaming ? "Writing the reply…" : `Working… ${secs} s`, "busy");
    renderFlow(m, steps, { working: true, secs });
    m.rev = isFinite(tr.rev) ? Math.max(0, Math.floor(tr.rev)) : m.rev || 0;
  }
  function renderUsage(m, usage, pending = false) {
    if (!m.cost) {
      const title = h("summary", null, "Usage & cost");
      m.costBody = h("div", { class: "request-cost-body" });
      m.cost = h("details", { class: "request-cost" }, title, m.costBody);
      m.li.append(m.cost);
    }
    const valid = (v) => typeof v === "number" && Number.isFinite(v) && v >= 0;
    if (!usage || typeof usage !== "object") {
      m.costBody.replaceChildren(h("p", null, pending ? "Usage will be available when the request finishes." : "Usage was not reported for this request.")); return;
    }
    const rows = [];
    const row = (name, value) => rows.push(h("div", null, h("dt", null, name), h("dd", null, value)));
    if (valid(usage.usd)) row("API-equivalent estimate", "$" + usage.usd.toFixed(6) + " USD");
    if (valid(usage.tokens)) row("Tokens", Math.round(usage.tokens).toLocaleString());
    if (valid(usage.calls)) row("Model calls", String(Math.round(usage.calls)));
    if (typeof usage.engine === "string" && usage.engine) row("Engine", str(usage.engine, 60));
    m.costBody.replaceChildren(h("dl", null, ...rows), h("p", { class: "fine" },
      usage.calls === 0 && usage.tokens === 0 ? "Local request; no metered model calls." : "Estimated API-equivalent value, not an extra charge to your subscription. Missing provider usage may make this estimate incomplete."));
  }
  function finishAsk(m, d, t0) {
    saveAsk("");
    renderUsage(m, d && d.usage);
    const tr = d && d.trace && typeof d.trace === "object" ? d.trace : {};
    renderFlow(m, cleanSteps(tr.steps), { working: false, secs: isFinite(tr.elapsed) ? Math.round(tr.elapsed) : Math.round((now() - t0) / 1000) });
    const reply = str(d && d.reply, 20000);
    const waiting = !!(d && (d.waiting || d.confirm)) || WAITING.test(reply);
    setMsg(m, reply || (waiting ? "Waiting for your OK on the PC." : "Done."), waiting ? "needs" : "ok");
    setAct(m, "");
    if (waiting) announce("Waiting for your OK on the PC.");
  }
  // Poll a running request until it ends. The server answers as soon as a step lands, so the tree
  // moves with the work; a dropped connection retries, then offers "Check again" (the job lives on).
  async function pumpAsk(m, d, t0, t) {
    let misses = 0;
    setAct(m, "");
    for (;;) {
      if (!d || !d.pending || !d.job) { finishAsk(m, d, t0); return; }
      const job = String(d.job);
      saveAsk(job, t, t0, m.target);
      paintAsk(m, d, t0);
      if (now() - t0 > ASK_GIVE_UP_MS) {
        setMsg(m, "Still working. The answer will show on the PC.", "needs");
        setAct(m, "Check again", () => { asking = true; renderAskMeta(); pumpAsk(m, { pending: true, job, trace: { rev: 0 } }, now(), t).finally(() => { asking = false; renderAskMeta(); }); });
        return;
      }
      await sleep(ASK_POLL_MS);
      try {
        d = await api("/phone/api/ask/" + encodeURIComponent(job) + "?after=" + (m.rev || 0), undefined, { timeout: 20000 });
        misses = 0;
      } catch (e) {
        if (e.kind === "auth" || isCsrf(e)) return;
        if (e.kind === "net" && ++misses <= 5) { d = { pending: true, job, trace: { rev: m.rev || 0 } }; await sleep(ASK_POLL_MS * misses); continue; }
        if (e.kind === "net") {
          setMsg(m, "Lost contact with JARVIS — is Tailscale on? Your request may still be running.", "bad");
          setAct(m, "Check again", () => { asking = true; renderAskMeta(); pumpAsk(m, { pending: true, job, trace: { rev: 0 } }, t0, t).finally(() => { asking = false; renderAskMeta(); }); });
          return;
        }
        saveAsk("");
        renderUsage(m, e.data && e.data.usage);
      const tr = e.data && e.data.trace;
        if (tr) renderFlow(m, cleanSteps(tr.steps), { working: false, secs: isFinite(tr.elapsed) ? Math.round(tr.elapsed) : NaN });
        setMsg(m, e.status === 404 ? "JARVIS no longer has that request. Ask again." : e.message, "bad");
        if (e.status === 502) setAct(m, "Try again", () => send(m, t, newKey(), m.target || ""));  // it failed on the PC: a new request
        return;
      }
    }
  }
  function newKey() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, ""); } catch (e) { /* fall through */ }
    return (now().toString(36) + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2)).slice(0, 32);
  }
  // Send once. The key lets a retry after a dropped reply find the same request instead of making a second.
  async function send(m, t, key, target = askTarget.value || "") {
    m.target = target;
    asking = true; renderAskMeta();
    const t0 = now();
    setMsg(m, "Working…", "busy");
    setAct(m, "");
    try {
      const d = await api("/phone/api/ask", { text: t, key, ...(target ? { device: target } : {}) }, { timeout: 20000 });
      await pumpAsk(m, d, t0, t);
    } catch (e) {
      if (e.kind === "auth") return;
      if (isCsrf(e)) { rememberAsk(key, t, target); return; }
      renderUsage(m, e.data && e.data.usage);
      const tr = e.data && e.data.trace;
      if (tr) renderFlow(m, cleanSteps(tr.steps), { working: false, secs: isFinite(tr.elapsed) ? Math.round(tr.elapsed) : NaN });
      setMsg(m, e.message, "bad");
      if (e.status === 400) { if (!el.aText.value) { el.aText.value = t; autosize(); countAsk(); } }
      // A request that failed on the PC is over: trying again is a new one. A lost reply or a busy PC
      // retries under the same key, so it can never run twice.
      else if (e.kind === "net" || e.status === 409 || e.status >= 500) setAct(m, "Try again", () => send(m, t, e.data && e.data.job ? newKey() : key, target));
    } finally {
      asking = false; renderAskMeta();
    }
  }
  function ask() {
    const t = el.aText.value.trim();
    if (!t || asking) return;
    if (t.length > MAX_ASK) { text(el.aNote, "That's over 2000 characters."); return; }
    const target = askTarget.value || "";
    let key = newKey();
    if (heldAsk && heldAsk.text === t && (heldAsk.target || "") === target && OP_RE.test(heldAsk.key)) key = heldAsk.key;
    heldAsk = null;
    addMsg("you", (target ? label(target) + ": " : "") + t, "");
    el.aText.value = ""; autosize(); text(el.aNote, ""); countAsk();
    return send(addMsg("jarvis", "Working…", "busy"), t, key, target);
  }
  // The app was closed or reloaded while a request ran on the PC: pick it up again.
  async function resumeAsk() {
    if (asking || !me) return;
    let saved = null;
    try { saved = JSON.parse(store.get(ASK_KEY, "") || "null"); } catch (e) { saved = null; }
    if (saved && saved.held === true) {
      if (OP_RE.test(String(saved.key || "")) && now() - saved.at < ASK_STALE_MS) {
        heldAsk = { key: String(saved.key), text: str(saved.text, 2000), target: str(saved.target, 64) };
        if (el.aText && !el.aText.value) { el.aText.value = heldAsk.text; autosize(); countAsk(); }
        if (heldAsk.target) store.set("jarvis.phone.target", heldAsk.target);
        return;
      }
      saveAsk("");
      return;
    }
    if (!saved || typeof saved !== "object" || !/^[0-9a-f]{6,32}$/.test(String(saved.job)) || !(now() - saved.at < ASK_STALE_MS)) { if (saved) saveAsk(""); return; }
    asking = true; renderAskMeta();
    if (el.aThread.textContent.indexOf(str(saved.text, 2000)) < 0 || !el.aThread.children.length) addMsg("you", str(saved.text, 2000), "");
    const m = addMsg("jarvis", "Working…", "busy");
    m.target = str(saved.target, 64);
    try { await pumpAsk(m, { pending: true, job: saved.job, trace: { rev: 0 } }, saved.at, str(saved.text, 2000)); }
    finally { asking = false; renderAskMeta(); }
  }
  function autosize() { el.aText.style.height = "auto"; const sh = el.aText.scrollHeight || 0; if (sh) el.aText.style.height = Math.min(140, sh) + "px"; }
  function countAsk() { text(el.aCount, `${el.aText.value.length} / ${MAX_ASK}`); }
  el.aText.addEventListener("input", () => { autosize(); countAsk(); renderAskMeta(); });
  el.aText.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); } });
  el.aSend.addEventListener("click", ask);

  // ---------- this phone ----------
  let forgetArm = 0, logoutOp = "";
  function renderPhone() {
    const p = me && me.phone && typeof me.phone === "object" ? me.phone : {};
    text(el.pnName, str(p.name, 40) || "–");
    text(el.pnPaired, day(p.created));
    text(el.pnHost, (me && str(me.host, 120)) || "–");
    text(el.pnMain, (me && me.main && (str(me.main.label, 60) || str(me.main.id, 60))) || "–");
    renderTheme();
  }
  function themePref() { const T = window.JarvisTheme; return (T && T.pref) || store.get("jarvis.theme", "dark"); }
  function renderTheme() { const p = themePref(); for (const k of ["system", "light", "dark"]) el["th" + k[0].toUpperCase() + k.slice(1)].setAttribute("aria-pressed", String(p === k)); }
  function setTheme(p) {
    store.set("jarvis.theme", p);
    const T = window.JarvisTheme;
    if (T && T.apply) { T.pref = p; T.apply(); }
    else document.documentElement.setAttribute("data-theme", p === "system" ? (media("(prefers-color-scheme: dark)") ? "dark" : "light") : p);
    pal = null; bgKey = "";
    renderTheme(); renderLink(); renderDevices();
  }
  for (const k of ["system", "light", "dark"]) el["th" + k[0].toUpperCase() + k.slice(1)].addEventListener("click", () => setTheme(k));
  el.pnForget.addEventListener("click", async () => {
    // Two taps, not a dialog: the second within 4 s forgets.
    if (!forgetArm) {
      forgetArm = setTimeout(() => { forgetArm = 0; text(el.pnForgetT, "Forget this phone"); }, 4000);
      text(el.pnForgetT, "Tap again to forget");
      return;
    }
    clearTimeout(forgetArm); forgetArm = 0;
    text(el.pnForgetT, "Forget this phone");
    if (!OP_RE.test(logoutOp)) logoutOp = operationId();
    try { await api("/phone/api/logout", { operation_id: logoutOp }, { timeout: 15000 }); }
    catch (e) {
      if (e.kind !== "auth") { text(el.pnMsg, e.message); return; }
    }
    logoutOp = "";
    toPair("This phone is forgotten. Pair it again to use JARVIS here.");
  });

  // ---------- sessions: screen and camera ----------
  let sess = null, starting = false, startTok = 0, ended = null, sessDevice = "", sessKind = "screen";
  let gen = 0, frameCtl = null, hiddenAt = 0, hiddenTimer = 0, tickTimer = 0;
  let gotFrame = false, fw = 0, fh = 0, lastFrameAt = 0, aliveAt = 0, attachedAt = 0;
  let frameTimes = [], frameRtts = [], shownUrl = "", nextUrl = "";
  const view = () => VIEWS[sessKind] || VIEWS.screen;
  // Each start has a token; leaving the screen (or a newer start) retires it, and a late reply
  // for a retired start is stopped at once instead of shown.
  async function startSession(device, kind) {
    if (sess || (starting && sessDevice === device && sessKind === kind)) return;
    const tok = ++startTok;
    sessDevice = device; sessKind = kind; ended = null; starting = true; gotFrame = false; fw = 0; fh = 0;
    renderSess();
    let s = null;
    try { s = await api("/phone/api/session/start", { device, kind, quality: prefQuality() }, { timeout: 30000 }); }
    catch (e) {
      if (tok !== startTok) return;
      starting = false;
      if (e.kind === "auth") return;
      ended = { error: e.message, frame: false };
      renderSess(); announce("Couldn't start: " + e.message);
      return;
    }
    if (tok !== startTok) { if (s && typeof s.id === "string") quietStop(s.id, false); return; }
    starting = false;
    if (!s || typeof s.id !== "string") { ended = { error: "JARVIS didn't start a session.", frame: false }; renderSess(); return; }
    if (cur.id !== device || cur.name !== (kind === "camera" ? "camera" : "control")) { quietStop(s.id, false); return; }
    attach(s);
  }
  function retireStart() { if (starting) { starting = false; startTok++; } }
  function attachTo(s, kind) { sessDevice = s.device; sessKind = kind; attach(s); }
  function attach(s) {
    detachLoop();
    sessKind = (s.kind || sessKind) === "camera" ? "camera" : "screen";
    sess = Object.assign({}, s, { kind: sessKind, lastSeq: 0 });
    sessDevice = s.device || sessDevice;
    gotFrame = false; lastFrameAt = 0; aliveAt = 0; attachedAt = now(); frameTimes = []; frameRtts = []; ended = null;
    fw = s.width || 0; fh = s.height || 0;
    resetInput(); setZoom(1, 0, 0);
    if (document.hidden) { hiddenAt = hiddenAt || now(); armHiddenStop(); } else startFrames();
    clearInterval(tickTimer); tickTimer = setInterval(renderSess, 500);
    lockScreen();
    renderSess();
    announce((sessKind === "camera" ? "Opening the camera on " : "Connecting to ") + label(sessDevice) + ".");
  }
  // The session is over (by us, the computer or an error); reason shows when there's no error.
  function finish(error, reason) {
    detachLoop();
    clearInterval(tickTimer); tickTimer = 0; clearTimeout(hiddenTimer); hiddenTimer = 0;
    const was = sess;
    resetInput();
    sess = null; starting = false;
    ended = { error: error || null, reason: reason || "", frame: gotFrame };
    gotFrame = false;
    dropFrames();
    unlockScreen();
    renderSess();
    if (was) announce(error ? "Session ended: " + error : reason || "Session ended.");
  }
  // Ends it now on the phone, then tells JARVIS (release first for a screen), best effort.
  function disconnect(why) {
    const s = sess;
    if (!s) return;
    const release = s.kind === "screen" && gotFrame;
    finish(null, why || "Ended.");
    quietStop(s.id, release);
  }
  async function quietStop(id, release) {
    if (release) { try { await call("/phone/api/session/input", { session: id, events: [{ t: "release" }] }, { timeout: 8000 }); } catch (e) { /* stop below lets go too */ } }
    try { await call("/phone/api/session/stop", { session: id }, { timeout: 8000 }); } catch (e) { /* already gone */ }
    if (me && !document.hidden) refresh();
  }
  // pagehide and the background stop: requests that outlive the page.
  function beacon(path, body) {
    const payload = stampBody(path, body);
    try {
      fetch(path, { method: "POST", keepalive: true, credentials: "same-origin", headers: phoneHeaders(true), body: JSON.stringify(payload) }).catch(() => {});
    } catch (e) { /* the page is going away */ }
  }
  function stopBeacon() {
    if (!sess) return;
    const id = sess.id;
    if (sess.kind === "screen") beacon("/phone/api/session/input", { session: id, events: [{ t: "release" }] });
    beacon("/phone/api/session/stop", { session: id });
  }
  function backgroundStop() { if (!sess) return; stopBeacon(); finish(null, BG_REASON); }
  function armHiddenStop() {
    clearTimeout(hiddenTimer);
    const left = HIDDEN_STOP_MS - (hiddenAt ? now() - hiddenAt : 0);
    hiddenTimer = setTimeout(() => { if (document.hidden && sess) backgroundStop(); }, Math.max(0, left));
  }

  // ---------- frames ----------
  function detachLoop() { gen++; if (frameCtl) { try { frameCtl.abort(); } catch (e) { /* done */ } frameCtl = null; } }
  function startFrames() { if (!sess) return; detachLoop(); frameLoop(sess.id, gen); }
  // One long-poll at a time; the hub holds each until a newer frame (or ~2.5 s).
  async function frameLoop(id, g) {
    let fails = 0;
    while (g === gen && sess && sess.id === id) {
      const ctl = new AbortController(), t0 = now();
      frameCtl = ctl;
      const timer = setTimeout(() => ctl.abort(), 15000);
      try {
        const res = await fetch(frameUrl(sess.lastSeq || 0),
          { signal: ctl.signal, credentials: "same-origin", cache: "no-store",
            headers: Object.assign(phoneHeaders(false), { "X-JARVIS-Session": id }) });
        if (g !== gen) return;
        if (res.status === 204) {
          fails = 0; reachable(true);
          const nodeAgo = parseFloat(res.headers.get("X-Node-Ago"));
          if (isFinite(nodeAgo) && nodeAgo < STALE_MS / 1000) aliveAt = now() - nodeAgo * 1000;
          continue;
        }
        if (res.status === 401 || res.status === 404 || res.status === 410) {
          const d = await readJson(res, 8192);
          if (g !== gen) return;
          if (res.status === 401 && d && d.pair) { toPair(); return; }
          finish(res.status === 404 ? GONE : str(d && d.error) || "The computer ended the session.", "");
          return;
        }
        if (!res.ok) {
          const d = await readJson(res, 8192);
          if (g !== gen || repairing) return;
          // A csrf refusal is not a bad picture: stop the loop, release locally, and pair again.
          if (isCsrf(new Fail("", "http", res.status, d))) {
            finish(null, "Ended.");
            showCsrfRecovery();
            return;
          }
          throw Object.assign(new Error(`JARVIS couldn't send the picture (HTTP ${res.status}).`), { http: true });
        }
        const blob = await res.blob();
        if (g !== gen || !sess) return;
        const seq = parseInt(res.headers.get("X-Frame-Seq"), 10);
        const w = parseInt(res.headers.get("X-Frame-Width"), 10), hh = parseInt(res.headers.get("X-Frame-Height"), 10);
        sess.lastSeq = isFinite(seq) ? seq : (sess.lastSeq || 0) + 1;
        if (w > 0 && hh > 0) { fw = w; fh = hh; }
        paint(blob);
        fails = 0; reachable(true);
        frameRtts.push(now() - t0); if (frameRtts.length > 6) frameRtts.shift();
        frameTimes.push(now()); lastFrameAt = now();
        if (!gotFrame) { gotFrame = true; renderSess(); announce("Live."); }
      } catch (e) {
        if (g !== gen || repairing) return;
        if (!e || !e.http) reachable(false);
        if (++fails >= 6) { finish(e && e.http ? e.message : LOST, ""); return; }
        await sleep(Math.min(3000, 300 * fails));
      } finally { clearTimeout(timer); }
    }
  }
  // One <img> per screen; each frame's blob URL is revoked once the next one has loaded.
  function paint(blob) {
    const img = view().img, url = URL.createObjectURL(blob);
    if (nextUrl) URL.revokeObjectURL(nextUrl);  // arrived before the last one loaded: drop that one
    nextUrl = url;
    img.src = url;
  }
  function onFrameLoad() {
    if (!nextUrl) return;
    if (shownUrl && shownUrl !== nextUrl) URL.revokeObjectURL(shownUrl);
    shownUrl = nextUrl; nextUrl = "";
    placeCursor();
  }
  function onFrameError() { if (nextUrl) { URL.revokeObjectURL(nextUrl); nextUrl = ""; } }
  function dropFrames() {
    for (const v of Object.values(VIEWS)) { v.img.removeAttribute("src"); v.img.hidden = true; }
    if (shownUrl) URL.revokeObjectURL(shownUrl);
    if (nextUrl) URL.revokeObjectURL(nextUrl);
    shownUrl = ""; nextUrl = "";
  }
  for (const v of Object.values(VIEWS)) { v.img.addEventListener("load", onFrameLoad); v.img.addEventListener("error", onFrameError); }

  // ---------- session rendering ----------
  const STATE_WORD = { starting: "Starting…", live: "Live", stale: "No new frames", ended: "Ended", failed: "Couldn't connect" };
  function sessState() {
    if (starting) return "starting";
    if (!sess) return ended ? (ended.error ? "failed" : "ended") : "starting";
    if (!gotFrame) return now() - attachedAt > NO_FRAME_MS ? "stale" : "starting";
    return now() - Math.max(lastFrameAt, aliveAt) > STALE_MS ? "stale" : "live";
  }
  function renderSess() {
    const v = view(), st = sessState(), name = label(sessDevice), cam = sessKind === "camera";
    v.state.setAttribute("data-st", st);
    text(v.st, st === "failed" && ended && ended.frame ? "Ended" : STATE_WORD[st]);
    text(v.h, (cam ? "Camera · " : "") + name);
    const pic = !!sess && gotFrame;
    v.img.hidden = !pic;
    v.ph.hidden = pic;
    v.img.setAttribute("alt", (cam ? "Live camera of " : "Live screen of ") + name);
    v.back.setAttribute("href", "#/device/" + encodeURIComponent(sessDevice));
    if (!pic) {
      let hd = "Starting…", p = cam ? `Opening the camera on ${name}. It shows a notice there while it's on.` : `Waiting for the first frame from ${name}.`;
      if (st === "stale") { hd = "No picture yet"; p = `${name} hasn't sent a frame. It may be asleep or busy.`; }
      else if (st === "failed") { hd = ended.frame ? "The session ended" : cam ? "The camera didn't start" : "Couldn't connect"; p = ended.error; }
      else if (st === "ended") { hd = "Ended"; p = (ended && ended.reason) || "Start again, or go back."; }
      text(v.phH, hd); text(v.phP, p);
    }
    v.phActs.hidden = !(st === "ended" || st === "failed");
    renderReadouts(v, st);
    if (!cam) {
      renderSticky(); for (const b of keyBtns.values()) b.disabled = !canSend(); placeCursor();
      if (canSend() && el.cNote.getAttribute("data-st") === "wait") { text(el.cNote, ""); el.cNote.removeAttribute("data-st"); }
    } else {
      el.vChrome.hidden = !pic;  // REC, the countdown and the cross only over a live picture
      fitChrome();
    }
  }
  // The viewfinder's ticks and readouts hug the camera picture, not the whole black stage.
  function fitChrome() {
    const c = el.vChrome, b = el.vStage.getBoundingClientRect(), iw = fw, ih = fh;
    if (!iw || !ih || !b || b.width < 1 || !gotFrame) { c.style.left = c.style.top = "0px"; c.style.width = c.style.height = "100%"; return; }
    const k = Math.min(b.width / iw, b.height / ih), w = Math.round(iw * k), hh = Math.round(ih * k);
    c.style.left = Math.round((b.width - w) / 2) + "px"; c.style.top = Math.round((b.height - hh) / 2) + "px";
    c.style.width = w + "px"; c.style.height = hh + "px";
  }
  function renderReadouts(v, st) {
    const t = now();
    while (frameTimes.length && t - frameTimes[0] > 2000) frameTimes.shift();
    const fps = frameTimes.length > 1 ? (frameTimes.length - 1) / ((frameTimes[frameTimes.length - 1] - frameTimes[0]) / 1000) : 0;
    const live = !!sess && gotFrame && st !== "stale";
    const cam = sessKind === "camera";
    const fpsText = live ? fps.toFixed(fps < 10 ? 1 : 0) : "–", rtt = frameRtts.length ? Math.min(...frameRtts) : 0;
    text(v.fps, cam ? fpsText + " FPS" : fpsText + " fps");
    text(v.res, fw && sess ? `${fw} × ${fh}` : cam ? "– × –" : "–");
    text(v.rtt, rtt && sess ? `${Math.round(rtt)} ${cam ? "MS" : "ms"}` : cam ? "– MS" : "– ms");
    if (cam) {
      const run = sess ? t - attachedAt : 0;
      text(v.clock, mmss(run));
      text(v.left, sess ? mmss(CAMERA_MAX_MS - run) + " LEFT" : "15:00 LEFT");
      text(v.q, String((sess && sess.quality) || prefQuality()).toUpperCase());
    }
  }
  for (const k of ["screen", "camera"]) {
    const v = VIEWS[k];
    v.end.addEventListener("click", () => { disconnect("Ended."); go("#/device/" + encodeURIComponent(sessDevice)); });
    v.again.addEventListener("click", () => { ended = null; startSession(sessDevice, k); });
  }

  // ---------- wake lock ----------
  let wake = null, waking = false;
  async function lockScreen() {
    if (!sess || document.hidden || wake || waking || !navigator.wakeLock) return;
    waking = true;
    try {
      const w = await navigator.wakeLock.request("screen");
      if (!sess) { w.release().catch(() => {}); return; }
      wake = w;
      w.addEventListener("release", () => { if (wake === w) wake = null; });
    } catch (e) { wake = null; /* refused or unsupported: the screen may dim, that's all */ }
    finally { waking = false; }
  }
  function unlockScreen() { const w = wake; wake = null; if (w) { try { w.release().catch(() => {}); } catch (e) { /* gone */ } } }

  // ---------- input ----------
  // Batched like the console: moves merge, other events go at once, one request in flight, at
  // most 200 events each. A failed batch puts a release first, so nothing stays held.
  // HTTP 4xx is permanent: drop ordinary events, send one release, and do not reschedule.
  // A network error or HTTP 5xx backs off from 300 ms, doubling to 5 s. Success starts again at 300 ms.
  let q = [], inflight = false, flushTimer = 0, lastSend = 0, inputErr = "", inputFails = 0, releaseAfterStop = false, inputStopped = false;
  const held = new Set(), heldKeys = new Set(), sticky = new Set(), keyBtns = new Map();
  const canSend = () => !!sess && sess.kind === "screen" && gotFrame;
  function resetInput() {
    clearTimeout(flushTimer); flushTimer = 0; clearTimeout(longTimer);
    q = []; held.clear(); heldKeys.clear(); sticky.clear(); ptrs.clear(); gest = null; inputErr = "";
    inputFails = 0; releaseAfterStop = false; inputStopped = false; wheelAcc = 0;
    text(el.cNote, "");
  }
  // Same decision as the console pump. ok / err / fails / events (batch just sent) / queue (still waiting).
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
    if (!canSend()) return false;
    if (inputStopped && ev.t !== "release") return false;
    if (ev.t === "move" && q.length && q[q.length - 1].t === "move") q[q.length - 1] = ev; else q.push(ev);
    if (urgent) pump();
    else if (!flushTimer && !inflight) flushTimer = setTimeout(pump, Math.max(0, FLUSH_MS - (now() - lastSend)));
    return true;
  }
  async function pump() {
    clearTimeout(flushTimer); flushTimer = 0;
    if (!q.length || !sess || inflight) return;
    const id = sess.id, events = q.slice(0, MAX_EVENTS);
    q = q.slice(MAX_EVENTS);
    const preserveNote = releaseAfterStop && events.length === 1 && events[0] && events[0].t === "release";
    releaseAfterStop = false;
    inflight = true; lastSend = now();
    let plan = null;
    try {
      await api("/phone/api/session/input", { session: id, events }, { timeout: 10000 });
      plan = inputPumpPlan(true, null, inputFails, events, q);
      inputFails = plan.fails;
      q = plan.queue;
      if (!preserveNote) inputStopped = false;
      if (plan.clearNote && inputErr && !preserveNote) { inputErr = ""; text(el.cNote, ""); }
    } catch (e) {
      if (e.kind === "auth") return;
      if (!sess || sess.id !== id) return;
      if (e.status === 404 || e.status === 410) { finish(e.status === 404 ? GONE : e.message, ""); return; }
      plan = inputPumpPlan(false, e, inputFails, events, q);
      inputFails = plan.fails;
      q = plan.queue;
      inputStopped = !!plan.stop;
      releaseAfterStop = !!plan.releaseOnce;
      if (e.message !== inputErr) { inputErr = e.message; text(el.cNote, "Input didn't arrive: " + e.message); el.cNote.setAttribute("data-st", "bad"); }
      // The lost batch may have held a key-up or button-up: let go of everything first, then
      // count as held only what the events still queued after that release press again.
      heldFrom(q);
    } finally {
      inflight = false;
      const live = plan && sess && sess.id === id;
      if (live && plan.releaseOnce && q.length) pump();
      else if (live && !plan.stop && q.length) {
        if (plan.delay) flushTimer = setTimeout(pump, plan.delay);
        else if (q.some((ev) => ev.t !== "move")) pump();
        else flushTimer = setTimeout(pump, FLUSH_MS);
      }
    }
  }
  function heldFrom(events) {
    held.clear(); heldKeys.clear();
    for (const ev of events) {
      if (ev.t === "release") { held.clear(); heldKeys.clear(); }
      else if (ev.t === "down") held.add(ev.b);
      else if (ev.t === "up") held.delete(ev.b);
      else if (ev.t === "key") { if (ev.down) heldKeys.add(ev.code); else heldKeys.delete(ev.code); }
    }
    for (const c of [...sticky]) if (!heldKeys.has(c)) sticky.delete(c);
    renderSticky();
  }
  // Let go of every key and button on the remote. force: even if nothing seems held.
  function releaseAll(force) {
    if (!canSend()) return;
    clearTimeout(longTimer); ptrs.clear(); gest = null;
    if (!force && !held.size && !heldKeys.size) return;
    held.clear(); heldKeys.clear(); sticky.clear(); renderSticky();
    push({ t: "release" }, true);
  }
  const at = (p) => (p ? { x: p.x, y: p.y } : {});
  function press(b, down, p) {
    if (down === held.has(b)) return;
    if (down) held.add(b); else held.delete(b);
    push(Object.assign({ t: down ? "down" : "up", b }, at(p)), true);
  }
  function click(b, p) { if (!p) return; press(b, true, p); press(b, false, p); }
  function key(code, down) { if (down) heldKeys.add(code); else heldKeys.delete(code); push({ t: "key", code, down }, true); }

  // ---------- picture geometry and zoom ----------
  const zoom = { s: 1, tx: 0, ty: 0 };
  let mode = store.get("jarvis.phone.mode", "direct") === "track" ? "track" : "direct";
  const cursor = { x: 0.5, y: 0.5 };
  const stageBox = () => el.cStage.getBoundingClientRect();
  // The picture's box inside the stage before zoom (object-fit: contain).
  function content(b) {
    const iw = fw || VIEWS.screen.img.naturalWidth, ih = fh || VIEWS.screen.img.naturalHeight;
    if (!iw || !ih || !b || b.width < 1 || b.height < 1) return null;
    const k = Math.min(b.width / iw, b.height / ih), w = iw * k, hh = ih * k;
    return { l: (b.width - w) / 2, t: (b.height - hh) / 2, w, h: hh };
  }
  // A point on the phone's screen -> 0..1 on the remote screen, undoing the pinch zoom.
  // null outside the picture, unless clamp (a drag that runs past the edge holds there).
  function norm(cx, cy, clampIt) {
    const b = stageBox(), c = content(b);
    if (!c) return null;
    const lx = (cx - b.left - zoom.tx) / zoom.s, ly = (cy - b.top - zoom.ty) / zoom.s;
    let x = (lx - c.l) / c.w, y = (ly - c.t) / c.h;
    if (x < 0 || x > 1 || y < 0 || y > 1) {
      if (!clampIt) return null;
      x = clamp(x, 0, 1); y = clamp(y, 0, 1);
    }
    return { x: r5(x), y: r5(y) };
  }
  function setZoom(s, tx, ty) {
    const b = stageBox(), w = (b && b.width) || 0, hh = (b && b.height) || 0;
    s = clamp(s, 1, MAX_ZOOM);
    zoom.s = s; zoom.tx = clamp(tx, w * (1 - s), 0); zoom.ty = clamp(ty, hh * (1 - s), 0);
    if (s === 1) { zoom.tx = 0; zoom.ty = 0; }
    el.cLayer.style.transform = s === 1 ? "" : `translate(${zoom.tx.toFixed(1)}px, ${zoom.ty.toFixed(1)}px) scale(${s.toFixed(3)})`;
    el.cFit.hidden = s === 1;
    placeCursor();
  }
  // Where a 0..1 point of the remote screen sits on the stage right now.
  function onStage(p) {
    const c = content(stageBox());
    if (!c) return null;
    return { x: zoom.tx + zoom.s * (c.l + p.x * c.w), y: zoom.ty + zoom.s * (c.t + p.y * c.h) };
  }
  function placeCursor() {
    const on = mode === "track" && canSend();
    el.cCursor.hidden = !on;
    if (!on) return;
    const s = onStage(cursor);
    if (s) el.cCursor.style.transform = `translate(${s.x.toFixed(1)}px, ${s.y.toFixed(1)}px)`;
  }
  let ringTimer = 0;
  function ring(p) {
    const s = p && onStage(p);
    if (!s) return;
    el.cRing.style.transform = `translate(${s.x.toFixed(1)}px, ${s.y.toFixed(1)}px)`;
    el.cRing.hidden = false;
    clearTimeout(ringTimer); ringTimer = setTimeout(() => { el.cRing.hidden = true; }, 450);
  }
  function setMode(m) {
    mode = m; store.set("jarvis.phone.mode", m);
    el.cDirect.setAttribute("aria-pressed", String(m === "direct"));
    el.cTrack.setAttribute("aria-pressed", String(m === "track"));
    placeCursor();
  }
  el.cDirect.addEventListener("click", () => setMode("direct"));
  el.cTrack.addEventListener("click", () => setMode("track"));
  el.cFit.addEventListener("click", () => setZoom(1, 0, 0));

  // ---------- touch gestures on the picture ----------
  // One finger: tap = left click (direct: at the finger; trackpad: at the cursor), hold 500 ms
  // without moving 8 px = right click, drag = left drag (direct) or cursor move (trackpad).
  // Two fingers: a vertical drag scrolls (wheel), a pinch zooms the picture on the phone only.
  const ptrs = new Map();
  let gest = null, longTimer = 0, wheelAcc = 0;
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  // A button laid over the picture (Fit, Start again) takes its own taps: none of them may
  // also click whatever on the remote screen sits under it.
  function onControl(e) {
    const t = e.target;
    return !!t && t !== el.cStage && (t === el.cFit || (typeof t.closest === "function" && !!t.closest("button, a")));
  }
  function pdown(e) {
    if (onControl(e)) return;
    e.preventDefault();
    if (!sess || sess.kind !== "screen" || !gotFrame || e.button > 0) return;
    try { el.cStage.setPointerCapture(e.pointerId); } catch (_) { /* fine */ }
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (ptrs.size === 1) {
      gest = { kind: "tap", id: e.pointerId, x0: e.clientX, y0: e.clientY, lx: e.clientX, ly: e.clientY };
      clearTimeout(longTimer); longTimer = setTimeout(longPress, LONG_PRESS_MS);
    } else if (ptrs.size === 2) {
      clearTimeout(longTimer);
      if (gest && gest.kind === "drag") press("left", false, norm(gest.lx, gest.ly, true));
      const [a, b] = [...ptrs.values()], c = mid(a, b);
      gest = { kind: "two", d0: dist(a, b), c0: c, last: c, s0: zoom.s, tx0: zoom.tx, ty0: zoom.ty, acc: 0 };
    }
  }
  function pmove(e) {
    const p = ptrs.get(e.pointerId);
    if (!p || !gest) return;
    e.preventDefault();
    p.x = e.clientX; p.y = e.clientY;
    if (gest.kind === "two" || gest.kind === "pinch" || gest.kind === "scroll") { twoMove(); return; }
    if (e.pointerId !== gest.id) return;
    if (gest.kind === "tap") {
      if (Math.hypot(e.clientX - gest.x0, e.clientY - gest.y0) <= SLOP_PX) return;
      clearTimeout(longTimer);
      if (mode === "direct") {
        const start = norm(gest.x0, gest.y0, false);
        if (!start) { gest.kind = "done"; return; }
        gest.kind = "drag";
        press("left", true, start);
      } else gest.kind = "track";
    }
    if (gest.kind === "drag") {
      const n = norm(e.clientX, e.clientY, true);
      if (n) push({ t: "move", x: n.x, y: n.y }, false);
    } else if (gest.kind === "track") {
      const c = content(stageBox());
      if (c) {
        cursor.x = clamp(cursor.x + ((e.clientX - gest.lx) / (c.w * zoom.s)) * TRACK_GAIN, 0, 1);
        cursor.y = clamp(cursor.y + ((e.clientY - gest.ly) / (c.h * zoom.s)) * TRACK_GAIN, 0, 1);
        push({ t: "move", x: r5(cursor.x), y: r5(cursor.y) }, false);
        placeCursor();
      }
    }
    gest.lx = e.clientX; gest.ly = e.clientY;
  }
  function twoMove() {
    if (ptrs.size < 2) return;
    const [a, b] = [...ptrs.values()], d = dist(a, b), c = mid(a, b);
    if (gest.kind === "two") {
      if (Math.abs(d - gest.d0) > PINCH_PX) gest.kind = "pinch";
      else if (Math.abs(c.y - gest.c0.y) > SLOP_PX) gest.kind = "scroll";
      else return;
    }
    if (gest.kind === "pinch") {
      // Keep the picture point first under the fingers' centre under it as they move and spread.
      const bx = stageBox(), s = clamp(gest.s0 * (d / Math.max(1, gest.d0)), 1, MAX_ZOOM);
      const lx = (gest.c0.x - bx.left - gest.tx0) / gest.s0, ly = (gest.c0.y - bx.top - gest.ty0) / gest.s0;
      setZoom(s, c.x - bx.left - lx * s, c.y - bx.top - ly * s);
      return;
    }
    // Scroll: fingers up = content up = wheel down (positive), one notch per 24 px.
    gest.acc += c.y - gest.last.y; gest.last = c;
    const n = Math.trunc(gest.acc / WHEEL_STEP_PX);
    if (n) { gest.acc -= n * WHEEL_STEP_PX; push({ t: "wheel", dy: clamp(-n, -20, 20) }, true); }
  }
  function pup(e) {
    if (!ptrs.has(e.pointerId)) return;
    e.preventDefault();
    ptrs.delete(e.pointerId);
    try { el.cStage.releasePointerCapture(e.pointerId); } catch (_) { /* fine */ }
    if (!gest) return;
    if (e.pointerId === gest.id && gest.kind === "tap") {
      clearTimeout(longTimer);
      click("left", mode === "direct" ? norm(e.clientX, e.clientY, false) : { x: r5(cursor.x), y: r5(cursor.y) });
    } else if (e.pointerId === gest.id && gest.kind === "drag") {
      press("left", false, norm(e.clientX, e.clientY, true));
    }
    gest = ptrs.size ? { kind: "done" } : null;  // a finger left over from two does nothing
  }
  function longPress() {
    if (!gest || gest.kind !== "tap") return;
    const p = mode === "direct" ? norm(gest.x0, gest.y0, false) : { x: r5(cursor.x), y: r5(cursor.y) };
    gest.kind = "done";
    if (!p) return;
    click("right", p);
    ring(p);
  }
  function pcancel() { releaseAll(true); }
  el.cStage.addEventListener("pointerdown", pdown);
  el.cStage.addEventListener("pointermove", pmove);
  el.cStage.addEventListener("pointerup", pup);
  el.cStage.addEventListener("pointercancel", pcancel);
  el.cStage.addEventListener("lostpointercapture", (e) => { if (ptrs.has(e.pointerId)) pcancel(); });
  el.cStage.addEventListener("contextmenu", (e) => e.preventDefault());
  el.cStage.addEventListener("dragstart", (e) => e.preventDefault());
  // iOS: no page zoom, callout or scroll from gestures on the picture.
  for (const k of ["gesturestart", "gesturechange", "gestureend"]) el.cStage.addEventListener(k, (e) => e.preventDefault());
  el.cStage.addEventListener("touchmove", (e) => e.preventDefault(), { passive: false });
  el.cStage.addEventListener("wheel", (e) => {
    if (!canSend()) return;
    e.preventDefault();
    wheelAcc += e.deltaMode === 1 ? e.deltaY / 3 : e.deltaMode === 2 ? Math.sign(e.deltaY) * 10 : e.deltaY / 100;
    wheelAcc = clamp(wheelAcc, -40, 40);
    const n = clamp(Math.trunc(wheelAcc), -20, 20);
    if (n) { wheelAcc -= n; push({ t: "wheel", dy: n }, true); }
  }, { passive: false });

  // ---------- keys and text ----------
  function buildKeys() {
    for (const [code, name, mod, spoken, ic] of KEYS) {
      const b = h("button", { type: "button", class: "key mono" + (mod ? " mod" : ""), "data-code": code, "aria-label": spoken || null }, ic ? icon(ic) : name);
      if (mod) b.setAttribute("aria-pressed", "false");
      // Keep the text field focused (and the soft keyboard up) while tapping keys.
      b.addEventListener("pointerdown", (e) => e.preventDefault());
      b.addEventListener("mousedown", (e) => e.preventDefault());
      b.addEventListener("click", () => keyTap(code));
      keyBtns.set(code, b);
      el.cKeys.append(b);
    }
  }
  // Modifiers are sticky: the first tap holds one down, the next key goes with it, then it's let go.
  function keyTap(code) {
    if (!canSend()) return;
    flushText();
    if (MODS.has(code)) {
      if (sticky.has(code)) { sticky.delete(code); key(code, false); } else { sticky.add(code); key(code, true); }
      renderSticky();
      return;
    }
    key(code, true); key(code, false);
    dropSticky();
  }
  function dropSticky() { for (const c of [...sticky]) key(c, false); sticky.clear(); renderSticky(); }
  function renderSticky() {
    for (const c of MODS) { const b = keyBtns.get(c); if (b) b.setAttribute("aria-pressed", String(sticky.has(c))); }
    // platform.system() names: "Windows", "Linux", "Darwin" (which also contains "win").
    const meta = keyBtns.get("MetaLeft"), os = String((dev(sessDevice) || {}).platform || "");
    const [face, spoken] = /^win/i.test(os) ? ["Win", "Windows key"] : /^(darwin|mac)/i.test(os) ? ["⌘", "Command key"] : ["Super", "Super key"];
    if (meta) { text(meta, face); meta.setAttribute("aria-label", spoken); }
  }
  // Text goes as whole words: on compositionend, Enter, Send and blur. A single letter or digit
  // with a sticky modifier goes as that key instead, so Ctrl then c is Ctrl+C.
  let composing = false;
  function flushText() {
    const v = el.cText.value;
    if (!v) return;
    if (!canSend()) {  // keep what was typed until it can go
      if (sess) { text(el.cNote, "Not sent yet: send again once the picture is live."); el.cNote.setAttribute("data-st", "wait"); }
      else { text(el.cNote, "Not sent: the session has ended."); el.cNote.setAttribute("data-st", "bad"); }
      return;
    }
    el.cText.value = "";
    if (sticky.size && /^[A-Za-z0-9 ]$/.test(v)) {
      const code = v === " " ? "Space" : /\d/.test(v) ? "Digit" + v : "Key" + v.toUpperCase();
      key(code, true); key(code, false); dropSticky();
      return;
    }
    const chars = Array.from(clean(v));
    for (let i = 0; i < chars.length; i += MAX_TEXT) push({ t: "text", text: chars.slice(i, i + MAX_TEXT).join("") }, true);
    if (sticky.size) dropSticky();
  }
  el.cText.addEventListener("compositionstart", () => { composing = true; });
  el.cText.addEventListener("compositionend", () => { composing = false; setTimeout(() => { if (!composing) flushText(); }, 0); });
  el.cText.addEventListener("keydown", (e) => {
    if (e.isComposing || composing || e.keyCode === 229) return;
    if (e.key === "Enter") { e.preventDefault(); if (el.cText.value) flushText(); else keyTap("Enter"); }
    else if (e.key === "Backspace" && !el.cText.value) { e.preventDefault(); keyTap("Backspace"); }
  });
  // Soft keyboards often send no usable keydown for Backspace; an empty field still gets this.
  el.cText.addEventListener("beforeinput", (e) => {
    if (e.inputType === "deleteContentBackward" && !composing && !el.cText.value) { e.preventDefault(); keyTap("Backspace"); }
  });
  el.cText.addEventListener("blur", () => { composing = false; flushText(); });
  el.cSend.addEventListener("pointerdown", (e) => e.preventDefault());
  el.cSend.addEventListener("click", () => { if (el.cText.value) flushText(); else keyTap("Enter"); });
  function setKeys(open) {
    el.cPanel.hidden = !open;
    el.cKeysBtn.setAttribute("aria-expanded", String(open));
    el.cKeysBtn.setAttribute("aria-label", open ? "Hide keys" : "Show keys");
    store.set("jarvis.phone.keys", open ? "1" : "0");
  }
  el.cKeysBtn.addEventListener("click", () => setKeys(el.cPanel.hidden));
  // Focus in the tap itself: iOS only opens the soft keyboard from a user gesture.
  el.cType.addEventListener("click", () => { if (el.cPanel.hidden) setKeys(true); el.cText.focus(); });

  // ---------- lifecycle ----------
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      hiddenAt = now();
      if (sess) {
        releaseAll(true);  // let go at once; frames pause; the session ends if nobody comes back
        detachLoop();
        armHiddenStop();
      }
      return;
    }
    clearTimeout(hiddenTimer); hiddenTimer = 0;
    // A throttled background timer may not have fired: past the limit, end it now.
    if (sess && hiddenAt && now() - hiddenAt >= HIDDEN_STOP_MS) backgroundStop();
    else if (sess) { startFrames(); lockScreen(); }
    hiddenAt = 0;
    if (!el.down.hidden) boot();
    else if (me) { if (!sess) refresh(); resumeAsk(); }
  });
  addEventListener("blur", () => releaseAll(true));
  addEventListener("pagehide", () => stopBeacon());
  // Back from the page cache with a session the server has long ended.
  addEventListener("pageshow", (e) => { if (e && e.persisted && sess) finish(null, "Ended when the app closed."); });
  addEventListener("online", () => { if (!el.down.hidden) boot(); else if (me) refresh(); });
  addEventListener("offline", () => reachable(false));

  // The soft keyboard: keep the app inside the visible part of the screen, so the text field,
  // the key bar and Ask's box sit above it.
  function viewport() {
    const vv = window.visualViewport, rs = document.documentElement.style;
    let kb = 0, top = 0, visibleHeight = "var(--full-viewport)";
    document.documentElement.setAttribute("data-ios-app", isIOS() && standalone() ? "1" : "0");
    const active = document.activeElement;
    const typing = active && (/^(INPUT|TEXTAREA)$/.test(active.tagName) || active.isContentEditable);
    // Safari can retain a small viewport difference after its bars or keyboard move.
    // Only reserve keyboard space during text entry and for a keyboard-sized change.
    if (typing && vv && Math.abs((vv.scale || 1) - 1) < 0.01) {
      const inset = Math.max(0, Math.round((window.innerHeight || 0) - vv.height - vv.offsetTop));
      if (inset > 100) {
        kb = inset;
        visibleHeight = Math.round(vv.height) + "px";
        top = Math.max(0, Math.round(vv.offsetTop || 0));
      }
    }
    rs.setProperty("--phone-visible-height", visibleHeight);
    rs.setProperty("--kb", kb + "px");
    rs.setProperty("--vv-top", top + "px");
    document.documentElement.setAttribute("data-kb", kb > 60 ? "1" : "0");
    placeCursor();
    if (sess && sessKind === "camera") fitChrome();
  }
  if (window.visualViewport) { window.visualViewport.addEventListener("resize", viewport); window.visualViewport.addEventListener("scroll", viewport); }
  addEventListener("resize", viewport);
  addEventListener("orientationchange", viewport);
  document.addEventListener("focusin", viewport);
  document.addEventListener("focusout", () => setTimeout(viewport, 0));

  let liveTimer = 0;
  function announce(msg) { text(el.live, ""); clearTimeout(liveTimer); liveTimer = setTimeout(() => text(el.live, msg), 60); }

  function registerWorker() {
    try {
      if (navigator.serviceWorker && window.isSecureContext !== false) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
    } catch (e) { /* no offline shell, the app still works */ }
  }

  // ---------- start ----------
  buildKeys();
  setMode(mode);
  setKeys(store.get("jarvis.phone.keys", "1") !== "0");
  renderQuality(); renderTheme(); countAsk(); viewport();
  registerWorker();
  boot();
})();
