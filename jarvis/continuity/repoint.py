"""Installation-signed successor endpoint/key instructions and verified publication.

Only state files change. A signature alone never publishes an endpoint: current
authority, local fencing, and a nonce-bound response from the successor are also
required. No route or SSH permission is inferred from a Wi-Fi address.
"""
import copy
import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from pathlib import Path

from .gateway import RUNTIME_NS, _message, sign_message, verify_message, pin_relay
from .runtime import RuntimeRefusal, _ident_ok

SCHEMA = 'jarvis-continuity-repoint/1'
FILE = 'continuity-endpoint.json'
FIELDS = {'schema', 'authority_id', 'fleet_id', 'incarnation', 'epoch', 'transition_id',
          'main', 'installation_id', 'target', 'endpoint', 'code_key', 'token', 'link'}
# One signed instruction is re-read every few seconds. A handful of distinct
# successes covers that hot path; the least recently used entry drops first.
_VERIFY_CACHE_MAX = 8
_VERIFY_CACHE = OrderedDict()
_VERIFY_CACHE_LOCK = threading.Lock()


def clear_verify_cache():
    """Drop remembered signature successes. Tests call this between cases."""
    with _VERIFY_CACHE_LOCK:
        _VERIFY_CACHE.clear()


def _verify_cache_key(instruction, authority_id, target, pins):
    """sha256 of the canonical instruction plus authority, target, and pins.

    None when those inputs cannot be fingerprinted. That call is not cached,
    so an unusual pins object still goes through ssh-keygen every time.
    """
    try:
        instruction_raw = json.dumps(
            instruction, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        pins_raw = json.dumps(
            pins, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        hash((authority_id, target))
    except (TypeError, ValueError):
        return None
    return (
        hashlib.sha256(instruction_raw.encode('utf-8')).hexdigest(),
        authority_id,
        target,
        hashlib.sha256(pins_raw.encode('utf-8')).hexdigest(),
    )


def _regular(path):
    from jarvis.brainsync import _reparse
    if path.is_symlink() or _reparse(path):
        raise RuntimeRefusal('bad_state')


def _save(path, value):
    from jarvis.fsio import atomic_write
    _regular(path)
    atomic_write(path, json.dumps(value, sort_keys=True), mode=0o600)


def _read(path):
    _regular(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise RuntimeRefusal('bad_state') from None
    if not isinstance(value, dict):
        raise RuntimeRefusal('bad_state')
    return value


def _shape(body):
    from jarvis.deploy import CODE_KEY
    from jarvis.transfer import _check_target
    if not isinstance(body, dict) or set(body) != FIELDS or body.get('schema') != SCHEMA:
        raise RuntimeRefusal('bad_request')
    if any(not _ident_ok(body[k]) for k in ('authority_id', 'fleet_id', 'transition_id', 'main', 'installation_id', 'target')):
        raise RuntimeRefusal('bad_request')
    if any(type(body[k]) is not int or body[k] < 0 for k in ('incarnation', 'epoch')):
        raise RuntimeRefusal('bad_request')
    if not isinstance(body['code_key'], str) or not CODE_KEY.fullmatch(body['code_key']):
        raise RuntimeRefusal('bad_request')
    if not isinstance(body['token'], str) or not 32 <= len(body['token']) <= 128:
        raise RuntimeRefusal('bad_request')
    if pin_relay(body['endpoint']) != body['endpoint']:
        raise RuntimeRefusal('bad_request')
    link = body['link']
    if link is not None:
        from jarvis.cluster_setup import HOST_KEY, ADDRESS
        if (not isinstance(link, dict) or set(link) != {'targets', 'host_key', 'hostname', 'identity_file'} or
                not isinstance(link['targets'], list) or not 1 <= len(link['targets']) <= 8 or
                not isinstance(link['host_key'], str) or not HOST_KEY.fullmatch(link['host_key']) or
                not isinstance(link['hostname'], str) or not ADDRESS.fullmatch(link['hostname']) or
                not isinstance(link['identity_file'], str) or not link['identity_file'] or
                any(c in link['identity_file'] for c in '\x00\r\n')):
            raise RuntimeRefusal('bad_request')
        for target in link['targets']:
            _check_target(target)


def _verify_uncached(instruction, *, authority_id, target, pins):
    if not isinstance(instruction, dict) or set(instruction) != {'body', 'signature'}:
        raise RuntimeRefusal('bad_request')
    body = instruction['body']; _shape(body)
    record = pins.get(body['main'], {})
    if (body['authority_id'] != authority_id or body['target'] != target or
            record.get('installation_id') != body['installation_id'] or
            not verify_message(RUNTIME_NS, _message(RUNTIME_NS, body), instruction['signature'], record.get('public_key'))):
        raise RuntimeRefusal('signature')
    return body


def verify(instruction, *, authority_id, target, pins):
    """Return the signed body, or raise the same refusal an uncached check raises.

    Successes are remembered so a steady re-read does not spawn ssh-keygen.
    effective_config and the installer's prior check are that hot path. The
    installer's new-instruction check uses this same cache: the key is the
    canonical instruction plus authority_id, target, and a fingerprint of
    pins, failures are not stored, and every caller gets its own deep copy.
    A changed file is a different instruction from _read, so it misses.
    """
    # Fingerprint and verify the same snapshot, including across ssh-keygen's
    # blocking call. Callers can retain and mutate their input dictionaries.
    instruction, pins = copy.deepcopy(instruction), copy.deepcopy(pins)
    key = _verify_cache_key(instruction, authority_id, target, pins)
    if key is not None:
        with _VERIFY_CACHE_LOCK:
            cached = _VERIFY_CACHE.get(key)
            if cached is not None:
                _VERIFY_CACHE.move_to_end(key)
                return copy.deepcopy(cached)
    body = _verify_uncached(instruction, authority_id=authority_id, target=target, pins=pins)
    stored = copy.deepcopy(body)
    if key is not None:
        with _VERIFY_CACHE_LOCK:
            _VERIFY_CACHE[key] = stored
            _VERIFY_CACHE.move_to_end(key)
            while len(_VERIFY_CACHE) > _VERIFY_CACHE_MAX:
                _VERIFY_CACHE.popitem(last=False)
    return copy.deepcopy(stored)


def verify_durable(instruction, *, authority_id, target, pins):
    """Read either durable route format, retaining separate authority validators."""
    from .recovery_repoint import SCHEMA as READY_SCHEMA, verify as verify_ready
    if isinstance(instruction, dict) and (instruction.get('body') or {}).get('schema') == READY_SCHEMA:
        return verify_ready(instruction, authority_id=authority_id, target=target, pins=pins)
    return verify(instruction, authority_id=authority_id, target=target, pins=pins)


def _grant(status, body):
    transition = status.get('transition') or {}
    if (status.get('authority_id') != body['authority_id'] or status.get('fleet_id') != body['fleet_id'] or
            status.get('incarnation') != body['incarnation'] or status.get('epoch') != body['epoch'] or
            status.get('lease_valid') is not True or status.get('main_ready') is not False or
            status.get('main') != body['main'] or transition.get('candidate') != body['main'] or
            transition.get('transition_id') != body['transition_id'] or transition.get('state') != 'GRANTED_NOT_READY'):
        raise RuntimeRefusal('stale')


class Issuer:
    def __init__(self, participant, state, broker):
        self.participant, self.state, self.broker = participant, Path(state), broker

    def issue(self, target, endpoint, *, link=None):
        from jarvis.deploy import code_key
        from jarvis.locking import exclusive_lock
        from jarvis.cluster_store import NODE_DEFAULT
        client = self.participant.client
        status = client.status(); transition = status.get('transition') or {}
        snap = self.participant.runtime.lease_snapshot()
        if (not _ident_ok(target) or not snap or snap.get('holder') != client.device_id or
                snap.get('incarnation') != status.get('incarnation') or snap.get('epoch') != status.get('epoch')):
            raise RuntimeRefusal('not_holder')
        body = {'schema': SCHEMA, 'authority_id': client.authority_id, 'fleet_id': status['fleet_id'],
                'incarnation': status['incarnation'], 'epoch': status['epoch'],
                'transition_id': transition.get('transition_id'), 'main': client.device_id,
                'installation_id': client.installation_id, 'target': target,
                'endpoint': pin_relay(endpoint), 'code_key': code_key(self.state),
                'token': 'placeholder-token-for-shape-validation', 'link': copy.deepcopy(link)}
        _shape(body); _grant(status, body)
        path = self.state / ('continuity-offer-'+body['transition_id']+'-'+target+'.json')
        with exclusive_lock(self.state/'continuity-offers.lock'):
            if path.exists():
                existing = _read(path)
                prior = existing.get('body', {})
                if {k: v for k, v in prior.items() if k != 'token'} != {k: v for k, v in body.items() if k != 'token'}:
                    raise RuntimeRefusal('conflict')
                # Never rotate a credential when an offer response is retried.
                return existing
            body['token'] = self.broker.enroll(target, NODE_DEFAULT)
            instruction = {'body': body, 'signature': sign_message(RUNTIME_NS, client._key, _message(RUNTIME_NS, body))}
            _save(path, instruction)
            return instruction


def endpoint_receipt(hub, target, nonce):
    """Only the actual serving hub can attest its endpoint and code signing key."""
    from .participant import lookup
    from jarvis.deploy import code_key
    participant = lookup(hub.state)
    endpoint = getattr(hub, 'continuity_endpoint', None)
    if (participant is None or not getattr(hub, 'started', False) or not _ident_ok(nonce) or
            not _ident_ok(target) or hub.main != participant.device_id or not endpoint):
        raise RuntimeRefusal('managed_unbound')
    status = participant.client.status(); transition = status.get('transition') or {}
    snap = participant.runtime.lease_snapshot()
    body = {'schema': 'jarvis-continuity-endpoint/1', 'authority_id': status['authority_id'],
            'fleet_id': status['fleet_id'], 'incarnation': status['incarnation'], 'epoch': status['epoch'],
            'transition_id': transition.get('transition_id'), 'main': hub.main, 'target': target,
            'endpoint': pin_relay(endpoint), 'code_key': code_key(hub.state), 'nonce': nonce}
    if (not snap or snap.get('holder') != hub.main or snap.get('epoch') != body['epoch'] or
            snap.get('incarnation') != body['incarnation'] or status.get('lease_valid') is not True or
            status.get('main_ready') is not False or transition.get('state') != 'GRANTED_NOT_READY'):
        raise RuntimeRefusal('stale')
    return {'body': body, 'signature': sign_message(RUNTIME_NS, participant.client._key, _message(RUNTIME_NS, body))}


def probe_endpoint(body, nonce):
    """Existing hub API, same target bearer credential, no redirects or proxy."""
    import urllib.request
    from jarvis.cluster import _NoRedirect
    request = urllib.request.Request(body['endpoint']+'/v1/continuity/endpoint',
        data=json.dumps({'nonce': nonce}).encode(), headers={
            'Authorization': 'Bearer '+body['token'], 'X-JARVIS-Main': body['main'],
            'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=5) as response:
        raw = response.read(8193)
    if len(raw) > 8192:
        raise RuntimeRefusal('bad_request')
    return json.loads(raw)


class Installer:
    def __init__(self, participant, state, pins, *, probe=probe_endpoint):
        self.participant, self.state, self.pins, self.probe = participant, Path(state), pins, probe

    def _install(self, instruction, *, publish):
        from jarvis.locking import exclusive_lock
        from .integration import production_binding
        # Publish exactly what was verified even if the caller changes its offer
        # while the endpoint probe is in flight.
        instruction = copy.deepcopy(instruction)
        client = self.participant.client
        body = verify(instruction, authority_id=client.authority_id, target=client.device_id, pins=self.pins)
        binding = production_binding(self.state)
        if (not binding or binding.get('runtime') is not self.participant.runtime or
                binding.get('executor') is not self.participant.guard or not callable(binding.get('issuer'))):
            raise RuntimeRefusal('managed_unbound')
        with exclusive_lock(self.state/'continuity-repoint.lock'):
            status = client.status(); _grant(status, body)
            fence = self.participant.guard.local_view()
            if (fence.get('quarantined') or fence.get('fenced_incarnation') != body['incarnation'] or
                    fence.get('fenced_epoch', -1) != body['epoch']-1 or fence.get('legacy_closed') is not True):
                raise RuntimeRefusal('target_unfenced')
            path = self.state/FILE
            if path.exists():
                prior = verify_durable(_read(path), authority_id=client.authority_id, target=client.device_id, pins=self.pins)
                if (prior['incarnation'], prior['epoch']) > (body['incarnation'], body['epoch']):
                    raise RuntimeRefusal('stale')
                if (prior['incarnation'], prior['epoch']) == (body['incarnation'], body['epoch']) and prior != body:
                    raise RuntimeRefusal('conflict')
            nonce = 'endpoint-'+secrets.token_hex(16)
            result = self.probe(body, nonce)
            expected = {k: body[k] for k in ('authority_id', 'fleet_id', 'incarnation', 'epoch', 'transition_id',
                                           'main', 'target', 'endpoint', 'code_key')}
            expected.update(schema='jarvis-continuity-endpoint/1', nonce=nonce)
            if (not isinstance(result, dict) or set(result) != {'body', 'signature'} or result['body'] != expected or
                    not verify_message(RUNTIME_NS, _message(RUNTIME_NS, expected), result['signature'],
                                       self.pins[body['main']]['public_key'])):
                raise RuntimeRefusal('signature')
            _grant(client.status(), body)  # a slow probe cannot publish a lost grant
            if not publish:
                return {'ok': True, 'authenticated': True, 'key_updated': True,
                        'endpoint_updated': False}
            # Publish intent first. A crash before pairing is fail closed on readback.
            _save(path, instruction)
            note = {'node_id': client.device_id, 'main_device': body['main'], 'token': body['token'],
                    'code_key': body['code_key']}
            if body['link'] is not None:
                note['link'] = copy.deepcopy(body['link'])
            _save(self.state/'cluster-node.json', note)
            if _read(path) != instruction or _read(self.state/'cluster-node.json') != note:
                raise RuntimeRefusal('bad_state')
            return {'ok': True, 'authenticated': True, 'key_updated': True, 'endpoint_updated': True}

    def verify_endpoint(self, instruction):
        """Verify a fresh successor receipt without publishing a new route.

        RoleLifecycle uses this before releasing its old tunnel. The signed
        endpoint is published only after the replacement transport is active.
        """
        return self._install(instruction, publish=False)

    def install(self, instruction):
        """Verify and publish the signed endpoint (for direct, already-routed use)."""
        return self._install(instruction, publish=True)


def effective_config(config, state):
    """Verify the durable instruction offline; invalid state never falls back."""
    path = Path(state)/FILE
    if not path.exists() and not path.is_symlink():
        return config
    from .cli import build_client
    from jarvis.topology import identity
    raw = config.get('continuity', {}).get('operator_config')
    if not isinstance(raw, str) or not raw:
        raise RuntimeRefusal('managed_unbound')
    operator = Path(raw).expanduser()
    if not operator.is_absolute():
        operator = Path(state).resolve().parent/operator
    client, data = build_client(operator)
    local = identity(config)[0]
    if client.device_id != local:
        raise RuntimeRefusal('candidacy_mismatch')
    body = verify_durable(_read(path), authority_id=client.authority_id, target=local, pins=data.get('enrollment', {}))
    note = _read(Path(state)/'cluster-node.json')
    if any(note.get(k) != v for k, v in {'node_id': local, 'main_device': body['main'],
            'code_key': body['code_key'], 'token': body['token']}.items()):
        raise RuntimeRefusal('bad_state')
    # Omitted and JSON null are the same direct-HTTPS route. The signed link owns
    # every SSH field; a note-only rewrite must not reach the tunnel reader.
    signed_link = body['link']
    saved_link = note['link'] if 'link' in note else None
    if saved_link != signed_link:
        raise RuntimeRefusal('bad_state')
    from jarvis.linkfinder import handoff_config
    return handoff_config(config, body)
