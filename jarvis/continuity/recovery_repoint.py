"""Authenticated peer reconnection to a ready recovered MAIN.

Separate from not-ready transition handoff: this never transfers a brain, opens
an authority transition, or changes preferred MAIN. The only discovery origins
are locally pinned direct HTTPS successor routes. Existing installation keys
bootstrap a least-privilege node credential on the existing Hub listener.
"""
import copy
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from .gateway import DEVICE_NS, RUNTIME_NS, _message, payload_digest, sign_message, verify_message
from .runtime import RuntimeRefusal, _ident_ok
from . import repoint

SCHEMA = 'jarvis-continuity-ready-repoint/1'
REQUEST = 'jarvis-continuity-ready-request/1'
RESPONSE = 'jarvis-continuity-ready-response/1'
PROOF = 'jarvis-continuity-ready-endpoint/1'
OFFER_PATH = '/v1/continuity/rejoin/offer'
PROOF_PATH = '/v1/continuity/rejoin/endpoint'
FIELDS = repoint.FIELDS - {'transition_id'} | {'code'}
TUPLE = ('authority_id', 'fleet_id', 'incarnation', 'epoch', 'main')
MAX_AGE = 30
MAX_BYTES = 16384
PHASE_FILE = 'continuity-rejoin-phase.json'


def _signed(body, client):
    return {'body': copy.deepcopy(body), 'signature': sign_message(
        RUNTIME_NS, client._key, _message(RUNTIME_NS, body))}


def _membership(client, nonce, at):
    fields = {'device_id': client.device_id, 'installation_id': client.installation_id,
              'nonce': nonce, 'issued_at': at}
    signature = sign_message(DEVICE_NS, client._key, _message(DEVICE_NS, {
        'v': 1, **fields, 'purpose': 'status', 'payload_digest': payload_digest({})}))
    return {'payload': {}, 'device': {**fields, 'signature': signature}}


def _current_member(client, request):
    envelope = request.get('membership')
    if (not isinstance(envelope, dict) or set(envelope) != {'payload', 'device'}
            or envelope['payload'] != {} or not isinstance(envelope['device'], dict)
            or set(envelope['device']) != {'device_id', 'installation_id', 'nonce', 'issued_at', 'signature'}
            or any(envelope['device'].get(k) != request.get(v) for k,v in (
                ('device_id', 'device_id'), ('installation_id', 'installation_id'),
                ('nonce', 'nonce'), ('issued_at', 'at')))):
        raise RuntimeRefusal('signature')
    try:
        # This fixed read is verified by the gateway against its current enrolled
        # directory, including removals. No caller origin or purpose is forwarded.
        answer = client._send('/v1/authority/status', envelope)
    except Exception:
        raise RuntimeRefusal('unauthorized') from None
    _ready(answer, request)


def _verify(envelope, public_key):
    if (not isinstance(envelope, dict) or set(envelope) != {'body', 'signature'}
            or not isinstance(envelope['body'], dict)
            or not verify_message(RUNTIME_NS, _message(RUNTIME_NS, envelope['body']),
                                  envelope['signature'], public_key)):
        raise RuntimeRefusal('signature')
    return envelope['body']


def _code(value):
    return isinstance(value, str) and len(value) == 16 and all(c in '0123456789abcdef' for c in value)


def _ready(status, body):
    if (any(status.get(k) != body.get(k) for k in TUPLE)
            or status.get('mode') != 'V2_ACTIVE' or status.get('lease_valid') is not True
            or status.get('main_ready') is not True or status.get('transition')
            or any(type(body.get(k)) is not int for k in ('incarnation', 'epoch'))):
        raise RuntimeRefusal('stale')


def _route(controller, device, *, allow_loopback=False):
    from .production import _route as configured_route
    route = configured_route(controller.operator, device)
    parsed = urlsplit(route['endpoint'])
    loopback = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
    if route['link'] is not None or (parsed.scheme != 'https' and not (allow_loopback and loopback)):
        raise RuntimeRefusal('successor_route_unavailable')
    return route


def verify(instruction, *, authority_id, target, pins):
    """Offline durable verification; never treats a ready offer as a transition grant."""
    instruction = copy.deepcopy(instruction)
    if not isinstance(instruction, dict):
        raise RuntimeRefusal('bad_request')
    body = instruction.get('body')
    if (not isinstance(body, dict) or set(body) != FIELDS or body.get('schema') != SCHEMA
            or not _code(body.get('code')) or body.get('link') is not None):
        raise RuntimeRefusal('bad_request')
    # Reuse the established field validators, without reusing grant authority.
    shape = {key: value for key, value in body.items() if key != 'code'}
    shape.update(schema=repoint.SCHEMA, transition_id='ready-recovery')
    repoint._shape(shape)
    pin = pins.get(body['main'], {})
    if (body['authority_id'] != authority_id or body['target'] != target
            or pin.get('installation_id') != body['installation_id']):
        raise RuntimeRefusal('signature')
    return copy.deepcopy(_verify(instruction, pin.get('public_key')))


def _serving(controller, body):
    hub, participant = controller.hub, controller.participant
    if (hub is None or getattr(hub, 'started', False) is not True
            or hub.main != controller.local or hub.local != controller.local
            or participant.runtime.device_id != controller.local
            or hub.console is None):
        raise RuntimeRefusal('managed_unbound')
    _ready(participant.client.status(), body)
    if getattr(participant.runtime, '_renewal_paused', False):
        raise RuntimeRefusal('renewal_paused')
    snapshot = participant.runtime.lease_snapshot()
    if (not snapshot or snapshot.get('holder') != controller.local
            or any(snapshot.get(k) != body.get(k) for k in ('incarnation', 'epoch'))):
        raise RuntimeRefusal('not_holder')
    # Status says ready before admission is checked: never consume/drop a
    # GRANTED_NOT_READY candidacy merely to discover its endpoint. Local
    # suspend, drain, paused renewal and invalid clock still deny this proof.
    admission = participant.runtime.require_authority('control')
    if (admission.get('holder') != controller.local
            or any(admission.get(k) != body.get(k) for k in ('incarnation', 'epoch'))):
        raise RuntimeRefusal('not_holder')
    view = participant.runtime.state()
    if (view.get('admission_open') is not True or view.get('stale') is not False
            or view.get('authority_id') != body.get('authority_id')
            or any(view.get(k) != body.get(k) for k in ('incarnation', 'epoch', 'main'))):
        raise RuntimeRefusal('not_ready')
    guard = participant.guard.local_view()
    if (guard.get('quarantined') or guard.get('journal') is not True
            or participant.runtime.readiness('control').get('meets') is not True):
        raise RuntimeRefusal('not_ready')
    from jarvis.release_runtime import require_loaded_code
    try:
        require_loaded_code(hub)
    except ValueError:
        raise RuntimeRefusal('code_mismatch') from None
    if hub.loaded_code != body.get('code'):
        raise RuntimeRefusal('code_mismatch')
    return hub


def _listener(hub, route):
    server = getattr(hub, 'server', None)
    address = getattr(server, 'server_address', ())
    if (len(address) < 2 or address[1] != route['listen_port']
            or address[0] != route['listen_host']):
        raise RuntimeRefusal('successor_hub_unavailable')
    advertised = getattr(hub, 'continuity_endpoint', None)
    if advertised is not None and advertised != route['endpoint']:
        raise RuntimeRefusal('successor_hub_unavailable')


class Issuer:
    def __init__(self, controller, *, wall=None, allow_loopback=False):
        self.controller = controller
        self.wall = wall or getattr(controller.participant.client, '_wall', None) or time.time
        self.allow_loopback = allow_loopback

    def offer(self, envelope):
        from jarvis.deploy import code_key
        from jarvis.cluster_store import NODE_DEFAULT
        from jarvis.locking import exclusive_lock
        c = self.controller
        body = envelope.get('body') if isinstance(envelope, dict) else None
        fields = set(TUPLE) | {'schema', 'device_id', 'installation_id', 'nonce', 'at', 'code', 'membership'}
        if (not isinstance(body, dict) or set(body) != fields or body.get('schema') != REQUEST
                or not _ident_ok(body.get('nonce')) or not _ident_ok(body.get('device_id'))
                or type(body.get('at')) is not int or abs(self.wall() - body['at']) > MAX_AGE
                or not _code(body.get('code'))):
            raise RuntimeRefusal('bad_request')
        target = body['device_id']
        pin = c.pins.get(target)
        if (target == c.local or not pin or pin.get('installation_id') != body['installation_id']):
            raise RuntimeRefusal('signature')
        _verify(envelope, pin.get('public_key'))
        _current_member(c.participant.client, body)
        route = _route(c, c.local, allow_loopback=self.allow_loopback)
        hub = _serving(c, body)
        _listener(hub, route)
        offer = {key: body[key] for key in TUPLE}
        offer.update(schema=SCHEMA, installation_id=c.participant.client.installation_id,
                     target=target, endpoint=route['endpoint'], code_key=code_key(c.state),
                     token=secrets.token_urlsafe(32), link=None, code=hub.loaded_code)
        path = c.state / ('continuity-ready-offer-' + str(body['incarnation']) + '-'
                          + str(body['epoch']) + '-' + target + '.json')
        with exclusive_lock(c.state / 'continuity-offers.lock'):
            _serving(c, body)
            nonces_path = c.state / 'continuity-ready-nonces.json'
            nonces = repoint._read(nonces_path) if nonces_path.exists() else {}
            nonces = {key:at for key,at in nonces.items()
                      if type(at) is int and abs(self.wall() - at) <= MAX_AGE}
            nonce_key = target + ':' + body['nonce']
            if nonce_key in nonces:
                raise RuntimeRefusal('replay')
            if len(nonces) >= 512:
                raise RuntimeRefusal('busy')
            nonces[nonce_key] = body['at']
            repoint._save(nonces_path, nonces)
            if path.exists():
                instruction = repoint._read(path)
                prior = verify(instruction, authority_id=body['authority_id'], target=target, pins=c.pins)
                if {k:v for k,v in prior.items() if k != 'token'} != {k:v for k,v in offer.items() if k != 'token'}:
                    raise RuntimeRefusal('conflict')
            else:
                instruction = _signed(offer, c.participant.client)
                # Persist before enrollment, so a crash can reinstall this exact credential.
                repoint._save(path, instruction)
            offer = instruction['body']
            hub.broker.enroll(target, NODE_DEFAULT, token=offer['token'])
            enrolled, caps = hub.broker.authenticate(offer['token'])
            if enrolled != target or set(caps) != set(NODE_DEFAULT):
                raise RuntimeRefusal('bad_state')
            _serving(c, body)
            return _signed({'schema': RESPONSE, 'nonce': body['nonce'], 'at': body['at'],
                            'instruction': instruction}, c.participant.client)

    def proof(self, target, nonce):
        c = self.controller
        status = c.participant.client.status()
        body = {key: status.get(key) for key in TUPLE}
        body['code'] = getattr(c.hub, 'loaded_code', None)
        hub = _serving(c, body)
        if not _ident_ok(target) or target == c.local or not _ident_ok(nonce):
            raise RuntimeRefusal('bad_request')
        route = _route(c, c.local, allow_loopback=self.allow_loopback)
        _listener(hub, route)
        from jarvis.deploy import code_key
        return _signed({**body, 'schema': PROOF, 'target': target, 'nonce': nonce,
                        'endpoint': route['endpoint'], 'code_key': code_key(c.state)}, c.participant.client)


def post(origin, path, envelope):
    """Pinned HTTPS only in production; no redirects, cookies, proxy, or owner token."""
    import urllib.request
    from jarvis.cluster import _NoRedirect
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(origin + path, data=json.dumps(envelope).encode(),
                                     headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=5) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise RuntimeRefusal('bad_request')
    return json.loads(raw)


def probe(body, nonce):
    import urllib.request
    from jarvis.cluster import _NoRedirect
    request = urllib.request.Request(body['endpoint'] + PROOF_PATH,
        data=json.dumps({'nonce': nonce}).encode(), headers={'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + body['token'], 'X-JARVIS-Main': body['main']})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=5) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise RuntimeRefusal('bad_request')
    return json.loads(raw)


class Installer:
    def __init__(self, controller, *, endpoint_probe=probe, allow_loopback=False):
        self.controller, self.endpoint_probe = controller, endpoint_probe
        self.allow_loopback = allow_loopback

    def install(self, instruction):
        from jarvis.locking import exclusive_lock
        from .integration import production_binding
        c, p = self.controller, self.controller.participant
        instruction = copy.deepcopy(instruction)
        body = verify(instruction, authority_id=p.client.authority_id, target=c.local, pins=c.pins)
        binding = production_binding(c.state)
        if (not binding or binding.get('runtime') is not p.runtime
                or binding.get('executor') is not p.guard or not callable(binding.get('issuer'))):
            raise RuntimeRefusal('managed_unbound')
        route = _route(c, body['main'], allow_loopback=self.allow_loopback)
        if body['main'] == c.local or body['endpoint'] != route['endpoint']:
            raise RuntimeRefusal('candidacy_mismatch')
        from jarvis.deploy import code_fingerprint
        if body['code'] != c.loaded_code or body['code'] != code_fingerprint():
            raise RuntimeRefusal('code_mismatch')
        with exclusive_lock(c.state / 'continuity-repoint.lock'):
            status = p.client.status(); _ready(status, body)
            path = c.state / repoint.FILE
            if path.exists():
                prior = repoint.verify_durable(repoint._read(path), authority_id=p.client.authority_id,
                                               target=c.local, pins=c.pins)
                old, new = (prior['incarnation'], prior['epoch']), (body['incarnation'], body['epoch'])
                if old > new:
                    raise RuntimeRefusal('stale')
                if old == new and prior != body:
                    raise RuntimeRefusal('conflict')
            nonce = 'ready-endpoint-' + secrets.token_hex(16)
            receipt = self.endpoint_probe(body, nonce)
            expected = {key:body[key] for key in (*TUPLE, 'code', 'target', 'endpoint', 'code_key')}
            expected.update(schema=PROOF, nonce=nonce)
            if _verify(receipt, c.pins[body['main']]['public_key']) != expected:
                raise RuntimeRefusal('signature')
            _ready(p.client.status(), body)
            # Fence every superseded epoch locally; no authority grant is inferred.
            p.guard.fence(body['incarnation'], body['epoch'] - 1, status['mode_version'])
            fence = p.guard.local_view()
            if (fence.get('quarantined') or not fence.get('legacy_closed')
                    or fence.get('fenced_incarnation') != body['incarnation']
                    or fence.get('fenced_epoch') != body['epoch'] - 1):
                raise RuntimeRefusal('target_unfenced')
            _ready(p.client.status(), body)
            repoint._save(path, instruction)
            note = {'node_id': c.local, 'main_device': body['main'], 'token': body['token'],
                    'code_key': body['code_key']}
            repoint._save(c.state / 'cluster-node.json', note)
            if repoint._read(path) != instruction or repoint._read(c.state / 'cluster-node.json') != note:
                raise RuntimeRefusal('bad_state')
        return {'ok': True, 'authenticated': True, 'endpoint_updated': True}


class Rejoin:
    """Retry from the existing renewer; never closes an old Hub before publication."""
    def __init__(self, controller, *, send=post, endpoint_probe=probe, restart=None,
                 wall=None, allow_loopback=False):
        self.controller, self.send = controller, send
        self.installer = Installer(controller, endpoint_probe=endpoint_probe, allow_loopback=allow_loopback)
        self.restart = restart or self._restart
        self.wall = wall or getattr(controller.participant.client, '_wall', None) or time.time
        self.allow_loopback = allow_loopback
        self._next = 0

    def _restart(self):
        c = self.controller
        from jarvis.cluster import _PROCESS_RECORD, _local_record, _systemd_service
        record = _local_record(c.state / _PROCESS_RECORD)
        if (isinstance(record, dict) and record.get('pid') == os.getpid()
                and record.get('state') == str(c.state.resolve()) and record.get('kind') == 'node-worker'):
            # _rejoined notices the durable new credential, then closes the guard,
            # services, Hub and role lock before its existing respawn adapter.
            return {'awaiting_node_restart': True}
        if not _systemd_service('jarvis-cluster'):
            raise RuntimeRefusal('role_restart_unavailable')
        from jarvis.cluster_setup import role_autostart
        return role_autostart(c.base / 'jarvis.config.json', c.state, True, restart=True)

    def tick(self):
        c, p = self.controller, self.controller.participant
        now = self.wall()
        if now < self._next:
            return None
        self._next = now + 15
        status = p.client.status()
        main = status.get('main')
        if main == c.local or not main or main not in c.pins:
            return None
        tuple_body = {key:status.get(key) for key in TUPLE}
        _ready(status, tuple_body)
        route = _route(c, main, allow_loopback=self.allow_loopback)
        from jarvis.deploy import code_fingerprint
        code = c.loaded_code
        request = {**tuple_body, 'schema': REQUEST, 'device_id': c.local,
                   'installation_id': p.client.installation_id, 'nonce': 'rejoin-' + secrets.token_hex(16),
                   'at': int(now), 'code': code}
        request['membership'] = _membership(p.client, request['nonce'], request['at'])
        response = self.send(route['endpoint'], OFFER_PATH, _signed(request, p.client))
        response_body = _verify(response, c.pins[main]['public_key'])
        if (set(response_body) != {'schema', 'nonce', 'at', 'instruction'}
                or response_body.get('schema') != RESPONSE or response_body.get('nonce') != request['nonce']
                or response_body.get('at') != request['at'] or abs(self.wall() - request['at']) > MAX_AGE):
            raise RuntimeRefusal('signature')
        instruction = response_body['instruction']
        body = verify(instruction, authority_id=p.client.authority_id, target=c.local, pins=c.pins)
        _ready(p.client.status(), body)
        if any(body.get(k) != request.get(k) for k in TUPLE) or body['code'] != code:
            raise RuntimeRefusal('stale')
        digest = hashlib.sha256(json.dumps(instruction, sort_keys=True).encode()).hexdigest()
        phase_path = c.state / PHASE_FILE
        if phase_path.exists():
            phase = repoint._read(phase_path)
            if phase.get('digest') == digest and phase.get('phase') == 'scheduled':
                # Read back the paired publication before treating a replay as complete.
                effective = repoint.effective_config(c.config, c.state)
                if (effective.get('cluster') or {}).get('main_device') != main:
                    raise RuntimeRefusal('bad_state')
                return {'ok': True, 'already': True}
        self.installer.install(instruction)
        repoint._save(phase_path, {'phase': 'published', 'digest': digest})
        result = self.restart()
        repoint._save(phase_path, {'phase': 'scheduled', 'digest': digest})
        return {'ok': True, 'main': main, 'restart': result}
