import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.cluster_cli import roles, set_main

CONFIG = {"cluster": {"enabled": True, "main_device": "dell", "local_device": "dell"},
          "devices": {"dell": {"label": "Dell"}, "zenbook": {"label": "Zenbook"}, "mac": {}}}


class _Bound:
    def __init__(self, target):
        self.target = target
        self.phase = "before"

    def candidate_readiness(self, device):
        return {"control_ready": True, "ai_ready": False}

    def authority_status(self):
        main = "dell" if self.phase == "before" else self.target
        body = {"mode": "V2_ACTIVE", "lease_valid": True, "main_ready": True, "main": main,
                "epoch": 2, "incarnation": 1, "grant_barrier": 0, "transition": None}
        if self.phase == "after":
            body["transition"] = {"state": "SUCCEEDED", "candidate": self.target,
                                  "transition_id": "t-role", "action": "set_preferred_main"}
        return body

    def __call__(self, old, new, config, base):
        self.phase = "after"
        self.target = new
        return {"ok": True, "status": "ready", "main": new, "transition_id": "t-role",
                "epoch": 2, "action": "set_preferred_main"}


def _bound(target):
    return _Bound(target)


class RoleTests(unittest.TestCase):
    def test_exactly_one_main(self):
        found = roles(CONFIG)["devices"]
        self.assertEqual([k for k, v in found.items() if v["role"] == "main"], ["dell"])
        self.assertTrue(found["dell"]["this_pc"])

    def test_set_main_moves_the_role_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jarvis.config.json"
            path.write_text(json.dumps(CONFIG), encoding="utf-8")
            with mock.patch("jarvis.cluster_cli._bound_handoff", return_value=_bound("zenbook")):
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
            path.write_text(json.dumps(CONFIG), encoding="utf-8")
            before = path.read_bytes()
            with mock.patch("jarvis.cluster_cli._bound_handoff", return_value=_bound("ghost")):
                with self.assertRaises(ValueError) as caught:
                    set_main(CONFIG, path, "ghost")
            self.assertIn("No device", str(caught.exception))
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
