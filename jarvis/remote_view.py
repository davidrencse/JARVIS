"""Remote control: David drives a node computer from the main's console, like AnyDesk.

Nothing new listens anywhere. It all rides the cluster hub the node already reaches over
its SSH tunnel (home LAN or, away from home, the Tailscale fallback target):

1. The console's Remote view asks the main's hub to open a session (Sessions.open). The
   hub sends the node one signed `remote` job: {"op": "start", "session", "quality"}.
2. The node starts a NodeSession (two daemon threads) and answers at once, so its job loop
   stays free for everything else:
   - frames: grab the screen, JPEG it, POST /v1/remote/frame. An unchanged screen is not
     resent (a small keep-alive goes every KEEPALIVE seconds instead).
   - input: POST /v1/remote/input, held by the hub until David's events arrive, then
     pressed here with the platform injector (Windows SendInput; Hyprland
     hyprctl + ydotool).
3. The console long-polls the hub's newest frame and posts David's mouse/keyboard events.

Safety:
- Only the main's own console can open a session. Nodes may not send `remote` jobs (the
  hub's /v1/control refuses them), and the hub takes frames and input polls only from the
  node a session was opened for, with that session's unguessable id.
- It is not the AI's `computer` tool and the brain has no tool for it: this is David's own
  hands, so there are no typed-line checks. It needs `desktop` capability on the node.
- A session ends when the console stops asking (tab closed: MAIN_IDLE), the node stops
  answering (NODE_IDLE), David presses Disconnect, control is stopped on the node
  (`jarvis control stop` there, or "Jarvis, stop controlling my computer"), or after
  MAX_SESSION. On the node, ending always releases every button and key it held.
- Every start and end is logged in the node's .jarvis/logs/control.jsonl.

Kinds and openers (for David's phone, phone.py): each hub session has a `kind` ("screen" here,
"camera" for camera.py's webcam view) and an `opener` ("console", or "phone:<id>"). There is
one live session per (computer, kind), and a phone can't take one another opener holds (Taken).
A phone sees only its own sessions: another opener's id reads as unknown. Camera sessions take
no input (WrongKind) and never pause Lan Mouse. The main itself can be the target too (the
hub runs the session in-process: cluster.Hub.remote_open(allow_main=True)).
"""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

QUALITY = {  # (max width, JPEG quality, frames per second at most)
    "low": (960, 45, 6),
    "medium": (1280, 60, 10),
    "high": (1920, 75, 15),
}
MAIN_IDLE = 20       # s without a console request before the hub ends a session
NODE_IDLE = 15       # s without a node request (after it started) before the hub ends it
START_GRACE = 30     # s a node has to send its first frame
MAX_SESSION = 4 * 3600
KEEPALIVE = 2.0      # s between frames when the screen hasn't changed
INPUT_HOLD = 2       # s the hub holds a node's input poll (short: the node's dead-man timer
                     # counts on a reply every few seconds while anything is held down)
CONSOLE_QUIET = 4    # s of total console silence (tab killed, link gone) before the hub
                     # tells the node to let go of everything
DEADMAN = 6          # s without an input reply before the node lets go of what it holds
FRAME_HOLD = 2.5     # s the hub holds a console frame request (its 204 also says the node is alive)
MAX_EVENTS = 200     # per console request
MAX_QUEUE = 2000     # events waiting for the node
MAX_FRAME = 2_500_000  # JPEG bytes (base64 of it stays under the cluster's MAX_RESULT)
KEEP_ENDED = 120     # s an ended session stays readable (so the console can say why)
SESSION_RE = re.compile(r"[0-9a-f]{32}")
CODE_RE = re.compile(r"[A-Za-z0-9]{1,24}")
BUTTONS = ("left", "right", "middle")
KINDS = ("screen", "camera")
OPENER_RE = re.compile(r"console|phone:[A-Za-z0-9_-]{1,64}")
CAMERA_MAX = 15 * 60  # s a camera session may run (camera.MAX_SESSION ends it on the node too)
_DPI_AWARE = False


def desktop_problem():
    """Return a useful Windows desktop-session problem, without blocking session startup."""
    try:
        import ctypes
        from . import platforms
        if platforms.detect()["os"] != "windows":
            return None
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        session = ctypes.c_ulong()
        if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
            return None
        if session.value == 0:
            return "JARVIS on this computer runs in session 0 (a service or scheduled task) and can't see the screen. Start it from the Startup folder."
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        desktop = user32.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            return "The screen is locked or a UAC prompt is showing."
        user32.CloseDesktop(ctypes.c_void_p(desktop))
        return None
    except Exception:
        return None


class Ended(Exception):
    """The session is over; str() says why."""


class WrongKind(ValueError):
    """A call that doesn't fit the session's kind: a camera session takes no input."""
    status = 409  # the phone server answers with it (the hub's own routes keep 400)


class Taken(RuntimeError):
    """Someone else (the console or another phone) already has that computer's screen or
    camera. Only the console may replace another opener's session."""
    status = 409


def _unit(value):
    """A 0-1 coordinate; a hair outside (a drag past the edge) is clamped, not refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("x and y must be numbers from 0 to 1")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError("x and y must be numbers from 0 to 1") from None
    if value != value or not -0.05 <= value <= 1.05:  # NaN, or far outside
        raise ValueError("x and y must be numbers from 0 to 1")
    return min(1.0, max(0.0, value))


def clean_event(event):
    """One console input event, checked and reduced to the fields the node uses."""
    if not isinstance(event, dict):
        raise ValueError("Each input event must be an object.")
    kind = event.get("t")
    if kind == "move":
        return {"t": "move", "x": _unit(event.get("x")), "y": _unit(event.get("y"))}
    if kind in ("down", "up"):
        if event.get("b") not in BUTTONS:
            raise ValueError("Mouse button must be left, right or middle.")
        out = {"t": kind, "b": event["b"]}
        if "x" in event or "y" in event:
            out.update(x=_unit(event.get("x")), y=_unit(event.get("y")))
        return out
    if kind == "wheel":
        dy = event.get("dy")
        if isinstance(dy, bool) or not isinstance(dy, int) or not -20 <= dy <= 20 or not dy:
            raise ValueError("wheel dy must be a whole number from -20 to 20, not 0.")
        return {"t": "wheel", "dy": dy}
    if kind == "key":
        code = event.get("code")
        if not isinstance(code, str) or not CODE_RE.fullmatch(code) or not isinstance(event.get("down"), bool):
            raise ValueError("A key event needs a KeyboardEvent code and down true/false.")
        return {"t": "key", "code": code, "down": event["down"]}
    if kind == "text":
        text = event.get("text")
        if not isinstance(text, str) or not 0 < len(text) <= 1000:
            raise ValueError("text must be 1-1000 characters.")
        if any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text):
            raise ValueError("text has characters that can't be typed.")
        return {"t": "text", "text": text}
    if kind == "release":
        return {"t": "release"}
    raise ValueError("Unknown input event.")


def reply_mac(key, kind, sid, reply):
    """HMAC of one hub reply to a node's frame or input request (kind "frame"/"input")."""
    body = {k: v for k, v in reply.items() if k != "mac"}
    text = json.dumps({"kind": kind, "session": sid, "reply": body}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, default=str)
    return hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()


# -- the main's side: the hub keeps sessions in memory -----------------------------------
SWEEP = 5.0  # s between hub expiry checks while a session is live


class Sessions:
    """Every remote session the hub knows about. One live session per (node, kind)."""

    def __init__(self, clock=time.monotonic, on_busy=None):
        self.clock = clock
        self._cond = threading.Condition()
        self._all = {}
        self.on_busy = on_busy  # called with True/False: a session is (not) live; must be quick
        self._sweeper = None

    def _sweep(self):
        """While any session is live, expire on a timer too: a crashed browser plus a node
        that lost power send no requests, and the main's Lan Mouse must not stay paused."""
        while True:
            time.sleep(SWEEP)
            with self._cond:
                self._expire()
                if not any(s["state"] != "ended" for s in self._all.values()):
                    self._sweeper = None
                    return

    def _busy(self):
        live = [s for s in self._all.values() if s["state"] != "ended"]
        if self._sweeper is None and live:
            self._sweeper = threading.Thread(target=self._sweep, name="remote-sweeper", daemon=True)
            self._sweeper.start()
        if self.on_busy is not None:
            try:  # only screen sessions drive a desktop: a camera view never pauses Lan Mouse
                self.on_busy(any(s["kind"] == "screen" for s in live))
            except Exception:
                pass

    def _expire(self):
        now = self.clock()
        for sid, s in list(self._all.items()):
            if s["state"] == "ended":
                if now - s["ended_at"] > KEEP_ENDED:
                    del self._all[sid]
                continue
            why = None
            if not s["pending"] and now - s["main_seen"] > MAIN_IDLE:  # pending: Connect still waiting
                who = "console" if s["opener"] == "console" else "phone"
                why = f"The {who} stopped watching, so the session ended."
            elif s["state"] == "live" and now - s["frame_seen"] > NODE_IDLE:  # frames, not input polls
                why = f"{s['node']} stopped answering (offline or asleep)."
            elif s["state"] == "starting" and now - s["opened"] > START_GRACE:
                why = f"{s['node']} never sent its {'camera picture' if s['kind'] == 'camera' else 'screen'}."
            elif s["kind"] == "camera" and now - s["opened"] > CAMERA_MAX:
                why = "Camera sessions end after 15 minutes; start again to go on."
            elif now - s["opened"] > MAX_SESSION:
                why = "Sessions end after 4 hours; connect again to go on."
            if why:
                self._end(s, why)
        self._busy()

    def _end(self, s, why, notify=True):
        if s["state"] != "ended":
            s.update(state="ended", error=why, ended_at=self.clock(), frame=None)
            s["events"].clear()
            self._cond.notify_all()
            if notify:
                self._busy()

    def _get(self, sid):
        if not isinstance(sid, str) or not SESSION_RE.fullmatch(sid):
            raise ValueError("Unknown remote session.")
        s = self._all.get(sid)
        if s is None:
            raise LookupError("Unknown remote session.")
        return s

    def _owned(self, sid, opener):
        """The session, if `opener` opened it (None: any opener, the console's own calls).
        Another opener's session reads as unknown, so a phone can't probe for ids."""
        s = self._get(sid)
        if opener is not None and s["opener"] != opener:
            raise LookupError("Unknown remote session.")
        return s

    @staticmethod
    def view(s):
        return {"id": s["id"], "device": s["node"], "state": s["state"], "error": s["error"],
                "width": s["width"], "height": s["height"], "frames": s["seq"],
                "quality": s["quality"], "started": s["wall"], "kind": s["kind"], "opener": s["opener"]}

    def open(self, node, quality="medium", pending=False, *, kind="screen", opener="console"):
        """pending=True: the console's start request is still waiting on the node (touch()
        ends that), so console silence doesn't end the session yet. A live session of the
        same kind on that node is replaced, but only the console (or the same opener) may
        replace one: a phone gets Taken instead of cutting off someone else."""
        if quality not in QUALITY:
            raise ValueError("Quality must be low, medium or high.")
        if kind not in KINDS:
            raise ValueError("Session kind must be screen or camera.")
        if not isinstance(opener, str) or not OPENER_RE.fullmatch(opener):
            raise ValueError("Unknown session opener.")
        with self._cond:
            self._expire()
            live = [s for s in self._all.values()
                    if s["node"] == node and s["kind"] == kind and s["state"] != "ended"]
            if opener != "console" and any(s["opener"] != opener for s in live):
                raise Taken("Someone else is already watching that camera." if kind == "camera" else
                            "Someone else is already controlling that computer.")
            for s in live:
                # No busy report here: the new session is inserted next, and a False in
                # between would restart the main's Lan Mouse mid-handoff.
                self._end(s, "Replaced by a new session.", notify=False)
            now = self.clock()
            sid = secrets.token_hex(16)
            self._all[sid] = {"id": sid, "node": node, "quality": quality, "state": "starting",
                              "error": None, "opened": now, "wall": time.time(), "main_seen": now,
                              "node_seen": now, "frame_seen": now, "ended_at": None, "seq": 0, "frame": None,
                              "width": 0, "height": 0, "events": [], "eseq": 0,
                              "quiet_released": False, "pending": bool(pending),
                              "key": secrets.token_hex(16), "kind": kind, "opener": opener}
            self._busy()
            return self.view(self._all[sid])

    def touch(self, sid):
        """The console's start request came back: count it as the console watching now. Until
        then (pending) the hub doesn't end the session for console silence, since a start can
        wait on the node longer than MAIN_IDLE."""
        with self._cond:
            s = self._get(sid)
            s["pending"] = False
            s["main_seen"] = self.clock()

    def current(self, kind="screen", opener="console"):
        """The newest session that isn't over (or the newest ended one, for its reason). By
        default only the console's own screen sessions: its take-over offer must never pick
        up a phone's session or a camera view. None: any kind / any opener."""
        with self._cond:
            self._expire()
            found = sorted((s for s in self._all.values()
                            if (kind is None or s["kind"] == kind) and (opener is None or s["opener"] == opener)),
                           key=lambda s: (s["state"] != "ended", s["opened"]))
            return self.view(found[-1]) if found else None

    def views(self, opener=None):
        """Every session `opener` opened that the hub still knows (None: all), oldest first."""
        with self._cond:
            self._expire()
            return [self.view(s) for s in sorted(self._all.values(), key=lambda s: s["opened"])
                    if opener is None or s["opener"] == opener]

    def get(self, sid, opener=None):
        with self._cond:
            self._expire()
            return self.view(self._owned(sid, opener))

    def kind(self, node, sid):
        """The kind of `node`'s session sid (the hub checks it against the node's capabilities)."""
        with self._cond:
            return self._node_session(node, sid)["kind"]

    def close(self, sid, why="Disconnected.", opener=None):
        with self._cond:
            s = self._owned(sid, opener)
            self._end(s, why)
            return self.view(s)

    def close_all(self, why):
        with self._cond:
            for s in self._all.values():
                self._end(s, why)

    def end_opener(self, opener, why):
        """End every live session `opener` opened (a phone unpaired or signed out)."""
        with self._cond:
            ended = []
            for s in self._all.values():
                if s["opener"] == opener and s["state"] != "ended":
                    self._end(s, why)
                    ended.append(self.view(s))
            return ended

    # console -> hub
    def _queue(self, s, event):
        s["eseq"] += 1
        s["events"].append((s["eseq"], event))

    def push(self, sid, events, opener=None):
        """Queue David's events for the node. A bad event is dropped on its own, so one odd
        value never costs the batch its key-up. Returns how many were queued."""
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            raise ValueError(f"events must be a list of at most {MAX_EVENTS}.")
        clean = []
        for event in events:
            try:
                clean.append(clean_event(event))
            except ValueError:
                continue
        with self._cond:
            self._expire()
            s = self._owned(sid, opener)
            if s["kind"] != "screen":
                raise WrongKind("A camera view takes no mouse or keyboard input.")
            if s["state"] == "ended":
                raise Ended(s["error"] or "The session ended.")
            s["main_seen"] = self.clock()
            s["quiet_released"] = False
            queue = s["events"]
            for event in clean:
                if event["t"] == "move" and queue and queue[-1][1]["t"] == "move":
                    queue.pop()  # only the newest pointer position matters (new number: resent)
                self._queue(s, event)
            if len(queue) > MAX_QUEUE:  # the node is far behind: drop it all, let go of everything
                queue.clear()
                self._queue(s, {"t": "release"})
            self._cond.notify_all()
            return len(clean)

    def wait_frame(self, sid, after, hold=FRAME_HOLD, opener=None):
        """(seq, jpeg, width, height) newer than `after`, or None after `hold` seconds."""
        deadline = self.clock() + hold
        with self._cond:
            while True:
                self._expire()
                s = self._owned(sid, opener)
                s["main_seen"] = self.clock()
                if s["state"] == "ended":
                    raise Ended(s["error"] or "The session ended.")
                if s["frame"] is not None and s["seq"] > after:
                    return s["seq"], s["frame"], s["width"], s["height"]
                left = deadline - self.clock()
                if left <= 0:
                    return None
                self._cond.wait(min(left, 1))

    def node_ago(self, sid, opener=None):
        """Seconds since the session's node last sent a frame or a frame keep-alive. Input polls
        don't count: a node whose frame thread is wedged while its input thread still polls
        shows a frozen picture, which must not read as live."""
        with self._cond:
            return max(0.0, self.clock() - self._owned(sid, opener)["frame_seen"])

    def key(self, sid):
        """The session's secret, sent to the node only inside its signed start job."""
        with self._cond:
            return self._get(sid)["key"]

    def seal(self, sid, kind, reply):
        """reply plus a MAC under the session's key, so the node can tell the hub that opened
        the session from a program squatting its tunnel port (which never saw the key)."""
        with self._cond:
            s = self._all.get(sid) if isinstance(sid, str) else None
            key = s["key"] if s else None
        if not key or not isinstance(reply, dict):
            return reply
        return {**reply, "mac": reply_mac(key, kind, sid, reply)}

    # node -> hub
    def _node_session(self, node, sid):
        s = self._get(sid)
        if s["node"] != node:
            raise PermissionError("That remote session belongs to another computer.")
        return s

    def frame(self, node, sid, jpeg, width, height):
        """A node's new frame (jpeg None: the screen hasn't changed). {"stop": bool}."""
        if jpeg is not None and len(jpeg) > MAX_FRAME:
            raise ValueError("Frame too large.")
        if jpeg is not None and jpeg[:3] != b"\xff\xd8\xff":
            raise ValueError("Frames must be JPEG.")
        with self._cond:
            self._expire()
            s = self._node_session(node, sid)
            if s["state"] == "ended":
                return {"stop": True, "why": s["error"]}
            s["node_seen"] = s["frame_seen"] = self.clock()
            if jpeg is not None:
                for v in (width, height):
                    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= 20000:
                        raise ValueError("Bad frame size.")
                # Always kept: the console only ever asks for the newest frame, and the node
                # counts any reply as "sent", so a dropped one would never be resent.
                s.update(frame=jpeg, width=width, height=height, seq=s["seq"] + 1, state="live")
                self._cond.notify_all()
            return {"stop": False}

    def node_end(self, node, sid, why):
        """The node ended the session itself (control stopped there, a crash)."""
        why = str(why or "The computer ended the session.")[:300]
        with self._cond:
            self._end(self._node_session(node, sid), why)
            return {"stop": True}

    def take_input(self, node, sid, after=0, hold=INPUT_HOLD, stop_event=None):
        """Events numbered past `after` (the last one the node pressed), held up to `hold` s.
        Events stay queued until a later poll's `after` says they were pressed, so a reply
        lost on the way (a dropped link) is sent again, never lost: a key-up always lands.
        {"events": [[n, event]...], "stop"}."""
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be the last event number pressed.")
        deadline = self.clock() + hold
        with self._cond:
            self._expire()
            s = self._node_session(node, sid)
            if s["kind"] != "screen":
                raise WrongKind("A camera view takes no mouse or keyboard input.")
            s["node_seen"] = self.clock()  # once per poll: a dead socket's wait doesn't count
            s["events"] = [(n, e) for n, e in s["events"] if n > after]
            while True:
                if s["state"] == "ended":
                    return {"events": [], "stop": True, "why": s["error"]}
                if not s["quiet_released"] and self.clock() - s["main_seen"] > CONSOLE_QUIET:
                    s["quiet_released"] = True  # the console went silent: nothing stays held
                    self._queue(s, {"t": "release"})
                waiting = [[n, e] for n, e in s["events"] if n > after]
                if waiting:
                    return {"events": waiting, "stop": False}
                left = deadline - self.clock()
                if left <= 0 or (stop_event is not None and stop_event.is_set()):
                    return {"events": [], "stop": False}
                self._cond.wait(min(left, 0.5))
                self._expire()


# -- the node's side ---------------------------------------------------------------------
# KeyboardEvent.code -> (Windows virtual key, extended?, Linux evdev code)
_LETTERS = dict(zip("QWERTYUIOPASDFGHJKLZXCVBNM",
                    [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 30, 31, 32, 33, 34, 35, 36, 37, 38,
                     44, 45, 46, 47, 48, 49, 50]))
KEYS = {
    **{f"Key{c}": (ord(c), False, ev) for c, ev in _LETTERS.items()},
    **{f"Digit{d}": (0x30 + d, False, 11 if d == 0 else d + 1) for d in range(10)},
    **{f"F{i}": (0x6F + i, False, (58 + i) if i <= 10 else (87 if i == 11 else 88)) for i in range(1, 13)},
    **{f"Numpad{d}": (0x60 + d, False, ev) for d, ev in
       zip(range(10), (82, 79, 80, 81, 75, 76, 77, 71, 72, 73))},
    "Enter": (0x0D, False, 28), "NumpadEnter": (0x0D, True, 96), "Escape": (0x1B, False, 1),
    "Backspace": (0x08, False, 14), "Tab": (0x09, False, 15), "Space": (0x20, False, 57),
    "Minus": (0xBD, False, 12), "Equal": (0xBB, False, 13), "BracketLeft": (0xDB, False, 26),
    "BracketRight": (0xDD, False, 27), "Backslash": (0xDC, False, 43), "Semicolon": (0xBA, False, 39),
    "Quote": (0xDE, False, 40), "Backquote": (0xC0, False, 41), "Comma": (0xBC, False, 51),
    "Period": (0xBE, False, 52), "Slash": (0xBF, False, 53), "IntlBackslash": (0xE2, False, 86),
    "CapsLock": (0x14, False, 58), "ShiftLeft": (0xA0, False, 42), "ShiftRight": (0xA1, False, 54),
    "ControlLeft": (0xA2, False, 29), "ControlRight": (0xA3, True, 97), "AltLeft": (0xA4, False, 56),
    "AltRight": (0xA5, True, 100), "MetaLeft": (0x5B, True, 125), "MetaRight": (0x5C, True, 126),
    "ContextMenu": (0x5D, True, 127), "ArrowUp": (0x26, True, 103), "ArrowDown": (0x28, True, 108),
    "ArrowLeft": (0x25, True, 105), "ArrowRight": (0x27, True, 106), "Home": (0x24, True, 102),
    "End": (0x23, True, 107), "PageUp": (0x21, True, 104), "PageDown": (0x22, True, 109),
    "Insert": (0x2D, True, 110), "Delete": (0x2E, True, 111), "PrintScreen": (0x2C, True, 99),
    "ScrollLock": (0x91, False, 70), "Pause": (0x13, False, 119), "NumLock": (0x90, True, 69),
    "NumpadMultiply": (0x6A, False, 55), "NumpadAdd": (0x6B, False, 78),
    "NumpadSubtract": (0x6D, False, 74), "NumpadDecimal": (0x6E, False, 83),
    "NumpadDivide": (0x6F, True, 98),
}
ALIASES = {"OSLeft": "MetaLeft", "OSRight": "MetaRight"}


def _key_code(code):
    return ALIASES.get(code, code)


class WindowsKeys:
    """Raw key down/up on Windows: SendInput with the scan code and extended flag, so
    arrows and right-hand modifiers aren't taken for numpad keys. A refused press raises
    OSError (keybd_event returned nothing), so a key-up that didn't land stays held."""

    def __init__(self):
        import ctypes
        self.user32 = ctypes.windll.user32

    def key(self, code, down):
        code = _key_code(code)
        vk, extended, _ = KEYS[code]
        scan = self.user32.MapVirtualKeyW(vk, 0)
        from .control import send_input
        send_input(self.user32, "key", vk, scan, (0x1 if extended else 0) | (0 if down else 0x2))


class EvdevKeys:
    """Raw key down/up on Linux through ydotool (evdev codes)."""

    def key(self, code, down):
        import subprocess
        code = _key_code(code)
        subprocess.run(["ydotool", "key", f"{KEYS[code][2]}:{1 if down else 0}"],
                       capture_output=True, timeout=5, check=True)


_DOING = {"move": "move the pointer", "down": "click", "up": "click", "wheel": "scroll",
          "key": "press keys", "text": "type", "release": "let go of keys"}


def failure_message(event, exc):
    """Why the session ended after MAX_FAILS presses failed in a row, named for what failed.
    On Windows a secure desktop (UAC prompt, lock screen) is the usual cause, so say so."""
    kind = event.get("t") if isinstance(event, dict) else None
    text = f"Couldn't {_DOING.get(kind, 'press keys')} here: {exc}"
    problem = desktop_problem()
    if problem:
        text = f"{text}. {problem}"
    return text[:300]


class Stopped(Exception):
    """Control was stopped on this computer, or the session already let go for good."""


class Injector:
    """David's events, pressed on this computer. Tracks what it holds down so ending
    always lets go of everything.

    One lock covers pressing and letting go: release() waits for a press in progress, so
    a key-down can never land after the final release (close=True), and none is accepted
    after it. A key or button counts as up only once its up event went through, so a
    failed up is tried again by every later release."""

    def __init__(self, pointer=None, keys=None, stopped=None):
        if pointer is None:
            from . import control, platforms
            found = platforms.detect()
            if found["os"] == "macos":
                raise LookupError("Remote control isn't built for macOS computers yet.")
            pointer = control.backend()  # LookupError when this desktop isn't supported
            keys = keys or (WindowsKeys() if found["os"] == "windows" else EvdevKeys())
        self.pointer, self.keys = pointer, keys
        self.stopped = stopped or (lambda: False)
        self.area = None  # {"x", "y", "w", "h"}: the real screen the frames show
        self.buttons, self.held = set(), set()
        self.closed = False
        self._lock = threading.RLock()
        # Called with (held, buttons) after a key or button is added before it's pressed, and
        # after it's let go: the node writes it into the session marker, so a restart after a
        # crash knows exactly what to let go of (a fresh Injector holds nothing).
        self.on_change = None

    def _changed(self):
        if self.on_change is not None:
            try:
                self.on_change(sorted(self.held), sorted(self.buttons))
            except Exception:
                pass

    def busy(self):
        return bool(self.buttons or self.held)

    def _at(self, event):
        if self.area is None or "x" not in event:
            return
        a = self.area
        # Never past the last whole pixel: the backends round, and x + 0.9999 * w rounds to
        # x + w, the first pixel of the monitor to the right (or below).
        x = a["x"] + min(event["x"] * a["w"], max(a["w"] - 1, 0))
        y = a["y"] + min(event["y"] * a["h"], max(a["h"] - 1, 0))
        self.pointer.move(x, y)

    def apply(self, event):
        with self._lock:
            if self.closed:
                raise Stopped("The session already let go.")
            if self.stopped():
                raise Stopped("Control was stopped on this computer.")
            kind = event["t"]
            if kind == "move":
                self._at(event)
            elif kind in ("down", "up"):
                self._at(event)
                if kind == "down":
                    self.buttons.add(event["b"])  # before: a failed down may still have pressed
                    self._changed()
                self.pointer.button(event["b"], kind)
                if kind == "up":
                    self.buttons.discard(event["b"])
                    self._changed()
            elif kind == "wheel":
                self.pointer.scroll("down" if event["dy"] > 0 else "up", abs(event["dy"]))
            elif kind == "key":
                code = _key_code(event["code"])
                if code not in KEYS:
                    return  # a key this computer has no code for (media keys...): ignored
                if event["down"] and code not in self.held:
                    self.held.add(code)
                    self._changed()
                self.keys.key(code, event["down"])
                if not event["down"] and code in self.held:
                    self.held.discard(code)
                    self._changed()
            elif kind == "text":
                self.pointer.type(event["text"])
            elif kind == "release":
                self.release()

    def release(self, close=False, backend=True):
        """Let go of every button and key this session pressed, then the backend's own
        release (mouse buttons and modifiers). close=True: for good, nothing presses after.
        backend=False: only the ups for what this session still holds (the late retry: the
        backend's sweep would also lift buttons/modifiers someone else holds now)."""
        with self._lock:
            if close:
                self.closed = True
            for attempt in range(2 if close else 1):
                for code in list(self.held):
                    try:
                        self.keys.key(code, False)
                        self.held.discard(code)
                    except Exception:
                        pass
                for which in list(self.buttons):
                    try:
                        self.pointer.button(which, "up")
                        self.buttons.discard(which)
                    except Exception:
                        pass
                if not self.busy():
                    break
                time.sleep(0.2)
            self._changed()
            if not backend:
                return
            try:
                self.pointer.release()
            except Exception:
                pass


def grab():
    """(PIL image, {"x","y","w","h"}) of this computer's screen in real pixels."""
    from PIL import Image
    from . import platforms
    if platforms.detect()["os"] == "windows":
        import ctypes
        from PIL import ImageGrab
        global _DPI_AWARE
        if not _DPI_AWARE:
            _set_dpi_awareness(ctypes)
            _DPI_AWARE = True
        m = ctypes.windll.user32.GetSystemMetrics
        try:
            image = ImageGrab.grab(all_screens=True, include_layered_windows=True)
        except TypeError:
            image = ImageGrab.grab(all_screens=True)
        # The area is in SetCursorPos's coordinates, which are GetSystemMetrics' own; clicks
        # arrive as 0-1 fractions of the picture, so the picture's pixel size never enters
        # the mapping (even if DPI virtualisation made it differ).
        return image, {"x": m(76), "y": m(77), "w": m(78), "h": m(79)}
    from . import control
    backend = control.backend()
    if isinstance(backend, control.Hyprland):
        return _grab_hyprland(backend, backend._monitor())
    png, area = backend.shot()
    return Image.open(io.BytesIO(png)), area


def hyprland_area(mon):
    """A Hyprland monitor's area in layout coordinates: pixels / scale, with width and height
    swapped when it's rotated 90 or 270 degrees (transforms 1, 3, 5, 7)."""
    scale = float(mon.get("scale") or 1)
    w, h = mon["width"] / scale, mon["height"] / scale
    if int(mon.get("transform") or 0) % 2:
        w, h = h, w
    return {"x": mon["x"], "y": mon["y"], "w": w, "h": h}


def _grab_hyprland(backend, mon):
    from PIL import Image  # PPM: grim skips PNG compression (~55 ms, not ~250)
    raw = backend._run("grim", "-o", mon["name"], "-t", "ppm", "-").stdout
    return Image.open(io.BytesIO(raw)), hyprland_area(mon)


class Grabber:
    """grab() for one session. On Hyprland the backend and the monitor are picked once, at
    the first frame: the picture no longer jumps to whichever monitor gets focus (and the
    clicks with it), and each frame skips four `which` lookups and a hyprctl call. A failed
    capture (monitor unplugged) picks the monitor again next time."""

    def __init__(self, detect=None, backend=None):
        self._detect, self._backend, self._mon, self._os = detect, backend, None, None

    def __call__(self):
        if self._os is None:
            from . import platforms
            self._os = (self._detect or platforms.detect)()["os"]
        if self._os == "windows":
            return grab()
        from . import control
        if self._backend is None:
            self._backend = control.backend()
        backend = self._backend
        if not isinstance(backend, control.Hyprland):
            from PIL import Image
            png, area = backend.shot()
            return Image.open(io.BytesIO(png)), area
        try:
            if self._mon is None:
                self._mon = backend._monitor()
            return _grab_hyprland(backend, self._mon)
        except Exception:
            self._mon = None
            raise


def _set_dpi_awareness(ctypes):
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return  # FALSE: this Windows has no per-monitor v2, try the older calls
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def encode(image, quality):
    """(jpeg bytes, width, height) at most the quality's width wide."""
    from PIL import Image
    width, q, _ = QUALITY[quality]
    if image.mode != "RGB":
        image = image.convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.BILINEAR, reducing_gap=2.0)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=q)
    return out.getvalue(), image.width, image.height


def _log(state, entry):
    try:
        path = Path(state) / "logs" / "control.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "remote": True, **entry}) + "\n")
    except OSError:
        pass


def notify_here(text, title="JARVIS"):
    """A desktop notification on this computer, so whoever sits at it sees that David's phone
    drives the screen or that the camera is on. Best effort and never blocks: it runs on a
    daemon thread. Silent under tests and with JARVIS_NO_NOTIFY set. True when one was sent."""
    from . import _under_tests
    if os.environ.get("JARVIS_NO_NOTIFY") or _under_tests():
        return False
    threading.Thread(target=_show_notice, args=(str(title)[:80], str(text)[:300]),
                     name="jarvis-notice", daemon=True).start()
    return True


# The text rides in environment variables, never in the script, so nothing in it can run.
_BALLOON = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.BalloonTipTitle = $env:JARVIS_NOTICE_TITLE; $n.BalloonTipText = $env:JARVIS_NOTICE_TEXT; "
            "$n.Visible = $true; $n.ShowBalloonTip(8000); Start-Sleep -Seconds 9; $n.Dispose()")


def _show_notice(title, text, popen=None, run=None, which=None):
    """notify-send on Linux; on Windows a balloon from a hidden, detached PowerShell (it has
    no notify-send). Never raises."""
    import shutil
    import subprocess
    from . import platforms
    which = which or shutil.which
    try:
        if platforms.on_windows():
            flags = platforms.NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            (popen or subprocess.Popen)(
                [which("powershell") or "powershell", "-NoProfile", "-NonInteractive", "-WindowStyle",
                 "Hidden", "-Command", _BALLOON],
                env={**os.environ, "JARVIS_NOTICE_TITLE": title, "JARVIS_NOTICE_TEXT": text},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=flags)
            return
        exe = which("notify-send")
        if exe:
            (run or subprocess.run)([exe, "-a", "JARVIS", title, text], timeout=5,
                                    capture_output=True, check=False)
    except Exception:
        pass


MARKER = "remote-session.json"  # on the node while a session runs; found at start = it died
MARKER_TOUCH = 10.0  # s between touches; sharedinput.REMOTE_MARKER_TTL (60 s) makes it stale


class NodeSession:
    """One remote session on the node: a frame thread, an input thread and a dead-man
    watchdog. Ending lets go of everything at once (end()), from whichever thread."""

    LINK_LOSS = 15  # s of failed requests to the main before the node ends the session
    MAX_FAILS = 5   # presses in a row that fail before the session ends
    LATE_RELEASE_FOR = 600.0  # s a refused up at the end is retried before the marker is left to recover()

    def __init__(self, config, state, sid, quality, client=None, injector=None, capture=None, encoder=None,
                 key=None, *, lan_mouse=True, announce=False):
        from .cluster import Client
        self.config, self.state, self.sid, self.quality = config, Path(state), sid, quality
        # From the signed start job: every frame/input reply must carry its MAC, or it came
        # from something squatting the tunnel port, not the hub that opened the session.
        self.key = key
        # lan_mouse=False: a session on the main itself, where the hub's ControllerPause owns
        # Lan Mouse (two owners would both stop it and both start it again).
        self.lan_mouse = lan_mouse
        self.announce = announce  # a notification here at start and end (a phone opened it)
        self.client = client or Client(config, state)
        self.injector = injector or Injector()
        self.injector.stopped = self._stopped_here
        self.capture, self.encoder = capture or Grabber(), encoder or encode
        self.stop_event = threading.Event()
        self.why = None
        self._done = False
        self.hub_ended = False  # the main ended it: nothing to tell it
        self.last_reply = time.monotonic()
        self.threads = []
        self._live = 0
        self._count = threading.Lock()
        self._checked = (0.0, False)
        self._lan_mouse_was_running = False
        self.release_thread = None  # set when it ended with an up refused (_release_later)

    def _suspend_lan_mouse(self):
        """Stop input sharing on this node while the remote session owns its desktop.

        _lan_mouse_was_running means "this session owes a restart". A session that replaced
        one which had stopped Lan Mouse inherits that debt (it reads "not running" itself)."""
        with _LAN_LOCK:
            try:
                from . import sharedinput
                running = bool(sharedinput.lan_mouse_status().get("running"))
                self._lan_mouse_was_running = self._lan_mouse_was_running or running
                if running:
                    result = sharedinput.stop(sharing_only=True)
                    if not result.get("stopped"):
                        _log(self.state, {"action": "remote_lan_mouse_error", "error": "Lan Mouse did not stop"})
            except Exception as exc:
                _log(self.state, {"action": "remote_lan_mouse_error", "error": str(exc)[:200]})

    def _restore_lan_mouse(self):
        """Bring Lan Mouse back, unless another session here still drives the desktop (or is
        starting): then that one inherits the restart, so it happens once, after the last."""
        with _LAN_LOCK:
            if not self._lan_mouse_was_running:
                return
            self._lan_mouse_was_running = False
            heir = next((s for s in list(_ACTIVE.values())
                         if s is not self and not s._done and getattr(s, "lan_mouse", True)), None)
            if heir is not None:
                heir._lan_mouse_was_running = True
                # Its marker must say so now: a crash before its next press still pays it once.
                with heir.injector._lock:
                    heir._mark(sorted(heir.injector.held), sorted(heir.injector.buttons))
                return
            if _LAN["starting"]:
                _LAN["owed"] = True  # run() is starting a session: it picks this up
                return
            _lan_mouse_back(self.state)

    def _stopped_here(self):
        """The control stop file, read at most every 0.1 s (checked before every press)."""
        at, value = self._checked
        if time.monotonic() - at > 0.1:
            from .control import _stopped
            value = _stopped(self.state)
            self._checked = (time.monotonic(), value)
        return value

    def end(self, why):
        """Stop, and let go of every key and button now (waits for a press in progress)."""
        if not self.stop_event.is_set():
            self.why = why
            self.stop_event.set()
        self.injector.release(close=True)

    def join(self, timeout=None):
        for thread in self.threads:
            thread.join(timeout)
        late = self.release_thread
        if late is not None and timeout is not None:  # bounded: the retry itself runs up to 10 min
            late.join(timeout)

    def _mark(self, held=(), buttons=()):
        if self._done:  # ended: a late release must not bring back the marker
            return
        if self.stop_event.is_set() and any(s is not self and not s.stop_event.is_set()
                                            for s in list(_ACTIVE.values())):
            return  # ending while a newer session runs: the marker is that one's now
        self._write_marker(held, buttons)

    def _write_marker(self, held, buttons):
        try:
            (self.state / MARKER).write_text(json.dumps({
                "session": self.sid[:8], "at": time.time(), "held": list(held), "buttons": list(buttons),
                # this session owes a Lan Mouse restart: recover() pays it after a crash
                "lan_mouse": bool(self._lan_mouse_was_running)}), encoding="utf-8")
        except OSError:
            pass

    def _marker_ours(self):
        """The marker is this session's (or gone) and no other session here is live."""
        if any(s is not self and not s._done for s in list(_ACTIVE.values())):
            return False
        try:
            data = json.loads((self.state / MARKER).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            return True  # unreadable, and nobody else is live
        return not isinstance(data, dict) or data.get("session") == self.sid[:8]

    def _held_now(self):
        with self.injector._lock:
            return sorted(self.injector.held), sorted(self.injector.buttons)

    def _hand_to(self, heir):
        """A newer session drives this desktop now: it takes over what's still down here (its
        marker lists it, its ending lets go of it) and this retry sends nothing more. False
        when the heir finished first (_done is set before _finish reads what's held)."""
        with heir.injector._lock:
            if heir._done:
                return False
            with self.injector._lock:
                heir.injector.held |= self.injector.held
                heir.injector.buttons |= self.injector.buttons
                self.injector.held.clear()
                self.injector.buttons.clear()
            heir._mark(sorted(heir.injector.held), sorted(heir.injector.buttons))
        return True

    def _release_later(self, last):
        try:
            self._retry_release(last)
        finally:
            if _LATE.get(self.sid) is self:
                _LATE.pop(self.sid, None)

    def _retry_release(self, last):
        """The session ended with something still down (the desktop refused the up: locked
        screen, secure desktop). Keep sending only those ups, backing off, until they go
        through; the marker keeps the list meanwhile, so recover() pays it if this process
        dies first. A newer session here takes the list over (and this stops sending at
        once); the control stop file or LATE_RELEASE_FOR ends the retry, marker kept."""
        began = time.monotonic()
        delay, due, outcome = 0.5, began + 0.5, "free"
        while True:
            time.sleep(0.1)
            if not any(self._held_now()):  # let go elsewhere (end_all / Hub close retried at once)
                break
            heir = next((s for s in list(_ACTIVE.values()) if s is not self and not s._done), None)
            if heir is not None:
                if self._hand_to(heir):
                    outcome = "handed over"
                    break
                continue
            if self._stopped_here() or time.monotonic() - began > self.LATE_RELEASE_FOR:
                outcome = "gave up"  # the marker stays: recover() at the next start lets go
                break
            if time.monotonic() < due:
                continue
            try:
                self.injector.release(backend=False)  # only this session's own ups
            except Exception:
                pass
            delay = min(delay * 2, 30.0)
            due = time.monotonic() + delay
            now = self._held_now()
            if not any(now):
                break
            if now != last and self._marker_ours():
                self._write_marker(*now)
                last = now
        if outcome == "free" and self._marker_ours():
            try:
                (self.state / MARKER).unlink(missing_ok=True)
            except OSError:
                pass
        _log(self.state, {"action": "remote_late_release", "session": self.sid[:8], "held": list(last[0]),
                          "buttons": list(last[1]), "outcome": outcome})

    def _adopt_leftovers(self):
        """What an ended session here left down (its retry gave up) or a dead process did
        (recover() couldn't let go), from the marker: this session takes it over, so its
        own marker keeps the list and its ending lets go of it."""
        try:
            data = json.loads((self.state / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("session") == self.sid[:8] or any(
                s is not self and not s._done and s.sid[:8] == data.get("session")
                for s in list(_ACTIVE.values())):
            return  # ours, or a session still finishing: its own retry hands over
        held = [c for c in data.get("held") or () if isinstance(c, str) and c in KEYS]
        buttons = [b for b in data.get("buttons") or () if b in BUTTONS]
        with self.injector._lock:
            self.injector.held.update(held)
            self.injector.buttons.update(buttons)

    def start(self):
        self._adopt_leftovers()
        self._mark(*self._held_now())
        self.injector.on_change = self._mark
        with _LAN_LOCK:  # a session that ended while run() replaced it left its restart to us
            if _LAN["owed"] and self.lan_mouse:
                _LAN["owed"] = False
                self._lan_mouse_was_running = True
        if self.lan_mouse and os.environ.get("JARVIS_NO_MODELS") != "1":  # tests never touch the real Lan Mouse
            self._suspend_lan_mouse()
        self._mark(*self._held_now())  # again: now it records whether Lan Mouse is ours to bring back
        loops = ((self._frames, "remote-frames"), (self._input, "remote-input"), (self._watchdog, "remote-deadman"))
        self._live = len(loops)
        for target, name in loops:
            thread = threading.Thread(target=self._guard(target), name=name, daemon=True)
            self.threads.append(thread)
            thread.start()
        _log(self.state, {"action": "remote_start", "session": self.sid[:8], "quality": self.quality})
        if self.announce:
            notify_here("Your phone is viewing and controlling this screen. "
                        "Run jarvis control stop here to end it.")
        return self

    def _guard(self, loop):
        def run():
            try:
                loop()
            except Exception as exc:  # any crash ends the session; never leaves a key down
                self.end(f"{type(exc).__name__}: {exc}"[:200])
            finally:
                self.end(self.why or "The session ended.")
                with self._count:
                    self._live -= 1
                    last = self._live == 0
                if last:
                    self._finish()
        return run

    def _finish(self):
        self._done = True
        if _ACTIVE.get(self.sid) is self:  # a redelivered start may have registered a newer one
            _ACTIVE.pop(self.sid, None)
        stuck = self._held_now()
        if any(stuck):  # a refused up: the marker keeps what's down until it comes up
            if self._marker_ours():
                self._write_marker(*stuck)
            self._restore_lan_mouse()
            if self._marker_ours():
                self._write_marker(*stuck)  # now lan_mouse:false, the restore has run
            self.release_thread = threading.Thread(target=self._release_later, args=(stuck,),
                                                   name="remote-late-release", daemon=True)
            _LATE[self.sid] = self  # still owes ups: end_all / Hub close must find it
            self.release_thread.start()
        else:
            self._drop_marker()
            self._restore_lan_mouse()
        newer = _ACTIVE.get(self.sid)
        superseded = newer is not None and newer is not self and not newer._done
        # tell the console why at once, not after NODE_IDLE of silence; but never for an id a
        # redelivered start now drives: that end would kill the live session on the hub
        if not self.hub_ended and not superseded:
            try:
                self.client.request("/v1/remote/frame", {"session": self.sid, "end": self.why}, timeout=5)
            except Exception:
                pass
        _log(self.state, {"action": "remote_end", "session": self.sid[:8], "why": self.why})
        if self.announce:
            notify_here("Your phone stopped controlling this screen.")

    def _drop_marker(self):
        if _ACTIVE.get(self.sid, self) is not self:
            return  # the marker now belongs to the newer session with this id
        try:  # only our own marker: a newer session's must stay (it keeps sharing paused)
            marker = self.state / MARKER
            data = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("session") == self.sid[:8]:
                marker.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:  # unreadable: ours unless a newer session is live
                if not any(not s._done for s in list(_ACTIVE.values())):
                    (self.state / MARKER).unlink(missing_ok=True)
            except OSError:
                pass

    def _post(self, path, body, timeout, on_fail=None):
        """client.request, tolerating a short link loss; None when the link stayed down."""
        failed_since = None
        while not self.stop_event.is_set():
            try:
                reply = self.client.request(path, body, timeout=timeout)
            except (OSError, ValueError, RuntimeError) as exc:
                from .cluster import HubRefused
                if on_fail:
                    on_fail()
                if isinstance(exc, HubRefused) and exc.status in (400, 403, 404):
                    self.end(f"The main refused the session: {exc}")
                    return None
                failed_since = failed_since or time.monotonic()
                if time.monotonic() - failed_since > self.LINK_LOSS:
                    self.end(f"Lost the link to the main: {exc}")
                    return None
                self.stop_event.wait(1)
                continue
            if self.key and not self._genuine(path, reply):
                self.end("A reply didn't come from the main that opened the session; ended for safety.")
                _log(self.state, {"action": "remote_bad_reply", "session": self.sid[:8], "path": path})
                return None
            return reply
        return None

    def _genuine(self, path, reply):
        if not isinstance(reply, dict) or not isinstance(reply.get("mac"), str):
            return False
        kind = "input" if path.endswith("/input") else "frame"
        return hmac.compare_digest(reply["mac"], reply_mac(self.key, kind, self.sid, reply))

    def _frames(self):
        _, _, fps = QUALITY[self.quality]
        last_digest, last_sent = None, 0.0
        capture_failures = 0
        while not self.stop_event.is_set():
            began = time.monotonic()
            if self._stopped_here():
                self.end("Control was stopped on this computer.")
                break
            try:
                image, area = self.capture()
                capture_failures = 0
            except (OSError, ValueError) as exc:
                capture_failures += 1
                if capture_failures >= 20:
                    message = f"Can't capture the screen: {exc}"
                    try:
                        from . import platforms
                        if platforms.detect()["os"] == "windows":
                            message = desktop_problem() or message
                    except Exception:
                        pass
                    self.end(message)
                    break
                self.stop_event.wait(0.5)
                continue
            self.injector.area = area
            reduced = image.reduce(8) if image.width >= 800 else image
            digest = hashlib.blake2b(reduced.tobytes(), digest_size=16).digest()
            body = {"session": self.sid}
            if digest != last_digest:
                jpeg, w, h = self.encoder(image, self.quality)
                body.update(jpeg=base64.b64encode(jpeg).decode(), width=int(area["w"]), height=int(area["h"]))
            elif began - last_sent < KEEPALIVE:
                self.stop_event.wait(max(0.05, 1 / fps - (time.monotonic() - began)))
                continue
            reply = self._post("/v1/remote/frame", body, timeout=20)
            if reply and reply.get("stop"):
                self.hub_ended = True
            if reply is None or reply.get("stop"):
                self.end((reply or {}).get("why") or self.why or "The main ended the session.")
                break
            last_digest, last_sent = digest, time.monotonic()
            self.stop_event.wait(max(0.0, 1 / fps - (time.monotonic() - began)))

    def _let_go(self):
        try:
            self.injector.release()
        except Stopped:
            pass

    def _input(self):
        pressed, fails = 0, 0  # the last event number pressed here; presses failing in a row
        while not self.stop_event.is_set():
            reply = self._post("/v1/remote/input", {"session": self.sid, "after": pressed},
                               timeout=INPUT_HOLD + 8, on_fail=self._let_go)
            if reply is None:
                break
            self.last_reply = time.monotonic()
            for item in reply.get("events") or []:
                if self.stop_event.is_set():
                    break
                try:
                    number, event = item
                    if not isinstance(number, int) or number <= pressed:
                        continue
                    pressed = number  # counted even if it fails: never pressed twice
                    self.injector.apply(clean_event(event))
                    fails = 0
                except Stopped as exc:
                    self.end(str(exc))
                    break
                except (ValueError, KeyError, TypeError):
                    continue  # one bad event never ends David's session
                except Exception as exc:  # ydotoold down, a locked screen...: let go, go on
                    fails += 1
                    self._let_go()
                    _log(self.state, {"action": "remote_input_error", "error": str(exc)[:200]})
                    if fails >= self.MAX_FAILS:
                        self.end(failure_message(event, exc))
                        break
            if reply.get("stop"):
                self.hub_ended = True
                self.end(reply.get("why") or "The main ended the session.")
                break

    def _watchdog(self):
        """Dead-man: something held down and no word from the main for DEADMAN seconds
        (the link froze mid-press): let go of it."""
        touched = time.monotonic()
        while not self.stop_event.wait(0.5):
            if self.injector.busy() and time.monotonic() - self.last_reply > DEADMAN:
                self._let_go()
            if time.monotonic() - touched >= MARKER_TOUCH:  # keeps sharing paused while we run
                touched = time.monotonic()
                try:
                    os.utime(self.state / MARKER)
                except OSError:
                    pass


def _lan_mouse_back(state):
    """Start the node's Lan Mouse again after remote control, unless the laptops read apart
    (the guard's memo): then leave it off and mark it the guard's to bring back once they're
    together, as ControllerPause does on the main. Never raises."""
    try:
        from . import sharedinput
        avail = sharedinput.sharing_availability(state)
        # Settled or fresh apart: leave it off and let the guard bring it back once together.
        # Unknown is not apart, so a session end still restores Lan Mouse.
        if avail.get("settled") == "apart" or avail.get("state") == "apart":
            sharedinput.update_guard_memo(state, stopped_by_guard=True, override=False, stopped_at=time.time())
        else:
            sharedinput.start()
    except Exception as exc:
        _log(state, {"action": "remote_lan_mouse_error", "error": str(exc)[:200]})


_ACTIVE = {}  # session id -> NodeSession, on the node
_LATE = {}    # session id -> ended NodeSession whose refused ups are still being retried
_LAN_LOCK = threading.RLock()  # Lan Mouse stop/start around sessions, and its handoff
_LAN = {"owed": False, "starting": 0}  # owed: stopped by a session that ended mid-replace


def end_all(why="JARVIS on this computer is restarting.", wait=3):
    """End every session here and let go of everything (before a restart), including ended
    sessions whose refused ups are still being retried: end() tries those ups again now,
    and join() gives the retry `wait` seconds, so they aren't lost with the process."""
    sessions = list(_ACTIVE.values()) + [s for s in list(_LATE.values()) if s not in _ACTIVE.values()]
    for session in sessions:
        session.end(why)
    for session in sessions:
        session.join(wait)
    # A session still finishing (an input poll stuck mid-request) may never reach _finish
    # before the process exits: pay its Lan Mouse restart now. Inside run() (a replace) the
    # new session takes it over instead; _finish then finds nothing owed.
    with _LAN_LOCK:
        owed = False
        for session in sessions:
            if not session._done and session._lan_mouse_was_running:
                session._lan_mouse_was_running = False
                owed = True
        if owed:
            if _LAN["starting"]:
                _LAN["owed"] = True
            else:
                _lan_mouse_back(sessions[0].state)


def recover(state, injector=None):
    """At start: a session marker left behind means the last process died mid-session
    (killed, crashed, restarted for an update). Let go of anything it may have held."""
    marker = Path(state) / MARKER
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    # What the dead session still held, as it wrote it before each press: a fresh Injector
    # holds nothing, so without this only the backend's mouse buttons and modifiers came up.
    held_raw, buttons_raw = data.get("held"), data.get("buttons")
    held = [c for c in (held_raw if isinstance(held_raw, list) else ()) if isinstance(c, str) and c in KEYS]
    buttons = [b for b in (buttons_raw if isinstance(buttons_raw, list) else ()) if b in BUTTONS]
    stuck = True
    try:
        injector = injector or Injector()
        injector.held.update(held)
        injector.buttons.update(buttons)
        injector.release(close=True)
        stuck = injector.busy()
    except Exception:
        pass
    _log(state, {"action": "remote_recover", "held": held, "buttons": buttons, "stuck": stuck,
                 "note": "released input after an unfinished session"})
    if stuck and (held or buttons):
        return True  # couldn't let go yet (no desktop session?): keep the marker, retry next start
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    # The dead session had stopped Lan Mouse and never got to start it again.
    if data.get("lan_mouse") is True and os.environ.get("JARVIS_NO_MODELS") != "1":
        try:
            from . import sharedinput
            if not sharedinput.lan_mouse_status().get("running"):
                _lan_mouse_back(state)
        except Exception as exc:
            _log(state, {"action": "remote_lan_mouse_error", "error": str(exc)[:200]})
    return True


def run(arg, config, state, **kw):
    """The node's `remote` job: {"op": "start"|"stop", "session", "quality", "key", "by"?}.
    by "phone": David's phone opened it, so this computer shows a notification. The hub also
    runs it in-process for the main itself (kw: client=LocalClient, lan_mouse=False)."""
    if not isinstance(arg, dict) or arg.get("op") not in ("start", "stop"):
        raise ValueError("Remote job needs op start or stop.")
    sid = arg.get("session")
    if not isinstance(sid, str) or not SESSION_RE.fullmatch(sid):
        raise ValueError("Remote job needs a session id.")
    if arg["op"] == "stop":
        session = _ACTIVE.get(sid)
        if session:
            session.end("Disconnected.")
        return {"ok": True, "text": "Stopped."}
    quality = arg.get("quality", "medium")
    if quality not in QUALITY:
        raise ValueError("Quality must be low, medium or high.")
    from . import platforms
    if platforms.detect()["os"] == "windows":
        problem = desktop_problem()
        if problem:
            return {"ok": False, "text": problem}
    from .control import _stopped
    if _stopped(state):
        return {"ok": False, "text": "Desktop control is stopped on this computer (jarvis control resume there)."}
    kw.setdefault("announce", arg.get("by") == "phone")
    with _LAN_LOCK:
        _LAN["starting"] += 1  # an old session ending now leaves Lan Mouse off for this one
    try:
        end_all("Replaced by a new session.")  # one at a time, and the old one has let go first
        try:
            session = NodeSession(config, state, sid, quality, key=arg.get("key"), **kw)
        except (LookupError, ImportError, OSError) as exc:
            return {"ok": False, "text": f"Remote control can't run on this computer: {exc}"}
        _ACTIVE[sid] = session
        with _LAN_LOCK:
            # An old session still finishing hands its restart over when it ends; one that
            # already ended left it in _LAN["owed"], which start() picks up.
            for old in list(_ACTIVE.values()):
                if old is not session and old._lan_mouse_was_running and session.lan_mouse:
                    old._lan_mouse_was_running = False
                    session._lan_mouse_was_running = True
        session.start()
        return {"ok": True, "text": "Remote session started."}
    finally:
        with _LAN_LOCK:
            _LAN["starting"] -= 1
            if _LAN["owed"] and not any(not s._done and getattr(s, "lan_mouse", True)
                                        for s in list(_ACTIVE.values())):
                _LAN["owed"] = False  # nothing took it over (the start failed): restore now
                _lan_mouse_back(state)


def validate(payload):
    if not isinstance(payload, dict) or payload.get("op") not in ("start", "stop") or \
            not isinstance(payload.get("session"), str) or not SESSION_RE.fullmatch(payload["session"]):
        raise ValueError("Invalid remote session job.")
    if payload.get("quality", "medium") not in QUALITY:
        raise ValueError("Quality must be low, medium or high.")
    key = payload.get("key")
    if key is not None and (not isinstance(key, str) or not SESSION_RE.fullmatch(key)):
        raise ValueError("Invalid remote session job.")
    if payload.get("by", "console") not in ("console", "phone"):
        raise ValueError("Invalid remote session job.")
    if set(payload) - {"op", "session", "quality", "key", "by"}:
        raise ValueError("Invalid remote session job.")
