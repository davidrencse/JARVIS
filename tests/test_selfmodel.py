"""JARVIS.md self-model: machine facts, mismatch detection and the brief."""

import json
import tempfile
import unittest
from pathlib import Path

from jarvis import selfmodel

ROOT = Path(__file__).resolve().parent.parent
CONFIG = {
    "devices": {
        "dell": {"label": "Dell G15", "aliases": ["windows", "g15"]},
        "zenbook": {"label": "Zenbook", "aliases": ["arch", "linux"]},
        "bare": {},
    },
    "cluster": {"main_device": "dell"},
}


class MachineFactsTests(unittest.TestCase):
    def test_lists_every_device_with_role_and_aliases(self):
        lines = selfmodel.machine_facts(CONFIG)
        self.assertEqual(len(lines), 3)
        dell, zen, bare = lines
        self.assertIn("dell", dell)
        self.assertIn("Dell G15", dell)
        self.assertIn("main PC", dell)
        self.assertIn("also called windows, g15", dell)
        self.assertIn("node", zen)
        self.assertNotIn("main PC", zen)
        self.assertIn("also called arch, linux", zen)
        self.assertNotIn("also called", bare)
        self.assertIn("bare", bare)  # label falls back to the id

    def test_no_devices(self):
        self.assertEqual(selfmodel.machine_facts({}), [])


class MismatchTests(unittest.TestCase):
    def test_wrong_main_pc_is_reported(self):
        found = selfmodel.mismatches("The zenbook is the main PC.", CONFIG)
        self.assertEqual(len(found), 1)
        self.assertIn("zenbook", found[0])
        self.assertIn("dell", found[0])

    def test_matching_line_is_fine(self):
        self.assertEqual(selfmodel.mismatches("The dell is the main PC.", CONFIG), [])

    def test_unrelated_lines_are_fine(self):
        text = "The zenbook runs Arch.\nMy main PC is great.\nNothing here."
        self.assertEqual(selfmodel.mismatches(text, CONFIG), [])


class BriefTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def write(self, text):
        (self.base / selfmodel.FILE).write_text(text, encoding="utf-8")

    def test_file_then_authoritative_facts(self):
        self.write("I am David. Hello.")
        out = selfmodel.brief(CONFIG, self.base)
        self.assertTrue(out.startswith("I am David. Hello."))
        self.assertIn("Configured machines right now (authoritative)", out)
        self.assertLess(out.index("I am David"), out.index("Configured machines"))
        self.assertNotIn("Note:", out)

    def test_mismatch_adds_note(self):
        self.write("The zenbook is the main PC.")
        out = selfmodel.brief(CONFIG, self.base)
        self.assertIn("Note: ", out)
        self.assertIn("Follow the configuration.", out)

    def test_file_part_capped(self):
        self.write("x" * (selfmodel.MAX_CHARS + 500))
        out = selfmodel.brief(CONFIG, self.base)
        file_part = out.split("\n\n")[0]
        self.assertEqual(len(file_part), selfmodel.MAX_CHARS)
        self.assertIn("Configured machines right now", out)

    def test_missing_file_gives_facts_only(self):
        out = selfmodel.brief(CONFIG, self.base)
        self.assertTrue(out.startswith("Configured machines right now (authoritative):"))

    def test_never_raises(self):
        for config in ({}, {"devices": None}):
            self.assertIsInstance(selfmodel.brief(config, self.base), str)
        self.assertEqual(selfmodel.brief({}, self.base), "")
        self.assertIsInstance(selfmodel.brief(CONFIG, self.base / "nope"), str)


class RealRepoTests(unittest.TestCase):
    def test_real_jarvis_md_matches_real_config(self):
        config = json.loads((ROOT / "jarvis.config.json").read_text(encoding="utf-8"))
        text = (ROOT / selfmodel.FILE).read_text(encoding="utf-8")
        self.assertEqual(selfmodel.mismatches(text, config), [])


if __name__ == "__main__":
    unittest.main()
