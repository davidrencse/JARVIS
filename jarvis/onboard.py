"""Voice setup: JARVIS talks David through a mic check and enrollment, start to finish.

Runs in the console server, on the same mic path the always-on listener uses (PortAudio,
16 kHz, no browser processing), so the voiceprint matches what the listener hears. Steps:

  quiet   two seconds of room: a mic that sends pure silence is caught here
  level   "Hey Jarvis, can you hear me?": loudness and Silero scores fit the speech
          cutoffs to this mic and room (.jarvis/voice/mic.json)
  enroll  ENROLL_PHRASES, read off the screen; clips land in enroll.new/ and replace the
          old ones only when every phrase is in
  test    one more phrase scored against the new voiceprint
  done    the listener restarts with the new print and mic settings, and starts at
          login from now on (the last screen's switch turns that off)

The listener is paused for the whole run so it doesn't answer the enrollment phrases.
A run that doesn't reach "done" (cancelled or failed at any step, even the test phrase)
puts back the mic settings, voiceprint and clips it started with.
"""

import shutil
import threading
import time
from pathlib import Path

INTRO = ("Hi, I'm JARVIS. Let's teach me your voice. It takes about a minute. "
         "First, stay quiet for a moment while I listen to the room.")
LEVEL = "Now say: Hey Jarvis, can you hear me?"
ENROLL = "Good. Now read each line on the screen, the way you'd normally talk to me."
TEST = "Last one. Say: Jarvis, what time is it?"
DONE = "All set. Just say Jarvis whenever you need me."
LEVEL_PHRASE, TEST_PHRASE = "Hey Jarvis, can you hear me?", "Jarvis, what time is it?"
QUIET_SECONDS, ATTEMPTS = 2.0, 3
KEPT = ("mic.json", "voiceprint.json", "enroll")  # what a run that doesn't finish puts back
SILENT = 1e-5  # RMS of a mic that is open but delivers nothing (a DSP mic read raw)
QUIET_VOICE = 0.006  # speech RMS below this transcribes poorly: ask for more input gain


class Cancelled(Exception):
    pass


class VoiceSetup:
    """One setup run at a time. `status()` is what the page polls; the work runs in a
    thread. `say`, `open_mic` and `daemon` are injectable so tests never touch audio."""

    def __init__(self, state, config, say=None, open_mic=None, daemon=None):
        self.state, self.config = Path(state), config
        self._say, self._open_mic, self._daemon = say, open_mic, daemon
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread, self._mic = None, None
        self.on_change = None  # called with each new view (the terminal prints from it)
        self.view = self._idle()

    def _idle(self):
        return {"running": False, "step": "idle", "message": "", "error": "", "warning": "",
                "phrase": "", "index": 0, "total": 0, "attempt": 0, "clips": [],
                "result": None, "device": None}

    # ---- what the page sees ----------------------------------------------------------
    def status(self):
        from .listen import mic_settings
        daemon = self._daemon_mod()
        voice = self.state / "voice"
        with self._lock:
            view = dict(self.view, clips=list(self.view["clips"]))
        mic = self._mic
        view["level"] = round(getattr(mic, "level", 0.0), 4) if view["running"] else 0.0
        view["enrolled"] = (voice / "voiceprint.json").is_file()
        view["mic"] = mic_settings(self.state)
        view["listening"] = daemon._pid(self.state) is not None
        try:
            view["autostart"] = daemon._startup_file().is_file()
        except Exception:
            view["autostart"] = None
        return view

    def devices(self):
        from .listen import input_devices
        return input_devices()

    # ---- control ---------------------------------------------------------------------
    def start(self, device=None):
        """device: a mic name, "" for the system default, None to keep the saved choice."""
        with self._lock:
            if self.view["running"]:
                raise RuntimeError("Voice setup is already running.")
            self._cancel.clear()
            self.view = {**self._idle(), "running": True, "step": "starting",
                         "device": device or None, "message": "Getting the microphone ready"}
            self._thread = threading.Thread(target=self._run, args=(device,),
                                            name="voice-setup", daemon=True)
            self._thread.start()
        return self.status()

    def autostart(self, enable):
        """Start the listener at login (the last screen offers it)."""
        self._daemon_mod().autostart(self.state, bool(enable))
        return self.status()

    def cancel(self):
        self._cancel.set()
        return self.status()

    # ---- the run -----------------------------------------------------------------------
    def _set(self, **changes):
        with self._lock:
            self.view.update(changes)
            view = dict(self.view)
        if self.on_change:
            self.on_change(view)

    def _daemon_mod(self):
        if self._daemon is None:
            from . import daemon
            self._daemon = daemon
        return self._daemon

    def _speak(self, text, mic):
        """JARVIS says it, then the mic forgets what it heard meanwhile (its own voice)."""
        self._set(message=text)
        if self._say is None:
            from .speech import Voice
            voice = Voice(self.state, self.config)
            self._say = lambda line: voice.say(line, wait=True)
        try:
            self._say(text)
        except Exception:
            pass  # no speaker or TTS: the same words are on screen
        self._check()
        mic.flush()

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    def _run(self, device):
        daemon = self._daemon_mod()
        was_listening = daemon._pid(self.state) is not None
        finished, mic = False, None
        kept = self._keep()
        try:
            if was_listening:
                self._set(message="Pausing the always-on listener")
                daemon.stop(self.state)
                for _ in range(20):  # it must let go of the voice lock and stop answering
                    if daemon._pid(self.state) is None:
                        break
                    time.sleep(0.25)
            from .listen import mic_settings
            tuning = mic_settings(self.state)
            if device is not None:  # no choice given (`jarvis voice enroll`): keep the saved mic
                tuning["device"] = device or None
            mic = self._mic = self._open(tuning)
            self._speak(INTRO, mic)
            noise_rms, noise_probs = self._quiet(mic)
            tuning.update(self._level(mic, tuning, noise_rms, noise_probs))
            clips = self._enroll(mic, tuning)
            result = self._finish(mic, tuning, clips)
            finished = True
            self._set(step="done", result=result, message=DONE, phrase="")
        except Cancelled:
            self._set(step="cancelled", message="Setup cancelled. Nothing "
                      "was changed.", phrase="")
        except Exception as exc:
            self._set(step="error", error=str(exc), phrase="")
        finally:
            self._mic = None
            if mic is not None:
                try:
                    mic.close()
                except Exception:
                    pass
            shutil.rmtree(self.state / "voice" / "enroll.new", ignore_errors=True)
            try:  # before the listener restarts, so it never loads a half-made setup
                self._put_back(kept, restore=not finished)
            except Exception as exc:
                self._set(warning=f"Couldn't put the old voice setup back: {exc}")
            if finished:
                try:  # a listener that dies with a reboot isn't "set up": start it at login too
                    if not daemon._startup_file().is_file():
                        daemon.autostart(self.state, True)
                except Exception as exc:
                    self._set(warning=f"Couldn't set the listener to start at login: {exc}")
            if was_listening or finished:
                try:
                    started = daemon.start(self.state)
                    if finished:
                        with self._lock:
                            if self.view.get("result") is not None:
                                self.view["result"]["listening"] = bool(
                                    started.get("started") or started.get("listening"))
                except Exception as exc:
                    self._set(warning=f"The listener didn't restart: {exc}")
            self._set(running=False)  # only now: the page and terminal read the final state

    def _keep(self):
        """Copy what this run may overwrite into voice/setup.old/; returns that folder."""
        voice, kept = self.state / "voice", self.state / "voice" / "setup.old"
        shutil.rmtree(kept, ignore_errors=True)
        kept.mkdir(parents=True)
        for name in KEPT:
            src = voice / name
            if src.is_dir():
                shutil.copytree(src, kept / name)
            elif src.is_file():
                shutil.copy2(src, kept / name)
        return kept

    def _put_back(self, kept, restore):
        voice = self.state / "voice"
        if restore:
            for name in KEPT:
                now, old = voice / name, kept / name
                if now.is_dir():
                    shutil.rmtree(now)
                elif now.exists():
                    now.unlink()
                if old.exists():
                    shutil.move(str(old), str(now))
        shutil.rmtree(kept, ignore_errors=True)

    def _open(self, tuning):
        if self._open_mic is not None:
            return self._open_mic(tuning)
        from .listen import LiveMic
        return LiveMic(tuning=tuning)

    def _read(self, mic):
        def read():
            self._check()
            return mic.read()
        return read

    def _gate(self, start, hold, scores):
        """Silero gate that also keeps each block's score, or None without Silero."""
        from .listen import _SpeechGate, _vad_model
        vad = _vad_model()
        if not vad:
            return None
        gate = _SpeechGate(vad, start=start, hold=hold)

        def check(block):
            speech = gate(block)
            scores.append(gate.last)
            return speech
        return check

    def _quiet(self, mic):
        from .listen import RATE, _SpeechGate, _np, _vad_model
        np = _np()
        self._set(step="quiet", message="Stay quiet for a moment")
        read, blocks = self._read(mic), []
        vad = _vad_model()
        gate, probs = (_SpeechGate(vad) if vad else None), []
        while sum(len(b) for b in blocks) < QUIET_SECONDS * RATE:
            block = read()
            blocks.append(block)
            if gate:
                gate(block)
                probs.append(gate.last)
        rms = [float(np.sqrt(np.mean(b ** 2))) for b in blocks]
        if max(rms) < SILENT:
            raise RuntimeError("This microphone is sending pure silence. Pick another one, "
                               "or check it isn't muted in the system sound settings.")
        return float(np.median(rms)), probs

    def _record(self, mic, tuning, phrase, scores=None, max_seconds=12, min_voiced=0.5):
        """One phrase, with a few tries. None after ATTEMPTS misses."""
        from .listen import HOLD_CUTOFF, START_CUTOFF, record_utterance
        for attempt in range(1, ATTEMPTS + 1):
            self._check()
            self._set(phrase=phrase, attempt=attempt)
            gate = None
            if scores is not None:
                scores.clear()
                gate = self._gate(tuning.get("vad_start", START_CUTOFF),
                                  tuning.get("vad_hold", HOLD_CUTOFF), scores)
            audio = record_utterance(max_seconds=max_seconds, min_voiced=min_voiced,
                                     wait_seconds=15, mic=_Reader(self._read(mic)),
                                     tuning=tuning, gate=gate)
            if audio is not None:
                return audio
            self._set(warning="Didn't catch that. Say it again, a little louder or closer.")
        return None

    def _level(self, mic, tuning, noise_rms, noise_probs):
        from .listen import _np, fit_cutoffs, save_mic_settings
        np = _np()
        self._speak(LEVEL, mic)
        self._set(step="level", message="Say the line below", warning="")
        scores = []
        # Permissive while measuring: the whole point is to find out how this voice scores.
        audio = self._record(mic, {**tuning, "vad_start": 0.2, "vad_hold": 0.1},
                             LEVEL_PHRASE, scores, min_voiced=0.4)
        if audio is None:
            raise RuntimeError("I couldn't hear you. Check the right microphone is picked "
                               "and its input volume is up, then try again.")
        frames = audio[:len(audio) // 512 * 512].reshape(-1, 512)
        loud = np.sqrt(np.mean(frames ** 2, axis=1))
        speech_rms = float(np.percentile(loud, 80)) if len(loud) else 0.0
        fitted = {"device": tuning.get("device"), "noise_rms": round(noise_rms, 5),
                  "speech_rms": round(speech_rms, 5),
                  "snr_db": round(20 * np.log10(max(speech_rms, 1e-6) / max(noise_rms, 1e-6)), 1),
                  "measured": time.strftime("%Y-%m-%d %H:%M")}
        if scores:
            fitted["vad_start"], fitted["vad_hold"] = fit_cutoffs(noise_probs, scores)
        save_mic_settings(self.state, fitted)
        warning = ""
        if speech_rms < QUIET_VOICE:
            warning = "Your voice is coming through quietly. Raising the mic's input volume helps."
        elif fitted["snr_db"] < 12:
            warning = "The room is noisy for this mic. Moving closer or cutting background sound helps."
        self._set(warning=warning, mic_check=fitted)
        return fitted

    def _enroll(self, mic, tuning):
        from .listen import ENROLL_PHRASES, RATE, save_wav
        self._speak(ENROLL, mic)
        folder = self.state / "voice" / "enroll.new"
        shutil.rmtree(folder, ignore_errors=True)
        clips = []
        self._set(step="enroll", total=len(ENROLL_PHRASES), warning="")
        for i, phrase in enumerate(ENROLL_PHRASES, 1):
            self._set(index=i, message="Read this line")
            audio = self._record(mic, tuning, phrase)
            if audio is None:
                raise RuntimeError("I couldn't hear enough speech for that line. Check the "
                                   "microphone and try again.")
            save_wav(folder / f"{i:02d}.wav", audio)
            clips.append(audio)
            with self._lock:
                self.view["clips"].append({"index": i, "seconds": round(len(audio) / RATE, 1)})
                self.view["warning"] = ""
        return clips

    def _finish(self, mic, tuning, clips):
        from .listen import VoicePrint
        self._set(step="learning", message="Learning your voice", phrase="")
        ears = VoicePrint(self.state, self.config)
        result = ears.enroll(clips)
        final = self.state / "voice" / "enroll"
        shutil.rmtree(final, ignore_errors=True)
        shutil.move(str(self.state / "voice" / "enroll.new"), str(final))
        self._speak(TEST, mic)
        self._set(step="test", message="Say the line below")
        audio = self._record(mic, tuning, TEST_PHRASE)
        if audio is not None:
            ok, score = ears.verify(audio)
            result.update(test_score=score, test_passed=bool(ok))
        else:
            result.update(test_score=None, test_passed=None)
        self._speak(DONE if result.get("test_passed") is not False else
                    "Done. That last one was a weak match; the listener will keep learning "
                    "your voice as you use it.", mic)
        return result


class _Reader:
    """Hands record_utterance a mic-like object whose reads stop when setup is cancelled."""

    def __init__(self, read):
        self.read = read


def run_terminal(state, config, device=None, out=None, poll=0.1, setup=None):
    """`jarvis voice enroll`: the same guided setup in a terminal, so it needs no console
    and no browser. JARVIS speaks each cue; the line to read is printed large, with a live
    mic meter. Ctrl+C cancels and leaves the old voiceprint as it was."""
    import sys
    out = out or sys.stderr
    setup = setup or VoiceSetup(state, config)
    lock, shown = threading.Lock(), {"step": None, "phrase": None, "warning": None}

    def show(view):  # runs on setup's thread at every change, so no line is ever skipped
        with lock:
            lines = []
            if view["phrase"] and (view["phrase"], view["index"]) != shown["phrase"]:
                count = f"[{view['index']}/{view['total']}]  " if view["step"] == "enroll" else ""
                lines.append(f"  {count}Say:  \"{view['phrase']}\"")
                shown["phrase"] = (view["phrase"], view["index"])
            elif view["message"] and view["step"] != shown["step"] and not view["phrase"]:
                lines.append(f"  {view['message']}")
            shown["step"] = view["step"]
            if view["warning"] and view["warning"] != shown["warning"]:
                lines.append(f"  ! {view['warning']}")
            shown["warning"] = view["warning"]
            if lines:
                out.write("\r" + " " * 40 + "\r\n" + "\n".join(lines) + "\n")
                out.flush()
    setup.on_change = show
    setup.start(device)
    try:
        while True:
            view = setup.status()
            if not view["running"]:
                break
            bars = int(min(1.0, (view["level"] * 14) ** 0.6) * 24)
            with lock:
                out.write("\r  mic " + "█" * bars + "·" * (24 - bars))
                out.flush()
            time.sleep(poll)
    except KeyboardInterrupt:
        setup.cancel()
        setup._thread.join(30)
        view = setup.status()
    out.write("\n")
    if view["step"] == "error":
        raise RuntimeError(view["error"])
    result = view.get("result") or {}
    return {"step": view["step"], "listening": view["listening"], "autostart": view["autostart"],
            **{k: result[k] for k in ("threshold", "test_score", "test_passed") if k in result},
            **({"mic": view["mic"]} if view["step"] == "done" else {}),
            **({"warning": view["warning"]} if view["warning"] else {})}
