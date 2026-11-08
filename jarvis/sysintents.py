"""System intents: brightness, screenshot, system_status, workspace, lock_screen,
reminder and weather. Typed and narrow, run directly like `volume` and `media` (not in the
terminal sandbox, which hides Wayland, Hyprland's socket and D-Bus on purpose).

Each executor takes a JSON spec string (or a dict) and returns (ok, text). Subprocesses go
through `_run` and the network through `_fetch`, so the tests replace both. Linux/Hyprland is
the real backend; where a feature has none the answer says so plainly instead of guessing.

    screenshot  only under ~/Pictures, ~/Downloads, ~/Desktop, ~/Documents; the reply names the
                saved path and claims success only when that file exists and is not empty.
    reminder    an in-process timer (threading.Timer) that fires notify-send. It lives as long
                as the JARVIS process that scheduled it: no daemon, no systemd unit.
"""

import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import platforms

BRIGHTNESS_STEP = 10
SCREENSHOT_ROOTS = ("Pictures", "Downloads", "Desktop", "Documents")
SCREENSHOT_SUFFIXES = (".png", ".jpg", ".jpeg")
STATUS_WHAT = ("battery", "wifi", "bluetooth", "windows", "disk", "all")
MAX_WINDOWS = 15
MAX_REMINDER_MINUTES = 24 * 60
MAX_REMINDERS = 20
WEATHER_DAYS = ("today", "tomorrow")
POWER_SUPPLY = Path("/sys/class/power_supply")
_LEVEL = re.compile(r"(?:up|down|\d{1,3})\Z")


def _run(*args, timeout=10):
    return platforms.run_quiet(args, timeout)


def _linux():
    return platforms.detect()["os"] == "linux"


def _spec(spec):
    """The dict a spec string or dict holds; ValueError when it is neither."""
    if isinstance(spec, dict):
        return spec
    text = str(spec or "").strip()
    if not text:
        return {}
    data = json.loads(text) if text.startswith("{") else None
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _unsupported(what):
    return False, f"{what} isn't supported on this computer's OS yet."


def _missing(tool, what):
    return False, f"{what} needs {tool}, which isn't installed here."


# -- brightness ------------------------------------------------------------------------

def valid_level(value):
    """up, down or a whole percent 0-100, as a string or int (never a bool)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    text = str(value).strip().lower().rstrip("%").strip()
    return bool(_LEVEL.match(text)) and (not text.isdigit() or int(text) <= 100)


def _brightness_now():
    """(percent, device) from `brightnessctl -m`, else None."""
    out = _run("brightnessctl", "-m", "-c", "backlight")
    if out.returncode:
        return None
    parts = (out.stdout.strip().splitlines() or [""])[0].split(",")
    if len(parts) >= 4 and parts[3].rstrip("%").isdigit():
        return int(parts[3].rstrip("%")), parts[0]
    return None


def brightness(spec):
    try:
        args = _spec(spec)
    except ValueError:
        args = {"level": str(spec)}
    level = args.get("level")
    if not valid_level(level):
        return False, "Brightness takes up, down or a level from 0 to 100."
    level = str(level).strip().lower().rstrip("%").strip()
    if not _linux():
        return _unsupported("Brightness control")
    if not shutil.which("brightnessctl"):
        return _missing("brightnessctl", "Brightness control")
    # -n keeps the panel at its lowest visible level: 0 percent must not black the screen out.
    target = {"up": f"+{BRIGHTNESS_STEP}%", "down": f"{BRIGHTNESS_STEP}%-"}.get(level, f"{level}%")
    out = _run("brightnessctl", "-c", "backlight", "-n", "set", target)
    if out.returncode:
        return False, out.stderr.strip() or "brightnessctl failed."
    now = _brightness_now()
    if now is None:
        return False, "Ran brightnessctl but couldn't read the brightness back."
    return True, f"Brightness is {now[0]} percent."


# -- screenshot ------------------------------------------------------------------------

def screenshot_path(path=None, home=None, now=None):
    """(Path, None) for a screenshot target inside an allowed folder, else (None, why).
    Symlinks and .. are resolved first; an existing file is never overwritten."""
    home = Path(home or Path.home()).resolve()
    if not path:
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        path = home / "Pictures" / "Screenshots" / f"jarvis-{stamp}.png"
    if not isinstance(path, (str, os.PathLike)) or "\0" in str(path):
        return None, "That isn't a valid path."
    text = str(path)
    target = home / text[2:] if text.startswith(("~/", "~\\")) else Path(text)
    if text == "~":
        target = home
    if not target.is_absolute():
        target = home / target
    target = Path(os.path.realpath(target))
    roots = [(home / name).resolve() for name in SCREENSHOT_ROOTS]
    if not any(target == root or root in target.parents for root in roots) or target in roots:
        return None, ("I only save screenshots under Pictures, Downloads, Desktop or Documents; "
                      f"{target} is outside those.")
    from .gates import _abs, _under, _vault_paths, local_config
    for vault in _vault_paths(local_config()):
        if _under(str(target).replace("\\", "/"), _abs(vault, "/")):
            return None, "The Obsidian vault is read-only; I won't save a screenshot there."
    if target.suffix.lower() not in SCREENSHOT_SUFFIXES:
        return None, "Screenshots are saved as .png, .jpg or .jpeg."
    if target.exists():
        return None, f"{target} already exists; I won't overwrite it."
    return target, None


def _window_geometry():
    """"x,y wxh" for grim -g from Hyprland's active window, else None."""
    out = _run("hyprctl", "activewindow", "-j")
    try:
        window = json.loads(out.stdout or "{}")
        (x, y), (w, h) = window["at"], window["size"]
        if out.returncode or int(w) <= 0 or int(h) <= 0:
            return None
        return f"{int(x)},{int(y)} {int(w)}x{int(h)}"
    except (ValueError, KeyError, TypeError):
        return None


def screenshot(spec, home=None):
    try:
        args = _spec(spec)
    except ValueError:
        args = {"path": str(spec)}
    window = args.get("window") in (True, "true", "window", "active")
    target, why = screenshot_path(args.get("path"), home)
    if why:
        return False, why
    if not _linux():
        return _unsupported("Screenshots")
    if not shutil.which("grim"):
        return _missing("grim", "Screenshots")
    command = ["grim"]
    if window:
        if not shutil.which("hyprctl"):
            return _missing("hyprctl (Hyprland)", "Window screenshots")
        region = _window_geometry()
        if not region:
            return False, "Couldn't read the active window's position, so I took nothing."
        command += ["-g", region]
    if target.suffix.lower() != ".png":
        command += ["-t", "jpeg"]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Couldn't create {target.parent}: {exc.strerror or exc}."
    out = _run(*command, str(target))
    if out.returncode:
        return False, out.stderr.strip() or "grim failed."
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    if size <= 0:
        return False, f"grim ran but {target} wasn't saved (no file or an empty one)."
    return True, f"Saved {'the active window' if window else 'the screen'} to {target}."


# -- system status (read-only) ---------------------------------------------------------

def _battery(root=None):
    root = Path(root or POWER_SUPPLY)
    try:
        for supply in sorted(root.glob("BAT*")):
            percent = (supply / "capacity").read_text().strip()
            state = (supply / "status").read_text().strip().lower()
            if percent.isdigit():
                charging = {"charging": "charging", "full": "full and plugged in",
                            "discharging": "on battery", "not charging": "plugged in, not charging"}
                return True, f"Battery {percent} percent, {charging.get(state, state)}."
    except OSError:
        pass
    return False, "No battery found."


def _wifi():
    if not shutil.which("nmcli"):
        return False, "Wi-Fi status needs nmcli (NetworkManager)."
    # --rescan no: the cached scan answers in ~0.1 s; a plain `dev wifi` rescans (5-8 s).
    out = _run("nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi", "list", "--rescan", "no")
    if out.returncode:
        return False, out.stderr.strip() or "nmcli failed."
    for line in out.stdout.splitlines():
        if line.startswith("yes:"):
            ssid = line[4:].replace("\\:", ":").replace("\\\\", "\\")
            return True, f"Wi-Fi connected to {ssid}."
    return True, "Wi-Fi isn't connected."


def _bluetooth():
    if not shutil.which("bluetoothctl"):
        return False, "Bluetooth status needs bluetoothctl."
    out = _run("bluetoothctl", "show")
    if out.returncode or "Powered:" not in out.stdout:
        return False, "No Bluetooth adapter answered."
    powered = re.search(r"Powered:\s*(\w+)", out.stdout).group(1).lower() == "yes"
    if not powered:
        return True, "Bluetooth is off."
    connected = [name for _, name in _devices("Connected")]
    return True, "Bluetooth is on." + (f" Connected: {', '.join(connected)}." if connected else "")


def _windows():
    if not shutil.which("hyprctl"):
        return False, "Listing windows needs Hyprland (hyprctl)."
    out = _run("hyprctl", "clients", "-j")
    try:
        clients = json.loads(out.stdout or "[]")
        if out.returncode or not isinstance(clients, list):
            raise ValueError
    except ValueError:
        return False, "Couldn't read the window list from Hyprland."
    shown = [c for c in clients if isinstance(c, dict) and c.get("mapped", True)]
    names = [f"{str(c.get('class') or '?')[:40]}: {str(c.get('title') or '').replace(chr(10), ' ')[:80]}"
             for c in shown[:MAX_WINDOWS]]  # titles are page text: data, shortened
    if not names:
        return True, "No windows are open."
    more = f" (and {len(shown) - MAX_WINDOWS} more)" if len(shown) > MAX_WINDOWS else ""
    return True, f"{len(shown)} windows open: " + "; ".join(names) + more + "."


def _disk(path=None):
    """Free space on the drive holding the home folder (shutil, no subprocess)."""
    try:
        usage = shutil.disk_usage(path or Path.home())
    except OSError as exc:
        return False, f"Couldn't read disk space: {exc}"
    gb = 1024 ** 3
    return True, (f"{usage.free / gb:.0f} GB free of {usage.total / gb:.0f} GB "
                  f"({usage.free * 100 // max(usage.total, 1)} percent free).")


def system_status(spec):
    try:
        what = str(_spec(spec).get("what") or "all").strip().lower()
    except ValueError:
        what = str(spec).strip().lower() or "all"
    if what not in STATUS_WHAT:
        return False, "Status takes battery, wifi, bluetooth, windows, disk or all."
    if what == "disk":  # any OS: no subprocess, so it works on the Windows nodes too
        return _disk()
    if not _linux():
        return _unsupported("System status")
    readers = {"battery": _battery, "wifi": _wifi, "bluetooth": _bluetooth, "windows": _windows}
    chosen = list(readers) if what == "all" else [what]
    results = [readers[name]() for name in chosen]
    return any(ok for ok, _ in results), " ".join(text for _, text in results)


# -- workspace and lock ----------------------------------------------------------------

def valid_workspace(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    return str(value).strip().isdigit() and 1 <= int(str(value).strip()) <= 99


def _active_workspace():
    try:
        return int(json.loads(_run("hyprctl", "activeworkspace", "-j").stdout or "{}")["id"])
    except (ValueError, KeyError, TypeError):
        return None


def workspace(spec):
    try:
        number = _spec(spec).get("n")
    except ValueError:
        number = spec
    if not valid_workspace(number):
        return False, "Workspace takes a number from 1 to 99."
    number = int(str(number).strip())
    if not _linux() or not shutil.which("hyprctl"):
        return _unsupported("Switching workspaces (Hyprland only)")
    # Hyprland 0.56+ takes Lua dispatchers; older ones the classic syntax. Each is read back.
    for command in ([f"hl.dsp.focus({{ workspace = {number} }})"], ["workspace", str(number)]):
        if _run("hyprctl", "dispatch", *command).stdout.strip() == "ok":
            time.sleep(0.15)
            if _active_workspace() == number:
                return True, f"Switched to workspace {number}."
    return False, f"Hyprland didn't move to workspace {number}."


def lock_screen(spec=None, config=None):
    if platforms.on_windows():
        out = _run("rundll32.exe", "user32.dll,LockWorkStation")
        return (not out.returncode), ("Locked the screen." if not out.returncode else "Lock failed.")
    if not _linux():
        return _unsupported("Locking the screen")
    if config is None:
        from .gates import local_config
        config = local_config()
    if (config.get("lock_screen") or {}).get("command") == "hyprlock" and shutil.which("hyprlock"):
        import subprocess  # hyprlock stays up until David unlocks: start it, don't wait for it
        try:
            subprocess.Popen(["hyprlock"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            return False, f"Couldn't start hyprlock: {exc.strerror or exc}."
        return True, "Started hyprlock."
    if not shutil.which("loginctl"):
        return _missing("loginctl", "Locking the screen")
    out = _run("loginctl", "lock-session")
    if out.returncode:
        return False, out.stderr.strip() or "loginctl lock-session failed."
    return True, "Asked the session to lock the screen."


# -- reminders -------------------------------------------------------------------------

_TIMERS = []
_TIMERS_LOCK = threading.Lock()


def valid_minutes(value):
    if isinstance(value, bool):
        return False
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return False
    return 0 < minutes <= MAX_REMINDER_MINUTES and minutes == minutes


def _notify(text):
    """notify-send for a fired reminder; (ok, why). Never raises."""
    try:
        if _linux() and shutil.which("notify-send"):
            out = _run("notify-send", "-a", "JARVIS", "-u", "normal", "JARVIS reminder", text[:300], timeout=5)
            return out.returncode == 0
        if platforms.detect()["os"] == "macos" and shutil.which("osascript"):
            safe = text[:300].replace("\\", " ").replace('"', "'")
            return _run("osascript", "-e", f'display notification "{safe}" with title "JARVIS reminder"',
                        timeout=5).returncode == 0
    except Exception:
        pass
    return False


def reminder(spec, timer=threading.Timer):
    try:
        args = _spec(spec)
    except ValueError:
        return False, "Reminder takes minutes and optional text."
    minutes = args.get("minutes")
    if not valid_minutes(minutes):
        return False, f"A reminder takes a time from a few seconds up to {MAX_REMINDER_MINUTES // 60} hours."
    text = str(args.get("text") or "").strip()[:300] or "Your timer is up."
    if not (_linux() or platforms.detect()["os"] == "macos"):
        return _unsupported("Reminders (no notifier)")
    seconds = float(minutes) * 60
    with _TIMERS_LOCK:
        _TIMERS[:] = [t for t in _TIMERS if t.is_alive()]
        if len(_TIMERS) >= MAX_REMINDERS:
            return False, f"Already holding {MAX_REMINDERS} reminders; wait for one to fire."
        pending = timer(seconds, _notify, args=(text,))
        pending.daemon = True
        pending.start()
        _TIMERS.append(pending)
    when = (datetime.now() + timedelta(seconds=seconds)).strftime("%H:%M")
    shown = f"{float(minutes):g} minute{'s' if float(minutes) != 1 else ''}"
    return True, (f"Reminder set for {when} (in {shown})"
                  + (f": {text}." if args.get("text") else ".")
                  + " It fires while JARVIS is running.")


# -- weather (read-only) ---------------------------------------------------------------

def _fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8 jarvis"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        return json.loads(response.read(500_000).decode("utf-8", "replace"))


def _city(config):
    return str(((config or {}).get("weather") or {}).get("city") or "").strip()


def valid_city(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[^\W\d_][\w .,'-]{0,59}", value.strip()))


def weather(spec, config=None, fetch=None):
    try:
        args = _spec(spec)
    except ValueError:
        args = {"city": str(spec)}
    day = str(args.get("day") or "today").lower()
    if day not in WEATHER_DAYS:
        return False, "Weather covers today or tomorrow."
    if config is None:
        from .gates import local_config
        config = local_config()
    city = str(args.get("city") or "").strip() or _city(config)
    if not city:
        return False, ("Which city? Say \"weather in <city>\", or set weather.city in "
                       "jarvis.config.json so I know where you are.")
    if not valid_city(city):
        return False, "That doesn't look like a city name."
    try:
        data = (fetch or _fetch)(f"https://wttr.in/{urllib.parse.quote(city)}?format=j1")
        now, days = data["current_condition"][0], data["weather"]
        plan = days[1 if day == "tomorrow" else 0]
        words = (now.get("weatherDesc") or [{}])[0].get("value", "").strip().lower()
        rain = max((int(h.get("chanceofrain", 0)) for h in plan.get("hourly", [])), default=0)
        hi, lo = plan["maxtempC"], plan["mintempC"]
        c, f = now["temp_C"], now["temp_F"]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        return False, f"Couldn't get the weather for {city} ({type(exc).__name__})."
    head = f"{city.title()}: {day}, high {hi}, low {lo} degrees C, {rain} percent chance of rain."
    if day == "today":
        head = f"{city.title()} now: {words}, {c} degrees C ({f} F). Today high {hi}, low {lo}, {rain} percent chance of rain."
    return True, head


# -- bluetooth ---------------------------------------------------------------------------

BT_ACTIONS = ("on", "off", "scan", "list", "connected", "connect", "disconnect", "pair", "forget")
BT_NEEDS_NAME = ("connect", "disconnect", "pair", "forget")
MAC = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\Z")
SCAN_SECONDS = 8
SEEN_TTL = 300  # pair only a device a scan saw in the last five minutes
SEEN_FILE = Path(__file__).resolve().parent.parent / ".jarvis" / "bluetooth-seen.json"
_SEEN = {}  # mac -> (name, icon, seen at); mirrored to SEEN_FILE so a later process can pair
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02")
_DEVICE = re.compile(r"Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2}) (.*)\Z")
ICON_WORDS = {"headphones": ("audio-headphones", "audio-headset"), "headset": ("audio-headset", "audio-headphones"),
              "earbuds": ("audio-headphones", "audio-headset"), "earphones": ("audio-headphones", "audio-headset"),
              "speaker": ("audio-speakers", "audio-card"), "keyboard": ("input-keyboard",),
              "mouse": ("input-mouse",), "controller": ("input-gaming",), "gamepad": ("input-gaming",)}


def valid_bt_action(value):
    return value in BT_ACTIONS


def _clean_name(text):
    return re.sub(r"[\x00-\x1f\x7f]", " ", _ANSI.sub("", str(text))).strip()[:60]


def _bt(*args, timeout=10):
    return _run("bluetoothctl", *args, timeout=timeout)


def _devices(kind=None, text=None):
    """[(mac, name)] from `bluetoothctl devices [Paired|Connected]` (or given output text).
    Only well-formed MACs survive: they are the one thing handed to a subprocess."""
    if text is None:
        out = _bt("devices", *([kind] if kind else []))
        text = out.stdout if not out.returncode else ""
    found = []
    for line in _ANSI.sub("", text).splitlines():
        match = _DEVICE.fullmatch(line.strip())
        if match:
            found.append((match.group(1), _clean_name(match.group(2))))
    return found


def _info(mac):
    """{"Name": ..., "Icon": ..., "Connected": "yes", "battery": 100} for a valid MAC, else {}."""
    if not MAC.fullmatch(mac):
        return {}
    out = _bt("info", mac)
    if out.returncode:
        return {}
    info = {}
    for line in _ANSI.sub("", out.stdout).splitlines():
        if line.startswith("\t") and ":" in line and not line.startswith("\tUUID"):
            key, _, value = line.strip().partition(":")
            info.setdefault(key, value.strip())
    battery = re.search(r"\((\d+)\)", info.get("Battery Percentage", ""))
    if battery:
        info["battery"] = int(battery.group(1))
    return info


def _powered():
    out = _bt("show")
    found = re.search(r"Powered:\s*(\w+)", out.stdout)
    return None if out.returncode or not found else found.group(1).lower() == "yes"


def _words(text):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"[-_]", " ", str(text).lower().replace("'", ""))).split()


STOP = {"my", "the", "a", "an", "to", "bluetooth", "device", "devices"}


def match_device(query, devices, icons=None):
    """Candidates among [(mac, name)] for what David called a device. Tiers, first hit wins:
    exact name; every query word in the name (typos allowed); a category word ("headphones")
    against the device's icon; a close fuzzy match of the whole name. Strict on purpose: a
    wrong guess here connects or forgets the wrong thing."""
    import difflib
    want = [w for w in _words(query) if w not in STOP]
    if not want:
        return []
    icons = icons or {}

    def close(a, b):
        return a == b or (len(a) >= 3 and b.startswith(a)) or \
            (len(a) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)
    tiers = [
        [d for d in devices if [w for w in _words(d[1]) if w not in STOP] == want],
        [d for d in devices if all(any(close(w, n) for n in _words(d[1])) for w in want)],
        [d for d in devices if len(want) == 1 and icons.get(d[0]) in ICON_WORDS.get(want[0], ())],
        [d for d in devices if difflib.SequenceMatcher(None, " ".join(want), " ".join(_words(d[1]))).ratio() >= 0.85],
    ]
    return next((t for t in tiers if t), [])


def _pick(query, devices, what, icons=None):
    """((mac, name), None) for one match, else (None, why) with the candidates listed."""
    hits = match_device(query, devices, icons)
    if len(hits) == 1:
        return hits[0], None
    if hits:
        return None, "Which one: " + ", ".join(name for _, name in hits) + "?"
    known = ", ".join(name for _, name in devices) or "none"
    return None, f"No {what} device matches {query!r}. Known: {known}."


def _load_seen():
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        for mac, (name, icon, at) in data.items():
            if MAC.fullmatch(mac):
                _SEEN.setdefault(mac, (_clean_name(name), str(icon)[:40], float(at)))
    except (OSError, ValueError, TypeError):
        pass


def _remember(found, now):
    for mac, name, icon in found:
        _SEEN[mac] = (name, icon, now)
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(json.dumps({m: list(v) for m, v in _SEEN.items() if now - v[2] < SEEN_TTL}),
                             encoding="utf-8")
    except OSError:
        pass


def _discovering():
    found = re.search(r"Discovering:\s*(\w+)", _bt("show").stdout)
    return bool(found) and found.group(1).lower() == "yes"


def _stop_discovery():
    """Make sure no discovery is left running; True once `show` says so."""
    for _ in range(5):
        try:
            _bt("scan", "off", timeout=5)
            if not _discovering():
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _bt_scan(unnamed, query):
    notes, was_off = [], _powered() is False
    if was_off:
        out = _bt("power", "on")
        if out.returncode or _powered() is not True:
            return False, "Bluetooth is off and I couldn't turn it on."
        notes.append("Bluetooth was off, so I turned it on.")
        time.sleep(1)
    try:
        out = _bt("--timeout", str(SCAN_SECONDS), "scan", "on", timeout=SCAN_SECONDS + 7)
        heard = _ANSI.sub("", out.stdout)
    except Exception as exc:
        heard = ""
        notes.append(f"The scan was cut short ({type(exc).__name__}).")
    finally:
        stopped = _stop_discovery()
    if not stopped:
        notes.append("Warning: couldn't confirm discovery stopped; run `bluetoothctl scan off`.")
    nearby = {m.group(1) for line in heard.splitlines() if re.match(r"\[(?:NEW|CHG)\] Device ", line)
              for m in [re.search(r"Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2})", line)] if m}
    names = dict(_devices())
    paired = {m for m, _ in _devices("Paired")}
    connected = {m for m, _ in _devices("Connected")}
    rows = []
    for mac in sorted(nearby):
        name = names.get(mac, "")
        if not name or name.replace("-", ":").upper() == mac:
            if unnamed:
                rows.append((mac, mac, ""))
            continue
        rows.append((mac, name, _info(mac).get("Icon", "")))
    if query:
        icons = {m: i for m, _, i in rows}
        keep = {m for m, _ in match_device(query, [(m, n) for m, n, _ in rows], icons)}
        rows = [r for r in rows if r[0] in keep]
    _remember([(m, n, i) for m, n, i in rows if n != m], time.time())
    if not rows:
        what = f"matching {query!r}" if query else "with a name"
        return True, " ".join([*notes, f"No bluetooth devices {what} found nearby."])
    shown = [f"{n}" + f" ({', '.join(x for x in (i, 'connected' if m in connected else 'paired' if m in paired else '') if x)})"
             .replace(" ()", "") for m, n, i in rows[:25]]
    return True, " ".join([*notes, f"{len(rows)} nearby: " + "; ".join(shown) + "."])


def _bt_list(only_connected):
    if only_connected:
        devices = _devices("Connected")
    else:
        devices = _devices("Paired")
    if not devices:
        return True, "No bluetooth devices are connected." if only_connected else "No paired bluetooth devices."
    connected = {m for m, _ in (devices if only_connected else _devices("Connected"))}
    rows = []
    for mac, name in devices[:20]:
        info = _info(mac)
        bits = [info.get("Icon", ""), "connected" if mac in connected else "not connected"]
        if "battery" in info:
            bits.append(f"battery {info['battery']}%")
        rows.append(f"{name} ({', '.join(b for b in bits if b)})")
    return True, ("Connected: " if only_connected else "Paired: ") + "; ".join(rows) + "."


def _wait_for(mac, key, want, tries=4):
    for _ in range(tries):
        if _info(mac).get(key) == want:
            return True
        time.sleep(0.5)
    return False


def bluetooth(spec):
    try:
        args = _spec(spec)
    except ValueError:
        args = {}
    action, name = args.get("action"), str(args.get("name") or "").strip()[:200]
    if not valid_bt_action(action):
        return False, f"Bluetooth takes one of: {', '.join(BT_ACTIONS)}."
    if action in BT_NEEDS_NAME and not name:
        return False, f"Which bluetooth device should I {action}?"
    if not _linux():
        return _unsupported("Bluetooth control")
    if not shutil.which("bluetoothctl"):
        return _missing("bluetoothctl", "Bluetooth control")
    powered = _powered()
    if powered is None:
        return False, "No Bluetooth adapter answered."
    if action in ("on", "off"):
        want = action == "on"
        if powered == want:
            return True, f"Bluetooth was already {action}."
        out = _bt("power", action)
        if out.returncode or _powered() != want:
            return False, out.stderr.strip() or f"Couldn't turn Bluetooth {action}."
        return True, f"Bluetooth is {action}."
    if action in ("list", "connected"):
        return _bt_list(action == "connected")
    if action == "scan":
        return _bt_scan(bool(args.get("unnamed")), name)
    if action == "pair":
        return _bt_pair(name, powered)
    paired = _devices("Paired")
    icons = {m: _info(m).get("Icon", "") for m, _ in paired} if name.lower() in ICON_WORDS or \
        any(w in ICON_WORDS for w in _words(name)) else {}
    device, why = _pick(name, paired, "paired", icons)
    if why:
        return False, why
    mac, label = device
    if action == "forget":
        out = _bt("remove", mac)
        if out.returncode or mac in {m for m, _ in _devices()}:
            return False, out.stderr.strip() or f"{label} is still known; it wasn't forgotten."
        return True, f"Forgot {label}."
    if not powered:
        return False, "Bluetooth is off. Turn it on first."
    want = "yes" if action == "connect" else "no"
    if _info(mac).get("Connected") == want:
        return True, f"{label} is already {'connected' if want == 'yes' else 'disconnected'}."
    out = _bt(action, mac, timeout=25)
    if _wait_for(mac, "Connected", want):  # claim success only when the device says so
        return True, f"{label} is {'connected' if want == 'yes' else 'disconnected'}."
    tail = (_ANSI.sub("", out.stdout + out.stderr).strip().splitlines() or [""])[-1][:120]
    return False, f"{label} did not {action}" + (f" ({tail})." if tail else ".")


def _bt_pair(name, powered):
    _load_seen()
    now = time.time()
    seen = [(m, v[0]) for m, v in _SEEN.items() if now - v[2] < SEEN_TTL]
    if not seen:
        return False, "I haven't seen that device in a recent scan. Say \"scan for bluetooth devices\" first."
    device, why = _pick(name, seen, "recently scanned", {m: v[1] for m, v in _SEEN.items()})
    if why:
        return False, why
    mac, label = device
    if mac in {m for m, _ in _devices("Paired")}:
        return True, f"{label} is already paired."
    if not powered:
        return False, "Bluetooth is off. Turn it on first."
    # NoInputNoOutput: "just works" pairing only. A device that wants a PIN or passkey fails
    # here, and no code is ever guessed.
    out = _bt("--agent", "NoInputNoOutput", "pair", mac, timeout=40)
    said = _ANSI.sub("", out.stdout + out.stderr)
    if "Pairing successful" not in said and _info(mac).get("Paired") != "yes":
        if re.search(r"passkey|pin|authentication|confirm", said, re.I):
            return False, (f"{label} needs a PIN or passkey, which I won't guess. "
                           "Pair it by hand in bluetoothctl or Settings.")
        return False, f"Couldn't pair {label}" + (f" ({said.strip().splitlines()[-1][:120]})." if said.strip() else ".")
    _bt("trust", mac)
    _bt("connect", mac, timeout=25)
    connected = _wait_for(mac, "Connected", "yes")
    return True, f"Paired {label}" + (" and connected." if connected else ", but it didn't connect yet.")


def run(action, spec):
    """(ok, text) for one system action; the single entry for remote.run_here and actions."""
    if action == "lock_screen":
        return lock_screen()
    return {"brightness": brightness, "screenshot": screenshot, "system_status": system_status,
            "workspace": workspace, "reminder": reminder, "weather": weather,
            "bluetooth": bluetooth}[action](spec)
