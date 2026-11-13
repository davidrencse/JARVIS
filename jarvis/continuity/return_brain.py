"""Explicit, hash-bound disposition of a frozen former MAIN before a return.

The normal brain receive/conflict and freeze implementations are unchanged.
Preparation is a local operator action with both role locks held; nothing is
archived automatically on receipt of a peer job.

A finished ``activated`` or ``refrozen`` approval can start another cycle: the
old file is renamed into its archive, never deleted. ``archiving`` and
``needs_review`` stay a review refusal until ``retire_interrupted`` parks that
file beside the archive. Activation records the grant before the freeze moves.
Refreeze records an in-progress phase before the marker is restored.
"""
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from .runtime import RuntimeRefusal
from .repoint import _read, _regular

FILE = 'brainsync-return-approval.json'
_PHASES = ('archiving', 'needs_review', 'prepared', 'received', 'activated', 'refreezing', 'refrozen')
_TERMINAL = ('activated', 'refrozen')
_REVIEW = ('archiving', 'needs_review')
_RESIDUE = frozenset({FILE, 'freeze.json', 'memory-backup.sqlite3'})


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _blob(path):
    _regular(path)
    return path.read_bytes()


def _present(path):
    return path.is_symlink() or path.exists()


def _save_durable(path, value):
    """Fsync the approval before any freeze byte moves."""
    from jarvis.fsio import atomic_write
    _regular(path)
    atomic_write(path, json.dumps(value, sort_keys=True), mode=0o600, fsync=True)


def _approval(state):
    import re
    from .runtime import _ident_ok
    record = _read(Path(state)/FILE)
    required = {'schema', 'phase', 'device', 'source', 'authority_id', 'fleet_id', 'incarnation',
                'prepared_epoch', 'archive', 'incoming_memory_sha256', 'current_memory_sha256', 'freeze_sha256'}
    if (set(record) - required - {'bundle_sha256', 'grant'} or required - set(record) or
            record['schema'] != 'jarvis-return-approval/1' or
            record['phase'] not in _PHASES or
            not isinstance(record['archive'], str) or not re.fullmatch(r'return-archive-[a-f0-9]{16}-[a-f0-9]{16}', record['archive']) or
            any(not _ident_ok(record[k]) for k in ('device', 'source', 'authority_id', 'fleet_id')) or
            any(type(record[k]) is not int or record[k] < 0 for k in ('incarnation', 'prepared_epoch')) or
            any(not isinstance(record[k], str) or not re.fullmatch('[a-f0-9]{64}', record[k])
                for k in ('incoming_memory_sha256', 'current_memory_sha256', 'freeze_sha256'))):
        raise RuntimeRefusal('bad_state')
    return record


def _valid_memory(memory):
    import sqlite3
    import tempfile
    if not memory.startswith(b'SQLite format 3\x00'):
        return False
    with tempfile.TemporaryDirectory(prefix='return-brain-check-') as root:
        path = Path(root)/'memory.sqlite3'; path.write_bytes(memory)
        try:
            db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
            try:
                return db.execute('pragma quick_check').fetchall() == [('ok',)]
            finally:
                db.close()
        except sqlite3.Error:
            return False


def _live_names(base):
    from jarvis import brainsync
    return ('memory.sqlite3', 'memory.sqlite3-wal', 'memory.sqlite3-shm',
            brainsync._pending_json(base).name, brainsync._pending_bin(base).name)


def _park_terminal(state, record):
    """Rename a finished approval into the archive it names. Do not rewrite it."""
    archive = state/record['archive']
    _regular(archive)
    if not archive.is_dir() or archive.is_symlink():
        raise RuntimeRefusal('bad_state')
    dest = archive/FILE
    if _present(dest):
        raise RuntimeRefusal('reconcile')
    _regular(dest)
    approval = state/FILE
    _regular(approval)
    approval.rename(dest)


def _claim_archive(archive):
    """Create the archive, or reuse one that holds only a retired approval and copies.

    A live database name inside the directory is a partial move. Leave it.
    """
    _regular(archive)
    if archive.is_symlink():
        raise RuntimeRefusal('bad_state')
    if not archive.exists():
        archive.mkdir(mode=0o700)
        return
    if not archive.is_dir():
        raise RuntimeRefusal('bad_state')
    names = []
    for child in archive.iterdir():
        if child.is_symlink() or not child.is_file():
            raise RuntimeRefusal('reconcile')
        names.append(child.name)
    if set(names) - _RESIDUE or (names and FILE not in names):
        raise RuntimeRefusal('reconcile')


def _hashes_match(record, path):
    if path.is_symlink() or not path.is_file():
        raise RuntimeRefusal('bad_state')
    return _sha(_blob(path)) == record['freeze_sha256']


def prepare(config, state, client, bundle_path, *, incoming_sha256, current_sha256, freeze_sha256, confirmed=False):
    """Archive the old local DB only after explicit operator hash confirmation.

    All raw database/WAL/SHM bytes and previous bundle artifacts are retained.
    The freeze marker stays active until a matching new grant activates the return.
    A previous terminal approval is parked in its own archive before the new record.
    """
    from jarvis import brainsync
    from jarvis.locking import exclusive_lock
    from jarvis.topology import identity
    from jarvis.transfer import _within
    from .brain_channel import _outside_git
    state = Path(state).resolve(); base = state.parent
    local = identity(config)[0]
    if not confirmed or client.device_id != local:
        raise RuntimeRefusal('confirmation_required')
    where = _within(config, str(bundle_path))
    path = Path(where['path']); _outside_git(path)
    blob = _blob(path)
    header, _, memory = brainsync._unbundle(blob)
    if (not brainsync._header_ok(header, local, local) or _sha(memory) != header.get('memory_sha256')
            or not _valid_memory(memory)):
        raise RuntimeRefusal('bad_request')
    if _sha(memory) != incoming_sha256:
        raise RuntimeRefusal('conflict')
    with ExitStack() as locks:
        locks.enter_context(exclusive_lock(state/'cluster-main.lock'))
        locks.enter_context(exclusive_lock(state/'cluster-node.lock'))
        locks.enter_context(brainsync._Lock(base))
        approval = state/FILE
        terminal = None
        if _present(approval):
            existing = _approval(state)
            if existing['phase'] not in _TERMINAL:
                raise RuntimeRefusal('reconcile')
            terminal = existing
        status = client.status()
        if status.get('transition') or (status.get('lease_valid') and status.get('main') == local):
            raise RuntimeRefusal('main_alive')
        marker_path = brainsync._marker_path(base)
        if not marker_path.is_file() or marker_path.is_symlink():
            raise RuntimeRefusal('conflict')
        marker_bytes = _blob(marker_path); marker = brainsync._read_marker(base)
        if (not marker or marker.get('corrupt') or marker.get('old_main') != local or
                marker.get('new_main') != header['old_main'] or _sha(marker_bytes) != freeze_sha256):
            raise RuntimeRefusal('conflict')
        snapshot, why = brainsync._stable_memory(state/'memory.sqlite3')
        if snapshot is None or why or _sha(snapshot) != current_sha256:
            raise RuntimeRefusal('source_changed')
        names = _live_names(base)
        for name in names:
            _regular(state/name)
        if terminal is not None:
            _park_terminal(state, terminal)
        archive_name = 'return-archive-'+freeze_sha256[:16]+'-'+current_sha256[:16]
        archive = state/archive_name
        _claim_archive(archive)
        record = {'schema': 'jarvis-return-approval/1', 'phase': 'archiving',
                  'device': local, 'source': header['old_main'], 'authority_id': client.authority_id,
                  'fleet_id': status['fleet_id'], 'incarnation': status['incarnation'],
                  'prepared_epoch': status['epoch'], 'archive': archive_name,
                  'incoming_memory_sha256': incoming_sha256, 'current_memory_sha256': current_sha256,
                  'freeze_sha256': freeze_sha256}
        # Intent precedes moves. A crash leaves an explicit refusal/review state.
        _save_durable(approval, record)
        try:
            brainsync._write_private(archive/'freeze.json', marker_bytes, 0o600)
            brainsync._write_private(archive/'memory-backup.sqlite3', snapshot, 0o600)
            fresh = client.status()
            if any(fresh.get(k) != status.get(k) for k in ('incarnation', 'epoch', 'main', 'lease_valid', 'transition')):
                raise RuntimeRefusal('stale')
            # Role locks and the original freeze remain in force throughout.
            for name in names:
                if (state/name).exists():
                    (state/name).rename(archive/name)
            record['phase'] = 'prepared'; _save_durable(approval, record)
        except Exception:
            record['phase'] = 'needs_review'; _save_durable(approval, record)
            raise
        return {'ok': True, 'status': 'return_prepared', 'archive': str(archive),
                'incoming_memory_sha256': incoming_sha256, 'freeze_preserved': True}


def retire_interrupted(config, state, client, *, incoming_sha256, current_sha256, freeze_sha256, confirmed=False):
    """Park an interrupted archiving or needs_review approval beside its archive.

    Requires the same three reviewed hashes. Does not move memory or the freeze.
    """
    from jarvis import brainsync
    from jarvis.locking import exclusive_lock
    from jarvis.topology import identity
    state = Path(state).resolve()
    local = identity(config)[0]
    if not confirmed or client.device_id != local:
        raise RuntimeRefusal('confirmation_required')
    with ExitStack() as locks:
        locks.enter_context(exclusive_lock(state/'cluster-main.lock'))
        locks.enter_context(exclusive_lock(state/'cluster-node.lock'))
        locks.enter_context(brainsync._Lock(state.parent))
        record = _approval(state)
        if record['phase'] not in _REVIEW:
            raise RuntimeRefusal('reconcile')
        if (incoming_sha256 != record['incoming_memory_sha256'] or
                current_sha256 != record['current_memory_sha256'] or
                freeze_sha256 != record['freeze_sha256']):
            raise RuntimeRefusal('conflict')
        archive = state/record['archive']
        _regular(archive)
        if _present(archive) and (archive.is_symlink() or not archive.is_dir()):
            raise RuntimeRefusal('bad_state')
        if not archive.exists():
            archive.mkdir(mode=0o700)
        dest = archive/FILE
        if _present(dest):
            raise RuntimeRefusal('reconcile')
        approval = state/FILE
        _regular(approval)
        approval.rename(dest)
        return {'ok': True, 'status': 'return_review_retired', 'archive': str(archive)}


def receive(config, state, blob):
    """Normal receive plus an approval check, never an override of memory_conflict."""
    from jarvis import brainsync
    state = Path(state); approval = state/FILE
    if not approval.exists():
        return brainsync.receive(config, state.parent, blob)
    with brainsync._Lock(state.parent):
        record = _approval(state)
        if record.get('phase') not in ('prepared', 'received'):
            raise RuntimeRefusal('reconcile')
        header, _, memory = brainsync._unbundle(blob)
        if (header.get('new_main') != record['device'] or header.get('old_main') != record['source'] or
                _sha(memory) != record['incoming_memory_sha256'] or
                _sha(_blob(brainsync._marker_path(state.parent))) != record['freeze_sha256']):
            raise RuntimeRefusal('conflict')
        result = brainsync._receive_unlocked(config, state.parent, blob)
        if result.get('ok') is True:
            record.update(phase='received', bundle_sha256=_sha(blob))
            _save_durable(approval, record)
        return result


def _return_grant(participant, record):
    status = participant.client.status(); transition = status.get('transition') or {}
    snap = participant.runtime.lease_snapshot()
    if (status.get('authority_id') != record['authority_id'] or status.get('fleet_id') != record['fleet_id'] or
            status.get('incarnation') != record['incarnation'] or status.get('epoch', -1) <= record['prepared_epoch'] or
            transition.get('state') != 'GRANTED_NOT_READY' or transition.get('source') != record['source'] or
            transition.get('candidate') != record['device'] or record['device'] != participant.device_id or
            status.get('main') != participant.device_id or status.get('main_ready') is not False or
            status.get('lease_valid') is not True or not snap or
            snap.get('holder') != participant.device_id or snap.get('epoch') != status['epoch'] or
            snap.get('incarnation') != status['incarnation']):
        raise RuntimeRefusal('stale')
    return status, {'transition_id': transition['transition_id'], 'incarnation': status['incarnation'],
                    'epoch': status['epoch']}


def activate(participant):
    """Only an already-fenced, not-ready return grant may release the old freeze."""
    from jarvis import brainsync
    from .integration import production_binding
    state = Path(participant.state); approval = state/FILE
    with brainsync._Lock(state.parent):
        record = _approval(state)
        binding = production_binding(state)
        if (not binding or binding.get('runtime') is not participant.runtime or
                binding.get('executor') is not participant.guard or
                not callable(binding.get('issuer'))):
            raise RuntimeRefusal('managed_unbound')
        _status, grant = _return_grant(participant, record)
        memory = brainsync.snapshot_memory(state/'memory.sqlite3')
        if _sha(memory) != record['incoming_memory_sha256']:
            raise RuntimeRefusal('memory_conflict')
        marker = brainsync._marker_path(state.parent)
        archive = state/record['archive']; _regular(archive)
        if not archive.is_dir() or archive.is_symlink():
            raise RuntimeRefusal('bad_state')
        archived = archive/'activated-freeze.json'
        phase = record.get('phase')
        if phase == 'activated':
            if record.get('grant') != grant:
                raise RuntimeRefusal('reconcile')
            # Both copies, or neither, is not a finished activation.
            if _present(marker) and _present(archived):
                raise RuntimeRefusal('reconcile')
            if _present(archived):
                if not _hashes_match(record, archived):
                    raise RuntimeRefusal('conflict')
                return {'ok': True, 'status': 'return_activated'}
            if not _present(marker) or not _hashes_match(record, marker):
                raise RuntimeRefusal('reconcile' if not _present(marker) else 'conflict')
            marker.rename(archived)
            return {'ok': True, 'status': 'return_activated'}
        if phase != 'received':
            raise RuntimeRefusal('reconcile')
        if _present(marker) and _present(archived):
            raise RuntimeRefusal('reconcile')
        if _present(marker):
            if not _hashes_match(record, marker):
                raise RuntimeRefusal('conflict')
            # Durable intent and the exact grant precede the rename.
            record.update(phase='activated', grant=grant); _save_durable(approval, record)
            marker.rename(archived)
            return {'ok': True, 'status': 'return_activated'}
        # The freeze already moved and the approval write did not. Finish that write.
        if not _present(archived) or not _hashes_match(record, archived):
            raise RuntimeRefusal('reconcile' if not _present(archived) else 'conflict')
        record.update(phase='activated', grant=grant); _save_durable(approval, record)
        return {'ok': True, 'status': 'return_activated'}


def _refreeze_status_ok(participant, record, status):
    grant = record.get('grant') or {}
    return (participant.client.device_id == record['device'] and
            record.get('phase') in ('activated', 'refreezing', 'refrozen') and
            status.get('authority_id') == record['authority_id'] and
            status.get('incarnation') == grant.get('incarnation') and
            status.get('epoch', -1) > grant.get('epoch', -1) and
            not status.get('lease_valid') and status.get('mode') == 'V2_ONLY_NO_MAIN')


def refreeze(participant):
    """Compensation preserves incoming memory after a failed return canary."""
    from jarvis import brainsync
    from jarvis.platforms import on_windows
    state = Path(participant.state)
    with brainsync._Lock(state.parent):
        record = _approval(state); status = participant.client.status()
        if not _refreeze_status_ok(participant, record, status):
            raise RuntimeRefusal('stale')
        marker = brainsync._marker_path(state.parent)
        archive = state/record['archive']; _regular(archive)
        archived = archive/'activated-freeze.json'
        approval = state/FILE
        phase = record.get('phase')
        if phase == 'refrozen':
            if not _present(marker) or not _hashes_match(record, marker):
                raise RuntimeRefusal('reconcile' if not _present(marker) else 'conflict')
            return {'ok': True, 'status': 'return_refrozen', 'memory_preserved': True}
        if phase == 'activated' and _present(marker):
            # Restored marker, approval write lost. Do not rewrite matching bytes.
            if not _hashes_match(record, marker):
                raise RuntimeRefusal('conflict')
            record['phase'] = 'refrozen'; _save_durable(approval, record)
            return {'ok': True, 'status': 'return_refrozen', 'memory_preserved': True}
        if phase == 'activated':
            record['phase'] = 'refreezing'; _save_durable(approval, record)
            phase = 'refreezing'
        if phase != 'refreezing':
            raise RuntimeRefusal('stale')
        if _present(marker):
            if not _hashes_match(record, marker):
                raise RuntimeRefusal('conflict')
        else:
            if not _present(archived) or not _hashes_match(record, archived):
                raise RuntimeRefusal('reconcile' if not _present(archived) else 'conflict')
            mode = 0o600 if on_windows() else 0o444
            brainsync._write_private(marker, _blob(archived), mode)
        record['phase'] = 'refrozen'; _save_durable(approval, record)
        return {'ok': True, 'status': 'return_refrozen', 'memory_preserved': True}
