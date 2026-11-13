import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis.cluster import NodeWorker, _PROCESS_RECORD, _STOP_ACK, request_node_worker_stop
from jarvis.locking import exclusive_lock
from jarvis.continuity import local_lifecycle as lifecycle


class LocalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="continuity-worker-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "jarvis.config.json"
        self.config.write_text(json.dumps({"cluster": {"enabled": True,
            "local_device": "dell", "main_device": "zenbook"},
            "devices": {"dell": {}, "zenbook": {}}}), encoding="utf-8")
        self.device = "dell"

    def _node(self):
        node = object.__new__(NodeWorker)
        node.state = self.state
        node.config_path = str(self.config)
        node.stop_event = threading.Event()
        node._process_registration = None
        node._cooperative_request = None
        node.cooperative_stop = False
        return node

    def _worker(self, *, started=None, cleanup_entered=None, cleanup_release=None,
                finish_event=None):
        node = self._node()
        def run():
            with exclusive_lock(self.state / lifecycle.NODE_LOCK):
                node._register_local_process()
                if started:
                    started.set()
                while not node.stop_event.is_set():
                    node._cooperative_stop_requested()
                    time.sleep(0.005)
                node._close_managed_process = lambda: None
                node._finish_local_process()
                if cleanup_entered:
                    cleanup_entered.set()
                if cleanup_release:
                    cleanup_release.wait(3)
                if finish_event:
                    finish_event.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, node

    def test_stop_requires_exact_receipt_and_writer_lock_release(self):
        started, cleanup_entered, cleanup_release = (threading.Event() for _ in range(3))
        worker, node = self._worker(started=started, cleanup_entered=cleanup_entered,
                                    cleanup_release=cleanup_release)
        self.assertTrue(started.wait(2))
        result = {}
        failure = []
        caller = threading.Thread(target=lambda: self._call_stop(result, failure), daemon=True)
        caller.start()
        self.assertTrue(cleanup_entered.wait(2))
        # The NodeWorker has written its receipt, but still owns the writer lock.
        self.assertTrue(lifecycle._lock_is_busy(self.state))
        self.assertTrue(caller.is_alive(), "receipt must not produce a premature cutover ack")
        cleanup_release.set()
        caller.join(2)
        worker.join(2)
        self.assertFalse(caller.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertFalse(failure)
        self.assertTrue(result["stopped"])
        self.assertFalse(lifecycle._lock_is_busy(self.state))
        self.assertFalse((self.state / _PROCESS_RECORD).exists())

    def _call_stop(self, result, failure):
        try:
            result.update(lifecycle.request_stop(self.state, self.config, self.device,
                                                 timeout=2, poll=0.005))
        except Exception as exc:
            failure.append(exc)

    def test_stuck_worker_times_out_and_is_not_reported_stopped(self):
        started, release = threading.Event(), threading.Event()
        node = self._node()
        def run():
            with exclusive_lock(self.state / lifecycle.NODE_LOCK):
                node._register_local_process()
                started.set()
                release.wait(3)
                node._finish_local_process()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(started.wait(2))
        with self.assertRaisesRegex(lifecycle.CooperativeStopError, "worker_stop_timeout"):
            lifecycle.request_stop(self.state, self.config, self.device, timeout=0.1, poll=0.005)
        self.assertTrue(lifecycle._lock_is_busy(self.state))
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_wrong_identity_and_stale_nonce_requests_are_ignored(self):
        node = self._node()
        with exclusive_lock(self.state / lifecycle.NODE_LOCK):
            registration = node._register_local_process()
            request = {**registration, "request_id": "a" * 32}
            request["config"] = str(self.root / "other-config.json")
            (self.state / "cluster-stop-request.json").write_text(json.dumps(request))
            self.assertFalse(node._cooperative_stop_requested())
            request["config"] = registration["config"]
            request["nonce"] = "b" * 48
            (self.state / "cluster-stop-request.json").write_text(json.dumps(request))
            self.assertFalse(node._cooperative_stop_requested())
            request["nonce"] = registration["nonce"]
            request["request_id"] = "not-a-request-nonce"
            (self.state / "cluster-stop-request.json").write_text(json.dumps(request))
            self.assertFalse(node._cooperative_stop_requested())
            node._finish_local_process()

    def test_stale_pid_record_cannot_be_treated_as_active_or_stopped(self):
        with exclusive_lock(self.state / lifecycle.NODE_LOCK):
            record = {"schema": 1, "kind": "node-worker", "pid": 2**30,
                      "nonce": "a" * 48, "config": str(self.config.resolve()),
                      "state": str(self.state.resolve())}
            (self.state / _PROCESS_RECORD).write_text(json.dumps(record), encoding="utf-8")
            self.assertFalse(lifecycle._worker_is_active(self.state, self.config, self.device))
            with self.assertRaisesRegex(lifecycle.CooperativeStopError, "worker_registration_unverified"):
                lifecycle.request_stop(self.state, self.config, self.device, timeout=0.1)

    def test_wrong_config_or_local_device_refuses_before_request(self):
        started = threading.Event()
        worker, _node = self._worker(started=started)
        self.assertTrue(started.wait(2))
        wrong = lifecycle.WindowsNodeLifecycle(self.root / "other.json", self.state,
            "zenbook", timeout=0.1, windows=True)
        self.assertFalse(wrong.active(wrong.service_name))
        with self.assertRaisesRegex(lifecycle.CooperativeStopError, "worker_registration_unverified"):
            lifecycle.request_stop(self.state, self.config, "zenbook", timeout=0.1)
        with mock.patch("jarvis.cluster._local_pid_alive", return_value=False):
            self.assertFalse(lifecycle._worker_is_active(self.state, self.config, self.device))
        # Stop the fixture through the exact worker protocol.
        request_node_worker_stop(self.config, self.state, timeout=2)
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_windows_lifecycle_restores_through_existing_launch_callback(self):
        started = threading.Event()
        worker, _node = self._worker(started=started)
        self.assertTrue(started.wait(2))
        new_started = threading.Event()
        release_new = threading.Event()
        new_thread = None
        def launch(config, state):
            nonlocal new_thread
            self.assertEqual(Path(config), self.config.resolve())
            self.assertEqual(Path(state), self.state.resolve())
            new_thread, _ = self._worker(started=new_started)
        control = lifecycle.WindowsNodeLifecycle(self.config, self.state, self.device,
            timeout=2, launch=launch, windows=True)
        self.assertTrue(control.supported())
        self.assertTrue(control.active(control.service_name))
        paused = control.pause(self.state)
        self.assertTrue(paused["paused"])
        worker.join(2)
        self.assertFalse(worker.is_alive())
        woke = control.wake(self.state)
        self.assertTrue(woke["woke"])
        self.assertTrue(new_started.is_set())
        self.assertTrue(control.active(control.service_name))
        # Shut down the new fixture with its own nonce-bound stop; no arbitrary kill.
        request_node_worker_stop(self.config, self.state, timeout=2)
        new_thread.join(2)
        self.assertFalse(new_thread.is_alive())

    def test_windows_restore_failure_is_not_reported_as_success(self):
        started = threading.Event()
        worker, _node = self._worker(started=started)
        self.assertTrue(started.wait(2))
        control = lifecycle.WindowsNodeLifecycle(self.config, self.state, self.device,
            timeout=0.2, launch=lambda *_: None, windows=True)
        control.pause(self.state)
        worker.join(2)
        with self.assertRaisesRegex(lifecycle.CooperativeStopError, "worker_restore_unverified"):
            control.wake(self.state)
        self.assertFalse(control.active(control.service_name))


if __name__ == "__main__":
    unittest.main()
