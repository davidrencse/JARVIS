"""PC-side cached MAIN validity (MAIN-HANDOFF.md §3.3).

A PC may act on a granted validity only while all of these hold:
- the elapsed-time source counts suspended time (Linux CLOCK_BOOTTIME, Windows
  GetTickCount64). Elsewhere there is no adapter, so nothing is cached and every check must
  go online. The Windows adapter still needs an on-device sleep check before cutover
  (blocker `windows_clock_untested`);
- the boot id is the one seen when the grant arrived (reboot = void);
- this process is the one that cached it (a new pid is a restart, not a grant);
- on Linux, CLOCK_BOOTTIME has not pulled ahead of CLOCK_MONOTONIC since the grant
  (that gap is suspend). Windows has no second clock here, so only expiry and an
  explicit invalidate apply;
- nothing else invalidated it (link loss, explicit suspend/resume, newer tuple);
- the deadline, anchored at the *request start* (not the response), has not passed.

A late or rejected response never restarts a TTL, never moves the high-water tuple,
and never drops a grant that is still valid. An older answer cannot shorten or extend
a newer one. A response for an older tuple is ignored.
"""

import math
import os
import sys
import time
from pathlib import Path

from .authority import LEASE_SECONDS

# CLOCK_BOOTTIME minus CLOCK_MONOTONIC grows only across suspend. The two reads are
# nanoseconds apart when the process is running; a larger jump is treated as sleep.
# Below this, a scheduling gap between the two reads must not force a renew.
_SUSPEND_GAP_SECONDS = 0.05

BOOT_ID = "/proc/sys/kernel/random/boot_id"
# DWORD the kernel raises on every boot. Fast-startup resumes keep it, but GetTickCount64
# also kept counting through that hibernate, so an old deadline has still passed.
WIN_BOOT_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"


def _windows_boot_id(winreg=None):
    try:
        if winreg is None:
            import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WIN_BOOT_KEY) as key:
            value, kind = winreg.QueryValueEx(key, "BootId")
    except (ImportError, OSError):
        return None
    if kind != getattr(winreg, "REG_DWORD", 4) or isinstance(value, bool) or not isinstance(value, int):
        return None
    return "win-boot-%d" % value


def boot_id(path=BOOT_ID, platform=None, winreg=None):
    if (platform or sys.platform) == "win32":
        return _windows_boot_id(winreg)
    try:
        value = Path(path).read_text().strip()
    except OSError:
        return None
    return value or None


def _windows_ticks(kernel32=None):
    """GetTickCount64 in seconds: milliseconds since boot, sleep and hibernate included."""
    try:
        if kernel32 is None:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetTickCount64.restype = ctypes.c_uint64
            kernel32.GetTickCount64.argtypes = []
        fn = kernel32.GetTickCount64
        first = fn()
    except (ImportError, OSError, AttributeError):
        return None
    if isinstance(first, bool) or not isinstance(first, int) or first <= 0:
        return None
    return lambda: fn() / 1000.0


def _clock_gettime(clock_id, platform, prefix):
    if clock_id is None or not platform.startswith(prefix):
        return None
    try:
        time.clock_gettime(clock_id)
    except OSError:
        return None
    return lambda: time.clock_gettime(clock_id)


def suspend_aware_clock(platform=None, kernel32=None):
    """Callable seconds counter that includes suspend, or None if this OS has no adapter."""
    platform = platform or sys.platform
    if platform == "win32":
        return _windows_ticks(kernel32)
    return _clock_gettime(getattr(time, "CLOCK_BOOTTIME", None), platform, "linux")


def monotonic_clock(platform=None):
    """Linux CLOCK_MONOTONIC (suspend excluded), or None. Paired with the boottime clock."""
    platform = platform or sys.platform
    return _clock_gettime(getattr(time, "CLOCK_MONOTONIC", None), platform, "linux")


def _finite(value):
    """True for a real finite number. Bool is not a timestamp or a TTL."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _tuple_int(value):
    """Non-negative int. True is an int in Python and is not an incarnation."""
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_for(value, cap):
    if not _finite(value) or not _finite(cap):
        return False
    return 0 < float(value) <= float(cap)


class CachedAuthority:
    """What this PC may still believe about the current MAIN without asking the authority."""

    def __init__(self, clock="auto", boot=boot_id, cap=LEASE_SECONDS, mono=None, pid=None):
        if clock == "auto":
            self._clock = suspend_aware_clock()
            self._mono = monotonic_clock() if mono is None else mono
        else:
            self._clock = clock
            self._mono = mono
        self._boot = boot
        self._pid = os.getpid if pid is None else pid
        self.cap = float(cap)
        self._grant = None
        self.seen = (0, 0)       # highest (incarnation, epoch) accepted this process
        self.reason = "none"

    def now(self):
        """Stamp a request start with this. None means: no cache possible, go online."""
        return self._clock() if self._clock else None

    def _process_token(self):
        try:
            token = self._pid()
        except Exception:
            return None
        if isinstance(token, bool) or not isinstance(token, int) or token <= 0:
            return None
        return token

    def _gap(self):
        """Boottime minus monotonic, or None when this cache has no suspend pair."""
        if self._clock is None or self._mono is None:
            return None
        try:
            mono = self._mono()
            boot = self._clock()
        except Exception:
            return None
        if not _finite(mono) or not _finite(boot):
            return None
        return float(boot) - float(mono)

    def _void_if_discontinuous(self):
        """Drop the grant when this process restarted or the machine suspended.

        ``seen`` stays. A restarted process must still refuse a tuple this
        process already accepted, and it must go online for a new grant.
        """
        grant = self._grant
        if grant is None:
            return
        token = self._process_token()
        if token is None or token != grant.get("pid"):
            self.invalidate("restart")
            return
        anchor = grant.get("suspend_gap")
        gap = self._gap()
        if anchor is None or gap is None or not _finite(anchor):
            return
        if gap > anchor + _SUSPEND_GAP_SECONDS:
            self.invalidate("suspend")

    def observe(self, *, holder, incarnation, epoch, valid_for, ready, request_started):
        """Record an authority answer. Returns True only if it may be cached.

        A refusal leaves ``seen`` and any still-valid grant unchanged. Restart
        and suspend are discontinuities: they drop the grant before the new
        answer is considered, then a good answer may be cached again.
        """
        if not _tuple_int(incarnation) or not _tuple_int(epoch):
            return self._refuse("bad_tuple")
        if not isinstance(ready, bool):
            return self._refuse("bad_ready")
        if not _valid_for(valid_for, self.cap):
            return self._refuse("bad_validity")
        tup = (incarnation, epoch)
        self._void_if_discontinuous()
        if tup < self.seen:
            return self._refuse("stale_tuple")
        if self._clock is None:
            return self._refuse("no_suspend_aware_clock")
        try:
            boot = self._boot()
        except Exception:
            boot = None
        if boot is None:
            return self._refuse("no_boot_id")
        if request_started is None:
            return self._refuse("no_request_start")
        if not _finite(request_started):
            return self._refuse("bad_request_start")
        try:
            now = self._clock()
        except Exception:
            return self._refuse("bad_clock")
        if not _finite(now):
            return self._refuse("bad_clock")
        if request_started > now:
            return self._refuse("request_start_in_future")
        deadline = float(request_started) + float(valid_for)
        if now >= deadline:
            return self._refuse("late_response")
        token = self._process_token()
        if token is None:
            return self._refuse("no_process_id")
        gap = self._gap()
        grant = self._grant
        if (
            grant
            and (grant["incarnation"], grant["epoch"], grant["holder"]) == (*tup, holder)
            and grant["boot"] == boot
        ):
            # Each answer is anchored at its own request start. An older answer
            # must not shorten the newer deadline or push it further out.
            if request_started < grant["request_started"]:
                self.reason = "ok"
                return True
            if request_started == grant["request_started"] and deadline > grant["deadline"]:
                deadline = grant["deadline"]
        if tup > self.seen:
            self.seen = tup
        self._grant = {
            "holder": holder,
            "incarnation": tup[0],
            "epoch": tup[1],
            "deadline": deadline,
            "boot": boot,
            "ready": ready,
            "request_started": float(request_started),
            "pid": token,
            "suspend_gap": gap,
        }
        self.reason = "ok"
        return True

    def _refuse(self, reason):
        self.reason = reason
        return False

    def invalidate(self, reason):
        """Suspend/resume, link loss, restart, authority incarnation change: forget the grant."""
        self._grant = None
        self.reason = reason

    def remaining(self, holder=None, require_ready=True):
        """Seconds the cached grant still covers new work (0 = ask the authority online)."""
        grant = self._grant
        if grant is None or self._clock is None:
            return 0.0
        self._void_if_discontinuous()
        grant = self._grant
        if grant is None:
            return 0.0
        try:
            boot = self._boot()
        except Exception:
            boot = None
        if boot != grant["boot"]:
            self.invalidate("reboot")
            return 0.0
        if holder is not None and holder != grant["holder"]:
            return 0.0
        if require_ready and not grant["ready"]:
            return 0.0
        try:
            now = self._clock()
        except Exception:
            self.invalidate("clock_regressed")
            return 0.0
        if not _finite(now) or now < grant["request_started"] or grant["deadline"] - now > self.cap:
            self.invalidate("clock_regressed")
            return 0.0
        left = grant["deadline"] - now
        if left <= 0:
            self.invalidate("expired")
            return 0.0
        return left
