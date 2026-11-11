"""Deterministic randomized checks over the JARVIS side of Lan Mouse (jarvis/sharedinput.py).

Every section draws from random.Random(SEED), so a failure reproduces exactly. Only fakes
touch the outside world: guard()'s start/stop/status callables are replaced, and in the
default-stop sweep the `systemctl`/`pkill` boundary (`_run`), `_units` and `lan_mouse_status`
are replaced too. Nothing starts, stops or signals a real Lan Mouse or service, and
JARVIS_NO_NOTIFY stops guard() from calling notify-send.

Known bugs are kept as @expectedFailure tests whose comments name them. Failing invariants
report the shortest failing event trace found, so a reproducer is at hand.
"""
import ipaddress
import os
import random
import shutil
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from jarvis import sharedinput as si

SEED = 20261105
GRACE = si.GUARD_GRACE
READ = {"T": True, "A": False, "N": None}  # together / apart / can't tell
TOKENS = ("T", "A", "N", "mstart", "mstop", "boot")
WEIGHTS = (4, 4, 2, 1.2, 1.2, 0.5)
N_GUARD = 7000        # guard sequences with fake start/stop (each reading is checked)
N_GUARD_DEFAULT = 1500  # guard sequences with the real stop() over a faked systemctl
N_CLASSIFY = 24000    # peer_nearby() against an ipaddress oracle
N_LOCAL_NETS = 6000   # local_networks() parsing of fake `ip -o addr` output
N_XML = 12000         # _task_xml() round-trip without CR
N_XML_CR = 2000       # _task_xml() round-trip including CR (expected failure)

COUNTS = {}


def _count(section, n):
    COUNTS[section] = COUNTS.get(section, 0) + n


class _Daemon:
    """Fake Lan Mouse: a running flag and call counters. start/stop only flip the flag."""

    def __init__(self, rng):
        self.running = rng.random() < 0.5
        self.starts = self.stops = 0
        self.clock = 10_000.0
        self.boot = 1_000_000.0
        self.guard_stop_clock = None  # the clock of the last stop the guard made

    def status(self):
        return {"running": self.running}

    def start(self):
        self.starts += 1
        self.running = True

    def note_stop(self):
        self.stops += 1
        self.guard_stop_clock = self.clock

    def stop(self):
        self.note_stop()
        self.running = False


def _run_sequence(rng, state, autostart, best, box, default_stop):
    """One random event sequence through guard(). Records each invariant violation found,
    keeping only the shortest trace per invariant."""
    (state / si.GUARD_FILE).unlink(missing_ok=True)
    d = _Daemon(rng)
    box["d"] = d
    stop_fn = (lambda: (si.stop(sharing_only=True), d.note_stop())) if default_stop else d.stop
    trace, hist = [], []

    def viol(name):
        text = f"autostart={autostart} " + " ".join(trace)
        if name not in best or len(text) < len(best[name]):
            best[name] = text

    flag = False               # stopped_by_guard as the last reading returned it
    stopped_since_boot = False
    manual_stopped = False     # a stop by hand, not undone until the next boot or start
    overridden = False         # a start by hand past the grace period, until a together reading
    seen_boot = None
    for _ in range(rng.randint(1, 14)):
        tok = rng.choices(TOKENS, WEIGHTS)[0]
        if tok == "boot":
            d.boot += 3600
            d.running = False  # a reboot ends the daemon
            d.clock += 600
            trace.append("boot")
            manual_stopped = stopped_since_boot = False
            continue
        if tok == "mstart":
            trace.append("mstart")
            if not d.running:
                d.running, manual_stopped = True, False
            continue
        if tok == "mstop":
            trace.append("mstop")
            if d.running:
                d.running, manual_stopped = False, True
            continue
        # a reading
        tick = rng.choice((30, 120))
        d.clock += tick
        trace.append(f"{tok}+{tick}")
        hist.append(tok)
        before_running, before_starts, before_stops = d.running, d.starts, d.stops
        boot_new = seen_boot != d.boot
        seen_boot = d.boot
        owned = flag
        if autostart and boot_new and not d.running:
            # the once-per-boot reset in guard(): a stopped daemon counts as the guard's
            owned, overridden, d.guard_stop_clock = True, False, 0.0
        if d.running and owned and d.guard_stop_clock is not None and d.clock - d.guard_stop_clock >= GRACE:
            overridden = True  # still running long after the guard's stop: a start by hand
        if tok == "T":
            overridden = False
        near = READ[tok]
        box["calls"] = []
        r = si.guard(state, nearby=lambda near=near: {"near": near, "peer": "Dell", "why": "fuzz."},
                     status_fn=d.status, start_fn=d.start, stop_fn=stop_fn,
                     autostart=autostart, boot=d.boot, now=d.clock)
        action = r["action"]
        trace[-1] += f"->{action or '-'}"
        stopped = d.stops > before_stops
        started = d.starts > before_starts
        if r.get("busy"):
            viol("busy")
        if stopped != (action == "stopped") or started != (action == "started"):
            viol("calls_match_action")
        if any("lan-mouse-ipwatch" in str(a) for c in box["calls"] for a in c):
            viol("stop_spares_ip_watcher")
        if action == "stopped":
            if hist[-2:] != ["A", "A"]:
                viol("stop_needs_two_apart_in_a_row")
            if overridden:
                viol("override_blocks_stop")
            if not before_running:
                viol("stop_only_if_running")
            stopped_since_boot = True
        if action == "started":
            if hist[-2:] != ["T", "T"]:
                viol("start_needs_two_together_in_a_row")
            if not owned:
                viol("start_only_after_guard_stop")
            if before_running:
                viol("start_only_if_stopped")
            if not autostart and not stopped_since_boot:
                viol("autostart_off_no_start_after_boot")
            if manual_stopped:
                viol("manual_stop_sticks")
        if tok == "N" and (action is not None or r["running"] != before_running):
            viol("cant_tell_changes_nothing")
        flag = bool(r["stopped_by_guard"])
    return len(hist)


def _sweep_guard(seed, n, default_stop):
    rng = random.Random(seed)
    best, box = {}, {"d": None, "calls": []}
    state = Path(tempfile.mkdtemp(prefix="lanmouse-fuzz-"))
    steps = 0
    try:
        patches = []
        if default_stop:
            def fake_run(*args, **kw):
                box["calls"].append(args)
                if "stop" in args and "lan-mouse.service" in args:
                    box["d"].running = False
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            patches = [
                mock.patch.object(si, "_units", lambda: ["lan-mouse.service", "lan-mouse-ipwatch.service"]),
                mock.patch.object(si, "_run", fake_run),
                mock.patch.object(si, "lan_mouse_status", lambda: {"running": box["d"].running, "build": None}),
                mock.patch.object(si.shutil, "which", lambda name: None),
            ]
        with mock.patch.dict(os.environ, {"JARVIS_NO_NOTIFY": "1"}):
            for p in patches:
                p.start()
            try:
                for _ in range(n):
                    steps += _run_sequence(rng, state, rng.random() < 0.5, best, box, default_stop)
            finally:
                for p in reversed(patches):
                    p.stop()
    finally:
        shutil.rmtree(state, ignore_errors=True)
    return best, steps


# -- 1. guard() over random event sequences --------------------------------------------------

class GuardSweep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.best, cls.steps = _sweep_guard(SEED, N_GUARD, default_stop=False)
        cls.best_default, cls.steps_default = _sweep_guard(SEED + 1, N_GUARD_DEFAULT, default_stop=True)
        _count("guard sequences (fake start/stop)", N_GUARD)
        _count("guard readings (fake start/stop)", cls.steps)
        _count("guard sequences (real stop, faked systemctl)", N_GUARD_DEFAULT)
        _count("guard readings (real stop, faked systemctl)", cls.steps_default)

    def assertNoViolation(self, name, best=None):
        best = self.best if best is None else best
        if name in best:
            self.fail(f"invariant {name!r} broken; shortest trace: {best[name]}")

    def test_never_stops_on_one_apart_reading(self):
        self.assertNoViolation("stop_needs_two_apart_in_a_row")

    def test_never_starts_on_one_together_reading(self):
        self.assertNoViolation("start_needs_two_together_in_a_row")

    def test_cant_tell_readings_change_nothing(self):
        self.assertNoViolation("cant_tell_changes_nothing")

    def test_stop_and_start_calls_match_the_reported_action(self):
        self.assertNoViolation("calls_match_action")
        self.assertNoViolation("busy")

    def test_stop_and_start_only_change_the_daemon_state_they_should(self):
        self.assertNoViolation("stop_only_if_running")
        self.assertNoViolation("start_only_if_stopped")
        self.assertNoViolation("start_only_after_guard_stop")

    def test_manual_start_is_an_override_until_together_again(self):
        self.assertNoViolation("override_blocks_stop")

    @unittest.expectedFailure
    def test_manual_stop_sticks_until_boot(self):
        # BUG (suspected): with autostart on, the first reading after a boot treats any stopped
        # daemon as the guard's (boot reset in guard()). A stop by hand made before that first
        # reading is therefore undone by the guard on two together readings. Minimal shape:
        # autostart, running -> mstop -> together, together -> started.
        self.assertNoViolation("manual_stop_sticks")

    def test_stop_never_touches_the_ip_watcher(self):
        self.assertNoViolation("stop_spares_ip_watcher", self.best_default)
        self.assertNoViolation("calls_match_action", self.best_default)

    def test_autostart_off_never_starts_after_boot(self):
        # BUG (suspected): guard() only writes memo["stopped_by_guard"] on a boot when autostart
        # is on. With autostart off, a stop from before a reboot keeps stopped_by_guard=True, so
        # after the reboot the guard starts Lan Mouse on two together readings. Minimal shape:
        # apart, apart (guard stops) -> boot -> together, together -> started.
        self.assertNoViolation("autostart_off_no_start_after_boot")


# -- 2. peer_nearby(): "together" iff the peer is on one of this computer's own subnets -------

POOL_NETS = ["172.1.41.0/24", "10.0.0.0/8", "192.168.0.0/16", "fd00:1::/64", "100.64.0.0/10"]


def _rand_nets(rng):
    nets = [ipaddress.ip_network(s) for s in POOL_NETS if rng.random() < 0.3]
    for _ in range(rng.randint(0, 2)):
        if rng.random() < 0.6:
            addr = ipaddress.IPv4Address(rng.getrandbits(32))
            nets.append(ipaddress.ip_network(f"{addr}/{rng.randint(8, 30)}", strict=False))
        else:
            addr = ipaddress.IPv6Address(rng.getrandbits(128))
            nets.append(ipaddress.ip_network(f"{addr}/{rng.randint(8, 124)}", strict=False))
    return nets


def _rand_host(rng, nets):
    if nets and rng.random() < 0.5:  # inside one of the subnets, so both answers occur
        n = rng.choice(nets)
        return str(n.network_address + rng.randrange(n.num_addresses))
    if rng.random() < 0.5:
        return str(ipaddress.IPv4Address(rng.getrandbits(32)))
    return str(ipaddress.IPv6Address(rng.getrandbits(128)))


def _oracle_inside(host, nets):
    addr = ipaddress.ip_address(host)
    return any(addr.version == n.version and addr in n for n in nets)


class PeerNearbyClassification(unittest.TestCase):
    def test_together_iff_peer_address_is_inside_an_own_subnet(self):
        rng = random.Random(SEED + 2)
        config = {"clients": [{"hostname": "WinLaptop", "ips": ["100.64.0.13"]}]}
        mismatches = []
        for _ in range(N_CLASSIFY):
            nets = _rand_nets(rng)
            online = rng.random() < 0.8
            host = _rand_host(rng, nets) if rng.random() < 0.9 else ""
            if host and ":" in host:
                cur = f"[{host}]:{rng.randint(1, 65535)}"
            else:
                cur = f"{host}:{rng.randint(1, 65535)}" if host else ""
            status = {"BackendState": "Running", "Peer": {"k": {
                "HostName": "WinLaptop", "Online": online, "CurAddr": cur,
                "TailscaleIPs": ["100.64.0.13"]}}}
            got = si.peer_nearby(config, status, nets)["near"]
            if not online:
                want = False
            elif not host:
                want = None
            else:
                want = _oracle_inside(host, nets)
            if got is not want:
                mismatches.append((got, want, cur, [str(n) for n in nets], online))
        _count("peer_nearby classifications", N_CLASSIFY)
        self.assertEqual(mismatches, [], f"{len(mismatches)} mismatches; first: {mismatches[:1]}")


# -- 2b. local_networks(): parse fake `ip -o addr` output, virtual interfaces left out ----------

PHYSICAL = ["wlan0", "eth0", "enp3s0", "wlp2s0", "Ethernet"]
VIRTUAL = ["tun0", "tailscale0", "docker0", "br-ab12", "veth1", "virbr0", "vmnet8", "wg0",
           "utun3", "lo", "vEthernet (x)", "cni0", "zt0", "ppp0", "vboxnet0"]


class LocalNetworksParsing(unittest.TestCase):
    def test_subnets_match_the_physical_interfaces_in_ip_output(self):
        rng = random.Random(SEED + 3)
        failures = []
        for _ in range(N_LOCAL_NETS):
            lines, expected = [], []
            families = rng.choice([(4,), (4, 6)])
            for idx in range(rng.randint(0, 8)):
                name = rng.choice(PHYSICAL + VIRTUAL)
                fam = rng.choice(["inet", "inet6"])
                if fam == "inet":
                    addr = str(ipaddress.IPv4Address(rng.getrandbits(32)))
                    plen = rng.randint(0, 32)
                    cidr = f"{addr}/{plen}"
                    text = f"{idx + 1}: {name}    inet {cidr} brd 1.2.3.255 scope global {name}"
                else:
                    addr = str(ipaddress.IPv6Address(rng.getrandbits(128)))
                    plen = rng.randint(0, 128)
                    if rng.random() < 0.25:
                        addr = f"fe80::{rng.getrandbits(16):x}%{name}"  # zone id: must be dropped
                    cidr = f"{addr}/{plen}"
                    text = f"{idx + 1}: {name}    inet6 {cidr} scope link"
                lines.append(text)
                if name in PHYSICAL:
                    net = ipaddress.ip_interface(f"{addr.split('%')[0]}/{plen}").network  # zone dropped
                    if net.version in families and not net.is_loopback and 8 <= net.prefixlen < net.max_prefixlen:
                        expected.append(str(net))
            out = types.SimpleNamespace(stdout="\n".join(lines), returncode=0)
            with mock.patch.object(si, "on_windows", lambda: False), \
                 mock.patch.object(si, "_run", lambda *a, **k: out), \
                 mock.patch.object(si.shutil, "which", lambda name: "/fake/ip" if name == "ip" else None):
                got = [str(n) for n in si.local_networks(families)]
            if got != expected:
                failures.append((families, lines, got, expected))
        _count("local_networks parses", N_LOCAL_NETS)
        self.assertEqual(failures, [], f"{len(failures)} mismatches; first: {failures[:1]}")


# -- 3. _task_xml(): round-trip through xml.etree ---------------------------------------------

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
XML_ALPHABET = list("abcXYZ019 _-.\\/:") + list("&<>\"'") + [
    "é", "漢", "😀", " ", " ", "\t", "\n", "]]>", "&amp;", "&#x41;", "%PATH%", "$(x)"]


def _rand_text(rng, alphabet):
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))  # non-empty: an empty element reads back as None


def _check_task_xml(command, arguments, user):
    text = si._task_xml(command, arguments, user)
    root = ET.fromstring(text)  # must be well-formed
    got = {
        "Command": root.find(f".//{NS}Command").text,
        "Arguments": root.find(f".//{NS}Arguments").text,
        "UserId": root.find(f".//{NS}UserId").text,
    }
    return got == {"Command": command, "Arguments": arguments, "UserId": user}, got


class TaskXmlRoundTrip(unittest.TestCase):
    def test_exact_strings_survive_xml_parsing(self):
        rng = random.Random(SEED + 4)
        failures = []
        for _ in range(N_XML):
            cmd, args, user = (_rand_text(rng, XML_ALPHABET) for _ in range(3))
            ok, got = _check_task_xml(cmd, args, user)
            if not ok:
                failures.append((cmd, args, user, got))
        _count("_task_xml round-trips", N_XML)
        self.assertEqual(failures, [], f"{len(failures)} failures; first: {failures[:1]}")

    def test_carriage_returns_survive_xml_parsing(self):
        # BUG: _task_xml() escapes only & < > (xml.sax.saxutils.escape), so a literal CR in
        # the command or arguments is read back as LF: XML end-of-line normalisation. Minimal:
        # _task_xml("c", "a\rb", "u") -> Arguments reads back as "a\nb".
        rng = random.Random(SEED + 5)
        failures = []
        for _ in range(N_XML_CR):
            cmd, args, user = (_rand_text(rng, XML_ALPHABET + ["\r", "\r\n"]) for _ in range(3))
            ok, got = _check_task_xml(cmd, args, user)
            if not ok:
                failures.append((cmd, args, user, got))
        _count("_task_xml round-trips with CR (expected failure)", N_XML_CR)
        self.assertEqual(failures, [], f"{len(failures)} failures; first: {failures[:1]}")


def tearDownModule():
    # The actual case counts, as the sections ran them.
    print("\nlanmouse fuzz case counts:", *(f"\n  {k}: {v}" for k, v in COUNTS.items()), flush=True)


if __name__ == "__main__":
    unittest.main()
