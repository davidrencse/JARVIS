"""Integrity fixes: brain tool allow-list, stop everywhere, release quiet period, upload
space/sweep, and the elevated Lan Mouse install folder."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import actions, control, release, sharedinput as si, transfer


class ToolAllowList(unittest.TestCase):
    def test_toolbox_refuses_undeclared_tool(self):
        from jarvis.vault import Toolbox
        remote = mock.Mock()
        remote.names.return_value = ["a", "b"]
        remote.here.return_value = "a"
        box = Toolbox(actions.DesktopTools(remote=remote))
        ok, _ = box.call("lan_mouse", {"device": "dell"})
        self.assertFalse(ok)
        remote.call.assert_not_called()

    def test_desktop_tools_refuse_undeclared_even_with_a_device(self):
        remote = mock.Mock()
        remote.names.return_value = ["a", "b"]
        remote.here.return_value = "a"
        tools = actions.DesktopTools(remote=remote)
        ok, text = tools.call("clipboard", {"device": "b"})
        self.assertFalse(ok)
        self.assertIn("Not a JARVIS tool", text)
        remote.call.assert_not_called()
        tools.call("open_app", {"device": "b", "name": "x"})
        remote.call.assert_called_once()


class StopEverywhere(unittest.TestCase):
    def remote(self, behave):
        r = mock.Mock()
        r.names.return_value = ["main", "dell", "slow", "down"]
        r.here.return_value = "main"
        r.call.side_effect = behave
        return r

    def test_local_stop_first_and_report(self):
        def behave(device, action, arg):
            if device == "down":
                raise RuntimeError("offline")
            if device == "slow":
                time.sleep(3)
            return True, "stopped"
        with tempfile.TemporaryDirectory() as tmp:
            start = time.time()
            ok, text = control.stop_all({}, tmp, timeout=0.3, remote=self.remote(behave))
            self.assertLess(time.time() - start, 2)
            self.assertTrue(ok)
            self.assertTrue(control._stopped(tmp))
        self.assertIn("dell", text.split("Not reached")[0])
        self.assertIn("slow", text.split("Not reached")[1])
        self.assertIn("down", text.split("Not reached")[1])

    def test_no_cluster_still_stops_here(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("jarvis.remote.Remote", side_effect=RuntimeError("no config")):
                ok, text = control.stop_all({}, tmp)
            self.assertTrue(ok)
            self.assertTrue(control._stopped(tmp))
            self.assertNotIn("Not reached", text)


class ReleaseQuiet(unittest.TestCase):
    def test_quiet_is_ten_minutes_with_a_fast_path(self):
        self.assertEqual(release.QUIET, 600)
        self.assertEqual(release.FAST_QUIET, 60)


class UploadSpace(unittest.TestCase):
    def payload(self, **kw):
        import base64
        return {"action": "put", "upload": "a" * 32, "offset": 0, "last": False,
                "data": base64.b64encode(b"hello").decode(), "name": "f.txt", **kw}

    def test_refuses_when_it_would_leave_under_2gb(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(transfer, "drop_folder", return_value=Path(tmp)), \
                mock.patch("shutil.disk_usage", return_value=mock.Mock(free=2 * 1024 ** 3 + 2)):
            with self.assertRaises(RuntimeError) as cm:
                transfer.run_here(self.payload(size=1024))
            self.assertIn("free space", str(cm.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_accepts_with_room_and_sweeps_old_parts(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(transfer, "drop_folder", return_value=Path(tmp)), \
                mock.patch("shutil.disk_usage", return_value=mock.Mock(free=50 * 1024 ** 3)):
            old = Path(tmp) / transfer.PART.format("b" * 32)
            old.write_bytes(b"x")
            ancient = time.time() - 25 * 3600
            os.utime(old, (ancient, ancient))
            fresh = Path(tmp) / transfer.PART.format("c" * 32)
            fresh.write_bytes(b"x")
            self.assertEqual(transfer.run_here(self.payload(size=5)), {"received": 5})
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())


class InstallFolder(unittest.TestCase):
    def test_install_dir_raises_off_windows(self):
        with mock.patch.object(si.os, "name", "posix"):
            with self.assertRaises(ValueError):
                si.install_dir()

    FOLDER = r"C:\Program Files\JARVIS Lan Mouse"
    SAFE = (FOLDER + " NT SERVICE\\TrustedInstaller:(F)\n"
            "                                   NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
            "                                   BUILTIN\\Administrators:(OI)(CI)(F)\n"
            "                                   BUILTIN\\Users:(OI)(CI)(RX)\n"
            "                                   NT AUTHORITY\\Authenticated Users:(OI)(CI)(RX)\n")

    def test_read_only_users_are_fine(self):
        self.assertEqual(si.writable_by_users(self.SAFE, self.FOLDER, "david"), [])

    def test_writable_users_are_found(self):
        out = self.SAFE + "                                   BUILTIN\\Users:(OI)(CI)(M)\n"
        self.assertEqual(si.writable_by_users(out, self.FOLDER, "david"), ["BUILTIN\\Users"])
        out = self.SAFE + "                                   PC\\David:(OI)(CI)(F)\n"
        self.assertEqual(si.writable_by_users(out, self.FOLDER, "David"), ["PC\\David"])
        out = self.FOLDER + " Everyone:(OI)(CI)(WD,AD)\n"
        self.assertEqual(si.writable_by_users(out, self.FOLDER, "david"), ["Everyone"])

    def test_deny_entries_are_ignored(self):
        out = self.SAFE + "                                   BUILTIN\\Users:(DENY)(W)\n"
        self.assertEqual(si.writable_by_users(out, self.FOLDER, "david"), [])

    def test_unsafe_folder_removes_the_tasks_and_raises(self):
        done = mock.Mock(returncode=0, stdout=self.SAFE + "Authenticated Users:(M)\n", stderr="")
        with mock.patch.object(si, "_run", return_value=done), \
                mock.patch.dict(os.environ, {"USERNAME": "david"}):
            with self.assertRaises(RuntimeError):
                si._verify_locked_down(Path(self.FOLDER))


if __name__ == "__main__":
    unittest.main()
