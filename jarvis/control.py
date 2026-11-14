"""Desktop control: JARVIS sees the screen and uses the mouse and keyboard like David would.

One desktop action, `computer`, with a JSON spec:

    {"action": "screenshot"}                         -> the focused screen, as an image
    {"action": "click"|"double_click"|"right_click"|"move", "x": 640, "y": 360}
    {"action": "drag", "x": 100, "y": 100, "x2": 400, "y2": 300}
    {"action": "type", "text": "hello"}
    {"action": "key", "keys": "ctrl+l"}              (enter, tab, esc, ctrl+shift+t, super...)
    {"action": "scroll", "x": 640, "y": 360, "direction": "down", "amount": 3}

x/y are pixels on the last screenshot JARVIS took; they are mapped to the real screen.
Backends (platforms.detect): Hyprland (grim, hyprctl, ydotool through the ydotoold user
service, wtype), Windows (Win32 through ctypes), macOS (screencapture + cliclick).

Safety, checked before every action:
- Stop: `jarvis control stop` (bound to a hotkey if David wants one) or "Jarvis, stop"
  writes STOP_FILE; every action refuses for STOP_MINUTES after.
- David takes over: if the pointer is not where JARVIS left it, he moved it, so JARVIS
  pauses (refuses) for PAUSE_SECONDS and says so.
- At most MAX_ACTIONS actions per 10 minutes.
- Never types card numbers or one-time codes (browser_gate's refusals); the brain is told
  never to type passwords.
- Fail-safe: any error mid-action releases the mouse buttons and modifier keys.
Every action is logged to .jarvis/logs/control.jsonl.
"""

from .home import home as _jarvis_home
import base64
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import platforms

STATE = Path(os.environ.get("JARVIS_STATE") or _jarvis_home() / ".jarvis")
STOP_FILE = "control-stop"
STOP_MINUTES = 10
PAUSE_SECONDS = 15
TAKEOVER_WINDOW = 120  # seconds after JARVIS's last action in which a moved pointer means David
MAX_ACTIONS = 120
SHOT_WIDTH = 1280
ACTIONS = ("screenshot", "click", "double_click", "right_click", "move", "drag", "type", "key", "scroll")

_LOCK = threading.Lock()
_STATE = {"map": None, "left_at": None, "paused_until": 0.0, "times": [], "announced": 0.0,
          "typed": ""}  # typed: text since the last enter / ctrl+c (or ctrl+u on a known line), checked as one
SUBMIT_KEYS = ("enter", "return", "kp_enter")
CLEAR_KEYS = ("ctrl+c", "ctrl+u")  # clear a shell line; ctrl+u only from the cursor back, so
# it counts as a clear only on a known line (cursor at the end); ctrl+c drops the whole line
UNKNOWN_MSG = ("The line being typed is no longer known (a click or cursor/editing keys may have "
               "moved the text cursor, or it grew past TYPED_MAX). In a terminal, press ctrl+c "
               "first (ctrl+u keeps text after the cursor), then type again; to fill in form "
               "fields, use app_ui instead.")
POINTER_ACTIONS = ("click", "double_click", "right_click", "drag")
TYPED_MAX = 4000


class Refused(Exception):
    pass


class ToolImage(str):
    """A tool result that is also a picture: str (the note the model reads) plus .b64 and
    .mime, which the ChatGPT and MCP tool paths send on as an image."""

    def __new__(cls, note, b64, mime="image/jpeg"):
        obj = super().__new__(cls, note)
        obj.b64, obj.mime = b64, mime
        return obj


def as_result(ok, text):
    """(ok, text) from act/run, with a screenshot's JSON turned into a ToolImage."""
    if ok and isinstance(text, str) and text.startswith("{") and '"b64"' in text:
        try:
            shot = json.loads(text)
            return ok, ToolImage(shot.get("note") or "Screenshot.", shot["b64"], shot.get("mime", "image/jpeg"))
        except (ValueError, KeyError):
            pass
    return ok, text


# -- safety ---------------------------------------------------------------------------------
def stop(state=None, minutes=STOP_MINUTES):
    """Halt desktop control now and for `minutes`."""
    path = Path(state or STATE) / STOP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time() + minutes * 60), encoding="utf-8")
    _release()
    _release_remote()
    return True, f"Desktop control stopped for {minutes} minutes."


def resume(state=None):
    try:
        (Path(state or STATE) / STOP_FILE).unlink()
    except FileNotFoundError:
        pass
    with _LOCK:
        _STATE.update(paused_until=0.0, left_at=None, typed="")
    return True, "Desktop control can run again."


def _stopped(state):
    try:
        until = float((Path(state) / STOP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return time.time() < until


def _check(state, action, spec, backend):
    now = time.time()
    if _stopped(state):
        raise Refused("Desktop control is stopped (jarvis control resume, or wait).")
    with _LOCK:
        if now < _STATE["paused_until"] and action != "screenshot":  # looking is always fine
            raise Refused("David is using the mouse; desktop control is paused for a moment.")
        recent = [t for t in _STATE["times"] if now - t < 600]
        if len(recent) >= MAX_ACTIONS:
            raise Refused(f"More than {MAX_ACTIONS} desktop actions in 10 minutes; stopping here.")
        left = _STATE["left_at"]
        # a screenshot is a look at the screen too: it keeps the clock fresh like an action
        if now - max(_STATE.get("left_time", 0), _STATE.get("shot_time", 0)) > TAKEOVER_WINDOW:
            left = None  # long since JARVIS's last action: the pointer moving is normal use,
            if action != "screenshot":  # so the old screenshot says nothing about the screen now
                _STATE["map"] = None
                if _STATE["typed"] is not None:  # an unknown line stays unknown
                    _STATE["typed"] = ""
    if left and action != "screenshot":
        here = backend.cursor()
        if here and (abs(here[0] - left[0]) > 6 or abs(here[1] - left[1]) > 6):
            with _LOCK:  # forget the screen too: nothing runs again before a fresh look
                _STATE["paused_until"], _STATE["left_at"], _STATE["map"] = now + PAUSE_SECONDS, None, None
            raise Refused("David moved the mouse, so JARVIS stepped back. Take a new screenshot "
                          "before trying again.")
    if action != "screenshot" and _STATE["map"] is None:
        raise Refused("Take a screenshot first, so JARVIS sees what it is about to touch.")
    with _LOCK:  # counted only once every check passed: refusals don't use up the budget
        _STATE["times"] = [t for t in _STATE["times"] if now - t < 600] + [now]
    if action in ("type", "key"):
        typed = _STATE["typed"]
        if action == "type":
            if typed is None:
                raise Refused(UNKNOWN_MSG)
            if _typed_next(typed, "type", spec) is None:  # too long to keep: never truncated
                raise Refused(f"That line would pass {TYPED_MAX} characters; JARVIS can't check it.")
            _check_line(typed + str(spec.get("text") or ""))
        else:
            keys = _norm_keys(spec.get("keys"))
            if keys in SUBMIT_KEYS:
                if typed is None:
                    raise Refused(UNKNOWN_MSG)
                _check_line(typed)
            else:
                nxt = _typed_next(typed, "key", spec)
                if typed is not None and nxt is None and len(typed) >= TYPED_MAX:
                    raise Refused(f"That line would pass {TYPED_MAX} characters; JARVIS can't check it.")
                if nxt:
                    _check_line(nxt)


def _check_line(text):
    """Refuse a typed line that is, as a whole, a secret or something the terminal would deny."""
    from .browser_gate import blocked
    # What is typed in pieces lands as one: check it together with everything typed into
    # the same place since the last enter (\"rm -rf\" then \" ~\", a split card).
    refusal = blocked("browser_type", {"text": text, "element": "desktop text"})
    if not refusal and re.fullmatch(r"\s*\d(?:[\s-]?\d){3,7}\s*", text):
        refusal = "JARVIS never types one-time codes or PINs"
    if not refusal:  # typed into a terminal it runs: the terminal tool's hard refusals apply
        # With this computer's config (vault paths) and in home, where a new terminal opens:
        # that terminal has no sandbox (audit 2026-11-04: cwd /tmp and {} let vault deletes by).
        from .gates import hard_deny, local_config
        refusal = hard_deny("terminal", {"command": text, "cwd": os.path.expanduser("~")}, local_config())
    if refusal:
        raise Refused(refusal)


def _log(state, entry):
    try:
        path = Path(state) / "logs" / "control.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}) + "\n")
    except OSError:
        pass


# -- backends -------------------------------------------------------------------------------
MODS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "super": "logo",
        "win": "logo", "meta": "logo", "cmd": "logo"}
WTYPE_KEYS = {"enter": "Return", "return": "Return", "tab": "Tab", "esc": "Escape",
              "escape": "Escape", "backspace": "BackSpace", "delete": "Delete", "space": "space",
              "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home",
              "end": "End", "pageup": "Prior", "pagedown": "Next", **{f"f{i}": f"F{i}" for i in range(1, 13)}}


def _parse_keys(keys):
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", str(keys or "")) if p.strip()]
    if not parts:
        raise ValueError("key needs keys, e.g. ctrl+l or enter")
    mods = [MODS[p] for p in parts[:-1] if p in MODS]
    if len(mods) != len(parts) - 1:
        raise ValueError(f"unknown modifier in {keys!r}")
    last = parts[-1]
    return mods, (MODS[last] if last in MODS else last)  # "super" alone is the logo key


class Hyprland:
    def _run(self, *args, data=None, timeout=15):
        return subprocess.run(list(args), capture_output=True, timeout=timeout, check=True,
                              input=data)

    def _monitor(self):
        mons = json.loads(self._run("hyprctl", "-j", "monitors").stdout)
        return next((m for m in mons if m.get("focused")), mons[0])

    def shot(self):
        mon = self._monitor()
        png = self._run("grim", "-o", mon["name"], "-t", "png", "-").stdout
        scale = float(mon.get("scale") or 1)
        return png, {"x": mon["x"], "y": mon["y"], "w": mon["width"] / scale, "h": mon["height"] / scale}

    def cursor(self):
        try:
            x, y = self._run("hyprctl", "cursorpos").stdout.decode().split(",")
            return float(x), float(y)
        except (subprocess.SubprocessError, ValueError, OSError):
            return None

    def move(self, x, y):
        self._run("hyprctl", "dispatch", f"hl.dsp.cursor.move({{ x = {round(x)}, y = {round(y)} }})")

    def button(self, which, how, timeout=15):  # how: click, down, up
        code = {"left": 0, "right": 1, "middle": 2}[which] | {"click": 0xC0, "down": 0x40, "up": 0x80}[how]
        self._run("ydotool", "click", hex(code), timeout=timeout)

    def type(self, text):
        self._run("wtype", "--", text)

    def key(self, keys):
        mods, key = _parse_keys(keys)
        args = ["wtype"]
        for m in mods:
            args += ["-M", m]
        args += ["-k", WTYPE_KEYS.get(key, key)] if len(key) > 1 else [key]
        for m in reversed(mods):
            args += ["-m", m]
        self._run(*args)

    def scroll(self, direction, amount):
        self._run("ydotool", "mousemove", "--wheel", "-x", "0", "-y",
                  str(-amount if direction == "up" else amount))

    # evdev codes of every modifier key: LCtrl, LShift, LAlt, LSuper, RCtrl, RShift, RAlt, RSuper
    MODIFIER_CODES = (29, 42, 56, 125, 97, 54, 100, 126)

    def release(self):
        """Let go of the mouse buttons and every modifier, like Windows.release. key() sends
        its modifier ups only if wtype lives to the end: a wtype killed after a modifier-down
        (the 15 s timeout) would leave Ctrl/Alt/Super held. Two independent ways, each best
        effort with a short timeout (this runs from a stop and from error paths: never raise,
        never hang): wtype -m releases its modifiers, ydotool sends the key-up codes."""
        for which in ("left", "right"):
            try:
                self.button(which, "up", timeout=3)
            except (subprocess.SubprocessError, OSError):
                pass
        attempts = (["wtype"] + [a for m in ("ctrl", "shift", "alt", "logo") for a in ("-m", m)],
                    ["ydotool", "key"] + [f"{code}:0" for code in self.MODIFIER_CODES])
        for args in attempts:
            try:
                subprocess.run(args, capture_output=True, timeout=3, check=True)
            except (subprocess.SubprocessError, OSError):
                pass


_INPUT = None


def win_input():
    """The Win32 INPUT struct (mouse or keyboard), built once."""
    global _INPUT
    if _INPUT is None:
        import ctypes
        from ctypes import wintypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]
        INPUT.MOUSEINPUT, INPUT.KEYBDINPUT = MOUSEINPUT, KEYBDINPUT
        _INPUT = INPUT
    return _INPUT


def send_input(user32, what, *fields):
    """One SendInput event: ("key", vk, scan, flags) or ("mouse", flags, data). Raises OSError
    when Windows refuses it (locked screen, UAC prompt: the secure desktop). keybd_event and
    mouse_event return nothing, so a dropped press went unnoticed and a key-up counted as
    done. UIPI (an elevated foreground window) still drops input with success."""
    import ctypes
    INPUT = win_input()
    events = (INPUT * 1)()
    if what == "key":
        vk, scan, flags = fields
        events[0].type = 1
        events[0].ki = INPUT.KEYBDINPUT(vk, scan, flags, 0, 0)
    else:
        flags, data = fields
        events[0].type = 0
        events[0].mi = INPUT.MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    if user32.SendInput(1, events, ctypes.sizeof(INPUT)) != 1:
        raise OSError("Windows refused the input (locked screen or secure desktop)")


class Windows:
    VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
          "delete": 0x2E, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
          "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
          "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "logo": 0x5B, **{f"f{i}": 0x6F + i for i in range(1, 13)}}

    def __init__(self):
        import ctypes
        self.user32 = ctypes.windll.user32
        try:
            if self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return  # FALSE: no per-monitor v2 on this Windows, use the older calls
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels, not scaled ones
        except (AttributeError, OSError):
            try:
                self.user32.SetProcessDPIAware()
            except Exception:
                pass

    def shot(self):
        from .screen import _windows
        m = self.user32.GetSystemMetrics
        return _windows(), {"x": m(76), "y": m(77), "w": m(78), "h": m(79)}  # the virtual screen

    def cursor(self):
        import ctypes
        import ctypes.wintypes
        point = ctypes.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return float(point.x), float(point.y)

    def move(self, x, y):
        if not self.user32.SetCursorPos(int(round(x)), int(round(y))):
            raise OSError("Windows refused to move the pointer (locked screen or secure desktop)")

    def button(self, which, how):
        if which in ("left", "right") and self.user32.GetSystemMetrics(23):  # SM_SWAPBUTTON
            # The flags name physical buttons, and a left-handed node swaps them: "left" is
            # the primary button here, so send the physical right one.
            which = "right" if which == "left" else "left"
        down, up = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}[which]
        if how in ("click", "down"):
            send_input(self.user32, "mouse", down, 0)
        if how in ("click", "up"):
            send_input(self.user32, "mouse", up, 0)

    def _vk(self, vk, up=False):
        self.user32.keybd_event(vk, 0, 2 if up else 0, 0)

    def type(self, text):
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]
        # One SendInput call for the whole text: sent one event at a time, Windows dropped or
        # repeated characters (2026-11-03: "desktop control" came out as "sssss...").
        units = text.encode("utf-16-le")
        codes = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
        events = (INPUT * (2 * len(codes)))()
        for i, code in enumerate(codes):
            for j, flags in enumerate((0x4, 0x4 | 0x2)):  # KEYEVENTF_UNICODE, then KEYUP
                events[2 * i + j].type = 1
                events[2 * i + j].ki = KEYBDINPUT(0, code, flags, 0, 0)
        sent = self.user32.SendInput(len(events), events, ctypes.sizeof(INPUT))
        if sent != len(events):
            raise OSError(f"Windows accepted {sent} of {len(events)} key events")

    def key(self, keys):
        mods, key = _parse_keys(keys)
        vk = self.VK.get(key) or (ord(key.upper()) if len(key) == 1 else None)
        if vk is None:
            raise ValueError(f"unknown key {key!r}")
        codes = [self.VK[m] for m in mods] + [vk]
        for c in codes:
            self._vk(c)
        for c in reversed(codes):
            self._vk(c, up=True)

    def scroll(self, direction, amount):
        self.user32.mouse_event(0x800, 0, 0, 120 * amount * (1 if direction == "up" else -1), 0)

    def release(self):
        for which in ("left", "right"):
            try:  # one refused up must not skip the rest (callers track what they hold)
                self.button(which, "up")
            except OSError:
                pass
        for m in ("ctrl", "shift", "alt", "logo"):
            self._vk(self.VK[m], up=True)


class MacOS:
    def _run(self, *args):
        return subprocess.run(list(args), capture_output=True, timeout=15, check=True)

    def shot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "s.png")
            self._run("screencapture", "-x", "-m", "-t", "png", str(path))
            bounds = self._run("osascript", "-e", 'tell application "Finder" to get bounds of window of desktop').stdout
            x0, y0, x1, y1 = [int(v) for v in bounds.decode().split(",")]
            return path.read_bytes(), {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}

    def _cli(self, *cmds):
        if not shutil.which("cliclick"):
            raise LookupError("cliclick isn't installed (brew install cliclick)")
        self._run("cliclick", *cmds)

    def cursor(self):
        try:
            x, y = self._run("cliclick", "p").stdout.decode().strip().split(",")
            return float(x), float(y)
        except (subprocess.SubprocessError, ValueError, OSError):
            return None

    def move(self, x, y):
        self._cli(f"m:{round(x)},{round(y)}")

    def button(self, which, how):
        x, y = self.cursor() or (0, 0)
        cmd = {("left", "click"): "c", ("right", "click"): "rc", ("left", "down"): "dd",
               ("left", "up"): "du"}.get((which, how))
        if not cmd:
            raise ValueError(f"{which} {how} isn't supported on macOS")
        self._cli(f"{cmd}:{round(x)},{round(y)}")

    def type(self, text):
        self._cli(f"t:{text}")

    def key(self, keys):
        mods, key = _parse_keys(keys)
        names = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "logo": "cmd"}
        cmds = [f"kd:{','.join(names[m] for m in mods)}"] if mods else []
        special = {"enter": "return", "esc": "esc", "escape": "esc", "tab": "tab", "space": "space",
                   "backspace": "delete", "delete": "fwd-delete", "up": "arrow-up", "down": "arrow-down",
                   "left": "arrow-left", "right": "arrow-right", "home": "home", "end": "end",
                   "pageup": "page-up", "pagedown": "page-down", **{f"f{i}": f"f{i}" for i in range(1, 17)}}
        if len(key) > 1 and key not in special:
            raise ValueError(f"unknown key {key!r} on macOS")
        cmds.append(f"kp:{special[key]}" if key in special else f"t:{key}")
        if mods:
            cmds.append(f"ku:{','.join(names[m] for m in mods)}")
        self._cli(*cmds)

    def scroll(self, direction, amount):
        raise LookupError("scrolling by wheel isn't supported on macOS yet; use key pagedown")

    def release(self):
        try:
            self._cli("du:.")
        except (LookupError, subprocess.SubprocessError, OSError):
            pass


def backend():
    found = platforms.detect()
    if found["os"] == "windows":
        return Windows()
    if found["os"] == "macos":
        return MacOS()
    if found["desktop"] == "hyprland":
        for tool in ("grim", "hyprctl", "ydotool", "wtype"):
            if not shutil.which(tool):
                raise LookupError(f"{tool} is missing (needed for desktop control on Hyprland)")
        return Hyprland()
    raise LookupError(f"desktop control isn't built for {found['label']} yet")


def _release():
    try:
        backend().release()
    except Exception:
        pass


def _release_remote():
    """The backend sweep above only knows buttons (and, on Windows, modifiers). A remote
    session here (or an ended one still retrying a refused key-up) tracks its own held keys:
    let go of those too, so a stop never leaves one down. Best effort, never raises."""
    try:
        from . import remote_view
        remote_view.release_held()
    except Exception:
        pass


# -- the action -----------------------------------------------------------------------------
def _shrink(png, width=SHOT_WIDTH):
    """(jpeg bytes, w, h) at most `width` wide; without Pillow the PNG goes as it is."""
    try:
        from PIL import Image
    except ImportError:
        import struct
        w, h = struct.unpack(">II", png[16:24])  # the PNG header's IHDR size
        return png, w, h
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=70, optimize=True)
    return out.getvalue(), image.width, image.height


def _real(spec, x_key="x", y_key="y"):
    """Screenshot pixels -> real screen coordinates, using the last screenshot's map."""
    m = _STATE["map"]
    if m is None:
        raise ValueError("take a screenshot first, so the coordinates mean something")
    try:
        x, y = float(spec[x_key]), float(spec[y_key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{spec.get('action')} needs {x_key} and {y_key} from the screenshot")
    if not (0 <= x < m["img_w"] and 0 <= y < m["img_h"]):
        raise ValueError(f"({x:g}, {y:g}) is outside the {m['img_w']}x{m['img_h']} screenshot")
    return m["x"] + x * m["w"] / m["img_w"], m["y"] + y * m["h"] / m["img_h"]


def _norm_keys(keys):
    return re.sub(r"\s*\+\s*", "+", str(keys or "").strip().lower())


def _typed_next(typed, action, spec):
    """The line being typed after `action`, or None when it can no longer be known.
    Cleared only by enter / a typed newline (checked first) and ctrl+c; ctrl+u clears only a
    known line, whose cursor is at the end (readline's ctrl+u keeps the text after the cursor,
    so on an unknown line it stays unknown, even after ctrl+a/ctrl+e/end); characters
    and backspace are tracked; cursor/editing keys (arrows, home/end, delete, tab, esc, other
    ctrl+ or alt+ combos) make it unknown, since the line may now differ from what was typed.
    So does a click or drag over a non-empty line: it may have moved the text cursor."""
    if action == "type":
        if typed is None:
            return None
        line = (typed + str(spec.get("text") or "")).rsplit("\n", 1)[-1]
        return line if len(line) <= TYPED_MAX else None  # past the cap: unknown, never cut
    if action in POINTER_ACTIONS:
        return "" if typed == "" else None
    if action != "key":
        return typed
    keys = _norm_keys(spec.get("keys"))
    if keys == "ctrl+u" and typed is None:  # cursor may be mid-line: text after it survives
        return None
    if keys in SUBMIT_KEYS or keys in CLEAR_KEYS:
        return ""
    parts = keys.split("+")
    mods, last = set(parts[:-1]), parts[-1]
    mods -= {"super", "win", "meta", "cmd"}  # window-manager keys don't touch the line
    if not last:
        return typed
    if typed is None:
        return None
    if mods <= {"shift"} and not (mods and not last.isalpha()):
        if last == "space":
            return typed + " " if len(typed) < TYPED_MAX else None
        if last == "backspace":
            return typed[:-1]
        if len(last) == 1:
            return typed + (last.upper() if mods else last) if len(typed) < TYPED_MAX else None
    return None


def act(spec, state=None, impl=None):
    """Run one action. Returns (ok, text); a screenshot's text is JSON with the image
    ({"note", "mime", "b64", "width", "height"})."""
    state = state or STATE
    action = str((spec or {}).get("action") or "").lower()
    if action not in ACTIONS:
        return False, f"action must be one of {', '.join(ACTIONS)}"
    try:
        b = impl or backend()
        _check(state, action, spec, b)
        if action == "screenshot":
            png, area = b.shot()
            data, w, h = _shrink(png)
            mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            with _LOCK:
                _STATE["map"] = {**area, "img_w": w, "img_h": h}
                _STATE["shot_time"] = time.time()
                stale = _STATE["left_at"] is None or \
                    time.time() - _STATE.get("left_time", 0) > TAKEOVER_WINDOW
            if stale:  # starting fresh: where the pointer is now is the baseline. Mid-task a
                here = b.cursor()  # screenshot never resets it, so David moving still counts.
                with _LOCK:
                    _STATE["left_at"], _STATE["left_time"] = here, time.time()
            _log(state, {"action": action, "size": [w, h]})
            return True, json.dumps({"note": f"Screenshot {w}x{h}; use these pixel coordinates.",
                                     "mime": mime, "b64": base64.b64encode(data).decode(),
                                     "width": w, "height": h})
        if time.time() - _STATE["announced"] > 120 and isinstance(b, Hyprland):
            _STATE["announced"] = time.time()
            subprocess.run(["hyprctl", "notify", "1", "4000", "rgb(ffaa00)",
                            "JARVIS is using the mouse and keyboard. Move the mouse to take over."],
                           capture_output=True, timeout=5)
        # Coordinates first: a bad one is refused before anything is pressed.
        if action in ("click", "double_click", "right_click", "move", "scroll", "drag"):
            x, y = _real(spec)
        if action == "drag":
            x2, y2 = _real(spec, "x2", "y2")
        try:
            if action in ("click", "double_click", "right_click", "move", "scroll"):
                b.move(x, y)
                if action == "click":
                    b.button("left", "click")
                elif action == "double_click":
                    b.button("left", "click"); time.sleep(0.08); b.button("left", "click")
                elif action == "right_click":
                    b.button("right", "click")
                elif action == "scroll":
                    direction = str(spec.get("direction") or "down").lower()
                    amount = max(1, min(int(spec.get("amount") or 3), 20))
                    b.scroll("up" if direction == "up" else "down", amount)
            elif action == "drag":
                b.move(x, y); b.button("left", "down"); time.sleep(0.1)
                b.move((x + x2) / 2, (y + y2) / 2); time.sleep(0.05)
                b.move(x2, y2); time.sleep(0.1); b.button("left", "up")
            elif action == "type":
                text = str(spec.get("text") or "")
                if not text:
                    raise ValueError("type needs text")
                b.type(text)
            elif action == "key":
                b.key(spec.get("keys"))
        except Exception:
            b.release()  # never leave a button or modifier held down
            raise
        here = b.cursor()
        with _LOCK:
            _STATE["left_at"], _STATE["left_time"] = here, time.time()
            _STATE["typed"] = _typed_next(_STATE["typed"], action, spec)
        _log(state, {"action": action, **{k: spec[k] for k in ("x", "y", "x2", "y2", "keys", "direction")
                                           if k in spec}, **({"chars": len(spec.get("text", ""))}
                                                             if action == "type" else {})})
        return True, f"Done: {action}. Take a screenshot to see the result."
    except Refused as exc:
        _log(state, {"action": action, "refused": str(exc)})
        return False, f"Refused: {exc}"
    except (ValueError, LookupError, KeyError, OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None)
        detail = detail.decode(errors="replace").strip()[-200:] if isinstance(detail, bytes) and detail else ""
        _log(state, {"action": action, "error": str(exc)[:200]})
        return False, f"Couldn't {action}: {detail or exc}"


def run(arg):
    """The `computer` desktop action over the cluster: arg is the JSON spec."""
    try:
        spec = json.loads(arg) if isinstance(arg, str) else dict(arg or {})
    except ValueError:
        return False, "computer takes a JSON spec"
    if not isinstance(spec, dict):
        return False, "computer takes a JSON object"
    if spec.get("action") == "stop":  # the main relays David's stop to every computer
        return stop()
    return act(spec)


def stop_everywhere(config, state):
    """Stop control here at once, then on every other computer (in the background: an
    offline node must not hold up the reply)."""
    result = stop(state)

    def relay():
        try:
            from .remote import Remote
            remote = Remote(config, state)
            for device in remote.names():
                if device != remote.here():
                    try:
                        remote.call(device, "computer", json.dumps({"action": "stop"}))
                    except Exception:
                        pass
        except Exception:
            pass
    threading.Thread(target=relay, name="control-stop-relay", daemon=True).start()
    return result


def relay_stop(config, state, timeout=6.0, remote=None):
    """Best-effort: tell every other computer to stop control. Each call runs in its own
    thread and `timeout` bounds the whole relay, so a slow or offline node never holds up the
    caller. Returns {"reached": [...], "failed": [...]}; never raises."""
    reached, failed = [], []
    try:
        if remote is None:
            from .remote import Remote
            remote = Remote(config, state)
        others = [d for d in remote.names() if d != remote.here()]
    except Exception:
        return {"reached": reached, "failed": failed}
    lock = threading.Lock()

    def one(device):
        try:
            result = remote.call(device, "computer", json.dumps({"action": "stop"}))
            good = bool(result[0]) if isinstance(result, (tuple, list)) and result else bool(result)
        except Exception:
            good = False
        with lock:
            (reached if good else failed).append(device)

    threads = [threading.Thread(target=one, args=(d,), name="control-stop-relay", daemon=True)
               for d in others]
    for t in threads:
        t.start()
    deadline = time.time() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.time()))
    with lock:
        done = set(reached) | set(failed)
        return {"reached": list(reached), "failed": list(failed) + [d for d in others if d not in done]}


def stop_all(config, state, timeout=6.0, remote=None):
    """Stop here first (always, whatever the relay does), then relay to the other computers."""
    ok, text = stop(state)
    relay = relay_stop(config, state, timeout=timeout, remote=remote)
    if relay["reached"]:
        text += " Also stopped on: " + ", ".join(relay["reached"]) + "."
    if relay["failed"]:
        text += " Not reached: " + ", ".join(relay["failed"]) + "."
    return ok, text
