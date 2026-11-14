"""Manual recovery after a hub restart must accept a genuinely expired MAIN."""
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity.authority import AuthorityError, LEASE_SECONDS
from jarvis.continuity.recovery import ManualRecovery
from jarvis.continuity.runtime import MainRuntime, RuntimeRefusal, StoreAdapter
from tests.continuity._fleet import Clock, cutover, open_store, promote, transition


class ManualExpiryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = open_store(Path(self.tmp.name) / 'authority.sqlite', self.clock)
        self.addCleanup(self.store.close)
        cutover(self.store)
        _, self.old_lease = promote(self.store, 'recover', 'zenbook', 'initial')
        self.port = StoreAdapter(self.store)
        self.canaries = []

    def recovery(self, ready=True):
        # Fresh process-local runtime: the old ready lease was lost at hub restart.
        candidate = MainRuntime(self.port, 'dell', 'dell-inst',
                                lambda: {'control_ready': ready, 'checkpoint': 'cp-2'},
                                clock=self.clock.mono, boot=lambda: 'boot-a')
        return ManualRecovery(self.port, candidate,
                              lambda device: self.canaries.append(device) or True)

    def test_expired_active_recovers_through_fence_and_real_canary(self):
        before = self.store.status()
        self.clock.tick(LEASE_SECONDS + self.store.margin + 1)
        expired = self.store.status()
        self.assertEqual(expired['mode'], 'V2_ACTIVE')
        self.assertIs(expired['lease_valid'], False)
        result = self.recovery()('dell', 'after-hub-restart')
        self.assertTrue(result['ok'], result)
        final = self.store.status()
        self.assertEqual(final['epoch'], before['epoch'] + 1)
        self.assertEqual(final['main'], 'dell')
        self.assertTrue(final['lease_valid'])
        self.assertTrue(final['main_ready'])
        self.assertIsNone(final['transition'])
        self.assertEqual(self.canaries, ['dell'])

    def test_valid_main_refuses_without_opening_a_transition(self):
        before = self.store.status()
        with self.assertRaisesRegex(RuntimeRefusal, 'main_alive'):
            self.recovery()('dell', 'still-live')
        self.assertEqual(self.store.status(), before)
        self.assertEqual(self.canaries, [])

    def test_expiry_does_not_bypass_validity_margin(self):
        self.clock.tick(LEASE_SECONDS)
        self.assertIs(self.store.status()['lease_valid'], False)
        with self.assertRaises(AuthorityError) as raised:
            self.recovery()('dell', 'expiry-margin')
        self.assertEqual(raised.exception.code, 'not_quiet')
        self.assertEqual(self.store.status()['transition']['state'], 'DRAINING')
        self.assertFalse(self.store.status()['main_ready'])
        self.assertEqual(self.canaries, [])
        self.clock.tick(self.store.margin + 1)
        self.assertTrue(self.recovery()('dell', 'expiry-margin')['ok'])

    def test_expired_active_still_requires_actual_readiness(self):
        self.clock.tick(LEASE_SECONDS + self.store.margin + 1)
        with self.assertRaises(AuthorityError) as raised:
            self.recovery(ready=False)('dell', 'not-ready')
        self.assertEqual(raised.exception.code, 'not_ready')
        status = self.store.status()
        self.assertFalse(status['lease_valid'])
        self.assertFalse(status['main_ready'])
        self.assertIsNone(status['transition'])
        self.assertEqual(self.canaries, [])

    def test_existing_requested_recovery_from_expired_active_is_resumed(self):
        self.clock.tick(LEASE_SECONDS + self.store.margin + 1)
        tid = transition(self.store, 'recover', 'dell', 'opened-before-restart')
        self.assertEqual(self.store.status()['transition']['state'], 'REQUESTED')
        result = self.recovery()('dell', 'opened-before-restart')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['transition_id'], tid)
        self.assertEqual(self.canaries, ['dell'])
