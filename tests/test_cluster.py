"""Main/node architecture tests: real loopback HTTP, mocked desktop/model actions."""

import copy
import json
import os
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
from jarvis.cluster_store import CAPABILITIES, NODE_DEFAULT, Broker
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
        token = self.hub.broker.enroll("node", CAPABILITIES)
        save_json(self.state / "cluster-node.json", {"node_id": "node", "main_device": "main", "token": token})
        self.client = Client(self.node, self.state)

    def test_node_request_runs_on_main_with_origin_and_is_idempotent(self):
        key = uuid.uuid4().hex
        self.assertEqual(self.client.ask("hello", timeout=3, request_id=key)["reply"], "main answered")
        self.client.ask("hello", timeout=3, request_id=key)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1:], ("node", "node"))
        self.assertFalse((self.state / "memory.sqlite3").exists())

    def test_job_result_request_is_held_until_the_result_lands(self):
        job = self.hub.broker.enqueue("main", "node", "ask", {"text": "slow"})
        self.hub.broker.claim("main", {"ask"})
        # Without ?wait the hub answers at once (what an old client expects).
        began = time.monotonic()
        self.assertEqual(self.client.request(f"/v1/jobs/{job}")["state"], "running")
        self.assertLess(time.monotonic() - began, 0.5)

        def finish():
            time.sleep(0.3)
            with self.hub._condition:
                self.hub.broker.finish(job, "main", {"reply": "late"})
                self.hub._condition.notify_all()
        threading.Thread(target=finish, daemon=True).start()
        began = time.monotonic()
        answer = self.client.request(f"/v1/jobs/{job}?wait=5")
        self.assertEqual((answer["state"], answer["result"]), ("done", {"reply": "late"}))
        self.assertTrue(0.2 < time.monotonic() - began < 2)

    def test_wait_makes_one_held_request_not_a_poll_loop(self):
        job = self.hub.broker.enqueue("main", "node", "ask", {"text": "slow"})
        self.hub.broker.claim("main", {"ask"})
        paths = []
        real = self.client.request

        def spy(path, body=None, timeout=45):
            paths.append(path)
            return real(path, body, timeout)

        def finish():
            time.sleep(0.5)
            with self.hub._condition:
                self.hub.broker.finish(job, "main", {"reply": "ok"})
                self.hub._condition.notify_all()
        threading.Thread(target=finish, daemon=True).start()
        with patch.object(self.client, "request", side_effect=spy):
            self.assertEqual(self.client.wait(job, 10), {"reply": "ok"})
        self.assertEqual(len(paths), 1)

    def test_wait_backs_off_when_the_hub_ignores_wait(self):
        states = iter([{"state": "running"}] * 6 + [{"state": "done", "result": 7}])
        sleeps = []
        with patch.object(self.client, "request", side_effect=lambda *a, **k: next(states)), \
                patch("jarvis.cluster.time.sleep", side_effect=sleeps.append):
            self.assertEqual(self.client.wait("x", 100), 7)
        self.assertEqual(sleeps, [0.15, 0.3, 0.6, 1.2, 2.0, 2.0])

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

    def test_invite_is_single_use_and_short_lived(self):
        from jarvis.cluster import redeem_invite
        invite = self.hub.broker.enroll("node", CAPABILITIES, invite_ttl=3600)
        key = self.state / "node.key"
        save_json(self.state / "cluster-node.json", {
            "node_id": "node", "main_device": "main", "token": invite, "redeem": True,
            "link": {"identity_file": str(key)}})
        joined, agent = Client(self.node, self.state), Client(self.node, self.state)
        # Never touch this PC's real authorized_keys or run ssh-keygen in a test.
        with patch("jarvis.cluster_setup.keypair", return_value=("PRIVATE\n", "ssh-ed25519 AAAA x")), \
                patch("jarvis.cluster_setup.authorize") as authorize:
            self.assertEqual(redeem_invite(self.node, self.state, joined), {"token": True, "ssh_key": True})
            self.assertIsNone(redeem_invite(self.node, self.state, joined))  # nothing left to redeem
        authorize.assert_called_once()
        self.assertEqual(key.read_text(), "PRIVATE\n")
        note = json.loads((self.state / "cluster-node.json").read_text())
        self.assertNotIn("redeem", note)
        self.assertNotEqual(note["token"], invite)
        joined.request("/v1/status")
        agent.request("/v1/status")  # started on the invite: picks up the saved token once
        self.assertEqual(agent.token, note["token"])
        leaked = Client(self.node, self.state)
        leaked.token, leaked.invite = invite, False
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            leaked.request("/v1/status")  # the invite code is dead once used
        with self.assertRaises(PermissionError):
            self.hub.broker.redeem("node")  # and can't be redeemed twice
        unused = self.hub.broker.enroll("late", CAPABILITIES, invite_ttl=-1)
        self.assertIsNone(self.hub.broker.authenticate(unused))  # nobody joined in time

    def test_capabilities_restrict_proxy_control_and_poll_work(self):
        token = self.hub.broker.enroll("node", {"ask"})
        self.client.token = token
        with self.assertRaises(RuntimeError):
            self.client.request("/v1/control", {"node": "main", "kind": "inventory", "payload": {}})
        self.assertEqual(self.client.request("/v1/poll", {})["job"], None)
        self.assertEqual(self.client.ask("hello", timeout=3)["reply"], "main answered")

    def test_files_node_may_hand_the_main_a_transfer_and_nothing_else(self):
        self.client.token = self.hub.broker.enroll("node", {"files"})
        with patch("jarvis.cluster.execute_local", return_value={"entries": []}) as execute:
            self.assertEqual(self.client.request("/v1/control", {
                "node": "main", "kind": "transfer", "payload": {"action": "inbox"}}), {"entries": []})
        execute.assert_called_once()
        for body in ({"node": "main", "kind": "desktop", "payload": {"action": "volume", "arg": "5"}},
                     {"node": "other", "kind": "transfer", "payload": {"action": "inbox"}}):
            with self.subTest(body=body), self.assertRaisesRegex(RuntimeError, "cannot use"):
                self.client.request("/v1/control", body)

    def test_large_transfer_chunk_reaches_the_node_and_is_compacted(self):
        executed = []
        worker = NodeWorker(self.node, self.state,
            executor=lambda kind, payload: executed.append(len(payload["data"])) or {"received": 1})
        worker.once()
        payload = {"action": "put", "upload": "c" * 32, "offset": 0, "name": "f", "last": False,
                   "data": "A" * 1_000_000}  # over the 64 KB control limit, under the bulk one
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.hub.dispatch, "node", "transfer", payload, 5)
            deadline = time.monotonic() + 5
            while not future.done() and time.monotonic() < deadline:
                worker.once()
                time.sleep(0.03)
            self.assertEqual(future.result(timeout=1), {"received": 1})
        self.assertEqual(executed, [1_000_000])
        with self.hub.broker.db() as db:
            self.assertEqual(db.execute("SELECT payload FROM jobs WHERE kind='transfer'").fetchone()[0], "{}")

    def _api(self, path, body=None):
        try:
            with self.client.open(path, None if body is None else json.dumps(body).encode(), 5,
                                  {"X-JARVIS": "1", "Content-Type": "application/json"}) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code

    def test_new_nodes_get_only_node_work_by_default(self):
        self.hub.broker.enroll("other")
        caps = {n["id"]: set(n["capabilities"]) for n in self.hub.broker.nodes()}
        self.assertEqual(caps["other"], set(NODE_DEFAULT))
        self.assertFalse(caps["other"] & {"ask", "console"})

    def test_node_console_reaches_only_listed_main_api(self):
        self.assertEqual(self._api("/api/overview"), 200)
        self.assertEqual(self._api("/api/jobs/x/../../settings"), 403)
        for path in ("/api/settings", "/api/fs?device=main&path=~/.ssh", "/api/screen",
                     "/api/memory", "/api/export?case=x"):
            self.assertEqual(self._api(path), 403, path)
        with patch.object(Console, "grokbot_command") as command, \
                patch.object(Console, "submit") as submit:
            for path, body in (("/api/grokbot", {"text": "id", "mode": "terminal"}),
                               ("/api/settings", {"changes": {}}),
                               ("/api/fs/copy", {}), ("/api/plans/x/run", {})):
                self.assertEqual(self._api(path, body), 403, path)
            command.assert_not_called()
            submit.return_value = {"id": "j"}
            self.assertEqual(self._api("/api/ask", {"text": "hi"}), 200)
            self.hub.broker.set_capabilities("node", {"console"})
            self.assertEqual(self._api("/api/ask", {"text": "hi"}), 403)
        submit.assert_called_once()

    def test_node_cannot_start_a_write_run(self):
        with self.assertRaisesRegex(RuntimeError, "only on the main"):
            self.client.ask("change the code", write=True, timeout=3)
        self.assertEqual(self.calls, [])

    def test_set_capabilities_keeps_the_credential(self):
        self.assertEqual(self.hub.broker.set_capabilities("node", {"desktop"}), ["desktop"])
        self.assertEqual(self.client.request("/v1/poll", {})["job"], None)
        with self.assertRaises(RuntimeError):
            self.client.ask("hello", timeout=3)
        with self.assertRaises(LookupError):
            self.hub.broker.set_capabilities("ghost", {"desktop"})

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


    def test_job_reported_not_run_during_update_is_never_run(self):
        sent, executor = [], Mock(return_value={"ok": True})
        worker = NodeWorker(self.node, self.state, executor=executor)
        worker._send_result = sent.append
        job = {"id": uuid.uuid4().hex, "kind": "desktop", "deadline": time.time() + 5,
               "payload": {"action": "volume", "arg": "5"}}
        worker.client.request = Mock(return_value={"job": job, "code": "newer-code"})
        with patch.object(worker, "_pull_update", side_effect=RuntimeError("bundle refused")):
            self.assertTrue(worker.once())
        executor.assert_not_called()
        self.assertEqual(len(sent), 1)
        self.assertIn("Not run", sent[0]["result"]["error"])

    def test_only_the_unit_itself_counts_as_systemd(self):
        from jarvis.cluster import _systemd_service
        unit = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/jarvis-cluster.service\n"
        shell = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-kitty-1.scope\n"
        for text, expected in ((unit, True), (shell, False)):
            with patch("jarvis.cluster.Path.read_text", return_value=text), \
                    patch.dict(os.environ, {"INVOCATION_ID": "inherited"}):
                self.assertIs(_systemd_service("jarvis-cluster"), expected)

    def test_lost_redeem_reply_can_be_retried(self):
        from jarvis.cluster import redeem_invite
        invite = self.hub.broker.enroll("node", CAPABILITIES, invite_ttl=3600)
        save_json(self.state / "cluster-node.json", {
            "node_id": "node", "main_device": "main", "token": invite, "redeem": True,
            "link": {"identity_file": str(self.state / "node.key")}})
        joined = Client(self.node, self.state)
        real = joined.request
        calls = []

        def lose_first_reply(path, body=None, timeout=45):
            answer = real(path, body, timeout)
            calls.append(path)
            if len(calls) == 1:
                raise ConnectionError("reply lost")
            return answer
        joined.request = lose_first_reply
        with patch("jarvis.cluster_setup.keypair", return_value=("PRIVATE\n", "ssh-ed25519 AAAA x")), \
                patch("jarvis.cluster_setup.authorize") as authorize, \
                patch("jarvis.cluster_setup.authorized", return_value=True) as authorized:
            with self.assertRaises(ConnectionError):
                redeem_invite(self.node, self.state, joined)  # the main redeemed; we never heard
            self.assertEqual(redeem_invite(self.node, self.state, joined), {"token": True, "ssh_key": True})
        authorize.assert_called_once()  # the retry only checks the first key landed
        authorized.assert_called_once_with("node", public_key="ssh-ed25519 AAAA x")
        note = json.loads((self.state / "cluster-node.json").read_text())
        self.assertNotIn("pending", note)
        Client(self.node, self.state).request("/v1/status")  # the node is not locked out

    def test_node_token_cannot_re_pair_a_new_ssh_key(self):
        invite = self.hub.broker.enroll("node", CAPABILITIES, invite_ttl=3600)
        with patch("jarvis.cluster_setup.authorize") as authorize, \
                patch("jarvis.cluster_setup.authorized", return_value=False):
            token = "t" * 40
            self.assertEqual(self.hub.redeem("node", "ssh-ed25519 AAAA first", token),
                             {"token": token, "key_rotated": True})
            self.assertEqual(self.hub.broker.authenticate(token)[0], "node")
            again = self.hub.redeem("node", "ssh-ed25519 BBBB attacker", token)
        authorize.assert_called_once()  # only the first pairing's key is written
        self.assertEqual(again, {"token": token, "key_rotated": False})
        self.assertNotEqual(invite, token)

    def test_executor_crash_reports_may_have_run_but_expiry_does_not(self):
        sent = []
        worker = NodeWorker(self.node, self.state, executor=Mock(side_effect=RuntimeError("boom")))
        worker._send_result = sent.append
        job = {"id": uuid.uuid4().hex, "kind": "desktop", "deadline": time.time() + 5,
               "payload": {"action": "close_app", "arg": "Spotify"}}
        worker.client.request = Mock(return_value={"job": job})
        worker.once()
        self.assertTrue(sent[-1]["failed"])
        self.assertIn("may have run", sent[-1]["result"]["error"])
        worker.executor = Mock()
        job = {**job, "id": uuid.uuid4().hex, "deadline": time.time() - 1}
        worker.client.request = Mock(return_value={"job": job})
        worker.once()
        worker.executor.assert_not_called()
        self.assertNotIn("may have run", sent[-1]["result"]["error"])


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
                      "127.0.0.1:7780:127.0.0.1:7780"):
            self.assertIn(value, args)
        self.assertEqual(args[-1], "jdoe@192.0.2.1")

    def test_pinned_key_is_not_tied_to_dhcp_address(self):
        from jarvis.cluster_tunnel import pinned_host
        options = pinned_host({"hostname": "node", "host_key": "ssh-ed25519 AAAA"}, self.state)
        self.assertIn("HostKeyAlias=jarvis-node", options)
        self.assertEqual((self.state / "known-host-jarvis-node").read_text(), "jarvis-node ssh-ed25519 AAAA\n")
        with self.assertRaises(ValueError):
            pinned_host({"host_key_alias": "bad\noption"}, self.state)

    def test_status_note_is_written_on_change_or_heartbeat_not_every_tick(self):
        from jarvis import cluster_tunnel as tunnel
        proc = Mock()
        polls = [None] * 200 + [255]
        proc.poll.side_effect = lambda: polls.pop(0) if polls else 255
        proc.returncode = 255
        saves, clock = [], [0]

        def sleep(seconds):
            clock[0] += seconds
            if not polls:
                (self.state / "cluster-tunnel.stop").touch()

        real = tunnel.save_json
        with patch.object(tunnel.shutil, "which", return_value="ssh"), \
                patch.object(tunnel.subprocess, "Popen", return_value=proc), \
                patch.object(tunnel, "save_json", side_effect=lambda *a: saves.append(a[1]["updated"]) or real(*a)), \
                patch.object(tunnel.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(tunnel.time, "sleep", side_effect=sleep):
            tunnel.run(self.config, self.state)
        # ~200 loop passes (several publishes each) used to be one write apiece.
        self.assertLessEqual(len(saves), 8)
        self.assertEqual(json.loads((self.state / "cluster-tunnel.json").read_text())["pid"], 0)

    def test_heartbeat_refreshes_an_unchanged_note(self):
        from jarvis import cluster_tunnel as tunnel
        proc = Mock()
        proc.poll.return_value = None
        stamps = iter(range(1000, 2000, 2))
        saves = []

        def sleep(seconds):
            if len(saves) >= 3:
                (self.state / "cluster-tunnel.stop").touch()
        with patch.object(tunnel.shutil, "which", return_value="ssh"), \
                patch.object(tunnel.subprocess, "Popen", return_value=proc), \
                patch.object(tunnel.time, "time", side_effect=lambda: next(stamps)), \
                patch.object(tunnel, "save_json", side_effect=lambda *a: saves.append(a[1]["updated"])), \
                patch.object(tunnel.time, "sleep", side_effect=sleep):
            tunnel.run(self.config, self.state)
        self.assertGreaterEqual(len(saves), 3)
        self.assertTrue(all(b - a >= tunnel.HEARTBEAT for a, b in zip(saves, saves[1:3])))

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
