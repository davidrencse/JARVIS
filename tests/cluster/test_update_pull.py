"""`jarvis cluster update` for pull nodes (Dell, Oracle): visible progress, bounded waits, and a
node that actually reports the release's code before the update counts as done."""

import argparse
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import cluster_cli, deploy
from tests.cluster.test_cluster import configuration

HOW = "dell has no SSH link: it pulls the main's code itself"


def seen(age=1):
    return {"seen": time.time() - age}


class PullUpdateVerifyTests(unittest.TestCase):
    def pull(self, nodes, cut=None, wait=60, want="new"):
        """Run _pull with `nodes` as the successive (status, code) the broker reports."""
        said = []
        feed = iter(nodes)
        last = []

        def node_code(state, node):
            try:
                last[:] = [next(feed)]
            except StopIteration:
                pass
            return last[0]
        cut = cut or (lambda state, quiet: {"released": True, "code": want})
        with patch("jarvis.deploy._node_code", node_code), patch("jarvis.deploy.time.sleep"), \
                patch("jarvis.release.served_code", return_value=want):
            return deploy._pull("state", "dell", HOW, cut=cut, say=said.append, wait=wait), said

    def test_update_is_done_only_when_the_node_reports_the_release(self):
        report, said = self.pull([(seen(), "old"), (seen(), "old"), (seen(), "old"), (seen(), "new")])
        self.assertTrue(report["updated"])
        self.assertEqual((report["before"], report["after"], report["expected"]), ("old", "new", "new"))
        self.assertTrue(any("waiting for dell" in line for line in said))

    def test_offline_node_is_reported_and_not_waited_for(self):
        sleeps = []
        with patch("jarvis.deploy.time.sleep", sleeps.append):
            report, _ = self.pull([(None, None)] * 4)
        self.assertFalse(report["updated"])
        self.assertIn("offline", report["note"])
        self.assertIn("Not retrying", report["note"])
        self.assertEqual(sleeps, [])

    def test_node_that_stays_on_old_code_fails_with_a_clear_note_instead_of_hanging(self):
        clock = iter(range(0, 10000, 30))
        with patch("jarvis.deploy.time.monotonic", lambda: next(clock)):
            report, _ = self.pull([(seen(), "old")] * 50, wait=90)
        self.assertFalse(report["updated"])
        self.assertIn("still reports old", report["note"])
        self.assertEqual(report["after"], "old")

    def test_failing_release_tests_stop_before_waiting_on_the_node(self):
        failed = {"released": False, "code": "x", "tests": "FAILED (failures=1)"}
        with patch("jarvis.deploy._node_code", return_value=(seen(), "old")) as node:
            with patch("jarvis.deploy.time.sleep") as sleep:
                report = deploy._pull("s", "dell", HOW, cut=lambda s, quiet: failed, say=lambda t: None)
        self.assertFalse(report["updated"])
        self.assertIn("release tests failed", report["note"])
        sleep.assert_not_called()
        self.assertEqual(node.call_count, 1)  # only the "before" read

    def test_cut_prints_what_it_is_doing_and_keeps_printing_while_the_suite_runs(self):
        said = []
        with patch("jarvis.deploy.HEARTBEAT", 0.01), patch("jarvis.deploy._node_code", return_value=(None, None)):
            deploy._pull("s", "dell", HOW, say=said.append, wait=0,
                         cut=lambda s, quiet: (time.sleep(0.1), {"released": False, "note": "Another release is being tested."})[1])
        self.assertIn("full test suite", said[0])
        self.assertTrue(any("still running" in line for line in said))

    def test_heartbeat_stops_when_the_step_ends(self):
        said = []
        with deploy._heartbeat("busy", every=0.01, say=said.append):
            time.sleep(0.06)
        count = len(said)
        time.sleep(0.05)
        self.assertGreaterEqual(count, 1)
        self.assertEqual(len(said), count)


class OneCutPerRunTests(unittest.TestCase):
    def run_update(self, outcomes):
        """`jarvis cluster update` for two pull nodes; every update() calls the shared cut."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = configuration()
            args = argparse.Namespace(action="update", config=str(tmp / "c.json"), state=str(tmp), value=None,
                                      rollback=False)
            cuts = []

            def real_cut(state, quiet=0):
                cuts.append(quiet)
                return outcomes[min(len(cuts), len(outcomes)) - 1]

            def update(config, state, node, rollback=False, cut=None):
                return {"node": node, "release": cut(state, quiet=0)}

            class Nodes:
                def __init__(self, state):
                    pass

                def nodes(self):
                    return [{"id": "dell"}, {"id": "oracle"}]
            with patch("jarvis.cluster_cli.enabled", return_value=True), patch("jarvis.cluster_cli.Broker", Nodes), \
                    patch("jarvis.deploy.update", update), patch("jarvis.release.cut", real_cut):
                reports = cluster_cli.run(args, config, tmp)
        return reports, cuts

    def test_a_failed_release_is_not_retested_for_every_node(self):
        failed = {"released": False, "tests": "FAILED (failures=1)"}
        reports, cuts = self.run_update([failed])
        self.assertEqual(len(cuts), 1)  # the ~9 minute suite ran once, not once per node
        self.assertEqual([r["release"] for r in reports], [failed, failed])

    def test_a_good_release_is_shared_and_later_nodes_see_it_already_cut(self):
        reports, cuts = self.run_update([{"released": True, "code": "x"}, {"released": False, "note": "Nodes already get this code."}])
        self.assertEqual(len(cuts), 2)
        self.assertTrue(reports[0]["release"]["released"])
        self.assertIn("already get", reports[1]["release"]["note"])


class ReleaseStageTests(unittest.TestCase):
    def test_head_bundle_test_skips_in_a_release_stage_without_git(self):
        """release._stage drops .git; the HEAD-bundle upgrade test used to ERROR there and block
        every release (the Dell and Oracle sat on old code, 2026-11-13)."""
        from tests.cluster import test_bundle_upgrade_path as upgrade
        with tempfile.TemporaryDirectory() as bare, patch.object(upgrade, "REPO", Path(bare)):
            with self.assertRaises(unittest.SkipTest):
                upgrade.load_head_deploy(bare)


if __name__ == "__main__":
    unittest.main()
