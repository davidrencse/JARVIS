"""Automatic gateway deploy: pure decisions plus `step` with fake deploy/notify (no ssh)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import gateway_auto as ga


def hub(tmp, on=True):
    cluster = {"auto_gateway_deploy": True} if on else {}
    return SimpleNamespace(state=tmp, config={"cluster": cluster})


class Pure(unittest.TestCase):
    def test_due(self):
        self.assertEqual(ga.due({}, None, 0), (False, "no release"))
        self.assertEqual(ga.due({"deployed": "a"}, "a", 0), (False, "current"))
        self.assertEqual(ga.due({"deployed": "a", "next_ok": 50}, "b", 10), (False, "retry window"))
        self.assertEqual(ga.due({"deployed": "a", "next_ok": 50}, "b", 50), (True, ""))
        self.assertEqual(ga.due({}, "b", 0), (True, ""))

    def test_gate(self):
        self.assertEqual(ga.gate(True, {}), "busy")
        self.assertEqual(ga.gate(False, {"handoff_live": True}), "handoff live")
        self.assertEqual(ga.gate(False, {"phone_live": True}), "phone session live")
        self.assertIsNone(ga.gate(False, {}))
        self.assertIsNone(ga.gate(False, None))

    def test_record_attempt(self):
        rec = ga.record_attempt({}, "a", None, 100)
        self.assertEqual(rec["next_ok"], 100 + ga.MIN_GAP)
        rec = ga.record_attempt(rec, "a", False, 100, "boom")
        self.assertEqual((rec["fails"], rec["next_ok"]), (1, 100 + 600))
        rec = ga.record_attempt(rec, "a", False, 200, "boom")
        self.assertEqual((rec["fails"], rec["next_ok"]), (2, 200 + 1800))
        for _ in range(10):
            rec = ga.record_attempt(rec, "a", False, 0, "x")
        self.assertEqual(rec["next_ok"], ga.BACKOFF[-1])
        rec = ga.record_attempt(rec, "a", True, 500)
        self.assertEqual((rec["deployed"], rec["fails"], rec["error"]), ("a", 0, None))
        self.assertGreaterEqual(rec["next_ok"] - 500, ga.MIN_GAP)


class Step(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.deploys = []
        self.notes = []
        p = mock.patch("jarvis.release.served_code", return_value="rel1")
        p.start()
        self.addCleanup(p.stop)

    def run_step(self, now, rc=0, busy=False, status=None, on=True):
        def deploy(h, code):
            self.deploys.append(code)
            return rc, "note"
        return ga.step(hub(self.tmp, on), deploy=deploy, notify=self.notes.append, now=now,
                       busy_fn=lambda *a, **k: busy, status_fn=lambda s: status or {})

    def test_off_by_default(self):
        self.assertEqual(self.run_step(0, on=False), "off")
        self.assertEqual(self.deploys, [])

    def test_deploys_once_then_current(self):
        self.assertEqual(self.run_step(1000), "deployed")
        self.assertEqual(self.run_step(5000), "current")
        self.assertEqual(self.deploys, ["rel1"])

    def test_gated_while_busy_or_phone_live_and_nothing_recorded(self):
        self.assertEqual(self.run_step(0, busy=True), "wait: busy")
        self.assertEqual(self.run_step(1, status={"phone_live": True}), "wait: phone session live")
        self.assertEqual(self.run_step(2, status={"handoff_live": True}), "wait: handoff live")
        self.assertEqual(self.deploys, [])
        self.assertFalse((Path(self.tmp) / ga.STATE_FILE).exists())
        self.assertEqual(self.run_step(3), "deployed")  # gate lifted: goes straight away

    def test_failure_backs_off_notifies_once(self):
        self.assertEqual(self.run_step(0, rc=3), "failed")
        self.assertEqual(self.run_step(300, rc=3), "wait: retry window")  # never inside 10 min
        self.assertEqual(self.run_step(601, rc=3), "failed")
        self.assertEqual(self.run_step(601 + 1800 - 1), "wait: retry window")
        self.assertEqual(len(self.deploys), 2)
        self.assertEqual(len(self.notes), 1)  # one notice for this release
        self.assertEqual(self.run_step(601 + 1800), "deployed")

    def test_deploy_exception_counts_as_failure(self):
        def boom(h, code):
            raise OSError("no route")
        out = ga.step(hub(self.tmp), deploy=boom, notify=self.notes.append, now=0,
                      busy_fn=lambda *a, **k: False, status_fn=lambda s: {})
        self.assertEqual(out, "failed")
        self.assertIn("no route", ga._read(self.tmp)["error"])

    def test_new_release_after_success_deploys_again(self):
        self.run_step(0)
        with mock.patch("jarvis.release.served_code", return_value="rel2"):
            self.assertEqual(self.run_step(ga.MIN_GAP - 1), "wait: retry window")
            self.assertEqual(self.run_step(ga.MIN_GAP), "deployed")
        self.assertEqual(self.deploys, ["rel1", "rel2"])


if __name__ == "__main__":
    unittest.main()
