"""Unified clipboard: copy on one laptop, paste on the other. Text, images and file copies, both ways.

With the cluster on, each laptop runs its own half (`jarvis clipboard start`, or
`autostart on`, once on each): it watches the local clipboard and hands every new copy
to the other laptop as a `transfer` job over the hub link (transfer.push), which the
receiver's node agent or hub pastes in with apply(). No SSH, so it works whichever way
the tunnel points and on Windows. apply() notes what it pasted in the state folder, so
the receiver's own watcher doesn't bounce the copy straight back.

Without the cluster it rides the SSH link JARVIS already trusts (same key, BatchMode,
strict host-key checking as `jarvis send`): one side runs `jarvis clipboard start`,
which opens one SSH session to the peer and sends this very file as the remote half
(Linux/Wayland peers only). Both halves speak one JSON line per clipboard change.

File copies require the cluster: regular files are staged with transfer.push(dest="drop"),
then a names-only files frame rides the clipboard channel. clipboard.files_max_bytes
limits the whole selection (default 512 MiB). The destination file manager copies from
its local JARVIS-Drop; no source file is moved or removed. Wayland publishes uri-list
only; Windows publishes CF_HDROP with Preferred DropEffect=COPY.

This module is deliberately stdlib-only with no package imports at the top level,
because the remote half runs it as a bare script.

Copies a password manager marks as secret (KeePassXC/KDE hint on Linux, Windows'
ExcludeClipboardContentFromMonitorProcessing) never leave the laptop.
"""

import base64
import glob
import hashlib
import json
import ntpath
import os
import re
import stat
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import urlsplit, unquote

MAX_BYTES = 32 * 1024 * 1024
FILES_MAX_BYTES = 512 * 1024 * 1024
URI_LIST = "text/uri-list"
TEXT = "text/plain;charset=utf-8"
TRANSIENT = "jarvis-clip-transient"  # grokbot writes the digest of its temporary paste here
# Executed by the remote shell: read this module's source from stdin, run its serve().
BOOTSTRAP = ("python3 -c 'import sys;r=sys.stdin.buffer;g={\"__name__\":\"jarvis_clip\"};"
             "exec(r.read(int(r.readline())),g);g[\"serve\"]()'")


def digest(kind, data):
    if kind == "text":  # CRLF and LF copies of the same text must hash alike (Windows adds CRs)
        data = data.replace(b"\r\n", b"\n")
    if kind == "files":
        data = json.dumps(sorted(Path(p).name for p in data), ensure_ascii=True).encode()
    return hashlib.sha256(kind.encode() + b"\0" + data).hexdigest()


def parse_uri_list(data, log=lambda _: None):
    """Decode only local, absolute file URIs; filesystem checks happen in select_files."""
    paths = []
    for line in data.decode("utf-8", errors="strict").splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            uri = urlsplit(line)
            if (uri.scheme != "file" or uri.netloc not in ("", "localhost")
                    or uri.query or uri.fragment or re.search(r"%(?![0-9a-fA-F]{2})", line)):
                raise ValueError("not a local file URI")
            path = unquote(uri.path, errors="strict")
            if not path.startswith("/") or path.startswith("//") or "\0" in path:
                raise ValueError("not a local absolute path")
            if any(part == ".." for part in path.split("/")):
                raise ValueError("parent segment")
            paths.append(path)
        except ValueError as exc:
            log(f"skipped file URI {line!r}: {exc}")
    return paths


def pack_hdrop(paths):
    """Win32 DROPFILES with fixed-width fields (also testable off Windows)."""
    if not paths or any(not isinstance(p, str) or not p or "\0" in p for p in paths):
        raise ValueError("Invalid file list.")
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(paths) + "\0\0").encode("utf-16-le")


def unpack_hdrop(data):
    if len(data) < 24:
        raise ValueError("Invalid DROPFILES.")
    offset, _, _, _, wide = struct.unpack("<IiiII", data[:20])
    if wide != 1 or offset < 20 or offset > len(data) - 4:
        raise ValueError("Invalid DROPFILES.")
    text = data[offset:].decode("utf-16-le")
    if not text.endswith("\0\0"):
        raise ValueError("Unterminated DROPFILES.")
    paths = text[:-2].split("\0")
    if not all(paths):
        raise ValueError("Empty DROPFILES path.")
    return paths


def select_files(paths, max_bytes=FILES_MAX_BYTES, log=lambda _: None):
    """Regular local files only; reject links (including parent links/reparse points)."""
    selected, total = [], 0
    for raw in dict.fromkeys(paths):
        path = Path(raw)
        try:
            drive, tail = ntpath.splitdrive(raw)
            if ("\0" in raw or not path.is_absolute() or raw.startswith(("//", "\\\\"))
                    or any(part == ".." for part in str(raw).replace("\\", "/").split("/"))
                    or (sys.platform == "win32" and (not drive or ":" in tail))):
                raise ValueError("not a local absolute path")
            for part in (path, *path.parents):
                info = part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("symlink or reparse point")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not a regular file")
        except (OSError, ValueError) as exc:
            log(f"skipped file {raw!r}: {exc}")
            continue
        selected.append(path)
        total += info.st_size
    if total > max_bytes:
        raise ValueError(f"File clipboard exceeds {max_bytes} bytes ({total} bytes); nothing sent.")
    return selected, total


def files_in_drop(paths, folder):
    root = Path(folder).resolve()
    return bool(paths) and all(Path(p).resolve().is_relative_to(root) for p in paths)


def file_names(data):
    """Validate the names-only clipboard frame using transfer's canonical rules."""
    from .transfer import NAME, safe_name
    frame = json.loads(data)
    names = frame.get("names") if isinstance(frame, dict) and frame.get("kind") == "files" else None
    if (not isinstance(names, list) or not names
            or any(not isinstance(n, str) or not NAME.fullmatch(n) or safe_name(n) != n for n in names)):
        raise ValueError("Invalid clipboard file names.")
    return names


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


def _inflate(data, limit):
    """Decompress at most ``limit`` bytes. A clipboard PNG must not expand without bound."""
    dec = zlib.decompressobj()
    out = dec.decompress(data, limit + 1)
    if len(out) > limit or dec.unconsumed_tail:
        raise ValueError("image data expands too far")
    tail = dec.flush()
    if tail or not dec.eof:
        raise ValueError("image data expands too far")
    return out


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
    raw = _inflate(b"".join(idat), MAX_PURE_PIXELS * 5 + 1024 * 1024)
    stride, bpp = width * channels, channels
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


MAX_PURE_PIXELS = 1_500_000  # the pure-Python converters take ~10 s for 1080p: skip above this


def _pillow():
    try:
        from PIL import Image
        return Image
    except Exception:
        return None


def dib_to_png(dib):
    """CF_DIB (BITMAPINFOHEADER + pixels, 24 or 32 bpp) -> PNG bytes."""
    size, width, height, _, bits, compression = struct.unpack("<IiiHHI", dib[:20])
    if bits not in (24, 32) or compression not in (0, 3):
        raise ValueError("unsupported bitmap")
    image = _pillow()
    if image is not None:
        try:
            import io
            offset = size + (12 if compression == 3 and size == 40 else 0)
            h = abs(height)
            stride = ((width * bits + 31) // 32) * 4
            mode = "BGRA" if bits == 32 else "BGR"
            img = image.frombuffer(mode, (width, h), bytes(dib[offset:offset + stride * h]),
                                   "raw", mode, stride, 1 if height < 0 else -1)
            if bits == 32:
                img = img.convert("RGBA")
                if img.getchannel("A").getextrema()[1] == 0:
                    img.putalpha(255)  # a 32-bpp DIB with no alpha at all is opaque
            else:
                img = img.convert("RGBA")
            out = io.BytesIO()
            img.save(out, "PNG")
            return out.getvalue()
        except Exception:
            pass  # fall back to the pure-Python path
    if width * abs(height) > MAX_PURE_PIXELS:
        raise ValueError("bitmap too large to convert without Pillow")
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
    image = _pillow()
    if image is not None:
        try:
            import io
            img = image.open(io.BytesIO(png)).convert("RGBA")
            width, height = img.size
            pixels = img.tobytes("raw", "BGRA", 0, -1)  # bottom-up
            head = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 32, 0, len(pixels),
                               2835, 2835, 0, 0)
            return head + pixels
        except Exception:
            pass  # fall back to the pure-Python path
    width, height = struct.unpack(">II", png[16:24]) if len(png) >= 24 else (0, 0)
    if width * height > MAX_PURE_PIXELS:
        raise ValueError("image too large to convert without Pillow")
    width, height, rows = png_decode(png)
    pixels = bytearray()
    for row in reversed(rows):
        for x in range(width):
            r, g, b, a = row[x * 4:x * 4 + 4]
            pixels += bytes((b, g, r, a))
    head = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 32, 0, len(pixels), 2835, 2835, 0, 0)
    return head + bytes(pixels)


def _die_with_parent():
    """Linux: have the kernel kill wl-paste if this process dies without cleaning up
    (SIGKILL, crash), so no `wl-paste --watch` is orphaned. Never raises."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 15, 0, 0, 0)  # PR_SET_PDEATHSIG, SIGTERM
    except Exception:
        pass


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

    def _transient(self):
        """Digest of JARVIS's own temporary copy (Grok prompt paste), while it's fresh."""
        runtime = self.env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        try:
            path = os.path.join(runtime, TRANSIENT)
            if time.time() - os.path.getmtime(path) > 60:
                return None
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None

    def read(self):
        types = (self._paste("--list-types") or b"").decode(errors="replace").split()
        if "x-kde-passwordManagerHint" in types:
            if (self._paste("--type", "x-kde-passwordManagerHint") or b"").strip() == b"secret":
                return None
        if URI_LIST in types:  # only local files count: a browser's copied image also lists its URL
            paths = parse_uri_list(self._paste("--type", URI_LIST) or b"", getattr(self, "log", lambda _: None))
            if paths:
                return ("files", paths)
        if "image/png" in types:
            data = self._paste("--type", "image/png")
            return ("png", data) if data else None
        for kind in (TEXT, "UTF8_STRING", "text/plain", "TEXT", "STRING"):
            if kind in types:
                data = self._paste("--no-newline", "--type", kind)
                if data and digest("text", data) == self._transient():
                    return None
                return ("text", data) if data else None
        return None

    def write(self, kind, data):
        mime = "image/png" if kind == "png" else TEXT
        if kind == "files":
            mime = URI_LIST
            data = ("\r\n".join(Path(p).as_uri() for p in data) + "\r\n").encode()
        subprocess.run(["wl-copy", "--type", mime],
                       input=data, env=self.env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=True)

    def watch(self, changed):
        """Calls changed() on every clipboard change until the watcher dies."""
        proc = self.watcher = subprocess.Popen(["wl-paste", "--watch", "echo"],
                                               stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL, env=self.env,
                                               preexec_fn=_die_with_parent)
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
    """Win32 clipboard through ctypes: text, images and CF_HDROP file copies."""
    CF_UNICODETEXT, CF_DIB, CF_HDROP = 13, 8, 15

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
        self.drop_effect = u.RegisterClipboardFormatW("Preferred DropEffect")
        self.drag_query = ctypes.windll.shell32.DragQueryFileW
        self.drag_query.restype = w.UINT
        self.drag_query.argtypes = [w.HANDLE, w.UINT, w.LPWSTR, w.UINT]
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
            if self.u.IsClipboardFormatAvailable(self.CF_HDROP):
                handle = self.u.GetClipboardData(self.CF_HDROP)
                paths = []
                for i in range(self.drag_query(handle, 0xFFFFFFFF, None, 0)):
                    size = self.drag_query(handle, i, None, 0) + 1
                    buf = self.ct.create_unicode_buffer(size)
                    self.drag_query(handle, i, buf, size)
                    paths.append(buf.value)
                if paths:
                    return ("files", paths)
            if self.u.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                raw = self._get(self.CF_UNICODETEXT) or b""
                text = raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
                return ("text", text.replace("\r\n", "\n").encode()) if text else None
            if self.png and self.u.IsClipboardFormatAvailable(self.png):
                data = self._get(self.png)
                return ("png", data) if data else None
            if self.u.IsClipboardFormatAvailable(self.CF_DIB):
                dib = self._get(self.CF_DIB)
                try:
                    return ("png", dib_to_png(dib)) if dib else None
                except (ValueError, struct.error, IndexError, TypeError):
                    return None
        finally:
            self.u.CloseClipboard()
        return None

    def write(self, kind, data):
        if kind == "png":
            try:
                dib = png_to_dib(data)
            except (ValueError, struct.error, zlib.error, IndexError, TypeError, OSError):
                dib = None  # an unusual PNG: browsers and Office still read the PNG format
        self._open()
        try:
            self.u.EmptyClipboard()
            if kind == "files":
                self._put(self.CF_HDROP, pack_hdrop([str(p) for p in data]))
                self._put(self.drop_effect, struct.pack("<I", 1))  # DROPEFFECT_COPY
            elif kind == "png":
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


def _wait_for_board(log, sleep=time.sleep):
    """The local clipboard, waiting for the desktop to come up rather than exiting: a unit
    started at boot runs before Hyprland, and five quick exits made systemd give up."""
    waited = False
    while True:
        try:
            return local_clipboard()
        except RuntimeError as exc:
            if "No Wayland session" not in str(exc):
                raise
            if not waited:
                log("waiting for the desktop session to start")
                waited = True
            sleep(10)


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
        if not item or item[0] not in ("text", "png") or not item[1] or len(item[1]) > MAX_BYTES:
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


# ---- over the cluster link ---------------------------------------------------------------
APPLIED = "clipboard-applied.json"


def _paused(state):
    """Why the shared clipboard is off right now (apart, not in remote control), or None."""
    try:
        from .sharedinput import clipboard_paused
    except ImportError:  # the bare-script remote half has no package
        return None
    try:
        return clipboard_paused(state)
    except Exception:
        return None


def apply(state, kind, data):
    """Paste a copy that came from the other laptop (the transfer job's last chunk).
    Returns False (not pasted) while sharing is paused here."""
    if kind not in ("text", "png", "files") or not data or len(data) > MAX_BYTES:
        raise ValueError("Not a clipboard copy.")
    if _paused(state):
        return False
    key_data = data
    if kind == "files":
        from .pathsafe import open_leaf
        from .transfer import drop_folder
        names = file_names(data)
        root = Path(drop_folder())
        paths = []
        for name in names:
            try:
                with open_leaf(root, name, "rb"):
                    pass
            except (OSError, ValueError):
                raise ValueError("Clipboard file is missing or outside the drop folder.") from None
            paths.append(root / name)
        key_data, data = names, paths
    if state:  # first, so this machine's watcher sees it as ours, not a new copy
        path = os.path.join(os.path.abspath(state), APPLIED)
        with open(path, "w", encoding="utf-8") as out:
            json.dump({"digest": digest(kind, key_data), "at": time.time()}, out)
    local_clipboard().write(kind, data)
    return True


APPLIED_TTL = 5.0  # seconds a pasted-in copy is recognised as the peer's


def _applied(state, busy=None):
    """busy = (start, end) of the last push: it blocks the watcher, so a paste made while it ran
    is aged from the moment the watcher is free to read it, not from the paste."""
    try:
        with open(os.path.join(state, APPLIED), encoding="utf-8") as f:
            saved = json.load(f)
        at = float(saved.get("at") or 0)
        if busy and busy[0] <= at <= busy[1]:
            at = busy[1]
        # Only the paste we just made: later, the same text copied here is a new copy.
        return saved.get("digest") if time.time() - at <= APPLIED_TTL else None
    except (OSError, ValueError, AttributeError):
        return None


def _notify_files_limit(message):
    from .confirm import notify
    if not notify(message) and sys.platform == "win32" and not os.environ.get("JARVIS_NO_NOTIFY"):
        # Windows has no notify-send; keep its native message off the watcher thread.
        import ctypes
        threading.Thread(target=ctypes.windll.user32.MessageBoxW,
                         args=(None, message, "JARVIS clipboard", 0x40), daemon=True).start()


def run_cluster(config, state, device_name=None, board=None, log=None):
    """Watch this clipboard and push each new copy to the other laptop(s) over the hub."""
    import io
    from .transfer import peer, peers, push, drop_folder
    targets = [peer(config, device_name)] if device_name else peers(config)
    devices = config.get("devices", {})
    # Servers have no desktop clipboard; sending there only logs a refusal on each copy.
    targets = [t for t in targets if (devices.get(t) or {}).get("profile") not in ("cloud", "server")]
    board = board or local_clipboard()
    log = log or _log_line(os.path.join(state, "logs", "clipboard.log"))
    if isinstance(board, (Wayland, Windows)):
        board.log = log
    last = [None]
    busy = [None]
    paused = [None]
    try:
        current = board.read()
        if current:  # don't push whatever happened to be copied before starting
            last[0] = digest(*current)
    except Exception:
        pass

    def changed():
        try:
            item = board.read()
        except Exception as exc:  # a busy or odd clipboard must not stop the watcher
            log(f"read failed: {exc}")
            return
        if not item or not item[1] or len(item[1]) > MAX_BYTES:
            return
        kind, data = item
        key = digest(kind, data)
        if key == last[0]:
            return
        last[0] = key
        if key == _applied(state, busy[0]):  # the other laptop's copy, just pasted in here
            return
        share(kind, data)

    lock = threading.Lock()

    def share(kind, data, dest="drop"):
        """Hand one copy (or an edge-zone drop: kind "files") to the other laptop(s)."""
        with lock:  # the clipboard watcher and the drop zone may both send at once
            _share(kind, data, dest)

    def _share(kind, data, dest):
        why = _paused(state)
        if why:  # apart (and no remote control on): this copy stays here
            if why != paused[0]:
                log(f"sharing paused ({why}); copies stay on this laptop")
            paused[0] = why
            return
        paused[0] = None
        paths, total = [], len(data)
        if kind == "files":
            try:
                paths, total = select_files(data, config.get("clipboard", {}).get("files_max_bytes", FILES_MAX_BYTES), log)
            except ValueError as exc:
                log(str(exc))
                _notify_files_limit(str(exc))
                return
            if not paths or files_in_drop(paths, drop_folder()):
                return
        began = time.time()
        for target in targets:
            try:
                content = data
                if kind == "files":
                    names = []
                    for path in paths:
                        with path.open("rb") as source:
                            result = push(config, state, target, source, path.stat().st_size,
                                          name=path.name, dest=dest)
                        names.append(result["sent"])
                        log(f"sent file {result['sent']} ({path.stat().st_size} bytes) to {target}")
                    if dest == "desktop":
                        log(f"delivered {len(names)} file(s) to {target} Desktop")
                        continue
                    content = json.dumps({"kind": "files", "names": names}).encode()
                    file_names(content)
                push(config, state, target, io.BytesIO(content), len(content), dest="clipboard", kind=kind)
                log(f"sent {kind} ({total} bytes) to {target}")
            except Exception as exc:  # offline peer, hub restarting: skip this copy
                log(f"not sent to {target}: {exc}")
        busy[0] = (began, time.time())

    log(f"linked with {', '.join(targets)} over the cluster")
    if isinstance(board, Wayland) and config.get("clipboard", {}).get("dropzone", True):
        def share_drop(kind, data):
            share(kind, data, dest="desktop")

        threading.Thread(target=_dropzone, args=(config, targets, share_drop, log, board.env),
                         name="dropzone", daemon=True).start()
    while True:
        try:
            board.watch(changed)  # Wayland: returns if wl-paste dies; Windows: runs on
        except Exception as exc:
            log(f"watch failed: {exc}")
        time.sleep(2)


DROPZONE_PYTHON = "/usr/bin/python3"  # has PyGObject + gtk4-layer-shell; the venv doesn't


def _dropzone_command(config, targets, session=None):
    """argv + env for the edge drop zone, or None when this desktop can't show it.
    It sits on the edge Lan Mouse crosses to the peer (its configured position).
    `session` is the clipboard's env, which has the Wayland socket even when this process
    was started without one (the hub's keep-alive, a boot-time unit)."""
    from .dropzone import LIB
    session = dict(os.environ if session is None else session)
    if not (os.path.exists(DROPZONE_PYTHON) and os.path.exists(LIB) and session.get("WAYLAND_DISPLAY")):
        return None
    try:
        from .sharedinput import read_config
        edge = (read_config().get("clients") or [{}])[0].get("position") or "right"
    except Exception:
        edge = "right"
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dropzone.py")
    env = dict(session, LD_PRELOAD=LIB)
    return [DROPZONE_PYTHON, script, "--edge", edge, "--label", ", ".join(targets)], env


def _dropzone(config, targets, share, log, session=None, popen=subprocess.Popen, sleep=time.sleep):
    """Run the drop zone as this process's child and send what is dropped on it, through
    the same path as a file copy. Restarted after a crash, at most every 30 s."""
    command = _dropzone_command(config, targets, session)
    if not command:
        log("drop zone off: needs Wayland, system PyGObject and gtk4-layer-shell")
        return
    argv, env = command
    while True:
        try:
            proc = popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=None, text=True, preexec_fn=_die_with_parent)
        except OSError as exc:
            log(f"drop zone failed to start: {exc}")
            return
        log(f"drop zone on the {argv[3]} edge")
        for line in proc.stdout:
            try:
                paths = json.loads(line).get("files") or []
            except (ValueError, AttributeError):
                continue
            if paths:
                log(f"dropped {len(paths)} file(s) on the edge zone")
                share("files", paths)
        if proc.wait() == 0:
            return  # closed on purpose
        log("drop zone exited; restarting in 30 s")
        sleep(30)


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
    keeps StrictHostKeyChecking on without a fresh "unknown host" after every move.
    The alias and the key are the only bytes written, so a saved key cannot add a line."""
    key = device.get("host_key")
    if not key or not state:
        return []
    alias = "jarvis-" + str(device.get("hostname") or "peer")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", alias):
        raise ValueError("Invalid SSH host key alias.")
    if not re.fullmatch(
            r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-[A-Za-z0-9-]+) [A-Za-z0-9+/=]+", str(key).strip()):
        raise ValueError("Invalid pinned SSH host key.")
    path = os.path.join(state, "known_hosts")
    with open(path, "w", encoding="ascii") as out:
        out.write(f"{alias} {str(key).strip()}\n")
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
    from .transfer import _ssh_args, ssh_host
    target = target or device.get("target")
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("The OpenSSH client isn't installed.")
    args = [*_ssh_args(ssh, device.get("identity_file")), *_pinned_host(device, state), "-T",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            *ssh_host(target), BOOTSTRAP]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=flags)
    try:
        with open(__file__, "rb") as handle:
            source = handle.read()
        proc.stdin.write(b"%d\n" % len(source) + source)
        proc.stdin.flush()
        hello = proc.stdout.readline()
        try:
            status = json.loads(hello)
        except ValueError:
            proc.kill()  # the peer is waiting on stdin: its stderr never ends otherwise
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


CODE_FILE = "clipboard.code"  # the code fingerprint the running watcher was started with


def _record_code(state):
    try:
        from .deploy import code_fingerprint
        with open(os.path.join(state, CODE_FILE), "w") as out:
            out.write(code_fingerprint())
    except Exception:  # never stop the watcher over bookkeeping
        pass


def refresh(state, log=print):
    """Restart a running watcher that is on older code. It is its own long-lived process,
    so neither a service restart nor a node's code update reached it (audit 2026-11-01:
    both laptops' watchers had run unchanged for days). The hub and the node call this
    when they start. True when it was restarted."""
    if not _pid(state):
        return False
    try:
        from .deploy import code_fingerprint
        live = code_fingerprint()
    except Exception:
        return False
    try:
        with open(os.path.join(state, CODE_FILE)) as f:
            started = f.read().strip()
    except OSError:
        started = None
    if started == live:
        return False
    if _unit_installed():
        result = start(state)  # restarts the unit, so systemd keeps owning the watcher
    else:
        stop(state)
        for _ in range(50):
            if not _pid(state):
                break
            time.sleep(0.1)
        result = start(state)
    log(f"clipboard watcher restarted on the current code ({live}): {result.get('started')}")
    return bool(result.get("started"))


LOCK = "clipboard.lock"


def run(config, state, device_name=None):
    """One watcher per state folder, for its whole life. The hub's keep-alive, the node
    agent's and `jarvis clipboard start` can race (audit cluster-05): a second watcher would
    push every copy twice and nothing would ever stop it. A later one leaves quietly."""
    from .locking import exclusive_lock
    state = os.path.abspath(state)
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    try:
        lock = exclusive_lock(os.path.join(state, LOCK))
        lock.__enter__()
    except ValueError:
        _log_line(os.path.join(state, "logs", "clipboard.log"))(
            "another clipboard watcher already runs here; this one exits")
        return None
    try:
        return _run_watcher(config, state, device_name)
    finally:
        lock.__exit__(None, None, None)


def _run_watcher(config, state, device_name=None):
    """Keep the link up: reconnect with backoff (sleep, Wi-Fi changes, the peer rebooting)."""
    from .topology import enabled
    from .transfer import ssh_device
    _record_code(state)
    log = _log_line(os.path.join(state, "logs", "clipboard.log"))
    _own_pid_file(state)
    if enabled(config):
        with open(os.path.join(state, "clipboard.pid"), "w") as pid:
            pid.write(str(os.getpid()))
        return run_cluster(config, state, device_name, board=_wait_for_board(log), log=log)
    device, board, wait = ssh_device(config, device_name), _wait_for_board(log), 2
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


def _own_pid_file(state):
    """Make a SIGTERM unwind the watcher (so `finally` reaps wl-paste) and remove the pid
    file when it exits, so a reused PID is never mistaken for it."""
    import atexit
    import signal
    mine = os.getpid()

    def cleanup():
        try:
            path = os.path.join(state, PID)
            with open(path) as f:
                if f.read().strip() == str(mine):
                    os.remove(path)
        except OSError:
            pass

    atexit.register(cleanup)
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ValueError, OSError):  # not the main thread
        pass


def _is_watcher(pid):
    """After a reboot a stale clipboard.pid can name an unrelated process: check it is
    really `jarvis ... clipboard run` before trusting it (or killing it)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().split(b"\0")
    except OSError:
        if os.name != "nt" and os.path.isdir("/proc"):
            return False  # /proc exists but this pid doesn't: gone
        return _windows_is_python(pid)
    if stat.rpartition(")")[2].split()[:1] == ["Z"]:
        return False
    return b"jarvis" in cmdline and b"clipboard" in cmdline and b"run" in cmdline


def _windows_is_python(pid):
    """No /proc: on Windows at least require the process to be a Python interpreter; elsewhere
    the liveness probe is all there is."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                                   ctypes.byref(size))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and "python" in os.path.basename(buf.value).lower()
    except Exception:
        return True


def _lock_held(state):
    """True while a live process holds clipboard.lock (the watcher holds it for its whole life)."""
    from .daemon import _try_lock
    path = os.path.join(state, LOCK)
    if not os.path.isfile(path):
        return False
    try:
        handle = open(path, "a+")
    except OSError:
        return False
    try:
        _try_lock(handle)
    except OSError:
        return True
    finally:
        handle.close()
    return False


def _pid(state):
    from .daemon import _alive
    path = os.path.join(state, PID)
    try:
        with open(path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if _alive(pid) and _is_watcher(pid) and (os.path.isdir("/proc") or _lock_held(state)):
        # Without /proc (Windows) "some Python" is all _is_watcher can tell, and a reused PID
        # after a reboot is often another pythonw: the watcher also holds clipboard.lock.
        return pid
    try:
        os.remove(path)  # stale: the process is gone or the PID was reused
    except OSError:
        pass
    return None


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


STOPPED = "clipboard.stopped"  # a stop by hand: {"boot": boot_time()} at that stop
STOP_JITTER = 120  # seconds: boot times closer than this are the same boot


def _boot():
    try:
        from .platforms import boot_time
        return boot_time()
    except Exception:
        return None


def stopped_by_hand(state):
    """True while a `clipboard stop` from this boot stands: the keep-alives leave it off.
    Like Lan Mouse, a stop by hand sticks until the next boot (audit cluster-02). An
    unreadable boot time never keeps it off."""
    try:
        with open(os.path.join(state, STOPPED), encoding="utf-8") as f:
            then = json.load(f).get("boot")
    except (OSError, ValueError, AttributeError):
        return False
    now = _boot()
    if then is None or now is None:
        return False
    try:
        return abs(float(then) - float(now)) <= STOP_JITTER
    except (TypeError, ValueError):
        return False


def _clear_stopped(state):
    try:
        os.remove(os.path.join(state, STOPPED))
    except OSError:
        pass


def start(state, device_name=None):
    _clear_stopped(state)  # asked to run: a hand stop earlier no longer holds
    if _pid(state):
        return {"started": False, **status(state)}
    state = os.path.abspath(state)
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             if os.name == "nt" else 0)
    if not device_name and _unit_installed():
        try:
            done = subprocess.run(["systemctl", "--user", "restart", UNIT_NAME],
                                  capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0:
            for _ in range(40):
                time.sleep(0.25)
                if _pid(state):
                    return {"started": True, **status(state)}
            return {"started": False, "note": "Didn't come up; check the log.", **status(state)}
    args = [sys.executable, "-m", "jarvis", "--state", state, "clipboard", "run"]
    if device_name:
        args += ["--device", device_name]
    if os.name != "nt":  # out of the caller's cgroup, so restarting jarvis-cluster keeps it
        from .daemon import _detach_from_unit
        args = _detach_from_unit(args)
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
    try:  # even when it's already down, so a keep-alive doesn't bring it back this boot
        with open(os.path.join(state, STOPPED), "w", encoding="utf-8") as f:
            json.dump({"boot": _boot()}, f)
    except OSError:
        pass
    pid = _pid(state)
    if not pid:
        return {"stopped": False, "note": "Not running."}
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, 15)
    return {"stopped": True, "pid": pid}


def _unit_installed():
    """The Linux systemd user unit is installed and runs `clipboard run`."""
    if os.name == "nt" or not shutil.which("systemctl"):
        return False
    try:
        return "clipboard run" in _startup_file().read_text(encoding="utf-8")
    except OSError:
        return False


def _startup_file():
    from .autostart import startup_file, unit_file
    return startup_file(STARTUP_NAME) if os.name == "nt" else unit_file(UNIT_NAME)


def autostart(state, enable):
    path, state = _startup_file(), os.path.abspath(state)
    if os.name != "nt":
        from .platforms import systemctl_user

        def systemctl(*args):
            return systemctl_user(*args)[0] == 0
        if not enable:
            systemctl("disable", "--now", UNIT_NAME)
            path.unlink(missing_ok=True)
            systemctl("daemon-reload")
            return {"autostart": False}
        from .autostart import systemd_unit
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(
            "JARVIS unified clipboard", [sys.executable, "-m", "jarvis", "--state", state, "clipboard", "run"],
            comment="JARVIS: unified clipboard. `jarvis clipboard autostart off` removes it.",
            after="graphical-session.target", wanted="graphical-session.target",
            service=['Environment="PYTHONIOENCODING=utf-8"']), encoding="utf-8")
        return {"autostart": True, "unit": str(path),
                "enabled": systemctl("daemon-reload") and systemctl("enable", UNIT_NAME)}
    if not enable:
        path.unlink(missing_ok=True)
        return {"autostart": False}
    from .autostart import write_launcher
    write_launcher(path, [sys.executable, "-m", "jarvis", "--state", state, "clipboard", "start"],
                   "JARVIS: start the unified clipboard hidden at login. Delete this file to disable.")
    return {"autostart": True, "launcher": str(path)}



def control(arg, state=None):
    """The `clipboard` desktop action, so any computer can switch this one's half on or off:
    start, stop, status, autostart-on or autostart-off. (ok, json text)."""
    from pathlib import Path
    state = state or str(Path(__file__).resolve().parents[1] / ".jarvis")
    try:
        if arg == "start":
            result = start(state)
        elif arg == "stop":
            result = stop(state)
        elif arg == "status":
            result = status(state)
        elif arg in ("autostart-on", "autostart-off"):
            result = autostart(state, arg == "autostart-on")
        else:
            raise ValueError("Use start, stop, status, autostart-on or autostart-off.")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    result.pop("log", None)  # a local path; the other computer can't open it
    return True, json.dumps(result)
