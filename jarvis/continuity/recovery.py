"""Manual initial lease coordinator. No timers, election, force flag or auto-recover."""
from .runtime import RuntimeRefusal


class ManualRecovery:
    """Owner steps plus actual participant readiness, durable fencing and canary.

    CandidatePort supplies locally signed readiness and participant fence RPCs.
    The candidate runtime must survive the CLI process (normally in the hub).
    canary must verify a fresh installation-signed receipt from that runtime.
    """
    def __init__(self, port, candidate, canary):
        if not callable(canary):
            raise RuntimeRefusal('bad_request')
        self.port, self.candidate, self.canary = port, candidate, canary

    def __call__(self, device, client_key):
        from .runtime import _ident_ok
        if device != self.candidate.device_id or not _ident_ok(client_key):
            raise RuntimeRefusal('candidacy_mismatch')
        status = self.port.status()
        current = status.get('transition') or {}
        if status.get('lease_valid') and not (current.get('action') == 'recover' and
                current.get('candidate') == device and current.get('state') == 'GRANTED_NOT_READY'):
            raise RuntimeRefusal('main_alive')
        if status.get('mode') not in ('V2_ONLY_NO_MAIN', 'HANDOVER'):
            raise RuntimeRefusal('bad_state')
        transition = status.get('transition')
        if not transition:
            self.port.transition_request({'caller': 'manual-recovery', 'client_key': client_key,
                'action': 'recover', 'candidate': device, 'expected_incarnation': status['incarnation'],
                'expected_epoch': status['epoch'], 'expected_policy': status['policy_version'], 'profile': 'control'})
            transition = self.port.status().get('transition') or {}
        if transition.get('candidate') != device or transition.get('action') != 'recover':
            raise RuntimeRefusal('candidacy_mismatch')
        tid = transition['transition_id']
        phase = transition.get('state')
        if phase in ('REQUESTED', 'VALIDATING'):
            self.port.transition_readiness(tid, self.candidate.readiness())
            # A false readiness is recorded by the candidate and drain refuses it.
            from .authority import AuthorityError
            try:
                transition = self.port.transition_step(tid, 'drain')
            except AuthorityError as exc:
                if exc.code in ('not_ready', 'profile'):
                    self.port.transition_step(tid, 'cancel')
                raise
            phase = transition['state']
        if phase == 'DRAINING':
            transition = self.port.transition_step(tid, 'fence')
            phase = transition['state']
        if phase == 'FENCING':
            lease = self.port.transition_step(tid, 'grant')
            # Lost grant/accept answers are never reconstructed from status. The
            # grant is already committed as GRANTED_NOT_READY, so a lease the
            # candidate cannot take is cancelled here; a retry then starts clean.
            try:
                self.candidate.accept_lease(lease)
            except Exception:
                self._cancel(tid)
                raise
            phase = 'GRANTED_NOT_READY'
        if phase != 'GRANTED_NOT_READY':
            raise RuntimeRefusal('bad_state')
        snapshot = self.candidate.lease_snapshot()
        fresh = self.port.status()
        if (not snapshot or snapshot.get('holder') != device or
                snapshot.get('incarnation') != fresh.get('incarnation') or
                snapshot.get('epoch') != fresh.get('epoch') or
                (fresh.get('transition') or {}).get('transition_id') != tid or
                fresh.get('lease_valid') is not True or fresh.get('main_ready') is not False):
            # A not-ready grant this candidate does not hold (for example after
            # a retry whose accept never happened) must not sit until something
            # else cancels it.
            self._cancel(tid)
            raise RuntimeRefusal('lease_unavailable')
        # A failed or unverifiable canary explicitly clears the not-ready grant.
        try:
            ok = self.canary(device) is True
        except Exception:
            ok = False
        self.port.transition_step(tid, 'complete', ok)
        final = self.port.status()
        ready = (ok and final.get('mode') == 'V2_ACTIVE' and final.get('lease_valid') is True
                 and final.get('main_ready') is True and final.get('main') == device
                 and final.get('incarnation') == snapshot['incarnation']
                 and final.get('epoch') == snapshot['epoch'] and not final.get('transition'))
        return {'ok': ready, 'status': 'ready' if ready else 'no_main',
                'transition_id': tid, 'main': final.get('main'),
                'incarnation': final.get('incarnation'), 'epoch': final.get('epoch')}

    def _cancel(self, tid):
        """Best-effort clear of this transition. The caller's error stays the answer."""
        try:
            self.port.transition_step(tid, 'cancel')
        except Exception:
            pass
