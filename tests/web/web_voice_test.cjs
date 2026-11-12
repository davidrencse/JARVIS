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
  const state = { fail: null, streams: [], contexts: [], nodes: [], gains: [], requests: 0, frames: [] };
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
        check("context"); this.sampleRate = 48000; this.state = "running"; this.closed = 0; this.destination = {};
        this.audioWorklet = { addModule: async () => { check("worklet"); if (state.worklet) await state.worklet.promise; } };
        state.contexts.push(this);
      }
      createMediaStreamSource() {
        check("source");
        return { connect() { check("connect"); }, disconnect() {} };
      }
      createAnalyser() { check("analyser"); return { frequencyBinCount: 8, getByteTimeDomainData() {}, disconnect() {} }; }
      createGain() {
        check("gain");
        const gain = { gain: { value: 1 }, connections: [], connect(target) { check("connect"); this.connections.push(target); }, disconnect() { this.disconnected = true; } };
        state.gains.push(gain); return gain;
      }
      async close() { this.closed++; this.state = "closed"; check("close"); }
    },
    AudioWorkletNode: class {
      constructor() { check("node"); this.port = {}; this.disconnected = 0; this.connections = []; state.nodes.push(this); }
      connect(target) { check("connect"); this.connections.push(target); }
      disconnect() { this.disconnected++; }
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../jarvis/web/voice.js"), "utf8"), context);
  const mic = new context.window.Mic({});
  return { state, mic, stream, context };
}

function pageHarness({ handsFree = false, listen = {} } = {}) {
  const env = harness(), { context } = env;
  const button = { handlers: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(k, v) { this.handlers[k] = v; }, classList: { add() {}, remove() {} } };
  const page = { handlers: {}, announcements: [], now: 0, stored: {}, calls: [], asked: [], audioBytes: 0 };
  Object.assign(context, {
    api: async (path, body) => { page.calls.push(path); page.audioBytes = body?.byteLength || 0; return listen; },
    ask: async (text) => { page.asked.push(text); }, prompt: { value: "" }, autosize() {},
    Mic: context.window.Mic, $: (selector) => selector === "#mic" ? button : null,
    Terrain: { redraw() {} }, busy: false, trace() {}, toast() {}, agent: { live: false },
    announce: (text) => page.announcements.push(text),
    plainError: (text) => text, typing: () => false, OVERLAY: false,
    addEventListener: (name, callback) => { page.handlers[name] = callback; },
    setTimeout() {}, clearTimeout() {}, Date: { now: () => page.now },
    handsFree, placeholder() {}, store: { set: (k, v) => { page.stored[k] = v; } },
  });
  context.document.activeElement = null;
  const code = fs.readFileSync(path.join(__dirname, "../../jarvis/web/app.js"), "utf8");
  const microphone = code.split("// ---------- microphone ----------")[1].split("// ---------- live:")[0];
  assert.ok(microphone, "microphone UI section must be found");
  vm.runInNewContext(microphone, context);
  page.event = (overrides = {}) => ({ preventDefault() {}, code: "Space", key: " ", ...overrides });
  return { ...env, page, button };
}

// Space starts the mic, half a second of speech after the room's quiet, Enter sends it.
async function spoke(options) {
  const env = pageHarness(options), { state, page } = env;
  page.handlers.keydown(page.event()); await new Promise(setImmediate);
  const frame = (level) => state.nodes[0].port.onmessage({ data: new Float32Array(4800).fill(level) });
  for (let i = 0; i < 4; i++) frame(0);
  for (let i = 0; i < 5; i++) frame(0.25);
  page.handlers.keydown(page.event({ code: "Enter", key: "Enter" })); await new Promise(setImmediate);
  return env;
}

function released(state) {
  for (const stream of state.streams) assert.equal(stream.track.stopped, 1);
  for (const ctx of state.contexts) assert.equal(ctx.closed, 1);
  for (const node of state.nodes) {
    assert.equal(node.port.onmessage, null);
    assert.equal(node.disconnected, 1);
  }
  for (const gain of state.gains) assert.equal(gain.disconnected, true);
}

const scenarios = {
  async failures_and_retry() {
    for (const phase of ["permission", "context", "worklet", "source", "node", "analyser", "gain", "connect"]) {
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
    assert.equal(state.gains[0].gain.value, 0, "the worklet output must be silently connected to the render graph");
    assert.equal(state.gains[0].connections[0], state.contexts[0].destination);
    assert.equal(state.nodes[0].connections[0], state.gains[0]);
    mic.node.port.onmessage({ data: new Float32Array(24000).fill(0.25) });
    const clip = await mic.stop();
    assert.equal(clip.voiced, 0.5); assert.equal(clip.audio.length, 8000);
    assert.equal(clip.audio[0], 0.25); assert.equal(clip.audio[7999], 0.25);
    assert.equal(mic.active, false); assert.equal(await mic.stop(), null); released(state);
  },
  async malformed_audio_is_sanitized() {
    const { state, mic } = harness(); await mic.start();
    mic.frame(new Float32Array(0));
    mic.frame(new Float32Array(4800).fill(NaN));
    for (let i = 0; i < 5; i++) mic.frame(new Float32Array(4800).fill(0.25));
    const clip = await mic.stop();
    assert.ok(clip.audio?.length > 0);
    assert.ok(clip.audio.every(Number.isFinite), "corrupt samples must not poison the transcription payload");
    assert.equal(clip.audio[0], 0);
    released(state);
  },
  async recording_length_is_capped() {
    const { state, mic } = harness(); await mic.start();
    let stops = 0; mic.onAutoStop = () => stops++;
    mic.frame(new Float32Array(31 * 48000).fill(0.25));
    assert.equal(mic.samples, 30 * 48000, "capture storage must stop at the 30 second limit");
    assert.equal(stops, 1, "hitting the cap must end capture immediately");
    const clip = await mic.stop();
    assert.equal(clip.audio.length, 30 * 16000);
    released(state);
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
  async always_on_sends_only_requests_that_start_with_jarvis() {
    for (const [listen, asked] of [
      [{ owner: true, addressed: false, text: "" }, []], // chatter: dropped by the server
      [{ owner: true, addressed: true, text: "open up Spotify." }, ["open up Spotify."]],
      [{ listener: true }, []], // the background listener answers it instead
    ]) {
      const { state, page } = await spoke({ handsFree: true, listen });
      assert.deepEqual(page.calls, ["/api/listen?wake=1"]); assert.deepEqual(page.asked, asked);
      released(state);
    }
  },
  async push_to_talk_needs_no_wake_word() {
    const { state, page } = await spoke({ listen: { owner: true, addressed: false, text: "open up Spotify." } });
    assert.deepEqual(page.calls, ["/api/listen"]); assert.deepEqual(page.asked, ["open up Spotify."]);
    released(state);
  },
  async noisy_room_mic_to_transcription() {
    const { state, page } = pageHarness({ listen: { owner: true, text: "Open my calendar." } });
    page.handlers.keydown(page.event()); await new Promise(setImmediate);
    const room = new Float32Array(4800), speech = new Float32Array(4800);
    for (let i = 0; i < room.length; i++) {
      const t = i / 48000;
      room[i] = 0.03 * Math.sin(2 * Math.PI * 180 * t);
      speech[i] = room[i] + 0.06 * Math.sin(2 * Math.PI * 420 * t);
    }
    const frame = (data) => state.nodes[0].port.onmessage({ data });
    for (let i = 0; i < 5; i++) frame(room); // steady room noise, about 0.02 RMS
    assert.deepEqual(page.calls, [], "steady room noise must not auto-submit a transcript");
    for (let i = 0; i < 6; i++) frame(speech); // quieter speech mixed into the same room
    await page.handlers.keydown(page.event({ code: "Enter", key: "Enter" }));
    await new Promise(setImmediate); await new Promise(setImmediate);
    assert.deepEqual(page.calls, ["/api/listen"]);
    assert.ok(page.audioBytes >= 48000 * 4 * 0.3, "captured Float32 audio must reach the transcription API");
    assert.deepEqual(page.asked, ["Open my calendar."]);
    released(state);
  },
  async noisy_room_wait_does_not_submit_ambience() {
    const { state, mic } = harness(); await mic.start({ wait: true });
    let stops = 0; mic.onAutoStop = () => stops++;
    const room = new Float32Array(4800), speech = new Float32Array(4800), silence = new Float32Array(4800);
    for (let i = 0; i < room.length; i++) {
      const t = i / 48000;
      room[i] = 0.03 * Math.sin(2 * Math.PI * 180 * t);
      speech[i] = room[i] + 0.06 * Math.sin(2 * Math.PI * 420 * t);
    }
    for (let i = 0; i < 20; i++) mic.frame(room); // 2 s of steady fan/room noise
    for (let i = 0; i < 13; i++) mic.frame(silence); // noise pause must not count as a spoken turn
    assert.equal(stops, 0);
    assert.equal(mic.voiced, 0);
    for (let i = 0; i < 6; i++) mic.frame(speech);
    for (let i = 0; i < 13; i++) mic.frame(room);
    assert.equal(stops, 1, "speech in the noisy room should end after a real pause");
    const clip = await mic.stop();
    assert.ok(clip.audio?.length > 16000, "retain the speech and pre-roll for transcription");
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
