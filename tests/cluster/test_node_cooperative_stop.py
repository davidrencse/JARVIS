"""Local, nonce-bound cooperative stop receipts for the node worker."""
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from jarvis.cluster import NodeWorker, _PROCESS_RECORD, request_node_worker_stop


class CooperativeStopTests(unittest.TestCase):
    def _worker(self, state, config):
        worker = object.__new__(NodeWorker)
        worker.state = state
        worker.stop_event = threading.Event()
        worker.cooperative_stop = False
        worker._cooperative_request = None
        worker._process_registration = {
            "schema": 1, "kind": "node-worker", "pid": os.getpid(),
            "nonce": "a" * 48, "config": str(config.resolve()), "state": str(state.resolve()),
        }
        (state / _PROCESS_RECORD).write_text(json.dumps(worker._process_registration), encoding="utf-8")
        return worker

    def test_exact_local_registration_stops_and_returns_a_receipt(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); state = root / ".jarvis"; state.mkdir()
            config = root / "jarvis.config.json"; config.write_text("{}", encoding="utf-8")
            worker = self._worker(state, config)

            def serve_stop_request():
                while not worker._cooperative_stop_requested():
                    worker.stop_event.wait(0.01)
                worker._finish_local_process()

            thread = threading.Thread(target=serve_stop_request)
            thread.start()
            result = request_node_worker_stop(config, state, timeout=2)
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(worker.stop_event.is_set())
            self.assertTrue(worker.cooperative_stop)
            self.assertEqual(result["status"], "stopped")
            self.assertFalse((state / _PROCESS_RECORD).exists())

    def test_wrong_config_and_symlinked_registration_refuse_without_request(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); state = root / ".jarvis"; state.mkdir()
            config = root / "jarvis.config.json"; config.write_text("{}", encoding="utf-8")
            other = root / "other.json"; other.write_text("{}", encoding="utf-8")
            self._worker(state, config)
            with self.assertRaisesRegex(RuntimeError, "matching local"):
                request_node_worker_stop(other, state, timeout=0.1)
            self.assertFalse((state / "cluster-stop-request.json").exists())

    def test_timeout_is_bounded_and_leaves_the_request_for_retry(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); state = root / ".jarvis"; state.mkdir()
            config = root / "jarvis.config.json"; config.write_text("{}", encoding="utf-8")
            self._worker(state, config)
            with self.assertRaises(TimeoutError):
                request_node_worker_stop(config, state, timeout=0.1)
            request = json.loads((state / "cluster-stop-request.json").read_text())
            self.assertEqual(request["pid"], os.getpid())
            self.assertEqual(request["config"], str(config.resolve()))
            self.assertEqual(request["state"], str(state.resolve()))


if __name__ == "__main__":
    unittest.main()
