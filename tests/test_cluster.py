"""Main/node architecture tests: real loopback HTTP, mocked desktop/model actions."""

import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.cluster import Client, Hub, NodeWorker, ProxyLane, save_json, validate_action
from jarvis.cluster_proxy import proxy_handler
from jarvis.cluster_store import Broker
from jarvis.topology import ORIGIN, endpoint, identity, is_node, request_from
from jarvis.ui import Console, ConsoleServer


def configuration(local="main"):
    return {"devices": {"main": {"hostname": "main-host", "transport": "local"},
                        "node": {"hostname": "node-host", "transport": "ssh"},
                        "other": {"hostname": "other-host"}},
            "cluster": {"enabled": True, "main_device": "main", "local_device": local, "auto_update": False,
                        "main_url": "http://127.0.0.1:7778"}}


class TopologyTests(unittest.TestCase):
    def test_main_can_change_by_configuration(self):
        cfg = configuration("node")
        self.assertTrue(is_node(cfg))
        cfg["cluster"]["main_device"] = "node"
        self.assertFalse(is_node(cfg))
        self.assertEqual(identity(cfg), ("node", "node"))

    def test_unknown_auto_identity_fails_closed(self):
        cfg = configuration("auto")
        with patch("jarvis.topology.socket.gethostname", return_value="unknown"):
            with self.assertRaisesRegex(ValueError, "local_device"):
                identity(cfg)

    def test_cleartext_remote_and_credential_urls_are_refused(self):
        cfg = configuration()
        for value in ("http://192.168.1.2:7778", "https://user:secret@example.test",
                      "https://example.test/path", "https://example.test?token=x", "http://localhost:0"):
            cfg["cluster"]["main_url"] = value
            with self.assertRaises(ValueError, msg=value):
                endpoint(cfg)

    def test_node_does_not_create_model_lane_or_run_tasks(self):
        from jarvis.chatgpt import make_lane
        from jarvis.runner import run_task
        with patch("jarvis.chatgpt.ChatGPTLane") as model:
            self.assertIsInstance(make_lane(configuration("node"), ".", ".jarvis"), ProxyLane)
            model.assert_not_called()
        with self.assertRaisesRegex(ValueError, "main PC"):
            run_task(None, "unused", configuration("node"), ".")

    def test_node_ask_bypasses_local_memory_and_recall(self):
        from jarvis.assistant import ask
        lane = ProxyLane(configuration("node"), "some-state")
        memory = Mock()
        with patch("jarvis.cluster.Client") as client, patch("jarvis.assistant.vault_recall") as recall:
            client.return_value.ask.return_value = {"reply": "from main", "tier": "simple"}
            result = ask("hello", None, memory, lane.config, ".", lane=lane)
        self.assertEqual(result["via"], "main")
        memory.add.assert_not_called()
        recall.assert_not_called()

    def test_origin_defaults_desktop_to_requesting_node(self):
        from jarvis.remote import Remote
        remote = Remote(configuration(), ".jarvis")
        with patch("jarvis.cluster.dispatch", return_value={"ok": True, "text": "opened"}) as dispatch:
            with request_from("node"):
                self.assertEqual(remote.call(None, "open_app", "Spotify"), (True, "opened"))
            self.assertIsNone(ORIGIN.get())
        self.assertEqual(dispatch.call_args.args[2:4], ("node", "desktop"))
        self.assertEqual(remote.resolve("main PC"), "main")


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = Broker(self.tmp.name)

    def test_claim_is_durable_and_not_replayed_after_restart(self):
        job = self.broker.enqueue("node", "main", "desktop", {"action": "volume", "arg": "5"})
        self.assertEqual(self.broker.claim("node", {"desktop"})["id"], job)
        restarted = Broker(self.tmp.name)
        restarted.recover()
        self.assertEqual(restarted.get(job)["state"], "uncertain")
        self.assertIsNone(restarted.claim("node", {"desktop"}))

    def test_duplicate_request_id_is_idempotent_but_cannot_change_payload(self):
        key = uuid.uuid4().hex
        self.broker.enqueue("main", "node", "ask", {"text": "hello"}, job_id=key)
        self.assertEqual(self.broker.enqueue("main", "node", "ask", {"text": "hello"}, job_id=key), key)
        with self.assertRaises(ValueError):
            self.broker.enqueue("main", "node", "ask", {"text": "different"}, job_id=key)

    def test_wrong_node_cannot_complete_work_and_results_can_be_acknowledged_twice(self):
        job = self.broker.enqueue("node", "main", "inventory", {})
        self.broker.claim("node", {"inventory"})
        with self.assertRaises(LookupError):
            self.broker.finish(job, "other", {"ok": True})
        self.broker.finish(job, "node", {"ok": True})
        self.broker.finish(job, "node", {"ok": True})
        self.assertEqual(self.broker.get(job)["state"], "done")

    def test_expired_pending_and_dispatched_jobs_have_different_outcomes(self):
        queued = self.broker.enqueue("node", "main", "inventory", {})
        running = self.broker.enqueue("other", "main", "inventory", {})
        self.broker.claim("other", {"inventory"})
        with self.broker.db() as db:
            db.execute("UPDATE jobs SET deadline=0")
        self.assertEqual(self.broker.get(queued)["state"], "expired")
        self.assertEqual(self.broker.get(running)["state"], "uncertain")


class ClusterHTTPTests(unittest.TestCase):
    def setUp(self):
        # Long polls hold an empty queue for POLL_WAIT (25 s); tests don't need to wait it out.
        poll = patch("jarvis.cluster.POLL_WAIT", 0.5)
        poll.start()
        self.addCleanup(poll.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = configuration()
        self.calls = []

        def answer(payload, origin):
            self.calls.append((payload, origin, ORIGIN.get()))
            return {"reply": "main answered", "tier": "simple", "memory": []}

        self.console = Console(self.main, self.root, self.root / "main")
        self.addCleanup(self.console.close)
        self.hub = Hub(self.main, self.root, self.root / "main", console=self.console,
                       ask_handler=answer).start(port=0)
        self.addCleanup(self.hub.close)
        self.main["cluster"]["main_url"] = f"http://127.0.0.1:{self.hub.server.server_address[1]}"
        self.node = copy.deepcopy(self.main)
        self.node["cluster"]["local_device"] = "node"
        self.state = self.root / "node"
        token = self.hub.broker.enroll("node")
        save_json(self.state / "cluster-node.json", {"node_id": "node", "main_device": "main", "token": token})
        self.client = Client(self.node, self.state)

    def test_node_request_runs_on_main_with_origin_and_is_idempotent(self):
        key = uuid.uuid4().hex
        self.assertEqual(self.client.ask("hello", timeout=3, request_id=key)["reply"], "main answered")
        self.client.ask("hello", timeout=3, request_id=key)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1:], ("node", "node"))
        self.assertFalse((self.state / "memory.sqlite3").exists())

    def test_main_dispatches_work_over_node_initiated_poll(self):
        executed = []
        worker = NodeWorker(self.node, self.state,
            executor=lambda kind, payload: executed.append((kind, payload)) or {"ok": True, "text": "node done"})
        worker.once()  # authenticated heartbeat
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.hub.dispatch, "node", "desktop", {"action": "volume", "arg": "5"}, 3)
            deadline = time.monotonic() + 3
            while not future.done() and time.monotonic() < deadline:
                worker.once()
                time.sleep(0.03)
            self.assertEqual(future.result(timeout=1), {"ok": True, "text": "node done"})
        self.assertEqual(executed, [("desktop", {"action": "volume", "arg": "5"})])

    def test_node_can_control_main_only_through_main_endpoint(self):
        with patch("jarvis.cluster.execute_local", return_value={"ok": True}) as execute:
            self.assertEqual(self.client.request("/v1/control", {
                "node": "main", "kind": "desktop", "payload": {"action": "volume", "arg": "5"}}), {"ok": True})
        execute.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "cannot use"):
            self.client.request("/v1/action", {"node": "main", "kind": "inventory", "payload": {}})

    def test_revoked_token_and_wrong_main_identity_are_refused(self):
        self.client.main = "other"
        with self.assertRaisesRegex(RuntimeError, "not the configured main"):
            self.client.request("/v1/status")
        self.client.main = "main"
        self.hub.broker.revoke("node")
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            self.client.request("/v1/status")

    def test_capabilities_restrict_proxy_control_and_poll_work(self):
        token = self.hub.broker.enroll("node", {"ask"})
        self.client.token = token
        with self.assertRaises(RuntimeError):
            self.client.request("/v1/control", {"node": "main", "kind": "inventory", "payload": {}})
        self.assertEqual(self.client.request("/v1/poll", {})["job"], None)
        self.assertEqual(self.client.ask("hello", timeout=3)["reply"], "main answered")

    def test_raw_shell_operations_are_rejected(self):
        for kind, payload in (("shell", {"command": "anything"}),
                              ("desktop", {"action": "shell", "arg": "anything"})):
            with self.assertRaises(ValueError):
                validate_action(kind, payload)

    def test_loopback_console_proxies_main_state_without_exposing_token(self):
        server = ConsoleServer(("127.0.0.1", 0), proxy_handler(self.client))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(url + "/api/overview", timeout=5) as response:
            view = json.load(response)
        self.assertEqual(view["cluster"]["main_device"], "main")
        with urllib.request.urlopen(url + "/", timeout=5) as response:
            self.assertNotIn(self.client.token.encode(), response.read())
        bad = urllib.request.Request(url + "/api/ask", data=b'{"text":"x"}')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(bad, timeout=3)
        error.exception.close()
        self.assertEqual(error.exception.code, 403)

    def test_result_retry_does_not_repeat_execution(self):
        executor = Mock(return_value={"ok": True})
        worker = NodeWorker(self.node, self.state, executor=executor)
        job = {"id": uuid.uuid4().hex, "kind": "desktop", "deadline": time.time() + 5,
               "payload": {"action": "volume", "arg": "5"}}
        worker.client.request = Mock(side_effect=[{"job": job}, ConnectionError("lost reply")])
        with self.assertRaises(ConnectionError):
            worker.once()
        self.assertTrue(worker.outbox.exists())
        worker.client.request = Mock(side_effect=[{"accepted": True}, {"job": None}])
        worker.once()
        self.assertFalse(worker.outbox.exists())
        executor.assert_called_once()


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = configuration()
        self.config["devices"]["node"].update(target="jdoe@192.0.2.1",
            fallback_targets=["jdoe@192.0.2.2"], host_key_alias="known-host", identity_file="id_ed25519")

    def test_command_pins_loopback_forward_and_host_key(self):
        from jarvis.cluster_tunnel import command
        with patch("jarvis.cluster_tunnel.shutil.which", return_value="ssh"):
            args = command(self.config, self.state, "jdoe@192.0.2.1")
        for value in ("BatchMode=yes", "StrictHostKeyChecking=yes", "HostKeyAlias=known-host",
                      "ExitOnForwardFailure=yes", "ServerAliveInterval=15", "ServerAliveCountMax=2",
                      "127.0.0.1:7778:127.0.0.1:7778"):
            self.assertIn(value, args)
        self.assertEqual(args[-1], "jdoe@192.0.2.1")

    def test_pinned_key_is_not_tied_to_dhcp_address(self):
        from jarvis.cluster_tunnel import pinned_host
        options = pinned_host({"hostname": "node", "host_key": "ssh-ed25519 AAAA"}, self.state)
        self.assertIn("HostKeyAlias=jarvis-node", options)
        self.assertEqual((self.state / "known-host-jarvis-node").read_text(), "jarvis-node ssh-ed25519 AAAA\n")
        with self.assertRaises(ValueError):
            pinned_host({"host_key_alias": "bad\noption"}, self.state)

    def test_target_rotation_backoff_and_cooperative_stop(self):
        from jarvis import cluster_tunnel as tunnel
        attempts, sleeps = [], []
        proc = Mock()
        proc.poll.return_value = 255
        proc.returncode = 255

        def popen(argv, **kwargs):
            attempts.append(argv[-1])
            if len(attempts) == 4:
                (self.state / "cluster-tunnel.stop").touch()
            return proc

        clock = [0]
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with patch.object(tunnel.shutil, "which", return_value="ssh"), \
                patch.object(tunnel.subprocess, "Popen", side_effect=popen), \
                patch.object(tunnel.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(tunnel.time, "sleep", side_effect=sleep):
            tunnel.run(self.config, self.state)
        self.assertEqual(attempts, ["jdoe@192.0.2.1", "jdoe@192.0.2.2"] * 2)
        self.assertEqual(sum(sleeps), 1)
        self.assertEqual(json.loads((self.state / "cluster-tunnel.json").read_text())["pid"], 0)

    def test_fallback_switches_back_when_first_target_returns(self):
        from jarvis import cluster_tunnel as tunnel
        attempts, probes, clock = [], [], [0]
        dead, live = Mock(), Mock()
        dead.poll.return_value = dead.returncode = 255
        live.poll.return_value, live.returncode = None, 0
        live.terminate.side_effect = lambda: setattr(live.poll, "return_value", 0)

        def popen(argv, **kwargs):
            attempts.append(argv[-1])
            if len(attempts) == 3:
                (self.state / "cluster-tunnel.stop").touch()
            return live if argv[-1] == "jdoe@192.0.2.2" else dead

        def reachable(target):
            probes.append((clock[0], target))
            return clock[0] >= 2 * tunnel.LAN_RETRY

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(tunnel.shutil, "which", return_value="ssh"), \
                patch.object(tunnel.subprocess, "Popen", side_effect=popen), \
                patch.object(tunnel, "reachable", side_effect=reachable), \
                patch.object(tunnel.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(tunnel.time, "sleep", side_effect=sleep):
            tunnel.run(self.config, self.state)
        self.assertEqual(attempts, ["jdoe@192.0.2.1", "jdoe@192.0.2.2", "jdoe@192.0.2.1"])
        self.assertEqual([t for _, t in probes], ["jdoe@192.0.2.1"] * 2)
        live.terminate.assert_called_once()

    def test_http_offline_error_preserves_safe_fallback_signal(self):
        import io
        from jarvis.cluster import NodeOffline
        save_json(self.state / "cluster-node.json", {"node_id": "node", "main_device": "main", "token": "x" * 30})
        client = Client(configuration("node"), self.state)
        error = urllib.error.HTTPError("http://127.0.0.1", 409, "offline", {},
                  io.BytesIO(b'{"error":"offline","code":"node_offline"}'))
        with patch.object(client, "open", side_effect=error):
            with self.assertRaises(NodeOffline):
                client.request("/v1/control", {})


class ConnectedAndAliveTests(unittest.TestCase):
    def test_connected_with_socketpair(self):
        import socket
        from jarvis.cluster import _connected
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertTrue(_connected(a))      # open peer, nothing to read
        b.send(b"x")
        self.assertTrue(_connected(a))      # pending data is not a hang-up
        self.assertEqual(a.recv(1), b"x")   # PEEK left it unread
        b.close()
        self.assertFalse(_connected(a))     # EOF
        a.close()
        self.assertFalse(_connected(a))     # closed socket

    def hub(self, running=0):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub = Hub(configuration(), tmp.name, Path(tmp.name) / "main")
        hub.broker = Mock()
        hub.broker.running.return_value = running
        return hub

    def test_alive_rules(self):
        now = time.time()
        self.assertFalse(self.hub().alive("node", {"online": False, "seen": now}))
        self.assertTrue(self.hub().alive("node", {"online": True, "seen": now - 2}))
        self.assertFalse(self.hub().alive("node", {"online": True, "seen": now - 30}))
        self.assertTrue(self.hub(running=1).alive("node", {"online": True, "seen": now - 30}))

    def test_alive_follows_held_poll_sockets(self):
        import socket
        old = {"online": True, "seen": time.time() - 30}
        hub = self.hub()
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        hub._polling["node"] = {a}
        self.assertTrue(hub.alive("node", old))
        b.close()
        self.assertFalse(hub.alive("node", old))


if __name__ == "__main__":
    unittest.main()
