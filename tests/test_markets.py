"""True Markets recon client and the strategy lab. No network: HTTP is mocked."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import markets, strategy
from jarvis.markets import TrueMarkets, b64u, es256_sign
from jarvis.strategy import Book, backtest, evaluate, parse, rsi, sma, validate


def reply(payload):
    return io.BytesIO(json.dumps(payload).encode())


class SignerTests(unittest.TestCase):
    def test_rfc6979_p256_sha256_vector(self):
        """RFC 6979 appendix A.2.5, message "sample"."""
        d = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
        sig = es256_sign(d, b"sample")
        self.assertEqual(sig[:32].hex().upper(),
                         "EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716")
        self.assertEqual(sig[32:].hex().upper(),
                         "F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8")


class ClientTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        d = (12345).to_bytes(32, "big")
        (self.state / "truemarkets-key.json").write_text(json.dumps({
            "key_id": "kid-1", "label": "jarvis",
            "private_key": {"kty": "EC", "crv": "P-256", "d": b64u(d)}}))
        self.tm = TrueMarkets(self.state)

    def test_token_exchange_signs_key_id_and_timestamp(self):
        responses = [reply({"access_token": "tok", "expires_in": "2099-01-01T00:00:00Z"}),
                     reply({"symbol": "BTC", "candles": []})]
        with patch("jarvis.markets.urllib.request.urlopen", side_effect=responses) as net:
            self.tm.candles("BTC")
        auth, call = (c.args[0] for c in net.call_args_list)
        body = json.loads(auth.data)
        self.assertTrue(auth.full_url.endswith("/v1/auth/api-key/token"))
        self.assertEqual(body["key_id"], "kid-1")
        self.assertEqual(len(markets.b64u_decode(body["signature"])), 64)
        self.assertEqual(call.get_header("Authorization"), "Bearer tok")
        self.assertEqual(call.get_method(), "GET")

    def test_recon_allowlist_refuses_trading_paths(self):
        for path in ("/v1/conductor/orders", "/v1/defi/core/trade", "/v1/conductor/transfers"):
            with self.assertRaises(ValueError):
                self.tm.get(path)

    def test_bad_ticker_and_window_rejected_before_network(self):
        with patch("jarvis.markets.urllib.request.urlopen") as net:
            for args in (("BTC&x=1",), ("BTC", "2y")):
                with self.assertRaises(ValueError):
                    self.tm.history(*args)
            net.assert_not_called()

    def test_monitor_computes_changes_from_batch_history(self):
        def fake(path, params=None, ttl=0):
            if "market-caps" in path:
                return {"assets": [{"symbol": "BTC", "market_cap": "100"},
                                   {"symbol": "USDC", "market_cap": "10"}]}
            if "batch" in path:
                return {"results": [
                    {"symbol": "BTC", "points": [{"price": "100"}, {"price": "90"},
                                                 {"price": "110"}]},
                    {"symbol": "USDC", "points": [{"price": "1"}, {"price": "1"}]}]}
            return {"data": [{"symbol": "BTC", "name": "Bitcoin", "is_active": True},
                             {"symbol": "USDC", "name": "USD Coin", "is_active": True,
                              "stable": True}]}
        with patch.object(self.tm, "get", side_effect=fake):
            rows = self.tm.monitor()["rows"]
            intel = self.tm.intel()
        btc = rows[0]
        self.assertEqual((btc["last"], btc["chg_24h"], btc["high"], btc["low"]),
                         (110, 10.0, 110, 90))
        self.assertAlmostEqual(btc["chg_1h"], 22.222, places=3)
        self.assertEqual(intel["breadth"], {"up": 1, "down": 0, "n": 1})  # stables excluded


class StrategyTests(unittest.TestCase):
    def test_indicators(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        self.assertEqual(rsi([1, 2, 3, 4, 5], 2)[-1], 100.0)
        self.assertLess(rsi([5, 4, 3, 2, 1], 2)[-1], 1)

    def test_rules_are_data_not_code(self):
        self.assertEqual(parse("sma(20) crosses_above sma(50)"),
                         ("sma(20)", "crosses_above", "sma(50)"))
        for bad in ("__import__('os') > 1", "sma(20) > ", "price == 3", "sma(9999) > 1"):
            with self.assertRaises(ValueError):
                parse(bad)

    def test_crossover_fires_once(self):
        prices = [10, 9, 8, 9, 11, 12]
        self.assertEqual(evaluate("price crosses_above 10", prices, {}),
                         [False, False, False, False, True, False])

    def test_backtest_fills_next_bar_and_reports(self):
        spec = validate({"name": "t", "symbols": ["BTC"], "window": "30d", "resolution": "1d",
                         "entry": ["price > 100"], "exit": ["price < 100"], "fee_bps": 0})
        prices = [90, 90, 101, 120, 120, 99, 80, 80, 80, 80]
        result = backtest(spec, prices)
        trade = result["trades"][0]
        self.assertEqual((trade["entry_px"], trade["exit_px"]), (120, 80))  # next-bar fills
        self.assertEqual(result["metrics"]["total_return"], round((80 / 120 - 1) * 100, 2))
        self.assertEqual(result["signal"], "FLAT")
        self.assertLess(result["metrics"]["max_drawdown"], 0)

    def test_stop_loss_exits(self):
        spec = validate({"name": "t", "symbols": ["X"], "entry": ["price > 0"],
                         "stop_loss": 10, "fee_bps": 0})
        result = backtest(spec, [100, 100, 95, 89, 89, 50, 50, 50, 50, 50, 50])
        self.assertEqual(result["trades"][0]["exit_px"], 89)

    def test_book_saves_validates_and_deletes(self):
        with tempfile.TemporaryDirectory() as temp:
            book = Book(temp)
            saved = book.save({"name": "Mine", "symbols": ["btc", "eth"],
                               "entry": ["rsi(14) < 30"]})
            self.assertEqual(saved["symbols"], ["BTC", "ETH"])
            self.assertEqual(book.get(saved["id"])["name"], "Mine")
            with self.assertRaises(ValueError):
                book.save({"name": "x", "symbols": ["BTC"], "entry": ["nonsense"]})
            book.delete(saved["id"])
            self.assertEqual(book.all(), [])

    def test_templates_are_valid(self):
        for template in strategy.TEMPLATES:
            validate({**template, "symbols": ["BTC"]})


if __name__ == "__main__":
    unittest.main()
