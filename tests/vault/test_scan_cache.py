"""The shared vault walk is reused across the watcher's 3 s poll and dropped on forget."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import vault as vault_module
from jarvis.vault import forget, scan


class ScanCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name, "Vault")
        (self.root / "notes").mkdir(parents=True)
        (self.root / "notes" / "n.md").write_text("hello\n", encoding="utf-8")
        forget(self.root)
        self.addCleanup(forget, self.root)

    def _counting_walk(self):
        real, count = os.walk, {"n": 0}

        def walk(*args, **kwargs):
            count["n"] += 1
            return real(*args, **kwargs)

        return count, patch.object(vault_module.os, "walk", walk)

    def test_second_scan_inside_ttl_returns_the_same_list_and_does_not_walk(self):
        count, walks = self._counting_walk()
        with walks:
            first = scan(self.root)
            second = scan(self.root)
        self.assertEqual(count["n"], 1)
        self.assertIs(first, second)
        self.assertEqual(len(first), 1)
        rel, is_link, mtime, size = first[0]
        self.assertEqual(rel, "notes/n.md")
        self.assertIs(is_link, False)
        self.assertIsInstance(mtime, float)
        self.assertEqual(size, len("hello\n".encode("utf-8")))

    def test_forget_drops_the_snapshot(self):
        count, walks = self._counting_walk()
        with walks:
            first = scan(self.root)
            forget(self.root)
            second = scan(self.root)
        self.assertEqual(count["n"], 2)
        self.assertIsNot(first, second)
        self.assertEqual(first, second)

    def test_force_rewalks_inside_ttl(self):
        count, walks = self._counting_walk()
        with walks:
            first = scan(self.root)
            forced = scan(self.root, force=True)
        self.assertEqual(count["n"], 2)
        self.assertIsNot(first, forced)
        self.assertEqual(first, forced)

    def test_watcher_interval_reuses_the_snapshot(self):
        """A scan 3 s later (one watcher tick) must not walk; the snapshot dies at SCAN_TTL."""
        clock = {"now": 1_000.0}
        count, walks = self._counting_walk()

        def monotonic():
            return clock["now"]

        with patch.object(vault_module.time, "monotonic", monotonic), walks:
            first = scan(self.root)
            clock["now"] += 3
            second = scan(self.root)
            self.assertEqual(count["n"], 1)
            self.assertIs(first, second)
            clock["now"] += 1  # age is now SCAN_TTL (4 s): the next reader walks again
            third = scan(self.root)
        self.assertEqual(count["n"], 2)
        self.assertIsNot(first, third)

    def test_concurrent_callers_of_one_root_share_one_walk(self):
        real = os.walk
        calls = {"n": 0}
        overlap = threading.Barrier(2)
        begin = threading.Barrier(2)
        results, errors = [], []

        def walk(*args, **kwargs):
            calls["n"] += 1
            try:
                overlap.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass
            return real(*args, **kwargs)

        def run():
            try:
                begin.wait(timeout=2)
                results.append(scan(self.root))
            except Exception as exc:
                errors.append(exc)

        with patch.object(vault_module.os, "walk", walk):
            threads = [threading.Thread(target=run), threading.Thread(target=run)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
