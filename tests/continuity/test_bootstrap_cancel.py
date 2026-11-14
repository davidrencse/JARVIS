"""Initial setup can be cancelled; managed-history rollback still needs fences."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis.continuity import authority as A, bootstrap
from tests.continuity import test_authority as fixtures


class InitialCancel(fixtures.Base):
    def test_unacked_initial_setup_cancels_at_newer_version(self):
        self.store.register_participant('dell', 'dell-inst', 'pc')
        self.store.set_mode('PREPARING_V2', 1)
        self.store.set_mode('QUIESCED', 2)
        self.refuse('unacked', self.store.set_mode, 'LEGACY_ACTIVE', 3)
        self.assertEqual(self.store.cancel_initial_bootstrap(3), 5)
        self.assertEqual(self.store.status()['mode'], 'LEGACY_ACTIVE')
        self.refuse('bad_edge', self.store.cancel_initial_bootstrap, 5)

    def test_wrong_version_and_active_mode_refuse(self):
        self.store.set_mode('PREPARING_V2', 1)
        self.refuse('stale', self.store.cancel_initial_bootstrap, 1)

    def test_even_failed_managed_transition_blocks_cancellation(self):
        self.active()
        with self.store._tx() as db:
            db.execute("UPDATE authority SET mode='QUIESCED', holder=NULL, lease_id=NULL, last_holder=NULL, max_validity=0, transition_id=NULL")
        status = self.store.status()
        self.refuse('busy', self.store.cancel_initial_bootstrap, status['mode_version'])


class LocalCancel(unittest.TestCase):
    def test_failed_owner_operation_keeps_marker_and_restores_service(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); marker=root/'continuity-managed.json'
            marker.write_text('{"schema":1,"managed":true}')
            client=Mock()
            before={'mode':'QUIESCED','mode_version':3}
            client.set_mode.side_effect=RuntimeError('refused')
            life=Mock(); life.supported.return_value=True
            life.active.side_effect=[True,False]
            life.service_name='jarvis-cluster.service'
            life.pause.return_value={'paused':True,'stopped':[life.service_name],'failed':[]}
            life.wake.return_value={'failed':[]}
            guard=Mock()
            with patch.object(bootstrap,'_operator',return_value=(root/'operator.json',{'device_id':'zenbook','enrollment':{}},client,before,None,root/'owner')), \
                 patch.object(bootstrap,'_hub',return_value={}), \
                 patch.object(bootstrap,'_validate_production',return_value=(None,guard,None)):
                with self.assertRaisesRegex(RuntimeError,'refused'):
                    bootstrap.cancel_initial(root/'config.json',root,root/'operator.json',expect_version=3,confirmed=True,lifecycle=life)
            self.assertTrue(marker.exists())
            guard.fence.assert_not_called()
            guard.close.assert_called_once()
            life.wake.assert_called_once()


from tests.continuity import test_gateway as wire


class CancelWire(wire.GatewayCase):
    def test_owner_only_cancellation_over_real_http(self):
        self.enroll('zenbook','pc',wire.PC)
        device=self._client('zenbook')
        self.owner.set_mode('PREPARING_V2',1)
        self.owner.set_mode('QUIESCED',2)
        with self.assertRaises(wire.GatewayError):
            device.set_mode('CANCEL_INITIAL_BOOTSTRAP',3)
        self.assertEqual(device.status()['mode'],'QUIESCED')
        result=self.owner.set_mode('CANCEL_INITIAL_BOOTSTRAP',3)
        self.assertEqual(result,{'mode':'LEGACY_ACTIVE','mode_version':5,'initial_bootstrap_cancelled':True})


class LocalSuccess(unittest.TestCase):
    def test_marker_is_retired_only_after_newer_verified_legacy_fence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); marker=root/'continuity-managed.json'
            marker.write_text('{"schema":1,"managed":true}')
            current={'mode':'LEGACY_ACTIVE','mode_version':5,'incarnation':2,'epoch':1,
                     'lease_valid':False,'transition':None}
            client=Mock(); client.status.return_value=current
            client.set_mode.return_value={'mode':'LEGACY_ACTIVE','mode_version':5,
                                          'initial_bootstrap_cancelled':True}
            life=Mock(); life.supported.return_value=True
            life.active.side_effect=[True,False]; life.service_name='jarvis-cluster.service'
            life.pause.return_value={'paused':True,'stopped':[life.service_name],'failed':[]}
            life.wake.return_value={'failed':[]}
            guard=Mock(); guard.local_view.return_value={'legacy_closed':False}
            guard.fence.side_effect=lambda *a,**kw: self.assertTrue(marker.exists())
            with patch.object(bootstrap,'_operator',return_value=(root/'operator.json',{'device_id':'zenbook','enrollment':{}},client,{'mode':'QUIESCED','mode_version':3},None,root/'owner')), \
                 patch.object(bootstrap,'_hub',return_value={}), \
                 patch.object(bootstrap,'_validate_production',return_value=(None,guard,None)):
                result=bootstrap.cancel_initial(root/'config.json',root,root/'operator.json',expect_version=3,confirmed=True,lifecycle=life)
            guard.fence.assert_called_once_with(2,1,5,reopen_legacy=True)
            self.assertTrue(result['ok'])
            self.assertFalse(marker.exists())
            self.assertTrue((root/'continuity-managed.cancelled-v5.json').is_file())
            life.wake.assert_called_once()
