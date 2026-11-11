"""Mic open at login (listen.open_mic), a dead stream (MicLost) and the listener's window
and log rules (daemon.follow_up_ok, may_rearm, ignored_line). Fakes only: no real mic."""
import unittest
import unittest.mock

import numpy as np

from jarvis import daemon, listen


class PortAudioError(Exception):
    pass


class FakeSD:
    def __init__(self):
        self.inits = 0

    def _terminate(self):
        pass

    def _initialize(self):
        self.inits += 1

    def query_devices(self, device=None, kind=None):
        return {"name": "pipewire", "hostapi": 0, "default_samplerate": 48000.0,
                "max_input_channels": 2}

    def query_hostapis(self, index):
        return {"name": "ALSA"}


class OpenMicTests(unittest.TestCase):
    def run_open(self, fails, wait=90, **kw):
        sd, lines, sleeps, made = FakeSD(), [], [], []

        def factory():
            if len(made) < fails:
                made.append(None)
                raise PortAudioError("Error opening InputStream: Unanticipated host error "
                                     "[PaErrorCode -9999]: 'Unknown error' [ALSA error -2]")
            mic = type("Mic", (), {"device": None})()
            made.append(mic)
            return mic
        mic = listen.open_mic({}, log=lines.append, wait=wait, sleep=sleeps.append, sd=sd,
                              factory=factory, **kw)
        return mic, sd, lines, sleeps, made

    def test_waits_for_pipewire_quietly(self):
        mic, sd, lines, sleeps, made = self.run_open(fails=2)
        self.assertIs(mic, made[-1])
        self.assertEqual(len(lines), 2)  # one "waiting", one "microphone: ..."
        self.assertTrue(lines[0].startswith("waiting for the microphone (PipeWire not ready)"))
        self.assertIn("pipewire (ALSA, 48000 Hz, 2 ch) after 2 tries", lines[1])
        self.assertEqual(sd.inits, 2)  # PortAudio re-read its devices between tries
        self.assertEqual(sleeps, [1, 1])

    def test_missing_mic_is_reported_once_then_polled_slowly(self):
        told = []
        mic, sd, lines, sleeps, _ = self.run_open(fails=4, wait=0, on_missing=lambda: told.append(1))
        self.assertEqual(told, [1])
        self.assertEqual(sleeps, [30, 30, 30, 30])
        self.assertEqual(sum("still missing" in l for l in lines), 1)

    def test_fresh_reinitialises_before_the_first_try(self):
        _, sd, lines, sleeps, _ = self.run_open(fails=0, fresh=True)
        self.assertEqual((sd.inits, sleeps, len(lines)), (1, [], 1))

    def test_missing_package_still_raises(self):
        def factory():
            raise RuntimeError(listen.INSTALL_HINT)
        with self.assertRaises(RuntimeError):
            listen.open_mic({}, log=lambda _: None, sleep=lambda _: None, sd=FakeSD(), factory=factory)

    def test_reinit_without_private_api_is_harmless(self):
        self.assertFalse(listen._reinit_portaudio(object()))

    def test_a_silent_stream_raises_mic_lost(self):
        class Stream:
            def start(self):
                pass

            def close(self):
                pass
        mic = listen.LiveMic(stream=Stream())
        mic.blocks.get = lambda timeout: (_ for _ in ()).throw(mic._queue.Empty())
        with self.assertRaises(listen.MicLost):
            mic.read()
        self.assertTrue(issubclass(listen.MicLost, RuntimeError))  # old handlers still catch it

    def test_a_stream_that_fails_to_start_is_closed(self):
        closed = []

        class Stream:
            def start(self):
                raise PortAudioError("no target node available")

            def close(self):
                closed.append(1)
        with self.assertRaises(PortAudioError):
            listen.LiveMic(stream=Stream())
        self.assertEqual(closed, [1])


class WindowTests(unittest.TestCase):
    def test_follow_up_must_pass_the_voice_check_itself(self):
        self.assertTrue(daemon.follow_up_ok("Spotify", began=1.0, armed=5.0, is_owner=True))
        self.assertFalse(daemon.follow_up_ok("Spotify", began=1.0, armed=5.0, is_owner=False))
        self.assertFalse(daemon.follow_up_ok("Spotify", began=6.0, armed=5.0, is_owner=True))
        self.assertFalse(daemon.follow_up_ok("  ", began=1.0, armed=5.0, is_owner=True))

    def test_windows_never_chain(self):
        # "Jarvis, open up" -> incomplete -> window; "the" (no wake word) -> unclear -> no window
        self.assertTrue(daemon.may_rearm(follow_up=False))
        self.assertFalse(daemon.may_rearm(follow_up=True))


class SpeakerCheckOffTests(unittest.TestCase):
    """voice.speaker_check: false. Nobody is taken for David: every clip is an unscored
    near miss that needs "Jarvis", no window opens and nothing is learned."""

    def test_check_off_never_calls_verify(self):
        def verify(*a):
            raise AssertionError("verify must not run with the check off")
        audio = np.zeros(16000, dtype="float32")
        self.assertEqual(daemon.check_voice(False, verify, audio, 0.4), (False, None))

    def test_check_on_passes_threshold_and_never_learns_inside_verify(self):
        seen = []

        def verify(audio, threshold, learn):
            seen.append((threshold, learn))
            return True, 0.71
        self.assertEqual(daemon.check_voice(True, verify, np.zeros(10, "float32"), 0.4), (True, 0.71))
        self.assertEqual(seen, [(0.4, False)])

    def test_check_off_makes_every_clip_a_near_miss(self):
        is_owner, score = daemon.check_voice(False, None, None, None)
        self.assertTrue(daemon.is_near(is_owner, score, daemon.BORDERLINE))
        # check on: a stranger far below the cutoff is no near miss; the owner never is
        self.assertFalse(daemon.is_near(False, 0.05, daemon.BORDERLINE))
        self.assertFalse(daemon.is_near(True, 0.9, daemon.BORDERLINE))

    def test_check_off_opens_no_follow_up_window(self):
        is_owner, _ = daemon.check_voice(False, None, None, None)
        self.assertFalse(daemon.follow_up_ok("Spotify", began=1.0, armed=5.0, is_owner=is_owner))


class LearnGateTests(unittest.TestCase):
    def test_only_an_owner_clip_that_said_jarvis_teaches(self):
        self.assertTrue(daemon.should_learn(is_owner=True, follow_up=False))
        # A follow-up and a reply-window answer ("Then check it.") both set follow_up=True.
        self.assertFalse(daemon.should_learn(is_owner=True, follow_up=True))
        self.assertFalse(daemon.should_learn(is_owner=False, follow_up=False))  # near miss + "Jarvis"
        is_owner, _ = daemon.check_voice(False, None, None, None)  # speaker check off
        self.assertFalse(daemon.should_learn(is_owner, follow_up=False))


class MicLostTests(unittest.TestCase):
    def test_reopen_closes_quietly_and_asks_for_fresh_portaudio(self):
        closed = []

        class Mic:
            def close(self):
                closed.append(1)
                raise OSError("already gone")
        with unittest.mock.patch.object(daemon, "log") as log:
            self.assertEqual(daemon.reopen_after(listen.MicLost("The mic stream died."), Mic()),
                             (None, True))
        self.assertEqual(closed, [1])
        self.assertIn("Reopening it.", log.call_args[0][0])
        with unittest.mock.patch.object(daemon, "log"):
            self.assertEqual(daemon.reopen_after(listen.MicLost("x"), None), (None, True))


class LogLineTests(unittest.TestCase):
    def test_ignored_line_keeps_its_prefix_and_shows_cutoff_and_level(self):
        audio = np.full(16000, 0.01, dtype="float32")  # -40 dBFS
        line = daemon.ignored_line(0.3596, 0.36, audio, near=True)
        self.assertEqual(line, "ignored another voice (similarity 0.359 < 0.36, -40.0 dBFS, near miss)")
        self.assertTrue(daemon.ignored_line(0.1, 0.36, audio).startswith("ignored another voice"))
        self.assertEqual(daemon.dbfs(np.zeros(10, dtype="float32")), -120.0)


if __name__ == "__main__":
    unittest.main()
