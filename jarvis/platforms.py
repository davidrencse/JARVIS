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

import functools
import os
import platform
import subprocess
import sys
from pathlib import Path

OSES = ("windows", "macos", "linux")
# creationflags for a child process that must not flash a console window (0 off Windows).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_quiet(args, timeout=5, **kw):
    """Argument-list subprocess (never a shell), text out, no console window on Windows."""
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW, **kw)


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


def is_macos():
    return detect()["os"] == "macos"


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
