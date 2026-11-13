"""One supervised, host-key-pinned SSH link between the main and a node, in either
direction (cluster.tunnel: reverse = main -> node, forward = node -> main).

Stop uses a cooperative marker, not a stale PID that could kill an unrelated process.
Windows launches through Explorer so the worker outlives the invoking CLI job.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .platforms import NO_WINDOW, on_windows
from .cluster import save_json
from .locking import exclusive_lock
from .topology import DEFAULT_PORT, identity
from .transfer import _check_target, _ssh_args, ssh_device, ssh_host

NAME = "cluster-tunnel"
# Operator stop. The worker deletes cluster-tunnel.stop when it starts, so that
# marker cannot keep the supervisor from bringing the tunnel back.
MANUAL_STOP = "cluster-tunnel.manual-stop"
BOOT_JITTER = 120  # same boot window as a clipboard stop by hand
ROLE_KEYS = ("local", "main", "mode", "link")
# While connected on a worse target, re-probe better ones this often. The interval does not
# grow with session length and does not use the reachability rank from when SSH started
# (that rank puts the only open port first, so the LAN was never checked again).
FAILBACK_PROBE_S = 60
# Port-22 check for ranking and failback. Short, and failback runs the checks together, so a
# round cannot stall the 5 s health loop. status() treats a note older than 15 s as dead.
REACH_TIMEOUT = 0.3
# LAN_RETRY is the same interval under the older name. LAN_RETRY_MAX is the old ceiling for a
# stretched gap; the probe does not stretch.
LAN_RETRY, LAN_RETRY_MAX = FAILBACK_PROBE_S, 30 * 60
# A session counts as stable (backoff back to 1 s) only after STABLE_SESSION seconds; a shorter
# one leaves the backoff growing, so a tunnel that flaps every few minutes isn't redialled every
# second. TARGET_PAUSE separates attempts on successive targets.
STABLE_SESSION, TARGET_PAUSE = 600, 1
HEARTBEAT = 5  # seconds between liveness refreshes of the status note (status() allows 15)


def reachable(target, timeout=2):
    """Cheap check that the target's sshd answers; the SSH attempt itself proves the rest.

    The supervisor passes REACH_TIMEOUT. The default is for one-off checks outside the
    5 s health loop.
    """
    try:
        socket.create_connection((target.split("@", 1)[1], 22), timeout=timeout).close()
        return True
    except OSError:
        return False


def targets_ahead(fresh, current):
    """Better targets in the latest candidates() order, not reachability-rank order.

    A refreshed list can put a learned address in front of the target SSH started on.
    A target that has dropped out of the list yields the whole new list.
    """
    try:
        place = fresh.index(current)
    except ValueError:
        return [item for item in fresh if item != current]
    return list(fresh[:place])


def preferred_failback(config, state, current):
    """Best reachable target ahead of the connected one, or None.

    candidates() is read again so an address learned during a long session is visible.
    The probes share one short timeout and run together.
    """
    try:
        ahead = targets_ahead(candidates(config, state), current)
    except (OSError, ValueError):
        return None
    if not ahead:
        return None
    from .cluster_recovery import parallel_probe
    try:
        hits = parallel_probe(lambda item: reachable(item, timeout=REACH_TIMEOUT), ahead)
    except OSError:
        return None
    return next((item for item, ok in zip(ahead, hits) if ok), None)


def pinned_host(machine, state):
    alias = machine.get("host_key_alias")
    key = machine.get("host_key")
    if key:
        alias = "jarvis-" + machine.get("hostname", "peer")
    if alias and not re.fullmatch(r"[A-Za-z0-9_.-]+", alias):
        raise ValueError("Invalid SSH host key alias.")
    if not key:
        return ["-o", f"HostKeyAlias={alias}"] if alias else []
    if not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-[A-Za-z0-9-]+) [A-Za-z0-9+/=]+", key.strip()):
        raise ValueError("Invalid pinned SSH host key.")
    path = Path(state).resolve() / f"known-host-{alias}"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"{alias} {key.strip()}\n"
    if not path.exists() or path.read_text(encoding="ascii") != text:
        path.write_text(text, encoding="ascii")
    return ["-o", f"HostKeyAlias={alias}", "-o", f"UserKnownHostsFile={path}"]


def mode(config):
    """reverse (default): the main runs `ssh -R` into each node, so nodes need an SSH
    server. forward: each node runs `ssh -L` to the main, so only the main needs one
    (e.g. a Linux or macOS main with a Windows node). Either way the node reaches the
    main's hub on its own 127.0.0.1:<listen_port>."""
    value = (config.get("cluster") or {}).get("tunnel", "reverse")
    if value not in ("reverse", "forward"):
        raise ValueError('cluster.tunnel must be "reverse" or "forward".')
    return value


def link(config, state):
    """The SSH link `jarvis cluster join` saved for this node (forward mode), as a device
    entry; None if the node was paired the old way and its link lives in the config."""
    try:
        note = json.loads((Path(state) / "cluster-node.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    saved = note.get("link") if isinstance(note, dict) else None
    if not isinstance(saved, dict) or note.get("main_device") != identity(config)[1]:
        return None
    targets = [t for t in saved.get("targets", []) if isinstance(t, str)]
    if not targets:
        return None
    return {"transport": "ssh", "target": targets[0], "fallback_targets": targets[1:],
            "host_key": saved.get("host_key"), "hostname": saved.get("hostname") or "main",
            "identity_file": saved.get("identity_file")}


def _peer_pin(machine):
    """SSH identity of a peer, ignoring which address this session dialed.

    Learned and failback addresses move without a new signed link. A new target
    list, host key, hostname, or identity file is a different link: the live SSH
    must not keep the previous one.
    """
    targets = tuple(item for item in [machine.get("target"), *(machine.get("fallback_targets") or [])] if item)
    return (targets, machine.get("host_key"), machine.get("hostname"), machine.get("identity_file"))


def peer(config, state=None):
    """The machine this end opens SSH to: a node (reverse, run on the main) or the main
    (forward, run on a node; the joined link in `state` wins over the config)."""
    local, main = identity(config)
    if mode(config) == "forward":
        if local == main:
            raise ValueError("With cluster.tunnel = forward, each node runs the tunnel to the main.")
        joined = link(config, state) if state is not None else None
        if joined:
            return joined
        machine = (config.get("devices") or {}).get(main) or {}
        if machine.get("transport") != "ssh" or not machine.get("target"):
            raise ValueError(f"Give devices.{main} an SSH target (transport ssh) so this node can reach it.")
        return machine
    if local != main:
        raise ValueError("With cluster.tunnel = reverse, the main runs the tunnel to its nodes.")
    return ssh_device(config, config.get("cluster", {}).get("tunnel_device"))


def candidates(config, state):
    """Tunnel targets, best first: verified scan finds, freshest learned addresses, then the
    ones saved at join time. Every target is validated; the list is capped."""
    from .cluster_recovery import merge_candidates
    machine = peer(config, state)
    configured = [t for t in [machine.get("target"), *machine.get("fallback_targets", [])] if t]
    fresh, found = [], []
    if configured:
        from .linkfinder import learned, device_for_peer, addresses_for, discovered
        device = device_for_peer(config, machine)
        fresh = addresses_for(config, state, device)
        if mode(config) == "forward":
            fresh = fresh or learned(config, state)
        found = discovered(machine, state)
    result = merge_candidates(found, fresh, configured, configured[0].split("@", 1)[0] if configured else "")
    for target in result:
        _check_target(target)
    return result


def hub_healthy(config, state):
    """Probe the forwarded hub, not merely a live ssh -N process."""
    from .cluster import Client
    from .continuity.runtime import RuntimeRefusal
    try:
        from .cluster_recovery import signature
        import secrets
        import hmac
        client = Client(config, state)
        nonce = secrets.token_hex(24)
        response = client.request("/v1/link-health", {"nonce": nonce}, timeout=2)
        expected = signature(client.token, client.main, client.local, nonce, [])
        return (isinstance(response, dict) and isinstance(response.get("signature"), str)
                and hmac.compare_digest(response["signature"], expected))
    except (OSError, ValueError, RuntimeError, RuntimeRefusal):
        # A continuity refusal is a down tunnel. It is not a live handshake.
        return False


def command(config, state, target):
    machine = peer(config, state)
    _check_target(target)
    binary = shutil.which("ssh")
    if not binary:
        raise ValueError("OpenSSH client is not installed.")
    port = int(config.get("cluster", {}).get("listen_port", DEFAULT_PORT))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid cluster listen port.")
    pending = Path(state) / "cluster-node-pending.key" if state is not None else None
    extra = ["-i", str(pending)] if pending and pending.exists() else []  # see cluster.redeem_invite
    return [*_ssh_args(binary, machine.get("identity_file")), *extra, *pinned_host(machine, state),
            "-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "-L" if mode(config) == "forward" else "-R",
            f"127.0.0.1:{port}:127.0.0.1:{port}", *ssh_host(target)]


def status(state):
    from .daemon import _alive
    state = Path(state)
    try:
        note = json.loads((state / f"{NAME}.json").read_text(encoding="utf-8"))
        running = bool(int(note["pid"]) > 0 and _alive(int(note["pid"])) and time.time() - note["updated"] < 15)
    except (OSError, ValueError, KeyError, TypeError):
        note, running = {}, False
    return {**note, "running": running, "ssh_alive": running and note.get("ssh_alive", False),
            "hub_healthy": running and note.get("hub_healthy", False),
            "autostart": startup_path().is_file(),
            "manual_stop": manual_stop_path(state).is_file()}


def current_boot():
    from .platforms import boot_time
    return boot_time()


def manual_stop_path(state):
    return Path(state) / MANUAL_STOP


def clear_manual_stop(state):
    try:
        manual_stop_path(state).unlink(missing_ok=True)
    except OSError:
        pass


def link_disposition(state):
    """How this role treats the forward tunnel. Matches ensure_tunnel's link rule."""
    from .fsio import read_json
    path = Path(state) / "continuity-endpoint.json"
    if not path.exists():
        return "plain"
    instruction = read_json(path, {})
    if not instruction:
        return "plain"
    body = instruction.get("body") if isinstance(instruction, dict) else None
    if not isinstance(body, dict) or body.get("link") is None:
        return "off"
    return "on"


def tunnel_role(config, state):
    from .linkfinder import role
    resolved, local, main = role(config, state)
    return {"local": local, "main": main, "mode": mode(resolved), "link": link_disposition(state)}


def record_manual_stop(config, state):
    """Persist an operator stop for this boot and role. Does not edit config."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    save_json(manual_stop_path(state), {**tunnel_role(config, state), "boot": current_boot()})


def _same_boot(saved, now):
    if saved is None or now is None:
        return True  # unknown boot is not permission to start
    try:
        return abs(float(saved) - float(now)) <= BOOT_JITTER
    except (TypeError, ValueError):
        return True


def manual_stop_holds(config, state):
    """True when an operator stop still applies to this boot and this role.

    A different role or a later boot deletes the record. Promotion, demotion, and
    a continuity link that turns the forward tunnel off must not leave a stuck
    disabled tunnel, and this function does not write config.
    """
    path = manual_stop_path(state)
    if not path.exists():
        return False
    from .fsio import read_json
    note = read_json(path, None)
    if not isinstance(note, dict) or any(key not in note for key in (*ROLE_KEYS, "boot")):
        clear_manual_stop(state)
        return False
    if not _same_boot(note.get("boot"), current_boot()):
        clear_manual_stop(state)
        return False
    current = tunnel_role(config, state)
    if any(note.get(key) != current[key] for key in ROLE_KEYS):
        clear_manual_stop(state)
        return False
    return True


def worker_locked(state):
    """True when this state's tunnel worker holds cluster-tunnel.lock.

    A stale status note is not ownership: the pid in it can be reused. A missing
    lock file is not a holder. The check takes the lock only when nobody else has
    it, then releases it before the caller starts a worker.
    """
    path = Path(state) / f"{NAME}.lock"
    if not path.exists():
        return False
    from .locking import LockBusy, exclusive_lock
    try:
        with exclusive_lock(path, timeout=0):
            return False
    except LockBusy:
        return True
    except OSError:
        return False


def _reap(proc):
    """Stop a live ssh and wait with a deadline. False when it never exits.

    An unbounded wait keeps the worker lock, so the next start dies LockBusy
    while this process no longer publishes a heartbeat.
    """
    if proc is None or proc.poll() is not None:
        return True
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return True


def _discover_keeping_heartbeat(config, state, publish, marker):
    """Scan without letting the status note go stale, and leave when asked to stop.

    discover's own budget is longer than the 15s status window. A handoff that
    sees a dead note starts another worker against a lock this process still
    holds. Returning 'stop' releases that lock without waiting out the scan.
    """
    from .linkfinder import discover
    box = {}
    done = threading.Event()

    def work():
        try:
            discover(peer(config, state), state, config)
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, name="cluster-tunnel-discover", daemon=True).start()
    while not done.wait(0.05):
        publish()
        if marker.exists():
            return "stop"
    if "error" in box:
        raise box["error"]
    return "ok"


def run(config, state):
    from .continuity.runtime import RuntimeRefusal
    try:
        if manual_stop_holds(config, state):
            return  # operator stop: do not delete it and do not open SSH
    except RuntimeRefusal:
        return  # unverifiable continuity state: do not open SSH and do not crash-loop
    from .linkfinder import role
    original_config = config
    try:
        config, _, _ = role(config, state)
    except RuntimeRefusal:
        return
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    machine = peer(config, state)
    targets = candidates(config, state)
    if not targets:
        raise ValueError("No cluster tunnel targets configured.")
    # Validate before claiming the worker lock.
    for target in targets:
        command(config, state, target)
    marker = state / f"{NAME}.stop"
    note = {"pid": os.getpid(), "ssh_alive": False, "hub_healthy": False}
    written = {}  # last content saved (sans `updated`) and when; wait() calls publish ~4x/s

    def publish(**values):
        # Write only when the content changed or the heartbeat is due: status() treats a
        # note older than 15 s as dead, so refreshing `updated` every HEARTBEAT s keeps it live.
        now = time.time()
        body = {k: v for k, v in {**note, **values}.items() if k != "updated"}
        if body == written.get("body") and now - written.get("at", 0) < HEARTBEAT:
            return
        note.update(values, updated=now)
        # Windows refuses to replace a file another process is reading (WinError 5), and
        # `cluster tunnel status` or the console reads this one. A status note must never
        # kill the tunnel: retry briefly, then skip; the next publish (<= 1 s) refreshes it.
        for _ in range(5):
            try:
                save_json(state / f"{NAME}.json", note)
                written.update(body=body, at=now)
                return
            except PermissionError:
                time.sleep(0.05)

    def wait(seconds):
        until = time.monotonic() + seconds
        while not marker.exists() and time.monotonic() < until:
            publish()
            time.sleep(min(0.25, max(0, until - time.monotonic())))

    with exclusive_lock(state / f"{NAME}.lock"):
        marker.unlink(missing_ok=True)
        delay, proc = 1, None
        logs = state / "logs"
        logs.mkdir(exist_ok=True)
        try:
            refused = None
            while not marker.exists():
                try:
                    config, local, main = role(original_config, state)
                except RuntimeRefusal as exc:
                    # A bad signed note must not keep the previous SSH or raise once
                    # per probe (systemd Restart=on-failure would burn the start limit).
                    publish(ssh_alive=False, hub_healthy=False, error=exc.code)
                    break
                if mode(config) == "forward" and local == main:
                    break  # a promoted main no longer owns a node's forward tunnel
                targets = candidates(config, state)  # pick up addresses learned while connected
                # Probe every candidate first; never spend a full SSH timeout on a dead LAN.
                from .cluster_recovery import rank_targets, SessionHealth, parallel_probe, next_backoff
                ordered = rank_targets(targets, lambda t: reachable(t, timeout=REACH_TIMEOUT), parallel_probe)
                refresh = False
                for target in ordered:
                    if marker.exists():
                        break
                    started = probed = time.monotonic()
                    preferred = None
                    refresh = False
                    health = SessionHealth()
                    publish(target=target, ssh_alive=False, hub_healthy=False, error=None)
                    try:
                        with (logs / f"{NAME}.log").open("a", encoding="utf-8") as log:
                            session_pin = _peer_pin(peer(config, state))
                            proc = subprocess.Popen(command(config, state, target), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=log,
                                creationflags=NO_WINDOW)
                            while proc.poll() is None and not marker.exists():
                                reconnect = state / 'cluster-tunnel-reconnect'
                                if reconnect.exists():
                                    reconnect.unlink(missing_ok=True)
                                    refresh = True
                                    proc.terminate()
                                    break
                                try:
                                    resolved, _local, current_main = role(original_config, state)
                                except RuntimeRefusal as exc:
                                    refused = exc
                                    proc.terminate()
                                    break
                                if current_main != identity(config)[1]:
                                    refresh = True
                                    proc.terminate()
                                    break
                                try:
                                    if _peer_pin(peer(resolved, state)) != session_pin:
                                        # Same main, new signed link: drop this SSH before
                                        # the next dial. Do not keep the old host key.
                                        refresh = True
                                        proc.terminate()
                                        break
                                except (OSError, ValueError):
                                    refresh = True
                                    proc.terminate()
                                    break
                                healthy = (hub_healthy(config, state) if mode(config) == "forward"
                                           else time.monotonic() - started >= 9)
                                publish(ssh_alive=healthy, hub_healthy=healthy)
                                if health.observe(time.monotonic(), healthy):
                                    proc.terminate()
                                    break
                                wait(5)
                                # Fresh preference order, not ordered[:index] from this session's rank.
                                if time.monotonic() - probed >= FAILBACK_PROBE_S:
                                    probed = time.monotonic()
                                    preferred = preferred_failback(config, state, target)
                                    if preferred:
                                        proc.terminate()
                                        break
                            if (marker.exists() or refresh or refused is not None) and proc.poll() is None:
                                proc.terminate()
                            proc.wait(timeout=5)
                            publish(ssh_alive=False, hub_healthy=False, error=f"{preferred} is reachable again; switching back"
                                    if preferred else f"SSH exited ({proc.returncode})")
                    except (OSError, subprocess.SubprocessError) as exc:
                        publish(ssh_alive=False, hub_healthy=False, error=str(exc))
                    finally:
                        if not _reap(proc):
                            publish(ssh_alive=False, hub_healthy=False, error="SSH did not exit")
                            marker.touch()
                        proc = None
                    if refused is not None or refresh:
                        break  # reload the signed role, or leave, before another SSH
                    lasted = time.monotonic() - started
                    if lasted >= STABLE_SESSION:
                        delay = 1
                    if preferred:
                        break  # next round ranks again and dials the better target
                    wait(TARGET_PAUSE)  # no instant redial of the next target
                if refused is not None:
                    publish(ssh_alive=False, hub_healthy=False, error=refused.code)
                    break
                if refresh:
                    continue  # a new main or signed link is dialed without the scan budget
                if not marker.exists():
                    if _discover_keeping_heartbeat(config, state, publish, marker) == "stop":
                        break
                wait(delay)
                delay = next_backoff(delay)
        finally:
            publish(ssh_alive=False, hub_healthy=False, pid=0)


TITLE = "JARVIS Cluster Tunnel"


def startup_path():
    from .autostart import path
    return path("jarvis-cluster-tunnel", TITLE)


def launcher(config_path, state):
    python = Path(sys.executable)
    if on_windows() and python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")
    argv = [str(python), "-m", "jarvis", "--config", str(Path(config_path).resolve()),
            "--state", str(Path(state).resolve()), "cluster", "tunnel", "run"]
    if any("\n" in arg or "\r" in arg for arg in argv):
        raise ValueError("Newlines are not allowed in startup paths.")
    return argv


def autostart(config_path, state, enable):
    from .autostart import install
    return install("jarvis-cluster-tunnel", TITLE, launcher(config_path, state), enable)


def start(config_path, state):
    # Explicit start clears the operator stop. ensure_tunnel does not call start
    # while that stop holds, so recovery cannot clear it.
    clear_manual_stop(state)
    if status(state)["running"]:
        return {"started": False, **status(state)}
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    if worker_locked(state):
        # The holder is alive even when its heartbeat is stale. A second process
        # would die on the lock and leave the old SSH up. Ask it to exit, then
        # take over only after it releases the lock.
        (state / f"{NAME}.stop").touch()
        for _ in range(40):
            time.sleep(0.25)
            if not worker_locked(state):
                break
        else:
            return {"started": False, "note": "Tunnel worker still holds its lock.", **status(state)}
    argv = launcher(config_path, state)
    if on_windows():
        script = state / f"{NAME}-launch.vbs"
        from .autostart import vbs
        line = subprocess.list2cmdline(argv).replace('"', '""')
        script.write_text(vbs(line), encoding="utf-8")
        subprocess.Popen(["explorer.exe", str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        time.sleep(0.25)
        if status(state)["running"]:
            return {"started": True, **status(state)}
    return {"started": False, "note": "Worker did not start; inspect configuration and the tunnel log.", **status(state)}


def stop(state, *, operator=False, config=None, sleep=None):
    """Ask the worker to exit. operator=True persists that choice for this boot and role.

    Role lifecycle and leave call stop without operator, so their marker is only
    the cooperative request. sleep is the pause between status checks (tests pass
    their own). Config is not rewritten.
    """
    pause = time.sleep if sleep is None else sleep
    if operator:
        if config is None:
            raise ValueError("An operator tunnel stop needs the current config.")
        record_manual_stop(config, state)
    if not status(state)["running"]:
        return {"stopped": False, "note": "Not running.", "manual_stop": operator}
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    (state / f"{NAME}.stop").touch()
    for _ in range(40):
        pause(0.25)
        if not status(state)["running"]:
            return {"stopped": True, "manual_stop": operator}
    return {"stopped": False, "note": "Stop requested; SSH is still shutting down.",
            "manual_stop": operator}
