"""The handoff journal locks with msvcrt on Windows and releases by closing."""

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import platforms
from jarvis.continuity.runtime import _HandoffJournal


class JournalLockOnWindows(unittest.TestCase):
    def test_windows_path_uses_msvcrt_and_closes_without_unlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            calls = []

            def locking(fd, mode, nbytes):
                self.assertGreaterEqual(os.fstat(fd).st_size, 1)
                calls.append((fd, mode, nbytes))
                if len(calls) == 1:
                    raise OSError(errno.EACCES, "busy")

            fake = mock.Mock()
            fake.LK_LOCK = 1
            fake.LK_UNLCK = 2
            fake.locking.side_effect = locking
            with mock.patch.object(platforms, "_os", return_value="windows"), \
                    mock.patch.dict(sys.modules, {"msvcrt": fake}):
                journal = _HandoffJournal(path)
                self.assertTrue(journal.bind({"transition_id": "t1", "candidate": "dell",
                                              "action": "handover"}, 1, 1))
                self.assertEqual(journal.claim("brain"), ("claimed", None))
            self.assertEqual({mode for _, mode, _ in calls}, {fake.LK_LOCK})
            self.assertEqual({n for _, _, n in calls}, {1})
            with self.assertRaises(OSError):
                os.fstat(calls[-1][0])
            again = _HandoffJournal(path)
            self.assertEqual(again.step("brain"), {"phase": "started", "ok": None})

    def test_posix_lock_still_serialises(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = _HandoffJournal(Path(tmp) / "journal.json")
            self.assertTrue(journal.bind({"transition_id": "t2", "candidate": "dell"}, 1, 1))
            self.assertEqual(journal.claim("brain"), ("claimed", None))
            self.assertEqual(_HandoffJournal(journal.path).claim("brain"), ("started", None))


if __name__ == "__main__":
    unittest.main()
