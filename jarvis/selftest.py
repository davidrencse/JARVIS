"""`jarvis selftest keyboard`: live checks that the Dell keyboard works with Lan Mouse
and the JARVIS overlay running. Every check reports PASS / WARN / FAIL / SKIP.

Default checks are passive or type only into a throwaway test window:
  lan-mouse process, build has the keyboard fix, watchdog, capture-queue log,
  peer reachability, overlay hotkey + no synthetic keys, Dell typing through
  Lan Mouse's keyboard hook (round-trip latency).
--capture  briefly hands the cursor to the ASUS (mouse moves only) and checks capture
           starts, injected keys stay on the Dell while captured, and capture releases.
--stress   while captured, floods the capture queue with mouse motion and checks the
           hook survives (still captures and releases afterwards).

Injected keys are flagged by Windows and the fixed build passes them through, so the
retry path for *physical* keys under a full queue is covered by Lan Mouse's Rust unit
tests (jarvis_key_retry_tests), not here.
"""

import ctypes
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .platforms import NO_WINDOW, on_windows

FIX_MARKER = b"keyboard event dropped: capture queue stayed full"
ACK_MARKER = b"did not acknowledge Enter within"  # stuck-capture guard
GUARD_RELEASED = r"did not acknowledge Enter"  # its log line (also "...: no connection within")
LM_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "lan-mouse"
LOG_TS = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")


def result(name, flag, detail):
    return {"check": name, "flag": flag, "detail": detail}


# -- Windows input plumbing -----------------------------------------------------------
if on_windows():
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    def _user32():
        u = ctypes.windll.user32
        u.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        u.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        u.BringWindowToTop.argtypes = (ctypes.c_void_p,)
        u.GetParent.restype = ctypes.c_void_p
        u.GetParent.argtypes = (ctypes.c_void_p,)
        return u

    def send_key(vk, up=False):
        u = _user32()
        i = INPUT(type=1)
        i.u.ki = KEYBDINPUT(vk, u.MapVirtualKeyW(vk, 0), 0x0002 if up else 0, 0, 0)
        u.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))

    def move_mouse(dx, dy):
        u = _user32()
        i = INPUT(type=0)
        i.u.mi = MOUSEINPUT(dx, dy, 0, 0x0001, 0, 0)  # MOUSEEVENTF_MOVE, relative
        u.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))

    def focus(hwnd):
        """Same approach as the overlay: join the foreground input queue to switch."""
        u, me = _user32(), ctypes.windll.kernel32.GetCurrentThreadId()
        for _ in range(10):
            if (u.GetForegroundWindow() or 0) == hwnd:
                return True
            fg = u.GetForegroundWindow()
            other = u.GetWindowThreadProcessId(fg, None) if fg else 0
            joined = bool(other and other != me and u.AttachThreadInput(me, other, True))
            try:
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
            finally:
                if joined:
                    u.AttachThreadInput(me, other, False)
            time.sleep(0.05)
        return (u.GetForegroundWindow() or 0) == hwnd


class TypingProbe:
    """A small throwaway Tk window; injected keystrokes must arrive in its text box."""

    def __init__(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.title("JARVIS keyboard self-test (closes itself)")
        self.root.attributes("-topmost", True)
        self.root.geometry("420x80+200+200")
        self.entry = tk.Entry(self.root, font=("Consolas", 14))
        self.entry.pack(fill="x", padx=10, pady=20)
        self.arrivals = []
        self.entry.bind("<KeyPress>", lambda e: self.arrivals.append(time.perf_counter()))
        self.pump(0.3)
        self.hwnd = _user32().GetParent(self.entry.winfo_id()) or self.root.winfo_id()
        self.hwnd = _user32().GetParent(self.hwnd) or self.hwnd  # Tk's top-level frame

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.005)

    def focused(self):
        ok = focus(self.hwnd)
        self.entry.focus_force()
        self.pump(0.2)
        return ok

    def type(self, text):
        """Type lowercase letters/digits/spaces; returns (received_text, latencies_ms)."""
        self.entry.delete(0, "end")
        self.arrivals.clear()
        sent = []
        for ch in text:
            vk = ord(ch.upper()) if ch.isalnum() else 0x20
            sent.append(time.perf_counter())
            send_key(vk)
            send_key(vk, up=True)
            self.pump(0.03)
        self.pump(0.3)
        lat = [(a - s) * 1000 for s, a in zip(sent, self.arrivals, strict=False)]
        return self.entry.get(), lat

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass


# -- checks ---------------------------------------------------------------------------
def _processes(pattern):
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath,CommandLine"
         " | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        creationflags=NO_WINDOW)
    import json
    rows = json.loads(out.stdout or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    return [r for r in rows if re.search(pattern, f"{r.get('Name')} {r.get('CommandLine')}", re.I)]


def check_lan_mouse():
    procs = [p for p in _processes(r"lan-mouse") if (p.get("Name") or "").lower() == "lan-mouse.exe"]
    if not procs:
        return [result("lan-mouse running", "FAIL", "not running; start it with the "
                       "'JARVIS Lan Mouse.vbs' startup launcher")], None
    out = [result("lan-mouse running", "PASS" if len(procs) == 1 else "WARN",
                  f"{len(procs)} process(es): pid {', '.join(str(p['ProcessId']) for p in procs)}")]
    path = procs[0].get("ExecutablePath") or ""
    patched = str(LM_DIR / "bin" / "lan-mouse.exe").lower()
    out.append(result("lan-mouse build", "PASS" if path.lower() == patched else "WARN",
                      path + ("" if path.lower() == patched else
                              "  (watchdog fell back to the official build: keyboard fix absent)")))
    try:
        binary = Path(path).read_bytes()  # one read; both markers are looked for in it
        has_fix = FIX_MARKER in binary
    except OSError as exc:
        has_fix = None
        out.append(result("keyboard fix in binary", "WARN", f"couldn't read binary: {exc}"))
    if has_fix is not None:
        out.append(result("keyboard fix in binary", "PASS" if has_fix else "FAIL",
                          "contains the no-drop key retry" if has_fix else "old build: keys can be dropped"))
        guard = ACK_MARKER in binary
        out.append(result("stuck-capture guard in binary", "PASS" if guard else "FAIL",
                          "input returns to the Dell if the ASUS doesn't acknowledge in 2.5 s" if guard
                          else "old build: an unanswered hand-over locks the Dell keyboard"))
    try:
        prio = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                               f"(Get-Process -Id {procs[0]['ProcessId']}).PriorityClass"],
                              capture_output=True, text=True, timeout=20,
                              creationflags=NO_WINDOW).stdout.strip()
        out.append(result("lan-mouse priority", "PASS" if prio == "High" else "WARN", prio or "unknown"))
    except Exception as exc:
        out.append(result("lan-mouse priority", "WARN", str(exc)))
    watchdog = _processes(r"watchdog\.ps1")
    out.append(result("lan-mouse watchdog", "PASS" if watchdog else "WARN",
                      "restarts lan-mouse if it exits" if watchdog else
                      "not running: a crash would leave input sharing off until next login"))
    return out, procs[0]


def log_lines(since=None):
    try:
        lines = (LM_DIR / "daemon.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if since is None:
        return lines
    keep, current = [], None
    for line in lines:
        m = LOG_TS.match(line)
        if m:
            current = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if current and current >= since:
            keep.append(line)
    return keep


def check_queue_log(lines):
    dropped = sum("keyboard event dropped" in l for l in lines)
    full = sum("no available capacity" in l for l in lines)
    if dropped:
        return result("no dropped keys (log)", "FAIL", f"{dropped} keyboard event(s) dropped")
    if full:
        return result("no dropped keys (log)", "WARN",
                      f"0 keys dropped; {full} mouse-motion event(s) dropped under load")
    return result("no dropped keys (log)", "PASS", "no capture-queue overflow logged")


def check_peer(config):
    # The first other computer reached over SSH, whatever it is called or runs.
    peer = next((d for d in (config.get("devices") or {}).values()
                 if d.get("transport") == "ssh" and d.get("target")), {})
    target = peer.get("target", "")
    host = target.split("@")[-1] if target else ""
    name = peer.get("label", "peer").split(",")[0]
    if not host:
        return result("peer reachable", "SKIP", "no SSH peer configured")
    # Windows ping: -n count, -w milliseconds; elsewhere: -c count, -W seconds.
    flags = ["-n", "1", "-w", "1500"] if on_windows() else ["-c", "1", "-W", "2"]
    ok = subprocess.run(["ping", *flags, host], capture_output=True,
                        creationflags=NO_WINDOW).returncode == 0
    return result(f"{name} reachable", "PASS" if ok else "WARN",
                  f"{host} {'answers ping' if ok else 'no ping reply (asleep? IP changed?)'}")


def check_overlay(base):
    src = Path(base) / "jarvis" / "overlay.py"
    out = []
    try:
        text = src.read_text(encoding="utf-8")
        uses = re.search(r"\.(keybd_event|SendInput)\(", text)
        out.append(result("overlay sends no fake keys", "FAIL" if uses else "PASS",
                          "found a synthetic key call in overlay.py" if uses else
                          "focus via input-queue join; hides if Windows refuses focus"))
    except OSError:
        out.append(result("overlay sends no fake keys", "SKIP", "overlay.py not found"))
    running = _processes(r"-m jarvis.*overlay")
    u = ctypes.windll.user32
    free = u.RegisterHotKey(None, 0x5A11, 0x0001 | 0x4000, ord("1"))  # Alt+1, NOREPEAT
    if free:
        u.UnregisterHotKey(None, 0x5A11)
    out.append(result("overlay hotkey", "PASS" if (running and not free) else "WARN",
                      "Alt+1 registered by the overlay" if (running and not free) else
                      ("overlay not running" if not running else "Alt+1 not registered")))
    return out


def check_typing(probe, label="Dell typing (not captured)"):
    if not probe.focused():
        return result(label, "SKIP", "Windows wouldn't focus the test window; click it and rerun")
    text = "jarvis keyboard 123"
    got, lat = probe.type(text)
    if got != text:
        return result(label, "FAIL", f"typed {text!r}, window received {got!r}")
    if not lat:
        return result(label, "WARN", f"all {len(text)} keys arrived, but none were timed")
    worst = max(lat)
    flag = "PASS" if worst < 30 else "WARN" if worst < 100 else "FAIL"
    return result(label, flag, f"all {len(text)} keys arrived; latency median "
                  f"{sorted(lat)[len(lat) // 2]:.1f} ms, worst {worst:.1f} ms")


def wait_log(pattern, since, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if any(re.search(pattern, l) for l in log_lines(since)):
            return True
        time.sleep(0.1)
    return False


def guard_released(since):
    """Whether the stuck-capture guard gave the input back since `since`."""
    return any(re.search(GUARD_RELEASED, l) for l in log_lines(since))


def _captured_checks(probe, stress, out):
    """What runs while the cursor is on the ASUS; check_capture hands it back afterwards
    even when this raises."""
    refocused = probe.focused()
    if not refocused:
        # never type into a window we couldn't bring to the front
        got, owner = None, _user32().GetForegroundWindow() or 0
    else:
        got, _ = probe.type("abc")
        owner = _user32().GetForegroundWindow() or 0
    if not refocused:
        out.append(result("injected keys stay on Dell while captured", "WARN",
                          "skipped typing: the test window could not take focus"))
    elif got == "abc":
        out.append(result("injected keys stay on Dell while captured", "PASS",
                          "software keystrokes reached the Dell window"))
    elif owner != probe.hwnd:
        out.append(result("injected keys stay on Dell while captured", "WARN",
                          f"inconclusive: the test window lost focus (foreground {owner:#x}); "
                          "something else was clicked or popped up mid-test"))
    else:
        out.append(result("injected keys stay on Dell while captured", "FAIL",
                          f"window kept focus but received {got!r}; injected keys were swallowed"))
    if stress:
        # A fast gaming mouse: 1000 reports/s for 2 s, as tiny jiggles that barely move
        # the ASUS cursor. (An unpaced 8,500/s flood starved the link; no real mouse does that.)
        t0, n = time.perf_counter(), 2000
        for i in range(n):
            move_mouse(1 if i % 2 else -1, 0)
            while time.perf_counter() < t0 + (i + 1) / 1000:
                pass
        took = time.perf_counter() - t0
        out.append(result("stress: 1000 Hz mouse for 2 s", "PASS",
                          f"{n} moves in {took:.2f} s without the hook stalling"))


def check_capture(probe, stress=False):
    out = []
    start = datetime.now(timezone.utc).replace(microsecond=0)
    probe.focused()
    # Start from the middle of the screen: a cursor already resting on the edge creates
    # no movement across it, so no hand-over would begin.
    u = ctypes.windll.user32
    u.SetCursorPos(u.GetSystemMetrics(0) // 2, u.GetSystemMetrics(1) // 2)
    probe.pump(0.2)
    for _ in range(60):  # push left past the Dell's left edge, toward the ASUS
        move_mouse(-80, 0)
        probe.pump(0.01)
    if not wait_log(r"entering client 0", start, 3):
        return [result("capture starts at left edge", "FAIL",
                       "no 'entering client' logged; is the ASUS connected?")]
    out.append(result("capture starts at left edge", "PASS", "cursor handed to the ASUS"))
    answered = wait_log(r"acknowledged the connection|did not acknowledge Enter", start, 3)
    # An Ack that lands after the guard already released is not an answer (older builds
    # still log "acknowledged the connection" for it); the timeout line comes first.
    timed_out = guard_released(start)
    if not answered or timed_out:
        # The ASUS didn't answer the hand-over: the guard must give the input back.
        back = timed_out or wait_log(GUARD_RELEASED, start, 4)
        out.append(result("unanswered hand-over returns input", "PASS" if back else "FAIL",
                          "ASUS didn't acknowledge; the guard gave the Dell its keyboard back"
                          if back else "Dell still captured: press Ctrl+Shift+Win+Alt"))
        out.append(check_typing(probe, "Dell typing after unanswered hand-over"))
        return out
    out.append(result("ASUS acknowledges hand-over", "PASS", "peer answered"))
    try:
        _captured_checks(probe, stress, out)
    finally:  # always hand the cursor back, whatever happened above
        release_from = datetime.now(timezone.utc).replace(microsecond=0)
        for _ in range(80):  # move right on the ASUS until it crosses back to the Dell
            move_mouse(80, 0)
            probe.pump(0.01)
    released = wait_log(r"releasing capture", release_from, 4)
    out.append(result("capture releases back to Dell", "PASS" if released else "FAIL",
                      "cursor returned" if released else
                      "still captured: press Ctrl+Shift+Win+Alt to get the Dell keyboard back"))
    if released:
        out.append(check_typing(probe, "Dell typing after capture"))
    if stress:
        out.append(check_queue_log(log_lines(start)))
        alive = any((p.get("Name") or "").lower() == "lan-mouse.exe" for p in _processes("lan-mouse"))
        out.append(result("stress: lan-mouse survived", "PASS" if alive else "FAIL",
                          "still running" if alive else "process exited during the test"))
    return out


def keyboard(config, base, capture=False, stress=False):
    if not on_windows():
        return [result("platform", "SKIP", "Windows-only self-test")]
    results, proc = check_lan_mouse()
    results.append(check_queue_log(log_lines()))
    results.append(check_peer(config))
    results += check_overlay(base)
    probe = TypingProbe()
    try:
        results.append(check_typing(probe))
        if capture or stress:
            if proc is None:
                results.append(result("capture tests", "SKIP", "lan-mouse isn't running"))
            else:
                results += check_capture(probe, stress)
    finally:
        probe.close()
    return results


def render(results):
    marks = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "skip"}
    width = max(len(r["check"]) for r in results)
    lines = [f"[{marks[r['flag']]}] {r['check']:<{width}}  {r['detail']}" for r in results]
    counts = {f: sum(r["flag"] == f for r in results) for f in marks}
    lines.append(f"\n{counts['PASS']} pass, {counts['WARN']} warn, {counts['FAIL']} fail, "
                 f"{counts['SKIP']} skipped")
    return "\n".join(lines)
