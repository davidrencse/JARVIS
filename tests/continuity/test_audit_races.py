"""Audit fixes: runtime drain race, recovery accept retry, journal rollback, brain bundle cleanup.

Races use Events handed to the fake port, never sleeps. Each test is well under a second.
"""

import base64
import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.continuity import brain_channel
from tests.continuity._fleet import Clock
from jarvis.continuity import runtime as runtime_module
from jarvis.continuity.authority import LEASE_SECONDS
from jarvis.continuity.recovery import ManualRecovery
from jarvis.continuity.runtime import (
    MainRuntime,
    RuntimeRefusal,
    SafeHandoff,
    _HandoffJournal,
)

LEASE = {"holder": "arch", "lease_id": "lease-1", "incarnation": 1, "epoch": 1}
WAIT = 5  # seconds; only a failure ever waits this long


class RacePort:
    """A one-PC authority. ``block_renew`` / ``block_status`` park the next call.

    A blocked ``status`` computes its answer first, so the reader holds a
    snapshot that predates anything the test does while it is parked.
    """

    def __init__(self):
        self.transition = None
        self.parked = threading.Event()
        self.go = threading.Event()
        self._arm = None

    def arm(self, where):
        self._arm = where
        self.parked.clear()
        self.go.clear()

    def _park(self, where):
        if self._arm == where:
            self._arm = None
            self.parked.set()
            if not self.go.wait(WAIT):
                raise AssertionError("test never released the parked call")

    def status(self):
        snapshot = {
            "mode": "HANDOVER" if self.transition else "V2_ACTIVE", "mode_version": 1,
            "incarnation": 1, "epoch": 1, "main": "arch", "preferred": "arch",
            "lease_valid": True, "main_ready": True, "authority_id": "auth-1",
            "policy_version": 1, "lease_holder": "arch",
            "transition": dict(self.transition) if self.transition else None,
        }
        self._park("status")
        return snapshot

    def renew(self, lease):
        self._park("renew")
        return {"ready": True, "valid_for": LEASE_SECONDS, "renew_after": LEASE_SECONDS / 4}

    def release(self, lease):
        return {}

    def acknowledge(self, device_id, installation_id, mode_version, fenced_epoch, boot_id=None):
        return {}


def make_runtime(port):
    runtime = MainRuntime(port, "arch", "arch-inst", lambda: {"control_ready": True},
                          clock=Clock().mono, boot=lambda: "boot-a")
    runtime.start()
    runtime.accept_lease(dict(LEASE))
    return runtime


def run_in_thread(fn):
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # surfaced by the test
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


class DrainRaceTests(unittest.TestCase):
    def assert_drained(self, runtime):
        self.assertEqual(runtime._block, "draining")
        self.assertFalse(runtime.state()["admission_open"])

    def test_renew_in_flight_cannot_cache_or_clear_a_drain(self):
        port = RacePort()
        runtime = make_runtime(port)
        port.arm("renew")
        thread, box = run_in_thread(runtime.renew_once)
        self.assertTrue(port.parked.wait(WAIT))
        # The renew request is out. A handoff drain starts now.
        port.transition = {"transition_id": "t-1", "state": "DRAINING", "source": "arch"}
        report = runtime.begin_drain("t-1")
        self.assertTrue(report["admission_stopped"])
        # The authority has no open transition again by the time the renew's status read runs.
        port.transition = None
        port.go.set()
        thread.join(WAIT)
        self.assertNotIn("error", box)
        self.assertEqual(box["result"]["cached"], False, "a grant answered before the drain must not be cached")
        self.assertEqual(runtime._cache.remaining("arch"), 0.0)
        self.assert_drained(runtime)

    def test_status_snapshot_taken_before_the_drain_cannot_clear_it(self):
        port = RacePort()
        runtime = make_runtime(port)
        port.arm("status")
        thread, box = run_in_thread(runtime.renew_once)
        self.assertTrue(port.parked.wait(WAIT))
        # renew already got its answer; its status snapshot (no transition) is parked.
        port.transition = {"transition_id": "t-1", "state": "DRAINING", "source": "arch"}
        runtime.begin_drain("t-1")
        port.transition = None
        port.go.set()
        thread.join(WAIT)
        self.assertNotIn("error", box)
        self.assert_drained(runtime)

    def test_drain_finished_read_from_before_a_new_drain_does_not_reopen(self):
        port = RacePort()
        runtime = make_runtime(port)
        port.transition = {"transition_id": "t-1", "state": "DRAINING", "source": "arch"}
        runtime.begin_drain("t-1")
        # t-1 closes. A caller checks admission; its status read (no transition) parks.
        port.transition = None
        port.arm("status")
        thread, box = run_in_thread(lambda: runtime.require_authority("control"))
        self.assertTrue(port.parked.wait(WAIT))
        port.transition = {"transition_id": "t-2", "state": "DRAINING", "source": "arch"}
        runtime.begin_drain("t-2")
        port.go.set()
        thread.join(WAIT)
        self.assertIsInstance(box.get("error"), RuntimeRefusal, box)
        self.assertEqual(runtime._block, "draining")
        self.assertFalse(runtime.state()["admission_open"])

    def test_suspend_set_during_renew_is_not_cleared(self):
        port = RacePort()
        runtime = make_runtime(port)
        port.arm("status")
        thread, box = run_in_thread(runtime.renew_once)
        self.assertTrue(port.parked.wait(WAIT))
        runtime.release_before_suspend()
        port.go.set()
        thread.join(WAIT)
        self.assertNotIn("error", box)
        self.assertEqual(runtime._block, "suspend")
        self.assertFalse(runtime.state()["admission_open"])

    def test_a_clean_renew_still_opens_admission(self):
        port = RacePort()
        runtime = make_runtime(port)
        result = runtime.renew_once()
        self.assertEqual(result["code"], "ok")
        self.assertTrue(runtime.state()["admission_open"])


class FakeRecoveryPort:
    """Authority stand-in: recover transition FENCING -> GRANTED_NOT_READY -> DONE / CANCELLED."""

    def __init__(self):
        self.steps = []
        self.state = "FENCING"
        self.epoch = 2
        self.opened = 0

    def status(self):
        live = self.state in ("FENCING", "GRANTED_NOT_READY")
        done = self.state == "DONE"
        return {"mode": "V2_ACTIVE" if done else ("HANDOVER" if live else "V2_ONLY_NO_MAIN"),
                "lease_valid": self.state == "GRANTED_NOT_READY" or done,
                "main_ready": done, "incarnation": 1, "epoch": self.epoch, "policy_version": 1,
                "main": "dell" if done else None,
                "transition": {"transition_id": "t-%d" % self.opened, "state": self.state,
                               "action": "recover", "candidate": "dell"} if live else None}

    def transition_request(self, request):
        self.opened += 1
        self.state = "FENCING"

    def transition_readiness(self, tid, readiness):
        pass

    def transition_step(self, tid, step, canary_ok=None):
        self.steps.append(step)
        if step == "grant":
            self.state = "GRANTED_NOT_READY"
            return {"holder": "dell", "lease_id": "lease-%d" % self.opened, "incarnation": 1,
                    "epoch": self.epoch}
        if step == "cancel":
            self.state = "CANCELLED"
            self.epoch += 1
        if step == "complete":
            self.state = "DONE" if canary_ok else "CANCELLED"
        return {"state": self.state}


class FakeCandidate:
    device_id = "dell"

    def __init__(self, accept_error=None):
        self.accept_error, self.snapshot, self.accepted = accept_error, None, 0

    def accept_lease(self, lease):
        self.accepted += 1
        if self.accept_error is not None:
            raise self.accept_error
        self.snapshot = dict(lease)

    def lease_snapshot(self):
        return self.snapshot

    def readiness(self):
        return {}


class RecoveryRetryTests(unittest.TestCase):
    def test_failed_accept_cancels_the_not_ready_grant(self):
        port, candidate = FakeRecoveryPort(), FakeCandidate(RuntimeRefusal("partition"))
        port.opened = 1
        with self.assertRaises(RuntimeRefusal) as caught:
            ManualRecovery(port, candidate, lambda device: True)("dell", "client-key-1")
        self.assertEqual(caught.exception.code, "partition")
        self.assertEqual(port.steps[-2:], ["grant", "cancel"])
        self.assertEqual(port.state, "CANCELLED")

    def test_retry_after_failed_accept_starts_clean_and_succeeds(self):
        port, candidate = FakeRecoveryPort(), FakeCandidate(RuntimeRefusal("partition"))
        port.opened = 1
        recovery = ManualRecovery(port, candidate, lambda device: True)
        with self.assertRaises(RuntimeRefusal):
            recovery("dell", "client-key-1")
        candidate.accept_error = None
        result = recovery("dell", "client-key-1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(port.opened, 2, "the retry opened a fresh transition")
        self.assertEqual(candidate.accepted, 2)
        self.assertEqual(port.state, "DONE")

    def test_retry_on_a_grant_the_candidate_never_took_cancels_instead_of_wedging(self):
        port, candidate = FakeRecoveryPort(), FakeCandidate()
        port.opened, port.state = 1, "GRANTED_NOT_READY"  # a call died between grant and accept
        with self.assertRaises(RuntimeRefusal) as caught:
            ManualRecovery(port, candidate, lambda device: True)("dell", "client-key-1")
        self.assertEqual(caught.exception.code, "lease_unavailable")
        self.assertIn("cancel", port.steps)
        self.assertEqual(port.state, "CANCELLED")


class _StubHandoff(SafeHandoff):
    """Just enough of SafeHandoff to run ``_callback`` against a real journal."""

    def __init__(self, journal, status, snap):
        self._active_journal = journal
        self._stub_status, self._stub_snap = status, snap
        self.paused = 0
        self.aborts = []

    def _acquire_grant(self, transition):
        return self._stub_status, self._stub_snap, None, None, False

    def _read_status(self):
        return self._stub_status, None

    def _abort(self, tid, code, transition):
        self.aborts.append(code)
        return {"status": "aborted", "code": code}

    def _pause_candidate_renewal(self):
        self.paused += 1


class JournalRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "journal.json"

    def journal(self):
        journal = _HandoffJournal(self.path)
        transition = {"transition_id": "t-1", "candidate": "dell", "action": "handoff"}
        self.assertTrue(journal.bind(transition, 1, 2))
        return journal

    def failing_write(self):
        return patch.object(runtime_module, "_journal_write", side_effect=OSError("disk full"))

    def test_failed_first_claim_leaves_memory_and_disk_unstarted(self):
        journal = self.journal()
        before = journal.doc["steps"].copy()
        with self.failing_write():
            phase, ok = journal.claim("brain")
        self.assertEqual((phase, ok), ("unwritable", None))
        self.assertEqual(journal.doc["steps"], before)
        self.assertIsNone(journal.step("brain").get("phase"))
        self.assertFalse(journal.corrupt)
        # The disk never said started either: a fresh process sees nothing recorded.
        self.assertFalse(_HandoffJournal(self.path).any_recorded())
        # With the disk back, the same journal claims and the step is owned.
        self.assertEqual(journal.claim("brain"), ("claimed", None))
        self.assertEqual(_HandoffJournal(self.path).step("brain")["phase"], "started")

    def test_failed_mark_does_not_pretend_finished(self):
        journal = self.journal()
        self.assertEqual(journal.claim("repoint")[0], "claimed")
        with self.failing_write():
            self.assertFalse(journal.mark("repoint", "finished", ok=True))
        self.assertEqual(journal.step("repoint")["phase"], "started")

    def test_in_memory_journal_merge_also_works_on_a_copy(self):
        journal = _HandoffJournal(None)
        self.assertEqual(journal.claim("canary"), ("claimed", None))
        self.assertEqual(journal.claim("canary"), ("started", None))

    def test_callback_surfaces_an_unwritable_journal_and_never_runs_the_hook(self):
        journal = self.journal()
        status = {"main": "dell", "epoch": 2, "mode": "HANDOVER"}
        handoff = _StubHandoff(journal, status, {"holder": "dell", "lease_id": "x", "incarnation": 1, "epoch": 2})
        ran = []
        transition = {"transition_id": "t-1", "candidate": "dell", "action": "handoff"}
        with self.failing_write():
            result = handoff._callback(
                transition, "brain", lambda snap: ran.append(1) or {"ok": True},
                lambda r: r.get("ok") is True, lambda r: "brain")
        self.assertEqual(ran, [])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["code"], "journal_unwritable")
        self.assertEqual(result["retry_after"], runtime_module._JOURNAL_RETRY)
        self.assertEqual(handoff.aborts, [])
        # The retry runs the hook exactly once, because nothing was recorded.
        result = handoff._callback(
            transition, "brain", lambda snap: ran.append(1) or {"ok": True},
            lambda r: r.get("ok") is True, lambda r: "brain")
        self.assertIsNone(result)
        self.assertEqual(ran, [1])


class BrainBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "staging"
        self.folder.mkdir()
        self.config = {}

    def name(self):
        return "brain-" + os.urandom(16).hex() + ".bundle"

    def within(self):
        def fake(config, path):
            target = Path(path)
            return {"path": str(target), "dir": target.is_dir(),
                    "size": target.stat().st_size if target.is_file() else 0}
        return patch("jarvis.transfer._within", side_effect=fake)

    def age(self, path, seconds):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_sweep_removes_only_stale_bundles(self):
        stale, fresh = self.folder / self.name(), self.folder / self.name()
        other = self.folder / "notes.txt"
        for file in (stale, fresh, other):
            file.write_bytes(b"x")
        self.age(stale, brain_channel.STALE_BUNDLE_SECONDS + 60)
        self.age(other, brain_channel.STALE_BUNDLE_SECONDS + 60)
        with self.within():
            brain_channel.run_here({"action": "brain_stage", "path": str(self.folder)},
                                   Path(self.tmp.name) / ".jarvis", self.config)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists(), "only brain-<hex>.bundle names are ever swept")

    def test_export_sweeps_stale_bundles_before_writing(self):
        stale = self.folder / self.name()
        stale.write_bytes(b"old")
        self.age(stale, brain_channel.STALE_BUNDLE_SECONDS + 60)
        payload = base64.b64encode(b"bundle").decode()
        with self.within(), patch("jarvis.brainsync.export_result", return_value={"ok": True, "payload": payload}):
            result = brain_channel.run_here(
                {"action": "brain_export", "path": str(self.folder), "request": "{}"},
                Path(self.tmp.name) / ".jarvis", self.config)
        self.assertTrue(result["ok"])
        self.assertFalse(stale.exists())
        self.assertEqual((self.folder / result["name"]).read_bytes(), b"bundle")

    def test_discard_removes_one_bundle_and_refuses_other_names(self):
        bundle, keep = self.folder / self.name(), self.folder / "keep.txt"
        bundle.write_bytes(b"x")
        keep.write_bytes(b"x")
        with self.within():
            self.assertTrue(brain_channel.run_here(
                {"action": "brain_discard", "path": str(bundle)}, Path(self.tmp.name) / ".jarvis", self.config)["ok"])
            with self.assertRaises(ValueError):
                brain_channel.run_here(
                    {"action": "brain_discard", "path": str(keep)}, Path(self.tmp.name) / ".jarvis", self.config)
            with self.assertRaises(ValueError):
                brain_channel.run_here(
                    {"action": "brain_discard", "path": str(self.folder)}, Path(self.tmp.name) / ".jarvis", self.config)
        self.assertFalse(bundle.exists())
        self.assertTrue(keep.exists())

    def test_discard_is_an_owner_only_brain_action_with_a_fixed_shape(self):
        self.assertIn("brain_discard", brain_channel.ACTIONS)
        brain_channel.validate({"action": "brain_discard", "path": "/staging/x"})
        for bad in ({"action": "brain_discard"}, {"action": "brain_discard", "path": "/x", "force": True}):
            with self.assertRaises(ValueError):
                brain_channel.validate(bad)

    def test_failed_receive_removes_the_upload(self):
        blob = b"bundle bytes"
        for outcome in ("not_ok", "raises", "digest"):
            upload = self.folder / self.name()
            upload.write_bytes(blob)
            digest = hashlib.sha256(blob).hexdigest() if outcome != "digest" else "0" * 64
            patcher = (patch("jarvis.continuity.return_brain.receive", side_effect=RuntimeError("boom"))
                       if outcome == "raises"
                       else patch("jarvis.continuity.return_brain.receive", return_value={"ok": False}))
            with self.within(), patcher:
                try:
                    result = brain_channel.run_here(
                        {"action": "brain_receive", "path": str(upload), "sha256": digest},
                        Path(self.tmp.name) / ".jarvis", self.config)
                except (RuntimeError, ValueError):
                    pass
                else:
                    self.assertFalse(result["ok"])
            self.assertFalse(upload.exists(), outcome)

    def test_good_receive_still_removes_the_upload(self):
        blob = b"bundle bytes"
        upload = self.folder / self.name()
        upload.write_bytes(blob)
        with self.within(), patch("jarvis.continuity.return_brain.receive", return_value={"ok": True}):
            brain_channel.run_here(
                {"action": "brain_receive", "path": str(upload), "sha256": hashlib.sha256(blob).hexdigest()},
                Path(self.tmp.name) / ".jarvis", self.config)
        self.assertFalse(upload.exists())

    def test_failed_receive_keeps_a_file_that_is_not_one_of_ours(self):
        other = self.folder / "precious.bin"
        other.write_bytes(b"data")
        with self.within(), patch("jarvis.continuity.return_brain.receive", return_value={"ok": False}):
            brain_channel.run_here(
                {"action": "brain_receive", "path": str(other), "sha256": hashlib.sha256(b"data").hexdigest()},
                Path(self.tmp.name) / ".jarvis", self.config)
        self.assertTrue(other.exists())

    def pull_calls(self, blob, name, digest=None, tamper=False):
        adapter = brain_channel.ClusterBundleTransport(
            {"devices": {"dell": {"brain_transfer_dir": "/staging"}}}, "/tmp/fake")
        calls = []

        def call(node, payload):
            calls.append(payload)
            if payload["action"] == "brain_export":
                return {"ok": True, "name": name, "size": len(blob),
                        "sha256": digest or hashlib.sha256(blob).hexdigest()}
            if payload["action"] == "get":
                data = blob[:-1] if tamper else blob
                return {"data": base64.b64encode(data).decode(), "size": len(blob)}
            return {"ok": True}

        with patch.object(adapter, "_call", side_effect=call):
            return adapter.pull("dell", b"{}"), calls

    def test_verified_pull_discards_the_export(self):
        name = self.name()
        result, calls = self.pull_calls(b"bundle", name)
        self.assertTrue(result["ok"])
        self.assertEqual(calls[-1], {"action": "brain_discard", "path": "/staging/" + name})

    def test_unverified_pull_also_discards_the_export(self):
        name = self.name()
        result, calls = self.pull_calls(b"bundle", name, digest="0" * 64)
        self.assertFalse(result["ok"])
        self.assertEqual(calls[-1], {"action": "brain_discard", "path": "/staging/" + name})

    def test_a_peer_that_cannot_discard_does_not_fail_the_pull(self):
        name = self.name()
        adapter = brain_channel.ClusterBundleTransport(
            {"devices": {"dell": {"brain_transfer_dir": "/staging"}}}, "/tmp/fake")
        blob = b"bundle"

        def call(node, payload):
            if payload["action"] == "brain_export":
                return {"ok": True, "name": name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
            if payload["action"] == "get":
                return {"data": base64.b64encode(blob).decode(), "size": len(blob)}
            raise ValueError("Unknown transfer action.")

        with patch.object(adapter, "_call", side_effect=call):
            self.assertTrue(adapter.pull("dell", b"{}")["ok"])

    def test_pull_refuses_a_peer_supplied_name_that_is_not_a_bundle(self):
        result, calls = self.pull_calls(b"bundle", "../../etc/passwd")
        self.assertFalse(result["ok"])
        self.assertEqual([c["action"] for c in calls], ["brain_export"])


if __name__ == "__main__":
    unittest.main()
