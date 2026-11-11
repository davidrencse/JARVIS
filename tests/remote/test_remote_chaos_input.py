"""Chaos tests for remote-control input: clean_event, Sessions.push and Injector.

Seeded random fuzzing (100k+ events per test) against fakes only: no real mouse, keyboard,
screen or Lan Mouse is touched. Real backend classes (control.Hyprland, control.Windows,
remote_view.WindowsKeys/EvdevKeys) run with their subprocess/user32 layers faked.

Tests that expose a still-open bug are marked expectedFailure with the finding id.
"""

import json
import math
import os
import random
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import control, remote_view
from jarvis.remote_view import (ALIASES, BUTTONS, KEYS, Ended, Injector, Sessions, Stopped,
                                clean_event, hyprland_area)

SEED = 20261107
N = 120_000

SCRATCH = Path(os.path.expanduser("~/.cache/jarvis-breakit"))


def _tmpdir():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=SCRATCH)


# -- random events -----------------------------------------------------------------------
_JUNK = [None, True, False, 0, -1, 1, 2, 10 ** 400, -10 ** 400, 1.5, -0.0, float("nan"),
         float("inf"), float("-inf"), 1e308, -1e-308, "", "0.5", "left", [], [0.5], {}, {"x": 1},
         b"KeyA", (1, 2), "x" * 5000, "\x00", "\ud800"]
_CODES = list(KEYS) + list(ALIASES) + ["MediaPlayPause", "AudioVolumeUp", "F24", "Fn", "Lang1",
                                       "IntlRo", "NumpadEqual", "Unidentified", "", "Key A",
                                       "KeyA;rm", "A" * 24, "A" * 25, "Ключ", "KeyΑ",
                                       "__proto__", "constructor"]


def _coord(rng):
    r = rng.random()
    if r < 0.6:
        return rng.random()
    if r < 0.75:
        return rng.choice([0, 1, 0.0, 1.0, 0.9999, 0.99995, 0.99999, -0.05, 1.05, -0.04999, 1.04999])
    if r < 0.85:
        return rng.uniform(-3, 3)
    return rng.choice(_JUNK)


def random_event(rng):
    """A console event: mostly well-formed, often subtly broken, sometimes garbage."""
    r = rng.random()
    if r < 0.03:
        return rng.choice(_JUNK + [[{"t": "move"}], "move", 42])
    kind = rng.choice(["move", "move", "down", "up", "wheel", "key", "key", "key", "text",
                       "release", "MOVE", "click", None, 7, "", "keydown"])
    ev = {"t": kind}
    if kind in ("move", "down", "up") or rng.random() < 0.1:
        if rng.random() < 0.95:
            ev["x"] = _coord(rng)
        if rng.random() < 0.95:
            ev["y"] = _coord(rng)
    if kind in ("down", "up") or rng.random() < 0.05:
        ev["b"] = rng.choice(list(BUTTONS) * 6 + ["Left", "back", "", None, 0, ["left"]])
    if kind == "wheel" or rng.random() < 0.05:
        ev["dy"] = rng.choice([rng.randint(-25, 25), rng.randint(-25, 25), rng.choice(_JUNK)])
    if kind == "key" or rng.random() < 0.05:
        ev["code"] = rng.choice(_CODES * 3 + _JUNK)
        ev["down"] = rng.choice([True, False, True, False, 1, 0, "true", None])
    if kind == "text" or rng.random() < 0.05:
        ev["text"] = rng.choice(["a", "hello world", "x" * 1000, "x" * 1001, "", 5, None,
                                 "line\nbreak", "é中"])
    if rng.random() < 0.05:
        ev[rng.choice(["extra", "__class__", "t2"])] = rng.choice(_JUNK)
    return ev


def _check_clean(test, out):
    """The shape every cleaned event must have (the node trusts nothing else)."""
    test.assertIsInstance(out, dict)
    t = out["t"]
    if t == "move":
        test.assertEqual(set(out), {"t", "x", "y"})
    elif t in ("down", "up"):
        test.assertIn(out["b"], BUTTONS)
        test.assertIn(set(out), ({"t", "b"}, {"t", "b", "x", "y"}))
    elif t == "wheel":
        test.assertEqual(set(out), {"t", "dy"})
        test.assertIs(type(out["dy"]), int)
        test.assertTrue(-20 <= out["dy"] <= 20 and out["dy"] != 0)
    elif t == "key":
        test.assertEqual(set(out), {"t", "code", "down"})
        test.assertIsInstance(out["code"], str)
        test.assertRegex(out["code"], r"\A[A-Za-z0-9]{1,24}\Z")
        test.assertIs(type(out["down"]), bool)
    elif t == "text":
        test.assertEqual(set(out), {"t", "text"})
        test.assertTrue(0 < len(out["text"]) <= 1000)
    elif t == "release":
        test.assertEqual(out, {"t": "release"})
    else:
        test.fail(f"unknown cleaned kind {t!r}")
    for k in ("x", "y"):
        if k in out:
            test.assertIs(type(out[k]), float)
            test.assertFalse(math.isnan(out[k]))
            test.assertTrue(0.0 <= out[k] <= 1.0, out)


class CleanEventFuzz(unittest.TestCase):
    def test_random_events_are_either_clean_or_a_ValueError(self):
        rng = random.Random(SEED)
        ok = bad = 0
        for i in range(N):
            ev = random_event(rng)
            try:
                out = clean_event(ev)
            except ValueError:
                bad += 1
                continue
            except Exception as exc:  # pragma: no cover - this is the failure
                self.fail(f"event #{i} {ev!r} raised {type(exc).__name__}: {exc}")
            ok += 1
            _check_clean(self, out)
            self.assertEqual(clean_event(out), out)  # idempotent: the node cleans again
        self.assertGreater(ok, N // 5)
        self.assertGreater(bad, N // 10)

    def test_clean_event_never_aliases_the_input(self):
        ev = {"t": "key", "code": "KeyA", "down": True, "extra": [1]}
        out = clean_event(ev)
        ev["code"] = "Delete"
        self.assertEqual(out["code"], "KeyA")

    def test_bool_and_out_of_range_numbers_rejected(self):
        for bad in ({"t": "move", "x": True, "y": 0.5}, {"t": "move", "x": float("nan"), "y": 0},
                    {"t": "move", "x": 10 ** 400, "y": 0}, {"t": "move", "x": 0, "y": -1},
                    {"t": "wheel", "dy": True}, {"t": "wheel", "dy": 0}, {"t": "wheel", "dy": 21},
                    {"t": "wheel", "dy": 1.0}, {"t": "key", "code": "KeyA", "down": 1},
                    {"t": "down", "b": "left", "x": 0.5}, {"t": "text", "text": "x" * 1001}):
            with self.assertRaises(ValueError, msg=bad):
                clean_event(bad)

    def test_text_that_no_backend_can_type_is_rejected(self):
        # NUL makes subprocess (wtype) raise ValueError; a lone surrogate can't be encoded as
        # UTF-16 (Windows SendInput) nor as valid UTF-8 argv. The hub should drop them.
        for text in ("a\x00b", "\ud800", "x\udc80y"):
            with self.assertRaises(ValueError, msg=repr(text)):
                clean_event({"t": "text", "text": text})


class PushFuzz(unittest.TestCase):
    def test_random_batches_only_raise_ValueError_or_Ended_and_queue_only_clean_events(self):
        rng = random.Random(SEED + 1)
        clock = [1000.0]
        s = Sessions(clock=lambda: clock[0])
        sid = s.open("node-a", "medium")["id"]
        total = 0
        while total < N:
            n = rng.choice([0, 1, 5, 50, 200, 201])
            batch = [random_event(rng) for _ in range(n)]
            if rng.random() < 0.02:
                batch = rng.choice([None, {}, "events", 5, {"t": "move"}])
            total += n
            try:
                queued = s.push(sid, batch)
            except ValueError:
                continue
            except Ended:  # pragma: no cover
                self.fail("session ended under fuzz")
            self.assertLessEqual(queued, len(batch))
            queue = s._all[sid]["events"]
            self.assertLessEqual(len(queue), remote_view.MAX_QUEUE + remote_view.MAX_EVENTS)
            nums = [n for n, _ in queue]
            self.assertEqual(nums, sorted(nums))
            self.assertEqual(len(nums), len(set(nums)))
            for _, ev in queue[-50:]:
                _check_clean(self, ev)
            # node keeps up now and then
            if rng.random() < 0.3 and queue:
                got = s.take_input("node-a", sid, after=queue[0][0] - 1, hold=0)
                for _, ev in got["events"]:
                    _check_clean(self, ev)
                s.take_input("node-a", sid, after=got["events"][-1][0], hold=0)
            clock[0] += 0.01

    def test_overflow_keeps_a_release_so_held_keys_still_come_up(self):
        s = Sessions(clock=lambda: 1000.0)
        sid = s.open("node-a")["id"]
        s.push(sid, [{"t": "key", "code": "ShiftLeft", "down": True}])
        for _ in range(remote_view.MAX_QUEUE // remote_view.MAX_EVENTS + 2):
            s.push(sid, [{"t": "key", "code": "KeyA", "down": True}] * remote_view.MAX_EVENTS)
        kinds = [e["t"] for _, e in s._all[sid]["events"]]
        self.assertIn("release", kinds)
        # Applying whatever is queued to a fresh Injector ends with nothing held.
        inj, keys = Injector(PhysPointer(), PhysKeys()), None
        keys = inj.keys
        for ev in (e for _, e in s._all[sid]["events"]):
            inj.apply(ev)
        last_release = max(i for i, k in enumerate(kinds) if k == "release")
        if all(k != "key" for k in kinds[last_release + 1:]):
            self.assertEqual(keys.pressed, set())


# -- physical-state fakes ----------------------------------------------------------------
class Flake:
    def __init__(self, rng=None, p=0.0):
        self.rng, self.p = rng, p

    def hit(self):
        return self.rng is not None and self.p and self.rng.random() < self.p


class PhysKeys:
    """Tracks which keys are physically down. Failures can land before or after the effect."""

    def __init__(self, flake=None):
        self.pressed, self.calls, self.flake = set(), 0, flake or Flake()

    def key(self, code, down):
        assert code in KEYS, code  # Injector must only send codes both backends know
        self.calls += 1
        before = self.flake.hit()
        if before and self.flake.rng.random() < 0.5:
            raise OSError("ydotool timed out (before)")
        (self.pressed.add if down else self.pressed.discard)(code)
        if before:
            raise OSError("ydotool timed out (after)")


class PhysPointer:
    def __init__(self, flake=None, area=None):
        self.pressed, self.calls, self.flake, self.moves = set(), 0, flake or Flake(), []

    def _maybe(self):
        if self.flake.hit():
            raise OSError("pointer backend failed")

    def move(self, x, y):
        self.calls += 1
        self._maybe()
        self.moves.append((x, y))

    def button(self, which, how):
        self.calls += 1
        if which not in BUTTONS or how not in ("down", "up", "click"):
            raise KeyError(which)
        fail = self.flake.hit()
        if fail and self.flake.rng.random() < 0.5:
            raise OSError("click failed (before)")
        if how == "down":
            self.pressed.add(which)
        elif how == "up":
            self.pressed.discard(which)
        if fail:
            raise OSError("click failed (after)")

    def scroll(self, direction, amount):
        self.calls += 1
        assert direction in ("up", "down") and isinstance(amount, int) and 1 <= amount <= 20
        self._maybe()

    def type(self, text):
        self.calls += 1
        self._maybe()

    def release(self):  # like control.Hyprland.release: left and right only
        self.calls += 1
        self.pressed.discard("left")
        self.pressed.discard("right")


class InjectorFuzz(unittest.TestCase):
    def setUp(self):
        p = patch("jarvis.remote_view.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, seed, p_fail, sequences=400, per=1000):
        rng = random.Random(seed)
        total = 0
        for _ in range(sequences):
            flake = Flake(random.Random(rng.random()), p_fail)
            keys, pointer = PhysKeys(flake), PhysPointer(flake)
            inj = Injector(pointer, keys)
            inj.area = {"x": rng.choice([0, -1920, 1920]), "y": rng.choice([0, -1080]),
                        "w": rng.choice([1920, 2048, 1706.6666666666667]), "h": 1080}
            for _ in range(per):
                try:
                    ev = clean_event(random_event(rng))
                except ValueError:
                    continue
                total += 1
                try:
                    inj.apply(ev)
                except OSError:
                    pass  # backend failure: the node lets go and goes on
                # Invariant: Injector's idea of held is a superset of what's physically down
                # (a key or button down is tracked before it's pressed).
                self.assertLessEqual(keys.pressed, inj.held, ev)
                self.assertLessEqual(pointer.pressed, inj.buttons, ev)
            flake.p = 0.0  # backends healthy again: the final release must clear everything
            inj.release(close=True)
            self.assertEqual(keys.pressed, set())
            self.assertEqual(pointer.pressed, set())
            self.assertFalse(inj.busy())
            before = (keys.calls, pointer.calls)
            for _ in range(5):
                ev = clean_event({"t": "key", "code": "KeyA", "down": True})
                with self.assertRaises(Stopped):
                    inj.apply(ev)
            self.assertEqual((keys.calls, pointer.calls), before)  # nothing after close
        return total

    def test_healthy_backends_never_leave_anything_held(self):
        self.assertGreater(self._run(SEED + 2, 0.0), 100_000)

    def test_flaky_backends_never_leave_anything_held_once_they_recover(self):
        self.assertGreater(self._run(SEED + 3, 0.15), 100_000)

    def test_transient_failure_during_final_release_is_retried(self):
        keys = PhysKeys()
        inj = Injector(PhysPointer(), keys)
        for code in ("ShiftLeft", "ControlLeft", "KeyA", "MetaLeft"):
            inj.apply({"t": "key", "code": code, "down": True})
        inj.apply({"t": "down", "b": "middle"})
        fails = {"n": 4}
        real = keys.key

        def flaky(code, down):
            if fails["n"] > 0:
                fails["n"] -= 1
                raise OSError("busy")
            real(code, down)
        keys.key = flaky
        inj.release(close=True)
        self.assertEqual(keys.pressed, set())
        self.assertEqual(inj.pointer.pressed, set())

    def test_concurrent_presses_and_final_release(self):
        rng = random.Random(SEED + 4)
        for _ in range(60):
            keys, pointer = PhysKeys(), PhysPointer()
            inj = Injector(pointer, keys)
            events = []
            while len(events) < 400:
                try:
                    events.append(clean_event(random_event(rng)))
                except ValueError:
                    pass
            go = threading.Event()

            def presser():
                go.wait(2)
                for ev in events:
                    try:
                        inj.apply(ev)
                    except Stopped:
                        return
            threads = [threading.Thread(target=presser) for _ in range(3)]
            for t in threads:
                t.start()
            go.set()
            inj.release(close=True)
            for t in threads:
                t.join(5)
            self.assertEqual(keys.pressed, set())
            self.assertEqual(pointer.pressed, set())

    def test_alias_codes_track_as_one_key(self):
        keys = PhysKeys()
        inj = Injector(PhysPointer(), keys)
        inj.apply(clean_event({"t": "key", "code": "OSLeft", "down": True}))
        self.assertEqual(keys.pressed, {"MetaLeft"})
        inj.apply(clean_event({"t": "key", "code": "MetaLeft", "down": False}))
        self.assertEqual((keys.pressed, inj.held), (set(), set()))
        inj.apply(clean_event({"t": "key", "code": "MetaRight", "down": True}))
        inj.apply(clean_event({"t": "key", "code": "OSRight", "down": False}))
        self.assertEqual((keys.pressed, inj.held), (set(), set()))

    def test_unknown_codes_never_reach_a_backend(self):
        keys = PhysKeys()
        inj = Injector(PhysPointer(), keys)
        for code in ("MediaPlayPause", "F24", "Fn", "Lang1", "NumpadEqual", "Unidentified"):
            inj.apply(clean_event({"t": "key", "code": code, "down": True}))
        self.assertEqual((keys.calls, inj.held), (0, set()))


# -- coordinate mapping through the real backends' move() ------------------------------
class _HyprRec(control.Hyprland):
    def __init__(self):
        self.points = []

    def _run(self, *args, data=None):
        cmd = " ".join(args)
        m = re.search(r"x = (-?\d+), y = (-?\d+)", cmd)
        if m:
            self.points.append((int(m.group(1)), int(m.group(2))))

        class R:
            stdout = b""
        return R()

    def button(self, which, how):
        pass


class _User32:
    def __init__(self):
        self.points, self.keys = [], []

    def SetCursorPos(self, x, y):
        assert isinstance(x, int) and isinstance(y, int)
        self.points.append((x, y))
        return 1

    def mouse_event(self, *a):
        pass

    def MapVirtualKeyW(self, vk, kind):
        return (vk * 7) & 0xFF

    def keybd_event(self, vk, scan, flags, extra):
        self.keys.append((vk, scan, flags))

    def SendInput(self, count, inputs, size):  # WindowsKeys sends one keyboard INPUT per key
        if inputs[0].type == 1:
            self.keys.append((inputs[0].ki.wVk, inputs[0].ki.wScan, inputs[0].ki.dwFlags))
        return count


def _windows_pointer():
    w = object.__new__(control.Windows)
    w.user32 = _User32()
    return w


def _monitors(rng):
    """A Hyprland layout row: monitors side by side (some with negative origins), each with
    a pixel size, a fractional scale and maybe a rotation."""
    out, x = [], rng.choice([0, -2560, -1920])
    y = rng.choice([0, -1080, 0, 1080])
    for _ in range(rng.randint(1, 3)):
        mon = {"name": "DP", "x": x, "y": y, "width": rng.choice([1280, 1366, 1600, 1920, 2560, 3840]),
               "height": rng.choice([720, 768, 1080, 1440, 1600, 2160]),
               "scale": rng.choice([1, 1, 1.25, 1.5, 1.6, 2, 4 / 3]),
               "transform": rng.choice([0, 0, 0, 1, 3])}
        a = hyprland_area(mon)
        out.append(a)
        x += math.ceil(a["w"])
    return out


def _console_point(rng):
    """What remote.js sends: norm() inside the letterboxed picture, r5-rounded, clamped to
    [0, 1] while dragging past an edge."""
    r = rng.random()
    if r < 0.3:
        return rng.choice([0.0, 1.0, 0.99999, 0.99995, 0.9999, 0.9995])
    v = rng.random()
    return round(v * 1e5) / 1e5


def _inside(test, pt, a, ctx):
    x, y = pt
    test.assertTrue(a["x"] <= x < a["x"] + a["w"] and a["y"] <= y < a["y"] + a["h"],
                    f"{pt} outside area {a} for {ctx}")


class CoordinateMapping(unittest.TestCase):
    def test_float_mapping_stays_inside_the_area(self):
        rng = random.Random(SEED + 5)
        for _ in range(20_000):
            for a in _monitors(rng):
                rec = PhysPointer()
                inj = Injector(rec, PhysKeys())
                inj.area = a
                p = {"t": "move", "x": _console_point(rng), "y": _console_point(rng)}
                inj.apply(clean_event(p))
                _inside(self, rec.moves[-1], a, p)

    def test_hyprland_move_lands_on_the_target_monitor(self):
        rng = random.Random(SEED + 6)
        for _ in range(20_000):
            for a in _monitors(rng):
                hy = _HyprRec()
                inj = Injector(hy, PhysKeys())
                inj.area = a
                p = {"t": "move", "x": _console_point(rng), "y": _console_point(rng)}
                inj.apply(clean_event(p))
                _inside(self, hy.points[-1], a, p)

    def test_windows_move_lands_inside_the_virtual_screen(self):
        rng = random.Random(SEED + 7)
        for _ in range(50_000):
            w = _windows_pointer()
            inj = Injector(w, PhysKeys())
            inj.area = {"x": rng.choice([0, -1920, -2560, -3840]), "y": rng.choice([0, -1080, -360]),
                        "w": rng.choice([1920, 3840, 4480, 5760, 7680]),
                        "h": rng.choice([1080, 1440, 2160])}
            p = {"t": "move", "x": _console_point(rng), "y": _console_point(rng)}
            inj.apply(clean_event(p))
            _inside(self, w.user32.points[-1], inj.area, p)

    def test_edge_case_minimal_repro_for_input_1(self):
        """The exact value INPUT-1 got wrong: x = y = 1.0 used to dispatch (1920, 1080)."""
        hy = _HyprRec()
        inj = Injector(hy, PhysKeys())
        inj.area = {"x": 0, "y": 0, "w": 1920, "h": 1080}  # a second monitor starts at x=1920
        inj.apply(clean_event({"t": "move", "x": 1.0, "y": 1.0}))
        self.assertEqual(hy.points[-1], (1919, 1079))  # the last pixel, not the next monitor's first

    def test_down_and_up_without_a_point_press_where_the_pointer_is(self):
        rec = PhysPointer()
        inj = Injector(rec, PhysKeys())
        inj.area = {"x": 0, "y": 0, "w": 100, "h": 100}
        inj.apply(clean_event({"t": "down", "b": "left"}))
        inj.apply(clean_event({"t": "up", "b": "left"}))
        self.assertEqual(rec.moves, [])

    def test_no_area_yet_never_moves(self):
        rec = PhysPointer()
        inj = Injector(rec, PhysKeys())
        inj.apply(clean_event({"t": "move", "x": 0.5, "y": 0.5}))
        self.assertEqual(rec.moves, [])


# -- key codes in both backends ----------------------------------------------------------
class KeyCodeConsistency(unittest.TestCase):
    def test_table_is_unique_and_in_range_for_both_backends(self):
        evdev = [v[2] for v in KEYS.values()]
        self.assertEqual(len(evdev), len(set(evdev)), "two codes share an evdev key")
        win = [(v[0], v[1]) for v in KEYS.values()]
        self.assertEqual(len(win), len(set(win)), "two codes share a Windows key")
        for code, (vk, ext, ev) in KEYS.items():
            self.assertRegex(code, r"\A[A-Za-z0-9]{1,24}\Z")  # clean_event accepts every one
            self.assertTrue(0 < vk < 0xFF, code)
            self.assertIs(type(ext), bool)
            self.assertTrue(0 < ev < 0x2FF, code)
        for alias, target in ALIASES.items():
            self.assertIn(target, KEYS)
            self.assertRegex(alias, r"\A[A-Za-z0-9]{1,24}\Z")

    def test_well_known_codes_match_the_linux_and_windows_tables(self):
        # input-event-codes.h and WinUser.h, spot checked
        expect = {"KeyA": (0x41, 30), "KeyZ": (0x5A, 44), "Digit0": (0x30, 11), "Digit1": (0x31, 2),
                  "Enter": (0x0D, 28), "Escape": (0x1B, 1), "Space": (0x20, 57), "F1": (0x70, 59),
                  "F10": (0x79, 68), "F11": (0x7A, 87), "F12": (0x7B, 88), "ShiftLeft": (0xA0, 42),
                  "ControlRight": (0xA3, 97), "AltRight": (0xA5, 100), "MetaLeft": (0x5B, 125),
                  "ArrowUp": (0x26, 103), "Delete": (0x2E, 111), "Numpad0": (0x60, 82),
                  "Numpad7": (0x67, 71), "NumpadDivide": (0x6F, 98), "Backquote": (0xC0, 41),
                  "IntlBackslash": (0xE2, 86)}
        for code, (vk, ev) in expect.items():
            self.assertEqual((KEYS[code][0], KEYS[code][2]), (vk, ev), code)

    def test_both_backends_send_matching_down_up_for_every_code(self):
        rng = random.Random(SEED + 8)
        runs = []
        with patch("subprocess.run", lambda args, **kw: runs.append(args)):
            evk = remote_view.EvdevKeys()
            wk = object.__new__(remote_view.WindowsKeys)
            wk.user32 = _User32()
            codes = list(KEYS) + list(ALIASES)
            for _ in range(N):
                code, down = rng.choice(codes), rng.random() < 0.5
                evk.key(code, down)
                wk.key(code, down)
                vk, ext, ev = KEYS[ALIASES.get(code, code)]
                self.assertEqual(runs[-1], ["ydotool", "key", f"{ev}:{1 if down else 0}"])
                wvk, scan, flags = wk.user32.keys[-1]
                self.assertEqual(wvk, vk)
                self.assertEqual(bool(flags & 0x1), ext, code)
                self.assertEqual(bool(flags & 0x2), not down, code)
                self.assertEqual(flags & ~0x3, 0)


# -- recovery from a marker a dead session left ------------------------------------------
class RecoverChaos(unittest.TestCase):
    def setUp(self):
        p = patch("jarvis.remote_view.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def _recover(self, payload):
        with _tmpdir() as tmp:
            (Path(tmp) / remote_view.MARKER).write_text(payload, encoding="utf-8")
            keys, pointer = PhysKeys(), PhysPointer()
            result = remote_view.recover(tmp, injector=Injector(pointer, keys))
            return result, (Path(tmp) / remote_view.MARKER).exists(), keys, pointer

    def test_garbage_markers_never_raise(self):
        for payload in ("", "null", "[]", "5", "{", '{"held": null}', '{"held": "KeyA"}',
                        '{"held": [1, null, "KeyA"]}', '{"buttons": [null]}', "\x00\xff"):
            result, kept, _, _ = self._recover(payload)
            self.assertTrue(result, payload)
            self.assertFalse(kept, payload)

    def test_non_list_held_does_not_raise(self):
        for payload in ('{"held": 5}', '{"buttons": 7}', '{"held": true}'):
            result, kept, _, _ = self._recover(payload)
            self.assertTrue(result, payload)
            self.assertFalse(kept, payload)

    def test_unknown_button_in_marker_does_not_keep_it_forever(self):
        result, kept, _, pointer = self._recover(json.dumps({"held": [], "buttons": ["back"]}))
        self.assertTrue(result)
        self.assertFalse(kept)  # every later start retries it, never clears it


if __name__ == "__main__":
    unittest.main()
