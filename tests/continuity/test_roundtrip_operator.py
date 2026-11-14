"""The local operator must acquire real signed evidence before it can arm."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis import deploy, release
from tests.cluster import test_successor_release_proof as fixtures


def load_helper():
    path=Path(__file__).resolve().parents[2]/'artifacts/main-handoff/dell-roundtrip-operator.py'
    spec=importlib.util.spec_from_file_location('local_roundtrip_operator',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class OperatorProofBootstrap(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SuccessorProofTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        self.note=f.snapshot(f.source)
        # Emulate an installer which accepted the code but ignored the comment.
        old=f.recomment(f.bundle(version=2000),b'')
        f.apply(old)
        self.assertEqual(release.current(f.state),{})
        self.bundle=f.bundle(version=2001)
        self.helper=load_helper()
        self.real_fingerprint=deploy.code_fingerprint
        def fingerprint(root=None):
            return self.real_fingerprint(f.repo/'jarvis' if root is None else root)
        self.enterContext(patch('jarvis.deploy.code_fingerprint',side_effect=fingerprint))
        self.enterContext(patch('jarvis.release.code_fingerprint',side_effect=fingerprint))
        self.client=Mock();self.client.request.return_value=self.bundle
        self.enterContext(patch('jarvis.cluster.Client',return_value=self.client))

    def test_same_code_refresh_gains_authenticated_evidence_without_new_code(self):
        f=self.fixture;before=self.real_fingerprint(f.repo/'jarvis')
        self.helper.ensure_tested_snapshot({},f.state,f.repo,self.note['code'])
        self.assertEqual(release.current(f.state)['code'],self.note['code'])
        self.assertEqual(self.real_fingerprint(f.repo/'jarvis'),before)
        self.assertTrue(release.code_parity(f.state,self.note['code'])['matched'])

    def test_different_dependency_file_refuses_before_apply(self):
        f=self.fixture
        (f.repo/'pyproject.toml').write_text('[project]\nname="changed"\n')
        with patch('jarvis.deploy.apply_bundle') as apply:
            with self.assertRaisesRegex(RuntimeError,'dependencies'):
                self.helper.ensure_tested_snapshot({},f.state,f.repo,self.note['code'])
        apply.assert_not_called();self.assertEqual(release.current(f.state),{})

    def test_missing_or_forged_proof_never_arms(self):
        f=self.fixture
        for bundle in (f.recomment(self.bundle,b''),
                       f.recomment(self.bundle,deploy.TESTED_PROOF+b'{"schema":1}',resign=False)):
            self.client.request.return_value=bundle
            with self.assertRaises((ValueError,RuntimeError)):
                self.helper.ensure_tested_snapshot({},f.state,f.repo,self.note['code'])
            self.assertEqual(release.current(f.state),{})

    def test_return_approval_is_exactly_typed_and_scoped(self):
        match=self.helper.matches_return_ack
        ack={'schema':'jarvis-roundtrip-return-approved/1','main':'dell','target':'zenbook',
             'incarnation':3,'epoch':15,'memory_sha256':'a'*64}
        self.assertTrue(match(ack,3,15,'a'*64))
        for key,value in [('epoch',True),('incarnation',True),('epoch',16),('target','other'),('memory_sha256','b'*64)]:
            self.assertFalse(match({**ack,key:value},3,15,'a'*64))
        self.assertFalse(match({**ack,'force':True},3,15,'a'*64))
