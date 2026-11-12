"""Phone ingress against the real gateway call shape. Not a live failover drill.

Run: .venv/bin/python -m unittest tests.continuity.test_ingress
"""

import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity.authority import AuthorityError
from jarvis.continuity.gateway import (
    DEVICE_NS,
    OWNER_NS,
    ContinuityGateway,
    GatewayError,
    OwnerProof as GatewayOwnerProof,
    _message,
    payload_digest,
    sign_message,
)
from jarvis.continuity.ingress import (
    BODY_TEXT,
    GATEWAY_NOT_OURS,
    PC_SEAM,
    PHONE_COOKIE,
    PHONE_SHELL,
)
from jarvis.continuity import ingress as ingress_mod
from jarvis.continuity.ingress import (
    APPROVALS,
    FRAME_MAX,
    GATEWAY_METHODS,
    MobileIngress,
    PhoneProtocolAdapter,
    PinnedTarget,
    Principal,
    ContinuityGatewaySession,
    IngressError,
    pin_target,
)
from jarvis.phone import (
    ASK_MAX,
    CODE_CHARS,
    COOKIE,
    MAX_BODY,
    PAIR_PATHS,
    SHELL,
    TAILNET,
    _APPROVALS,
    cookie_values as phone_cookie_values,
)
from jarvis.remote_view import MAX_FRAME

ORIGIN = "https://oracle.tailnet.ts.net"
SECRET = "secret-prompt-do-not-store"
REPLY = "SECRET-REPLY-not-on-disk"
PASSKEY = "assertion-secret-do-not-store"
EVENT = "typed-secret-phrase"
FRAME = b"JPEG-SECRET-BYTES"
PATH_SECRET = "spool-file-secret-do-not-open"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Gateway:
    """Enforces the session call shape. It does not accept a phone body."""

    def __init__(self):
        self.calls = []
        self.forbidden = []
        self.view = {
            "fleet_id": "home", "authority_id": "oracle-auth", "mode": "V2_ACTIVE",
            "mode_version": 4, "incarnation": 2, "epoch": 3, "policy_version": 1,
            "main": "zenbook", "lease_holder": "zenbook", "lease_valid": True,
            "main_ready": True, "preferred": "zenbook", "last_main": "dell",
        }
        self.relay_handler = None
        self.transition_handler = None
        self.transitions = {}
        self.status_error = None

    def status(self):
        self.calls.append(("status",))
        if self.status_error is not None:
            raise self.status_error
        return dict(self.view)

    def relay_metadata(self, operation_id, target, attempt_id, digest, capability):
        payload = {
            "operation_id": operation_id, "target": target, "attempt_id": attempt_id,
            "digest": digest, "capability": capability,
        }
        self.calls.append(("relay", operation_id, target, capability, tuple(sorted(payload))))
        if set(payload) != {"operation_id", "target", "attempt_id", "digest", "capability"}:
            raise GatewayError("bad_request")
        if self.relay_handler is not None:
            return self.relay_handler(operation_id, target, attempt_id, digest, capability)
        return {"operation_id": operation_id, "relayed": True, "effect_authorized": False,
                "upstream_accepted": True}

    def transition_request(self, proof, payload):
        self.calls.append(("transition", isinstance(proof, GatewayOwnerProof),
                           tuple(sorted(payload)), "receipt" in payload, "reconcile" in payload,
                           "caller" in payload))
        if not isinstance(proof, GatewayOwnerProof):
            raise GatewayError("unauthorized")
        if self.transition_handler is not None:
            return self.transition_handler(proof, payload)
        key = payload["client_key"]
        self.transitions.setdefault(key, {
            "transition_id": "tr-1", "state": "REQUESTED", "action": payload["action"],
            "candidate": payload["candidate"], "profile": payload["profile"],
        })
        return dict(self.transitions[key])

    def renew(self, *args, **kwargs):
        self.forbidden.append("renew")

    def release(self, *args, **kwargs):
        self.forbidden.append("release")

    def acknowledge(self, *args, **kwargs):
        self.forbidden.append("acknowledge")

    def consume(self, *args, **kwargs):
        self.forbidden.append("consume")

    def admission(self, *args, **kwargs):
        self.forbidden.append("admission")

    def transition_readiness(self, *args, **kwargs):
        self.forbidden.append("transition_readiness")

    def transition_step(self, *args, **kwargs):
        self.forbidden.append("transition_step")

    def set_mode(self, *args, **kwargs):
        self.forbidden.append("set_mode")

    def enroll(self, *args, **kwargs):
        self.forbidden.append("enroll")

    def relays(self):
        return [item for item in self.calls if item[0] == "relay"]

    def transitions_called(self):
        return [item for item in self.calls if item[0] == "transition"]


def peer_from(headers):
    kind = headers.get("X-Test-Peer") or "phone"
    if kind == "none":
        return None
    table = {
        "phone": ("phone-stable", "david@example.com", "iOS", "iPhone", "100.64.0.2"),
        "other": ("phone-other", "david@example.com", "iOS", "Other", "100.64.0.3"),
        "pc": ("pc-zenbook", "david@example.com", "linux", "zenbook", "100.64.0.10"),
        "desktop": ("phone-stable", "david@example.com", "linux", "laptop", "100.64.0.4"),
    }
    stable, login, os_name, node, address = table[kind]
    return {"stable_id": stable, "login": login, "os": os_name, "node": node, "address": address}


def owner_from(material):
    if material != "owner-token":
        return None
    return {"role": "owner", "installation_id": "zenbook-inst", "device_id": "zenbook"}


def headers(peer="phone", cookie=None, csrf=None, origin=ORIGIN, site="same-origin", host=None):
    host = host or "oracle.tailnet.ts.net"
    out = {
        "Host": host, "Origin": origin, "X-JARVIS-Phone": "1", "Sec-Fetch-Site": site,
        "Tailscale-User-Login": "david@example.com", "X-Test-Peer": peer,
    }
    if cookie:
        out["Cookie"] = f"{PHONE_COOKIE}={cookie}"
    if csrf:
        out["X-CSRF-Token"] = csrf
    return out


def disk_bytes(folder):
    blob = b""
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            blob += Path(dirpath, name).read_bytes()
    return blob


def assert_absent(blob, text):
    if re.fullmatch(r"[0-9A-Fa-f]+", text or ""):
        return
    if text.encode() in blob:
        raise AssertionError(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.gw = Gateway()
        self.logs = []
        self.passkey_calls = []
        self.proofs = []
        self.ing = self.open()
        self.addCleanup(self.ing.close)
        self.http = PhoneProtocolAdapter(self.ing)
        self.zen = pin_target("zenbook", "https://zenbook.tailnet.ts.net", "100.64.0.10")
        self.dell = pin_target("dell", "https://dell.tailnet.ts.net", "100.64.0.11")

    def open(self, allowed=None, blocked=("pc-zenbook", "pc-dell")):
        if allowed is None:
            allowed = {
                "zenbook": pin_target("zenbook", "https://zenbook.tailnet.ts.net", "100.64.0.10"),
                "dell": pin_target("dell", "https://dell.tailnet.ts.net", "100.64.0.11"),
            }
        return MobileIngress(
            Path(self.tmp.name) / "ingress.sqlite", self.gw, origin=ORIGIN,
            installation_id="oracle-inst", allowed_mains=allowed, peer_verifier=peer_from,
            blocked_stable_ids=blocked, passkey_verifier=self.passkey, owner_verifier=owner_from,
            owner_proof=self.mint, clock=self.clock, log=self.logs.append)

    def passkey(self, principal, preview, proof):
        self.passkey_calls.append(proof.get("assertion"))
        if proof.get("assertion") != "good":
            return None
        return "receipt-ok-secret"

    def mint(self, payload):
        self.proofs.append(dict(payload))
        return GatewayOwnerProof(object(), "owner-test", "transition", "sha256:" + "ab" * 32)

    def activate(self, version=4):
        report = self.ing.activate(version, legacy_phone_fenced=True)
        self.assertFalse(report["proved"])
        return report

    def owner(self):
        self.ing.identify(headers())
        return self.ing.authenticate_owner("owner-token")

    def pair(self, peer="phone"):
        self.activate()
        issued = self.ing.issue_code(self.owner(), "Pixel")
        first = self.http.handle("POST", "/phone/pair", headers(peer),
                                 json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.assertEqual(first.status, 202)
        self.assertNotIn("Set-Cookie", first.headers)
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(peer),
                                json.dumps({"code": issued["code"]}).encode())
        self.assertEqual(done.status, 200, done.body)
        self.assertFalse(done.body["manage_main"])
        return issued, done.body

    def blob(self):
        return disk_bytes(self.tmp.name)


class Pins(unittest.TestCase):
    def test_tailnet_pin_rejects_metadata_and_public_addresses(self):
        rejected = (
            ("zenbook", "http://169.254.169.254/", "169.254.169.254"),
            ("zenbook", "https://8.8.8.8/", "8.8.8.8"),
            ("zenbook", "https://10.1.1.1/", "10.1.1.1"),
            ("zenbook", "https://user:secret@zenbook.tailnet.ts.net/", "100.64.0.10"),
            ("zenbook", "https://zenbook.tailnet.ts.net/latest/meta-data", "100.64.0.10"),
            ("zenbook", "https://zenbook.tailnet.ts.net/?q=1", "100.64.0.10"),
            ("zenbook", "file:///etc/passwd", "100.64.0.10"),
            ("zenbook", "https://100.64.0.11/", "100.64.0.10"),
            ("zenbook", "http://zenbook.tailnet.ts.net/", "100.64.0.10"),
            ("zenbook", "https://127.0.0.1/", "127.0.0.1"),
        )
        for device, url, ip in rejected:
            with self.assertRaises(IngressError):
                pin_target(device, url, ip)
        pinned = pin_target("zenbook", "https://zenbook.tailnet.ts.net", "100.64.0.10")
        self.assertIsInstance(pinned, PinnedTarget)
        self.assertEqual(pinned.connect_ip, "100.64.0.10")
        v6 = pin_target("dell", "https://dell.tailnet.ts.net", "fd7a:115c:a1e0::12")
        self.assertEqual(v6.connect_ip, "fd7a:115c:a1e0::12")


class Cutover(Base):
    def test_inert_until_activate_and_activation_is_not_durable(self):
        result = self.http.handle("POST", "/phone/pair", headers(), b"{}")
        self.assertEqual(result.status, 503)
        self.assertEqual(result.body["code"], "legacy_retained")
        self.assertNotIn("Set-Cookie", result.headers)
        self.assertEqual(self.gw.calls, [])
        self.assertEqual(self.gw.relays(), [])
        with self.assertRaises(IngressError) as raised:
            self.ing.activate(4, legacy_phone_fenced=False)
        self.assertEqual(raised.exception.code, "legacy_unfenced")
        self.assertEqual(self.gw.calls, [])
        bare = MobileIngress(
            Path(self.tmp.name) / "other.sqlite", self.gw, origin=ORIGIN,
            installation_id="oracle-inst", allowed_mains={"zenbook": self.zen},
            peer_verifier=peer_from, blocked_stable_ids=(), clock=self.clock)
        self.addCleanup(bare.close)
        with self.assertRaises(IngressError) as raised:
            bare.activate(4, legacy_phone_fenced=True)
        self.assertEqual(raised.exception.code, "peer_policy_unset")
        self.gw.view["mode"] = "LEGACY_ACTIVE"
        with self.assertRaises(IngressError) as raised:
            self.ing.activate(4, legacy_phone_fenced=True)
        self.assertEqual(raised.exception.code, "legacy_retained")
        self.gw.view["mode"] = "QUARANTINED"
        with self.assertRaises(IngressError) as raised:
            self.ing.activate(4, legacy_phone_fenced=True)
        self.assertEqual(raised.exception.code, "quarantined")
        self.gw.view["mode"] = "V2_ACTIVE"
        with self.assertRaises(IngressError) as raised:
            self.ing.activate(99, legacy_phone_fenced=True)
        self.assertEqual(raised.exception.code, "stale")
        self.activate()
        issued, body = self.pair_ready()
        del issued
        principal = self.ing.authenticate(self.cookie, body["csrf_token"], ORIGIN)
        self.gw.view["mode_version"] = 5
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(principal, "op-meta-01", {"attempt_id": "attempt-1"})
        self.assertEqual(raised.exception.code, "cutover_stale")
        self.assertEqual(self.gw.relays(), [])
        self.ing.close()
        revived = self.open()
        self.addCleanup(revived.close)
        again = PhoneProtocolAdapter(revived).handle("POST", "/phone/pair", headers(), b"{}")
        self.assertEqual(again.body["code"], "legacy_retained")
        revived.close()
        db = sqlite3.connect(Path(self.tmp.name) / "ingress.sqlite")
        db.execute("UPDATE meta SET value='99' WHERE key='schema'")
        db.commit()
        db.close()
        with self.assertRaises(IngressError) as raised:
            self.open()
        self.assertEqual(raised.exception.code, "quarantined")

    def pair_ready(self):
        issued = self.ing.issue_code(self.owner(), "Pixel")
        self.http.handle("POST", "/phone/pair", headers(),
                         json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(),
                                json.dumps({"code": issued["code"]}).encode())
        self.cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        return issued, done.body


class Routing(Base):
    def test_no_ready_main_and_no_substitute_target(self):
        self.activate()
        issued, body = self.pair_body()
        del issued
        cases = []
        original = dict(self.gw.view)
        def submit():
            self.ing.identify(headers())
            principal = self.ing.authenticate(self.cookie, body["csrf_token"], ORIGIN)
            return self.ing.submit(principal, "op-meta-01", {"attempt_id": "attempt-1"})
        self.gw.view["mode"] = "V2_ONLY_NO_MAIN"
        self.gw.view["main"] = None
        self.gw.view["main_ready"] = False
        with self.assertRaises(IngressError) as raised:
            submit()
        self.assertEqual(raised.exception.code, "no_main")
        self.gw.view = dict(original)
        self.gw.view["main_ready"] = False
        with self.assertRaises(IngressError) as raised:
            submit()
        self.assertEqual(raised.exception.code, "no_main")
        self.gw.view = dict(original)
        self.gw.view["lease_valid"] = False
        with self.assertRaises(IngressError) as raised:
            submit()
        self.assertEqual(raised.exception.code, "no_main")
        self.gw.view = dict(original)
        self.gw.view["mode"] = "HANDOVER"
        with self.assertRaises(IngressError) as raised:
            submit()
        self.assertEqual(raised.exception.code, "no_main")
        self.gw.view = dict(original)
        self.gw.view["main"] = None
        self.gw.view["main_ready"] = False
        self.gw.view["last_main"] = "dell"
        self.gw.view["preferred"] = "dell"
        with self.assertRaises(IngressError) as raised:
            submit()
        self.assertEqual(raised.exception.code, "no_main")
        self.gw.view = dict(original)
        self.gw.view["lease_holder"] = "dell"
        with self.assertRaises(IngressError) as raised:
            submit()
        self.assertEqual(raised.exception.code, "no_main")
        self.assertEqual(self.gw.relays(), [])
        self.gw.view = dict(original)
        self.ing.close()
        narrowed = self.open(allowed={"dell": self.dell})
        self.addCleanup(narrowed.close)
        self.ing = narrowed
        self.http = PhoneProtocolAdapter(narrowed)
        self.activate()
        issued, body = self.pair_body()
        del issued
        with self.assertRaises(IngressError) as raised:
            submit()
        self.assertEqual(raised.exception.code, "target_denied")
        self.assertEqual(self.gw.relays(), [])
        self.assertEqual(self.gw.forbidden, [])
        del cases

    def pair_body(self):
        issued = self.ing.issue_code(self.owner(), "Pixel")
        self.http.handle("POST", "/phone/pair", headers(),
                         json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(),
                                json.dumps({"code": issued["code"]}).encode())
        self.cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        return issued, done.body


class Metadata(Base):
    def test_metadata_is_once_and_not_an_effect(self):
        self.activate()
        _issued, body = self.ready_phone()
        principal = self.principal(body)
        first = self.ing.submit(principal, "op-meta-01", {"attempt_id": "attempt-1", "effect_class": "screen"})
        self.assertEqual(first["state"], "forwarded")
        self.assertFalse(first["effect_authorized"])
        self.assertTrue(first["upstream_accepted"])
        self.assertEqual(len(self.gw.relays()), 1)
        self.assertEqual(self.gw.relays()[0][2], "zenbook")
        self.assertEqual(self.gw.relays()[0][3], "client")
        self.assertNotIn(SECRET, json.dumps(self.gw.calls))
        again = self.ing.submit(principal, "op-meta-01", {"attempt_id": "attempt-1", "effect_class": "screen"})
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["effect_authorized"])
        self.assertEqual(len(self.gw.relays()), 1)
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(principal, "op-meta-01", {"attempt_id": "attempt-2", "effect_class": "screen"})
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(len(self.gw.relays()), 1)
        looked = self.ing.operation(principal, "op-meta-01")
        self.assertEqual(looked["state"], "forwarded")
        self.assertEqual(len(self.gw.relays()), 1)
        self.assertNotIn(self.principal_phone(body), json.dumps([item for item in self.gw.calls if item[0] == "status"]))

    def test_uncertain_metadata_is_not_sent_again(self):
        self.activate()
        _issued, body = self.ready_phone()
        principal = self.principal(body)
        self.gw.relay_handler = lambda *args: (_ for _ in ()).throw(GatewayError("no_relay"))
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(principal, "op-meta-02", {"attempt_id": "attempt-1"})
        self.assertEqual(raised.exception.code, "no_relay")
        self.assertEqual(len(self.gw.relays()), 1)
        self.gw.relay_handler = None
        second = self.ing.submit(principal, "op-meta-02", {"attempt_id": "attempt-1"})
        self.assertTrue(second["uncertain"])
        self.assertFalse(second["effect_authorized"])
        self.assertEqual(len(self.gw.relays()), 1)

    def test_overclaim_and_spool_are_not_success(self):
        self.activate()
        _issued, body = self.ready_phone()
        principal = self.principal(body)
        secret_path = Path(self.tmp.name) / "spool.txt"
        secret_path.write_text(PATH_SECRET, encoding="utf-8")

        def overclaim(operation_id, target, attempt_id, digest, capability):
            del target, attempt_id, digest, capability
            return {"operation_id": operation_id, "relayed": True, "effect_authorized": True,
                    "upstream_accepted": True, "reply": REPLY}

        self.gw.relay_handler = overclaim
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(principal, "op-meta-03", {"attempt_id": "attempt-1"})
        self.assertEqual(raised.exception.code, "relay_overclaim")
        self.assertNotIn(REPLY, str(raised.exception))
        stored = self.ing.operation(principal, "op-meta-03")
        self.assertTrue(stored["uncertain"])
        self.assertFalse(stored["effect_authorized"])

        def spool(operation_id, target, attempt_id, digest, capability):
            del target, attempt_id, digest, capability
            return {"operation_id": operation_id, "relayed": True, "effect_authorized": False,
                    "upstream_accepted": True, "path": str(secret_path), "reply": REPLY}

        self.gw.relay_handler = spool
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(principal, "op-meta-04", {"attempt_id": "attempt-1"})
        self.assertEqual(raised.exception.code, "relay_overclaim")
        self.assertEqual(secret_path.read_text(encoding="utf-8"), PATH_SECRET)
        store = b"".join(path.read_bytes() for path in Path(self.tmp.name).iterdir()
                         if path.is_file() and path.name != "spool.txt")
        assert_absent(store, PATH_SECRET)
        assert_absent(store, REPLY)

    def test_crash_during_relay_stays_uncertain_after_reopen(self):
        self.activate()
        _issued, body = self.ready_phone()
        cookie = self.cookie
        csrf = body["csrf_token"]

        class Boom(BaseException):
            pass

        def boom(*args):
            raise Boom()

        self.gw.relay_handler = boom
        with self.assertRaises(Boom):
            principal = self.principal(body)
            self.ing.submit(principal, "op-meta-05", {"attempt_id": "attempt-1"})
        self.assertEqual(len(self.gw.relays()), 1)
        self.ing.close()
        self.gw.relay_handler = None
        revived = self.open()
        self.addCleanup(revived.close)
        revived.activate(4, legacy_phone_fenced=True)
        revived.identify(headers(cookie=cookie, csrf=csrf))
        principal = revived.authenticate(cookie, csrf, ORIGIN)
        second = revived.submit(principal, "op-meta-05", {"attempt_id": "attempt-1"})
        self.assertTrue(second["uncertain"])
        self.assertEqual(len(self.gw.relays()), 1)

    def test_personal_fields_are_refused_before_relay(self):
        self.activate()
        _issued, body = self.ready_phone()
        principal = self.principal(body)
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(principal, "op-meta-06", {"attempt_id": "attempt-1", "text": SECRET, "url": "http://evil"})
        self.assertEqual(raised.exception.code, "personal_field")
        self.assertEqual(self.gw.relays(), [])
        assert_absent(self.blob(), SECRET)
        self.gw.status_error = AuthorityError("stale", "/tmp/secret-authority.sqlite")
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(principal, "op-meta-07", {"attempt_id": "attempt-1"})
        self.assertNotIn("secret-authority", raised.exception.message)
        self.assertNotIn("sqlite", raised.exception.message)
        self.gw.status_error = RuntimeError("/tmp/secret-authority.sqlite")
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(principal, "op-meta-08", {"attempt_id": "attempt-1"})
        self.assertEqual(raised.exception.code, "authority_unavailable")
        self.assertNotIn("sqlite", raised.exception.message)

    def ready_phone(self):
        issued = self.ing.issue_code(self.owner(), "Pixel")
        self.http.handle("POST", "/phone/pair", headers(),
                         json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(),
                                json.dumps({"code": issued["code"]}).encode())
        self.cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        self.csrf = done.body["csrf_token"]
        return issued, done.body

    def principal(self, body):
        self.ing.identify(headers(cookie=self.cookie, csrf=body["csrf_token"]))
        return self.ing.authenticate(self.cookie, body["csrf_token"], ORIGIN)

    def principal_phone(self, body):
        return body["phone"]["id"]


class PhoneBodies(Base):
    def test_phone_effects_are_not_forwarded(self):
        self.activate()
        issued, body = self.ready()
        result = self.http.handle(
            "POST", "/phone/api/ask", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"text": SECRET, "key": "ask-key-01", "url": "http://evil", "note": "SMUGGLED-NOTE"}).encode())
        self.assertEqual(result.status, 503)
        self.assertEqual(result.body["code"], "body_not_relayable")
        self.assertNotIn(SECRET, json.dumps(result.body))
        self.assertEqual(self.gw.relays(), [])
        assert_absent(self.blob(), SECRET)
        assert_absent(self.blob(), "SMUGGLED-NOTE")
        assert_absent(self.blob(), self.cookie)
        assert_absent(self.blob(), body["csrf_token"])
        if re.search(r"[G-Z]", issued["code"]):
            assert_absent(self.blob(), issued["code"])
        self.assertNotIn(SECRET, "".join(self.logs))
        approval = self.http.handle(
            "POST", "/phone/api/ask", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"text": "ok", "key": "ask-key-02"}).encode())
        self.assertEqual(approval.status, 400)
        self.assertEqual(approval.body["code"], "approval")
        yes = self.http.handle(
            "POST", "/phone/api/ask", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"text": "yes", "key": "ask-key-03"}).encode())
        self.assertEqual(yes.body["code"], "approval")
        normal = self.http.handle(
            "POST", "/phone/api/ask", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"text": "What time is sunset tomorrow", "key": "ask-key-04"}).encode())
        self.assertEqual(normal.body["code"], "body_not_relayable")
        self.assertEqual(self.gw.relays(), [])
        missing = self.http.handle(
            "POST", "/phone/api/session/start", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"device": "dell", "kind": "screen"}).encode())
        self.assertEqual(missing.status, 400)
        self.assertEqual(missing.body["code"], "operation_id_required")
        started = self.http.handle(
            "POST", "/phone/api/session/start", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"operation_id": "sess-start-01", "device": "dell", "kind": "screen", "quality": "low"}).encode())
        self.assertEqual(started.body["code"], "body_not_relayable")
        self.assertEqual(self.gw.relays(), [])
        self.ing.grant_manage_main(self.owner(), body["phone"]["id"])
        granted = self.http.handle(
            "POST", "/phone/api/ask", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"text": "list the calendar", "key": "ask-key-05"}).encode())
        self.assertEqual(granted.body["code"], "body_not_relayable")
        self.assertEqual(self.gw.transitions_called(), [])
        me = self.http.handle("GET", "/phone/api/me", headers(cookie=self.cookie, csrf=body["csrf_token"]))
        self.assertTrue(me.body["manage_main"])
        self.assertFalse(me.body["relay"])
        self.assertTrue(me.body["metadata_only"])

    def test_session_input_checks_run_and_the_text_is_not_stored(self):
        self.activate()
        _issued, body = self.ready()
        sid = "ab" * 16
        self.ing._db.execute(
            "INSERT INTO sessions(session_id, phone_id, device, kind, state, incarnation, epoch, main, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, body["phone"]["id"], "dell", "screen", "live", 2, 3, "zenbook", self.clock.t))
        typed = self.http.handle(
            "POST", "/phone/api/session/input", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"operation_id": "input-op-01", "session": sid,
                        "events": [{"t": "text", "text": EVENT, "url": "http://evil"}]}).encode())
        self.assertEqual(typed.body["code"], "body_not_relayable")
        assert_absent(self.blob(), EVENT)
        self.assertEqual(self.gw.relays(), [])
        unknown = self.http.handle(
            "POST", "/phone/api/session/input", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"operation_id": "input-op-02", "session": sid, "events": [{"t": "explode"}]}).encode())
        self.assertEqual(unknown.status, 400)
        self.gw.view["epoch"] = 9
        stale = self.http.handle(
            "POST", "/phone/api/session/input", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"operation_id": "input-op-03", "session": sid,
                        "events": [{"t": "release"}]}).encode())
        self.assertEqual(stale.body["code"], "stale_epoch")
        stopped = self.http.handle(
            "POST", "/phone/api/session/stop", headers(cookie=self.cookie, csrf=body["csrf_token"]),
            json.dumps({"session": sid}).encode())
        self.assertEqual(stopped.body["code"], "body_not_relayable")
        self.assertEqual(self.gw.relays(), [])
        self.ing.identify(headers(peer="other", cookie=self.cookie, csrf=body["csrf_token"]))
        other = self.pair_other()
        foreign = self.http.handle(
            "POST", "/phone/api/session/input", headers(peer="other", cookie=other[0], csrf=other[1]),
            json.dumps({"operation_id": "input-op-04", "session": sid, "events": [{"t": "release"}]}).encode())
        self.assertEqual(foreign.status, 404)
        self.assertEqual(self.gw.relays(), [])

    def test_reads_logout_and_shell_do_not_relay(self):
        self.activate()
        _issued, body = self.ready()
        overview = self.http.handle("GET", "/phone/api/overview", headers(cookie=self.cookie, csrf=body["csrf_token"]))
        self.assertEqual(overview.body["code"], "body_not_relayable")
        fleet = self.http.handle("GET", "/phone/api/fleet", headers(cookie=self.cookie, csrf=body["csrf_token"]))
        self.assertEqual(fleet.status, 503)
        frame = self.http.handle("GET", "/phone/api/session/frame?session=" + "cd" * 16,
                                 headers(cookie=self.cookie, csrf=body["csrf_token"]))
        self.assertIn(frame.status, (404, 503))
        self.assertEqual(self.gw.relays(), [])
        assert_absent(self.blob(), "100.64.0.50")
        locate = self.http.handle("GET", "/phone/api/locate", headers(cookie=self.cookie, csrf=body["csrf_token"]))
        self.assertEqual(locate.status, 404)
        shell = self.http.handle("GET", "/", headers())
        self.assertEqual(shell.status, 404)
        self.assertEqual(shell.body["code"], "shell_not_ingress")
        put = self.http.handle("PUT", "/phone/api/me", headers(cookie=self.cookie, csrf=body["csrf_token"]))
        self.assertEqual(put.status, 405)
        done = self.http.handle("POST", "/phone/api/logout", headers(cookie=self.cookie, csrf=body["csrf_token"]), b"{}")
        self.assertEqual(done.body["ending"], "local_only")
        self.assertEqual(self.gw.relays(), [])
        again = self.http.handle("GET", "/phone/api/me", headers(cookie=self.cookie, csrf=body["csrf_token"]))
        self.assertEqual(again.status, 401)
        projected = self.ing._project("ask", {"status": 400, "body": {"error": SECRET, "reply": REPLY}})
        self.assertNotIn(SECRET, json.dumps(projected))
        self.assertNotIn(REPLY, json.dumps(projected))
        framed = self.ing._project("session_frame", {"status": 200, "raw": FRAME, "path": "/tmp/x"})
        self.assertNotIn("path", framed)
        self.assertNotIn(FRAME, self.blob())

    def ready(self):
        issued = self.ing.issue_code(self.owner(), "Pixel")
        self.http.handle("POST", "/phone/pair", headers(),
                         json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(),
                                json.dumps({"code": issued["code"]}).encode())
        self.cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        return issued, done.body

    def pair_other(self):
        issued = self.ing.issue_code(self.owner(), "Other")
        self.http.handle("POST", "/phone/pair", headers(peer="other"),
                         json.dumps({"code": issued["code"], "name": "Other"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(peer="other"),
                                json.dumps({"code": issued["code"]}).encode())
        cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        return cookie, done.body["csrf_token"]


class Pairing(Base):
    def test_pairing_auth_and_no_plaintext_secrets(self):
        self.activate()
        issued = self.ing.issue_code(self.owner(), "Pixel")
        early_status = self.http.handle("POST", "/phone/pair/status", headers(),
                                        json.dumps({"code": issued["code"]}).encode())
        self.assertEqual(early_status.status, 400)
        pending = self.http.handle("POST", "/phone/pair", headers(),
                                   json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.assertEqual(pending.status, 202)
        self.assertNotIn("Set-Cookie", pending.headers)
        early = self.http.handle("POST", "/phone/pair/status", headers(peer="other"),
                                 json.dumps({"code": issued["code"]}).encode())
        self.assertIn(early.status, (400, 409))
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(),
                                json.dumps({"code": issued["code"]}).encode())
        self.assertEqual(done.status, 200)
        self.assertFalse(done.body["manage_main"])
        cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        row = self.ing._db.execute("SELECT manage_main FROM phones WHERE id=?", (done.body["phone"]["id"],)).fetchone()
        self.assertEqual(row["manage_main"], 0)
        blob = self.blob()
        assert_absent(blob, cookie)
        assert_absent(blob, done.body["csrf_token"])
        if re.search(r"[G-Z]", issued["code"]):
            assert_absent(blob, issued["code"])
        desktop = self.http.handle("POST", "/phone/pair", headers(peer="desktop"),
                                   json.dumps({"code": "ZZZZZZZZ"}).encode())
        self.assertEqual(desktop.status, 403)
        blocked = self.http.handle("POST", "/phone/pair", headers(peer="pc"),
                                   json.dumps({"code": issued["code"]}).encode())
        self.assertEqual(blocked.status, 403)
        self.assertIn("computers", blocked.body["error"])
        for _ in range(5):
            bad = self.http.handle("POST", "/phone/pair", headers(),
                                   json.dumps({"code": "ZZZZZZZZ"}).encode())
            self.assertEqual(bad.status, 403)
        limited = self.http.handle("POST", "/phone/pair", headers(),
                                   json.dumps({"code": "YYYYYYYY"}).encode())
        self.assertEqual(limited.status, 429)
        missing = self.http.handle("GET", "/phone/api/me", headers())
        self.assertEqual(missing.status, 401)
        self.assertTrue(missing.body["pair"])
        csrf = self.http.handle("GET", "/phone/api/me", headers(cookie=cookie))
        self.assertEqual(csrf.status, 403)
        self.assertEqual(csrf.body["code"], "csrf")
        cross = self.http.handle("POST", "/phone/api/ask", headers(cookie=cookie, csrf=done.body["csrf_token"],
                                                                   origin="https://evil.example", site="cross-site"),
                                 json.dumps({"text": "hi there friend", "key": "ask-key-09"}).encode())
        self.assertEqual(cross.status, 403)
        forged = {"role": "phone", "phone_id": done.body["phone"]["id"]}
        with self.assertRaises(IngressError) as raised:
            self.ing.submit(forged, "op-meta-09", {"attempt_id": "attempt-1"})
        self.assertEqual(raised.exception.code, "bad_request")
        self.assertNotIsInstance(forged, Principal)


class MainChange(Base):
    def test_passkey_is_not_an_owner_proof(self):
        self.activate()
        _issued, body = self.ready()
        self.ing.identify(headers(cookie=self.cookie, csrf=body["csrf_token"]))
        principal = self.ing.authenticate(self.cookie, body["csrf_token"], ORIGIN)
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        self.assertEqual(raised.exception.code, "forbidden")
        self.assertEqual(self.gw.transitions_called(), [])
        self.ing.grant_manage_main(self.owner(), body["phone"]["id"])
        preview = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell", "profile": "control"})
        self.assertEqual(self.gw.transitions_called(), [])
        assert_absent(self.blob(), preview["challenge"])
        self.ing._passkeys = None
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"], {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "passkey_unavailable")
        self.assertEqual(self.gw.transitions_called(), [])
        self.ing._passkeys = self.passkey
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"],
                                        {"assertion": "good", "challenge": "wrong-challenge"})
        self.assertEqual(raised.exception.code, "forbidden")
        self.assertEqual(self.passkey_calls, [])
        saved = self.mint
        self.ing._owner_proof = lambda payload: {"receipt": PASSKEY}
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"],
                                        {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "owner_proof_required")
        self.assertEqual(self.gw.transitions_called(), [])
        assert_absent(self.blob(), PASSKEY)
        self.ing._owner_proof = saved
        self.passkey_calls.clear()
        self.gw.view["epoch"] = 8
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"],
                                        {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "stale")
        self.assertEqual(self.passkey_calls, [])
        self.assertEqual(self.gw.transitions_called(), [])
        self.gw.view["epoch"] = 3
        first = self.ing.manage_main_confirm(principal, preview["preview_id"],
                                            {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(first["transition_id"], "tr-1")
        self.assertFalse(first["duplicate"])
        self.assertEqual(len(self.gw.transitions_called()), 1)
        self.assertFalse(self.gw.transitions_called()[0][3])
        self.assertFalse(self.gw.transitions_called()[0][4])
        self.assertNotIn("caller", self.proofs[-1])
        self.assertNotIn("receipt", self.proofs[-1])
        assert_absent(self.blob(), "receipt-ok-secret")
        assert_absent(self.blob(), PASSKEY)
        second = self.ing.manage_main_confirm(principal, preview["preview_id"],
                                             {"assertion": "good", "challenge": preview["challenge"]})
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.gw.transitions_called()), 1)

    def test_uncertain_confirm_reuses_the_client_key_without_a_receipt(self):
        self.activate()
        _issued, body = self.ready()
        self.ing.identify(headers(cookie=self.cookie, csrf=body["csrf_token"]))
        principal = self.ing.authenticate(self.cookie, body["csrf_token"], ORIGIN)
        self.ing.grant_manage_main(self.owner(), body["phone"]["id"])
        preview = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        self.gw.transition_handler = lambda proof, payload: (_ for _ in ()).throw(GatewayError("unavailable"))
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"],
                                        {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "unavailable")
        self.gw.transition_handler = None
        calls = len(self.passkey_calls)
        again = self.ing.manage_main_confirm(principal, preview["preview_id"],
                                            {"assertion": "again", "challenge": "nope"})
        self.assertEqual(again["transition_id"], "tr-1")
        self.assertEqual(len(self.passkey_calls), calls)
        self.assertEqual(self.proofs[-1]["client_key"], preview["preview_id"])
        self.assertNotIn("reconcile", self.proofs[-1])
        self.assertNotIn("receipt", self.proofs[-1])
        assert_absent(self.blob(), "receipt-ok-secret")

    def ready(self):
        issued = self.ing.issue_code(self.owner(), "Pixel")
        self.http.handle("POST", "/phone/pair", headers(),
                         json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(),
                                json.dumps({"code": issued["code"]}).encode())
        self.cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        return issued, done.body


class Contract(unittest.TestCase):
    def test_phone_constants_and_schema_have_no_body_columns(self):
        self.assertEqual(APPROVALS.pattern, _APPROVALS.pattern)
        self.assertEqual(ingress_mod.PHONE_COOKIE, COOKIE)
        self.assertEqual(ingress_mod.CODE_CHARS, CODE_CHARS)
        self.assertEqual(ingress_mod.TAILNET, TAILNET)
        self.assertEqual(ingress_mod.ASK_MAX, ASK_MAX)
        self.assertEqual(ingress_mod.MAX_BODY, MAX_BODY)
        self.assertEqual(ingress_mod.PAIR_PATHS, PAIR_PATHS)
        self.assertEqual(set(PHONE_SHELL), set(SHELL))
        self.assertEqual(FRAME_MAX, MAX_FRAME)
        class _Headers:
            def get_all(self, name):
                return [f"{PHONE_COOKIE}=abc"] if name == "Cookie" else []

            def get(self, name, default=None):
                return f"{PHONE_COOKIE}=abc" if name == "Cookie" else default

        self.assertEqual(ingress_mod.cookie_values(_Headers()), phone_cookie_values(_Headers()))
        self.assertEqual(PC_SEAM["function"], "accept_gateway_phone")
        self.assertIn("raw __Host-jarvis_phone cookie", " ".join(PC_SEAM["must_refuse"]))
        self.assertIn("X-Forwarded-For", " ".join(PC_SEAM["must_refuse"]))
        self.assertIn("not_called", PC_SEAM)
        self.assertEqual(GATEWAY_METHODS, ("status", "relay_metadata", "transition_request"))
        self.assertIn("consume", GATEWAY_NOT_OURS)
        with tempfile.TemporaryDirectory() as folder:
            gw = Gateway()
            ing = MobileIngress(
                Path(folder) / "ingress.sqlite", gw, origin=ORIGIN, installation_id="oracle-inst",
                allowed_mains={"zenbook": pin_target("zenbook", "https://zenbook.tailnet.ts.net", "100.64.0.10")},
                peer_verifier=peer_from, blocked_stable_ids=["pc-zenbook"], clock=Clock())
            try:
                columns = []
                for table in ("phones", "codes", "operations", "sessions", "previews"):
                    for row in ing._db.execute(f"PRAGMA table_info({table})"):
                        columns.append(row[1])
                joined = " ".join(columns)
                for banned in ("prompt", "reply", "cookie", "frame", "events"):
                    self.assertNotIn(banned, joined)
                self.assertIn("token_sha256", joined)
                self.assertIn("csrf_sha256", joined)
            finally:
                ing.close()


def _keys(folder):
    made = {}
    for name in ("owner", "ingress"):
        path = Path(folder) / name
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-continuity-test", "-f", str(path)],
            check=True, capture_output=True, timeout=10)
        token = (Path(folder) / f"{name}.pub").read_text(encoding="ascii").split()
        made[name] = (f"{token[0]} {token[1]}", path)
    return made


def _owner_call(gateway, purpose, payload, private_key):
    digest = payload_digest(payload)
    challenge = gateway.issue_challenge(digest)
    issued = int(gateway._wall())
    nonce = "n-" + secrets.token_hex(8)
    signature = sign_message(OWNER_NS, private_key, _message(OWNER_NS, {
        "v": 1, "challenge_id": challenge["challenge_id"], "nonce": nonce,
        "issued_at": issued, "payload_digest": digest,
    }))
    proof = gateway.authenticate_owner(
        challenge_id=challenge["challenge_id"], nonce=nonce, issued_at=issued,
        purpose=purpose, payload=payload, signature=signature)
    if purpose == "enroll":
        return gateway.enroll(proof, payload)
    raise AssertionError(purpose)


class RealGateway(unittest.TestCase):
    def test_session_uses_signed_metadata_and_refuses_a_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            keys = _keys(folder)
            gw = ContinuityGateway.open(
                Path(folder) / "authority.sqlite", "home", "oracle-auth", [keys["owner"][0]])
            try:
                self.assertIsNone(gw.listening)
                _owner_call(gw, "enroll", {
                    "device_id": "ingress-gw", "installation_id": "ingress-inst", "kind": "client",
                    "public_key": keys["ingress"][0], "capabilities": ["status", "client"],
                }, keys["owner"][1])
                seen = []
                original = gw.relay

                def wrapped(principal, payload):
                    seen.append(dict(payload))
                    return original(principal, payload)

                gw.relay = wrapped
                session = ContinuityGatewaySession(
                    gw, "ingress-gw", "ingress-inst", keys["ingress"][1])
                status = session.status()
                self.assertEqual(status["mode"], "LEGACY_ACTIVE")
                self.assertEqual(status["authority_id"], "oracle-auth")
                ing = MobileIngress(
                    Path(folder) / "ingress.sqlite", session, origin=ORIGIN,
                    installation_id="oracle-inst",
                    allowed_mains={"zenbook": pin_target("zenbook", "https://zenbook.tailnet.ts.net", "100.64.0.10")},
                    peer_verifier=peer_from, blocked_stable_ids=["pc-zenbook"])
                try:
                    with self.assertRaises(IngressError) as raised:
                        ing.activate(status["mode_version"], legacy_phone_fenced=True)
                    self.assertEqual(raised.exception.code, "legacy_retained")
                    asked = PhoneProtocolAdapter(ing).handle(
                        "POST", "/phone/pair", headers(), json.dumps({"code": "ABCDEFGH"}).encode())
                    self.assertEqual(asked.body["code"], "legacy_retained")
                    self.assertEqual(seen, [])
                    with self.assertRaises(GatewayError) as relayed:
                        session.relay_metadata("op-meta-01", "zenbook", "attempt-1", "sha256:" + "ab" * 32, "client")
                    self.assertEqual(relayed.exception.code, "no_relay")
                    self.assertEqual(set(seen[0]), {"operation_id", "target", "attempt_id", "digest", "capability"})
                    self.assertNotIn("prompt", seen[0])
                    self.assertNotIn(SECRET, json.dumps(seen))
                finally:
                    ing.close()
                smuggled = {
                    "operation_id": "op-meta-02", "target": "zenbook", "attempt_id": "attempt-1",
                    "digest": "sha256:" + "cd" * 32, "capability": "client", "prompt": SECRET,
                }
                issued = int(gw._wall())
                nonce = "n-" + secrets.token_hex(8)
                signature = sign_message(DEVICE_NS, keys["ingress"][1], _message(DEVICE_NS, {
                    "v": 1, "device_id": "ingress-gw", "installation_id": "ingress-inst",
                    "nonce": nonce, "issued_at": issued, "purpose": "relay",
                    "payload_digest": payload_digest(smuggled),
                }))
                principal = gw.authenticate_device(
                    device_id="ingress-gw", installation_id="ingress-inst", nonce=nonce,
                    issued_at=issued, purpose="relay", payload=smuggled, signature=signature)
                with self.assertRaises(GatewayError) as refused:
                    original(principal, smuggled)
                self.assertEqual(refused.exception.code, "bad_request")
                self.assertNotIn(SECRET, str(refused.exception))
                self.assertNotIn(SECRET.encode(), Path(folder, "authority.sqlite").read_bytes())
                forged = GatewayOwnerProof(object(), "owner-test", "transition", "sha256:" + "ab" * 32)
                with self.assertRaises(GatewayError) as refused:
                    session.transition_request(forged, {
                        "client_key": "pv-test", "action": "recover", "candidate": "zenbook",
                        "expected_incarnation": status["incarnation"], "expected_epoch": status["epoch"],
                        "expected_policy": status["policy_version"], "profile": "control",
                    })
                self.assertEqual(refused.exception.code, "unauthorized")
                self.assertEqual(gw.store.status()["mode"], "LEGACY_ACTIVE")
            finally:
                gw.close()


if __name__ == "__main__":
    unittest.main()
