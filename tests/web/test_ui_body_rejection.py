"""Refused HTTP bodies: a predictable 413/403/400, bounded in bytes, time and threads, nothing run."""

import http.client
import json
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock

from jarvis.ui import Handler, MAX_AUDIO


def _serve(**limits):
    console = MagicMock()
    console.confirmations.return_value = {"pending": []}
    handler = type("H", (Handler,), {"console": console, "hosts": ("127.0.0.1", "localhost"), **limits})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, console


def _head(port, path, length, extra=()):
    lines = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "X-JARVIS: 1"]
    if length is not None:
        lines.append(f"Content-Length: {length}")
    lines += list(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _status_and_eof(sock, limit=20):
    """Read the whole reply; returns (status, body, closed_by_server)."""
    sock.settimeout(limit)
    buf = b""
    closed = False
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            closed = True
            break
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    return int(head.split()[1]) if head else None, body, closed


class RejectedBodies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.console = _serve()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.console.reset_mock()

    def post(self, length, body=b"", path="/api/listen", extra=(), send_head=None):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        try:
            s.sendall(send_head or _head(self.port, path, length, extra))
            if body:
                s.sendall(body)
            return _status_and_eof(s)
        finally:
            s.close()

    def test_near_limit_upload_gets_413_every_time(self):
        """The audit's 1-in-20 BrokenPipe: sizes just over the limit, many times, never a reset."""
        for i in range(40):
            size = MAX_AUDIO + 1000 + (i % 5) * 4096
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
            try:
                conn.putrequest("POST", "/api/listen", skip_host=True)
                conn.putheader("Host", f"127.0.0.1:{self.port}")
                conn.putheader("X-JARVIS", "1")
                conn.putheader("Content-Length", str(size))
                conn.endheaders(b"\0" * size)
                res = conn.getresponse()
                self.assertEqual(res.status, 413, f"attempt {i}")
                self.assertEqual(json.loads(res.read())["error"], "Too large.")
            finally:
                conn.close()
        self.console.transcribe.assert_not_called()

    def test_whole_near_limit_body_is_read_and_discarded_not_kept(self):
        size = MAX_AUDIO + 100_000
        status, body, closed = self.post(size, b"\0" * size)
        self.assertEqual(status, 413)
        self.assertTrue(closed)
        self.console.transcribe.assert_not_called()

    def test_huge_content_length_is_refused_without_allocating_or_waiting_for_it(self):
        started = time.monotonic()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        try:
            s.sendall(_head(self.port, "/api/listen", 10 ** 15))
            s.settimeout(5)
            data = s.recv(65536)  # the 413 arrives with no body sent at all
        finally:
            s.close()
        self.assertIn(b" 413 ", data.split(b"\r\n", 1)[0])
        self.assertLess(time.monotonic() - started, 4)
        self.console.transcribe.assert_not_called()

    def test_absurd_digit_string_is_refused_not_a_crash(self):
        status, _, _ = self.post(None, extra=("Content-Length: " + "9" * 6000,))
        self.assertEqual(status, 413)

    def test_bad_lengths_get_400_and_close(self):
        for value in ("-1", "abc", "1_0", "+5", "5, 5x", "٥", "1.5", ""):
            with self.subTest(value=value):
                head = _head(self.port, "/api/confirm", None).replace(
                    b"\r\n\r\n", f"\r\nContent-Length: {value}\r\n\r\n".encode("utf-8"))
                s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                try:
                    s.sendall(head)
                    status, _, closed = _status_and_eof(s, 10)
                finally:
                    s.close()
                self.assertEqual(status, 400)
                self.assertTrue(closed)
        self.console.confirmation.assert_not_called()

    def test_conflicting_content_lengths_and_chunked_bodies_are_refused(self):
        for extra in (("Content-Length: 5",), ("Transfer-Encoding: chunked",)):
            with self.subTest(extra=extra):
                status, _, closed = self.post(2, b"{}", path="/api/confirm", extra=extra)
                self.assertEqual(status, 400)
                self.assertTrue(closed)
        self.console.confirmation.assert_not_called()

    def test_unauthorized_body_is_refused_and_never_acted_on(self):
        body = json.dumps({"id": "x", "answer": "yes"}).encode()
        for extra in (("Sec-Fetch-Site: cross-site",), ("Origin: https://evil.example", "Sec-Fetch-Site: same-site")):
            with self.subTest(extra=extra):
                status, payload, closed = self.post(len(body), body, path="/api/confirm", extra=extra)
                self.assertEqual(status, 403)
                self.assertTrue(closed)
        # no X-JARVIS header
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            head = _head(self.port, "/api/confirm", len(body)).replace(b"X-JARVIS: 1\r\n", b"")
            s.sendall(head + body)
            status, _, _ = _status_and_eof(s, 10)
        finally:
            s.close()
        self.assertEqual(status, 403)
        self.console.confirmation.assert_not_called()

    def test_unauthorized_large_body_gets_403_or_reset_but_is_never_executed(self):
        size = 1 << 20  # beyond the 64 KiB refusal drain: cheap close, the 403 may be lost
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(_head(self.port, "/api/listen", size, ("Sec-Fetch-Site: cross-site",)))
            try:
                s.sendall(b"\0" * size)
            except OSError:
                pass
        finally:
            s.close()
        time.sleep(0.3)
        self.console.transcribe.assert_not_called()

    def test_oversize_json_never_reaches_the_handler(self):
        status, _, _ = self.post(MAX_AUDIO + 1, b"{" * 1000 + b"\0" * (MAX_AUDIO + 1 - 1000), path="/api/confirm")
        self.assertEqual(status, 413)
        self.console.confirmation.assert_not_called()


class StalledClients(unittest.TestCase):
    """Short bounds so the test is fast; the production values are class attributes of Handler."""

    def test_stalled_client_holds_its_thread_for_the_deadline_only(self):
        server, console = _serve(REJECT_IDLE_S=0.3, REJECT_DEADLINE_S=1.0)
        port = server.server_address[1]
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            s.sendall(_head(port, "/api/listen", MAX_AUDIO + 1))  # promises a body, sends none
            started = time.monotonic()
            status, _, closed = _status_and_eof(s, 6)
            s.close()
            self.assertEqual(status, 413)
            self.assertTrue(closed, "server must close a stalled rejected connection")
            self.assertLess(time.monotonic() - started, 2.5)
        finally:
            server.shutdown(); server.server_close()
        console.transcribe.assert_not_called()

    def test_dribbling_client_cannot_outlast_the_total_deadline(self):
        server, console = _serve(REJECT_IDLE_S=0.5, REJECT_DEADLINE_S=1.0)
        port = server.server_address[1]
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            s.sendall(_head(port, "/api/listen", MAX_AUDIO + 1))
            started = time.monotonic()
            closed = False
            while time.monotonic() - started < 5:
                try:
                    s.sendall(b"\0")  # always inside the idle window
                    s.settimeout(0.2)
                    try:
                        if s.recv(65536) == b"" :
                            closed = True
                            break
                    except socket.timeout:
                        pass
                except OSError:
                    closed = True
                    break
            s.close()
            self.assertTrue(closed)
            self.assertLess(time.monotonic() - started, 3.5)
        finally:
            server.shutdown(); server.server_close()

    def test_concurrent_drains_are_capped_and_released(self):
        server, _ = _serve(REJECT_IDLE_S=0.3, REJECT_DEADLINE_S=0.6)
        port = server.server_address[1]
        handler = server.RequestHandlerClass
        try:
            socks = []
            for _ in range(20):
                s = socket.create_connection(("127.0.0.1", port), timeout=10)
                s.sendall(_head(port, "/api/listen", MAX_AUDIO + 1))
                socks.append(s)
            for s in socks:
                status, _, closed = _status_and_eof(s, 6)
                self.assertEqual(status, 413)
                self.assertTrue(closed)
                s.close()
            # every permit came back
            deadline = time.monotonic() + 3  # the server releases just after it closes
            got = []
            while time.monotonic() < deadline:
                got += [p for p in (handler.DRAINS.acquire(blocking=False),) if p]
                if len(got) == 8:
                    break
                time.sleep(0.05)
            self.assertEqual(len(got), 8)
            for _ in got:
                handler.DRAINS.release()
        finally:
            server.shutdown(); server.server_close()

    def test_documented_bounds_are_coherent(self):
        self.assertGreater(Handler.REJECT_DRAIN_BYTES, MAX_AUDIO)
        self.assertLessEqual(Handler.REJECT_DRAIN_BYTES - MAX_AUDIO, 2 << 20)
        self.assertLessEqual(Handler.REFUSE_DRAIN_BYTES, 64 * 1024)
        self.assertLessEqual(Handler.REJECT_DEADLINE_S, 10)


if __name__ == "__main__":
    unittest.main()
