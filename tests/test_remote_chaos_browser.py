"""Chaos tests for the console's Remote view (jarvis/web/remote.js), breaker "browser".

Two parts:
  1. Logic, in a Node VM with fake DOM, network and timers (the approach of
     tests/web_remote_test.cjs): held buttons/keys across failed requests, visibility,
     pointer capture, wheel, keyboard lock, blur, IME, Disconnect races.
  2. Rendering, in headless Chrome against a stub API server on a random localhost port
     started inside the test: no horizontal scroll, picture letterboxed right, controls
     reachable, clicks land where they were aimed. Skipped if Chrome or playwright-core is
     missing.

Nothing real is touched: no pointer, keyboard, mic, Lan Mouse, node or JARVIS service.
Tests marked expectedFailure expose a real bug; the comment names the finding id (BR-n).
"""

import glob
import json
import os
import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "jarvis" / "web"
NODE = shutil.which("node")
CHROME = "/usr/bin/google-chrome-stable"
SCRATCH = Path(os.path.expanduser("~/.cache/jarvis-breakit"))


def _playwright_core():
    hits = sorted(glob.glob(os.path.expanduser("~/.npm/_npx/*/node_modules/playwright-core")))
    return hits[-1] if hits else None


# ---------------------------------------------------------------------------------------
# Part 1: the Node VM harness
# ---------------------------------------------------------------------------------------
VM_JS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const REMOTE_JS = process.env.REMOTE_JS;

const flush = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r)); };
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }

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
      now = end; await flush();
    },
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
      naturalWidth: 0, naturalHeight: 0, rect: { left: 0, top: 0, width: 800, height: 500 }, captured: new Set(),
      classList: { toggle() {}, add() {}, remove() {} },
      setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      removeAttribute(k) { delete this.attrs[k]; },
      addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
      querySelectorAll: () => [], append() {},
      focus() { if (doc.activeElement !== this) { const o = doc.activeElement; if (o) o.blur(); doc.activeElement = this; fire(this, "focus", {}); } },
      blur() { if (doc.activeElement === this) { doc.activeElement = null; fire(this, "blur", {}); } },
      setPointerCapture(id) { this.captured.add(id); }, releasePointerCapture(id) { this.captured.delete(id); },
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

  let sidN = 0;
  const state = {
    clock, els, doc, inputs: [], calls: [], fetches: [], frames: [], session: null,
    inputGates: [], inputFail: null, inputFailAlways: null, startGate: null, locks: 0, unlocks: 0, winHandlers: {}, mo: null,
    devices: [{ id: "dell", label: "Dell", ready: true, online: true }],
    log: [],  // ordered [kind, payload] of everything the "server" received
  };
  const api = async (p, body) => {
    state.calls.push([p, body]);
    if (p === "/api/remote") return { devices: state.devices, session: state.session };
    if (p === "/api/remote/start") {
      if (state.startGate) await state.startGate.promise;
      const id = String(++sidN).repeat(32).slice(0, 32);
      state.session = { id, device: "dell", state: "starting", error: null, width: 0, height: 0, quality: body.quality };
      return Object.assign({}, state.session);
    }
    if (p === "/api/remote/stop") {
      state.log.push(["stop", body.session]);
      if (state.session) state.session = Object.assign({}, state.session, { state: "ended", error: "Disconnected." });
      return { ok: true };
    }
    if (p === "/api/remote/input") {
      const gate = state.inputGates.shift();
      if (gate) await gate.promise;
      if (state.inputFailAlways) throw new Error(state.inputFailAlways);
      if (state.inputFail) { const m = state.inputFail; state.inputFail = null; throw new Error(m); }
      const evs = JSON.parse(JSON.stringify(body.events));
      state.inputs.push(...evs.map((e) => Object.assign({ session: body.session }, e)));
      for (const e of evs) state.log.push(["input", Object.assign({ session: body.session }, e)]);
      return { ok: true, queued: evs.length };
    }
    throw new Error("unexpected api " + p);
  };
  const fetch = (url, opts = {}) => {
    state.fetches.push([String(url), opts]);
    if (!String(url).startsWith("/api/remote/frame")) {
      if (String(url) === "/api/remote/stop") state.log.push(["beacon-stop", opts.body]);
      if (String(url) === "/api/remote/input") state.log.push(["beacon-input", opts.body]);
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    }
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
    MutationObserver: class { constructor(cb) { state.mo = cb; } observe() {} },
    navigator: { keyboard: { lock: async () => { state.locks++; }, unlock: () => { state.unlocks++; } } },
    addEventListener: (k, f) => { (state.winHandlers[k] ||= []).push(f); },
    JSON, Math, Number, String, Object, Array, Set, Error, parseInt, parseFloat, encodeURIComponent, RegExp,
  };
  context.window = context;
  context.Jarvis = { api };
  vm.runInNewContext(fs.readFileSync(REMOTE_JS, "utf8"), context);
  const view = els["rm-view"];
  const ev = (o) => Object.assign({ pointerId: 1, button: -1, buttons: 0, clientX: 400, clientY: 250 }, o);
  const frameRes = (seq) => () => ({ ok: true, status: 200, blob: async () => ({}),
    headers: { get: (h) => ({ "X-Frame-Seq": String(seq), "X-Frame-Width": "1600", "X-Frame-Height": "1000" })[h] ?? null } });
  return {
    state, view, els, doc, clock, fire,
    down: (o) => fire(view, "pointerdown", ev(o)),
    move: (o) => fire(view, "pointermove", ev(o)),
    up: (o) => fire(view, "pointerup", ev(o)),
    key: (kind, code, extra) => fire(view, kind, Object.assign({ code, key: code, isComposing: false }, extra || {})),
    sent: () => state.inputs.filter((e) => e.t !== "move"),
    frame(seq = 1) { state.frames.push(frameRes(seq)); },
    gone(msg) { state.frames.push(() => ({ ok: false, status: 410, json: async () => ({ error: msg }) })); },
    hideView() { els["view-remote"].hidden = true; state.mo && state.mo(); },
    hideTab() { doc.hidden = true; fire(doc, "visibilitychange", {}); },
    showTab() { doc.hidden = false; fire(doc, "visibilitychange", {}); },
    serverEnds(msg) { state.session = Object.assign({}, state.session, { state: "ended", error: msg }); },
    frameFetchesSince(n) { return state.fetches.slice(n).filter(([u]) => u.startsWith("/api/remote/frame")).length; },
    async connect({ frame = true } = {}) {
      await flush();
      if (frame) this.frame();
      fire(els["rm-go"], "click", {});
      await clock.advance(50);
    },
  };
}
const brief = (e) => e.t === "key" ? `key:${e.code}:${e.down ? "down" : "up"}` : e.b ? `${e.t}:${e.b}` : e.t;

const scenarios = {
  // ----- BR-1: leaving the Remote view while Connect is still waiting -----
  async leaving_view_while_connect_pending_ends_the_session() {
    const p = page(); await flush();
    p.state.startGate = deferred();
    p.fire(p.els["rm-go"], "click", {}); await flush();
    p.hideView();                                    // David switches to another view
    p.frame(); p.state.startGate.resolve();
    await p.clock.advance(5000);
    // The view is hidden, so no session may run: it must have been stopped.
    assert.ok(p.state.log.some(([k]) => k === "stop" || k === "beacon-stop"), "a session started behind a hidden view was never stopped");
    assert.equal(p.els["rm-go"].textContent, "Connect");
  },

  // ----- BR-2: the tab goes to the background while Connect is still waiting -----
  async background_tab_while_connect_pending_still_times_out() {
    const p = page(); await flush();
    p.state.startGate = deferred();
    p.fire(p.els["rm-go"], "click", {}); await flush();
    p.hideTab();
    p.frame(); p.state.startGate.resolve();
    const n = p.state.fetches.length;
    await p.clock.advance(30000);
    assert.ok(p.state.log.some(([k]) => k === "stop" || k === "beacon-stop"), "a background tab's session never ended");
    assert.equal(p.frameFetchesSince(n) <= 1, true, "frames kept streaming into a background tab");
  },

  // ----- BR-3: input typed during Disconnect's drain goes out after the final release -----
  async nothing_is_sent_after_the_disconnect_release() {
    const p = page(); await p.connect();
    p.state.inputGates.push(deferred());
    const gate = p.state.inputGates[0];
    p.fire(p.els["rm-go"], "click", {});          // Disconnect: release goes out first
    p.key("keydown", "ShiftLeft");                 // still focused, still "live" for the drain
    p.down({ button: 0, buttons: 1 });
    gate.resolve();
    await p.clock.advance(2000);
    const order = p.state.log.filter(([k]) => k === "input" || k === "stop").map(([k, e]) => k === "stop" ? "stop" : brief(e));
    const rel = order.lastIndexOf("release"), stop = order.indexOf("stop");
    assert.ok(rel >= 0 && stop > rel);
    assert.deepEqual(order.slice(rel + 1, stop), [], "presses after the last release: " + order.join(","));
  },

  // ----- BR-4: keyboard badge says "captured" after the session ended -----
  async keyboard_badge_clears_when_the_session_ends() {
    const p = page(); await p.connect();
    assert.equal(p.els["rm-kb"].getAttribute("data-on"), "1");
    p.serverEnds("Bob's laptop stopped answering.");
    await p.clock.advance(3100);                   // the 3 s poll notices
    assert.equal(p.els["rm-go"].textContent, "Connect");
    assert.equal(p.els["rm-kb"].getAttribute("data-on"), "0", "badge still says the keyboard is captured: " + p.els["rm-kb"].textContent);
  },

  // ----- BR-6: a hung input request from an ended session blocks the next session -----
  async hung_input_from_an_old_session_does_not_block_the_next() {
    const p = page(); await p.connect();
    const hung = deferred(); p.state.inputGates.push(hung);
    p.key("keydown", "KeyA");                      // in flight forever (stalled link)
    p.serverEnds("Replaced by a new session.");
    await p.clock.advance(3100);                   // session A is over
    assert.equal(p.els["rm-go"].textContent, "Connect");
    await p.connect();                             // session B
    p.key("keydown", "KeyB"); p.key("keyup", "KeyB");
    await p.clock.advance(1000);
    const b = p.state.inputs.filter((e) => e.t === "key" && e.code === "KeyB");
    assert.equal(b.length, 2, "session B's keys are stuck behind session A's request");
    hung.resolve(); await p.clock.advance(100);
  },

  // ----- BR-7: pointer capture lost while a button is held -----
  async lost_pointer_capture_lets_go_of_the_button() {
    const p = page(); await p.connect();
    p.down({ button: 0, buttons: 1 });
    p.fire(p.view, "lostpointercapture", { pointerId: 1 });
    await p.clock.advance(100);
    const last = p.sent().pop();
    assert.ok(last.t === "release" || (last.t === "up" && last.b === "left"), "left stays held on the node: " + brief(last));
  },

  // ----- BR-8: the same input error in a later session is silent and never re-checks -----
  async input_errors_are_reported_in_each_session() {
    const p = page(); await p.connect();
    p.state.inputFailAlways = "HTTP 502";
    p.key("keydown", "KeyA"); await p.clock.advance(100);
    assert.match(p.els["rm-note"].textContent, /Input was not delivered/);
    p.serverEnds("Replaced by a new session."); await p.clock.advance(3100);
    await p.connect();
    const before = p.state.calls.filter(([u]) => u === "/api/remote").length;
    p.key("keydown", "KeyB"); await p.clock.advance(100);
    assert.match(p.els["rm-note"].textContent || "", /Input was not delivered/, "session B's failing input shows nothing");
    assert.ok(p.state.calls.filter(([u]) => u === "/api/remote").length > before, "no status check after B's input failed");
  },

  // ----- BR-9: the wheel is swallowed even with no session -----
  async wheel_without_a_session_is_not_swallowed() {
    const p = page(); await flush();
    const e = p.fire(p.view, "wheel", { deltaY: 100, deltaMode: 0, clientX: 400, clientY: 250 });
    assert.ok(!e.prevented, "the placeholder can't be scrolled with the wheel");
  },

  // ----- guards (expected to pass) -----
  async failed_batch_with_a_held_key_is_followed_by_a_release() {
    const p = page(); await p.connect();
    p.state.inputFail = "HTTP 502";
    p.key("keydown", "ControlLeft");               // lost
    await p.clock.advance(1000);
    p.key("keyup", "ControlLeft");                 // not held any more: nothing to send
    await p.clock.advance(500);
    assert.deepEqual(p.sent().map(brief), ["release"]);
  },

  async failure_while_dragging_never_leaves_the_button_down() {
    const p = page(); await p.connect();
    p.down({ button: 0, buttons: 1 }); await p.clock.advance(100);
    p.state.inputFail = "HTTP 503";
    p.move({ buttons: 1, clientX: 500 }); await p.clock.advance(100);   // this batch is lost
    p.move({ buttons: 1, clientX: 520 });
    p.up({ button: 0, buttons: 0, clientX: 520 });
    await p.clock.advance(1000);
    const s = p.sent().map(brief);
    assert.equal(s[0], "down:left");
    assert.ok(s.includes("release"), s.join(","));
    const lastDown = s.lastIndexOf("down:left"), lastRel = Math.max(s.lastIndexOf("release"), s.lastIndexOf("up:left"));
    assert.ok(lastRel > lastDown, "left left down: " + s.join(","));
  },

  async pointercancel_releases() {
    const p = page(); await p.connect();
    p.down({ button: 2, buttons: 2 });
    p.fire(p.view, "pointercancel", { pointerId: 1 });
    await p.clock.advance(100);
    assert.deepEqual(p.sent().map(brief), ["down:right", "release"]);
  },

  async blur_with_only_keys_held_releases() {
    const p = page(); await p.connect();
    p.key("keydown", "AltLeft"); p.key("keydown", "Tab");
    p.view.blur(); await p.clock.advance(100);
    assert.equal(p.sent().pop().t, "release");
  },

  async escape_hatch_releases_and_blurs() {
    const p = page(); await p.connect();
    p.key("keydown", "ControlLeft"); p.key("keydown", "AltLeft"); p.key("keydown", "ShiftLeft");
    const e = p.key("keydown", "Escape", { ctrlKey: true, altKey: true, shiftKey: true });
    await p.clock.advance(100);
    assert.ok(e.prevented);
    assert.equal(p.doc.activeElement, null);
    assert.equal(p.sent().pop().t, "release");
    assert.ok(!p.sent().some((x) => x.code === "Escape"));
  },

  async hidden_tab_releases_held_keys_at_once() {
    const p = page(); await p.connect();
    p.key("keydown", "MetaLeft");
    p.hideTab(); await p.clock.advance(100);
    assert.equal(p.sent().pop().t, "release");
  },

  async pagehide_sends_release_before_stop() {
    const p = page(); await p.connect();
    p.key("keydown", "ShiftLeft"); await p.clock.advance(100);
    for (const f of p.state.winHandlers.pagehide) f({});
    const kinds = p.state.log.map(([k]) => k).filter((k) => k.startsWith("beacon"));
    assert.deepEqual(kinds, ["beacon-input", "beacon-stop"]);
    assert.match(p.state.log.find(([k]) => k === "beacon-input")[1], /"release"/);
  },

  async ime_composition_keydown_sends_nothing() {
    const p = page(); await p.connect();
    p.key("keydown", "KeyN", { isComposing: true });
    p.key("keyup", "KeyN", { isComposing: true });
    await p.clock.advance(100);
    assert.deepEqual(p.sent(), []);
  },

  async contextmenu_is_suppressed_and_right_click_reaches_node() {
    const p = page(); await p.connect();
    p.down({ button: 2, buttons: 2 }); p.up({ button: 2, buttons: 0 });
    assert.ok(p.fire(p.view, "contextmenu", {}).prevented);
    await p.clock.advance(100);
    assert.deepEqual(p.sent().map(brief), ["down:right", "up:right"]);
  },

  async trackpad_small_deltas_add_up() {
    const p = page(); await p.connect();
    for (let i = 0; i < 4; i++) p.fire(p.view, "wheel", { deltaY: 30, deltaMode: 0, clientX: 400, clientY: 250 });
    await p.clock.advance(100);
    assert.deepEqual(p.state.inputs.filter((e) => e.t === "wheel").map((e) => e.dy), [1]);
  },

  async wheel_in_the_letterbox_goes_nowhere() {
    const p = page(); await p.connect();
    p.fire(p.view, "wheel", { deltaY: 300, deltaMode: 0, clientX: 400, clientY: 2 });  // 1600x1000 in 800x500: no letterbox...
    p.els["rm-img"].rect = { left: 0, top: 0, width: 800, height: 800 };                 // ...now 150 px bars top and bottom
    p.fire(p.view, "wheel", { deltaY: 300, deltaMode: 0, clientX: 400, clientY: 20 });
    await p.clock.advance(100);
    assert.deepEqual(p.state.inputs.filter((e) => e.t === "wheel").map((e) => e.dy), [3]);
  },

  async session_end_in_fullscreen_unlocks_the_keyboard() {
    const p = page(); await p.connect();
    p.els["rm-stage"].requestFullscreen = () => { p.doc.fullscreenElement = p.els["rm-stage"]; p.fire(p.doc, "fullscreenchange", {}); return Promise.resolve(); };
    p.fire(p.els["rm-fs"], "click", {}); await flush();
    assert.equal(p.state.locks, 1);
    p.serverEnds("x"); await p.clock.advance(3100);
    assert.equal(p.doc.fullscreenElement, null);
    assert.equal(p.state.unlocks, 1);
  },

  async no_input_after_a_410() {
    const p = page(); await flush();
    p.frame(); p.gone("The remote computer ended the session.");
    p.fire(p.els["rm-go"], "click", {}); await p.clock.advance(50);
    assert.equal(p.els["rm-go"].textContent, "Connect");
    p.key("keydown", "KeyA"); p.down({ button: 0, buttons: 1 });
    p.fire(p.view, "wheel", { deltaY: 300, deltaMode: 0, clientX: 400, clientY: 250 });
    await p.clock.advance(1000);
    assert.deepEqual(p.state.inputs, []);
  },
};

(async () => {
  const name = process.argv[2];
  if (!scenarios[name]) throw new Error("unknown scenario " + name);
  await scenarios[name](); console.log(`PASS ${name}`);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def setUpModule():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "remote_chaos_vm.cjs").write_text(VM_JS, encoding="utf-8")


def run_vm(test, name):
    result = subprocess.run([NODE, str(SCRATCH / "remote_chaos_vm.cjs"), name], capture_output=True, text=True,
                            encoding="utf-8", timeout=30, env=dict(os.environ, REMOTE_JS=str(WEB / "remote.js")))
    test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    test.assertIn("PASS " + name, result.stdout)


@unittest.skipUnless(NODE, "Node.js is required")
class RemoteJsChaos(unittest.TestCase):
    # BR-1: connect() attaches whatever /api/remote/start returns even if the view was left
    # while it waited; the session then streams behind a hidden view until the hub's 4 h cap.
    def test_br1_leaving_view_while_connect_pending_ends_the_session(self):
        run_vm(self, "leaving_view_while_connect_pending_ends_the_session")

    # BR-2: same race with the tab hidden: no hidden timer is armed, frames keep polling,
    # so the hub never sees the console go quiet.
    def test_br2_background_tab_while_connect_pending_still_times_out(self):
        run_vm(self, "background_tab_while_connect_pending_still_times_out")

    # BR-3: disconnect() queues the release, but canSend() stays true during drain(), so
    # keys/buttons pressed in those ~600 ms go out after the release.
    def test_br3_nothing_is_sent_after_the_disconnect_release(self):
        run_vm(self, "nothing_is_sent_after_the_disconnect_release")

    # BR-4: finish() never calls kbState(): "Keyboard: captured" stays after the session ends.
    def test_br4_keyboard_badge_clears_when_the_session_ends(self):
        run_vm(self, "keyboard_badge_clears_when_the_session_ends")

    # BR-6: one global `inflight` flag; an old session's stalled request blocks the next
    # session's input, and its finally() only re-pumps for the old id.
    def test_br6_hung_input_from_an_old_session_does_not_block_the_next(self):
        run_vm(self, "hung_input_from_an_old_session_does_not_block_the_next")

    # BR-7: no lostpointercapture handler; a capture lost mid-drag leaves the button held.
    def test_br7_lost_pointer_capture_lets_go_of_the_button(self):
        run_vm(self, "lost_pointer_capture_lets_go_of_the_button")

    # BR-8: inputErr is not reset in finish()/attach(), so the same error in the next
    # session counts as "not fresh": no banner, no refresh.
    def test_br8_input_errors_are_reported_in_each_session(self):
        run_vm(self, "input_errors_are_reported_in_each_session")

    # BR-9: the wheel handler preventDefaults before checking for a session, so the
    # placeholder (overflow: auto) can't be scrolled.
    def test_br9_wheel_without_a_session_is_not_swallowed(self):
        run_vm(self, "wheel_without_a_session_is_not_swallowed")

    def test_failed_batch_with_a_held_key_is_followed_by_a_release(self):
        run_vm(self, "failed_batch_with_a_held_key_is_followed_by_a_release")

    def test_failure_while_dragging_never_leaves_the_button_down(self):
        run_vm(self, "failure_while_dragging_never_leaves_the_button_down")

    def test_pointercancel_releases(self):
        run_vm(self, "pointercancel_releases")

    def test_blur_with_only_keys_held_releases(self):
        run_vm(self, "blur_with_only_keys_held_releases")

    def test_escape_hatch_releases_and_blurs(self):
        run_vm(self, "escape_hatch_releases_and_blurs")

    def test_hidden_tab_releases_held_keys_at_once(self):
        run_vm(self, "hidden_tab_releases_held_keys_at_once")

    def test_pagehide_sends_release_before_stop(self):
        run_vm(self, "pagehide_sends_release_before_stop")

    def test_ime_composition_keydown_sends_nothing(self):
        run_vm(self, "ime_composition_keydown_sends_nothing")

    def test_contextmenu_is_suppressed_and_right_click_reaches_node(self):
        run_vm(self, "contextmenu_is_suppressed_and_right_click_reaches_node")

    def test_trackpad_small_deltas_add_up(self):
        run_vm(self, "trackpad_small_deltas_add_up")

    def test_wheel_in_the_letterbox_goes_nowhere(self):
        run_vm(self, "wheel_in_the_letterbox_goes_nowhere")

    def test_session_end_in_fullscreen_unlocks_the_keyboard(self):
        run_vm(self, "session_end_in_fullscreen_unlocks_the_keyboard")

    def test_no_input_after_a_410(self):
        run_vm(self, "no_input_after_a_410")


class HubLivenessChaos(unittest.TestCase):
    # BR-5: the console shows "Live" while a 204's X-Node-Ago < 3 s, and node_ago counts the
    # node's *input* polls (every <= 2 s, INPUT_HOLD). A node whose frame thread is wedged
    # (capture hung, frame POST stuck) keeps polling input, so a frozen picture reads Live.
    def test_br5_frozen_frame_pipeline_does_not_read_as_alive(self):
        from jarvis import remote_view
        now = [1000.0]
        hub = remote_view.Sessions(clock=lambda: now[0])
        sid = hub.open("dell")["id"]
        hub.frame("dell", sid, b"\xff\xd8\xff" + b"0" * 10, 1600, 1000)
        for i in range(30):                      # 30 s: frames stopped, input polls go on
            now[0] += 1.0
            if i == 3:  # past STALE_MS: the console's 204 no longer reads as alive
                self.assertGreater(hub.node_ago(sid), 3.0,
                                   "no frame or keep-alive for 4 s, yet the hub says the node just checked in")
            try:
                self.assertIsNone(hub.wait_frame(sid, 1, hold=0))   # the console keeps watching
                hub.take_input("dell", sid, after=0, hold=0)
            except remote_view.Ended:
                break
        # NODE_IDLE fires on frames, not input polls: the frozen picture ends the session
        self.assertEqual(hub.get(sid)["state"], "ended")
        self.assertIn("stopped answering", hub.get(sid)["error"])


# ---------------------------------------------------------------------------------------
# Part 2: headless Chrome against a stub API server
# ---------------------------------------------------------------------------------------
def _jpeg(w, h):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), (30, 120, 200))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w - 1, h - 1], outline=(255, 255, 0), width=8)
    d.line([0, 0, w, h], fill=(255, 255, 255), width=6)
    buf = BytesIO(); im.save(buf, "JPEG", quality=70)
    return buf.getvalue()


class Stub:
    W, H = 1600, 1000

    def __init__(self):
        self.lock = threading.Lock()
        self.session = None
        self.inputs = []
        self.jpeg = _jpeg(self.W, self.H)
        self.n = 0


def _handler(stub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/remote":
                return self._json({"devices": [{"id": "dell", "label": "Dell", "ready": True, "online": True}],
                                   "session": stub.session, "why": None})
            if u.path == "/api/remote/frame":
                q = parse_qs(u.query)
                after = int((q.get("after") or ["0"])[0])
                s = stub.session
                if not s or s["state"] == "ended":
                    return self._json({"error": "The session ended."}, 410)
                if after >= 1:
                    threading.Event().wait(0.5)
                    self.send_response(204); self.send_header("X-Node-Ago", "0.2"); self.end_headers()
                    return
                s["state"] = "live"
                self.send_response(200)
                for k, v in (("Content-Type", "image/jpeg"), ("X-Frame-Seq", "1"), ("X-Frame-Width", str(stub.W)),
                             ("X-Frame-Height", str(stub.H)), ("Content-Length", str(len(stub.jpeg)))):
                    self.send_header(k, v)
                self.end_headers(); self.wfile.write(stub.jpeg)
                return
            if u.path == "/api/voice/setup":  # enrolled: the first-run voice dialog stays shut
                return self._json({"enrolled": True, "running": False, "listening": False, "mic": {}})
            if u.path.startswith("/api/"):
                return self._json({})
            rel = u.path.lstrip("/") or "index.html"
            f = (WEB / rel).resolve()
            if WEB.resolve() not in f.parents or not f.is_file():
                self.send_response(404); self.end_headers(); return
            data = f.read_bytes()
            ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                     ".woff2": "font/woff2"}.get(f.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if u.path == "/api/remote/start":
                stub.n += 1
                stub.session = {"id": f"{stub.n:032x}", "device": "dell", "state": "starting", "error": None,
                                "width": stub.W, "height": stub.H, "quality": body.get("quality")}
                return self._json(stub.session)
            if u.path == "/api/remote/input":
                with stub.lock:
                    stub.inputs.extend(body.get("events") or [])
                return self._json({"ok": True, "queued": len(body.get("events") or [])})
            if u.path == "/api/remote/stop":
                if stub.session:
                    stub.session = dict(stub.session, state="ended", error="Disconnected.")
                return self._json({"ok": True})
            return self._json({})
    return H


CHROME_JS = r"""
const { chromium } = require(process.env.PW_CORE);
const base = process.env.BASE, sizes = JSON.parse(process.env.SIZES);
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true,
    args: ["--use-fake-ui-for-media-stream=0", "--deny-permission-prompts", "--no-sandbox"] });
  const out = [];
  try {
    for (const [w, h] of sizes) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h } });
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(String(e)));
      await page.goto(base + "/#remote", { waitUntil: "load" });
      // Headless Chrome denies the mic, and app.js toasts that over the page: hide the toast only.
      await page.addStyleTag({ content: "#toast { display: none !important; }" });
      await page.waitForSelector("#view-remote:not([hidden]) #rm-go:not([disabled])", { timeout: 10000 });
      const before = await page.evaluate(() => {
        const ph = document.getElementById("rm-ph");
        return { sw: document.documentElement.scrollWidth, iw: innerWidth,
                 phOverflow: ph.scrollHeight > ph.clientHeight + 1 };
      });
      await page.click("#rm-go");
      await page.waitForFunction(() => { const i = document.getElementById("rm-img"); return !i.hidden && i.naturalWidth > 0 && i.complete; }, null, { timeout: 10000 });
      await page.waitForTimeout(150);
      const m = await page.evaluate(() => {
        const r = (id) => { const b = document.getElementById(id).getBoundingClientRect(); return { l: b.left, t: b.top, w: b.width, h: b.height, r: b.right, b: b.bottom }; };
        const img = document.getElementById("rm-img");
        const reach = {};
        for (const id of ["rm-go", "rm-fs", "rm-quality", "rm-st", "rm-kb"]) {
          const e = document.getElementById(id);
          e.scrollIntoView({ block: "nearest", inline: "nearest" });
          const b = e.getBoundingClientRect();
          const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
          reach[id] = { inView: b.left >= 0 && b.top >= 0 && b.right <= innerWidth + 0.5 && b.bottom <= innerHeight + 0.5 && b.width > 0,
                        hit: !!hit && (hit === e || e.contains(hit) || (hit.closest && hit.closest("label") && hit.closest("label").contains(e))) };
        }
        document.getElementById("rm-stage").scrollIntoView({ block: "nearest" });
        // Is every part of the drawn picture actually on top (not under the assistant dock)?
        const ib = img.getBoundingClientRect(), sc = Math.min(ib.width / img.naturalWidth, ib.height / img.naturalHeight);
        const cw = img.naturalWidth * sc, ch = img.naturalHeight * sc, cl = ib.left + (ib.width - cw) / 2, ct = ib.top + (ib.height - ch) / 2;
        const covered = [];
        for (const [fx, fy] of [[0.5, 0.5], [0.5, 0.05], [0.05, 0.5], [0.95, 0.5], [0.5, 0.95], [0.05, 0.95], [0.95, 0.95]]) {
          const x = cl + cw * fx, y = ct + ch * fy;
          const hit = document.elementFromPoint(x, y);
          if (y > innerHeight || x > innerWidth || !hit || !(hit === img || hit.id === "rm-view")) {
            const c = hit && hit.closest("[id]");
            covered.push(`${fx},${fy} -> ${hit ? hit.tagName.toLowerCase() + (c ? "#" + c.id : "") + "." + String(hit.className).split(" ")[0] : "offscreen"}`);
          }
        }
        return { covered, sw: document.documentElement.scrollWidth, bw: document.body.scrollWidth, iw: innerWidth, ih: innerHeight,
                 img: r("rm-img"), stage: r("rm-stage"), nw: img.naturalWidth, nh: img.naturalHeight,
                 fit: getComputedStyle(img).objectFit, reach, st: document.getElementById("rm-st").textContent };
      });
      // aim at the centre and at a quarter of the drawn picture, and into the letterbox
      const s = Math.min(m.img.w / m.nw, m.img.h / m.nh), cw = m.nw * s, ch = m.nh * s;
      const cl = m.img.l + (m.img.w - cw) / 2, ct = m.img.t + (m.img.h - ch) / 2;
      const barX = (m.img.w - cw) / 2, barY = (m.img.h - ch) / 2;
      let letterbox = null;
      if (barX > 6) letterbox = [m.img.l + 2, m.img.t + m.img.h / 2];
      else if (barY > 6) letterbox = [m.img.l + m.img.w / 2, m.img.t + 2];
      const start = (await (await fetch(base + "/__inputs")).json()).length;
      await page.mouse.click(cl + cw * 0.25, ct + ch * 0.75);
      await page.waitForTimeout(150);
      const mark = await (await fetch(base + "/__inputs")).json();
      if (letterbox) await page.mouse.click(letterbox[0], letterbox[1]);
      await page.waitForTimeout(200);
      const after = await (await fetch(base + "/__inputs")).json();
      await page.click("#rm-go");    // Disconnect
      await page.waitForTimeout(300);
      out.push({ size: [w, h], start, before, m, content: { cl, ct, cw, ch, barX, barY }, mark, after, letterbox, errors: errors.filter((e) => /remote/i.test(e)) });
      await ctx.close();
    }
  } finally { await browser.close(); }
  console.log("RESULT " + JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@unittest.skipUnless(NODE and os.path.exists(CHROME) and _playwright_core(), "headless Chrome + playwright-core needed")
class RemoteViewRendered(unittest.TestCase):
    SIZES = [[1440, 900], [1180, 800], [720, 450], [320, 640]]

    @classmethod
    def setUpClass(cls):
        cls.stub = Stub()
        handler = _handler(cls.stub)
        orig_get = handler.do_GET

        def do_get(self):
            if self.path == "/__inputs":
                with cls.stub.lock:
                    return self._json(list(cls.stub.inputs))
            return orig_get(self)
        handler.do_GET = do_get
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        SCRATCH.mkdir(parents=True, exist_ok=True)
        script = SCRATCH / "remote_chaos_chrome.cjs"
        script.write_text(CHROME_JS, encoding="utf-8")
        env = dict(os.environ, PW_CORE=_playwright_core(), CHROME=CHROME, SIZES=json.dumps(cls.SIZES),
                   BASE=f"http://127.0.0.1:{cls.server.server_address[1]}")
        r = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=180,
                           env=env, cwd=str(SCRATCH))
        cls.raw = r.stdout + r.stderr
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
        cls.results = {tuple(x["size"]): x for x in json.loads(line[7:])} if line else None

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def each(self):
        self.assertIsNotNone(self.results, self.raw[-3000:])
        for size in map(tuple, self.SIZES):
            with self.subTest(size=size):
                yield size, self.results[size]

    def test_no_horizontal_scroll(self):
        for size, x in self.each():
            self.assertLessEqual(x["before"]["sw"], x["before"]["iw"], "before connect")
            self.assertLessEqual(x["m"]["sw"], x["m"]["iw"], "while live")

    def test_picture_is_letterboxed_inside_the_stage(self):
        for size, x in self.each():
            m, c = x["m"], x["content"]
            self.assertEqual(m["fit"], "contain")
            self.assertEqual((m["nw"], m["nh"]), (Stub.W, Stub.H))
            self.assertAlmostEqual(c["cw"] / c["ch"], Stub.W / Stub.H, places=2)
            self.assertTrue(c["barX"] < 1 or c["barY"] < 1, "bars on both axes")
            self.assertGreaterEqual(c["cl"] + 0.5, m["stage"]["l"]); self.assertLessEqual(c["cl"] + c["cw"], m["stage"]["r"] + 0.5)
            self.assertGreaterEqual(c["ct"] + 0.5, m["stage"]["t"]); self.assertLessEqual(c["ct"] + c["ch"], m["stage"]["b"] + 0.5)
            self.assertGreaterEqual(m["stage"]["h"], 120, "the picture has room to be seen")

    def test_controls_are_reachable(self):
        for size, x in self.each():
            for cid, r in x["m"]["reach"].items():
                self.assertTrue(r["inView"] and r["hit"], f"{cid} at {size}: {r}")

    def test_click_lands_where_aimed_and_letterbox_clicks_go_nowhere(self):
        for size, x in self.each():
            downs = [e for e in x["mark"][x["start"]:] if e.get("t") == "down"]
            self.assertTrue(downs, x["mark"])
            d = downs[-1]
            self.assertAlmostEqual(d["x"], 0.25, delta=0.01)
            self.assertAlmostEqual(d["y"], 0.75, delta=0.01)
            if x["letterbox"]:
                new = x["after"][len(x["mark"]):]
                self.assertFalse([e for e in new if e.get("t") in ("down", "up")], new)

    def check_uncovered(self, size):
        self.assertIsNotNone(self.results, self.raw[-3000:])
        self.assertEqual(self.results[size]["m"]["covered"], [], f"parts of the picture are covered at {size}")

    def test_whole_picture_is_on_top_and_clickable(self):
        """Every part of the drawn picture must receive the pointer (nothing overlays it)."""
        for size in [(1440, 900), (1180, 800), (320, 640)]:
            with self.subTest(size=size):
                self.check_uncovered(size)

    # BR-10: at 720x450 (= 1440x900 at 200% zoom) the sticky top bar and the fixed assistant
    # dock leave less height than the stage's 180 px minimum: with the stage scrolled into
    # view, the top of the picture sits under header.topbar (and scrolled the other way the
    # bottom sits under the dock), so part of the remote screen can never be seen or clicked.
    def test_br10_whole_picture_is_on_top_at_200_percent_zoom(self):
        self.check_uncovered((720, 450))

    def test_no_remote_script_errors(self):
        for size, x in self.each():
            self.assertEqual(x["errors"], [])


if __name__ == "__main__":
    unittest.main()
