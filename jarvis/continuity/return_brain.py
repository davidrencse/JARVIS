"""Explicit, hash-bound disposition of a frozen former MAIN before a return.

The normal brain receive/conflict and freeze implementations are unchanged.
Preparation is a local operator action with both role locks held; nothing is
archived automatically on receipt of a peer job.
"""
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from .runtime import RuntimeRefusal
from .repoint import _read, _save, _regular

FILE = 'brainsync-return-approval.json'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _blob(path):
    _regular(path)
    return path.read_bytes()


def _approval(state):
    import re
    from .runtime import _ident_ok
    record = _read(Path(state)/FILE)
    required = {'schema', 'phase', 'device', 'source', 'authority_id', 'fleet_id', 'incarnation',
                'prepared_epoch', 'archive', 'incoming_memory_sha256', 'current_memory_sha256', 'freeze_sha256'}
    if (set(record) - required - {'bundle_sha256', 'grant'} or required - set(record) or
            record['schema'] != 'jarvis-return-approval/1' or
            record['phase'] not in ('archiving', 'needs_review', 'prepared', 'received', 'activated', 'refrozen') or
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


def prepare(config, state, client, bundle_path, *, incoming_sha256, current_sha256, freeze_sha256, confirmed=False):
    """Archive the old local DB only after explicit operator hash confirmation.

    All raw database/WAL/SHM bytes and previous bundle artifacts are retained.
    The freeze marker stays active until a matching new grant activates the return.
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
        if approval.exists():
            raise RuntimeRefusal('reconcile')
        marker_path = brainsync._marker_path(base)
        marker_bytes = _blob(marker_path); marker = brainsync._read_marker(base)
        if (not marker or marker.get('corrupt') or marker.get('old_main') != local or
                marker.get('new_main') != header['old_main'] or _sha(marker_bytes) != freeze_sha256):
            raise RuntimeRefusal('conflict')
        snapshot, why = brainsync._stable_memory(state/'memory.sqlite3')
        if snapshot is None or why or _sha(snapshot) != current_sha256:
            raise RuntimeRefusal('source_changed')
        status = client.status()
        if status.get('transition') or (status.get('lease_valid') and status.get('main') == local):
            raise RuntimeRefusal('main_alive')
        # No links/reparse points can enter the archival set, including sidecars.
        names = ['memory.sqlite3', 'memory.sqlite3-wal', 'memory.sqlite3-shm',
                 brainsync._pending_json(base).name, brainsync._pending_bin(base).name]
        for name in names:
            _regular(state/name)
        archive_name = 'return-archive-'+freeze_sha256[:16]+'-'+current_sha256[:16]
        archive = state/archive_name
        _regular(archive)
        archive.mkdir(mode=0o700, exist_ok=False)
        record = {'schema': 'jarvis-return-approval/1', 'phase': 'archiving',
                  'device': local, 'source': header['old_main'], 'authority_id': client.authority_id,
                  'fleet_id': status['fleet_id'], 'incarnation': status['incarnation'],
                  'prepared_epoch': status['epoch'], 'archive': archive_name,
                  'incoming_memory_sha256': incoming_sha256, 'current_memory_sha256': current_sha256,
                  'freeze_sha256': freeze_sha256}
        # Intent precedes moves. A crash leaves an explicit refusal/review state.
        _save(approval, record)
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
            record['phase'] = 'prepared'; _save(approval, record)
        except Exception:
            record['phase'] = 'needs_review'; _save(approval, record)
            raise
        return {'ok': True, 'status': 'return_prepared', 'archive': str(archive),
                'incoming_memory_sha256': incoming_sha256, 'freeze_preserved': True}


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
            _save(approval, record)
        return result


def activate(participant):
    """Only an already-fenced, not-ready return grant may release the old freeze."""
    from jarvis import brainsync
    from .integration import production_binding
    state = Path(participant.state); approval = state/FILE
    with brainsync._Lock(state.parent):
        record = _approval(state)
        status = participant.client.status(); transition = status.get('transition') or {}
        snap = participant.runtime.lease_snapshot()
        binding = production_binding(state)
        if (not binding or binding.get('runtime') is not participant.runtime or
                binding.get('executor') is not participant.guard or
                not callable(binding.get('issuer'))):
            raise RuntimeRefusal('managed_unbound')
        if (status.get('authority_id') != record['authority_id'] or status.get('fleet_id') != record['fleet_id'] or
                status.get('incarnation') != record['incarnation'] or status.get('epoch', -1) <= record['prepared_epoch'] or
                transition.get('state') != 'GRANTED_NOT_READY' or transition.get('source') != record['source'] or
                transition.get('candidate') != record['device'] or record['device'] != participant.device_id or
                status.get('main') != participant.device_id or status.get('main_ready') is not False or
                status.get('lease_valid') is not True or not snap or
                snap.get('holder') != participant.device_id or snap.get('epoch') != status['epoch'] or
                snap.get('incarnation') != status['incarnation']):
            raise RuntimeRefusal('stale')
        memory = brainsync.snapshot_memory(state/'memory.sqlite3')
        if _sha(memory) != record['incoming_memory_sha256']:
            raise RuntimeRefusal('memory_conflict')
        grant = {'transition_id': transition['transition_id'], 'incarnation': status['incarnation'], 'epoch': status['epoch']}
        if record.get('phase') == 'activated':
            marker = brainsync._marker_path(state.parent)
            if marker.exists() or marker.is_symlink() or record.get('grant') != grant:
                raise RuntimeRefusal('reconcile')
            return {'ok': True, 'status': 'return_activated'}
        if record.get('phase') != 'received':
            raise RuntimeRefusal('reconcile')
        marker = brainsync._marker_path(state.parent)
        if _sha(_blob(marker)) != record['freeze_sha256']:
            raise RuntimeRefusal('conflict')
        archive = state/record['archive']; _regular(archive)
        # Keep the freeze file, rather than deleting evidence of the first handoff.
        marker.rename(archive/'activated-freeze.json')
        record.update(phase='activated', grant=grant); _save(approval, record)
        return {'ok': True, 'status': 'return_activated'}


def refreeze(participant):
    """Compensation preserves incoming memory after a failed return canary."""
    from jarvis import brainsync
    state = Path(participant.state)
    with brainsync._Lock(state.parent):
        record = _approval(state); status = participant.client.status()
        grant = record.get('grant') or {}
        if (participant.client.device_id != record['device'] or record.get('phase') != 'activated' or status.get('authority_id') != record['authority_id'] or
                status.get('incarnation') != grant.get('incarnation') or status.get('epoch', -1) <= grant.get('epoch', -1) or
                status.get('lease_valid') or status.get('mode') != 'V2_ONLY_NO_MAIN'):
            raise RuntimeRefusal('stale')
        marker = brainsync._marker_path(state.parent)
        if marker.exists():
            raise RuntimeRefusal('reconcile')
        archive = state/record['archive']; _regular(archive)
        saved = _blob(archive/'activated-freeze.json')
        if _sha(saved) != record['freeze_sha256']:
            raise RuntimeRefusal('conflict')
        from jarvis.platforms import on_windows
        brainsync._write_private(marker, saved, 0o600 if on_windows() else 0o444)
        record['phase'] = 'refrozen'; _save(state/FILE, record)
        return {'ok': True, 'status': 'return_refrozen', 'memory_preserved': True}
