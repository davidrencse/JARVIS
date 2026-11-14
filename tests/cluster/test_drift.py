import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import drift

NEW, OLD = "new", "old"
T = 100000.0


def node(name, code, online=True):
    return {"id": name, "code": code, "online": online}


class DriftTests(unittest.TestCase):
    def test_first_seen_then_alert_after_limit_once(self):
        state, report = drift.drift([node("dell", OLD)], NEW, T, {})
        self.assertEqual(report["behind"], [])
        self.assertEqual(state["since"], {"dell": T})
        state, report = drift.drift([node("dell", OLD)], NEW, T + 31 * 60, state)
        self.assertEqual(report["behind"], [{"id": "dell", "minutes": 31, "alert": True}])
        state, report = drift.drift([node("dell", OLD)], NEW, T + 40 * 60, state)
        self.assertFalse(report["behind"][0]["alert"])  # once per node and release

    def test_clears_when_it_matches_and_can_alert_again(self):
        state, _ = drift.drift([node("dell", OLD)], NEW, T, {})
        state, report = drift.drift([node("dell", NEW)], NEW, T + 60, state)
        self.assertEqual(report["cleared"], ["dell"])
        self.assertEqual(state, {"since": {}, "told": {}})
        state, _ = drift.drift([node("dell", OLD)], NEW, T + 120, state)
        _, report = drift.drift([node("dell", OLD)], NEW, T + 120 + 31 * 60, state)
        self.assertTrue(report["behind"][0]["alert"])

    def test_offline_nodes_do_not_alert_and_restart_their_clock(self):
        state, _ = drift.drift([node("dell", OLD)], NEW, T, {})
        state, report = drift.drift([node("dell", OLD, online=False)], NEW, T + 3600, state)
        self.assertEqual((report["behind"], report["offline"]), ([], ["dell"]))
        self.assertEqual(state["since"], {})
        state, report = drift.drift([node("dell", OLD)], NEW, T + 3700, state)
        self.assertEqual(report["behind"], [])  # clock began at reconnect

    def test_no_release_means_no_drift(self):
        state, report = drift.drift([node("dell", OLD)], None, T, {"since": {"dell": 1}})
        self.assertEqual((state, report["behind"]), ({"since": {}, "told": {}}, []))

    def test_check_persists_state_and_notifies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = SimpleNamespace(state=Path(tmp), broker=SimpleNamespace(nodes=lambda: [node("dell", OLD)]))
            with patch("jarvis.release.current", return_value={"code": NEW}), patch("jarvis.confirm.notify") as notify:
                drift.check(hub, now=T)
                drift.check(hub, now=T + 31 * 60)
                drift.check(hub, now=T + 45 * 60)
            notify.assert_called_once_with("Node dell behind release for 31 min")
            notes = drift.notes(Path(tmp), [node("dell", OLD)], NEW, now=T + 50 * 60)
            self.assertEqual(notes, {"dell": "node dell behind release for 50 min"})
            self.assertEqual(drift.notes(Path(tmp), [node("zen", OLD, online=False)], NEW, now=T),
                             {"zen": "offline, will pull"})
            self.assertEqual(drift.notes(Path(tmp), [node("dell", NEW)], NEW, now=T), {})


if __name__ == "__main__":
    unittest.main()
