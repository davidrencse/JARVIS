"""MAIN handoff across a gateway process, two PCs, and a scripted phone.

A, B, the phone, and the gateway are separate processes on 127.0.0.1. Leases
use real time. Signed phone identity is a device signature, not a cookie. Brain
movement and membership removal are separate rows. A passing row is not a live
Oracle host, a Dell service, or the phone listener on 127.0.0.1:7787.

Run: env $(env | grep -o '^CODEX_[A-Z_]*' | sed 's/^/-u /') JARVIS_NO_MODELS=1 \
  .venv/bin/python -m unittest tests.continuity.test_drill_handoff
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from jarvis.continuity.gateway import AuthorityClient, GatewayError
from jarvis.continuity.runtime import MainRuntime

from tests.continuity.test_drill_process import (
    CLIENT,
    PC,
    SeamGap,
    SeamMixin,
    GatewayProc,
    LoopProxy,
    WorkerProc,
    listener_open,
    make_keys,
    origin_port,
    post_bytes,
    refuse_live,
    write_config,
)

FLEET = "drill-fleet"
AUTH = "drill-auth"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789+/"


def must(reply):
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise AssertionError("worker refused: %s" % json.dumps(reply, default=str)[:800])
    return reply.get("result")


def _flip_char(signature):
    """Change one payload character. Leave the SSH armor so verification runs."""
    begin = signature.find("BEGIN SSH SIGNATURE")
    end = signature.find("END SSH SIGNATURE")
    header_end = signature.find("\n", begin)
    if begin < 0 or end <= begin or header_end < 0 or header_end >= end:
        raise AssertionError("signature armor missing")
    chars = list(signature)
    for index in range(header_end + 1, end):
        char = chars[index]
        if char in _ALPHABET:
            chars[index] = "B" if char != "B" else "C"
            mutated = "".join(chars)
            if "BEGIN SSH SIGNATURE" not in mutated or "END SSH SIGNATURE" not in mutated:
                raise AssertionError("flip broke the armor")
            return mutated
    raise AssertionError("signature had no flippable character")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class World:
    """One temp fleet. Cleanup stops every child even when startup fails."""

    def __init__(self, case):
        self.case = case
        self.root = Path(tempfile.mkdtemp(prefix="jarvis-drill-hand-"))
        self.closed = False
        self.gateway = None
        self.proxy = None
        self.owner = None
        self.workers = {}
        self.origin = None
        self.pids = {}
        case.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        case.addCleanup(self.close)
        refuse_live(self.root)
        self.keys = make_keys(self.root / "keys", ("owner", "node-a", "node-b", "node-c", "phone"))
        self.sentinel = self.root / "sentinel"
        self.sentinel.write_bytes(b"drill-sentinel-not-a-vault")
        self.sentinel_hash = _sha(self.sentinel)

    def close(self):
        """Stop every child. One failure must not leave the others running."""
        if self.closed:
            return
        self.closed = True
        errors = []

        def attempt(action):
            try:
                action()
            except Exception as exc:
                errors.append(exc)

        proxy = self.proxy
        self.proxy = None
        if proxy is not None:
            attempt(proxy.close)
        workers = list(self.workers.values())
        self.workers.clear()
        for worker in reversed(workers):
            attempt(worker.stop)
        gateway = self.gateway
        self.gateway = None
        if gateway is not None:
            attempt(gateway.stop)
        if errors:
            raise errors[0]

    def start_gateway(self):
        state = self.root / "authority.sqlite"
        # 15s is the longest validity CachedAuthority will keep. A shorter lease
        # expires while a loaded node is still inside one relay.
        cfg = write_config(
            self.root, state=state, fleet=FLEET, authority=AUTH,
            owner_public=self.keys["owner"][0], name="gateway.json",
            lease_seconds=15)
        self.gateway = GatewayProc(cfg)
        info = self.gateway.serve()
        self.origin = info["origin"]
        self.pids["gateway"] = info["pid"]
        self.pids["test"] = os.getpid()
        self.case.assertEqual(set(info), {"authority_id", "origin", "pid"})
        self.case.assertEqual(info["pid"], self.gateway.proc.pid)
        self.case.assertNotEqual(info["pid"], os.getpid())
        self.case.assertTrue(info["origin"].startswith("http://127.0.0.1:"))
        self.case.assertNotIn(str(self.root), info["origin"])
        self.owner = AuthorityClient(
            self.origin, authority_id=AUTH, owner_private_key_path=self.keys["owner"][1])
        return info

    def open_proxy(self):
        if self.proxy is not None:
            self.proxy.close()
        host, port = origin_port(self.origin)
        self.proxy = LoopProxy(host, port)
        return self.proxy

    def boot(self, device_id, role, origin):
        worker = WorkerProc()
        worker.start()
        self.workers[device_id] = worker
        journal = None if role == "phone" else str(self.root / ("%s.sqlite" % device_id))
        public = {name: self.keys[name][0] for name in ("node-a", "node-b", "node-c")}
        view = must(worker.call("boot", {
            "role": "phone" if role == "phone" else "node",
            "origin": origin,
            "authority_id": AUTH,
            "fleet_id": FLEET,
            "device_id": device_id,
            "installation_id": device_id + "-inst",
            "private_key": str(self.keys[device_id][1]),
            "public_keys": public,
            "journal": journal,
        }))
        self.pids[device_id] = view["pid"]
        self.case.assertNotEqual(view["pid"], os.getpid())
        self.case.assertNotEqual(view["pid"], self.pids.get("gateway"))
        return view

    def enroll(self, device_id, kind, caps, relay=None):
        record = {
            "device_id": device_id,
            "installation_id": device_id + "-inst",
            "kind": kind,
            "public_key": self.keys[device_id][0],
            "capabilities": list(caps),
        }
        if relay is not None:
            record["relay_origin"] = relay
        # One attempt can sit for the client timeout before it reports
        # unavailable. The retry budget has to cover more than one attempt.
        deadline = time.monotonic() + 120
        while True:
            try:
                return self.owner.enroll(record)
            except GatewayError as exc:
                if exc.code != "unavailable" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def cutover(self, nodes):
        status = must(nodes[0].call("status"))
        preparing = self.owner.set_mode("PREPARING_V2", status["mode_version"])
        quiesced = self.owner.set_mode("QUIESCED", preparing["mode_version"])
        for node in nodes:
            ack = must(node.call("fence", {
                "incarnation": status["incarnation"],
                "epoch": 0,
                "mode_version": quiesced["mode_version"] + 1,
            }))
            self.case.assertTrue(ack["authority_recorded"])
            self.case.assertTrue(ack["legacy_closed"])
        self.owner.set_mode("V2_ONLY_NO_MAIN", quiesced["mode_version"])

    def promote(self, worker, device_id, key):
        status = must(worker.call("status"))
        ack = must(worker.call("fence", {
            "incarnation": status["incarnation"],
            "epoch": status["epoch"],
            "mode_version": status["mode_version"],
        }))
        self.case.assertTrue(ack["authority_recorded"])
        transition = self.owner.transition_request(
            key, "recover", device_id, status["incarnation"], status["epoch"], status["policy_version"])
        tid = transition["transition_id"]
        must(worker.call("readiness", {"transition_id": tid}))
        self.owner.step(tid, "drain")
        self._fence(tid)
        lease = self.owner.step(tid, "grant")
        self.case.assertEqual(lease["holder"], device_id)
        self.case.assertFalse(lease["ready"])
        must(worker.call("accept_lease", {"lease": lease}))
        done = self.owner.step(tid, "complete", True)
        self.case.assertEqual(done["state"], "SUCCEEDED")
        started = must(worker.call("start"))
        self.case.assertIsNone(started["block"], started)
        holding = must(worker.call("hold", {"on": True}))
        self.case.assertEqual(holding["renew"].get("code"), "ok", holding)
        current = must(worker.call("status"))
        self.case.assertEqual(current["main"], device_id)
        self.case.assertEqual(current["mode"], "V2_ACTIVE")
        self.case.assertTrue(current["lease_valid"])
        self.case.assertTrue(current["main_ready"])
        return current

    def _fence(self, tid):
        deadline = time.monotonic() + 60
        while True:
            try:
                self.owner.step(tid, "fence")
                return
            except GatewayError as exc:
                if exc.code not in ("not_quiet", "lease_live"):
                    raise
                if exc.wait is None:
                    raise AssertionError("fence %s refused without a wait" % exc.code)
                if time.monotonic() + exc.wait > deadline:
                    raise AssertionError("fence wait %s exceeds the deadline" % exc.wait)
                time.sleep(exc.wait)

    def retarget(self, *device_ids, origin):
        for device_id in device_ids:
            must(self.workers[device_id].call("set_origin", {"origin": origin}))

    def relay(self, main_hint):
        status = must(self.workers["phone"].call("status"))
        self.case.assertEqual(status["main"], main_hint)
        return status["main"]

    def wait_barrier(self, device_id):
        deadline = time.monotonic() + 60
        while True:
            status = must(self.workers[device_id].call("status"))
            barrier = status.get("grant_barrier")
            if barrier == 0:
                return status
            if isinstance(barrier, bool) or not isinstance(barrier, (int, float)):
                raise AssertionError("grant_barrier missing: %s" % status)
            if time.monotonic() > deadline:
                raise AssertionError("grant_barrier stayed %s" % barrier)
            time.sleep(min(float(barrier), 1.0))


class HandoffDrill(SeamMixin, unittest.TestCase):
    def test_release_recovery_phone_route_network_loss_and_restart(self):
        world = World(self)
        info = world.start_gateway()
        proxy = world.open_proxy()
        node_a = world.boot("node-a", "node", proxy.origin)
        node_b = world.boot("node-b", "node", info["origin"])
        phone = world.boot("phone", "phone", info["origin"])
        self.assertEqual(len(set(world.pids.values())), len(world.pids))
        world.enroll("node-a", "pc", PC, node_a["relay_origin"])
        world.enroll("node-b", "pc", PC, node_b["relay_origin"])
        world.enroll("phone", "client", CLIENT)
        world.cutover([world.workers["node-a"], world.workers["node-b"]])
        world.promote(world.workers["node-a"], "node-a", "k-a")
        main, first = self._relay(world, "op-a-1")
        self.assertEqual(main, "node-a")
        self.assertTrue(first["upstream_accepted"])
        self.assertFalse(first["effect_authorized"])
        _same, again = self._relay(world, "op-a-1")
        self.assertTrue(again["upstream_accepted"])
        hits_a = must(world.workers["node-a"].call("hits"))
        self.assertEqual(hits_a["effects"], ["op-a-1"])
        self.assertEqual(len(hits_a["seen"]), 1)
        self.assertEqual(hits_a["seen"][0]["peer"], "127.0.0.1")
        self.assertEqual(must(world.workers["node-b"].call("hits"))["seen"], [])
        stashed = must(world.workers["node-a"].call("sign_ticket", {"operation_id": "op-stash"}))
        must(world.workers["node-a"].call("hold", {"on": False}))
        must(world.workers["node-a"].call("renew"))
        gateway_port = origin_port(info["origin"])[1]
        proxy.close()
        world.proxy = None
        self.assertFalse(proxy._thread.is_alive())
        self.assertFalse(listener_open(proxy.port))
        self.assertTrue(listener_open(gateway_port))
        refused = world.workers["node-a"].call("require")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], "unavailable")
        self.assertEqual(must(world.workers["node-a"].call("hits"))["effects"], ["op-a-1"])
        _cut_main, cut = self._relay(world, "op-cut", expect_main="node-a")
        self.assertFalse(cut["upstream_accepted"])
        self.assertFalse(cut["effect_authorized"])
        after_cut = must(world.workers["node-a"].call("hits"))
        self.assertEqual(after_cut["effects"], ["op-a-1"])
        self.assertEqual(len(after_cut["seen"]), 2)
        self.assertEqual(after_cut["seen"][1]["peer"], "127.0.0.1")
        self.assertFalse(after_cut["seen"][1]["accepted"])
        self.assertEqual(must(world.workers["node-b"].call("status"))["authority_id"], AUTH)
        self.assertIsNone(world.gateway.proc.poll())
        replacement = world.open_proxy()
        world.retarget("node-a", origin=replacement.origin)
        released = must(world.workers["node-a"].call("release"))
        self.assertTrue(released["released"])
        self.assertTrue(released["acked"])
        self.assertEqual(released["code"], "released")
        suspended = world.workers["node-a"].call("require")
        self.assertFalse(suspended["ok"])
        self.assertEqual(suspended["error"], "suspend")
        world.promote(world.workers["node-b"], "node-b", "k-b")
        main, routed = self._relay(world, "op-b-1")
        self.assertEqual(main, "node-b")
        self.assertTrue(routed["upstream_accepted"])
        self.assertFalse(routed["effect_authorized"])
        hits_b = must(world.workers["node-b"].call("hits"))
        self.assertEqual(hits_b["effects"], ["op-b-1"])
        self.assertEqual(hits_b["seen"][0]["peer"], "127.0.0.1")
        self.assertEqual(must(world.workers["node-a"].call("hits"))["effects"], ["op-a-1"])
        stale = must(world.workers["node-a"].call("accept_ticket", stashed))
        self.assertFalse(stale["ran"])
        self.assertEqual(stale["error"], "stale")
        forged = {"signed": dict(stashed["signed"]), "ticket": stashed["ticket"]}
        forged["signed"]["signature"] = _flip_char(forged["signed"]["signature"])
        tampered = must(world.workers["node-a"].call("accept_ticket", forged))
        self.assertFalse(tampered["ran"])
        self.assertEqual(tampered["error"], "signature")
        self.assertEqual(_sha(world.sentinel), world.sentinel_hash)
        before = must(world.workers["node-b"].call("status"))
        must(world.workers["node-b"].call("hold", {"on": False}))
        old_port = origin_port(world.origin)[1]
        self.assertEqual(world.gateway.stop(), 0)
        deadline = time.monotonic() + 90
        while listener_open(old_port) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(listener_open(old_port))
        info = world.start_gateway()
        world.retarget("node-a", "node-b", "phone", origin=info["origin"])
        restarted = must(world.workers["node-b"].call("status"))
        self.assertGreater(restarted["incarnation"], before["incarnation"])
        self.assertGreater(restarted["epoch"], before["epoch"])
        self.assertFalse(restarted["lease_valid"])
        self.assertIsNone(restarted["main"])
        self.assertEqual(restarted["mode"], "V2_ONLY_NO_MAIN")
        self.assertLess(stashed["ticket"]["epoch"], restarted["epoch"])
        old = must(world.workers["node-a"].call("accept_ticket", stashed))
        self.assertFalse(old["ran"])
        self.assertEqual(old["error"], "stale")
        world.wait_barrier("node-b")
        world.promote(world.workers["node-b"], "node-b", "k-b2")
        main, recovered = self._relay(world, "op-b-2")
        self.assertEqual(main, "node-b")
        self.assertTrue(recovered["upstream_accepted"])
        self.assertFalse(recovered["effect_authorized"])
        node_c = world.boot("node-c", "node", info["origin"])
        denied = world.workers["node-c"].call("status")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"], "unauthorized")
        world.enroll("node-c", "pc", PC, node_c["relay_origin"])
        joined = must(world.workers["node-c"].call("status"))
        self.assertEqual(joined["authority_id"], AUTH)
        recorded = must(world.workers["node-c"].call("fence", {
            "incarnation": joined["incarnation"],
            "epoch": joined["epoch"],
            "mode_version": joined["mode_version"],
        }))
        self.assertTrue(recorded["authority_recorded"])
        still = must(world.workers["node-b"].call("status"))
        self.assertEqual(still["main"], "node-b")
        self.assertTrue(still["lease_valid"])
        main, after_join = self._relay(world, "op-b-3")
        self.assertEqual(main, "node-b")
        self.assertTrue(after_join["upstream_accepted"])
        self.assertEqual(must(world.workers["node-c"].call("hits"))["effects"], [])
        self.assertEqual(_sha(world.sentinel), world.sentinel_hash)
        self.assertGreaterEqual(len(set(world.pids.values())), 6)

    def test_participant_removal_is_owner_proven_and_revokes_the_worker(self):
        world = World(self)
        info = world.start_gateway()
        node_c = world.boot("node-c", "node", info["origin"])
        denied = world.workers["node-c"].call("status")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"], "unauthorized")
        world.enroll("node-c", "pc", PC, node_c["relay_origin"])
        joined = must(world.workers["node-c"].call("status"))
        self.assertEqual(joined["mode"], "LEGACY_ACTIVE")
        extra = {
            "device_id": "node-c", "installation_id": "node-c-inst", "kind": "pc",
            "public_key": world.keys["node-c"][0], "capabilities": list(PC),
            "relay_origin": node_c["relay_origin"], "eligible": False,
        }
        with self.assertRaises(GatewayError) as raised:
            world.owner.enroll(extra)
        self.assertEqual(raised.exception.code, "bad_request")
        # The route exists, but a body with no owner proof is refused, never a 404 or a removal.
        status, headers, body = post_bytes(
            info["origin"], "/v1/authority/participants/disable", b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "bad_request"})
        self.assertFalse(any(key.lower() == "set-cookie" for key in headers))
        still = must(world.workers["node-c"].call("status"))
        self.assertEqual(still["authority_id"], AUTH)
        ack = must(world.workers["node-c"].call("fence", {
            "incarnation": still["incarnation"], "epoch": still["epoch"],
            "mode_version": still["mode_version"],
        }))
        self.assertTrue(ack["authority_recorded"])
        with self.assertRaises(GatewayError) as wrong:
            world.owner.disable_participant("node-c", "other-inst")
        self.assertEqual(wrong.exception.code, "installation_mismatch")
        with self.assertRaises(GatewayError) as ghost:
            world.owner.disable_participant("ghost", "ghost-inst")
        self.assertEqual(ghost.exception.code, "unknown_participant")
        meta = world.owner.disable_participant("node-c", "node-c-inst")
        self.assertTrue(meta["disabled"])
        self.assertFalse(meta["eligible"])
        self.assertFalse(meta["already"])
        self.assertTrue(world.owner.disable_participant("node-c", "node-c-inst")["already"])
        refused = world.workers["node-c"].call("fence", {
            "incarnation": still["incarnation"], "epoch": still["epoch"],
            "mode_version": still["mode_version"],
        })
        self.assertFalse(refused["ok"])
        # The worker reads authenticated status before acknowledging a fence.
        # Disabled installations now lose that status capability immediately;
        # direct acknowledge still retains the authority's revoked response.
        self.assertEqual(refused["error"], "unauthorized")

    def test_phone_cookie_is_a_ws4_seam(self):
        world = World(self)
        info = world.start_gateway()
        node_a = world.boot("node-a", "node", info["origin"])
        node_b = world.boot("node-b", "node", info["origin"])
        world.boot("phone", "phone", info["origin"])
        world.enroll("node-a", "pc", PC, node_a["relay_origin"])
        world.enroll("node-b", "pc", PC, node_b["relay_origin"])
        world.enroll("phone", "client", CLIENT)
        world.cutover([world.workers["node-a"], world.workers["node-b"]])
        world.promote(world.workers["node-a"], "node-a", "k-cookie-a")
        released = must(world.workers["node-a"].call("release"))
        self.assertTrue(released["released"])
        world.promote(world.workers["node-b"], "node-b", "k-cookie-b")
        main, routed = self._relay(world, "op-cookie-b")
        self.assertEqual(main, "node-b")
        self.assertTrue(routed["upstream_accepted"])
        self.assertFalse(routed["effect_authorized"])
        self.assertEqual(must(world.workers["node-b"].call("hits"))["effects"], ["op-cookie-b"])
        headers = {
            "Cookie": "__Host-jarvis_phone=drill-cookie-not-a-signature",
            "X-CSRF-Token": "drill-csrf",
            "X-Forwarded-For": "203.0.113.9",
        }
        responses = []
        for path in ("/phone/pair", "/phone/api/ask"):
            status, response_headers, body = post_bytes(info["origin"], path, b"{}", headers=headers)
            responses.append((path, status, response_headers, body))
        cookie_set = any(
            key.lower() == "set-cookie" for _path, _status, response_headers, _body in responses
            for key in response_headers)
        session = any(status == 200 for _path, status, _headers, _body in responses)
        if session or cookie_set:
            # A body that mentions node-b is not a session. The raw cookie has
            # to change the result, and the same call without it must not be a
            # 200 or a Set-Cookie. This gateway is still the temp loopback
            # process, not the live phone listener.
            text = b" ".join(body for _path, _status, _headers, body in responses).decode("utf-8", "replace")
            self.assertIn("node-b", text)
            self.assertNotIn("node-a", text)
            self.assertNotIn("BEGIN SSH SIGNATURE", text)
            self.assertNotIn("drill-cookie-not-a-signature", text)
            for path, status, response_headers, body in responses:
                self.assertTrue(status == 200 or any(
                    key.lower() == "set-cookie" for key in response_headers), path)
                for key, value in response_headers.items():
                    if key.lower() == "set-cookie":
                        self.assertNotIn("drill-cookie-not-a-signature", value)
                bare_status, bare_headers, bare_body = post_bytes(
                    info["origin"], path, b"{}", headers={"X-CSRF-Token": "drill-csrf"})
                self.assertNotEqual(bare_status, 200, path)
                self.assertFalse(any(key.lower() == "set-cookie" for key in bare_headers), path)
                self.assertNotEqual(bare_body, body, path)
            return
        for path, status, _headers, body in responses:
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(body.decode("utf-8")), {"error": "not_found"})
        raise SeamGap(
            "WS4 PhoneProtocolAdapter and ContinuityGatewaySession are in-process. "
            "Signed installation identity is not cookie continuity. "
            "GATEWAY_SESSION_DEPENDENCIES network_transient is unsupported, "
            "network_transition is unsupported, and this module invents no gateway route. "
            "PC_SEAM refuses a raw __Host-jarvis_phone cookie and X-Forwarded-For.")

    def test_brain_transfer_is_a_ws3_seam(self):
        """WS3 brain bytes are a separate call from signed MAIN identity.

        ``SafeHandoff`` imports ``jarvis.brainsync.handoff`` only after a fence.
        With brainsync left off, that hook refuses and a temp sentinel stays.
        Enabling it would open ``SshBundleTransport`` (``deploy._remote``). This
        drill does not do that and does not use the live vault.
        """
        from jarvis.brainsync import SshBundleTransport, handoff
        from jarvis.continuity.runtime import SafeHandoff, _brainsync_handoff

        folder = tempfile.TemporaryDirectory(prefix="jarvis-drill-brain-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        repo_live = Path(__file__).resolve().parents[2] / ".jarvis"
        home_live = Path.home() / ".local" / "state" / "jarvis"
        self.assertFalse(root.resolve().is_relative_to(repo_live.resolve()))
        self.assertFalse(root.resolve().is_relative_to(home_live.resolve()))
        sentinel = root / "sentinel"
        sentinel.write_bytes(b"drill-sentinel-not-a-vault")
        digest = _sha(sentinel)
        base = root / "base"
        base.mkdir()
        carried = base / "sentinel"
        carried.write_bytes(sentinel.read_bytes())
        self.assertTrue(callable(handoff))
        self.assertTrue(callable(SafeHandoff))
        self.assertTrue(callable(SshBundleTransport.push))
        result = _brainsync_handoff(
            "node-a", "node-b", {"brainsync": {"enabled": False}}, os.fspath(base))
        self.assertIsInstance(result, dict)
        self.assertIs(result.get("ok"), False)
        self.assertEqual(result.get("error"), "disabled")
        self.assertEqual(_sha(sentinel), digest)
        self.assertEqual(carried.read_bytes(), b"drill-sentinel-not-a-vault")
        self.assertFalse((base / ".jarvis" / "brainsync-handoff.json").exists())
        if result.get("ok") is True or any(
                hasattr(MainRuntime, name) for name in ("transfer_brain", "handoff_brain")):
            self.fail(
                "brain bytes must follow the new MAIN before this row passes: %s" % (result,))
        raise SeamGap(
            "WS3 SafeHandoff._promote calls jarvis.brainsync.handoff after the fence. "
            "With brainsync disabled that hook returns error=disabled and the temp sentinel stays. "
            "The default transport is SshBundleTransport through deploy._remote. "
            "This drill does not enable brainsync, open SSH, or use the live vault. "
            "MainRuntime.release_before_suspend has no transfer_brain or handoff_brain. "
            "Signed MAIN identity is not brain movement.")

    def _relay(self, world, operation_id, expect_main=None):
        if expect_main is None:
            status = must(world.workers["phone"].call("status"))
            expect_main = status["main"]
            self.assertIsNotNone(expect_main)
        result = must(world.workers["phone"].call("relay", {
            "operation_id": operation_id, "target": expect_main, "attempt_id": "try-1",
        }))
        return expect_main, result


if __name__ == "__main__":
    unittest.main()
