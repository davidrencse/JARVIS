"""Isolated loopback of Arch sleep, Dell promotion, and replay refusal.

The temp store uses the real authority rules. Production ContinuityIntegration
stays legacy, and a passing test is not a live failover.
"""

import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from jarvis.continuity.authority import SAFETY_MARGIN, AuthorityError, AuthorityStore
from jarvis.continuity.integration import (
    CONTRACT_HTTP,
    EXTRA_HTTP,
    HTTP_STATUS,
    INCOMPATIBILITIES,
    ORIGIN,
    ContinuityIntegration,
    IntegrationError,
    IsolatedFleet,
    LocalExecutor,
    deployment_facts,
    http_status,
    peer_report,
)
from tests.continuity._fleet import Clock


SECRET = "SECRET-PROMPT-do-not-store"
POINTER = "POINTER-9-do-not-persist"


class ProductionBlocked(unittest.TestCase):
    def test_default_mode_refuses_cutover(self):
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
        self.assertIn("peer_gateway", ids)
        self.assertIn("peer_ingress", ids)
        self.assertIn("peer_runtime", ids)
        self.assertIn("peer_executor", ids)
        self.assertIn("gateway_host_unverified", ids)
        self.assertIn("dell_provider_unverified", ids)
        self.assertIn("luna_grok_unverified", ids)
        self.assertIn("signature_verifier_absent", ids)
        self.assertIn("passkey_verifier_absent", ids)
        self.assertIn("legacy_paths_unfenced", ids)
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

    def test_peers_missing_and_repo_has_no_oracle_gateway(self):
        peers = peer_report()
        for name in ("gateway", "ingress", "runtime", "executor"):
            self.assertFalse(peers[name]["present"])
            self.assertFalse(peers[name]["compatible"])
        facts = deployment_facts()
        self.assertTrue(facts["config_read"])
        self.assertFalse(facts["oracle_device"])
        self.assertNotIn("oracle", facts["device_ids"])
        self.assertEqual(facts["phone_bind"], "127.0.0.1:7787")
        self.assertFalse(facts["provider_verified"])
        self.assertFalse(facts["windows_clock_tested"])
        self.assertIn("zenbook", facts["device_ids"])
        self.assertIn("dell", facts["device_ids"])
        ids = {row["id"] for row in INCOMPATIBILITIES}
        self.assertIn("peers_absent", ids)
        self.assertIn("no_signatures", ids)
        self.assertIn("store_states", ids)
        self.assertIn("no_passkey", ids)

    def test_store_codes_fail_closed(self):
        source = Path("jarvis/continuity/authority.py").read_text(encoding="utf-8")
        codes = set(re.findall(r'AuthorityError\("([a-z_]+)"', source))
        self.assertTrue(codes)
        self.assertTrue(codes <= set(HTTP_STATUS))
        for code in codes:
            status, _ = http_status(code, 1.5)
            self.assertNotEqual(status, 200)
            self.assertIn(status, (400, 401, 403, 409, 410, 503))
        self.assertEqual(CONTRACT_HTTP["ticket_expired"], 410)
        self.assertEqual(CONTRACT_HTTP["not_quiet"], 409)
        self.assertEqual(http_status("not_quiet", 3)[1], 3)
        self.assertEqual(http_status("stale", 3)[1], None)
        self.assertEqual(EXTRA_HTTP["writer_busy"], 503)
        self.assertEqual(http_status("never_heard_of")[0], 503)


class LoopbackCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.clock = Clock()
        self.fleet = IsolatedFleet(self.tmp.name, self.clock)

    def tearDown(self):
        self.fleet.close()
        self.tmp.cleanup()

    def _pair(self):
        proof = self.fleet.ingress.pairing_code
        session = self.fleet.ingress.pair(proof, {"name": "phone"})
        principal = self.fleet.ingress.authenticate(session["cookie"], session["csrf"], ORIGIN)
        return session, principal

    def test_arch_sleep_promotes_dell_and_phone_session_survives(self):
        fleet = self.fleet
        booted = fleet.boot_zenbook()
        self.assertEqual(booted["not_ready"]["code"], "no_admission")
        self.assertEqual(booted["status"]["main"], "zenbook")
        self.assertTrue(booted["status"]["main_ready"])
        self.assertFalse(fleet.dispatch_flags()["both"])
        self.assertEqual(fleet.production.mode()["mode"], "LEGACY_ACTIVE")
        renews = fleet.gateway.renew_calls
        self.assertTrue(fleet.arch.require_authority("control")["ok"])
        self.assertEqual(fleet.gateway.renew_calls, renews)
        session, principal = self._pair()
        accepted = fleet.ingress.submit(principal, "op-accepted",
                                        {"target": "dell", "effect_class": "control"})
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["run_count"], 1)
        self.assertFalse(accepted.get("signature_verified", False))
        held = fleet.ingress.hold(principal, "op-held", {"target": "dell", "effect_class": "control"})
        self.assertEqual(held["state"], "held")
        noted = fleet.ingress.note_input(principal, POINTER)
        self.assertTrue(noted["accepted"])
        self.assertFalse(noted["persisted"])

        def during_sleep():
            return fleet.ingress.submit(principal, "op-while-asleep",
                                        {"target": "dell", "effect_class": "control"})

        report = fleet.sleep_promote_dell(during_sleep)
        self.assertEqual(report["early_local"]["code"], "admission_stopped")
        self.assertGreater(report["cache_while_asleep"], 0)
        self.assertEqual(report["early"]["code"], "main_alive")
        self.assertEqual(report["still_alive"]["code"], "main_alive")
        self.assertEqual(report["waited"]["code"], "not_quiet")
        self.assertGreater(report["waited"]["retry_after"], 0)
        self.assertEqual(report["not_ready"]["code"], "no_admission")
        self.assertEqual(report["during_sleep"]["code"], "admission_stopped")
        self.assertEqual(fleet.dell_exec.runs("op-while-asleep"), 0)
        self.assertEqual(report["status"]["main"], "dell")
        self.assertTrue(report["status"]["main_ready"])
        self.assertGreater(report["status"]["epoch"], report["old_lease"]["epoch"])
        self.assertEqual(report["dell_cached_seconds"], 0)
        self.assertEqual(fleet.dell_exec.runs("op-before-canary"), 0)
        again = fleet.ingress.authenticate(session["cookie"], session["csrf"], ORIGIN)
        self.assertEqual(again["session_id"], session["session_id"])
        self.assertFalse(again["manage_main"])
        pinned = fleet.ingress.submit(again, "op-held", {"target": "dell", "effect_class": "control"})
        self.assertEqual(pinned["state"], "held")
        self.assertEqual(fleet.dell_exec.runs("op-held"), 0)
        queried = fleet.ingress.operation(again, "op-accepted")
        self.assertEqual(queried["run_count"], 1)
        self.assertEqual(fleet.dell_exec.runs("op-accepted"), 1)
        continued = fleet.ingress.submit(again, "op-after", {"target": "dell", "effect_class": "control"})
        self.assertEqual(continued["state"], "accepted")
        self.assertEqual(continued["run_count"], 1)
        ai = fleet.ingress.submit(again, "op-ai", {"target": "dell", "effect_class": "ai"})
        self.assertEqual(ai["code"], "ai_not_ready")
        self.assertEqual(fleet.dell_exec.runs("op-ai"), 0)
        self.assertFalse(fleet.dell.readiness("ai")["ai_ready"])
        self.assertIsNone(fleet.dell.readiness("ai")["provider"])
        stopped = fleet.ingress.disconnect(session["cookie"])
        self.assertEqual(stopped["input_open"], False)
        self.assertFalse(fleet.ingress.note_input(again, POINTER)["accepted"])
        fleet.arch.admitting = True
        fleet.arch.lease = report["old_lease"]
        stale_local = fleet.arch.require_authority("control")
        self.assertFalse(stale_local["ok"])
        self.assertEqual(stale_local["code"], "stale")
        stale_renew = fleet.capture(lambda: fleet.gateway.renew(
            fleet.identities["zenbook"], report["old_lease"]))
        self.assertEqual(stale_renew["code"], "stale")
        stale_effect = fleet.capture(lambda: fleet.arch_exec.accept(
            fleet.identities["zenbook"], {}, fleet.ticket(
                "zenbook", "op-stale-arch", "zenbook", report["old_lease"])))
        self.assertEqual(stale_effect["code"], "stale")
        self.assertEqual(fleet.arch_exec.runs("op-stale-arch"), 0)
        self.assertFalse(fleet.production.v2_request(again, "op-live", {"target": "dell"})["ok"])
        self.assertFalse(fleet.production.activate_v2(report["status"], True)["applied"])
        self.assertFalse(fleet.dispatch_flags()["both"])
        self.assertNotIn(SECRET.encode(), fleet.persisted_bytes())
        self.assertNotIn(POINTER.encode(), fleet.persisted_bytes())

    def test_reboot_partition_and_restart_do_not_replay(self):
        fleet = self.fleet
        fleet.boot_zenbook()
        session, principal = self._pair()
        fleet.ingress.submit(principal, "op-accepted", {"target": "dell", "effect_class": "control"})
        report = fleet.sleep_promote_dell()
        principal = fleet.ingress.authenticate(session["cookie"], session["csrf"], ORIGIN)
        fleet.ingress.submit(principal, "op-after", {"target": "dell", "effect_class": "control"})
        crashed = fleet.dell_exec.accept(
            fleet.identities["dell"], {"crash": True},
            fleet.ticket("dell", "op-crash", "dell", fleet.dell.lease))
        self.assertEqual(crashed["state"], "uncertain")
        self.assertEqual(crashed["run_count"], 0)
        again = fleet.dell_exec.accept(
            fleet.identities["dell"], {}, fleet.ticket("dell", "op-crash", "dell", fleet.dell.lease))
        self.assertEqual(again["run_count"], 0)
        prior = fleet.dell_exec.reconcile("op-after", "a-1")
        fleet.reboot_executor("dell")
        fleet.gateway.up = False
        replay_ticket = {"target": "dell", "operation_id": "op-after", "attempt_id": "a-1",
                         "admission_id": prior["admission_id"], "digest": prior["digest"],
                         "incarnation": prior["incarnation"], "epoch": prior["epoch"], "main": "dell"}
        replayed = fleet.dell_exec.accept(fleet.identities["dell"], {}, replay_ticket)
        self.assertEqual(replayed["state"], "accepted")
        self.assertEqual(fleet.dell_exec.runs("op-after"), 1)
        old = fleet.capture(lambda: fleet.dell_exec.accept(
            fleet.identities["zenbook"], {}, fleet.ticket(
                "zenbook", "op-old-offline", "dell", report["old_lease"])))
        self.assertEqual(old["code"], "stale")
        fresh = fleet.capture(lambda: fleet.dell_exec.accept(
            fleet.identities["dell"], {}, fleet.ticket("dell", "op-offline-new", "dell", fleet.dell.lease)))
        self.assertEqual(fresh["code"], "authority_unavailable")
        self.assertEqual(fleet.dell_exec.runs("op-offline-new"), 0)
        self.assertEqual(fleet.dell_exec.runs("op-old-offline"), 0)
        blocked = fleet.ingress.submit(principal, "op-partition",
                                       {"target": "dell", "effect_class": "control"})
        self.assertEqual(blocked["code"], "authority_unavailable")
        quiet = fleet.ingress.status(principal)
        self.assertFalse(quiet["commands"])
        self.assertEqual(quiet["code"], "authority_unavailable")
        fleet.gateway.up = True
        restarted = fleet.restart_authority()
        self.assertIsNone(restarted["main"])
        self.assertGreater(restarted["grant_barrier"], 0)
        self.assertGreater(restarted["incarnation"], report["status"]["incarnation"])
        survived = fleet.ingress.authenticate(session["cookie"], session["csrf"], ORIGIN)
        self.assertEqual(survived["session_id"], session["session_id"])
        refused = fleet.ingress.submit(survived, "op-after-restart",
                                       {"target": "dell", "effect_class": "control"})
        self.assertEqual(refused["code"], "no_admission")
        self.assertEqual(fleet.dell_exec.runs("op-after"), 1)
        self.assertEqual(fleet.dell_exec.runs("op-after-restart"), 0)
        stale_ticket = fleet.capture(lambda: fleet.gateway.consume(
            fleet.identities["dell"], fleet.ticket("dell", "op-reborn", "dell", {
                "incarnation": prior["incarnation"], "epoch": prior["epoch"]})))
        self.assertEqual(stale_ticket["code"], "stale")
        self.assertEqual(fleet.dell_exec.runs("op-reborn"), 0)
        self.assertNotIn(POINTER.encode(), fleet.persisted_bytes())

    def test_canary_failure_ai_profile_and_rollback_leave_one_or_none(self):
        fleet = self.fleet
        fleet.boot_zenbook()
        self.assertFalse(fleet.dispatch_flags()["both"])
        opened = fleet._request("dell", "ai-1", "ai")
        fleet.gateway.transition_readiness(fleet.identities["dell"], opened["transition_id"], {
            "control_ready": True, "ai_ready": False, "checkpoint": "cp-1"})
        refused = fleet.capture(lambda: fleet.gateway.transition_step(
            fleet.identities["owner-pc"], opened["transition_id"], "drain"))
        self.assertEqual(refused["code"], "not_ready")
        fleet.gateway.transition_step(fleet.identities["owner-pc"], opened["transition_id"], "cancel")
        self.assertEqual(fleet.store.status()["main"], "zenbook")
        moved = fleet._request("dell", "handover-1", "control")
        fleet.prepare_candidate(moved["transition_id"], "dell", control=True, ai=False)
        fleet.ack_awake()
        fenced = fleet.try_fence(moved["transition_id"])
        self.assertTrue(fenced["ok"])
        lease = fleet.grant_candidate(moved["transition_id"], "dell")
        fleet.finish(moved["transition_id"], "dell", False)
        status = fleet.store.status()
        self.assertIsNone(status["main"])
        self.assertEqual(status["mode"], "V2_ONLY_NO_MAIN")
        self.assertFalse(fleet.dispatch_flags()["legacy"])
        self.assertFalse(fleet.dispatch_flags()["v2"])
        self.assertFalse(fleet.dispatch_flags()["both"])
        stale = fleet.capture(lambda: fleet.gateway.renew(fleet.identities["zenbook"], fleet.arch.lease))
        self.assertEqual(stale["code"], "stale")
        ghost = fleet.capture(lambda: fleet.gateway.renew(fleet.identities["dell"], lease))
        self.assertIn(ghost["code"], ("stale", "expired"))
        with self.assertRaises(AuthorityError) as caught:
            fleet.store.set_mode("ROLLBACK_QUIESCED", status["mode_version"])
        self.assertEqual(caught.exception.code, "bad_edge")
        self.assertFalse(fleet.production.rollback_quiesced("drill", status["mode_version"])["applied"])
        self.assertEqual(fleet.production.mode()["mode"], "LEGACY_ACTIVE")

    def test_auth_gaps_do_not_open_a_transition_or_extend_a_lease(self):
        fleet = self.fleet
        fleet.boot_zenbook()
        before = fleet.store.status()["lease_remaining"]
        calls = fleet.gateway.renew_calls
        bad = dict(fleet.identities["zenbook"])
        bad["secret"] = "0" * len(bad["secret"])
        refused = fleet.capture(lambda: fleet.gateway.renew(bad, fleet.arch.lease))
        self.assertEqual(refused["http"], 401)
        self.assertEqual(fleet.gateway.renew_calls, calls)
        self.assertEqual(fleet.store.status()["lease_remaining"], before)
        session, principal = self._pair()
        with self.assertRaises(IntegrationError):
            fleet.ingress.authenticate(session["cookie"], "csrf-wrong", ORIGIN)
        owner = {"manage_main": True, "role": "owner"}
        view = fleet.store.status()
        request = {"action": "make_main_now", "candidate": "dell", "profile": "control", "client_key": "admin-1",
                   "expected_incarnation": view["incarnation"], "expected_epoch": view["epoch"],
                   "expected_policy": view["policy_version"]}
        preview = fleet.ingress.manage_main_preview(owner, request)
        self.assertFalse(preview["mutates"])
        self.assertFalse(preview["passkey_verifier"])
        self.assertIsNone(fleet.store.status()["transition"])
        with self.assertRaises(IntegrationError) as phone_admin:
            fleet.ingress.manage_main_confirm(principal, preview["preview_id"], preview["challenge"])
        self.assertEqual(phone_admin.exception.code, "not_manage_main")
        self.assertIsNone(fleet.store.status()["transition"])
        with self.assertRaises(IntegrationError):
            fleet.ingress.manage_main_confirm(owner, preview["preview_id"], "ch-wrong")
        self.assertIsNone(fleet.store.status()["transition"])
        first = fleet.ingress.manage_main_confirm(owner, preview["preview_id"], preview["challenge"])
        second = fleet.ingress.manage_main_confirm(owner, preview["preview_id"], preview["challenge"])
        self.assertEqual(first["transition_id"], second["transition_id"])
        self.assertEqual(fleet.store.status()["transition"]["transition_id"], first["transition_id"])
        fleet.gateway.transition_step(fleet.identities["owner-pc"], first["transition_id"], "cancel")
        epoch = fleet.store.status()["epoch"]
        with self.assertRaises(IntegrationError) as step:
            fleet.gateway.transition_step(fleet.identities["owner-pc"], first["transition_id"], "catching_up")
        self.assertEqual(step.exception.code, "bad_request")
        self.assertEqual(fleet.store.status()["epoch"], epoch)
        ineligible = fleet.try_request("oracle", "oracle-main")
        self.assertEqual(ineligible["code"], "ineligible")

    def test_personal_fields_never_reach_the_store(self):
        fleet = self.fleet
        fleet.boot_zenbook()
        _, principal = self._pair()
        with self.assertRaises(IntegrationError) as caught:
            fleet.ingress.submit(principal, "op-secret",
                                 {"target": "dell", "effect_class": "control", "prompt": SECRET})
        self.assertEqual(caught.exception.code, "personal_field")
        blob = fleet.persisted_bytes()
        self.assertNotIn(SECRET.encode(), blob)
        self.assertNotIn(b"prompt", blob)
        text = json.dumps(fleet.gateway.log)
        self.assertNotIn(SECRET, text)
        for secret in fleet.secrets.values():
            self.assertNotIn(secret.encode(), blob)

    def test_second_writer_and_live_state_are_refused(self):
        with self.assertRaises(AuthorityError) as caught:
            AuthorityStore(self.fleet.db_path, "home", "oracle-auth", monotonic=self.clock.mono,
                           wall=self.clock.wall)
        self.assertEqual(caught.exception.code, "writer_busy")
        live = Path(__file__).resolve().parents[2] / ".jarvis"
        with self.assertRaises(IntegrationError) as blocked:
            IsolatedFleet(live, self.clock)
        self.assertEqual(blocked.exception.code, "refuses_live_state")

    def test_default_executor_refuses_without_a_signature_verifier(self):
        guard = LocalExecutor(Path(self.tmp.name) / "strict.sqlite", self.fleet.gateway, "dell")
        try:
            with self.assertRaises(IntegrationError) as caught:
                guard.accept(self.fleet.identities["dell"], {}, {
                    "target": "dell", "operation_id": "op-sig", "attempt_id": "a-1",
                    "admission_id": "adm-op-sig", "digest": "sha256:" + "ab" * 32,
                    "main": "dell", "incarnation": 1, "epoch": 1})
            self.assertEqual(caught.exception.code, "signature_unverified")
            self.assertEqual(guard.runs("op-sig"), 0)
        finally:
            guard.close()

    def test_dell_validates_online_and_safety_margin_is_the_placeholder(self):
        self.assertEqual(SAFETY_MARGIN, 2.0)
        fleet = self.fleet
        fleet.boot_zenbook()
        fleet.sleep_promote_dell()
        calls = fleet.gateway.renew_calls
        first = fleet.dell.require_authority("control")
        second = fleet.dell.require_authority("control")
        self.assertTrue(first["ok"])
        self.assertEqual(first["source"], "online")
        self.assertTrue(second["ok"])
        self.assertEqual(fleet.gateway.renew_calls, calls + 2)
        self.assertEqual(fleet.dell.cache.remaining("dell"), 0.0)
        self.assertTrue(fleet.dell.online_only)


if __name__ == "__main__":
    unittest.main()
