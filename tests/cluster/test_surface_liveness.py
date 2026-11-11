"""One liveness word everywhere: a node mid-job that last checked in over 75 s ago is "busy,
awaiting contact": not proved online, not offline. Connections, Fleet and the phone read the same
projection (connections.presence). Temp broker only; nothing polls a device."""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from jarvis import connections, fleet_management as fm
from jarvis.cluster_store import Broker, NODE_DEFAULT

CONFIG = {"cluster": {"enabled": True, "main_device": "main", "local_device": "main", "tunnel": "forward",
                      "main_url": "http://127.0.0.1:7778"},
          "devices": {"main": {"label": "Main", "hostname": "m", "transport": "local"},
                      "dell": {"label": "Dell", "hostname": "d", "transport": "cluster", "lan_ip": "10.1.2.3"}}}


class Liveness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.broker = Broker(self.state)
        self.broker.enroll("dell", sorted(NODE_DEFAULT), invite_ttl=3600)
        self.seen(time.time() - 100)  # past the 75 s window, not a minute-old

    def seen(self, when):
        db = sqlite3.connect(self.state / "cluster.sqlite3")
        db.execute("UPDATE nodes SET invite_expires=NULL, seen=? WHERE id='dell'", (when,))
        db.commit()
        db.close()

    def run_job(self, deadline_in=600):
        job = self.broker.enqueue("dell", "main", "ask", {"text": "x"}, timeout=deadline_in)
        self.assertEqual(self.broker.claim("dell", ["ask"])["id"], job)

    def devs(self, audience):
        return {d["id"]: d for d in fm.inventory(CONFIG, self.state, audience=audience)["devices"]}

    def test_pure_projection(self):
        now = 1000.0
        self.assertEqual(connections.presence(now - 10, False, now), "online")
        self.assertEqual(connections.presence(now - 10, True, now), "online")   # proof beats busy
        self.assertEqual(connections.presence(now - 100, True, now), "busy")
        self.assertEqual(connections.presence(now - 100, False, now), "offline")
        self.assertEqual(connections.presence(None, True, now), "busy")
        self.assertEqual(connections.presence(None, False, now), "offline")

    def test_busy_seen_100s_is_busy_in_every_view_and_never_proved_online(self):
        self.run_job()
        node = {n["id"]: n for n in connections.snapshot(CONFIG, self.state)["cluster"]["nodes"]}["dell"]
        self.assertEqual((node["presence"], node["online"], node["busy"]), ("busy", False, True))
        owner = self.devs("owner")["dell"]
        self.assertEqual((owner["status"], owner["presence"], owner["online"]), ("busy", "busy", False))
        phone = self.devs("phone")["dell"]   # the phone keeps the proved word in status, same projection beside it
        self.assertEqual((phone["status"], phone["presence"], phone["online"]), ("offline", "busy", False))
        # read-only and no location: the busy projection adds one word, not addresses or write actions
        for key in ("address", "hostname", "active_jobs", "invite_expires", "code_ok", "lat", "location"):
            self.assertNotIn(key, phone)
        self.assertNotIn("locate", phone["capabilities"])
        view = fm.inventory(CONFIG, self.state, audience="phone")
        self.assertFalse(view["can_write"])
        self.assertEqual([a["id"] for a in view["actions"] if a["id"] in ("invite", "revoke", "capabilities")
                          and a["support"] != "blocked"], [])

    def test_genuine_offline_and_old_payload_stay_offline(self):
        for audience in ("owner", "phone"):
            d = self.devs(audience)["dell"]
            self.assertEqual((d["status"], d["presence"], d["online"]), ("offline", "offline", False))
        node = {n["id"]: n for n in connections.snapshot(CONFIG, self.state)["cluster"]["nodes"]}["dell"]
        self.assertEqual((node["presence"], node["busy"]), ("offline", False))
        # a job whose deadline passed long ago no longer counts (a hung job stops being "busy")
        self.run_job(deadline_in=1)
        db = sqlite3.connect(self.state / "cluster.sqlite3")
        db.execute("UPDATE jobs SET deadline=?", (time.time() - 600,))
        db.commit()
        db.close()
        self.assertEqual(self.devs("owner")["dell"]["status"], "offline")

    def test_online_within_the_window_is_online_with_or_without_a_job(self):
        self.seen(time.time() - 5)
        self.run_job()
        self.assertEqual(self.devs("owner")["dell"]["status"], "online")
        self.assertTrue(self.devs("phone")["dell"]["online"])

    def test_busy_never_hides_an_unenrolled_state_or_the_enrolled_guard(self):
        self.run_job()
        row = fm._db_rows(self.state)[0]["dell"]
        self.assertEqual(fm.link_state(row), "offline")   # invite/enrolled guards still read the old words

    def test_no_broker_file_is_not_created_by_reading(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(connections.busy_nodes(Path(empty)), set())
            self.assertFalse((Path(empty) / "cluster.sqlite3").exists())

    def test_capabilities_carry_titles_and_ids_stay_in_the_payload(self):
        caps = {c["id"]: c for c in connections.snapshot(CONFIG, self.state)["cluster"]["nodes"][0]["capabilities"]}
        self.assertEqual(caps["files"]["title"], "Files and clipboard")
        self.assertIn("does", caps["files"])
        json.dumps(caps)  # plain data


class ArchiveProjection(unittest.TestCase):
    def test_allowlist_carries_partial_file_fields_and_nothing_free_text(self):
        from unittest import mock
        full = {"enabled": True, "state": "partial", "files_stale": True, "archive_baseline_pending": 2,
                "archive_baseline_seeded": True, "last_error": "secret path /home/x", "files": {"x": 1},
                "capture_complete": True, "capture_enabled": True}
        with mock.patch("jarvis.second_brain_runtime.status", return_value=full):
            out = fm.archive_status({}, Path("."))
        self.assertEqual((out["state"], out["files_stale"], out["archive_baseline_pending"]), ("partial", True, 2))
        self.assertTrue(out["has_error"])
        self.assertNotIn("last_error", json.dumps(out))
        self.assertNotIn("files", out)


if __name__ == "__main__":
    unittest.main()
