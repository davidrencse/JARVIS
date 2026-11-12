"""Real signature checks for the phone ingress. Not a live failover drill.

Run: JARVIS_NO_MODELS=1 .venv/bin/python -m unittest tests.continuity.test_phone_ingress_security
"""

import base64
import hashlib
import json
import subprocess
import threading
import unittest
from pathlib import Path

from jarvis.continuity.authority import TICKET_FIELDS
from jarvis.continuity.gateway import (
    TICKET_NS,
    ContinuityGateway,
    GatewayError,
    OwnerProof as GatewayOwnerProof,
    _message,
    sign_message,
)
from jarvis.continuity.ingress import (
    ENVELOPE_FIELDS,
    GATEWAY_SESSION_DEPENDENCIES,
    PHONE_DOMAIN,
    PHONE_ENVELOPE,
    PHONE_PURPOSE,
    PREVIEW_TTL,
    EnvelopeSigner,
    IngressError,
    MobileIngress,
    NetworkGatewaySession,
    OwnerAssertionBridge,
    PhoneProtocolAdapter,
    TicketSignatureVerifier,
    WebAuthnAssertionVerifier,
    _envelope,
    _project_trace,
    _public_effect,
    pin_target,
    production_ingress,
    verify_phone_envelope,
)
from jarvis.phone import GatewayPhoneVerifier, _GATEWAY_PHONE_FIELDS, gateway_phone_digest
from tests.continuity.test_ingress import ORIGIN, Base, assert_absent, headers, owner_from, peer_from

_OPENSSL = "/usr/bin/openssl"
_RP_ID = "oracle.tailnet.ts.net"
_CRED = "cGhvbmUtY3JlZA"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value):
    pad = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _ssh_key(folder, name):
    folder = Path(folder)
    path = folder / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-continuity-test", "-f", str(path)],
        check=True, capture_output=True, timeout=10)
    token = path.with_suffix(".pub").read_text(encoding="ascii").split()
    return f"{token[0]} {token[1]}", path


def _p256(folder):
    folder = Path(folder)
    key = folder / "p256.pem"
    pub = folder / "p256.pub.pem"
    subprocess.run(
        [_OPENSSL, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
        check=True, capture_output=True, timeout=10)
    subprocess.run(
        [_OPENSSL, "ec", "-in", str(key), "-pubout", "-out", str(pub)],
        check=True, capture_output=True, timeout=10)
    return key, pub.read_text(encoding="ascii")


def _sign_assertion(private_key, auth, client):
    message = auth + hashlib.sha256(client).digest()
    signed = subprocess.run(
        [_OPENSSL, "dgst", "-sha256", "-sign", str(private_key)],
        input=message, capture_output=True, check=True, timeout=10)
    return signed.stdout


def _client_json(challenge, origin, *, cross=False, omit_cross=False):
    data = {"type": "webauthn.get", "challenge": challenge, "origin": origin}
    if not omit_cross:
        data["crossOrigin"] = cross
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def _assertion(private_key, *, challenge, origin, rp_id, credential_id, counter=1, flags=0x05,
               client=None):
    auth = hashlib.sha256(rp_id.encode("utf-8")).digest() + bytes([flags]) + counter.to_bytes(4, "big")
    if client is None:
        client = _client_json(challenge, origin)
    signature = _sign_assertion(private_key, auth, client)
    return {
        "id": credential_id, "type": "public-key",
        "response": {
            "clientDataJSON": _b64(client),
            "authenticatorData": _b64(auth),
            "signature": _b64(signature),
        },
    }


def _ticket(epoch=3):
    return {
        "admission_id": "adm-1", "target": "zenbook", "operation_id": "op-ticket-1",
        "attempt_id": "a-0001", "digest": "sha256:" + "ab" * 32, "main": "zenbook",
        "incarnation": 2, "epoch": epoch, "issued_at": 100, "admit_until": 200,
        "issuer_incarnation": 2,
    }


class Crypto(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.folder = Path(self._dir.name)

    def test_ticket_signature_accepts_a_real_signature_and_rejects_tamper(self):
        public, private = _ssh_key(self.folder, "main")
        other, _other_private = _ssh_key(self.folder, "other")
        fields = _ticket()
        signature = sign_message(TICKET_NS, private, _message(TICKET_NS, fields))
        signed = {**fields, "authority_id": "oracle-auth", "signature": signature}
        verifier = TicketSignatureVerifier({"zenbook": public})
        self.assertTrue(verifier(signed))
        tampered = dict(signed, epoch=4)
        self.assertFalse(verifier(tampered))
        self.assertFalse(verifier(dict(signed, main="dell")))
        missing = dict(signed)
        missing.pop("signature")
        self.assertFalse(verifier(missing))
        incomplete = {key: value for key, value in signed.items() if key != "admit_until"}
        self.assertFalse(verifier(incomplete))
        self.assertFalse(TicketSignatureVerifier({"zenbook": other})(signed))
        self.assertFalse(verifier("nope"))
        self.assertTrue(verifier.verify(signed))
        self.assertFalse(verifier.verify(tampered))
        with self.assertRaises(IngressError):
            TicketSignatureVerifier({})
        with self.assertRaises(IngressError):
            TicketSignatureVerifier({"zenbook": "not-a-key"})

    def test_webauthn_assertion_accepts_openssl_and_rejects_tamper(self):
        private, public = _p256(self.folder)
        origin = "https://" + _RP_ID
        verifier = WebAuthnAssertionVerifier(
            credential_id=_CRED, public_pem=public, rp_id=_RP_ID, origin=origin)
        self.assertEqual(verifier.credential_ids, (_CRED,))
        self.assertEqual(verifier.rp_id, _RP_ID)
        good = _assertion(private, challenge="challenge-1", origin=origin, rp_id=_RP_ID, credential_id=_CRED)
        self.assertTrue(verifier.verify(good, "challenge-1"))
        replay = _assertion(private, challenge="challenge-1", origin=origin, rp_id=_RP_ID,
                            credential_id=_CRED, counter=1)
        self.assertFalse(verifier.verify(replay, "challenge-1"))
        nxt = _assertion(private, challenge="challenge-1", origin=origin, rp_id=_RP_ID,
                         credential_id=_CRED, counter=2)
        self.assertTrue(verifier.verify(nxt, "challenge-1"))

        fresh = WebAuthnAssertionVerifier(
            credential_id=_CRED, public_pem=public, rp_id=_RP_ID, origin=origin)
        broken = _assertion(private, challenge="challenge-1", origin=origin, rp_id=_RP_ID, credential_id=_CRED)
        raw = bytearray(_b64decode(broken["response"]["signature"]))
        raw[0] ^= 0x01
        broken["response"]["signature"] = _b64(bytes(raw))
        self.assertFalse(fresh.verify(broken, "challenge-1"))
        self.assertTrue(fresh.verify(good, "challenge-1"))

        self.assertFalse(fresh.verify(good, "other-challenge"))
        wrong_origin = _assertion(
            private, challenge="c-origin", origin="https://evil.example", rp_id=_RP_ID, credential_id=_CRED, counter=3)
        self.assertFalse(fresh.verify(wrong_origin, "c-origin"))
        wrong_rp = _assertion(
            private, challenge="c-rp", origin=origin, rp_id="evil.example", credential_id=_CRED, counter=4)
        self.assertFalse(fresh.verify(wrong_rp, "c-rp"))
        no_uv = _assertion(
            private, challenge="c-uv", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=5, flags=0x01)
        self.assertFalse(fresh.verify(no_uv, "c-uv"))
        no_up = _assertion(
            private, challenge="c-up", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=6, flags=0x04)
        self.assertFalse(fresh.verify(no_up, "c-up"))
        attested = _assertion(
            private, challenge="c-at", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=11, flags=0x45)
        self.assertFalse(fresh.verify(attested, "c-at"))
        crossed = _client_json("c-cross", origin, cross=True)
        cross_assertion = _assertion(
            private, challenge="c-cross", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=7, client=crossed)
        self.assertFalse(fresh.verify(cross_assertion, "c-cross"))
        omitted = _client_json("c-omit", origin, omit_cross=True)
        omit_assertion = _assertion(
            private, challenge="c-omit", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=8, client=omitted)
        self.assertFalse(fresh.verify(omit_assertion, "c-omit"))

        zeros = WebAuthnAssertionVerifier(
            credential_id=_CRED, public_pem=public, rp_id=_RP_ID, origin=origin)
        first = _assertion(private, challenge="z-1", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=0)
        second = _assertion(private, challenge="z-2", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=0)
        self.assertTrue(zeros.verify(first, "z-1"))
        self.assertTrue(zeros.verify(second, "z-2"))
        self.assertFalse(zeros.verify(first, "z-1"))

        private_pem = private.read_text(encoding="ascii")
        with self.assertRaises(IngressError):
            WebAuthnAssertionVerifier(credential_id=_CRED, public_pem=private_pem, rp_id=_RP_ID, origin=origin)

        rsa = self.folder / "rsa.pem"
        rsa_pub = self.folder / "rsa.pub.pem"
        subprocess.run(
            [_OPENSSL, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(rsa)],
            check=True, capture_output=True, timeout=20)
        subprocess.run(
            [_OPENSSL, "pkey", "-in", str(rsa), "-pubout", "-out", str(rsa_pub)],
            check=True, capture_output=True, timeout=10)
        with self.assertRaises(IngressError):
            WebAuthnAssertionVerifier(
                credential_id=_CRED, public_pem=rsa_pub.read_text(encoding="ascii"), rp_id=_RP_ID, origin=origin)
        p384 = self.folder / "p384.pem"
        p384_pub = self.folder / "p384.pub.pem"
        subprocess.run(
            [_OPENSSL, "ecparam", "-name", "secp384r1", "-genkey", "-noout", "-out", str(p384)],
            check=True, capture_output=True, timeout=10)
        subprocess.run(
            [_OPENSSL, "ec", "-in", str(p384), "-pubout", "-out", str(p384_pub)],
            check=True, capture_output=True, timeout=10)
        with self.assertRaises(IngressError):
            WebAuthnAssertionVerifier(
                credential_id=_CRED, public_pem=p384_pub.read_text(encoding="ascii"), rp_id=_RP_ID, origin=origin)

        mismatched = _assertion(private, challenge="raw-1", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=9)
        mismatched["rawId"] = "bm90LXRoZS1pZA"
        raw_verifier = WebAuthnAssertionVerifier(
            credential_id=_CRED, public_pem=public, rp_id=_RP_ID, origin=origin)
        self.assertFalse(raw_verifier.verify(mismatched, "raw-1"))
        matched = _assertion(private, challenge="raw-2", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=10)
        matched["rawId"] = _CRED
        self.assertTrue(raw_verifier.verify(matched, "raw-2"))

        raced = WebAuthnAssertionVerifier(
            credential_id=_CRED, public_pem=public, rp_id=_RP_ID, origin=origin)
        assertion = _assertion(private, challenge="race-1", origin=origin, rp_id=_RP_ID, credential_id=_CRED, counter=3)
        results = []
        barrier = threading.Barrier(8)

        def once():
            barrier.wait(2)
            results.append(raced.verify(assertion, "race-1"))

        threads = [threading.Thread(target=once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)

    def test_signed_claim_contract_names_the_pc_verifier(self):
        self.assertEqual(tuple(PHONE_ENVELOPE["signed_fields"]), tuple(_GATEWAY_PHONE_FIELDS))
        self.assertEqual(tuple(ENVELOPE_FIELDS), tuple(_GATEWAY_PHONE_FIELDS))
        self.assertEqual(PHONE_ENVELOPE["purpose"], PHONE_PURPOSE)
        self.assertEqual(PHONE_PURPOSE, "gateway_phone")
        self.assertEqual(PHONE_ENVELOPE["domain"], PHONE_DOMAIN)
        self.assertEqual(PHONE_DOMAIN, "jarvis-continuity-gateway-phone-v1")
        self.assertEqual(PHONE_ENVELOPE["namespace"], "jarvis-continuity-device-v1")
        self.assertEqual(PHONE_ENVELOPE["wire"], "UTF-8 JSON {v:1, claim, signature, body}")
        self.assertIn("phone_id", PHONE_ENVELOPE["signed_fields"])
        self.assertIn("manage_main", PHONE_ENVELOPE["signed_fields"])
        self.assertIn("attempt_id", PHONE_ENVELOPE["signed_fields"])
        self.assertIn("issuer_incarnation", PHONE_ENVELOPE["signed_fields"])
        self.assertNotIn("body_digest", PHONE_ENVELOPE["signed_fields"])
        self.assertIn("verify_phone_envelope", PHONE_ENVELOPE["verifier"])
        self.assertIn("GatewayPhoneVerifier", PHONE_ENVELOPE["verifier"])
        self.assertEqual(GATEWAY_SESSION_DEPENDENCIES["network_class"], "NetworkGatewaySession")
        self.assertIn("unsupported", GATEWAY_SESSION_DEPENDENCIES["network_transient"])
        self.assertIn("unsupported", GATEWAY_SESSION_DEPENDENCIES["network_transition"])
        self.assertFalse(NetworkGatewaySession.transient_supported)
        self.assertEqual(len(TICKET_FIELDS), 11)


class IngressSecurity(Base):
    def open_phone(self):
        self.activate()
        issued = self.ing.issue_code(self.owner(), "Pixel")
        self.http.handle("POST", "/phone/pair", headers(),
                         json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", headers(),
                                json.dumps({"code": issued["code"]}).encode())
        self.assertEqual(done.status, 200, done.body)
        self.cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        self.csrf = done.body["csrf_token"]
        self.phone_id = done.body["phone"]["id"]
        self.body = done.body
        return done.body

    def principal(self):
        self.ing.identify(headers(cookie=self.cookie, csrf=self.csrf))
        return self.ing.authenticate(self.cookie, self.csrf, ORIGIN)

    def post(self, path, payload, cookie=None, csrf=None, peer="phone"):
        return self.http.handle(
            "POST", path, headers(peer=peer, cookie=cookie or self.cookie, csrf=csrf or self.csrf),
            json.dumps(payload).encode())

    def enable_relay(self, watch=None):
        public, private = _ssh_key(self.tmp.name, "relay-ingress")
        self.relay_public = public
        self.ing._signer = EnvelopeSigner(private)
        gateway = self.gw
        gateway.envelopes = []
        gateway.transient_ids = []
        gateway.attempts = []
        gateway.flip_after = False
        gateway.fail = None
        gateway.read_body = {"ok": True}

        def relay_transient(operation_id, target, attempt_id, digest, capability, kind, envelope):
            del digest, capability
            gateway.calls.append(("transient", operation_id, kind, target))
            gateway.envelopes.append(bytes(envelope))
            gateway.transient_ids.append((kind, operation_id))
            gateway.attempts.append(attempt_id)
            if watch is not None:
                watch()
            if gateway.fail is not None:
                raise gateway.fail
            if gateway.flip_after:
                gateway.view["epoch"] = 99
            if kind in ("ask", "session_start", "session_input", "session_stop", "logout_end"):
                return {"operation_id": operation_id, "relayed": True, "effect_authorized": False,
                        "upstream_accepted": True}
            return {"operation_id": operation_id, "relayed": True, "effect_authorized": False,
                    "result": {"status": 200, "body": gateway.read_body}}

        gateway.relay_transient = relay_transient

    def test_preview_confirm_and_me_expose_manage_main(self):
        body = self.open_phone()
        me = self.http.handle("GET", "/phone/api/me", headers(cookie=self.cookie, csrf=self.csrf))
        self.assertFalse(me.body["manage_main"])
        denied = self.post("/phone/api/main/preview", {"action": "recover", "candidate": "dell"})
        self.assertEqual(denied.status, 403)
        self.assertEqual(self.gw.transitions_called(), [])
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        me = self.http.handle("GET", "/phone/api/me", headers(cookie=self.cookie, csrf=self.csrf))
        self.assertTrue(me.body["manage_main"])
        preview = self.post("/phone/api/main/preview", {"action": "recover", "candidate": "dell", "profile": "control"})
        self.assertEqual(preview.status, 200, preview.body)
        self.assertEqual(preview.body["rp_id"], "")
        self.assertEqual(preview.body["credential_ids"], [])
        self.assertNotIn(preview.body["challenge"].encode(), self.blob())
        self.assertNotIn(body["csrf_token"].encode(), self.blob())
        conflict = self.post("/phone/api/main/confirm", {
            "preview_id": preview.body["preview_id"],
            "proof": {"assertion": "good", "challenge": preview.body["challenge"]},
            "passkey_proof": {"assertion": "good", "challenge": "other-value"},
        })
        self.assertEqual(conflict.status, 403)
        self.assertEqual(self.passkey_calls, [])
        self.assertEqual(self.gw.transitions_called(), [])
        bad = self.post("/phone/api/main/confirm", {
            "preview_id": preview.body["preview_id"],
            "proof": {"assertion": "nope", "challenge": preview.body["challenge"]},
        })
        self.assertEqual(bad.status, 403)
        self.assertEqual(self.gw.transitions_called(), [])
        done = self.post("/phone/api/main/confirm", {
            "preview_id": preview.body["preview_id"],
            "passkey_proof": {"assertion": "good", "challenge": preview.body["challenge"]},
        })
        self.assertEqual(done.status, 200, done.body)
        self.assertEqual(done.body["transition_id"], "tr-1")
        self.assertNotIn("caller", done.body)
        self.assertNotIn("receipt", done.body)
        self.assertNotIn("caller", self.proofs[-1])
        self.assertNotIn("receipt", self.proofs[-1])
        again = self.post("/phone/api/main/preview", {"action": "recover", "candidate": "dell"})
        alias = self.post("/phone/api/main/confirm", {
            "preview_id": again.body["preview_id"],
            "proof": {"assertion": "good", "challenge": again.body["challenge"]},
        })
        self.assertEqual(alias.status, 200, alias.body)
        self.assertFalse(alias.body["duplicate"])

    def test_uncertain_reconcile_requires_the_verified_preview(self):
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        principal = self.principal()
        preview = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        self.gw.transition_handler = lambda proof, payload: (_ for _ in ()).throw(GatewayError("unavailable"))
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"],
                                        {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "unavailable")
        self.assertIn(preview["preview_id"], self.ing._passkey_verified)
        calls, proofs = len(self.passkey_calls), len(self.proofs)
        self.gw.transition_handler = None
        again = self.ing.manage_main_confirm(principal, preview["preview_id"],
                                            {"assertion": "again", "challenge": "nope"})
        self.assertEqual(again["transition_id"], "tr-1")
        self.assertEqual(len(self.passkey_calls), calls)
        self.assertEqual(self.proofs[-1]["client_key"], preview["preview_id"])
        self.assertNotIn("receipt", self.proofs[-1])

        other = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        self.gw.transition_handler = lambda proof, payload: (_ for _ in ()).throw(GatewayError("unavailable"))
        with self.assertRaises(IngressError):
            self.ing.manage_main_confirm(principal, other["preview_id"],
                                        {"assertion": "good", "challenge": other["challenge"]})
        calls = len(self.passkey_calls)
        self.ing._passkey_verified.clear()
        self.gw.transition_handler = None
        row = self.ing._preview(other["preview_id"])
        self.assertEqual(row["authorized"], 1)
        self.assertEqual(row["state"], "uncertain")
        proofs = len(self.proofs)
        again_other = self.ing.manage_main_confirm(principal, other["preview_id"],
                                                  {"assertion": "again", "challenge": "nope"})
        self.assertEqual(again_other["transition_id"], "tr-1")
        self.assertEqual(self.proofs[-1]["client_key"], other["preview_id"])
        self.assertEqual(len(self.proofs), proofs + 1)
        self.assertEqual(len(self.passkey_calls), calls)

        naked = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        self.ing._mark_preview(naked["preview_id"], "uncertain")
        self.ing._passkey_verified.clear()
        proofs = len(self.proofs)
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, naked["preview_id"],
                                        {"assertion": "again", "challenge": "nope"})
        self.assertEqual(raised.exception.code, "forbidden")
        self.assertEqual(len(self.proofs), proofs)
        self.assertEqual(self.ing._preview(naked["preview_id"])["authorized"], 0)

    def test_restart_does_not_mint_for_an_unverified_preview(self):
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        principal = self.principal()
        preview = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        preview_id = preview["preview_id"]
        self.ing._mark_preview(preview_id, "sending")
        cookie, csrf = self.cookie, self.csrf
        self.ing.close()
        revived = self.open()
        self.addCleanup(revived.close)
        revived.activate(4, legacy_phone_fenced=True)
        revived.identify(headers(cookie=cookie, csrf=csrf))
        principal = revived.authenticate(cookie, csrf, ORIGIN)
        proofs, calls = len(self.proofs), len(self.passkey_calls)
        with self.assertRaises(IngressError) as raised:
            revived.manage_main_confirm(principal, preview_id, {"assertion": "again", "challenge": "nope"})
        self.assertEqual(raised.exception.code, "forbidden")
        self.assertEqual(len(self.proofs), proofs)
        self.assertEqual(len(self.passkey_calls), calls)
        row = revived._preview(preview_id)
        self.assertEqual(row["state"], "uncertain")
        self.assertEqual(row["authorized"], 0)

    def test_production_standins_fail_closed(self):
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        principal = self.principal()
        preview = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        called = []
        self.ing._webauthn = lambda *args: called.append("webauthn") or True
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"],
                                        {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "passkey_unavailable")
        self.assertEqual(called, [])
        self.assertEqual(self.gw.transitions_called(), [])
        self.ing._webauthn = None
        self.ing._owner_bridge = lambda payload: called.append(payload) or GatewayOwnerProof(
            object(), "owner-test", "transition", "sha256:" + "ab" * 32)
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"],
                                        {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "owner_proof_required")
        self.assertEqual(called, [])
        self.assertEqual(self.gw.transitions_called(), [])
        self.ing._tickets = lambda signed: called.append("ticket") or True
        self.assertFalse(self.ing.verify_ticket({"signature": "BEGIN SSH SIGNATURE"}))
        self.assertEqual(called, [])

    def test_owner_bridge_and_webauthn_confirm_for_real(self):
        private, public = _p256(self.tmp.name)
        origin = "https://" + _RP_ID
        webauthn = WebAuthnAssertionVerifier(
            credential_id=_CRED, public_pem=public, rp_id=_RP_ID, origin=origin)
        owner_pub, owner_key = _ssh_key(self.tmp.name, "owner")
        gateway = ContinuityGateway.open(
            Path(self.tmp.name) / "authority.sqlite", "home", "oracle-auth", [owner_pub])
        self.addCleanup(gateway.close)
        challenges = []
        issue = gateway.issue_challenge

        def counting(digest):
            challenges.append(digest)
            return issue(digest)

        gateway.issue_challenge = counting
        bridge = OwnerAssertionBridge(gateway, owner_key)
        self.ing._webauthn = webauthn
        self.ing._owner_bridge = bridge
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        preview = self.post("/phone/api/main/preview", {"action": "recover", "candidate": "dell"})
        self.assertEqual(preview.body["rp_id"], _RP_ID)
        self.assertEqual(preview.body["credential_ids"], [_CRED])
        bad = _assertion(private, challenge=preview.body["challenge"], origin=origin, rp_id=_RP_ID,
                         credential_id=_CRED, flags=0x01)
        refused = self.post("/phone/api/main/confirm", {
            "preview_id": preview.body["preview_id"],
            "proof": {"challenge": preview.body["challenge"], "assertion": bad},
        })
        self.assertEqual(refused.status, 403)
        self.assertEqual(challenges, [])
        self.assertEqual(self.gw.transitions_called(), [])
        self.assertEqual(self.passkey_calls, [])
        good = _assertion(private, challenge=preview.body["challenge"], origin=origin, rp_id=_RP_ID,
                          credential_id=_CRED, counter=4)
        done = self.post("/phone/api/main/confirm", {
            "preview_id": preview.body["preview_id"],
            "proof": {"challenge": preview.body["challenge"], "assertion": good},
        })
        self.assertEqual(done.status, 200, done.body)
        self.assertEqual(done.body["transition_id"], "tr-1")
        self.assertNotIn("caller", done.body)
        self.assertNotIn("receipt", done.body)
        self.assertEqual(len(challenges), 1)
        self.assertEqual(self.passkey_calls, [])
        recorded = self.gw.transitions_called()
        self.assertEqual(len(recorded), 1)
        self.assertTrue(recorded[0][1])
        self.assertNotIn("caller", self.gw.transitions_called()[0][2])
        self.assertNotIn("receipt", recorded[0][2])

    def test_stale_main_hides_the_body_and_does_not_retry(self):
        self.enable_relay()
        self.open_phone()
        marker = "SECRET-BODY-not-shown"
        self.gw.read_body = {"reply": marker, "main": "zenbook"}
        self.gw.flip_after = True
        overview = self.http.handle("GET", "/phone/api/overview", headers(cookie=self.cookie, csrf=self.csrf))
        self.assertEqual(overview.status, 409)
        self.assertEqual(overview.body["code"], "stale")
        self.assertNotIn(marker, json.dumps(overview.body))
        self.assertNotIn(marker, " ".join(self.logs))
        self.gw.flip_after = False
        self.gw.view["epoch"] = 3
        asked = self.post("/phone/api/ask", {"key": "op-stale-01", "text": marker})
        self.assertEqual(asked.status, 200, asked.body)
        self.gw.flip_after = True
        self.gw.view["epoch"] = 3
        stale = self.post("/phone/api/ask", {"key": "op-stale-02", "text": "draw the curtains"})
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.body["code"], "stale")
        self.assertNotIn("draw the curtains", json.dumps(stale.body))
        row = self.ing._op("op-stale-02")
        self.assertEqual(row["state"], "uncertain")
        sent = [item for item in self.gw.transient_ids if item[0] == "ask"]
        again = self.post("/phone/api/ask", {"key": "op-stale-02", "text": "draw the curtains"})
        self.assertEqual(again.status, 409)
        self.assertEqual(again.body["code"], "uncertain")
        self.assertEqual([item for item in self.gw.transient_ids if item[0] == "ask"], sent)
        changed = self.post("/phone/api/ask", {"key": "op-stale-02", "text": "open the pod bay doors"})
        self.assertEqual(changed.status, 409)
        self.assertEqual(changed.body["code"], "conflict")
        self.assertEqual([item for item in self.gw.transient_ids if item[0] == "ask"], sent)
        issued = self.ing.issue_code(self.owner(), "Other")
        self.http.handle("POST", "/phone/pair", headers(peer="other"),
                         json.dumps({"code": issued["code"], "name": "Other"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        other = self.http.handle("POST", "/phone/pair/status", headers(peer="other"),
                                 json.dumps({"code": issued["code"]}).encode())
        foreign = self.post(
            "/phone/api/ask", {"key": "op-stale-02", "text": "draw the curtains"},
            cookie=other.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1],
            csrf=other.body["csrf_token"], peer="other")
        self.assertEqual(foreign.status, 409)
        self.assertEqual(foreign.body["code"], "conflict")
        self.assertEqual([item for item in self.gw.transient_ids if item[0] == "ask"], sent)
        self.gw.flip_after = False
        self.gw.view["main"] = "dell"
        self.gw.view["lease_holder"] = "dell"
        fresh = self.post("/phone/api/ask", {"key": "op-stale-03", "text": "draw the curtains"})
        self.assertEqual(fresh.status, 200, fresh.body)
        routed = [item for item in self.gw.calls if item[0] == "transient" and item[1] == "op-stale-03"]
        self.assertEqual(routed, [("transient", "op-stale-03", "ask", "dell")])
        self.assertEqual([item for item in self.gw.transient_ids if item[1] == "op-stale-02"],
                         [("ask", "op-stale-02")])

    def test_trace_and_target_survive_projection(self):
        projected = self.ing._project("ask_poll", {"status": 200, "body": {
            "cursor": 7, "reply": "hello", "target": "zenbook",
            "trace": {
                "available": True, "rev": 7,
                "steps": [
                    {"id": "plan-1", "label": "token=super-secret", "state": "run", "kind": "step",
                     "detail": "https://a:b@host.example/x?q=1"},
                    "not-a-step",
                    {"id": "tool:1", "label": "look", "state": "bogus", "kind": "tool"},
                    {"id": "bare-1"},
                ],
            },
        }})
        public = _public_effect(projected)
        self.assertEqual(public["target"], "zenbook")
        self.assertEqual([step["id"] for step in public["trace"]["steps"]], ["plan-1", "tool:1", "bare-1"])
        self.assertEqual(public["trace"]["rev"], 7)
        self.assertNotIn("super-secret", json.dumps(public))
        self.assertIn("[hidden]", public["trace"]["steps"][0]["label"])
        self.assertNotIn("a:b@", json.dumps(public))
        self.assertNotIn("?q=1", json.dumps(public))
        tool = public["trace"]["steps"][1]
        self.assertNotIn("state", tool)
        self.assertEqual(tool["kind"], "tool")
        self.assertEqual(public["trace"]["steps"][2], {"id": "bare-1"})
        self.assertNotIn("Step", json.dumps(public["trace"]["steps"]))
        self.assertNotIn("Tool", json.dumps(public["trace"]["steps"]))
        cursor_only = _public_effect(self.ing._project("ask_poll", {"status": 200, "body": {"cursor": 7}}))
        self.assertEqual(cursor_only["trace"], {"rev": 7})
        self.assertNotIn("steps", cursor_only["trace"])
        self.assertIsNone(_project_trace({"steps": ["nope", 3]}))

        self.enable_relay()
        self.open_phone()
        asked = self.post("/phone/api/ask", {"key": "op-trace-01", "text": "draw the curtains"})
        self.assertEqual(asked.status, 200, asked.body)
        self.assertEqual(asked.body.get("target"), "zenbook")
        self.gw.read_body = {
            "target": "zenbook", "cursor": 4,
            "trace": {"rev": 4, "steps": [{"id": "plan-1", "label": "token=super-secret", "state": "run", "kind": "step"}]},
        }
        found = self.http.handle("GET", "/phone/api/effect/op-trace-01", headers(cookie=self.cookie, csrf=self.csrf))
        self.assertEqual(found.status, 200, found.body)
        self.assertEqual(found.body["target"], "zenbook")
        self.assertEqual(found.body["trace"]["steps"][0]["id"], "plan-1")
        self.assertNotIn("super-secret", json.dumps(found.body))
        self.assertNotEqual(found.body["trace"], {"rev": 4})
        issued = self.ing.issue_code(self.owner(), "Other")
        self.http.handle("POST", "/phone/pair", headers(peer="other"),
                         json.dumps({"code": issued["code"], "name": "Other"}).encode())
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        other = self.http.handle("POST", "/phone/pair/status", headers(peer="other"),
                                 json.dumps({"code": issued["code"]}).encode())
        other_cookie = other.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        before = len(self.gw.transient_ids)
        denied = self.http.handle(
            "GET", "/phone/api/effect/op-trace-01",
            headers(peer="other", cookie=other_cookie, csrf=other.body["csrf_token"]))
        self.assertEqual(denied.status, 404)
        self.assertEqual(len(self.gw.transient_ids), before)

    def test_signed_envelope_binds_the_phone_and_fails_closed(self):
        public, private = _ssh_key(self.tmp.name, "ingress")
        wrong, _wrong_key = _ssh_key(self.tmp.name, "wrong")
        self.enable_relay()
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        self.ing._signer = EnvelopeSigner(private)
        prompt = "draw the curtains please"
        asked = self.post("/phone/api/ask", {"key": "op-sign-0001", "text": prompt})
        self.assertEqual(asked.status, 200, asked.body)
        ask_at = next(i for i, item in enumerate(self.gw.transient_ids) if item == ("ask", "op-sign-0001"))
        envelope = self.gw.envelopes[ask_at]
        verified = GatewayPhoneVerifier(public).verify(envelope)
        self.assertEqual(verified.phone_id, self.phone_id)
        self.assertEqual(verified.target, "zenbook")
        self.assertEqual(verified.attempt_id, self.gw.attempts[ask_at])
        self.assertEqual(verified.kind, "ask")
        claim = verify_phone_envelope(envelope, public)
        self.assertEqual(claim["phone_id"], self.phone_id)
        self.assertEqual(claim["role"], "phone")
        self.assertIs(claim["manage_main"], False)
        self.assertEqual(claim["opener"], "phone:" + self.phone_id)
        self.assertEqual(claim["kind"], "ask")
        self.assertEqual(claim["purpose"], "gateway_phone")
        self.assertEqual(claim["domain"], PHONE_DOMAIN)
        self.assertEqual(claim["target"], "zenbook")
        self.assertEqual(claim["main"], "zenbook")
        self.assertEqual(claim["attempt_id"], self.gw.attempts[ask_at])
        self.assertEqual(set(claim), set(ENVELOPE_FIELDS))
        parsed = json.loads(envelope)
        self.assertEqual(set(parsed), {"v", "claim", "signature", "body"})
        self.assertEqual(parsed["v"], 1)
        self.assertEqual(parsed["body"], {"text": prompt})
        self.assertEqual(parsed["claim"]["digest"], gateway_phone_digest(parsed["body"]))
        self.assertNotIn(prompt.encode(), self.blob())
        self.assertIsNone(verify_phone_envelope(_envelope({**parsed, "body": {"text": "changed"}}), public))
        tampered = json.loads(envelope)
        tampered["claim"]["opener"] = "phone:abcdabcd"
        self.assertIsNone(verify_phone_envelope(_envelope(tampered), public))
        flagged = json.loads(envelope)
        flagged["claim"]["manage_main"] = True
        self.assertIsNone(verify_phone_envelope(_envelope(flagged), public))
        self.assertIsNone(verify_phone_envelope(envelope, wrong))
        unsigned = dict(parsed)
        unsigned.pop("signature")
        self.assertIsNone(verify_phone_envelope(_envelope(unsigned), public))
        before = len(self.gw.envelopes)
        self.ing._signer = object()
        refused = self.post("/phone/api/ask", {"key": "op-sign-0002", "text": prompt})
        self.assertEqual(refused.status, 503)
        self.assertEqual(refused.body["code"], "envelope_unsigned")
        self.assertEqual(len(self.gw.envelopes), before)
        self.assertIsNone(self.ing._op("op-sign-0002"))

    def test_unsigned_envelope_still_relays(self):
        self.enable_relay()
        self.open_phone()
        self.ing._signer = None
        asked = self.post("/phone/api/ask", {"key": "op-unsigned1", "text": "draw the curtains"})
        self.assertEqual(asked.status, 503)
        self.assertEqual(asked.body["code"], "envelope_unsigned")
        self.assertEqual(self.gw.envelopes, [])
        self.assertIsNone(self.ing._op("op-unsigned1"))
        self.ing._signer = object()
        again = self.post("/phone/api/ask", {"key": "op-unsigned1", "text": "draw the curtains"})
        self.assertEqual(again.status, 503)
        self.assertEqual(again.body["code"], "envelope_unsigned")
        self.assertEqual(self.gw.envelopes, [])
        self.assertEqual(self.gw.transient_ids, [])

    def test_logout_releases_before_revoke_when_relay_exists(self):
        seen = []

        def watch():
            row = self.ing._db.execute("SELECT revoked FROM phones WHERE id=?", (self.phone_id,)).fetchone()
            seen.append(row["revoked"])

        self.enable_relay(watch=watch)
        self.open_phone()
        done = self.post("/phone/api/logout", {"operation_id": "logout-0001"})
        self.assertEqual(done.status, 200, done.body)
        self.assertEqual(done.body["ending"], "released")
        self.assertNotIn("release_error", done.body)
        self.assertEqual(seen, [0])
        self.assertIn(("logout_end", "logout-0001"), self.gw.transient_ids)
        row = self.ing._db.execute("SELECT revoked FROM phones WHERE id=?", (self.phone_id,)).fetchone()
        self.assertEqual(row["revoked"], 1)
        again = self.http.handle("GET", "/phone/api/me", headers(cookie=self.cookie, csrf=self.csrf))
        self.assertEqual(again.status, 401)

    def test_logout_without_relay_is_local_only(self):
        self.open_phone()
        local = self.post("/phone/api/logout", {})
        self.assertEqual(local.body["ending"], "local_only")
        self.assertFalse(any(item[0] == "transient" for item in self.gw.calls))

    def test_logout_still_revokes_when_the_release_is_not_accepted(self):
        seen = []

        def watch():
            row = self.ing._db.execute("SELECT revoked FROM phones WHERE id=?", (self.phone_id,)).fetchone()
            seen.append(row["revoked"])

        self.enable_relay(watch=watch)
        self.open_phone()
        self.gw.fail = GatewayError("no_relay")
        done = self.post("/phone/api/logout", {"operation_id": "logout-0002"})
        self.assertEqual(done.body["ending"], "release_unconfirmed")
        self.assertEqual(done.body["release_error"], "no_relay")
        self.assertEqual(seen, [0])
        row = self.ing._db.execute("SELECT revoked FROM phones WHERE id=?", (self.phone_id,)).fetchone()
        self.assertEqual(row["revoked"], 1)

    def test_csrf_stays_required_and_the_session_survives_a_main_change(self):
        body = self.open_phone()
        missing = self.http.handle("GET", "/phone/api/me", headers())
        self.assertEqual(missing.status, 401)
        self.assertTrue(missing.body["pair"])
        bare = self.http.handle("GET", "/phone/api/me", headers(cookie=self.cookie))
        self.assertEqual(bare.status, 403)
        self.assertEqual(bare.body["code"], "csrf")
        self.assertNotIn("pair", bare.body)
        self.gw.view["epoch"] = 9
        self.gw.view["main"] = "dell"
        self.gw.view["lease_holder"] = "dell"
        me = self.http.handle("GET", "/phone/api/me", headers(cookie=self.cookie, csrf=self.csrf))
        self.assertEqual(me.status, 200, me.body)
        self.assertEqual(me.body["phone"]["id"], self.phone_id)
        self.assertEqual(me.body["main"]["id"], "dell")
        self.assertNotIn(body["csrf_token"].encode(), self.blob())

    def test_network_session_does_not_carry_a_body(self):
        session = NetworkGatewaySession(
            "http://127.0.0.1:9", authority_id="oracle-auth", device_id="phone-gw",
            installation_id="phone-inst", private_key_path=str(Path(self.tmp.name) / "missing-key"))
        with self.assertRaises(IngressError) as raised:
            session.relay_transient("op-net-0001", "zenbook", "a-0001", "sha256:" + "ab" * 32,
                                   "client", "ask", b"{}")
        self.assertEqual(raised.exception.code, "transient_not_networked")
        proof = GatewayOwnerProof("tok", "owner-test", "transition", "sha256:" + "ab" * 32)
        with self.assertRaises(IngressError) as raised:
            session.transition_request(proof, {"client_key": "pv-1"})
        self.assertEqual(raised.exception.code, "transition_not_networked")
        with self.assertRaises(IngressError) as raised:
            session.transition_request({"receipt": "nope"}, {})
        self.assertEqual(raised.exception.code, "owner_proof_required")

        class Local(NetworkGatewaySession):
            def status(self):
                return {
                    "fleet_id": "home", "authority_id": "oracle-auth",
                    "mode": "V2_ACTIVE", "mode_version": 4, "incarnation": 2, "epoch": 3,
                    "policy_version": 1, "main": "zenbook", "main_ready": True, "lease_valid": True,
                    "lease_holder": "zenbook",
                }

        local = Local(
            "http://127.0.0.1:9", authority_id="oracle-auth", device_id="phone-gw",
            installation_id="phone-inst", private_key_path=str(Path(self.tmp.name) / "missing-key"))
        calls = []
        real = local.relay_transient

        def wrapped(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        local.relay_transient = wrapped
        folder = Path(self.tmp.name) / "net"
        folder.mkdir()
        ingress = MobileIngress(
            folder / "ingress.sqlite", local, origin=ORIGIN, installation_id="oracle-inst",
            allowed_mains={"zenbook": pin_target("zenbook", "https://zenbook.tailnet.ts.net", "100.64.0.10")},
            peer_verifier=peer_from, owner_verifier=owner_from,
            blocked_stable_ids=["pc-zenbook"], clock=self.clock)
        _net_public, net_private = _ssh_key(folder, "net-ingress")
        ingress._signer = EnvelopeSigner(net_private)
        self.addCleanup(ingress.close)
        http = PhoneProtocolAdapter(ingress)
        ingress.activate(4, legacy_phone_fenced=True)
        ingress.identify(headers())
        owner = ingress.authenticate_owner("owner-token")
        issued = ingress.issue_code(owner, "Pixel")
        http.handle("POST", "/phone/pair", headers(), json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        ingress.decide_pairing(owner, issued["code_id"], True)
        done = http.handle("POST", "/phone/pair/status", headers(), json.dumps({"code": issued["code"]}).encode())
        cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        csrf = done.body["csrf_token"]
        first = http.handle("POST", "/phone/api/ask", headers(cookie=cookie, csrf=csrf),
                            json.dumps({"key": "op-net-0001", "text": "draw the curtains"}).encode())
        self.assertEqual(first.status, 503, first.body)
        self.assertEqual(first.body["code"], "transient_not_networked")
        second = http.handle("POST", "/phone/api/ask", headers(cookie=cookie, csrf=csrf),
                             json.dumps({"key": "op-net-0001", "text": "draw the curtains"}).encode())
        self.assertEqual(second.status, 409)
        self.assertEqual(second.body["code"], "uncertain")
        self.assertEqual(calls, ["op-net-0001"])

    def test_restart_reconciles_the_same_authorized_preview(self):
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        principal = self.principal()
        preview = self.ing.manage_main_preview(
            principal, {"action": "recover", "candidate": "dell", "operation_id": "preview-keep-1"})
        preview_id = preview["preview_id"]
        self.gw.transition_handler = lambda proof, payload: (_ for _ in ()).throw(GatewayError("unavailable"))
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview_id,
                                        {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "unavailable")
        stored = self.ing._preview(preview_id)
        self.assertEqual(stored["authorized"], 1)
        self.assertEqual(stored["state"], "uncertain")
        self.assertIsNone(stored["transition_id"])
        self.assertEqual(self.proofs[-1]["client_key"], preview_id)
        cookie, csrf = self.cookie, self.csrf
        self.ing.close()
        revived = self.open()
        self.addCleanup(revived.close)
        revived.activate(4, legacy_phone_fenced=True)
        revived.identify(headers(cookie=cookie, csrf=csrf))
        principal = revived.authenticate(cookie, csrf, ORIGIN)
        self.assertEqual(revived._passkey_verified, set())
        self.gw.transition_handler = None
        proofs = len(self.proofs)
        again = revived.manage_main_confirm(principal, preview_id, {"assertion": "nope", "challenge": "nope"})
        self.assertEqual(again["transition_id"], "tr-1")
        self.assertFalse(again["duplicate"])
        self.assertEqual(self.proofs[-1]["client_key"], preview_id)
        self.assertEqual(len(self.proofs), proofs + 1)
        self.assertEqual(len(self.passkey_calls), 1)
        proofs = len(self.proofs)
        duplicate = revived.manage_main_confirm(principal, preview_id, {"assertion": "nope", "challenge": "nope"})
        self.assertEqual(duplicate["transition_id"], "tr-1")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.proofs), proofs)
        self.assertEqual(revived._preview(preview_id)["transition_id"], "tr-1")

    def test_expired_and_revoked_previews_do_not_mint(self):
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        principal = self.principal()
        preview = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        self.clock.t = preview["expires"] + 1
        proofs = len(self.proofs)
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, preview["preview_id"],
                                        {"assertion": "good", "challenge": preview["challenge"]})
        self.assertEqual(raised.exception.code, "expired")
        self.assertGreater(self.clock.t - (preview["expires"] - PREVIEW_TTL), PREVIEW_TTL)
        self.assertEqual(len(self.proofs), proofs)
        self.assertEqual(self.gw.transitions_called(), [])
        fresh = self.ing.manage_main_preview(principal, {"action": "recover", "candidate": "dell"})
        self.ing.revoke_phone(self.owner(), self.phone_id)
        with self.assertRaises(IngressError) as raised:
            self.ing.manage_main_confirm(principal, fresh["preview_id"],
                                        {"assertion": "good", "challenge": fresh["challenge"]})
        self.assertEqual(raised.exception.code, "unauthorized")
        self.assertEqual(len(self.proofs), proofs)
        self.assertEqual(self.gw.transitions_called(), [])

    def test_two_confirms_share_one_sending_transition(self):
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        principal = self.principal()
        preview = self.ing.manage_main_preview(
            principal, {"action": "recover", "candidate": "dell", "operation_id": "preview-race1"})
        started = threading.Event()
        release = threading.Event()

        def handler(proof, payload):
            started.set()
            if not release.wait(3):
                raise GatewayError("unavailable")
            return {"transition_id": "tr-1", "state": "REQUESTED", "action": payload["action"],
                    "candidate": payload["candidate"], "profile": payload["profile"]}

        self.gw.transition_handler = handler
        outcomes = []

        def confirm():
            try:
                outcomes.append(("ok", self.ing.manage_main_confirm(
                    principal, preview["preview_id"],
                    {"assertion": "good", "challenge": preview["challenge"]})))
            except IngressError as exc:
                outcomes.append(("err", exc.code))

        first = threading.Thread(target=confirm)
        first.start()
        self.assertTrue(started.wait(3))
        second = threading.Thread(target=confirm)
        second.start()
        second.join(3)
        self.assertIn(("err", "uncertain"), outcomes)
        self.assertEqual(len(self.proofs), 1)
        self.assertEqual(len(self.gw.transitions_called()), 1)
        self.assertEqual(self.proofs[0]["client_key"], preview["preview_id"])
        release.set()
        first.join(3)
        self.assertTrue(any(item[0] == "ok" and item[1]["transition_id"] == "tr-1" for item in outcomes))
        self.assertEqual(len(self.proofs), 1)
        self.assertEqual(len(self.gw.transitions_called()), 1)

    def test_preview_operation_id_does_not_mint_twice(self):
        self.open_phone()
        self.ing.grant_manage_main(self.owner(), self.phone_id)
        op = "preview-op-01"
        payload = {"action": "recover", "candidate": "dell", "operation_id": op}
        first = self.post("/phone/api/main/preview", payload)
        self.assertEqual(first.status, 200, first.body)
        second = self.post("/phone/api/main/preview", payload)
        self.assertEqual(second.status, 200, second.body)
        self.assertEqual(second.body["preview_id"], first.body["preview_id"])
        self.assertEqual(second.body["challenge"], first.body["challenge"])
        self.assertNotIn(first.body["challenge"].encode(), self.blob())
        clash = self.post("/phone/api/main/preview", {"action": "recover", "candidate": "zenbook", "operation_id": op})
        self.assertEqual(clash.status, 409)
        self.assertEqual(clash.body["code"], "conflict")
        legacy = self.post("/phone/api/main/preview", {"action": "recover", "candidate": "dell"})
        self.assertEqual(legacy.status, 200, legacy.body)
        self.assertNotEqual(legacy.body["preview_id"], first.body["preview_id"])
        self.assertEqual(self.gw.transitions_called(), [])
        cookie, csrf, preview_id = self.cookie, self.csrf, first.body["preview_id"]
        self.ing.close()
        revived = self.open()
        self.addCleanup(revived.close)
        revived.activate(4, legacy_phone_fenced=True)
        http = PhoneProtocolAdapter(revived)
        again = http.handle(
            "POST", "/phone/api/main/preview", headers(cookie=cookie, csrf=csrf),
            json.dumps(payload).encode())
        self.assertEqual(again.status, 200, again.body)
        self.assertEqual(again.body["preview_id"], preview_id)
        self.assertNotIn("challenge", again.body)

    def test_pair_operation_id_is_one_use_and_the_cookie_is_not_stored(self):
        self.activate()
        issued = self.ing.issue_code(self.owner(), "Pixel")
        op = "pair-op-0001"
        claimed = self.http.handle(
            "POST", "/phone/pair", headers(),
            json.dumps({"code": issued["code"], "name": "Pixel", "operation_id": op}).encode())
        self.assertEqual(claimed.status, 202, claimed.body)
        mismatch = self.http.handle(
            "POST", "/phone/pair/status", headers(),
            json.dumps({"code": issued["code"], "operation_id": "pair-op-9999"}).encode())
        self.assertEqual(mismatch.status, 409)
        self.assertEqual(mismatch.body["code"], "conflict")
        self.ing.decide_pairing(self.owner(), issued["code_id"], True)
        done = self.http.handle(
            "POST", "/phone/pair/status", headers(),
            json.dumps({"code": issued["code"], "operation_id": op}).encode())
        self.assertEqual(done.status, 200, done.body)
        cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        self.assertNotIn(cookie.encode(), self.blob())
        self.assertNotIn(done.body["csrf_token"].encode(), self.blob())
        assert_absent(self.blob(), issued["code"])
        lost = self.http.handle(
            "POST", "/phone/pair/status", headers(),
            json.dumps({"code": issued["code"], "operation_id": op}).encode())
        self.assertEqual(lost.status, 409)
        self.assertEqual(lost.body["code"], "pair_lost")
        self.assertNotIn("Set-Cookie", lost.headers)
        self.assertEqual(self.ing._db.execute("SELECT COUNT(*) AS n FROM phones").fetchone()["n"], 1)
        issued_again = self.ing.issue_code(self.owner(), "Other")
        replay = self.http.handle(
            "POST", "/phone/pair", headers(peer="other"),
            json.dumps({"code": issued_again["code"], "name": "Other", "operation_id": op}).encode())
        self.assertEqual(replay.status, 409)
        self.assertEqual(replay.body["code"], "pair_lost")
        self.assertEqual(self.ing._db.execute("SELECT COUNT(*) AS n FROM phones").fetchone()["n"], 1)
        bind = self.ing._db.execute("SELECT state, phone_id FROM pair_binds WHERE operation_id=?", (op,)).fetchone()
        self.assertEqual(bind["state"], "issued")
        self.assertEqual(bind["phone_id"], done.body["phone"]["id"])

    def test_effect_status_returns_the_pc_outcome_without_storing_the_reply(self):
        self.enable_relay()
        self.open_phone()
        prompt = "draw the curtains please"
        reply = "the curtains moved"
        self.gw.read_body = {
            "job": "abc123", "reply": reply, "target": "zenbook",
            "trace": {"rev": 2, "steps": [
                {"id": "plan-1", "label": "token=super-secret", "state": "run", "kind": "step"}]},
        }
        asked = self.post("/phone/api/ask", {"key": "op-effect01", "text": prompt, "device": "dell"})
        self.assertEqual(asked.status, 200, asked.body)
        self.assertEqual(asked.body["job"], "abc123")
        self.assertEqual(asked.body["reply"], reply)
        self.assertEqual(asked.body["target"], "zenbook")
        self.assertEqual(asked.body["trace"]["steps"][0]["id"], "plan-1")
        self.assertNotIn("super-secret", json.dumps(asked.body))
        self.assertNotIn(prompt.encode(), self.blob())
        self.assertNotIn(reply.encode(), self.blob())
        self.assertEqual(self.ing._op("op-effect01")["job_id"], "abc123")
        ask = json.loads(next(raw for raw in self.gw.envelopes if json.loads(raw)["claim"]["kind"] == "ask"))
        self.assertEqual(ask["body"], {"text": prompt, "device": "dell"})
        self.assertEqual(ask["claim"]["target"], "zenbook")
        self.assertNotIn("kind", ask["body"])
        status_env = json.loads(next(
            raw for raw in self.gw.envelopes if json.loads(raw)["claim"]["kind"] == "effect_status"))
        self.assertEqual(status_env["body"], {"operation_id": "op-effect01"})
        self.assertEqual(ask["claim"]["attempt_id"], self.gw.attempts[self.gw.transient_ids.index(("ask", "op-effect01"))])

        sid = "cd" * 16
        self.gw.read_body = {"id": sid, "state": "live", "device": "dell"}
        started = self.post("/phone/api/session/start", {
            "operation_id": "sess-real-01", "device": "dell", "kind": "screen", "quality": "low"})
        self.assertEqual(started.status, 200, started.body)
        self.assertEqual(started.body["id"], sid)
        self.assertEqual(self.ing._op("sess-real-01")["session_id"], sid)
        self.assertNotIn(prompt.encode(), self.blob())
        start_env = json.loads(next(
            raw for raw in self.gw.envelopes if json.loads(raw)["claim"]["operation_id"] == "sess-real-01"))
        self.assertEqual(start_env["body"]["device"], "dell")
        self.assertEqual(start_env["claim"]["target"], "zenbook")
        mismatch = self.post("/phone/api/session/stop", {"session": sid, "operation_id": "stop-not-the-session"})
        self.assertEqual(mismatch.status, 409)
        self.assertEqual(mismatch.body["code"], "conflict")
        self.assertNotIn(("session_stop", "stop-not-the-session"), self.gw.transient_ids)
        stopped = self.post("/phone/api/session/stop", {"session": sid})
        self.assertEqual(stopped.status, 200, stopped.body)
        self.assertIn(("session_stop", "stop-" + sid), self.gw.transient_ids)

        self.gw.read_body = {"ok": True}
        synthetic = self.post("/phone/api/session/start", {
            "operation_id": "sess-synth01", "device": "dell", "kind": "screen", "quality": "low"})
        expected = hashlib.sha256(b"session\nsess-synth01").hexdigest()[:32]
        self.assertEqual(synthetic.status, 200, synthetic.body)
        self.assertEqual(synthetic.body["id"], expected)
        self.assertEqual(self.ing._op("sess-synth01")["session_id"], expected)

    def test_production_ingress_rejects_callbacks(self):
        allowed = {
            "zenbook": pin_target("zenbook", "https://zenbook.tailnet.ts.net", "100.64.0.10"),
            "dell": pin_target("dell", "https://dell.tailnet.ts.net", "100.64.0.11"),
        }
        with self.assertRaises(IngressError) as raised:
            production_ingress(
                Path(self.tmp.name) / "nope.sqlite", self.gw, origin=ORIGIN, installation_id="oracle-inst",
                allowed_mains=allowed, peer_verifier=peer_from, blocked_stable_ids=["pc-zenbook"],
                webauthn=lambda *args: True, owner_bridge=lambda payload: None,
                envelope_signer=lambda claim: "nope", ticket_verifier=lambda signed: True,
                owner_verifier=owner_from, clock=self.clock)
        self.assertEqual(raised.exception.code, "envelope_unsigned")

        private, public = _p256(self.tmp.name)
        origin = "https://" + _RP_ID
        webauthn = WebAuthnAssertionVerifier(
            credential_id=_CRED, public_pem=public, rp_id=_RP_ID, origin=origin)
        owner_pub, owner_key = _ssh_key(self.tmp.name, "prod-owner")
        gateway = ContinuityGateway.open(
            Path(self.tmp.name) / "prod-authority.sqlite", "home", "oracle-auth", [owner_pub])
        self.addCleanup(gateway.close)
        challenges = []
        issue = gateway.issue_challenge

        def counting(digest):
            challenges.append(digest)
            return issue(digest)

        gateway.issue_challenge = counting
        bridge = OwnerAssertionBridge(gateway, owner_key)
        _sign_pub, sign_key = _ssh_key(self.tmp.name, "prod-ingress")
        signer = EnvelopeSigner(sign_key)
        tickets = TicketSignatureVerifier({"zenbook": owner_pub})
        made = production_ingress(
            Path(self.tmp.name) / "prod-ingress.sqlite", self.gw, origin=ORIGIN,
            installation_id="oracle-inst", allowed_mains=allowed, peer_verifier=peer_from,
            blocked_stable_ids=["pc-zenbook"], webauthn=webauthn, owner_bridge=bridge,
            envelope_signer=signer, ticket_verifier=tickets, owner_verifier=owner_from, clock=self.clock)
        self.addCleanup(made.close)
        called = []
        made._passkeys = lambda *args: called.append("passkey") or "receipt-ok-secret"
        made._owner_proof = lambda payload: called.append("owner") or GatewayOwnerProof(
            object(), "owner-test", "transition", "sha256:" + "ab" * 32)
        self.assertTrue(made._production)
        made.activate(4, legacy_phone_fenced=True)
        http = PhoneProtocolAdapter(made)
        made.identify(headers())
        owner = made.authenticate_owner("owner-token")
        issued = made.issue_code(owner, "Pixel")
        http.handle("POST", "/phone/pair", headers(), json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        made.decide_pairing(owner, issued["code_id"], True)
        done = http.handle("POST", "/phone/pair/status", headers(), json.dumps({"code": issued["code"]}).encode())
        cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        csrf = done.body["csrf_token"]
        phone_id = done.body["phone"]["id"]
        made.grant_manage_main(owner, phone_id)
        preview = http.handle(
            "POST", "/phone/api/main/preview", headers(cookie=cookie, csrf=csrf),
            json.dumps({"action": "recover", "candidate": "dell", "operation_id": "prod-preview1"}).encode())
        self.assertEqual(preview.status, 200, preview.body)
        bad = _assertion(private, challenge=preview.body["challenge"], origin=origin, rp_id=_RP_ID,
                         credential_id=_CRED, flags=0x01)
        refused = http.handle(
            "POST", "/phone/api/main/confirm", headers(cookie=cookie, csrf=csrf),
            json.dumps({"preview_id": preview.body["preview_id"], "passkey_proof": bad}).encode())
        self.assertEqual(refused.status, 403)
        self.assertEqual(challenges, [])
        self.assertEqual(called, [])
        self.assertEqual(self.gw.transitions_called(), [])
        saved_webauthn = made._webauthn
        made._webauthn = None
        blocked = http.handle(
            "POST", "/phone/api/main/confirm", headers(cookie=cookie, csrf=csrf),
            json.dumps({"preview_id": preview.body["preview_id"], "passkey_proof": {
                "assertion": "good", "challenge": preview.body["challenge"]}}).encode())
        self.assertEqual(blocked.status, 503)
        self.assertEqual(blocked.body["code"], "passkey_unavailable")
        self.assertEqual(called, [])
        made._webauthn = saved_webauthn
        good = _assertion(private, challenge=preview.body["challenge"], origin=origin, rp_id=_RP_ID,
                          credential_id=_CRED, counter=1)
        confirmed = http.handle(
            "POST", "/phone/api/main/confirm", headers(cookie=cookie, csrf=csrf),
            json.dumps({"preview_id": preview.body["preview_id"], "passkey_proof": good}).encode())
        self.assertEqual(confirmed.status, 200, confirmed.body)
        self.assertEqual(confirmed.body["transition_id"], "tr-1")
        self.assertEqual(len(challenges), 1)
        self.assertEqual(called, [])
        self.assertEqual(len(self.gw.transitions_called()), 1)
        self.assertTrue(self.gw.transitions_called()[0][1])


if __name__ == "__main__":
    unittest.main()
