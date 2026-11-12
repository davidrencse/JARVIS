"""What computer is this? OS, version, distro and desktop session, detected, never assumed.

Every place JARVIS describes or drives "this machine" asks here instead of assuming the
main PC runs Windows or the node runs Arch. The main and the nodes can each be Windows,
macOS or any Linux distribution; what differs is which desktop backend acts on them.

    detect() -> {"os": "linux", "name": "Arch Linux", "version": "rolling",
                 "desktop": "hyprland", "label": "Arch Linux (Hyprland)"}

os is one of windows, macos, linux. desktop is the backend family the action code uses:
windows, macos, hyprland, sway, gnome, kde, x11, wayland (another Wayland compositor) or
none (a headless session, e.g. reached over SSH with no desktop to borrow).
"""

import errno
import functools
import os
import platform
import secrets
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

# creationflags for a child process that must not flash a console window (0 off Windows).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_quiet(args, timeout=5, **kw):
    """Argument-list subprocess (never a shell), text out, no console window on Windows."""
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW, **kw)


def systemctl_user(*args, timeout=30):
    """`systemctl --user ...` -> (returncode, stdout+stderr); never raises."""
    try:
        out = run_quiet(["systemctl", "--user", *args], timeout)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _os():
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _windows_name():
    release, version = platform.release(), platform.version()
    try:
        build = int(version.split(".")[-1])
    except (ValueError, IndexError):
        build = 0
    # Windows 11 still reports release "10"; build 22000+ is 11.
    return ("Windows 11" if release == "10" and build >= 22000 else f"Windows {release}"), version


def _macos_name():
    version = platform.mac_ver()[0] or ""
    return "macOS", version


def _linux_name(os_release=Path("/etc/os-release")):
    fields = {}
    try:
        for line in os_release.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value.strip().strip('"')
    except OSError:
        pass
    name = fields.get("NAME") or "Linux"
    version = fields.get("VERSION_ID") or fields.get("BUILD_ID") or ""
    return name, version


def desktop(env=None, system=None):
    """The desktop backend family for this session (see module docstring)."""
    env = os.environ if env is None else env
    system = system or _os()
    if system in ("windows", "macos"):
        return system
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if env.get("SWAYSOCK"):
        return "sway"
    current = (env.get("XDG_CURRENT_DESKTOP") or env.get("DESKTOP_SESSION") or "").lower()
    if "gnome" in current or "ubuntu" in current or "pop" in current:
        return "gnome"
    if "kde" in current or "plasma" in current:
        return "kde"
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "none"


@functools.lru_cache(maxsize=1)
def detect():
    """This computer, detected once per process."""
    system = _os()
    name, version = {"windows": _windows_name, "macos": _macos_name}.get(system, _linux_name)()
    session = desktop(system=system)
    if system == "linux" and session == "none":
        # Started over SSH or as a service: borrow the logged-in desktop's session, if any.
        try:
            from .remote import session_env
            session = desktop({**os.environ, **session_env()}, system) or "none"
        except Exception:
            pass
    label = name if system != "linux" else (
        f"{name} ({session.capitalize() if session not in ('x11', 'none') else session.upper() if session == 'x11' else 'no desktop'})")
    return {"os": system, "name": name, "version": version, "desktop": session, "label": label}


def label():
    return detect()["label"]


def on_windows():
    """Cheap OS test that reads os.name at call time (tests patch it); detect() is heavier."""
    return os.name == "nt"


def on_macos():
    return sys.platform == "darwin"


def on_linux():
    return sys.platform.startswith("linux")


def is_windows():
    return detect()["os"] == "windows"


def private_dir(path):
    """Make JARVIS's state folder (secrets, tokens, memory, voiceprint) readable by this
    account only. On Linux and macOS that's mode 700 on the folder, which closes every file
    inside to other local accounts whatever their own modes. Windows keeps it under the
    user's profile, whose ACL already does this."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if _os() != "windows" and path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


FILETIME_EPOCH = 116444736000000000  # 1601-01-01 to 1970-01-01, in 100 ns ticks


def _windows_logon_time():
    """Unix seconds when the console desktop session logged on, or None. Fixed for the whole
    login: node-agent restarts (even from an SSH shell, whose own logon session is newer)
    don't move it."""
    import ctypes
    from ctypes import wintypes

    class WTSINFOW(ctypes.Structure):
        _fields_ = [("State", ctypes.c_int), ("SessionId", wintypes.DWORD),
                    ("IncomingBytes", wintypes.DWORD), ("OutgoingBytes", wintypes.DWORD),
                    ("IncomingFrames", wintypes.DWORD), ("OutgoingFrames", wintypes.DWORD),
                    ("IncomingCompressedBytes", wintypes.DWORD),
                    ("OutgoingCompressedBytes", wintypes.DWORD),
                    ("WinStationName", ctypes.c_wchar * 32), ("Domain", ctypes.c_wchar * 17),
                    ("UserName", ctypes.c_wchar * 21), ("ConnectTime", ctypes.c_longlong),
                    ("DisconnectTime", ctypes.c_longlong), ("LastInputTime", ctypes.c_longlong),
                    ("LogonTime", ctypes.c_longlong), ("CurrentTime", ctypes.c_longlong)]

    active = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId
    active.restype = wintypes.DWORD  # the default c_int would read "none" (0xFFFFFFFF) as -1
    session = active()
    if session == 0xFFFFFFFF:  # no one at the console
        return None
    wts = ctypes.windll.wtsapi32
    buf, size = ctypes.c_void_p(), wintypes.DWORD()
    if not wts.WTSQuerySessionInformationW(None, session, 24, ctypes.byref(buf), ctypes.byref(size)):
        return None  # 24 = WTSSessionInfo
    try:
        if size.value < ctypes.sizeof(WTSINFOW):
            return None
        logon = WTSINFOW.from_address(buf.value).LogonTime
    finally:
        wts.WTSFreeMemory(buf)
    return (logon - FILETIME_EPOCH) / 1e7 if logon > FILETIME_EPOCH else None


def _windows_tick_boot():
    import ctypes
    import time
    tick = ctypes.windll.kernel32.GetTickCount64
    tick.restype = ctypes.c_ulonglong  # the default c_int wraps after 24.9 days of uptime
    return time.time() - tick() / 1000


def _windows_boot(logon=None, ticks=None):
    """Windows' "boot" is the console session's logon time. Fast Startup (on by default)
    resumes a hibernated kernel after Shutdown, so the uptime counter keeps running and a
    power-on never looked like a new boot (audit cluster-04). The uptime counter is the
    fallback when no one is logged on at the console."""
    for source in (logon or _windows_logon_time, ticks or _windows_tick_boot):
        try:
            value = source()
        except (OSError, ValueError, AttributeError):
            continue
        if value:
            return float(value)
    return None


def boot_time():
    """When this computer last booted (Unix seconds), or None if it can't be read. On Windows
    it is when the console session logged on (see _windows_boot): new for every sign-in after
    a Shutdown or Restart, fixed for the rest of that login."""
    try:
        system = _os()
        if system == "windows":
            return _windows_boot()
        if system == "macos":  # "{ sec = 1730000000, usec = 0 } Mon Nov ..."
            out = run_quiet(["sysctl", "-n", "kern.boottime"]).stdout
            return float(out.split("sec =", 1)[1].split(",", 1)[0])
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError, AttributeError, subprocess.SubprocessError):
        pass
    return None


def desktop_backend(windows=None):
    """Desktop action backend, allowing a caller's explicit Windows detection."""
    if (on_windows() if windows is None else windows):
        return "windows"
    session = detect()["desktop"]
    return "hyprland" if session == "windows" else session


# --- cluster membership (OS paths, key files, detached start) ---------------------------
# Callers in cluster_setup keep their current behavior until they delegate here.
# Branches follow _os() (same answer as detect()["os"]), except spawn_detached,
# which follows on_windows() because cluster_setup._launch_agent does.

_SSH_KEYS_THREADS = threading.Lock()  # one lock for every authorized_keys path
_BUSY_LOCK = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
if getattr(errno, "EDEADLOCK", None) is not None:
    _BUSY_LOCK.add(errno.EDEADLOCK)


def host_keys_dir():
    """Directory of this machine's SSH server host public keys.

    Windows OpenSSH keeps them under ProgramData. Linux and macOS use ``/etc/ssh``.
    The directory is not created.
    """
    if _os() == "windows":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ssh"
    return Path("/etc/ssh")


def _windows_process_is_admin():
    """True when this process token is elevated. Windows only."""
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def ssh_admin_account():
    """True when this Windows account is in Administrators, elevated or not.

    sshd logs the account in with its full token, so a UAC-filtered admin still
    uses ``administrators_authorized_keys``. ``IsUserAnAdmin`` only reports whether
    this process is elevated; ``whoami /groups`` lists the group either way.
    Other operating systems return False and do not run either check.
    """
    if _os() != "windows":
        return False
    try:
        out = run_quiet(["whoami", "/groups", "/fo", "csv", "/nh"], timeout=10)
        text = out.stdout or ""
    except (OSError, subprocess.SubprocessError):
        text = ""
    if "S-1-5-32-544" in text:  # BUILTIN\Administrators
        return True
    return _windows_process_is_admin()


def ssh_authorized_keys_path():
    """The authorized_keys file OpenSSH will read for this account.

    Windows admin accounts use ``administrators_authorized_keys``. Everyone else
    uses ``~/.ssh/authorized_keys``. The file is not created.
    """
    if _os() == "windows" and ssh_admin_account():
        return host_keys_dir() / "administrators_authorized_keys"
    return Path.home() / ".ssh" / "authorized_keys"


# Fixed script. Path and account are -File arguments ($args), never interpolated.
# One Set-Acl publishes the finished DACL: inheritance off, every previous ACE
# gone, one FullControl allow for the account argument.
_WINDOWS_PRIVATE_KEY_ACL = (
    "$ErrorActionPreference = 'Stop'\n"
    "if ($args.Count -ne 2) { throw 'expected a path and an account' }\n"
    "$path = $args[0]\n"
    "$account = $args[1]\n"
    "if ([string]::IsNullOrEmpty($account)) { throw 'account is empty' }\n"
    "$acl = Get-Acl -LiteralPath $path\n"
    "$acl.SetAccessRuleProtection($true, $false)\n"
    "foreach ($rule in @($acl.Access)) { [void]$acl.PurgeAccessRules($rule.IdentityReference) }\n"
    "$grant = New-Object System.Security.AccessControl.FileSystemAccessRule($account, 'FullControl', 'Allow')\n"
    "[void]$acl.AddAccessRule($grant)\n"
    "Set-Acl -LiteralPath $path -AclObject $acl\n"
)


def _windows_private_key_account():
    """``DOMAIN\\user`` for this logon, or ``user`` when USERDOMAIN is unset."""
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN") or ""
    if not user:
        raise ValueError("private key permissions need USERNAME of the account that reads the key.")
    return f"{domain}\\{user}" if domain else user


def _restrict_windows_private_key(path, account):
    """Replace the key DACL with one allow for ``account``.

    ``icacls /inheritance:r /grant:r`` drops inherited ACEs and still leaves
    every other account's explicit allow in place. The script below edits a
    copy of the descriptor in memory and calls Set-Acl once, so the file never
    passes through a parent-inherited DACL. The script file is constant text.
    ``powershell.exe -File`` receives the path and the account as data
    arguments (execution policy applies only to this process).
    """
    script_path = None
    try:
        fd, script_path = tempfile.mkstemp(prefix="jarvis-key-acl-", suffix=".ps1")
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
            handle.write(_WINDOWS_PRIVATE_KEY_ACL)
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-InputFormat", "None", "-ExecutionPolicy", "Bypass",
            "-File", script_path, str(path), account,
        ]
        out = run_quiet(command, timeout=30, stdin=subprocess.DEVNULL)
        if out.returncode != 0:
            raise subprocess.CalledProcessError(out.returncode, command, out.stdout, out.stderr)
    finally:
        if script_path is not None:
            try:
                os.unlink(script_path)
            except OSError:
                pass


def private_key_permissions(path):
    """Restrict an existing SSH private key to this account.

    POSIX: mode ``0600`` (chmod ignores umask). Windows: one allow FullControl
    ACE for this account and inheritance disabled. SYSTEM and Administrators
    stay off the DACL. Win32-OpenSSH tolerates those SIDs on a client key and
    accepts the key with only the current user; host keys, which those
    accounts have to read, are a different contract. A non-zero PowerShell
    status raises ``CalledProcessError``. Returns the path.
    """
    path = Path(path)
    if _os() != "windows":
        os.chmod(path, 0o600)
        return path
    _restrict_windows_private_key(path, _windows_private_key_account())
    return path


def _ssh_keys_lock_path(path):
    """Sibling lock file ``cluster_setup._keys_locked`` already uses."""
    path = Path(path)
    return path.with_name(f".{path.name}.jarvis-lock")


def _prepare_lock_handle(handle):
    """Make the lock file at least one byte, without truncating it.

    Windows ``msvcrt.locking`` refuses to lock a byte past EOF, so an empty
    lock file fails with a sharing error. Append one byte only when the file
    is empty. Two processes may both append; the lock still covers byte 0.
    """
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def _acquire_os_lock(handle):
    """Block until this handle holds the OS lock. POSIX waits in the kernel.
    Windows ``LK_LOCK`` gives up after about 10 seconds; retry those busy
    errors so a live holder is not reported as a hard failure."""
    _prepare_lock_handle(handle)
    if _os() == "windows":
        import msvcrt
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError as exc:
                if exc.errno not in _BUSY_LOCK:
                    raise
            _prepare_lock_handle(handle)
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


@contextmanager
def locked_ssh_keys(path):
    """Hold ``path`` for one read-modify-write of an authorized_keys file.

    Threads in this process share ``_SSH_KEYS_THREADS``. Other processes take
    the OS lock on ``_ssh_keys_lock_path(path)``. The thread lock is acquired
    first and released only after the OS lock is gone, so the two never invert.

    The OS lock is released by closing the handle. An explicit unlock and then
    a close leaves a gap on Windows: another process can lock the file, and
    the close drops that new lock too. There is no ``LK_UNLCK`` / ``LOCK_UN``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SSH_KEYS_THREADS:
        handle = open(_ssh_keys_lock_path(path), "a+b", buffering=0)
        try:
            _acquire_os_lock(handle)
            yield
        finally:
            handle.close()


# Leave deletes these. Membership wires the glob; this module only creates them.
# The fixed name cluster-node-launch.vbs is not used: two launches would share it.
DETACHED_LAUNCH_GLOB = "cluster-node-launch-*.vbs"


def _detached_command_line(argv):
    """VBScript command line. CR or LF in an argument is rejected first.

    ``list2cmdline`` does not escape line breaks, and ``autostart.vbs`` places
    the line inside one double-quoted string. A CR or LF would end that
    statement and run the rest of the argument as script. Callers must invoke
    this before creating a launcher file or a process.
    """
    for arg in argv:
        if "\n" in arg or "\r" in arg:
            raise ValueError("spawn_detached rejects a command argument that contains a line break.")
    line = subprocess.list2cmdline(argv).replace('"', '""')
    if "\n" in line or "\r" in line:
        raise ValueError("spawn_detached rejects a command argument that contains a line break.")
    return line


def _publish_launch_script(state, text):
    """Write one unique launcher and publish it only when the bytes are complete.

    The temp file sits in ``state`` so the replace stays on one volume. The
    name appears after the full text is flushed. Explorer is started by the
    caller, after this returns.
    """
    directory = Path(state)
    final = directory / f"cluster-node-launch-{secrets.token_hex(16)}.vbs"
    fd, tmp_name = tempfile.mkstemp(prefix=".cluster-node-launch-", suffix=".tmp", dir=directory)
    published = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, final)
        published = True
    finally:
        if not published:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return final


def spawn_detached(argv, *, state=None):
    """Start ``argv`` so it outlives this process. Returns the ``Popen``.

    ``argv`` is an argument list, never a shell string. The caller picks the
    interpreter (including ``pythonw.exe``). POSIX: new session, stdio discarded.
    Windows: write a unique file matching ``DETACHED_LAUNCH_GLOB`` under
    ``state`` and open that path with ``explorer.exe`` after the file is
    complete. ``state`` is required on Windows and ignored elsewhere. An
    argument that contains CR or LF raises ``ValueError`` before any launcher
    file or process exists. Leave removes ``DETACHED_LAUNCH_GLOB`` under the
    state directory.
    """
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
        raise TypeError("spawn_detached expects an argument list, not a command string.")
    argv = [os.fsdecode(arg) if isinstance(arg, bytes) else str(arg) for arg in argv]
    if on_windows():
        if state is None:
            raise ValueError("Windows detached launch needs a state directory for the Explorer launcher.")
        from .autostart import vbs
        script = _publish_launch_script(state, vbs(_detached_command_line(argv)))
        return subprocess.Popen(["explorer.exe", str(script)], creationflags=NO_WINDOW)
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
