import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from jarvis.assistant import ask, decide
from jarvis.events import Bus, Turn, Watcher, emit, feed_sender
from jarvis.memory import Memory
from jarvis.store import Store
from jarvis.ui import Console, Handler

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "jarvis.config.json").read_text(encoding="utf-8"))


class Brain:
    """Stands in for the ChatGPT lane: streams two sentences."""

    def ask(self, text, recall=(), on_sentence=None):
        for sentence in ("It is Tuesday.", "Anything else?"):
            if on_sentence:
                on_sentence(sentence)
        return "It is Tuesday. Anything else?"


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()


class BusTests(unittest.TestCase):
    def test_replay_after_last_seen_and_unsubscribe(self):
        bus = Bus(replay=3)
        for i in range(5):
            bus.publish("x", {"i": i})
        sub, backlog = bus.subscribe(after=3)
        self.assertEqual([e["data"]["i"] for e in backlog], [3, 4])
        self.assertEqual(bus.subscribe(after=99)[1], [])  # from a previous server run
        bus.publish("y")
        self.assertEqual(sub.get_nowait()["kind"], "y")
        bus.unsubscribe(sub)
        bus.publish("z")
        self.assertTrue(sub.empty())

    def test_turn_reports_lifecycle(self):
        sent = []
        turn = Turn(lambda kind, data: sent.append((kind, data)), "voice", "hi", job="j1")
        turn.start({"route": {"tier": "simple", "reasons": ["r"]}, "message": "message:1"})
        turn.sentence("Hello.")
        turn.done({"reply": "Hello.", "tier": "simple", "memory": ["message:1", "reply:2"],
                   "streamed": True})
        kinds = [k for k, _ in sent]
        self.assertEqual(kinds, ["turn.start", "turn.sentence", "turn.done"])
        self.assertTrue(all(d["message"] == "message:1" and d["channel"] == "voice"
                            and d["job"] == "j1" for _, d in sent))
        self.assertEqual(sent[2][1]["reply_id"], "reply:2")


class WatcherTests(Temp):
    def test_relays_feed_and_state_changes(self):
        memory, store, bus = Memory(self.base), Store(self.base), Bus()
        memory.add("note", "before the console started")
        watcher = Watcher(bus, self.base, memory, store)
        watcher._voice_at = time.time() + 60  # no process checks in this test
        watcher.poll()
        self.assertEqual(bus.seq, 0)  # starts at "now"

        send = feed_sender(self.base)
        Turn(send, "voice", "hello").start({"route": {"tier": "simple"}, "message": "m:1"})
        emit(self.base, "task.forged", x=1)  # not a relayed kind
        with open(self.base / "events.jsonl", "a", encoding="utf-8") as feed:
            feed.write('{"kind": "turn.sentence", "data": {"te')  # half-written line
        item = memory.add("message", "jarvis what time is it")
        task = store.add({"objective": "x"})
        (self.base / "plans").mkdir()
        (self.base / "plans" / "plan-0000abcd.json").write_text('{"state": "planned"}')
        watcher.poll()
        got = [(e["kind"], e["data"]) for e in bus.recent]
        kinds = [k for k, _ in got]
        self.assertEqual(kinds.count("turn.start"), 1)
        self.assertNotIn("task.forged", kinds)
        self.assertNotIn("turn.sentence", kinds)  # waits for the rest of the line
        self.assertIn(("memory.added", item["id"]), [(k, d.get("id")) for k, d in got])
        self.assertIn(("task.event", task["id"]), [(k, d.get("task_id")) for k, d in got])
        self.assertIn(("plan.updated", "plan-0000abcd"), [(k, d.get("id")) for k, d in got])

        with open(self.base / "events.jsonl", "a", encoding="utf-8") as feed:
            feed.write('xt": "Hi."}}\n')
        watcher.poll()
        self.assertEqual(bus.recent[-1]["kind"], "turn.sentence")
        self.assertEqual(bus.recent[-1]["data"]["text"], "Hi.")


class AskWiringTests(Temp):
    def test_route_decision_and_channel_are_shared(self):
        self.assertEqual(decide("build a production dashboard", {}, conversational=True)["tier"],
                         "simple")
        self.assertEqual(decide("build a production dashboard", {})["tier"], "complex")
        self.assertEqual(decide("hi", {}, "medium", conversational=True)["tier"], "medium")
        starts = []
        store, memory = Store(self.base), Memory(self.base)
        result = ask("what day is it", store, memory, {}, self.base, lane=Brain(),
                     on_start=starts.append, channel="voice")
        self.assertEqual(starts[0]["message"], result["memory"][0])
        self.assertEqual(memory.get(result["memory"][0])["meta"]["channel"], "voice")


class ReviewFixTests(Temp):
    def test_repeated_question_is_a_new_message(self):
        memory = Memory(self.base)
        a = memory.add("message", "what time is it")
        b = memory.add("message", "what time is it")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(memory.add("note", "fact")["id"], memory.add("note", "fact")["id"])

    def test_emit_never_raises(self):
        emit(self.base, "voice.heard", text="bad \ud800 surrogate")
        emit(self.base / "missing" / "dir", "voice.ready")
        line = (self.base / "events.jsonl").read_text(encoding="utf-8").strip()
        self.assertEqual(json.loads(line)["data"]["text"], "bad \ud800 surrogate")


class AppModeTests(unittest.TestCase):
    def test_server_stops_when_the_last_window_leaves(self):
        from jarvis.ui import close_with_window

        class Server:
            stopped = False

            def shutdown(self):
                self.stopped = True

        bus, server = Bus(), Server()
        thread = threading.Thread(target=close_with_window, args=(server, bus, 0.2, 0.05),
                                  daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertFalse(server.stopped)  # no window has connected yet: keep serving
        sub, _ = bus.subscribe()
        time.sleep(0.2)
        bus.unsubscribe(sub)
        thread.join(2)
        self.assertTrue(server.stopped)

    def test_opens_a_chromeless_window(self):
        from unittest.mock import patch
        from jarvis import ui
        with patch.object(ui.subprocess, "Popen") as popen, \
                patch.object(ui.webbrowser, "open") as browser:
            path = ui.app_window("http://127.0.0.1:7777/")
        if path:  # a Chromium browser is installed on this machine
            self.assertIn("--app=http://127.0.0.1:7777/", popen.call_args[0][0])
            browser.assert_not_called()
        else:
            browser.assert_called_once()


class ConsoleLiveTests(Temp):
    def test_console_ask_streams_through_the_bus(self):
        console = Console(CONFIG, ROOT, self.base)
        console._lane = Brain()
        sub, _ = console.bus.subscribe()
        self.assertEqual(console.route("hello there")["handler"], "ChatGPT brain")
        job = console.submit("what is the capital of Mongolia")  # not a local-clock question
        for _ in range(100):
            if job["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "done", job.get("error"))
        events = []
        while not sub.empty():
            events.append(sub.get_nowait())
        self.assertEqual([e["kind"] for e in events],
                         ["turn.start", "turn.sentence", "turn.sentence", "turn.done"])
        self.assertTrue(all(e["data"]["job"] == job["id"] for e in events))
        self.assertEqual(job["partial"], "It is Tuesday. Anything else?")
        self.assertEqual(console.history()[-1]["channel"], "console")
        self.assertEqual(console.history()[-1]["reply"]["body"], "It is Tuesday. Anything else?")

    def test_plan_run_does_not_block_conversation(self):
        console = Console(CONFIG, ROOT, self.base)
        console._lane = Brain()
        console.jobs["p"] = {"id": "p", "state": "running", "plan": "plan-0000abcd"}
        job = console.submit("still there?")
        for _ in range(100):
            if job["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "done", job.get("error"))


class EventStreamTests(Temp):
    def setUp(self):
        super().setUp()
        self.console = Console(CONFIG, ROOT, self.base)
        handler = type("H", (Handler,), {"console": self.console,
                                          "hosts": ("127.0.0.1", "localhost")})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_streams_hello_then_live_events(self):
        self.console.bus.publish("memory.added", {"id": "note:1"})  # before connecting
        res = urllib.request.urlopen(urllib.request.Request(
            self.url + "/api/events", headers={"Last-Event-ID": "0"}), timeout=5)
        self.addCleanup(res.close)
        self.assertTrue(res.headers["Content-Type"].startswith("text/event-stream"))

        def next_event():
            while True:
                line = res.readline().decode()
                if line.startswith("data: "):
                    return json.loads(line[6:])

        self.assertEqual(next_event()["kind"], "hello")
        self.assertEqual(next_event()["data"]["id"], "note:1")  # replayed backlog
        self.console.bus.publish("turn.sentence", {"text": "Hi."})
        self.assertEqual(next_event()["data"]["text"], "Hi.")

    def test_cross_site_requests_are_refused(self):
        req = urllib.request.Request(self.url + "/api/history",
                                     headers={"Sec-Fetch-Site": "cross-site"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()  # the error object holds the response
        req = urllib.request.Request(self.url + "/api/route?text=check+my+instagram&tier=",
                                     headers={"Sec-Fetch-Site": "same-origin"})
        with urllib.request.urlopen(req, timeout=5) as res:
            self.assertEqual(json.loads(res.read())["tier"], "simple")  # auto_escalate off


if __name__ == "__main__":
    unittest.main()
