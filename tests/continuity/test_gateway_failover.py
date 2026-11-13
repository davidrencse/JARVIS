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
