"""Composed loopback of Arch sleep, Dell promotion, one phone session and replay refusal.

Everything here is the real module: `AuthorityStore` behind `ContinuityGateway`,
`AuthorityClient` with installation-key signatures, `MainRuntime`, `ExecutorGuard`,
`ContinuityGatewaySession` and `MobileIngress`. The stand-ins are a loopback MAIN
relay sink per PC and a fake `Clock`. A passing run is not a live failover: no Oracle
gateway, Tailscale, Dell, or the phone listener on 127.0.0.1:7787 has been exercised.

`deployment_facts` is only called with a temp config. The repo `jarvis.config.json`
is not opened by these tests. `ContinuityIntegration.blockers` still reads that
file inside the production refuse path; a missing `gateway_host_unverified` there
is a device id named oracle, not an enrolled host.
"""

import json
import re
import socket
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.request import Request, urlopen

from jarvis.continuity.authority import LEASE_SECONDS, SAFETY_MARGIN, AuthorityError, AuthorityStore
from jarvis.continuity.executor import ExecutorGuard
from jarvis.continuity.gateway import AuthorityClient, ContinuityGateway, GatewayError
from jarvis.continuity.integration import (
    AUTHORITY_ID,
    CONTRACT_HTTP,
    EXTRA_HTTP,
    FLEET_ID,
    HTTP_STATUS,
    INCOMPATIBILITIES,
    ORIGIN,
    SELF_RECOVERY,
    ContinuityIntegration,
    IntegrationError,
    IsolatedFleet,
    LinkedClient,
    _blockers,
    deployment_facts,
    apply_node_seen,
    close_unfenced,
    http_status,
    node_seen_mode,
    open_unfenced,
    peer_report,
    phone_listener_open,
    refuse_unfenced,
    set_node_seen_mode,
)
from jarvis.continuity.ingress import (
    PHONE_COOKIE,
    ContinuityGatewaySession,
    IngressError,
    PhoneProtocolAdapter,
)
from jarvis.continuity.runtime import GatewayPort, RuntimeRefusal, SafeHandoff
from jarvis.continuity.participant import Participant
from jarvis.continuity.renewal import LeaseRenewer
from tests.continuity._fleet import Clock

SECRET = "SECRET-PROMPT-do-not-store"
HOST = "oracle.tailnet.ts.net"
PHONE_ORIGIN = "https://" + HOST


def peer_from(headers):
    if (headers.get("X-Test-Peer") or "phone") == "none":
        return None
    return {"stable_id": "phone-stable", "login": "david@example.com", "os": "iOS",
            "node": "iPhone", "address": "100.64.0.2"}


def owner_from(material):
    if material != "owner-token":
        return None
    return {"role": "owner", "installation_id": "zenbook-inst", "device_id": "zenbook"}


def hdr(cookie=None, csrf=None):
    out = {"Host": HOST, "Origin": PHONE_ORIGIN, "X-JARVIS-Phone": "1", "Sec-Fetch-Site": "same-origin",
           "Tailscale-User-Login": "david@example.com", "X-Test-Peer": "phone"}
    if cookie:
        out["Cookie"] = "%s=%s" % (PHONE_COOKIE, cookie)
    if csrf:
        out["X-CSRF-Token"] = csrf
    return out


_STABLE_BLOCKERS = (
    "phone_origin_arch_loopback",
    "dell_provider_unverified",
    "luna_grok_unverified",
    "safety_margin_unmeasured",
    "windows_clock_untested",
    "legacy_paths_unfenced",
    "passkey_verifier_absent",
    "signature_verifier_absent",
    "automatic_failover_disabled",
)
_QUIET_PEERS = {"gateway": {"compatible": True}}


def _temp_config(case, document):
    """A config that is not the repo file. `deployment_facts` reads only this path."""
    folder = TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    path = Path(folder.name) / "jarvis.config.json"
    repo = Path(__file__).resolve().parents[2] / "jarvis.config.json"
    case.assertNotEqual(path.resolve(), repo.resolve())
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# Production posts a relay with a 2s urllib timeout (gateway._post_json) and the authority
# client uses 10s. Those are wall-clock deadlines on a loopback hop: on a machine under load
# (load >30 during a release run) the node's handler thread is not scheduled inside 2s, the
# gateway answers `no_relay` (503) and the request fails although nothing is wrong. This
# fixture keeps the real transport, the at-most-once rules and every assertion, and only
# raises those two deadlines for the duration of one test. Nothing here sleeps or retries.
_LOOPBACK_DEADLINES = {2: 60, 10: 90}


def widen_loopback_deadlines(case):
    real = urllib.request.OpenerDirector.open

    def open(self, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        return real(self, fullurl, data, _LOOPBACK_DEADLINES.get(timeout, timeout))

    patcher = mock.patch.object(urllib.request.OpenerDirector, "open", open)
    patcher.start()
    case.addCleanup(patcher.stop)


def _blocker_ids(facts, peers=_QUIET_PEERS):
    return {row["id"] for row in _blockers(facts, peers)}


class ProductionBlocked(unittest.TestCase):
    def test_default_mode_refuses_cutover(self):
        """Refuse stays on. This does not read the repo config and does not prove a host.

        `activate_v2` still calls the production blocker list, which reads
        `jarvis.config.json` on its own. `gateway_host_unverified` and
        `hub_loopback` follow that file, so they are not asserted here.
        """
        integration = ContinuityIntegration()
        mode = integration.mode()
        self.assertEqual(mode["mode"], "LEGACY_ACTIVE")
        self.assertFalse(mode["v2_dispatch"])
        self.assertFalse(mode["production_failover"])
        report = integration.activate_v2({"incarnation": 1, "epoch": 9}, True)
        self.assertFalse(report["applied"])
        self.assertFalse(report["activated"])
        self.assertTrue(report["ignored_tuple"])
        ids = {row["id"] for row in report["blockers"]}
        for blocker in _STABLE_BLOCKERS:
            self.assertIn(blocker, ids)
        self.assertFalse(any(name.startswith("peer_") for name in ids))
        self.assertNotIn("no_oracle_device", ids)
        self.assertFalse(integration.v2_request({"role": "phone"}, "op-1", {"target": "dell"})["ok"])
        legacy = integration.legacy_request({"target": "dell", "effect_class": "control"})
        self.assertFalse(legacy["acted"])
        self.assertEqual(legacy["code"], "legacy_unwired")
        with self.assertRaises(IntegrationError) as caught:
            integration.legacy_request({"prompt": SECRET})
        self.assertEqual(caught.exception.code, "personal_field")
        self.assertFalse(integration.prepare_v2(1)["applied"])
        self.assertFalse(integration.quiesce_legacy(1)["applied"])
        self.assertFalse(integration.rollback_quiesced("drill", 1)["applied"])

    def test_peer_probe_and_temp_config_are_not_a_live_gateway(self):
        """Runtime and executor run a temp fixture. Gateway and ingress do not.

        `peer_report` marks gateway and ingress compatible when
        `forward_transient` and `relay_transient` are callable. That is not a
        served relay and not an Oracle process. `phone_bind` is the constant
        `127.0.0.1:7787`; the temp document's listener is ignored.
        """
        peers = peer_report()
        for name in ("runtime", "executor"):
            self.assertTrue(peers[name]["present"], name)
            self.assertTrue(peers[name]["compatible"], (name, peers[name]))
            self.assertNotIn("behavior", peers[name]["missing"], peers[name])
        self.assertTrue(callable(getattr(ContinuityGateway, "forward_transient", None)))
        self.assertTrue(callable(getattr(ContinuityGatewaySession, "relay_transient", None)))
        for name in ("gateway", "ingress"):
            self.assertTrue(peers[name]["present"], name)
            self.assertEqual(peers[name]["missing"], [], peers[name])
            self.assertTrue(peers[name]["compatible"], (name, peers[name]))
            self.assertNotIn("origin", peers[name])
            self.assertNotIn("pid", peers[name])

        missing_dir = TemporaryDirectory()
        self.addCleanup(missing_dir.cleanup)
        absent = deployment_facts(Path(missing_dir.name) / "missing.json")
        self.assertFalse(absent["config_read"])
        self.assertFalse(absent["oracle_device"])
        self.assertEqual(absent["phone_bind"], "127.0.0.1:7787")
        self.assertIn("gateway_host_unverified", _blocker_ids(absent))

        no_oracle = deployment_facts(_temp_config(self, {
            "devices": {"zenbook": {"hostname": "arch"}, "dell": {"hostname": "dell"}},
            "cluster": {"listen_host": "127.0.0.1", "listen_port": 7700,
                        "phone_bind": "0.0.0.0:9999"},
        }))
        self.assertTrue(no_oracle["config_read"])
        self.assertEqual(no_oracle["device_ids"], ["dell", "zenbook"])
        self.assertFalse(no_oracle["oracle_device"])
        self.assertEqual(no_oracle["phone_bind"], "127.0.0.1:7787")
        self.assertFalse(no_oracle["provider_verified"])
        self.assertFalse(no_oracle["windows_clock_tested"])
        no_oracle_ids = _blocker_ids(no_oracle)
        self.assertIn("gateway_host_unverified", no_oracle_ids)
        self.assertIn("hub_loopback", no_oracle_ids)

        for host in (None, "127.0.0.1", "localhost"):
            loop = dict(no_oracle, listen_host=host)
            self.assertIn("hub_loopback", _blocker_ids(loop), host)

        tailnet = deployment_facts(_temp_config(self, {
            "devices": {"zenbook": {"hostname": "arch"}},
            "cluster": {"listen_host": "100.64.0.8"},
        }))
        self.assertNotIn("hub_loopback", _blocker_ids(tailnet))
        self.assertIn("gateway_host_unverified", _blocker_ids(tailnet))

        # A devices key named oracle, with no origin, key, or enrollment, sets
        # oracle_device. _blockers then drops gateway_host_unverified. The fact
        # dict has no enrollment field, so that drop is not a verified host.
        name_only_record = {
            "hostname": "oracle-vm", "label": "oracle", "os": "linux", "profile": "cloud",
        }
        self.assertEqual(set(name_only_record), {"hostname", "label", "os", "profile"})
        named = deployment_facts(_temp_config(self, {
            "devices": {"oracle": name_only_record},
            "cluster": {"listen_host": "100.64.0.8", "main_device": "zenbook"},
        }))
        self.assertEqual(named["oracle_device"], "oracle" in named["device_ids"])
        self.assertTrue(named["oracle_device"])
        self.assertEqual(named["phone_bind"], "127.0.0.1:7787")
        for absent_key in ("enrolled", "origin", "public_key", "relay_origin"):
            self.assertNotIn(absent_key, named)
        named_ids = _blocker_ids(named)
        self.assertNotIn("gateway_host_unverified", named_ids)
        self.assertNotIn("hub_loopback", named_ids)
        self.assertEqual(
            "gateway_host_unverified" in _blocker_ids(dict(named, oracle_device=False)),
            True)
        self.assertNotIn("gateway_host_unverified", _blocker_ids(dict(named, oracle_device=True)))

        static_ids = {row["id"] for row in INCOMPATIBILITIES}
        for kept in ("no_signatures", "store_states", "no_passkey", "no_oracle_device"):
            self.assertIn(kept, static_ids)
        self.assertNotIn("peers_absent", static_ids)
        self.assertNotIn("inprocess_auth_double", static_ids)
        self.assertNotIn("gateway_host_unverified", static_ids)
        self.assertNotIn("no_oracle_device", named_ids)
        self.assertNotEqual(static_ids, named_ids)

    def test_store_codes_fail_closed(self):
        """Literal AuthorityError("code") tokens in authority.py. A dynamic code string is invisible."""
        source = Path("jarvis/continuity/authority.py").read_text(encoding="utf-8")
        codes = set(re.findall(r'AuthorityError\("([a-z_]+)"', source))
        self.assertTrue(codes)
        self.assertTrue(codes <= set(HTTP_STATUS))
        for code in codes:
            status, _ = http_status(code, 1.5)
            self.assertNotEqual(status, 200)
            self.assertIn(status, (400, 401, 403, 404, 409, 410, 503))
        self.assertEqual(CONTRACT_HTTP["ticket_expired"], 410)
        self.assertEqual(CONTRACT_HTTP["not_quiet"], 409)
        self.assertEqual(http_status("not_quiet", 3)[1], 3)
        self.assertEqual(http_status("stale", 3)[1], None)
        self.assertEqual(EXTRA_HTTP["writer_busy"], 503)
        self.assertEqual(http_status("not_found")[0], 404)
        self.assertEqual(http_status("unknown_participant")[0], 403)
        self.assertEqual(http_status("never_heard_of")[0], 503)


class _CandidateReadinessPort:
    """Like production's CandidatePort: readiness is signed by the candidate itself."""

    def __init__(self, owner_port, candidate):
        self._owner, self._candidate = owner_port, candidate

    def __getattr__(self, name):
        return getattr(self._owner, name)

    def transition_readiness(self, transition_id, facts):
        return self._candidate.client.readiness(
            transition_id, facts["control_ready"], facts["ai_ready"], facts["checkpoint"])


class StandInHandoff:
    """Stand-in for the current MAIN's bound ProductionController.

    The handoff itself is the real SafeHandoff over the isolated authority, run
    with the current MAIN as source. Only its edges are recorded stand-ins: the
    brain transfer, the signed repoint, and the owner-proxy route (a client
    signed as the source PC plus the owner key, standing in for the proxy).
    """

    def __init__(self, test, source, candidate, journal):
        self.config, self.base = {}, str(journal.parent)
        self.candidate, self.clock = candidate, test.clock
        self.brain_calls, self.brain_at, self.repoint_calls = [], [], []
        self._handoff = SafeHandoff(
            _CandidateReadinessPort(
                GatewayPort(test.fleet._client(LinkedClient, source.name, owner=True)), candidate),
            source_runtime=source.runtime,
            candidate_runtime=candidate.runtime, readiness=self._facts, repoint=self._repoint,
            canary=lambda device: device == candidate.name and test.node_canary(candidate),
            journal_path=journal, brain=self._brain)

    def _facts(self, device):
        if device != self.candidate.name:
            return None
        facts = self.candidate.runtime.readiness("control")
        return {key: facts.get(key) for key in ("control_ready", "ai_ready", "checkpoint")}

    def candidate_readiness(self, device):
        facts = self._facts(device)
        if facts is None:
            raise RuntimeRefusal("candidacy_mismatch")
        return facts

    def _brain(self, old, new, config, base):
        self.brain_calls.append((old, new))
        self.brain_at.append(self.clock.wall())
        return {"ok": True, "status": "moved"}

    def _repoint(self, old, new, lease):
        self.repoint_calls.append((old, new))
        return {"ok": True, "authenticated": True, "key_updated": True, "endpoint_updated": True}

    def __call__(self, old, new, config, base):
        return self._handoff(old, new, config, base)


class Composed(unittest.TestCase):
    """Temp fleet. Arch is MAIN. The phone session is the in-process ingress, not :7787."""

    def setUp(self):
        widen_loopback_deadlines(self)
        self.tmp = TemporaryDirectory()
        self.clock = Clock()
        self.fleet = IsolatedFleet(self.tmp.name, self.clock)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.fleet.close)
        self.arch = self.fleet.nodes["zenbook"]
        self.dell = self.fleet.nodes["dell"]
        self.relay_events = []
        for node in (self.arch, self.dell):
            self.record_relay(node)
        live = Path(__file__).resolve().parents[2] / ".jarvis"
        root = self.fleet.root.resolve()
        live_root = live.resolve()
        self.assertTrue(self.fleet.origin.startswith("http://127.0.0.1:"), self.fleet.origin)
        self.assertFalse(self.fleet.gateway.policy.auto_failover)
        self.assertFalse(root == live_root or live_root in root.parents)
        self.fleet.cutover()
        _, self.arch_lease = self.fleet.promote("zenbook")
        self.arch.runtime.start()
        self.arch.runtime.accept_lease(self.arch_lease)
        self.ingress = self.fleet.phone_ingress(
            Path(self.tmp.name) / "phone.sqlite", peer_verifier=peer_from, owner_verifier=owner_from,
            origin=PHONE_ORIGIN)
        self.ingress.activate(self.fleet.status()["mode_version"], legacy_phone_fenced=True)
        self.http = PhoneProtocolAdapter(self.ingress)
        self.cookie, self.csrf = self._pair()

    def record_relay(self, node):
        """Keep fixture errors visible instead of losing them in Handler's 503.

        Preserve the real transport, deadline, and at-most-once behavior. In
        particular, never retry an ask to make an intermittent failure pass.
        """
        real = node.handle_relay

        def recorded(body):
            event = {"node": node.name, "kind": body.get("kind")}
            started = time.monotonic()
            self.relay_events.append(event)
            try:
                result = real(body)
                event["status"] = result[0]
                return result
            except Exception as exc:
                event["exception"] = type(exc).__name__ + ": " + str(exc)
                raise
            finally:
                event["elapsed"] = time.monotonic() - started

        node.handle_relay = recorded

    def _pair(self):
        self.ingress.identify(hdr())
        owner = self.ingress.authenticate_owner("owner-token")
        issued = self.ingress.issue_code(owner, "Pixel")
        first = self.http.handle("POST", "/phone/pair", hdr(),
                                 json.dumps({"code": issued["code"], "name": "Pixel"}).encode())
        self.assertEqual(first.status, 202)
        self.ingress.decide_pairing(owner, issued["code_id"], True)
        done = self.http.handle("POST", "/phone/pair/status", hdr(),
                                json.dumps({"code": issued["code"]}).encode())
        self.assertEqual(done.status, 200, done.body)
        cookie = done.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        return cookie, done.body["csrf_token"]

    def call(self, method, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        return self.http.handle(method, path, hdr(self.cookie, self.csrf), raw)

    def ask(self, key, text="open the pod bay doors"):
        return self.call("POST", "/phone/api/ask", {"key": key, "text": text})

    def renew_arch(self):
        return self.arch.runtime.renew_once()

    def sleep_arch_promote_dell(self):
        """Arch stops renewing without releasing; Dell waits out the lease and the margin."""
        self.clock.tick(15 - 1)
        early = self.request_dell()
        self.clock.tick(2 + SAFETY_MARGIN)
        self.dell.runtime.start()
        _, lease = self.fleet.promote("dell")
        self.dell.runtime.accept_lease(lease)
        return early, lease

    def request_dell(self):
        status = self.fleet.status()
        try:
            self.fleet.owner.transition_request(
                "early-dell", "recover", "dell", status["incarnation"], status["epoch"],
                status["policy_version"])
        except GatewayError as exc:
            return exc.code
        return None

    # --- the headline property --------------------------------------------------

    def test_arch_sleep_promotes_dell_same_phone_session_sink_once_stale_arch_refused(self):
        first = self.ask("op-ask-0001")
        self.assertEqual(first.status, 200, (first.body, self.relay_events))
        self.assertEqual(self.arch.effects, ["op-ask-0001"])
        again = self.ask("op-ask-0001")
        self.assertEqual(again.status, 200, again.body)
        self.assertEqual(self.arch.effects, ["op-ask-0001"], "same operation id reached the sink twice")
        overview = self.call("GET", "/phone/api/overview")
        self.assertEqual(overview.status, 200)
        self.assertEqual(overview.body["main"], "zenbook")
        me = self.call("GET", "/phone/api/me")
        self.assertTrue(me.body["relay"])
        self.assertFalse(me.body["metadata_only"])

        early, dell_lease = self.sleep_arch_promote_dell()
        self.assertEqual(early, "main_alive")
        status = self.fleet.status()
        self.assertEqual(status["main"], "dell")
        self.assertGreater(status["epoch"], self.arch_lease["epoch"])

        # same phone cookie, same csrf: the paired session survived the failover
        me = self.call("GET", "/phone/api/me")
        self.assertEqual(me.status, 200)
        self.assertEqual(me.body["main"]["id"], "dell")
        after = self.ask("op-ask-0002")
        self.assertEqual(after.status, 200, after.body)
        self.assertEqual(self.dell.effects, ["op-ask-0002"])
        self.assertEqual(self.arch.effects, ["op-ask-0001"])
        replay = self.ask("op-ask-0002")
        self.assertEqual(replay.status, 200)
        self.assertEqual(self.dell.effects, ["op-ask-0002"])

        # stale Arch: the old holder wakes up believing it is MAIN
        self.arch.runtime.invalidate_after_resume()
        with self.assertRaises(Exception) as local:
            self.arch.runtime.require_authority("ordinary", target="zenbook")
        self.assertIn(getattr(local.exception, "code", ""), ("stale", "not_holder", "no_lease_id", "legacy"))
        code, body = self.arch.handle_relay(self.relay_body("op-stale-arch", "zenbook", "zenbook"))
        self.assertEqual(body.get("accepted"), False)
        self.assertEqual(self.arch.effects, ["op-ask-0001"])
        with self.assertRaises(GatewayError) as renew:
            self.arch.client.renew("zenbook", self.arch_lease["lease_id"],
                                   self.arch_lease["incarnation"], self.arch_lease["epoch"])
        self.assertIn(renew.exception.code, ("stale", "expired", "not_holder"))

    # --- reboot, partition, restart ---------------------------------------------

    def crash_arch(self):
        """Lose the relay and renewals without drain, release, fence, or handoff.

        shutdown() stops only the loopback stand-in, not a JARVIS service. Keep
        the socket so the return test can bring the same endpoint back online.
        """
        self.arch.server.shutdown()
        self.arch.thread.join(timeout=60)
        self.assertFalse(self.arch.thread.is_alive())
        self.assertTrue(self.dell.client.status()["lease_valid"])

    def background_ticks(self, seconds, *nodes, controllers=None):
        """Drive the production background worker with deterministic time.

        Hub.start and Agent.run currently start LeaseRenewer, not the optional
        RecoveryOrchestrator. Do not substitute owner.promote here: that would
        hide the missing automatic recovery that caused the incident. No owner
        private key is given to a node. Each candidate's canary checks the
        actual loopback listener, open executor and local control readiness.
        """
        workers = []
        for node in nodes:
            participant = Participant(node.client, node.runtime, guard=node.executor,
                                      canary=lambda node=node: self.node_canary(node))
            if controllers and node.name in controllers:
                participant.production_controller = controllers[node.name]
            workers.append(LeaseRenewer(participant))
        results = []
        for _ in range(seconds):
            self.clock.tick(1)
            results.append([worker.tick() for worker in workers])
        return results

    @staticmethod
    def node_canary(node):
        # These are the real loopback services of this isolated fixture, not
        # production Dell/Oracle readiness. A stopped serving thread must fail
        # even when shutdown() has left its listening socket open for failback.
        if not node.thread.is_alive() or not node.runtime.lease_snapshot():
            return False
        view = node.executor.local_view()
        if (view.get('journal') is not True or view.get('quarantined')
                or node.runtime.readiness('control').get('meets') is not True):
            return False
        try:
            with socket.create_connection(node.server.server_address, timeout=0.5):
                return True
        except OSError:
            return False

    def test_recovery_canary_refuses_a_stopped_candidate_listener(self):
        self.assertTrue(self.node_canary(self.arch))
        self.crash_arch()
        self.assertFalse(self.node_canary(self.arch))

    def assert_phone_served_by(self, device, key):
        # Separate checks expose both the authority/routing failure and its
        # phone consequence, rather than stopping at the first failed assertion.
        with self.subTest(stage="oracle-main"):
            self.assertEqual(self.dell.client.status()["main"], device)
        with self.subTest(stage="paired-session"):
            me = self.call("GET", "/phone/api/me")
            self.assertEqual(me.status, 200, me.body)
            self.assertEqual((me.body.get("main") or {}).get("id"), device, me.body)
        with self.subTest(stage="phone-answer"):
            answer = self.call("GET", "/phone/api/overview")
            self.assertEqual(answer.status, 200, answer.body)
            self.assertEqual(answer.body["main"], device, answer.body)
        with self.subTest(stage="phone-effect"):
            answer = self.ask(key)
            self.assertEqual(answer.status, 200, answer.body)
            self.assertEqual(self.fleet.nodes[device].effects, [key])

    def test_abrupt_arch_loss_automatically_promotes_dell_and_routes_phone(self):
        """Incident regression: no owner command and no cooperative Arch RPC."""
        self.dell.runtime.start()
        before = self.arch.client.renew_count()
        self.crash_arch()
        self.background_ticks(int(LEASE_SECONDS + SAFETY_MARGIN + 5), self.dell)
        self.assertEqual(self.arch.client.renew_count(), before)
        # This must turn green when the production worker can recover MAIN.
        # Do not mark expectedFailure: the requested availability is missing.
        self.assert_phone_served_by("dell", "op-crash-dell")
        self.assertEqual(self.arch.effects, [])

    def test_returning_arch_automatically_reclaims_main_and_routes_same_phone(self):
        """The returning preferred PC gets MAIN back with no owner command.

        The one manual recovery below is an explicit setup precondition, not
        evidence of automatic failover. Everything after Arch returns happens
        on the background tick: Dell (current MAIN) runs its bound SafeHandoff,
        so the brain moves Dell -> Arch and nodes are repointed before Arch
        serves the same phone.
        """
        self.crash_arch()
        self.clock.tick(LEASE_SECONDS + SAFETY_MARGIN + 1)
        self.dell.runtime.start()
        _, lease = self.fleet.promote("dell")
        self.dell.runtime.accept_lease(lease)
        self.assertEqual(self.call("GET", "/phone/api/overview").body["main"], "dell")

        self.arch.reboot()
        self.arch.runtime.invalidate_after_resume()
        self.arch.runtime.start()
        self.arch.thread = threading.Thread(target=self.arch.server.serve_forever, daemon=False)
        self.arch.thread.start()
        handoff = StandInHandoff(self, self.dell, self.arch, Path(self.tmp.name) / "dell-handoff.json")
        # Keep Dell alive while the returning preferred PC is observed for the
        # full 30-second stability window. Then the fence still waits out
        # Dell's last lease plus the safety margin (no full ack quorum: the
        # Oracle server participant does not tick here), so the handback
        # cannot finish before 30 + LEASE_SECONDS + SAFETY_MARGIN.
        returned_at = self.clock.wall()
        self.background_ticks(int(30 + LEASE_SECONDS + SAFETY_MARGIN + 5), self.dell, self.arch,
                              controllers={"dell": handoff})
        # The handoff started only after the full stability window.
        self.assertGreaterEqual(handoff.brain_at[0] - returned_at, 30)
        self.assertEqual(handoff.brain_calls, [("dell", "zenbook")])
        self.assertEqual(handoff.repoint_calls, [("dell", "zenbook")])
        self.assert_phone_served_by("zenbook", "op-return-arch")
        self.assertEqual(self.dell.effects, [])

    def test_returning_arch_stays_off_main_when_brain_transfer_fails(self):
        """A failed brain transfer never hands MAIN over: no memories are dropped."""
        self.crash_arch()
        self.clock.tick(LEASE_SECONDS + SAFETY_MARGIN + 1)
        self.dell.runtime.start()
        _, lease = self.fleet.promote("dell")
        self.dell.runtime.accept_lease(lease)
        self.arch.reboot()
        self.arch.runtime.invalidate_after_resume()
        self.arch.runtime.start()
        self.arch.thread = threading.Thread(target=self.arch.server.serve_forever, daemon=False)
        self.arch.thread.start()
        handoff = StandInHandoff(self, self.dell, self.arch, Path(self.tmp.name) / "dell-handoff.json")
        failed = []
        handoff._handoff._brain = lambda *args: failed.append(args[:2]) or {
            "ok": False, "status": "error", "error": "hook_failed"}
        # 30 s stability + fence wait, then the aborted grant's wait before
        # Dell (newest brain, given first claim) recovers MAIN for itself.
        self.background_ticks(int(30 + 2 * (LEASE_SECONDS + SAFETY_MARGIN) + 15), self.dell, self.arch,
                              controllers={"dell": handoff})
        self.assertEqual(failed, [("dell", "zenbook")])  # tried once, then backed off
        self.assertEqual(self.fleet.status()["main"], "dell")
        self.assertEqual(handoff.repoint_calls, [])
        self.assertEqual(self.arch.effects, [])

    def test_reboot_partition_and_restart_do_not_replay(self):
        self.assertEqual(self.ask("op-before-01").status, 200)
        _, dell_lease = self.sleep_arch_promote_dell()
        self.assertEqual(self.ask("op-after-001").status, 200)
        self.assertEqual(self.dell.effects, ["op-after-001"])
        old = self.fleet.status()

        self.dell.reboot()
        code, replay = self.dell.handle_relay(self.relay_body("op-after-001", "dell", "dell", attempt="a-other"))
        self.assertEqual(replay["accepted"], False)
        self.assertEqual(self.ask("op-after-001").status, 200)  # stored outcome, not a resend
        self.assertEqual(self.dell.effects, ["op-after-001"])
        self.assertEqual(self.ask("op-after-002").status, 200)
        self.assertEqual(self.dell.effects, ["op-after-001", "op-after-002"])

        self.dell.client.up = False
        cut = self.ask("op-partition")
        self.assertEqual(cut.status, 409)
        self.assertEqual(cut.body["code"], "refused")
        self.assertEqual(self.dell.effects, ["op-after-001", "op-after-002"])
        self.assertIn("partition", self.dell.refusals)
        self.dell.client.up = True

        # restart_gateway also re-enrols from its RAM list. That loop is not
        # what keeps the directory; the next test reopens the temp sqlite with
        # no enroll call. Neither restart is an Oracle process.
        restarted = self.fleet.restart_gateway()
        self.assertIsNone(restarted["main"])
        self.assertGreater(restarted["incarnation"], old["incarnation"])
        self.assertGreater(restarted["grant_barrier"], 0)
        me = self.call("GET", "/phone/api/me")
        self.assertEqual(me.status, 200, "in-process phone adapter, not the live :7787 listener")
        self.assertFalse(me.body["relay"])
        refused = self.ask("op-after-restart")
        self.assertEqual(refused.status, 503)
        self.assertEqual(refused.body["code"], "no_main")
        self.assertEqual(self.dell.effects, ["op-after-001", "op-after-002"])
        code, direct = self.dell.handle_relay(self.relay_body("op-direct-01", "dell", "dell"))
        self.assertEqual(direct["accepted"], False)

        ticket = {"admission_id": "adm-old", "target": "dell", "operation_id": "op-reborn",
                  "attempt_id": "a-0001", "digest": "sha256:" + "ab" * 32, "main": "dell",
                  "incarnation": old["incarnation"], "epoch": old["epoch"],
                  "issued_at": self.clock.wall() - 1, "admit_until": self.clock.wall() + 60,
                  "issuer_incarnation": old["incarnation"]}
        signature = self.dell.client.sign_ticket(ticket)
        with self.assertRaises(GatewayError) as stale:
            self.dell.client.consume(ticket, signature)
        self.assertIn(stale.exception.code, ("stale", "no_admission"))
        self.assertEqual(self.dell.effects, ["op-after-001", "op-after-002"])

    def test_directory_survives_reopen_without_reenroll(self):
        """Temp sqlite reload. Not restart_gateway's re-enrol loop and not Oracle.

        The gateway process is the same interpreter. `open` loads
        `gateway_directory`. Status from the already-enrolled keys must work
        with no further `enroll` call. MAIN is cleared because a new store
        incarnation voids the old lease.
        """
        before = self.arch.client.status()
        self.assertEqual(before["main"], "zenbook")
        enrolled = len(self.fleet._enrolled)
        self.assertGreaterEqual(enrolled, 4)
        previous = self.fleet.origin
        self.fleet.gateway.close()
        gateway = ContinuityGateway.open(
            self.fleet.root / "authority.sqlite", FLEET_ID, AUTHORITY_ID,
            [self.fleet.public("owner")],
            monotonic=self.clock.mono, wall=self.clock.wall,
            self_recovery=SELF_RECOVERY)
        self.fleet.gateway = gateway
        origin = gateway.serve()
        self.assertTrue(origin.startswith("http://127.0.0.1:"), origin)
        self.assertNotEqual(origin, previous)
        self.assertFalse(gateway.policy.auto_failover)
        self.fleet.origin = origin
        clients = [self.fleet.owner, self.fleet.oracle, self.fleet.phone_client]
        clients.extend(node.client for node in self.fleet.nodes.values())
        for client in clients:
            client.origin = origin
        self.assertEqual(len(self.fleet._enrolled), enrolled)
        for client, name in (
                (self.arch.client, "zenbook"),
                (self.dell.client, "dell"),
                (self.fleet.oracle, "oracle"),
                (self.fleet.phone_client, "ingress")):
            status = client.status()
            self.assertEqual(status["authority_id"], AUTHORITY_ID, name)
            self.assertIsNone(status["main"], name)
            self.assertGreater(status["incarnation"], before["incarnation"], name)
        self.assertEqual(len(self.fleet._enrolled), enrolled)

    def test_ambiguous_effect_is_never_sent_again(self):
        self.arch.server.shutdown()  # the relay target dies after the gateway picked it
        calls = []
        real = self.ingress._gateway.relay_transient

        def counting(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        self.ingress._gateway.relay_transient = counting
        first = self.ask("op-ambiguous-1")
        self.assertNotEqual(first.status, 200)
        self.assertEqual(calls, ["op-ambiguous-1"])
        second = self.ask("op-ambiguous-1")
        self.assertEqual(calls, ["op-ambiguous-1"], "an ambiguous effect was retried")
        self.assertIn(second.status, (409, 503))
        changed = self.ask("op-ambiguous-1", text="something else")
        self.assertEqual(changed.status, 409)
        self.assertEqual(changed.body["code"], "conflict")
        self.assertEqual(self.arch.effects, [])

    # --- canary and rollback ----------------------------------------------------

    def test_canary_failure_leaves_no_main_and_no_effect(self):
        self.sleep_arch_promote_dell_until_canary()
        status = self.fleet.status()
        self.assertIsNone(status["main"])
        self.assertEqual(status["mode"], "V2_ONLY_NO_MAIN")
        self.assertEqual(self.ask("op-canary-01").status, 503)
        for node in (self.arch, self.dell):
            code, body = node.handle_relay(self.relay_body("op-direct-" + node.name, node.name, node.name))
            self.assertEqual(body["accepted"], False)
            self.assertEqual(node.effects, [])
        with self.assertRaises(GatewayError):
            self.dell.client.renew("dell", self.dell_lease["lease_id"],
                                   self.dell_lease["incarnation"], self.dell_lease["epoch"])
        with self.assertRaises(GatewayError):
            self.arch.client.renew("zenbook", self.arch_lease["lease_id"],
                                   self.arch_lease["incarnation"], self.arch_lease["epoch"])

    def sleep_arch_promote_dell_until_canary(self):
        self.clock.tick(14)
        self.clock.tick(2 + SAFETY_MARGIN)
        self.dell.runtime.start()
        _, self.dell_lease = self.fleet.promote("dell", canary=False)

    # --- authentication gaps ----------------------------------------------------

    def test_auth_gaps_do_not_open_a_transition_or_extend_a_lease(self):
        before = self.fleet.status()
        calls = self.arch.client.renew_count()
        forged = AuthorityClient(
            self.fleet.origin, authority_id=AUTHORITY_ID, wall=self.clock.wall, device_id="zenbook",
            installation_id="zenbook-inst", private_key_path=self.fleet._keys["dell"][1])
        with self.assertRaises(GatewayError) as caught:
            forged.renew("zenbook", self.arch_lease["lease_id"], self.arch_lease["incarnation"],
                         self.arch_lease["epoch"])
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertEqual(self.arch.client.renew_count(), calls)
        self.assertEqual(self.fleet.status()["lease_remaining"], before["lease_remaining"])

        # a phone without the separate grant cannot even preview a MAIN change
        self.ingress.identify(hdr())
        principal = self.ingress.authenticate(self.cookie, self.csrf, PHONE_ORIGIN)
        request = {"action": "make_main_now", "candidate": "dell", "profile": "control"}
        with self.assertRaises(IngressError) as denied:
            self.ingress.manage_main_preview(principal, request)
        self.assertEqual(denied.exception.status, 403)
        self.assertIsNone(self.fleet.status()["transition"])

        # granted, but with no passkey verifier nothing may open
        owner = self.ingress.authenticate_owner("owner-token")
        me = self.call("GET", "/phone/api/me").body["phone"]["id"]
        self.ingress.grant_manage_main(owner, me)
        principal = self.ingress.authenticate(self.cookie, self.csrf, PHONE_ORIGIN)
        preview = self.ingress.manage_main_preview(principal, request)
        with self.assertRaises(IngressError) as nopass:
            self.ingress.manage_main_confirm(principal, preview["preview_id"], {"challenge": preview["challenge"]})
        self.assertEqual(nopass.exception.code, "passkey_unavailable")
        self.assertIsNone(self.fleet.status()["transition"])
        self.assertEqual(self.fleet.status()["main"], "zenbook")

        # a phone device key is a client, not a PC: it cannot renew or acknowledge a lease
        for call in (lambda: self.fleet.phone_client.renew("zenbook", self.arch_lease["lease_id"],
                                                          self.arch_lease["incarnation"], self.arch_lease["epoch"]),):
            with self.assertRaises(GatewayError):
                call()
        self.assertEqual(self.fleet.status()["lease_remaining"], before["lease_remaining"])

    # --- personal content -------------------------------------------------------

    def test_personal_content_never_reaches_a_store_or_log(self):
        self.assertEqual(self.ask("op-personal-1", text=SECRET).status, 200)
        self.assertEqual(self.arch.effects, ["op-personal-1"])
        blob = self.fleet.persisted_bytes()
        self.assertNotIn(SECRET.encode(), blob)
        self.assertNotIn(SECRET, json.dumps(self.fleet.gateway.audit_events()))
        for name in ("owner", "zenbook", "dell", "oracle", "ingress"):
            private = (self.fleet.root / "keys" / name).read_bytes()
            self.assertNotIn(private.strip(), blob)
        # personal key names are refused at the real gateway, not by a double
        for key in ("prompt", "secret", "text", "path", "url"):
            with self.assertRaises(GatewayError) as bad:
                self.ingress._gateway._device(
                    "relay", {"operation_id": "op-bad-0001", "target": "dell", "attempt_id": "a-0001",
                              "digest": "sha256:" + "cd" * 32, "capability": "client", key: SECRET})
            self.assertEqual(bad.exception.code, "bad_request", key)
        self.assertNotIn(SECRET.encode(), self.fleet.persisted_bytes())

    # --- the store, the executor, the margin ------------------------------------

    def test_second_writer_and_live_state_are_refused(self):
        with self.assertRaises(AuthorityError) as caught:
            AuthorityStore(self.fleet.root / "authority.sqlite", FLEET_ID, AUTHORITY_ID,
                           monotonic=self.clock.mono, wall=self.clock.wall)
        self.assertEqual(caught.exception.code, "writer_busy")
        live = Path(__file__).resolve().parents[2] / ".jarvis"
        with self.assertRaises(IntegrationError) as blocked:
            IsolatedFleet(live, self.clock)
        self.assertEqual(blocked.exception.code, "refuses_live_state")

    def test_default_executor_refuses_and_tampered_signatures_are_refused(self):
        status = self.fleet.status()
        wall = self.clock.wall()
        ticket = {"admission_id": "adm-tamper", "target": "zenbook", "operation_id": "op-tamper-1",
                  "attempt_id": "a-0001", "digest": "sha256:" + "ee" * 32, "main": "zenbook",
                  "incarnation": status["incarnation"], "epoch": status["epoch"], "issued_at": wall - 1,
                  "admit_until": wall + 60, "issuer_incarnation": status["incarnation"]}
        signed = {**ticket, "authority_id": AUTHORITY_ID, "fleet_id": FLEET_ID,
                  "mode_version": status["mode_version"], "policy_version": status["policy_version"],
                  "effect_class": "ordinary", "signature": self.dell.client.sign_ticket(ticket)}
        bare = ExecutorGuard(Path(self.tmp.name) / "bare.sqlite", device_id="dell", installation_id="dell-inst",
                             fleet_id=FLEET_ID, authority_id=AUTHORITY_ID)
        try:
            with self.assertRaises(AuthorityError) as closed:
                bare.accept(signed, ticket, lambda: None)
            self.assertEqual(closed.exception.code, "legacy")
            ran_bare = []
            self.assertEqual(bare.legacy_effect(lambda: ran_bare.append(1)), None)
            self.assertEqual(ran_bare, [1])
        finally:
            bare.close()
        ran = []
        with self.assertRaises(AuthorityError):
            self.arch.executor.accept(signed, ticket, lambda: ran.append(1))  # signed by Dell, not MAIN
        signed["signature"] = self.arch.client.sign_ticket(ticket)
        signed["digest"] = "sha256:" + "ff" * 32
        with self.assertRaises(AuthorityError):
            self.arch.executor.accept(signed, ticket, lambda: ran.append(2))
        self.assertEqual(ran, [])
        self.assertEqual(self.arch.effects, [])

    def test_dell_validates_online_and_safety_margin_is_the_placeholder(self):
        self.assertEqual(SAFETY_MARGIN, 2.0)
        self.sleep_arch_promote_dell()
        before = self.dell.client.calls.count("/v1/authority/status")
        self.dell.runtime.require_authority("ordinary", target="dell")
        self.dell.runtime.require_authority("ordinary", target="dell")
        self.assertEqual(self.dell.client.calls.count("/v1/authority/status") - before, 2)
        self.dell.client.up = False
        with self.assertRaises(RuntimeRefusal) as cut:
            self.dell.runtime.require_authority("ordinary", target="dell")
        self.assertEqual(cut.exception.code, "partition")

    # --- opt-in launch contract -------------------------------------------------

    def test_launch_contract_is_off_until_explicitly_closed(self):
        """RAM flags and `apply_node_seen`. This does not construct `NodeWorker`.

        The helper records after `run` returns, so an exception skips the
        record. `jarvis.cluster.NodeWorker` is the production mark and is not
        called here.
        """
        self.assertTrue(phone_listener_open())
        self.assertEqual(node_seen_mode(), "before")
        refuse_unfenced("remote")  # default: legacy keeps working, nothing raised
        close_unfenced("remote")
        try:
            with self.assertRaises(IntegrationError) as shut:
                refuse_unfenced("remote")
            self.assertEqual(shut.exception.code, "legacy_retained")
            refuse_unfenced("sleep")  # only the named surface closed
        finally:
            open_unfenced()
        refuse_unfenced("remote")
        seen, ran = [], []
        set_node_seen_mode("after")
        try:
            with self.assertRaises(RuntimeError):
                apply_node_seen(lambda: seen.append(1), lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            self.assertEqual(seen, [], "'after' recorded a job whose run failed")
        finally:
            set_node_seen_mode("before")

    def relay_body(self, op, target, main, attempt="a-0001", text="stale"):
        import base64
        import hashlib
        envelope = json.dumps({"text": text}).encode()
        status = self.fleet.status()
        return {"operation_id": op, "target": target, "attempt_id": attempt,
                "digest": "sha256:" + hashlib.sha256(envelope).hexdigest(), "capability": "client",
                "kind": "ask", "authority_id": AUTHORITY_ID, "incarnation": status["incarnation"],
                "epoch": status["epoch"], "main": main,
                "envelope_b64": base64.b64encode(envelope).decode("ascii")}


if __name__ == "__main__":
    unittest.main()
