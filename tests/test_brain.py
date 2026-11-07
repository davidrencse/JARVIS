import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarvis import brain
from jarvis.assistant import _orchestrated


def plan_json(*steps):
    return json.dumps({"steps": list(steps)})


def S(id, do, level=2, tools=False, after=()):
    return {"id": id, "do": do, "level": level, "tools": tools, "after": list(after)}


class Stub:
    """Stand-in for the model call: records (level, prompt, role) and answers by role."""

    def __init__(self, plan, check=("PASS",), worker=None, combine="FINAL ANSWER"):
        self.plan, self.checks, self.combine = plan, list(check), combine
        self.worker = worker or (lambda level, prompt: "result")
        self.calls, self.lock = [], threading.Lock()

    def __call__(self, level, prompt, instructions=""):
        role = {id(brain.PLANNER): "plan", id(brain.WORKER): "work", id(brain.CHECKER): "check",
                id(brain.COMBINER): "combine"}[id(instructions)]
        with self.lock:
            self.calls.append((level, prompt, role))
            if role == "check":
                verdict = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
        if role == "plan":
            return self.plan
        if role == "check":
            return verdict
        if role == "combine":
            return self.combine
        return self.worker(level, prompt)

    def levels(self, role):
        return [c[0] for c in self.calls if c[2] == role]


class JudgeTests(unittest.TestCase):
    def test_simple_requests_do_not_plan(self):
        for q in ("open spotify", "tell me a joke", "what time is it"):
            self.assertFalse(brain.judge(q)["plan"], q)

    def test_research_is_level_3_plan(self):
        v = brain.judge("research the best 4k monitors under 400 dollars, compare the top three "
                        "and recommend one")
        self.assertTrue(v["plan"])
        self.assertEqual(v["level"], 3)

    def test_sequence_of_jobs_plans(self):
        v = brain.judge("check my gmail and then summarize the newest three emails and draft a "
                        "reply to the first")
        self.assertTrue(v["plan"])

    def test_disabled_in_config(self):
        v = brain.judge("research the best 4k monitors under 400 dollars, compare the top three "
                        "and recommend one", {"orchestrator": {"enabled": False}})
        self.assertFalse(v["plan"])


class ParsePlanTests(unittest.TestCase):
    def test_valid_json_plain_prose_and_fenced(self):
        raw = plan_json(S("s1", "look it up", 1), S("s2", "write it", 2, after=["s1"]))
        for reply in (raw, "Here is the plan:\n" + raw + "\nDone.", "```json\n" + raw + "\n```"):
            steps = brain.parse_plan(reply, "req")
            self.assertEqual([s["id"] for s in steps], ["s1", "s2"])
            self.assertEqual(steps[1]["after"], ["s1"])

    def test_levels_clamped(self):
        steps = brain.parse_plan(plan_json(S("s1", "a", 0), S("s2", "b", 9), S("s3", "c", "x")), "r")
        self.assertEqual([s["level"] for s in steps], [1, 3, 2])

    def test_unknown_after_dropped(self):
        steps = brain.parse_plan(plan_json(S("s1", "a"), S("s2", "b", after=["s1", "zz"])), "r")
        self.assertEqual(steps[1]["after"], ["s1"])

    def test_truncated_to_max_steps(self):
        many = [S(f"s{i}", f"do {i}") for i in range(1, brain.MAX_STEPS + 4)]
        self.assertEqual(len(brain.parse_plan(plan_json(*many), "r")), brain.MAX_STEPS)

    def test_garbage_falls_back_to_single_l2_step(self):
        for reply in ("no json here", "{not json}", '{"steps": "nope"}', ""):
            steps = brain.parse_plan(reply, "do the thing")
            self.assertEqual(len(steps), 1, reply)
            self.assertEqual((steps[0]["do"], steps[0]["level"], steps[0]["tools"]),
                             ("do the thing", 2, False))


class BudgetTests(unittest.TestCase):
    def test_cap_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            b = brain.Budget(d, 3)
            self.assertEqual([b.take(3) for _ in range(3)], [3, 3, 3])
            self.assertEqual(b.take(3), 2)
            self.assertEqual(brain.Budget(d, 3).take(3), 2)  # a new instance sees the same state
            self.assertEqual(brain.Budget(d, 3).left(), 0)

    def test_low_levels_never_consume(self):
        with tempfile.TemporaryDirectory() as d:
            b = brain.Budget(d, 1)
            for _ in range(5):
                self.assertEqual(b.take(1), 1)
                self.assertEqual(b.take(2), 2)
            self.assertEqual(b.take(3), 3)


    def test_concurrent_orchestrations_never_pass_the_cap(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIs(brain.Budget(d, 3).lock, brain.Budget(Path(d) / ".", 3).lock)
            real_load = brain.Budget._load

            def slow_load(self):  # widen the read-modify-write window
                stamps = real_load(self)
                import time
                time.sleep(0.01)
                return stamps
            granted, start = [], threading.Barrier(8)

            def one():
                start.wait()
                granted.append(brain.Budget(d, 3).take(3))  # each orchestrate() has its own
            with patch.object(brain.Budget, "_load", slow_load):
                threads = [threading.Thread(target=one) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual(granted.count(3), 3)
            self.assertEqual(len(json.loads((Path(d) / "brain-budget.json").read_text())), 3)


class OrchestrateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = tmp.name

    def run_it(self, stub, config=None, **kw):
        return brain.orchestrate("the request", config or {}, Path(self.state), self.state,
                                 ask=stub, **kw)

    def test_happy_path_and_dependency_order(self):
        plan = plan_json(S("s1", "gather facts"), S("s2", "write summary", after=["s1"]))
        stub = Stub(plan, worker=lambda lvl, p: "FACTS-OUT" if "Your step: gather" in p
                    else "SUMMARY-OUT")
        out = self.run_it(stub)
        self.assertEqual(out["reply"], "FINAL ANSWER")
        self.assertTrue(all(s["ok"] for s in out["steps"]))
        self.assertEqual([s["id"] for s in out["steps"]], ["s1", "s2"])
        work = [c[1] for c in stub.calls if c[2] == "work"]
        s2 = next(p for p in work if "write summary" in p)
        self.assertIn("FACTS-OUT", s2)
        s1 = next(p for p in work if "gather facts" in p and "write summary" not in p)
        self.assertNotIn("Results it builds on", s1)
        combine = next(c[1] for c in stub.calls if c[2] == "combine")
        self.assertIn("FACTS-OUT", combine)
        self.assertIn("SUMMARY-OUT", combine)
        self.assertEqual(stub.calls[-1][2], "combine")

    def test_failed_check_retries_once_one_level_up(self):
        stub = Stub(plan_json(S("s1", "small job", 1)), check=["FAIL too short", "FAIL still"])
        out = self.run_it(stub)
        self.assertEqual(stub.levels("work"), [1, 2])  # exactly one retry
        self.assertEqual(len(stub.levels("check")), 2)
        self.assertFalse(out["steps"][0]["ok"])
        self.assertEqual(out["reply"], "FINAL ANSWER")

    def test_retry_that_passes_marks_ok(self):
        stub = Stub(plan_json(S("s1", "small job", 1)), check=["FAIL", "PASS"])
        self.assertTrue(self.run_it(stub)["steps"][0]["ok"])

    def test_tool_step_uses_run_tools_and_is_not_retried(self):
        stub = Stub(plan_json(S("s1", "open the site", 2, tools=True)), check=["FAIL nope"])
        run_tools = MagicMock(return_value="did it")
        out = self.run_it(stub, run_tools=run_tools)
        run_tools.assert_called_once()
        self.assertIn("open the site", run_tools.call_args[0][0])
        self.assertEqual(stub.levels("work"), [])
        self.assertEqual(len(stub.levels("check")), 1)  # checked once, never repeated
        self.assertFalse(out["steps"][0]["ok"])

    def test_tool_step_without_tools_lane_is_reported(self):
        stub = Stub(plan_json(S("s1", "open the site", 2, tools=True)))
        out = self.run_it(stub, run_tools=None)
        self.assertFalse(out["steps"][0]["ok"])
        self.assertEqual(out["reply"], "FINAL ANSWER")
        combine = next(c[1] for c in stub.calls if c[2] == "combine")
        self.assertIn("NOT verified", combine)

    def test_cap_zero_plans_at_level_2(self):
        stub = Stub(plan_json(S("s1", "a")))
        out = self.run_it(stub, {"orchestrator": {"l3_per_5h": 0}})
        self.assertEqual(stub.levels("plan"), [2])
        self.assertNotIn(3, out["levels"])

    def test_planner_uses_l3_under_cap(self):
        stub = Stub(plan_json(S("s1", "a")))
        self.run_it(stub)
        self.assertEqual(stub.levels("plan"), [3])

    def test_worker_exception_fails_only_that_step(self):
        def worker(level, prompt):
            if "bad job" in prompt:
                raise RuntimeError("boom")
            return "fine"
        stub = Stub(plan_json(S("s1", "bad job"), S("s2", "good job")), worker=worker)
        out = self.run_it(stub)
        ok = {s["id"]: s["ok"] for s in out["steps"]}
        self.assertEqual(ok, {"s1": False, "s2": True})
        self.assertEqual(out["reply"], "FINAL ANSWER")


class AssistantHookTests(unittest.TestCase):
    def call(self, text, detail, on_sentence=None):
        with tempfile.TemporaryDirectory() as d:
            memory = SimpleNamespace(path=Path(d) / "memory.sqlite3")
            return _orchestrated(text, {}, Path(d), memory, MagicMock(), lambda *a, **k: None,
                                 lambda: [], on_sentence, detail)

    def test_no_plan_returns_none_and_touches_no_model(self):
        detail = {}
        with patch("jarvis.brain.orchestrate") as orch, \
                patch("jarvis.inference.ask_level") as ask:
            self.assertIsNone(self.call("what time is it", detail))
        orch.assert_not_called()
        ask.assert_not_called()
        self.assertEqual(detail, {})

    def test_plan_returns_reply_and_fills_detail(self):
        detail, spoken = {}, []
        out = {"reply": "all done", "steps": [{"id": "s1"}], "levels": [3, 2], "l3_left": 19}
        with patch("jarvis.brain.orchestrate", return_value=out) as orch:
            reply = self.call("research the best 4k monitors under 400 dollars, compare the top "
                              "three and recommend one", detail, spoken.append)
        orch.assert_called_once()
        self.assertEqual(reply, "all done")
        self.assertEqual(spoken, ["all done"])
        self.assertEqual(detail["lane"], "orchestrator")
        self.assertEqual((detail["levels"], detail["l3_left"]), ([3, 2], 19))

    def test_orchestrator_failure_returns_none(self):
        with patch("jarvis.brain.orchestrate", side_effect=RuntimeError("limited")):
            self.assertIsNone(self.call("research the best 4k monitors under 400 dollars, compare "
                                        "the top three and recommend one", {}))


class AuditOverhaulFixes(unittest.TestCase):
    """Audit 2026-11-07: F6 (checker failure keeps a worker's answer), F5 (spoken lead only)."""

    def test_checker_failure_on_a_plain_step_keeps_the_worker_result(self):
        class Boom(Stub):
            def __call__(self, level, prompt, instructions=""):
                if instructions is brain.CHECKER:
                    raise RuntimeError("both providers limited")
                return super().__call__(level, prompt, instructions)
        stub = Boom(plan_json(S("s1", "summarise it")), worker=lambda lvl, p: "WORKER-OUT")
        with tempfile.TemporaryDirectory() as d:
            res = brain.orchestrate("req", {}, Path(d), d, ask=stub)
        step = res["steps"][0]
        self.assertFalse(step["ok"])
        combined = next(c[1] for c in stub.calls if c[2] == "combine")
        self.assertIn("WORKER-OUT (unchecked)", combined)
        self.assertNotIn("this step failed", combined)

    def run_orchestrated(self, reply):
        spoken, detail = [], {}
        out = {"reply": reply, "steps": [], "levels": [3], "l3_left": 1}
        with tempfile.TemporaryDirectory() as d, \
                patch("jarvis.brain.orchestrate", return_value=out):
            memory = SimpleNamespace(path=Path(d) / "memory.sqlite3")
            got = _orchestrated("research the best 4k monitors under 400 dollars, compare the top "
                                "three and recommend one", {}, Path(d), memory, MagicMock(),
                                lambda *a, **k: None, lambda: [], spoken.append, detail)
        return got, spoken

    def test_table_and_links_are_not_spoken(self):
        reply = ("The Dell S2722QC is the pick. It is the cheapest of the three!\n\n"
                 "| Model | Price |\n|---|---|\n| Dell | $299 |\n\nhttps://example.com/dell")
        got, spoken = self.run_orchestrated(reply)
        self.assertEqual(got, reply)  # the transcript and memory keep everything
        self.assertEqual(spoken, ["The Dell S2722QC is the pick.", "It is the cheapest of the three!"])

    def test_list_right_after_the_lead_stops_speech(self):
        got, spoken = self.run_orchestrated("Here are three\n- one\n- two")
        self.assertEqual(spoken, ["Here are three"])

    def test_reply_that_opens_with_a_table_says_it_is_on_screen(self):
        got, spoken = self.run_orchestrated("| a | b |\n|---|---|")
        self.assertEqual(spoken, ["The answer is on screen."])


if __name__ == "__main__":
    unittest.main()
