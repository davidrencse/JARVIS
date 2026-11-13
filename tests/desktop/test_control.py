import base64
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from jarvis import control
from jarvis.assistant import STOP_CONTROL

_PNG_CACHE = {}


def png(w=2560, h=1440):
    key = (w, h)
    cached = _PNG_CACHE.get(key)
    if cached is None:
        out = io.BytesIO()
        Image.new("RGB", (w, h), (10, 20, 30)).save(out, "PNG")
        cached = _PNG_CACHE[key] = out.getvalue()
    return cached


class Fake:
    """A backend that records calls and touches nothing real."""

    def __init__(self, w=2560, h=1440):
        self.w, self.h = w, h
        self.calls = []
        self.pos = (0.0, 0.0)
        self.fail_on = None

    def shot(self):
        self.calls.append(("shot",))
        return png(self.w, self.h), {"x": 0, "y": 0, "w": self.w, "h": self.h}

    def cursor(self):
        return self.pos

    def move(self, x, y):
        self.calls.append(("move", x, y))
        self.pos = (x, y)

    def button(self, which, how):
        self.calls.append(("button", which, how))
        if self.fail_on == (which, how):
            raise OSError("boom")

    def type(self, text):
        self.calls.append(("type", text))

    def key(self, keys):
        self.calls.append(("key", keys))

    def scroll(self, direction, amount):
        self.calls.append(("scroll", direction, amount))

    def release(self):
        self.calls.append(("release",))

    def names(self):
        return [c[0] for c in self.calls]


class ControlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        png()  # one full-desktop encode, outside each test's clock

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = self._tmp.name
        control._STATE.update(map=None, left_at=None, paused_until=0.0, times=[],
                              announced=time.time(), typed="", shot_time=0)
        self.fake = Fake()
        for target in ("time.sleep",):
            p = mock.patch("jarvis.control." + target)
            p.start()
            self.addCleanup(p.stop)

    def act(self, spec):
        return control.act(spec, state=self.state, impl=self.fake)

    def shot(self):
        ok, text = self.act({"action": "screenshot"})
        self.assertTrue(ok, text)
        return json.loads(text)

    def test_screenshot_json_shrinks_and_sets_map(self):
        data = self.shot()
        self.assertEqual((data["width"], data["height"]), (control.SHOT_WIDTH, 720))
        img = Image.open(io.BytesIO(base64.b64decode(data["b64"])))
        self.assertEqual(img.size, (control.SHOT_WIDTH, 720))
        self.assertEqual(control._STATE["map"]["img_w"], control.SHOT_WIDTH)
        self.assertEqual(control._STATE["map"]["w"], 2560)
        ok, res = control.as_result(True, json.dumps(data))
        self.assertIsInstance(res, control.ToolImage)
        self.assertIsInstance(res, str)
        self.assertEqual(res.b64, data["b64"])
        self.assertEqual(res.mime, data["mime"])

    def test_click_before_screenshot_refused(self):
        ok, text = self.act({"action": "click", "x": 10, "y": 10})
        self.assertFalse(ok)
        self.assertIn("take a screenshot first", text.lower())
        self.assertNotIn(("button", "left", "click"), self.fake.calls)

    def test_coordinate_mapping_and_bounds(self):
        self.shot()
        ok, text = self.act({"action": "click", "x": 640, "y": 360})
        self.assertTrue(ok, text)
        self.assertIn(("move", 1280.0, 720.0), self.fake.calls)
        before = len(self.fake.calls)
        for bad in ({"x": 1281, "y": 10}, {"x": -1, "y": 10}, {"x": 10, "y": 721}):
            ok, text = self.act({"action": "click", **bad})
            self.assertFalse(ok)
            self.assertIn("outside", text)
        self.assertEqual([c for c in self.fake.calls[before:] if c[0] != "release"], [])

    def test_mouse_actions_call_backend_in_order(self):
        self.shot()
        cases = {
            "click": [("move", 200.0, 200.0), ("button", "left", "click")],
            "double_click": [("move", 200.0, 200.0), ("button", "left", "click"),
                             ("button", "left", "click")],
            "right_click": [("move", 200.0, 200.0), ("button", "right", "click")],
            "scroll": [("move", 200.0, 200.0), ("scroll", "up", 5)],
        }
        for action, expected in cases.items():
            self.fake.calls.clear()
            spec = {"action": action, "x": 100, "y": 100}
            if action == "scroll":
                spec.update(direction="up", amount=5)
            ok, text = self.act(spec)
            self.assertTrue(ok, text)
            self.assertEqual(self.fake.calls, expected, action)
        self.fake.calls.clear()
        ok, text = self.act({"action": "drag", "x": 100, "y": 100, "x2": 300, "y2": 200})
        self.assertTrue(ok, text)
        self.assertEqual(self.fake.names(), ["move", "button", "move", "move", "button"])
        self.assertEqual(self.fake.calls[1], ("button", "left", "down"))
        self.assertEqual(self.fake.calls[4], ("button", "left", "up"))
        self.assertEqual(self.fake.calls[3], ("move", 600.0, 400.0))

    def test_type_refuses_card_number(self):
        self.shot(); self.fake.calls.clear()
        ok, text = self.act({"action": "type", "text": "4111 1111 1111 1111"})
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Refused"), text)
        self.assertEqual(self.fake.calls, [])
        ok, text = self.act({"action": "type", "text": "hello world"})
        self.assertTrue(ok, text)
        self.assertEqual(self.fake.calls, [("type", "hello world")])

    def _typing(self, *steps):
        """Run (action, value) steps after a screenshot; returns the list of ok flags."""
        self.shot()
        out = []
        for action, value in steps:
            spec = {"action": action, "text" if action == "type" else "keys": value} \
                if action in ("type", "key") else {"action": action, "x": 10, "y": 10}
            out.append(self.act(spec)[0])
        return out

    def test_split_around_click_refused(self):
        self.assertEqual(self._typing(("type", "rm -rf "), ("click", 0), ("type", "~\n")),
                         [True, True, False])

    def test_click_mid_line_makes_line_unknown(self):
        # audit SU-3: "rm  ~", click between the spaces, "-rf" is really "rm -rf ~"
        res = self._typing(("type", "rm  ~"), ("click", 0), ("type", "-rf"), ("key", "enter"))
        self.assertEqual(res, [True, True, False, False])
        ok, text = self.act({"action": "type", "text": "x"})
        self.assertFalse(ok)
        self.assertIn(control.UNKNOWN_MSG, text)
        for action in ("double_click", "right_click"):
            control._STATE["typed"] = "ls"
            self.assertTrue(self.act({"action": action, "x": 10, "y": 10})[0])
            self.assertIsNone(control._STATE["typed"])
        control._STATE["typed"] = "ls"
        self.assertTrue(self.act({"action": "drag", "x": 10, "y": 10, "x2": 20, "y2": 20})[0])
        self.assertIsNone(control._STATE["typed"])

    def test_click_on_empty_line_keeps_it_known(self):
        self.assertEqual(self._typing(("click", 0), ("type", "ls"), ("key", "enter")),
                         [True, True, True])
        self.assertEqual(self._typing(("click", 0), ("type", "rm -rf ~"), ("key", "enter")),
                         [True, False, True])  # still checked: the refused text was never sent

    def test_split_around_single_key_refused(self):
        self.assertEqual(self._typing(("type", "rm -rf "), ("key", "~"), ("key", "enter")),
                         [True, False, True])  # "~" refused and not sent: the line is still "rm -rf "

    def test_backspace_edit_refused(self):
        self.assertEqual(self._typing(("type", "rm -rf ~/a"), ("key", "backspace"),
                                      ("key", "backspace"), ("key", "enter")),
                         [True, False, False, True])  # "rm -rf ~/" refused; the sent line is "rm -rf ~/a"

    def test_cursor_key_makes_line_unknown(self):
        res = self._typing(("type", "ls"), ("key", "left"), ("type", "x"), ("key", "enter"))
        self.assertEqual(res, [True, True, False, False])
        ok, text = self.act({"action": "type", "text": "y"})
        self.assertIn("ctrl+c", text)
        # ctrl+c, not ctrl+u: the line is unknown here, and ctrl+u keeps text after the cursor
        self.assertEqual(self._typing(("key", "ctrl+c"), ("type", "ls"), ("key", "enter"))[1:], [True, True])

    def test_ctrl_u_after_click_does_not_clear_unknown_line(self):
        # " ~", click (cursor maybe at the start), ctrl+u keeps " ~": "rm -rf" runs "rm -rf ~"
        res = self._typing(("type", " ~"), ("click", 0), ("key", "ctrl+u"), ("type", "rm -rf"),
                           ("key", "enter"))
        self.assertEqual(res, [True, True, True, False, False])
        self.assertIsNone(control._STATE["typed"])
        ok, text = self.act({"action": "key", "keys": "enter"})
        self.assertFalse(ok)
        self.assertIn(control.UNKNOWN_MSG, text)

    def test_ctrl_c_after_click_makes_line_known(self):
        res = self._typing(("type", " ~"), ("click", 0), ("key", "ctrl+c"), ("type", "rm -rf ~"),
                           ("type", "ls"), ("key", "enter"))
        self.assertEqual(res, [True, True, True, False, True, True])  # rm -rf ~ refused by the gate

    def test_ctrl_u_on_known_line_clears_it(self):
        self.assertEqual(self._typing(("type", "rm -rf "), ("key", "ctrl+u"), ("type", "~"),
                                      ("key", "enter")), [True, True, True, True])
        self.assertEqual(control._STATE["typed"], "")

    def test_cursor_keys_then_ctrl_u_stay_unknown(self):
        self.fake = Fake(w=320, h=180)  # the checks are the key gate, not a full-desktop shot
        for key in ("home", "left", "ctrl+a", "ctrl+e", "end"):
            control._STATE["typed"] = ""
            res = self._typing(("type", "x"), ("key", key), ("key", "ctrl+u"), ("type", "ls"))
            self.assertEqual(res, [True, True, True, False], key)
            self.assertIsNone(control._STATE["typed"], key)

    def test_enter_and_ctrl_c_clear_buffer(self):
        self.assertEqual(self._typing(("type", "echo hi"), ("key", "enter"), ("type", "ls ~")),
                         [True, True, True])
        self.assertEqual(self._typing(("type", "rm -rf "), ("key", "ctrl+c"), ("type", "~")), [True, True, True])

    def test_pin_via_keys_refused(self):
        self.assertEqual(self._typing(("key", "1"), ("key", "2"), ("key", "3"), ("key", "4")),
                         [True, True, True, False])

    def test_screenshot_keeps_clock_fresh(self):
        t0 = 1000.0
        with mock.patch("jarvis.control.time.time", return_value=t0):
            self.shot()
            self.assertTrue(self.act({"action": "move", "x": 5, "y": 5})[0])
        with mock.patch("jarvis.control.time.time", return_value=t0 + 100):
            self.shot()
        with mock.patch("jarvis.control.time.time", return_value=t0 + 125):
            ok, text = self.act({"action": "click", "x": 5, "y": 5})
        self.assertTrue(ok, text)

    def test_key_parsing(self):
        self.shot(); self.fake.calls.clear()
        ok, text = self.act({"action": "key", "keys": "ctrl+shift+t"})
        self.assertTrue(ok, text)
        self.assertEqual(self.fake.calls, [("key", "ctrl+shift+t")])
        self.assertEqual(control._parse_keys("ctrl+shift+t"), (["ctrl", "shift"], "t"))
        self.assertEqual(control._parse_keys("super+l"), (["logo"], "l"))
        with self.assertRaises(ValueError):
            control._parse_keys("banana+t")

    def test_takeover_pauses(self):
        self.shot()
        self.assertTrue(self.act({"action": "move", "x": 100, "y": 100})[0])
        self.fake.pos = (self.fake.pos[0] + 50, self.fake.pos[1])  # David moves the mouse
        ok, text = self.act({"action": "screenshot"})  # screenshots still allowed
        self.assertTrue(ok, text)
        ok, text = self.act({"action": "click", "x": 5, "y": 5})
        self.assertFalse(ok)
        self.assertIn("moved the mouse", text)
        ok, text = self.act({"action": "click", "x": 5, "y": 5})
        self.assertFalse(ok)
        self.assertIn("paused", text)

    def test_old_baseline_is_not_takeover(self):
        self.shot()
        self.assertTrue(self.act({"action": "move", "x": 100, "y": 100})[0])
        control._STATE["left_time"] -= control.TAKEOVER_WINDOW + 1
        control._STATE["shot_time"] -= control.TAKEOVER_WINDOW + 1  # minutes later...
        self.fake.pos = (self.fake.pos[0] + 300, self.fake.pos[1])  # ...normal mouse use since
        self.assertTrue(self.act({"action": "screenshot"})[0])
        ok, text = self.act({"action": "click", "x": 5, "y": 5})
        self.assertTrue(ok, text)

    def test_stale_screenshot_refused_after_takeover_window(self):
        self.shot()
        self.assertTrue(self.act({"action": "click", "x": 100, "y": 100})[0])
        control._STATE["left_time"] -= control.TAKEOVER_WINDOW + 1
        control._STATE["shot_time"] -= control.TAKEOVER_WINDOW + 1  # minutes later...
        self.fake.pos = (900.0, 900.0)  # ...David has used the mouse since
        self.fake.calls.clear()
        ok, text = self.act({"action": "click", "x": 50, "y": 50})
        self.assertFalse(ok, text)
        self.assertIn("screenshot first", text)
        self.assertEqual(self.fake.calls, [])  # nothing pressed blind
        self.shot()
        self.assertTrue(self.act({"action": "click", "x": 50, "y": 50})[0])

    def test_split_typing_checked_as_one(self):
        self.shot()
        self.assertTrue(self.act({"action": "type", "text": "rm -rf"})[0])
        ok, text = self.act({"action": "type", "text": " ~"})
        self.assertFalse(ok, text)
        self.assertIn("Refused", text)
        self.assertTrue(self.act({"action": "key", "keys": "space"})[0])
        self.assertFalse(self.act({"action": "type", "text": "~"})[0])  # space keeps the buffer
        self.assertTrue(self.act({"action": "key", "keys": "ctrl+u"})[0])         # line cleared
        self.assertTrue(self.act({"action": "type", "text": "4111 1111 11"})[0])
        self.assertFalse(self.act({"action": "type", "text": "11 1111"})[0])   # split card
        self.assertTrue(self.act({"action": "key", "keys": "enter"})[0])          # fresh field
        self.assertTrue(self.act({"action": "type", "text": "12"})[0])
        self.assertFalse(self.act({"action": "type", "text": "34"})[0])           # split PIN
        self.assertTrue(self.act({"action": "key", "keys": "enter"})[0])
        self.assertTrue(self.act({"action": "type", "text": "hello"})[0])
        self.assertTrue(self.act({"action": "type", "text": " world"})[0])

    def test_small_cursor_jitter_is_not_takeover(self):
        self.shot()
        self.assertTrue(self.act({"action": "move", "x": 100, "y": 100})[0])
        self.fake.pos = (self.fake.pos[0] + 5, self.fake.pos[1])
        self.assertTrue(self.act({"action": "move", "x": 110, "y": 110})[0])

    def test_takeover_needs_fresh_screenshot_even_for_typing(self):
        self.shot()
        self.assertTrue(self.act({"action": "move", "x": 100, "y": 100})[0])
        self.fake.pos = (self.fake.pos[0] + 50, self.fake.pos[1])
        self.assertFalse(self.act({"action": "click", "x": 5, "y": 5})[0])   # took over
        control._STATE["paused_until"] = 0                                    # pause over
        ok, text = self.act({"action": "type", "text": "hi"})
        self.assertFalse(ok)
        self.assertIn("screenshot first", text)

    def test_typing_shell_wipes_and_codes_refused(self):
        self.shot()
        for text in ("rm -rf ~", "bash -c 'rm -rf ~'", "123456", "4821-1194"):
            ok, said = self.act({"action": "type", "text": text})
            self.assertFalse(ok, text)
        self.assertTrue(self.act({"action": "type", "text": "hello from jarvis"})[0])

    def test_overlong_line_fails_closed_not_truncated(self):
        # audit 4: `rm -rf /tmp/q` + 4000 spaces, then `~`, then Enter got past a 4000-char tail.
        self.shot()
        self.assertTrue(self.act({"action": "type", "text": "rm -rf /tmp/q"})[0])
        ok, text = self.act({"action": "type", "text": " " * control.TYPED_MAX})
        self.assertFalse(ok, text)
        self.assertIn("Refused", text)
        self.assertEqual([c[0] for c in self.fake.calls].count("type"), 1)  # only the first piece
        self.assertTrue(self.act({"action": "type", "text": " " * (control.TYPED_MAX - 13)})[0])  # exactly full
        self.assertFalse(self.act({"action": "key", "keys": "space"})[0])
        self.assertFalse(self.act({"action": "type", "text": "~"})[0])
        self.assertTrue(self.act({"action": "key", "keys": "enter"})[0])  # the whole line was checked
        self.assertEqual(control._typed_next("x" * control.TYPED_MAX, "type", {"text": "y"}), None)

    def test_terminal_check_uses_config_and_home(self):
        # audit 4: hard_deny got {} and cwd /tmp, so vault paths were unknown.
        cfg = {"vault": {"path": {"linux": "~/Documents/obsidian-vault"}}}
        self.shot()
        with mock.patch("jarvis.gates.local_config", return_value=cfg):
            for text in ("rm -rf ~/Documents/obsidian-vault", "cat ~/.grok/auth.json"):
                ok, said = self.act({"action": "type", "text": text})
                self.assertFalse(ok, text)
                self.assertIn("Refused", said)
            self.assertTrue(self.act({"action": "type", "text": "ls ~/Documents"})[0])

    def test_stop_file_blocks_until_resume(self):
        with mock.patch.object(control, "_release"):
            control.stop(self.state)
        self.assertTrue((Path(self.state) / control.STOP_FILE).exists())
        ok, text = self.act({"action": "screenshot"})
        self.assertFalse(ok)
        self.assertIn("stopped", text)
        control.resume(self.state)
        self.assertTrue(self.act({"action": "screenshot"})[0])

    def test_max_actions_cap(self):
        self.shot(); control._STATE["times"] = []
        with mock.patch.object(control, "MAX_ACTIONS", 3):
            results = [self.act({"action": "key", "keys": "enter"}) for _ in range(4)]
        self.assertEqual([r[0] for r in results], [True, True, True, False])
        self.assertIn("More than 3", results[3][1])

    def test_failsafe_releases_on_error(self):
        self.shot()
        self.fake.fail_on = ("left", "up")
        ok, text = self.act({"action": "drag", "x": 100, "y": 100, "x2": 300, "y2": 200})
        self.assertFalse(ok)
        self.assertIn("Couldn't drag", text)
        self.assertIn(("release",), self.fake.calls)

    def test_failsafe_releases_on_bad_key(self):
        class BadKey(Fake):
            def key(self, keys):
                control._parse_keys(keys)
        self.fake = BadKey()
        self.shot()
        ok, text = self.act({"action": "key", "keys": "nope+x"})
        self.assertFalse(ok)
        self.assertIn(("release",), self.fake.calls)

    def test_log_written_without_typed_text(self):
        self.shot()
        self.act({"action": "type", "text": "secret-ish words"})
        self.act({"action": "key", "keys": "enter"})
        log = Path(self.state) / "logs" / "control.jsonl"
        raw = log.read_text(encoding="utf-8")
        self.assertNotIn("secret-ish", raw)
        entries = [json.loads(line) for line in raw.splitlines()]
        typed = next(e for e in entries if e["action"] == "type")
        self.assertEqual(typed["chars"], len("secret-ish words"))
        self.assertNotIn("text", typed)
        self.assertTrue(any(e["action"] == "key" and e["keys"] == "enter" for e in entries))

    def test_stop_control_regex(self):
        for yes in ("Jarvis, stop controlling my computer", "hands off",
                    "can you stop controlling my pc please"):
            self.assertTrue(STOP_CONTROL.search(yes), yes)
        for no in ("stop the music", "stop scrolling"):
            self.assertIsNone(STOP_CONTROL.search(no), no)

    def test_desktop_tools_computer_returns_tool_image(self):
        from jarvis.actions import DesktopTools
        shot = json.dumps({"note": "Screenshot 2x2", "mime": "image/jpeg", "b64": "QUJD",
                           "width": 2, "height": 2})
        with mock.patch("jarvis.control.act", return_value=(True, shot)) as act:
            ok, res = DesktopTools().call("computer", {"action": "screenshot"})
        act.assert_called_once_with({"action": "screenshot"})
        self.assertTrue(ok)
        self.assertIsInstance(res, control.ToolImage)
        self.assertEqual((res.b64, res.mime, str(res)), ("QUJD", "image/jpeg", "Screenshot 2x2"))

    def test_vault_mcp_returns_image_content(self):
        from jarvis import vault_mcp
        image = control.ToolImage("Screenshot", "QUJD", "image/png")
        tools = mock.Mock()
        tools.call.return_value = (True, image)
        reply = vault_mcp.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "computer", "arguments": {}}})
        content = reply["result"]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Screenshot"})
        self.assertEqual(content[1], {"type": "image", "data": "QUJD", "mimeType": "image/png"})
        self.assertFalse(reply["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
