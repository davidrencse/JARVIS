"""A link blip or a raised hook must not cancel or stick a not-ready grant.

Fixtures and the acking runtime come from the safe-handoff tests. This module
does not change those tests.
"""

import json
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity.authority import LEASE_SECONDS
from jarvis.continuity.recovery import ManualRecovery
from jarvis.continuity.runtime import LinkError, MainRuntime, RuntimeRefusal
from tests.continuity._fleet import Clock, HandoffFixtures, ack, cutover, open_store, promote
from tests.continuity.test_safe_handoff import TOKEN, BASE, AckingRuntime


def _confirmed(old, new, lease):
    return {"ok": True, "authenticated": True, "key_updated": True, "endpoint_updated": True}


class _PortSpy:
    """Counts renew and transition steps. Everything else is the real port."""

    def __init__(self, inner):
        self.inner = inner
        self.steps = []
        self.renews = 0

    def renew(self, lease_tuple):
        self.renews += 1
        return self.inner.renew(lease_tuple)

    def transition_step(self, transition_id, step, canary_ok=None):
        self.steps.append(step)
        return self.inner.transition_step(transition_id, step, canary_ok)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class HandoffRecoveryTests(HandoffFixtures, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "authority.sqlite"
        self.clock = Clock()
        self.store = open_store(self.db, self.clock)
        self.addCleanup(self.store.close)
        self.port = _PortSpy(self._adapter())
        cutover(self.store)
        _tid, self.lease = promote(self.store, "recover", "zenbook", "k-boot")
        self.events = []

    def _adapter(self):
        from jarvis.continuity.runtime import StoreAdapter
        return StoreAdapter(self.store)

    def brain(self, old, new, config, base):
        self.events.append("brain")
        return {"ok": True, "status": "synced"}

    def repoint(self, old, new, lease):
        self.events.append("repoint")
        return _confirmed(old, new, lease)

    def canary(self, new):
        self.events.append("canary")
        return True

    def prepare(self, candidate_cls=MainRuntime):
        self.use_brain(self.brain)
        ack(self.store, ("dell", "oracle"))
        source = self.seat(cls=AckingRuntime)
        candidate = self.seat("dell", cls=candidate_cls)
        return source, candidate

    def _faulty(self, faults):
        pending = list(faults)

        class _Fault(MainRuntime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.snapshot_calls = 0

            def lease_snapshot(self):
                self.snapshot_calls += 1
                if pending:
                    fault = pending.pop(0)
                    if fault is not None:
                        raise fault
                return super().lease_snapshot()

        return _Fault

    def assert_no_lease_id(self, result):
        encoded = json.dumps(result)
        self.assertNotIn("lease_id", encoded)
        self.assertNotIn("ls-", encoded)

    def test_01_link_blip_is_partition_and_the_next_call_completes(self):
        self._assert_one_blip(LinkError("partition"))

    def test_01b_oserror_blip_is_partition_and_the_next_call_completes(self):
        self._assert_one_blip(OSError("snapshot down"))

    def _assert_one_blip(self, fault):
        journal = Path(self.tmp.name) / ("blip-%s.json" % type(fault).__name__)
        source, candidate = self.prepare(self._faulty([fault]))
        first = self.handoff(source, candidate, journal_path=journal)(
            "zenbook", "dell", TOKEN, BASE)
        self.assert_no_lease_id(first)
        self.assertFalse(first["ok"])
        self.assertEqual(first["status"], "partition")
        self.assertEqual(first["code"], "partition")
        self.assertEqual(candidate.snapshot_calls, 2)
        held = self.store.status()
        self.assertEqual(held["transition"]["state"], "GRANTED_NOT_READY")
        self.assertEqual(held["main"], "dell")
        self.assertTrue(held["lease_valid"])
        self.assertFalse(held["main_ready"])
        self.assertEqual(candidate.lease_snapshot()["holder"], "dell")
        self.assertNotIn("cancel", self.port.steps)
        self.assertEqual(self.events, [])
        self.assertFalse(journal.exists())
        second = self.handoff(source, candidate, journal_path=journal)(
            "zenbook", "dell", TOKEN, BASE)
        self.assert_no_lease_id(second)
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["code"], "new_main")
        self.assertEqual(self.store.status()["mode"], "V2_ACTIVE")
        self.assertEqual(self.events, ["brain", "repoint", "canary"])
        self.assertNotIn("cancel", self.port.steps)

    def test_02_persistent_snapshot_failure_retries_then_partition(self):
        journal = Path(self.tmp.name) / "down.json"
        source, candidate = self.prepare(self._faulty([LinkError("partition")] * 5))
        result = self.handoff(source, candidate, journal_path=journal)(
            "zenbook", "dell", TOKEN, BASE)
        self.assert_no_lease_id(result)
        self.assertEqual(result["status"], "partition")
        self.assertEqual(result["code"], "partition")
        self.assertEqual(candidate.snapshot_calls, 3)
        held = self.store.status()
        self.assertEqual(held["transition"]["state"], "GRANTED_NOT_READY")
        self.assertEqual(held["main"], "dell")
        self.assertTrue(held["lease_valid"])
        self.assertNotIn("cancel", self.port.steps)
        self.assertEqual(self.events, [])
        self.assertFalse(journal.exists())

    def test_03_empty_snapshot_with_empty_journal_still_aborts(self):
        class _Empty(MainRuntime):
            def lease_snapshot(self):
                return None

        source, candidate = self.prepare(_Empty)
        result = self.handoff(source, candidate)("zenbook", "dell", TOKEN, BASE)
        self.assert_no_lease_id(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_main")
        self.assertEqual(result["code"], "lease_unavailable")
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")
        self.assertIn("cancel", self.port.steps)
        self.assertEqual(self.events, [])

    def test_04_failed_grant_check_does_not_record_started(self):
        clock = self.clock

        class _ExpireAfterRead(MainRuntime):
            def lease_snapshot(self):
                snap = super().lease_snapshot()
                clock.tick(LEASE_SECONDS + 0.01)
                return snap

        journal = Path(self.tmp.name) / "nostart.json"
        source, candidate = self.prepare(_ExpireAfterRead)
        result = self.handoff(source, candidate, journal_path=journal)(
            "zenbook", "dell", TOKEN, BASE)
        self.assert_no_lease_id(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_main")
        text = journal.read_text(encoding="utf-8")
        self.assertNotIn("started", text)
        self.assertNotIn("lease_id", text)
        doc = json.loads(text)
        self.assertEqual(doc.get("steps"), {})
        self.assertEqual(self.events, [])

    def test_05_raised_hook_pauses_renewal_until_the_lease_lapses(self):
        journal = Path(self.tmp.name) / "raise.json"
        calls = {"brain": 0, "repoint": 0, "canary": 0}

        def brain(old, new, config, base):
            calls["brain"] += 1
            return {"ok": True, "status": "synced"}

        def repoint(old, new, lease):
            calls["repoint"] += 1
            raise RuntimeError("lost")

        def canary(new):
            calls["canary"] += 1
            return True

        self.use_brain(brain)
        ack(self.store, ("dell", "oracle"))
        source = self.seat(cls=AckingRuntime)
        candidate = self.seat("dell")
        first = self.handoff(
            source, candidate, journal_path=journal, repoint=repoint, canary=canary)(
            "zenbook", "dell", TOKEN, BASE)
        self.assert_no_lease_id(first)
        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(first["code"], "reconcile")
        self.assertEqual(calls, {"brain": 1, "repoint": 1, "canary": 0})
        held = self.store.status()
        self.assertEqual(held["transition"]["state"], "GRANTED_NOT_READY")
        expires = self.store._lease["expires"]
        renews = self.port.renews
        paused = candidate.renew_once()
        self.assertEqual(paused, {"code": "renewal_paused", "wait": None})
        self.assertNotIn("lease_id", paused)
        self.assertEqual(self.port.renews, renews)
        self.assertEqual(self.store._lease["expires"], expires)
        second = self.handoff(
            source, candidate, journal_path=journal, repoint=repoint, canary=canary)(
            "zenbook", "dell", TOKEN, BASE)
        self.assertEqual(second["status"], "uncertain")
        self.assertEqual(second["code"], "reconcile")
        self.assertEqual(calls, {"brain": 1, "repoint": 1, "canary": 0})
        self.assertEqual(
            self.store.status()["transition"]["transition_id"],
            held["transition"]["transition_id"])
        self.clock.tick(LEASE_SECONDS + 0.01)
        third = self.handoff(
            source, candidate, journal_path=journal, repoint=repoint, canary=canary)(
            "zenbook", "dell", TOKEN, BASE)
        self.assert_no_lease_id(third)
        self.assertFalse(third["ok"])
        self.assertEqual(third["status"], "no_main")
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")
        self.assertIsNone(self.store.status()["main"])
        self.assertIsNone(self.store.status()["transition"])
        row = self.store._db.execute(
            "SELECT state FROM transitions WHERE transition_id=?",
            (first["transition_id"],)).fetchone()
        self.assertEqual(row["state"], "FAILED")
        self.assertEqual(calls, {"brain": 1, "repoint": 1, "canary": 0})
        saved = journal.read_text(encoding="utf-8")
        self.assertNotIn("lease_id", saved)
        self.assertNotIn("ls-", saved)

    def test_06_accept_lease_clears_renewal_pause(self):
        runtime = self.seat()
        expires = self.store._lease["expires"]
        renews = self.port.renews
        runtime.pause_renewal()
        paused = runtime.renew_once()
        self.assertEqual(paused, {"code": "renewal_paused", "wait": None})
        self.assertEqual(set(paused), {"code", "wait"})
        self.assertEqual(self.port.renews, renews)
        self.assertEqual(self.store._lease["expires"], expires)
        runtime.accept_lease(self.lease)
        renewed = runtime.renew_once()
        self.assertEqual(renewed["code"], "ok")
        self.assertGreater(self.port.renews, renews)
        self.assertGreaterEqual(self.store._lease["expires"], expires)


class _StepPort:
    """Counts owner steps and can fail complete before or after the store commits."""

    def __init__(self, inner, complete_at=None):
        self.inner = inner
        self.complete_at = complete_at
        self.steps = []
        self.completes = 0

    def transition_step(self, transition_id, step, canary_ok=None):
        self.steps.append(step)
        if step != "complete":
            return self.inner.transition_step(transition_id, step, canary_ok)
        self.completes += 1
        if self.complete_at == "before" and self.completes == 1:
            raise LinkError("partition")
        result = self.inner.transition_step(transition_id, step, canary_ok)
        if self.complete_at == "after" and self.completes == 1:
            raise LinkError("partition")
        return result

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _Crash(BaseException):
    """Stands in for a process death after the canary has been marked started."""


class ManualRecoveryReceiptTests(unittest.TestCase):
    """Same client key replays the authority outcome. Canary and complete do not run twice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = open_store(Path(self.tmp.name) / "authority.sqlite", self.clock)
        self.addCleanup(self.store.close)
        from jarvis.continuity.runtime import StoreAdapter
        self.port = _StepPort(StoreAdapter(self.store))
        cutover(self.store)
        self.receipts = Path(self.tmp.name) / "receipts.json"
        self.canary_calls = 0
        self.runtime = MainRuntime(
            self.port, "dell", "dell-inst",
            lambda: {"control_ready": True, "checkpoint": "cp-1"},
            clock=self.clock.mono, boot=lambda: "boot-a")

    def canary(self, device):
        self.canary_calls += 1
        return True

    def recover(self, canary=None, port=None):
        return ManualRecovery(
            port or self.port, self.runtime, canary or self.canary, receipts=self.receipts)

    def assert_no_lease(self, result=None):
        if result is not None:
            encoded = json.dumps(result)
            self.assertNotIn("lease_id", encoded)
            self.assertNotIn("ls-", encoded)
        if self.receipts.exists():
            text = self.receipts.read_text(encoding="utf-8")
            self.assertNotIn("lease_id", text)
            self.assertNotIn("ls-", text)
            self.assertEqual(self.receipts.stat().st_mode & 0o777, 0o600)

    def test_same_key_replays_ready_without_a_second_canary(self):
        first = self.recover()("dell", "req-ready")
        self.assert_no_lease(first)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["status"], "ready")
        self.assertNotIn("already", first)
        self.assertEqual(self.canary_calls, 1)
        self.assertEqual(self.store.status()["mode"], "V2_ACTIVE")
        epoch = self.store.status()["epoch"]
        second = self.recover(lambda device: self.fail("canary replayed"))("dell", "req-ready")
        self.assert_no_lease(second)
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["status"], "ready")
        self.assertTrue(second["already"])
        self.assertEqual(second["transition_id"], first["transition_id"])
        self.assertEqual(second["epoch"], first["epoch"])
        self.assertEqual(self.store.status()["epoch"], epoch)
        self.assertEqual(self.canary_calls, 1)
        self.assertEqual(self.port.completes, 1)

    def test_failed_canary_rollback_is_replayed_not_retried(self):
        def fail(device):
            self.canary_calls += 1
            return False

        first = self.recover(fail)("dell", "req-rollback")
        self.assert_no_lease(first)
        self.assertFalse(first["ok"])
        self.assertEqual(first["status"], "no_main")
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")
        self.assertFalse(self.store.status()["lease_valid"])
        epoch = self.store.status()["epoch"]
        second = self.recover(lambda device: self.fail("canary replayed"))("dell", "req-rollback")
        self.assert_no_lease(second)
        self.assertFalse(second["ok"])
        self.assertEqual(second["status"], "no_main")
        self.assertTrue(second["already"])
        self.assertEqual(second["transition_id"], first["transition_id"])
        self.assertEqual(self.store.status()["epoch"], epoch)
        self.assertEqual(self.canary_calls, 1)
        self.assertEqual(self.port.completes, 1)
        self.assertIsNone(self.store.status()["transition"])

    def test_new_key_can_recover_after_rollback(self):
        self.recover(lambda device: False)("dell", "req-fail")
        ack(self.store)
        fresh = self.recover()("dell", "req-again")
        self.assert_no_lease(fresh)
        self.assertTrue(fresh["ok"], fresh)
        self.assertEqual(fresh["status"], "ready")
        self.assertEqual(self.store.status()["main"], "dell")
        self.assertEqual(self.canary_calls, 1)

    def test_complete_lost_after_commit_returns_the_ready_receipt(self):
        port = _StepPort(self.port.inner, complete_at="after")
        self.runtime._port = port
        result = self.recover(port=port)("dell", "req-lost-after")
        self.assert_no_lease(result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("already", result)
        self.assertEqual(self.canary_calls, 1)
        self.assertEqual(port.completes, 1)
        self.assertEqual(self.store.status()["mode"], "V2_ACTIVE")
        again = self.recover(lambda device: self.fail("canary replayed"), port=port)(
            "dell", "req-lost-after")
        self.assertTrue(again["already"])
        self.assertTrue(again["ok"], again)
        self.assertEqual(port.completes, 1)
        self.assertEqual(self.canary_calls, 1)

    def test_complete_lost_before_commit_is_not_a_rollback(self):
        port = _StepPort(self.port.inner, complete_at="before")
        self.runtime._port = port
        with self.assertRaises(LinkError) as caught:
            self.recover(port=port)("dell", "req-lost-before")
        self.assertEqual(caught.exception.code, "partition")
        self.assertEqual(self.canary_calls, 1)
        self.assertEqual(port.completes, 1)
        held = self.store.status()
        self.assertEqual(held["transition"]["state"], "GRANTED_NOT_READY")
        self.assertTrue(held["lease_valid"])
        self.assertFalse(held["main_ready"])
        self.assertNotIn("cancel", port.steps)
        port.complete_at = None
        result = self.recover(lambda device: self.fail("canary replayed"), port=port)(
            "dell", "req-lost-before")
        self.assert_no_lease(result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.canary_calls, 1)
        self.assertEqual(port.completes, 2)
        self.assertEqual(self.store.status()["mode"], "V2_ACTIVE")

    def test_canary_that_does_not_return_stays_uncertain(self):
        def crash(device):
            self.canary_calls += 1
            raise _Crash()

        with self.assertRaises(_Crash):
            self.recover(crash)("dell", "req-uncertain")
        self.assertEqual(self.canary_calls, 1)
        held = self.store.status()
        self.assertEqual(held["transition"]["state"], "GRANTED_NOT_READY")
        self.assertTrue(held["lease_valid"])
        self.assertNotIn("cancel", self.port.steps)
        self.assert_no_lease()
        result = self.recover(lambda device: self.fail("canary replayed"))("dell", "req-uncertain")
        self.assert_no_lease(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "uncertain")
        self.assertNotIn("already", result)
        self.assertEqual(self.store.status()["transition"]["state"], "GRANTED_NOT_READY")
        self.assertEqual(self.canary_calls, 1)
        self.assertNotIn("complete", self.port.steps)
        self.assertNotIn("cancel", self.port.steps)

    def test_accept_that_lands_is_not_cancelled(self):
        def accept(lease):
            MainRuntime.accept_lease(self.runtime, lease)
            raise RuntimeRefusal("partition")

        self.runtime.accept_lease = accept
        with self.assertRaises(RuntimeRefusal) as caught:
            self.recover()("dell", "req-accept")
        self.assertEqual(caught.exception.code, "partition")
        self.assertEqual(self.store.status()["transition"]["state"], "GRANTED_NOT_READY")
        self.assertNotIn("cancel", self.port.steps)
        self.assertEqual(self.port.steps.count("grant"), 1)
        self.runtime.accept_lease = lambda lease: self.fail("accept replayed")
        result = self.recover()("dell", "req-accept")
        self.assert_no_lease(result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.port.steps.count("grant"), 1)
        self.assertEqual(self.canary_calls, 1)
        self.assertEqual(self.store.status()["mode"], "V2_ACTIVE")

    def test_failed_accept_replays_no_main_on_the_real_store(self):
        def miss(lease):
            raise RuntimeRefusal("partition")

        self.runtime.accept_lease = miss
        with self.assertRaises(RuntimeRefusal) as caught:
            self.recover()("dell", "req-missed")
        self.assertEqual(caught.exception.code, "partition")
        self.assertIn("cancel", self.port.steps)
        self.assertIsNone(self.store.status()["transition"])
        self.assertFalse(self.store.status()["lease_valid"])
        epoch = self.store.status()["epoch"]
        result = self.recover(lambda device: self.fail("canary after missed accept"))(
            "dell", "req-missed")
        self.assert_no_lease(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_main")
        self.assertTrue(result["already"])
        self.assertEqual(self.store.status()["epoch"], epoch)
        self.assertEqual(self.canary_calls, 0)
        self.assertEqual(self.port.completes, 0)

    def test_spent_key_without_the_receipt_file_does_not_open_another_effect(self):
        ManualRecovery(self.port, self.runtime, self.canary)("dell", "req-untracked")
        self.assertEqual(self.canary_calls, 1)
        epoch = self.store.status()["epoch"]
        fresh = MainRuntime(
            self.port, "dell", "dell-inst",
            lambda: {"control_ready": True, "checkpoint": "cp-1"},
            clock=self.clock.mono, boot=lambda: "boot-a")
        with self.assertRaises(RuntimeRefusal) as caught:
            ManualRecovery(self.port, fresh, lambda device: self.fail("second effect"))(
                "dell", "req-untracked")
        self.assertEqual(caught.exception.code, "key_reused")
        self.assertEqual(self.store.status()["epoch"], epoch)
        self.assertEqual(self.store.status()["mode"], "V2_ACTIVE")
        self.assertEqual(self.canary_calls, 1)
        self.assertIsNone(self.store.status()["transition"])

    def test_receipt_carrying_a_lease_id_is_ignored(self):
        self.receipts.write_text(json.dumps({"req-leak": {
            "schema": 1, "client_key": "req-leak", "candidate": "dell",
            "expected_incarnation": 1, "expected_epoch": 0, "expected_policy": 1,
            "canary_started": True, "canary": True, "lease_id": "ls-secret"}}), encoding="utf-8")
        result = self.recover()("dell", "req-leak")
        self.assert_no_lease(result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.canary_calls, 1)
        self.assertNotIn("ls-secret", self.receipts.read_text(encoding="utf-8"))
