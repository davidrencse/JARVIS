"""Hearing: microphone → is it David? → text. Everything runs locally, so it costs nothing.

- Utterance capture: Silero voice activity detection (bundled with faster-whisper) on
  32 ms blocks; without it, a loudness threshold calibrated against the room's noise.
- Speaker check: sherpa-onnx ERes2Net speaker embedding (≈26 MB ONNX model, CPU),
  cosine similarity against an enrolled voiceprint. Runs before transcription so
  other voices never reach the model or the agents.
- Transcription: Parakeet TDT 0.6B v3 (int8 ONNX via onnx-asr, the model Handy ships) in-process
  on CPU first; faster-whisper (RTX 4060 CUDA fp16, CPU int8) is the fallback whenever Parakeet
  is missing, fails, or its words lack the wake word. voice.stt_engine: parakeet | moonshine | whisper.

Optional packages: pip install numpy sounddevice faster-whisper sherpa-onnx onnx-asr
Speaker model: SPEAKER_MODEL_URL below, saved to .jarvis/voice/.
"""

import json
import os
import re
from contextlib import contextmanager
from .platforms import on_windows
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
INSTALL_HINT = "Voice input needs: pip install numpy sounddevice faster-whisper sherpa-onnx onnx-asr"


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
    if on_windows():
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
    if on_windows():
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
        if d["max_input_channels"] < 1 or name in seen or (not on_windows() and "(hw:" in name):
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
        self.device = None
        self.blocks = queue.Queue(maxsize=max(1, int(backlog * RATE / self.block)))
        if stream is None:
            try:
                import sounddevice as sd
            except ImportError:
                raise RuntimeError(INSTALL_HINT) from None
            self.device = _input_device(sd, self.tuning.get("device"))
            stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                                    blocksize=self.block, device=self.device,
                                    callback=self._callback)
        self.stream = stream
        try:
            self.stream.start()
        except Exception:  # never leave a half-open stream behind a PortAudio re-init
            try:
                self.stream.close()
            except Exception:
                pass
            raise

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
            raise MicLost("The microphone stopped sending audio.") from None

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


class MicLost(RuntimeError):
    """The open stream stopped delivering audio (suspend/resume, a source change): reopen
    it, which is no failed request."""


def _reinit_portaudio(sd):
    """PortAudio lists devices once, when it starts: a PipeWire node that appears later
    (WirePlumber still starting at login) stays invisible until it is re-initialised.
    Private sounddevice API, so guarded. Only call it while this process has no stream open."""
    terminate, initialize = getattr(sd, "_terminate", None), getattr(sd, "_initialize", None)
    if not (callable(terminate) and callable(initialize)):
        return False
    try:
        terminate()
        initialize()
        return True
    except Exception:
        return False


@contextmanager
def _quiet_stderr():
    """Mute fd 2 while PortAudio probes the mic: a missing PipeWire node makes ALSA print a
    dozen lines per try. Best effort; our own one-line summary is logged afterwards."""
    import sys
    try:
        sys.stderr.flush()
        saved, null = os.dup(2), os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 2)
    except (OSError, AttributeError, ValueError):
        yield
        return
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(null)


def describe_input(sd, device):
    """The capture device as one log phrase: name, host API, default rate and channels."""
    try:
        info = sd.query_devices(device, "input") if device is not None else sd.query_devices(kind="input")
        api = sd.query_hostapis(info["hostapi"])["name"]
        return (f"{info['name']} ({api}, {int(info['default_samplerate'])} Hz, "
                f"{info['max_input_channels']} ch)")
    except Exception:
        return "default input" if device is None else str(device)


MIC_WAIT = 90  # seconds of "waiting for the microphone" before it is reported missing


def open_mic(tuning=None, log=print, wait=MIC_WAIT, sleep=time.sleep, sd=None, factory=None,
             on_missing=None, fresh=False):
    """A LiveMic, waiting for the mic to exist. At login PipeWire's source can be missing
    for 20-60 s ("no target node available"); that is no failed request, so nothing is
    spoken: one log line, a PortAudio re-init and a retry each second, then every 30 s
    after `wait` seconds (when `on_missing` is told once). `fresh` re-initialises first
    (a reopen after MicLost). Only a missing package raises."""
    if sd is None:
        try:
            import sounddevice as sd
        except ImportError:
            raise RuntimeError(INSTALL_HINT) from None
    factory = factory or (lambda: LiveMic(tuning=tuning))
    if fresh:
        _reinit_portaudio(sd)
    started, tries, missing = time.monotonic(), 0, False
    while True:
        try:
            with _quiet_stderr():
                mic = factory()
        except Exception as exc:
            if INSTALL_HINT in str(exc):
                raise
            tries += 1
            if tries == 1:
                detail = (str(exc).splitlines() or [""])[0][:160]
                log(f"waiting for the microphone (PipeWire not ready): {type(exc).__name__}: {detail}")
            if not missing and time.monotonic() - started >= wait:
                missing = True
                log(f"microphone still missing after {wait}s; checking every 30 s")
                if on_missing:
                    try:
                        on_missing()
                    except Exception:
                        pass
            _reinit_portaudio(sd)
            sleep(30 if missing else 1)
            continue
        after = f" after {tries} tries" if tries else ""
        log(f"microphone: {describe_input(sd, getattr(mic, 'device', None))}{after}")
        return mic


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
    """Enrolled speaker embedding + threshold, stored in .jarvis/voice/voiceprint.json.

    The profile is the enrollment clips ("pinned", the first `clips` entries, never changed)
    followed by clips learned live. Learned clips can only nudge a score (NUDGE) and must
    themselves match the enrollment (ANCHOR): until 2026-11-07 any clip scoring 0.48 joined,
    with no wake word, and 3 learned clips of the TV or a call were enough to accept it.
    voice.adapt: false stops learning; forget_learned() drops what was learned."""

    def __init__(self, state_dir, config=None):
        cfg = (config or {}).get("voice", {})
        self.dir = Path(state_dir).resolve() / "voice"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.model = Path(cfg.get("speaker_model") or self.dir / Path(SPEAKER_MODEL_URL).name)
        self.path = self.dir / "voiceprint.json"
        self.adapt = cfg.get("adapt", True) is not False
        self._extractor = None
        self.last_embedding = None
        self.last_score = self.last_limit = None

    def release(self):
        """Free the speaker model; the next check loads it again (well under a second)."""
        self._extractor = None

    def preload(self):
        self._ensure_extractor()

    def _ensure_extractor(self):
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
        return self._extractor

    def _embed(self, audio):
        np = _np()
        self._ensure_extractor()
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=RATE, waveform=audio)
        stream.input_finished()
        vector = np.array(self._extractor.compute(stream), dtype="float32")
        return vector / (np.linalg.norm(vector) or 1.0)

    # ERes2Net cosine on this mic: other voices <= 0.26, JARVIS's own Kokoro voice <= 0.19,
    # David live (not enrollment) 0.38-0.45. Enrollment clips score higher than live
    # speech, so the learned threshold is capped well below them.
    # Offline (2026-11-07, jarvis.voice_eval): his enrollment clips stay >= 0.465 against
    # each other under 3 dB noise or 0.7 s reverb, so learned clips only need to nudge.
    FLOOR, CEILING, MARGIN, MAX_PROFILE = 0.30, 0.36, 0.10, 40
    NUDGE = 0.10        # learned clips lift a score at most this far above the enrolled match
    ANCHOR = 0.30       # a learned clip must match the enrollment alone at least this well
    LEARN_MARGIN = 0.05  # only turns this far above the threshold teach the profile
    DUPLICATE = 0.90    # a new clip this close to a learned one replaces it (one session,
                        # one voice, can't fill the profile)
    NEG_GAP = 0.05      # calibrate(): the threshold sits at least this far above negatives

    @staticmethod
    def _unit(vector):
        np = _np()
        return vector / (np.linalg.norm(vector) or 1.0)

    @classmethod
    def _match(cls, embedding, profile):
        """Best of: match to the centroid, or mean of the 3 closest clips."""
        np = _np()
        profile = np.asarray(profile, dtype="float32")
        centroid = float(embedding @ cls._unit(profile.mean(axis=0)))
        nearest = np.sort(profile @ embedding)[-3:]
        return max(centroid, float(nearest.mean()))

    @classmethod
    def score(cls, embedding, profile, pinned=0):
        """The match to the profile. With `pinned` (enrollment clips at its front) the
        enrollment match anchors it: learned clips that match the enrollment themselves
        (ANCHOR) can lift it by at most NUDGE, so no voice is accepted on learned clips alone."""
        if not pinned or pinned >= len(profile):
            return cls._match(embedding, profile)
        base = cls._match(embedding, profile[:pinned])
        learned = cls.trusted(profile, pinned)[pinned:]
        if not learned:
            return base
        return max(base, min(cls._match(embedding, learned), base + cls.NUDGE))

    @classmethod
    def trusted(cls, profile, pinned):
        """The profile without learned clips that don't match the enrollment (ANCHOR):
        a voiceprint poisoned before 2026-11-07 is defused without rewriting the file."""
        np = _np()
        if not pinned or pinned >= len(profile):
            return list(profile)
        enrolled = list(profile[:pinned])
        return enrolled + [e for e in profile[pinned:]
                           if cls._match(np.asarray(e, dtype="float32"), enrolled) >= cls.ANCHOR]

    @classmethod
    def calibrate(cls, embeddings, negatives=None, report=None):
        """Leave-one-out: score each clip against the others; threshold sits just below
        the weakest genuine match, clamped to [FLOOR, CEILING]. `negatives` (other voices'
        embeddings) push it up to NEG_GAP above the closest of them, but never above CEILING
        or his own weakest clip minus MARGIN: live David scores 0.38-0.45, so a stricter
        cutoff would lock him out. A negative closer than that is reported, not obeyed
        (2026-11-07 eval: one Kokoro voice at 0.332 asked for 0.382).
        `report` (a dict) is filled with what the choice rests on."""
        np = _np()
        if len(embeddings) < 2:
            raise ValueError("Need at least 2 clips to calibrate.")
        own = [cls.score(e, [o for j, o in enumerate(embeddings) if j != i])
               for i, e in enumerate(embeddings)]
        threshold = min(cls.CEILING, max(cls.FLOOR, min(own) - cls.MARGIN))
        info = {"own_min": round(min(own), 3), "negatives": len(negatives or ())}
        if negatives:
            neg = [cls.score(n, embeddings) for n in negatives]
            need = max(neg) + cls.NEG_GAP
            cap = max(cls.FLOOR, min(cls.CEILING, min(own) - cls.MARGIN))
            if need > threshold:
                threshold = min(need, cap)
            info.update(negative_max=round(max(neg), 3), negatives_too_close=need > cap,
                        false_accept=round(float(np.mean([n >= threshold for n in neg])), 4))
        info.update(threshold=round(threshold, 3),
                    false_reject=round(float(np.mean([o < threshold for o in own])), 4))
        if report is not None:
            report.update(info)
        return cls._unit(np.mean(embeddings, axis=0)), threshold, own

    def _add(self, data, embedding):
        """Append a learned clip, if it matches the enrollment (ANCHOR). Enrollment clips
        are pinned; only learned ones rotate, and a near-copy replaces its twin."""
        np = _np()
        profile = data.get("profile") or [data["embedding"]]
        clips = data.get("clips", 0)
        pinned = profile[:clips]
        if pinned and self._match(embedding, pinned) < self.ANCHOR:
            return False
        learned = list(profile[len(pinned):])
        if learned:
            sims = np.asarray(learned, dtype="float32") @ embedding
            twin = int(np.argmax(sims))
            if sims[twin] >= self.DUPLICATE:
                learned.pop(twin)
        room = max(1, self.MAX_PROFILE - len(pinned))  # [-0:] would keep everything
        learned = (learned + [embedding.tolist()])[-room:]
        data["profile"] = pinned + learned
        data["embedding"] = self._unit(np.mean(data["profile"], axis=0)).tolist()
        self._save(data, keep_threshold=True)
        return True

    def learn(self):
        """Add the last verified clip: called once a turn that said "Jarvis" itself passed
        the full voice check by LEARN_MARGIN, which is strong evidence it was David."""
        embedding, score, limit, self.last_embedding = (
            self.last_embedding, self.last_score, self.last_limit, None)
        if (not self.adapt or embedding is None or not self.path.is_file()
                or score is None or limit is None or score < limit + self.LEARN_MARGIN):
            return False
        return self._add(self._load(), embedding)

    def forget_learned(self):
        """Drop every learned clip; the enrollment stays. Returns how many were dropped."""
        np = _np()
        data = self._load()
        profile, clips = data.get("profile") or [], data.get("clips", 0)
        if not clips or len(profile) <= clips:
            return 0
        data["profile"] = profile[:clips]
        data["embedding"] = self._unit(np.mean(data["profile"], axis=0)).tolist()
        self._save(data, keep_threshold=True)
        return len(profile) - clips

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

    def enroll(self, clips, negatives=None):
        """`negatives`: embeddings of other voices that the threshold must reject."""
        if len(clips) < 3:
            raise ValueError("Enroll with at least 3 clips of your voice.")
        embeddings = [self._embed(c) for c in clips]
        report = {}
        center, threshold, own = self.calibrate(embeddings, negatives, report)
        report["date"] = time.strftime("%Y-%m-%d %H:%M")
        self._save({"embedding": center.tolist(), "profile": [e.tolist() for e in embeddings],
                    "threshold": threshold, "clips": len(clips), "model": self.model.name,
                    "calibration": report})
        return {"threshold": round(threshold, 3), "clips": len(clips),
                "own_scores": [round(s, 3) for s in own], "path": str(self.path),
                "calibration": report}

    def recalibrate(self):
        """Rebuild the voiceprint from saved enrollment clips (e.g. after a model change).
        Learned clips are dropped; the old file is kept as voiceprint.json.prev. Local
        voices in voice/impostors/ are negatives: the cutoff is fitted to reject them."""
        import glob
        import shutil

        clips = [load_wav(p) for p in sorted(glob.glob(str(self.dir / "enroll" / "*.wav")))]
        if len(clips) < 3:
            raise RuntimeError("No saved enrollment clips. Run: jarvis voice enroll")
        others = sorted(glob.glob(str(self.dir / "impostors" / "*.wav")))
        negatives = [self._embed(load_wav(p)) for p in others]
        if self.path.is_file():
            shutil.copy2(self.path, self.path.with_name(self.path.name + ".prev"))
        result = self.enroll(clips, negatives or None)
        if others:
            data = self._load()
            scores = [self.score(e, data["profile"], data.get("clips", 0)) for e in negatives]
            result["impostor_max"] = round(max(scores), 3)
            result["safe"] = max(scores) < data["threshold"]
        return result

    def verify(self, audio, threshold=None, learn=False):
        """(accepted, score). Only scores: the profile learns through learn(), once the
        turn has proved itself (said "Jarvis", passed by LEARN_MARGIN). learn=True is the
        old one-call form: score, then learn() if confident."""
        _np()  # fail early with the install hint if numpy is missing
        if not self.path.is_file():
            raise RuntimeError("No voiceprint yet. Run: python -m jarvis voice enroll")
        data = self._load()
        if data.get("model") != self.model.name:
            raise RuntimeError("Voiceprint was made with a different speaker model. "
                               "Run: jarvis voice recalibrate")
        profile = data.get("profile") or [data["embedding"]]
        embedding = self._embed(audio)
        score = self.score(embedding, profile, data.get("clips", 0) if "profile" in data else 0)
        limit = data["threshold"] if threshold is None else threshold
        self.last_embedding = embedding if score >= limit else None
        self.last_score, self.last_limit = score, data["threshold"]
        accepted = score >= limit
        if learn and accepted:
            self.learn()
        return accepted, round(score, 3)


def load_wav(path):
    """16-bit WAV as float32 mono 16 kHz (resampled linearly if needed)."""
    import wave
    np = _np()
    with wave.open(str(path)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype("float32")
        rate, channels = w.getframerate(), w.getnchannels()
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    pcm /= 32768
    if rate != RATE:
        pcm = np.interp(np.arange(0, len(pcm), rate / RATE), np.arange(len(pcm)),
                        pcm).astype("float32")
    return pcm


def score_text(score, limit):
    """"0.358 < 0.36": a score shown next to its cutoff, truncated so a miss never reads
    as a pass after rounding."""
    import math
    if score is None:
        return "unchecked"
    shown = math.floor(float(score) * 1000) / 1000
    if limit is None:
        return f"{shown:.3f}"
    return f"{shown:.3f} {'>=' if score >= limit else '<'} {limit:g}"


def _expose_cuda_dlls():
    """pip's nvidia-cublas/cudnn wheels put DLLs in site-packages/nvidia/*/bin, which
    Windows doesn't search. Add them so ctranslate2 finds CUDA instead of failing over."""
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


# Parakeet TDT 0.6B v3 languages (ISO 639-1). Other languages go straight to Whisper.
PARAKEET_LANGS = ("bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu", "it",
                  "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru", "uk")


class Parakeet:
    """NVIDIA Parakeet TDT 0.6B v3, int8 ONNX, in-process via onnx-asr (pip: onnx-asr): the
    model Handy ships. ~0.3 s for a 3 s clip on this CPU vs ~1.5 s for small.en (measured
    2026-11-05). Returns (text, mean token logprob or None), or None while it isn't ready.

    It loads in a background thread on first use (the first run downloads ~640 MB), so a
    slow link never blocks the listener: Whisper answers until it's ready. A failed load
    (offline at login) is retried after RETRY seconds; only a missing package is final.
    Threads are capped and don't spin, so it doesn't starve the voice check and TTS."""

    RETRY = 120

    def __init__(self, name="nemo-parakeet-tdt-0.6b-v3", threads=4):
        import threading
        self.name, self.threads, self._lock = name, threads, threading.Lock()
        self._model, self._loading, self._failed_at, self.error, self.missing = (
            None, None, None, None, None)

    def _load(self):
        try:
            import onnx_asr
            import onnxruntime as rt
            opts = rt.SessionOptions()
            opts.intra_op_num_threads = self.threads
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            # No arena: it keeps the peak of the longest clip ever heard (hundreds of MB for
            # a 20 s clip) for the life of the process; plain malloc gives it back.
            opts.enable_cpu_mem_arena = False
            model = onnx_asr.load_model(self.name, quantization="int8",
                                        sess_options=opts).with_timestamps()
        except ImportError as exc:
            self.missing = self.error = exc
        except Exception as exc:  # offline, disk full, corrupt download: try again later
            self.error, self._failed_at = exc, time.monotonic()
        else:
            self._model, self.error = model, None
        finally:
            with self._lock:
                self._loading = None

    def release(self):
        """Drop the model (it reloads on the next ready()). False while a load is running:
        that thread would only put it back."""
        with self._lock:
            if self._loading is not None:
                return False
            self._model = None
            return True

    def ready(self):
        """True once loaded; otherwise starts (or retries) the background load."""
        if self._model is not None:
            return True
        with self._lock:
            if self.missing is not None:
                raise ImportError(f"onnx-asr unavailable: {self.missing}")
            retry_due = self._failed_at is None or time.monotonic() - self._failed_at > self.RETRY
            if self._loading is None and retry_due:
                import threading
                self._loading = threading.Thread(target=self._load, name="parakeet-load",
                                                 daemon=True)
                self._loading.start()
        return False

    def __call__(self, audio):
        import numpy as np
        audio = np.asarray(audio, dtype="float32")
        if len(audio) < RATE // 10:  # before ready(): too short to be worth a load
            return "", None
        if not self.ready():
            return None
        r = self._model.recognize(audio[:60 * RATE], sample_rate=RATE)
        lps = list(getattr(r, "logprobs", None) or [])
        return (r.text or "").strip(), (sum(lps) / len(lps) if lps else None)


_CUDA = None


def _has_cuda():
    """A CUDA GPU for Whisper? Checked once; there large-v3-turbo is fast and better."""
    global _CUDA
    if _CUDA is None:
        try:
            import ctranslate2
            _CUDA = bool(ctranslate2.get_cuda_device_count())
        except Exception:
            _CUDA = False
    return _CUDA


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


# Parakeet's own spellings of "Jarvis" in a noisy room (2026-11-07: David's enrollment clips
# at 9.4 dB SNR, Kokoro voices). Not English words, so they count as the wake word outright.
# Real words it also produced ("drivers", "try this", "Java") only send the clip to Whisper
# (fuzzy_wake), whose words then decide; "Travis" stays a name, never the wake word.
WAKE_VARIANTS = ("jovers", "jarbus", "jovis")


def wake(text, closing=True):
    """assistant.wake_word with Parakeet's variants too: the request, or None.
    closing=False: only "Jarvis, ..." at the start counts (an unverified voice)."""
    from .assistant import wake_word
    try:
        from .assistant import WAKE_WORDS
    except ImportError:  # a stand-in assistant module (tests): its own rule, closing form only
        return wake_word(text or "") if closing else None
    return wake_word(text or "", WAKE_WORDS + WAKE_VARIANTS, closing=closing)


def _edits(a, b):
    """Levenshtein distance."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


_SOUNDS = {"g": "j", "d": "j", "t": "j", "b": "v", "f": "v", "w": "v", "z": "s", "c": "s", "x": "s"}


def _skeleton(word):
    """Consonant outline, by sound: "jarvis", "jarbus", "drivers" -> jrvs / jrvs / jrvrs."""
    w = re.sub(r"[^a-z]", "", word.lower())
    for a, b in (("th", "s"), ("ch", "j"), ("dj", "j"), ("dr", "jr"), ("tr", "jr"), ("ph", "v")):
        w = w.replace(a, b)
    out = []
    for c in w:
        c = _SOUNDS.get(c, c)
        if c not in "aeiouhy" and (not out or out[-1] != c):
            out.append(c)
    return "".join(out)


_WAKE_PREFIXES = ("jav", "jarv", "jerv", "jorv", "jov", "jarb", "jerb")


def fuzzy_wake(text):
    """Could the opening words be a misheard "Jarvis"? ("Okay, Jovers", "Try this stall",
    "Drivers summarized", "Javi."). Only the start counts: that is where the wake word is
    said. Never the wake word by itself, only a reason to ask Whisper."""
    try:
        from .assistant import WAKE_LEAD
    except ImportError:  # a stand-in assistant module (tests): common fillers only
        WAKE_LEAD = r"(?:(?:hey|hi|hello|ok|okay|so|um|uh)[\s,.!?]+)*"
    lead = re.match(r"^\W*" + WAKE_LEAD, text or "", re.I)
    tokens = re.findall(r"[a-z']+", (text or "")[lead.end() if lead else 0:].lower())[:3]
    if not tokens:
        return False
    try:
        from .assistant import WAKE_WORDS as names
    except ImportError:
        names = ("jarvis",)
    if any(t in names or t in WAKE_VARIANTS for t in tokens):
        return True  # "Who Jarvis, what time is it?": the name behind an unknown filler word
    first = re.sub(r"[^a-z]", "", tokens[0])
    if len(first) >= 4 and (first.startswith(_WAKE_PREFIXES) or _edits(first, "jarvis") <= 2):
        return True
    one = _skeleton(first)
    if len(one) >= 3 and one[0] == "j" and _edits(one, "jrvs") <= (2 if one[-1] == "s" else 1):
        return True  # "jovers", "drivers" (2 off, same ending), "driver" (1 off)
    if len(tokens) > 1:  # "jar this", "try this": the name split in two
        two = _skeleton(first + tokens[1])
        return len(two) >= 3 and two[0] == "j" and two[-1] == "s" and _edits(two, "jrvs") <= 1
    return False



class Transcriber:
    def __init__(self, config=None):
        cfg = (config or {}).get("voice", {})
        self.model_name = cfg.get("stt_model", "large-v3-turbo")
        self.language = cfg.get("language", "en")
        # voice.stt_engine "moonshine": try Moonshine first and keep its words only when
        # they carry the wake word; anything else (no "Jarvis", noise, a failure) is
        # transcribed by Whisper as before, so a misheard wake word is never lost.
        # "parakeet" (default): Parakeet v3 first, same rule. "whisper": no fast engine.
        self.engine = cfg.get("stt_engine", "parakeet")
        lang = self.language.lower()
        self.fast = None
        if self.engine == "parakeet" and lang.split("-")[0] in PARAKEET_LANGS:
            self.fast = Parakeet()
        elif self.engine == "moonshine" and lang.startswith("en"):
            self.fast = Moonshine(cfg.get("moonshine_model", "base"))
        self._model, self.device = None, None
        self._stats = {}
        self.last_engine = None
        self.need_wake = True  # see _try_fast
        # A fuzzy "Jarvis" from the fast engine went to Whisper: did Whisper hear the wake
        # word? (None: no second opinion was asked.) Counted for the log and the eval.
        self.fuzzy = None
        self.rescued = self.second_opinions = 0

    def __call__(self, audio):
        # On a CUDA node Whisper is fast already: never load (or download) Parakeet there.
        if isinstance(self.fast, Parakeet) and _has_cuda():
            self.fast = None
        # A str is a file path (knowledge.py): Whisper reads files, the fast engines don't.
        self.fuzzy, hint = None, False
        if self.fast is not None and not isinstance(audio, str) and self.device != "cuda":
            text = self._try_fast(audio)
            if text is not None:
                return text
            hint = self._hint
        self.last_engine = "whisper"
        text = self._whisper(audio)
        if hint:
            self.second_opinions += 1
            self.fuzzy = wake(text) is not None
            self.rescued += self.fuzzy
        return text

    def _try_fast(self, audio):
        """The fast engine's words, else None (Whisper decides). The engine returns str
        (Moonshine), (text, logprob) (Parakeet), or None (Parakeet still loading).
        With `need_wake` (the default: the caller needs "Jarvis" in it) words without the
        wake word go to Whisper too, so a misheard "Jarvis" ("Jovers") is never lost.
        A caller that needs no wake word (push-to-talk, a follow-up window) sets it False
        and gets Parakeet's words when they look sure, without a second transcription.
        Words that only sound like "Jarvis" (fuzzy_wake: "Drivers", "Try this") go to
        Whisper whatever the caller needs: Parakeet stays confident when it mishears it."""
        self._hint = False
        try:
            out = self.fast(audio)
        except ImportError:
            self.fast = None  # missing package: Whisper only from now on
            return None
        except RuntimeError as exc:
            if INSTALL_HINT in str(exc):
                self.fast = None
            return None
        except Exception:
            return None  # one bad clip; Parakeet stays on for the next
        if out is None:
            return None
        text, logprob = out if isinstance(out, tuple) else (out, None)
        engine = "parakeet" if isinstance(out, tuple) else "moonshine"
        if not plausible(text, len(audio) / RATE):
            return None
        stats = {"logprob": logprob, "no_speech": None, "engine": engine}
        if wake(text) is None:
            if fuzzy_wake(text):
                self._hint = True
                return None
            if self.need_wake or engine != "parakeet" or unsure(stats):
                return None
        # No log probabilities from Moonshine: unsure() sees None and lets it through.
        self.last = stats
        self._remember(audio, self.last)
        self.last_engine = engine
        return text

    def _remember(self, audio, stats):
        self._stats[id(audio)] = stats
        while len(self._stats) > 8:
            self._stats.pop(next(iter(self._stats)))

    def release(self):
        """Free Whisper and the fast engine; both load again on the next clip. True when
        everything was freed (False: Parakeet was mid-load and keeps loading)."""
        self._model, self.device = None, None
        self._stats.clear()
        fast = getattr(self.fast, "release", None)
        return fast() if fast else True

    def loaded(self):
        return self._model is not None or getattr(self.fast, "_model", None) is not None

    def preload(self):
        """Load before the first request (start.sh): Parakeet in its background thread,
        Whisper (the backup and wake-word second opinion) here."""
        ready = getattr(self.fast, "ready", None)
        if ready:
            try:
                ready()
            except ImportError:
                pass
        self._load_whisper()

    def _load_whisper(self):
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

    def _whisper(self, audio):
        self._load_whisper()
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
# Parakeet's mean token logprob is on a different scale (it is rarely below -0.5 on clean speech).
# Measured 2026-11-05 on the 7 enrollment clips in .jarvis/voice (clean, 1.5-3.8 s): min -0.49
# (a 2-word "Jarvis stop."), median -0.11, max -0.04; none flagged. No garbled clips were
# available, so this sits well below the clean minimum: only clearly guessed text trips it.
PARAKEET_UNSURE_LOGPROB = -0.9


def unsure(stats):
    """True when a transcript's confidence (Transcriber.last) says it was probably misheard."""
    if not isinstance(stats, dict) or stats.get("logprob") is None:
        return False
    if stats.get("engine") == "parakeet":
        return stats["logprob"] < PARAKEET_UNSURE_LOGPROB
    logprob, silence = stats["logprob"], stats.get("no_speech") or 0.0
    return logprob < UNSURE_LOGPROB or (silence > UNSURE_NO_SPEECH and logprob < -0.6)


def give_back_memory():
    """After dropping models: collect, then ask glibc to hand freed heap back to the OS
    (otherwise RSS stays high though nothing uses it)."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):  # not glibc (Windows, macOS, musl)
        pass
