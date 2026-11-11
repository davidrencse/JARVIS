"""PC-side cached MAIN validity (MAIN-HANDOFF.md §3.3).

A PC may act on a granted validity only while all of these hold:
- the elapsed-time source counts suspended time (Linux CLOCK_BOOTTIME). Elsewhere there is
  no tested adapter yet, so nothing is cached and every check must go online;
- the boot id is the one seen when the grant arrived (reboot = void);
- nothing invalidated it (suspend/resume, link loss, process restart, newer tuple seen);
- the deadline, anchored at the *request start* (not the response), has not passed.

A late response never restarts a TTL; a response for an older tuple is ignored.
"""

import sys
import time
from pathlib import Path

from .authority import LEASE_SECONDS

BOOT_ID = "/proc/sys/kernel/random/boot_id"


def boot_id(path=BOOT_ID):
    try:
        value = Path(path).read_text().strip()
    except OSError:
        return None
    return value or None


def suspend_aware_clock(platform=None):
    """Callable seconds counter that includes suspend, or None if none is tested here."""
    platform = platform or sys.platform
    cid = getattr(time, "CLOCK_BOOTTIME", None)
    if cid is None or not platform.startswith("linux"):
        return None
    try:
        time.clock_gettime(cid)
    except OSError:
        return None
    return lambda: time.clock_gettime(cid)


class CachedAuthority:
    """What this PC may still believe about the current MAIN without asking the authority."""

    def __init__(self, clock="auto", boot=boot_id, cap=LEASE_SECONDS):
        self._clock = suspend_aware_clock() if clock == "auto" else clock
        self._boot = boot
        self.cap = float(cap)
        self._grant = None
        self.seen = (0, 0)       # highest (incarnation, epoch) ever observed this process
        self.reason = "none"

    def now(self):
        """Stamp a request start with this. None means: no cache possible, go online."""
        return self._clock() if self._clock else None

    def observe(self, *, holder, incarnation, epoch, valid_for, ready, request_started):
        """Record an authority answer. Returns True only if it may be cached."""
        tup = (int(incarnation), int(epoch))
        if tup < self.seen:
            return self._refuse("stale_tuple")
        if tup > self.seen:
            self.seen = tup
            if self._grant and (self._grant["incarnation"], self._grant["epoch"]) != tup:
                self.invalidate("newer_tuple")
        if self._clock is None:
            return self._refuse("no_suspend_aware_clock")
        boot = self._boot()
        if boot is None:
            return self._refuse("no_boot_id")
        if request_started is None:
            return self._refuse("no_request_start")
        now = self._clock()
        if isinstance(valid_for, bool) or not isinstance(valid_for, (int, float)) or not 0 < valid_for <= self.cap:
            return self._refuse("bad_validity")
        if request_started > now:
            return self._refuse("request_start_in_future")
        deadline = request_started + float(valid_for)
        if now >= deadline:
            return self._refuse("late_response")
        g = self._grant
        if g and (g["incarnation"], g["epoch"], g["holder"]) == (*tup, holder) and g["boot"] == boot:
            # Each answer is conservative on its own (anchored at its request start); an older
            # answer arriving late must not shorten a newer renewal, nor extend past it.
            deadline = max(deadline, g["deadline"])
        self._grant = {"holder": holder, "incarnation": tup[0], "epoch": tup[1], "deadline": deadline,
                       "boot": boot, "ready": bool(ready)}
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
        g = self._grant
        if g is None or self._clock is None:
            return 0.0
        if self._boot() != g["boot"]:
            self.invalidate("reboot")
            return 0.0
        if holder is not None and holder != g["holder"]:
            return 0.0
        if require_ready and not g["ready"]:
            return 0.0
        left = g["deadline"] - self._clock()
        if left <= 0:
            self.invalidate("expired")
            return 0.0
        return left
