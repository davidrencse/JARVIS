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
    "Jarvis, open the trading dashboard.",
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


def _input_device(sd):
    """On Linux, ALSA's "default" is the raw sound card unless pipewire-alsa is installed,
    and a DSP mic read that way is pure silence. Go through PipeWire (or Pulse) instead."""
    if os.name == "nt":
        return None
    names = {d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0}
    return next((n for n in ("pipewire", "pulse") if n in names), None)


def speech_threshold(floor_rms):
    """Loudness a frame needs to count as speech, from the RMS of quiet-room frames.
    The 20th percentile ignores a cough or the user starting early; in a noisy room
    (fans, other voices) a conservative floor multiple can miss quieter speech. Keep a
    small absolute margin while limiting the multiplier so normal mic-level speech starts."""
    np = _np()
    floor = float(np.percentile(np.asarray(floor_rms, dtype="float64"), 20))
    return max(0.002, min(floor * 2.0, floor * 1.2 + 0.008))


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


class _SpeechGate:
    """Is this block speech? Scores the newest block with ~1 s of context, so fans,
    keyboards and room hum don't count the way they do for a loudness threshold."""

    def __init__(self, model, context=32, cutoff=0.35):
        self.model, self.cutoff, self.recent = model, cutoff, []
        self.context = context

    def __call__(self, block):
        np = _np()
        self.recent = (self.recent + [block])[-self.context:]
        return float(self.model(np.concatenate(self.recent)).reshape(-1)[-1]) > self.cutoff


def _collect(read, is_speech, block, max_seconds, max_silence, wait_seconds, min_voiced,
             onset, preroll=0):
    """One utterance from blocks of `block` samples: starts after `onset` speech blocks in
    a row (keeping `preroll` blocks before them), ends after `max_silence` seconds without
    speech. None if nobody spoke, or there was too little speech to be a sentence."""
    np = _np()
    recent, run, heard, quiet, voiced, waited = [], 0, [], 0, 0, 0
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


def record_utterance(max_seconds=20, max_silence=0.6, calibrate=0.5, wait_seconds=None,
                     min_voiced=0.4):
    """Block until one spoken utterance ends; return float32 mono 16 kHz audio or None.
    Uses Silero VAD when faster-whisper is installed, else a loudness threshold."""
    np = _np()
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError(INSTALL_HINT) from None
    vad = _vad_model()
    block = VAD_CHUNK if vad else FRAME
    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=block,
                        device=_input_device(sd)) as mic:
        def read():
            return mic.read(block)[0][:, 0]
        if vad:
            # Lower VAD cutoff catches quieter speech; 2 blocks (~64 ms) start it, with
            # 200 ms of preroll to retain the beginning of the utterance.
            return _collect(read, _SpeechGate(vad), block, max_seconds, max_silence,
                            wait_seconds, min_voiced, onset=2, preroll=6)
        floor_rms = [float(np.sqrt(np.mean(read() ** 2)))
                     for _ in range(int(calibrate * RATE / block))]
        threshold = speech_threshold(floor_rms)
        # 3 frames (90 ms) of continuous sound start an utterance; min_voiced filters blips.
        return _collect(read, lambda b: float(np.sqrt(np.mean(b ** 2))) > threshold, block,
                        max_seconds, max_silence, wait_seconds, min_voiced, onset=3)


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
        self._save(data)

    def learn(self):
        """Add the last verified clip: called once the transcript also starts with 'Jarvis',
        which is strong evidence it was David talking to JARVIS."""
        if self.last_embedding is not None and self.path.is_file():
            self._add(json.loads(self.path.read_text(encoding="utf-8")), self.last_embedding)
            self.last_embedding = None

    def _save(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

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
            data = json.loads(self.path.read_text(encoding="utf-8"))
            scores = [self.score(self._embed(load(p)), data["profile"]) for p in others]
            result["impostor_max"] = round(max(scores), 3)
            result["safe"] = max(scores) < data["threshold"]
        return result

    def verify(self, audio, threshold=None):
        """(accepted, score). Confident matches join the profile so it adapts to the
        real mic and room; borderline ones never do."""
        _np()  # fail early with the install hint if numpy is missing
        if not self.path.is_file():
            raise RuntimeError("No voiceprint yet. Run: python -m jarvis voice enroll")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("model") != self.model.name:
            raise RuntimeError("Voiceprint was made with a different speaker model. "
                               "Run: jarvis voice recalibrate")
        profile = data.get("profile") or [data["embedding"]]
        embedding = self._embed(audio)
        score = self.score(embedding, profile)
        limit = data["threshold"] if threshold is None else threshold
        self.last_embedding = embedding if score >= limit else None
        if score >= data["threshold"] + self.ADAPT_MARGIN and "profile" in data:
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


class Transcriber:
    def __init__(self, config=None):
        cfg = (config or {}).get("voice", {})
        self.model_name = cfg.get("stt_model", "large-v3-turbo")
        self.language = cfg.get("language", "en")
        self._model, self.device = None, None

    def __call__(self, audio):
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
                                             beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()
