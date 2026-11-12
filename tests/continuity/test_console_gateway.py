import json
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity import console


class Client:
    calls = 0
    def status(self):
        Client.calls += 1
        return {"mode": "LEGACY_ACTIVE", "epoch": 2, "main": None, "lease_valid": False,
                "private_key_path": "/secret/key", "authority_id": "x"}


class Boom:
    def status(self): raise RuntimeError("/home/u/.keys/owner.pem refused")


class GatewayLine(unittest.TestCase):
    def setUp(self):
        console._CACHE.clear(); Client.calls = 0
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "operator.json"
        self.cfg = {"continuity": {"operator_config": str(self.path)}}

    def test_missing_file_is_not_set_up(self):
        self.assertEqual(console.gateway_status(self.cfg, factory=lambda p: 1 / 0), {"state": "not_set_up"})

    def test_ok_returns_only_safe_fields_and_caches(self):
        self.path.write_text("{}")
        f = lambda p: (Client(), {"origin": "https://oracle-vm:8443/x", "private_key_path": "/secret/key"})
        r = console.gateway_status(self.cfg, factory=f, now=100)
        self.assertEqual(r, {"state": "ok", "host": "oracle-vm", "mode": "LEGACY_ACTIVE", "main": None,
                             "lease_valid": False, "epoch": 2})
        self.assertNotIn("secret", json.dumps(r))
        console.gateway_status(self.cfg, factory=f, now=120)
        self.assertEqual(Client.calls, 1)
        console.gateway_status(self.cfg, factory=f, now=131)
        self.assertEqual(Client.calls, 2)

    def test_errors_are_caught_and_scrubbed(self):
        self.path.write_text("{}")
        r = console.gateway_status(self.cfg, factory=lambda p: (Boom(), {"origin": "https://h"}), now=1)
        self.assertEqual(r, {"state": "unreachable", "host": "h", "error": "RuntimeError"})
        console._CACHE.clear()
        r = console.gateway_status(self.cfg, factory=lambda p: 1 / 0, now=1)
        self.assertEqual(r["state"], "unreachable")
        self.assertNotIn("division", json.dumps(r))

    def test_timeout(self):
        import threading
        self.path.write_text("{}")
        ev = threading.Event(); self.addCleanup(ev.set)
        class Slow:
            def status(self): ev.wait(5)
        r = console.gateway_status(self.cfg, factory=lambda p: (Slow(), {"origin": "https://h"}), now=1, timeout=0.1)
        self.assertEqual(r, {"state": "unreachable", "error": "Timeout"})


if __name__ == "__main__":
    unittest.main()
