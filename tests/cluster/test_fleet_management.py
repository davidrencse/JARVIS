"""Fleet management backend (jarvis/fleet_management.py + the Console's /api/fleet routes).

Everything is fake or temporary: cluster_setup.invite's key/sshd side effects are patched (a
test must never touch ~/.ssh/authorized_keys, start sshd or rotate a real credential), the
config lives in a temp folder, git runs only on a temp repo. Nothing here calls a model."""

import copy
import http.client
import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from jarvis import cluster_setup, fleet_management as fm
from jarvis.cluster import Hub
from jarvis.cluster_store import CAPABILITIES, NODE_DEFAULT, Broker
from jarvis.config_lock import ConfigBusy
from jarvis.ui import Console, Handler

CODE = "jarvis-invite-1:SECRET-CODE-PAYLOAD-9f2c"
HAND_TUNED = {"keep": {"me": [1, 2, 3]}, "rules": {"confirm": "ask"}}


def config(**cluster):
    return {"cluster": {"enabled": True, "main_device": "main", "local_device": "main", "tunnel": "forward",
                        "auto_update": False, "main_url": "http://127.0.0.1:7778", **cluster},
            "devices": {"main": {"label": "Main", "hostname": "main-host", "transport": "local"},
                        "dell": {"label": "Dell", "hostname": "dell-host", "transport": "cluster",
                                 "lan_ip": "10.1.2.3"}},
            **HAND_TUNED}


def fake_invite(calls, code=CODE):
    """Stands in for cluster_setup.invite: records, enrolls for real in the temp broker (so the
    rotation / pending state is genuine) and returns a code, touching no key file."""
    def invite(cfg, path, state, node, extra=(), capabilities=None, output=None):
        calls.append({"node": node, "extra": list(extra), "capabilities": sorted(capabilities or NODE_DEFAULT),
                      "devices": sorted(cfg.get("devices"))})
        caps = sorted(capabilities or NODE_DEFAULT)
        Broker(state).enroll(node, caps, invite_ttl=cluster_setup.INVITE_TTL)
        return {"invited": node, "capabilities": caps, "addresses": list(extra) or ["100.1.1.1"],
                "authorized_keys": "/never/written", "expires": "in 24 hours, or once used", "code": code}
    return invite


class FleetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / ".jarvis"
        self.state.mkdir()
        self.path = self.root / "jarvis.config.json"
        self.config = config()
        self.path.write_text(json.dumps(self.config, indent=2))
        self.calls, self.revoked = [], []
        fm._GIT_CACHE.clear()
        patches = [mock.patch.object(cluster_setup, "invite", fake_invite(self.calls)),
                   mock.patch.object(cluster_setup, "_sshd_up", lambda: True),
                   mock.patch.object(cluster_setup, "unauthorize", lambda node: self.revoked.append(node) or True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def invite(self, **body):
        body = {"request_id": "req-0001", "node": "oracle1", "profile": "cloud", **body}
        return fm.issue_invite(self.config, self.path, self.state, body)

    def refused(self, fn, *args):
        with self.assertRaises(fm.FleetError) as caught:
            fn(*args)
        return caught.exception

    def set_row(self, node, **fields):
        """Direct sqlite edit of the temp broker: time travel for 'expired' and 'online'."""
        import sqlite3
        db = sqlite3.connect(self.state / "cluster.sqlite3")
        for k, v in fields.items():
            db.execute(f"UPDATE nodes SET {k}=? WHERE id=?", (v, node))
        db.commit()
        db.close()

    def config_text(self):
        return self.path.read_text()


class InventoryTests(FleetBase):
    def test_no_state_is_readable_and_creates_nothing(self):
        view = fm.inventory(self.config, self.state)
        self.assertFalse((self.state / "cluster.sqlite3").exists())
        self.assertEqual([d["id"] for d in view["devices"]], ["main", "dell"])
        main, dell = view["devices"]
        self.assertEqual((main["role"], main["status"], main["main_eligible"]), ("main", "main", False))
        self.assertEqual((dell["status"], dell["online"]), ("not_enrolled", False))
        self.assertTrue(view["can_write"])
        self.assertEqual({p["id"] for p in view["profiles"]}, {"pc", "headless", "cloud"})
        self.assertEqual({c["id"] for c in view["capabilities"]}, CAPABILITIES)

    def test_link_states_follow_the_broker(self):
        now = time.time()
        broker = Broker(self.state)
        broker.enroll("dell", sorted(NODE_DEFAULT), invite_ttl=3600)
        states = lambda: fm.inventory(self.config, self.state, now=time.time())["devices"][1]
        self.assertEqual(states()["status"], "invite_pending")
        self.assertGreater(states()["invite_expires"], now)
        self.set_row("dell", invite_expires=now - 5)
        self.assertEqual(states()["status"], "invite_expired")
        self.set_row("dell", invite_expires=None, seen=time.time())
        self.assertEqual((states()["status"], states()["online"]), ("online", True))
        self.set_row("dell", seen=time.time() - 600)
        self.assertEqual((states()["status"], states()["online"]), ("offline", False))
        self.set_row("dell", seen=None)
        self.assertEqual(states()["status"], "waiting_for_first_contact")

    def test_owner_view_has_no_token_hash_or_invite_code(self):
        Broker(self.state).enroll("dell", sorted(NODE_DEFAULT), invite_ttl=3600)
        text = json.dumps(fm.inventory(self.config, self.state))
        import sqlite3
        db = sqlite3.connect(self.state / "cluster.sqlite3")
        token_hash = db.execute("SELECT token_hash FROM nodes WHERE id='dell'").fetchone()[0]
        db.close()
        self.assertTrue(token_hash)
        self.assertNotIn(token_hash, text)
        self.assertNotIn("jarvis-invite", text)
        self.assertNotIn("token", text.lower().replace("tokens", ""))

    def test_phone_audience_is_a_safe_projection(self):
        Broker(self.state).enroll("dell", sorted(NODE_DEFAULT), invite_ttl=3600)
        view = fm.inventory(self.config, self.state, audience="phone")
        text = json.dumps(view)
        for leaked in ("10.1.2.3", "dell-host", "main-host", "invite_expires", "hostname", "address"):
            self.assertNotIn(leaked, text)
        self.assertIsNone(view["cluster"]["local"])
        self.assertFalse(view["can_write"])
        support = {a["id"]: a["support"] for a in view["actions"]}
        for blocked in ("invite", "revoke", "capabilities", "main_transfer"):
            self.assertEqual(support[blocked], "blocked")
        self.assertEqual(support["setup_guide"], "guide")
        self.assertNotIn("preview", support)

    def test_actions_distinguish_supported_guide_blocked_and_unsupported(self):
        support = {a["id"]: a["support"] for a in fm.inventory(self.config, self.state)["actions"]}
        self.assertEqual(support["invite"], "supported")
        self.assertEqual(support["main_transfer"], "blocked")
        self.assertEqual(support["cloud_provisioning"], "unsupported")
        self.assertEqual(support["setup_guide"], "guide")

    def test_on_a_node_or_with_the_cluster_off_writes_are_blocked(self):
        node_view = config()
        node_view["cluster"]["local_device"] = "dell"
        view = fm.inventory(node_view, self.state)
        self.assertFalse(view["can_write"])
        self.assertEqual({a["id"]: a["support"] for a in view["actions"]}["invite"], "blocked")
        off = config(enabled=False)
        self.assertFalse(fm.inventory(off, self.state)["can_write"])

    def test_cloud_profile_is_never_main_eligible_and_defaults_to_inventory(self):
        cfg = config()
        cfg["devices"]["oracle1"] = {"label": "Oracle", "profile": "cloud"}
        cfg["devices"]["box"] = {"label": "Box", "profile": "headless"}
        by_id = {d["id"]: d for d in fm.inventory(cfg, self.state)["devices"]}
        self.assertIs(by_id["oracle1"]["main_eligible"], False)
        self.assertEqual(by_id["box"]["main_eligible"], "blocked")
        profiles = {p["id"]: p for p in fm.inventory(cfg, self.state)["profiles"]}
        self.assertEqual(profiles["cloud"]["default_capabilities"], ["inventory"])
        self.assertEqual(profiles["cloud"]["main_candidate"], "never")

    def test_a_hub_node_missing_from_the_config_is_flagged(self):
        Broker(self.state).enroll("ghost", ["inventory"], invite_ttl=3600)
        by_id = {d["id"]: d for d in fm.inventory(self.config, self.state)["devices"]}
        self.assertEqual(by_id["ghost"]["status"], "unconfigured")


class PreviewTests(FleetBase):
    def test_preview_changes_nothing_and_never_calls_invite(self):
        before = self.config_text()
        result = fm.preview_setup(self.config, self.state, {"node": "oracle1", "profile": "cloud", "os": "linux"})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config_text(), before)
        self.assertFalse((self.state / "cluster.sqlite3").exists())
        self.assertEqual(list(self.root.glob("*.pre-*")), [])
        self.assertFalse(result["mutated"])
        self.assertTrue(result["ready"])
        self.assertEqual([c["id"] for c in result["capabilities"]], ["inventory"])
        self.assertTrue(result["limits"])  # cloud: honest about no provisioning
        self.assertIn("jarvis cluster join", result["node_commands"][0]["command"])
        self.assertNotIn("jarvis-invite-1", json.dumps(result).replace("<INVITE CODE>", ""))

    def test_os_specific_commands(self):
        win = fm.preview_setup(self.config, self.state, {"node": "laptop", "os": "windows"}, probe=False)
        self.assertIn(".venv\\Scripts\\python.exe", win["node_commands"][0]["command"])
        mac = fm.preview_setup(self.config, self.state, {"node": "laptop", "os": "macos"}, probe=False)
        self.assertIn(".venv/bin/python", mac["node_commands"][0]["command"])
        none = fm.preview_setup(self.config, self.state, {"node": "laptop"}, probe=False)
        self.assertIsNone(none["node_commands"][0]["command"])

    def test_preview_reports_problems(self):
        Broker(self.state).enroll("dell", sorted(NODE_DEFAULT), invite_ttl=3600)
        self.set_row("dell", invite_expires=None, seen=time.time())
        result = fm.preview_setup(self.config, self.state, {"node": "dell"}, probe=False)
        self.assertFalse(result["ready"])
        self.assertEqual(result["problems"][0]["code"], "node_enrolled")
        self.assertEqual(result["link"], "online")

    def test_preview_flags_a_down_sshd_and_reverse_tunnel(self):
        with mock.patch.object(cluster_setup, "_sshd_up", lambda: False):
            result = fm.preview_setup(self.config, self.state, {"node": "oracle1", "profile": "cloud"})
        self.assertFalse(result["ready"])
        self.assertEqual([c["id"] for c in result["checks"] if c["state"] == "problem"], ["sshd"])
        reverse = config(tunnel="reverse")
        result = fm.preview_setup(reverse, self.state, {"node": "oracle1"}, probe=False)
        self.assertFalse(result["ready"])

    def test_preview_flags_extra_capabilities_needing_acknowledgement(self):
        result = fm.preview_setup(self.config, self.state, {"node": "oracle1", "profile": "cloud",
                                                             "capabilities": ["inventory", "camera"]}, probe=False)
        self.assertEqual(result["needs_acknowledge"], ["camera"])

    def test_malformed_preview_bodies(self):
        for body, code in ((None, "bad_body"), ([], "bad_body"), ("x", "bad_body"),
                           ({"node": "oracle1", "evil": 1}, "unknown_fields"),
                           ({}, "bad_node"), ({"node": ""}, "bad_node"), ({"node": 5}, "bad_node"),
                           ({"node": "../etc"}, "bad_node"), ({"node": "a b"}, "bad_node"),
                           ({"node": "x" * 65}, "bad_node"), ({"node": "main"}, "main_not_allowed"),
                           ({"node": "ok", "profile": "mainframe"}, "bad_profile"),
                           ({"node": "ok", "os": "beos"}, "bad_os"),
                           ({"node": "ok", "addresses": "1.2.3.4"}, "bad_address"),
                           ({"node": "ok", "addresses": ["127.0.0.1"]}, "bad_address"),
                           ({"node": "ok", "addresses": ["a b;rm -rf"]}, "bad_address"),
                           ({"node": "ok", "addresses": ["1.1.1.1"] * 5}, "bad_address"),
                           ({"node": "ok", "label": "x" * 61}, "bad_label"),
                           ({"node": "ok", "capabilities": []}, "bad_capabilities"),
                           ({"node": "ok", "capabilities": ["root"]}, "bad_capabilities")):
            with self.subTest(body=body):
                self.assertEqual(self.refused(fm.preview_setup, self.config, self.state, body, False).payload["code"], code)


class InviteTests(FleetBase):
    def test_cached_main_config_cannot_issue_after_role_changed_on_disk(self):
        updated = copy.deepcopy(self.config)
        updated["cluster"]["main_device"] = "dell"
        self.path.write_text(json.dumps(updated))
        error = self.refused(self.invite)
        self.assertEqual(error.payload["code"], "not_main")
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads(self.path.read_text()), updated)

    def test_success_backs_up_and_preserves_other_config(self):
        original = self.config_text()
        result = self.invite(addresses=["203.0.113.9"], label="Oracle box")
        self.assertEqual(result["code"], CODE)
        self.assertTrue(result["secret"])
        self.assertEqual([c["id"] for c in result["capabilities"]], ["inventory"])
        self.assertEqual(self.calls, [{"node": "oracle1", "extra": ["203.0.113.9"], "capabilities": ["inventory"],
                                       "devices": ["dell", "main", "oracle1"]}])
        written = json.loads(self.config_text())
        self.assertEqual(written["devices"]["oracle1"], {"label": "Oracle box", "profile": "cloud", "os": "linux"})
        self.assertEqual(written["devices"]["dell"], self.config["devices"]["dell"])
        self.assertEqual(written["keep"], HAND_TUNED["keep"])
        backups = list(self.root.glob("jarvis.config.json.pre-fleet-onboarding-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), oct(backups[0].stat().st_mode & 0o777))

    def test_secret_is_nowhere_but_the_reply(self):
        self.invite()
        for path in list(self.root.rglob("*")):
            if path.is_file() and path.suffix != ".sqlite3":
                self.assertNotIn("SECRET-CODE", path.read_text(errors="ignore"), path)
        self.assertNotIn("SECRET-CODE", json.dumps(fm.inventory(self.config, self.state)))
        self.assertNotIn("SECRET-CODE", json.dumps(fm.preview_setup(self.config, self.state, {"node": "oracle1"}, False)))

    def test_lost_reply_never_issues_a_second_code(self):
        self.invite()
        err = self.refused(self.invite)  # same request_id
        self.assertEqual((err.status, err.payload["code"]), (409, "already_issued"))
        self.assertNotIn("SECRET", json.dumps(err.payload))
        self.assertEqual(len(self.calls), 1)

    def test_a_request_id_cannot_be_reused_for_another_node(self):
        self.invite()
        err = self.refused(self.invite, ) if False else self.refused(
            lambda: fm.issue_invite(self.config, self.path, self.state,
                                    {"request_id": "req-0001", "node": "other2", "profile": "cloud"}))
        self.assertEqual(err.payload["code"], "already_issued")
        self.assertEqual(len(self.calls), 1)

    def test_pending_invite_needs_replace_pending_and_a_new_request(self):
        self.invite()
        err = self.refused(lambda: self.invite(request_id="req-0002"))
        self.assertEqual(err.payload["code"], "invite_pending")
        self.assertEqual(len(self.calls), 1)
        again = self.invite(request_id="req-0002", replace_pending=True)
        self.assertEqual(again["code"], CODE)
        self.assertEqual(len(self.calls), 2)

    def test_expired_invite_can_be_reissued_without_replace(self):
        self.invite()
        self.set_row("oracle1", invite_expires=time.time() - 1)
        self.assertEqual(self.invite(request_id="req-0003")["node"], "oracle1")

    def test_a_live_node_is_never_rotated(self):
        self.invite()
        self.set_row("oracle1", invite_expires=None, seen=time.time())
        import sqlite3
        db = sqlite3.connect(self.state / "cluster.sqlite3")
        before = db.execute("SELECT token_hash FROM nodes WHERE id='oracle1'").fetchone()
        for n, state in enumerate((time.time(), time.time() - 3600, None)):
            self.set_row("oracle1", seen=state)
            for replace in (False, True):
                err = self.refused(lambda: self.invite(request_id=f"r-{n}-{replace}-xx", replace_pending=replace))
                self.assertEqual(err.payload["code"], "node_enrolled")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(db.execute("SELECT token_hash FROM nodes WHERE id='oracle1'").fetchone(), before)
        db.close()

    def test_main_is_refused_in_every_spelling(self):
        for node in ("main",):
            self.assertEqual(self.refused(self.invite, ) if False else self.refused(
                lambda: self.invite(node=node)).payload["code"], "main_not_allowed")
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads(self.config_text()), self.config)

    def test_only_the_main_may_invite(self):
        cfg = config()
        cfg["cluster"]["local_device"] = "dell"
        err = self.refused(lambda: fm.issue_invite(cfg, self.path, self.state,
                                                   {"request_id": "req-0001", "node": "oracle1"}))
        self.assertEqual(err.payload["code"], "not_main")
        self.assertEqual(self.calls, [])
        off = config(enabled=False)
        self.assertEqual(self.refused(lambda: fm.issue_invite(off, self.path, self.state,
                         {"request_id": "req-0001", "node": "oracle1"})).payload["code"], "cluster_off")

    def test_reverse_tunnel_and_down_sshd_refuse_before_any_write(self):
        before = self.config_text()
        rev = config(tunnel="reverse")
        err = self.refused(lambda: fm.issue_invite(rev, self.path, self.state, {"request_id": "req-0001", "node": "x1"}))
        self.assertEqual(err.payload["code"], "tunnel_forward")
        with mock.patch.object(cluster_setup, "_sshd_up", lambda: False):
            err = self.refused(self.invite)
        self.assertEqual((err.payload["code"], err.payload["retryable"]), ("sshd", True))
        self.assertEqual(self.config_text(), before)
        self.assertEqual(list(self.root.glob("*.pre-*")), [])
        self.assertEqual(self.calls, [])

    def test_failed_invite_is_retryable_with_the_same_request(self):
        def boom(*a, **k):
            raise ValueError("No address found for this PC; pass --address IP_OR_NAME.")
        with mock.patch.object(cluster_setup, "invite", boom):
            err = self.refused(self.invite)
        self.assertEqual((err.payload["code"], err.payload["retryable"], err.payload["step"]),
                         ("invite_failed", True, "invite"))
        self.assertEqual(self.invite()["node"], "oracle1")  # same request_id: nothing was issued, so it works

    def test_extra_capabilities_need_explicit_acknowledgement(self):
        err = self.refused(lambda: self.invite(capabilities=["inventory", "camera", "locate"]))
        self.assertEqual((err.status, err.payload["code"]), (400, "capability_ack_required"))
        self.assertEqual(err.payload["needs_ack"], ["camera", "locate"])
        self.assertEqual(self.calls, [])
        ok = self.invite(capabilities=["inventory", "camera"], acknowledge=["camera"])
        self.assertEqual(sorted(c["id"] for c in ok["capabilities"]), ["camera", "inventory"])

    def test_pc_defaults_need_no_acknowledgement_and_cloud_never_gets_desktop_by_default(self):
        self.invite(node="laptop2", profile="pc", os="windows", request_id="req-pc01")
        self.assertEqual(self.calls[-1]["capabilities"], sorted(NODE_DEFAULT))
        self.invite(node="oracle1", request_id="req-cl01")
        self.assertEqual(self.calls[-1]["capabilities"], ["inventory"])

    def test_bad_request_ids_and_bodies(self):
        for body in ({"node": "x1"}, {"node": "x1", "request_id": 5}, {"node": "x1", "request_id": "short"},
                     {"node": "x1", "request_id": "has space 123"}, {"node": "x1", "request_id": "a" * 65},
                     {"node": "x1", "request_id": "req-0001", "replace_pending": "yes"},
                     {"node": "x1", "request_id": "req-0001", "force": True}):
            with self.subTest(body=body):
                self.assertEqual(self.refused(fm.issue_invite, self.config, self.path, self.state, body).status, 400)
        self.assertEqual(self.calls, [])

    def test_profile_mismatch_with_existing_device(self):
        self.invite()  # oracle1 is a cloud now
        self.set_row("oracle1", invite_expires=time.time() - 1)
        cfg = json.loads(self.config_text())
        err = self.refused(lambda: fm.issue_invite(cfg, self.path, self.state,
                           {"request_id": "req-0009", "node": "oracle1", "profile": "pc"}))
        self.assertEqual(err.payload["code"], "profile_mismatch")

    def test_unreadable_config_changes_nothing(self):
        self.path.write_text("{not json")
        err = self.refused(self.invite)
        self.assertEqual(err.payload["code"], "config_unreadable")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_text(), "{not json")

    def test_concurrent_same_request_id_issues_once(self):
        results = []
        def go():
            try:
                results.append(self.invite())
            except fm.FleetError as exc:
                results.append(exc)
        threads = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, fm.FleetError) and r.payload["code"] == "already_issued" for r in results), 5)

    def test_busy_lock_is_a_retryable_409(self):
        from jarvis.locking import exclusive_lock
        with exclusive_lock(self.state / "fleet-manage.lock"):
            with mock.patch("jarvis.locking.exclusive_lock", side_effect=lambda p, timeout=0: exclusive_lock(p, 0)):
                err = self.refused(self.invite)
        self.assertEqual((err.payload["code"], err.payload["retryable"]), ("busy", True))
        self.assertEqual(self.calls, [])

    def test_main_flipped_during_device_write_does_not_invite(self):
        real = fm._write_device

        def flip(cfg, file, node, entry, **kw):
            disk = json.loads(Path(file).read_text(encoding="utf-8"))
            disk["cluster"]["main_device"] = "dell"
            Path(file).write_text(json.dumps(disk), encoding="utf-8")
            return real(cfg, file, node, entry, **kw)

        with mock.patch.object(fm, "_write_device", side_effect=flip):
            err = self.refused(self.invite)
        self.assertEqual(err.payload["code"], "not_main")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.state / "cluster.sqlite3").exists())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["cluster"]["main_device"], "dell")

    def test_tunnel_flipped_during_device_write_does_not_invite(self):
        real = fm._write_device

        def flip(cfg, file, node, entry, **kw):
            disk = json.loads(Path(file).read_text(encoding="utf-8"))
            disk["cluster"]["tunnel"] = "reverse"
            Path(file).write_text(json.dumps(disk), encoding="utf-8")
            return real(cfg, file, node, entry, **kw)

        with mock.patch.object(fm, "_write_device", side_effect=flip):
            err = self.refused(self.invite)
        self.assertEqual(err.payload["code"], "tunnel_forward")
        self.assertEqual(self.calls, [])

    def test_restart_cannot_reissue_the_same_request_even_with_replace_pending(self):
        self.invite()
        err = self.refused(lambda: self.invite(replace_pending=True))
        self.assertEqual(err.payload["code"], "already_issued")
        self.assertEqual(len(self.calls), 1)
        receipt = (self.state / "cluster-invite-receipts.json").read_text(encoding="utf-8")
        self.assertIn("req-0001", receipt)
        self.assertIn("oracle1", receipt)
        self.assertNotIn("SECRET-CODE", receipt)
        self.assertNotIn("jarvis-invite-1:", receipt)
        self.assertNotIn("PRIVATE", receipt)
        self.invite(request_id="req-0099", replace_pending=True)
        self.assertEqual(len(self.calls), 2)

    def test_pending_receipt_with_a_row_is_not_rotated(self):
        self.invite()
        path = self.state / "cluster-invite-receipts.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["receipts"]["req-0001"] = {"node": "oracle1", "state": "pending", "at": 1}
        path.write_text(json.dumps(data), encoding="utf-8")
        err = self.refused(lambda: self.invite(replace_pending=True))
        self.assertEqual(err.payload["code"], "already_issued")
        self.assertEqual(len(self.calls), 1)

    def test_pending_receipt_without_a_credential_can_retry(self):
        path = self.state / "cluster-invite-receipts.json"
        path.write_text(json.dumps({"version": 1, "receipts": {
            "req-0001": {"node": "oracle1", "state": "pending", "at": 1}}}), encoding="utf-8")
        self.assertEqual(self.invite()["node"], "oracle1")
        self.assertEqual(len(self.calls), 1)

    def test_damaged_receipt_file_is_not_treated_as_empty(self):
        (self.state / "cluster-invite-receipts.json").write_text("{", encoding="utf-8")
        err = self.refused(self.invite)
        self.assertEqual((err.payload["code"], err.payload["retryable"]), ("invite_failed", False))
        self.assertEqual(self.calls, [])

    def test_uncertain_cleanup_is_not_retried_and_does_not_claim_success(self):
        def boom(cfg, path, state, node, extra=(), capabilities=None, output=None):
            Broker(state).enroll(node, ["inventory"], invite_ttl=cluster_setup.INVITE_TTL)
            raise cluster_setup.InviteIncomplete(
                "The invite stopped after a credential change and the cleanup could not be verified. "
                "It was not atomic. Retrying this request would not be safe.",
                retryable=False)
        with mock.patch.object(cluster_setup, "invite", side_effect=boom):
            err = self.refused(self.invite)
        self.assertEqual((err.payload["code"], err.payload["retryable"]), ("invite_failed", False))
        self.assertIn("not atomic", err.payload["error"])
        self.assertNotIn("jarvis-invite-1:", err.payload["error"])
        with mock.patch.object(cluster_setup, "invite", side_effect=boom) as again:
            err = self.refused(lambda: self.invite(replace_pending=True))
        self.assertFalse(err.payload["retryable"])
        again.assert_not_called()

    def test_cloud_why_is_inventory_not_a_gateway(self):
        cfg = config()
        cfg["devices"]["oracle1"] = {"label": "Oracle", "profile": "cloud"}
        view = fm.inventory(cfg, self.state)
        cloud = next(d for d in view["devices"] if d["id"] == "oracle1")
        about = next(p["about"] for p in view["profiles"] if p["id"] == "cloud")
        self.assertNotIn("gateway", cloud["main_eligible_why"].lower())
        self.assertIs(cloud["main_eligible"], False)
        self.assertIn("does not install a gateway", about)


class RevokeAndCapabilityTests(FleetBase):
    def test_owner_mutations_revalidate_current_main_from_disk(self):
        fresh = copy.deepcopy(self.config)
        fresh["cluster"]["main_device"] = "dell"
        self.path.write_text(json.dumps(fresh))
        for fn, body in ((fm.revoke_node, {"node": "dell", "confirm": "dell"}),
                         (fm.set_node_capabilities, {"node": "dell", "capabilities": ["inventory"]})):
            with self.subTest(action=fn.__name__), self.assertRaises(fm.FleetError) as caught:
                fn(self.config, self.state, body, config_path=self.path)
            self.assertEqual(caught.exception.payload["code"], "not_main")
        self.assertEqual(self.revoked, [])

    def enrolled(self):
        self.invite()
        self.set_row("oracle1", invite_expires=None, seen=time.time())
        self.config = json.loads(self.config_text())

    def test_revoke_needs_confirm_and_never_targets_main(self):
        self.enrolled()
        for body, code in (({"node": "oracle1"}, "confirm_required"), ({"node": "oracle1", "confirm": "nope"}, "confirm_required"),
                           ({"node": "main", "confirm": "main"}, "main_not_allowed"), ({"node": "../x", "confirm": "../x"}, "bad_node"),
                           ({"node": "oracle1", "confirm": "oracle1", "extra": 1}, "unknown_fields")):
            self.assertEqual(self.refused(fm.revoke_node, self.config, self.state, body).payload["code"], code)
        self.assertEqual(self.revoked, [])
        self.assertEqual(Broker(self.state).nodes()[0]["id"], "oracle1")

    def test_revoke_removes_credential_and_key_and_frees_the_request_id(self):
        self.enrolled()
        result = fm.revoke_node(self.config, self.state, {"node": "oracle1", "confirm": "oracle1"})
        self.assertEqual((result["ok"], result["was_enrolled"], result["ssh_key_removed"]), (True, True, True))
        self.assertEqual(self.revoked, ["oracle1"])
        self.assertEqual(Broker(self.state).nodes(), [])
        self.assertEqual(self.invite(request_id="req-0007")["node"], "oracle1")  # re-pairing is a fresh decision
        # Revoke drops that node's receipt, so the original request id is a new decision too.
        self.assertEqual(self.invite(request_id="req-0001", replace_pending=True)["node"], "oracle1")

    def test_revoke_of_a_node_that_isnt_enrolled_is_harmless(self):
        result = fm.revoke_node(self.config, self.state, {"node": "dell", "confirm": "dell"})
        self.assertFalse(result["was_enrolled"])

    def test_revoke_off_main_refused(self):
        cfg = config()
        cfg["cluster"]["local_device"] = "dell"
        self.assertEqual(self.refused(fm.revoke_node, cfg, self.state, {"node": "oracle1", "confirm": "oracle1"}).payload["code"], "not_main")

    def test_capability_change_keeps_the_credential_and_needs_ack(self):
        self.enrolled()
        import sqlite3
        db = sqlite3.connect(self.state / "cluster.sqlite3")
        before = db.execute("SELECT token_hash FROM nodes WHERE id='oracle1'").fetchone()
        err = self.refused(fm.set_node_capabilities, self.config, self.state,
                           {"node": "oracle1", "capabilities": ["inventory", "desktop"]})
        self.assertEqual(err.payload["code"], "capability_ack_required")
        result = fm.set_node_capabilities(self.config, self.state, {"node": "oracle1",
                                          "capabilities": ["inventory", "desktop"], "acknowledge": ["desktop"]})
        self.assertEqual((result["changed"], result["added"], result["credential_rotated"]), (True, ["desktop"], False))
        self.assertEqual(db.execute("SELECT token_hash FROM nodes WHERE id='oracle1'").fetchone(), before)
        db.close()
        self.assertEqual(sorted(Broker(self.state).nodes()[0]["capabilities"]), ["desktop", "inventory"])
        again = fm.set_node_capabilities(self.config, self.state, {"node": "oracle1",
                                         "capabilities": ["inventory", "desktop"], "acknowledge": ["desktop"]})
        self.assertFalse(again["changed"])

    def test_capability_errors(self):
        for body, code, status in (({"node": "main", "capabilities": ["inventory"]}, "main_not_allowed", 409),
                                   ({"node": "ghost", "capabilities": ["inventory"]}, "unknown_node", 404),
                                   ({"node": "dell", "capabilities": ["inventory"]}, "not_enrolled", 404),
                                   ({"node": "dell", "capabilities": ["sudo"]}, "bad_capabilities", 400),
                                   ({"node": "dell", "capabilities": []}, "bad_capabilities", 400)):
            err = self.refused(fm.set_node_capabilities, self.config, self.state, body)
            self.assertEqual((err.payload["code"], err.status), (code, status), body)


class MainTransferTests(FleetBase):
    def test_always_blocked_and_never_rewrites_the_config(self):
        before = self.config_text()
        for target in ("dell", "main", "oracle1"):
            result = fm.main_transfer(self.config, self.state, {"target": target})
            self.assertEqual((result["status"], result["support"], result["mutated"]), ("blocked", "blocked", False))
            self.assertEqual({m["id"] for m in result["missing"]}, {i for i, _ in fm.MAIN_TRANSFER_MISSING})
            self.assertTrue(result["phases"])
        err = self.refused(fm.main_transfer_refuse, self.config, self.state, {"target": "dell"})
        self.assertEqual((err.status, err.payload["code"], err.payload["mutated"]), (409, "blocked", False))
        self.assertEqual(self.config_text(), before)
        self.assertEqual(self.config["cluster"]["main_device"], "main")

    def test_cloud_target_readiness_says_never(self):
        cfg = config()
        cfg["devices"]["oracle1"] = {"profile": "cloud"}
        ready = {c["id"]: c["state"] for c in fm.main_transfer(cfg, self.state, {"target": "oracle1"})["readiness"]}
        self.assertEqual(ready["not_cloud"], "problem")
        self.assertEqual(ready["known_device"], "ok")

    def test_bad_target(self):
        self.assertEqual(self.refused(fm.main_transfer, self.config, self.state, {"target": 3}).status, 400)
        self.assertEqual(self.refused(fm.main_transfer, self.config, self.state, {}).status, 400)


class VaultStatusTests(FleetBase):
    def git(self, *args, cwd=None):
        subprocess.run(["git", "-C", str(cwd or self.vault), *args], check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(self.root)})

    def setUp(self):
        super().setUp()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.vault / "a-private-note-name.md").write_text("PRIVATE BODY")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "x")
        self.git("remote", "add", "origin", "https://user:ghp_SECRETTOKEN@github.com/owner/obsidian-vault.git")
        self.cfg = {**config(), "vault": {"enabled": True, "path": str(self.vault), "write": False}}

    def test_status_is_numeric_scrubbed_and_offline(self):
        (self.vault / "untracked-secret-file.md").write_text("more")
        (self.vault / "a-private-note-name.md").write_text("changed")
        out = fm.vault_status(self.cfg, self.state)
        text = json.dumps(out)
        for leaked in ("ghp_SECRETTOKEN", "user:", "private-note", "untracked-secret", "PRIVATE BODY", str(self.vault)):
            self.assertNotIn(leaked, text)
        git = out["git"]
        self.assertEqual(git["branch"], "main")
        self.assertEqual(git["remote"], {"name": "origin", "provider": "github", "host": "github.com",
                                         "repo": "owner/obsidian-vault", "credentials_embedded": True})
        self.assertEqual((git["dirty"]["modified"], git["dirty"]["untracked"], git["clean"]), (1, 1, False))
        self.assertFalse(out["network"])
        self.assertEqual(out["visibility"]["value"], "unknown")
        self.assertTrue(any("credential" in p for p in out["problems"]))
        self.assertFalse(out["vault"]["write_enabled"])

    def test_ssh_remote_and_clean_repo(self):
        self.git("remote", "set-url", "origin", "git@github.com:owner/notes-vault.git")
        out = fm.vault_status(self.cfg, self.state)
        self.assertEqual(out["git"]["remote"]["repo"], "owner/notes-vault")
        self.assertFalse(out["git"]["remote"]["credentials_embedded"])
        self.assertTrue(out["git"]["clean"])
        self.assertTrue(out["git"]["connected"])
        self.assertFalse(out["git"]["upstream_set"])

    def test_cache_and_no_vault_cases(self):
        fm.vault_status(self.cfg, self.state, now=1000)
        with mock.patch.object(fm, "_git", side_effect=AssertionError("cached")):
            fm.vault_status(self.cfg, self.state, now=1005)
        self.assertFalse(fm.vault_status({}, self.state)["vault"]["configured"])
        gone = {**self.cfg, "vault": {"enabled": True, "path": str(self.root / "missing")}}
        self.assertFalse(fm.vault_status(gone, self.state)["vault"]["exists"])
        plain = self.root / "plain"
        plain.mkdir()
        self.assertFalse(fm.vault_status({**self.cfg, "vault": {"enabled": True, "path": str(plain)}}, self.state)["vault"]["is_git"])
        off = {**self.cfg, "vault": {"enabled": False, "path": str(self.vault)}}
        self.assertFalse(fm.vault_status(off, self.state)["vault"]["configured"])

    def test_git_calls_are_fixed_argument_lists_with_timeout(self):
        seen = []
        real = subprocess.run
        def spy(cmd, **kw):
            seen.append((cmd, kw))
            return real(cmd, **kw)
        with mock.patch.object(fm.subprocess, "run", spy):
            fm.vault_status(self.cfg, self.state)
        self.assertTrue(seen)
        for cmd, kw in seen:
            self.assertIsInstance(cmd, list)
            self.assertNotIn(kw.get("shell"), (True,))
            self.assertLessEqual(kw["timeout"], 8)
            self.assertNotIn("fetch", cmd)
            self.assertNotIn("push", cmd)
            self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_scrub_remote_shapes(self):
        cases = {"https://x-access-token:abc@github.com/o/r.git": ("github", "o/r", True),
                 "https://abc@github.com/o/r": ("github", "o/r", True),
                 "https://github.com/o/r": ("github", "o/r", False),
                 "ssh://git@github.com/o/r.git": ("github", "o/r", False),
                 "git@gitlab.com:o/r.git": ("other", None, False),
                 "": (None, None, False), "garbage": (None, None, False)}
        for url, (provider, repo, embedded) in cases.items():
            got = fm.scrub_remote(url)
            self.assertEqual((got["provider"], got["repo"], got["credentials_embedded"]), (provider, repo, embedded), url)
            self.assertNotIn("abc", json.dumps(got))

    def test_archive_block_is_allowlisted_numbers(self):
        fake = {"enabled": True, "state": "current", "archive_pending": 0, "capture_complete": True,
                "last_error": "PRIVATE NOTE NAME.md failed", "secret_path": "/home/x/vault/NOTE.md", "queue": {"pending": 0}}
        with mock.patch("jarvis.second_brain_runtime.status", return_value=fake):
            out = fm.vault_status(self.cfg, self.state)["archive"]
        self.assertNotIn("PRIVATE", json.dumps(out))
        self.assertNotIn("secret_path", out)
        self.assertTrue(out["has_error"])
        self.assertEqual(out["state"], "current")


class ConsoleHTTPTests(FleetBase):
    """The real Handler + Console: Host/Sec-Fetch/X-JARVIS gates, status codes, no-store."""

    def setUp(self):
        super().setUp()
        self.console = Console(self.config, self.root, self.state)
        self.addCleanup(self.console.close)
        self.events = []
        real = self.console.bus.publish
        self.console.bus.publish = lambda kind, data=None: self.events.append((kind, data)) or real(kind, data)
        handler = type("H", (Handler,), {"console": self.console, "hosts": ("127.0.0.1", "localhost", "::1")})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def call(self, method, path, body=None, headers=None, host="default", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host == "default":
                conn.putheader("Host", f"127.0.0.1:{self.port}")
            elif host is not None:
                conn.putheader("Host", host)
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
            sent = {"X-JARVIS": "1"} if method == "POST" else {}
            sent.update(headers or {})
            for k, v in sent.items():
                if v is not None:
                    conn.putheader(k, v)
            if method == "POST":
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            res = conn.getresponse()
            payload = res.read()
            try:
                return res.status, json.loads(payload), res
            except ValueError:
                return res.status, payload, res
        finally:
            conn.close()

    def test_reads(self):
        status, body, res = self.call("GET", "/api/fleet")
        self.assertEqual(status, 200)
        self.assertEqual([d["id"] for d in body["devices"]], ["main", "dell"])
        self.assertEqual(self.call("GET", "/api/fleet/vault")[0], 200)
        self.assertEqual(self.call("GET", "/api/fleet", host="evil.example")[0], 403)

    def test_invite_over_http_is_no_store_and_publishes_no_secret(self):
        status, body, res = self.call("POST", "/api/fleet/invite", {"request_id": "http-0001", "node": "oracle1",
                                                                     "profile": "cloud", "os": "linux"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], CODE)
        self.assertIn("no-store", res.getheader("Cache-Control"))
        self.assertEqual(res.getheader("Pragma"), "no-cache")
        self.assertNotIn("SECRET-CODE", json.dumps(self.events))
        self.assertIn(("fleet.changed", {"action": "invite", "node": "oracle1"}), self.events)
        self.assertEqual(self.console.config["devices"]["oracle1"]["profile"], "cloud")  # live config follows the file
        status, body, res = self.call("POST", "/api/fleet/invite", {"request_id": "http-0001", "node": "oracle1",
                                                                     "profile": "cloud"})
        self.assertEqual((status, body["code"]), (409, "already_issued"))
        self.assertIn("no-store", res.getheader("Cache-Control"))
        self.assertEqual(len(self.calls), 1)
        status, body, _ = self.call("GET", "/api/fleet")
        self.assertNotIn("SECRET-CODE", json.dumps(body))
        self.assertEqual({d["id"]: d["status"] for d in body["devices"]}["oracle1"], "invite_pending")

    def test_post_gates(self):
        good = {"request_id": "http-0002", "node": "oracle1"}
        self.assertEqual(self.call("POST", "/api/fleet/invite", good, headers={"X-JARVIS": None})[0], 403)
        self.assertEqual(self.call("POST", "/api/fleet/invite", good, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.call("POST", "/api/fleet/invite", good, host="evil.example")[0], 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config_text(), json.dumps(config(), indent=2))

    def test_malformed_bodies_and_path_variants(self):
        for raw in (b"[]", b"null", b"not json", b'"x"', b"123"):
            status, body, _ = self.call("POST", "/api/fleet/invite", raw=raw)
            self.assertEqual(status, 400, raw)
        status, body, _ = self.call("POST", "/api/fleet/invite", {"node": "x1", "request_id": "http-0003", "evil": 1})
        self.assertEqual((status, body["code"]), (400, "unknown_fields"))
        for path in ("/api/fleet/invite/", "/api/fleet/invite/../revoke", "/api/FLEET/invite",                      "/api/fleet/%69nvite", "/api/fleet/other"):
            status, _, _ = self.call("POST", path, {"request_id": "http-0004", "node": "x1"})
            self.assertIn(status, (404, 400, 403), path)
        self.assertEqual(self.calls, [])

    def test_main_and_error_codes_over_http(self):
        status, body, _ = self.call("POST", "/api/fleet/invite", {"request_id": "http-0005", "node": "main"})
        self.assertEqual((status, body["code"]), (409, "main_not_allowed"))
        status, body, _ = self.call("POST", "/api/fleet/revoke", {"node": "dell"})
        self.assertEqual((status, body["code"]), (400, "confirm_required"))
        status, body, _ = self.call("POST", "/api/fleet/capabilities", {"node": "ghost", "capabilities": ["inventory"]})
        self.assertEqual((status, body["code"]), (404, "unknown_node"))

    def test_transfer_routes_are_preview_or_409(self):
        status, body, _ = self.call("POST", "/api/fleet/main-transfer/preview", {"target": "dell"})
        self.assertEqual((status, body["status"], body["mutated"]), (200, "blocked", False))
        status, body, res = self.call("POST", "/api/fleet/main-transfer", {"target": "dell"})
        self.assertEqual((status, body["code"], body["mutated"]), (409, "blocked", False))
        self.assertTrue(body["missing"])
        self.assertEqual(self.console.config["cluster"]["main_device"], "main")
        self.assertEqual(self.config_text(), json.dumps(config(), indent=2))

    def test_preview_over_http_changes_nothing(self):
        status, body, _ = self.call("POST", "/api/fleet/preview", {"node": "oracle1", "profile": "cloud", "os": "linux"})
        self.assertEqual((status, body["mutated"]), (200, False))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config_text(), json.dumps(config(), indent=2))

    def test_unexpected_exceptions_do_not_leak(self):
        with mock.patch.object(fm, "inventory", side_effect=RuntimeError("boom /home/jdoe/secret")):
            status, body, _ = self.call("GET", "/api/fleet")
        self.assertEqual(status, 500)


class HubAuthTests(FleetBase):
    """Over the real hub: a node token (even with every capability) is refused every fleet route."""

    def setUp(self):
        super().setUp()
        self.console = Console(self.config, self.root, self.state)
        self.addCleanup(self.console.close)
        self.hub = Hub(self.config, self.root, self.state, console=self.console).start(port=0)
        self.addCleanup(self.hub.close)
        self.port = self.hub.server.server_address[1]
        self.token = self.hub.broker.enroll("dell", CAPABILITIES)

    def call(self, method, path, token, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Authorization": f"Bearer {token}", "X-JARVIS": "1", "X-JARVIS-Main": "main"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            res = conn.getresponse()
            return res.status, res.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def test_node_tokens_are_denied_in_every_spelling(self):
        for path in ("/api/fleet", "/api/fleet/", "/api/fleet?x=1", "/api/fleet;x=1", "/api/fleet/vault",
                     "/api/FLEET", "/api/fleet/../fleet", "/api/fleet/%2e%2e/fleet"):
            status, body = self.call("GET", path, self.token)
            self.assertIn(status, (403, 404), path)
            self.assertNotIn("devices", body)
        for path in ("/api/fleet/invite", "/api/fleet/revoke", "/api/fleet/capabilities", "/api/fleet/preview",
                     "/api/fleet/main-transfer", "/api/fleet/main-transfer/preview"):
            status, body = self.call("POST", path, self.token, {"node": "x1", "request_id": "node-0001"})
            self.assertEqual(status, 403, path)
        self.assertEqual(self.calls, [])

    def test_owner_token_reaches_the_routes(self):
        status, body = self.call("GET", "/api/fleet", self.hub.owner)
        self.assertEqual(status, 200)
        self.assertIn("devices", json.loads(body))


class SigningKeyAndRoleLockTests(unittest.TestCase):
    """Real invite() against temp files: a signing-key failure must not be a bare 500,
    and set_main shares the fleet lock when it is given a state folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / ".jarvis"
        self.state.mkdir()
        self.path = self.root / "jarvis.config.json"
        self.config = config()
        self.path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        self.keys = self.root / "authorized_keys"
        self.keys.write_text("ssh-ed25519 AAAAexisting someone@else\n", encoding="utf-8")

    def _issue(self):
        return fm.issue_invite(self.config, self.path, self.state,
                               {"request_id": "partial-0001", "node": "oracle1", "profile": "cloud"})

    def test_code_key_failure_is_retryable_and_leaves_no_credential(self):
        order = []

        def code_key(state):
            order.append("code_key")
            raise RuntimeError("Couldn't make the code-signing key: timeout")

        def authorize(*args, **kwargs):
            order.append("authorize")
            raise AssertionError("authorize")

        with mock.patch.object(cluster_setup, "_sshd_up", return_value=True), \
                mock.patch.object(cluster_setup, "addresses", return_value=["203.0.113.10"]), \
                mock.patch.object(cluster_setup, "host_key", return_value="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R"), \
                mock.patch.object(cluster_setup, "_authorized_keys", return_value=self.keys), \
                mock.patch.object(cluster_setup, "code_key", side_effect=code_key), \
                mock.patch.object(cluster_setup, "authorize", side_effect=authorize):
            err = None
            try:
                self._issue()
            except fm.FleetError as exc:
                err = exc
            self.assertIsNotNone(err)
            self.assertEqual((err.status, err.payload["code"], err.payload["retryable"]),
                             (409, "invite_failed", True))
            self.assertNotIn("BEGIN OPENSSH", err.payload["error"])
            try:
                self._issue()
            except fm.FleetError as exc:
                again = exc
            self.assertEqual((again.payload["code"], again.payload["retryable"]), ("invite_failed", True))
        self.assertEqual(order, ["code_key", "code_key"])
        self.assertEqual(self.keys.read_text(encoding="utf-8"), "ssh-ed25519 AAAAexisting someone@else\n")
        self.assertFalse((self.state / "cluster.sqlite3").exists())

    def test_code_key_failure_over_http_is_invite_failed(self):
        from jarvis.ui import Console, Handler
        console = Console(self.config, self.root, self.state)
        self.addCleanup(console.close)
        handler = type("H", (Handler,), {"console": console, "hosts": ("127.0.0.1", "localhost", "::1")})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))

        def code_key(state):
            raise RuntimeError("Couldn't make the code-signing key: timeout BEGIN OPENSSH PRIVATE KEY")

        with mock.patch.object(cluster_setup, "_sshd_up", return_value=True), \
                mock.patch.object(cluster_setup, "addresses", return_value=["203.0.113.10"]), \
                mock.patch.object(cluster_setup, "code_key", side_effect=code_key):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            body = json.dumps({"request_id": "partial-0001", "node": "oracle1", "profile": "cloud"}).encode()
            conn.putrequest("POST", "/api/fleet/invite", skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", f"127.0.0.1:{port}")
            conn.putheader("X-JARVIS", "1")
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            res = conn.getresponse()
            payload = json.loads(res.read().decode())
            conn.close()
        self.assertEqual(res.status, 409)
        self.assertEqual(payload["code"], "invite_failed")
        self.assertTrue(payload["retryable"])
        self.assertNotIn("PRIVATE KEY", payload["error"])
        self.assertNotIn("BEGIN OPENSSH", json.dumps(payload))

    def test_set_main_with_state_waits_on_the_fleet_lock_and_keeps_new_devices(self):
        from jarvis.cluster_cli import set_main
        from jarvis.locking import exclusive_lock
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        disk["devices"]["oracle1"] = {"label": "Oracle", "profile": "cloud"}
        self.path.write_text(json.dumps(disk), encoding="utf-8")
        stale = config()  # no oracle1
        with exclusive_lock(self.state / "fleet-manage.lock"):
            with mock.patch("jarvis.locking.exclusive_lock", side_effect=lambda p, timeout=0: exclusive_lock(p, 0)):
                with self.assertRaises(ConfigBusy) as caught:
                    set_main(stale, self.path, "dell", state=self.state)
        self.assertEqual(caught.exception.code, "busy")
        saved = set_main(stale, self.path, "dell", state=self.state)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["cluster"]["main_device"], "dell")
        self.assertEqual(written["devices"]["oracle1"]["profile"], "cloud")
        self.assertEqual(saved["devices"]["dell"]["role"], "main")


if __name__ == "__main__":
    unittest.main()
