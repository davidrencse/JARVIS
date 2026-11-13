"""Incremental reads of Claude Code jsonl usage logs. No network, no credentials."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import credits as mod


class ClaudeIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.projects = self.home / ".claude" / "projects" / "demo"
        self.projects.mkdir(parents=True)
        self.path = self.projects / "session.jsonl"
        home = patch.object(mod, "HOME", self.home)
        home.start()
        self.addCleanup(home.stop)
        # An empty override still falls through to HOME; a real dir would not.
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""})
        env.start()
        self.addCleanup(env.stop)
        self.credits = mod.Credits(self.home)

    def line(self, message_id, output):
        return json.dumps({
            "type": "assistant",
            "timestamp": mod._now().isoformat(),
            "message": {"id": message_id, "usage": {"input_tokens": 3, "output_tokens": output}},
        })

    def refresh(self):
        reading = {"facts": [], "meters": [], "headline": None, "note": "",
                   "source": "none", "status": "unconfigured"}
        self.credits._claude_local_unlocked(reading, ())
        return reading

    def cached(self):
        return self.credits._claude_files[self.path]

    def watch(self, func):
        """Count json.loads and binary reads of the session log while func runs."""
        loads = []
        real_loads = mod.json.loads

        def counting(text, *args, **kwargs):
            loads.append(text)
            return real_loads(text, *args, **kwargs)

        opens = []
        real_open = open

        def counting_open(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            if Path(file) == self.path and "b" in str(mode):
                info = {"seek": [], "read": []}
                opens.append(info)
                real_seek, real_read = handle.seek, handle.read

                def seek(off, whence=0):
                    info["seek"].append((off, whence))
                    return real_seek(off, whence)

                def read(*a, **k):
                    data = real_read(*a, **k)
                    info["read"].append(len(data) if isinstance(data, (bytes, bytearray)) else data)
                    return data

                handle.seek, handle.read = seek, read
            return handle

        with patch.object(mod.json, "loads", counting), patch("builtins.open", counting_open):
            result = func()
        return result, loads, opens

    def test_helper_stops_at_last_newline_but_public_read_keeps_partial(self):
        body = (self.line("a", 1) + "\n" + self.line("b", 2)).encode()
        self.path.write_bytes(body)
        rows, offset = mod.claude_usage_rows_from(self.path, 0)
        self.assertEqual([row[0] for row in rows], ["a"])
        self.assertEqual(offset, body.index(b"\n") + 1)
        again, offset2 = mod.claude_usage_rows_from(self.path, offset)
        self.assertEqual((again, offset2), ([], offset))
        self.assertEqual([row[0] for row in mod.claude_usage_rows(self.path)], ["a", "b"])

    def test_append_parses_only_the_new_tail(self):
        noise = json.dumps({"type": "user", "timestamp": mod._now().isoformat(),
                             "message": {"role": "user"}, "usage": {"output_tokens": 99}})
        bad = '{"type":"assistant","usage": not-json}\n'
        first = (bad + noise + "\n" + self.line("m1", 10) + "\n").encode()
        self.path.write_bytes(first)
        reading = self.refresh()
        self.assertEqual(reading["headline"]["value"], 10)
        self.assertEqual([row[0] for row in self.cached()[4]], ["m1"])
        _, loads, opens = self.watch(self.refresh)
        self.assertEqual((loads, opens), ([], []))  # unchanged: no read

        extra = (self.line("m2", 4) + "\n" + self.line("m2", 4) + "\n").encode()
        with self.path.open("ab") as handle:
            handle.write(extra)
        offset = self.cached()[3]
        reading, loads, opens = self.watch(self.refresh)
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["seek"], [(offset, 0)])
        self.assertEqual(opens[0]["read"], [len(extra)])
        self.assertEqual(len(loads), 2)
        self.assertTrue(all("m1" not in text for text in loads))
        self.assertEqual([row[0] for row in self.cached()[4]], ["m1", "m2", "m2"])
        self.assertEqual(self.cached()[4], mod.claude_usage_rows(self.path))
        self.assertEqual(reading["headline"]["value"], 14)  # duplicate m2 counted once
        self.assertEqual(self.credits.claude_hourly()["total"], 14)

    def test_partial_trailing_line_is_not_consumed_until_completed(self):
        complete = (self.line("m1", 10) + "\n").encode()
        partial = self.line("m2", 6).encode()
        self.path.write_bytes(complete + partial[:12])
        self.refresh()
        self.assertEqual([row[0] for row in self.cached()[4]], ["m1"])
        self.assertEqual(self.cached()[3], len(complete))
        self.assertLess(self.cached()[3], self.path.stat().st_size)
        _, loads, opens = self.watch(self.refresh)
        self.assertEqual((loads, opens), ([], []))

        rest = partial[12:] + b"\n"
        with self.path.open("ab") as handle:
            handle.write(rest)
        _, loads, opens = self.watch(self.refresh)
        self.assertEqual(opens[0]["seek"], [(len(complete), 0)])
        self.assertEqual(opens[0]["read"], [len(partial[:12] + rest)])
        # The unfinished prefix plus what we just wrote, not the whole file.
        self.assertEqual(len(loads), 1)
        self.assertIn("m2", loads[0])
        self.assertEqual([row[0] for row in self.cached()[4]], ["m1", "m2"])
        self.assertEqual(self.cached()[3], self.path.stat().st_size)
        self.assertEqual(self.cached()[4], mod.claude_usage_rows(self.path))

    def test_shrink_and_replace_reparse_the_whole_file(self):
        self.path.write_text(self.line("m1", 10) + "\n" + self.line("m2", 4) + "\n")
        self.refresh()
        self.assertEqual([row[0] for row in self.cached()[4]], ["m1", "m2"])

        self.path.write_text(self.line("only", 3) + "\n")
        _, loads, _opens = self.watch(self.refresh)
        self.assertEqual(len(loads), 1)
        self.assertIn("only", loads[0])
        self.assertNotIn("m1", loads[0])
        self.assertEqual([row[0] for row in self.cached()[4]], ["only"])
        self.assertEqual(self.cached()[4], mod.claude_usage_rows(self.path))
        self.assertEqual(self.refresh()["headline"]["value"], 3)

        old_ino = self.path.stat().st_ino
        bigger = "".join(self.line(f"n{i}", i + 1) + "\n" for i in range(4))
        swapped = self.path.with_suffix(".jsonl.new")
        swapped.write_text(bigger)
        os.replace(swapped, self.path)
        self.assertNotEqual(self.path.stat().st_ino, old_ino)
        self.assertGreater(self.path.stat().st_size, len(self.line("only", 3) + "\n"))
        _, loads, opens = self.watch(self.refresh)
        self.assertEqual(opens[0]["seek"], [(0, 0)])
        self.assertEqual(len(loads), 4)
        self.assertTrue(all("only" not in text for text in loads))
        self.assertEqual([row[0] for row in self.cached()[4]], ["n0", "n1", "n2", "n3"])
        self.assertEqual(self.cached()[4], mod.claude_usage_rows(self.path))
