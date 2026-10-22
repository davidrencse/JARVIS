import unittest

from jarvis.overlay import parse_hotkey


class HotkeyTests(unittest.TestCase):
    def test_parses_modifiers_and_keys(self):
        self.assertEqual(parse_hotkey("alt+1"), (0x1, 0x31, "Alt+1"))
        self.assertEqual(parse_hotkey("Ctrl + Alt + J"), (0x3, 0x4A, "Ctrl+Alt+J"))
        self.assertEqual(parse_hotkey("win+f9")[1], 0x78)
        self.assertEqual(parse_hotkey("shift+space")[1], 0x20)

    def test_rejects_bare_or_unknown_keys(self):
        for bad in ("1", "alt+", "hyper+1", "alt+enter", ""):
            with self.assertRaises(ValueError):
                parse_hotkey(bad)


if __name__ == "__main__":
    unittest.main()
