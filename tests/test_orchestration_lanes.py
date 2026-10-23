"""Run lanes: the ledger's concurrency limit, and the orchestrator running a ready wave.

Never calls a real model; run_task is replaced throughout.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator, validate_plan
from jarvis.router import specialists
from jarvis.store import MAX_LANES, Store, lanes_from

ROOT = Path(__file__).resolve().parents[1]
CATALOG = specialists(ROOT)
CONFIG = {"workspace_roots": ["."]}


def finished(answer):
    return {"task_id": "t", "state": "finished", "answer": answer}


class LaneTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def claim(self, store, n):
        """Start n tasks running; returns their ids."""
        ids = []
        for i in range(n):
            task = store.add({"objective": f"job {i}"})["id"]
            store.transition(task, "queued", "running")
            ids.append(task)
        return ids

    def test_one_lane_still_means_one_task(self):
        store = Store(self.base)
        self.assertEqual(store.lanes, 1)
        self.claim(store, 1)
        spare = store.add({"objective": "spare"})["id"]
        with self.assertRaisesRegex(ValueError, "1 run lane in this ledger"):
            store.transition(spare, "queued", "running")

    def test_a_second_lane_lets_a_second_task_run(self):
        store = Store(self.base, lanes=2)
        running = self.claim(store, 2)
        self.assertEqual([store.get(t)["state"] for t in running], ["running", "running"])
        third = store.add({"objective": "third"})["id"]
        with self.assertRaisesRegex(ValueError, "2 run lanes in this ledger"):
            store.transition(third, "queued", "running")
        # Freeing one lane lets the next task in.
        store.transition(running[0], "running", "finished")
        store.transition(third, "queued", "running")
        self.assertEqual(store.get(third)["state"], "running")

    def test_claiming_a_task_someone_else_took_says_so(self):
        store = Store(self.base, lanes=3)
        task = self.claim(store, 1)[0]
        with self.assertRaisesRegex(ValueError, "Task must be queued"):
            store.transition(task, "queued", "running")

    def test_racing_claims_never_exceed_the_lanes(self):
        """The claim is one UPDATE whose WHERE counts running rows, so the count is read
        under the write lock. Twelve threads must still leave exactly three running."""
        store = Store(self.base, lanes=3)
        tasks = [store.add({"objective": f"race {i}"})["id"] for i in range(12)]
        start, won, lost = threading.Barrier(len(tasks)), [], []

        def grab(task_id):
            start.wait(timeout=10)
            try:
                store.transition(task_id, "queued", "running")
                won.append(task_id)
            except ValueError:
                lost.append(task_id)

        threads = [threading.Thread(target=grab, args=(t,)) for t in tasks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        with store.connect() as db:
            running = db.execute("SELECT COUNT(*) FROM tasks WHERE state='running'").fetchone()[0]
        self.assertEqual(running, 3)
        self.assertEqual(len(won), 3)
        self.assertEqual(len(won) + len(lost), 12)

    def test_lane_count_is_clamped(self):
        self.assertEqual(lanes_from({}), 1)
        self.assertEqual(lanes_from({"orchestration": {"lanes": 99}}), MAX_LANES)
        self.assertEqual(lanes_from({"orchestration": {"lanes": 0}}), 1)
        self.assertEqual(lanes_from({"orchestration": {"lanes": "two"}}), 1)
        self.assertEqual(Store(self.base, lanes=99).lanes, MAX_LANES)


class WaveTests(unittest.TestCase):
    PLAN = {"summary": "two independent reads, then a review", "subtasks": [
        {"id": "a", "specialist": "scout", "objective": "map one half"},
        {"id": "b", "specialist": "researcher", "objective": "map the other half"},
        {"id": "c", "specialist": "reviewer", "objective": "review both", "depends_on": ["a", "b"]},
    ]}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.memory = Memory(self.base)

    def orch(self, lanes):
        store = Store(self.base, lanes=lanes)
        return Orchestrator(store, self.memory, CONFIG, ROOT)

    def test_independent_steps_run_in_one_wave(self):
        """Both must be in flight at once: the barrier only clears if they overlap."""
        orch = self.orch(lanes=2)
        plan = orch.create("map the project", plan=self.PLAN)
        together = threading.Barrier(2, timeout=5)

        def fake(store, task_id, config, base, timeout):
            together.wait()  # raises if the other step never arrives
            return finished("mapped")

        with patch("jarvis.orchestrator.run_task", side_effect=fake):
            record = orch.run(plan["id"], max_steps=2)
        self.assertEqual({sid: r["state"] for sid, r in record["results"].items()},
                         {"a": "finished", "b": "finished"})
        self.assertEqual(record["state"], "planned")  # c is ready but max_steps stopped us

    def test_a_dependent_step_waits_for_its_wave(self):
        orch = self.orch(lanes=4)
        plan = orch.create("map the project", plan=self.PLAN)
        waves = []

        def fake(store, task_id, config, base, timeout):
            waves.append(store.get(task_id)["spec"]["objective"])
            return finished("done")

        with patch("jarvis.orchestrator.run_task", side_effect=fake):
            record = orch.run(plan["id"])
        self.assertEqual(record["state"], "finished")
        # c reviews both halves, so it can only be dispatched after them.
        self.assertEqual(waves[-1], "review both")
        self.assertEqual(sorted(waves[:2]), ["map one half", "map the other half"])

    def test_one_lane_runs_the_same_plan_one_step_at_a_time(self):
        orch = self.orch(lanes=1)
        plan = orch.create("map the project", plan=self.PLAN)
        live, peak = [], []

        def fake(store, task_id, config, base, timeout):
            live.append(task_id)
            peak.append(len(live))
            live.pop()
            return finished("done")

        with patch("jarvis.orchestrator.run_task", side_effect=fake):
            record = orch.run(plan["id"])
        self.assertEqual(record["state"], "finished")
        self.assertEqual(max(peak), 1)

    def test_a_failed_step_does_not_discard_its_siblings_answer(self):
        """An ordinary failure: run_task reports it, so the wave records both outcomes."""
        orch = self.orch(lanes=2)
        plan = orch.create("map the project", plan=self.PLAN)

        def fake(store, task_id, config, base, timeout):
            if store.get(task_id)["spec"]["objective"] == "map one half":
                return {"task_id": task_id, "state": "failed", "answer": None}
            return finished("the other half is mapped")

        with patch("jarvis.orchestrator.run_task", side_effect=fake):
            record = orch.run(plan["id"])
        self.assertEqual(record["state"], "blocked")
        self.assertEqual(record["results"]["a"]["state"], "failed")
        self.assertEqual(record["results"]["b"]["state"], "finished")
        self.assertEqual(record["results"]["b"]["answer"], "the other half is mapped")
        self.assertNotIn("c", record["results"])  # never reached

    def test_a_raising_step_propagates_but_checkpoints_its_sibling_first(self):
        """A raise means the bookkeeping is unsafe, so it must reach the caller. The step
        beside it already finished, and that answer must survive on disk."""
        orch = self.orch(lanes=2)
        plan = orch.create("map the project", plan=self.PLAN)

        def fake(store, task_id, config, base, timeout):
            if store.get(task_id)["spec"]["objective"] == "map one half":
                raise OSError("the ledger went away")
            return finished("the other half is mapped")

        with patch("jarvis.orchestrator.run_task", side_effect=fake):
            with self.assertRaises(OSError):
                orch.run(plan["id"])
        saved = orch.load(plan["id"])
        self.assertEqual(saved["results"]["b"]["answer"], "the other half is mapped")
        # The raising step keeps its dispatch and no result, so the next run refuses to
        # guess and recover() has to look at it.
        self.assertIn("a", saved["tasks"])
        self.assertNotIn("a", saved["results"])
        with self.assertRaisesRegex(ValueError, "without a checkpoint"):
            orch.run(plan["id"])

    def test_every_dispatch_is_checkpointed_before_the_wave_starts(self):
        """A crash mid-wave must leave both steps recoverable by their own task ids."""
        orch = self.orch(lanes=2)
        plan = orch.create("map the project", plan=self.PLAN)
        seen = {}

        def fake(store, task_id, config, base, timeout):
            seen.update(orch.load(plan["id"])["tasks"])
            return finished("done")

        with patch("jarvis.orchestrator.run_task", side_effect=fake):
            orch.run(plan["id"], max_steps=2)
        self.assertEqual(sorted(seen), ["a", "b"])  # both on disk before either finished


class OwnedPathTests(unittest.TestCase):
    def plan(self, *paths):
        return {"subtasks": [
            {"id": f"s{i}", "specialist": "implementer", "objective": "write",
             "mode": "workspace-write", "owned_paths": [p]}
            for i, p in enumerate(paths)]}

    def test_identical_paths_are_refused(self):
        with self.assertRaisesRegex(ValueError, "overlaps"):
            validate_plan(self.plan("src/app.py", "src/app.py"), CATALOG)

    def test_a_folder_and_a_file_inside_it_are_refused(self):
        """Writers run side by side now, so nesting is a real collision."""
        for a, b in (("src", "src/app.py"), ("src/app.py", "src"),
                     ("src/", "src/deep/app.py"), ("a/b", "a/b/c/d.py")):
            with self.subTest(a=a, b=b), self.assertRaisesRegex(ValueError, "overlaps"):
                validate_plan(self.plan(a, b), CATALOG)

    def test_windows_separators_are_compared_the_same_way(self):
        with self.assertRaisesRegex(ValueError, "overlaps"):
            validate_plan(self.plan(r"src\app.py", "src/app.py"), CATALOG)

    def test_siblings_are_allowed(self):
        plan = validate_plan(self.plan("src/app.py", "src/other.py", "docs"), CATALOG)
        self.assertEqual(len(plan["subtasks"]), 3)

    def test_a_shared_prefix_that_is_not_a_folder_is_allowed(self):
        validate_plan(self.plan("src/app.py", "src/app.py.bak"), CATALOG)
        validate_plan(self.plan("web", "website"), CATALOG)


if __name__ == "__main__":
    unittest.main()
