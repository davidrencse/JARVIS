"""Seeded state-machine fuzz of link recovery. Pure model plus the production decision code.

Three devices (a, b, c). The main is a or b and can switch; the others are nodes. Faults: DHCP
changes (from a small pool, so addresses get REUSED by other devices or by an impostor), Tailscale
down on either side, sleep, node and main reboots, hub restarts, agent updates, roaming off the LAN,
a MAIN switch (also during a DHCP change) after which the demoted main keeps announcing, and forged
announces (bad signature, former main's key, replay, stale, wrong fleet).

The production pieces under test: candidates order (`merge_candidates`), probe ordering
(`rank_targets`), health rotation (`SessionHealth`), backoff (`next_backoff`), address learning
(`authenticated_addresses`, `merge_values`), the signed-announce accept rule (`accept_announce`),
scan rate limit (`scan_due`) and keep-alive (`ensure_dependents`). Only the network and the
processes are fake. Nothing here touches a socket, a file or a real peer.

Invariants after every tick: no session on an unpinned host key or to a device that isn't the
main; learned addresses never contain what only a forger or a non-main announced; lists stay
capped; backoff stays <= 30 s; clipboard and Lan Mouse guard are re-ensured after recovery; and a
node whose main stays reachable (any path) for BOUND ticks is linked.
"""
import concurrent.futures
import hashlib
import hmac
import json
import os
import random
import time
import unittest
from unittest.mock import patch

from jarvis import cluster_recovery as r
from jarvis import linkfinder as f

SEED = 20261113
SCENARIOS = int(__import__("os").environ.get("LINK_FUZZ_N", 100_000))
DT = 15.0                      # seconds per tick
BOUND = 30                     # ticks (450 s): > one 300 s scan interval on both sides
DEVICES = ('a', 'b', 'c')
MAINS = ('a', 'b')
POOL = ['172.1.41.%d' % n for n in range(170, 176)]
MAGIC = {d: d + '.tail1234.ts.net' for d in DEVICES}
TSIP = {d: '100.64.0.%d' % (i + 1) for i, d in enumerate(DEVICES)}
HOST_KEY = {d: 'ssh-ed25519 K' + d for d in DEVICES}
SIGN_KEY = {d: 'code-' + d for d in DEVICES}
FLEET = 'fleet'
NOTE_KEYS = ('bad_sig', 'former', 'replay', 'stale', 'fleet', 'ghost')


def sign(data, key):
    return hmac.new(key.encode(), data, hashlib.sha256).hexdigest()


def verify(data, sig, key):
    return isinstance(sig, str) and hmac.compare_digest(sig, sign(data, key))


class Node:
    """Everything one device keeps as a node (and what the guard threads act on)."""

    def __init__(self, name):
        self.name = name
        self.learned = []           # addresses of the CURRENT main, per owner, freshest first
        self.peers = {}             # device -> learned list: kept per device across MAIN switches
        self.session = None
        self.health = r.SessionHealth()
        self.next_attempt = 0.0
        self.delay = 1
        self.last_scan = None
        self.down = 0               # ticks left of a reboot
        self.connected = False      # what the supervisor last saw (guard transition detector)
        self.clip = True
        self.guard = True
        self.bound = None           # IP the announce listener is bound to
        self.nonces = {}
        self.reach_ticks = 0
        self.seen = 0.0             # last time the main heard this node
        self.polls = 0
        self.session_key = self.session_pin = None


class World:
    def __init__(self, rng):
        self.rng = rng
        self.now = 1000.0
        self.main = rng.choice(MAINS)
        ips = rng.sample(POOL, len(DEVICES))
        self.ip = dict(zip(DEVICES, ips))
        self.group = {d: 'home' for d in DEVICES}
        self.ts = {d: True for d in DEVICES}
        self.awake = {d: True for d in DEVICES}
        self.hub_up = True
        self.impostor = {}          # ip -> host key of whatever now sits there
        # join-time addresses of each possible main: a stale LAN address, MagicDNS, Tailscale IP
        self.configured = {d: [rng.choice(POOL), MAGIC[d], TSIP[d]] for d in DEVICES}
        self.nodes = {d: Node(d) for d in DEVICES}
        self.main_scan = {d: None for d in DEVICES}
        self.ghost = {}             # demoted main -> ticks it still announces
        self.poison = set()         # addresses only a forger / non-main ever published
        self.last_note = None
        self.honest = set()
        self.serial = 0
        for d in DEVICES:
            self.nodes[d].bound = self.ip[d]
            self.nodes[d].seen = self.now
        for d in DEVICES:
            if d != self.main:
                self.nodes[d].session = self.target(d)
                self.nodes[d].connected = True

    # -- addressing -------------------------------------------------------------------------
    def addrs(self, device):
        return [self.ip[device], TSIP[device], MAGIC[device]]

    def target(self, node):
        return 'u@' + self.ip[self.main]

    def answer(self, node, address):
        """(device, host key) that answers port 22 at `address` as seen from `node`, or None."""
        for d in DEVICES:
            if not self.awake[d]:
                continue
            if address == self.ip[d] and self.group[d] == self.group[node] and d != node:
                return d, HOST_KEY[d]
            if address in (TSIP[d], MAGIC[d]) and self.ts[d] and self.ts[node] and d != node:
                return d, HOST_KEY[d]
        key = self.impostor.get(address)
        if key and any(self.ip[x] != address for x in (node,)):
            return 'x', key
        return None

    def reachable(self, node):
        main = self.main
        if node == main or not (self.awake[node] and self.awake[main] and self.hub_up
                                and self.nodes[node].down == 0 and self.nodes[main].down == 0):
            return False
        return self.group[node] == self.group[main] or (self.ts[node] and self.ts[main])


def run_scenario(world, counters):
    rng, w = world.rng, world
    ticks = 0

    def tick():
        nonlocal ticks
        ticks += 1
        counters['ticks'] += 1
        w.now += DT
        step(w, counters)

    for _ in range(rng.randint(1, 4)):
        event(w, counters)
        for _ in range(rng.randint(1, 3)):
            tick()
    # Heal enough for at least one path, then the link MUST recover within BOUND ticks.
    for d in DEVICES:
        w.awake[d] = True
    w.hub_up = True
    if rng.getrandbits(1):
        for d in DEVICES:
            w.ts[d] = True
            w.group[d] = rng.choice(('home', 'home', w.group[d]))
    else:
        for d in DEVICES:
            w.group[d] = 'home'
            w.ts[d] = rng.getrandbits(1)
    for d in DEVICES:
        w.nodes[d].reach_ticks = 0
    for _ in range(BOUND):
        tick()
        if all(w.nodes[d].session and w.hub_up for d in DEVICES if d != w.main):
            break
    for d in DEVICES:
        if d != w.main:
            assert w.reachable(d) is False or w.nodes[d].session, (
                'node %s never recovered main %s: ip=%s learned=%s configured=%s' % (
                    d, w.main, w.ip, w.nodes[d].learned, w.configured[w.main]))


def set_main(w, new):
    """MAIN switch: every node keeps learned addresses per device, so it swaps to the new main's."""
    for d in DEVICES:
        n = w.nodes[d]
        n.peers[w.main] = n.learned
        n.learned = n.peers.get(new, [])
    w.main = new


def event(w, counters):
    rng = w.rng
    kind = rng.randrange(15)
    counters['events'][kind] += 1
    node = rng.choice([d for d in DEVICES if d != w.main])
    if kind in (0, 1, 2):          # DHCP on main / node / both, addresses recycle through a small pool
        who = {0: [w.main], 1: [node], 2: [w.main, node]}[kind]
        for d in who:
            free = [p for p in POOL if p not in w.ip.values()]
            old = w.ip[d]
            w.ip[d] = rng.choice(free)
            if rng.getrandbits(2) == 0:
                w.impostor[old] = 'ssh-ed25519 EVIL'   # the freed address is now someone else
            w.nodes[d].bound = None if rng.getrandbits(1) else w.ip[d]   # listener rebinds on a later tick
            w.nodes[d].session = None if d == node else w.nodes[d].session
    elif kind == 3:
        w.ts[w.main] = False
    elif kind == 4:
        w.ts[node] = False
    elif kind == 5:
        w.ts = {d: True for d in DEVICES}
    elif kind == 6:                # main sleeps (hub and sshd gone) for a while
        w.awake[w.main] = False
        w.hub_up = False
    elif kind == 7:                # node reboot: clipboard + guard die, worker comes back via autostart
        n = w.nodes[node]
        n.down, n.session, n.connected, n.clip, n.guard = 2, None, False, False, False
        n.health = r.SessionHealth()
    elif kind == 8:                # main reboot: hub down, sessions drop
        w.nodes[w.main].down = 2
        w.hub_up = False
    elif kind == 9:                # hub restart: ssh alive, forwarded hub dead for a tick
        w.hub_up = False
        w.hub_back = 1
    elif kind == 10:               # agent update: restarts, clipboard needs re-ensuring
        n = w.nodes[node]
        n.connected, n.clip, n.guard = False, False, False
    elif kind == 11:               # MAIN switch; the demoted main keeps announcing a while
        old, new = w.main, ('b' if w.main == 'a' else 'a')
        set_main(w, new)
        w.ghost[old] = rng.randint(1, 4)
        w.ghost.pop(new, None)
        w.nodes[new].session = None
        w.nodes[new].connected = False
        for d in DEVICES:
            w.nodes[d].health = r.SessionHealth()
    elif kind == 12:               # a device leaves the home LAN (or returns)
        w.group[node] = 'away' if w.group[node] == 'home' else 'home'
    elif kind == 13:               # DHCP on main during a MAIN switch
        event_switch_during_dhcp(w)
    else:                          # forged traffic only
        forge(w, node)


def event_switch_during_dhcp(w):
    rng = w.rng
    old, new = w.main, ('b' if w.main == 'a' else 'a')
    free = [p for p in POOL if p not in w.ip.values()]
    w.ip[old] = rng.choice(free)
    free = [p for p in POOL if p not in w.ip.values()]
    w.ip[new] = rng.choice(free)
    set_main(w, new)
    w.ghost[old] = rng.randint(1, 4)
    w.ghost.pop(new, None)
    for d in DEVICES:
        w.nodes[d].session = None
        w.nodes[d].connected = False
        w.nodes[d].health = r.SessionHealth()


def packet(w, kind, sender, now):
    """An announce as `sender` would build it. Poison addresses never belong to anyone real."""
    w.serial += 1
    poison = '172.9.%d.%d' % divmod(w.serial % 60000, 250)
    body = {'main': sender, 'fleet': FLEET, 'timestamp': int(now), 'nonce': ('%048x' % w.serial),
            'addresses': [poison] if kind != 'honest' else w.addrs(sender)}
    key = SIGN_KEY[sender]
    if kind == 'bad_sig':
        key = 'forged'
    elif kind == 'former':          # a demoted main still signing with ITS key, claiming itself
        body['main'] = sender
    elif kind == 'stale':
        body['timestamp'] = int(now) - 600
    elif kind == 'fleet':
        body['fleet'] = 'other'
    if kind != 'honest':
        w.poison.add(poison)
    else:
        w.honest.add(body['nonce'])
    return {'body': body, 'signature': sign(f.packet_bytes(body), key)}


def body_nonce(note):
    body = note.get('body') if isinstance(note, dict) else None
    return body.get('nonce') if isinstance(body, dict) else None


def deliver(w, receiver, note):
    """The node's listener rule: pinned key of the CURRENT main, fresh, unseen. Returns bool."""
    node = w.nodes[receiver]
    seen = body_nonce(note) in node.nonces
    body = f.accept_announce(note, node.nonces, w.now, w.main, FLEET, SIGN_KEY[w.main], verify)
    if body is None:
        return False
    # Accepted: it must be an honest, first-time announce from the CURRENT main. Nothing else.
    assert not seen, 'replay accepted on %s' % receiver
    assert body['nonce'] in w.honest, 'forged announce accepted on %s' % receiver
    assert body['main'] == w.main, 'non-current main accepted on %s' % receiver
    node.learned = r.merge_values(body['addresses'], node.learned)
    node.next_attempt = w.now            # the reconnect marker
    return True


def forge(w, node):
    rng = w.rng
    kind = rng.choice(NOTE_KEYS)
    if kind == 'replay' and w.last_note is not None:
        note = w.last_note
    else:
        sender = w.main if kind in ('bad_sig', 'stale', 'fleet') else next(
            (g for g in w.ghost), 'a' if w.main == 'b' else 'b')
        note = packet(w, 'bad_sig' if kind == 'replay' else ('former' if kind == 'ghost' else kind),
                      sender, w.now)
    for target in [d for d in DEVICES if d != w.main]:
        deliver(w, target, note)        # deliver() asserts what may be accepted


def step(w, counters):
    rng = w.rng
    now = w.now
    if not w.hub_up and getattr(w, 'hub_back', 0):
        w.hub_back -= 1
        if not w.hub_back:
            w.hub_up = True
    for g in list(w.ghost):
        w.ghost[g] -= 1
        if w.ghost[g] <= 0:
            del w.ghost[g]
    if w.nodes[w.main].down:
        w.nodes[w.main].down -= 1
        if not w.nodes[w.main].down:
            w.awake[w.main] = True
            w.hub_up = True
    if not w.awake[w.main] and rng.getrandbits(1):      # main wakes, maybe with a new address
        w.awake[w.main] = True
        w.hub_up = True
        if rng.getrandbits(1):
            w.ip[w.main] = rng.choice([p for p in POOL if p not in w.ip.values()])
    for g in w.ghost:                                    # demoted main still announcing
        note = packet(w, 'former', g, now)
        for d in DEVICES:
            if d != w.main and d != g:
                assert not deliver(w, d, note), 'announce from demoted main %s accepted' % g
    if rng.random() < 0.15:
        forge(w, rng.choice(DEVICES))
    for d in DEVICES:
        n = w.nodes[d]
        if n.down:
            n.down -= 1
            continue
        if d == w.main:
            main_side(w, d, counters)
            continue
        node_side(w, d, counters)
    check(w)


def node_side(w, d, counters):
    n, now = w.nodes[d], w.now
    n.bound = w.ip[d]                                    # listener refresh (every 30 s) has caught up
    session = n.session
    if session is not None:
        dev = w.answer(d, session.split('@', 1)[1])
        if not dev or dev[0] != w.main or dev[1] != HOST_KEY[w.main]:
            n.session = session = None                    # ssh died, or the main changed under us
    healthy = session is not None and w.hub_up
    if session is not None and n.health.observe(now, healthy):
        n.session = session = None                        # live ssh, dead hub: rotate
        counters['rotations'] += 1
    if healthy:
        n.seen = now
        n.polls += 1
        # Authenticated poll (token MAC bound to main, node, nonce). Run on the first poll after a
        # link comes up and then periodically, which keeps 100k scenarios inside the time budget.
        if n.polls % 3 == 1:
            nonce = '%d' % w.serial
            token = 'tok-' + d
            values = w.addrs(w.main)
            note = {'values': values, 'signature': r.signature(token, w.main, d, nonce, values)}
            assert r.authenticated_addresses(note, token, w.main, d, nonce)
            n.learned = r.merge_values(values, n.learned)
            if n.polls % 9 == 1:
                forged = {'values': [next(iter(w.poison), '172.9.9.9')], 'signature': 'attacker'}
                assert not r.authenticated_addresses(forged, token, w.main, d, nonce)
    if healthy and not n.connected:
        run_dependents(w, d)
        counters['recoveries'] += 1
    n.connected = healthy
    if n.session is None and now >= n.next_attempt:
        cands = r.merge_candidates([], n.learned, ['u@' + a for a in w.configured[w.main]], 'u')
        assert len(cands) <= 32
        ordered = r.rank_targets(cands, lambda t: w.answer(d, t.split('@', 1)[1]) is not None)
        for target in ordered:
            ans = w.answer(d, target.split('@', 1)[1])
            # StrictHostKeyChecking=yes against the pinned key of the CURRENT main: any other
            # presented key (an impostor on a recycled address, a former main) is refused.
            if ans and ans[1] == HOST_KEY[w.main]:
                n.session = target
                n.session_key, n.session_pin = ans[1], HOST_KEY[w.main]
                n.delay = 1
                n.health = r.SessionHealth()
                break
        else:
            if r.scan_due(n.last_scan, now):
                n.last_scan = now
                counters['scans'] += 1
                found = [w.ip[x] for x in DEVICES if x != d and w.awake[x] and w.group[x] == w.group[d]
                         and w.ip[x] != w.ip[d] and HOST_KEY[x] == HOST_KEY[w.main] and x == w.main]
                if found:
                    n.learned = r.merge_values(found, n.learned)
            n.delay = r.next_backoff(n.delay)
            n.next_attempt = now + n.delay
            assert n.delay <= 30
    if w.reachable(d):
        n.reach_ticks += 1
        assert n.session or n.reach_ticks <= BOUND, (
            'node %s unlinked %d ticks with %s reachable' % (d, n.reach_ticks, w.main))
    else:
        n.reach_ticks = 0


def run_dependents(w, d):
    CUR[0], CUR[1] = w, d
    r.ensure_dependents({'clipboard': {'autostart': True}, 'lan_mouse': {'off_when_apart': True}}, 'state')


def main_side(w, d, counters):
    """The current main: scans for nodes silent > 60 s (at most one scan per node per 5 min),
    identifies them ONLY by their pinned credential, then sends a signed announce."""
    now = w.now
    for node in DEVICES:
        if node == d:
            continue
        n = w.nodes[node]
        if now - n.seen <= 60 or not r.scan_due(w.main_scan[node], now):
            continue
        w.main_scan[node] = now
        counters['main_scans'] += 1
        if (w.awake[node] and not n.down and w.group[node] == w.group[d] and n.bound == w.ip[node]):
            note = packet(w, 'honest', d, now)
            assert deliver(w, node, note), 'honest current-main announce rejected'
            w.last_note = note
            assert not deliver(w, node, note), 'replay of a delivered announce accepted'


def check(w):
    for d in DEVICES:
        n = w.nodes[d]
        assert len(n.learned) <= r.MAX_ADDRESSES
        assert not (w.poison & set(n.learned)), 'learned a forged/non-main address on %s' % d
        if n.session is not None:
            assert d != w.main
            ans = w.answer(d, n.session.split('@', 1)[1])
            assert n.session_key == n.session_pin, 'ssh session on an unpinned host key: %s' % n.session_key
        if n.connected and d != w.main and n.session and w.hub_up:
            assert n.clip and n.guard, 'clipboard/guard not re-ensured on %s' % d


CUR = [None, None]


def fake_pid(state):
    w, d = CUR
    return 1 if w.nodes[d].clip else None


def fake_start(state, *a):
    w, d = CUR
    w.nodes[d].clip = True


def fake_guard(config, state):
    w, d = CUR
    w.nodes[d].guard = True


def run_shard(bounds):
    """One shard of scenarios in its own process. Only fakes are patched, in this process."""
    lo, hi = bounds
    counters = {'ticks': 0, 'recoveries': 0, 'scans': 0, 'main_scans': 0, 'rotations': 0,
                'events': [0] * 15}
    with patch('jarvis.clipsync._pid', new=fake_pid), \
         patch('jarvis.clipsync.stopped_by_hand', new=lambda *a: False), \
         patch('jarvis.clipsync.start', new=fake_start), \
         patch('jarvis.sharedinput.guard_for', new=fake_guard):
        for i in range(lo, hi):
            try:
                run_scenario(World(random.Random(SEED + i)), counters)
            except AssertionError as exc:
                return 'scenario %d (seed %d): %s' % (i, SEED + i, exc)
    return counters


class LinkFuzz(unittest.TestCase):
    def test_seeded_state_machine_fuzz(self):
        workers = max(1, min(os.cpu_count() or 1, 16))
        size = -(-SCENARIOS // (workers * 4))
        shards = [(lo, min(lo + size, SCENARIOS)) for lo in range(0, SCENARIOS, size)]
        total = {'ticks': 0, 'recoveries': 0, 'scans': 0, 'main_scans': 0, 'rotations': 0,
                 'events': [0] * 15}
        start = time.monotonic()
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(run_shard, shards):
                self.assertIsInstance(result, dict, result)
                for key, value in result.items():
                    if key == 'events':
                        total[key] = [a + b for a, b in zip(total[key], value)]
                    else:
                        total[key] += value
        elapsed = time.monotonic() - start
        print('\nLink fuzz: %d scenarios, %d ticks, %d recoveries, %d node scans, %d main scans, '
              '%d rotations, events %s, %d workers, %.1fs' % (
                  SCENARIOS, total['ticks'], total['recoveries'], total['scans'], total['main_scans'],
                  total['rotations'], total['events'], workers, elapsed), flush=True)
        if SCENARIOS >= 100_000:
            self.assertGreater(total['recoveries'], SCENARIOS // 2)
            self.assertGreater(total['scans'], 1000)
            self.assertGreater(total['main_scans'], 1000)
            self.assertLess(elapsed, 60)


if __name__ == '__main__':
    unittest.main()
