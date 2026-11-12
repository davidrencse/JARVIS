"""One copy of the ticket fields, one signature cap, one meaning per refusal code.

These pin the consolidation in CONTINUITY-CONSOLIDATION.md: each boundary keeps its own
validation and error type, but the shared tuples and status codes cannot drift apart again.
"""

import inspect
import unittest
from tempfile import TemporaryDirectory

from jarvis.continuity import authority, executor, gateway, ingress, integration, runtime
from jarvis.continuity.authority import MAX_SIGNATURE, TICKET_FIELDS, AuthorityError, AuthorityStore
from jarvis.continuity.gateway import GatewayError, _signature
from jarvis.continuity.integration import IsolatedFleet
from jarvis.continuity.runtime import GatewayPort, RuntimeRefusal

from tests.continuity._fleet import Clock
from tests.continuity.test_executor import Base as ExecutorBase

ARMOR = "-----BEGIN SSH SIGNATURE-----\n%s\n-----END SSH SIGNATURE-----"


def armored(length):
    """A well-formed armor exactly `length` characters long."""
    pad = length - len(ARMOR % "")
    return ARMOR % ("A" * pad)


class TicketFields(unittest.TestCase):
    def test_every_boundary_shares_the_authority_tuple(self):
        self.assertIs(gateway._TICKET_FIELDS, TICKET_FIELDS)
        self.assertIs(runtime._TICKET_FIELDS, TICKET_FIELDS)
        self.assertIs(executor._TICKET_FIELDS, TICKET_FIELDS)
        self.assertIs(integration._CONSUME, TICKET_FIELDS)

    def test_the_tuple_is_what_the_store_consumes(self):
        params = list(inspect.signature(AuthorityStore.consume).parameters)[1:]
        self.assertEqual(tuple(params), TICKET_FIELDS)
        self.assertEqual(len(set(TICKET_FIELDS)), len(TICKET_FIELDS))

    def test_the_signed_tuple_is_unchanged(self):
        # Order is part of what MAIN signs; a reorder would invalidate every ticket.
        self.assertEqual(TICKET_FIELDS, (
            "admission_id", "target", "operation_id", "attempt_id", "digest", "main",
            "incarnation", "epoch", "issued_at", "admit_until", "issuer_incarnation"))


class RefusalStatus(unittest.TestCase):
    def test_gateway_codes_keep_their_status_at_every_boundary(self):
        for name, table in (("ingress gateway", ingress._GATEWAY_HTTP), ("ingress auth", ingress._AUTH_STATUS),
                            ("integration contract", integration.CONTRACT_HTTP)):
            for code, status in table.items():
                if code == "authority_unavailable":
                    continue  # the ingress's own 503 for a gateway it cannot trust
                self.assertEqual(gateway.HTTP_STATUS[code], status, "%s: %s" % (name, code))
        shared = set(integration.EXTRA_HTTP) & set(gateway.HTTP_STATUS)
        for code in shared:
            self.assertEqual(gateway.HTTP_STATUS[code], integration.EXTRA_HTTP[code], code)

    def test_values_the_phone_and_hub_relied_on_are_unchanged(self):
        self.assertEqual(ingress._GATEWAY_HTTP["not_found"], 404)
        self.assertEqual(ingress._GATEWAY_HTTP["no_relay"], 503)
        self.assertEqual(ingress._GATEWAY_HTTP["forbidden"], 403)
        self.assertEqual(ingress._AUTH_STATUS["quarantined"], 503)
        self.assertEqual(integration.EXTRA_HTTP["unknown_participant"], 403)
        self.assertEqual(integration.EXTRA_HTTP["not_found"], 404)
        self.assertEqual(integration.CONTRACT_HTTP["ticket_expired"], 410)

    def test_integration_only_codes_stay_fixed(self):
        only = {k: v for k, v in integration.EXTRA_HTTP.items() if k not in gateway.HTTP_STATUS}
        self.assertEqual(only, {
            "authority_unavailable": 503, "personal_field": 400, "unauthenticated": 401,
            "bad_passkey": 401, "not_manage_main": 403, "not_owner": 403, "not_candidate": 403,
            "signature_unverified": 403, "admission_stopped": 409, "ai_not_ready": 409,
            "v2_inactive": 409, "legacy_unwired": 409, "refuses_live_state": 403})

    def test_ingress_turns_a_gateway_refusal_into_the_gateway_status(self):
        class Bare(ingress.MobileIngress):
            def __init__(self):  # only the pure status mapping is under test
                pass

            def _note(self, code):
                pass

        bare = Bare()
        for code in ("not_found", "forbidden", "no_relay", "conflict", "ticket_expired", "writer_busy"):
            with self.subTest(code=code):
                err = bare._from_gateway(GatewayError(code))
                self.assertEqual(err.status, gateway.HTTP_STATUS[code])
        unknown = bare._from_gateway(GatewayError("never_heard_of_it"))
        self.assertEqual((unknown.code, unknown.status), ("authority_unavailable", 503))


class SignatureCap(unittest.TestCase):
    def test_the_cap_has_one_owner(self):
        self.assertEqual(MAX_SIGNATURE, 1800)
        self.assertIs(gateway.MAX_SIGNATURE, MAX_SIGNATURE)
        self.assertIs(runtime.MAX_SIGNATURE, MAX_SIGNATURE)
        self.assertIs(executor.MAX_SIGNATURE, MAX_SIGNATURE)

    def test_gateway_edge(self):
        self.assertEqual(_signature(armored(MAX_SIGNATURE)), armored(MAX_SIGNATURE))
        for bad in (armored(MAX_SIGNATURE + 1), armored(MAX_SIGNATURE).replace("A", "é", 1), "x"):
            with self.assertRaises(GatewayError) as caught:
                _signature(bad)
            self.assertEqual(caught.exception.code, "bad_request")

    def test_runtime_port_refuses_what_the_gateway_would_before_any_io(self):
        """Before the fix the port passed up to 4096 characters and the gateway refused them late."""
        with TemporaryDirectory() as tmp:
            fleet = IsolatedFleet(tmp, Clock())
            try:
                fleet.cutover()
                client = fleet.nodes["zenbook"].client
                port = GatewayPort(client)
                fields = {key: "x" for key in TICKET_FIELDS}
                before = len(client.calls)
                for bad in (armored(MAX_SIGNATURE + 1), armored(4000), armored(MAX_SIGNATURE).replace("A", "é", 1)):
                    with self.assertRaises(RuntimeRefusal) as caught:
                        port.consume(ticket_signature=bad, **fields)
                    self.assertEqual(caught.exception.code, "bad_request")
                self.assertEqual(len(client.calls), before, "a refused signature reached the wire")
                # At the cap it is forwarded; the gateway then refuses these placeholder fields for
                # its own reasons, which proves the adapter let it through.
                with self.assertRaises((AuthorityError, RuntimeRefusal)):
                    port.consume(ticket_signature=armored(MAX_SIGNATURE), **fields)
                self.assertGreater(len(client.calls), before)
            finally:
                fleet.close()


class ExecutorCap(ExecutorBase):
    def test_signature_at_cap_passes_the_shape_check_and_one_over_is_refused_without_effect(self):
        guard = self.guard()
        calls, sink = self.ran()
        self.activate()
        self.fence_working(guard)
        over, admit = self.work(op="op-over", signature="s" * (MAX_SIGNATURE + 1))
        self.refuse("bad_request", guard.accept, over, admit, sink)
        empty, admit = self.work(op="op-empty", signature="")
        self.refuse("bad_request", guard.accept, empty, admit, sink)
        self.assertEqual((calls, self.spy.consumes), ([], 0))

        # At the cap the shape check passes; the fixture verifier then refuses the unknown
        # signature, so the refusal is not the cap's "bad_request" and still no effect or consume.
        edge, admit = self.work(op="op-edge", signature="s" * MAX_SIGNATURE)
        with self.assertRaises(AuthorityError) as caught:
            guard.accept(edge, admit, sink)
        self.assertNotEqual(caught.exception.code, "bad_request")
        self.assertEqual((calls, self.spy.consumes), ([], 0))


if __name__ == "__main__":
    unittest.main()
