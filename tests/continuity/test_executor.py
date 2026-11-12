"""Executor fence and admission: legacy until cutover, then one online consume before an effect."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity.authority import AuthorityError
from jarvis.continuity.executor import ExecutorGuard

from tests.continuity._fleet import DEVICES, Clock, ack, cutover, digest, open_store, promote, ticket, transition


SIGN = "sig-do-not-persist"
SECRET = "prompt-do-not-persist"


class Spy:
    def __init__(self, store):
        self.store = store
        self.consumes = 0

    def status(self):
        return self.store.status()

    def acknowledge(self, *args, **kwargs):
        return self.store.acknowledge(*args, **kwargs)

    def admission(self, admission_id):
        return self.store.admission(admission_id)

    def consume(self, **kwargs):
        self.consumes += 1
        return self.store.consume(**kwargs)


class Down:
    def status(self):
        raise OSError(SECRET)

    def acknowledge(self, *args, **kwargs):
        raise OSError(SECRET)

    def admission(self, admission_id):
        raise OSError(SECRET)

    def consume(self, **kwargs):
        raise OSError(SECRET)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "authority.sqlite"
        self.clock = Clock()
        self.store = open_store(self.db, self.clock)
        self.spy = Spy(self.store)
        self.boot = {"id": "boot-a"}
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def refuse(self, code, fn, *args, **kwargs):
        with self.assertRaises(AuthorityError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def guard(self, device="dell", path=None, managed=False, authority=None, verifier=True):
        path = Path(path) if path else Path(self.tmp.name) / f"exec-{device}.sqlite"
        guard = ExecutorGuard(
            path, device_id=device, installation_id=device + "-inst", fleet_id="home",
            authority_id="oracle-auth", authority=self.spy if authority is None else authority,
            verifier=(lambda op: op.get("signature") == SIGN) if verifier is True else verifier,
            boot=lambda: self.boot["id"], wall=self.clock.wall, managed=managed)
        self.addCleanup(guard.close)
        return guard

    def activate(self, main="zenbook"):
        cutover(self.store)
        self.lease = promote(self.store, "recover", main, "k-boot")[1]
        return self.lease

    def fence_working(self, guard):
        status = self.store.status()
        return guard.fence(status["incarnation"], status["epoch"] - 1, status["mode_version"])

    def work(self, op="op-1", attempt="a-1", target="dell", body="open firefox", effect="ordinary",
             signature=SIGN, lease=None, **overrides):
        lease = self.lease if lease is None else lease
        status = self.store.status()
        raw = ticket(self.store, lease, self.clock, op=op, attempt=attempt, target=target, body=body)
        signed = {
            "authority_id": "oracle-auth", "fleet_id": "home", "main": raw["main"],
            "incarnation": raw["incarnation"], "epoch": raw["epoch"],
            "mode_version": status["mode_version"], "policy_version": status["policy_version"],
            "target": raw["target"], "operation_id": raw["operation_id"], "attempt_id": raw["attempt_id"],
            "digest": raw["digest"], "effect_class": effect, "issued_at": raw["issued_at"],
            "admit_until": raw["admit_until"], "issuer_incarnation": raw["issuer_incarnation"],
            "admission_id": raw["admission_id"], "signature": signature,
        }
        admit = {key: raw[key] for key in (
            "admission_id", "target", "operation_id", "attempt_id", "digest", "main", "incarnation",
            "epoch", "issued_at", "admit_until", "issuer_incarnation")}
        for key, value in overrides.items():
            if key in signed:
                signed[key] = value
            if key in admit:
                admit[key] = value
        return signed, admit

    def ran(self):
        calls = []

        def sink():
            calls.append("ran")
        return calls, sink

    def files_text(self):
        chunks = []
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_file():
                chunks.append(path.read_bytes())
        return b"\n".join(chunks)


class Legacy(Base):
    def test_default_legacy_runs_and_v2_ticket_does_not(self):
        guard = self.guard()
        calls, sink = self.ran()
        self.assertEqual(guard.legacy_effect(lambda: "ok-legacy"), "ok-legacy")
        self.activate()
        signed, admit = self.work()
        self.refuse("legacy", guard.accept, signed, admit, sink)
        self.assertEqual(calls, [])
        self.assertEqual(self.spy.consumes, 0)
        self.assertFalse(guard.local_view()["legacy_closed"])

    def test_managed_missing_journal_does_not_become_legacy(self):
        path = Path(self.tmp.name) / "fresh.sqlite"
        guard = self.guard(path=path, managed=True)
        self.assertFalse(path.exists())
        self.assertTrue(guard.local_view()["quarantined"])
        calls, sink = self.ran()
        self.refuse("quarantined", guard.legacy_effect, sink)
        self.refuse("quarantined", guard.fence, 1, 0, 1)
        signed = {"signature": SIGN}
        self.refuse("quarantined", guard.accept, signed, signed, sink)
        self.assertEqual(calls, [])

    def test_preparing_ack_keeps_legacy_quiesced_closes_it(self):
        for device, kind in DEVICES:
            self.store.register_participant(device, device + "-inst", kind)
        version = self.store.set_mode("PREPARING_V2", self.store.status()["mode_version"])
        guard = self.guard()
        status = self.store.status()
        guard.fence(status["incarnation"], status["epoch"], version)
        calls, sink = self.ran()
        guard.legacy_effect(sink)
        self.assertEqual(calls, ["ran"])
        version = self.store.set_mode("QUIESCED", version)
        status = self.store.status()
        guard.fence(status["incarnation"], status["epoch"], version)
        self.refuse("fenced", guard.legacy_effect, sink)
        self.activate_from_quiesced(version)
        # Still no ready MAIN until promote; a v2 ticket must not run on the closed legacy path.
        self.lease = promote(self.store, "recover", "zenbook", "k-after-quiet")[1]

    def activate_from_quiesced(self, version):
        status = self.store.status()
        self.assertEqual(status["mode"], "QUIESCED")
        for device, _kind in DEVICES:
            self.store.acknowledge(device, device + "-inst", version + 1, status["epoch"])
        self.store.set_mode("V2_ONLY_NO_MAIN", version)

    def test_v2_fence_closes_legacy_and_survives_reboot(self):
        self.activate()
        guard = self.guard()
        ack_view = self.fence_working(guard)
        self.assertTrue(ack_view["legacy_closed"])
        self.assertEqual(ack_view["high_epoch"], self.store.status()["epoch"])
        self.assertLess(ack_view["fenced_epoch"], ack_view["high_epoch"])
        calls, sink = self.ran()
        self.refuse("fenced", guard.legacy_effect, sink)
        guard.close()
        self.boot["id"] = "boot-b"
        rebooted = self.guard()
        view = rebooted.local_view()
        self.assertTrue(view["rebooted"])
        self.assertTrue(view["legacy_closed"])
        self.assertEqual(view["high_epoch"], self.store.status()["epoch"])
        self.refuse("fenced", rebooted.legacy_effect, sink)
        self.assertEqual(calls, [])

    def test_fence_is_monotonic_and_cannot_invent_an_epoch(self):
        self.activate()
        guard = self.guard()
        status = self.store.status()
        guard.fence(status["incarnation"], 0, status["mode_version"])
        again = guard.fence(status["incarnation"], 0, status["mode_version"])
        self.assertEqual((again["fenced_epoch"], again["high_epoch"]), (0, status["epoch"]))
        self.refuse("ahead", guard.fence, status["incarnation"], status["epoch"] + 5, status["mode_version"])
        self.refuse("ahead", guard.fence, status["incarnation"], 0, status["mode_version"] + 9)
        view = guard.local_view()
        self.assertEqual((view["fenced_epoch"], view["high_epoch"], view["high_mode_version"]),
                         (0, status["epoch"], status["mode_version"]))

    def test_unenrolled_fence_stays_closed(self):
        self.activate()
        guard = self.guard("stranger")
        status = self.store.status()
        calls, sink = self.ran()
        self.refuse("unknown_participant", guard.fence, status["incarnation"], 0, status["mode_version"])
        self.refuse("fenced", guard.legacy_effect, sink)
        self.assertEqual(calls, [])

    def test_second_writer_is_refused(self):
        self.activate()
        guard = self.guard()
        self.fence_working(guard)
        self.refuse("writer_busy", self.guard)

    def test_legacy_reopens_only_after_authority_rollback(self):
        self.activate()
        guard = self.guard()
        self.fence_working(guard)
        status = self.store.status()
        self.refuse("bad_edge", guard.fence, status["incarnation"], 0, status["mode_version"], reopen_legacy=True)
        self.store.release(self.lease["holder"], self.lease["lease_id"], self.lease["incarnation"], self.lease["epoch"])
        status = self.store.status()
        ack(self.store, epoch=status["epoch"])
        version = self.store.set_mode("ROLLBACK_QUIESCED", status["mode_version"])
        status = self.store.status()
        for device, _kind in DEVICES:
            self.store.acknowledge(device, device + "-inst", version + 1, status["epoch"])
        self.store.set_mode("LEGACY_ACTIVE", version)
        status = self.store.status()
        self.assertEqual(status["mode"], "LEGACY_ACTIVE")
        opened = guard.fence(status["incarnation"], status["epoch"], status["mode_version"], reopen_legacy=True)
        self.assertFalse(opened["legacy_closed"])
        calls, sink = self.ran()
        guard.legacy_effect(sink)
        signed, admit = self.work()
        self.refuse("legacy", guard.accept, signed, admit, sink)
        self.assertEqual(calls, ["ran"])
        self.assertEqual(self.spy.consumes, 0)


class Admission(Base):
    def test_consume_happens_before_one_effect_and_dedup_keeps_original_expiry(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        signed, admit = self.work(body=SECRET)
        receipt = guard.accept(signed, admit, sink)
        self.assertEqual(calls, ["ran"])
        self.assertEqual((receipt["state"], receipt["effect"], receipt["already"]), ("COMPLETED", "completed", False))
        self.assertEqual(self.spy.consumes, 1)
        self.assertNotIn(SECRET.encode(), self.files_text())
        self.assertNotIn(SIGN.encode(), self.files_text())
        again = guard.accept(signed, admit, sink)
        self.assertTrue(again["already"])
        self.assertEqual(calls, ["ran"])
        self.assertEqual(self.spy.consumes, 1)
        self.assertEqual(guard.reconcile("op-1", "a-1")["issued_at"], receipt["issued_at"])
        refreshed, refreshed_ticket = self.work(body=SECRET)
        refreshed["admit_until"] = self.clock.w + 10
        refreshed_ticket["admit_until"] = refreshed["admit_until"]
        self.refuse("conflict", guard.accept, refreshed, refreshed_ticket, sink)
        self.assertEqual(calls, ["ran"])

    def test_conflict_digest_expiry_and_ordinary_retry_do_not_run(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        signed, admit = self.work()
        guard.accept(signed, admit, sink)
        other, other_ticket = self.work(body="different")
        self.refuse("conflict", guard.accept, other, other_ticket, sink)
        retry, retry_ticket = self.work(attempt="a-2")
        self.refuse("conflict", guard.accept, retry, retry_ticket, sink)
        self.assertEqual(calls, ["ran"])
        self.assertEqual(self.spy.consumes, 1)

    def test_expired_ticket_is_pinned_and_not_consumed(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        signed, admit = self.work()
        signed["issued_at"] = self.clock.w - 100
        signed["admit_until"] = self.clock.w - 1
        admit["issued_at"] = signed["issued_at"]
        admit["admit_until"] = signed["admit_until"]
        self.refuse("ticket_expired", guard.accept, signed, admit, sink)
        self.refuse("ticket_expired", guard.accept, signed, admit, sink)
        signed["admit_until"] = self.clock.w + 30
        admit["admit_until"] = signed["admit_until"]
        self.refuse("conflict", guard.accept, signed, admit, sink)
        self.assertEqual(calls, [])
        self.assertEqual(self.spy.consumes, 0)

    def test_stale_epoch_is_refused_without_authority_and_without_effect(self):
        self.activate()
        guard = self.ready()
        guard._authority = Down()
        calls, sink = self.ran()
        signed, admit = self.work(epoch=0)
        error = self.refuse("stale", guard.accept, signed, admit, sink)
        self.assertNotIn(SECRET, str(error))
        current, current_ticket = self.work(op="op-live", attempt="a-live")
        error = self.refuse("unavailable", guard.accept, current, current_ticket, sink)
        self.assertNotIn(SECRET, str(error))
        self.assertEqual(calls, [])

    def test_forged_higher_epoch_does_not_move_the_high_water_mark(self):
        self.activate()
        guard = self.ready()
        before = guard.local_view()["high_epoch"]
        calls, sink = self.ran()
        signed, admit = self.work(epoch=99)
        self.refuse("stale", guard.accept, signed, admit, sink)
        self.assertEqual(guard.local_view()["high_epoch"], before)
        self.assertEqual(self.spy.consumes, 0)
        self.assertEqual(calls, [])

    def test_tuple_mismatch_and_not_ready_do_not_run(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        signed, admit = self.work(mode_version=1)
        if self.store.status()["mode_version"] == 1:
            signed["mode_version"] = 2
            # policy stays aligned; mode version is the mismatch
        self.refuse("stale", guard.accept, signed, admit, sink)
        wrong_policy, wrong_ticket = self.work(op="op-policy", attempt="a-policy", policy_version=9)
        self.refuse("stale", guard.accept, wrong_policy, wrong_ticket, sink)
        self.assertEqual(self.spy.consumes, 0)
        self.assertEqual(calls, [])

    def test_not_ready_grant_admits_nothing(self):
        cutover(self.store)
        transition_id = transition(self.store, "recover", "zenbook", "k-not-ready")
        self.store.readiness(transition_id, True, True, "cp-1")
        self.store.drain(transition_id)
        self.store.fence(transition_id)
        self.lease = self.store.grant(transition_id)
        guard = self.guard()
        status = self.store.status()
        guard.fence(status["incarnation"], status["epoch"] - 1, status["mode_version"])
        calls, sink = self.ran()
        signed, admit = self.work()
        self.refuse("no_admission", guard.accept, signed, admit, sink)
        self.assertEqual(calls, [])
        self.assertEqual(self.spy.consumes, 0)

    def test_signature_verifier_and_personal_fields_fail_closed(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        signed, admit = self.work(signature="nope")
        self.refuse("signature", guard.accept, signed, admit, sink)
        signed, admit = self.work(op="op-body")
        signed["prompt"] = SECRET
        self.refuse("bad_request", guard.accept, signed, admit, sink)

        def explodes(_op):
            raise RuntimeError(SECRET)

        guard._verifier = explodes
        signed, admit = self.work(op="op-boom")
        error = self.refuse("signature", guard.accept, signed, admit, sink)
        self.assertNotIn(SECRET, str(error))
        bare = self.guard(device="zenbook", verifier=None)
        self.fence_working(bare)
        signed, admit = self.work(op="op-bare", target="zenbook")
        self.refuse("no_verifier", bare.accept, signed, admit, sink)
        self.assertEqual(calls, [])
        self.assertEqual(self.spy.consumes, 0)
        self.assertNotIn(SECRET.encode(), self.files_text())

    def test_fencing_the_live_epoch_stops_this_target(self):
        self.activate()
        guard = self.guard()
        status = self.store.status()
        guard.fence(status["incarnation"], status["epoch"], status["mode_version"])
        calls, sink = self.ran()
        signed, admit = self.work()
        self.refuse("stale", guard.accept, signed, admit, sink)
        self.assertEqual(calls, [])
        self.assertEqual(self.spy.consumes, 0)
        self.refuse("stale", self.store.consume, **admit)

    def test_handover_rejects_old_epoch_and_runs_the_new_one_once(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        first, first_ticket = self.work(op="op-old", attempt="a-old")
        guard.accept(first, first_ticket, sink)
        status = self.store.status()
        ack(self.store, epoch=status["epoch"])
        _tid, new_lease = promote(self.store, "make_main_now", "dell", "k-move")
        self.lease = new_lease
        stale, stale_ticket = self.work(op="op-stale", attempt="a-stale", epoch=first["epoch"])
        self.refuse("stale", guard.accept, stale, stale_ticket, sink)
        fresh, fresh_ticket = self.work(op="op-new", attempt="a-new", target="dell")
        guard.accept(fresh, fresh_ticket, sink)
        self.assertEqual(calls, ["ran", "ran"])
        guard.accept(fresh, fresh_ticket, sink)
        self.assertEqual(calls, ["ran", "ran"])

    def test_pure_compute_retry_needs_a_new_attempt_after_completion(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        signed, admit = self.work(effect="pure_compute")
        guard.accept(signed, admit, sink)
        again, again_ticket = self.work(effect="pure_compute", attempt="a-2")
        guard.accept(again, again_ticket, sink)
        self.assertEqual(calls, ["ran", "ran"])
        ordinary, ordinary_ticket = self.work(op="op-2", attempt="a-1")
        guard.accept(ordinary, ordinary_ticket, sink)
        blocked, blocked_ticket = self.work(op="op-2", attempt="a-9")
        self.refuse("conflict", guard.accept, blocked, blocked_ticket, sink)
        self.assertEqual(calls, ["ran", "ran", "ran"])

    def test_consumed_elsewhere_is_not_replayed(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        signed, admit = self.work()
        self.store.consume(**admit)
        self.refuse("uncertain", guard.accept, signed, admit, sink)
        self.refuse("uncertain", guard.accept, signed, admit, sink)
        self.assertEqual(guard.reconcile("op-1", "a-1")["effect"], "uncertain")
        self.assertEqual(calls, [])
        self.assertEqual(self.spy.consumes, 0)

    def test_interactive_stop_releases_without_replay(self):
        self.activate()
        guard = self.ready()
        stopped = guard.stop_interactive("op-click", "owner_lost")
        self.assertFalse(stopped["replay"])
        calls, sink = self.ran()
        signed, admit = self.work(op="op-click", effect="interactive")
        self.refuse("released", guard.accept, signed, admit, sink)
        self.assertEqual(calls, [])
        self.assertEqual(self.spy.consumes, 0)
        guard._authority = Down()
        again = guard.stop_interactive("op-click", "owner_lost")
        self.assertTrue(again["already"])
        self.assertFalse(again["replay"])

    def ready(self):
        guard = self.guard()
        self.fence_working(guard)
        return guard


class Crash(Base):
    def test_crash_windows_do_not_double_run(self):
        self.activate()
        guard = self.ready()
        calls, sink = self.ran()
        prepared, prepared_ticket = self.work(op="op-prep")
        self.refuse("crash", guard.accept, prepared, prepared_ticket, sink, failpoint="after_prepare")
        self.assertEqual(self.spy.consumes, 0)
        status = self.store.status()
        ack(self.store, epoch=status["epoch"])
        promote(self.store, "make_main_now", "dell", "k-move")
        self.refuse("stale", guard.accept, prepared, prepared_ticket, sink)
        self.assertEqual(calls, [])
        self.assertEqual(self.spy.consumes, 0)

        status = self.store.status()
        self.lease = {"holder": status["main"], "incarnation": status["incarnation"], "epoch": status["epoch"]}
        consumed, consumed_ticket = self.work(op="op-cons")
        self.refuse("crash", guard.accept, consumed, consumed_ticket, sink, failpoint="after_consume")
        self.assertEqual(calls, [])
        guard.accept(consumed, consumed_ticket, sink)
        self.assertEqual(calls, ["ran"])

        accepted, accepted_ticket = self.work(op="op-acc")
        self.refuse("crash", guard.accept, accepted, accepted_ticket, sink, failpoint="after_accept")
        self.assertEqual(guard.reconcile("op-acc", "a-1")["effect"], "uncertain")
        self.refuse("uncertain", guard.accept, accepted, accepted_ticket, sink)
        running, running_ticket = self.work(op="op-run")
        self.refuse("crash", guard.accept, running, running_ticket, sink, failpoint="during_effect")
        self.refuse("uncertain", guard.accept, running, running_ticket, sink)
        self.assertEqual(calls, ["ran", "ran"])

    def test_sink_exception_is_uncertain_and_not_replayed(self):
        self.activate()
        guard = self.ready()
        calls = []

        def sink():
            calls.append("ran")
            raise RuntimeError(SECRET)

        signed, admit = self.work()
        error = self.refuse("uncertain", guard.accept, signed, admit, sink)
        self.assertNotIn(SECRET, str(error))
        self.refuse("uncertain", guard.accept, signed, admit, sink)
        self.assertEqual(calls, ["ran"])
        self.assertEqual(guard.reconcile("op-1", "a-1")["state"], "ACCEPTED")

    def test_tamper_and_schema_quarantine_do_not_fall_back_to_legacy(self):
        self.activate()
        guard = self.ready()
        path = Path(self.tmp.name) / "exec-dell.sqlite"
        calls, sink = self.ran()
        signed, admit = self.work()
        guard.accept(signed, admit, sink)
        guard.close()
        db = sqlite3.connect(path)
        db.execute("UPDATE fence SET fenced_epoch = fenced_epoch + 1")
        db.commit()
        db.close()
        broken = self.guard()
        self.assertTrue(broken.local_view()["quarantined"])
        self.refuse("quarantined", broken.legacy_effect, sink)
        self.refuse("quarantined", broken.accept, signed, admit, sink)
        self.assertEqual(calls, ["ran"])
        broken.close()
        db = sqlite3.connect(path)
        db.execute("UPDATE meta SET value='99' WHERE key='schema'")
        db.commit()
        db.close()
        foreign = self.guard(device="zenbook", path=Path(self.tmp.name) / "other-install.sqlite")
        # A second installation does not reuse dell's journal. Tampered schema is the next open.
        schema_path = Path(self.tmp.name) / "schema.sqlite"
        guard = self.guard(path=schema_path)
        self.fence_working(guard)
        guard.close()
        db = sqlite3.connect(schema_path)
        db.execute("UPDATE meta SET value='99' WHERE key='schema'")
        db.commit()
        db.close()
        quarantined = self.guard(path=schema_path)
        self.refuse("quarantined", quarantined.legacy_effect, sink)
        self.refuse("quarantined", quarantined.accept, signed, admit, sink)
        self.assertFalse(foreign.local_view()["legacy_closed"])

    def test_journal_bound_to_another_installation_is_quarantined(self):
        self.activate()
        guard = self.ready()
        path = Path(self.tmp.name) / "exec-dell.sqlite"
        guard.close()
        other = self.guard(device="zenbook", path=path)
        self.assertTrue(other.local_view()["quarantined"])
        calls, sink = self.ran()
        self.refuse("quarantined", other.legacy_effect, sink)
        self.assertEqual(calls, [])

    def test_status_regression_does_not_authorize_work(self):
        script = Scripted()
        guard = self.guard(authority=script)
        guard.fence(1, 0, script.st["mode_version"])
        script.st["epoch"] = 0
        calls, sink = self.ran()
        signed, admit = self.script_ticket(script)
        error = self.refuse("stale", guard.accept, signed, admit, sink)
        self.assertNotIn("ran", calls)
        self.assertEqual(script.consumes, 0)
        self.assertEqual(guard.local_view()["high_epoch"], 1)
        self.assertNotIn(SECRET, str(error))

    def script_ticket(self, script):
        body = digest("script-body")
        signed = {
            "authority_id": "oracle-auth", "fleet_id": "home", "main": "zenbook",
            "incarnation": 1, "epoch": 1, "mode_version": script.st["mode_version"], "policy_version": 1,
            "target": "dell", "operation_id": "op-script", "attempt_id": "a-script", "digest": body,
            "effect_class": "ordinary", "issued_at": self.clock.w - 1, "admit_until": self.clock.w + 60,
            "issuer_incarnation": 1, "admission_id": "adm-script", "signature": SIGN,
        }
        admit = {key: signed[key] for key in (
            "admission_id", "target", "operation_id", "attempt_id", "digest", "main", "incarnation",
            "epoch", "issued_at", "admit_until", "issuer_incarnation")}
        return signed, admit

    def ready(self):
        guard = self.guard()
        self.fence_working(guard)
        return guard


class Scripted:
    def __init__(self):
        self.consumes = 0
        self.st = {"authority_id": "oracle-auth", "incarnation": 1, "epoch": 1, "mode_version": 4,
                   "policy_version": 1, "mode": "V2_ACTIVE", "main": "zenbook", "main_ready": True,
                   "lease_valid": True}

    def status(self):
        return dict(self.st)

    def acknowledge(self, *args, **kwargs):
        return None

    def admission(self, admission_id):
        return None

    def consume(self, **kwargs):
        self.consumes += 1
        return dict(kwargs, already=False, consumed_at=1.0)
