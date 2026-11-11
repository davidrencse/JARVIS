"""Where are David's computers? The typed `locate` job behind the phone's radar.

The phone asks the main; the main answers for itself (run_here) or sends a `locate` job to a
node (hub.dispatch). Each computer finds itself with what its own OS already offers:

- Linux: geoclue's `where-am-i` demo. Geoclue sends nearby Wi-Fi BSSIDs to BeaconDB
  (api.beacondb.net) and may fall back to an IP lookup; that is geoclue's call, not JARVIS's.
- Windows: .NET's GeoCoordinateWatcher from Windows PowerShell 5.1, which asks Microsoft's
  location service. Location off in Settings means no answer, never a workaround.

JARVIS itself makes no third-party calls: the network context (SSID, LAN and Tailscale
IPv4) comes from nmcli/netsh/tailscale on that machine. The brain has no tool for this, and
only the phone server starts the job; the notification is the sign on screen that it did.

    Fix = {lat, lon, accuracy_m, source: "geoclue"|"windows"|"cache", at (epoch s), error?}

A fix with an error has lat/lon None. source "cache" is the last good fix the main kept in
.jarvis/locations.json; with no coordinates it means nothing live and nothing kept.
"""

import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .fsio import atomic_write, read_json
from .platforms import NO_WINDOW, on_macos, on_windows, run_quiet
from .topology import DEVICE_ID

SOURCES = ("geoclue", "windows", "cache")
DEFAULT_TIMEOUT, MIN_TIMEOUT, MAX_TIMEOUT = 15, 5, 15
GOOD_ENOUGH_M = 500  # Wi-Fi fixes are tens to hundreds of metres; GeoIP ones are kilometres
EARTH_M = 2.1e7  # wider than half the planet means unknown (geoclue prints DBL_MAX for it)
NETWORK_WAIT = 5  # seconds the network reading may run past the location
DEMO_DIRS = ("/usr/lib/geoclue-2.0/demos", "/usr/libexec/geoclue-2.0/demos")
CACHE = "locations.json"
NOTE = "Location requested from your phone."
PRIVACY = "Location is off in Windows Settings > Privacy & security > Location."
TAILNET = ipaddress.ip_network("100.64.0.0/10")
_CACHE_LOCK = threading.Lock()  # one read-modify-write of the cache at a time
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]+")

# Windows PowerShell 5.1 only (pwsh has no System.Device). Single quotes only: Windows
# re-quotes the -Command argument, and double quotes don't survive that reliably. Numbers
# leave as invariant-culture strings so a comma-decimal locale can't bend the JSON.
WATCHER = (
    "$ErrorActionPreference = 'Stop'; $inv = [Globalization.CultureInfo]::InvariantCulture; "
    "$out = @{}; try { Add-Type -AssemblyName System.Device; "
    "$w = New-Object System.Device.Location.GeoCoordinateWatcher -ArgumentList 'High'; "
    "$until = (Get-Date).AddSeconds(__T__); "
    "[void]$w.TryStart($false, [TimeSpan]::FromSeconds(__T__)); "
    "while ($w.Position.Location.IsUnknown -and [string]$w.Permission -ne 'Denied' -and "
    "[string]$w.Status -ne 'Disabled' -and (Get-Date) -lt $until) { Start-Sleep -Milliseconds 250 }; "
    "$out.permission = [string]$w.Permission; $out.status = [string]$w.Status; "
    "$loc = $w.Position.Location; if (-not $loc.IsUnknown) { "
    "$out.lat = $loc.Latitude.ToString('R', $inv); $out.lon = $loc.Longitude.ToString('R', $inv); "
    "$out.accuracy = $loc.HorizontalAccuracy.ToString('R', $inv) }; $w.Stop(); $w.Dispose() "
    "} catch { $out.error = $_.Exception.Message }; $out | ConvertTo-Json -Compress")

# The text arrives through the environment, never spliced into the script.
BALLOON = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
    "$n.BalloonTipTitle = $env:JARVIS_NOTE_TITLE; $n.BalloonTipText = $env:JARVIS_NOTE; "
    "$n.Visible = $true; $n.ShowBalloonTip(6000); Start-Sleep -Seconds 7; $n.Dispose()")


# -- the job ----------------------------------------------------------------------------------
def validate(payload):
    """{} or {"timeout": 5-15}, nothing else (cluster.validate_action). The wait is capped
    because a node holds its one job slot while it looks."""
    if not isinstance(payload, dict) or set(payload) - {"timeout"}:
        raise ValueError("Invalid locate request.")
    if "timeout" in payload:
        wait = payload["timeout"]
        if not isinstance(wait, int) or isinstance(wait, bool) or not MIN_TIMEOUT <= wait <= MAX_TIMEOUT:
            raise ValueError(f"Locate timeout must be {MIN_TIMEOUT}-{MAX_TIMEOUT} seconds.")


def run_here(payload, config=None, state=None, *, system=None, run=None, which=None):
    """Find this computer: {fix, network}. Says so on screen first (the notification is the
    only sign a phone looked), and reads the network while the location provider works, so
    the whole job fits inside the main's wait (timeout + 10 s)."""
    validate(payload)
    wait = payload.get("timeout", DEFAULT_TIMEOUT)
    system = system or _system()
    notify(NOTE, system=system, run=run, which=which)  # run None: the real one (silent under tests)
    run, which = run or _run, which or shutil.which
    network = {}
    reader = threading.Thread(target=_read_network, args=(network, system, run, which), daemon=True)
    reader.start()
    if system == "windows":
        fix = _windows_fix(wait, run, which)
    elif system == "linux":
        fix = _geoclue_fix(wait, run, which)
    else:
        fix = _failed("cache", "Locating isn't supported on this operating system yet.")
    reader.join(NETWORK_WAIT)
    return {"fix": fix, "network": dict(network)}


def request(hub, device, timeout=DEFAULT_TIMEOUT, **here):
    """{device, fix, network} for the phone. The main answers in-process; a node gets a
    `locate` job. A failed look (offline, not enrolled, location off) still answers: with the
    last good fix marked "cache" plus the error, or with just the error. Raises ValueError
    only for a bad timeout or a device that isn't configured. `here` (run=, which=, system=)
    goes to run_here on the main, so tests never start a real location provider."""
    payload = {"timeout": timeout}
    validate(payload)
    config = hub.config
    if not isinstance(device, str) or device not in (config.get("devices") or {}):
        raise ValueError("Unknown device.")
    try:
        if device == (config.get("cluster") or {}).get("main_device"):
            answer = run_here(payload, config, hub.state, **here)
        else:
            answer = hub.dispatch(device, "locate", payload, timeout=timeout + 10)
        fix, network = _clean_answer(answer)
    except Exception as exc:  # whatever went wrong, the phone gets a sentence, not a 500
        fix, network = _failed("cache", _plain(exc) or "Couldn't reach that computer."), {}
    if _good(fix):
        _remember(hub.state, device, fix)
        return {"device": device, "fix": fix, "network": network}
    kept = last(hub.state).get(device)
    if kept:
        fix = {**kept, "error": fix.get("error") or "No live location."}
    return {"device": device, "fix": fix, "network": network}


def last(state):
    """{device: Fix} of each computer's last good fix, marked source "cache". Entries that
    don't read as a fix (a hand edit, an older format) are skipped, never trusted."""
    saved = read_json(Path(state) / CACHE, {})
    found = {}
    for device, raw in (saved.items() if isinstance(saved, dict) else ()):
        if isinstance(device, str) and DEVICE_ID.fullmatch(device):
            fix = _clean_fix(raw)
            if _good(fix):
                found[device] = {**fix, "source": "cache"}
    return found


def notify(text, title="JARVIS", *, system=None, run=None, which=None, spawn=None):
    """Best-effort sign on this computer's screen that someone looked (a phone locate, a
    camera starting). Never raises, never waits more than 3 s; off under JARVIS_NO_NOTIFY.
    Windows gets a tray balloon from a detached, hidden PowerShell. Under tests it stays
    silent unless the test hands in its own runner, like remote_view.notify_here."""
    from . import _under_tests
    if os.environ.get("JARVIS_NO_NOTIFY") or (run is None and spawn is None and _under_tests()):
        return False
    text, title = _plain(text, 200) or "JARVIS", _plain(title, 60) or "JARVIS"
    which = which or shutil.which
    try:
        if (system or _system()) == "windows":
            (spawn or subprocess.Popen)(
                [which("powershell.exe") or "powershell.exe", "-NoProfile", "-NonInteractive",
                 "-WindowStyle", "Hidden", "-Command", BALLOON],
                env={**os.environ, "JARVIS_NOTE": text, "JARVIS_NOTE_TITLE": title},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            return True
        exe = which("notify-send")
        if not exe:
            return False
        (run or _run)([exe, "-a", "JARVIS", title, text], 3)
        return True
    except Exception:
        return False


# -- Linux: geoclue ---------------------------------------------------------------------------
# [^\S\n]: spaces but never a newline, so an empty value can't swallow the next line.
_FIELD = re.compile(r"^[^\S\n]*(latitude|longitude|accuracy)[^\S\n]*:[^\S\n]*(.*?)[^\S\n]*$",
                    re.I | re.M)
_NUMBER = re.compile(r"([+\-\u2212]?)[^\S\n]*(\d+(?:[.,]\d+)?)")
# A hemisphere letter after the number ("33.86 S"), never the s of "degrees".
_SOUTH_WEST = re.compile(r"[^\S\n]*(?:\xb0|\xba|deg\w*)?[^\S\n]*(?<![A-Za-z])[SW]\b")


def parse_where_am_i(text):
    """Every fix where-am-i printed, oldest first, as (lat, lon, accuracy_m or None).
    Reads '52.520008°', '52,520008°' (comma locales), '33.86 S', 'Accuracy: 25 meters'
    and DBL_MAX accuracy (unknown); a block without both coordinates is skipped."""
    fixes = []
    for block in re.split(r"new location", text or "", flags=re.I):
        seen = {}
        for name, value in _FIELD.findall(block):
            seen[name.lower()] = _reading(value)
        lat, lon = seen.get("latitude"), seen.get("longitude")
        if _valid(lat, lon):
            fixes.append((lat, lon, _accuracy(seen.get("accuracy"))))
    return fixes


def geoclue_answer(stdout, stderr, returncode, timeout):
    """where-am-i's output -> Fix: the most accurate fix it printed (the later one on a tie),
    else a plain reason it gave none."""
    fixes = parse_where_am_i(stdout)
    if fixes:
        lat, lon, accuracy = min(reversed(fixes), key=lambda f: math.inf if f[2] is None else f[2])
        return _fix(lat, lon, accuracy, "geoclue")
    return _failed("geoclue", _geoclue_problem(f"{stdout}\n{stderr}", returncode, timeout))


def _geoclue_fix(wait, run, which):
    exe = which("where-am-i", path=os.pathsep.join(DEMO_DIRS)) or which("where-am-i")
    if not exe:
        return _failed("geoclue", "Locating this PC needs geoclue's where-am-i demo "
                                  "(it ships with the geoclue package).")
    try:  # where-am-i prints refinements until -t; the runner stops once one is good enough
        out = run([exe, "-t", str(wait), "-a", "8"], wait + 3, env=_plain_env(), enough=_settled)
    except subprocess.TimeoutExpired:
        return _failed("geoclue", f"Geoclue found no location within {wait} s.")
    except OSError as exc:
        return _failed("geoclue", f"where-am-i couldn't start: {_plain(exc)}")
    return geoclue_answer(out.stdout or "", out.stderr or "", out.returncode, wait)


def _settled(text):
    """Stop waiting once a Wi-Fi-grade fix is in; a GeoIP-wide one may still be refined."""
    return any(f[2] is not None and f[2] <= GOOD_ENOUGH_M for f in parse_where_am_i(text))


def _geoclue_problem(text, returncode, wait):
    """A plain sentence for where-am-i's refusals (it prints a GDBus error and exits)."""
    if re.search(r"no agent", text, re.I):
        return ("Geoclue refused: no location agent is running for this user. Start your "
                "desktop's geoclue agent (or /usr/lib/geoclue-2.0/demos/agent).")
    if re.search(r"\bdisabled\b", text, re.I):
        return "Location is turned off for this user in geoclue."
    if re.search(r"ServiceUnknown|not provided by any \.service", text):
        return "The geoclue service isn't installed or can't start."
    if re.search(r"AccessDenied|disallowed|rejected|not allowed", text, re.I):
        return ("Geoclue refused the location request (its agent or /etc/geoclue/geoclue.conf "
                "doesn't allow where-am-i).")
    problem = [line for line in text.splitlines() if re.search(r"critical|error|failed", line, re.I)]
    if problem:
        detail = re.sub(r"^.*?\*\*:\s*(?:[\d:.]+:\s*)?", "", problem[-1])
        detail = re.sub(r"GDBus\.Error:[\w.]+:\s*", "", detail)
        return f"Geoclue couldn't locate this PC: {_plain(detail, 160)}"
    if returncode:
        return f"where-am-i failed (exit {returncode})."
    return f"Geoclue found no location within {wait} s (is Wi-Fi on?)."


# -- Windows: GeoCoordinateWatcher ------------------------------------------------------------
def watcher_answer(text, timeout):
    """The watcher script's JSON -> Fix. Denied permission or a Disabled/NoData status all
    mean location is off in Settings; anything else without coordinates ran out of time."""
    data = None
    for line in reversed((text or "").splitlines()):
        line = line.strip(" \t\r\n\ufeff")  # PowerShell may lead with a byte-order mark
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if not isinstance(data, dict):
        return _failed("windows", "Windows didn't answer with a location.")
    if data.get("error"):
        error = str(data["error"])
        if re.search(r"access is denied|unauthorized", error, re.I):
            return _failed("windows", PRIVACY)
        if re.search(r"System\.Device", error):
            return _failed("windows", "Windows location isn't available on this PC "
                                      "(System.Device didn't load).")
        return _failed("windows", f"Windows location failed: {_plain(error, 160)}")
    lat, lon = _number(data.get("lat")), _number(data.get("lon"))
    if _valid(lat, lon):
        return _fix(lat, lon, _accuracy(data.get("accuracy")), "windows")
    if str(data.get("permission")).lower() == "denied" or \
            str(data.get("status")).lower() in ("disabled", "nodata"):
        return _failed("windows", PRIVACY)
    return _failed("windows", f"Windows found no location within {timeout} s.")


def _windows_fix(wait, run, which):
    exe = which("powershell.exe") or which("powershell") or "powershell.exe"
    try:  # PowerShell and Add-Type take a few seconds of their own before the watcher starts
        out = run([exe, "-NoProfile", "-NonInteractive", "-Command",
                   WATCHER.replace("__T__", str(wait))], wait + 6)
    except subprocess.TimeoutExpired:
        return _failed("windows", f"Windows found no location within {wait} s.")
    except OSError as exc:
        return _failed("windows", f"Windows PowerShell couldn't start: {_plain(exc)}")
    return watcher_answer(out.stdout or "", wait)


# -- network context (all local) --------------------------------------------------------------
def _read_network(out, system, run, which):
    """Fill `out` part by part, never raising: the caller takes whatever is in by the time
    the location is, so one slow command can't hold the node's job slot."""
    for key, read in (("ssid", _ssid), ("lan_ip", _lan_ip), ("tailscale_ip", _tailscale_ip)):
        try:
            value = read(system, run, which)
        except Exception:
            value = None
        if value:
            out[key] = value


def _ssid(system, run, which):
    if system == "windows":  # refused without location permission: no SSID line, no SSID
        out = run([which("netsh") or "netsh", "wlan", "show", "interfaces"], 4)
        return netsh_ssid(out.stdout or "")
    exe = which("nmcli")
    if not exe:
        return None
    # --rescan no: the cached scan answers in ~0.1 s; a rescan takes 5-8 s.
    out = run([exe, "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no"], 4,
              env=_plain_env())
    return nmcli_ssid(out.stdout or "") if out.returncode == 0 else None


def nmcli_ssid(text):
    """The active SSID from `nmcli -t -f ACTIVE,SSID ...` (terse mode escapes ':' and '\\')."""
    for line in text.splitlines():
        if line.startswith("yes:"):
            return _plain(re.sub(r"\\(.)", r"\1", line[4:]), 64) or None
    return None


def netsh_ssid(text):
    """The first connected SSID from `netsh wlan show interfaces` (BSSID lines don't match)."""
    for found in re.findall(r"^[^\S\n]*SSID[^\S\n]*:[^\S\n]*(.*?)[^\S\n]*$", text, re.M):
        if _plain(found, 64):
            return _plain(found, 64)
    return None


def _lan_ip(system=None, run=None, which=None):
    """The address of the default route (a UDP connect sends nothing), if it's a LAN one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            address = _ipv4(probe.getsockname()[0])
    except OSError:
        return None
    if not address or address in TAILNET or address.is_loopback or address.is_link_local:
        return None
    return str(address)


def _tailscale_ip(system, run, which):
    exe = which("tailscale")
    if not exe:
        return None
    out = run([exe, "ip", "-4"], 4)
    for word in (out.stdout or "").split() if out.returncode == 0 else ():
        address = _ipv4(word)
        if address and address in TAILNET:
            return str(address)
    return None


# -- shapes and checks ------------------------------------------------------------------------
def _fix(lat, lon, accuracy, source, at=None):
    return {"lat": round(lat, 6), "lon": round(lon, 6), "accuracy_m": accuracy,
            "source": source, "at": int(time.time()) if at is None else at}


def _failed(source, error):
    return {"lat": None, "lon": None, "accuracy_m": None, "source": source,
            "at": int(time.time()), "error": error}


def _good(fix):
    return isinstance(fix, dict) and not fix.get("error") and _valid(fix.get("lat"), fix.get("lon"))


def _valid(lat, lon):
    """Real coordinates; exactly 0,0 is a provider's "nothing", not a place David is."""
    return (isinstance(lat, float) and isinstance(lon, float) and -90 <= lat <= 90
            and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0))


def _clean_answer(answer):
    """A node's {fix, network} is data from another machine: keep only well-formed fields."""
    if not isinstance(answer, dict):
        return _failed("cache", "That computer's answer wasn't a location."), {}
    network = answer.get("network") if isinstance(answer.get("network"), dict) else {}
    clean = {}
    ssid = _plain(network.get("ssid"), 64) if isinstance(network.get("ssid"), str) else ""
    if ssid:
        clean["ssid"] = ssid
    for key in ("lan_ip", "tailscale_ip"):
        address = _ipv4(network.get(key))
        if address:
            clean[key] = str(address)
    return _clean_fix(answer.get("fix")), clean


def _clean_fix(raw):
    if not isinstance(raw, dict):
        return _failed("cache", "That computer's answer wasn't a location.")
    source = raw.get("source") if raw.get("source") in SOURCES else "cache"
    at = _number(raw.get("at"))
    at = int(at) if at is not None and 0 <= at < 1e11 else int(time.time())  # no stamp: as of now
    error = _plain(raw.get("error"), 200) if raw.get("error") else ""
    lat, lon = _number(raw.get("lat")), _number(raw.get("lon"))
    if error or not _valid(lat, lon):
        return {**_failed(source, error or "The answer had no usable coordinates."), "at": at}
    return _fix(lat, lon, _accuracy(raw.get("accuracy_m")), source, at)


def _remember(state, device, fix):
    """Keep the last good fix per computer, atomically, owner-only. A failed write costs
    only the "last known" fallback, so it never fails the locate."""
    path = Path(state) / CACHE
    with _CACHE_LOCK:
        saved = read_json(path, {})
        saved = saved if isinstance(saved, dict) else {}
        saved[device] = {k: fix[k] for k in ("lat", "lon", "accuracy_m", "source", "at")}
        try:
            atomic_write(path, json.dumps(saved, indent=1, sort_keys=True), mode=0o600)
        except OSError:
            pass


def _reading(text):
    """The number on a where-am-i line: comma or dot decimals, a minus (or S/W) for sign."""
    found = _NUMBER.search(text or "")
    if not found:
        return None
    value = float(found.group(2).replace(",", "."))
    if found.group(1) in ("-", "\u2212"):
        value = -value
    if _SOUTH_WEST.match(text[found.end():]):
        value = -abs(value)
    return value if math.isfinite(value) else None


def _number(value):
    """A finite float from a JSON number or a numeric string (comma decimals allowed)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace("\u2212", "-")
        value = value.replace(",", ".") if "." not in value else value
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _accuracy(value):
    value = value if isinstance(value, float) else _number(value)
    return round(value, 1) if value is not None and 0 <= value < EARTH_M else None


def _ipv4(text):
    try:
        return ipaddress.IPv4Address(str(text).strip()) if text else None
    except ValueError:
        return None


def _plain(value, limit=200):
    """One line of printable text, capped: node and OS strings reach the phone and logs."""
    return " ".join(_UNSAFE.sub(" ", str(value)).split())[:limit]


def _plain_env():
    """This environment with C numbers and untranslated messages, so the parsers read dot
    decimals and English words; the charset stays David's, so SSIDs keep their letters."""
    env = dict(os.environ)
    whole = env.pop("LC_ALL", None)
    if whole:
        env["LANG"] = whole
    env.pop("LANGUAGE", None)
    env.update(LC_NUMERIC="C", LC_MESSAGES="C")
    return env


def _system():
    return "windows" if on_windows() else "macos" if on_macos() else "linux"


def _run(args, timeout, env=None, enough=None):
    """platforms.run_quiet; given `enough`, read stdout as it comes and stop the child once
    enough(text so far) is true (where-am-i otherwise keeps printing until its own -t).
    Running out of time with `enough` returns what was read rather than raising."""
    if enough is None:
        return run_quiet(args, timeout=timeout, env=env)
    child = subprocess.Popen(list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", env=env, creationflags=NO_WINDOW)
    out, err = [], []
    pumps = [threading.Thread(target=_pump, args=pair, daemon=True)
             for pair in ((child.stdout, out), (child.stderr, err))]
    for pump in pumps:
        pump.start()
    deadline = time.monotonic() + timeout
    while child.poll() is None and time.monotonic() < deadline and not enough("".join(out)):
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    for pump in pumps:
        pump.join(1)
    return subprocess.CompletedProcess(list(args), child.returncode, "".join(out), "".join(err))


def _pump(stream, sink):
    try:
        for line in stream:
            sink.append(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
