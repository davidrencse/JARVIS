"""Shared vault walk, cached link decoding and the wiki page parse cache: each must save
work on an unchanged vault and notice every kind of change (edit, add, delete)."""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import vault as vault_module
from jarvis.events import Watcher
from jarvis.knowledge import KnowledgeBase, render_page
from jarvis.vault import Vault, forget, scan

CLAIM = {"id": "caaaaaa", "text": "The review routine runs daily at 02:00.", "date": "2026-08-01",
         "sources": ["source-001"], "flag": None, "with": []}
PAGE = render_page({"title": "Routines", "type": "page", "claims": [CLAIM], "related": []},
                   date(2026, 9, 1))


class Walks:
    """Counts os.walk calls made by the vault module."""

    def __enter__(self):
        self.count, real = 0, os.walk

        def walk(*args, **kwargs):
            self.count += 1
            return real(*args, **kwargs)

        self.patch = patch.object(vault_module.os, "walk", walk)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class SharedWalkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name, "Vault")
        (self.root / "notes").mkdir(parents=True)
        for i in range(5):
            (self.root / "notes" / f"n{i}.md").write_text(
                f"---\ntitle: N{i}\ntype: note\n---\n[[n{(i + 1) % 5}]]\n", encoding="utf-8")
        self.state = Path(temp.name, "state")
        forget(self.root)
        self.addCleanup(forget, self.root)

    def test_index_tasks_and_watcher_share_one_walk(self):
        vault = Vault(self.root, self.state, {"write": True})
        watcher = SimpleNamespace(vault=str(self.root))
        with Walks() as walks:
            vault.refresh(force=True)
            list(vault.markdown())
            vault.names()
            Watcher._vault_stamp(watcher)
            Watcher._vault_stamp(watcher)
        self.assertEqual(walks.count, 1)

    def test_a_new_edited_or_deleted_file_is_seen_after_the_snapshot_expires(self):
        vault = Vault(self.root, self.state, {"write": True})
        watcher = SimpleNamespace(vault=str(self.root))
        before = Watcher._vault_stamp(watcher)
        (self.root / "notes" / "new.md").write_text("new", encoding="utf-8")
        self.assertEqual(Watcher._vault_stamp(watcher), before)  # still inside the TTL
        with patch.object(vault_module, "SCAN_TTL", 0):
            after = Watcher._vault_stamp(watcher)
            self.assertEqual(after[0], before[0] + 1)
            path = self.root / "notes" / "n0.md"
            os.utime(path, (after[1] + 50, after[1] + 50))
            self.assertEqual(Watcher._vault_stamp(watcher)[1], after[1] + 50)
            (self.root / "notes" / "new.md").unlink()
            self.assertEqual(Watcher._vault_stamp(watcher)[0], before[0])
            vault.refresh(force=True)
            self.assertNotIn("notes/new.md", {n["path"] for n in vault.notes()})

    def test_forced_refresh_and_own_writes_skip_the_ttl(self):
        vault = Vault(self.root, self.state, {"write": True})
        vault.refresh(force=True)
        (self.root / "notes" / "late.md").write_text("---\ntitle: Late\n---\nx", encoding="utf-8")
        vault.refresh(force=True)  # force re-walks even inside the TTL
        self.assertIn("notes/late.md", {n["path"] for n in vault.notes()})
        vault.capture("Fresh idea", "body")  # our own write forgets the snapshot
        self.assertIn("notes/fresh-idea.md", {r for r, _, _, _ in scan(self.root)}
                      | {n["path"] for n in vault.notes()})

    def test_links_are_decoded_once_not_per_call(self):
        vault = Vault(self.root, self.state, {"write": True})
        vault.refresh(force=True)
        vault.graph()  # warm
        real = json.loads
        with patch.object(vault_module.json, "loads", side_effect=real) as loads:
            vault.graph()
            vault.health()
            vault.backlinks("n1")
        self.assertEqual(loads.call_count, 0)


class PageCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name, "knowledge")
        self.kb = KnowledgeBase(self.root, today=date(2026, 9, 1))
        self.page = self.root / "wiki" / "routines.md"
        self.page.write_text(PAGE, encoding="utf-8")

    def parses(self):
        return patch("jarvis.knowledge.parse_page", wraps=__import__("jarvis.knowledge").knowledge.parse_page)

    def test_unchanged_pages_are_not_reparsed(self):
        with self.parses() as parse:
            for _ in range(3):
                self.kb.recall("review routine daily")
        self.assertEqual(parse.call_count, 1)

    def test_edit_add_and_delete_invalidate(self):
        self.assertTrue(self.kb.recall("review routine daily"))
        self.page.write_text(PAGE.replace("daily at 02:00", "weekly on Sunday"), encoding="utf-8")
        self.assertIn("Sunday", " ".join(self.kb.recall("review routine weekly")))
        (self.root / "wiki" / "backups.md").write_text(
            PAGE.replace("Routines", "Backups").replace("review routine runs daily at 02:00",
                                                         "nightly backups reach the NAS drive"),
            encoding="utf-8")
        self.assertIn("backups", self.kb.pages(shared=True))
        (self.root / "wiki" / "backups.md").unlink()
        self.assertNotIn("backups", self.kb.pages(shared=True))

    def test_callers_get_private_copies(self):
        self.kb.pages()["routines"]["claims"].clear()
        self.assertTrue(self.kb.pages()["routines"]["claims"])
        self.assertTrue(self.kb.recall("review routine daily"))


if __name__ == "__main__":
    unittest.main()
