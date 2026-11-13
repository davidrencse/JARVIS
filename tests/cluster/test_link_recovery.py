"""Recovery regression tests: fake sockets/processes only, temporary state, no peer access."""
import ipaddress
import json
import random
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from jarvis import cluster_recovery as r, cluster_discovery as d, cluster_tunnel as t

KEY = 'ssh-ed25519 AAAA'
CONFIG = {'cluster': {'local_device': 'node', 'main_device': 'main', 'tunnel': 'forward'},
          'devices': {'node': {}, 'main': {'transport': 'ssh', 'target': 'u@old', 'host_key': KEY}}}


class RecoveryTests(unittest.TestCase):
    def test_addresses_authenticated_nonce_bound_validated_and_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = SimpleNamespace(state=Path(tmp), token='secret', main='main', local='node')
            values = ['172.1.41.171', '100.64.0.11', 'archmain.tail.ts.net']
            response = {'addresses': {'values': values, 'signature': r.signature('secret', 'main', 'node', 'nonce', values)}}
            self.assertFalse(r.learn(client, response, 'replay'))
            self.assertFalse((Path(tmp) / 'cluster-addresses.json').exists())
            self.assertTrue(r.learn(client, response, 'nonce'))
            self.assertEqual(t.candidates(CONFIG, tmp)[:3], ['u@' + a for a in values])
            self.assertEqual(r.learned({'cluster': {'local_device': 'node', 'main_device': 'other'}, 'devices': {'node': {}, 'other': {}}}, tmp), [])
            for invalid in ['--option', '127.0.0.1', 'localhost', '999.5.1.1', 'host;cmd', 'bad..host', '0.0.0.0']:
                self.assertFalse(r.valid_address(invalid), invalid)
            self.assertEqual(len(r.clean(['h' + str(i) for i in range(100)])), 16)
            self.assertFalse((Path(tmp) / 'jarvis.config.json').exists())

    def test_health_rotates_live_but_dead_forward_and_resets(self):
        health = r.SessionHealth()
        for at in [0, 5, 9]:
            self.assertFalse(health.observe(at, False))
        self.assertTrue(health.observe(10, False))
        self.assertFalse(health.observe(11, True))
        self.assertFalse(health.observe(99, False))
        self.assertEqual(r.rank_targets(['dead', 'live'], lambda x: x == 'live'), ['live', 'dead'])

    def test_every_address_uses_same_pinned_key(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(t.shutil, 'which', return_value='ssh'):
            for target in ['u@172.1.41.171', 'u@100.64.0.11', 'u@main.ts.net']:
                args = t.command(CONFIG, tmp, target)
                self.assertIn('StrictHostKeyChecking=yes', args)
                self.assertIn('HostKeyAlias=jarvis-peer', args)
                self.assertEqual((Path(tmp) / 'known-host-jarvis-peer').read_text(), 'jarvis-peer ' + KEY + '\n')

    def test_supervisor_installs_and_restarts_but_keeps_live_worker(self):
        with patch.object(t, 'startup_path', return_value=SimpleNamespace(is_file=lambda: False)), \
             patch.object(t, 'autostart') as startup, patch.object(t, 'start') as start, \
             patch.object(t, 'status', return_value={'running': False}):
            r.ensure_tunnel(CONFIG, 'fake-state', 'fake-config')
            startup.assert_called_once_with('fake-config', 'fake-state', True)
            start.assert_called_once()
        with patch.object(t, 'startup_path', return_value=SimpleNamespace(is_file=lambda: True)), \
             patch.object(t, 'start') as start, patch.object(t, 'status', return_value={'running': True}):
            r.ensure_tunnel(CONFIG, 'fake-state', 'fake-config')
            start.assert_not_called()

    def test_dependents_respect_manual_stop_and_opt_out(self):
        with patch('jarvis.clipsync._pid', return_value=None), \
             patch('jarvis.clipsync.stopped_by_hand', return_value=False), \
             patch('jarvis.clipsync.start') as clip, patch('jarvis.sharedinput.guard_for') as guard:
            r.ensure_dependents({}, 'fake-state')
            clip.assert_called_once()
            guard.assert_called_once()
            clip.reset_mock(); guard.reset_mock()
            r.ensure_dependents({'clipboard': {'autostart': False}, 'lan_mouse': {'off_when_apart': False}}, 'fake-state')
            clip.assert_not_called(); guard.assert_not_called()
        with patch('jarvis.clipsync._pid', return_value=None), \
             patch('jarvis.clipsync.stopped_by_hand', return_value=True), \
             patch('jarvis.clipsync.start') as clip, patch('jarvis.sharedinput.guard_for'):
            r.ensure_dependents({}, 'fake-state')
            clip.assert_not_called()

    def test_alert_one_per_outage_only_after_online(self):
        alert, notify = r.OfflineAlerts(), Mock()
        alert.check([{'id': 'node', 'online': False}], 1000, notify)
        notify.assert_not_called()
        alert.check([{'id': 'node', 'online': True}], 1000, notify)
        alert.check([{'id': 'node', 'online': False}], 1180, notify)
        notify.assert_not_called()
        for now in [1181, 2000, 3000]:
            alert.check([{'id': 'node', 'online': False}], now, notify)
        notify.assert_called_once()
        alert.check([{'id': 'node', 'online': True}], 4000, notify)
        alert.check([{'id': 'node', 'online': False}], 4181, notify)
        self.assertEqual(notify.call_count, 2)

    def test_tailscale_disconnected_and_magicdns(self):
        with patch.object(r.shutil, 'which', return_value='fake-tailscale'), \
             patch.object(r.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"BackendState":"Stopped"}')):
            self.assertEqual(r.tailscale_status()['BackendState'], 'Stopped')
        with patch.object(r, 'tailscale_status', return_value={'Self': {'DNSName': 'main.tail.ts.net.', 'HostName': 'main'}}):
            self.assertEqual(r.magic_names(), ['main.tail.ts.net', 'main'])

    def test_discovery_new_ip_wrong_key_rate_limit_and_subnet_cap(self):
        interfaces = [ipaddress.ip_interface('172.1.41.171/16')]
        self.assertEqual(str(d.scan_networks(interfaces)[0]), '172.1.44.0/22')
        self.assertEqual(d.scan_networks([ipaddress.ip_interface('127.0.0.1/8')]), [])
        machine = CONFIG['devices']['main']
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(t, 'reachable', side_effect=lambda target, **kw: target in ['u@172.1.41.172', 'u@172.1.41.173']), \
             patch.object(d, 'verify', side_effect=lambda target, *args, **kwargs: target.endswith('.172')) as verify:
            self.assertEqual(d.discover(machine, tmp, now=1000, interfaces=interfaces), ['u@172.1.41.172'])
            self.assertEqual(d.discovered(machine, tmp), ['u@172.1.41.172'])
            self.assertEqual(d.discovered({**machine, 'host_key': 'different'}, tmp), [])
            self.assertEqual(d.discover(machine, tmp, now=1299, interfaces=interfaces), [])
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(d.discover(machine, tmp, now=1300, interfaces=interfaces), ['u@172.1.41.172'])

    def test_scan_verifier_requires_pin_and_strict_ssh(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(d.subprocess, 'run') as run, \
             patch('shutil.which', return_value='fake-ssh'):
            run.return_value = SimpleNamespace(returncode=255, stdout='')
            self.assertFalse(d.verify('u@172.1.41.172', CONFIG['devices']['main'], tmp))
            self.assertIn('StrictHostKeyChecking=yes', run.call_args.args[0])
            self.assertTrue(any(x.startswith('UserKnownHostsFile=') for x in run.call_args.args[0]))
            run.return_value = SimpleNamespace(returncode=0, stdout='jarvis-node-link')
            self.assertTrue(d.verify('u@172.1.41.172', CONFIG['devices']['main'], tmp))
            self.assertFalse(d.verify('u@172.1.41.172', {'target': 'u@old'}, tmp))

    def test_forward_health_requires_authenticated_nonce_reply(self):
        def request(path, body, **kwargs):
            self.assertEqual(path, '/v1/link-health')
            return {'signature': r.signature('secret', 'main', 'node', body['nonce'], [])}
        client = SimpleNamespace(token='secret', main='main', local='node', request=request)
        with patch('jarvis.cluster.Client', return_value=client):
            self.assertTrue(t.hub_healthy(CONFIG, 'fake-state'))
            client.request = lambda *a, **k: {'signature': 'untrusted-local-port'}
            self.assertFalse(t.hub_healthy(CONFIG, 'fake-state'))

    def test_production_forward_loop_rotates_live_ssh_when_hub_dies(self):
        config = json.loads(json.dumps(CONFIG))
        config['devices']['main']['fallback_targets'] = ['u@live']
        clock, attempts, current = [0.0], [], [None]
        class Process:
            returncode = 0
            def __init__(self): self.dead = False
            def poll(self): return 0 if self.dead else None
            def terminate(self): self.dead = True
            def kill(self): self.dead = True
            def wait(self, **kwargs): return 0
        with tempfile.TemporaryDirectory() as tmp:
            def popen(argv, **kwargs):
                current[0] = argv[-1]
                attempts.append(current[0])
                return Process()
            def sleep(seconds):
                clock[0] += seconds
                if clock[0] > 25:
                    (Path(tmp)/'cluster-tunnel.stop').touch()
            with patch.object(t.shutil, 'which', return_value='fake-ssh'), \
                 patch.object(t, 'reachable', return_value=True), \
                 patch.object(t, 'hub_healthy', side_effect=lambda *a: current[0] == 'u@live'), \
                 patch.object(t.subprocess, 'Popen', side_effect=popen), \
                 patch.object(t.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(t.time, 'sleep', side_effect=sleep):
                t.run(config, tmp)
        self.assertEqual(attempts, ['u@old', 'u@live'])

    def test_missing_discovery_credentials_and_failed_optional_save_do_not_abort_poll(self):
        self.assertFalse(r.learn(SimpleNamespace(), {'job': {}}, 'nonce'))
        client = SimpleNamespace(token='secret', main='main', local='node', state=Path('fake-state'))
        values = ['172.1.41.171']
        response = {'addresses': {'values': values, 'signature': r.signature('secret', 'main', 'node', 'nonce', values)}}
        with patch.object(r, 'remember', side_effect=PermissionError('fake read-only state')):
            self.assertFalse(r.learn(client, response, 'nonce'))

    def test_running_tunnel_reloads_current_main_and_pin_before_next_connect(self):
        from jarvis import linkfinder
        original = json.loads(json.dumps(CONFIG))
        original['devices']['successor'] = {'transport': 'ssh', 'target': 'u@new', 'host_key': 'ssh-ed25519 BBBB', 'hostname': 'successor'}
        next_role = json.loads(json.dumps(original))
        next_role['cluster']['main_device'] = 'successor'
        clock, attempts = [0.0], []
        class Process:
            returncode = 0
            def __init__(self): self.dead = False
            def poll(self): return 0 if self.dead else None
            def terminate(self): self.dead = True
            def kill(self): self.dead = True
            def wait(self, **kwargs): return 0
        with tempfile.TemporaryDirectory() as tmp:
            def role(*args):
                return (original, 'node', 'main') if clock[0] < 1 else (next_role, 'node', 'successor')
            def popen(argv, **kwargs):
                target = argv[-1]
                attempts.append(target)
                alias = 'peer' if target == 'u@old' else 'successor'
                key = KEY if target == 'u@old' else 'ssh-ed25519 BBBB'
                self.assertEqual((Path(tmp)/('known-host-jarvis-'+alias)).read_text(), 'jarvis-'+alias+' '+key+'\n')
                return Process()
            def sleep(seconds):
                clock[0] += seconds
                if clock[0] > 16:
                    (Path(tmp)/'cluster-tunnel.stop').touch()
            with patch.object(linkfinder, 'role', side_effect=role), \
                 patch.object(t.shutil, 'which', return_value='fake-ssh'), \
                 patch.object(t, 'reachable', return_value=True), \
                 patch.object(t, 'hub_healthy', return_value=True), \
                 patch.object(t.subprocess, 'Popen', side_effect=popen), \
                 patch.object(t.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(t.time, 'sleep', side_effect=sleep):
                t.run(original, tmp)
        self.assertEqual(attempts, ['u@old', 'u@new'])
