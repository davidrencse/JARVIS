"""Scrolling: the direct executor, its fail-safes, and the registry/pipeline wiring."""

import json
import threading
import unittest
from unittest import mock

from jarvis import actions, registry
from jarvis.pipeline import DIRECT

DEVICES = {"dell": {}, "zenbook": {}}
WINDOWS = [{"id": "0xa1", "app": "google-chrome google-chrome", "title": "Shorts - YouTube"},
           {"id": "0xb2", "app": "kitty kitty", "title": "fish ~/projects"}]


class FakeStop:
    """An Event whose wait() returns at once, set after `ticks` waits."""

    def __init__(self, ticks):
        self.ticks = ticks

    def wait(self, _seconds):
        self.ticks -= 1
        return self.ticks < 0


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        actions._SCROLLER.update(stop=None, target=None)
        self.sent = []
        patches = [mock.patch.object(actions, "open_windows", return_value=WINDOWS),
                   mock.patch.object(actions, "_send_key",
                                     side_effect=lambda w, k: self.sent.append((w["id"], k)) or True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_once_on_shorts_uses_the_arrow_key_in_the_named_window(self):
        with mock.patch.object(actions, "_active_window_id", return_value="0xb2"):
            ok, text = actions.scroll(json.dumps({"mode": "once", "direction": "down",
                                                  "target": "youtube",
                                                  "url": "https://www.youtube.com/shorts"}))
        self.assertTrue(ok, text)
        self.assertEqual(self.sent, [("0xa1", "Down")])

    def test_once_without_target_scrolls_the_focused_window_a_page(self):
        with mock.patch.object(actions, "_active_window_id", return_value="0xb2"):
            ok, _ = actions.scroll(json.dumps({"mode": "once", "direction": "up"}))
        self.assertTrue(ok)
        self.assertEqual(self.sent, [("0xb2", "Prior")])

    def test_missing_window_is_reported_not_guessed(self):
        ok, text = actions.scroll(json.dumps({"mode": "once", "target": "tiktok"}))
        self.assertFalse(ok)
        self.assertIn("tiktok", text)
        self.assertEqual(self.sent, [])

    def test_auto_starts_one_scroller_and_stop_ends_it(self):
        with mock.patch.object(actions, "_scroll_loop"):
            ok, text = actions.scroll(json.dumps({"mode": "auto", "target": "youtube",
                                                  "every": "5"}))
            self.assertTrue(ok, text)
            first = actions._SCROLLER["stop"]
            self.assertIsInstance(first, threading.Event)
            actions.scroll(json.dumps({"mode": "auto", "target": "youtube"}))
            self.assertTrue(first.is_set(), "a new auto scroll replaces the old one")
            ok, text = actions.scroll(json.dumps({"mode": "stop"}))
        self.assertTrue(ok)
        self.assertIn("Stopped", text)
        self.assertIsNone(actions._SCROLLER["stop"])

    def test_auto_needs_a_target(self):
        ok, _ = actions.scroll(json.dumps({"mode": "auto"}))
        self.assertFalse(ok)

    def test_loop_only_presses_while_the_window_is_focused(self):
        focus = iter(["0xa1", "0xb2", "0xa1"])
        with mock.patch.object(actions, "_active_window_id", side_effect=lambda: next(focus)):
            actions._scroll_loop(FakeStop(3), "youtube", "Chrome", "Down", 1)
        self.assertEqual(self.sent, [("0xa1", "Down"), ("0xa1", "Down")])

    def test_loop_ends_once_the_window_is_gone(self):
        stop = FakeStop(100)
        with mock.patch.object(actions, "open_windows", return_value=[]), \
                mock.patch.object(actions, "_active_window_id", return_value=None):
            actions._scroll_loop(stop, "youtube", None, "Down", 1)
        self.assertEqual(stop.ticks, 100 - actions.SCROLL_MISSES)
        self.assertEqual(self.sent, [])

    def test_loop_ends_at_the_time_cap(self):
        clock = iter([0, 0, actions.SCROLL_MAX + 1])
        with mock.patch.object(actions, "_active_window_id", return_value="0xa1"):
            actions._scroll_loop(FakeStop(100), "youtube", None, "Down", 1,
                                 clock=lambda: next(clock))
        self.assertEqual(len(self.sent), 1)


class HyprlandKeyTests(unittest.TestCase):
    def test_send_key_aims_at_one_window_address(self):
        calls = []
        out = mock.Mock(stdout="ok")
        with mock.patch.object(actions, "_backend", return_value="hyprland"), \
                mock.patch.object(actions, "_run", side_effect=lambda *a: calls.append(a) or out):
            self.assertTrue(actions._send_key({"id": "0xa1"}, "Down"))
            self.assertFalse(actions._send_key({"id": "0xa1; rm"}, "Down"))
        self.assertEqual(len(calls), 1)
        self.assertIn('window = "address:0xa1"', calls[0][2])


class WiringTests(unittest.TestCase):
    def test_registry_and_direct_executor(self):
        self.assertIn("scroll", DIRECT)
        good = [{"id": "c1", "intent": "scroll", "device": "dell",
                 "args": {"mode": "auto", "direction": "down", "target": "youtube",
                          "every": "8", "url": "https://www.youtube.com/shorts"},
                 "depends_on": []}]
        self.assertEqual(registry.validate(good, DEVICES), [])
        for bad in ({"mode": "forever"}, {"every": "1"}, {"every": "999"},
                    {"url": "file:///etc"}, {"key": "Down"}):
            cmd = [{**good[0], "args": bad}]
            self.assertTrue(registry.validate(cmd, DEVICES), bad)


if __name__ == "__main__":
    unittest.main()
