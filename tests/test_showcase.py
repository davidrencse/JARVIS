import unittest
from unittest.mock import patch

from jarvis import remote, showcase
from jarvis.showcase import SHOW_MARKET, answer, market_line, open_page


class FakeRemote:
    def __init__(self, ok=True, here="dell"):
        self.calls, self.ok, self._here = [], ok, here

    def names(self):
        return ["dell", "zenbook"]

    def here(self):
        return self._here

    def resolve(self, name):
        return (name or "").lower() or None

    def call(self, device, action, arg):
        self.calls.append((device, action, arg))
        return self.ok, "Opened the Pro terminal." if self.ok else "the tunnel from dell is down"


class FakeMarket:
    def intel(self):
        return {"cap_weighted_24h": -2.31, "breadth": {"up": 3, "down": 37, "n": 40},
                "gainers": [{"symbol": "HYPE", "chg_24h": 4.2}],
                "losers": [{"symbol": "NEAR", "chg_24h": -10.22}]}


class Broken:
    def intel(self):
        raise ConnectionError("no key")


class ShowMarketTest(unittest.TestCase):
    def test_matches_market_requests(self):
        for text in ("Jarvis, check the market", "check the markets", "show me the crypto market",
                     "how's the market doing today", "pull up the pro terminal",
                     "Jarvis, open the market terminal on the Dell laptop"):
            self.assertTrue(SHOW_MARKET.match(text), text)

    def test_leaves_other_requests_alone(self):
        for text in ("open terminal", "check my email", "check the market cap of btc",
                     "what's the market for used cars like"):
            self.assertFalse(SHOW_MARKET.match(text), text)

    def test_names_the_laptop(self):
        self.assertEqual(SHOW_MARKET.match("pull up the markets on the dell").group("device"), "dell")


class AnswerTest(unittest.TestCase):
    def test_opens_terminal_here_and_reads_the_market(self):
        fake = FakeRemote()
        reply = answer("Jarvis, check the market", {}, "/tmp", remote=fake, market=FakeMarket())
        self.assertEqual(fake.calls, [(None, "open_page", "terminal")])
        self.assertIn("down 2.3 percent", reply)
        self.assertIn("3 of 40", reply)
        self.assertTrue(reply.endswith("Pro terminal is up."))

    def test_opens_on_the_named_laptop(self):
        fake = FakeRemote(here="zenbook")
        reply = answer("check the market on the dell", {}, "/tmp", remote=fake, market=Broken())
        self.assertEqual(fake.calls, [("dell", "open_page", "terminal")])
        self.assertEqual(reply, "Pro terminal is up on the Dell.")

    def test_unknown_laptop_is_said_not_tried(self):
        fake = FakeRemote()
        reply = answer("check the market on the toaster", {}, "/tmp", remote=fake, market=Broken())
        self.assertEqual(fake.calls, [])
        self.assertIn("toaster", reply)

    def test_failure_is_spoken(self):
        reply = answer("check the market", {}, "/tmp", remote=FakeRemote(ok=False), market=Broken())
        self.assertIn("couldn't open the Pro terminal", reply)
        self.assertIn("tunnel", reply)

    def test_not_a_market_request(self):
        self.assertIsNone(answer("what time is it", {}, "/tmp", remote=FakeRemote()))

    def test_market_line_without_data(self):
        self.assertIsNone(market_line(Broken()))


class OpenPageTest(unittest.TestCase):
    def test_uses_a_running_console(self):
        with patch.object(showcase, "console_port", return_value=7790), \
                patch.object(showcase, "start_console") as start, \
                patch("jarvis.ui.app_window") as window:
            ok, text = open_page("terminal")
        self.assertTrue(ok)
        start.assert_not_called()
        self.assertEqual(window.call_args.args[0], "http://127.0.0.1:7790/terminal.html")
        self.assertIn("Pro terminal", text)

    def test_starts_a_console_when_none_runs(self):
        with patch.object(showcase, "console_port", return_value=None), \
                patch.object(showcase, "start_console", return_value=7777) as start, \
                patch("jarvis.ui.app_window") as window:
            ok, _ = open_page("markets")
        self.assertTrue(ok)
        start.assert_called_once()
        self.assertEqual(window.call_args.args[0], "http://127.0.0.1:7777/trade.html")

    def test_unknown_page(self):
        ok, text = open_page("../secrets")
        self.assertFalse(ok)
        self.assertIn("pages:", text)

    def test_travels_as_a_desktop_action(self):
        with patch.object(showcase, "open_page", return_value=(True, "Opened.")) as page:
            self.assertEqual(remote.run_here("open_page", "terminal"), (True, "Opened."))
        page.assert_called_once_with("terminal")


if __name__ == "__main__":
    unittest.main()
