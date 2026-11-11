"""System design page data: every module has a station, and broken things come with a fix."""

import json
import tempfile
import unittest
from pathlib import Path

from jarvis.sysview import PATHS, STATIONS, design
from jarvis.ui import Console

ROOT = Path(__file__).resolve().parents[2]


class SystemDesignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = json.loads((ROOT / "jarvis.config.json").read_text(encoding="utf-8"))
        config["vault"] = {"path": str(Path(self.tmp.name, "no-vault"))}
        config["knowledge"] = {"path": str(Path(self.tmp.name, "kb"))}
        self.console = Console(config, ROOT, self.tmp.name)
        self.data = design(self.console)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stores_survive_files_vanishing_mid_walk(self):
        from unittest import mock
        from jarvis import sysview
        real = Path.stat
        def flaky(self, *a, **k):
            if self.name == "ghost.txt":
                raise FileNotFoundError(str(self))
            return real(self, *a, **k)
        kb = Path(self.tmp.name, "kb")
        kb.mkdir(exist_ok=True)
        (kb / "ghost.txt").write_text("x")
        with mock.patch.object(Path, "stat", flaky):
            rows = sysview.stores(self.console)
        self.assertTrue(rows)

    def test_every_module_belongs_to_a_station(self):
        loose = [m["name"] for m in self.data["modules"] if m["station"] == "other" and m["name"] != "__init__"]
        self.assertEqual(loose, [], "add new modules to a station in jarvis/sysview.py")

    def test_paths_only_name_real_stations(self):
        ids = {s["id"] for s in STATIONS}
        for tier, path in PATHS.items():
            self.assertTrue(set(path) <= ids, tier)

    def test_a_fresh_machine_says_what_to_do(self):
        checks = {c["label"]: c for c in self.data["checks"]}
        voiceprint = checks["Your voiceprint is enrolled"]
        self.assertEqual(voiceprint["state"], "fail")
        self.assertIn("voice enroll", voiceprint["fix"])
        self.assertEqual(checks["Obsidian vault found"]["state"], "fail")
        self.assertTrue(all(c["fix"] for c in self.data["checks"] if c["state"] == "fail"))
        self.assertTrue(all(not c["fix"] for c in self.data["checks"] if c["state"] == "ok"))

    def test_config_check_catches_a_bad_edit(self):
        from jarvis.sysview import config_check
        base = Path(self.tmp.name)
        self.assertEqual(config_check(base)["state"], "fail")                     # missing
        (base / "jarvis.config.json").write_text('{"rules": {', encoding="utf-8")
        broken = config_check(base)
        self.assertEqual(broken["state"], "fail")
        self.assertIn("pre-*", broken["fix"])
        (base / "jarvis.config.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(config_check(base)["state"], "fail")                     # not an object
        (base / "jarvis.config.json").write_text('{"rules": {}, "vault": {}}', encoding="utf-8")
        ok = config_check(base)
        self.assertEqual(ok["state"], "ok")
        self.assertEqual(ok["fix"], "")
        self.assertIn("Config file reads cleanly", {c["label"] for c in self.data["checks"]})

    def test_vault_off_by_choice_is_not_a_failure(self):
        config = dict(self.console.config, vault={"enabled": False, "path": str(Path(self.tmp.name, "no-vault"))})
        checks = {c["label"]: c for c in design(Console(config, ROOT, self.tmp.name))["checks"]}
        self.assertNotIn("Obsidian vault found", checks)
        self.assertEqual(checks["Obsidian vault off by choice"]["state"], "ok")
        self.assertIn("kb ingest", checks["Wiki built from your sources"]["fix"])

    def test_stores_and_setup(self):
        names = {s["name"] for s in self.data["stores"]}
        self.assertIn("Wiki", names)
        self.assertIn("Conversation memory", names)
        self.assertIn(self.data["setup"]["brain"], ("ChatGPT", "Claude"))


if __name__ == "__main__":
    unittest.main()


class ScheduleCheckTests(unittest.TestCase):
    def test_a_schtasks_kind_alone_is_not_proof_it_is_scheduled(self):
        from unittest import mock
        from jarvis import sysview
        for sched, expect in (({"kind": "schtasks", "at": "02:00", "installed": False}, False),
                              ({"kind": "systemd", "at": "03:30", "installed": True}, True)):
            with tempfile.TemporaryDirectory() as tmp:
                config = {"vault": {"path": str(Path(tmp, "none"))}, "knowledge": {"path": str(Path(tmp, "kb"))}}
                console = Console(config, ROOT, tmp)
                snap = console.kb().snapshot()
                snap["checker"]["schedule"] = sched
                with mock.patch.object(type(console.kb()), "snapshot", return_value=snap):
                    row = next(c for c in sysview.checks(console) if c["label"] == "Nightly wiki check scheduled")
                self.assertEqual(row["state"] == "ok", expect, row)


class ComponentCardsTest(unittest.TestCase):
    def test_every_card_has_the_grammar_and_never_raises(self):
        import tempfile
        from types import SimpleNamespace
        from jarvis import sysview
        with tempfile.TemporaryDirectory() as state:
            cards = sysview.components(SimpleNamespace(config={}, state=state))
        self.assertGreaterEqual(len(cards), 8)
        for card in cards:
            self.assertIn(card["state"], ("ok", "warn", "off", "fail"), card)
            self.assertTrue(card["title"] and 1 <= len(card["rows"]) <= 5, card)
            self.assertTrue(all(len(row) == 2 for row in card["rows"]), card)
        self.assertNotIn("fail", [c["state"] for c in cards])  # an empty state folder is not an error
