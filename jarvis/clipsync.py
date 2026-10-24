"""Unified clipboard: copy on one laptop, paste on the other. Text and images, both ways.

It rides the SSH link JARVIS already trusts (same key, BatchMode, strict host-key
checking as `jarvis send`), so there is no new port, pairing code or crypto. The Dell
runs `jarvis clipboard start`; that opens one SSH session to the Zenbook and sends this
very file as the remote half, which watches the Wayland clipboard with wl-paste. Both
halves speak one JSON line per clipboard change.

This module is deliberately stdlib-only with no package imports at the top level,
because the remote half runs it as a bare script.

Copies a password manager marks as secret (KeePassXC/KDE hint on Linux, Windows'
ExcludeClipboardContentFromMonitorProcessing) never leave the laptop.
"""

import base64
import glob
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib

MAX_BYTES = 32 * 1024 * 1024
TEXT = "text/plain;charset=utf-8"
# Executed by the remote shell: read this module's source from stdin, run its serve().
BOOTSTRAP = ("python3 -c 'import sys;r=sys.stdin.buffer;g={\"__name__\":\"jarvis_clip\"};"
             "exec(r.read(int(r.readline())),g);g[\"serve\"]()'")


def digest(kind, data):
    return hashlib.sha256(kind.encode() + b"\0" + data).hexdigest()


# ---- PNG <-> Windows DIB (pure Python, for the image formats screenshots use) -------------
def _png_chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_encode(width, height, rows, alpha):
    """rows: top-down bytes, RGB or RGBA per pixel."""
    raw = b"".join(b"\0" + row for row in rows)
    head = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", head)
            + _png_chunk(b"IDAT", zlib.compress(raw, 6)) + _png_chunk(b"IEND", b""))


def png_decode(data):
    """(width, height, top-down RGBA rows) for 8-bit RGB/RGBA/grey, non-interlaced PNGs."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, head = 8, [], None
    while pos < len(data):
        size, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        pos += 12 + size
        if tag == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
    width, height, depth, color, _, _, interlace = head
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if depth != 8 or interlace or not channels:
        raise ValueError("unsupported PNG layout")
    raw, stride, bpp = zlib.decompress(b"".join(idat)), width * channels, channels
    rows, prev = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        if channels == 4:
            rows.append(bytes(line))
        else:
            out = bytearray()
            for x in range(width):
                px = line[x * channels:(x + 1) * channels]
                if channels == 3:
                    out += px + b"\xff"
                elif channels == 1:
                    out += bytes((px[0], px[0], px[0], 255))
                else:
                    out += bytes((px[0], px[0], px[0], px[1]))
            rows.append(bytes(out))
    return width, height, rows


def dib_to_png(dib):
    """CF_DIB (BITMAPINFOHEADER + pixels, 24 or 32 bpp) -> PNG bytes."""
    size, width, height, _, bits, compression = struct.unpack("<IiiHHI", dib[:20])
    if bits not in (24, 32) or compression not in (0, 3):
        raise ValueError("unsupported bitmap")
    offset = size + (12 if compression == 3 and size == 40 else 0)
    stride = ((width * bits + 31) // 32) * 4
    top_down, height = height < 0, abs(height)
    rows = []
    for y in range(height):
        line = dib[offset + y * stride:offset + y * stride + width * bits // 8]
        px = bytearray()
        step = bits // 8
        for x in range(width):
            b, g, r = line[x * step:x * step + 3]
            px += bytes((r, g, b, line[x * step + 3] if bits == 32 else 255))
        rows.append(bytes(px))
    if not top_down:
        rows.reverse()
    if bits == 32 and not any(row[i] for row in rows for i in range(3, len(row), 4)):
        rows = [bytes(v if i % 4 != 3 else 255 for i, v in enumerate(row)) for row in rows]
    return png_encode(width, height, rows, alpha=True)


def png_to_dib(png):
    """PNG bytes -> CF_DIB (32 bpp, bottom-up BGRA)."""
    width, height, rows = png_decode(png)
    pixels = bytearray()
    for row in reversed(rows):
        for x in range(width):
            r, g, b, a = row[x * 4:x * 4 + 4]
            pixels += bytes((b, g, r, a))
    head = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 32, 0, len(pixels), 2835, 2835, 0, 0)
    return head + bytes(pixels)


# ---- clipboards --------------------------------------------------------------------------
class Wayland:
    """wl-clipboard on a Wayland session (Hyprland); works from an SSH login too."""

    def __init__(self):
        if not shutil.which("wl-paste") or not shutil.which("wl-copy"):
            raise RuntimeError("wl-clipboard isn't installed (sudo pacman -S wl-clipboard).")
        self.env = dict(os.environ)
        if not self.env.get("WAYLAND_DISPLAY"):  # an SSH session doesn't inherit it
            runtime = self.env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            sockets = sorted(p for p in glob.glob(os.path.join(runtime, "wayland-*"))
                             if not p.endswith(".lock"))
            if not sockets:
                raise RuntimeError("No Wayland session is running for this user.")
            self.env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])

    def _paste(self, *args):
        result = subprocess.run(["wl-paste", *args], capture_output=True, env=self.env,
                                timeout=10)
        return result.stdout if result.returncode == 0 else None

    def read(self):
        types = (self._paste("--list-types") or b"").decode(errors="replace").split()
        if "x-kde-passwordManagerHint" in types:
            if (self._paste("--type", "x-kde-passwordManagerHint") or b"").strip() == b"secret":
                return None
        if "image/png" in types:
            data = self._paste("--type", "image/png")
            return ("png", data) if data else None
        for kind in (TEXT, "UTF8_STRING", "text/plain", "TEXT", "STRING"):
            if kind in types:
                data = self._paste("--no-newline", "--type", kind)
                return ("text", data) if data else None
        return None

    def write(self, kind, data):
        subprocess.run(["wl-copy", "--type", "image/png" if kind == "png" else TEXT],
                       input=data, env=self.env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=True)

    def watch(self, changed):
        """Calls changed() on every clipboard change until the watcher dies."""
        proc = self.watcher = subprocess.Popen(["wl-paste", "--watch", "echo"],
                                               stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL, env=self.env)
        try:
            for _ in proc.stdout:
                changed()
        finally:
            proc.kill()
            proc.wait()


    def close(self):
        watcher = getattr(self, "watcher", None)
        if watcher and watcher.poll() is None:
            watcher.kill()


class Windows:
    """Win32 clipboard through ctypes: Unicode text, PNG and CF_DIB images."""
    CF_UNICODETEXT, CF_DIB = 13, 8

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ct = ctypes
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        for fn, res, args in (
                (u.OpenClipboard, w.BOOL, [w.HWND]), (u.CloseClipboard, w.BOOL, []),
                (u.EmptyClipboard, w.BOOL, []), (u.GetClipboardData, w.HANDLE, [w.UINT]),
                (u.SetClipboardData, w.HANDLE, [w.UINT, w.HANDLE]),
                (u.IsClipboardFormatAvailable, w.BOOL, [w.UINT]),
                (u.GetClipboardSequenceNumber, w.DWORD, []),
                (u.RegisterClipboardFormatW, w.UINT, [w.LPCWSTR]),
                (k.GlobalAlloc, w.HGLOBAL, [w.UINT, ctypes.c_size_t]),
                (k.GlobalLock, w.LPVOID, [w.HGLOBAL]), (k.GlobalUnlock, w.BOOL, [w.HGLOBAL]),
                (k.GlobalSize, ctypes.c_size_t, [w.HGLOBAL]),
                (k.GlobalFree, w.HGLOBAL, [w.HGLOBAL])):
            fn.restype, fn.argtypes = res, args
        self.u, self.k = u, k
        self.png = u.RegisterClipboardFormatW("PNG")
        self.secret = [u.RegisterClipboardFormatW(n) for n in (
            "ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")]

    def _open(self):
        for _ in range(20):  # another app may hold it for a moment
            if self.u.OpenClipboard(None):
                return
            time.sleep(0.05)
        raise RuntimeError("The Windows clipboard is busy.")

    def _get(self, fmt):
        handle = self.u.GetClipboardData(fmt)
        if not handle:
            return None
        ptr = self.k.GlobalLock(handle)
        try:
            return self.ct.string_at(ptr, self.k.GlobalSize(handle))
        finally:
            self.k.GlobalUnlock(handle)

    def _put(self, fmt, data):
        handle = self.k.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ptr = self.k.GlobalLock(handle)
        self.ct.memmove(ptr, data, len(data))
        self.k.GlobalUnlock(handle)
        if not self.u.SetClipboardData(fmt, handle):
            self.k.GlobalFree(handle)  # ownership passes to Windows only on success

    def read(self):
        self._open()
        try:
            if any(self.u.IsClipboardFormatAvailable(f) for f in self.secret):
                return None
            if self.u.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                raw = self._get(self.CF_UNICODETEXT) or b""
                text = raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
                return ("text", text.replace("\r\n", "\n").encode()) if text else None
            if self.png and self.u.IsClipboardFormatAvailable(self.png):
                data = self._get(self.png)
                return ("png", data) if data else None
            if self.u.IsClipboardFormatAvailable(self.CF_DIB):
                dib = self._get(self.CF_DIB)
                return ("png", dib_to_png(dib)) if dib else None
        finally:
            self.u.CloseClipboard()
        return None

    def write(self, kind, data):
        if kind == "png":
            try:
                dib = png_to_dib(data)
            except (ValueError, struct.error, zlib.error):
                dib = None  # an unusual PNG: browsers and Office still read the PNG format
        self._open()
        try:
            self.u.EmptyClipboard()
            if kind == "png":
                self._put(self.png, data)
                if dib:
                    self._put(self.CF_DIB, dib)
            else:
                text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
                self._put(self.CF_UNICODETEXT, text.replace("\n", "\r\n").encode("utf-16-le")
                          + b"\0\0")
        finally:
            self.u.CloseClipboard()

    def watch(self, changed):
        generation = self.generation = getattr(self, "generation", 0) + 1
        seen = self.u.GetClipboardSequenceNumber()
        while self.generation == generation:  # close() or a newer watch() ends this one
            time.sleep(0.25)
            now = self.u.GetClipboardSequenceNumber()
            if now != seen:
                seen = now
                changed()


    def close(self):
        self.generation = getattr(self, "generation", 0) + 1


def local_clipboard():
    return Windows() if sys.platform == "win32" else Wayland()


# ---- the sync loop -----------------------------------------------------------------------
class Sync:
    """Mirror one clipboard to a peer over a pair of byte streams (one JSON line per copy)."""

    def __init__(self, board, inbound, outbound, log=lambda _: None):
        self.board, self.inbound, self.outbound, self.log = board, inbound, outbound, log
        self.last, self.lock, self.send_lock = None, threading.Lock(), threading.Lock()
        self.sent = self.received = 0

    def _send(self, frame):
        with self.send_lock:
            self.outbound.write(json.dumps(frame).encode() + b"\n")
            self.outbound.flush()

    def changed(self):
        try:
            item = self.board.read()
        except Exception as exc:  # a busy or odd clipboard must not kill the link
            self.log(f"read failed: {exc}")
            return
        if not item or not item[1] or len(item[1]) > MAX_BYTES:
            return
        kind, data = item
        key = digest(kind, data)
        with self.lock:
            if key == self.last:  # what we just pasted in, or an unchanged re-copy
                return
            self.last = key
        try:
            self._send({"kind": kind, "data": base64.b64encode(data).decode()})
        except OSError:  # the link just dropped; the reconnect loop takes over
            return
        self.sent += 1
        self.log(f"sent {kind} ({len(data)} bytes)")

    def receive(self):
        """Apply peer copies until the stream ends."""
        for line in self.inbound:
            if len(line) > MAX_BYTES * 4 // 3 + 200:
                continue
            try:
                frame = json.loads(line)
                kind, data = frame["kind"], base64.b64decode(frame["data"], validate=True)
            except (ValueError, KeyError, TypeError):
                continue
            if kind not in ("text", "png") or not data:
                continue
            with self.lock:
                self.last = digest(kind, data)
            try:
                self.board.write(kind, data)
                self.received += 1
                self.log(f"pasted {kind} ({len(data)} bytes)")
            except Exception as exc:
                self.log(f"write failed: {exc}")

    def run(self):
        """Watch locally in the background; return when the peer hangs up."""
        try:
            current = self.board.read()
            if current:  # don't push whatever happened to be copied before connecting
                self.last = digest(*current)
        except Exception:
            pass
        threading.Thread(target=self.board.watch, args=(self.changed,), daemon=True).start()
        try:
            self.receive()
        finally:
            getattr(self.board, "close", lambda: None)()  # don't orphan wl-paste --watch


def serve():
    """The remote half: sync this machine's clipboard over stdin/stdout."""
    try:
        board = local_clipboard()
    except Exception as exc:
        sys.stdout.buffer.write(json.dumps({"error": str(exc)}).encode() + b"\n")
        sys.stdout.buffer.flush()
        return
    sys.stdout.buffer.write(b'{"ready": true}\n')
    sys.stdout.buffer.flush()
    Sync(board, sys.stdin.buffer, sys.stdout.buffer).run()


# ---- the local half: connect, reconnect, daemon ------------------------------------------
def _log_line(path):
    def log(message):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
        try:
            with open(path, "a", encoding="utf-8") as out:
                out.write(line)
        except OSError:
            pass
        print(line, end="", flush=True)
    return log


def _pinned_host(device, state):
    """ssh options that check the peer against its pinned host key, whatever its address.

    The peer's IP changes with DHCP and networks; keying known_hosts by a fixed alias
    keeps StrictHostKeyChecking on without a fresh "unknown host" after every move."""
    key = device.get("host_key")
    if not key or not state:
        return []
    alias = "jarvis-" + device.get("hostname", "peer")
    path = os.path.join(state, "known_hosts")
    with open(path, "w", encoding="utf-8") as out:
        out.write(f"{alias} {key.strip()}\n")
    return ["-o", f"HostKeyAlias={alias}", "-o", f"UserKnownHostsFile={path}"]


def _targets(device):
    """The configured target first, then any fallbacks (another network, Tailscale)."""
    seen = []
    for target in [device.get("target"), *device.get("fallback_targets", [])]:
        if target and target not in seen:
            seen.append(target)
    return seen


def connect(device, board, log, target=None, state=None):
    """One SSH session to the peer; returns when it drops."""
    from .transfer import _check_target, _ssh_args
    target = target or device.get("target")
    _check_target(target)
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("The OpenSSH client isn't installed.")
    args = [*_ssh_args(ssh, device.get("identity_file")), *_pinned_host(device, state), "-T",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            target, BOOTSTRAP]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=flags)
    try:
        source = open(__file__, "rb").read()
        proc.stdin.write(b"%d\n" % len(source) + source)
        proc.stdin.flush()
        hello = proc.stdout.readline()
        try:
            status = json.loads(hello)
        except ValueError:
            err = proc.stderr.read(2000).decode(errors="replace").strip().splitlines()
            raise RuntimeError("SSH failed: " + (err[-1] if err else "no response")) from None
        if not status.get("ready"):
            raise RuntimeError("The other laptop can't share its clipboard: "
                               + str(status.get("error")))
        log(f"linked with {target}")
        Sync(board, proc.stdout, proc.stdin, log).run()
    finally:
        proc.kill()
        proc.wait()


def run(config, state, device_name=None):
    """Keep the link up: reconnect with backoff (sleep, Wi-Fi changes, the peer rebooting)."""
    from .transfer import ssh_device
    state = os.path.abspath(state)
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    log = _log_line(os.path.join(state, "logs", "clipboard.log"))
    device, board, wait = ssh_device(config, device_name), local_clipboard(), 2
    targets, turn = _targets(device), 0
    with open(os.path.join(state, "clipboard.pid"), "w") as pid:
        pid.write(str(os.getpid()))
    while True:
        started, target = time.time(), targets[turn % len(targets)]
        try:
            connect(device, board, log, target, state)
            log("link dropped")
        except Exception as exc:
            log(f"not linked via {target}: {exc}")
            turn += 1
        if turn % len(targets) and time.time() - started < 60:
            continue  # try the next address straight away
        wait = 2 if time.time() - started > 60 else min(wait * 2, 60)
        time.sleep(wait)


PID, STARTUP_NAME, UNIT_NAME = "clipboard.pid", "JARVIS Clipboard.vbs", "jarvis-clipboard.service"


def _pid(state):
    from .daemon import _alive
    try:
        pid = int(open(os.path.join(state, PID)).read().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def status(state):
    path = os.path.join(os.path.abspath(state), "logs", "clipboard.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            recent = f.read().splitlines()[-6:]
    except OSError:
        recent = []
    linked = None
    for line in reversed(recent):
        if "linked with" in line or "not linked" in line or "link dropped" in line:
            linked = "linked with" in line
            break
    pid = _pid(state)
    return {"running": pid is not None, "pid": pid, "linked": bool(pid and linked),
            "autostart": _startup_file().is_file(), "log": path, "recent": recent}


def start(state, device_name=None):
    if _pid(state):
        return {"started": False, **status(state)}
    state = os.path.abspath(state)
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             if os.name == "nt" else 0)
    args = [sys.executable, "-m", "jarvis", "--state", state, "clipboard", "run"]
    if device_name:
        args += ["--device", device_name]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags,
                     start_new_session=os.name != "nt", close_fds=True,
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    for _ in range(40):
        time.sleep(0.25)
        if _pid(state):
            return {"started": True, **status(state)}
    return {"started": False, "note": "Didn't come up; check the log.", **status(state)}


def stop(state):
    pid = _pid(state)
    if not pid:
        return {"stopped": False, "note": "Not running."}
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, 15)
    return {"stopped": True, "pid": pid}


def _startup_file():
    from pathlib import Path
    if os.name != "nt":
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return config / "systemd" / "user" / UNIT_NAME
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / STARTUP_NAME)


def autostart(state, enable):
    path, state = _startup_file(), os.path.abspath(state)
    command = f'"{sys.executable}" -m jarvis --state "{state}" clipboard'
    if os.name != "nt":
        def systemctl(*args):
            return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                                  timeout=30).returncode == 0
        if not enable:
            systemctl("disable", "--now", UNIT_NAME)
            path.unlink(missing_ok=True)
            systemctl("daemon-reload")
            return {"autostart": False}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# JARVIS: unified clipboard. `jarvis clipboard autostart off` removes it.\n"
            "[Unit]\nDescription=JARVIS unified clipboard\n"
            "After=graphical-session.target\n\n"
            f"[Service]\nExecStart={command} run\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=graphical-session.target\n", encoding="utf-8")
        return {"autostart": True, "unit": str(path),
                "enabled": systemctl("daemon-reload") and systemctl("enable", UNIT_NAME)}
    if not enable:
        path.unlink(missing_ok=True)
        return {"autostart": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "' JARVIS: start the unified clipboard hidden at login. Delete this file to disable.\n"
        f'CreateObject("WScript.Shell").Run "{(command + " start").replace(chr(34), chr(34) * 2)}"'
        ", 0, False\n", encoding="ascii")
    return {"autostart": True, "launcher": str(path)}

