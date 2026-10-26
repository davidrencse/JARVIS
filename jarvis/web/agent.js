// Live voice call with the ElevenLabs agent. The page connects straight to ElevenLabs
// over a signed WebSocket (the key stays on the server): mic audio goes up as 16-bit
// PCM, the agent's voice comes back as PCM and is queued gaplessly. Barge-in clears the
// queue. When the agent calls its ask_jarvis tool, the request runs through the local
// server like any typed ask and the reply goes back to the agent to speak.
// ElevenLabs fixes the voice when a conversation starts, so change_voice saves the new
// voice and reconnects in place (mic stays open), handing over the recent transcript.
(function () {
  const CHUNK_SECONDS = 0.1;
  const KEEP_LINES = 8;

  function rateOf(format, fallback) {
    const m = /^pcm_(\d+)$/.exec(format || "");
    return m ? Number(m[1]) : fallback;
  }
  function toBase64(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function pcm16(float32) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const sample = Number.isFinite(float32[i]) ? float32[i] : 0;
      const v = Math.max(-1, Math.min(1, sample));
      out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    return new Uint8Array(out.buffer);
  }

  function readAudioSettings() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem("jarvis.agentAudio") || "{}"); } catch (_) { /* defaults */ }
    const gain = Number(saved?.gain);
    return {
      deviceId: typeof saved?.deviceId === "string" ? saved.deviceId.slice(0, 256) : "",
      gain: Number.isFinite(gain) ? Math.max(0.7, Math.min(1.8, gain)) : 1,
      echoCancellation: typeof saved?.echoCancellation === "boolean" ? saved.echoCancellation : true,
      noiseSuppression: typeof saved?.noiseSuppression === "boolean" ? saved.noiseSuppression : true,
      autoGainControl: typeof saved?.autoGainControl === "boolean" ? saved.autoGainControl : true,
      allMicrophones: typeof saved?.allMicrophones === "boolean" ? saved.allMicrophones : !saved?.deviceId,
    };
  }

  class ElevenAgent {
    // hooks: api(path, body), onState(state), onLevel(0..1), onUser(text),
    //        onAgent(text), onTool(request, phase), onVoice(result), onError(message)
    constructor(hooks) {
      this.h = hooks; this.state = "idle"; this.sources = new Set(); this.lines = [];
    }
    get live() { return this.state !== "idle"; }
    set(state) { this.state = state; this.h.onState?.(state); }

    async start() {
      if (this.live) return;
      this.set("connecting");
      try {
        const session = await this.h.api("/api/agent/session", {});
        this.audioSettings = readAudioSettings();
        const audio = { channelCount: 1, echoCancellation: this.audioSettings.echoCancellation,
          noiseSuppression: this.audioSettings.noiseSuppression, autoGainControl: this.audioSettings.autoGainControl };
        if (this.audioSettings.deviceId) audio.deviceId = { exact: this.audioSettings.deviceId };
        this.streams = [];
        this.inputLabels = [];
        if (this.audioSettings.allMicrophones) {
          // The `default` and `communications` entries are aliases, not physical mics.
          let devices = [];
          try { devices = await navigator.mediaDevices.enumerateDevices(); }
          catch (_) { this.h.onError?.("Couldn't list microphones; using the system default for this call."); }
          const ids = [...new Set(devices.filter((d) => d.kind === "audioinput" && d.deviceId &&
            !["default", "communications"].includes(d.deviceId)).map((d) => d.deviceId))];
          for (const deviceId of ids) {
            try {
              this.streams.push(await navigator.mediaDevices.getUserMedia({ audio: { ...audio, deviceId: { exact: deviceId } } }));
              this.inputLabels.push(devices.find((d) => d.deviceId === deviceId)?.label || `Microphone ${this.streams.length}`);
            }
            catch (error) { this.h.onError?.(`Couldn't open one microphone (${error.name || "device error"}); continuing with the others.`); }
          }
        }
        if (!this.streams.length) {
          try {
            this.streams.push(await navigator.mediaDevices.getUserMedia({ audio }));
            this.inputLabels.push("System default microphone");
          }
          catch (error) {
            if (!audio.deviceId || !["NotFoundError", "OverconstrainedError", "ConstraintNotSatisfiedError"].includes(error.name)) throw error;
            delete audio.deviceId;
            this.h.onError?.("Saved microphone is unavailable; using the system default for this call.");
            this.streams.push(await navigator.mediaDevices.getUserMedia({ audio }));
            this.inputLabels.push("System default microphone");
          }
        }
        this.stream = this.streams[0];
        this.ctx = new AudioContext();
        if (this.ctx.state === "suspended") await this.ctx.resume();
        await this.ctx.audioWorklet.addModule("recorder-worklet.js");
        this.inRate = 16000; this.outRate = 16000; this.playAt = 0;
        this.pending = []; this.pendingLen = 0; this.lines = [];
        await this.open(session);
        this.node = new AudioWorkletNode(this.ctx, "jarvis-recorder");
        this.node.port.onmessage = (ev) => this.capture(ev.data);
        this.inputGain = this.ctx.createGain(); this.inputGain.gain.value = this.audioSettings.gain;
        this.mute = this.ctx.createGain(); this.mute.gain.value = 0;
        // Keep the worklet in a live render graph without routing microphone audio to speakers.
        this.inputCandidates = this.streams.map((stream, index) => {
          const source = this.ctx.createMediaStreamSource(stream), analyser = this.ctx.createAnalyser();
          analyser.fftSize = 1024;
          const sink = this.ctx.createGain(); sink.gain.value = 0;
          source.connect(analyser); analyser.connect(sink); sink.connect(this.ctx.destination);
          return { source, analyser, sink, samples: [], data: new Float32Array(analyser.fftSize),
            label: this.inputLabels[index], connected: false };
        });
        this.activeInput = 0; this.inputEvalTick = 0; this.inputSwitchWait = 0; this.connectInput(this.activeInput);
        this.inputGain.connect(this.node); this.node.connect(this.mute); this.mute.connect(this.ctx.destination);
        this.set("listening");
      } catch (error) {
        await this.stop();
        throw error;
      }
    }

    open(session) {
      return new Promise((resolve, reject) => {
        const ws = new WebSocket(session.signed_url);
        this.ws = ws;
        let ready = false;
        ws.onopen = () => {
          const init = { type: "conversation_initiation_client_data" }, tts = {};
          if (session.voice_id) tts.voice_id = session.voice_id;
          for (const k of ["speed", "stability"]) if (typeof session[k] === "number") tts[k] = session[k];
          const override = {};
          if (Object.keys(tts).length) override.tts = tts;
          if (session.first_message) override.agent = { first_message: session.first_message };
          if (Object.keys(override).length) init.conversation_config_override = override;
          ws.send(JSON.stringify(init));
        };
        ws.onmessage = (ev) => {
          let msg; try { msg = JSON.parse(ev.data); } catch (_) { return; }
          if (msg.type === "conversation_initiation_metadata") {
            const meta = msg.conversation_initiation_metadata_event || {};
            this.inRate = rateOf(meta.user_input_audio_format, 16000);
            this.outRate = rateOf(meta.agent_output_audio_format, 16000);
            this.conversation = meta.conversation_id;
            if (session.context) this.send({ type: "contextual_update", text: session.context });
            if (!ready) { ready = true; resolve(); }
            return;
          }
          this.receive(msg);
        };
        ws.onerror = () => { if (!ready) reject(new Error("Couldn't reach the ElevenLabs agent.")); };
        ws.onclose = (ev) => {
          if (!ready) return reject(new Error(ev.reason || "The ElevenLabs agent closed the call."));
          if (this.ws === ws && this.live) {
            if (ev.code !== 1000 && ev.reason) this.h.onError?.(ev.reason);
            this.stop();
          }
        };
      });
    }

    receive(msg) {
      switch (msg.type) {
        case "ping":
          setTimeout(() => this.send({ type: "pong", event_id: msg.ping_event.event_id }), msg.ping_event.ping_ms || 0);
          break;
        case "audio":
          this.play(msg.audio_event.audio_base_64);
          break;
        case "interruption":
          this.flush();
          break;
        case "user_transcript":
          this.remember("David", msg.user_transcription_event.user_transcript);
          this.h.onUser?.(msg.user_transcription_event.user_transcript);
          break;
        case "agent_response":
          this.remember("JARVIS", msg.agent_response_event.agent_response);
          this.h.onAgent?.(msg.agent_response_event.agent_response);
          break;
        case "agent_chat_response_part":
          this.h.onAgentPart?.(msg.text_response_part || {});
          break;
        case "client_tool_call":
          this.tool(msg.client_tool_call);
          break;
      }
    }

    remember(who, text) {
      if (!text) return;
      this.lines.push(`${who}: ${text}`);
      if (this.lines.length > KEEP_LINES) this.lines.shift();
    }

    async tool(call) {
      const reply = (result, isError) => this.send({ type: "client_tool_result", tool_call_id: call.tool_call_id, result, is_error: !!isError });
      if (call.tool_name === "change_voice") return this.changeVoice(call.parameters || {}, reply);
      if (call.tool_name !== "ask_jarvis") return reply(`Unknown tool ${call.tool_name}.`, true);
      const request = String(call.parameters?.request || "").trim();
      if (!request) return reply("No request was given.", true);
      this.h.onTool?.(request, "start");
      try {
        const r = await this.h.api("/api/agent/ask", { text: request });
        this.h.onTool?.(request, r.error ? "fail" : "done");
        reply(r.reply || "Done.", r.error);
      } catch (e) {
        this.h.onTool?.(request, "fail");
        reply(`JARVIS couldn't do that: ${e.message}`, true);
      }
    }

    async changeVoice(params, reply) {
      const body = {};
      if (params.voice) body.voice = String(params.voice);
      for (const k of ["speed", "stability"]) if (params[k] != null && isFinite(params[k])) body[k] = Number(params[k]);
      if (!Object.keys(body).length) return reply("Say which voice, speed or steadiness to change.", true);
      let r;
      try { r = await this.h.api("/api/agent/voice", body); }
      catch (e) { return reply(`Couldn't change the voice: ${e.message}`, true); }
      this.h.onVoice?.(r);
      reply("Changed. The call is reconnecting in the new voice; say nothing more.");
      await this.reconnect("How's this?");
    }

    // Swap the conversation for a fresh one (new voice settings), keeping mic and audio.
    async reconnect(firstMessage) {
      if (!this.live) return;
      const old = this.ws; this.ws = null;
      if (old && old.readyState <= WebSocket.OPEN) { try { old.close(1000); } catch (_) { /* closing anyway */ } }
      this.flush(); this.pending = []; this.pendingLen = 0;
      this.set("connecting");
      try {
        const session = await this.h.api("/api/agent/session", {});
        if (!this.live) return;
        if (firstMessage) session.first_message = firstMessage;
        if (this.lines.length) session.context = "The call just reconnected to change your voice. "
          + "Conversation so far:\n" + this.lines.join("\n");
        await this.open(session);
        if (this.live) this.set("listening");
      } catch (e) {
        if (!this.live) return;  // hung up while reconnecting
        this.h.onError?.(e.message);
        await this.stop();
      }
    }

    capture(frame) {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      if (!frame || !Number.isSafeInteger(frame.length) || frame.length < 1) return;
      if (this.audioSettings?.allMicrophones && this.inputCandidates?.length > 1) this.evaluateInputs();
      let sum = 0; for (let i = 0; i < frame.length; i++) if (Number.isFinite(frame[i])) sum += frame[i] * frame[i];
      this.h.onLevel?.(Math.min(1, Math.sqrt(sum / frame.length) * 9));
      this.pending.push(frame); this.pendingLen += frame.length;
      if (this.pendingLen < this.ctx.sampleRate * CHUNK_SECONDS) return;
      const all = new Float32Array(this.pendingLen); let o = 0;
      for (const f of this.pending) { all.set(f, o); o += f.length; }
      this.pending = []; this.pendingLen = 0;
      this.send({ user_audio_chunk: toBase64(pcm16(Mic.resample(all, this.ctx.sampleRate, this.inRate))) });
    }

    setAudioSettings(settings) {
      settings = settings || {};
      const gain = Number(settings.gain);
      if (Number.isFinite(gain)) settings = { ...settings, gain: Math.max(0.7, Math.min(1.8, gain)) };
      this.audioSettings = { ...this.audioSettings, ...settings };
      if (this.inputGain && Number.isFinite(gain)) {
        const now = this.ctx.currentTime;
        this.inputGain.gain.setTargetAtTime(this.audioSettings.gain, now, 0.04);
      }
    }

    connectInput(index) {
      const next = this.inputCandidates?.[index];
      if (!next || next.connected) return;
      const previous = this.inputCandidates[this.activeInput];
      if (previous?.connected) {
        try { previous.source.disconnect(this.inputGain); } catch (_) { /* already disconnected */ }
        previous.connected = false;
      }
      next.source.connect(this.inputGain); next.connected = true; this.activeInput = index;
      this.h.onMicSelected?.(next.label);
    }

    evaluateInputs() {
      if (++this.inputEvalTick % 32) return;
      for (const candidate of this.inputCandidates) {
        candidate.analyser.getFloatTimeDomainData(candidate.data);
        let sum = 0, clipped = 0;
        for (const sample of candidate.data) {
          if (Number.isFinite(sample)) sum += sample * sample;
          if (Math.abs(sample) >= 0.98) clipped++;
        }
        candidate.samples.push([Math.sqrt(sum / candidate.data.length), clipped / candidate.data.length]);
      }
      if (this.inputCandidates[0].samples.length < 6) return;
      const score = (candidate) => {
        const levels = candidate.samples.map(([level]) => level).sort((a, b) => a - b);
        const low = levels[Math.floor((levels.length - 1) * 0.2)], high = levels[Math.floor((levels.length - 1) * 0.8)];
        const clipping = candidate.samples.reduce((n, sample) => n + sample[1], 0) / candidate.samples.length;
        const quality = Math.max(0.1, 1 - clipping * 4);
        return { value: Math.max(0, high - low) * Math.sqrt(Math.max(high, 0)) * quality, high };
      };
      const ranked = this.inputCandidates.map((candidate, index) => ({ candidate, index, ...score(candidate) }));
      for (const candidate of this.inputCandidates) candidate.samples = [];
      if (this.inputSwitchWait > 0) { this.inputSwitchWait--; return; }
      ranked.sort((a, b) => b.value - a.value);
      const current = ranked.find((item) => item.index === this.activeInput), best = ranked[0];
      // Require clear speech energy and a meaningful quality win to avoid switching
      // to a noisy or merely louder mic while the room is quiet.
      if (best.index !== this.activeInput && best.high > 0.012 && best.value > Math.max(0.0008, current.value * 1.35)) {
        this.connectInput(best.index);
        this.inputSwitchWait = 4; // keep a clear winner long enough to avoid mic-to-mic chatter
      }
    }

    play(b64) {
      const bin = atob(b64), n = bin.length >> 1;
      if (!n) return;
      const view = new DataView(new ArrayBuffer(n * 2));
      for (let i = 0; i < n * 2; i++) view.setUint8(i, bin.charCodeAt(i));
      const buf = this.ctx.createBuffer(1, n, this.outRate), ch = buf.getChannelData(0);
      for (let i = 0; i < n; i++) ch[i] = view.getInt16(i * 2, true) / 0x8000;
      const node = this.ctx.createBufferSource();
      node.buffer = buf; node.connect(this.ctx.destination);
      const at = Math.max(this.ctx.currentTime + 0.02, this.playAt);
      node.start(at); this.playAt = at + buf.duration;
      this.sources.add(node);
      this.set("speaking");
      node.onended = () => { this.sources.delete(node); if (!this.sources.size && this.live) this.set("listening"); };
    }

    flush() {
      for (const node of this.sources) { try { node.stop(); } catch (_) { /* already ended */ } }
      this.sources.clear(); this.playAt = 0;
      if (this.live) this.set("listening");
    }

    send(obj) { if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj)); }

    async stop() {
      const was = this.state; this.state = "idle";
      const ws = this.ws; this.ws = null;
      if (ws && ws.readyState <= WebSocket.OPEN) { try { ws.close(1000); } catch (_) { /* closing anyway */ } }
      this.flush();
      if (this.node) this.node.port.onmessage = null;
      for (const candidate of this.inputCandidates || [])
        for (const n of [candidate.source, candidate.analyser, candidate.sink]) { try { n?.disconnect(); } catch (_) { /* already disconnected */ } }
      for (const n of [this.inputGain, this.node, this.mute]) { try { n?.disconnect(); } catch (_) { /* already disconnected */ } }
      for (const stream of this.streams || (this.stream ? [this.stream] : []))
        for (const t of stream.getTracks() || []) { try { t.stop(); } catch (_) { /* release the rest */ } }
      try { if (this.ctx && this.ctx.state !== "closed") await this.ctx.close(); } catch (_) { /* closing anyway */ }
      this.inputCandidates = this.inputLabels = this.streams = this.inputGain = this.node = this.mute = this.stream = this.ctx = null;
      this.h.onLevel?.(0);
      if (was !== "idle") this.set("idle");
    }
  }

  window.ElevenAgent = ElevenAgent;
})();
