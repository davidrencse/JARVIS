"""Ready-recovery reconnection: real signatures, gateway, Hub and broker; fake restart only."""
import copy
import json
import secrets
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import cluster, deploy
from jarvis.cluster_store import NODE_DEFAULT
from jarvis.continuity import recovery_repoint as ready, repoint
from jarvis.continuity.authority import LEASE_SECONDS, SAFETY_MARGIN
from jarvis.continuity.runtime import RuntimeRefusal
from tests.continuity import test_participant_channel as fixtures
from tests.continuity.test_gateway import _keys


class RecoveryRepointTests(unittest.TestCase):
    def setUp(self):
        self.env = fixtures.ParticipantChannelTests(); self.env.setUp()
        self.addCleanup(self.env.doCleanups)
        e = self.env
        e.seat_zenbook()
        # Former MAIN disappears without release or a source RPC. Recover Dell
        # using authority fencing after its full validity+margin elapsed.
        e.clock.tick(LEASE_SECONDS + SAFETY_MARGIN + 1)
        tid = e.open('dell', 'recover')
        e.store.readiness(tid, True, False, 'cp-dell')
        e.store.drain(tid); e.store.fence(tid)
        e.participants['dell'].runtime.start()
        e.participants['dell'].runtime.accept_lease(e.store.grant(tid))
        e.store.complete(tid, True)
        e.participants['dell'].runtime.renew_once()
        self.code = deploy.code_fingerprint()
        self.pins = {d: {'kind': 'pc', 'installation_id': d+'-inst', 'public_key': _keys()[d][0]}
                     for d in e.participants}
        self.configs = {}
        for d in e.participants:
            self.configs[d] = {'cluster': {'enabled': True, 'local_device': d,
                'main_device': 'dell' if d == 'dell' else 'zenbook', 'auto_update': False,
                'auto_release': False, 'main_url': 'http://127.0.0.1:1'},
                'devices': {'dell': {}, 'zenbook': {}},
                'continuity': {'operator_config': 'operator.json'}}
            (e.states[d].parent / 'operator.json').write_text(json.dumps({
                'origin': 'http://127.0.0.1:1', 'authority_id': 'oracle-auth', 'device_id': d,
                'installation_id': d+'-inst', 'private_key_path': str(_keys()[d][1]),
                'enrollment': self.pins}))
        self.hub = cluster.Hub(self.configs['dell'], e.states['dell'].parent,
                               e.states['dell'], console=Mock())
        self.hub.continuity_standby = True
        self.hub.start(port=0)
        self.addCleanup(self.hub.close)
        port = self.hub.server.server_address[1]
        self.endpoint = 'http://127.0.0.1:' + str(port)
        self.hub.config['cluster']['main_url'] = self.endpoint
        self.hub.continuity_endpoint = self.endpoint
        self.controllers = {}
        for d, p in e.participants.items():
            c = SimpleNamespace(participant=p, local=d, state=e.states[d], pins=self.pins,
                hub=self.hub if d == 'dell' else None, base=e.states[d].parent,
                config=self.configs[d], loaded_code=self.code,
                operator={'successor_routes': {'dell': {'endpoint': self.endpoint,
                    'listen_host': '127.0.0.1', 'listen_port': port, 'link': None}}})
            self.controllers[d] = c
            p.production_controller = c
        self.issuer = ready.Issuer(self.controllers['dell'], allow_loopback=True)
        self.controllers['dell'].ready_rejoin_issuer = lambda: self.issuer
        self.parity = patch('jarvis.release_runtime.require_loaded_code', return_value={'matched': True})
        self.parity.start(); self.addCleanup(self.parity.stop)

    def request(self, **changes):
        e = self.env
        body = {key:e.store.status().get(key) for key in ready.TUPLE}
        body.update(schema=ready.REQUEST, device_id='zenbook', installation_id='zenbook-inst',
                    nonce='rejoin-'+secrets.token_hex(16), at=int(e.clock.wall()), code=self.code)
        body.update(changes)
        body['membership'] = ready._membership(e.clients['zenbook'], body['nonce'], body['at'])
        return ready._signed(body, e.clients['zenbook'])

    def offer(self):
        return self.issuer.offer(self.request())['body']['instruction']

    def installer(self, **kwargs):
        return ready.Installer(self.controllers['zenbook'], allow_loopback=True, **kwargs)

    def test_real_http_offer_probe_and_eventual_peer_poll_without_source(self):
        restarts = []
        rejoin = ready.Rejoin(self.controllers['zenbook'], restart=lambda: restarts.append(True),
                              allow_loopback=True)
        old = copy.deepcopy(self.configs['zenbook'])
        self.assertTrue(rejoin.tick()['ok'])
        self.assertEqual(restarts, [True])
        state = self.env.states['zenbook']
        view = repoint.effective_config(old, state)
        self.assertEqual(view['cluster']['main_device'], 'dell')
        self.assertEqual(old, self.configs['zenbook'])
        note = repoint._read(state/'cluster-node.json')
        self.assertEqual(self.hub.broker.authenticate(note['token']), ('zenbook', set(NODE_DEFAULT)))
        worker = cluster.NodeWorker(old, state, executor=lambda *_: None)
        worker.code = self.code
        # No old Hub, source signer, desktop action, or model participates.
        with patch('jarvis.cluster.POLL_WAIT', 0.01), patch.object(worker, '_can_pull', return_value=False):
            worker.once()
        row = next(n for n in self.hub.broker.nodes() if n['id'] == 'zenbook')
        self.assertTrue(row['online'])
        self.assertEqual(row['code'], self.code)
        # Pass the 15 s retry spacing while the successor keeps its 15 s lease renewed.
        for _ in range(2):
            self.env.clock.tick(8)
            self.env.participants['dell'].runtime.renew_once()
        self.assertTrue(rejoin.tick()['already'])
        self.assertEqual(restarts, [True])

    def test_new_nonces_reuse_stable_offer_and_do_not_reset_broker_heartbeat(self):
        first = self.offer()
        self.hub.broker.heartbeat('zenbook', {'code': self.code})
        second = self.offer()
        self.assertEqual(first, second)
        self.assertTrue(self.hub.broker.nodes()[0]['online'])

    def test_exact_request_replay_and_stale_request_refused(self):
        request = self.request()
        self.issuer.offer(request)
        with self.assertRaises(RuntimeRefusal):
            self.issuer.offer(request)
        with self.assertRaises(RuntimeRefusal):
            self.issuer.offer(self.request(at=int(self.env.clock.wall())-31))

    def test_wrong_installation_signature_cannot_enroll(self):
        request = self.request()
        request['signature'] = ready._signed(request['body'], self.env.clients['dell'])['signature']
        with self.assertRaises(RuntimeRefusal):
            self.issuer.offer(request)
        self.assertEqual(self.hub.broker.nodes(), [])

    def test_gateway_removed_peer_is_refused_even_if_local_pins_remain(self):
        self.env.clients['zenbook'].disable_participant('zenbook', 'zenbook-inst')
        with self.assertRaises(RuntimeRefusal):
            self.issuer.offer(self.request())
        self.assertEqual(self.hub.broker.nodes(), [])

    def test_unready_or_paused_holder_cannot_offer_or_attest(self):
        runtime = self.env.participants['dell'].runtime
        for change in ('paused', 'held', 'unready'):
            with self.subTest(change=change):
                request = self.request()
                if change == 'paused':
                    runtime._renewal_paused = True
                elif change == 'held':
                    runtime.stop_new_admission('stopped')
                else:
                    self.env.ready['dell'] = False
                with self.assertRaises(RuntimeRefusal):
                    self.issuer.offer(request)
                with self.assertRaises(RuntimeRefusal):
                    self.issuer.proof('zenbook', 'probe-1')
                runtime._renewal_paused = False
                runtime._hold = None
                self.env.ready['dell'] = True

    def test_code_parity_refusal_does_not_publish_or_restart(self):
        with patch('jarvis.release_runtime.require_loaded_code', side_effect=ValueError('mismatch')):
            with self.assertRaises(RuntimeRefusal):
                self.issuer.offer(self.request())
        with self.assertRaises(RuntimeRefusal):
            self.issuer.offer(self.request(code='0'*16))
        self.assertFalse((self.env.states['zenbook']/repoint.FILE).exists())

    def test_wrong_nonce_or_wrong_signer_endpoint_proof_cannot_publish(self):
        offer = self.offer()
        for tamper in ('nonce', 'signer'):
            def bad_probe(body, nonce):
                receipt = self.issuer.proof('zenbook', nonce if tamper == 'signer' else 'old-nonce')
                return ready._signed(receipt['body'], self.env.clients['zenbook']) if tamper == 'signer' else receipt
            with self.subTest(tamper=tamper), self.assertRaises(RuntimeRefusal):
                self.installer(endpoint_probe=bad_probe).install(offer)
        self.assertFalse((self.env.states['zenbook']/repoint.FILE).exists())

    def test_lost_or_changed_authority_tuple_cannot_publish(self):
        offer = self.offer()
        original = self.env.clients['zenbook'].status
        for key, value in (('epoch', 999), ('incarnation', 99), ('main_ready', False), ('lease_valid', False)):
            with self.subTest(key=key), patch.object(self.env.clients['zenbook'], 'status',
                                                   return_value={**original(), key:value}):
                with self.assertRaises(RuntimeRefusal):
                    self.installer().install(offer)
        self.assertFalse((self.env.states['zenbook']/repoint.FILE).exists())

    def test_probe_lease_expiry_rechecked_before_publication(self):
        offer = self.offer()
        def slow(body, nonce):
            result = self.issuer.proof('zenbook', nonce)
            self.env.clock.tick(LEASE_SECONDS + 1)
            return result
        with self.assertRaises(RuntimeRefusal):
            self.installer(endpoint_probe=slow).install(offer)
        self.assertFalse((self.env.states['zenbook']/repoint.FILE).exists())

    def test_old_transition_verifier_still_refuses_ready_recovery_offer(self):
        with self.assertRaises(RuntimeRefusal):
            repoint.verify(self.offer(), authority_id='oracle-auth', target='zenbook', pins=self.pins)

    def test_production_transport_refuses_loopback_or_ssh_routes(self):
        with self.assertRaises(RuntimeRefusal):
            ready.Issuer(self.controllers['dell']).offer(self.request())
        self.controllers['dell'].operator['successor_routes']['dell']['link'] = {'targets': []}
        with self.assertRaises(RuntimeRefusal):
            self.issuer.offer(self.request())

    def test_offer_persisted_before_broker_install_can_retry_same_credential_after_crash(self):
        request = self.request()
        with patch.object(self.hub.broker, 'enroll', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.issuer.offer(request)
        path = next(self.env.states['dell'].glob('continuity-ready-offer-*.json'))
        saved = repoint._read(path)
        retried = self.offer()
        self.assertEqual(saved, retried)
        self.assertEqual(self.hub.broker.authenticate(saved['body']['token'])[0], 'zenbook')
