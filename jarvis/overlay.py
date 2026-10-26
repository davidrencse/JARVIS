"""The console as a full-screen overlay toggled by a global hotkey (default Alt+1).

`python -m jarvis overlay` hosts its own console server and keeps one borderless,
always-on-top, slightly translucent app window over the monitor under the cursor.
The hotkey hides it and brings it back; hiding never closes the page, so a running
request, the session thread and the live feed all carry on in the background.

Windows only, standard library only: RegisterHotKey and the window calls go through
ctypes, and the window is a Chromium browser's --app mode with its own profile
directory, so it never mixes with your normal browsing.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

if os.name == "nt":
    import ctypes.wintypes  # MSG, used by the message loop in Overlay.run

TITLE = "JARVIS Overlay"
MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_APP_HIDE, WM_APP_SHOW, WM_QUIT = 0x0312, 0x8001, 0x8002, 0x0012
STARTUP_NAME = "JARVIS Overlay.vbs"


def parse_hotkey(text):
    """"alt+1" → (modifier flags, virtual-key code, "Alt+1")."""
    parts = [p for p in (text or "").lower().replace(" ", "").split("+") if p]
    if len(parts) < 2:
        raise ValueError("Use a modifier and a key, e.g. alt+1 or ctrl+alt+j.")
    *mods, key = parts
    flags = 0
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f"Unknown modifier {mod!r}; use alt, ctrl, shift or win.")
        flags |= MODIFIERS[mod]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    elif key in ("space", "`", "backquote"):
        vk = 0x20 if key == "space" else 0xC0
    else:
        raise ValueError(f"Unsupported key {key!r}; use a letter, digit, F1–F24, space or `.")
    label = "+".join([m.capitalize() if m != "ctrl" else "Ctrl" for m in mods] +
                     [key.upper() if len(key) == 1 else key.capitalize()])
    return flags, vk, label


class Window:
    """Finds, launches, styles, shows and hides the overlay's app window."""

    GWL_STYLE, GWL_EXSTYLE = -16, -20
    WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
    WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00080000, 0x00000080, 0x00040000
    HWND_TOPMOST = -1
    SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0020, 0x0040
    SW_HIDE, SW_SHOW = 0, 5

    def __init__(self, url, profile, opacity=0.96):
        self.url, self.profile, self.opacity = url, Path(profile), opacity
        self.user32 = ctypes.windll.user32
        self.hwnd = None
        self._setup_prototypes()

    def _setup_prototypes(self):
        u = self.user32
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
        u.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint)
        u.MonitorFromPoint.restype = ctypes.c_void_p
        u.MonitorFromPoint.argtypes = (POINT, ctypes.c_uint)
        u.GetMonitorInfoW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        u.GetForegroundWindow.restype = ctypes.c_void_p  # 64-bit HWND, not a C int
        u.GetWindowThreadProcessId.restype = ctypes.c_ulong
        u.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        u.AttachThreadInput.argtypes = (ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool)
        u.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        u.BringWindowToTop.argtypes = (ctypes.c_void_p,)
        u.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)

    def find(self):
        found = []
        buf = ctypes.create_unicode_buffer(256)
        cls = ctypes.create_unicode_buffer(64)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def visit(hwnd, _):
            self.user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "Chrome_WidgetWin_1":
                self.user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value == TITLE:
                    found.append(hwnd)
                    return False
            return True
        self.user32.EnumWindows(visit, 0)
        self.hwnd = found[0] if found else None
        return self.hwnd

    def close_stale(self, timeout=5):
        """Close a window left by an earlier run; it points at a server that's gone."""
        while self.find():
            self.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
            end = time.time() + timeout
            while time.time() < end and self.user32.IsWindow(self.hwnd):
                time.sleep(0.1)
            if self.user32.IsWindow(self.hwnd):
                break
        self.hwnd = None

    def alive(self):
        return (bool(self.hwnd) and bool(self.user32.IsWindow(self.hwnd))) or bool(self.find())

    def launch(self, timeout=20):
        from .ui import APP_BROWSERS
        import shutil
        for name in APP_BROWSERS:
            path = os.path.expandvars(name) if "\\" in name else shutil.which(name)
            if path and Path(path).is_file():
                break
        else:
            raise RuntimeError("No Edge or Chrome found for the overlay window.")
        self.profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([path, f"--app={self.url}", f"--user-data-dir={self.profile}",
                          "--no-first-run", "--no-default-browser-check",
                          "--disable-features=Translate"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        end = time.time() + timeout
        while time.time() < end:  # the title appears once the page has loaded
            if self.find():
                return self.hwnd
            time.sleep(0.25)
        raise RuntimeError("The overlay window didn't appear; is the console reachable?")

    def style(self):
        """Borderless, layered (translucent), out of Alt+Tab and the taskbar."""
        u, h = self.user32, self.hwnd
        style = u.GetWindowLongPtrW(h, self.GWL_STYLE) & ~(self.WS_CAPTION | self.WS_THICKFRAME)
        u.SetWindowLongPtrW(h, self.GWL_STYLE, style)
        ex = u.GetWindowLongPtrW(h, self.GWL_EXSTYLE)
        ex = (ex | self.WS_EX_LAYERED | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW
        u.SetWindowLongPtrW(h, self.GWL_EXSTYLE, ex)
        u.SetLayeredWindowAttributes(h, 0, int(255 * max(0.3, min(1.0, self.opacity))), 0x2)

    def cover(self):
        """Fill the monitor under the mouse, above everything else."""
        point = POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        monitor = self.user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        self.user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        r = info.rcMonitor
        self.user32.SetWindowPos(self.hwnd, self.HWND_TOPMOST, r.left, r.top,
                                 r.right - r.left, r.bottom - r.top,
                                 self.SWP_FRAMECHANGED | self.SWP_SHOWWINDOW)

    def visible(self):
        return self.alive() and bool(self.user32.IsWindowVisible(self.hwnd))

    def foreground(self):
        return self.alive() and (self.user32.GetForegroundWindow() or 0) == (self.hwnd or 0)

    def focus(self, attempts=8):
        """Give the overlay keyboard focus. Windows only lets the foreground's input
        queue hand focus over, so join it for the moment of the switch. No synthetic
        keys: a fake Alt tap was swallowed by Lan Mouse's keyboard hook while input was
        shared with the ASUS, and mid-Alt+1 it left Alt in a confused state."""
        u, kernel32 = self.user32, ctypes.windll.kernel32
        me = kernel32.GetCurrentThreadId()
        for _ in range(attempts):
            if self.foreground():
                return True
            current = u.GetForegroundWindow()
            other = u.GetWindowThreadProcessId(current, None) if current else 0
            joined = bool(other and other != me and u.AttachThreadInput(me, other, True))
            try:
                u.BringWindowToTop(self.hwnd)
                u.SetForegroundWindow(self.hwnd)
            finally:
                if joined:
                    u.AttachThreadInput(me, other, False)
            time.sleep(0.04)
        return self.foreground()

    def show(self):
        if not self.alive():
            self.launch()
        self.style()
        self.cover()
        self.user32.ShowWindow(self.hwnd, self.SW_SHOW)
        if not self.focus():
            # Never leave a full-screen, always-on-top window up without the keyboard:
            # it would cover the screen while typing went to whatever was underneath.
            self.hide()
            raise RuntimeError("Windows didn't give the overlay keyboard focus, so it was "
                               "hidden instead of covering your screen. Press the hotkey again.")

    def hide(self):
        if self.alive():
            self.user32.ShowWindow(self.hwnd, self.SW_HIDE)

    def toggle(self):
        if self.visible() and self.foreground():
            self.hide()
            return "hidden"
        self.show()
        return "shown"


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong)]


class Overlay:
    """Owns the hotkey and the window; every window call runs on this one thread."""

    def __init__(self, window, hotkey="alt+1"):
        self.window = window
        self.flags, self.vk, self.label = parse_hotkey(hotkey)
        self.thread_id = None
        self.ready = threading.Event()

    def request(self, action):
        """Thread-safe: ask the hotkey thread to hide or show the window."""
        if self.thread_id is None:
            raise RuntimeError("Overlay isn't running.")
        msg = {"hide": WM_APP_HIDE, "show": WM_APP_SHOW}.get(action)
        if msg is None:
            raise ValueError("action must be hide or show.")
        ctypes.windll.user32.PostThreadMessageW(self.thread_id, msg, 0, 0)
        return {"overlay": action}

    def run(self, show_first=True):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.flags | MOD_NOREPEAT, self.vk):
            self.ready.set()
            raise RuntimeError(f"{self.label} is already taken by another app; start the "
                               "overlay with --key, e.g. --key ctrl+alt+j.")
        self.ready.set()
        print(f"JARVIS overlay ready. {self.label} shows and hides it.", flush=True)
        try:
            if show_first:
                self._safe(self.window.show)
            msg = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._safe(self.window.toggle)
                elif msg.message == WM_APP_HIDE:
                    self._safe(self.window.hide)
                elif msg.message == WM_APP_SHOW:
                    self._safe(self.window.show)
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    @staticmethod
    def _safe(fn):
        try:
            fn()
        except Exception as exc:  # a failed show must never kill the hotkey loop
            print(f"overlay: {type(exc).__name__}: {exc}", flush=True)


def free_port(start):
    import socket
    for port in range(start, start + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port for the overlay's console.")


def run(config, base, state, hotkey="alt+1", port=7790, opacity=None, hidden=False):
    if os.name != "nt":
        raise RuntimeError("The overlay is Windows-only; on Arch, bind a Hyprland key to "
                           "`jarvis app` instead.")
    from .ui import Console, ConsoleServer, Handler
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JARVIS-Overlay")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        raise RuntimeError("The JARVIS overlay is already running; press its hotkey.")
    label = parse_hotkey(hotkey)[2]  # validates the hotkey and names it for the page
    # The overlay hosts its own console so the page's "hide" button can reach it, on a
    # fixed port so the page's saved settings (theme) persist across runs.
    port = free_port(port)
    url = f"http://127.0.0.1:{port}/?overlay=1&key={label.replace('+', '%2B')}#console"
    opacity = opacity if opacity is not None else float(
        config.get("ui", {}).get("overlay_opacity", 0.96))
    window = Window(url, Path(state) / "overlay-profile", opacity)
    window.close_stale()
    overlay = Overlay(window, hotkey)
    from .topology import enabled
    runtime = None
    if enabled(config):
        from .cluster_proxy import local_server
        server, runtime = local_server(config, base, state, port, overlay)
        console = runtime.console
    else:
        console = Console(config, base, state)
        if hasattr(console, "start_live"):
            console = console.start_live()
        console.overlay = overlay
        from .grokbot import autostart_tunnel
        autostart_tunnel(config, state)
        handler = type("OverlayHandler", (Handler,), {
            "console": console, "hosts": ("127.0.0.1", "localhost", "::1")})
        server = ConsoleServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"JARVIS overlay console on http://127.0.0.1:{port}/", flush=True)
    try:
        overlay.run(show_first=not hidden)
    except KeyboardInterrupt:
        pass
    finally:
        overlay.stop()
        server.shutdown()
        server.server_close()
        if runtime:
            runtime.close()
        elif hasattr(console, "close"):
            console.close()
        ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


def _startup_file():
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / STARTUP_NAME)


def autostart(state, enable, hotkey="alt+1"):
    """Start the overlay hidden at login, so the hotkey works all day."""
    if os.name != "nt":
        raise RuntimeError("The overlay is Windows-only; on Arch, bind a Hyprland key to "
                           "`jarvis app` instead.")
    path = _startup_file()
    if not enable:
        path.unlink(missing_ok=True)
        return {"autostart": False}
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.is_file() else Path(sys.executable)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = (f'"{exe}" -m jarvis --state "{Path(state).resolve()}" overlay --hidden '
               f'--key {parse_hotkey(hotkey)[2].lower()}')
    path.write_text(
        "' JARVIS: start the console overlay hidden at login; its hotkey shows it.\n"
        "' Delete this file to disable.\n"
        f'CreateObject("WScript.Shell").Run "{command.replace(chr(34), chr(34) * 2)}", 0, False\n',
        encoding="utf-8")
    return {"autostart": True, "file": str(path), "command": command}
