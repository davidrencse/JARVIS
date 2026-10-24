// Push-to-talk capture. Audio never leaves the laptop: it is resampled to 16 kHz
// mono float32 and posted to the local server, which checks the voiceprint and runs
// Whisper on the GPU, exactly like the always-on listener.
(function () {
  const TARGET = 16000, MAX_SECONDS = 30, SILENCE_END = 1.2;

  class Mic {
    constructor({ wave, onLevel, onAutoStop }) {
      this.wave = wave; this.onLevel = onLevel || (() => {}); this.onAutoStop = onAutoStop || (() => {});
      this.active = false;
      this._session = null;
    }
    get starting() { return Boolean(this._session && !this.active); }
    async start() {
      if (this.active) return;
      if (this._session) return this._session.ready;
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("This browser can't use the microphone here.");
      const session = {};
      this._session = session;
      session.ready = this._start(session);
      return session.ready;
    }
    async _start(session) {
      try {
        session.stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        if (this._session !== session) return await this._release(session);
        session.ctx = new AudioContext();
        await session.ctx.audioWorklet.addModule("recorder-worklet.js");
        if (this._session !== session) return await this._release(session);
        session.source = session.ctx.createMediaStreamSource(session.stream);
        session.node = new AudioWorkletNode(session.ctx, "jarvis-recorder");
        const analyser = session.ctx.createAnalyser(); analyser.fftSize = 1024;
        session.source.connect(session.node); session.source.connect(analyser);
        this.stream = session.stream; this.ctx = session.ctx;
        this.node = session.node; this.analyser = analyser;
        this.chunks = []; this.samples = 0; this.voiced = 0; this.quiet = 0; this.floor = 0.004;
        this.node.port.onmessage = (ev) => this.frame(ev.data);
        this.active = true; this.t0 = performance.now();
        this.paint();
      } catch (error) {
        if (this._session === session) { this._session = null; this.active = false; }
        await this._release(session);
        throw error;
      }
    }
    async _release(session) {
      const { stream, ctx, source, node } = session;
      session.stream = session.ctx = session.source = session.node = null;
      if (node) node.port.onmessage = null;
      for (const connected of [source, node]) {
        try { connected?.disconnect(); } catch (_) { /* already disconnected */ }
      }
      for (const track of stream?.getTracks() || []) {
        try { track.stop(); } catch (_) { /* still release the other resources */ }
      }
      try { if (ctx && ctx.state !== "closed") await ctx.close(); } catch (_) { /* preserve the startup error */ }
    }
    frame(data) {
      if (!this.active) return;
      this.chunks.push(data); this.samples += data.length;
      let sum = 0; for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const rms = Math.sqrt(sum / data.length), dt = data.length / this.ctx.sampleRate;
      const elapsed = this.samples / this.ctx.sampleRate;
      if (elapsed < 0.35) this.floor = Math.max(this.floor, rms * 0.9);
      const speaking = rms > Math.max(this.floor * 3, 0.012);
      if (speaking) { this.voiced += dt; this.quiet = 0; } else this.quiet += dt;
      this.level = Math.min(1, rms * 9);
      this.onLevel(this.level);
      if ((this.voiced > 0.3 && this.quiet > SILENCE_END) || elapsed > MAX_SECONDS) this.onAutoStop();
    }
    paint(session = this._session) {
      if (!this.active || !this.wave || this._session !== session) return;
      const c = this.wave, g = c.getContext("2d"), dpr = Math.min(devicePixelRatio || 1, 2);
      const w = c.clientWidth, h = c.clientHeight;
      if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
      const buf = new Uint8Array(this.analyser.frequencyBinCount); this.analyser.getByteTimeDomainData(buf);
      g.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--inv-ink").trim();
      g.lineWidth = 1.4; g.beginPath();
      const left = 40, span = w - left - 10;
      for (let i = 0; i < buf.length; i += 8) {
        const x = left + (i / buf.length) * span, y = h / 2 + ((buf[i] - 128) / 128) * h * 0.9;
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.stroke();
      requestAnimationFrame(() => this.paint(session));
    }
    async stop() {
      const session = this._session;
      if (!session) return null;
      this._session = null;
      const active = this.active, rate = this.ctx?.sampleRate, voiced = this.voiced;
      const chunks = this.chunks, samples = this.samples;
      this.active = false;
      this.chunks = [];
      try { this.onLevel(0); } finally { await this._release(session); }
      if (!active) return null;
      if (voiced < 0.2) return { audio: null, voiced };
      const all = new Float32Array(samples); let o = 0;
      for (const c of chunks) { all.set(c, o); o += c.length; }
      return { audio: resample(all, rate, TARGET), voiced };
    }
  }

  // Box-filtered decimation: average the source samples that fall in each output slot.
  function resample(input, from, to) {
    if (from === to) return input;
    const ratio = from / to, out = new Float32Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const a = Math.floor(i * ratio), b = Math.min(input.length, Math.floor((i + 1) * ratio));
      let s = 0; for (let j = a; j < b; j++) s += input[j];
      out[i] = s / Math.max(1, b - a);
    }
    return out;
  }

  Mic.resample = resample;
  window.Mic = Mic;
})();
