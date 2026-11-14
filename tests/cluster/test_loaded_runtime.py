"""Startup attestation stays honest when a release changes under a running hub."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.cluster import Client, Hub
from jarvis import release_runtime as runtime
from tests.cluster.test_cluster import configuration


class LoadedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with patch('jarvis.cluster.code_fingerprint', return_value='old-loaded'):
            self.hub = Hub(configuration(), self.root, self.root / 'state')

    def test_startup_capture_survives_release_thread_and_disk_change(self):
        # Stop at the first maintenance iteration, without release/deployment work.
        with patch.object(self.hub.stop_event, 'wait', side_effect=[False, True]), \
                patch.object(self.hub, '_auto_update'), \
                patch('jarvis.release_runtime.maybe_restart'), \
                patch('jarvis.cluster.code_fingerprint', return_value='new-disk'):
            self.hub._keep_up(settle=0)
        self.assertEqual(self.hub.loaded_code, 'old-loaded')
        self.assertEqual(self.hub._release_started_code, 'old-loaded')

    def test_status_reports_served_release_without_claiming_it_is_loaded(self):
        self.hub.start(port=0)
        self.addCleanup(self.hub.close)
        self.hub.config['cluster']['main_url'] = (
            f'http://127.0.0.1:{self.hub.server.server_address[1]}')
        client = Client(self.hub.config, self.hub.state, owner=True)
        with patch('jarvis.release.served_code', return_value='new-release'), \
                patch('jarvis.deploy.code_fingerprint', return_value='new-release'):
            status = client.request('/v1/status')
        self.assertEqual(status['code'], 'new-release')  # legacy wire protocol
        self.assertEqual(status['served_code'], 'new-release')
        self.assertEqual(status['disk_code'], 'new-release')
        self.assertEqual(status['loaded_code'], 'old-loaded')
        self.assertTrue(status['restart_required'])

    def test_main_endpoint_refuses_before_control_or_brain_transfer(self):
        self.hub.start(port=0)
        self.addCleanup(self.hub.close)
        self.hub.config['cluster']['main_url'] = (
            f'http://127.0.0.1:{self.hub.server.server_address[1]}')
        client = Client(self.hub.config, self.hub.state, owner=True)
        with patch('jarvis.release.current', return_value={'code': 'new-release'}), \
                patch('jarvis.release.code_fingerprint', return_value='new-release'), \
                patch('jarvis.continuity.hub_control.run_main') as control:
            with self.assertRaisesRegex(RuntimeError, 'restart jarvis-cluster'):
                client.request('/v1/continuity/main', {'device': 'node'})
        control.assert_not_called()

    def test_gate_requires_loaded_tested_and_disk_code_to_match(self):
        for loaded, disk, tested, allowed in (
                ('old-loaded', 'new-release', 'new-release', False),
                ('new-release', 'dirty', 'new-release', False),
                (None, 'new-release', 'new-release', False),
                ('new-release', 'new-release', None, False),
                ('new-release', 'new-release', 'new-release', True)):
            with self.subTest(loaded=loaded, disk=disk, tested=tested), \
                    patch('jarvis.release.current', return_value={'code': tested} if tested else {}), \
                    patch('jarvis.release.code_fingerprint', return_value=disk):
                hub = SimpleNamespace(state=self.hub.state, loaded_code=loaded)
                if allowed:
                    self.assertTrue(runtime.require_loaded_code(hub)['matched'])
                else:
                    with self.assertRaises(ValueError):
                        runtime.require_loaded_code(hub)

    def test_restart_uses_loaded_code_even_if_old_bookkeeping_claims_new(self):
        hub = SimpleNamespace(state=self.hub.state, config={'cluster': {'auto_restart': True}},
                              loaded_code='old-loaded', _release_started_code='new-release')
        with patch('jarvis.release.current', return_value={'code': 'new-release'}), \
                patch('jarvis.deploy.code_fingerprint', return_value='new-release'), \
                patch.object(runtime, 'busy', return_value=False), \
                patch('jarvis.cluster._systemd_service', return_value=True), \
                patch.object(runtime.os, '_exit', side_effect=SystemExit) as exit_process:
            with self.assertRaises(SystemExit):
                runtime.maybe_restart(hub)
        exit_process.assert_called_once_with(75)
