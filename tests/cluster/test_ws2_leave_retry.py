"""CLI leave reaches cluster_setup.leave before the enabled guard.

Temp files only. The tunnel and login autostart boundaries are fakes.
Nothing here reads a real config, starts a service, or loads a model.
"""

import argparse
import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from jarvis import cluster_cli

NODE = "node-7k"
MAIN = "main-3p"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE-NODE-KEY\n-----END OPENSSH PRIVATE KEY-----\n"
REVOKE = "Restart or stop this PC's node agent. On the main: jarvis cluster revoke "
TIMEOUT_NEXT = (
    "The tunnel is still running. Credentials, the stop marker, the lock, "
    "and the status note were kept. Autostart is off and the cluster is "
    "disabled so it will not start again. Stop the tunnel, then leave again."
)


def _args(path, state):
    return argparse.Namespace(
        action="leave", config=str(path), state=str(state), value=None,
        main_device=None, local_device=None, url=None, hostname=None, capability=None,
        output=None, device_id=None, address=[], no_autostart=True, no_start=True,
        port=7777, rollback=False, autostart=False)


def _fleet(local, main=MAIN, enabled=True):
    return {
        "cluster": {
            "enabled": enabled,
            "main_device": main,
            "local_device": local,
            "tunnel": "forward",
            "main_url": "http://127.0.0.1:7781",
        },
        "devices": {
            MAIN: {"hostname": "fixture-main", "label": "Main"},
            NODE: {"hostname": "fixture-node", "label": "Node"},
        },
        "keep": {"mine": True},
    }


class LeaveRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "jarvis.config.json"
        self.state = self.tmp / "state"
        self.state.mkdir()

    def _write(self, doc):
        self.path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    def _disk(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _plant_link(self):
        (self.state / "cluster-node.json").write_text(json.dumps({
            "node_id": NODE, "main_device": MAIN, "token": "t" * 32,
        }), encoding="utf-8")
        (self.state / "cluster-node.key").write_text(KEY, encoding="utf-8")
        (self.state / "cluster-tunnel.stop").write_text("marker\n", encoding="utf-8")
        (self.state / "cluster-tunnel.json").write_text("{}\n", encoding="utf-8")
        (self.state / "cluster-tunnel.lock").write_bytes(b"\0")
        (self.state / "notes.txt").write_text("keep\n", encoding="utf-8")

    @contextmanager
    def _boundary(self, running):
        calls = {"stop": 0, "autostart": []}

        def status(_state):
            return {"running": running[0], "ssh_alive": False}

        def stop(_state):
            calls["stop"] += 1
            return {"stopped": False, "note": "still running"}

        def tunnel_autostart(_path, _state, enable):
            calls["autostart"].append(("tunnel", enable))
            return {"autostart": False}

        def role_autostart(_path, _state, enable):
            calls["autostart"].append(("node", enable))
            return {"autostart": False}

        with mock.patch("jarvis.cluster_tunnel.status", side_effect=status), \
                mock.patch("jarvis.cluster_tunnel.stop", side_effect=stop), \
                mock.patch("jarvis.cluster_tunnel.autostart", side_effect=tunnel_autostart), \
                mock.patch("jarvis.cluster_cli.autostart", side_effect=role_autostart), \
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen")):
            yield calls

    def _leave(self, snapshot, running):
        with self._boundary(running) as calls:
            report = cluster_cli.run(_args(self.path, self.state), snapshot, self.tmp)
        return report, calls

    def test_node_leave_uses_the_file_and_returns_the_setup_result(self):
        self._write(_fleet(NODE))
        self._plant_link()
        stale = {"cluster": {"enabled": False}}
        report, calls = self._leave(stale, [False])
        self.assertEqual(report, {"left": NODE, "next": REVOKE + NODE})
        disk = self._disk()
        self.assertFalse(disk["cluster"]["enabled"])
        self.assertEqual(disk["cluster"]["local_device"], NODE)
        self.assertEqual(disk["cluster"]["main_device"], MAIN)
        self.assertEqual(disk["devices"][NODE]["hostname"], "fixture-node")
        self.assertEqual(disk["keep"], {"mine": True})
        self.assertFalse((self.state / "cluster-node.json").exists())
        self.assertFalse((self.state / "cluster-node.key").exists())
        self.assertEqual((self.state / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(calls["stop"], 0)
        self.assertEqual(calls["autostart"], [("tunnel", False), ("node", False)])
        self.assertEqual(stale, {"cluster": {"enabled": False}})

    def test_empty_file_does_not_copy_a_stale_snapshot(self):
        self._write({})
        (self.state / "notes.txt").write_text("keep\n", encoding="utf-8")
        stale = _fleet(NODE)
        stale["keep"] = "from-snapshot"
        report, _calls = self._leave(stale, [False])
        self.assertEqual(report, {"left": "NODE", "next": REVOKE + "NODE"})
        self.assertEqual(self._disk(), {"cluster": {"enabled": False}})
        self.assertNotIn("from-snapshot", self.path.read_text(encoding="utf-8"))
        self.assertEqual((self.state / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(stale["keep"], "from-snapshot")

    def test_enabled_main_is_refused_from_a_stale_snapshot(self):
        self._write(_fleet(MAIN))
        self._plant_link()
        before = self.path.read_bytes()
        key = (self.state / "cluster-node.key").read_bytes()
        note = (self.state / "cluster-node.json").read_bytes()
        expected = (f"This PC is the main ({MAIN}). Nodes leave; the main revokes "
                    "them. To hand the fleet to another PC: jarvis cluster main <device>")
        snapshots = {
            "disabled": {"cluster": {"enabled": False}},
            "node": _fleet(NODE),
        }
        for label, stale in snapshots.items():
            with self.subTest(label=label):
                with self._boundary([True]) as calls:
                    with self.assertRaises(ValueError) as caught:
                        cluster_cli.run(_args(self.path, self.state), stale, self.tmp)
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual((self.state / "cluster-node.key").read_bytes(), key)
                self.assertEqual((self.state / "cluster-node.json").read_bytes(), note)
                self.assertTrue(self._disk()["cluster"]["enabled"])
                self.assertEqual(self._disk()["cluster"]["local_device"], MAIN)
                self.assertEqual(self._disk()["cluster"]["main_device"], MAIN)
                self.assertEqual(calls["stop"], 0)
                self.assertEqual(calls["autostart"], [])

    def test_stop_timeout_keeps_credentials_until_the_retry(self):
        self._write(_fleet(NODE))
        self._plant_link()
        key = (self.state / "cluster-node.key").read_bytes()
        note = (self.state / "cluster-node.json").read_bytes()
        lock = self.state / "cluster-tunnel.lock"
        inode = lock.lstat().st_ino
        running = [True]
        with self._boundary(running) as calls:
            first = cluster_cli.run(_args(self.path, self.state), _fleet(NODE), self.tmp)
            self.assertEqual(first["left"], NODE)
            self.assertEqual(first["credentials_retained"], "retained until the tunnel stops")
            self.assertIn("still running", first["tunnel_stop_error"])
            self.assertEqual(first["next"], TIMEOUT_NEXT)
            disk = self._disk()
            self.assertFalse(disk["cluster"]["enabled"])
            self.assertEqual(disk["cluster"]["local_device"], NODE)
            self.assertEqual(disk["cluster"]["main_device"], MAIN)
            self.assertEqual(disk["keep"], {"mine": True})
            self.assertEqual((self.state / "cluster-node.key").read_bytes(), key)
            self.assertEqual((self.state / "cluster-node.json").read_bytes(), note)
            self.assertTrue((self.state / "cluster-tunnel.stop").is_file())
            self.assertTrue((self.state / "cluster-tunnel.json").is_file())
            self.assertEqual(lock.lstat().st_ino, inode)
            self.assertEqual((self.state / "notes.txt").read_text(encoding="utf-8"), "keep\n")
            running[0] = False
            again = cluster_cli.run(
                _args(self.path, self.state), {"cluster": {"enabled": False}}, self.tmp)
        self.assertEqual(again, {"left": NODE, "next": REVOKE + NODE})
        self.assertFalse((self.state / "cluster-node.json").exists())
        self.assertFalse((self.state / "cluster-node.key").exists())
        self.assertFalse((self.state / "cluster-tunnel.stop").exists())
        self.assertFalse((self.state / "cluster-tunnel.json").exists())
        self.assertEqual(lock.lstat().st_ino, inode)
        self.assertEqual((self.state / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        disk = self._disk()
        self.assertFalse(disk["cluster"]["enabled"])
        self.assertEqual(disk["cluster"]["local_device"], NODE)
        self.assertEqual(disk["cluster"]["main_device"], MAIN)
        self.assertEqual(disk["keep"], {"mine": True})
        self.assertEqual(calls["stop"], 1)
        self.assertEqual(calls["autostart"], [("tunnel", False), ("node", False)] * 2)


if __name__ == "__main__":
    unittest.main()
