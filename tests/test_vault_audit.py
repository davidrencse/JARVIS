"""Vault write-path fixes from the 2026-11-07 audit."""
import tempfile
import unittest
from pathlib import Path

from jarvis.tasks import Tasks
from jarvis.vault import Vault, split_frontmatter

EMPTY_FM = "---\n---\n\n# Drone\n\nIntro line.\n\n---\n\n## Parts\n- motor\n"


class EmptyFrontmatterTests(unittest.TestCase):
    def test_empty_block_ends_at_its_own_closing_line(self):
        fields, body = split_frontmatter(EMPTY_FM)
        self.assertEqual(fields, {})
        self.assertIn("Intro line.", body)  # the intro is body, so recall can find it

    def test_touch_writes_inside_the_empty_block_not_the_body(self):
        out = Vault._touch(EMPTY_FM, "2026-11-07")
        self.assertTrue(out.startswith("---\nupdated: 2026-11-07\n---\n\n# Drone"))
        self.assertEqual(out.count("updated:"), 1)
        self.assertTrue(out.endswith("Intro line.\n\n---\n\n## Parts\n- motor\n"))

    def test_normal_frontmatter_unchanged(self):
        text = "---\ntitle: x\nupdated: 2026-01-01\n---\nbody\n---\nmore"
        self.assertEqual(split_frontmatter(text), ({"title": "x", "updated": "2026-01-01"},
                                                   "body\n---\nmore"))
        self.assertEqual(Vault._touch(text, "2026-11-07"),
                         "---\ntitle: x\nupdated: 2026-11-07\n---\nbody\n---\nmore")


class VaultCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "vault"
        self.root.mkdir()
        self.state = Path(tmp.name) / "state"


class LiveProtectedTests(VaultCase):
    def test_a_note_added_to_protected_is_guarded_without_a_restart(self):
        (self.root / "hand.md").write_text("# Hand\n", "utf-8")
        vault = Vault(self.root, self.state, {"write": True})
        self.assertNotIn("hand.md", vault.protected)
        vault.settings = {"write": True, "protected": ["hand.md"]}  # what vault_for does
        self.assertIn("hand.md", vault.protected)
        with self.assertRaises(Exception):
            vault.append("hand", "- more", None)
        self.assertEqual((self.root / "hand.md").read_text("utf-8"), "# Hand\n")


class RecurringIdTests(VaultCase):
    def test_next_occurrence_gets_no_block_id_or_task_id(self):
        (self.root / "p.md").write_text(
            "- [ ] Water plants 🔁 every week 📅 2026-11-01 🆔 wat1 ^abc\n", "utf-8")
        tasks = Tasks(Vault(self.root, self.state, {"write": True}))
        tasks.update("water plants", "done")
        text = (self.root / "p.md").read_text("utf-8")
        self.assertEqual(text.count("^abc"), 1)
        self.assertEqual(text.count("🆔"), 1)
        done = [l for l in text.splitlines() if l.startswith("- [x]")][0]
        self.assertIn("^abc", done)  # links keep pointing at the finished task
        fresh = [l for l in text.splitlines() if l.startswith("- [ ]")][0]
        self.assertIn("🔁 every week", fresh)


if __name__ == "__main__":
    unittest.main()
