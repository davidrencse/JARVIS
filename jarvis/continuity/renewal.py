"""Renew an accepted lease and, when the gateway allows it, recover MAIN for this node.

Renewal never requests, elects or promotes. Self-recovery is a separate step on
the same tick (`SelfRecovery`): it does nothing unless the gateway config turns
`self_recovery` on and lists this PC. Then this node opens a `recover`
transition for itself once the old MAIN's lease has lapsed. That path is
metadata only, so it never returns MAIN to the preferred PC (`_RecoveryOnly`).

The return is `AutoReturn`, on the same tick of the CURRENT MAIN: once the
preferred PC has been steadily ready for the stability window it calls the
bound SafeHandoff (the same brain transfer, signed repoint and canary as
`jarvis continuity main <preferred>`). The authority's fence still waits out
the old lease, so two MAINs never hold valid leases.
"""
import threading
import time

from .runtime import RecoveryOrchestrator, RuntimePolicy, RuntimeRefusal, SelfRecoveryPort, _step

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


# How long a gateway's self-recovery answer is trusted before asking again.
_POLICY_TTL = 300.0


# After a handoff to this PC (`set_preferred_main`) disappears without making
# it MAIN, the source PC holds the newest brain. Give it this long to reclaim
# MAIN before this PC may `recover` for itself with older memories.
_SOURCE_FIRST = 120.0


class _RecoveryOnly(RecoveryOrchestrator):
    """Production's local canary cannot substitute for SafeHandoff's data and route steps."""

    _return_seen_at = None

    def _recover(self, status):
        seen = self._return_seen_at
        if seen is not None and self._clock() - seen < _SOURCE_FIRST:
            return _step(False, 'standby', 'source_first')
        return super()._recover(status)

    def _failback(self, status):
        target = status.get('preferred') or self.policy.preferred
        if target == self._candidate.device_id and target != status.get('main'):
            return _step(False, 'main', 'safe_handoff_required', candidate=target)
        return super()._failback(status)

    def _advance(self, status, transition):
        # A restart must not resume an earlier metadata-only automatic return.
        # Leave explicit handoff coordination to its composed SafeHandoff owner.
        if transition.get('action') == 'set_preferred_main':
            self._return_seen_at = self._clock()
            return _step(False, 'standby', 'safe_handoff_required', transition=transition)
        return super()._advance(status, transition)


class SelfRecovery:
    """Runs one `RecoveryOrchestrator.step` per tick for this node only.

    Inert unless the participant has a signing client with the self-recovery
    calls and the gateway answers `enabled`. The orchestrator is node-owned
    (`candidate_runtime`): it only advances transitions where this node is the
    candidate, except cancelling a dead candidate's not-ready grant. Readiness
    is this node's own probe; activation is the participant's canary.
    A missing canary does not count as success. Failback is refused here:
    this adapter has no brain-transfer or signed-repoint composition. The
    current MAIN's `AutoReturn` runs the composed SafeHandoff instead.
    """

    def __init__(self, participant, *, clock=None):
        self.participant = participant
        client = getattr(participant, 'client', None)
        runtime = getattr(participant, 'runtime', None)
        self._usable = (client is not None and runtime is not None
                        and callable(getattr(client, 'self_policy', None))
                        and callable(getattr(client, 'self_request', None))
                        and callable(getattr(client, 'self_step', None)))
        self._clock = clock or getattr(client, '_wall', None) or time.time
        self._policy = None
        self._policy_at = None
        self._orchestrator = None

    def _refresh(self):
        now = self._clock()
        if self._policy_at is not None and now - self._policy_at < _POLICY_TTL:
            return self._policy
        try:
            answer = self.participant.client.self_policy()
        except Exception:
            # The cached answer is already past its TTL. A failed refresh must
            # not keep an enabled policy forever.
            self._policy, self._orchestrator, self._policy_at = None, None, None
            return None
        self._policy_at = now
        if not isinstance(answer, dict) or answer.get('enabled') is not True:
            self._policy, self._orchestrator = None, None
            return None
        key = (tuple(answer.get('candidates') or ()), answer.get('preferred'), answer.get('stability_s'))
        if self._policy != key:
            self._policy = key
            self._orchestrator = None
        return self._policy

    def policy(self):
        """The cached gateway self-recovery key, or None when it is off or unusable."""
        if not self._usable:
            return None
        return self._refresh()

    def _build(self, key):
        runtime = self.participant.runtime
        me = runtime.device_id
        candidates, preferred, stability = key
        if me not in candidates:
            return None
        policy = RuntimePolicy(
            preferred=preferred, recover_candidates=(me,), auto_recover=True,
            auto_failback=False, failback_stability_s=float(stability or 30.0),
            caller='self-recovery')
        return _RecoveryOnly(
            SelfRecoveryPort(self.participant.client), self, policy, self._clock,
            candidate_runtime=runtime, activate=self._activate)

    def read(self, device):
        """Directory for the orchestrator: this node's own facts, nobody else's."""
        runtime = self.participant.runtime
        if device != runtime.device_id:
            return None
        try:
            view = runtime.readiness('control')
        except Exception:
            return None
        facts = dict(view)
        facts.update(kind='pc', observed_at=self._clock())
        facts.pop('reason', None)
        return facts

    def _activate(self, device, snapshot):
        runtime = self.participant.runtime
        if device != runtime.device_id:
            return False
        try:
            if runtime.readiness('control').get('meets') is not True:
                return False
        except Exception:
            return False
        canary = getattr(self.participant, 'canary', None)
        if not callable(canary):
            return False
        try:
            return canary() is True
        except Exception:
            return False

    def tick(self):
        if not self._usable:
            return None
        key = self._refresh()
        if key is None:
            return None
        if self._orchestrator is None:
            self._orchestrator = self._build(key)
            if self._orchestrator is None:
                return None
        if getattr(self.participant.runtime, '_started', True) is not True:
            return None  # a node that has not started its runtime cannot serve as MAIN
        return self._orchestrator.step()


# After a failed or uncertain automatic return, wait this long before trying
# again. A fresh stability window is still required after the wait.
_RETURN_BACKOFF = 300.0
# A handoff answer that means "still working on it": call again next tick and
# let SafeHandoff resume its own journal instead of opening anything new.
_RETURN_RESUME = frozenset({'pending'})


def _auto_return_enabled(controller):
    config = getattr(controller, 'config', None)
    continuity = config.get('continuity') if isinstance(config, dict) else None
    if isinstance(continuity, dict) and continuity.get('auto_return') is False:
        return False
    return True


class AutoReturn:
    """On the current MAIN: give MAIN back to the preferred PC once it is steadily ready.

    This is the manual `jarvis continuity main <preferred>` path, driven by the
    renewal tick instead of the owner: it calls the participant's bound
    handoff (`ProductionController`, i.e. SafeHandoff with brain transfer,
    signed repoint and canary) with this node as the source. Nothing here
    opens a transition by itself, and the metadata-only `_RecoveryOnly` path
    still refuses failback on the preferred PC.

    Inert unless the gateway's self-recovery policy is on and names a
    different preferred PC, this node holds a valid, ready lease with no
    transition open, and a bound handoff with `candidate_readiness` exists.
    The preferred PC's validated readiness must hold for the policy's
    stability window. A pending handoff is resumed on the next tick; any other
    failure leaves SafeHandoff's own outcome (it keeps this node MAIN until
    the fence) and backs off before a fresh window starts.
    Off switch: `continuity.auto_return: false` in the bound config.
    """

    def __init__(self, participant, recovery, *, clock=None):
        self.participant = participant
        self._recovery = recovery
        client = getattr(participant, 'client', None)
        self._clock = clock or getattr(client, '_wall', None) or time.time
        self._stable_since = None
        self._target = None
        self._transition_id = None
        self._backoff_until = None
        self.last_result = None
        self._worker = None
        # Direct `tick()` callers (tests) run the handoff inline; the
        # renewer's own thread sets this False so renewal never waits on it.
        self._sync = True

    def _reset(self):
        self._stable_since = None
        self._target = None

    def _controller(self):
        controller = getattr(self.participant, 'production_controller', None)
        if not callable(controller) or not callable(getattr(controller, 'candidate_readiness', None)):
            return None
        if not _auto_return_enabled(controller):
            return None
        return controller

    def _ready(self, controller, target):
        try:
            facts = controller.candidate_readiness(target)
        except Exception:
            return False
        return (isinstance(facts, dict) and facts.get('control_ready') is True
                and facts.get('meets') is not False and bool(facts.get('checkpoint')))

    def _handoff(self, controller, me, target):
        # The brain transfer can outlast a lease. Run it off the renewer
        # thread so this node keeps renewing until the authority fences it.
        worker = self._worker
        if worker is not None and worker.is_alive():
            return {'code': 'auto_return_running', 'candidate': target}
        if self._sync:
            return self._run_handoff(controller, me, target)
        self._worker = threading.Thread(
            target=self._run_handoff, args=(controller, me, target),
            name='jarvis-auto-return', daemon=True)
        self._worker.start()
        return {'code': 'auto_return_started', 'candidate': target}

    def _run_handoff(self, controller, me, target):
        try:
            result = controller(me, target, getattr(controller, 'config', None),
                                getattr(controller, 'base', None))
        except Exception as exc:
            result = {'ok': False, 'status': 'refused', 'code': getattr(exc, 'code', 'error')}
        if not isinstance(result, dict):
            result = {'ok': False, 'status': 'refused', 'code': 'bad_result'}
        self.last_result = result
        if result.get('ok') is True:
            self._transition_id = None
            self._reset()
        elif result.get('status') in _RETURN_RESUME and result.get('transition_id'):
            self._transition_id = result.get('transition_id')
        else:
            self._transition_id = None
            self._backoff_until = self._clock() + _RETURN_BACKOFF
            self._reset()
        return {'code': 'auto_return', 'result': result}

    def tick(self):
        controller = self._controller()
        key = self._recovery.policy() if controller is not None else None
        if key is None:
            self._reset()
            return None
        candidates, preferred, stability = key
        me = self.participant.runtime.device_id
        try:
            status = self.participant.client.status()
        except Exception:
            self._reset()
            return None
        if not isinstance(status, dict):
            self._reset()
            return None
        target = status.get('preferred') or preferred
        transition = status.get('transition') or {}
        if transition:
            self._reset()
            # Resume only the handoff this node opened. Anything else belongs
            # to its own coordinator (an owner command, a recovery).
            if (self._transition_id is not None
                    and transition.get('transition_id') == self._transition_id
                    and transition.get('action') == 'set_preferred_main'
                    and transition.get('candidate') == target):
                return self._handoff(controller, me, target)
            return None
        self._transition_id = None
        if (not target or target == me or target not in candidates
                or status.get('mode') != 'V2_ACTIVE' or status.get('lease_valid') is not True
                or status.get('main') != me or status.get('main_ready') is not True):
            self._reset()
            return None
        now = self._clock()
        if self._backoff_until is not None and now < self._backoff_until:
            self._reset()
            return {'code': 'backoff', 'until': self._backoff_until}
        if not self._ready(controller, target):
            self._reset()
            return {'code': 'not_ready', 'candidate': target}
        if self._target != target or self._stable_since is None:
            self._target, self._stable_since = target, now
        stable = now - self._stable_since
        if stable < float(stability or 30.0):
            return {'code': 'stabilising', 'candidate': target, 'stable_for': stable}
        return self._handoff(controller, me, target)


class LeaseRenewer:
    def __init__(self, participant, *, interval=3):
        if type(interval) not in (int, float) or not 0 < interval <= _MAX_INTERVAL:
            raise RuntimeRefusal('bad_request')
        self.participant, self.interval = participant, interval
        self.stop = threading.Event()
        self.thread = None
        self.recovery = SelfRecovery(participant)
        self.auto_return = AutoReturn(participant, self.recovery)
        self.last_recovery = None
        self.last_return = None
        self.last_rejoin = None
        self.last_error = None

    def _suspended(self):
        """True while this PC has released for sleep and must not extend a lease."""
        runtime = getattr(self.participant, 'runtime', None)
        return getattr(runtime, '_block', None) == 'suspend'

    def tick(self):
        if self._suspended():
            # Sleep already invalidated the cache. Do not renew, recover, or
            # tell services this node is still MAIN.
            self.last_recovery = None
            self.last_return = None
            return {'code': 'suspend'}
        try:
            self.last_recovery = self.recovery.tick()
        except Exception as exc:
            # Recovery must never stop renewal of a lease this node already holds.
            self.last_recovery = {'code': 'error', 'error': type(exc).__name__}
        try:
            self.last_return = self.auto_return.tick()
        except Exception as exc:
            # Same rule: a failed return attempt never stops renewal.
            self.last_return = {'code': 'error', 'error': type(exc).__name__}
        if self.participant.runtime.lease_snapshot() is None:
            # A restarted process has no grant in RAM and must not renew one.
            result = {'code': 'no_lease'}
        else:
            result = self.participant.runtime.renew_once()
        services = getattr(self.participant, 'services', None)
        if services is not None and callable(getattr(services, 'sync', None)):
            try:
                services.sync(self.participant.runtime)  # a demoted node stops serving
            except Exception:
                pass
        controller = getattr(self.participant, 'production_controller', None)
        rejoin = getattr(controller, 'rejoin_recovered_main', None)
        if callable(rejoin):
            try:
                self.last_rejoin = rejoin()
            except Exception as exc:
                self.last_rejoin = {'ok': False, 'code': getattr(exc, 'code', 'unavailable')}
        return result

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
                self.last_error = None
            except RuntimeRefusal as exc:
                # renew_once already returns partition and authority codes.
                # A refusal here is a poll, not a schedule and not a new lease.
                result = {'code': exc.code, 'wait': None}
                self.last_error = exc.code
            except Exception as exc:
                # The runtime/guard still validate authority at every effect.
                # An unreachable gateway does not kill the hub or open a lease.
                result = None
                self.last_error = type(exc).__name__
            delay = self._next_delay(result)

    def start(self):
        if self.thread is not None:
            raise RuntimeRefusal('bad_state')
        self.stop.clear()
        self.auto_return._sync = False
        self.thread = threading.Thread(target=self._run, name='continuity-lease-renewal', daemon=True)
        self.thread.start()
        return self

    def close(self):
        """Stop the thread. A finished join can start again; a stuck one cannot."""
        thread = self.thread
        if thread is None:
            return
        self.stop.set()
        thread.join(timeout=2)
        if thread.is_alive():
            return
        self.thread = None
        self.stop.clear()


def start_bound(state, config=None, base=None):
    """Existing application lifecycle only; absent/incomplete binding is inert.

    With `config`, a continuity-managed PC that is not bound yet binds itself first
    (`startup.bind_managed`); an unmanaged PC stays untouched.
    """
    from .participant import lookup
    from .integration import production_binding
    if config is not None and lookup(state) is None:
        from .startup import bind_managed
        bind_managed(config, state, base)
    participant, binding = lookup(state), production_binding(state)
    if (participant is None or not binding or binding.get('runtime') is not participant.runtime or
            binding.get('executor') is not participant.guard or not callable(binding.get('issuer'))):
        return None
    return LeaseRenewer(participant).start()
