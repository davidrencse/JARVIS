import unittest
from unittest.mock import patch

from jarvis import remote, showcase
from jarvis.showcase import open_page


class OpenPageTest(unittest.TestCase):
    def test_uses_a_running_console_for_supported_page(self):
        with patch.object(showcase, "console_port", return_value=7790), \
                patch.object(showcase, "start_console") as start, \
                patch("jarvis.ui.app_window") as window:
            ok, text = open_page("wiki")
        self.assertTrue(ok)
        start.assert_not_called()
        self.assertEqual(window.call_args.args[0], "http://127.0.0.1:7790/knowledge.html")
        self.assertEqual(text, "Opened Knowledge.")

    def test_starts_a_console_when_none_runs(self):
        with patch.object(showcase, "console_port", return_value=None), \
                patch.object(showcase, "start_console", return_value=7777) as start, \
                patch("jarvis.ui.app_window") as window:
            ok, _ = open_page("system")
        self.assertTrue(ok)
        start.assert_called_once()
        self.assertEqual(window.call_args.args[0], "http://127.0.0.1:7777/design.html")

    def test_removed_markets_page_is_not_supported(self):
        ok, text = open_page("markets")
        self.assertFalse(ok)
        self.assertIn("pages:", text)

    def test_unknown_page(self):
        ok, text = open_page("../secrets")
        self.assertFalse(ok)
        self.assertIn("pages:", text)

    def test_page_action_can_travel_to_other_laptop(self):
        with patch.object(showcase, "open_page", return_value=(True, "Opened.")) as page:
            self.assertEqual(remote.run_here("open_page", "wiki"), (True, "Opened."))
        page.assert_called_once_with("wiki")


if __name__ == "__main__":
    unittest.main()
