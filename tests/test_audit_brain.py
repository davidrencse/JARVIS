"""Brain and memory audit fixes: lane-full failover, lane error wrapping, overload replay,
and the cached vector matrix. Everything is faked; nothing touches a network or a CLI."""

import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from jarvis.assistant import ask
from jarvis.chatgpt import ChatGPTLane
from jarvis.memory import Memory
from jarvis.store import Store

DIM = 384


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()


class FakeFast:
    instances = []

    def __init__(self, answer="direct answer", fail=None):
        self.answer, self.fail, self.closed, self.asked = answer, fail, False, []

    def ask(self, text, recall=(), on_sentence=None):
        self.asked.append(text)
        if self.fail:
            raise self.fail
        return self.answer

    def close(self):
        self.closed = True


class Down:
    def ask(self, *_, **__):
        raise RuntimeError("ChatGPT usage limit: until 8:00 PM")


class LaneFullFallback(Temp):
    def setUp(self):
        super().setUp()
        self.store = Store(self.base / "s", lanes=1)
        self.mem = Memory(self.base / "s")
        busy = self.store.add({"objective": "orchestrator wave"})["id"]
        self.store.transition(busy, "queued", "running")  # this pid: alive, never reaped

    def states(self):
        return [t["state"] for t in self.store.list()]

    def run_ask(self, fast, lane=None):
        with patch("jarvis.assistant.LANE_WAIT", 0.3), \
                patch("jarvis.assistant._fastlane", return_value=fast):
            return ask("what is the capital of Mongolia?", self.store, self.mem, {"workspace_roots": ["."]}, self.base,
                       lane=lane or Down())

    def test_answers_directly_and_leaves_no_queued_task(self):
        fast = FakeFast()
        result = self.run_ask(fast)
        self.assertEqual(result["reply"], "direct answer")
        self.assertEqual(fast.asked, ["what is the capital of Mongolia?"])
        self.assertTrue(fast.closed)
        self.assertNotIn("queued", self.states())
        self.assertEqual(sorted(self.states()), ["failed", "running"])
        self.assertEqual(len(result["memory"]), 2)  # inbound and a reply were both stored

    def test_failed_fallback_still_replies_and_fails_the_task(self):
        result = self.run_ask(FakeFast(fail=RuntimeError("claude CLI not found on PATH.")))
        self.assertIn("claude CLI not found", result["reply"])
        self.assertNotIn("queued", self.states())

    def test_keyerror_from_lane_triggers_fallback(self):
        class Odd:
            def ask(self, *_, **__):
                return {}["thread"]

        result = self.run_ask(FakeFast(), lane=Odd())
        self.assertEqual(result["reply"], "direct answer")
        self.assertIn("thread", result["lane_error"])

    def test_oserror_from_lane_triggers_fallback(self):
        class Gone:
            def ask(self, *_, **__):
                raise OSError("spawn failed")

        self.assertEqual(self.run_ask(FakeFast(), lane=Gone())["reply"], "direct answer")


class LaneErrorWrapping(Temp):
    def lane(self, **tier):
        return ChatGPTLane({"models": {"chatgpt": tier}}, self.base)

    def test_keyerror_and_typeerror_become_runtimeerror(self):
        for exc in (KeyError("thread"), TypeError("bad"), ValueError("bad json")):
            lane = self.lane()
            with patch.object(lane, "_ask", side_effect=exc):
                with self.assertRaises(RuntimeError) as caught:
                    lane.ask("hi")
            self.assertIs(caught.exception.__cause__, exc)

    def test_spawn_oserror_becomes_runtimeerror(self):
        lane = ChatGPTLane({}, self.base, command=[str(self.base / "no-such-binary")])
        self.addCleanup(lane.close)
        with self.assertRaises(RuntimeError):
            lane.ask("hi")

    def test_keyboardinterrupt_is_not_swallowed(self):
        lane = self.lane()
        with patch.object(lane, "_ask", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                lane.ask("hi")


class OverloadReplay(Temp):
    def lane(self):
        return ChatGPTLane({"models": {"chatgpt": {"model": "big", "fallback_model": "small"}}},
                           self.base)

    def test_overload_without_tools_retries_on_fallback_model(self):
        lane = self.lane()
        models = []

        def fake(text, recall, on_sentence):
            models.append(lane.model)
            if len(models) == 1:
                raise RuntimeError("serverOverloaded")
            return "ok"

        with patch.object(lane, "_ask", side_effect=fake):
            self.assertEqual(lane.ask("hi"), "ok")
        self.assertEqual(models, ["big", "small"])

    def test_overload_after_a_tool_ran_is_not_replayed(self):
        lane = self.lane()
        calls = []

        def fake(text, recall, on_sentence):
            calls.append(lane.model)
            lane.tool_log.append(("open_app", True, "opened"))
            raise RuntimeError("serverOverloaded")

        with patch.object(lane, "_ask", side_effect=fake):
            with self.assertRaises(RuntimeError) as caught:
                lane.ask("open notepad")
        self.assertEqual(calls, ["big"])
        self.assertIn("open_app", str(caught.exception))
        self.assertEqual(lane.model, "big")

    def test_stale_tool_log_from_an_earlier_turn_does_not_block_retry(self):
        lane = self.lane()
        lane.tool_log = [("old", True, "")]
        seen = []

        def fake(text, recall, on_sentence):
            seen.append(1)
            if len(seen) == 1:
                raise RuntimeError("serverOverloaded")
            return "ok"

        with patch.object(lane, "_ask", side_effect=fake):
            self.assertEqual(lane.ask("hi"), "ok")


def _vec(text):
    v = np.zeros(DIM, dtype=np.float32)
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in ("thesis", "dissertation"):
            v[0] += 1
        else:
            v[16 + int(hashlib.md5(word.encode()).hexdigest(), 16) % (DIM - 16)] += 1
    n = np.linalg.norm(v)
    return v / n if n else v


class Embedder:
    def __init__(self):
        self.calls = []

    def __call__(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        return np.stack([_vec(t) for t in texts]).astype(np.float32)


class CachedVectors(Temp):
    def setUp(self):
        super().setUp()
        self.embedder = Embedder()
        patcher = patch("jarvis.embed.embed", self.embedder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mem = Memory(self.base, semantic=True)

    def test_cached_matrix_is_not_reread_and_new_rows_are_picked_up(self):
        a = self.mem.add("note", "thesis draft")["id"]
        self.mem.add("note", "grocery milk eggs")
        self.mem.index_vectors()
        self.assertEqual(self.mem.similar("dissertation", index=False)[0][0], a)
        # Wreck the stored blob of an already-cached row (same rowid): a recall that still
        # finds it is reading the in-memory matrix, not the table.
        with self.mem.connect() as db:
            db.execute("UPDATE vectors SET vec=? WHERE id=?",
                       (np.zeros(DIM, dtype=np.float32).tobytes(), a))
        self.assertEqual(self.mem.similar("dissertation", index=False)[0][0], a)
        # A second Memory on the same file shares the cache.
        self.assertEqual(Memory(self.base, semantic=True).similar(
            "dissertation", index=False)[0][0], a)
        # New rows appear once embedded, without a full reload.
        b = self.mem.add("note", "thesis chapter two")["id"]
        self.assertNotIn(b, [i for i, _ in self.mem.similar("dissertation", index=False)])
        self.mem.index_vectors()
        self.assertIn(b, [i for i, _ in self.mem.similar("dissertation", index=False)])

    def test_where_filter_uses_cache(self):
        self.mem.add("note", "thesis draft")
        t = self.mem.add("task", "thesis submit")["id"]
        self.mem.index_vectors()
        res = self.mem.similar("dissertation", where=("i.kind=?",), args=("task",), index=False)
        self.assertEqual([i for i, _ in res], [t])

    def test_search_does_not_embed_new_items_on_the_request_path(self):
        self.mem.add("note", "thesis draft")
        self.mem.index_vectors()
        new = self.mem.add("note", "thesis appendix material")["id"]
        self.embedder.calls.clear()
        with patch.object(Memory, "_index_in_background") as background:
            hits = self.mem.search("thesis appendix")
        self.assertEqual(self.embedder.calls, [["thesis appendix"]])  # only the query
        self.assertIn(new, [h["id"] for h in hits])  # keyword recall still finds it
        background.assert_called_once()

    def test_background_indexer_catches_up(self):
        a = self.mem.add("note", "thesis draft")["id"]
        self.mem._index_in_background()
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.mem.connect() as db:
                if db.execute("SELECT COUNT(*) FROM vectors WHERE id=?", (a,)).fetchone()[0]:
                    break
            time.sleep(0.05)
        self.assertEqual(self.mem.similar("dissertation", index=False)[0][0], a)


if __name__ == "__main__":
    unittest.main()
