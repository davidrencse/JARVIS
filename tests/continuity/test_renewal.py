"""LeaseRenewer waits for the authority renew_after, and falls back to its interval."""

import threading
import time
import types
import unittest

from jarvis.continuity.renewal import LeaseRenewer, SelfRecovery
from jarvis.continuity.runtime import RuntimeRefusal


def _runtime(answer):
    runtime = types.SimpleNamespace(calls=0, answer=answer)

    def lease_snapshot():
        return {'holder': 'dell', 'lease_id': 'l', 'incarnation': 1, 'epoch': 1}

    def renew_once():
        runtime.calls += 1
        if isinstance(runtime.answer, BaseException):
            raise runtime.answer
        return runtime.answer

    runtime.lease_snapshot = lease_snapshot
    runtime.renew_once = renew_once
    return runtime


class NextDelay(unittest.TestCase):
    def setUp(self):
        self.renewer = LeaseRenewer(types.SimpleNamespace(), interval=3)

    def test_successful_renew_after_is_capped_by_lease_floor_and_limit(self):
        ok = {'code': 'ok', 'renew_after': 5.0, 'remaining': 15}
        self.assertEqual(self.renewer._next_delay(ok), 5.0)
        self.assertEqual(self.renewer._next_delay({**ok, 'remaining': 6}), 3.0)
        self.assertEqual(self.renewer._next_delay({**ok, 'renew_after': 0.1}), 0.5)
        self.assertEqual(self.renewer._next_delay({**ok, 'renew_after': 99}), 5)
        # A non-ok code is still a schedule when it is not a blocked error.
        late = {'code': 'late_response', 'renew_after': 4.0, 'remaining': 15}
        self.assertEqual(self.renewer._next_delay(late), 4.0)
        self.assertEqual(self.renewer._next_delay({'renew_after': 5.0, 'remaining': 15}), 5.0)

    def test_bad_renew_after_and_blocked_codes_use_the_interval(self):
        base = {'code': 'ok', 'remaining': 15}
        for renew_after in (True, False, float('nan'), float('inf'), float('-inf'), -1, 0, None):
            self.assertEqual(
                self.renewer._next_delay({**base, 'renew_after': renew_after}), 3, renew_after)
        self.assertEqual(self.renewer._next_delay({'code': 'ok', 'remaining': 15}), 3)
        for code in ('partition', 'draining', 'no_lease', 'renewal_paused', 'suspend'):
            blocked = {'code': code, 'renew_after': 5.0, 'remaining': 15}
            self.assertEqual(self.renewer._next_delay(blocked), 3, code)
        # Half of the remaining lease wins when that half is under the 0.5s floor.
        tight = {'code': 'ok', 'renew_after': 5.0, 'remaining': 0.4}
        self.assertEqual(self.renewer._next_delay(tight), 0.2)
        for result in (None, 'ok', 5, ['code', 'ok']):
            self.assertEqual(self.renewer._next_delay(result), 3, type(result))

    def test_tick_returns_the_renew_once_dict(self):
        answer = {'code': 'ok', 'renew_after': 5.0, 'remaining': 15, 'ready': True}
        runtime = _runtime(answer)
        renewer = LeaseRenewer(types.SimpleNamespace(runtime=runtime))
        self.assertEqual(renewer.tick(), answer)
        self.assertEqual(runtime.calls, 1)
        runtime.lease_snapshot = lambda: None
        self.assertEqual(renewer.tick(), {'code': 'no_lease'})
        self.assertEqual(runtime.calls, 1)


class RunLoop(unittest.TestCase):
    def test_successive_waits_follow_renew_after(self):
        runtime = _runtime({'code': 'ok', 'renew_after': 5.0, 'remaining': 15})
        renewer = LeaseRenewer(types.SimpleNamespace(runtime=runtime), interval=3)
        delays = []

        def wait(delay):
            delays.append(delay)
            return len(delays) >= 4

        renewer.stop.wait = wait
        renewer._run()
        # First wait is the constructor interval; each later wait is renew_after.
        self.assertEqual(delays, [3, 5.0, 5.0, 5.0])
        self.assertEqual(runtime.calls, 3)

    def test_exception_does_not_stop_the_loop_and_polls_at_interval(self):
        runtime = _runtime(RuntimeError('gateway down'))
        renewer = LeaseRenewer(types.SimpleNamespace(runtime=runtime), interval=3)
        delays = []

        def wait(delay):
            delays.append(delay)
            return len(delays) >= 3

        renewer.stop.wait = wait
        renewer._run()
        self.assertEqual(delays, [3, 3, 3])
        self.assertEqual(runtime.calls, 2)

    def test_close_interrupts_a_long_renew_after_wait(self):
        saw = threading.Event()

        def renew_once():
            saw.set()
            return {'code': 'ok', 'renew_after': 5.0, 'remaining': 15}

        runtime = types.SimpleNamespace(
            lease_snapshot=lambda: {'holder': 'dell'}, renew_once=renew_once)
        renewer = LeaseRenewer(types.SimpleNamespace(runtime=runtime), interval=0.05)
        renewer.start()
        self.addCleanup(renewer.close)
        self.assertTrue(saw.wait(1))
        started = time.monotonic()
        thread = renewer.thread
        renewer.close()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(renewer.thread)


class _Stuck:
    def join(self, timeout=None):
        return None

    def is_alive(self):
        return True


class SuspendAndRestart(unittest.TestCase):
    def _renewer(self, answer=None, block=None):
        runtime = _runtime(answer or {'code': 'ok', 'renew_after': 5.0, 'remaining': 15})
        runtime._block = block
        services = types.SimpleNamespace(calls=0, runtime=runtime)

        def sync(bound):
            services.calls += 1
            services.runtime = bound

        services.sync = sync
        renewer = LeaseRenewer(types.SimpleNamespace(runtime=runtime, services=services))
        renewer.recovery = types.SimpleNamespace(calls=0)

        def recover():
            renewer.recovery.calls += 1
            return {'code': 'recover'}

        renewer.recovery.tick = recover
        return renewer, runtime, services

    def test_suspend_does_not_renew_recover_or_sync(self):
        renewer, runtime, services = self._renewer(block='suspend')
        self.assertEqual(renewer.tick(), {'code': 'suspend'})
        self.assertEqual(runtime.calls, 0)
        self.assertEqual(renewer.recovery.calls, 0)
        self.assertEqual(services.calls, 0)
        self.assertIsNone(renewer.last_recovery)

    def test_resume_renews_again(self):
        renewer, runtime, services = self._renewer(block='resumed')
        answer = renewer.tick()
        self.assertEqual(answer['code'], 'ok')
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(renewer.recovery.calls, 1)
        self.assertEqual(services.calls, 1)

    def test_restarted_runtime_with_no_lease_does_not_renew(self):
        renewer, runtime, services = self._renewer()
        runtime.lease_snapshot = lambda: None
        self.assertEqual(renewer.tick(), {'code': 'no_lease'})
        self.assertEqual(runtime.calls, 0)
        self.assertEqual(renewer.recovery.calls, 1)
        self.assertEqual(services.calls, 1)

    def test_finished_close_allows_a_new_start(self):
        started = threading.Event()

        def renew_once():
            started.set()
            return {'code': 'suspend'}

        runtime = types.SimpleNamespace(
            lease_snapshot=lambda: {'holder': 'dell'}, renew_once=renew_once)
        renewer = LeaseRenewer(types.SimpleNamespace(runtime=runtime), interval=0.05)
        self.addCleanup(renewer.close)
        renewer.start()
        self.assertTrue(started.wait(1))
        thread = renewer.thread
        renewer.close()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(renewer.thread)
        started.clear()
        renewer.start()
        self.assertTrue(started.wait(1))
        self.assertIsNotNone(renewer.thread)

    def test_join_timeout_refuses_another_start(self):
        renewer = LeaseRenewer(types.SimpleNamespace(), interval=3)
        renewer.thread = _Stuck()
        renewer.close()
        self.assertIsInstance(renewer.thread, _Stuck)
        with self.assertRaises(RuntimeRefusal) as caught:
            renewer.start()
        self.assertEqual(caught.exception.code, 'bad_state')

    def test_close_before_start_does_not_stick_the_stop(self):
        started = threading.Event()

        def renew_once():
            started.set()
            return {'code': 'ok', 'renew_after': 5.0, 'remaining': 15}

        runtime = types.SimpleNamespace(
            lease_snapshot=lambda: {'holder': 'dell'}, renew_once=renew_once)
        renewer = LeaseRenewer(types.SimpleNamespace(runtime=runtime), interval=0.05)
        renewer.close()
        self.assertIsNone(renewer.thread)
        self.addCleanup(renewer.close)
        renewer.start()
        self.assertTrue(started.wait(1))


class _PolicyClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def self_policy(self):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def self_request(self):
        return None

    def self_step(self):
        return None


class SelfRecoveryGate(unittest.TestCase):
    def _preferred_recovery(self, *, lease_valid=True, transition=None):
        client = _PolicyClient([{'enabled': True, 'candidates': ['zenbook', 'dell'],
                                 'preferred': 'zenbook', 'stability_s': 30}])
        client.device_id = 'zenbook'
        client.authority_id = 'oracle-auth'
        client.requests = []
        client.steps = []
        def unexpected(*args, **kwargs):
            raise AssertionError('unrelated authority operation')
        for method in ('renew', 'release', 'acknowledge', 'readiness', 'step',
                       'transition_request', 'consume', 'admission'):
            setattr(client, method, unexpected)
        client.status = lambda: {'mode': 'V2_ACTIVE', 'main': 'dell' if lease_valid else None,
            'preferred': None, 'lease_valid': lease_valid, 'main_ready': lease_valid,
            'transition': transition, 'incarnation': 5, 'epoch': 35, 'policy_version': 1}
        def request(*args, **kwargs):
            client.requests.append((args, kwargs))
            return {'state': 'REQUESTED', 'candidate': 'zenbook', 'transition_id': 'return-1',
                    'action': kwargs.get('action', args[1] if len(args) > 1 else None)}
        client.self_request = request
        client.self_step = lambda *args, **kwargs: client.steps.append((args, kwargs))
        clock = {'t': 0.0}
        runtime = types.SimpleNamespace(device_id='zenbook', _started=True,
            readiness=lambda profile: {'meets': True, 'control_ready': True, 'checkpoint': 'cp-live'})
        recovery = SelfRecovery(types.SimpleNamespace(runtime=runtime, client=client,
                                                       canary=lambda: True), clock=lambda: clock['t'])
        return recovery, client, clock

    def test_preferred_pc_cannot_return_over_live_main_without_safe_handoff(self):
        recovery, client, clock = self._preferred_recovery()
        for elapsed in (0, 30, 300, 3600):
            # Refill policy before its bounded refresh; preference never grants a handoff.
            client.answers.append({'enabled': True, 'candidates': ['zenbook', 'dell'],
                                   'preferred': 'zenbook', 'stability_s': 30})
            clock['t'] = elapsed
            self.assertEqual(recovery.tick()['code'], 'safe_handoff_required')
        self.assertFalse(recovery._orchestrator.policy.auto_failback)
        self.assertEqual(client.requests, [])
        self.assertEqual(client.steps, [])

    def test_metadata_only_recovery_cannot_resume_an_open_preferred_return(self):
        for state in ('REQUESTED', 'DRAINING', 'FENCING', 'GRANTED_NOT_READY'):
            with self.subTest(state=state):
                recovery, client, _ = self._preferred_recovery(transition={
                    'action': 'set_preferred_main', 'candidate': 'zenbook',
                    'transition_id': 'return-1', 'state': state})
                self.assertEqual(recovery.tick()['code'], 'safe_handoff_required')
                self.assertEqual(client.requests, [])
                self.assertEqual(client.steps, [])

    def test_expired_main_recovery_remains_available_to_preferred_pc(self):
        recovery, client, _ = self._preferred_recovery(lease_valid=False)
        self.assertEqual(recovery.tick()['code'], 'requested')
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0][0][1], 'recover')

    def _recovery(self, canary, answers, clock):
        runtime = types.SimpleNamespace(
            device_id='dell',
            readiness=lambda profile: {'meets': True, 'control_ready': True})
        participant = types.SimpleNamespace(
            runtime=runtime, client=_PolicyClient(answers), canary=canary)
        return SelfRecovery(participant, clock=clock)

    def test_missing_canary_does_not_activate(self):
        clock = {'t': 0.0}
        recovery = self._recovery(None, [], lambda: clock['t'])
        self.assertFalse(recovery._activate('dell', {}))
        self.assertFalse(recovery._activate('zenbook', {}))
        recovery.participant.canary = lambda: False
        self.assertFalse(recovery._activate('dell', {}))
        recovery.participant.canary = lambda: 1
        self.assertFalse(recovery._activate('dell', {}))

        def broken():
            raise RuntimeError('probe down')

        recovery.participant.canary = broken
        self.assertFalse(recovery._activate('dell', {}))
        recovery.participant.canary = lambda: True
        self.assertTrue(recovery._activate('dell', {}))
        recovery.participant.runtime.readiness = lambda profile: {'meets': False}
        self.assertFalse(recovery._activate('dell', {}))

    def test_stale_policy_drops_when_refresh_fails(self):
        clock = {'t': 0.0}
        enabled = {
            'enabled': True, 'candidates': ['dell'], 'preferred': 'dell', 'stability_s': 30}
        recovery = self._recovery(
            lambda: True, [enabled, RuntimeError('gateway down')], lambda: clock['t'])
        self.assertEqual(
            recovery._refresh(), (('dell',), 'dell', 30))
        clock['t'] = 299.0
        self.assertEqual(recovery.participant.client.calls, 1)
        self.assertIsNotNone(recovery._refresh())
        clock['t'] = 300.0
        self.assertIsNone(recovery._refresh())
        self.assertIsNone(recovery._policy)
        self.assertIsNone(recovery._orchestrator)
        self.assertEqual(recovery.participant.client.calls, 2)


class Constructor(unittest.TestCase):
    def test_rejects_interval_outside_the_open_zero_to_five_range(self):
        for bad in (0, -1, -0.1, 5.1, 6, True, False, '3', None):
            with self.assertRaises(RuntimeRefusal) as caught:
                LeaseRenewer(object(), interval=bad)
            self.assertEqual(caught.exception.code, 'bad_request', bad)
        accepted = LeaseRenewer(object(), interval=5)
        self.assertEqual(accepted.interval, 5)
        self.assertEqual(LeaseRenewer(object(), interval=0.5).interval, 0.5)


if __name__ == '__main__':
    unittest.main()
