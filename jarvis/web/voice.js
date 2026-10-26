// Microphone capture, push-to-talk or always-on (`start({ wait: true })` idles until
// you speak and ends the clip when you pause). Audio never leaves the laptop: it is resampled to 16 kHz
// mono float32 and posted to the local server, which checks the voiceprint and runs
// Whisper on the GPU, exactly like the always-on listener.
(function () {
  const TARGET = 16000, MAX_SECONDS = 30, SILENCE_END = 1.2, PREROLL = 0.4;

  function speechThreshold(floor) {
    // Ambient noise is the baseline, not something speech must overpower by 3x.
    // Cap the margin so quieter speech can still open the gate in a busy room.
    return Math.max(0.008, Math.min(floor * 1.6, floor + 0.015));
  }

  class Mic {
    constructor({ wave, onLevel, onAutoStop }) {
      this.wave = wave; this.onLevel = onLevel || (() => {}); this.onAutoStop = onAutoStop || (() => {});
      this.active = false;
      this._session = null;
    }
    get starting() { return Boolean(this._session && !this.active); }
    async start({ wait = false } = {}) {
      if (this.active) return;
      this.wait = wait;
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
        // Opened without a click (always-on at page load), audio stays suspended until the first gesture.
        if (session.ctx.state === "suspended") {
          const wake = () => session.ctx?.resume().catch(() => {});
          for (const ev of ["pointerdown", "keydown"]) addEventListener(ev, wake, { once: true, capture: true });
        }
        await session.ctx.audioWorklet.addModule("recorder-worklet.js");
        if (this._session !== session) return await this._release(session);
        session.source = session.ctx.createMediaStreamSource(session.stream);
        session.node = new AudioWorkletNode(session.ctx, "jarvis-recorder");
        session.analyser = session.ctx.createAnalyser(); session.analyser.fftSize = 1024;
        session.sink = session.ctx.createGain(); session.sink.gain.value = 0;
        session.source.connect(session.node); session.source.connect(session.analyser);
        // The worklet needs a rendered output path to keep receiving mic frames.
        // Route its silent output to the destination so mic audio never reaches speakers.
        session.node.connect(session.sink); session.sink.connect(session.ctx.destination);
        this.stream = session.stream; this.ctx = session.ctx;
        this.node = session.node; this.analyser = session.analyser; this.sink = session.sink;
        this.chunks = []; this.samples = 0; this.total = 0; this.voiced = 0; this.quiet = 0; this.floor = 0.004;
        this.calibration = []; this.calibrationTime = 0; this.calibrated = false;
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
      const { stream, ctx, source, node, analyser, sink } = session;
      session.stream = session.ctx = session.source = session.node = session.analyser = session.sink = null;
      if (node) node.port.onmessage = null;
      for (const connected of [source, node, analyser, sink]) {
        try { connected?.disconnect(); } catch (_) { /* already disconnected */ }
      }
      for (const track of stream?.getTracks() || []) {
        try { track.stop(); } catch (_) { /* still release the other resources */ }
      }
      try { if (ctx && ctx.state !== "closed") await ctx.close(); } catch (_) { /* preserve the startup error */ }
    }
    frame(data) {
      if (!this.active) return;
      if (!data || !Number.isSafeInteger(data.length) || data.length < 1) return;
      const rate = this.ctx.sampleRate, remaining = Math.max(0, Math.floor(rate * MAX_SECONDS) - this.samples);
      if (!remaining) { this.onAutoStop(); return; }
      const length = Math.min(data.length, remaining);
      let chunk = length === data.length ? data : data.subarray(0, length);
      let sum = 0, invalid = false;
      for (let i = 0; i < length; i++) {
        const sample = chunk[i];
        if (!Number.isFinite(sample) || sample < -1 || sample > 1) invalid = true;
        else sum += sample * sample;
      }
      if (invalid) {
        const clean = new Float32Array(length); sum = 0;
        for (let i = 0; i < length; i++) {
          const sample = Number.isFinite(chunk[i]) ? Math.max(-1, Math.min(1, chunk[i])) : 0;
          clean[i] = sample; sum += sample * sample;
        }
        chunk = clean;
      }
      this.chunks.push(chunk); this.samples += length;
      const rms = Math.sqrt(sum / length), dt = length / rate;
      this.total += dt;
      if (!this.calibrated) {
        this.calibration.push([rms, dt]); this.calibrationTime += dt;
        if (this.calibrationTime >= 0.2) this.finishCalibration();
      } else {
        const speaking = rms > speechThreshold(this.floor);
        if (speaking) { this.voiced += dt; this.quiet = 0; }
        else {
          this.quiet += dt;
          // Follow changes in room noise only during frames currently judged quiet.
          // Release slowly so a normal pause does not erase the room baseline and
          // make the same background noise look like a fresh utterance.
          const adapt = 1 - Math.exp(-dt / 6);
          this.floor = Math.max(0.004, this.floor + (rms - this.floor) * adapt);
        }
      }
      if (this.wait && this.voiced < 0.3 && this.quiet > SILENCE_END) {
        // Nothing said yet: follow the room's noise and keep only a short pre-roll,
        // so waiting costs no memory and never runs into the time limit.
        const keep = PREROLL * rate; let n = 0, i = this.chunks.length;
        while (i > 0 && n < keep) n += this.chunks[--i].length;
        this.chunks = this.chunks.slice(i); this.samples = n; this.voiced = 0;
      }
      const elapsed = this.samples / rate;
      this.level = Math.min(1, rms * 9);
      this.onLevel(this.level);
      if ((this.voiced > 0.3 && this.quiet > SILENCE_END) || elapsed >= MAX_SECONDS) this.onAutoStop();
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
      if (!this.calibrated && this.calibration?.length) this.finishCalibration();
      const active = this.active, rate = this.ctx?.sampleRate, voiced = this.voiced;
      const chunks = this.chunks, samples = this.samples;
      this.active = false;
      this.chunks = [];
      try { this.onLevel(0); } finally { await this._release(session); }
      this.stream = this.ctx = this.node = this.analyser = this.sink = null;
      if (!active) return null;
      if (voiced < 0.2) return { audio: null, voiced };
      const all = new Float32Array(samples); let o = 0;
      for (const c of chunks) { all.set(c, o); o += c.length; }
      return { audio: resample(all, rate, TARGET), voiced };
    }
  }

  Mic.prototype.finishCalibration = function () {
    const levels = this.calibration.map(([level]) => level).sort((a, b) => a - b);
    // Do not let speech that starts immediately become an impossibly high noise baseline.
    this.floor = Math.max(0.004, Math.min(0.035, levels[Math.floor((levels.length - 1) * 0.2)]));
    const threshold = speechThreshold(this.floor);
    // Preserve the initial frames; users often press-to-talk and start immediately.
    for (const [level, duration] of this.calibration) {
      if (level > threshold) { this.voiced += duration; this.quiet = 0; }
      else this.quiet += duration;
    }
    this.calibration = []; this.calibrated = true;
  };

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
  Mic.speechThreshold = speechThreshold;
  window.Mic = Mic;
})();
