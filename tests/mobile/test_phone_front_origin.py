"""PhoneServer accepts the phone front's origin only when phone.front_origin names it."""

import json
import tempfile
import unittest
from pathlib import Path

from jarvis import phone

DNS = "archmain.tailnet-example.ts.net"
FRONT = "https://oracle-vm.tailnet-example.ts.net:8443"
STATUS = {"Self": {"ID": "nMAIN", "DNSName": DNS + ".", "HostName": "archmain", "TailscaleIPs": ["100.100.1.1"]}}


ORACLE_IP, PHONE_IP, OTHER_IP = "100.64.0.12", "100.64.0.14", "100.70.0.9"
LOGIN = "owner@example.com"


def _whois(name, stable, os_name, login=LOGIN):
    return {"Node": {"Name": name + ".", "StableID": stable, "Hostinfo": {"OS": os_name, "Hostname": name.split(".")[0]},
                     "Addresses": []}, "UserProfile": {"LoginName": login}}


WHOIS = {ORACLE_IP: _whois("oracle-vm.tailnet-example.ts.net", "nORACLE", "linux"),
         PHONE_IP: _whois("iphone-1.tailnet-example.ts.net", "nPHONE", "iOS"),
         OTHER_IP: _whois("friendpc.tailnet-example.ts.net", "nFRIEND", "linux", "friend@example.com")}


class FakeTailscale:
    def __call__(self, args, timeout=5):
        if args[:2] == ["status", "--json"]:
            return 0, json.dumps(STATUS)
        if args[:2] == ["whois", "--json"] and args[2] in WHOIS:
            return 0, json.dumps(WHOIS[args[2]])
        return 1, ""


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



class FrontIdentityTests(FrontOriginTests):
    """Through the front, the hop is the Oracle node; the phone is named in the front's
    X-Jarvis-Front-For/-Login, and this PC checks that name with its own whois."""

    def front(self, **extra):
        return {"X-Forwarded-For": ORACLE_IP, "Tailscale-User-Login": LOGIN, **extra}

    def test_phone_behind_front_is_identified(self):
        server = self.server({"phone": {"front_origin": FRONT}})
        who = server.identify(self.front(**{"X-Jarvis-Front-For": PHONE_IP,
                                            "X-Jarvis-Front-Login": LOGIN}))
        self.assertEqual(who["stable_id"], "nPHONE")

    def test_front_without_phone_name_is_refused(self):
        server = self.server({"phone": {"front_origin": FRONT}})
        with self.assertRaises(phone.Reply) as caught:
            server.identify(self.front())
        self.assertEqual(caught.exception.status, 401)

    def test_front_cannot_invent_a_login(self):
        server = self.server({"phone": {"front_origin": FRONT}})
        with self.assertRaises(phone.Reply) as caught:
            server.identify(self.front(**{"X-Jarvis-Front-For": OTHER_IP,
                                          "X-Jarvis-Front-Login": LOGIN}))
        self.assertEqual(caught.exception.status, 403)

    def test_front_headers_from_any_other_hop_are_ignored(self):
        server = self.server({"phone": {"front_origin": FRONT}})
        who = server.identify({"X-Forwarded-For": PHONE_IP, "Tailscale-User-Login": LOGIN,
                               "X-Jarvis-Front-For": OTHER_IP,
                               "X-Jarvis-Front-Login": "friend@example.com"})
        self.assertEqual(who["stable_id"], "nPHONE")

    def test_without_front_config_the_oracle_is_just_a_device(self):
        server = self.server({})
        who = server.identify(self.front(**{"X-Jarvis-Front-For": PHONE_IP,
                                            "X-Jarvis-Front-Login": LOGIN}))
        self.assertEqual(who["stable_id"], "nORACLE")


if __name__ == "__main__":
    unittest.main()
