import json
import tempfile
import unittest
from pathlib import Path

from jarvis.cluster_cli import roles, set_main

CONFIG = {"cluster": {"enabled": True, "main_device": "dell", "local_device": "dell"},
          "devices": {"dell": {"label": "Dell"}, "zenbook": {"label": "Zenbook"}, "mac": {}}}


class RoleTests(unittest.TestCase):
    def test_exactly_one_main(self):
        found = roles(CONFIG)["devices"]
        self.assertEqual([k for k, v in found.items() if v["role"] == "main"], ["dell"])
        self.assertTrue(found["dell"]["this_pc"])

    def test_set_main_moves_the_role_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jarvis.config.json"
            out = set_main(CONFIG, path, "zenbook")
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["cluster"]["main_device"], "zenbook")
        self.assertEqual(out["devices"]["dell"]["role"], "node")
        self.assertEqual(sum(v["role"] == "main" for v in out["devices"].values()), 1)

    def test_unknown_or_missing_device_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            for bad in ("laptop", None):
                with self.assertRaises(ValueError):
                    set_main(CONFIG, path, bad)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
