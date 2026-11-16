"""Transition-scoped runtime RPC on the existing signed cluster job channel.

No callback accepts a key path, gateway URL, readiness flag, or shell command from
its caller. The installation client and runtime are constructed on this machine.
Ordinary jobs continue through ExecutorGuard; these maintenance operations have a
separate, narrow authority check against the live owner-created transition.
"""
from pathlib import Path
import threading

from .runtime import GatewayPort, MainRuntime, RuntimeRefusal

KIND = 'continuity'
_ACTIONS = frozenset({'readiness', 'drain', 'release', 'accept', 'snapshot', 'fence', 'probe', 'brain', 'canary', 'activate_return', 'repoint', 'pause_renewal', 'owner_proxy'})
_PHASES = {
    'readiness': {'REQUESTED', 'VALIDATING'},
    'drain': {'DRAINING'}, 'release': {'DRAINING'},
    'accept': {'GRANTED_NOT_READY'}, 'snapshot': {'GRANTED_NOT_READY'},
    'fence': {'DRAINING', 'FENCING'}, 'brain': {'GRANTED_NOT_READY'}, 'canary': {'GRANTED_NOT_READY'}, 'activate_return': {'GRANTED_NOT_READY'},
    'repoint': {'GRANTED_NOT_READY'},
    'pause_renewal': {'GRANTED_NOT_READY'},
}
_REGISTRY = {}
_LOCK = threading.RLock()


def validate(payload):
    if not isinstance(payload, dict):
        raise RuntimeRefusal('bad_request')
    action = payload.get('action')
    required = {'action', 'transition_id', 'incarnation', 'epoch'}
    if action == 'accept':
        required.add('lease')
    if action == 'brain':
        required.add('operation')
    if action == 'canary':
        required.add('nonce')
    if action == 'repoint':
        required.add('target')
    if action == 'owner_proxy':
        required.add('operation')
    if action not in _ACTIONS or set(payload) != required:
        raise RuntimeRefusal('bad_request')
    from .runtime import _ident_ok
    proxy_request = (action == 'owner_proxy' and isinstance(payload.get('operation'), dict)
                     and payload['operation'].get('kind') == 'request')
    if not ((action == 'probe' and payload['transition_id'] is None)
            or (proxy_request and payload['transition_id'] is None)) and not _ident_ok(payload['transition_id']):
        raise RuntimeRefusal('bad_request')
    if action == 'canary' and not _ident_ok(payload['nonce']):
        raise RuntimeRefusal('bad_request')
    if action == 'repoint' and not _ident_ok(payload['target']):
        raise RuntimeRefusal('bad_request')
    if any(type(payload[k]) is not int or payload[k] < 0 for k in ('incarnation', 'epoch')):
        raise RuntimeRefusal('bad_request')


def _closed_epoch(status, transition):
    """Epoch a fence acknowledgement must name.

    The transition opens under ``expected_epoch``. ``fence()`` advances the
    authority epoch only after every executor has acknowledged that one.
    A later status read shows the new epoch. Acknowledging it would mark
    the new MAIN's targets as already fenced.
    """
    state = transition.get('state')
    expected, epoch = transition.get('expected_epoch'), status.get('epoch')
    if (type(expected) is not int or type(epoch) is not int or expected < 0 or epoch < 0
            or transition.get('expected_incarnation') != status.get('incarnation')):
        raise RuntimeRefusal('bad_state')
    if state == 'DRAINING':
        if epoch != expected:
            raise RuntimeRefusal('stale')
        return expected
    if state == 'FENCING':
        if epoch != expected + 1 or transition.get('fenced_epoch') != epoch:
            raise RuntimeRefusal('stale')
        return expected
    raise RuntimeRefusal('bad_state')


def _bound_ack(result, *, device_id, transition_id, incarnation, epoch, mode_version):
    """Reject a fence result that is not this device's ack of the closed epoch."""
    from .runtime import _ident_ok
    receipt = result.get('receipt') if isinstance(result, dict) else None
    signature = result.get('signature') if isinstance(result, dict) else None
    if (not isinstance(receipt, dict) or not isinstance(signature, str)
            or 'BEGIN SSH SIGNATURE' not in signature or result.get('authority_recorded') is not True
            or result.get('fenced_epoch') != epoch or result.get('device_id') != device_id):
        raise RuntimeRefusal('signature')
    if (receipt.get('device_id') != device_id or receipt.get('transition_id') != transition_id
            or receipt.get('incarnation') != incarnation or receipt.get('epoch') != epoch
            or receipt.get('mode_version') != mode_version
            or receipt.get('installation_id') != result.get('installation_id')
            or receipt.get('authority_id') != result.get('authority_id')
            or receipt.get('fleet_id') != result.get('fleet_id')
            or not _ident_ok(receipt.get('installation_id'))
            or not _ident_ok(receipt.get('authority_id')) or not _ident_ok(receipt.get('fleet_id'))):
        raise RuntimeRefusal('stale')
    return receipt


class Participant:
    def __init__(self, client, runtime, *, guard=None, config=None, state=None, canary=None):
        if (runtime.device_id != client.device_id or
                runtime.installation_id != client.installation_id):
            raise RuntimeRefusal('candidacy_mismatch')
        self.client, self.runtime, self.guard = client, runtime, guard
        self.config, self.state, self.canary = config, state, canary
        self.device_id = runtime.device_id
        self._lock = threading.RLock()
        self._fence_acks = {}

    def _authorized(self, payload, requester):
        validate(payload)
        status = self.client.status()  # authenticated with THIS installation key
        transition = status.get('transition') or {}
        if (status.get('incarnation') != payload['incarnation'] or
                status.get('epoch') != payload['epoch']):
            raise RuntimeRefusal('stale')
        if payload['action'] == 'owner_proxy':
            self._last_proxy_result = self._authorize_owner_proxy(
                payload['operation'], requester, status, transition)
            return status, transition
        if payload['action'] == 'probe':
            coordinator = status.get('main') if status.get('lease_valid') else self.device_id
            if requester != coordinator:
                raise RuntimeRefusal('forbidden')
            return status, transition
        if transition.get('transition_id') != payload['transition_id']:
            raise RuntimeRefusal('unknown_transition')
        action = payload['action']
        if transition.get('state') == 'GRANTED_NOT_READY' and (
                status.get('lease_valid') is not True or status.get('main_ready') is not False or
                status.get('main') != transition.get('candidate')):
            raise RuntimeRefusal('expired')
        if transition.get('state') not in _PHASES[action]:
            raise RuntimeRefusal('bad_state')
        source, candidate = transition.get('source'), transition.get('candidate')
        # Recover is coordinated by the candidate, never by an arbitrary peer.
        coordinator = source if source is not None else candidate
        if requester != coordinator:
            raise RuntimeRefusal('forbidden')
        role = source if action in ('drain', 'release') else candidate
        if action == 'brain':
            self._brain_authorized(payload['operation'], transition)
            role = self.device_id
        if action == 'repoint':
            if (transition.get('action') != 'set_preferred_main'
                    or transition.get('source') != requester
                    or transition.get('candidate') != self.device_id
                    or payload.get('target') != requester):
                raise RuntimeRefusal('candidacy_mismatch')
            role = self.device_id
        # Every enrolled executor may acknowledge the epoch this transition
        # opened under. The gateway still requires this installation's fence
        # capability on the signed acknowledge. The post-fence epoch is the
        # new MAIN's and is not an acknowledgement target.
        if action != 'fence' and self.device_id != role:
            raise RuntimeRefusal('candidacy_mismatch')
        if action not in ('readiness', 'snapshot'):
            # A guard object alone does not fence legacy entry points. Refuse
            # until those entry points use this runtime/guard and a real issuer.
            from .integration import production_binding
            binding = production_binding(self.state) if self.state is not None else None
            if (not binding or binding.get('runtime') is not self.runtime or
                    binding.get('executor') is not self.guard or not callable(binding.get('issuer'))):
                raise RuntimeRefusal('managed_unbound')
        return status, transition

    def _authorize_owner_proxy(self, operation, requester, status, transition):
        """Authorize one enrolled ready MAIN to request/step a return to this PC."""
        controller = getattr(self, 'production_controller', None)
        if controller is None:
            raise RuntimeRefusal('managed_unbound')
        allowed = controller.operator.get('owner_proxy_clients', ())
        client = self.client
        owner_path = getattr(client, '_owner_key', None)
        if (requester not in allowed or not getattr(client, 'can_sign_owner', False)
                or owner_path is None or not Path(owner_path).is_file()):
            raise RuntimeRefusal('owner_proof_unavailable')
        if not isinstance(operation, dict):
            raise RuntimeRefusal('bad_request')
        from .runtime import GatewayPort, _caller_key
        owner = GatewayPort(client)
        if operation == {'kind': 'request'}:
            if (transition or status.get('mode') != 'V2_ACTIVE' or status.get('main') != requester
                    or status.get('lease_valid') is not True or status.get('main_ready') is not True):
                raise RuntimeRefusal('stale')
            request = {
                'caller': 'safe-handoff',
                'client_key': _caller_key('set_preferred_main', requester, self.device_id, status),
                'action': 'set_preferred_main', 'candidate': self.device_id,
                'expected_incarnation': status['incarnation'], 'expected_epoch': status['epoch'],
                'expected_policy': status['policy_version'], 'profile': 'control',
            }
            return owner.transition_request(request)
        keys = {'kind', 'transition_id', 'step', 'incarnation', 'epoch'}
        if operation.get('step') == 'complete':
            keys.add('canary_ok')
        if set(operation) != keys or operation.get('kind') != 'step':
            raise RuntimeRefusal('bad_request')
        if (operation.get('transition_id') != transition.get('transition_id')
                or transition.get('action') != 'set_preferred_main'
                or transition.get('source') != requester
                or transition.get('candidate') != self.device_id
                or operation.get('incarnation') != status.get('incarnation')
                or operation.get('epoch') != status.get('epoch')
                or operation.get('step') not in {'drain', 'fence', 'grant', 'complete', 'cancel'}):
            raise RuntimeRefusal('stale')
        if operation['step'] == 'complete':
            if type(operation.get('canary_ok')) is not bool:
                raise RuntimeRefusal('bad_request')
        elif 'canary_ok' in operation:
            raise RuntimeRefusal('bad_request')
        return owner.transition_step(operation['transition_id'], operation['step'],
                                     operation.get('canary_ok'))

    def _brain_authorized(self, operation, transition):
        from jarvis import transfer
        import json
        import ntpath
        import posixpath
        import re
        if self.config is None or self.state is None or not isinstance(operation, dict):
            raise RuntimeRefusal('managed_unbound')
        transfer.validate(operation)
        root = self.config.get('devices', {}).get(self.device_id, {}).get('brain_transfer_dir')
        if not isinstance(root, str) or not root:
            raise RuntimeRefusal('managed_unbound')
        paths = ntpath if '\\' in root else posixpath
        action = operation.get('action')
        source, candidate = transition.get('source'), transition.get('candidate')
        destination = action in ('brain_stage', 'put', 'brain_receive')
        if self.device_id != (candidate if destination else source):
            raise RuntimeRefusal('candidacy_mismatch')
        if action == 'put':
            if (set(operation) != {'action', 'upload', 'offset', 'last', 'data', 'dest', 'name', 'size', 'dir'} or
                    operation.get('dest') != 'path' or operation.get('dir') != root or
                    type(operation.get('last')) is not bool):
                raise RuntimeRefusal('bad_request')
            name = operation.get('name')
        elif action in ('brain_stage', 'brain_export'):
            if operation.get('path') != root:
                raise RuntimeRefusal('forbidden')
            name = None
        elif action in ('brain_receive', 'brain_discard'):
            if paths.dirname(operation['path']) != root:
                raise RuntimeRefusal('forbidden')
            name = paths.basename(operation['path'])
        elif action == 'get':
            if (set(operation) != {'action', 'path', 'rel', 'offset'} or
                    operation.get('path') != root or len(operation.get('rel', [])) != 1):
                raise RuntimeRefusal('forbidden')
            name = operation['rel'][0]
        elif action == 'brain_freeze':
            name = None
        else:
            raise RuntimeRefusal('forbidden')
        if name is not None and not re.fullmatch(r'brain-[a-f0-9]{32}\.bundle', name):
            raise RuntimeRefusal('forbidden')
        if action in ('brain_export', 'brain_freeze'):
            try:
                spec = json.loads(operation['request'])
            except (ValueError, TypeError):
                raise RuntimeRefusal('bad_request') from None
            if not isinstance(spec, dict) or spec.get('old_main') != source or spec.get('new_main') != candidate:
                raise RuntimeRefusal('candidacy_mismatch')
        # Even put/get chunks must stay in an approved non-Git storage root.
        from .brain_channel import _outside_git
        where = transfer._within(self.config, root)
        if not where['dir']:
            raise RuntimeRefusal('forbidden')
        _outside_git(Path(where['path']))

    def _canary_receipt(self, payload, status):
        import hashlib
        from .gateway import RUNTIME_NS, _message, sign_message
        snap = self.runtime.lease_snapshot()
        if (not callable(self.canary) or not snap or
                snap.get('incarnation') != status['incarnation'] or snap.get('epoch') != status['epoch'] or
                snap.get('holder') != self.device_id or
                self.runtime.readiness('control').get('meets') is not True or self.canary() is not True):
            raise RuntimeRefusal('not_ready')
        # A canary implementation must check the prepared endpoint, signing key
        # and application health. None never means success.
        receipt = {'device_id': self.device_id, 'installation_id': self.client.installation_id,
                   'authority_id': status['authority_id'], 'fleet_id': status['fleet_id'],
                   'transition_id': payload['transition_id'], 'incarnation': status['incarnation'],
                   'epoch': status['epoch'], 'nonce': payload['nonce'],
                   'lease_digest': hashlib.sha256(snap['lease_id'].encode()).hexdigest()}
        return {'receipt': receipt, 'signature': sign_message(
            RUNTIME_NS, self.client._key, _message(RUNTIME_NS, receipt))}

    def execute(self, payload, *, requester):
        with self._lock:
            status, transition = self._authorized(payload, requester)
            tid, action = payload['transition_id'], payload['action']
            if action == 'activate_return':
                from .return_brain import activate
                return activate(self)
            if action == 'canary':
                return self._canary_receipt(payload, status)
            if action == 'probe':
                facts = self.runtime.readiness('control')
                return {key: facts.get(key) for key in ('control_ready', 'ai_ready', 'checkpoint')}
            if action == 'brain':
                from jarvis.transfer import run_here
                config = self.config
                if getattr(self, 'production_controller', None) is not None:
                    from jarvis.brainsync import jarvis_handoff_config
                    config = jarvis_handoff_config(config)
                try:
                    return run_here(payload['operation'], self.state, config)
                except RuntimeRefusal:
                    raise
                except Exception as exc:
                    # The node reply carries only RuntimeRefusal.code, never this text.
                    from .brain_channel import safe_code
                    raise RuntimeRefusal(safe_code(getattr(exc, 'code', None),
                                                   'brain_' + safe_code(type(exc).__name__.lower(), 'error')[:40])) from None
            if action == 'repoint':
                controller = getattr(self, 'production_controller', None)
                if controller is None:
                    raise RuntimeRefusal('managed_unbound')
                return controller.prepare_repoint(payload['target'])
            if action == 'owner_proxy':
                result, self._last_proxy_result = self._last_proxy_result, None
                if result is None:
                    raise RuntimeRefusal('bad_state')
                return result
            if action == 'pause_renewal':
                self.runtime.pause_renewal()
                return {'paused': True}
            if action == 'readiness':
                facts = self.runtime.readiness(transition['profile'])
                # The client signs locally; no facts supplied by the source are used.
                return self.client.readiness(tid, facts['control_ready'], facts['ai_ready'],
                                             facts.get('checkpoint'))
            if action == 'drain':
                return self.runtime.begin_drain(tid)
            if action == 'release':
                return self.runtime.release_for_handoff(tid)
            if action == 'fence':
                return self._fence(payload, status, transition)
            if action == 'accept':
                lease = payload['lease']
                if (not isinstance(lease, dict) or lease.get('holder') != self.device_id or
                        lease.get('incarnation') != status['incarnation'] or
                        lease.get('epoch') != status['epoch'] or lease.get('ready') is not False or
                        status.get('lease_holder') != self.device_id or
                        status.get('lease_valid') is not True or status.get('main_ready') is not False):
                    raise RuntimeRefusal('stale')
                self.runtime.start()
                self.runtime.accept_lease(lease)
            return self.runtime.lease_snapshot()

    def _fence(self, payload, status, transition):
        """Sign one idempotent acknowledgement of the closed epoch.

        A repeat of the same transition, incarnation and closed epoch returns
        the receipt already issued. It does not ask the authority to record a
        higher epoch. A guard that has already fenced past that epoch refuses
        before another acknowledge is sent.
        """
        if self.guard is None:
            raise RuntimeRefusal('managed_unbound')
        from .authority import AuthorityError
        from .gateway import RUNTIME_NS, GatewayError, _message, sign_message
        from .runtime import _ident_ok
        closed = _closed_epoch(status, transition)
        incarnation = status['incarnation']
        if (not _ident_ok(status.get('authority_id')) or not _ident_ok(status.get('fleet_id'))
                or type(status.get('mode_version')) is not int or status['mode_version'] < 0):
            raise RuntimeRefusal('bad_state')
        key = (payload['transition_id'], incarnation, closed)
        cached = self._fence_acks.get(key)
        if cached is not None:
            return cached
        view = self.guard.local_view()
        prior_epoch, prior_inc = view.get('fenced_epoch'), view.get('fenced_incarnation')
        if (type(prior_epoch) is int and type(prior_inc) is int
                and (prior_inc, prior_epoch) > (incarnation, closed)):
            raise RuntimeRefusal('stale')
        try:
            recorded = self.guard.fence(incarnation, closed, status['mode_version'])
        except AuthorityError as exc:
            raise RuntimeRefusal(exc.code, exc.wait) from None
        if (not isinstance(recorded, dict) or recorded.get('fenced_epoch') != closed
                or recorded.get('fenced_incarnation') != incarnation
                or recorded.get('authority_recorded') is not True
                or recorded.get('device_id') != self.device_id
                or recorded.get('installation_id') != self.client.installation_id
                or recorded.get('authority_id') != status['authority_id']
                or recorded.get('fleet_id') != status['fleet_id']):
            raise RuntimeRefusal('stale')
        receipt = {
            'device_id': self.device_id, 'installation_id': self.client.installation_id,
            'authority_id': status['authority_id'], 'fleet_id': status['fleet_id'],
            'transition_id': payload['transition_id'], 'incarnation': incarnation,
            'epoch': closed, 'mode_version': status['mode_version'],
        }
        try:
            signature = sign_message(RUNTIME_NS, self.client._key, _message(RUNTIME_NS, receipt))
        except GatewayError:
            raise RuntimeRefusal('signature') from None
        signed = {**recorded, 'receipt': receipt, 'signature': signature}
        self._fence_acks = {key: signed}
        return signed


def load_local(config, state, *, probe, client_factory=None, guard=None, boot=None):
    """Load only this PC's configured operator file. No network or marker writes.

    A real readiness probe is required; absence never manufactures readiness.
    Relative operator paths resolve against the checkout containing state.
    """
    from .cli import build_client
    from jarvis.topology import identity
    raw = config.get('continuity', {}).get('operator_config')
    if not isinstance(raw, str) or not raw or not callable(probe):
        raise RuntimeRefusal('managed_unbound')
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(state).resolve().parent / path
    client, _ = (client_factory or build_client)(path)
    if client.device_id != identity(config)[0]:
        raise RuntimeRefusal('candidacy_mismatch')
    runtime = MainRuntime(GatewayPort(client), client.device_id, client.installation_id, probe,
                          boot=boot)
    return Participant(client, runtime, guard=guard, config=config, state=state)


def bind(state, participant):
    if not isinstance(participant, Participant):
        raise RuntimeRefusal('bad_request')
    with _LOCK:
        _REGISTRY[str(Path(state).resolve())] = participant


def unbind(state):
    with _LOCK:
        _REGISTRY.pop(str(Path(state).resolve()), None)


def lookup(state):
    with _LOCK:
        return _REGISTRY.get(str(Path(state).resolve()))


def execute_job(state, payload, requester):
    participant = lookup(state)
    if participant is None:
        raise RuntimeRefusal('managed_unbound')
    return participant.execute(payload, requester=requester)


class RemoteRuntime:
    """SafeHandoff runtime proxy. Calls stay on the existing job transport."""
    def __init__(self, device_id, port, call):
        self.device_id, self.port, self.call = device_id, port, call

    def _request(self, action, *, _scope=None, **extra):
        if _scope is not None:
            if set(_scope) != {'transition_id', 'incarnation', 'epoch'}:
                raise RuntimeRefusal('bad_request')
            payload = {'action': action, **_scope, **extra}
            validate(payload)
            return self.call(self.device_id, payload)
        status = self.port.status()
        transition = status.get('transition') or {}
        tid = None if action == 'probe' else transition.get('transition_id')
        payload = {'action': action, 'transition_id': tid,
                   'incarnation': status['incarnation'], 'epoch': status['epoch'], **extra}
        validate(payload)
        return self.call(self.device_id, payload)

    def readiness(self):
        return self._request('probe')

    def begin_drain(self, tid):
        status = self.port.status()
        if (status.get('transition') or {}).get('transition_id') != tid:
            raise RuntimeRefusal('unknown_transition')
        return self._request('drain', _scope=transition_scope(status))

    def release_for_handoff(self, tid):
        status = self.port.status()
        if (status.get('transition') or {}).get('transition_id') != tid:
            raise RuntimeRefusal('unknown_transition')
        return self._request('release', _scope=transition_scope(status))

    def accept_lease(self, lease):
        return self._request('accept', lease=lease)

    def lease_snapshot(self):
        return self._request('snapshot')

    def pause_renewal(self):
        return self._request('pause_renewal')


class CandidatePort:
    """Route readiness to the candidate, keeping owner steps on the owner port."""
    def __init__(self, owner_port, candidate, participants=()):
        self.owner_port, self.candidate = owner_port, candidate
        self.participants = tuple(participants)

    def __getattr__(self, name):
        return getattr(self.owner_port, name)

    def transition_readiness(self, transition_id, facts):
        # facts are validated by SafeHandoff but never signed on the source PC.
        status = self.owner_port.status()
        transition = status.get('transition') or {}
        if (transition.get('candidate') != self.candidate.device_id or
                transition.get('transition_id') != transition_id):
            raise RuntimeRefusal('candidacy_mismatch')
        return self.candidate._request('readiness', _scope=transition_scope(status))


    def transition_step(self, transition_id, step, canary_ok=None):
        if step == 'fence':
            status = self.owner_port.status()
            transition = status.get('transition') or {}
            if transition.get('transition_id') != transition_id:
                raise RuntimeRefusal('unknown_transition')
            # Only the draining epoch is collected. A replay after fence()
            # advanced the epoch must not acknowledge the new MAIN's epoch.
            if transition.get('state') == 'DRAINING':
                scope = transition_scope(status)
                for peer in self.participants:
                    result = peer._request('fence', _scope=scope)
                    _bound_ack(result, device_id=peer.device_id, transition_id=transition_id,
                               incarnation=status['incarnation'], epoch=status['epoch'],
                               mode_version=status['mode_version'])
        return self.owner_port.transition_step(transition_id, step, canary_ok)


def dispatch(hub, node, payload, timeout=120):
    """Private maintenance queue, never the ordinary dispatch admission fallback.

    Jobs retain code matching, online/capability checks, poll-time signatures,
    deadlines and at-most-once execution. Receiver independently checks authority.
    """
    import time
    from jarvis.cluster import NodeOffline, served_code
    from jarvis.cluster_store import TERMINAL
    validate(payload)
    participant = lookup(hub.state)
    if participant is None:
        raise RuntimeRefusal('managed_unbound')
    if node == hub.local:
        return participant.execute(payload, requester=hub.local)
    status = next((row for row in hub.broker.nodes() if row['id'] == node), None)
    if not status or 'files' not in status['capabilities']:
        raise RuntimeRefusal('ineligible')
    if not hub.alive(node, status) and not hub._await_poll(node, status):
        raise NodeOffline('Continuity participant is offline.')
    if status.get('code') != served_code(hub.state):
        raise RuntimeRefusal('code_mismatch')
    with hub._condition:
        job_id = hub.broker.enqueue(node, hub.local, KIND, payload, timeout)
        hub._condition.notify_all()
    end = time.monotonic() + timeout
    try:
        while not hub.stop_event.is_set():
            with hub._condition:
                job = hub.broker.get(job_id)
                if job['state'] in TERMINAL:
                    if job['state'] == 'done':
                        return job['result']
                    refusal = RuntimeRefusal('uncertain' if job['state'] == 'uncertain' else 'callback_failed')
                    # Diagnostics only: callers still see callback_failed/uncertain.
                    from .brain_channel import safe_code
                    reply = job.get('result') if isinstance(job.get('result'), dict) else {}
                    refusal.remote_code = safe_code(reply.get('code'), None)
                    raise refusal
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise RuntimeRefusal('uncertain')
                hub._condition.wait(remaining)
        raise RuntimeRefusal('uncertain')
    finally:
        hub.broker.compact(job_id)


class SignedCanary:
    """Fresh nonce + exact grant tuple + pinned candidate installation signature."""
    def __init__(self, candidate, public_key, installation_id):
        self.candidate, self.public_key, self.installation_id = candidate, public_key, installation_id

    def __call__(self, device):
        import secrets
        import hashlib
        from .gateway import RUNTIME_NS, _message, verify_message
        if device != self.candidate.device_id:
            return False
        nonce = 'canary-' + secrets.token_hex(16)
        status = self.candidate.port.status()
        snap = self.candidate.lease_snapshot()
        if not snap:
            return False
        expected = {'device_id': device, 'installation_id': self.installation_id,
                    'authority_id': status['authority_id'], 'fleet_id': status['fleet_id'],
                    'transition_id': (status.get('transition') or {}).get('transition_id'),
                    'incarnation': status['incarnation'], 'epoch': status['epoch'], 'nonce': nonce,
                    'lease_digest': hashlib.sha256(snap['lease_id'].encode()).hexdigest()}
        try:
            result = self.candidate._request('canary', _scope=transition_scope(status), nonce=nonce)
        except RuntimeRefusal as exc:
            if exc.code in ('not_ready', 'signature', 'candidacy_mismatch', 'expired', 'stale', 'managed_unbound'):
                return False
            raise
        return (isinstance(result, dict) and set(result) == {'receipt', 'signature'} and
                result['receipt'] == expected and verify_message(
                    RUNTIME_NS, _message(RUNTIME_NS, expected), result['signature'], self.public_key))


def transition_scope(status):
    """Pin a callback to one transition and grant tuple, never a later retry."""
    return {'transition_id': (status.get('transition') or {}).get('transition_id'),
            'incarnation': status['incarnation'], 'epoch': status['epoch']}
