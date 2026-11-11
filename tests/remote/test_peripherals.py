"""Devices view's Connect & share: overview from doctor + desktop actions, and its buttons."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import clipsync, peripherals, remote, sharedinput

CONFIG = {"cluster": {"enabled": True, "main_device": "zenbook"},
          "devices": {"zenbook": {"label": "ASUS Zenbook"}, "dell": {"label": "Dell G15"}}}


class FakeRemote:
    def __init__(self, here="zenbook", answers=None):
        self.where, self.calls, self.answers = here, [], answers or {}

    def here(self):
        return self.where

    def call(self, device, action, arg):
        self.calls.append((device, action, arg))
        answer = self.answers.get((device, action))
        if isinstance(answer, Exception):
            raise answer
        return answer or (True, json.dumps({"running": True}))


def doctor_report(online):
    return {"ok": online, "role": "main", "checks": [
        {"check": "this PC's identity", "ok": True, "detail": "zenbook (main)"},
        {"check": "dell: online", "ok": online, "detail": "last seen 9 s ago"}]}


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def overview(self, online, fake):
        with patch("jarvis.cluster_setup.doctor", return_value=doctor_report(online)):
            return peripherals.overview(CONFIG, self.state / "c.json", self.state, remote=fake)

    def test_online_node_reports_both_halves(self):
        lan = {"running": True, "build": "jarvis", "config": {"clients": [{"hostname": "WinLaptop", "position": "right"}]}}
        fake = FakeRemote(answers={("zenbook", "lan_mouse"): (True, json.dumps(lan))})
        got = self.overview(True, fake)
        here = next(d for d in got["devices"] if d["id"] == "zenbook")
        self.assertEqual(here["lan_mouse"]["peers"], [{"hostname": "WinLaptop", "position": "right"}])
        self.assertNotIn("config", here["lan_mouse"])
        dell = next(d for d in got["devices"] if d["id"] == "dell")
        self.assertEqual((dell["role"], dell["online"]), ("node", True))
        self.assertEqual({(d, a) for d, a, _ in fake.calls},
                         {(d, a) for d in ("zenbook", "dell") for a in ("lan_mouse", "clipboard")})

    def test_offline_node_is_skipped_not_waited_on(self):
        fake = FakeRemote()
        got = self.overview(False, fake)
        dell = next(d for d in got["devices"] if d["id"] == "dell")
        self.assertEqual((dell["lan_mouse"], dell["lan_mouse_error"]), (None, "offline"))
        self.assertNotIn("dell", {d for d, _, _ in fake.calls})
        self.assertFalse(got["ok"])

    def test_one_failing_computer_does_not_blank_the_view(self):
        fake = FakeRemote(answers={("dell", "clipboard"): RuntimeError("boom"),
                                   ("dell", "lan_mouse"): (True, "[1]")})
        dell = next(d for d in self.overview(True, fake)["devices"] if d["id"] == "dell")
        self.assertIn("boom", dell["clipboard_error"])
        self.assertIn("other than a report", dell["lan_mouse_error"])


class ActTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def act(self, body, fake=None):
        return peripherals.act(CONFIG, self.state / "c.json", self.state, body, remote=fake or FakeRemote())

    def test_switches_go_to_the_chosen_computer(self):
        fake = FakeRemote()
        self.act({"do": "clipboard", "device": "dell", "action": "autostart-on"}, fake)
        self.act({"do": "lan_mouse", "device": "dell", "action": "stop"}, fake)
        self.assertEqual(fake.calls, [("dell", "clipboard", "autostart-on"), ("dell", "lan_mouse", "stop")])

    def test_unknown_actions_refused_before_anything_runs(self):
        fake = FakeRemote()
        for body in ({"do": "lan_mouse", "action": "pair"}, {"do": "clipboard", "action": "run"}, {"do": "rm"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.act(body, fake)
        self.assertEqual(fake.calls, [])

    def _apart(self):
        now = time.time()
        (self.state / sharedinput.GUARD_FILE).write_text(json.dumps(
            {"apart_now": True, "read_at": now, "why": "Dell is on another network.", "override": False}))

    def test_start_while_apart_is_refused_and_clipboard_still_runs(self):
        self._apart()
        fake = FakeRemote()
        with self.assertRaisesRegex(RuntimeError, "Remote control still works"):
            self.act({"do": "lan_mouse", "device": "dell", "action": "start"}, fake)
        self.assertEqual(fake.calls, [])
        self.act({"do": "clipboard", "device": "dell", "action": "start"}, fake)
        self.assertEqual(fake.calls, [("dell", "clipboard", "start")])

    def test_pair_while_apart_does_not_run(self):
        self._apart()
        with patch("jarvis.sharedinput.pair") as pair:
            with self.assertRaises(RuntimeError):
                self.act({"do": "pair", "device": "dell", "position": "right"})
        pair.assert_not_called()

    def test_unknown_network_still_sends_start(self):
        fake = FakeRemote()
        self.act({"do": "lan_mouse", "device": "dell", "action": "start"}, fake)
        self.assertEqual(fake.calls, [("dell", "lan_mouse", "start")])

    def test_failed_switch_raises_with_the_reason(self):
        fake = FakeRemote(answers={("dell", "lan_mouse"): (False, "Lan Mouse isn't installed")})
        with self.assertRaisesRegex(RuntimeError, "isn't installed"):
            self.act({"do": "lan_mouse", "device": "dell", "action": "start"}, fake)

    def test_join_takes_only_a_code_never_a_path(self):
        with patch("jarvis.cluster_setup.join") as join:
            for code in ("/etc/passwd", "~/.ssh/id_ed25519", ""):
                with self.subTest(code=code), self.assertRaises(ValueError):
                    self.act({"do": "join", "code": code})
        join.assert_not_called()

    def test_invite_returns_only_what_the_view_shows(self):
        report = {"invited": "dell", "code": "jarvis-invite-1:x", "authorized_keys": "/root/.ssh/x",
                  "addresses": ["10.0.0.2"], "expires": "in 24 hours", "next": "..."}
        with patch("jarvis.cluster_setup.invite", return_value=report):
            got = self.act({"do": "invite", "device": "dell"})
        self.assertEqual(got["code"], "jarvis-invite-1:x")
        self.assertNotIn("authorized_keys", got)


class NodeConsoleTests(unittest.TestCase):
    """A node's console answers Connect & share itself, even with no main to forward to."""

    def test_answered_locally_not_forwarded(self):
        import threading
        import urllib.request
        from jarvis.cluster_proxy import proxy_handler
        from jarvis.ui import ConsoleServer
        server = ConsoleServer(("127.0.0.1", 0), proxy_handler(None, local=("cfg", "path", "state")))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/api/peripherals"
        with patch("jarvis.peripherals.overview", return_value={"here": "dell"}) as overview, \
                patch("jarvis.peripherals.act", return_value={"joined": True}) as act:
            with urllib.request.urlopen(url, timeout=5) as res:
                self.assertEqual(json.load(res), {"here": "dell"})
            req = urllib.request.Request(url, data=b'{"do":"join","code":"x"}',
                                         headers={"X-JARVIS": "1", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as res:
                self.assertEqual(json.load(res), {"joined": True})
        overview.assert_called_once_with("cfg", "path", "state")
        self.assertEqual(act.call_args.args, ("cfg", "path", "state", {"do": "join", "code": "x"}))


class ClipboardActionTests(unittest.TestCase):
    def test_action_is_routed_and_validated(self):
        self.assertIn("clipboard", remote.ACTIONS)
        ok, text = clipsync.control("format c:", state=tempfile.gettempdir())
        self.assertFalse(ok)
        self.assertIn("Use start", text)

    def test_status_hides_the_local_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, text = clipsync.control("status", state=tmp)
        self.assertTrue(ok)
        report = json.loads(text)
        self.assertNotIn("log", report)
        self.assertFalse(report["running"])


if __name__ == "__main__":
    unittest.main()
