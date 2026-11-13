"""Session-id migration and phone-cookie rotation, through the real HTTP handler.

Contract these tests pin (the server may not have landed it yet):

- GET /phone/api/session/frame reads the session id from X-JARVIS-Session
  (32 hex). `after` stays in the query. The legacy `?session=` query is used
  only when that header is absent. A header that is not 32 hex is
  404 {"error": "No such session."} and does not fall through to the query.
  When both are present, the header wins.
- phone.ROTATE_AFTER is 7 * 24 * 3600 and phone.ROTATE_GRACE is 600.
  phone_for rotates on a non-frame route when
  now - (phone.get("rotated") or phone["created"]) >= ROTATE_AFTER.
  The new secret is stored as token_sha256, the old hash moves to
  prev_token_sha256 with prev_until = now + ROTATE_GRACE, and rotated = now.
  The response carries one Set-Cookie from phone.set_cookie (the __Host-
  cookie: Path=/, Secure, HttpOnly, SameSite=Strict, no Domain).
- The previous cookie still authenticates that same stable id and login while
  now < prev_until, and it never rotates again. At prev_until it is
  401 {pair: true}. A frame request never rotates. A row with no `rotated`
  (and maybe no csrf_sha256) keeps working and rotates from `created`.
  Pairing stamps rotated at the pairing clock. Logout and revoke drop both
  hashes. CSRF is not rotated.

The clock is the server's injectable clock. Nothing here patches time.sleep
or threading.Event.wait.
"""

import json
import secrets
import threading
import unittest

from jarvis import phone
from tests.mobile.test_phone import LOGIN, PHONE_A, PHONE_B, WHOIS, PhoneBase

WEEK = 7 * 24 * 3600
GRACE = 600


def _digest(token):
    return phone._digest(token)


class RotationWindows(unittest.TestCase):
    def test_rotate_after_is_seven_days_and_grace_is_ten_minutes(self):
        self.assertTrue(hasattr(phone, "ROTATE_AFTER"), "phone.ROTATE_AFTER")
        self.assertTrue(hasattr(phone, "ROTATE_GRACE"), "phone.ROTATE_GRACE")
        self.assertEqual(phone.ROTATE_AFTER, WEEK)
        self.assertEqual(phone.ROTATE_GRACE, GRACE)


class PhoneSessionRotation(PhoneBase):
    def _windows(self):
        """The contract's windows. A missing constant still lets the scenario run
        against those numbers; the name itself is asserted above."""
        after = getattr(phone, "ROTATE_AFTER", WEEK)
        grace = getattr(phone, "ROTATE_GRACE", GRACE)
        self.assertEqual(after, WEEK)
        self.assertEqual(grace, GRACE)
        return after, grace

    def _set_cookies(self, res):
        return list(res.headers.get_all("Set-Cookie") or [])

    def _cookie_token(self, header):
        """The rotation Set-Cookie: __Host- prefix, the set_cookie attributes, no Domain."""
        token = header.split(";", 1)[0].split("=", 1)[1]
        self.assertEqual(header, phone.set_cookie(token))
        self.assertTrue(header.startswith("__Host-jarvis_phone="), header)
        attrs = [part.strip() for part in header.split(";")[1:]]
        self.assertEqual(attrs, ["Path=/", "Secure", "HttpOnly", "SameSite=Strict",
                                 f"Max-Age={phone.COOKIE_AGE}"])
        self.assertIn("Secure", attrs)
        self.assertIn("HttpOnly", attrs)
        self.assertIn("SameSite=Strict", attrs)
        self.assertFalse(any(part.lower().startswith("domain") for part in attrs), header)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{16,128}$")
        return token

    def _row(self, pid):
        found = [p for p in self.server.store.load()["phones"] if p.get("id") == pid]
        self.assertEqual(len(found), 1, pid)
        return found[0]

    def _anchor(self, pid):
        row = self._row(pid)
        return float(row.get("rotated") or row["created"])

    def _plant(self, token, *, created, csrf=None, rotated=None, pid="oldrow01"):
        """A phone.json row from before this change: hash only, no plaintext."""
        node = WHOIS[PHONE_A]
        row = {
            "id": pid, "name": "Legacy", "token_sha256": _digest(token),
            "stable_id": node["Node"]["StableID"], "node": "iphone-1", "os": "iOS",
            "login": node["UserProfile"]["LoginName"], "ua": "legacy",
            "created": created, "last_seen": created,
        }
        if csrf is not None:
            row["csrf_sha256"] = _digest(csrf)
        if rotated is not None:
            row["rotated"] = rotated

        def change(data):
            data["phones"] = [p for p in data["phones"] if p.get("id") != pid]
            data["phones"].append(row)

        self.server.store.update(change)
        self.assertNotIn(token, (self.state / phone.STORE).read_text(encoding="utf-8"))
        if csrf is not None:
            self._csrf[token] = csrf
        return pid

    def _open(self, token):
        status, view, res = self.request(
            "POST", "/phone/api/session/start",
            body={"device": "dell", "kind": "screen", "quality": "low"},
            cookie=token, csrf=token in self._csrf)
        self.assertEqual(status, 200, view)
        sid = view["id"]
        self.assertRegex(sid, r"^[0-9a-f]{32}$")
        self.assertEqual(self._set_cookies(res), [])
        return sid

    def _frame(self, token, *, header=None, query_sid=None, after=0):
        parts = [f"after={after}"]
        if query_sid is not None:
            parts.append("session=" + query_sid)
        headers = None if header is None else {"X-JARVIS-Session": header}
        return self.request("GET", "/phone/api/session/frame?" + "&".join(parts),
                            cookie=token, headers=headers)

    def _watch_frames(self):
        seen = []
        real = self.hub.session_frame

        def wrapped(sid, after, opener):
            seen.append((sid, after, opener))
            return real(sid, after, opener)

        self.hub.session_frame = wrapped
        return seen

    def _set_login(self, pid, login):
        def change(data):
            for item in data["phones"]:
                if item["id"] == pid:
                    item["login"] = login

        self.server.store.update(change)

    def _quiet_seen(self, pid):
        now = float(self.clock.t)

        def change(data):
            for item in data["phones"]:
                if item["id"] == pid:
                    item["last_seen"] = now

        self.server.store.update(change)

    def _expect_rotation(self, token, pid):
        """One non-frame request at the due clock. Returns the new cookie secret."""
        _after, grace = self._windows()
        before = self._row(pid)
        self.logged.clear()
        status, body, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 200, body)
        cookies = self._set_cookies(res)
        self.assertEqual(len(cookies), 1, cookies)
        new = self._cookie_token(cookies[0])
        self.assertNotEqual(new, token)
        row = self._row(pid)
        self.assertEqual(row["token_sha256"], _digest(new))
        self.assertEqual(row["prev_token_sha256"], _digest(token))
        self.assertEqual(float(row["rotated"]), float(self.clock.t))
        self.assertEqual(float(row["prev_until"]), float(self.clock.t) + grace)
        self.assertEqual(row.get("created"), before.get("created"))
        if before.get("csrf_sha256"):
            self.assertEqual(row["csrf_sha256"], before["csrf_sha256"])
        else:
            self.assertNotIn("csrf_sha256", row)
        stored = json.dumps(row)
        self.assertNotIn(token, stored)
        self.assertNotIn(new, stored)
        # The new cookie works, and the old one is now prev: it authenticates
        # and must not mint a second cookie.
        status, body, res = self.request("GET", "/phone/api/me", cookie=new)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._set_cookies(res), [])
        status, body, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._set_cookies(res), [])
        again = self._row(pid)
        self.assertEqual(again["token_sha256"], _digest(new))
        self.assertEqual(again["prev_token_sha256"], _digest(token))
        self.assertEqual(again["rotated"], row["rotated"])
        self.assertEqual(again["prev_until"], row["prev_until"])
        logged = "\n".join(self.logged)
        self.assertNotIn(token, logged)
        self.assertNotIn(new, logged)
        return new

    def _due(self, pid):
        after, _grace = self._windows()
        self.clock.t = self._anchor(pid) + after

    def test_pairing_stamps_rotated_at_the_pairing_clock(self):
        token, pid = self.pair()
        row = self._row(pid)
        self.assertEqual(float(row["created"]), float(self.clock.t))
        self.assertIn("rotated", row)
        self.assertEqual(float(row["rotated"]), float(row["created"]))
        self.assertNotIn("prev_token_sha256", row)
        self.assertEqual(row["token_sha256"], _digest(token))
        self.assertNotIn(token, json.dumps(row))

    def test_no_rotation_one_second_before_and_rotation_at_rotate_after(self):
        after, _grace = self._windows()
        token, pid = self.pair()
        anchor = self._anchor(pid)
        self.clock.t = anchor + after - 1
        status, body, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._set_cookies(res), [])
        row = self._row(pid)
        self.assertEqual(row["token_sha256"], _digest(token))
        self.assertNotIn("prev_token_sha256", row)
        self.clock.t = anchor + after
        self._expect_rotation(token, pid)

    def test_rotated_stamp_wins_over_an_older_created(self):
        after, _grace = self._windows()
        token, pid = self.pair()
        stamped = float(self.clock.t)

        def split(data):
            for item in data["phones"]:
                if item["id"] == pid:
                    item["created"] = stamped - after - 50
                    item["rotated"] = stamped

        self.server.store.update(split)
        self.clock.t = stamped + after - 1
        status, body, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._set_cookies(res), [])
        self.assertNotIn("prev_token_sha256", self._row(pid))
        self.clock.t = stamped + after
        self._expect_rotation(token, pid)

    def test_old_row_keeps_working_and_rotates_from_created(self):
        token = secrets.token_urlsafe(32)
        created = float(self.clock.t)
        pid = self._plant(token, created=created)
        status, body, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["phone"]["id"], pid)
        self.assertEqual(body["phone"]["name"], "Legacy")
        self.assertEqual(self._set_cookies(res), [])
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "hi"},
                                       cookie=token, csrf=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.console.submitted), 1)
        row = self._row(pid)
        self.assertNotIn("rotated", row)
        self.assertNotIn("csrf_sha256", row)
        self.assertNotIn("prev_token_sha256", row)
        after, _grace = self._windows()
        self.clock.t = created + after - 1
        sid = self._open(token)
        self.assertEqual(self._row(pid)["token_sha256"], _digest(token))
        self.assertNotIn("prev_token_sha256", self._row(pid))
        self.clock.t = created + after
        status, body, res = self._frame(token, query_sid=sid, after=0)
        self.assertEqual((status, body), (200, b"\xff\xd8jpeg"))
        self.assertEqual(self._set_cookies(res), [])
        self.assertEqual(self._row(pid)["token_sha256"], _digest(token))
        self.assertNotIn("prev_token_sha256", self._row(pid))
        new = self._expect_rotation(token, pid)
        self.assertNotIn("csrf_sha256", self._row(pid))
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "still here"},
                                       cookie=new, csrf=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.console.submitted), 2)

    def test_previous_cookie_works_until_prev_until_then_asks_to_pair(self):
        _after, grace = self._windows()
        token, pid = self.pair()
        self._due(pid)
        new = self._expect_rotation(token, pid)
        status, body, res = self.request("GET", "/phone/api/me", cookie=token, ip=PHONE_B)
        self.assertEqual(status, 401, body)
        self.assertIs(body.get("pair"), True)
        self.assertEqual(self._set_cookies(res), [])
        self.assertEqual(self._row(pid)["token_sha256"], _digest(new))
        self.assertEqual(self._row(pid)["prev_token_sha256"], _digest(token))
        # The stored login still binds both hashes. identify() passes (the header
        # matches whois); phone_for must still reject the row.
        self._set_login(pid, "other@example.com")
        for cookie in (token, new):
            status, body, res = self.request("GET", "/phone/api/me", cookie=cookie)
            self.assertEqual(status, 401, cookie)
            self.assertIs(body.get("pair"), True)
            self.assertEqual(self._set_cookies(res), [])
        self._set_login(pid, LOGIN)
        self.assertEqual(self._row(pid)["token_sha256"], _digest(new))
        self.assertEqual(self._row(pid)["prev_token_sha256"], _digest(token))
        # Accepted through the last second of the grace window.
        rotated_at = float(self._row(pid)["rotated"])
        self.assertEqual(float(self._row(pid)["prev_until"]), rotated_at + grace)
        self.clock.t = rotated_at + grace - 1
        status, body, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._set_cookies(res), [])
        # prev_until itself is refused: the old cookie is replayed after grace.
        self.clock.t = rotated_at + grace
        status, body, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 401, body)
        self.assertIs(body.get("pair"), True)
        self.assertEqual(self._set_cookies(res), [])
        status, body, res = self.request("GET", "/phone/api/me", cookie=new)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._set_cookies(res), [])
        self.assertEqual(self._row(pid)["token_sha256"], _digest(new))
        self.clock.t = rotated_at + grace + 30
        status, body, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 401, body)
        self.assertIs(body.get("pair"), True)
        self.assertEqual(self._set_cookies(res), [])
        status, body, res = self.request("GET", "/phone/api/me", cookie=new)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._set_cookies(res), [])

    def test_two_concurrent_old_cookies_rotate_once(self):
        _after, grace = self._windows()
        for _round in range(2):
            token, pid = self.pair()
            self._due(pid)
            self._quiet_seen(pid)
            barrier = threading.Barrier(2)
            outcomes, errors = [], []
            lock = threading.Lock()

            def hit(token=token, barrier=barrier, outcomes=outcomes, errors=errors, lock=lock):
                try:
                    barrier.wait(5)
                    result = self.request("GET", "/phone/api/me", cookie=token)
                except Exception as exc:  # noqa: BLE001 — report the thread's failure on the test
                    with lock:
                        errors.append(exc)
                else:
                    with lock:
                        outcomes.append(result)

            threads = [threading.Thread(target=hit, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            self.assertEqual(errors, [], _round)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(outcomes), 2)
            cookies = []
            for status, body, res in outcomes:
                self.assertEqual(status, 200, body)
                cookies.extend(self._set_cookies(res))
            self.assertEqual(len(cookies), 1, cookies)
            new = self._cookie_token(cookies[0])
            row = self._row(pid)
            self.assertEqual(row["prev_token_sha256"], _digest(token))
            self.assertEqual(row["token_sha256"], _digest(new))
            self.assertEqual(float(row["prev_until"]), float(self.clock.t) + grace)
            self.assertEqual(float(row["rotated"]), float(self.clock.t))
            status, body, res = self.request("GET", "/phone/api/me", cookie=token)
            self.assertEqual(status, 200, body)
            self.assertEqual(self._set_cookies(res), [])
            status, body, res = self.request("GET", "/phone/api/me", cookie=new)
            self.assertEqual(status, 200, body)
            self.assertEqual(self._set_cookies(res), [])
            self.assertEqual(self._row(pid)["token_sha256"], _digest(new))

    def test_frame_route_does_not_rotate_when_the_token_is_due(self):
        token, pid = self.pair()
        sid = self._open(token)
        digest = self._row(pid)["token_sha256"]
        self._due(pid)
        seen = self._watch_frames()
        status, body, res = self._frame(token, query_sid=sid, after=1)
        self.assertEqual((status, body), (200, b"\xff\xd8jpeg"))
        self.assertEqual(res.getheader("X-Frame-Seq"), "3")
        self.assertEqual(self._set_cookies(res), [])
        self.assertEqual(seen, [(sid, 1, f"phone:{pid}")])
        row = self._row(pid)
        self.assertEqual(row["token_sha256"], digest)
        self.assertNotIn("prev_token_sha256", row)
        self._expect_rotation(token, pid)

    def test_frame_header_supplies_the_session_and_after_stays_in_the_query(self):
        token, pid = self.pair()
        sid = self._open(token)
        seen = self._watch_frames()
        status, body, res = self._frame(token, header=sid, after=4)
        self.assertEqual((status, body), (200, b"\xff\xd8jpeg"))
        self.assertEqual(res.getheader("X-Frame-Seq"), "3")
        self.assertEqual(seen, [(sid, 4, f"phone:{pid}")])
        self.assertEqual(self._set_cookies(res), [])

    def test_frame_header_wins_over_a_different_query_session(self):
        token, pid = self.pair()
        first = self._open(token)
        second = self._open(token)
        seen = self._watch_frames()
        status, body, res = self._frame(token, header=first, query_sid=second, after=5)
        self.assertEqual((status, body), (200, b"\xff\xd8jpeg"))
        self.assertEqual(res.getheader("X-Frame-Seq"), "3")
        self.assertEqual(seen, [(first, 5, f"phone:{pid}")])

    def test_frame_legacy_query_works_when_the_header_is_absent(self):
        token, pid = self.pair()
        sid = self._open(token)
        seen = self._watch_frames()
        status, body, res = self._frame(token, query_sid=sid, after=2)
        self.assertEqual((status, body), (200, b"\xff\xd8jpeg"))
        self.assertEqual(res.getheader("X-Frame-Seq"), "3")
        self.assertEqual(seen, [(sid, 2, f"phone:{pid}")])
        self.assertEqual(self._set_cookies(res), [])

    def test_non_hex_or_other_session_header_is_404_and_ignores_the_query(self):
        token, _pid = self.pair()
        sid = self._open(token)
        seen = self._watch_frames()
        others = (
            "nope",
            "g" * 32,
            "0" * 31,
            "0" * 33,
            "0" * 32,
            "",
        )
        for header in others:
            with self.subTest(header=header):
                start = len(seen)
                status, body, _res = self._frame(token, header=header, query_sid=sid, after=0)
                self.assertEqual((status, body), (404, {"error": "No such session."}))
                fetched = [item[0] for item in seen[start:]]
                self.assertNotIn(sid, fetched, header)
                self.assertTrue(all(item == header for item in fetched), fetched)

    def test_rotated_cookie_is_host_strict_and_has_no_domain(self):
        token, pid = self.pair()
        self._due(pid)
        new = self._expect_rotation(token, pid)
        self.assertNotIn(new, (self.state / phone.STORE).read_text(encoding="utf-8"))
        self.assertTrue(phone.COOKIE.startswith("__Host-"))

    def test_session_id_never_appears_in_the_log(self):
        token, _pid = self.pair()
        self.logged.clear()
        sid = self._open(token)
        self.assertRegex(sid, r"^[0-9a-f]{32}$")
        self._frame(token, query_sid=sid, after=0)
        self._frame(token, header=sid, after=0)
        self._frame(token, header="nope", query_sid=sid, after=0)

        def explode(session_id, _after, _opener):
            raise RuntimeError("frame failed " + session_id)

        self.hub.session_frame = explode
        self._frame(token, query_sid=sid, after=0)
        self._frame(token, header=sid, query_sid=sid, after=3)
        for line in self.logged:
            self.assertNotIn(sid, str(line), line)

    def test_csrf_is_still_required_and_is_not_rotated(self):
        token, pid = self.pair()
        csrf = self._csrf[token]
        digest = self._row(pid)["csrf_sha256"]
        self.assertNotIn(csrf, (self.state / phone.STORE).read_text(encoding="utf-8"))
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "hi"},
                                       cookie=token, csrf=False)
        self.assertEqual((status, body.get("code")), (403, "csrf"))
        self.assertEqual(self.console.submitted, [])
        self._due(pid)
        new = self._expect_rotation(token, pid)
        self.assertEqual(self._row(pid)["csrf_sha256"], digest)
        self.assertNotIn(csrf, (self.state / phone.STORE).read_text(encoding="utf-8"))
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "read the calendar"},
                                       cookie=new, csrf=False)
        self.assertEqual((status, body.get("code")), (403, "csrf"))
        self.assertEqual(self.console.submitted, [])
        self._csrf[new] = csrf
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "what time is it"},
                                       cookie=new)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.console.submitted), 1)
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "old"},
                                       cookie=token, csrf=False)
        self.assertEqual((status, body.get("code")), (403, "csrf"))
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "and the old cookie"},
                                       cookie=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._row(pid)["csrf_sha256"], digest)
        self.assertEqual(len(self.console.submitted), 2)

    def test_logout_after_rotation_invalidates_the_old_and_new_cookies(self):
        token, pid = self.pair()
        self._due(pid)
        new = self._expect_rotation(token, pid)
        self._csrf[new] = self._csrf[token]
        status, body, res = self.request("POST", "/phone/api/logout", body={}, cookie=new)
        self.assertEqual((status, body.get("ok")), (200, True))
        cleared = self._set_cookies(res)
        self.assertTrue(cleared, cleared)
        self.assertTrue(all("Max-Age=0" in item and "Domain" not in item for item in cleared), cleared)
        self.assertEqual(self.server.store.load()["phones"], [])
        for cookie in (token, new):
            status, body, again = self.request("GET", "/phone/api/me", cookie=cookie)
            self.assertEqual(status, 401, cookie)
            self.assertIs(body.get("pair"), True)
            self.assertEqual(self._set_cookies(again), [])

    def test_revoke_after_rotation_invalidates_the_old_and_new_cookies(self):
        token, pid = self.pair()
        self._due(pid)
        new = self._expect_rotation(token, pid)
        self.assertEqual([item["id"] for item in self.server.store.revoke(pid)], [pid])
        self.server.sync_phones()
        self.assertEqual(self.server.store.load()["phones"], [])
        for cookie in (token, new):
            status, body, _ = self.request("GET", "/phone/api/me", cookie=cookie)
            self.assertEqual(status, 401, cookie)
            self.assertIs(body.get("pair"), True)
        self.assertNotIn(token, "\n".join(self.logged))
        self.assertNotIn(new, "\n".join(self.logged))
