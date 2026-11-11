"""The voice listener frees its speech models when idle and loads them when start.sh asks."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import daemon
from jarvis.listen import Parakeet


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class IdleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.stt, self.ears, self.clock = mock.MagicMock(), mock.MagicMock(), Clock()
        self.stt.release.return_value = True
        for p in (mock.patch("jarvis.speech.release_kokoro"), mock.patch("jarvis.listen.give_back_memory"),
                  mock.patch.object(daemon, "log")):
            p.start()
            self.addCleanup(p.stop)

    def idle(self, **voice):
        return daemon.Idle(self.state, {"voice": voice}, self.stt, self.ears, clock=self.clock)

    def test_unloads_once_after_the_quiet_period(self):
        idle = self.idle(idle_unload=600)
        self.clock.t = 599
        self.assertIsNone(idle.tick())
        self.clock.t = 600
        self.assertEqual(idle.tick(), "unloaded")
        self.stt.release.assert_called_once()
        self.ears.release.assert_called_once()
        self.clock.t = 2000
        self.assertIsNone(idle.tick())  # already free: nothing more to do

    def test_speech_resets_the_timer_and_marks_models_in_use(self):
        idle = self.idle(idle_unload=600)
        self.clock.t = 600
        idle.tick()
        self.clock.t = 900
        idle.heard()  # the next utterance loads what it needs
        self.clock.t = 1400
        self.assertIsNone(idle.tick())
        self.clock.t = 1500
        self.assertEqual(idle.tick(), "unloaded")

    def test_zero_turns_unloading_off(self):
        idle = self.idle(idle_unload=0)
        self.clock.t = 10 ** 6
        self.assertIsNone(idle.tick())
        self.stt.release.assert_not_called()

    def test_a_parakeet_load_in_progress_is_retried_next_tick(self):
        self.stt.release.return_value = False
        idle = self.idle(idle_unload=10)
        self.clock.t = 10
        self.assertIsNone(idle.tick())
        self.stt.release.return_value = True
        self.clock.t += daemon.IDLE_TICK  # retried one idle tick later
        self.assertEqual(idle.tick(), "unloaded")

    def test_failed_unload_waits_before_retrying(self):
        self.stt.release.return_value = False  # Parakeet mid-load
        idle = self.idle(idle_unload=10)
        self.clock.t = 10
        idle.tick()
        idle.tick()  # many empty clips a second: no gc/malloc_trim storm
        self.assertEqual(self.stt.release.call_count, 1)
        self.clock.t = 10 + daemon.IDLE_TICK
        self.stt.release.return_value = True
        self.assertEqual(idle.tick(), "unloaded")

    def test_start_sh_warm_request_preloads(self):
        idle = self.idle(idle_unload=600)
        self.assertIsNone(idle.tick())  # an old request isn't new
        path = daemon.request_warm(self.state)
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual(idle.tick(), "warmed")
        self.ears.preload.assert_called_once()
        self.stt.preload.assert_called_once()
        self.assertIsNone(idle.tick())  # each request once

    def test_voice_start_on_a_running_listener_asks_for_warm(self):
        with mock.patch.object(daemon, "_pid", return_value=123), \
                mock.patch.object(daemon, "status", return_value={"listening": True}):
            out = daemon.start(self.state)
        self.assertTrue(out["warming"] and out["listening"])
        self.assertTrue((self.state / "voice" / daemon.WARM_REQUEST).is_file())


class LevelWatchTests(unittest.TestCase):
    """The mic-level file is written 10x/s only while a console reads it."""

    def test_console_read_marks_the_level_watched(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            path = daemon.level_file(tmp)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.assertFalse(daemon.watched(path))
            daemon.level(tmp)
            self.assertTrue(daemon.watched(path))
            self.assertFalse(daemon.watched(path, now=time.time() + daemon.LEVEL_WATCHED + 1))

    def test_unwatched_rate_still_counts_as_live(self):
        self.assertLess(daemon.LEVEL_IDLE_EVERY, daemon.LEVEL_STALE)


class StatusTailTests(unittest.TestCase):
    def test_long_log_drops_the_cut_first_line(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(daemon, "_pid", return_value=None):
            logs = Path(tmp) / "logs"
            logs.mkdir()
            body = "".join(f"2026-11-07 10:00:{i % 60:02d}  line {i} " + "x" * 80 + "\n" for i in range(2000))
            (logs / "voice.log").write_text(body + "2026-11-07 11:00:00  last line\n", encoding="utf-8")
            recent = daemon.status(tmp)["recent"]
            self.assertEqual(recent[-1], "2026-11-07 11:00:00  last line")
            self.assertTrue(all(line.startswith("2026-11-07") for line in recent))


class ParakeetReleaseTests(unittest.TestCase):
    def test_release_frees_a_loaded_model(self):
        p = Parakeet()
        p._model = object()
        self.assertTrue(p.release())
        self.assertIsNone(p._model)

    def test_release_waits_while_loading(self):
        p = Parakeet()
        p._model, p._loading = object(), object()
        self.assertFalse(p.release())
        self.assertIsNotNone(p._model)


if __name__ == "__main__":
    unittest.main()
