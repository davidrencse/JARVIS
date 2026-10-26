"""Shared mouse and keyboard (Lan Mouse), read-only, for the console's Settings view.

What each computer's Lan Mouse is configured to do, its screens, and whether the two
configurations agree: which edge leads where, whether the peer addresses are current, and
whether motion_scale keeps hand travel identical across screens of different pixel
density. It reads configs, processes and monitor geometry; it never starts, stops or
edits Lan Mouse.

Each computer reports itself through the read-only `input_info` desktop action
(local_info), so the Zenbook answers over the cluster or SSH like any other action.
Certificate fingerprints and authorized keys are never read out of the configs.
"""

import json
import math
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

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
def config_path():
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
    if os.name == "nt":
        return _monitors_nt()
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
    return {"running": False, "build": None}


def addresses():
    """This computer's IPv4 addresses (LAN and Tailscale), loopback excluded."""
    found = set()
    if os.name != "nt" and shutil.which("ip"):
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
    return {"hostname": socket.gethostname(), "os": "windows" if os.name == "nt" else "linux",
            "monitors": safely(monitors, []), "addresses": safely(addresses, []),
            "lan_mouse": {**safely(lan_mouse_status, {"running": False, "build": None}),
                          "config": safely(read_config, None)}}


def run(_arg=""):
    """The read-only `input_info` desktop action: (ok, json text)."""
    return True, json.dumps(local_info())


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
