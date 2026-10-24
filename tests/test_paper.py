"""Paper trading: the practice account, the strategy loop, voice commands. No network."""

import json
import math
import tempfile
import unittest

from jarvis import paper as P
from jarvis.paper import Paper, answer, is_trading_request, plain, rule_from_speech


class FakeMarket:
    def __init__(self, trend=0.0):
        self.trend = trend

    def configured(self):
        return True

    def history(self, symbol, window, resolution):
        base = {"BTC": 60000, "ETH": 3000, "SOL": 150}.get(symbol, 10)
        return [{"t": i, "price": base * (1 + 0.2 * math.sin(i / 15)) * (1 + self.trend * i)}
                for i in range(300)]


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paper = Paper(self.dir)
        self.market = FakeMarket()

    def test_fresh_account_has_ten_thousand(self):
        s = self.paper.summary()
        self.assertEqual((s["value"], s["cash"], s["pnl"]), (10000, 10000, 0))

    def test_manual_buy_and_sell_round_trip(self):
        self.paper.order("BTC", "buy", 500, price=100)
        s = self.paper.summary()
        self.assertEqual(s["cash"], 9500)
        self.assertEqual(len(s["positions"]), 1)
        out = self.paper.order("BTC", "sell", price=110)
        self.assertGreater(out["pnl"], 40)  # +10% less fees
        self.assertEqual(self.paper.summary()["positions"], [])

    def test_partial_sell_splits_the_lot(self):
        self.paper.order("ETH", "buy", 1000, price=100)
        self.paper.order("ETH", "sell", 400, price=100)
        (left,) = self.paper.summary()["positions"]
        self.assertAlmostEqual(left["units"] * 100, 1000 * (1 - 0.001) - 400, delta=1)

    def test_cannot_overspend_or_sell_what_you_lack(self):
        with self.assertRaises(ValueError):
            self.paper.order("BTC", "sell", price=100)
        self.paper.order("BTC", "buy", 20000, price=100)  # capped at cash
        self.assertEqual(self.paper.summary()["cash"], 0)
        with self.assertRaises(ValueError):
            self.paper.order("BTC", "buy", 10, price=100)

    def test_strategy_follows_its_rules_and_stop_sells(self):
        spec = self.paper.book.save({"name": "Always in", "symbols": ["BTC", "ETH"],
                                     "window": "365d", "resolution": "1d",
                                     "entry": ["price > 0"], "exit": []})
        self.paper.start(spec["id"], 2000)
        out = self.paper.tick(self.market, force=True)
        self.assertEqual(sorted(t["symbol"] for t in out["trades"]), ["BTC", "ETH"])
        self.assertAlmostEqual(self.paper.summary()["cash"], 8000, delta=1)
        self.assertEqual(self.paper.tick(self.market, force=True)["trades"], [])  # already in
        self.paper.stop(spec["id"])
        s = self.paper.summary()
        self.assertEqual(s["positions"], [])
        self.assertFalse(s["bots"][0]["on"])

    def test_tick_is_throttled_unless_forced(self):
        spec = self.paper.book.save({"name": "x", "symbols": ["BTC"], "entry": ["price > 0"]})
        self.paper.start(spec["id"])
        self.assertFalse(self.paper.tick(self.market, force=True)["skipped"])
        self.assertTrue(self.paper.tick(self.market)["skipped"])

    def test_reset_turns_everything_off(self):
        spec = self.paper.book.save({"name": "x", "symbols": ["BTC"], "entry": ["price > 0"]})
        self.paper.start(spec["id"])
        self.paper.tick(self.market, force=True)
        s = self.paper.reset()
        self.assertEqual((s["value"], s["running"], s["positions"]), (10000, 0, []))

    def test_state_is_plain_json(self):
        self.paper.order("SOL", "buy", 100, price=10)
        data = json.loads((self.paper.path).read_text())
        self.assertEqual(data["positions"][0]["symbol"], "SOL")


class SpeechTests(unittest.TestCase):
    def test_spoken_rules(self):
        cases = {"rsi is below 30": "rsi(14) < 30",
                 "the 20 day average crosses above the 50 day average": "sma(20) crosses_above sma(50)",
                 "price goes above the 200 day moving average": "price > sma(200)",
                 "price hits a new 20 day high": "price > high(20)",
                 "price is down 5 percent": "roc(1) < -5"}
        for said, rule in cases.items():
            self.assertEqual(rule_from_speech(said), rule, said)
        self.assertIsNone(rule_from_speech("the moon is full"))

    def test_plain_words(self):
        self.assertEqual(plain("sma(20) crosses_above sma(50)"),
                         "the 20-day average crosses above the 50-day average")

    def test_trading_requests_go_to_the_tool_brain(self):
        self.assertTrue(is_trading_request("make me a momentum strategy for doge"))
        self.assertTrue(is_trading_request("how's my paper account"))
        self.assertFalse(is_trading_request("open spotify"))


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paper = Paper(self.dir)
        P._ticker["thread"] = None
        self._ensure, P._ensure = P._ensure, lambda state, config: None

    def tearDown(self):
        P._ensure = self._ensure

    def say(self, text):
        return answer(text, {}, self.dir, market=FakeMarket(), paper=self.paper)

    def test_unrelated_speech_is_left_alone(self):
        for text in ("what time is it", "check the market", "buy bitcoin", "open spotify"):
            self.assertIsNone(self.say(text), text)

    def test_start_a_template_on_named_coins(self):
        reply = self.say("Jarvis, start paper trading buy the dip on bitcoin and ethereum")
        self.assertIn("Buy the dip · BTC ETH", reply)
        (bot,) = self.paper.summary()["bots"]
        self.assertTrue(bot["on"])
        self.assertEqual(bot["symbols"], ["BTC", "ETH"])

    def test_budget_is_heard(self):
        self.say("paper trade ride big trends on solana with $2,000")
        self.assertEqual(self.paper.summary()["bots"][0]["budget"], 2000)

    def test_create_by_voice_then_trade_it(self):
        reply = self.say("create a strategy called Cross that buys btc when the 20 day average "
                         "crosses above the 50 day average and sells when the 20 day average "
                         "crosses below the 50 day average")
        self.assertIn("Saved Cross", reply)
        self.assertIn("the 20-day average crosses above", reply)
        self.assertIn("Paper trading Cross", self.say("paper trade it"))

    def test_create_and_start_in_one_breath(self):
        reply = self.say("create a strategy that buys eth when rsi is below 35 and sells when "
                         "rsi is above 65 and paper trade it")
        self.assertIn("paper trading now", reply)

    def test_unreadable_strategy_falls_through_to_the_brain(self):
        self.assertIsNone(self.say("create a strategy that buys btc when the moon is full"))

    def test_orders_status_stop_reset(self):
        self.assertIn("Paper bought $500 of BTC", self.say("paper buy $500 of bitcoin"))
        self.assertIn("Paper sold", self.say("paper sell all my bitcoin"))
        self.assertIn("practice account is worth", self.say("how's my paper portfolio?"))
        self.say("paper trade catch breakouts")
        self.assertIn("Stopped", self.say("stop all paper trading"))
        self.assertIn("reset", self.say("reset my paper account"))

    def test_unknown_strategy_lists_the_options(self):
        self.assertIn("buy the dip", self.say("paper trade the unicorn"))


if __name__ == "__main__":
    unittest.main()
