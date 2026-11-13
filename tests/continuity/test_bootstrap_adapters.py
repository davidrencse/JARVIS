"""Temp storage and fake cluster calls only; no live credentials or transports."""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import brainsync, cluster_cli, transfer
from jarvis.continuity import brain_channel, hub_control
from tests.data import test_brainsync as fixtures


class HubControlTests(unittest.TestCase):
    def test_unbound_and_remote_refuse_before_coordinator(self):
        with tempfile.TemporaryDirectory() as root:
            hub = SimpleNamespace(state=Path(root), base=Path(root), config={})
            for peer in ('127.0.0.1', '203.0.113.1'):
                with self.subTest(peer=peer), patch.object(cluster_cli, 'set_main') as run:
                    with self.assertRaises((ValueError, PermissionError)):
                        hub_control.run_main(hub, {'device': 'dell'}, peer)
                    run.assert_not_called()

    def test_bound_call_uses_hub_paths_never_caller_paths(self):
        hub = SimpleNamespace(state=Path('/tmp/fake-state'), base=Path('/tmp/fake-base'), config={})
        with patch.object(cluster_cli, 'configured_handoff', return_value=object()), \
                patch.object(cluster_cli, 'set_main', return_value={'ok': True}) as run:
            self.assertEqual(hub_control.run_main(hub, {'device': 'dell'}, '::1'), {'ok': True})
            run.assert_called_once_with({}, hub.base / 'jarvis.config.json', 'dell', state=hub.state)
            with self.assertRaises(ValueError):
                hub_control.run_main(hub, {'device': 'dell', 'path': '/etc/x'}, '127.0.0.1')

    def test_client_uses_owner_auth_and_no_remote_or_dns_origin(self):
        cfg = {'cluster': {'main_url': 'http://127.0.0.1:7780'}}
        with patch('jarvis.cluster.Client') as client:
            hub_control.request_main(cfg, '/tmp/fake', 'dell')
            client.assert_called_once_with(cfg, '/tmp/fake', owner=True)
            client.return_value.request.assert_called_once_with(
                '/v1/continuity/main', {'device': 'dell'}, timeout=600)
        for origin in ('https://example.com', 'http://localhost:7780'):
            with patch('jarvis.cluster.Client') as client:
                with self.assertRaises(ValueError):
                    hub_control.request_main({'cluster': {'main_url': origin}}, '/tmp/fake', 'dell')
                client.assert_not_called()


class BrainChannelTests(unittest.TestCase):
    def test_windows_target_uses_path_jobs_and_preserves_bytes(self):
        bundle = b'private memory snapshot'
        cfg = {'devices': {'dell': {'os': 'windows', 'brain_transfer_dir': r'C:\staging'}}}
        adapter = brain_channel.ClusterBundleTransport(cfg, '/tmp/fake')
        calls = []
        def call(node, payload):
            calls.append(payload)
            return {'ok': True}
        with patch.object(adapter, '_call', side_effect=call), patch('jarvis.transfer.push', return_value={'sent': 'bundle'}) as push:
            self.assertTrue(adapter.push('dell', bundle)['ok'])
            self.assertEqual(push.call_args.args[3].getvalue(), bundle)
            self.assertEqual(push.call_args.kwargs['dest'], 'path')
        self.assertEqual(calls[0], {'action': 'brain_stage', 'path': r'C:\staging'})
        self.assertEqual(calls[1]['path'], r'C:\staging\bundle')
        self.assertEqual(calls[1]['sha256'], hashlib.sha256(bundle).hexdigest())
        self.assertTrue(transfer.is_path_op(calls[1]))
        self.assertTrue(transfer.is_path_op({'action': 'brain_freeze'}))
        self.assertIsInstance(brainsync._default_transport(cfg, '/tmp/fake'), brain_channel.ClusterBundleTransport)

    def test_no_staging_default_or_ssh_fallback(self):
        self.assertFalse(brain_channel.ClusterBundleTransport({}, '/tmp/fake').push('dell', b'x')['ok'])

    def test_refused_staging_never_uploads(self):
        adapter = brain_channel.ClusterBundleTransport({'devices': {'dell': {'brain_transfer_dir': '/staging'}}}, '/tmp/fake')
        with patch.object(adapter, '_call', return_value={'ok': False}), patch('jarvis.transfer.push') as push:
            self.assertFalse(adapter.push('dell', b'x')['ok'])
            push.assert_not_called()

    def test_git_staging_and_corrupt_receive_refuse(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root); (folder / '.git').mkdir()
            cfg = {}
            with patch('jarvis.transfer._within', return_value={'path': root, 'dir': True}):
                with self.assertRaisesRegex(ValueError, 'outside Git'):
                    brain_channel.run_here({'action': 'brain_stage', 'path': root}, folder / '.jarvis', cfg)
            (folder / '.git').rmdir()
            file = folder / 'x'; file.write_bytes(b'bad')
            with patch('jarvis.transfer._within', return_value={'path': str(file), 'dir': False}), patch.object(brainsync, 'receive') as receive:
                with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                    brain_channel.run_here({'action': 'brain_receive', 'path': str(file), 'sha256': '0'*64}, folder / '.jarvis', cfg)
                receive.assert_not_called()

    def test_pull_refuses_changed_size_and_digest(self):
        adapter = brain_channel.ClusterBundleTransport({'devices': {'dell': {'brain_transfer_dir': '/staging'}}}, '/tmp/fake')
        for size, digest in ((2, hashlib.sha256(b'x').hexdigest()), (1, '0'*64)):
            with patch.object(adapter, '_call', side_effect=[{'ok': True, 'name': 'x', 'size': 1, 'sha256': digest}, {'size': size, 'data': base64.b64encode(b'x').decode()}]):
                self.assertFalse(adapter.pull('dell', b'{}')['ok'])

    def test_request_shape_and_size_refuse(self):
        for body in ({'action': 'brain_freeze', 'request': 'x'*4097},
                     {'action': 'brain_freeze', 'request': '{}', 'force': True},
                     {'action': 'brain_receive', 'path': '/x', 'sha256': 'bad'}):
            with self.assertRaises(ValueError): transfer.validate(body)


class RealBundleChannelTests(unittest.TestCase):
    def test_bundle_to_windows_target_through_fake_transfer_jobs(self):
        fixture = fixtures.BrainSyncTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        arch, ac, _ = fixture.make('arch', 'arch', {'note.md': 'kept'}, ['private memory'])
        dell, dc, vault = fixture.make('dell', 'arch', {})
        staging = fixture.root / 'staging'; staging.mkdir()
        ac['devices']['dell'].update(os='windows', brain_transfer_dir=str(staging))
        original = transfer.run_here
        def within(config, path):
            target = Path(path).resolve()
            if target != staging and staging not in target.parents:
                raise ValueError('outside storage roots')
            return {'path': str(target), 'dir': target.is_dir(),
                    'size': target.stat().st_size}
        def fake_call(config, state, device, payload, timeout=120):
            self.assertEqual(device, 'dell')
            return original(payload, dell / '.jarvis', dc)
        with patch('jarvis.transfer.call', side_effect=fake_call), \
                patch('jarvis.transfer._within', side_effect=within), \
                patch('jarvis.deploy._remote', side_effect=AssertionError('SSH forbidden')):
            result = brainsync.handoff('arch', 'dell', ac, arch)
        self.assertTrue(result['ok'], result)
        self.assertEqual((vault / 'note.md').read_text(), 'kept')
        import sqlite3
        with sqlite3.connect(dell / '.jarvis' / 'memory.sqlite3') as db:
            self.assertEqual(db.execute('select body from items').fetchone()[0], 'private memory')
        self.assertFalse(list(staging.glob('*.bundle')))


class BridgeOptInTests(unittest.TestCase):
    def test_absent_invalid_or_wrong_installation_does_not_opt_in(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = {'cluster': {'enabled': True, 'local_device': 'arch', 'main_device': 'arch'},
                   'devices': {'arch': {}}}
            self.assertFalse(hub_control.operator_configured(cfg, root))
            cfg['continuity'] = {'operator_config': 'missing.json'}
            self.assertFalse(hub_control.operator_configured(cfg, root))
            path = Path(root) / 'operator.json'; path.write_text('not json')
            cfg['continuity']['operator_config'] = str(path)
            self.assertFalse(hub_control.operator_configured(cfg, root))
            data = {'origin': 'http://127.0.0.1:8766', 'authority_id': 'test',
                    'device_id': 'dell', 'installation_id': 'dell-test', 'private_key_path': 'fake-key'}
            path.write_text(json.dumps(data))
            self.assertFalse(hub_control.operator_configured(cfg, root))
            data.update(device_id='arch', origin='http://public.example:8766')
            path.write_text(json.dumps(data))
            self.assertFalse(hub_control.operator_configured(cfg, root))

    def test_cli_bridge_runs_before_fleet_lock(self):
        cfg = {'continuity': {'operator_config': 'fake'}}
        with patch.object(cluster_cli, 'configured_handoff', return_value=None), \
                patch.object(hub_control, 'operator_configured', return_value=True), \
                patch.object(hub_control, 'request_main', return_value={'ok': False}) as request, \
                patch('jarvis.config_lock.config_mutation', side_effect=AssertionError('client must not hold lock')):
            result = cluster_cli.set_main(cfg, '/tmp/fake/jarvis.config.json', 'dell', '/tmp/fake-state')
        self.assertEqual(result, {'ok': False})
        request.assert_called_once_with(cfg, '/tmp/fake-state', 'dell')


class HubRouteAuthTests(unittest.TestCase):
    def test_route_rejects_node_credentials_before_dispatch_without_socket(self):
        import io
        from jarvis import cluster, ui
        hub = SimpleNamespace(console=None, config={})
        def server_init(server, address, handler):
            server.RequestHandlerClass = handler
        with patch.object(ui.ConsoleServer, '__init__', server_init):
            server = cluster.make_server(hub, '127.0.0.1', 0)
        cases = [('/v1/continuity/main', {'device': 'dell'}, caps, expected)
                 for caps, expected in (({'files'}, 403), ({'owner'}, 200))]
        cases.append(('/v1/continuity/recover', {'device': 'arch', 'request_id': 'recover-1'}, {'files', 'console'}, 403))
        cases.append(('/v1/control', {'kind': 'transfer', 'node': 'arch',
            'payload': {'action': 'brain_freeze', 'request': '{}'}}, {'files', 'console'}, 403))
        for path, payload, caps, expected in cases:
            hub.authenticate = lambda headers, capability=None, source=None: ('arch', caps)
            handler = object.__new__(server.RequestHandlerClass)
            handler.path = path
            body = json.dumps(payload).encode()
            from email.message import Message
            handler.headers = Message()
            handler.headers['Content-Length'] = str(len(body))
            handler.headers['Host'] = '127.0.0.1'
            handler.headers['X-JARVIS'] = '1'
            handler.rfile = io.BytesIO(body)
            handler.connection = SimpleNamespace(settimeout=lambda _: None)
            handler.client_address = ('127.0.0.1', 1234)
            sent = []
            with patch.object(ui.Handler, '_send', lambda self, status, body: sent.append((status, body))), \
                    patch.object(hub_control, 'run_main', return_value={'ok': True}) as run:
                handler.do_POST()
            self.assertEqual(sent[0][0], expected)
            self.assertEqual(run.call_count, int(expected == 200))
