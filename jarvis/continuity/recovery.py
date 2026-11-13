"""Manual initial lease coordinator. No timers, election, force flag or auto-recover.

A client key is one attempt. The authority row is the outcome. This module
remembers only enough local metadata to send that same request again and to
avoid a second canary. It does not store a lease id, and a lost answer is not
rebuilt from status.
"""
import json
from pathlib import Path

from jarvis.fsio import atomic_write, read_json
from jarvis.locking import LockBusy, exclusive_lock

from .authority import AuthorityError
from .runtime import RuntimeRefusal, _ident_ok


_TERMINAL = frozenset(('SUCCEEDED', 'FAILED', 'CANCELLED'))
_NOTE_FIELDS = (
    'schema', 'client_key', 'candidate', 'expected_incarnation', 'expected_epoch',
    'expected_policy', 'transition_id', 'canary_started', 'canary',
)
_EXPECTED = ('expected_incarnation', 'expected_epoch', 'expected_policy')


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _leaky(value):
    return isinstance(value, str) and (value.startswith('ls-') or 'BEGIN ' in value)


def _note_ok(key, note):
    if not isinstance(note, dict) or set(note) - set(_NOTE_FIELDS) or _leaky(key):
        return False
    if note.get('schema') != 1 or note.get('client_key') != key or not _ident_ok(note.get('candidate')):
        return False
    if any(_leaky(value) for value in note.values()):
        return False
    for name in _EXPECTED:
        if name in note and not _is_int(note[name]):
            return False
    tid = note.get('transition_id')
    if tid is not None and not _ident_ok(tid):
        return False
    if 'canary_started' in note and not isinstance(note['canary_started'], bool):
        return False
    if 'canary' in note and not isinstance(note['canary'], bool):
        return False
    return True


def _public(ready, status_name, transition_id, status, already=False):
    result = {
        'ok': ready, 'status': status_name, 'transition_id': transition_id,
        'main': None if status is None else status.get('main'),
        'incarnation': None if status is None else status.get('incarnation'),
        'epoch': None if status is None else status.get('epoch'),
    }
    if already:
        result['already'] = True
    return result


class ManualRecovery:
    """Owner steps plus actual participant readiness, durable fencing and canary.

    CandidatePort supplies locally signed readiness and participant fence RPCs.
    The candidate runtime must survive the CLI process (normally in the hub).
    canary must verify a fresh installation-signed receipt from that runtime.

    ``receipts`` is an optional mode-0600 JSON file of request tuples and canary
    bits. The authority remains the source of a ready or rolled-back outcome.
    """
    def __init__(self, port, candidate, canary, *, receipts=None):
        if not callable(canary):
            raise RuntimeRefusal('bad_request')
        self.port, self.candidate, self.canary = port, candidate, canary
        self._receipts = None if receipts is None else Path(receipts)
        self._notes = {}
        if self._receipts is not None and self._receipts.is_symlink():
            raise RuntimeRefusal('bad_request')

    def __call__(self, device, client_key):
        if device != self.candidate.device_id or not _ident_ok(client_key):
            raise RuntimeRefusal('candidacy_mismatch')
        self._load()
        status = self.port.status()
        current = status.get('transition') or {}
        if not current:
            replay = self._open_or_replay(device, client_key, status)
            if replay is not None:
                return replay
            status = self.port.status()
            current = status.get('transition') or {}
        if status.get('lease_valid') and not (current.get('action') == 'recover' and
                current.get('candidate') == device and current.get('state') == 'GRANTED_NOT_READY'):
            raise RuntimeRefusal('main_alive')
        if status.get('mode') not in ('V2_ONLY_NO_MAIN', 'HANDOVER'):
            raise RuntimeRefusal('bad_state')
        if current.get('candidate') != device or current.get('action') != 'recover':
            raise RuntimeRefusal('candidacy_mismatch')
        self._capture(client_key, device, status, current)
        tid = current['transition_id']
        phase = current.get('state')
        if phase in ('REQUESTED', 'VALIDATING'):
            self.port.transition_readiness(tid, self.candidate.readiness())
            # A false readiness is recorded by the candidate and drain refuses it.
            try:
                current = self.port.transition_step(tid, 'drain')
            except AuthorityError as exc:
                if exc.code in ('not_ready', 'profile'):
                    self.port.transition_step(tid, 'cancel')
                raise
            phase = current['state']
        if phase == 'DRAINING':
            current = self.port.transition_step(tid, 'fence')
            phase = current['state']
        if phase == 'FENCING':
            lease = self.port.transition_step(tid, 'grant')
            # Lost grant answers are never reconstructed from status. A raised
            # accept cancels only when the candidate has no snapshot. A snapshot,
            # or a snapshot we cannot read, may already hold the grant.
            try:
                self.candidate.accept_lease(lease)
            except Exception:
                if not self._accept_landed(device):
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
        decided = self._canary_decision(client_key, tid)
        if decided is None:
            # The canary was entered and did not return. Do not run it again
            # and do not roll the grant back on a guess.
            return _public(False, 'uncertain', tid, fresh)
        if decided is _RUN:
            self._mark_canary_started(client_key, tid)
            try:
                ok = self.canary(device) is True
            except Exception:
                ok = False
            self._mark_canary(client_key, tid, ok)
        else:
            ok = decided
        try:
            self.port.transition_step(tid, 'complete', ok)
        except Exception:
            resolved = self._reconcile_complete(client_key, device, tid)
            if resolved is None:
                raise
            return resolved
        final = self.port.status()
        ready = (ok and final.get('mode') == 'V2_ACTIVE' and final.get('lease_valid') is True
                 and final.get('main_ready') is True and final.get('main') == device
                 and final.get('incarnation') == snapshot['incarnation']
                 and final.get('epoch') == snapshot['epoch'] and not final.get('transition'))
        return _public(ready, 'ready' if ready else 'no_main', tid, final)

    def _cancel(self, tid):
        """Best-effort clear of this transition. The caller's error stays the answer."""
        try:
            self.port.transition_step(tid, 'cancel')
        except Exception:
            pass

    def _accept_landed(self, device):
        try:
            held = self.candidate.lease_snapshot()
        except Exception:
            return True
        return isinstance(held, dict) and held.get('holder') == device

    def _open_or_replay(self, device, client_key, status):
        fields = self._fields_for(client_key, device, status)
        try:
            view = self.port.transition_request({
                'caller': 'manual-recovery', 'client_key': client_key, 'action': 'recover',
                'candidate': device, 'expected_incarnation': fields['expected_incarnation'],
                'expected_epoch': fields['expected_epoch'],
                'expected_policy': fields['expected_policy'], 'profile': 'control'})
        except AuthorityError as exc:
            if exc.code == 'main_alive':
                raise RuntimeRefusal('main_alive') from None
            if exc.code == 'key_reused':
                # The key was spent under a tuple this process no longer has.
                # Do not open a second effect and do not invent its outcome.
                raise RuntimeRefusal('key_reused') from None
            raise
        if not isinstance(view, dict):
            return None
        self._remember(client_key, device, fields, view.get('transition_id'))
        if view.get('state') not in _TERMINAL:
            return None
        if view.get('candidate') != device or view.get('action') != 'recover':
            raise RuntimeRefusal('candidacy_mismatch')
        fresh = self.port.status()
        ready = self._ready(view, fresh, device)
        return _public(ready, 'ready' if ready else 'no_main', view.get('transition_id'), fresh, already=True)

    def _reconcile_complete(self, client_key, device, tid):
        """Return the committed row when complete's answer was lost. None if still open."""
        try:
            fresh = self.port.status()
            view = self._lookup(client_key, device)
        except Exception:
            return None
        if not isinstance(view, dict) or view.get('transition_id') != tid or view.get('state') not in _TERMINAL:
            return None
        ready = self._ready(view, fresh, device)
        return _public(ready, 'ready' if ready else 'no_main', tid, fresh)

    def _lookup(self, client_key, device):
        note = self._notes.get(client_key)
        if note is None or not all(_is_int(note.get(name)) for name in _EXPECTED):
            return None
        view = self.port.transition_request({
            'caller': 'manual-recovery', 'client_key': client_key, 'action': 'recover',
            'candidate': device, 'expected_incarnation': note['expected_incarnation'],
            'expected_epoch': note['expected_epoch'], 'expected_policy': note['expected_policy'],
            'profile': 'control'})
        return view if isinstance(view, dict) else None

    def _ready(self, view, status, device):
        return (view.get('state') == 'SUCCEEDED' and status.get('mode') == 'V2_ACTIVE'
                and status.get('lease_valid') is True and status.get('main_ready') is True
                and status.get('main') == device and not status.get('transition'))

    def _fields_for(self, client_key, device, status):
        note = self._notes.get(client_key)
        if (note and note.get('candidate') == device
                and all(_is_int(note.get(name)) for name in _EXPECTED)):
            return {name: note[name] for name in _EXPECTED}
        return {
            'expected_incarnation': status['incarnation'],
            'expected_epoch': status['epoch'],
            'expected_policy': status['policy_version'],
        }

    def _capture(self, client_key, device, status, transition):
        if not all(_is_int(transition.get(name)) for name in ('expected_incarnation', 'expected_epoch')):
            return
        if not _is_int(status.get('policy_version')):
            return
        self._remember(client_key, device, {
            'expected_incarnation': transition['expected_incarnation'],
            'expected_epoch': transition['expected_epoch'],
            'expected_policy': status['policy_version'],
        }, transition.get('transition_id'))

    def _remember(self, client_key, device, fields, transition_id):
        note = dict(self._notes.get(client_key) or {
            'schema': 1, 'client_key': client_key, 'candidate': device, 'canary_started': False,
        })
        if note.get('candidate') not in (None, device):
            raise RuntimeRefusal('candidacy_mismatch')
        note['schema'] = 1
        note['client_key'] = client_key
        note['candidate'] = device
        for name in _EXPECTED:
            if name not in note and _is_int(fields.get(name)):
                note[name] = fields[name]
        if _ident_ok(transition_id):
            held = note.get('transition_id')
            if held is not None and held != transition_id:
                raise RuntimeRefusal('conflict')
            note['transition_id'] = transition_id
        if 'canary_started' not in note:
            note['canary_started'] = False
        self._write_note(client_key, note)

    def _canary_decision(self, client_key, tid):
        note = self._notes.get(client_key) or {}
        held = note.get('transition_id')
        if held is not None and held != tid:
            raise RuntimeRefusal('conflict')
        if isinstance(note.get('canary'), bool):
            return note['canary']
        if note.get('canary_started') is True:
            return None
        return _RUN

    def _mark_canary_started(self, client_key, tid):
        note = dict(self._notes.get(client_key) or {
            'schema': 1, 'client_key': client_key, 'candidate': self.candidate.device_id,
        })
        if note.get('transition_id') not in (None, tid):
            raise RuntimeRefusal('conflict')
        note['canary_started'] = True
        note['transition_id'] = tid
        note.pop('canary', None)
        self._write_note(client_key, note)

    def _mark_canary(self, client_key, tid, ok):
        note = dict(self._notes[client_key])
        note['canary_started'] = True
        note['transition_id'] = tid
        note['canary'] = ok is True
        self._write_note(client_key, note)

    def _load(self):
        if self._receipts is None:
            return
        if self._receipts.is_symlink():
            raise RuntimeRefusal('bad_request')
        self._notes = self._read_file()

    def _read_file(self):
        if self._receipts is None or not self._receipts.exists():
            return {}
        raw = read_json(self._receipts, None)
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items() if _ident_ok(key) and _note_ok(key, value)}

    def _write_note(self, client_key, note):
        body = {name: note[name] for name in _NOTE_FIELDS if name in note}
        self._notes[client_key] = body
        path = self._receipts
        if path is None:
            return
        if path.is_symlink():
            raise RuntimeRefusal('bad_request')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with exclusive_lock(path.with_name(path.name + '.lock')):
                current = self._read_file()
                current[client_key] = body
                atomic_write(path, json.dumps(current, sort_keys=True, separators=(',', ':')) + '\n',
                             mode=0o600, fsync=True)
        except LockBusy:
            raise RuntimeRefusal('busy') from None


_RUN = object()
