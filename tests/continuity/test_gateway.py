"""HTTP authority gateway. Auth, fencing, and metadata limits — not a live failover drill.

Run: .venv/bin/python -m unittest tests.continuity.test_gateway
"""

import hashlib
import inspect
import json
import math
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis.continuity.authority import AuthorityError, AuthorityStore
from jarvis.continuity.gateway import (
    DEVICE_NS, HTTP_STATUS, AuthenticatedPort, AuthorityClient, ContinuityGateway, DevicePrincipal,
    GatewayError, TICKET_NS, _Record, _agreed_wait, _message, _post_json, canonical, pin_origin,
    pin_relay, sign_message, verify_message,
)
from jarvis.continuity.ingress import ContinuityGatewaySession, IngressError
from jarvis.continuity.integration import (
    INCOMPATIBILITIES, IntegrationError, peer_report,
)
from jarvis.continuity.runtime import GatewayPort, MainRuntime
from tests.continuity._fleet import Clock, digest


PC = ["status", "lease", "fence", "candidate", "consume", "reconcile"]
SERVER = ["status", "fence", "reconcile"]
CLIENT = ["status", "client"]
SECRET = "do not log this prompt body"
_KEYS = None


def _keys():
    global _KEYS
    if _KEYS is not None:
        return _KEYS
    folder = Path(tempfile.mkdtemp(prefix="jarvis-continuity-keys-"))
    made = {}
    for name in ("owner", "zenbook", "dell", "oracle", "phone", "stranger"):
        path = folder / name
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-continuity-test", "-f", str(path)],
            check=True, capture_output=True, timeout=10,
        )
        token = (folder / f"{name}.pub").read_text(encoding="ascii").split()
        made[name] = (f"{token[0]} {token[1]}", path)
    _KEYS = made
    return made


def _exchange(origin, path, obj):
    request = urllib.request.Request(origin + path, data=canonical(obj), method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            raw = response.read(8192)
            return response.status, response.headers, json.loads(raw.decode())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(8192)
            return exc.code, exc.headers, json.loads(raw.decode())
        finally:
            exc.close()


class Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        gateway = self.server
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw.decode())
        gateway.bodies.append(body)
        stolen = body.get("operation_id") == "op-redirect"
        if stolen:
            self.send_response(302)
            self.send_header("Location", gateway.stolen)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        reply = json.dumps({"operation_id": body["operation_id"], "accepted": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, fmt, *args):
        return


class Stolen(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits += 1
        self.send_response(200)
        self.end_headers()

    do_POST = do_GET

    def log_message(self, fmt, *args):
        return


def _serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _stop(httpd):
    httpd.shutdown()
    httpd.server_close()


class Pins(unittest.TestCase):
    def test_package_import_does_not_export_or_listen(self):
        import jarvis.continuity as package
        self.assertFalse(hasattr(package, "ContinuityGateway"))

    def test_origins_reject_off_box_and_userinfo(self):
        self.assertEqual(pin_origin("http://127.0.0.1:9/"), "http://127.0.0.1:9")
        for bad in ("http://example.com:9", "http://127.0.0.1:9/relay", "http://user:pw@127.0.0.1:9",
                    "http://127.0.0.1:9?q=1", "http://localhost:9", "file:///tmp/x", "http://127.0.0.1"):
            with self.assertRaises(GatewayError):
                pin_origin(bad)
        with self.assertRaises(GatewayError):
            pin_relay("https://127.0.0.1:9")
        with self.assertRaises(GatewayError):
            pin_relay("http://10.1.2.3:9")
        self.assertEqual(pin_relay("https://zen.tail.ts.net:443"), "https://zen.tail.ts.net:443")
        for bad in ("http://zen.tail.ts.net:80", "https://1.2.3.4:443", "https://[fd7a::1]:443",
                    "https://u@zen.tail.ts.net:443", "https://zen.tail.ts.net:443/?a=1",
                    "https://zen.tail.ts.net:443#f", "https://evilts.net:443", "https://ts.net:443",
                    "https://ts.net.evil.com:443", "https://zen.tail.ts.net:443/relay"):
            with self.assertRaises(GatewayError, msg=bad):
                pin_relay(bad)
        with self.assertRaises(GatewayError):
            AuthorityClient("http://example.com:443", authority_id="oracle-auth")

    def test_redirect_and_mismatch_are_not_followed(self):
        stolen = _serve(Stolen)
        stolen.hits = 0
        port = stolen.server_address[1]
        decoy = _serve(Stolen)
        decoy.hits = 0

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        redirect = _serve(Redirect)
        try:
            with self.assertRaises(GatewayError) as raised:
                _post_json(f"http://127.0.0.1:{redirect.server_address[1]}/v1/continuity/relay", b"{}", 100)
            self.assertEqual(raised.exception.code, "redirect")
            self.assertEqual(stolen.hits, 0)
            client = AuthorityClient(
                f"http://127.0.0.1:{redirect.server_address[1]}", authority_id="oracle-auth",
                device_id="phone", installation_id="phone-inst", private_key_path=_keys()["phone"][1])
            with self.assertRaises(GatewayError) as raised:
                client.status()
            self.assertEqual(raised.exception.code, "redirect")
            self.assertEqual(stolen.hits, 0)
        finally:
            _stop(redirect)
            _stop(stolen)
            _stop(decoy)

    def test_code_signature_is_not_a_device_signature(self):
        private = _keys()["zenbook"][1]
        data = _message(DEVICE_NS, {"v": 1})
        signed = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-q", "-f", str(private), "-n", "jarvis-code"],
            input=data, capture_output=True, timeout=10, check=True,
        )
        from jarvis.continuity.gateway import verify_message
        self.assertFalse(verify_message(DEVICE_NS, data, signed.stdout.decode("ascii"), _keys()["zenbook"][0]))
        self.assertTrue(verify_message(DEVICE_NS, data, sign_message(DEVICE_NS, private, data), _keys()["zenbook"][0]))

    def test_required_store_codes_keep_their_http_status(self):
        for code, status in (("bad_request", 400), ("bad_ticket", 400), ("ticket_expired", 410),
                             ("stale", 409), ("busy", 409), ("key_reused", 409), ("conflict", 409),
                             ("not_quiet", 409), ("lease_live", 409), ("quarantined", 503)):
            self.assertEqual(HTTP_STATUS[code], status)
        # A missing admission is 404, and integration.EXTRA_HTTP now agrees.
        self.assertEqual(HTTP_STATUS["not_found"], 404)

    def test_peer_names_match_and_call_shapes_do_not(self):
        peers = peer_report()
        for name in ("gateway", "ingress", "runtime", "executor"):
            self.assertTrue(peers[name]["present"])
            self.assertTrue(peers[name]["compatible"])
            self.assertEqual(peers[name]["missing"], [])
        # Name compatibility is not a cutover: the static list still names what is missing.
        stale = {row["id"] for row in INCOMPATIBILITIES}
        self.assertNotIn("peers_absent", stale)
        self.assertIn("no_signatures", stale)
        self.assertIn("no_passkey", stale)
        gateway_ack = list(inspect.signature(ContinuityGateway.acknowledge).parameters)
        port_ack = list(inspect.signature(AuthenticatedPort.acknowledge).parameters)
        self.assertEqual(gateway_ack, ["self", "principal", "payload"])
        self.assertEqual(port_ack, ["self", "device_id", "installation_id", "mode_version",
                                    "fenced_epoch", "boot_id"])
        self.assertIn("ticket_signature", inspect.getsource(AuthenticatedPort.consume))
        # Behavior, not names: the real peers carry the new transient channel.
        self.assertTrue(callable(getattr(ContinuityGateway, "forward_transient", None)))

    def test_real_gateway_refuses_personal_keys_and_stays_legacy(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            gateway = ContinuityGateway.open(
                Path(folder) / "authority.sqlite", "home", "oracle-auth", [_keys()["owner"][0]],
                monotonic=clock.mono, wall=clock.wall)
            try:
                origin = gateway.serve()
                owner = AuthorityClient(origin, authority_id="oracle-auth", wall=clock.wall,
                                        owner_private_key_path=_keys()["owner"][1])
                zen = AuthorityClient(origin, authority_id="oracle-auth", wall=clock.wall,
                                      device_id="zenbook", installation_id="zenbook-inst",
                                      private_key_path=_keys()["zenbook"][1])
                owner.enroll({"device_id": "zenbook", "installation_id": "zenbook-inst", "kind": "pc",
                              "public_key": _keys()["zenbook"][0], "capabilities": list(PC)})
                for key in ("secret", "prompt"):
                    with self.assertRaises(GatewayError) as caught:
                        zen._device("acknowledge", {"mode_version": 1, "fenced_epoch": 0, key: "x"})
                    self.assertEqual(caught.exception.code, "bad_request")
                status = zen.status()
                self.assertEqual(status["mode"], "LEGACY_ACTIVE")
                self.assertIsNone(status["main"])
            finally:
                gateway.close()


class GatewayCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.db = Path(self.tmp.name) / "authority.sqlite"
        self.gw = ContinuityGateway.open(
            self.db, "home", "oracle-auth", [_keys()["owner"][0]],
            monotonic=self.clock.mono, wall=self.clock.wall,
        )
        self.addCleanup(self.gw.close)
        self.assertIsNone(self.gw.listening)
        self.origin = self.gw.serve()
        self.assertTrue(self.origin.startswith("http://127.0.0.1:"))
        self.owner = self._client(owner=True)

    def _client(self, name=None, *, owner=False, authority_id="oracle-auth"):
        keys = _keys()
        kwargs = {"authority_id": authority_id, "wall": self.clock.wall}
        if name is not None:
            kwargs.update(device_id=name, installation_id=name + "-inst", private_key_path=keys[name][1])
        if owner:
            kwargs["owner_private_key_path"] = keys["owner"][1]
        return AuthorityClient(self.origin, **kwargs)

    def enroll(self, name, kind, caps, relay=None):
        record = {"device_id": name, "installation_id": name + "-inst", "kind": kind,
                  "public_key": _keys()[name][0], "capabilities": list(caps)}
        if relay is not None:
            record["relay_origin"] = relay
        return self.owner.enroll(record)

    def cutover(self, relay=None):
        self.enroll("zenbook", "pc", PC, relay)
        self.enroll("dell", "pc", PC)
        self.enroll("oracle", "server", SERVER)
        zen = self._client("zenbook")
        version = zen.status()["mode_version"]
        version = self.owner.set_mode("PREPARING_V2", version)["mode_version"]
        version = self.owner.set_mode("QUIESCED", version)["mode_version"]
        for name in ("zenbook", "dell", "oracle"):
            self._client(name).acknowledge(version + 1, 0)
        self.owner.set_mode("V2_ONLY_NO_MAIN", version)
        return zen

    def promote(self, candidate, key):
        status = self._client(candidate).status()
        transition = self.owner.transition_request(
            key, "recover", candidate, status["incarnation"], status["epoch"], status["policy_version"])
        tid = transition["transition_id"]
        self._client(candidate).readiness(tid, True, False, "cp-1")
        self.owner.step(tid, "drain")
        self.owner.step(tid, "fence")
        lease = self.owner.step(tid, "grant")
        done = self.owner.step(tid, "complete", True)
        return tid, lease, done


class Legacy(GatewayCase):
    def test_fresh_gateway_stays_legacy_and_does_not_listen_until_serve(self):
        self.enroll("zenbook", "pc", PC)
        status = self._client("zenbook").status()
        self.assertEqual(status["mode"], "LEGACY_ACTIVE")
        self.assertIsNone(status["main"])
        self.assertEqual(status["authority_id"], "oracle-auth")

    def test_bind_refuses_wildcards_names_and_unconfirmed_lan(self):
        for host in ("0.0.0.0", "::", "example.com", "10.1.2.3", "localhost"):
            with self.assertRaises(GatewayError) as raised:
                self.gw.serve(host)
            self.assertEqual(raised.exception.code, "refused_bind")
        with self.assertRaises(GatewayError) as raised:
            self.gw.serve("0.0.0.0", confirm_bind=True)
        self.assertEqual(raised.exception.code, "refused_bind")
        self.assertTrue(self.gw.listening.startswith("http://127.0.0.1:"))

    def test_transition_and_direct_v2_are_refused_while_legacy(self):
        self.enroll("zenbook", "pc", PC)
        status = self._client("zenbook").status()
        with self.assertRaises(GatewayError) as raised:
            self.owner.transition_request(
                "k1", "recover", "zenbook", status["incarnation"], status["epoch"], status["policy_version"])
        self.assertEqual(raised.exception.code, "mode")
        with self.assertRaises(GatewayError) as raised:
            self.owner.set_mode("V2_ACTIVE", status["mode_version"])
        self.assertEqual(raised.exception.code, "bad_request")
        self.assertEqual(self._client("zenbook").status()["mode"], "LEGACY_ACTIVE")

    def test_passkey_flag_and_personal_field_do_not_promote_or_get_logged(self):
        self.enroll("zenbook", "pc", PC)
        status = self._client("zenbook").status()
        payload = {"client_key": "k1", "action": "recover", "candidate": "zenbook",
                   "expected_incarnation": status["incarnation"], "expected_epoch": status["epoch"],
                   "expected_policy": status["policy_version"], "profile": "control",
                   "passkey": True, "prompt": SECRET}
        digest = __import__("jarvis.continuity.gateway", fromlist=["payload_digest"]).payload_digest(payload)
        challenge = self.gw.issue_challenge(digest)
        issued = int(self.clock.w)
        signature = sign_message("jarvis-continuity-owner-v1", _keys()["owner"][1], _message(
            "jarvis-continuity-owner-v1", {
                "v": 1, "challenge_id": challenge["challenge_id"], "nonce": "n-personal",
                "issued_at": issued, "payload_digest": digest,
            }))
        status_code, _headers, body = _exchange(self.origin, "/v1/authority/transitions", {
            "payload": payload, "owner": {"challenge_id": challenge["challenge_id"], "nonce": "n-personal",
                                           "issued_at": issued, "signature": signature},
        })
        self.assertEqual(status_code, 400)
        self.assertEqual(body, {"error": "bad_request"})
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn(SECRET, json.dumps(self.gw.audit_events()))
        self.assertEqual(self._client("zenbook").status()["mode"], "LEGACY_ACTIVE")

    def test_unsigned_renew_and_oversized_body_are_refused(self):
        self.enroll("zenbook", "pc", PC)
        code, _headers, body = _exchange(self.origin, "/v1/authority/renew", {
            "payload": {"holder": "zenbook", "lease_id": "ls-x", "incarnation": 1, "epoch": 0},
            "device": {"device_id": "zenbook", "installation_id": "zenbook-inst", "nonce": "n-nope",
                       "issued_at": int(self.clock.w), "signature": "nope"},
        })
        self.assertEqual((code, body["error"]), (400, "bad_request"))
        self.assertNotIn(str(self.db), json.dumps(body))
        raw = (
            "POST /v1/authority/status HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
            "Content-Length: 999999\r\nConnection: close\r\n\r\n"
        ).encode()
        port = int(self.origin.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(raw)
            reply = sock.recv(1024)
        self.assertIn(b"400", reply.split(b"\r\n", 1)[0])
        self.assertNotIn(b"sqlite", reply)
        self.assertNotIn(str(self.db).encode(), reply)

    def test_second_writer_is_refused(self):
        with self.assertRaises(GatewayError) as raised:
            ContinuityGateway.open(self.db, "home", "oracle-auth", [_keys()["owner"][0]])
        self.assertEqual(raised.exception.code, "writer_busy")

    def test_relay_before_v2_does_not_connect(self):
        upstream = _serve(Upstream)
        upstream.bodies = []
        upstream.stolen = "http://127.0.0.1:1/stolen"
        self.addCleanup(_stop, upstream)
        origin = f"http://127.0.0.1:{upstream.server_address[1]}"
        self.enroll("zenbook", "pc", PC, origin)
        self.enroll("phone", "client", CLIENT)
        with self.assertRaises(GatewayError) as raised:
            self._client("phone").relay("op-legacy", "dell", "a-1", digest("open firefox"), "status")
        self.assertEqual(raised.exception.code, "no_relay")
        self.assertEqual(raised.exception.operation_id, "op-legacy")
        self.assertEqual(upstream.bodies, [])

    def test_wrong_authority_id_is_not_treated_as_this_fleet(self):
        self.enroll("zenbook", "pc", PC)
        client = self._client("zenbook", authority_id="other-auth")
        with self.assertRaises(GatewayError) as raised:
            client.status()
        self.assertEqual(raised.exception.code, "authority_mismatch")

    def test_close_stops_the_listener(self):
        port = int(self.origin.rsplit(":", 1)[1])
        self.gw.shutdown()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        self.assertIsNone(self.gw.listening)


class Authentication(GatewayCase):
    def test_replay_skew_and_wrong_key_fail_closed(self):
        self.enroll("zenbook", "pc", PC)
        client = self._client("zenbook")
        first = client.status()
        self.assertEqual(first["mode"], "LEGACY_ACTIVE")
        payload = {}
        issued = int(self.clock.w)
        nonce = "n-once"
        from jarvis.continuity.gateway import payload_digest
        digest_value = payload_digest(payload)
        signature = sign_message(DEVICE_NS, _keys()["zenbook"][1], _message(DEVICE_NS, {
            "v": 1, "device_id": "zenbook", "installation_id": "zenbook-inst", "nonce": nonce,
            "issued_at": issued, "purpose": "status", "payload_digest": digest_value,
        }))
        envelope = {"payload": payload, "device": {
            "device_id": "zenbook", "installation_id": "zenbook-inst", "nonce": nonce,
            "issued_at": issued, "signature": signature,
        }}
        code, _headers, body = _exchange(self.origin, "/v1/authority/status", envelope)
        self.assertEqual(code, 200, body)
        code, _headers, body = _exchange(self.origin, "/v1/authority/status", envelope)
        self.assertEqual((code, body), (401, {"error": "unauthorized"}))
        stale = json.loads(json.dumps(envelope))
        stale["device"] = dict(envelope["device"], nonce="n-stale", issued_at=issued - 31)
        stale["device"]["signature"] = sign_message(DEVICE_NS, _keys()["zenbook"][1], _message(DEVICE_NS, {
            "v": 1, "device_id": "zenbook", "installation_id": "zenbook-inst", "nonce": "n-stale",
            "issued_at": issued - 31, "purpose": "status", "payload_digest": digest_value,
        }))
        code, _headers, body = _exchange(self.origin, "/v1/authority/status", stale)
        self.assertEqual((code, body["error"]), (401, "unauthorized"))
        forged = json.loads(json.dumps(envelope))
        forged["device"] = dict(envelope["device"], nonce="n-forged")
        forged["device"]["signature"] = sign_message(DEVICE_NS, _keys()["stranger"][1], _message(DEVICE_NS, {
            "v": 1, "device_id": "zenbook", "installation_id": "zenbook-inst", "nonce": "n-forged",
            "issued_at": issued, "purpose": "status", "payload_digest": digest_value,
        }))
        code, _headers, _body = _exchange(self.origin, "/v1/authority/status", forged)
        self.assertEqual(code, 401)

    def test_capability_and_holder_are_device_bound(self):
        with self.assertRaises(GatewayError) as raised:
            self.enroll("oracle", "server", ["status", "candidate"])
        self.assertEqual(raised.exception.code, "bad_request")
        with self.assertRaises(GatewayError) as raised:
            self.enroll("phone", "client", ["status", "manage_main"])
        self.assertEqual(raised.exception.code, "bad_request")
        self.enroll("oracle", "server", SERVER)
        with self.assertRaises(GatewayError) as raised:
            self._client("oracle").renew("oracle", "ls-nope", 1, 0)
        self.assertEqual(raised.exception.code, "forbidden")
        self.cutover()
        _tid, lease, _done = self.promote("zenbook", "k-boot")
        with self.assertRaises(GatewayError) as raised:
            self._client("dell").renew("zenbook", lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.assertEqual(raised.exception.code, "forbidden")
        view = self._client("zenbook").renew("zenbook", lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.assertEqual(view["valid_for"], 15.0)
        self.assertNotIn(lease["lease_id"], json.dumps(self.gw.audit_events()))

    def test_owner_challenge_is_single_use_and_principal_does_not_travel(self):
        self.enroll("zenbook", "pc", PC)
        status = self._client("zenbook").status()
        payload = {"client_key": "k-once", "action": "recover", "candidate": "zenbook",
                   "expected_incarnation": status["incarnation"], "expected_epoch": status["epoch"],
                   "expected_policy": status["policy_version"], "profile": "control"}
        from jarvis.continuity.gateway import payload_digest
        digest_value = payload_digest(payload)
        challenge = self.gw.issue_challenge(digest_value)
        issued = int(self.clock.w)
        signature = sign_message("jarvis-continuity-owner-v1", _keys()["owner"][1], _message(
            "jarvis-continuity-owner-v1", {
                "v": 1, "challenge_id": challenge["challenge_id"], "nonce": "n-owner",
                "issued_at": issued, "payload_digest": digest_value,
            }))
        envelope = {"payload": payload, "owner": {
            "challenge_id": challenge["challenge_id"], "nonce": "n-owner",
            "issued_at": issued, "signature": signature,
        }}
        code, _headers, body = _exchange(self.origin, "/v1/authority/transitions", envelope)
        self.assertEqual(code, 409, body)  # legacy mode, after the proof is accepted
        self.assertEqual(body["error"], "mode")
        code, _headers, body = _exchange(self.origin, "/v1/authority/transitions", envelope)
        self.assertEqual((code, body["error"]), (401, "unauthorized"))
        proof = self.gw.authenticate_device(
            device_id="zenbook", installation_id="zenbook-inst", nonce="n-local", issued_at=issued,
            purpose="status", payload={}, signature=sign_message(DEVICE_NS, _keys()["zenbook"][1], _message(
                DEVICE_NS, {"v": 1, "device_id": "zenbook", "installation_id": "zenbook-inst",
                            "nonce": "n-local", "issued_at": issued, "purpose": "status",
                            "payload_digest": payload_digest({})})))
        self.gw.status(proof, {})
        with self.assertRaises(GatewayError) as raised:
            self.gw.status(proof, {})
        self.assertEqual(raised.exception.code, "unauthorized")
        other = ContinuityGateway.open(
            Path(self.tmp.name) / "other.sqlite", "home", "oracle-auth", [_keys()["owner"][0]],
            monotonic=self.clock.mono, wall=self.clock.wall)
        self.addCleanup(other.close)
        forged = DevicePrincipal(other._token, "zenbook", "zenbook-inst", "pc", frozenset({"status"}),
                                 "status", payload_digest({}))
        with self.assertRaises(GatewayError) as raised:
            self.gw.status(forged, {})
        self.assertEqual(raised.exception.code, "unauthorized")


class Removal(GatewayCase):
    """Owner-proven participant removal. A removed installation stays out until the store re-enables it."""

    def _disable_raw(self, payload, *, sign_payload=None, key="owner", nonce="n-rm"):
        from jarvis.continuity.gateway import _OWNER_PATH, payload_digest
        signed = payload if sign_payload is None else sign_payload
        digest_value = payload_digest(signed)
        challenge = self.gw.issue_challenge(digest_value)
        issued = int(self.clock.w)
        signature = sign_message("jarvis-continuity-owner-v1", _keys()[key][1], _message(
            "jarvis-continuity-owner-v1", {
                "v": 1, "challenge_id": challenge["challenge_id"], "nonce": nonce,
                "issued_at": issued, "payload_digest": digest_value,
            }))
        envelope = {"payload": payload, "owner": {
            "challenge_id": challenge["challenge_id"], "nonce": nonce, "issued_at": issued,
            "signature": signature,
        }}
        return envelope, _exchange(self.origin, _OWNER_PATH["disable"], envelope)

    def _still_enrolled(self, name):
        """Eligible means the store has not revoked it: its ack is accepted, not `revoked`."""
        status = self._client("zenbook").status()
        return self._client(name).acknowledge(status["mode_version"], 0)

    def test_owner_proof_removal_works_over_http_and_is_idempotent(self):
        self.cutover()
        self.promote("zenbook", "k-boot")
        payload = {"device_id": "dell", "installation_id": "dell-inst"}
        _envelope, (code, headers, body) = self._disable_raw(payload)
        self.assertEqual(code, 200, body)
        self.assertEqual((body["device_id"], body["installation_id"]), ("dell", "dell-inst"))
        self.assertTrue(body["disabled"])
        self.assertFalse(body["eligible"])
        self.assertFalse(body["already"])
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn(str(self.db), json.dumps(body))
        again = self.owner.disable_participant("dell", "dell-inst")
        self.assertTrue(again["already"])
        self.assertEqual(self._client("zenbook").status()["main"], "zenbook")

    def test_removed_participant_cannot_acknowledge_or_be_given_a_lease(self):
        self.cutover()
        self.promote("zenbook", "k-boot")
        status = self._client("zenbook").status()
        self.owner.disable_participant("dell", "dell-inst")
        with self.assertRaises(GatewayError) as acked:
            self._client("dell").acknowledge(status["mode_version"], status["epoch"])
        self.assertEqual((acked.exception.code, acked.exception.status), ("revoked", 403))
        with self.assertRaises(GatewayError) as moved:
            self.owner.transition_request(
                "k-to-dell", "make_main_now", "dell", status["incarnation"], status["epoch"],
                status["policy_version"])
        self.assertEqual((moved.exception.code, moved.exception.status), ("ineligible", 409))
        with self.assertRaises(GatewayError) as took:
            self.owner.transition_request(
                "k-rec-dell", "recover", "dell", status["incarnation"], status["epoch"],
                status["policy_version"])
        self.assertEqual(took.exception.code, "ineligible")
        self.assertEqual(took.exception.status, 409)
        after = self._client("zenbook").status()
        self.assertEqual((after["main"], after["lease_holder"], after["transition"]),
                         ("zenbook", "zenbook", None))
        # Another enrolled device is untouched.
        self.assertEqual(self._still_enrolled("oracle")["device_id"], "oracle")

    def test_removed_participant_stays_removed_after_restart_and_re_enroll(self):
        self.cutover()
        self.owner.disable_participant("dell", "dell-inst")
        self.gw.close()
        self.gw = ContinuityGateway.open(
            self.db, "home", "oracle-auth", [_keys()["owner"][0]],
            monotonic=self.clock.mono, wall=self.clock.wall)
        self.addCleanup(self.gw.close)
        self.origin = self.gw.serve()
        self.owner = self._client(owner=True)
        for name, kind, caps in (("zenbook", "pc", PC), ("dell", "pc", PC), ("oracle", "server", SERVER)):
            self.enroll(name, kind, caps)
        status = self._client("zenbook").status()
        with self.assertRaises(GatewayError) as acked:
            self._client("dell").acknowledge(status["mode_version"], status["epoch"])
        self.assertEqual(acked.exception.code, "revoked")
        self.assertTrue(self.owner.disable_participant("dell", "dell-inst")["already"])

    def test_current_main_cannot_be_removed(self):
        self.cutover()
        _tid, lease, _done = self.promote("zenbook", "k-boot")
        _envelope, (code, _headers, body) = self._disable_raw(
            {"device_id": "zenbook", "installation_id": "zenbook-inst"})
        self.assertEqual((code, body["error"]), (409, "active_holder"))
        self.assertEqual(HTTP_STATUS["active_holder"], 409)
        with self.assertRaises(GatewayError) as raised:
            self.owner.disable_participant("zenbook", "zenbook-inst")
        self.assertEqual((raised.exception.code, raised.exception.status), ("active_holder", 409))
        zen = self._client("zenbook")
        self.assertEqual(zen.status()["main"], "zenbook")
        self.assertEqual(
            zen.renew("zenbook", lease["lease_id"], lease["incarnation"], lease["epoch"])["valid_for"], 15.0)
        self.assertEqual(self._still_enrolled("zenbook")["device_id"], "zenbook")

    def test_a_non_owner_or_bad_proof_removes_nothing(self):
        from jarvis.continuity.integration import EXTRA_HTTP
        self.assertEqual(EXTRA_HTTP["active_holder"], 409)
        self.cutover()
        self.promote("zenbook", "k-boot")
        payload = {"device_id": "dell", "installation_id": "dell-inst"}
        # A device key is not the owner key.
        envelope, (code, _headers, body) = self._disable_raw(payload, key="stranger", nonce="n-bad-key")
        self.assertEqual((code, body["error"]), (401, "unauthorized"))
        # An owner proof for another payload does not cover this one.
        _envelope, (code, _headers, body) = self._disable_raw(
            payload, sign_payload={"device_id": "oracle", "installation_id": "oracle-inst"}, nonce="n-wrong-body")
        self.assertEqual((code, body["error"]), (401, "unauthorized"))
        # No owner client: no key, no request.
        with self.assertRaises(GatewayError) as keyless:
            self._client("zenbook").disable_participant("dell", "dell-inst")
        self.assertEqual(keyless.exception.code, "unauthorized")
        # A device-signed envelope on the owner route is not a proof.
        code, _headers, body = self._device_post_to_disable("zenbook", payload)
        self.assertIn(code, (400, 401))
        # A valid proof is single use.
        good, (code, _headers, body) = self._disable_raw(payload, nonce="n-good")
        self.assertEqual(code, 200, body)
        from jarvis.continuity.gateway import _OWNER_PATH
        code, _headers, body = _exchange(self.origin, _OWNER_PATH["disable"], good)
        self.assertEqual((code, body["error"]), (401, "unauthorized"))
        # Extra fields are not silently ignored.
        _envelope, (code, _headers, body) = self._disable_raw(
            {"device_id": "oracle", "installation_id": "oracle-inst", "force": True}, nonce="n-extra")
        self.assertEqual((code, body["error"]), (400, "bad_request"))
        self.assertEqual(self._still_enrolled("oracle")["device_id"], "oracle")
        self.assertEqual(self._client("zenbook").status()["main"], "zenbook")

    def _device_post_to_disable(self, name, payload):
        from jarvis.continuity.gateway import _OWNER_PATH, payload_digest
        issued = int(self.clock.w)
        signature = sign_message(DEVICE_NS, _keys()[name][1], _message(DEVICE_NS, {
            "v": 1, "device_id": name, "installation_id": name + "-inst", "nonce": "n-dev-rm",
            "issued_at": issued, "purpose": "status", "payload_digest": payload_digest(payload)}))
        return _exchange(self.origin, _OWNER_PATH["disable"], {"payload": payload, "device": {
            "device_id": name, "installation_id": name + "-inst", "nonce": "n-dev-rm",
            "issued_at": issued, "signature": signature}})


class Handover(GatewayCase):
    def test_manual_recover_consume_release_and_restart_barrier(self):
        zen = self.cutover()
        self.assertEqual(zen.status()["mode"], "V2_ONLY_NO_MAIN")
        _tid, lease, done = self.promote("zenbook", "k-boot")
        self.assertEqual(done["state"], "SUCCEEDED")
        self.assertTrue(zen.status()["main_ready"])
        self.assertNotIn(lease["lease_id"], json.dumps(self.gw.audit_events()))
        ticket = {
            "admission_id": "adm-1", "target": "dell", "operation_id": "op-1", "attempt_id": "a-1",
            "digest": digest("open firefox"), "main": "zenbook", "incarnation": lease["incarnation"],
            "epoch": lease["epoch"], "issued_at": self.clock.w - 1, "admit_until": self.clock.w + 3600,
            "issuer_incarnation": zen.status()["incarnation"],
        }
        signature = self._client("zenbook").sign_ticket(ticket)
        with self.assertRaises(GatewayError) as raised:
            self._client("zenbook").consume(ticket, signature)
        self.assertEqual(raised.exception.code, "forbidden")
        first = self._client("dell").consume(ticket, signature)
        self.assertFalse(first["already"])
        again = self._client("dell").consume(ticket, signature)
        self.assertTrue(again["already"])
        self.assertEqual(again["admission_id"], "adm-1")
        changed = dict(ticket, digest=digest("other"))
        with self.assertRaises(GatewayError) as raised:
            self._client("dell").consume(changed, self._client("zenbook").sign_ticket(changed))
        self.assertEqual(raised.exception.code, "conflict")
        with self.assertRaises(GatewayError) as raised:
            self._client("oracle").admission("adm-1")
        self.assertEqual(raised.exception.code, "not_found")
        self.assertEqual(self._client("dell").admission("adm-1")["digest"], ticket["digest"])
        status = self._client("zenbook").release("zenbook", lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.assertEqual(status["mode"], "V2_ONLY_NO_MAIN")
        self.assertIsNone(status["main"])
        code, headers, body = self._renew_raw(lease)
        self.assertEqual(body["error"], "stale")
        self.assertNotIn("Retry-After", headers)
        self.assertNotIn("sqlite", json.dumps(body))
        self.assertNotIn(str(self.db), json.dumps(body))

        incarnation = status["incarnation"]
        epoch = status["epoch"]
        self.gw.close()
        self.gw = ContinuityGateway.open(
            self.db, "home", "oracle-auth", [_keys()["owner"][0]],
            monotonic=self.clock.mono, wall=self.clock.wall)
        self.addCleanup(self.gw.close)
        self.origin = self.gw.serve()
        self.owner = self._client(owner=True)
        for name, kind, caps in (("zenbook", "pc", PC), ("dell", "pc", PC), ("oracle", "server", SERVER)):
            self.enroll(name, kind, caps)
        revived = self._client("zenbook").status()
        self.assertGreater(revived["incarnation"], incarnation)
        self.assertGreater(revived["epoch"], epoch)
        self.assertIsNone(revived["main"])
        code, headers, body = self._renew_raw(lease)
        self.assertEqual(body["error"], "stale")
        self.assertNotIn("Retry-After", headers)
        replay = self._client("dell").consume(ticket, signature)
        self.assertTrue(replay["already"])
        self.assertEqual(replay["epoch"], lease["epoch"])
        transition = self.owner.transition_request(
            "k-again", "recover", "zenbook", revived["incarnation"], revived["epoch"], revived["policy_version"])
        tid = transition["transition_id"]
        ready = {"transition_id": tid, "control_ready": 1, "ai_ready": False, "checkpoint": "cp-1"}
        code, _headers, body = self._device_post("zenbook", "readiness", ready)
        self.assertEqual((code, body["error"]), (400, "bad_request"))
        self.assertEqual(self._client("zenbook").status()["transition"]["state"], "REQUESTED")
        self._client("zenbook").readiness(tid, True, False, "cp-1")
        self.owner.step(tid, "drain")
        code, headers, body = self._owner_post("step", {"transition_id": tid, "step": "fence"})
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "not_quiet")
        self.assertGreater(body["wait"], 0)
        self.assertEqual(headers["Retry-After"], str(math.ceil(body["wait"])))
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn(str(self.db), headers.get("Retry-After", ""))
        with self.assertRaises(GatewayError) as raised:
            self.owner.step(tid, "fence")
        self.assertEqual(raised.exception.code, "not_quiet")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.wait, body["wait"])
        self.assertIsNone(_agreed_wait(True, "1"))
        self.assertIsNone(_agreed_wait(body["wait"], None))
        self.assertIsNone(_agreed_wait(body["wait"], "0"))

    def test_http_lease_live_keeps_the_store_wait(self):
        self.cutover()
        self.promote("zenbook", "k-boot")
        status = self._client("zenbook").status()
        transition = self.owner.transition_request(
            "k-move", "make_main_now", "dell", status["incarnation"], status["epoch"],
            status["policy_version"])
        tid = transition["transition_id"]
        self._client("dell").readiness(tid, True, False, "cp-dell")
        self.owner.step(tid, "drain")
        with self.assertRaises(GatewayError) as raised:
            self.owner.step(tid, "fence")
        self.assertEqual(raised.exception.code, "lease_live")
        self.assertEqual(raised.exception.status, 409)
        self.assertGreater(raised.exception.wait, 0)
        self.assertLessEqual(raised.exception.wait, 17)
        port = GatewayPort(self.owner)
        with self.assertRaises(AuthorityError) as caught:
            port.transition_step(tid, "fence")
        self.assertEqual(caught.exception.code, "lease_live")
        self.assertEqual(caught.exception.wait, raised.exception.wait)
        self.assertNotIn(str(self.db), json.dumps(self.gw.audit_events()))

    def _renew_raw(self, lease):
        payload = {"holder": "zenbook", "lease_id": lease["lease_id"], "incarnation": lease["incarnation"],
                   "epoch": lease["epoch"]}
        return self._device_post("zenbook", "renew", payload)

    def _device_post(self, name, purpose, payload):
        from jarvis.continuity.gateway import _PURPOSE_PATH, payload_digest
        issued = int(self.clock.w)
        nonce = "n-" + purpose + "-" + hashlib.sha256(canonical(payload)).hexdigest()[:8]
        # Nonce must be unique per call. Mix in the clock and a counter via the digest plus id.
        nonce = "n-" + hashlib.sha256(f"{purpose}:{nonce}:{issued}:{id(payload)}".encode()).hexdigest()[:16]
        signature = sign_message(DEVICE_NS, _keys()[name][1], _message(DEVICE_NS, {
            "v": 1, "device_id": name, "installation_id": name + "-inst", "nonce": nonce,
            "issued_at": issued, "purpose": purpose, "payload_digest": payload_digest(payload),
        }))
        return _exchange(self.origin, _PURPOSE_PATH[purpose], {"payload": payload, "device": {
            "device_id": name, "installation_id": name + "-inst", "nonce": nonce,
            "issued_at": issued, "signature": signature,
        }})

    def _owner_post(self, purpose, payload):
        from jarvis.continuity.gateway import _OWNER_PATH, payload_digest
        digest_value = payload_digest(payload)
        challenge = self.gw.issue_challenge(digest_value)
        issued = int(self.clock.w)
        nonce = "n-" + digest_value[-12:]
        signature = sign_message("jarvis-continuity-owner-v1", _keys()["owner"][1], _message(
            "jarvis-continuity-owner-v1", {
                "v": 1, "challenge_id": challenge["challenge_id"], "nonce": nonce,
                "issued_at": issued, "payload_digest": digest_value,
            }))
        return _exchange(self.origin, _OWNER_PATH[purpose], {"payload": payload, "owner": {
            "challenge_id": challenge["challenge_id"], "nonce": nonce, "issued_at": issued,
            "signature": signature,
        }})


class Alignment(GatewayCase):
    def test_runtime_port_keeps_device_binding_and_owner_proof(self):
        self.enroll("zenbook", "pc", PC)
        client = self._client("zenbook")
        port = AuthenticatedPort(client)
        status = port.status()
        self.assertEqual(status["mode"], "LEGACY_ACTIVE")
        self.assertEqual(status["authority_id"], "oracle-auth")
        acked = port.acknowledge("zenbook", "zenbook-inst", status["mode_version"], status["epoch"],
                                 boot_id="boot-1")
        self.assertTrue(acked["accepted"])
        self.assertEqual(acked["device_id"], "zenbook")
        with self.assertRaises(AuthorityError) as raised:
            port.acknowledge("dell", "dell-inst", status["mode_version"], status["epoch"])
        self.assertEqual(raised.exception.code, "forbidden")
        lease = {"holder": "dell", "lease_id": "ls-other", "incarnation": 1, "epoch": 0}
        with self.assertRaises(AuthorityError) as raised:
            port.renew(lease)
        self.assertEqual(raised.exception.code, "forbidden")
        leaked = {"holder": "zenbook", "lease_id": "ls-x", "incarnation": 1, "epoch": 0, "prompt": SECRET}
        with self.assertRaises(AuthorityError) as raised:
            port.renew(leaked)
        self.assertEqual(raised.exception.code, "bad_request")
        self.assertNotIn(SECRET, str(raised.exception))
        request = {
            "caller": "managed-hub", "client_key": "k-port", "action": "recover", "candidate": "zenbook",
            "expected_incarnation": status["incarnation"], "expected_epoch": status["epoch"],
            "expected_policy": status["policy_version"], "profile": "control",
        }
        with self.assertRaises(AuthorityError) as raised:
            port.transition_request(request)
        self.assertEqual(raised.exception.code, "unauthorized")
        with self.assertRaises(AuthorityError) as raised:
            port.transition_readiness("tid-1", {"control_ready": 1, "ai_ready": False, "checkpoint": None})
        self.assertEqual(raised.exception.code, "bad_request")
        owner = AuthenticatedPort(self._client("zenbook", owner=True))
        with self.assertRaises(AuthorityError) as raised:
            owner.transition_request(request)
        self.assertEqual(raised.exception.code, "mode")
        self.assertEqual(port.status()["mode"], "LEGACY_ACTIVE")
        self.assertIsNone(port.status()["main"])
        dead = AuthorityClient(
            "http://127.0.0.1:1", authority_id="oracle-auth", wall=self.clock.wall,
            device_id="zenbook", installation_id="zenbook-inst",
            private_key_path=_keys()["zenbook"][1])
        unsigned = AuthenticatedPort(dead)
        with self.assertRaises(AuthorityError) as raised:
            unsigned.consume(target="zenbook", admission_id="adm-x", ticket_signature=None)
        self.assertEqual(raised.exception.code, "unauthorized")
        with self.assertRaises(AuthorityError) as raised:
            unsigned.consume(target="dell", admission_id="adm-x", ticket_signature="ssh-ed25519 dead")
        self.assertEqual(raised.exception.code, "forbidden")
        runtime = MainRuntime(port, "zenbook", "zenbook-inst", lambda: {"kind": "pc"})
        view = runtime.start()
        self.assertEqual(view["phase"], "legacy")
        self.assertFalse(view["admission_open"])
        self.assertIsNone(view["main"])

    def test_ingress_session_signs_status_and_refuses_a_string_owner_proof(self):
        self.enroll("phone", "client", CLIENT)
        session = ContinuityGatewaySession(
            self.gw, "phone", "phone-inst", _keys()["phone"][1], wall=self.clock.wall)
        status = session.status()
        self.assertEqual(status["authority_id"], "oracle-auth")
        self.assertEqual(status["mode"], "LEGACY_ACTIVE")
        with self.assertRaises(GatewayError) as raised:
            session.relay_metadata("op-session", "dell", "a-1", digest("open firefox"), "status")
        self.assertEqual(raised.exception.code, "no_relay")
        self.assertEqual(raised.exception.operation_id, "op-session")
        with self.assertRaises(IngressError) as caught:
            session.transition_request("own-not-a-proof", {"client_key": "k", "passkey": True})
        self.assertEqual(caught.exception.code, "owner_proof_required")
        self.assertNotIn("passkey", json.dumps(self.gw.audit_events()))


class Relay(GatewayCase):
    def test_relay_is_metadata_idempotent_and_not_an_effect(self):
        upstream = _serve(Upstream)
        stolen = _serve(Stolen)
        stolen.hits = 0
        upstream.bodies = []
        upstream.stolen = f"http://127.0.0.1:{stolen.server_address[1]}/stolen"
        self.addCleanup(_stop, upstream)
        self.addCleanup(_stop, stolen)
        origin = f"http://127.0.0.1:{upstream.server_address[1]}"
        self.cutover(origin)
        self.enroll("phone", "client", CLIENT)
        phone = self._client("phone")
        body = digest("open firefox")
        with self.assertRaises(GatewayError) as raised:
            phone.relay("op-1", "dell", "a-1", body, "status")
        self.assertEqual(raised.exception.operation_id, "op-1")
        self.assertEqual(upstream.bodies, [])
        self.promote("zenbook", "k-boot")
        result = phone.relay("op-1", "dell", "a-1", body, "status")
        self.assertEqual(result["operation_id"], "op-1")
        self.assertTrue(result["relayed"])
        self.assertFalse(result["effect_authorized"])
        self.assertEqual(len(upstream.bodies), 1)
        self.assertNotIn("prompt", upstream.bodies[0])
        self.assertNotIn("url", upstream.bodies[0])
        self.assertEqual(upstream.bodies[0]["authority_id"], "oracle-auth")
        again = phone.relay("op-1", "dell", "a-1", body, "status")
        self.assertEqual(again["operation_id"], "op-1")
        self.assertEqual(len(upstream.bodies), 1)
        with self.assertRaises(GatewayError) as raised:
            phone.relay("op-1", "dell", "a-2", digest("different"), "status")
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(len(upstream.bodies), 1)
        with self.assertRaises(GatewayError) as raised:
            phone.relay("op-redirect", "dell", "a-1", body, "status")
        self.assertEqual(raised.exception.code, "no_relay")
        self.assertEqual(stolen.hits, 0)
        with self.assertRaises(GatewayError):
            phone.relay("op-redirect", "dell", "a-1", body, "status")
        self.assertEqual(stolen.hits, 0)
        self.assertEqual(sum(1 for body in upstream.bodies if body.get("operation_id") == "op-redirect"), 1)
        smuggled = {"operation_id": "op-2", "target": "dell", "attempt_id": "a-1", "digest": body,
                    "capability": "status", "url": "http://example.com/hook", "prompt": SECRET}
        code, _headers, response = self._client_payload(phone, "relay", smuggled)
        self.assertEqual((code, response), (400, {"error": "bad_request"}))
        self.assertNotIn(SECRET, json.dumps(self.gw.audit_events()))
        self.assertFalse(any("prompt" in item or "url" in item for item in upstream.bodies))

    def _client_payload(self, client, purpose, payload):
        from jarvis.continuity.gateway import _PURPOSE_PATH, payload_digest
        issued = int(self.clock.w)
        nonce = "n-smuggle"
        signature = sign_message(DEVICE_NS, _keys()["phone"][1], _message(DEVICE_NS, {
            "v": 1, "device_id": "phone", "installation_id": "phone-inst", "nonce": nonce,
            "issued_at": issued, "purpose": purpose, "payload_digest": payload_digest(payload),
        }))
        return _exchange(self.origin, _PURPOSE_PATH[purpose], {"payload": payload, "device": {
            "device_id": "phone", "installation_id": "phone-inst", "nonce": nonce,
            "issued_at": issued, "signature": signature,
        }})


class Quarantine(unittest.TestCase):
    def test_foreign_authority_open_fails_closed(self):
        keys = _keys()
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "authority.sqlite"
            clock = Clock()
            first = ContinuityGateway.open(db, "home", "oracle-auth", [keys["owner"][0]],
                                            monotonic=clock.mono, wall=clock.wall)
            first.close()
            foreign = ContinuityGateway.open(db, "home", "other-auth", [keys["owner"][0]],
                                              monotonic=clock.mono, wall=clock.wall)
            origin = foreign.serve()
            try:
                owner = AuthorityClient(origin, authority_id="oracle-auth", wall=clock.wall,
                                        owner_private_key_path=keys["owner"][1])
                owner.enroll({"device_id": "phone", "installation_id": "phone-inst", "kind": "client",
                              "public_key": keys["phone"][0], "capabilities": CLIENT})
                phone = AuthorityClient(origin, authority_id="oracle-auth", wall=clock.wall,
                                        device_id="phone", installation_id="phone-inst",
                                        private_key_path=keys["phone"][1])
                self.assertEqual(phone.status()["mode"], "QUARANTINED")
                foreign_client = AuthorityClient(origin, authority_id="other-auth", wall=clock.wall,
                                                 device_id="phone", installation_id="phone-inst",
                                                 private_key_path=keys["phone"][1])
                with self.assertRaises(GatewayError) as raised:
                    foreign_client.status()
                self.assertEqual(raised.exception.code, "authority_mismatch")
                with self.assertRaises(GatewayError) as raised:
                    owner.enroll({"device_id": "zenbook", "installation_id": "zenbook-inst", "kind": "pc",
                                  "public_key": keys["zenbook"][0], "capabilities": PC})
                self.assertEqual(raised.exception.code, "quarantined")
            finally:
                foreign.close()


class DirectoryNamespace(GatewayCase):
    """A client enrollment cannot replace a live PC directory key, and consume binds it."""

    def _directory(self, device_id):
        row = self.gw.store._db.execute(
            "SELECT device_id, installation_id, kind, public_key FROM gateway_directory WHERE device_id=?",
            (device_id,)).fetchone()
        return None if row is None else tuple(row)

    def _participant(self, device_id):
        row = self.gw.store._db.execute(
            "SELECT installation_id, kind, eligible, mode_version, fenced_epoch"
            " FROM participants WHERE device_id=?", (device_id,)).fetchone()
        return None if row is None else tuple(row)

    def _ticket(self, lease, admission_id, operation_id):
        status = self._client("zenbook").status()
        return {
            "admission_id": admission_id, "target": "dell", "operation_id": operation_id,
            "attempt_id": "a-1", "digest": digest("open firefox"), "main": "zenbook",
            "incarnation": lease["incarnation"], "epoch": lease["epoch"],
            "issued_at": self.clock.w - 1, "admit_until": self.clock.w + 3600,
            "issuer_incarnation": status["incarnation"],
        }

    def test_client_enroll_cannot_replace_a_live_pc_and_consume_binds_installation(self):
        self.cutover()
        _tid, lease, done = self.promote("zenbook", "k-boot")
        self.assertEqual(done["state"], "SUCCEEDED")
        self.assertTrue(self._client("zenbook").status()["main_ready"])
        directory = self._directory("zenbook")
        participant = self._participant("zenbook")
        self.enroll("zenbook", "pc", PC)
        self.assertEqual(self._participant("zenbook"), participant)
        self.assertEqual(self._directory("zenbook"), directory)
        with self.assertRaises(GatewayError) as raised:
            self.owner.enroll({
                "device_id": "zenbook", "installation_id": "phone-inst", "kind": "client",
                "public_key": _keys()["phone"][0], "capabilities": list(CLIENT),
            })
        self.assertEqual((raised.exception.code, raised.exception.status), ("conflict", 409))
        self.assertEqual(self._directory("zenbook"), directory)
        self.assertEqual(self._participant("zenbook"), participant)
        self.assertEqual(self._client("zenbook").status()["main"], "zenbook")
        forged = AuthorityClient(
            self.origin, authority_id="oracle-auth", wall=self.clock.wall,
            device_id="zenbook", installation_id="phone-inst",
            private_key_path=_keys()["phone"][1])
        with self.assertRaises(GatewayError) as raised:
            forged.status()
        self.assertEqual(raised.exception.code, "unauthorized")
        forged_ticket = self._ticket(lease, "adm-forged", "op-forged")
        with self.assertRaises(GatewayError) as raised:
            self._client("dell").consume(forged_ticket, forged.sign_ticket(forged_ticket))
        self.assertEqual(raised.exception.code, "unauthorized")
        self.assertIsNone(self.gw.store.admission("adm-forged"))
        real = self._ticket(lease, "adm-real", "op-real")
        first = self._client("dell").consume(real, self._client("zenbook").sign_ticket(real))
        self.assertFalse(first["already"])
        with self.assertRaises(GatewayError) as raised:
            self.owner.enroll({
                "device_id": "zenbook", "installation_id": "zenbook-new", "kind": "pc",
                "public_key": _keys()["stranger"][0], "capabilities": list(PC),
            })
        self.assertEqual(raised.exception.code, "active_holder")
        with self.assertRaises(GatewayError) as raised:
            self.owner.enroll({
                "device_id": "zenbook", "installation_id": "zenbook-inst", "kind": "server",
                "public_key": _keys()["zenbook"][0], "capabilities": list(SERVER),
            })
        self.assertEqual(raised.exception.code, "active_holder")
        self.assertEqual(self._directory("zenbook"), directory)
        self.assertEqual(self._participant("zenbook"), participant)
        with self.assertRaises(GatewayError) as raised:
            self.owner.enroll({
                "device_id": "oracle", "installation_id": "phone-inst", "kind": "client",
                "public_key": _keys()["phone"][0], "capabilities": list(CLIENT),
            })
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(self._directory("oracle")[2], "server")
        self.enroll("phone", "client", CLIENT)
        self.assertIsNone(self._participant("phone"))
        self.assertEqual(self._client("phone").status()["main"], "zenbook")
        self.owner.enroll({
            "device_id": "phone", "installation_id": "phone-inst", "kind": "client",
            "public_key": _keys()["stranger"][0], "capabilities": list(CLIENT),
        })
        self.assertEqual(self._directory("phone")[1:], ("phone-inst", "client", _keys()["stranger"][0]))
        self.assertIsNone(self._participant("phone"))
        rotated = AuthorityClient(
            self.origin, authority_id="oracle-auth", wall=self.clock.wall,
            device_id="phone", installation_id="phone-inst",
            private_key_path=_keys()["stranger"][1])
        self.assertEqual(rotated.status()["mode"], "V2_ACTIVE")
        with self.assertRaises(GatewayError) as raised:
            self._client("phone").status()
        self.assertEqual(raised.exception.code, "unauthorized")
        with self.assertRaises(GatewayError) as raised:
            self.owner.enroll({
                "device_id": "phone", "installation_id": "phone-inst-2", "kind": "client",
                "public_key": _keys()["phone"][0], "capabilities": list(CLIENT),
            })
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(self._directory("phone")[1], "phone-inst")
        with self.assertRaises(GatewayError) as raised:
            self.owner.enroll({
                "device_id": "phone", "installation_id": "phone-inst", "kind": "pc",
                "public_key": _keys()["phone"][0], "capabilities": list(PC),
            })
        self.assertEqual(raised.exception.code, "conflict")
        self.assertIsNone(self._participant("phone"))
        self.assertEqual(self._directory("phone")[2], "client")
        oracle = self._participant("oracle")
        self.owner.enroll({
            "device_id": "oracle", "installation_id": "oracle-inst-2", "kind": "server",
            "public_key": _keys()["oracle"][0], "capabilities": list(SERVER),
        })
        reset = self._participant("oracle")
        self.assertEqual(reset[0], "oracle-inst-2")
        self.assertEqual((reset[3], reset[4]), (0, -1))
        self.assertNotEqual(reset[3:], oracle[3:])
        self.assertEqual(self._directory("oracle")[1], "oracle-inst-2")
        dell = self._participant("dell")
        self.enroll("dell", "pc", PC)
        self.assertEqual(self._participant("dell"), dell)
        phone = self.gw._devices["phone"]
        planted = _Record("zenbook", "phone-inst", "pc", phone.public_key, phone.capabilities, None)
        split = self._ticket(lease, "adm-split", "op-split")
        with self.gw._lock, self.gw.store._lock:
            self.gw.store._db.execute(
                "UPDATE gateway_directory SET installation_id=?, kind=?, public_key=? WHERE device_id=?",
                ("phone-inst", "pc", phone.public_key, "zenbook"))
            self.gw._devices["zenbook"] = planted
        self.assertEqual(self._directory("zenbook")[1:3], ("phone-inst", "pc"))
        self.assertEqual(self._participant("zenbook")[0], "zenbook-inst")
        signature = rotated.sign_ticket(split)
        self.assertTrue(verify_message(TICKET_NS, _message(TICKET_NS, split), signature, phone.public_key))
        with self.assertRaises(GatewayError) as raised:
            self._client("dell").consume(split, signature)
        self.assertEqual(raised.exception.code, "unauthorized")
        self.assertIsNone(self.gw.store.admission("adm-split"))
        self.assertIsNotNone(self.gw.store.admission("adm-real"))
        self.assertEqual(self._participant("zenbook"), participant)


if __name__ == "__main__":
    unittest.main()
