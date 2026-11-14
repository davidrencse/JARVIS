import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from jarvis import release
from jarvis import release_runtime as runtime


class CommitTests(unittest.TestCase):
    def test_archive_ignores_dirty_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            def git(*args, **kw):
                return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True, **kw).stdout.strip()
            git('init')
            (repo / 'jarvis').mkdir()
            (repo / 'jarvis/a.py').write_text('committed')
            git('add', '.')
            tree = git('write-tree')
            commit = git('-c', 'user.name=Fake', '-c', 'user.email=fake@example.invalid', 'commit-tree', tree, input='fixture\n')
            git('update-ref', 'HEAD', commit)
            (repo / 'jarvis/a.py').write_text('dirty')
            (repo / 'jarvis/secret.py').write_text('untracked')
            state = Path(tmp) / 'state'
            result = release.cut(state, repo=repo, tests=lambda _: (True, 'OK'))
            self.assertTrue(result['released'])
            self.assertEqual(result['commit'], commit)
            self.assertEqual((release.source(state) / 'a.py').read_text(), 'committed')
            self.assertFalse((release.source(state) / 'secret.py').exists())
            with patch.object(release, 'PACKAGE', repo / 'jarvis'):
                self.assertFalse(release.due(state))
            git('update-ref', 'refs/remotes/origin/main', commit)
            newer = git('-c', 'user.name=Fake', '-c', 'user.email=fake@example.invalid', 'commit-tree', tree, input='newer local commit\n')
            git('update-ref', 'HEAD', newer)
            self.assertEqual(release.source_commit(repo), commit)

    def test_failure_state_names_tests_and_notifies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            def archive(repo, stage, commit):
                (stage / 'jarvis').mkdir()
                (stage / 'jarvis/a.py').write_text('x')
            with patch.object(release, 'source_commit', return_value='sha'), patch.object(release, '_archive', archive), patch('jarvis.confirm.notify') as notify:
                for _ in range(2):
                    result = release.cut(state, tests=lambda _: (False, {'summary':'failed', 'failures':['test.core']}), retry=lambda stage, ids: list(ids))
                    self.assertFalse(result['released'])
                notify.assert_called_once()
            self.assertEqual(release.status(state)['message'], 'Release blocked: test.core')

    def _cut(self, state, tests, retry):
        def archive(repo, stage, commit):
            (stage / 'jarvis').mkdir()
            (stage / 'jarvis/a.py').write_text('x')
        with patch.object(release, 'source_commit', return_value='sha'), patch.object(release, '_archive', archive), patch('jarvis.confirm.notify') as notify:
            return release.cut(state, tests=tests, retry=retry), notify

    def test_flaky_failure_passing_on_rerun_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            seen = []
            result, notify = self._cut(state, lambda _: (False, {'summary': 'f', 'failures': ['t.A', 't.B']}),
                                       lambda stage, ids: seen.append(list(ids)) or [])
            self.assertTrue(result['released'])
            self.assertEqual(seen, [['t.A', 't.B']])  # only the failing ids, once
            self.assertEqual(release.current(state)['retried'], ['t.A', 't.B'])
            notify.assert_not_called()
            self.assertFalse((state / release.FAILED).exists())

    def test_repeat_failure_blocks_and_records_both_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            result, notify = self._cut(state, lambda _: (False, {'summary': 'f', 'failures': ['t.A', 't.B']}),
                                       lambda stage, ids: ['t.B'])
            self.assertFalse(result['released'])
            saved = json.loads((state / release.FAILED).read_text())
            self.assertEqual(saved['failures'], ['t.B'])
            self.assertEqual(saved['attempts'], [{'failures': ['t.A', 't.B']}, {'failures': ['t.B']}])
            self.assertEqual(release.status(state)['message'], 'Release blocked: t.B')
            notify.assert_called_once()

    def test_runner_failure_is_not_a_test_id_to_rerun(self):
        with patch('jarvis.release_gate._run') as run:
            self.assertEqual(release.retry_failures(Path('.'), ['runner:core']), ['runner:core'])
            run.assert_not_called()
            run.return_value = {'failures': []}
            self.assertEqual(release.retry_failures(Path('.'), ['t.A']), [])
            run.return_value = {'failures': ['t.A']}
            self.assertEqual(release.retry_failures(Path('.'), ['t.A']), ['t.A'])


class LockHolderTests(unittest.TestCase):
    def test_busy_lock_names_holder_and_status_reports_it(self):
        import os
        from jarvis.locking import exclusive_lock
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with exclusive_lock(state / 'code-release.lock'):
                (state / release.LOCK_NOTE).write_text(json.dumps({'pid': os.getpid(), 'at': 1000, 'command': 'jarvis cluster release'}))
                result = release.cut(state)
                self.assertFalse(result['released'])
                self.assertIn(f'pid {os.getpid()}', result['note'])
                self.assertIn('jarvis cluster release', result['note'])
                self.assertEqual(result['holder']['pid'], os.getpid())
                self.assertIn('Another release is being tested', release.status(state)['message'])

    def test_dead_holder_note_self_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / release.LOCK_NOTE).write_text(json.dumps({'pid': 2 ** 22 + 12345, 'at': 1000, 'command': 'x'}))
            with patch.object(release, '_pid_alive', return_value=False):
                self.assertIsNone(release.lock_holder(state))
            self.assertFalse((state / release.LOCK_NOTE).exists())

    def test_cut_clears_its_note_when_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with patch.object(release, 'source_commit', side_effect=ValueError('none')):
                release.cut(state)
            self.assertFalse((state / release.LOCK_NOTE).exists())


class CutRetryAndWarningTests(unittest.TestCase):
    def test_warning_is_shipped_and_deferred_run_remains_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            def archive(repo, stage, commit):
                (stage / 'jarvis').mkdir()
                (stage / 'jarvis/a.py').write_text('x')
            with patch.object(release, 'source_commit', return_value='sha'), patch.object(release, '_archive', archive):
                result = release.cut(state, tests=lambda _: (False, {'deferred':True, 'summary':'load'}))
                self.assertTrue(result['deferred'])
                self.assertFalse((state / release.FAILED).exists())
                self.assertTrue(release.due(state))
                result = release.cut(state, tests=lambda _: (True, {'warnings':['test.full'], 'summary':'warn'}))
                self.assertTrue(result['released'])
                self.assertEqual(release.current(state)['warnings'], ['test.full'])
                warning = json.loads((state / release.FAILED).read_text())
                self.assertFalse(warning['blocking'])
                self.assertEqual(warning['failures'], ['test.full'])
                self.assertEqual(release.status(state)['message'], 'Release shipped with warnings')


class RuntimeTests(unittest.TestCase):
    def test_restart_is_disabled_by_default(self):
        from types import SimpleNamespace
        hub = SimpleNamespace(config={}, state='fake')
        with patch('jarvis.release.current', return_value={'code':'x'}), patch.object(runtime.os, 'execv') as execute, patch.object(runtime.os, '_exit') as exit:
            self.assertFalse(runtime.maybe_restart(hub))
            execute.assert_not_called()
            exit.assert_not_called()

    def test_remote_session_defers(self):
        with patch('jarvis.sharedinput.remote_session_live', return_value=True):
            self.assertTrue(runtime.busy('fake'))

    def test_chunk_download_resumes_after_link_failure(self):
        raw = json.dumps({'code':'x','zip':'payload'})
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def request(path, **kw):
                offset = int(path.split('=')[1]); calls.append(offset)
                if len(calls) == 2:
                    raise OSError('link')
                data = raw[offset:offset+10]
                return {'code':'x', 'offset':offset, 'total':len(raw), 'data':data}
            client = SimpleNamespace(request=request)
            with self.assertRaises(OSError):
                runtime.fetch(client, tmp, 'x')
            self.assertEqual(runtime.fetch(client, tmp, 'x')['code'], 'x')
            self.assertEqual(calls[:3], [0,10,10])

    def test_restart_when_opted_in_and_idle_uses_existing_service_policy(self):
        from types import SimpleNamespace
        hub = SimpleNamespace(config={'cluster': {'auto_restart':True}}, state='fake', _release_started_code='old')
        with patch('jarvis.release.current', return_value={'code':'new'}), patch('jarvis.deploy.code_fingerprint', return_value='new'), patch.object(runtime, 'busy', return_value=False), patch('jarvis.cluster._systemd_service', return_value=True), patch.object(runtime.os, '_exit', side_effect=SystemExit) as exit:
            with self.assertRaises(SystemExit):
                runtime.maybe_restart(hub)
            exit.assert_called_once_with(75)

    def test_restart_defers_until_idle_or_when_dirty(self):
        from types import SimpleNamespace
        hub = SimpleNamespace(config={'cluster': {'auto_restart':True}}, state='fake', _release_started_code='old')
        with patch('jarvis.release.current', return_value={'code':'new'}), patch('jarvis.deploy.code_fingerprint', return_value='new'), patch.object(runtime, 'busy', return_value=True), patch.object(runtime.os, '_exit') as exit, patch.object(runtime.os, 'execv') as execute:
            self.assertFalse(runtime.maybe_restart(hub))
            exit.assert_not_called(); execute.assert_not_called()
        with patch('jarvis.release.current', return_value={'code':'new'}), patch('jarvis.deploy.code_fingerprint', return_value='dirty'):
            self.assertFalse(runtime.maybe_restart(hub))

    def test_node_defers_before_fetch_during_remote_session(self):
        from jarvis.cluster import NodeWorker
        agent = NodeWorker.__new__(NodeWorker)
        agent.state = Path('fake')
        with patch.object(runtime, 'busy', return_value=True), patch('jarvis.deploy.apply_bundle') as apply:
            self.assertFalse(agent._pull_update('new'))
            apply.assert_not_called()

    def test_handoff_journal_defers_restart(self):
        with tempfile.TemporaryDirectory() as tmp, patch('jarvis.sharedinput.remote_session_live', return_value=False), patch('jarvis.sharedinput.controlling_remote', return_value=False), patch('jarvis.remote_view._ACTIVE', {}):
            (Path(tmp) / 'continuity-handoff.json').write_text(json.dumps({'steps': {'brain': {'phase': 'started'}}}))
            self.assertTrue(runtime.busy(tmp))
