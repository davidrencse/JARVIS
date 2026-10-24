// Behavioral tests for the shipped microphone controller. All browser APIs are fakes.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function harness() {
  const state = { fail: null, streams: [], contexts: [], nodes: [], requests: 0, frames: [] };
  const check = (phase) => { if (state.fail === phase) throw new Error(phase); };
  const stream = () => {
    const track = { stopped: 0, stop() { this.stopped++; } };
    const value = { track, getTracks: () => [track] };
    state.streams.push(value);
    return value;
  };
  const context = {
    window: {}, performance: { now: () => 0 }, devicePixelRatio: 1,
    document: { documentElement: {} },
    getComputedStyle: () => ({ getPropertyValue: () => "#000" }),
    requestAnimationFrame: (callback) => state.frames.push(callback),
    navigator: { mediaDevices: { async getUserMedia() {
      state.requests++; check("permission");
      if (state.permission) return state.permission.promise;
      return stream();
    } } },
    AudioContext: class {
      constructor() {
        check("context"); this.sampleRate = 48000; this.state = "running"; this.closed = 0;
        this.audioWorklet = { addModule: async () => { check("worklet"); if (state.worklet) await state.worklet.promise; } };
        state.contexts.push(this);
      }
      createMediaStreamSource() {
        check("source");
        return { connect() { check("connect"); }, disconnect() {} };
      }
      createAnalyser() { check("analyser"); return { frequencyBinCount: 8, getByteTimeDomainData() {} }; }
      async close() { this.closed++; this.state = "closed"; check("close"); }
    },
    AudioWorkletNode: class {
      constructor() { check("node"); this.port = {}; this.disconnected = 0; state.nodes.push(this); }
      disconnect() { this.disconnected++; }
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../jarvis/web/voice.js"), "utf8"), context);
  const mic = new context.window.Mic({});
  return { state, mic, stream, context };
}

function pageHarness({ handsFree = false } = {}) {
  const env = harness(), { context } = env;
  const button = { handlers: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(k, v) { this.handlers[k] = v; } };
  const page = { handlers: {}, announcements: [], now: 0, stored: {} };
  Object.assign(context, {
    Mic: context.window.Mic, $: (selector) => selector === "#mic" ? button : null,
    Terrain: { redraw() {} }, busy: false, trace() {}, toast() {}, agent: { live: false },
    announce: (text) => page.announcements.push(text),
    plainError: (text) => text, typing: () => false, OVERLAY: false,
    addEventListener: (name, callback) => { page.handlers[name] = callback; },
    setTimeout() {}, Date: { now: () => page.now },
    handsFree, placeholder() {}, store: { set: (k, v) => { page.stored[k] = v; } },
  });
  context.document.activeElement = null;
  const code = fs.readFileSync(path.join(__dirname, "../jarvis/web/app.js"), "utf8");
  const microphone = code.split("// ---------- microphone ----------")[1].split("// ---------- live:")[0];
  assert.ok(microphone, "microphone UI section must be found");
  vm.runInNewContext(microphone, context);
  page.event = (overrides = {}) => ({ preventDefault() {}, code: "Space", key: " ", ...overrides });
  return { ...env, page, button };
}

function released(state) {
  for (const stream of state.streams) assert.equal(stream.track.stopped, 1);
  for (const ctx of state.contexts) assert.equal(ctx.closed, 1);
  for (const node of state.nodes) {
    assert.equal(node.port.onmessage, null);
    assert.equal(node.disconnected, 1);
  }
}

const scenarios = {
  async failures_and_retry() {
    for (const phase of ["permission", "context", "worklet", "source", "node", "analyser", "connect"]) {
      const { state, mic } = harness(); state.fail = phase;
      await assert.rejects(mic.start(), { message: phase });
      assert.equal(mic.active, false); assert.equal(await mic.stop(), null); released(state);
      state.fail = null; await mic.start(); assert.equal(mic.active, true);
      await mic.stop(); assert.equal(mic.active, false); released(state);
    }
  },
  async concurrent_start() {
    const { state, mic, stream } = harness(); state.permission = deferred();
    const first = mic.start(), second = mic.start();
    assert.equal(state.requests, 1);
    state.permission.resolve(stream()); await Promise.all([first, second]);
    assert.equal(state.contexts.length, 1); assert.equal(mic.active, true);
    await mic.stop(); await mic.stop(); released(state);
  },
  async cancel_permission() {
    const { state, mic, stream } = harness(); state.permission = deferred();
    const pending = mic.start(); assert.equal(await mic.stop(), null);
    state.permission.resolve(stream()); await pending;
    assert.equal(mic.active, false); assert.equal(state.contexts.length, 0); released(state);
  },
  async cancel_worklet() {
    const { state, mic } = harness(); state.worklet = deferred();
    const pending = mic.start(); await new Promise(setImmediate);
    assert.equal(state.contexts.length, 1); assert.equal(await mic.stop(), null);
    released(state); state.worklet.resolve(); await pending;
    assert.equal(mic.active, false); released(state);
  },
  async stale_start_cannot_affect_retry() {
    for (const reject of [false, true]) {
      const { state, mic, stream } = harness(); const old = state.permission = deferred();
      const pending = mic.start();
      const failure = reject ? assert.rejects(pending, { message: "cancelled old request" }) : pending;
      await mic.stop(); state.permission = null; await mic.start();
      if (reject) old.reject(new Error("cancelled old request")); else old.resolve(stream());
      await failure; assert.equal(mic.active, true);
      assert.equal(state.streams[0].track.stopped, 0);
      await mic.stop(); released(state);
    }
  },
  async audio_and_cleanup() {
    const { state, mic } = harness(); await mic.start();
    mic.node.port.onmessage({ data: new Float32Array(24000).fill(0.25) });
    const clip = await mic.stop();
    assert.equal(clip.voiced, 0.5); assert.equal(clip.audio.length, 8000);
    assert.equal(clip.audio[0], 0.25); assert.equal(clip.audio[7999], 0.25);
    assert.equal(mic.active, false); assert.equal(await mic.stop(), null); released(state);
  },
  async no_speech_and_close_error() {
    const { state, mic } = harness(); await mic.start(); state.fail = "close";
    mic.frame(new Float32Array(24000));
    const clip = await mic.stop(); assert.equal(clip.audio, null); assert.equal(clip.voiced, 0);
    assert.equal(mic.active, false); released(state);
  },
  async callback_error_still_releases() {
    const { state, mic } = harness(); await mic.start();
    mic.onLevel = () => { throw new Error("UI callback failed"); };
    await assert.rejects(mic.stop(), { message: "UI callback failed" });
    assert.equal(mic.active, false); assert.equal(await mic.stop(), null); released(state);
  },
  async old_animation_loop_stops() {
    const { state, mic } = harness(); let painted = 0;
    const drawing = { clearRect() { painted++; }, setTransform() {}, beginPath() {},
      lineTo() {}, moveTo() {}, stroke() {} };
    mic.wave = { clientWidth: 100, clientHeight: 20, width: 0, getContext: () => drawing };
    await mic.start(); const oldFrame = state.frames.shift();
    await mic.stop(); await mic.start();
    const before = painted, queued = state.frames.length;
    oldFrame(); assert.equal(painted, before); assert.equal(state.frames.length, queued);
    state.frames.shift()(); assert.equal(painted, before + 1);
    await mic.stop(); released(state);
  },
  async ui_cancels_startup_and_preserves_quick_tap() {
    for (const cancellation of ["escape", "long-space"]) {
      const { state, stream, page, button } = pageHarness(); state.permission = deferred();
      const pending = page.handlers.keydown(page.event());
      assert.equal(state.requests, 1);
      if (cancellation === "escape") await page.handlers.keydown(page.event({ key: "Escape" }));
      if (cancellation === "long-space") { page.now = 500; page.handlers.keyup(page.event()); }
      state.permission.resolve(stream()); await pending; await new Promise(setImmediate);
      assert.deepEqual(page.announcements, []); assert.equal(button.attrs["aria-pressed"], "false");
      released(state);
    }
    const { state, stream, page, button } = pageHarness(); state.permission = deferred();
    const pending = page.handlers.keydown(page.event()); page.now = 100; page.handlers.keyup(page.event());
    state.permission.resolve(stream()); await pending; await new Promise(setImmediate);
    assert.deepEqual(page.announcements, ["Listening."]);
    assert.equal(button.attrs["aria-pressed"], "true");
    await page.handlers.keydown(page.event({ key: "Escape" })); released(state);
  },
  async mic_button_toggles_always_listening() {
    const { state, stream, page, button } = pageHarness(); state.permission = deferred();
    const pending = button.handlers.click();
    assert.equal(state.requests, 1); assert.equal(page.stored["jarvis.handsFree"], true);
    assert.equal(button.attrs["aria-pressed"], "true");
    state.permission.resolve(stream()); await pending; await new Promise(setImmediate);
    assert.equal(state.contexts.length, 1); assert.equal(state.streams[0].track.stopped, 0);
    button.handlers.click(); await new Promise(setImmediate);
    assert.equal(page.stored["jarvis.handsFree"], false); assert.equal(button.attrs["aria-pressed"], "false");
    released(state);
  },
  async enter_sends_what_it_heard() {
    const { state, page } = pageHarness({ handsFree: true });
    page.handlers.keydown(page.event()); await new Promise(setImmediate);
    assert.equal(state.streams[0].track.stopped, 0);
    page.handlers.keydown(page.event({ code: "Enter", key: "Enter" })); await new Promise(setImmediate);
    released(state);
  },
  async waiting_keeps_only_a_preroll() {
    const { state, mic } = harness(); await mic.start({ wait: true });
    let stops = 0; mic.onAutoStop = () => stops++;
    for (let i = 0; i < 40; i++) mic.frame(new Float32Array(4800)); // 4 s of silence
    assert.ok(mic.samples <= 0.4 * 48000 + 4800); assert.equal(stops, 0);
    for (let i = 0; i < 10; i++) mic.frame(new Float32Array(4800).fill(0.25)); // 1 s of speech
    for (let i = 0; i < 13; i++) mic.frame(new Float32Array(4800)); // then a pause
    assert.equal(stops, 1);
    const clip = await mic.stop(); assert.ok(clip.audio.length > 16000); released(state);
  },
};

(async () => {
  const selected = process.argv[2] ? [process.argv[2]] : Object.keys(scenarios);
  for (const name of selected) { await scenarios[name](); console.log(`PASS ${name}`); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
