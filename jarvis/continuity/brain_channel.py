"""Brain bundles over owner-only cluster transfer path operations (no shell/SSH).

The explicit per-peer brain_transfer_dir has no default. It must already exist
inside that peer's storage roots and outside every Git checkout. Ordinary transfer
admission still applies; this module does not bypass a drained runtime's fence.
"""
import base64
import hashlib
import io
import os
import re
import stat
import time
import uuid
from pathlib import Path

ACTIONS = frozenset({'brain_receive', 'brain_export', 'brain_freeze', 'brain_stage', 'brain_discard'})
BUNDLE_NAME = re.compile(r'brain-[a-f0-9]{32}\.bundle')
# Safe diagnostic codes: no path, name or content ever goes into one.
CODE = re.compile(r'[a-z][a-z0-9_]{0,47}')


class BrainChannelError(ValueError):
    """A refused brain operation. ``code`` is safe to report; the text is not."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def safe_code(value, default='unreported'):
    return value if isinstance(value, str) and CODE.fullmatch(value) else default
# Staged bundles left by a crashed or abandoned transfer are removed after this.
STALE_BUNDLE_SECONDS = 24 * 3600


def validate(payload):
    action = payload.get('action')
    required = ({'action', 'path'} if action == 'brain_stage' else
                {'action', 'path', 'sha256'} if action == 'brain_receive' else
                {'action', 'path'} if action == 'brain_discard' else
                {'action', 'path', 'request'} if action == 'brain_export' else
                {'action', 'request'})
    if action not in ACTIONS or set(payload) != required:
        raise ValueError('Invalid brain transfer operation.')
    if 'path' in payload and (not isinstance(payload['path'], str) or
            not 0 < len(payload['path']) <= 4096 or '\x00' in payload['path']):
        raise ValueError('Invalid brain transfer path.')
    if 'sha256' in payload and not (isinstance(payload['sha256'], str) and
            re.fullmatch('[a-f0-9]{64}', payload['sha256'])):
        raise ValueError('Invalid brain transfer digest.')
    if 'request' in payload and not (isinstance(payload['request'], str) and
                                     len(payload['request'].encode('utf-8')) <= 4096):
        raise ValueError('Invalid brain transfer request.')


def _outside_git(path):
    if any((parent / '.git').exists() for parent in (path, *path.parents)):
        raise BrainChannelError('brain_dir_in_git', 'Brain staging must be outside Git checkouts.')


def _drop(path):
    """Remove one staged bundle. Only our own file names; never raises."""
    if not BUNDLE_NAME.fullmatch(path.name):
        return
    try:
        path.unlink()
    except OSError:
        pass


def _sweep(folder, now=None):
    """Remove staged bundles older than STALE_BUNDLE_SECONDS from the staging folder."""
    cutoff = (time.time() if now is None else now) - STALE_BUNDLE_SECONDS
    try:
        children = list(folder.iterdir())
    except OSError:
        return
    for child in children:
        if not BUNDLE_NAME.fullmatch(child.name):
            continue
        try:
            info = child.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                child.unlink()
        except OSError:
            continue


def run_here(payload, state, config):
    from jarvis import brainsync, transfer
    validate(payload)
    base = Path(state).resolve().parent
    if payload['action'] == 'brain_freeze':
        return brainsync.freeze(config, base, payload['request'].encode('utf-8'))
    where = transfer._within(config, payload['path'])
    path = Path(where['path'])
    _outside_git(path)
    if payload['action'] == 'brain_discard':
        if where['dir'] or not BUNDLE_NAME.fullmatch(path.name):
            raise BrainChannelError('bad_bundle_path', 'Invalid brain bundle.')
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return {'ok': True}
    if payload['action'] == 'brain_receive':
        if where['dir']:
            raise BrainChannelError('bad_bundle_path', 'Invalid brain bundle.')
        # An upload that cannot be imported must not stay in the staging folder.
        try:
            if path.stat().st_size > brainsync.MAX_BUNDLE_BYTES:
                raise BrainChannelError('bundle_too_large', 'Invalid brain bundle.')
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != payload['sha256']:
                raise BrainChannelError('bundle_digest_mismatch', 'Brain bundle digest mismatch.')
            from .return_brain import receive
            result = receive(config, state, blob)
        except BaseException:
            _drop(path)
            raise
        if result.get('ok') is True:
            path.unlink()
        else:
            _drop(path)
        return result
    if not where['dir']:
        raise BrainChannelError('brain_dir_not_dir', 'Brain staging must be a directory.')
    _sweep(path)
    if payload['action'] == 'brain_stage':
        return {'ok': True}
    result = brainsync.export_result(config, base, payload['request'].encode('utf-8'))
    if result.get('ok') is not True:
        return result
    blob = base64.b64decode(result['payload'], validate=False)
    name = 'brain-' + uuid.uuid4().hex + '.bundle'
    # Private staging file; never commit the memory snapshot to the vault repo.
    brainsync._write_private(path / name, blob, 0o600)
    return {'ok': True, 'name': name, 'size': len(blob),
            'sha256': hashlib.sha256(blob).hexdigest()}


class ClusterBundleTransport:
    def __init__(self, config, base, *, call=None):
        self.config, self.state = config, Path(base) / '.jarvis'
        self._caller = call
        # Last operation, codes only: which stage it reached and how it ended.
        self.stage = None
        self.diagnostic = None

    def _folder(self, node):
        value = self.config.get('devices', {}).get(node, {}).get('brain_transfer_dir')
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ValueError('Peer brain_transfer_dir is required.')
        return value

    def _call(self, node, payload):
        from jarvis.transfer import call
        if self._caller is not None:
            return self._caller(node, payload)
        return call(self.config, self.state, node, payload, timeout=180)

    def _note(self, operation, result=None, exc=None):
        out = {'operation': operation, 'stage': safe_code(self.stage, None)}
        if exc is not None:
            out.update(ok=False, raised=safe_code(type(exc).__name__.lower()),
                       code=safe_code(getattr(exc, 'code', None), None),
                       remote_code=safe_code(getattr(exc, 'remote_code', None), None))
        elif isinstance(result, dict):
            out.update(ok=result.get('ok') is True, status=safe_code(result.get('status'), None),
                       error=safe_code(result.get('error'), None))
        else:
            out.update(ok=False, error='bad_result')
        self.diagnostic = {key: value for key, value in out.items() if value is not None}

    def _attempt(self, operation, callback):
        self.stage = None
        try:
            result = callback()
        except (ConnectionError, OSError, TimeoutError) as exc:
            self._note(operation, exc=exc)
            return {'ok': False, 'status': 'source_offline', 'error': 'source_offline'}
        except (ValueError, RuntimeError, KeyError, TypeError) as exc:
            self._note(operation, exc=exc)
            return {'ok': False, 'status': 'transport_failed', 'error': 'transport_failed'}
        except BaseException as exc:
            self._note(operation, exc=exc)
            raise
        self._note(operation, result)
        return result

    def push(self, node, bundle):
        return self._attempt('push', lambda: self._push(node, bundle))

    def pull(self, node, request):
        return self._attempt('pull', lambda: self._pull(node, request))

    def freeze(self, node, request):
        def run():
            self.stage = 'freeze'
            return self._call(node, {'action': 'brain_freeze', 'request': request.decode('utf-8')})
        return self._attempt('freeze', run)

    def _push(self, node, bundle):
        from jarvis.transfer import push
        from jarvis import brainsync
        self.stage = 'prepare'
        if len(bundle) > brainsync.MAX_BUNDLE_BYTES:
            raise ValueError('Brain bundle too large.')
        folder = self._folder(node)
        self.stage = 'stage'
        checked = self._call(node, {'action': 'brain_stage', 'path': folder})
        if checked.get('ok') is not True:
            raise ValueError('Peer refused brain staging.')
        self.stage = 'upload'
        name = 'brain-' + uuid.uuid4().hex + '.bundle'
        # A drop after staging may already have uploaded the bundle. That is
        # not source_offline: a retry could deliver the snapshot twice.
        uploaded = name
        try:
            sent = push(self.config, self.state, node, io.BytesIO(bundle), len(bundle),
                        name=name, dest='path', folder=folder,
                        **({'call_fn': lambda cfg, state, peer, body: self._call(peer, body)}
                           if self._caller is not None else {}))
            remote = sent['sent']
            if isinstance(remote, str) and BUNDLE_NAME.fullmatch(remote):
                uploaded = remote
            # Join using the peer's path convention, independent of this host's OS.
            import ntpath, posixpath
            join = ntpath.join if '\\' in folder else posixpath.join
            self.stage = 'receive'
            return self._call(node, {'action': 'brain_receive',
                'path': join(folder, remote), 'sha256': hashlib.sha256(bundle).hexdigest()})
        except (ConnectionError, OSError, TimeoutError):
            self._discard(node, folder, name)
            if uploaded != name:
                self._discard(node, folder, uploaded)
            return {'ok': False, 'status': 'uncertain', 'error': 'uncertain'}

    def _pull(self, node, request):
        from jarvis import brainsync
        self.stage = 'prepare'
        folder = self._folder(node)
        self.stage = 'export'
        result = self._call(node, {'action': 'brain_export', 'path': folder,
                                   'request': request.decode('utf-8')})
        if result.get('ok') is not True:
            return result
        size = result.get('size')
        if type(size) is not int or not 0 <= size <= brainsync.MAX_BUNDLE_BYTES:
            raise ValueError('Invalid brain bundle size.')
        name = result.get('name')
        if not isinstance(name, str) or not BUNDLE_NAME.fullmatch(name):
            raise ValueError('Invalid brain bundle name.')
        self.stage = 'download'
        try:
            blob = bytearray()
            while len(blob) < size:
                chunk = self._call(node, {'action': 'get', 'path': folder,
                    'rel': [name], 'offset': len(blob)})
                data = base64.b64decode(chunk['data'], validate=True)
                if not data or len(blob) + len(data) > size or chunk['size'] != size:
                    raise ValueError('Brain bundle changed during transfer.')
                blob.extend(data)
            if hashlib.sha256(blob).hexdigest() != result['sha256']:
                raise ValueError('Brain bundle digest mismatch.')
            return {'ok': True, 'bundle': bytes(blob)}
        finally:
            # The export is a private staging copy. Verified or not, it is done.
            self._discard(node, folder, name)

    def _discard(self, node, folder, name):
        """Best effort. A peer that cannot discard still sweeps stale bundles itself."""
        import ntpath, posixpath
        join = ntpath.join if '\\' in folder else posixpath.join
        try:
            self._call(node, {'action': 'brain_discard', 'path': join(folder, name)})
        except Exception:
            pass



class TransitionBundleTransport(ClusterBundleTransport):
    """All chunks and import/freeze actions require the same live transition.

    remote(device) returns Participant RemoteRuntime for that installation. Its
    _request reads the current tuple on every chunk; receiver validates it again.
    The ordinary transfer channel and ExecutorGuard are never disabled.
    """
    def __init__(self, config, base, remote):
        from jarvis.topology import identity
        from .participant import transition_scope
        from .runtime import RuntimeRefusal
        status = remote(identity(config)[0]).port.status()
        if ((status.get('transition') or {}).get('state') != 'GRANTED_NOT_READY' or
                status.get('lease_valid') is not True or status.get('main_ready') is not False):
            raise RuntimeRefusal('bad_state')
        scope = transition_scope(status)
        super().__init__(config, base, call=lambda node, body: remote(node)._request(
            'brain', _scope=scope, operation=body))
