import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jarvis import selftest

LOG = """[2026-10-21T14:10:00Z INFO  lan_mouse] using config
[2026-10-21T14:10:05Z WARN  input_capture::windows::event_thread] e: no available capacity
continuation line without a timestamp
[2026-10-21T14:12:00Z INFO  lan_mouse::service] entering client 0 ...
[2026-10-21T14:12:01Z WARN  lan_mouse::capture] releasing capture: client did not acknowledge Enter within 2.5s
"""


class SelftestLogicTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        Path(self.dir.name, "daemon.log").write_text(LOG, encoding="utf-8")
        patcher = patch.object(selftest, "LM_DIR", Path(self.dir.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_log_lines_since_keeps_untimestamped_continuations(self):
        since = datetime(2026, 10, 21, 14, 10, 5, tzinfo=timezone.utc)
        lines = selftest.log_lines(since)
        self.assertEqual(len(lines), 4)
        self.assertIn("continuation line without a timestamp", lines)
        later = selftest.log_lines(datetime(2026, 10, 21, 14, 12, 0, tzinfo=timezone.utc))
        self.assertTrue(all("no available capacity" not in l for l in later))

    def test_queue_verdicts(self):
        self.assertEqual(selftest.check_queue_log([])["flag"], "PASS")
        self.assertEqual(selftest.check_queue_log(["e: no available capacity"])["flag"], "WARN")
        dropped = selftest.check_queue_log(["keyboard event dropped: capture queue stayed full"])
        self.assertEqual(dropped["flag"], "FAIL")

    def test_wait_log_sees_guard_release(self):
        since = datetime(2026, 10, 21, 14, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(selftest.wait_log(r"did not acknowledge Enter", since, 0.2))
        self.assertFalse(selftest.wait_log(r"acknowledged the connection", since, 0.2))

    def test_late_ack_after_guard_is_not_an_answer(self):
        late = LOG + ("[2026-10-21T14:12:01Z INFO  lan_mouse::capture] sending Leave event to client 0\n"
                      "[2026-10-21T14:12:01Z INFO  lan_mouse::capture] client 0 acknowledged the connection!\n")
        Path(self.dir.name, "daemon.log").write_text(late, encoding="utf-8")
        since = datetime(2026, 10, 21, 14, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(selftest.wait_log(r"acknowledged the connection", since, 0.2))
        self.assertTrue(selftest.guard_released(since))
        self.assertFalse(selftest.guard_released(datetime(2026, 10, 21, 14, 13, 0, tzinfo=timezone.utc)))

    def test_render_counts_flags(self):
        text = selftest.render([selftest.result("a", "PASS", "ok"),
                                selftest.result("b", "FAIL", "bad"),
                                selftest.result("c", "SKIP", "n/a")])
        self.assertIn("[FAIL] b", text)
        self.assertTrue(text.endswith("1 pass, 0 warn, 1 fail, 1 skipped"))

    def test_markers_match_the_rust_log_messages(self):
        src = Path(r"C:\Users\jdoe\src\lan-mouse")
        if not src.is_dir():
            self.skipTest("lan-mouse source not on this machine")
        capture = (src / "src" / "capture.rs").read_text(encoding="utf-8")
        hook = (src / "input-capture" / "src" / "windows" / "event_thread.rs").read_text(encoding="utf-8")
        self.assertIn(selftest.ACK_MARKER.decode(), capture)
        self.assertIn(selftest.FIX_MARKER.decode(), hook)


if __name__ == "__main__":
    unittest.main()
