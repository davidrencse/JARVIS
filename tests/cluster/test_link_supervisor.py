"""Unit tests for the tunnel supervisor pieces: candidate merging, probe order, backoff,
the pure announce accept rule, the listener's bind set and doctor's tunnel verdict. Fakes only."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import cluster_recovery as r, cluster_setup, linkfinder as f
from tests.cluster.test_link_announce import fake_sign, fake_verify
from tests.cluster.test_link_recovery import CONFIG


class CandidateTests(unittest.TestCase):
    def test_merge_values_freshest_first_validated_and_capped(self):
        self.assertEqual(r.merge_values(['172.1.41.171'], ['192.168.0.194', '172.1.41.171']),
                         ['172.1.41.171', '192.168.0.194'])
        self.assertEqual(r.merge_values(['127.0.0.1', 'bad host!', 5], ['10.0.0.2']), ['10.0.0.2'])
        many = ['10.1.0.%d' % n for n in range(1, 60)]
        self.assertEqual(len(r.merge_values(many, many)), r.MAX_ADDRESSES)

    def test_candidates_freshest_then_configured_and_deduplicated(self):
        got = r.merge_candidates(['u@172.1.41.171'], ['172.1.41.172', '172.1.41.171'],
                                 ['u@192.168.0.194', 'u@main.ts.net'], 'u')
        self.assertEqual(got[0], 'u@172.1.41.171')
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(set(got), {'u@172.1.41.171', 'u@172.1.41.172',
                                    'u@192.168.0.194', 'u@main.ts.net'})
        self.assertLessEqual(len(r.merge_candidates([], [], ['u@x%d.example' % n for n in range(99)], 'u')), 32)

    def test_dead_stale_target_never_blocks_a_live_one(self):
        targets = ['u@192.168.0.194', 'u@main.tail.ts.net', 'u@172.1.41.171']
        live = {'u@main.tail.ts.net'}
        self.assertEqual(r.rank_targets(targets, lambda t: t in live)[0], 'u@main.tail.ts.net')
        self.assertCountEqual(r.rank_targets(targets, lambda t: False), targets)  # all still tried

    def test_backoff_doubles_and_caps_at_30_seconds(self):
        delay, seen = 1, []
        for _ in range(10):
            delay = r.next_backoff(delay)
            seen.append(delay)
        self.assertEqual(seen[:4], [2, 4, 8, 16])
        self.assertEqual(max(seen), 30)
        self.assertEqual(r.BACKOFF_CAP, 30)

    def test_parallel_probe_keeps_input_order(self):
        self.assertEqual(r.parallel_probe(lambda t: t != 'b', ['a', 'b', 'c']), [True, False, True])
        self.assertEqual(r.parallel_probe(lambda t: True, []), [])


class AnnounceRuleTests(unittest.TestCase):
    def note(self, main='main', key=None, nonce='1' * 48, ts=1000, fleet='f'):
        body = {'main': main, 'fleet': fleet, 'timestamp': ts, 'nonce': nonce, 'addresses': ['172.1.41.171']}
        return {'body': body, 'signature': fake_sign(f.packet_bytes(body), key or main)}

    def accept(self, note, nonces, now=1000, main='main'):
        return f.accept_announce(note, nonces, now, main, 'f', main, fake_verify)

    def test_accepts_once_then_rejects_replay(self):
        nonces = {}
        self.assertIsNotNone(self.accept(self.note(), nonces))
        self.assertIsNone(self.accept(self.note(), nonces))

    def test_rejects_former_main_bad_key_stale_and_wrong_fleet(self):
        nonces = {}
        self.assertIsNone(self.accept(self.note(main='old'), nonces))
        self.assertIsNone(self.accept(self.note(key='attacker'), nonces))
        self.assertIsNone(self.accept(self.note(ts=800), nonces))
        self.assertIsNone(self.accept(self.note(ts=1300), nonces))
        self.assertIsNone(self.accept(self.note(fleet='other'), nonces))
        self.assertEqual(nonces, {})          # rejected packets leave no replay state

    def test_after_main_switch_old_main_is_rejected_and_new_accepted(self):
        nonces = {}
        old = self.note(main='zen', nonce='2' * 48)
        new = self.note(main='dell', nonce='3' * 48)
        self.assertIsNotNone(self.accept(old, nonces, main='zen'))
        self.assertIsNone(self.accept(self.note(main='zen', nonce='4' * 48), nonces, main='dell'))
        self.assertIsNotNone(self.accept(new, nonces, main='dell'))

    def test_nonce_cache_forgets_only_expired_entries(self):
        nonces = {}
        self.accept(self.note(nonce='5' * 48), nonces)
        self.assertIsNotNone(self.accept(self.note(nonce='6' * 48, ts=1500), nonces, now=1500))
        self.assertNotIn('main:' + '5' * 48, nonces)


class ListenerBindTests(unittest.TestCase):
    def test_binds_lan_and_own_tailscale_only(self):
        net = [SimpleNamespace(ip=__import__('ipaddress').ip_address('172.1.41.171'), version=4)]
        status = {'Self': {'TailscaleIPs': ['100.64.0.3', 'fd7a:115c:a1e0::3', '127.0.0.1', 'junk']}}
        with patch('jarvis.sharedinput.local_networks', return_value=net), \
                patch.object(f, 'tailscale_status', return_value=status):
            self.assertEqual(f.listen_ips(), {'172.1.41.171', '100.64.0.3'})
        with patch('jarvis.sharedinput.local_networks', return_value=net), \
                patch.object(f, 'tailscale_status', return_value=None):
            self.assertEqual(f.listen_ips(), {'172.1.41.171'})


class DoctorTunnelTests(unittest.TestCase):
    def run_doctor(self, hub_healthy, fix):
        state = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, state, True)
        checks = []
        ssh_ok = SimpleNamespace(returncode=0, stdout='jarvis-node-link', stderr='')
        with patch.object(cluster_setup, 'Client'), \
                patch('jarvis.cluster_tunnel.peer', return_value=CONFIG['devices']['main']), \
                patch('jarvis.cluster_tunnel.candidates', return_value=['u@172.1.41.171']), \
                patch('jarvis.cluster_tunnel.reachable', return_value=True), \
                patch('jarvis.cluster_tunnel.pinned_host', return_value=[]), \
                patch('jarvis.cluster_tunnel.status', return_value={'hub_healthy': hub_healthy,
                                                                  'ssh_alive': True}), \
                patch('jarvis.cluster_recovery.tailscale_status',
                      return_value={'BackendState': 'NeedsLogin'}), \
                patch('jarvis.cluster_recovery.ensure_tunnel', return_value=True) as tun, \
                patch('jarvis.cluster_recovery.ensure_dependents') as dep, \
                patch.object(cluster_setup.shutil, 'which', return_value='/usr/bin/ssh'), \
                patch.object(cluster_setup.subprocess, 'run', return_value=ssh_ok):
            cluster_setup._doctor_node(CONFIG, state, 'node', 'main', True, checks, 'code',
                                       fix=fix, config_path=state / 'c.json')
        return {c['check']: c for c in checks}, tun, dep

    def test_reachable_but_tunnel_down_names_the_supervisor_and_offers_fix(self):
        checks, tun, dep = self.run_doctor(False, fix=False)
        verdict = checks['tunnel supervisor']
        self.assertFalse(verdict['ok'])
        self.assertIn('reachable but tunnel down: the supervisor fixes it within', verdict['detail'])
        self.assertIn('--fix', verdict['fix'])
        tun.assert_not_called()
        self.assertFalse(checks['Tailscale connected']['ok'])
        self.assertIn('tailscale installed but not connected', checks['Tailscale connected']['detail'])

    def test_fix_starts_the_worker_and_reensures_dependents(self):
        checks, tun, dep = self.run_doctor(False, fix=True)
        tun.assert_called_once()
        dep.assert_called_once()
        self.assertIn('--fix started', checks['tunnel supervisor']['fix'])

    def test_healthy_tunnel_adds_no_supervisor_failure(self):
        checks, tun, dep = self.run_doctor(True, fix=True)
        self.assertNotIn('tunnel supervisor', checks)
        tun.assert_not_called()


if __name__ == '__main__':
    unittest.main()
