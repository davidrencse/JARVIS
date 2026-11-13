"""Doctor wording when the operator has stopped the forward tunnel.

Temp state and faked SSH only. No tunnel process, no login unit, no network.
The stop record is the real ``cluster-tunnel.manual-stop`` file. The cooperative
``cluster-tunnel.stop`` marker is not an operator stop.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import cluster_setup, cluster_tunnel
from jarvis.cluster_recovery import SUPERVISOR_WITHIN

STOPPED = "tunnel stopped by the operator; the supervisor will not restart it"
START = "jarvis cluster tunnel start"
WITHIN = f"reachable but tunnel down: the supervisor fixes it within {SUPERVISOR_WITHIN} s"
RESTARTS = "restarts a dead worker within 15 s"

CONFIG = {
    "cluster": {"local_device": "node", "main_device": "main", "tunnel": "forward",
                "listen_port": 7780, "main_url": "http://127.0.0.1:7780"},
    "devices": {"node": {}, "main": {"transport": "ssh", "target": "u@172.1.41.171",
                                     "hostname": "main"}},
}


class DoctorOperatorStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config_path = self.state / "jarvis.config.json"

    def _checks(self, fix=False):
        ssh_ok = SimpleNamespace(returncode=0, stdout="jarvis-node-link", stderr="")
        with patch.object(cluster_setup, "Client"), \
                patch("jarvis.cluster_tunnel.peer", return_value=CONFIG["devices"]["main"]), \
                patch("jarvis.cluster_tunnel.candidates", return_value=["u@172.1.41.171"]), \
                patch("jarvis.cluster_tunnel.reachable", return_value=True), \
                patch("jarvis.cluster_tunnel.pinned_host", return_value=[]), \
                patch("jarvis.cluster_tunnel.status", return_value={
                    "hub_healthy": False, "ssh_alive": False, "running": False}), \
                patch("jarvis.cluster_recovery.tailscale_status", return_value=None), \
                patch("jarvis.cluster_recovery.ensure_dependents") as dependents, \
                patch.object(cluster_tunnel, "start", side_effect=AssertionError("start")), \
                patch.object(cluster_tunnel, "autostart", side_effect=AssertionError("autostart")), \
                patch.object(cluster_setup.shutil, "which", return_value="/usr/bin/ssh"), \
                patch.object(cluster_setup.subprocess, "run", return_value=ssh_ok):
            checks = []
            cluster_setup._doctor_node(
                CONFIG, self.state, "node", "main", True, checks, "code",
                fix=fix, config_path=self.config_path)
        return {item["check"]: item for item in checks}, dependents

    def _record_stop(self):
        cluster_tunnel.record_manual_stop(CONFIG, self.state)
        self.assertTrue(cluster_tunnel.manual_stop_path(self.state).is_file())
        self.assertEqual(cluster_tunnel.manual_stop_path(self.state).name,
                         "cluster-tunnel.manual-stop")
        self.assertTrue(cluster_tunnel.manual_stop_holds(CONFIG, self.state))

    def test_no_stop_keeps_the_supervisor_promise(self):
        self.assertFalse(cluster_tunnel.manual_stop_path(self.state).exists())
        checks, dependents = self._checks(fix=False)
        supervisor = checks["tunnel supervisor"]
        running = checks["tunnel running"]
        self.assertFalse(supervisor["ok"])
        self.assertEqual(supervisor["detail"], WITHIN)
        self.assertIn("jarvis cluster doctor --fix", supervisor["fix"])
        self.assertNotIn(STOPPED, supervisor["detail"])
        self.assertFalse(running["ok"])
        self.assertIn(RESTARTS, running["fix"])
        self.assertIn("jarvis cluster run", running["fix"])
        self.assertNotIn(STOPPED, running["detail"])
        self.assertNotIn(STOPPED, running["fix"])
        dependents.assert_not_called()

    def test_operator_stop_does_not_promise_a_restart(self):
        self._record_stop()
        checks, dependents = self._checks(fix=False)
        supervisor = checks["tunnel supervisor"]
        running = checks["tunnel running"]
        self.assertFalse(supervisor["ok"])
        self.assertEqual(supervisor["detail"], STOPPED)
        self.assertEqual(supervisor["fix"], START)
        self.assertNotIn("within", supervisor["detail"])
        self.assertNotIn(str(SUPERVISOR_WITHIN), supervisor["detail"])
        self.assertNotIn("--fix", supervisor["fix"])
        self.assertFalse(running["ok"])
        self.assertEqual(running["detail"], STOPPED)
        self.assertEqual(running["fix"], START)
        self.assertNotIn(RESTARTS, running["fix"])
        self.assertNotIn("within 15", running["detail"] + running["fix"])
        self.assertTrue(cluster_tunnel.manual_stop_holds(CONFIG, self.state))
        dependents.assert_not_called()

    def test_fix_leaves_an_operator_stop_and_names_tunnel_start(self):
        self._record_stop()
        checks, dependents = self._checks(fix=True)
        supervisor = checks["tunnel supervisor"]
        self.assertEqual(supervisor["detail"], STOPPED)
        self.assertIn(START, supervisor["fix"])
        self.assertIn("left the operator stop", supervisor["fix"])
        self.assertNotIn("already supervised", supervisor["fix"])
        self.assertNotIn("started the tunnel", supervisor["fix"])
        self.assertNotIn(str(SUPERVISOR_WITHIN), supervisor["detail"])
        self.assertEqual(checks["tunnel running"]["detail"], STOPPED)
        self.assertEqual(checks["tunnel running"]["fix"], START)
        dependents.assert_called_once()
        self.assertTrue(cluster_tunnel.manual_stop_path(self.state).is_file())
        self.assertTrue(cluster_tunnel.manual_stop_holds(CONFIG, self.state))

    def test_cooperative_stop_marker_is_not_an_operator_stop(self):
        (self.state / "cluster-tunnel.stop").write_text("marker\n", encoding="utf-8")
        self.assertFalse(cluster_tunnel.manual_stop_path(self.state).exists())
        checks, _dependents = self._checks(fix=False)
        self.assertEqual(checks["tunnel supervisor"]["detail"], WITHIN)
        self.assertIn(RESTARTS, checks["tunnel running"]["fix"])
        self.assertTrue((self.state / "cluster-tunnel.stop").is_file())
        self.assertFalse(cluster_tunnel.manual_stop_holds(CONFIG, self.state))


if __name__ == "__main__":
    unittest.main()
