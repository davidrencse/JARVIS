"""Managed handoffs must not resolve, read, bundle, or materialize an external vault."""
import hashlib
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import brainsync as B


class JarvisScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.configs={}; self.bases={}
        for device in ('arch','dell'):
            base=self.root/device; (base/'.jarvis').mkdir(parents=True)
            self.bases[device]=base
            self.configs[device]=B.jarvis_handoff_config({
                'cluster':{'enabled':True,'local_device':device,'main_device':'arch'},
                'devices':{'arch':{},'dell':{}},
                'vault':{'enabled':False,'path':'/external/vault/must-not-be-accessed'},
                'brainsync':{'enabled':True}})
        with closing(sqlite3.connect(self.bases['arch']/'.jarvis/memory.sqlite3')) as db, db:
            db.execute('CREATE TABLE items(body TEXT)'); db.execute("INSERT INTO items VALUES ('JARVIS memory')")

    def no_vault(self):
        from contextlib import ExitStack
        stack=ExitStack()
        for name in ('_vault_root','_collect_vault','_materialize','_git','_prepare','_import_git_bundle'):
            stack.enter_context(patch.object(B,name,side_effect=AssertionError('external scope touched: '+name)))
        return stack

    def make(self):
        return B._make_bundle(self.configs['arch'],self.bases['arch'],'arch','dell',None)[0]

    def test_memory_export_import_freeze_and_replay_never_access_vault(self):
        with self.no_vault():
            blob=self.make()
            header,git,memory=B._unbundle(blob)
            self.assertEqual(header['schema'],B.JARVIS_SCHEMA)
            self.assertEqual(git,b'')
            result=B.receive(self.configs['dell'],self.bases['dell'],blob)
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['scope'],'jarvis')
            self.assertEqual(result['created'],[])
            pending=json.loads(B._pending_json(self.bases['arch']).read_text())
            self.assertTrue(B._source_unchanged(self.configs['arch'],self.bases['arch'],pending))
            B._write_marker(self.bases['arch'],'arch','dell',hashlib.sha256(blob).hexdigest())
            self.assertEqual(self.make(),blob)
        with closing(sqlite3.connect(self.bases['dell']/'.jarvis/memory.sqlite3')) as db, db:
            self.assertEqual(db.execute('SELECT body FROM items').fetchall(),[('JARVIS memory',)])

    def test_vault_bundle_refuses_before_external_access(self):
        memory=B.snapshot_memory(self.bases['arch']/'.jarvis/memory.sqlite3')
        header={'schema':B.SCHEMA,'old_main':'arch','new_main':'dell','quiesced':True,
                'memory_sha256':hashlib.sha256(memory).hexdigest()}
        with self.no_vault():
            result=B.receive(self.configs['dell'],self.bases['dell'],B._bundle_bytes(header,b'vault data',memory))
        self.assertFalse(result['ok']); self.assertEqual(result['error'],'handoff_scope')

    def test_memory_bundle_rejects_hidden_git_payload(self):
        with self.no_vault():
            header,_git,memory=B._unbundle(self.make())
            result=B.receive(self.configs['dell'],self.bases['dell'],B._bundle_bytes(header,b'external content',memory))
        self.assertFalse(result['ok']); self.assertEqual(result['error'],'bad_bundle')

    def test_existing_destination_memory_is_preserved(self):
        path=self.bases['dell']/'.jarvis/memory.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE items(body TEXT)'); db.execute("INSERT INTO items VALUES ('existing Dell data')")
        before=B.snapshot_memory(path)
        with self.no_vault():result=B.receive(self.configs['dell'],self.bases['dell'],self.make())
        self.assertFalse(result['ok']); self.assertEqual(result['error'],'memory_conflict')
        self.assertEqual(B.snapshot_memory(path),before)

    def test_production_coordinator_selects_jarvis_scope_without_mutating_config(self):
        from jarvis.continuity.production import ProductionController
        from unittest.mock import Mock
        controller=object.__new__(ProductionController)
        controller._remote=Mock()
        config={'brainsync':{'enabled':True},'vault':{'path':'/external/do-not-read'}}
        with patch('jarvis.brainsync.handoff',return_value={'ok':True}) as handoff, \
             patch('jarvis.continuity.brain_channel.TransitionBundleTransport'):
            self.assertTrue(controller._brain('arch','dell',config,self.root)['ok'])
        sent=handoff.call_args.args[2]
        self.assertEqual(sent['brainsync']['handoff_scope'],'jarvis')
        self.assertNotIn('handoff_scope',config['brainsync'])

    def approve(self, blob):
        _header,_git,memory=B._unbundle(blob)
        return B.approve_pending_handoff(self.configs['arch'],self.bases['arch'],'arch','dell',
            incarnation=3,epoch=14,memory_sha256=hashlib.sha256(memory).hexdigest(),
            bundle_sha256=hashlib.sha256(blob).hexdigest())

    def test_reviewed_memory_reuses_exact_bundle_without_rebuilding(self):
        with self.no_vault():
            blob=self.make(); self.approve(blob)
            with patch.object(B,'_remember_pending') as remember, \
                 patch.object(B,'_bundle_bytes',side_effect=AssertionError('reviewed bytes rebuilt')):
                replay,how=B._make_bundle(self.configs['arch'],self.bases['arch'],'arch','dell',None)
        self.assertEqual(replay,blob); self.assertEqual(how,'reviewed_pending')
        remember.assert_not_called()

    def test_changed_memory_refuses_and_preserves_reviewed_bundle(self):
        with self.no_vault():
            blob=self.make(); self.approve(blob)
            with closing(sqlite3.connect(self.bases['arch']/'.jarvis/memory.sqlite3')) as db,db:
                db.execute("INSERT INTO items VALUES ('new real memory')")
            with self.assertRaises(B.BrainSyncError) as caught:self.make()
            self.assertEqual(caught.exception.code,'reviewed_memory_changed')
        self.assertEqual(B._pending_bin(self.bases['arch']).read_bytes(),blob)

    def test_reviewed_tuple_and_extra_payload_refuse(self):
        with self.no_vault():
            blob=self.make(); self.approve(blob)
            with self.assertRaises(B.BrainSyncError) as stale:
                B._reviewed_pending_bundle(self.configs['arch'],self.bases['arch'],'arch','dell',incarnation=3,epoch=15)
            self.assertEqual(stale.exception.code,'reviewed_handoff_stale')
            header,_git,memory=B._unbundle(blob)
            wrong=B._bundle_bytes(header,b'external data',memory)
            B._pending_bin(self.bases['arch']).write_bytes(wrong)
            with self.assertRaises(B.BrainSyncError) as invalid:self.approve(wrong)
            self.assertEqual(invalid.exception.code,'reviewed_handoff_invalid')
