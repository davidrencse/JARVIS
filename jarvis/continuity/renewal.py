"""Renew an already accepted lease; never request, recover, elect, or promote."""
import threading

from .runtime import RuntimeRefusal


class LeaseRenewer:
    def __init__(self, participant, *, interval=3):
        if type(interval) not in (int, float) or not 0 < interval <= 5:
            raise RuntimeRefusal('bad_request')
        self.participant, self.interval = participant, interval
        self.stop = threading.Event()
        self.thread = None

    def tick(self):
        if self.participant.runtime.lease_snapshot() is None:
            return {'code': 'no_lease'}
        return self.participant.runtime.renew_once()

    def _run(self):
        while not self.stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                # The runtime/guard still validate authority at every effect.
                # An unreachable gateway does not kill the hub or open a lease.
                pass

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
