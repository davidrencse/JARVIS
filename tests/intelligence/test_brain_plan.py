"""Planner level, plan parsing, and ChatGPT lane reuse in brain.orchestrate."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import brain, inference


def plan_json(*steps):
    return json.dumps({"steps": list(steps)})


def S(id, do, level=2, tools=False, after=()):
    return {"id": id, "do": do, "level": level, "tools": tools, "after": list(after)}


class ParseEdgesTests(unittest.TestCase):
    def test_prose_and_trailing_braces_keep_the_plan(self):
        raw = plan_json(S("s1", "look it up", 1), S("s2", "write it", 2, after=["s1"]))
        reply = ("Here is the plan:\n" + raw
                 + '\nIgnore {"note": "x"} and the set {a, b}.')
        steps = brain.parse_plan(reply, "req")
        self.assertEqual([s["id"] for s in steps], ["s1", "s2"])
        self.assertEqual(steps[1]["after"], ["s1"])

    def test_an_earlier_object_without_steps_is_skipped(self):
        raw = plan_json(S("s1", "only this"))
        reply = '{"steps": "nope", "aside": "{not a plan}"} ' + raw + " trailing {brace}"
        steps = brain.parse_plan(reply, "req")
        self.assertEqual([s["id"] for s in steps], ["s1"])
        self.assertEqual(steps[0]["do"], "only this")

    def test_forward_after_is_kept(self):
        steps = brain.parse_plan(plan_json(S("s1", "second", after=["s2"]), S("s2", "first")), "r")
        self.assertEqual(steps[0]["after"], ["s2"])
        self.assertEqual(steps[1]["after"], [])

    def test_self_after_is_dropped(self):
        steps = brain.parse_plan(plan_json(S("s1", "alone", after=["s1", "s1"])), "r")
        self.assertEqual(steps[0]["after"], [])

    def test_duplicate_ids_are_renamed_before_edges_resolve(self):
        steps = brain.parse_plan(plan_json(
            S("s1", "keep"),
            S("s1", "rename", after=["s1"]),
        ), "r")
        self.assertEqual([s["id"] for s in steps], ["s1", "s2"])
        self.assertEqual(steps[1]["after"], ["s1"])

    def test_forward_after_runs_the_later_step_first(self):
        order = []

        def ask(level, prompt, instructions=""):
            if instructions is brain.PLANNER:
                return plan_json(S("s1", "second job", after=["s2"]), S("s2", "first job"))
            if instructions is brain.CHECKER:
                return "PASS"
            if instructions is brain.COMBINER:
                return "DONE"
            if "Your step: first job" in prompt:
                order.append("s2")
                return "ONE"
            if "Your step: second job" in prompt:
                order.append("s1")
                self.assertIn("ONE", prompt)
                return "TWO"
            self.fail(prompt)

        with tempfile.TemporaryDirectory() as d:
            out = brain.orchestrate("req", {}, Path(d), d, ask=ask)
        self.assertEqual(order, ["s2", "s1"])
        self.assertEqual([s["id"] for s in out["steps"]], ["s2", "s1"])


class PlannerLevelTests(unittest.TestCase):
    def labels(self, level, config=None):
        seen = []

        def ask(lvl, prompt, instructions=""):
            seen.append(("ask", lvl, instructions))
            if instructions is brain.PLANNER:
                return plan_json(S("s1", "a", 1))
            if instructions is brain.CHECKER:
                return "PASS"
            return "ok"

        trace = []

        def step(*args):
            trace.append(args)

        with tempfile.TemporaryDirectory() as d:
            brain.orchestrate("req", config or {}, Path(d), d, ask=ask, step=step, level=level)
        plan = [args[2] for args in trace if args and args[0] == "plan"]
        asked = [lvl for kind, lvl, instr in seen if instr is brain.PLANNER]
        return plan, asked

    def test_given_level_is_the_label_and_the_call(self):
        labels, asked = self.labels(1)
        self.assertEqual(labels, ["Plan · L1", "Plan · L1"])
        self.assertEqual(asked, [1])

    def test_omitted_level_still_plans_at_l3(self):
        labels, asked = self.labels(None)
        self.assertEqual(labels, ["Plan · L3", "Plan · L3"])
        self.assertEqual(asked, [3])

    def test_cap_label_is_the_granted_level(self):
        labels, asked = self.labels(3, {"orchestrator": {"l3_per_5h": 0}})
        self.assertEqual(labels, ["Plan · L2", "Plan · L2"])
        self.assertEqual(asked, [2])


class _Lane:
    """Stand-in for ChatGPTLane. Records overlap on this instance."""

    made = []

    def __init__(self, config, base, max_turns=1, timeout=180, tier_key="chatgpt",
                 instructions="", overrides=(), **kwargs):
        self.tier_key = tier_key
        self.instructions = instructions
        self.timeout = timeout
        self.overrides = overrides
        self.closed = 0
        self.asks = []
        self.inflight = 0
        self.max_inflight = 0
        self._lock = threading.Lock()
        _Lane.made.append(self)

    def ask(self, prompt):
        with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            self.asks.append((self.instructions, prompt))
            if self.instructions is brain.WORKER and "Your step:" in prompt:
                _Lane.workers.wait(timeout=2)
            time.sleep(0.05)
            if self.instructions is brain.PLANNER:
                return _Lane.plan
            if self.instructions is brain.CHECKER:
                return "PASS"
            if self.instructions is brain.COMBINER:
                return "COMBINED"
            return "worked"
        finally:
            with self._lock:
                self.inflight -= 1

    def close(self):
        self.closed += 1


class _Claude:
    made = []

    def __init__(self, *args, **kwargs):
        self.asked = []
        self.closed = 0
        _Claude.made.append(self)

    def ask(self, text):
        self.asked.append(text)
        return "from-claude"

    def close(self):
        self.closed += 1


def _quiet_limit():
    return patch("jarvis.chatgpt.usage_limit", return_value="")


class LaneReuseTests(unittest.TestCase):
    def setUp(self):
        _Lane.made = []
        _Lane.plan = plan_json(S("s1", "alpha", 2), S("s2", "beta", 1))
        _Lane.workers = threading.Barrier(2)
        _Claude.made = []

    def test_one_lane_per_level_across_hops_then_closed_once(self):
        with tempfile.TemporaryDirectory() as d, \
                _quiet_limit(), \
                patch("jarvis.chatgpt.ChatGPTLane", _Lane), \
                patch("jarvis.fastlane.FastLane", _Claude):
            out = brain.orchestrate("the request", {}, Path(d), d)
        self.assertEqual(out["reply"], "COMBINED")
        self.assertTrue(all(step["ok"] for step in out["steps"]))
        self.assertEqual(_Claude.made, [])
        by_tier = {}
        for lane in _Lane.made:
            by_tier.setdefault(lane.tier_key, []).append(lane)
        # L3 plans, L2 works and combines, L1 works and checks. One process each.
        self.assertEqual(set(by_tier), {"chatgpt", "chatgpt_l2", "chatgpt_l3"})
        for lanes in by_tier.values():
            self.assertEqual(len(lanes), 1, lanes[0].tier_key)
            self.assertEqual(lanes[0].closed, 1)
            self.assertEqual(lanes[0].max_inflight, 1)
            self.assertEqual(lanes[0].overrides, ['web_search="disabled"'])
        l2 = by_tier["chatgpt_l2"][0]
        self.assertIn(brain.WORKER, [instr for instr, _ in l2.asks])
        self.assertIn(brain.COMBINER, [instr for instr, _ in l2.asks])
        self.assertGreaterEqual(len(l2.asks), 2)
        l1 = by_tier["chatgpt"][0]
        self.assertIn(brain.WORKER, [instr for instr, _ in l1.asks])
        self.assertIn(brain.CHECKER, [instr for instr, _ in l1.asks])
        self.assertGreaterEqual(len(l1.asks), 2)

    def test_lanes_close_when_the_planner_raises(self):
        class Boom(_Lane):
            def ask(self, prompt):
                raise RuntimeError("planner down")

        with tempfile.TemporaryDirectory() as d, \
                _quiet_limit(), \
                patch("jarvis.chatgpt.ChatGPTLane", Boom), \
                patch("jarvis.fastlane.FastLane", _Claude):
            with self.assertRaisesRegex(RuntimeError, "planner down"):
                brain.orchestrate("req", {}, Path(d), d)
        self.assertEqual(len(Boom.made), 1)
        self.assertEqual(Boom.made[0].closed, 1)
        self.assertEqual(_Claude.made, [])

    def test_same_level_calls_do_not_overlap_and_a_limit_uses_claude(self):
        lanes = inference.LevelLanes()
        overlap = []
        state = {"inflight": 0}

        class Slow(_Lane):
            def ask(self, prompt):
                with self._lock:
                    state["inflight"] += 1
                    if state["inflight"] > 1:
                        overlap.append(state["inflight"])
                try:
                    time.sleep(0.05)
                    if prompt == "limited":
                        raise RuntimeError("usage limit reached")
                    return "ok"
                finally:
                    with self._lock:
                        state["inflight"] -= 1

        start = threading.Barrier(3)
        errors = []

        def one(prompt):
            try:
                start.wait(timeout=2)
                return inference.ask_level({}, Path("."), 2, prompt, "RULES", lanes=lanes)
            except Exception as exc:
                errors.append(exc)
                return None

        # Three share the level-2 lane; the fourth hits a limit after they finish.
        with _quiet_limit(), patch("jarvis.chatgpt.ChatGPTLane", Slow), \
                patch("jarvis.fastlane.FastLane", _Claude):
            threads = [threading.Thread(target=one, args=("q",)) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(overlap, [])
            self.assertEqual(len(Slow.made), 1)
            self.assertEqual(Slow.made[0].closed, 0)
            reply, engine = inference.ask_level({}, Path("."), 2, "limited", "RULES", lanes=lanes)
        self.assertEqual((reply, engine), ("from-claude", "claude"))
        self.assertEqual(_Claude.made[0].asked, ["RULES\n\nlimited"])
        self.assertEqual(Slow.made[0].closed, 1)  # limit closes the reused lane
        lanes.close()  # already dropped; must not close it again
        self.assertEqual(Slow.made[0].closed, 1)

    def test_ask_level_without_a_cache_still_closes_each_lane(self):
        with _quiet_limit(), patch("jarvis.chatgpt.ChatGPTLane", _Lane), \
                patch("jarvis.fastlane.FastLane", _Claude):
            _Lane.plan = "ok"
            first, engine = inference.ask_level({}, Path("."), 1, "hi", brain.WORKER)
            second, engine2 = inference.ask_level({}, Path("."), 1, "hi again", brain.WORKER)
        self.assertEqual((first, engine, second, engine2), ("worked", "chatgpt", "worked", "chatgpt"))
        self.assertEqual(len(_Lane.made), 2)
        self.assertTrue(all(lane.closed == 1 for lane in _Lane.made))
        self.assertEqual(_Claude.made, [])


if __name__ == "__main__":
    unittest.main()
