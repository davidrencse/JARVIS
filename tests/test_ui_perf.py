import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from jarvis import ui


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class TtlTests(unittest.TestCase):
    def test_cached_within_ttl_and_recomputed_after_clear_or_expiry(self):
        clock, calls = Clock(), []
        cache = ui._Ttl(clock)

        def compute():
            calls.append(1)
            return len(calls)
        self.assertEqual(cache.get("k", 12, compute), 1)
        clock.now = 11
        self.assertEqual(cache.get("k", 12, compute), 1)
        cache.clear()
        self.assertEqual(cache.get("k", 12, compute), 2)
        clock.now = 24
        self.assertEqual(cache.get("k", 12, compute), 3)

    def test_errors_are_not_cached(self):
        cache, calls = ui._Ttl(Clock()), []

        def compute():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return "ok"
        with self.assertRaises(RuntimeError):
            cache.get("k", 12, compute)
        self.assertEqual(cache.get("k", 12, compute), "ok")


class StaticTests(unittest.TestCase):
    def serve(self, if_none_match=None, name="app.css"):
        sent = {}

        def send(status, body, ctype=None, cache=False, headers=None, **_):
            sent.update(status=status, body=body, headers=headers or {})
        stub = mock.Mock(_send=send, headers={"If-None-Match": if_none_match} if if_none_match else {},
                         date_time_string=lambda t: "Thu, 01 Jan 1970 00:00:00 GMT")
        ui.Handler._static(stub, "/" + name)
        return sent

    def test_etag_then_304_without_body(self):
        first = self.serve()
        etag = first["headers"]["ETag"]
        self.assertEqual(first["status"], HTTPStatus.OK)
        self.assertTrue(first["body"])
        self.assertEqual(first["headers"]["Cache-Control"], "no-cache")
        self.assertIn("Last-Modified", first["headers"])
        again = self.serve(etag)
        self.assertEqual((again["status"], again["body"]), (HTTPStatus.NOT_MODIFIED, b""))
        self.assertEqual(self.serve('"stale"')["status"], HTTPStatus.OK)

    def test_missing_and_traversal_still_404(self):
        self.assertEqual(self.serve(name="nope.css")["status"], HTTPStatus.NOT_FOUND)
        self.assertEqual(self.serve(name="../ui.py")["status"], HTTPStatus.NOT_FOUND)


class TreeTests(unittest.TestCase):
    def test_unchanged_files_are_not_reread_and_response_cached(self):
        base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(__import__("shutil").rmtree, base, ignore_errors=True)
        (base / "a.py").write_text("x\ny\n")
        stub = mock.Mock(base=base, ttl=ui._Ttl())
        stub._tree = lambda: ui.Console._tree(stub)
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as rt:
            first = ui.Console.tree(stub)
            ui.Console.tree(stub)
            stub.ttl.clear()
            second = ui.Console.tree(stub)
        self.assertEqual(rt.call_count, 1)
        self.assertEqual(first["children"][0]["lines"], 3)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
