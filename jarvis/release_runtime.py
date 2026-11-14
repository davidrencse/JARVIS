"""Local update idle checks and resumable signed release transport."""
import json
import os
import sys
import time
from pathlib import Path

from .fsio import read_json, atomic_write

CHUNK = 256 * 1024
MAX_BUNDLE = 128 * 1024 * 1024


def loaded_code_status(hub):
    """Separate the process startup fingerprint from mutable disk/release code.

    ``code`` remains the offered release for older clients' update protocol.
    Missing startup evidence is never inferred from disk.
    """
    from .deploy import code_fingerprint
    from .release import served_code
    loaded = getattr(hub, 'loaded_code', None)
    served = served_code(hub.state)
    try:
        disk = code_fingerprint()
    except (OSError, ValueError):
        disk = None
    return {'code': served, 'served_code': served, 'loaded_code': loaded,
            'disk_code': disk,
            'restart_required': not loaded or loaded != served or loaded != disk}


def require_loaded_code(hub):
    """Refuse MAIN admission until this process runs the tested local release.

    Direct production callers must use this gate too, before readiness or brain
    transfer. A disk/release parity check alone cannot attest the running hub.
    """
    from .release import code_parity
    loaded = getattr(hub, 'loaded_code', None)
    parity = code_parity(hub.state, reported=loaded)
    if not loaded or parity.get('matched') is not True:
        raise ValueError('MAIN transfer requires the hub to run the tested local release. '
                         'Check release parity and restart jarvis-cluster before retrying. '
                         + (parity.get('instruction') or ''))
    return parity


def busy(state, *, hub=None, voice=False):
    from .sharedinput import remote_session_live, controlling_remote
    if remote_session_live(state) or controlling_remote(state):
        return True
    from .remote_view import _ACTIVE
    if _ACTIVE:
        return True
    if hub is not None:
        if any(s.get('state') != 'ended' for s in hub.remote.views()):
            return True
        if any(hub.broker.busy_job(n['id']) for n in hub.broker.nodes()):
            return True
    # Clipboard puts span several jobs; their .part files remain between chunks.
    from .daemon import _alive
    for marker in Path(state).glob("release-transfer-*.json"):
        try:
            if _alive(int(marker.read_text())):
                return True
        except (OSError, ValueError):
            pass
    from .transfer import drop_folder
    if any(time.time() - p.stat().st_mtime < 120 for p in drop_folder().glob('.jarvis-upload-*.part')):
        return True
    phase = read_json(Path(state) / 'continuity-role-phase.json', {})
    if phase and phase.get('phase') not in ('active', 'complete', 'completed', 'aborted'):
        return True
    for filename in ('handoff-journal.json', 'continuity-handoff.json'):
        journal = read_json(Path(state) / filename, {})
        if journal:
            complete = (journal.get('steps') or {}).get('complete', {})
            if complete.get('phase') != 'finished':
                return True
    if voice:
        console = getattr(hub, 'console', None)
        lock = getattr(console, '_voice_lock', None)
        if lock is not None and lock.locked():
            return True
        from .daemon import status
        if status(state).get('listening'):
            return True  # conservative: never interrupt an active voice daemon
    return False


def run_from_release(config):
    """cluster.run_from_release: the main's services run the tested release copy, not the checkout."""
    return bool((config.get('cluster') or {}).get('run_from_release', False))


def running_from(state, package=None):
    """Where this process imported jarvis from: 'release' (<state>/code-release/jarvis) or 'dev tree'."""
    from .deploy import PACKAGE
    from .release import release_dir
    try:
        return ('release' if Path(package or PACKAGE).resolve() == (release_dir(state) / 'jarvis').resolve()
                else 'dev tree')
    except OSError:
        return 'dev tree'


def run_source(config, state):
    """(ok, detail, fix) for the `cluster doctor` line "main runs from: release | dev tree"."""
    from .release import current
    where, want = running_from(state), run_from_release(config)
    code = current(state).get('code')
    if where == 'release':
        return True, f"release (code {code})", ''
    if not want:
        return True, 'dev tree (cluster.run_from_release is off)', ''
    if not code:
        return False, 'dev tree; cluster.run_from_release is on but no tested release exists', \
            'jarvis cluster release'
    return False, 'dev tree; cluster.run_from_release is on but this process was started from the checkout', \
        'jarvis cluster autostart on   (rewrites the unit), then restart jarvis-cluster'


def unit_runs_release(state, unit=None):
    """True when the installed jarvis-cluster unit loads the release copy, so a restart lands on it."""
    from .autostart import unit_file
    from .release import release_dir
    try:
        text = (Path(unit) if unit else unit_file('jarvis-cluster.service')).read_text(encoding='utf-8')
    except OSError:
        return False
    return 'PYTHONPATH=' + str(release_dir(state).resolve()) in text


def maybe_restart(hub):
    """Opt-in only. Never replace the main's dirty checkout with the release.

    With cluster.run_from_release the services import the release copy, so the checkout's state
    is irrelevant: restart whenever the loaded code differs from the release (busy() still gates
    it). Until the unit is switched (running from the checkout, unit not pointing at the
    release) a restart would load the same code again, so the old checkout rule applies."""
    from .release import current
    from .deploy import code_fingerprint
    note = current(hub.state)
    if not (hub.config.get('cluster') or {}).get('auto_restart', False) or not note:
        return False
    if note.get('code') == getattr(hub, 'loaded_code',
                                   getattr(hub, '_release_started_code', None)):
        return False
    lands_on_release = run_from_release(hub.config) and (
        running_from(hub.state) == 'release' or unit_runs_release(hub.state))
    if not lands_on_release and code_fingerprint() != note.get('code'):
        return False
    if busy(hub.state, hub=hub, voice=True):
        return False
    from .cluster import _systemd_service
    if _systemd_service('jarvis-cluster'):
        os._exit(75)  # existing Restart=on-failure policy, no unit mutation
    os.execv(sys.executable, [sys.executable, '-m', 'jarvis', *sys.argv[1:]])
    return True


def serve_chunk(hub, query):
    from .deploy import code_bundle
    from .release import served_code
    code = served_code(hub.state)
    cache = getattr(hub, '_release_chunk_cache', None)
    if cache is None or cache[0] != code:
        raw = json.dumps(code_bundle(hub.state), separators=(',', ':'))
        cache = hub._release_chunk_cache = (code, raw)
    offset = int(query.get('offset', ['0'])[0])
    if offset < 0 or offset > len(cache[1]):
        raise ValueError('Invalid release offset')
    return {'code': code, 'offset': offset, 'total': len(cache[1]),
            'data': cache[1][offset:offset + CHUNK]}


def fetch(client, state, want):
    """Resume JSON wire bytes; apply_bundle still verifies the complete signed zip."""
    part = Path(state) / 'code-download.part'
    mark = Path(state) / 'code-download.json'
    if read_json(mark, {}).get('code') != want:
        part.unlink(missing_ok=True)
        atomic_write(mark, json.dumps({'code': want}))
    offset = part.stat().st_size if part.exists() else 0
    while True:
        reply = client.request('/v1/code/chunk?offset=' + str(offset), timeout=60)
        total, data = reply.get('total'), reply.get('data')
        if (reply.get('code') != want or reply.get('offset') != offset or
                not isinstance(total, int) or not 0 < total <= MAX_BUNDLE or
                not isinstance(data, str) or not 0 < len(data) <= CHUNK or offset + len(data) > total):
            part.unlink(missing_ok=True)
            raise ValueError('Invalid or changed release chunk')
        with part.open('ab') as out:
            out.write(data.encode('ascii'))
        offset += len(data)
        if offset == total:
            bundle = json.loads(part.read_text(encoding='ascii'))
            part.unlink(missing_ok=True)
            mark.unlink(missing_ok=True)
            return bundle
