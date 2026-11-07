"""L5 observe: did an action's effect actually appear? Runs on the machine that acted.

The main sends a predicate ("a window of the app Spotify", "a window whose title names
youtube"), and this polls the desktop every 250 ms until the predicate holds or the
settle time runs out. Only {"match", "after_ms", "app"} travels back: window titles never
leave the machine (docs/jarvis-os-architecture.md, "Privacy at the source").

Windows are listed straight from Win32 (EnumWindows), Hyprland (hyprctl), sway (swaymsg),
X11 (wmctrl) or macOS (System Events through osascript), chosen by platforms.detect(), not
through PowerShell, which takes most of a second per call and is too slow to poll.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import ctypes
import uuid
from urllib.parse import urlsplit

from .platforms import NO_WINDOW, on_windows
from . import platforms

KINDS = ("window", "title", "focused", "gone", "playback", "volume")
# playerctl's player choice on Linux: Spotify when it is running, else whatever plays.
# Without it playerctl drives the most recently active player (a browser video, say).
PLAYERCTL_PLAYERS = "spotify,%any"
MAX_SETTLE = 15.0
POLL = 0.25
UNCHANGED_GRACE = 1.5
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
# Window classes that are the Windows shell itself, never an app: WM_CLOSE on Progman
# opens the shutdown dialog, so they are never listed (nothing can close or focus them).
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
# Store (UWP) apps draw inside ApplicationFrameHost; the app's own process owns a child
# window (Calculator: CalculatorApp). Listed as "ApplicationFrameHost CalculatorApp".
UWP_HOST = "applicationframehost"


def _windows_nt():
    """Visible, uncloaked top-level windows: {"id" (HWND), "app", "title", "focused"}."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    try:
        dwm = ctypes.windll.dwmapi
    except OSError:
        dwm = None
    found, names = [], {}
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def pid_of(hwnd):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def cloaked(hwnd):
        # Suspended Store apps and other virtual-desktop windows are "visible" but cloaked.
        if dwm is None:
            return False
        value = ctypes.c_int(0)
        return dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), ctypes.sizeof(value)) == 0 \
            and value.value != 0

    def class_of(hwnd):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not cloaked(hwnd) and class_of(hwnd) not in SHELL_CLASSES:
            if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW: no taskbar entry
                return True
            size = user32.GetWindowTextLengthW(hwnd)
            if size:
                title = ctypes.create_unicode_buffer(size + 1)
                user32.GetWindowTextW(hwnd, title, size + 1)
                found.append((hwnd, pid_of(hwnd), title.value))
        return True

    def name_of(pid):
        if pid not in names:
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            name = ""
            if handle:
                path, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    name = os.path.splitext(os.path.basename(path.value))[0]
                kernel32.CloseHandle(handle)
            names[pid] = name
        return names[pid]

    def hosted(hwnd, host):
        """The process of a Store app drawn in an ApplicationFrameHost window."""
        inner = []

        def child(sub, _):
            pid = pid_of(sub)
            if pid != host:
                inner.append(pid)
                return False
            return True
        user32.EnumChildWindows(hwnd, proc(child), 0)
        return name_of(inner[0]) if inner else ""

    user32.EnumWindows(proc(visit), 0)
    focused = user32.GetForegroundWindow()
    listed = []
    for hwnd, pid, title in found:
        app = name_of(pid)
        if app.lower() == UWP_HOST:
            inner = hosted(hwnd, pid)
            app = f"{app} {inner}" if inner else app
        listed.append({"id": int(hwnd), "app": app, "title": title, "focused": hwnd == focused})
    return listed


def _windows_hyprland():
    if not shutil.which("hyprctl"):
        raise LookupError("observing windows needs Hyprland (hyprctl)")

    def query(*args):
        try:
            out = subprocess.run(["hyprctl", *args, "-j"], capture_output=True, text=True,
                                 timeout=5)
        except subprocess.TimeoutExpired:
            raise LookupError("hyprctl timed out") from None
        except OSError:
            return None
        if out.returncode or not out.stdout.strip():
            return None  # a failed query is not "no windows"
        try:
            return json.loads(out.stdout)
        except ValueError:
            return None

    clients = query("clients")
    if not isinstance(clients, list):
        return None  # the window list is unknown (callers: None = can't tell)
    active = (query("activewindow") or {}).get("address")
    return [{"app": f"{app_class(c.get('class'))} {app_class(c.get('initialClass'))}",
             "title": c.get("title", ""), "focused": c.get("address") == active}
            for c in clients]


def _run(*args, timeout=5):
    """Argument-list subprocess (never a shell); no console window on Windows."""
    return platforms.run_quiet(args, timeout)


def backend():
    """Which desktop family to drive. A non-Windows os.name never maps to the Windows
    backend: it falls back to Hyprland, the original non-Windows behaviour."""
    if on_windows():
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
        out = _run("swaymsg", "-t", "get_tree")
        tree = json.loads(out.stdout) if not out.returncode and out.stdout.strip() else None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None  # unreadable is not "no windows"
    return _sway_tree(tree, []) if isinstance(tree, dict) else None


def _windows_wmctrl():
    if not shutil.which("wmctrl"):
        raise LookupError(NO_WINDOW_TOOL)
    active = None
    if shutil.which("xdotool"):
        out = _run("xdotool", "getactivewindow")
        if not out.returncode and out.stdout.strip().isdigit():
            active = int(out.stdout.strip())
    try:
        listing = _run("wmctrl", "-lx")
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode:
        return None  # wmctrl failed (no display?): not "no windows"
    found = []
    for line in listing.stdout.splitlines():
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
    the ids to focus and close). Raises LookupError when nothing can drive this desktop;
    a listing that failed reads as empty here (observation uses _list_details: None)."""
    found = _list_details()
    return [] if found is None else found


def _list_details():
    desktop = backend()
    if desktop == "sway":
        return _windows_sway()
    if desktop == "macos":
        return _windows_macos()
    if desktop in ("x11", "gnome", "kde"):
        return _windows_wmctrl()
    raise LookupError(NO_WINDOW_TOOL)


def windows():
    """The open windows, or None when the listing failed (can't tell, not "none")."""
    desktop = backend()
    if desktop == "windows":
        return _windows_nt()
    if desktop == "hyprland":
        return _windows_hyprland()
    found = _list_details()
    if found is None:
        return None
    return [{"app": w["app"], "title": w["title"], "focused": w["focused"]} for w in found]


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
$track = ''
try {
  $null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties,Windows.Media.Control,ContentType=WindowsRuntime]
  $props = [System.WindowsRuntimeSystemExtensions]::AsTask($session.TryGetMediaPropertiesAsync()).GetAwaiter().GetResult()
  $track = "$($props.Artist)|$($props.Title)"
} catch {}
@{status=$status; track=$track} | ConvertTo-Json -Compress
'''
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=8,
                         creationflags=NO_WINDOW)
    if out.returncode:
        raise LookupError(out.stderr.strip() or "Windows media session is unavailable")
    try:
        return json.loads(out.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise LookupError("Windows media session returned unreadable state") from exc


def playback():
    if on_windows():
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
    out = _run("playerctl", "-p", PLAYERCTL_PLAYERS, "status")
    if out.returncode:
        return None
    state = {"status": out.stdout.strip()}
    track = _run("playerctl", "-p", PLAYERCTL_PLAYERS, "metadata", "--format",
                 "{{mpris:trackid}}|{{xesam:artist}}|{{xesam:title}}")
    if not track.returncode and track.stdout.strip(" |\n"):
        state["track"] = track.stdout.strip()
    return state


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
    if not on_windows():
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
    if not on_windows():
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
# What an app is called by its process or window class, when that isn't its name: the
# Store's Calculator is CalculatorApp, Word is WINWORD, Discord on the Zenbook is Vesktop.
# Keys and values are flattened (lowercase letters and digits only).
APP_IDS = {
    "calculator": ("calculatorapp", "calculator", "gnomecalculator", "kcalc", "qalculate",
                   "qalculategtk", "galculator", "speedcrunch"),
    "calc": ("calculatorapp", "calculator", "gnomecalculator", "kcalc", "qalculate"),
    "discord": ("discord", "vesktop", "vencord", "webcord", "discordcanary", "discordptb"),
    "word": ("winword", "word"), "microsoftword": ("winword",),
    "excel": ("excel",), "microsoftexcel": ("excel",),
    "powerpoint": ("powerpnt", "powerpoint"), "outlook": ("outlook", "olk"),
    "onenote": ("onenote",), "teams": ("teams", "msteams", "msteams"),
    "settings": ("systemsettings", "systemsettings", "gnomecontrolcenter", "systemsettings5"),
    "fileexplorer": ("explorer", "dolphin", "nautilus", "thunar", "nemo", "orgkdedolphin"),
    "files": ("explorer", "dolphin", "nautilus", "thunar", "nemo", "orgkdedolphin"),
    "photos": ("photos", "microsoftphotos", "photosapp"),
    "notepad": ("notepad",), "paint": ("mspaint", "paint"),
    "edge": ("msedge",), "microsoftedge": ("msedge",),
    "zen": ("zen", "zenbrowser", "appzenbrowserzen"), "zenbrowser": ("zen", "zenbrowser", "appzenbrowserzen"),
    "chrome": ("chrome", "googlechrome", "comgooglechrome"),
    "googlechrome": ("chrome", "googlechrome", "comgooglechrome"),
    "visualstudiocode": ("code", "vscodium", "codeoss"), "vscode": ("code", "vscodium", "codeoss"),
    # No "terminal": every kitty/Windows Terminal window would match, and closing them all
    # kills whatever runs in them. "close kitty" names the app; a title picks one window.
    "commandprompt": ("cmd",), "powershell": ("powershell", "pwsh"),
    "steam": ("steam", "steamwebhelper"), "taskmanager": ("taskmgr",),
    "snippingtool": ("snippingtool", "screensketch"), "clock": ("time", "clock"),
    "virtualbox": ("virtualbox", "virtualboxmanager", "virtualboxvm"),
}


# Reverse-domain pieces that name no app: "close app" must not match app.zen_browser.zen.
CLASS_NOISE = frozenset({"app", "apps", "org", "com", "dev", "io", "net", "desktop", "kde",
                         "gnome", "github", "flatpak", "bin", "exe", "x86", "64", "client"})


def app_ids(query):
    """The flattened names an app's window can go by: the query itself plus its known
    process/class names."""
    flat = _flat(query)
    return (flat,) + tuple(n for n in APP_IDS.get(flat, ()) if n != flat)


def app_match(window, query, words=()):
    """0 = not this app; 3 = a process/class name is exactly one of its names; 2 = a name
    contains what was said ("googlechrome" for chrome), or is one of its words ("code" for
    visual studio code); 1 = only the title mentions it. Known other names count only
    exactly: "close code" must never close Codex because "code" is inside it."""
    names = app_ids(query)
    app = str(window.get("app") or "")
    # Each name and each segment of a reverse-domain class: dev.vencord.Vesktop -> vesktop.
    segments = [x for x in re.split(r"[\s._-]+", app) if _flat(x) not in CLASS_NOISE]
    parts = list(dict.fromkeys(p for p in (_flat(x) for x in app.split() + segments) if p))
    if any(part in names for part in parts):
        return 3
    said = names[0]
    # A multi-word name ("visual studio code") needs all its words among the class parts,
    # or "vs code" would match Android Studio's "jetbrains-studio".
    needed = {w for w in words if len(w) >= 3}
    word_hit = any(len(part) >= 3 and part in words for part in parts) and (
        len(words) < 2 or needed <= set(parts))
    if (len(said) >= 4 and said not in CLASS_NOISE and any(said in part for part in parts)) or word_hit:
        return 2
    return 1 if said and said in _flat(window.get("title") or "") else 0


def _is_app(window, query):
    return app_match(window, query) > 0


def matches(predicate, listed):
    """(match, app) for a predicate against a window list."""
    kind = predicate["kind"]
    if listed is None and kind not in ("playback", "volume"):
        return False, None  # the window list couldn't be read: no proof either way
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
        return True, None  # relative: check() also needs a change (or a bound), see _relative
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
    hits = [w for w in listed if app_match(w, query) >= 2]
    if kind == "gone":  # close_app never closes a browser tab titled with the name, so it
        return not hits, None  # doesn't count as the app still being open
    if not hits:  # no app/class match: a browser window titled with the name still counts,
        # but a terminal titled "nvim discord_bot.py" does not
        hits = [w for w in listed if is_browser(w["app"]) and app_match(w, query) == 1]
    if kind == "focused":
        hits = [w for w in hits if w["focused"]]
    return bool(hits), hits[0]["app"].split()[0] if hits and hits[0]["app"] else None


def wants_snapshot(predicate):
    """Whether a before/after comparison can tell this effect from a state that already
    held: windows, focus, page titles, and track changes. Play/pause is read by the
    media executor itself ("Already paused."), volume and "gone" are fine as they are."""
    kind = (predicate or {}).get("kind")
    return kind in ("window", "focused", "title") or (
        kind == "playback" and predicate.get("command") in ("next", "previous")) or (
        kind == "volume" and predicate.get("level") in ("up", "down"))


def fingerprint(predicate, listed):
    """An opaque digest of the state the predicate looks at, so "did it change?" can be
    asked without window titles or track names leaving the machine. None: nothing to compare."""
    kind = predicate["kind"]
    if listed is None and kind not in ("playback", "volume"):
        return None
    if kind == "volume":
        if not listed or predicate.get("level") not in ("up", "down"):
            return None
        state = [listed.get("percent"), bool(listed.get("muted"))]
    elif kind == "playback":
        if not listed or not listed.get("track"):
            return None  # no track identity: a skip can't be told apart from no skip
        state = [listed.get("status", ""), listed["track"]]
    elif kind in ("window", "focused"):
        query = _flat(predicate["app"])
        state = sorted([w["app"], w["title"], bool(w["focused"])]
                       for w in listed if _is_app(w, query))
    elif kind == "title":
        state = sorted([w["app"], w["title"], bool(w["focused"])]
                       for w in listed if is_browser(w["app"]))
    else:
        return None
    return hashlib.sha256(json.dumps(state).encode()).hexdigest()[:16]


def _relative(predicate, listed, changed):
    """Whether a match still has to be set aside as unverified. A relative volume step or a
    skip only proves itself by changing the readback (volume at 0/100 can't), so a no-op
    key press, or no earlier reading to compare, is "can't tell", never "worked"."""
    kind, key = predicate["kind"], predicate.get("level") or predicate.get("command")
    if kind == "volume" and key in ("up", "down"):
        percent = (listed or {}).get("percent")
        bound = percent is not None and percent >= 100 if key == "up" else percent == 0
        return changed is not True and not bound
    if kind == "playback" and key in ("next", "previous"):
        return changed is not True
    return False


def _lister(predicate):
    kind = predicate.get("kind") if isinstance(predicate, dict) else None
    return playback if kind == "playback" else volume_state if kind == "volume" else windows


def snapshot(predicate, lister=None):
    """{"before": digest or None} for wants_snapshot predicates, taken before acting."""
    validate(predicate)
    if not wants_snapshot(predicate):
        return {"before": None}
    return {"before": fingerprint(predicate, (lister or _lister(predicate))())}


def check(predicate, settle, lister=None, clock=time.monotonic, sleep=time.sleep, before=None):
    """Poll until the predicate holds or `settle` seconds pass. Never raises for a
    missing match; raises only for a bad predicate or when windows can't be listed.
    With `before` (a snapshot digest) the state must also differ from it: a window that
    was already open, focused and unchanged doesn't prove "open it" did anything.
    "changed" (with `before` only) is None when there was nothing to compare."""
    validate(predicate)
    lister = lister or _lister(predicate)
    settle = max(0.0, min(float(settle), MAX_SETTLE))
    start = clock()
    while True:
        listed = lister()
        match, app = matches(predicate, listed)
        changed = None
        if before is not None:
            now = fingerprint(predicate, listed)
            changed = None if now is None else now != before
        unverified = listed is None or (match and _relative(predicate, listed, changed))
        if unverified:
            match = False
        elapsed = clock() - start
        # An unchanged match gets a short grace for a late change, not the whole settle.
        if (match and (changed is not False or elapsed >= UNCHANGED_GRACE)) or elapsed >= settle:
            seen = {"match": match, "after_ms": round(elapsed * 1000), "app": app}
            if unverified:
                seen["unverified"] = True  # no proof either way: the caller leaves it "sent"
            return {**seen, "changed": changed} if before is not None else seen
        sleep(min(POLL, settle - elapsed))


FOCUSED_TITLE = 80  # characters of the focused window's title the brain is shown


def now():
    """What this computer is doing, for the brain's per-request snapshot (situation.py):
    the apps with windows, the focused window's app and (shortened) title, and what's
    playing. Only the focused title: other titles hold mail subjects and chat names that
    David chose not to send with every request (2026-10-31)."""
    apps, focused = [], None
    try:
        listed = windows()
        if listed is None:
            raise LookupError("window list unreadable")
        for w in listed:
            words = list(dict.fromkeys(str(w.get("app") or "").split()))  # "kitty kitty" -> kitty
            app = " ".join(words)
            if app and app not in apps:
                apps.append(app)
            if w.get("focused"):
                title = " ".join(str(w.get("title") or "").split())
                focused = {"app": app, "title": title[:FOCUSED_TITLE] +
                           ("…" if len(title) > FOCUSED_TITLE else "")}
    except (LookupError, OSError, ValueError, subprocess.SubprocessError):
        apps = None  # can't list windows on this desktop: say nothing rather than "no apps"
    try:
        playing = playback()
    except (LookupError, OSError, ValueError, subprocess.SubprocessError):
        playing = None
    return {"apps": apps, "focused": focused, "playing": playing}


def run(arg):
    """The `observe` desktop action: arg is JSON {"predicate", "settle", "before"},
    {"snapshot": predicate} for the before-state digest, or {"now": true} for now().
    (ok, json text)."""
    try:
        body = json.loads(arg)
        if body.get("now") is True:
            return True, json.dumps(now())
        if "snapshot" in body:
            return True, json.dumps(snapshot(body["snapshot"]))
        before = body.get("before")
        if before is not None and not (isinstance(before, str) and
                                       re.fullmatch(r"[0-9a-f]{16}", before)):
            raise ValueError("bad snapshot digest")
        result = check(body.get("predicate"), body.get("settle", 5), before=before)
    except (ValueError, TypeError, AttributeError, LookupError, OSError,
            subprocess.SubprocessError) as exc:
        return False, f"Can't observe: {exc}"
    return True, json.dumps(result)
