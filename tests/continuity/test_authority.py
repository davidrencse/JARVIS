"""MAIN authority prototype: every test is a failure the design says must not happen."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity import authority as A
from jarvis.continuity.authority import AuthorityError, AuthorityStore

from tests.continuity._fleet import Clock, ack, cutover, digest, open_store, promote, ticket, transition


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "authority.sqlite"
        self.clock = Clock()
        self.store = open_store(self.db, self.clock)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.store.close())

    def refuse(self, code, fn, *args, **kw):
        with self.assertRaises(AuthorityError) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        return cm.exception

    def restart(self, authority_id="oracle-auth"):
        self.store.close()
        self.store = open_store(self.db, self.clock, authority_id)
        return self.store

    def active(self, main="zenbook"):
        cutover(self.store)
        return promote(self.store, "recover", main, "k-boot")[1]


class Cutover(Base):
    def test_fresh_store_is_legacy_and_grants_nothing(self):
        st = self.store.status()
        self.assertEqual((st["mode"], st["main"], st["epoch"], st["incarnation"]), ("LEGACY_ACTIVE", None, 0, 1))
        self.refuse("mode", transition, self.store, "recover", "dell", "k1")

    def test_v2_needs_every_eligible_participant_to_ack(self):
        for device, kind in (("zenbook", "pc"), ("dell", "pc")):
            self.store.register_participant(device, device + "-inst", kind)
        v = self.store.set_mode("PREPARING_V2", 1)
        v = self.store.set_mode("QUIESCED", v)
        self.store.acknowledge("zenbook", "zenbook-inst", v + 1, 0)
        err = self.refuse("unacked", self.store.set_mode, "V2_ONLY_NO_MAIN", v)
        self.assertIn("dell", str(err))
        self.store.acknowledge("dell", "dell-inst", v + 1, 0)
        self.store.set_mode("V2_ONLY_NO_MAIN", v)

    def test_no_shortcut_edges_and_stale_versions(self):
        self.refuse("bad_edge", self.store.set_mode, "V2_ACTIVE", 1)
        self.refuse("bad_edge", self.store.set_mode, "V2_ONLY_NO_MAIN", 1)
        self.refuse("stale", self.store.set_mode, "PREPARING_V2", 7)

    def test_ack_cannot_claim_unissued_versions_or_foreign_installation(self):
        self.store.register_participant("dell", "dell-inst", "pc")
        self.refuse("ahead", self.store.acknowledge, "dell", "dell-inst", 9, 0)
        self.refuse("ahead", self.store.acknowledge, "dell", "dell-inst", 1, 5)
        self.refuse("unknown_participant", self.store.acknowledge, "dell", "cloned-inst", 1, 0)

    def test_reinstall_resets_acks(self):
        cutover(self.store)
        self.store.register_participant("dell", "dell-reinstalled", "pc")
        self.refuse("ineligible", transition, self.store, "recover", "dell", "k1")


class SingleMain(Base):
    def test_second_writer_on_same_file_is_refused(self):
        self.refuse("writer_busy", open_store, self.db, self.clock)

    def test_first_main_needs_recovery_and_a_canary(self):
        cutover(self.store)
        tid = transition(self.store, "recover", "zenbook", "k1")
        self.refuse("not_ready", self.store.drain, tid)  # no readiness yet
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        self.store.fence(tid)
        lease = self.store.grant(tid)
        st = self.store.status()
        self.assertFalse(st["main_ready"])
        self.refuse("no_admission", self.store.consume, **ticket(self.store, lease, self.clock))
        self.store.complete(tid, True)
        st = self.store.status()
        self.assertEqual((st["main"], st["mode"], st["main_ready"]), ("zenbook", "V2_ACTIVE", True))

    def test_recover_refused_while_main_alive(self):
        self.active()
        self.refuse("main_alive", transition, self.store, "recover", "dell", "k2")

    def test_only_one_open_transition(self):
        self.active()
        transition(self.store, "make_main_now", "dell", "k2")
        self.refuse("busy", transition, self.store, "make_main_now", "dell", "k3")

    def test_server_is_never_a_candidate(self):
        self.active()
        self.refuse("ineligible", transition, self.store, "make_main_now", "oracle", "k2")
        self.refuse("ineligible", transition, self.store, "make_main_now", "ghost", "k3")

    def test_request_is_idempotent_per_client_key(self):
        self.active()
        st = self.store.status()
        a = self.store.request("phone-1", "k2", "make_main_now", "dell", st["incarnation"], st["epoch"], 1)
        b = self.store.request("phone-1", "k2", "make_main_now", "dell", st["incarnation"], st["epoch"], 1)
        self.assertEqual(a["transition_id"], b["transition_id"])
        self.refuse("key_reused", self.store.request, "phone-1", "k2", "set_preferred_main", "dell",
                    st["incarnation"], st["epoch"], 1)

    def test_request_needs_current_tuple(self):
        self.active()
        st = self.store.status()
        self.refuse("stale", self.store.request, "phone-1", "k2", "make_main_now", "dell",
                    st["incarnation"], st["epoch"] - 1, 1)


class Lease(Base):
    def test_lapsed_lease_is_never_revived(self):
        lease = self.active()
        args = (lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.store.renew(*args)
        self.clock.tick(A.LEASE_SECONDS + 0.01)
        self.refuse("expired", self.store.renew, *args)
        self.assertIsNone(self.store.status()["main"])

    def test_wall_clock_jumps_do_not_extend_or_cut_a_lease(self):
        lease = self.active()
        self.clock.w += 10 * 3600  # NTP jump forward
        self.assertTrue(self.store.status()["lease_valid"])
        self.clock.w -= 20 * 3600
        self.clock.m += A.LEASE_SECONDS + 1
        self.assertFalse(self.store.status()["lease_valid"])
        self.refuse("expired", self.store.renew, lease["holder"], lease["lease_id"], lease["incarnation"],
                    lease["epoch"])

    def test_wrong_tuple_cannot_renew(self):
        lease = self.active()
        self.refuse("stale", self.store.renew, "dell", lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.refuse("stale", self.store.renew, lease["holder"], lease["lease_id"], lease["incarnation"],
                    lease["epoch"] - 1)

    def test_drain_stops_renewals_and_admission(self):
        lease = self.active()
        tid = transition(self.store, "make_main_now", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        self.refuse("draining", self.store.renew, lease["holder"], lease["lease_id"], lease["incarnation"],
                    lease["epoch"])
        self.refuse("no_admission", self.store.consume, **ticket(self.store, lease, self.clock))


class SleepRecovery(Base):
    """User report: Arch (zenbook) sleeps; Dell must become MAIN without two MAINs."""

    def test_sleeping_main_cannot_be_replaced_until_its_validity_has_drained(self):
        lease = self.active()
        self.store.renew(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.clock.tick(A.LEASE_SECONDS - 5)
        self.refuse("main_alive", transition, self.store, "recover", "dell", "k2")
        self.clock.tick(6)  # lease lapsed: Arch stopped renewing (asleep)
        tid = transition(self.store, "recover", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        # Dell and Oracle acked, the sleeping zenbook cannot: wait out validity + margin.
        ack(self.store, ("dell", "oracle"))
        err = self.refuse("not_quiet", self.store.fence, tid)
        self.assertGreater(err.wait, 0)
        self.clock.tick(err.wait + 0.01)
        self.store.fence(tid)
        new = self.store.grant(tid)
        self.store.complete(tid, True)
        st = self.store.status()
        self.assertEqual((st["main"], st["main_ready"]), ("dell", True))
        self.assertGreater(new["epoch"], lease["epoch"])
        # Arch wakes up and tries its old tuple: refused everywhere.
        self.refuse("stale", self.store.renew, lease["holder"], lease["lease_id"], lease["incarnation"],
                    lease["epoch"])
        self.refuse("stale", self.store.consume, **ticket(self.store, lease, self.clock, op="op-arch"))

    def test_planned_release_before_suspend(self):
        lease = self.active()
        self.store.release(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        st = self.store.status()
        self.assertEqual((st["mode"], st["main"], st["last_main"]), ("V2_ONLY_NO_MAIN", None, "zenbook"))
        tid = transition(self.store, "recover", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        self.refuse("not_quiet", self.store.fence, tid)  # zenbook's grant may still be honoured
        ack(self.store)  # zenbook acked before sleeping, plus everyone else
        self.store.fence(tid)
        self.store.grant(tid)
        self.store.complete(tid, True)
        self.assertEqual(self.store.status()["main"], "dell")


class Handover(Base):
    def test_acks_allow_handover_before_lease_expiry(self):
        lease = self.active()
        tid = transition(self.store, "make_main_now", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        ack(self.store, ("dell", "oracle"))
        self.refuse("lease_live", self.store.fence, tid)  # zenbook has not acked
        ack(self.store, ("zenbook",))
        self.store.fence(tid)
        self.assertIsNone(self.store.status()["main"])
        new = self.store.grant(tid)
        self.store.complete(tid, True)
        self.assertEqual((self.store.status()["main"], new["epoch"]), ("dell", lease["epoch"] + 1))

    def test_ai_profile_needs_ai_readiness(self):
        self.active()
        tid = transition(self.store, "make_main_now", "dell", "k2", profile="ai")
        self.store.readiness(tid, True, False)
        self.refuse("not_ready", self.store.drain, tid)

    def test_cancel_before_fence_keeps_old_main(self):
        lease = self.active()
        tid = transition(self.store, "make_main_now", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        self.store.cancel(tid)
        self.store.renew(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.assertEqual(self.store.status()["mode"], "V2_ACTIVE")

    def test_cancel_after_targets_acked_leaves_no_main(self):
        lease = self.active()
        tid = transition(self.store, "make_main_now", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        ack(self.store)  # fence quorum: every eligible participant and the previous holder
        t = self.store.cancel(tid)
        self.assertEqual((t["state"], self.store.status()["main"]), ("FAILED", None))
        self.refuse("stale", self.store.renew, lease["holder"], lease["lease_id"], lease["incarnation"],
                    lease["epoch"])

    def test_failure_after_fence_never_resurrects_old_main(self):
        lease = self.active()
        tid = transition(self.store, "make_main_now", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        ack(self.store)
        self.store.fence(tid)
        new = self.store.grant(tid)
        t = self.store.complete(tid, False)
        st = self.store.status()
        self.assertEqual((t["outcome"], st["main"], st["mode"]), ("canary_failed", None, "V2_ONLY_NO_MAIN"))
        self.assertGreater(st["epoch"], new["epoch"])
        for old in (lease, new):
            self.refuse("stale", self.store.renew, old["holder"], old["lease_id"], old["incarnation"], old["epoch"])

    def test_candidate_lease_lapsing_before_canary_fails_closed(self):
        self.active()
        tid = transition(self.store, "make_main_now", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        ack(self.store)
        self.store.fence(tid)
        self.store.grant(tid)
        self.clock.tick(A.LEASE_SECONDS + 1)
        t = self.store.complete(tid, True)
        self.assertEqual((t["outcome"], self.store.status()["main"]), ("lease_lapsed", None))

    def test_set_preferred_main_records_preference(self):
        self.active()
        ack(self.store)
        promote(self.store, "set_preferred_main", "dell", "k2")
        self.assertEqual(self.store.status()["preferred"], "dell")


class Restart(Base):
    def test_restart_voids_every_lease_and_waits_before_granting(self):
        lease = self.active()
        self.restart()
        st = self.store.status()
        self.assertEqual((st["mode"], st["main"], st["incarnation"]), ("V2_ONLY_NO_MAIN", None, 2))
        self.assertGreater(st["grant_barrier"], A.LEASE_SECONDS)
        self.refuse("stale", self.store.renew, lease["holder"], lease["lease_id"], lease["incarnation"],
                    lease["epoch"])
        ack(self.store)  # acks never shortcut the incarnation barrier
        tid = transition(self.store, "recover", "dell", "k2")
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        err = self.refuse("not_quiet", self.store.fence, tid)
        self.clock.tick(err.wait + 0.01)
        self.store.fence(tid)
        self.store.grant(tid)

    def test_restart_fails_open_transitions(self):
        self.active()
        tid = transition(self.store, "make_main_now", "dell", "k2")
        self.restart()
        t = self.store.transition(tid)
        self.assertEqual((t["state"], t["outcome"]), ("FAILED", "authority_restarted"))

    def test_ticket_from_previous_incarnation_is_refused(self):
        lease = self.active()
        old = ticket(self.store, lease, self.clock, op="op-old")
        self.restart()
        self.clock.tick(A.LEASE_SECONDS + A.SAFETY_MARGIN + 1)
        ack(self.store)
        _, new = promote(self.store, "recover", "dell", "k2")
        old.update(main=new["holder"], incarnation=new["incarnation"], epoch=new["epoch"], target="zenbook")
        self.refuse("stale", self.store.consume, **old)

    def test_foreign_authority_id_quarantines(self):
        self.active()
        self.restart("someone-else")
        self.assertEqual(self.store.status()["mode"], "QUARANTINED")
        self.refuse("quarantined", transition, self.store, "recover", "dell", "k2")

    def test_unknown_schema_quarantines(self):
        self.active()
        self.store.close()
        con = sqlite3.connect(self.db)
        con.execute("UPDATE meta SET value='99' WHERE key='schema'")
        con.commit()
        con.close()
        self.store = open_store(self.db, self.clock)
        self.refuse("quarantined", self.store.register_participant, "x", "x-inst", "pc")


class Admission(Base):
    def setUp(self):
        super().setUp()
        self.lease = self.active()

    def test_consumed_exactly_once(self):
        t = ticket(self.store, self.lease, self.clock)
        first = self.store.consume(**t)
        again = self.store.consume(**t)
        self.assertFalse(first["already"])
        self.assertTrue(again["already"])
        self.assertEqual(first["consumed_at"], again["consumed_at"])

    def test_same_attempt_different_payload_or_admission_is_conflict(self):
        t = ticket(self.store, self.lease, self.clock)
        self.store.consume(**t)
        self.refuse("conflict", self.store.consume, **dict(t, digest=digest("rm -rf")))
        self.refuse("conflict", self.store.consume, **dict(t, admission_id="adm-other"))

    def test_new_attempt_is_new_work(self):
        self.store.consume(**ticket(self.store, self.lease, self.clock))
        rec = self.store.consume(**ticket(self.store, self.lease, self.clock, attempt="a-2"))
        self.assertFalse(rec["already"])

    def test_retry_after_handover_reconciles_instead_of_replaying(self):
        t = ticket(self.store, self.lease, self.clock)
        self.store.consume(**t)
        ack(self.store)
        promote(self.store, "make_main_now", "dell", "k2")
        rec = self.store.consume(**t)
        self.assertTrue(rec["already"])
        self.assertEqual(rec["epoch"], self.lease["epoch"])
        # ...but a fresh operation under the old tuple is new work and refused.
        self.refuse("stale", self.store.consume, **ticket(self.store, self.lease, self.clock, op="op-2"))

    def test_expired_ticket_refused_before_lookup(self):
        t = ticket(self.store, self.lease, self.clock)
        self.store.consume(**t)
        self.clock.w = t["admit_until"] + 1
        self.refuse("ticket_expired", self.store.consume, **t)
        self.assertIsNotNone(self.store.admission(t["admission_id"]))  # reconciliation still answers

    def test_ticket_lifetime_bounds(self):
        t = ticket(self.store, self.lease, self.clock)
        self.refuse("bad_ticket", self.store.consume, **dict(t, admit_until=t["issued_at"] + A.MAX_TICKET_SECONDS + 1))
        self.refuse("bad_ticket", self.store.consume, **dict(t, issued_at=self.clock.w + 60,
                                                             admit_until=self.clock.w + 120))

    def test_target_must_have_acked_current_mode(self):
        self.store.register_participant("newbox", "newbox-inst", "pc")
        self.refuse("target_unfenced", self.store.consume,
                    **ticket(self.store, self.lease, self.clock, target="newbox"))

    def test_lapsed_main_admits_nothing(self):
        self.clock.tick(A.LEASE_SECONDS + 1)
        self.refuse("no_admission", self.store.consume, **ticket(self.store, self.lease, self.clock))

    def test_lookup_never_consumes(self):
        self.assertIsNone(self.store.admission("adm-op-1-a-1"))
        self.assertIsNone(self.store.admission("adm-op-1-a-1"))


class MetadataOnly(Base):
    """Oracle holds ids, digests and numbers: no prompt or body can get in."""

    def test_free_text_is_refused_everywhere(self):
        lease = self.active()
        t = ticket(self.store, lease, self.clock)
        self.refuse("bad_request", self.store.consume, **dict(t, operation_id="summarise my diary entry"))
        self.refuse("bad_request", self.store.consume, **dict(t, digest="open firefox"))
        tid = transition(self.store, "make_main_now", "dell", "k2")
        self.refuse("bad_request", self.store.readiness, tid, True, True, "checkpoint with my notes")
        self.refuse("bad_request", self.store.register_participant, "x" * 200, "i", "pc")

    def test_schema_has_no_body_columns(self):
        con = sqlite3.connect(self.db)
        cols = {r[1] for t in ("authority", "participants", "transitions", "admissions")
                for r in con.execute(f"PRAGMA table_info({t})")}
        con.close()
        for banned in ("prompt", "body", "payload", "result", "text", "content", "memory", "args"):
            self.assertFalse([c for c in cols if banned in c], banned)

    def test_store_runs_wal_full(self):
        con = self.store._db
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 2)


class Rollback(Base):
    def test_rollback_waits_for_v2_grants_then_needs_acks(self):
        lease = self.active()
        self.store.release(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        v = self.store.status()["mode_version"]
        self.refuse("not_quiet", self.store.set_mode, "ROLLBACK_QUIESCED", v)
        self.clock.tick(A.LEASE_SECONDS + A.SAFETY_MARGIN + 1)
        v = self.store.set_mode("ROLLBACK_QUIESCED", v)
        self.refuse("unacked", self.store.set_mode, "LEGACY_ACTIVE", v)
        for device in ("zenbook", "dell", "oracle"):
            self.store.acknowledge(device, device + "-inst", v + 1, self.store.status()["epoch"])
        self.store.set_mode("LEGACY_ACTIVE", v)
        self.refuse("no_admission", self.store.consume, **ticket(self.store, lease, self.clock, op="op-9"))


class QuarantineLatch(Base):
    """A stored QUARANTINED mode stays a mutation latch, including the next open."""

    def _snap(self, store, fleet_id="home"):
        row = store._db.execute(
            "SELECT authority_id, mode, incarnation, epoch, holder, lease_id"
            " FROM authority WHERE fleet_id=?", (fleet_id,)).fetchone()
        return tuple(row)

    def test_second_open_after_a_bad_mode_still_refuses_participants(self):
        self.active()
        self.store.close()
        con = sqlite3.connect(self.db)
        con.execute("UPDATE authority SET mode='NOT_A_MODE' WHERE fleet_id='home'")
        con.commit()
        con.close()
        self.store = open_store(self.db, self.clock)
        self.assertEqual(self.store.status()["mode"], "QUARANTINED")
        latched = self._snap(self.store)
        self.refuse("quarantined", self.store.register_participant, "spare", "spare-inst", "pc")
        self.store.close()
        self.store = open_store(self.db, self.clock)
        self.assertTrue(self.store._quarantined)
        self.assertEqual(self._snap(self.store), latched)
        self.assertEqual(self.store.status()["mode"], "QUARANTINED")
        self.refuse("quarantined", self.store.register_participant, "spare", "spare-inst", "pc")
        self.refuse("quarantined", self.store.set_mode, "LEGACY_ACTIVE", self.store.status()["mode_version"])
        self.assertIsNone(self.store._db.execute(
            "SELECT 1 FROM participants WHERE device_id='spare'").fetchone())
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 3)

    def test_reopen_with_the_stored_authority_id_stays_quarantined(self):
        self.active()
        self.restart("someone-else")
        self.assertEqual(self.store.status()["mode"], "QUARANTINED")
        latched = self._snap(self.store)
        self.refuse("quarantined", self.store.register_participant, "spare", "spare-inst", "pc")
        self.restart("oracle-auth")
        self.assertTrue(self.store._quarantined)
        self.assertEqual(self._snap(self.store), latched)
        self.assertEqual(self.store.status()["mode"], "QUARANTINED")
        self.refuse("quarantined", self.store.register_participant, "spare", "spare-inst", "pc")
        self.assertIsNone(self.store._db.execute(
            "SELECT 1 FROM participants WHERE device_id='spare'").fetchone())

    def test_missing_fleet_status_is_quarantined(self):
        self.active()
        home = self._snap(self.store)
        self.store.close()
        self.store = AuthorityStore(
            self.db, "other-fleet", "oracle-auth", monotonic=self.clock.mono, wall=self.clock.wall)
        self.assertTrue(self.store._quarantined)
        self.refuse("quarantined", self.store.status)
        self.refuse("quarantined", self.store.register_participant, "spare", "spare-inst", "pc")
        self.assertEqual(self._snap(self.store), home)
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 3)


class StoreInputGuards(Base):
    """Direct store calls reject the margin, flags, and retention the gateway already rejects."""

    def test_invalid_margin_refuses_before_taking_the_writer(self):
        path = Path(self.tmp.name) / "margin.sqlite"
        for margin in (-30, float("nan"), float("inf"), True):
            with self.assertRaises(AuthorityError) as caught:
                AuthorityStore(path, "home", "oracle-auth", monotonic=self.clock.mono,
                               wall=self.clock.wall, margin=margin)
            self.assertEqual(caught.exception.code, "bad_request", margin)
        opened = AuthorityStore(path, "home", "oracle-auth", monotonic=self.clock.mono,
                                wall=self.clock.wall, margin=0)
        try:
            self.assertEqual(opened.status()["mode"], "LEGACY_ACTIVE")
            self.assertEqual(opened.margin, 0.0)
        finally:
            opened.close()

    def test_non_bool_flags_and_bool_epochs_do_not_write(self):
        lease = self.active()
        self.refuse("bad_request", self.store.renew, lease["holder"], lease["lease_id"], True, lease["epoch"])
        self.assertTrue(self.store.status()["lease_valid"])
        self.assertEqual(self.store.status()["main"], "zenbook")
        self.refuse("bad_request", self.store.release, lease["holder"], lease["lease_id"],
                    lease["incarnation"], True)
        self.assertEqual(self.store.status()["main"], "zenbook")
        before = self.store._db.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
        self.refuse("bad_request", self.store.register_participant, "spare", "spare-inst", "pc", "false")
        self.refuse("bad_request", self.store.register_participant, "spare", "spare-inst", "pc", 1)
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM participants").fetchone()[0], before)
        self.assertIsNone(self.store._db.execute(
            "SELECT 1 FROM participants WHERE device_id='spare'").fetchone())
        ack(self.store)
        tid = transition(self.store, "make_main_now", "dell", "k-flag")
        self.refuse("bad_request", self.store.readiness, tid, "false", 0)
        self.assertEqual(self.store.transition(tid)["state"], "REQUESTED")
        self.assertIsNone(self.store.transition(tid)["control_ready"])
        self.store.readiness(tid, True, False, "cp-1")
        self.store.drain(tid)
        self.store.fence(tid)
        self.store.grant(tid)
        self.refuse("bad_request", self.store.complete, tid, "no")
        self.assertEqual(self.store.transition(tid)["state"], "GRANTED_NOT_READY")
        self.assertFalse(self.store.status()["main_ready"])
        failed = self.store.complete(tid, False)
        self.assertEqual((failed["state"], failed["outcome"]), ("FAILED", "canary_failed"))

    def test_prune_refuses_quarantine_and_a_negative_window(self):
        lease = self.active()
        minted = ticket(self.store, lease, self.clock)
        self.store.consume(**minted)
        self.refuse("bad_request", self.store.prune, -100000)
        self.refuse("bad_request", self.store.prune, float("nan"))
        self.assertEqual(self.store.prune(0), 0)
        self.assertIsNotNone(self.store.admission(minted["admission_id"]))
        self.store.close()
        con = sqlite3.connect(self.db)
        con.execute("UPDATE meta SET value='99' WHERE key='schema'")
        con.commit()
        con.close()
        self.store = open_store(self.db, self.clock)
        self.refuse("quarantined", self.store.prune, -100000)
        self.refuse("quarantined", self.store.register_participant, "spare", "spare-inst", "pc")
        self.assertIsNotNone(self.store.admission(minted["admission_id"]))


class CancelDrainQuorum(Base):
    """cancel() during DRAINING uses fence()'s quorum, not a single ack."""

    def _draining(self, key):
        lease = self.active()
        epoch = self.store.status()["epoch"]
        tid = transition(self.store, "make_main_now", "dell", key)
        self.store.readiness(tid, True, False)
        self.store.drain(tid)
        self.assertEqual(self.store.transition(tid)["state"], "DRAINING")
        return lease, epoch, tid

    def _assert_old_main_kept(self, lease, epoch, tid):
        t = self.store.cancel(tid)
        st = self.store.status()
        self.assertEqual((t["state"], t["outcome"]), ("CANCELLED", "old_main_kept"))
        self.assertEqual((st["mode"], st["main"], st["lease_holder"], st["epoch"]),
                         ("V2_ACTIVE", "zenbook", "zenbook", epoch))
        self.assertTrue(st["lease_valid"])
        row = self.store._db.execute(
            "SELECT lease_id, epoch FROM authority WHERE fleet_id='home'").fetchone()
        self.assertEqual((row["lease_id"], row["epoch"]), (lease["lease_id"], epoch))
        renewed = self.store.renew(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.assertTrue(renewed["ready"])

    def test_cancel_draining_one_eligible_ack_keeps_old_main(self):
        lease, epoch, tid = self._draining("k-partial")
        ack(self.store, ("dell",))
        self._assert_old_main_kept(lease, epoch, tid)

    def test_cancel_draining_full_quorum_leaves_no_main(self):
        lease, epoch, tid = self._draining("k-full")
        ack(self.store)  # zenbook (previous holder), dell, and oracle
        t = self.store.cancel(tid)
        st = self.store.status()
        self.assertEqual((t["state"], t["outcome"]), ("FAILED", "cancelled_after_acks"))
        self.assertEqual((st["mode"], st["main"], st["lease_valid"]), ("V2_ONLY_NO_MAIN", None, False))
        self.assertGreater(st["epoch"], epoch)
        self.refuse("stale", self.store.renew, lease["holder"], lease["lease_id"],
                    lease["incarnation"], lease["epoch"])

    def test_cancel_draining_ineligible_ack_keeps_old_main(self):
        lease, epoch, tid = self._draining("k-ineligible")
        ack(self.store, ("oracle",))
        disabled = self.store.disable_participant("oracle", "oracle-inst")
        self.assertFalse(disabled["eligible"])
        self.assertGreaterEqual(disabled["fenced_epoch"], epoch)
        self._assert_old_main_kept(lease, epoch, tid)

    def _assert_fenced_cancel_fails_closed(self, lease, epoch, tid):
        fenced_epoch = self.store.status()["epoch"]
        self.assertGreater(fenced_epoch, epoch)
        # Nobody has acked the epoch fence() just published. Cancel still fails
        # closed: the old lease is already gone.
        acked_new = self.store._db.execute(
            "SELECT COUNT(*) FROM participants WHERE fenced_epoch>=?",
            (fenced_epoch,)).fetchone()[0]
        self.assertEqual(acked_new, 0)
        t = self.store.cancel(tid)
        st = self.store.status()
        self.assertEqual((t["state"], t["outcome"]), ("FAILED", "cancelled_after_fence"))
        self.assertEqual(st["mode"], "V2_ONLY_NO_MAIN")
        self.assertIsNone(st["main"])
        self.assertGreater(st["epoch"], fenced_epoch)
        self.refuse("stale", self.store.renew, lease["holder"], lease["lease_id"],
                    lease["incarnation"], lease["epoch"])

    def test_cancel_fencing_still_fails_closed(self):
        lease, epoch, tid = self._draining("k-fencing")
        ack(self.store)
        self.store.fence(tid)
        self.assertEqual(self.store.transition(tid)["state"], "FENCING")
        self._assert_fenced_cancel_fails_closed(lease, epoch, tid)

    def test_cancel_granted_not_ready_still_fails_closed(self):
        lease, epoch, tid = self._draining("k-granted")
        ack(self.store)
        self.store.fence(tid)
        self.store.grant(tid)
        self.assertEqual(self.store.transition(tid)["state"], "GRANTED_NOT_READY")
        self._assert_fenced_cancel_fails_closed(lease, epoch, tid)

    def test_cancel_draining_only_source_eligible_and_no_acks_keeps_old_main(self):
        lease, epoch, tid = self._draining("k-only-source")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE participants SET eligible=0 WHERE device_id!=?", ("zenbook",))
        con.commit()
        con.close()
        eligible = [r[0] for r in self.store._db.execute(
            "SELECT device_id FROM participants WHERE eligible=1 ORDER BY device_id")]
        acked = self.store._db.execute(
            "SELECT COUNT(*) FROM participants WHERE fenced_epoch>=?", (epoch,)).fetchone()[0]
        self.assertEqual(eligible, ["zenbook"])
        self.assertEqual(acked, 0)
        self._assert_old_main_kept(lease, epoch, tid)

    def test_cancel_draining_vacuous_acked_keeps_old_main(self):
        # _acked is trivially true: nobody eligible remains, and the prior row's
        # highwater is set while that row is ineligible. That must not retire MAIN.
        lease, epoch, tid = self._draining("k-vacuous")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE participants SET eligible=0")
        con.execute("UPDATE participants SET fenced_epoch=? WHERE device_id='zenbook'", (epoch,))
        con.commit()
        con.close()
        eligible_acks = self.store._db.execute(
            "SELECT COUNT(*) FROM participants WHERE eligible=1 AND fenced_epoch>=?",
            (epoch,)).fetchone()[0]
        self.assertEqual(eligible_acks, 0)
        self._assert_old_main_kept(lease, epoch, tid)


if __name__ == "__main__":
    unittest.main()
