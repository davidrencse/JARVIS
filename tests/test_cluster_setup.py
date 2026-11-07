import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import cluster_setup, cluster_tunnel, deploy
from jarvis.cluster import Client
from jarvis.cluster_store import Broker

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R"
MAIN = {"cluster": {"enabled": True, "main_device": "zenbook", "local_device": "auto",
                    "tunnel": "forward", "listen_port": 7780, "main_url": "http://127.0.0.1:7780"},
        "devices": {"zenbook": {"hostname": "arch", "label": "Zenbook"},
                    "dell": {"hostname": "WinLaptop", "label": "Dell", "transport": "cluster"}}}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.keys = self.tmp / "authorized_keys"
        self.keys.write_text("ssh-ed25519 AAAAexisting someone@else\n", encoding="utf-8")

    def _invite(self, node="dell"):
        path, state = self.tmp / "main.json", self.tmp / "main-state"
        path.write_text(json.dumps(MAIN), encoding="utf-8")
        with mock.patch.object(cluster_setup, "_hostname", return_value="arch"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="arch"), \
                mock.patch.object(cluster_setup, "_sshd_up", return_value=True), \
                mock.patch.object(cluster_setup, "host_key", return_value=HOST_KEY), \
                mock.patch.object(cluster_setup, "_local_ips", return_value=["192.168.1.5", "100.64.0.9"]), \
                mock.patch.object(cluster_setup, "_authorized_keys", return_value=self.keys), \
                mock.patch.object(cluster_setup.getpass, "getuser", return_value="jdoe"):
            return cluster_setup.invite(MAIN, path, state, node), state

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_invite_authorizes_one_restricted_key_and_enrolls(self):
        report, state = self._invite()
        lines = self.keys.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "ssh-ed25519 AAAAexisting someone@else")  # others untouched
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith('restrict,port-forwarding,permitopen="127.0.0.1:7780",'))
        self.assertIn('command="echo jarvis-node-link"', lines[1])
        self.assertTrue(lines[1].endswith(" jarvis-node:dell"))
        self.assertEqual([n["id"] for n in Broker(state).nodes()], ["dell"])
        note = cluster_setup.decode(report["code"])
        self.assertEqual((note["node_id"], note["main_device"], note["port"]), ("dell", "zenbook", 7780))
        self.assertEqual(note["addresses"], ["192.168.1.5", "100.64.0.9"])
        self.assertIn("BEGIN OPENSSH PRIVATE KEY", note["key"])
        self.assertTrue(Broker(state).authenticate(note["token"]))
        # A second invite rotates: still one line for dell, and the old credential is dead.
        again, _ = self._invite()
        self.assertEqual(len(self.keys.read_text(encoding="utf-8").splitlines()), 2)
        self.assertIsNone(Broker(state).authenticate(note["token"]))
        self.assertTrue(Broker(state).authenticate(cluster_setup.decode(again["code"])["token"]))

    def test_existing_invite_file_fails_before_anything_rotates(self):
        # Audit cluster-01: a leftover --output file used to fail only after the node's SSH
        # key and hub token were already replaced, unpairing it with no new code shown.
        path, state = self.tmp / "main.json", self.tmp / "main-state"
        path.write_text(json.dumps(MAIN), encoding="utf-8")
        old = self.tmp / "invite.json"
        old.write_text("{}", encoding="utf-8")
        with mock.patch.object(cluster_setup, "_hostname", return_value="arch"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="arch"), \
                mock.patch.object(cluster_setup, "_sshd_up", return_value=True), \
                mock.patch.object(cluster_setup, "authorize") as authorize, \
                mock.patch.object(cluster_setup, "keypair") as keypair, \
                mock.patch.object(cluster_setup.Broker, "enroll") as enroll:
            with self.assertRaisesRegex(ValueError, "already exists"):
                cluster_setup.invite(MAIN, path, state, "dell", output=str(old))
        authorize.assert_not_called()
        keypair.assert_not_called()
        enroll.assert_not_called()
        self.assertEqual(old.read_text(encoding="utf-8"), "{}")

    def test_concurrent_authorizes_keep_every_line(self):
        # invite (CLI) and the hub's redeem (a thread) both rewrite the file: none may be lost.
        import threading
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R"
        nodes = [f"n{i}" for i in range(12)]
        real_write, barrier = cluster_setup._write_keys, threading.Barrier(len(nodes), timeout=1)

        def slow_write(path, lines):  # widen the read-modify-write window
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            real_write(path, lines)

        with mock.patch.object(cluster_setup, "_write_keys", slow_write):
            threads = [threading.Thread(target=cluster_setup.authorize, args=(n, key, 7780, self.keys))
                       for n in nodes]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        text = self.keys.read_text(encoding="utf-8")
        self.assertIn("someone@else", text)
        for node in nodes:
            self.assertTrue(cluster_setup.authorized(node, self.keys), node)

    def test_windows_admin_found_by_group_even_unelevated(self):
        groups = '"BUILTIN\\Administrators","Alias","S-1-5-32-544","Group used for deny only"\n'
        run = mock.Mock(return_value=mock.Mock(stdout=groups))
        with mock.patch.object(cluster_setup.subprocess, "run", run):
            self.assertTrue(cluster_setup._windows_admin())
        with mock.patch.object(cluster_setup, "_os", return_value="windows"), \
                mock.patch.object(cluster_setup, "_windows_admin", return_value=True), \
                mock.patch.dict(cluster_setup.os.environ, {"PROGRAMDATA": str(self.tmp)}):
            self.assertEqual(cluster_setup._authorized_keys(), self.tmp / "ssh/administrators_authorized_keys")
        with mock.patch.object(cluster_setup, "_os", return_value="windows"), \
                mock.patch.object(cluster_setup, "_windows_admin", return_value=False):
            self.assertEqual(cluster_setup._authorized_keys(), Path.home() / ".ssh/authorized_keys")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_revoke_removes_credential_and_key(self):
        report, state = self._invite()
        with mock.patch.object(cluster_setup, "_authorized_keys", return_value=self.keys):
            out = cluster_setup.revoke(state, "dell")
        self.assertTrue(out["ssh_key_removed"])
        self.assertEqual(self.keys.read_text(encoding="utf-8"), "ssh-ed25519 AAAAexisting someone@else\n")
        self.assertIsNone(Broker(state).authenticate(cluster_setup.decode(report["code"])["token"]))

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_join_saves_link_in_state_not_config(self):
        report, _ = self._invite()
        path, state = self.tmp / "node.json", self.tmp / "node-state"
        shared = json.loads(json.dumps(MAIN))  # the same committed config, on the Dell
        path.write_text(json.dumps(shared), encoding="utf-8")
        with mock.patch.object(cluster_setup, "_hostname", return_value="WinLaptop"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop"):
            out = cluster_setup.join(shared, path, state, cluster_setup.decode(report["code"]),
                                     autostart=False, start=False)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(out["addresses"], ["jdoe@192.168.1.5", "jdoe@100.64.0.9"])
            self.assertEqual(saved["cluster"]["local_device"], "auto")  # hostname already matches
            self.assertNotIn("token", json.dumps(saved))
            self.assertNotIn("PRIVATE KEY", json.dumps(saved))
            self.assertTrue((state / "cluster-node.key").is_file())
            # The main's code-signing key is pinned: only code it signs gets installed here.
            pinned = json.loads((state / "cluster-node.json").read_text(encoding="utf-8"))
            self.assertEqual(pinned["code_key"], deploy.code_key(self.tmp / "main-state"))
            link = cluster_tunnel.peer(saved, state)
            self.assertEqual(link["target"], "jdoe@192.168.1.5")
            self.assertEqual(link["fallback_targets"], ["jdoe@100.64.0.9"])
            argv = cluster_tunnel.command(saved, state, link["target"])
            self.assertIn("127.0.0.1:7780:127.0.0.1:7780", argv)
            self.assertIn(str(state / "cluster-node.key"), argv)
            self.assertEqual(Client(saved, state).main, "zenbook")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_join_refuses_an_invite_for_another_pc(self):
        report, _ = self._invite()
        path = self.tmp / "node.json"
        path.write_text(json.dumps(MAIN), encoding="utf-8")
        with mock.patch.object(cluster_setup, "_hostname", return_value="arch"), \
                self.assertRaises(ValueError):
            cluster_setup.join(MAIN, path, self.tmp / "s", cluster_setup.decode(report["code"]),
                               autostart=False, start=False)

    def test_decode_accepts_files_and_rejects_damage(self):
        code = cluster_setup.encode({"v": 1, "x": 1})
        self.assertEqual(cluster_setup.decode(code)["x"], 1)
        self.assertEqual(cluster_setup.decode(code[:30] + "\n" + code[30:])["x"], 1)  # wrapped paste
        stored = self.tmp / "invite.txt"
        stored.write_text(code + "\n", encoding="utf-8")
        self.assertEqual(cluster_setup.decode(str(stored))["x"], 1)
        written = self.tmp / "dell.invite"  # what invite --output writes
        written.write_text(json.dumps({"code": code}), encoding="utf-8")
        self.assertEqual(cluster_setup.decode(str(written))["x"], 1)
        self.assertIsNone(cluster_setup.decode('{"node_id": "dell"}'))  # an old pairing bundle
        with self.assertRaises(ValueError):
            cluster_setup.decode(code[:-6])

    def test_validate_rejects_tampered_invites(self):
        good = {"v": 1, "node_id": "dell", "main_device": "zenbook", "token": "t" * 40,
                "host_key": HOST_KEY, "key": "-----BEGIN OPENSSH PRIVATE KEY-----\n", "port": 7780,
                "user": "jdoe", "addresses": ["192.168.1.5"], "code_key": "ssh-ed25519 AAAAC3Nz"}
        cluster_setup._validate(good)
        for change in ({"addresses": ["1.2.3.4 -oProxyCommand=x"]}, {"user": "-oProxy"},
                       {"port": 0}, {"host_key": "ssh-ed25519 AAA\nevil"}, {"main_device": "dell"},
                       {"addresses": []}, {"main": {"hostname": "a b"}},
                       {"code_key": None}, {"code_key": "ssh-rsa AAAA"}, {"code_key": "ssh-ed25519 A\nx"}):
            with self.assertRaises(ValueError, msg=change):
                cluster_setup._validate({**good, **change})

    def test_init_registers_this_pc_as_forward_main(self):
        path = self.tmp / "c.json"
        config = {"devices": {"dell": {"hostname": "WinLaptop"}}}
        with mock.patch.object(cluster_setup, "_hostname", return_value="archmain"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="archmain"), \
                mock.patch.object(cluster_setup, "_sshd_up", return_value=True):
            out = cluster_setup.init(config, path, self.tmp / "s", autostart=False)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(out["main_device"], "archmain")
        self.assertEqual(saved["cluster"]["main_device"], "archmain")
        self.assertEqual(saved["cluster"]["tunnel"], "forward")
        self.assertEqual(saved["devices"]["archmain"]["hostname"], "archmain")

    def test_doctor_on_main_names_unpaired_nodes(self):
        with mock.patch("jarvis.topology.socket.gethostname", return_value="arch"), \
                mock.patch.object(cluster_setup, "_sshd_up", return_value=True), \
                mock.patch.object(cluster_setup.Client, "request", side_effect=ConnectionError("down")), \
                mock.patch.object(cluster_setup.Client, "__init__", return_value=None):
            report = cluster_setup.doctor(MAIN, self.tmp / "c.json", self.tmp / "s")
        failed = {c["check"]: c for c in report["checks"] if not c["ok"]}
        self.assertFalse(report["ok"])
        self.assertIn("hub running", failed)
        self.assertEqual(failed["dell: paired"]["fix"], "jarvis cluster invite dell")


if __name__ == "__main__":
    unittest.main()


class TunnelStatusTests(unittest.TestCase):
    def test_exited_worker_note_with_pid_zero_is_not_running(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / f"{cluster_tunnel.NAME}.json").write_text(
                json.dumps({"pid": 0, "updated": time.time()}), encoding="utf-8")
            self.assertFalse(cluster_tunnel.status(tmp)["running"])
