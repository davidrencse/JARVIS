// Behavioural tests for the phone app (jarvis/web/phone/phone.js). Every browser API is a fake: no
// real pointer, keyboard, network, camera, location or timer is touched.
// Run one scenario: node tests/mobile/web_phone_test.cjs <name>; all: node tests/mobile/web_phone_test.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "../../jarvis/web/phone/phone.js"), "utf8");
const SID = "s".repeat(24);
const HOSTILE = '<img src=x onerror="alert(1)">';
const flush = async () => { for (let i = 0; i < 40; i++) await new Promise((r) => setImmediate(r)); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function fakeClock() {
  let now = 1_800_000_000_000, id = 0;
  const timers = new Map();
  const add = (fn, ms, every) => { const t = ++id; timers.set(t, { at: now + Math.max(0, +ms || 0), fn, every }); return t; };
  return {
    now: () => now,
    setTimeout: (fn, ms) => add(fn, ms, 0),
    setInterval: (fn, ms) => add(fn, ms, Math.max(1, +ms || 1)),
    clear: (t) => { timers.delete(t); },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        await flush();
        let next = null;
        for (const [t, v] of timers) if (v.at <= end && (!next || v.at < next[1].at)) next = [t, v];
        if (!next) break;
        now = Math.max(now, next[1].at);
        if (next[1].every) next[1].at += next[1].every; else timers.delete(next[0]);
        next[1].fn();
      }
      now = end;
      await flush();
    },
    jump(ms) { now += ms; },  // time passes but no timer fires (a suspended app)
  };
}

const DEVICES = () => [
  { id: "zen", label: "Zenbook", platform: "linux", role: "main", online: true, seen: 1_800_000_000 - 2,
    caps: { remote: true, camera: true, locate: true }, why: {}, location: null, network: {} },
  { id: "dell", label: "Dell", platform: "windows", role: "node", online: true, seen: 1_800_000_000 - 5,
    caps: { remote: true, camera: false, locate: true }, why: { camera: "Camera is off on this PC." },
    location: { lat: 51.5, lon: -0.12, accuracy_m: 40, source: "windows", at: 1_800_000_000 - 60 }, network: { ssid: "Home" } },
];

const FLEET = () => ({
  audience: "phone", can_write: false, cluster: { enabled: true, local: null, main: "main", local_is_main: true },
  capabilities: [{ id: "inventory", title: "Hardware and status report", risk: "low", explain: "x" }, { id: "files", title: "Files and clipboard", risk: "medium", explain: "x" }],
  devices: [
    { id: "main", label: "Main PC", role: "main", profile: "pc", profile_label: "PC", status: "main", online: true, capabilities: ["inventory", "files"] },
    { id: "oracle1", label: "Oracle ARM", role: "node", profile: "cloud", profile_label: "Cloud server", status: "online", online: true, capabilities: ["inventory", "locate"] },
    { id: "spare", label: "Spare", role: "node", profile: "pc", profile_label: "PC", status: "invite_pending", online: false, capabilities: [] },
  ],
  actions: [{ id: "setup_guide", support: "guide", why: "x" }],
  guide: [{ id: "choose", title: "Pick what to connect", who: "main", body: "On the main PC open Connections." }, { id: "join", title: "Run the join command", who: "node", body: "On the new computer." }],
});

function page(opt = {}) {
  const clock = fakeClock();
  const state = {
    clock, calls: [], inputs: [], inputCalls: [], frames: [], revoked: [], blobs: 0, frameInFlight: 0, frameMaxInFlight: 0,
    offline: false, innerHTML: [], winHandlers: {}, inputGate: null, inputFail: null, locks: 0, nodeAgo: 0.5,
    me: { phone: { id: "p1", name: "Pocket", created: 1_790_000_000 }, host: "zen.tail.ts.net", main: { id: "zen", label: "Zenbook" } },
    overview: { devices: DEVICES(), sessions: [], ask: { enabled: true } },
    routes: {},
  };
  const created = [];
  const doc = {
    hidden: !!opt.hidden, activeElement: null, handlers: {}, els: {},
    getElementById(id) { return this.els[id] || (this.els[id] = element("div", id)); },
    createElement: (tag) => element(tag),
    createElementNS: (_ns, tag) => element(tag),
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t) }),
    addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
  };
  doc.documentElement = element("html");
  function element(tag, id) {
    const e = {
      tagName: String(tag).toUpperCase(), id: id || "", hidden: false, attrs: {}, handlers: {}, children: [], parent: null, _text: "",
      value: "", disabled: false, src: "", naturalWidth: 0, naturalHeight: 0, scrollTop: 0, scrollHeight: 0, clientWidth: 0, clientHeight: 0,
      rect: { left: 0, top: 0, width: 400, height: 300 },
      style: { setProperty(k, v) { this[k] = v; } },
      get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
      set textContent(v) { this._text = String(v); this.children = []; },
      get innerHTML() { return ""; },
      set innerHTML(v) { state.innerHTML.push(String(v)); },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      removeAttribute(k) { delete this.attrs[k]; if (k === "src") this.src = ""; },
      addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
      append(...kids) { for (const k of kids) { const n = typeof k === "string" ? doc.createTextNode(k) : k; n.parent = this; this.children.push(n); } },
      replaceChildren(...kids) { for (const c of this.children) c.parent = null; this.children = []; this.append(...kids); },
      remove() { if (this.parent) { this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; } },
      focus() { doc.activeElement = this; fire(this, "focus", {}); },
      blur() { if (doc.activeElement === this) { doc.activeElement = null; fire(this, "blur", {}); } },
      setPointerCapture() {}, releasePointerCapture() {},
      getBoundingClientRect() { return this.rect; },
    };
    created.push(e);
    return e;
  }
  function fire(target, kind, init) {
    const e = Object.assign({ prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {} }, init);
    for (const f of (target.handlers || {})[kind] || []) f(e);
    return e;
  }
  const win = (kind, init) => fire({ handlers: state.winHandlers }, kind, init);

  let hash = opt.hash || "";
  const navigation = [{ hash, state: null }];
  let navigationIndex = 0;
  const history = {
    get state() { return navigation[navigationIndex].state; },
    replaceState(value, _title, url) { hash = url; navigation[navigationIndex] = { hash, state: value }; },
    pushState(value, _title, url) { hash = url; navigation.splice(++navigationIndex); navigation.push({ hash, state: value }); },
    back() { this.move(-1); },
    forward() { this.move(1); },
    move(delta) {
      const next = navigationIndex + delta;
      if (next < 0 || next >= navigation.length) return;
      navigationIndex = next; hash = navigation[next].hash;
      clock.setTimeout(() => { win("popstate", { state: navigation[next].state }); win("hashchange", {}); }, 0);
    },
  };
  const location = {
    reload() { state.reloads = (state.reloads || 0) + 1; },
    get hash() { return hash; },
    set hash(v) {
      v = String(v); if (!v.startsWith("#")) v = "#" + v; if (v === hash) return;
      history.pushState(null, "", v); clock.setTimeout(() => win("hashchange", {}), 0);
    },
  };

  // ---------- network ----------
  const reply = (status, body, headers = {}) => ({
    ok: status >= 200 && status < 300, status,
    headers: { get: (k) => (k in headers ? String(headers[k]) : null) },
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
    json: async () => (body === undefined ? {} : JSON.parse(JSON.stringify(body))),
    blob: async () => ({ size: 3 }),
  });
  const defaults = {
    "GET /phone/api/me": () => [200, state.me],
    "GET /phone/api/overview": () => [200, state.overview],
    "POST /phone/api/session/start": (b) => [200, { id: SID, device: b.device, kind: b.kind, opener: "phone:p1", state: "starting", error: null,
      width: 0, height: 0, frames: 0, quality: b.quality, started: 1_800_000_000 }],
    "POST /phone/api/session/stop": () => [200, { ok: true }],
    "POST /phone/api/locate": (b) => [200, { device: b.device, fix: { lat: 51.51, lon: -0.1, accuracy_m: 25, source: "geoclue", at: 1_800_000_000 }, network: {} }],
    "POST /phone/api/ask": () => [200, { reply: "Done." }],
    "POST /phone/api/logout": () => [200, { ok: true }],
    "POST /phone/pair": () => [202, { pending: true }],
    "POST /phone/pair/status": () => [202, { pending: true }],
  };
  async function fetch(url, opts = {}) {
    const method = opts.method || "GET", u = String(url), p = u.split("?")[0];
    const body = opts.body ? JSON.parse(opts.body) : undefined;  // parsed here: plain objects of this realm
    state.calls.push({ method, path: p, url: u, body, headers: Object.assign({}, opts.headers), keepalive: !!opts.keepalive });
    await null;  // like a real network: the answer comes later, after the test has set its routes
    if (state.offline) throw new TypeError("Failed to fetch");
    if (p === "/phone/api/session/frame") return frameFetch(opts);
    if (p === "/phone/api/session/input" && method === "POST") {
      state.inputCalls.push(body.events);
      const gate = state.inputGate; state.inputGate = null;
      if (gate) await gate.promise;
      if (state.inputFail) { const m = state.inputFail; state.inputFail = null; return reply(502, { error: m }); }
      state.inputs.push(...body.events);
      return reply(200, { ok: true, queued: body.events.length });
    }
    const key = method + " " + p;
    const route = state.routes[key] || defaults[key] || (p.startsWith("/phone/api/ask/") && state.routes["GET /phone/api/ask/*"]);
    if (!route) return reply(404, { error: "no route " + key });
    const [status, data, headers] = await route(body, u);
    return reply(status, data, Object.assign({}, state.replyHeaders || {}, headers || {}));
  }
  // The hub holds a frame request until a frame comes or 2.5 s pass (then 204).
  let waiting = null;
  function frameFetch(opts) {
    state.frameInFlight++;
    state.frameMaxInFlight = Math.max(state.frameMaxInFlight, state.frameInFlight);
    const next = state.frames.shift();
    if (next) { state.frameInFlight--; return Promise.resolve(next()); }
    return new Promise((ok, fail) => {
      const w = { t: 0, give(v) { if (waiting !== w) return; waiting = null; clock.clear(w.t); state.frameInFlight--; ok(v); } };
      waiting = w;
      w.t = clock.setTimeout(() => w.give(reply(204, undefined, { "X-Node-Ago": String(state.nodeAgo) })), 2500);
      if (opts.signal) opts.signal.addEventListener("abort", () => {
        if (waiting !== w) return;
        waiting = null; clock.clear(w.t); state.frameInFlight--;
        const e = new Error("aborted"); e.name = "AbortError"; fail(e);
      });
    });
  }
  function pushFrame(make) { if (waiting) waiting.give(make()); else state.frames.push(make); }

  const ua = opt.ua || "Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile";
  class FakeDate extends Date {
    constructor(...a) { if (a.length) super(...a); else super(clock.now()); }
    static now() { return clock.now(); }
  }
  const store = new Map(Object.entries(opt.storage || {}));
  const context = {
    document: doc, location, history, fetch, Date: FakeDate, AbortController, Promise, console,
    navigator: { userAgent: ua, platform: opt.platform || "Linux armv8l", maxTouchPoints: 5, standalone: opt.standalone === true, serviceWorker: opt.serviceWorker, onLine: opt.onLine,
      wakeLock: { request: async () => { state.locks++; return { release: async () => {}, addEventListener() {} }; } } },
    matchMedia: (q) => ({ matches: q === "(display-mode: standalone)" ? opt.standalone === true : false, addEventListener() {} }),
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { if (opt.storageSetThrows) throw new Error("denied"); store.set(k, String(v)); },
      removeItem: (k) => { store.delete(k); },
    },
    btoa: (s) => Buffer.from(String(s), "latin1").toString("base64"),
    atob: (s) => Buffer.from(String(s), "base64").toString("latin1"),
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    setTimeout: clock.setTimeout, clearTimeout: clock.clear, setInterval: clock.setInterval, clearInterval: clock.clear,
    URL: { createObjectURL: () => "blob:" + ++state.blobs, revokeObjectURL: (u) => state.revoked.push(u) },
    addEventListener: (k, f) => { (state.winHandlers[k] ||= []).push(f); },
  };
  context.innerHeight = opt.innerHeight || 844;
  context.visualViewport = opt.visualViewport;
  context.window = context;
  vm.runInNewContext(SOURCE, context, { filename: "phone.js" });

  const els = doc.els, stage = () => els["c-stage"];
  const p = {
    state, els, doc, clock, fire, win, created, helpers: context.JarvisPhone,
    async settle(ms = 0) { await clock.advance(ms); },
    async go(h) { location.hash = h; await clock.advance(5); },
    async back() { history.back(); await clock.advance(5); },
    async forward() { history.forward(); await clock.advance(5); },
    click: (id) => fire(els[id], "click", {}),
    nav: context.navigator,
    storage: (k) => store.get(k),
    hash: () => location.hash,
    sent: () => state.inputs.filter((e) => e.t !== "move"),
    callsTo: (path, method = "POST") => state.calls.filter((c) => c.path === path && c.method === method),
    frame(seq) {
      pushFrame(() => reply(200, undefined, { "X-Frame-Seq": String(seq), "X-Frame-Width": "1600", "X-Frame-Height": "1000" }));
    },
    frameReply(status, body, headers) { pushFrame(() => reply(status, body, headers)); },
    ptr(kind, o) { return fire(stage(), kind, Object.assign({ pointerId: 1, pointerType: "touch", button: 0, buttons: 1, clientX: 200, clientY: 150 }, o)); },
    // Device detail, then the Control (or Camera) button, then the first frame.
    async open(kind = "control", device = "dell") {
      await this.go("#/device/" + device);
      this.frame(1);
      this.click(kind === "camera" ? "d-camera" : "d-control");
      await clock.advance(20);
      const img = kind === "camera" ? els["v-img"] : els["c-img"];
      fire(img, "load", {});
      await clock.advance(5);
    },
    hide(on) { doc.hidden = on; fire(doc, "visibilitychange", {}); },
    key: (code) => fire(p.findKey(code), "click", {}),
    findKey: (code) => created.find((e) => e.attrs["data-code"] === code),
  };
  return p;
}
function findAll(e, pred, out = []) { if (e && e.attrs && pred(e)) out.push(e); for (const c of (e && e.children) || []) findAll(c, pred, out); return out; }
function find(e, pred) { return findAll(e, pred)[0] || null; }
async function booted(opt) { const p = page(opt); await p.settle(10); return p; }

const scenarios = {
  async core_hidden_launch_and_offline_launch_recover() {
    const hidden = await booted({ hidden: true });
    assert.equal(hidden.state.calls.length, 0);
    assert.equal(hidden.els.down.hidden, true);
    assert.equal(hidden.helpers.store.state("stage"), "boot");
    hidden.hide(false); await hidden.settle(20);
    assert.equal(hidden.els.app.hidden, false);
    const offline = await booted({ onLine: false });
    assert.equal(offline.els.down.hidden, false);
    assert.equal(offline.state.calls.length, 0);
    offline.win("online"); await offline.settle(20);
    assert.equal(offline.els.app.hidden, false);
  },
  async core_update_prompt_waits_for_the_current_request() {
    const events = {};
    const p = await booted({ serviceWorker: { controller: {},
      register: async () => ({ addEventListener() {} }),
      addEventListener: (key, fn) => { events[key] = fn; } } });
    events.controllerchange();
    const reload = find(p.els.app, (e) => e.tagName === "BUTTON" && e.textContent === "Reload");
    assert.ok(reload);
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "abc123" }];
    p.state.routes["GET /phone/api/ask/*"] = () => [200, { reply: "Done." }];
    p.els["a-text"].value = "hello"; p.click("a-send"); await p.settle(20);
    p.fire(reload, "click", {});
    assert.equal(p.state.reloads, undefined);
    assert.match(p.els.app.textContent, /Finish the current/);
    await p.settle(800); p.fire(reload, "click", {});
    assert.equal(p.state.reloads, 1);
  },
  async core_lost_ask_reply_keeps_its_key_across_reload() {
    const p = await booted();
    p.state.offline = true;
    p.els["a-text"].value = "hello"; p.click("a-send"); await p.settle(20);
    const sent = p.callsTo("/phone/api/ask")[0];
    const saved = p.storage("jarvis.phone.ask");
    assert.equal(JSON.parse(saved).key, sent.body.key);
    const next = await booted({ storage: { "jarvis.phone.ask": saved } });
    assert.equal(next.els["a-text"].value, "hello");
    next.click("a-send"); await next.settle(20);
    assert.equal(next.callsTo("/phone/api/ask")[0].body.key, sent.body.key);
  },
  async core_store_emits_only_changes_and_unsubscribes() {
    const p = await booted();
    const seen = [];
    const off = p.helpers.store.on("test", (value) => seen.push(value));
    assert.equal(p.helpers.store.update("test", { count: 1 }), true);
    assert.equal(p.helpers.store.update("test", { count: 1 }), false);
    off(); p.helpers.store.update("test", { count: 2 });
    assert.equal(seen.length, 1);
  },
  async core_ask_pauses_hidden_and_resumes_without_duplicate_messages() {
    const p = await booted();
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "abc123" }];
    p.state.routes["GET /phone/api/ask/*"] = () => [200, { reply: "Answer arrived." }];
    p.els["a-text"].value = "hello"; p.click("a-send"); await p.settle(20);
    const rows = p.els["a-thread"].children.length;
    p.hide(true); await p.settle(30000);
    assert.equal(p.callsTo("/phone/api/ask/abc123", "GET").length, 0);
    const before = p.state.calls.length;
    await p.settle(30000); assert.equal(p.state.calls.length, before);
    p.hide(false); await p.settle(800);
    assert.equal(p.callsTo("/phone/api/ask").length, 1);
    assert.equal(p.els["a-thread"].children.length, rows);
    assert.match(p.els["a-thread"].textContent, /Answer arrived/);
  },
  async core_pairing_stops_hidden_and_times_out_after_suspension() {
    const p = page();
    p.state.routes["GET /phone/api/me"] = () => [401, { pair: true }];
    await p.settle(10);
    p.els["p-code"].value = "ABCD1234"; p.click("p-go"); await p.settle(20);
    p.hide(true); await p.settle(310000);
    assert.equal(p.callsTo("/phone/pair/status").length, 0);
    p.hide(false); await p.settle(10);
    assert.equal(p.els["pair-form"].hidden, false);
    assert.match(p.els["p-msg"].textContent, /timed out/);
  },
  async core_overview_stops_offline_and_reconnects_on_online() {
    const p = await booted();
    p.win("offline"); const before = p.state.calls.length;
    await p.settle(60000); assert.equal(p.state.calls.length, before);
    assert.equal(p.els.net.hidden, false);
    p.win("online"); await p.settle(20);
    assert.equal(p.els.net.hidden, true);
    assert.ok(p.state.calls.length > before);
  },
  async core_read_retries_are_bounded_and_posts_never_auto_repeat() {
    const p = await booted();
    let attempts = 0;
    p.state.routes["GET /phone/api/test"] = () => (++attempts < 3 ? [503, {}] : [200, { ok: true }]);
    const read = p.helpers.api("/phone/api/test", undefined, { retries: 2, session: SID });
    await p.settle(1600); assert.equal((await read).ok, true);
    assert.equal(attempts, 3);
    assert.equal(p.callsTo("/phone/api/test", "GET")[0].headers["X-JARVIS-Session"], SID);
    p.state.routes["POST /phone/api/test"] = () => [503, {}];
    await assert.rejects(p.helpers.api("/phone/api/test", {}, { retries: 2 }));
    assert.equal(p.callsTo("/phone/api/test").length, 1);
  },
  async an_undecodable_frame_pauses_input_and_a_third_finger_never_clicks() {
    const p = await booted(); await p.open();
    p.fire(p.els["c-img"], "error", {}); await p.settle(5);
    assert.equal(p.els["c-stale"].hidden, false);
    assert.equal(p.sent().pop().t, "release");
    p.frame(2); await p.settle(600); p.state.inputs.length = 0;
    p.ptr("pointerdown", {pointerId:1});
    p.ptr("pointerdown", {pointerId:2});
    p.ptr("pointerdown", {pointerId:3});
    await p.settle(600);
    for (const pointerId of [1,2,3]) p.ptr("pointerup", {pointerId});
    await p.settle(50);
    assert.deepEqual(p.sent(), []);
  },

  async session_options_keep_keys_exclusive_and_start_collapsed() {
    const p = await booted(); await p.open();
    assert.equal(p.els["c-panel"].hidden, true);
    p.click("c-more");
    assert.equal(p.els["c-options"].hidden, false);
    p.click("c-keys-btn");
    assert.equal(p.els["c-options"].hidden, true);
    assert.equal(p.els["c-panel"].hidden, false);
    p.click("c-more");
    assert.equal(p.els["c-panel"].hidden, true);
  },

  async lifecycle_release_drops_queued_presses_behind_an_inflight_batch() {
    const p = await booted(); await p.open();
    const gate = deferred(); p.state.inputGate = gate;
    p.key("ControlLeft"); await flush();
    p.key("ShiftLeft");
    p.win("blur", {}); await p.settle(5);
    gate.resolve(); await p.settle(10);
    assert.equal(p.state.inputs.some(e => e.code === "ShiftLeft" && e.down), false);
    assert.equal(p.sent().pop().t, "release");
    assert.equal(p.findKey("ControlLeft").getAttribute("aria-pressed"), "false");
  },

  async stale_session_blocks_taps_and_releases_held_modifiers() {
    const p = await booted(); await p.open();
    p.key("ControlLeft"); await p.settle(5);
    p.state.nodeAgo = 30; await p.settle(9000);
    assert.equal(p.els["c-stale"].hidden, false);
    assert.equal(p.sent().pop().t, "release");
    const count = p.state.inputs.length;
    p.ptr("pointerdown", {}); p.ptr("pointerup", {}); await p.settle(30);
    assert.equal(p.state.inputs.length, count);
    p.frame(2); await p.settle(600);
    assert.equal(p.els["c-stale"].hidden, true);
  },

  async frame_failure_pauses_input_then_recovers_on_a_new_frame() {
    const p = await booted(); await p.open();
    p.frameReply(502, {error:"Temporary failure"}); await p.settle(5);
    assert.equal(p.els["c-stale"].hidden, false);
    assert.match(p.els["c-stale"].textContent, /Reconnecting/);
    const count = p.state.inputs.length;
    p.ptr("pointerdown", {}); p.ptr("pointerup", {}); await p.settle(30);
    assert.equal(p.state.inputs.length, count);
    p.frame(2); await p.settle(800);
    assert.equal(p.els["c-stale"].hidden, true);
    assert.equal(p.state.frameMaxInFlight, 1);
  },

  async quality_change_releases_stops_then_restarts_explicitly() {
    const p = await booted(); await p.open();
    p.click("c-more"); p.els["c-quality"].value = "high";
    p.click("c-apply"); await p.settle(30);
    assert.equal(p.callsTo("/phone/api/session/start").length, 2);
    assert.equal(p.callsTo("/phone/api/session/start")[1].body.quality, "high");
    const calls = p.state.calls;
    const stop = calls.findIndex(c => c.path === "/phone/api/session/stop");
    const starts = calls.map((c,i) => c.path === "/phone/api/session/start" ? i : -1).filter(i => i >= 0);
    assert.ok(stop < starts[1]);
    assert.ok(calls.slice(0,stop).some(c => c.body && c.body.events && c.body.events.some(e => e.t === "release")));
  },

  async old_input_reply_cannot_clear_a_new_sessions_pending_text() {
    const p = await booted(); await p.open();
    const gate = deferred(); p.state.inputGate = gate;
    p.key("ControlLeft"); await flush();
    p.click("c-end"); await p.settle(20);
    await p.open();
    p.els["c-text"].value = "new session"; p.click("c-send");
    gate.resolve(); await p.settle(50);
    assert.ok(p.state.inputs.some(e => e.t === "text" && e.text === "new session"));
  },

  async pagehide_stops_locally_and_cannot_accept_further_input() {
    const p = await booted(); await p.open();
    p.win("pagehide", {}); await p.settle(5);
    const count = p.state.inputs.length;
    p.ptr("pointerdown", {}); p.ptr("pointerup", {}); await p.settle(30);
    assert.equal(p.state.inputs.length, count);
    assert.equal(p.callsTo("/phone/api/session/stop").length, 1);
    assert.ok(p.callsTo("/phone/api/session/stop")[0].keepalive);
  },

  async request_cost_is_expandable_and_not_a_subscription_charge() {
    const p = await booted();
    p.state.routes["POST /phone/api/ask"] = () => [200, {reply:"Done.", usage:{usd:0.01234,tokens:420,calls:1,engine:"chatgpt"}}];
    p.els["a-text"].value = "hello"; p.click("a-send"); await p.settle(20);
    const content = p.els["a-thread"].textContent;
    assert(content.includes("Usage & cost"));
    assert(content.includes("$0.012340 USD"));
    assert(content.includes("not an extra charge"));
    const details = p.created.filter(e => e.tagName === "DETAILS" && e.attrs.class === "request-cost");
    assert.equal(details.length,1); assert.equal(details[0].getAttribute("open"),null);
  },

  async ask_sends_selected_computer_and_retries_same_target() {
    const p = await booted();
    p.els["a-target"].value = "dell";
    p.els["a-text"].value = "list apps";
    p.click("a-send");
    await p.settle(10);
    const calls = p.callsTo("/phone/api/ask");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].body.device, "dell");
  },

  async installed_ios_uses_full_height_and_measured_keyboard_height() {
    const vv = { height: 810, offsetTop: 0, scale: 1, addEventListener() {} };
    const p = await booted({ ua: "iPhone", standalone: true, innerHeight: 810, visualViewport: vv });
    const root = p.doc.documentElement;
    assert.equal(root.attrs["data-ios-app"], "1");
    assert.equal(root.style["--phone-visible-height"], "var(--full-viewport)");
    p.els["a-text"].tagName = "TEXTAREA";
    p.els["a-text"].focus();
    vv.height = 480; vv.offsetTop = 12;
    p.win("resize");
    assert.equal(root.style["--phone-visible-height"], "480px");
    assert.equal(root.style["--vv-top"], "12px");
    p.els["a-text"].blur();
    p.fire(p.doc, "focusout", {});
    await p.settle(1);
    assert.equal(root.style["--phone-visible-height"], "var(--full-viewport)");
    assert.equal(root.style["--vv-top"], "0px");
  },

  async viewport_gap_is_not_treated_as_a_keyboard() {
    const vv = { height: 810, offsetTop: 0, scale: 1, addEventListener() {} };
    const p = await booted({ visualViewport: vv });
    const root = p.doc.documentElement;
    assert.equal(root.style["--kb"], "0px");
    p.els["a-text"].tagName = "TEXTAREA";
    p.els["a-text"].focus();
    p.win("resize");
    assert.equal(root.style["--kb"], "0px");
    vv.height = 500;
    p.win("resize");
    assert.equal(root.style["--kb"], "344px");
    assert.equal(root.attrs["data-kb"], "1");
    p.els["a-text"].blur();
    p.fire(p.doc, "focusout", {});
    await p.settle(1);
    assert.equal(root.style["--kb"], "0px");
    assert.equal(root.attrs["data-kb"], "0");
  },

  async a_401_with_pair_shows_the_pair_form() {
    const p = page();
    p.state.routes["GET /phone/api/me"] = () => [401, { error: "Pair this phone first.", pair: true }];
    await p.settle(10);
    assert.equal(p.els.pair.hidden, false);
    assert.equal(p.els.app.hidden, true);
    assert.equal(p.els.down.hidden, true);
    assert.equal(p.els["pair-form"].hidden, false);
    assert.equal(p.els["pair-ios"].hidden, true);
    assert.equal(p.els["p-name"].value, "Android phone");
  },

  async a_401_without_pair_is_an_error_not_the_pair_screen() {
    const p = page();
    p.state.routes["GET /phone/api/me"] = () => [401, { error: "Tailscale serve did not forward the client address" }];
    await p.settle(10);
    assert.equal(p.els.pair.hidden, true);
    assert.equal(p.els.down.hidden, false);
    assert.equal(p.els["down-p"].textContent, "Tailscale serve did not forward the client address");
  },

  async network_failure_says_cannot_reach_and_keeps_the_pairing() {
    const p = page();
    p.state.offline = true;
    await p.settle(10);
    assert.equal(p.els.down.hidden, false);
    assert.equal(p.els.pair.hidden, true);
    assert.match(p.els["down-h"].textContent, /Can't reach JARVIS — is Tailscale on\?/);
    // Tailscale comes back: the app retries by itself and opens.
    p.state.offline = false;
    await p.settle(15100);
    assert.equal(p.els.app.hidden, false);
    assert.equal(p.els.net.hidden, true);
    // Lost again mid-use: a banner, never the pair screen, never a logout.
    p.state.offline = true;
    await p.settle(5100);
    assert.equal(p.els.net.hidden, false);
    assert.equal(p.els.app.hidden, false);
    assert.equal(p.els.pair.hidden, true);
    assert.equal(p.els.link.getAttribute("data-st"), "bad");
    assert.equal(p.callsTo("/phone/api/logout").length, 0);
    p.state.offline = false;
    await p.settle(5100);
    assert.equal(p.els.net.hidden, true);
  },

  async ios_outside_the_home_screen_app_gets_the_add_to_home_screen_notice() {
    const ios = { ua: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148", platform: "iPhone" };
    const p = page(ios);
    p.state.routes["GET /phone/api/me"] = () => [401, { error: "Pair first.", pair: true }];
    await p.settle(10);
    assert.equal(p.els["pair-ios"].hidden, false);
    assert.equal(p.els["pair-form"].hidden, true, "no form in Safari: its cookie wouldn't reach the Home Screen app");
    const q = page(Object.assign({ standalone: true }, ios));
    q.state.routes["GET /phone/api/me"] = () => [401, { error: "Pair first.", pair: true }];
    await q.settle(10);
    assert.equal(q.els["pair-ios"].hidden, true);
    assert.equal(q.els["pair-form"].hidden, false);
    assert.equal(q.els["p-name"].value, "iPhone");
  },

  async pairing_waits_for_the_ok_on_the_main_then_opens_the_app() {
    const p = page();
    let paired = false;
    p.state.routes["GET /phone/api/me"] = () => (paired ? [200, p.state.me] : [401, { error: "Pair first.", pair: true }]);
    await p.settle(10);
    p.els["p-code"].value = "abcd-2345"; p.els["p-name"].value = "Pocket\u0007";
    p.click("p-go");
    await p.settle(10);
    const first = p.callsTo("/phone/pair")[0];
    assert.equal(first.body.code, "ABCD2345");
    assert.equal(first.body.name, "Pocket");
    assert.match(first.body.operation_id, /^[A-Za-z0-9_-]{8,64}$/);
    assert.equal(first.headers["X-JARVIS-Phone"], "1");
    assert.equal(first.headers["Content-Type"], "application/json");
    assert.equal(p.els["pair-wait"].hidden, false);
    assert.equal(p.els["pair-form"].hidden, true);
    await p.settle(2010);
    assert.equal(p.callsTo("/phone/pair/status").length, 1);
    assert.equal(p.callsTo("/phone/pair/status")[0].body.operation_id, first.body.operation_id);
    p.state.routes["POST /phone/pair/status"] = () => { paired = true; return [200, { ok: true, phone: { id: "p1", name: "Pocket" } }]; };
    await p.settle(2010);
    assert.equal(p.els.app.hidden, false);
    assert.equal(p.els.pair.hidden, true);
    // A bad code is caught before it's sent.
    const q = page();
    q.state.routes["GET /phone/api/me"] = () => [401, { error: "Pair first.", pair: true }];
    await q.settle(10);
    q.els["p-code"].value = "abc"; q.click("p-go"); await q.settle(10);
    assert.equal(q.callsTo("/phone/pair").length, 0);
    assert.equal(q.els["p-msg"].getAttribute("data-st"), "bad");
  },

  async the_frame_loop_keeps_one_request_in_flight_and_revokes_old_frames() {
    const p = await booted();
    await p.open();
    assert.equal(p.els["c-img"].src, "blob:1");
    assert.equal(p.els["c-st"].textContent, "Live");
    p.frame(2); await p.settle(5);
    p.fire(p.els["c-img"], "load", {});
    assert.deepEqual(p.state.revoked, ["blob:1"], "frame 1 is revoked once frame 2 has loaded");
    p.frame(3); p.frame(4); await p.settle(5);
    // Frame 4 arrived before 3 loaded: 3 is dropped at once, 2 stays until 4 shows.
    assert.deepEqual(p.state.revoked, ["blob:1", "blob:3"]);
    p.fire(p.els["c-img"], "load", {});
    assert.deepEqual(p.state.revoked, ["blob:1", "blob:3", "blob:2"]);
    const urls = p.state.calls.filter((c) => c.path === "/phone/api/session/frame").map((c) => c.url);
    assert.match(urls[1], /after=1$/);
    assert.match(urls[3], /after=3$/);
    await p.settle(8000);  // idle long-polls (204) keep it alive, one at a time
    assert.equal(p.state.frameMaxInFlight, 1);
    p.click("c-end"); await p.settle(10);
    assert.ok(p.state.revoked.includes("blob:4"), "ending revokes the frame on screen");
    assert.equal(p.callsTo("/phone/api/session/stop").length, 1);
  },

  async input_moves_merge_and_a_failed_batch_sends_release_first() {
    const p = await booted();
    await p.open();
    p.click("c-track");
    p.ptr("pointerdown", {});
    for (const x of [212, 224, 236, 248]) p.ptr("pointermove", { clientX: x });
    p.ptr("pointerup", { clientX: 248 });
    await p.settle(100);
    const moves = p.state.inputCalls.filter((evs) => evs.some((e) => e.t === "move"));
    assert.equal(moves.length, 1, "moves within 33 ms go in one request");
    assert.equal(moves[0].filter((e) => e.t === "move").length, 1, "and merge into the last one");
    assert.ok(moves[0][0].x > 0.5, "trackpad moves the cursor right");
    // A tap whose first request fails: the retry starts with a release, then the up.
    p.click("c-direct");
    const n = p.state.inputCalls.length;
    p.state.inputFail = "HTTP 502";
    p.ptr("pointerdown", { clientX: 100, clientY: 100 });
    p.ptr("pointerup", { clientX: 100, clientY: 100 });
    await p.settle(50);
    assert.match(p.els["c-note"].textContent, /Input didn't arrive/);
    await p.settle(400);
    const after = p.state.inputCalls.slice(n);
    assert.deepEqual(after[0].map((e) => e.t), ["down"]);
    assert.deepEqual(after[1].map((e) => e.t), ["release", "up"]);
    assert.equal(p.els["c-note"].textContent, "", "the note clears once input gets through");
  },

  async a_long_press_is_a_right_click_and_never_a_left_click() {
    const p = await booted();
    await p.open();
    p.ptr("pointerdown", {});
    await p.settle(520);
    p.ptr("pointerup", {});
    await p.settle(50);
    assert.deepEqual(p.sent(), [{ t: "down", b: "right", x: 0.5, y: 0.5 }, { t: "up", b: "right", x: 0.5, y: 0.5 }]);
    // Moving 10 px first cancels the right click: that's a left drag instead.
    p.state.inputs.length = 0;
    p.ptr("pointerdown", {});
    p.ptr("pointermove", { clientX: 210 });
    await p.settle(600);
    p.ptr("pointerup", { clientX: 210 });
    await p.settle(50);
    assert.deepEqual(p.sent().map((e) => [e.t, e.b]), [["down", "left"], ["up", "left"]]);
  },

  async two_fingers_scroll_with_whole_wheel_steps_and_a_pinch_zooms_the_picture() {
    const p = await booted();
    await p.open();
    p.ptr("pointerdown", { pointerId: 1, clientX: 150, clientY: 200 });
    p.ptr("pointerdown", { pointerId: 2, clientX: 250, clientY: 200 });
    for (const y of [190, 180, 170, 160, 150, 140]) {
      p.ptr("pointermove", { pointerId: 1, clientX: 150, clientY: y });
      p.ptr("pointermove", { pointerId: 2, clientX: 250, clientY: y });
    }
    p.ptr("pointerup", { pointerId: 1, clientX: 150, clientY: 140 });
    p.ptr("pointerup", { pointerId: 2, clientX: 250, clientY: 140 });
    await p.settle(100);
    const wheels = p.state.inputs.filter((e) => e.t === "wheel");
    assert.ok(wheels.length >= 1);
    assert.ok(wheels.every((e) => Number.isInteger(e.dy) && e.dy > 0), "fingers up scroll down");
    assert.equal(wheels.reduce((a, e) => a + e.dy, 0), 2, "60 px is two 24 px steps");
    assert.deepEqual(p.sent().filter((e) => e.t !== "wheel"), [], "no clicks from a two-finger gesture");
    // Pinch out to 2x around (200, 150), the centre drifting to (250, 150).
    p.ptr("pointerdown", { pointerId: 3, clientX: 150, clientY: 150 });
    p.ptr("pointerdown", { pointerId: 4, clientX: 250, clientY: 150 });
    p.ptr("pointermove", { pointerId: 4, clientX: 350, clientY: 150 });
    p.ptr("pointerup", { pointerId: 3, clientX: 150, clientY: 150 });
    p.ptr("pointerup", { pointerId: 4, clientX: 350, clientY: 150 });
    assert.match(p.els["c-layer"].style.transform, /scale\(2\.000\)/);
    assert.equal(p.els["c-fit"].hidden, false);
    // A tap now lands where the zoomed picture shows it.
    p.state.inputs.length = 0;
    p.ptr("pointerdown", { pointerId: 5, clientX: 100, clientY: 100 });
    p.ptr("pointerup", { pointerId: 5, clientX: 100, clientY: 100 });
    await p.settle(50);
    assert.deepEqual(p.sent()[0], { t: "down", b: "left", x: 0.3125, y: 0.4 });
    p.click("c-fit");
    assert.equal(p.els["c-layer"].style.transform, "");
  },

  async text_goes_on_compositionend_and_enter_and_backspace_maps_to_a_key() {
    const p = await booted();
    await p.open();
    const t = p.els["c-text"];
    p.fire(t, "compositionstart", {});
    t.value = "héllo";
    p.fire(t, "compositionend", {});
    await p.settle(5);
    assert.deepEqual(p.sent().pop(), { t: "text", text: "héllo" });
    assert.equal(t.value, "");
    t.value = "ls -la";
    assert.ok(p.fire(t, "keydown", { key: "Enter" }).prevented);
    await p.settle(5);
    assert.deepEqual(p.sent().pop(), { t: "text", text: "ls -la" });
    p.fire(t, "keydown", { key: "Enter" });
    await p.settle(5);
    assert.deepEqual(p.sent().slice(-2), [{ t: "key", code: "Enter", down: true }, { t: "key", code: "Enter", down: false }]);
    const e = p.fire(t, "beforeinput", { inputType: "deleteContentBackward" });
    await p.settle(5);
    assert.ok(e.prevented);
    assert.deepEqual(p.sent().slice(-2), [{ t: "key", code: "Backspace", down: true }, { t: "key", code: "Backspace", down: false }]);
    t.value = "ab";  // something to delete here: that stays local
    const n = p.sent().length;
    assert.ok(!p.fire(t, "beforeinput", { inputType: "deleteContentBackward" }).prevented);
    assert.equal(p.sent().length, n);
    p.fire(t, "blur", {});  // leaving the field sends what's typed
    await p.settle(5);
    assert.deepEqual(p.sent().pop(), { t: "text", text: "ab" });
    // Sticky Ctrl then c is Ctrl+C, and Ctrl lets go after.
    p.key("ControlLeft");
    assert.equal(p.findKey("ControlLeft").getAttribute("aria-pressed"), "true");
    t.value = "c";
    p.fire(t, "keydown", { key: "Enter" });
    await p.settle(5);
    assert.deepEqual(p.sent().slice(-4).map((x) => [x.code, x.down]),
      [["ControlLeft", true], ["KeyC", true], ["KeyC", false], ["ControlLeft", false]]);
    assert.equal(p.findKey("ControlLeft").getAttribute("aria-pressed"), "false");
    // The keyboard button focuses the field in the tap itself.
    p.els["c-text"].blur();
    p.click("c-type");
    assert.equal(p.doc.activeElement, p.els["c-text"]);
  },

  async going_hidden_lets_go_at_once_and_ends_the_session_after_15_s() {
    const p = await booted();
    await p.open();
    p.hide(true);
    await p.settle(5);
    assert.deepEqual(p.sent().pop(), { t: "release" });
    const frames = p.state.calls.filter((c) => c.path === "/phone/api/session/frame").length;
    await p.settle(14000);
    assert.equal(p.state.calls.filter((c) => c.path === "/phone/api/session/frame").length, frames, "no frames while hidden");
    assert.equal(p.callsTo("/phone/api/session/stop").length, 0);
    await p.settle(1100);
    const stop = p.callsTo("/phone/api/session/stop");
    assert.equal(stop.length, 1);
    assert.ok(stop[0].keepalive);
    assert.equal(p.els["c-st"].textContent, "Ended");
    assert.equal(p.els["c-ph-p"].textContent, "Ended: the app was in the background too long.");
    // A short look away keeps the session and resumes the picture.
    const q = await booted();
    await q.open();
    q.hide(true); await q.settle(5000);
    const before = q.state.calls.filter((c) => c.path === "/phone/api/session/frame").length;
    q.hide(false); await q.settle(5);
    assert.ok(q.state.calls.filter((c) => c.path === "/phone/api/session/frame").length > before);
    assert.equal(q.callsTo("/phone/api/session/stop").length, 0);
    // Suspended past the limit with no timer run (iOS): ended on return.
    const r = await booted();
    await r.open();
    r.hide(true); r.clock.jump(30000); r.hide(false); await r.settle(5);
    assert.equal(r.callsTo("/phone/api/session/stop").length, 1);
  },

  async hostile_strings_are_drawn_as_text() {
    const p = page();
    p.state.overview.devices[1].label = HOSTILE;
    p.state.overview.devices[1].platform = "<script>x()</script>";
    p.state.overview.devices[1].why = { camera: "<b>off</b>" };
    p.state.me.main.label = HOSTILE;
    p.state.routes["POST /phone/api/ask"] = () => [200, { reply: "<i>hi</i>" + HOSTILE }];
    p.state.routes["POST /phone/api/session/start"] = () => [409, { error: "<u>in use</u>" }];
    await p.settle(10);
    const row = p.els["dv-list"].children.find((li) => li.textContent.includes(HOSTILE));
    assert.ok(row, "the label shows literally");
    assert.equal(p.els["main-label"].textContent, HOSTILE);
    await p.go("#/device/dell");
    assert.equal(p.els["d-h"].textContent, HOSTILE);
    assert.equal(p.els["d-camera-why"].textContent, "<b>off</b>");
    p.click("d-control"); await p.settle(20);
    assert.equal(p.els["c-ph-p"].textContent, "<u>in use</u>");
    await p.go("#/ask");
    p.els["a-text"].value = "hello"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send"); await p.settle(20);
    assert.ok(p.els["a-thread"].textContent.includes("<i>hi</i>" + HOSTILE));
    assert.deepEqual(p.state.innerHTML, [], "phone.js never writes innerHTML");
  },

  async a_camera_session_sends_no_input_and_its_end_sends_no_release() {
    const p = await booted();
    await p.open("camera", "zen");
    assert.equal(p.callsTo("/phone/api/session/start")[0].body.kind, "camera");
    assert.equal(p.callsTo("/phone/api/session/start")[0].body.quality, "low");
    assert.equal(p.els["v-st"].textContent, "Live");
    p.ptr("pointerdown", {}); p.ptr("pointerup", {});
    p.hide(true); p.hide(false);
    p.win("blur", {});
    await p.settle(100);
    assert.equal(p.state.inputCalls.length, 0);
    p.click("v-end"); await p.settle(20);
    assert.equal(p.state.inputCalls.length, 0);
    assert.equal(p.callsTo("/phone/api/session/stop").length, 1);
  },

  async a_reload_on_a_session_screen_never_starts_a_session() {
    const p = page({ hash: "#/camera/zen" });
    await p.settle(20);
    assert.equal(p.callsTo("/phone/api/session/start").length, 0);
    assert.equal(p.state.calls.some((c) => c.path === "/phone/api/session/start"), false);
    assert.equal(p.doc.els["s-device"].hidden, false);
  },

  async leaving_the_control_screen_releases_then_stops() {
    const p = await booted();
    await p.open();
    await p.go("#/devices");
    await p.settle(20);
    const tail = p.state.calls.filter((c) => c.method === "POST").slice(-2).map((c) => c.path);
    assert.deepEqual(tail, ["/phone/api/session/input", "/phone/api/session/stop"]);
    assert.deepEqual(p.state.inputs.pop(), { t: "release" });
    // pagehide mid-session: a keepalive release and stop that outlive the page.
    const q = await booted();
    await q.open();
    q.win("pagehide", {});
    const last = q.state.calls.slice(-2);
    assert.deepEqual(last.map((c) => [c.path, c.keepalive]), [["/phone/api/session/input", true], ["/phone/api/session/stop", true]]);
  },

  async ask_polls_a_pending_job_and_shows_waiting_for_ok_in_amber() {
    const p = await booted();
    await p.go("#/ask");
    let polls = 0;
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "j1" }];
    p.state.routes["GET /phone/api/ask/*"] = () => (++polls < 2 ? [200, { pending: true, job: "j1" }] : [200, { reply: "Waiting for your OK on the PC." }]);
    p.els["a-text"].value = "buy a cable"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send");
    await p.settle(5000);
    assert.equal(polls, 2);
    const msgs = p.els["a-thread"].children;
    assert.equal(msgs[0].getAttribute("class"), "msg you");
    assert.equal(msgs[1].getAttribute("data-st"), "needs");
    assert.match(msgs[1].textContent, /Waiting for your OK on the PC/);
    // Busy: JARVIS's own words, shown as a failure.
    p.state.routes["POST /phone/api/ask"] = () => [409, { error: "JARVIS is still on the last request." }];
    p.els["a-text"].value = "again"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send"); await p.settle(20);
    assert.equal(msgs[3].getAttribute("data-st"), "bad");
    assert.match(msgs[3].textContent, /still on the last request/);
  },

  async ask_draws_the_decision_tree_as_text_and_updates_it_in_place() {
    const p = await booted();
    await p.go("#/ask");
    const steps = [
      { id: "recall", parent: "root", label: "Recalling memory", detail: "2 notes", state: "done", kind: "step" },
      { id: "plan", parent: "root", label: "Planning", detail: "2 jobs", state: "run", kind: "step" },
      { id: "plan:a", parent: "plan", label: "Check calendar", detail: "", state: "done", kind: "step" },
      { id: "plan:b", parent: "plan", label: HOSTILE, detail: "<b>x</b>", state: "run", kind: "step" },
      { id: "tool:1", parent: "plan:a", label: "calendar_read", detail: "", state: "done", kind: "tool" },
    ];
    let polls = 0, seen = [];
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "j1", trace: { available: true, rev: 3, steps: steps.slice(0, 2) } }];
    p.state.routes["GET /phone/api/ask/*"] = (b, url) => {
      seen.push(url); polls++;
      if (polls === 1) return [200, { pending: true, job: "j1", trace: { available: true, rev: 5, steps } }];
      return [200, { reply: "All set.", job: "j1", trace: { available: true, rev: 7, steps: steps.map((s) => Object.assign({}, s, { state: "done" })), elapsed: 4 } }];
    };
    p.els["a-text"].value = "plan my day"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send");
    await p.settle(30);
    const msg = p.els["a-thread"].children[1];
    const flow = find(msg, (e) => e.getAttribute("class") === "flow");
    assert.ok(flow, "tree exists while working");
    assert.equal(flow.getAttribute("open"), "");
    assert.match(msg.textContent, /Working · Planning/);
    assert.match(msg.textContent, /Decision path/);
    await p.settle(900);
    const nodes = findAll(flow, (e) => /\bnode\b/.test(e.getAttribute("class") || ""));
    assert.equal(nodes.length, 5, "every real step, nested");
    const plan = nodes.find((n) => n.getAttribute("data-st") === "run" && /Planning/.test(n.textContent));
    assert.ok(findAll(plan, (e) => /\bnode\b/.test(e.getAttribute("class") || "")).length >= 3, "children sit under their parent");
    assert.ok(msg.textContent.includes(HOSTILE), "hostile label shown literally");
    assert.deepEqual(p.state.innerHTML, [], "no innerHTML");
    assert.match(seen[0], /\?after=3$/, "polls from the last rev it has seen");
    const li = nodes[0];
    await p.settle(900);
    assert.equal(p.els["a-thread"].children[1], msg);
    assert.match(msg.textContent, /All set\./);
    assert.equal(msg.getAttribute("data-st"), "ok");
    assert.equal(findAll(msg, (e) => /\bnode\b/.test(e.getAttribute("class") || "")).filter((n) => n === li).length, 1, "nodes are reused, not rebuilt");
    assert.equal(flow.getAttribute("open"), null, "folds away once the answer is in");
    assert.match(msg.textContent, /How it got there/);
    assert.equal(p.storage("jarvis.phone.ask") || "", "", "nothing left to resume");
  },

  async ask_without_events_just_says_working() {
    const p = await booted();
    await p.go("#/ask");
    let polls = 0;
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "j2", trace: { available: false, rev: 0, steps: [] } }];
    p.state.routes["GET /phone/api/ask/*"] = () => (++polls < 2 ? [200, { pending: true, job: "j2", trace: { available: false, rev: 0, steps: [] } }] : [200, { reply: "Done." }]);
    p.els["a-text"].value = "hello"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send");
    await p.settle(30);
    const msg = p.els["a-thread"].children[1];
    assert.equal(find(msg, (e) => e.getAttribute("class") === "flow"), null, "no steps, no invented tree");
    assert.match(msg.textContent, /Working… \d+ s/);
    await p.settle(1500);
    assert.match(msg.textContent, /Done\./);
  },

  async ask_garbage_trace_is_ignored() {
    const p = await booted();
    await p.go("#/ask");
    p.state.routes["POST /phone/api/ask"] = () => [200, { reply: "ok", trace: { steps: [null, 5, "x", { id: 1 }, { id: "a", parent: "a", label: "Loop" }, { id: "b", parent: "c", label: "B" }, { id: "c", parent: "b", label: "C" }] } }];
    p.els["a-text"].value = "x"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send"); await p.settle(30);
    const msg = p.els["a-thread"].children[1];
    assert.match(msg.textContent, /ok/);
    assert.ok(findAll(msg, (e) => /\bnode\b/.test(e.getAttribute("class") || "")).length <= 3, "cycles and junk neither loop nor crash");
  },

  async ask_failure_keeps_the_tree_open_and_try_again_reuses_the_key() {
    const p = await booted();
    await p.go("#/ask");
    const trace = { available: true, rev: 2, steps: [{ id: "brain", parent: "root", label: "Thinking", detail: "", state: "fail", kind: "step" }] };
    let n = 0;
    p.state.routes["POST /phone/api/ask"] = () => (++n === 1 ? [502, { error: "JARVIS couldn't finish that: usage limit", job: "j3", trace }] : [200, { reply: "Second time.", trace }]);
    p.els["a-text"].value = "go"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send"); await p.settle(2000);
    const msg = p.els["a-thread"].children[1];
    assert.equal(msg.getAttribute("data-st"), "bad");
    assert.match(msg.textContent, /usage limit/);
    assert.match(msg.textContent, /Where it stopped/);
    const flow = find(msg, (e) => e.getAttribute("class") === "flow");
    assert.equal(flow.getAttribute("open"), "", "a failed tree stays open");
    const retry = find(msg, (e) => e.getAttribute("class") === "msg-act");
    assert.ok(retry && /Try again/.test(retry.textContent));
    p.fire(retry, "click", {}); await p.settle(30);
    const calls = p.callsTo("/phone/api/ask");
    assert.equal(calls.length, 2);
    assert.notEqual(calls[0].body.key, calls[1].body.key, "a request that failed on the PC retries as a new one");
    assert.equal(calls[0].body.key, calls[0].body.operation_id);
    assert.equal(calls[1].body.key, calls[1].body.operation_id);
    assert.match(calls[0].body.key, /^[A-Za-z0-9_-]{8,64}$/);
    assert.match(msg.textContent, /Second time\./);
  },

  async ask_a_request_that_fails_while_polling_keeps_its_tree_and_offers_a_new_try() {
    const p = await booted();
    await p.go("#/ask");
    const trace = { available: true, rev: 4, steps: [{ id: "brain", parent: "root", label: "Thinking", detail: "", state: "fail", kind: "step" }] };
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "j6", trace: { rev: 1, steps: [] } }];
    p.state.routes["GET /phone/api/ask/*"] = () => [502, { error: "JARVIS couldn't finish that: usage limit", job: "j6", trace }];
    p.els["a-text"].value = "go"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send"); await p.settle(1500);
    const msg = p.els["a-thread"].children[1];
    assert.equal(msg.getAttribute("data-st"), "bad");
    assert.match(msg.textContent, /Where it stopped/);
    const retry = find(msg, (e) => e.getAttribute("class") === "msg-act");
    assert.ok(retry && /Try again/.test(retry.textContent));
  },

  async ask_a_lost_reply_retries_under_the_same_key() {
    const p = await booted();
    await p.go("#/ask");
    let n = 0;
    p.state.routes["POST /phone/api/ask"] = () => { if (++n === 1) throw new TypeError("x"); return [200, { reply: "Once." }]; };
    p.els["a-text"].value = "go"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send"); await p.settle(30);
    const msg = p.els["a-thread"].children[1];
    const retry = find(msg, (e) => e.getAttribute("class") === "msg-act");
    assert.ok(retry && /Try again/.test(retry.textContent), msg.textContent);
    p.fire(retry, "click", {}); await p.settle(30);
    const calls = p.callsTo("/phone/api/ask");
    assert.equal(calls[0].body.key, calls[1].body.key);
    assert.equal(calls[0].body.operation_id, calls[0].body.key);
    assert.equal(calls[1].body.operation_id, calls[0].body.key);
    assert.match(msg.textContent, /Once\./);
  },

  async ask_survives_a_dropped_connection_then_offers_check_again() {
    const p = await booted();
    await p.go("#/ask");
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "j4", trace: { rev: 0, steps: [] } }];
    let ok = false;
    p.state.routes["GET /phone/api/ask/*"] = () => (ok ? [200, { reply: "Back.", job: "j4" }] : [200, { pending: true, job: "j4", trace: { rev: 0, steps: [] } }]);
    p.els["a-text"].value = "long one"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send"); await p.settle(30);
    p.state.offline = true;
    await p.settle(20000);
    const msg = p.els["a-thread"].children[1];
    const again = find(msg, (e) => e.getAttribute("class") === "msg-act");
    assert.ok(again && /Check again/.test(again.textContent), msg.textContent);
    p.state.offline = false; ok = true;
    p.fire(again, "click", {}); await p.settle(1500);
    assert.match(msg.textContent, /Back\./);
  },

  async ask_resumes_a_saved_running_request_after_a_reload() {
    const saved = JSON.stringify({ job: "abc123def456", text: "plan my day", at: 1_800_000_000_000 });
    const p = page({ storage: { "jarvis.phone.ask": saved } });
    p.state.routes["GET /phone/api/ask/*"] = () => [200, { reply: "Planned.", job: "abc123def456" }];
    await p.settle(1500);
    const text = p.els["a-thread"].textContent;
    assert.match(text, /plan my day/);
    assert.match(text, /Planned\./);
    assert.equal(p.callsTo("/phone/api/ask").length, 0, "resuming never submits again");
  },

  async ask_a_vanished_job_says_so_and_stops_polling() {
    const p = await booted();
    await p.go("#/ask");
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "j5", trace: { rev: 0, steps: [] } }];
    let polls = 0;
    p.state.routes["GET /phone/api/ask/*"] = () => { polls++; return [404, { error: "No such request." }]; };
    p.els["a-text"].value = "x"; p.fire(p.els["a-text"], "input", {});
    p.click("a-send"); await p.settle(3000);
    const msg = p.els["a-thread"].children[1];
    assert.match(msg.textContent, /no longer has that request/);
    const seen = polls;
    await p.settle(5000);
    assert.equal(polls, seen);
  },

  async tapping_fit_while_zoomed_clicks_nothing_on_the_computer() {
    const p = await booted();
    await p.open();
    p.ptr("pointerdown", { pointerId: 3, clientX: 150, clientY: 150 });
    p.ptr("pointerdown", { pointerId: 4, clientX: 250, clientY: 150 });
    p.ptr("pointermove", { pointerId: 4, clientX: 450, clientY: 150 });
    p.ptr("pointerup", { pointerId: 3, clientX: 150, clientY: 150 });
    p.ptr("pointerup", { pointerId: 4, clientX: 450, clientY: 150 });
    assert.equal(p.els["c-fit"].hidden, false);
    await p.settle(50);
    p.state.inputs.length = 0;
    const fit = p.els["c-fit"];
    const down = p.ptr("pointerdown", { pointerId: 5, clientX: 380, clientY: 20, target: fit });
    assert.equal(down.prevented, false, "the button keeps its own tap");
    await p.settle(600);  // longer than a long press
    p.ptr("pointerup", { pointerId: 5, clientX: 380, clientY: 20, target: fit });
    p.click("c-fit");
    await p.settle(50);
    assert.equal(p.els["c-layer"].style.transform, "");
    assert.deepEqual(p.sent(), [], "no click, no right click");
  },

  // review 2026-11-08: a still screen only sends keep-alives (every 2 s, reported by a 204 held
  // up to 2.5 s): it is live, not flickering to "No new frames".
  async a_still_screen_stays_live_between_keepalives() {
    const p = await booted();
    p.state.nodeAgo = 1.9;
    await p.open();
    const seen = new Set();
    for (let i = 0; i < 60; i++) { await p.settle(250); seen.add(p.els["c-st"].textContent); }
    assert.deepEqual([...seen], ["Live"]);
    // A node that truly went quiet still shows it.
    p.state.nodeAgo = 30;
    await p.settle(9000);
    assert.equal(p.els["c-st"].textContent, "No new frames");
  },

  // review 2026-11-08: text typed before the first frame (or after the end) is kept, not dropped.
  async text_typed_before_the_picture_is_kept_until_it_can_go() {
    const p = await booted();
    await p.go("#/device/dell");
    p.click("d-control");
    await p.settle(20);
    assert.equal(p.els["c-st"].textContent, "Starting…");
    const t = p.els["c-text"];
    t.value = "rm -rf build && make";
    p.fire(t, "keydown", { key: "Enter" });
    await p.settle(5);
    assert.equal(t.value, "rm -rf build && make");
    assert.match(p.els["c-note"].textContent, /Not sent yet/);
    assert.equal(p.state.inputCalls.length, 0);
    p.frame(1); await p.settle(5);
    p.fire(p.els["c-img"], "load", {});
    assert.equal(p.els["c-note"].textContent, "", "the note goes once the picture is live");
    p.fire(t, "keydown", { key: "Enter" });
    await p.settle(5);
    assert.deepEqual(p.sent().pop(), { t: "text", text: "rm -rf build && make" });
    assert.equal(t.value, "");
  },

  // review 2026-11-08: REC, the countdown and the cross only over a live camera picture.
  async the_viewfinder_shows_only_over_a_live_picture() {
    const p = await booted();
    await p.go("#/device/zen");
    p.click("d-camera");
    await p.settle(20);
    assert.equal(p.els["v-st"].textContent, "Starting…");
    assert.equal(p.els["v-chrome"].hidden, true);
    p.frame(1); await p.settle(5);
    p.fire(p.els["v-img"], "load", {});
    assert.equal(p.els["v-chrome"].hidden, false);
    p.click("v-end"); await p.settle(20);
    assert.equal(p.els["v-chrome"].hidden, true);
  },

  // review 2026-11-08: tailscale serve's bare 502 (JARVIS restarting) isn't a refusal; the
  // phone server's own 503 shows its message.
  async a_bare_gateway_error_says_jarvis_is_not_answering() {
    const p = page();
    p.state.routes["GET /phone/api/me"] = () => [502, undefined];
    await p.settle(10);
    assert.equal(p.els.down.hidden, false);
    assert.equal(p.els["down-h"].textContent, "JARVIS isn't answering");
    assert.match(p.els["down-p"].textContent, /may be restarting/);
    p.state.routes["GET /phone/api/me"] = () => [503, { error: "JARVIS's phone link is off. Turn it on at the PC: jarvis phone on" }];
    await p.settle(15100);
    assert.equal(p.els["down-h"].textContent, "JARVIS didn't let this phone in");
    assert.match(p.els["down-p"].textContent, /jarvis phone on/);
    p.state.routes["GET /phone/api/me"] = () => [200, p.state.me];
    await p.settle(30100); // A second failure backs off to 30 seconds.
    assert.equal(p.els.app.hidden, false);
  },

  // review 2026-11-08: "Darwin" contains "win"; a Mac's Meta key is Command, not Windows.
  async the_meta_key_is_named_for_the_computers_system() {
    for (const [platform, face, spoken] of [["Windows", "Win", "Windows key"], ["Darwin", "⌘", "Command key"], ["Linux", "Super", "Super key"]]) {
      const p = page();
      p.state.overview.devices[1].platform = platform;
      await p.settle(10);
      await p.open();
      const meta = p.findKey("MetaLeft");
      assert.equal(meta.textContent, face, platform);
      assert.equal(meta.getAttribute("aria-label"), spoken, platform);
    }
  },

  // ---- Manage computers (read-only fleet summary) ----
  async fleet_screen_reads_only_the_phone_fleet_endpoint_and_has_no_write_controls() {
    const p = await booted();
    p.state.routes["GET /phone/api/fleet"] = () => [200, FLEET()];
    await p.go("#/fleet");
    await p.settle(20);
    const calls = p.state.calls.filter((c) => c.path.startsWith("/phone/api/fleet") || c.path.startsWith("/api/fleet"));
    assert.deepEqual(calls.map((c) => c.method + " " + c.path), ["GET /phone/api/fleet"]);
    assert.equal(p.els["s-fleet"].hidden, false);
    assert.equal(p.els["s-devices"].hidden, true);
    const names = p.created.filter((e) => e.attrs.class === "fl-row");
    assert.equal(names.length, 3);
    const text = p.els["fl-list"].textContent;
    assert.match(text, /Main PC/); assert.match(text, /Oracle ARM/);
    assert.match(text, /Invite pending/);
    assert.match(text, /May: Hardware and status report/);
    assert.doesNotMatch(text, /locate|Location|203\.0\.113|jarvis-invite/i);
    assert.equal(p.els["fl-guide-wrap"].hidden, false);
    assert.match(p.els["fl-guide"].textContent, /Pick what to connect/);
    // no write control on this screen: only the retry button (hidden) exists
    const ctl = [...findAll(p.els["fl-list"], (e) => ["BUTTON", "A", "INPUT", "SELECT"].includes(e.tagName)), ...findAll(p.els["fl-guide"], (e) => ["BUTTON", "A", "INPUT"].includes(e.tagName))];
    assert.equal(ctl.length, 0);
    assert.equal(p.els["fl-retry-row"].hidden, true);
    assert.deepEqual(p.state.innerHTML, []);
  },

  async fleet_screen_shows_hostile_text_as_text_and_recovers_from_errors() {
    const p = await booted();
    let fail = true;
    p.state.routes["GET /phone/api/fleet"] = () => fail ? [500, { error: "boom" }] : [200, Object.assign(FLEET(), { devices: [{ id: "x", label: HOSTILE, role: "node", status: "online", online: true, profile: "pc", capabilities: ["locate", "inventory"] }] })];
    await p.go("#/fleet"); await p.settle(20);
    assert.match(p.els["fl-state"].textContent, /boom/);
    assert.equal(p.els["fl-retry-row"].hidden, false);
    fail = false; p.click("fl-retry"); await p.settle(20);
    assert.equal(p.els["fl-state"].textContent, "");
    assert.equal(p.els["fl-retry-row"].hidden, true);
    assert.deepEqual(p.state.innerHTML, []);
    assert.match(p.els["fl-list"].textContent, /onerror/);
    assert.doesNotMatch(p.els["fl-list"].textContent, /locate/);
  },

  async fleet_screen_offline_says_so_and_keeps_the_last_reading() {
    const p = await booted();
    p.state.routes["GET /phone/api/fleet"] = () => [200, FLEET()];
    await p.go("#/fleet"); await p.settle(20);
    p.state.offline = true; p.click("fl-retry"); await p.settle(20);
    assert.match(p.els["fl-state"].textContent, /Can't reach JARVIS/);
    assert.match(p.els["fl-list"].textContent, /Main PC/);
  },

  async computer_cards_start_one_session_and_keep_refresh_focus() {
    const p = await booted();
    const row = p.els["dv-list"].children.find((li) => li.textContent.includes("Dell"));
    assert.match(row.textContent, /Online/);
    assert.match(row.textContent, /Last seen/);
    assert.match(row.textContent, /Screen control/);
    const action = row.children[1];
    assert.equal(action.textContent, "Control screen");
    p.fire(action, "click", {}); await p.settle(20);
    assert.equal(p.hash(), "#/control/dell");
    assert.equal(p.callsTo("/phone/api/session/start").length, 1);
    await p.back();
    assert.equal(p.hash(), "#/devices");
    p.els["dv-refresh"].focus();
    p.click("dv-refresh"); await p.settle(20);
    assert.equal(p.doc.activeElement, p.els["dv-refresh"]);
    assert.equal(p.els["dv-refresh"].disabled, false);
    assert.match(p.els["dv-feedback"].textContent, /updated/);
    assert.equal(p.els["dv-list"].children.find((li) => li.textContent.includes("Dell")), row);
  },

  async navigation_restores_scroll_focus_and_guards_the_app_entry() {
    const p = await booted();
    p.els.main.scrollTop = 185;
    await p.go("#/device/dell");
    assert.equal(p.els.main.scrollTop, 0);
    assert.equal(p.doc.activeElement, p.els["d-h"]);
    p.click("d-back"); await p.settle(10);
    assert.equal(p.hash(), "#/devices");
    assert.equal(p.els.main.scrollTop, 185);
    assert.equal(p.doc.activeElement, p.els["dv-h"]);
    await p.back();
    assert.equal(p.hash(), "#/devices");
    await p.back();
    assert.equal(p.hash(), "#/devices");
    await p.go("#/ask/invalid");
    assert.equal(p.els.app.attrs["data-screen"], "devices");
    const direct = await booted({ hash: "#/device/dell" });
    await direct.back();
    assert.equal(direct.hash(), "#/devices");
  },

  async missing_and_offline_computers_have_recovery_without_stale_details() {
    const p = await booted();
    await p.go("#/device/dell");
    assert.equal(p.els["d-r-ssid"].hidden, false);
    p.state.overview.devices[1].online = false;
    p.click("dv-refresh"); await p.settle(20);
    assert.equal(p.els["d-control"].disabled, true);
    assert.match(p.els["d-presence"].textContent, /Wake this computer/);
    p.state.overview.devices = [];
    p.click("dv-refresh"); await p.settle(20);
    assert.equal(p.els["d-r-ssid"].hidden, true);
    assert.match(p.els["d-presence"].textContent, /Manage computers/);
    assert.equal(p.els["dv-empty"].hidden, false);
  },

  async devices_screen_has_one_manage_entry_and_remote_stays_first() {
    const p = await booted();
    const html = fs.readFileSync(path.join(__dirname, "../../jarvis/web/phone/index.html"), "utf8");
    assert.equal((html.match(/href="#\/fleet"/g) || []).length, 1);
    assert.equal((html.match(/data-tab|class="tabs"/g) || []).length >= 1, true);
    assert.equal((html.match(/id="tab-/g) || []).length, 3);
    assert(html.indexOf('id="dv-list"') < html.indexOf('id="dv-manage"'));
    await p.go("#/fleet"); await p.settle(10);
    assert.equal(p.els["tab-devices"].attrs["aria-current"], "page");
  },

  async fleet_screen_names_who_and_hides_secrets_addresses_and_location() {
    const p = await booted();
    const data = FLEET();
    const long = "Spare laptop with a very long name that must stay readable on a narrow phone";
    data.devices[2].label = long;
    data.devices[2].step = "Run the join command on the node before it expires.";
    data.devices[2].address = "203.0.113.9";
    data.devices[2].hostname = "secret-host.example";
    data.devices[2].code = "jarvis-invite-1:SHOULDNOTSHOW0123456789";
    data.devices.push({ id: "mystery", label: "Mystery box", role: "node", status: "something_new", online: false, profile: "pc", capabilities: ["locate", "inventory"], step: "Create an invite." });
    p.state.routes["GET /phone/api/fleet"] = () => [200, data];
    await p.go("#/fleet"); await p.settle(20);
    const text = p.els["fl-list"].textContent;
    assert.match(text, /Next: Run the join command on the node/);
    assert.match(text, new RegExp(long));
    assert.match(text, /Unknown/);
    assert.match(text, /May: Hardware and status report/);
    assert.doesNotMatch(text, /203\.0\.113|secret-host|jarvis-invite|locate/i);
    assert.match(p.els["fl-guide"].textContent, /Who: main/);
    assert.match(p.els["fl-guide"].textContent, /Who: node/);
    assert.match(p.els["fl-guide"].textContent, /On the main PC/);
    const writes = p.state.calls.filter((c) => c.method !== "GET" && /fleet/.test(c.path));
    assert.equal(writes.length, 0);
    assert.equal(p.state.calls.some((c) => c.path === "/api/fleet" || c.path.startsWith("/api/fleet/")), false);
  },

  async fleet_screen_empty_is_distinct_from_loading() {
    const p = await booted();
    let release;
    p.state.routes["GET /phone/api/fleet"] = () => new Promise((ok) => { release = () => ok([200, Object.assign(FLEET(), { devices: [] })]); });
    const going = p.go("#/fleet");
    await going;
    assert.match(p.els["fl-state"].textContent, /Loading/);
    assert.equal(p.els["fl-empty"].hidden, true);
    release();
    await p.settle(20);
    assert.equal(p.els["fl-state"].textContent, "");
    assert.equal(p.els["fl-empty"].hidden, false);
    assert.match(p.els["fl-empty"].textContent, /main PC/);
    assert.equal(p.els["fl-list"].hidden, true);
  },

  async fleet_source_has_no_owner_write_endpoint() {
    const src = fs.readFileSync(path.join(__dirname, "../../jarvis/web/phone/phone.js"), "utf8");
    const chunk = src.slice(src.indexOf("manage computers (read-only"), src.indexOf("this phone's sessions"));
    assert(chunk.includes('"/phone/api/fleet"'));
    assert.equal(/["'`]\/api\/fleet/.test(chunk), false);
    assert.equal(chunk.includes("main-transfer"), false);
    assert.equal(chunk.includes("localStorage"), false);
    assert.equal(chunk.includes('id !== "locate"'), true);
    assert.equal(chunk.includes("localStorage"), false);
    assert.equal(/console\.(log|debug|info|warn|error)/.test(chunk), false);
  },

  async csrf_is_kept_per_main_behind_the_phone_front() {
    const arch = "csrf-arch-0123456789abcdef", dell = "csrf-dell-0123456789abcdef";
    const p = page({ storage: { "jarvis.phone.csrf.by-main": JSON.stringify({ zenbook: arch, "bad key!": dell }) } });
    p.state.replyHeaders = { "X-Jarvis-Main": "zenbook" };
    await p.settle(10);
    const me = p.state.calls.find((c) => c.path === "/phone/api/me");
    assert.equal(me.headers["X-Jarvis-Csrf-zenbook"], arch);
    assert.equal(me.headers["X-Jarvis-Csrf-bad key!"], undefined);
    // MAIN moved to the Dell, which this phone never paired with: only the Dell's slot clears.
    p.state.replyHeaders = { "X-Jarvis-Main": "dell" };
    p.state.routes["GET /phone/api/overview"] = () => [401, { error: "Pair first.", pair: true }];
    await p.settle(5100);
    assert.deepEqual(JSON.parse(p.storage("jarvis.phone.csrf.by-main")), { zenbook: arch });
    p.state.routes["GET /phone/api/me"] = () => [401, { error: "Pair first.", pair: true }];
    p.state.routes["POST /phone/pair"] = () => [202, { pending: true }];
    p.state.routes["POST /phone/pair/status"] = () => {
      p.state.routes["GET /phone/api/me"] = () => [200, p.state.me];
      p.state.routes["GET /phone/api/overview"] = () => [200, p.state.overview];
      return [200, { ok: true, csrf_token: dell, phone: { id: "p1", name: "Pocket" } }];
    };
    await p.settle(10);
    if (!p.els.pair.hidden) {
      p.els["p-code"].value = "ABCD2345";
      p.els["p-name"].value = "Pocket";
      p.click("p-go");
      await p.settle(4010);
    }
    assert.deepEqual(JSON.parse(p.storage("jarvis.phone.csrf.by-main")), { zenbook: arch, dell });
    assert.equal(p.storage("jarvis.phone.csrf"), undefined, "a front pairing never writes the plain slot");
    const last = p.state.calls.filter((c) => c.path === "/phone/api/me").at(-1);
    assert.equal(last.headers["X-Jarvis-Csrf-dell"], dell);
    assert.equal(last.headers["X-Jarvis-Csrf-zenbook"], arch);
  },

  async csrf_is_captured_on_pair_reused_and_cleared_only_on_forget_or_revoke() {
    const token = "csrf-token-value-0123456789abcd";
    const kept = page({ storage: { "jarvis.phone.csrf": token } });
    await kept.settle(10);
    const me = kept.state.calls.find((c) => c.path === "/phone/api/me");
    assert.equal(me.headers["X-CSRF-Token"], token);
    assert.equal(kept.storage("jarvis.phone.csrf"), token);
    kept.state.offline = true;
    await kept.settle(5100);
    assert.equal(kept.storage("jarvis.phone.csrf"), token, "a lost link keeps the token");
    kept.state.offline = false;
    kept.state.routes["GET /phone/api/overview"] = () => [500, { error: "busy" }];
    await kept.settle(5100);
    assert.equal(kept.storage("jarvis.phone.csrf"), token, "a refusal is not a revoke");
    kept.state.routes["GET /phone/api/overview"] = () => [401, { error: "Not paired.", pair: false }];
    await kept.settle(5100);
    assert.equal(kept.storage("jarvis.phone.csrf"), token, "401 without pair is not a revoke");

    const stale = page({ storage: { "jarvis.phone.csrf": "short" } });
    await stale.settle(10);
    assert.equal(stale.storage("jarvis.phone.csrf"), undefined);
    assert.equal(stale.state.calls.find((c) => c.path === "/phone/api/me").headers["X-CSRF-Token"], undefined);

    const p = page();
    let paired = false;
    p.state.routes["GET /phone/api/me"] = () => (paired ? [200, p.state.me] : [401, { error: "Pair first.", pair: true }]);
    await p.settle(10);
    p.els["p-code"].value = "ABCD2345";
    p.els["p-name"].value = "Pocket";
    p.click("p-go");
    await p.settle(10);
    assert.equal(p.callsTo("/phone/pair")[0].headers["X-CSRF-Token"], undefined);
    p.state.routes["POST /phone/pair/status"] = () => {
      paired = true;
      return [200, { ok: true, csrf_token: token, phone: { id: "p1", name: "Pocket" } }];
    };
    await p.settle(4010);
    assert.equal(p.storage("jarvis.phone.csrf"), token);
    assert.equal(p.els.app.hidden, false);
    const authed = p.state.calls.filter((c) => c.path === "/phone/api/me").at(-1);
    assert.equal(authed.headers["X-CSRF-Token"], token);
    await p.open();
    const frame = p.state.calls.find((c) => c.path === "/phone/api/session/frame");
    assert.equal(frame.headers["X-CSRF-Token"], token);
    assert.equal(frame.headers["X-JARVIS-Session"], SID);
    assert.equal(frame.headers["X-JARVIS-Phone"], "1");
    assert.match(frame.url, /^\/phone\/api\/session\/frame\?after=\d+$/);
    assert.equal(frame.url.includes(SID), false);
    assert.doesNotMatch(frame.url, /session=|csrf|token|operation_id/i);
    await p.go("#/phone");
    p.click("pn-forget");
    await p.settle(10);
    p.click("pn-forget");
    await p.settle(30);
    assert.equal(p.els.pair.hidden, false);
    assert.equal(p.storage("jarvis.phone.csrf"), undefined);
    assert.equal(p.callsTo("/phone/api/logout")[0].headers["X-CSRF-Token"], token);

    const revoked = page({ storage: { "jarvis.phone.csrf": token } });
    revoked.state.routes["GET /phone/api/me"] = () => [401, { error: "This phone was revoked.", pair: true }];
    await revoked.settle(10);
    assert.equal(revoked.els.pair.hidden, false);
    assert.equal(revoked.storage("jarvis.phone.csrf"), undefined);
  },

  async operation_ids_are_distinct_and_stable_when_the_same_request_is_retried() {
    const p = page();
    let paired = false;
    p.state.routes["GET /phone/api/me"] = () => (paired ? [200, p.state.me] : [401, { error: "Pair first.", pair: true }]);
    await p.settle(10);
    p.els["p-code"].value = "ABCD2345";
    p.click("p-go");
    await p.settle(2010);
    const pairId = p.callsTo("/phone/pair")[0].body.operation_id;
    assert.equal(p.callsTo("/phone/pair/status")[0].body.operation_id, pairId);
    p.state.offline = true;
    await p.settle(2010);
    const polls = p.callsTo("/phone/pair/status");
    assert.ok(polls.length >= 2);
    assert.ok(polls.every((c) => c.body.operation_id === pairId));
    p.state.offline = false;
    p.state.routes["POST /phone/pair/status"] = () => { paired = true; return [200, { ok: true, phone: { id: "p1", name: "Pocket" } }]; };
    await p.settle(2010);
    assert.equal(p.els.app.hidden, false);

    await p.go("#/ask");
    p.state.routes["POST /phone/api/ask"] = () => [200, { reply: "Done." }];
    p.els["a-text"].value = "one"; p.fire(p.els["a-text"], "input", {}); p.click("a-send"); await p.settle(20);
    p.els["a-text"].value = "two"; p.fire(p.els["a-text"], "input", {}); p.click("a-send"); await p.settle(20);
    const asks = p.callsTo("/phone/api/ask");
    assert.equal(asks[0].body.key, asks[0].body.operation_id);
    assert.equal(asks[1].body.key, asks[1].body.operation_id);
    assert.notEqual(asks[0].body.operation_id, asks[1].body.operation_id);
    assert.notEqual(asks[0].body.operation_id, pairId);

    await p.open();
    const start = p.callsTo("/phone/api/session/start")[0];
    assert.match(start.body.operation_id, /^[A-Za-z0-9_-]{8,64}$/);
    p.ptr("pointerdown", { clientX: 40, clientY: 40 });
    p.ptr("pointerup", { clientX: 40, clientY: 40 });
    await p.settle(40);
    const inputs = p.callsTo("/phone/api/session/input");
    assert.ok(inputs.length >= 1);
    assert.match(inputs[0].body.operation_id, /^[A-Za-z0-9_-]{8,64}$/);
    assert.notEqual(inputs[0].body.operation_id, start.body.operation_id);
    await p.go("#/devices");
    await p.settle(20);
    const stop = p.callsTo("/phone/api/session/stop").at(-1);
    assert.equal(stop.body.operation_id, "stop-" + SID);
    assert.equal(stop.body.session, SID);

    let n = 0;
    p.state.routes["POST /phone/api/logout"] = () => { if (++n === 1) throw new TypeError("lost"); return [200, { ok: true }]; };
    await p.go("#/phone");
    p.click("pn-forget"); await p.settle(10); p.click("pn-forget"); await p.settle(20);
    assert.equal(p.els.pair.hidden, true);
    p.click("pn-forget"); await p.settle(10); p.click("pn-forget"); await p.settle(20);
    const outs = p.callsTo("/phone/api/logout");
    assert.equal(outs.length, 2);
    assert.equal(outs[0].body.operation_id, outs[1].body.operation_id);
    assert.match(outs[0].body.operation_id, /^[A-Za-z0-9_-]{8,64}$/);
    assert.notEqual(outs[0].body.operation_id, pairId);
  },

  async fleet_promote_is_owner_gated_and_names_pending_refused_and_stale() {
    const legacy = await booted();
    legacy.state.me.can_write = true;
    legacy.state.routes["GET /phone/api/fleet"] = () => [200, Object.assign(FLEET(), { can_write: true, actions: [{ id: "invite", path: "/api/fleet/invite" }] })];
    legacy.storage && legacy.nav;
    await legacy.go("#/fleet");
    await legacy.settle(20);
    assert.equal(legacy.els["fl-promote"].hidden, true);
    assert.match(legacy.els["fl-main-status"].textContent, /No authoritative MAIN status/);
    assert.equal(legacy.els["fl-transition"].textContent, "");
    assert.equal(legacy.state.calls.some((c) => c.path.startsWith("/phone/api/main") || c.path.startsWith("/api/fleet")), false);
    assert.match(legacy.els["fl-note"].textContent, /look, not change/);

    const owner = await booted();
    owner.state.me.manage_main = true;
    owner.state.me.authority = { main: "main", main_ready: true, mode: "V2_ACTIVE", epoch: 3, transition: { state: "REQUESTED", candidate: "dell" } };
    owner.state.me.main = { id: "main", label: "Main PC" };
    owner.state.routes["GET /phone/api/fleet"] = () => [200, FLEET()];
    await owner.go("#/fleet");
    await owner.settle(20);
    assert.equal(owner.els["fl-promote"].hidden, false);
    assert.match(owner.els["fl-main-status"].textContent, /MAIN is Main PC/);
    assert.match(owner.els["fl-transition"].textContent, /Promotion pending/);
    assert.equal(owner.els["fl-candidate"].value, "spare");
    assert.doesNotMatch(owner.els["fl-candidate"].textContent, /Oracle|locate|203/);
    owner.state.me.authority.transition = { state: "FAILED", outcome: HOSTILE };
    owner.state.me.authority = owner.state.me.authority;
    owner.click("fl-retry");
    await owner.settle(20);
    assert.match(owner.els["fl-transition"].textContent, /Promotion refused/);
    assert.ok(owner.els["fl-transition"].textContent.includes(HOSTILE));
    assert.deepEqual(owner.state.innerHTML, []);
    owner.state.me.authority.transition = { state: "stale" };
    await owner.go("#/devices");
    await owner.go("#/fleet");
    await owner.settle(20);
    assert.match(owner.els["fl-transition"].textContent, /Promotion stale/);
    assert.match(owner.els["fl-transition"].textContent, /Nothing was changed/);

    const unready = await booted();
    unready.state.me.manage_main = true;
    unready.state.me.authority = { main: null, main_ready: false, mode: "V2_ONLY_NO_MAIN" };
    unready.state.routes["GET /phone/api/fleet"] = () => [200, FLEET()];
    await unready.go("#/fleet");
    await unready.settle(20);
    assert.match(unready.els["fl-main-status"].textContent, /No authoritative MAIN\./);
    assert.equal(unready.els["fl-promote"].hidden, false);
    unready.state.routes["POST /phone/api/main/preview"] = (b) => {
      assert.equal(b.manage_main, undefined);
      assert.equal(b.action, "recover");
      assert.equal(b.profile, "control");
      return [503, { error: "Passkey checks are not configured. Nothing was changed.", code: "passkey_unavailable" }];
    };
    unready.click("fl-promote-go");
    await unready.settle(20);
    assert.match(unready.els["fl-promote-msg"].textContent, /Passkey checks are not configured/);
    assert.equal(unready.callsTo("/phone/api/main/confirm").length, 0);
    assert.equal(unready.nav.credentials, undefined);
  },

  async fleet_promote_sends_a_passkey_proof_and_retries_the_same_operation() {
    const p = await booted();
    p.state.me.manage_main = true;
    p.state.me.authority = { main: "main", main_ready: true, transition: null };
    p.state.me.main = { id: "main", label: "Main PC" };
    const seen = [];
    p.nav.credentials = {
      get: async (opt) => {
        seen.push(opt);
        const enc = (s) => Uint8Array.from(s, (ch) => ch.charCodeAt(0));
        return {
          rawId: enc("raw-id").buffer,
          response: {
            clientDataJSON: enc("client").buffer,
            authenticatorData: enc("authdata").buffer,
            signature: enc("sig").buffer,
          },
        };
      },
    };
    const challenge = "Y2hhbGxlbmdl";
    let previews = 0, confirms = 0;
    p.state.routes["GET /phone/api/fleet"] = () => [200, FLEET()];
    p.state.routes["POST /phone/api/main/preview"] = (b) => {
      previews++;
      assert.equal(b.action, "make_main_now");
      assert.equal(b.candidate, "spare");
      assert.equal(b.manage_main, undefined);
      assert.match(b.operation_id, /^[A-Za-z0-9_-]{8,64}$/);
      return [200, { preview_id: "pv-1", challenge, rp_id: "zenbook.tail.ts.net", credential_ids: ["Y3JlZA"], action: "make_main_now", candidate: "spare" }];
    };
    p.state.routes["POST /phone/api/main/confirm"] = (b) => {
      confirms++;
      if (confirms === 1) throw new TypeError("lost");
      return [200, { transition_id: "tr-1", state: "REQUESTED" }];
    };
    await p.go("#/fleet");
    await p.settle(20);
    p.click("fl-promote-go");
    await p.settle(30);
    assert.equal(seen.length, 1);
    assert.equal(seen[0].publicKey.userVerification, "required");
    assert.equal(seen[0].publicKey.rpId, "zenbook.tail.ts.net");
    assert.equal(seen[0].publicKey.allowCredentials.length, 1);
    assert.equal(seen[0].publicKey.allowCredentials[0].type, "public-key");
    const challengeBytes = seen[0].publicKey.challenge;
    const allowId = seen[0].publicKey.allowCredentials[0].id;
    assert.equal(Object.prototype.toString.call(challengeBytes), "[object Uint8Array]");
    assert.equal(Object.prototype.toString.call(allowId), "[object Uint8Array]");
    assert.equal(Buffer.from(challengeBytes).toString("utf8"), "challenge");
    assert.equal(Buffer.from(allowId).toString("utf8"), "cred");
    assert.match(p.els["fl-transition"].textContent, /Promotion pending/);
    assert.equal(p.els["fl-promote-go"].textContent, "Try again");
    p.click("fl-promote-go");
    await p.settle(30);
    assert.equal(previews, 1, "a lost confirm does not mint another preview");
    assert.equal(confirms, 2);
    const bodies = p.callsTo("/phone/api/main/confirm");
    assert.equal(bodies[0].body.operation_id, bodies[1].body.operation_id);
    assert.equal(bodies[0].body.preview_id, "pv-1");
    assert.equal(bodies[0].body.proof, undefined);
    const proof = bodies[0].body.passkey_proof;
    assert.equal(proof.challenge, challenge);
    assert.equal(proof.type, "public-key");
    assert.equal(proof.rawId, Buffer.from("raw-id").toString("base64url"));
    assert.equal(proof.id, proof.rawId);
    assert.equal(proof.response.clientDataJSON, Buffer.from("client").toString("base64url"));
    assert.equal(proof.response.authenticatorData, Buffer.from("authdata").toString("base64url"));
    assert.equal(proof.response.signature, Buffer.from("sig").toString("base64url"));
    assert.deepEqual(bodies[1].body.passkey_proof, proof);
    assert.equal(bodies[0].body.operation_id === p.callsTo("/phone/api/main/preview")[0].body.operation_id, false);
    assert.equal(JSON.stringify(bodies[0].body).includes("private"), false);
    assert.doesNotMatch(p.els["s-fleet"].textContent, /raw-id|authdata/);
    assert.match(p.els["fl-transition"].textContent, /Promotion pending/);
  },

  async ask_keeps_the_dell_target_and_the_running_decision_trace() {
    const p = await booted();
    await p.go("#/ask");
    const steps = [
      { id: "route", parent: "", label: "Routing to Dell", detail: "dell", state: "run", kind: "step" },
      { id: "tool:1", parent: "route", label: "notes", detail: "", state: "run", kind: "tool" },
    ];
    p.state.routes["POST /phone/api/ask"] = () => [200, { pending: true, job: "abc123", trace: { available: true, rev: 2, steps: steps.slice(0, 1) } }];
    p.state.routes["GET /phone/api/ask/*"] = () => [200, { pending: true, job: "abc123", trace: { available: true, rev: 4, steps, streaming: true } }];
    p.els["a-target"].value = "dell";
    p.fire(p.els["a-target"], "change", {});
    p.els["a-text"].value = "check the Dell notes";
    p.fire(p.els["a-text"], "input", {});
    p.click("a-send");
    await p.settle(40);
    const sent = p.callsTo("/phone/api/ask")[0];
    assert.equal(sent.body.device, "dell");
    assert.equal(sent.body.key, sent.body.operation_id);
    const you = p.els["a-thread"].children[0];
    const msg = p.els["a-thread"].children[1];
    assert.match(you.textContent, /Dell: check the Dell notes/);
    assert.match(msg.textContent, /Decision path/);
    assert.match(msg.textContent, /Routing to Dell/);
    await p.settle(800);
    assert.match(msg.textContent, /Decision path/);
    assert.match(msg.textContent, /notes/);
    const flow = find(msg, (e) => e.getAttribute("class") === "flow");
    assert.equal(flow.getAttribute("open"), "");
    assert.equal(p.els["a-target"].value, "dell");
    assert.equal(p.els["a-target"].disabled, true);
  },

  async csrf_storage_failure_keeps_this_tab_and_a_reload_offers_repair() {
    const token = "csrf-token-value-0123456789abcd";
    const broken = page({ storageSetThrows: true });
    let paired = false;
    broken.state.routes["GET /phone/api/me"] = () => (paired ? [200, broken.state.me] : [401, { error: "Pair first.", pair: true }]);
    await broken.settle(10);
    broken.els["p-code"].value = "ABCD2345";
    broken.els["p-name"].value = "Pocket";
    broken.click("p-go");
    await broken.settle(10);
    broken.state.routes["POST /phone/pair/status"] = () => {
      paired = true;
      return [200, { ok: true, csrf_token: token, phone: { id: "p1", name: "Pocket" } }];
    };
    await broken.settle(4010);
    assert.equal(broken.els.app.hidden, false, "the open tab keeps the check in memory");
    assert.equal(broken.storage("jarvis.phone.csrf"), undefined);
    const authed = broken.state.calls.filter((c) => c.path === "/phone/api/me").at(-1);
    assert.equal(authed.headers["X-CSRF-Token"], token);
    assert.match(broken.els.app.textContent, /could not be saved/);
    assert.equal(broken.state.calls.some((c) => String(c.url).includes(token)), false);
    assert.doesNotMatch(broken.hash(), /csrf|token/i);

    const again = page();
    again.state.routes["GET /phone/api/me"] = () => [403, { error: "The session token did not match this origin.", code: "csrf" }];
    await again.settle(10);
    assert.equal(again.els.app.hidden, true);
    assert.equal(again.els.pair.hidden, true);
    assert.equal(again.els.down.hidden, false);
    assert.match(again.els["down-h"].textContent, /Pair this phone again/);
    assert.match(again.els["down-p"].textContent, /does not open the app/);
    assert.equal(again.state.calls.some((c) => c.path === "/phone/api/overview"), false, "the paired cookie is not used as authorization");
    const repair = find(again.els.down, (e) => e.tagName === "BUTTON" && e.textContent === "Pair again");
    assert.ok(repair);
    assert.equal(repair.hidden, false);
    assert.equal(again.els["down-retry"].hidden, true);
    const before = again.state.calls.length;
    await again.settle(16000);
    assert.equal(again.state.calls.length, before, "a csrf refusal does not keep presenting the cookie");
    assert.equal(again.els.pair.hidden, true);
    again.fire(repair, "click", {});
    await again.settle(10);
    assert.equal(again.els.pair.hidden, false);
    assert.equal(again.els["pair-form"].hidden, false);
    assert.equal(again.els.app.hidden, true);
    assert.doesNotMatch(again.hash(), /csrf|token/i);
    assert.equal(again.callsTo("/phone/api/logout").length, 0);

    const kept = page({ storage: { "jarvis.phone.csrf": token } });
    await kept.settle(10);
    kept.state.offline = true;
    await kept.settle(5100);
    assert.equal(kept.storage("jarvis.phone.csrf"), token);
    kept.state.offline = false;
    kept.state.routes["GET /phone/api/overview"] = () => [403, { error: "Refused.", code: "forbidden" }];
    await kept.settle(5100);
    assert.equal(kept.storage("jarvis.phone.csrf"), token, "an ordinary 403 keeps the saved check");
    assert.equal(kept.els.app.hidden, false);
    assert.equal(kept.els.down.hidden, true);
    assert.equal(find(kept.els.down, (e) => e.textContent === "Pair again"), null);
  },

  async ask_csrf_refusal_keeps_the_key_and_the_target() {
    const token = "csrf-token-value-0123456789abcd";
    const p = await booted({ storage: { "jarvis.phone.csrf": token } });
    await p.go("#/ask");
    p.els["a-target"].value = "dell";
    p.fire(p.els["a-target"], "change", {});
    p.els["a-text"].value = "check the Dell notes";
    p.fire(p.els["a-text"], "input", {});
    p.state.routes["POST /phone/api/ask"] = () => [403, { error: "The session token did not match this origin.", code: "csrf" }];
    p.click("a-send");
    await p.settle(30);
    const sent = p.callsTo("/phone/api/ask")[0];
    assert.equal(sent.body.device, "dell");
    assert.equal(sent.body.key, sent.body.operation_id);
    const saved = JSON.parse(p.storage("jarvis.phone.ask"));
    assert.equal(saved.held, true);
    assert.equal(saved.key, sent.body.key);
    assert.equal(saved.target, "dell");
    assert.equal(saved.text, "check the Dell notes");
    assert.equal(p.storage("jarvis.phone.csrf"), token, "the refusal does not clear the saved check");
    assert.equal(p.storage("jarvis.phone.target"), "dell");
    assert.equal(p.els.app.hidden, true);
    assert.match(p.els["down-h"].textContent, /Pair this phone again/);
    assert.equal(p.state.calls.some((c) => String(c.url).includes(token)), false);

    const next = await booted({ storage: {
      "jarvis.phone.csrf": token,
      "jarvis.phone.ask": p.storage("jarvis.phone.ask"),
      "jarvis.phone.target": "dell",
    } });
    await next.settle(20);
    assert.equal(next.els["a-text"].value, "check the Dell notes");
    await next.go("#/ask");
    assert.equal(next.els["a-target"].value, "dell");
    next.state.routes["POST /phone/api/ask"] = () => [200, { reply: "Done." }];
    next.click("a-send");
    await next.settle(20);
    const retried = next.callsTo("/phone/api/ask")[0];
    assert.equal(retried.body.key, saved.key);
    assert.equal(retried.body.operation_id, saved.key);
    assert.equal(retried.body.device, "dell");
  },

  async fleet_promote_pending_clears_when_the_authoritative_main_switches() {
    const p = await booted();
    p.state.me.manage_main = true;
    p.state.me.authority = { main: "main", main_ready: true, transition: null };
    p.state.me.main = { id: "main", label: "Main PC" };
    p.nav.credentials = {
      get: async () => {
        const enc = (s) => Uint8Array.from(s, (ch) => ch.charCodeAt(0));
        return {
          rawId: enc("raw-id").buffer,
          response: {
            clientDataJSON: enc("client").buffer,
            authenticatorData: enc("authdata").buffer,
            signature: enc("sig").buffer,
          },
        };
      },
    };
    p.state.routes["GET /phone/api/fleet"] = () => [200, FLEET()];
    p.state.routes["POST /phone/api/main/preview"] = (b) => {
      assert.equal(b.candidate, "spare");
      return [200, { preview_id: "pv-9", challenge: "Y2hhbGxlbmdl", rp_id: "zenbook.tail.ts.net", credential_ids: ["Y3JlZA"], action: "make_main_now", candidate: "spare" }];
    };
    p.state.routes["POST /phone/api/main/confirm"] = (b) => {
      assert.match(b.operation_id, /^[A-Za-z0-9_-]{8,64}$/);
      return [200, { transition_id: "tr-9", state: "REQUESTED", candidate: "spare" }];
    };
    await p.go("#/fleet");
    await p.settle(20);
    p.click("fl-promote-go");
    await p.settle(30);
    assert.equal(p.callsTo("/phone/api/main/confirm").length, 1);
    assert.match(p.els["fl-transition"].textContent, /Promotion pending/);
    assert.match(p.els["fl-main-status"].textContent, /MAIN is Main PC/);
    assert.equal(p.state.me.authority.main, "main", "a pending confirm does not invent the new MAIN");
    const moved = FLEET();
    moved.devices = moved.devices.map((d) => {
      if (d.id === "spare") return Object.assign({}, d, { role: "main", status: "main", online: true });
      if (d.id === "main") return Object.assign({}, d, { role: "node", status: "online" });
      return d;
    });
    p.state.routes["GET /phone/api/fleet"] = () => [200, moved];
    p.state.me.authority = { main: "spare", main_ready: true, transition: { state: "REQUESTED", candidate: "spare" } };
    p.state.me.main = { id: "spare", label: "Spare" };
    p.click("fl-retry");
    await p.settle(30);
    assert.equal(/Promotion pending/.test(p.els["fl-transition"].textContent), false);
    assert.match(p.els["fl-main-status"].textContent, /MAIN is Spare/);
    assert.match(p.els["fl-list"].textContent, /Spare/);
    assert.equal(p.callsTo("/phone/api/main/confirm").length, 1);
  },

  async fleet_promote_unrelated_main_keeps_pending() {
    const p = await booted();
    p.state.me.manage_main = true;
    p.state.me.authority = { main: "A", main_ready: true, transition: null };
    p.state.me.main = { id: "A", label: "Source A" };
    const fleet = FLEET();
    fleet.devices = [
      { id: "A", label: "Source A", role: "main", profile: "pc", profile_label: "PC", status: "main", online: true, capabilities: [] },
      { id: "B", label: "Candidate B", role: "node", profile: "pc", profile_label: "PC", status: "online", online: true, capabilities: [] },
      { id: "C", label: "Other C", role: "node", profile: "pc", profile_label: "PC", status: "online", online: true, capabilities: [] },
    ];
    p.nav.credentials = {
      get: async () => {
        const enc = (s) => Uint8Array.from(s, (ch) => ch.charCodeAt(0));
        return {
          rawId: enc("raw-id").buffer,
          response: {
            clientDataJSON: enc("client").buffer,
            authenticatorData: enc("authdata").buffer,
            signature: enc("sig").buffer,
          },
        };
      },
    };
    p.state.routes["GET /phone/api/fleet"] = () => [200, fleet];
    p.state.routes["POST /phone/api/main/preview"] = (b) => {
      assert.equal(b.candidate, "B");
      return [200, { preview_id: "pv-abc", challenge: "Y2hhbGxlbmdl", rp_id: "zenbook.tail.ts.net", credential_ids: ["Y3JlZA"], action: "make_main_now", candidate: "B" }];
    };
    p.state.routes["POST /phone/api/main/confirm"] = (b) => {
      assert.match(b.operation_id, /^[A-Za-z0-9_-]{8,64}$/);
      return [200, { transition_id: "tr-abc", state: "REQUESTED", candidate: "B" }];
    };
    await p.go("#/fleet");
    await p.settle(20);
    assert.equal(p.els["fl-candidate"].value, "B");
    p.click("fl-promote-go");
    await p.settle(30);
    const confirmId = p.callsTo("/phone/api/main/confirm")[0].body.operation_id;
    assert.match(p.els["fl-transition"].textContent, /Promotion pending/);
    assert.match(p.els["fl-main-status"].textContent, /MAIN is Source A/);
    p.state.me.authority = { main: "C", main_ready: true, transition: null };
    p.state.me.main = { id: "C", label: "Other C" };
    p.click("fl-retry");
    await p.settle(30);
    assert.match(p.els["fl-transition"].textContent, /Promotion pending/);
    assert.match(p.els["fl-transition"].textContent, /uncertain/);
    assert.match(p.els["fl-main-status"].textContent, /MAIN is Other C/);
    assert.equal(p.els["fl-promote-msg"].textContent.includes("finished"), false);
    assert.equal(p.callsTo("/phone/api/main/confirm").length, 1);
    assert.equal(p.callsTo("/phone/api/main/confirm")[0].body.operation_id, confirmId);
  },

  async frame403csrf() {
    const token = "csrf-token-value-0123456789abcd";
    const p = await booted({ storage: { "jarvis.phone.csrf": token } });
    await p.open();
    const framesAtOpen = p.state.calls.filter((c) => c.path === "/phone/api/session/frame").length;
    p.frameReply(403, { error: "The session token did not match this origin.", code: "csrf", token });
    await p.settle(30);
    assert.equal(p.els.app.hidden, true);
    assert.equal(p.els.pair.hidden, true);
    assert.equal(p.els.down.hidden, false);
    assert.match(p.els["down-h"].textContent, /Pair this phone again/);
    const repair = find(p.els.down, (e) => e.tagName === "BUTTON" && e.textContent === "Pair again");
    assert.ok(repair);
    assert.equal(repair.hidden, false);
    assert.equal(p.els["down-retry"].hidden, true);
    assert.equal(p.storage("jarvis.phone.csrf"), token);
    const frames = p.state.calls.filter((c) => c.path === "/phone/api/session/frame");
    assert.equal(frames.length, framesAtOpen, "a csrf frame refusal does not start another frame request");
    for (const c of p.state.calls) {
      assert.equal(String(c.url).includes(token), false);
      assert.doesNotMatch(c.url, /csrf|token|operation_id/i);
    }
    assert.equal(p.callsTo("/phone/api/session/stop").length, 0);
    assert.equal(p.callsTo("/phone/api/session/input").length, 0);
    const before = p.state.calls.length;
    await p.settle(20000);
    assert.equal(p.state.calls.length, before, "csrf recovery does not poll, retry frames, or beacon");
    assert.equal(p.storage("jarvis.phone.csrf"), token);

    const other = await booted({ storage: { "jarvis.phone.csrf": token } });
    await other.open();
    other.frameReply(403, { error: "Refused.", code: "forbidden" });
    await other.settle(40);
    assert.equal(other.storage("jarvis.phone.csrf"), token, "another 403 keeps the saved check");
    assert.equal(other.els.app.hidden, false);
    assert.equal(other.els.down.hidden, true);
    assert.equal(find(other.els.down, (e) => e.textContent === "Pair again"), null);

    const lost = await booted({ storage: { "jarvis.phone.csrf": token } });
    await lost.open();
    lost.state.offline = true;
    await lost.settle(4000);
    assert.equal(lost.storage("jarvis.phone.csrf"), token, "a lost frame link keeps the saved check");
    assert.equal(lost.els.down.hidden, true);
    assert.equal(find(lost.els.down, (e) => e.textContent === "Pair again"), null);
  },

};

(async () => {
  const names = process.argv[2] ? [process.argv[2]] : Object.keys(scenarios);
  for (const name of names) {
    if (!scenarios[name]) throw new Error("unknown scenario " + name);
    await scenarios[name]();
    console.log("PASS " + name);
  }
})().catch((e) => { console.error(e); process.exit(1); });
