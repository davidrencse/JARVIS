"""A browser ask's own deadline holds even when the SQLite lock delays the approval (audit 2026-11-13)."""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import confirm
from jarvis.confirm import ConfirmQueue


def browser_command(expires_at):
    return {"intent": "browser_action", "device": "dell",
            "args": {"text": "let the browser click Buy",
                     "browser": {"site": "shop.example", "verb": "click", "stakes": "high",
                                 "named": False, "expires_at": expires_at}}}


class BrowserDeadlineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.q = ConfirmQueue(Path(tmp.name))

    def test_approve_after_deadline_while_lock_was_held_is_refused(self):
        item = self.q.park(browser_command(time.time() + 1.0), "dell", "console")
        number = int(item["id"][1:])
        holder = sqlite3.connect(self.q.path, timeout=10, check_same_thread=False)
        holder.isolation_level = None
        holder.execute("BEGIN IMMEDIATE")  # the history DB write lock, held across expires_at
        result = {}

        def approve():
            try:
                result["command"] = self.q.approve(item["id"], "dell")
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(target=approve)
        worker.start()
        time.sleep(1.6)  # past the ask's 1 s deadline, long before its 600 s row window
        holder.execute("COMMIT")
        holder.close()
        worker.join(15)
        self.assertNotIn("command", result, "approve() returned a command after the deadline")
        self.assertIsInstance(result.get("error"), ValueError)
        self.assertIn("expired", str(result["error"]))
        self.assertEqual(self.q.status(number), "expired")

    def test_approve_refuses_a_pending_row_past_its_deadline_without_expire_running(self):
        item = self.q.park(browser_command(time.time() + 0.3), "dell", "console")
        time.sleep(0.5)
        with mock.patch.object(ConfirmQueue, "expire", lambda self: 0):  # as if expire() had run too early
            with self.assertRaises(ValueError):
                self.q.approve(item["id"], "dell")
            with self.assertRaises(ValueError):
                self.q.approve(None, "dell")
        self.assertEqual(self.q.status(int(item["id"][1:])), "expired")

    def test_status_of_an_approval_stamped_after_the_deadline_reads_expired(self):
        item = self.q.park(browser_command(time.time() + 60), "dell", "console")
        number = int(item["id"][1:])
        self.q.approve(item["id"], "dell")
        self.assertEqual(self.q.status(number), "approved")  # in time: still honoured
        with self.q.history.connect() as db:  # a legacy/raced row approved after its deadline
            due = time.time() - 5
            raw = db.execute("SELECT command FROM confirmations WHERE number=?", (number,)).fetchone()["command"]
            raw = raw.replace(raw[raw.index('"expires_at":'):].split(",")[0].split("}")[0],
                              f'"expires_at":{due}')
            db.execute("UPDATE confirmations SET command=?, resolved=? WHERE number=?", (raw, time.time(), number))
        self.assertEqual(self.q.status(number), "expired")

    def test_in_time_approval_still_works_and_expire_takes_now_under_the_lock(self):
        item = self.q.park(browser_command(time.time() + 60), "dell", "console")
        self.assertEqual(self.q.approve(item["id"], "dell")["intent"], "browser_action")
        self.assertEqual(self.q.status(int(item["id"][1:])), "approved")

    def test_expire_reads_the_clock_after_taking_the_lock(self):
        # expire() must not stamp a "now" from before it waited for the write lock.
        item = self.q.park(browser_command(time.time() + 1.0), "dell", "console")
        holder = sqlite3.connect(self.q.path, timeout=10, check_same_thread=False)
        holder.isolation_level = None
        holder.execute("BEGIN IMMEDIATE")
        threading.Timer(1.5, lambda: (holder.execute("COMMIT"), holder.close())).start()
        self.assertEqual(self.q.expire(), 1)  # waited past the deadline, then saw it as due
        self.assertEqual(self.q.status(int(item["id"][1:])), "expired")


if __name__ == "__main__":
    unittest.main()
