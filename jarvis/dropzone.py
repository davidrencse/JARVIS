"""Edge drop zone (Wayland): drag files onto a thin strip at the screen edge that faces the
other laptop, and they go there, the same as copying them (clipsync's file path).

The strip sits just inside Lan Mouse's 1 px edge surface, inside Hyprland's outer gap, so
it covers no tiled window and never blocks the crossing. It is invisible until a file drag
hovers it, then lights up. It accepts COPY only, so the file manager never deletes the
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
.zone { background: transparent; border-radius: 12px 0 0 12px; }
.zone.left { border-radius: 0 12px 12px 0; }
.zone.hot { background: alpha(#4c8dff, 0.55); box-shadow: 0 0 18px alpha(#4c8dff, 0.8); }
"""


def message(paths):
    """The line the parent reads for one drop (only absolute local paths)."""
    return json.dumps({"files": [p for p in paths if isinstance(p, str) and p.startswith("/")]})


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
        zone.add_css_class("zone")
        zone.add_css_class(args.edge)
        zone.set_tooltip_text(f"Drop files here to send them to {args.label}")
        win.set_child(zone)

        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)

        def enter(*_):
            zone.add_css_class("hot")
            return Gdk.DragAction.COPY

        def leave(*_):
            zone.remove_css_class("hot")

        def drop(_target, value, _x, _y):
            zone.remove_css_class("hot")
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            if paths:
                print(message(paths), flush=True)
            return bool(paths)

        target.connect("enter", enter)
        target.connect("leave", leave)
        target.connect("drop", drop)
        zone.add_controller(target)
        win.present()

    app.connect("activate", build)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
