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



class PublicOriginTests(FrontOriginTests):
    def test_dedicated_port_accepts_only_its_exact_own_origin(self):
        public = f"https://{DNS}:8444"
        server = self.server({"phone": {"public_origin": public, "front_origin": FRONT}})
        self.assertEqual(server._public_origin(), public)
        self.assertTrue(server.host_ok(f"{DNS}:8444"))
        self.assertTrue(server.host_ok(f"{DNS.upper()}:8444"))
        self.assertTrue(server.origin_ok({"Origin": public}))
        for port in (8443, 8445, 9119):
            self.assertFalse(server.host_ok(f"{DNS}:{port}"))
            self.assertFalse(server.origin_ok({"Origin": f"https://{DNS}:{port}"}))
        self.assertTrue(server.host_ok(DNS))
        self.assertTrue(server.host_ok(f"{DNS}:443"))
        self.assertTrue(server.origin_ok({"Origin": FRONT}))
        self.assertFalse(server.origin_ok({"Origin": public + "/"}))
        self.assertFalse(server.origin_ok({"Origin": "null"}))

    def test_bad_public_origins_grant_no_additional_host_or_origin(self):
        public = f"https://{DNS}:8444"
        for bad in (None, False, 8444, f"http://{DNS}:8444", f"https://{DNS}",
                    "https://other.ts.net:8444", "https://127.0.0.1:8444",
                    public + "/", public + "/phone", public + "?x=1", public + "#x",
                    public + "?", public + "#", public + "\x00",
                    public.replace("https://", "https://user:pass@"),
                    public.replace(":8444", ":0"), public.replace(":8444", ":65536"),
                    public.replace(":8444", ":08444"), public.replace(":8444", ":bad"),
                    " " + public, public + "\n", public.replace("https", "ht\ttps")):
            with self.subTest(public_origin=bad):
                server = self.server({"phone": {"public_origin": bad}})
                self.assertIsNone(server._public_origin())
                self.assertFalse(server.host_ok(f"{DNS}:8444"))
                self.assertFalse(server.origin_ok({"Origin": public}))
                self.assertFalse(server.host_ok("other.ts.net:8444"))


class FrontFromGatewayTests(FrontOriginTests):
    """No phone.front_origin: a managed PC takes the gateway host from its operator config."""

    def managed(self, origin, phone_cfg=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "operator.json"
        path.write_text(json.dumps({"origin": origin}))
        config = {"continuity": {"operator_config": str(path)}}
        if phone_cfg is not None:
            config["phone"] = phone_cfg
        return self.server(config)

    def test_front_derived_from_gateway(self):
        server = self.managed("https://oracle-vm.tailnet-example.ts.net:443")
        self.assertTrue(server.host_ok("oracle-vm.tailnet-example.ts.net:8443"))
        self.assertTrue(server.origin_ok({"Origin": FRONT}))
        self.assertFalse(server.origin_ok({"Origin": "https://oracle-vm.tailnet-example.ts.net"}))

    def test_front_port_and_off_switch(self):
        server = self.managed("https://oracle-vm.tailnet-example.ts.net", {"front_port": 9443})
        self.assertTrue(server.origin_ok({"Origin": "https://oracle-vm.tailnet-example.ts.net:9443"}))
        self.assertFalse(server.origin_ok({"Origin": FRONT}))
        server = self.managed("https://oracle-vm.tailnet-example.ts.net", {"front_origin": False})
        self.assertFalse(server.origin_ok({"Origin": FRONT}))

    def test_bad_gateway_origin_gives_no_front(self):
        for bad in ("http://oracle-vm.tailnet-example.ts.net", "nonsense", 5, None):
            server = self.managed(bad)
            self.assertFalse(server.origin_ok({"Origin": FRONT}), repr(bad))
        server = self.server({"continuity": {"operator_config": "/nonexistent/operator.json"}})
        self.assertFalse(server.origin_ok({"Origin": FRONT}))


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
