import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from jarvis import deploy
from jarvis.cluster import save_json


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class JobSigningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.key = deploy.code_key(self.state)
        self.job = {"id": "j1", "kind": "desktop", "payload": {"action": "volume", "arg": "5"},
                    "remaining": 30}

    def test_signed_job_verifies_for_its_node_only(self):
        signed = deploy.sign_job(self.job, "dell", self.state)
        self.assertIsNone(deploy.verify_job(signed, "dell", self.key))
        self.assertIsNotNone(deploy.verify_job(signed, "zenbook", self.key))

    def test_tampered_payload_is_refused(self):
        signed = deploy.sign_job(self.job, "dell", self.state)
        evil = {**signed, "payload": {"action": "terminal", "arg": '{"command": "rm -rf ~"}'}}
        self.assertIsNotNone(deploy.verify_job(evil, "dell", self.key))
        self.assertIsNotNone(deploy.verify_job({**self.job, "issued": time.time()}, "dell", self.key))

    def test_stale_job_is_refused(self):
        signed = deploy.sign_job(self.job, "dell", self.state)
        later = time.time() + deploy.JOB_SKEW + 5
        self.assertIn("too old", deploy.verify_job(signed, "dell", self.key, now=later))

    def test_node_refuses_unsigned_and_replayed_jobs(self):
        from jarvis.cluster import NodeWorker
        node_state = self.state / "node"
        node_state.mkdir()
        save_json(node_state / "cluster-node.json", {"node_id": "dell", "code_key": self.key})
        worker = NodeWorker.__new__(NodeWorker)
        worker.state = node_state
        self.assertEqual(worker._untrusted(self.job), "not signed by the main")
        signed = deploy.sign_job(self.job, "dell", self.state)
        self.assertIsNone(worker._untrusted(signed))
        self.assertIn("replayed", worker._untrusted(signed))

    def test_node_without_pinned_key_accepts_as_before(self):
        from jarvis.cluster import NodeWorker
        node_state = self.state / "old"
        node_state.mkdir()
        save_json(node_state / "cluster-node.json", {"node_id": "dell"})
        worker = NodeWorker.__new__(NodeWorker)
        worker.state = node_state
        self.assertIsNone(worker._untrusted(self.job))


if __name__ == "__main__":
    unittest.main()
