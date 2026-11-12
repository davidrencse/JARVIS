"""OS membership helpers in jarvis.platforms. Temp files only; no real SSH, services, or keys.

Windows key ACLs are applied to an in-memory model. This host does not run
Windows OpenSSH, icacls, or PowerShell.
"""

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import autostart, platforms

ROOT = Path(__file__).resolve().parents[2]
ADMIN_SID = "S-1-5-32-544"


def _completed(stdout="", stderr="", code=0, args=("cmd",)):
    return subprocess.CompletedProcess(list(args), code, stdout, stderr)


def _tracking_mkstemp(box):
    """mkstemp wrapper that records paths. Capture the real function before patching."""
    real = tempfile.mkstemp

    def track(*args, **kwargs):
        fd, name = real(*args, **kwargs)
        box.append(name)
        return fd, name

    return track


class _Ace:
    def __init__(self, identity, rights, inherited):
        self.identity = identity
        self.rights = rights
        self.inherited = bool(inherited)


class _Dacl:
    def __init__(self, aces, protected=False):
        self.aces = list(aces)
        self.protected = protected

    def copy(self):
        return _Dacl([_Ace(ace.identity, ace.rights, ace.inherited) for ace in self.aces], self.protected)


class WindowsAclModel:
    """In-memory DACL. icacls keeps other explicit ACEs; the helper's script does not.

    Operations match the Windows tools the helper used to call and the script it
    writes now. A command that only grants one account leaves every other
    explicit ACE in place. Set-Acl is the only disk commit, so a script that
    clears rules in memory and adds one account publishes that DACL once.
    """

    def __init__(self):
        self.files = {}
        self.commands = []
        self.commits = []
        self.script = ""

    def seed(self, path, rows):
        aces = [_Ace(identity, rights, inherited) for identity, rights, inherited in rows]
        self.files[os.fspath(path)] = _Dacl(aces, protected=False)

    def identities(self, path):
        return [ace.identity for ace in self.files[os.fspath(path)].aces]

    def __call__(self, args, timeout=None, **kwargs):
        if kwargs.get("shell"):
            raise AssertionError("ACL tool was started with a shell")
        if not isinstance(args, (list, tuple)):
            raise AssertionError("ACL tool arguments are not a list")
        args = list(args)
        self.commands.append(args)
        if args[0] == "icacls":
            self._apply_icacls(args)
        elif args[0] == "powershell.exe":
            self._apply_powershell(args)
        else:
            raise AssertionError(args[0])
        return _completed(code=0, args=args)

    def _commit(self, path, dacl):
        self.files[path] = dacl.copy()
        self.commits.append(tuple(ace.identity for ace in dacl.aces))

    def _apply_icacls(self, args):
        """``/inheritance:r`` drops inherited ACEs. ``/grant:r`` replaces one identity."""
        path = args[1]
        dacl = self.files[path].copy()
        index = 2
        while index < len(args):
            token = args[index]
            if token == "/inheritance:r":
                dacl.protected = True
                dacl.aces = [ace for ace in dacl.aces if not ace.inherited]
            elif token == "/grant:r":
                index += 1
                identity, sep, rights = args[index].rpartition(":")
                if sep != ":":
                    raise AssertionError(args[index])
                dacl.aces = [ace for ace in dacl.aces if ace.identity != identity]
                dacl.aces.append(_Ace(identity, rights, False))
            elif token == "/reset":
                # Parent ACL, the broad window /reset writes before a later tighten.
                dacl = _Dacl([
                    _Ace(r"NT AUTHORITY\SYSTEM", "F", True),
                    _Ace(r"BUILTIN\Administrators", "F", True),
                    _Ace(r"BUILTIN\Users", "RX", True),
                ], protected=False)
            else:
                raise AssertionError(token)
            index += 1
        self._commit(path, dacl)

    def _apply_powershell(self, args):
        if "-Command" in args or "-EncodedCommand" in args:
            raise AssertionError("path or account was composed into a PowerShell command")
        file_at = args.index("-File")
        script_path, path, account = args[file_at + 1:file_at + 4]
        if args[file_at + 4:]:
            raise AssertionError("extra PowerShell arguments")
        script = Path(script_path).read_text(encoding="ascii")
        self.script = script
        self._apply_script(path, script, account)

    def _apply_script(self, path, script, account):
        """Walk the script in order. Only Set-Acl commits the working DACL."""
        working = None
        pending = None
        for raw in script.splitlines():
            line = raw.strip()
            if not line:
                continue
            if "Get-Acl" in line and "-LiteralPath" in line and "$path" in line:
                working = self.files[path].copy()
            elif "SetAccessRuleProtection($true, $false)" in line:
                if working is None:
                    raise AssertionError("protection changed before the descriptor was loaded")
                working.protected = True
                working.aces = [ace for ace in working.aces if not ace.inherited]
            elif "PurgeAccessRules" in line and "@($acl.Access)" in line:
                if working is None:
                    raise AssertionError("rules purged before the descriptor was loaded")
                working.aces = []
            elif "FileSystemAccessRule($account" in line:
                if working is None:
                    raise AssertionError("rule built before the descriptor was loaded")
                pending = _Ace(account, "FullControl", False)
            elif "AddAccessRule" in line:
                if working is None or pending is None:
                    raise AssertionError("access rule added without the account argument")
                working.aces.append(pending)
                pending = None
            elif line.startswith("Set-Acl"):
                if working is None or not working.protected:
                    raise AssertionError("ACL committed without disabling inheritance")
                self._commit(path, working)
                working = self.files[path].copy()


def _lock_is_busy(path):
    """True when another handle already holds the OS lock on ``path``."""
    if os.name == "nt":
        import msvcrt
        handle = open(path, "a+b", buffering=0)
        try:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        finally:
            handle.close()
    import fcntl
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class MembershipPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_host_keys_dir_windows_uses_programdata(self):
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(os.environ, {"PROGRAMDATA": str(self.tmp)}):
            self.assertEqual(platforms.host_keys_dir(), self.tmp / "ssh")
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROGRAMDATA", None)
            self.assertEqual(platforms.host_keys_dir(), Path(r"C:\ProgramData") / "ssh")

    def test_host_keys_dir_posix_is_etc_ssh(self):
        for name in ("linux", "macos"):
            with self.subTest(os=name), mock.patch.object(platforms, "_os", return_value=name):
                self.assertEqual(platforms.host_keys_dir(), Path("/etc/ssh"))

    def test_ssh_admin_account_is_false_off_windows(self):
        for name in ("linux", "macos"):
            with self.subTest(os=name), \
                    mock.patch.object(platforms, "_os", return_value=name), \
                    mock.patch.object(platforms, "run_quiet") as run, \
                    mock.patch.object(platforms, "_windows_process_is_admin") as token:
                self.assertFalse(platforms.ssh_admin_account())
            run.assert_not_called()
            token.assert_not_called()

    def test_ssh_admin_account_reads_the_administrators_group(self):
        groups = f'"BUILTIN\\Administrators","Alias","{ADMIN_SID}","Group used for deny only"\n'
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.object(platforms, "run_quiet", return_value=_completed(groups)) as run, \
                mock.patch.object(platforms, "_windows_process_is_admin") as token:
            self.assertTrue(platforms.ssh_admin_account())
        run.assert_called_once_with(["whoami", "/groups", "/fo", "csv", "/nh"], timeout=10)
        token.assert_not_called()

    def test_ssh_admin_account_uses_the_process_token_when_the_group_is_absent(self):
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.object(platforms, "run_quiet", return_value=_completed(stderr=ADMIN_SID)), \
                mock.patch.object(platforms, "_windows_process_is_admin", return_value=True) as token:
            self.assertTrue(platforms.ssh_admin_account())
        token.assert_called_once_with()
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.object(platforms, "run_quiet", side_effect=OSError("whoami missing")), \
                mock.patch.object(platforms, "_windows_process_is_admin", return_value=False) as token:
            self.assertFalse(platforms.ssh_admin_account())
        token.assert_called_once_with()

    def test_authorized_keys_path_follows_os_and_admin(self):
        home = self.tmp / "home"
        with mock.patch.object(platforms.Path, "home", return_value=home):
            with mock.patch.object(platforms, "_os", return_value="windows"), \
                    mock.patch.object(platforms, "ssh_admin_account", return_value=True), \
                    mock.patch.dict(os.environ, {"PROGRAMDATA": str(self.tmp)}):
                self.assertEqual(platforms.ssh_authorized_keys_path(),
                                 self.tmp / "ssh" / "administrators_authorized_keys")
            with mock.patch.object(platforms, "_os", return_value="windows"), \
                    mock.patch.object(platforms, "ssh_admin_account", return_value=False) as admin:
                self.assertEqual(platforms.ssh_authorized_keys_path(), home / ".ssh" / "authorized_keys")
            admin.assert_called_once_with()
            for name in ("linux", "macos"):
                with self.subTest(os=name), mock.patch.object(platforms, "_os", return_value=name), \
                        mock.patch.object(platforms, "ssh_admin_account") as admin:
                    self.assertEqual(platforms.ssh_authorized_keys_path(), home / ".ssh" / "authorized_keys")
                admin.assert_not_called()


class PrivateKeyPermissions(unittest.TestCase):
    """Windows DACL behavior is checked with WindowsAclModel, not on Windows.

    Actual Windows verification is unavailable on this host. icacls, PowerShell
    Set-Acl, and OpenSSH's private-key permission check do not run here. The
    model applies the helper's script to an in-memory DACL. A pass is not a
    Windows kernel or OpenSSH acceptance result.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.key = self.tmp / "cluster-node.key"
        self.key.write_text("secret\n", encoding="ascii")
        os.chmod(self.key, 0o644)

    def _seed(self, model):
        model.seed(self.key, [
            (r"HOME\jdoe", "FullControl", False),
            (r"HOME\other", "Read", False),
            (r"BUILTIN\Users", "ReadAndExecute", True),
            (r"NT AUTHORITY\SYSTEM", "FullControl", False),
            (r"BUILTIN\Administrators", "FullControl", False),
        ])

    def test_posix_mode_is_0600(self):
        for name in ("linux", "macos"):
            os.chmod(self.key, 0o644)
            with self.subTest(os=name), mock.patch.object(platforms, "_os", return_value=name), \
                    mock.patch.object(platforms, "run_quiet") as run:
                self.assertEqual(platforms.private_key_permissions(self.key), self.key)
            run.assert_not_called()
            self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)

    def test_windows_acl_tool_is_an_argument_list_and_status_is_checked(self):
        made = []
        seen = {}

        def run(args, timeout=None, **kwargs):
            seen["args"] = list(args)
            seen["timeout"] = timeout
            seen["kwargs"] = kwargs
            seen["script"] = Path(args[args.index("-File") + 1]).read_text(encoding="ascii")
            return _completed(code=0, args=args)

        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(os.environ, {"USERNAME": "jdoe", "USERDOMAIN": "HOME"}), \
                mock.patch.object(platforms.os, "chmod") as chmod, \
                mock.patch.object(platforms.tempfile, "mkstemp", side_effect=_tracking_mkstemp(made)), \
                mock.patch.object(platforms, "run_quiet", side_effect=run):
            self.assertEqual(platforms.private_key_permissions(self.key), self.key)
        command = seen["args"]
        self.assertEqual(command[:8], [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-InputFormat", "None", "-ExecutionPolicy", "Bypass", "-File",
        ])
        self.assertEqual(command[-2:], [str(self.key), r"HOME\jdoe"])
        self.assertIsInstance(command, list)
        self.assertNotIn("shell", seen["kwargs"])
        self.assertIs(seen["kwargs"].get("stdin"), subprocess.DEVNULL)
        self.assertEqual(seen["timeout"], 30)
        self.assertEqual(seen["script"], platforms._WINDOWS_PRIVATE_KEY_ACL)
        self.assertNotIn(str(self.key), seen["script"])
        self.assertNotIn(r"HOME\jdoe", seen["script"])
        self.assertNotIn("-Command", command)
        self.assertNotIn("icacls", command)
        chmod.assert_not_called()
        self.assertTrue(made)
        self.assertTrue(all(not os.path.exists(name) for name in made))
        self.assertEqual(self.key.read_text(encoding="ascii"), "secret\n")

        denied_made = []
        denied_command = {}

        def deny(args, timeout=None, **kwargs):
            denied_command["cmd"] = list(args)
            return _completed(stdout="", stderr="Access is denied", code=5, args=args)

        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(os.environ, {"USERNAME": "jdoe", "USERDOMAIN": "HOME"}), \
                mock.patch.object(platforms.tempfile, "mkstemp", side_effect=_tracking_mkstemp(denied_made)), \
                mock.patch.object(platforms, "run_quiet", side_effect=deny):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                platforms.private_key_permissions(self.key)
        self.assertEqual(caught.exception.returncode, 5)
        self.assertEqual(list(caught.exception.cmd), denied_command["cmd"])
        self.assertEqual(caught.exception.cmd[-1], r"HOME\jdoe")
        self.assertIn("Access is denied", caught.exception.stderr)
        self.assertTrue(all(not os.path.exists(name) for name in denied_made))

    def test_windows_grant_omits_an_empty_domain(self):
        env = os.environ.copy()
        env["USERNAME"] = "jdoe"
        env.pop("USERDOMAIN", None)
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(platforms, "run_quiet", return_value=_completed()) as run:
            platforms.private_key_permissions(self.key)
        self.assertEqual(run.call_args.args[0][-1], "jdoe")
        self.assertNotIn("jdoe:F", run.call_args.args[0])

    def test_windows_missing_username_does_not_touch_the_acl(self):
        env = os.environ.copy()
        env.pop("USERNAME", None)
        env.pop("USERDOMAIN", None)
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(platforms, "run_quiet") as run, \
                mock.patch.object(platforms.tempfile, "mkstemp") as mkstemp:
            with self.assertRaises(ValueError):
                platforms.private_key_permissions(self.key)
        run.assert_not_called()
        mkstemp.assert_not_called()

    def test_icacls_grant_keeps_another_accounts_explicit_ace(self):
        model = WindowsAclModel()
        self._seed(model)
        model([
            "icacls", str(self.key), "/inheritance:r", "/grant:r", r"HOME\jdoe:F",
        ])
        self.assertEqual(set(model.identities(self.key)), {
            r"HOME\jdoe", r"HOME\other", r"NT AUTHORITY\SYSTEM", r"BUILTIN\Administrators",
        })
        self.assertNotIn(r"BUILTIN\Users", model.identities(self.key))
        self.assertEqual(len(model.commits), 1)

    def test_windows_exact_dacl_drops_a_second_users_explicit_grant(self):
        model = WindowsAclModel()
        self._seed(model)
        made = []
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(os.environ, {"USERNAME": "jdoe", "USERDOMAIN": "HOME"}), \
                mock.patch.object(platforms.os, "chmod") as chmod, \
                mock.patch.object(platforms.tempfile, "mkstemp", side_effect=_tracking_mkstemp(made)), \
                mock.patch.object(platforms, "run_quiet", side_effect=model):
            self.assertEqual(platforms.private_key_permissions(self.key), self.key)
        self.assertEqual(
            [(ace.identity, ace.rights, ace.inherited) for ace in model.files[str(self.key)].aces],
            [(r"HOME\jdoe", "FullControl", False)])
        self.assertNotIn(r"HOME\other", model.identities(self.key))
        self.assertNotIn(r"NT AUTHORITY\SYSTEM", model.identities(self.key))
        self.assertNotIn(r"BUILTIN\Administrators", model.identities(self.key))
        self.assertNotIn(r"BUILTIN\Users", model.identities(self.key))
        self.assertTrue(model.files[str(self.key)].protected)
        self.assertEqual(model.commits, [(r"HOME\jdoe",)])
        self.assertEqual(model.script, platforms._WINDOWS_PRIVATE_KEY_ACL)
        self.assertNotIn(str(self.key), model.script)
        self.assertNotIn(r"HOME\jdoe", model.script)
        self.assertNotIn(r"HOME\other", model.script)
        self.assertEqual(model.commands[0][0], "powershell.exe")
        self.assertNotIn("icacls", model.commands[0])
        chmod.assert_not_called()
        self.assertTrue(made)
        self.assertTrue(all(not os.path.exists(name) for name in made))

    def test_model_keeps_a_second_grant_when_the_script_does_not_clear_it(self):
        model = WindowsAclModel()
        self._seed(model)
        # Inheritance off and one grant, with no purge of the ACEs already loaded.
        weak = "\n".join([
            "$acl = Get-Acl -LiteralPath $path",
            "$acl.SetAccessRuleProtection($true, $false)",
            "$grant = New-Object System.Security.AccessControl.FileSystemAccessRule($account, 'FullControl', 'Allow')",
            "[void]$acl.AddAccessRule($grant)",
            "Set-Acl -LiteralPath $path -AclObject $acl",
            "",
        ])
        model._apply_script(str(self.key), weak, r"HOME\jdoe")
        self.assertIn(r"HOME\other", model.identities(self.key))
        self.assertIn(r"NT AUTHORITY\SYSTEM", model.identities(self.key))
        self.assertNotIn(r"BUILTIN\Users", model.identities(self.key))
        self.assertEqual(len(model.commits), 1)


class LockedSshKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _keys(self, name="authorized_keys"):
        return self.tmp / name

    def test_lock_file_gains_one_byte_and_a_failure_releases_it(self):
        path = self._keys()
        with self.assertRaises(RuntimeError):
            with platforms.locked_ssh_keys(path):
                lock = platforms._ssh_keys_lock_path(path)
                self.assertGreaterEqual(lock.stat().st_size, 1)
                self.assertTrue(_lock_is_busy(lock))
                raise RuntimeError("stop")
        size = platforms._ssh_keys_lock_path(path).stat().st_size
        with platforms.locked_ssh_keys(path):
            self.assertTrue(_lock_is_busy(platforms._ssh_keys_lock_path(path)))
        self.assertEqual(platforms._ssh_keys_lock_path(path).stat().st_size, size)

    def test_threads_on_one_file_cannot_overlap(self):
        path = self._keys()
        counter = self.tmp / "counter"
        counter.write_text("0", encoding="ascii")
        barrier = threading.Barrier(4, timeout=0.4)
        errors = []

        def bump():
            try:
                with platforms.locked_ssh_keys(path):
                    value = int(counter.read_text(encoding="ascii"))
                    try:
                        barrier.wait(0.4)
                    except threading.BrokenBarrierError:
                        pass
                    counter.write_text(str(value + 1), encoding="ascii")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(counter.read_text(encoding="ascii"), "4")

    def test_thread_lock_is_shared_across_paths(self):
        first, second = self._keys("a"), self._keys("b")
        barrier = threading.Barrier(2, timeout=0.4)
        overlapped = []

        def hold(path):
            with platforms.locked_ssh_keys(path):
                try:
                    barrier.wait(0.4)
                except threading.BrokenBarrierError:
                    return
                overlapped.append(path.name)

        threads = [threading.Thread(target=hold, args=(path,)) for path in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(overlapped, [])

    def test_processes_block_on_the_local_os_lock(self):
        path = self._keys()
        ready = self.tmp / "ready"
        held = self.tmp / "held"
        free = self.tmp / "free"
        child = r"""
import os, sys
from pathlib import Path
os.environ["JARVIS_NO_MODELS"] = "1"
sys.path.insert(0, sys.argv[1])
from jarvis.platforms import locked_ssh_keys, _ssh_keys_lock_path

def busy(lock):
    if os.name == "nt":
        import msvcrt
        handle = open(lock, "a+b", buffering=0)
        try:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        finally:
            handle.close()
    import fcntl
    fd = os.open(lock, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)

root, keys, ready, held, free = sys.argv[1:]
path = Path(keys)
lock = _ssh_keys_lock_path(path)
if not busy(lock):
    Path(free).write_text("free", encoding="ascii")
    sys.exit(3)
Path(ready).write_text("ready", encoding="ascii")
with locked_ssh_keys(path):
    Path(held).write_text("held", encoding="ascii")
"""
        env = os.environ.copy()
        env["JARVIS_NO_MODELS"] = "1"
        proc = None
        try:
            with platforms.locked_ssh_keys(path):
                lock = platforms._ssh_keys_lock_path(path)
                self.assertTrue(_lock_is_busy(lock))
                proc = subprocess.Popen(
                    [sys.executable, "-c", child, str(ROOT), str(path), str(ready), str(held), str(free)],
                    cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                deadline = time.monotonic() + 15
                while not ready.exists() and not free.exists() and proc.poll() is None:
                    if time.monotonic() > deadline:
                        break
                    time.sleep(0.02)
                if free.exists() or (proc.poll() is not None and not ready.exists()):
                    out, err = proc.communicate()
                    self.fail(f"child did not observe the held lock\n{out}\n{err}")
                deadline = time.monotonic() + 0.8
                while time.monotonic() < deadline:
                    if held.exists() or proc.poll() is not None:
                        out, err = proc.communicate()
                        self.fail(f"child entered the lock while this process held it\n{out}\n{err}")
                    time.sleep(0.02)
                self.assertFalse(held.exists())
                self.assertIsNone(proc.poll())
            code = proc.wait(timeout=10)
            out, err = proc.communicate()
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        self.assertEqual(code, 0, err)
        self.assertEqual(held.read_text(encoding="ascii"), "held")
        self.assertFalse(free.exists())

    def test_windows_lock_retries_a_busy_file_and_does_not_unlock(self):
        path = self._keys()
        calls = []

        def locking(fd, mode, nbytes):
            self.assertGreaterEqual(os.fstat(fd).st_size, 1)
            self.assertEqual(nbytes, 1)
            calls.append((fd, mode))
            if len(calls) == 1:
                raise OSError(errno.EACCES, "busy")

        fake = mock.Mock()
        fake.LK_LOCK = 1
        fake.LK_UNLCK = 2
        fake.locking.side_effect = locking
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(sys.modules, {"msvcrt": fake}):
            with platforms.locked_ssh_keys(path):
                self.assertEqual(len(calls), 2)
                os.fstat(calls[-1][0])  # still open for the whole body
        self.assertEqual([mode for _, mode in calls], [fake.LK_LOCK, fake.LK_LOCK])
        fake.locking.assert_called()
        with self.assertRaises(OSError):
            os.fstat(calls[-1][0])  # close released the lock; no LK_UNLCK first

        modes = []

        def refuse(fd, mode, nbytes):
            modes.append(mode)
            raise OSError(errno.EBADF, "bad")

        fake.locking.side_effect = refuse
        with mock.patch.object(platforms, "_os", return_value="windows"), \
                mock.patch.dict(sys.modules, {"msvcrt": fake}):
            with self.assertRaises(OSError) as caught:
                with platforms.locked_ssh_keys(path):
                    self.fail("lock was not acquired")
        self.assertEqual(caught.exception.errno, errno.EBADF)
        self.assertEqual(modes, [fake.LK_LOCK])


class SpawnDetached(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_posix_starts_a_new_session_without_a_shell(self):
        argv = ["/usr/bin/python", "-m", "jarvis", "cluster", "run"]
        with mock.patch.object(platforms, "on_windows", return_value=False), \
                mock.patch.object(platforms.subprocess, "Popen", return_value="child") as popen:
            self.assertEqual(platforms.spawn_detached(argv, state=self.tmp), "child")
        popen.assert_called_once_with(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.assertEqual(list(self.tmp.glob("cluster-node-launch*")), [])
        self.assertEqual(list(self.tmp.glob(platforms.DETACHED_LAUNCH_GLOB)), [])

    def test_windows_publishes_a_unique_launcher_before_explorer(self):
        argv = [r"C:\Program Files\pythonw.exe", "-m", "jarvis", "--state", r'C:\state\odd "dir"']
        line = subprocess.list2cmdline(argv).replace('"', '""')
        expected = autostart.vbs(line)
        events = []
        real_replace = os.replace

        def spy_replace(src, dst):
            text = Path(src).read_text(encoding="utf-8")
            events.append(("replace", os.fspath(dst), text, os.fspath(src)))
            return real_replace(src, dst)

        def spy_popen(args, **kwargs):
            self.assertEqual(kwargs.get("creationflags"), platforms.NO_WINDOW)
            self.assertNotIn("shell", kwargs)
            text = Path(args[1]).read_text(encoding="utf-8")
            events.append(("explorer", args[1], text))
            return "child"

        with mock.patch.object(platforms, "on_windows", return_value=True), \
                mock.patch.object(platforms.os, "replace", side_effect=spy_replace), \
                mock.patch.object(platforms.subprocess, "Popen", side_effect=spy_popen):
            self.assertEqual(platforms.spawn_detached(argv, state=self.tmp), "child")
        self.assertEqual([kind for kind, *_ in events], ["replace", "explorer"])
        published, explorer = events
        self.assertEqual(published[1], explorer[1])
        self.assertEqual(published[2], expected)
        self.assertEqual(explorer[2], expected)
        self.assertNotEqual(published[3], published[1])
        self.assertTrue(Path(published[3]).name.startswith(".cluster-node-launch-"))
        script = Path(explorer[1])
        self.assertTrue(script.match(platforms.DETACHED_LAUNCH_GLOB))
        self.assertNotEqual(script.name, "cluster-node-launch.vbs")
        self.assertFalse((self.tmp / "cluster-node-launch.vbs").exists())
        self.assertEqual(script.read_text(encoding="utf-8"), expected)
        self.assertEqual(list(self.tmp.glob(".cluster-node-launch-*.tmp")), [])
        self.assertEqual(list(self.tmp.glob(platforms.DETACHED_LAUNCH_GLOB)), [script])

    def test_windows_rejects_cr_and_lf_before_any_write_or_popen(self):
        payloads = (
            ("lf", ["python", "ok\nWScript.Echo 1"]),
            ("cr", ["python", "ok\rWScript.Echo 1"]),
            ("crlf", ["python", "ok\r\nWScript.Echo 1"]),
            ("lf-bytes", [b"python", b"ok\nWScript.Echo 1"]),
        )
        for label, argv in payloads:
            with self.subTest(which=label), \
                    mock.patch.object(platforms, "on_windows", return_value=True), \
                    mock.patch.object(platforms.tempfile, "mkstemp") as mkstemp, \
                    mock.patch.object(platforms.os, "replace") as replace, \
                    mock.patch.object(platforms.subprocess, "Popen") as popen:
                with self.assertRaises(ValueError) as caught:
                    platforms.spawn_detached(argv, state=self.tmp)
            self.assertIn("line break", str(caught.exception))
            mkstemp.assert_not_called()
            replace.assert_not_called()
            popen.assert_not_called()
            self.assertEqual(list(self.tmp.glob("cluster-node-launch*")), [])
            self.assertEqual(list(self.tmp.glob("*.vbs")), [])

    def test_windows_concurrent_launches_do_not_cross_commands(self):
        argvs = [[r"C:\pythonw.exe", "-m", "jarvis", f"marker{i}"] for i in range(8)]
        opened = []
        lock = threading.Lock()
        barrier = threading.Barrier(len(argvs), timeout=5)
        errors = []

        def popen(args, **kwargs):
            barrier.wait()
            script = Path(args[1])
            text = script.read_text(encoding="utf-8")
            with lock:
                opened.append((list(args), text))
            return "child"

        def launch(argv):
            try:
                platforms.spawn_detached(argv, state=self.tmp)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(platforms, "on_windows", return_value=True), \
                mock.patch.object(platforms.subprocess, "Popen", side_effect=popen):
            threads = [threading.Thread(target=launch, args=(argv,)) for argv in argvs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(opened), len(argvs))
        paths = [args[1] for args, _text in opened]
        self.assertEqual(len(set(paths)), len(argvs))
        self.assertFalse((self.tmp / "cluster-node-launch.vbs").exists())
        self.assertEqual(list(self.tmp.glob(".cluster-node-launch-*.tmp")), [])
        scripts = list(self.tmp.glob(platforms.DETACHED_LAUNCH_GLOB))
        self.assertEqual(len(scripts), len(argvs))
        for argv in argvs:
            line = subprocess.list2cmdline(argv).replace('"', '""')
            expected = autostart.vbs(line)
            matches = [text for _args, text in opened if text == expected]
            self.assertEqual(matches, [expected])
            marker = argv[-1]
            for other in argvs:
                if other[-1] != marker:
                    self.assertNotIn(other[-1], matches[0])
        for args, text in opened:
            self.assertEqual(args[0], "explorer.exe")
            script = Path(args[1])
            self.assertTrue(script.match(platforms.DETACHED_LAUNCH_GLOB))
            self.assertEqual(script.read_text(encoding="utf-8"), text)

    def test_windows_requires_state_and_a_string_is_not_a_command(self):
        with mock.patch.object(platforms, "on_windows", return_value=True), \
                mock.patch.object(platforms.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                platforms.spawn_detached(["python", "-m", "jarvis"], state=None)
            with self.assertRaises(TypeError):
                platforms.spawn_detached("python -m jarvis", state=self.tmp)
        popen.assert_not_called()
        self.assertEqual(list(self.tmp.glob("cluster-node-launch*")), [])
        self.assertEqual(list(self.tmp.glob(platforms.DETACHED_LAUNCH_GLOB)), [])


if __name__ == "__main__":
    unittest.main()
