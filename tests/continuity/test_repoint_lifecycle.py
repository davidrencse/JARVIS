"""Real instruction/probe signatures and state publication; every OS action faked."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.continuity import repoint, lifecycle, participant as rpc
from jarvis.continuity.runtime import RuntimeRefusal, GatewayPort, MainRuntime
from jarvis.continuity.executor import ExecutorGuard
from jarvis.cluster_store import Broker
from jarvis.locking import exclusive_lock, LockBusy
from tests.continuity import test_participant_channel as fixtures
from tests.continuity.test_gateway import _keys


class RepointLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.env = fixtures.ParticipantChannelTests(); self.env.setUp()
        self.addCleanup(self.env.doCleanups)
        e = self.env
        e.seat_zenbook(); tid = e.open('dell', 'set_preferred_main')
        peers = {d: rpc.RemoteRuntime(d, e.port, e.call) for d in e.participants}
        port = rpc.CandidatePort(e.port, peers['dell'], peers.values())
        port.transition_readiness(tid, peers['dell'].readiness()); port.transition_step(tid, 'drain')
        peers['zenbook'].begin_drain(tid); peers['zenbook'].release_for_handoff(tid)
        port.transition_step(tid, 'fence'); peers['dell'].accept_lease(port.transition_step(tid, 'grant'))
        self.pins = {d: {'installation_id': d+'-inst', 'public_key': _keys()[d][0]} for d in e.participants}
        self.broker = Broker(e.states['dell'])
        self.issuer = repoint.Issuer(e.participants['dell'], e.states['dell'], self.broker)
        self.endpoint = 'https://dell.example.ts.net:443'
        self.hub = SimpleNamespace(state=e.states['dell'], main='dell', started=True, continuity_endpoint=self.endpoint)

    def offer(self, target='zenbook'):
        return self.issuer.issue(target, self.endpoint)

    def probe(self, body, nonce):
        return repoint.endpoint_receipt(self.hub, body['target'], nonce)

    def installer(self):
        return repoint.Installer(self.env.participants['zenbook'], self.env.states['zenbook'], self.pins, probe=self.probe)

    def test_signed_endpoint_and_key_publication_uses_new_credentials_without_config_edit(self):
        e = self.env; state = e.states['zenbook']; instruction = self.offer()
        result = self.installer().install(instruction)
        self.assertTrue(all(result.values()))
        note = json.loads((state/'cluster-node.json').read_text())
        self.assertEqual(self.broker.authenticate(note['token'])[0], 'zenbook')
        self.assertEqual(note['code_key'], instruction['body']['code_key'])
        cfg = {'cluster': {'enabled': True, 'local_device': 'zenbook', 'main_device': 'zenbook'},
               'devices': {'zenbook': {}, 'dell': {}}, 'continuity': {'operator_config': 'operator.json'}}
        (state.parent/'operator.json').write_text(json.dumps({'origin': 'http://127.0.0.1:1',
            'authority_id': 'oracle-auth', 'device_id': 'zenbook', 'installation_id': 'zenbook-inst',
            'private_key_path': str(_keys()['zenbook'][1]), 'enrollment': self.pins}))
        original = copy.deepcopy(cfg)
        view = repoint.effective_config(cfg, state)
        self.assertEqual(view['cluster']['main_device'], 'dell')
        self.assertEqual(view['cluster']['main_url'], self.endpoint)
        self.assertEqual(cfg, original)
        from jarvis.cluster import Client
        client = Client(cfg, state)
        self.assertEqual(client.main, 'dell'); self.assertEqual(client.url, self.endpoint)
        self.assertEqual(client.token, note['token'])
        # Omitted link and JSON null are the same direct-HTTPS route.
        null_note = dict(note); null_note['link'] = None
        (state/'cluster-node.json').write_text(json.dumps(null_note))
        null_view = repoint.effective_config(cfg, state)
        self.assertEqual(null_view['cluster']['main_device'], 'dell')
        self.assertEqual(null_view['cluster']['main_url'], self.endpoint)
        self.assertEqual(cfg, original)
        # No instruction fallback after partial/tampered pairing publication.
        note['code_key'] = _keys()['zenbook'][0]
        (state/'cluster-node.json').write_text(json.dumps(note))
        with self.assertRaises(RuntimeRefusal): repoint.effective_config(cfg, state)

    def test_offer_retry_does_not_rotate_credential(self):
        first = self.offer(); second = self.offer()
        self.assertEqual(first, second)
        self.assertEqual(self.broker.authenticate(first['body']['token'])[0], 'zenbook')

    def test_probe_cannot_change_the_instruction_published_after_verification(self):
        instruction = self.offer()
        original = copy.deepcopy(instruction)
        def probe(body, nonce):
            instruction['body']['endpoint'] = 'https://evil.example:443'
            return self.probe(body, nonce)
        installer = repoint.Installer(self.env.participants['zenbook'],
            self.env.states['zenbook'], self.pins, probe=probe)
        installer.install(instruction)
        saved = json.loads((self.env.states['zenbook']/repoint.FILE).read_text())
        self.assertEqual(saved, original)

    def test_tampered_wrong_target_and_wrong_installation_are_refused(self):
        original = self.offer()
        for key, value in (('endpoint', 'https://evil.example.ts.net:443'), ('target', 'dell'),
                           ('installation_id', 'other-installation'), ('code_key', _keys()['zenbook'][0])):
            instruction = copy.deepcopy(original); instruction['body'][key] = value
            with self.subTest(field=key), self.assertRaises(RuntimeRefusal): self.installer().install(instruction)
        self.assertFalse((self.env.states['zenbook']/repoint.FILE).exists())

    def test_replayed_probe_nonce_and_expired_grant_never_publish(self):
        instruction = self.offer()
        stale = self.probe(instruction['body'], 'old-nonce')
        bad = repoint.Installer(self.env.participants['zenbook'], self.env.states['zenbook'], self.pins,
                               probe=lambda body, nonce: stale)
        with self.assertRaises(RuntimeRefusal): bad.install(instruction)
        self.env.clock.tick(100)
        with self.assertRaises(RuntimeRefusal): self.installer().install(instruction)
        self.assertFalse((self.env.states['zenbook']/repoint.FILE).exists())

    def test_slow_probe_cannot_publish_after_lease_expires(self):
        instruction = self.offer()
        def slow(body, nonce):
            result = self.probe(body, nonce); self.env.clock.tick(100); return result
        installer = repoint.Installer(self.env.participants['zenbook'], self.env.states['zenbook'], self.pins, probe=slow)
        with self.assertRaises(RuntimeRefusal): installer.install(instruction)
        self.assertFalse((self.env.states['zenbook']/repoint.FILE).exists())

    def test_unprepared_hub_cannot_attest_endpoint(self):
        self.hub.started = False
        with self.assertRaisesRegex(RuntimeRefusal, 'managed_unbound'):
            repoint.endpoint_receipt(self.hub, 'zenbook', 'nonce')

    def test_successor_uses_existing_role_helpers_and_scoped_grant_only_once(self):
        e = self.env; state = e.states['dell']; instruction = self.offer('dell')
        role = lifecycle.RoleLifecycle(e.participants['dell'], state.parent/'jarvis.config.json', state, self.pins, startup_ready=lambda: True)
        with patch('jarvis.cluster_tunnel.status', return_value={'running': False}), \
                patch('jarvis.cluster_setup.role_autostart', return_value={'autostart': True}) as activate, \
                patch('jarvis.cluster_setup._launch_agent') as launch, patch('jarvis.autostart.system', return_value='windows'):
            self.assertTrue(role.prepare_successor(instruction)['scheduled'])
            activate.assert_called_once_with(state.parent/'jarvis.config.json', state, True, restart=True)
            launch.assert_called_once()
        old = e.participants['dell']
        runtime = MainRuntime(GatewayPort(old.client), 'dell', 'dell-inst', lambda: {'control_ready': True},
                              clock=e.clock.mono, boot=lambda: 'test-boot')
        p = rpc.Participant(old.client, runtime)
        self.assertTrue(lifecycle.accept_successor_grant(p, state, self.pins))
        self.assertFalse(lifecycle.accept_successor_grant(p, state, self.pins))
        self.assertEqual(runtime.lease_snapshot()['holder'], 'dell')

    def test_active_node_lock_or_busy_tunnel_blocks_successor_without_starting_unit(self):
        e = self.env; state = e.states['dell']; instruction = self.offer('dell')
        role = lifecycle.RoleLifecycle(e.participants['dell'], state.parent/'jarvis.config.json', state, self.pins, startup_ready=lambda: True)
        with patch('jarvis.cluster_setup.role_autostart') as activate:
            with exclusive_lock(state/'cluster-node.lock'):
                with self.assertRaises(LockBusy): role.prepare_successor(instruction)
            activate.assert_not_called()
            with patch('jarvis.cluster_tunnel.status', return_value={'running': True}), \
                    patch('jarvis.cluster_tunnel.stop') as stop:
                with self.assertRaisesRegex(RuntimeRefusal, 'tunnel_busy'): role.prepare_successor(instruction)
                stop.assert_called_once()
            activate.assert_not_called()
        self.assertFalse((state/lifecycle.GRANT_FILE).exists())

    def test_missing_successor_startup_binding_never_starts_a_unit(self):
        e = self.env; state = e.states['dell']
        role = lifecycle.RoleLifecycle(e.participants['dell'], state.parent/'jarvis.config.json', state, self.pins)
        with patch('jarvis.cluster_setup.role_autostart') as activate, patch('jarvis.cluster_tunnel.stop') as stop:
            with self.assertRaisesRegex(RuntimeRefusal, 'startup_incomplete'):
                role.prepare_successor(self.offer('dell'))
            activate.assert_not_called(); stop.assert_not_called()
        self.assertFalse((state/lifecycle.GRANT_FILE).exists())

    def test_unsigned_instruction_cannot_trigger_any_system_action(self):
        e = self.env; role = lifecycle.RoleLifecycle(e.participants['dell'], e.root/'config.json', e.states['dell'], self.pins, startup_ready=lambda: True)
        instruction = self.offer('dell'); instruction['signature'] = 'bad'
        with patch('jarvis.cluster_setup.role_autostart') as activate, patch('jarvis.cluster_tunnel.stop') as stop:
            with self.assertRaises(RuntimeRefusal): role.prepare_successor(instruction)
            activate.assert_not_called(); stop.assert_not_called()

    def test_direct_https_switch_stops_old_tunnel_without_dialling_an_ssh_host(self):
        e = self.env; role = lifecycle.RoleLifecycle(e.participants['zenbook'], e.root/'config.json', e.states['zenbook'], self.pins, probe=self.probe, startup_ready=lambda: True)
        with patch('jarvis.cluster_tunnel.status', return_value={'running': False}), \
                patch('jarvis.cluster_tunnel.autostart') as tunnel_startup, \
                patch('jarvis.cluster_tunnel.start') as start, patch('jarvis.cluster_setup.role_autostart') as startup:
            self.assertTrue(role.switch_node(self.offer())['ok'])
            tunnel_startup.assert_called_once_with(e.root/'config.json', e.states['zenbook'], False)
            start.assert_not_called(); startup.assert_called_once()

    def test_signed_link_tamper_refuses_before_tunnel_use(self):
        e = self.env; state = e.states['zenbook']
        link = {'targets': ['david@198.51.100.10'],
                'host_key': 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R',
                'hostname': 'dell.example', 'identity_file': '/tmp/jarvis-return-repoint-not-a-key'}
        instruction = self.issuer.issue('zenbook', self.endpoint, link=link)
        self.assertEqual(instruction['body']['link'], link)
        self.assertTrue(self.installer().install(instruction)['ok'])
        cfg = {'cluster': {'enabled': True, 'local_device': 'zenbook', 'main_device': 'zenbook'},
               'devices': {'zenbook': {}, 'dell': {}}, 'continuity': {'operator_config': 'operator.json'}}
        (state.parent/'operator.json').write_text(json.dumps({'origin': 'http://127.0.0.1:1',
            'authority_id': 'oracle-auth', 'device_id': 'zenbook', 'installation_id': 'zenbook-inst',
            'private_key_path': str(_keys()['zenbook'][1]), 'enrollment': self.pins}))
        original = copy.deepcopy(cfg)
        note_text = (state/'cluster-node.json').read_text()
        from jarvis import linkfinder
        with patch('jarvis.linkfinder.handoff_config', wraps=linkfinder.handoff_config) as handoff, \
                patch('jarvis.cluster_tunnel.link') as tunnel, patch('jarvis.cluster_tunnel.start') as start:
            view = repoint.effective_config(cfg, state)
        self.assertEqual(view['cluster']['main_device'], 'dell')
        self.assertEqual(view['cluster']['main_url'], self.endpoint)
        self.assertEqual(cfg, original)
        handoff.assert_called_once(); tunnel.assert_not_called(); start.assert_not_called()
        self.assertEqual(json.loads(note_text)['link'], link)

        def refuse(mutate):
            body = json.loads(note_text); mutate(body)
            (state/'cluster-node.json').write_text(json.dumps(body))
            with patch('jarvis.linkfinder.handoff_config') as overlay, \
                    patch('jarvis.cluster_tunnel.link') as reader, \
                    patch('jarvis.cluster_tunnel.start') as dial:
                with self.assertRaises(RuntimeRefusal) as caught:
                    repoint.effective_config(cfg, state)
            self.assertEqual(caught.exception.code, 'bad_state')
            overlay.assert_not_called(); reader.assert_not_called(); dial.assert_not_called()
            self.assertEqual(cfg, original)

        refuse(lambda body: body['link']['targets'].__setitem__(0, 'david@203.0.113.50'))
        refuse(lambda body: body['link'].__setitem__('host_key', 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINotTheSignedHostKey'))
        refuse(lambda body: body['link'].__setitem__('hostname', 'evil.example'))
        refuse(lambda body: body['link'].__setitem__('identity_file', '/tmp/not-the-signed-key'))
        refuse(lambda body: body.__setitem__('link', None))
        refuse(lambda body: body.pop('link'))
        (state/'cluster-node.json').write_text(note_text)
        self.assertEqual(repoint.effective_config(cfg, state)['cluster']['main_url'], self.endpoint)


class ExistingUnitHelperTests(unittest.TestCase):
    def test_explicit_restart_is_the_existing_systemd_unit_and_every_call_is_mocked(self):
        from jarvis import autostart
        with tempfile.TemporaryDirectory() as root:
            unit = Path(root)/'jarvis-cluster.service'
            with patch.object(autostart, 'system', return_value='linux'), patch.object(autostart, 'path', return_value=unit), \
                    patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
                autostart.install('jarvis-cluster', 'JARVIS Cluster', ['python', '-m', 'jarvis'], True)
                run.reset_mock()
                autostart.install('jarvis-cluster', 'JARVIS Cluster', ['python', '-m', 'jarvis'], True, restart=True)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(['systemctl', '--user', 'restart', 'jarvis-cluster.service'], commands)
            self.assertTrue(all(cmd[0] == 'systemctl' for cmd in commands))
