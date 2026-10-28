"""L5 observe: did an action's effect actually appear? Runs on the machine that acted.

The main sends a predicate ("a window of the app Spotify", "a window whose title names
youtube"), and this polls the desktop every 250 ms until the predicate holds or the
settle time runs out. Only {"match", "after_ms", "app"} travels back: window titles never
leave the machine (docs/jarvis-os-architecture.md, "Privacy at the source").

Windows are listed straight from Win32 (EnumWindows), Hyprland (hyprctl), sway (swaymsg),
X11 (wmctrl) or macOS (System Events through osascript), chosen by platforms.detect(), not
through PowerShell, which takes most of a second per call and is too slow to poll.
"""

import json
import os
import re
import shutil
import subprocess
import time
import ctypes
import uuid
from urllib.parse import urlsplit

from . import platforms

KINDS = ("window", "title", "focused", "gone", "playback", "volume")
MAX_SETTLE = 15.0
POLL = 0.25
# Sites whose window titles don't contain their domain name.
# JARVIS's own console runs as a Chromium --app window (class "chrome-127.0.0.1__-Default"),
# which must not count as Chrome: "close chrome" would close the console instead.
CONSOLE_CLASS = re.compile(r"^(?:chrome|chromium|msedge|brave)-(?:127\.0\.0\.1|localhost)", re.I)


def app_class(name):
    """A window class as JARVIS matches it: the console's app window becomes "localconsole"."""
    return "localconsole" if CONSOLE_CLASS.match(name or "") else (name or "")


# X's tab titles are "Home / X" (logged out: "... / X"), never "x" or "twitter": a bare "x"
# fails validate's 2-char minimum and would match any title, so the predicate is the
# "/ X" suffix, matched on the raw title (see matches) rather than flattened.
TITLE_WORDS = {"mail.google.com": "gmail", "x.com": "/ X", "twitter.com": "/ X",
               "www.youtube.com": "youtube"}

# "open <site>" is only seen in a browser window: a terminal titled "vim github.py" or the
# "- Google Chrome" tail on every Chrome title must not satisfy it.
BROWSER_APP = re.compile(r"(?:^|[\s.\-_])(?:google-?chrome|chrome|chromium|firefox|zen|"
                         r"brave|msedge|microsoft-?edge|safari|vivaldi|opera)(?:$|[\s.\-_])")
BROWSER_SUFFIX = re.compile(r"\s+[-\u2014\u2013|]\s+(?:google chrome|chromium|mozilla firefox|"
                            r"zen(?: browser)?|brave|microsoft.? edge|safari|vivaldi|opera)\b.*$", re.I)


def is_browser(app):
    app = str(app or "").lower()
    if CONSOLE_CLASS.match(app) or "localconsole" in app:  # JARVIS's own console isn't a browser
        return False
    return any(BROWSER_APP.search(token) for token in [app, *app.split()])


def page_title(title):
    """A browser window's title without the trailing browser name."""
    return BROWSER_SUFFIX.sub("", str(title or "")).strip()


def _flat(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def predicate_for(command):
    """The predicate that proves `command` worked, or None when its effect can't be seen
    as a window or through the native playback/volume state APIs."""
    intent, args = command["intent"], command["args"]
    if intent == "open_app":
        return {"kind": "window", "app": args["name"]}
    if intent == "focus_window":
        return {"kind": "focused", "app": args["name"]}
    if intent == "close_app":
        return {"kind": "gone", "app": args["name"]}
    if intent == "open_url":
        host = (urlsplit(args["url"]).hostname or "").lower()
        if not host:
            return None
        word = TITLE_WORDS.get(host) or host.removeprefix("www.").split(".")[0]
        return {"kind": "title", "text": word}
    if intent == "media":
        command = str(args.get("command", "")).lower().replace("-", "_")
        return {"kind": "playback", "command": command} if command else None
    if intent == "spotify_play":
        return {"kind": "playback", "command": "play"}
    if intent == "volume":
        level = str(args.get("level", "")).lower().rstrip("%")
        return {"kind": "volume", "level": level} if level else None
    return None


def validate(predicate):
    if not isinstance(predicate, dict) or predicate.get("kind") not in KINDS:
        raise ValueError("Unknown observe predicate.")
    if predicate["kind"] == "playback":
        from .registry import MEDIA
        if predicate.get("command") not in MEDIA:
            raise ValueError("The predicate needs a media command.")
        return predicate
    if predicate["kind"] == "volume":
        level = predicate.get("level")
        if not isinstance(level, str) or not re.fullmatch(r"up|down|mute|unmute|\d{1,3}", level) \
                or (level.isdigit() and int(level) > 100):
            raise ValueError("The predicate needs a volume level.")
        return predicate
    key = "text" if predicate["kind"] == "title" else "app"
    value = predicate.get(key)
    # "/ X" (X's title suffix) is one letter once flattened, but is matched raw, so it's valid.
    size = len(_flat(value)) + 1 if key == "text" and str(value).strip().startswith("/") \
        else len(_flat(value))
    if not isinstance(value, str) or not 2 <= size <= 100:
        raise ValueError(f"The predicate needs a {key}.")
    return predicate


# -- listing windows ----------------------------------------------------------------------
def _windows_nt():
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found, names = [], {}
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            size = user32.GetWindowTextLengthW(hwnd)
            if size:
                title = ctypes.create_unicode_buffer(size + 1)
                user32.GetWindowTextW(hwnd, title, size + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((hwnd, pid.value, title.value))
        return True

    user32.EnumWindows(proc(visit), 0)
    for _, pid, _ in found:
        if pid in names:
            continue
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        name = ""
        if handle:
            path, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                name = os.path.splitext(os.path.basename(path.value))[0]
            kernel32.CloseHandle(handle)
        names[pid] = name
    focused = user32.GetForegroundWindow()
    return [{"app": names[pid], "title": title, "focused": hwnd == focused}
            for hwnd, pid, title in found]


def _windows_hyprland():
    if not shutil.which("hyprctl"):
        raise LookupError("observing windows needs Hyprland (hyprctl)")

    def query(*args):
        out = subprocess.run(["hyprctl", *args, "-j"], capture_output=True, text=True,
                             timeout=5).stdout
        return json.loads(out or "null")

    active = (query("activewindow") or {}).get("address")
    return [{"app": f"{app_class(c.get('class'))} {app_class(c.get('initialClass'))}",
             "title": c.get("title", ""), "focused": c.get("address") == active}
            for c in query("clients") or []]


def _run(*args, timeout=5):
    """Argument-list subprocess (never a shell); no console window on Windows."""
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def backend():
    """Which desktop family to drive. A non-Windows os.name never maps to the Windows
    backend: it falls back to Hyprland, the original non-Windows behaviour."""
    if os.name == "nt":
        return "windows"
    desktop = platforms.detect()["desktop"]
    return "hyprland" if desktop == "windows" else desktop


NO_WINDOW_TOOL = ("window control needs wmctrl (X11) or a supported compositor "
                  "(Hyprland, sway): install wmctrl, or run an X11 session")


def _sway_tree(node, found):
    """Walk `swaymsg -t get_tree`: leaves with an app_id (Wayland) or window class (XWayland)."""
    app = node.get("app_id") or (node.get("window_properties") or {}).get("class")
    if app and node.get("type") in (None, "con", "floating_con"):
        found.append({"id": node.get("id"), "app": str(app), "title": node.get("name") or "",
                      "focused": bool(node.get("focused"))})
    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        _sway_tree(child, found)
    return found


def _windows_sway():
    if not shutil.which("swaymsg"):
        raise LookupError("window control needs swaymsg (sway)")
    try:
        tree = json.loads(_run("swaymsg", "-t", "get_tree").stdout or "null")
    except ValueError:
        return []
    return _sway_tree(tree, []) if isinstance(tree, dict) else []


def _windows_wmctrl():
    if not shutil.which("wmctrl"):
        raise LookupError(NO_WINDOW_TOOL)
    active = None
    if shutil.which("xdotool"):
        out = _run("xdotool", "getactivewindow")
        if not out.returncode and out.stdout.strip().isdigit():
            active = int(out.stdout.strip())
    found = []
    for line in _run("wmctrl", "-lx").stdout.splitlines():
        parts = line.split(None, 4)  # id, desktop, class, host, title
        if len(parts) < 4 or not re.fullmatch(r"0x[0-9a-fA-F]+", parts[0]):
            continue
        found.append({"id": parts[0].lower(), "app": parts[2].replace(".", " "),
                      "title": parts[4] if len(parts) > 4 else "",
                      "focused": active is not None and int(parts[0], 16) == active})
    return found


def parse_macos_processes(text):
    """osascript prints {name, frontmost} lists flat: 'Finder, Safari, true, false'. Names
    come first, then one boolean each; parse defensively and never invent a pairing."""
    tokens = [t.strip().strip("{}").strip().strip('"') for t in str(text).strip().split(",")]
    tokens = [t for t in tokens if t]
    flags = []
    while tokens and tokens[-1] in ("true", "false") and len(tokens) - 1 >= len(flags) + 1:
        flags.insert(0, tokens.pop() == "true")
    if len(flags) != len(tokens):  # unreadable pairing: names only, focus unknown
        flags = [False] * len(tokens)
    return [{"id": name, "app": name, "title": name, "focused": focused}
            for name, focused in zip(tokens, flags)]


def _windows_macos():
    script = ('tell application "System Events" to get {name, frontmost} of '
              '(processes whose background only is false)')
    out = _run("osascript", "-e", script, timeout=10)
    if out.returncode:
        raise LookupError(out.stderr.strip() or "System Events could not list the apps")
    return parse_macos_processes(out.stdout)


def window_details():
    """Windows with their ids for the non-Windows, non-Hyprland desktops (actions.py uses
    the ids to focus and close). Raises LookupError when nothing can drive this desktop."""
    desktop = backend()
    if desktop == "sway":
        return _windows_sway()
    if desktop == "macos":
        return _windows_macos()
    if desktop in ("x11", "gnome", "kde"):
        return _windows_wmctrl()
    raise LookupError(NO_WINDOW_TOOL)


def windows():
    desktop = backend()
    if desktop == "windows":
        return _windows_nt()
    if desktop == "hyprland":
        return _windows_hyprland()
    return [{"app": w["app"], "title": w["title"], "focused": w["focused"]}
            for w in window_details()]


# -- playback and master volume ----------------------------------------------------------
def _playback_nt():
    """Read the current Windows SMTC session using the Windows Runtime API."""
    script = r'''
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionPlaybackStatus,Windows.Media.Control,ContentType=WindowsRuntime]
$op = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()
$manager = [System.WindowsRuntimeSystemExtensions]::AsTask($op).GetAwaiter().GetResult()
$session = $manager.GetCurrentSession()
if ($null -eq $session) { 'null'; exit }
$status = $session.GetPlaybackInfo().PlaybackStatus.ToString()
@{status=$status} | ConvertTo-Json -Compress
'''
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=8,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode:
        raise LookupError(out.stderr.strip() or "Windows media session is unavailable")
    try:
        return json.loads(out.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise LookupError("Windows media session returned unreadable state") from exc


def playback():
    if os.name == "nt":
        return _playback_nt()
    if backend() == "macos":
        if not shutil.which("nowplaying-cli"):
            raise LookupError("observing playback on macOS needs nowplaying-cli "
                              "(brew install nowplaying-cli)")
        out = _run("nowplaying-cli", "get", "playbackRate")
        rate = out.stdout.strip()
        if out.returncode or not re.fullmatch(r"-?\d+(?:\.\d+)?", rate):
            return None  # "(null)": nothing is playing
        return {"status": "Playing" if float(rate) > 0 else "Paused"}
    if not shutil.which("playerctl"):
        raise LookupError("observing playback needs playerctl")
    out = subprocess.run(["playerctl", "status"], capture_output=True, text=True, timeout=5)
    if out.returncode:
        return None
    return {"status": out.stdout.strip()}


def pactl_volume():
    """PulseAudio / PipeWire-pulse default sink: (percent, muted)."""
    vol = _run("pactl", "get-sink-volume", "@DEFAULT_SINK@")
    mute = _run("pactl", "get-sink-mute", "@DEFAULT_SINK@")
    if vol.returncode or mute.returncode:
        raise LookupError((vol.stderr or mute.stderr).strip() or "pactl could not read volume")
    match = re.search(r"(\d+)%", vol.stdout)
    if not match:
        raise LookupError("pactl returned unreadable volume")
    return int(match.group(1)), bool(re.search(r"yes", mute.stdout, re.I))


def macos_volume():
    """macOS output volume: (percent, muted), through osascript."""
    vol = _run("osascript", "-e", "output volume of (get volume settings)")
    mute = _run("osascript", "-e", "output muted of (get volume settings)")
    if vol.returncode or mute.returncode:
        raise LookupError((vol.stderr or mute.stderr).strip() or "osascript could not read volume")
    text = vol.stdout.strip()
    if not text.isdigit():  # "missing value" on outputs without a software volume
        raise LookupError("macOS returned unreadable volume")
    return int(text), mute.stdout.strip().lower() == "true"


def volume_state():
    if os.name != "nt":
        if backend() == "macos":
            percent, muted = macos_volume()
            return {"percent": percent, "muted": muted}
        if not shutil.which("wpctl"):
            if shutil.which("pactl"):
                percent, muted = pactl_volume()
                return {"percent": percent, "muted": muted}
            raise LookupError("observing volume needs wpctl or pactl")
        out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode:
            raise LookupError(out.stderr.strip() or "wpctl could not read volume")
        match = re.search(r"([\d.]+)(\s*\[MUTED\])?", out.stdout)
        if not match:
            raise LookupError("wpctl returned unreadable volume")
        return {"percent": round(float(match.group(1)) * 100), "muted": bool(match.group(2))}
    scalar, muted = _core_audio_volume()
    return {"percent": round(scalar * 100), "muted": bool(muted)}


def _core_audio_volume():
    """Read the default render endpoint via Core Audio. Returns (scalar, muted)."""
    return core_audio()


def core_audio(level=None, mute=None):
    """The default render endpoint via Core Audio: optionally set the master level
    (0.0-1.0) and/or mute first, then read back. Returns (scalar, muted).

    Setting through IAudioEndpointVolume is exact, unlike media-key presses (which can
    miss the session and only toggle mute). Kept in one function so callers/tests can
    isolate the native COM boundary. The vtable indices below are from
    IMMDeviceEnumerator, IMMDevice, and IAudioEndpointVolume respectively.
    """
    if level is not None and not 0.0 <= float(level) <= 1.0:
        raise ValueError("level must be between 0.0 and 1.0")
    if os.name != "nt":
        raise LookupError("Core Audio volume is available only on Windows")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        def __init__(self, value):
            raw = uuid.UUID(value).bytes_le
            super().__init__(int.from_bytes(raw[:4], "little"),
                             int.from_bytes(raw[4:6], "little"),
                             int.from_bytes(raw[6:8], "little"),
                             (ctypes.c_ubyte * 8).from_buffer_copy(raw[8:]))

    def check(hr, operation):
        if hr < 0:
            raise LookupError(f"Core Audio {operation} failed (HRESULT 0x{hr & 0xffffffff:08X})")

    def method(ptr, index, restype, *argtypes):
        vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])

    # WinDLL leaves HRESULTs as values so RPC_E_CHANGED_MODE can be handled as an
    # already-initialized apartment instead of being converted to an OSError.
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                       ctypes.c_ulong, ctypes.POINTER(GUID),
                                       ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long

    # S_OK and S_FALSE both require a matching CoUninitialize.
    initialized = False
    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    endpoint = ctypes.c_void_p()
    try:
        hr = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        if hr == 0x80010106 - 0x100000000:  # RPC_E_CHANGED_MODE: COM already initialized
            initialized = False
        else:
            check(hr, "CoInitializeEx")
            initialized = True
        clsid = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
        iid_enum = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x17,
                                    ctypes.byref(iid_enum), ctypes.byref(enumerator))
        check(hr, "CoCreateInstance(MMDeviceEnumerator)")

        # GetDefaultAudioEndpoint(eRender=0, eConsole=0)
        device = ctypes.c_void_p()
        hr = method(enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p))(enumerator, 0, 0,
                                                     ctypes.byref(device))
        check(hr, "GetDefaultAudioEndpoint")

        iid_volume = GUID("5CDF2C82-841E-4546-9722-0CF74078229A")
        endpoint = ctypes.c_void_p()
        hr = method(device, 3, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_ulong,
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                        device, ctypes.byref(iid_volume), 0x17, None,
                        ctypes.byref(endpoint))
        check(hr, "Activate(IAudioEndpointVolume)")

        if level is not None:  # SetMasterVolumeLevelScalar(float, LPCGUID eventContext)
            hr = method(endpoint, 7, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)(
                endpoint, float(level), None)
            check(hr, "SetMasterVolumeLevelScalar")
        if mute is not None:  # SetMute(BOOL, LPCGUID eventContext)
            hr = method(endpoint, 14, ctypes.c_long, ctypes.c_int, ctypes.c_void_p)(
                endpoint, int(bool(mute)), None)
            check(hr, "SetMute")
        scalar = ctypes.c_float()
        hr = method(endpoint, 9, ctypes.c_long, ctypes.POINTER(ctypes.c_float))(
            endpoint, ctypes.byref(scalar))
        check(hr, "GetMasterVolumeLevelScalar")
        muted = ctypes.c_int()
        hr = method(endpoint, 15, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
            endpoint, ctypes.byref(muted))
        check(hr, "GetMute")
        return scalar.value, bool(muted.value)
    except OSError as exc:
        raise LookupError(f"Core Audio could not reach the master volume: {exc}") from exc
    finally:
        for ptr in (endpoint, device, enumerator):
            if ptr and ptr.value:
                method(ptr, 2, ctypes.c_ulong)(ptr)
        if initialized:
            ole32.CoUninitialize()


# -- matching ---------------------------------------------------------------------------
def _is_app(window, query):
    return any(query in _flat(part) for part in window["app"].split()) or \
        query in _flat(window["title"])


def matches(predicate, listed):
    """(match, app) for a predicate against a window list."""
    kind = predicate["kind"]
    if kind == "playback":
        state = listed
        if not state:
            return False, None
        status = state.get("status", "").lower()
        command = predicate["command"]
        return (status == "playing" if command in ("play", "play_pause") else
                status in ("paused", "stopped") if command in ("pause", "stop") else
                bool(status)), None
    if kind == "volume":
        state = listed
        if not state:
            return False, None
        level = predicate["level"]
        if level.isdigit():
            return state["percent"] == int(level), None
        if level == "mute":
            return state["muted"], None
        if level == "unmute":
            return not state["muted"], None
        return True, None  # relative adjustments are confirmed by a successful readback
    if kind == "title":
        text = predicate["text"].strip()
        titles = [(w, page_title(w["title"])) for w in listed if is_browser(w["app"])]
        if text.startswith("/"):  # "/ X": the end of the page title, compared as written
            hit = next((w for w, t in titles if t.lower().endswith(text.lower())), None)
        else:
            query = _flat(text)
            hit = next((w for w, t in titles if query in _flat(t)), None)
        return bool(hit), hit["app"].split()[0] if hit and hit["app"] else None
    query = _flat(predicate["app"])
    hits = [w for w in listed if _is_app(w, query)]
    if kind == "gone":
        return not hits, None
    if kind == "focused":
        hits = [w for w in hits if w["focused"]]
    return bool(hits), hits[0]["app"].split()[0] if hits and hits[0]["app"] else None


def check(predicate, settle, lister=None, clock=time.monotonic, sleep=time.sleep):
    """Poll until the predicate holds or `settle` seconds pass. Never raises for a
    missing match; raises only for a bad predicate or when windows can't be listed."""
    validate(predicate)
    kind = predicate.get("kind") if isinstance(predicate, dict) else None
    lister = lister or (playback if kind == "playback" else
                        volume_state if kind == "volume" else windows)
    settle = max(0.0, min(float(settle), MAX_SETTLE))
    start = clock()
    while True:
        match, app = matches(predicate, lister())
        elapsed = clock() - start
        if match or elapsed >= settle:
            return {"match": match, "after_ms": round(elapsed * 1000), "app": app}
        sleep(min(POLL, settle - elapsed))


def run(arg):
    """The `observe` desktop action: arg is JSON {"predicate", "settle"}. (ok, json text)."""
    try:
        body = json.loads(arg)
        result = check(body.get("predicate"), body.get("settle", 5))
    except (ValueError, TypeError, AttributeError, LookupError, OSError,
            subprocess.SubprocessError) as exc:
        return False, f"Can't observe: {exc}"
    return True, json.dumps(result)
