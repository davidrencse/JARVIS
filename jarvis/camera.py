"""Camera: a live view of a computer's webcam for David's phone (or console), and nothing else.

It rides the remote-control path (remote_view.py); nothing new listens anywhere:
1. The hub registers a session of kind "camera" and sends the computer one signed `camera`
   job: {"op": "start", "session", "quality", "key"} (the main itself runs it in-process).
2. run() starts a CameraSession: ffmpeg kept open, writing back-to-back JPEGs to its stdout.
   A reader thread cuts them apart (Splitter) and keeps only the newest; a sender thread
   posts that one to /v1/remote/frame at the preset's rate, checking every reply's MAC.

Safety:
- Consent lives on the computer itself, in .jarvis/camera.json {"allow": true}, set only by
  `jarvis camera allow on` there. It is read from disk at every start (and while streaming),
  so whatever the main asks, a computer that never allowed it refuses.
- Only the main's console or phone server opens a session: the hub refuses `camera` jobs on
  /v1/action and /v1/control for every caller, and the brain has no tool for it.
- This computer shows a notification when the camera turns on and off.
- A session ends when the viewer stops watching, `jarvis control stop` here, consent is
  withdrawn, the link is lost, ffmpeg exits, or after MAX_SESSION. Every end path stops
  ffmpeg (terminate, then kill), so the camera is released. Infrared cameras are skipped.
- Every start and end is logged in .jarvis/logs/control.jsonl.
"""

import base64
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .remote_view import CAMERA_MAX, MAX_FRAME, SESSION_RE, notify_here, reply_mac

PRESETS = {  # (width, height, frames per second, ffmpeg -q:v: lower is better)
    "low": (640, 480, 5, 8),
    "medium": (960, 540, 8, 6),
    "high": (1280, 720, 10, 5),
}
CAPTURE = {"low": "640x480", "medium": "1280x720", "high": "1280x720"}  # asked of a V4L2 camera
MAX_SESSION = CAMERA_MAX  # 15 min
KEEPALIVE = 2.0    # s between posts when no new frame came (the hub ends a silent session)
START_WAIT = 5.0   # s run() waits for the first frame, so a busy camera fails the start itself
TERM_WAIT = 2.0    # s ffmpeg gets after terminate, and again after kill
WATCH_EVERY = 0.25  # s between the watchdog's looks at stop, consent, the time cap and the link
SETTINGS = "camera.json"
MARKER = "camera-session.json"  # while a session streams here; `jarvis camera status` reads it
MARKER_TOUCH = 10.0
MARKER_TTL = 30.0
IR_RE = re.compile(r"\b(ir|infrared)\b", re.IGNORECASE)
DEVICE_RE = re.compile(r"/dev/[A-Za-z0-9._:@+/-]+")
SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_LIST_CACHE = {"at": 0.0, "found": None}  # Windows device listing (it runs ffmpeg), 60 s


class CameraError(RuntimeError):
    """The camera couldn't start; str() says why in plain words."""


def _os_name():
    from . import platforms
    if platforms.on_windows():
        return "windows"
    return "macos" if platforms.on_macos() else "linux"


def _log(state, entry):
    try:
        path = Path(state) / "logs" / "control.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "camera": True, **entry}) + "\n")
    except OSError:
        pass


# -- consent: a file on this computer, never jarvis.config.json (that one is shared in git) ---
def _settings(state):
    try:
        data = json.loads((Path(state) / SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def allowed_here(state):
    """David allowed JARVIS to use this computer's camera (`jarvis camera allow on`, here)."""
    return _settings(state).get("allow") is True


def set_allowed(state, on):
    """Turn consent on or off on this computer. Off also ends a session in this process; one in
    the node agent ends within a second, when its watchdog reads the file again."""
    from .fsio import atomic_write
    from .platforms import private_dir
    private_dir(state)
    data = _settings(state)
    data["allow"] = bool(on)
    atomic_write(Path(state) / SETTINGS, json.dumps(data, indent=2), mode=0o600)
    if not on:
        end_all("The camera was turned off for JARVIS on this computer.")
    return {"allowed": bool(on)}


# -- which camera ----------------------------------------------------------------------------
def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _udev_caps(root, node):
    """udev's ID_V4L_CAPABILITIES for a video4linux node (":capture:"...), None if unknown."""
    dev = _read(node / "dev")
    if not re.fullmatch(r"\d+:\d+", dev):
        return None
    for line in _read(Path(root) / "run" / "udev" / "data" / f"c{dev}").splitlines():
        if line.startswith("E:ID_V4L_CAPABILITIES="):
            return line.split("=", 1)[1]
    return None


def linux_devices(root="/"):
    """RGB cameras on this Linux computer, best first: [{"path", "node", "name"}]. One USB
    webcam shows up as several nodes (picture, metadata, infrared): keep the capture node of
    each interface (index 0) whose name isn't infrared. by-path links are stable across
    replugging; by-id ones aren't usable (both interfaces share a serial, so the RGB and IR
    links collide). root is "/" except in tests."""
    root = Path(root)
    found = {}
    for node in (root / "sys" / "class" / "video4linux").glob("video*"):
        name = _read(node / "name")
        if not name or IR_RE.search(name) or _read(node / "index") not in ("", "0"):
            continue
        caps = _udev_caps(root, node)
        if caps is not None and ":capture:" not in caps:
            continue
        found[node.name] = name
    stable = {}
    for link in sorted((root / "dev" / "v4l" / "by-path").glob("*-video-index0")):
        try:
            target = Path(os.readlink(link)).name
        except OSError:
            continue
        if target in found and target not in stable:
            stable[target] = f"/dev/v4l/by-path/{link.name}"
    order = sorted(found, key=lambda n: (int(n[5:]) if n[5:].isdigit() else 1 << 20, n))
    return [{"path": stable.get(n, f"/dev/{n}"), "node": n, "name": found[n]} for n in order]


def _parse_dshow(text):
    """Video device names from `ffmpeg -f dshow -list_devices true -i dummy` (old and new
    layouts), infrared ones and names dshow can't take (a ':' splits video from audio) skipped."""
    names, section = [], None
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        if "Alternative name" in line:
            continue
        match = re.search(r'"([^"]+)"\s*(?:\(([^)]*)\))?\s*$', line)
        if not match:
            continue
        name, kinds = match.group(1), match.group(2)
        video = "video" in kinds if kinds is not None else section == "video"
        if video and ":" not in name and not IR_RE.search(name) and name not in names:
            names.append(name)
    return names


def windows_devices(exe, run=None):
    """Cameras DirectShow lists on Windows: [{"path": name, "node": name, "name": name}]."""
    now = time.monotonic()
    if run is None and _LIST_CACHE["found"] is not None and now - _LIST_CACHE["at"] < 60:
        return _LIST_CACHE["found"]
    from .platforms import NO_WINDOW
    try:
        done = (run or subprocess.run)([exe, "-hide_banner", "-f", "dshow", "-list_devices", "true",
                                        "-i", "dummy"], capture_output=True, text=True, errors="replace",
                                       timeout=10, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    found = [{"path": n, "node": n, "name": n} for n in _parse_dshow((done.stderr or "") + (done.stdout or ""))]
    if run is None:
        _LIST_CACHE.update(at=now, found=found)
    return found


def find_devices(os_name=None, root="/", exe=None, run=None):
    os_name = os_name or _os_name()
    if os_name == "windows":
        return windows_devices(exe, run) if exe else []
    if os_name == "linux":
        return linux_devices(root)
    return []


def pick_device(state, os_name=None, root="/", exe=None, run=None):
    """(device, None) for the camera to open, or (None, why). camera.json's "device" overrides
    the pick: a /dev path on Linux, a DirectShow name on Windows."""
    os_name = os_name or _os_name()
    if os_name not in ("linux", "windows"):
        return None, "The camera view isn't built for this kind of computer yet."
    chosen = _settings(state).get("device")
    if chosen is not None:
        if os_name == "windows":
            chosen = chosen[6:] if isinstance(chosen, str) and chosen.startswith("video=") else chosen
            good = isinstance(chosen, str) and 0 < len(chosen) <= 200 and not any(
                c in '":' or ord(c) < 32 for c in chosen)
        else:
            good = isinstance(chosen, str) and DEVICE_RE.fullmatch(chosen) and ".." not in chosen
        if not good:
            return None, f"The camera device set in .jarvis/{SETTINGS} isn't usable; remove \"device\" to pick one."
        return chosen, None
    if os_name == "windows" and not exe:
        return None, "ffmpeg isn't installed on this computer, so JARVIS can't list its cameras."
    found = find_devices(os_name, root, exe, run)
    if not found:
        return None, "No camera found on this computer (infrared cameras are skipped)."
    return found[0]["path"], None


def busy(state):
    """A camera session streams on this computer now (this process, or the node agent's)."""
    if any(not s.done.is_set() for s in list(_ACTIVE.values())):
        return True
    try:
        return time.time() - (Path(state) / MARKER).stat().st_mtime < MARKER_TTL
    except OSError:
        return False


def why_not(config, state, os_name=None, root="/", which=None, run=None):
    """(device, ffmpeg, None) when a camera session could start here now, else (.., .., why)."""
    from .control import _stopped
    exe = (which or shutil.which)("ffmpeg")
    if not allowed_here(state):
        return None, exe, "The camera is off for JARVIS on this computer. To allow it, run there: jarvis camera allow on"
    if _stopped(state):
        return None, exe, "Desktop control is stopped on this computer (jarvis control resume there)."
    if not exe:
        return None, None, "ffmpeg isn't installed on this computer, so JARVIS can't open the camera."
    device, why = pick_device(state, os_name, root, exe, run)
    return device, exe, why


def ready(config, state, **kw):
    """(ok, why) for the hub's device list (the main's own camera)."""
    _, _, why = why_not(config, state, **kw)
    return why is None, why


def status(config, state, os_name=None, root="/", which=None, run=None):
    """What `jarvis camera status` shows: consent, the camera it would open, ffmpeg, busy."""
    exe = (which or shutil.which)("ffmpeg")
    device, problem = pick_device(state, os_name, root, exe, run)
    _, _, why = why_not(config, state, os_name, root, which, run)
    return {"allowed": allowed_here(state), "device": device, "ffmpeg": exe, "busy": busy(state),
            "why": why or problem}


# -- the stream: JPEGs cut from ffmpeg's stdout ---------------------------------------------
def _frame_end(buf, start):
    """Index just past the EOI of the JPEG whose SOI is at buf[start], or None when the buffer
    doesn't hold all of it yet. Walks the marker segments, so an FF D9 inside a header (a
    thumbnail, a comment) never cuts a frame short; inside scan data an FF is always stuffed
    (FF 00) or a restart marker, so the first other marker ends the scan. ValueError: garbage."""
    i, n = start + 2, len(buf)
    while True:
        if i + 1 >= n:
            return None
        if buf[i] != 0xFF:
            raise ValueError("not a JPEG marker")
        marker = buf[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xD9:
            return i + 2
        if marker == 0xD8:
            raise ValueError("a new frame began inside this one")
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 3 >= n:
            return None
        length = (buf[i + 2] << 8) | buf[i + 3]
        if length < 2:
            raise ValueError("bad JPEG segment length")
        i += 2 + length
        if marker != 0xDA:
            continue
        while True:  # scan data after SOS: up to the next real marker
            i = buf.find(b"\xff", i)
            if i < 0 or i + 1 >= n:
                return None
            following = buf[i + 1]
            if following == 0x00 or 0xD0 <= following <= 0xD7:
                i += 2
            elif following == 0xFF:
                i += 1
            else:
                break


class Splitter:
    """Cuts a stream of back-to-back JPEGs (ffmpeg -f mjpeg) into whole frames. Bytes outside
    a frame are skipped; a frame that grows past `limit` is dropped whole."""

    def __init__(self, limit=MAX_FRAME):
        self.buf, self.limit = bytearray(), limit

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(b"\xff\xd8")
            if start < 0:
                del self.buf[:-1 if self.buf[-1:] == b"\xff" else len(self.buf)]
                return frames
            del self.buf[:start]
            try:
                end = _frame_end(self.buf, 0)
            except ValueError:
                del self.buf[:2]  # resync at the next SOI
                continue
            if end is None:
                if len(self.buf) > self.limit:
                    del self.buf[:2]
                    continue
                return frames
            frame = bytes(self.buf[:end])
            del self.buf[:end]
            if len(frame) <= self.limit:
                frames.append(frame)


def jpeg_size(data):
    """(width, height) from a JPEG's frame header, or None when it has none."""
    if data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xD8, 0xD9, 0xDA):
            return None
        if marker in SOF and i + 9 <= n:
            height, width = (data[i + 5] << 8) | data[i + 6], (data[i + 7] << 8) | data[i + 8]
            return (width, height) if width and height else None
        length = (data[i + 2] << 8) | data[i + 3]
        if length < 2:
            return None
        i += 2 + length
    return None


def command(exe, device, quality, os_name):
    """ffmpeg's argv: the camera in, the preset's size and rate, MJPEG frames on stdout."""
    width, height, fps, q = PRESETS[quality]
    if os_name == "windows":
        source = ["-f", "dshow", "-i", f"video={device}"]  # one argv item: no shell, no quoting
    else:
        source = ["-f", "v4l2", "-video_size", CAPTURE[quality], "-i", device]
    scale = f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    return [exe, "-hide_banner", "-loglevel", "error", "-nostdin", *source, "-an", "-vf", scale,
            "-f", "mjpeg", "-q:v", str(q), "-"]


def explain(lines, os_name):
    """ffmpeg's last words, in plain English."""
    text = " ".join(lines)
    low = text.lower()
    if "device or resource busy" in low:
        return "The camera is in use by another app. Close it there and try again."
    if os_name == "windows" and any(s in low for s in ("could not run graph", "access is denied",
                                                       "0x80070005", "i/o error")):
        return ("Windows wouldn't open the camera: another app is using it, or camera access is off "
                "in Settings > Privacy & security > Camera (let desktop apps access your camera).")
    if "permission denied" in low:
        return "This account isn't allowed to open the camera (permission denied on the device)."
    if "no such file or directory" in low or "could not find video device" in low:
        return "The camera isn't there any more (unplugged?)."
    last = next((line for line in reversed(lines) if line.strip()), "")
    return f"The camera stopped: {last[:200]}" if last else "The camera stopped (ffmpeg exited)."


class CameraSession:
    """One camera stream on this computer. end() stops ffmpeg at once, from any thread."""

    LINK_LOSS = 15  # s of failed posts to the main before the session ends

    def __init__(self, config, state, sid, quality, client=None, key=None, device=None, exe="ffmpeg",
                 os_name=None, popen=None, notify=None, clock=time.monotonic, start_wait=START_WAIT):
        from .cluster import Client
        if quality not in PRESETS:
            raise ValueError("Quality must be low, medium or high.")
        self.config, self.state, self.sid, self.quality = config, Path(state), sid, quality
        self.key = key  # from the signed start job: every reply must carry its MAC
        self.client = client or Client(config, state)
        self.device, self.exe = device, exe
        self.os_name = os_name or _os_name()
        self.popen = popen or subprocess.Popen
        self.notify = notify or notify_here
        self.clock, self.start_wait = clock, start_wait
        self.stop_event, self.first, self.exited, self.done = (threading.Event() for _ in range(4))
        self.why, self.hub_ended, self.proc, self.started = None, False, None, None
        self.last_reply = time.monotonic()  # the main's last good answer (the watchdog's link check)
        self.latest = (0, None, 0, 0)  # (seq, jpeg, width, height): only the newest frame matters
        self.errors = deque(maxlen=20)
        self.threads = []
        self._frame_lock, self._end_lock, self._proc_lock = threading.Lock(), threading.Lock(), threading.Lock()
        self._announced = False

    # -- ending ---------------------------------------------------------------------------
    def end(self, why):
        """Stop now: ffmpeg is terminated (then killed) before this returns."""
        with self._end_lock:
            if not self.stop_event.is_set():
                self.why = why
                self.stop_event.set()
        self._stop_ffmpeg()

    def _stop_ffmpeg(self):
        with self._proc_lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            for stop in (proc.terminate, proc.kill):
                try:
                    stop()
                except OSError:
                    pass  # already gone
                try:
                    proc.wait(timeout=TERM_WAIT)
                    return
                except subprocess.TimeoutExpired:
                    continue
            _log(self.state, {"action": "camera_kill_failed", "session": self.sid[:8]})

    def join(self, timeout=None):
        self.done.wait(timeout)

    def _forget(self):
        if _ACTIVE.get(self.sid) is self:
            _ACTIVE.pop(self.sid, None)
        try:
            marker = self.state / MARKER
            if json.loads(marker.read_text(encoding="utf-8")).get("session") == self.sid[:8]:
                marker.unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError):
            pass

    def _finish(self):
        """After the sender stops: ffmpeg gone, the hub told why, the notification shown."""
        self._stop_ffmpeg()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(TERM_WAIT)
        self._forget()
        if not self.hub_ended:  # say why at once, not after the hub's NODE_IDLE of silence
            try:
                self.client.request("/v1/remote/frame", {"session": self.sid, "end": self.why}, timeout=5)
            except Exception:
                pass
        self._say_off()
        _log(self.state, {"action": "camera_end", "session": self.sid[:8], "why": self.why})
        self.done.set()

    def _say_off(self):
        """The "camera off" notification, once it said "on" (the watchdog and the sender may
        both get here). Never raises: it's on every end path."""
        with self._end_lock:
            announced, self._announced = self._announced, False
        if announced:
            try:
                self.notify("JARVIS turned the camera off.", "JARVIS camera")
            except Exception:
                pass

    # -- starting -------------------------------------------------------------------------
    def start(self):
        """Open the camera and wait (START_WAIT at most) for its first frame: an ffmpeg that
        quits first (camera busy, Windows privacy off) fails the start with its reason."""
        from .platforms import NO_WINDOW
        argv = command(self.exe, self.device, self.quality, self.os_name)
        try:
            self.proc = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, **({"creationflags": NO_WINDOW}
                                                              if self.os_name == "windows" else {}))
        except OSError as exc:
            self._forget()
            raise CameraError(f"Couldn't start ffmpeg: {exc}") from None
        try:
            return self._begin()
        except BaseException:  # whatever failed, ffmpeg never outlives a start that didn't happen
            self.end(self.why or "The camera didn't start.")
            for thread in self.threads:
                if thread.ident is not None and thread is not threading.current_thread():
                    thread.join(TERM_WAIT)
            self._forget()
            self._say_off()
            self.done.set()
            raise

    def _begin(self):
        self.started = self.clock()
        self.threads += [threading.Thread(target=target, name=name, daemon=True)
                         for target, name in ((self._drain, "camera-errors"), (self._read, "camera-read"))]
        for thread in self.threads:  # the drain first: the reader joins it to explain an exit
            thread.start()
        deadline = time.monotonic() + self.start_wait
        while not self.first.is_set() and not self.exited.is_set() and time.monotonic() < deadline:
            self.first.wait(0.05)
        if not self.first.is_set() and (self.exited.is_set() or self.stop_event.is_set()):
            raise CameraError(self.why or "The camera didn't start.")
        self._touch_marker(force=True)
        self._announced = True
        self.notify("JARVIS turned the camera on for a remote view. Run jarvis control stop here to end it.",
                    "JARVIS camera")
        _log(self.state, {"action": "camera_start", "session": self.sid[:8], "quality": self.quality,
                          "device": str(self.device)[:200]})
        self.last_reply = time.monotonic()
        for target, name in ((self._sender, "camera-send"), (self._watch, "camera-watch")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()
        return self

    # -- threads --------------------------------------------------------------------------
    def _read(self):
        """ffmpeg's stdout -> the newest whole frame. EOF means ffmpeg exited."""
        splitter = Splitter()
        stream = self.proc.stdout
        read = getattr(stream, "read1", None) or stream.read
        try:
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                for frame in splitter.feed(chunk):
                    size = jpeg_size(frame)
                    if size:
                        with self._frame_lock:
                            self.latest = (self.latest[0] + 1, frame, *size)
                        self.first.set()
        except (OSError, ValueError):
            pass  # the pipe closed under us: ffmpeg is gone either way
        finally:
            try:
                if not self.stop_event.is_set():
                    for thread in self.threads:
                        if thread.name == "camera-errors" and thread.ident is not None:
                            thread.join(1)  # its last words explain the exit
                    self.end(explain(list(self.errors), self.os_name))
            finally:
                self.exited.set()

    def _drain(self):
        """ffmpeg's stderr, read all along (a full pipe would stall it); the last lines kept."""
        try:
            for line in iter(self.proc.stderr.readline, b""):
                text = line.decode("utf-8", "replace").strip() if isinstance(line, bytes) else str(line).strip()
                if text:
                    self.errors.append(text[:300])
        except (OSError, ValueError):
            pass

    def _sender(self):
        try:
            self._send_loop()
        except Exception as exc:  # any crash still stops ffmpeg and tells the hub
            self.end(f"{type(exc).__name__}: {exc}"[:200])
        finally:
            self.end(self.why or "The camera session ended.")
            self._finish()

    def _must_end(self):
        """Why this computer ends the session now (control stopped here, consent withdrawn, the
        15-minute cap), or None."""
        from .control import _stopped
        if _stopped(self.state):
            return "Control was stopped on this computer."
        if not allowed_here(self.state):
            return "The camera was turned off for JARVIS on this computer."
        if self.clock() - self.started >= MAX_SESSION:
            return "Camera sessions end after 15 minutes; start again to go on."
        return None

    def _watch(self):
        """Stop, consent, the cap and the link, checked apart from the sender: a frame post stuck
        on a stalled link (a request waits up to 20 s, and is retried for LINK_LOSS) must not keep
        the camera on after `jarvis control stop` or `jarvis camera allow off` here. end() stops
        ffmpeg at once; the stuck post finishes on its own."""
        while not self.stop_event.wait(WATCH_EVERY):
            why = self._must_end()
            if why is None and time.monotonic() - self.last_reply > self.LINK_LOSS:
                why = f"Lost the link to the main: no answer for {self.LINK_LOSS} seconds."
            if why:
                self.end(why)
                self._say_off()  # the camera is off now: say so, not once the stuck post gives up

    def _send_loop(self):
        fps = PRESETS[self.quality][2]
        sent, last_post = 0, 0.0
        while not self.stop_event.is_set():
            began = time.monotonic()
            why = self._must_end()
            if why:
                self.end(why)
                break
            with self._frame_lock:
                seq, jpeg, width, height = self.latest
            body = {"session": self.sid}
            if seq > sent:
                body.update(jpeg=base64.b64encode(jpeg).decode(), width=width, height=height)
            elif began - last_post < KEEPALIVE:
                self.stop_event.wait(1 / fps)
                continue
            reply = self._post("/v1/remote/frame", body, timeout=20)
            if reply and reply.get("stop"):
                self.hub_ended = True
            if reply is None or reply.get("stop"):
                self.end((reply or {}).get("why") or self.why or "The main ended the camera session.")
                break
            sent, last_post = seq, time.monotonic()
            self._touch_marker()
            self.stop_event.wait(max(0.0, 1 / fps - (time.monotonic() - began)))

    def _post(self, path, body, timeout):
        """client.request, tolerating a short link loss; None when the session must end."""
        from .cluster import HubRefused
        failed_since = None
        while not self.stop_event.is_set():
            try:
                reply = self.client.request(path, body, timeout=timeout)
            except (OSError, ValueError, RuntimeError) as exc:
                if isinstance(exc, HubRefused) and exc.status in (400, 403, 404):
                    self.end(f"The main refused the camera session: {exc}")
                    return None
                failed_since = failed_since or time.monotonic()
                if time.monotonic() - failed_since > self.LINK_LOSS:
                    self.end(f"Lost the link to the main: {exc}")
                    return None
                self.stop_event.wait(1)
                continue
            if self.key and not self._genuine(reply):
                self.end("A reply didn't come from the main that opened the session; ended for safety.")
                _log(self.state, {"action": "camera_bad_reply", "session": self.sid[:8]})
                return None
            self.last_reply = time.monotonic()
            return reply
        return None

    def _genuine(self, reply):
        if not isinstance(reply, dict) or not isinstance(reply.get("mac"), str):
            return False
        return hmac.compare_digest(reply["mac"], reply_mac(self.key, "frame", self.sid, reply))

    def _touch_marker(self, force=False):
        now = time.monotonic()
        if not force and now - getattr(self, "_touched", 0.0) < MARKER_TOUCH:
            return
        self._touched = now
        try:
            (self.state / MARKER).write_text(json.dumps({"session": self.sid[:8], "at": time.time()}),
                                             encoding="utf-8")
        except OSError:
            pass


_ACTIVE = {}  # session id -> CameraSession on this computer (never remote_view._ACTIVE)


def end_all(why="JARVIS on this computer is stopping.", wait=3):
    """End every camera session here; each one's ffmpeg is stopped before this returns."""
    sessions = list(_ACTIVE.values())
    for session in sessions:
        session.end(why)
    for session in sessions:
        session.join(wait)


def validate(payload):
    """A `camera` job: {"op": "start"|"stop", "session", "quality"?, "key"?} and nothing else."""
    if not isinstance(payload, dict) or payload.get("op") not in ("start", "stop") or \
            not isinstance(payload.get("session"), str) or not SESSION_RE.fullmatch(payload["session"]):
        raise ValueError("Invalid camera job.")
    if payload.get("quality", "low") not in PRESETS:
        raise ValueError("Quality must be low, medium or high.")
    key = payload.get("key")
    if key is not None and (not isinstance(key, str) or not SESSION_RE.fullmatch(key)):
        raise ValueError("Invalid camera job.")
    if set(payload) - {"op", "session", "quality", "key"}:
        raise ValueError("Invalid camera job.")


def run(arg, config, state, client=None, *, os_name=None, root="/", which=None, list_run=None, **kw):
    """The `camera` job here: start (one stream at a time on this computer) or stop.
    {"ok": False, "text"} says plainly why it can't start (no consent, no ffmpeg, busy...)."""
    validate(arg)
    sid = arg["session"]
    if arg["op"] == "stop":
        session = _ACTIVE.get(sid)
        if session:
            session.end("Disconnected.")
        return {"ok": True, "text": "Stopped."}
    os_name = os_name or _os_name()
    device, exe, why = why_not(config, state, os_name, root, which, list_run)
    if why:
        return {"ok": False, "text": why}
    end_all("Replaced by a new camera session.")
    try:
        session = CameraSession(config, state, sid, arg.get("quality", "low"), client=client,
                                key=arg.get("key"), device=device, exe=exe, os_name=os_name, **kw)
    except (ValueError, OSError) as exc:
        return {"ok": False, "text": f"The camera can't run on this computer: {exc}"}
    _ACTIVE[sid] = session
    try:
        session.start()
    except CameraError as exc:
        return {"ok": False, "text": str(exc)}
    return {"ok": True, "text": "Camera on."}
