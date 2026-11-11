import os
import unittest


class FakeUser32:
    """Stands in for user32: records calls; the foreground window is whatever we say."""

    def __init__(self, grant_focus):
        self.grant_focus, self.calls, self.fg = grant_focus, [], 111

    def __getattr__(self, name):
        def call(*args):
            self.calls.append(name)
            if name == "GetForegroundWindow":
                return self.fg
            if name == "SetForegroundWindow" and self.grant_focus:
                self.fg = 42
            if name in ("IsWindow", "IsWindowVisible", "AttachThreadInput"):
                return 1
            if name == "GetWindowThreadProcessId":
                return 777
            return 1
        return call


@unittest.skipUnless(os.name == "nt", "overlay is Windows-only")
class OverlayFocusTests(unittest.TestCase):
    def window(self, grant):
        from jarvis.overlay import Window
        w = Window("http://127.0.0.1:1/", "unused-profile")
        w.user32, w.hwnd = FakeUser32(grant), 42
        w.style = w.cover = lambda: None
        return w

    def test_takes_focus_without_synthetic_keys(self):
        w = self.window(grant=True)
        w.show()
        self.assertIn("AttachThreadInput", w.user32.calls)
        self.assertNotIn("keybd_event", w.user32.calls)  # never fake keypresses
        self.assertNotIn("SendInput", w.user32.calls)

    def test_hides_instead_of_trapping_the_keyboard(self):
        w = self.window(grant=False)
        with self.assertRaises(RuntimeError):
            w.show()
        shown = w.user32.calls.index("ShowWindow")
        self.assertIn("ShowWindow", w.user32.calls[shown + 1:])  # shown, then hidden again


if __name__ == "__main__":
    unittest.main()
