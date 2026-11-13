"""Last-resort LAN discovery: bounded TCP probes followed by pinned SSH verification."""
import concurrent.futures
import ipaddress
import subprocess
import time
from pathlib import Path

from .fsio import read_json
from .platforms import NO_WINDOW

INTERVAL = 300
WORKERS = 32
SCAN_BUDGET = 45  # one wall clock for every probe and pin check, not per host
PROBE_TIMEOUT = 0.15
VERIFY_TIMEOUT = 3


def scan_networks(interfaces):
    nets = []
    for interface in interfaces[:8]:
        if interface.version != 4 or interface.ip.is_loopback or interface.ip.is_link_local:
            continue
        net = ipaddress.ip_network(f'{interface.ip}/{max(22, interface.network.prefixlen)}', strict=False)
        if net not in nets:
            nets.append(net)
    return nets


def verify(target, machine, state, timeout=VERIFY_TIMEOUT):
    """Strict known-hosts SSH proves possession of the pinned private host key.

    A keyscan alone can repeat someone else's public key; never persist on that basis.
    The restricted forward-mode account ignores `echo` and returns jarvis-node-link.
    Normal SSH accounts (reverse mode) can execute echo on Windows and Unix alike.
    timeout is the time left in the scan budget, already capped at VERIFY_TIMEOUT.
    """
    from .cluster_tunnel import pinned_host
    from .transfer import _ssh_args, ssh_host
    import shutil
    if not timeout or timeout <= 0:
        return False
    binary = shutil.which('ssh')
    if not binary or not machine.get('host_key'):
        return False
    try:
        result = subprocess.run([*_ssh_args(binary, machine.get('identity_file'), timeout=min(2, timeout)),
            *pinned_host(machine, state), '-T', *ssh_host(target), 'echo jarvis-node-link'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return result.returncode == 0 and 'jarvis-node-link' in result.stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def run_bounded(items, attempt, *, workers, deadline, clock):
    """Run attempt(item, remaining) until deadline.

    At most `workers` attempts are in flight. remaining shrinks with the clock.
    Queued work is cancelled at the deadline. The pool is not waited on after
    that: a pin check still running is dropped and is not an acceptance.
    """
    pending, results, index, exhausted = {}, {}, 0, False
    items = iter(items)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        while clock() < deadline:
            while not exhausted and len(pending) < workers and clock() < deadline:
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                try:
                    item = next(items)
                except StopIteration:
                    exhausted = True
                    break
                pending[pool.submit(attempt, item, remaining)] = index
                index += 1
            if not pending:
                break
            remaining = deadline - clock()
            if remaining <= 0:
                break
            done, _pending = concurrent.futures.wait(
                set(pending), timeout=remaining, return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                break  # budget spent; do not wait for the in-flight attempts
            for fut in done:
                slot = pending.pop(fut, None)
                if slot is None or fut.cancelled():
                    continue
                try:
                    value = fut.result(timeout=0)
                except (concurrent.futures.CancelledError, Exception):
                    continue
                if value is not None:
                    results[slot] = value
        for fut in pending:
            fut.cancel()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [results[i] for i in sorted(results)]


def discovery_path(machine, state):
    import hashlib
    identity = machine.get('host_key', '') + ':' + machine.get('hostname', '')
    return Path(state) / ('cluster-discovered-' + hashlib.sha256(identity.encode()).hexdigest()[:16] + '.json')


def discovered(machine, state):
    note = read_json(discovery_path(machine, state), {})
    from .cluster_recovery import clean
    if not machine.get('host_key') or note.get('host_key') != machine['host_key']:
        return []
    user = machine['target'].split('@', 1)[0]
    return [f'{user}@{a}' for a in clean(note.get('values'))]


def _stopped(state):
    from .cluster_tunnel import MANUAL_STOP
    return (state / 'cluster-tunnel.stop').exists() or (state / MANUAL_STOP).exists()


def discover(machine, state, *, now=None, interfaces=None, clock=None, budget=None,
             workers=None, reach=None, check=None):
    """Called only after known targets fail. Rate limit persists across worker reboots.

    clock is monotonic. budget (default 45s) covers every TCP probe and pinned SSH
    check together. reach and check default to the production socket and SSH pin.
    """
    from .cluster import save_json
    from .sharedinput import local_networks
    from . import _under_tests
    if interfaces is None and _under_tests():
        return []  # tests must provide fake interface inventory before any LAN scan
    clock = time.monotonic if clock is None else clock
    budget = SCAN_BUDGET if budget is None else budget
    workers = WORKERS if workers is None else workers
    if reach is None:
        from .cluster_tunnel import reachable as reach
    check = verify if check is None else check
    state = Path(state)
    now = time.time() if now is None else now
    stamp = read_json(state / 'cluster-scan.json', {})
    last = stamp.get('at')
    from .cluster_recovery import scan_due
    if not scan_due(last, now):
        return []
    if not machine.get('host_key'):
        return []
    save_json(state / 'cluster-scan.json', {'at': now})
    nets = scan_networks(local_networks(with_addresses=True) if interfaces is None else interfaces)
    user = machine['target'].split('@', 1)[0]
    deadline = clock() + budget

    def attempt(ip, remaining):
        if _stopped(state) or clock() >= deadline:
            return None
        left = min(remaining, deadline - clock())
        probe = min(PROBE_TIMEOUT, left)
        if probe <= 0:
            return None
        target = f'{user}@{ip}'
        if not reach(target, timeout=probe):
            return None
        left = min(VERIFY_TIMEOUT, deadline - clock())
        if left <= 0 or not check(target, machine, state, timeout=left):
            return None
        # A check that finished late, or a stop during the check, is not acceptance.
        if _stopped(state) or clock() > deadline:
            return None
        return str(ip)

    found = run_bounded((ip for net in nets for ip in net.hosts()), attempt,
                        workers=workers, deadline=deadline, clock=clock)
    found = list(dict.fromkeys(found))[:16]
    if found:
        save_json(discovery_path(machine, state), {'host_key': machine['host_key'], 'values': found, 'at': now})
    return [f'{user}@{ip}' for ip in found]
