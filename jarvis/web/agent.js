// Live voice call with the ElevenLabs agent. The page connects straight to ElevenLabs
// over a signed WebSocket (the key stays on the server): mic audio goes up as 16-bit
// PCM, the agent's voice comes back as PCM and is queued gaplessly. Barge-in clears the
// queue. When the agent calls its ask_jarvis tool, the request runs through the local
// server like any typed ask and the reply goes back to the agent to speak.
(function () {
  const CHUNK_SECONDS = 0.1;

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
      const v = Math.max(-1, Math.min(1, float32[i]));
      out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    return new Uint8Array(out.buffer);
  }

  class ElevenAgent {
    // hooks: api(path, body), onState(state), onLevel(0..1), onUser(text),
    //        onAgent(text), onTool(request, phase), onError(message)
    constructor(hooks) {
      this.h = hooks; this.state = "idle"; this.sources = new Set();
    }
    get live() { return this.state !== "idle"; }
    set(state) { this.state = state; this.h.onState?.(state); }

    async start() {
      if (this.live) return;
      this.set("connecting");
      try {
        const session = await this.h.api("/api/agent/session", {});
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        this.ctx = new AudioContext();
        await this.ctx.audioWorklet.addModule("recorder-worklet.js");
        this.inRate = 16000; this.outRate = 16000; this.playAt = 0;
        this.pending = []; this.pendingLen = 0;
        await this.open(session);
        const src = this.ctx.createMediaStreamSource(this.stream);
        this.node = new AudioWorkletNode(this.ctx, "jarvis-recorder");
        this.node.port.onmessage = (ev) => this.capture(ev.data);
        src.connect(this.node); this.src = src;
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
          const init = { type: "conversation_initiation_client_data" };
          if (session.voice_id) init.conversation_config_override = { tts: { voice_id: session.voice_id } };
          ws.send(JSON.stringify(init));
        };
        ws.onmessage = (ev) => {
          let msg; try { msg = JSON.parse(ev.data); } catch (_) { return; }
          if (msg.type === "conversation_initiation_metadata") {
            const meta = msg.conversation_initiation_metadata_event || {};
            this.inRate = rateOf(meta.user_input_audio_format, 16000);
            this.outRate = rateOf(meta.agent_output_audio_format, 16000);
            this.conversation = meta.conversation_id;
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
          this.h.onUser?.(msg.user_transcription_event.user_transcript);
          break;
        case "agent_response":
          this.h.onAgent?.(msg.agent_response_event.agent_response);
          break;
        case "client_tool_call":
          this.tool(msg.client_tool_call);
          break;
      }
    }

    async tool(call) {
      const reply = (result, isError) => this.send({ type: "client_tool_result", tool_call_id: call.tool_call_id, result, is_error: !!isError });
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

    capture(frame) {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      let sum = 0; for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
      this.h.onLevel?.(Math.min(1, Math.sqrt(sum / frame.length) * 9));
      this.pending.push(frame); this.pendingLen += frame.length;
      if (this.pendingLen < this.ctx.sampleRate * CHUNK_SECONDS) return;
      const all = new Float32Array(this.pendingLen); let o = 0;
      for (const f of this.pending) { all.set(f, o); o += f.length; }
      this.pending = []; this.pendingLen = 0;
      this.send({ user_audio_chunk: toBase64(pcm16(Mic.resample(all, this.ctx.sampleRate, this.inRate))) });
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
      for (const n of [this.src, this.node]) { try { n?.disconnect(); } catch (_) { /* already disconnected */ } }
      for (const t of this.stream?.getTracks() || []) { try { t.stop(); } catch (_) { /* release the rest */ } }
      try { if (this.ctx && this.ctx.state !== "closed") await this.ctx.close(); } catch (_) { /* closing anyway */ }
      this.src = this.node = this.stream = this.ctx = null;
      this.h.onLevel?.(0);
      if (was !== "idle") this.set("idle");
    }
  }

  window.ElevenAgent = ElevenAgent;
})();
