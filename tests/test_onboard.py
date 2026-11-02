import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
except ImportError:  # the voice tests need numpy, like the listener itself
    np = None


class FakeMic:
    """Alternates a second of room noise with a second of 'speech' (a loud tone)."""

    def __init__(self, silent=False):
        from jarvis.listen import FRAME, RATE
        self.size, self.n, self.silent, self.closed, self.level = FRAME, 0, silent, False, 0.0
        self.per_second = RATE // FRAME
        self.rng = np.random.default_rng(0)

    def read(self):
        self.n += 1
        if self.silent:
            return np.zeros(self.size, dtype="float32")
        if (self.n // self.per_second) % 2:
            block = 0.1 * np.sin(np.arange(self.size) / 3.0)
        else:
            block = self.rng.normal(scale=0.002, size=self.size)
        return block.astype("float32")

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeDaemon:
    def __init__(self, listening, root):
        self.pid, self.calls, self.root = (4242 if listening else None), [], Path(root)

    def _pid(self, state):
        return self.pid

    def stop(self, state):
        self.calls.append("stop")
        self.pid = None

    def start(self, state):
        self.calls.append("start")
        self.pid = 4243
        return {"started": True}

    def autostart(self, state, enable):
        self.calls.append(f"autostart:{enable}")
        path = self._startup_file()
        path.write_text("x") if enable else path.unlink(missing_ok=True)

    def _startup_file(self):
        return self.root / "autostart.unit"


def fake_embed(self, audio):
    vector = np.ones(16, dtype="float32") + np.float32(len(audio) % 7) * 0.001
    return vector / np.linalg.norm(vector)


@unittest.skipIf(np is None, "numpy not installed")
class VoiceSetupTest(unittest.TestCase):
    def run_setup(self, state, mic, listening=True, cancel_after=None):
        from jarvis.onboard import VoiceSetup
        said, daemon = [], FakeDaemon(listening, state)

        def say(line):
            said.append(line)
            if cancel_after and len(said) == cancel_after:
                setup.cancel()
        setup = VoiceSetup(state, {}, say=say, open_mic=lambda tuning: mic, daemon=daemon)
        with mock.patch("jarvis.listen._vad_model", return_value=None), \
                mock.patch("jarvis.listen.VoicePrint._embed", fake_embed):
            setup.start()
            setup._thread.join(60)
        self.assertFalse(setup._thread.is_alive())
        return setup, said, daemon

    def test_full_run_enrolls_tunes_the_mic_and_restarts_the_listener(self):
        from jarvis.listen import ENROLL_PHRASES
        with tempfile.TemporaryDirectory() as d:
            mic = FakeMic()
            setup, said, daemon = self.run_setup(d, mic)
            view = setup.status()
            self.assertEqual(view["step"], "done", view.get("error"))
            self.assertTrue(view["enrolled"])
            self.assertTrue(view["result"]["test_passed"])
            self.assertTrue(view["result"]["listening"])
            self.assertEqual(len(view["clips"]), len(ENROLL_PHRASES))
            voice = Path(d) / "voice"
            self.assertEqual(len(list((voice / "enroll").glob("*.wav"))), len(ENROLL_PHRASES))
            self.assertFalse((voice / "enroll.new").exists())
            tuned = json.loads((voice / "mic.json").read_text())
            self.assertGreater(tuned["snr_db"], 20)  # a 0.1 tone over 0.002 noise
            # paused, set to start at login, then back with the new print
            self.assertEqual(daemon.calls, ["stop", "autostart:True", "start"])
            self.assertTrue(view["autostart"] or setup.status()["autostart"])
            self.assertTrue(mic.closed)
            self.assertIn("JARVIS", said[0])

    def test_mic_only_tunes_the_mic_and_keeps_the_voiceprint(self):
        # 2026-11-01: voice status said mic_tuned false; the full setup re-enrolls for that.
        from jarvis.onboard import MIC_DONE, VoiceSetup
        with tempfile.TemporaryDirectory() as d:
            self.run_setup(d, FakeMic())  # an enrolled voice first
            voice = Path(d) / "voice"
            (voice / "mic.json").unlink()
            before = (voice / "voiceprint.json").read_bytes()
            clips = sorted(p.name for p in (voice / "enroll").iterdir())
            said, daemon = [], FakeDaemon(True, d)
            setup = VoiceSetup(d, {}, say=said.append, open_mic=lambda tuning: FakeMic(), daemon=daemon)
            with mock.patch("jarvis.listen._vad_model", return_value=None), \
                    mock.patch("jarvis.listen.VoicePrint._embed", fake_embed):
                setup.start(mic_only=True)
                setup._thread.join(60)
            view = setup.status()
            self.assertEqual(view["step"], "done", view.get("error"))
            self.assertTrue(view["result"]["test_passed"])
            self.assertEqual(view["result"]["voiceprint"], "kept")
            self.assertTrue((voice / "mic.json").is_file())
            self.assertEqual((voice / "voiceprint.json").read_bytes(), before)  # never learned from
            self.assertEqual(sorted(p.name for p in (voice / "enroll").iterdir()), clips)
            self.assertEqual(said[-1], MIC_DONE)
            self.assertNotIn("teach me your voice", " ".join(said))
            self.assertEqual(daemon.calls, ["stop", "start"])

    def test_cancel_keeps_the_old_voiceprint_and_clips(self):
        with tempfile.TemporaryDirectory() as d:
            voice = Path(d) / "voice"
            (voice / "enroll").mkdir(parents=True)
            (voice / "enroll" / "01.wav").write_bytes(b"old")
            (voice / "voiceprint.json").write_text('{"old": true}')
            setup, _, daemon = self.run_setup(d, FakeMic(), cancel_after=3)  # during enrollment
            self.assertEqual(setup.status()["step"], "cancelled")
            self.assertEqual((voice / "voiceprint.json").read_text(), '{"old": true}')
            self.assertEqual((voice / "enroll" / "01.wav").read_bytes(), b"old")
            self.assertFalse((voice / "enroll.new").exists())
            self.assertEqual(daemon.calls, ["stop", "start"])  # it was on before, so it's on again

    def test_cancel_after_the_voiceprint_is_made_still_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            voice = Path(d) / "voice"
            (voice / "enroll").mkdir(parents=True)
            (voice / "enroll" / "01.wav").write_bytes(b"old")
            (voice / "voiceprint.json").write_text('{"old": true}')
            (voice / "mic.json").write_text('{"device": "old mic"}')
            # intro, level, enroll, then the test prompt: cancelled after the print is written
            setup, _, daemon = self.run_setup(d, FakeMic(), cancel_after=4)
            self.assertEqual(setup.status()["step"], "cancelled")
            self.assertEqual((voice / "voiceprint.json").read_text(), '{"old": true}')
            self.assertEqual((voice / "mic.json").read_text(), '{"device": "old mic"}')
            self.assertEqual([p.name for p in (voice / "enroll").iterdir()], ["01.wav"])
            self.assertFalse((voice / "setup.old").exists())
            self.assertEqual(daemon.calls, ["stop", "start"])

    def test_a_first_run_cancelled_leaves_no_mic_settings(self):
        with tempfile.TemporaryDirectory() as d:
            setup, _, _ = self.run_setup(d, FakeMic(), listening=False, cancel_after=3)
            self.assertEqual(setup.status()["step"], "cancelled")
            voice = Path(d) / "voice"
            for name in ("mic.json", "voiceprint.json", "enroll", "setup.old"):
                self.assertFalse((voice / name).exists(), name)

    def test_a_silent_mic_is_named_and_nothing_starts(self):
        with tempfile.TemporaryDirectory() as d:
            setup, _, daemon = self.run_setup(d, FakeMic(silent=True), listening=False)
            view = setup.status()
            self.assertEqual(view["step"], "error")
            self.assertIn("pure silence", view["error"])
            self.assertFalse(view["enrolled"])
            self.assertEqual(daemon.calls, [])  # it wasn't listening and setup didn't finish

    def test_terminal_run_prints_each_line_and_reports(self):
        import io
        from jarvis.listen import ENROLL_PHRASES
        from jarvis.onboard import VoiceSetup, run_terminal
        with tempfile.TemporaryDirectory() as d:
            mic, daemon = FakeMic(), FakeDaemon(False, d)
            setup = VoiceSetup(d, {}, say=lambda line: None, open_mic=lambda t: mic, daemon=daemon)
            out = io.StringIO()
            with mock.patch("jarvis.listen._vad_model", return_value=None), \
                    mock.patch("jarvis.listen.VoicePrint._embed", fake_embed):
                report = run_terminal(d, {}, out=out, poll=0.01, setup=setup)
            self.assertEqual(report["step"], "done")
            self.assertTrue(report["test_passed"] and report["listening"] and report["autostart"])
            text = out.getvalue()
            for phrase in ENROLL_PHRASES:
                self.assertIn(phrase, text)
            self.assertIn(f"[{len(ENROLL_PHRASES)}/{len(ENROLL_PHRASES)}]", text)

    def test_one_run_at_a_time_and_autostart_toggle(self):
        from jarvis.onboard import VoiceSetup
        with tempfile.TemporaryDirectory() as d:
            daemon = FakeDaemon(False, d)
            setup = VoiceSetup(d, {}, daemon=daemon)
            setup.view["running"] = True
            with self.assertRaises(RuntimeError):
                setup.start()
            setup.view["running"] = False
            self.assertTrue(setup.autostart(True)["autostart"])
            self.assertFalse(setup.autostart(False)["autostart"])


@unittest.skipIf(np is None, "numpy not installed")
class SpeechGateTest(unittest.TestCase):
    def gate(self, scores, **kw):
        from jarvis.listen import _SpeechGate
        it = iter(scores)
        return _SpeechGate(lambda audio: np.array([next(it)]), **kw)

    def test_hysteresis_starts_high_and_holds_low(self):
        block = np.zeros(512, dtype="float32")
        scores = [0.3, 0.5, 0.6, 0.25, 0.2, 0.1, 0.25]
        g = self.gate(scores, start=0.4, hold=0.18)
        got = [g(block) for _ in scores]
        # 0.3 alone isn't speech; two blocks over 0.4 engage it, 0.25/0.2 hold, 0.1 lets go
        # and a later 0.25 needs the full start score again.
        self.assertEqual(got, [False, True, True, True, True, False, False])

    def test_a_single_click_never_engages_the_hold(self):
        block = np.zeros(512, dtype="float32")
        g = self.gate([0.9, 0.3, 0.3], start=0.4, hold=0.18)
        self.assertEqual([g(block) for _ in range(3)], [True, False, False])

    def test_streamed_scores_match_scoring_the_whole_clip(self):
        from jarvis.listen import _SpeechGate, _vad_model
        model = _vad_model()
        if model is None:
            self.skipTest("faster-whisper's Silero VAD not installed")
        t = np.arange(16000 * 2) / 16000
        audio = (0.3 * np.sin(2 * np.pi * 220 * t) * (t > 0.7)).astype("float32")
        audio = audio[:len(audio) // 512 * 512]
        g = _SpeechGate(model)
        streamed = []
        for block in audio.reshape(-1, 512):
            g(block)
            streamed.append(g.last)
        whole = model(audio.copy()).reshape(-1)
        # faster-whisper zeroes the final block's last 64 samples before scoring it (its
        # context slice is a view), so the whole-clip score for that one block is off.
        self.assertLess(float(np.abs(np.array(streamed) - whole)[:-1].max()), 1e-5)

    def test_fit_cutoffs_follow_the_voice_but_stay_above_the_room(self):
        from jarvis.listen import START_CUTOFF, fit_cutoffs
        loud = fit_cutoffs([0.01] * 50, [0.97] * 50)
        self.assertEqual(loud[0], START_CUTOFF)  # a clear voice keeps the default
        soft = fit_cutoffs([0.01] * 50, [0.35] * 50)
        self.assertLess(soft[0], START_CUTOFF)  # a quiet voice lowers the start bar
        noisy = fit_cutoffs([0.5] * 50, [0.35] * 50)
        self.assertGreater(noisy[0], 0.5)  # but never under the room's own score
        for start, hold in (loud, soft, noisy):
            self.assertLess(hold, start)


if __name__ == "__main__":
    unittest.main()
