import argparse
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis.continuity import bootstrap, cli


class Client:
    def __init__(self, device, installation, status):
        self.device_id = device
        self.installation_id = installation
        self._owner_key = None
        self._status = status
        self.calls = []

    def status(self):
        self.calls.append("status")
        return dict(self._status)

    def renew(self, *args): return {}
    def release(self, *args): return {}
    def acknowledge(self, *args, **kwargs): return {"ok": True}
    def readiness(self, *args, **kwargs): return {}
    def step(self, *args, **kwargs): return {}
    def transition_request(self, *args, **kwargs): return {}
    def consume(self, *args, **kwargs): return {}
    def admission(self, *args, **kwargs): return {}


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="continuity-bootstrap-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state = self.root / "state"
        self.hub_path = self.root / "jarvis.config.json"
        self.operator_path = self.root / "operator.json"
        self.key_path = self.root / "local-key"
        self.remote_key_path = self.root / "remote-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key_path)],
                       check=True, capture_output=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.remote_key_path)],
                       check=True, capture_output=True)
        public = " ".join(self.key_path.with_suffix(".pub").read_text().strip().split()[:2])
        self.operator = {
            "origin": "https://gateway.invalid",
            "authority_id": "test-authority",
            "fleet_id": "test-fleet",
            "device_id": "zenbook",
            "installation_id": "zen-inst",
            "private_key_path": str(self.key_path),
            "enrollment": {"zenbook": {"kind": "pc", "installation_id": "zen-inst",
                                          "public_key": public},
                           "dell": {"kind": "pc", "installation_id": "dell-inst",
                                    "public_key": " ".join(self.remote_key_path.with_suffix(".pub").read_text().strip().split()[:2])}},
            "successor_routes": {
                "zenbook": {"endpoint": "https://arch.tailnet.test:8443",
                            "listen_host": "127.0.0.1", "listen_port": 7781, "link": None},
                "dell": {"endpoint": "https://dell.tailnet.test:8443",
                         "listen_host": "127.0.0.1", "listen_port": 7781, "link": None}},
        }
        self.original_hub = {
            "cluster": {"enabled": True, "local_device": "zenbook", "main_device": "zenbook",
                        "main_url": "https://arch.tailnet.test"},
            "devices": {"zenbook": {}, "dell": {}, "oracle": {"kind": "server"}},
        }
        self.hub_path.write_text(json.dumps(self.original_hub), encoding="utf-8")
        self.operator_path.write_text(json.dumps(self.operator), encoding="utf-8")
        self.settings_path = self.root / "continuity-settings.json"
        self.settings = {"schema": 1, "staging_dirs": {
            "zenbook": str(self.root / "local-brain-stage"),
            "dell": r"C:\Users\jdoe\JARVIS\.brain-transfer"},
            "brainsync_enabled": True}
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        self.client = Client("zenbook", "zen-inst", self.status())

    def status(self, mode="LEGACY_ACTIVE", version=1, **extra):
        return {"authority_id": "test-authority", "fleet_id": "test-fleet",
                "device_id": "zenbook", "mode": mode, "mode_version": version,
                "incarnation": 2, "epoch": 7, "lease_valid": False,
                "main_ready": False, "transition": None, **extra}

    def factory(self, _path):
        return self.client, self.operator

    def configure_local(self):
        staged = bootstrap.stage(self.operator_path, self.root / "staged",
            hub_config=self.hub_path, settings_manifest=self.settings_path,
            client_factory=self.factory)
        result = bootstrap.configure(self.hub_path, self.state, staged["operator_config"],
                                     settings_manifest=staged["settings_manifest"],
                                     confirmed=True, client_factory=self.factory)
        return Path(result["operator_config"])

    def test_preflight_is_read_only_and_does_not_create_managed_marker(self):
        report = bootstrap.preflight(self.hub_path, self.state, self.operator_path,
                                     client_factory=self.factory)
        self.assertFalse(report["ok"])
        self.assertIn("storage_path_invalid", report["blockers"])
        report = bootstrap.preflight(self.hub_path, self.state, self.operator_path,
            settings_manifest=self.settings_path, client_factory=self.factory)
        self.assertTrue(report["ok"])
        self.assertEqual(report["gateway_mode"], "LEGACY_ACTIVE")
        self.assertFalse(report["managed_marker_present"])
        self.assertFalse(report["activation_performed"])
        self.assertFalse(self.state.exists())
        self.assertEqual(json.loads(self.hub_path.read_text()), self.original_hub)

    def test_stage_only_copies_metadata_and_does_not_change_role_or_state(self):
        staging, storage = self.root / "setup" / "operator", self.root / "local-brain-stage"
        result = bootstrap.stage(self.operator_path, staging,
            hub_config=self.hub_path, settings_manifest=self.settings_path,
            client_factory=self.factory)
        staged = json.loads(Path(result["operator_config"]).read_text())
        self.assertEqual(staged["private_key_path"], str(self.key_path.resolve()))
        self.assertFalse(result["owner_key_copied"])
        self.assertFalse(result["managed_marker_created"])
        self.assertFalse(self.state.exists())
        self.assertTrue(storage.is_dir())
        self.assertTrue((storage / ".jarvis-continuity-storage.json").is_file())
        self.assertFalse((self.state / "continuity-managed.json").exists())
        self.assertEqual(json.loads(self.hub_path.read_text()), self.original_hub)
        self.assertFalse((staging / self.key_path.name).exists())
        self.assertNotIn(self.key_path.read_text(), json.dumps(staged))
        self.assertEqual(os.stat(staging).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(Path(result["operator_config"])).st_mode & 0o777, 0o600)

    def test_configure_writes_backups_and_operator_pointer_without_activation(self):
        staged = bootstrap.stage(self.operator_path, self.root / "staged",
            hub_config=self.hub_path, settings_manifest=self.settings_path,
            client_factory=self.factory)
        result = bootstrap.configure(self.hub_path, self.state, staged["operator_config"],
                                     settings_manifest=staged["settings_manifest"],
                                     confirmed=True, client_factory=self.factory)
        config = json.loads(self.hub_path.read_text())
        staged_operator = json.loads(Path(result["operator_config"]).read_text())
        self.assertEqual(config["cluster"]["main_device"], "zenbook")
        self.assertEqual(config["cluster"]["main_url"], "https://arch.tailnet.test")
        self.assertEqual(config["devices"]["dell"]["brain_transfer_dir"],
                         r"C:\Users\jdoe\JARVIS\.brain-transfer")
        self.assertNotIn("brain_transfer_dir", config["devices"]["oracle"])
        self.assertTrue(config["brainsync"]["enabled"])
        self.assertEqual(Path(config["continuity"]["operator_config"]), self.root / "xdg/jarvis/continuity/operator.json")
        self.assertEqual(Path(result["config_backup"]).read_bytes(), json.dumps(self.original_hub).encode())
        self.assertEqual(staged_operator["private_key_path"], str(self.key_path.resolve()))
        self.assertFalse(result["managed_marker_created"])
        self.assertFalse((self.state / "continuity-managed.json").exists())

    def test_incomplete_route_refuses_without_partial_configuration(self):
        self.operator["successor_routes"] = {}
        self.operator_path.write_text(json.dumps(self.operator), encoding="utf-8")
        before = self.hub_path.read_bytes()
        with self.assertRaises(Exception):
            bootstrap.configure(self.hub_path, self.state, self.operator_path,
                                settings_manifest=self.settings_path,
                                confirmed=True, client_factory=self.factory)
        self.assertEqual(self.hub_path.read_bytes(), before)
        self.assertFalse(self.hub_path.with_name(self.hub_path.name + ".pre-continuity-bootstrap").exists())
        self.assertFalse((self.root / "continuity-operator.json").exists())
        self.assertFalse((self.state / "continuity-managed.json").exists())

    def test_invalid_local_pin_refuses_before_any_side_effect(self):
        self.operator["enrollment"]["zenbook"]["public_key"] = "ssh-ed25519 AAAAWRONG key"
        self.operator_path.write_text(json.dumps(self.operator), encoding="utf-8")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "local_pin_mismatch"):
            bootstrap.configure(self.hub_path, self.state, self.operator_path,
                                settings_manifest=self.settings_path,
                                confirmed=True, client_factory=self.factory)
        self.assertEqual(json.loads(self.hub_path.read_text()), self.original_hub)
        self.assertFalse(self.state.exists())

    def test_configure_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(bootstrap.BootstrapError, "confirmation_required"):
            bootstrap.configure(self.hub_path, self.state, self.operator_path,
                                settings_manifest=self.settings_path,
                                client_factory=self.factory)
        self.assertEqual(json.loads(self.hub_path.read_text()), self.original_hub)

    def test_cutover_requires_active_service_and_exact_quiescent_gateway_state(self):
        life = SimpleNamespace(supported=lambda: True, active=lambda _unit: False,
                               pause=lambda _state: {})
        with self.assertRaisesRegex(bootstrap.BootstrapError, "local_service_not_active"):
            bootstrap.cutover(self.hub_path, self.state, self.operator_path,
                expect_mode="QUIESCED", expect_version=3, confirmed=True,
                client_factory=self.factory, lifecycle=life)
        self.assertFalse(self.client.calls)
        configured_operator = self.configure_local()
        self.client._status = self.status(mode="QUIESCED", version=3,
                                         transition={"state": "DRAINING"})
        life.active = lambda _unit: True
        with mock.patch.object(life, "pause", side_effect=AssertionError("must not pause")):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "gateway_transition_open"):
                bootstrap.cutover(self.hub_path, self.state, configured_operator,
                    expect_mode="QUIESCED", expect_version=3, confirmed=True,
                    client_factory=self.factory, lifecycle=life)
        self.assertFalse((self.state / "continuity-managed.json").exists())

    def test_cutover_binds_then_durably_fences_only_after_pause(self):
        configured_operator = self.configure_local()
        self.client._status = self.status(mode="QUIESCED", version=3)
        events = []

        class Guard:
            def accept(self, *args, **kwargs): return None
            def fence(self, incarnation, epoch, version):
                events.append(("fence", incarnation, epoch, version))
                return {"authority_recorded": True, "mode_version": version}
            def local_view(self):
                return {"fenced_epoch": 7, "legacy_closed": True}
            def close(self):
                events.append(("close",))

        participant = SimpleNamespace(runtime=SimpleNamespace(
            require_authority=lambda *_: None, release_before_suspend=lambda: None))
        controller = SimpleNamespace(_production_issuer=lambda *_: {})
        service = {"active": True}
        lifecycle = SimpleNamespace(
            supported=lambda: True, active=lambda _unit: service["active"],
            pause=lambda _state: events.append(("pause",)) or
                service.update(active=False) or
                {"paused": True, "stopped": ["jarvis-cluster.service"], "failed": []},
            wake=lambda _state: events.append(("wake",)) or {"woke": True, "failed": []})
        with mock.patch.object(bootstrap, "_validate_production",
                               return_value=(participant, Guard(), controller)), \
             mock.patch("jarvis.continuity.participant.bind"):
            result = bootstrap.cutover(self.hub_path, self.state, configured_operator,
                expect_mode="QUIESCED", expect_version=3, confirmed=True,
                client_factory=self.factory, lifecycle=lifecycle)
        self.assertEqual(events[0], ("pause",))
        self.assertEqual(events[1], ("fence", 2, 7, 4))
        self.assertEqual(events[-1], ("wake",))
        self.assertTrue(result["durable_ack_recorded"])
        self.assertTrue((self.state / "continuity-managed.json").is_file())

    def test_windows_cutover_waits_for_cooperative_writer_before_fence_and_restores(self):
        from jarvis.cluster import NodeWorker
        from jarvis.locking import exclusive_lock
        from jarvis.continuity.local_lifecycle import NODE_LOCK, WindowsNodeLifecycle
        configured_operator = self.configure_local()
        self.state.mkdir(exist_ok=True)
        self.client._status = self.status(mode="QUIESCED", version=3)
        old_started, old_finished = threading.Event(), threading.Event()
        new_started, release_new = threading.Event(), threading.Event()
        event_log = []

        def worker(started, release=None):
            node = object.__new__(NodeWorker)
            node.state = self.state
            node.config_path = str(self.hub_path)
            node.stop_event = threading.Event()
            node._process_registration = None
            node._cooperative_request = None
            node.cooperative_stop = False
            with exclusive_lock(self.state / NODE_LOCK):
                node._register_local_process()
                started.set()
                while release is None or not release.is_set():
                    if node._cooperative_stop_requested():
                        event_log.append("cooperative-stop-accepted")
                        node._close_managed_process = lambda: None
                        node._finish_local_process()
                        event_log.append("worker-cleanup-complete")
                        return
                    time.sleep(0.005)
                node._finish_local_process()

        old_thread = threading.Thread(target=worker, args=(old_started,), daemon=True)
        old_thread.start()
        self.assertTrue(old_started.wait(2))
        new_thread = None

        def launch(config, state):
            nonlocal new_thread
            self.assertEqual(Path(config), self.hub_path.resolve())
            self.assertEqual(Path(state), self.state.resolve())
            event_log.append("restore-launch")
            new_thread = threading.Thread(target=worker, args=(new_started, release_new), daemon=True)
            new_thread.start()

        local = WindowsNodeLifecycle(self.hub_path, self.state, "zenbook",
                                     timeout=2, launch=launch, windows=True)

        class Guard:
            def accept(self, *args, **kwargs): return None
            def fence(_self, incarnation, epoch, version):
                self.assertFalse(local.active(local.service_name),
                                 "fence ack must wait until the cluster writer lock is released")
                event_log.append("durable-fence-ack")
                return {"authority_recorded": True, "mode_version": version}
            def local_view(_self):
                return {"fenced_epoch": 7, "legacy_closed": True}
            def close(_self): pass

        participant = SimpleNamespace(runtime=SimpleNamespace(
            require_authority=lambda *_: None, release_before_suspend=lambda: None))
        controller = SimpleNamespace(_production_issuer=lambda *_: {})
        with mock.patch.object(bootstrap, "_validate_production",
                               return_value=(participant, Guard(), controller)), \
             mock.patch("jarvis.continuity.participant.bind"):
            result = bootstrap.cutover(self.hub_path, self.state, configured_operator,
                expect_mode="QUIESCED", expect_version=3, confirmed=True,
                client_factory=self.factory, lifecycle=local)
        self.assertTrue(result["durable_ack_recorded"])
        self.assertLess(event_log.index("worker-cleanup-complete"), event_log.index("durable-fence-ack"))
        self.assertLess(event_log.index("durable-fence-ack"), event_log.index("restore-launch"))
        self.assertTrue(new_started.is_set())
        self.assertTrue(local.active(local.service_name))
        release_new.set()
        new_thread.join(2)
        old_thread.join(2)

    def test_operator_commands_parse_and_do_not_need_global_continuity_config(self):
        parser = argparse.ArgumentParser()
        cli.add_arguments(parser)
        args = parser.parse_args(["bootstrap", "preflight", "--hub-config", "hub.json",
            "--state", "state", "--operator-config", "operator.json"])
        self.assertEqual(args.bootstrap_action, "preflight")
        args = parser.parse_args(["bootstrap", "cutover", "--hub-config", "hub.json",
            "--state", "state", "--operator-config", "operator.json",
            "--expect-mode", "QUIESCED", "--expect-version", "4", "--yes"])
        self.assertEqual((args.expect_mode, args.expect_version, args.yes), ("QUIESCED", 4, True))

    def test_operator_cli_stage_and_configure_commands_are_executable(self):
        parser = argparse.ArgumentParser()
        cli.add_arguments(parser)
        staged_path = self.root / "staged" / "continuity-operator.json"
        out = []
        args = parser.parse_args(["bootstrap", "preflight", "--hub-config", str(self.hub_path),
            "--state", str(self.state), "--operator-config", str(self.operator_path),
            "--settings-manifest", str(self.settings_path)])
        self.assertEqual(cli.run(args, client_factory=self.factory, output=out.append), 0)
        args = parser.parse_args(["bootstrap", "prepare", "--hub-config", str(self.hub_path),
            "--operator-config", str(self.operator_path), "--staging-dir", str(staged_path.parent),
            "--settings-manifest", str(self.settings_path)])
        self.assertEqual(cli.run(args, client_factory=self.factory, output=out.append), 0)
        self.assertTrue(staged_path.is_file())
        args = parser.parse_args(["bootstrap", "configure", "--hub-config", str(self.hub_path),
            "--state", str(self.state), "--operator-config", str(staged_path),
            "--settings-manifest", str(staged_path.parent / "continuity-settings-manifest.json"),
            "--yes"])
        self.assertEqual(cli.run(args, client_factory=self.factory, output=out.append), 0)
        self.assertFalse((self.state / "continuity-managed.json").exists())
        self.assertEqual(json.loads(self.hub_path.read_text())["cluster"]["main_device"], "zenbook")
        self.assertNotIn(self.key_path.read_text(), "".join(out))


if __name__ == "__main__":
    unittest.main()
