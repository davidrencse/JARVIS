"""Plan ownership/reconciliation checks: never call a real model."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.locking import exclusive_lock
from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator
from jarvis.store import Store

ROOT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.store, self.memory = Store(self.base), Memory(self.base)
        self.orch = Orchestrator(self.store, self.memory, {"workspace_roots": ["."]}, ROOT)
        self.record = self.orch.create("inspect project", plan={"subtasks": [
            {"id": "a", "specialist": "scout", "objective": "inspect"}]})
        self.plan_id = self.record["id"]

    def dispatched(self, state="queued", answer=None):
        task = self.store.add({"objective": "inspect", "provider": "claude"})
        if state != "queued":
            self.store.transition(task["id"], "queued", "running")
        if state not in ("queued", "running"):
            self.store.transition(task["id"], "running", state)
        if answer is not None:
            folder = self.base / "runs" / task["id"]
            folder.mkdir(parents=True)
            (folder / "stdout.jsonl").write_text(json.dumps({"type": "result", "result": answer}),
                                                encoding="utf-8")
        self.record["state"] = "running"
        self.record["tasks"]["a"] = task["id"]
        self.orch._save(self.record)
        return task["id"]

    def test_inspection_leaves_plan_ledger_and_memory_unchanged(self):
        self.dispatched("finished", "answer")
        before = [p.read_bytes() for p in (self.orch._path(self.plan_id), self.store.path,
                                         self.memory.path)]
        report = self.orch.recover(self.plan_id)
        self.assertFalse(report["applied"])
        self.assertEqual(report["steps"][0]["action"], "restore_checkpoint")
        self.assertEqual(before, [p.read_bytes() for p in (self.orch._path(self.plan_id),
                                                         self.store.path, self.memory.path)])

    def test_finished_execution_is_adopted_without_launching_again(self):
        task_id = self.dispatched("finished", "A completed answer.")
        with patch("jarvis.orchestrator.run_task") as run:
            report = self.orch.recover(self.plan_id, apply=True)
            record = self.orch.run(self.plan_id)
        run.assert_not_called()
        self.assertEqual(report["state"], "finished")
        self.assertEqual(record["results"]["a"]["task"], task_id)
        self.assertEqual(record["results"]["a"]["answer"], "A completed answer.")
        self.assertIn("memory_id", record["results"]["a"])

    def test_failed_and_interrupted_work_stays_blocked(self):
        for state in ("failed", "timed_out", "interrupted"):
            with self.subTest(state=state):
                self.dispatched(state, "partial")
                report = self.orch.recover(self.plan_id, apply=True)
                self.assertEqual(report["state"], "blocked")
                with patch("jarvis.orchestrator.run_task") as run:
                    self.assertEqual(self.orch.run(self.plan_id)["state"], "blocked")
                run.assert_not_called()

    def test_queued_recovery_reuses_original_task_and_requires_explicit_run(self):
        task_id = self.dispatched()
        with patch("jarvis.orchestrator.run_task") as run:
            self.assertEqual(self.orch.recover(self.plan_id, apply=True)["state"], "planned")
        run.assert_not_called()

        def complete(store, claimed_id, *_):
            self.assertEqual(claimed_id, task_id)
            self.assertEqual(self.orch.load(self.plan_id).get("resume_tasks"), {})
            store.transition(claimed_id, "queued", "running")
            store.transition(claimed_id, "running", "finished")
            return {"state": "finished", "answer": "done"}

        with patch("jarvis.orchestrator.run_task", side_effect=complete):
            self.assertEqual(self.orch.run(self.plan_id)["state"], "finished")
        self.assertEqual(len(self.store.list()), 1)

    def test_direct_task_runner_can_win_without_duplicate_dispatch(self):
        task_id = self.dispatched()
        self.orch.recover(self.plan_id, apply=True)
        self.store.transition(task_id, "queued", "running")

        def claim(store, claimed_id, *_):
            self.assertEqual(claimed_id, task_id)
            store.transition(claimed_id, "queued", "running")
            self.fail("the already claimed task must not execute again")

        with patch("jarvis.orchestrator.run_task", side_effect=claim), self.assertRaises(ValueError):
            self.orch.run(self.plan_id)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.orch.load(self.plan_id).get("resume_tasks"), {})

    def test_running_or_missing_task_refuses_apply(self):
        self.dispatched("running")
        before = self.orch._path(self.plan_id).read_bytes()
        with self.assertRaisesRegex(ValueError, "inspection"):
            self.orch.recover(self.plan_id, apply=True)
        self.assertEqual(self.orch._path(self.plan_id).read_bytes(), before)
        self.record["tasks"]["a"] = "missing"
        self.orch._save(self.record)
        with self.assertRaisesRegex(ValueError, "inspection"):
            self.orch.recover(self.plan_id, apply=True)

    def test_missing_finished_output_is_reported_and_never_rerun(self):
        self.dispatched("finished")
        report = self.orch.recover(self.plan_id, apply=True)
        self.assertEqual(report["missing_output"], ["a"])
        self.assertEqual(self.orch.load(self.plan_id)["results"]["a"]["answer"], "")
        with patch("jarvis.orchestrator.run_task") as run:
            self.orch.run(self.plan_id)
        run.assert_not_called()

    def test_index_repair_is_idempotent_including_long_output(self):
        answer = "evidence " * 3000
        task_id = self.dispatched("finished", answer)
        with patch.object(self.memory, "add", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.orch.recover(self.plan_id, apply=True)
        self.assertEqual(self.orch.load(self.plan_id)["results"]["a"]["task"], task_id)
        self.orch.recover(self.plan_id, apply=True)
        self.orch.recover(self.plan_id, apply=True)
        hits = self.memory.search("", kind="result")
        self.assertEqual(len(hits), 1)
        self.assertEqual(self.memory.get(hits[0]["id"])["body"], answer.strip())

    def test_invalid_limits_are_rejected_before_dispatch(self):
        for kwargs in ({"max_steps": 0}, {"max_steps": 21}, {"timeout": 0}, {"timeout": 3601}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.orch.run(self.plan_id, **kwargs)
        self.assertEqual(self.store.list(), [])

    def test_run_and_recovery_lock_before_loading(self):
        with exclusive_lock(self.orch._path(self.plan_id).with_suffix(".lock")), \
                patch.object(self.orch, "load", side_effect=AssertionError("loaded before lock")):
            for operation in (self.orch.run, self.orch.recover):
                with self.assertRaisesRegex(ValueError, "busy"):
                    operation(self.plan_id)

    def test_exceptions_release_lock(self):
        with patch("jarvis.orchestrator.run_task", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.orch.run(self.plan_id)
        self.assertEqual(self.orch.recover(self.plan_id)["steps"][0]["state"], "queued")

    def test_recover_cli_inspects_and_applies_without_provider(self):
        self.dispatched("finished", "CLI recovery")
        command = [sys.executable, "-m", "jarvis", "--state", str(self.base),
                   "orchestrate", "recover", self.plan_id]
        for suffix, applied in (([], False), (["--apply"], True)):
            result = subprocess.run(command + suffix, cwd=ROOT, capture_output=True,
                                    text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["applied"], applied)

    def test_process_contention_and_crash_release(self):
        # Child runs the actual Orchestrator.run, holding ownership while its fake
        # provider waits. Termination simulates a crashed process, not a lease timer.
        script = self.base / "worker.py"
        script.write_text('''
import sys, time
from pathlib import Path
from unittest.mock import patch
from jarvis.orchestrator import Orchestrator
from jarvis.memory import Memory
from jarvis.store import Store
state, root, plan = sys.argv[1:]
def pause(*args):
    print("OWNED", flush=True)
    time.sleep(120)
with patch("jarvis.orchestrator.run_task", pause):
    Orchestrator(Store(state), Memory(state), {}, Path(root)).run(plan)
''', encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        child = subprocess.Popen([sys.executable, str(script), str(self.base), str(ROOT), self.plan_id],
                                 cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            # Poll via communicate on a bounded helper thread would obscure errors;
            # use a marker file-independent stdout future with a bounded deadline.
            import queue
            import threading
            output = queue.Queue()
            reader = threading.Thread(target=lambda: output.put(child.stdout.readline()), daemon=True)
            reader.start()
            self.assertEqual(output.get(timeout=15).strip(), "OWNED")
            reader.join(timeout=1)
            other = Orchestrator(Store(self.base), Memory(self.base), {}, ROOT)
            with self.assertRaisesRegex(ValueError, "busy"):
                other.run(self.plan_id)
            with self.assertRaisesRegex(ValueError, "busy"):
                other.recover(self.plan_id, apply=True)
            child.kill()
            child.wait(timeout=10)
            # A stale lock file is harmless after the OS closes the crashed owner.
            self.assertTrue(self.orch._path(self.plan_id).with_suffix(".lock").is_file())
            self.assertEqual(other.recover(self.plan_id)["steps"][0]["state"], "queued")
            self.assertEqual(len(self.store.list()), 1)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            child.stdout.close()
            child.stderr.close()


if __name__ == "__main__":
    unittest.main()
