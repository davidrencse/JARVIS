"""Locate (jarvis/locate.py): parsing geoclue and Windows output, the job, the main's cache.

The where-am-i samples follow geoclue 2.8's format strings ("Latitude:    %f\u00b0",
"Accuracy:    %.0f meters"; it calls setlocale, so a comma locale prints "52,520008").

Every runner here is a fake: no geoclue, PowerShell, nmcli, netsh or tailscale is started,
and no real location is ever read. Only the streaming-runner tests start a child, and that
child is this Python printing canned where-am-i lines.
"""

import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import locate

CONFIG = {"cluster": {"enabled": True, "main_device": "zenbook"},
          "devices": {"zenbook": {"label": "ASUS Zenbook"}, "dell": {"label": "Dell G15"}}}

WIFI_FIX = """Client object: /org/freedesktop/GeoClue2/Client/3

New location:
Latitude:    43.651070\u00b0
Longitude:   -79.347015\u00b0
Accuracy:    35 meters
Altitude:    76.000000 meters
Description: WiFi
Timestamp:   Sun 08 Nov 2026 09:51:03 PM EST (1794192663 seconds since the Epoch)
"""

GEOIP_THEN_WIFI = """
New location:
Latitude:    43.700000\u00b0
Longitude:   -79.400000\u00b0
Accuracy:    25000 meters

New location:
Latitude:    43.651070\u00b0
Longitude:   -79.347015\u00b0
Accuracy:    35 meters
"""

COMMA_LOCALE = """
New location:
Latitude:    52,520008\u00b0
Longitude:   13,404954\u00b0
Accuracy:    120 meters
"""

NO_ACCURACY_C_LOCALE = """
New location:
Latitude:    51.507351?
Longitude:   -0.127758?
"""

DBL_MAX = f"""
New location:
Latitude:    48.856613\u00c2\u00b0
Longitude:   2.352222\u00c2\u00b0
Accuracy:    {int(sys.float_info.max)} meters
"""

CRITICAL = "\n** (where-am-i:48213): CRITICAL **: 21:50:01.123: Failed to connect to GeoClue2 service: "
NO_AGENT = (CRITICAL + "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: "
            "'geoclue-where-am-i' disallowed, no agent for UID 1000\n")
DISABLED = CRITICAL + "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: Geolocation disabled for UID 1000\n"
REJECTED = (CRITICAL + "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: Agent rejected "
            "'geoclue-where-am-i' for user 'jdoe'. Please ensure that 'geoclue-where-am-i' has "
            "installed a valid geoclue-where-am-i.desktop file.\n")
NO_SERVICE = (CRITICAL + "GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name "
              "org.freedesktop.GeoClue2 was not provided by any .service files\n")
DBUS_TIMEOUT = CRITICAL + "Timeout was reached\n"

WIN_OK = '{"permission":"Granted","status":"Ready","lat":"43.65107","lon":"-79.347015","accuracy":"35"}'
WIN_COMMA_NAN = '{"lat":"43,65107","lon":"-79,347015","accuracy":"NaN","permission":"Granted","status":"Ready"}'
WIN_NUMBERS = '{"lat":43.65107,"lon":-79.347015,"accuracy":35.0,"permission":"Granted","status":"Ready"}'
WIN_DENIED = '{"permission":"Denied","status":"Disabled"}'
WIN_DISABLED = '{"permission":"Granted","status":"Disabled"}'
WIN_NODATA = '{"permission":"Unknown","status":"NoData"}'
WIN_SLOW = '{"permission":"Granted","status":"Initializing"}'
WIN_NO_ASSEMBLY = '{"error":"Cannot add type. The assembly \'System.Device\' could not be found."}'

NETSH = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6E AX211 160MHz
    Physical address       : a4:6b:b6:00:00:01
    State                  : connected
    SSID                   : Home WiFi 5G
    AP BSSID               : 3c:84:6a:00:00:02
    BSSID                  : 3c:84:6a:00:00:02
    Band                   : 5 GHz
    Signal                 : 92%
"""
NETSH_REFUSED = ("Network shell commands need location permission to access WLAN information. "
                 "Turn on Location services on the Location page in Privacy & security settings.\n\n"
                 "Here is the URI for the Location page in the Settings app:\n    ms-settings:privacy-location\n")


class FakeRun:
    """Answers by program name and records every call; never starts a process."""
    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def __call__(self, args, timeout, **kw):
        self.calls.append((list(args), timeout, kw))
        answer = self.answers.get(Path(args[0]).name)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return subprocess.CompletedProcess(args, 127, "", "not found")
        code, out, err = answer if isinstance(answer, tuple) else (0, answer, "")
        return subprocess.CompletedProcess(args, code, out, err)

    def called(self, name):
        return [call for call in self.calls if Path(call[0][0]).name == name]


def fake_which(*present):
    """where-am-i lives in geoclue's demo folder; everything else on a fake PATH."""
    def which(name, path=None):
        if name not in present:
            return None
        if name == "where-am-i":
            return "/usr/libexec/geoclue-2.0/demos/where-am-i" if path and "demos" in path else None
        return f"/fake/bin/{name}"
    return which


def no_real_process(*args, **kwargs):
    raise AssertionError(f"a real process was about to start: {args!r}")


class Guarded(unittest.TestCase):
    """No real runner, no real socket probe, no popups, whatever a test forgets to inject."""
    def setUp(self):
        for patcher in (mock.patch.object(locate, "_run", no_real_process),
                        mock.patch.object(locate, "_lan_ip", return_value="172.1.0.20"),
                        mock.patch.dict(os.environ, {"JARVIS_NO_NOTIFY": "1"})):
            patcher.start()
            self.addCleanup(patcher.stop)


class WhereAmIParsing(unittest.TestCase):
    def test_reads_degree_signs_and_meters(self):
        self.assertEqual(locate.parse_where_am_i(WIFI_FIX), [(43.65107, -79.347015, 35.0)])

    def test_comma_decimal_locale(self):
        self.assertEqual(locate.parse_where_am_i(COMMA_LOCALE), [(52.520008, 13.404954, 120.0)])

    def test_missing_accuracy_and_ascii_degree(self):
        self.assertEqual(locate.parse_where_am_i(NO_ACCURACY_C_LOCALE), [(51.507351, -0.127758, None)])

    def test_unknown_accuracy_and_mojibake_degree(self):
        self.assertEqual(locate.parse_where_am_i(DBL_MAX), [(48.856613, 2.352222, None)])

    def test_hemisphere_letters_and_unicode_minus(self):
        text = "Latitude: 33.8688\u00b0 S\nLongitude: 151.2093\u00b0 E\nAccuracy: 80 m\n"
        self.assertEqual(locate.parse_where_am_i(text), [(-33.8688, 151.2093, 80.0)])
        text = "Latitude: \u221233.8688 S\nLongitude: 70.5 W\n"
        self.assertEqual(locate.parse_where_am_i(text), [(-33.8688, -70.5, None)])
        text = "Latitude: 52.5 degrees\nLongitude: 13.4 degrees\n"  # the s of degrees isn't South
        self.assertEqual(locate.parse_where_am_i(text), [(52.5, 13.4, None)])

    def test_empty_value_never_swallows_the_next_line(self):
        self.assertEqual(locate.parse_where_am_i("Accuracy:\nLatitude: 1.5\nLongitude: 2.5\n"),
                         [(1.5, 2.5, None)])

    def test_impossible_or_null_island_coordinates_are_skipped(self):
        for text in ("Latitude: 123.0\nLongitude: 13.4\n", "Latitude: 0.000000\nLongitude: 0.000000\n",
                     "Latitude: 52.5\n", "nothing here", ""):
            self.assertEqual(locate.parse_where_am_i(text), [], text)

    def test_best_fix_wins_and_settled_only_for_wifi_grade(self):
        fix = locate.geoclue_answer(GEOIP_THEN_WIFI, "", 0, 15)
        self.assertEqual((fix["lat"], fix["lon"], fix["accuracy_m"], fix["source"]),
                         (43.65107, -79.347015, 35.0, "geoclue"))
        self.assertNotIn("error", fix)
        self.assertIsInstance(fix["at"], int)
        coarse = GEOIP_THEN_WIFI.split("New location:")[1]
        self.assertFalse(locate._settled("New location:" + coarse))
        self.assertTrue(locate._settled(GEOIP_THEN_WIFI))

    def test_tie_prefers_the_later_fix(self):
        text = ("New location:\nLatitude: 1.0\nLongitude: 1.0\nAccuracy: 50 meters\n"
                "New location:\nLatitude: 2.0\nLongitude: 2.0\nAccuracy: 50 meters\n")
        self.assertEqual(locate.geoclue_answer(text, "", 0, 15)["lat"], 2.0)


class GeoclueRefusals(unittest.TestCase):
    def error(self, stderr, code=255, stdout=""):
        fix = locate.geoclue_answer(stdout, stderr, code, 10)
        self.assertEqual((fix["lat"], fix["lon"], fix["source"]), (None, None, "geoclue"))
        return fix["error"]

    def test_no_agent(self):
        self.assertIn("no location agent", self.error(NO_AGENT))

    def test_disabled(self):
        self.assertEqual(self.error(DISABLED), "Location is turned off for this user in geoclue.")

    def test_agent_rejected(self):
        self.assertIn("Geoclue refused the location request", self.error(REJECTED))

    def test_service_missing(self):
        self.assertIn("isn't installed", self.error(NO_SERVICE))

    def test_other_critical_is_shown_plainly(self):
        error = self.error(DBUS_TIMEOUT)
        self.assertEqual(error, "Geoclue couldn't locate this PC: Failed to connect to GeoClue2 "
                                "service: Timeout was reached")

    def test_silence(self):
        self.assertEqual(self.error("", code=0), "Geoclue found no location within 10 s (is Wi-Fi on?).")
        self.assertEqual(self.error("", code=3), "where-am-i failed (exit 3).")


class WindowsWatcher(unittest.TestCase):
    def test_ok_from_strings_numbers_and_comma_locale(self):
        for text in (WIN_OK, WIN_NUMBERS):
            fix = locate.watcher_answer(text, 15)
            self.assertEqual((fix["lat"], fix["lon"], fix["accuracy_m"], fix["source"]),
                             (43.65107, -79.347015, 35.0, "windows"))
            self.assertNotIn("error", fix)
        fix = locate.watcher_answer(WIN_COMMA_NAN, 15)
        self.assertEqual((fix["lat"], fix["lon"], fix["accuracy_m"]), (43.65107, -79.347015, None))

    def test_byte_order_mark_and_warnings_before_the_json(self):
        fix = locate.watcher_answer("\ufeffWARNING: slow provider\r\n" + WIN_OK + "\r\n", 15)
        self.assertEqual(fix["lat"], 43.65107)

    def test_permission_denied_disabled_and_no_data_point_at_settings(self):
        for text in (WIN_DENIED, WIN_DISABLED, WIN_NODATA):
            fix = locate.watcher_answer(text, 15)
            self.assertEqual(fix["error"], locate.PRIVACY, text)
            self.assertIsNone(fix["lat"])

    def test_timeout_and_missing_assembly_and_garbage(self):
        self.assertEqual(locate.watcher_answer(WIN_SLOW, 12)["error"], "Windows found no location within 12 s.")
        self.assertIn("System.Device didn't load", locate.watcher_answer(WIN_NO_ASSEMBLY, 15)["error"])
        self.assertEqual(locate.watcher_answer("Add-Type : boom", 15)["error"],
                         "Windows didn't answer with a location.")
        self.assertEqual(locate.watcher_answer('{"error":"Access is denied."}', 15)["error"], locate.PRIVACY)

    def test_script_stays_free_of_double_quotes(self):
        self.assertNotIn('"', locate.WATCHER)
        self.assertNotIn('"', locate.BALLOON)


class Validate(unittest.TestCase):
    def test_accepts_empty_and_5_to_15(self):
        for payload in ({}, {"timeout": 5}, {"timeout": 15}):
            locate.validate(payload)

    def test_refuses_everything_else(self):
        for payload in (None, [], {"timeout": 4}, {"timeout": 16}, {"timeout": True}, {"timeout": "10"},
                        {"timeout": 10.0}, {"timeout": 10, "cmd": "x"}, {"device": "dell"}):
            with self.assertRaises(ValueError, msg=repr(payload)):
                locate.validate(payload)


class RunHereLinux(Guarded):
    def test_fix_and_network_from_fakes(self):
        run = FakeRun(**{"where-am-i": WIFI_FIX, "nmcli": "no:Neighbour\nyes:Ren\\:Home\n",
                         "tailscale": "100.64.0.11\n"})
        with mock.patch.dict(os.environ, {"LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"}):
            answer = locate.run_here({"timeout": 10}, CONFIG, None, system="linux", run=run,
                                     which=fake_which("where-am-i", "nmcli", "tailscale"))
        fix = answer["fix"]
        self.assertEqual((fix["lat"], fix["lon"], fix["accuracy_m"], fix["source"]),
                         (43.65107, -79.347015, 35.0, "geoclue"))
        self.assertEqual(answer["network"], {"ssid": "Ren:Home", "lan_ip": "172.1.0.20",
                                             "tailscale_ip": "100.64.0.11"})
        [(args, timeout, kw)] = run.called("where-am-i")
        self.assertEqual(args, ["/usr/libexec/geoclue-2.0/demos/where-am-i", "-t", "10", "-a", "8"])
        self.assertEqual(timeout, 13)
        self.assertIs(kw["enough"], locate._settled)
        env = kw["env"]
        self.assertEqual((env["LC_NUMERIC"], env["LC_MESSAGES"], env["LANG"]), ("C", "C", "de_DE.UTF-8"))
        self.assertNotIn("LC_ALL", env)
        self.assertNotIn("LANGUAGE", env)
        [(nmcli, _, _)] = run.called("nmcli")
        self.assertIn("--rescan", nmcli)

    def test_default_timeout_is_fifteen(self):
        run = FakeRun(**{"where-am-i": WIFI_FIX})
        locate.run_here({}, system="linux", run=run, which=fake_which("where-am-i"))
        self.assertEqual(run.called("where-am-i")[0][0][2], "15")

    def test_no_where_am_i_says_how_to_get_it(self):
        run = FakeRun()
        answer = locate.run_here({}, system="linux", run=run, which=fake_which())
        self.assertIn("where-am-i", answer["fix"]["error"])
        self.assertEqual(answer["network"], {"lan_ip": "172.1.0.20"})
        self.assertEqual(run.calls, [])

    def test_refusal_and_runner_failures_become_errors(self):
        which = fake_which("where-am-i")
        answer = locate.run_here({}, system="linux", run=FakeRun(**{"where-am-i": (255, "", NO_AGENT)}),
                                 which=which)
        self.assertIn("no location agent", answer["fix"]["error"])
        answer = locate.run_here({}, system="linux", which=which,
                                 run=FakeRun(**{"where-am-i": subprocess.TimeoutExpired("where-am-i", 18)}))
        self.assertEqual(answer["fix"]["error"], "Geoclue found no location within 15 s.")
        answer = locate.run_here({}, system="linux", which=which,
                                 run=FakeRun(**{"where-am-i": PermissionError(13, "Permission denied")}))
        self.assertIn("where-am-i couldn't start", answer["fix"]["error"])

    def test_broken_network_readers_never_break_the_fix(self):
        run = FakeRun(**{"where-am-i": WIFI_FIX, "nmcli": OSError("boom"),
                         "tailscale": (1, "", "Tailscale is stopped.")})
        with mock.patch.object(locate, "_lan_ip", side_effect=OSError("no route")):
            answer = locate.run_here({}, system="linux", run=run,
                                     which=fake_which("where-am-i", "nmcli", "tailscale"))
        self.assertEqual(answer["network"], {})
        self.assertEqual(answer["fix"]["lat"], 43.65107)

    def test_unsupported_os(self):
        answer = locate.run_here({}, system="macos", run=FakeRun(), which=fake_which())
        self.assertIn("isn't supported", answer["fix"]["error"])
        self.assertIsNone(answer["fix"]["lat"])

    def test_bad_payload_refused_before_anything_runs(self):
        run = FakeRun()
        with self.assertRaises(ValueError):
            locate.run_here({"timeout": 60}, system="linux", run=run, which=fake_which("where-am-i"))
        self.assertEqual(run.calls, [])

    def test_tells_the_screen_first(self):
        run = FakeRun(**{"where-am-i": WIFI_FIX, "notify-send": ""})
        os.environ.pop("JARVIS_NO_NOTIFY")  # the setUp patch restores it
        locate.run_here({}, system="linux", run=run, which=fake_which("where-am-i", "notify-send"))
        self.assertEqual(Path(run.calls[0][0][0]).name, "notify-send")
        self.assertEqual(run.calls[0][0][1:], ["-a", "JARVIS", "JARVIS", locate.NOTE])


class RunHereWindows(Guarded):
    def test_watcher_netsh_and_tailscale(self):
        run = FakeRun(**{"powershell.exe": WIN_OK, "netsh": NETSH, "tailscale": "100.101.1.2\n"})
        answer = locate.run_here({"timeout": 12}, system="windows", run=run,
                                 which=fake_which("powershell.exe", "netsh", "tailscale"))
        self.assertEqual(answer["fix"]["source"], "windows")
        self.assertEqual(answer["network"], {"ssid": "Home WiFi 5G", "lan_ip": "172.1.0.20",
                                             "tailscale_ip": "100.101.1.2"})
        [(args, timeout, _)] = run.called("powershell.exe")
        self.assertEqual(args[:4], ["/fake/bin/powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("FromSeconds(12)", args[4])
        self.assertIn("AddSeconds(12)", args[4])
        self.assertEqual(timeout, 18)

    def test_netsh_refusal_leaves_no_ssid(self):
        run = FakeRun(**{"powershell.exe": WIN_DENIED, "netsh": (1, NETSH_REFUSED, "")})
        answer = locate.run_here({}, system="windows", run=run, which=fake_which("powershell.exe", "netsh"))
        self.assertEqual(answer["fix"]["error"], locate.PRIVACY)
        self.assertEqual(answer["network"], {"lan_ip": "172.1.0.20"})

    def test_powershell_hangs_or_is_missing(self):
        run = FakeRun(**{"powershell.exe": subprocess.TimeoutExpired("powershell.exe", 21)})
        answer = locate.run_here({}, system="windows", run=run, which=fake_which("powershell.exe"))
        self.assertEqual(answer["fix"]["error"], "Windows found no location within 15 s.")
        run = FakeRun(**{"powershell.exe": FileNotFoundError(2, "No such file")})
        answer = locate.run_here({}, system="windows", run=run, which=fake_which())
        self.assertIn("PowerShell couldn't start", answer["fix"]["error"])
        self.assertEqual(run.called("powershell.exe")[0][0][0], "powershell.exe")


class NetworkParsing(unittest.TestCase):
    def test_nmcli_terse_escapes(self):
        self.assertEqual(locate.nmcli_ssid("no:Other\nyes:Caf\u00e9\\:Net\\\\2\n"), "Caf\u00e9:Net\\2")
        self.assertIsNone(locate.nmcli_ssid("no:Other\n"))
        self.assertIsNone(locate.nmcli_ssid(""))

    def test_netsh_skips_bssid_and_blank(self):
        self.assertEqual(locate.netsh_ssid(NETSH), "Home WiFi 5G")
        self.assertIsNone(locate.netsh_ssid("    SSID                   : \n    BSSID : aa\n"))
        self.assertIsNone(locate.netsh_ssid(NETSH_REFUSED))

    def test_tailscale_only_tailnet_addresses(self):
        run = FakeRun(tailscale="192.168.1.4\nnot-an-ip\n100.64.0.11\n")
        self.assertEqual(locate._tailscale_ip("linux", run, fake_which("tailscale")), "100.64.0.11")
        self.assertIsNone(locate._tailscale_ip("linux", FakeRun(tailscale="10.0.0.1\n"), fake_which("tailscale")))
        self.assertIsNone(locate._tailscale_ip("linux", FakeRun(), fake_which()))

    def test_lan_ip_skips_tailnet_and_loopback(self):
        class Probe:
            address = "172.1.0.20"
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def connect(self, target): pass
            def getsockname(self): return (Probe.address, 40000)
        with mock.patch.object(locate.socket, "socket", Probe):
            self.assertEqual(locate._lan_ip(), "172.1.0.20")
            for address in ("100.64.0.11", "127.0.0.1", "169.254.3.4"):
                Probe.address = address
                self.assertIsNone(locate._lan_ip(), address)


class Notify(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("JARVIS_NO_NOTIFY", None)

    def test_off_switch(self):
        os.environ["JARVIS_NO_NOTIFY"] = "1"
        run, spawn = FakeRun(), mock.Mock()
        self.assertFalse(locate.notify("hi", system="linux", run=run, which=fake_which("notify-send")))
        self.assertFalse(locate.notify("hi", system="windows", spawn=spawn, which=fake_which()))
        self.assertEqual(run.calls, [])
        spawn.assert_not_called()

    def test_linux_notify_send(self):
        run = FakeRun(**{"notify-send": ""})
        self.assertTrue(locate.notify("Camera on\x1b[31m\u202e", system="linux", run=run,
                                      which=fake_which("notify-send")))
        [(args, timeout, _)] = run.calls
        self.assertEqual(args, ["/fake/bin/notify-send", "-a", "JARVIS", "JARVIS", "Camera on [31m"])
        self.assertEqual(timeout, 3)
        self.assertFalse(locate.notify("x", system="linux", run=run, which=fake_which()))

    def test_windows_balloon_is_detached_hidden_and_never_spliced(self):
        spawn = mock.Mock()
        text = "Location requested'; Remove-Item C:\\ -Recurse; '"
        self.assertTrue(locate.notify(text, system="windows", spawn=spawn, which=fake_which()))
        args, kw = spawn.call_args
        self.assertEqual(args[0][:5], ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden"])
        self.assertEqual(args[0][-1], locate.BALLOON)
        self.assertNotIn("Remove-Item", " ".join(args[0]))
        self.assertEqual(kw["env"]["JARVIS_NOTE"], text)
        self.assertEqual(kw["creationflags"] & locate.NO_WINDOW, locate.NO_WINDOW)
        self.assertIs(kw["stdout"], subprocess.DEVNULL)

    def test_never_raises(self):
        run = FakeRun(**{"notify-send": OSError("dbus gone")})
        self.assertFalse(locate.notify("x", system="linux", run=run, which=fake_which("notify-send")))
        spawn = mock.Mock(side_effect=OSError("no powershell"))
        self.assertFalse(locate.notify("x", system="windows", spawn=spawn, which=fake_which()))


class FakeHub:
    """Matches Hub.dispatch(node, kind, payload, timeout=45); records instead of sending."""
    def __init__(self, state, answer=None, error=None):
        self.config, self.state, self.calls = CONFIG, Path(state), []
        self.answer, self.error = answer, error

    def dispatch(self, node, kind, payload, timeout=45):
        self.calls.append((node, kind, dict(payload), timeout))
        if self.error:
            raise self.error
        return self.answer


GOOD_NODE = {"fix": {"lat": 43.65107, "lon": -79.347015, "accuracy_m": 35.0, "source": "windows",
                     "at": 1794192663},
             "network": {"ssid": "Home WiFi 5G", "lan_ip": "172.1.0.30", "tailscale_ip": "100.101.1.2"}}


class Request(Guarded):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_matches_the_real_hub_dispatch_signature(self):
        from jarvis.cluster import Hub
        params = inspect.signature(Hub.dispatch).parameters
        self.assertEqual(list(params)[:5], ["self", "node", "kind", "payload", "timeout"])

    def test_node_goes_through_dispatch_with_ten_seconds_spare(self):
        hub = FakeHub(self.state, GOOD_NODE)
        answer = locate.request(hub, "dell")
        self.assertEqual(hub.calls, [("dell", "locate", {"timeout": 15}, 25)])
        self.assertEqual(answer, {"device": "dell", "fix": GOOD_NODE["fix"], "network": GOOD_NODE["network"]})
        locate.request(hub, "dell", timeout=5)
        self.assertEqual(hub.calls[-1], ("dell", "locate", {"timeout": 5}, 15))

    def test_good_fix_is_cached_owner_only_and_atomically(self):
        locate.request(FakeHub(self.state, GOOD_NODE), "dell")
        path = self.state / "locations.json"
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["locations.json"])
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["dell"]["lat"], 43.65107)
        self.assertEqual(locate.last(self.state), {"dell": {**GOOD_NODE["fix"], "source": "cache"}})

    def test_main_answers_in_process_never_dispatches(self):
        hub = FakeHub(self.state, error=AssertionError("dispatch on the main"))
        run = FakeRun(**{"where-am-i": WIFI_FIX})
        answer = locate.request(hub, "zenbook", 10, system="linux", run=run, which=fake_which("where-am-i"))
        self.assertEqual(hub.calls, [])
        self.assertEqual((answer["device"], answer["fix"]["source"], answer["fix"]["lat"]),
                         ("zenbook", "geoclue", 43.65107))
        self.assertEqual(run.called("where-am-i")[0][0][2], "10")
        self.assertEqual(locate.last(self.state)["zenbook"]["source"], "cache")

    def test_offline_node_without_a_kept_fix(self):
        hub = FakeHub(self.state, error=ConnectionError("Node dell is offline. Start jarvis cluster node on that PC."))
        answer = locate.request(hub, "dell")
        fix = answer["fix"]
        self.assertEqual((fix["lat"], fix["lon"], fix["source"]), (None, None, "cache"))
        self.assertEqual(fix["error"], "Node dell is offline. Start jarvis cluster node on that PC.")
        self.assertEqual(answer["network"], {})
        self.assertFalse((self.state / "locations.json").exists())

    def test_failed_look_falls_back_to_the_kept_fix_with_the_reason(self):
        locate.request(FakeHub(self.state, GOOD_NODE), "dell")
        for hub in (FakeHub(self.state, error=TimeoutError("no confirmed result")),
                    FakeHub(self.state, {"fix": {"lat": None, "lon": None, "source": "windows",
                                                 "at": 1794199999, "error": locate.PRIVACY},
                                         "network": {"lan_ip": "172.1.0.30"}})):
            answer = locate.request(hub, "dell")
            fix = answer["fix"]
            self.assertEqual((fix["lat"], fix["source"], fix["at"]), (43.65107, "cache", 1794192663))
            self.assertIn(fix["error"], ("no confirmed result", locate.PRIVACY))
        self.assertEqual(answer["network"], {"lan_ip": "172.1.0.30"})
        self.assertEqual(json.loads((self.state / "locations.json").read_text())["dell"]["at"], 1794192663)

    def test_node_answer_is_sanitised(self):
        junk = {"fix": {"lat": "43.65107", "lon": -79.347015, "accuracy_m": float("inf"),
                        "source": "<script>", "at": True, "extra": "x"},
                "network": {"ssid": "Home\x1b]0;pwn\x07\u202eNet", "lan_ip": "999.1.1.1",
                            "tailscale_ip": "100.101.1.2", "near": True, "cmd": "rm -rf"}}
        answer = locate.request(FakeHub(self.state, junk), "dell")
        self.assertEqual(answer["fix"]["source"], "cache")
        self.assertEqual((answer["fix"]["lat"], answer["fix"]["accuracy_m"]), (43.65107, None))
        self.assertAlmostEqual(answer["fix"]["at"], time.time(), delta=5)  # no stamp: as of now
        self.assertNotIn("extra", answer["fix"])
        self.assertEqual(answer["network"], {"ssid": "Home ]0;pwn Net", "tailscale_ip": "100.101.1.2"})
        for bad in (None, "text", [], {"fix": "x"}, {"fix": {"lat": 91, "lon": 0}}):
            answer = locate.request(FakeHub(self.state, bad), "dell")
            self.assertTrue(answer["fix"]["error"])  # shown over the kept fix ...
            self.assertEqual(answer["fix"]["source"], "cache")
        self.assertEqual(locate.last(self.state)["dell"]["lat"], 43.65107)  # ... never replacing it

    def test_bad_input_raises_before_anything_runs(self):
        hub = FakeHub(self.state, GOOD_NODE)
        for device, timeout in (("laptop9", 15), (None, 15), ("dell", 30), ("dell", 0)):
            with self.assertRaises(ValueError):
                locate.request(hub, device, timeout)
        self.assertEqual(hub.calls, [])

    def test_cache_survives_junk_and_parallel_writers(self):
        path = self.state / "locations.json"
        path.write_text("{not json")
        self.assertEqual(locate.last(self.state), {})
        fix = GOOD_NODE["fix"]
        threads = [threading.Thread(target=locate._remember, args=(self.state, f"pc{i}", fix)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(locate.last(self.state)), sorted(f"pc{i}" for i in range(16)))
        saved = json.loads(path.read_text())
        saved.update({"../etc": fix, "bad": {"lat": 200, "lon": 1}, "worse": "x"})
        path.write_text(json.dumps(saved))
        self.assertEqual(len(locate.last(self.state)), 16)


class StreamingRunner(unittest.TestCase):
    """The real _run, driven by a Python child that prints canned where-am-i lines."""
    def child(self, lines, then_sleep):
        script = ("import sys, time\n"
                  f"for line in {lines!r}:\n    print(line, flush=True)\n"
                  f"time.sleep({then_sleep})\n")
        return [sys.executable, "-u", "-c", script]

    def test_stops_once_a_wifi_grade_fix_is_in(self):
        started = time.monotonic()
        out = locate._run(self.child(["New location:", "Latitude: 1.5", "Longitude: 2.5",
                                      "Accuracy: 40 meters"], 30), 20, enough=locate._settled)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(locate.parse_where_am_i(out.stdout), [(1.5, 2.5, 40.0)])

    def test_out_of_time_returns_what_was_read(self):
        started = time.monotonic()
        out = locate._run(self.child(["New location:", "Latitude: 1.5", "Longitude: 2.5",
                                      "Accuracy: 25000 meters"], 30), 1, enough=locate._settled)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(locate.parse_where_am_i(out.stdout), [(1.5, 2.5, 25000.0)])

    def test_child_that_exits_on_its_own(self):
        out = locate._run(self.child(["no fix"], 0), 10, enough=locate._settled)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "no fix"))
        out = locate._run(self.child(["plain"], 0), 10)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "plain"))


class Boundaries(unittest.TestCase):
    def test_no_http_client_in_locate(self):
        source = Path(locate.__file__).read_text(encoding="utf-8")
        for needle in ("urllib", "http.client", "import requests", "beacondb.net/v1"):
            self.assertNotIn(needle, source)

    def test_brain_gets_no_locate_action(self):
        from jarvis import remote
        self.assertNotIn("locate", remote.ACTIONS)


if __name__ == "__main__":
    unittest.main()
