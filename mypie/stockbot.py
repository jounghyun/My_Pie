"""주식 자동매매 봇 (토스증권 · 국내 주식).

실행: python3 -m mypie.stockbot          (계속 실행, 서비스 이름 mypie-stock)
      python3 -m mypie.stockbot --once   (한 번만 돌고 상태 출력, 점검용)

계좌 구조는 vault.py 참고: 실제 계좌 하나를 금고 + 종목 계좌(종목 하나 + 전략 하나) 여러 개로 나눠 쓴다.
코인 봇(bot.py)과 같은 전략·같은 규칙을 쓰고, 판단은 일봉으로만 한다 (주식 장은 하루 6시간 반만 열린다).

  - 거래일 장 시작(09:00) 뒤 첫 루프: 종목 계좌마다 계획을 세우고, 매도 신호가 난 계좌를 먼저 판 다음
    매수 신호가 난 계좌를 산다 ('보유 유지'는 그대로). 사는 돈은 그 종목 계좌의 잔액뿐이다
  - 장중(09:00~15:20): 10초마다 현재가를 보면서 돌파 매수·이탈 매도
  - 장 마감 뒤: 마감 리포트(텔레그램), 전략 순위 계산
  - 웹페이지 요청(종목 계좌 추가·삭제·전략 변경·금고 이체·계좌 변경)은 장 시간과 상관없이 바로 처리한다

전원이 나갔다 들어오거나 재시작돼도 멈추지 않는다. 끊기기 전에 낸 주문이 있으면 토스증권에 물어서
장부를 먼저 맞추고(recover), 곧바로 이어서 매매한다.
"""

import argparse
import json
import logging
import re
import signal
import threading
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import backtest, buyhold, krx, stoploss, telegram, toss
from .config import load_config
from .db import Database
from .stockbroker import LiveStockExecutor, PaperStockExecutor
from .strategies import STOCK_STRATEGIES, DayPlan, create_strategy, strategy_params
from .strategies.combo import check_rule, make_rule, normalize_rule, rule_label
from .timeutil import KST, now_kst, ts
from .vault import VaultBook, VaultError, new_slot_id

log = logging.getLogger("mypie.stock")

EQUITY_SNAPSHOT_SEC = 300   # 자산 추이 기록 간격
SYNC_SEC = 30               # 실거래 장중: 실제 잔고 확인 간격 (장 밖에서는 IDLE_REFRESH_SEC)
IDLE_REFRESH_SEC = 300      # 장 밖에서 시세·잔고 갱신 간격
MAX_BACKOFF_SEC = 300       # 오류가 계속될 때 최대 대기 시간
MAX_SLOTS = 20
REQUEST_PREFIX = "request:"  # 웹페이지가 남기는 요청 (request:<시각>-<번호>)
MAX_RESULTS = 20
SYMBOL_PATTERN = re.compile(r"^[0-9A-Z]{6}$")
TRADING_DAYS_PER_YEAR = 250
SESSION_TEXT = {"closed": "휴장일", "pre": "장 시작 전", "open": "장 열림", "closing": "장 마감 동시호가",
                "after": "장 마감"}


def sync_daily(db: Database, client, symbol: str, wanted: int, today: str) -> None:
    """완성된 일봉(오늘 것 빼고)을 DB에 모은다. 모자라면 과거로, 한동안 안 받았으면 빈 사이도 채운다."""
    stored = db.candle_history(symbol, 1, "1d")
    latest = client.daily_candles(symbol, 200)
    db.upsert_candles(symbol, [c for c in latest if c["day"] < today], "1d")
    edge = latest[-1]["day"] if latest else None
    while stored and edge and edge > stored[0]["day"]:
        older = client.daily_candles(symbol, 200, before=edge)
        if not older or older[-1]["day"] >= edge:
            break
        db.upsert_candles(symbol, older, "1d")
        edge = older[-1]["day"]
    count, oldest = db.candle_range(symbol, "1d")
    while count < wanted and oldest:
        older = client.daily_candles(symbol, 200, before=oldest)
        if not older:
            break  # 상장일까지 다 받음
        db.upsert_candles(symbol, older, "1d")
        count, new_oldest = db.candle_range(symbol, "1d")
        if new_oldest == oldest:
            break
        oldest = new_oldest


REGULAR_TF = "1dk"          # 정규장(09:00~15:30) 일봉. 판단·순위·시뮬레이션은 모두 이걸로 한다 (봇은 정규장에서만 사고판다)
REGULAR_BUDGET = {True: 5, False: 40}  # 한 루프에 1분봉으로 다시 만드는 날 수 (장중이면 조금씩: 루프가 오래 멈추지 않게)


def sync_regular(db: Database, client, symbol: str, nxt: bool, today: str, budget: int) -> bool:
    """정규장 일봉을 일봉('1d')에 맞춰 채운다. NXT 종목의 NXT 개장일 뒤 날짜만 1분봉으로 다시 만들고
    (토스증권 일봉이 08:00~20:00을 합친 값이라), 그 전 날짜와 NXT가 아닌 종목은 그대로 옮긴다.
    1분봉으로 만드는 날은 한 번에 budget개까지. 다 맞췄으면 True."""
    have = {r["day"] for r in db.candle_history(symbol, 100_000, REGULAR_TF)}
    todo = [r for r in db.candle_history(symbol, 100_000, "1d") if r["day"] not in have and r["day"] < today]
    copies = [r for r in todo if not nxt or r["day"] < toss.NXT_START]
    if copies:
        db.upsert_candles(symbol, copies, REGULAR_TF)
    rebuild = sorted((r for r in todo if nxt and r["day"] >= toss.NXT_START), key=lambda r: r["day"], reverse=True)
    for r in rebuild[:budget]:
        bar = client.regular_day(symbol, r["day"])
        db.upsert_candles(symbol, [bar or r], REGULAR_TF)  # 정규장 거래가 없던 날(거래정지 등)은 일봉 그대로
    return len(rebuild) <= budget


def regular_ready(db: Database, symbol: str) -> bool:
    """정규장 일봉이 일봉과 같은 날들까지 다 만들어졌는지 (덜 됐으면 사이가 비어서 지표가 틀린다)."""
    count, _ = db.candle_range(symbol, "1d")
    return count > 0 and db.candle_range(symbol, REGULAR_TF)[0] >= count


class StockSlot:
    """종목 계좌 하나의 전략과 오늘 계획 (돈과 주식은 장부 VaultBook에 있다)."""

    def __init__(self, cfg: dict, sid: str, entry: dict):
        self.id = sid
        self.symbol = entry["symbol"]
        self.name = entry.get("name") or entry["symbol"]
        self.rule = normalize_rule(entry)  # 살 때·팔 때 전략 (예전 계좌는 둘 다 같은 전략 하나)
        self.strategy = make_rule(self.rule, lambda n: create_strategy(n, {**strategy_params(cfg, n), "_bar": "일"},
                                                                       STOCK_STRATEGIES))
        self.stop_loss = float(entry.get("stop_loss") or 0)  # 손절선 % (0이면 안 씀)
        self.closing = bool(entry.get("closing"))  # 팔고 빼는 중
        self.plan: DayPlan | None = None
        self.plan_day: str | None = None


class StockBot:
    def __init__(self, cfg: dict, db: Database, client_factory=None, executor_factory=None):
        self.cfg = cfg
        self.db = db
        sc = cfg["stock"]
        self.fee_rate = float(sc["fee_rate"])
        self.sell_tax = float(sc["sell_tax"])
        self.interval = float(sc["interval_sec"])
        self.idle = float(sc["idle_sec"])
        self.paper_cash = float(sc["paper_cash"])
        self.backtest_days = int(sc["backtest_days"])
        self.rank_by = sc.get("rank_by", "return")
        self.client_factory = client_factory or self._make_client
        self.executor_factory = executor_factory or self._make_executor
        self.notifier = telegram.Notifier()

        self.settings = db.get_state("settings") or {"mode": sc["mode"]}
        db.set_state("settings", self.settings)
        self.client = None
        self.executor = None
        self.book: VaultBook | None = None
        self.slots: list[StockSlot] = []
        self.quotes: dict[str, dict] = {}
        self.prices: dict[str, float] = {}
        self.broker: dict = {}      # 실거래: 마지막으로 확인한 실제 계좌 (현금, 봇이 안 굴리는 주식)
        self.holidays: set[str] = set(db.get_state("extra_holidays") or [])
        self.open_day: str | None = None
        self.synced_today: dict[str, tuple[str, int]] = {}  # 종목 → (날짜, 받아 둔 일봉 수)
        self.regular_today: dict[str, str] = {}  # 종목 → 정규장 일봉을 다 맞춘 날
        self.report_day: str | None = db.get_state("report_day")
        self.stocks_day: str | None = (db.get_state("stocks") or {}).get("day")
        self.last_quotes = self.last_sync = self.last_snapshot = self.last_client_try = 0.0
        self.ip_error = False
        self.last_error: str | None = None
        self.warned: set = set()
        self.stop_event = threading.Event()

    # ---- 준비 ----

    def _make_client(self):
        creds = toss.load_credentials()
        return toss.TossClient(*creds, db=self.db) if creds else None

    def _make_executor(self, mode: str):
        if mode == "live":
            return LiveStockExecutor(self.db, self.client, self.fee_rate, self.sell_tax)
        return PaperStockExecutor(self.fee_rate, self.sell_tax)

    def _ready(self) -> bool:
        """토스증권 키가 있어야 시세를 받을 수 있다 (모의투자도). 없으면 1분마다 다시 확인한다."""
        if self.client is None:
            if time.monotonic() - self.last_client_try < 60 and self.last_client_try:
                return False
            self.last_client_try = time.monotonic()
            self.client = self.client_factory()
            if self.client is None:
                return False
        if self.executor is None:
            mode = self.settings["mode"]
            self.executor = self.executor_factory(mode)
            self.book = VaultBook(self.db, f"book:{mode}", self.paper_cash if mode == "paper" else 0.0)
            self._load_slots()
        return True

    def _load_slots(self) -> None:
        """장부의 종목 계좌로 StockSlot을 만든다. 종목·전략이 그대로인 계좌는 오늘 계획을 이어 쓴다."""
        old = {s.id: s for s in self.slots}
        slots = []
        for sid, entry in self.book.data["slots"].items():
            slot = StockSlot(self.cfg, sid, entry)
            prev = old.get(sid)
            if prev and prev.symbol == slot.symbol and prev.rule == slot.rule:
                slot.plan, slot.plan_day = prev.plan, prev.plan_day
            slots.append(slot)
        self.slots = slots

    def event(self, level: str, message: str, notify: bool = False) -> None:
        getattr(log, level)(message)
        self.db.add_event(level, message)
        if notify or level == "error":
            self._notify(message)

    def _notify(self, message: str, prefix: str = "📈 ") -> None:
        """텔레그램 알림. 코인 봇과 같은 텔레그램을 쓰고, 웹페이지에서 '매매 알림'을 끄면 둘 다 안 보낸다."""
        if self._notify_on():
            self.notifier(prefix + message)

    def _notify_on(self) -> bool:
        path = Path(self.cfg["general"]["db_path"])
        if not path.exists():
            return True
        db = Database(path, create=False)  # 텔레그램 설정은 코인 봇 DB에 있다
        try:
            return (db.get_state("telegram_settings") or {}).get("notify", True)
        finally:
            db.close()

    def _tag(self, slot: StockSlot) -> str:
        return f"[{'실거래' if self.executor.mode == 'live' else '모의'} {slot.name}] "

    def _sstate(self, slot: StockSlot) -> dict:
        return (self.db.get_state("slot_state") or {}).get(slot.id, {})

    def _set_sstate(self, sid: str, **changes) -> None:
        states = self.db.get_state("slot_state") or {}
        states[sid] = {**states.get(sid, {}), **changes}
        self.db.set_state("slot_state", states)

    def _drop_sstate(self, sid: str) -> None:
        states = self.db.get_state("slot_state") or {}
        states.pop(sid, None)
        self.db.set_state("slot_state", states)

    # ---- 한 번의 루프 ----

    def step(self, now: datetime | None = None) -> str:
        """한 번 돈다. 지금 장 상태를 돌려준다 (다음 루프까지 얼마나 쉴지 정하는 데 쓴다)."""
        now = (now or now_kst()).astimezone(KST)
        today = now.date().isoformat()
        sess = krx.session(now, self.holidays)
        if not self._ready():
            self._write_status(now, sess)
            return sess
        self._recover()
        self._refresh_quotes(sess, now)
        self._sync(sess)  # 실거래면 금고를 실제 현금에 먼저 맞춘 뒤에 이체 요청을 처리한다
        self._handle_requests(today)
        if sess == "open" and self._market_open_today(now, today):
            for s in [s for s in self.slots if s.closing and s.symbol in self.prices]:
                self._close_out(s)
            for s in self.slots:
                if s.plan_day != today and not s.closing:
                    self._plan(s, today)
            ready = [s for s in self.slots if s.plan_day == today and s.symbol in self.prices and not s.closing]
            self._open_actions(ready, today)
            for s in ready:
                self._intraday(s, today)
        self._refresh_stocks(today)
        self._update_rankings(today)
        if sess == "after" and self.open_day == today:
            self._close_report(today)
        self._write_status(now, krx.session(now, self.holidays))
        if time.monotonic() - self.last_snapshot >= EQUITY_SNAPSHOT_SEC:
            self.db.add_equity(self.executor.mode, 0.0, self.book.equity(self.prices))
            self.last_snapshot = time.monotonic()
        return sess

    def _refresh_quotes(self, sess: str, now: datetime | None = None) -> None:
        symbols = sorted({s.symbol for s in self.slots})
        if not symbols:
            return
        busy = sess in ("open", "closing")
        if not busy and self.quotes and time.monotonic() - self.last_quotes < IDLE_REFRESH_SEC \
                and all(sym in self.quotes for sym in symbols):
            return
        self.quotes.update(self._call(self.client.quotes, symbols))
        self.prices.update({sym: q["price"] for sym, q in self.quotes.items() if q.get("price")})
        self.last_quotes = time.monotonic()
        previous = krx.previous_trading_day((now or now_kst()).date(), self.holidays).isoformat()
        for sym in symbols:  # 등락률은 전날 정규장 종가 기준 (NXT 애프터마켓 20:00 종가 말고)
            last = self.db.candle_history(sym, 1, REGULAR_TF)
            if sym in self.quotes and last and last[0]["day"] == previous:
                self.quotes[sym]["prev_close"] = last[0]["close"]

    def _call(self, fn, *args, **kwargs):
        """토스증권 호출. 허용 IP 오류면 표시해 두고, 키 만료면 알린 뒤 그대로 올려 보낸다."""
        try:
            result = fn(*args, **kwargs)
        except toss.TossError as e:
            if toss.is_ip_error(e):
                self.ip_error = True
            if toss.is_key_error(e):
                self._key_alert()
            raise
        self.ip_error = False
        if (self.db.get_state("key_alert") or {}).get("expired"):  # 만료 알림 뒤 새 키로 다시 통했다
            self.db.delete_state("key_alert")
            self.event("info", "토스증권 API 키가 다시 연결됐어요", notify=True)
        return result

    def _key_alert(self) -> None:
        """API 키 만료 알림: 갱신 방법까지 텔레그램으로, 하루 한 번 (매매 알림을 꺼도 보낸다). 화면 기록에는 첫 줄만."""
        day = now_kst().date().isoformat()
        if (self.db.get_state("key_alert") or {}).get("day") == day:
            return
        self.db.set_state("key_alert", {"day": day, "at": ts(), "expired": True})
        text = toss.expired_text()
        log.warning(text.splitlines()[0])
        self.db.add_event("error", text.splitlines()[0])
        self.notifier("📈 " + text)

    def _market_open_today(self, now: datetime, today: str) -> bool:
        """달력상 거래일이어도 임시 휴장일일 수 있어서 토스증권 장 달력으로 하루 한 번 확인한다."""
        if self.open_day == today:
            return True
        if self._call(self.client.is_trading_day, today):
            self.open_day = today
            return True
        self.holidays.add(today)
        self.db.set_state("extra_holidays", sorted(self.holidays)[-30:])
        self.event("info", f"오늘({today})은 장이 열리지 않는 날이라 쉬어요 (토스증권 장 달력)")
        return False

    def _plan(self, slot: StockSlot, today: str) -> None:
        """오늘 계획: 어제까지의 정규장 일봉 + 오늘 정규장 시가. 시가나 정규장 일봉이 아직이면 다음 루프에서 다시."""
        today_open = (self.quotes.get(slot.symbol) or {}).get("open")
        if not today_open:
            return
        if not self._ensure_history(slot.symbol, today, slot.strategy.window + 5):
            return  # 정규장 일봉을 만드는 중 (다음 루프에서 이어서)
        history = self.db.candle_history(slot.symbol, slot.strategy.window, REGULAR_TF)
        slot.plan = slot.strategy.plan(history, today_open)
        slot.plan_day = today
        self.event("info", f"[{today}] {slot.name} · {slot.strategy.label} · {slot.plan.note}")

    def _ensure_history(self, symbol: str, today: str, wanted: int) -> bool:
        """일봉을 모으고 정규장 일봉을 맞춘다. 정규장 일봉까지 다 됐으면 True."""
        if self.synced_today.get(symbol, ("", 0)) < (today, wanted):
            self._call(sync_daily, self.db, self.client, symbol, wanted, today)
            self.synced_today[symbol] = (today, wanted)
            self.regular_today.pop(symbol, None)
        if self.regular_today.get(symbol) == today:
            return True
        busy = krx.session(now_kst(), self.holidays) in ("open", "closing")
        done = self._call(sync_regular, self.db, self.client, symbol, self._nxt(symbol, today), today,
                          REGULAR_BUDGET[busy])
        if done:
            self.regular_today[symbol] = today
        return done

    def _nxt(self, symbol: str, today: str) -> bool:
        """넥스트레이드에서도 거래되는 종목인지 (한 달에 한 번 확인해서 기억해 둔다)."""
        known = self.db.get_state("nxt_symbols") or {}
        entry = known.get(symbol)
        if not entry or entry["checked"] < (date.fromisoformat(today) - timedelta(days=30)).isoformat():
            entry = {"nxt": self._call(self.client.nxt_supported, [symbol]).get(symbol, False), "checked": today}
            known[symbol] = entry
            self.db.set_state("nxt_symbols", known)
        return entry["nxt"]

    def _rank_bars(self) -> int:
        """순위 계산에 쓰는 일봉 수: 백테스트 기간(거래일) + 지표 계산용 앞부분."""
        return self.backtest_days * TRADING_DAYS_PER_YEAR // 365 + backtest.history_needed(STOCK_STRATEGIES)

    def _open_actions(self, slots: list[StockSlot], today: str) -> None:
        """장 시작 매매. 매도 신호가 난 계좌를 먼저 팔고, 그다음 매수 신호가 난 계좌를 산다."""
        todo = [s for s in slots if self._sstate(s).get("open_done_day") != today]
        sold = set()
        for s in todo:
            if self._holding(s) and s.plan.sell_at_open:
                reason = "산 다음 장 시작에 매도" if s.strategy.exit_next_open else f"매도 신호 · {s.plan.note}"
                self._sell(s, reason)
                self._set_sstate(s.id, open_done_day=today)
                sold.add(s.id)
        for s in todo:
            cut = self._sstate(s).get("cut")
            if cut and not s.plan.buy_at_open:
                cut = False  # 손절 뒤 매수 신호가 한 번 꺼졌다 → 다음 신호부터 다시 산다
                self._set_sstate(s.id, cut=False)
            # 방금 판 계좌는 같은 장 시작에 다시 사지 않는다 (살 때·팔 때 조합에서 사자·팔자가 같이 켜진 경우. 시뮬레이션과 같은 규칙)
            if s.id not in sold and not cut and not self._holding(s) and s.plan.buy_at_open:
                self._buy(s, today, f"매수 신호 · {s.plan.note}")
            self._set_sstate(s.id, open_done_day=today)  # 재시작해도 장 시작 매매를 두 번 하지 않게

    def _loss_line(self, slot: StockSlot) -> float | None:
        s = self.book.slot(slot.id)
        return stoploss.line(s["cost"], s["qty"], slot.stop_loss)

    def _intraday(self, slot: StockSlot, today: str) -> None:
        plan, st, price = slot.plan, self._sstate(slot), self.prices[slot.symbol]
        if self._holding(slot):
            loss = self._loss_line(slot)  # 손절선은 산 직후에도 지킨다
            if loss is not None and price <= loss:
                self._sell(slot, f"{stoploss.text(slot.stop_loss)} ({loss:,.0f}원) 이탈")
                self._set_sstate(slot.id, last_stop_day=today, cut=True)
                return
            if plan.sell_stop is not None and st.get("last_buy_day") != today and price <= plan.sell_stop:
                self._sell(slot, f"{plan.sell_stop:,.0f}원 이탈")
                self._set_sstate(slot.id, last_stop_day=today)
        elif (plan.buy_stop is not None and price >= plan.buy_stop
              and st.get("last_buy_day") != today and st.get("last_stop_day") != today):
            self._buy(slot, today, f"{plan.buy_stop:,.0f}원 돌파")

    def _close_out(self, slot: StockSlot) -> None:
        """'팔고 빼기'를 누른 계좌: 들고 있는 주식을 팔고, 다 팔렸으면 잔액을 금고로 뺀다 (계좌는 0원 지켜보기로 남는다)."""
        if self._holding(slot):
            self._sell(slot, "팔고 빼기")
        if not self._holding(slot):  # 일부만 체결되면 다음 루프에서 나머지를 판다
            moved = self._withdraw(slot.id)
            self.event("info", f"{self._tag(slot)}다 팔고 잔액 {moved:,.0f}원을 금고로 뺐어요 · "
                               "계좌는 0원 지켜보기로 남아요 (필요 없으면 '지우기')", notify=True)

    def _withdraw(self, sid: str) -> float:
        """팔고 빼기 마무리: 잔액을 전부 금고로 빼고 계좌는 지켜보기로 남긴다. 뺀 금액을 돌려준다."""
        moved = self.book.slot(sid)["cash"]
        if moved > 0:
            self.book.transfer(sid, -moved)
        self.book.set_closing(sid, False)
        self._load_slots()
        return moved

    def _holding(self, slot: StockSlot) -> bool:
        return self.book.slot(slot.id)["qty"] > 0

    # ---- 주문 ----
    # 주문 오류는 그대로 위로 올려 보낸다. 루프가 잠시 기다렸다 다시 시도하고,
    # 결과를 모르는 주문은 다음 루프의 _recover()가 토스증권에 물어서 정리한다.

    def _buy(self, slot: StockSlot, today: str, reason: str) -> None:
        s, price = self.book.slot(slot.id), self.prices[slot.symbol]
        result = None
        if s["cash"] >= price:
            result = self._call(self.executor.buy, slot.symbol, s["cash"], price,
                                {"slot": slot.id, "reason": reason, "day": today})
        if result is None:
            if (slot.id, today) not in self.warned:
                self.warned.add((slot.id, today))
                self.event("warning", f"{self._tag(slot)}매수 신호가 났지만 잔액 {s['cash']:,.0f}원으로는 1주"
                                      f"({price:,.0f}원)도 살 수 없어요. 금고에서 이체해 주세요", notify=True)
            return
        qty, fill_price, fee = result
        if qty <= 0:
            self.executor.clear_pending()
            self.event("warning", f"{self._tag(slot)}매수 주문이 체결되지 않아서 취소했어요 · {reason}")
            return
        trade = self.book.apply_buy(slot.id, qty, fill_price, fee, reason)
        self.executor.clear_pending()
        self._set_sstate(slot.id, last_buy_day=today, cut=False)
        self.db.add_trade(self.executor.mode, slot.symbol, trade)
        self.event("info", f"🔴 {self._tag(slot)}매수 {qty:,}주 @ {fill_price:,.0f}원 ({trade['krw']:,.0f}원) · {reason}",
                   notify=True)

    def _sell(self, slot: StockSlot, reason: str) -> None:
        s = self.book.slot(slot.id)
        qty, fill_price, fee, tax = self._call(self.executor.sell, slot.symbol, s["qty"], self.prices[slot.symbol],
                                               {"slot": slot.id, "reason": reason})
        if qty <= 0:
            self.executor.clear_pending()
            self.event("warning", f"{self._tag(slot)}매도 주문이 체결되지 않아서 취소했어요. 다음 루프에 다시 해요 · {reason}")
            return
        trade = self.book.apply_sell(slot.id, qty, fill_price, fee, tax, reason)
        self.executor.clear_pending()
        self.db.add_trade(self.executor.mode, slot.symbol, trade)
        pct = trade["profit"] / trade["cost"] * 100 if trade.get("cost") else 0.0
        self.event("info", f"🔵 {self._tag(slot)}매도 {qty:,}주 @ {fill_price:,.0f}원 · 손익 {trade['profit']:+,.0f}원 "
                           f"({pct:+.2f}%) · {reason}", notify=True)

    def _recover(self) -> None:
        result = self.executor.recover()
        if not result:
            return
        pending, fill = result["pending"], result["fill"]
        side = "매수" if pending["side"] == "buy" else "매도"
        sid = pending.get("slot")
        if fill and sid and self.book.has(sid):
            reason = f"{pending.get('reason', '')} · 재시작 후 확인".lstrip(" ·")
            if pending["side"] == "buy":
                trade = self.book.apply_buy(sid, fill["qty"], fill["price"], fill["fee"], reason)
                if pending.get("day"):
                    self._set_sstate(sid, last_buy_day=pending["day"])
            else:
                trade = self.book.apply_sell(sid, fill["qty"], fill["price"], fill["fee"], fill["tax"], reason)
            self.executor.clear_pending()
            self.db.add_trade(self.executor.mode, pending["symbol"], trade)
            self.event("warning", f"[실거래] 끊기기 전에 낸 {pending['symbol']} {side} 주문이 체결돼 있어서 장부에 "
                                  f"반영했어요 ({fill['qty']:,}주 @ {fill['price']:,.0f}원)", notify=True)
        else:
            self.executor.clear_pending()
            self.event("info", f"[실거래] 끊기기 전 {pending['symbol']} {side} 주문은 체결되지 않았어요. 이어서 매매해요",
                       notify=True)

    def _sync(self, sess: str) -> None:
        """실거래: 토스증권 실제 현금·주식으로 장부를 맞춘다 (입금·출금, 원래 있던 주식, 직접 사고판 것)."""
        if self.executor.mode != "live":
            return
        busy = sess in ("open", "closing")
        if self.book.data.get("synced") and time.monotonic() - self.last_sync < (SYNC_SEC if busy else IDLE_REFRESH_SEC):
            return
        cash = self._call(self.client.cash)
        holdings = self._call(self.client.holdings)
        mine = {s.symbol for s in self.slots}
        first = not self.book.data.get("synced")
        changes = self.book.sync(cash, holdings, self.prices)
        self.last_sync = time.monotonic()
        self.broker = {"cash": cash, "checked_at": ts(),
                       "other": [{"symbol": sym, "name": h.get("name") or sym, "qty": h["qty"]}
                                 for sym, h in holdings.items() if sym not in mine and h.get("qty")]}
        if first:
            self.event("info", f"[실거래] 토스증권 계좌를 연결했어요 · 현금 {cash:,.0f}원은 금고로", notify=True)
        elif changes["cash"]:
            kind = "입금" if changes["cash"] > 0 else "출금"
            self.event("info", f"[실거래] 계좌에 {kind}(또는 직접 매매)이 있었어요 · {changes['cash']:+,.0f}원을 금고에 반영했어요",
                       notify=True)
        if changes["added"]:
            self._load_slots()  # 봇이 모르는 주식을 모을 바이앤홀드 계좌를 새로 만들었다
        slots = self.book.data["slots"]
        for sym, delta in changes["shares"].items():
            hold = next((slots[x]["name"] for x in slots if slots[x]["symbol"] == sym and self.book.is_hold(x)), sym)
            what = (f"원래 있던(또는 직접 산) {delta:,}주를 '{hold}' 계좌(바이앤홀드)로 가져왔어요" if delta > 0
                    else f"{sym} {-delta:,}주가 밖에서 팔렸어요 (바이앤홀드 계좌부터 뺐어요)")
            self.event("info", f"[실거래] {what}", notify=True)

    # ---- 웹페이지 요청 ----

    def _handle_requests(self, today: str) -> None:
        for key, request in self.db.states(REQUEST_PREFIX):
            try:
                message = self._apply_request(request, today)
                ok = True
            except VaultError as e:
                message, ok = str(e), False
            except (KeyError, TypeError, ValueError) as e:
                message, ok = f"잘못된 요청이에요 ({e})", False
            self.db.delete_state(key)
            results = (self.db.get_state("request_results") or [])[-(MAX_RESULTS - 1):]
            results.append({"id": request.get("id"), "ok": ok, "message": message, "at": ts()})
            self.db.set_state("request_results", results)
            self.event("info" if ok else "warning", message)

    @staticmethod
    def _rule_text(rule: dict) -> str:
        return rule_label(rule, lambda n: STOCK_STRATEGIES[n].label)

    def _apply_request(self, r: dict, today: str) -> str:
        action = r["action"]
        if action == "mode":
            mode = r["mode"]
            if mode not in ("paper", "live"):
                raise VaultError("계좌를 다시 골라 주세요")
            if mode == self.settings["mode"]:
                return "이미 그 계좌예요"
            self.settings["mode"] = mode
            self.db.set_state("settings", self.settings)
            self.executor = self.book = None
            self.slots = []
            self._ready()
            if mode == "live":
                self._sync("pre")  # 실거래 금고를 바로 실제 현금에 맞춘다 (처음이면 계좌 현금 전부가 금고로)
            label = "실거래" if mode == "live" else "모의투자"
            self._notify(f"계좌를 {label}로 바꿨어요")
            return f"계좌를 {label}로 바꿨어요. {label} 금고와 종목 계좌를 써요"
        if action == "paper_cash":
            if self.executor.mode != "paper":
                raise VaultError("가상 입금·출금은 모의투자에서만 돼요")
            amount = float(r["amount"])
            self.book.deposit(amount)
            return f"모의 금고에 {'입금' if amount > 0 else '출금'} {abs(amount):,.0f}원"
        if action == "add":
            symbol = str(r["symbol"])
            if not SYMBOL_PATTERN.match(symbol):
                raise VaultError(f"잘못된 종목 코드예요: {symbol}")
            rule, problem = check_rule(r, STOCK_STRATEGIES)  # 살 때·팔 때 전략
            stop, stop_problem = stoploss.parse(r.get("stop_loss"))
            if problem or stop_problem:
                raise VaultError(problem or stop_problem)
            if len(self.book.data["slots"]) >= MAX_SLOTS:
                raise VaultError(f"종목 계좌는 {MAX_SLOTS}개까지 만들 수 있어요")
            name = str(r.get("name") or symbol)[:40]
            amount = float(r.get("amount") or 0)
            if amount > self.book.vault + 0.5:
                raise VaultError(f"금고에 {self.book.vault:,.0f}원밖에 없어요")
            sid = new_slot_id()
            self.book.add_slot(sid, symbol, name, rule)
            self.book.set_stop(sid, stop)
            if amount > 0:
                self.book.transfer(sid, amount)
            self._load_slots()
            self.last_sync = 0.0  # 실거래면 원래 있던 이 종목 주식을 바로 가져오게
            return (f"{name} 종목 계좌를 만들었어요 · {self._rule_text(rule)}" + (f" · {stoploss.text(stop)}" if stop else "")
                    + (f" · 금고에서 {amount:,.0f}원" if amount > 0 else ""))
        sid = str(r["slot"])
        if not self.book.has(sid):
            raise VaultError("그 종목 계좌가 없어요")
        name = self.book.slot(sid)["name"]
        if action == "remove":  # 지우기: 잔액도 주식도 없는(지켜보기) 계좌만
            cash = self.book.slot(sid)["cash"]
            if self.book.slot(sid)["qty"] <= 0 and cash >= 1:
                raise VaultError(f"잔액 {cash:,.0f}원이 남아 있어서 지울 수 없어요. '팔고 빼기'로 금고에 먼저 빼 주세요")
            self.book.remove_slot(sid)  # 주식이 남아 있으면 못 지운다
            self._drop_sstate(sid)
            self._load_slots()
            return f"{name} 종목 계좌를 지웠어요"
        if action == "close":  # 팔고 빼기: 장중에 주식을 다 팔고 잔액을 금고로 뺀다 (계좌는 0원 지켜보기로 남는다)
            if self.book.slot(sid)["qty"] <= 0:
                moved = self._withdraw(sid)
                return (f"{name}: 잔액 {moved:,.0f}원을 금고로 뺐어요 · 계좌는 0원 지켜보기로 남아요" if moved
                        else f"{name}: 뺄 돈이 없어요 · 이미 0원 지켜보기예요")
            self.book.set_closing(sid, True)
            self._load_slots()
            return (f"{name}: 주식 {self.book.slot(sid)['qty']:,}주를 팔고 나서 잔액을 금고로 빼요 "
                    "(장중이면 바로, 아니면 다음 장 시작에) · 계좌는 지켜보기로 남아요")
        if action == "cancel_close":
            self.book.set_closing(sid, False)
            self._load_slots()
            return f"{name}: 팔고 빼기를 취소했어요 · 다시 전략대로 사고팔아요"
        if action == "strategy":
            rule, problem = check_rule(r, STOCK_STRATEGIES)
            stop, stop_problem = stoploss.parse(r.get("stop_loss"))
            if problem or stop_problem:
                raise VaultError(problem or stop_problem)
            same_rule = normalize_rule(self.book.slot(sid)) == rule
            self.book.set_rule(sid, rule)
            self.book.set_stop(sid, stop)
            if not same_rule:
                self._drop_sstate(sid)  # 새 전략이 처음부터 판단한다 (장중이면 지금, 아니면 다음 장 시작 때)
            self._load_slots()
            if same_rule:
                return f"{name} 손절선을 바꿨어요 · " + (stoploss.text(stop) if stop else "안 씀")
            return (f"{name} 전략을 바꿨어요 · {self._rule_text(rule)}" + (f" · {stoploss.text(stop)}" if stop else "")
                    + " · 주식은 그대로 두고 새 전략이 판단해요")
        if action == "transfer":
            s = self.book.slot(sid)
            if r.get("all"):
                amount = self.book.vault if r.get("direction") == "in" else -s["cash"]
            else:
                amount = float(r["amount"]) * (1 if r.get("direction", "in") == "in" else -1)
            if not amount:
                raise VaultError("옮길 돈이 없어요")
            self.book.transfer(sid, amount)
            arrow = "금고 → " + name if amount > 0 else name + " → 금고"
            return f"{arrow} {abs(amount):,.0f}원 옮겼어요" + (" · 매수 신호가 날 때 사요" if amount > 0 else "")
        raise VaultError(f"알 수 없는 요청이에요: {action}")

    # ---- 하루 한 번 하는 일 ----

    def _refresh_stocks(self, today: str) -> None:
        """국내 주식 종목 목록 (웹페이지 종목 검색용)."""
        if self.stocks_day == today:
            return
        self.stocks_day = today
        try:
            self.db.set_state("stocks", {"day": today, "list": self._call(self.client.stocks)})
        except toss.TossError as e:
            log.warning("종목 목록을 받지 못했어요: %s", e)

    def _update_rankings(self, today: str) -> None:
        """종목 계좌 종목은 하루 한 번, 웹페이지에서 부탁한 종목은 바로 (한 루프에 하나씩) 전략 순위를 계산한다."""
        rankings = self.db.get_state("rankings") or {}
        wanted = [key for key, _ in self.db.states("ranking_request:")]
        symbols = [k.split(":", 1)[1] for k in wanted] + [s.symbol for s in self.slots]
        for sym in symbols:
            self.db.delete_state(f"ranking_request:{sym}")
            old = rankings.get(sym) or {}
            if old.get("day") == today and old.get("bars") == REGULAR_TF \
                    and (old.get("error") or len(old.get("results") or []) == len(STOCK_STRATEGIES)):
                continue
            if not SYMBOL_PATTERN.match(sym):
                continue
            self._rank(sym, today, rankings)
            return

    def _rank(self, symbol: str, today: str, rankings: dict) -> None:
        wanted = self._rank_bars()
        try:
            ready = self._ensure_history(symbol, today, wanted)
        except toss.TossError as e:
            rankings[symbol] = {"day": today, "bars": REGULAR_TF, "error": str(e)}
            self.db.set_state("rankings", rankings)
            return
        if not ready:  # 정규장 일봉을 만드는 중: 다음 루프에서 이어서 (웹페이지가 부탁한 종목도 잊지 않게)
            self.db.set_state(f"ranking_request:{symbol}", {"at": ts()})
            return
        candles = self.db.candle_history(symbol, wanted, REGULAR_TF)
        if len(candles) < backtest.WARMUP + 30:
            rankings[symbol] = {"day": today, "bars": REGULAR_TF, "error": "상장한 지 얼마 안 돼서 과거 데이터가 부족해요"}
            self.db.set_state("rankings", rankings)
            return
        # 앞부분은 지표 계산용 (52주 신고가처럼 긴 전략도 1년치를 보고 시작). 상장이 짧으면 있는 만큼만
        warmup = max(backtest.WARMUP, min(backtest.history_needed(STOCK_STRATEGIES), len(candles) - 30))
        params = {name: strategy_params(self.cfg, name) for name in STOCK_STRATEGIES}
        results = backtest.rank_strategies(candles, self.fee_rate, self.rank_by, TRADING_DAYS_PER_YEAR, params,
                                           warmup=warmup, sell_cost=self.sell_tax, strategies=STOCK_STRATEGIES)
        rankings[symbol] = {"day": today, "bars": REGULAR_TF, "computed_at": ts(), "rank_by": self.rank_by,
                            "from": candles[warmup]["day"], "to": candles[-1]["day"],
                            "days": len(candles) - warmup, "results": results}
        self.db.set_state("rankings", rankings)
        log.info("%s 전략 순위 갱신 · 1위 %s", symbol, results[0]["label"])

    def _close_report(self, today: str) -> None:
        if self.report_day == today:
            return
        first = self.report_day is None
        self.report_day = today
        self.db.set_state("report_day", today)
        if first or not telegram.load():
            return
        trades = self.db.trades(self.executor.mode, since=f"{today} 00:00:00")
        self._notify(report_text(trades, self.db.get_state("status")), prefix="")

    # ---- 상태 (웹페이지·텔레그램용) ----

    def _state_text(self, slot: StockSlot, sess: str, today: str) -> str:
        s, plan, st = self.book.slot(slot.id), slot.plan, self._sstate(slot)
        price = self.prices.get(slot.symbol)
        if slot.closing:
            return "팔고 빼는 중 · 장중이면 바로 팔아요" if sess == "open" else "다음 장 시작(09:00)에 팔고 빼요"
        fresh = plan is not None and slot.plan_day == today and sess in ("open", "closing")
        if s["qty"] > 0:
            loss = self._loss_line(slot)
            cut = f" · {stoploss.text(slot.stop_loss)} {loss:,.0f}원" if loss is not None else ""
            if not fresh:
                return "보유 중 · 다음 장 시작(09:00)에 다시 판단" + cut
            if slot.strategy.exit_next_open:
                return "다음 장 시작 때 매도 예정" + cut
            if plan.sell_stop is not None:
                return f"{plan.sell_stop:,.0f}원 아래로 내려가면 매도" + cut
            return "보유 유지 · 매도 신호가 나면 다음 장 시작 때 매도" + cut
        if price and s["cash"] < price:
            return f"잔액이 1주 가격({price:,.0f}원)보다 적어요 · 금고에서 이체해 주세요"
        if not fresh:
            return "다음 장 시작(09:00)에 판단해요"
        if st.get("last_buy_day") == today or st.get("last_stop_day") == today:
            return "오늘 매매 완료 · 다음 장에 다시 판단"
        if st.get("cut"):
            return "손절 뒤 쉬는 중 · 매수 신호가 한 번 꺼졌다가 다시 켜지면 사요"
        if plan.buy_stop is not None:
            return f"{plan.buy_stop:,.0f}원 이상 오르면 매수"
        return "매수 신호를 기다리는 중 · 매일 09:00 판단"

    def _write_status(self, now: datetime, sess: str) -> None:
        today = now.date().isoformat()
        status = {"updated_at": ts(), "mode": self.settings["mode"], "session": sess,
                  "session_text": SESSION_TEXT[sess], "today": today,
                  "next_open": ts(krx.next_open(now, self.holidays)), "keys": self.client is not None,
                  "ip_error": self.ip_error, "slots": [], "account": None, "broker": self.broker or None}
        if self.book is not None:
            slots, changed = [], False
            profits = self.db.slot_profits(self.executor.mode)
            for slot in self.slots:
                s = self.book.slot(slot.id)
                q = self.quotes.get(slot.symbol) or {}
                price = self.prices.get(slot.symbol) or s["avg_price"]
                value = s["qty"] * price
                equity = s["cash"] + value
                if "hold" not in s:  # 바이앤홀드 비교가 생기기 전부터 있던 계좌: 첫 매매 가격부터 들고 있었다고 친다
                    first = self.db.trades(self.executor.mode, slot.symbol)[:1]
                    buyhold.start(s, first[0]["price"] if first else None)
                    changed = True
                changed |= bool(s.get("hold_cash")) and bool(self.prices.get(slot.symbol))
                hold_value = buyhold.settle(s, self.prices.get(slot.symbol))
                slots.append({
                    "id": slot.id, "symbol": slot.symbol, "name": slot.name, "price": price,
                    "change_rate": (price / q["prev_close"] - 1) * 100 if q.get("prev_close") else None,
                    "strategy": {"name": slot.strategy.name, "label": slot.strategy.label,
                                 "description": slot.strategy.description, "guide": slot.strategy.guide,
                                 "exit_next_open": slot.strategy.exit_next_open},
                    "rule": slot.rule,  # 살 때·팔 때 전략
                    "stop_loss": slot.stop_loss, "stop_price": self._loss_line(slot),  # 손절선 (들고 있을 때만 값)
                    "closing": slot.closing,  # 팔고 빼는 중
                    "plan": asdict(slot.plan) if slot.plan and slot.plan_day == today else None,
                    "state_text": self._state_text(slot, sess, today), "holding": s["qty"] > 0,
                    "cash": s["cash"], "qty": s["qty"], "avg_price": s["avg_price"], "cost": s["cost"],
                    "contrib": s["contrib"], "value": value, "equity": equity,
                    "return_pct": (equity / s["contrib"] - 1) * 100 if s["contrib"] > 0 else None,
                    "hold_return_pct": (hold_value / s["contrib"] - 1) * 100
                    if hold_value is not None and s["contrib"] > 0 else None,  # 바이앤홀드였다면
                    "hold_pnl": hold_value - s["contrib"] if hold_value is not None else None,
                    "unrealized": value - s["cost"] if s["qty"] else None,
                    "unrealized_pct": (value / s["cost"] - 1) * 100 if s["qty"] and s["cost"] > 0 else None,
                    # 판 매매로 확정된 손익 (수수료·거래세 뺀 뒤): 합계, 번 돈, 잃은 돈, 판 횟수, 이긴 횟수
                    **{k: (profits.get(slot.id) or {}).get(k) or 0 for k in ("realized", "gains", "losses", "sells", "wins")},
                })
            if changed:
                self.book.save()
            equity = self.book.equity(self.prices)
            contributions = self.book.data["contributions"]
            status["slots"] = slots
            status["account"] = {"vault": self.book.vault, "cash": self.book.cash_total(), "equity": equity,
                                 "invested": sum(x["value"] for x in slots), "contributions": contributions,
                                 "return_pct": (equity / contributions - 1) * 100 if contributions > 0 else None,
                                 "synced": self.book.data.get("synced", False)}
        self.db.set_state("status", status)

    # ---- 계속 실행 ----

    def run(self) -> None:
        self.event("info", f"📈 주식 봇 시작 · {'실거래' if self.settings['mode'] == 'live' else '모의투자'}", notify=True)
        wait = self.interval
        while not self.stop_event.is_set():
            try:
                sess = self.step()
                if self.last_error:
                    self.event("info", "정상으로 돌아왔어요")
                    self.last_error = None
                pending = self.executor is not None and getattr(self.executor, "pending", lambda: None)()
                busy = sess in ("open", "closing") or pending or self.db.states("ranking_request:")  # 순위·정규장 일봉 만드는 중이면 빨리
                wait = self.interval if busy else self.idle
            except Exception as e:  # 어떤 오류가 나도 봇은 멈추지 않고 재시도한다
                message = f"토스증권 오류: {e}" if isinstance(e, toss.TossError) else f"오류: {e!r}"
                if message != self.last_error:
                    if isinstance(e, toss.TossError):
                        self.event("warning", message)
                    else:
                        log.exception("예상치 못한 오류")
                        self.db.add_event("error", message)
                    self.last_error = message
                wait = self.interval if isinstance(e, toss.OrderUnknown) else min(wait * 2, MAX_BACKOFF_SEC)
            # 오류가 이어지는 중이거나 아직 준비 전(토스 키 없음)이면 요청이 와도 그냥 기다린다
            self._wait(wait, wake=self.last_error is None and self.executor is not None)
        self.event("info", "주식 봇 종료")

    def _wait(self, seconds: float, wake: bool = True) -> None:
        """다음 루프까지 쉰다. 웹페이지 요청이 들어오면 바로 깬다 (장 밖에서도 이체 등이 금방 처리되게)."""
        end = time.monotonic() + seconds
        while not self.stop_event.is_set() and time.monotonic() < end:
            if wake and (self.db.states(REQUEST_PREFIX) or self.db.states("ranking_request:")):
                return
            self.stop_event.wait(min(2.0, max(end - time.monotonic(), 0.0)))


def report_text(trades: list[dict], status: dict | None) -> str:
    """장 마감 리포트: 오늘 매매와 금고·종목 계좌 상태."""
    sells = [t for t in trades if t["side"] == "sell" and t.get("profit") is not None]
    realized = sum(t["profit"] for t in sells)
    lines = ["📈 주식 장 마감 리포트",
             f"오늘 매매 {len(trades)}건" + (f" · 확정 손익 {realized:+,.0f}원" if sells else "")]
    names = {s["symbol"]: s["name"] for s in (status or {}).get("slots", [])}
    for t in trades:
        side = "🔴 매수" if t["side"] == "buy" else "🔵 매도"
        line = f"{t['ts'][11:16]} {side} {names.get(t['market'], t['market'])} {t['volume']:,.0f}주 @{t['price']:,.0f}원"
        if t.get("profit") is not None:
            line += f" · {t['profit']:+,.0f}원"
        lines.append(line)
    return "\n".join(lines) + "\n\n" + status_text(status)


def status_text(status: dict | None) -> str:
    """텔레그램 /주식 답장과 마감 리포트에 쓰는 금고·종목 계좌 요약."""
    if not status or not status.get("account"):
        return "📈 주식 봇이 아직 준비되지 않았어요 (토스증권 키를 등록하면 시작해요)."
    a = status["account"]
    mode = "실거래" if status["mode"] == "live" else "모의투자"
    ret = f"{a['return_pct']:+.2f}%" if a.get("return_pct") is not None else "-"
    lines = [f"📈 주식 총 자산 {a['equity']:,.0f}원 ({ret}) · {mode} · {status['session_text']}",
             f"🏦 금고 {a['vault']:,.0f}원"]
    for s in status["slots"]:
        held = f"🟢 {s['qty']:,}주" if s["holding"] else "⏳ 대기"
        slot_ret = f" · {s['return_pct']:+.2f}%" if s.get("return_pct") is not None else ""
        lines.append(f"\n• {s['name']} · {s['strategy']['label']}")
        lines.append(f"  {held} · 잔액 {s['cash']:,.0f}원 · 현재가 {s['price']:,.0f}원{slot_ret}")
        lines.append(f"  {s['state_text']}")
    lines.append(f"\n({status['updated_at'][5:16]} 기준)")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="My_Pie 주식 자동매매 봇 (토스증권)")
    parser.add_argument("--once", action="store_true", help="한 번만 실행하고 상태를 출력")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    bot = StockBot(cfg, Database(cfg["stock"]["db_path"]))
    if args.once:
        bot.step()
        print(json.dumps(bot.db.get_state("status"), ensure_ascii=False, indent=2))
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: bot.stop_event.set())
    bot.run()


if __name__ == "__main__":
    main()
