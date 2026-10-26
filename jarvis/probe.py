"""Live link checks for the console: is each laptop reachable, and is Lan Mouse up?

- Local (Windows): the Lan Mouse process and its UDP sockets from `tasklist` and
  `netstat`, which also show which peer address the daemon is talking to. Local on
  Arch runs the remote script below in-process.
- Remote (SSH devices): one fixed, read-only bash script sent over stdin with the
  configured key, BatchMode and strict host keys, so it never prompts and never
  installs anything. It reports the Lan Mouse units, the UDP 4242 socket, memory
  pressure and Wi-Fi power save (the two known causes of Lan Mouse lag), and the
  laptop's current IP, because DHCP can move it away from the paired address.

A daemon that is up and holding its socket is not proof that input crosses the
link; the result says "daemons up", not "working".
"""

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

PORT = 4242
REMOTE_SCRIPT = r"""
echo "host=$(cat /proc/sys/kernel/hostname)"
echo "uptime=$(cut -d' ' -f1 /proc/uptime)"
echo "load=$(cut -d' ' -f1 /proc/loadavg)"
echo "lm_unit=$(systemctl --user is-active lan-mouse.service 2>/dev/null)"
echo "lm_fallback=$(systemctl --user is-active lan-mouse-fallback.service 2>/dev/null)"
echo "lm_udp=$(ss -uanH 'sport = :4242' 2>/dev/null | wc -l)"
awk '/^MemTotal/{t=$2} /^MemAvailable/{a=$2} /^SwapTotal/{st=$2} /^SwapFree/{sf=$2}
     END{print "mem_total_kb="t; print "mem_avail_kb="a; print "swap_total_kb="st; print "swap_free_kb="sf}' /proc/meminfo
IW=$(command -v iw || echo /usr/sbin/iw)
echo "powersave=$($IW dev wlan0 get power_save 2>/dev/null | awk '{print $NF}')"
echo "ip=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
"""


def _run(args, timeout, **kw):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, shell=False,
                          creationflags=flags, **kw)


def parse_kv(text):
    return {k: v.strip() for k, v in re.findall(r"^([a-z_]+)=(.*)$", text or "", re.M)}


def parse_netstat_udp(text, pid):
    """(listening on PORT, sorted peer addresses) for one PID from `netstat -ano -p UDP`."""
    listening, peers = False, set()
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] != "UDP" or parts[-1] != str(pid):
            continue
        local, remote = parts[1], parts[2]
        if local.endswith(f":{PORT}"):
            listening = True
        if remote.endswith(f":{PORT}") and not remote.startswith("*"):
            peers.add(remote.rsplit(":", 1)[0])
    return listening, sorted(peers)


def tcp_ping(host, port=22, timeout=2.0):
    """Round trip in ms for a TCP connect, or None if it didn't connect."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.perf_counter() - start) * 1000, 1)
    except OSError:
        return None


def probe_local():
    out = {"lan_mouse": {"running": False, "pid": None, "listening": False, "peers": []}}
    if os.name != "nt":  # on the Arch laptop: the same script the Dell sends over SSH
        try:
            out.update(remote_summary(parse_kv(_run(["bash", "-s"], 15, input=REMOTE_SCRIPT).stdout)))
        except (OSError, subprocess.TimeoutExpired) as exc:
            out["error"] = f"Couldn't read local status: {exc}"
        return out
    try:
        tasks = _run(["tasklist", "/FI", "IMAGENAME eq lan-mouse.exe", "/FO", "CSV", "/NH"], 8)
        match = re.search(r'"lan-mouse\.exe","(\d+)"', tasks.stdout)
        if match:
            pid = int(match.group(1))
            listening, peers = parse_netstat_udp(_run(["netstat", "-ano", "-p", "UDP"], 8).stdout, pid)
            out["lan_mouse"] = {"running": True, "pid": pid, "listening": listening, "peers": peers}
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["error"] = f"Couldn't read local processes: {exc}"
    return out


def probe_ssh(target, identity_file=None):
    """Run the fixed status script on an SSH device. Never raises."""
    host = target.split("@", 1)[-1]
    out = {"target": target, "reachable": False, "ssh": False, "ping_ms": tcp_ping(host)}
    binary = shutil.which("ssh")
    if not binary:
        out["error"] = "OpenSSH client is not installed."
        return out
    out["reachable"] = out["ping_ms"] is not None
    if not out["reachable"]:
        out["error"] = (f"{host}:22 didn't answer. The laptop may be asleep, off Wi-Fi, "
                        "or on a new DHCP address.")
        return out
    args = [binary, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5"]
    if identity_file:
        args += ["-i", str(Path(identity_file).expanduser()), "-o", "IdentitiesOnly=yes"]
    start = time.perf_counter()
    try:
        # Bytes, not text: Windows text mode would turn the script's \n into \r\n for bash.
        result = subprocess.run([*args, target, "bash -s"], input=REMOTE_SCRIPT.encode(),
                                capture_output=True, timeout=15, shell=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["error"] = f"SSH timed out or failed to start: {exc}"
        return out
    out["ssh_ms"] = round((time.perf_counter() - start) * 1000)
    stdout = result.stdout.decode("utf-8", "replace")
    if result.returncode:
        tail = (result.stderr.decode("utf-8", "replace") or stdout).strip().splitlines()[-1:] or [""]
        out["error"] = f"SSH login failed: {tail[0][:200]}"
        return out
    kv = parse_kv(stdout)
    out["ssh"] = True
    out.update(remote_summary(kv))
    return out


def remote_summary(kv):
    def num(key):
        try:
            return float(kv.get(key) or 0)
        except ValueError:
            return 0.0
    total, avail = num("mem_total_kb"), num("mem_avail_kb")
    swap_total, swap_free = num("swap_total_kb"), num("swap_free_kb")
    unit, fallback = kv.get("lm_unit") or "unknown", kv.get("lm_fallback") or "unknown"
    return {
        "hostname": kv.get("host"), "ip": kv.get("ip") or None,
        "uptime_s": num("uptime"), "load": num("load"),
        "mem_avail_pct": round(100 * avail / total, 1) if total else None,
        "swap_used_pct": round(100 * (1 - swap_free / swap_total), 1) if swap_total else None,
        "wifi_power_save": kv.get("powersave") or None,
        "lan_mouse": {"unit": unit, "fallback": fallback,
                      "running": unit == "active" or fallback == "active",
                      "listening": num("lm_udp") > 0},
    }


def verdict(local, remote, device_ip, local_name="Dell", remote_name="Zenbook"):
    """One plain-language line per concern, worst first, for the Machines panel."""
    issues = []
    lm_local, lm_remote = local.get("lan_mouse", {}), (remote or {}).get("lan_mouse", {})
    if remote and not remote.get("reachable"):
        issues.append(("down", remote.get("error") or f"{remote_name} unreachable."))
    elif remote and not remote.get("ssh"):
        issues.append(("down", remote.get("error") or "SSH failed."))
    if not lm_local.get("running"):
        issues.append(("down", f"Lan Mouse isn't running on the {local_name}."))
    elif not lm_local.get("listening"):
        issues.append(("warn", f"{local_name} Lan Mouse isn't holding UDP {PORT}."))
    if remote and remote.get("ssh"):
        if not lm_remote.get("running"):
            issues.append(("down", f"Lan Mouse isn't running on the {remote_name}."))
        elif lm_remote.get("fallback") == "active":
            issues.append(("warn", f"{remote_name} is on the fallback (official) Lan Mouse build."))
        if remote.get("ip") and device_ip and remote["ip"] != device_ip:
            issues.append(("warn", f"{remote_name} moved to {remote['ip']} (paired as {device_ip}); "
                                   "regenerate the Lan Mouse config."))
        if lm_local.get("peers") and remote.get("ip") and remote["ip"] not in lm_local["peers"]:
            issues.append(("warn", f"{local_name} is sending to "
                                   f"{', '.join(lm_local['peers'])}, not {remote_name}'s {remote['ip']}."))
        if (remote["mem_avail_pct"] if remote.get("mem_avail_pct") is not None else 100) < 10:
            issues.append(("warn", f"{remote_name} memory is tight ({remote['mem_avail_pct']}% free); "
                                   "expect input lag."))
        if remote.get("wifi_power_save") == "on":
            issues.append(("warn", f"{remote_name} Wi-Fi power save is on; it can cause Lan Mouse lag."))
    up = ("Both Lan Mouse daemons are up and holding UDP 4242." if remote is not None
          else f"{local_name} Lan Mouse is up and holding UDP 4242.")
    if not issues:
        return "up", [up]
    if not any(level == "down" for level, _ in issues):
        # Warnings only: the link itself is fine, so say so before the caveats.
        return "warn", [up] + [text for _, text in issues]
    return "down", [text for _, text in issues]


class Prober:
    """Cached, single-flight probe so many open tabs don't SSH in parallel."""

    def __init__(self, config, max_age=55, state=None):
        self.config, self.max_age = config, max_age
        self.state = state
        self._lock, self._result = threading.Lock(), None

    def get(self, refresh=False):
        with self._lock:
            if self._result and not refresh and time.time() - self._result["checked_at"] < self.max_age:
                return self._result
            self._result = self._probe()
            return self._result

    def _probe(self):
        from .topology import enabled, identity
        if enabled(self.config):
            from .cluster import _HUBS
            here, main = identity(self.config)
            hub = _HUBS.get(str(Path(self.state).resolve())) if self.state else None
            live = {n["id"]: n for n in hub.broker.nodes()} if hub else {}
            results = {name: {"local": name == here, "reachable": name == here or live.get(name, {}).get("online", False),
                             "via": "main" if name == main else "node", **live.get(name, {})}
                       for name in self.config.get("devices", {})}
            return {"checked_at": time.time(), "devices": results,
                    "input_link": {"level": "warn", "lines": [
                        "Node reachability is reported by authenticated heartbeats. Input sharing is configured separately."]}}
        devices = self.config.get("devices", {})
        results, threads = {}, []

        def work(name, device):
            if device.get("transport") == "ssh":
                results[name] = probe_ssh(device.get("target", ""), device.get("identity_file"))
            else:
                results[name] = {"reachable": True, "ssh": None, "local": True, **probe_local()}

        for name, device in devices.items():
            thread = threading.Thread(target=work, args=(name, device), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(20)
        local_id = next((n for n, r in results.items() if r.get("local")), None)
        local = results.get(local_id, {"lan_mouse": {}})
        local_label = (devices.get(local_id, {}).get("hostname") or local_id or "Local device")
        assessments = []
        for remote_id, device in devices.items():
            if device.get("transport") != "ssh":
                continue
            remote = results.get(remote_id)
            device_ip = device.get("target", "").split("@")[-1] or None
            remote_label = device.get("hostname") or remote_id
            assessments.append(verdict(local, remote, device_ip, local_label, remote_label))
        if not assessments:
            assessments.append(verdict(local, None, None, local_label))
        level = max((item[0] for item in assessments), key={"up": 0, "warn": 1, "down": 2}.get)
        lines = list(dict.fromkeys(line for _, items in assessments for line in items))
        if len(assessments) > 1:
            lines = [line.replace("Both Lan Mouse daemons are up and holding UDP 4242.",
                                  "Lan Mouse daemons are up across configured computers and holding UDP 4242.")
                     for line in lines]
        if level == "down":
            lines = [line for line in lines if " daemons are up " not in line and " Lan Mouse is up " not in line]
        return {"checked_at": time.time(), "devices": results,
                "input_link": {"level": level, "lines": lines}}
