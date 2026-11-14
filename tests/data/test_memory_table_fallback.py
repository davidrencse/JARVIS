"""Old SQLite returns an empty cursor for unknown PRAGMAs, not an exception."""
import sqlite3
import unittest
from types import SimpleNamespace
from jarvis.brainsync import _content_tables


class OldSQLiteTables(unittest.TestCase):
    def test_unknown_pragma_falls_back_and_retains_content_table(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE memories(body TEXT)')
        db.execute("INSERT INTO memories VALUES ('must be retained')")
        db.execute('CREATE VIRTUAL TABLE notes USING fts5(body)')
        class OldConnection:
            def execute(self,sql):
                if sql == 'PRAGMA main.table_list':
                    return SimpleNamespace(fetchall=lambda: [])
                return db.execute(sql)
        tables=_content_tables(OldConnection())
        self.assertIn('memories',tables)
        self.assertIn('notes',tables)
        self.assertNotIn('notes_data',tables)
        self.assertNotIn('notes_config',tables)
