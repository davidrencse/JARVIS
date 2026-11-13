"""Renew an already accepted lease; never request, recover, elect, or promote."""
import threading

from .runtime import RuntimeRefusal

# Constructor interval stays in (0, 5]. A successful renew waits for the
# authority's renew_after, never past this cap, and never in a hot loop.
_MAX_INTERVAL = 5.0
_MIN_DELAY = 0.5
# These answers are not a renewal schedule. A stray renew_after on the same
# dict must not stretch the wait; the constructor interval is the poll.
_BLOCKED = frozenset({
    'no_lease', 'partition', 'draining', 'suspend', 'renewal_paused',
    'stopped', 'expired', 'stale', 'quarantined', 'legacy', 'unavailable',
    'writer_busy', 'not_holder', 'not_ready', 'no_main', 'busy',
})


def _positive_seconds(value):
    """True for a finite number above zero. Bool is not a duration."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value != value or value == float('inf'):
        return False
    return value > 0


class LeaseRenewer:
    def __init__(self, participant, *, interval=3):
        if type(interval) not in (int, float) or not 0 < interval <= _MAX_INTERVAL:
            raise RuntimeRefusal('bad_request')
        self.participant, self.interval = participant, interval
        self.stop = threading.Event()
        self.thread = None

    def tick(self):
        if self.participant.runtime.lease_snapshot() is None:
            return {'code': 'no_lease'}
        return self.participant.runtime.renew_once()

    def _next_delay(self, result):
        """Wait renew_after after a real renewal; otherwise poll at interval.

        The wait is at least 0.5 s and at most 5 s, and at most half of a
        positive ``remaining`` so the next renew stays inside the lease.
        When that half is itself under 0.5 s, the half wins.
        """
        if not isinstance(result, dict) or result.get('code') in _BLOCKED:
            return self.interval
        renew_after = result.get('renew_after')
        if not _positive_seconds(renew_after):
            return self.interval
        delay = min(float(renew_after), _MAX_INTERVAL)
        remaining = result.get('remaining')
        half = None
        if _positive_seconds(remaining):
            half = float(remaining) / 2.0
            delay = min(delay, half)
        floor = half if half is not None and half < _MIN_DELAY else _MIN_DELAY
        if delay < floor:
            return floor
        return delay

    def _run(self):
        delay = self.interval
        while not self.stop.wait(delay):
            try:
                result = self.tick()
            except Exception:
                # The runtime/guard still validate authority at every effect.
                # An unreachable gateway does not kill the hub or open a lease.
                result = None
            delay = self._next_delay(result)

    def start(self):
        if self.thread is not None:
            raise RuntimeRefusal('bad_state')
        self.thread = threading.Thread(target=self._run, name='continuity-lease-renewal', daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


def start_bound(state):
    """Existing application lifecycle only; absent/incomplete binding is inert."""
    from .participant import lookup
    from .integration import production_binding
    participant, binding = lookup(state), production_binding(state)
    if (participant is None or not binding or binding.get('runtime') is not participant.runtime or
            binding.get('executor') is not participant.guard or not callable(binding.get('issuer'))):
        return None
    return LeaseRenewer(participant).start()
