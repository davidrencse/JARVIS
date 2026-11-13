"""Relay outage and epoch changes must never pin subsequent reads to the old MAIN."""
import hashlib
import unittest
from unittest.mock import patch

from jarvis.continuity.gateway import ContinuityGateway, GatewayError, RELAY_PATH, _Record
from tests.continuity.test_gateway_frame_timeout import _Gateway, _principal


class GatewayFailoverTests(unittest.TestCase):
    def test_failed_arch_read_then_dell_takeover_uses_the_new_origin(self):
        gateway = _Gateway('http://127.0.0.1:9001')
        view = {'main': 'zenbook', 'authority_id': 'oracle-auth', 'incarnation': 1, 'epoch': 1}
        gateway._relay_status = lambda: dict(view)
        gateway._devices['dell'] = _Record('dell', 'dell-inst', 'pc', 'ssh-ed25519 AAAA',
                                          frozenset({'client'}), 'http://127.0.0.1:9002')
        envelope = b'{}'
        payload = {'operation_id': 'read0001', 'target': 'zenbook', 'attempt_id': 'attempt01',
                   'digest': 'sha256:' + hashlib.sha256(envelope).hexdigest(),
                   'capability': 'client', 'kind': 'overview'}
        def read():
            return ContinuityGateway.forward_transient(
                gateway, _principal(gateway._token, payload), payload, envelope)
        with patch('jarvis.continuity.gateway._post_json', side_effect=GatewayError('no_relay')):
            with self.assertRaisesRegex(GatewayError, 'no_relay'):
                read()
        view.update(main='dell', epoch=2)
        payload.update(target='dell', operation_id='read0002')
        with patch('jarvis.continuity.gateway._post_json', return_value={
                'operation_id': 'read0002', 'result': {'status': 200}}) as post:
            self.assertTrue(read()['relayed'])
        self.assertEqual(post.call_args.args[0], 'http://127.0.0.1:9002' + RELAY_PATH)

    def test_held_read_from_old_main_is_discarded_after_authority_moves(self):
        gateway = _Gateway('http://127.0.0.1:9001')
        view = {'main': 'zenbook', 'authority_id': 'oracle-auth', 'incarnation': 1, 'epoch': 1}
        gateway._relay_status = lambda: dict(view)
        envelope = b'{}'
        payload = {'operation_id': 'read0001', 'target': 'zenbook', 'attempt_id': 'attempt01',
                   'digest': 'sha256:' + hashlib.sha256(envelope).hexdigest(),
                   'capability': 'client', 'kind': 'overview'}
        def post(*args, **kwargs):
            view.update(main='dell', epoch=2)
            return {'operation_id': 'read0001', 'result': {'status': 200}}
        with patch('jarvis.continuity.gateway._post_json', side_effect=post):
            with self.assertRaisesRegex(GatewayError, 'no_relay'):
                ContinuityGateway.forward_transient(
                    gateway, _principal(gateway._token, payload), payload, envelope)

    def test_effect_during_takeover_is_not_replayed_to_the_new_main(self):
        gateway = _Gateway('http://127.0.0.1:9001')
        view = {'main': 'zenbook', 'authority_id': 'oracle-auth', 'incarnation': 1,
                'epoch': 1, 'mode_version': 1}
        gateway._relay_status = lambda: dict(view)
        gateway._devices['dell'] = _Record(
            'dell', 'dell-inst', 'pc', 'ssh-ed25519 AAAA', frozenset({'client'}),
            'http://127.0.0.1:9002')
        envelope = b'{"ask":1}'

        def payload(operation_id, target):
            return {'operation_id': operation_id, 'target': target, 'attempt_id': 'attempt01',
                    'digest': 'sha256:' + hashlib.sha256(envelope).hexdigest(),
                    'capability': 'client', 'kind': 'ask'}

        def post(url, data, limit, timeout=2):
            view.update(main='dell', epoch=2)
            return {'operation_id': 'ask000001', 'accepted': True}

        first = payload('ask000001', 'zenbook')
        with patch('jarvis.continuity.gateway._post_json', side_effect=post) as posted:
            with self.assertRaisesRegex(GatewayError, 'no_relay'):
                ContinuityGateway.forward_transient(
                    gateway, _principal(gateway._token, first), first, envelope)
        self.assertEqual(posted.call_args.args[0], 'http://127.0.0.1:9001' + RELAY_PATH)
        self.assertEqual(gateway._effects['ask000001']['state'], 'error')
        with patch('jarvis.continuity.gateway._post_json') as again:
            with self.assertRaisesRegex(GatewayError, 'no_relay'):
                ContinuityGateway.forward_transient(
                    gateway, _principal(gateway._token, first), first, envelope)
        again.assert_not_called()
        second = payload('ask000002', 'dell')
        with patch('jarvis.continuity.gateway._post_json', return_value={
                'operation_id': 'ask000002', 'accepted': True}) as moved:
            result = ContinuityGateway.forward_transient(
                gateway, _principal(gateway._token, second), second, envelope)
        self.assertTrue(result['relayed'])
        self.assertFalse(result['effect_authorized'])
        self.assertEqual(moved.call_args.args[0], 'http://127.0.0.1:9002' + RELAY_PATH)
