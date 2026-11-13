"""Reconnect blockers: operator tunnel stop, and the 45s scan budget.

Fakes, temp state, and injected clocks only. No LAN probe, no SSH binary, no
login startup, no live config.
"""
import ipaddress
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import cluster_discovery as d, cluster_recovery as r, cluster_tunnel as t, linkfinder
from jarvis.cluster import NodeWorker
from tests.cluster.test_link_recovery import CONFIG, KEY


def _clock(now=0.0):
    box = {'now': now, 'lock': threading.Lock()}

    def clock():
        with box['lock']:
            return box['now']

    def advance(seconds):
        with box['lock']:
            box['now'] += seconds

    clock.advance = advance
    return clock


class ManualStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config_path = self.state / 'jarvis.config.json'
        self.config_path.write_text(json.dumps(CONFIG), encoding='utf-8')
        self.before = self.config_path.read_bytes()

    def _stop(self, config=None, running=False):
        flags = {'running': running}

        def status(_state):
            return {'running': flags['running'], 'hub_healthy': False, 'ssh_alive': False}

        def pause(_seconds):
            flags['running'] = False

        with patch.object(t, 'status', side_effect=status):
            result = t.stop(self.state, operator=True, config=config or CONFIG, sleep=pause)
        return result

    def _guard(self, config, ticks=3):
        agent = NodeWorker.__new__(NodeWorker)
        agent.config, agent.state, agent.config_path = config, self.state, self.config_path

        class Stop:
            def __init__(self):
                self.n = 0

            def is_set(self):
                return self.n >= ticks

            def wait(self, timeout=None):
                self.n += 1
                return self.is_set()

        agent.stop_event = Stop()
        seen, started, autos = [], [], []
        real = r.ensure_tunnel

        def wrapped(*args, **kwargs):
            value = real(*args, **kwargs)
            seen.append(value)
            return value

        startup = SimpleNamespace(is_file=lambda: True)
        with patch.object(linkfinder, 'ensure_tunnel', side_effect=wrapped), \
                patch.object(t, 'status', return_value={'running': False, 'hub_healthy': False}), \
                patch.object(t, 'startup_path', return_value=startup), \
                patch.object(t, 'autostart', side_effect=lambda *a, **k: autos.append(a)), \
                patch.object(t, 'start', side_effect=lambda *a, **k: started.append(a) or {'started': True}):
            agent._tunnel_guard()
        return seen, started, autos

    def test_operator_stop_survives_repeated_guard_ticks(self):
        stopped = self._stop()
        self.assertFalse(stopped['stopped'])
        self.assertTrue(stopped['manual_stop'])
        self.assertTrue(t.manual_stop_path(self.state).is_file())
        seen, started, autos = self._guard(CONFIG)
        self.assertGreaterEqual(len(seen), 3)
        self.assertEqual(seen, [False, False, False])
        self.assertEqual(started, [])
        self.assertEqual(autos, [])
        self.assertTrue(t.manual_stop_holds(CONFIG, self.state))
        self.assertEqual(self.config_path.read_bytes(), self.before)

    def test_explicit_start_clears_and_the_next_guard_can_start(self):
        self._stop()
        with patch.object(t, 'status', return_value={'running': True, 'hub_healthy': False}), \
                patch.object(t.subprocess, 'Popen', side_effect=AssertionError('launched')):
            started = t.start(self.config_path, self.state)
        self.assertFalse(started['started'])
        self.assertFalse(t.manual_stop_path(self.state).is_file())
        _seen, started_calls, autos = self._guard(CONFIG)
        self.assertEqual(len(started_calls), 3)
        self.assertEqual(autos, [])
        self.assertEqual(self.config_path.read_bytes(), self.before)

    def test_stop_while_running_keeps_the_marker_and_blocks_run(self):
        stopped = self._stop(running=True)
        self.assertTrue(stopped['stopped'])
        self.assertTrue((self.state / 'cluster-tunnel.stop').is_file())
        self.assertTrue(t.manual_stop_holds(CONFIG, self.state))
        with patch.object(t.subprocess, 'Popen', side_effect=AssertionError('ssh')):
            self.assertIsNone(t.run(CONFIG, self.state))
        self.assertTrue(t.manual_stop_path(self.state).is_file())
        self.assertTrue((self.state / 'cluster-tunnel.stop').is_file())
        self.assertEqual(self.config_path.read_bytes(), self.before)

    def test_lifecycle_stop_does_not_block_recovery(self):
        flags = {'running': True}

        def status(_state):
            return {'running': flags['running']}

        with patch.object(t, 'status', side_effect=status):
            result = t.stop(self.state, sleep=lambda _seconds: flags.update(running=False))
        self.assertTrue(result['stopped'])
        self.assertFalse(result['manual_stop'])
        self.assertFalse(t.manual_stop_path(self.state).exists())
        self.assertTrue((self.state / 'cluster-tunnel.stop').is_file())
        _seen, started, _autos = self._guard(CONFIG)
        self.assertEqual(len(started), 3)

    def test_role_change_clears_the_stop_without_rewriting_config(self):
        self._stop()
        promoted = json.loads(json.dumps(CONFIG))
        promoted['cluster']['local_device'] = 'main'
        promoted['cluster']['main_device'] = 'main'
        seen, started, _autos = self._guard(promoted)
        self.assertEqual(started, [])
        self.assertEqual(seen, [False, False, False])
        self.assertFalse(t.manual_stop_path(self.state).exists())
        self.assertEqual(self.config_path.read_bytes(), self.before)
        _seen, started, _autos = self._guard(CONFIG)
        self.assertEqual(len(started), 3)
        self.assertEqual(self.config_path.read_bytes(), self.before)

    def test_other_main_and_direct_link_are_not_a_disabled_tunnel(self):
        self._stop()
        successor = json.loads(json.dumps(CONFIG))
        successor['devices']['successor'] = {'transport': 'ssh', 'target': 'u@new', 'host_key': 'ssh-ed25519 BBBB'}
        successor['cluster']['main_device'] = 'successor'
        _seen, started, _autos = self._guard(successor)
        self.assertEqual(len(started), 3)
        self.assertFalse(t.manual_stop_path(self.state).exists())
        self.assertEqual(self.config_path.read_bytes(), self.before)
        self._stop()
        (self.state / 'continuity-endpoint.json').write_text(
            json.dumps({'body': {'link': None}}), encoding='utf-8')

        def same_role(config, state):
            return config, 'node', 'main'

        with patch.object(linkfinder, 'role', side_effect=same_role):
            self.assertEqual(t.link_disposition(self.state), 'off')
            self.assertFalse(t.manual_stop_holds(CONFIG, self.state))
            self.assertFalse(r.ensure_tunnel(CONFIG, self.state, self.config_path))
        self.assertFalse(t.manual_stop_path(self.state).exists())
        (self.state / 'continuity-endpoint.json').unlink()
        _seen, started, _autos = self._guard(CONFIG)
        self.assertEqual(len(started), 3)
        self.assertEqual(self.config_path.read_bytes(), self.before)

    def test_later_boot_clears_and_unknown_boot_holds(self):
        with patch.object(t, 'current_boot', return_value=1000.0):
            self._stop()
        with patch.object(t, 'current_boot', return_value=1000.0 + t.BOOT_JITTER):
            self.assertTrue(t.manual_stop_holds(CONFIG, self.state))
        with patch.object(t, 'current_boot', return_value=1000.0 + t.BOOT_JITTER + 1):
            _seen, started, _autos = self._guard(CONFIG)
        self.assertEqual(len(started), 3)
        self.assertFalse(t.manual_stop_path(self.state).exists())
        with patch.object(t, 'current_boot', return_value=None):
            self._stop()
            self.assertTrue(t.manual_stop_holds(CONFIG, self.state))
            seen, started, _autos = self._guard(CONFIG)
        self.assertEqual(started, [])
        self.assertEqual(seen, [False, False, False])

    def test_cli_stop_is_the_operator_stop_and_start_clears_it(self):
        from jarvis import cluster_cli
        config = json.loads(json.dumps(CONFIG))
        config['cluster']['enabled'] = True
        self.config_path.write_text(json.dumps(config), encoding='utf-8')
        before = self.config_path.read_bytes()
        args = SimpleNamespace(action='tunnel', value='stop', config=str(self.config_path),
                               state=str(self.state), autostart=False)
        with patch.object(t, 'status', return_value={'running': False, 'hub_healthy': False}):
            stopped = cluster_cli.run(args, config, self.config_path.parent)
        self.assertTrue(stopped['manual_stop'])
        self.assertTrue(t.manual_stop_holds(config, self.state))
        self.assertEqual(self.config_path.read_bytes(), before)
        args.value = 'start'
        with patch.object(t, 'status', return_value={'running': True, 'hub_healthy': False}), \
                patch.object(t.subprocess, 'Popen', side_effect=AssertionError('launched')):
            cluster_cli.run(args, config, self.config_path.parent)
        self.assertFalse(t.manual_stop_path(self.state).exists())
        self.assertEqual(self.config_path.read_bytes(), before)


class DoctorStopTests(unittest.TestCase):
    def test_passive_and_fix_do_not_restart_an_operator_stop(self):
        from jarvis import cluster_setup
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            config_path = state / 'jarvis.config.json'
            config_path.write_text(json.dumps(CONFIG), encoding='utf-8')
            before = config_path.read_bytes()
            with patch.object(t, 'status', return_value={'running': False}), \
                    patch.object(t, 'current_boot', return_value=50.0):
                t.stop(state, operator=True, config=CONFIG, sleep=lambda _s: None)
            started, autos, fixes = [], [], []
            startup = SimpleNamespace(is_file=lambda: True)
            ssh_ok = SimpleNamespace(returncode=0, stdout='jarvis-node-link', stderr='')
            with patch.object(cluster_setup, 'Client'), \
                    patch('jarvis.cluster_tunnel.peer', return_value=CONFIG['devices']['main']), \
                    patch('jarvis.cluster_tunnel.candidates', return_value=['u@172.1.41.171']), \
                    patch('jarvis.cluster_tunnel.reachable', return_value=True), \
                    patch('jarvis.cluster_tunnel.pinned_host', return_value=[]), \
                    patch('jarvis.cluster_tunnel.status', return_value={'hub_healthy': False, 'ssh_alive': False, 'running': False}), \
                    patch('jarvis.cluster_tunnel.startup_path', return_value=startup), \
                    patch('jarvis.cluster_tunnel.autostart', side_effect=lambda *a, **k: autos.append(a)), \
                    patch('jarvis.cluster_tunnel.start', side_effect=lambda *a, **k: started.append(a)), \
                    patch('jarvis.cluster_recovery.tailscale_status', return_value={'BackendState': 'Running'}), \
                    patch('jarvis.cluster_recovery.ensure_dependents', side_effect=lambda *a, **k: fixes.append(a)), \
                    patch.object(t, 'current_boot', return_value=50.0), \
                    patch.object(cluster_setup.shutil, 'which', return_value='/usr/bin/ssh'), \
                    patch.object(cluster_setup.subprocess, 'run', return_value=ssh_ok):
                checks = []
                cluster_setup._doctor_node(CONFIG, state, 'node', 'main', True, checks, 'code',
                                           fix=False, config_path=config_path)
                self.assertEqual(started, [])
                self.assertEqual(fixes, [])
                cluster_setup._doctor_node(CONFIG, state, 'node', 'main', True, [], 'code',
                                           fix=True, config_path=config_path)
                self.assertEqual(started, [])
                self.assertEqual(autos, [])
                self.assertEqual(len(fixes), 1)
                self.assertTrue(t.manual_stop_holds(CONFIG, state))
                self.assertEqual(config_path.read_bytes(), before)
                self.assertFalse(checks[[c['check'] for c in checks].index('tunnel supervisor')]['ok'])


class ScanBudgetTests(unittest.TestCase):
    def test_constants_match_the_handbook(self):
        self.assertEqual(d.SCAN_BUDGET, 45)
        self.assertEqual(d.WORKERS, 32)
        self.assertEqual(d.PROBE_TIMEOUT, 0.15)
        self.assertEqual(d.VERIFY_TIMEOUT, 3)
        nets = d.scan_networks([ipaddress.ip_interface('10.0.0.1/16')] * 9)
        self.assertEqual(len(nets), 1)
        self.assertEqual(nets[0].prefixlen, 22)

    def test_pin_checks_share_one_45s_budget_and_unpinned_hosts_are_not_saved(self):
        clock = _clock()
        records = []
        machine = CONFIG['devices']['main']

        def check(target, _machine, _state, timeout=3):
            start = clock()
            records.append((start, timeout))
            clock.advance(timeout)
            return False

        interfaces = [ipaddress.ip_interface('172.1.41.171/16')]
        with tempfile.TemporaryDirectory() as tmp:
            found = d.discover(machine, tmp, now=1000, interfaces=interfaces, clock=clock,
                               workers=1, reach=lambda *_a, **_k: True, check=check)
            self.assertEqual(found, [])
            self.assertEqual(list(Path(tmp).glob('cluster-discovered-*.json')), [])
        self.assertEqual(len(records), 15)
        self.assertTrue(all(start < 45 and timeout <= 3 and start + timeout <= 45 for start, timeout in records))

    def test_a_late_or_unpinned_result_is_not_persisted(self):
        machine = CONFIG['devices']['main']
        clock = _clock()

        def check(target, _machine, _state, timeout=3):
            clock.advance(100)
            return True

        interfaces = [ipaddress.ip_interface('172.1.41.169/30')]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(d.discover(machine, tmp, now=1000, interfaces=interfaces, clock=clock,
                                        workers=1, reach=lambda *_a, **_k: True, check=check), [])
            self.assertEqual(list(Path(tmp).glob('cluster-discovered-*.json')), [])
            clock = _clock()

            def pinned(target, _machine, _state, timeout=3):
                return target.endswith('.170')

            found = d.discover(machine, tmp, now=2000, interfaces=interfaces, clock=clock,
                               reach=lambda *_a, **_k: True, check=pinned)
            self.assertEqual(found, ['u@172.1.41.170'])
            self.assertEqual(d.discovered(machine, tmp), ['u@172.1.41.170'])
            self.assertEqual(d.discovered({**machine, 'host_key': 'different'}, tmp), [])
            saved = json.loads(d.discovery_path(machine, tmp).read_text(encoding='utf-8'))
            self.assertEqual(saved['values'], ['172.1.41.170'])
            self.assertEqual(saved['host_key'], KEY)

    def test_stuck_pin_checks_do_not_hold_the_scan_past_the_budget(self):
        release = threading.Event()
        timeouts, inflight, peak = [], {'n': 0}, {'n': 0}
        lock = threading.Lock()
        machine = CONFIG['devices']['main']

        def check(_target, _machine, _state, timeout=3):
            timeouts.append(timeout)
            with lock:
                inflight['n'] += 1
                peak['n'] = max(peak['n'], inflight['n'])
            try:
                release.wait(3)
                return True
            finally:
                with lock:
                    inflight['n'] -= 1

        interfaces = [ipaddress.ip_interface('172.1.41.171/26')]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                started = time.monotonic()
                found = d.discover(machine, tmp, now=1000, interfaces=interfaces, budget=0.4,
                                   reach=lambda *_a, **_k: True, check=check)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 1.5)
                self.assertEqual(found, [])
                self.assertEqual(list(Path(tmp).glob('cluster-discovered-*.json')), [])
        finally:
            release.set()
        self.assertTrue(timeouts)
        self.assertLessEqual(len(timeouts), d.WORKERS)
        self.assertLessEqual(peak['n'], d.WORKERS)
        self.assertTrue(all(timeout <= 0.4 + 1e-6 for timeout in timeouts))

    def test_verify_timeout_is_the_ssh_timeout_and_zero_does_not_run(self):
        machine = CONFIG['devices']['main']
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(d.subprocess, 'run') as run, \
                patch('shutil.which', return_value='fake-ssh'):
            self.assertFalse(d.verify('u@172.1.41.172', machine, tmp, timeout=0))
            run.assert_not_called()
            run.return_value = SimpleNamespace(returncode=0, stdout='jarvis-node-link')
            self.assertTrue(d.verify('u@172.1.41.172', machine, tmp, timeout=0.25))
            self.assertEqual(run.call_args.kwargs['timeout'], 0.25)
            self.assertIn('StrictHostKeyChecking=yes', run.call_args.args[0])
            run.return_value = SimpleNamespace(returncode=0, stdout='welcome')
            self.assertFalse(d.verify('u@172.1.41.172', machine, tmp, timeout=0.25))

    def test_main_scan_fits_the_same_budget_and_does_not_wait_on_stuck_probes(self):
        clock = _clock()
        records = []

        class Socket:
            def __init__(self):
                self.timeout = None
                self.body = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, value):
                self.timeout = value

            def connect(self, address):
                self.ip = address[0]

            def send(self, raw):
                self.body = json.loads(raw)

            def recv(self, _size):
                start = clock()
                records.append((start, self.timeout))
                clock.advance(self.timeout)
                if self.ip == '172.1.41.250':
                    return json.dumps({'proof': linkfinder.identity_mac(digest, self.body)}).encode()
                return b'no'

        config = {'cluster': {'local_device': 'main', 'main_device': 'main', 'tunnel': 'forward',
                              'link_discovery': True},
                  'devices': {'main': {}, 'node': {}}}
        digest = 'ab' * 32
        interfaces = [ipaddress.ip_interface('172.1.44.1/22')]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(linkfinder.socket, 'socket', side_effect=lambda *a, **k: Socket()):
            found = linkfinder.find_node(config, tmp, 'node', digest, now=1000, interfaces=interfaces,
                                         clock=clock, workers=1)
        self.assertEqual(found, [])
        self.assertGreater(len(records), 200)
        self.assertLess(len(records), 400)
        self.assertTrue(all(start < 45 and start + timeout <= 45 + 1e-9 for start, timeout in records))

        # A valid HMAC that finishes after the deadline is not an address. The same
        # proof, finished inside a later budget, is.
        clock = _clock()
        proof_records = []

        class Proof(Socket):
            late = True

            def recv(self, _size):
                proof_records.append((clock(), self.timeout))
                clock.advance(100 if self.late else self.timeout)
                return json.dumps({'proof': linkfinder.identity_mac(digest, self.body)}).encode()

        small = [ipaddress.ip_interface('172.1.41.169/30')]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(linkfinder.socket, 'socket', side_effect=lambda *a, **k: Proof()):
            late = linkfinder.find_node(config, tmp, 'node', digest, now=3000, interfaces=small,
                                        clock=clock, workers=1)
            self.assertEqual(late, [])
            self.assertEqual(len(proof_records), 1)
            self.assertGreater(proof_records[0][0] + 100, 45)
            Proof.late = False
            clock = _clock()
            on_time = linkfinder.find_node(config, tmp, 'node', digest, now=3300, interfaces=small,
                                           clock=clock, workers=1)
        self.assertEqual(on_time, ['172.1.41.169', '172.1.41.170'])

        release = threading.Event()
        timeouts = []

        class Stuck(Socket):
            def recv(self, _size):
                release.wait(3)
                return b'no'

            def settimeout(self, value):
                timeouts.append(value)

        try:
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(linkfinder.socket, 'socket', side_effect=lambda *a, **k: Stuck()):
                started = time.monotonic()
                found = linkfinder.find_node(config, tmp, 'node', digest, now=1000,
                                             interfaces=[ipaddress.ip_interface('172.1.41.171/26')],
                                             budget=0.4)
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.5)
            self.assertEqual(found, [])
        finally:
            release.set()
        self.assertTrue(timeouts)
        self.assertLessEqual(len(timeouts), d.WORKERS)
        self.assertTrue(all(timeout <= 0.15 + 1e-9 for timeout in timeouts))


if __name__ == '__main__':
    unittest.main()
