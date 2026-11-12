"""Same-machine races for legacy JSON pairing join and enroll.

Temp directories only. Device ids are arbitrary. Nothing here branches on the host OS,
writes a production config, starts a service, or calls a model.
"""

import argparse
import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import cluster_cli, config_lock
from jarvis.cluster_store import Broker
from jarvis.config_lock import ConfigDrift

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINewPairingKeyMaterialOnly0000"
NODE = "node-7k"
MAIN = "main-3p"
OTHER = "node-4r"
FLIP = "main-9z"
OLD = "o" * 32
NEW = "n" * 32
RUN = "1" * 32
DONE = "3" * 32


def _args(path, state, action, **kw):
    base = dict(action=action, config=str(path), state=str(state), value=None,
                main_device=None, local_device=None, url=None, hostname=None, capability=None,
                output=None, device_id=None, address=[], no_autostart=True, no_start=True,
                port=7777, rollback=False, autostart=False)
    return argparse.Namespace(**{**base, **kw})


def _note_bytes(node, main, token, key):
    body = json.dumps({"node_id": node, "main_device": main, "token": token, "code_key": key},
                      separators=(",", ":"), sort_keys=True)
    return (body + "\n").encode()


class PairingRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.path = self.tmp / "jarvis.config.json"
        self.note = self.state / "cluster-node.json"
        self.bundle = self.tmp / "bundle.json"

    def _write(self, path, doc):
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    def _bundle(self, node=NODE, main=MAIN, token=NEW, key=OTHER_KEY, url="http://127.0.0.1:7791"):
        self._write(self.bundle, {
            "node_id": node, "main_device": main, "token": token, "code_key": key, "main_url": url,
            "devices": {main: {"hostname": "h-main", "label": "BundleMain"},
                        node: {"hostname": "h-node", "label": "BundleNode"}},
        })

    def _backups(self):
        return [p.name for p in self.tmp.iterdir() if ".pre-" in p.name]

    def test_stale_bundle_after_promotion_refuses_without_touching_key_note_or_config(self):
        disk = {"cluster": {"enabled": True, "local_device": NODE, "main_device": NODE,
                            "main_url": "http://127.0.0.1:7780"},
                "devices": {NODE: {"hostname": "h-node", "label": "DiskNode"},
                            MAIN: {"hostname": "h-main", "label": "DiskMain"}},
                "promoted": True}
        self._write(self.path, disk)
        config_bytes = self.path.read_bytes()
        self.note.write_bytes(_note_bytes(NODE, MAIN, OLD, HOST_KEY))
        note_bytes = self.note.read_bytes()
        self._bundle()
        stale = {"cluster": {"enabled": True, "local_device": NODE, "main_device": MAIN,
                             "main_url": "http://127.0.0.1:7780"},
                 "devices": {NODE: {"hostname": "h-node"}, MAIN: {"hostname": "h-main"}}}
        real_read = config_lock.read_config

        def read_config(path):
            self.assertTrue(config_lock.held(self.state))
            return real_read(path)

        with mock.patch.object(config_lock, "read_config", side_effect=read_config):
            with self.assertRaises(ValueError) as caught:
                cluster_cli.run(_args(self.path, self.state, "join", value=str(self.bundle)),
                                stale, self.tmp)
        self.assertIn("main", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), config_bytes)
        self.assertEqual(self.note.read_bytes(), note_bytes)
        self.assertIn(HOST_KEY.encode(), note_bytes)
        self.assertNotIn(OTHER_KEY.encode(), self.note.read_bytes())
        self.assertEqual(json.loads(self.note.read_text(encoding="utf-8"))["token"], OLD)
        self.assertEqual(self._backups(), [])

    def test_invalid_enabled_identity_refuses_join(self):
        disk = {"cluster": {"enabled": True, "local_device": "missing-id", "main_device": MAIN,
                            "main_url": "http://127.0.0.1:7780"},
                "devices": {MAIN: {"hostname": "h-main"}, NODE: {"hostname": "h-node"}},
                "keep": [1]}
        self._write(self.path, disk)
        config_bytes = self.path.read_bytes()
        self.note.write_bytes(_note_bytes(NODE, MAIN, OLD, HOST_KEY))
        note_bytes = self.note.read_bytes()
        self._bundle()
        stale = {"cluster": {"enabled": True, "local_device": NODE, "main_device": MAIN,
                             "main_url": "http://127.0.0.1:7780"},
                 "devices": {NODE: {"hostname": "h-node"}, MAIN: {"hostname": "h-main"}}}
        with self.assertRaises(ValueError) as caught:
            cluster_cli.run(_args(self.path, self.state, "join", value=str(self.bundle)),
                            stale, self.tmp)
        self.assertIn("invalid", str(caught.exception).lower())
        self.assertEqual(self.path.read_bytes(), config_bytes)
        self.assertEqual(self.note.read_bytes(), note_bytes)
        self.assertEqual(self._backups(), [])

    def test_active_pairing_for_another_node_is_refused(self):
        disk = {"cluster": {"enabled": True, "local_device": NODE, "main_device": MAIN,
                            "main_url": "http://127.0.0.1:7780"},
                "devices": {NODE: {"hostname": "h-node", "label": "DiskNode"},
                            MAIN: {"hostname": "h-main", "label": "DiskMain"}}}
        self._write(self.path, disk)
        config_bytes = self.path.read_bytes()
        self.note.write_bytes(_note_bytes(NODE, MAIN, OLD, HOST_KEY))
        note_bytes = self.note.read_bytes()
        self._bundle(node=OTHER, token=NEW, key=OTHER_KEY)
        with self.assertRaises(ValueError) as caught:
            cluster_cli.run(_args(self.path, self.state, "join", value=str(self.bundle)),
                            disk, self.tmp)
        self.assertIn("already paired", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), config_bytes)
        self.assertEqual(self.note.read_bytes(), note_bytes)
        self.assertEqual(self._backups(), [])

    def test_config_conflict_leaves_the_prior_node_credential(self):
        disk = {"cluster": {"enabled": True, "local_device": NODE, "main_device": MAIN,
                            "main_url": "http://127.0.0.1:7781"},
                "devices": {NODE: {"hostname": "h-node", "label": "DiskNode"},
                            MAIN: {"hostname": "h-main", "label": "DiskMain"},
                            OTHER: {"hostname": "h-other"}}}
        self._write(self.path, disk)
        self.note.write_bytes(_note_bytes(NODE, MAIN, OLD, HOST_KEY))
        note_bytes = self.note.read_bytes()
        self._bundle(url="http://127.0.0.1:7782", token=NEW, key=OTHER_KEY)
        real_backup = config_lock._backup

        def backup(path, raw, tag, mode):
            self.assertTrue(config_lock.held(self.state))
            current = json.loads(Path(path).read_text(encoding="utf-8"))
            current["marker"] = "editor"
            Path(path).write_text(json.dumps(current), encoding="utf-8")
            return real_backup(path, raw, tag, mode)

        with mock.patch.object(config_lock, "_backup", side_effect=backup):
            with self.assertRaises(ConfigDrift):
                cluster_cli.run(_args(self.path, self.state, "join", value=str(self.bundle)),
                                disk, self.tmp)
        self.assertEqual(self.note.read_bytes(), note_bytes)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["marker"], "editor")
        self.assertEqual(saved["cluster"]["main_url"], "http://127.0.0.1:7781")
        self.assertEqual(saved["cluster"]["local_device"], NODE)
        self.assertEqual(json.loads(self.note.read_text(encoding="utf-8"))["code_key"], HOST_KEY)
        self.assertNotIn(NEW.encode(), self.note.read_bytes())

    def test_legacy_pairing_join_uses_the_disk_and_arbitrary_ids(self):
        disk = {"cluster": {"enabled": False, "extra": "stay"},
                "devices": {MAIN: {"hostname": "h-main", "label": "DiskMain"},
                            NODE: {"hostname": "h-node", "label": "DiskNode"}},
                "keep": {"flag": [1, 2]}}
        self._write(self.path, disk)
        self._bundle(url="http://127.0.0.1:7791", token=NEW, key=HOST_KEY)
        stale = {"cluster": {"enabled": True, "local_device": "stale-main", "main_device": "stale-main",
                             "main_url": "http://127.0.0.1:7780"},
                 "devices": {"stale-main": {"hostname": "h-stale"}}}
        report = cluster_cli.run(_args(self.path, self.state, "join", value=str(self.bundle)),
                                 stale, self.tmp)
        self.assertEqual(report["joined"], NODE)
        self.assertEqual(report["main_device"], MAIN)
        self.assertEqual(report["main_url"], "http://127.0.0.1:7791")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["cluster"]["local_device"], NODE)
        self.assertEqual(saved["cluster"]["main_device"], MAIN)
        self.assertTrue(saved["cluster"]["enabled"])
        self.assertEqual(saved["cluster"]["main_url"], "http://127.0.0.1:7791")
        self.assertEqual(saved["cluster"]["extra"], "stay")
        self.assertEqual(saved["keep"], {"flag": [1, 2]})
        self.assertEqual(saved["devices"][MAIN]["label"], "DiskMain")
        self.assertEqual(saved["devices"][NODE]["label"], "DiskNode")
        self.assertNotIn("stale-main", saved["devices"])
        got = json.loads(self.note.read_text(encoding="utf-8"))
        self.assertEqual(got["node_id"], NODE)
        self.assertEqual(got["main_device"], MAIN)
        self.assertEqual(got["token"], NEW)
        self.assertEqual(got["code_key"], HOST_KEY)

    def test_missing_code_key_writes_nothing(self):
        disk = {"keep": {"flag": [1]}, "devices": {MAIN: {"hostname": "h-main"}, NODE: {"hostname": "h-node"}}}
        self._write(self.path, disk)
        config_bytes = self.path.read_bytes()
        self._write(self.bundle, {"node_id": NODE, "main_device": MAIN, "token": NEW,
                                  "main_url": "http://127.0.0.1:7791"})
        with self.assertRaises(ValueError) as caught:
            cluster_cli.run(_args(self.path, self.state, "join", value=str(self.bundle)),
                            disk, self.tmp)
        self.assertIn("code-signing", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), config_bytes)
        self.assertFalse(self.note.exists())
        self.assertEqual(self._backups(), [])

    def test_enroll_role_flip_during_code_key_changes_nothing(self):
        disk = {"cluster": {"enabled": True, "local_device": MAIN, "main_device": MAIN,
                            "main_url": "http://127.0.0.1:7780"},
                "devices": {MAIN: {"hostname": "h-main", "label": "Main"},
                            NODE: {"hostname": "h-node", "label": "Node"},
                            FLIP: {"hostname": "h-flip", "label": "Flip"}}}
        self._write(self.path, disk)
        memory = json.loads(self.path.read_text(encoding="utf-8"))
        old = Broker(self.state).enroll(NODE, ["desktop"])
        before_hash = hashlib.sha256(old.encode()).hexdigest()
        with Broker(self.state).db() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                       (RUN, NODE, "owner", "status", "{}", "running", 1.0, time.time() + 3600, None))
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                       (DONE, NODE, "owner", "status", "{}", "done", 1.0, 0, "{}"))
        flipped = {}
        seen = []
        called = []
        output = self.tmp / "node-7k.pair.json"

        def code_key(state):
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            doc["cluster"]["main_device"] = FLIP
            self._write(self.path, doc)
            flipped["raw"] = self.path.read_bytes()
            return HOST_KEY

        real_read = config_lock.read_config

        def read_config(path):
            self.assertTrue(config_lock.held(self.state))
            doc = real_read(path)
            seen.append(doc["cluster"]["main_device"])
            return doc

        def enroll(*args, **kwargs):
            called.append("enroll")
            raise AssertionError("enroll ran")

        def settle(*args, **kwargs):
            called.append("settle")
            raise AssertionError("settle ran")

        with mock.patch.object(cluster_cli, "code_key", side_effect=code_key), \
                mock.patch.object(config_lock, "read_config", side_effect=read_config), \
                mock.patch.object(Broker, "enroll", side_effect=enroll), \
                mock.patch.object(Broker, "settle_rotated", side_effect=settle):
            with self.assertRaises(ValueError) as caught:
                cluster_cli.run(_args(self.path, self.state, "enroll", value=NODE, output=str(output)),
                                memory, self.tmp)
        self.assertIn("no longer the main", str(caught.exception))
        self.assertEqual(seen, [FLIP])
        self.assertEqual(called, [])
        self.assertEqual(self.path.read_bytes(), flipped["raw"])
        self.assertFalse(output.exists())
        self.assertFalse(self.note.exists())
        self.assertFalse((self.state / "cluster-invite-receipts.json").exists())
        self.assertEqual(Broker(self.state).authenticate(old)[0], NODE)
        with Broker(self.state).db() as db:
            row = db.execute("SELECT token_hash FROM nodes WHERE id=?", (NODE,)).fetchone()
            running = db.execute("SELECT state FROM jobs WHERE id=?", (RUN,)).fetchone()["state"]
            done = db.execute("SELECT state FROM jobs WHERE id=?", (DONE,)).fetchone()["state"]
        self.assertEqual(row["token_hash"], before_hash)
        self.assertEqual(running, "running")
        self.assertEqual(done, "done")
        self.assertEqual(memory["cluster"]["main_device"], MAIN)


if __name__ == "__main__":
    unittest.main()
