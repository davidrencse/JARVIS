"""Participant RPC using signed fake transports and a real in-process gateway.

No sockets, SSH peers, service actions, live configs or model calls.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.continuity import gateway as g, participant as rpc
from jarvis.continuity.executor import ExecutorGuard
from jarvis.continuity.authority import AuthorityError
from jarvis.continuity.runtime import GatewayPort, MainRuntime, RuntimeRefusal
from tests.continuity._fleet import Clock, open_store
from tests.continuity.test_gateway import _keys, PC


class InProcessClient(g.AuthorityClient):
    def __init__(self, gateway, device, *, owner=False, clock):
        keys = _keys()
        super().__init__('http://127.0.0.1:1', authority_id='oracle-auth',
            device_id=device, installation_id=device+'-inst', private_key_path=keys[device][1],
            owner_private_key_path=keys['owner'][1] if owner else None, wall=clock.wall)
        self.gateway, self.sent = gateway, []

    def _send(self, path, obj):
        self.sent.append((path, copy.deepcopy(obj)))
        if path == '/v1/authority/challenge':
            return self.gateway.issue_challenge(obj['request_digest'])
        if path in g._OWNER_PURPOSE:
            proof = self.gateway._owner_from_body(path, obj)
            return self.gateway._dispatch_owner(g._OWNER_PURPOSE[path], proof, obj['payload'])
        principal = self.gateway._device_from_body(path, obj)
        return self.gateway._dispatch_device(g._DEVICE_PURPOSE[path], principal, obj['payload'])


class ParticipantChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.clock = Clock()
        self.store = open_store(self.root/'authority.sqlite', self.clock)
        self.addCleanup(self.store.close)
        self.gateway = g.ContinuityGateway(self.store, [_keys()['owner'][0]], wall=self.clock.wall)
        self.clients = {d: InProcessClient(self.gateway, d, owner=d=='zenbook', clock=self.clock)
                        for d in ('zenbook', 'dell')}
        owner = self.clients['zenbook']
        for d in self.clients:
            owner.enroll({'device_id': d, 'installation_id': d+'-inst', 'kind': 'pc',
                          'public_key': _keys()[d][0], 'capabilities': PC})
        v = self.store.status()['mode_version']
        v = owner.set_mode('PREPARING_V2', v)['mode_version']
        v = owner.set_mode('QUIESCED', v)['mode_version']
        for client in self.clients.values(): client.acknowledge(v+1, 0)
        owner.set_mode('V2_ONLY_NO_MAIN', v)
        self.port = GatewayPort(owner)
        self.ready = {'zenbook': True, 'dell': True}
        self.participants = {}
        self.states = {}
        for d, client in self.clients.items():
            state = self.root/d/'.jarvis'; state.mkdir(parents=True)
            self.states[d] = state
            runtime = MainRuntime(GatewayPort(client), d, d+'-inst',
                lambda d=d: {'control_ready': self.ready[d]}, clock=self.clock.mono, boot=lambda: 'test-boot')
            guard = ExecutorGuard(state/'executor.sqlite', device_id=d, installation_id=d+'-inst',
                fleet_id='home', authority_id='oracle-auth', authority=GatewayPort(client),
                boot=lambda: 'test-boot', wall=self.clock.wall)
            self.addCleanup(guard.close)
            from jarvis.continuity.integration import configure_production, clear_production, production_issuer
            from jarvis.continuity.authority import TICKET_FIELDS
            guard._verifier = lambda signed: g.verify_message(g.TICKET_NS,
                g._message(g.TICKET_NS, {key: signed[key] for key in TICKET_FIELDS}),
                signed['signature'], _keys()[self.store.status()['main']][0])
            configure_production(state, runtime, executor=guard,
                issuer=production_issuer(_keys()[d][1], client, wall=self.clock.wall))
            self.addCleanup(clear_production, state)
            participant = rpc.Participant(client, runtime, guard=guard, state=state)
            self.participants[d] = participant
            rpc.bind(state, participant); self.addCleanup(rpc.unbind, state)
        self.requester = 'zenbook'

    def payload(self, action, **extra):
        st = self.port.status()
        return {'action': action, 'transition_id': (st.get('transition') or {}).get('transition_id'),
                'incarnation': st['incarnation'], 'epoch': st['epoch'], **extra}

    def call(self, node, payload):
        # A real signed cluster envelope, verified before the actual receiver.
        from jarvis.deploy import sign_job, code_key, verify_job
        import time
        job = sign_job({'id': 'fixture', 'kind': rpc.KIND, 'payload': payload,
                        'requester': self.requester, 'deadline': time.time()+120}, node,
                       self.states[self.requester])
        self.assertIsNone(verify_job(job, node, code_key(self.states[self.requester])))
        return rpc.execute_job(self.states[node], job['payload'], job['requester'])

    def ack_epochs(self, device, start):
        epochs = []
        for path, obj in self.clients[device].sent[start:]:
            if path.endswith('/acknowledge'):
                self.assertEqual(obj['device']['device_id'], device)
                self.assertEqual(obj['device']['installation_id'], device + '-inst')
                self.assertIn('BEGIN SSH SIGNATURE', obj['device']['signature'])
                epochs.append(obj['payload']['fenced_epoch'])
        return epochs

    def stored_epoch(self, device):
        return self.store._db.execute(
            'SELECT fenced_epoch FROM participants WHERE device_id=?', (device,)).fetchone()[0]

    def open(self, candidate='zenbook', action='recover'):
        st = self.port.status()
        return self.clients['zenbook'].transition_request('test-'+str(st['epoch']), action, candidate,
            st['incarnation'], st['epoch'], st['policy_version'], 'control')['transition_id']

    def seat_zenbook(self):
        tid = self.open()
        self.call('zenbook', self.payload('readiness'))
        self.port.transition_step(tid, 'drain')
        for node in self.participants:
            self.call(node, self.payload('fence')) if node == 'zenbook' else self.participants[node].guard.fence(
                self.store.status()['incarnation'], self.store.status()['epoch'], self.store.status()['mode_version'])
        self.port.transition_step(tid, 'fence')
        lease = self.port.transition_step(tid, 'grant')
        self.call('zenbook', self.payload('accept', lease=lease))
        self.port.transition_step(tid, 'complete', True)  # fixture's initial ready lease
        self.participants['zenbook'].runtime.start()
        return lease

    def test_candidate_signs_readiness_with_its_own_key_and_refuses_source_key(self):
        self.seat_zenbook()
        tid = self.open('dell', 'set_preferred_main')
        remote = rpc.RemoteRuntime('dell', self.port, self.call)
        port = rpc.CandidatePort(self.port, remote)
        before = len(self.clients['zenbook'].sent)
        port.transition_readiness(tid, {'control_ready': True, 'ai_ready': True})
        owner_new = self.clients['zenbook'].sent[before:]
        self.assertFalse(any(path.endswith('/readiness') for path, _ in owner_new))
        signed = next(obj for path, obj in reversed(self.clients['dell'].sent) if path.endswith('/readiness'))
        self.assertEqual(signed['device']['device_id'], 'dell')
        self.assertEqual(signed['device']['installation_id'], 'dell-inst')
        self.assertFalse(signed['payload']['ai_ready'])
        with self.assertRaises(g.GatewayError):
            self.clients['zenbook'].readiness(tid, True, False)

    def test_uncertain_handoff_stops_remote_candidate_renewing_the_grant(self):
        from jarvis.continuity.runtime import SafeHandoff
        self.seat_zenbook()
        remotes = {device: rpc.RemoteRuntime(device, self.port, self.call)
                   for device in self.participants}
        candidate = remotes['dell']
        port = rpc.CandidatePort(self.port, candidate, remotes.values())

        def failed_brain(*_args):
            raise OSError('lost reply after starting transfer')

        handoff = SafeHandoff(port, source_runtime=remotes['zenbook'], candidate_runtime=candidate,
            readiness=lambda _: candidate.readiness(), repoint=lambda *_: self.fail('repoint after uncertainty'),
            canary=lambda _: self.fail('canary after uncertainty'), brain=failed_brain,
            journal_path=self.states['zenbook'] / 'uncertain-handoff.json')
        result = handoff('zenbook', 'dell', {}, self.states['zenbook'].parent)
        self.assertEqual(result['status'], 'uncertain', result)
        self.assertFalse(self.port.status()['main_ready'])
        before = len(self.clients['dell'].sent)
        self.assertEqual(self.participants['dell'].runtime.renew_once()['code'], 'renewal_paused')
        self.assertEqual(len(self.clients['dell'].sent), before)
        self.requester = 'dell'
        with self.assertRaisesRegex(RuntimeRefusal, 'forbidden'):
            candidate.pause_renewal()
        self.clock.tick(30)
        self.assertFalse(self.port.status()['lease_valid'])

    def test_source_drain_release_fence_and_candidate_lease_acceptance(self):
        self.seat_zenbook()
        tid = self.open('dell', 'set_preferred_main')
        source = rpc.RemoteRuntime('zenbook', self.port, self.call)
        candidate = rpc.RemoteRuntime('dell', self.port, self.call)
        port = rpc.CandidatePort(self.port, candidate, (source, candidate))
        port.transition_readiness(tid, candidate.readiness())
        port.transition_step(tid, 'drain')
        self.assertTrue(source.begin_drain(tid)['admission_stopped'])
        self.assertTrue(source.release_for_handoff(tid)['released'])
        port.transition_step(tid, 'fence')
        lease = port.transition_step(tid, 'grant')
        candidate.accept_lease(lease)
        self.assertEqual(candidate.lease_snapshot()['lease_id'], lease['lease_id'])
        for p in self.participants.values():
            self.assertTrue(p.guard.local_view()['legacy_closed'])
        self.assertFalse(self.store.status()['main_ready'])

    def test_not_ready_candidate_does_not_drain_or_promote(self):
        self.seat_zenbook(); self.ready['dell'] = False
        tid = self.open('dell', 'set_preferred_main')
        candidate = rpc.RemoteRuntime('dell', self.port, self.call)
        port = rpc.CandidatePort(self.port, candidate)
        port.transition_readiness(tid, {'control_ready': True, 'ai_ready': True})
        with self.assertRaises(AuthorityError): port.transition_step(tid, 'drain')
        self.assertEqual(self.store.status()['main'], 'zenbook')
        self.assertTrue(self.store.status()['lease_valid'])

    def test_stale_wrong_transition_requester_role_and_payload_are_refused(self):
        self.seat_zenbook(); self.open('dell', 'set_preferred_main')
        valid = self.payload('readiness')
        variants = [dict(valid, epoch=valid['epoch']+1), dict(valid, transition_id='other'),
                    dict(valid, control_ready=True), dict(valid, action='release')]
        for payload in variants:
            with self.assertRaises(RuntimeRefusal): self.call('dell', payload)
        with self.assertRaises(RuntimeRefusal):
            self.participants['dell'].execute(valid, requester='stranger')
        self.clock.tick(100)
        with self.assertRaises(RuntimeRefusal):
            self.call('dell', self.payload('accept', lease={}))

    def test_replay_after_cancel_is_refused(self):
        self.seat_zenbook(); tid = self.open('dell', 'set_preferred_main')
        payload = self.payload('readiness')
        self.port.transition_step(tid, 'cancel')
        with self.assertRaises(RuntimeRefusal): self.call('dell', payload)

    def test_loader_uses_only_local_config_and_never_calls_gateway(self):
        cfg = {'cluster': {'enabled': True, 'local_device': 'dell', 'main_device': 'zenbook'},
               'devices': {'dell': {}, 'zenbook': {}},
               'continuity': {'operator_config': 'operator.json'}}
        calls = []
        factory = lambda path: calls.append(path) or (self.clients['dell'], {})
        old = len(self.clients['dell'].sent)
        p = rpc.load_local(cfg, self.states['dell'], probe=lambda: {'control_ready': False}, client_factory=factory)
        self.assertEqual(calls, [self.states['dell'].parent/'operator.json'])
        self.assertEqual(len(self.clients['dell'].sent), old)
        self.assertEqual(p.device_id, 'dell')
        with self.assertRaises(RuntimeRefusal):
            rpc.load_local(cfg, self.states['dell'], probe=lambda: {},
                           client_factory=lambda _: (self.clients['zenbook'], {}))

    def test_node_worker_verifies_signed_rpc_and_preserves_ordinary_fence(self):
        from types import SimpleNamespace
        from jarvis import cluster, deploy
        from jarvis.cluster_store import Broker
        from jarvis.continuity import integration
        self.seat_zenbook(); self.open('dell', 'set_preferred_main')
        state = self.states['dell']; source = self.states['zenbook']
        key = deploy.code_key(source)
        (state/'cluster-node.json').write_text(json.dumps({'node_id': 'dell', 'code_key': key}))
        broker = Broker(source)
        def request(path, body=None, **kw):
            if path == '/v1/poll':
                job = broker.claim('dell', {rpc.KIND, 'desktop'})
                return {'job': deploy.sign_job(job, 'dell', source) if job else None, 'code': 'test-code'}
            self.assertEqual(path, '/v1/result')
            broker.finish(body['id'], 'dell', body['result'], body['failed'])
            return {'accepted': True}
        cfg = {'cluster': {'enabled': True, 'local_device': 'dell', 'main_device': 'zenbook'},
               'devices': {'dell': {}, 'zenbook': {}}}
        client = SimpleNamespace(main='zenbook', request=request, token='fake-token')
        with patch('jarvis.cluster.Client', return_value=client):
            worker = cluster.NodeWorker(cfg, state, executor=lambda *a: self.fail('ordinary executor must not run'))
        worker.code = 'test-code'; worker._confirmed = True
        integration.configure_production(state, self.participants['dell'].runtime)
        self.addCleanup(integration.clear_production, state)
        with patch('jarvis.hostmeter.cached_sample', return_value={}), patch.object(worker, '_speaks', return_value='no'):
            job = broker.enqueue('dell', 'zenbook', rpc.KIND, self.payload('readiness'))
            self.assertTrue(worker.once())
            self.assertEqual(broker.get(job)['state'], 'done')
            ordinary = broker.enqueue('dell', 'zenbook', 'desktop', {'action': 'open_app', 'arg': 'fake'})
            self.assertTrue(worker.once())
            self.assertEqual(broker.get(ordinary)['state'], 'failed')
            self.assertIn('admission', broker.get(ordinary)['result']['error'])
        # Continuity payloads, including grant ids and chunks, stay out of Broker SQLite.
        with broker.db() as db:
            self.assertEqual(db.execute('select payload from jobs where id=?', (job,)).fetchone()[0], '{}')

    def test_scoped_brain_transfer_after_drain_and_expiry_refusal(self):
        import contextlib
        import sqlite3
        import hashlib
        from jarvis import brainsync, transfer
        from jarvis.continuity.brain_channel import TransitionBundleTransport
        self.seat_zenbook(); tid = self.open('dell', 'set_preferred_main')
        remotes = {d: rpc.RemoteRuntime(d, self.port, self.call) for d in self.participants}
        port = rpc.CandidatePort(self.port, remotes['dell'], remotes.values())
        port.transition_readiness(tid, remotes['dell'].readiness())
        port.transition_step(tid, 'drain')
        remotes['zenbook'].begin_drain(tid); remotes['zenbook'].release_for_handoff(tid)
        port.transition_step(tid, 'fence')
        lease = port.transition_step(tid, 'grant'); remotes['dell'].accept_lease(lease)
        configs = {}
        devices = {d: {'brain_transfer_dir': str(self.root/(d+'-stage')), 'os': 'windows' if d=='dell' else 'linux'}
                   for d in self.participants}
        for d, p in self.participants.items():
            base = self.states[d].parent
            vault = base/'vault'; vault.mkdir()
            Path(devices[d]['brain_transfer_dir']).mkdir(mode=0o700)
            configs[d] = {'cluster': {'enabled': True, 'local_device': d, 'main_device': 'zenbook'},
                          'devices': devices, 'brainsync': {'enabled': True},
                          'vault': {'enabled': True, 'path': str(vault)}}
            p.config, p.state = configs[d], self.states[d]
        (self.root/'zenbook/vault/note.md').write_text('latest note')
        with contextlib.closing(sqlite3.connect(self.states['zenbook']/'memory.sqlite3')) as db, db:
            db.execute('create table items(body text)')
            db.execute('insert into items values (?)', ('private sqlite bytes',))
        def within(config, path):
            d = config['cluster']['local_device']
            root = Path(devices[d]['brain_transfer_dir']); target = Path(path).resolve()
            if target != root and root not in target.parents: raise ValueError('outside staging')
            return {'path': str(target), 'dir': target.is_dir(), 'size': target.stat().st_size}
        adapter = TransitionBundleTransport(configs['zenbook'], self.states['zenbook'].parent, remotes.__getitem__)
        with patch('jarvis.transfer._within', side_effect=within), patch('jarvis.deploy._remote', side_effect=AssertionError('no SSH')):
            result = brainsync.handoff('zenbook', 'dell', configs['zenbook'], self.states['zenbook'].parent, transport=adapter)
            self.assertTrue(result['ok'], result)
            self.assertEqual((self.root/'dell/vault/note.md').read_text(), 'latest note')
            with contextlib.closing(sqlite3.connect(self.states['dell']/'memory.sqlite3')) as db, db:
                self.assertEqual(db.execute('select body from items').fetchone()[0], 'private sqlite bytes')
            operation = {'action': 'brain_stage', 'path': devices['dell']['brain_transfer_dir']}
            self.clock.tick(100)
            with self.assertRaisesRegex(RuntimeRefusal, 'expired'):
                remotes['dell']._request('brain', operation=operation)
        self.assertFalse(self.store.status()['main_ready'])

    def test_scoped_brain_refuses_arbitrary_file_writes_before_execution(self):
        self.seat_zenbook(); tid = self.open('dell', 'set_preferred_main')
        remotes = {d: rpc.RemoteRuntime(d, self.port, self.call) for d in self.participants}
        port = rpc.CandidatePort(self.port, remotes['dell'], remotes.values())
        port.transition_readiness(tid, remotes['dell'].readiness()); port.transition_step(tid, 'drain')
        remotes['zenbook'].begin_drain(tid); remotes['zenbook'].release_for_handoff(tid)
        port.transition_step(tid, 'fence'); remotes['dell'].accept_lease(port.transition_step(tid, 'grant'))
        self.participants['dell'].config = {'devices': {'dell': {'brain_transfer_dir': '/fake-stage'}}}
        self.participants['dell'].state = self.states['dell']
        operations = [{'action': 'inbox'}, {'action': 'brain_stage', 'path': '/elsewhere'},
                      {'action': 'get', 'path': '/fake-stage', 'rel': ['memory.sqlite3'], 'offset': 0},
                      {'action': 'brain_freeze', 'request': '{}'}]
        with patch('jarvis.transfer.run_here', side_effect=AssertionError('must refuse before effect')):
            for operation in operations:
                with self.assertRaises(RuntimeRefusal):
                    remotes['dell']._request('brain', operation=operation)

    def recover(self, *, healthy=True, key=None):
        from jarvis.continuity.recovery import ManualRecovery
        peers = {d: rpc.RemoteRuntime(d, self.port, self.call) for d in self.participants}
        candidate = peers['zenbook']
        self.participants['zenbook'].canary = lambda: healthy
        canary = rpc.SignedCanary(candidate, key or _keys()['zenbook'][0], 'zenbook-inst')
        return ManualRecovery(rpc.CandidatePort(self.port, candidate, peers.values()), candidate, canary)

    def test_manual_recovery_has_real_owner_steps_installation_canary_and_no_leaked_lease(self):
        result = self.recover()('zenbook', 'manual-1')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['main'], 'zenbook')
        self.assertNotIn('lease_id', json.dumps(result))
        self.assertEqual(self.store.status()['mode'], 'V2_ACTIVE')
        self.assertTrue(self.participants['zenbook'].runtime.lease_snapshot())

    def test_manual_recovery_bad_or_wrongly_signed_canary_rolls_back_to_no_main(self):
        for healthy, key in ((False, None), (True, _keys()['dell'][0])):
            result = self.recover(healthy=healthy, key=key)('zenbook', 'manual-'+str(self.store.status()['epoch']))
            self.assertFalse(result['ok'])
            self.assertEqual(self.store.status()['mode'], 'V2_ONLY_NO_MAIN')
            self.assertFalse(self.store.status()['lease_valid'])

    def test_manual_recovery_not_ready_cancels_without_promotion(self):
        self.ready['zenbook'] = False
        with self.assertRaises(AuthorityError): self.recover()('zenbook', 'manual-1')
        self.assertFalse(self.store.status()['lease_valid'])
        self.assertIsNone(self.store.status()['transition'])

    def test_manual_recovery_live_main_is_never_forced(self):
        self.seat_zenbook()
        with self.assertRaisesRegex(RuntimeRefusal, 'main_alive'):
            self.recover()('zenbook', 'manual-2')
        self.assertTrue(self.store.status()['lease_valid'])

    def test_signed_canary_rejects_nonce_replay(self):
        tid = self.open(); self.call('zenbook', self.payload('readiness'))
        self.port.transition_step(tid, 'drain')
        for p in self.participants.values():
            st = self.port.status(); p.guard.fence(st['incarnation'], st['epoch'], st['mode_version'])
        self.port.transition_step(tid, 'fence')
        lease = self.port.transition_step(tid, 'grant')
        self.call('zenbook', self.payload('accept', lease=lease))
        self.participants['zenbook'].canary = lambda: True
        remote = rpc.RemoteRuntime('zenbook', self.port, self.call)
        signed = remote._request('canary', nonce='old-nonce')
        original = remote._request
        remote._request = lambda action, **kw: signed if action == 'canary' else original(action, **kw)
        canary = rpc.SignedCanary(remote, _keys()['zenbook'][0], 'zenbook-inst')
        self.assertFalse(canary('zenbook'))

    def test_manual_recovery_cli_uses_hub_process_and_failure_exit_code(self):
        from types import SimpleNamespace
        from jarvis.continuity import cli, hub_control
        import argparse
        parser = argparse.ArgumentParser(); cli.add_arguments(parser)
        args = parser.parse_args(['--config', 'operator.json', '--yes', 'recover',
            '--hub-config', str(self.root/'config.json'), '--state', str(self.states['zenbook']),
            '--request-id', 'initial-main'])
        (self.root/'config.json').write_text(json.dumps({'cluster': {
            'enabled': True, 'local_device': 'zenbook', 'main_device': 'zenbook'},
            'devices': {'zenbook': {}, 'dell': {}}}))
        before = len(self.clients['zenbook'].sent)
        for ready in (False, True):
            with patch.object(hub_control, 'request_recover', return_value={'ok': ready}) as request:
                code = cli.run(args, client_factory=lambda _: (self.clients['zenbook'], {'device_id': 'zenbook'}), output=lambda _: None)
            self.assertEqual(code, int(not ready))
            self.assertEqual(request.call_args.args[2:], ('zenbook', 'initial-main'))
        self.assertEqual(len(self.clients['zenbook'].sent), before)
        hub = SimpleNamespace(local='zenbook')
        with self.assertRaisesRegex(ValueError, 'not bound'):
            hub_control.run_recover(hub, {'device': 'zenbook', 'request_id': 'initial-main'}, '127.0.0.1')
        with self.assertRaises(PermissionError):
            hub_control.run_recover(hub, {'device': 'zenbook', 'request_id': 'initial-main'}, '192.0.2.1')

    def test_signed_fence_ack_is_idempotent_and_does_not_cover_the_new_epoch(self):
        self.seat_zenbook()
        tid = self.open('dell', 'set_preferred_main')
        remotes = {d: rpc.RemoteRuntime(d, self.port, self.call) for d in self.participants}
        port = rpc.CandidatePort(self.port, remotes['dell'], tuple(remotes.values()))
        port.transition_readiness(tid, remotes['dell'].readiness())
        port.transition_step(tid, 'drain')
        remotes['zenbook'].begin_drain(tid)
        remotes['zenbook'].release_for_handoff(tid)
        closed = self.port.status()['epoch']
        start = {d: len(self.clients[d].sent) for d in self.participants}
        first = {d: self.call(d, self.payload('fence')) for d in self.participants}
        for device, signed in first.items():
            self.assertEqual(self.ack_epochs(device, start[device]), [closed])
            self.assertEqual(signed['fenced_epoch'], closed)
            self.assertEqual(signed['receipt']['epoch'], closed)
            self.assertEqual(signed['receipt']['transition_id'], tid)
            self.assertTrue(g.verify_message(
                g.RUNTIME_NS, g._message(g.RUNTIME_NS, signed['receipt']),
                signed['signature'], _keys()[device][0]))
            other = 'zenbook' if device == 'dell' else 'dell'
            self.assertFalse(g.verify_message(
                g.RUNTIME_NS, g._message(g.RUNTIME_NS, signed['receipt']),
                signed['signature'], _keys()[other][0]))
            again = self.call(device, self.payload('fence'))
            self.assertEqual(again['signature'], signed['signature'])
            self.assertEqual(again['receipt'], signed['receipt'])
            self.assertEqual(self.ack_epochs(device, start[device]), [closed])
        self.port.transition_step(tid, 'fence')
        opened = self.port.status()['epoch']
        self.assertEqual(opened, closed + 1)
        self.assertEqual(self.port.status()['transition']['state'], 'FENCING')
        for device in self.participants:
            self.participants[device]._fence_acks.clear()
            replay = self.call(device, self.payload('fence'))
            self.assertEqual(replay['receipt']['epoch'], closed)
            self.assertEqual(replay['fenced_epoch'], closed)
            self.assertNotIn(opened, self.ack_epochs(device, start[device]))
            self.assertEqual(self.stored_epoch(device), closed)
            self.assertTrue(g.verify_message(
                g.RUNTIME_NS, g._message(g.RUNTIME_NS, replay['receipt']),
                replay['signature'], _keys()[device][0]))
        # Authority fence replay is idempotent once FENCING is committed. The
        # peer acknowledgements above remain bound to the closed epoch; replay
        # returns the same transition without advancing either epoch or acks.
        replayed_fence = port.transition_step(tid, 'fence')
        self.assertEqual(replayed_fence['state'], 'FENCING')
        self.assertEqual(self.port.status()['epoch'], opened)
        for device in self.participants:
            self.assertEqual(self.stored_epoch(device), closed)
            self.assertNotIn(opened, self.ack_epochs(device, start[device]))

    def test_fence_role_phase_and_guard_failure_do_not_acknowledge(self):
        self.seat_zenbook()
        tid = self.open('dell', 'set_preferred_main')
        self.call('dell', self.payload('readiness'))
        before = len(self.clients['dell'].sent)
        with self.assertRaisesRegex(RuntimeRefusal, 'bad_state'):
            self.call('dell', self.payload('fence'))
        self.assertEqual(self.ack_epochs('dell', before), [])
        self.port.transition_step(tid, 'drain')
        before = len(self.clients['dell'].sent)
        with self.assertRaisesRegex(RuntimeRefusal, 'candidacy_mismatch'):
            self.call('dell', self.payload('release'))
        with self.assertRaisesRegex(RuntimeRefusal, 'candidacy_mismatch'):
            self.call('dell', self.payload('drain'))
        with self.assertRaisesRegex(RuntimeRefusal, 'forbidden'):
            self.participants['dell'].execute(self.payload('fence'), requester='dell')

        def refuse_ack(*args, **kwargs):
            raise AuthorityError('revoked', 'disabled installation cannot acknowledge')

        original = self.participants['dell'].guard.fence
        self.participants['dell'].guard.fence = refuse_ack
        try:
            with self.assertRaisesRegex(RuntimeRefusal, 'revoked'):
                self.call('dell', self.payload('fence'))
        finally:
            self.participants['dell'].guard.fence = original
        self.assertEqual(self.ack_epochs('dell', before), [])
        closed = self.port.status()['epoch']
        for device in self.participants:
            self.call(device, self.payload('fence'))
        self.port.transition_step(tid, 'fence')
        lease = self.port.transition_step(tid, 'grant')
        self.assertEqual(self.port.status()['epoch'], closed + 1)
        marks = {d: len(self.clients[d].sent) for d in self.participants}
        with self.assertRaisesRegex(RuntimeRefusal, 'candidacy_mismatch'):
            self.call('zenbook', self.payload('accept', lease=lease))
        with self.assertRaisesRegex(RuntimeRefusal, 'bad_state'):
            self.call('dell', self.payload('fence'))
        self.assertEqual(self.ack_epochs('dell', marks['dell']), [])
        self.assertEqual(self.ack_epochs('zenbook', marks['zenbook']), [])
        self.assertEqual(self.stored_epoch('dell'), closed)
        self.assertNotEqual(self.stored_epoch('dell'), self.port.status()['epoch'])

    def test_mismatched_fence_receipt_does_not_advance_the_epoch(self):
        self.seat_zenbook()
        tid = self.open('dell', 'set_preferred_main')
        remotes = {d: rpc.RemoteRuntime(d, self.port, self.call) for d in self.participants}
        port = rpc.CandidatePort(self.port, remotes['dell'], tuple(remotes.values()))
        port.transition_readiness(tid, remotes['dell'].readiness())
        port.transition_step(tid, 'drain')
        remotes['zenbook'].begin_drain(tid)
        remotes['zenbook'].release_for_handoff(tid)
        epoch = self.port.status()['epoch']
        real = remotes['dell']._request

        def lie(action, **kwargs):
            result = real(action, **kwargs)
            if action == 'fence':
                result = copy.deepcopy(result)
                result['receipt']['epoch'] = epoch + 1
            return result

        remotes['dell']._request = lie
        with self.assertRaisesRegex(RuntimeRefusal, 'stale'):
            port.transition_step(tid, 'fence')
        self.assertEqual(self.port.status()['epoch'], epoch)
        self.assertEqual(self.port.status()['transition']['state'], 'DRAINING')
        self.assertEqual(self.port.status()['transition']['transition_id'], tid)

    def test_fence_refuses_unbound_legacy_entry_points_before_ack(self):
        from jarvis.continuity.integration import clear_production
        self.open()
        self.call('zenbook', self.payload('readiness'))
        tid = self.port.status()['transition']['transition_id']
        self.port.transition_step(tid, 'drain')
        clear_production(self.states['dell'])
        before = len(self.clients['dell'].sent)
        with self.assertRaisesRegex(RuntimeRefusal, 'managed_unbound'):
            self.call('dell', self.payload('fence'))
        self.assertFalse(any(path.endswith('/acknowledge') for path, obj in self.clients['dell'].sent[before:]))

    def test_missing_canary_implementation_refuses_instead_of_succeeding(self):
        from jarvis.continuity.recovery import ManualRecovery
        remotes = {d: rpc.RemoteRuntime(d, self.port, self.call) for d in self.participants}
        candidate = remotes['zenbook']
        canary = rpc.SignedCanary(candidate, _keys()['zenbook'][0], 'zenbook-inst')
        result = ManualRecovery(rpc.CandidatePort(self.port, candidate, remotes.values()), candidate, canary)('zenbook', 'no-canary')
        self.assertFalse(result['ok'])
        self.assertFalse(self.store.status()['lease_valid'])

    def test_brain_transport_cannot_follow_a_later_transition(self):
        from types import SimpleNamespace
        from jarvis.continuity.brain_channel import TransitionBundleTransport
        status = {'transition': {'transition_id': 'old-transition', 'state': 'GRANTED_NOT_READY'},
                  'incarnation': 1, 'epoch': 3, 'lease_valid': True, 'main_ready': False}
        calls = []
        peer = SimpleNamespace(port=SimpleNamespace(status=lambda: status),
            _request=lambda action, **kw: calls.append(kw['_scope']) or {'ok': True})
        cfg = {'cluster': {'enabled': True, 'local_device': 'zenbook', 'main_device': 'zenbook'},
               'devices': {'zenbook': {}, 'dell': {}}}
        transport = TransitionBundleTransport(cfg, self.root, lambda node: peer)
        status['transition']['transition_id'] = 'next-transition'; status['epoch'] = 5
        transport._call('dell', {'action': 'brain_stage', 'path': '/stage'})
        self.assertEqual(calls, [{'transition_id': 'old-transition', 'incarnation': 1, 'epoch': 3}])
