"""`jarvis desktop --device <node>` from a terminal: the CLI holds no MAIN lease, so it
asks the running console (/api/desktop) to send the job. Browsers may not use it."""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from jarvis import ui
from jarvis.ui import Console, Handler, desktop_via_console


class Endpoint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.console = Console({}, Path(tmp.name), Path(tmp.name) / "state")
        handler = type("H", (Handler,), {"console": self.console,
                                         "hosts": ("127.0.0.1", "localhost", "::1")})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = mock.patch("jarvis.remote.Remote.call", return_value=(True, "hi from dell"))
        self.call = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, body, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = json.dumps(body).encode()
            conn.request("POST", "/api/desktop", body=data, headers={
                "Host": f"127.0.0.1:{self.port}", "X-JARVIS": "1",
                "Content-Type": "application/json", **(headers or {})})
            res = conn.getresponse()
            return res.status, json.loads(res.read() or b"{}")
        finally:
            conn.close()

    def test_cli_call_reaches_remote_call(self):
        status, reply = self.post({"device": "dell", "action": "terminal", "value": "hostname"})
        self.assertEqual(status, 200)
        self.assertEqual(reply, {"ok": True, "text": "hi from dell"})
        self.call.assert_called_once_with("dell", "terminal", "hostname")

    def test_no_lease_is_a_plain_answer(self):
        from jarvis.continuity.runtime import RuntimeRefusal
        self.call.side_effect = RuntimeRefusal("no_lease_id")
        status, reply = self.post({"device": "dell", "action": "terminal", "value": "x"})
        self.assertEqual(status, 200)
        self.assertFalse(reply["ok"])
        self.assertIn("no_lease_id", reply["text"])

    def test_browser_is_refused_and_nothing_runs(self):
        for extra in ({"Origin": f"http://127.0.0.1:{self.port}"}, {"Sec-Fetch-Site": "same-origin"}):
            status, _ = self.post({"device": "dell", "action": "terminal", "value": "x"}, extra)
            self.assertEqual(status, 403, extra)
        self.call.assert_not_called()

    def test_missing_header_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/desktop", body=b"{}", headers={
                "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"})
            self.assertNotEqual(conn.getresponse().status, 200)
        finally:
            conn.close()
        self.call.assert_not_called()

    def test_bad_input_is_refused(self):
        for body in ({"device": "dell", "action": "rm_everything", "value": ""},
                     {"device": "", "action": "terminal", "value": "x"},
                     {"device": "dell", "action": "terminal", "value": 5},
                     {"device": "dell", "action": "terminal", "value": "x" * (ui.DESKTOP_VALUE_MAX + 1)}):
            status, _ = self.post(body)
            self.assertNotEqual(status, 200, body)
        self.call.assert_not_called()


class Client(unittest.TestCase):
    def test_round_trip_against_real_endpoint(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        console = Console({}, Path(tmp.name), Path(tmp.name) / "state")
        handler = type("H", (Handler,), {"console": console, "hosts": ("127.0.0.1", "localhost", "::1")})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch("jarvis.remote.Remote.call", return_value=(False, "Refused on this computer: x")):
            ok, text = desktop_via_console("dell", "terminal", "x", port=server.server_address[1])
        self.assertFalse(ok)
        self.assertEqual(text, "Refused on this computer: x")

    def test_console_not_running(self):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        ok, text = desktop_via_console("dell", "terminal", "x", port=port, timeout=2)
        self.assertFalse(ok)
        self.assertIn("isn't running", text)


class CliFallback(unittest.TestCase):
    def run_cli(self, side_effect, device="dell"):
        from jarvis.__main__ import main
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Path(tmp.name) / "jarvis.config.json"
        config.write_text("{}")
        argv = ["jarvis", "--config", str(config), "--state", str(Path(tmp.name) / "state"),
                "desktop", "terminal", "hostname"] + (["--device", device] if device else [])
        with mock.patch("sys.argv", argv), \
                mock.patch("jarvis.remote.Remote.call", side_effect=side_effect), \
                mock.patch("jarvis.ui.desktop_via_console", return_value=(True, "via console")) as via, \
                mock.patch("builtins.print") as out:
            try:
                code = main()
            except Exception as exc:
                return exc, via, out
        return code, via, out

    def unbound(self, *a, **k):
        from jarvis.continuity.integration import IntegrationError
        raise IntegrationError("managed_unbound")

    def test_unbound_cli_goes_through_console(self):
        code, via, out = self.run_cli(self.unbound)
        self.assertEqual(code, 0)
        via.assert_called_once_with("dell", "terminal", "hostname")
        self.assertEqual(json.loads(out.call_args[0][0]), {"ok": True, "text": "via console"})

    def test_other_errors_and_local_calls_do_not_fall_back(self):
        result, via, _ = self.run_cli(RuntimeError("boom"))
        self.assertNotEqual(result, 0)
        via.assert_not_called()
        result, via, _ = self.run_cli(self.unbound, device=None)
        self.assertNotEqual(result, 0)
        via.assert_not_called()


if __name__ == "__main__":
    unittest.main()
