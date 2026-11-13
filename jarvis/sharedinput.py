"""Shared mouse and keyboard (Lan Mouse), read-only, for the console's Settings view.

What each computer's Lan Mouse is configured to do, its screens, and whether the two
configurations agree: which edge leads where, whether the peer addresses are current, and
whether motion_scale keeps hand travel identical across screens of different pixel
density. It reads configs, processes and monitor geometry.

control() is the one part that acts: the `lan_mouse` desktop action starts, stops or
pairs Lan Mouse on this computer, so `jarvis lanmouse start|stop|pair` reach the other
laptop over the cluster with that computer's own OS paths (no SSH, no ~/.config on
Windows). Starting runs the same daemon as before, whose release_bind shortcut is the
fail-safe for captured input; nothing here changes login autostart.

Screens, processes and addresses are read per OS and desktop (Windows, macOS, Hyprland, sway,
X11); platforms.detect() says which.

Each computer reports itself through the read-only `input_info` desktop action
(local_info), so the Zenbook answers over the cluster or SSH like any other action.
It includes this computer's own certificate fingerprint (public; pairing needs it), but
authorized fingerprints and keys are never read out of the configs.
"""

import json
import math
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

from .fsio import read_json
from .platforms import NO_WINDOW, on_windows
from . import platforms

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
SCALE_TOLERANCE = 0.03  # relative error in motion_scale that still feels identical
FLAGS = NO_WINDOW


def _run(*args, timeout=5):
    return platforms.run_quiet(args, timeout)


# -- this computer ---------------------------------------------------------------------------
def _desktop():
    """platforms.detect()'s desktop family; os.name == "nt" always wins (checked by callers)."""
    return platforms.detect()["desktop"]


def config_path():
    if _desktop() == "macos":  # never true on Windows, so that branch below is unchanged
        return Path.home() / "Library" / "Application Support" / "lan-mouse" / "config.toml"
    if on_windows():
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "lan-mouse" / "config.toml"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "lan-mouse" / "config.toml"


def read_config(path=None):
    """The safe parts of Lan Mouse's config.toml, or None if there is none."""
    path = Path(path or config_path())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data = tomllib.loads(text) if tomllib else _mini_toml(text)
    clients = [{"position": c.get("position"), "hostname": c.get("hostname"),
                "ips": [str(ip) for ip in c.get("ips", [])], "port": c.get("port")}
               for c in data.get("clients", []) if isinstance(c, dict)]
    scale = data.get("motion_scale")
    return {"path": str(path), "port": data.get("port"),
            "motion_scale": float(scale) if isinstance(scale, (int, float)) else None,
            "release_bind": [str(k) for k in data.get("release_bind", [])],
            "capture_backend": data.get("capture_backend"), "clients": clients}


def _mini_toml(text):
    """Enough of TOML for Lan Mouse's config (Python 3.10 has no tomllib)."""
    data, current = {}, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith('"') else ""
        if not line:
            continue
        if line == "[[clients]]":
            current = {}
            data.setdefault("clients", []).append(current)
            continue
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if value.startswith("["):
            value = re.findall(r'"([^"]*)"', value)
        elif value.startswith('"'):
            value = value.strip('"')
        elif value in ("true", "false"):
            value = value == "true"
        else:
            try:
                value = float(value) if "." in value else int(value)
            except ValueError:
                continue
        (current if current is not None else data)[key] = value
    return data


def monitors():
    """This computer's screens in physical pixels: [{name, width, height, x, y, primary}]."""
    desktop = _desktop()
    if desktop == "sway":
        return _monitors_sway()
    if desktop == "macos":
        return _monitors_macos()
    if desktop in ("x11", "gnome", "kde"):
        return _monitors_xrandr()
    if on_windows():
        return _monitors_nt()
    if desktop not in ("hyprland", "windows"):  # another Wayland compositor, or no desktop
        return []
    if not shutil.which("hyprctl"):
        return []
    from .remote import session_env
    env = {**os.environ, **session_env()}
    out = subprocess.run(["hyprctl", "-j", "monitors"], capture_output=True, text=True,
                         timeout=5, env=env)
    try:
        listed = json.loads(out.stdout or "[]")
    except ValueError:
        return []
    return [{"name": m.get("name"), "description": m.get("description"),
             "width": m.get("width"), "height": m.get("height"), "x": m.get("x"),
             "y": m.get("y"), "scale": m.get("scale"), "primary": bool(m.get("focused"))}
            for m in listed]


def _monitors_sway():
    if not shutil.which("swaymsg"):
        return []
    try:
        listed = json.loads(_run("swaymsg", "-t", "get_outputs").stdout or "[]")
    except ValueError:
        return []
    found = []
    for m in listed if isinstance(listed, list) else []:
        if not isinstance(m, dict) or m.get("active") is False:
            continue
        mode, rect = m.get("current_mode") or {}, m.get("rect") or {}
        found.append({"name": m.get("name"),
                      "description": " ".join(str(m.get(k)) for k in ("make", "model") if m.get(k)) or None,
                      "width": mode.get("width") or rect.get("width"),
                      "height": mode.get("height") or rect.get("height"),
                      "x": rect.get("x", 0), "y": rect.get("y", 0), "scale": m.get("scale"),
                      "primary": bool(m.get("focused"))})
    return found


# "0: +*eDP-1 1920/344x1080/194+0+0  eDP-1"; the /mm parts are optional.
XRANDR_MONITOR = re.compile(r"^\s*\d+:\s+\+?(\*)?(\S+)\s+(\d+)(?:/\d+)?x(\d+)(?:/\d+)?\+(-?\d+)\+(-?\d+)")


def _monitors_xrandr():
    if not shutil.which("xrandr"):
        return []
    found = []
    for line in _run("xrandr", "--listmonitors").stdout.splitlines():
        m = XRANDR_MONITOR.match(line)
        if m:
            star, name, w, h, x, y = m.groups()
            found.append({"name": name, "width": int(w), "height": int(h), "x": int(x),
                          "y": int(y), "primary": bool(star)})
    if found and not any(m["primary"] for m in found):
        found[0]["primary"] = True
    return found


def _monitors_macos():
    if not shutil.which("system_profiler"):
        return []
    try:
        data = json.loads(_run("system_profiler", "SPDisplaysDataType", "-json", timeout=20).stdout or "{}")
    except ValueError:
        return []
    found = []
    for card in data.get("SPDisplaysDataType") or []:
        for d in card.get("spdisplays_ndrvs") or []:
            size = re.search(r"(\d+)\s*x\s*(\d+)", str(d.get("_spdisplays_resolution")
                                                      or d.get("_spdisplays_pixels") or ""))
            if size:  # x/y are not reported: the arrangement is not known here
                found.append({"name": d.get("_name"), "width": int(size.group(1)),
                              "height": int(size.group(2)), "x": 0, "y": 0,
                              "primary": d.get("spdisplays_main") == "spdisplays_yes"})
    if found and not any(m["primary"] for m in found):
        found[0]["primary"] = True
    return found


def _monitors_nt():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:  # per-monitor DPI awareness for this thread, so sizes are physical pixels
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def visit(handle, _dc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.rcMonitor
            found.append({"name": info.szDevice.replace("\\\\.\\", ""), "width": r.right - r.left,
                          "height": r.bottom - r.top, "x": r.left, "y": r.top,
                          "primary": bool(info.dwFlags & 1)})
        return 1

    user32.EnumDisplayMonitors(None, None, proc(visit), 0)
    return found


def _windows_build(path):
    """patched for the JARVIS build: its own bin folder, or the copy `elevate` installed in
    Program Files (audit cluster-06: that one was labelled official)."""
    path = path.lower().replace("/", "\\")
    try:
        home = str(install_dir()).lower().replace("/", "\\").rstrip("\\") + "\\"
    except ValueError:  # not Windows, or Program Files unknown: only the bin folder counts
        home = "\0"
    return "patched" if "\\lan-mouse\\bin\\" in path or path.startswith(home) else "official"


def lan_mouse_status():
    """{running, build} where build is patched (JARVIS), official, or None when stopped."""
    if _desktop() == "macos":
        if shutil.which("pgrep") and _run("pgrep", "-x", "lan-mouse").returncode == 0:
            return {"running": True, "build": "official"}  # the JARVIS patches target Windows/Arch
        return {"running": False, "build": None}
    if on_windows():
        out = _run("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-CimInstance Win32_Process -Filter \"Name='lan-mouse.exe'\" | "
                   "Select-Object -First 1 -ExpandProperty ExecutablePath", timeout=10)
        path = out.stdout.strip()
        if path:
            return {"running": True, "build": _windows_build(path)}
        # Windows hides an elevated process's path from an unelevated query: the daemon the
        # elevated task started read as stopped, and start() launched a second one.
        if not _quick_status()["running"]:
            return {"running": False, "build": None}
        return {"running": True, "build": "patched" if _task_exists(ELEVATED_TASK) else "unknown"}
    for unit, build in (("lan-mouse.service", "patched"), ("lan-mouse-fallback.service", "official")):
        if shutil.which("systemctl") and _run("systemctl", "--user", "is-active", unit).stdout.strip() == "active":
            return {"running": True, "build": build}
    if shutil.which("pgrep"):  # started by hand, outside the units ("lan-mouse-jarvis" -> 15 chars)
        if _run("pgrep", "-x", "lan-mouse-jarvi").returncode == 0:
            return {"running": True, "build": "patched"}
        if _run("pgrep", "-x", "lan-mouse").returncode == 0:
            return {"running": True, "build": "official"}
    return {"running": False, "build": None}


def addresses():
    """This computer's IPv4 addresses (LAN and Tailscale), loopback excluded."""
    found = set()
    if _desktop() == "macos" and shutil.which("ifconfig"):
        found.update(re.findall(r"(?:^|\s)inet (\d+\.\d+\.\d+\.\d+)\s", _run("ifconfig").stdout))
    elif not on_windows() and shutil.which("ip"):
        found.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", _run("ip", "-4", "-o", "addr").stdout))
    else:
        try:
            found.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
    return sorted(ip for ip in found if not ip.startswith("127."))


def local_info():
    """Everything this computer can say about its shared input, as plain data."""
    def safely(fn, fallback):
        try:
            return fn()
        except Exception:  # one unreadable part must not hide the rest
            return fallback
    return {"hostname": socket.gethostname(), "os": "windows" if on_windows() else platforms.detect()["os"],
            "monitors": safely(monitors, []), "addresses": safely(addresses, []),
            "lan_mouse": {**safely(lan_mouse_status, {"running": False, "build": None}),
                          "config": safely(read_config, None),
                          "fingerprint": safely(_fingerprint, None)}}


def _fingerprint():
    from .desktop import local_fingerprint
    return local_fingerprint()


def run(_arg=""):
    """The read-only `input_info` desktop action: (ok, json text)."""
    return True, json.dumps(local_info())


# -- starting, stopping and pairing this computer's Lan Mouse ------------------------------------
UNITS = ("lan-mouse.service", "lan-mouse-ipwatch.service")
STARTUP_NAME = "JARVIS Lan Mouse.vbs"
ELEVATED_TASK = "JARVIS Lan Mouse"  # on-demand scheduled tasks that run Lan Mouse elevated (see elevate())
ELEVATED_STOP_TASK = "JARVIS Lan Mouse Stop"


def _units():
    """The JARVIS systemd user units that exist here (none on a hand-made setup)."""
    if not shutil.which("systemctl"):
        return []
    listed = _run("systemctl", "--user", "list-unit-files", "--no-legend", *UNITS, "lan-mouse-fallback.service").stdout
    return [line.split()[0] for line in listed.splitlines() if line.strip()]


def _windows_launcher():
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / STARTUP_NAME)


def _windows_once_launcher():
    """start-now.vbs: runs bin\\lan-mouse.exe once, no watchdog (login autostart stays off)."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "lan-mouse" / "start-now.vbs"


def _binary():
    if on_windows():
        patched = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "lan-mouse" / "bin" / "lan-mouse.exe"
        return str(patched) if patched.is_file() else shutil.which("lan-mouse")
    patched = Path.home() / ".local" / "bin" / "lan-mouse-jarvis"
    return str(patched) if patched.is_file() else shutil.which("lan-mouse")


def _quick_status():
    """lan_mouse_status() for polling: Windows swaps the slow PowerShell/CIM query for tasklist
    (no build type, which the callers of this don't need)."""
    if os.name == "nt":
        out = _run("tasklist", "/FI", "IMAGENAME eq lan-mouse.exe", "/FO", "CSV", "/NH", timeout=10)
        return {"running": "lan-mouse.exe" in out.stdout.lower(), "build": None}
    return lan_mouse_status()


def _task_exists(name):
    return os.name == "nt" and _run("schtasks", "/Query", "/TN", name, timeout=10).returncode == 0


def start(_attempt=0):
    first = lan_mouse_status()
    if first["running"]:
        return {"started": False, "note": "Lan Mouse is already running.", **first}
    if _desktop() == "macos":
        raise ValueError("Start Lan Mouse from its app on macOS.")
    units = _units()
    if os.name == "nt" and _task_exists(ELEVATED_TASK):
        # Elevated, so Windows lets it inject input into admin windows too (UIPI).
        done = _run("schtasks", "/Run", "/TN", ELEVATED_TASK, timeout=15)
        if done.returncode:
            if _attempt < 2:
                return start(_attempt=_attempt + 1)
            raise RuntimeError("schtasks couldn't start Lan Mouse: " + (done.stderr.strip()[-200:] or "no detail"))
    elif os.name != "nt" and "lan-mouse.service" in units:
        wanted = [u for u in UNITS if u in units]
        done = _run("systemctl", "--user", "start", *wanted, timeout=30)
        if done.returncode:
            raise RuntimeError("systemctl couldn't start Lan Mouse: " + (done.stderr.strip()[-200:] or "no detail"))
    elif os.name == "nt" and _windows_once_launcher().is_file():
        # Through Explorer, so the daemon runs outside the calling shell's job (a remote
        # `lanmouse start` otherwise dies when the node's command shell exits).
        subprocess.Popen(["explorer.exe", str(_windows_once_launcher())], creationflags=FLAGS, close_fds=True)
    elif os.name == "nt" and _windows_launcher().is_file():
        subprocess.Popen(["explorer.exe", str(_windows_launcher())], creationflags=FLAGS, close_fds=True)
    else:
        binary = _binary()
        if not binary:
            raise ValueError("Lan Mouse isn't installed on this computer.")
        log = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / (
            "lan-mouse/daemon.log" if os.name == "nt" else "lan-mouse.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as out:
            subprocess.Popen([binary, "daemon"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                             close_fds=True, creationflags=FLAGS | getattr(subprocess, "DETACHED_PROCESS", 0),
                             start_new_session=os.name != "nt")
    import time
    for _ in range(20):
        time.sleep(0.25)
        status = _quick_status()
        if status["running"]:
            return {"started": True, **(lan_mouse_status() if os.name == "nt" else status)}
    if os.name == "nt" and _attempt < 2:
        return start(_attempt=_attempt + 1)
    return {"started": False, "note": "Lan Mouse did not stay up after three launch attempts; check daemon.log and run jarvis lanmouse elevate from an admin terminal if the task is missing or broken.", **lan_mouse_status()}


def stop(sharing_only=False):
    """Stop Lan Mouse. sharing_only keeps the IP watcher running (the guard lives in it)."""
    if os.name == "nt":
        import time
        done = _run("taskkill", "/IM", "lan-mouse.exe", "/F", timeout=15)
        if done.returncode and _task_exists(ELEVATED_STOP_TASK):
            # An unelevated taskkill can't end the elevated daemon ("Access is denied").
            _run("schtasks", "/Run", "/TN", ELEVATED_STOP_TASK, timeout=15)
        # Wait for the exit either way: taskkill returns while the process is still going,
        # and elevate()'s start() right after then saw it "already running" and left it off.
        for _ in range(20):
            if not _quick_status()["running"]:
                break
            time.sleep(0.25)
    else:
        units = _units()
        if sharing_only:
            units = [u for u in units if u != "lan-mouse-ipwatch.service"]
        if units:
            _run("systemctl", "--user", "stop", *units, timeout=30)
        if shutil.which("pkill"):
            _run("pkill", "-x", "lan-mouse")
            _run("pkill", "-x", "lan-mouse-jarvi")
    status = lan_mouse_status()
    return {"stopped": not status["running"], **status}


def _task_xml(command, arguments, user):
    """Task Scheduler XML: run on demand only, elevated (highest available) as `user`."""
    from xml.sax.saxutils import escape
    CR = {"\r": "&#13;"}  # else XML parsing turns a CR into a LF
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>JARVIS Lan Mouse, elevated so admin windows don't trap the shared input.</Description></RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user, CR)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command, CR)}</Command>
      <Arguments>{escape(arguments, CR)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def install_dir():
    """Where elevate() installs the copy the elevated task runs (admin-writable only).
    Windows only; Program Files comes from the shell (SHGetKnownFolderPath), never from the
    %ProgramFiles% variable, which any process running as David can override in HKCU\\Environment.
    Off Windows, or if the shell won't say, this raises rather than guess a path."""
    if os.name != "nt":
        raise ValueError("Elevated Lan Mouse is a Windows feature.")
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                    ("d", ctypes.c_ubyte * 8)]
    # FOLDERID_ProgramFiles {905e63b6-c1bf-494e-b29c-65b732d3d21a}
    folder_id = GUID(0x905E63B6, 0xC1BF, 0x494E,
                     (ctypes.c_ubyte * 8)(0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0xD2, 0x1A))
    out = ctypes.c_wchar_p()
    try:
        shell = ctypes.windll.shell32
        shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                               wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        if shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)) != 0:
            raise OSError("SHGetKnownFolderPath failed")
        base = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception as exc:
        raise ValueError(f"Can't tell where Program Files is, so nothing was installed ({exc}).") from None
    if not base or not Path(base).is_absolute():
        raise ValueError("Can't tell where Program Files is, so nothing was installed.")
    return Path(base) / "JARVIS Lan Mouse"


_WRITE_RIGHTS = {"F", "M", "W", "WD", "AD", "WA", "WEA", "DC", "DE", "WDAC", "WO", "GA", "GW"}
_UNTRUSTED = ("builtin\\users", "nt authority\\authenticated users", "everyone", "users",
              "authenticated users", "s-1-5-32-545", "s-1-5-11", "s-1-1-0")


def writable_by_users(icacls_output, folder, user=None):
    """Principals (BUILTIN\\Users, Authenticated Users, Everyone, the current user) that icacls
    says can write or modify `folder`. Empty when only admins/SYSTEM/TrustedInstaller can."""
    bad, mine = [], (user or "").lower()
    for line in str(icacls_output).splitlines():
        line = line.strip()
        if line.lower().startswith(str(folder).lower()):
            line = line[len(str(folder)):].strip()
        for who, perms in re.findall(r"([^\s:()][^:()]*?):((?:\([A-Za-z0-9,]+\))+)", line):
            who = who.strip()
            tokens = {t for group in re.findall(r"\(([^)]*)\)", perms) for t in group.split(",")}
            if "DENY" in tokens or not tokens & _WRITE_RIGHTS:
                continue
            name = who.lower()
            if name in _UNTRUSTED or (mine and (name == mine or name.split("\\")[-1] == mine.split("\\")[-1])):
                bad.append(who)
    return bad


def _verify_locked_down(home):
    """Raise unless only admins can write `home` (the elevated task runs what's in it)."""
    done = _run("icacls", str(home), timeout=30)
    if done.returncode:
        raise RuntimeError("icacls couldn't read the install folder's permissions: "
                           + (done.stderr.strip()[-200:] or "no detail"))
    user = os.environ.get("USERNAME", "")
    bad = writable_by_users(done.stdout, home, user)
    if bad:
        raise RuntimeError(f"{home} is writable by {', '.join(sorted(set(bad)))}; refusing to run "
                           "anything from it elevated. The elevated tasks were removed.")


def _is_admin():
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def elevate(state=None):
    """Windows, once, from an admin terminal: create the on-demand tasks that run Lan Mouse
    elevated (no UAC prompt per start), then restart a running unelevated daemon through them."""
    if os.name != "nt":
        raise ValueError("Elevated Lan Mouse is a Windows feature.")
    if not _is_admin():
        raise ValueError("Run this once from an admin terminal: jarvis lanmouse elevate")
    binary = _binary()
    if not binary:
        raise ValueError("Lan Mouse isn't installed on this computer.")
    import tempfile
    # The task runs elevated with no prompt, so everything it runs must be out of reach of
    # unelevated code: a launcher or exe in %LOCALAPPDATA% could be swapped by any process
    # running as David (JARVIS terminal commands have no sandbox on Windows) for silent admin.
    # Program Files only admins can write; re-run `elevate` after rebuilding Lan Mouse.
    home = install_dir()
    was_running = lan_mouse_status()["running"]
    if was_running:
        stop()  # its exe in `home` may be in use
    _install_copy(Path(binary), home)
    try:
        _verify_locked_down(home)
    except Exception:
        unelevate()  # never leave a task pointing at a folder users can write
        raise
    exe = home / Path(binary).name
    vbs = home / "start-elevated.vbs"
    log = home / "daemon.log"
    inner = f'cmd /c ""{exe}" daemon >> "{log}" 2>&1"'
    vbs.write_text('CreateObject("WScript.Shell").Run "' + inner.replace('"', '""') + '", 0, False\r\n',
                   encoding="ascii", errors="replace")
    user = os.environ["USERDOMAIN"] + "\\" + os.environ["USERNAME"]
    tasks = ((ELEVATED_TASK, "wscript.exe", f'"{vbs}"'),
             (ELEVATED_STOP_TASK, "taskkill.exe", "/IM lan-mouse.exe /F"))
    with tempfile.TemporaryDirectory() as tmp:
        for name, command, arguments in tasks:
            xml = Path(tmp) / (name.replace(" ", "_") + ".xml")
            xml.write_text(_task_xml(command, arguments, user), encoding="utf-16")
            done = _run("schtasks", "/Create", "/F", "/TN", name, "/XML", str(xml), timeout=30)
            if done.returncode:
                raise RuntimeError(f"schtasks couldn't create '{name}': " + (done.stderr.strip()[-200:] or "no detail"))
    # Only a daemon that was up comes back: one the guard stopped (laptops apart) stays off,
    # or the guard would later read it as a start by hand (audit cluster-07).
    running = (bool(start().get("started")) or lan_mouse_status()["running"]) if was_running else False
    return {"elevated": True, "tasks": [t[0] for t in tasks], "running": running}


def _install_copy(binary, home):
    """Copy only what the daemon runs (its exe and DLLs) into `home`, and drop anything else
    there but the launcher and log: old builds kept beside the exe (`*.pre-*`, `.off`) were
    copied along and piled up in Program Files."""
    import shutil as _shutil
    keep = {binary.name.lower(), "start-elevated.vbs", "daemon.log"}
    home.mkdir(parents=True, exist_ok=True)
    for src in binary.parent.iterdir():
        # A symlink in the user-writable bin folder must not be copied into Program Files.
        if src.is_symlink() or not src.is_file():
            continue
        if src.name.lower() == binary.name.lower() or src.suffix.lower() == ".dll":
            _shutil.copy2(src, home / src.name, follow_symlinks=False)
            keep.add(src.name.lower())
    for old in home.iterdir():
        if old.is_file() and old.name.lower() not in keep:
            old.unlink()


def unelevate(state=None):
    """Delete both tasks; Lan Mouse starts unelevated again."""
    if os.name != "nt":
        raise ValueError("Elevated Lan Mouse is a Windows feature.")
    removed = [name for name in (ELEVATED_TASK, ELEVATED_STOP_TASK)
               if _task_exists(name) and _run("schtasks", "/Delete", "/TN", name, "/F", timeout=15).returncode == 0]
    return {"elevated": False, "removed": removed}


# -- off while the laptops are apart -----------------------------------------------------------
# Sharing a mouse with a laptop that isn't on the desk is useless, and an edge that hands the
# pointer to an unseen screen is how input gets "stuck". Tailscale already knows how the two
# reach each other: a direct address on one of this computer's own subnets means the same
# local network; a public
# endpoint, a DERP relay or an offline peer means apart. guard() runs on a timer (the Arch
# IP watcher, the node agent on Windows) and stops Lan Mouse after two "apart" readings in a
# row; it starts it again only if it was the one that stopped it, so a manual stop sticks.
# This only stops the sharing daemon: each laptop's own keyboard and mouse are untouched.
GUARD_FILE = "lanmouse-guard.json"
GUARD_STREAK = 2  # readings in a row before acting (30-60 s apart): a brief relay doesn't flap it
GUARD_GRACE = 90  # seconds a daemon the guard just signalled may take to die before "running" means a manual start
BOOT_JITTER = 120  # seconds: boot times closer than this are the same boot (Windows derives it from uptime)


def _tailscale_status():
    exe = shutil.which("tailscale")
    if not exe and on_windows():
        guess = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
        exe = str(guess) if guess.is_file() else None
    if not exe:
        return None
    out = _run(exe, "status", "--json", timeout=8)
    if out.returncode:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


# VPN, VM and container interfaces: their subnets say nothing about where this laptop sits.
VIRTUAL_IF = re.compile(r"^(lo|tailscale|docker|br-|veth|virbr|vEthernet|Loopback|zt|wg|tun|tap|utun|ppp|"
                        r"vmnet|vboxnet|cni|flannel|ham)", re.I)


def local_networks(families=(4,)):
    """This computer's real LAN subnets (Wi-Fi/Ethernet), virtual and VPN interfaces left out.
    A home LAN can use any range (David's uses 172.1.x, which isn't RFC1918), so "same subnet
    as me" is the test, not "private address". families: IP versions to list; the Lan Mouse
    guard reads IPv4 only, address checks ask for (4, 6)."""
    import ipaddress
    pairs = []  # (interface, "ip/prefix")
    if on_windows():
        fam = "" if 6 in families else " -AddressFamily IPv4"
        out = _run("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   f"Get-NetIPAddress{fam} | ForEach-Object {{ $_.InterfaceAlias + '|' + "
                   "$_.IPAddress + '/' + $_.PrefixLength }", timeout=10)
        pairs = [tuple(line.strip().rsplit("|", 1)) for line in out.stdout.splitlines() if "|" in line]
    elif shutil.which("ip"):
        for line in _run("ip", "-o", "addr").stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
                pairs.append((parts[1], parts[3]))
    elif shutil.which("ifconfig"):  # macOS/BSD: no iproute2
        pairs = _ifconfig_pairs(_run("ifconfig").stdout)
    nets = []
    for name, cidr in pairs:
        if VIRTUAL_IF.match(name or ""):
            continue
        try:
            net = ipaddress.ip_interface(re.sub(r"%[^/]*", "", cidr)).network  # drop fe80::1%12 zone
        except ValueError:
            continue
        if net.version not in families or net.is_loopback:
            continue
        if 8 <= net.prefixlen < net.max_prefixlen:
            nets.append(net)
    return nets


def _ifconfig_pairs(text):
    """(interface, "ip/prefix") from BSD ifconfig: `inet 10.0.0.5 netmask 0xffffff00` and
    `inet6 fe80::1%en0 prefixlen 64`."""
    pairs, name = [], ""
    for line in text.splitlines():
        if line and not line[0].isspace():
            name = line.split(":", 1)[0]
            continue
        parts = line.split()
        try:
            if parts[:1] == ["inet"] and "netmask" in parts:
                mask = parts[parts.index("netmask") + 1]
                bits = bin(int(mask, 16)).count("1") if mask.startswith("0x") else \
                    sum(bin(int(o)).count("1") for o in mask.split("."))
                pairs.append((name, f"{parts[1]}/{bits}"))
            elif parts[:1] == ["inet6"] and "prefixlen" in parts:
                pairs.append((name, f"{parts[1].split('%')[0]}/{parts[parts.index('prefixlen') + 1]}"))
        except (ValueError, IndexError):
            continue
    return pairs


def _in(ip, nets):
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


# Tailscale `status --json` reports a phone as OS ios/android. Its cellular address is
# where the phone is, never proof that the two computers are apart. Same set as phone.MOBILE_OS.
_PHONE_OS = frozenset({"ios", "android", "ipados"})


def _phone_peer(peer):
    return str((peer or {}).get("OS") or "").strip().lower() in _PHONE_OS


def peer_nearby(config=None, status=None, nets=None):
    """{near: True|False|None, peer, why}. Together on any local evidence; apart only on
    positive evidence (online over a public address, or offline with no local sign);
    None means it can't tell, and the guard then leaves things alone.
    A phone on the tailnet is ignored: its cellular path is not the other computer."""
    config = read_config() if config is None else config
    clients = (config or {}).get("clients") or []
    if not clients:
        return {"near": None, "peer": None, "why": "Lan Mouse has no peer configured."}
    names = {str(c.get("hostname") or "").lower().removesuffix(".local") for c in clients} - {""}
    ips = {ip for c in clients for ip in (c.get("ips") or [])}
    status = _tailscale_status() if status is None else status
    if not status or status.get("BackendState", "Running") != "Running":
        return {"near": None, "peer": None, "why": "Tailscale isn't running here."}
    nets = local_networks() if nets is None else nets
    peers = [p for p in (status.get("Peer") or {}).values() if isinstance(p, dict) and not _phone_peer(p)]
    matches = [p for p in peers
               if str(p.get("HostName") or "").lower() in names or ips & set(p.get("TailscaleIPs") or [])]
    if not matches:
        return {"near": None, "peer": None, "why": "The Lan Mouse peer isn't in the tailnet."}
    # A re-registered machine leaves a stale offline twin: prefer the live one.
    p = sorted(matches, key=lambda m: (not m.get("Online"), not (ips & set(m.get("TailscaleIPs") or []))))[0]
    name = p.get("HostName")
    # CurAddr outlives a connection: only an online peer's address says where it is now.
    cur = str(p.get("CurAddr") or "") if p.get("Online") else ""
    host = cur.rsplit(":", 1)[0].strip("[]") if cur else ""
    if host and _in(host, nets):  # this computer's own subnets, never "any private address"
        return {"near": True, "peer": name, "why": f"{name} is on this network ({host})."}
    if host:  # online and Tailscale reaches it elsewhere: that beats a configured address,
        # which a spoofed mDNS answer on café Wi-Fi could have planted (audit 2026-11-07)
        return {"near": False, "peer": name, "why": f"{name} is on another network ({host})."}
    from .pathsafe import lan_mouse_ip_blocked
    lan = sorted(ip for ip in ips if _in(ip, nets) and not lan_mouse_ip_blocked(ip))
    if lan:  # the IP watcher only writes addresses it just found
        return {"near": True, "peer": name, "why": f"{name} has an address on this network ({lan[0]})."}
    if not p.get("Online"):
        return {"near": False, "peer": name, "why": f"{name} is offline and not on this network."}
    return {"near": None, "peer": name, "why": f"Tailscale hasn't talked to {name} lately; can't tell."}


def _notify(text):
    if on_windows() or not shutil.which("notify-send") or os.environ.get("JARVIS_NO_NOTIFY"):
        return
    try:
        _run("notify-send", "-a", "JARVIS", "Lan Mouse", text)
    except (OSError, subprocess.SubprocessError):
        pass


def _save_memo(path, memo):
    from .fsio import atomic_write
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(memo))
        return True
    except OSError:
        return False


def guard(state, nearby=None, status_fn=None, start_fn=None, stop_fn=None, autostart=False, boot=None,
          now=None, allow_start=True):
    """One reading: stop Lan Mouse when the laptops are apart, start it again when they're
    back together (only if this guard stopped it). A start by hand while apart is an override:
    left alone until the laptops have been together again. Returns what it saw and did.

    autostart: on the first reading after each boot (boot = platforms.boot_time()), a stopped
    Lan Mouse counts as stopped by the guard, so it comes on once the laptops read together
    and stays off while they're apart. Same on every OS: whatever runs the guard at login
    (the IP watcher on Linux, the node agent on Windows) is the autostart. Without autostart a
    new boot forgets the last session's guard stop: nothing comes up on its own after a boot.
    allow_start=False (lan_mouse.enabled false): the guard may stop sharing, never start it.

    The read-modify-write of the memo runs under a file lock (the node agent and the IP watcher
    can both run a guard on one computer); a tick that finds it busy does nothing."""
    from .locking import exclusive_lock
    state = Path(state)
    try:
        state.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(state / (GUARD_FILE + ".lock")):
            return _guard(state, nearby, status_fn, start_fn, stop_fn, autostart, boot, now, allow_start)
    except ValueError:  # the lock is busy: another guard is mid-reading
        return {"near": None, "peer": None, "why": "Another guard reading is in progress.",
                "running": None, "action": None, "stopped_by_guard": False, "override": False, "busy": True}


def _guard(state, nearby, status_fn, start_fn, stop_fn, autostart, boot, now, allow_start=True):
    import time
    nearby = nearby or peer_nearby
    status_fn, start_fn = status_fn or lan_mouse_status, start_fn or start
    stop_fn = stop_fn or (lambda: stop(sharing_only=True))  # never the IP watcher that runs us
    clock = time.time() if now is None else now
    path = Path(state) / GUARD_FILE
    memo = read_json(path, {})
    memo = memo if isinstance(memo, dict) else {}
    reading = nearby()
    near = reading["near"]
    running = status_fn()["running"]
    previous_boot = memo.get("boot")
    new_boot = boot is not None and (previous_boot is None or abs(boot - float(previous_boot)) > BOOT_JITTER)
    if new_boot:
        memo.update(boot=boot, apart=0, together=0, override=bool(running))
        memo.pop("apart_now", None)
        memo.pop("pending_start", None)
        # A stop recorded after this boot, even before the first guard tick, wins.
        if float(memo.get("manual_stop_at") or 0) < boot:
            memo.pop("manual_stop_at", None)
        memo["stopped_by_guard"] = bool(autostart and not running and not memo.get("manual_stop_at"))
        memo["stopped_at"] = 0
    if memo.get("manual_stop_at"):
        memo["stopped_by_guard"] = False
    # Two consecutive readings from THIS boot are required.
    memo["apart"] = memo.get("apart", 0) + 1 if near is False else 0
    memo["together"] = memo.get("together", 0) + 1 if near is True else 0
    dying = False
    if running and memo.pop("pending_start", False):
        # We crashed after launching but before acknowledging it. This is OUR start,
        # never a manual override that could keep sharing enabled while apart.
        memo["stopped_by_guard"], memo["override"] = False, False
    if running and memo.get("stopped_by_guard"):
        # stop() only signals: the daemon we just stopped may still be alive at this reading.
        # Only a daemon still up after the grace period is a start by hand.
        if clock - float(memo.get("stopped_at") or 0) < GUARD_GRACE:
            dying = True
        else:
            memo["stopped_by_guard"], memo["override"] = False, True
    if near is True:
        memo["override"] = False
    # What this reading saw, for sharing_availability(): the UI says "apart" only while the
    # guard's timer is still reading, so a dead guard never leaves a stale claim on screen.
    memo["read_at"], memo["near"], memo["why"] = clock, near, str(reading.get("why") or "")[:200]
    if memo["apart"] >= GUARD_STREAK:
        memo["apart_now"] = True  # read by sharing_paused(): the clipboard follows this too
    elif memo["together"] >= GUARD_STREAK:
        memo["apart_now"] = False
    action = None
    if dying:
        pass
    elif memo["apart"] >= GUARD_STREAK and running and not memo.get("override"):
        action = "stopped"
        memo["stopped_by_guard"], memo["stopped_at"] = True, clock
    elif (allow_start and memo["together"] >= GUARD_STREAK and memo.get("stopped_by_guard")
          and not memo.get("override")  # a hand start must not become an automatic start
          and memo.get("apart_now") is not True  # settled apart: never turn it back on
          and not running and not remote_session_live(state)  # a remote-control session owns this desktop
          and not controlling_remote(state)):  # or David is driving another computer from this one
        action = "started"
        memo["pending_start"] = True
    # Before a stop, so a crash mid-action can't lose what we did. A start is saved only once
    # it worked: a failed one leaves stopped_by_guard set and the next reading tries again.
    if _save_memo(path, memo) is False:
        return {**reading, "running": running, "action": None, "reason": "persistence_failed",
                "why": "Guard state could not be saved; no action taken."}
    if action == "stopped":
        try:
            stopped = stop_fn()
            if isinstance(stopped, dict) and stopped.get("stopped") is False:
                raise RuntimeError("Lan Mouse is still running after stop; retrying next reading.")
        except Exception as exc:
            action = "stop_failed"
            memo["stopped_by_guard"] = False
            reading = {**reading, "why": f"{reading['why']} Stop failed: {exc}"}
        if action == "stopped":
            _notify(f"Sharing is off: {reading['why']} It comes back when the laptops are together again.")
    elif action == "started":
        try:
            start_fn()
        except Exception as exc:  # e.g. systemctl refused: keep the state, retry next reading
            action = "start_failed"
            reading = {**reading, "why": f"{reading['why']} Start failed: {exc}"}
        else:
            if status_fn()["running"]:
                memo["stopped_by_guard"] = False
                memo.pop("pending_start", None)
                _save_memo(path, memo)
                _notify(f"Sharing is back on: {reading['why']}")
            else:
                action = "start_failed"
    reason = ("disabled" if not allow_start else
              "remote_control" if remote_session_live(state) or controlling_remote(state) else
              "manual_stop" if memo.get("manual_stop_at") else
              action or ("apart" if memo.get("apart_now") else
                         "waiting_together" if memo.get("stopped_by_guard") else
                         "boot_unavailable" if autostart and boot is None and not running else "idle"))
    memo.update(reason=reason, why=reading.get("why", ""))
    _save_memo(path, memo)
    return {**reading, "reason": reason, "running": status_fn()["running"] if action else running, "action": action,
            "stopped_by_guard": bool(memo.get("stopped_by_guard")), "override": bool(memo.get("override"))}


REMOTE_MARKER = "remote-session.json"  # remote_view.MARKER; refreshed every few s while live
REMOTE_MARKER_TTL = 60.0


def remote_session_live(state):
    """A remote-control session from the main is driving this computer right now."""
    import time
    try:
        stamp = (Path(state) / REMOTE_MARKER).stat().st_mtime
        boot = platforms.boot_time()
        return (boot is None or stamp >= boot) and time.time() - stamp < REMOTE_MARKER_TTL
    except (OSError, TypeError):
        return False


CONTROLLING_MARKER = "remote-controlling.json"  # on the main while its console drives a node


def controlling_remote(state):
    """This computer's console is driving another computer right now (ControllerPause)."""
    import time
    try:
        stamp = (Path(state) / CONTROLLING_MARKER).stat().st_mtime
        boot = platforms.boot_time()
        return (boot is None or stamp >= boot) and time.time() - stamp < REMOTE_MARKER_TTL
    except (OSError, TypeError):
        return False


class ControllerPause:
    """On the main: while David drives another computer from the console, his mouse and
    keyboard stay on this one. Lan Mouse is stopped for the session, so no edge crossing can
    hand them to the node, and comes back after it (only if it was running, and not while
    the laptops read apart: then the guard brings it back once they're together).

    set(live) is cheap and called on every hub poll; the work runs on a short thread that
    always applies the newest wish, so calls arriving out of order still settle right.
    A process that dies mid-session leaves its marker; the next one adopts it, so its first
    set(False) restores Lan Mouse like a normal end."""

    TOUCH = 10.0  # s between marker refreshes while a session is live

    def __init__(self, state, status_fn=None, start_fn=None, stop_fn=None, clock=None):
        import threading
        import time
        self.state = Path(state)
        self.status_fn, self.start_fn = status_fn or lan_mouse_status, start_fn or start
        self.stop_fn = stop_fn or (lambda: stop(sharing_only=True))
        self.clock = clock or time.monotonic
        self.want = self.applied = False
        self.was_running = False
        self._memo_owed = False  # True once the guard memo records our stop
        self._touched = 0.0
        self._lock = threading.Lock()
        try:
            path = self.state / CONTROLLING_MARKER
            left = json.loads(path.read_text(encoding="utf-8"))
            # only this boot's: after a reboot the guard's autostart owns Lan Mouse again
            if isinstance(left, dict) and path.stat().st_mtime > (platforms.boot_time() or 0):
                self.applied = self.want = True
                self.was_running = bool(left.get("was_running"))
        except (OSError, ValueError):
            pass

    def set(self, live, wait=False):
        import threading
        self.want = bool(live)
        if self.want == self.applied and not (self.want and self.clock() - self._touched >= self.TOUCH):
            return
        thread = threading.Thread(target=self.apply, name="lan-mouse-controller", daemon=True)
        thread.start()
        if wait:
            thread.join()

    def apply(self):
        with self._lock:
            # Loop until the newest wish is applied: a set() that lands while we work (Connect,
            # then an instant failure) returns at once and relies on this loop to catch it.
            while True:
                live = self.want
                if live != self.applied or live:
                    self._apply_once(live)
                if self.want == live:
                    return

    def _apply_once(self, live):
        import time
        marker = self.state / CONTROLLING_MARKER
        try:
            if live:
                if not self.applied:
                    self._memo_owed = False
                    self.was_running = bool(self.status_fn().get("running"))
                    self._write(marker)  # first: the guard must not bring it back mid-stop
                    if self.was_running:
                        self.stop_fn()
                        _notify("Lan Mouse is paused while you control another computer.")
                else:
                    self._write(marker)
                self._touched = self.clock()
            elif self.applied:
                marker.unlink(missing_ok=True)
                if self.was_running and self._apart():
                    # Not now: the guard brings it back once the laptops are together. Said
                    # here too, so a busy guard lock at pause time can't lose the debt.
                    self._guard_memo(stopped_by_guard=True, override=False, stopped_at=time.time())
                elif (self.was_running and not self.status_fn().get("running")
                      and not read_json(self.state / GUARD_FILE, {}).get("manual_stop_at")
                      and not remote_session_live(self.state)):
                    self.start_fn()
                    # Ours to undo: left set, the guard would read this start as one by hand
                    # (an override) and never stop Lan Mouse when the laptops part.
                    self._guard_memo(stopped_by_guard=False, override=False)
                self.was_running = False
        except Exception:  # never break the remote session over Lan Mouse
            pass
        self.applied = live

    def _guard_memo(self, tries=50, **fields):
        return update_guard_memo(self.state, tries=tries, **fields)

    def _write(self, marker):
        import time
        from .fsio import atomic_write
        marker.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(marker, json.dumps({"was_running": self.was_running}))
        if self.was_running and not self._memo_owed:
            # A crash mid-session: the guard restores it once together. One quick try per
            # refresh (never stall the hub on a guard reading); the end path covers a miss.
            self._memo_owed = self._guard_memo(tries=1, stopped_by_guard=True, override=False,
                                               stopped_at=time.time())

    def _apart(self):
        # The guard memo, via the one projection: settled apart, or a fresh apart reading.
        # Unknown (no settled flag) is not apart, so a session end still restores Lan Mouse.
        avail = sharing_availability(self.state)
        return avail.get("settled") == "apart" or avail.get("state") == "apart"


def update_guard_memo(state, tries=50, **fields):
    """Merge fields into the guard's memo under its lock. False if the lock stayed busy."""
    import time
    from .locking import exclusive_lock
    state = Path(state)
    for attempt in range(tries):  # a guard reading holds the lock for a few seconds at most
        if attempt:
            time.sleep(0.1)
        try:
            with exclusive_lock(state / (GUARD_FILE + ".lock")):
                path = state / GUARD_FILE
                memo = read_json(path, {})
                if not isinstance(memo, dict):
                    memo = {}
                memo.update(fields)
                return _save_memo(path, memo) is not False
        except ValueError:
            continue
    return False


def apart_now(state):
    """The guard's last settled reading says the laptops are apart."""
    try:
        memo = json.loads((Path(state) / GUARD_FILE).read_text(encoding="utf-8"))
        return isinstance(memo, dict) and memo.get("apart_now") is True
    except (OSError, ValueError):
        return False


def sharing_paused(state):
    """Why shared input and the shared clipboard are off right now, or None.

    Remote control wins. Otherwise the same projection as start and pair: a settled
    apart reading pauses, fresh or stale. A dead guard that last said apart does not
    resume the clipboard, and this function does not start or stop Lan Mouse.
    off_when_apart is not consulted here; start and pair are the ones that honor it."""
    if not state:
        return None
    if remote_session_live(state):
        return "remote control is on"
    avail = sharing_availability(state)
    if avail.get("settled") == "apart":
        return "the laptops are apart"
    return None


def clipboard_paused(state):
    """Why the shared clipboard is off right now, or None. Unlike shared input, a live
    remote-control session (either side: this computer is driven, or its console drives a
    node) turns the clipboard ON even while the laptops are apart, so copy and paste work
    across the Remote view (David, 2026-11-12). Otherwise it follows sharing_paused."""
    if not state:
        return None
    if remote_session_live(state) or controlling_remote(state):
        return None
    avail = sharing_availability(state)
    if avail.get("settled") == "apart":
        return "the laptops are apart"
    return None


AVAILABILITY_TTL = 300.0  # s: the guard reads every 30-60 s; older than this means it isn't running


def sharing_availability(state, now=None):
    """Is shared mouse/keyboard usable between this computer and its peer right now?
    Built only from the guard's memo (peer_nearby via guard()): no second network test, and a
    phone's mobile data never reaches this function. {"state": together|apart|unknown,
    "settled": the memo's apart_now without the freshness window, "available": True|False|None,
    "why", "override", "remote": True}. `state` is apart or together only while the guard's
    timestamp is fresh, so a dead guard cannot leave a stale claim on screen. `settled` is what
    a restart consults, so a missing timestamp is not treated as together. Remote control is a
    separate path (hub/tunnel) and stays available whatever this says."""
    import time
    clock = time.time() if now is None else now
    try:
        memo = json.loads((Path(state) / GUARD_FILE).read_text(encoding="utf-8")) if state else None
    except (OSError, ValueError):
        memo = None
    memo = memo if isinstance(memo, dict) else {}
    settled = ("apart" if memo.get("apart_now") is True else
               "together" if memo.get("apart_now") is False else "unknown")
    out = {"state": "unknown", "settled": settled, "available": None, "remote": True,
           "override": bool(memo.get("override")),
           "stopped_by_guard": bool(memo.get("stopped_by_guard")),
           "reason": memo.get("reason", "not_checked"),
           "why": "Not checked yet: the guard hasn't read the network."}
    try:
        age = clock - float(memo.get("read_at"))
    except (TypeError, ValueError):
        age = None
    if age is None and memo.get("apart_now") in (True, False):
        out["why"] = "The guard's last reading has no timestamp; can't tell if it is current."
    elif age is not None and (age > AVAILABILITY_TTL or age < -60):
        out["why"] = "The guard hasn't read the network lately; can't tell."
    elif memo.get("apart_now") is True:
        out.update(state="apart", available=False, why=memo.get("why") or "The laptops are apart.")
    elif memo.get("apart_now") is False:
        out.update(state="together", available=True, why=memo.get("why") or "The laptops are together.")
    elif age is not None:
        out["why"] = memo.get("why") or "Can't tell where the other laptop is."
    return out


APART_START = ("Shared mouse and keyboard stays off while the computers are apart. "
               "Remote control still works.")


def with_switch(sharing, config):
    """The same projection plus whether lan_mouse.off_when_apart is on (default on)."""
    out = dict(sharing or {})
    out["enforced"] = (config or {}).get("lan_mouse", {}).get("off_when_apart", True) is not False
    return out


def start_blocked(state, config=None, now=None):
    """A sentence when Lan Mouse must not be started, else None.
    A fresh apart reading always blocks. A recorded hand start also blocks while the settled
    flag still says apart, so nothing turns it back on from a stale screen. Unknown does not
    block: a 'can't tell' changes nothing, and a stale apart memo does not trap a manual start.
    Pair is stricter (pair_blocked): it also refuses a stale settled apart, before any write.
    off_when_apart false blocks nothing."""
    if not state:
        return None
    if config is not None and (config.get("lan_mouse") or {}).get("off_when_apart", True) is False:
        return None
    avail = sharing_availability(state, now=now)
    if avail["state"] == "apart":
        if avail.get("override"):
            return ("Lan Mouse was started by hand and the computers are still apart, "
                    "so it is not turned back on. Remote control still works.")
        return APART_START
    if avail.get("settled") == "apart" and avail.get("override"):
        return ("Lan Mouse was started by hand and the computers still read apart, "
                "so it is not turned back on. Remote control still works.")
    return None


def pair_blocked(state, config=None, now=None):
    """A sentence when pairing must not write either Lan Mouse config, else None.

    Uses sharing_availability, the same projection as start and the clipboard.
    A fresh apart reading and a stale settled apart both refuse, before any local
    or peer write and before a daemon stop or start. Start stays narrower: unknown
    does not block a manual start. Pair would install a pairing and can restart a
    running daemon, so a stale apart is enough. off_when_apart false refuses nothing."""
    if not state:
        return None
    if config is not None and (config.get("lan_mouse") or {}).get("off_when_apart", True) is False:
        return None
    avail = sharing_availability(state, now=now)
    if avail.get("state") != "apart" and avail.get("settled") != "apart":
        return None
    if avail.get("state") != "apart":
        return ("Shared mouse and keyboard stays unpaired while the last reading "
                "still says the computers are apart. Nothing was written. "
                "Remote control still works.")
    return APART_START


def present_input_link(level, lines, sharing):
    """Fold the guard projection into the Machines link text. Does not read the network."""
    sharing = dict(sharing or {})
    lines = [str(line) for line in (lines or [])]
    if sharing.get("enforced") is False or sharing.get("state") not in ("apart", "unknown"):
        return level, lines, sharing
    if sharing.get("state") == "unknown":
        why = sharing.get("why") or "no reading yet"
        note = f"Can't tell whether the computers are on the same network: {why}"
        if note not in lines:
            lines = [note] + lines
        return level, lines, sharing
    kept = [line for line in lines if "isn't running" not in line and "isn't holding UDP" not in line]
    up = any(("Lan Mouse" in line and "are up" in line) or "Lan Mouse is up" in line for line in kept)
    if up:
        note = ("The computers are apart. Shared mouse and keyboard is still running and will not "
                "be started again if it stops. Remote control still works.")
        if level == "up":
            level = "warn"
    else:
        note = APART_START
        # "Not running" is expected while apart. Any other line that was already there
        # (the peer is unreachable, SSH failed) keeps the stronger level.
        if level == "down" and not kept:
            level = "warn"
    return level, [note] + [line for line in kept if line != note], sharing


def guard_for(config, state):
    """guard() with this machine's settings: lan_mouse.autostart (default on) brings sharing
    up after each boot once the laptops read together."""
    from .platforms import boot_time
    settings = config.get("lan_mouse") or {}
    # lan_mouse.enabled false: switched off until David turns it back on. The guard still
    # stops a running daemon when apart, but nothing here ever starts it.
    enabled = settings.get("enabled", True) is not False
    on = bool(settings.get("autostart", True)) and enabled
    try:
        boot = boot_time()
    except Exception:
        boot = None
    return guard(state, autostart=on, boot=boot, allow_start=enabled)


def validate_pairing(text):
    """Reject incomplete or VPN-routed configs before touching the known-good file."""
    import ipaddress
    if tomllib is None:
        raise ValueError("Safe pairing requires Python 3.11+ (tomllib).")
    data = tomllib.loads(text)
    clients = data.get("clients") or []
    trusted = data.get("authorized_fingerprints") or {}
    if not clients or not trusted:
        raise ValueError("Pairing must keep a peer and its authorized fingerprint.")
    if any(not re.fullmatch(r"(?:[0-9a-f]{2}:){31}[0-9a-f]{2}", fp) for fp in trusted):
        raise ValueError("Invalid authorized fingerprint.")
    for client in clients:
        name = client.get("hostname")
        if not name or name not in trusted.values() or not client.get("ips"):
            raise ValueError("Pairing must keep the peer, fingerprint and nonempty IPs.")
        for ip in client["ips"]:
            addr = ipaddress.ip_address(ip)
            if addr in ipaddress.ip_network("100.64.0.0/10") or addr in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
                raise ValueError("Tailscale addresses cannot carry shared input.")
    return data


def write_pairing(peer_name, peer_ips, position, peer_fingerprint, path=None, state=None, config=None):
    from .locking import exclusive_lock
    target = Path(path or config_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(target.with_name(target.name + ".lock")):
        return _write_pairing(peer_name, peer_ips, position, peer_fingerprint, target, state, config)


def _write_pairing(peer_name, peer_ips, position, peer_fingerprint, path=None, state=None, config=None):
    """Write this computer's config for one peer; the old file is kept as a dated backup.
    The release shortcut, port and motion_scale already chosen here carry over."""
    import time
    from .desktop import lan_mouse_config
    from .fsio import atomic_write
    path = Path(path or config_path())
    if path.is_symlink():
        raise ValueError("Lan Mouse config is a link; nothing was written.")
    old = read_config(path) or {}
    backend = old.get("capture_backend") or ("layer-shell" if _desktop() == "hyprland" else None)
    text = lan_mouse_config(peer_name, peer_ips, position, peer_fingerprint,
                            release_bind=old.get("release_bind") or
                            ("KeyLeftCtrl", "KeyLeftShift", "KeyLeftMeta", "KeyLeftAlt"),
                            port=old.get("port") or 4242, capture_backend=backend,
                            motion_scale=old.get("motion_scale"))
    validate_pairing(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        backup = path.with_name(f"{path.name}.bak-{time.time_ns()}")
        atomic_write(backup, path.read_bytes(), mode=0o600, fsync=True)
    atomic_write(path, text, mode=0o600, fsync=True)
    restarted = False
    if lan_mouse_status()["running"]:  # it reads the config only at startup
        # Fresh or stale apart: leave the running daemon. Stopping it here and starting
        # it again would turn sharing back on while the last reading says apart.
        if not pair_blocked(state, config):
            stop()
            import time as _time
            for _ in range(20):  # stop() only signals; start() would see the dying daemon and skip
                if not lan_mouse_status()["running"]:
                    break
                _time.sleep(0.25)
            restarted = bool(start().get("started"))
    return {"written": str(path), "backup": str(backup) if backup else None, "restarted": restarted}


PAIR_KEYS = ("peer_name", "peer_ips", "position", "peer_fingerprint")


def control(arg, state=None, config=None, now=None):
    """The `lan_mouse` desktop action: (ok, json text). arg: start, stop, status, or
    {"pair": {peer_name, peer_ips, position, peer_fingerprint}} as JSON.
    A start while the guard's projection says apart is refused. Stop, status and an
    unknown reading are unchanged. Remote control does not come through here."""
    try:
        if arg == "start":
            blocked = start_blocked(state, config, now=now)
            if blocked:
                return False, blocked
            result = start()
            if state and result.get("running"):
                update_guard_memo(state, manual_stop_at=None, pending_start=False)
        elif arg == "stop":
            if state:
                import time
                # Record intent BEFORE the action; a crash cannot turn the stop into a retry.
                if not update_guard_memo(state, manual_stop_at=time.time() if now is None else now,
                                         stopped_by_guard=False, override=False):
                    raise RuntimeError("Guard busy; manual stop was not recorded. Retry stop.")
            result = stop()
        elif arg == "status":
            result = {**lan_mouse_status(), "config": read_config(),
                      "guard": sharing_availability(state, now=now)}
        else:
            request = json.loads(arg)
            spec = request.get("pair") if isinstance(request, dict) else None
            if not isinstance(spec, dict) or set(spec) != set(PAIR_KEYS):
                raise ValueError("Use start, stop, status or a pairing request.")
            result = write_pairing(*(spec[k] for k in PAIR_KEYS), state=state, config=config)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return True, json.dumps(result)


def pair(config, state, device, position, remote=None):
    """Pair this computer's Lan Mouse with `device`, which sits on our `position` side:
    exchange certificate fingerprints and addresses over the cluster (or SSH), then
    have each side write its own config. Both must have run Lan Mouse once.
    Refuses before either write while the computers read apart, fresh or stale."""
    from .remote import Remote
    if position not in OPPOSITE:
        raise ValueError(f"position must be one of {', '.join(OPPOSITE)}.")
    blocked = pair_blocked(state, config)
    if blocked:
        raise RuntimeError(blocked)
    remote = remote or Remote(config, state)
    ok, text = remote.call(device, "input_info", "")
    if not ok:
        raise RuntimeError(f"{device} didn't answer: {text}")
    theirs, mine = json.loads(text), local_info()
    for who, info in ((device, theirs), ("this computer", mine)):
        if not (info.get("lan_mouse") or {}).get("fingerprint"):
            raise ValueError(f"Lan Mouse has no certificate on {who} yet; start it there once "
                             "(jarvis lanmouse start), then pair again.")
    ok, text = remote.call(device, "lan_mouse", json.dumps({"pair": {
        "peer_name": mine["hostname"].split(".")[0], "peer_ips": mine["addresses"],
        "position": OPPOSITE[position], "peer_fingerprint": mine["lan_mouse"]["fingerprint"]}}))
    if not ok:
        raise RuntimeError(f"{device} couldn't write its Lan Mouse config: {text}")
    there = json.loads(text)
    here = write_pairing(theirs["hostname"].split(".")[0], theirs["addresses"], position,
                         theirs["lan_mouse"]["fingerprint"], state=state, config=config)
    return {"device": device, "position": position, "here": here, "there": there}


# -- both computers ----------------------------------------------------------------------------
def ppi(monitor, inches):
    """Pixels per inch along the diagonal; None if the screen size isn't configured."""
    if not monitor or not inches:
        return None
    return math.hypot(monitor["width"], monitor["height"]) / float(inches)


def _main_monitor(info):
    listed = (info or {}).get("monitors") or []
    return next((m for m in listed if m.get("primary")), listed[0] if listed else None)


def _host(name):
    """Hostnames compare without case or domain: "archmain.local" == "archmain"."""
    return str(name or "").strip().lower().split(".")[0]


def checks(devices):
    """Cross-check the computers' Lan Mouse configs: [{level, text}] (ok / warn / down)."""
    out = []
    by_host = {_host((d.get("info") or {}).get("hostname")): d for d in devices}
    for device in devices:
        info, name = device.get("info"), device.get("name") or device["label"]
        if not info:
            out.append({"level": "down", "text": f"{name} didn't answer: {device.get('error') or 'unreachable'}."})
            continue
        lm = info["lan_mouse"]
        if not lm.get("running"):
            out.append({"level": "warn", "text": f"Lan Mouse isn't running on {name}."})
        elif lm.get("build") == "official":
            out.append({"level": "warn", "text": f"{name} runs the official Lan Mouse build, without the JARVIS patches."})
        config = lm.get("config")
        if not config:
            out.append({"level": "down", "text": f"{name} has no Lan Mouse config."})
            continue
        for client in config["clients"]:
            peer = by_host.get(_host(client.get("hostname")))
            if peer is None or not peer.get("info"):
                continue
            peer_info, peer_name = peer["info"], peer.get("name") or peer["label"]
            live = set(peer_info.get("addresses") or [])
            stale = [ip for ip in client["ips"] if ip not in live]
            if stale and live:
                out.append({"level": "warn", "text": f"{name} looks for {peer_name} at {', '.join(stale)}, "
                            f"which {peer_name} no longer has (it has {', '.join(sorted(live))})."})
            elif not stale:
                out.append({"level": "ok", "text": f"{name} has {peer_name}'s current addresses."})
            back = next((c for c in (peer_info["lan_mouse"].get("config") or {}).get("clients", [])
                         if _host(c.get("hostname")) == _host(info.get("hostname"))), None)
            if back and OPPOSITE.get(client.get("position")) != back.get("position"):
                out.append({"level": "down", "text": f"{name} puts {peer_name} on its {client.get('position')}, "
                            f"but {peer_name} puts {name} on its {back.get('position')}."})
            mine, theirs = device.get("ppi"), peer.get("ppi")
            if mine and theirs and config.get("motion_scale"):
                want = mine / theirs
                off = abs(config["motion_scale"] - want) / want
                out.append({"level": "ok" if off <= SCALE_TOLERANCE else "warn",
                            "text": f"{name} motion scale {config['motion_scale']:g} "
                                    + ("matches" if off <= SCALE_TOLERANCE else f"should be about {want:.3f} to match")
                                    + f" {peer_name}'s pixel density."})
    binds = {tuple(((d.get("info") or {}).get("lan_mouse", {}).get("config") or {}).get("release_bind", []))
             for d in devices if d.get("info")}
    binds.discard(())
    if len(binds) > 1:
        out.append({"level": "warn", "text": "The computers use different release shortcuts."})
    return out


def overview(config, state, remote=None):
    """The Settings view's data: every configured computer, its screens and Lan Mouse, and
    the cross-checks. Remote computers report through the input_info action."""
    from .remote import Remote
    remote = remote or Remote(config, state)
    here = remote.here()
    devices = []
    for key, device in (config.get("devices") or {}).items():
        label = device.get("label") or key
        entry = {"id": key, "label": label, "name": label.split(",")[0].strip(), "here": key == here,
                 "screen_inches": device.get("screen_inches"), "info": None, "error": None}
        try:
            if key == here:
                entry["info"] = local_info()
            else:
                ok, text = remote.call(key, "input_info", "")
                info = json.loads(text) if ok else None
                entry["info"] = info if isinstance(info, dict) else None
                if ok and entry["info"] is None:
                    entry["error"] = "answered with something other than a report"
                entry["error"] = entry.get("error") or (None if ok else text)
        except (ValueError, OSError) as exc:
            entry["error"] = str(exc)
        main = _main_monitor(entry["info"])
        entry["ppi"] = ppi(main, entry["screen_inches"])
        devices.append(entry)
    # Arrange around this computer using its own config: where each peer sits.
    layout = {here: "center"} if here else {}
    own = next((d for d in devices if d["here"] and d["info"]), None)
    for client in ((own or {}).get("info", {}).get("lan_mouse", {}).get("config") or {}).get("clients", []):
        peer = next((d for d in devices if _host((d.get("info") or {}).get("hostname"))
                     == _host(client.get("hostname"))), None)
        if peer:
            layout[peer["id"]] = client.get("position")
    sharing = with_switch(sharing_availability(state), config)
    found = checks(devices)
    if sharing["state"] == "apart" and sharing.get("enforced") is not False:  # meant to be off: not a fault
        found = [c for c in found if not (c["level"] == "warn" and "isn't running on" in c["text"])]
    return {"here": here, "devices": devices, "layout": layout, "checks": found, "sharing": sharing}
