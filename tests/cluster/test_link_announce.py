"""Announce/discovery fakes never bind a real socket or contact a computer."""
import copy
import hashlib
import hmac
import ipaddress
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import linkfinder as f
from tests.cluster.test_link_recovery import CONFIG


def fake_sign(data, key):
    return hmac.new(key.encode(), data, hashlib.sha256).hexdigest()


def fake_verify(data, sig, key):
    return isinstance(sig, str) and hmac.compare_digest(sig, fake_sign(data, key))


class AnnounceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = copy.deepcopy(CONFIG)
        self.now = 1000
        self.pair('main', 'main-key')
        self.receiver = f.Receiver(self.config, self.state, clock=lambda: self.now)
        verify = patch('jarvis.deploy.verify', side_effect=fake_verify)
        verify.start(); self.addCleanup(verify.stop)

    def pair(self, main, key):
        (self.state / 'cluster-node.json').write_text(json.dumps({'node_id': 'node', 'main_device': main,
            'token': 'node-token', 'code_key': key}))

    def packet(self, *, main='main', key='main-key', timestamp=1000, nonce='a'*48):
        body = {'main': main, 'fleet': 'jarvis', 'timestamp': timestamp, 'nonce': nonce,
                'addresses': ['172.1.41.171', 'main.tail.ts.net']}
        return json.dumps({'body': body, 'signature': fake_sign(f.packet_bytes(body), key)}).encode()

    def test_valid_announce_learns_and_requests_reconnect_only(self):
        self.assertTrue(self.receiver.receive(self.packet()))
        self.assertTrue((self.state / 'cluster-tunnel-reconnect').exists())
        self.assertEqual(f.learned(self.config, self.state), ['172.1.41.171', 'main.tail.ts.net'])
        self.assertEqual(json.loads((self.state / 'cluster-node.json').read_text())['code_key'], 'main-key')

    def test_bad_signature_wrong_key_stale_oversize_and_flood(self):
        for raw in [self.packet(key='attacker'), self.packet(timestamp=879), self.packet(timestamp=1121), b'x'*4097]:
            self.assertIsNone(self.receiver.receive(raw))
        self.assertFalse((self.state / 'cluster-addresses.json').exists())
        self.now += 1
        for _ in range(4):
            self.assertIsNone(self.receiver.receive(b'{}'))
        self.assertIsNone(self.receiver.receive(self.packet()))
        self.now += 1
        self.assertTrue(self.receiver.receive(self.packet()))

    def test_replay_across_agent_reboot(self):
        raw = self.packet()
        self.assertTrue(self.receiver.receive(raw))
        self.assertIsNone(self.receiver.receive(raw))
        restarted = f.Receiver(self.config, self.state, clock=lambda: self.now+1)
        self.assertIsNone(restarted.receive(raw))

    def test_main_switch_both_directions_rejects_former_main_and_keeps_device_memory(self):
        self.assertTrue(self.receiver.receive(self.packet()))
        self.config['devices']['successor'] = {}
        self.config['cluster']['main_device'] = 'successor'
        self.pair('successor', 'successor-key')
        self.now += 1
        self.assertIsNone(self.receiver.receive(self.packet(nonce='b'*48)))
        self.assertIsNone(self.receiver.receive(self.packet(main='successor', key='main-key', nonce='c'*48)))
        self.assertTrue(self.receiver.receive(self.packet(main='successor', key='successor-key', nonce='d'*48)))
        self.config['cluster']['main_device'] = 'main'
        self.pair('main', 'main-key')
        self.now += 1
        self.assertIsNone(self.receiver.receive(self.packet(main='successor', key='successor-key', nonce='e'*48)))
        self.assertTrue(self.receiver.receive(self.packet(nonce='f'*48)))
        peers = json.loads((self.state / 'cluster-addresses.json').read_text())['peers']
        self.assertEqual(set(peers), {'main', 'successor'})

    def test_node_identity_proof_is_nonce_bound_and_uses_enrolled_credential(self):
        body = {'kind': 'probe', 'node': 'node', 'nonce': 'a'*48, 'fleet': 'jarvis'}
        raw = self.receiver.receive(json.dumps(body).encode())
        digest = hashlib.sha256(b'node-token').hexdigest()
        self.assertEqual(json.loads(raw)['proof'], f.identity_mac(digest, body))
        self.assertNotEqual(f.identity_mac(digest, body), f.identity_mac(digest, {**body, 'nonce':'b'*48}))

    def test_main_scan_verifies_node_proof_and_rate_limits(self):
        config = copy.deepcopy(self.config)
        config['cluster']['local_device'] = 'main'
        digest = hashlib.sha256(b'node-token').hexdigest()
        class Socket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, n): pass
            def connect(self, address): self.ip = address[0]
            def send(self, raw): self.body = json.loads(raw)
            def recv(self, size):
                key = digest if self.ip == '172.1.41.172' else 'wrong-node'
                return json.dumps({'proof': f.identity_mac(key, self.body)}).encode()
        with patch.object(f.socket, 'socket', side_effect=lambda *a: Socket()):
            found = f.find_node(config, self.state, 'node', digest, now=1000,
                                interfaces=[ipaddress.ip_interface('172.1.41.171/29')])
            self.assertEqual(found, ['172.1.41.172'])
            self.assertEqual(f.find_node(config, self.state, 'node', digest, now=1299, interfaces=[]), [])

    def test_signed_continuity_roles_win_over_stale_config(self):
        current = copy.deepcopy(self.config)
        current['devices']['successor'] = {}
        current['cluster']['main_device'] = 'successor'
        self.pair('successor', 'successor-key')
        with patch('jarvis.continuity.repoint.effective_config', return_value=current):
            self.assertIsNone(self.receiver.receive(self.packet()))
            self.assertTrue(self.receiver.receive(self.packet(main='successor', key='successor-key')))

    def test_listener_binds_only_specific_lan_interface(self):
        import threading
        stop = threading.Event()
        bound = []
        class Socket:
            def bind(self, address): bound.append(address)
            def setblocking(self, value): pass
            def close(self): pass
        def select(*args):
            stop.set()
            return [], [], []
        with patch('jarvis.sharedinput.local_networks', return_value=[ipaddress.ip_interface('172.1.41.171/16')]), \
             patch.object(f, 'tailscale_status', return_value={'Self': {'TailscaleIPs': ['100.64.0.3']}}), \
             patch.object(f.socket, 'socket', side_effect=lambda *a: Socket()), \
             patch.object(f.select, 'select', side_effect=select):
            f.listen(self.config, self.state, stop)
        # Own LAN address and own Tailscale address, each bound individually: never a wildcard.
        self.assertEqual(sorted(bound), [('100.64.0.3', 47781), ('172.1.41.171', 47781)])

    def test_disabled_discovery_accepts_no_packet(self):
        self.config['cluster']['link_discovery'] = False
        self.assertIsNone(self.receiver.receive(self.packet()))

    def test_promoted_main_scans_node_that_could_not_make_first_poll(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        config = copy.deepcopy(self.config)
        config['cluster']['local_device'] = 'main'
        broker = Mock()
        broker.nodes.return_value = [{'id': 'node', 'seen': None}]
        broker.db.return_value.__enter__ = Mock(return_value=SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: {'token_hash': 'hash', 'invite_expires': None})))
        broker.db.return_value.__exit__ = Mock(return_value=False)
        hub = SimpleNamespace(config=config, state=self.state, broker=broker)
        with patch.object(f.time, 'time', return_value=1000), patch.object(f, 'find_node') as find:
            f.main_tick(hub)
            find.assert_not_called()
        with patch.object(f.time, 'time', return_value=1061), patch.object(f, 'find_node', return_value=[]) as find:
            f.main_tick(hub)
            find.assert_called_once()
