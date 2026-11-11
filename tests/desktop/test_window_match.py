"""Phase 5 (2026-11-01): the app a request names, matched to the window it really has.
Seen 2026-10-26: "close calculator" on the Dell found no window, because the Store's
Calculator draws inside ApplicationFrameHost; Discord on the Zenbook is Vesktop."""

import unittest
from unittest import mock

from jarvis import actions
from jarvis.observe import app_match


def window(app, title, wid=1, focused=False):
    return {"id": wid, "app": app, "title": title, "focused": focused}


class MatchTests(unittest.TestCase):
    def test_other_names_match_exactly(self):
        self.assertEqual(app_match(window("ApplicationFrameHost CalculatorApp", "Calculator"), "Calculator"), 3)
        self.assertEqual(app_match(window("WINWORD", "Document1 - Word"), "Word"), 3)
        self.assertEqual(app_match(window("dev.vencord.Vesktop dev.vencord.Vesktop", "(2) Discord"), "discord"), 3)
        self.assertEqual(app_match(window("app.zen_browser.zen", "Task 1 — Zen Browser"), "zen"), 3)
        self.assertEqual(app_match(window("SystemSettings", "Settings"), "settings"), 3)

    def test_partial_names_use_only_what_was_said(self):
        self.assertEqual(app_match(window("google-chrome", "Inbox"), "chrome"), 3)
        self.assertEqual(app_match(window("codex", "codex"), "visual studio code"), 0)
        self.assertEqual(app_match(window("Code", "main.py - Visual Studio Code"), "code"), 3)

    def test_title_only_is_the_weakest(self):
        self.assertEqual(app_match(window("ApplicationFrameHost", "Calculator"), "calculator"), 1)
        self.assertEqual(app_match(window("kitty", "vim"), "calculator"), 0)


class CloseTests(unittest.TestCase):
    def run_close(self, name, windows):
        closed = []
        with mock.patch.object(actions, "open_windows", return_value=windows), \
                mock.patch.object(actions, "_close", side_effect=lambda w: closed.append(w["id"]) or True):
            result = actions.close_app(name)
        return result, closed

    def test_store_calculator_closes(self):
        (ok, _), closed = self.run_close("calculator", [
            window("ApplicationFrameHost CalculatorApp", "Calculator", 7),
            window("zen", "Calculator - Google Search — Zen Browser", 8)])
        self.assertTrue(ok)
        self.assertEqual(closed, [7])  # never the browser tab that mentions it

    def test_word_and_vesktop(self):
        self.assertEqual(self.run_close("word", [window("WINWORD", "Essay - Word", 3)])[1], [3])
        self.assertEqual(self.run_close("discord", [window("dev.vencord.Vesktop", "(9) Discord", 4)])[1], [4])

    def test_code_never_closes_codex(self):
        (ok, text), closed = self.run_close("code", [window("codex", "codex", 5)])
        self.assertFalse(ok)
        self.assertEqual(closed, [])

    def test_vague_title_still_asks(self):
        (ok, text), closed = self.run_close("inbox", [window("zen", "Inbox (3)", 1), window("chrome", "Inbox", 2)])
        self.assertFalse(ok)
        self.assertIn("Which one?", text)
        self.assertEqual(closed, [])


class FocusTests(unittest.TestCase):
    def test_store_app_focus_reads_back(self):
        target = window("ApplicationFrameHost CalculatorApp", "Calculator", 9)
        with mock.patch.object(actions, "open_windows", return_value=[target]), \
                mock.patch.object(actions, "raise_window", return_value=True), \
                mock.patch.object(actions, "_active_window_id", return_value=9):
            self.assertEqual(actions.focus_window("calculator"), (True, "Switched to Calculator."))

    def test_one_of_several_already_in_front_is_done(self):
        listed = [window("kitty", "a", 1, focused=True), window("kitty", "b", 2)]
        with mock.patch.object(actions, "open_windows", return_value=listed):
            ok, text = actions.focus_window("kitty")
        self.assertTrue(ok)
        self.assertIn("already in front", text)

    def test_unknown_focus_state_trusts_the_desktop(self):
        with mock.patch.object(actions, "open_windows", return_value=[window("kitty", "a", 1)]), \
                mock.patch.object(actions, "raise_window", return_value=True), \
                mock.patch.object(actions, "_active_window_id", return_value=None):
            self.assertTrue(actions.focus_window("kitty")[0])


if __name__ == "__main__":
    unittest.main()
