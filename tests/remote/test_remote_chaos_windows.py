"""Breaker "windows": the Windows-only paths of remote control, run on Linux.

ctypes.windll does not exist here, so every test hands remote_view/control a fake user32
(and shcore) and asserts the exact calls they make. Nothing touches a real mouse, keyboard,
screen or Lan Mouse.

Tests marked expectedFailure expose a real bug; the comment names the finding id.
"""

import ctypes
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import control, remote_view, sharedinput
from jarvis.remote_view import Injector, Stopped

SCRATCH = Path(os.path.expanduser("~/.cache/jarvis-breakit"))

# GetSystemMetrics indexes
SM_SWAPBUTTON = 23
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
LEFT = (0x2, 0x4)
RIGHT = (0x8, 0x10)
MIDDLE = (0x20, 0x40)

# What MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) returns on a US layout, for the keys used here.
SCANS = {0xA0: 0x2A, 0xA2: 0x1D, 0xA3: 0x1D, 0xA4: 0x38, 0xA5: 0x38, 0x25: 0x4B, 0x26: 0x48,
         0x27: 0x4D, 0x28: 0x50, 0x0D: 0x1C, 0x41: 0x1E, 0x2E: 0x53, 0x5B: 0x5B}


class FakeUser32:
    def __init__(self, metrics=None, dpi_context_ok=1, send_ok=True):
        self.calls = []
        self.metrics = metrics or {SM_XVIRTUALSCREEN: 0, SM_YVIRTUALSCREEN: 0,
                                   SM_CXVIRTUALSCREEN: 1920, SM_CYVIRTUALSCREEN: 1080}
        self.dpi_context_ok = dpi_context_ok
        self.send_ok = send_ok

    def SetProcessDpiAwarenessContext(self, ctx):
        self.calls.append(("SetProcessDpiAwarenessContext",))
        return self.dpi_context_ok  # a BOOL: 0 on failure, never an exception

    def SetProcessDPIAware(self):
        self.calls.append(("SetProcessDPIAware",))
        return 1

    def GetSystemMetrics(self, index):
        self.calls.append(("GetSystemMetrics", index))
        return self.metrics.get(index, 0)

    def SetCursorPos(self, x, y):
        self.calls.append(("SetCursorPos", x, y))
        return 1

    def mouse_event(self, flags, dx, dy, data, extra):
        self.calls.append(("mouse_event", flags, dx, dy, data))

    def MapVirtualKeyW(self, vk, kind):
        return SCANS.get(vk, vk + 0x100)

    def keybd_event(self, vk, scan, flags, extra):
        # void in Windows: a locked screen, a UAC prompt or UIPI drops it without a word
        self.calls.append(("keybd_event", vk, scan, flags))

    def SendInput(self, count, inputs, size):
        self.calls.append(("SendInput", count))
        if not self.send_ok:
            return 0  # 0: the input desktop refused it
        for i in range(count):  # recorded in the old keybd_event/mouse_event shapes
            item = inputs[i]
            if item.type == 1:
                self.calls.append(("keybd_event", item.ki.wVk, item.ki.wScan, item.ki.dwFlags))
            elif item.type == 0:
                self.calls.append(("mouse_event", item.mi.dwFlags, item.mi.dx, item.mi.dy, item.mi.mouseData))
        return count

    def of(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeShcore:
    def __init__(self):
        self.calls = []

    def SetProcessDpiAwareness(self, value):
        self.calls.append(value)
        return 0


def windll(user32, shcore=None):
    return SimpleNamespace(user32=user32, shcore=shcore or FakeShcore(),
                           kernel32=SimpleNamespace())


def windows_injector(user32):
    """An Injector wired to the real control.Windows pointer and WindowsKeys, on a fake user32."""
    with patch.object(ctypes, "windll", windll(user32), create=True):
        pointer = control.Windows()
        keys = remote_view.WindowsKeys()
    return Injector(pointer, keys)


def keys_sent(user32):
    return [c[1:] for c in user32.of("keybd_event")]


class MultiMonitorCoordinates(unittest.TestCase):
    def test_secondary_monitor_left_of_primary_gives_negative_virtual_coordinates(self):
        user32 = FakeUser32()
        inj = windows_injector(user32)
        inj.area = {"x": -1920, "y": 0, "w": 3840, "h": 1080}  # left monitor, then the primary
        for fx, want in ((0.0, -1920), (0.25, -960), (0.5, 0), (0.75, 960)):
            inj.apply({"t": "move", "x": fx, "y": 0.5})
            self.assertEqual(user32.of("SetCursorPos")[-1], ("SetCursorPos", want, 540))

    def test_monitor_above_primary_gives_negative_y(self):
        user32 = FakeUser32()
        inj = windows_injector(user32)
        inj.area = {"x": 0, "y": -1440, "w": 2560, "h": 2520}
        inj.apply({"t": "down", "b": "left", "x": 0.5, "y": 0.0})
        self.assertEqual(user32.of("SetCursorPos")[-1], ("SetCursorPos", 1280, -1440))
        self.assertEqual(user32.of("mouse_event")[-1][1], LEFT[0])

    def test_click_moves_first_then_presses_at_the_pointer(self):
        user32 = FakeUser32()
        inj = windows_injector(user32)
        inj.area = {"x": -1920, "y": 0, "w": 3840, "h": 1080}
        inj.apply({"t": "down", "b": "left", "x": 0.1, "y": 0.1})
        inj.apply({"t": "up", "b": "left", "x": 0.1, "y": 0.1})
        names = [c[0] for c in user32.calls if c[0] in ("SetCursorPos", "mouse_event")]
        self.assertEqual(names, ["SetCursorPos", "mouse_event", "SetCursorPos", "mouse_event"])
        self.assertEqual([c[1] for c in user32.of("mouse_event")], [LEFT[0], LEFT[1]])

    # W4: x = 1.0 lands one pixel past the virtual screen's right edge (fixed with INPUT-1).
    def test_far_edge_stays_inside_the_virtual_screen(self):
        user32 = FakeUser32()
        inj = windows_injector(user32)
        inj.area = {"x": -1920, "y": 0, "w": 3840, "h": 1080}
        inj.apply({"t": "move", "x": 1.0, "y": 1.0})
        _, x, y = user32.of("SetCursorPos")[-1]
        self.assertLessEqual(x, -1920 + 3840 - 1)
        self.assertLessEqual(y, 1080 - 1)


class GrabAndDpi(unittest.TestCase):
    def setUp(self):
        remote_view._DPI_AWARE = False
        self.addCleanup(setattr, remote_view, "_DPI_AWARE", False)

    def test_grab_sets_dpi_awareness_before_reading_the_virtual_screen(self):
        from PIL import Image
        user32 = FakeUser32(metrics={SM_XVIRTUALSCREEN: -1920, SM_YVIRTUALSCREEN: -200,
                                     SM_CXVIRTUALSCREEN: 4480, SM_CYVIRTUALSCREEN: 1640})
        picture = Image.new("RGB", (4480, 1640))
        with patch.object(ctypes, "windll", windll(user32), create=True), \
                patch("jarvis.platforms.detect", return_value={"os": "windows", "desktop": "windows"}), \
                patch("PIL.ImageGrab.grab", return_value=picture) as grabbed:
            image, area = remote_view.grab()
        self.assertIs(image, picture)
        self.assertEqual(area, {"x": -1920, "y": -200, "w": 4480, "h": 1640})
        self.assertTrue(grabbed.call_args.kwargs.get("all_screens"))
        names = [c[0] for c in user32.calls]
        self.assertLess(names.index("SetProcessDpiAwarenessContext"), names.index("GetSystemMetrics"))

    # W3: SetProcessDpiAwarenessContext returns FALSE (Windows 10 before 1703 has no
    # PER_MONITOR_AWARE_V2) instead of raising, and the code takes that as success: no fallback,
    # the process stays DPI-unaware and clicks land scaled on a 125%/150% screen.
    def test_remote_view_dpi_falls_back_when_the_context_call_returns_false(self):
        user32, shcore = FakeUser32(dpi_context_ok=0), FakeShcore()
        remote_view._set_dpi_awareness(SimpleNamespace(windll=windll(user32, shcore), c_void_p=ctypes.c_void_p))
        self.assertTrue(shcore.calls or user32.of("SetProcessDPIAware"))

    def test_control_windows_dpi_falls_back_when_the_context_call_returns_false(self):
        user32, shcore = FakeUser32(dpi_context_ok=0), FakeShcore()
        with patch.object(ctypes, "windll", windll(user32, shcore), create=True):
            control.Windows()
        self.assertTrue(shcore.calls or user32.of("SetProcessDPIAware"))


class KeysOnWindows(unittest.TestCase):
    def test_extended_flag_for_arrows_and_right_ctrl_not_left_ctrl(self):
        user32 = FakeUser32()
        inj = windows_injector(user32)
        for code in ("ArrowLeft", "ControlRight", "ControlLeft", "AltRight", "NumpadEnter", "Enter"):
            inj.apply({"t": "key", "code": code, "down": True})
            inj.apply({"t": "key", "code": code, "down": False})
        sent = keys_sent(user32)
        self.assertEqual(sent, [
            (0x25, 0x4B, 0x1), (0x25, 0x4B, 0x3),
            (0xA3, 0x1D, 0x1), (0xA3, 0x1D, 0x3),
            (0xA2, 0x1D, 0x0), (0xA2, 0x1D, 0x2),
            (0xA5, 0x38, 0x1), (0xA5, 0x38, 0x3),
            (0x0D, 0x1C, 0x1), (0x0D, 0x1C, 0x3),
            (0x0D, 0x1C, 0x0), (0x0D, 0x1C, 0x2),
        ])

    def test_ending_sends_a_key_up_for_every_key_down_with_the_same_scan_and_extended_flag(self):
        user32 = FakeUser32()
        inj = windows_injector(user32)
        inj.area = {"x": 0, "y": 0, "w": 1920, "h": 1080}
        for code in ("ShiftLeft", "ControlRight", "ArrowDown", "KeyA", "MetaLeft", "Delete"):
            inj.apply({"t": "key", "code": code, "down": True})
        inj.apply({"t": "down", "b": "middle", "x": 0.5, "y": 0.5})
        downs = [k for k in keys_sent(user32) if not k[2] & KEYEVENTF_KEYUP]
        user32.calls.clear()
        inj.release(close=True)
        ups = [k for k in keys_sent(user32) if k[2] & KEYEVENTF_KEYUP]
        for vk, scan, flags in downs:
            with self.subTest(vk=hex(vk)):
                self.assertIn((vk, scan, flags | KEYEVENTF_KEYUP), ups)
        self.assertIn(MIDDLE[1], [c[1] for c in user32.of("mouse_event")])
        self.assertFalse(inj.busy())
        user32.calls.clear()
        with self.assertRaises(Stopped):
            inj.apply({"t": "key", "code": "KeyB", "down": True})
        self.assertEqual(user32.calls, [])  # nothing reaches the desktop after the end

    def test_wheel_down_is_negative_wheel_delta(self):
        user32 = FakeUser32()
        inj = windows_injector(user32)
        inj.apply({"t": "wheel", "dy": 3})
        inj.apply({"t": "wheel", "dy": -1})
        self.assertEqual([c[1:] for c in user32.of("mouse_event")], [(0x800, 0, 0, -360), (0x800, 0, 0, 120)])

    # W1: WindowsKeys presses with keybd_event, which returns nothing. On a locked screen, a
    # UAC prompt or against an elevated window the press is dropped silently: Injector counts
    # the key-up as done (no retry), MAX_FAILS never trips for keyboard input, and recover()
    # always deletes its marker. SendInput returns 0 when the input desktop refuses it.
    def test_a_refused_key_press_is_reported(self):
        user32 = FakeUser32(send_ok=False)
        with patch.object(ctypes, "windll", windll(user32), create=True):
            keys = remote_view.WindowsKeys()
            with self.assertRaises(OSError):
                keys.key("ControlLeft", False)

    # W1: a refused key-up must stay held so a later release retries it
    def test_a_refused_key_up_stays_held_for_the_next_release(self):
        user32 = FakeUser32()
        with patch.object(ctypes, "windll", windll(user32), create=True):
            inj = Injector(control.Windows(), remote_view.WindowsKeys())
            inj.apply({"t": "key", "code": "ControlLeft", "down": True})
            user32.send_ok = False  # the UAC prompt came up while Ctrl was down
            inj.release()
        self.assertIn("ControlLeft", inj.held)

    # W1: recover() drops the marker though Windows refused every key-up
    def test_recover_keeps_the_marker_when_the_desktop_refuses_key_ups(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            marker = Path(tmp) / remote_view.MARKER
            marker.write_text(json.dumps({"session": "abcd1234", "held": ["ControlLeft", "AltLeft"],
                                          "buttons": []}), encoding="utf-8")
            user32 = FakeUser32(send_ok=False)  # locked screen at JARVIS restart
            with patch.object(ctypes, "windll", windll(user32), create=True):
                inj = Injector(control.Windows(), remote_view.WindowsKeys())
                remote_view.recover(tmp, injector=inj)
            self.assertTrue(marker.exists())


class MouseButtonsOnWindows(unittest.TestCase):
    def test_end_releases_every_button_held(self):
        user32 = FakeUser32()
        inj = windows_injector(user32)
        inj.area = {"x": 0, "y": 0, "w": 100, "h": 100}
        for b in ("left", "right", "middle"):
            inj.apply({"t": "down", "b": b, "x": 0.5, "y": 0.5})
        user32.calls.clear()
        inj.release(close=True)
        flags = [c[1] for c in user32.of("mouse_event")]
        for up in (LEFT[1], RIGHT[1], MIDDLE[1]):
            self.assertIn(up, flags)

    # W2: with the node's buttons swapped (left-handed mouse setting), mouse_event's LEFTDOWN is
    # the physical left button, which Windows then treats as secondary: David's primary click
    # (browser button 0, already his primary) arrives as a right-click on the node.
    def test_primary_click_stays_primary_when_the_node_swapped_its_buttons(self):
        user32 = FakeUser32()
        user32.metrics[SM_SWAPBUTTON] = 1
        inj = windows_injector(user32)
        inj.area = {"x": 0, "y": 0, "w": 100, "h": 100}
        inj.apply({"t": "down", "b": "left", "x": 0.5, "y": 0.5})
        self.assertEqual(user32.of("mouse_event")[-1][1], RIGHT[0])


class NodeLanMouseRestore(unittest.TestCase):
    class Client:
        def request(self, *a, **k):
            return {}

    class Pointer:
        def move(self, x, y): pass
        def button(self, which, how): pass
        def scroll(self, d, n): pass
        def type(self, text): pass
        def release(self): pass

    class Keys:
        def key(self, code, down): pass

    def _session(self, tmp):
        return remote_view.NodeSession({}, tmp, "a" * 32, "low", client=self.Client(),
                                       injector=Injector(self.Pointer(), self.Keys()),
                                       capture=lambda: None, encoder=lambda *a: (b"", 1, 1))

    def setUp(self):
        self.addCleanup(remote_view._ACTIVE.clear)
        remote_view._ACTIVE.clear()
        remote_view._LAN.update(owed=False, starting=0)

    def test_restore_after_the_last_session_starts_lan_mouse_once_when_together(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp, \
                patch.object(sharedinput, "start") as start:
            session = self._session(tmp)
            session._lan_mouse_was_running = True
            session._restore_lan_mouse()
            session._restore_lan_mouse()
        self.assertEqual(start.call_count, 1)

    # W5: the node restores Lan Mouse after a session without asking the guard. If the laptops
    # parted during the session (apart_now), Lan Mouse comes back on while apart and stays on
    # until the next guard reading (60 s on Windows). It should leave it off and hand the
    # restart to the guard (stopped_by_guard=True), as ControllerPause does on the main.
    def test_restore_while_apart_leaves_it_to_the_guard(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp, \
                patch.object(sharedinput, "start") as start:
            (Path(tmp) / sharedinput.GUARD_FILE).write_text(json.dumps({"apart_now": True, "apart": 3}))
            session = self._session(tmp)
            session._lan_mouse_was_running = True
            session._restore_lan_mouse()
            memo = json.loads((Path(tmp) / sharedinput.GUARD_FILE).read_text())
        start.assert_not_called()
        self.assertTrue(memo.get("stopped_by_guard"))


if __name__ == "__main__":
    unittest.main()
