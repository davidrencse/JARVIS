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


class SingleInstanceTests(unittest.TestCase):
    def test_second_overlay_is_refused_using_the_thread_safe_last_error(self):
        from unittest import mock
        from jarvis import overlay
        import jarvis.ui  # noqa: F401  imported under os.name "nt", shutil looks for `nt`
        kernel = mock.MagicMock()
        with mock.patch.object(overlay.os, "name", "nt"), \
                mock.patch.object(overlay.ctypes, "WinDLL", create=True, return_value=kernel) as windll, \
                mock.patch.object(overlay.ctypes, "get_last_error", create=True, return_value=183):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                overlay.run({}, "", "")
        windll.assert_called_once_with("kernel32", use_last_error=True)
        kernel.CreateMutexW.assert_called_once()
