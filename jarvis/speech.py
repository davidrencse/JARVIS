"""Spoken replies. Engines: ElevenLabs (`voice.engine: "elevenlabs"`, falls back to
Kokoro) or Kokoro alone, a local 82M-parameter voice model — free, no network.

Kokoro on the Dell's CPU (measured 2026-10-20): ~0.25 s for "On it.", ~0.7 s for a
typical sentence, about 4-5x faster than real time. The GPU build of onnxruntime and
the int8 model were no faster, so it runs fp32 on CPU and leaves the GPU to Whisper.
SpeechQueue synthesizes sentence N+1 while sentence N plays, so only the first
sentence's synthesis is ever waited on. Every phrase is cached as WAV; playback
uses winsound (Windows) or pw-play/aplay (Linux), no extra packages.
"""

import hashlib
import json
import os
import queue
import re
import threading
import time
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path
from .fsio import read_json

# Fallback if the key can't list voices: ElevenLabs' premade "Daniel" (British, calm).
DEFAULT_VOICE = "onwK4e9ZLuTAKqWW03F9"

DEFAULTS = {"engine": "kokoro", "kokoro_voice": "bm_george", "kokoro_speed": 1.0,
            "model": "eleven_flash_v2_5", "monthly_char_budget": 30000,
            "max_spoken_chars": 350, "sample_rate": 22050}
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
BRITISH_MALE = ("bm_george", "bm_lewis", "bm_daniel", "bm_fable")
_kokoro, _kokoro_lock = None, threading.Lock()


def kokoro_model(state_dir):
    """Load Kokoro once per process (~1 s)."""
    global _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            try:
                from kokoro_onnx import Kokoro
            except ImportError:
                raise RuntimeError("Local voice needs: pip install kokoro-onnx") from None
            folder = Path(state_dir).resolve() / "voice" / "kokoro"
            missing = [f for f in KOKORO_FILES if not (folder / f).is_file()]
            if missing:
                raise RuntimeError(f"Kokoro files missing in {folder}: download "
                                   + ", ".join(KOKORO_URL + f for f in missing))
            _kokoro = Kokoro(*(str(folder / f) for f in KOKORO_FILES))
        return _kokoro


def release_kokoro():
    """Drop the local voice model; kokoro_model() loads it again when next needed."""
    global _kokoro
    with _kokoro_lock:
        _kokoro = None


def write_wav(path, pcm, rate):
    partial = Path(path).with_suffix(".part")
    with wave.open(str(partial), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    partial.replace(path)


def spoken(text, limit):
    """Strip markdown/code and keep whole sentences up to `limit` characters."""
    text = re.sub(r"```.*?```", " I put the code in the transcript. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    return out or text[:limit].rsplit(" ", 1)[0] + "…"


NODE_MUTED = "muted: JARVIS only talks on the main PC"


def may_speak(config):
    """JARVIS talks out loud only on the main PC (David, 2026-11-07). A node stays silent:
    its replies still show as text. voice.nodes_speak: true lets a node talk again.
    A config that can't say which computer this is counts as the main (never mute the main)."""
    if ((config or {}).get("voice") or {}).get("nodes_speak") is True:
        return True
    try:
        from .topology import is_node
        return not is_node(config or {})
    except (ValueError, KeyError, TypeError):
        return True


class Voice:
    def __init__(self, state_dir, config=None):
        cfg = {**DEFAULTS, **(config or {}).get("voice", {})}
        self.muted = not may_speak(config)
        self.engine = cfg["engine"]
        self.kokoro_voice, self.speed = cfg["kokoro_voice"], float(cfg["kokoro_speed"])
        self.state = state_dir
        self.model = os.environ.get("ELEVENLABS_MODEL_ID") or cfg["model"]
        self.budget = int(cfg["monthly_char_budget"])
        self.limit = int(cfg["max_spoken_chars"])
        self.rate = int(cfg["sample_rate"])
        self.cache = Path(state_dir).resolve() / "audio" / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.usage_path = Path(state_dir).resolve() / "voice-usage.json"
        self.secrets = Path(state_dir).resolve() / "secrets.json"
        self.last_error = None

    def credential(self, name):
        """Environment variable first, then .jarvis/secrets.json (written by voice setup)."""
        if os.environ.get(name):
            return os.environ[name]
        try:
            return json.loads(self.secrets.read_text(encoding="utf-8")).get(name)
        except (OSError, ValueError):
            return None

    def voice_settings(self):
        """Speed and stability picked in the console, shared with the live agent; only the
        ones that were set, so ElevenLabs keeps the voice's own defaults otherwise."""
        out = {}
        for key, name, (lo, hi) in (("speed", "ELEVENLABS_VOICE_SPEED", (0.7, 1.2)),
                                    ("stability", "ELEVENLABS_VOICE_STABILITY", (0.0, 1.0))):
            try:
                value = float(self.credential(name))
            except (TypeError, ValueError):
                continue
            if lo <= value <= hi:
                out[key] = value
        return out

    def usage(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        data = read_json(self.usage_path, {})
        return month, data

    def _charge(self, chars):
        month, data = self.usage()
        data[month] = data.get(month, 0) + chars
        self.usage_path.write_text(json.dumps(data), encoding="utf-8")

    def remaining(self):
        month, data = self.usage()
        return self.budget - data.get(month, 0)

    def synth(self, text):
        """(WAV path, source) for `text`; path is None when it can't be voiced.
        ElevenLabs falls back to local Kokoro (no key yet, over budget, offline, out of
        credits), so a reply is never lost or read by the robotic system voice."""
        if self.engine == "kokoro":
            return self._kokoro(text)
        try:
            path, source = self._elevenlabs(text)
        except Exception as exc:
            path, source = None, "elevenlabs-error"
            self.last_error = str(exc)
        if path:
            return path, source
        try:
            return self._kokoro(text)[0], f"kokoro ({source})"
        except Exception:  # Kokoro isn't installed on this laptop either
            return None, source

    def _kokoro(self, text):
        digest = hashlib.sha256(f"kokoro|{self.kokoro_voice}|{self.speed}|{text}".encode())
        path = self.cache / f"k{digest.hexdigest()[:24]}.wav"
        if path.is_file():
            return path, "cache"
        lang = "en-gb" if self.kokoro_voice[:1] == "b" else "en-us"
        samples, rate = kokoro_model(self.state).create(text, voice=self.kokoro_voice,
                                                        speed=self.speed, lang=lang)
        import numpy as np
        write_wav(path, (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes(), rate)
        return path, "kokoro"

    def _elevenlabs(self, text):
        key = self.credential("ELEVENLABS_API_KEY")
        voice = self.credential("ELEVENLABS_VOICE_ID")
        if not key or not voice or not re.fullmatch(r"[A-Za-z0-9_-]+", voice):
            return None, "no-credentials"
        settings = self.voice_settings()
        # Untuned clips keep their old cache key, so the cached ones still count.
        tag = "".join(f"{k}={v}|" for k, v in sorted(settings.items()))
        digest = hashlib.sha256(f"{tag}{voice}|{self.model}|{self.rate}|{text}".encode()).hexdigest()
        path = self.cache / f"{digest[:24]}.wav"
        if path.is_file():
            return path, "cache"
        if len(text) > self.remaining():
            return None, "over-budget"
        request = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_{self.rate}",
            data=json.dumps({"text": text, "model_id": self.model,
                             **({"voice_settings": settings} if settings else {})}).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                pcm = response.read(25_000_000)
        except urllib.error.HTTPError as exc:
            exc.close()  # the error object holds the response; drop its socket now
            raise RuntimeError(f"ElevenLabs returned HTTP {exc.code}; check key, voice and "
                               "credits.") from None
        if not pcm or len(pcm) % 2:
            raise RuntimeError("ElevenLabs returned empty or malformed PCM audio.")
        write_wav(path, pcm, self.rate)
        self._charge(len(text))
        return path, "elevenlabs"

    def say(self, text, play=True, wait=True):
        if self.muted:  # a node: no synthesis (no ElevenLabs characters spent), no sound
            return {"spoken": "", "source": NODE_MUTED}
        line = spoken(text, self.limit)
        if not line:
            return {"spoken": "", "source": "none"}
        path, source = self.synth(line)
        if play:
            play_wav(path, wait) if path else local_tts(line)
        result = {"spoken": line, "source": source if path else f"local ({source})"}
        if self.engine == "elevenlabs":
            result["chars_left_this_month"] = self.remaining()
        return result


class SpeechQueue:
    """Two-stage pipeline: synthesize the next sentence while the current one plays."""

    def __init__(self, voice):
        self.voice, self.texts, self.wavs = voice, queue.Queue(), queue.Queue()
        threading.Thread(target=self._synth, daemon=True).start()
        threading.Thread(target=self._play, daemon=True).start()

    def put(self, text):
        if getattr(self.voice, "muted", False):  # a node never talks
            return
        line = spoken(text, 10_000)
        if line:
            self.texts.put(line)

    def _synth(self):
        while True:
            text = self.texts.get()
            try:
                path, _ = self.voice.synth(text)
            except Exception:
                path = None
            self.wavs.put((path, text))
            self.texts.task_done()

    def _play(self):
        while True:
            path, text = self.wavs.get()
            try:
                play_wav(path) if path else local_tts(text)
            except Exception:
                pass
            finally:
                self.wavs.task_done()

    def drain(self, timeout=None):
        """Block until everything queued has been spoken, or `timeout` seconds pass (a
        stalled synthesis must not hang the listener). True when all of it was spoken."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for pending in (self.texts, self.wavs):
            with pending.all_tasks_done:
                while pending.unfinished_tasks:
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    pending.all_tasks_done.wait(left)
        return True

    def clear(self, timeout=None):
        """Drop what hasn't started playing, then wait (up to `timeout`) for the line
        playing now."""
        for pending in (self.texts, self.wavs):
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break
                pending.task_done()
        return self.drain(timeout)


def list_voices(key):
    """Voices the key can use, British/male first (closest to a JARVIS feel)."""
    request = urllib.request.Request("https://api.elevenlabs.io/v1/voices",
                                     headers={"xi-api-key": key, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        voices = json.loads(response.read(5_000_000)).get("voices", [])

    def label(v):
        labels = v.get("labels") or {}
        return " ".join(str(labels[k]) for k in ("gender", "accent", "description", "age")
                        if labels.get(k))

    def jarvisness(v):
        about = v["about"].lower()
        return ("british" not in about, "male" not in about or "female" in about, v["name"])

    return sorted(({"voice_id": v["voice_id"], "name": v.get("name", "?"), "about": label(v)}
                   for v in voices if v.get("voice_id")), key=jarvisness)


def save_secrets(state_dir, **values):
    """Merge values into .jarvis/secrets.json, readable only by the owner."""
    path = Path(state_dir).resolve() / "secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_json(path, {})
    data.update(values)
    from .credits import write_private
    write_private(path, json.dumps(data, indent=2))
    return path


def setup(state_dir, config=None, ask=input, secret=None):
    """Paste the key once (hidden), pick a voice by number, hear a test line."""
    import getpass
    secret = secret or getpass.getpass
    key = secret("Paste your ElevenLabs API key (hidden), then Enter: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", key):
        raise ValueError("That doesn't look like an ElevenLabs key (starts with sk_). Try again.")
    try:
        voices = list_voices(key)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            print("Key works for speech only (no 'voices' permission); using the default "
                  "British voice 'Daniel'.")
            voices = []
        else:
            raise RuntimeError(f"ElevenLabs returned HTTP {exc.code} for that key.") from None
    choice = DEFAULT_VOICE
    if voices:
        shown = voices[:15]
        for i, v in enumerate(shown, 1):
            print(f"  {i:>2}. {v['name']:<18} {v['about']}")
        pick = ask("Pick a voice number [1]: ").strip() or "1"
        if not pick.isdigit() or not 1 <= int(pick) <= len(shown):
            raise ValueError("Pick one of the listed numbers.")
        choice = shown[int(pick) - 1]["voice_id"]
    path = save_secrets(state_dir, ELEVENLABS_API_KEY=key, ELEVENLABS_VOICE_ID=choice)
    result = Voice(state_dir, config).say("JARVIS online. Good to hear you, David.")
    return {"saved": str(path), "voice_id": choice, "test": result["source"]}


_playing = None  # the one unwaited player from a wait=False call


def play_wav(path, wait=True):
    """wait=False returns immediately; the next play_wav call cuts it off."""
    global _playing
    if sys.platform == "win32":
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | (0 if wait else winsound.SND_ASYNC))
        return
    if _playing is not None:  # stop the previous async clip and reap it, as on Windows
        if _playing.poll() is None:
            _playing.terminate()
        try:
            _playing.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _playing.kill()
        _playing = None
    for player in ("pw-play", "paplay", "aplay"):
        if shutil.which(player):
            proc = subprocess.Popen([player, str(path)])
            if wait:
                try:
                    proc.wait(timeout=120)
                except BaseException:  # timeout or interrupt: don't leave it sounding
                    proc.kill()
                    proc.wait()
                    raise
            else:
                _playing = proc
            return
    raise RuntimeError("No audio player found (install pipewire or alsa-utils).")


def local_tts(text):
    """Free fallback voice. Text goes through stdin, never the command line."""
    if sys.platform == "win32":
        script = ("Add-Type -AssemblyName System.Speech; "
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$s.Speak([Console]::In.ReadToEnd())")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       input=text, text=True, check=False, timeout=120)
        return
    for tool in ("espeak-ng", "espeak"):
        if shutil.which(tool):
            subprocess.run([tool, "--stdin"], input=text, text=True, check=False, timeout=120)
            return
    print(text)
