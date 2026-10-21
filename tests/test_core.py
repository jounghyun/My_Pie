"""핵심 로직 테스트 (인터넷 연결 없이, 실제 주문 없이 돌아간다).

실행: python3 -m unittest discover -s tests -v
"""

import base64
import hashlib
import hmac
import json
import logging
import math
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from mypie import auth, backtest, indicators, keys, pin, reports, timeframes, upbit, web
from mypie.bot import Bot, validate_slots
from mypie.broker import Book, LiveExecutor, PaperExecutor, sync_full
from mypie.config import DEFAULTS
from mypie.db import Database
from mypie.strategies import STRATEGIES, create_strategy, strategy_params
from mypie.strategies.base import render_text
from mypie.timeutil import KST, trading_day
from mypie.upbit import OrderRejected, UpbitError, make_token

logging.disable(logging.CRITICAL)


def candle(day, o, h, l, c):
    return {"day": day, "open": o, "high": h, "low": l, "close": c, "volume": 0}


def flat_history(n, price=100.0):
    return [candle(f"d{i:04d}", price, price, price, price) for i in range(n)]


class FakeClient:
    """업비트 대신 쓰는 가짜 거래소. 모든 주문이 가격 100원에 바로 체결된다."""

    PRICE = 100.0

    def __init__(self, krw=1_000_000.0, coins=None, reject=None):
        self.krw, self.coins, self.reject = krw, dict(coins or {}), reject
        self.orders, self.filled, self.by_id = [], {}, {}
        self.attempts, self.fail_wait = 0, False

    def balance(self, currency):
        return self.krw if currency == "KRW" else self.coins.get(currency, 0.0)

    def accounts(self):
        rows = [{"currency": "KRW", "balance": str(self.krw), "locked": "0", "avg_buy_price": "0"}]
        rows += [{"currency": c, "balance": str(v), "locked": "0", "avg_buy_price": str(self.PRICE if v else 0)}
                 for c, v in self.coins.items()]
        return rows

    def key_expiry(self):
        return "2027-10-21T09:00:00+09:00"

    def place_order(self, params):
        self.attempts += 1
        if self.reject == params["side"]:
            raise OrderRejected("주문 거절 (테스트)", status=400, name="insufficient_funds")
        uuid = f"u{len(self.orders) + 1}"
        self.orders.append(params)
        self.by_id[params["identifier"]] = uuid
        coin = params["market"].split("-")[1]
        if params["side"] == "bid":
            funds = float(params["price"])
            volume, fee = funds / self.PRICE, funds * 0.0005
            self.krw -= funds + fee
            self.coins[coin] = self.coins.get(coin, 0.0) + volume
        else:
            volume = float(params["volume"])
            funds, fee = volume * self.PRICE, volume * self.PRICE * 0.0005
            self.coins[coin] -= volume
            self.krw += funds - fee
        self.filled[uuid] = {"uuid": uuid, "state": "done", "executed_volume": str(volume), "paid_fee": str(fee),
                             "trades": [{"volume": str(volume), "funds": str(funds)}]}
        return {"uuid": uuid}

    def wait_order(self, uuid):
        if self.fail_wait:
            raise UpbitError("통신 끊김 (테스트)")
        return self.filled[uuid]

    def get_order(self, uuid=None, identifier=None):
        uuid = uuid or self.by_id.get(identifier)
        if uuid is None:
            raise UpbitError("없는 주문 (테스트)", status=404, name="order_not_found")
        return self.filled[uuid]


class FakeMarket:
    """가짜 시세: 종목·판단 주기마다 직전 봉, 지금 봉의 시가, 현재가."""

    def __init__(self):
        self.price, self.bars = {}, {}

    def set(self, market, day, today_open, prev, price, tf="1d"):
        self.bars[(market, tf)] = (candle(day, today_open, today_open, today_open, price), prev)
        self.price[market] = price

    def prices(self, markets):
        missing = [m for m in markets if m not in self.price]
        if missing:
            raise UpbitError("Code not found (테스트)", status=404, name="not_found")
        return {m: self.price[m] for m in markets}

    def candles(self, market, tf="1d", count=200, to=None):
        return [] if to else list(self.bars[(market, tf)])


class TradingDayTest(unittest.TestCase):
    def test_rolls_over_at_9am_kst(self):
        self.assertEqual(trading_day(datetime(2026, 10, 21, 8, 59, 59, tzinfo=KST)), "2026-10-20")
        self.assertEqual(trading_day(datetime(2026, 10, 21, 9, 0, 0, tzinfo=KST)), "2026-10-21")


class TimeframeTest(unittest.TestCase):
    NOW = datetime(2026, 10, 21, 14, 37, 5, tzinfo=KST)                  # = 05:37 UTC

    def test_bar_keys(self):
        self.assertEqual(timeframes.bar_key("1d", self.NOW), "2026-10-21")
        self.assertEqual(timeframes.bar_key("240m", self.NOW), "2026-10-21T04:00")  # KST 13:00에 시작한 4시간봉
        self.assertEqual(timeframes.bar_key("60m", self.NOW), "2026-10-21T05:00")
        self.assertEqual(timeframes.bar_key("15m", self.NOW), "2026-10-21T05:30")
        self.assertEqual(timeframes.bar_key("1d", datetime(2026, 10, 21, 8, 0, tzinfo=KST)), "2026-10-20")

    def test_next_bar(self):
        self.assertEqual(timeframes.next_bar("60m", self.NOW).astimezone(KST).strftime("%H:%M"), "15:00")
        self.assertEqual(timeframes.next_bar("240m", self.NOW).astimezone(KST).strftime("%H:%M"), "17:00")
        self.assertEqual(timeframes.next_bar("1d", self.NOW).astimezone(KST).strftime("%d %H:%M"), "22 09:00")

    def test_minute_candles_from_upbit(self):
        raw = [{"candle_date_time_utc": "2026-10-21T05:00:00", "opening_price": 1, "high_price": 2, "low_price": 0.5,
                "trade_price": 1.5, "candle_acc_trade_volume": 3}]
        with mock.patch("mypie.upbit._get", return_value=raw) as get:
            bars = upbit.get_candles("KRW-BTC", "60m", 200, to="2026-10-21T06:00")
        self.assertEqual(bars[0]["day"], "2026-10-21T05:00")
        path, params = get.call_args[0]
        self.assertEqual((path, params["to"]), ("/candles/minutes/60", "2026-10-21T06:00:00"))

    def test_slot_validation(self):
        ok, _ = validate_slots([{"market": "KRW-BTC", "strategy": "rsi", "weight": 10}])
        self.assertEqual(ok[0]["tf"], "1d")                                # 주기를 안 정하면 일봉
        self.assertIsNotNone(validate_slots([{"market": "KRW-BTC", "strategy": "rsi", "tf": "7m", "weight": 10}])[1])


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

    def test_true_range_rma_and_linreg(self):
        self.assertEqual(indicators.true_ranges([12, 15], [8, 11], [10, 14]), [4, 5])   # 둘째 봉: 직전 종가 10 → 고가 15
        self.assertEqual(indicators.rma_series([2.0] * 5, 3), [2.0, 2.0, 2.0])
        self.assertAlmostEqual(indicators.linreg([1, 3, 5, 7], 4), 7)                   # 직선이면 마지막 값 그대로
        self.assertAlmostEqual(indicators.linreg([0, 2, 0, 2], 4), 1.6)


class StrategyTest(unittest.TestCase):
    def test_every_strategy_has_explanations(self):
        for cls in STRATEGIES.values():
            self.assertTrue(cls.label and cls.description and cls.guide, cls.name)

    def test_explanations_follow_timeframe(self):
        guide = STRATEGIES["ma_cross"].guide
        self.assertIn("최근 5일 평균 가격이 20일 평균보다", render_text(guide, 1440))
        self.assertIn("최근 5봉(1시간 15분) 평균 가격이 20봉(5시간) 평균보다", render_text(guide, 15))
        self.assertIn("다음 봉이 시작될 때", render_text(STRATEGIES["volatility_breakout"].guide, 60))
        for cls in STRATEGIES.values():
            for text in (cls.description, cls.guide):
                self.assertNotIn("{", render_text(text, 240), cls.name)   # 모르는 자리표시가 남으면 안 된다

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

    def test_supertrend_follows_trend(self):
        up = [candle(f"d{i}", 100 + i, 101 + i, 99 + i, 100 + i) for i in range(40)]
        self.assertTrue(create_strategy("supertrend").plan(up, 0).buy_at_open)
        crash = up + [candle(f"c{i}", 70, 71, 69, 70) for i in range(3)]       # 아래 선을 뚫고 마감
        self.assertTrue(create_strategy("supertrend").plan(crash, 0).sell_at_open)

    def test_smart_money_follows_structure_breaks(self):
        zigzag = [100 + 2 * i + 5 * math.sin(i * math.pi / 4) for i in range(60)]   # 고점·저점이 점점 높아짐
        up = [candle(f"d{i}", p, p + 1, p - 1, p) for i, p in enumerate(zigzag)]
        down = [candle(f"d{i}", 300 - p, 301 - p, 299 - p, 300 - p) for i, p in enumerate(zigzag)]
        strategy = create_strategy("smart_money", {"swing": 2})
        self.assertTrue(strategy.plan(up, 0).buy_at_open)
        self.assertTrue(strategy.plan(down, 0).sell_at_open)

    def test_squeeze_momentum_buys_breakout_and_sells_when_power_fades(self):
        flat = [candle(f"f{i}", 100, 101, 99, 100) for i in range(40)]          # 좁게 뭉친 구간 (스퀴즈)
        strategy = create_strategy("squeeze_momentum")
        self.assertFalse(strategy.plan(flat, 0).buy_at_open)
        burst = flat + [candle(f"b{i}", p, p + 1, p - 1, p) for i, p in enumerate((105, 112, 120))]
        self.assertTrue(strategy.plan(burst, 0).buy_at_open)
        fade = burst + [candle(f"x{i}", p, p + 1, p - 1, p) for i, p in enumerate((112, 104))]
        self.assertTrue(strategy.plan(fade, 0).sell_at_open)

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
        self.assertAlmostEqual(r["total_return"], 199 / (110 * 1.0005) - 1)
        self.assertEqual(r["trades"], 0)
        self.assertAlmostEqual(r["exposure"], 1.0)

    def test_volatility_breakout_round_trip(self):
        candles = flat_history(3) + [candle("buy", 100, 130, 100, 125), candle("sell", 125, 125, 125, 125)]
        r = backtest.simulate(create_strategy("volatility_breakout", {"k": 0.5}), candles, 0.0, warmup=3)
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

    def test_rank_period_runs_only_recent_bars_and_reports_fills(self):
        candles = [candle(f"d{i:03d}", 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i) for i in range(300)]
        results = backtest.rank_period(candles, 90, 0.0005)
        hold = next(r for r in results if r["name"] == "buy_and_hold")
        self.assertEqual(len(hold["detail"]["equity"]), 90)
        self.assertEqual(hold["detail"]["fills"], [{"i": 0, "side": "buy", "price": 310.0}])  # 기간 첫 봉 시가에 산다
        self.assertAlmostEqual(hold["total_return"], 399 / (310 * 1.0005) - 1)
        self.assertTrue(hold["detail"]["holding"])
        with self.assertRaises(ValueError):                             # 지표 계산용 앞부분이 모자라면 못 한다
            backtest.rank_period(candles[:100], 90, 0.0005)


class ValidateSlotsTest(unittest.TestCase):
    def test_rules(self):
        ok, problem = validate_slots([{"market": "KRW-BTC", "strategy": "rsi", "weight": 60},
                                      {"market": "KRW-ETH", "strategy": "turtle", "weight": "40"}])
        self.assertIsNone(problem)
        self.assertEqual([s["weight"] for s in ok], [60.0, 40.0])
        self.assertIn("100%", validate_slots([{"market": "KRW-BTC", "strategy": "rsi", "weight": 70},
                                              {"market": "KRW-ETH", "strategy": "rsi", "weight": 40}])[1])
        self.assertIn("두 번", validate_slots([{"market": "KRW-BTC", "strategy": "rsi", "weight": 10},
                                              {"market": "KRW-BTC", "strategy": "macd", "weight": 10}])[1])
        self.assertIsNotNone(validate_slots([])[1])
        self.assertIsNotNone(validate_slots([{"market": "BTC", "strategy": "rsi", "weight": 10}])[1])
        self.assertIsNone(validate_slots([{"market": "KRW-BTC", "strategy": "rsi", "weight": 0}])[1])   # 0%는 지켜보기
        self.assertIsNotNone(validate_slots([{"market": "KRW-BTC", "strategy": "rsi", "weight": -1}])[1])


class TempDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class BookTest(TempDbTest):
    def test_allocate_splits_cash_by_weight(self):
        book = Book(self.db, "book:paper", "paper", 1_000_000)
        book.slot("a", "KRW-BTC"), book.slot("b", "KRW-ETH")
        book.allocate({"a": 50, "b": 30}, {"KRW-BTC": 100, "KRW-ETH": 100})
        self.assertAlmostEqual(book.slot("a")["krw"], 500_000)
        self.assertAlmostEqual(book.slot("b")["krw"], 300_000)
        self.assertAlmostEqual(book.data["reserve"], 200_000)          # 비중에 안 들어간 20%는 대기

    def test_allocate_counts_coins_already_held(self):
        book = Book(self.db, "book:paper", "paper", 400_000)
        book.slot("a", "KRW-BTC").update(coin=6000.0, cost=600_000.0, avg_price=100.0)
        book.slot("b", "KRW-ETH")
        book.allocate({"a": 50, "b": 50}, {"KRW-BTC": 100, "KRW-ETH": 100})
        self.assertAlmostEqual(book.slot("a")["krw"], 0)               # 이미 몫보다 많이 들고 있음
        self.assertAlmostEqual(book.slot("b")["krw"], 400_000)

    def test_buy_sell_round_trip(self):
        book = Book(self.db, "book:paper", "paper", 0)
        book.slot("a", "KRW-BTC")["krw"] = 100_000
        ex = PaperExecutor(0.0005)
        book.apply_buy("a", *ex.buy("KRW-BTC", 100_000, 100, {}), "test")
        trade = book.apply_sell("a", *ex.sell("KRW-BTC", book.slot("a")["coin"], 110, {}), "test")
        self.assertAlmostEqual(book.slot("a")["krw"], 100_000 / 1.0005 * 1.1 * 0.9995)
        self.assertAlmostEqual(trade["profit"], book.slot("a")["krw"] - 100_000)

    def test_full_mode_sync_takes_existing_coins_and_splits_deposits(self):
        book = Book(self.db, "book:live", "all", 0)
        prices = {"KRW-BTC": 100, "KRW-ETH": 100}
        slots, weights = {"a": "KRW-BTC", "b": "KRW-ETH"}, {"a": 50, "b": 50}
        client = FakeClient(krw=50_000, coins={"BTC": 100.0, "DOGE": 5.0})  # DOGE는 포트폴리오 밖 → 무시
        self.assertEqual(sync_full(book, client.accounts(), slots, weights, prices), 0)
        self.assertEqual(book.slot("a")["coin"], 100.0)
        self.assertAlmostEqual(book.data["contributions"], 60_000)    # 원화 5만 + BTC 1만
        book.allocate(weights, prices)
        self.assertAlmostEqual(book.slot("a")["krw"], 20_000)          # 3만 몫 − 이미 가진 BTC 1만
        self.assertAlmostEqual(book.slot("b")["krw"], 30_000)

        client.krw += 30_000                                            # 입금 → 반반 나눠 담기
        self.assertAlmostEqual(sync_full(book, client.accounts(), slots, weights, prices), 30_000)
        self.assertAlmostEqual(book.slot("a")["krw"], 35_000)
        self.assertAlmostEqual(book.slot("b")["krw"], 45_000)
        self.assertAlmostEqual(book.data["contributions"], 90_000)

        client.krw -= 40_000                                            # 출금 → 각 줄 원화에서 나눠 빼기
        sync_full(book, client.accounts(), slots, weights, prices)
        self.assertAlmostEqual(book.cash(), 40_000)

    def test_full_mode_profit_counts_only_after_takeover(self):
        book = Book(self.db, "book:live", "all", 0)
        slots, weights = {"a": "KRW-BTC"}, {"a": 100}
        old = [{"currency": "KRW", "balance": "0", "avg_buy_price": "0"},
               {"currency": "BTC", "balance": "100", "avg_buy_price": "40"}]   # 예전에 40원에 산 코인
        sync_full(book, old, slots, weights, {"KRW-BTC": 100})
        self.assertAlmostEqual(book.slot("a")["cost"], 10_000)          # 넘겨받을 때 시세(100원)가 원가
        self.assertAlmostEqual(book.slot("a")["avg_price"], 100)

        trade = book.apply_sell("a", *PaperExecutor(0.0005).sell("KRW-BTC", 100, 110, {}), "test")
        self.assertAlmostEqual(trade["profit"], 11_000 * 0.9995 - 10_000)  # 봇이 굴린 동안 오른 만큼만

    def test_full_mode_sync_keeps_bot_cost_and_values_outside_coins_at_market(self):
        book = Book(self.db, "book:live", "all", 0)
        slots, weights = {"a": "KRW-BTC"}, {"a": 100}
        acc = lambda coin, avg: [{"currency": "KRW", "balance": "0", "avg_buy_price": "0"},
                                 {"currency": "BTC", "balance": str(coin), "avg_buy_price": str(avg)}]
        sync_full(book, acc(100, 40), slots, weights, {"KRW-BTC": 100})
        sync_full(book, acc(100, 40), slots, weights, {"KRW-BTC": 120})  # 업비트 평균가로 덮어쓰지 않는다
        self.assertAlmostEqual(book.slot("a")["cost"], 10_000)

        self.assertAlmostEqual(sync_full(book, acc(150, 60), slots, weights, {"KRW-BTC": 120}), 6_000)  # 코인 입금
        self.assertAlmostEqual(book.slot("a")["cost"], 16_000)          # 들어온 50개는 지금 시세 120원으로
        self.assertAlmostEqual(book.slot("a")["avg_price"], 16_000 / 150)

        sync_full(book, acc(75, 60), slots, weights, {"KRW-BTC": 120})   # 절반을 밖에서 뺌
        self.assertAlmostEqual(book.slot("a")["cost"], 8_000)
        sync_full(book, acc(0, 0), slots, weights, {"KRW-BTC": 120})
        self.assertEqual((book.slot("a")["cost"], book.slot("a")["avg_price"]), (0.0, 0.0))


class LiveExecutorTest(TempDbTest):
    def test_buy_keeps_pending_mark_until_cleared(self):
        client = FakeClient(krw=1_000_000)
        ex = LiveExecutor(self.db, client, 0.0005, full=False)
        volume, funds, fee = ex.buy("KRW-BTC", 100_000, 100, {"slot": "a"})
        order = client.orders[0]
        self.assertEqual((order["side"], order["ord_type"], order["price"]), ("bid", "price", "99950"))
        self.assertTrue(order["identifier"].startswith("mypie-"))
        self.assertIsNotNone(ex.pending())                              # 장부에 반영할 때까지 남겨 둔다
        ex.clear_pending()
        self.assertIsNone(ex.pending())

    def test_not_enough_krw_is_rejected_before_ordering(self):
        client = FakeClient(krw=3_000)
        ex = LiveExecutor(self.db, client, 0.0005, full=False)
        with self.assertRaises(OrderRejected):
            ex.buy("KRW-BTC", 100_000, 100, {})
        self.assertEqual(client.attempts, 0)
        self.assertIsNone(ex.pending())

    def test_full_mode_keeps_margin(self):
        client = FakeClient(krw=100_000)
        LiveExecutor(self.db, client, 0.0005, full=True).buy("KRW-BTC", 100_000, 100, {})
        self.assertEqual(client.orders[0]["price"], str(math.floor(100_000 * 0.995 / 1.0005)))


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
    """가짜 시세·가짜 거래소로 봇이 하루하루 어떻게 움직이는지 확인한다."""

    D1, D2 = "2026-10-21", "2026-10-22"
    BTC_PREV = candle("2026-10-20", 90, 120, 80, 100)                   # 다음 날 목표가 = 100 + 40 × 0.5 = 120

    def setUp(self):
        super().setUp()
        self.cfg = {section: dict(values) for section, values in DEFAULTS.items()}
        self.cfg["backtest"]["days"] = 5
        self.client = FakeClient()
        self.market = FakeMarket()

    def factory(self, mode, budget):
        if mode == "live":
            return LiveExecutor(self.db, self.client, 0.0005, full=budget == "all")
        return PaperExecutor(0.0005)

    def portfolio(self, *slots, mode="paper", budget=100_000):
        self.db.set_state("settings", {"mode": mode, "budget": budget, "slots": [
            {"id": sid, "market": m, "strategy": st, "weight": w} for sid, m, st, w in slots]})

    def make_bot(self):
        return Bot(self.cfg, self.db, self.factory)

    def go(self, bot, day, steps=1, errors_ok=False, ip="1.1.1.1", hour=None):
        """day 거래일(분봉 줄이 있으면 hour 봉 키도)에 봇을 steps번 돌린다."""
        with mock.patch("mypie.bot.trading_day", return_value=day), \
             mock.patch("mypie.bot.bar_key", side_effect=lambda tf, now=None: hour), \
             mock.patch("mypie.upbit.get_prices", side_effect=self.market.prices), \
             mock.patch("mypie.upbit.get_candles", side_effect=self.market.candles), \
             mock.patch("mypie.upbit.get_krw_markets", return_value=[]), \
             mock.patch("mypie.upbit.get_tickers", return_value=[]), \
             mock.patch("mypie.network.public_ip", return_value=ip):
            for _ in range(steps):
                try:
                    bot.step()
                except UpbitError:
                    if not errors_ok:
                        raise

    def btc(self, day, price, today_open=100, prev=None):
        self.market.set("KRW-BTC", day, today_open, prev or self.BTC_PREV, price)

    def trades(self):
        return [(t["side"], t["market"]) for t in reversed(self.db.recent_trades())]

    # ---- 모의투자 ----

    def test_migrates_old_single_strategy_setup(self):
        self.db.set_state("settings", {"market": "KRW-BTC", "mode": "paper", "budget": 100_000})
        self.db.set_state("active_strategy", {"name": "turtle"})
        self.db.set_state("paper_account", {"krw": 900_000.0, "coin": 0.0, "avg_price": 0.0, "cost": 0.0,
                                            "initial_krw": 1_000_000.0})
        bot = self.make_bot()
        self.assertEqual([s.conf() for s in bot.slots],
                         [{"id": "s1", "market": "KRW-BTC", "strategy": "turtle", "tf": "1d", "weight": 100.0}])
        self.assertAlmostEqual(bot.book.slot("s1")["krw"], 900_000)
        self.assertAlmostEqual(bot.book.data["contributions"], 1_000_000)

    def test_breakout_buy_survives_restart_and_sells_next_day(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 100))
        bot = self.make_bot()
        self.btc(self.D1, 110)
        self.go(bot, self.D1)                                          # 110 < 120 대기
        self.btc(self.D1, 125)
        self.go(bot, self.D1)                                          # 125 ≥ 120 매수
        self.assertEqual(self.trades(), [("buy", "KRW-BTC")])

        restarted = self.make_bot()                                     # 같은 날 재시작해도 팔면 안 된다
        self.btc(self.D1, 126)
        self.go(restarted, self.D1)
        self.btc(self.D2, 130, today_open=130, prev=candle(self.D1, 100, 130, 100, 126))
        self.go(restarted, self.D2)                                    # 다음 날 09시 매도
        self.assertEqual(self.trades(), [("buy", "KRW-BTC"), ("sell", "KRW-BTC")])
        self.assertGreater(self.db.recent_trades()[0]["profit"], 0)

    def test_two_slots_trade_separately_and_hold_signal_is_not_sold(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 50), ("b", "KRW-ETH", "buy_and_hold", 30))
        bot = self.make_bot()
        eth_prev = candle("2026-10-20", 50, 50, 50, 50)
        self.btc(self.D1, 110)
        self.market.set("KRW-ETH", self.D1, 50, eth_prev, 50)
        self.go(bot, self.D1)                                          # ETH 09시 매수 (30만), BTC 대기
        self.btc(self.D1, 125)
        self.go(bot, self.D1)                                          # BTC 돌파 매수 (50만)
        buys = {t["market"]: t["krw"] for t in self.db.recent_trades()}
        self.assertAlmostEqual(buys["KRW-ETH"], 300_000)
        self.assertAlmostEqual(buys["KRW-BTC"], 500_000)
        self.assertAlmostEqual(bot.book.data["reserve"], 200_000)       # 비중 밖 20%는 그대로

        eth_coin = bot.book.slot("b")["coin"]
        self.btc(self.D2, 130, today_open=130, prev=candle(self.D1, 100, 130, 100, 126))
        self.market.set("KRW-ETH", self.D2, 55, candle(self.D1, 50, 55, 50, 55), 55)
        self.go(bot, self.D2)                                          # 다음 날 09시
        self.assertEqual(self.trades()[-1], ("sell", "KRW-BTC"))        # 변동성 돌파 줄만 판다
        self.assertEqual(bot.book.slot("b")["coin"], eth_coin)          # '보유 유지' 줄은 그대로

    def test_weight_change_does_not_sell_but_removed_slot_does(self):
        self.portfolio(("a", "KRW-BTC", "buy_and_hold", 50), ("b", "KRW-ETH", "buy_and_hold", 50))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.market.set("KRW-ETH", self.D1, 100, candle("2026-10-20", 100, 100, 100, 100), 100)
        self.go(bot, self.D1)                                          # 둘 다 09시 매수
        btc_coin = bot.book.slot("a")["coin"]
        self.db.set_state("control_request", {"mode": "paper", "slots": [
            {"id": "a", "market": "KRW-BTC", "strategy": "buy_and_hold", "weight": 80}]})
        self.go(bot, self.D1, steps=2)
        self.assertEqual(self.trades()[2], ("sell", "KRW-ETH"))         # 빠진 줄만 판다
        self.assertGreater(bot.book.slot("a")["coin"], btc_coin)        # 비중이 늘어난 몫만큼 추가 매수
        self.assertEqual([s.id for s in bot.slots], ["a"])
        self.assertEqual(self.make_bot().settings["slots"][0]["weight"], 80.0)  # 재시작해도 유지

    def test_zero_weight_row_only_watches(self):
        self.portfolio(("a", "KRW-BTC", "buy_and_hold", 100), ("b", "KRW-ETH", "buy_and_hold", 0))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.market.set("KRW-ETH", self.D1, 100, candle("2026-10-20", 100, 100, 100, 100), 100)
        self.go(bot, self.D1, steps=2)
        self.assertEqual(self.trades(), [("buy", "KRW-BTC")])          # 0% 줄은 매수 신호가 나도 안 산다
        state = {x["id"]: x["state_text"] for x in self.db.get_state("status")["slots"]}
        self.assertIn("지켜보기", state["b"])

    def test_zero_weight_row_sells_held_coins_and_frees_the_money(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 50), ("b", "KRW-ETH", "buy_and_hold", 50))
        bot = self.make_bot()
        self.btc(self.D1, 125)                                          # 목표가 120 돌파 → BTC 매수
        self.market.set("KRW-ETH", self.D1, 100, candle("2026-10-20", 100, 100, 100, 100), 100)
        self.go(bot, self.D1)
        self.db.set_state("control_request", {"mode": "paper", "slots": [   # BTC 줄을 0%로 (비중만 바꿔서 바로 팔진 않음)
            {"id": "a", "market": "KRW-BTC", "strategy": "volatility_breakout", "weight": 0},
            {"id": "b", "market": "KRW-ETH", "strategy": "buy_and_hold", "weight": 50}]})
        self.go(bot, self.D1, steps=2)
        self.assertEqual(self.trades(), [("buy", "KRW-ETH"), ("buy", "KRW-BTC")])  # ETH는 09시, BTC는 장중 돌파
        self.btc(self.D2, 150, today_open=125, prev=candle(self.D1, 100, 130, 95, 125))
        self.go(bot, self.D2, steps=2)                                  # 다음 날 09시 매도, 목표가를 넘어도 다시 안 삼
        self.assertEqual(self.trades()[2:], [("sell", "KRW-BTC")])
        self.assertEqual(bot.book.slot("a")["krw"], 0.0)                # 판 돈은 대기 원화로
        self.assertGreaterEqual(bot.book.data["reserve"], self.db.recent_trades()[0]["krw"] - 1)

    def test_unknown_market_is_rejected(self):
        self.portfolio(("a", "KRW-BTC", "rsi", 100))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.db.set_state("control_request", {"mode": "paper", "slots": [
            {"market": "KRW-NOPE", "strategy": "rsi", "weight": 50}]})
        self.go(bot, self.D1)
        self.assertEqual([s.market for s in bot.slots], ["KRW-BTC"])
        self.assertIsNone(self.db.get_state("control_request"))

    # ---- 실거래 ----

    def test_rejected_order_is_retried_without_pausing(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 100), mode="live", budget=50_000)
        self.client.reject = "bid"
        bot = self.make_bot()
        self.btc(self.D1, 125)
        self.go(bot, self.D1, steps=2, errors_ok=True)
        self.assertEqual(self.client.attempts, 2)                       # 멈추지 않고 다시 시도
        self.client.reject = None
        self.go(bot, self.D1)
        self.assertEqual(self.client.orders[-1]["price"], str(int(50_000 / 1.0005)))
        self.assertEqual(self.db.recent_trades()[0]["mode"], "live")

    def test_power_loss_after_order_is_reconciled_and_trading_continues(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 100), mode="live", budget=100_000)
        self.client.fail_wait = True                                    # 주문은 들어갔는데 결과를 받기 전에 끊김
        bot = self.make_bot()
        self.btc(self.D1, 125)
        self.go(bot, self.D1, errors_ok=True)
        self.assertEqual(bot.book.slot("a")["coin"], 0)                 # 장부는 아직 모름

        self.client.fail_wait = False
        rebooted = self.make_bot()                                      # 전원 복구
        self.go(rebooted, self.D1)
        self.assertEqual(self.client.attempts, 1)                       # 같은 주문을 또 내지 않는다
        self.assertGreater(rebooted.book.slot("a")["coin"], 0)
        self.assertIn("재시작 후 확인", self.db.recent_trades()[0]["reason"])
        self.assertIsNone(rebooted.executor.pending())

    def test_power_loss_before_order_reached_upbit(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 100), mode="live", budget=100_000)
        self.db.set_state(LiveExecutor.PENDING_KEY, {"identifier": "mypie-lost", "side": "bid", "market": "KRW-BTC",
                                                     "slot": "a", "day": self.D1, "at": "2026-10-21 10:00:00"})
        bot = self.make_bot()
        self.btc(self.D1, 125)
        self.go(bot, self.D1)                                          # 확인 → 안 들어갔음 → 바로 정상 매수
        self.assertEqual(self.client.attempts, 1)
        self.assertGreater(bot.book.slot("a")["coin"], 0)

    def test_full_mode_sells_coins_already_in_the_account(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 100), mode="live", budget="all")
        self.client.krw, self.client.coins = 0.0, {"BTC": 100.0}       # 계좌에 원래 BTC만 있음
        bot = self.make_bot()
        self.btc(self.D1, 110)
        self.go(bot, self.D1)                                          # 변동성 돌파: 09시 매도 계획 → 전부 매도
        self.assertEqual(self.client.coins["BTC"], 0)
        self.assertEqual(self.trades(), [("sell", "KRW-BTC")])

    def test_full_mode_buys_whole_share_and_tops_up_after_deposit(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 100), mode="live", budget="all")
        self.client.krw = 100_000.0
        bot = self.make_bot()
        self.btc(self.D1, 125)
        self.go(bot, self.D1)
        self.assertEqual(self.client.orders[0]["price"], str(math.floor(100_000 * 0.995 / 1.0005)))
        self.client.krw += 50_000                                       # 보유 중에 입금
        self.go(bot, self.D1)
        self.assertEqual(len(self.client.orders), 2)                    # 입금한 돈으로 추가 매수
        self.assertAlmostEqual(bot.book.data["contributions"], 150_000, delta=1)

    def test_switch_to_full_mode_from_web_request(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 100))
        bot = self.make_bot()
        self.btc(self.D1, 110)
        self.db.set_state("control_request", {"mode": "live", "budget": "all", "slots": [
            {"id": "a", "market": "KRW-BTC", "strategy": "volatility_breakout", "weight": 100}]})
        with mock.patch("mypie.bot.load_keys", return_value=("a", "b")), \
             mock.patch("mypie.upbit.UpbitClient", return_value=self.client):
            self.go(bot, self.D1)
        self.assertEqual((bot.executor.mode, bot.settings["budget"]), ("live", "all"))
        self.assertTrue(bot.book.full)
        self.assertEqual(self.db.get_state("key_info")["expire_at"][:10], "2027-10-21")

    def test_warns_when_public_ip_changes(self):
        self.portfolio(("a", "KRW-BTC", "rsi", 100), mode="live", budget=100_000)
        bot = self.make_bot()
        self.btc(self.D1, 110)
        self.go(bot, self.D1, ip="1.1.1.1")                            # 업비트가 이 IP를 받아 줌
        self.assertEqual(self.db.get_state("network")["upbit_ok_ip"], "1.1.1.1")
        bot.last_ip_check = 0
        with mock.patch("mypie.bot.load_keys", return_value=("a", "b")):
            self.go(bot, self.D1, ip="2.2.2.2")
        self.assertTrue(self.db.get_state("status")["network"]["ip_changed"])
        self.assertTrue(any("IP가 바뀐" in e["message"] for e in self.db.recent_events(10)))

    def test_hourly_slot_plans_every_hour_and_daily_slot_is_untouched(self):
        self.portfolio(("a", "KRW-BTC", "volatility_breakout", 50), ("h", "KRW-ETH", "volatility_breakout", 50))
        settings = self.db.get_state("settings")
        settings["slots"][1]["tf"] = "60m"
        self.db.set_state("settings", settings)
        bot = self.make_bot()
        self.btc(self.D1, 110)                                           # 일봉 줄: 목표가 120 대기
        h1, h2 = "2026-10-21T03:00", "2026-10-21T04:00"
        eth_prev = candle("2026-10-21T02:00", 90, 120, 80, 100)           # 1시간봉 목표가 120
        self.market.set("KRW-ETH", h1, 100, eth_prev, 125, tf="60m")
        self.go(bot, self.D1, hour=h1)                                    # 1시간봉 줄만 돌파 매수
        self.assertEqual(self.trades(), [("buy", "KRW-ETH")])
        self.assertIn("다음 봉 시작", self.db.get_state("status")["slots"][1]["state_text"])

        self.market.set("KRW-ETH", h2, 130, candle(h1, 100, 130, 100, 126), 130, tf="60m")
        self.go(bot, self.D1, hour=h2)                                    # 다음 봉 시작 → 매도 (일봉은 그대로)
        self.assertEqual(self.trades(), [("buy", "KRW-ETH"), ("sell", "KRW-ETH")])
        self.assertEqual(bot.slots[0].plan_day, self.D1)
        self.assertEqual(bot.slots[1].plan_day, h2)

    def test_timeframe_change_sells_that_row(self):
        self.portfolio(("a", "KRW-BTC", "buy_and_hold", 100))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.go(bot, self.D1)                                             # 일봉 바이앤홀드 매수
        self.db.set_state("control_request", {"mode": "paper", "slots": [
            {"id": "a", "market": "KRW-BTC", "strategy": "buy_and_hold", "tf": "240m", "weight": 100}]})
        self.market.set("KRW-BTC", "2026-10-21T00:00", 100, candle("2026-10-20T20:00", 100, 100, 100, 100), 100, tf="240m")
        self.go(bot, self.D1, hour="2026-10-21T00:00")
        self.assertEqual(self.trades()[:2], [("buy", "KRW-BTC"), ("sell", "KRW-BTC")])
        self.assertEqual(bot.slots[0].tf, "240m")

    def test_live_without_keys_falls_back_to_paper(self):
        self.portfolio(("a", "KRW-BTC", "rsi", 100), mode="live", budget=100_000)
        with mock.patch("mypie.bot.load_keys", return_value=None):
            bot = Bot(self.cfg, self.db)
        self.assertEqual(bot.executor.mode, "paper")


class WebTest(TempDbTest):
    def setUp(self):
        super().setUp()
        self.db.set_state(pin.STATE_KEY, pin.make("2468"))
        web._pin_failures.clear()
        self.request = {"mode": "paper", "budget": 100_000,
                        "slots": [{"market": "KRW-BTC", "strategy": "rsi", "weight": 60},
                                  {"market": "KRW-ETH", "strategy": "turtle", "weight": 40}]}

    def tearDown(self):
        web._pin_failures.clear()
        super().tearDown()

    def test_pin_verify(self):
        stored = pin.make("1357")
        self.assertTrue(pin.verify("1357", stored))
        self.assertFalse(pin.verify("1358", stored))

    def test_control_needs_right_pin(self):
        self.assertEqual(web.request_control(self.path, {**self.request, "pin": "0000"}, 1_000_000)[0], 403)
        self.assertEqual(web.request_control(self.path, {**self.request, "pin": "2468"}, 1_000_000)[0], 200)
        self.assertEqual(len(self.db.get_state("control_request")["slots"]), 2)

    def test_locks_after_too_many_failures(self):
        for _ in range(web.MAX_PIN_FAILURES):
            web.request_control(self.path, {**self.request, "pin": "0000"}, 1_000_000)
        self.assertEqual(web.request_control(self.path, {**self.request, "pin": "2468"}, 1_000_000)[0], 429)

    def test_live_needs_keys_confirmation_and_sane_budget(self):
        live = {**self.request, "mode": "live", "pin": "2468"}
        with mock.patch("mypie.web.load_keys", return_value=None):
            self.assertEqual(web.request_control(self.path, {**live, "confirm_live": True}, 1_000_000)[0], 400)
        with mock.patch("mypie.web.load_keys", return_value=("a", "b")):
            self.assertEqual(web.request_control(self.path, live, 1_000_000)[0], 400)
            self.assertEqual(web.request_control(self.path, {**live, "budget": 5_000_000, "confirm_live": True},
                                                 1_000_000)[0], 400)
            self.assertEqual(web.request_control(self.path, {**live, "budget": "all", "confirm_live": True},
                                                 1_000_000)[0], 200)
            self.assertEqual(self.db.get_state("control_request")["budget"], "all")

            # 이미 실거래 중이면 종목·비중만 바꿀 때는 다시 체크하지 않는다
            self.db.set_state("status", {"settings": {"mode": "live", "budget": 100_000}})
            self.assertEqual(web.request_control(self.path, {**live, "budget": 100_000}, 1_000_000)[0], 200)
            self.assertEqual(web.request_control(self.path, {**live, "budget": "all"}, 1_000_000)[0], 400)  # 계좌 전액으로 넓힐 땐 체크
            self.db.set_state("status", {"settings": {"mode": "live", "budget": "all"}})
            self.assertEqual(web.request_control(self.path, {**live, "budget": "all"}, 1_000_000)[0], 200)

    def test_rejects_bad_portfolio(self):
        over = {**self.request, "pin": "2468", "slots": [{"market": "KRW-BTC", "strategy": "rsi", "weight": 90},
                                                         {"market": "KRW-ETH", "strategy": "rsi", "weight": 20}]}
        self.assertEqual(web.request_control(self.path, over, 1e6)[0], 400)
        self.assertEqual(web.request_control(self.path, {**self.request, "slots": [], "pin": "2468"}, 1e6)[0], 400)

    def test_ranking_request(self):
        self.assertEqual(web.request_ranking(self.path, "KRW-SOL")[0], 200)
        web.request_ranking(self.path, "KRW-XRP")
        web.request_ranking(self.path, "KRW-SOL")                       # 이미 줄 서 있으면 다시 넣지 않는다
        self.assertEqual(self.db.get_state("ranking_request")["markets"], ["KRW-SOL|1d", "KRW-XRP|1d"])
        self.assertEqual(web.request_ranking(self.path, "KRW-SOL", "60m")[0], 200)
        self.assertIn("KRW-SOL|60m", self.db.get_state("ranking_request")["markets"])
        self.assertEqual(web.request_ranking(self.path, "KRW-SOL", "7m")[0], 400)
        self.assertEqual(web.request_ranking(self.path, "SOL")[0], 400)

    def test_stats(self):
        self.db.set_state("status", {"mode": "paper"})
        for market, side, profit in (("KRW-BTC", "buy", None), ("KRW-BTC", "sell", 1000.0), ("KRW-ETH", "buy", None),
                                     ("KRW-ETH", "sell", -400.0), ("KRW-BTC", "buy", None), ("KRW-BTC", "sell", 300.0)):
            self.db.add_trade("paper", market, {"side": side, "price": 100, "volume": 1, "krw": 10_000 + (profit or 0),
                                                "fee": 5, "profit": profit})
        st = web.build_stats(self.path)
        self.assertEqual((st["round_trips"], st["wins"]), (3, 2))
        self.assertAlmostEqual(st["realized"], 900)
        self.assertEqual(st["streak"], {"kind": "win", "count": 1})
        self.assertEqual(st["by_market"]["KRW-BTC"], {"round_trips": 2, "wins": 2, "realized": 1300.0})

    def test_system_and_candles_endpoints_answer(self):
        self.assertIn("disk_total_gb", web.build_system(self.path))
        self.db.set_state("status", {"mode": "paper"})
        self.db.upsert_candles("KRW-ETH", [candle("2026-10-19", 1, 2, 0.5, 1.5), candle("2026-10-20", 1.5, 3, 1, 2)])
        data = web.build_candles(self.path, "KRW-ETH", 90)
        self.assertEqual([c["day"] for c in data["candles"]], ["2026-10-19", "2026-10-20"])

    def test_simulation_endpoint(self):
        start = datetime(2025, 1, 1)
        prices = [100 + 10 * math.sin(i / 5) for i in range(400)]
        self.db.upsert_candles("KRW-ETH", [candle((start + timedelta(days=i)).strftime("%Y-%m-%d"), p, p * 1.02, p * 0.98, p)
                                           for i, p in enumerate(prices)])
        cfg = {section: dict(values) for section, values in DEFAULTS.items()}
        sim = web.build_simulation(self.path, cfg, "KRW-ETH", "1d", 90, "rsi")
        self.assertTrue(sim["ready"])
        self.assertEqual((sim["days"], sim["detail"]["name"], len(sim["detail"]["close"])), (90, "rsi", 90))
        self.assertEqual(len(sim["results"]), len(STRATEGIES))
        self.assertNotIn("detail", sim["results"][0])
        self.assertEqual([p["ok"] for p in sim["periods"]], [True, True, True, False, False])  # 1년·3년은 봉이 모자람
        self.assertTrue(sim["stale"])                                   # 마지막 봉이 오래전
        self.assertEqual(web.build_simulation(self.path, cfg, "KRW-ETH", "1d", 1095, "")["days"], 180)  # 되는 기간 중 가장 긴 것
        self.assertTrue(web.build_simulation(self.path, cfg, "KRW-SOL", "1d", 90, "")["need_data"])
        self.assertFalse(web.build_simulation(self.path, cfg, "SOL", "1d", 90, "")["ready"])


class AuthTest(TempDbTest):
    def test_code_is_single_use_and_limited(self):
        code = auth.issue_code(self.db, 6)
        self.assertEqual(len(code), 6)
        self.assertFalse(auth.check_code(self.db, "x" + code[1:])[0])
        self.assertTrue(auth.check_code(self.db, code)[0])
        self.assertFalse(auth.check_code(self.db, code)[0])             # 한 번 쓰면 끝

    def test_resend_cooldown_and_attempt_limit(self):
        code = auth.issue_code(self.db, 6)
        with self.assertRaises(auth.TooSoon):
            auth.issue_code(self.db, 6)                                  # 30초 안에 또 요청
        for _ in range(auth.MAX_ATTEMPTS):
            auth.check_code(self.db, "000000" if code != "000000" else "111111")
        self.assertFalse(auth.check_code(self.db, code)[0])             # 다 틀려서 코드가 지워짐

    def test_expired_code(self):
        code = auth.issue_code(self.db, 6)
        stored = self.db.get_state(auth.CODE_KEY)
        self.db.set_state(auth.CODE_KEY, {**stored, "expires": 0})
        self.assertFalse(auth.check_code(self.db, code)[0])

    def test_sessions(self):
        token = auth.create_session(self.db, 30, {"ip": "1.2.3.4"})
        self.assertTrue(auth.valid_session(self.db, token))
        self.assertFalse(auth.valid_session(self.db, "nope"))
        auth.end_session(self.db, token)
        self.assertFalse(auth.valid_session(self.db, token))


class EnvTest(unittest.TestCase):
    def test_write_env_keeps_other_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            keys.save_keys("access", "secret", path)
            keys.write_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, path)
            self.assertEqual(keys.load_keys(path), ("access", "secret"))  # 업비트 키가 지워지면 안 된다
            self.assertEqual(keys.read_env(path)["TELEGRAM_CHAT_ID"], "1")


class TelegramSetupTest(TempDbTest):
    TOKEN = "123456789:" + "A" * 35

    def setUp(self):
        super().setUp()
        self.db.set_state(pin.STATE_KEY, pin.make("2468"))
        web._pin_failures.clear()
        self.env = {}  # 가짜 .env

    def test_link_from_web(self):
        def fake_call(token, method, params=None, timeout=8):
            old = {"update_id": 6, "message": {"chat": {"id": 99, "type": "private"}, "date": time.time() - 7200}}
            new = {"update_id": 7, "message": {"chat": {"id": 42, "type": "private"}, "date": time.time()}}
            return {"getMe": {"username": "my_pie_bot"}, "deleteWebhook": True,
                    "getUpdates": [old] if self.waiting else [old, new]}[method]
        self.waiting = True
        with mock.patch("mypie.telegram.call", side_effect=fake_call), \
             mock.patch("mypie.telegram.send") as send, \
             mock.patch("mypie.web.read_env", side_effect=lambda: dict(self.env)), \
             mock.patch("mypie.web.write_env", side_effect=self.env.update):
            self.assertEqual(web.telegram_start(self.path, {"token": self.TOKEN, "pin": "0000"})[0], 403)
            self.assertEqual(web.telegram_start(self.path, {"token": "bad", "pin": "2468"})[0], 400)
            code, body = web.telegram_start(self.path, {"token": self.TOKEN, "pin": "2468"})
            self.assertEqual((code, body["bot"]), (200, "my_pie_bot"))
            self.assertEqual(web._pending()["bot"], "my_pie_bot")       # 서버가 재시작돼도 .env에 남아 있음
            waiting = web.telegram_link()[1]
            self.assertFalse(waiting["linked"])                           # 2시간 전 메시지는 인정 안 함
            self.assertIn("1개", waiting["message"])                      # 받은 메시지 수는 알려 줌
            self.waiting = False
            code, body = web.telegram_link()
            self.assertTrue(body["linked"])
            self.assertEqual((self.env["TELEGRAM_BOT_TOKEN"], self.env["TELEGRAM_CHAT_ID"]), (self.TOKEN, "42"))
            self.assertIsNone(web._pending())                            # 기다리던 표시는 지움
            send.assert_called()


class ReportTest(TempDbTest):
    STATUS = {"mode": "paper", "updated_at": "2026-10-21 09:00:05",
              "account": {"equity": 1_012_345, "return_pct": 1.23, "contributions": 1_000_000, "reserve": 0},
              "slots": [{"market": "KRW-BTC", "tf_label": "일봉", "weight": 100, "holding": True, "price": 110_000_000,
                         "return_pct": 1.2, "state_text": "내일 09시에 매도 예정",
                         "strategy": {"label": "변동성 돌파"}}]}

    def add_trades(self):
        self.db.add_trade("paper", "KRW-BTC", {"side": "buy", "price": 100_000_000, "volume": 0.01, "krw": 1_000_000,
                                               "fee": 500, "profit": None})
        self.db.add_trade("paper", "KRW-BTC", {"side": "sell", "price": 101_000_000, "volume": 0.01, "krw": 1_009_000,
                                               "fee": 505, "profit": 9_000})

    def test_texts(self):
        self.add_trades()
        trades = self.db.trades("paper")
        self.assertIn("🔵 매도 BTC @101,000,000원", reports.trades_text(trades))
        self.assertIn("손익 +9,000원", reports.trades_text(trades))
        status = reports.status_text(self.STATUS)
        self.assertIn("총 자산 1,012,345원 (+1.23%)", status)
        self.assertIn("BTC 일봉 · 변동성 돌파", status)
        daily = reports.daily_text(trades, self.STATUS)
        self.assertIn("매매 2건 · 확정 손익 +9,000원 (1승 0패)", daily)

    def test_commands(self):
        self.add_trades()
        self.db.set_state("status", self.STATUS)
        self.assertIn("최근 매매 기록", reports.command_reply("/기록", self.db))
        self.assertIn("최근 매매 기록", reports.command_reply("/trades@my_pie_bot", self.db))
        self.assertIn("총 자산", reports.command_reply("/status", self.db))
        self.assertIn("명령어", reports.command_reply("안녕", self.db))       # 모르는 말에는 안내

    def test_morning_report_once_per_day(self):
        cfg = {section: dict(values) for section, values in DEFAULTS.items()}
        bot = Bot(cfg, self.db, lambda mode, budget: PaperExecutor(0.0005))
        sent = []
        bot.notifier = sent.append
        self.add_trades()
        self.db.set_state("status", self.STATUS)
        with mock.patch("mypie.telegram.load", return_value=("t", "1")):
            bot._daily_report("2026-10-21")                             # 처음 켰을 때는 안 보냄
            self.assertEqual(sent, [])
            bot._daily_report("2026-10-22")                             # 다음 거래일 아침에 보냄
            bot._daily_report("2026-10-22")                             # 같은 날 두 번은 안 보냄
        self.assertEqual(len(sent), 1)
        self.assertIn("아침 리포트", sent[0])


class WebLoginTest(TempDbTest):
    """진짜 웹 서버를 띄워서 텔레그램 코드 로그인 흐름을 확인한다."""

    def setUp(self):
        super().setUp()
        web.Handler.db_path = self.path
        web.Handler.login_enabled = True
        web.Handler.code_digits = 6
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.sent = []
        patches = [mock.patch("mypie.telegram.load", return_value=("t", "1")),
                   mock.patch("mypie.telegram.send", side_effect=lambda text, creds=None: self.sent.append(text))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, path, body=None, cookie=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode(), r.headers.get("Set-Cookie")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), None

    def test_login_flow(self):
        self.assertEqual(self.request("/api/status")[0], 401)
        status, page, _ = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("로그인 코드", page)                                  # 로그인 화면
        self.assertIn('maxlength="6"', page)                               # 설정한 자릿수가 들어감
        self.assertEqual(self.request("/api/login/send", {})[0], 200)
        code = re.match(r"(\d{6}) \(", self.sent[-1]).group(1)             # "123456 (요청한 IP)" 모양
        self.assertEqual(self.request("/api/login/verify", {"code": "x"})[0], 403)
        status, _, cookie = self.request("/api/login/verify", {"code": code})
        self.assertEqual(status, 200)
        session = cookie.split(";")[0]
        self.assertEqual(self.request("/api/status", cookie=session)[0], 200)
        self.assertEqual(self.request("/api/control", {}, cookie=None)[0], 401)
        self.request("/api/logout", {}, cookie=session)
        self.assertEqual(self.request("/api/status", cookie=session)[0], 401)

    def test_no_login_when_telegram_not_connected(self):
        with mock.patch("mypie.telegram.load", return_value=None):
            self.assertEqual(self.request("/api/status")[0], 200)


if __name__ == "__main__":
    unittest.main()
