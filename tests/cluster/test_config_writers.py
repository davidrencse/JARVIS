"""The managed config lock (jarvis/config_lock.py) and the writers that use it: the console
invite, `cluster_setup.invite`, `jarvis cluster configure|main|enroll|join`, init and leave.

Temp folders only. Key, sshd and host-key side effects are patched; nothing writes a real
authorized_keys file, starts a service or calls a model."""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import cluster_cli, cluster_setup, config_lock, fleet_management as fm
from jarvis.cluster_store import Broker
from jarvis.config_lock import ConfigBusy, ConfigDrift, config_mutation, merge_changes, patch_config

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R"
PUB2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIWritersSecondKeyOnly00000"
PRIOR = "ssh-ed25519 AAAAexisting someone@else"
MAIN = {"cluster": {"enabled": True, "main_device": "zenbook", "local_device": "auto",
                    "tunnel": "forward", "listen_port": 7780, "main_url": "http://127.0.0.1:7780"},
        "devices": {"zenbook": {"hostname": "arch", "label": "Zenbook"},
                    "dell": {"hostname": "WinLaptop", "label": "Dell", "transport": "cluster"}},
        "keep": {"me": [1, 2, 3]}}
JOB = "a" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        for item in (mock.patch.object(cluster_setup, "_hostname", return_value="arch"),
                     mock.patch("jarvis.topology.socket.gethostname", return_value="arch")):
            item.start()
            self.addCleanup(item.stop)
        self.state = self.tmp / ".jarvis"
        self.path = self.tmp / "jarvis.config.json"
        self.path.write_text(json.dumps(MAIN, indent=2), encoding="utf-8")
        self.keys = self.tmp / "authorized_keys"
        self.keys.write_text(PRIOR + "\n", encoding="utf-8")

    def disk(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    @contextlib.contextmanager
    def bound_main(self):
        """A contract-compliant handoff for set_main. The patch replaces only the binding
        lookup: set_main still takes the managed lock, reads the file, and patches the role
        itself after the authenticated status shows the requested main. The handoff writes nothing."""
        seen = []
        state = self.state

        class _Handoff:
            def __init__(self):
                self.phase = "before"
                self.target = "dell"
                self.state = state

            def candidate_readiness(self, device):
                return {"control_ready": True, "ai_ready": False}

            def authority_status(self):
                main = "zenbook" if self.phase == "before" else self.target
                return {"mode": "V2_ACTIVE", "lease_valid": True, "main_ready": True, "main": main,
                        "epoch": 1, "incarnation": 1, "grant_barrier": 0,
                        "transition": None if self.phase == "before" else {
                            "state": "SUCCEEDED", "candidate": self.target, "transition_id": "t-writers",
                            "action": "set_preferred_main"}}

            def __call__(self, old, new, config, base):
                seen.append({"old": old, "new": new, "config": config, "base": base,
                             "held": config_lock.held(self.state)})
                self.target = new
                self.phase = "after"
                return {"ok": True, "status": "ready", "main": new,
                        "transition_id": "t-writers", "epoch": 1, "action": "set_preferred_main"}

        with mock.patch.object(cluster_cli, "_bound_handoff", return_value=_Handoff()):
            yield seen

    def edit_disk(self, fn):
        doc = self.disk()
        fn(doc)
        self.path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def backups(self):
        return sorted(p.name for p in self.tmp.iterdir() if ".pre-" in p.name)

    def common(self, code_key=None, encode=None, keypair=None):
        patches = [
            mock.patch.object(cluster_setup, "_hostname", return_value="arch"),
            mock.patch("jarvis.topology.socket.gethostname", return_value="arch"),
            mock.patch.object(cluster_setup, "_sshd_up", return_value=True),
            mock.patch.object(cluster_setup, "addresses", return_value=["203.0.113.10"]),
            mock.patch.object(cluster_setup, "host_key", return_value=HOST_KEY),
            mock.patch.object(cluster_setup, "_authorized_keys", return_value=self.keys),
            mock.patch.object(cluster_setup.getpass, "getuser", return_value="jdoe"),
            mock.patch.object(cluster_setup, "keypair", side_effect=keypair or (
                lambda node: ("-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n", PUB2))),
        ]
        patches.append(mock.patch.object(cluster_setup, "code_key", side_effect=code_key)
                       if code_key else mock.patch.object(cluster_setup, "code_key", return_value=HOST_KEY))
        if encode:
            patches.append(mock.patch.object(cluster_setup, "encode", side_effect=encode))
        stack = contextlib.ExitStack()
        for item in patches:
            stack.enter_context(item)
        return stack

    def running_job(self, node="pixel"):
        broker = Broker(self.state)
        broker.enqueue(node, "owner", "status", {}, job_id=JOB)
        broker.claim(node, ["status"])
        return broker

    def job_state(self):
        with Broker(self.state).db() as db:
            return db.execute("SELECT state FROM jobs WHERE id=?", (JOB,)).fetchone()["state"]


class PatchConfigTests(Base):
    def test_return_uses_authenticated_local_main_when_raw_role_names_target(self):
        doc = self.disk()
        doc["cluster"]["local_device"] = "dell"
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        state = self.state
        calls = []

        class Handoff:
            phase = "before"
            def candidate_readiness(self, device):
                return {"control_ready": True, "ai_ready": False}
            def authority_status(self):
                main = "dell" if self.phase == "before" else "zenbook"
                return {"mode": "V2_ACTIVE", "lease_valid": True, "main_ready": True,
                        "main": main, "epoch": 2, "incarnation": 1,
                        "transition": None if self.phase == "before" else {
                            "state": "SUCCEEDED", "candidate": "zenbook",
                            "transition_id": "t-return-role", "action": "set_preferred_main"}}
            def __call__(self, old, new, config, base):
                calls.append((old, new))
                self.phase = "after"
                return {"ok": True, "status": "ready", "main": new,
                        "transition_id": "t-return-role", "epoch": 2,
                        "action": "set_preferred_main"}

        with mock.patch.object(cluster_cli, "_bound_handoff", return_value=Handoff()):
            result = cluster_cli.set_main(doc, self.path, "zenbook", state=state)
        self.assertEqual(calls, [("dell", "zenbook")])
        self.assertTrue(result["ok"])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_only_the_change_is_written_and_unrelated_edits_survive(self):
        stale = json.loads(json.dumps(MAIN))
        self.edit_disk(lambda d: d.setdefault("rules", {}).update(confirm="never"))
        merged, changed = config_lock.save_delta(
            self.state, self.path, stale, {**stale, "cluster": {**stale["cluster"], "listen_port": 7781}},
            backup="t")
        self.assertTrue(changed)
        self.assertEqual(self.disk()["rules"], {"confirm": "never"})  # not clobbered by the stale copy
        self.assertEqual(self.disk()["cluster"]["listen_port"], 7781)
        self.assertEqual(merged, self.disk())

    def test_conflicting_change_to_the_same_key_is_refused(self):
        stale = json.loads(json.dumps(MAIN))
        self.edit_disk(lambda d: d["cluster"].update(main_device="dell"))
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(ConfigDrift):
            config_lock.save_delta(self.state, self.path, stale,
                                   {**stale, "cluster": {**stale["cluster"], "main_device": "pixel"}})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_merge_changes_removes_and_recurses(self):
        doc = {"a": {"x": 1, "y": 2}, "b": 1, "z": 9}
        merge_changes(doc, {"a": {"x": 1, "y": 2}, "b": 1}, {"a": {"x": 5, "y": 2}})
        self.assertEqual(doc, {"a": {"x": 5, "y": 2}, "z": 9})

    def test_an_editor_write_between_read_and_replace_is_not_overwritten(self):
        def change(doc):  # an unmanaged writer lands while this change is being made
            self.edit_disk(lambda d: d.update(editor=True))
            doc["mine"] = 1
        with self.assertRaises(ConfigDrift):
            patch_config(self.state, self.path, change, backup="t")
        self.assertTrue(self.disk()["editor"])
        self.assertNotIn("mine", self.disk())

    def test_backups_never_collide_and_keep_the_previous_bytes(self):
        originals = []
        for n in range(5):
            originals.append(self.path.read_bytes())
            patch_config(self.state, self.path, lambda d, n=n: d.update(n=n), backup="t")
        names = self.backups()
        self.assertEqual(len(names), 5)
        self.assertEqual(sorted((self.tmp / n).read_bytes() for n in names), sorted(originals))

    def test_no_change_writes_nothing(self):
        mtime = self.path.stat().st_mtime_ns
        _, changed = patch_config(self.state, self.path, lambda d: None, backup="t")
        self.assertFalse(changed)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_reentrant_in_one_thread_and_exclusive_across_threads(self):
        order = []
        with config_mutation(self.state):
            with config_mutation(self.state):  # would self-deadlock on a plain flock
                order.append("inner")
            other = threading.Thread(target=lambda: self._hold(order))
            other.start()
            time.sleep(0.2)
            order.append("main done")
        other.join(5)
        self.assertEqual(order, ["inner", "main done", "other"])

    def _hold(self, order):
        with config_mutation(self.state):
            order.append("other")

    def test_another_process_holding_the_lock_is_busy(self):
        self.state.mkdir(parents=True, exist_ok=True)
        script = ("import sys,time; sys.path.insert(0, sys.argv[1]);"
                  "from jarvis.config_lock import config_mutation;"
                  "cm = config_mutation(sys.argv[2]); cm.__enter__(); print('held', flush=True);"
                  "sys.stdin.readline()")
        proc = subprocess.Popen([sys.executable, "-c", script, str(Path(__file__).resolve().parents[2]),
                                 str(self.state)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                env={**os.environ, "JARVIS_NO_MODELS": "1"})
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            with self.assertRaises(ConfigBusy):
                with config_mutation(self.state, timeout=0.2):
                    pass
            with self.assertRaises(ConfigBusy):
                cluster_cli.set_main(MAIN, self.path, "dell", state=self.state)
            self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        finally:
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.wait(10)
            proc.stdin.close()
            proc.stdout.close()
        # The other process has released the lock. Unbound set_main still refuses, so the
        # write that proves this process can take the lock is a bound handoff.
        with self.bound_main():
            moved = cluster_cli.set_main(MAIN, self.path, "dell", state=self.state)
        self.assertEqual(moved["main_device"], "dell")
        self.assertTrue(moved["ok"] and moved["mutated"])
        self.assertEqual(self.disk()["cluster"]["main_device"], "dell")


class CliWriterTests(Base):
    def args(self, action, **kw):
        base = dict(action=action, config=str(self.path), state=str(self.state), value=None,
                    main_device=None, local_device=None, url=None, hostname=None, capability=None,
                    output=None, device_id=None, address=[], no_autostart=True, no_start=True,
                    port=7777, rollback=False, autostart=False)
        return argparse.Namespace(**{**base, **kw})

    def test_configure_waits_for_a_held_lock_then_keeps_the_holders_edit(self):
        stale = json.loads(json.dumps(MAIN))
        started, result = threading.Event(), {}

        def configure():
            started.set()
            result["out"] = cluster_cli.run(self.args("configure", main_device="zenbook",
                                                      local_device="zenbook"), stale, self.tmp)
        with config_mutation(self.state):
            worker = threading.Thread(target=configure)
            worker.start()
            started.wait(5)
            time.sleep(0.3)
            self.assertNotIn("out", result)  # still waiting on the lock
            # The holder (an invite, say) adds a device while configure waits.
            self.edit_disk(lambda d: d["devices"].update(oracle1={"label": "oracle1"}))
        worker.join(10)
        self.assertEqual(result["out"]["role"], "main")
        disk = self.disk()
        self.assertIn("oracle1", disk["devices"])
        self.assertEqual(disk["cluster"]["local_device"], "zenbook")
        self.assertEqual(disk["keep"], {"me": [1, 2, 3]})

    def test_set_main_keeps_a_device_written_after_the_caller_loaded_its_copy(self):
        stale = json.loads(json.dumps(MAIN))
        self.edit_disk(lambda d: d["devices"].update(oracle1={"label": "oracle1", "profile": "cloud"}))
        with self.bound_main() as seen:
            cluster_cli.set_main(stale, self.path, "dell", state=self.state)
        disk = self.disk()
        self.assertEqual(disk["devices"]["oracle1"]["profile"], "cloud")
        self.assertEqual(disk["cluster"]["main_device"], "dell")
        self.assertEqual(disk["keep"], {"me": [1, 2, 3]})
        self.assertEqual(len(self.backups()), 1)
        saved = json.loads((self.tmp / self.backups()[0]).read_text(encoding="utf-8"))
        self.assertEqual(saved["cluster"]["main_device"], "zenbook")
        self.assertEqual(saved["devices"]["oracle1"]["profile"], "cloud")
        self.assertNotIn("oracle1", stale["devices"])
        self.assertEqual(seen[0]["old"], "zenbook")
        self.assertEqual(seen[0]["new"], "dell")
        self.assertEqual(seen[0]["config"]["devices"]["oracle1"]["profile"], "cloud")
        self.assertTrue(seen[0]["held"])
        self.assertEqual(seen[0]["base"], self.tmp)

    def test_unbound_set_main_refuses_without_changing_the_role(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            cluster_cli.set_main(MAIN, self.path, "dell", state=self.state)
        self.assertIn("Moving MAIN is unavailable", str(caught.exception))
        self.assertIn("safe handoff", str(caught.exception))
        self.assertIn("not changed", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_set_main_checks_the_device_on_disk(self):
        self.edit_disk(lambda d: d["devices"].pop("dell"))
        before = self.path.read_bytes()
        with self.bound_main() as seen:
            with self.assertRaises(ValueError) as caught:
                cluster_cli.set_main(MAIN, self.path, "dell", state=self.state)
        self.assertIn("No device 'dell'", str(caught.exception))
        self.assertEqual(seen, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")

    def test_leave_only_turns_the_cluster_off(self):
        self.edit_disk(lambda d: d["cluster"].update(local_device="dell"))
        self.edit_disk(lambda d: d.update(newer=True))
        node = self.disk()
        with mock.patch("jarvis.cluster_tunnel.status", return_value={"running": False}), \
                mock.patch("jarvis.cluster_tunnel.autostart", return_value={}), \
                mock.patch.object(cluster_cli, "autostart", return_value={}):
            cluster_setup.leave(node, self.path, self.state)
        disk = self.disk()
        self.assertFalse(disk["cluster"]["enabled"])
        self.assertTrue(disk["newer"])
        self.assertEqual(disk["cluster"]["local_device"], "dell")
        self.assertEqual(disk["cluster"]["main_device"], "zenbook")
        self.assertEqual(disk["devices"], node["devices"])
        self.assertEqual(disk["keep"], node["keep"])


class InviteUnderLockTests(Base):
    def invite(self, config=None, node="pixel", **kw):
        with self.common(**kw):
            return cluster_setup.invite(config or MAIN, self.path, self.state, node)

    def test_role_flip_while_the_signing_key_is_made_stops_before_any_credential(self):
        def code_key(state):  # a managed role write lands while the key is generated
            def flip(doc):
                doc["cluster"]["main_device"] = "dell"
            config_lock.patch_config(state, self.path, flip, backup="cluster-main")
            return HOST_KEY
        with self.assertRaises(ValueError) as caught:
            self.invite(code_key=code_key)
        self.assertIn("no longer the main", str(caught.exception))
        self.assertEqual(self.keys.read_text(encoding="utf-8"), PRIOR + "\n")
        self.assertIsNone(cluster_setup._node_snapshot(self.state, "pixel"))
        disk = self.disk()
        self.assertEqual(disk["cluster"]["main_device"], "dell")
        self.assertNotIn("pixel", disk["devices"])

    def test_tunnel_flip_while_keys_are_made_stops(self):
        def code_key(state):
            self.edit_disk(lambda d: d["cluster"].update(tunnel="reverse"))
            return HOST_KEY
        with self.assertRaises(ValueError):
            self.invite(code_key=code_key)
        self.assertIsNone(cluster_setup._node_snapshot(self.state, "pixel"))

    def test_stale_config_does_not_clobber_newer_edits(self):
        stale = json.loads(json.dumps(MAIN))
        self.edit_disk(lambda d: (d["devices"].update(oracle1={"label": "Oracle", "profile": "cloud"}),
                                  d.update(rules={"confirm": "never"})))
        report = self.invite(config=stale)
        self.assertEqual(report["invited"], "pixel")
        disk = self.disk()
        self.assertEqual(disk["devices"]["oracle1"]["profile"], "cloud")
        self.assertEqual(disk["rules"], {"confirm": "never"})
        self.assertEqual(disk["devices"]["pixel"], {"label": "pixel"})

    def test_cli_invite_is_busy_while_another_writer_holds_the_lock(self):
        errors, held, release = [], threading.Event(), threading.Event()

        def holder():
            with config_mutation(self.state):
                held.set()
                release.wait(10)
        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        try:
            with mock.patch.object(config_lock, "DEFAULT_TIMEOUT", 0.1):
                self.invite()
        except ConfigBusy as exc:
            errors.append(exc)
        finally:
            release.set()
            t.join(10)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), PRIOR + "\n")
        self.assertIsNone(cluster_setup._node_snapshot(self.state, "pixel"))
        self.assertNotIn("pixel", self.disk()["devices"])
        self.assertEqual(self.invite()["invited"], "pixel")  # free again: works

    def test_success_marks_a_running_job_uncertain_only_after_commit(self):
        self.invite()
        self.running_job()
        self.invite(keypair=lambda node: ("-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n", HOST_KEY))
        self.assertEqual(self.job_state(), "uncertain")

    def test_failed_reinvite_leaves_a_running_job_running(self):
        first = self.invite()
        self.running_job()
        real = cluster_setup.encode

        def encode(note):
            raise OSError("disk full")
        with self.assertRaises(cluster_setup.InviteIncomplete) as caught:
            self.invite(encode=encode, keypair=lambda node: (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n", HOST_KEY))
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.job_state(), "running")
        self.assertTrue(Broker(self.state).authenticate(real and cluster_setup.decode(first["code"])["token"]))

    def test_job_finished_during_cleanup_is_never_resurrected(self):
        self.invite()
        self.running_job()

        def encode(note):  # the node reports the job done while this attempt fails
            Broker(self.state).finish(JOB, "pixel", {"ok": True})
            raise OSError("disk full")
        with self.assertRaises(cluster_setup.InviteIncomplete):
            self.invite(encode=encode, keypair=lambda node: (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n", HOST_KEY))
        self.assertEqual(self.job_state(), "done")


class ConsoleInviteLedgerTests(Base):
    """The console invite with the real cluster_setup.invite (keys patched)."""

    def issue(self, request_id, node="oracle1", **body):
        cfg = self.disk()
        return fm.issue_invite(cfg, self.path, self.state,
                               {"request_id": request_id, "node": node, "profile": "cloud", **body})

    def test_console_invite_holds_one_lock_end_to_end(self):
        with self.common():
            out = self.issue("ledger-0001")
        self.assertEqual(out["node"], "oracle1")
        self.assertEqual(self.disk()["devices"]["oracle1"]["profile"], "cloud")
        self.assertTrue(config_lock.held(self.state) is False)

    def test_role_flip_between_device_read_and_replace_is_not_clobbered(self):
        """Audit repro clobber_between_read_and_write: a configure lands after the device
        write read the file and before it replaced it."""
        real = config_lock._backup

        def backup(path, raw, tag, mode):
            self.edit_disk(lambda d: (d["cluster"].update(main_device="dell"),
                                      d.update(audit_marker="concurrent-configure")))
            return real(path, raw, tag, mode)
        with self.common(), mock.patch.object(config_lock, "_backup", side_effect=backup):
            with self.assertRaises(fm.FleetError) as caught:
                self.issue("ledger-0001")
        self.assertEqual((caught.exception.payload["code"], caught.exception.payload["retryable"]),
                         ("config_changed", True))
        disk = self.disk()
        self.assertEqual((disk["cluster"]["main_device"], disk["audit_marker"]), ("dell", "concurrent-configure"))
        self.assertNotIn("oracle1", disk["devices"])
        self.assertIsNone(cluster_setup._node_snapshot(self.state, "oracle1"))
        self.assertEqual(self.keys.read_text(encoding="utf-8"), PRIOR + "\n")
        with self.common():  # the receipt was dropped: the same request can retry, and now sees dell is main
            with self.assertRaises(fm.FleetError) as again:
                self.issue("ledger-0001")
        self.assertEqual(again.exception.payload["code"], "not_main")

    def test_uncertain_attempt_blocks_the_node_even_with_a_new_request_id(self):
        def encode(note):  # someone else's credential replaces ours: cleanup can't be verified
            with Broker(self.state).db() as db:
                db.execute("UPDATE nodes SET token_hash=? WHERE id=?", ("ab" * 32, "oracle1"))
            raise RuntimeError("encode failed")
        with self.common(encode=encode):
            with self.assertRaises(fm.FleetError) as caught:
                self.issue("ledger-0001")
        self.assertFalse(caught.exception.payload["retryable"])
        with self.common():
            for rid, replace in (("ledger-0001", True), ("ledger-0002", False), ("ledger-0003", True)):
                with self.assertRaises(fm.FleetError) as again:
                    self.issue(rid, replace_pending=replace)
                self.assertFalse(again.exception.payload["retryable"])
            self.assertEqual(self.keys.read_text(encoding="utf-8"), PRIOR + "\n")
            # Explicit reconciliation: revoking the node clears the uncertain receipt.
            fm.revoke_node(self.disk(), self.state, {"node": "oracle1", "confirm": "oracle1"},
                           config_path=self.path)
            self.assertEqual(self.issue("ledger-0004")["node"], "oracle1")
        receipts = (self.state / "cluster-invite-receipts.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE KEY", receipts)
        self.assertNotIn("jarvis-invite-1:", receipts)

    def test_issued_receipt_write_failure_is_not_retried_or_reissued(self):
        real = fm._store_receipts

        def store(state, receipts):
            if any(item.get("state") == "issued" for item in receipts.values()):
                raise OSError("disk full")
            return real(state, receipts)
        with self.common(), mock.patch.object(fm, "_store_receipts", side_effect=store):
            with self.assertRaises(fm.FleetError) as caught:
                self.issue("ledger-0001")
        self.assertFalse(caught.exception.payload["retryable"])
        with self.common():
            with self.assertRaises(fm.FleetError) as again:
                self.issue("ledger-0001", replace_pending=True)
        self.assertEqual(again.exception.payload["code"], "already_issued")

    def test_repeat_request_after_a_restart_is_refused_from_the_receipts_file(self):
        with self.common():
            self.issue("ledger-0001")  # nothing is kept in memory: the file is the only ledger
            with self.assertRaises(fm.FleetError) as caught:
                self.issue("ledger-0001", replace_pending=True)
        self.assertEqual(caught.exception.payload["code"], "already_issued")

    def test_fleet_busy_maps_to_a_retryable_409(self):
        with config_mutation(self.state):
            err = []
            with mock.patch.object(config_lock, "DEFAULT_TIMEOUT", 0.1):
                t = threading.Thread(target=lambda: self._busy(err))
                t.start()
                t.join(10)
        self.assertEqual((err[0].status, err[0].payload["code"], err[0].payload["retryable"]),
                         (409, "busy", True))

    def _busy(self, err):
        try:
            with self.common():
                self.issue("ledger-0001")
        except fm.FleetError as exc:
            err.append(exc)


if __name__ == "__main__":
    unittest.main()
