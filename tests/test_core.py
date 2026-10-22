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

from mypie import (auth, backtest, indicators, keys, krx, network, pin, reports, sshwatch, stoploss, telegram, timeframes,
                   upbit, web)
from mypie.bot import Bot, check_account
from mypie.broker import Book, LiveExecutor, PaperExecutor, migrate_book
from mypie.config import DEFAULTS
from mypie.db import Database
from mypie.strategies import STRATEGIES, create_strategy, strategy_params
from mypie.strategies.base import DayPlan, Strategy, render_text
from mypie.strategies.combo import check_rule, make_rule, normalize_rule, rule_fields
from mypie.timeutil import KST, trading_day
from mypie.upbit import OrderRejected, UpbitError, make_token
from mypie.vault import VaultError

logging.disable(logging.CRITICAL)
# 테스트 속 봇이 파이 .env의 진짜 텔레그램으로 알림을 보내지 않게 한다 (필요한 테스트는 따로 mock.patch로 바꾼다)
telegram.load = lambda: None


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

    def test_account_validation(self):
        rule, tf, stop = check_account({"strategy": "rsi"})
        self.assertEqual((rule["strategy"], tf, stop), ("rsi", "1d", 0.0))  # 주기를 안 정하면 일봉, 손절선은 안 씀
        for bad in ({"strategy": "rsi", "tf": "7m"}, {"strategy": "rsi", "stop_loss": 0.3}, {"strategy": "rsi", "sell": ["nope"]}):
            with self.assertRaises(VaultError):
                check_account(bad)


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


class Script(Strategy):
    """정해 둔 순서대로 '보유/비보유'를 말하는 시험용 전략."""
    name = label = "script"
    window = 1

    def __init__(self, wants):
        super().__init__()
        self.wants = iter(wants)

    def plan(self, history, today_open):
        want = next(self.wants)
        return DayPlan(buy_at_open=want, sell_at_open=not want)


class StopLossTest(unittest.TestCase):
    def test_parse_and_line(self):
        self.assertEqual(stoploss.parse(None), (0.0, None))              # 안 씀
        self.assertEqual(stoploss.parse("10"), (10.0, None))
        self.assertIsNotNone(stoploss.parse(0.5)[1])                    # 너무 작으면 잡음에도 팔려서 막는다
        self.assertIsNotNone(stoploss.parse(60)[1])
        self.assertIsNotNone(stoploss.parse("x")[1])
        self.assertAlmostEqual(stoploss.line(1000, 10, 10), 90)          # 1개 100원에 샀으면 90원
        self.assertIsNone(stoploss.line(1000, 10, 0))
        self.assertIsNone(stoploss.line(0, 0, 10))

    def test_backtest_cuts_and_waits_for_a_fresh_signal(self):
        bars = [candle("d0", 100, 100, 100, 100), candle("d1", 100, 101, 99, 100),
                candle("d2", 100, 100, 80, 85),                          # 손절선(약 90원) 아래로
                candle("d3", 85, 86, 84, 85), candle("d4", 85, 86, 84, 85), candle("d5", 85, 86, 84, 85)]
        wants = [True, True, True, False, True]                          # d3엔 신호가 아직 켜져 있어도 안 산다
        r = backtest.simulate(Script(wants), bars, 0.0005, warmup=1, detail=True, stop_loss=10)
        sides = [(f["i"], f["side"], f.get("stop_loss", False)) for f in r["detail"]["fills"]]
        self.assertEqual(sides, [(0, "buy", False), (1, "sell", True), (4, "buy", False)])
        self.assertAlmostEqual(r["detail"]["fills"][1]["price"], 100 * 1.0005 * 0.9)
        plain = backtest.simulate(Script(wants), bars, 0.0005, warmup=1, detail=True)
        self.assertEqual([(f["i"], f["side"]) for f in plain["detail"]["fills"]],  # 손절이 없으면 신호대로만 (d4에 판다)
                         [(0, "buy"), (3, "sell"), (4, "buy")])

    def test_higher_strategy_stop_fires_first(self):
        class Trail(Script):
            def plan(self, history, today_open):
                return DayPlan(buy_at_open=next(self.wants), sell_stop=95)  # 전략의 이탈 가격이 손절선보다 위
        bars = [candle("d0", 100, 100, 100, 100), candle("d1", 100, 100, 100, 100), candle("d2", 100, 100, 80, 85)]
        r = backtest.simulate(Trail([True, True]), bars, 0.0, warmup=1, detail=True, stop_loss=10)
        sell = r["detail"]["fills"][-1]
        self.assertEqual((sell["price"], sell.get("stop_loss", False)), (95, False))


class ComboRuleTest(unittest.TestCase):
    """살 때·팔 때 전략 조합 (strategies/combo.py)."""

    def test_rules_are_tidied_like_the_web_page(self):
        self.assertEqual(normalize_rule({"strategy": "rsi"}),                     # 예전 설정: 살 때·팔 때 모두 그 전략
                         {"strategy": "rsi", "buy": ["rsi"], "buy_op": "and", "sell": ["rsi"], "sell_op": "and"})
        rule = normalize_rule({"strategy": "x", "buy": ["rsi", "rsi"], "buy_op": "or", "sell": ["macd", "rsi", "ma_cross"],
                               "sell_op": "or"})
        self.assertEqual(rule, {"strategy": "rsi", "buy": ["rsi"], "buy_op": "and",   # 같은 전략 두 번은 하나로
                                "sell": ["macd", "rsi"], "sell_op": "or"})            # 한쪽에 둘까지
        self.assertEqual(rule_fields(normalize_rule({"strategy": "rsi", "sell": ["rsi"]})), {"strategy": "rsi"})
        self.assertEqual(rule_fields(rule), rule)
        self.assertIsNone(check_rule(rule, STRATEGIES)[1])
        self.assertIsNotNone(check_rule({"strategy": "rsi", "sell": ["nope"]}, STRATEGIES)[1])

    def test_simple_rule_is_the_strategy_itself(self):
        made = make_rule(normalize_rule({"strategy": "rsi"}), lambda n: STRATEGIES[n](None))
        self.assertIs(type(made), STRATEGIES["rsi"])

    def test_combo_trades_like_its_parts(self):
        def run(phase, strategy):
            prices = [100 + 10 * math.sin((i + phase) / 6) for i in range(260)]
            candles = [candle(f"d{i:03d}", p, p * 1.01, p * 0.99, p) for i, p in enumerate(prices)]
            return backtest.simulate(strategy, candles, 0.0005, 150, detail=True)

        # 팔 때 '골든크로스 또는 그냥 들고 있기'는 골든크로스만 보는 것과 같다 (그냥 들고 있기는 팔자고 하지 않음)
        rule = normalize_rule({"strategy": "ma_cross", "sell": ["ma_cross", "buy_and_hold"], "sell_op": "or"})
        combo = make_rule(rule, lambda n: STRATEGIES[n](None))
        self.assertEqual(combo.name, "combo")
        self.assertIn("팔 때", combo.label)
        raw, mixed = run(20, STRATEGIES["ma_cross"](None)), run(20, combo)
        self.assertEqual(mixed["detail"]["fills"], raw["detail"]["fills"])
        self.assertAlmostEqual(mixed["total_return"], raw["total_return"])
        # 시작할 때 이미 '보유' 상태면 조합은 새 매수 신호(안 삼 → 삼)를 기다린다
        raw, mixed = run(8, STRATEGIES["ma_cross"](None)), run(8, make_rule(rule, lambda n: STRATEGIES[n](None)))
        self.assertEqual(raw["detail"]["fills"][0]["i"], 0)
        self.assertGreater(mixed["detail"]["fills"][0]["i"], 0)

    def test_strategy_short_of_data_does_not_force_a_sell(self):
        rule = normalize_rule({"strategy": "rsi", "sell": ["ma_cross", "macd"], "sell_op": "or"})
        plan = make_rule(rule, lambda n: STRATEGIES[n](None)).plan(flat_history(10), 100.0)  # 둘 다 봉이 모자람
        self.assertFalse(plan.sell_at_open)
        self.assertIn("데이터 부족", plan.note)


class TempDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class BookTest(TempDbTest):
    def test_vault_accounts_transfer_and_remove(self):
        b = Book(self.db, "book:paper", 1_000_000)
        b.add_slot("a", "KRW-BTC", "비트코인", "rsi", "240m")
        b.add_slot("b", "KRW-BTC", "비트코인", "macd", "1d")              # 같은 코인도 여러 계좌
        self.assertEqual((b.slot("a")["name"], b.slot("b")["name"]), ("비트코인", "비트코인 2"))
        self.assertEqual((b.slot("a")["strategy"], b.slot("a")["tf"]), ("rsi", "240m"))
        b.transfer("a", 300_000)
        self.assertEqual((b.vault, b.slot("a")["krw"]), (700_000, 300_000))
        with self.assertRaises(VaultError):
            b.transfer("a", 800_000)                                    # 금고보다 많이
        with self.assertRaises(VaultError):
            b.transfer("a", -400_000)                                   # 계좌 원화보다 많이
        ex = PaperExecutor(0.0005)
        b.apply_buy("a", *ex.buy("KRW-BTC", 300_000, 100, {}), "test")
        with self.assertRaises(VaultError):
            b.remove_slot("a")                                          # 코인이 있으면 못 지운다
        trade = b.apply_sell("a", *ex.sell("KRW-BTC", b.slot("a")["coin"], 110, {}), "test")
        self.assertEqual(trade["slot"], "a")
        self.assertEqual(b.vault, 700_000)                              # 판 돈은 계좌에 남는다
        moved = b.remove_slot("a")
        self.assertAlmostEqual(b.vault, 700_000 + moved)                # 지우면 원화는 금고로
        b.deposit(-50_000)                                              # 모의투자 가상 출금
        self.assertAlmostEqual(b.data["contributions"], 950_000)

    def test_buy_sell_round_trip(self):
        book = Book(self.db, "book:paper", 0)
        book.add_slot("a", "KRW-BTC", "BTC", "rsi", "1d")
        book.slot("a")["krw"] = 100_000
        ex = PaperExecutor(0.0005)
        book.apply_buy("a", *ex.buy("KRW-BTC", 100_000, 100, {}), "test")
        trade = book.apply_sell("a", *ex.sell("KRW-BTC", book.slot("a")["coin"], 110, {}), "test")
        self.assertAlmostEqual(book.slot("a")["krw"], 100_000 / 1.0005 * 1.1 * 0.9995)
        self.assertAlmostEqual(trade["profit"], book.slot("a")["krw"] - 100_000)

    def test_live_sync_keeps_outside_coins_in_buy_and_hold_and_deposits_in_vault(self):
        b = Book(self.db, "book:live", 0)
        b.add_slot("a", "KRW-BTC", "비트코인", "volatility_breakout", "1d")
        acc = lambda krw, btc: FakeClient(krw=krw, coins={"BTC": btc, "DOGE": 5.0}).accounts()
        changes = b.sync(acc(50_000, 100.0), {"KRW-BTC": 100}, {"KRW-BTC": "비트코인"})  # DOGE는 계좌가 없어서 그대로
        hold = changes["added"][0]                                      # 원래 있던 BTC는 바이앤홀드 계좌로
        self.assertEqual((b.slot(hold)["name"], b.is_hold(hold), b.slot(hold)["coin"]),
                         ("비트코인 · 원래 있던 코인", True, 100.0))
        self.assertEqual((b.vault, b.slot("a")["coin"]), (50_000, 0.0))
        self.assertAlmostEqual(b.slot(hold)["cost"], 10_000)            # 넘겨받을 때 시세가 원가
        self.assertAlmostEqual(b.data["contributions"], 60_000)
        b.transfer("a", 20_000)
        changes = b.sync(acc(80_000, 100.0), {"KRW-BTC": 100})         # 3만 원 입금 → 금고로 (자동으로 사지 않는다)
        self.assertAlmostEqual(changes["cash"], 30_000)
        self.assertEqual((b.vault, b.slot("a")["krw"]), (60_000, 20_000))
        b.slot("a").update(coin=10.0, cost=1_000.0, avg_price=100.0)    # 봇이 산 10개 (합계 110개)
        b.sync(acc(80_000, 70.0), {"KRW-BTC": 100})                    # 밖에서 40개 팔림 → 바이앤홀드 계좌부터
        self.assertEqual((b.slot(hold)["coin"], b.slot("a")["coin"]), (60.0, 10.0))
        b.sync(acc(10_000, 70.0), {"KRW-BTC": 100})                    # 금고보다 많이 출금 → 계좌 원화에서
        self.assertEqual((b.vault, b.slot("a")["krw"]), (0.0, 10_000))

    def test_old_weight_book_is_migrated(self):
        ledger = {"krw": 0.0, "coin": 0.0, "avg_price": 0.0, "cost": 0.0, "contrib": 0.0, "hold": 0.0, "hold_cash": 0.0}
        old = {"budget": "all", "reserve": 1_000.0, "contributions": 50_000.0, "needs_sync": False, "needs_alloc": False,
               "slots": {"a": {**ledger, "market": "KRW-BTC", "krw": 40_000.0, "coin": 0.1, "cost": 9.0},
                         "x": {**ledger, "market": "KRW-ETH", "coin": 2.0}}}
        defs = {"a": {"id": "a", "market": "KRW-BTC", "strategy": "bollinger", "buy": ["bollinger", "macd"], "buy_op": "or",
                      "sell": ["momentum", "stochastic"], "sell_op": "and", "tf": "240m", "weight": 100.0,
                      "stop_loss": 10, "name": "비트코인"}}
        new = migrate_book(old, defs)
        self.assertEqual((new["vault"], new["synced"]), (1_000.0, True))  # 대기 원화 → 금고
        self.assertFalse({"budget", "reserve", "needs_alloc", "needs_sync"} & set(new))
        a = new["slots"]["a"]
        self.assertEqual((a["name"], a["tf"], a["buy"], a["stop_loss"], a["krw"], a["coin"]),
                         ("비트코인", "240m", ["bollinger", "macd"], 10, 40_000.0, 0.1))  # 원화·코인은 그대로 그 계좌 것
        self.assertEqual(new["slots"]["x"]["strategy"], "buy_and_hold")  # 설정에 없던 줄(코인만 남은 것)은 바이앤홀드


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

    def test_keeps_margin(self):
        client = FakeClient(krw=100_000)
        LiveExecutor(self.db, client, 0.0005).buy("KRW-BTC", 100_000, 100, {})
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

    def factory(self, mode):
        if mode == "live":
            return LiveExecutor(self.db, self.client, 0.0005)
        return PaperExecutor(0.0005)

    def accounts(self, *rows, mode="paper", vault=0.0):
        """금고와 코인 계좌를 바로 만든다. 줄: (id, 종목, 전략, 원화[, {판단 주기·손절선 같은 것}])."""
        slots = {}
        for sid, market, strategy, krw, *extra in rows:
            slots[sid] = {"market": market, "name": market.split("-")[1], "strategy": strategy, "tf": "1d",
                          "krw": float(krw), "coin": 0.0, "avg_price": 0.0, "cost": 0.0,
                          "contrib": float(krw), "hold": 0.0, "hold_cash": float(krw), **(extra[0] if extra else {})}
        total = vault + sum(s["krw"] for s in slots.values())
        self.db.set_state("settings", {"mode": mode})
        self.db.set_state(f"book:{mode}", {"vault": float(vault), "slots": slots, "contributions": total,
                                           "synced": mode == "live"})
        if mode == "live":
            self.client.krw = total                                     # 업비트 실제 원화와 장부가 맞게

    def request(self, n, **r):
        self.db.set_state(f"request:{n:04d}", {"id": str(n), **r})

    def results(self):
        return [(r["ok"], r["message"]) for r in self.db.get_state("request_results") or []]

    def make_bot(self):
        return Bot(self.cfg, self.db, self.factory)

    def go(self, bot, day, steps=1, errors_ok=False, ip="1.1.1.1", hour=None):
        """day 거래일(분봉 계좌가 있으면 hour 봉 키도)에 봇을 steps번 돌린다."""
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

    def eth(self, day, price, today_open=100):
        self.market.set("KRW-ETH", day, today_open, candle("2026-10-20", today_open, today_open, today_open, today_open), price)

    def trades(self):
        return [(t["side"], t["market"]) for t in reversed(self.db.recent_trades())]

    # ---- 모의투자 ----

    def test_old_portfolio_moves_to_vault_on_start(self):
        ledger = {"krw": 0.0, "coin": 0.0, "avg_price": 0.0, "cost": 0.0, "contrib": 0.0, "hold": 0.0, "hold_cash": 0.0}
        self.db.set_state("settings", {"mode": "paper", "budget": 100_000, "slots": [
            {"id": "a", "market": "KRW-BTC", "strategy": "rsi", "tf": "240m", "weight": 100.0},
            {"id": "b", "market": "KRW-ETH", "strategy": "macd", "weight": 0.0}]})
        self.db.set_state("book:paper", {"budget": "paper", "reserve": 5_000.0, "contributions": 1_000_000.0,
                                         "needs_alloc": False, "slots": {"a": {**ledger, "market": "KRW-BTC", "krw": 995_000.0},
                                                                         "b": {**ledger, "market": "KRW-ETH"}}})
        bot = self.make_bot()
        self.assertEqual(self.db.get_state("settings"), {"mode": "paper"})
        self.assertEqual(bot.book.vault, 5_000)                          # 대기 원화 → 금고
        self.assertEqual([(s.id, s.market, s.rule["strategy"], s.tf) for s in bot.slots],
                         [("a", "KRW-BTC", "rsi", "240m"), ("b", "KRW-ETH", "macd", "1d")])
        self.assertEqual(bot.book.slot("a")["krw"], 995_000)             # 줄의 원화는 그대로 그 계좌 것
        self.assertIn("book:paper", self.db.get_state("backup:pre_vault"))  # 예전 장부는 백업
        self.assertEqual(self.make_bot().book.vault, 5_000)               # 다시 켜도 한 번만 옮긴다

    def test_breakout_buy_survives_restart_and_sells_next_day(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 100_000))
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

    def test_two_accounts_trade_separately_and_hold_signal_is_not_sold(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 500_000), ("b", "KRW-ETH", "buy_and_hold", 300_000),
                      vault=200_000)
        bot = self.make_bot()
        self.btc(self.D1, 110)
        self.eth(self.D1, 50, today_open=50)
        self.go(bot, self.D1)                                          # ETH 09시 매수 (30만), BTC 대기
        self.btc(self.D1, 125)
        self.go(bot, self.D1)                                          # BTC 돌파 매수 (50만)
        buys = {t["market"]: t["krw"] for t in self.db.recent_trades()}
        self.assertAlmostEqual(buys["KRW-ETH"], 300_000)
        self.assertAlmostEqual(buys["KRW-BTC"], 500_000)
        self.assertAlmostEqual(bot.book.vault, 200_000)                 # 금고는 그대로
        eth_coin = bot.book.slot("b")["coin"]
        self.btc(self.D2, 130, today_open=130, prev=candle(self.D1, 100, 130, 100, 126))
        self.market.set("KRW-ETH", self.D2, 55, candle(self.D1, 50, 55, 50, 55), 55)
        self.go(bot, self.D2)                                          # 다음 날 09시
        self.assertEqual(self.trades()[-1], ("sell", "KRW-BTC"))        # 변동성 돌파 계좌만 판다
        self.assertEqual(bot.book.slot("b")["coin"], eth_coin)          # '보유 유지' 계좌는 그대로
        self.assertGreater(bot.book.slot("a")["krw"], 0)                # 판 돈은 그 계좌에 남는다

    def test_requests_add_transfer_change_and_sell_then_remove(self):
        self.accounts(vault=1_000_000)
        bot = self.make_bot()
        self.btc(self.D1, 100)
        rule = {"strategy": "rsi", "buy": ["rsi", "macd"], "buy_op": "or", "sell": ["ma_cross"], "sell_op": "and"}
        self.request(1, action="add", market="KRW-BTC", strategy="buy_and_hold", tf="1d", stop_loss=0, amount=300_000)
        self.go(bot, self.D1)                                          # 계좌가 생기고 09시 매수
        sid = bot.slots[0].id
        self.assertEqual(self.trades(), [("buy", "KRW-BTC")])
        self.assertEqual((bot.slots[0].name, bot.book.vault), ("BTC", 700_000))
        self.request(2, action="transfer", slot=sid, direction="in", amount=100_000)
        self.go(bot, self.D1)
        self.assertEqual(len(self.trades()), 1)                         # 들고 있으면 늘어난 원화로 더 사지 않는다
        self.assertEqual((bot.book.vault, bot.book.slot(sid)["krw"]), (600_000, 100_000))
        self.request(3, action="strategy", slot=sid, **rule, tf="1d", stop_loss=10)
        self.go(bot, self.D1)
        s = bot.book.slot(sid)
        self.assertEqual((bot.slots[0].rule, s["stop_loss"]), (rule, 10))
        self.assertGreater(s["coin"], 0)                                # 전략을 바꿔도 코인은 그대로
        self.request(4, action="remove", slot=sid)
        self.go(bot, self.D1)
        self.assertFalse(self.results()[-1][0])                         # 코인이 있으면 그냥은 못 지운다
        self.request(5, action="close", slot=sid)
        self.go(bot, self.D1)                                          # 팔고 빼기: 바로 팔고 지운다
        self.assertEqual(self.trades()[-1], ("sell", "KRW-BTC"))
        self.assertEqual(bot.slots, [])
        self.assertAlmostEqual(bot.book.vault, bot.book.cash())         # 판 돈과 원화는 금고로
        self.request(6, action="add", market="KRW-NOPE", strategy="rsi", tf="1d", amount=0)
        self.go(bot, self.D1)
        self.assertFalse(self.results()[-1][0])                         # 업비트에 없는 종목

    def test_close_can_be_cancelled_before_it_runs(self):
        self.accounts(("a", "KRW-BTC", "buy_and_hold", 100_000))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.go(bot, self.D1)
        self.request(1, action="close", slot="a")
        self.request(2, action="cancel_close", slot="a")                 # 같은 루프에서 차례로 처리된다
        self.go(bot, self.D1)
        self.assertEqual(self.trades(), [("buy", "KRW-BTC")])
        self.assertFalse(bot.slots[0].closing)

    def test_close_with_only_cash_moves_it_to_the_vault_at_once(self):
        self.accounts(("a", "KRW-BTC", "buy_and_hold", 100_000), ("b", "KRW-ETH", "buy_and_hold", 0), vault=5_000)
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.request(1, action="close", slot="a")                        # 코인 없이 원화만: 바로 금고로 빼고 지운다
        self.request(2, action="remove", slot="b")                       # 돈이 없는 계좌(지켜보기): 그냥 지우기
        self.go(bot, self.D1)
        self.assertEqual(self.trades(), [])
        self.assertEqual((bot.slots, bot.book.vault), ([], 105_000))
        self.assertEqual([ok for ok, _ in self.results()], [True, True])
        self.assertIn("뺐어요 · 원화 100,000원은 금고로", self.results()[0][1])
        self.assertIn("ETH 계좌를 지웠어요", self.results()[1][1])

    def test_same_coin_in_two_accounts(self):
        self.accounts(("a", "KRW-BTC", "buy_and_hold", 100_000), ("b", "KRW-BTC", "buy_and_hold", 50_000))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.go(bot, self.D1)
        rows = self.db.recent_trades()
        self.assertEqual(sorted((r["slot"], round(r["krw"])) for r in rows), [("a", 100_000), ("b", 50_000)])

    def test_account_without_money_only_watches(self):
        self.accounts(("a", "KRW-BTC", "buy_and_hold", 100_000), ("b", "KRW-ETH", "buy_and_hold", 0))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.eth(self.D1, 100)
        self.go(bot, self.D1, steps=2)
        self.assertEqual(self.trades(), [("buy", "KRW-BTC")])          # 원화가 없는 계좌는 매수 신호가 나도 안 산다
        state = {x["id"]: x["state_text"] for x in self.db.get_state("status")["slots"]}
        self.assertIn("지켜보기", state["b"])
        self.assertFalse(any("최소 주문" in e["message"] for e in self.db.recent_events(20)))  # 경고도 안 한다

    def test_paper_cash_and_mode_switch(self):
        self.accounts(("a", "KRW-BTC", "rsi", 100_000))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.request(1, action="paper_cash", amount=500_000)
        with mock.patch("mypie.bot.load_keys", return_value=None):
            self.request(2, action="mode", mode="live")
            self.go(bot, self.D1)
        self.assertEqual(bot.book.vault, 500_000)
        self.assertFalse(self.results()[-1][0])                         # 키가 없으면 실거래로 못 바꾼다
        with mock.patch("mypie.bot.load_keys", return_value=("a", "b")), \
             mock.patch("mypie.upbit.UpbitClient", return_value=self.client):
            self.request(3, action="mode", mode="live")
            self.go(bot, self.D1)
        self.assertEqual((bot.executor.mode, bot.book.key, bot.slots), ("live", "book:live", []))
        self.assertAlmostEqual(bot.book.vault, self.client.krw)          # 업비트 원화 전부가 실거래 금고로
        self.assertEqual(self.db.get_state("book:paper")["vault"], 500_000)  # 모의 금고·계좌는 따로 그대로
        self.assertEqual(self.db.get_state("key_info")["expire_at"][:10], "2027-10-21")

    def test_buy_and_sell_rule_is_saved_and_reported(self):
        rule = {"strategy": "rsi", "buy": ["rsi", "macd"], "buy_op": "or", "sell": ["ma_cross"], "sell_op": "and"}
        self.accounts(("a", "KRW-BTC", "rsi", 100_000, rule))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.go(bot, self.D1)
        self.assertEqual((bot.slots[0].rule, bot.slots[0].strategy.name), (rule, "combo"))
        self.assertEqual(self.db.get_state("status")["slots"][0]["rule"], rule)

    def test_key_expiry_alerts_once_a_day_with_renewal_steps(self):
        self.accounts(("a", "KRW-BTC", "rsi", 100_000))
        bot = self.make_bot()
        sent = []
        bot.notifier = sent.append
        expired = upbit.UpbitError("API 키가 만료되었습니다.", 401, "expired_access_key")
        self.assertTrue(network.is_key_error(expired))
        self.assertFalse(network.is_key_error(upbit.UpbitError("잔고 부족", 400, "insufficient_funds")))
        bot._check_key_error(expired)
        bot._check_key_error(expired)                                  # 같은 날 두 번째는 조용히
        self.assertEqual(len(sent), 1)
        self.assertIn("만료", sent[0])
        self.assertIn("python3 -m mypie.keys", sent[0])                 # 갱신 방법까지
        self.assertIn("Open API 관리", sent[0])
        with mock.patch("mypie.network.public_ip", return_value="1.1.1.1"):
            bot._mark_upbit_ok()                                       # 새 키로 다시 통함
        self.assertIn("다시 연결", sent[-1])
        soon = (datetime.now(KST) + timedelta(days=7, hours=1)).isoformat()
        with mock.patch("mypie.bot.load_keys", return_value=("a", "b")), \
             mock.patch("mypie.upbit.UpbitClient.key_expiry", return_value=soon):
            bot._update_key_info(self.D2)                              # 만료 7일 전
        self.assertIn("앞으로 7일", sent[-1])
        gone = (datetime.now(KST) - timedelta(days=1)).isoformat()
        self.db.delete_state("key_alert")
        with mock.patch("mypie.bot.load_keys", return_value=("a", "b")), \
             mock.patch("mypie.upbit.UpbitClient.key_expiry", return_value=gone):
            bot._update_key_info(self.D2)                              # 이미 지났으면 '만료됐어요'
        self.assertIn("만료됐거나", sent[-1])

    def test_stop_loss_sells_and_waits_for_a_fresh_signal(self):
        self.accounts(("a", "KRW-BTC", "buy_and_hold", 100_000, {"stop_loss": 10}))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.go(bot, self.D1)                                          # 09시 매수 (100원)
        self.assertIn("손절선 −10%", self.db.get_state("status")["slots"][0]["state_text"])
        self.btc(self.D1, 89)                                          # 손절선 약 90원 아래로 (산 날이어도 지킨다)
        self.go(bot, self.D1)
        self.assertEqual(self.trades(), [("buy", "KRW-BTC"), ("sell", "KRW-BTC")])
        self.assertIn("손절선", self.db.recent_trades()[0]["reason"])
        self.btc(self.D2, 95)
        self.go(bot, self.D2, steps=2)                                  # 바이앤홀드는 매수 신호가 안 꺼져서 다시 안 산다
        self.assertEqual(len(self.trades()), 2)
        self.assertIn("손절 뒤 쉬는 중", self.db.get_state("status")["slots"][0]["state_text"])

    def test_sold_account_is_not_bought_back_at_the_same_open(self):
        self.accounts(("a", "KRW-BTC", "buy_and_hold", 100_000))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.go(bot, self.D1)                                          # 09시 매수
        both = lambda strategy, history, today_open: DayPlan(buy_at_open=True, sell_at_open=True, note="둘 다")
        self.btc(self.D2, 100)
        with mock.patch.object(STRATEGIES["buy_and_hold"], "plan", both):  # 조합에서 사자·팔자가 같이 켜진 봉
            self.go(bot, self.D2, steps=2)
        self.assertEqual(self.trades(), [("buy", "KRW-BTC"), ("sell", "KRW-BTC")])  # 팔기만 하고 바로 다시 사지 않는다

    def test_status_shows_buy_and_hold_comparison(self):
        self.accounts(("a", "KRW-BTC", "rsi", 100_000))
        bot = self.make_bot()
        self.btc(self.D1, 100)
        self.go(bot, self.D1)                                          # 100원일 때 계좌에 돈이 들어옴
        self.btc(self.D1, 110)
        self.go(bot, self.D1)
        x = self.db.get_state("status")["slots"][0]
        self.assertAlmostEqual(x["hold_return_pct"], 10.0)            # 그냥 들고 있었다면 +10%
        self.assertAlmostEqual(x["hold_pnl"], 10_000)
        self.assertAlmostEqual(x["return_pct"], 0.0)                  # RSI는 아직 안 샀다
        self.assertEqual(self.db.get_state("status")["account"]["vault"], 0.0)

    def test_hourly_account_plans_every_hour_and_daily_account_is_untouched(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 100_000),
                      ("h", "KRW-ETH", "volatility_breakout", 100_000, {"tf": "60m"}))
        bot = self.make_bot()
        self.btc(self.D1, 110)                                           # 일봉 계좌: 목표가 120 대기
        h1, h2 = "2026-10-21T03:00", "2026-10-21T04:00"
        eth_prev = candle("2026-10-21T02:00", 90, 120, 80, 100)           # 1시간봉 목표가 120
        self.market.set("KRW-ETH", h1, 100, eth_prev, 125, tf="60m")
        self.go(bot, self.D1, hour=h1)                                    # 1시간봉 계좌만 돌파 매수
        self.assertEqual(self.trades(), [("buy", "KRW-ETH")])
        self.assertIn("다음 봉 시작", self.db.get_state("status")["slots"][1]["state_text"])
        self.market.set("KRW-ETH", h2, 130, candle(h1, 100, 130, 100, 126), 130, tf="60m")
        self.go(bot, self.D1, hour=h2)                                    # 다음 봉 시작 → 매도 (일봉은 그대로)
        self.assertEqual(self.trades(), [("buy", "KRW-ETH"), ("sell", "KRW-ETH")])
        self.assertEqual(bot.slots[0].plan_day, self.D1)
        self.assertEqual(bot.slots[1].plan_day, h2)

    # ---- 실거래 ----

    def test_rejected_order_is_retried_without_pausing(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 50_000), mode="live", vault=950_000)
        self.client.reject = "bid"
        bot = self.make_bot()
        self.btc(self.D1, 125)
        self.go(bot, self.D1, steps=2, errors_ok=True)
        self.assertEqual(self.client.attempts, 2)                       # 멈추지 않고 다시 시도
        self.client.reject = None
        self.go(bot, self.D1)
        self.assertEqual(self.client.orders[-1]["price"], str(int(50_000 / 1.0005)))  # 그 계좌 원화만큼만
        self.assertEqual(self.db.recent_trades()[0]["mode"], "live")

    def test_power_loss_after_order_is_reconciled_and_trading_continues(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 100_000), mode="live")
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
        self.assertEqual(len(rebooted.slots), 1)                        # 장부가 맞으니 바이앤홀드 계좌가 안 생긴다

    def test_power_loss_before_order_reached_upbit(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 100_000), mode="live")
        self.db.set_state(LiveExecutor.PENDING_KEY, {"identifier": "mypie-lost", "side": "bid", "market": "KRW-BTC",
                                                     "slot": "a", "day": self.D1, "at": "2026-10-21 10:00:00"})
        bot = self.make_bot()
        self.btc(self.D1, 125)
        self.go(bot, self.D1)                                          # 확인 → 안 들어갔음 → 바로 정상 매수
        self.assertEqual(self.client.attempts, 1)
        self.assertGreater(bot.book.slot("a")["coin"], 0)

    def test_coins_already_in_the_account_go_to_buy_and_hold_not_the_strategy(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 0), mode="live")
        self.client.krw, self.client.coins = 0.0, {"BTC": 100.0}        # 계좌에 원래 BTC만 있음
        bot = self.make_bot()
        self.btc(self.D1, 110)
        self.go(bot, self.D1)
        self.assertEqual(self.trades(), [])                             # 변동성 돌파 계좌 것이 아니라서 팔지 않는다
        hold = next(s for s in bot.slots if s.id != "a")
        self.assertEqual((hold.name, hold.rule["strategy"], bot.book.slot(hold.id)["coin"]),
                         ("BTC · 원래 있던 코인", "buy_and_hold", 100.0))

    def test_deposit_goes_to_vault_and_is_not_bought(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 100_000), mode="live", vault=100_000)
        bot = self.make_bot()
        self.btc(self.D1, 125)
        self.go(bot, self.D1)
        self.assertEqual(self.client.orders[0]["price"], str(math.floor(100_000 / 1.0005)))  # 그 계좌 원화만큼
        self.client.krw += 50_000                                       # 보유 중에 입금
        self.go(bot, self.D1)
        self.assertEqual(len(self.client.orders), 1)                    # 알아서 사지 않는다 (계좌로 옮겨야 산다)
        self.assertAlmostEqual(bot.book.vault, 150_000, delta=1)
        self.assertAlmostEqual(bot.book.data["contributions"], 250_000, delta=1)

    def test_whole_account_money_keeps_a_small_margin(self):
        self.accounts(("a", "KRW-BTC", "volatility_breakout", 100_000), mode="live")
        bot = self.make_bot()
        self.btc(self.D1, 125)
        self.go(bot, self.D1)                                          # 계좌 원화가 업비트 원화 전부면 0.5% 남기고 산다
        self.assertEqual(self.client.orders[0]["price"], str(math.floor(100_000 * 0.995 / 1.0005)))

    def test_warns_when_public_ip_changes(self):
        self.accounts(("a", "KRW-BTC", "rsi", 100_000), mode="live")
        bot = self.make_bot()
        self.btc(self.D1, 110)
        self.go(bot, self.D1, ip="1.1.1.1")                            # 업비트가 이 IP를 받아 줌
        self.assertEqual(self.db.get_state("network")["upbit_ok_ip"], "1.1.1.1")
        bot.last_ip_check = 0
        with mock.patch("mypie.bot.load_keys", return_value=("a", "b")):
            self.go(bot, self.D1, ip="2.2.2.2")
        self.assertTrue(any("IP가 바뀐" in e["message"] for e in self.db.recent_events(10)))
        # 실거래는 루프마다 업비트 잔고를 확인해서, 업비트가 새 IP도 받아 주면 경고 표시는 바로 사라진다
        self.assertEqual(self.db.get_state("network")["upbit_ok_ip"], "2.2.2.2")
        self.assertFalse(self.db.get_state("status")["network"]["ip_changed"])

    def test_live_without_keys_falls_back_to_paper(self):
        self.accounts(("a", "KRW-BTC", "rsi", 100_000), mode="live")
        with mock.patch("mypie.bot.load_keys", return_value=None):
            bot = Bot(self.cfg, self.db)
        self.assertEqual(bot.executor.mode, "paper")


class WebTest(TempDbTest):
    def setUp(self):
        super().setUp()
        self.db.set_state(pin.STATE_KEY, pin.make("2468"))
        web._pin_failures.clear()
        self.request = {"action": "add", "market": "KRW-BTC", "strategy": "rsi", "tf": "240m", "amount": 100_000}

    def tearDown(self):
        web._pin_failures.clear()
        super().tearDown()

    def test_pin_verify(self):
        stored = pin.make("1357")
        self.assertTrue(pin.verify("1357", stored))
        self.assertFalse(pin.verify("1358", stored))

    def test_control_needs_right_pin(self):
        self.assertEqual(web.request_coin(self.path, {**self.request, "pin": "0000"})[0], 403)
        self.assertEqual(web.request_coin(self.path, {**self.request, "pin": "2468"})[0], 200)
        queued = self.db.states("request:")[0][1]
        self.assertEqual((queued["action"], queued["market"], queued["tf"], queued["amount"]), ("add", "KRW-BTC", "240m", 100_000))

    def test_locks_after_too_many_failures(self):
        for _ in range(web.MAX_PIN_FAILURES):
            web.request_coin(self.path, {**self.request, "pin": "0000"})
        self.assertEqual(web.request_coin(self.path, {**self.request, "pin": "2468"})[0], 429)

    def test_live_needs_keys_and_confirmation(self):
        live = {"action": "mode", "mode": "live", "pin": "2468"}
        with mock.patch("mypie.web.load_keys", return_value=None):
            self.assertEqual(web.request_coin(self.path, {**live, "confirm_live": True})[0], 400)
        with mock.patch("mypie.web.load_keys", return_value=("a", "b")):
            self.assertEqual(web.request_coin(self.path, live)[0], 400)           # '실제 돈' 체크 없이
            self.assertEqual(web.request_coin(self.path, {**live, "confirm_live": True})[0], 200)

    def test_rejects_bad_requests(self):
        ok = {**self.request, "pin": "2468"}
        self.assertEqual(web.request_coin(self.path, {**ok, "action": "nope"})[0], 400)
        self.assertEqual(web.request_coin(self.path, {**ok, "market": "BTC"})[0], 400)
        self.assertEqual(web.request_coin(self.path, {**ok, "tf": "7m"})[0], 400)
        self.assertEqual(web.request_coin(self.path, {**ok, "stop_loss": 0.2})[0], 400)
        self.assertEqual(web.request_coin(self.path, {**ok, "sell": ["nope"]})[0], 400)
        self.assertEqual(web.request_coin(self.path, {"action": "transfer", "slot": "a", "amount": 0, "pin": "2468"})[0], 400)
        self.assertEqual(self.db.states("request:"), [])

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

    def test_coin_chart_picks_candles_by_range(self):
        calls = []

        def fake_candles(market, tf, count, to=None):  # 최신순, to가 있으면 그 전 것만
            calls.append((tf, count, to))
            start = datetime(2026, 10, 22) - timedelta(days=0 if to is None else (datetime(2026, 10, 22) - datetime.fromisoformat(to)).days)
            first = 0 if to is None else 1
            return [candle((start - timedelta(days=i)).strftime("%Y-%m-%d"), 1, 2, 0.5, 1.5) for i in range(first, first + count)]

        web._chart_cache.clear()
        with mock.patch("mypie.upbit.get_candles", side_effect=fake_candles), mock.patch("time.sleep"):
            chart = web.build_coin_chart(self.path, "KRW-BTC", "1y")
            self.assertEqual((chart["tf"], len(chart["candles"])), ("1d", 365))           # 1년 = 일봉 365개
            self.assertEqual([c[1] for c in calls], [200, 165])                         # 200개씩 거슬러 받음
            self.assertLess(chart["candles"][0]["day"], chart["candles"][-1]["day"])   # 오래된 것 → 최신
            self.assertEqual(web.build_coin_chart(self.path, "KRW-BTC", "1h")["tf"], "1m")
            self.assertEqual(web.build_coin_chart(self.path, "KRW-BTC", "5y")["tf"], "1w")
            self.assertIn("message", web.build_coin_chart(self.path, "KRW-BTC", "2y"))
        web._chart_cache.clear()

    def test_power_is_estimated_from_cpu_use(self):
        p = web._power("Raspberry Pi 3 Model B Rev 1.2", 0.5)
        self.assertAlmostEqual(p["power_w"], 1.4 + (3.7 - 1.4) * 0.5)
        self.assertEqual(p["power_w_recommended"], 12.5)
        self.assertAlmostEqual(web._power("Raspberry Pi 3 Model B Plus Rev 1.3", 0.0)["power_w"], 1.9)  # 3B+는 따로
        self.assertEqual(web._power("Some PC", 0.5), {})                 # 모르는 기기는 보여주지 않는다

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

        # 살 때·팔 때 조합: 맨 위에 '조합' 줄, 순위는 그 전략들 사이에 넣었을 때
        query = {"strategy": ["rsi"], "buy": ["rsi,macd"], "buy_op": ["or"], "sell": ["ma_cross"]}
        rule = web._query_rule(query, STRATEGIES)
        self.assertEqual((rule["buy"], rule["buy_op"], rule["sell"]), (["rsi", "macd"], "or", ["ma_cross"]))
        self.assertIsNone(web._query_rule({"strategy": ["rsi"]}, STRATEGIES))
        self.assertIsNone(web._query_rule({**query, "sell": ["nope"]}, STRATEGIES))
        sim = web.build_simulation(self.path, cfg, "KRW-ETH", "1d", 90, "rsi", rule)
        top = sim["results"][0]
        self.assertTrue(top["combo"])
        self.assertEqual((sim["detail"]["name"], len(sim["results"])), ("combo", len(STRATEGIES) + 1))
        self.assertTrue(1 <= top["rank"] <= len(STRATEGIES) + 1)
        self.assertEqual(sum(not r.get("combo") for r in sim["results"]), len(STRATEGIES))
        simple = web.build_simulation(self.path, cfg, "KRW-ETH", "1d", 90, "rsi", normalize_rule({"strategy": "rsi"}))
        self.assertEqual((simple["detail"]["name"], len(simple["results"])), ("rsi", len(STRATEGIES)))

        # 손절선: 모든 전략에 같은 손절선. 비교선 '그냥 들고 있었다면'은 손절 없이
        self.assertEqual((web._query_stop({"stop": ["10"]}), web._query_stop({"stop": ["0.2"]}), web._query_stop({})), (10, 0, 0))
        cut = web.build_simulation(self.path, cfg, "KRW-ETH", "1d", 90, "rsi", None, 10)
        plain_hold = next(r for r in simple["results"] if r["name"] == "buy_and_hold")["total_return"]
        self.assertEqual(cut["stop_loss"], 10)
        self.assertAlmostEqual(cut["hold"]["total_return"], plain_hold)
        self.assertEqual(simple["hold"]["total_return"], plain_hold)


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
        token = auth.create_session(self.db, 24, {"ip": "1.2.3.4"})
        self.assertTrue(auth.valid_session(self.db, token, 24))
        self.assertFalse(auth.valid_session(self.db, "nope", 24))
        with mock.patch("time.time", return_value=time.time() + 25 * 3600):
            self.assertFalse(auth.valid_session(self.db, token, 24))      # 24시간이 지나면 다시 묻는다
        with mock.patch("time.time", return_value=time.time() + 2 * 3600):
            self.assertFalse(auth.valid_session(self.db, token, 1))      # 설정을 줄이면 예전 로그인도 거기에 맞춘다
        auth.end_session(self.db, token)
        self.assertFalse(auth.valid_session(self.db, token, 24))
        sessions = self.db.get_state(auth.SESSIONS_KEY) or {}            # 30일짜리였던 예전 로그인 (시작 시각 없음)
        sessions[auth._hash("old")] = {"expires": time.time() + 29 * 86400, "created": "2026-10-21 09:00:00"}
        self.db.set_state(auth.SESSIONS_KEY, sessions)
        self.assertFalse(auth.valid_session(self.db, "old", 24))

    def test_trusted_ips_expire(self):
        auth.trust_ip(self.db, "1.2.3.4", 24)
        self.assertTrue(auth.trusted_ip(self.db, "1.2.3.4"))
        self.assertFalse(auth.trusted_ip(self.db, "1.2.3.5"))            # 다른 IP는 아니다
        with mock.patch("time.time", return_value=time.time() + 25 * 3600):
            self.assertFalse(auth.trusted_ip(self.db, "1.2.3.4"))        # 하루가 지나면 다시 묻는다
        auth.untrust_ip(self.db, "1.2.3.4")
        self.assertFalse(auth.trusted_ip(self.db, "1.2.3.4"))


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
              "account": {"equity": 1_012_345, "return_pct": 1.23, "contributions": 1_000_000, "vault": 0},
              "slots": [{"market": "KRW-BTC", "tf_label": "일봉", "book": {"krw": 0}, "holding": True, "price": 110_000_000,
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
        self.assertIn("주식 봇이 아직", reports.command_reply("/주식", self.db, "/no/such.db"))

    def test_morning_report_once_per_day(self):
        cfg = {section: dict(values) for section, values in DEFAULTS.items()}
        bot = Bot(cfg, self.db, lambda mode: PaperExecutor(0.0005))
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
        self.assertIn("SameSite=Lax", cookie)                              # 다른 앱의 링크로 열어도 쿠키가 붙게
        self.assertIn("Max-Age=86400", cookie)                             # 쿠키도 최대 24시간
        session = cookie.split(";")[0]
        self.assertEqual(self.request("/api/status", cookie=session)[0], 200)
        self.assertEqual(self.request("/api/status")[0], 200)              # 같은 IP는 쿠키 없이도 하루 동안
        self.db.set_state(auth.TRUSTED_KEY, {"127.0.0.1": 0})              # 하루가 지나면
        self.assertEqual(self.request("/api/control", {}, cookie=None)[0], 401)
        self.assertEqual(self.request("/api/status", cookie=session)[0], 200)  # 쿠키는 그대로 30일
        self.request("/api/logout", {}, cookie=session)
        self.assertEqual(self.request("/api/status", cookie=session)[0], 401)

    def test_logout_also_forgets_the_ip(self):
        auth.trust_ip(self.db, "127.0.0.1", 24)
        self.assertEqual(self.request("/api/status")[0], 200)
        self.request("/api/logout", {})
        self.assertEqual(self.request("/api/status")[0], 401)

    def test_no_login_when_telegram_not_connected(self):
        with mock.patch("mypie.telegram.load", return_value=None):
            self.assertEqual(self.request("/api/status")[0], 200)


class SshWatchTest(unittest.TestCase):
    def test_alerts_outside_logins_and_sums_up_failures_twice_a_day(self):
        sent, now = [], [datetime(2026, 10, 22, 9, 0, tzinfo=KST)]
        watch = sshwatch.SshWatch(sent.append, now=lambda: now[0])
        watch.handle("Accepted publickey for jounghyun from 192.168.100.195 port 4933 ssh2: ED25519 SHA256:x")
        self.assertEqual(sent, [])                                       # 집 안 PC 로그인은 조용히
        watch.handle("Accepted password for jounghyun from 45.67.89.10 port 5000 ssh2")
        self.assertIn("45.67.89.10", sent[-1])                           # 집 밖 로그인은 바로
        self.assertIn("비밀번호", sent[-1])
        for port in (40000, 40001, 40002):
            watch.handle(f"Invalid user admin from 91.200.12.34 port {port}")
            watch.handle(f"Failed password for invalid user admin from 91.200.12.34 port {port} ssh2")  # 같은 접속은 한 번
        watch.handle("Failed password for root from 192.168.100.7 port 5001 ssh2")
        self.assertEqual(len(sent), 1)                                   # 실패는 바로 알리지 않는다
        self.assertFalse(watch.summarize())                              # 아직 오전
        now[0] = datetime(2026, 10, 22, 12, 0, 5, tzinfo=KST)
        self.assertTrue(watch.summarize())                               # 낮 12시: 오전치를 한 번에
        self.assertIn("10/22 00:00~12:00 · 총 4번", sent[-1])
        self.assertIn("91.200.12.34 3번 (admin)", sent[-1])
        self.assertIn("192.168.100.7 (집 안) 1번 (root)", sent[-1])
        now[0] = datetime(2026, 10, 23, 0, 0, 5, tzinfo=KST)
        watch.summarize()
        self.assertEqual(len(sent), 2)                                   # 실패가 없던 반나절은 안 보낸다
        late = datetime(2026, 10, 22, 23, 50, tzinfo=KST)                # 밤 12시 전 기록을 늦게 읽어도 그 반나절로
        watch.handle("Failed password for pi from 45.67.89.10 port 6000 ssh2", late)
        self.assertTrue(watch.summarize())
        self.assertIn("10/22 12:00~24:00 · 총 1번", sent[-1])


if __name__ == "__main__":
    unittest.main()
