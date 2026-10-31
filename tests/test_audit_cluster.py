"""Audit fixes in the cluster: wedged outbox, clock drift, job grace, pull-style code update."""

import base64
import json
import copy
import io
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis import deploy
from jarvis.cluster import Hub, HubRefused, NodeWorker, save_json
from jarvis.cluster_store import GRACE, Broker
from tests.test_cluster import configuration


def job(**extra):
    return {"id": uuid.uuid4().hex, "kind": "desktop", "payload": {"action": "volume", "arg": "5"},
            "deadline": time.time() + 30, **extra}


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "node"
        save_json(self.state / "cluster-node.json", {"node_id": "node", "main_device": "main", "token": "t" * 24})
        self.executor = Mock(return_value={"ok": True})
        self.worker = NodeWorker(configuration("node"), self.state, executor=self.executor)

    def saved(self, message=None):
        save_json(self.worker.outbox, {"main_device": "main", "message": message or
                                       {"id": uuid.uuid4().hex, "result": {}, "failed": False}})


class OutboxTests(WorkerCase):
    def test_permanent_rejection_drops_outbox_and_polling_continues(self):
        for status in (400, 404):
            self.saved()
            self.worker.client.request = Mock(side_effect=[HubRefused("Job is no longer running", status),
                                                           {"job": None}])
            self.worker.once()
            self.assertFalse(self.worker.outbox.exists())
            self.assertEqual(self.worker.client.request.call_args_list[-1][0][0], "/v1/poll")

    def test_transient_errors_keep_outbox(self):
        for error in (ConnectionError("refused"), TimeoutError("slow"), HubRefused("boom", 503)):
            self.saved()
            self.worker.client.request = Mock(side_effect=error)
            with self.assertRaises(type(error)):
                self.worker.once()
            self.assertTrue(self.worker.outbox.exists())

    def test_rejection_of_fresh_result_also_drops_it(self):
        self.worker.client.request = Mock(side_effect=[{"job": job(remaining=20)}, HubRefused("gone", 400)])
        self.assertTrue(self.worker.once())
        self.assertFalse(self.worker.outbox.exists())


class GraceTests(unittest.TestCase):
    def test_running_job_survives_deadline_by_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(tmp)
            late, gone = (broker.enqueue("node", "main", "inventory", {}) for _ in range(2))
            broker.claim("node", {"inventory"})
            broker.claim("node", {"inventory"})
            with broker.db() as db:
                db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 3, late))
                db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - GRACE - 3, gone))
            self.assertEqual(broker.get(late)["state"], "running")
            broker.finish(late, "node", {"ok": True})
            self.assertEqual(broker.get(late)["state"], "done")
            self.assertEqual(broker.get(gone)["state"], "uncertain")

    def test_claim_reports_remaining_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(tmp)
            broker.enqueue("node", "main", "inventory", {}, timeout=40)
            self.assertAlmostEqual(broker.claim("node", {"inventory"})["remaining"], 40, delta=2)


class RemainingTests(WorkerCase):
    def test_node_uses_remaining_not_main_wall_clock(self):
        # Main's clock is an hour behind: the old comparison would call this expired.
        self.worker.client.request = Mock(side_effect=[{"job": job(deadline=time.time() - 3600, remaining=20)},
                                                       {"accepted": True}])
        self.worker.once()
        self.executor.assert_called_once()

    def test_expired_by_remaining_even_if_wall_clock_looks_fine(self):
        self.worker.client.request = Mock(side_effect=[{"job": job(remaining=-1)}, {"accepted": True}])
        self.worker.once()
        self.executor.assert_not_called()
        self.assertIn("expired", self.worker.client.request.call_args[0][1]["result"]["error"])

    def test_without_remaining_falls_back_to_deadline(self):
        self.worker.client.request = Mock(side_effect=[{"job": job(deadline=time.time() - 1)},
                                                       {"accepted": True}])
        self.worker.once()
        self.executor.assert_not_called()


def make_tree(root, files):
    for name, data in files.items():
        path = Path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def bundle_of(entries, code, signer=None):
    """A /v1/code bundle; `signer` is a state folder whose code-signing key signs it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    raw = buffer.getvalue()
    bundle = {"code": code, "zip": base64.b64encode(raw).decode()}
    if signer:
        bundle["signature"] = deploy.sign(raw, signer)
    return bundle


class PullUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        make_tree(self.repo, {"jarvis/a.py": b"old\n", "jarvis/stale.py": b"x\n", "pyproject.toml": b"old\n"})
        self.new = {"jarvis/a.py": b"new\n", "jarvis/sub/b.py": b"b\n", "pyproject.toml": b"new\n"}
        stage = Path(self.tmp.name) / "stage"
        make_tree(stage, self.new)
        self.want = deploy.code_fingerprint(stage / "jarvis")
        self.signer = Path(self.tmp.name) / "main-state"
        self.key = deploy.code_key(self.signer)

    def test_installs_verified_files_and_drops_stale_ones(self):
        deploy.apply_bundle(bundle_of(self.new, self.want, self.signer), self.key, self.repo)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"new\n")
        self.assertEqual((self.repo / "jarvis/sub/b.py").read_bytes(), b"b\n")
        self.assertEqual((self.repo / "pyproject.toml").read_bytes(), b"new\n")
        self.assertFalse((self.repo / "jarvis/stale.py").exists())
        self.assertEqual(deploy.code_fingerprint(self.repo / "jarvis"), self.want)

    def test_rejects_path_traversal_and_absolute_paths(self):
        outside = Path(self.tmp.name) / "evil.py"
        for name in ("jarvis/../../evil.py", "../evil.py", str(outside), "jarvis\\..\\evil.py",
                     "C:/evil.py", "other/x.py"):
            with self.assertRaises(ValueError, msg=name):
                deploy.apply_bundle(bundle_of({name: b"x"}, "0" * 16, self.signer), self.key, self.repo)
        self.assertFalse(outside.exists())
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")

    def test_code_that_cannot_start_installs_nothing(self):
        # 2026-10-30: a bundle taken mid-edit raised NameError at import and killed the node.
        broken = {"jarvis/__init__.py": b"", "jarvis/deploy.py": b"FLAGS = NO_WINDOW\n",
                  "jarvis/cluster.py": b"from . import deploy\n", "pyproject.toml": b"new\n"}
        stage = Path(self.tmp.name) / "broken-stage"
        make_tree(stage, broken)
        want = deploy.code_fingerprint(stage / "jarvis")
        with self.assertRaisesRegex(ValueError, "doesn't start.*NO_WINDOW"):
            deploy.apply_bundle(bundle_of(broken, want, self.signer), self.key, self.repo)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.repo / "jarvis/deploy.py").exists())

    def test_update_keeps_the_previous_code(self):
        deploy.apply_bundle(bundle_of(self.new, self.want, self.signer), self.key, self.repo)
        self.assertEqual((self.repo / "jarvis.prev/a.py").read_bytes(), b"old\n")

    def test_wrong_fingerprint_installs_nothing(self):
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            deploy.apply_bundle(bundle_of(self.new, "f" * 16, self.signer), self.key, self.repo)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertTrue((self.repo / "jarvis/stale.py").exists())

    def test_unsigned_or_foreign_or_tampered_bundles_install_nothing(self):
        """Whatever answers the node's loopback port can't hand it code: only bundles
        signed by the pinned main key install."""
        other = Path(self.tmp.name) / "squatter"
        good = bundle_of(self.new, self.want, self.signer)
        tampered = {**bundle_of({**self.new, "jarvis/a.py": b"evil\n"}, self.want),
                    "signature": good["signature"]}
        cases = {"unsigned": (bundle_of(self.new, self.want), self.key),
                 "other key": (bundle_of(self.new, self.want, other), self.key),
                 "tampered zip": (tampered, self.key),
                 "nothing pinned": (good, None)}
        for name, (bundle, key) in cases.items():
            with self.assertRaises(ValueError, msg=name):
                deploy.apply_bundle(bundle, key, self.repo)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertTrue((self.repo / "jarvis/stale.py").exists())

    def test_node_pulls_once_then_restarts_instead_of_looping(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            save_json(state / "cluster-node.json", {"node_id": "node", "main_device": "main",
                                                    "token": "t" * 24, "code_key": "ssh-ed25519 AAAA"})
            worker = NodeWorker(configuration("node"), state, executor=Mock())
            bundle = {"code": "abc", "zip": ""}
            worker.client.request = Mock(side_effect=[{"job": None, "code": "abc"}, bundle,
                                                      {"job": None, "code": "abc"}])
            with patch("jarvis.deploy.apply_bundle", return_value=3) as apply:
                self.assertFalse(worker.once())
                self.assertTrue(worker.restarting)
                apply.assert_called_once_with(bundle, "ssh-ed25519 AAAA")
                worker.restarting = False
                worker.once()  # same target again within 10 minutes: no second download
                apply.assert_called_once()

    def test_node_backs_up_confirms_and_never_repulls_rolled_back_code(self):
        from jarvis import release
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            save_json(state / "cluster-node.json", {"node_id": "node", "main_device": "main",
                                                    "token": "t" * 24, "code_key": "ssh-ed25519 AAAA"})
            worker = NodeWorker(configuration("node"), state, executor=Mock())
            worker.client.request = Mock(side_effect=[{"job": None, "code": "abc"},
                                                      {"code": "abc", "zip": ""}])
            with patch("jarvis.deploy.apply_bundle", return_value=3), \
                    patch("jarvis.release.backup") as backup:
                worker.once()
            backup.assert_called_once_with(state)  # before the new files land
            self.assertEqual(worker.installed, "abc")  # what the watchdog waits for
            ok = json.loads((state / release.CONFIRMED).read_text())
            self.assertEqual(ok["code"], worker.code)  # this process reached the main
            # The watchdog rolled "abc" back: the node stays on its code, without re-pulling.
            save_json(state / release.BAD, {"code": "abc"})
            (state / "cluster-update.json").unlink()
            worker.restarting = False
            worker.client.request = Mock(return_value={"job": None, "code": "abc"})
            with patch("jarvis.deploy.apply_bundle") as apply:
                worker.once()
            apply.assert_not_called()
            self.assertFalse(worker.restarting)

    def test_a_main_restart_counts_as_one_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            save_json(state / "cluster-node.json", {"node_id": "node", "main_device": "main",
                                                    "token": "t" * 24, "code_key": "ssh-ed25519 AAAA"})
            worker = NodeWorker(configuration("node"), state, executor=Mock())
            down, up = ConnectionError("main restarting"), {"job": None, "code": worker.code}
            worker.client.request = Mock(side_effect=[down, down, down, up, up, down, up])
            for _ in range(7):
                try:
                    worker.once()
                except OSError:
                    pass
            stats = json.loads((state / "cluster-link-stats.json").read_text())
            self.assertEqual((stats["polls_failed"], stats["outages"]), (4, 2))

    def test_hub_serves_bundle_and_skips_non_ssh_nodes(self):
        bundle = deploy.code_bundle(self.signer)
        self.assertEqual(bundle["code"], deploy.code_fingerprint())
        self.assertTrue(deploy.verify(base64.b64decode(bundle["zip"]), bundle["signature"], self.key))
        config = configuration()
        config["devices"]["dell"] = {"transport": "cluster"}
        self.assertFalse(deploy.is_ssh_node(config, "dell"))
        self.assertTrue(deploy.is_ssh_node(config, "node"))
        report = deploy.update(config, self.tmp.name, "dell")
        self.assertTrue(report["pull"])

    def test_auto_update_skips_non_ssh_node_quietly(self):
        config = configuration()
        config["devices"]["dell"] = {"transport": "cluster"}
        with tempfile.TemporaryDirectory() as tmp:
            hub = Hub(config, tmp, tmp)
            hub.broker.enroll("dell")
            hub.broker.heartbeat("dell", {"code": "old"})
            with patch("jarvis.deploy.update") as update:
                hub._auto_update(settle=0)
            update.assert_not_called()


class SshUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = configuration()
        self.config["devices"]["node"]["target"] = "jdoe@10.0.0.2"

    def test_non_posix_node_pulls_instead_of_running_a_shell_script(self):
        remote = Mock(return_value=(1, "", "'uname' is not recognized"))
        with patch("jarvis.deploy._remote", remote):
            report = deploy.update(self.config, self.tmp.name, "node")
            self.assertTrue(report["pull"])
            self.assertEqual(remote.call_count, 1)  # only the probe ran
            with self.assertRaises(ValueError):
                deploy.update(self.config, self.tmp.name, "node", rollback=True)

    def test_update_runs_under_bash_and_survives_a_failed_restart(self):
        calls = []

        def remote(config, state, node, command, stdin=b"", timeout=120):
            calls.append(command)
            return (0, "Darwin\n", "") if command == "uname -s" else (0, "ok\n", "")

        checked_in = ({"seen": time.time()}, deploy.code_fingerprint())
        with patch("jarvis.deploy._remote", remote), \
                patch("jarvis.deploy._node_code", return_value=checked_in):
            report = deploy.update(self.config, self.tmp.name, "node")
        self.assertTrue(report["updated"])
        command = calls[1]
        self.assertTrue(command.startswith("bash -c 'eval"))
        script = base64.b64decode(command.split("echo ")[1].split(" |")[0]).decode()
        self.assertIn("launchctl kickstart", script)
        self.assertNotIn("systemctl", script)
        self.assertIn("|| echo 'agent restart failed", script)


    def test_node_that_never_checks_in_is_rolled_back(self):
        calls = []

        def remote(config, state, node, command, stdin=b"", timeout=120):
            calls.append(command + "\n" + stdin.decode(errors="replace") if command == "bash -s" else command)
            if command == "uname -s":
                return 0, "Linux\n", ""
            return (0, "prev123\n", "") if command == "bash -s" else (0, "ok\n", "")

        with patch("jarvis.deploy._remote", remote), patch("jarvis.deploy.WAIT", 0):
            report = deploy.update(self.config, self.tmp.name, "node")
        self.assertFalse(report["updated"])
        self.assertTrue(report["rolled_back"])
        self.assertEqual(report["after"], "prev123")
        self.assertTrue(any("mv jarvis.prev jarvis" in c for c in calls))  # the rollback script ran


class FingerprintTests(unittest.TestCase):
    def test_crlf_and_lf_checkouts_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            make_tree(a, {"x.py": b"one\ntwo\n", "d/y.py": b"1\n"})
            make_tree(b, {"x.py": b"one\r\ntwo\r\n", "d/y.py": b"1\r\n"})
            self.assertEqual(deploy.code_fingerprint(a), deploy.code_fingerprint(b))
            make_tree(b, {"x.py": b"one\r\nTWO\r\n"})
            self.assertNotEqual(deploy.code_fingerprint(a), deploy.code_fingerprint(b))


if __name__ == "__main__":
    unittest.main()
