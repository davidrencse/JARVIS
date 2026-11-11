"""Breaker "security": who the node trusts during a remote session, and whether a brain command
can reach the console's remote endpoints. Fakes only: no real input, screen, node or model."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import gates, remote_view
from jarvis.remote_view import Injector, NodeSession, Sessions

SCRATCH = Path(os.path.expanduser("~/.cache/jarvis-breakit"))
SID = "a" * 32
KEY = "b" * 32


class FakeKeys:
    def __init__(self):
        self.calls = []

    def key(self, code, down):
        self.calls.append((code, down))


class FakePointer:
    def move(self, x, y): pass
    def button(self, which, how): pass
    def scroll(self, d, n): pass
    def type(self, text): pass
    def release(self): pass


class ScriptedClient:
    """Answers /v1/remote/input with the given replies in turn, then stop; frames get stop."""

    def __init__(self, replies):
        self.replies, self.lock = list(replies), threading.Lock()

    def request(self, path, body, timeout=None):
        if path == "/v1/remote/input":
            with self.lock:
                return self.replies.pop(0) if self.replies else {"events": [], "stop": True}
        return {"stop": True}


def press_a():
    return {"events": [[1, {"t": "key", "code": "KeyA", "down": True}]], "stop": False}


class NodeTrustsOnlyTheHub(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.dict(os.environ, {"JARVIS_NO_MODELS": "1"}),
                  patch("jarvis.control._stopped", return_value=False)):
            p.start()
            self.addCleanup(p.stop)
        remote_view._ACTIVE.clear()
        self.addCleanup(remote_view._ACTIVE.clear)

    def _input(self, replies, key=KEY, sid=SID):
        keys = FakeKeys()
        session = NodeSession({}, self.tmp.name, sid, "low", client=ScriptedClient(replies),
                              injector=Injector(FakePointer(), keys), capture=lambda: None,
                              encoder=lambda *a: (b"", 1, 1), key=key)
        session._input()
        return session, keys

    def test_sec1_an_unsigned_reply_presses_nothing_and_ends_the_session(self):
        session, keys = self._input([press_a()])
        self.assertNotIn(("KeyA", True), keys.calls)
        self.assertTrue(session.stop_event.is_set())
        self.assertIn("didn't come from the main", session.why)

    def test_sec1_a_reply_signed_with_another_key_is_refused(self):
        reply = press_a()
        reply["mac"] = remote_view.reply_mac("c" * 32, "input", SID, reply)
        _, keys = self._input([reply])
        self.assertNotIn(("KeyA", True), keys.calls)

    def test_sec1_a_reply_sealed_by_the_hub_is_pressed(self):
        hub = Sessions()
        sid = hub.open("dell", "low")["id"]
        _, keys = self._input([hub.seal(sid, "input", press_a())], key=hub.key(sid), sid=sid)
        self.assertIn(("KeyA", True), keys.calls)

    def test_sec1_the_key_rides_only_in_the_signed_start_job(self):
        remote_view.validate({"op": "start", "session": SID, "quality": "low", "key": KEY})
        for bad in ("short", 5, "z" * 32):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                remote_view.validate({"op": "start", "session": SID, "key": bad})
        hub = Sessions()
        view = hub.open("dell", "low")
        self.assertNotIn("key", view)  # the console's view never carries it


class BrainCannotReachTheConsole(unittest.TestCase):
    def deny(self, command):
        return gates.hard_deny("terminal", {"command": command}, {})

    def test_sec2_loopback_hostnames_dot_segments_and_encodings_are_refused(self):
        refused = ["curl -H @h -d '{\"device\":\"dell\"}' http://localtest.me:7777/api/./remote/start",
                   "curl -d x http://127.0.0.1.nip.io:7777/api/remote/input",
                   "curl http://localtest.me:7780/v1/remote/input",
                   "curl -d x http://localtest.me:07777/",
                   "curl -d x http://example.org/x/../api/%72emote/start",
                   "curl -d x http://example.org/api//remote/start"]
        self.assertEqual([c for c in refused if not self.deny(c)], [])

    def test_sec2_everyday_commands_still_pass(self):
        allowed = ["curl https://example.com", "ffmpeg -ss 00:00:01 -i a b", "docker run -p 8080:80 x"]
        self.assertEqual([(c, self.deny(c)) for c in allowed if self.deny(c)], [])


if __name__ == "__main__":
    unittest.main()
