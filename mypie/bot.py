"""자동매매 봇 본체 (포트폴리오).

실행: python3 -m mypie.bot          (계속 실행)
      python3 -m mypie.bot --once   (한 번만 돌고 상태 출력, 점검용)

포트폴리오는 여러 줄(slot)로 되어 있고, 줄마다 종목 + 전략 + 비중(%)이 있다.
줄마다 따로 판단하고 따로 사고판다 (돈도 줄마다 따로, broker.Book 참고).

줄마다 판단 주기(일봉·4시간봉·1시간봉·15분봉)가 있다. 그 주기의 새 봉이 시작될 때(일봉은 매일 09:00 KST)
계획을 세우고, 매도 신호가 난 줄을 먼저 판 다음 매수 신호가 난 줄을 산다 ('보유 유지'는 그대로).
그 뒤 10초마다 현재가를 보면서 돌파 매수·이탈 매도를 한다.

전원이 나갔다 들어오거나 재시작돼도 멈추지 않는다. 끊기기 전에 낸 주문이 있으면 업비트에 물어서
장부를 먼저 맞추고(recover), 곧바로 같은 설정으로 이어서 매매한다.
"""

import argparse
import json
import logging
import re
import signal
import threading
import time
import uuid
from dataclasses import asdict
from datetime import timedelta

from . import backtest, network, reports, telegram, timeframes, upbit
from .broker import MIN_ORDER_KRW, Book, LiveExecutor, PaperExecutor, sync_full
from .config import load_config
from .db import Database
from .history import sync_candles
from .keys import days_left, load_keys
from .strategies import STRATEGIES, DayPlan, create_strategy, strategy_params
from .timeframes import bar_key, next_bar
from .timeutil import KST, now_kst, trading_day, ts

log = logging.getLogger("mypie.bot")

EQUITY_SNAPSHOT_SEC = 300   # 자산 추이 기록 간격
EXCHANGE_REFRESH_SEC = 60   # 실거래일 때 업비트 실제 잔고 확인 간격
MARKET_INFO_SEC = 60        # 화면용 시세 요약·급등락 갱신 간격
IP_CHECK_SEC = 600          # 집 공인 IP 확인 간격
MAX_BACKOFF_SEC = 300       # 오류가 계속될 때 최대 대기 시간
TOPUP_MIN_KRW = 10_000      # 보유 중인 줄에 이만큼 이상 원화 몫이 생기면(입금·비중 변경) 추가 매수
MAX_SLOTS = 10
MARKET_PATTERN = re.compile(r"^KRW-[A-Z0-9]{1,15}$")


class Slot:
    """포트폴리오의 한 줄: 종목 + 전략 + 판단 주기 + 비중."""

    def __init__(self, cfg: dict, conf: dict):
        self.id = conf["id"]
        self.market = conf["market"]
        self.tf = conf.get("tf") or timeframes.DEFAULT
        self.weight = float(conf["weight"])
        params = {**strategy_params(cfg, conf["strategy"]), "_bar": timeframes.TIMEFRAMES[self.tf]["bar"]}
        self.strategy = create_strategy(conf["strategy"], params)
        self.plan: DayPlan | None = None
        self.plan_day: str | None = None  # 계획을 세운 봉의 키 (일봉이면 날짜)

    @property
    def coin(self) -> str:
        return self.market.split("-")[-1]

    @property
    def rank_key(self) -> str:
        return ranking_key(self.market, self.tf)

    def conf(self) -> dict:
        return {"id": self.id, "market": self.market, "strategy": self.strategy.name, "tf": self.tf,
                "weight": self.weight}


def ranking_key(market: str, tf: str) -> str:
    """전략 순위는 종목 × 판단 주기마다 따로다."""
    return f"{market}|{tf}"


def new_slot_id() -> str:
    return "s" + uuid.uuid4().hex[:6]


def validate_slots(raw) -> tuple[list[dict] | None, str | None]:
    """웹페이지에서 온 포트폴리오를 검사한다. (정리된 줄 목록, 문제) 중 하나."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SLOTS:
        return None, f"종목은 1~{MAX_SLOTS}개까지 넣을 수 있어요"
    slots, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            return None, "잘못된 줄이 있어요"
        market, strategy = str(item.get("market", "")), str(item.get("strategy", ""))
        tf = str(item.get("tf") or timeframes.DEFAULT)
        try:
            weight = float(item.get("weight"))
        except (TypeError, ValueError):
            return None, f"{market}의 비중을 숫자로 넣어 주세요"
        if not MARKET_PATTERN.match(market):
            return None, f"잘못된 종목: {market}"
        if market in seen:
            return None, f"{market}이(가) 두 번 들어 있어요. 종목마다 한 줄씩만 넣어 주세요"
        if strategy not in STRATEGIES:
            return None, f"알 수 없는 전략: {strategy}"
        if tf not in timeframes.TIMEFRAMES:
            return None, f"알 수 없는 판단 주기: {tf}"
        if not 0 <= weight <= 100:  # 0%는 '지켜보기만 하는 줄' (나중에 비중만 올리면 바로 매매)
            return None, f"{market}의 비중은 0~100% 사이로 넣어 주세요"
        seen.add(market)
        slots.append({"id": str(item.get("id") or ""), "market": market, "strategy": strategy, "tf": tf,
                      "weight": weight})
    total = sum(s["weight"] for s in slots)
    if total > 100.0001:
        return None, f"비중 합계가 {total:g}%예요. 100% 이하로 맞춰 주세요"
    return slots, None


class Bot:
    def __init__(self, cfg: dict, db: Database, executor_factory=None):
        self.cfg = cfg
        self.db = db
        self.executor_factory = executor_factory or self._make_executor
        self.notifier = telegram.Notifier()  # 텔레그램 알림 (연결돼 있고 알림을 켰을 때만 보낸다)
        self.interval = float(cfg["general"]["interval_sec"])
        self.fee_rate = float(cfg["paper"]["fee_rate"])
        self.max_budget = float(cfg["live"]["max_budget"])
        self.backtest_days = int(cfg["backtest"]["days"])
        self.rank_by = cfg["backtest"]["rank_by"]

        self.settings = self._load_settings()
        try:
            self.executor = self.executor_factory(self.settings["mode"], self.settings["budget"])
        except Exception as e:  # 예: 실거래인데 키가 없어짐 → 모의투자로 안전하게 시작
            self.event("error", f"실거래 계좌를 열 수 없어서 모의투자로 시작합니다: {e}")
            self.settings["mode"] = "paper"
            self.executor = self.executor_factory("paper", self.settings["budget"])
        self.db.set_state("settings", self.settings)
        self.book = self._open_book()
        self.slots = [Slot(cfg, c) for c in self.settings["slots"]]
        self._ensure_book_slots()

        self.prices: dict[str, float] = {}
        self.markets_day: str | None = (db.get_state("markets") or {}).get("day")
        self.key_day: str | None = (db.get_state("key_info") or {}).get("checked")
        self.report_day: str | None = db.get_state("report_day")
        self.last_snapshot = 0.0
        self.last_exchange_check = 0.0
        self.last_ip_check = 0.0
        self.last_market_info = 0.0
        self.exchange: dict | None = None
        self.last_error: str | None = None
        self.warned: set = set()
        self.stop_event = threading.Event()

    # ---- 설정·장부 ----

    def _load_settings(self) -> dict:
        saved = self.db.get_state("settings") or {}
        budget = saved.get("budget", self.cfg["live"]["budget"])
        settings = {"mode": saved.get("mode", self.cfg["general"]["mode"]),
                    "budget": "all" if budget == "all" else float(budget)}
        if saved.get("slots"):
            settings["slots"] = saved["slots"]
        else:  # 예전 버전(종목 하나 + 전략 하나)에서 옮겨 오기
            market = saved.get("market", self.cfg["general"]["market"])
            chosen = (self.db.get_state("active_strategy") or {}).get("name")
            strategy = chosen if chosen in STRATEGIES else self.cfg["strategy"]["name"]
            settings["slots"] = [{"id": "s1", "market": market, "strategy": strategy, "weight": 100.0}]
            self._migrate_old_books(market)
        return settings

    def _migrate_old_books(self, market: str) -> None:
        old_state = self.db.get_state("bot_state") or {}
        if old_state:
            self.db.set_state("slot_state", {"s1": {k: old_state.get(k) for k in
                                                    ("open_done_day", "last_buy_day", "last_stop_day")}})
        for old_key, new_key, budget_of in (("paper_account", "book:paper", lambda a: "paper"),
                                            ("live_account", "book:live", lambda a: a.get("budget"))):
            acct = self.db.get_state(old_key)
            if not acct or self.db.get_state(new_key):
                continue
            self.db.set_state(new_key, {
                "budget": budget_of(acct), "reserve": 0.0, "contributions": acct.get("initial_krw", 0.0),
                "needs_sync": acct.get("budget") == "all", "needs_alloc": False,
                "slots": {"s1": {"market": acct.get("market", market), "krw": acct["krw"], "coin": acct["coin"],
                                 "avg_price": acct["avg_price"], "cost": acct["cost"],
                                 "contrib": acct.get("initial_krw", 0.0)}},
            })

    def _make_executor(self, mode: str, budget):
        if mode == "live":
            keys = load_keys()
            if not keys:
                raise upbit.UpbitError("업비트 API 키가 없어요. 파이에서 python3 -m mypie.keys 로 등록하세요")
            return LiveExecutor(self.db, upbit.UpbitClient(*keys), self.fee_rate, full=budget == "all")
        return PaperExecutor(self.fee_rate)

    def _open_book(self) -> Book:
        if self.executor.mode == "paper":
            return Book(self.db, "book:paper", "paper", float(self.cfg["paper"]["initial_krw"]))
        budget = self.settings["budget"]
        return Book(self.db, "book:live", budget, 0.0 if budget == "all" else float(budget))

    def _ensure_book_slots(self) -> None:
        """장부에 지금 줄이 다 있게 하고, 없어진 줄(코인 없는 것)의 원화는 대기 원화로 돌린다."""
        ids = {s.id for s in self.slots}
        for sid, entry in list(self.book.data["slots"].items()):
            if sid not in ids and not entry["coin"]:
                self.book.remove_slot(sid)
        for s in self.slots:
            self.book.slot(s.id, s.market)
        self.book.save()

    def _weights(self) -> dict:
        return {s.id: s.weight for s in self.slots}

    def event(self, level: str, message: str, notify: bool = False) -> None:
        getattr(log, level)(message)
        self.db.add_event(level, message)
        if notify or level == "error":
            self._notify(message)

    def _notify(self, message: str, prefix: str = "🥧 ") -> None:
        """텔레그램 알림. 웹페이지 텔레그램 카드에서 '매매 알림'을 끄면 보내지 않는다."""
        if (self.db.get_state("telegram_settings") or {}).get("notify", True):
            self.notifier(prefix + message)

    def _daily_report(self, day: str) -> None:
        """거래일이 바뀌고 09시 매매가 끝나면 지난 24시간 매매 기록과 지금 상태를 텔레그램으로 보낸다."""
        if self.report_day == day:
            return
        first = self.report_day is None  # 처음 켰을 때는 보내지 않고 날짜만 기억 (재시작마다 오지 않게)
        self.report_day = day
        self.db.set_state("report_day", day)
        if first or not telegram.load():
            return
        trades = self.db.trades(self.executor.mode, since=ts(now_kst() - timedelta(days=1)))
        self._notify(reports.daily_text(trades, self.db.get_state("status")), prefix="")

    def _listen_commands(self) -> None:
        """텔레그램 명령(/상태, /기록, /도움)에 답한다. 따로 도는 스레드라서 DB 연결도 따로 연다.
        연결된 내 채팅에서 온 메시지에만 답한다."""
        db = Database(self.db.path)
        offset, menu_set = None, False
        while not self.stop_event.is_set():
            creds = telegram.load()
            if not creds:
                offset = None
                self.stop_event.wait(30)
                continue
            token, chat = creds
            try:
                if not menu_set:  # 텔레그램 입력창 메뉴에 명령어 목록을 띄운다
                    telegram.call(token, "setMyCommands", {"commands": [{"command": c, "description": d}
                                                                        for c, d in reports.COMMANDS.items()]})
                    menu_set = True
                if offset is None:  # 봇이 꺼져 있는 동안 쌓인 메시지에는 답하지 않는다
                    backlog = telegram.call(token, "getUpdates", {"timeout": 0})
                    offset = backlog[-1]["update_id"] + 1 if backlog else 0
                updates = telegram.call(token, "getUpdates", {"offset": offset, "timeout": 25}, timeout=35)
            except telegram.TelegramError as e:
                log.debug("텔레그램 명령 확인 실패: %s", e)
                self.stop_event.wait(15)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                if str(message.get("chat", {}).get("id")) != str(chat):
                    continue
                try:
                    telegram.send(reports.command_reply(message.get("text", ""), db), creds)
                except telegram.TelegramError as e:
                    log.warning("텔레그램 답장 실패: %s", e)

    def _sstate(self, slot: Slot) -> dict:
        return (self.db.get_state("slot_state") or {}).get(slot.id, {})

    def _set_sstate(self, slot_id: str, **changes) -> None:
        states = self.db.get_state("slot_state") or {}
        states[slot_id] = {**states.get(slot_id, {}), **changes}
        self.db.set_state("slot_state", states)

    def _drop_sstate(self, slot_id: str) -> None:
        states = self.db.get_state("slot_state") or {}
        states.pop(slot_id, None)
        self.db.set_state("slot_state", states)

    def _holding(self, slot: Slot, price: float) -> bool:
        """팔 수 있을 만큼(최소 주문 금액 이상) 들고 있는지."""
        return self.book.slot(slot.id, slot.market)["coin"] * price >= MIN_ORDER_KRW

    def _tag(self, slot: Slot) -> str:
        return f"[{'실거래' if self.executor.mode == 'live' else '모의'} {slot.coin}] "

    # ---- 한 번의 루프 ----

    def step(self) -> None:
        day = trading_day()
        self._check_network()
        self._recover()  # 끊기기 전 주문이 있으면 장부부터 맞춘다 (확인 못 하면 여기서 멈추고 다음 루프에 재시도)
        self._handle_requests()
        prices = upbit.get_prices([s.market for s in self.slots])
        self.prices.update(prices)
        self._sync(prices)
        if self.book.data.get("needs_alloc"):
            self.book.allocate(self._weights(), prices)

        bars = {s.id: self._bar(s) for s in self.slots}
        ready = [s for s in self.slots if s.plan_day == bars[s.id] or self._start_bar(s, bars[s.id])]
        self._open_actions(ready, bars, prices)
        for s in ready:
            self._intraday(s, prices[s.market], bars[s.id])

        self._refresh_exchange()
        self._refresh_market_info()
        self._write_status(day)
        self._daily_report(day)
        if time.monotonic() - self.last_snapshot >= EQUITY_SNAPSHOT_SEC:
            self.db.add_equity(self.executor.mode, 0.0, self.book.equity(self.prices))
            self.last_snapshot = time.monotonic()
        if self.markets_day != day:
            self._update_markets(day)
        if self.key_day != day:
            self._update_key_info(day)
        self._update_rankings(day)

    def _bar(self, slot: Slot) -> str:
        """그 줄의 판단 주기에서 지금 진행 중인 봉의 키. 일봉이면 거래일(YYYY-MM-DD)."""
        return trading_day() if slot.tf == "1d" else bar_key(slot.tf)

    def _bars_wanted(self, tf: str) -> int:
        """백테스트·계획에 쓸 과거 봉 개수."""
        return (self.backtest_days if tf == "1d" else timeframes.TIMEFRAMES[tf]["backtest_bars"]) + backtest.WARMUP

    def _start_bar(self, slot: Slot, bar: str) -> bool:
        """새 봉 시작(또는 재시작·설정 변경 직후): 그 줄의 봉을 받아 이번 봉의 계획을 세운다."""
        current = sync_candles(self.db, slot.market, self._bars_wanted(slot.tf), slot.tf)
        if current["day"] != bar:
            log.info("%s %s 새 봉이 아직 없어 다음 루프에서 다시 시도합니다", slot.market, slot.tf)
            return False
        history = self.db.candle_history(slot.market, backtest.WINDOW, slot.tf)
        slot.plan = slot.strategy.plan(history, current["open"])
        slot.plan_day = bar
        message = f"[{bar}] {slot.coin} {timeframes.TIMEFRAMES[slot.tf]['short']} · {slot.strategy.label} · {slot.plan.note}"
        if timeframes.minutes(slot.tf) >= 240:
            self.event("info", message)
        else:
            log.info(message)  # 1시간·15분봉은 너무 잦아서 이벤트 목록에는 남기지 않는다
        return True

    def _open_actions(self, slots: list[Slot], bars: dict, prices: dict) -> None:
        """봉 시작 매매. 매도 신호가 난 줄을 먼저 팔고, 그다음 매수 신호가 난 줄을 산다 (보유 유지는 그대로).
        상태의 *_day 값에는 봉 키가 들어간다 (일봉이면 날짜)."""
        todo = [s for s in slots if self._sstate(s).get("open_done_day") != bars[s.id]]
        for s in todo:
            if self._holding(s, prices[s.market]) and s.plan.sell_at_open:
                if s.strategy.exit_next_open:
                    reason = "산 다음 날 09시 매도" if s.tf == "1d" else "산 다음 봉 시작에 매도"
                else:
                    reason = f"매도 신호 · {s.plan.note}"
                self._sell(s, prices[s.market], reason)
        for s in todo:
            if not self._holding(s, prices[s.market]) and s.plan.buy_at_open:
                self._buy(s, prices[s.market], bars[s.id], f"매수 신호 · {s.plan.note}")
            self._set_sstate(s.id, open_done_day=bars[s.id])  # 재시작해도 봉 시작 매매를 두 번 하지 않게

    def _intraday(self, slot: Slot, price: float, bar: str) -> None:
        plan, st = slot.plan, self._sstate(slot)
        if self._holding(slot, price):
            if plan.sell_stop is not None and st.get("last_buy_day") != bar and price <= plan.sell_stop:
                self._sell(slot, price, f"{plan.sell_stop:,.0f}원 이탈")
                self._set_sstate(slot.id, last_stop_day=bar)
                return
            # 입금·비중 변경으로 이 줄의 원화 몫이 생겼으면 같이 굴린다
            if self.book.slot(slot.id)["krw"] >= TOPUP_MIN_KRW:
                self._buy(slot, price, bar, "늘어난 몫만큼 추가 매수", topup=True)
        elif (plan.buy_stop is not None and price >= plan.buy_stop
              and st.get("last_buy_day") != bar and st.get("last_stop_day") != bar):
            self._buy(slot, price, bar, f"{plan.buy_stop:,.0f}원 돌파")

    # ---- 주문 ----
    # 주문 오류는 그대로 위로 올려 보낸다. 루프가 잠시 기다렸다 다시 시도하고,
    # 결과를 모르는 주문은 다음 루프의 _recover()가 업비트에 물어서 정리한다.

    def _buy(self, slot: Slot, price: float, day: str, reason: str, topup: bool = False) -> bool:
        if slot.weight <= 0:
            return False  # 비중 0% 줄은 지켜보기만 한다
        amount = self.book.slot(slot.id)["krw"]
        if amount < MIN_ORDER_KRW:
            if not topup and (slot.id, day) not in self.warned:
                self.warned.add((slot.id, day))
                self.event("warning", f"{self._tag(slot)}이 줄의 원화 몫 {amount:,.0f}원이 최소 주문 금액 "
                                      f"{MIN_ORDER_KRW:,}원보다 적어서 매수하지 않아요 (비중을 늘려 보세요)")
            return False
        note = {"slot": slot.id, "reason": reason, "day": None if topup else day}
        volume, funds, fee = self.executor.buy(slot.market, amount, price, note)
        trade = self.book.apply_buy(slot.id, volume, funds, fee, reason)
        self.executor.clear_pending()
        if not topup:  # 추가 매수는 '오늘 산 날'로 치지 않는다 (손절 규칙이 그대로 살아 있게)
            self._set_sstate(slot.id, last_buy_day=day)
        self.db.add_trade(self.executor.mode, slot.market, trade)
        self.event("info", f"🔴 {self._tag(slot)}매수 {trade['volume']:.8g} @ {trade['price']:,.0f}원 "
                           f"({trade['krw']:,.0f}원) · {reason}", notify=True)
        return True

    def _sell(self, slot: Slot, price: float, reason: str) -> bool:
        coin = self.book.slot(slot.id)["coin"]
        fill = self.executor.sell(slot.market, coin, price, {"slot": slot.id, "reason": reason})
        if fill is None:
            self.book.clear_dust(slot.id)
            self.event("info", f"{self._tag(slot)}최소 주문 금액보다 적은 자투리라 장부에서만 정리했어요")
            return True
        trade = self.book.apply_sell(slot.id, *fill, reason)
        self.executor.clear_pending()
        if slot.weight <= 0:
            self.book.release(slot.id)  # 비중 0% 줄이 판 돈은 그 줄에 두지 않고 대기 원화로
        self.db.add_trade(self.executor.mode, slot.market, trade)
        pct = trade["profit"] / trade["cost"] * 100 if trade.get("cost") else 0.0
        self.event("info", f"🔵 {self._tag(slot)}매도 {trade['volume']:.8g} @ {trade['price']:,.0f}원 · "
                           f"손익 {trade['profit']:+,.0f}원 ({pct:+.2f}%) · {reason}", notify=True)
        return True

    def _recover(self) -> None:
        result = self.executor.recover()
        if not result:
            return
        pending = result["pending"]
        side = "매수" if pending["side"] == "bid" else "매도"
        market = pending.get("market", "?")
        if result["fill"]:
            slot_id = pending.get("slot") or "s1"
            self.book.slot(slot_id, market)
            reason = f"{pending.get('reason', '')} · 재시작 후 확인".lstrip(" ·")
            apply = self.book.apply_buy if pending["side"] == "bid" else self.book.apply_sell
            trade = apply(slot_id, *result["fill"], reason)
            self.executor.clear_pending()
            self.db.add_trade(self.executor.mode, market, trade)
            if pending["side"] == "bid" and pending.get("day"):
                self._set_sstate(slot_id, last_buy_day=pending["day"])
            self.event("warning", f"[실거래] 끊기기 전에 낸 {market} {side} 주문이 체결돼 있어서 장부에 반영했어요 "
                                  f"({trade['volume']:.8g} @ {trade['price']:,.0f}원)", notify=True)
        else:
            self.event("info", f"[실거래] 끊기기 전 {market} {side} 주문은 들어가지 않았어요. 그대로 이어서 매매해요",
                       notify=True)

    def _sync(self, prices: dict) -> None:
        """계좌 전액 모드: 업비트 실제 잔고로 장부를 맞춘다 (원래 있던 코인, 입금·출금 반영)."""
        if self.executor.mode != "live" or not self.book.full:
            return
        try:
            accounts = self.executor.client.accounts()
        except upbit.UpbitError as e:
            if network.is_ip_error(e):
                self._mark_ip_error()
            raise
        self._mark_upbit_ok()
        flow = sync_full(self.book, accounts, {s.id: s.market for s in self.slots}, self._weights(), prices)
        if flow:
            kind = "입금" if flow > 0 else "출금"
            self.event("info", f"[실거래] 계좌에 {kind}(또는 직접 매매)이 있었어요 · {flow:+,.0f}원 · "
                               "비중대로 나눠 담고, 넣은 돈 합계에 반영해서 수익률이 틀어지지 않게 했어요", notify=True)

    # ---- 웹페이지 요청 ----

    def _handle_requests(self) -> None:
        request = self.db.get_state("control_request")
        if request is not None:
            self._apply_control(request)

    def _apply_control(self, request: dict) -> None:
        """포트폴리오·계좌 변경. 계좌가 바뀌면 전부 팔고 새 계좌로, 줄이 빠지거나 종목·전략이 바뀌면 그 줄만 판다.
        비중만 바뀐 줄은 팔지 않고 원화 몫만 다시 나눈다."""
        def fail(level: str, message: str) -> None:
            self.db.delete_state("control_request")
            self.event(level, message)

        slots, problem = validate_slots(request.get("slots", [c for c in self.settings["slots"]]))
        mode = request.get("mode") or self.settings["mode"]
        budget = request.get("budget") or self.settings["budget"]
        if problem is None and mode not in ("paper", "live"):
            problem = f"알 수 없는 계좌: {mode}"
        if problem is None and mode == "live" and budget != "all":
            try:
                budget = float(budget)
            except (TypeError, ValueError):
                problem = "투자 한도를 숫자로 넣어 주세요"
            else:
                if not MIN_ORDER_KRW <= budget <= self.max_budget:
                    problem = f"투자 한도는 {MIN_ORDER_KRW:,}원 ~ {self.max_budget:,.0f}원 사이여야 해요"
        if mode == "paper":
            budget = self.settings["budget"]  # 모의투자에서는 한도를 쓰지 않는다
        if problem:
            return fail("warning", f"설정 변경 실패 · {problem}")

        current_ids = {s.id for s in self.slots}
        for c in slots:
            if c["id"] not in current_ids:
                c["id"] = new_slot_id()
        account_changed = (mode, budget) != (self.settings["mode"], self.settings["budget"])
        if not account_changed and [c for c in slots] == [s.conf() for s in self.slots]:
            return self.db.delete_state("control_request")

        if account_changed and mode == "live":
            keys = load_keys()
            try:
                if not keys:
                    raise upbit.UpbitError("API 키가 없어요. 파이에서 python3 -m mypie.keys 로 등록하세요")
                upbit.UpbitClient(*keys).accounts()  # 키와 허용 IP가 맞는지 미리 확인
            except upbit.UpbitError as e:
                if network.is_ip_error(e):
                    self._mark_ip_error()
                return fail("error", f"실거래로 바꾸지 못했어요 · {e}")
            self._mark_upbit_ok()

        known = {s.market for s in self.slots}
        for c in slots:  # 새 종목이 업비트에 진짜 있는지
            if c["market"] not in known:
                try:
                    upbit.get_prices([c["market"]])
                except upbit.UpbitError as e:
                    if e.status in (400, 404):
                        return fail("warning", f"설정 변경 실패 · 업비트에 없는 종목이에요: {c['market']}")
                    raise  # 통신 오류면 요청을 남겨 두고 다음 루프에서 다시 시도

        # 팔아야 할 줄을 먼저 판다. 실패하면(통신 오류 등) 요청을 남겨 두고 다음 루프에서 다시 시도한다.
        new_by_id = {c["id"]: c for c in slots}
        prices = upbit.get_prices([s.market for s in self.slots])
        sync_prices = {**self.prices, **prices}
        self._sync(sync_prices)
        for s in self.slots:
            c = new_by_id.get(s.id)
            changed = (account_changed or c is None or c["market"] != s.market
                       or c["strategy"] != s.strategy.name or c["tf"] != s.tf)
            if changed and self._holding(s, prices[s.market]):
                self._sell(s, prices[s.market], "포트폴리오 변경")

        before = self._describe()
        if account_changed:
            self.settings.update(mode=mode, budget=budget)
            self.executor = self.executor_factory(mode, budget)
            self.book = self._open_book()
        old_by_id = {s.id: s for s in self.slots}
        new_slots = []
        for c in slots:
            slot = Slot(self.cfg, c)
            old = old_by_id.get(c["id"])
            if (old and not account_changed and old.market == slot.market
                    and old.strategy.name == slot.strategy.name and old.tf == slot.tf):
                slot.plan, slot.plan_day = old.plan, old.plan_day  # 비중만 바뀐 줄은 오늘 계획 그대로
            else:
                self._drop_sstate(c["id"])
            new_slots.append(slot)
        for sid, old in old_by_id.items():
            c = new_by_id.get(sid)
            if c is None or c["market"] != old.market:
                self.book.remove_slot(sid)
                self._drop_sstate(sid)
        self.slots = new_slots
        self._ensure_book_slots()
        self.book.data["needs_alloc"] = True  # 다음 단계에서 원화 몫을 새 비중대로 다시 나눈다
        self.book.save()
        self.settings["slots"] = [s.conf() for s in self.slots]
        self.db.set_state("settings", self.settings)
        self.exchange = None
        self.last_exchange_check = 0.0
        self.last_snapshot = 0.0
        self.db.delete_state("control_request")
        self.event("info", f"설정 변경 · {before} → {self._describe()}", notify=True)

    def _describe(self) -> str:
        budget = self.settings["budget"]
        if self.settings["mode"] == "paper":
            account = "모의투자"
        else:
            account = "실거래(계좌 전액)" if budget == "all" else f"실거래(한도 {budget:,.0f}원)"
        parts = ", ".join(f"{s.coin} {s.strategy.label}"
                          f"{'' if s.tf == '1d' else '(' + timeframes.TIMEFRAMES[s.tf]['short'] + ')'} {s.weight:g}%"
                          for s in self.slots)
        return f"{account} · {parts}"

    # ---- 집 공인 IP (업비트 허용 IP) ----

    def _network(self) -> dict:
        return self.db.get_state("network", {})

    def _check_network(self) -> None:
        """10분마다 집 공인 IP를 확인한다. 업비트가 받아 줬던 IP와 다르면 웹페이지에 경고가 뜬다."""
        if time.monotonic() - self.last_ip_check < IP_CHECK_SEC:
            return
        self.last_ip_check = time.monotonic()
        ip = network.public_ip()
        if not ip:
            return
        net = self._network()
        ok_ip = net.get("upbit_ok_ip")
        if ok_ip and ip != ok_ip and net.get("ip") != ip and load_keys():
            self.event("warning", f"집 공인 IP가 바뀐 것 같아요 ({ok_ip} → {ip}). 업비트 Open API 관리에서 "
                                  f"허용 IP를 {ip}(으)로 바꿔야 실거래 주문이 나가요", notify=True)
        self.db.set_state("network", {**net, "ip": ip, "checked_at": ts()})

    def _mark_upbit_ok(self) -> None:
        """업비트 개인 API가 통했다 = 지금 IP가 허용 IP다."""
        net = self._network()
        if net.get("ip") and (net.get("upbit_ok_ip") != net["ip"] or net.get("ip_error")):
            if net.get("ip_error"):
                self.event("info", "업비트 허용 IP 문제가 해결됐어요", notify=True)
            self.db.set_state("network", {**net, "upbit_ok_ip": net["ip"], "ip_error": False})

    def _mark_ip_error(self) -> None:
        net = self._network()
        if not net.get("ip_error"):
            self.event("error", f"업비트가 지금 IP({net.get('ip', '?')})를 허용하지 않아요. Open API 관리에서 허용 IP를 바꿔 주세요")
            self.db.set_state("network", {**net, "ip_error": True, "ip_error_at": ts()})

    # ---- 화면용 상태 ----

    def _refresh_exchange(self) -> None:
        """실거래일 때 업비트 실제 잔고를 가끔 확인해서 화면에 보여준다."""
        if self.executor.mode != "live":
            self.exchange = None
            return
        if time.monotonic() - self.last_exchange_check < EXCHANGE_REFRESH_SEC:
            return
        self.last_exchange_check = time.monotonic()
        try:
            balances = {a["currency"]: float(a["balance"]) + float(a["locked"]) for a in self.executor.client.accounts()}
            self.exchange = {"krw": balances.get("KRW", 0.0), "updated_at": ts(),
                             "coins": {s.market: balances.get(s.coin, 0.0) for s in self.slots}}
            self._mark_upbit_ok()
        except upbit.UpbitError as e:
            if network.is_ip_error(e):
                self._mark_ip_error()
            log.warning("업비트 잔고 확인 실패: %s", e)

    def _refresh_market_info(self) -> None:
        """1분마다: 포트폴리오 종목의 시세 요약과 거래대금 상위 종목의 급등·급락 (화면용, 매매와 무관)."""
        if time.monotonic() - self.last_market_info < MARKET_INFO_SEC:
            return
        self.last_market_info = time.monotonic()
        names = {m["market"]: m["name"] for m in (self.db.get_state("markets") or {}).get("list", [])}
        try:
            tickers = upbit.get_tickers(list(dict.fromkeys([*(s.market for s in self.slots), *names])))
        except upbit.UpbitError as e:
            log.warning("시세 요약을 받지 못했어요: %s", e)
            return
        movers = sorted((dict(t, name=names.get(t["market"], t["market"])) for t in tickers if t["market"] in names),
                        key=lambda t: t["change_rate"], reverse=True)
        self.db.set_state("market_info", {"updated_at": ts(), "tickers": {t["market"]: t for t in tickers},
                                          "gainers": movers[:5], "losers": movers[::-1][:5]})

    def _state_text(self, slot: Slot, price: float) -> str:
        plan, st, bar = slot.plan, self._sstate(slot), slot.plan_day
        if plan is None:
            return "계획을 세우는 중"
        daily = slot.tf == "1d"
        at = next_bar(slot.tf).astimezone(KST).strftime("%H:%M")
        if slot.weight <= 0 and not self._holding(slot, price):
            return "비중 0% · 지켜보기만 해요 (비중을 올리면 이 전략대로 사고팔아요)"
        if self._holding(slot, price):
            if slot.strategy.exit_next_open:
                return "내일 09시에 매도 예정" if daily else f"다음 봉 시작({at})에 매도 예정"
            if plan.sell_stop is not None:
                return f"{plan.sell_stop:,.0f}원 아래로 내려가면 매도"
            return "보유 유지 · 매도 신호가 나면 " + ("09시에 매도" if daily else "다음 봉 시작에 매도")
        if st.get("last_buy_day") == bar or st.get("last_stop_day") == bar:
            return "오늘 매매 완료 · 내일 다시 판단" if daily else f"이번 봉 매매 완료 · {at}에 다시 판단"
        if plan.buy_stop is not None:
            return f"{plan.buy_stop:,.0f}원 이상 오르면 매수"
        return "매수 신호를 기다리는 중 · " + ("매일 09시 판단" if daily else f"다음 판단 {at}")

    def _write_status(self, day: str) -> None:
        equity = self.book.equity(self.prices)
        contributions = self.book.data["contributions"]
        slots = []
        for s in self.slots:
            b = self.book.slot(s.id, s.market)
            price = self.prices.get(s.market, 0.0)
            value = b["coin"] * price
            slot_equity = b["krw"] + value
            holding = self._holding(s, price)
            slots.append({
                "id": s.id, "market": s.market, "coin": s.coin, "weight": s.weight, "price": price,
                "tf": s.tf, "tf_label": timeframes.TIMEFRAMES[s.tf]["short"], "bar": s.plan_day,
                "next_at": ts(next_bar(s.tf)), "rank_key": s.rank_key,
                "strategy": {"name": s.strategy.name, "label": s.strategy.label, "description": s.strategy.description,
                             "guide": s.strategy.guide, "exit_next_open": s.strategy.exit_next_open},
                "plan": asdict(s.plan) if s.plan else None,
                "state_text": self._state_text(s, price),
                "holding": holding,
                "book": b, "value": value, "equity": slot_equity,
                "return_pct": (slot_equity / b["contrib"] - 1) * 100 if b["contrib"] > 0 else None,
                "unrealized": value - b["cost"] if holding else None,
                "unrealized_pct": (value / b["cost"] - 1) * 100 if holding and b["cost"] > 0 else None,
                "actual_weight": slot_equity / equity * 100 if equity > 0 else 0.0,
            })
        net = self._network()
        self.db.set_state("status", {
            "updated_at": ts(),
            "mode": self.executor.mode,
            "budget": self.settings["budget"],
            "full": self.book.full,
            "day": day,
            "interval_sec": self.interval,
            "settings": self.settings,
            "slots": slots,
            "account": {"cash": self.book.cash(), "reserve": self.book.data["reserve"], "equity": equity,
                        "contributions": contributions,
                        "return_pct": (equity / contributions - 1) * 100 if contributions > 0 else None},
            "exchange": self.exchange,
            "network": {**net, "ip_changed": bool(net.get("upbit_ok_ip") and net.get("ip")
                                                  and net["ip"] != net["upbit_ok_ip"])},
        })

    def _update_markets(self, day: str) -> None:
        """하루 한 번: 거래대금 상위 원화 종목 목록 (웹페이지 종목 선택용)."""
        self.markets_day = day
        try:
            self.db.set_state("markets", {"day": day, "list": upbit.get_krw_markets(30)})
        except upbit.UpbitError as e:
            log.warning("종목 목록을 받지 못했어요: %s", e)

    def _update_key_info(self, day: str) -> None:
        """하루 한 번: 업비트 키 만료일 확인 (키는 발급 후 1년이면 만료되고 연장이 안 된다)."""
        self.key_day = day
        keys = load_keys()
        if not keys:
            self.db.delete_state("key_info")
            return
        try:
            expire_at = upbit.UpbitClient(*keys).key_expiry()
        except upbit.UpbitError as e:
            if network.is_ip_error(e):
                self._mark_ip_error()
            log.warning("업비트 키 만료일을 확인하지 못했어요: %s", e)
            return
        self._mark_upbit_ok()
        self.db.set_state("key_info", {"expire_at": expire_at, "checked": day})
        if expire_at and days_left(expire_at) <= 30:
            self.event("warning", f"업비트 API 키가 {expire_at[:10]}에 만료돼요 (앞으로 {days_left(expire_at)}일). "
                                  "만료되면 실거래가 멈추니 새 키를 받아 python3 -m mypie.keys 로 등록하세요", notify=True)

    def _update_rankings(self, day: str) -> None:
        """종목 × 판단 주기별 전략 순위. 포트폴리오 줄은 하루 한 번, 웹페이지에서 요청한 건 바로 (한 루프에 하나씩).
        요청은 "KRW-BTC|60m" 모양 (주기가 없으면 일봉)."""
        rankings = self.db.get_state("rankings") or {}
        queue = (self.db.get_state("ranking_request") or {}).get("markets", [])
        while queue:
            item, queue = queue[0], queue[1:]
            if queue:
                self.db.set_state("ranking_request", {"markets": queue})
            else:
                self.db.delete_state("ranking_request")
            market, _, tf = item.partition("|")
            tf = tf or timeframes.DEFAULT
            key = ranking_key(market, tf)
            if not (MARKET_PATTERN.match(market) and tf in timeframes.TIMEFRAMES):
                continue
            old = rankings.get(key) or {}
            if old.get("day") != day or len(old.get("results") or []) != len(STRATEGIES):
                self._rank_market(market, tf, day, rankings)
                return
            try:  # 순위는 오늘 것이 있어도 봉은 새로 받아 둔다 (웹페이지 전략 시뮬레이션용)
                sync_candles(self.db, market, self._bars_wanted(tf), tf)
            except upbit.UpbitError as e:
                log.warning("%s %s 봉을 받지 못했어요: %s", market, tf, e)
            return
        for s in self.slots:
            old = rankings.get(s.rank_key) or {}
            # 오늘 것이 아니거나, 그 뒤로 전략이 추가·삭제됐으면 다시 계산
            if old.get("day") != day or len(old.get("results") or []) != len(STRATEGIES):
                self._rank_market(s.market, s.tf, day, rankings)
                return

    def _rank_market(self, market: str, tf: str, day: str, rankings: dict) -> None:
        key = ranking_key(market, tf)
        wanted = self._bars_wanted(tf)
        try:
            sync_candles(self.db, market, wanted, tf)
        except upbit.UpbitError as e:
            log.warning("%s %s 봉을 받지 못해서 순위를 계산하지 못했어요: %s", market, tf, e)
            rankings[key] = {"day": day, "market": market, "tf": tf, "error": str(e)}
            self.db.set_state("rankings", rankings)
            return
        candles = self.db.candle_history(market, wanted, tf)
        if len(candles) < backtest.WARMUP + 30:
            rankings[key] = {"day": day, "market": market, "tf": tf, "error": "상장한 지 얼마 안 돼서 과거 데이터가 부족해요"}
            self.db.set_state("rankings", rankings)
            return
        started = time.monotonic()
        params = {name: strategy_params(self.cfg, name) for name in STRATEGIES}
        results = backtest.rank_strategies(candles, self.fee_rate, self.rank_by,
                                           bars_per_year=timeframes.bars_per_year(tf), params=params)
        rankings[key] = {
            "day": day, "market": market, "tf": tf, "computed_at": ts(), "rank_by": self.rank_by,
            "from": candles[backtest.WARMUP]["day"], "to": candles[-1]["day"],
            "days": len(candles) - backtest.WARMUP, "results": results,
        }
        self.db.set_state("rankings", rankings)
        label = timeframes.TIMEFRAMES[tf]["short"]
        log.info("%s %s 전략 순위 계산 %.1f초", market, tf, time.monotonic() - started)
        top = results[0]
        self.event("info", f"{market} {label} 전략 순위 갱신 · 1위 {top['label']} ({top['total_return'] * 100:+.1f}%)")

    # ---- 계속 실행 ----

    def run(self) -> None:
        self.event("info", f"🥧 봇 시작 · {self._describe()}", notify=True)
        threading.Thread(target=self._listen_commands, name="telegram-commands", daemon=True).start()
        wait = self.interval
        while not self.stop_event.is_set():
            try:
                self.step()
                if self.last_error:
                    self.event("info", "정상으로 돌아왔어요")
                    self.last_error = None
                wait = self.interval
            except Exception as e:  # 어떤 오류가 나도 봇은 멈추지 않고 재시도한다
                if network.is_ip_error(e):
                    self._mark_ip_error()
                message = f"업비트 오류: {e}" if isinstance(e, upbit.UpbitError) else f"오류: {e!r}"
                if message != self.last_error:
                    if isinstance(e, upbit.UpbitError):
                        self.event("warning", message)
                    else:
                        log.exception("예상치 못한 오류")
                        self.db.add_event("error", message)
                    self.last_error = message
                # 주문 결과 확인은 빨리 다시 시도하고, 나머지 오류는 점점 길게 기다린다
                wait = self.interval if isinstance(e, upbit.OrderUnknown) else min(wait * 2, MAX_BACKOFF_SEC)
            self.stop_event.wait(wait)
        self.event("info", "봇 종료")


def main() -> None:
    parser = argparse.ArgumentParser(description="My_Pie 자동매매 봇")
    parser.add_argument("--once", action="store_true", help="한 번만 실행하고 상태를 출력")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    log.info("설정 파일: %s", cfg["_path"] or "(기본값)")
    bot = Bot(cfg, Database(cfg["general"]["db_path"]))

    if args.once:
        bot.step()
        print(json.dumps(bot.db.get_state("status"), ensure_ascii=False, indent=2))
        return

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: bot.stop_event.set())
    bot.run()


if __name__ == "__main__":
    main()
