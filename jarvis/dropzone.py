"""Edge drop zone (Wayland): drag files onto a thin strip at the screen edge that faces the
other laptop, and they go there, the same as copying them (clipsync's file path).

The strip sits just inside Lan Mouse's 1 px edge surface, inside Hyprland's outer gap, so
it covers no tiled window and never blocks the crossing. A faint blue strip marks the
target; it brightens during a file drag. It accepts COPY only, so the file manager never deletes the
original. Each drop prints one JSON line on stdout: {"files": ["/abs/path", ...]}.

Run by the clipboard service (clipsync.run_cluster) with the system python, which has
PyGObject and gtk4-layer-shell (the venv doesn't), and LD_PRELOAD of that library, which
gtk4-layer-shell requires from Python. It dies with its parent. Nothing here listens on
the network; the bytes travel through transfer.push like any other file copy.
"""

import json
import sys

LIB = "/usr/lib/libgtk4-layer-shell.so"
WIDTH = 24      # logical px; Hyprland's outer gap is wider, so no tiled window is covered
SPAN = 0.6      # share of the edge's length the strip covers, centred
CSS = b"""
window { background: transparent; }
.zone { background: alpha(#4c8dff, 0.18); border-radius: 12px 0 0 12px; }
.zone.left { border-radius: 0 12px 12px 0; }
.zone.hot { background: alpha(#4c8dff, 0.55); }
"""


def message(paths):
    """The line the parent reads for one drop (only absolute local paths)."""
    return json.dumps({"files": [p for p in paths if isinstance(p, str) and p.startswith("/")]})


MAX_URI_BYTES = 1024 * 1024


def uri_paths(data):
    """Local files from a text/uri-list. Never turn remote URLs into local paths."""
    from urllib.parse import urlsplit, unquote
    paths = []
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        uri = urlsplit(line)
        if uri.scheme != "file" or uri.netloc not in ("", "localhost") or uri.query or uri.fragment:
            continue
        path = unquote(uri.path, errors="strict")
        if path.startswith("/") and "\x00" not in path and path not in paths:
            paths.append(path)
    return paths


def receive_drop(drop, copy_action, priority=0, emit=print):
    """Read the offered URI list asynchronously, then acknowledge COPY after handing it off."""
    chunks = bytearray()
    stream = None
    finished = False

    def finish(paths=None, error=None):
        nonlocal finished
        if finished:
            return
        finished = True
        if error:
            print(f"drop zone: file list rejected: {error}", file=sys.stderr, flush=True)
        action = 0
        try:
            if paths:
                emit(message(paths), flush=True)
                action = copy_action
        finally:
            drop.finish(action)
            if stream is not None:
                stream.close(None)

    def read_chunk(source, result, *_):
        try:
            data = source.read_bytes_finish(result).get_data()
            if not data:
                finish(uri_paths(bytes(chunks)))
                return
            if len(chunks) + len(data) > MAX_URI_BYTES:
                finish(error="file list exceeds 1 MiB")
                return
            chunks.extend(data)
            source.read_bytes_async(65536, priority, None, read_chunk)
        except Exception as exc:
            finish(error=str(exc))

    def opened(source, result, *_):
        nonlocal stream
        try:
            stream, mime = source.read_finish(result)
            if stream is None or mime != "text/uri-list":
                finish(error="no file URI list received")
                return
            stream.read_bytes_async(65536, priority, None, read_chunk)
        except Exception as exc:
            finish(error=str(exc))

    try:
        drop.read_async(["text/uri-list"], priority, None, opened)
    except Exception as exc:
        finish(error=str(exc))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", choices=("left", "right", "top", "bottom"), default="right")
    ap.add_argument("--label", default="the other laptop")
    args = ap.parse_args(argv)

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gdk, Gtk, Gtk4LayerShell as Layer

    app = Gtk.Application(application_id="dev.jarvis.dropzone")

    def build(app):
        win = Gtk.ApplicationWindow(application=app, title="JARVIS drop zone")
        Layer.init_for_window(win)
        Layer.set_namespace(win, "jarvis-dropzone")
        Layer.set_layer(win, Layer.Layer.TOP)  # under fullscreen windows, unlike OVERLAY
        Layer.set_keyboard_mode(win, Layer.KeyboardMode.NONE)
        Layer.set_exclusive_zone(win, 0)
        side = {"left": Layer.Edge.LEFT, "right": Layer.Edge.RIGHT,
                "top": Layer.Edge.TOP, "bottom": Layer.Edge.BOTTOM}[args.edge]
        Layer.set_anchor(win, side, True)
        Layer.set_margin(win, side, 1)  # leave Lan Mouse's own 1 px edge exposed
        monitor = Gdk.Display.get_default().get_monitors().get_item(0)
        geo = monitor.get_geometry()
        across = args.edge in ("left", "right")
        length = int((geo.height if across else geo.width) * SPAN)
        win.set_default_size(WIDTH if across else length, length if across else WIDTH)

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        zone = Gtk.Box()
        zone.set_hexpand(True)
        zone.set_vexpand(True)
        zone.add_css_class("zone")
        zone.add_css_class(args.edge)
        zone.set_tooltip_text(f"Drop files here to copy them to {args.label} Desktop")
        win.set_child(zone)

        # Read Dolphin's standard MIME data directly instead of relying on GTK's
        # GdkFileList/portal conversion before it accepts the drag.
        formats = Gdk.ContentFormats.new(["text/uri-list"])
        target = Gtk.DropTargetAsync.new(formats, Gdk.DragAction.COPY)

        def accept(_target, offered):
            compatible = offered.get_formats().contain_mime_type("text/uri-list")
            print(f"drop zone: offer actions={offered.get_actions()} uri-list={compatible}",
                  file=sys.stderr, flush=True)
            return compatible

        def enter(*_):
            zone.add_css_class("hot")
            return Gdk.DragAction.COPY

        def leave(*_):
            zone.remove_css_class("hot")

        def drop(_target, offered, _x, _y):
            zone.remove_css_class("hot")
            print("drop zone: reading dropped file list", file=sys.stderr, flush=True)
            receive_drop(offered, Gdk.DragAction.COPY)
            return True

        target.connect("accept", accept)
        target.connect("drag-enter", enter)
        target.connect("drag-motion", enter)
        target.connect("drag-leave", leave)
        target.connect("drop", drop)
        win.add_controller(target)
        win.present()

    app.connect("activate", build)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
