// Behavioral tests for the ElevenLabs live-call client. WebSocket and audio are fakes.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const tick = () => new Promise((r) => setImmediate(r));

function harness({ failMic = false, missingDevice = false, failDeviceId = null, failEnumeration = false, audioPrefs = null,
  microphoneLevels = [[0.02], [0.02]] } = {}) {
  const state = { longTimers: [], sockets: [], sources: [], tracks: [], contexts: [], calls: [], states: [], tools: [], mediaConstraints: [], errors: [], connectedMics: 0, selectedMics: [], analyserCount: 0 };
  class FakeSocket {
    constructor(url) { this.url = url; this.sent = []; this.readyState = 0; state.sockets.push(this); setImmediate(() => { this.readyState = 1; this.onopen?.(); }); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close(code) { this.readyState = 3; this.closed = code; }
    server(msg) { this.onmessage?.({ data: JSON.stringify(msg) }); }
  }
  FakeSocket.OPEN = 1;
  const context = {
    window: {}, localStorage: { getItem: () => audioPrefs ? JSON.stringify(audioPrefs) : null }, setTimeout: (fn, ms) => { if (ms >= 10000) { state.longTimers.push(fn); return fn; } return setImmediate(fn); },
    clearTimeout: (h) => { const i = state.longTimers.indexOf(h); if (i >= 0) state.longTimers.splice(i, 1); }, setImmediate, btoa: (s) => Buffer.from(s, "binary").toString("base64"),
    atob: (s) => Buffer.from(s, "base64").toString("binary"), WebSocket: FakeSocket,
    navigator: { mediaDevices: { async getUserMedia(constraints) {
      state.mediaConstraints.push(JSON.parse(JSON.stringify(constraints)));
      if (failMic) throw Object.assign(new Error("denied"), { name: "NotAllowedError" });
      if (failDeviceId && constraints.audio.deviceId?.exact === failDeviceId)
        throw Object.assign(new Error("unavailable"), { name: "NotFoundError" });
      if (missingDevice && constraints.audio.deviceId) throw Object.assign(new Error("missing"), { name: "OverconstrainedError" });
      const track = { stopped: 0, stop() { this.stopped++; } }; state.tracks.push(track);
      return { getTracks: () => [track] };
    }, async enumerateDevices() {
      if (failEnumeration) throw Object.assign(new Error("blocked"), { name: "NotAllowedError" });
      return ["mic-a", "mic-b", "default"].map((deviceId) => ({ kind: "audioinput", deviceId }));
    } } },
    AudioContext: class {
      constructor() { this.sampleRate = 48000; this.currentTime = 0; this.state = "running"; this.destination = {}; state.contexts.push(this);
        this.audioWorklet = { addModule: async () => {} }; }
      createGain() { return { gain: { value: 1, setTargetAtTime(v) { this.value = v; } }, connect() {}, disconnect() {} }; }
      createAnalyser() {
        const index = state.analyserCount++; let read = 0;
        return { fftSize: 0, getFloatTimeDomainData(data) {
          const levels = microphoneLevels[index] || [0]; data.fill(levels[read++ % levels.length]);
        }, connect() {}, disconnect() {} };
      }
      createMediaStreamSource() { state.connectedMics++; return { connections: [], connect(target) { this.connections.push(target); }, disconnect(target) { this.connections = this.connections.filter((item) => item !== target); } }; }
      createBuffer(ch, n, rate) { const data = new Float32Array(n); return { duration: n / rate, getChannelData: () => data, rate }; }
      createBufferSource() { const s = { connect() {}, start(at) { this.at = at; }, stop() { this.stopped = true; } }; state.sources.push(s); return s; }
      async close() { this.state = "closed"; }
    },
    AudioWorkletNode: class { constructor() { this.port = {}; } connect() {} disconnect() {} },
  };
  for (const file of ["voice.js", "agent.js"])
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../jarvis/web", file), "utf8"), context);
  context.Mic = context.window.Mic;
  vm.runInNewContext("Mic = window.Mic", context);
  const agent = new context.window.ElevenAgent({
    async api(p, body) {
      state.calls.push([p, body]);
      if (p === "/api/agent/session") return { signed_url: "wss://api.elevenlabs.io/v1/convai/conversation?token=t", voice_id: "uk1" };
      if (p === "/api/agent/ask") return { reply: "Your next class is at ten." };
      if (p === "/api/agent/voice") return { current: "f1", name: "Lily - Velvety Actress", speed: body.speed };
      throw new Error("unexpected " + p);
    },
    onState: (s) => state.states.push(s), onTool: (r, phase) => state.tools.push([r, phase]),
    onError: (message) => state.errors.push(message),
    onMicSelected: (label) => state.selectedMics.push(label),
  });
  return { state, agent };
}

async function connect(env) {
  const started = env.agent.start();
  await tick(); await tick();
  const ws = env.state.sockets[0];
  ws.server({ type: "conversation_initiation_metadata", conversation_initiation_metadata_event: {
    conversation_id: "c1", user_input_audio_format: "pcm_16000", agent_output_audio_format: "pcm_24000" } });
  await started;
  return ws;
}

const scenarios = {
  async handshake_that_never_answers_gives_up() {
    const env = harness();
    const started = env.agent.start().then(() => "connected", (e) => e.message);
    await tick(); await tick();
    assert.equal(env.state.longTimers.length, 1, "a handshake timeout is armed");
    env.state.longTimers[0]();  // 15 s pass with no metadata
    assert.match(await started, /didn't answer/);
    assert.equal(env.state.sockets[0].readyState, 3, "the half-open socket is closed");
    const ok = harness();  // answering in time disarms the timer
    await connect(ok);
    assert.equal(ok.state.longTimers.length, 0);
    await ok.agent.stop();
  },
  async protocol() {
    const env = harness(), { state, agent } = env;
    const ws = await connect(env);
    assert.equal(ws.url, "wss://api.elevenlabs.io/v1/convai/conversation?token=t");
    assert.deepEqual(ws.sent[0], { type: "conversation_initiation_client_data",
      conversation_config_override: { tts: { voice_id: "uk1" } } });
    assert.deepEqual([...state.states], ["connecting", "listening"]);

    // 0.1 s of 48 kHz mic audio becomes one 16 kHz, 16-bit chunk (1600 samples = 3200 bytes).
    const node = agent.node;
    for (let i = 0; i < 38; i++) node.port.onmessage({ data: new Float32Array(128).fill(0.5) });
    const chunk = ws.sent.find((m) => m.user_audio_chunk);
    assert.ok(chunk, "mic audio must be sent");
    const bytes = Buffer.from(chunk.user_audio_chunk, "base64");
    assert.equal(bytes.length, 2 * Math.floor(38 * 128 / 3));
    assert.equal(bytes.readInt16LE(0), Math.trunc(0.5 * 0x7fff));

    ws.server({ type: "ping", ping_event: { event_id: 7, ping_ms: 0 } });
    await tick();
    assert.deepEqual(ws.sent.at(-1), { type: "pong", event_id: 7 });

    // Agent audio plays back to back at the output rate; barge-in stops it.
    const pcm = Buffer.alloc(4800); // 2400 samples = 0.1 s at 24 kHz
    ws.server({ type: "audio", audio_event: { audio_base_64: pcm.toString("base64"), event_id: 1 } });
    ws.server({ type: "audio", audio_event: { audio_base_64: pcm.toString("base64"), event_id: 2 } });
    assert.equal(state.sources.length, 2);
    assert.ok(Math.abs(state.sources[1].at - state.sources[0].at - 0.1) < 1e-9);
    assert.equal(agent.state, "speaking");
    ws.server({ type: "interruption", interruption_event: { reason: "user" } });
    assert.ok(state.sources.every((s) => s.stopped));
    assert.equal(agent.state, "listening");

    // ask_jarvis goes through the local server and the answer goes back to the agent.
    ws.server({ type: "client_tool_call", client_tool_call: { tool_name: "ask_jarvis", tool_call_id: "t1",
      parameters: { request: "what's my next class" }, event_id: 3, expects_response: true } });
    await tick(); await tick();
    assert.equal(JSON.stringify(state.calls.at(-1)), JSON.stringify(["/api/agent/ask", { text: "what's my next class" }]));
    assert.deepEqual(ws.sent.at(-1), { type: "client_tool_result", tool_call_id: "t1",
      result: "Your next class is at ten.", is_error: false });
    assert.deepEqual(JSON.parse(JSON.stringify(state.tools)), [["what's my next class", "start"], ["what's my next class", "done"]]);
    ws.server({ type: "client_tool_call", client_tool_call: { tool_name: "rm_rf", tool_call_id: "t2", parameters: {} } });
    await tick();
    assert.equal(ws.sent.at(-1).is_error, true);

    await agent.stop();
    assert.equal(ws.closed, 1000);
    assert.equal(state.tracks[0].stopped, 1);
    assert.equal(state.contexts[0].state, "closed");
    assert.equal(agent.state, "idle");
    assert.equal(state.states.at(-1), "idle");
    assert.equal(state.states.filter((s) => s === "idle").length, 1);
  },

  async start_failure() {
    const env = harness({ failMic: true });
    await assert.rejects(env.agent.start(), /denied/);
    assert.equal(env.agent.live, false);
    assert.equal(env.state.sockets.length, 0);
    assert.deepEqual([...env.state.states], ["connecting", "idle"]);
    const ok = harness();
    ok.agent.h.onState = () => {};
    // a socket that closes before the handshake rejects start and releases the mic
    const started = ok.agent.start();
    await tick(); await tick();
    ok.state.sockets[0].onclose({ code: 1008, reason: "Override for voice_id is not allowed" });
    await assert.rejects(started, /Override for voice_id/);
    await tick();
    assert.equal(ok.state.tracks[0].stopped, 1);
    assert.equal(ok.agent.live, false);
  },

  async change_voice() {
    const env = harness(), { state, agent } = env;
    let voiced;
    agent.h.onVoice = (r) => (voiced = r);
    const ws = await connect(env);
    ws.server({ type: "user_transcript", user_transcription_event: { user_transcript: "use Lily, a bit faster" } });
    ws.server({ type: "client_tool_call", client_tool_call: { tool_name: "change_voice", tool_call_id: "v1",
      parameters: { voice: "Lily", speed: 1.1 } } });
    await tick(); await tick();
    assert.equal(JSON.stringify(state.calls.find((c) => c[0] === "/api/agent/voice")),
      JSON.stringify(["/api/agent/voice", { voice: "Lily", speed: 1.1 }]));
    assert.equal(voiced.current, "f1");
    assert.equal(ws.sent.find((m) => m.tool_call_id === "v1").is_error, false);
    assert.equal(ws.closed, 1000);
    await tick(); await tick();
    // a fresh conversation in the same call: mic and audio context are kept
    const fresh = state.sockets[1];
    assert.ok(fresh, "must reconnect");
    assert.equal(fresh.sent[0].conversation_config_override.agent.first_message, "How's this?");
    fresh.server({ type: "conversation_initiation_metadata", conversation_initiation_metadata_event: {
      conversation_id: "c2", user_input_audio_format: "pcm_16000", agent_output_audio_format: "pcm_16000" } });
    await tick(); await tick();
    const update = fresh.sent.find((m) => m.type === "contextual_update");
    assert.match(update.text, /David: use Lily, a bit faster/);
    assert.equal(agent.state, "listening");
    assert.equal(state.tracks[0].stopped, 0);
    assert.equal(state.contexts.length, 1);
    // the old socket closing late must not end the call
    ws.onclose?.({ code: 1000 });
    assert.equal(agent.live, true);
    // nothing to change is an error back to the agent, no reconnect
    fresh.server({ type: "client_tool_call", client_tool_call: { tool_name: "change_voice", tool_call_id: "v2", parameters: {} } });
    await tick();
    assert.equal(fresh.sent.at(-1).is_error, true);
    assert.equal(state.sockets.length, 2);
    await agent.stop();
  },

  async audio_controls() {
    const env = harness({ audioPrefs: { deviceId: "mic-2", gain: 1.4, echoCancellation: false, noiseSuppression: true, autoGainControl: false } });
    const ws = await connect(env);
    assert.deepEqual(JSON.parse(JSON.stringify(env.state.mediaConstraints[0])), { audio: {
      channelCount: 1, deviceId: { exact: "mic-2" }, echoCancellation: false, noiseSuppression: true, autoGainControl: false,
    } });
    assert.equal(env.agent.inputGain.gain.value, 1.4);
    env.agent.setAudioSettings({ gain: 1.1 });
    assert.equal(env.agent.inputGain.gain.value, 1.1);
    assert.equal(env.agent.mute.gain.value, 0);
    await env.agent.stop();
  },
  async microphone_fallback_and_gain_limits() {
    const env = harness({ missingDevice: true, audioPrefs: { deviceId: "removed-mic", gain: 99 } });
    await connect(env);
    assert.equal(env.state.mediaConstraints.length, 2);
    assert.deepEqual(JSON.parse(JSON.stringify(env.state.mediaConstraints[0].audio.deviceId)), { exact: "removed-mic" });
    assert.equal(env.state.mediaConstraints[1].audio.deviceId, undefined);
    assert.equal(env.agent.inputGain.gain.value, 1.8, "saved gain must be clamped to the UI range");
    assert.match(env.state.errors[0], /system default/);
    env.agent.setAudioSettings({ gain: -10 });
    assert.equal(env.agent.inputGain.gain.value, 0.7);
    await env.agent.stop();
  },
  async all_microphones_select_one_input_and_use_one_playback_output() {
    const env = harness({ audioPrefs: { allMicrophones: true } });
    await connect(env);
    assert.equal(env.state.mediaConstraints.length, 2, "open each physical mic and skip the default alias");
    assert.equal(env.state.connectedMics, 2);
    assert.equal(env.agent.inputGain.gain.value, 1);
    assert.equal(env.agent.mute.gain.value, 0, "mic mix stays muted at the output destination");
    await env.agent.stop();
    assert.ok(env.state.tracks.every((track) => track.stopped === 1), "stop every microphone track");
  },
  async clearest_microphone_is_selected_during_speech() {
    const env = harness({ audioPrefs: { allMicrophones: true }, microphoneLevels: [
      [0.02], [0.01, 0.05, 0.025, 0.06, 0.012, 0.055],
    ] });
    await connect(env);
    for (let i = 0; i < 192; i++) env.agent.capture(new Float32Array(128).fill(0.1));
    assert.equal(env.agent.activeInput, 1, "choose the mic with stronger speech dynamics instead of blending inputs");
    assert.deepEqual(env.state.selectedMics, ["Microphone 1", "Microphone 2"]);
    assert.equal(env.agent.inputCandidates[0].source.connections.includes(env.agent.inputGain), false);
    assert.equal(env.agent.inputCandidates[1].source.connections.includes(env.agent.inputGain), true);
    await env.agent.stop();
  },
  async all_microphone_mode_survives_unavailable_inputs() {
    const partial = harness({ audioPrefs: { allMicrophones: true }, failDeviceId: "mic-b" });
    await connect(partial);
    assert.equal(partial.state.tracks.length, 1);
    assert.equal(partial.agent.inputGain.gain.value, 1, "normalize against successfully opened inputs only");
    assert.match(partial.state.errors[0], /continuing with the others/);
    await partial.agent.stop();

    const blocked = harness({ audioPrefs: { allMicrophones: true }, failEnumeration: true });
    await connect(blocked);
    assert.equal(blocked.state.mediaConstraints.length, 1, "enumeration failure falls back to the system default");
    assert.equal(blocked.state.tracks.length, 1);
    assert.match(blocked.state.errors[0], /using the system default/);
    await blocked.agent.stop();
  },
  async corrupt_live_audio_is_silent_not_nan() {
    const env = harness(), ws = await connect(env);
    env.agent.capture(new Float32Array(4800).fill(NaN));
    const chunk = ws.sent.find((message) => message.user_audio_chunk);
    assert.ok(chunk);
    assert.ok(Buffer.from(chunk.user_audio_chunk, "base64").every((byte) => byte === 0));
    await env.agent.stop();
  },
};

const name = process.argv[2];
scenarios[name]().then(() => console.log("PASS " + name), (e) => { console.error(e); process.exit(1); });
