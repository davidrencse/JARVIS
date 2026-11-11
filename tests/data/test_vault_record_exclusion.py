"""Generated personal archive revisions are separate from authored vault knowledge."""

import tempfile
import unittest
from pathlib import Path

from jarvis.vault import Vault


class ArchiveIndexBoundary(unittest.TestCase):
    def test_generated_revisions_excluded_but_other_reference_notes_searchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "vault")
            records = root / "reference" / "jarvis-records"
            records.mkdir(parents=True)
            (records / "jr-memory-r0001.md").write_text("# Old memory\nquartzsentinel", encoding="utf-8")
            (root / "reference" / "research.md").write_text("# Authored research\nquartzsentinel", encoding="utf-8")
            vault = Vault(root, Path(tmp, "state"))
            vault.refresh(force=True)
            with vault.connect() as db:
                paths = [row[0] for row in db.execute("SELECT path FROM notes")]
            self.assertEqual(paths, ["reference/research.md"])
            self.assertEqual(len(vault.search("quartzsentinel")), 1)

    def test_existing_old_index_rows_are_removed_after_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state = Path(tmp, "vault"), Path(tmp, "state")
            root.mkdir()
            old = Vault(root, state)
            rel = "reference/jarvis-records/old-revision.md"
            full = root / rel
            full.parent.mkdir(parents=True)
            full.write_text("# Old revision\nquartzsentinel", encoding="utf-8")
            with old.connect() as db:
                old._index(db, rel, full)
                db.execute("INSERT OR REPLACE INTO meta VALUES('version','2')")
            upgraded = Vault(root, state)
            upgraded.refresh(force=True)
            with upgraded.connect() as db:
                self.assertEqual(db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0], "3")
                self.assertEqual(db.execute("SELECT count(*) FROM notes WHERE path=?", (rel,)).fetchone()[0], 0)
            self.assertEqual(upgraded.search("quartzsentinel"), [])
