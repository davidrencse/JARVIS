"""Failback from a worse tunnel target. Fake clock and probes, no sockets."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import cluster_recovery, cluster_tunnel as tunnel

KEY = 'ssh-ed25519 AAAA'
LAN = 'u@192.0.2.1'
VPN = 'u@192.0.2.2'
LEARNED_IP = '192.0.2.10'
LEARNED = 'u@' + LEARNED_IP


def tunnel_config(mode):
    peer = {'hostname': 'peer', 'transport': 'ssh', 'target': LAN,
            'fallback_targets': [VPN], 'host_key': KEY}
    if mode == 'forward':
        cluster = {'local_device': 'node', 'main_device': 'main', 'tunnel': 'forward',
                   'main_url': 'http://127.0.0.1:7780'}
        devices = {'node': {'hostname': 'node-host'}, 'main': peer}
    else:
        cluster = {'local_device': 'main', 'main_device': 'main', 'tunnel': 'reverse',
                   'main_url': 'http://127.0.0.1:7780'}
        devices = {'main': {'hostname': 'main-host', 'transport': 'local'}, 'node': peer}
    return {'cluster': cluster, 'devices': devices}


class Proc:
    def __init__(self, stay):
        self.returncode = 0
        self.dead = not stay

    def poll(self):
        return None if not self.dead else 0

    def terminate(self):
        self.dead = True

    def kill(self):
        self.dead = True

    def wait(self, **kwargs):
        return 0


def drive(config, reachable, stay, done, prepare=None, limit=400.0):
    """Run the supervisor on a fake clock. `done` stops it; `limit` is the backstop."""
    attempts, probes, batches, clock = [], [], [], [0.0]
    state_holder = []

    def finish():
        if clock[0] > limit or done(attempts, clock[0]):
            (state_holder[0] / 'cluster-tunnel.stop').touch()

    def popen(argv, **kwargs):
        if not any(arg.startswith('HostKeyAlias=') for arg in argv):
            raise AssertionError('SSH is not pinned')
        target = argv[-1]
        attempts.append((clock[0], target))
        finish()
        return Proc(stay(target))

    def sleep(seconds):
        clock[0] += seconds
        if prepare is not None:
            prepare(clock[0], state_holder[0])
        finish()

    def wrapped(target, timeout=2):
        probes.append((clock[0], target, timeout))
        return reachable(target, clock[0])

    real_probe = cluster_recovery.parallel_probe

    def spy(probe, targets, workers=16):
        batches.append(list(targets))
        return real_probe(probe, targets, workers)

    with tempfile.TemporaryDirectory() as tmp:
        state_holder.append(Path(tmp))
        with patch.object(tunnel.shutil, 'which', return_value='ssh'), \
                patch.object(tunnel.subprocess, 'Popen', side_effect=popen), \
                patch.object(tunnel, 'reachable', side_effect=wrapped), \
                patch.object(tunnel.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(tunnel.time, 'sleep', side_effect=sleep), \
                patch.object(tunnel, 'hub_healthy', return_value=True), \
                patch('jarvis.linkfinder.discover', return_value=[]), \
                patch.object(cluster_recovery, 'parallel_probe', side_effect=spy):
            tunnel.run(config, state_holder[0])
    return attempts, probes, batches


def at(probes, moment):
    return sorted(target for now, target, _timeout in probes if now == moment)


class FailbackTests(unittest.TestCase):
    def test_targets_ahead_follows_preference_order(self):
        fresh = [LAN, 'u@192.0.2.3', VPN]
        self.assertEqual(tunnel.targets_ahead(fresh, VPN), [LAN, 'u@192.0.2.3'])
        self.assertEqual(tunnel.targets_ahead(fresh, LAN), [])
        self.assertEqual(tunnel.targets_ahead([LEARNED, *fresh], VPN)[0], LEARNED)
        self.assertEqual(tunnel.targets_ahead([LEARNED], VPN), [LEARNED])

    def test_long_session_on_the_open_fallback_still_returns_to_lan(self):
        # LAN is TCP-down at rank time, so the open VPN is rank 0. The old check
        # only probed ordered[:index] and never looked at the LAN again.
        step = tunnel.FAILBACK_PROBE_S
        back_at = 3 * step

        def reachable(target, now):
            if target == VPN:
                return True
            if target == LAN:
                return now >= back_at
            return False

        for mode in ('forward', 'reverse'):
            with self.subTest(mode=mode):
                attempts, probes, batches = drive(
                    tunnel_config(mode), reachable, stay=lambda target: target == VPN,
                    done=lambda attempts, now: any(target == LAN for _, target in attempts))
                self.assertEqual([target for _, target in attempts], [VPN, LAN])
                lones = [now for now in _moments(probes) if at(probes, now) == [LAN]]
                self.assertEqual(lones, [step, 2 * step, back_at])
                self.assertEqual([batch for batch in batches if batch == [LAN]], [[LAN]] * 3)
                self.assertTrue(all(timeout == tunnel.REACH_TIMEOUT for _, _, timeout in probes))

    def test_a_failed_switch_back_does_not_stretch_the_next_probe(self):
        step = tunnel.FAILBACK_PROBE_S

        def reachable(target, now):
            if target == VPN:
                return True
            return target == LAN and now >= step

        def done(attempts, now):
            return sum(target == LAN for _, target in attempts) >= 2

        attempts, probes, _batches = drive(
            tunnel_config('forward'), reachable, stay=lambda target: target == VPN, done=done)
        self.assertEqual([target for _, target in attempts], [VPN, LAN, VPN, LAN])
        vpn_again = [now for now, target in attempts if target == VPN][1]
        later = [now for now in _moments(probes) if now >= vpn_again and at(probes, now) == [LAN]]
        self.assertEqual(later[0] - vpn_again, step)

    def test_learned_address_is_probed_during_a_session_and_wins(self):
        # The rank snapshot at t=0 does not contain the learned address. It has to be
        # picked up while the VPN session is still up, and it wins over the LAN.
        step = tunnel.FAILBACK_PROBE_S
        wrote = []

        def prepare(now, state):
            if now < step / 2 or wrote:
                return
            wrote.append(now)
            note = {'node': 'node', 'peers': {'main': {'values': [LEARNED_IP]}}}
            (state / 'cluster-addresses.json').write_text(json.dumps(note), encoding='utf-8')

        def reachable(target, now):
            if target == VPN:
                return True
            return target == LEARNED and now >= step / 2

        attempts, probes, batches = drive(
            tunnel_config('forward'), reachable, stay=lambda target: target == VPN,
            done=lambda attempts, now: any(target == LEARNED for _, target in attempts),
            prepare=prepare)
        self.assertEqual([target for _, target in attempts], [VPN, LEARNED])
        self.assertNotIn(LEARNED, at(probes, 0))
        self.assertIn([LEARNED, LAN], batches)
        self.assertTrue(all(timeout == tunnel.REACH_TIMEOUT for _, _, timeout in probes))
        self.assertLess(tunnel.REACH_TIMEOUT, 5)

    def test_a_session_on_the_preferred_target_does_not_probe_worse_ones(self):
        step = tunnel.FAILBACK_PROBE_S
        attempts, _probes, batches = drive(
            tunnel_config('forward'), reachable=lambda target, now: True,
            stay=lambda target: target == LAN,
            done=lambda attempts, now: now >= 2 * step + 5)
        self.assertEqual([target for _, target in attempts], [LAN])
        self.assertEqual(batches, [[LAN, VPN]])


def _moments(probes):
    seen = []
    for now, _target, _timeout in probes:
        if now not in seen:
            seen.append(now)
    return seen


if __name__ == '__main__':
    unittest.main()
