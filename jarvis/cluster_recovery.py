"""Bounded, authenticated endpoint discovery and link keep-alive helpers."""
import hashlib
import hmac
import functools
import ipaddress
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .fsio import read_json
from .platforms import NO_WINDOW

MAX_ADDRESSES = 16
CHECK = 5  # node agent: how often it looks at the tunnel worker
BACKOFF_CAP = 30  # seconds: the longest the tunnel supervisor ever waits between rounds
UNHEALTHY_AFTER = 10  # seconds a live ssh may fail the forwarded-hub check before it is rotated
# Worst case from "link dead" to "supervisor acted": 5 s poll + 10 s grace + 15 s restart of a dead worker.
SUPERVISOR_WITHIN = 30


def valid_address(value):
    if not isinstance(value, str) or len(value) > 253:
        return False
    return _valid_address(value)


@functools.lru_cache(maxsize=4096)
def _valid_address(value):
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and not (ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local)
    except ValueError:
        return bool(not re.fullmatch(r'[0-9.]+', value) and value.lower() != 'localhost' and
                    re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', value) and
                    all(0 < len(p) <= 63 and not p.startswith('-') and not p.endswith('-')
                        for p in value.split('.')))


def clean(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(v for v in values[:64] if valid_address(v)))[:MAX_ADDRESSES]


def tailscale_status():
    binary = shutil.which('tailscale')
    from .platforms import on_windows
    import os
    if not binary and on_windows():
        guess = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Tailscale' / 'tailscale.exe'
        binary = str(guess) if guess.is_file() else None
    if not binary:
        return None
    try:
        out = subprocess.run([binary, 'status', '--json'], capture_output=True, text=True,
                             timeout=3, creationflags=NO_WINDOW)
        return json.loads(out.stdout) if out.returncode == 0 else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def magic_names():
    status = tailscale_status() or {}
    own = status.get('Self') or {}
    return clean([str(own.get('DNSName') or '').rstrip('.'), own.get('HostName')])


def signature(token, main, node, nonce, values):
    payload = json.dumps([main, node, nonce, values], separators=(',', ':'), ensure_ascii=True)
    return hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()


def offer(config, main, node, token, nonce, values=None):
    from .cluster_setup import addresses
    values = addresses(config, main) if values is None else clean(values)
    return {'values': values, 'signature': signature(token, main, node, nonce, values)}


def authenticated_addresses(note, token, main, local, nonce):
    if not isinstance(note, dict) or not isinstance(token, str) or not token:
        return False
    values = note.get('values')
    if not isinstance(values, list) or len(values) > MAX_ADDRESSES or clean(values) != values:
        return False
    sig = note.get('signature')
    return isinstance(sig, str) and hmac.compare_digest(sig, signature(token, main, local, nonce, values))


def learn(client, response, nonce):
    """Only a nonce-bound MAC from the authenticated hub may update the node-local cache."""
    note = response.get('addresses')
    if not isinstance(note, dict):
        return False  # older hubs and non-network test clients publish no discovery envelope
    if not authenticated_addresses(note, getattr(client, 'token', None), client.main, client.local, nonce):
        return False
    try:
        remember(getattr(client, "config", {}), client.state, client.main, note['values'],
                 source='authenticated-hub', local=client.local)
    except (OSError, ValueError):
        return False  # optional discovery metadata must never drop an already claimed job
    return True


def merge_values(fresh, old):
    """Learned-address list after new news: freshest first, then what was known, capped."""
    return clean([*clean(fresh), *clean(old)])


def remember(config, state, device, values, *, source, local=None):
    from .cluster import save_json
    from .topology import identity
    local = local or identity(config)[0]
    path = Path(state) / 'cluster-addresses.json'
    from .locking import exclusive_lock
    with exclusive_lock(path.with_suffix('.lock'), timeout=0.2):
        prior = read_json(path, {})
        peers = prior.get('peers', {}) if prior.get('node') == local else {}
        # Freshest first, then what was known before (a main can flip back to an older address).
        peers[device] = {'values': merge_values(values, (peers.get(device) or {}).get('values')),
                         'source': source, 'updated': time.time()}
        save_json(path, {'node': local, 'peers': peers})


def learned(config, state):
    from .topology import identity
    local, main = identity(config)
    note = read_json(Path(state) / 'cluster-addresses.json', {})
    if note.get('node') != local:
        return []
    entry = (note.get('peers') or {}).get(main, {})
    # Read the initial cache format for upgrades, scoped to its original device.
    return clean(entry.get('values')) or (clean(note.get('values')) if note.get('main') == main else [])


def ensure_tunnel(config, state, config_path):
    """Node only: login autostart installed and the worker running. True when it acted.

    Supervisor, passive doctor, and doctor --fix all come through here. An operator
    stop for this boot and role is left alone: this does not start the worker and
    does not rewrite config.
    """
    from . import cluster_tunnel
    from .linkfinder import role
    config, local, main = role(config, state)
    if cluster_tunnel.manual_stop_holds(config, state):
        return False
    instruction = read_json(Path(state) / 'continuity-endpoint.json', {})
    if (cluster_tunnel.mode(config) != 'forward' or local == main or
            (instruction and (instruction.get('body') or {}).get('link') is None)):
        return False
    acted = False
    if not cluster_tunnel.startup_path().is_file():
        cluster_tunnel.autostart(config_path, state, True)
        acted = True
    if not cluster_tunnel.status(state)['running']:
        cluster_tunnel.start(config_path, state)
        acted = True
    return acted


class OfflineAlerts:
    """One alert per outage; only nodes observed online can trigger one."""
    def __init__(self):
        self.seen = {}
        self.sent = set()

    def check(self, nodes, now, notify):
        for node in nodes:
            key = node['id']
            if node.get('online'):
                self.seen[key] = now
                self.sent.discard(key)
            elif key in self.seen and now - self.seen[key] > 180 and key not in self.sent:
                self.sent.add(key)
                notify(f"JARVIS: {key} offline for over 3 minutes. Likely asleep, network/DHCP changed, "
                       "Tailscale disconnected, or its tunnel/agent stopped. The node supervisor retries automatically.")


class SessionHealth:
    """A live SSH process gets at most ten seconds without a forwarded hub handshake."""
    def __init__(self):
        self.failed_at = None

    def observe(self, now, healthy):
        if healthy:
            self.failed_at = None
        elif self.failed_at is None:
            self.failed_at = now
        return self.failed_at is not None and now - self.failed_at >= UNHEALTHY_AFTER


def rank_targets(targets, probe, mapper=map):
    """Port-open candidates first, each group keeping its given (freshest-first) order.
    mapper lets the tunnel probe every candidate at once instead of one dead LAN at a time."""
    targets = list(targets)
    available = dict(zip(targets, mapper(probe, targets)))
    return sorted(targets, key=lambda target: not available[target])


def parallel_probe(probe, targets, workers=16):
    """mapper for rank_targets: concurrent probes, results in input order."""
    import concurrent.futures
    targets = list(targets)
    if len(targets) < 2:
        return [probe(t) for t in targets]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(targets))) as pool:
        return list(pool.map(probe, targets))


def merge_candidates(discovered, fresh, configured, user, limit=32):
    """Tunnel targets, best first: addresses found by a verified scan, then the freshest
    learned ones, then the ones saved at join time. Pure, so the fuzz drives the real order."""
    targets = [*discovered, *(f"{user}@{a}" for a in fresh), *configured]
    return list(dict.fromkeys(t for t in targets if t))[:limit]


def next_backoff(delay):
    return min(delay * 2, BACKOFF_CAP)


def ensure_dependents(config, state):
    """Recheck after a link returns, respecting all existing manual-stop/off switches."""
    from . import clipsync, sharedinput
    if (config.get('clipboard') or {}).get('autostart', True):
        if not clipsync._pid(str(state)) and not clipsync.stopped_by_hand(str(state)):
            clipsync.start(str(state))
    if (config.get('lan_mouse') or {}).get('off_when_apart', True):
        sharedinput.guard_for(config, state)


def scan_due(last, now):
    return not isinstance(last, (int, float)) or now - last >= 300
