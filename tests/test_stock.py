"""주식 봇 테스트: 장 시간 규칙, 금고 장부, 모의·실거래 흐름 (가짜 토스증권으로)."""

import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from mypie import krx, pin, stockbot, telegram, toss, web
from mypie.config import DEFAULTS
from mypie.db import Database
from mypie.stockbot import StockBot
from mypie.stockbroker import LiveStockExecutor, PaperStockExecutor
from mypie.strategies import STOCK_STRATEGIES
from mypie.strategies.base import DayPlan
from mypie.timeutil import KST
from mypie.vault import VaultBook, VaultError

logging.disable(logging.CRITICAL)
telegram.load = lambda: None  # 테스트가 진짜 텔레그램으로 알림을 보내지 않게

DAY1, DAY2 = "2026-10-22", "2026-10-23"  # 목요일, 금요일


def at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=KST)


def candle(day, o, h, l, c):
    return {"day": day, "open": o, "high": h, "low": l, "close": c, "volume": 1000}


class FakeToss:
    """토스증권 대신 쓰는 가짜 거래소. 주문은 지금 가격에 바로 다 체결된다."""

    FEE, TAX = 0.00015, 0.002

    def __init__(self, cash=1_000_000.0, holdings=None):
        self.cash_ = cash
        self.hold = dict(holdings or {})
        self.quotes_, self.candles_ = {}, {}
        self.orders, self.placed = {}, []
        self.fill_ratio = 1.0
        self.reject = False
        self.closed = set()  # 장이 안 열리는 날
        self.nxt, self.regular_, self.regular_calls = set(), {}, []  # 넥스트레이드 종목, 정규장 일봉, 받아 간 날

    def set(self, symbol, day, price, today_open=None, prev=None):
        """오늘(day) 시세와, 어제까지 일봉 + 오늘 진행 중인 일봉."""
        today_open = today_open or price
        prev = prev or [candle("2026-10-21", 100, 110, 90, 100)]
        self.quotes_[symbol] = {"price": price, "open": today_open, "prev_close": prev[0]["close"]}
        self.candles_[symbol] = [candle(day, today_open, max(today_open, price), min(today_open, price), price)] + prev

    def quotes(self, symbols):
        return {s: dict(self.quotes_[s]) for s in symbols if s in self.quotes_}

    def daily_candles(self, symbol, count, before=None):
        rows = self.candles_.get(symbol, [])
        if before:
            rows = [c for c in rows if c["day"] < before]
        return rows[:count]

    def nxt_supported(self, symbols):
        return {s: s in self.nxt for s in symbols}

    def regular_day(self, symbol, day):
        """정규장만의 일봉: 따로 정해 두지 않았으면 일봉과 같다 (NXT가 아닌 종목처럼)."""
        self.regular_calls.append((symbol, day))
        return self.regular_.get((symbol, day)) or next((c for c in self.candles_.get(symbol, []) if c["day"] == day), None)

    def cash(self):
        return self.cash_

    def holdings(self):
        return {s: {"qty": q, "avg_price": 0.0, "name": s} for s, q in self.hold.items() if q}

    def place_order(self, symbol, side, qty, price, client_id):
        if self.reject:
            raise toss.OrderRejected("주문 거절 (테스트)", status=400)
        oid = f"o{len(self.placed) + 1}"
        self.placed.append((symbol, side, qty, price))
        filled = int(qty * self.fill_ratio)
        p = self.quotes_[symbol]["price"]
        value = filled * p
        fee, tax = value * self.FEE, value * self.TAX if side == "sell" else 0.0
        if side == "buy":
            self.cash_ -= value + fee
            self.hold[symbol] = self.hold.get(symbol, 0) + filled
        else:
            self.cash_ += value - fee - tax
            self.hold[symbol] -= filled
        status = "filled" if filled == qty else "partial"
        self.orders[oid] = {"id": oid, "client_id": client_id, "status": status, "filled_qty": filled,
                            "avg_price": p if filled else 0.0, "fee": fee, "tax": tax}
        return oid

    def order(self, oid):
        return dict(self.orders[oid])

    def find_order(self, client_id, symbol):
        return next((dict(o) for o in self.orders.values() if o["client_id"] == client_id), None)

    def cancel(self, oid):
        if self.orders[oid]["status"] == "partial":
            self.orders[oid]["status"] = "cancelled"

    def is_trading_day(self, day):
        return day not in self.closed

    def stocks(self):
        return [{"symbol": "005930", "name": "삼성전자", "market": "KOSPI"},
                {"symbol": "000660", "name": "SK하이닉스", "market": "KOSPI"}]


class KrxTest(unittest.TestCase):
    def test_sessions(self):
        self.assertEqual(krx.session(at(DAY1, "08:59")), "pre")
        self.assertEqual(krx.session(at(DAY1, "09:00")), "open")
        self.assertEqual(krx.session(at(DAY1, "15:20")), "closing")    # 마감 동시호가부터는 새 주문 안 냄
        self.assertEqual(krx.session(at(DAY1, "15:30")), "after")
        self.assertEqual(krx.session(at("2026-10-24", "10:00")), "closed")  # 토요일
        self.assertEqual(krx.session(at("2026-12-25", "10:00")), "closed")  # 휴장일
        self.assertEqual(krx.session(at(DAY1, "10:00"), {DAY1}), "closed")   # 임시 휴장일

    def test_next_open_skips_weekend_and_holidays(self):
        self.assertEqual(krx.next_open(at(DAY1, "08:00")), at(DAY1, "09:00"))
        self.assertEqual(krx.next_open(at(DAY2, "16:00")), at("2026-10-26", "09:00"))     # 금 → 월
        self.assertEqual(krx.next_open(at("2026-12-24", "16:00")), at("2026-12-28", "09:00"))  # 성탄절·주말

    def test_ticks(self):
        self.assertEqual(krx.tick_size(1_999), 1)
        self.assertEqual(krx.tick_size(55_000), 100)
        self.assertEqual(krx.round_tick(55_123, up=True), 55_200)
        self.assertEqual(krx.round_tick(55_123, up=False), 55_100)
        self.assertEqual(krx.round_tick(700_000, up=True), 700_000)


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "stock.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class VaultTest(TempDb):
    def book(self, cash=1_000_000):
        b = VaultBook(self.db, "book:paper", cash)
        b.add_slot("a", "005930", "삼성전자", "buy_and_hold")
        return b

    def test_transfers_and_errors(self):
        b = self.book()
        b.transfer("a", 300_000)
        self.assertEqual((b.vault, b.slot("a")["cash"]), (700_000, 300_000))
        with self.assertRaises(VaultError):
            b.transfer("a", 800_000)                                    # 금고보다 많이
        with self.assertRaises(VaultError):
            b.transfer("a", -400_000)                                   # 종목 계좌 잔액보다 많이
        b.transfer("a", -100_000)
        self.assertEqual((b.vault, b.slot("a")["cash"], b.slot("a")["contrib"]), (800_000, 200_000, 200_000))

    def test_sell_money_stays_in_slot_and_slot_with_shares_cannot_be_removed(self):
        b = self.book()
        b.transfer("a", 100_100)
        b.apply_buy("a", 10, 10_000, 15, "test")
        self.assertEqual((b.slot("a")["qty"], b.slot("a")["cash"]), (10, 85))
        with self.assertRaises(VaultError):
            b.remove_slot("a")
        trade = b.apply_sell("a", 10, 11_000, 16.5, 220, "test")
        self.assertAlmostEqual(trade["profit"], 110_000 - 16.5 - 220 - 100_015)
        self.assertEqual(b.vault, 899_900)                             # 판 돈은 금고로 가지 않는다
        self.assertAlmostEqual(b.slot("a")["cash"], 109_848.5)
        self.assertAlmostEqual(b.remove_slot("a"), 109_848.5)           # 주식이 없으면 지우고 잔액은 금고로
        self.assertAlmostEqual(b.vault, 1_009_748.5)

    def test_live_sync_takes_cash_and_existing_shares(self):
        b = VaultBook(self.db, "book:live", 0)
        b.add_slot("a", "005930", "삼성전자", "buy_and_hold")
        changes = b.sync(500_000, {"005930": {"qty": 3}, "035720": {"qty": 7}}, {"005930": 60_000})
        self.assertEqual((b.vault, b.slot("a")["qty"], b.slot("a")["cost"]), (500_000, 3, 180_000))
        self.assertEqual(changes["shares"], {"005930": 3})               # 다른 종목(035720)은 건드리지 않는다
        self.assertAlmostEqual(b.data["contributions"], 680_000)

        b.transfer("a", 100_000)
        b.sync(600_000, {"005930": {"qty": 3}}, {"005930": 60_000})     # 10만 원 입금
        self.assertEqual((b.vault, b.slot("a")["cash"]), (500_000, 100_000))
        b.sync(450_000, {"005930": {"qty": 3}}, {"005930": 60_000})     # 15만 원 출금 → 금고에서
        self.assertEqual(b.vault, 350_000)
        b.sync(50_000, {"005930": {"qty": 3}}, {"005930": 60_000})      # 금고보다 많이 출금 → 종목 잔액에서
        self.assertEqual((b.vault, b.slot("a")["cash"]), (0.0, 50_000))
        b.sync(50_030, {"005930": {"qty": 1}}, {"005930": 61_000})      # 밖에서 2주 팔림, 30원 오차
        self.assertEqual(b.slot("a")["qty"], 1)
        self.assertAlmostEqual(b.slot("a")["cost"], 60_000)

    def test_same_stock_in_several_accounts_and_outside_shares_go_to_buy_and_hold(self):
        b = VaultBook(self.db, "book:live", 0)
        b.add_slot("a", "005930", "삼성전자", "supertrend")
        b.add_slot("b", "005930", "삼성전자", "ma_alignment")
        self.assertEqual((b.slot("a")["name"], b.slot("b")["name"]), ("삼성전자", "삼성전자 2"))
        b.slot("a").update(qty=4, cost=240_000, avg_price=60_000)        # 봇이 산 주식
        changes = b.sync(100_000, {"005930": {"qty": 7, "name": "삼성전자"}}, {"005930": 60_000})
        hold = changes["added"][0]                                       # 봇이 모르는 3주는 바이앤홀드 계좌로
        self.assertEqual((b.slot(hold)["name"], b.is_hold(hold)), ("삼성전자 · 원래 있던 주식", True))
        self.assertEqual([b.slot(x)["qty"] for x in ("a", "b", hold)], [4, 0, 3])
        self.assertEqual(changes["shares"], {"005930": 3})
        self.assertEqual(b.sync(100_000, {"005930": {"qty": 7}}, {"005930": 60_000})["added"], [])  # 다시 안 만든다
        b.sync(100_000, {"005930": {"qty": 2}}, {"005930": 60_000})     # 밖에서 5주 팔림
        self.assertEqual([b.slot(x)["qty"] for x in ("a", "b", hold)], [2, 0, 0])  # 바이앤홀드 3주부터, 나머지는 a에서
        self.assertAlmostEqual(b.slot("a")["cost"], 120_000)


class StockBotTest(TempDb):
    SYMBOL = "000660"

    def setUp(self):
        super().setUp()
        self.cfg = {section: dict(values) for section, values in DEFAULTS.items()}
        self.cfg["general"]["db_path"] = str(Path(self.tmp.name) / "none.db")
        self.toss = FakeToss()
        self.client = self.toss

    def make_bot(self, mode="paper"):
        self.db.set_state("settings", {"mode": mode})

        def executor(m):
            if m == "live":
                return LiveStockExecutor(self.db, self.toss, 0.00015, 0.002, wait_sec=0, sleep=lambda s: None)
            return PaperStockExecutor(0.00015, 0.002)
        return StockBot(self.cfg, self.db, client_factory=lambda: self.client, executor_factory=executor)

    def request(self, n, **r):
        self.db.set_state(f"request:{n:04d}", {"id": n, **r})

    def results(self):
        return [(r["ok"], r["message"]) for r in self.db.get_state("request_results") or []]

    def trades(self):
        return [(t["side"], t["market"], t["volume"]) for t in reversed(self.db.recent_trades())]

    def slot(self, bot):
        return next(iter(bot.book.data["slots"].values()))

    def test_no_keys_waits_without_crashing(self):
        self.client = None
        bot = self.make_bot()
        bot.step(at(DAY1, "10:00"))
        self.assertFalse(self.db.get_state("status")["keys"])

    def test_paper_buys_only_on_signal_with_slot_money(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=350_000)
        bot.step(at(DAY1, "08:30"))                                      # 장 전: 계좌만 만든다
        self.assertEqual(self.trades(), [])
        self.assertEqual(bot.book.vault, 650_000)
        bot.step(at(DAY1, "09:01"))                                      # 장 시작: 매수 신호 → 종목 잔액으로만 산다
        self.assertEqual(self.trades(), [("buy", self.SYMBOL, 3)])
        s = self.slot(bot)
        self.assertAlmostEqual(s["cash"], 350_000 - 300_000 * 1.00015)
        self.assertEqual(bot.book.vault, 650_000)

        sid = next(iter(bot.book.data["slots"]))
        self.request(2, action="transfer", slot=sid, amount=200_000, direction="in")
        bot.step(at(DAY1, "10:00"))
        bot.step(at(DAY2, "09:01"))                                      # 보유 중이라 늘어난 잔액으로 더 사지 않는다
        self.assertEqual(len(self.trades()), 1)
        self.request(3, action="remove", slot=sid)
        bot.step(at(DAY2, "09:02"))
        self.assertFalse(self.results()[-1][0])                          # 주식이 있어서 못 지움
        self.request(4, action="strategy", slot=sid, strategy="ma_cross")
        bot.step(at(DAY2, "09:03"))
        self.assertEqual(self.slot(bot)["qty"], 3)                       # 전략을 바꿔도 주식은 그대로
        self.assertEqual(self.slot(bot)["strategy"], "ma_cross")
        self.request(5, action="strategy", slot=sid, strategy="rsi", buy=["rsi", "new_high_52w"], buy_op="or",
                     sell=["ma_cross"], sell_op="and")                   # 살 때·팔 때 조합 (주식 전용 전략도)
        bot.step(at(DAY2, "09:04"))
        s = self.slot(bot)
        self.assertEqual((s["buy"], s["buy_op"], s["sell"], s["qty"]), (["rsi", "new_high_52w"], "or", ["ma_cross"], 3))
        self.assertEqual(self.db.get_state("status")["slots"][0]["rule"]["sell"], ["ma_cross"])
        self.request(6, action="strategy", slot=sid, strategy="ma_cross")  # 하나로 되돌리면 조합 칸은 지운다
        bot.step(at(DAY2, "09:05"))
        self.assertNotIn("buy", self.slot(bot))

    def test_close_sells_first_then_removes_the_account(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=350_000)
        self.request(2, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="rsi", amount=0)
        bot.step(at(DAY1, "08:30"))
        empty = next(k for k, v in bot.book.data["slots"].items() if v["name"] == "SK하이닉스 2")
        self.request(3, action="remove", slot=empty)                     # 0원 계좌(지켜보기)는 그냥 지운다
        bot.step(at(DAY1, "08:31"))
        self.assertEqual(len(bot.book.data["slots"]), 1)
        bot.step(at(DAY1, "09:01"))                                      # 3주 매수
        sid = next(iter(bot.book.data["slots"]))
        self.request(4, action="close", slot=sid)
        bot.step(at(DAY1, "15:40"))                                      # 장 마감 뒤: 표시만 해 두고
        self.assertTrue(self.slot(bot)["closing"])
        self.assertIn("다음 장 시작(09:00)에 팔고 빼요", self.db.get_state("status")["slots"][0]["state_text"])
        self.request(5, action="cancel_close", slot=sid)                 # 취소하면 다시 전략대로
        bot.step(at(DAY1, "15:41"))
        self.assertNotIn("closing", self.slot(bot))
        self.request(6, action="close", slot=sid)
        bot.step(at(DAY1, "15:42"))
        self.toss.set(self.SYMBOL, DAY2, 101_000)
        bot.step(at(DAY2, "09:01"))                                      # 다음 장 시작에 다 팔고 잔액을 금고로
        self.assertEqual([t[0] for t in self.trades()], ["buy", "sell"])
        s = self.slot(bot)
        self.assertEqual((s["qty"], s["cash"], "closing" in s), (0, 0, False))  # 계좌는 0원 지켜보기로 남는다
        sold = self.db.trades("paper")[-1]
        card = self.db.get_state("status")["slots"][0]
        self.assertEqual((card["sells"], card["wins"]), (1, 1))         # 계좌별 확정 손익 (수수료·거래세 뺀 뒤)
        self.assertAlmostEqual(card["realized"], sold["profit"])
        self.assertEqual((card["gains"], card["losses"]), (card["realized"], 0))
        self.assertGreater(bot.book.vault, 1_000_000)                    # 판 돈과 잔액은 금고로
        self.request(7, action="remove", slot=sid)
        bot.step(at(DAY2, "09:02"))
        self.assertEqual(bot.book.data["slots"], {})                     # 0원이 된 뒤에 지우기

    def test_same_stock_can_have_several_accounts(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=200_000)
        self.request(2, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=300_000)
        bot.step(at(DAY1, "08:30"))
        self.assertEqual(sorted(s["name"] for s in bot.book.data["slots"].values()), ["SK하이닉스", "SK하이닉스 2"])
        bot.step(at(DAY1, "09:01"))
        rows = self.db.recent_trades()
        self.assertEqual(sorted(r["volume"] for r in rows), [1, 2])      # 계좌마다 자기 잔액으로 (20만 원 → 1주, 30만 원 → 2주)
        self.assertEqual({r["slot"] for r in rows}, set(bot.book.data["slots"]))  # 어느 계좌의 매매인지 남는다
        status = self.db.get_state("status")
        self.assertEqual(len(status["slots"]), 2)

    def test_nxt_stock_is_judged_on_regular_session_bars(self):
        prev = [candle("2026-10-21", 100_000, 120_000, 80_000, 110_000),   # NXT 종목 일봉 = 08:00~20:00 전체
                candle("2025-02-28", 50_000, 51_000, 49_000, 50_500)]       # NXT가 생기기 전: 일봉 = 정규장
        self.toss.set(self.SYMBOL, DAY1, 100_000, prev=prev)
        self.toss.nxt.add(self.SYMBOL)
        regular = candle("2026-10-21", 101_000, 105_000, 99_000, 104_000)   # 09:01~15:30 1분봉으로 만든 정규장 일봉
        self.toss.regular_[(self.SYMBOL, "2026-10-21")] = regular
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=350_000)
        bot.step(at(DAY1, "08:30"))
        bot.step(at(DAY1, "09:01"))
        rows = self.db.candle_history(self.SYMBOL, 10, stockbot.REGULAR_TF)
        ohlc = lambda c: (c["day"], c["open"], c["high"], c["low"], c["close"])
        self.assertEqual([ohlc(r) for r in rows], [ohlc(prev[1]), ohlc(regular)])
        self.assertEqual(self.toss.regular_calls, [(self.SYMBOL, "2026-10-21")])  # 1분봉은 NXT 뒤 날짜만, 한 번
        bot.step(at(DAY1, "09:02"))
        x = self.db.get_state("status")["slots"][0]
        self.assertAlmostEqual(x["change_rate"], (100_000 / 104_000 - 1) * 100)  # 등락률도 전날 정규장 종가 기준
        self.assertEqual(self.db.get_state("rankings")[self.SYMBOL]["bars"], stockbot.REGULAR_TF)

    def test_regular_bars_are_built_a_few_days_at_a_time(self):
        days = [f"2026-10-{d:02d}" for d in range(1, 21)]
        self.toss.candles_[self.SYMBOL] = [candle(d, 100, 110, 90, 105) for d in reversed(days)]
        self.toss.nxt.add(self.SYMBOL)
        self.db.upsert_candles(self.SYMBOL, self.toss.candles_[self.SYMBOL])
        done = stockbot.sync_regular(self.db, self.toss, self.SYMBOL, True, DAY1, budget=8)
        self.assertFalse(done)                                           # 한 번에 8일만
        self.assertFalse(stockbot.regular_ready(self.db, self.SYMBOL))
        self.assertEqual(self.toss.regular_calls[0], (self.SYMBOL, "2026-10-20"))  # 최근 날부터
        stockbot.sync_regular(self.db, self.toss, self.SYMBOL, True, DAY1, budget=8)
        self.assertTrue(stockbot.sync_regular(self.db, self.toss, self.SYMBOL, True, DAY1, budget=8))
        self.assertTrue(stockbot.regular_ready(self.db, self.SYMBOL))
        self.assertEqual(len(self.toss.regular_calls), 20)

    def test_key_expiry_is_alerted_with_renewal_steps(self):
        bot = self.make_bot()
        sent = []
        bot.notifier = sent.append
        bad = toss.TossError("토스증권 인증 실패 (invalid_client)", 401, "invalid_client")
        self.assertTrue(toss.is_key_error(bad))
        self.assertFalse(toss.is_key_error(toss.TossError("잔고 부족", 400, "insufficient")))

        def fail():
            raise bad
        for _ in range(2):                                               # 같은 날 두 번째는 조용히
            with self.assertRaises(toss.TossError):
                bot._call(fail)
        self.assertEqual(len(sent), 1)
        self.assertIn("python3 -m mypie.toss", sent[0])                  # 갱신 방법까지
        bot._call(lambda: None)                                          # 새 키로 다시 통함
        self.assertIn("다시 연결", sent[-1])

    def test_stop_loss_sells_intraday_and_waits_for_a_fresh_signal(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=350_000,
                     stop_loss=10)
        bot.step(at(DAY1, "08:30"))
        bot.step(at(DAY1, "09:01"))
        self.assertEqual(self.trades(), [("buy", self.SYMBOL, 3)])
        self.assertEqual(self.slot(bot)["stop_loss"], 10)
        self.toss.quotes_[self.SYMBOL]["price"] = 89_000                 # 손절선 약 90,000원 아래로 (산 날이어도 지킨다)
        bot.step(at(DAY1, "10:00"))
        self.assertEqual([t[0] for t in self.trades()], ["buy", "sell"])
        self.toss.set(self.SYMBOL, DAY2, 95_000)
        bot.step(at(DAY2, "09:01"))                                      # 매수 신호가 한 번 꺼질 때까지 다시 안 산다
        bot.step(at(DAY2, "09:02"))
        self.assertEqual(len(self.trades()), 2)
        sid = next(iter(bot.book.data["slots"]))
        self.request(2, action="strategy", slot=sid, strategy="buy_and_hold", stop_loss=0)
        bot.step(at(DAY2, "09:03"))
        self.assertNotIn("stop_loss", self.slot(bot))
        self.assertIn("손절선을 바꿨어요", self.results()[-1][1])
        self.request(3, action="strategy", slot=sid, strategy="buy_and_hold", stop_loss=0.2)
        bot.step(at(DAY2, "09:04"))
        self.assertFalse(self.results()[-1][0])                          # 너무 작은 손절선은 받지 않는다

    def test_sold_account_is_not_bought_back_at_the_same_open(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=350_000)
        bot.step(at(DAY1, "08:30"))                                      # 장 전: 계좌만 만든다
        bot.step(at(DAY1, "09:01"))
        self.assertEqual(self.trades(), [("buy", self.SYMBOL, 3)])
        self.toss.set(self.SYMBOL, DAY2, 100_000)
        both = lambda strategy, history, today_open: DayPlan(buy_at_open=True, sell_at_open=True, note="둘 다")
        with mock.patch.object(STOCK_STRATEGIES["buy_and_hold"], "plan", both):  # 조합에서 사자·팔자가 같이 켜진 날
            bot.step(at(DAY2, "09:01"))
            bot.step(at(DAY2, "09:02"))
        self.assertEqual([t[0] for t in self.trades()], ["buy", "sell"])  # 팔기만 하고 바로 다시 사지 않는다

    def test_sell_proceeds_stay_in_the_slot(self):
        prev = [candle("2026-10-21", 90_000, 110_000, 90_000, 100_000)]  # 목표가 = 시가 + 20,000 × 0.5
        self.toss.set(self.SYMBOL, DAY1, 100_000, today_open=100_000, prev=prev)
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="volatility_breakout", amount=500_000)
        bot.step(at(DAY1, "09:01"))
        self.assertEqual(self.trades(), [])                              # 아직 목표가(110,000) 아래
        self.toss.quotes_[self.SYMBOL]["price"] = 111_000
        bot.step(at(DAY1, "10:00"))                                      # 돌파 매수
        self.assertEqual(self.trades(), [("buy", self.SYMBOL, 4)])
        self.toss.set(self.SYMBOL, DAY2, 120_000, today_open=120_000,
                      prev=[candle(DAY1, 100_000, 112_000, 99_000, 111_000)] + prev)
        bot.step(at(DAY2, "09:01"))                                      # 다음 장 시작에 매도
        self.assertEqual([t[0] for t in self.trades()], ["buy", "sell"])
        self.assertEqual(bot.book.vault, 500_000)                        # 판 돈은 금고가 아니라
        self.assertGreater(self.slot(bot)["cash"], 500_000)              # 종목 계좌 잔액에 남는다

    def test_status_compares_with_buy_and_hold(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="rsi", amount=350_000)
        bot.step(at(DAY1, "08:30"))
        bot.step(at(DAY1, "08:31"))                                      # 시세를 받으면 그 가격에 산 셈
        self.toss.quotes_[self.SYMBOL]["price"] = 110_000
        bot.last_quotes = 0
        bot.step(at(DAY1, "08:32"))
        x = self.db.get_state("status")["slots"][0]
        self.assertAlmostEqual(x["hold_return_pct"], 10.0)
        self.assertAlmostEqual(x["hold_pnl"], 35_000)
        self.assertAlmostEqual(x["return_pct"], 0.0)                    # RSI는 아직 안 샀다

    def test_holiday_is_detected_when_no_trading_shows_up(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        self.toss.closed.add(DAY1)                                      # 임시 휴장일 (장 달력에서 닫힘)
        bot = self.make_bot()
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=350_000)
        bot.step(at(DAY1, "09:05"))
        bot.step(at(DAY1, "09:11"))
        self.assertEqual(self.trades(), [])
        self.assertIn(DAY1, bot.holidays)
        self.assertEqual(self.db.get_state("status")["session"], "closed")

    def test_paper_cash_and_mode_switch(self):
        bot = self.make_bot()
        self.request(1, action="paper_cash", amount=500_000)
        self.request(2, action="paper_cash", amount=-2_000_000)
        bot.step(at(DAY1, "08:00"))
        self.assertEqual(bot.book.vault, 1_500_000)
        self.assertEqual([ok for ok, _ in self.results()], [True, False])
        self.request(3, action="mode", mode="live")
        bot.step(at(DAY1, "08:01"))
        self.assertEqual(bot.executor.mode, "live")
        self.assertEqual(bot.book.vault, 1_000_000)                      # 실제 계좌 현금이 금고로

    def test_live_takes_existing_shares_trades_and_tracks_deposits(self):
        self.toss.hold[self.SYMBOL] = 2
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        bot = self.make_bot("live")
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=300_000)
        bot.step(at(DAY1, "08:30"))
        bot.last_sync = 0
        bot.step(at(DAY1, "08:31"))
        s = self.slot(bot)
        self.assertEqual((s["qty"], s["cash"]), (2, 300_000))           # 원래 있던 2주를 가져옴
        self.assertEqual(bot.book.vault, 700_000)
        sid = next(iter(bot.book.data["slots"]))
        self.request(2, action="strategy", slot=sid, strategy="ma_cross")  # 다시 사게 하려고 전략만 바꿔 봄
        self.toss.cash_ += 100_000                                       # 입금
        bot.last_sync = 0
        bot.step(at(DAY1, "08:40"))
        self.assertEqual(bot.book.vault, 800_000)

    def test_live_order_survives_restart(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        bot = self.make_bot("live")
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=350_000)
        bot.step(at(DAY1, "08:30"))
        sid = next(iter(bot.book.data["slots"]))
        # 주문은 들어갔는데 결과를 적기 전에 전원이 나간 상황
        oid = self.toss.place_order(self.SYMBOL, "buy", 3, 101_000, "mypie-x")
        self.db.set_state(LiveStockExecutor.PENDING_KEY, {"client_id": "mypie-x", "symbol": self.SYMBOL, "side": "buy",
                                                          "qty": 3, "slot": sid, "day": DAY1, "reason": "매수 신호"})
        bot2 = self.make_bot("live")
        bot2.step(at(DAY1, "09:01"))
        self.assertEqual(self.trades()[0], ("buy", self.SYMBOL, 3))
        self.assertEqual(self.slot(bot2)["qty"], 3)
        self.assertEqual(len(self.toss.placed), 1)                       # 다시 사지 않는다 (오늘 이미 샀음)
        self.assertIsNone(self.db.get_state(LiveStockExecutor.PENDING_KEY))
        self.assertTrue(oid)

    def test_partial_fill_is_recorded_and_rest_cancelled(self):
        self.toss.set(self.SYMBOL, DAY1, 100_000)
        self.toss.fill_ratio = 0.5
        bot = self.make_bot("live")
        self.request(1, action="add", symbol=self.SYMBOL, name="SK하이닉스", strategy="buy_and_hold", amount=400_000)
        bot.step(at(DAY1, "09:01"))                                      # 계좌를 만들고
        bot.step(at(DAY1, "09:02"))                                      # 그 종목 시세를 받은 다음 루프에 산다
        self.assertEqual(self.trades(), [("buy", self.SYMBOL, 1)])     # 3주 주문 중 절반(1주)만 체결
        self.assertEqual(self.slot(bot)["qty"], 1)




class StockStrategyTest(unittest.TestCase):
    def test_stock_list_has_common_and_stock_only_strategies(self):
        from mypie.strategies import STOCK_STRATEGIES, STRATEGIES
        self.assertTrue(set(STRATEGIES) < set(STOCK_STRATEGIES))
        self.assertNotIn("new_high_52w", STRATEGIES)                     # 주식 전용은 코인 목록에 없다
        self.assertEqual(list(STOCK_STRATEGIES)[-1], "buy_and_hold")
        for cls in STOCK_STRATEGIES.values():
            self.assertTrue(cls.label and cls.description and cls.guide, cls.name)

    def test_new_high_buys_at_52_week_high_and_exits_below_ma(self):
        from mypie.strategies import STOCK_STRATEGIES, create_strategy
        hist = [candle(f"d{i:03d}", 100, 100 + (i == 100) * 50, 95, 100) for i in range(255)]
        plan = create_strategy("new_high_52w", registry=STOCK_STRATEGIES).plan(hist, 100)
        self.assertEqual(plan.buy_stop, 150)                             # 1년 중 최고가
        hist[-1] = candle("x", 100, 100, 60, 60)                          # 20일 평균 아래로 마감
        plan = create_strategy("new_high_52w", registry=STOCK_STRATEGIES).plan(hist, 60)
        self.assertTrue(plan.sell_at_open)
        self.assertIsNone(plan.buy_stop)

    def test_volume_breakout_disparity_and_alignment(self):
        from mypie.strategies import STOCK_STRATEGIES, create_strategy
        make = lambda name: create_strategy(name, registry=STOCK_STRATEGIES)
        flat = [dict(candle(f"d{i}", 100, 101, 99, 100), volume=1000) for i in range(25)]
        burst = flat + [dict(candle("b", 100, 112, 100, 110), volume=3000)]
        self.assertTrue(make("volume_breakout").plan(burst, 110).buy_at_open)
        self.assertFalse(make("volume_breakout").plan(flat + [dict(candle("b", 100, 112, 100, 110), volume=1500)], 110).buy_at_open)
        dip = flat + [candle("x", 100, 100, 85, 85)]                     # 20일 평균보다 8% 넘게 빠짐
        self.assertTrue(make("disparity").plan(dip, 85).buy_at_open)
        up = [candle(f"u{i}", 0, 0, 0, 100 + i) for i in range(130)]
        self.assertTrue(make("ma_alignment").plan(up, 0).buy_at_open)
        self.assertTrue(make("ma_alignment").plan(up[::-1], 0).sell_at_open)

    def test_ranking_with_stock_list_uses_long_history(self):
        from mypie import backtest
        from mypie.strategies import STOCK_STRATEGIES
        candles = [candle(f"d{i:04d}", 100 + i % 9, 104 + i % 9, 97 + i % 9, 101 + i % 9) for i in range(420)]
        results = backtest.rank_period(candles, 120, 0.00015, sell_cost=0.002, strategies=STOCK_STRATEGIES)
        self.assertEqual(len(results), len(STOCK_STRATEGIES))
        self.assertEqual(backtest.history_needed(STOCK_STRATEGIES), 260)


class StockWebTest(TempDb):
    def setUp(self):
        super().setUp()
        self.main_path = str(Path(self.tmp.name) / "main.db")
        self.stock_path = str(Path(self.tmp.name) / "stock.db")
        main = Database(self.main_path)
        main.set_state(pin.STATE_KEY, pin.make("2468"))
        main.close()
        web._pin_failures.clear()

    def tearDown(self):
        web._pin_failures.clear()
        super().tearDown()

    def control(self, **data):
        return web.request_stock(self.stock_path, self.main_path, {"pin": "2468", **data})

    def test_requests_are_validated_and_queued(self):
        code, body = self.control(action="add", symbol="005930", name="삼성전자", strategy="new_high_52w", amount=300000)
        self.assertEqual(code, 200)
        queued = self.db.states("request:")
        self.assertEqual(queued[0][1]["strategy"], "new_high_52w")   # 주식 전용 전략도 받는다
        self.assertEqual(self.control(action="add", symbol="BTC", strategy="rsi")[0], 400)
        self.assertEqual(self.control(action="transfer", slot="k1", amount=0)[0], 400)
        self.assertEqual(self.control(action="transfer", slot="k1", all=True, direction="out")[0], 200)
        self.assertEqual(web.request_stock(self.stock_path, self.main_path, {"action": "remove", "slot": "k1", "pin": "0000"})[0], 403)
        with mock.patch("mypie.toss.load_credentials", return_value=("a", "b", "1")):
            self.assertEqual(self.control(action="mode", mode="live")[0], 400)   # '실제 돈' 체크 없이
            self.assertEqual(self.control(action="mode", mode="live", confirm_live=True)[0], 200)
        with mock.patch("mypie.toss.load_credentials", return_value=None):
            self.assertEqual(self.control(action="mode", mode="live", confirm_live=True)[0], 400)  # 키 없음
        self.assertEqual(self.control(action="strategy", slot="k1", strategy="rsi", sell=["nope"])[0], 400)
        self.assertEqual(self.control(action="add", symbol="005930", strategy="rsi", stop_loss=0.2)[0], 400)  # 손절선 1~50%
        self.assertEqual(self.control(action="strategy", slot="k1", strategy="rsi", buy=["rsi", "disparity"], buy_op="or",
                                      sell=["ma_cross"])[0], 200)
        queued = [r for _, r in self.db.states("request:") if r["action"] == "strategy"][0]
        self.assertEqual((queued["buy"], queued["buy_op"], queued["sell"]), (["rsi", "disparity"], "or", ["ma_cross"]))
        self.assertEqual(len(self.db.states("request:")), 4)

    def test_intraday_chart_groups_minute_bars_of_the_latest_day(self):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def minute_candles(self, symbol, before=None):
                rows = []
                for day in (21, 22):
                    rows.append({"ts": f"2026-10-{day}T08:30", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})  # 장 전(NXT)도 보여준다
                    rows.append({"ts": f"2026-10-{day}T20:30", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})  # 20시 뒤는 없음
                    rows.append({"ts": f"2026-10-{day}T17:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0})  # 거래 없는 장 후 봉은 뺀다
                    for m in range(12):  # 09:01~09:12에 끝난 1분봉 = 09:00~09:11 시작
                        end = datetime(2026, 10, day, 9, 1) + timedelta(minutes=m)
                        rows.append({"ts": end.strftime("%Y-%m-%dT%H:%M"), "open": 100 + m, "high": 101 + m,
                                     "low": 99 + m, "close": 100.5 + m, "volume": 10})
                return rows[::-1], "cursor"

        web._intraday_cache.clear()
        with mock.patch("mypie.toss.load_credentials", return_value=("a", "b", "1")), \
             mock.patch("mypie.toss.TossClient", FakeClient):
            chart = web.build_stock_intraday(self.stock_path, "005930", "1d")
        web._intraday_cache.clear()
        self.assertEqual([c["day"] for c in chart["candles"]],
                         ["2026-10-22T08:25", "2026-10-22T09:00", "2026-10-22T09:05", "2026-10-22T09:10"])  # 최근 하루만, 08:00부터 5분씩
        first = chart["candles"][1]                                       # 정규장 첫 5분봉 (09:00~09:05)
        self.assertEqual((first["open"], first["close"], first["volume"]), (100, 104.5, 50))
        with mock.patch("mypie.toss.load_credentials", return_value=None):
            self.assertIn("키", web.build_stock_intraday(self.stock_path, "005930", "1w")["message"])

    def test_five_year_chart_groups_days_into_weeks(self):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def daily_candles(self, symbol, count, before=None):
                days = [datetime(2026, 10, 23) - timedelta(days=i) for i in range(10)]  # 10/14(수)~10/23(금)
                rows = [candle(d.strftime("%Y-%m-%d"), 100 + i, 110 + i, 90 + i, 105 + i)
                        for i, d in enumerate(days) if d.weekday() < 5]
                return [r for r in rows if not before or r["day"] < before][:count]

        web._chart_cache.clear()
        with mock.patch("mypie.toss.load_credentials", return_value=("a", "b", "1")), \
             mock.patch("mypie.toss.TossClient", FakeClient):
            chart = web.build_stock_weekly(self.stock_path, "005930")
        web._chart_cache.clear()
        self.assertEqual([c["day"] for c in chart["candles"]], ["2026-10-12", "2026-10-19"])  # 주의 키 = 월요일
        week = chart["candles"][1]                                       # 10/19(월)~10/23(금)
        self.assertEqual((week["open"], week["close"], week["high"], week["low"]), (104, 105, 114, 90))

    def test_search_payload_and_simulation(self):
        self.db.set_state("stocks", {"day": DAY1, "list": [{"symbol": "005930", "name": "삼성전자", "market": "KOSPI"},
                                                           {"symbol": "005935", "name": "삼성전자우", "market": "KOSPI"},
                                                           {"symbol": "000660", "name": "SK하이닉스", "market": "KOSPI"}]})
        self.assertEqual([s["symbol"] for s in web.stock_search(self.stock_path, "삼성전자")], ["005930", "005935"])
        self.assertEqual([s["symbol"] for s in web.stock_search(self.stock_path, "하이닉")], ["000660"])
        payload = web.build_stock_payload(self.stock_path, self.main_path)
        self.assertTrue(payload["pin_set"])
        self.assertTrue(any(s["stock_only"] for s in payload["strategies"]))

        cfg = {section: dict(values) for section, values in DEFAULTS.items()}
        start = datetime(2024, 1, 1)
        rows = [candle((start + timedelta(days=i)).strftime("%Y-%m-%d"), 100 + i % 7, 104 + i % 7, 97 + i % 7, 101 + i % 7)
                for i in range(600)]
        self.db.upsert_candles("005930", rows)
        sim = web.build_stock_simulation(self.stock_path, cfg, "005930", 365, "disparity")
        self.assertTrue(sim["need_data"])                                # 정규장 일봉이 아직 없으면 기다린다
        self.db.upsert_candles("005930", rows, stockbot.REGULAR_TF)
        sim = web.build_stock_simulation(self.stock_path, cfg, "005930", 365, "disparity")
        self.assertTrue(sim["ready"])
        self.assertEqual((sim["detail"]["name"], len(sim["detail"]["close"])), ("disparity", 365 * 250 // 365))
        self.assertTrue(sim["stale"])                                    # 마지막 일봉이 오래전
        self.assertEqual(len(sim["results"]), len(STOCK_STRATEGIES))
        rule = web._query_rule({"strategy": ["disparity"], "buy": ["disparity,new_high_52w"], "sell": ["ma_cross"]}, STOCK_STRATEGIES)
        sim = web.build_stock_simulation(self.stock_path, cfg, "005930", 365, "disparity", rule)
        self.assertEqual((sim["detail"]["name"], len(sim["results"])), ("combo", len(STOCK_STRATEGIES) + 1))
        self.assertTrue(sim["results"][0]["combo"])
        self.assertTrue(web.build_stock_simulation(self.stock_path, cfg, "000660", 90, "")["need_data"])


if __name__ == "__main__":
    unittest.main()
