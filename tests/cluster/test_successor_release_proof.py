"""Real signed updates carry tested snapshots to a future MAIN, without git sync."""
import base64
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import deploy, release
from jarvis.release_runtime import require_loaded_code
from tests.cluster.bundles import make_tree
from tests.cluster.test_release_health import agent_files, node_config, TOKEN


class SuccessorProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.state, self.repo = (self.root / n for n in ('source', 'node', 'repo'))
        self.state.mkdir()
        self.key = deploy.code_key(self.source)
        make_tree(self.repo, agent_files({'jarvis/value.py': b"value = 'old'\n"}))
        (self.repo / 'jarvis.config.json').write_text(json.dumps(node_config()))
        (self.state / 'cluster-node.json').write_text(json.dumps({
            'node_id': 'dell', 'main_device': 'zenbook', 'token': TOKEN, 'code_key': self.key}))

    def snapshot(self, state, value='new', at=1000):
        folder = release.release_dir(state)
        shutil.rmtree(folder, ignore_errors=True)
        make_tree(folder, agent_files({'jarvis/value.py': f'value = {value!r}\n'.encode()}))
        note = {'code': deploy.code_fingerprint(folder / 'jarvis'), 'commit': 'a' * 40,
                'tests': 'Ran focused and safety tests: OK', 'at': at, 'warnings': []}
        (folder / 'release.json').write_text(json.dumps(note))
        return note

    def bundle(self, version=2000):
        with patch('jarvis.deploy.time.time', return_value=version):
            return deploy.code_bundle(self.source)

    def apply(self, bundle, **kw):
        return deploy.apply_bundle(bundle, self.key, self.repo, self.state, **kw)

    def recomment(self, bundle, comment, resign=True):
        raw = base64.b64decode(bundle['zip'])
        buffer = io.BytesIO(raw)
        with zipfile.ZipFile(buffer, 'a') as archive:
            archive.comment = comment
        raw = buffer.getvalue()
        bundle = {**bundle, 'zip': base64.b64encode(raw).decode()}
        if resign:
            bundle['signature'] = deploy.sign(raw, self.source)
            bundle['vsig'] = deploy.sign(deploy._bundle_bytes(raw, bundle['version'], bundle['code']), self.source)
        return bundle

    def test_signed_tested_apply_enables_promoted_parity_and_exact_successor_serving(self):
        note = self.snapshot(self.source)
        bundle = self.bundle()
        with patch('socket.create_connection', side_effect=AssertionError('network')):
            self.apply(bundle)  # actual staged imports and post-apply local health
        received = release.current(self.state)
        self.assertEqual(received['code'], note['code'])
        self.assertEqual(received['tests'], note['tests'])
        self.assertEqual(received['bundle_version'], 2000)
        with patch('jarvis.release.code_fingerprint', return_value=deploy.code_fingerprint(self.repo / 'jarvis')):
            self.assertTrue(release.code_parity(self.state, note['code'])['matched'])
            self.assertTrue(require_loaded_code(SimpleNamespace(state=self.state, loaded_code=note['code']))['matched'])
        # A later checkout edit cannot ride along when this PC becomes MAIN.
        (self.repo / 'jarvis/value.py').write_text('dirty = True\n')
        successor = deploy.code_bundle(self.state)
        self.assertEqual(successor['code'], note['code'])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(successor['zip']))) as archive:
            self.assertEqual(archive.read('jarvis/value.py'), b"value = 'new'\n")
            self.assertEqual(json.loads(archive.comment[len(deploy.TESTED_PROOF):])['code'], note['code'])

    def test_untested_fallback_and_old_bundles_never_gain_tested_evidence(self):
        self.snapshot(self.source)
        for tested in (False, True):
            with self.subTest(tested=tested):
                if not tested:
                    with patch('jarvis.release.source', return_value=self.repo / 'jarvis'):
                        bundle = self.bundle()
                else:
                    bundle = self.recomment(self.bundle(version=2001), b'')
                self.apply(bundle)
                self.assertEqual(release.current(self.state), {})

    def test_proof_comment_is_inside_signature_and_mismatch_is_rejected_before_apply(self):
        note = self.snapshot(self.source)
        bundle = self.bundle()
        for mutation, signed in (({**note, 'schema': 1, 'code': '0' * 16}, False),
                                 ({**note, 'schema': 1, 'code': '0' * 16}, True),
                                 ({'schema': 1, 'code': note['code']}, True)):
            with self.subTest(signed=signed, mutation=mutation):
                bad = self.recomment(bundle, deploy.TESTED_PROOF + json.dumps(mutation).encode(), signed)
                with patch('jarvis.deploy._post_apply_health') as health:
                    with self.assertRaises(ValueError):
                        self.apply(bad)
                    health.assert_not_called()
                self.assertEqual(release.current(self.state), {})
                self.assertEqual(deploy.installed_version(self.state), 0)

    def test_authenticated_proof_cannot_bypass_package_hash_check(self):
        note = self.snapshot(self.source)
        bundle = self.bundle()
        raw = io.BytesIO(base64.b64decode(bundle['zip']))
        with zipfile.ZipFile(raw, 'a') as archive:
            archive.writestr('jarvis/unexpected.py', b'extra = True\n')
        payload = raw.getvalue()
        bundle['zip'] = base64.b64encode(payload).decode()
        bundle['vsig'] = deploy.sign(deploy._bundle_bytes(payload, bundle['version'], note['code']), self.source)
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.apply(bundle)
        self.assertEqual(release.current(self.state), {})

    def test_health_failure_preserves_old_evidence_and_actual_old_package(self):
        old = self.snapshot(self.state, 'old', 500)
        note = self.snapshot(self.source)
        with self.assertRaisesRegex(ValueError, 'restored the previous code'):
            self.apply(self.bundle(), health=lambda repo, state: 'unhealthy')
        self.assertEqual(deploy.code_fingerprint(self.repo / 'jarvis'), old['code'])
        self.assertEqual(release.current(self.state)['code'], old['code'])
        self.assertTrue(release.is_bad(self.state, note['code']))
        self.assertEqual(deploy.installed_version(self.state), 0)

    def test_older_or_equal_replay_cannot_adopt_proof(self):
        self.snapshot(self.source)
        bundle = self.bundle()
        (self.state / deploy.VERSION_FILE).write_text(json.dumps({'version': 2000}))
        self.apply(bundle)  # existing equal-version compatibility, but no new evidence
        self.assertEqual(release.current(self.state), {})
        older = self.bundle(version=1999)
        with self.assertRaisesRegex(ValueError, 'replay'):
            self.apply(older)
        self.assertEqual(release.current(self.state), {})

    def test_rewrapped_older_or_equal_source_release_cannot_replace_newer_snapshot(self):
        for at in (1000, 1100):
            with self.subTest(local_at=at):
                self.snapshot(self.source, at=1000)
                newer = self.snapshot(self.state, 'newer-local', at)
                self.apply(self.bundle(version=2000 + at))
                self.assertEqual(release.current(self.state)['code'], newer['code'])

    def test_watchdog_rollback_restores_previous_evidence_and_cannot_serve_failed_release(self):
        old = self.snapshot(self.state, 'old', 500)
        release.backup(self.state, self.repo)
        note = self.snapshot(self.source)
        self.apply(self.bundle())
        script = self.root / 'guard.py'
        script.write_text(release.GUARD.replace('time.sleep(2)', 'time.sleep(0.01)', 1))
        (self.state / 'logs').mkdir()
        out = subprocess.run([sys.executable, str(script), str(self.repo), str(self.state),
                              note['code'], '0', 'launch', sys.executable, '-c', 'pass'],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(deploy.code_fingerprint(self.repo / 'jarvis'), old['code'])
        self.assertEqual(release.current(self.state)['code'], old['code'])
        self.assertEqual(release.served_code(self.state), old['code'])
        self.assertTrue(release.is_bad(self.state, note['code']))
        self.assertEqual(deploy.installed_version(self.state), 2000)  # anti-replay floor stays
        with patch('jarvis.release.code_fingerprint', return_value=old['code']):
            self.assertTrue(release.code_parity(self.state, old['code'])['matched'])

    def test_unversioned_signed_proof_and_mismatched_source_do_not_create_evidence(self):
        note = self.snapshot(self.source)
        bundle = self.bundle()
        del bundle['version']
        del bundle['vsig']
        with patch('jarvis.deploy.unversioned_allowed', return_value=True):
            self.apply(bundle)
        self.assertEqual(release.current(self.state), {})
        # Merely signing changed snapshot contents cannot preserve the old test claim.
        (release.release_dir(self.source) / 'jarvis/value.py').write_text('changed = True\n')
        bundle = self.bundle()
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle['zip']))) as archive:
            self.assertEqual(archive.comment, b'')
        self.apply(bundle)
        self.assertEqual(release.current(self.state), {})
        self.assertNotEqual(bundle['code'], note['code'])

    def test_rolled_back_source_cannot_issue_new_test_evidence(self):
        note = self.snapshot(self.source)
        (self.source / release.BAD).write_text(json.dumps({'code': note['code']}))
        bundle = self.bundle()
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle['zip']))) as archive:
            self.assertEqual(archive.comment, b'')
        self.apply(bundle)
        self.assertEqual(release.current(self.state), {})

    def test_watchdog_without_prior_evidence_removes_failed_tested_snapshot(self):
        release.backup(self.state, self.repo)
        note = self.snapshot(self.source)
        self.apply(self.bundle())
        script = self.root / 'guard.py'
        script.write_text(release.GUARD.replace('time.sleep(2)', 'time.sleep(0.01)', 1))
        out = subprocess.run([sys.executable, str(script), str(self.repo), str(self.state),
                              note['code'], '0', 'launch', sys.executable, '-c', 'pass'],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(release.current(self.state), {})
        self.assertTrue(release.is_bad(self.state, note['code']))
        with patch('jarvis.release.code_fingerprint', return_value=deploy.code_fingerprint(self.repo / 'jarvis')):
            self.assertFalse(release.code_parity(self.state, note['code'])['matched'])

    def test_stale_successor_git_cannot_cut_over_imported_snapshot(self):
        gitrepo = self.root / 'gitrepo'
        gitrepo.mkdir()
        def git(*args):
            return subprocess.run(['git', *args], cwd=gitrepo, capture_output=True, text=True, check=True).stdout.strip()
        git('init')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        commits = []
        for content in ('old', 'tested', 'new'):
            (gitrepo / 'file').write_text(content)
            git('add', 'file')
            git('commit', '-m', content)
            commits.append(git('rev-parse', 'HEAD'))
        self.snapshot(self.source)
        self.apply(self.bundle())
        path = release.release_dir(self.state) / 'release.json'
        note = json.loads(path.read_text())
        note['commit'] = commits[1]
        path.write_text(json.dumps(note))
        self.assertFalse(release._successor_commit_allowed(self.state, commits[0], gitrepo))
        self.assertTrue(release._successor_commit_allowed(self.state, commits[2], gitrepo))
        self.assertFalse(release._successor_commit_allowed(self.state, 'f' * 40, gitrepo))
        with patch('jarvis.release.source_commit', return_value=commits[0]), \
                patch('jarvis.release._archive') as archive:
            result = release._cut(self.state, gitrepo, 0, lambda _: self.fail('must not run tests'))
        self.assertFalse(result['released'])
        archive.assert_not_called()
        self.assertEqual(release.current(self.state)['commit'], commits[1])
