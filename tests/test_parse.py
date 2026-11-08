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

    def test_everyday_status_questions_parse(self):
        """Measured brain fall-throughs (events.jsonl): battery and Wi-Fi questions."""
        def shape(text):
            got = parse(text, DEVICES, "zenbook", main="zenbook")
            self.assertIsNotNone(got["commands"], (text, got["reason"]))
            return [(c["intent"], c["args"]) for c in got["commands"]]
        for text in ("what's my battery at", "how much battery do I have left",
                     "how much battery is left", "how much charge do i have left"):
            self.assertEqual(shape(text), [("system_status", {"what": "battery"})], text)
        for text in ("what network am I on", "which network am i connected to",
                     "what's my network name"):
            self.assertEqual(shape(text), [("system_status", {"what": "wifi"})], text)
        # Two questions one read answers: run it once.
        self.assertEqual(shape("is wifi connected and what network am I on"),
                         [("system_status", {"what": "wifi"})])
        self.assertEqual(shape("is wifi connected and how much battery do i have left"),
                         [("system_status", {"what": "wifi"}), ("system_status", {"what": "battery"})])

    def test_on_spotify_names_the_player_not_a_place(self):
        got = parse("play music on spotify", DEVICES, "zenbook", main="zenbook")["commands"]
        self.assertEqual([(c["intent"], c["args"]) for c in got], [("media", {"command": "play"})])
        # A named song or artist needs a search the registry can't express: the brain's.
        for text in ("play drake on spotify", "open spotify and play drake", "play something"):
            got = parse(text, DEVICES, "zenbook", main="zenbook")
            self.assertIsNone(got["commands"], text)
            self.assertNotIn("place", got["reason"], text)

    def test_status_near_misses_stay_unparsed(self):
        for text in ("tell me about the battery industry", "how much battery does a tesla have",
                     "what's the battery industry at", "what network is the game on tonight",
                     "what's the network", "how much battery is", "how much space is in the room"):
            self.assertIsNone(parse(text, DEVICES, "zenbook", main="zenbook")["commands"], text)

    def test_close_needs_an_exact_app_name(self):
        for text in ("close the stream", "close sword", "close explore"):
            self.assertIsNone(parse(text, DEVICES, "dell")["commands"], text)
        got = parse("close steam", DEVICES, "dell")["commands"]
        self.assertEqual(got[0]["args"], {"name": "Steam"})

    def test_then_orders_commands_and_plain_and_does_not(self):
        got = parse("close spotify and then open spotify", DEVICES, "dell")["commands"]
        self.assertEqual([c["depends_on"] for c in got], [[], ["c1"]])
        got = parse("close spotify then open chrome", DEVICES, "dell")["commands"]
        self.assertEqual(got[1]["depends_on"], ["c1"])
        got = parse("open chrome and open discord", DEVICES, "dell")["commands"]
        self.assertEqual([c["depends_on"] for c in got], [[], []])
        got = parse("close spotify and open spotify", DEVICES, "dell")["commands"]
        self.assertEqual(got[1]["depends_on"], ["c1"])

    def test_filler_inside_a_url_path_is_kept(self):
        for text, url in (("go to reddit.com/r/again", "https://reddit.com/r/again"),
                          ("go to example.com/please", "https://example.com/please")):
            got = parse(text, DEVICES, "dell")["commands"]
            self.assertEqual(got[0]["args"], {"url": url}, text)

    def test_scroll_is_never_inherited(self):
        for text in ("scroll down and then chrome", "start scrolling, youtube"):
            got = parse(text, DEVICES, "dell")
            self.assertIsNone(got["commands"], text)

    def test_an_explicit_http_scheme_is_kept(self):
        got = parse("open http://neverssl.com", DEVICES, "dell")["commands"]
        self.assertEqual(got[0]["args"], {"url": "http://neverssl.com"})
        got = parse("open neverssl.com", DEVICES, "dell")["commands"]
        self.assertEqual(got[0]["args"], {"url": "https://neverssl.com"})

    def test_device_apps_extend_the_catalog(self):
        self.assertIsNone(parse("open blender", DEVICES, "dell")["commands"])
        got = parse("open blender", DEVICES, "dell", apps={"dell": ["Blender"]})
        self.assertEqual(got["commands"][0]["args"], {"name": "Blender"})

    def test_whisper_opened_at_the_start_is_open(self):
        def cmds(text):
            got = parse(text, DEVICES, "dell", main="dell")
            return [[c["intent"], c["device"], c["args"]] for c in got["commands"] or []]
        self.assertEqual(cmds("opened Spotify here."), [["open_app", "dell", {"name": "Spotify"}]])
        self.assertEqual(cmds("opened Discord on Arch and then Steam."),
                         [["open_app", "zenbook", {"name": "Discord"}],
                          ["open_app", "zenbook", {"name": "Steam"}]])
        self.assertEqual(cmds("hey jarvis, opened chrome"), [["open_app", "dell", {"name": "Chrome"}]])
        self.assertEqual(cmds("Opened up Google Chrome."), [["open_app", "dell", {"name": "Chrome"}]])

    def test_opened_elsewhere_or_as_past_tense_is_not_an_action(self):
        for text in ("I opened it yesterday", "opened it yesterday", "opened the door",
                     "open chrome and opened steam"):
            self.assertIsNone(parse(text, DEVICES, "dell", main="dell")["commands"], text)

    def test_link_path_keeps_its_case(self):
        # Video ids are case-sensitive: v=dqw4w9wgxcq is a different (or no) video.
        for text, url in (("open youtube.com/watch?v=dQw4w9WgXcQ",
                           "https://youtube.com/watch?v=dQw4w9WgXcQ"),
                          ("Jarvis, open HTTPS://YouTube.com/watch?v=dQw4w9WgXcQ on Arch",
                           "https://youtube.com/watch?v=dQw4w9WgXcQ"),
                          ("go to github.com/DavidRen/JARVIS.", "https://github.com/DavidRen/JARVIS")):
            with self.subTest(text=text):
                got = parse(text, DEVICES, "dell")
                self.assertIsNotNone(got["commands"], got["reason"])
                self.assertEqual(got["commands"][0]["args"], {"url": url})

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
