"""PC gateway phone receiver. Real SSH signatures, no listener, no hardware.

Run: env $(env | grep -o '^CODEX_[A-Z_]*' | sed 's/^/-u /') JARVIS_NO_MODELS=1 \
  .venv/bin/python -m unittest tests.continuity.test_gateway_phone_receiver
"""

import base64
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity.authority import TICKET_FIELDS
from jarvis.continuity.executor import ExecutorGuard
from jarvis.continuity.gateway import DEVICE_NS, TICKET_NS, _message, sign_message
from jarvis.continuity.ingress import TicketSignatureVerifier
from jarvis.phone import (
    GATEWAY_PHONE_FIELDS,
    GATEWAY_PHONE_NS,
    GATEWAY_PHONE_PURPOSE,
    GatewayPhoneClaim,
    GatewayPhoneReceiver,
    GatewayPhoneVerifier,
    GatewayTicketVerifier,
    PhoneGatewayError,
    PhoneServer,
    Tailnet,
    accept_gateway_phone,
    gateway_phone_digest,
    gateway_phone_message,
)
from tests.continuity._fleet import Clock, cutover, open_store, promote


FIELDS = {
    "domain", "purpose", "v", "phone_id", "role", "manage_main", "opener",
    "operation_id", "attempt_id", "kind", "digest", "target", "main",
    "authority_id", "fleet_id", "incarnation", "epoch", "mode_version",
    "policy_version", "issuer_incarnation",
}
SESSION = "ab" * 16


class Spy:
    def __init__(self, store):
        self.store = store
        self.consumes = 0
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return self.store.status()

    def acknowledge(self, *args, **kwargs):
        return self.store.acknowledge(*args, **kwargs)

    def admission(self, admission_id):
        return self.store.admission(admission_id)

    def consume(self, **kwargs):
        self.consumes += 1
        return self.store.consume(**kwargs)


class Flip:
    """Real store status, then a newer epoch on the second read."""

    def __init__(self, store):
        self.store = store
        self.n = 0

    def status(self):
        self.n += 1
        status = dict(self.store.status())
        if self.n >= 2:
            status["epoch"] = status["epoch"] + 1
        return status


class Console:
    def __init__(self):
        self.server = None
        self.calls = []
        self.jobs = {}

    def submit(self, text, origin_device=None, approval_device=None):
        self.calls.append((text, origin_device, approval_device))
        job = {"id": "abc123", "state": "done", "result": {"reply": "noted"},
               "started": 1.0, "finished": 2.0}
        self.jobs[job["id"]] = job
        if self.server is not None:
            self.server.trace.feed("turn.step", {
                "job": job["id"], "channel": "console", "id": "plan:look",
                "label": "Look", "state": "done", "parent": "root",
            })
        return job

    def job(self, job_id):
        if job_id not in self.jobs:
            raise LookupError(job_id)
        return self.jobs[job_id]


class Hub:
    def __init__(self):
        self.main = "zenbook"
        self.opens = []
        self.inputs = []
        self.closes = []
        self.frames = []
        self.sessions_for = []
        self.ended = []

    def target_caps(self, device):
        return {"remote": (True, ""), "camera": (True, "")}

    def sessions(self, opener):
        self.sessions_for.append(opener)
        return []

    def remote_open(self, device, quality, kind, opener, allow_main):
        self.opens.append((device, quality, kind, opener, allow_main))
        return {"id": SESSION, "device": device, "state": "live", "kind": kind,
                "opener": opener, "width": 8, "height": 8, "frames": 0,
                "quality": quality, "started": 1}

    def session_input(self, sid, events, opener):
        self.inputs.append((sid, events, opener))
        return 1

    def session_frame(self, sid, after, opener):
        self.frames.append((sid, after, opener))
        return (3, b"\xff\xd8\xff", 8, 8)

    def remote_close(self, sid, opener):
        self.closes.append((sid, opener))
        return {"id": sid, "state": "ended"}

    def end_opener(self, opener):
        self.ended.append(opener)

    def session_node_ago(self, sid, opener):
        return 1.0


class Count:
    def __init__(self, inner):
        self._inner = inner
        self.accepts = 0
        self.stops = 0

    def accept(self, *args, **kwargs):
        self.accepts += 1
        return self._inner.accept(*args, **kwargs)

    def stop_interactive(self, *args, **kwargs):
        self.stops += 1
        return self._inner.stop_interactive(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _boom(*_args, **_kwargs):
    raise AssertionError("gateway phone used a cookie or a forwarded address")


class GatewayPhoneReceiverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_dir = tempfile.TemporaryDirectory()
        cls.keys = {}
        for name in ("ingress", "main", "stranger"):
            path = Path(cls.keys_dir.name) / name
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-test", "-f", str(path)],
                check=True, capture_output=True, timeout=10,
            )
            token = path.with_suffix(".pub").read_text(encoding="ascii").split()
            cls.keys[name] = (f"{token[0]} {token[1]}", path)

    @classmethod
    def tearDownClass(cls):
        cls.keys_dir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.console = Console()
        self.hub = Hub()
        state = Path(self.tmp.name) / "state"
        config = {
            "cluster": {"main_device": "zenbook", "enabled": False},
            "devices": {
                "zenbook": {"label": "Zenbook", "hostname": "not-this-host"},
                "dell": {"label": "Dell", "hostname": "not-this-host"},
            },
        }
        self.server = PhoneServer(
            config, state, console=self.console, hub=self.hub, port=0,
            tailnet=Tailnet(run=lambda args, timeout=5: (1, "")),
        )
        self.console.server = self.server
        self.server.identify = _boom
        self.server.phone_for = _boom
        self.server.store.revoke = _boom

    def pack(self, fields, body, private=None):
        fields = dict(fields)
        fields["digest"] = gateway_phone_digest(body)
        private = self.keys["ingress"][1] if private is None else private
        signature = sign_message(DEVICE_NS, private, gateway_phone_message(fields))
        raw = json.dumps(
            {"v": 1, "claim": fields, "signature": signature, "body": body},
            sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        return raw, fields

    def relay(self, raw, fields):
        return {
            "operation_id": fields["operation_id"],
            "target": fields["target"],
            "attempt_id": fields["attempt_id"],
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "capability": "client",
            "kind": fields["kind"],
            "authority_id": fields["authority_id"],
            "incarnation": fields["incarnation"],
            "epoch": fields["epoch"],
            "main": fields["main"],
            "envelope_b64": base64.b64encode(raw).decode("ascii"),
        }

    def fixed(self, kind, body, operation_id="opfixed01", **over):
        fields = {
            "attempt_id": "attempt01", "authority_id": "oracle-auth", "digest": "sha256:" + "ab" * 32,
            "domain": GATEWAY_PHONE_NS, "epoch": 2, "fleet_id": "home", "incarnation": 1,
            "issuer_incarnation": 1, "kind": kind, "main": "zenbook", "manage_main": False,
            "mode_version": 4, "opener": "phone:pixel", "operation_id": operation_id,
            "phone_id": "pixel", "policy_version": 1, "purpose": GATEWAY_PHONE_PURPOSE,
            "role": "phone", "target": "zenbook", "v": 1,
        }
        fields.update(over)
        return self.pack(fields, body)

    def open_fleet(self):
        self.clock = Clock()
        self.store = open_store(Path(self.tmp.name) / "authority.sqlite", self.clock)
        self.addCleanup(self.store.close)
        self.spy = Spy(self.store)
        cutover(self.store)
        promote(self.store, "recover", "zenbook", "k-boot")
        self.tickets = GatewayTicketVerifier({"zenbook": self.keys["main"][0]})
        path = Path(self.tmp.name) / "exec.sqlite"
        self.guard_path = path
        inner = ExecutorGuard(
            path, device_id="zenbook", installation_id="zenbook-inst", fleet_id="home",
            authority_id="oracle-auth", authority=self.spy, verifier=self.tickets,
            boot=lambda: "boot-a", wall=self.clock.wall,
        )
        self.addCleanup(inner.close)
        status = self.store.status()
        inner.fence(status["incarnation"], status["epoch"] - 1, status["mode_version"])
        self.inner = inner
        self.guard = Count(inner)
        self.status = self.store.status()
        self.receiver = GatewayPhoneReceiver(
            self.server, self.keys["ingress"][0], device_id="zenbook", guard=self.guard,
        )

    def live_fields(self, kind, body, operation_id, attempt="attempt01"):
        status = self.store.status()
        fields = {
            "attempt_id": attempt, "authority_id": "oracle-auth",
            "digest": gateway_phone_digest(body), "domain": GATEWAY_PHONE_NS,
            "epoch": status["epoch"], "fleet_id": "home", "incarnation": status["incarnation"],
            "issuer_incarnation": status["incarnation"], "kind": kind, "main": status["main"],
            "manage_main": False, "mode_version": status["mode_version"], "opener": "phone:pixel",
            "operation_id": operation_id, "phone_id": "pixel", "policy_version": status["policy_version"],
            "purpose": GATEWAY_PHONE_PURPOSE, "role": "phone", "target": "zenbook", "v": 1,
        }
        return self.pack(fields, body)

    def admission(self, fields, effect, session_id=None):
        status = self.store.status()
        ticket = {
            "admission_id": "adm-" + fields["operation_id"], "target": "zenbook",
            "operation_id": fields["operation_id"], "attempt_id": fields["attempt_id"],
            "digest": fields["digest"], "main": fields["main"], "incarnation": fields["incarnation"],
            "epoch": fields["epoch"], "issued_at": self.clock.w - 1, "admit_until": self.clock.w + 3600,
            "issuer_incarnation": fields["issuer_incarnation"],
        }
        signature = sign_message(TICKET_NS, self.keys["main"][1], _message(TICKET_NS, ticket))
        signed = dict(ticket)
        signed.update({
            "authority_id": "oracle-auth", "fleet_id": "home", "mode_version": status["mode_version"],
            "policy_version": status["policy_version"], "effect_class": effect, "signature": signature,
        })
        if session_id is not None:
            signed["session_id"] = session_id
        admit = {key: ticket[key] for key in TICKET_FIELDS}
        return signed, admit

    def receipts(self):
        if not self.guard_path.exists():
            return 0
        con = sqlite3.connect(self.guard_path)
        try:
            return con.execute("select count(*) from receipts").fetchone()[0]
        except sqlite3.Error:
            return 0
        finally:
            con.close()

    def refuse(self, fn, *args, **kwargs):
        with self.assertRaises(PhoneGatewayError) as caught:
            fn(*args, **kwargs)
        return caught.exception

    def test_claim_is_minted_only_by_the_verifier(self):
        self.assertEqual(set(GATEWAY_PHONE_FIELDS), FIELDS)
        body = {}
        raw, fields = self.fixed("overview", body)
        message = gateway_phone_message(fields)
        self.assertTrue(message.startswith(GATEWAY_PHONE_NS.encode("ascii") + b"\n"))
        self.assertIn(b"jarvis-continuity-device-v1\n", message)
        self.assertRaises(PhoneGatewayError, GatewayPhoneClaim, object())

        class Child(GatewayPhoneClaim):
            pass

        self.assertRaises(PhoneGatewayError, Child, object())
        claim = GatewayPhoneVerifier(self.keys["ingress"][0]).verify(raw)
        self.assertIs(type(claim), GatewayPhoneClaim)
        self.assertEqual(claim.role, "phone")
        self.assertIs(claim.manage_main, False)
        self.assertEqual(claim.opener, "phone:pixel")
        object.__setattr__(claim, "_phone_id", "other")
        refused = self.refuse(
            accept_gateway_phone, claim, "overview", fields["operation_id"], body,
            guard=None, receiver=self.server,
        )
        self.assertEqual(refused.code, "forbidden")
        refused = self.refuse(
            accept_gateway_phone, {"role": "phone", "phone_id": "pixel"}, "overview",
            "opfixed01", body, guard=None, receiver=self.server,
        )
        self.assertEqual(refused.code, "forbidden")

    def test_signature_tampers_and_identity_headers_are_refused(self):
        body = {"text": "open the notes"}
        raw, fields = self.fixed("ask", body, operation_id="opasktamp")
        verifier = GatewayPhoneVerifier(self.keys["ingress"][0])
        changed = json.loads(raw.decode("ascii"))
        changed["body"] = {"text": "open the notes now"}
        retampered = json.dumps(changed, sort_keys=True, separators=(",", ":")).encode("ascii")
        refused = self.refuse(verifier.verify, retampered)
        self.assertEqual(refused.code, "bad_request")
        for key, value, code in (
            ("phone_id", "other", "unauthorized"),
            ("operation_id", "opchanged", "unauthorized"),
            ("epoch", fields["epoch"] + 1, "unauthorized"),
        ):
            edited = json.loads(raw.decode("ascii"))
            edited["claim"][key] = value
            blob = json.dumps(edited, sort_keys=True, separators=(",", ":")).encode("ascii")
            self.assertEqual(self.refuse(verifier.verify, blob).code, code)
        opener = dict(fields)
        opener["opener"] = "phone:other"
        self.assertEqual(self.refuse(verifier.verify, self.pack(opener, body)[0]).code, "forbidden")
        role = dict(fields)
        role["role"] = "owner"
        self.assertEqual(self.refuse(verifier.verify, self.pack(role, body)[0]).code, "forbidden")
        managed = dict(fields)
        managed["manage_main"] = True
        self.assertEqual(self.refuse(verifier.verify, self.pack(managed, body)[0]).code, "forbidden")
        claim = verifier.verify(raw)
        refused = self.refuse(
            accept_gateway_phone, claim, "ask", "opother01", body, guard=None, receiver=self.server,
        )
        self.assertEqual(refused.code, "conflict")
        cookie = {"v": 1, "claim": fields, "signature": "nope", "body": {"text": "hi", "cookie": "a=b"}}
        blob = json.dumps(cookie, sort_keys=True, separators=(",", ":")).encode("ascii")
        self.assertEqual(self.refuse(verifier.verify, blob).code, "forbidden")
        forwarded = {"text": "hi", "meta": {"X-Forwarded-For": "203.0.113.5"}}
        nested = {"v": 1, "claim": fields, "signature": "nope", "body": forwarded}
        blob = json.dumps(nested, sort_keys=True, separators=(",", ":")).encode("ascii")
        self.assertEqual(self.refuse(verifier.verify, blob).code, "forbidden")

    def test_wrong_key_and_relay_digest(self):
        body = {"text": "open the notes"}
        fields = {
            "attempt_id": "attempt01", "authority_id": "oracle-auth", "digest": gateway_phone_digest(body),
            "domain": GATEWAY_PHONE_NS, "epoch": 2, "fleet_id": "home", "incarnation": 1,
            "issuer_incarnation": 1, "kind": "ask", "main": "zenbook", "manage_main": False,
            "mode_version": 4, "opener": "phone:pixel", "operation_id": "opwrong01",
            "phone_id": "pixel", "policy_version": 1, "purpose": GATEWAY_PHONE_PURPOSE,
            "role": "phone", "target": "zenbook", "v": 1,
        }
        raw, fields = self.pack(fields, body, private=self.keys["stranger"][1])
        self.assertEqual(
            self.refuse(GatewayPhoneVerifier(self.keys["ingress"][0]).verify, raw).code,
            "unauthorized",
        )
        raw, fields = self.pack(fields, body)
        posted = self.relay(raw, fields)
        self.assertNotEqual(posted["digest"], fields["digest"])
        posted["digest"] = "sha256:" + "cd" * 32
        receiver = GatewayPhoneReceiver(
            self.server, self.keys["ingress"][0], device_id="zenbook", guard=None,
        )
        self.assertEqual(self.refuse(receiver.receive, posted).code, "bad_request")
        posted = self.relay(raw, fields)
        posted["cookie"] = "a=b"
        self.assertEqual(self.refuse(receiver.receive, posted).code, "bad_request")
        self.assertEqual(self.console.calls, [])

    def test_failed_admission_and_true_callback_do_nothing(self):
        self.open_fleet()
        body = {"text": "open the notes", "device": "dell"}
        raw, fields = self.live_fields("ask", body, "opfail001")
        signed, ticket = self.admission(fields, "ordinary")
        signed["signature"] = sign_message(
            TICKET_NS, self.keys["stranger"][1], _message(TICKET_NS, ticket),
        )
        before = self.receipts()
        refused = self.refuse(
            self.receiver.receive, self.relay(raw, fields),
            signed_operation=signed, admission_ticket=ticket,
        )
        self.assertEqual(refused.code, "signature")
        self.assertEqual(self.spy.consumes, 0)
        self.assertEqual(self.console.calls, [])
        self.assertEqual(self.receipts(), before)
        stale_fields = dict(fields)
        stale_fields["epoch"] = fields["epoch"] + 9
        stale_fields["operation_id"] = "opstale01"
        stale_raw, stale_fields = self.pack(stale_fields, body)
        stale_signed, stale_ticket = self.admission(stale_fields, "ordinary")
        refused = self.refuse(
            self.receiver.receive, self.relay(stale_raw, stale_fields),
            signed_operation=stale_signed, admission_ticket=stale_ticket,
        )
        self.assertEqual(refused.code, "stale")
        self.assertEqual(self.spy.consumes, 0)
        self.assertEqual(self.console.calls, [])
        self.assertEqual(self.receipts(), before)
        path = Path(self.tmp.name) / "exec-lambda.sqlite"
        opened = ExecutorGuard(
            path, device_id="zenbook", installation_id="zenbook-inst", fleet_id="home",
            authority_id="oracle-auth", authority=self.spy, verifier=lambda op: True,
            boot=lambda: "boot-a", wall=self.clock.wall,
        )
        self.addCleanup(opened.close)
        status = self.store.status()
        opened.fence(status["incarnation"], status["epoch"] - 1, status["mode_version"])
        raw, fields = self.live_fields("ask", body, "optrue001", attempt="attempt02")
        signed, ticket = self.admission(fields, "ordinary")
        self.assertIs(self.tickets.verify(signed), True)
        self.assertIsInstance(self.tickets._peer, TicketSignatureVerifier)
        self.assertIsNotNone(self.tickets._peer)
        self.assertTrue(callable(self.tickets._peer))
        self.assertIs(self.tickets._peer.verify(signed), True)
        tampered = dict(signed)
        tampered["epoch"] = signed["epoch"] + 1
        self.assertIs(self.tickets._peer.verify(tampered), False)
        self.assertIs(self.tickets.verify(tampered), False)
        lame = GatewayPhoneReceiver(
            self.server, self.keys["ingress"][0], device_id="zenbook", guard=Count(opened),
        )
        refused = self.refuse(
            lame.receive, self.relay(raw, fields), signed_operation=signed, admission_ticket=ticket,
        )
        self.assertEqual(refused.code, "unavailable")
        self.assertEqual(self.spy.consumes, 0)
        self.assertEqual(self.console.calls, [])
        self.assertEqual(self.receipts(), before)

    def test_ask_wire_trace_target_and_recovered_status(self):
        self.open_fleet()
        body = {"text": "open the notes", "device": "dell"}
        raw, fields = self.live_fields("ask", body, "opask0001")
        signed, ticket = self.admission(fields, "ordinary")
        wire = self.receiver.receive(
            self.relay(raw, fields), signed_operation=signed, admission_ticket=ticket,
        )
        self.assertEqual(wire, {"operation_id": "opask0001", "accepted": True})
        self.assertEqual(self.console.calls, [("open the notes", "dell", "zenbook")])
        self.assertEqual(self.spy.consumes, 2)
        self.assertEqual(self.server._keys[("pixel", "dell", "opask0001")][0], "abc123")
        stored = json.dumps(self.server._gateway_ops["opask0001"]["result"])
        self.assertNotIn("open the notes", stored)
        status_body = {"operation_id": "opask0001", "kind": "ask"}
        status_raw, status_fields = self.live_fields("effect_status", status_body, "opstat001")
        self.console.submit = _boom
        recovered = self.receiver.receive(self.relay(status_raw, status_fields))
        self.assertEqual(set(recovered), {"operation_id", "result"})
        self.assertEqual(recovered["result"]["status"], 200)
        outcome = recovered["result"]["body"]
        self.assertEqual(outcome["reply"], "noted")
        self.assertEqual(outcome["job"], "abc123")
        self.assertEqual(outcome["kind"], "ask")
        self.assertEqual(outcome["trace"]["steps"][0]["label"], "Look")
        self.assertEqual(self.spy.consumes, 2)
        self.assertEqual(self.guard.accepts, 1)
        again = self.receiver.receive(
            self.relay(raw, fields), signed_operation=signed, admission_ticket=ticket,
        )
        self.assertEqual(again, {"operation_id": "opask0001", "accepted": True})
        self.assertEqual(self.spy.consumes, 2)
        self.assertEqual(self.guard.accepts, 1)
        other = {"text": "open the notes later", "device": "dell"}
        other_raw, other_fields = self.live_fields("ask", other, "opask0001", attempt="attempt09")
        other_signed, other_ticket = self.admission(other_fields, "ordinary")
        refused = self.refuse(
            self.receiver.receive, self.relay(other_raw, other_fields),
            signed_operation=other_signed, admission_ticket=other_ticket,
        )
        self.assertEqual(refused.code, "conflict")
        self.assertEqual(self.spy.consumes, 2)
        self.assertNotIn("pixel", self.server._known)
        first = self.server.gateway_identity(GatewayPhoneVerifier(self.keys["ingress"][0]).verify(raw))
        second = self.server.gateway_identity(GatewayPhoneVerifier(self.keys["ingress"][0]).verify(status_raw))
        self.assertIs(first, second)

    def test_reads_check_the_live_tuple_and_do_not_accept(self):
        self.open_fleet()
        self.console.jobs["abc123"] = {
            "id": "abc123", "state": "done", "result": {"reply": "saved"},
            "via": "phone:pixel", "started": 1.0, "finished": 2.0,
        }
        before = self.spy.status_calls
        overview_raw, overview_fields = self.live_fields("overview", {}, "opover001")
        overview = self.receiver.receive(self.relay(overview_raw, overview_fields))
        self.assertEqual(set(overview), {"operation_id", "result"})
        self.assertEqual(overview["result"]["status"], 200)
        self.assertIn("devices", overview["result"]["body"])
        self.assertGreaterEqual(self.spy.status_calls - before, 2)
        fleet_raw, fleet_fields = self.live_fields("fleet", {}, "opfleet01")
        fleet = self.receiver.receive(self.relay(fleet_raw, fleet_fields))
        self.assertEqual(fleet["result"]["body"]["audience"], "phone")
        self.assertIs(fleet["result"]["body"]["can_write"], False)
        poll_raw, poll_fields = self.live_fields("ask_poll", {"job": "abc123"}, "oppoll001")
        poll = self.receiver.receive(self.relay(poll_raw, poll_fields))
        self.assertEqual(poll["result"]["body"]["reply"], "saved")
        frame_body = {"session": SESSION}
        frame_raw, frame_fields = self.live_fields("session_frame", frame_body, "opframe01")
        frame = self.receiver.receive(self.relay(frame_raw, frame_fields))
        self.assertEqual(set(frame), {"operation_id", "result", "raw_b64"})
        self.assertEqual(base64.b64decode(frame["raw_b64"]), b"\xff\xd8\xff")
        self.assertNotIn("raw", frame["result"]["body"])
        self.assertEqual(self.hub.frames[-1][2], "phone:pixel")
        missing_raw, missing_fields = self.live_fields(
            "effect_status", {"operation_id": "opmissing1"}, "opmiss001",
        )
        refused = self.refuse(self.receiver.receive, self.relay(missing_raw, missing_fields))
        self.assertEqual(refused.code, "not_found")
        old = dict(overview_fields)
        old["operation_id"] = "opold0001"
        old["epoch"] = overview_fields["epoch"] + 5
        old_raw, old_fields = self.pack(old, {})
        refused = self.refuse(self.receiver.receive, self.relay(old_raw, old_fields))
        self.assertEqual(refused.code, "stale")
        self.assertEqual(self.guard.accepts, 0)
        self.assertEqual(self.spy.consumes, 0)
        self.assertEqual(self.console.calls, [])
        self.assertEqual(self.hub.opens, [])
        self.assertEqual(self.receipts(), 0)

    def test_stale_after_the_read_discards_the_reply(self):
        self.open_fleet()
        flip = Flip(self.store)
        path = Path(self.tmp.name) / "exec-flip.sqlite"
        inner = ExecutorGuard(
            path, device_id="zenbook", installation_id="zenbook-inst", fleet_id="home",
            authority_id="oracle-auth", authority=flip, verifier=self.tickets,
            boot=lambda: "boot-a", wall=self.clock.wall,
        )
        self.addCleanup(inner.close)
        receiver = GatewayPhoneReceiver(
            self.server, self.keys["ingress"][0], device_id="zenbook", guard=Count(inner),
        )
        raw, fields = self.live_fields("overview", {}, "opafter01")
        refused = self.refuse(receiver.receive, self.relay(raw, fields))
        self.assertEqual(refused.code, "stale")
        self.assertGreaterEqual(flip.n, 2)
        self.assertEqual(len(self.hub.sessions_for), 1)
        self.assertEqual(receiver.guard.accepts, 0)
        self.assertEqual(self.console.calls, [])
        self.assertEqual(self.spy.consumes, 0)

    def test_session_id_is_recovered_without_redoing_work(self):
        self.open_fleet()
        start = {"device": "dell", "session_kind": "screen", "quality": "low"}
        raw, fields = self.live_fields("session_start", start, "opstart01")
        signed, ticket = self.admission(fields, "interactive")
        wire = self.receiver.receive(
            self.relay(raw, fields), signed_operation=signed, admission_ticket=ticket,
        )
        self.assertEqual(wire, {"operation_id": "opstart01", "accepted": True})
        self.assertEqual(self.hub.opens[0][3], "phone:pixel")
        status_raw, status_fields = self.live_fields(
            "effect_status", {"operation_id": "opstart01"}, "opstartst",
        )
        self.hub.remote_open = _boom
        recovered = self.receiver.receive(self.relay(status_raw, status_fields))
        self.assertEqual(recovered["result"]["status"], 200)
        self.assertEqual(recovered["result"]["body"]["id"], SESSION)
        self.assertEqual(recovered["result"]["body"]["kind"], "session_start")
        entered = {"session": SESSION, "events": [{"t": "move", "x": 0.2, "y": 0.4}]}
        entered_raw, entered_fields = self.live_fields("session_input", entered, "opinput01")
        entered_signed, entered_ticket = self.admission(entered_fields, "interactive", SESSION)
        entered_wire = self.receiver.receive(
            self.relay(entered_raw, entered_fields),
            signed_operation=entered_signed, admission_ticket=entered_ticket,
        )
        self.assertEqual(entered_wire["accepted"], True)
        self.assertEqual(self.hub.inputs[0][2], "phone:pixel")
        stop = {"session": SESSION, "release_only": True}
        stop_raw, stop_fields = self.live_fields("session_stop", stop, "opstop001")
        stop_signed, stop_ticket = self.admission(stop_fields, "ordinary")
        stop_wire = self.receiver.receive(
            self.relay(stop_raw, stop_fields),
            signed_operation=stop_signed, admission_ticket=stop_ticket,
        )
        self.assertEqual(stop_wire, {"operation_id": "opstop001", "accepted": True})
        self.assertEqual(self.hub.closes, [(SESSION, "phone:pixel")])
        self.assertEqual(self.guard.stops, 1)
        again = self.inner.stop_interactive(SESSION, "phone_release")
        self.assertTrue(again["already"])
        logout = {"phone_id": "pixel"}
        logout_raw, logout_fields = self.live_fields("logout_end", logout, "oplogout1")
        logout_signed, logout_ticket = self.admission(logout_fields, "ordinary")
        logout_wire = self.receiver.receive(
            self.relay(logout_raw, logout_fields),
            signed_operation=logout_signed, admission_ticket=logout_ticket,
        )
        self.assertEqual(logout_wire["accepted"], True)
        self.assertEqual(self.hub.ended, ["phone:pixel"])
        self.assertFalse((Path(self.tmp.name) / "state" / "phone.json").exists())
        self.assertEqual(self.spy.consumes, 8)
