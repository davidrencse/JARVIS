import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from jarvis import release_gate as gate


def report(*failures, ids=(), safety=()):
    return {'failures': list(failures), 'ids': list(ids), 'safety': list(safety), 'count': 1}


class GateTests(unittest.TestCase):
    def run_gate(self, results):
        with patch.object(gate, 'wait_for_capacity', return_value=True), patch.object(gate, '_run', side_effect=results):
            return gate.run_tests('stage')

    def test_core_failure_blocks_before_full(self):
        ok, result = self.run_gate([report('tests.cluster.test_x.C.test_bad'), report('tests.cluster.test_x.C.test_bad')])
        self.assertFalse(ok)
        self.assertEqual(result['failures'], ['tests.cluster.test_x.C.test_bad'])
        self.assertNotIn('full', result)

    def test_full_failure_warns_after_isolated_retry(self):
        name = 'tests.misc.test_x.C.test_bad'
        ok, result = self.run_gate([report(), report(name), report(name)])
        self.assertTrue(ok)
        self.assertEqual(result['warnings'], [name])

    def test_safety_or_tagged_or_core_full_failure_blocks(self):
        for name, ids, tagged in [('tests.security.test_x.C.test_bad', [], []),
                                   ('tests.misc.test_hard_deny.C.test_bad', [], []),
                                   ('tests.misc.test_autostart.C.test_guard', [], []),
                                   ('tests.misc.test_x.C.test_bad', [], ['tests.misc.test_x.C.test_bad']),
                                   ('tests.cluster.test_x.C.test_bad', ['tests.cluster.test_x.C.test_bad'], [])]:
            with self.subTest(name=name):
                ok, result = self.run_gate([report(ids=ids), report(name, safety=tagged), report(name)])
                self.assertFalse(ok)
                self.assertEqual(result['failures'], [name])

    def test_flaky_test_passes_on_isolated_retry(self):
        ok, result = self.run_gate([report('tests.cluster.test_x.C.test_flaky'), report(), report()])
        self.assertTrue(ok)
        self.assertEqual(result['core']['retried'], ['tests.cluster.test_x.C.test_flaky'])
        self.assertEqual(result['warnings'], [])

    def test_incomplete_full_runner_is_blocking_not_a_shipping_warning(self):
        for incomplete in (report('runner:full'), {**report(), 'complete': False}):
            with self.subTest(incomplete=incomplete):
                ok, result = self.run_gate([report(), incomplete])
                self.assertFalse(ok)
                self.assertIn('runner:full', result['failures'])
                self.assertEqual(result['warnings'], [])

    def test_overload_defers_without_running_or_counting_failure(self):
        with patch.object(gate, 'wait_for_capacity', return_value=False), patch.object(gate, '_run') as run:
            ok, result = gate.run_tests('stage')
            self.assertFalse(ok)
            self.assertTrue(result['deferred'])
            self.assertEqual(result['failures'], [])
            run.assert_not_called()

    def test_load_wait_is_bounded(self):
        with patch.object(gate.os, 'getloadavg', return_value=(3,3,3)), patch.object(gate.os, 'cpu_count', return_value=2), patch.object(gate.time, 'monotonic', side_effect=[0,0,60]), patch.object(gate.time, 'sleep') as sleep:
            self.assertFalse(gate.wait_for_capacity())
            sleep.assert_called_once_with(2)

    def test_load_can_settle_within_bound(self):
        with patch.object(gate.os, 'getloadavg', side_effect=[(3,3,3), (1,1,1)]), patch.object(gate.os, 'cpu_count', return_value=2), patch.object(gate.time, 'monotonic', side_effect=[0,0]), patch.object(gate.time, 'sleep'):
            self.assertTrue(gate.wait_for_capacity())

    def test_actual_fake_suite_retries_in_a_new_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tests').mkdir()
            (root / 'tests/__init__.py').write_text('')
            (root / 'tests/test_fake.py').write_text('''import unittest
from pathlib import Path
class C(unittest.TestCase):
    def test_flaky(self):
        p = Path('first-run')
        if not p.exists():
            p.touch()
            self.fail('first run only')
''')
            result = gate._tier(root, 'full', 20)
            self.assertEqual(result['failures'], [])
            self.assertEqual(result['retried'], ['tests.test_fake.C.test_flaky'])

    def test_timeout_records_active_test_name(self):
        import json
        import subprocess
        def timeout(argv, **kw):
            Path(argv[-1]).write_text(json.dumps({'active': 'tests.test_fake.C.test_slow', 'failures': [], 'count': 1, 'complete': False}))
            raise subprocess.TimeoutExpired(argv, kw['timeout'])
        with patch.object(gate.subprocess, 'run', side_effect=timeout):
            result = gate._run('stage', 'full', 1)
            self.assertIn('tests.test_fake.C.test_slow', result['failures'])
            self.assertIn('runner:full', result['failures'])
