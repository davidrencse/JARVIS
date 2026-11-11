// Behavioural tests for the console's Remote view (jarvis/web/remote.js). Every browser API
// is a fake: no real pointer, keyboard, network or timer is touched.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SID = "a".repeat(32);
const flush = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r)); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function fakeClock() {
  let now = 1_000_000, id = 0;
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
    jump(ms) { now += ms; },  // time passes but no timer fires (a throttled background tab)
  };
}

function page() {
  const clock = fakeClock();
  const els = {};
  const doc = {
    hidden: false, activeElement: null, fullscreenElement: null, handlers: {},
    getElementById: (id) => els[id] || (els[id] = element(id)),
    addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
    createElement: (tag) => element(tag),
    exitFullscreen() { doc.fullscreenElement = null; fire(doc, "fullscreenchange", {}); return Promise.resolve(); },
  };
  function element(id) {
    return {
      id, hidden: false, attrs: {}, handlers: {}, textContent: "", innerHTML: "", className: "", value: "", disabled: false,
      naturalWidth: 0, naturalHeight: 0, rect: { left: 0, top: 0, width: 800, height: 500 },
      classList: { toggle() {}, add() {}, remove() {} },
      setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      removeAttribute(k) { delete this.attrs[k]; },
      addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
      querySelectorAll: () => [], append() {},
      focus() { doc.activeElement = this; fire(this, "focus", {}); },
      blur() { if (doc.activeElement === this) { doc.activeElement = null; fire(this, "blur", {}); } },
      setPointerCapture() {}, releasePointerCapture() {},
      getBoundingClientRect() { return this.rect; },
    };
  }
  function fire(target, kind, init) {
    const e = Object.assign({ prevented: false, stopped: false, preventDefault() { this.prevented = true; },
      stopPropagation() { this.stopped = true; } }, init);
    for (const f of target.handlers[kind] || []) f(e);
    return e;
  }
  els["view-remote"] = element("view-remote");
  els["rm-root"] = element("rm-root");

  const state = {
    clock, els, doc, inputs: [], calls: [], fetches: [], frames: [], session: null, inputGate: null, inputFail: null,
    locks: 0, unlocks: 0, winHandlers: {},
    devices: [{ id: "dell", label: "Dell", ready: true, online: true }],
  };
  const api = async (p, body) => {
    state.calls.push([p, body]);
    if (p === "/api/remote") return { devices: state.devices, session: state.session };
    if (p === "/api/remote/start") {
      state.session = { id: SID, device: "dell", state: "starting", error: null, width: 0, height: 0, quality: body.quality };
      return state.session;
    }
    if (p === "/api/remote/stop") { if (state.session) state.session = Object.assign({}, state.session, { state: "ended", error: "Disconnected." }); return { ok: true }; }
    if (p === "/api/remote/input") {
      const gate = state.inputGate; state.inputGate = null;
      if (gate) await gate.promise;
      if (state.inputFail) { const m = state.inputFail; state.inputFail = null; throw new Error(m); }
      state.inputs.push(...JSON.parse(JSON.stringify(body.events)));  // out of the VM realm
      return { ok: true, queued: body.events.length };
    }
    throw new Error("unexpected api " + p);
  };
  const fetch = (url, opts = {}) => {
    state.fetches.push([url, opts]);
    if (!String(url).startsWith("/api/remote/frame")) return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    const next = state.frames.shift();
    if (next) return Promise.resolve(next());
    return new Promise((_, fail) => opts.signal?.addEventListener("abort", () => {
      const e = new Error("aborted"); e.name = "AbortError"; fail(e);
    }));
  };
  const context = {
    document: doc, fetch, localStorage: { getItem: () => null, setItem() {} },
    performance: { now: () => clock.now() }, Date: Object.assign(function () {}, { now: () => clock.now() }),
    setTimeout: clock.setTimeout, clearTimeout: clock.clear, setInterval: clock.setInterval, clearInterval: clock.clear,
    AbortController, Promise, URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    MutationObserver: class { observe() {} },
    navigator: { keyboard: { lock: async () => { state.locks++; }, unlock: () => { state.unlocks++; } } },
    addEventListener: (k, f) => { (state.winHandlers[k] ||= []).push(f); },
    JSON, Math, Number, String, Object, Array, Set, Error, parseInt, parseFloat, encodeURIComponent, RegExp,
  };
  context.window = context;
  context.Jarvis = { api };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../jarvis/web/remote.js"), "utf8"), context);
  const view = els["rm-view"];
  const ev = (o) => Object.assign({ pointerId: 1, button: -1, buttons: 0, clientX: 400, clientY: 250 }, o);
  return {
    state, view, els, doc, clock, fire,
    down: (o) => fire(view, "pointerdown", ev(o)),
    move: (o) => fire(view, "pointermove", ev(o)),
    up: (o) => fire(view, "pointerup", ev(o)),
    key: (kind, code) => fire(view, kind, { code, key: code, isComposing: false }),
    sent: () => state.inputs.filter((e) => e.t !== "move"),
    frame(seq = 1) {
      state.frames.push(() => ({ ok: true, status: 200, blob: async () => ({}),
        headers: { get: (h) => ({ "X-Frame-Seq": String(seq), "X-Frame-Width": "1600", "X-Frame-Height": "1000" })[h] ?? null } }));
    },
    async connect({ frame = true } = {}) {
      await flush();
      if (frame) this.frame();
      fire(els["rm-go"], "click", {});
      await clock.advance(50);
    },
  };
}

const scenarios = {
  async plain_click_sends_one_down_and_one_up() {
    const p = page(); await p.connect();
    p.down({ button: 0, buttons: 1 }); p.up({ button: 0, buttons: 0 });
    await p.clock.advance(100);
    assert.deepEqual(p.sent().map((e) => [e.t, e.b]), [["down", "left"], ["up", "left"]]);
    assert.deepEqual([p.sent()[0].x, p.sent()[0].y], [0.5, 0.5]);
  },

  async second_button_while_one_is_held_never_sticks() {
    // H2: Pointer Events report a chorded press/release as pointermove, not pointerdown/up.
    const p = page(); await p.connect();
    p.down({ button: 0, buttons: 1 });
    p.move({ button: 2, buttons: 3 });   // right goes down while left is held
    p.move({ button: 0, buttons: 2 });   // left comes up while right is held
    p.up({ button: 2, buttons: 0 });     // right up: the last button
    await p.clock.advance(100);
    assert.deepEqual(p.sent().map((e) => [e.t, e.b]),
      [["down", "left"], ["down", "right"], ["up", "left"], ["up", "right"]]);
  },

  async a_missed_up_is_sent_when_moves_say_nothing_is_down() {
    const p = page(); await p.connect();
    p.down({ button: 0, buttons: 1 });
    p.move({ buttons: 0 });              // released somewhere we never heard about
    await p.clock.advance(100);
    assert.deepEqual(p.sent().map((e) => [e.t, e.b]), [["down", "left"], ["up", "left"]]);
  },

  async failed_input_keeps_downs_queued_while_it_was_in_flight() {
    // H3: a down queued during a failed request must still get its up.
    const p = page(); await p.connect();
    p.state.inputGate = deferred(); p.state.inputFail = "HTTP 502";
    const gate = p.state.inputGate;
    p.key("keydown", "KeyA"); p.key("keyup", "KeyA");  // first batch in flight, second queued
    p.down({ button: 0, buttons: 1 });                  // queued while the first is in flight
    gate.resolve(); await p.clock.advance(400);         // first batch fails; the retry goes out
    p.up({ button: 0, buttons: 0 });
    await p.clock.advance(400);
    const s = p.sent().map((e) => e.t === "key" ? ["key", e.code, e.down] : [e.t, e.b]);
    assert.deepEqual(s.slice(0, 4), [["release", undefined], ["key", "KeyA", false], ["down", "left"], ["up", "left"]]);
    assert.equal(p.state.calls.filter(([u]) => u === "/api/remote/input").length >= 3, true);
  },

  async keys_before_the_first_frame_stay_out_of_the_console() {
    // L2: Digit1 would switch the console view (and end the session); Space would start the mic.
    const p = page(); await p.connect({ frame: false });
    for (const code of ["Digit1", "Space"]) {
      const e = p.key("keydown", code);
      assert.ok(e.prevented && e.stopped, code + " must not reach the console's shortcuts");
      const u = p.key("keyup", code);
      assert.ok(u.prevented && u.stopped);
    }
    await p.clock.advance(100);
    assert.deepEqual(p.state.inputs, []);
    // With no session at all, keys behave normally.
    const q = page(); await flush();
    assert.ok(!q.key("keydown", "Digit1").prevented);
  },

  async hidden_tab_ends_with_its_own_reason_before_the_hub_idles_out() {
    // M3: 15 s < the hub's MAIN_IDLE (20 s), so the console's own reason wins.
    const p = page(); await p.connect();
    p.doc.hidden = true; p.fire(p.doc, "visibilitychange", {});
    await p.clock.advance(14000);
    assert.equal(p.els["rm-go"].textContent, "Disconnect");
    await p.clock.advance(1100);
    assert.equal(p.els["rm-st"].textContent, "Stopped: the tab was in the background too long.");
    assert.ok(p.state.fetches.some(([u]) => u === "/api/remote/stop"));
  },

  async throttled_background_timer_still_gives_the_background_reason() {
    const p = page(); await p.connect();
    p.doc.hidden = true; p.fire(p.doc, "visibilitychange", {});
    p.clock.jump(25000);                 // the timer never got to run
    p.doc.hidden = false; p.fire(p.doc, "visibilitychange", {});
    await flush();
    assert.equal(p.els["rm-st"].textContent, "Stopped: the tab was in the background too long.");
  },

  async short_tab_switch_keeps_the_session() {
    const p = page(); await p.connect();
    p.doc.hidden = true; p.fire(p.doc, "visibilitychange", {});
    await p.clock.advance(5000);
    p.doc.hidden = false; p.fire(p.doc, "visibilitychange", {});
    await p.clock.advance(100);
    assert.equal(p.els["rm-go"].textContent, "Disconnect");
  },

  async unknown_session_says_the_main_restarted() {
    // L1: a 404 is the hub forgetting the session (JARVIS restarted), not a lost network.
    const p = page(); await flush();
    p.state.frames.push(() => ({ ok: false, status: 404, json: async () => ({ error: "Unknown remote session." }) }));
    p.fire(p.els["rm-go"], "click", {}); await p.clock.advance(50);
    assert.match(p.els["rm-ph"].innerHTML, /JARVIS on the main restarted/);
    assert.equal(p.els["rm-go"].textContent, "Connect");
  },

  async unknown_session_on_input_ends_without_retrying() {
    const p = page(); await p.connect();
    p.state.inputFail = "Unknown remote session.";
    p.key("keydown", "KeyA");
    await p.clock.advance(2000);
    assert.equal(p.els["rm-go"].textContent, "Connect");
    assert.equal(p.state.calls.filter(([u]) => u === "/api/remote/input").length, 1);
  },

  async drag_past_the_edge_keeps_moving_at_the_edge() {
    // L3: while a button is held, moves outside the picture clamp to its edge.
    const p = page(); await p.connect();
    p.down({ button: 0, buttons: 1 });
    p.move({ buttons: 1, clientX: 950, clientY: 250 });
    await p.clock.advance(100);
    assert.deepEqual(p.state.inputs.filter((e) => e.t === "move").pop(), { t: "move", x: 1, y: 0.5 });
    // Without a held button, a move in the letterbox goes nowhere.
    p.up({ button: 0, buttons: 0, clientX: 950 });
    await p.clock.advance(100);
    const n = p.state.inputs.length;
    p.move({ clientX: 950 }); await p.clock.advance(100);
    assert.equal(p.state.inputs.length, n);
  },

  async an_up_with_no_picture_size_is_sent_without_a_point() {
    // L3: never an up at (0, 0), which threw the remote pointer into the corner.
    const p = page(); await p.connect();
    p.down({ button: 0, buttons: 1 });
    p.els["rm-img"].rect = { left: 0, top: 0, width: 0, height: 0 };
    p.up({ button: 0, buttons: 0 });
    await p.clock.advance(100);
    assert.deepEqual(p.sent().pop(), { t: "up", b: "left" });
  },

  async back_and_forward_buttons_never_navigate_the_console() {
    const p = page(); await p.connect();
    for (const button of [3, 4]) {
      assert.ok(p.down({ button, buttons: 8 }).prevented);
      assert.ok(p.fire(p.view, "mouseup", { button }).prevented);
      assert.ok(p.fire(p.view, "auxclick", { button }).prevented);
    }
    assert.ok(!p.fire(p.view, "mouseup", { button: 0 }).prevented);
    await p.clock.advance(100);
    assert.deepEqual(p.sent(), []);
  },

  async a_big_wheel_fling_is_clamped_not_dropped() {
    // L5: the hub refuses |dy| > 20, so one 5000 px fling used to scroll nothing.
    const p = page(); await p.connect();
    p.fire(p.view, "wheel", { deltaY: 5000, deltaMode: 0, clientX: 400, clientY: 250 });
    p.fire(p.view, "wheel", { deltaY: -250, deltaMode: 0, clientX: 400, clientY: 250 });
    await p.clock.advance(100);
    const dys = p.state.inputs.filter((e) => e.t === "wheel").map((e) => e.dy);
    assert.equal(dys[0], 20);
    assert.ok(dys.every((d) => Math.abs(d) <= 20 && d !== 0));
  },

  async closing_the_tab_mid_session_asks_first() {
    // M4: Ctrl+W outside fullscreen can't be stopped by preventDefault; the page asks instead.
    const p = page(); await p.connect();
    const e = { returnValue: undefined, prevented: false, preventDefault() { this.prevented = true; } };
    for (const f of p.state.winHandlers.beforeunload) f(e);
    assert.ok(e.prevented);
    const q = page(); await flush();
    const f = { prevented: false, preventDefault() { this.prevented = true; } };
    for (const h of q.state.winHandlers.beforeunload) h(f);
    assert.ok(!f.prevented, "no session: closing doesn't ask");
  },

  async fullscreen_locks_browser_keys_and_unlocks_on_exit() {
    const p = page(); await p.connect();
    p.els["rm-stage"].requestFullscreen = () => { p.doc.fullscreenElement = p.els["rm-stage"]; p.fire(p.doc, "fullscreenchange", {}); return Promise.resolve(); };
    p.fire(p.els["rm-fs"], "click", {}); await flush();
    assert.equal(p.state.locks, 1);
    assert.match(p.els["rm-hint"].textContent, /Hold Esc/);
    p.doc.fullscreenElement = null; p.fire(p.doc, "fullscreenchange", {});
    assert.equal(p.state.unlocks, 1);
    assert.doesNotMatch(p.els["rm-hint"].textContent, /Hold Esc/);
  },

  async blur_lets_go_of_everything_held() {
    const p = page(); await p.connect();
    p.down({ button: 0, buttons: 1 }); p.key("keydown", "ShiftLeft");
    p.view.blur();
    await p.clock.advance(100);
    assert.equal(p.sent().pop().t, "release");
  },
};

(async () => {
  const selected = process.argv[2] ? [process.argv[2]] : Object.keys(scenarios);
  for (const name of selected) {
    if (!scenarios[name]) throw new Error("unknown scenario " + name);
    await scenarios[name](); console.log(`PASS ${name}`);
  }
})().catch((e) => { console.error(e); process.exit(1); });
