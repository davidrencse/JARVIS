"""Managed JARVIS-memory handoff onto a destination that has run JARVIS but holds no memories.

Uses the real adapters (SafeHandoff, TransitionBundleTransport, Participant RPC,
brain_channel.run_here, brainsync) from test_roundtrip_adapters. Only the wire,
process and service boundaries are fake. The external vault is never touched.
"""
import contextlib
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import brainsync
from jarvis.memory import Memory
# Module import: a bare class import would make the loader rerun that whole suite here.
from tests.continuity import test_roundtrip_adapters as roundtrip


def _no_vault():
    stack = contextlib.ExitStack()
    for name in ('_vault_root', '_collect_vault', '_materialize', '_git', '_prepare', '_import_git_bundle'):
        stack.enter_context(patch.object(brainsync, name, side_effect=AssertionError('vault touched: ' + name)))
    return stack


def _count(path, table):
    with contextlib.closing(sqlite3.connect(path)) as db:
        return db.execute(f'select count(*) from {table}').fetchone()[0]


class FreshDestinationHandoff(unittest.TestCase):
    def setUp(self):
        self.r = roundtrip.RoundTripAdapters('test_zenbook_to_dell_to_zenbook_with_explicit_return_confirmation')
        self.r.setUp(); self.addCleanup(self.r.doCleanups)
        for cfg in self.r.configs.values():
            cfg['brainsync']['handoff_scope'] = 'jarvis'
        self.e = self.r.e

    def fresh_jarvis_memory(self, device, capture=False):
        """What a JARVIS install that never stored a memory has on disk."""
        Memory(self.e.states[device])
        if capture:
            from jarvis import second_brain_queue
            second_brain_queue.configure(self.e.states[device], enabled=True)
            Memory(self.e.states[device])
        return self.e.states[device] / 'memory.sqlite3'

    def test_fresh_destination_schema_is_replaced_and_kept_aside(self):
        dest = self.fresh_jarvis_memory('dell')
        prior = brainsync.snapshot_memory(dest)
        with _no_vault():
            result = self.r.handoff('zenbook', 'dell')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.e.store.status()['main'], 'dell')
        self.assertTrue(brainsync._frozen(self.e.states['zenbook'].parent, 'zenbook'))
        self.assertEqual(_count(dest, 'items'), 1)
        kept = self.e.states['dell'] / ('memory.sqlite3.prior-' + hashlib.sha256(prior).hexdigest())
        self.assertEqual(kept.read_bytes(), prior)

    def test_fresh_destination_with_archive_capture_is_replaced(self):
        dest = self.fresh_jarvis_memory('dell', capture=True)
        with _no_vault():
            result = self.r.handoff('zenbook', 'dell')
        self.assertTrue(result['ok'], result)
        self.assertEqual(_count(dest, 'items'), 1)

    def handoff_watching_transport(self):
        made = []
        real = roundtrip.TransitionBundleTransport
        def build(*args):
            made.append(real(*args))
            return made[-1]
        with _no_vault(), patch.object(roundtrip, 'TransitionBundleTransport', side_effect=build):
            result = self.r.handoff('zenbook', 'dell')
        self.assertEqual(len(made), 1)
        return result, made[0].diagnostic

    def assert_refused_untouched(self, result, dest, before):
        self.assertFalse(result['ok'])
        self.assertEqual(result.get('code'), 'brain')
        self.assertEqual(brainsync.snapshot_memory(dest), before)
        self.assertFalse(brainsync._frozen(self.e.states['zenbook'].parent, 'zenbook'))
        self.assertNotEqual(self.e.store.status()['main'], 'dell')

    def test_destination_with_a_memory_still_refuses_and_records_code(self):
        dest = self.fresh_jarvis_memory('dell')
        Memory(self.e.states['dell']).add('note', 'Dell memory', 'kept')
        before = brainsync.snapshot_memory(dest)
        result, diagnostic = self.handoff_watching_transport()
        self.assert_refused_untouched(result, dest, before)
        self.assertEqual(diagnostic, {'operation': 'push', 'stage': 'receive', 'ok': False,
                                      'status': 'memory_conflict', 'error': 'memory_conflict'})

    def test_staging_refusal_on_destination_reports_its_code_without_paths(self):
        dest = self.fresh_jarvis_memory('dell')
        before = brainsync.snapshot_memory(dest)
        stage = Path(self.r.configs['dell']['devices']['dell']['brain_transfer_dir'])
        (stage / '.git').mkdir()
        result, diagnostic = self.handoff_watching_transport()
        self.assertEqual(diagnostic['stage'], 'stage')
        self.assertEqual(diagnostic['code'], 'brain_dir_in_git')
        self.assertNotIn(str(stage), json.dumps(diagnostic))
        self.assertFalse(result['ok'])
        self.assertEqual(brainsync.snapshot_memory(dest), before)
        self.assertFalse(brainsync._frozen(self.e.states['zenbook'].parent, 'zenbook'))

    def test_destination_folder_spelled_differently_is_refused_as_forbidden(self):
        dest = self.fresh_jarvis_memory('dell')
        before = brainsync.snapshot_memory(dest)
        # The source's copy of the Dell folder must match the Dell's own string exactly.
        devices = dict(self.r.configs['zenbook']['devices'])
        devices['dell'] = {**devices['dell'], 'brain_transfer_dir': devices['dell']['brain_transfer_dir'] + '/'}
        self.r.configs['zenbook']['devices'] = devices
        result, diagnostic = self.handoff_watching_transport()
        self.assertEqual((diagnostic['stage'], diagnostic['code']), ('stage', 'forbidden'))
        self.assertEqual(brainsync.snapshot_memory(dest), before)
        self.assertFalse(brainsync._frozen(self.e.states['zenbook'].parent, 'zenbook'))


class OutcomeRecord(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name); (self.base / '.jarvis').mkdir()
        from jarvis.continuity.production import ProductionController
        self.controller = object.__new__(ProductionController)
        self.controller._remote = lambda device: None

    def read(self):
        from jarvis.continuity.production import BRAIN_OUTCOME
        return json.loads((self.base / '.jarvis' / BRAIN_OUTCOME).read_text())

    def transport(self, diagnostic):
        class Fake:
            def __init__(self, *args):
                self.diagnostic = diagnostic
        return patch('jarvis.continuity.brain_channel.TransitionBundleTransport', Fake)

    def test_refused_result_is_recorded_as_codes_only(self):
        diagnostic = {'operation': 'push', 'stage': 'receive', 'ok': False,
                      'status': 'memory_conflict', 'error': 'memory_conflict', 'path': 'C:\\Users\\x'}
        with self.transport(diagnostic), patch('jarvis.brainsync.handoff', return_value={
                'ok': False, 'status': 'memory_conflict', 'error': 'memory_conflict', 'side': 'secret-name'}):
            result = self.controller._brain('zenbook', 'dell', {'brainsync': {'enabled': True}}, self.base)
        self.assertFalse(result['ok'])
        record = self.read()
        self.assertEqual(record['error'], 'memory_conflict')
        self.assertEqual(record['transport'], {k: v for k, v in diagnostic.items() if k != 'path'})
        text = json.dumps(record)
        self.assertNotIn('secret-name', text); self.assertNotIn('Users', text)

    def test_raised_refusal_is_recorded_and_still_raised(self):
        from jarvis.continuity.runtime import RuntimeRefusal
        refusal = RuntimeRefusal('callback_failed'); refusal.remote_code = 'forbidden'
        with self.transport({'operation': 'push', 'stage': 'stage', 'ok': False, 'raised': 'runtimerefusal',
                             'code': 'callback_failed', 'remote_code': 'forbidden'}), \
                patch('jarvis.brainsync.handoff', side_effect=refusal):
            with self.assertRaises(RuntimeRefusal):
                self.controller._brain('zenbook', 'dell', {}, self.base)
        record = self.read()
        self.assertEqual((record['ok'], record['code'], record['remote_code']), (False, 'callback_failed', 'forbidden'))
        self.assertEqual(record['transport']['stage'], 'stage')

    def test_unwritable_record_never_changes_the_result(self):
        (self.base / '.jarvis').rmdir(); (self.base / '.jarvis').write_text('not a dir')
        with self.transport(None), patch('jarvis.brainsync.handoff', return_value={'ok': True}):
            self.assertEqual(self.controller._brain('zenbook', 'dell', {}, self.base), {'ok': True})


class RemoteCode(unittest.TestCase):
    def test_participant_brain_errors_become_safe_codes(self):
        from jarvis.continuity.brain_channel import BrainChannelError, safe_code
        self.assertEqual(safe_code('memory_conflict'), 'memory_conflict')
        self.assertEqual(safe_code('C:\\Users\\x'), 'unreported')
        self.assertEqual(safe_code('/home/x', None), None)
        self.assertEqual(BrainChannelError('brain_dir_in_git', 'text').code, 'brain_dir_in_git')


class EmptinessRules(unittest.TestCase):
    def snap(self, build):
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp))
            return brainsync.snapshot_memory(Path(tmp) / 'memory.sqlite3')

    def test_fresh_schema_is_empty(self):
        self.assertFalse(brainsync._db_nonempty(self.snap(Memory)))

    def test_fresh_schema_with_capture_config_is_empty(self):
        def build(path):
            from jarvis import second_brain_queue
            Memory(path); second_brain_queue.configure(path, enabled=True); Memory(path)
        self.assertFalse(brainsync._db_nonempty(self.snap(build)))

    def test_any_memory_row_is_nonempty(self):
        def build(path):
            Memory(path).add('note', 'title', 'body')
        self.assertTrue(brainsync._db_nonempty(self.snap(build)))

    def test_link_or_vector_rows_are_nonempty(self):
        for table, row in (('links', "(1, 2, 'x', 0)"), ('vectors', "(1, 'm', x'00')")):
            def build(path, table=table, row=row):
                Memory(path)
                with contextlib.closing(sqlite3.connect(path / 'memory.sqlite3')) as db, db:
                    cols = [c[1] for c in db.execute(f'pragma table_info({table})')]
                    db.execute(f"insert into {table}({','.join(cols[:4 if table == 'links' else 3])}) values {row}")
            with self.subTest(table=table):
                self.assertTrue(brainsync._db_nonempty(self.snap(build)))

    def test_pending_archive_outbox_is_nonempty(self):
        def build(path):
            from jarvis import second_brain_queue
            Memory(path); second_brain_queue.configure(path, enabled=True)
            Memory(path).add('note', 'title', 'body')
            with contextlib.closing(sqlite3.connect(path / 'memory.sqlite3')) as db, db:
                db.execute('delete from items')
        self.assertTrue(brainsync._db_nonempty(self.snap(build)))

    def test_fts_content_without_external_table_counts(self):
        def build(path):
            with contextlib.closing(sqlite3.connect(path / 'memory.sqlite3')) as db, db:
                db.execute('create virtual table notes using fts5(body)')
                db.execute("insert into notes values ('kept')")
        self.assertTrue(brainsync._db_nonempty(self.snap(build)))

    def test_unreadable_is_nonempty(self):
        self.assertTrue(brainsync._db_nonempty(b'SQLite format 3\x00' + b'\x00' * 200))


if __name__ == '__main__':
    unittest.main()
