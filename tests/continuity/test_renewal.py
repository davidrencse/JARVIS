"""LeaseRenewer waits for the authority renew_after, and falls back to its interval."""

import threading
import time
import types
import unittest

from jarvis.continuity.renewal import LeaseRenewer
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
        for code in ('partition', 'draining', 'no_lease', 'renewal_paused'):
            blocked = {'code': code, 'renew_after': 5.0, 'remaining': 15}
            self.assertEqual(self.renewer._next_delay(blocked), 3, code)
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
        renewer.close()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(renewer.thread.is_alive())


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
