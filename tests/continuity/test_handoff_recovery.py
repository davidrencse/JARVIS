"""A link blip or a raised hook must not cancel or stick a not-ready grant.

Fixtures and the acking runtime come from the safe-handoff tests. This module
does not change those tests.
"""

import json
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity.authority import LEASE_SECONDS
from jarvis.continuity.runtime import LinkError, MainRuntime, SafeHandoff
from tests.continuity._fleet import Clock, ack, cutover, open_store, promote
from tests.continuity.test_safe_handoff import TOKEN, BASE, AckingRuntime, _facts


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


class HandoffRecoveryTests(unittest.TestCase):
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

    def pc(self, device, cls=MainRuntime):
        return cls(
            self.port, device, device + "-inst",
            lambda: {"control_ready": True, "checkpoint": "cp-1"},
            clock=self.clock.mono, boot=lambda: "boot-a")

    def seat(self, device="zenbook", cls=MainRuntime):
        runtime = self.pc(device, cls)
        if device == "zenbook":
            runtime.accept_lease(self.lease)
        runtime.start()
        return runtime

    def use_brain(self, fn):
        import sys
        import types
        previous = sys.modules.get("jarvis.brainsync")
        module = types.ModuleType("jarvis.brainsync")
        module.handoff = fn
        sys.modules["jarvis.brainsync"] = module

        def restore():
            if previous is None:
                sys.modules.pop("jarvis.brainsync", None)
            else:
                sys.modules["jarvis.brainsync"] = previous

        self.addCleanup(restore)

    def brain(self, old, new, config, base):
        self.events.append("brain")
        return {"ok": True, "status": "synced"}

    def repoint(self, old, new, lease):
        self.events.append("repoint")
        return _confirmed(old, new, lease)

    def canary(self, new):
        self.events.append("canary")
        return True

    def handoff(self, source, candidate, **kwargs):
        return SafeHandoff(
            self.port, source_runtime=source, candidate_runtime=candidate,
            readiness=kwargs.get("readiness", _facts),
            repoint=kwargs.get("repoint", self.repoint),
            canary=kwargs.get("canary", self.canary),
            journal_path=kwargs.get("journal_path"))

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
