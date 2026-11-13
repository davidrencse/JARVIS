"""Transfer, clipboard, drop zone, and Lan Mouse checks that stay inside temp dirs.

No wl-paste, xclip, PowerShell, SSH, or network. Home is patched the same way
tests.remote.test_transfer_cluster patches it.
"""
import base64
import ipaddress
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import clipsync, desktop, dropzone, sharedinput, transfer

FP = ":".join(["ab"] * 32)


def _put(upload, data, name="note.txt", offset=0, last=True, dest="drop"):
    return {"action": "put", "upload": upload, "offset": offset, "last": last,
            "data": base64.b64encode(data).decode(), "name": name, "dest": dest}


class HomeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.secret = self.home / "secret"
        self.secret.write_bytes(b"SECRET")

    def here(self, payload):
        with mock.patch.object(Path, "home", return_value=self.home):
            return transfer.run_here(payload)


class DropReceiveTests(HomeCase):
    def test_get_does_not_read_through_a_drop_symlink(self):
        drop = self.home / "JARVIS-Drop"
        drop.mkdir()
        (drop / "note.txt").symlink_to(self.secret)
        with self.assertRaises(ValueError):
            self.here({"action": "get", "name": "note.txt", "offset": 0})
        self.assertEqual(self.secret.read_bytes(), b"SECRET")
        self.assertTrue((drop / "note.txt").is_symlink())

    def test_get_still_reads_a_regular_file(self):
        drop = self.home / "JARVIS-Drop"
        drop.mkdir()
        (drop / "note.txt").write_bytes(b"hello")
        out = self.here({"action": "get", "name": "note.txt", "offset": 0})
        self.assertEqual(base64.b64decode(out["data"]), b"hello")

    def test_put_does_not_write_through_a_part_symlink(self):
        drop = self.home / "JARVIS-Drop"
        drop.mkdir()
        upload = "a" * 32
        part = drop / f".jarvis-upload-{upload}.part"
        part.symlink_to(self.secret)
        with self.assertRaises((OSError, ValueError)):
            self.here(_put(upload, b"pwn", last=False))
        self.assertEqual(self.secret.read_bytes(), b"SECRET")
        self.assertTrue(part.is_symlink())

    def test_continuation_unlinks_a_swapped_part_symlink_not_its_target(self):
        upload = "b" * 32
        self.here(_put(upload, b"abcd", last=False))
        drop = self.home / "JARVIS-Drop"
        part = drop / f".jarvis-upload-{upload}.part"
        self.assertEqual(part.read_bytes(), b"abcd")
        part.unlink()
        part.symlink_to(self.secret)
        with self.assertRaises(ValueError):
            self.here(_put(upload, b"zzzz", offset=4, last=False))
        self.assertEqual(self.secret.read_bytes(), b"SECRET")
        self.assertFalse(os.path.lexists(part))

    def test_traversal_name_lands_inside_the_drop_folder(self):
        upload = "c" * 32
        bashrc = self.home / ".bashrc"
        out = self.here(_put(upload, b"nope", name="../../.bashrc"))
        self.assertEqual(out["sent"], "bashrc")
        self.assertFalse(bashrc.exists())
        landed = self.home / "JARVIS-Drop" / "bashrc"
        self.assertEqual(landed.read_bytes(), b"nope")
        self.assertEqual(stat.S_IMODE(landed.stat().st_mode), 0o600)
        self.assertNotIn(":", transfer.safe_name("file::$DATA"))
        self.assertEqual(transfer.safe_name("../../.bashrc"), "bashrc")
        self.assertFalse(transfer.safe_name(".bashrc").startswith("."))

    def test_drop_folder_is_private_and_not_a_symlink(self):
        with mock.patch.object(Path, "home", return_value=self.home):
            folder = transfer.drop_folder()
        self.assertEqual(folder, self.home / "JARVIS-Drop")
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        real = self.home / "elsewhere"
        real.mkdir()
        # A symlink sitting where the drop folder should be is refused.
        drop = self.home / "JARVIS-Drop"
        drop.rename(real / "moved")
        drop.symlink_to(real)
        with mock.patch.object(Path, "home", return_value=self.home):
            with self.assertRaises(ValueError):
                transfer.drop_folder()
        self.assertTrue(drop.is_symlink())
        self.assertEqual(list(real.iterdir()), [real / "moved"])


class ScpNameTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src = Path(tmp.name) / "local.txt"
        self.src.write_bytes(b"x")
        self.calls = []

        def fake_run(args, timeout, **kwargs):
            self.calls.append(list(args))
            return mock.Mock(returncode=0, stdout="", stderr="")

        self.fake_run = fake_run

    def _send(self, name):
        with mock.patch.object(transfer, "_run", side_effect=self.fake_run), \
                mock.patch.object(transfer, "_tools", return_value=("/usr/bin/ssh", "/usr/bin/scp")):
            return transfer.send("jdoe@host", None, self.src, name=name)

    def test_command_parentheses_never_reach_scp(self):
        with self.assertRaises(ValueError):
            self._send("a (id).txt")
        self.assertTrue(self.calls)
        self.assertFalse(any(args[0].endswith("scp") for args in self.calls))

    def test_collision_name_stays_unquoted(self):
        out = self._send("shot (2).png")
        self.assertEqual(out["sent"], "shot (2).png")
        scp = [args for args in self.calls if args[0].endswith("scp")]
        self.assertEqual(len(scp), 1)
        self.assertEqual(scp[0][-1], "jdoe@host:JARVIS-Drop/shot (2).png")
        self.assertFalse(any(arg.startswith("-oProxyCommand") for arg in scp[0]))


class ClipboardAndDropTests(unittest.TestCase):
    def test_uri_lists_skip_parent_segments(self):
        self.assertEqual(clipsync.parse_uri_list(b"file:///tmp/../etc/passwd\n"), [])
        self.assertEqual(clipsync.parse_uri_list(b"file:///tmp/ok\n"), ["/tmp/ok"])
        data = (b"# Dolphin\r\nfile:///home/u/a%20b.txt\r\n"
                b"file://localhost/tmp/caf%C3%A9.txt\r\n"
                b"file:///tmp/../etc/passwd\nfile:///tmp/ok\n")
        self.assertEqual(dropzone.uri_paths(data),
                         ["/home/u/a b.txt", "/tmp/café.txt", "/tmp/ok"])

    def test_select_files_does_not_walk_parent_segments(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        secret = root / "secret.txt"
        secret.write_bytes(b"SECRET")
        selected, total = clipsync.select_files([f"{root}/sub/../secret.txt"])
        self.assertEqual((selected, total), ([], 0))
        self.assertEqual(secret.read_bytes(), b"SECRET")
        chosen, size = clipsync.select_files([str(secret)])
        self.assertEqual(chosen, [secret])
        self.assertEqual(size, len(b"SECRET"))


class LanMouseWriteTests(unittest.TestCase):
    def test_config_drops_tailscale_and_bounds_port_and_keys(self):
        text = desktop.lan_mouse_config("peer", ["172.1.1.1", "100.64.0.5"], "right", FP)
        self.assertIn("172.1.1.1", text)
        self.assertNotIn("100.64", text)
        self.assertIn(f'"{FP}"', text)
        with self.assertRaises(ValueError):
            desktop.lan_mouse_config("peer", ["100.64.0.11"], "right", FP)
        for port in (0, 70000, True, -1):
            with self.subTest(port=port), self.assertRaises(ValueError):
                desktop.lan_mouse_config("peer", ["172.1.1.1"], "right", FP, port=port)
        kept = desktop.lan_mouse_config("peer", ["172.1.1.1"], "right", FP, port=4242.0)
        self.assertIn("port = 4242\n", kept)
        with self.assertRaises(ValueError):
            desktop.lan_mouse_config("peer", ["172.1.1.1"], "right", FP, release_bind=['Key"'])

    def test_remote_fingerprint_rejects_an_option_target_before_ssh(self):
        with mock.patch("jarvis.desktop.subprocess.run") as run:
            with self.assertRaises(ValueError):
                desktop.remote_fingerprint("-oProxyCommand=x@y")
            run.assert_not_called()
        with mock.patch("jarvis.desktop.shutil.which", return_value="/usr/bin/ssh"), \
                mock.patch("jarvis.desktop.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=FP + "\n", stderr="")
            self.assertEqual(desktop.remote_fingerprint("jdoe@host"), FP)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/ssh")
        self.assertEqual(args[-2:], ["jdoe@host", "python3 -"])
        self.assertFalse(run.call_args.kwargs.get("shell"))

    def test_write_pairing_refuses_a_symlink_and_writes_mode_0600(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = root / "real.toml"
        target.write_text("ORIGINAL")
        link = root / "config.toml"
        link.symlink_to(target)
        with mock.patch.object(sharedinput, "lan_mouse_status", return_value={"running": False}):
            with self.assertRaises(ValueError):
                sharedinput.write_pairing("peer", ["172.1.1.1"], "right", FP, path=link)
        self.assertEqual(target.read_text(), "ORIGINAL")
        plain = root / "plain.toml"
        with mock.patch.object(sharedinput, "lan_mouse_status", return_value={"running": False}):
            sharedinput.write_pairing("peer", ["172.1.1.1"], "right", FP, path=plain)
        self.assertEqual(stat.S_IMODE(plain.stat().st_mode), 0o600)
        self.assertIn("172.1.1.1", plain.read_text(encoding="utf-8"))
        self.assertNotIn("100.64", plain.read_text(encoding="utf-8"))

    def test_install_copy_skips_symlinks_from_the_user_bin(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        src = root / "bin"
        home = root / "Program Files" / "lan-mouse"
        src.mkdir()
        home.mkdir(parents=True)
        evil = root / "evil"
        evil.write_bytes(b"EVIL")
        (src / "Lan-Mouse.EXE").write_bytes(b"REAL")
        (src / "ok.dll").write_bytes(b"DLL")
        (src / "bad.dll").symlink_to(evil)
        (src / "lan-mouse.exe").symlink_to(evil)
        sharedinput._install_copy(src / "Lan-Mouse.EXE", home)
        names = {p.name for p in home.iterdir()}
        self.assertEqual(names, {"Lan-Mouse.EXE", "ok.dll"})
        self.assertEqual((home / "Lan-Mouse.EXE").read_bytes(), b"REAL")
        self.assertEqual((home / "ok.dll").read_bytes(), b"DLL")
        self.assertEqual(evil.read_bytes(), b"EVIL")

    def test_peer_nearby_ignores_tailscale_configured_addresses(self):
        status = {"BackendState": "Running", "Peer": {"k": {
            "HostName": "peer", "Online": True, "CurAddr": "", "OS": "linux",
            "TailscaleIPs": [],
        }}}
        v6 = {"clients": [{"hostname": "peer", "ips": ["fd7a:115c:a1e0::1234"]}]}
        near = sharedinput.peer_nearby(
            v6, status=status, nets=[ipaddress.ip_network("fd7a:115c:a1e0::/48")])
        self.assertNotEqual(near["near"], True)
        v4 = {"clients": [{"hostname": "peer", "ips": ["100.64.0.5"]}]}
        near = sharedinput.peer_nearby(
            v4, status=status, nets=[ipaddress.ip_network("100.64.0.0/10")])
        self.assertNotEqual(near["near"], True)
        lan = {"clients": [{"hostname": "peer", "ips": ["172.1.1.5"]}]}
        near = sharedinput.peer_nearby(
            lan, status=status, nets=[ipaddress.ip_network("172.1.0.0/16")])
        self.assertTrue(near["near"])


if __name__ == "__main__":
    unittest.main()
