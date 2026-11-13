"""Unexpected MAIN loss: real authority and runtimes, no services or paid brain calls."""
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity.authority import LEASE_SECONDS, AuthorityError
from jarvis.continuity.runtime import (
    LinkError, MainRuntime, RecoveryOrchestrator, RuntimePolicy, RuntimeRefusal, SafeHandoff, StoreAdapter,
)
from tests.continuity._fleet import Clock, ack, cutover, open_store, promote


class AutomaticTakeoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.store = open_store(self.root/'authority.sqlite', self.clock)
        self.addCleanup(self.store.close)
        cutover(self.store)
        _, self.old_lease = promote(self.store, 'recover', 'zenbook', 'boot')
        self.port = StoreAdapter(self.store)
        self.nodes = {}
        self.directory = {}
        self.events = []
        for device in ('zenbook', 'dell'):
            runtime = MainRuntime(self.port, device, device+'-inst',
                                  lambda: {'control_ready': True, 'checkpoint': 'cp-1'},
                                  clock=self.clock.mono, boot=lambda: 'test-boot')
            runtime.start()
            self.nodes[device] = runtime
        self.nodes['zenbook'].accept_lease(self.old_lease)
        self.refresh('zenbook', 'dell')

    def refresh(self, *devices):
        self.directory.clear()
        for device in devices:
            self.directory[device] = {'kind': 'pc', 'control_ready': True,
                                      'checkpoint': 'cp-1', 'observed_at': self.clock.m}

    def driver(self, device, activate=None):
        policy = RuntimePolicy(preferred='zenbook', recover_candidates=('dell',),
                               auto_recover=True, auto_failback=True)
        return RecoveryOrchestrator(self.port, self.directory, policy, self.clock.mono,
                                    candidate_runtime=self.nodes[device],
                                    activate=activate or self.activate)

    def activate(self, device, lease):
        # The serving hook runs on the lease owner while ordinary effects are closed.
        self.assertEqual(self.nodes[device].lease_snapshot(), lease)
        self.assertFalse(self.store.status()['main_ready'])
        self.events.append(device)
        return True

    def lose_arch(self):
        self.clock.tick(LEASE_SECONDS + 0.01)
        self.refresh('dell')
        self.assertFalse(self.store.status()['lease_valid'])

    def finish(self, driver):
        result = None
        for _ in range(8):
            result = driver.step()
            if result.get('wait'):
                self.clock.tick(result['wait'] + 0.01)
                self.refresh(*self.directory)
            if result.get('phase') == 'main_ready':
                break
        return result

    def test_crash_mid_lease_waits_margin_then_owner_serves_and_returns(self):
        dell = self.driver('dell')
        # Arch vanishes mid-lease: no release and no Arch fence acknowledgement.
        self.clock.tick(LEASE_SECONDS / 2)
        self.refresh('dell')
        dell.step()
        self.assertIsNone(self.store.status()['transition'])
        self.clock.tick(LEASE_SECONDS / 2 + 0.01)
        self.refresh('dell')
        self.assertEqual(dell.step()['code'], 'requested')
        self.assertEqual(dell.step()['code'], 'draining')
        waiting = dell.step()
        self.assertEqual(waiting['code'], 'not_quiet')
        self.assertGreater(waiting['wait'], 0)
        self.assertEqual(self.events, [])
        self.clock.tick(waiting['wait'] + 0.01)
        self.refresh('dell')
        self.assertTrue(dell.step()['ok'])
        self.assertEqual(self.events, ['dell'])
        self.assertEqual(self.nodes['dell'].require_authority('control')['holder'], 'dell')
        with self.assertRaises(AuthorityError):
            self.store.renew('zenbook', self.old_lease['lease_id'],
                             self.old_lease['incarnation'], self.old_lease['epoch'])

        # Arch returns. Continuous readiness and a checkpoint precede handback.
        arch = self.driver('zenbook')
        self.refresh('zenbook', 'dell')
        self.assertEqual(arch.step()['code'], 'stabilising')
        for _ in range(6):
            self.clock.tick(5)
            self.nodes['dell'].renew_once()
            self.refresh('zenbook', 'dell')
        self.assertEqual(arch.step()['code'], 'requested')
        self.assertTrue(self.finish(arch)['ok'])
        self.assertEqual(self.events, ['dell', 'zenbook'])
        self.assertEqual(self.nodes['zenbook'].require_authority('control')['holder'], 'zenbook')
        with self.assertRaises(RuntimeRefusal):
            self.nodes['dell'].require_authority('control')

    def test_another_node_cannot_promote_the_candidate(self):
        self.lose_arch()
        dell = self.driver('dell')
        dell.step()
        other = self.driver('zenbook')
        self.assertEqual(other.step()['code'], 'not_candidate')
        self.assertEqual(self.events, [])
        self.assertTrue(self.finish(dell)['ok'])

    def test_no_activation_or_failed_canary_never_opens_admission(self):
        self.lose_arch()
        driver = RecoveryOrchestrator(self.port, self.directory,
            RuntimePolicy(recover_candidates=('dell',), auto_recover=True), self.clock.mono,
            candidate_runtime=self.nodes['dell'])
        self.finish(driver)
        self.assertEqual(self.nodes['dell'].lease_snapshot()['holder'], 'dell')
        self.assertFalse(self.store.status()['main_ready'])
        failed = self.driver('dell', activate=lambda device, lease: False)
        self.assertFalse(failed.step()['ok'])
        self.assertIsNone(self.store.status()['main'])
        self.assertIsNone(self.store.status()['transition'])

    def test_lost_accept_response_retries_the_real_grant_on_its_owner(self):
        self.lose_arch()
        runtime = self.nodes['dell']
        class LostAccept:
            device_id = 'dell'
            failed = False
            def accept_lease(inner, lease):
                runtime.accept_lease(lease)
                if not inner.failed:
                    inner.failed = True
                    raise LinkError()
            def __getattr__(inner, name):
                return getattr(runtime, name)
        driver = RecoveryOrchestrator(self.port, self.directory,
            RuntimePolicy(recover_candidates=('dell',), auto_recover=True), self.clock.mono,
            candidate_runtime=LostAccept(), activate=self.activate)
        driver.step(); driver.step()
        self.clock.tick(self.store.margin + 0.01)
        self.assertEqual(driver.step()['code'], 'partition')
        grant = runtime.lease_snapshot()
        self.assertFalse(self.store.status()['main_ready'])
        self.assertEqual(driver.step()['phase'], 'main_ready')
        self.assertEqual(runtime.lease_snapshot(), grant)
        self.assertEqual(self.events, ['dell'])

    def test_dead_not_ready_holder_does_not_wedge_recovery(self):
        self.lose_arch()
        driver = RecoveryOrchestrator(self.port, self.directory,
            RuntimePolicy(recover_candidates=('dell',), auto_recover=True), self.clock.mono)
        self.finish(driver)
        self.assertEqual(self.store.status()['transition']['state'], 'GRANTED_NOT_READY')
        self.clock.tick(LEASE_SECONDS + 1)
        self.assertEqual(self.driver('zenbook').step()['phase'], 'cancelled')
        self.assertIsNone(self.store.status()['transition'])
        self.assertIsNone(self.store.status()['main'])

    def test_safe_recovery_resumes_own_not_ready_lease_without_contacting_arch(self):
        self.lose_arch()
        # Construct a granted recovery interrupted just before promotion.
        st = self.store.status()
        tr = self.store.request('owner', 'recover', 'recover', 'dell',
                                st['incarnation'], st['epoch'], st['policy_version'])
        tid = tr['transition_id']
        self.store.readiness(tid, True, False, 'cp-1')
        self.store.drain(tid)
        self.clock.tick(self.store.margin + 0.01)
        self.store.fence(tid)
        self.nodes['dell'].accept_lease(self.store.grant(tid))
        class Vanished:
            device_id = 'zenbook'
            def begin_drain(self, tid):
                raise AssertionError('must not contact vanished source')
        handoff = SafeHandoff(self.port, source_runtime=Vanished(),
            candidate_runtime=self.nodes['dell'],
            readiness=lambda device: {'control_ready': True, 'ai_ready': False, 'checkpoint': 'cp-1'},
            brain=lambda *args: {'ok': True},
            repoint=lambda *args: {'ok': True, 'authenticated': True,
                                  'key_updated': True, 'endpoint_updated': True},
            canary=lambda device: True, journal_path=self.root/'handoff.json')
        self.assertTrue(handoff('zenbook', 'dell', {}, self.root)['ok'])

    def test_safe_recovery_does_not_drain_an_unreachable_expired_source(self):
        self.lose_arch()
        self.clock.tick(self.store.margin + 0.01)
        class Vanished:
            device_id = 'zenbook'
            def begin_drain(self, tid):
                raise AssertionError('expired source must not block recovery')
        handoff = SafeHandoff(self.port, source_runtime=Vanished(),
            candidate_runtime=self.nodes['dell'],
            readiness=lambda device: {'control_ready': True, 'ai_ready': False, 'checkpoint': 'cp-1'},
            brain=lambda *args: {'ok': True},
            repoint=lambda *args: {'ok': True, 'authenticated': True,
                                  'key_updated': True, 'endpoint_updated': True},
            canary=lambda device: True, journal_path=self.root/'handoff.json')
        self.assertTrue(handoff('zenbook', 'dell', {}, self.root)['ok'])
        self.assertEqual(self.nodes['dell'].require_authority('control')['holder'], 'dell')

    def test_cancel_partial_quorum_after_crash_does_not_block_later_fence(self):
        self.lose_arch()
        driver = self.driver('dell')
        driver.step(); driver.step()
        ack(self.store, ('dell', 'oracle'))
        tid = self.store.status()['transition']['transition_id']
        self.store.cancel(tid)
        # 11fed01 keeps partial acks from claiming a full cooperative fence.
        self.assertIsNone(self.store.status()['transition'])
        self.clock.tick(self.store.margin + 0.01)
        self.refresh('dell')
        self.assertEqual(self.finish(self.driver('dell'))['phase'], 'main_ready')
        self.assertEqual(self.store.status()['main'], 'dell')
