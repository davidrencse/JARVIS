import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import settings
from jarvis.cluster_store import Broker

CONFIG = {"cluster": {"enabled": True, "main_device": "zenbook", "local_device": "zenbook"},
          "devices": {"zenbook": {"hostname": "arch", "label": "Zenbook"},
                      "dell": {"hostname": "WinLaptop", "label": "Dell", "transport": "cluster"},
                      "pi": {"hostname": "pi", "transport": "cluster"}}}


class MachineStatusTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)

    def test_main_reports_paired_online_and_code(self):
        broker = Broker(self.state)
        broker.enroll("dell")
        broker.heartbeat("dell", {"code": "abc"})
        with mock.patch("jarvis.deploy.code_fingerprint", return_value="abc"):
            out = {m["id"]: m for m in settings.machines(CONFIG, self.state)}
        self.assertEqual(out["zenbook"]["role"], "main")
        self.assertTrue(out["zenbook"]["this"])
        self.assertNotIn("paired", out["zenbook"])
        self.assertTrue(out["dell"]["paired"] and out["dell"]["online"] and out["dell"]["same_code"])
        self.assertFalse(out["pi"]["paired"])  # configured but never invited
        self.assertEqual(out["pi"]["label"], "pi")

    def test_node_shows_roles_only(self):
        node = {**CONFIG, "cluster": {**CONFIG["cluster"], "local_device": "dell"}}
        out = {m["id"]: m for m in settings.machines(node, self.state)}
        self.assertTrue(out["dell"]["this"])
        self.assertNotIn("paired", out["dell"])
        self.assertFalse((self.state / "cluster.sqlite3").exists())  # never creates a hub database


class RestartMarkerTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.path = self.state / "jarvis.config.json"

    def save(self, config, value):
        import json
        self.path.write_text(json.dumps(config))
        with mock.patch("jarvis.settings.read", return_value={}):
            return settings.write(config, self.path, self.state, {"voice.max_spoken_chars": value})

    def test_a_new_value_asks_for_a_restart(self):
        out = self.save({"voice": {"max_spoken_chars": 350}}, 400)
        self.assertEqual(out["restart"], ["voice.max_spoken_chars"])

    def test_saving_the_same_value_does_not(self):
        out = self.save({"voice": {"max_spoken_chars": 350}}, 350)
        self.assertEqual(out["saved"], ["voice.max_spoken_chars"])
        self.assertEqual(out["restart"], [])


if __name__ == "__main__":
    unittest.main()
