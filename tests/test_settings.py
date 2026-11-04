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
        with mock.patch("jarvis.release.served_code", return_value="abc"):  # the release nodes get
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


class SavedClusterTests(unittest.TestCase):
    def test_a_saved_main_shows_before_the_restart_and_no_broker_is_built(self):
        import json
        state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        settings._SAVED_CLUSTER.pop(str(state), None)
        self.addCleanup(settings._SAVED_CLUSTER.pop, str(state), None)
        path = state / "jarvis.config.json"
        path.write_text(json.dumps(CONFIG))
        config = json.loads(json.dumps(CONFIG))
        with mock.patch("jarvis.cluster_store.Broker", side_effect=AssertionError("DDL on every read")):
            settings.write(config, path, state, {"cluster.main_device": "dell"})
            view = settings.read(config, state)
        self.assertEqual(config["cluster"]["main_device"], "zenbook")  # running config unchanged
        row = {f["path"]: f for f in view["fields"]}["cluster.main_device"]
        self.assertEqual(row["value"], "dell")
        self.assertEqual({m["id"]: m["role"] for m in view["machines"]}["dell"], "main")

    def test_node_rows_read_the_file_without_creating_it(self):
        state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        broker = Broker(state)
        broker.enroll("dell")
        with mock.patch("jarvis.cluster_store.Broker", side_effect=AssertionError("no Broker")):
            self.assertEqual([n["id"] for n in settings._node_rows(state)], ["dell"])


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

    def test_refresh_brings_console_saves_into_another_process(self):
        import json
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        path = folder / "jarvis.config.json"
        path.write_text(json.dumps({"vault": {"path": "v", "write": True}, "voice": {"near_miss": 0.2}}))
        running = {"vault": {"path": "v", "write": True}, "voice": {"near_miss": 0.2}}
        vault_settings = running["vault"]  # what a Vault holds on to
        settings.refresh(running, path)
        path.write_text(json.dumps({"vault": {"path": "v", "write": False}, "voice": {"near_miss": 0.3}}))
        import os
        os.utime(path, ns=(1, 1))  # a different mtime, however fast the test runs
        self.assertTrue(settings.refresh(running, path))
        self.assertIs(running["vault"], vault_settings)
        self.assertFalse(vault_settings["write"])
        self.assertEqual(running["voice"]["near_miss"], 0.2)  # needs a restart: left alone
        self.assertFalse(settings.refresh(running, path))  # unchanged file: not reread


if __name__ == "__main__":
    unittest.main()
