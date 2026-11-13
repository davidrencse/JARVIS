"""session_frame relay reads must outlast the hub's unchanged-frame hold.

Run: .venv/bin/python -m unittest tests.continuity.test_gateway_frame_timeout
"""

import hashlib
import json
import threading
import unittest
from unittest import mock

from jarvis.continuity.gateway import (
    READ_RELAY_TIMEOUT, ContinuityGateway, DevicePrincipal, GatewayError, _Record,
    _post_json, payload_digest,
)
from jarvis.remote_view import FRAME_HOLD


class _Gateway:
    """Just enough of ContinuityGateway for forward_transient's relay post."""

    def __init__(self, origin):
        self._token = object()
        self._lock = threading.Lock()
        self._effects = {}
        self._listening_origin = "http://127.0.0.1:1"
        self._devices = {
            "zenbook": _Record(
                "zenbook", "zenbook-inst", "pc", "ssh-ed25519 AAAA",
                frozenset({"client"}), origin,
            ),
        }

    def _relay_status(self):
        return {"main": "zenbook", "authority_id": "oracle-auth", "incarnation": 1, "epoch": 0}

    _accept_device = ContinuityGateway._accept_device
    _transient_result = staticmethod(ContinuityGateway._transient_result)


def _principal(token, payload):
    return DevicePrincipal(
        token, "phone", "phone-inst", "client", frozenset({"client"}), "relay",
        payload_digest(payload),
    )


class FrameRelayTimeout(unittest.TestCase):
    def test_session_frame_read_outlasts_the_frame_hold(self):
        self.assertGreater(READ_RELAY_TIMEOUT, FRAME_HOLD)
        self.assertGreaterEqual(READ_RELAY_TIMEOUT, FRAME_HOLD + 3)
        seen = {}

        def fake(url, data, limit, timeout=2):
            seen["timeout"] = timeout
            seen["kind"] = json.loads(data)["kind"]
            return {"operation_id": "opframe01", "result": {"status": 204}}

        gateway = _Gateway("http://127.0.0.1:9")
        envelope = b'{"session":"abc"}'
        payload = {
            "operation_id": "opframe01", "target": "zenbook", "attempt_id": "attempt01",
            "digest": "sha256:" + hashlib.sha256(envelope).hexdigest(),
            "capability": "client", "kind": "session_frame",
        }
        with mock.patch("jarvis.continuity.gateway._post_json", fake):
            result = ContinuityGateway.forward_transient(
                gateway, _principal(gateway._token, payload), payload, envelope)
        self.assertEqual(seen["kind"], "session_frame")
        self.assertGreater(seen["timeout"], FRAME_HOLD)
        self.assertEqual(seen["timeout"], READ_RELAY_TIMEOUT)
        self.assertTrue(result["relayed"])
        self.assertFalse(result["effect_authorized"])

    def test_other_reads_keep_the_short_relay_timeout(self):
        seen = {}

        def fake(url, data, limit, timeout=2):
            seen["timeout"] = timeout
            return {"operation_id": "opread001", "result": {"status": 200}}

        gateway = _Gateway("http://127.0.0.1:9")
        envelope = b'{"n":1}'
        payload = {
            "operation_id": "opread001", "target": "zenbook", "attempt_id": "attempt01",
            "digest": "sha256:" + hashlib.sha256(envelope).hexdigest(),
            "capability": "client", "kind": "overview",
        }
        with mock.patch("jarvis.continuity.gateway._post_json", fake):
            ContinuityGateway.forward_transient(
                gateway, _principal(gateway._token, payload), payload, envelope)
        self.assertEqual(seen["timeout"], 2)
        self.assertLessEqual(seen["timeout"], FRAME_HOLD)

    def test_post_json_passes_timeout_to_the_opener(self):
        seen = {}

        class Response:
            status = 200

            def getcode(self):
                return 200

            def read(self, n):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Opener:
            def open(self, request, timeout=None):
                seen["timeout"] = timeout
                seen["method"] = request.get_method()
                return Response()

        with mock.patch("jarvis.continuity.gateway._opener", return_value=Opener()):
            parsed = _post_json(
                "http://127.0.0.1:9/v1/continuity/relay", b"{}", 100, timeout=READ_RELAY_TIMEOUT)
        self.assertEqual(parsed, {"ok": True})
        self.assertEqual(seen["timeout"], READ_RELAY_TIMEOUT)
        self.assertGreater(seen["timeout"], FRAME_HOLD)
        self.assertEqual(seen["method"], "POST")

        seen.clear()
        with mock.patch("jarvis.continuity.gateway._opener", return_value=Opener()):
            _post_json("http://127.0.0.1:9/v1/continuity/relay", b"{}", 100)
        self.assertEqual(seen["timeout"], 2)

    def test_post_json_timeout_does_not_hide_relay_failures(self):
        class Opener:
            def open(self, request, timeout=None):
                raise TimeoutError("timed out")

        with mock.patch("jarvis.continuity.gateway._opener", return_value=Opener()):
            with self.assertRaises(GatewayError) as raised:
                _post_json("http://127.0.0.1:9/v1/continuity/relay", b"{}", 100, timeout=READ_RELAY_TIMEOUT)
        self.assertEqual(raised.exception.code, "no_relay")


if __name__ == "__main__":
    unittest.main()
