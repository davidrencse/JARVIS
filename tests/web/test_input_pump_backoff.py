"""Input pumps stop on HTTP 4xx and back off on network errors and HTTP 5xx.

Loads inputPumpPlan from phone.js and remote.js (the same pure function) and drives
remote.js in a Node VM with a fake DOM, clock and API. No server, no real input.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PHONE = ROOT / "jarvis" / "web" / "phone" / "phone.js"
REMOTE = ROOT / "jarvis" / "web" / "remote.js"
NODE = shutil.which("node")


def _extract_function(src, name):
    needle = "function %s(" % name
    start = src.find(needle)
    if start < 0:
        raise AssertionError("%s is missing" % name)
    open_brace = src.find("{", start)
    depth = 0
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("%s is unclosed" % name)


PLAN_JS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
function extract(src, name) {
  const needle = "function " + name + "(";
  const start = src.indexOf(needle);
  if (start < 0) throw new Error(name + " missing");
  const open = src.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(name + " unclosed");
}
const phone = fs.readFileSync(process.env.PHONE_JS, "utf8");
const remote = fs.readFileSync(process.env.REMOTE_JS, "utf8");
const phonePlan = extract(phone, "inputPumpPlan");
const remotePlan = extract(remote, "inputPumpPlan");
assert.equal(phonePlan, remotePlan, "phone and remote inputPumpPlan drifted");
const inputPumpPlan = new Function(phonePlan + "\nreturn inputPumpPlan;")();

const FOUR = [400, 401, 403, 404, 409, 410, 429];
for (const status of FOUR) {
  const first = inputPumpPlan(false, { status, message: "no" }, 4, [{ t: "key", code: "KeyA", down: true }], [{ t: "move" }, { t: "up" }]);
  assert.equal(first.stop, true, status);
  assert.equal(first.delay, 0, status);
  assert.equal(first.releaseOnce, true, status);
  assert.equal(first.fails, 0, status);
  assert.deepEqual(first.queue, [{ t: "release" }], status + " kept " + JSON.stringify(first.queue));
  const second = inputPumpPlan(false, { status }, 0, [{ t: "release" }], [{ t: "key" }, { t: "release" }]);
  assert.equal(second.stop, true, status);
  assert.equal(second.releaseOnce, false, status);
  assert.equal(second.delay, 0, status);
  assert.deepEqual(second.queue, [], status + " rescheduled " + JSON.stringify(second.queue));
}

function delaysFor(err) {
  let fails = 0, queue = [{ t: "wheel", dy: 1 }];
  const got = [];
  for (let i = 0; i < 8; i++) {
    const sent = queue;
    queue = [{ t: "move" }];
    const plan = inputPumpPlan(false, err, fails, sent, queue);
    fails = plan.fails;
    queue = plan.queue;
    assert.equal(plan.stop, false);
    assert.equal(plan.releaseOnce, false);
    assert.equal(queue[0].t, "release");
    assert.equal(queue[1].t, "move");
    got.push(plan.delay);
  }
  return got;
}
const expect = [300, 600, 1200, 2400, 4800, 5000, 5000, 5000];
for (const err of [null, undefined, {}, { kind: "net" }, { status: 0 }, { status: 500 }, { status: 502 }, { status: 503 }, { status: 599 }, new Error("Failed to fetch")]) {
  assert.deepEqual(delaysFor(err), expect, JSON.stringify(err && err.status) + " " + (err && err.message));
}
// A success puts the next transient failure back at 300 ms.
let fails = 3;
const mid = inputPumpPlan(true, null, fails, [{ t: "release" }], []);
assert.equal(mid.fails, 0);
assert.equal(mid.clearNote, true);
assert.equal(mid.stop, false);
const again = inputPumpPlan(false, { status: 500 }, mid.fails, [{ t: "key" }], []);
assert.equal(again.delay, 300);
assert.equal(again.fails, 1);
// Already-queued release is not duplicated, and earlier keys stay behind it.
const kept = inputPumpPlan(false, { status: 503 }, 1, [{ t: "down" }], [{ t: "release" }, { t: "key" }]);
assert.equal(kept.delay, 600);
assert.deepEqual(kept.queue.map((e) => e.t), ["release", "key"]);
console.log("PASS plan");
"""


VM_JS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const REMOTE_JS = process.env.REMOTE_JS;
const flush = async () => { for (let i = 0; i < 40; i++) await new Promise((r) => setImmediate(r)); };

function fakeClock() {
  let now = 0, id = 0;
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
  };
}

function page(fail) {
  const clock = fakeClock();
  const els = {};
  const doc = {
    hidden: false, activeElement: null, fullscreenElement: null, handlers: {},
    getElementById: (id) => els[id] || (els[id] = element(id)),
    addEventListener(k, f) { (this.handlers[k] = this.handlers[k] || []).push(f); },
    createElement: (tag) => element(tag),
  };
  function element(id) {
    return {
      id, hidden: false, attrs: {}, handlers: {}, textContent: "", innerHTML: "", className: "", value: "", disabled: false,
      naturalWidth: 0, naturalHeight: 0, classList: { toggle() {}, add() {}, remove() {} },
      setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
      removeAttribute(k) { delete this.attrs[k]; },
      addEventListener(k, f) { (this.handlers[k] = this.handlers[k] || []).push(f); },
      querySelectorAll: () => [], append() {},
      focus() { doc.activeElement = this; },
      blur() { if (doc.activeElement === this) doc.activeElement = null; },
      setPointerCapture() {}, releasePointerCapture() {},
      getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 500 }; },
    };
  }
  function fire(target, kind, init) {
    const e = Object.assign({ prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {} }, init);
    for (const f of (target.handlers && target.handlers[kind]) || []) f(e);
    return e;
  }
  const state = {
    clock, els, inputs: [], calls: [],
    devices: [{ id: "dell", label: "Dell", ready: true, online: true }],
    session: null, frames: [],
  };
  const httpError = (status, message) => { const e = new Error(message || ("HTTP " + status)); e.status = status; return e; };
  const api = async (p, body) => {
    state.calls.push(p);
    if (p === "/api/remote") return { devices: state.devices, session: state.session };
    if (p === "/api/remote/start") {
      state.session = { id: "abc", device: "dell", state: "starting", error: null, width: 1600, height: 1000, quality: body.quality };
      return Object.assign({}, state.session);
    }
    if (p === "/api/remote/stop") {
      state.calls.push("stop-body");
      if (state.session) state.session = Object.assign({}, state.session, { state: "ended" });
      return { ok: true };
    }
    if (p === "/api/remote/input") {
      const evs = body.events.map((e) => e.t);
      state.inputs.push({ at: clock.now(), evs });
      const err = fail(state.inputs.length, evs);
      if (err) throw err;
      return { ok: true, queued: evs.length };
    }
    throw new Error("unexpected api " + p);
  };
  const fetch = (url, opts = {}) => {
    if (!String(url).startsWith("/api/remote/frame")) return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    const next = state.frames.shift();
    if (next) return Promise.resolve(next);
    return new Promise((_, reject) => opts.signal && opts.signal.addEventListener("abort", () => {
      const e = new Error("aborted"); e.name = "AbortError"; reject(e);
    }));
  };
  const frame = {
    ok: true, status: 200, blob: async () => ({}), json: async () => ({}),
    headers: { get: (h) => ({ "X-Frame-Seq": "1", "X-Frame-Width": "1600", "X-Frame-Height": "1000" })[h] || null },
  };
  const context = {
    document: doc, fetch, localStorage: { getItem: () => null, setItem() {} },
    performance: { now: () => clock.now() }, Date, setTimeout: clock.setTimeout, clearTimeout: clock.clear,
    setInterval: clock.setInterval, clearInterval: clock.clear, AbortController,
    Promise, URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    MutationObserver: class { observe() {} },
    navigator: { keyboard: { lock: async () => {}, unlock() {} } },
    addEventListener() {}, JSON, Math, Number, String, Object, Array, Set, Error, parseInt, parseFloat,
    encodeURIComponent, RegExp, isFinite,
  };
  context.window = context;
  context.Jarvis = { api };
  vm.runInNewContext(fs.readFileSync(REMOTE_JS, "utf8"), context, { filename: REMOTE_JS });
  return {
    state, els, clock, fire, httpError,
    async connect() {
      await flush();
      state.frames.push(frame);
      fire(els["rm-go"], "click", {});
      await clock.advance(50);
    },
    key(code) { fire(els["rm-view"], "keydown", { code, key: code, isComposing: false }); },
  };
}

(async () => {
  // Permanent 4xx: the key is posted, one release follows, then nothing for 10s.
  // Disconnect still posts a release (and stop) so held keys are let go.
  {
    const p = page(() => p.httpError(403, "CSRF check failed"));
    await p.connect();
    p.key("KeyA");
    await p.clock.advance(10000);
    const evs = (row) => JSON.parse(JSON.stringify(row.evs));
    assert.equal(p.state.inputs.length, 2, "4xx kept posting " + JSON.stringify(p.state.inputs));
    assert.deepEqual(evs(p.state.inputs[0]), ["key"]);
    assert.deepEqual(evs(p.state.inputs[1]), ["release"]);
    assert.match(p.els["rm-note"].textContent, /Input was not delivered: CSRF check failed/);
    assert.equal(p.els["rm-note"].hidden, false);
    const stopped = p.state.inputs.length;
    p.key("KeyB");
    await p.clock.advance(5000);
    assert.equal(p.state.inputs.length, stopped, "4xx resumed after a later key");
    p.fire(p.els["rm-go"], "click", {});
    await p.clock.advance(1000);
    assert.ok(p.state.inputs.length > stopped, "disconnect sent no release");
    assert.deepEqual(evs(p.state.inputs[stopped]), ["release"]);
    assert.ok(p.state.calls.includes("/api/remote/stop"), "disconnect sent no stop");
    console.log("PASS remote-4xx");
  }
  // 5xx and a status-less network error: 300, 600, 1200, 2400, 4800, then the 5s cap.
  for (const [name, err] of [["remote-5xx", { status: 502, message: "bad gateway" }], ["remote-net", null]]) {
    const p = page(() => err ? p.httpError(err.status, err.message) : new Error("Failed to fetch"));
    await p.connect();
    p.key("KeyA");
    await p.clock.advance(20000);
    const deltas = [];
    for (let i = 1; i < p.state.inputs.length; i++) deltas.push(p.state.inputs[i].at - p.state.inputs[i - 1].at);
    assert.deepEqual(deltas.slice(0, 7), [300, 600, 1200, 2400, 4800, 5000, 5000], name + " " + deltas.join(","));
    assert.ok(p.state.inputs.slice(1).every((row) => JSON.parse(JSON.stringify(row.evs))[0] === "release"), name);
    assert.match(p.els["rm-note"].textContent, /Input was not delivered/);
    console.log("PASS " + name);
  }
  // Two transient failures then a success. The next failure waits 300 ms, not 1200.
  {
    const script = [{ status: 500 }, { status: 500 }, null, { status: 500 }];
    const p = page(() => {
      const step = script.shift();
      return step ? p.httpError(step.status, "unavailable") : null;
    });
    await p.connect();
    p.key("KeyA");
    await p.clock.advance(900);
    assert.equal(p.state.inputs.length, 3, "warmup " + JSON.stringify(p.state.inputs));
    const warm = p.state.inputs.map((row) => row.at);
    assert.deepEqual([warm[1] - warm[0], warm[2] - warm[1]], [300, 600], JSON.stringify(p.state.inputs));
    const before = p.state.inputs.length;
    p.key("KeyB");
    await p.clock.advance(5000);
    const after = p.state.inputs.slice(before);
    assert.ok(after.length >= 2, "no retry after the reset " + JSON.stringify(p.state.inputs));
    assert.equal(after[1].at - after[0].at, 300, "backoff did not reset " + JSON.stringify(p.state.inputs));
    console.log("PASS remote-reset");
  }
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@unittest.skipUnless(NODE, "node not installed")
class InputPumpBackoffTests(unittest.TestCase):
    def test_syntax(self):
        for path in (PHONE, REMOTE):
            result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_pumps_use_the_plan_and_not_a_fixed_300ms_retry(self):
        phone = PHONE.read_text(encoding="utf-8")
        remote = REMOTE.read_text(encoding="utf-8")
        self.assertEqual(_extract_function(phone, "inputPumpPlan"), _extract_function(remote, "inputPumpPlan"))
        for src in (phone, remote):
            self.assertNotIn("setTimeout(pump, 300)", src)
            pump = _extract_function(src, "pump")
            self.assertIn("inputPumpPlan(false,", pump)
            self.assertIn("plan.releaseOnce", pump)
            self.assertIn("plan.delay", pump)
            self.assertIn("!plan.stop", pump)
        # Ending a phone screen session still posts release outside the pump, then stop.
        self.assertIn('events: [{ t: "release" }]', phone)
        self.assertIn('"/phone/api/session/stop"', phone)
        # Disconnect drains a release through the pump; page hide beacons one too.
        self.assertIn('q.push({ t: "release" })', remote)
        self.assertIn('events: [{ t: "release" }]', remote)
        self.assertIn('"/api/remote/stop"', remote)

    def test_plan_4xx_stops_and_transient_backs_off(self):
        env = os.environ.copy()
        env.update({"PHONE_JS": str(PHONE), "REMOTE_JS": str(REMOTE)})
        result = subprocess.run([NODE, "-"], input=PLAN_JS, capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS plan", result.stdout)

    def test_remote_pump_stops_backs_off_and_still_releases_on_disconnect(self):
        env = os.environ.copy()
        env["REMOTE_JS"] = str(REMOTE)
        result = subprocess.run([NODE, "-"], input=VM_JS, capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("remote-4xx", "remote-5xx", "remote-net", "remote-reset"):
            self.assertIn("PASS " + name, result.stdout)


if __name__ == "__main__":
    unittest.main()
