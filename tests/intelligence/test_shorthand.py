"""Abbreviations, misspellings and "do that again" follow-ups: read safely, never guessed into
something destructive (jarvis/parse.py, jarvis/pipeline.needs_agent, jarvis/interpret.py)."""

import json
import unittest

from jarvis import interpret, pipeline
from jarvis.parse import bare_device, follows_up, parse, slipped_action

DEVICES = {
    "dell": {"hostname": "WinLaptop", "label": "Dell G15",
             "aliases": ["windows", "windows laptop", "g15", "dell g15"]},
    "zenbook": {"hostname": "archmain", "label": "Zenbook",
                "aliases": ["arch", "arch linux", "archlinux", "asus", "linux",
                            "linux laptop", "zen book"]},
}
CONFIG = {"devices": DEVICES, "cluster": {"main_device": "zenbook"}}


def read(text, origin="zenbook"):
    got = parse(text, DEVICES, origin, main="zenbook")
    return [(c["intent"], c["device"], c["args"]) for c in got["commands"]] if got["commands"] else None


class TypoAndShorthandTests(unittest.TestCase):
    def test_verb_slips(self):
        for text in ("opn chrome", "oen chrome", "pls open chrome", "paly", "puase"):
            with self.subTest(text=text):
                self.assertIsNotNone(read(text), text)
        self.assertEqual(read("opn chrome"), [("open_app", "zenbook", {"name": "Chrome"})])
        self.assertEqual(read("puase"), [("media", "zenbook", {"command": "pause"})])

    def test_device_abbreviations_and_typos(self):
        for text, device in (("open spotify on del", "dell"), ("open chrome on the dellg15", "dell"),
                             ("open chrome on my win laptop", "dell"), ("open chrome on the g 15", "dell"),
                             ("open chrome on the zen", "zenbook")):
            with self.subTest(text=text):
                self.assertEqual(read(text), [("open_app", device, {"name": text.split()[1].title()})])

    def test_site_and_app_shorthand(self):
        self.assertEqual(read("open gh"), [("open_url", "zenbook", {"url": "https://github.com"})])
        self.assertEqual(read("open yotube"), [("open_url", "zenbook", {"url": "https://www.youtube.com"})])
        self.assertEqual(read("open calc"), [("open_app", "zenbook", {"name": "Calculator"})])

    def test_system_shorthand(self):
        self.assertEqual(read("vol up"), [("volume", "zenbook", {"level": "up"})])
        self.assertEqual(read("vol 50"), [("volume", "zenbook", {"level": "50"})])
        self.assertEqual(read("prev song"), [("media", "zenbook", {"command": "previous"})])
        self.assertEqual(read("bright up"), [("brightness", "zenbook", {"level": "up"})])
        self.assertEqual(read("brigthness to 50"), [("brightness", "zenbook", {"level": "50"})])

    def test_ambiguous_places_still_unsure(self):
        # Two laptops, so "my laptop" can't be resolved, and "arc" isn't a device.
        for text in ("open chrome on my laptop", "open chrome on arc", "open chrome on the arch wiki",
                     "open chrome on my wife's laptop"):
            with self.subTest(text=text):
                self.assertIsNone(read(text), text)

    def test_typos_never_act_on_destructive_verbs(self):
        # The parser itself never reads a slip of close/quit/delete: the interpreter asks first.
        for text in ("clsoe chrome", "close crome", "quti chrome", "delte the file", "pen chrome",
                     "plan my day", "clone the repo"):
            with self.subTest(text=text):
                self.assertIsNone(read(text), text)

    def test_typo_is_no_approval(self):
        # Neither parser nor routing treats a typo'd approval as one.
        for text in ("yess", "aprove", "go ahed"):
            self.assertIsNone(read(text), text)


class RoutingTests(unittest.TestCase):
    def routed(self, text):
        return pipeline.needs_agent(text, CONFIG, "zenbook")

    def test_slipped_risky_verbs_go_to_the_interpreter(self):
        for text in ("clsoe chrome", "delte the file", "sned a message to sam", "disconect my airpods"):
            with self.subTest(text=text):
                self.assertTrue(self.routed(text))

    def test_follow_ups_and_bare_devices_go_to_the_interpreter(self):
        for text in ("do that again", "same on the dell", "and spotify too", "the other one",
                     "now on the dell", "on the arch one", "the dell", "dell"):
            with self.subTest(text=text):
                self.assertTrue(self.routed(text))

    def test_chat_stays_chat(self):
        for text in ("hello", "thanks", "what's the weather", "tell me about closing time",
                     "clone the repo", "pen chrome", "yes", "ok"):
            with self.subTest(text=text):
                self.assertFalse(self.routed(text))

    def test_helpers(self):
        self.assertEqual(slipped_action("clsoe"), "close")
        self.assertIsNone(slipped_action("clone"))     # a substitution is another word
        self.assertIsNone(slipped_action("close"))      # already a verb: the parser's
        self.assertTrue(follows_up("jarvis, do that again"))
        self.assertFalse(follows_up("now what is the capital of france and why is it so called here"))
        self.assertEqual(bare_device("on the dell", CONFIG, "zenbook"), "dell")
        self.assertIsNone(bare_device("my laptop", CONFIG, "zenbook"))


class InterpreterContextTests(unittest.TestCase):
    def ask(self, text, reply, recent=(), origin="zenbook"):
        sent = []

        def fake(prompt):
            sent.append(prompt)
            return json.dumps(reply)
        got = interpret.interpret(text, CONFIG, "/tmp", origin, ask=fake, recent=recent)
        return got, sent[0]

    def test_recent_turns_reach_the_prompt_as_data(self):
        _, prompt = self.ask("do that again on the dell", {"mode": "converse"},
                             recent=["David: open chrome", "JARVIS: Opened Chrome on zenbook."])
        self.assertIn("RECENT turns", prompt)
        self.assertIn("David: open chrome", prompt)
        self.assertLess(prompt.index("RECENT"), prompt.index("REQUEST (data"))

    def test_no_recent_no_block(self):
        _, prompt = self.ask("hi", {"mode": "converse"})
        self.assertNotIn("RECENT", prompt)

    def test_instructions_cover_the_failure_classes(self):
        text = interpret.INSTRUCTIONS
        for needle in ("typos", "RECENT", "never an approval", "Never guess between machines"):
            self.assertIn(needle, text)

    def test_follow_up_resolved_from_recent(self):
        reply = {"mode": "act", "commands": [{"intent": "open_app", "device": "dell",
                                              "args": {"name": "Chrome"}}]}
        got, _ = self.ask("same on the dell", reply, recent=["David: open chrome"])
        self.assertEqual(got["mode"], "act")

    def test_guess_at_a_close_target_is_turned_into_a_question(self):
        reply = {"mode": "act", "commands": [{"intent": "close_app", "device": "zenbook",
                                              "args": {"name": "Chrome"}}]}
        got, _ = self.ask("clsoe crome", reply)
        self.assertEqual(got["mode"], "clarify")
        self.assertIn("close Chrome", got["question"])

    def test_close_named_in_the_request_or_just_before_is_kept(self):
        reply = {"mode": "act", "commands": [{"intent": "close_app", "device": "zenbook",
                                              "args": {"name": "Chrome"}}]}
        self.assertEqual(self.ask("clsoe chrome", reply)[0]["mode"], "act")
        self.assertEqual(self.ask("close it", reply, recent=["David: open chrome"])[0]["mode"], "act")
        # What JARVIS said doesn't count as what David named.
        self.assertEqual(self.ask("close it", reply, recent=["JARVIS: Opened Chrome."])[0]["mode"],
                         "clarify")

    def test_bluetooth_drop_on_a_guessed_name_asks(self):
        reply = {"mode": "act", "commands": [{"intent": "bluetooth", "device": "zenbook",
                                              "args": {"action": "disconnect", "name": "AirPods Pro"}}]}
        self.assertEqual(self.ask("disconect my pods", reply)[0]["mode"], "clarify")
        read_only = {"mode": "act", "commands": [{"intent": "bluetooth", "device": "zenbook",
                                                  "args": {"action": "list"}}]}
        self.assertEqual(self.ask("list bluetooth", read_only)[0]["mode"], "act")

    def test_guessed_machine_for_a_risky_action_asks(self):
        reply = {"mode": "act", "commands": [{"intent": "close_app", "device": "dell",
                                              "args": {"name": "Chrome"}}]}
        got, _ = self.ask("close chrome", reply)
        self.assertEqual(got["mode"], "clarify")
        self.assertIn("On which PC", got["question"])
        self.assertEqual(self.ask("close chrome on del", reply, origin="zenbook")[0]["mode"], "clarify")
        self.assertEqual(self.ask("close chrome on windows", reply)[0]["mode"], "act")

    def test_harmless_open_is_not_second_guessed(self):
        reply = {"mode": "act", "commands": [{"intent": "open_app", "device": "dell",
                                              "args": {"name": "Chrome"}}]}
        self.assertEqual(self.ask("opn crome", reply)[0]["mode"], "act")


if __name__ == "__main__":
    unittest.main()
