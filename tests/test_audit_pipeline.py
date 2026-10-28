"""Regression tests for the request-pipeline audit: parser punctuation and chat reasons,
mixed confirm/run requests, the per-request duplicate guard, and browser-only title
observation. Fakes only: nothing here touches the desktop."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import gates, observe, pipeline
from jarvis.parse import parse_for

CONFIG = {"devices": {"dell": {}, "zenbook": {}}, "cluster": {"enabled": False}}


class FakeRemote:
    def __init__(self):
        self.calls = []

    def here(self):
        return "dell"

    def call(self, device, action, arg):
        self.calls.append((device, action, arg))
        if action == "observe":
            return True, json.dumps({"match": True, "after_ms": 1})
        return True, f"{action} ok"


def parsed(text):
    return parse_for(text, CONFIG, "dell")


class ParserPunctuationTests(unittest.TestCase):
    def test_trailing_and_leading_please(self):
        for text in ("Jarvis, open Chrome, please.", "please, open spotify",
                     "open chrome, please, and spotify"):
            with self.subTest(text):
                got = parsed(text)
                self.assertIsNotNone(got["commands"], got["reason"])
                self.assertTrue(all(c["intent"] == "open_app" for c in got["commands"]))

    def test_chat_with_on_is_not_an_unknown_place(self):
        for text in ("what's on my calendar", "what's on tv tonight",
                     "what do you think about the war on drugs"):
            with self.subTest(text):
                self.assertEqual(parsed(text)["reason"], "no action verb")
                self.assertFalse(pipeline.needs_agent(text, CONFIG, "dell"))

    def test_device_binding_still_works(self):
        got = parsed("open chrome on zenbook")
        self.assertEqual([(c["intent"], c["device"]) for c in got["commands"]],
                         [("open_app", "zenbook")])
        self.assertIn("unknown place", parsed("open chrome on mars")["reason"])


class MixedConfirmTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        patcher = patch.object(pipeline, "INFLIGHT", gates.InFlight())
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_text(self, text):
        remote = FakeRemote()
        result = pipeline.fast_path(text, CONFIG, self.state, remote=remote,
                                    app_exists=lambda d, n: True)
        return result, remote

    def test_safe_command_runs_and_risky_one_is_parked(self):
        result, remote = self.run_text("close word and open notepad")
        self.assertEqual([c[1] for c in remote.calls if c[1] != "observe"], ["open_app"])
        self.assertEqual(len(result["confirmations"]), 1)
        self.assertIn("open_app ok", result["reply"])
        self.assertIn("close Word", result["reply"])
        self.assertIn("Confirm (C1)", result["reply"])
        states = {o["id"]: o for o in result["outcomes"]}
        self.assertEqual(states["c1"]["state"], "not_sent")
        self.assertTrue(states["c2"]["ok"])

    def test_dependent_of_parked_command_is_held(self):
        commands = [
            {"id": "c1", "intent": "close_app", "device": "dell", "args": {"name": "Word"}, "depends_on": []},
            {"id": "c2", "intent": "open_app", "device": "dell", "args": {"name": "Notepad"}, "depends_on": ["c1"]},
            {"id": "c3", "intent": "open_app", "device": "dell", "args": {"name": "Spotify"}, "depends_on": []},
        ]
        remote = FakeRemote()
        result = pipeline.run_commands("t", commands, CONFIG, self.state, remote=remote,
                                       app_exists=lambda d, n: True)
        opened = [c[2] for c in remote.calls if c[1] == "open_app"]
        self.assertEqual(opened, ["Spotify"])
        self.assertIn("held", result["reply"].lower())
        self.assertFalse(result["outcomes"][1]["ok"])
        self.assertEqual(result["outcomes"][1]["state"], "not_sent")


class DuplicateGuardTests(unittest.TestCase):
    def test_same_request_repeats_run_twice(self):
        flight = gates.InFlight()
        command = {"id": "c1", "intent": "media", "device": "dell", "args": {"command": "next"}}
        again = {**command, "id": "c2"}
        self.assertTrue(flight.claim(command, "r1"))
        flight.release(command, True, "r1")
        self.assertTrue(flight.claim(again, "r1"))

    def test_other_request_duplicate_still_merged(self):
        flight = gates.InFlight()
        command = {"id": "c1", "intent": "media", "device": "dell", "args": {"command": "next"}}
        self.assertTrue(flight.claim(command, "r1"))
        flight.release(command, True, "r1")
        self.assertFalse(flight.claim(command, "r2"))

    def test_next_and_next_sends_both(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "INFLIGHT", gates.InFlight()):
            remote = FakeRemote()
            result = pipeline.fast_path("next and next", CONFIG, Path(tmp), remote=remote,
                                        app_exists=lambda d, n: True)
        self.assertEqual(len([c for c in remote.calls if c[1] != "observe"]), 2)
        self.assertNotIn("Already doing that", result["reply"])


def win(app, title):
    return {"app": app, "title": title, "focused": False}


class BrowserTitleTests(unittest.TestCase):
    def match(self, text, windows):
        return observe.matches({"kind": "title", "text": text}, windows)[0]

    def test_chrome_suffix_does_not_satisfy_open_google(self):
        self.assertFalse(self.match("google", [win("google-chrome", "Inbox - Google Chrome")]))
        self.assertTrue(self.match("google", [win("google-chrome", "Google - Google Chrome")]))

    def test_terminal_title_is_not_a_page(self):
        self.assertFalse(self.match("github", [win("kitty", "vim github.py")]))
        self.assertTrue(self.match("github", [win("firefox", "GitHub — Mozilla Firefox")]))
        self.assertTrue(self.match("github", [win("zen", "GitHub — Zen Browser")]))

    def test_console_window_is_not_a_browser(self):
        self.assertFalse(observe.is_browser("localconsole localconsole"))
        self.assertFalse(observe.is_browser("chrome-127.0.0.1__-Default"))
        self.assertFalse(self.match("jarvis", [win("localconsole", "JARVIS - Google Chrome")]))
        self.assertTrue(observe.is_browser("Google Chrome"))
        self.assertTrue(observe.is_browser("chrome.exe"))

    def test_twitter_can_be_observed(self):
        predicate = observe.predicate_for({"intent": "open_url", "args": {"url": "https://x.com"}})
        observe.validate(predicate)
        self.assertTrue(observe.matches(predicate, [win("zen", "Home / X — Zen Browser")])[0])
        self.assertTrue(observe.matches(predicate, [win("google-chrome", "(3) Home / X - Google Chrome")])[0])
        self.assertFalse(observe.matches(predicate, [win("google-chrome", "Inbox - Google Chrome")])[0])
        self.assertFalse(observe.matches(predicate, [win("kitty", "Home / X")])[0])


if __name__ == "__main__":
    unittest.main()
