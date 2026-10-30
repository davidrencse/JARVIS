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

from . import platforms

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
SCALE_TOLERANCE = 0.03  # relative error in motion_scale that still feels identical
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(*args, timeout=5):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=FLAGS)


# -- this computer ---------------------------------------------------------------------------
def _desktop():
    """platforms.detect()'s desktop family; os.name == "nt" always wins (checked by callers)."""
    return platforms.detect()["desktop"]


def config_path():
    if _desktop() == "macos":  # never true on Windows, so that branch below is unchanged
        return Path.home() / "Library" / "Application Support" / "lan-mouse" / "config.toml"
    if os.name == "nt":
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
    if os.name == "nt":
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


def lan_mouse_status():
    """{running, build} where build is patched (JARVIS), official, or None when stopped."""
    if _desktop() == "macos":
        if shutil.which("pgrep") and _run("pgrep", "-x", "lan-mouse").returncode == 0:
            return {"running": True, "build": "official"}  # the JARVIS patches target Windows/Arch
        return {"running": False, "build": None}
    if os.name == "nt":
        out = _run("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-CimInstance Win32_Process -Filter \"Name='lan-mouse.exe'\" | "
                   "Select-Object -First 1 -ExpandProperty ExecutablePath", timeout=10)
        path = out.stdout.strip()
        if not path:
            return {"running": False, "build": None}
        return {"running": True,
                "build": "patched" if "\\lan-mouse\\bin\\" in path.lower().replace("/", "\\")
                else "official"}
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
    elif os.name != "nt" and shutil.which("ip"):
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
    return {"hostname": socket.gethostname(), "os": "windows" if os.name == "nt" else platforms.detect()["os"],
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


def _units():
    """The JARVIS systemd user units that exist here (none on a hand-made setup)."""
    if not shutil.which("systemctl"):
        return []
    listed = _run("systemctl", "--user", "list-unit-files", "--no-legend", *UNITS, "lan-mouse-fallback.service").stdout
    return [line.split()[0] for line in listed.splitlines() if line.strip()]


def _windows_launcher():
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / STARTUP_NAME)


def _binary():
    if os.name == "nt":
        patched = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "lan-mouse" / "bin" / "lan-mouse.exe"
        return str(patched) if patched.is_file() else shutil.which("lan-mouse")
    patched = Path.home() / ".local" / "bin" / "lan-mouse-jarvis"
    return str(patched) if patched.is_file() else shutil.which("lan-mouse")


def start():
    if lan_mouse_status()["running"]:
        return {"started": False, "note": "Lan Mouse is already running.", **lan_mouse_status()}
    if _desktop() == "macos":
        raise ValueError("Start Lan Mouse from its app on macOS.")
    units = _units()
    if os.name != "nt" and "lan-mouse.service" in units:
        wanted = [u for u in UNITS if u in units]
        done = _run("systemctl", "--user", "start", *wanted, timeout=30)
        if done.returncode:
            raise RuntimeError("systemctl couldn't start Lan Mouse: " + (done.stderr.strip()[-200:] or "no detail"))
    elif os.name == "nt" and _windows_launcher().is_file():
        # The startup launcher also raises its priority and keeps its log, as at login.
        subprocess.Popen(["wscript.exe", str(_windows_launcher())], creationflags=FLAGS, close_fds=True)
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
        status = lan_mouse_status()
        if status["running"]:
            return {"started": True, **status}
    return {"started": False, "note": "Lan Mouse didn't come up; check its log.", **lan_mouse_status()}


def stop():
    if os.name == "nt":
        _run("taskkill", "/IM", "lan-mouse.exe", "/F", timeout=15)
    else:
        units = _units()
        if units:
            _run("systemctl", "--user", "stop", *units, timeout=30)
        if shutil.which("pkill"):
            _run("pkill", "-x", "lan-mouse")
            _run("pkill", "-x", "lan-mouse-jarvi")
    return {"stopped": not lan_mouse_status()["running"], **lan_mouse_status()}


def write_pairing(peer_name, peer_ips, position, peer_fingerprint, path=None):
    """Write this computer's config for one peer; the old file is kept as a dated backup.
    The release shortcut, port and motion_scale already chosen here carry over."""
    import time
    from .desktop import lan_mouse_config
    path = Path(path or config_path())
    old = read_config(path) or {}
    backend = old.get("capture_backend") or ("layer-shell" if _desktop() == "hyprland" else None)
    text = lan_mouse_config(peer_name, peer_ips, position, peer_fingerprint,
                            release_bind=old.get("release_bind") or
                            ("KeyLeftCtrl", "KeyLeftShift", "KeyLeftMeta", "KeyLeftAlt"),
                            port=old.get("port") or 4242, capture_backend=backend,
                            motion_scale=old.get("motion_scale"))
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_bytes(path.read_bytes())
    path.write_text(text, encoding="utf-8")
    restarted = False
    if lan_mouse_status()["running"]:  # it reads the config only at startup
        stop()
        restarted = bool(start().get("running"))
    return {"written": str(path), "backup": str(backup) if backup else None, "restarted": restarted}


PAIR_KEYS = ("peer_name", "peer_ips", "position", "peer_fingerprint")


def control(arg):
    """The `lan_mouse` desktop action: (ok, json text). arg: start, stop, status, or
    {"pair": {peer_name, peer_ips, position, peer_fingerprint}} as JSON."""
    try:
        if arg in ("start", "stop"):
            result = start() if arg == "start" else stop()
        elif arg == "status":
            result = {**lan_mouse_status(), "config": read_config()}
        else:
            request = json.loads(arg)
            spec = request.get("pair") if isinstance(request, dict) else None
            if not isinstance(spec, dict) or set(spec) != set(PAIR_KEYS):
                raise ValueError("Use start, stop, status or a pairing request.")
            result = write_pairing(*(spec[k] for k in PAIR_KEYS))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return True, json.dumps(result)


def pair(config, state, device, position, remote=None):
    """Pair this computer's Lan Mouse with `device`, which sits on our `position` side:
    exchange certificate fingerprints and addresses over the cluster (or SSH), then
    have each side write its own config. Both must have run Lan Mouse once."""
    from .remote import Remote
    if position not in OPPOSITE:
        raise ValueError(f"position must be one of {', '.join(OPPOSITE)}.")
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
                         theirs["lan_mouse"]["fingerprint"])
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
    return {"here": here, "devices": devices, "layout": layout, "checks": checks(devices)}
