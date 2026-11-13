"""Role-aware discovery, pinned announces and device-local endpoint memory.

All authority comes from existing pairing or a verified continuity instruction. Discovery
never enrolls a device, changes a key/role, or grants an execution capability.
"""
import hashlib
import hmac
import json
import secrets
import select
import socket
import time
from pathlib import Path

from .cluster_recovery import (clean, valid_address, learned, learn, offer, signature, magic_names,
    tailscale_status, merge_values, SessionHealth, rank_targets, ensure_tunnel, ensure_dependents, OfflineAlerts, CHECK, scan_due)
from .cluster_discovery import (scan_networks, verify, discover as _discover, discovered,
    run_bounded, SCAN_BUDGET, WORKERS, PROBE_TIMEOUT)
from .fsio import read_json

DEFAULTS = {'link_discovery': True, 'announce_port': 47781, 'fleet_id': 'jarvis'}
DOMAIN = b'jarvis-address-announce-v1\n'
LIMIT = 4096


def role(config, state):
    from .continuity.repoint import effective_config
    from .topology import identity
    config = effective_config(config, state)
    local, main = identity(config)
    return config, local, main


def handoff_config(config, body):
    """Called only after repoint has verified the signed instruction and paired note."""
    import copy
    result = copy.deepcopy(config)
    result['cluster'].update(main_device=body['main'], main_url=body['endpoint'])
    return result


def fleet(config, state):
    # effective_config has already verified the durable continuity instruction.
    record = read_json(Path(state) / 'continuity-endpoint.json', {})
    return (record.get('body') or {}).get('fleet_id') or (config.get('cluster') or {}).get('fleet_id', DEFAULTS['fleet_id'])


def settings(config):
    return {**DEFAULTS, **(config.get('cluster') or {})}


def enabled(config):
    return settings(config)['link_discovery'] is True


def device_for_peer(config, machine):
    from .topology import identity
    local, main = identity(config)
    if (config.get('cluster') or {}).get('tunnel', 'reverse') == 'forward':
        return main
    return next((name for name, entry in (config.get('devices') or {}).items()
                 if name != local and entry == machine), None)


def addresses_for(config, state, device):
    from .topology import identity
    local, _ = identity(config)
    record = read_json(Path(state)/'cluster-addresses.json', {})
    if record.get('node') != local:
        return []
    return clean(((record.get('peers') or {}).get(device) or {}).get('values'))


def discover(machine, state, config=None):
    found = _discover(machine, state)
    if found and config is not None:
        device = device_for_peer(config, machine)
        if device:
            from .cluster_recovery import remember
            remember(config, state, device, [t.split('@', 1)[1] for t in found], source='pinned-ssh')
    return found


def packet_bytes(body):
    return DOMAIN + json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def announce(config, state):
    from .cluster_setup import addresses
    from .deploy import sign, SIGNING_KEY
    if not (Path(state) / SIGNING_KEY).is_file():
        raise ValueError('No existing main signing key.')
    config, local, main = role(config, state)
    if local != main:
        raise ValueError('Only the current main may announce.')
    body = {'main': main, 'fleet': fleet(config, state), 'timestamp': int(time.time()),
            'nonce': secrets.token_hex(24), 'addresses': addresses(config, main)}
    raw = json.dumps({'body': body, 'signature': sign(packet_bytes(body), state)}, separators=(',', ':')).encode()
    if len(raw) > LIMIT:
        raise ValueError('Announce too large.')
    return raw


def identity_mac(token_hash, body):
    return hmac.new(token_hash.encode(), b'jarvis-lan-node-proof-v1\n' + packet_bytes(body), hashlib.sha256).hexdigest()


def validate_announce(note, main, fleet_id, key, now, verify_signature):
    if not isinstance(note, dict):
        return None
    body = note.get('body')
    if (set(note) != {'body', 'signature'} or not isinstance(body, dict) or
            set(body) != {'main', 'fleet', 'timestamp', 'nonce', 'addresses'} or
            body['main'] != main or body['fleet'] != fleet_id or
            type(body['timestamp']) is not int or abs(now - body['timestamp']) > 120 or
            not isinstance(body['nonce'], str) or len(body['nonce']) != 48 or
            not isinstance(body['addresses'], list) or not body['addresses'] or
            len(body['addresses']) > 16 or clean(body['addresses']) != body['addresses']):
        return None
    if not key or not verify_signature(packet_bytes(body), note['signature'], key):
        return None
    return body


def accept_announce(note, nonces, now, main, fleet_id, key, verify_signature):
    """Pure accept rule for a signed announce: the CURRENT main's pinned key, fresh, unseen nonce.
    Returns the body (and records its nonce in `nonces`), or None. Nothing else is decided here."""
    body = validate_announce(note, main, fleet_id, key, now, verify_signature)
    if body is None:
        return None
    replay_key = main + ':' + body['nonce']
    for stale in [k for k, v in nonces.items() if not isinstance(v, (int, float)) or abs(now - v) > 120]:
        del nonces[stale]
    if replay_key in nonces:
        return None
    nonces[replay_key] = now
    return body


class Receiver:
    def __init__(self, config, state, *, clock=time.time):
        self.config, self.state, self.clock = config, Path(state), clock
        self.window = 0
        self.count = 0
        self.nonces = read_json(self.state / 'cluster-announces.json', {})

    def receive(self, raw):
        """Return an identity response or accept an announce; no command dispatch here."""
        now = self.clock()
        if not isinstance(raw, bytes) or len(raw) > LIMIT:
            return None
        if now - self.window >= 1 or now < self.window:
            self.window, self.count = now, 0
        self.count += 1
        if self.count > 4:  # global, before parsing, key lookup or expensive signature checks
            return None
        try:
            note = json.loads(raw)
            config, local, main = role(self.config, self.state)
            if not enabled(config) or local == main or not isinstance(note, dict):
                return None
            paired = read_json(self.state / 'cluster-node.json', {})
            if paired.get('main_device') != main or paired.get('node_id') != local:
                return None
            if note.get('kind') == 'probe':
                if (set(note) != {'kind', 'node', 'nonce', 'fleet'} or note['node'] != local or
                        note['fleet'] != fleet(config, self.state) or not isinstance(note['nonce'], str) or
                        len(note['nonce']) != 48 or not isinstance(paired.get('token'), str)):
                    return None
                digest = hashlib.sha256(paired['token'].encode()).hexdigest()
                return json.dumps({'proof': identity_mac(digest, note)}, separators=(',', ':')).encode()
            from .deploy import verify as verify_signature
            key = paired.get('code_key')
            nonces = dict(self.nonces)
            body = accept_announce(note, nonces, now, main, fleet(config, self.state), key, verify_signature)
            if body is None:
                return None
            # Resolve again after verification: a handoff during verification fails closed.
            if role(self.config, self.state)[2] != main or read_json(self.state/'cluster-node.json', {}).get('code_key') != key:
                return None
            from .cluster_recovery import remember
            from .cluster import save_json
            remember(config, self.state, main, body['addresses'], source='signed-announce')
            self.nonces = nonces
            save_json(self.state / 'cluster-announces.json', self.nonces)
            (self.state / 'cluster-tunnel-reconnect').touch()
            return True
        except (ValueError, TypeError, KeyError, OSError, RuntimeError):
            return None


def listen_ips():
    """IPv4 addresses the listener may bind: this computer's real LAN interfaces plus its own
    Tailscale address. Never the wildcard, never other virtual or container interfaces."""
    from .sharedinput import local_networks
    ips = {str(i.ip) for i in local_networks(with_addresses=True) if i.version == 4}
    own = (tailscale_status() or {}).get('Self') or {}
    for value in own.get('TailscaleIPs') or []:
        if valid_address(value) and value.count('.') == 3:
            ips.add(value)
    return ips


def listen(config, state, stop):
    """Existing agent thread only. Bind individual local interface addresses, never wildcard."""
    receiver = Receiver(config, state)
    sockets = {}
    refreshed = -100
    try:
        while not stop.is_set():
            current, local, main = role(config, state)
            if not enabled(current) or local == main:
                return
            if time.monotonic() - refreshed >= 30:
                refreshed = time.monotonic()
                ips = listen_ips()
                for ip in list(sockets):
                    if ip not in ips:
                        sockets.pop(ip).close()
                for ip in ips - sockets.keys():
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        sock.bind((ip, int(settings(current)['announce_port'])))
                        sock.setblocking(False)
                        sockets[ip] = sock
                    except OSError:
                        sock.close()
            if not sockets:
                stop.wait(1)
                continue
            ready, _, _ = select.select(list(sockets.values()), [], [], 0.5)
            for sock in ready:
                raw, source = sock.recvfrom(LIMIT + 1)
                reply = receiver.receive(raw)
                if isinstance(reply, bytes):
                    sock.sendto(reply, source)
    finally:
        for sock in sockets.values():
            sock.close()


def find_node(config, state, node, token_hash, *, now=None, interfaces=None, clock=None,
              budget=None, workers=None):
    """Probe each local host with a fresh nonce; only the pinned node credential hash can prove it.

    clock is monotonic. The same 45s budget covers every probe. A reply that arrives
    after the deadline is not accepted.
    """
    from . import _under_tests
    if interfaces is None and _under_tests():
        return []
    from .sharedinput import local_networks
    from .cluster import save_json
    clock = time.monotonic if clock is None else clock
    budget = SCAN_BUDGET if budget is None else budget
    workers = WORKERS if workers is None else workers
    config, local, main = role(config, state)
    if not enabled(config) or local != main:
        return []
    now = time.time() if now is None else now
    path = Path(state) / ('cluster-main-scan-' + hashlib.sha256(node.encode()).hexdigest()[:16] + '.json')
    stamp = read_json(path, {})
    if not scan_due(stamp.get('at'), now):
        return []
    save_json(path, {'at': now})
    networks = scan_networks(local_networks(with_addresses=True) if interfaces is None else interfaces)
    port = int(settings(config)['announce_port'])
    deadline = clock() + budget

    def attempt(ip, remaining):
        if clock() >= deadline:
            return None
        left = min(PROBE_TIMEOUT, remaining, deadline - clock())
        if left <= 0:
            return None
        body = {'kind': 'probe', 'node': node, 'nonce': secrets.token_hex(24), 'fleet': fleet(config, state)}
        raw = json.dumps(body, separators=(',', ':')).encode()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(left)
                sock.connect((str(ip), port))
                sock.send(raw)
                reply = json.loads(sock.recv(LIMIT + 1))
            expected = identity_mac(token_hash, body)
            if (isinstance(reply, dict) and isinstance(reply.get('proof'), str)
                    and hmac.compare_digest(reply['proof'], expected) and clock() <= deadline):
                return str(ip)
        except (OSError, ValueError):
            pass
        return None

    found = run_bounded((ip for net in networks for ip in net.hosts()), attempt,
                        workers=workers, deadline=deadline, clock=clock)
    return list(dict.fromkeys(found))[:16]


def main_tick(hub):
    """Current main only; node tokens remain hashed, never exposed or used as bearer credentials."""
    config, local, main = role(hub.config, hub.state)
    if local != main or not enabled(config):
        return
    from .cluster_store import Broker
    now = time.time()
    from .cluster import save_json
    waiting_path = Path(hub.state) / 'cluster-offline-since.json'
    waiting = read_json(waiting_path, {})
    for node in hub.broker.nodes():
        # A newly promoted main may have enrolled a node that cannot make its first
        # poll yet (both LAN addresses changed, VPN down). Do not require a prior poll.
        since = node.get('seen') or waiting.setdefault(node['id'], now)
        save_json(waiting_path, waiting)
        if now - since <= 60:
            continue
        with hub.broker.db() as db:
            row = db.execute('SELECT token_hash,invite_expires FROM nodes WHERE id=?', (node['id'],)).fetchone()
        if not row or row['invite_expires'] is not None:
            continue
        found = find_node(config, hub.state, node['id'], row['token_hash'])
        if not found:
            continue
        from .cluster_recovery import remember
        remember(config, hub.state, node['id'], found, source='authenticated-node')
        # Re-resolve role immediately before signing/sending, including a MAIN switch during scan.
        if role(hub.config, hub.state)[2] != main:
            return
        raw = announce(config, hub.state)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for ip in found:
                sock.sendto(raw, (ip, int(settings(config)['announce_port'])))


def activate_tunnel(config_path, state, link):
    """Explicit lifecycle action; the worker uses the same role/discovery/store path."""
    from . import cluster_tunnel
    from .continuity.runtime import RuntimeRefusal
    cluster_tunnel.autostart(config_path, state, link is not None)
    if link is not None:
        cluster_tunnel.start(config_path, state)
        if not cluster_tunnel.status(state)['running']:
            raise RuntimeRefusal('partition')
