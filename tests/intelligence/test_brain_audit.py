"""Brain-path audit fixes: spec smuggling, Grok park, close_app titles, ORIGIN routing, site words."""

import json
import unittest
from unittest import mock

from jarvis import actions, observe
from jarvis.chatgpt import ConfirmGate
from jarvis.topology import ORIGIN


class Recorder:
    def __init__(self):
        self.calls = []

    def specs(self):
        return []

    def call(self, name, args):
        self.calls.append((name, dict(args)))
        return True, "ran"


class SpecSmugglingTests(unittest.TestCase):
    def gate(self, tmp="/tmp"):
        tools = Recorder()
        config = {"rules": {"confirm": "never"}}
        return tools, ConfirmGate(tools, config, tmp, here=lambda: "arch")

    def test_terminal_spec_never_reaches_executor(self):
        tools, gate = self.gate()
        gate.call("terminal", {"command": "ls", "spec": "rm -rf ~/Documents"})
        for _, args in tools.calls:
            self.assertNotIn("spec", args)
            self.assertEqual(args.get("command"), "ls")

    def test_bluetooth_spec_dropped(self):
        tools, gate = self.gate()
        gate.call("bluetooth", {"action": "list", "spec": json.dumps({"action": "forget", "name": "x"})})
        for _, args in tools.calls:
            self.assertNotIn("spec", args)

    def test_executor_builds_from_judged_keys(self):
        tools = actions.DesktopTools.__new__(actions.DesktopTools)
        tools.remote = None
        seen = {}
        import jarvis.terminal as terminal
        with mock.patch.object(terminal, "run", lambda spec: seen.setdefault("spec", spec) and (True, "")):
            _, gate = self.gate()
            gate.tools = tools
            gate.call("terminal", {"command": "ls", "spec": "rm -rf ~/Documents"})
        self.assertEqual(json.loads(seen["spec"]).get("command"), "ls")

    def test_run_here_spec_still_works(self):
        seen = {}
        import jarvis.terminal as terminal
        tools = actions.DesktopTools.__new__(actions.DesktopTools)
        tools.remote = None
        with mock.patch.object(terminal, "run", lambda spec: seen.setdefault("spec", spec) and (True, "")):
            tools.call("terminal", {"spec": "ls"})
        self.assertEqual(seen["spec"], "ls")


class GrokParkTests(unittest.TestCase):
    def run_ask(self, text, confirm):
        import tempfile
        from pathlib import Path
        from jarvis.assistant import ask
        from jarvis.grokbot import Grokbot
        from jarvis.memory import Memory
        from jarvis.store import Store
        config = {"personal_agents": {"grokbot": {"integration": "window_automation"}},
                  "rules": {"confirm": confirm}}
        prepared = {"decision": "forward", "task": text, "reply": "", "reason": "x"}
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("jarvis.assistant.grok_first", return_value=True), \
                mock.patch("jarvis.assistant._orchestrated", return_value=None), \
                mock.patch("jarvis.grok_preflight.assess", return_value=prepared), \
                mock.patch.object(Grokbot, "task", return_value={"id": "g1", "state": "done", "answer": "done"}) as task:
            base = Path(folder)
            result = ask(text, Store(base), Memory(base), config, base, lane=mock.Mock())
        return result, task

    def test_dm_and_subscribe_wait_for_david_under_confirm_never(self):
        for text in ("dm sam that I'm running late", "subscribe to the channel"):
            with self.subTest(text=text):
                result, task = self.run_ask(text, "never")
                task.assert_not_called()

    def test_plain_task_still_forwards_under_confirm_never(self):
        result, task = self.run_ask("check my gmail inbox count", "never")
        task.assert_called_once()


class CloseAppTests(unittest.TestCase):
    def test_terminal_titled_like_app_not_closed(self):
        windows = [{"app": "kitty", "title": "spotdl spotify playlist download", "focused": False}]
        with mock.patch.object(actions, "open_windows", return_value=windows), \
                mock.patch.object(actions, "_close") as close:
            ok, text = actions.close_app("spotify")
        self.assertFalse(ok)
        close.assert_not_called()
        self.assertIn("isn't running", text)


class OriginRoutingTests(unittest.TestCase):
    def tools(self):
        remote = mock.Mock()
        remote.here.return_value = "arch"
        remote.call.return_value = (True, "remote")
        t = actions.DesktopTools.__new__(actions.DesktopTools)
        t.remote = remote
        return t, remote

    def test_node_origin_routes_to_node(self):
        t, remote = self.tools()
        token = ORIGIN.set("dell")
        try:
            t.call("terminal", {"command": "ls ~/Downloads"})
        finally:
            ORIGIN.reset(token)
        self.assertEqual(remote.call.call_args[0][0], "dell")

    def test_no_origin_or_local_origin_stays_local(self):
        t, remote = self.tools()
        with mock.patch.object(t, "open_app", return_value=(True, "x"), create=True):
            t.call("open_app", {"name": "x"})
            token = ORIGIN.set("arch")
            try:
                t.call("open_app", {"name": "x"})
            finally:
                ORIGIN.reset(token)
        remote.call.assert_not_called()


class SiteWordTests(unittest.TestCase):
    def word(self, url):
        p = observe.predicate_for({"intent": "open_url", "args": {"url": url}})
        return p and p["text"]

    def test_words(self):
        self.assertEqual(self.word("https://en.wikipedia.org/wiki/X"), "wikipedia")
        self.assertEqual(self.word("https://open.spotify.com/"), "spotify")
        self.assertEqual(self.word("https://www.github.com/"), "github")
        self.assertEqual(self.word("https://bbc.co.uk/"), "bbc")
        self.assertEqual(self.word("https://mail.google.com/"), "gmail")
        self.assertIsNone(self.word("https://t.co/"))


if __name__ == "__main__":
    unittest.main()
