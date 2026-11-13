"""Auth backoff on the cluster hub.

Per source, `free` failures inside `window` are free. The next ones wait
min(cap, base * 2 ** (n - free)) seconds. A global counter applies that same
delay to every source once it reaches `global_free`. Failures older than
`window` drop out. The clock is injected; nothing here patches sleep or
Event.wait.
"""

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from jarvis.cluster import AuthBackoff, AuthBackoffError, Hub
from jarvis.cluster_store import CAPABILITIES
from jarvis.ui import Console

try:
    from tests.cluster.test_cluster import configuration
except ImportError:  # python -m unittest discover -s tests
    from test_cluster import configuration


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _tracked_sources(backoff):
    """Largest string-keyed map or string sequence hanging off the backoff object."""
    best = 0
    seen = set()

    def walk(obj):
        nonlocal best
        if id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, dict):
            keys = sum(isinstance(key, str) for key in obj)
            if keys >= 8:
                best = max(best, keys)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, (list, tuple, set, deque)):
            seq = list(obj)
            if len(seq) >= 8 and all(isinstance(item, str) for item in seq[:8]):
                best = max(best, len(seq))
            for item in seq[:32]:
                walk(item)

    walk(vars(backoff))
    return best


class AuthBackoffTests(unittest.TestCase):
    def backoff(self, **overrides):
        clock = FakeClock()
        params = {"clock": clock.monotonic, "free": 5, "global_free": 50,
                  "base": 1.0, "cap": 60.0, "window": 300.0}
        params.update(overrides)
        return clock, AuthBackoff(**params)

    def test_no_delay_until_free_failures(self):
        _, backoff = self.backoff(free=5)
        self.assertIsInstance(backoff.retry_after("src"), float)
        self.assertEqual(backoff.retry_after("src"), 0.0)
        for _ in range(4):
            backoff.fail("src")
            self.assertEqual(backoff.retry_after("src"), 0.0)
        backoff.fail("src")
        self.assertEqual(backoff.retry_after("src"), 1.0)
        self.assertEqual(backoff.retry_after("quiet"), 0.0)

    def test_delay_doubles_until_the_cap(self):
        # n < free → 0; n >= free → min(cap, base * 2 ** (n - free))
        _, backoff = self.backoff(free=5, base=1.0, cap=60.0)
        for _ in range(4):
            backoff.fail("src")
        for expected in (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0):
            backoff.fail("src")
            self.assertEqual(backoff.retry_after("src"), expected)
        self.assertEqual(backoff.retry_after("quiet"), 0.0)

    def test_failures_older_than_the_window_are_forgotten(self):
        clock, backoff = self.backoff(free=3, global_free=50, window=300.0)
        for _ in range(3):
            backoff.fail("src")
        self.assertEqual(backoff.retry_after("src"), 1.0)
        clock.advance(200)
        for _ in range(3):
            backoff.fail("src")
        self.assertEqual(backoff.retry_after("src"), 8.0)  # 6 still inside the window
        clock.advance(150)  # the first 3 are now older than the window; 3 remain
        self.assertEqual(backoff.retry_after("src"), 1.0)
        clock.advance(200)  # the rest aged out too
        self.assertEqual(backoff.retry_after("src"), 0.0)

    def test_ok_clears_that_source_counter(self):
        _, backoff = self.backoff(free=3, global_free=50)
        for _ in range(3):
            backoff.fail("src")
            backoff.fail("other")
        self.assertEqual(backoff.retry_after("src"), 1.0)
        backoff.ok("src")
        self.assertEqual(backoff.retry_after("src"), 0.0)
        self.assertEqual(backoff.retry_after("other"), 1.0)
        backoff.fail("src")
        backoff.fail("src")
        self.assertEqual(backoff.retry_after("src"), 0.0)
        backoff.fail("src")
        self.assertEqual(backoff.retry_after("src"), 1.0)

    def test_global_threshold_delays_a_fresh_source(self):
        _, backoff = self.backoff(free=100, global_free=4, base=1.0, cap=60.0)
        for i in range(3):
            backoff.fail(f"busy-{i}")
        self.assertEqual(backoff.retry_after("fresh"), 0.0)
        backoff.fail("busy-3")
        self.assertEqual(backoff.retry_after("fresh"), 1.0)
        backoff.fail("busy-4")
        self.assertEqual(backoff.retry_after("fresh"), 2.0)
        self.assertEqual(backoff.retry_after("busy-0"), 2.0)

    def test_eviction_drops_the_oldest_source_and_stays_bounded(self):
        _, backoff = self.backoff(free=3, global_free=10 ** 9)
        for _ in range(3):
            backoff.fail("old")
        self.assertEqual(backoff.retry_after("old"), 1.0)
        for i in range(1200):
            backoff.fail(f"n{i:04d}")
        last = "n1199"
        backoff.fail(last)
        backoff.fail(last)
        self.assertEqual(backoff.retry_after(last), 1.0)
        self.assertEqual(backoff.retry_after("old"), 0.0)
        tracked = _tracked_sources(backoff)
        self.assertGreater(tracked, 100)
        self.assertLessEqual(tracked, 1024)

    def test_eight_threads_hammering_fail_do_not_raise(self):
        backoff = AuthBackoff(free=5, global_free=50, base=1.0, cap=60.0, window=300.0)
        errors = []

        def hammer(index):
            try:
                for n in range(200):
                    backoff.fail("shared" if n % 3 == 0 else f"t{index}-{n % 7}")
            except Exception as exc:  # the assertion is "does not raise"
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])


class HubAuthBackoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.hub = Hub(configuration(), self.root, self.root / "main")
        self.clock = FakeClock()
        self.hub.backoff = AuthBackoff(clock=self.clock.monotonic, free=5, global_free=50,
                                        base=1.0, cap=60.0, window=300.0)
        self.node_token = self.hub.broker.enroll("node", CAPABILITIES)
        self.ok_sources = []
        real_ok = self.hub.backoff.ok

        def spy(source):
            self.ok_sources.append(source)
            return real_ok(source)

        self.hub.backoff.ok = spy

    def headers(self, token):
        return {"X-JARVIS-Main": "main", "Authorization": f"Bearer {token}"}

    def bad(self):
        return self.headers("b" * 32)

    def test_main_mismatch_is_not_counted(self):
        for _ in range(40):
            for headers in ({"X-JARVIS-Main": "other", "Authorization": "Bearer " + "z" * 32},
                            {"Authorization": "Bearer " + "z" * 32}):
                with self.assertRaises(PermissionError) as caught:
                    self.hub.authenticate(headers, source="mismatch")
                self.assertNotIsInstance(caught.exception, AuthBackoffError)
        with self.assertRaises(PermissionError) as caught:
            self.hub.authenticate(self.bad(), source="mismatch")
        self.assertNotIsInstance(caught.exception, AuthBackoffError)
        self.assertEqual(self.hub.backoff.retry_after("mismatch"), 0.0)
        self.assertEqual(self.hub.backoff.retry_after("never-seen"), 0.0)

    def test_early_bad_tokens_are_plain_permission_errors(self):
        for _ in range(4):
            with self.assertRaises(PermissionError) as caught:
                self.hub.authenticate(self.bad(), source="early")
            self.assertNotIsInstance(caught.exception, AuthBackoffError)
        with self.assertRaises(AuthBackoffError) as caught:
            self.hub.authenticate(self.bad(), source="early")
        self.assertIsInstance(caught.exception.retry_after, int)
        self.assertGreaterEqual(caught.exception.retry_after, 1)

    def test_valid_tokens_still_authenticate_after_a_flood(self):
        for i in range(500):
            for source in ("same", f"other-{i % 20}"):
                with self.assertRaises(PermissionError):
                    self.hub.authenticate(self.bad(), source=source)
        with self.assertRaises(AuthBackoffError) as caught:
            self.hub.authenticate(self.bad(), source="same")
        self.assertIsInstance(caught.exception.retry_after, int)
        self.assertGreaterEqual(caught.exception.retry_after, 1)

        owner_same = self.hub.authenticate(self.headers(self.hub.owner), source="same")
        self.assertEqual(owner_same[0], "main")
        self.assertIn("owner", owner_same[1])
        node_other = self.hub.authenticate(self.headers(self.node_token), "console", "other-0")
        self.assertEqual(node_other[0], "node")
        self.assertIn("console", node_other[1])
        self.hub.authenticate(self.headers(self.hub.owner), source="other-1")
        self.hub.authenticate(self.headers(self.node_token), source="same")
        self.hub.authenticate(self.headers(self.node_token))  # source defaults to None
        self.assertIn("same", self.ok_sources)
        self.assertIn("other-0", self.ok_sources)

        self.clock.advance(301)
        with self.assertRaises(PermissionError) as caught:
            self.hub.authenticate(self.bad(), source="same")
        self.assertNotIsInstance(caught.exception, AuthBackoffError)


class HubBackoffHTTPTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        config = configuration()
        self.console = Console(config, root, root / "main")
        self.addCleanup(self.console.close)
        self.hub = Hub(config, root, root / "main", console=self.console).start(port=0)
        self.addCleanup(self.hub.close)
        self.node_token = self.hub.broker.enroll("node", CAPABILITIES)
        self.port = self.hub.server.server_address[1]

    def hit(self, token):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/status",
            headers={"Authorization": f"Bearer {token}", "X-JARVIS-Main": "main"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers.get("Retry-After")
        except urllib.error.HTTPError as exc:
            try:
                retry = exc.headers.get("Retry-After")
                return exc.code, retry
            finally:
                exc.close()

    def test_backed_off_bad_token_is_429_and_a_good_token_is_200(self):
        bad = "b" * 32
        seen = [self.hit(bad) for _ in range(12)]
        self.assertEqual(seen[-1][0], 429)
        self.assertGreaterEqual(int(float(seen[-1][1])), 1)
        self.assertEqual(self.hit(self.node_token)[0], 200)
        self.assertEqual(self.hit(self.hub.owner)[0], 200)


if __name__ == "__main__":
    unittest.main()
