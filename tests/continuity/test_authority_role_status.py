"""Static configured hubs must not be reported as the leased MAIN."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import cluster_cli
from jarvis.cluster import Client, Hub
from jarvis.continuity import integration
from jarvis.continuity.role_status import report
from tests.cluster.test_cluster import configuration


class AuthorityRoles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / '.jarvis'
        self.config = configuration()
        self.config['continuity'] = {'operator_config': 'operator.json'}
        self.ready = {'state': 'ok', 'host': 'gateway', 'mode': 'V2_ACTIVE',
                      'main': 'node', 'lease_valid': True, 'main_ready': True,
                      'incarnation': 5, 'epoch': 35}

    def manage(self):
        integration._write_marker(self.state)

    def test_unmanaged_roles_keep_configured_main_without_gateway_call(self):
        with patch('jarvis.continuity.console._fetch') as fetch:
            out = report(self.config, self.state)
        self.assertEqual(out, {'main_device': 'main', 'role': 'main'})
        fetch.assert_not_called()

    def test_managed_roles_use_authority_and_keep_configured_topology(self):
        self.manage()
        with patch('jarvis.continuity.console._fetch', return_value=self.ready) as fetch:
            out = cluster_cli.roles(self.config, self.state)
        self.assertEqual(out['main_device'], 'node')
        self.assertEqual(out['configured_main_device'], 'main')
        self.assertEqual(out['role'], 'node')
        self.assertEqual(out['devices']['node']['role'], 'main')
        self.assertEqual(out['devices']['main']['role'], 'node')
        fetch.assert_called_once_with(self.base / 'operator.json', None, 3.0)

    def test_expired_preparing_or_unknown_authority_never_falls_back_to_static_main(self):
        self.manage()
        for answer in ({**self.ready, 'lease_valid': False},
                       {**self.ready, 'main_ready': False},
                       {**self.ready, 'mode': 'HANDOVER'},
                       {'state': 'unreachable', 'error': 'Timeout'}):
            with self.subTest(answer=answer), patch('jarvis.continuity.console._fetch', return_value=answer):
                out = cluster_cli.roles(self.config, self.state)
            self.assertIsNone(out['main_device'])
            self.assertEqual(out['role'], 'unknown')
            self.assertTrue(all(d['role'] == 'unknown' for d in out['devices'].values()))

    def test_cli_status_preserves_transport_connection_without_claiming_main(self):
        self.manage()
        args = SimpleNamespace(action='status', config=str(self.base / 'config.json'), state=str(self.state))
        with patch('jarvis.continuity.console._fetch', return_value=self.ready), \
                patch('jarvis.cluster_cli.Client') as client:
            client.return_value.request.return_value = {'main_device': 'main', 'nodes': []}
            out = cluster_cli.run(args, self.config, self.base)
        self.assertTrue(out['connected'])
        self.assertEqual(out['main_device'], 'node')
        self.assertEqual(out['configured_main_device'], 'main')
        self.assertEqual(out['role'], 'node')

    def test_old_hub_status_reports_authority_and_listener_identity_separately(self):
        hub = Hub(self.config, self.base, self.state).start(port=0)
        self.addCleanup(hub.close)
        self.config['cluster']['main_url'] = f'http://127.0.0.1:{hub.server.server_address[1]}'
        self.manage()  # avoid startup trying to bind the deliberately fake operator
        with patch('jarvis.continuity.console._fetch', return_value=self.ready):
            out = Client(self.config, self.state, owner=True).request('/v1/status')
        self.assertEqual(out['main_device'], 'node')
        self.assertEqual(out['configured_main_device'], 'main')
        self.assertEqual(out['hub_device'], 'main')
        self.assertEqual(out['role'], 'node')

    def test_successor_handler_uses_console_attached_after_listener_started(self):
        hub = Hub(self.config, self.base, self.state).start(port=0)
        self.addCleanup(hub.close)
        self.config['cluster']['main_url'] = f'http://127.0.0.1:{hub.server.server_address[1]}'
        console = Mock()
        console.remote_status.return_value = {'devices': [{'id': 'other'}]}
        hub.console = console
        out = Client(self.config, self.state, owner=True).request('/api/remote')
        self.assertEqual(out, {'devices': [{'id': 'other'}]})
        console.remote_status.assert_called_once_with()
