"""A tool call that STARTED but never finished must not be replayed (P2 prep, 2026-11-11).

ChatGPTLane/FastLane log only finished calls (tool_log) but report every call to on_tool, with
ok=None at the start. A usage limit or crash between the two left tool_log empty, so Failover
re-ran the turn on the Claude stand-in and assistant.py's own fallbacks ran it a third time.
Failover now records started tools per request and puts them on the exception
(exc.tool_unfinished); the assistant says "started, can't tell" and does not run it again.
Fakes only: no model, CLI, network or OS input. This is a conservative no-replay rule, not a
lease or fence."""

import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from jarvis import assistant
from jarvis.inference import Failover
from jarvis.memory import Memory
from jarvis.store import Store

LIMIT = RuntimeError("ChatGPT error: You've hit your usage limit. Try again at 11:34 PM.")
OTHER = RuntimeError("ChatGPT lane failed: EOF")


class Lane:
    """Fake lane. For each ask it emits `events` through on_tool like the real lanes
    ((key, name, ok) with ok None = started), then raises `error` or returns `reply`."""

    def __init__(self, reply="ok", error=None, events=(), finished=(), thread=False):
        self.reply, self.error, self.events, self._finished = reply, error, list(events), list(finished)
        self.thread = thread  # emit from another thread, like nothing real does today
        self.tool_log, self.on_tool, self.asked, self.closed = [], None, [], False

    def emit(self, key, name, ok, text=""):
        def go():
            if self.on_tool:
                self.on_tool(key, name, ok, text)
        if self.thread:
            t = threading.Thread(target=go)
            t.start()
            t.join()
        else:
            go()

    def ask(self, text, recall=(), on_sentence=None):
        self.asked.append(text)
        self.tool_log = []
        for key, name, ok in self.events:
            self.emit(key, name, ok)
        self.tool_log = list(self._finished)
        if self.error:  # a fresh exception each time: Failover annotates what it re-raises
            raise type(self.error)(*self.error.args)
        return self.reply

    def pick(self, text):
        return "act"

    def warm(self):
        pass

    def close(self):
        self.closed = True


def no_limit():
    return mock.patch("jarvis.chatgpt.usage_limit", return_value="")


STARTED = [("c1", "jarvis.open_app", None)]


class StartedToolBlocksReplay(unittest.TestCase):
    def failover(self, primary):
        self.made = []

        def make():
            lane = Lane("Claude answered.")
            self.made.append(lane)
            return lane
        return Failover(primary, make)

    def test_start_then_limit_without_finish_is_not_replayed(self):
        fo = self.failover(Lane(error=LIMIT, events=STARTED))
        with no_limit(), self.assertRaises(RuntimeError) as caught:
            fo.ask("open spotify")
        self.assertEqual(self.made, [])  # the stand-in never even started
        self.assertEqual(caught.exception.tool_unfinished, ["jarvis.open_app"])
        self.assertTrue(caught.exception.spoken)  # caller: do not answer this request again
        self.assertIn("usage limit", str(caught.exception))  # the real error is not rewritten

    def test_blocks_with_no_external_callback_and_tool_log_empty(self):
        primary = Lane(error=LIMIT, events=STARTED)
        fo = self.failover(primary)
        self.assertIsNone(fo.on_tool)
        with no_limit(), self.assertRaises(RuntimeError):
            fo.ask("open spotify")
        self.assertEqual((primary.tool_log, self.made), ([], []))

    def test_external_callback_still_sees_every_event(self):
        seen = []
        fo = self.failover(Lane(error=LIMIT, events=STARTED + [("c2", "jarvis.x", True)]))
        fo.on_tool = lambda key, name, ok, text: seen.append((key, name, ok))
        with no_limit(), self.assertRaises(RuntimeError):
            fo.ask("go")
        self.assertEqual(seen, [("c1", "jarvis.open_app", None), ("c2", "jarvis.x", True)])

    def test_a_raising_external_callback_neither_hides_the_start_nor_fails_the_turn(self):
        def boom(*args):
            raise ValueError("ui exploded")
        fo = self.failover(Lane(error=LIMIT, events=STARTED))
        fo.on_tool = boom
        with no_limit(), self.assertRaises(RuntimeError) as caught:
            fo.ask("open spotify")
        self.assertIn("usage limit", str(caught.exception))  # not the callback's ValueError
        self.assertEqual((self.made, caught.exception.tool_unfinished), ([], ["jarvis.open_app"]))
        # and with no failure the reply still comes back
        fo = self.failover(Lane("done", events=STARTED))
        fo.on_tool = boom
        with no_limit():
            self.assertEqual(fo.ask("open spotify"), "done")

    def test_completed_call_still_blocks(self):
        fo = self.failover(Lane(error=LIMIT, finished=[("open_app", True, "ok")]))
        with no_limit(), self.assertRaises(RuntimeError):
            fo.ask("open spotify")
        self.assertEqual(self.made, [])

    def test_pure_no_tool_limit_still_fails_over(self):
        fo = self.failover(Lane(error=LIMIT))
        with no_limit():
            self.assertEqual(fo.ask("what time is it"), "Claude answered.")
        self.assertEqual(len(self.made), 1)

    def test_limited_up_front_goes_straight_to_the_stand_in(self):
        primary = Lane(events=STARTED)
        fo = self.failover(primary)
        with mock.patch("jarvis.chatgpt.usage_limit", return_value="until 11:34 PM"):
            self.assertEqual(fo.ask("open spotify"), "Claude answered.")
        self.assertEqual(primary.asked, [])

    def test_start_then_non_limit_error_is_raised_with_the_unfinished_tool(self):
        fo = self.failover(Lane(error=OTHER, events=STARTED))
        with no_limit(), self.assertRaises(RuntimeError) as caught:
            fo.ask("open spotify")
        self.assertEqual(str(caught.exception), str(OTHER))
        self.assertEqual(caught.exception.tool_unfinished, ["jarvis.open_app"])
        self.assertEqual(self.made, [])

    def test_a_start_on_the_stand_in_is_reported_too(self):
        err = RuntimeError("Fast lane timed out.")
        fo = Failover(Lane(error=LIMIT), lambda: Lane(error=err, events=[("s1", "Bash", None)]))
        with no_limit(), self.assertRaises(RuntimeError) as caught:
            fo.ask("run it")
        self.assertEqual(caught.exception.tool_unfinished, ["Bash"])

    def test_started_tool_from_another_thread_is_charged_to_the_running_request(self):
        fo = self.failover(Lane(error=LIMIT, events=STARTED, thread=True))
        with no_limit(), self.assertRaises(RuntimeError) as caught:
            fo.ask("open spotify")
        self.assertEqual((self.made, caught.exception.tool_unfinished), ([], ["jarvis.open_app"]))

    def test_no_state_leaks_into_the_next_request_or_after_close(self):
        primary = Lane(error=LIMIT, events=STARTED)
        fo = self.failover(primary)
        with no_limit(), self.assertRaises(RuntimeError):
            fo.ask("open spotify")
        self.assertEqual(getattr(fo._local, "stack", []), [])
        self.assertEqual([r for lanes in fo._requests.values() for r in lanes], [])
        primary.events, primary.error = [], LIMIT  # next request: nothing started, plain limit
        with no_limit():
            self.assertEqual(fo.ask("what time is it"), "Claude answered.")
        fo.close()
        primary.error = None
        with no_limit():
            self.assertEqual(fo.ask("hi"), "ok")


class ConcurrencyAndNesting(unittest.TestCase):
    def test_concurrent_requests_do_not_contaminate_each_other(self):
        """A's tool starts and then the limit hits; B (no tool) hits the same limit while A
        is still in flight on the shared lane. B may fail over; A may not."""
        a_started, b_may_fail = threading.Event(), threading.Event()

        class Shared(Lane):
            def ask(self, text, recall=(), on_sentence=None):
                if text == "A":
                    self.emit("a1", "jarvis.open_app", None)
                    a_started.set()
                    b_may_fail.wait(5)
                    raise RuntimeError(*LIMIT.args)
                a_started.wait(5)  # A's start is already on record when B fails
                b_may_fail.set()
                raise RuntimeError(*LIMIT.args)

        made = []
        fo = Failover(Shared(), lambda: made.append(Lane("Claude answered.")) or made[-1])
        out = {}

        def run(name):
            try:
                out[name] = fo.ask(name)
            except RuntimeError as exc:
                out[name] = exc
        with no_limit():
            threads = [threading.Thread(target=run, args=(n,)) for n in "AB"]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(out["B"], "Claude answered.")
        self.assertEqual(made[0].asked, ["B"])
        self.assertEqual(out["A"].tool_unfinished, ["jarvis.open_app"])
        self.assertFalse(hasattr(out["B"], "tool_unfinished"))

    def test_nested_request_without_a_tool_does_not_block_the_outer_fallback(self):
        made = []
        holder = {}

        class Outer(Lane):
            def ask(self, text, recall=(), on_sentence=None):
                if text == "outer":
                    inner = holder["fo"].ask("inner")  # no tool, plain limit: falls over
                    assert inner == "Claude answered.", inner
                raise RuntimeError(*LIMIT.args)

        fo = holder["fo"] = Failover(Outer(), lambda: made.append(Lane("Claude answered.")) or made[-1])
        with no_limit():
            self.assertEqual(fo.ask("outer"), "Claude answered.")
        self.assertEqual(made[0].asked, ["inner", "outer"])
        self.assertEqual(getattr(fo._local, "stack", []), [])

    def test_a_tool_started_in_a_nested_request_blocks_the_outer_replay_too(self):
        """Conservative: the nested ask is part of the outer turn, so its started tool is."""
        made = []
        holder = {}

        class Outer(Lane):
            def ask(self, text, recall=(), on_sentence=None):
                if text == "outer":
                    try:
                        holder["fo"].ask("inner")
                    except RuntimeError:
                        pass
                    raise RuntimeError(*LIMIT.args)
                self.emit("n1", "jarvis.open_app", None)
                raise RuntimeError(*LIMIT.args)

        fo = holder["fo"] = Failover(Outer(), lambda: made.append(Lane("c")) or made[-1])
        with no_limit(), self.assertRaises(RuntimeError) as caught:
            fo.ask("outer")
        self.assertEqual((made, caught.exception.tool_unfinished), ([], ["jarvis.open_app"]))


class AssistantNeverReplaysAnAmbiguousEffect(unittest.TestCase):
    """assistant.ask: a lane error after a tool started must not fall back to a ledger run,
    the warm Claude lane or a one-shot lane, and must not claim the tool did or did not run."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.addCleanup(assistant._STANDIN.update, lane=None)
        self.store = Store(self.base / "s", lanes=1)
        self.mem = Memory(self.base / "s")

    def run_ask(self, lane):
        def nope(*a, **k):
            raise AssertionError("a second run was started after a tool began")
        with mock.patch("jarvis.assistant.run_task", side_effect=nope), \
                mock.patch("jarvis.assistant._fastlane", side_effect=nope), \
                mock.patch("jarvis.assistant._standin_reply", side_effect=nope), \
                no_limit():
            return assistant.ask("open spotify and play jazz", self.store, self.mem,
                                 {"workspace_roots": ["."]}, self.base, lane=lane)

    def check(self, result):
        reply = result["reply"]
        self.assertIn("jarvis.open_app started", reply)
        self.assertIn("can't tell whether it happened", reply)
        self.assertNotIn("failed", reply)  # no lie either way
        self.assertNotIn("jarvis.open_app ran", reply)
        self.assertEqual(self.store.list(), [])

    def test_limit_after_start_through_failover(self):
        self.check(self.run_ask(Failover(Lane(error=LIMIT, events=STARTED),
                                         lambda: self.fail("stand-in"))))

    def test_non_limit_error_after_start_through_failover(self):
        self.check(self.run_ask(Failover(Lane(error=RuntimeError("ChatGPT lane failed: EOF"),
                                              events=STARTED), lambda: self.fail("stand-in"))))

    def test_completed_tool_still_reported_as_ran(self):
        result = self.run_ask(Failover(Lane(error=LIMIT, finished=[("open_app", True, "ok")]),
                                       lambda: self.fail("stand-in")))
        self.assertIn("open_app ran", result["reply"])

    def test_pure_no_tool_limit_still_reaches_the_stand_in(self):
        stand_in = Lane("Claude answered.")
        fo = Failover(Lane(error=LIMIT), lambda: stand_in)
        with no_limit():
            result = assistant.ask("what is the capital of Mongolia?", self.store, self.mem,
                                   {"workspace_roots": ["."]}, self.base, lane=fo)
        self.assertEqual(result["reply"], "Claude answered.")

    def orchestrated(self, lane, orchestrate_error=RuntimeError("all steps failed")):
        """assistant._orchestrated with a planner that runs one tools step then fails."""
        def fake(text, config, base, state, run_tools=None, step=None, recall=(), level=None):
            try:
                run_tools("open spotify")
            except Exception:
                pass  # brain.orchestrate keeps going after a failed step...
            raise orchestrate_error  # ...and here everything failed: caller used to retry
        detail = {}
        with mock.patch("jarvis.brain.judge", return_value={"plan": True, "reasons": ["x"]}), \
                mock.patch("jarvis.brain.orchestrate", side_effect=fake), no_limit():
            reply = assistant._orchestrated("open spotify", {}, self.base, self.mem, lane,
                                            lambda *a, **k: None, lambda: [], None, detail)
        return reply, detail

    def test_orchestrator_failure_after_a_started_tool_does_not_hand_back_to_the_brain(self):
        lane = Failover(Lane(error=LIMIT, events=STARTED), lambda: self.fail("stand-in"))
        reply, detail = self.orchestrated(lane)
        self.assertIn("jarvis.open_app started", reply)
        self.assertEqual(detail["tools"], [["jarvis.open_app", None]])

    def test_orchestrator_failure_with_no_tool_still_returns_none_for_the_brain(self):
        lane = Failover(Lane(error=LIMIT), lambda: Lane(error=LIMIT))
        reply, detail = self.orchestrated(lane)
        self.assertIsNone(reply)

    def test_later_planner_failure_preserves_an_earlier_successful_effect(self):
        lane = Lane(reply="done", finished=[("open_app", True, "opened")])
        reply, detail = self.orchestrated(lane)
        self.assertIn("open_app ran", reply)
        self.assertEqual(detail["state"], "partial")
        self.assertEqual(detail["tools"], [["open_app", True]])

    def test_two_threads_keep_a_finished_tool_when_the_shared_log_is_cleared(self):
        """Thread B takes the lane after A's ask returns and before the orchestrator reads.
        The shared tool_log is then B's. The reply must still say open_app ran."""
        from jarvis.chatgpt import ChatGPTLane
        lane = ChatGPTLane({"models": {"chatgpt": {"fallback_model": "second"}}}, str(self.base))
        fo = Failover(lane, lambda: self.fail("stand-in"))
        released, cleared = threading.Event(), threading.Event()

        def scripted(text, recall=(), on_sentence=None):
            if text == "open spotify":
                lane.tool_log.append(("open_app", True, "opened"))
            return "done"

        real = lane.ask

        def wrapped(text, recall=(), on_sentence=None):
            result = real(text, recall, on_sentence)
            if text == "open spotify":
                released.set()
                self.assertTrue(cleared.wait(3), "thread B did not reset the shared log")
            return result

        def other():
            self.assertTrue(released.wait(3))
            real("other request")
            cleared.set()

        def planner(text, config, base, state, run_tools=None, step=None, recall=(), level=None):
            run_tools("open spotify")
            raise RuntimeError("planner failed after the tool step")

        detail = {}
        with mock.patch.object(lane, "ask", side_effect=wrapped), \
                mock.patch.object(lane, "_ask", side_effect=scripted), no_limit(), \
                mock.patch("jarvis.chatgpt.note_usage_limit"), \
                mock.patch("jarvis.brain.judge", return_value={"plan": True, "reasons": ["x"]}), \
                mock.patch("jarvis.brain.orchestrate", side_effect=planner):
            worker = threading.Thread(target=other)
            worker.start()
            reply = assistant._orchestrated(
                "open spotify", {}, self.base, self.mem, fo,
                lambda *a, **k: None, lambda: [], None, detail)
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(lane.tool_log, [])  # B replaced the shared list
        self.assertEqual(lane.turn_log(), [("open_app", True, "opened")])
        self.assertIn("open_app ran", reply)
        self.assertEqual(detail.get("state"), "partial")
        self.assertIsNotNone(reply)  # None would hand the same tool to the brain lane

    def test_partial_reply_wording(self):
        text = assistant._partial_reply([("a", True, "")], ["b"])
        self.assertIn("a ran", text)
        self.assertIn("b started but I never saw it finish", text)
        self.assertNotIn("b failed", text)
        self.assertNotIn("started", assistant._partial_reply([("a", False, "")]))


class _Proc:
    """In-memory Claude stream. ask() must not spawn a provider process."""

    def __init__(self):
        self.stdin = self

    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class _Immediate(queue.Queue):
    """get() never blocks: an empty stream is the lane's timeout."""

    def get(self, block=True, timeout=None):
        if self.empty():
            raise queue.Empty
        return queue.Queue.get(self, block=False)


def _tool_use(name, key="c1"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": key, "name": name}]}}


def _tool_result(name, key="c1", text="opened"):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": key, "content": text}]}}


def _answer(text="done"):
    return {"type": "result", "result": text}


def _lane_error(subtype, detail=None):
    event = {"type": "result", "is_error": True, "subtype": subtype}
    if detail is not None:
        event["result"] = detail
    return event


OPENED = [_tool_use("open_app"), _tool_result("open_app", text="opened"), _answer("done")]
OPENED_BOOM = [_tool_use("open_app"), _tool_result("open_app", text="opened"), _lane_error("boom")]
OPENED_LIMIT = [_tool_use("open_app"), _tool_result("open_app", text="opened"),
                _lane_error("error", "You've hit your usage limit.")]
VOLUME = [_tool_use("volume", key="v1"), _tool_result("volume", key="v1", text="set"), _answer("ok")]


class FastLaneEvidenceSurvivesUnlock(unittest.TestCase):
    """Real FastLane / Failover / assistant. The CLI is an in-memory event stream.
    No provider process. A second thread mutates the shared log only after A's lock drops."""

    def setUp(self):
        from jarvis import fastlane
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.addCleanup(assistant._STANDIN.update, lane=None)
        self.store = Store(self.base / "s", lanes=1)
        self.mem = Memory(self.base / "s")
        fastlane._CLAUDE_LIMIT.update(until=0.0, text="")
        self.addCleanup(fastlane._CLAUDE_LIMIT.update, until=0.0, text="")

    def config(self):
        return {"workspace_roots": ["."], "brain": {"situation": False},
                "routing": {"fast_path": False, "agent_fallback": False}}

    def fast(self, events):
        from jarvis.fastlane import FastLane
        lane = FastLane({}, self.base, command=["jarvis-test-no-spawn"], timeout=5)
        self.arm(lane, events)

        def refuse():
            raise AssertionError("FastLane tried to start a provider process")
        lane._spawn = refuse
        return lane

    def arm(self, lane, events):
        lane.proc = _Proc()
        lane.lines = _Immediate()
        lane._reader = None
        lane.turns = 0
        for event in events:
            lane.lines.put(json.dumps(event))

    def interleave(self, lane, watch, other, expect=None, unfinished=None):
        """Pause on `watch` after ask() has left the lane lock. `other` runs then."""
        released, cleared = threading.Event(), threading.Event()
        box, seen = {}, []
        real = lane.ask

        def wrapped(text, recall=(), on_sentence=None):
            seen.append(text)
            try:
                result = real(text, recall, on_sentence)
            except BaseException as exc:
                if text == watch:
                    box["exc"] = exc
                    if expect is not None:
                        self.assertEqual(exc.tool_log, expect)
                        self.assertEqual(lane.turn_log(), expect)
                    if unfinished is False:
                        self.assertFalse(hasattr(exc, "tool_unfinished"))
                    released.set()
                    self.assertTrue(cleared.wait(5), "other thread did not finish")
                raise
            else:
                if text == watch:
                    if expect is not None:
                        self.assertEqual(lane.turn_log(), expect)
                        self.assertEqual(list(lane.tool_log), expect)
                    released.set()
                    self.assertTrue(cleared.wait(5), "other thread did not finish")
                return result

        def side():
            try:
                self.assertTrue(released.wait(5), "the watched ask never unlocked")
                other(real)
            except BaseException as exc:  # surfaced after the main assert, not as a hang
                box["side"] = exc
            finally:
                cleared.set()

        lane.ask = wrapped
        worker = threading.Thread(target=side)
        worker.start()
        return worker, box, seen

    def finish(self, worker, box):
        worker.join(5)
        if worker.is_alive():
            self.fail("interleave thread still running")
        side = box.get("side")
        if side is not None:
            raise side

    def second_ask(self, lane, real, events):
        """B's ask, after A's lock is gone. Rebinds the shared log through FastLane.ask."""
        def install():
            self.arm(lane, events)
        lane._spawn = install
        if lane.proc is not None:  # success path: the process is still the scripted one
            install()
        real("other")

    def ask_planned(self, fo, usage="until 11:34 PM"):
        def planner(text, config, base, state, run_tools=None, step=None, recall=(), level=None):
            try:
                run_tools("open spotify")
            except Exception:
                pass  # brain.orchestrate keeps going, then the plan itself fails
            raise RuntimeError("planner failed after the tool step")

        with mock.patch("jarvis.assistant.run_task", side_effect=AssertionError("ledger")), \
                mock.patch("jarvis.assistant._fastlane", side_effect=AssertionError("talk lane")), \
                mock.patch("jarvis.assistant._standin_reply",
                           side_effect=AssertionError("stand-in reply")), \
                mock.patch("jarvis.chatgpt.usage_limit", return_value=usage), \
                mock.patch("jarvis.brain.judge", return_value={"plan": True, "reasons": ["x"]}), \
                mock.patch("jarvis.brain.orchestrate", side_effect=planner):
            return assistant.ask("open spotify and play jazz", self.store, self.mem,
                                 self.config(), self.base, lane=fo)

    def test_cleared_log_keeps_the_finished_tool_and_skips_a_second_ask(self):
        primary = Lane(error=AssertionError("primary was asked"))
        lane = self.fast(OPENED)
        fo = Failover(primary, lambda: lane)
        worker, box, seen = self.interleave(
            lane, "open spotify", lambda real: self.second_ask(lane, real, [_answer("other")]),
            expect=[("open_app", True, "opened")])
        try:
            result = self.ask_planned(fo)
        finally:
            self.finish(worker, box)
        self.assertEqual(primary.asked, [])
        self.assertEqual(seen, ["open spotify"])  # not asked again with the original text
        self.assertEqual(lane.tool_log, [])  # B's ask rebound the shared list
        self.assertEqual(lane.turn_log(), [("open_app", True, "opened")])
        self.assertEqual(fo.turn_log(), [("open_app", True, "opened")])
        self.assertIn("open_app ran", result["reply"])
        self.assertEqual(result.get("state"), "partial")
        self.assertEqual(result.get("tools"), [["open_app", True]])

    def test_other_threads_tool_is_not_attributed_to_the_first(self):
        primary = Lane(error=AssertionError("primary was asked"))
        lane = self.fast(OPENED)
        fo = Failover(primary, lambda: lane)
        worker, box, seen = self.interleave(
            lane, "open spotify", lambda real: self.second_ask(lane, real, VOLUME),
            expect=[("open_app", True, "opened")])
        try:
            result = self.ask_planned(fo)
        finally:
            self.finish(worker, box)
        self.assertEqual(seen, ["open spotify"])
        self.assertEqual(lane.tool_log, [("volume", True, "set")])
        self.assertEqual(fo.turn_log(), [("open_app", True, "opened")])
        self.assertIn("open_app ran", result["reply"])
        self.assertNotIn("volume", result["reply"])
        self.assertEqual(result.get("tools"), [["open_app", True]])

    def test_error_after_a_finished_tool_keeps_a_copy_when_the_log_is_cleared(self):
        # ChatGPT is already limited, so the tools lane is the FastLane stand-in.
        # Once with a plain lane error, once with limit wording. Neither may replay.
        from jarvis import fastlane
        for events in (OPENED_BOOM, OPENED_LIMIT):
            with self.subTest(kind=events[-1]["subtype"]):
                fastlane._CLAUDE_LIMIT.update(until=0.0, text="")
                primary = Lane(error=AssertionError("primary was asked"))
                lane = self.fast(events)
                fo = Failover(primary, lambda current=lane: current)
                worker, box, seen = self.interleave(
                    lane, "open spotify",
                    lambda real, current=lane: self.second_ask(current, real, [_answer("other")]),
                    expect=[("open_app", True, "opened")], unfinished=False)
                try:
                    result = self.ask_planned(fo)
                finally:
                    self.finish(worker, box)
                self.assertEqual(primary.asked, [])
                self.assertEqual(seen, ["open spotify"])
                self.assertEqual(box["exc"].tool_log, [("open_app", True, "opened")])
                self.assertFalse(hasattr(box["exc"], "tool_unfinished"))
                self.assertEqual(lane.tool_log, [])
                self.assertIn("open_app ran", result["reply"])
                self.assertNotIn("can't tell", result["reply"])
                self.assertEqual(result.get("state"), "partial")

    def test_raw_start_timeout_blocks_the_ledger(self):
        lane = self.fast([_tool_use("mcp__jarvis__open_app")])
        box = {}
        real = lane.ask

        def wrapped(text, recall=(), on_sentence=None):
            try:
                return real(text, recall, on_sentence)
            except BaseException as exc:
                box["exc"] = exc
                raise

        lane.ask = wrapped
        with mock.patch("jarvis.assistant.run_task", side_effect=AssertionError("ledger")), \
                mock.patch("jarvis.assistant._fastlane", side_effect=AssertionError("talk lane")), \
                mock.patch("jarvis.assistant._standin_reply",
                           side_effect=AssertionError("stand-in reply")), \
                mock.patch("jarvis.brain.judge", return_value={"plan": False, "reasons": []}):
            result = assistant.ask("open spotify and play jazz", self.store, self.mem,
                                   self.config(), self.base, lane=lane)
        self.assertEqual(box["exc"].tool_unfinished, ["open_app"])
        self.assertEqual(box["exc"].tool_log, [])
        self.assertIn("open_app started", result["reply"])
        self.assertIn("can't tell whether it happened", result["reply"])
        self.assertNotIn("open_app ran", result["reply"])
        self.assertEqual(result.get("tools"), [["open_app", None]])

    def test_empty_limit_still_reaches_the_stand_in(self):
        """An explicit empty tool_log is this turn. Another thread's tool must not block it."""
        lane = self.fast([_lane_error("error", "You've hit your usage limit.")])
        stand = Lane("Claude answered.")

        def other(real):
            lane.tool_log.append(("sneak", True, "in"))  # same list would show on the exception
            lane.tool_log = [("volume", True, "set")]

        worker, box, seen = self.interleave(lane, "what time is it", other, expect=[])
        fo = Failover(lane, lambda: stand)
        try:
            with no_limit():
                reply = fo.ask("what time is it")
        finally:
            self.finish(worker, box)
        self.assertEqual(reply, "Claude answered.")
        self.assertEqual(stand.asked, ["what time is it"])
        self.assertEqual(seen, ["what time is it"])
        self.assertEqual(box["exc"].tool_log, [])
        self.assertFalse(hasattr(box["exc"], "tool_unfinished"))
        self.assertEqual(lane.tool_log, [("volume", True, "set")])
        self.assertEqual(lane.turn_log(), [])
        self.assertEqual(assistant._request_tools(lane), [])

    def test_empty_failure_still_reaches_the_ledger(self):
        lane = self.fast([_lane_error("boom")])
        calls = []

        def other(real):
            lane.tool_log.append(("sneak", True, "in"))
            lane.tool_log = [("volume", True, "set")]

        def ledger(store, task_id, config, base, timeout):
            calls.append(task_id)
            return {"answer": "LEDGER", "task_id": task_id, "state": "finished"}

        worker, box, seen = self.interleave(lane, "what is the capital of Mongolia?", other, expect=[])
        try:
            with mock.patch("jarvis.assistant.run_task", side_effect=ledger), \
                    mock.patch("jarvis.assistant._fastlane", side_effect=AssertionError("talk lane")), \
                    mock.patch("jarvis.assistant._standin_reply",
                               side_effect=AssertionError("stand-in reply")), \
                    mock.patch("jarvis.brain.judge", return_value={"plan": False, "reasons": []}):
                result = assistant.ask("what is the capital of Mongolia?", self.store, self.mem,
                                       self.config(), self.base, lane=lane)
        finally:
            self.finish(worker, box)
        self.assertEqual(seen, ["what is the capital of Mongolia?"])
        self.assertEqual(calls, [result["task"]])
        self.assertEqual(result["reply"], "LEDGER")
        self.assertNotIn("volume", result["reply"])
        self.assertEqual(box["exc"].tool_log, [])
        self.assertFalse(hasattr(box["exc"], "tool_unfinished"))
        self.assertEqual(lane.turn_log(), [])

    def test_cooldown_before_any_tool_still_fails_over(self):
        """Claude's own cooldown runs nothing. A stale shared log must not block the stand-in."""
        from jarvis import fastlane
        from jarvis.fastlane import FastLane
        fastlane.note_claude_limit(RuntimeError("Claude usage limit reached"))
        lane = FastLane({}, self.base)  # no command: refuse before a process exists
        lane.tool_log = [("old", True, "stale")]

        def refuse():
            raise AssertionError("spawned")
        lane._spawn = refuse
        stand = Lane("Claude answered.")
        fo = Failover(lane, lambda: stand)
        with no_limit():
            reply = fo.ask("what time is it")
        self.assertEqual(reply, "Claude answered.")
        self.assertEqual(stand.asked, ["what time is it"])
        self.assertEqual(lane.tool_log, [("old", True, "stale")])
        self.assertIsNone(lane.turn_log())


if __name__ == "__main__":
    unittest.main()
