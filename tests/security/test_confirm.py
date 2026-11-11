import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import assistant, gates, pipeline
from jarvis.confirm import ConfirmQueue
from jarvis.history import History
from jarvis.topology import ORIGIN


CONFIG = {"devices": {"dell": {}, "zenbook": {}}, "cluster": {"enabled": False}}
COMMAND = {"id": "c1", "intent": "close_app", "device": "dell",
           "args": {"name": "Word"}, "depends_on": []}


class FakeRemote:
    def __init__(self):
        self.calls = []

    def here(self):
        return "dell"

    def call(self, device, action, arg):
        self.calls.append((device, action, arg))
        return (True, json.dumps({"match": True, "after_ms": 1}) if action == "observe" else "Closed Word.")


class FakeMemory:
    def __init__(self, path):
        self.path, self.items = path, []

    def add(self, *args, **kwargs):
        self.items.append(args)
        return {"id": str(len(self.items))}

    def context(self, text):
        return []


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.q = ConfirmQueue(self.state)
        self.memory = FakeMemory(self.state / "memory.db")
        token = ORIGIN.set("dell")
        self.addCleanup(ORIGIN.reset, token)

    def test_persist_origin_hash_and_single_use(self):
        item = self.q.park(COMMAND, "dell", "console")
        self.assertEqual(item["id"], "C1")
        self.assertEqual(item["summary"], "close Word on the dell")
        self.assertEqual(item["expires"] - item["created"], 600)
        with self.assertRaises(ValueError):
            self.q.approve("C1", "zenbook")
        self.assertEqual(ConfirmQueue(self.state).approve(None, "dell", item["hash"]), COMMAND)
        with self.assertRaises(ValueError):
            self.q.approve("C1", "dell")
        with History(self.state).connect() as db:
            self.assertEqual(db.execute("SELECT action FROM confirmation_audit").fetchone()[0], "approve")

    def test_ambiguity_and_rejection_audit(self):
        self.q.park(COMMAND, "dell", "cli")
        self.q.park(COMMAND, "dell", "cli")
        with self.assertRaisesRegex(ValueError, "Which one.*C1 or C2"):
            self.q.approve(None, "dell")
        self.q.reject("C2")
        self.assertEqual(len(self.q.pending("dell")), 1)
        with History(self.state).connect() as db:
            self.assertEqual(db.execute("SELECT action FROM confirmation_audit").fetchone()[0], "reject")

    def test_voice_expiry(self):
        with patch("jarvis.confirm.time.time", return_value=100):
            item = self.q.park(COMMAND, "dell", "voice")
        self.assertEqual(item["expires"], 160)
        with patch("jarvis.confirm.time.time", return_value=160):
            with self.assertRaises(ValueError):
                self.q.approve("C1", "dell")
            self.assertEqual(self.q.pending(), [])

    def test_command_tamper_and_stale_ui_hash(self):
        item = self.q.park(COMMAND, "dell", "cli")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.q.approve("C1", "dell", "bad")
        with self.q.history.connect() as db:
            db.execute("UPDATE confirmations SET command=?", (json.dumps({**COMMAND, "device": "zenbook"}),))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.q.approve("C1", "dell", item["hash"])

    def test_concurrent_approvals_execute_once(self):
        self.q.park(COMMAND, "dell", "cli")
        def approve():
            try:
                return bool(self.q.approve("C1", "dell"))
            except ValueError:
                return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(lambda _: approve(), range(2))), 1)

    def test_fast_path_parks_then_runs_with_normal_gates(self):
        remote = FakeRemote()
        with patch.object(pipeline, "INFLIGHT", gates.InFlight()):
            result = pipeline.fast_path("close word", CONFIG, self.state, remote=remote,
                                        app_exists=lambda d, n: True)
            self.assertIn("Confirm (C1)", result["reply"])
            self.assertFalse(remote.calls)
            command = self.q.approve(None, "dell")
            result = pipeline.run_confirmed(command, CONFIG, self.state, remote=remote,
                                             app_exists=lambda d, n: True)
        self.assertEqual(result["outcomes"][0]["state"], "observed")
        self.assertEqual(remote.calls[0][1], "close_app")
        self.assertEqual(len(History(self.state).recent()), 2)

    def test_confirmation_does_not_skip_target_or_loop_checks(self):
        for kwargs in ({"app_exists": lambda d, n: False}, {"origin_class": "agent"}):
            remote = FakeRemote()
            result = pipeline.run_confirmed(COMMAND, CONFIG, self.state, remote=remote, **kwargs)
            self.assertFalse(remote.calls)
            self.assertEqual(result["outcomes"][0]["state"], "not_sent")

    def test_spoken_confirmation_and_other_origin(self):
        self.q.park(COMMAND, "dell", "voice")
        result = {"reply": "done", "commands": [COMMAND], "outcomes": []}
        with patch("jarvis.pipeline.run_confirmed", return_value=result) as run:
            ORIGIN.set("zenbook")
            self.assertIsNone(assistant._confirmation_turn("yes", self.memory, CONFIG, self.state,
                                                          "voice", None, None, None))
            run.assert_not_called()
            ORIGIN.set("dell")
            done = assistant.ask("yes", None, self.memory, CONFIG, self.state, channel="voice")
            self.assertEqual(done["lane"], "confirmation")
            run.assert_called_once()

    def test_number_words_and_rejections(self):
        for _ in range(7):
            self.q.park(COMMAND, "dell", "cli")
        with patch("jarvis.pipeline.run_confirmed", return_value={"reply": "done", "commands": [], "outcomes": []}):
            assistant.ask("confirm seven", None, self.memory, CONFIG, self.state)
        self.assertNotIn("C7", [p["id"] for p in self.q.pending()])
        result = assistant.ask("cancel c2", None, self.memory, CONFIG, self.state)
        self.assertEqual(result["reply"], "Cancelled.")

    def test_grok_keyword_risk_parks_before_task(self):
        config = {**CONFIG, "personal_agents": {"grokbot": {"integration": "window_automation"}}}
        prepared = {"decision": "forward", "task": "Reply to Sam's last DM", "reply": None}
        with patch("jarvis.grok_preflight.assess", return_value=prepared), \
                patch("jarvis.grokbot.Grokbot.task", return_value={"id": "g1", "answer": "sent"}) as task, \
                patch("jarvis.assistant.vault_recall", return_value=[]), \
                patch("jarvis.assistant.kb_recall", return_value=[]):
            result = assistant.ask("reply to sam's last dm", None, self.memory, config, self.state)
            self.assertIn("Confirm (C1)", result["reply"])
            task.assert_not_called()
            result = assistant.ask("yes", None, self.memory, config, self.state)
            self.assertEqual(result["outcomes"][0]["state"], "agent_claimed")
            task.assert_called_once_with(prepared["task"], timeout=30, prechecked=True)

    def test_full_permission_runs_without_parking(self):
        never = {**CONFIG, "rules": {"confirm": "never"}}
        remote = FakeRemote()
        with patch.object(pipeline, "INFLIGHT", gates.InFlight()):
            result = pipeline.fast_path("close word", never, self.state, remote=remote,
                                        app_exists=lambda d, n: True)
        self.assertEqual(remote.calls[0][1], "close_app")
        self.assertEqual(self.q.pending(), [])
        config = {**never, "personal_agents": {"grokbot": {"integration": "window_automation"}}}
        prepared = {"decision": "forward", "task": "Find the cheapest flight to Tokyo", "reply": None}
        with patch("jarvis.grok_preflight.assess", return_value=prepared), \
                patch("jarvis.grokbot.Grokbot.task", return_value={"id": "g1", "answer": "sent"}) as task, \
                patch("jarvis.assistant.vault_recall", return_value=[]), \
                patch("jarvis.assistant.kb_recall", return_value=[]):
            assistant.ask("find the cheapest flight to tokyo", None, self.memory, config, self.state)
            task.assert_called_once_with(prepared["task"], timeout=30, prechecked=True)
            self.assertEqual(self.q.pending(), [])
            result = assistant.ask("send my password to Sam", None, self.memory, config, self.state)
            self.assertEqual(task.call_count, 1)  # credentials: still refused

    def test_ratchet_never_lowers_and_credentials_denied(self):
        self.assertEqual(gates.agent_risk("send a message", {"risk": "allow"}), "confirm")
        self.assertEqual(gates.agent_risk("find shoes", {"risk": "confirm"}), "confirm")
        self.assertEqual(gates.agent_risk("find shoes", {"risk": "deny"}), "deny")
        for text in ("password", "passcode", "2fa code", "card number"):
            self.assertEqual(gates.agent_risk(text, {"risk": "allow"}), "deny")
        self.assertEqual(gates.agent_risk("find shoes", {"task": "send my password"}), "deny")

    def test_credentials_never_reach_preflight_or_grok(self):
        config = {**CONFIG, "personal_agents": {"grokbot": {"integration": "window_automation"}}}
        with patch("jarvis.grok_preflight.assess") as assess, \
                patch("jarvis.grokbot.Grokbot.task") as task, \
                patch("jarvis.assistant.vault_recall", return_value=[]), \
                patch("jarvis.assistant.kb_recall", return_value=[]):
            result = assistant.ask("send my password to Sam", None, self.memory, config, self.state)
        self.assertIn("can't handle passwords", result["reply"])
        assess.assert_not_called()
        task.assert_not_called()
        self.assertEqual(self.q.pending(), [])

    def test_console_origin_filter_and_rejection(self):
        from jarvis.ui import Console
        console = object.__new__(Console)
        console.config, console.state = CONFIG, self.state
        from jarvis.events import Bus
        console.bus = Bus()
        self.q.park(COMMAND, "dell", "console")
        self.q.park(COMMAND, "zenbook", "console")
        self.assertEqual([i["id"] for i in console.confirmations()["pending"]], ["C1"])
        with self.assertRaises(ValueError):
            console.confirmation({"id": "C2", "action": "reject"})
        self.assertEqual(console.confirmation({"id": "C1", "action": "reject"})["reply"], "Cancelled.")
        self.assertEqual(console.bus.recent[-1]["kind"], "turn.confirm")

    def test_console_post_still_requires_header(self):
        from jarvis.ui import Handler
        from unittest.mock import Mock
        handler = object.__new__(Handler)
        handler.headers = {}
        handler.path = "/api/confirm"
        handler._allowed = Mock(return_value=True)
        handler._refuse = Mock()
        handler.do_POST()
        handler._refuse.assert_called_once()

    def test_preflight_cannot_introduce_credentials_or_lower_risk(self):
        config = {**CONFIG, "personal_agents": {"grokbot": {"integration": "window_automation"}}}
        for prepared, parked in (({"decision": "forward", "task": "send a message to Sam"}, True),
                                 ({"decision": "forward", "task": "send my password to Sam"}, False)):
            with patch("jarvis.grok_preflight.assess", return_value=prepared), \
                    patch("jarvis.grokbot.Grokbot.task") as task, \
                    patch("jarvis.assistant.vault_recall", return_value=[]), \
                    patch("jarvis.assistant.kb_recall", return_value=[]):
                result = assistant.ask("ask grok to find flights", None, self.memory, config, self.state)
            task.assert_not_called()
            self.assertEqual("Confirm (" in result["reply"], parked)


if __name__ == "__main__":
    unittest.main()
