"""vault_answer opens no second memory database on a miss, and a blocked reply
survives an empty args map."""

import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import pipeline
from jarvis.assistant import vault_answer


class VaultAnswerMemoryTests(unittest.TestCase):
    def test_non_briefing_text_never_opens_memory(self):
        with patch("jarvis.memory.Memory") as memory, \
                patch("jarvis.vault.vault_for", return_value=object()) as vault:
            self.assertIsNone(vault_answer("tell me a joke", {}, "/tmp/state"))
        memory.assert_not_called()
        vault.assert_not_called()

    def test_caller_memory_opens_no_new_database(self):
        given = object()
        with patch("jarvis.memory.Memory") as memory, \
                patch("jarvis.vault.vault_for", return_value=object()), \
                patch("jarvis.tasks.tasks_for", return_value=object()), \
                patch("jarvis.briefing.answer", return_value="Good morning.") as answer:
            got = vault_answer("brief me", {}, "/tmp/state", memory=given)
        self.assertEqual(got, "Good morning.")
        memory.assert_not_called()
        self.assertIs(answer.call_args.kwargs["memory"], given)

    def test_recognised_question_opens_memory_once_when_none_was_passed(self):
        state = "/tmp/state"
        with patch("jarvis.memory.Memory") as memory, \
                patch("jarvis.vault.vault_for", return_value=object()), \
                patch("jarvis.tasks.tasks_for", return_value=object()), \
                patch("jarvis.briefing.answer", return_value="Nothing is overdue.") as answer:
            got = vault_answer("what\u2019s due", {}, state)
        self.assertEqual(got, "Nothing is overdue.")
        memory.assert_called_once_with(Path(state))
        self.assertIs(answer.call_args.kwargs["memory"], memory.return_value)

    def test_no_vault_or_a_failure_returns_none(self):
        with patch("jarvis.memory.Memory") as memory, \
                patch("jarvis.vault.vault_for", return_value=None):
            self.assertIsNone(vault_answer("brief me", {}, "/tmp/state"))
        memory.assert_not_called()
        with patch("jarvis.vault.vault_for", side_effect=OSError("disk")), \
                patch("jarvis.memory.Memory") as memory:
            self.assertIsNone(vault_answer("brief me", {}, "/tmp/state"))
        memory.assert_not_called()

    def test_memory_open_failure_still_answers(self):
        with patch("jarvis.memory.Memory", side_effect=OSError("locked")), \
                patch("jarvis.vault.vault_for", return_value=object()), \
                patch("jarvis.tasks.tasks_for", return_value=object()), \
                patch("jarvis.briefing.answer", return_value="Good morning.") as answer:
            self.assertEqual(vault_answer("brief me", {}, "/tmp/state"), "Good morning.")
        self.assertIsNone(answer.call_args.kwargs["memory"])


class BlockedReplyTests(unittest.TestCase):
    def test_empty_args_confirm_omits_the_value(self):
        reply = pipeline._blocked_reply(
            {"intent": "open_app", "args": {}, "device": "dell"},
            {"risk": "confirm", "why": "needs a yes"})
        self.assertEqual(
            reply,
            "I need your OK to open app on dell, and I can't ask for "
            "confirmations yet, so I didn't do it.")

    def test_empty_args_other_risk_returns_the_reason(self):
        reply = pipeline._blocked_reply(
            {"intent": "open_app", "args": {}, "device": "dell"},
            {"risk": "deny", "why": "That app isn't installed."})
        self.assertEqual(reply, "That app isn't installed.")

    def test_missing_and_non_dict_args_omit_the_value(self):
        commands = (
            {"intent": "lock_screen", "device": "zenbook"},
            {"intent": "lock_screen", "args": None, "device": "zenbook"},
            {"intent": "lock_screen", "args": ["nope"], "device": "zenbook"},
        )
        for command in commands:
            reply = pipeline._blocked_reply(command, {"risk": "confirm", "why": "no"})
            self.assertIn("to lock screen on zenbook", reply)

    def test_present_arg_stays_in_the_confirm_sentence(self):
        reply = pipeline._blocked_reply(
            {"intent": "close_app", "args": {"name": "Photoshop"}, "device": "node"},
            {"risk": "confirm", "why": "needs a yes"})
        self.assertEqual(
            reply,
            "I need your OK to close app Photoshop on node, and I can't ask for "
            "confirmations yet, so I didn't do it.")
