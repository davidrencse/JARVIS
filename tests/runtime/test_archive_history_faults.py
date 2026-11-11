"""History capture failure is visible without turning an executed action into a retry."""

import tempfile
import time
import unittest
from unittest.mock import Mock

from jarvis.history import History
from jarvis.pipeline import _record
from jarvis.second_brain_queue import ArchiveCaptureError, configure


class HistoryDurabilityFault(unittest.TestCase):
    def record(self, history, count=1):
        command = {"id": "c1", "device": "main", "intent": "open_app", "args": {"name": "Spotify"}}
        outcome = {"id": "c1", "state": "acknowledged", "ok": True, "text": "Opened Spotify"}
        result = {"reply": "Opened Spotify.", "outcomes": [dict(outcome) for _ in range(count)]}
        _record({"history": history, "parse_ms": 0, "request": "r1", "text": "open spotify",
                 "origin": "main", "origin_class": "owner"}, [command] * count, result, time.monotonic())
        return result

    def test_fault_is_visible_and_successful_action_outcomes_are_preserved(self):
        history = Mock()
        history.record.side_effect = ArchiveCaptureError("private implementation details")
        result = self.record(history, count=2)
        self.assertEqual(history.record.call_count, 2)
        self.assertEqual(result["recording"], {"ok": False, "failed": 2})
        self.assertTrue(all(row["ok"] for row in result["outcomes"]))
        self.assertEqual(result["reply"].count("Command history could not be saved"), 1)
        self.assertNotIn("private implementation", str(result))

    def test_real_failed_outbox_enqueue_rolls_back_history_and_reports_fault(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertTrue(configure(state)["ok"])
            history = History(state)
            with history.connect() as db:
                db.execute("CREATE TRIGGER reject_archive BEFORE INSERT ON archive_outbox "
                           "BEGIN SELECT RAISE(ABORT, 'rejected'); END")
            result = self.record(history)
            self.assertFalse(result["recording"]["ok"])
            self.assertTrue(result["outcomes"][0]["ok"])
            with history.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 0)
