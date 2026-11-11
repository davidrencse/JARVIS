"""The task ledger indexes events by task so claim retries and task views stay fast."""

import sqlite3
import tempfile
import unittest

from jarvis.store import Store


class IndexTests(unittest.TestCase):
    def test_events_have_a_task_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            with sqlite3.connect(store.path) as db:
                names = {r[1] for r in db.execute("PRAGMA index_list(events)")}
            self.assertIn("events_task", names)


if __name__ == "__main__":
    unittest.main()
