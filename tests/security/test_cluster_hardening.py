"""Cluster SSH and hub checks that must fail closed. No real ssh, and no service."""

import base64
import io
import json
import os
import stat
import struct
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from jarvis import cluster_setup, deploy, probe
from jarvis.cluster import Hub, hub_hosts
from jarvis.cluster_store import Broker
from jarvis.clipsync import _pinned_host
from jarvis.transfer import _ssh_args, ssh_host

KNOWN = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R"
MAIN = {"devices": {"main": {"hostname": "main-host", "transport": "local"}},
        "cluster": {"enabled": True, "main_device": "main", "local_device": "main",
                    "auto_update": False, "main_url": "http://127.0.0.1:7780"}}


def _ssh_rsa(bits):
    """An ssh-rsa line whose modulus is `bits` long. Not a real key; the parser only counts bits."""
    modulus = (1 << (bits - 1)) | 1
    exponent = 65537

    def mpint(value):
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if raw[0] & 0x80:
            raw = b"\x00" + raw
        return struct.pack(">I", len(raw)) + raw

    blob = struct.pack(">I", 7) + b"ssh-rsa" + mpint(exponent) + mpint(modulus)
    return "ssh-rsa " + base64.b64encode(blob).decode("ascii")


class AuthorizedKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = Path(self.tmp.name) / "authorized_keys"
        self.keys.write_text("ssh-ed25519 AAAAexisting someone@else\n", encoding="utf-8")

    def test_ed25519_line_stays_restricted_and_drops_the_supplied_comment(self):
        cluster_setup.authorize("dell", KNOWN + " jarvis-node:evil", 7780, self.keys)
        line = self.keys.read_text(encoding="utf-8").splitlines()[1]
        self.assertTrue(line.startswith(
            'restrict,port-forwarding,permitopen="127.0.0.1:7780",'))
        self.assertIn('command="echo jarvis-node-link"', line)
        self.assertTrue(line.endswith(" jarvis-node:dell"))
        self.assertNotIn("evil", line)
        self.assertNotIn("\n", line)

    def test_refuses_injection_weak_keys_and_bad_ids(self):
        before = self.keys.read_text(encoding="utf-8")
        refused = (
            KNOWN + "\ncommand=\"evil\"",
            'command="evil",restrict ssh-ed25519 AAAA',
            "ecdsa-sha2-nistp256 AAAA",
            "ssh-ed25519 AAAA bad\"comment",
            _ssh_rsa(2048),
            KNOWN + " not a comment",
        )
        for key in refused:
            with self.subTest(key=key[:40]), self.assertRaises(ValueError):
                cluster_setup.authorize("dell", key, 7780, self.keys)
        for node in ("dell\ncommand=\"evil\"", 'dell"from="0.0.0.0"', "-dell", ""):
            with self.subTest(node=node), self.assertRaises(ValueError):
                cluster_setup.authorize(node, KNOWN, 7780, self.keys)
        with self.assertRaises(ValueError):
            cluster_setup.authorize("dell", KNOWN, True, self.keys)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), before)
        self.assertFalse(cluster_setup.authorized("dell\n", self.keys))
        with self.assertRaises(ValueError):
            cluster_setup.unauthorize("dell\n", self.keys)

    def test_accepts_sk_ed25519_and_rsa_3072(self):
        cluster_setup.authorize("sk", "sk-ed25519@openssh.com AAAA", 7780, self.keys)
        cluster_setup.authorize("rsa", _ssh_rsa(3072) + " jarvis-node:rsa", 7780, self.keys)
        text = self.keys.read_text(encoding="utf-8")
        self.assertIn(" sk-ed25519@openssh.com AAAA jarvis-node:sk", text)
        self.assertIn(" ssh-rsa ", text)
        self.assertTrue(cluster_setup.authorized("rsa", self.keys, public_key=_ssh_rsa(3072)))


class SshArgvTests(unittest.TestCase):
    def test_shared_options_and_double_dash(self):
        args = _ssh_args("ssh", "~/.ssh/jarvis_ed25519", timeout=5)
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=5",
                       "ForwardAgent=no", "ForwardX11=no",
                       "IdentitiesOnly=yes"):
            self.assertIn(option, args)
        self.assertNotIn("--", args)
        # ClearAllForwardings also clears the command-line -L/-R and would kill the tunnel.
        self.assertNotIn("ClearAllForwardings=yes", args)
        self.assertEqual(ssh_host("jdoe@192.0.2.1"), ["--", "jdoe@192.0.2.1"])
        for bad in ("-oProxyCommand=evil", "user@host;evil", "user@$(evil)", None, "jdoe@-bad"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ssh_host(bad)
        for path in ("-oProxyCommand=evil", "id\nfile", "id\x00file"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _ssh_args("ssh", path)

    def test_tunnel_keeps_its_forward_and_pins_the_host(self):
        from jarvis.cluster_tunnel import command
        with tempfile.TemporaryDirectory() as tmp, \
                patch("jarvis.cluster_tunnel.peer", return_value={"identity_file": "id_ed25519"}), \
                patch("jarvis.cluster_tunnel.shutil.which", return_value="ssh"), \
                patch("jarvis.cluster_tunnel.pinned_host", return_value=[]):
            args = command({"cluster": {"tunnel": "forward", "listen_port": 7780}}, tmp,
                           "jdoe@192.0.2.1")
        self.assertNotIn("ClearAllForwardings=yes", args)
        self.assertIn("-L", args)
        self.assertEqual(args[-2:], ["--", "jdoe@192.0.2.1"])
        self.assertIn("127.0.0.1:7780:127.0.0.1:7780", args)
        self.assertNotIn("0.0.0.0", args)

    def test_probe_ssh_returns_an_error_instead_of_running_ssh(self):
        with patch.object(probe, "tcp_ping", return_value=1.0), \
                patch.object(probe.shutil, "which", return_value="ssh"), \
                patch.object(probe.subprocess, "run") as run:
            out = probe.probe_ssh("-oProxyCommand=evil")
            again = probe.probe_ssh("jdoe@192.0.2.1", "id\nfile")
        run.assert_not_called()
        self.assertFalse(out["ssh"])
        self.assertIn("error", out)
        self.assertIn("error", again)


class KnownHostsTests(unittest.TestCase):
    def test_pinned_host_refuses_a_second_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _pinned_host({"hostname": "peer", "host_key": "ssh-ed25519 AAAA\nssh-rsa BBBB"}, tmp)
            with self.assertRaises(ValueError):
                _pinned_host({"hostname": "bad name", "host_key": "ssh-ed25519 AAAA"}, tmp)
            self.assertFalse((Path(tmp) / "known_hosts").exists())
            options = _pinned_host({"hostname": "peer", "host_key": "ssh-ed25519 AAAA"}, tmp)
            self.assertIn("HostKeyAlias=jarvis-peer", options)
            self.assertEqual((Path(tmp) / "known_hosts").read_text(encoding="ascii"),
                             "jarvis-peer ssh-ed25519 AAAA\n")


class BundleTests(unittest.TestCase):
    def test_names_and_symlinks(self):
        for name in ("jarvis/a\x00.py", "jarvis/a\n.py", "jarvis/../x.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                deploy._safe_name(name)
        info = zipfile.ZipInfo("jarvis/link.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assertTrue(deploy._zip_symlink(info))
        info.create_system = 0
        self.assertFalse(deploy._zip_symlink(info))

    def test_apply_bundle_rejects_a_unix_symlink(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            info = zipfile.ZipInfo("jarvis/link.py")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"../outside")
        bundle = {"code": "0" * 16, "zip": base64.b64encode(buf.getvalue()).decode("ascii"),
                  "signature": "ignored"}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("jarvis.deploy.verify", return_value=True):
            with self.assertRaisesRegex(ValueError, "Unsafe path"):
                deploy.apply_bundle(bundle, "ssh-ed25519 AAAA", Path(tmp))

    def test_package_stores_symlink_targets_as_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "jarvis"
            root.mkdir()
            real = Path(tmp) / "real.py"
            real.write_bytes(b"body\n")
            (root / "alias.py").symlink_to(real)
            raw = deploy.package(root)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            member = tar.getmember("jarvis/alias.py")
            self.assertFalse(member.issym() or member.islnk())
            self.assertEqual(tar.extractfile(member).read(), b"body\n")


class HubHardeningTests(unittest.TestCase):
    def test_hosts_are_loopback_bind_and_main_url_only(self):
        allowed = set(hub_hosts("0.0.0.0", {"cluster": {"main_url": "https://hub.example:8443"}}))
        self.assertEqual(allowed, {"0.0.0.0", "127.0.0.1", "localhost", "::1", "hub.example"})

    def test_owner_token_length_is_checked_before_compare(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Hub(MAIN, tmp, tmp)
            self.addCleanup(getattr(hub, "close", lambda: None))
            with self.assertRaises(PermissionError):
                hub.authenticate({"X-JARVIS-Main": "main", "Authorization": "Bearer short"})
            with self.assertRaises(PermissionError):
                hub.authenticate({"X-JARVIS-Main": "main",
                                  "Authorization": "Bearer " + ("a" * 500)})
            node, caps = hub.authenticate({"X-JARVIS-Main": "main",
                                           "Authorization": "Bearer " + hub.owner})
            self.assertEqual(node, "main")
            self.assertIn("owner", caps)
            mode = (Path(tmp) / "cluster.sqlite3").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_bad_host_and_bad_length_never_change_state(self):
        import http.client
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub = Hub(MAIN, tmp.name, tmp.name).start(port=0)
        self.addCleanup(hub.close)
        port = hub.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/status", headers={"Host": "evil.example"})
        refused = conn.getresponse()
        self.assertEqual(refused.status, 403)
        self.assertEqual(json.loads(refused.read())["error"], "Unexpected Host header.")
        conn.request("GET", "/v1/status", headers={
            "Host": f"127.0.0.1:{port}", "Sec-Fetch-Site": "cross-site"})
        cross = conn.getresponse()
        self.assertEqual(cross.status, 403)
        self.assertIn("other sites", json.loads(cross.read())["error"])
        conn.putrequest("POST", "/v1/poll", skip_host=1)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Authorization", "Bearer " + hub.owner)
        conn.putheader("X-JARVIS-Main", "main")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "+5")
        conn.endheaders()
        bad = conn.getresponse()
        self.assertEqual(bad.status, 400)
        self.assertIn("length", json.loads(bad.read())["error"])
        conn.close()


class GrokbotPathTests(unittest.TestCase):
    def test_tunnel_refuses_a_device_id_that_is_shell(self):
        from jarvis.grokbot import Grokbot
        import jarvis.grokbot as grokbot
        config = {"devices": {"zenbook": {"transport": "ssh", "target": "jdoe@10.0.0.2"}}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Grokbot, "here", return_value="dell;touch"), \
                patch.object(grokbot.subprocess, "run") as run, \
                patch.object(grokbot, "relay_server") as relay:
            with self.assertRaises(LookupError):
                Grokbot(config, tmp).tunnel(once=True)
        run.assert_not_called()
        relay.assert_not_called()


class StoreModeTests(unittest.TestCase):
    def test_database_is_private(self):
        if os.name == "nt":
            self.skipTest("mode bits")
        with tempfile.TemporaryDirectory() as tmp:
            Broker(tmp)
            self.assertEqual((Path(tmp) / "cluster.sqlite3").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
