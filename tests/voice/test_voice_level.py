"""The listener's live mic level for the console's wave (daemon.level_file/post_level/level)."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from jarvis import daemon
from jarvis.listen import LiveMic


class _Stream:
    def start(self): pass
    def stop(self): pass
    def close(self): pass


class LevelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_off_until_posted_then_live_then_stale(self):
        self.assertEqual(daemon.level(self.tmp.name), {"live": False})
        path = daemon.level_file(self.tmp.name)
        self.assertTrue(str(path).startswith(self.tmp.name))  # tmpfs, not the state folder
        now = time.time()
        daemon.post_level(path, 0.0312, now=now)
        got = daemon.level(self.tmp.name, now=now + 0.2)
        self.assertTrue(got["live"])
        self.assertAlmostEqual(got["peak"], 0.0312)
        self.assertEqual(daemon.level(self.tmp.name, now=now + daemon.LEVEL_STALE + 1),
                         {"live": False})

    def test_garbage_file_reads_as_off(self):
        daemon.level_file(self.tmp.name).write_text("{not json")
        self.assertEqual(daemon.level(self.tmp.name), {"live": False})

    def test_no_runtime_dir_uses_state_folder(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            path = daemon.level_file(self.tmp.name)
        self.assertEqual(path.parent.name, "voice")

    def test_mic_peak_is_the_loudest_block_then_resets(self):
        import numpy as np
        mic = LiveMic(stream=_Stream())
        for amp in (0.01, 0.2, 0.05):
            mic._callback(np.full((mic.block, 1), amp, dtype="float32"), mic.block, None, None)
        self.assertAlmostEqual(mic.take_peak(), 0.2, places=5)
        self.assertEqual(mic.take_peak(), 0.0)


if __name__ == "__main__":
    unittest.main()
