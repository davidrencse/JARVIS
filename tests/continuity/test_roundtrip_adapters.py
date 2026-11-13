"""Two PC handoffs using actual adapters, fake wire/process/service boundaries."""
import base64
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import brainsync
from jarvis.continuity import participant as rpc, repoint, lifecycle, return_brain
from jarvis.continuity.brain_channel import TransitionBundleTransport
from jarvis.continuity.executor import ExecutorGuard
from jarvis.continuity.integration import configure_production, production_issuer
from jarvis.continuity.runtime import MainRuntime, GatewayPort, SafeHandoff, RuntimeRefusal
from jarvis.cluster_store import Broker
from tests.continuity import test_participant_channel as fixtures
from tests.continuity.test_gateway import _keys


class RoundTripAdapters(unittest.TestCase):
    def setUp(self):
        self.e = fixtures.ParticipantChannelTests(); self.e.setUp(); self.addCleanup(self.e.doCleanups)
        e = self.e; self.healthy = {d: True for d in e.participants}; self.bad_canary = None
        self.pins = {d: {'installation_id': d+'-inst', 'public_key': _keys()[d][0]} for d in e.participants}
        devices = {d: {'brain_transfer_dir': str(e.root/(d+'-stage')), 'os': 'windows' if d=='dell' else 'linux'} for d in e.participants}
        self.configs = {}; self.hubs = {}
        for d, p in e.participants.items():
            state = e.states[d]; vault = state.parent/'vault'; vault.mkdir()
            Path(devices[d]['brain_transfer_dir']).mkdir(mode=0o700)
            cfg = {'cluster': {'enabled': True, 'local_device': d, 'main_device': 'zenbook'}, 'devices': devices,
                   'vault': {'enabled': True, 'path': str(vault)}, 'brainsync': {'enabled': True},
                   'continuity': {'operator_config': 'operator.json'}}
            self.configs[d] = cfg; p.config = cfg; p.state = state
            (state.parent/'operator.json').write_text(json.dumps({'origin': 'http://127.0.0.1:1',
                'device_id': d, 'installation_id': d+'-inst', 'authority_id': 'oracle-auth',
                'private_key_path': str(_keys()[d][1]), 'enrollment': self.pins}))
            p.runtime._probe = lambda d=d: {'control_ready': self.healthy[d]}
            self.hubs[d] = SimpleNamespace(state=state, main=d, started=False,
                continuity_endpoint='https://'+d+'.example.ts.net:443')
        with sqlite3.connect(e.states['zenbook']/'memory.sqlite3') as db:
            db.execute('create table items(body text)'); db.execute("insert into items values ('first brain')")
        (e.root/'zenbook/vault/note.md').write_text('first note')
        e.seat_zenbook()
        self.hubs['zenbook'].started = True
        self.switches = []

    def within(self, config, path):
        device = config['cluster']['local_device']
        root = Path(config['devices'][device]['brain_transfer_dir']); target = Path(path).resolve()
        if target != root and root not in target.parents: raise ValueError('outside staging')
        return {'path': str(target), 'dir': target.is_dir(), 'size': target.stat().st_size}

    def new_process(self, device):
        e = self.e; old = e.participants[device]; state = e.states[device]
        runtime = MainRuntime(GatewayPort(old.client), device, device+'-inst',
            lambda: {'control_ready': self.healthy[device]}, clock=e.clock.mono, boot=lambda: 'test-boot')
        guard = ExecutorGuard(state/'executor.sqlite', device_id=device, installation_id=device+'-inst',
            fleet_id='home', authority_id='oracle-auth', authority=GatewayPort(old.client),
            boot=lambda: 'test-boot', wall=e.clock.wall, verifier=old.guard._verifier)
        self.addCleanup(guard.close)
        part = rpc.Participant(old.client, runtime, guard=guard, config=self.configs[device], state=state,
            canary=lambda: self.hubs[device].started and device != self.bad_canary and self.quick_check(device))
        e.participants[device] = part; rpc.bind(state, part)
        configure_production(state, runtime, executor=guard,
            issuer=production_issuer(_keys()[device][1], old.client, wall=e.clock.wall))
        self.assertTrue(lifecycle.accept_successor_grant(part, state, self.pins))
        self.hubs[device].started = True
        return part

    def quick_check(self, device):
        with sqlite3.connect(self.e.states[device]/'memory.sqlite3') as db:
            return db.execute('pragma quick_check').fetchone()[0] == 'ok'

    def handoff(self, old, new):
        e = self.e; e.requester = old
        peers = {d: rpc.RemoteRuntime(d, e.port, e.call) for d in e.participants}
        port = rpc.CandidatePort(e.port, peers[new], peers.values())
        def brain(source, target, config, base):
            return brainsync.handoff(source, target, config, base,
                transport=TransitionBundleTransport(config, base, peers.__getitem__))
        def repoint_hook(source, target, lease):
            if (e.states[target]/return_brain.FILE).exists():
                self.assertTrue(peers[target]._request('activate_return')['ok'])
            p = e.participants[target]
            issuer = repoint.Issuer(p, e.states[target], Broker(e.states[target]))
            self_offer = issuer.issue(target, self.hubs[target].continuity_endpoint)
            role = lifecycle.RoleLifecycle(p, e.states[target].parent/'jarvis.config.json', e.states[target], self.pins, startup_ready=lambda: True)
            self.assertTrue(role.prepare_successor(self_offer)['scheduled'])
            self.new_process(target)  # faked process boundary, real grant consumption
            source_offer = issuer.issue(source, self.hubs[target].continuity_endpoint)
            source_role = lifecycle.RoleLifecycle(e.participants[source], e.states[source].parent/'jarvis.config.json',
                e.states[source], self.pins, startup_ready=lambda: True, probe=lambda body, nonce: repoint.endpoint_receipt(self.hubs[target], body['target'], nonce))
            result = source_role.switch_node(source_offer)
            self.switches.append((source, target))
            return result
        coordinator = SafeHandoff(port, source_runtime=peers[old], candidate_runtime=peers[new],
            readiness=lambda device: peers[device].readiness(), repoint=repoint_hook,
            canary=rpc.SignedCanary(peers[new], _keys()[new][0], new+'-inst'), brain=brain,
            journal_path=e.states[old]/('drill-'+old+'-to-'+new+'.json'))
        with patch('jarvis.transfer._within', side_effect=self.within), \
                patch('jarvis.cluster_tunnel.status', return_value={'running': False}), \
                patch('jarvis.cluster_tunnel.autostart'), patch('jarvis.cluster_tunnel.start'), \
                patch('jarvis.cluster_setup.role_autostart', return_value={'autostart': True}), \
                patch('jarvis.cluster_setup._launch_agent'), patch('jarvis.autostart.system', return_value='windows'), \
                patch('jarvis.deploy._remote', side_effect=AssertionError('no SSH')):
            return coordinator(old, new, self.configs[old], e.states[old].parent)

    def approval(self, device):
        return json.loads((self.e.states[device]/return_brain.FILE).read_text())

    def stage_return(self, source, target, **changes):
        e = self.e
        request = json.dumps({'schema': brainsync.EXPORT_SCHEMA, 'old_main': source, 'new_main': target}).encode()
        exported = brainsync.export_result(self.configs[source], e.states[source].parent, request)
        self.assertTrue(exported['ok'], exported)
        blob = base64.b64decode(exported['payload']); _, _, memory = brainsync._unbundle(blob)
        path = Path(self.configs[target]['devices'][target]['brain_transfer_dir'])/'return-proposal.bundle'
        path.write_bytes(blob)
        current = brainsync.snapshot_memory(e.states[target]/'memory.sqlite3')
        freeze = brainsync._marker_path(e.states[target].parent).read_bytes()
        hashes = {'incoming_sha256': hashlib.sha256(memory).hexdigest(),
                  'current_sha256': hashlib.sha256(current).hexdigest(),
                  'freeze_sha256': hashlib.sha256(freeze).hexdigest()}
        confirmed = changes.pop('confirmed', True)
        hashes.update(changes)
        with patch('jarvis.transfer._within', side_effect=self.within):
            result = return_brain.prepare(self.configs[target], e.states[target], e.clients[target],
                                         path, **hashes, confirmed=confirmed)
        self.assertTrue(result['freeze_preserved'])
        self.assertTrue(brainsync._frozen(e.states[target].parent, target))
        return path, hashes

    def prepare_return(self, **changes):
        with sqlite3.connect(self.e.states['dell']/'memory.sqlite3') as db:
            db.execute("insert into items values ('new Dell memory')")
        return self.stage_return('dell', 'zenbook', **changes)

    def test_zenbook_to_dell_to_zenbook_with_explicit_return_confirmation(self):
        e = self.e; first = self.handoff('zenbook', 'dell')
        self.assertTrue(first['ok'], first)
        self.assertEqual(e.store.status()['main'], 'dell')
        self.assertTrue(brainsync._frozen(e.states['zenbook'].parent, 'zenbook'))
        self.prepare_return()
        result = self.handoff('dell', 'zenbook')
        self.assertTrue(result['ok'], result)
        self.assertEqual(e.store.status()['main'], 'zenbook')
        self.assertFalse(brainsync._frozen(e.states['zenbook'].parent, 'zenbook'))
        self.assertTrue(brainsync._frozen(e.states['dell'].parent, 'dell'))
        with sqlite3.connect(e.states['zenbook']/'memory.sqlite3') as db:
            self.assertEqual(db.execute('select count(*) from items').fetchone()[0], 2)
        self.assertEqual(self.switches, [('zenbook', 'dell'), ('dell', 'zenbook')])
        with self.assertRaises(RuntimeRefusal): e.participants['dell'].runtime.require_authority('ordinary')
        self.assertTrue(e.participants['zenbook'].runtime.require_authority('ordinary')['ready'])

    def test_return_without_operator_disposition_preserves_memory_conflict_and_freeze(self):
        e = self.e; self.assertTrue(self.handoff('zenbook', 'dell')['ok'])
        with sqlite3.connect(e.states['dell']/'memory.sqlite3') as db: db.execute("insert into items values ('different')")
        before = brainsync.snapshot_memory(e.states['zenbook']/'memory.sqlite3')
        result = self.handoff('dell', 'zenbook')
        self.assertFalse(result['ok'])
        self.assertEqual(brainsync.snapshot_memory(e.states['zenbook']/'memory.sqlite3'), before)
        self.assertTrue(brainsync._frozen(e.states['zenbook'].parent, 'zenbook'))
        self.assertFalse(e.store.status()['lease_valid'])

    def test_failed_return_canary_refreezes_and_retains_incoming_memory(self):
        e = self.e; self.assertTrue(self.handoff('zenbook', 'dell')['ok'])
        self.prepare_return(); self.bad_canary = 'zenbook'
        result = self.handoff('dell', 'zenbook'); self.assertFalse(result['ok'])
        self.assertFalse(e.store.status()['lease_valid'])
        self.assertTrue(return_brain.refreeze(e.participants['zenbook'])['memory_preserved'])
        self.assertTrue(brainsync._frozen(e.states['zenbook'].parent, 'zenbook'))
        with sqlite3.connect(e.states['zenbook']/'memory.sqlite3') as db:
            self.assertEqual(db.execute('select count(*) from items').fetchone()[0], 2)

    def test_candidate_readiness_failure_keeps_existing_main(self):
        self.healthy['dell'] = False
        result = self.handoff('zenbook', 'dell')
        self.assertFalse(result['ok'])
        self.assertEqual(self.e.store.status()['main'], 'zenbook')
        self.assertTrue(self.e.store.status()['main_ready'])
        self.assertEqual(self.switches, [])

    def test_return_prepare_requires_explicit_confirmation_and_all_hashes(self):
        e = self.e; self.assertTrue(self.handoff('zenbook', 'dell')['ok'])
        before = brainsync.snapshot_memory(e.states['zenbook']/'memory.sqlite3')
        for wrong in ({'confirmed': False}, {'incoming_sha256': '0'*64},
                      {'current_sha256': '0'*64}, {'freeze_sha256': '0'*64}):
            with self.assertRaises(RuntimeRefusal): self.prepare_return(**wrong)
            self.assertEqual(brainsync.snapshot_memory(e.states['zenbook']/'memory.sqlite3'), before)
            self.assertTrue(brainsync._frozen(e.states['zenbook'].parent, 'zenbook'))
            self.assertFalse((e.states['zenbook']/return_brain.FILE).exists())

    def test_return_prepare_refuses_a_running_role(self):
        from jarvis.locking import exclusive_lock, LockBusy
        self.assertTrue(self.handoff('zenbook', 'dell')['ok'])
        with exclusive_lock(self.e.states['zenbook']/'cluster-node.lock'):
            with self.assertRaises(LockBusy): self.prepare_return()
        self.assertTrue((self.e.states['zenbook']/'memory.sqlite3').exists())

    def test_lease_renewal_keeps_accepted_main_and_does_not_recover_an_expired_lease(self):
        from jarvis.continuity.renewal import LeaseRenewer
        self.assertTrue(self.handoff('zenbook', 'dell')['ok'])
        renewer = LeaseRenewer(self.e.participants['dell'])
        for _ in range(12):
            self.e.clock.tick(3); self.assertEqual(renewer.tick()['code'], 'ok')
            self.assertTrue(self.e.store.status()['lease_valid'])
        self.e.clock.tick(100)
        self.assertNotEqual(renewer.tick()['code'], 'ok')
        self.assertFalse(self.e.store.status()['lease_valid'])
        self.assertFalse(self.e.store.status()['transition'])

    def test_return_approval_archive_path_is_not_trusted(self):
        self.assertTrue(self.handoff('zenbook', 'dell')['ok']); self.prepare_return()
        approval = self.e.states['zenbook']/return_brain.FILE
        record = json.loads(approval.read_text()); record['archive'] = '../../outside'
        approval.write_text(json.dumps(record))
        with self.assertRaises(RuntimeRefusal): return_brain.activate(self.e.participants['zenbook'])
        self.assertTrue(brainsync._frozen(self.e.states['zenbook'].parent, 'zenbook'))

    def test_two_outward_and_return_cycles_preserve_archives(self):
        e = self.e
        self.assertTrue(self.handoff('zenbook', 'dell')['ok'])
        self.prepare_return()
        first_archive = self.approval('zenbook')['archive']
        self.assertTrue(self.handoff('dell', 'zenbook')['ok'])
        approval = e.states['zenbook']/return_brain.FILE
        self.assertEqual(self.approval('zenbook')['phase'], 'activated')
        request = json.dumps({'schema': brainsync.EXPORT_SCHEMA, 'old_main': 'dell', 'new_main': 'zenbook'}).encode()
        exported = brainsync.export_result(self.configs['dell'], e.states['dell'].parent, request)
        self.assertTrue(exported['ok'], exported)
        blob = base64.b64decode(exported['payload']); _, _, memory = brainsync._unbundle(blob)
        bundle = Path(self.configs['zenbook']['devices']['zenbook']['brain_transfer_dir'])/'still-main.bundle'
        bundle.write_bytes(blob)
        with patch('jarvis.transfer._within', side_effect=self.within):
            with self.assertRaises(RuntimeRefusal) as alive:
                return_brain.prepare(self.configs['zenbook'], e.states['zenbook'], e.clients['zenbook'], bundle,
                    incoming_sha256=hashlib.sha256(memory).hexdigest(), current_sha256='0'*64,
                    freeze_sha256='0'*64, confirmed=True)
        self.assertEqual(alive.exception.code, 'main_alive')
        self.assertTrue(approval.is_file())
        self.assertFalse((e.states['zenbook']/first_archive/return_brain.FILE).exists())
        with sqlite3.connect(e.states['zenbook']/'memory.sqlite3') as db:
            db.execute("insert into items values ('second outward')")
        self.stage_return('zenbook', 'dell')
        dell_archive = self.approval('dell')['archive']
        dell_memory = (e.states['dell']/dell_archive/'memory.sqlite3').read_bytes()
        self.assertTrue(self.handoff('zenbook', 'dell')['ok'])
        self.assertEqual(e.store.status()['main'], 'dell')
        self.assertFalse(brainsync._frozen(e.states['dell'].parent, 'dell'))
        self.assertTrue(brainsync._frozen(e.states['zenbook'].parent, 'zenbook'))
        self.assertEqual((e.states['dell']/dell_archive/'memory.sqlite3').read_bytes(), dell_memory)
        with sqlite3.connect(e.states['dell']/'memory.sqlite3') as db:
            db.execute("insert into items values ('second return')")
        with self.assertRaises(RuntimeRefusal) as wrong:
            self.stage_return('dell', 'zenbook', incoming_sha256='0'*64)
        self.assertEqual(wrong.exception.code, 'conflict')
        self.assertFalse((e.states['zenbook']/first_archive/return_brain.FILE).exists())
        self.stage_return('dell', 'zenbook')
        parked = json.loads((e.states['zenbook']/first_archive/return_brain.FILE).read_text())
        self.assertEqual(parked['phase'], 'activated')
        second = self.approval('zenbook')
        self.assertEqual(second['phase'], 'prepared')
        self.assertNotEqual(second['archive'], first_archive)
        self.assertTrue(self.handoff('dell', 'zenbook')['ok'])
        self.assertEqual(e.store.status()['main'], 'zenbook')
        self.assertEqual(self.approval('zenbook')['phase'], 'activated')
        self.assertEqual(self.switches, [('zenbook', 'dell'), ('dell', 'zenbook'), ('zenbook', 'dell'), ('dell', 'zenbook')])
        with sqlite3.connect(e.states['zenbook']/'memory.sqlite3') as db:
            self.assertEqual([row[0] for row in db.execute('select body from items order by rowid')],
                             ['first brain', 'new Dell memory', 'second outward', 'second return'])
        with sqlite3.connect(e.states['zenbook']/first_archive/'memory.sqlite3') as db:
            self.assertEqual([row[0] for row in db.execute('select body from items')], ['first brain'])
        self.assertTrue((e.states['zenbook']/first_archive/'activated-freeze.json').is_file())
        with sqlite3.connect(e.states['zenbook']/second['archive']/'memory.sqlite3') as db:
            self.assertEqual(db.execute('select count(*) from items').fetchone()[0], 3)
        self.assertEqual((e.states['dell']/dell_archive/'memory.sqlite3').read_bytes(), dell_memory)

    def test_activate_crash_before_marker_move_checks_grant_and_hash(self):
        e = self.e
        self.assertTrue(self.handoff('zenbook', 'dell')['ok']); self.prepare_return()
        record = self.approval('zenbook')
        archive = e.states['zenbook']/record['archive']
        archived_memory = (archive/'memory.sqlite3').read_bytes()
        marker = brainsync._marker_path(e.states['zenbook'].parent)
        freeze = marker.read_bytes()
        real = return_brain._save_durable
        def crash(path, value):
            real(path, value)
            if value.get('phase') == 'activated':
                raise RuntimeError('crash before marker move')
        with patch.object(return_brain, '_save_durable', side_effect=crash):
            self.assertFalse(self.handoff('dell', 'zenbook')['ok'])
        saved = self.approval('zenbook')
        self.assertEqual(saved['phase'], 'activated')
        self.assertEqual(set(saved['grant']), {'transition_id', 'incarnation', 'epoch'})
        self.assertEqual(marker.read_bytes(), freeze)
        self.assertFalse((archive/'activated-freeze.json').exists())
        marker.chmod(0o644); marker.write_bytes(freeze + b'\n')
        with self.assertRaises(RuntimeRefusal) as conflict:
            return_brain.activate(e.participants['zenbook'])
        self.assertEqual(conflict.exception.code, 'conflict')
        self.assertFalse((archive/'activated-freeze.json').exists())
        marker.write_bytes(freeze); marker.chmod(0o444)
        approval = e.states['zenbook']/return_brain.FILE
        tampered = dict(saved); tampered['grant'] = dict(saved['grant'], epoch=saved['grant']['epoch']+1)
        approval.write_text(json.dumps(tampered))
        with self.assertRaises(RuntimeRefusal) as reconcile:
            return_brain.activate(e.participants['zenbook'])
        self.assertEqual(reconcile.exception.code, 'reconcile')
        self.assertEqual(marker.read_bytes(), freeze)
        approval.write_text(json.dumps(saved))
        self.assertEqual(return_brain.activate(e.participants['zenbook'])['status'], 'return_activated')
        self.assertFalse(marker.exists())
        self.assertEqual((archive/'activated-freeze.json').read_bytes(), freeze)
        self.assertEqual((archive/'memory.sqlite3').read_bytes(), archived_memory)
        self.assertEqual(return_brain.activate(e.participants['zenbook'])['status'], 'return_activated')

    def test_activate_finishes_when_the_freeze_moved_before_the_approval_write(self):
        e = self.e
        self.assertTrue(self.handoff('zenbook', 'dell')['ok']); self.prepare_return()
        record = self.approval('zenbook')
        archive = e.states['zenbook']/record['archive']
        memory = (archive/'memory.sqlite3').read_bytes()
        marker = brainsync._marker_path(e.states['zenbook'].parent)
        freeze = marker.read_bytes()
        def old_crash(participant):
            approval = json.loads((Path(participant.state)/return_brain.FILE).read_text())
            brainsync._marker_path(Path(participant.state).parent).rename(
                Path(participant.state)/approval['archive']/'activated-freeze.json')
            raise RuntimeError('crash after marker move')
        with patch.object(return_brain, 'activate', side_effect=old_crash):
            self.assertFalse(self.handoff('dell', 'zenbook')['ok'])
        self.assertEqual(self.approval('zenbook')['phase'], 'received')
        self.assertNotIn('grant', self.approval('zenbook'))
        self.assertFalse(marker.exists())
        archived = archive/'activated-freeze.json'
        self.assertEqual(archived.read_bytes(), freeze)
        archived.chmod(0o644)
        archived.write_bytes(freeze + b'x')
        with self.assertRaises(RuntimeRefusal) as conflict:
            return_brain.activate(e.participants['zenbook'])
        self.assertEqual(conflict.exception.code, 'conflict')
        self.assertNotIn('grant', self.approval('zenbook'))
        archived.write_bytes(freeze)
        self.assertEqual(return_brain.activate(e.participants['zenbook'])['status'], 'return_activated')
        self.assertEqual(self.approval('zenbook')['phase'], 'activated')
        self.assertFalse(marker.exists())
        self.assertEqual((archive/'memory.sqlite3').read_bytes(), memory)
        self.assertEqual(archived.read_bytes(), freeze)

    def test_refreeze_crash_points_retry_and_refrozen_cycle_keeps_the_archive(self):
        e = self.e
        self.assertTrue(self.handoff('zenbook', 'dell')['ok']); self.prepare_return()
        archive = e.states['zenbook']/self.approval('zenbook')['archive']
        original = (archive/'memory.sqlite3').read_bytes()
        self.bad_canary = 'zenbook'
        self.assertFalse(self.handoff('dell', 'zenbook')['ok'])
        self.bad_canary = None
        marker = brainsync._marker_path(e.states['zenbook'].parent)
        self.assertEqual(self.approval('zenbook')['phase'], 'activated')
        self.assertFalse(marker.exists())
        live = brainsync.snapshot_memory(e.states['zenbook']/'memory.sqlite3')
        real = return_brain._save_durable
        def crash_before(path, value):
            if value.get('phase') == 'refreezing':
                raise RuntimeError('crash before intent')
            real(path, value)
        with patch.object(return_brain, '_save_durable', side_effect=crash_before):
            with self.assertRaises(RuntimeError): return_brain.refreeze(e.participants['zenbook'])
        self.assertEqual(self.approval('zenbook')['phase'], 'activated')
        self.assertFalse(marker.exists())
        def crash_after_intent(path, value):
            real(path, value)
            if value.get('phase') == 'refreezing':
                raise RuntimeError('crash before restore')
        with patch.object(return_brain, '_save_durable', side_effect=crash_after_intent):
            with self.assertRaises(RuntimeError): return_brain.refreeze(e.participants['zenbook'])
        self.assertEqual(self.approval('zenbook')['phase'], 'refreezing')
        self.assertFalse(marker.exists())
        with patch('jarvis.brainsync._write_private', side_effect=OSError('crash during marker restore')):
            with self.assertRaises(OSError): return_brain.refreeze(e.participants['zenbook'])
        self.assertEqual(self.approval('zenbook')['phase'], 'refreezing')
        self.assertFalse(marker.exists())
        def crash_final(path, value):
            if value.get('phase') == 'refrozen':
                raise RuntimeError('crash after restore before approval write')
            real(path, value)
        with patch.object(return_brain, '_save_durable', side_effect=crash_final):
            with self.assertRaises(RuntimeError): return_brain.refreeze(e.participants['zenbook'])
        self.assertEqual(self.approval('zenbook')['phase'], 'refreezing')
        restored = marker.read_bytes()
        self.assertEqual(restored, (archive/'activated-freeze.json').read_bytes())
        self.assertTrue(return_brain.refreeze(e.participants['zenbook'])['memory_preserved'])
        self.assertEqual(self.approval('zenbook')['phase'], 'refrozen')
        self.assertEqual(marker.read_bytes(), restored)
        self.assertEqual(brainsync.snapshot_memory(e.states['zenbook']/'memory.sqlite3'), live)
        self.assertEqual((archive/'memory.sqlite3').read_bytes(), original)
        self.assertTrue(return_brain.refreeze(e.participants['zenbook'])['memory_preserved'])
        forged = self.approval('zenbook'); forged['phase'] = 'activated'
        (e.states['zenbook']/return_brain.FILE).write_text(json.dumps(forged))
        phases = []
        def watch(path, value):
            phases.append(value['phase']); real(path, value)
        with patch.object(return_brain, '_save_durable', side_effect=watch):
            self.assertTrue(return_brain.refreeze(e.participants['zenbook'])['memory_preserved'])
        self.assertEqual(phases, ['refrozen'])
        self.assertEqual(marker.read_bytes(), restored)
        self.stage_return('dell', 'zenbook')
        parked = json.loads((archive/return_brain.FILE).read_text())
        self.assertEqual(parked['phase'], 'refrozen')
        self.assertEqual((archive/'memory.sqlite3').read_bytes(), original)
        self.assertFalse((e.states['zenbook']/'memory.sqlite3').exists())
        self.assertEqual(self.approval('zenbook')['phase'], 'prepared')

    def test_prepare_failpoints_and_review_retire_preserve_memory(self):
        from jarvis.locking import exclusive_lock, LockBusy
        e = self.e
        self.assertTrue(self.handoff('zenbook', 'dell')['ok'])
        state = e.states['zenbook']
        before = brainsync.snapshot_memory(state/'memory.sqlite3')
        marker = brainsync._marker_path(state.parent)
        freeze = marker.read_bytes()
        real = return_brain._save_durable
        def crash_before(path, value):
            if value.get('phase') == 'archiving':
                raise RuntimeError('crash before approval write')
            real(path, value)
        with patch.object(return_brain, '_save_durable', side_effect=crash_before):
            with self.assertRaises(RuntimeError): self.prepare_return()
        self.assertFalse((state/return_brain.FILE).exists())
        self.assertEqual(brainsync.snapshot_memory(state/'memory.sqlite3'), before)
        self.assertEqual(marker.read_bytes(), freeze)
        write_private = brainsync._write_private

        def crash_archive_write(path, data, mode):
            if Path(path).name == 'memory-backup.sqlite3' and Path(path).parent.name.startswith('return-archive-'):
                raise OSError('crash after approval write')
            return write_private(path, data, mode)

        with patch('jarvis.brainsync._write_private', side_effect=crash_archive_write):
            with self.assertRaises(OSError): self.prepare_return()
        record = self.approval('zenbook')
        self.assertEqual(record['phase'], 'needs_review')
        self.assertEqual(brainsync.snapshot_memory(state/'memory.sqlite3'), before)
        self.assertEqual(marker.read_bytes(), freeze)
        with self.assertRaises(RuntimeRefusal) as again:
            self.stage_return('dell', 'zenbook')
        self.assertEqual(again.exception.code, 'reconcile')
        self.assertEqual(brainsync.snapshot_memory(state/'memory.sqlite3'), before)
        with self.assertRaises(RuntimeRefusal) as bad_hash:
            return_brain.retire_interrupted(self.configs['zenbook'], state, e.clients['zenbook'],
                incoming_sha256='0'*64, current_sha256=record['current_memory_sha256'],
                freeze_sha256=record['freeze_sha256'], confirmed=True)
        self.assertEqual(bad_hash.exception.code, 'conflict')
        self.assertTrue((state/return_brain.FILE).is_file())
        untouched = (state/return_brain.FILE).read_bytes()
        with exclusive_lock(state/'cluster-main.lock'):
            with self.assertRaises(LockBusy):
                return_brain.retire_interrupted(self.configs['zenbook'], state, e.clients['zenbook'],
                    incoming_sha256=record['incoming_memory_sha256'], current_sha256=record['current_memory_sha256'],
                    freeze_sha256=record['freeze_sha256'], confirmed=True)
        self.assertEqual((state/return_brain.FILE).read_bytes(), untouched)
        archive = state/record['archive']
        (archive/'memory.sqlite3').write_bytes(before)
        parked = (state/return_brain.FILE).read_bytes()
        retired = return_brain.retire_interrupted(self.configs['zenbook'], state, e.clients['zenbook'],
            incoming_sha256=record['incoming_memory_sha256'], current_sha256=record['current_memory_sha256'],
            freeze_sha256=record['freeze_sha256'], confirmed=True)
        self.assertEqual(retired['status'], 'return_review_retired')
        self.assertEqual((archive/return_brain.FILE).read_bytes(), parked)
        self.assertEqual((archive/'memory.sqlite3').read_bytes(), before)
        self.assertFalse((state/return_brain.FILE).exists())
        with self.assertRaises(RuntimeRefusal) as partial:
            self.stage_return('dell', 'zenbook')
        self.assertEqual(partial.exception.code, 'reconcile')
        self.assertEqual(brainsync.snapshot_memory(state/'memory.sqlite3'), before)
        self.assertFalse((state/return_brain.FILE).exists())
        (archive/'memory.sqlite3').unlink()
        self.prepare_return()
        retried = self.approval('zenbook')
        self.assertEqual(retried['phase'], 'prepared')
        retry_archive = state/retried['archive']
        self.assertEqual((retry_archive/'memory-backup.sqlite3').read_bytes(), before)
        self.assertEqual(json.loads((archive/return_brain.FILE).read_text())['phase'], 'needs_review')
        self.assertEqual(marker.read_bytes(), freeze)
        with self.assertRaises(RuntimeRefusal) as prepared:
            return_brain.retire_interrupted(self.configs['zenbook'], state, e.clients['zenbook'],
                incoming_sha256=record['incoming_memory_sha256'], current_sha256=record['current_memory_sha256'],
                freeze_sha256=record['freeze_sha256'], confirmed=True)
        self.assertEqual(prepared.exception.code, 'reconcile')
        self.assertTrue((state/return_brain.FILE).is_file())
