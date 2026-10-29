"""A snapshot of this computer's screen, for the Devices view's screen viewer.

Read-only: it takes a picture and changes nothing. The `screen` desktop action returns
{"mime", "b64", "width", "height"} as JSON text, so it travels like any other desktop
action (cluster hub, SSH or the Grokbot tunnel). Images are downscaled and JPEG'd to stay
far under the cluster's result limit.
"""

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_WIDTH = 1600
QUALITY = 60
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _shrink(data, width, quality):
    """(jpeg bytes, w, h) at most `width` wide. Without Pillow, the capture goes as it came."""
    try:
        from PIL import Image
    except ImportError:
        return data, 0, 0
    image = Image.open(io.BytesIO(data)).convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue(), image.width, image.height


def _windows():
    try:
        from PIL import ImageGrab
        out = io.BytesIO()
        ImageGrab.grab(all_screens=True).save(out, "PNG")
        return out.getvalue()
    except ImportError:
        pass
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder, "screen.png")
        script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
                  "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                  "$g=[System.Drawing.Graphics]::FromImage($i);"
                  "$g.CopyFromScreen($b.Left,$b.Top,0,0,$i.Size);"
                  f"$i.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Png)")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=20, creationflags=FLAGS, check=True)
        return path.read_bytes()


def _macos():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder, "screen.png")
        subprocess.run(["screencapture", "-x", "-t", "png", str(path)], capture_output=True,
                       timeout=20, check=True)
        return path.read_bytes()


def _linux():
    if os.environ.get("WAYLAND_DISPLAY"):
        if not shutil.which("grim"):
            raise LookupError("grim is not installed (needed to capture a Wayland screen)")
        return subprocess.run(["grim", "-t", "png", "-"], capture_output=True, timeout=20,
                              check=True).stdout
    for args in (["maim"], ["import", "-window", "root", "png:-"]):
        if shutil.which(args[0]):
            return subprocess.run(args, capture_output=True, timeout=20, check=True).stdout
    raise LookupError("no screen capture tool (grim, maim or ImageMagick import) is installed")


def capture(width=MAX_WIDTH, quality=QUALITY):
    from .platforms import detect
    system = detect()["os"]
    raw = {"windows": _windows, "macos": _macos}.get(system, _linux)()
    if not raw:
        raise OSError("the capture came back empty")
    data, w, h = _shrink(raw, width, quality)
    return {"mime": "image/jpeg" if w else "image/png",
            "b64": base64.b64encode(data).decode(), "width": w, "height": h}


def run(arg):
    """The `screen` desktop action: arg is "" or JSON {"width", "quality"}. (ok, json text)."""
    try:
        body = json.loads(arg) if arg else {}
        width = max(320, min(int(body.get("width", MAX_WIDTH)), 2560))
        quality = max(20, min(int(body.get("quality", QUALITY)), 90))
        return True, json.dumps(capture(width, quality))
    except subprocess.CalledProcessError as exc:
        return False, f"Can't capture the screen: {(exc.stderr or b'').decode(errors='replace').strip()[-200:] or exc}"
    except (ValueError, TypeError, LookupError, OSError, subprocess.SubprocessError) as exc:
        return False, f"Can't capture the screen: {exc}"
