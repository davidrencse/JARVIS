"""Successful repoint signature checks are cached. Failures are not.

The underlying check is verify_message, which shells out to ssh-keygen.
These tests mock that wrapper. They do not sign anything.
"""
import copy
import threading
import unittest
from unittest.mock import patch

from jarvis.continuity import repoint
from jarvis.continuity.runtime import RuntimeRefusal


AUTH = 'oracle-auth'
TARGET = 'zenbook'
MAIN = 'dell'
CODE_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R'
OTHER_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
TOKEN = 'placeholder-token-for-shape-validation'
LINK = {
    'targets': ['david@198.51.100.10'],
    'host_key': CODE_KEY,
    'hostname': 'dell.example',
    'identity_file': '/tmp/jarvis-return-repoint-not-a-key',
}


def _body(**overrides):
    body = {
        'schema': repoint.SCHEMA,
        'authority_id': AUTH,
        'fleet_id': 'fleet-1',
        'incarnation': 1,
        'epoch': 2,
        'transition_id': 'transition-1',
        'main': MAIN,
        'installation_id': 'dell-inst',
        'target': TARGET,
        'endpoint': 'https://dell.example.ts.net:443',
        'code_key': CODE_KEY,
        'token': TOKEN,
        'link': copy.deepcopy(LINK),
    }
    body.update(overrides)
    return body


def _instruction(**overrides):
    return {'body': _body(**overrides), 'signature': 'test-signature'}


def _pins(public_key=CODE_KEY, installation_id='dell-inst'):
    return {
        'extra': {'installation_id': 'extra-inst', 'public_key': OTHER_KEY},
        MAIN: {'installation_id': installation_id, 'public_key': public_key},
    }


class RepointVerifyCacheTests(unittest.TestCase):
    def setUp(self):
        repoint.clear_verify_cache()
        self.addCleanup(repoint.clear_verify_cache)

    def _verify(self, instruction=None, *, authority_id=AUTH, target=TARGET, pins=None):
        return repoint.verify(
            _instruction() if instruction is None else instruction,
            authority_id=authority_id,
            target=target,
            pins=_pins() if pins is None else pins,
        )

    def test_second_identical_verify_skips_ssh_keygen(self):
        with patch('jarvis.continuity.repoint.verify_message', return_value=True) as checks:
            first = self._verify()
            second = self._verify()
            # Same pins content in a different insertion order is the same key.
            reordered = {key: _pins()[key] for key in reversed(list(_pins()))}
            third = self._verify(pins=reordered)
        self.assertEqual(checks.call_count, 1)
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertIsNot(second, first)

    def test_changed_content_reverifies(self):
        changed = _instruction(token='another-valid-token-for-the-shape-check')
        with patch('jarvis.continuity.repoint.verify_message', return_value=True) as checks:
            self._verify()
            self._verify(changed)
        self.assertEqual(checks.call_count, 2)
        self.assertEqual(len(repoint._VERIFY_CACHE), 2)

    def test_changed_pins_reverifies(self):
        with patch('jarvis.continuity.repoint.verify_message', return_value=True) as checks:
            self._verify()
            self._verify(pins=_pins(public_key=OTHER_KEY))
            # A pin the signature check consults must not reuse the success,
            # including when the mismatch fails closed before ssh-keygen.
            with self.assertRaises(RuntimeRefusal) as caught:
                self._verify(pins=_pins(installation_id='other-inst'))
            self.assertEqual(caught.exception.code, 'signature')
            self._verify()
        self.assertEqual(checks.call_count, 2)

    def test_different_target_and_authority_do_not_reuse_success(self):
        addressed = _instruction(target='other-node')
        with patch('jarvis.continuity.repoint.verify_message', return_value=True) as checks:
            self._verify()
            other = self._verify(addressed, target='other-node')
            self.assertEqual(other['target'], 'other-node')
            with self.assertRaises(RuntimeRefusal) as caught:
                self._verify(target='other-node')
            self.assertEqual(caught.exception.code, 'signature')
            with self.assertRaises(RuntimeRefusal) as caught:
                self._verify(authority_id='other-auth')
            self.assertEqual(caught.exception.code, 'signature')
            # The original success is still cached. The mismatches were not stored.
            again = self._verify()
        self.assertEqual(again['target'], TARGET)
        self.assertEqual(checks.call_count, 2)

    def test_failure_is_not_cached(self):
        with patch('jarvis.continuity.repoint.verify_message', return_value=False) as checks:
            with self.assertRaises(RuntimeRefusal) as first:
                self._verify()
            with self.assertRaises(RuntimeRefusal) as second:
                self._verify()
        self.assertEqual(first.exception.code, 'signature')
        self.assertEqual(second.exception.code, 'signature')
        self.assertEqual(checks.call_count, 2)
        self.assertEqual(len(repoint._VERIFY_CACHE), 0)
        with patch('jarvis.continuity.repoint.verify_message', return_value=True) as checks:
            self._verify()
            self._verify()
        self.assertEqual(checks.call_count, 1)

    def test_returned_body_mutation_does_not_leak(self):
        with patch('jarvis.continuity.repoint.verify_message', return_value=True) as checks:
            first = self._verify()
            first['token'] = 'mutated-token-mutated-token-mutated'
            first['link']['hostname'] = 'evil.example'
            first['link']['targets'].append('root@203.0.113.50')
            second = self._verify()
        self.assertEqual(checks.call_count, 1)
        self.assertEqual(second['token'], TOKEN)
        self.assertEqual(second['link']['hostname'], 'dell.example')
        self.assertEqual(second['link']['targets'], ['david@198.51.100.10'])
        self.assertIsNot(first['link'], second['link'])
        self.assertIsNot(first['link']['targets'], second['link']['targets'])

    def test_clear_hooks_drop_the_success(self):
        with patch('jarvis.continuity.repoint.verify_message', return_value=True) as checks:
            self._verify()
            repoint._VERIFY_CACHE.clear()
            self._verify()
            repoint.clear_verify_cache()
            self._verify()
        self.assertEqual(checks.call_count, 3)

    def test_cache_evicts_the_oldest_success(self):
        with patch('jarvis.continuity.repoint.verify_message', return_value=True) as checks:
            first = _instruction(token='token-for-cache-slot-00-padding-ok')
            self._verify(first)
            for index in range(1, repoint._VERIFY_CACHE_MAX + 1):
                self._verify(_instruction(token=f'token-for-cache-slot-{index:02d}-padding-ok'))
            self.assertEqual(len(repoint._VERIFY_CACHE), repoint._VERIFY_CACHE_MAX)
            self._verify(first)
            newest = _instruction(token=f'token-for-cache-slot-{repoint._VERIFY_CACHE_MAX:02d}-padding-ok')
            self._verify(newest)
        # The first success was evicted. The newest one was still cached.
        self.assertEqual(checks.call_count, repoint._VERIFY_CACHE_MAX + 2)

    def test_concurrent_calls_return_independent_copies(self):
        calls = {'n': 0}
        guard = threading.Lock()

        def fake(*_args, **_kwargs):
            with guard:
                calls['n'] += 1
            return True

        barrier = threading.Barrier(4)
        found = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                found.append(self._verify())
            except Exception as exc:
                errors.append(exc)

        with patch('jarvis.continuity.repoint.verify_message', side_effect=fake):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(found), 4)
        self.assertTrue(all(body['token'] == TOKEN for body in found))
        self.assertEqual(len({id(body) for body in found}), 4)
        self.assertGreaterEqual(calls['n'], 1)
        self.assertLessEqual(calls['n'], 4)


if __name__ == '__main__':
    unittest.main()
