"""Reboot pipeline regressions and 100,000 deterministic in-memory guard scenarios.

The sweep executes the production _guard, replacing every I/O boundary with plain
functions. Each scenario ends with a fair recovery suffix (two together readings,
working start, switches enabled, remote ended). No real services or input.
"""
import json
import ipaddress
import tomllib
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import sharedinput as si
from jarvis.desktop import lan_mouse_config
from jarvis.lanmouse_peer_ips import write_ips, prepare_ips

FP = ':'.join(['ab'] * 32)


class RebootTests(unittest.TestCase):
    def test_previous_boot_markers_ignored_future_markers_fail_closed(self):
        import os
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(si.platforms, 'boot_time', return_value=200), mock.patch('time.time', return_value=300):
            for name, fn in ((si.REMOTE_MARKER, si.remote_session_live), (si.CONTROLLING_MARKER, si.controlling_remote)):
                p = Path(tmp) / name
                p.write_text('{}')
                for stamp, expected in ((100, False), (1000, True), (290, True)):
                    os.utime(p, (stamp, stamp))
                    self.assertEqual(fn(tmp), expected)

    def test_pairing_rejects_empty_and_vpn_without_touching_original(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(si, '_desktop', return_value='windows'), mock.patch.object(si, 'lan_mouse_status', return_value={'running': False}):
            p = Path(tmp) / 'config.toml'
            p.write_text(lan_mouse_config('Dell', ['172.1.1.2'], 'right', FP))
            original = p.read_bytes()
            for ips in ([], ['100.74.1.2']):
                with self.assertRaises(ValueError):
                    si.write_pairing('Dell', ips, 'right', FP, p)
                self.assertEqual(p.read_bytes(), original)
            si.write_pairing('Dell', ['172.1.1.3'], 'right', FP, p)
            self.assertEqual(next(p.parent.glob('*.bak-*')).read_bytes(), original)
            si.validate_pairing(p.read_text())

    def test_watcher_preserves_trust_and_backup_on_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'config.toml'
            p.write_text(lan_mouse_config('Dell', ['172.1.1.2'], 'right', FP))
            original = p.read_text()
            with mock.patch('jarvis.fsio.os.replace', side_effect=OSError('crash')):
                with self.assertRaises(OSError):
                    write_ips(p, 'Dell', ['172.1.1.3'])
            self.assertEqual(p.read_text(), original)
            self.assertTrue(write_ips(p, 'Dell', ['172.1.1.3']))
            self.assertEqual(si.validate_pairing(p.read_text())['authorized_fingerprints'], {FP: 'Dell'})
            for ips in ([], ['100.64.0.1'], ['fd7a:115c:a1e0::1']):
                before = p.read_bytes()
                with self.assertRaises(ValueError):
                    write_ips(p, 'Dell', ips)
                self.assertEqual(p.read_bytes(), before)

    def test_windows_retries_task_and_verifies_running(self):
        from types import SimpleNamespace
        with mock.patch.object(si.os, 'name', 'nt'), mock.patch.object(si, '_desktop', return_value='windows'), mock.patch.object(si, '_units', return_value=[]), mock.patch.object(si, '_task_exists', return_value=True), mock.patch.object(si, '_run', return_value=SimpleNamespace(returncode=0)), mock.patch.object(si, 'lan_mouse_status', return_value={'running': False}), mock.patch.object(si, '_quick_status', return_value={'running': False}), mock.patch('time.sleep'):
            with mock.patch.object(si, '_run', return_value=SimpleNamespace(returncode=0)) as run:
                result = si.start()
            self.assertFalse(result['started'])
            self.assertEqual(run.call_count, 3)

    def test_manual_stop_records_before_action(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(si, 'stop', side_effect=RuntimeError('crash')):
            self.assertFalse(si.control('stop', tmp, now=1000)[0])
            self.assertEqual(json.loads((Path(tmp) / si.GUARD_FILE).read_text())['manual_stop_at'], 1000)

    def test_crash_after_launch_does_not_create_manual_override(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(si, '_notify'):
            state = Path(tmp)
            (state / si.GUARD_FILE).write_text(json.dumps(dict(boot=1000, pending_start=True,
                stopped_by_guard=True, stopped_at=0, together=2)))
            running = [True]
            for clock in (2000, 2100):
                out = si.guard(state, nearby=lambda: {'near': False, 'why': 'apart'},
                    status_fn=lambda: {'running': running[0]}, start_fn=lambda: self.fail('start'),
                    stop_fn=lambda: running.__setitem__(0, False), boot=1000, now=clock)
            self.assertFalse(out['override'])
            self.assertFalse(running[0])
            self.assertEqual(out['action'], 'stopped')

    def test_failed_stop_is_retried_not_mistaken_for_manual_override(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(si, '_notify'):
            stop = mock.Mock(return_value={'stopped': False, 'running': True})
            for clock in (1000, 1100, 1200):
                out = si.guard(tmp, nearby=lambda: {'near': False, 'why': 'apart'},
                    status_fn=lambda: {'running': True}, stop_fn=stop, start_fn=lambda: self.fail('start'), now=clock)
            self.assertEqual(out['action'], 'stop_failed')
            self.assertFalse(out['override'])
            self.assertEqual(stop.call_count, 2)

    def test_boot_resets_stale_apart_override_and_streaks(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(si, '_notify'):
            state = Path(tmp)
            (state / si.GUARD_FILE).write_text(json.dumps(dict(boot=1000, apart=50, together=50,
                apart_now=True, override=True, stopped_by_guard=False, manual_stop_at=1100)))
            running = [False]
            def tick():
                return si.guard(state, nearby=lambda: {'near': True, 'why': 'test'},
                    status_fn=lambda: {'running': running[0]}, start_fn=lambda: running.__setitem__(0, True),
                    stop_fn=lambda: running.__setitem__(0, False), autostart=True, boot=2000, now=2100)
            self.assertIsNone(tick()['action'])
            self.assertFalse(running[0])
            self.assertEqual(tick()['action'], 'started')

    def test_missing_boot_and_persistence_failure_have_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = dict(nearby=lambda: {'near': True, 'why': 'test'},
                        status_fn=lambda: {'running': False}, start_fn=lambda: self.fail('unexpected start'),
                        stop_fn=lambda: self.fail('unexpected stop'), autostart=True)
            self.assertEqual(si.guard(tmp, **args)['reason'], 'boot_unavailable')
            with mock.patch.object(si, '_save_memo', return_value=False):
                self.assertEqual(si.guard(tmp, boot=2000, **args)['reason'], 'persistence_failed')

    def test_100000_boot_state_machine_scenarios(self):
        rng = random.Random(20261112)
        started_at = time.monotonic()
        box = {}
        def save(path, memo):
            box['memo'] = dict(memo)
        def status():
            return {'running': box['running']}
        def start():
            assert not box['remote'], box
            assert not box['manual'], box
            if box['fail'] == 1:
                raise RuntimeError('injected launch failure')
            box['running'] = True
            if box['fail'] == 2:
                raise RuntimeError('crash after daemon launch')
        def stop():
            box['running'] = False
        def tick(near):
            box['clock'] += 60
            result = si._guard(Path('/fake'), lambda: {'near': near, 'why': 'fuzz'}, status, start, stop,
                               box['auto'], box['boot'], box['clock'], box['enabled'])
            memo = box['memo']
            if memo.get('together', 0) >= 2:
                box['hand'] = False
            if memo.get('apart_now') and not box['hand'] and not memo.get('override'):
                assert not box['running'], box
            if box['manual']:
                assert not box['running'], box
            # The config artifact is immutable unless an explicit validated update succeeds.
            assert box['config'] == box['known_good'], box
            return result
        config = lan_mouse_config('Dell', ['172.1.1.2'], 'right', FP)
        si.validate_pairing(config)
        with mock.patch.object(si, 'read_json', side_effect=lambda *a: dict(box.get('memo', {}))), mock.patch.object(si, '_save_memo', side_effect=save), mock.patch.object(si, '_notify', lambda *a: None), mock.patch.object(si, 'remote_session_live', side_effect=lambda *a: box['remote']), mock.patch.object(si, 'controlling_remote', return_value=False):
            # Replace mocks' hot-path overhead with plain functions for the large sweep.
            with mock.patch.object(si, 'read_json', lambda *a: dict(box.get('memo', {}))), mock.patch.object(si, '_save_memo', save), mock.patch.object(si, 'remote_session_live', lambda *a: box['remote']), mock.patch.object(si, 'controlling_remote', lambda *a: False):
                for scenario in range(100000):
                    box.clear()
                    box.update(memo={}, hand=False, running=False, remote=False, manual=False, fail=False,
                               auto=True, enabled=True, boot=1000., clock=2000., config=config, known_good=config)
                    for _ in range(8):
                        event = rng.randrange(15)
                        if event == 0:  # local reboot; persisted memo survives
                            box.update(boot=box['clock'] + 600, clock=box['clock'] + 610, running=False, manual=False, remote=False, hand=False)
                        elif event == 1:  # main reboot / sleep while node remains up
                            tick(False); tick(False)
                        elif event == 2:  # explicit manual stop intent, including crash mid-stop
                            box['manual'] = True
                            box['running'] = False
                            box['memo'].update(manual_stop_at=box['clock'], stopped_by_guard=False)
                        elif event == 3 and not box['remote']:  # manual start while together (explicit override)
                            box.update(manual=False)
                            box['memo'].pop('manual_stop_at', None)
                            tick(True); tick(True)
                            box['running'] = True
                            box['hand'] = True
                        elif event == 4:
                            box['fail'] = rng.randrange(3)
                        elif event == 5:  # live remote owns input; its pause records debt
                            box.update(remote=True, running=False)
                            if not box['manual']:
                                box['memo']['stopped_by_guard'] = True
                        elif event == 6:
                            box['remote'] = False  # expired stale marker / ended session
                        elif event == 7:
                            box['enabled'] = not box['enabled']
                        elif event == 8:
                            box['auto'] = not box['auto']
                        elif event == 9:  # process crash: retain durable state
                            box['running'] = False
                        elif event == 14:
                            candidate = rng.choice(([], ['100.64.0.1'], ['not-ip'], ['172.1.1.3']))
                            try:
                                updated = prepare_ips(box['config'], 'Dell', candidate)
                            except ValueError:
                                pass
                            else:
                                parsed = tomllib.loads(updated)  # independent config oracle
                                assert parsed['authorized_fingerprints'] == {FP: 'Dell'}
                                assert len(parsed['clients']) == 1
                                client = parsed['clients'][0]
                                assert client['hostname'] == 'Dell' and client['ips']
                                for ip in client['ips']:
                                    assert ipaddress.ip_address(ip) not in ipaddress.ip_network('100.64.0.0/10')
                                box['config'] = box['known_good'] = updated
                        else:
                            tick(rng.choice((True, False, None)))
                    # A fresh boot must recover within exactly GUARD_STREAK fair readings.
                    box.update(boot=box['clock'] + 600, clock=box['clock'] + 610, running=False,
                               manual=False, remote=False, fail=False, auto=True, enabled=True, hand=False)
                    for _ in range(si.GUARD_STREAK):
                        tick(True)
                    assert box['running'], (scenario, box)
        print(f'Lan Mouse reboot fuzz: 100000 scenarios, {time.monotonic() - started_at:.3f}s', flush=True)
