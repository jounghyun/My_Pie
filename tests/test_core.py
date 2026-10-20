"""핵심 로직 테스트 (인터넷 연결 없이, 실제 주문 없이 돌아간다).

실행: python3 -m unittest discover -s tests -v
"""

import base64
import hashlib
import hmac
import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from mypie import backtest, indicators, pin, web
from mypie.bot import Bot
from mypie.broker import LiveBroker, PaperBroker
from mypie.config import DEFAULTS
from mypie.db import Database
from mypie.strategies import STRATEGIES, create_strategy, strategy_params
from mypie.timeutil import KST, trading_day
from mypie.upbit import UpbitError, make_token

logging.disable(logging.CRITICAL)


def candle(day, o, h, l, c):
    return {"day": day, "open": o, "high": h, "low": l, "close": c, "volume": 0}


def flat_history(n, price=100.0):
    return [candle(f"d{i:04d}", price, price, price, price) for i in range(n)]


class FakeClient:
    """업비트 대신 쓰는 가짜 거래소. 모든 주문이 가격 100원에 바로 체결된다."""

    def __init__(self, krw=1_000_000.0, coin=0.0, reject=None):
        self.krw, self.coin, self.reject = krw, coin, reject
        self.orders, self.attempts = [], 0

    def balance(self, currency):
        return self.krw if currency == "KRW" else self.coin

    def accounts(self):
        return [{"currency": "KRW", "balance": str(self.krw), "locked": "0"},
                {"currency": "BTC", "balance": str(self.coin), "locked": "0"}]

    def place_order(self, params):
        self.attempts += 1
        if self.reject == params["side"]:
            raise UpbitError("주문 거부 (테스트)")
        self.orders.append(params)
        return {"uuid": f"u{len(self.orders)}"}

    def wait_order(self, uuid):
        params = self.orders[int(uuid[1:]) - 1]
        if params["side"] == "bid":
            funds = float(params["price"])
            volume, fee = funds / 100, funds * 0.0005
            self.krw -= funds + fee
            self.coin += volume
        else:
            volume = float(params["volume"])
            funds, fee = volume * 100, volume * 100 * 0.0005
            self.coin -= volume
            self.krw += funds - fee
        return {"uuid": uuid, "state": "done", "executed_volume": str(volume), "paid_fee": str(fee),
                "trades": [{"volume": str(volume), "funds": str(funds)}]}


class TradingDayTest(unittest.TestCase):
    def test_rolls_over_at_9am_kst(self):
        self.assertEqual(trading_day(datetime(2026, 10, 21, 8, 59, 59, tzinfo=KST)), "2026-10-20")
        self.assertEqual(trading_day(datetime(2026, 10, 21, 9, 0, 0, tzinfo=KST)), "2026-10-21")


class IndicatorTest(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(indicators.sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(indicators.sma([1], 2))

    def test_rsi_extremes(self):
        self.assertEqual(indicators.rsi(list(range(1, 30)), 14), 100.0)
        self.assertEqual(indicators.rsi(list(range(30, 1, -1)), 14), 0.0)

    def test_bollinger_flat(self):
        self.assertEqual(indicators.bollinger([5.0] * 20), (5.0, 5.0, 5.0))

    def test_macd_positive_in_uptrend(self):
        line, sig = indicators.macd([float(i) ** 1.5 for i in range(60)])
        self.assertGreater(line, 0)
        self.assertGreater(line, sig)

    def test_stochastic_at_top(self):
        values = [float(i) for i in range(30)]
        k, d = indicators.stochastic(values, values, values)
        self.assertAlmostEqual(k, 100)
        self.assertAlmostEqual(d, 100)


class StrategyTest(unittest.TestCase):
    def test_every_strategy_has_explanations(self):
        for cls in STRATEGIES.values():
            self.assertTrue(cls.label and cls.description and cls.guide, cls.name)

    def test_volatility_breakout_target(self):
        plan = create_strategy("volatility_breakout", {"k": 0.5}).plan([candle("y", 90, 120, 80, 100)], 100)
        self.assertAlmostEqual(plan.buy_stop, 120)
        self.assertTrue(plan.sell_at_open)

    def test_ma_filter_skips_downtrend(self):
        hist = [candle(f"d{i}", 0, 0, 0, c) for i, c in enumerate([200, 200, 200, 200, 100])]
        plan = create_strategy("volatility_breakout_ma").plan(hist, 100)
        self.assertIsNone(plan.buy_stop)
        self.assertTrue(plan.sell_at_open)

    def test_turtle_levels(self):
        hist = flat_history(25)
        hist[-15] = candle("x", 100, 150, 100, 100)   # 20일 안의 최고가
        hist[-12] = candle("y", 100, 100, 50, 100)    # 10일 밖이라 무시돼야 할 최저가
        plan = create_strategy("turtle").plan(hist, 100)
        self.assertEqual(plan.buy_stop, 150)
        self.assertEqual(plan.sell_stop, 100)

    def test_ma_cross(self):
        up = [candle(f"d{i}", 0, 0, 0, float(i)) for i in range(30)]
        self.assertTrue(create_strategy("ma_cross").plan(up, 0).buy_at_open)
        self.assertTrue(create_strategy("ma_cross").plan(up[::-1], 0).sell_at_open)

    def test_rsi_oversold_buys(self):
        down = [candle(f"d{i}", 0, 0, 0, float(100 - i)) for i in range(30)]
        self.assertTrue(create_strategy("rsi").plan(down, 0).buy_at_open)

    def test_momentum(self):
        hist = [candle(f"d{i}", 0, 0, 0, float(100 + i)) for i in range(40)]
        self.assertTrue(create_strategy("momentum").plan(hist, 0).buy_at_open)

    def test_not_enough_data_does_nothing(self):
        for name in STRATEGIES:
            if name == "buy_and_hold":
                continue
            plan = create_strategy(name).plan([], 100)
            self.assertFalse(plan.buy_at_open, name)
            self.assertIsNone(plan.buy_stop, name)

    def test_params_from_config(self):
        cfg = {"strategy": {"name": "volatility_breakout", "k": 0.3}, "params": {"rsi": {"period": 7}}}
        self.assertEqual(strategy_params(cfg, "volatility_breakout"), {"k": 0.3})
        self.assertEqual(strategy_params(cfg, "rsi"), {"period": 7})


class BacktestTest(unittest.TestCase):
    def test_buy_and_hold_matches_price_change(self):
        candles = [candle(f"d{i:03d}", 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i) for i in range(100)]
        r = backtest.simulate(create_strategy("buy_and_hold"), candles, 0.0005, warmup=10)
        # 10번째 날 시가(110)에 사서 마지막 날 종가(199)로 평가
        self.assertAlmostEqual(r["total_return"], 199 / (110 * 1.0005) - 1)
        self.assertEqual(r["trades"], 0)
        self.assertAlmostEqual(r["exposure"], 1.0)

    def test_volatility_breakout_round_trip(self):
        candles = flat_history(3) + [candle("buy", 100, 130, 100, 125), candle("sell", 125, 125, 125, 125)]
        r = backtest.simulate(create_strategy("volatility_breakout", {"k": 0.5}), candles, 0.0, warmup=3)
        # 전날 변동폭 0 → 목표가 = 시가 100, 시가에 돌파 매수 → 다음 날 시가 125 매도
        self.assertEqual(r["trades"], 1)
        self.assertAlmostEqual(r["total_return"], 0.25)
        self.assertEqual(r["win_rate"], 1.0)

    def test_rank_strategies_returns_all_sorted(self):
        candles = [candle(f"d{i:03d}", 100.0 + i % 7, 103.0 + i % 7, 98.0 + i % 7, 101.0 + i % 7) for i in range(200)]
        results = backtest.rank_strategies(candles, 0.0005)
        self.assertEqual(len(results), len(STRATEGIES))
        self.assertEqual([r["rank"] for r in results], list(range(1, len(STRATEGIES) + 1)))
        returns = [r["total_return"] for r in results]
        self.assertEqual(returns, sorted(returns, reverse=True))
        self.assertTrue(all(r["guide"] for r in results))


class TempDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class PaperBrokerTest(TempDbTest):
    def test_round_trip_with_fees(self):
        b = PaperBroker(self.db, "KRW-BTC", 1_000_000, 0.0005)
        buy = b.buy(100_000_000, 1_000_000)
        self.assertAlmostEqual(b.account()["krw"], 0)
        self.assertAlmostEqual(buy["fee"] + buy["volume"] * 100_000_000, 1_000_000)

        sell = b.sell(110_000_000)
        expected = buy["volume"] * 110_000_000 * (1 - 0.0005)
        self.assertAlmostEqual(b.account()["krw"], expected)
        self.assertAlmostEqual(sell["profit"], expected - 1_000_000)
        self.assertEqual(b.account()["coin"], 0)

    def test_account_survives_restart(self):
        PaperBroker(self.db, "KRW-BTC", 1_000_000, 0.0005).buy(100, 400_000)
        again = PaperBroker(self.db, "KRW-BTC", 1_000_000, 0.0005)
        self.assertAlmostEqual(again.account()["krw"], 600_000)
        self.assertGreater(again.account()["coin"], 0)


class LiveBrokerTest(TempDbTest):
    def test_buy_stays_within_budget_and_pays_fee_on_top(self):
        client = FakeClient(krw=1_000_000)
        b = LiveBroker(self.db, "KRW-BTC", 100_000, 0.0005, client)
        trade = b.buy(100, b.account()["krw"])
        self.assertEqual(client.orders[0], {"market": "KRW-BTC", "side": "bid", "ord_type": "price", "price": "99950"})
        self.assertAlmostEqual(trade["krw"], 99950 * 1.0005)
        self.assertAlmostEqual(b.account()["coin"], 999.5)
        self.assertLess(b.account()["krw"], 1)                      # 한도를 거의 다 씀
        self.assertGreater(client.krw, 899_000)                      # 계좌의 나머지 돈은 그대로

    def test_sell_only_what_the_bot_bought(self):
        client = FakeClient(krw=1_000_000, coin=5.0)                 # 원래 갖고 있던 코인 5개
        b = LiveBroker(self.db, "KRW-BTC", 100_000, 0.0005, client)
        b.buy(100, b.account()["krw"])
        trade = b.sell(100)
        self.assertEqual(client.orders[1]["volume"], "999.50000000")
        self.assertAlmostEqual(client.coin, 5.0)                     # 원래 코인은 남아 있어야 한다
        self.assertEqual(b.account()["coin"], 0)
        self.assertLess(trade["profit"], 0)                          # 같은 값에 사고팔면 수수료만큼 손해

    def test_not_enough_krw_is_rejected_before_ordering(self):
        client = FakeClient(krw=3_000)
        b = LiveBroker(self.db, "KRW-BTC", 100_000, 0.0005, client)
        with self.assertRaises(UpbitError):
            b.buy(100, b.account()["krw"])
        self.assertEqual(client.attempts, 0)


class TokenTest(unittest.TestCase):
    def test_jwt_signature_and_query_hash(self):
        token = make_token("my-access", "my-secret", {"market": "KRW-BTC", "side": "bid"})
        header, body, sig = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad(body)))
        self.assertEqual(payload["access_key"], "my-access")
        self.assertEqual(payload["query_hash"], hashlib.sha512(b"market=KRW-BTC&side=bid").hexdigest())
        self.assertEqual(payload["query_hash_alg"], "SHA512")
        expected = hmac.new(b"my-secret", f"{header}.{body}".encode(), hashlib.sha256).digest()
        self.assertEqual(base64.urlsafe_b64decode(pad(sig)), expected)


class BotFlowTest(TempDbTest):
    """업비트 대신 가짜 시세를 넣어서 봇이 하루하루 어떻게 움직이는지 확인한다."""

    def setUp(self):
        super().setUp()
        self.cfg = {section: dict(values) for section, values in DEFAULTS.items()}
        self.cfg["backtest"]["days"] = 5
        self.client = FakeClient()

    def factory(self, mode, market, budget):
        if mode == "live":
            return LiveBroker(self.db, market, budget, 0.0005, self.client)
        return PaperBroker(self.db, market, 1_000_000, 0.0005)

    def make_bot(self):
        return Bot(self.cfg, self.db, self.factory)

    def run_day(self, bot, day, today_open, prev, prices, steps=None):
        """day 거래일에 prices 순서대로 시세가 들어온다고 보고 봇을 steps번 돌린다."""
        today = candle(day, today_open, today_open, today_open, prices[0])

        def daily_candles(market, count=200, to=None):
            return [] if to else [today, prev]  # 과거 일봉은 더 없는 걸로

        with mock.patch("mypie.bot.trading_day", return_value=day), \
             mock.patch("mypie.upbit.get_daily_candles", side_effect=daily_candles), \
             mock.patch("mypie.upbit.get_krw_markets", return_value=[]), \
             mock.patch("mypie.upbit.get_price", side_effect=list(prices) + [prices[-1]] * 10):
            for _ in range(steps or len(prices)):
                bot.step()

    def test_breakout_buy_survives_restart_and_sells_next_day(self):
        bot = self.make_bot()
        prev = candle("2026-10-20", 90, 120, 80, 100)            # 목표가 = 100 + 40 × 0.5 = 120
        self.run_day(bot, "2026-10-21", 100, prev, [110, 125])   # 110 대기 → 125 매수
        self.assertAlmostEqual(bot.broker.account()["krw"], 0)

        restarted = self.make_bot()                               # 같은 날 재시작해도 팔면 안 된다
        self.run_day(restarted, "2026-10-21", 100, prev, [126])
        self.assertAlmostEqual(restarted.broker.account()["krw"], 0)

        self.run_day(restarted, "2026-10-22", 130, candle("2026-10-21", 100, 130, 100, 126), [130])
        trades = self.db.recent_trades()
        self.assertEqual([t["side"] for t in trades], ["sell", "buy"])
        self.assertGreater(trades[0]["profit"], 0)

    def test_control_request_sells_then_switches_market_and_strategy(self):
        bot = self.make_bot()
        prev = candle("2026-10-20", 90, 120, 80, 100)
        self.run_day(bot, "2026-10-21", 100, prev, [125])          # 변동성 돌파로 매수

        self.db.set_state("control_request", {"market": "KRW-ETH", "mode": "paper", "budget": 100_000,
                                              "strategy": "buy_and_hold"})
        self.run_day(bot, "2026-10-21", 100, prev, [126, 127], steps=2)  # 매도 → 새 종목에서 바이앤홀드 매수

        self.assertEqual(bot.strategy.name, "buy_and_hold")
        self.assertEqual(bot.market, "KRW-ETH")
        self.assertIsNone(self.db.get_state("control_request"))
        trades = self.db.recent_trades()
        self.assertEqual([(t["side"], t["market"]) for t in trades],
                         [("buy", "KRW-ETH"), ("sell", "KRW-BTC"), ("buy", "KRW-BTC")])
        again = self.make_bot()                                    # 재시작해도 고른 설정이 유지된다
        self.assertEqual((again.market, again.strategy.name), ("KRW-ETH", "buy_and_hold"))

    def test_live_trading_uses_budget_and_halts_on_order_error(self):
        self.cfg["general"]["mode"] = "live"
        self.cfg["live"]["budget"] = 50_000
        self.client.reject = "bid"
        bot = self.make_bot()
        prev = candle("2026-10-20", 90, 120, 80, 100)
        self.run_day(bot, "2026-10-21", 100, prev, [125, 126, 127])
        self.assertEqual(self.client.attempts, 1)                  # 한 번 실패하면 더 주문하지 않는다
        self.assertIsNotNone(self.db.get_state("status")["halted"])

        self.client.reject = None
        self.db.set_state("resume_request", {})
        self.run_day(bot, "2026-10-21", 100, prev, [128])          # 다시 시작하면 매수
        self.assertEqual(self.client.orders[-1]["price"], str(int(50_000 / 1.0005)))
        self.assertIsNone(self.db.get_state("status")["halted"])
        self.assertEqual(self.db.recent_trades()[0]["mode"], "live")

    def test_live_without_keys_falls_back_to_paper(self):
        self.cfg["general"]["mode"] = "live"
        with mock.patch("mypie.bot.load_keys", return_value=None):
            bot = Bot(self.cfg, self.db)
        self.assertEqual(bot.broker.mode, "paper")


class WebTest(TempDbTest):
    def setUp(self):
        super().setUp()
        self.db.set_state(pin.STATE_KEY, pin.make("2468"))
        web._pin_failures.clear()
        self.request = {"strategy": "rsi", "market": "KRW-BTC", "mode": "paper", "budget": 100_000}

    def tearDown(self):
        web._pin_failures.clear()
        super().tearDown()

    def test_pin_verify(self):
        stored = pin.make("1357")
        self.assertTrue(pin.verify("1357", stored))
        self.assertFalse(pin.verify("1358", stored))

    def test_control_needs_right_pin(self):
        code, _ = web.request_control(self.path, {**self.request, "pin": "0000"}, 1_000_000)
        self.assertEqual(code, 403)
        code, _ = web.request_control(self.path, {**self.request, "pin": "2468"}, 1_000_000)
        self.assertEqual(code, 200)
        self.assertEqual(self.db.get_state("control_request")["strategy"], "rsi")

    def test_locks_after_too_many_failures(self):
        for _ in range(web.MAX_PIN_FAILURES):
            web.request_control(self.path, {**self.request, "pin": "0000"}, 1_000_000)
        code, _ = web.request_control(self.path, {**self.request, "pin": "2468"}, 1_000_000)
        self.assertEqual(code, 429)

    def test_live_needs_keys_confirmation_and_sane_budget(self):
        live = {**self.request, "mode": "live", "pin": "2468"}
        with mock.patch("mypie.web.load_keys", return_value=None):
            self.assertEqual(web.request_control(self.path, {**live, "confirm_live": True}, 1_000_000)[0], 400)
        with mock.patch("mypie.web.load_keys", return_value=("a", "b")):
            self.assertEqual(web.request_control(self.path, live, 1_000_000)[0], 400)
            self.assertEqual(web.request_control(self.path, {**live, "budget": 5_000_000, "confirm_live": True},
                                                 1_000_000)[0], 400)
            self.assertEqual(web.request_control(self.path, {**live, "confirm_live": True}, 1_000_000)[0], 200)

    def test_rejects_unknown_strategy_and_market(self):
        self.assertEqual(web.request_control(self.path, {**self.request, "strategy": "nope", "pin": "2468"}, 1e6)[0], 400)
        self.assertEqual(web.request_control(self.path, {**self.request, "market": "BTC", "pin": "2468"}, 1e6)[0], 400)


if __name__ == "__main__":
    unittest.main()
