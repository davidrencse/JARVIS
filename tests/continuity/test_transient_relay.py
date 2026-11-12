"""Gateway `forward_transient` and the ingress phone relay, on the real gateway.

The upstream answer is controlled by patching `gateway._post_json`; nothing listens
except the gateway itself and the fleet's loopback MAIN sinks.
"""

import base64
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from jarvis.continuity import gateway as gateway_module
from jarvis.continuity.gateway import GatewayError
from jarvis.continuity.integration import IsolatedFleet
from jarvis.continuity.ingress import ContinuityGatewaySession
from tests.continuity._fleet import Clock

SECRET = "SECRET-PROMPT-do-not-store"


def digest_of(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class Transient(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.fleet = IsolatedFleet(self.tmp.name, self.clock)
        self.addCleanup(self.fleet.close)
        self.fleet.cutover()
        _, lease = self.fleet.promote("zenbook")
        self.fleet.nodes["zenbook"].runtime.start()
        self.fleet.nodes["zenbook"].runtime.accept_lease(lease)
        self.session = ContinuityGatewaySession(
            self.fleet.gateway, "ingress", "ingress-inst", self.fleet._keys["ingress"][1],
            wall=self.clock.wall)
        self.zen = self.fleet.nodes["zenbook"]

    def send(self, op, kind="ask", text=SECRET, target="zenbook", attempt="a-0001", digest=None, raw=None):
        raw = raw if raw is not None else json.dumps({"text": text}).encode()
        return self.session.relay_transient(op, target, attempt, digest or digest_of(raw), "client", kind, raw)

    def patched(self, reply):
        calls = []

        def fake(url, data, limit):
            calls.append((url, data, limit))
            if isinstance(reply, Exception):
                raise reply
            return dict(reply)

        return mock.patch.object(gateway_module, "_post_json", fake), calls

    def test_effect_reaches_the_real_main_once_and_claims_no_authority(self):
        first = self.send("op-real-0001")
        self.assertEqual(first, {"operation_id": "op-real-0001", "relayed": True,
                                 "effect_authorized": False, "upstream_accepted": True})
        self.assertEqual(self.send("op-real-0001"), first)
        self.assertEqual(self.zen.effects, ["op-real-0001"])

    def test_repeat_is_stored_and_not_posted_again(self):
        ctx, calls = self.patched({"operation_id": "op-store-001", "accepted": True})
        with ctx:
            self.send("op-store-001")
            self.send("op-store-001")
        self.assertEqual(len(calls), 1)

    def test_changed_body_target_or_attempt_conflicts(self):
        ctx, calls = self.patched({"operation_id": "op-conf-0001", "accepted": True})
        with ctx:
            self.send("op-conf-0001")
            with self.assertRaises(GatewayError) as caught:
                self.send("op-conf-0001", text="different")
            self.assertEqual(caught.exception.code, "conflict")
            with self.assertRaises(GatewayError) as caught:
                self.send("op-conf-0001", attempt="a-0002")
            self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(len(calls), 1)

    def test_validation_happens_before_any_row_or_post(self):
        ctx, calls = self.patched({"operation_id": "op-valid-001", "accepted": True})
        with ctx:
            raw = b'{"text":"x"}'
            for bad in (
                lambda: self.send("op-valid-001", digest="sha256:" + "0" * 64),
                lambda: self.send("op-valid-001", kind="shell"),
                lambda: self.send("op-valid-001", raw=b""),
                lambda: self.send("op-valid-001", raw=b"x" * (256 * 1024 + 1)),
                lambda: self.session.relay_transient("op-valid-001", "zenbook", "a-0001", digest_of(raw),
                                                    "authority", "ask", raw),
            ):
                with self.assertRaises(GatewayError) as caught:
                    bad()
                self.assertEqual(caught.exception.code, "bad_request")
            self.assertEqual(self.fleet.gateway._effects, {})
        self.assertEqual(calls, [])

    def test_target_must_be_the_ready_main(self):
        ctx, calls = self.patched({"operation_id": "op-tgt-00001", "accepted": True})
        with ctx:
            with self.assertRaises(GatewayError) as caught:
                self.send("op-tgt-00001", target="dell")
            self.assertEqual(caught.exception.code, "no_relay")
        self.assertEqual(calls, [])

    def test_overclaim_lost_and_odd_answers_leave_the_effect_unresolved_and_unsent(self):
        replies = (
            {"operation_id": "op-odd-00001", "accepted": True, "effect_authorized": True},
            {"operation_id": "op-odd-00002", "accepted": "yes"},
            {"operation_id": "other-op-001", "accepted": True},
            GatewayError("unavailable"),
        )
        for index, reply in enumerate(replies, 1):
            op = "op-odd-%05d" % index
            ctx, calls = self.patched(reply)
            with ctx:
                with self.assertRaises(GatewayError) as caught:
                    self.send(op)
                self.assertEqual(caught.exception.code, "no_relay")
                with self.assertRaises(GatewayError) as again:
                    self.send(op)
                self.assertEqual(again.exception.code, "no_relay")
            self.assertEqual(len(calls), 1, reply)

    def test_read_is_not_remembered_and_may_carry_raw_bytes(self):
        reply = {"operation_id": "op-read-0001", "result": {"status": 200, "body": {"ok": True}},
                 "raw_b64": base64.b64encode(b"\x00frame\xff").decode()}
        ctx, calls = self.patched(reply)
        with ctx:
            got = self.send("op-read-0001", kind="session_frame", text="x")
            self.assertEqual(got["raw"], b"\x00frame\xff")
            self.assertFalse(got["effect_authorized"])
            self.send("op-read-0001", kind="session_frame", text="x")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.fleet.gateway._effects, {})

    def test_read_with_extra_keys_or_bad_base64_is_refused(self):
        for reply in ({"operation_id": "op-rd-000001", "result": {}, "extra": 1},
                      {"operation_id": "op-rd-000001", "result": {}, "raw_b64": "!!!"},
                      {"operation_id": "op-rd-000001", "result": "nope"}):
            ctx, _ = self.patched(reply)
            with ctx:
                with self.assertRaises(GatewayError) as caught:
                    self.send("op-rd-000001", kind="overview", text="x")
                self.assertEqual(caught.exception.code, "no_relay")

    def test_text_never_reaches_audit_store_or_files(self):
        self.send("op-priv-0001")
        blob = self.fleet.persisted_bytes()
        self.assertNotIn(SECRET.encode(), blob)
        self.assertNotIn(base64.b64encode(SECRET.encode()), blob)
        for row in self.fleet.gateway._effects.values():
            self.assertNotIn(SECRET, json.dumps(row, default=str))


if __name__ == "__main__":
    unittest.main()
