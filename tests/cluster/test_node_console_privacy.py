"""A console-capable node must not read other requests' text through the main's console API.

Real loopback hub + real Console; fake brain (nothing here asks a model). The sentinel strings
stand for David's private requests, replies and recalled memory held on the main."""

import copy
import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import cluster
from jarvis.cluster import Client, Hub, NodeWorker, node_api, node_fields, node_job, save_json
from jarvis.cluster_store import CAPABILITIES
from jarvis.ui import Console

SECRET = "PRIVATE-REQUEST-7f3a9c"
MINE = "NODE-OWN-REQUEST-51d2"


def configuration():
    return {"devices": {"main": {"hostname": "main-host", "transport": "local"},
                        "node": {"hostname": "node-host", "transport": "ssh"},
                        "other": {"hostname": "other-host"}},
            "cluster": {"enabled": True, "main_device": "main", "local_device": "main",
                        "auto_update": False, "main_url": "http://127.0.0.1:7778"}}


class NodeAuthorizationTests(unittest.TestCase):
    """node_api / node_fields / node_job on their own: no server."""

    DENIED = ("/api/events", "/api/history", "/api/history/commands", "/api/cases",
              "/api/tasks/abc", "/api/plans/abc", "/api/memory", "/api/memory/item",
              "/api/settings", "/api/files", "/api/export", "/api/kb/state", "/api/vault/brief",
              "/api/inbox", "/api/screen", "/api/graph/heap", "/api/jobs/", "/api/jobs/a/b",
              "/api/jobs/a/../../history", "/api/events/", "/api/history/", "/API/history")

    def test_content_routes_and_path_variants_are_denied_for_a_node(self):
        for path in self.DENIED:
            with self.subTest(path=path), self.assertRaises(PermissionError):
                node_api(path, False, {"console"})

    def test_nodes_still_get_the_metadata_and_scoped_routes(self):
        for path in ("/api/overview", "/api/confirm", "/api/route", "/api/usage", "/api/credits",
                     "/api/voice", "/api/agent", "/api/grokbot", "/api/jobs/abc123"):
            with self.subTest(path=path):
                self.assertIsNone(node_api(path, False, {"console"}))

    def test_posts_still_need_the_ask_capability(self):
        with self.assertRaises(PermissionError):
            node_api("/api/ask", True, {"console"})
        self.assertIsNone(node_api("/api/ask", True, {"console", "ask"}))
        with self.assertRaises(PermissionError):
            node_api("/api/history", True, {"console", "ask"})

    def test_fields_projection_keeps_only_listed_top_level_keys(self):
        body = {"version": "1", "memory": {"recent": [SECRET]}, "tasks": [SECRET],
                "plans": [SECRET], "devices": [{"id": "main"}], "todo": SECRET}
        view = node_fields("/api/overview", body)
        self.assertEqual(set(view), {"version", "devices"})
        self.assertNotIn(SECRET, json.dumps(view))
        confirm = node_fields("/api/confirm", {"pending": [], "grants": [], "recent": [SECRET]})
        self.assertEqual(set(confirm), {"pending", "grants"})
        self.assertEqual(node_fields("/api/usage", {"a": 1}), {"a": 1})  # unlisted: untouched

    def test_a_node_reads_only_a_job_its_own_request_started(self):
        class Fake:
            jobs = {"mine": {"origin_device": "node"}, "other": {"origin_device": "laptop"},
                    "main": {"origin_device": None}, "plan": {}}

            def job(self, job_id):
                try:
                    return self.jobs[job_id]
                except KeyError:
                    raise LookupError("Unknown job.") from None

        node_job(Fake(), "node", "/api/jobs/mine")
        for name in ("other", "main", "plan", "missing"):
            with self.subTest(job=name), self.assertRaises(PermissionError):
                node_job(Fake(), "node", f"/api/jobs/{name}")


class NodeConsoleHTTPTests(unittest.TestCase):
    def setUp(self):
        _skip = patch.object(NodeWorker, "_untrusted", return_value=None)
        _skip.start()
        self.addCleanup(_skip.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        main = configuration()
        self.console = Console(main, root, root / "main")
        self.addCleanup(self.console.close)
        # David's private request, its reply, and recalled memory, all on the main.
        question = self.console.memory.add("message", SECRET, tags=["simple"], meta={"channel": "voice"})
        answer = self.console.memory.add("note", f"reply {SECRET}", meta={})
        self.console.memory.link(answer["id"], question["id"], "answers")
        self.console.memory.add("note", f"memory {SECRET}", title=f"title {SECRET}")
        with self.console._jobs_lock:
            self.console.jobs["mainjob"] = {"id": "mainjob", "text": SECRET, "state": "done",
                                            "origin_device": None, "result": {"reply": SECRET}}
            self.console.jobs["mine"] = {"id": "mine", "text": MINE, "state": "done",
                                         "origin_device": "node", "result": {"reply": MINE}}
            self.console.jobs["theirs"] = {"id": "theirs", "text": SECRET, "state": "done",
                                           "origin_device": "other", "result": {"reply": SECRET}}
        self.hub = Hub(main, root, root / "main", console=self.console).start(port=0)
        self.addCleanup(self.hub.close)
        main["cluster"]["main_url"] = f"http://127.0.0.1:{self.hub.server.server_address[1]}"
        node = copy.deepcopy(main)
        node["cluster"]["local_device"] = "node"
        state = root / "node"
        token = self.hub.broker.enroll("node", CAPABILITIES)
        save_json(state / "cluster-node.json", {"node_id": "node", "main_device": "main", "token": token})
        self.client = Client(node, state)
        self.port = self.hub.server.server_address[1]

    def get(self, path, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path, headers={
                "Authorization": f"Bearer {token or self.client.token}", "X-JARVIS": "1",
                "X-JARVIS-Main": "main"})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            connection.close()

    def test_node_is_denied_every_content_route_in_every_spelling(self):
        for path in ("/api/history", "/api/history?limit=200", "/api/history;x=1",
                     "/api/history/commands", "/api/history/commands?limit=50",
                     "/api/events", "/api/events?after=0", "/api/cases", "/api/tasks/abc",
                     "/api/tasks/abc?x=1", "/api/plans/abc", "/api/jobs/mainjob",
                     "/api/jobs/theirs", "/api/jobs/missing", "/api/jobs/mainjob/../../history",
                     "/api/history/", "/api/memory?q=x", "/api/settings"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 403)
                self.assertNotIn(SECRET, body)

    def test_node_overview_and_confirm_carry_no_private_content(self):
        status, body = self.get("/api/overview")
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertNotIn(SECRET, body)
        self.assertTrue({"version", "cluster", "devices"} <= set(view))
        for key in ("memory", "tasks", "plans", "todo", "orchestration", "agents", "network", "desktop"):
            self.assertNotIn(key, view)
        status, body = self.get("/api/confirm")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body)), {"pending", "grants"})

    def test_node_reads_its_own_job_and_nobody_elses(self):
        status, body = self.get("/api/jobs/mine")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["reply"], MINE)

    def test_a_node_without_the_console_capability_is_refused_everywhere(self):
        self.hub.broker.set_capabilities("node", {"ask"})
        self.assertEqual(self.get("/api/overview")[0], 403)
        self.assertEqual(self.get("/api/jobs/mine")[0], 403)

    def test_owner_keeps_full_access(self):
        owner = self.hub.owner
        status, body = self.get("/api/history", owner)
        self.assertEqual(status, 200)
        self.assertIn(SECRET, body)
        status, body = self.get("/api/overview", owner)
        view = json.loads(body)
        self.assertEqual(status, 200)
        for key in ("memory", "tasks", "plans", "todo", "network"):
            self.assertIn(key, view)
        self.assertIn(SECRET, body)  # recalled memory is in the owner's overview
        for job, text in (("mainjob", SECRET), ("theirs", SECRET), ("mine", MINE)):
            status, body = self.get(f"/api/jobs/{job}", owner)
            self.assertEqual((status, text in body), (200, True), job)
        self.assertNotEqual(self.get("/api/tasks/abc", owner)[0], 403)  # a task id, not auth
        self.assertNotEqual(self.get("/api/plans/abc", owner)[0], 403)
        self.assertNotEqual(self.get("/api/history/commands", owner)[0], 403)
        self.assertNotEqual(self.get("/api/cases", owner)[0], 403)

    def test_the_node_status_endpoint_still_answers(self):
        status, body = self.get("/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["main_device"], "main")
        # a node_path from an /api/ call must not leak into the next request on the connection
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Authorization": f"Bearer {self.client.token}", "X-JARVIS": "1",
                   "X-JARVIS-Main": "main"}
        try:
            connection.request("GET", "/api/overview", headers=headers)
            connection.getresponse().read()
            connection.request("GET", "/v1/status", headers=headers)
            response = connection.getresponse()
            self.assertIn("nodes", json.loads(response.read()))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
