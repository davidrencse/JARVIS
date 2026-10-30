"""Offline regressions for bounded, isolated and checkpointed pipelines."""

import json
import queue
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.chatgpt import ChatGPTLane
from jarvis.fastlane import FastLane, close_process
from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator
from jarvis.runner import final_message
from jarvis.store import Store
from jarvis.ui import Console

ROOT = Path(__file__).resolve().parents[1]

CLAUDE = r'''
import json, sys
for line in sys.stdin:
    text = json.loads(line)["message"]["content"].splitlines()[-1]
    if text.endswith("fail"):
        print(json.dumps({"type": "result", "is_error": True}), flush=True)
        print(json.dumps({"type": "result", "result": "STALE"}), flush=True)
    else:
        print(json.dumps({"type": "stream_event", "event": {
            "delta": {"type": "text_delta", "text": "Fresh answer. "}}}), flush=True)
        print(json.dumps({"type": "result", "result": "Fresh answer."}), flush=True)
'''

CODEX = r'''
import json, sys
def send(m): print(json.dumps(m), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        send({"id": m["id"], "result": {}})
    elif m.get("method") == "thread/start":
        send({"id": m["id"], "result": {"thread": {"id": "t"}}})
    elif m.get("method") == "turn/start":
        send({"method": "item/agentMessage/delta", "params": {
            "itemId": "a", "delta": "Fresh answer. "}})
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
'''


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)


class LaneIsolationTests(Temp):
    def lane(self, cls):
        script = self.base / (cls.__name__ + ".py")
        script.write_text(CLAUDE if cls is FastLane else CODEX, encoding="utf-8")
        lane = cls({}, self.base, command=[sys.executable, str(script)], timeout=10)
        self.addCleanup(lane.close)
        return lane

    def test_failed_claude_turn_discards_remaining_output(self):
        lane = self.lane(FastLane)
        lane.warm()
        child, reader = lane.proc, lane._reader
        with self.assertRaisesRegex(RuntimeError, "Fast lane error"):
            lane.ask("fail")
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed and child.stdout.closed)
        self.assertFalse(reader.is_alive())
        self.assertEqual(lane.ask("next"), "Fresh answer.")

    def test_callback_failure_resets_both_lanes(self):
        for cls in (FastLane, ChatGPTLane):
            with self.subTest(lane=cls.__name__):
                lane = self.lane(cls)
                lane.warm()
                child = lane.proc
                with self.assertRaisesRegex(ValueError, "speaker failed"):
                    lane.ask("first", on_sentence=Mock(side_effect=ValueError("speaker failed")))
                self.assertIsNone(lane.proc)
                self.assertIsNotNone(child.poll())
                self.assertTrue(child.stdin.closed and child.stdout.closed)
                self.assertEqual(lane.ask("second"), "Fresh answer.")

    def test_repeated_close_reaps_children_and_closes_pipes(self):
        for cls in (FastLane, ChatGPTLane):
            with self.subTest(lane=cls.__name__):
                lane = self.lane(cls)
                lane.warm()
                child, reader = lane.proc, lane._reader
                lane.close()
                lane.close()
                self.assertIsNotNone(child.returncode)
                self.assertTrue(child.stdin.closed and child.stdout.closed)
                self.assertFalse(reader.is_alive())

    def test_force_killed_child_is_waited_for(self):
        child, reader = Mock(), Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired("fake", 5), 0]
        close_process(child, reader)
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)
        reader.join.assert_called_once()

    def test_busy_claude_stream_cannot_bypass_deadline(self):
        lane = FastLane({}, self.base, timeout=1)
        lane.proc = Mock()
        lane.proc.poll.return_value = None
        lane.lines = pending = queue.Queue()
        pending.put(json.dumps({"type": "noise"}))
        pending.put(json.dumps({"type": "result", "result": "too late"}))
        with patch("jarvis.fastlane.time.monotonic", side_effect=[10, 10, 12]), \
                self.assertRaisesRegex(RuntimeError, "timed out"):
            lane.ask("hello")
        self.assertEqual(pending.qsize(), 1)
        self.assertIsNone(lane.proc)

    def test_busy_codex_stream_cannot_bypass_deadline(self):
        lane = ChatGPTLane({}, self.base)
        lane.lines = pending = queue.Queue()
        pending.put('{"method": "turn/completed"}')
        with patch("jarvis.chatgpt.time.monotonic", return_value=12), \
                self.assertRaisesRegex(RuntimeError, "timed out"):
            lane._next(11)
        self.assertEqual(pending.qsize(), 1)


class TaskReadTests(Temp):
    def test_overview_matches_existing_contract_with_one_connection(self):
        store = Store(self.base)
        for n in range(25):
            task = store.add({"objective": "x" * 170 + str(n), "provider": "claude",
                              "power": "low", "role": "assistant", "mode": "read-only"})
            if n % 2:
                store.transition(task["id"], "queued", "finished")
        expected, counts = [], {}
        for row in store.list():
            counts[row["state"]] = counts.get(row["state"], 0) + 1
            if len(expected) < 14:
                spec = store.get(row["id"])["spec"]
                expected.append({**row, "objective": spec["objective"][:160],
                                 **{k: spec.get(k) for k in ("provider", "power", "role", "mode")}})
        connect, queries = store.connect, []

        @contextmanager
        def traced():
            with connect() as db:
                db.set_trace_callback(queries.append)
                yield db

        with patch.object(store, "connect", side_effect=traced) as connections:
            result = store.overview()
        self.assertEqual(result, {"recent": expected, "states": counts})
        self.assertEqual(connections.call_count, 1)
        self.assertEqual(sum(q.startswith("SELECT") for q in queries), 2)
        self.assertEqual(len(store.overview(3)["recent"]), 3)

    def test_console_uses_bounded_overview(self):
        console = Console({}, ROOT, self.base)
        self.addCleanup(console.close)
        with patch.object(console.store, "list", side_effect=AssertionError("full scan")), \
                patch.object(console.store, "get", side_effect=AssertionError("N+1 read")):
            self.assertEqual(console.overview()["tasks"], {"recent": [], "states": {}})

    def test_final_answer_reads_log_incrementally(self):
        path = self.base / "stdout.jsonl"
        path.write_text('invalid\n[]\n' + json.dumps({"type": "result", "result": "early"})
                        + '\n' + json.dumps({"type": "result", "result": " last "}),
                        encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=AssertionError("whole log read")):
            self.assertEqual(final_message("claude", self.base), "last")


class CheckpointTests(Temp):
    def setUp(self):
        super().setUp()
        self.orch = Orchestrator(Store(self.base), Memory(self.base), {}, ROOT)
        self.record = self.orch.create("goal", plan={"subtasks": [
            {"id": "a", "specialist": "scout", "objective": "inspect"}]})

    def test_memory_failure_cannot_reexecute_finished_step(self):
        with patch("jarvis.orchestrator.run_task", return_value={
                "state": "finished", "answer": "done"}) as run, \
                patch.object(self.orch.memory, "add", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.orch.run(self.record["id"])
            self.assertEqual(self.orch.load(self.record["id"])["results"]["a"]["state"], "finished")
            self.assertEqual(self.orch.run(self.record["id"])["state"], "finished")
            self.assertEqual(run.call_count, 1)

    def test_failed_step_is_checkpointed_before_memory_indexing(self):
        with patch("jarvis.orchestrator.run_task", return_value={
                "state": "failed", "answer": "partial"}) as run, \
                patch.object(self.orch.memory, "add", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.orch.run(self.record["id"])
            self.assertEqual(self.orch.run(self.record["id"])["state"], "blocked")
            self.assertEqual(run.call_count, 1)

    def test_uncertain_dispatch_is_not_retried(self):
        with patch("jarvis.orchestrator.run_task", side_effect=OSError("interrupted")) as run:
            with self.assertRaises(OSError):
                self.orch.run(self.record["id"])
            with self.assertRaisesRegex(ValueError, "without a checkpoint"):
                self.orch.run(self.record["id"])
            self.assertEqual(run.call_count, 1)

    def test_failed_replace_preserves_previous_plan(self):
        previous = self.orch.load(self.record["id"])
        self.record["state"] = "running"
        with patch("jarvis.fsio.os.replace", side_effect=OSError("busy")), \
                self.assertRaises(OSError):
            self.orch._save(self.record)
        self.assertEqual(self.orch.load(self.record["id"]), previous)
        self.assertEqual(list(self.orch.dir.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
