"""Manual SafeHandoff against the real authority store and a loopback gateway.

The in-process cases drive ``StoreAdapter``. The loopback case runs the authority
in one process and two ``MainRuntime`` processes in two others, with installation
keys the gateway actually verifies. Each test installs an explicit stub
``jarvis.brainsync.handoff``. The real storage hook is not the code under test.
"""

import inspect
import json
import os
import select
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from jarvis.continuity.authority import LEASE_SECONDS, AuthorityError
from jarvis.continuity.runtime import (
    LinkError,
    MainRuntime,
    RuntimeRefusal,
    SafeHandoff,
    StoreAdapter,
    _repoint_ok,
)
from tests.continuity._fleet import Clock, ack, cutover, open_store, promote, ticket


TOKEN = "HANDOFF-TOKEN-7f3a"
BASE = "HANDOFF-BASE-7f3a"
_MISSING = object()
PC = ["status", "lease", "fence", "candidate", "consume", "reconcile"]
SERVER = ["status", "fence", "reconcile"]


def _facts(candidate):
    return {"control_ready": True, "ai_ready": False, "checkpoint": "cp-1"}


def _confirmed(old, new, lease):
    return {"ok": True, "authenticated": True, "key_updated": True, "endpoint_updated": True}


def _canary(_new):
    return True


class AckingRuntime(MainRuntime):
    """A draining PC that acknowledges the epoch it is leaving."""

    def begin_drain(self, transition_id):
        report = super().begin_drain(transition_id)
        self.acknowledge_current()
        return report


class StopDrain(MainRuntime):
    def begin_drain(self, transition_id):
        raise RuntimeRefusal("stopped")


def _secret_keys(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"lease_id", "secret", "password", "ticket_signature", "private_key"}:
                found.append(key)
            found.extend(_secret_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_secret_keys(item))
    return found


class SafeHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "authority.sqlite"
        self.clock = Clock()
        self.store = open_store(self.db, self.clock)
        self.addCleanup(self._close_store)
        self.port = StoreAdapter(self.store)
        cutover(self.store)
        _tid, self.lease = promote(self.store, "recover", "zenbook", "k-boot")
        self.start_epoch = self.store.status()["epoch"]
        self.events = []
        self.brain_status = []
        self.repoint_lease = []

    def _close_store(self):
        try:
            self.store.close()
        except Exception:
            pass

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
        previous = sys.modules.get("jarvis.brainsync", _MISSING)
        module = types.ModuleType("jarvis.brainsync")
        module.handoff = fn
        sys.modules["jarvis.brainsync"] = module

        def restore():
            if previous is _MISSING:
                sys.modules.pop("jarvis.brainsync", None)
            else:
                sys.modules["jarvis.brainsync"] = previous

        self.addCleanup(restore)
        return module

    def brain(self, old, new, config, base):
        self.events.append("brain")
        self.brain_status.append(self.store.status())
        self.assertEqual((old, new, config, base), ("zenbook", "dell", TOKEN, BASE))
        return {"ok": True, "status": "synced"}

    def repoint(self, old, new, lease):
        self.events.append("repoint")
        self.repoint_lease.append(dict(lease))
        self.assertEqual((old, new), ("zenbook", "dell"))
        return _confirmed(old, new, lease)

    def canary(self, new):
        self.events.append("canary")
        self.assertEqual(new, "dell")
        return True

    def handoff(self, source, candidate, port=None, **kwargs):
        readiness = kwargs.get("readiness", _facts)
        repoint = kwargs.get("repoint", self.repoint)
        canary = kwargs.get("canary", self.canary)
        return SafeHandoff(
            port or self.port, source_runtime=source, candidate_runtime=candidate,
            readiness=readiness, repoint=repoint, canary=canary,
            journal_path=kwargs.get("journal_path"))

    def refuse_store(self, fn, code):
        with self.assertRaises(AuthorityError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def disk(self):
        blob = self.db.read_bytes()
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.db) + suffix)
            if side.exists():
                blob += side.read_bytes()
        return blob

    def assert_public(self, result):
        self.assertEqual(_secret_keys(result), [])
        encoded = json.dumps(result)
        self.assertNotIn("ls-", encoded)
        self.assertNotIn(TOKEN, encoded)
        self.assertNotIn(TOKEN.encode(), self.disk())
        self.assertNotIn(BASE.encode(), self.disk())

    def test_01_success_switches_main_and_refuses_the_old_lease(self):
        self.assertNotIn("force", inspect.signature(SafeHandoff.__init__).parameters)
        self.assertNotIn("force", inspect.signature(SafeHandoff.__call__).parameters)
        self.use_brain(self.brain)
        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        result = self.handoff(arch, dell)("zenbook", "dell", TOKEN, BASE)
        self.assert_public(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["code"], "new_main")
        self.assertEqual(result["action"], "set_preferred_main")
        self.assertEqual(result["main"], "dell")
        self.assertGreater(result["epoch"], self.start_epoch)
        status = self.store.status()
        self.assertEqual((status["main"], status["main_ready"], status["preferred"], status["mode"]),
                         ("dell", True, "dell", "V2_ACTIVE"))
        self.assertEqual(status["epoch"], result["epoch"])
        self.assertIsNone(status["transition"])
        seen = self.brain_status[0]
        self.assertEqual(seen["transition"]["state"], "GRANTED_NOT_READY")
        self.assertEqual(seen["main"], "dell")
        self.assertFalse(seen["main_ready"])
        self.assertFalse(seen["transition"]["ai_ready"])
        self.assertGreater(seen["epoch"], self.start_epoch)
        self.assertEqual(self.repoint_lease[0]["holder"], "dell")
        self.assertGreater(self.repoint_lease[0]["epoch"], self.start_epoch)
        self.assertEqual(self.events, ["brain", "repoint", "canary"])
        again = self.handoff(arch, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(again["ok"])
        self.assertEqual((again["status"], again["code"]), ("already_main", "already_main"))
        self.assertEqual(again["epoch"], result["epoch"])
        self.assertEqual(self.events, ["brain", "repoint", "canary"])
        dell.accept_lease(self.repoint_lease[0])
        self.assertEqual(dell.require_authority("control")["holder"], "dell")
        stale = arch.renew_once()
        self.assertEqual(stale["code"], "stale")
        self.assertEqual(set(stale), {"code", "wait"})
        with self.assertRaises(RuntimeRefusal) as caught:
            arch.require_authority("control")
        self.assertEqual(caught.exception.code, "not_holder")
        body = ticket(self.store, self.lease, self.clock, op="op-old", body=TOKEN)
        with self.assertRaises(AuthorityError) as caught:
            self.store.consume(**body)
        self.assertEqual(caught.exception.code, "stale")
        self.assertNotIn(TOKEN.encode(), self.disk())
        self.assertFalse(Path(BASE).exists())
        self.assertFalse((Path("/home/jdoe/projects/JARVIS") / ".jarvis" / "handoff-journal.json").exists())

    def test_02_live_old_cannot_recover(self):
        self.use_brain(self.brain)
        arch = self.seat()
        dell = self.seat("dell")
        blocked = self.handoff(None, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["code"], "source_required")
        self.assertIsNone(self.store.status()["transition"])
        self.assertEqual(self.store.status()["main"], "zenbook")
        status = self.store.status()
        self.refuse_store(lambda: self.store.request(
            "owner-pc", "k-live", "recover", "dell", status["incarnation"], status["epoch"],
            status["policy_version"]), "main_alive")
        self.assertIsNone(self.store.status()["transition"])
        stopped = self.seat(cls=StopDrain)
        refused = self.handoff(stopped, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["code"], "stopped")
        opened = self.store.status()["transition"]
        self.assertEqual(opened["action"], "set_preferred_main")
        self.assertEqual(opened["state"], "DRAINING")
        self.assertNotEqual(opened["action"], "recover")
        self.assertEqual(self.events, [])
        cancelled = self.store.cancel(opened["transition_id"])
        self.assertEqual(cancelled["outcome"], "old_main_kept")
        self.assertEqual(self.store.status()["main"], "zenbook")
        self.assertEqual(arch.renew_once()["code"], "ok")
        self.assertEqual(arch.require_authority("ordinary")["holder"], "zenbook")

    def test_03_expired_recovery_without_source_runtime(self):
        self.use_brain(self.brain)
        arch = self.seat()
        dell = self.seat("dell")
        self.clock.tick(LEASE_SECONDS + 0.01)
        self.assertIsNone(self.store.status()["main"])
        self.assertFalse(self.store.status()["lease_valid"])
        ack(self.store)
        result = self.handoff(None, dell)("zenbook", "dell", TOKEN, BASE)
        self.assert_public(result)
        self.assertTrue(result["ok"])
        self.assertEqual((result["action"], result["main"], result["code"]), ("recover", "dell", "new_main"))
        status = self.store.status()
        self.assertEqual((status["main"], status["main_ready"], status["preferred"]), ("dell", True, None))
        self.assertGreater(status["epoch"], self.start_epoch)
        self.assertEqual(self.brain_status[0]["transition"]["action"], "recover")
        self.assertEqual(arch.renew_once()["code"], "stale")
        dell.accept_lease(self.repoint_lease[0])
        self.assertEqual(dell.require_authority("control")["holder"], "dell")

    def test_04_failed_brain_or_repoint_leaves_no_main(self):
        self.assertFalse(_repoint_ok({"ok": True}))
        self.assertFalse(_repoint_ok({
            "ok": True, "authenticated": True, "key_updated": True, "endpoint_updated": True,
            "config_only": True}))
        self.assertTrue(_repoint_ok({
            "ok": True, "authenticated": True, "key_updated": True, "endpoint_updated": True}))
        module = self.use_brain(self.brain)
        steps = _Recording(self.port)
        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        saved = sys.modules["jarvis.brainsync"]
        sys.modules["jarvis.brainsync"] = None
        try:
            missing = self.handoff(arch, dell, port=steps)("zenbook", "dell", TOKEN, BASE)
        finally:
            sys.modules["jarvis.brainsync"] = saved
        self.assertEqual(missing["code"], "missing_hook")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["status"], "no_main")
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")
        self.assertEqual(arch.renew_once()["code"], "stale")
        self.assertNotIn(("complete", True), steps.steps)
        self.assertNotIn(("complete", False), steps.steps)
        self.assertIn("cancel", [step for step, _ok in steps.steps])

        ack(self.store)
        module.handoff = lambda *args: {"ok": False, "status": "unavailable"}
        brain_failed = self.handoff(None, dell, port=steps)("zenbook", "dell", TOKEN, BASE)
        self.assertEqual(brain_failed["code"], "brain")
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")

        ack(self.store)
        module.handoff = self.brain
        bare = self.handoff(None, dell, port=steps, repoint=lambda *args: {"ok": True})(
            "zenbook", "dell", TOKEN, BASE)
        self.assertEqual(bare["code"], "repoint")
        self.assertIsNone(self.store.status()["main"])
        self.assertTrue(self.events.count("brain") >= 1)
        self.assertNotIn("repoint", self.events)
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")

        ack(self.store)
        self.events.clear()
        failed_canary = self.handoff(None, dell, port=steps, canary=lambda _new: False)(
            "zenbook", "dell", TOKEN, BASE)
        self.assertEqual(failed_canary["code"], "canary")
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")
        self.assertEqual(self.events, ["brain", "repoint"])
        self.assertNotIn(("complete", False), steps.steps)
        self.assertFalse(self.store.status()["lease_valid"])
        self.assert_public(failed_canary)

    def test_05_pending_fence_resumes_the_same_transition(self):
        self.use_brain(self.brain)
        ack(self.store, ("dell", "oracle"))
        arch = self.seat()
        dell = self.seat("dell")
        first = self.handoff(arch, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(first["ok"])
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["code"], "not_quiet")
        self.assertGreater(first["retry_after"], 10)
        self.assertGreater(first["retry_after"], self.store.margin)
        status = self.store.status()
        self.assertEqual(status["mode"], "HANDOVER")
        self.assertFalse(status["lease_valid"])
        self.assertIsNone(status["lease_holder"])
        self.assertEqual(status["transition"]["state"], "DRAINING")
        self.assertEqual(status["transition"]["transition_id"], first["transition_id"])
        self.assertEqual(status["transition"]["action"], "set_preferred_main")
        self.assertEqual(self.events, [])
        self.assertIsNone(status["main"])
        arch.acknowledge_current()
        second = self.handoff(arch, dell)("zenbook", "dell", TOKEN, BASE)
        self.assert_public(second)
        self.assertTrue(second["ok"])
        self.assertEqual(second["transition_id"], first["transition_id"])
        self.assertEqual(second["main"], "dell")
        self.assertGreater(second["epoch"], first["epoch"])
        self.assertEqual(self.store.status()["main_ready"], True)
        self.assertEqual(len(self.brain_status), 1)
        rows = self.store._db.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
        self.assertEqual(rows, 2)  # boot recover plus this one handoff

    def test_06_release_is_narrow_and_bad_input_does_not_promote(self):
        self.use_brain(self.brain)
        arch = self.seat()
        dell = self.seat("dell")
        mismatch = self.handoff(arch, dell)("zenbook", "pixel", TOKEN, BASE)
        self.assertEqual(mismatch["code"], "candidacy_mismatch")
        self.assertIsNone(self.store.status()["transition"])
        swapped = self.handoff(dell, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertEqual(swapped["code"], "candidacy_mismatch")
        self.assertIsNone(self.store.status()["transition"])
        for raw, code in (
                ({}, "not_ready"),
                ({"control_ready": True}, "not_ready"),
                ({"control_ready": 1, "ai_ready": True}, "not_ready"),
                ({"control_ready": True, "ai_ready": False, "checkpoint": "free text"}, "bad_checkpoint"),
                ({"control_ready": True, "ai_ready": False, "prompt": "x"}, "bad_request")):
            refused = self.handoff(arch, dell, readiness=lambda _candidate, raw=raw: raw)(
                "zenbook", "dell", TOKEN, BASE)
            self.assertEqual(refused["code"], code, raw)
            self.assertIsNone(self.store.status()["transition"])
            self.assertEqual(self.store.status()["main"], "zenbook")
        status = self.store.status()
        other = self.store.request(
            "owner-pc", "k-other", "make_main_now", "dell", status["incarnation"], status["epoch"],
            status["policy_version"])
        conflict = self.handoff(arch, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["transition_id"], other["transition_id"])
        self.assertEqual(self.store.transition(other["transition_id"])["state"], "REQUESTED")
        self.store.cancel(other["transition_id"])
        self.assertEqual(arch.renew_once()["code"], "ok")

        status = self.store.status()
        requested = self.store.request(
            "safe-handoff", "sh-release", "set_preferred_main", "dell", status["incarnation"],
            status["epoch"], status["policy_version"])
        self.refuse_store(lambda: self.store.release(
            self.lease["holder"], self.lease["lease_id"], self.lease["incarnation"], self.lease["epoch"]), "busy")
        self.assertEqual(self.store.status()["main"], "zenbook")
        self.store.readiness(requested["transition_id"], True, False, "cp-1")
        self.refuse_store(lambda: self.store.release(
            self.lease["holder"], self.lease["lease_id"], self.lease["incarnation"], self.lease["epoch"]), "busy")
        self.assertTrue(self.store.status()["lease_valid"])
        self.store.drain(requested["transition_id"])
        self.store.release(
            self.lease["holder"], self.lease["lease_id"], self.lease["incarnation"], self.lease["epoch"])
        drained = self.store.status()
        self.assertEqual(drained["mode"], "HANDOVER")
        self.assertEqual(drained["transition"]["state"], "DRAINING")
        self.assertEqual(drained["transition"]["transition_id"], requested["transition_id"])
        self.assertIsNone(drained["lease_holder"])
        self.assertFalse(drained["lease_valid"])
        waiting = self.refuse_store(lambda: self.store.fence(requested["transition_id"]), "not_quiet")
        self.assertGreater(waiting.wait, 10)
        self.assertGreater(waiting.wait, self.store.margin)
        cancelled = self.store.cancel(requested["transition_id"])
        self.assertEqual(cancelled["outcome"], "cancelled_after_release")
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")
        self.refuse_store(lambda: self.store.renew(
            self.lease["holder"], self.lease["lease_id"], self.lease["incarnation"], self.lease["epoch"]), "stale")

        ack(self.store)
        status = self.store.status()
        granted_id = self.store.request(
            "owner-pc", "k-grant", "recover", "dell", status["incarnation"], status["epoch"],
            status["policy_version"])["transition_id"]
        self.store.readiness(granted_id, True, False, "cp-1")
        self.store.drain(granted_id)
        self.store.fence(granted_id)
        granted = self.store.grant(granted_id)
        self.assertGreater(granted["epoch"], self.lease["epoch"])
        self.refuse_store(lambda: self.store.release(
            granted["holder"], granted["lease_id"], granted["incarnation"], granted["epoch"]), "busy")
        self.assertEqual(self.store.status()["main"], "dell")
        self.assertTrue(self.store.status()["lease_valid"])
        self.store.cancel(granted_id)
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")

    def test_07_noop_unready_and_authority_status_partition(self):
        dell = self.seat("dell")
        live = self.handoff(self.seat(), dell)
        status = live.authority_status()
        self.assertEqual(status["main"], "zenbook")
        self.assertNotIn("lease_id", status)

        class _View:
            def __init__(self, inner):
                self.inner = inner

            def status(self):
                body = dict(self.inner.status())
                body["main"] = "dell"
                body["lease_holder"] = "dell"
                body["lease_valid"] = True
                body["main_ready"] = False
                body["mode"] = "V2_ACTIVE"
                body["transition"] = None
                return body

            def release(self, *_args, **_kwargs):
                raise AssertionError("release")

            def transition_request(self, *_args, **_kwargs):
                raise AssertionError("request")

            def transition_readiness(self, *_args, **_kwargs):
                raise AssertionError("readiness")

            def transition_step(self, *_args, **_kwargs):
                raise AssertionError("step")

        unready = self.handoff(self.seat(), dell, port=_View(self.port))(
            "zenbook", "dell", TOKEN, BASE)
        self.assertFalse(unready["ok"])
        self.assertEqual(unready["status"], "not_ready")
        self.assertEqual(unready["code"], "not_ready")
        self.assertEqual(self.events, [])
        self.assertIsNone(self.store.status()["transition"])
        self.assertEqual(self.store.status()["main"], "zenbook")

        class _Down:
            def status(self):
                raise LinkError("partition")

            def release(self, *_args, **_kwargs):
                raise AssertionError("release")

            def transition_request(self, *_args, **_kwargs):
                raise AssertionError("request")

            def transition_readiness(self, *_args, **_kwargs):
                raise AssertionError("readiness")

            def transition_step(self, *_args, **_kwargs):
                raise AssertionError("step")

        down = self.handoff(None, dell, port=_Down())
        with self.assertRaises(RuntimeRefusal) as caught:
            down.authority_status()
        self.assertEqual(caught.exception.code, "partition")

    def test_08_lost_grant_does_not_guess_a_lease(self):
        self.use_brain(self.brain)
        ack(self.store, ("dell", "oracle"))
        arch = _PublicRuntime(self.seat(cls=AckingRuntime))
        dell = _PublicRuntime(self.seat("dell"))
        port = _DropResponse(self.port, "grant")
        result = self.handoff(arch, dell, port=port)("zenbook", "dell", TOKEN, BASE)
        self.assert_public(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_main")
        self.assertEqual(result["code"], "lease_unavailable")
        self.assertEqual(self.events, [])
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")
        self.assertFalse(self.store.status()["lease_valid"])
        self.assertEqual(port.calls.count("grant"), 1)

    def test_09_lost_complete_reconciles_the_same_epoch(self):
        self.use_brain(self.brain)
        journal = Path(self.tmp.name) / "complete.json"
        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        port = _DropResponse(self.port, "complete")
        result = self.handoff(arch, dell, port=port, journal_path=journal)(
            "zenbook", "dell", TOKEN, BASE)
        self.assert_public(result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["code"], "new_main")
        status = self.store.status()
        self.assertEqual((status["main"], status["main_ready"], status["mode"]), ("dell", True, "V2_ACTIVE"))
        self.assertEqual(status["epoch"], result["epoch"])
        self.assertIsNone(status["transition"])
        self.assertEqual(self.events, ["brain", "repoint", "canary"])
        self.assertEqual(port.calls.count("complete"), 1)
        rows = self.store._db.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
        self.assertEqual(rows, 2)
        text = journal.read_text(encoding="utf-8")
        self.assertNotIn("lease_id", text)
        self.assertNotIn("ls-", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(BASE, text)
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        again = self.handoff(arch, dell, journal_path=journal)("zenbook", "dell", TOKEN, BASE)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["code"], "new_main")
        self.assertEqual(again["epoch"], result["epoch"])
        self.assertEqual(again["transition_id"], result["transition_id"])
        self.assertEqual(again["action"], result["action"])
        self.assertEqual(self.events, ["brain", "repoint", "canary"])
        self.assertEqual(port.calls.count("complete"), 1)

    def test_10_started_callback_is_not_replayed(self):
        journal = Path(self.tmp.name) / "callbacks.json"
        calls = {"brain": 0, "repoint": 0}

        def brain(old, new, config, base):
            calls["brain"] += 1
            self.assertEqual((old, new, config, base), ("zenbook", "dell", TOKEN, BASE))
            return {"ok": True, "status": "synced"}

        def repoint(old, new, lease):
            calls["repoint"] += 1
            self.assertEqual(lease["holder"], "dell")
            raise RuntimeError("lost")

        self.use_brain(brain)
        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        first = self.handoff(arch, dell, journal_path=journal, repoint=repoint)(
            "zenbook", "dell", TOKEN, BASE)
        self.assertFalse(first["ok"])
        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(first["code"], "reconcile")
        self.assertEqual(calls, {"brain": 1, "repoint": 1})
        held = self.store.status()
        self.assertEqual(held["transition"]["state"], "GRANTED_NOT_READY")
        self.assertEqual(held["main"], "dell")
        self.assertFalse(held["main_ready"])
        text = journal.read_text(encoding="utf-8")
        self.assertNotIn("lease_id", text)
        self.assertNotIn("ls-", text)
        self.assertNotIn(TOKEN, text)
        fresh = self.handoff(arch, dell, journal_path=journal, repoint=repoint)(
            "zenbook", "dell", TOKEN, BASE)
        self.assertEqual(fresh["status"], "uncertain")
        self.assertEqual(fresh["code"], "reconcile")
        self.assertEqual(calls, {"brain": 1, "repoint": 1})
        self.assertEqual(self.store.status()["epoch"], held["epoch"])
        self.assertEqual(self.store.status()["transition"]["transition_id"], held["transition"]["transition_id"])
        rows = self.store._db.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
        self.assertEqual(rows, 2)

    def test_11_stale_grant_does_not_replay(self):
        self.use_brain(self.brain)
        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        status = self.store.status()
        opened = self.store.request(
            "owner-pc", "k-stale-grant", "set_preferred_main", "dell",
            status["incarnation"], status["epoch"], status["policy_version"])
        tid = opened["transition_id"]
        self.store.readiness(tid, True, False, "cp-1")
        self.store.drain(tid)
        arch.begin_drain(tid)
        self.store.release(
            self.lease["holder"], self.lease["lease_id"], self.lease["incarnation"], self.lease["epoch"])
        self.store.fence(tid)
        granted = self.store.grant(tid)
        dell.accept_lease(granted)
        self.clock.tick(LEASE_SECONDS + 0.01)
        self.assertFalse(self.store.status()["lease_valid"])
        result = self.handoff(arch, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["status"], "ready")
        self.assertEqual(self.events, [])
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")

    def test_12_source_race_does_not_override_the_fence(self):
        self.use_brain(self.brain)
        blind = self.seat(cls=_BlindSource)
        dell = self.seat("dell")
        missing = self.handoff(blind, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "no_lease")
        status = self.store.status()
        self.assertTrue(status["lease_valid"])
        self.assertEqual(status["main"], "zenbook")
        self.assertEqual(status["transition"]["state"], "DRAINING")
        self.assertEqual(status["epoch"], self.start_epoch)
        self.assertEqual(self.events, [])
        cancelled = self.store.cancel(status["transition"]["transition_id"])
        self.assertEqual(cancelled["outcome"], "old_main_kept")
        self.assertEqual(self.store.status()["epoch"], self.start_epoch)

    def test_12b_stale_source_snapshot_does_not_release(self):
        self.use_brain(self.brain)
        stale = self.seat(cls=_StaleSource)
        dell = self.seat("dell")
        result = self.handoff(stale, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "stale")
        self.assertTrue(self.store.status()["lease_valid"])
        self.assertEqual(self.store.status()["main"], "zenbook")
        self.assertEqual(self.store.status()["epoch"], self.start_epoch)
        self.assertEqual(self.events, [])

    def test_12c_expired_source_still_waits_for_the_fence(self):
        self.use_brain(self.brain)
        _ExpireSource.clock = self.clock
        expired = self.seat(cls=_ExpireSource)
        dell = self.seat("dell")
        pending = self.handoff(expired, dell)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(pending["ok"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["code"], "not_quiet")
        self.assertGreater(pending["retry_after"], 0)
        after = self.store.status()
        self.assertNotEqual(after["main"], "dell")
        self.assertFalse(after["main_ready"])
        self.assertEqual(after["transition"]["state"], "DRAINING")
        self.assertEqual(self.events, [])

    def test_13_callback_record_is_not_readiness(self):
        self.use_brain(self.brain)
        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        status = self.store.status()
        opened = self.store.request(
            "owner-pc", "k-record", "set_preferred_main", "dell",
            status["incarnation"], status["epoch"], status["policy_version"])
        tid = opened["transition_id"]
        self.store.readiness(tid, True, False, "cp-1")
        self.store.drain(tid)
        arch.begin_drain(tid)
        self.store.release(
            self.lease["holder"], self.lease["lease_id"], self.lease["incarnation"], self.lease["epoch"])
        self.store.fence(tid)
        granted = self.store.grant(tid)
        dell.accept_lease(granted)
        journal = Path(self.tmp.name) / "poison.json"
        journal.write_text(json.dumps({
            "schema": 1,
            "transition_id": tid,
            "candidate": "dell",
            "action": "set_preferred_main",
            "incarnation": granted["incarnation"],
            "epoch": granted["epoch"],
            "steps": {
                "brain": {"phase": "finished", "ok": True},
                "repoint": {"phase": "finished", "ok": True},
                "canary": {"phase": "finished", "ok": True},
                "complete": {"phase": "finished", "ok": True},
            },
        }), encoding="utf-8")
        result = self.handoff(arch, dell, journal_path=journal)("zenbook", "dell", TOKEN, BASE)
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["code"], "new_main")
        self.assertNotEqual(result["code"], "noop")
        self.assertFalse(self.store.status()["main_ready"])
        self.assertEqual(self.store.status()["transition"]["state"], "GRANTED_NOT_READY")
        self.assertEqual(self.store.status()["epoch"], granted["epoch"])
        self.assertEqual(self.events, [])

    def test_14_default_journal_follows_a_directory_base(self):
        folder = Path(self.tmp.name) / "vault"
        folder.mkdir()

        def brain(old, new, config, base):
            self.assertEqual((old, new, config, Path(base)), ("zenbook", "dell", TOKEN, folder))
            self.events.append("brain")
            return {"ok": True, "status": "synced"}

        self.use_brain(brain)
        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        result = self.handoff(arch, dell)("zenbook", "dell", TOKEN, str(folder))
        self.assertTrue(result["ok"], result)
        journal = folder / ".jarvis" / "handoff-journal.json"
        self.assertTrue(journal.is_file())
        text = journal.read_text(encoding="utf-8")
        self.assertNotIn("lease_id", text)
        self.assertNotIn("ls-", text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        self.assertFalse((Path("/home/jdoe/projects/JARVIS") / ".jarvis" / "handoff-journal.json").exists())

    def test_15_multiprocess_loopback_handoff(self):
        _loopback_handoff(self)

    def test_16_candidate_readiness_is_validated_and_side_effect_free(self):
        calls = []

        class _Port:
            def __init__(self, inner):
                self.inner = inner

            def status(self):
                calls.append("status")
                return self.inner.status()

            def release(self, *args, **kwargs):
                calls.append("release")
                return self.inner.release(*args, **kwargs)

            def transition_request(self, request):
                calls.append("request")
                return self.inner.transition_request(request)

            def transition_readiness(self, transition_id, readiness):
                calls.append("readiness")
                return self.inner.transition_readiness(transition_id, readiness)

            def transition_step(self, transition_id, step, canary_ok=None):
                calls.append(step)
                return self.inner.transition_step(transition_id, step, canary_ok)

        class _Device:
            def __init__(self, device_id):
                self.device_id = device_id

        dell = _Device("dell")
        before = self.disk()
        status = self.store.status()
        seen = []

        def ready(device):
            seen.append(device)
            return {"control_ready": True, "ai_ready": False, "checkpoint": "cp-1"}

        live = self.handoff(None, dell, port=_Port(self.port), readiness=ready)
        self.assertEqual(
            live.candidate_readiness("dell"),
            {"control_ready": True, "ai_ready": False, "checkpoint": "cp-1"})

        def false_flags(device):
            seen.append(device)
            return {"control_ready": False, "ai_ready": False}

        flags = self.handoff(None, dell, port=_Port(self.port), readiness=false_flags)
        self.assertEqual(
            flags.candidate_readiness("dell"),
            {"control_ready": False, "ai_ready": False, "checkpoint": None})

        def malformed(device):
            seen.append(device)
            return {"control_ready": True, "ai_ready": True, "path": "/tmp/nope"}

        bad = self.handoff(None, dell, port=_Port(self.port), readiness=malformed)
        with self.assertRaises(RuntimeRefusal) as caught:
            bad.candidate_readiness("dell")
        self.assertEqual(caught.exception.code, "bad_request")

        def explodes(device):
            seen.append(device)
            raise RuntimeError("probe failed")

        broken = self.handoff(None, dell, port=_Port(self.port), readiness=explodes)
        with self.assertRaises(RuntimeRefusal) as caught:
            broken.candidate_readiness("dell")
        self.assertEqual(caught.exception.code, "not_ready")

        def wrong(_device):
            raise AssertionError("readiness")

        other = self.handoff(None, dell, port=_Port(self.port), readiness=wrong)
        with self.assertRaises(RuntimeRefusal) as caught:
            other.candidate_readiness("zenbook")
        self.assertEqual(caught.exception.code, "candidacy_mismatch")
        self.assertEqual(seen, ["dell", "dell", "dell", "dell"])
        self.assertEqual(calls, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.disk(), before)
        self.assertEqual(self.store.status(), status)
        self.assertIsNone(live._active_journal)

    def test_17_local_config_reaches_brain_and_stays_out_of_the_journal(self):
        secret_path = "/var/jarvis/secret-vault"
        secret_url = "https://handoff.example/secret"
        secret_host = "secret-host.internal"
        config = {"path": secret_path, "url": secret_url, "host": secret_host, "label": "dell"}
        seen = []
        outbound = []

        def brain(old, new, got, base):
            seen.append((old, new, got, base))
            return {"ok": True, "status": "synced"}

        class _Wire:
            def __init__(self, inner):
                self.inner = inner

            def status(self):
                return self.inner.status()

            def release(self, lease):
                outbound.append(lease)
                return self.inner.release(lease)

            def transition_request(self, request):
                outbound.append(request)
                return self.inner.transition_request(request)

            def transition_readiness(self, transition_id, readiness):
                outbound.append({"transition_id": transition_id, "readiness": readiness})
                return self.inner.transition_readiness(transition_id, readiness)

            def transition_step(self, transition_id, step, canary_ok=None):
                outbound.append({"transition_id": transition_id, "step": step, "canary_ok": canary_ok})
                return self.inner.transition_step(transition_id, step, canary_ok)

        self.use_brain(brain)
        journal = Path(self.tmp.name) / "config-journal.json"
        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        result = self.handoff(arch, dell, port=_Wire(self.port), journal_path=journal)(
            "zenbook", "dell", config, BASE)
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen, [("zenbook", "dell", config, BASE)])
        blob = json.dumps(outbound) + journal.read_text(encoding="utf-8")
        for secret in (secret_path, secret_url, secret_host):
            self.assertNotIn(secret, blob)
            self.assertNotIn(secret.encode(), self.disk())

    def test_18_failed_cancel_or_blind_status_is_not_no_main(self):
        self.use_brain(lambda *_args: {"ok": False, "status": "no"})

        class _Hold:
            def __init__(self, inner):
                self.inner = inner

            def transition_step(self, transition_id, step, canary_ok=None):
                if step == "cancel":
                    raise AuthorityError("busy", "holder still live")
                return self.inner.transition_step(transition_id, step, canary_ok)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        held = self.handoff(arch, dell, port=_Hold(self.port))("zenbook", "dell", TOKEN, BASE)
        self.assertEqual(held["status"], "uncertain")
        self.assertNotEqual(held["status"], "no_main")
        self.assertTrue(held["transition_id"])
        self.assertEqual(self.store.status()["main"], "dell")
        self.assertIs(self.store.status()["lease_valid"], True)

    def test_18b_status_down_after_cancel_is_partition(self):
        self.use_brain(lambda *_args: {"ok": False, "status": "no"})

        class _Blind:
            def __init__(self, inner):
                self.inner = inner
                self.hide = False

            def status(self):
                if self.hide:
                    raise LinkError("partition")
                return self.inner.status()

            def transition_step(self, transition_id, step, canary_ok=None):
                result = self.inner.transition_step(transition_id, step, canary_ok)
                if step == "cancel":
                    self.hide = True
                return result

            def __getattr__(self, name):
                return getattr(self.inner, name)

        ack(self.store, ("dell", "oracle"))
        arch = self.seat(cls=AckingRuntime)
        dell = self.seat("dell")
        blind = self.handoff(arch, dell, port=_Blind(self.port))("zenbook", "dell", TOKEN, BASE)
        self.assertEqual(blind["status"], "partition")
        self.assertNotEqual(blind["status"], "no_main")
        self.assertTrue(blind["transition_id"])

    def test_19_two_processes_run_the_callback_once(self):
        folder = Path(self.tmp.name) / "claim"
        folder.mkdir()
        journal = folder / "journal.json"
        counter = folder / "effects.txt"
        counter.write_text("", encoding="utf-8")
        ready = folder / "ready"
        ready.mkdir()
        procs = []
        for name in ("a", "b"):
            proc = _spawn(_CLAIM_RACE, [str(journal), str(counter), str(ready)], folder / f"{name}.err")
            procs.append(proc)
            self.addCleanup(_stop, proc)
        codes = []
        for proc in procs:
            codes.append(proc.wait(timeout=25))
        text = counter.read_text(encoding="utf-8")
        errors = "\n".join((folder / f"{name}.err").read_text(encoding="utf-8")[-1500:] for name in ("a", "b"))
        self.assertEqual(codes, [0, 0], errors)
        self.assertEqual(text.count("brain\n"), 1, text + "\n" + errors)
        self.assertEqual(text.count("repoint\n"), 1, text)
        self.assertEqual(text.count("canary\n"), 1, text)
        saved = journal.read_text(encoding="utf-8")
        self.assertNotIn("ls-hidden", saved)
        self.assertNotIn(TOKEN, saved)


class _DropResponse:
    def __init__(self, inner, step):
        self.inner = inner
        self.step = step
        self.calls = []

    def transition_step(self, transition_id, step, canary_ok=None):
        result = self.inner.transition_step(transition_id, step, canary_ok)
        self.calls.append(step)
        if step == self.step:
            raise LinkError("partition")
        return result

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _PublicRuntime:
    """Fails if a handoff reads the runtime's private lease."""

    def __init__(self, inner):
        self._inner = inner
        self.device_id = inner.device_id

    def begin_drain(self, transition_id):
        return self._inner.begin_drain(transition_id)

    def accept_lease(self, lease):
        return self._inner.accept_lease(lease)

    def lease_snapshot(self):
        return self._inner.lease_snapshot()

    def release_for_handoff(self, transition_id):
        return self._inner.release_for_handoff(transition_id)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AssertionError(name)
        raise AttributeError(name)


class _BlindSource(MainRuntime):
    def lease_snapshot(self):
        return None


class _StaleSource(MainRuntime):
    def lease_snapshot(self):
        snap = super().lease_snapshot()
        if snap is None:
            return None
        stale = dict(snap)
        stale["epoch"] = snap["epoch"] - 1
        return stale


class _ExpireSource(MainRuntime):
    clock = None

    def begin_drain(self, transition_id):
        report = super().begin_drain(transition_id)
        self.clock.tick(LEASE_SECONDS + 0.01)
        self._lease = None
        return report


class _Recording:
    def __init__(self, inner):
        self.inner = inner
        self.steps = []

    def transition_step(self, transition_id, step, canary_ok=None):
        self.steps.append((step, canary_ok))
        return self.inner.transition_step(transition_id, step, canary_ok)

    def __getattr__(self, name):
        return getattr(self.inner, name)


_GATEWAY = r"""
import sys
from pathlib import Path
from jarvis.continuity.gateway import ContinuityGateway
db, pub_path = sys.argv[1], sys.argv[2]
token = Path(pub_path).read_text(encoding="ascii").split()
gw = ContinuityGateway.open(db, "home", "oracle-auth", [token[0] + " " + token[1]])
sys.stdout.buffer.write((gw.serve() + "\n").encode())
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
gw.close()
"""

_RUNTIME = r"""
import json
import sys
from pathlib import Path
from jarvis.continuity.gateway import AuthorityClient
from jarvis.continuity.runtime import GatewayPort, MainRuntime

origin, device, key_path, lease_path = sys.argv[1:5]
client = AuthorityClient(
    origin, authority_id="oracle-auth", device_id=device, installation_id=device + "-inst",
    private_key_path=key_path)
runtime = MainRuntime(
    GatewayPort(client), device, device + "-inst",
    lambda: {"control_ready": True, "checkpoint": "cp-1"},
    boot=lambda: "boot-a")
try:
    if lease_path != "-":
        runtime.accept_lease(json.loads(Path(lease_path).read_text(encoding="utf-8")))
    runtime.start()
except Exception as exc:
    sys.stdout.buffer.write(json.dumps({"ready": False, "code": getattr(exc, "code", "error")}).encode() + b"\n")
    sys.stdout.buffer.flush()
    raise SystemExit(1)
sys.stdout.buffer.write(b"READY\n")
sys.stdout.buffer.flush()
for raw in sys.stdin.buffer:
    cmd = json.loads(raw.decode())
    try:
        op = cmd["op"]
        if op == "begin_drain":
            report = runtime.begin_drain(cmd["transition_id"])
            if cmd.get("ack"):
                runtime.acknowledge_current()
                report = dict(report, acked=True)
            out = report
        elif op == "accept_lease":
            runtime.accept_lease(cmd["lease"])
            out = {"ok": True, "phase": runtime.state()["phase"], "main": runtime.state()["main"]}
        elif op == "lease_snapshot":
            out = {"ok": True, "lease": runtime.lease_snapshot()}
        elif op == "release_for_handoff":
            out = runtime.release_for_handoff(cmd["transition_id"])
        elif op == "authority_status":
            out = {"ok": True, "status": runtime._port.status()}
        elif op == "renew":
            out = runtime.renew_once()
        elif op == "state":
            out = runtime.state()
        elif op == "require":
            try:
                out = {"ok": True, "context": runtime.require_authority("control")}
            except Exception as exc:
                out = {"ok": False, "code": getattr(exc, "code", "error")}
        else:
            out = {"ok": False, "code": "bad_op"}
    except Exception as exc:
        out = {"ok": False, "code": getattr(exc, "code", "error")}
    sys.stdout.buffer.write(json.dumps(out).encode() + b"\n")
    sys.stdout.buffer.flush()
"""


def _read_line(stream, timeout):
    chunks = []
    while True:
        found, _writable, _errors = select.select([stream], [], [], timeout)
        if not found:
            raise TimeoutError(b"".join(chunks))
        byte = stream.read(1)
        if not byte:
            raise EOFError(b"".join(chunks))
        if byte == b"\n":
            return b"".join(chunks).decode()
        chunks.append(byte)


def _key(folder, name):
    path = folder / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-continuity-test", "-f", str(path)],
        check=True, capture_output=True, timeout=10)
    token = (folder / f"{name}.pub").read_text(encoding="ascii").split()
    return f"{token[0]} {token[1]}", path


def _spawn(script, args, err_path):
    err = open(err_path, "w", encoding="utf-8")
    env = os.environ.copy()
    env["JARVIS_NO_MODELS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, bufsize=0, cwd="/home/jdoe/projects/JARVIS",
        env=env)
    proc._err = err
    return proc


def _stop(proc):
    if proc is None:
        return
    for stream in (proc.stdin, proc.stdout):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    for stream in (getattr(proc, "_err", None), proc.stderr):
        try:
            if stream is not None and not stream.closed:
                stream.close()
        except Exception:
            pass


def _rpc(proc, payload):
    proc.stdin.write(json.dumps(payload).encode() + b"\n")
    proc.stdin.flush()
    return json.loads(_read_line(proc.stdout, 20))


class _LivePort:
    """Owner steps and device calls over real signed clients. Not a truth stub."""

    def __init__(self, owner_device, candidate):
        self.owner_device = owner_device
        self.candidate = candidate

    def status(self):
        return self._call(self.owner_device.status)

    def release(self, lease_tuple):
        return self._call(lambda: self.owner_device.release(
            lease_tuple["holder"], lease_tuple["lease_id"], lease_tuple["incarnation"], lease_tuple["epoch"]))

    def transition_request(self, request):
        return self._call(lambda: self.owner_device.transition_request(
            request["client_key"], request["action"], request["candidate"],
            request["expected_incarnation"], request["expected_epoch"], request["expected_policy"],
            request["profile"]))

    def transition_readiness(self, transition_id, readiness):
        return self._call(lambda: self.candidate.readiness(
            transition_id, readiness["control_ready"], readiness["ai_ready"], readiness.get("checkpoint")))

    def transition_step(self, transition_id, step, canary_ok=None):
        return self._call(lambda: self.owner_device.step(transition_id, step, canary_ok))

    def _call(self, fn):
        from jarvis.continuity.authority import AuthorityError
        from jarvis.continuity.gateway import GatewayError
        from jarvis.continuity.runtime import LinkError
        try:
            return fn()
        except GatewayError as exc:
            if exc.code in {"unavailable", "partition", "no_relay", "authority_mismatch", "redirect"}:
                raise LinkError("partition") from None
            raise AuthorityError(exc.code, exc.code, wait=exc.wait) from None


class _ProcRuntime:
    def __init__(self, proc, device_id):
        self.proc = proc
        self.device_id = device_id
        self.reports = []

    def begin_drain(self, transition_id):
        report = _rpc(self.proc, {"op": "begin_drain", "transition_id": transition_id, "ack": True})
        self.reports.append(report)
        if report.get("ok") is False:
            raise RuntimeRefusal(report.get("code") or "drain")
        return report

    def accept_lease(self, lease):
        body = {key: lease[key] for key in ("holder", "lease_id", "incarnation", "epoch")}
        report = _rpc(self.proc, {"op": "accept_lease", "lease": body})
        self.reports.append(report)
        if report.get("ok") is not True:
            raise RuntimeRefusal(report.get("code") or "accept")
        return report

    def lease_snapshot(self):
        report = _rpc(self.proc, {"op": "lease_snapshot"})
        if report.get("ok") is not True:
            raise RuntimeRefusal(report.get("code") or "partition")
        lease = report.get("lease")
        if lease is None:
            return None
        return {key: lease[key] for key in ("holder", "lease_id", "incarnation", "epoch")}

    def release_for_handoff(self, transition_id):
        report = _rpc(self.proc, {"op": "release_for_handoff", "transition_id": transition_id})
        if "released" not in report:
            raise RuntimeRefusal(report.get("code") or "no_lease")
        return report

    def authority_status(self):
        report = _rpc(self.proc, {"op": "authority_status"})
        if report.get("ok") is not True:
            raise RuntimeRefusal(report.get("code") or "partition")
        status = report.get("status")
        if not isinstance(status, dict):
            raise RuntimeRefusal("partition")
        return status


_CLAIM_RACE = r"""
import os
import sys
import time
import types
from pathlib import Path

journal, counter, ready_dir = sys.argv[1:4]


class _Port:
    def status(self):
        return {
            "mode": "V2_ACTIVE",
            "main": "dell",
            "lease_holder": "dell",
            "lease_valid": True,
            "main_ready": False,
            "incarnation": 1,
            "epoch": 3,
            "policy_version": 1,
            "transition": {
                "transition_id": "tr-claim",
                "state": "GRANTED_NOT_READY",
                "candidate": "dell",
                "action": "set_preferred_main",
                "source": "zenbook",
                "expected_epoch": 1,
            },
        }

    def release(self, *_args, **_kwargs):
        raise AssertionError("release")

    def transition_request(self, *_args, **_kwargs):
        raise AssertionError("request")

    def transition_readiness(self, *_args, **_kwargs):
        raise AssertionError("readiness")

    def transition_step(self, transition_id, step, canary_ok=None):
        if step != "complete":
            raise AssertionError(step)
        return {"state": "SUCCEEDED", "transition_id": transition_id}


class _Runtime:
    def __init__(self, device_id):
        self.device_id = device_id

    def lease_snapshot(self):
        return {
            "holder": "dell", "lease_id": "ls-hidden", "incarnation": 1, "epoch": 3, "ready": False,
        }


def _mark(name):
    import fcntl
    with open(counter, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(name + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def brain(_old, _new, _config, _base):
    _mark("brain")
    time.sleep(0.4)
    return {"ok": True, "status": "synced"}


def repoint(_old, _new, _lease):
    _mark("repoint")
    return {"ok": True, "authenticated": True, "key_updated": True, "endpoint_updated": True}


def canary(_new):
    _mark("canary")
    return True


module = types.ModuleType("jarvis.brainsync")
module.handoff = brain
sys.modules["jarvis.brainsync"] = module

from jarvis.continuity.runtime import SafeHandoff

Path(ready_dir, str(os.getpid())).write_text("ready", encoding="utf-8")
deadline = time.time() + 8
while time.time() < deadline:
    if len(list(Path(ready_dir).iterdir())) >= 2:
        break
    time.sleep(0.01)
else:
    raise SystemExit("peer missing")

handoff = SafeHandoff(
    _Port(), source_runtime=_Runtime("zenbook"), candidate_runtime=_Runtime("dell"),
    readiness=lambda _device: {"control_ready": True, "ai_ready": False, "checkpoint": "cp-1"},
    repoint=repoint, canary=canary, journal_path=journal)
result = handoff("zenbook", "dell", "HANDOFF-TOKEN-7f3a", "HANDOFF-BASE-7f3a")
if not isinstance(result, dict) or "status" not in result:
    raise SystemExit("no result")
"""


def _loopback_handoff(case):
    from jarvis.continuity.gateway import AuthorityClient, GatewayError

    folder = Path(case.tmp.name)
    pubs = {}
    keys = {}
    for name in ("owner", "zenbook", "dell", "oracle", "stranger"):
        pubs[name], keys[name] = _key(folder, name)
    err_gw = folder / "gateway.err"
    gateway = _spawn(_GATEWAY, [str(folder / "loopback.sqlite"), str(keys["owner"].with_suffix(".pub"))], err_gw)
    case.addCleanup(_stop, gateway)
    try:
        origin = _read_line(gateway.stdout, 15)
    except Exception:
        _stop(gateway)
        raise AssertionError(err_gw.read_text(encoding="utf-8")[-2000:])
    case.assertTrue(origin.startswith("http://127.0.0.1:"), origin)

    def client(name=None, owner=False):
        kwargs = {"authority_id": "oracle-auth"}
        if name is not None:
            kwargs.update(device_id=name, installation_id=name + "-inst", private_key_path=str(keys[name]))
        if owner:
            kwargs["owner_private_key_path"] = str(keys["owner"])
        return AuthorityClient(origin, **kwargs)

    owner = client(owner=True)
    zen = client("zenbook", owner=True)
    dell_client = client("dell")
    oracle = client("oracle")
    stranger = AuthorityClient(
        origin, authority_id="oracle-auth", device_id="zenbook", installation_id="zenbook-inst",
        private_key_path=str(keys["stranger"]))
    with case.assertRaises(GatewayError) as forged:
        stranger.status()
    case.assertEqual(forged.exception.code, "unauthorized")

    def enroll(name, kind, caps):
        owner.enroll({"device_id": name, "installation_id": name + "-inst", "kind": kind,
                      "public_key": pubs[name], "capabilities": list(caps)})

    enroll("zenbook", "pc", PC)
    enroll("dell", "pc", PC)
    enroll("oracle", "server", SERVER)
    version = zen.status()["mode_version"]
    version = owner.set_mode("PREPARING_V2", version)["mode_version"]
    version = owner.set_mode("QUIESCED", version)["mode_version"]
    for name, device in (("zenbook", zen), ("dell", dell_client), ("oracle", oracle)):
        device.acknowledge(version + 1, 0)
    owner.set_mode("V2_ONLY_NO_MAIN", version)
    status = zen.status()
    opened = owner.transition_request(
        "k-boot", "recover", "zenbook", status["incarnation"], status["epoch"], status["policy_version"])
    zen.readiness(opened["transition_id"], True, False, "cp-1")
    owner.step(opened["transition_id"], "drain")
    owner.step(opened["transition_id"], "fence")
    boot = owner.step(opened["transition_id"], "grant")
    owner.step(opened["transition_id"], "complete", True)
    case.assertEqual(zen.status()["main"], "zenbook")
    lease_path = folder / "zenbook-lease.json"
    lease_body = {key: boot[key] for key in ("holder", "lease_id", "incarnation", "epoch")}
    lease_path.write_text(json.dumps(lease_body), encoding="utf-8")

    source_proc = _spawn(
        _RUNTIME, [origin, "zenbook", str(keys["zenbook"]), str(lease_path)], folder / "source.err")
    case.addCleanup(_stop, source_proc)
    cand_proc = _spawn(
        _RUNTIME, [origin, "dell", str(keys["dell"]), "-"], folder / "candidate.err")
    case.addCleanup(_stop, cand_proc)
    for proc, err_path in ((source_proc, folder / "source.err"), (cand_proc, folder / "candidate.err")):
        try:
            ready = _read_line(proc.stdout, 15)
        except Exception as exc:
            raise AssertionError(f"{exc}\n{err_path.read_text(encoding='utf-8')[-2000:]}") from None
        case.assertEqual(ready, "READY")
    case.assertGreaterEqual(len({gateway.pid, source_proc.pid, cand_proc.pid, os.getpid()}), 4)

    current = dell_client.status()
    dell_client.acknowledge(current["mode_version"], current["epoch"])
    oracle.acknowledge(current["mode_version"], current["epoch"])
    source = _ProcRuntime(source_proc, "zenbook")
    candidate = _ProcRuntime(cand_proc, "dell")
    before = source.authority_status()
    case.assertEqual(before["main"], "zenbook")
    case.assertNotIn("lease_id", before)
    boot_epoch = boot["epoch"]
    seen = {}

    def brain(old, new, config, base):
        seen["brain"] = zen.status()
        seen["args"] = (old, new, config, base)
        view = seen["brain"]
        if view.get("main") != "dell" or view.get("main_ready") or view.get("epoch", 0) <= boot_epoch:
            return {"ok": False, "status": "early"}
        if (view.get("transition") or {}).get("state") != "GRANTED_NOT_READY":
            return {"ok": False, "status": "early"}
        return {"ok": True, "status": "synced"}

    def repoint(old, new, lease):
        seen["lease"] = dict(lease)
        if lease.get("holder") != new or lease.get("epoch", 0) <= boot_epoch:
            return {"ok": False}
        enrolled = owner.enroll({
            "device_id": "dell", "installation_id": "dell-inst", "kind": "pc",
            "public_key": pubs["dell"], "capabilities": list(PC),
            "relay_origin": "http://127.0.0.1:9"})
        seen["enroll"] = enrolled
        if enrolled.get("device_id") != "dell":
            return {"ok": False}
        return {"ok": True, "authenticated": True, "key_updated": True, "endpoint_updated": True}

    def canary(new):
        state = _rpc(cand_proc, {"op": "state"})
        seen["canary"] = state
        return state.get("phase") == "main_not_ready" and state.get("main") == new

    case.use_brain(brain)
    config_path = Path("/home/jdoe/projects/JARVIS/jarvis.config.json")
    before = config_path.read_bytes()
    result = SafeHandoff(
        _LivePort(zen, dell_client), source_runtime=source, candidate_runtime=candidate,
        readiness=_facts, repoint=repoint, canary=canary)("zenbook", "dell", TOKEN, BASE)
    case.assertEqual(config_path.read_bytes(), before)
    case.assert_public(result)
    case.assertTrue(result["ok"], result)
    case.assertEqual(result["action"], "set_preferred_main")
    case.assertEqual(result["main"], "dell")
    case.assertGreater(result["epoch"], boot_epoch)
    case.assertEqual(seen["args"], ("zenbook", "dell", TOKEN, BASE))
    case.assertEqual(seen["enroll"]["device_id"], "dell")
    case.assertEqual(seen["canary"]["phase"], "main_not_ready")
    case.assertTrue(source.reports and source.reports[0].get("acked") is True)
    held = candidate.lease_snapshot()
    case.assertEqual(held["holder"], "dell")
    case.assertNotIn("lease_id", candidate.authority_status())
    case.assertEqual(candidate.authority_status()["main"], "dell")
    case.assertNotIn(seen["lease"]["lease_id"], json.dumps(result))
    final = dell_client.status()
    case.assertEqual((final["main"], final["main_ready"], final["preferred"]), ("dell", True, "dell"))
    case.assertEqual(final["epoch"], result["epoch"])
    renewed = _rpc(source_proc, {"op": "renew"})
    case.assertEqual(renewed["code"], "stale")
    case.assertNotIn("lease_id", renewed)
    required = _rpc(cand_proc, {"op": "require"})
    case.assertTrue(required["ok"], required)
    case.assertEqual(required["context"]["holder"], "dell")
    case.assertNotIn("lease_id", json.dumps(required))
