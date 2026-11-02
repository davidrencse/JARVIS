"""Regression tests for cluster hardening: control-route privilege, update/job handling,
redeem key safety, node argv, deploy timeouts, and proxy-free tunnel calls."""

import copy
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis import deploy, remote
from jarvis.cluster import Client, Hub, NodeWorker, save_json
from jarvis.cluster_store import CAPABILITIES
from jarvis.ui import Console

try:
    from tests.test_cluster import configuration
except ImportError:  # run with -s tests and no top-level dir
    from test_cluster import configuration


class ControlRouteTests(unittest.TestCase):
    def setUp(self):
        poll = patch("jarvis.cluster.POLL_WAIT", 0.5)
        poll.start()
        self.addCleanup(poll.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.main = configuration()
        self.console = Console(self.main, self.root, self.root / "main")
        self.addCleanup(self.console.close)
        self.hub = Hub(self.main, self.root, self.root / "main", console=self.console).start(port=0)
        self.addCleanup(self.hub.close)
        self.main["cluster"]["main_url"] = f"http://127.0.0.1:{self.hub.server.server_address[1]}"
        self.node = copy.deepcopy(self.main)
        self.node["cluster"]["local_device"] = "node"
        self.state = self.root / "node"
        token = self.hub.broker.enroll("node", CAPABILITIES)  # includes "console"
        save_json(self.state / "cluster-node.json", {"node_id": "node", "main_device": "main", "token": token})
        self.client = Client(self.node, self.state)

    def test_console_node_cannot_reach_main_terminal_storage_or_grokbot(self):
        bodies = [
            {"node": "main", "kind": "desktop", "payload": {"action": "terminal", "arg": "id"}},
            {"node": "main", "kind": "storage", "payload": {"action": "list", "path": "/"}},
            {"node": "main", "kind": "grokbot", "payload": {"text": "hi"}},
        ]
        with patch("jarvis.cluster.execute_local") as execute:
            for body in bodies:
                with self.subTest(kind=body["kind"]), self.assertRaisesRegex(RuntimeError, "main PC"):
                    self.client.request("/v1/control", body)
        execute.assert_not_called()

    def test_console_node_may_still_hand_main_a_transfer(self):
        with patch("jarvis.cluster.execute_local", return_value={"entries": []}) as execute:
            self.client.request("/v1/control", {
                "node": "main", "kind": "transfer", "payload": {"action": "inbox"}})
        execute.assert_called_once()

    def test_terminal_is_refused_for_non_owner_to_any_node(self):
        with patch.object(self.hub, "dispatch") as dispatch:
            with self.assertRaisesRegex(RuntimeError, "terminal"):
                self.client.request("/v1/control", {
                    "node": "node", "kind": "desktop", "payload": {"action": "terminal", "arg": "id"}})
            dispatch.assert_not_called()
            self.client.request("/v1/control", {
                "node": "node", "kind": "desktop", "payload": {"action": "volume", "arg": "5"}})
            dispatch.assert_called_once()


class UpdateJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.node = configuration("node")
        save_json(self.state / "cluster-node.json",
                  {"node_id": "node", "main_device": "main", "token": "t" * 40})

    def worker(self, kind, payload, executor):
        worker = NodeWorker(self.node, self.state, executor=executor)
        self.sent = []
        worker._send_result = self.sent.append
        job = {"id": uuid.uuid4().hex, "kind": kind, "deadline": time.time() + 5, "payload": payload}
        worker.client.request = Mock(return_value={"job": job, "code": "newer-code"})
        return worker

    def test_read_only_job_runs_when_update_does_not_proceed(self):
        executor = Mock(return_value={"ok": True})
        worker = self.worker("inventory", {}, executor)
        with patch.object(worker, "_pull_update", return_value=False):
            self.assertTrue(worker.once())
        executor.assert_called_once()
        self.assertFalse(self.sent[0]["failed"])

    def test_mutating_job_is_refused_when_code_differs_and_no_update(self):
        executor = Mock()
        worker = self.worker("desktop", {"action": "volume", "arg": "5"}, executor)
        with patch.object(worker, "_pull_update", return_value=False):
            self.assertTrue(worker.once())
        executor.assert_not_called()
        self.assertIn("Not run", self.sent[0]["result"]["error"])

    def test_job_is_refused_when_update_proceeds(self):
        executor = Mock()
        worker = self.worker("inventory", {}, executor)
        with patch.object(worker, "_pull_update", return_value=True):
            self.assertFalse(worker.once())
        executor.assert_not_called()
        self.assertTrue(worker.restarting)
        self.assertIn("updating its code", self.sent[0]["result"]["error"])


class RedeemKeyTests(unittest.TestCase):
    def test_pending_key_is_on_disk_before_redeem_is_sent(self):
        from jarvis.cluster import PENDING_KEY, redeem_invite
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            save_json(state / "cluster-node.json", {
                "node_id": "node", "main_device": "main", "token": "i" * 40, "redeem": True,
                "link": {"identity_file": str(state / "cluster-node.key")}})
            client = Mock(token="i" * 40)
            seen = []

            def request(path, body, timeout=15):
                seen.append((state / PENDING_KEY).read_text())
                raise ConnectionError("reply lost")
            client.request = request
            with patch("jarvis.cluster_setup.keypair", return_value=("PRIVATE\n", "ssh-ed25519 AAAA x")):
                with self.assertRaises(ConnectionError):
                    redeem_invite({}, state, client)
            self.assertEqual(seen, ["PRIVATE\n"])
            self.assertTrue((state / PENDING_KEY).exists())  # kept for the tunnel after a lost reply

    def test_tunnel_offers_pending_key_too(self):
        from jarvis import cluster_tunnel
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "cluster-node-pending.key").write_text("k")
            machine = {"transport": "ssh", "target": "u@h", "identity_file": str(state / "a.key")}
            with patch.object(cluster_tunnel, "peer", return_value=machine), \
                    patch.object(cluster_tunnel.shutil, "which", return_value="/usr/bin/ssh"), \
                    patch.object(cluster_tunnel, "pinned_host", return_value=[]):
                argv = cluster_tunnel.command({"cluster": {}}, state, "u@h")
            ids = [argv[i + 1] for i, a in enumerate(argv) if a == "-i"]
            self.assertEqual(ids, [str(state / "a.key"), str(state / "cluster-node-pending.key")])


class NodeArgvTests(unittest.TestCase):
    def run_main(self, argv):
        from jarvis import node
        seen = []
        with patch.object(sys, "argv", argv), \
                patch("jarvis.__main__.main", side_effect=lambda: seen.append(list(sys.argv)) or 0):
            node.main()
        return seen[0]

    def test_cluster_node_is_not_appended_twice(self):
        first = self.run_main(["jarvis-node", "--state", "x"])
        self.assertEqual(first, ["jarvis-node", "--state", "x", "cluster", "node"])
        self.assertEqual(self.run_main(first), first)  # what a self-restart relaunches


class DeployFailureTests(unittest.TestCase):
    def test_verify_false_and_sign_runtimeerror_on_subprocess_failure(self):
        key = "ssh-ed25519 " + "A" * 68
        for exc in (subprocess.TimeoutExpired("ssh-keygen", 30), subprocess.CalledProcessError(1, "x")):
            with self.subTest(exc=type(exc).__name__), \
                    patch.object(deploy.subprocess, "run", side_effect=exc), \
                    patch.object(deploy, "_keygen", return_value="ssh-keygen"):
                self.assertFalse(deploy.verify(b"d", "-----BEGIN SSH SIGNATURE-----", key))
                with tempfile.TemporaryDirectory() as tmp, patch.object(deploy, "code_key"):
                    with self.assertRaises(RuntimeError):
                        deploy.sign(b"d", tmp)

    def test_remote_wraps_timeouts_and_update_uses_long_ssh_timeout(self):
        with patch.object(deploy, "_ssh", return_value=(["ssh"], ["t"], "~/j")), \
                patch.object(deploy.subprocess, "run",
                             side_effect=subprocess.TimeoutExpired("ssh", 1)):
            with self.assertRaises(RuntimeError):
                deploy._remote({}, ".", "node", "x")
        self.assertGreaterEqual(deploy.PIP_SSH_TIMEOUT, 700)
        calls = []

        def fake_remote(config, state, node, command, stdin=b"", timeout=120):
            calls.append(timeout)
            return 0, "Linux" if command == "uname -s" else "ok", ""
        config = configuration()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(deploy, "_remote", fake_remote), \
                patch.object(deploy, "_ssh", return_value=(["ssh"], ["t"], "~/j")), \
                patch.object(deploy, "package", return_value=b""), \
                patch.object(deploy, "WAIT", 0), \
                patch("jarvis.release.served_code", return_value="c1"), \
                patch("jarvis.release.source", return_value=tmp), \
                patch.object(deploy, "_node_code", return_value=(None, "c0")):
            deploy.update(config, tmp, "node")
        self.assertIn(deploy.PIP_SSH_TIMEOUT, calls)


class TunnelProxyTests(unittest.TestCase):
    def test_tunnel_call_ignores_proxy_environment(self):
        import json
        from jarvis import grokbot
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "grokbot-dell.json").write_text(json.dumps({"port": 1, "token": "tok"}))
            opened = []

            class Res:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return b'{"ok": true, "text": "hi"}'
            opener = Mock(open=lambda req, timeout: opened.append(req) or Res())
            with patch.object(grokbot, "TUNNEL_DIR", Path(tmp)), \
                    patch.object(remote, "_NO_PROXY", opener), \
                    patch.object(urllib.request, "urlopen", side_effect=AssertionError("proxied")):
                machine = remote.Remote.__new__(remote.Remote)
                self.assertEqual(machine._tunnel("dell", "volume", "5"), (True, "hi"))
            self.assertEqual(len(opened), 1)
        # An empty ProxyHandler replaces the default env-driven one, so no handler proxies.
        self.assertFalse([h for h in remote._NO_PROXY.handlers
                          if getattr(h, "proxies", None)])
        self.assertNotIn("http", {k for h in remote._NO_PROXY.handlers
                                  for k in getattr(h, "proxies", {})})


if __name__ == "__main__":
    unittest.main()
