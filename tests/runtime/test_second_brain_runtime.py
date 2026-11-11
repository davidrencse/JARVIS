"""Second-brain C2 runtime (jarvis/second_brain_runtime.py): drainer gates, lifecycle, backoff
and safe status. A fake queue stands in for jarvis.second_brain_queue; a fake vault and state live
in a temp folder. No real vault, state, model, service or network is touched."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis import second_brain_runtime as rt  # noqa: E402

SECRET = "SECRETBODY-do-not-leak"


class FakeQueue:
    def __init__(self, report=None, counts=None, error=None):
        self.report = report if report is not None else {"ok": True, "acked": 0, "remaining": 0}
        self.counts, self.error = counts or {}, error
        self.configured, self.drains, self.statuses = [], [], 0

    def configure(self, state_dir, enabled=True):
        self.configured.append((str(state_dir), enabled))

    def drain(self, state_dir, vault_root, limit=100):
        self.drains.append((str(state_dir), str(vault_root), limit))
        if self.error:
            raise self.error
        return self.report

    def status(self, state_dir):
        self.statuses += 1
        return {"ok": True, "enabled": True, "capture_complete": True,
                "unreconciled": 0, "source_gone": 0, "baseline_seeded": True,
                "baseline_pending": 0, **self.counts}


def wait_for(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.005)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(rt.stop_all, 2)
        self.tmp = Path(tmp.name)
        self.vault, self.state = self.tmp / "vault", self.tmp / "state"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.state.mkdir()
        self.config = {"vault": {"enabled": True, "path": str(self.vault), "write": False},
                       "second_brain": {"enabled": True}}

    def snapshot(self, root):
        return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))

    def db_files(self):
        for name in rt.DBS:
            (self.state / name).write_bytes(b"")


class SettingsAndGates(Base):
    def test_default_off_and_strict_true(self):
        self.assertFalse(rt.settings({})["enabled"])
        for bad in ("yes", 1, "true", None):
            self.assertFalse(rt.settings({"second_brain": {"enabled": bad}})["enabled"])
        self.assertTrue(rt.settings(self.config)["enabled"])

    def test_numbers_are_clamped_and_junk_ignored(self):
        cfg = rt.settings({"second_brain": {"enabled": True, "interval_s": 0, "batch": 10 ** 9,
                                            "max_backoff_s": "x"}})
        self.assertEqual(cfg["interval_s"], 1.0)
        self.assertEqual(cfg["batch"], 500)
        self.assertEqual(cfg["max_backoff_s"], rt.MAX_BACKOFF_S)

    def test_gate_reasons(self):
        cases = [
            ({}, "disabled"),
            ({"second_brain": {"enabled": True}}, "vault-not-configured"),
            ({**self.config, "vault": {**self.config["vault"], "enabled": False}}, "vault-disabled"),
            ({**self.config, "vault": {"path": str(self.tmp / "missing")}}, "vault-unavailable"),
            ({**self.config, "vault": {"path": {"windows": r"C:\Users\x\Vault"}}}, "vault-not-configured"),
        ]
        for config, reason in cases:
            with self.subTest(reason=reason):
                gate = rt.resolve(config, self.state)
                self.assertFalse(gate["ok"])
                self.assertEqual(gate["reason"], reason)

    def test_empty_mount_point_has_no_sentinel(self):
        empty = self.tmp / "unmounted"
        empty.mkdir()
        gate = rt.resolve({**self.config, "vault": {"path": str(empty)}}, self.state)
        self.assertEqual(gate["reason"], "vault-sentinel-missing")
        self.assertFalse(gate["permanent"])
        self.assertEqual(self.snapshot(empty), [])  # the check created nothing

    def test_archive_index_counts_as_sentinel(self):
        other = self.tmp / "v2"
        area = other / "reference" / "jarvis-records"
        area.mkdir(parents=True)
        (area / "jarvis-records-index.md").write_text("x")
        self.assertTrue(rt.resolve({**self.config, "vault": {"path": str(other)}}, self.state)["ok"])

    def test_per_os_vault_map_uses_existing_resolver(self):
        config = {**self.config, "vault": {"path": {"windows": r"C:\Users\x\Vault",
                                                    "linux": str(self.vault)}}}
        with mock.patch("jarvis.vault.on_windows", return_value=False):
            gate = rt.resolve(config, self.state)
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["root"], str(self.vault.resolve()))

    def test_node_never_drains(self):
        with mock.patch("jarvis.topology.is_node", return_value=True):
            gate = rt.resolve(self.config, self.state)
        self.assertEqual((gate["ok"], gate["reason"], gate["permanent"]), (False, "not-main", True))

    def test_unreadable_role_fails_closed(self):
        with mock.patch("jarvis.topology.is_node", side_effect=KeyError("x")):
            self.assertEqual(rt.resolve(self.config, self.state)["reason"], "role-unknown")


class DrainCycle(Base):
    def drainer(self, queue, **kw):
        kw.setdefault("interval", 10)
        kw.setdefault("max_backoff", 80)
        return rt.Drainer(self.config, self.state, queue=queue, **kw)

    def test_success_configures_once_and_drains_bounded(self):
        q = FakeQueue({"ok": True, "acked": 3, "remaining": 0})
        d = self.drainer(q, batch=7)
        self.assertEqual(d.cycle(), 10)
        self.assertEqual(d.cycle(), 10)
        self.assertEqual(q.configured, [(str(self.state), True)])
        self.assertEqual(q.drains[0], (str(self.state), str(self.vault.resolve()), 7))
        self.assertEqual(d.info["drained_total"], 6)
        self.assertIsNotNone(d.info["last_success"])

    def test_full_batch_with_backlog_drains_again_soon(self):
        d = self.drainer(FakeQueue({"ok": True, "acked": 100, "remaining": 50}))
        self.assertEqual(d.cycle(), rt.CATCHUP_S)

    def test_backoff_doubles_is_capped_and_resets(self):
        q = FakeQueue(error=RuntimeError(SECRET))
        d = self.drainer(q)
        self.assertEqual([d.cycle() for _ in range(5)], [20, 40, 80, 80, 80])
        self.assertEqual(d.info["last_error"], "RuntimeError")
        self.assertNotIn(SECRET, json.dumps(d.info))
        q.error = None
        d.cycle()
        self.assertEqual((d.info["failures"], d.info["last_error"]), (0, None))

    def test_failed_report_is_not_success(self):
        d = self.drainer(FakeQueue({"ok": False, "failed": 2, "acked": 1}))
        self.assertEqual(d.cycle(), 20)
        self.assertIsNone(d.info["last_success"])
        self.assertEqual(d.info["last_error"], "drain-failed")

    def test_garbage_report_is_a_failure(self):
        d = self.drainer(FakeQueue("fine"))
        d.cycle()
        self.assertIsNone(d.info["last_success"])

    def test_busy_lock_is_neither_success_nor_failure(self):
        d = self.drainer(FakeQueue({"busy": True}))
        self.assertEqual(d.cycle(), 10)
        self.assertEqual((d.info["failures"], d.info["last_success"]), (0, None))

    def test_queue_module_missing_backs_off(self):
        d = self.drainer(None)
        with mock.patch.dict(sys.modules, {"jarvis.second_brain_queue": None}):
            self.assertEqual(d.cycle(), 20)
        self.assertEqual(d.info["last_error"], "queue-unavailable")

    def test_configure_failure_retries_and_blocks_drain(self):
        q = FakeQueue()
        q.configure = mock.Mock(side_effect=OSError("locked"))
        d = self.drainer(q)
        d.cycle()
        self.assertEqual(q.drains, [])
        self.assertEqual(d.info["last_error"], "OSError")

    def test_no_write_paths_when_gate_fails(self):
        before = self.snapshot(self.vault)
        for config, node in ((self.config, True),
                             ({"second_brain": {"enabled": False}, "vault": self.config["vault"]}, False)):
            q = FakeQueue()
            d = rt.Drainer(config, self.state, queue=q, interval=10)
            with mock.patch("jarvis.topology.is_node", return_value=node):
                d.cycle()
            self.assertEqual((q.drains, q.configured), ([], []))
        self.assertEqual(self.snapshot(self.vault), before)
        self.assertEqual(self.snapshot(self.state), [])

    def test_offline_vault_keeps_capture_armed_without_draining(self):
        q = FakeQueue()
        config = {**self.config, "vault": {"path": str(self.tmp / "gone")}}
        d = rt.Drainer(config, self.state, queue=q, interval=10)
        d.cycle()
        self.assertEqual(q.drains, [])
        self.assertEqual(q.configured, [(str(self.state), True)])

    def test_missing_drain_success_flag_is_not_success(self):
        d = self.drainer(FakeQueue(report={"acked": 0, "remaining": 0}))
        d.cycle()
        self.assertIsNone(d.info["last_success"])
        self.assertEqual(d.info["last_error"], "drain-failed")

    def test_vault_write_flag_untouched(self):
        d = self.drainer(FakeQueue())
        d.cycle()
        self.assertIs(self.config["vault"]["write"], False)


class Lifecycle(Base):
    def test_concurrent_starts_share_one_armed_worker(self):
        from concurrent.futures import ThreadPoolExecutor
        q = FakeQueue()
        with ThreadPoolExecutor(max_workers=8) as pool:
            workers = list(pool.map(lambda _: rt.start(self.config, self.state, queue=q,
                                                       first_delay=1), range(8)))
        self.assertTrue(all(worker is workers[0] for worker in workers))
        self.assertEqual(len(q.configured), 1)
        self.assertEqual(workers[0].refs, 8)
        for worker in workers:
            rt.stop(worker)
        self.assertFalse(workers[0].alive)

    def test_disabled_and_node_start_nothing(self):
        before = threading.active_count()
        self.assertIsNone(rt.start({"vault": self.config["vault"]}, self.state))
        with mock.patch("jarvis.topology.is_node", return_value=True):
            self.assertIsNone(rt.start(self.config, self.state))
        self.assertEqual(threading.active_count(), before)

    def test_thread_drains_then_stops_without_leak(self):
        q = FakeQueue()
        d = rt.start(self.config, self.state, queue=q, interval=0.01, first_delay=0)
        self.assertTrue(wait_for(lambda: len(q.drains) >= 2))
        self.assertIs(rt.running(self.state), d)
        rt.stop(d)
        self.assertFalse(d.alive)
        self.assertIsNone(rt.running(self.state))
        self.assertFalse([t for t in threading.enumerate() if t.name == "jarvis-second-brain-drain"])
        n = len(q.drains)
        time.sleep(0.05)
        self.assertEqual(len(q.drains), n)

    def test_shared_start_is_reference_counted(self):
        q = FakeQueue()
        a = rt.start(self.config, self.state, queue=q, interval=0.01, first_delay=0)
        b = rt.start(self.config, self.state, queue=q)
        self.assertIs(a, b)
        rt.stop(a)
        self.assertTrue(a.alive)
        rt.stop(b)
        self.assertFalse(a.alive)

    def test_unmounted_vault_waits_then_drains_when_it_appears(self):
        late = self.tmp / "late"
        config = {**self.config, "vault": {"path": str(late)}}
        q = FakeQueue()
        d = rt.start(config, self.state, queue=q, interval=0.01, max_backoff=0.02, first_delay=0)
        self.assertTrue(wait_for(lambda: d.info["last_error"] == "vault-unavailable"))
        self.assertEqual(q.drains, [])
        (late / ".obsidian").mkdir(parents=True)
        self.assertTrue(wait_for(lambda: len(q.drains) >= 1))
        rt.stop(d)

    def test_config_turned_off_ends_the_thread(self):
        q = FakeQueue()
        d = rt.start(self.config, self.state, queue=q, interval=0.01, first_delay=0)
        self.assertTrue(wait_for(lambda: q.drains))
        self.config["second_brain"]["enabled"] = False
        self.assertTrue(wait_for(lambda: not d.alive))
        self.assertIsNone(rt.running(self.state))
        rt.stop(d)

    def test_crashing_cycle_does_not_kill_the_loop(self):
        q = FakeQueue()
        d = rt.Drainer(self.config, self.state, queue=q, interval=0.01, max_backoff=0.02, first_delay=0)
        real, calls = d.cycle, [0]

        def flaky():
            calls[0] += 1
            if calls[0] == 1:
                raise ValueError(SECRET)
            return real()
        d.cycle = flaky
        d.refs = 1
        d.begin()
        self.assertTrue(wait_for(lambda: q.drains))
        self.assertNotIn(SECRET, json.dumps(d.info))
        d.stop(2, force=True)

    def test_stop_all_joins(self):
        d = rt.start(self.config, self.state, queue=FakeQueue(), interval=0.01, first_delay=0)
        rt.stop_all(2)
        self.assertFalse(d.alive)


class Status(Base):
    def test_disabled_never_touches_state_or_queue(self):
        q = FakeQueue(counts={"pending": 4})
        out = rt.status({"vault": self.config["vault"]}, self.state, queue=q)
        self.assertEqual((out["state"], out["enabled"]), ("disabled", False))
        self.assertEqual(self.snapshot(self.state), [])
        self.assertEqual(q.statuses, 0)  # no DB files exist: nothing is opened or created

    def test_no_state_dbs_means_queue_untouched_and_nothing_created(self):
        q = FakeQueue()
        out = rt.status(self.config, self.state, queue=q)
        self.assertEqual((q.statuses, out["queue"]), (0, "no-state"))
        self.assertEqual(self.snapshot(self.state), [])
        self.assertEqual(out["state"], "unverified")

    def test_states_never_overclaim(self):
        self.db_files()
        cases = [
            ({"pending": 0, "held": 0, "errors": 0}, "unverified"),   # nothing proven yet
            ({"pending": 0, "held": 0, "errors": 0, "last_success": 1.0}, "current"),
            ({"pending": 5, "last_success": 1.0}, "pending"),
            ({"pending": 5, "held": 2, "last_success": 1.0}, "held"),
            ({"pending": 0, "held": 0, "errors": 1, "last_success": 1.0}, "error"),
            ({"pending": 0, "last_success": 1.0}, "unverified"),
            ({"pending": 0, "held": float("nan"), "errors": 0, "last_success": 1.0}, "unverified"),
            ({"ok": False, "pending": 0, "held": 0, "errors": 0, "last_success": 1.0}, "unverified"),
            ({"capture_complete": False, "pending": 0, "held": 0, "errors": 0, "last_success": 1.0}, "unverified"),
            ({"unreconciled": 1, "pending": 0, "held": 0, "errors": 0, "last_success": 1.0}, "pending"),
        ]
        for counts, want in cases:
            with self.subTest(want=want):
                out = rt.status(self.config, self.state, queue=FakeQueue(counts=counts))
                self.assertEqual(out["state"], want)
                self.assertIs(out["canonical"], False)
                self.assertIs(out["lease_fenced"], False)
                self.assertEqual(out["stage"], "c2-transitional")

    def test_counts_pass_through_and_text_is_dropped(self):
        self.db_files()
        counts = {"pending": 3, "held": 1, "errors": 0, "oldest_age_s": 12.5, "body": SECRET,
                  "ids": [SECRET], "last_error": SECRET, "pending_by_store": {"memory": SECRET}}
        out = rt.status(self.config, self.state, queue=FakeQueue(counts=counts))
        text = json.dumps(out)
        self.assertNotIn(SECRET, text)
        self.assertEqual((out["archive_pending"], out["archive_held"], out["oldest_pending_age_s"]),
                         (3, 1, 12.5))

    def test_queue_trouble_is_unverified_not_current(self):
        self.db_files()
        with mock.patch.dict(sys.modules, {"jarvis.second_brain_queue": None}):
            out = rt.status(self.config, self.state)
        self.assertEqual((out["queue"], out["state"]), ("unavailable", "unverified"))
        q = FakeQueue()
        q.status = mock.Mock(side_effect=RuntimeError(SECRET))
        out = rt.status(self.config, self.state, queue=q)
        self.assertEqual(out["queue"], "error:RuntimeError")
        self.assertNotIn(SECRET, json.dumps(out))

    def test_blocked_gate(self):
        out = rt.status({**self.config, "vault": {"path": str(self.tmp / "x")}}, self.state)
        self.assertEqual((out["state"], out["gate"]), ("blocked", "vault-unavailable"))

    def test_status_starts_no_thread(self):
        before = threading.active_count()
        rt.status(self.config, self.state)
        self.assertEqual(threading.active_count(), before)

    def test_local_drain_success_feeds_current(self):
        self.db_files()
        q = FakeQueue(counts={"pending": 0, "held": 0, "errors": 0})
        d = rt.start(self.config, self.state, queue=q, interval=0.01, first_delay=0)
        self.assertTrue(wait_for(lambda: d.info["last_success"]))
        out = rt.status(self.config, self.state, queue=q)
        rt.stop(d)
        self.assertEqual((out["state"], out["running_here"]), ("current", True))


class ConsoleStatus(Base):
    def test_archive_starts_even_when_model_warmup_is_disabled(self):
        from jarvis.ui import Console
        stub = SimpleNamespace(config=self.config, state=self.state, bus=None, memory=None,
                               store=None, vault=lambda: None)
        with mock.patch("jarvis.ui.Watcher"), mock.patch.object(rt, "start", return_value=None) as start:
            Console.start_live(stub, warm=False)
        start.assert_called_once_with(self.config, self.state)

    def test_overview_archive_block_is_numeric_only(self):
        from jarvis.ui import Console, _Ttl
        self.db_files()
        q = FakeQueue(counts={"pending": 2, "body": SECRET})
        stub = SimpleNamespace(ttl=_Ttl(), config=self.config, state=self.state)
        with mock.patch.object(rt, "_queue", return_value=q):
            out = Console.archive_status(stub)
        self.assertEqual((out["state"], out["archive_pending"]), ("pending", 2))
        self.assertNotIn(SECRET, json.dumps(out))

    def test_status_failure_never_breaks_overview(self):
        from jarvis.ui import Console, _Ttl
        stub = SimpleNamespace(ttl=_Ttl(), config=self.config, state=self.state)
        with mock.patch.object(rt, "status", side_effect=RuntimeError(SECRET)):
            out = Console.archive_status(stub)
        self.assertEqual(out, {"state": "unverified", "error": "RuntimeError"})


class RealQueueIntegration(Base):
    def test_capture_drain_status_and_restore_with_real_queue(self):
        from jarvis import second_brain_queue as queue
        from jarvis.memory import Memory
        from jarvis.second_brain import SecondBrain
        d = rt.Drainer(self.config, self.state)
        self.assertTrue(d.prime())
        item = Memory(self.state).add("note", "a durable personal memory")
        self.assertEqual(queue.status(self.state)["pending"], 1)
        d.cycle()
        out = rt.status(self.config, self.state)
        self.assertEqual((out["state"], out["archive_pending"], out["capture_complete"]), ("current", 0, True))
        self.assertRegex(out["last_success"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")   # ISO UTC
        self.assertRegex(out["last_drain"], r"^\d{4}-")
        brain = SecondBrain(self.vault)
        self.assertEqual(brain.latest(f"memory/memory-item/{item['id']}")["fields"]["body"], item["body"])
        restored = brain.restore(self.tmp / "rebuilt")
        self.assertTrue(restored["ok"])
        self.assertEqual(Memory(self.tmp / "rebuilt").get(item["id"])["body"], item["body"])

    def test_capture_is_armed_before_offline_vault_returns(self):
        from jarvis import second_brain_queue as queue
        from jarvis.memory import Memory
        late = self.tmp / "late-vault"
        cfg = {**self.config, "vault": {"path": str(late)}}
        d = rt.start(cfg, self.state, first_delay=10)
        self.assertTrue(queue.status(self.state)["capture_complete"])
        memory = Memory(self.state)
        item = memory.add("note", "initial")
        memory.merge_meta(item["id"], {"revision": 2})
        self.assertEqual(queue.status(self.state)["pending"], 2)
        d.cycle()
        self.assertEqual(queue.status(self.state)["pending"], 2)
        (late / ".obsidian").mkdir(parents=True)
        d.cycle()
        self.assertEqual(queue.status(self.state)["pending"], 0)
        rt.stop(d)


if __name__ == "__main__":
    unittest.main()
