"""Executor lock scope: authority calls and sinks must not stall other threads.

Waits are bounded so a regression fails the test instead of hanging the suite.
"""

import threading
import time
import unittest

from jarvis.continuity.authority import AuthorityError

from tests.continuity._fleet import DEVICES
from tests.continuity.test_executor import Base


WAIT = 3
GATE_WAIT = 4


class Gate:
    """Authority spy that can hold one status or consume call."""

    def __init__(self, inner):
        self.inner = inner
        self._mu = threading.Lock()
        self.acks = []
        self.consumes = 0
        self._arm_status = False
        self._arm_consume = None
        self.status_entered = threading.Event()
        self.consume_entered = threading.Event()
        self.release_status = threading.Event()
        self.release_consume = threading.Event()

    def arm_status(self):
        self.status_entered.clear()
        self.release_status.clear()
        self._arm_status = True

    def arm_consume(self, n):
        """Block the nth consume of this gate (1-based) until release_consume."""
        self.consume_entered.clear()
        self.release_consume.clear()
        self._arm_consume = n

    def ack_count(self):
        with self._mu:
            return len(self.acks)

    def status(self):
        if self._arm_status:
            self._arm_status = False
            self.status_entered.set()
            if not self.release_status.wait(GATE_WAIT):
                raise OSError("blocked status was not released")
        return self.inner.status()

    def acknowledge(self, *args, **kwargs):
        with self._mu:
            self.acks.append(args)
        return self.inner.acknowledge(*args, **kwargs)

    def admission(self, admission_id):
        return self.inner.admission(admission_id)

    def consume(self, **kwargs):
        with self._mu:
            self.consumes += 1
            n = self.consumes
            block = self._arm_consume == n
        if block:
            self.consume_entered.set()
            if not self.release_consume.wait(GATE_WAIT):
                raise OSError("blocked consume was not released")
        return self.inner.consume(**kwargs)


class ExecutorConcurrency(Base):
    def setUp(self):
        super().setUp()
        self.gate = Gate(self.spy)
        self._threads = []

    def tearDown(self):
        self.gate.release_status.set()
        self.gate.release_consume.set()
        for thread in self._threads:
            thread.join(WAIT)
        super().tearDown()

    def ready(self):
        self.activate()
        guard = self.guard(authority=self.gate)
        self.fence_working(guard)
        return guard

    def spawn(self, fn):
        thread = threading.Thread(target=fn, daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    def finish(self, thread, label):
        thread.join(WAIT)
        self.assertFalse(thread.is_alive(), label)

    def wait_until(self, predicate, message):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    def test_sink_does_not_block_readers_or_another_accept(self):
        guard = self.ready()
        release = threading.Event()
        entered = threading.Event()
        side_done = threading.Event()
        box = {}
        runs = []
        signed, admit = self.work(op="op-hold")
        other_signed, other_admit = self.work(op="op-other")

        def sink():
            entered.set()
            release.wait(GATE_WAIT)
            runs.append("first")

        def side():
            try:
                box["view"] = guard.local_view()
                box["receipt"] = guard.reconcile("op-hold", "a-1")
                box["stop"] = guard.stop_interactive("sess-side", "operator")
                box["other"] = guard.accept(other_signed, other_admit, lambda: runs.append("other"))
            except Exception as exc:
                box["error"] = exc
            side_done.set()

        worker = self.spawn(lambda: self._capture(box, "first", guard.accept, signed, admit, sink))
        self.assertTrue(entered.wait(WAIT), "sink did not start")
        self.spawn(side)
        self.assertTrue(side_done.wait(WAIT), "local work blocked behind the sink")
        self.assertNotIn("error", box, box.get("error"))
        self.assertTrue(box["view"]["journal"])
        self.assertEqual(box["receipt"]["state"], "ACCEPTED")
        self.assertEqual(box["stop"]["state"], "released")
        self.assertEqual(box["other"]["state"], "COMPLETED")
        self.assertFalse(box["other"]["already"])
        self.assertEqual(runs, ["other"])
        self.assertGreaterEqual(self.gate.consumes, 4)
        release.set()
        self.finish(worker, "first accept did not finish")
        self.assertEqual(runs, ["other", "first"])
        self.assertEqual(box["first"]["state"], "COMPLETED")

    def test_slow_authority_does_not_block_local_view(self):
        guard = self.ready()
        signed, admit = self.work(op="op-status")
        consume_signed, consume_admit = self.work(op="op-consume")
        box = {}

        self.gate.arm_status()
        status_worker = self.spawn(lambda: self._capture(box, "status", guard.accept, signed, admit, lambda: None))
        self.assertTrue(self.gate.status_entered.wait(WAIT), "status was not called")
        self._view_while_blocked(guard, box, "status_view")
        self.gate.release_status.set()
        self.finish(status_worker, "accept blocked in status did not finish")
        self.assertEqual(box["status"]["state"], "COMPLETED")

        self.gate.arm_consume(self.gate.consumes + 1)
        consume_worker = self.spawn(
            lambda: self._capture(box, "consume", guard.accept, consume_signed, consume_admit, lambda: None))
        self.assertTrue(self.gate.consume_entered.wait(WAIT), "consume was not called")
        self._view_while_blocked(guard, box, "consume_view")
        self.gate.release_consume.set()
        self.finish(consume_worker, "accept blocked in consume did not finish")
        self.assertEqual(box["consume"]["state"], "COMPLETED")

    def test_fence_acks_only_after_the_running_sink(self):
        guard = self.ready()
        release = threading.Event()
        entered = threading.Event()
        fence_done = threading.Event()
        box = {}
        ran = []
        signed, admit = self.work(op="op-live")
        later_signed, later_admit = self.work(op="op-later")
        status = self.store.status()
        baseline = self.gate.ack_count()

        def sink():
            entered.set()
            release.wait(GATE_WAIT)
            ran.append("live")

        worker = self.spawn(lambda: self._capture(box, "live", guard.accept, signed, admit, sink))
        self.assertTrue(entered.wait(WAIT), "sink did not start")
        self.spawn(lambda: self._capture(
            box, "fence", guard.fence, status["incarnation"], status["epoch"], status["mode_version"],
            done=fence_done))
        self.wait_until(
            lambda: guard.local_view()["fenced_epoch"] == status["epoch"] and self.gate.ack_count() == baseline,
            "fence tuple was not durable before acknowledge")
        self.assertEqual(self.gate.ack_count(), baseline)

        stale_done = threading.Event()
        self.spawn(lambda: self._capture(
            box, "stale", guard.accept, later_signed, later_admit, lambda: ran.append("later"),
            done=stale_done))
        self.assertTrue(stale_done.wait(WAIT), "accept of a fenced tuple blocked behind the sink")
        self.assertIsInstance(box["stale"], AuthorityError)
        self.assertEqual(box["stale"].code, "stale")
        self.assertEqual(ran, [])
        self.assertEqual(self.gate.ack_count(), baseline)
        self.assertFalse(fence_done.is_set(), "fence acknowledged while the sink was still running")

        release.set()
        self.assertTrue(fence_done.wait(WAIT), "fence did not finish after the sink")
        self.finish(worker, "live accept did not finish")
        self.assertNotIsInstance(box["fence"], Exception)
        self.assertEqual(box["fence"]["fenced_epoch"], status["epoch"])
        self.assertEqual(self.gate.ack_count(), baseline + 1)
        self.assertEqual(ran, ["live"])
        self.assertEqual(box["live"]["state"], "COMPLETED")

    def test_fence_between_consume_and_accept_does_not_run(self):
        guard = self.ready()
        fence_done = threading.Event()
        box = {}
        ran = []
        signed, admit = self.work(op="op-race")
        status = self.store.status()
        baseline = self.gate.ack_count()
        # The second consume is the one immediately before ACCEPTED. An identical
        # replay still returns success after the authority ack, so the guard must
        # refuse from its own fence.
        self.gate.arm_consume(2)

        worker = self.spawn(lambda: self._capture(box, "race", guard.accept, signed, admit, lambda: ran.append("ran")))
        self.assertTrue(self.gate.consume_entered.wait(WAIT), "second consume did not start")
        self.spawn(lambda: self._capture(
            box, "fence", guard.fence, status["incarnation"], status["epoch"], status["mode_version"],
            done=fence_done))
        self.assertTrue(fence_done.wait(WAIT), "fence did not finish while consume was held")
        self.assertNotIsInstance(box["fence"], Exception, box.get("fence"))
        self.assertEqual(self.gate.ack_count(), baseline + 1)
        self.gate.release_consume.set()
        self.finish(worker, "accept did not finish after the fence")
        self.assertIsInstance(box["race"], AuthorityError)
        self.assertEqual(box["race"].code, "stale")
        self.assertEqual(ran, [])
        receipt = guard.reconcile("op-race", "a-1")
        self.assertEqual(receipt["state"], "REJECTED")
        self.assertEqual(receipt["result_code"], "stale")
        self.assertNotEqual(receipt["state"], "ACCEPTED")

    def test_same_attempt_runs_the_sink_once(self):
        guard = self.ready()
        release = threading.Event()
        entered = threading.Event()
        signed, admit = self.work(op="op-once")
        runs = []
        results = []

        def sink():
            runs.append("ran")
            entered.set()
            release.wait(GATE_WAIT)

        def one():
            try:
                results.append(guard.accept(signed, admit, sink))
            except AuthorityError as exc:
                results.append(exc)

        first = self.spawn(one)
        self.assertTrue(entered.wait(WAIT), "first sink did not start")
        second = self.spawn(one)
        time.sleep(0.05)
        self.assertEqual(runs, ["ran"])
        release.set()
        self.finish(first, "first accept did not finish")
        self.finish(second, "second accept did not finish")
        self.assertEqual(runs, ["ran"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not isinstance(item, AuthorityError) for item in results), results)
        self.assertEqual(sorted(item["already"] for item in results), [False, True])
        self.assertTrue(all(item["state"] == "COMPLETED" for item in results))

    def test_same_attempt_exception_is_uncertain_once(self):
        guard = self.ready()
        signed, admit = self.work(op="op-uncertain")
        runs = []
        errors = []

        def sink():
            runs.append("ran")
            raise RuntimeError("boom")

        def one():
            try:
                guard.accept(signed, admit, sink)
                errors.append(None)
            except AuthorityError as exc:
                errors.append(exc)

        first = self.spawn(one)
        second = self.spawn(one)
        self.finish(first, "first uncertain accept did not finish")
        self.finish(second, "second uncertain accept did not finish")
        self.assertEqual(runs, ["ran"])
        self.assertEqual([exc.code for exc in errors], ["uncertain", "uncertain"])
        self.assertTrue(all("boom" not in str(exc) for exc in errors))
        self.assertEqual(guard.reconcile("op-uncertain", "a-1")["state"], "ACCEPTED")

    def test_reentry_is_busy_only_on_the_sink_thread(self):
        guard = self.ready()
        entered = threading.Event()
        other_done = threading.Event()
        box = {}
        other_ran = []
        signed, admit = self.work(op="op-outer")
        busy_signed, busy_admit = self.work(op="op-reenter")
        other_signed, other_admit = self.work(op="op-parallel")

        def sink():
            try:
                guard.accept(busy_signed, busy_admit, lambda: other_ran.append("reenter"))
                box["accept"] = "returned"
            except AuthorityError as exc:
                box["accept"] = exc.code
            try:
                guard.fence(0, 0, 0)
                box["fence"] = "returned"
            except AuthorityError as exc:
                box["fence"] = exc.code
            entered.set()
            other_done.wait(GATE_WAIT)

        def other():
            try:
                box["other"] = guard.accept(other_signed, other_admit, lambda: other_ran.append("other"))
            except AuthorityError as exc:
                box["other"] = exc
            other_done.set()

        worker = self.spawn(lambda: self._capture(box, "outer", guard.accept, signed, admit, sink))
        self.assertTrue(entered.wait(WAIT), "sink did not reach re-entry")
        self.assertEqual(box["accept"], "busy")
        self.assertEqual(box["fence"], "busy")
        self.spawn(other)
        self.assertTrue(other_done.wait(WAIT), "another thread was blocked or busy during the sink")
        self.assertEqual(other_ran, ["other"])
        self.assertEqual(box["other"]["state"], "COMPLETED")
        self.finish(worker, "outer accept did not finish")
        self.assertEqual(box["outer"]["state"], "COMPLETED")

    def test_legacy_sink_does_not_block_local_view_and_fence_waits(self):
        for device, kind in DEVICES:
            self.store.register_participant(device, device + "-inst", kind)
        guard = self.guard(authority=self.gate)
        release = threading.Event()
        entered = threading.Event()
        fence_done = threading.Event()
        box = {}
        status = self.store.status()

        def sink():
            entered.set()
            release.wait(GATE_WAIT)
            return "legacy-ok"

        worker = self.spawn(lambda: self._capture(box, "legacy", guard.legacy_effect, sink))
        self.assertTrue(entered.wait(WAIT), "legacy sink did not start")
        self._view_while_blocked(guard, box, "view")

        self.spawn(lambda: self._capture(
            box, "fence", guard.fence, status["incarnation"], status["epoch"], status["mode_version"],
            done=fence_done))
        self.wait_until(
            lambda: guard.local_view()["fenced_epoch"] == status["epoch"] and self.gate.ack_count() == 0,
            "legacy fence acked before the sink returned")
        self.assertEqual(self.gate.ack_count(), 0)
        self.assertFalse(fence_done.is_set())
        release.set()
        self.assertTrue(fence_done.wait(WAIT), "fence did not ack after the legacy sink")
        self.finish(worker, "legacy effect did not finish")
        self.assertEqual(box["legacy"], "legacy-ok")
        self.assertNotIsInstance(box["fence"], Exception, box.get("fence"))
        self.assertEqual(self.gate.ack_count(), 1)
        self.assertEqual(box["fence"]["fenced_epoch"], status["epoch"])

    def _view_while_blocked(self, guard, box, key):
        seen = threading.Event()

        def reader():
            try:
                box[key] = guard.local_view()
            except Exception as exc:
                box[key] = exc
            seen.set()

        self.spawn(reader)
        self.assertTrue(seen.wait(WAIT), "local_view blocked")
        self.assertIsInstance(box[key], dict, box[key])

    def _capture(self, box, key, fn, *args, done=None):
        try:
            box[key] = fn(*args)
        except Exception as exc:
            box[key] = exc
        finally:
            if done is not None:
                done.set()


if __name__ == "__main__":
    unittest.main()
