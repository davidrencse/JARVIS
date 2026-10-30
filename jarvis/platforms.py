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
import sys
from pathlib import Path

OSES = ("windows", "macos", "linux")


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
