"""jarvis.screen with every capture backend mocked: no real screen is ever read."""

import base64
import io
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from jarvis import screen

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


def png(width=100, height=50, color=(10, 20, 30)):
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def system(name):
    return patch("jarvis.platforms.detect", return_value={"os": name})


def done(stdout=b"", code=0):
    return subprocess.CompletedProcess([], code, stdout, b"")


@unittest.skipIf(Image is None, "Pillow not installed")
class ShrinkTests(unittest.TestCase):
    def test_wide_image_is_downscaled_to_jpeg(self):
        data, w, h = screen._shrink(png(2000, 1000), 1000, 60)
        self.assertEqual((w, h), (1000, 500))
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_narrow_image_keeps_size(self):
        data, w, h = screen._shrink(png(100, 50), 1600, 60)
        self.assertEqual((w, h), (100, 50))
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_garbage_bytes_raise_oserror(self):
        with self.assertRaises(OSError):
            screen._shrink(b"not an image", 100, 60)


class ShrinkNoPillowTests(unittest.TestCase):
    def test_without_pillow_data_passes_through(self):
        with patch.dict("sys.modules", {"PIL": None}):
            self.assertEqual(screen._shrink(b"raw", 100, 60), (b"raw", 0, 0))


class LinuxTests(unittest.TestCase):
    def test_wayland_uses_grim(self):
        with patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}), \
                patch("shutil.which", return_value="/usr/bin/grim"), \
                patch("subprocess.run", return_value=done(b"PNG")) as run:
            self.assertEqual(screen._linux(), b"PNG")
        self.assertEqual(run.call_args.args[0], ["grim", "-t", "png", "-"])

    def test_wayland_without_grim(self):
        with patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}), \
                patch("shutil.which", return_value=None), patch("subprocess.run") as run:
            with self.assertRaisesRegex(LookupError, "grim is not installed"):
                screen._linux()
        run.assert_not_called()

    def test_x11_prefers_maim_then_import(self):
        env = {k: v for k, v in __import__("os").environ.items() if k != "WAYLAND_DISPLAY"}
        with patch.dict("os.environ", env, clear=True):
            with patch("shutil.which", side_effect=lambda n: "/x/" + n), \
                    patch("subprocess.run", return_value=done(b"A")) as run:
                self.assertEqual(screen._linux(), b"A")
            self.assertEqual(run.call_args.args[0], ["maim"])
            with patch("shutil.which", side_effect=lambda n: "/x/import" if n == "import" else None), \
                    patch("subprocess.run", return_value=done(b"B")) as run:
                self.assertEqual(screen._linux(), b"B")
            self.assertEqual(run.call_args.args[0], ["import", "-window", "root", "png:-"])

    def test_no_tool_installed(self):
        with patch.dict("os.environ", {}, clear=True), patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(LookupError, "no screen capture tool"):
                screen._linux()

    def test_tool_failure_propagates(self):
        with patch.dict("os.environ", {}, clear=True), patch("shutil.which", return_value="/x/maim"), \
                patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "maim")):
            with self.assertRaises(subprocess.CalledProcessError):
                screen._linux()


class MacTests(unittest.TestCase):
    def test_screencapture_file_is_read(self):
        def fake(args, **kw):
            self.assertEqual(args[:4], ["screencapture", "-x", "-t", "png"])
            __import__("pathlib").Path(args[4]).write_bytes(b"MACPNG")
            return done()
        with patch("subprocess.run", side_effect=fake):
            self.assertEqual(screen._macos(), b"MACPNG")

    def test_screencapture_failure(self):
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "screencapture")):
            with self.assertRaises(subprocess.CalledProcessError):
                screen._macos()

    def test_no_file_written_is_oserror(self):
        with patch("subprocess.run", return_value=done()):
            with self.assertRaises(OSError):
                screen._macos()


class WindowsTests(unittest.TestCase):
    def test_imagegrab_path(self):
        grab = MagicMock()
        grab.grab.return_value.save.side_effect = lambda out, fmt: out.write(b"WINPNG")
        fake_pil = MagicMock(ImageGrab=grab)
        with patch.dict("sys.modules", {"PIL": fake_pil, "PIL.ImageGrab": grab}), \
                patch("subprocess.run") as run:
            self.assertEqual(screen._windows(), b"WINPNG")
        grab.grab.assert_called_once_with(all_screens=True)
        run.assert_not_called()

    def test_powershell_fallback_without_pillow(self):
        def fake(args, **kw):
            self.assertEqual(args[0], "powershell")
            self.assertTrue(kw["check"])
            import re
            path = re.search(r"Save\('([^']+)'", args[-1]).group(1)
            __import__("pathlib").Path(path).write_bytes(b"PSPNG")
            return done()
        with patch.dict("sys.modules", {"PIL": None}), patch("subprocess.run", side_effect=fake):
            self.assertEqual(screen._windows(), b"PSPNG")

    def test_powershell_failure(self):
        with patch.dict("sys.modules", {"PIL": None}), \
                patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "powershell")):
            with self.assertRaises(subprocess.CalledProcessError):
                screen._windows()


class CaptureTests(unittest.TestCase):
    def test_dispatch_by_platform(self):
        for name, attr in (("windows", "_windows"), ("macos", "_macos"), ("linux", "_linux"),
                           ("freebsd", "_linux"), ("", "_linux")):
            with self.subTest(os=name), system(name), \
                    patch.object(screen, "_shrink", return_value=(b"J", 4, 3)), \
                    patch.multiple(screen, _windows=MagicMock(return_value=b"w"),
                                   _macos=MagicMock(return_value=b"m"),
                                   _linux=MagicMock(return_value=b"l")):
                result = screen.capture()
                for other in ("_windows", "_macos", "_linux"):
                    self.assertEqual(getattr(screen, other).called, other == attr)
            self.assertEqual(result, {"mime": "image/jpeg", "b64": base64.b64encode(b"J").decode(),
                                      "width": 4, "height": 3})

    def test_empty_capture_is_oserror(self):
        with system("linux"), patch.object(screen, "_linux", return_value=b""):
            with self.assertRaisesRegex(OSError, "came back empty"):
                screen.capture()

    def test_without_pillow_returns_png_with_zero_size(self):
        with system("linux"), patch.object(screen, "_linux", return_value=b"RAW"), \
                patch.dict("sys.modules", {"PIL": None}):
            result = screen.capture()
        self.assertEqual(result["mime"], "image/png")
        self.assertEqual((result["width"], result["height"]), (0, 0))
        self.assertEqual(base64.b64decode(result["b64"]), b"RAW")

    def test_arguments_reach_shrink(self):
        with system("linux"), patch.object(screen, "_linux", return_value=b"x"), \
                patch.object(screen, "_shrink", return_value=(b"j", 1, 1)) as shrink:
            screen.capture(800, 33)
        shrink.assert_called_once_with(b"x", 800, 33)

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_end_to_end_with_real_image(self):
        with system("linux"), patch.object(screen, "_linux", return_value=png(3200, 1600)):
            result = screen.capture(1600, 60)
        self.assertEqual((result["width"], result["height"]), (1600, 800))
        self.assertEqual(result["mime"], "image/jpeg")
        self.assertTrue(base64.b64decode(result["b64"]).startswith(b"\xff\xd8"))


class RunTests(unittest.TestCase):
    def run_with(self, arg, **kw):
        with patch.object(screen, "capture", return_value={"mime": "m"}, **kw) as cap:
            return screen.run(arg), cap

    def test_empty_arg_uses_defaults(self):
        (ok, text), cap = self.run_with("")
        self.assertTrue(ok)
        self.assertEqual(json.loads(text), {"mime": "m"})
        cap.assert_called_once_with(screen.MAX_WIDTH, screen.QUALITY)

    def test_values_are_clamped(self):
        cases = (({"width": 1, "quality": 1}, (320, 20)), ({"width": 99999, "quality": 999}, (2560, 90)),
                 ({"width": "800", "quality": "50"}, (800, 50)), ({"width": 1000}, (1000, screen.QUALITY)),
                 ({"width": -5, "quality": -5}, (320, 20)), ({"width": 7.9}, (320, screen.QUALITY)))
        for body, expected in cases:
            with self.subTest(body=body):
                (ok, _), cap = self.run_with(json.dumps(body))
                self.assertTrue(ok)
                cap.assert_called_once_with(*expected)

    def test_bad_arguments_fail_softly(self):
        for arg in ("{not json", '{"width": "abc"}', '{"width": null}', '{"quality": []}', '{"width": {}}'):
            with self.subTest(arg=arg):
                (ok, text), cap = self.run_with(arg)
                self.assertFalse(ok)
                self.assertTrue(text.startswith("Can't capture the screen:"))
                cap.assert_not_called()

    def test_non_object_json_fails_softly(self):
        for arg in ("[1]", "null", "5", '"x"'):
            with self.subTest(arg=arg):
                (ok, text), _ = self.run_with(arg)
                self.assertFalse(ok)

    def test_called_process_error_reports_stderr_tail(self):
        err = subprocess.CalledProcessError(1, "maim", stderr=b"x" * 500 + b"boom\n")
        with patch.object(screen, "capture", side_effect=err):
            ok, text = screen.run("")
        self.assertFalse(ok)
        self.assertTrue(text.endswith("boom"))
        self.assertLess(len(text), 260)

    def test_called_process_error_without_stderr(self):
        err = subprocess.CalledProcessError(2, "maim")
        with patch.object(screen, "capture", side_effect=err):
            ok, text = screen.run("")
        self.assertFalse(ok)
        self.assertIn("maim", text)

    def test_other_failures_are_reported(self):
        for exc in (LookupError("no tool"), OSError("empty"), subprocess.TimeoutExpired("maim", 20)):
            with self.subTest(exc=exc), patch.object(screen, "capture", side_effect=exc):
                ok, text = screen.run("")
                self.assertFalse(ok)
                self.assertIn("Can't capture the screen:", text)

    def test_unexpected_exception_is_not_swallowed(self):
        with patch.object(screen, "capture", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                screen.run("")


if __name__ == "__main__":
    unittest.main()
