"""LeaseRenewer waits for the authority renew_after, and falls back to its interval."""

import threading
import time
import types
import unittest

from jarvis.continuity.renewal import _RETURN_BACKOFF, AutoReturn, LeaseRenewer, SelfRecovery
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


class _Controller:
    """Stand-in for the bound ProductionController (SafeHandoff)."""

    def __init__(self, results, ready=True):
        self.config, self.base = {}, '/tmp/jarvis-test'
        self.results, self.ready, self.calls = list(results), ready, []

    def candidate_readiness(self, device):
        if self.ready is None:
            raise RuntimeRefusal('partition')
        return {'control_ready': self.ready, 'ai_ready': False, 'checkpoint': 'cp-1'}

    def __call__(self, old, new, config, base):
        self.calls.append((old, new))
        return self.results.pop(0)


class AutoReturnOnCurrentMain(unittest.TestCase):
    def _auto(self, controller, *, main='dell', transition=None, preferred='zenbook'):
        clock = {'t': 0.0}
        client = _PolicyClient([{'enabled': True, 'candidates': ['zenbook', 'dell'],
                                 'preferred': preferred, 'stability_s': 30}] * 50)
        state = {'main': main, 'transition': transition}
        client.status = lambda: {'mode': 'V2_ACTIVE', 'main': state['main'], 'preferred': None,
                                 'lease_valid': True, 'main_ready': True,
                                 'transition': state['transition']}
        runtime = types.SimpleNamespace(device_id='dell', _started=True)
        participant = types.SimpleNamespace(runtime=runtime, client=client,
                                            production_controller=controller)
        recovery = SelfRecovery(participant, clock=lambda: clock['t'])
        return AutoReturn(participant, recovery, clock=lambda: clock['t']), clock, state

    def test_hands_back_through_the_bound_handoff_after_the_stability_window(self):
        controller = _Controller([{'ok': True, 'status': 'ready', 'transition_id': 't-1'}])
        auto, clock, _ = self._auto(controller)
        for elapsed in (0, 10, 29.5):
            clock['t'] = elapsed
            self.assertEqual(auto.tick()['code'], 'stabilising')
        self.assertEqual(controller.calls, [])
        clock['t'] = 30
        self.assertEqual(auto.tick()['result']['ok'], True)
        self.assertEqual(controller.calls, [('dell', 'zenbook')])

    def test_renewer_thread_runs_the_handoff_off_its_own_thread(self):
        release = threading.Event()

        class Slow(_Controller):
            def __call__(self, old, new, config, base):
                release.wait(5)
                return super().__call__(old, new, config, base)

        controller = Slow([{'ok': True, 'status': 'ready', 'transition_id': 't-1'}])
        auto, clock, _ = self._auto(controller)
        auto._sync = False
        auto.tick()
        clock['t'] = 30
        self.assertEqual(auto.tick()['code'], 'auto_return_started')
        self.assertEqual(auto.tick()['code'], 'auto_return_running')
        release.set()
        auto._worker.join(5)
        self.assertEqual(controller.calls, [('dell', 'zenbook')])
        self.assertTrue(auto.last_result['ok'])

    def test_a_readiness_miss_restarts_the_window(self):
        controller = _Controller([])
        auto, clock, _ = self._auto(controller)
        auto.tick()
        clock['t'] = 20
        controller.ready = None
        self.assertEqual(auto.tick()['code'], 'not_ready')
        controller.ready = True
        clock['t'] = 21
        auto.tick()
        clock['t'] = 45
        self.assertEqual(auto.tick()['code'], 'stabilising')
        self.assertEqual(controller.calls, [])

    def test_pending_handoff_resumes_its_own_transition_only(self):
        controller = _Controller([
            {'ok': False, 'status': 'pending', 'code': 'not_quiet', 'transition_id': 't-1'},
            {'ok': True, 'status': 'ready', 'transition_id': 't-1'}])
        auto, clock, state = self._auto(controller)
        auto.tick()
        clock['t'] = 30
        auto.tick()
        state['transition'] = {'transition_id': 't-1', 'action': 'set_preferred_main',
                               'candidate': 'zenbook', 'state': 'DRAINING'}
        clock['t'] = 31
        self.assertTrue(auto.tick()['result']['ok'])
        self.assertEqual(len(controller.calls), 2)
        state['transition'] = {'transition_id': 'owner-9', 'action': 'set_preferred_main',
                               'candidate': 'zenbook', 'state': 'DRAINING'}
        self.assertIsNone(auto.tick())
        self.assertEqual(len(controller.calls), 2)

    def test_failure_backs_off_then_needs_a_fresh_window(self):
        controller = _Controller([{'ok': False, 'status': 'no_main', 'code': 'brain'}])
        auto, clock, _ = self._auto(controller)
        auto.tick()
        clock['t'] = 30
        auto.tick()
        clock['t'] = 30 + _RETURN_BACKOFF - 1
        self.assertEqual(auto.tick()['code'], 'backoff')
        clock['t'] = 30 + _RETURN_BACKOFF
        self.assertEqual(auto.tick()['code'], 'stabilising')
        self.assertEqual(len(controller.calls), 1)

    def test_inert_without_bound_handoff_off_switch_or_when_not_main(self):
        auto, _, _ = self._auto(None)
        self.assertIsNone(auto.tick())
        controller = _Controller([])
        controller.config = {'continuity': {'auto_return': False}}
        auto, _, _ = self._auto(controller)
        self.assertIsNone(auto.tick())
        for kwargs in ({'main': 'zenbook'}, {'preferred': 'dell'}):
            auto, clock, _ = self._auto(_Controller([]), **kwargs)
            clock['t'] = 100
            self.assertIsNone(auto.tick())


class SourceFirstAfterFailedReturn(SelfRecoveryGate):
    def test_preferred_pc_waits_for_the_source_after_an_aborted_return(self):
        recovery, client, clock = self._preferred_recovery(transition={
            'action': 'set_preferred_main', 'candidate': 'zenbook',
            'transition_id': 'return-1', 'state': 'GRANTED_NOT_READY'})
        recovery.tick()
        client.status = lambda: {'mode': 'V2_ONLY_NO_MAIN', 'main': None, 'preferred': 'zenbook',
            'lease_valid': False, 'main_ready': False, 'transition': None,
            'incarnation': 5, 'epoch': 36, 'policy_version': 1}
        clock['t'] = 119
        self.assertEqual(recovery.tick()['code'], 'source_first')
        self.assertEqual(client.requests, [])
        clock['t'] = 120
        self.assertEqual(recovery.tick()['code'], 'requested')


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
