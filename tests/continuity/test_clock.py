"""PC-side cached MAIN validity: failures that must make a PC go back online."""

import unittest

from jarvis.continuity import clock as C


class Fake:
    def __init__(self):
        self.t = 100.0
        self.boot = "boot-a"

    def now(self):
        return self.t


class Cached(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        self.c = C.CachedAuthority(clock=self.f.now, boot=lambda: self.f.boot)

    def grant(self, started=None, valid_for=15, inc=1, epoch=3, holder="dell", ready=True):
        started = self.f.t if started is None else started
        return self.c.observe(holder=holder, incarnation=inc, epoch=epoch, valid_for=valid_for, ready=ready,
                              request_started=started)

    def test_anchor_is_request_start_not_response(self):
        started = self.c.now()
        self.f.t += 4  # slow response
        self.assertTrue(self.grant(started=started))
        self.assertAlmostEqual(self.c.remaining("dell"), 11)

    def test_late_response_is_not_cached(self):
        started = self.c.now()
        self.f.t += 16
        self.assertFalse(self.grant(started=started))
        self.assertEqual(self.c.reason, "late_response")
        self.assertEqual(self.c.remaining(), 0)

    def test_older_tuple_ignored_newer_tuple_replaces(self):
        self.assertTrue(self.grant(epoch=3))
        self.assertFalse(self.grant(epoch=2))
        self.assertEqual(self.c.reason, "stale_tuple")
        self.assertTrue(self.grant(epoch=4, holder="zenbook"))
        self.assertEqual(self.c.remaining("dell"), 0)
        self.assertGreater(self.c.remaining("zenbook"), 0)

    def test_new_authority_incarnation_beats_higher_epoch(self):
        self.assertTrue(self.grant(inc=1, epoch=9))
        self.assertTrue(self.grant(inc=2, epoch=0))
        self.assertFalse(self.grant(inc=1, epoch=10))

    def test_late_old_renewal_does_not_shorten_newer_one(self):
        early = self.c.now()
        self.f.t += 3
        self.assertTrue(self.grant())             # renewal anchored at t=103
        self.assertTrue(self.grant(started=early))  # older request answered late
        self.assertAlmostEqual(self.c.remaining("dell"), 15)

    def test_validity_capped(self):
        self.assertFalse(self.grant(valid_for=3600))
        self.assertFalse(self.grant(valid_for=0))
        self.assertFalse(self.grant(valid_for=True))

    def test_expiry(self):
        self.grant()
        self.f.t += 15
        self.assertEqual(self.c.remaining("dell"), 0)
        self.assertEqual(self.c.reason, "expired")

    def test_reboot_voids(self):
        self.grant()
        self.f.boot = "boot-b"
        self.assertEqual(self.c.remaining("dell"), 0)
        self.assertEqual(self.c.reason, "reboot")

    def test_explicit_invalidation_on_suspend_or_link_loss(self):
        self.grant()
        self.c.invalidate("resumed")
        self.assertEqual(self.c.remaining("dell"), 0)

    def test_not_ready_grant_admits_nothing(self):
        self.grant(ready=False)
        self.assertEqual(self.c.remaining("dell"), 0)
        self.assertGreater(self.c.remaining("dell", require_ready=False), 0)

    def test_no_suspend_aware_clock_means_no_cache(self):
        c = C.CachedAuthority(clock=None, boot=lambda: "b")
        self.assertIsNone(c.now())
        self.assertFalse(c.observe(holder="dell", incarnation=1, epoch=1, valid_for=15, ready=True,
                                   request_started=None))
        self.assertEqual(c.reason, "no_suspend_aware_clock")

    def test_missing_boot_id_means_no_cache(self):
        c = C.CachedAuthority(clock=self.f.now, boot=lambda: None)
        self.assertFalse(c.observe(holder="dell", incarnation=1, epoch=1, valid_for=15, ready=True,
                                   request_started=self.f.t))

    def test_macos_has_no_suspend_aware_clock(self):
        self.assertIsNone(C.suspend_aware_clock("darwin"))


class FakeKernel32:
    def __init__(self, ms=5_000):
        self.ms = ms

    def GetTickCount64(self):
        return self.ms


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_LOCAL_MACHINE = "HKLM"
    REG_DWORD = 4
    REG_SZ = 1

    def __init__(self, value=17, kind=4, fail=False):
        self.value, self.kind, self.fail, self.opened = value, kind, fail, None

    def OpenKey(self, root, path):
        if self.fail:
            raise OSError("no key")
        self.opened = (root, path)
        return FakeKey()

    def QueryValueEx(self, key, name):
        assert name == "BootId"
        return self.value, self.kind


class Windows(unittest.TestCase):
    def test_tick_count_is_seconds_and_counts_sleep(self):
        k = FakeKernel32(5_000)
        clock = C.suspend_aware_clock("win32", kernel32=k)
        self.assertEqual(clock(), 5.0)
        k.ms += 3_600_000  # an hour asleep still advances GetTickCount64
        self.assertEqual(clock(), 3605.0)

    def test_broken_or_zero_tick_count_means_no_clock(self):
        self.assertIsNone(C.suspend_aware_clock("win32", kernel32=FakeKernel32(0)))
        self.assertIsNone(C.suspend_aware_clock("win32", kernel32=object()))

    def test_boot_id_from_registry(self):
        reg = FakeWinreg(17)
        self.assertEqual(C.boot_id(platform="win32", winreg=reg), "win-boot-17")
        self.assertEqual(reg.opened, ("HKLM", C.WIN_BOOT_KEY))

    def test_unreadable_or_wrong_type_boot_id_means_none(self):
        self.assertIsNone(C.boot_id(platform="win32", winreg=FakeWinreg(fail=True)))
        self.assertIsNone(C.boot_id(platform="win32", winreg=FakeWinreg("17", kind=1)))

    def test_cache_voids_on_windows_reboot(self):
        k, reg = FakeKernel32(10_000), FakeWinreg(17)
        c = C.CachedAuthority(clock=C.suspend_aware_clock("win32", kernel32=k),
                              boot=lambda: C.boot_id(platform="win32", winreg=reg))
        self.assertTrue(c.observe(holder="zenbook", incarnation=1, epoch=1, valid_for=15, ready=True,
                                  request_started=c.now()))
        self.assertGreater(c.remaining("zenbook"), 0)
        reg.value = 18
        self.assertEqual(c.remaining("zenbook"), 0)
        self.assertEqual(c.reason, "reboot")

    def test_sleep_past_deadline_expires_grant(self):
        k = FakeKernel32(10_000)
        c = C.CachedAuthority(clock=C.suspend_aware_clock("win32", kernel32=k), boot=lambda: "b")
        c.observe(holder="zenbook", incarnation=1, epoch=1, valid_for=15, ready=True, request_started=c.now())
        k.ms += 20_000
        self.assertEqual(c.remaining("zenbook"), 0)
        self.assertEqual(c.reason, "expired")


if __name__ == "__main__":
    unittest.main()
