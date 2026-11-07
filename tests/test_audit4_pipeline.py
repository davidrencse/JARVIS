import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import observe, parse
from jarvis.chatgpt import ConfirmGate
from jarvis.confirm import ConfirmQueue

DEVICES = {"dell": {"hostname": "dell", "aliases": ["windows"]},
           "zenbook": {"hostname": "zenbook", "aliases": ["arch", "arch linux"]}}
CONFIG = {"devices": DEVICES, "cluster": {"enabled": False}}


def deps(text):
    result = parse.parse(text, DEVICES, "dell")
    return [(c["intent"], c["depends_on"]) for c in result["commands"]]


class SameResourceOrderTests(unittest.TestCase):
    def test_sound_commands_run_in_spoken_order(self):
        for text in ("mute and unmute", "pause and play", "pause and mute"):
            self.assertEqual(deps(text)[1][1], ["c1"], text)

    def test_close_then_play_it_waits(self):
        self.assertEqual(deps("close spotify and play it"),
                         [("close_app", []), ("media", ["c1"])])

    def test_different_apps_stay_parallel(self):
        self.assertEqual([d for _, d in deps("open chrome and open spotify")], [[], []])

    def test_chain_follows_latest_shared_resource(self):
        self.assertEqual([d for _, d in deps("pause and open chrome and play")],
                         [[], ["c1"], ["c2"]])


class GateAliasTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.tools = mock.Mock()
        self.tools.call.return_value = (True, "ran")
        self.gate = ConfirmGate(self.tools, CONFIG, self.state, here=lambda: "dell")

    def test_alias_resolves_for_close_app(self):
        self.gate.call("close_app", {"name": "Word", "device": "arch"})
        [item] = ConfirmQueue(self.state).pending()
        self.assertEqual(item["command"]["device"], "zenbook")

    def test_local_alias_open_url_is_not_parked(self):
        self.assertEqual(self.gate.call("open_url", {"url": "https://example.com",
                                                      "device": "windows"}), (True, "ran"))
        self.assertEqual(ConfirmQueue(self.state).pending(), [])

    def test_remote_alias_open_url_is_parked_as_the_key(self):
        ok, _ = self.gate.call("open_url", {"url": "https://example.com", "device": "arch linux"})
        self.assertFalse(ok)
        [item] = ConfirmQueue(self.state).pending()
        self.assertEqual(item["command"]["device"], "zenbook")


def vol(percent, muted=False):
    return {"percent": percent, "muted": muted}


class RelativeVolumeTests(unittest.TestCase):
    P = {"kind": "volume", "level": "up"}

    def check(self, before_state, after_state, predicate=None):
        predicate = predicate or self.P
        before = observe.snapshot(predicate, lister=lambda: before_state)["before"]
        return observe.check(predicate, 0, lister=lambda: after_state, before=before)

    def test_changed_readback_is_observed(self):
        got = self.check(vol(40), vol(45))
        self.assertTrue(got["match"])
        self.assertNotIn("unverified", got)

    def test_no_op_press_is_unverified(self):
        got = self.check(vol(40), vol(40))
        self.assertFalse(got["match"])
        self.assertTrue(got["unverified"])

    def test_no_before_is_unverified(self):
        got = observe.check(self.P, 0, lister=lambda: vol(40))
        self.assertTrue(got["unverified"])

    def test_bounds_still_count(self):
        self.assertTrue(self.check(vol(100), vol(100))["match"])
        down = {"kind": "volume", "level": "down"}
        self.assertTrue(self.check(vol(0), vol(0), down)["match"])
        self.assertFalse(self.check(vol(100), vol(100), down)["match"])


class SkipTests(unittest.TestCase):
    P = {"kind": "playback", "command": "next"}

    def test_no_track_fingerprint_is_unverified(self):
        got = observe.check(self.P, 0, lister=lambda: {"status": "Playing"})
        self.assertFalse(got["match"])
        self.assertTrue(got["unverified"])

    def test_new_track_is_observed(self):
        before = observe.snapshot(self.P, lister=lambda: {"status": "Playing", "track": "a"})["before"]
        got = observe.check(self.P, 0, lister=lambda: {"status": "Playing", "track": "b"},
                            before=before)
        self.assertTrue(got["match"])


class ListerFailureTests(unittest.TestCase):
    def completed(self, rc=0, out="", err=""):
        return mock.Mock(returncode=rc, stdout=out, stderr=err)

    def test_hyprland_failures_are_none(self):
        for done in (self.completed(1, "[]"), self.completed(0, ""), self.completed(0, "not json")):
            with mock.patch.object(observe.shutil, "which", return_value="/x"), \
                    mock.patch.object(observe.subprocess, "run", return_value=done):
                self.assertIsNone(observe._windows_hyprland())

    def test_hyprland_empty_list_is_a_real_empty(self):
        with mock.patch.object(observe.shutil, "which", return_value="/x"), \
                mock.patch.object(observe.subprocess, "run",
                                  return_value=self.completed(0, "[]")):
            self.assertEqual(observe._windows_hyprland(), [])

    def test_sway_and_wmctrl_failures_are_none(self):
        with mock.patch.object(observe.shutil, "which", return_value="/x"), \
                mock.patch.object(observe, "_run", return_value=self.completed(1, "", "boom")):
            self.assertIsNone(observe._windows_sway())
            self.assertIsNone(observe._windows_wmctrl())
        with mock.patch.object(observe.shutil, "which", return_value="/x"), \
                mock.patch.object(observe, "_run", return_value=self.completed(0, "garbage")):
            self.assertIsNone(observe._windows_sway())

    def test_gone_on_unreadable_list_is_unverified_not_closed(self):
        got = observe.check({"kind": "gone", "app": "Spotify"}, 0, lister=lambda: None)
        self.assertFalse(got["match"])
        self.assertTrue(got["unverified"])

    def test_gone_on_real_empty_list_is_closed(self):
        got = observe.check({"kind": "gone", "app": "Spotify"}, 0, lister=lambda: [])
        self.assertTrue(got["match"])

    def test_now_survives_unreadable_list(self):
        with mock.patch.object(observe, "windows", return_value=None), \
                mock.patch.object(observe, "playback", return_value=None):
            self.assertIsNone(observe.now()["apps"])


class PipelineUnverifiedTests(unittest.TestCase):
    def test_unverified_check_stays_acknowledged(self):
        from jarvis import pipeline

        class Remote:
            def call(self, device, action, arg):
                return True, json.dumps({"match": False, "after_ms": 5, "app": None,
                                         "unverified": True})
        command = {"id": "c1", "intent": "close_app", "device": "dell", "args": {"name": "Spotify"}}
        self.assertEqual(pipeline._observe(command, Remote(), lambda *a, **k: None, "x"), {})


if __name__ == "__main__":
    unittest.main()
