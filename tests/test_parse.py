"""The strict parser against its labelled set, and the Intent Registry's checks."""

import json
import unittest
from pathlib import Path

from jarvis import registry
from jarvis.parse import parse, parse_for

CASES = json.loads((Path(__file__).with_name("parse_cases.json")).read_text(encoding="utf-8"))
DEVICES = {
    "dell": {"hostname": "WinLaptop",
             "aliases": ["windows", "windows laptop", "g15", "dell g15"]},
    "zenbook": {"hostname": "archmain",
                "aliases": ["arch", "arch linux", "archlinux", "asus", "linux",
                            "linux laptop", "zen book"]},
}


class LabelledSetTests(unittest.TestCase):
    def test_every_labelled_case(self):
        for case in CASES["cases"]:
            with self.subTest(text=case["text"], origin=case.get("origin", "dell")):
                got = parse(case["text"], DEVICES, case.get("origin", "dell"), main="dell")
                if case["expect"] is None:
                    self.assertIsNone(got["commands"], "must go to the interpreter")
                    self.assertTrue(got["reason"])
                    continue
                self.assertIsNotNone(got["commands"], got["reason"])
                self.assertEqual([[c["intent"], c["device"], c["args"], c["depends_on"]]
                                  for c in got["commands"]], case["expect"])

    def test_precision_is_total(self):
        """No labelled case may get a wrong parse: certain means right, or no answer."""
        wrong = []
        for case in CASES["cases"]:
            got = parse(case["text"], DEVICES, case.get("origin", "dell"), main="dell")
            if got["commands"] is not None:
                shape = [[c["intent"], c["device"], c["args"], c["depends_on"]]
                         for c in got["commands"]]
                if shape != case["expect"]:
                    wrong.append(case["text"])
        self.assertEqual(wrong, [])


class ParserTests(unittest.TestCase):
    def test_parse_for_reads_devices_and_main_from_config(self):
        config = {"devices": DEVICES, "cluster": {"main_device": "zenbook"}}
        got = parse_for("open spotify on my main pc", config, origin="dell")
        self.assertEqual(got["commands"][0]["device"], "zenbook")

    def test_device_apps_extend_the_catalog(self):
        self.assertIsNone(parse("open blender", DEVICES, "dell")["commands"])
        got = parse("open blender", DEVICES, "dell", apps={"dell": ["Blender"]})
        self.assertEqual(got["commands"][0]["args"], {"name": "Blender"})

    def test_shared_alias_never_resolves(self):
        devices = {**DEVICES, "zenbook": {**DEVICES["zenbook"],
                                          "aliases": DEVICES["zenbook"]["aliases"] + ["g15"]}}
        self.assertIsNone(parse("open chrome on g15", devices, "dell")["commands"])

    def test_unknown_origin_and_oversized_text(self):
        self.assertIsNone(parse("open spotify", DEVICES, "phone")["commands"])
        self.assertIsNone(parse("open spotify " * 60, DEVICES, "dell")["commands"])


class RegistryTests(unittest.TestCase):
    def command(self, intent, args, device="dell", **extra):
        return {"id": "c1", "intent": intent, "device": device, "args": args,
                "depends_on": [], **extra}

    def test_valid_commands_pass(self):
        for intent, args in (("open_app", {"name": "Spotify"}),
                             ("open_url", {"url": "https://x.com", "browser": "Chrome"}),
                             ("open_url", {"url": "spotify:collection:tracks"}),
                             ("media", {"command": "next"}), ("volume", {"level": "100"}),
                             ("spotify_play", {"target": "playlist:37i9dQZF1DX"}),
                             ("agent_task", {"text": "reply to Sam"})):
            self.assertEqual(registry.validate([self.command(intent, args)], DEVICES), [])

    def test_invalid_commands_fail(self):
        bad = (("open_url", {"url": "file:///C:/Windows"}), ("open_url", {"url": "javascript:x"}),
               ("volume", {"level": "101"}), ("media", {"command": "rewind"}),
               ("open_app", {}), ("open_app", {"name": "x", "shell": "rm"}),
               ("spotify_play", {"target": "liked; rm"}), ("nope", {}))
        for intent, args in bad:
            with self.subTest(intent=intent, args=args):
                self.assertTrue(registry.validate([self.command(intent, args)], DEVICES))
        self.assertTrue(registry.validate([self.command("open_app", {"name": "x"}, "phone")],
                                          DEVICES))
        self.assertTrue(registry.validate([self.command("open_app", {"name": "x"},
                                                        depends_on=["c9"])], DEVICES))

    def test_dependency_cycles_are_rejected(self):
        a = {**self.command("open_app", {"name": "x"}), "depends_on": ["c2"]}
        b = {**self.command("open_app", {"name": "y"}), "id": "c2", "depends_on": ["c1"]}
        self.assertIn("depends_on has a cycle: c1, c2.", registry.validate([a, b], DEVICES))
        a["depends_on"] = []
        self.assertEqual(registry.validate([a, b], DEVICES), [])

    def test_risk_comes_from_the_table(self):
        self.assertEqual(registry.risk(self.command("open_app", {"name": "Spotify"})), "allow")
        self.assertEqual(registry.risk(self.command("close_app", {"name": "Word"})), "confirm")
        self.assertEqual(registry.risk(self.command("close_app", {"name": "Spotify"})), "allow")
        self.assertEqual(registry.risk(self.command("agent_task", {"text": "x"})), "ratchet")

    def test_only_effectful_intents_have_observers(self):
        self.assertFalse(registry.get("answer").effect)
        self.assertTrue(registry.get("open_app").effect)
        self.assertTrue(registry.get("spotify_play").escalate)
        self.assertFalse(registry.get("open_app").escalate)


if __name__ == "__main__":
    unittest.main()
