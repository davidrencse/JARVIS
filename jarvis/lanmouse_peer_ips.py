"""Safe replacement for the IP watcher's sed writer; never starts or stops anything.

Usage: python -m jarvis.lanmouse_peer_ips CONFIG PEER IP [IP ...]
"""
import json
import re
import sys
import time
from pathlib import Path

from .fsio import atomic_write
from .locking import exclusive_lock
from .sharedinput import validate_pairing


def prepare_ips(original, peer, ips):
    """Pure validation and rewrite, shared by the watcher and state-machine tests."""
    before = validate_pairing(original)
    clients = before['clients']
    if len(clients) != 1 or clients[0]['hostname'] != peer:
        raise ValueError('Watcher requires exactly its configured peer; refusing ambiguous rewrite.')
    if not ips:
        raise ValueError('No peer IPs; keeping known-good config.')
    line = 'ips = ' + json.dumps(list(dict.fromkeys(ips)))
    updated, count = re.subn(r'^ips\s*=\s*\[[^\n]*\]\s*$', line, original, flags=re.M)
    if count != 1:
        raise ValueError('Expected one single-line IP list; keeping config.')
    after = validate_pairing(updated)
    if before['authorized_fingerprints'] != after['authorized_fingerprints']:
        raise ValueError('Fingerprint changed during IP update.')
    return updated


def write_ips(path, peer, ips):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Config is a link; keeping it untouched.')
    with exclusive_lock(path.with_name(path.name + '.lock')):
        original = path.read_text(encoding='utf-8')
        updated = prepare_ips(original, peer, ips)
        if updated == original:
            return False
        atomic_write(path.with_name(f'{path.name}.bak-{time.time_ns()}'), original, fsync=True)
        atomic_write(path, updated, fsync=True)
        return True


if __name__ == '__main__':
    sys.exit(0 if write_ips(sys.argv[1], sys.argv[2], sys.argv[3:]) else 1)
