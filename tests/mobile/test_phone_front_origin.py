"""PhoneServer accepts the phone front's origin only when phone.front_origin names it."""

import json
import tempfile
import unittest
from pathlib import Path

from jarvis import phone

DNS = "archmain.tailnet-example.ts.net"
FRONT = "https://oracle-vm.tailnet-example.ts.net:8443"
STATUS = {"Self": {"ID": "nMAIN", "DNSName": DNS + ".", "HostName": "archmain", "TailscaleIPs": ["100.100.1.1"]}}


class FakeTailscale:
    def __call__(self, args, timeout=5):
        return (0, json.dumps(STATUS)) if args[:2] == ["status", "--json"] else (1, "")


class FrontOriginTests(unittest.TestCase):
    def server(self, config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = phone.PhoneServer(config, Path(tmp.name) / ".jarvis", port=0,
                                   tailnet=phone.Tailnet(run=FakeTailscale()), web=Path(tmp.name))
        self.addCleanup(server.close)
        return server

    def test_unchanged_without_config(self):
        server = self.server({})
        self.assertFalse(server.host_ok("oracle-vm.tailnet-example.ts.net:8443"))
        self.assertFalse(server.origin_ok({"Origin": FRONT}))
        self.assertTrue(server.host_ok(DNS))
        self.assertTrue(server.origin_ok({"Origin": f"https://{DNS}"}))
        self.assertTrue(server.origin_ok({}))

    def test_front_allowed_when_configured(self):
        server = self.server({"phone": {"front_origin": FRONT}})
        self.assertTrue(server.host_ok("oracle-vm.tailnet-example.ts.net:8443"))
        self.assertTrue(server.host_ok("Oracle-VM.tailnet-example.ts.net:8443"))
        self.assertFalse(server.host_ok("oracle-vm.tailnet-example.ts.net"))
        self.assertFalse(server.host_ok("evil.ts.net:8443"))
        self.assertTrue(server.origin_ok({"Origin": FRONT}))
        self.assertFalse(server.origin_ok({"Origin": FRONT + "/"}))
        self.assertFalse(server.origin_ok({"Origin": "https://evil.ts.net:8443"}))
        self.assertFalse(server.origin_ok({"Origin": "null"}))
        # the PC's own names still work
        self.assertTrue(server.host_ok(DNS))
        self.assertTrue(server.origin_ok({"Origin": f"https://{DNS}"}))

    def test_bad_front_origin_config_is_ignored(self):
        for bad in ("http://a1.ts.net:8443", "https://a1.ts.net/x", "https://", "x", 5, None, "https://a1.ts.net:xx"):
            server = self.server({"phone": {"front_origin": bad}})
            self.assertFalse(server.origin_ok({"Origin": "https://a1.ts.net:8443"}), repr(bad))
            self.assertFalse(server.host_ok("a1.ts.net:8443"), repr(bad))


if __name__ == "__main__":
    unittest.main()
