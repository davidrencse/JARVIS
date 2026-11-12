"""Safe MAIN handoff on the CLI, Settings, and Fleet surfaces.

Each success goes through cluster_cli.set_main and a temp config. The handoff callback
is the one configure_production stores on the in-process binding. Nothing here writes
the checkout config, starts a service, or spends a model.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from jarvis import cluster_cli, fleet_management as fm, settings
from jarvis.config_lock import held, patch_config
from jarvis.continuity.integration import (
    IntegrationError, clear_production, close_unfenced, configure_production, open_unfenced,
)


BASE = {
    "cluster": {"enabled": True, "main_device": "zenbook", "local_device": "zenbook", "tunnel": "forward"},
    "devices": {"zenbook": {"label": "Zenbook"}, "dell": {"label": "Dell"}},
    "keep": {"me": [1, 2, 3]},
}


class _Runtime:
    def require_authority(self, kind, target=None):
        return {"ok": True, "kind": kind}

    def release_before_suspend(self):
        return {"released": True}


class _Authority:
    """Status the coordinator reads. `before` is the live lease; `after` is the grant.

    This is the adapter fixture. The network test below uses a real gateway client
    instead of this object and does not claim a brain-sync handoff.
    """

    def __init__(self):
        self.phase = "before"
        self.old = "zenbook"
        self.target = "dell"
        self.epoch = 4
        self.transition_id = "t-1"
        self.action = "set_preferred_main"
        self.lease_valid = True
        self.barrier = 0
        self.ready = True
        self.mode = "V2_ACTIVE"
        self.incarnation = 1
        self.readiness = {"control_ready": True, "ai_ready": False}
        self.main_after = None
        self.active = None
        self.force_active = False
        self.fail_status = 0

    def status(self):
        if self.fail_status:
            self.fail_status -= 1
            raise RuntimeError("status down")
        if self.phase == "before":
            body = {"mode": self.mode, "lease_valid": self.lease_valid,
                    "main_ready": self.ready if self.lease_valid else False,
                    "main": self.old if self.lease_valid else None, "epoch": self.epoch,
                    "incarnation": self.incarnation, "grant_barrier": self.barrier,
                    "transition": self.active}
        else:
            main = self.main_after or self.target
            body = {"mode": "V2_ACTIVE", "lease_valid": True, "main_ready": True, "main": main,
                    "epoch": self.epoch, "incarnation": self.incarnation, "grant_barrier": 0,
                    "transition": {"state": "SUCCEEDED", "candidate": main,
                                   "transition_id": self.transition_id, "action": self.action}}
        if self.force_active:
            body["mode"] = self.mode
            body["lease_valid"] = self.lease_valid
            body["main_ready"] = self.ready if self.lease_valid else False
            body["main"] = self.old if self.lease_valid else None
            body["transition"] = self.active
            body["grant_barrier"] = self.barrier
        return body

    def advance(self, result, new):
        self.phase = "after"
        self.target = new
        if not isinstance(result, dict):
            return
        if isinstance(result.get("epoch"), int) and not isinstance(result.get("epoch"), bool):
            self.epoch = result["epoch"]
        if isinstance(result.get("transition_id"), str):
            self.transition_id = result["transition_id"]
        if isinstance(result.get("action"), str):
            self.action = result["action"]
        if isinstance(result.get("main"), str):
            self.main_after = result["main"]


class _Adapter:
    def __init__(self, fn, authority):
        self.fn = fn
        self.authority = authority

    def authority_status(self):
        return self.authority.status()

    def candidate_readiness(self, device):
        report = self.authority.readiness
        return report(device) if callable(report) else report

    def __call__(self, old, new, config, base):
        result = self.fn(old, new, config, base)
        self.authority.advance(result, new)
        return result


class HandoffSurfaces(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.path = self.tmp / "jarvis.config.json"
        self.calls = []
        self.addCleanup(self._cleanup)
        self.write_disk(self.with_relay())

    def _cleanup(self):
        open_unfenced()
        clear_production(self.state)
        settings._SAVED_CLUSTER.pop(str(self.state), None)

    def with_relay(self):
        doc = json.loads(json.dumps(BASE))
        doc["devices"]["relay"] = {"label": "Relay"}
        return doc

    def stale(self):
        """Caller memory that must not be written back over the disk."""
        doc = json.loads(json.dumps(BASE))
        doc["cluster"]["tunnel"] = "reverse"
        return doc

    def write_disk(self, doc):
        self.path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def fresh_role(self):
        """Put the role back so the next surface starts its own handoff.

        A published receipt would otherwise finish the config write without calling
        the callback. These legs are separate handoffs, not a publication retry.
        """
        self.write_disk(self.with_relay())
        self.calls.clear()
        intent = self.state / "main-handoff.json"
        if intent.is_file():
            intent.unlink()
        if getattr(self, "auth", None) is not None:
            self.auth.phase = "before"
            self.auth.main_after = None

    def disk(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def backups(self):
        return list(self.tmp.glob("jarvis.config.json.pre-cluster-main-*"))

    def bind(self, fn):
        self.auth = _Authority()
        configure_production(self.state, _Runtime(), handoff=_Adapter(fn, self.auth))

    def intent(self):
        return json.loads((self.state / "main-handoff.json").read_text(encoding="utf-8"))

    def handoff(self, old, new, config, base, *, status="ready", ok=True, main=None, add=None, action=None):
        recorded = {"old": old, "new": new, "config": config, "base": base, "held": held(self.state)}
        path = self.state / "main-handoff.json"
        recorded["intent"] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        self.calls.append(recorded)
        if add:
            def change(doc):
                doc.setdefault("devices", {})[add] = {"label": add}
            patch_config(self.state, self.path, change)
        body = {"ok": ok, "status": status, "transition_id": "t-1", "main": new if main is None else main,
                "epoch": 4, "lease_id": "ls-must-not-be-stored"}
        if action:
            body["action"] = action
        return body

    def accept(self, old, new, config, base):
        return self.handoff(old, new, config, base, add="pixel")

    def test_unconfigured_set_main_does_not_rewrite_the_role(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertIn("Moving MAIN is unavailable", str(caught.exception))
        self.assertIn("safe handoff", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.backups(), [])

    def test_marker_without_a_bound_runtime_refuses(self):
        self.bind(self.accept)
        clear_production(self.state)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertIn("bound managed runtime", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_success_on_cli_settings_and_fleet(self):
        self.bind(self.accept)
        stale = self.stale()
        out = cluster_cli.set_main(stale, self.path, "dell", state=self.state)
        disk = self.disk()
        self.assertEqual(disk["cluster"]["main_device"], "dell")
        self.assertEqual(disk["cluster"]["tunnel"], "forward")
        self.assertEqual(disk["devices"]["relay"]["label"], "Relay")
        self.assertEqual(disk["devices"]["pixel"]["label"], "pixel")
        self.assertEqual(disk["keep"], {"me": [1, 2, 3]})
        self.assertNotIn("relay", stale["devices"])
        self.assertEqual(stale["cluster"]["tunnel"], "reverse")
        self.assertTrue(out["ok"] and out["mutated"])
        self.assertEqual((out["status"], out["main"], out["epoch"], out["transition_id"]), ("ready", "dell", 4, "t-1"))
        self.assertEqual(out["devices"]["dell"]["role"], "main")
        self.assertNotIn("next", out)
        self.assertEqual(len(self.backups()), 1)
        saved = json.loads(self.backups()[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["cluster"]["main_device"], "zenbook")
        self.assertIn("pixel", saved["devices"])
        self.assertIn("relay", saved["devices"])
        self.assertEqual(self.calls[0]["old"], "zenbook")
        self.assertEqual(self.calls[0]["base"], self.tmp)
        self.assertTrue(self.calls[0]["held"])
        self.assertIn("relay", self.calls[0]["config"]["devices"])
        self.assertNotIn("pixel", self.calls[0]["config"]["devices"])
        self.assertEqual(self.calls[0]["intent"]["publication"], "outcome_unknown")
        self.assertIs(self.calls[0]["intent"]["reconcile_required"], True)
        self.assertEqual((self.calls[0]["intent"]["target"], self.calls[0]["intent"]["old"]), ("dell", "zenbook"))
        self.assertIsNone(self.calls[0]["intent"]["transition_id"])
        stored = self.intent()
        self.assertEqual(stored["publication"], "published")
        self.assertEqual(stored["transition_id"], "t-1")
        self.assertNotIn("lease_id", json.dumps(stored))

        self.fresh_role()
        memory = self.stale()
        settings._SAVED_CLUSTER.pop(str(self.state), None)
        saved_settings = settings.write(memory, self.path, self.state, {"cluster.main_device": "dell"})
        disk = self.disk()
        self.assertEqual(disk["cluster"]["main_device"], "dell")
        self.assertEqual(disk["devices"]["relay"]["label"], "Relay")
        self.assertEqual(disk["devices"]["pixel"]["label"], "pixel")
        self.assertEqual(disk["keep"]["me"], [1, 2, 3])
        self.assertEqual(disk["cluster"]["tunnel"], "forward")
        self.assertEqual(memory["cluster"]["main_device"], "zenbook")
        self.assertEqual(saved_settings["saved"], ["cluster.main_device"])
        self.assertEqual(saved_settings["restart"], [])
        self.assertEqual(saved_settings["handoff"]["ok"], True)
        self.assertEqual(saved_settings["handoff"]["main"], "dell")
        self.assertNotIn("next", saved_settings)
        shown = {row["path"]: row["value"] for row in saved_settings["fields"]}
        self.assertEqual(shown["cluster.main_device"], "dell")

        self.fresh_role()
        before = self.path.read_bytes()
        preview = fm.main_transfer(self.with_relay(), self.state, {"target": "dell"}, config_path=self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((preview["mutated"], preview["support"], preview["status"]), (False, "supported", "ready"))
        fleet = self.stale()
        moved = fm.main_transfer_refuse(fleet, self.state, {"target": "dell"}, config_path=self.path)
        disk = self.disk()
        self.assertEqual(moved["ok"], True)
        self.assertEqual(moved["mutated"], True)
        self.assertEqual(moved["main"], "dell")
        self.assertNotIn("next", moved)
        self.assertEqual(disk["cluster"]["main_device"], "dell")
        self.assertEqual(disk["devices"]["relay"]["label"], "Relay")
        self.assertEqual(disk["cluster"]["tunnel"], "forward")
        self.assertEqual(fleet["cluster"]["main_device"], "zenbook")
        self.assertEqual(len(self.calls), 1)

    def test_failed_and_pending_preserve_role_and_concurrent_devices(self):
        def failed(old, new, config, base):
            return self.handoff(old, new, config, base, ok=False, status="failed", add="pixel")

        def pending(old, new, config, base):
            return self.handoff(old, new, config, base, ok=False, status="pending", add="pixel")

        self.bind(failed)
        before = self.disk()
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["ok"], False)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["mutated"], False)
        self.assertEqual(out["main_device"], "zenbook")
        self.assertNotIn("next", out)
        disk = self.disk()
        self.assertEqual(disk["cluster"]["main_device"], "zenbook")
        self.assertEqual(disk["devices"]["pixel"]["label"], "pixel")
        self.assertEqual(disk["devices"]["relay"]["label"], "Relay")
        self.assertEqual(disk["cluster"]["tunnel"], before["cluster"]["tunnel"])
        self.assertEqual(self.backups(), [])

        self.write_disk(self.with_relay())
        clear_production(self.state)
        self.bind(pending)
        memory = self.stale()
        saved = settings.write(memory, self.path, self.state, {"cluster.main_device": "dell"})
        self.assertEqual(saved["saved"], [])
        self.assertEqual(saved["restart"], [])
        self.assertEqual(saved["handoff"]["status"], "pending")
        self.assertEqual(saved["handoff"]["ok"], False)
        self.assertNotIn("main_device", settings._SAVED_CLUSTER.get(str(self.state), {}))
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(self.disk()["devices"]["relay"]["label"], "Relay")
        self.assertEqual(self.disk()["devices"]["pixel"]["label"], "pixel")
        self.assertEqual(memory["cluster"]["main_device"], "zenbook")

        self.write_disk(self.with_relay())
        with self.assertRaises(fm.FleetError) as caught:
            fm.main_transfer_refuse(self.stale(), self.state, {"target": "dell"}, config_path=self.path)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.payload["code"], "pending")
        self.assertEqual(caught.exception.payload["mutated"], False)
        self.assertEqual(caught.exception.payload["handoff"]["status"], "pending")
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertIn("relay", self.disk()["devices"])
        self.assertIn("pixel", self.disk()["devices"])
        self.assertEqual(self.backups(), [])

    def test_preview_stays_read_only(self):
        self.bind(self.accept)
        before = self.path.read_bytes()
        blocked = fm.main_transfer(self.with_relay(), self.state, {"target": "dell"})
        self.assertEqual((blocked["status"], blocked["support"], blocked["mutated"]), ("blocked", "blocked", False))
        ready = fm.main_transfer(self.with_relay(), self.state, {"target": "dell"}, config_path=self.path)
        self.assertEqual((ready["status"], ready["support"], ready["mutated"]), ("ready", "supported", False))
        cloud = dict(self.with_relay())
        cloud["devices"]["oracle"] = {"label": "Oracle", "profile": "cloud"}
        denied = fm.main_transfer(cloud, self.state, {"target": "oracle"}, config_path=self.path)
        self.assertEqual(denied["support"], "blocked")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_support_needs_owner_local_main_binding_and_path(self):
        self.assertEqual(self._support(True, True), "blocked")
        self.bind(self.accept)
        self.assertEqual(self._support(True, True, self.state, self.path), "supported")
        self.assertEqual(self._support(True, True), "blocked")
        self.assertEqual(self._support(False, True, self.state, self.path), "blocked")
        self.assertEqual(self._support(True, False, self.state, self.path), "blocked")
        self.assertEqual(self._support(True, True, self.state, self.tmp / "missing.json"), "blocked")
        self.assertEqual(self._support(True, True, self.state, "jarvis.config.json"), "blocked")
        clear_production(self.state)
        self.assertEqual(self._support(True, True, self.state, self.path), "blocked")

    def _support(self, owner, local_main, state=None, config_path=None):
        rows = fm._actions(owner, local_main, state, config_path)
        return {row["id"]: row["support"] for row in rows}["main_transfer"]

    def test_missing_path_and_relative_path_fail_closed(self):
        self.bind(self.accept)
        before = self.path.read_bytes()
        for path in (None, "jarvis.config.json", self.tmp / "missing.json"):
            with self.assertRaises(fm.FleetError) as caught:
                fm.main_transfer_refuse(self.with_relay(), self.state, {"target": "dell"}, config_path=path)
            self.assertEqual((caught.exception.status, caught.exception.payload["code"],
                              caught.exception.payload["mutated"]), (409, "blocked", False))
            self.assertTrue(caught.exception.payload["missing"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_node_and_disabled_cluster_do_not_call_handoff(self):
        self.bind(self.accept)
        node = self.with_relay()
        node["cluster"]["local_device"] = "dell"
        off = self.with_relay()
        off["cluster"]["enabled"] = False
        for cfg, code in ((node, "not_main"), (off, "cluster_off")):
            with self.assertRaises(fm.FleetError) as caught:
                fm.main_transfer_refuse(cfg, self.state, {"target": "dell"}, config_path=self.path)
            self.assertEqual(caught.exception.payload["code"], code)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_closed_gate_refuses_before_handoff(self):
        self.bind(self.accept)
        close_unfenced("set_main")
        before = self.path.read_bytes()
        with self.assertRaises(IntegrationError) as caught:
            cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(caught.exception.code, "legacy_retained")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_mixed_settings_reject_without_writing(self):
        self.bind(self.accept)
        memory = self.stale()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            settings.write(memory, self.path, self.state, {"cluster.main_device": "dell", "voice.kokoro_speed": 1.1})
        self.assertIn("cannot be combined", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.assertNotIn("kokoro_speed", memory.get("voice", {}))
        self.assertEqual(self.backups(), [])

    def test_cloud_unknown_and_mismatched_main_do_not_persist(self):
        self.bind(self.accept)
        with self.assertRaises(ValueError):
            cluster_cli.set_main(self.stale(), self.path, "ghost", state=self.state)
        cloud = self.with_relay()
        cloud["devices"]["oracle"] = {"profile": "cloud"}
        self.write_disk(cloud)
        with self.assertRaises(ValueError) as caught:
            cluster_cli.set_main(self.stale(), self.path, "oracle", state=self.state)
        self.assertIn("cloud", str(caught.exception))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

        def mismatch(old, new, config, base):
            return self.handoff(old, new, config, base, ok=True, main="zenbook")

        clear_production(self.state)
        self.bind(mismatch)
        self.write_disk(self.with_relay())
        with self.assertRaises(ValueError) as caught:
            cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertIn("different main", str(caught.exception))
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(self.backups(), [])
        self.assertEqual(len(self.calls), 1)

    def test_callback_success_without_authority_status_does_not_publish(self):
        configure_production(self.state, _Runtime(), handoff=self.accept)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertIn("authenticated authority status", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.state / "main-handoff.json").exists())

    def test_unproven_recovery_and_unreadiness_cannot_publish(self):
        self.bind(self.accept)
        self.auth.lease_valid = False
        self.auth.barrier = 3
        before = self.path.read_bytes()
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["code"], "unproven_recovery")
        self.assertEqual(out["mutated"], False)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

        self.auth.lease_valid = True
        self.auth.barrier = 0
        self.auth.readiness = {"control_ready": False, "ai_ready": False}
        blocked = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(blocked["status"], "not_ready")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

        self.auth.readiness = {"control_ready": True, "ai_ready": False}
        claimed = self.bind  # keep the same adapter; rebind a recover claim over a live lease
        def recover(old, new, config, base):
            return self.handoff(old, new, config, base, ok=True, action="recover")
        claimed(recover)
        self.auth.lease_valid = True
        self.auth.barrier = 0
        refused = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(refused["code"], "unproven_recovery")
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(len(self.calls), 1)

    def test_publication_failure_retries_without_a_second_handoff(self):
        self.bind(self.handoff)
        real = patch_config
        calls = {"n": 0}

        def fail_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real(*args, **kwargs)

        with mock.patch("jarvis.config_lock.patch_config", side_effect=fail_once):
            failed = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(failed["status"], "publication_failed")
        self.assertEqual(failed["authority"], "granted")
        self.assertEqual(failed["mutated"], False)
        self.assertIn("not published", failed["error"])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(self.intent()["publication"], "publication_failed")
        self.assertEqual(len(self.calls), 1)

        retried = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(retried["ok"], True)
        self.assertEqual(retried["main"], "dell")
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.intent()["publication"], "published")
        self.assertNotIn("lease_id", json.dumps(self.intent()))

    def test_publication_conflict_leaves_the_other_role(self):
        def move(old, new, config, base):
            self.handoff(old, new, config, base)
            def change(doc):
                doc["cluster"]["main_device"] = "pixel"
                doc.setdefault("devices", {})["pixel"] = {"label": "Pixel"}
            patch_config(self.state, self.path, change)
            return {"ok": True, "status": "ready", "transition_id": "t-1", "main": new, "epoch": 4,
                    "action": "set_preferred_main"}

        self.bind(move)
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["status"], "publication_conflict")
        self.assertIn("Authority may have moved", out["error"])
        self.assertEqual(self.disk()["cluster"]["main_device"], "pixel")
        self.assertEqual(len(self.calls), 1)
        again = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(again["status"], "publication_conflict")
        self.assertEqual(self.disk()["cluster"]["main_device"], "pixel")
        self.assertEqual(len(self.calls), 1)

    def test_pending_records_the_transition_id_and_fsyncs_the_intent(self):
        def pending(old, new, config, base):
            return self.handoff(old, new, config, base, ok=False, status="pending")

        self.bind(pending)
        with mock.patch("os.fsync", wraps=os.fsync) as synced:
            out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertGreaterEqual(synced.call_count, 2)
        self.assertEqual(out["status"], "pending")
        self.assertEqual(self.intent()["publication"], "pending")
        self.assertEqual(self.intent()["transition_id"], "t-1")
        self.assertNotIn("lease", json.dumps(self.intent()))
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_preview_missing_is_dynamic_and_offline_is_not_ready(self):
        self.bind(self.accept)
        ready = fm.main_transfer(self.with_relay(), self.state, {"target": "dell"}, config_path=self.path)
        static = {item for item, _why in fm.MAIN_TRANSFER_MISSING}
        self.assertEqual(ready["status"], "ready")
        self.assertFalse(static & {item["id"] for item in ready["missing"]})
        self.assertNotIn("not implemented", ready["why"])
        self.assertIn("not proof", ready["why"])
        from jarvis.cluster_store import Broker
        Broker(self.state).enroll("dell", ["inventory"])
        import sqlite3
        db = sqlite3.connect(self.state / "cluster.sqlite3")
        db.execute("UPDATE nodes SET seen=? WHERE id=?", (1, "dell"))
        db.commit()
        db.close()
        offline = fm.main_transfer(self.with_relay(), self.state, {"target": "dell"}, config_path=self.path)
        self.assertEqual(offline["status"], "blocked")
        self.assertIn("offline", offline["why"].lower())
        self.assertIn("online", {item["id"] for item in offline["missing"]})
        self.assertFalse(static <= {item["id"] for item in offline["missing"]})
        view = fm.inventory(self.with_relay(), self.state, config_path=self.path)
        dell = next(row for row in view["devices"] if row["id"] == "dell")
        self.assertIn("not operational proof", dell["main_eligible_why"])
        self.assertEqual({row["id"]: row["support"] for row in view["actions"]}["main_transfer"], "supported")

    def test_console_preview_and_post_pass_the_config_path(self):
        from jarvis.ui import Console
        self.bind(self.accept)
        console = Console(self.with_relay(), self.tmp, self.state)
        self.addCleanup(console.close)
        preview = console.fleet_main_preview({"target": "dell"})
        self.assertEqual((preview["status"], preview["support"], preview["mutated"]), ("ready", "supported", False))
        self.assertEqual(self.calls, [])
        moved = console.fleet_main_transfer({"target": "dell"})
        self.assertEqual(moved["main"], "dell")
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")

    def test_authenticated_gateway_status_gates_publication(self):
        """Loopback gateway status is the authority check. This is not a brain-sync handoff."""
        from jarvis.continuity.gateway import AuthorityClient, ContinuityGateway
        from tests.continuity._fleet import Clock
        from tests.continuity.test_gateway import PC, _keys

        clock = Clock()
        gateway = ContinuityGateway.open(
            self.tmp / "authority.sqlite", "home", "oracle-auth", [_keys()["owner"][0]],
            monotonic=clock.mono, wall=clock.wall)
        self.addCleanup(gateway.close)
        origin = gateway.serve()
        self.assertTrue(origin.startswith("http://127.0.0.1:"), origin)
        owner = AuthorityClient(origin, authority_id="oracle-auth", wall=clock.wall,
                                owner_private_key_path=_keys()["owner"][1])
        for name, kind in (("zenbook", "pc"), ("dell", "pc")):
            owner.enroll({"device_id": name, "installation_id": name + "-inst", "kind": kind,
                          "public_key": _keys()[name][0], "capabilities": list(PC)})
        zen = AuthorityClient(origin, authority_id="oracle-auth", wall=clock.wall,
                              device_id="zenbook", installation_id="zenbook-inst",
                              private_key_path=_keys()["zenbook"][1])
        version = zen.status()["mode_version"]
        version = owner.set_mode("PREPARING_V2", version)["mode_version"]
        version = owner.set_mode("QUIESCED", version)["mode_version"]
        for name in ("zenbook", "dell"):
            AuthorityClient(origin, authority_id="oracle-auth", wall=clock.wall,
                            device_id=name, installation_id=name + "-inst",
                            private_key_path=_keys()[name][1]).acknowledge(version + 1, 0)
        owner.set_mode("V2_ONLY_NO_MAIN", version)
        self.assertEqual(zen.status()["mode"], "V2_ONLY_NO_MAIN")
        self.assertIs(zen.status()["lease_valid"], False)

        class _Live:
            def __init__(self, client):
                self.client = client
                self.calls = []

            def authority_status(self):
                return self.client.status()

            def candidate_readiness(self, device):
                return {"control_ready": True, "ai_ready": False}

            def __call__(self, old, new, config, base):
                fresh = self.client.status()
                self.calls.append(fresh["mode"])
                return {"ok": True, "status": "ready", "main": new, "transition_id": "net-1",
                        "epoch": fresh.get("epoch"), "action": "recover"}

        live = _Live(zen)
        configure_production(self.state, _Runtime(), handoff=live)
        refused = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertNotEqual(refused.get("ok"), True)
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        status = zen.status()
        opened = owner.transition_request(
            "k-surface", "recover", "dell", status["incarnation"], status["epoch"], status["policy_version"])
        dell = AuthorityClient(origin, authority_id="oracle-auth", wall=clock.wall,
                               device_id="dell", installation_id="dell-inst",
                               private_key_path=_keys()["dell"][1])
        dell.readiness(opened["transition_id"], True, False, "cp-1")
        owner.step(opened["transition_id"], "drain")
        owner.step(opened["transition_id"], "fence")
        owner.step(opened["transition_id"], "grant")
        owner.step(opened["transition_id"], "complete", True)
        granted = dell.status()
        self.assertEqual((granted["mode"], granted["main"], granted["main_ready"]), ("V2_ACTIVE", "dell", True))
        clear_production(self.state)
        (self.state / "main-handoff.json").unlink(missing_ok=True)

        class _Publish(_Live):
            def __call__(self, old, new, config, base):
                fresh = self.client.status()
                self.calls.append(fresh["main"])
                return {"ok": True, "status": "ready", "main": fresh["main"], "transition_id": "net-2",
                        "epoch": fresh["epoch"], "action": "set_preferred_main"}

        publisher = _Publish(dell)
        configure_production(self.state, _Runtime(), handoff=publisher)
        published = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(published["ok"], True)
        self.assertEqual(published["main"], "dell")
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")
        self.assertEqual(publisher.calls, ["dell"])
        self.assertNotIn("lease_id", json.dumps(self.intent()))

    def test_missing_config_file_is_not_seeded_from_memory(self):
        self.bind(self.accept)
        missing = self.tmp / "absent.json"
        with self.assertRaises(ValueError):
            cluster_cli.set_main(self.with_relay(), missing, "dell", state=self.state)
        self.assertFalse(missing.exists())
        self.assertEqual(self.calls, [])

    def test_changed_incarnation_is_not_adopted(self):
        def shift(old, new, config, base):
            body = self.handoff(old, new, config, base)
            self.auth.incarnation = 2
            return body

        self.bind(shift)
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["code"], "incarnation_changed")
        self.assertEqual(out["mutated"], False)
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(self.intent()["incarnation"], 1)
        self.assertNotEqual(self.intent()["incarnation"], 2)
        self.assertNotEqual(self.intent()["publication"], "published")
        again = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(again["code"], "incarnation_changed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.intent()["incarnation"], 1)
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_status_loss_after_callback_reconciles_without_replay(self):
        def once(old, new, config, base):
            body = self.handoff(old, new, config, base)
            self.auth.fail_status = 1
            return body

        self.bind(once)
        lost = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(lost["code"], "reconcile_required")
        self.assertEqual(lost["authority"], "unknown")
        self.assertEqual(lost["publication"], "outcome_unknown")
        self.assertEqual(lost["mutated"], False)
        self.assertEqual(lost["callback"]["transition_id"], "t-1")
        self.assertNotIn("lease_id", json.dumps(lost))
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(self.intent()["publication"], "outcome_unknown")
        self.assertIs(self.intent()["reconcile_required"], True)
        self.assertEqual(len(self.calls), 1)
        retried = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(retried["ok"], True)
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")
        self.assertEqual(len(self.calls), 1)

    def test_lost_result_binds_only_a_matching_transition(self):
        def lost(old, new, config, base):
            self.handoff(old, new, config, base)
            self.auth.force_active = True
            self.auth.mode = "HANDOVER"
            self.auth.lease_valid = True
            self.auth.active = {
                "state": "DRAINING", "candidate": "dell", "source": "zenbook",
                "transition_id": "t-bound", "action": "set_preferred_main",
                "expected_incarnation": 1, "expected_epoch": 4,
            }
            return {"ok": True, "status": "ready", "main": new}

        self.bind(lost)
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["code"], "bound")
        self.assertEqual(out["mutated"], False)
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(self.intent()["transition_id"], "t-bound")
        self.assertEqual(self.intent()["incarnation"], 1)
        self.assertNotEqual(self.intent()["publication"], "published")

    def test_ready_target_without_a_tuple_is_not_success(self):
        def lost(old, new, config, base):
            self.handoff(old, new, config, base)
            return {"ok": True, "status": "ready", "main": new}

        self.bind(lost)
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["code"], "not_proof")
        self.assertEqual(out["mutated"], False)
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertNotEqual(self.intent()["publication"], "published")
        self.assertEqual(len(self.calls), 1)

    def test_ambiguous_retry_does_not_call_again(self):
        record = {
            "target": "dell", "old": "zenbook", "transition_id": None, "epoch": 4,
            "publication": "outcome_unknown", "action": "set_preferred_main",
            "incarnation": 1, "mode": "V2_ACTIVE", "outcome": "outcome_unknown",
            "reconcile_required": True, "expected_epoch": 4, "expected_incarnation": 1,
            "schema": 1,
        }
        (self.state / "main-handoff.json").write_text(json.dumps(record), encoding="utf-8")
        self.bind(self.accept)
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["code"], "reconcile_required")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_corrupt_journal_refuses_without_overwrite_or_callback(self):
        path = self.state / "main-handoff.json"
        cases = (
            b"{",
            b"[]",
            b'{"target":"dell","epoch":true,"publication":"pending"}',
            b'{"schema":2,"target":"dell","publication":"pending"}',
            b'{"target":"dell","publication":"pending","lease_id":"ls-secret-value"}',
        )
        self.bind(self.accept)
        for raw in cases:
            path.write_bytes(raw)
            before = path.read_bytes()
            with self.assertRaises(ValueError) as caught:
                cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
            self.assertIn("handoff journal", str(caught.exception))
            self.assertNotIn("ls-secret-value", str(caught.exception))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.calls, [])
            self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_legacy_journal_still_finishes_publication(self):
        legacy = {
            "target": "dell", "old": "zenbook", "transition_id": "t-1", "epoch": 4,
            "publication": "publication_failed", "action": "set_preferred_main",
            "incarnation": 1, "mode": "V2_ACTIVE",
        }
        (self.state / "main-handoff.json").write_text(json.dumps(legacy), encoding="utf-8")
        self.bind(self.accept)
        self.auth.phase = "after"
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["ok"], True)
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.intent()["incarnation"], 1)

    def test_conflicting_transition_refuses_without_a_journal(self):
        self.bind(self.accept)
        self.auth.mode = "HANDOVER"
        self.auth.active = {
            "state": "DRAINING", "candidate": "pixel", "source": "zenbook",
            "transition_id": "t-other", "action": "set_preferred_main",
            "expected_incarnation": 1, "expected_epoch": 4,
        }
        before = self.path.read_bytes()
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["code"], "transition_conflict")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.state / "main-handoff.json").exists())

    def test_same_target_draining_resumes_and_does_not_publish(self):
        def pending(old, new, config, base):
            return self.handoff(old, new, config, base, ok=False, status="pending")

        self.bind(pending)
        self.auth.mode = "HANDOVER"
        self.auth.active = {
            "state": "DRAINING", "candidate": "dell", "source": "zenbook",
            "transition_id": "t-1", "action": "set_preferred_main",
            "expected_incarnation": 1, "expected_epoch": 4,
        }
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["status"], "pending")
        self.assertNotEqual(out.get("code"), "unproven_recovery")
        self.assertEqual(out["mutated"], False)
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.intent()["publication"], "pending")

        self.auth.active["expected_epoch"] = 99
        self.auth.phase = "before"
        self.calls.clear()
        (self.state / "main-handoff.json").unlink()
        refused = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(refused["code"], "transition_not_validated")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_windows_skips_directory_fsync(self):
        self.bind(self.accept)
        real_open = os.open

        def guard(path, flags, *args, **kwargs):
            if flags & getattr(os, "O_DIRECTORY", 0):
                raise AssertionError("directory fsync on Windows")
            return real_open(path, flags, *args, **kwargs)

        class _NtOs:
            """Stand-in for cluster_cli.os. name is Windows; everything else is the real os.

            Patching os.name on the module itself makes pathlib build WindowsPath.
            """

            name = "nt"

            def __getattr__(self, key):
                return getattr(os, key)

        with mock.patch("jarvis.cluster_cli.os", _NtOs()), \
                mock.patch("os.open", side_effect=guard), \
                mock.patch("os.fsync", wraps=os.fsync) as synced:
            out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["ok"], True)
        self.assertGreaterEqual(synced.call_count, 1)
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")

    def test_directory_fsync_failure_does_not_rewrite_the_role(self):
        self.bind(self.accept)
        real_open = os.open

        def boom(path, flags, *args, **kwargs):
            if flags & getattr(os, "O_DIRECTORY", 0):
                raise OSError("directory fsync failed")
            return real_open(path, flags, *args, **kwargs)

        before = self.path.read_bytes()
        with mock.patch("os.open", side_effect=boom):
            with self.assertRaises(OSError):
                cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_source_offline_promotion_needs_central_expiry(self):
        from types import SimpleNamespace

        def recover(old, new, config, base):
            body = self.handoff(old, new, config, base, action="recover")
            body["epoch"] = 5
            return body

        self.bind(recover)
        self.auth.lease_valid = False
        self.auth.barrier = None
        self.auth.mode = "V2_ONLY_NO_MAIN"
        refused = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(refused["code"], "unproven_recovery")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

        self.auth.lease_valid = True
        self.auth.barrier = 0
        self.auth.mode = "V2_ACTIVE"
        still = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(still["code"], "unproven_recovery")
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

        self.auth.lease_valid = False
        self.auth.barrier = 0
        self.auth.mode = "V2_ONLY_NO_MAIN"
        args = SimpleNamespace(action="main", value="dell", config=str(self.path), state=str(self.state))
        out = cluster_cli.run(args, self.stale(), self.tmp)
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["main"], "dell")
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")
        self.assertEqual(len(self.calls), 1)

    def test_real_safe_handoff_readiness_seam(self):
        """Uses SafeHandoff itself. The readiness method is not on that class yet.

        Until it is, set_main reports that seam and does not publish. When the method
        exists, the same test drives the real handoff through set_main to a published role.
        """
        from jarvis.continuity.runtime import MainRuntime, SafeHandoff, StoreAdapter
        from tests.continuity._fleet import Clock, ack, cutover, open_store, promote
        from tests.continuity.test_safe_handoff import AckingRuntime

        clock = Clock()
        store = open_store(self.tmp / "authority.sqlite", clock)
        self.addCleanup(store.close)
        cutover(store)
        _tid, lease = promote(store, "recover", "zenbook", "k-boot")
        # Dell and oracle must already refuse this epoch. The source acks during drain.
        # Without that, the fence stays pending and set_main does not publish.
        ack(store, ("dell", "oracle"))
        port = StoreAdapter(store)

        def runtime(device, cls=MainRuntime):
            item = cls(port, device, device + "-inst",
                       lambda: {"control_ready": True, "checkpoint": "cp-1"},
                       clock=clock.mono, boot=lambda: "boot-a")
            if device == "zenbook":
                item.accept_lease(lease)
            item.start()
            return item

        source = runtime("zenbook", AckingRuntime)
        candidate = runtime("dell")
        previous = sys.modules.get("jarvis.brainsync")
        module = types.ModuleType("jarvis.brainsync")
        module.handoff = lambda old, new, config, base: {"ok": True, "status": "synced"}
        sys.modules["jarvis.brainsync"] = module

        def restore():
            if previous is None:
                sys.modules.pop("jarvis.brainsync", None)
            else:
                sys.modules["jarvis.brainsync"] = previous

        self.addCleanup(restore)
        handoff = SafeHandoff(
            port, source_runtime=source, candidate_runtime=candidate,
            readiness=lambda _device: {"control_ready": True, "ai_ready": False, "checkpoint": "cp-1"},
            repoint=lambda old, new, _lease: {
                "ok": True, "authenticated": True, "key_updated": True, "endpoint_updated": True},
            canary=lambda _new: True,
            journal_path=self.state / "safe-handoff-journal.json")
        configure_production(self.state, source, handoff=handoff)
        if not callable(getattr(handoff, "candidate_readiness", None)):
            with self.assertRaises(ValueError) as caught:
                cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
            self.assertIn("candidate readiness", str(caught.exception))
            self.assertIn("cluster.main_device was not changed", str(caught.exception))
            self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
            self.assertFalse((self.state / "main-handoff.json").exists())
            return
        out = cluster_cli.set_main(self.stale(), self.path, "dell", state=self.state)
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["main"], "dell")
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")


if __name__ == "__main__":
    unittest.main()
