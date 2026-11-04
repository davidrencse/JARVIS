"""Hearing: microphone → is it David? → text. Everything runs locally, so it costs nothing.

- Utterance capture: Silero voice activity detection (bundled with faster-whisper) on
  32 ms blocks; without it, a loudness threshold calibrated against the room's noise.
- Speaker check: sherpa-onnx ERes2Net speaker embedding (≈26 MB ONNX model, CPU),
  cosine similarity against an enrolled voiceprint. Runs before transcription so
  other voices never reach the model or the agents.
- Transcription: faster-whisper on the RTX 4060 (CUDA fp16), CPU int8 fallback.

Optional packages: pip install numpy sounddevice faster-whisper sherpa-onnx
Speaker model: SPEAKER_MODEL_URL below, saved to .jarvis/voice/.
"""

import json
import os
import time
from pathlib import Path

RATE = 16000
FRAME = 480  # 30 ms at 16 kHz
SPEAKER_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                     "speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx")
# Measured on David's Dell mic (2026-10-20): ERes2Net separated him from other voices
# by +0.51 cosine; both CAM++ models scored strangers and even noise above him.
# Mix of short commands (how JARVIS is really used) and longer sentences.
ENROLL_PHRASES = (
    "Jarvis, what's on my schedule today?",
    "Jarvis, stop.",
    "Hey Jarvis, remind me to push the recon branch tonight.",
    "I'm teaching Jarvis my voice so it can pick me out from anyone else in the room.",
    "Okay Jarvis, how much did I spend on API credits this month?",
    "Jarvis, summarize my unread email and flag anything urgent.",
    "Jarvis, start a new plan for the Underlord game.",
)
INSTALL_HINT = "Voice input needs: pip install numpy sounddevice faster-whisper sherpa-onnx"


def _np():
    try:
        import numpy
    except ImportError:
        raise RuntimeError(INSTALL_HINT) from None
    return numpy


def segment(samples, noise_floor, start_ratio=3.0, min_speech=0.25, max_silence=0.8):
    """Split float32 mono audio into speech segments using frame RMS energy.

    Returns a list of (start_sample, end_sample). Pure function, unit-testable.
    """
    np = _np()
    frames = len(samples) // FRAME
    if not frames:
        return []
    rms = np.sqrt(np.mean(samples[:frames * FRAME].reshape(frames, FRAME) ** 2, axis=1))
    voiced = rms > max(noise_floor * start_ratio, 1e-4)
    gap = int(max_silence * RATE / FRAME)
    segments, start, quiet = [], None, 0
    for i, v in enumerate(voiced):
        if v:
            start = i if start is None else start
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet > gap:
                segments.append((start, i - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        segments.append((start, frames - quiet))
    keep = int(min_speech * RATE / FRAME)
    return [(s * FRAME, e * FRAME) for s, e in segments if e - s >= keep]


def _input_device(sd, preferred=None):
    """The mic chosen in voice setup if it is still plugged in. Otherwise, on Linux, ALSA's
    "default" is the raw sound card unless pipewire-alsa is installed, and a DSP mic read
    that way is pure silence. Go through PipeWire (or Pulse) instead."""
    devices = [(i, d) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    names = {d["name"] for _, d in devices}
    if preferred is not None and (preferred in names or preferred in (i for i, _ in devices)):
        return _resolve_device(sd, devices, preferred)
    if os.name == "nt":
        return None
    return next((n for n in ("pipewire", "pulse") if n in names), None)


def _resolve_device(sd, devices, preferred):
    """ONE device index for the saved mic name. On Windows the same name exists under MME,
    DirectSound and WASAPI, and sounddevice refuses a name that matches several."""
    if isinstance(preferred, int):
        return preferred
    matches = [(i, d) for i, d in devices if d["name"] == preferred]
    if len(matches) == 1:
        return matches[0][0]
    if os.name == "nt":
        try:
            apis = sd.query_hostapis()
            for i, d in matches:
                if "wasapi" in str(apis[d["hostapi"]]["name"]).lower():
                    return i
        except Exception:
            pass
    return matches[0][0]


def input_devices():
    """Microphones the setup screen can offer. Raw ALSA hw: endpoints are left out on
    Linux: on these laptops they are the DSP mic read around PipeWire, i.e. silence."""
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError(INSTALL_HINT) from None
    seen, out = set(), []
    for d in sd.query_devices():
        name = d["name"]
        if d["max_input_channels"] < 1 or name in seen or (os.name != "nt" and "(hw:" in name):
            continue
        seen.add(name)
        out.append(name)
    return out


MIC_FILE = "mic.json"


def mic_settings(state_dir):
    """What voice setup measured for this machine's mic: the device, and the speech
    cutoffs fitted to the room. {} before setup has run."""
    try:
        return json.loads((Path(state_dir) / "voice" / MIC_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_mic_settings(state_dir, data):
    path = Path(state_dir) / "voice" / MIC_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fit_cutoffs(noise_probs, speech_probs):
    """Speech cutoffs for this mic and room, from Silero scores of quiet-room blocks and of
    the owner speaking. Starting needs a score clearly above the room's noise; once speech
    has started, a lower score holds it so soft word endings and short pauses aren't cut.
    The start comes down for a quiet voice or a far mic, never below the room's own noise."""
    np = _np()
    noise = float(np.percentile(noise_probs, 95)) if len(noise_probs) else 0.0
    voiced = [p for p in speech_probs if p > 0.1]
    speech = float(np.percentile(voiced, 30)) if voiced else 1.0
    start = min(START_CUTOFF, speech * 0.6)
    start = round(min(0.6, max(start, noise + 0.12, 0.15)), 3)
    hold = round(min(HOLD_CUTOFF, max(0.1, start * 0.45)), 3)
    return start, hold


def speech_threshold(floor_rms):
    """Loudness a frame needs to count as speech, from the RMS of quiet-room frames.
    The 20th percentile ignores a cough or the user starting early; in a noisy room
    (fans, other voices) a conservative floor multiple can miss quieter speech. Keep a
    small absolute margin while limiting the multiplier so normal mic-level speech starts."""
    np = _np()
    floor = float(np.percentile(np.asarray(floor_rms, dtype="float64"), 20))
    return max(0.004, min(floor * 3.0, floor * 1.5 + 0.01))


def _vad_model():
    """faster-whisper's bundled Silero VAD, loaded once; None if it isn't installed."""
    global _VAD
    if _VAD is None:
        try:
            from faster_whisper.vad import get_vad_model
            _VAD = get_vad_model()
        except Exception:
            _VAD = False
    return _VAD or None


_VAD = None
VAD_CHUNK = 512  # 32 ms: the block size Silero VAD scores
# Silero scored David's enrolled speech at a median of 0.97 and room noise under 0.04
# (2026-10-29). A single 0.18 cutoff let TV and music open ~1,600 clips in two days;
# starting at 0.4 and holding at 0.18 keeps soft speech without the junk.
START_CUTOFF, HOLD_CUTOFF = 0.4, 0.18


class _SpeechGate:
    """Is this block speech? Scores the newest block with the context before it, so fans,
    keyboards and room hum don't count the way they do for a loudness threshold.
    Hysteresis: two blocks in a row above `start` engage it, then anything above `hold`
    stays speech until a block falls below; a lone click never reaches the lower bar."""

    def __init__(self, model, context=32, start=START_CUTOFF, hold=HOLD_CUTOFF):
        self.model, self.start, self.hold, self.recent = model, start, min(hold, start), []
        self.context, self.run, self.engaged, self.last = context, 0, False, 0.0
        # Silero carries its LSTM state (h, c) and the previous block's last 64 samples
        # between blocks; scoring one block with them gives exactly what scoring the whole
        # stream would. Re-scoring a 32-block window per block did 32x the work, nonstop.
        self.session = getattr(model, "session", None)
        self.h = self.c = None

    def _stream(self, block):
        np = _np()
        if self.h is None:
            self.h = np.zeros((1, 1, 128), dtype="float32")
            self.c = np.zeros((1, 1, 128), dtype="float32")
            self.tail = np.zeros(64, dtype="float32")
        x = np.concatenate([self.tail, np.asarray(block, dtype="float32")])[None]
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.tail = x[0, -64:]
        return float(out.reshape(-1)[-1])

    def __call__(self, block):
        np = _np()
        if self.session is not None and len(block) == VAD_CHUNK:
            self.last = p = self._stream(block)
        else:  # a stand-in model (tests) or an odd-sized block: score a window, as before
            self.recent = (self.recent + [block])[-self.context:]
            self.last = p = float(self.model(np.concatenate(self.recent)).reshape(-1)[-1])
        self.run = self.run + 1 if p > self.start else 0
        if self.run >= 2:
            self.engaged = True
        elif p <= self.hold:
            self.engaged = False
        return p > self.start or (self.engaged and p > self.hold)


def _collect(read, is_speech, block, max_seconds, max_silence, wait_seconds, min_voiced,
             onset, preroll=0, initial=()):
    """One utterance from blocks of `block` samples: starts after `onset` speech blocks in
    a row (keeping `preroll` blocks before them), ends after `max_silence` seconds without
    speech. None if nobody spoke, or there was too little speech to be a sentence."""
    np = _np()
    # Calibration frames are also capture pre-roll. Without this, the no-VAD path
    # threw away the first half second of speech every time the mic reopened.
    recent, run, heard, quiet, voiced, waited = [], 0, [], 0, 0, 0
    for chunk in initial:
        speech = is_speech(chunk)
        recent = (recent + [chunk])[-(onset + preroll):]
        run = run + 1 if speech else 0
        if run >= onset:
            heard, voiced = recent.copy(), run
    while True:
        chunk = read()
        speech = is_speech(chunk)
        if not heard:
            waited += 1
            if wait_seconds is not None and waited * block > wait_seconds * RATE:
                return None
            recent = (recent + [chunk])[-(onset + preroll):]
            run = run + 1 if speech else 0
            if run >= onset:
                heard, voiced = recent, onset
            continue
        heard.append(chunk)
        voiced += speech
        quiet = 0 if speech else quiet + 1
        if quiet * block > max_silence * RATE or len(heard) * block > max_seconds * RATE:
            if voiced * block < min_voiced * RATE:
                return None  # too little actual speech: a cough, a tap, a word fragment
            # Drop trailing silence so it doesn't dilute the speaker embedding.
            return np.concatenate(heard[:len(heard) - quiet] or heard)


def record_utterance(max_seconds=20, max_silence=0.8, calibrate=0.5, wait_seconds=None,
                     min_voiced=0.2, mic=None, tuning=None, gate=None):
    """Block until one spoken utterance ends; return float32 mono 16 kHz audio or None.
    Uses Silero VAD when faster-whisper is installed, else a loudness threshold.
    `mic` is a LiveMic held open between calls; without one the mic opens per call.
    `tuning` is mic_settings() (device and cutoffs; a LiveMic carries its own), and
    `gate` replaces the speech check outright (voice setup watches the scores)."""
    vad = _vad_model()
    block = VAD_CHUNK if vad else FRAME
    tuning = tuning if tuning is not None else getattr(mic, "tuning", None) or {}
    if vad and gate is None:
        gate = _SpeechGate(vad, start=tuning.get("vad_start", START_CUTOFF),
                           hold=tuning.get("vad_hold", HOLD_CUTOFF))
    if mic is not None:
        return _utterance(mic.read, block, gate, max_seconds, max_silence, calibrate,
                          wait_seconds, min_voiced)
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError(INSTALL_HINT) from None
    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=block,
                        device=_input_device(sd, tuning.get("device"))) as stream:
        def read():
            return stream.read(block)[0][:, 0]
        return _utterance(read, block, gate, max_seconds, max_silence, calibrate,
                          wait_seconds, min_voiced)


def _utterance(read, block, gate, max_seconds, max_silence, calibrate, wait_seconds,
               min_voiced):
    np = _np()
    if gate:
        # Two positive blocks avoid triggering on a single click; the gate's low hold
        # cutoff and 384 ms of preroll catch quiet speech without clipping its start.
        return _collect(read, gate, block, max_seconds, max_silence,
                        wait_seconds, min_voiced, onset=2, preroll=12)
    calibration = [read() for _ in range(max(1, int(calibrate * RATE / block)))]
    floor_rms = [float(np.sqrt(np.mean(chunk ** 2))) for chunk in calibration]
    threshold = speech_threshold(floor_rms)
    # 2 frames of sound start an utterance; preserve calibration frames so an
    # immediately spoken wake word isn't cut off while the noise floor is measured.
    return _collect(read, lambda b: float(np.sqrt(np.mean(b ** 2))) > threshold, block,
                    max_seconds, max_silence, wait_seconds, min_voiced, onset=2,
                    preroll=12, initial=calibration)


class LiveMic:
    """The mic held open for the always-on listener. Opening it per utterance left it
    deaf while the last clip was checked and transcribed (about 1.3 s on CPU, after every
    clip of music or other voices too), so a "Jarvis" said then was lost or clipped.
    A PortAudio callback queues every block instead; at most `backlog` seconds wait."""

    def __init__(self, backlog=15.0, stream=None, tuning=None):
        import queue
        self._queue = queue
        self.tuning = tuning or {}
        self.level = 0.0  # RMS of the newest block, for the setup screen's meter
        self.peak = 0.0   # loudest block since take_peak(), for the console's wave
        self.block = VAD_CHUNK if _vad_model() else FRAME
        self.blocks = queue.Queue(maxsize=max(1, int(backlog * RATE / self.block)))
        if stream is None:
            try:
                import sounddevice as sd
            except ImportError:
                raise RuntimeError(INSTALL_HINT) from None
            stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                                    blocksize=self.block,
                                    device=_input_device(sd, self.tuning.get("device")),
                                    callback=self._callback)
        self.stream = stream
        self.stream.start()

    def _callback(self, data, frames, time_info, status):
        chunk = data[:, 0].copy()
        self.level = float((chunk ** 2).mean()) ** 0.5
        if self.level > self.peak:
            self.peak = self.level
        while True:
            try:
                self.blocks.put_nowait(chunk)
                return
            except self._queue.Full:  # keep the newest audio, drop the oldest
                try:
                    self.blocks.get_nowait()
                except self._queue.Empty:
                    pass

    def read(self):
        try:
            return self.blocks.get(timeout=5)
        except self._queue.Empty:
            raise RuntimeError("The microphone stopped sending audio.") from None

    def take_peak(self):
        """The loudest block since the last call (0 when none arrived), then reset."""
        peak, self.peak = self.peak, 0.0
        return peak

    def flush(self):
        """Drop queued audio, e.g. what the mic heard while JARVIS was talking."""
        while True:
            try:
                self.blocks.get_nowait()
            except self._queue.Empty:
                return

    def close(self):
        try:
            self.stream.stop()
        finally:
            self.stream.close()


def save_wav(path, audio):
    """16-bit mono WAV, used to keep enrollment clips for re-calibration/diagnosis."""
    import wave
    np = _np()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


class VoicePrint:
    """Enrolled speaker embedding + threshold, stored in .jarvis/voice/voiceprint.json."""

    def __init__(self, state_dir, config=None):
        cfg = (config or {}).get("voice", {})
        self.dir = Path(state_dir).resolve() / "voice"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.model = Path(cfg.get("speaker_model") or self.dir / Path(SPEAKER_MODEL_URL).name)
        self.path = self.dir / "voiceprint.json"
        self._extractor = None
        self.last_embedding = None

    def _embed(self, audio):
        np = _np()
        if self._extractor is None:
            try:
                import sherpa_onnx
            except ImportError:
                raise RuntimeError(INSTALL_HINT) from None
            if not self.model.is_file():
                raise RuntimeError(f"Speaker model missing at {self.model}. Download "
                                   f"{SPEAKER_MODEL_URL}")
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self.model), num_threads=6))
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=RATE, waveform=audio)
        stream.input_finished()
        vector = np.array(self._extractor.compute(stream), dtype="float32")
        return vector / (np.linalg.norm(vector) or 1.0)

    # ERes2Net cosine on this mic: other voices ≤ 0.26, JARVIS's own Kokoro voice ≤ 0.18,
    # David live (not enrollment) 0.38–0.45. Enrollment clips score higher than live
    # speech, so the learned threshold is capped well below them.
    FLOOR, CEILING, MARGIN, ADAPT_MARGIN, MAX_PROFILE = 0.30, 0.36, 0.10, 0.12, 40

    @staticmethod
    def _unit(vector):
        np = _np()
        return vector / (np.linalg.norm(vector) or 1.0)

    @classmethod
    def score(cls, embedding, profile):
        """Best of: match to the centroid, or mean of the 3 closest enrolled clips."""
        np = _np()
        profile = np.asarray(profile, dtype="float32")
        centroid = float(embedding @ cls._unit(profile.mean(axis=0)))
        nearest = np.sort(profile @ embedding)[-3:]
        return max(centroid, float(nearest.mean()))

    @classmethod
    def calibrate(cls, embeddings):
        """Leave-one-out: score each clip against the others; threshold sits just below
        the weakest genuine match, clamped to [FLOOR, CEILING]."""
        np = _np()
        if len(embeddings) < 2:
            raise ValueError("Need at least 2 clips to calibrate.")
        own = [cls.score(e, [o for j, o in enumerate(embeddings) if j != i])
               for i, e in enumerate(embeddings)]
        threshold = min(cls.CEILING, max(cls.FLOOR, min(own) - cls.MARGIN))
        return cls._unit(np.mean(embeddings, axis=0)), threshold, own

    def _add(self, data, embedding):
        """Append a learned clip. Enrollment clips are pinned; only learned ones rotate."""
        np = _np()
        profile = data.get("profile") or [data["embedding"]]
        pinned = profile[:data.get("clips", 0)]
        room = max(1, self.MAX_PROFILE - len(pinned))  # [-0:] would keep everything
        learned = (profile[len(pinned):] + [embedding.tolist()])[-room:]
        data["profile"] = pinned + learned
        data["embedding"] = self._unit(np.mean(data["profile"], axis=0)).tolist()
        self._save(data, keep_threshold=True)

    def learn(self):
        """Add the last verified clip: called once the transcript also starts with 'Jarvis',
        which is strong evidence it was David talking to JARVIS."""
        if self.last_embedding is not None and self.path.is_file():
            self._add(self._load(), self.last_embedding)
            self.last_embedding = None

    def _save(self, data, keep_threshold=False):
        # voice service and console transcribe both read and write this file
        from .fsio import atomic_write
        try:  # learning: the threshold is Settings' to change, so keep what the file holds now
            current = json.loads(self.path.read_text(encoding="utf-8")) if keep_threshold else {}
            if "threshold" in current:
                data = {**data, "threshold": current["threshold"]}
        except (OSError, ValueError):
            pass
        atomic_write(self.path, json.dumps(data))

    def _load(self):
        """The voiceprint, tolerating a read that races another process's write."""
        for attempt in range(3):
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                if attempt == 2:
                    raise
                time.sleep(0.05)

    def enroll(self, clips):
        if len(clips) < 3:
            raise ValueError("Enroll with at least 3 clips of your voice.")
        embeddings = [self._embed(c) for c in clips]
        center, threshold, own = self.calibrate(embeddings)
        self._save({"embedding": center.tolist(), "profile": [e.tolist() for e in embeddings],
                    "threshold": threshold, "clips": len(clips), "model": self.model.name})
        return {"threshold": round(threshold, 3), "clips": len(clips),
                "own_scores": [round(s, 3) for s in own], "path": str(self.path)}

    def recalibrate(self):
        """Rebuild the voiceprint from saved enrollment clips (e.g. after a model change).
        Built-in local voices in voice/impostors/ double-check the cutoff rejects others."""
        import glob
        import wave
        np = _np()

        def load(path):
            with wave.open(path) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype("float32")
                rate = w.getframerate()
            pcm /= 32768
            if rate != RATE:
                pcm = np.interp(np.arange(0, len(pcm), rate / RATE), np.arange(len(pcm)),
                                pcm).astype("float32")
            return pcm

        clips = [load(p) for p in sorted(glob.glob(str(self.dir / "enroll" / "*.wav")))]
        if len(clips) < 3:
            raise RuntimeError("No saved enrollment clips. Run: jarvis voice enroll")
        result = self.enroll(clips)
        others = sorted(glob.glob(str(self.dir / "impostors" / "*.wav")))
        if others:
            data = self._load()
            scores = [self.score(self._embed(load(p)), data["profile"]) for p in others]
            result["impostor_max"] = round(max(scores), 3)
            result["safe"] = max(scores) < data["threshold"]
        return result

    def verify(self, audio, threshold=None, learn=True):
        """(accepted, score). Confident matches join the profile so it adapts to the
        real mic and room; borderline ones never do. learn=False only scores (a mic check)."""
        _np()  # fail early with the install hint if numpy is missing
        if not self.path.is_file():
            raise RuntimeError("No voiceprint yet. Run: python -m jarvis voice enroll")
        data = self._load()
        if data.get("model") != self.model.name:
            raise RuntimeError("Voiceprint was made with a different speaker model. "
                               "Run: jarvis voice recalibrate")
        profile = data.get("profile") or [data["embedding"]]
        embedding = self._embed(audio)
        score = self.score(embedding, profile)
        limit = data["threshold"] if threshold is None else threshold
        self.last_embedding = embedding if score >= limit else None
        if learn and score >= data["threshold"] + self.ADAPT_MARGIN and "profile" in data:
            self._add(data, embedding)
            self.last_embedding = None  # already learned
        return score >= limit, round(score, 3)


def _expose_cuda_dlls():
    """pip's nvidia-cublas/cudnn wheels put DLLs in site-packages/nvidia/*/bin, which
    Windows doesn't search. Add them so ctranslate2 finds CUDA instead of failing over."""
    import os
    import sys
    if sys.platform != "win32":
        return
    import site
    for root in site.getsitepackages() + [site.getusersitepackages()]:
        for bin_dir in Path(root, "nvidia").glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


class Moonshine:
    """Moonshine ONNX (pip: useful-moonshine-onnx), English only. Whisper pads every clip
    to 30 s, so on this CPU a 2 s command cost ~2.5 s with small.en; Moonshine encodes
    only the audio it gets (~0.2-0.8 s here, measured 2026-11-04). Loaded on first use."""

    def __init__(self, name="base"):
        self.name, self._model, self._tok = name, None, None

    def __call__(self, audio):
        if self._model is None:
            from moonshine_onnx import MoonshineOnnxModel, load_tokenizer
            self._model, self._tok = MoonshineOnnxModel(model_name=self.name), load_tokenizer()
        import numpy as np
        audio = np.asarray(audio, dtype="float32")
        if len(audio) < RATE // 10:  # the model takes 0.1-64 s
            return ""
        return self._tok.decode_batch(self._model.generate(audio[:64 * RATE][None]))[0].strip()


def plausible(text, seconds):
    """False for what a small model emits on noise: too many words for the time spoken,
    or one word looping ("the the the the the")."""
    words = text.split()
    if not words or len(words) > seconds * 6 + 3:
        return False
    run = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a.lower() == b.lower() else 1
        if run > 4:
            return False
    return True


class Transcriber:
    def __init__(self, config=None):
        cfg = (config or {}).get("voice", {})
        self.model_name = cfg.get("stt_model", "large-v3-turbo")
        self.language = cfg.get("language", "en")
        # voice.stt_engine "moonshine": try Moonshine first and keep its words only when
        # they carry the wake word; anything else (no "Jarvis", noise, a failure) is
        # transcribed by Whisper as before, so a misheard wake word is never lost.
        self.engine = cfg.get("stt_engine", "whisper")
        self.fast = Moonshine(cfg.get("moonshine_model", "base")) \
            if self.engine == "moonshine" and self.language.startswith("en") else None
        self._model, self.device = None, None
        self._stats = {}
        self.last_engine = None

    def __call__(self, audio):
        if self.fast is not None and self.device != "cuda":  # Whisper on a GPU is fast already
            text = self._try_fast(audio)
            if text is not None:
                return text
        self.last_engine = "whisper"
        return self._whisper(audio)

    def _try_fast(self, audio):
        """Moonshine's words when they hold the wake word, else None (Whisper decides)."""
        from .assistant import wake_word
        try:
            text = self.fast(audio)
        except Exception:
            self.fast = None  # missing package or model: Whisper only from now on
            return None
        if not plausible(text, len(audio) / RATE) or wake_word(text) is None:
            return None
        # No log probabilities from Moonshine: unsure() sees None and lets it through.
        self.last = {"logprob": None, "no_speech": None, "engine": "moonshine"}
        self._remember(audio, self.last)
        self.last_engine = "moonshine"
        return text

    def _remember(self, audio, stats):
        self._stats[id(audio)] = stats
        while len(self._stats) > 8:
            self._stats.pop(next(iter(self._stats)))

    def _whisper(self, audio):
        if self._model is None:
            _expose_cuda_dlls()
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise RuntimeError(INSTALL_HINT) from None
            try:
                import ctranslate2
                if not ctranslate2.get_cuda_device_count():  # e.g. the Zenbook: skip the
                    raise RuntimeError("no CUDA device")    # big GPU model's download
                self._model = WhisperModel(self.model_name, device="cuda", compute_type="float16")
                self.device = "cuda"
            except Exception:  # no CUDA runtime: fall back to CPU
                self._model = WhisperModel("small.en", device="cpu", compute_type="int8")
                self.device = "cpu"
        try:
            return self._run(audio)
        except RuntimeError as exc:  # missing cuBLAS/cuDNN often surfaces only here
            if self.device != "cuda" or not any(w in str(exc).lower()
                                                for w in ("cuda", "cublas", "cudnn")):
                raise
            from faster_whisper import WhisperModel
            self._model = WhisperModel("small.en", device="cpu", compute_type="int8")
            self.device = "cpu"
            return self._run(audio)

    def _run(self, audio):
        segments, _ = self._model.transcribe(audio, language=self.language, vad_filter=True,
                                             beam_size=1, hotwords="Jarvis")
        segments = list(segments)
        tokens = sum(len(s.tokens) for s in segments)
        # How sure Whisper was, for unsure(): token-weighted log probability, worst silence score.
        # Kept per clip (stats(audio)): on CUDA a cancelled stranger's clip can finish later.
        self.last = {"logprob": (sum(s.avg_logprob * len(s.tokens) for s in segments) / tokens
                                 if tokens else None),
                     "no_speech": max((s.no_speech_prob for s in segments), default=None)}
        self._remember(audio, self.last)
        return " ".join(s.text.strip() for s in segments).strip()

    def stats(self, audio):
        """The confidence of this clip's transcript (see unsure()), or None."""
        return self._stats.pop(id(audio), None)


# Whisper's own fallback threshold is avg_logprob < -1.0; below it the words are mostly guessed
# ("for the day." answered with "It's Tuesday.", 2026-10-27). Mostly-silence clips that still
# produced text are guesses too.
UNSURE_LOGPROB, UNSURE_NO_SPEECH = -1.0, 0.6


def unsure(stats):
    """True when a transcript's confidence (Transcriber.last) says it was probably misheard."""
    if not isinstance(stats, dict) or stats.get("logprob") is None:
        return False
    logprob, silence = stats["logprob"], stats.get("no_speech") or 0.0
    return logprob < UNSURE_LOGPROB or (silence > UNSURE_NO_SPEECH and logprob < -0.6)
