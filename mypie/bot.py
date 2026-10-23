"""코인 자동매매 봇 본체 (금고 + 코인 계좌).

실행: python3 -m mypie.bot          (계속 실행)
      python3 -m mypie.bot --once   (한 번만 돌고 상태 출력, 점검용)

돈은 금고와 코인 계좌 여러 개로 나눠 쓴다 (broker.Book 참고, 주식 봇과 같은 생각).
코인 계좌마다 코인 하나 + 전략(살 때·팔 때) + 판단 주기 + 손절선이 있고, 자기 원화로만 따로 판단하고 사고판다.
웹페이지 요청(계좌 만들기·이체·전략 바꾸기·팔고 빼기·지우기·계좌 전환)은 하나씩 처리한다.

계좌마다 판단 주기(일봉·4시간봉·1시간봉·15분봉)가 있다. 그 주기의 새 봉이 시작될 때(일봉은 매일 09:00 KST)
계획을 세우고, 매도 신호가 난 계좌를 먼저 판 다음 매수 신호가 난 계좌를 산다 ('보유 유지'는 그대로).
그 뒤 10초마다 현재가를 보면서 돌파 매수·이탈 매도·손절을 한다.

전원이 나갔다 들어오거나 재시작돼도 멈추지 않는다. 끊기기 전에 낸 주문이 있으면 업비트에 물어서
장부를 먼저 맞추고(recover), 곧바로 같은 설정으로 이어서 매매한다.
"""

import argparse
import json
import logging
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import timedelta

from . import backtest, buyhold, network, reports, sshwatch, stoploss, telegram, timeframes, upbit
from .broker import MIN_ORDER_KRW, Book, LiveExecutor, PaperExecutor, migrate_book, new_slot_id
from .config import load_config
from .db import Database
from .history import sync_candles
from .keys import days_left, expired_text, expiring_text, load_keys
from .strategies import STRATEGIES, DayPlan, create_strategy, strategy_params
from .strategies.combo import check_rule, make_rule, normalize_rule, rule_label
from .timeframes import bar_key, next_bar
from .timeutil import KST, now_kst, trading_day, ts
from .vault import VaultError

log = logging.getLogger("mypie.bot")

EQUITY_SNAPSHOT_SEC = 300   # 자산 추이 기록 간격
EXCHANGE_REFRESH_SEC = 60   # 실거래일 때 업비트 실제 잔고 확인 간격
MARKET_INFO_SEC = 60        # 화면용 시세 요약·급등락 갱신 간격
IP_CHECK_SEC = 600          # 집 공인 IP 확인 간격
MAX_BACKOFF_SEC = 300       # 오류가 계속될 때 최대 대기 시간
MAX_SLOTS = 20
REQUEST_PREFIX = "request:"  # 웹페이지가 남기는 요청 (request:<시각>-<번호>)
MAX_RESULTS = 20
KEY_WARN_DAYS = (30, 14, 7, 3, 1)  # 업비트 키 만료 며칠 전에 텔레그램으로 알릴지
MARKET_PATTERN = re.compile(r"^KRW-[A-Z0-9]{1,15}$")


class Slot:
    """코인 계좌 하나의 전략과 이번 봉 계획 (돈과 코인은 장부 Book에 있다)."""

    def __init__(self, cfg: dict, sid: str, entry: dict):
        self.id = sid
        self.market = entry["market"]
        self.name = entry.get("name") or self.market
        self.tf = entry.get("tf") or timeframes.DEFAULT
        self.rule = normalize_rule(entry)  # 살 때·팔 때 전략
        bar = timeframes.TIMEFRAMES[self.tf]["bar"]
        self.strategy = make_rule(self.rule, lambda n: create_strategy(n, {**strategy_params(cfg, n), "_bar": bar}))
        self.stop_loss = float(entry.get("stop_loss") or 0)  # 손절선 % (0이면 안 씀)
        self.closing = bool(entry.get("closing"))  # 팔고 빼는 중
        self.plan: DayPlan | None = None
        self.plan_day: str | None = None  # 계획을 세운 봉의 키 (일봉이면 날짜)

    @property
    def coin(self) -> str:
        return self.market.split("-")[-1]

    @property
    def rank_key(self) -> str:
        return ranking_key(self.market, self.tf)


def ranking_key(market: str, tf: str) -> str:
    """전략 순위는 종목 × 판단 주기마다 따로다."""
    return f"{market}|{tf}"


def check_account(r: dict) -> tuple[dict, str, float]:
    """웹페이지에서 온 계좌 설정(살 때·팔 때 전략, 판단 주기, 손절선)을 검사한다. 잘못이면 VaultError."""
    rule, problem = check_rule(r, STRATEGIES)
    if problem:
        raise VaultError(problem)
    tf = str(r.get("tf") or timeframes.DEFAULT)
    if tf not in timeframes.TIMEFRAMES:
        raise VaultError(f"알 수 없는 판단 주기: {tf}")
    stop, problem = stoploss.parse(r.get("stop_loss"))
    if problem:
        raise VaultError(problem)
    return rule, tf, stop


class Bot:
    def __init__(self, cfg: dict, db: Database, executor_factory=None):
        self.cfg = cfg
        self.db = db
        self.executor_factory = executor_factory or self._make_executor
        self.notifier = telegram.Notifier()  # 텔레그램 알림 (연결돼 있고 알림을 켰을 때만 보낸다)
        self.interval = float(cfg["general"]["interval_sec"])
        self.fee_rate = float(cfg["paper"]["fee_rate"])
        self.backtest_days = int(cfg["backtest"]["days"])
        self.rank_by = cfg["backtest"]["rank_by"]

        self.settings = self._load_settings()
        try:
            self.executor = self.executor_factory(self.settings["mode"])
        except Exception as e:  # 예: 실거래인데 키가 없어짐 → 모의투자로 안전하게 시작
            self.event("error", f"실거래 계좌를 열 수 없어서 모의투자로 시작합니다: {e}")
            self.settings["mode"] = "paper"
            self.executor = self.executor_factory("paper")
        self.db.set_state("settings", self.settings)
        self.book = self._open_book()
        self.slots: list[Slot] = []
        self._load_slots()

        self.prices: dict[str, float] = {}
        stored = db.get_state("markets") or {}
        self.markets_day: str | None = stored.get("day") if stored.get("all") else None  # 예전(상위 30개) 목록이면 바로 다시 받는다
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
        if "slots" in saved:  # 비중(%) 포트폴리오 시절 설정 → 금고 + 코인 계좌로 한 번 옮긴다
            self._migrate_to_vault(saved)
        mode = saved.get("mode", self.cfg["general"]["mode"])
        return {"mode": mode if mode in ("paper", "live") else "paper"}

    def _migrate_to_vault(self, saved: dict) -> None:
        """줄마다의 원화·코인은 그대로 그 코인 계좌 것으로, 대기 원화는 금고로 옮긴다.
        예전 설정과 장부는 backup:pre_vault에 그대로 남겨 둔다."""
        names = self._market_names()
        defs = {c["id"]: {**c, "name": names.get(c["market"]) or c["market"].split("-")[-1]}
                for c in saved.get("slots", [])}
        backup = {"settings": saved, "at": ts()}
        for key in ("book:paper", "book:live"):
            data = self.db.get_state(key)
            if data is not None and "reserve" in data:
                backup[key] = data
                self.db.set_state(key, migrate_book(data, defs))
        if self.db.get_state("backup:pre_vault") is None:
            self.db.set_state("backup:pre_vault", backup)
        self.event("info", "포트폴리오(비중)를 금고 + 코인 계좌로 옮겼어요 · 줄마다의 원화·코인은 그 계좌로, "
                           "대기 원화는 금고로", notify=True)

    def _market_names(self) -> dict:
        """종목 → 한글 이름 (업비트 종목 목록, 하루 한 번 받아 둔다)."""
        return {m["market"]: m.get("name") or m["market"] for m in (self.db.get_state("markets") or {}).get("list", [])}

    def _make_executor(self, mode: str):
        if mode == "live":
            keys = load_keys()
            if not keys:
                raise upbit.UpbitError("업비트 API 키가 없어요. 파이에서 python3 -m mypie.keys 로 등록하세요")
            return LiveExecutor(self.db, upbit.UpbitClient(*keys), self.fee_rate)
        return PaperExecutor(self.fee_rate)

    def _open_book(self) -> Book:
        """모의투자와 실거래는 금고·코인 계좌가 따로다."""
        if self.executor.mode == "paper":
            return Book(self.db, "book:paper", float(self.cfg["paper"]["initial_krw"]))
        return Book(self.db, "book:live", 0.0)

    def _load_slots(self) -> None:
        """장부의 코인 계좌로 Slot을 만든다. 종목·전략·주기가 그대로인 계좌는 이번 봉 계획을 이어 쓴다."""
        old = {s.id: s for s in self.slots}
        slots = []
        for sid, entry in self.book.data["slots"].items():
            slot = Slot(self.cfg, sid, entry)
            prev = old.get(sid)
            if prev and prev.market == slot.market and prev.rule == slot.rule and prev.tf == slot.tf:
                slot.plan, slot.plan_day = prev.plan, prev.plan_day
            slots.append(slot)
        self.slots = slots

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
                    step = reports.reboot_step(message.get("text", ""))
                    if step == "do":
                        telegram.send("🔄 재부팅해요. 1~2분 뒤 봇이 다시 시작하면 알려드려요.", creds)
                        self.event("info", "텔레그램 명령으로 라즈베리파이를 재부팅해요")
                        self._reboot(creds)
                        continue
                    telegram.send(reports.REBOOT_ASK if step == "ask"
                                  else reports.command_reply(message.get("text", ""), db, self.cfg["stock"]["db_path"]), creds)
                except telegram.TelegramError as e:
                    log.warning("텔레그램 답장 실패: %s", e)

    def _reboot(self, creds) -> None:
        """파이를 다시 켠다. 비밀번호 없이 되는 방법을 차례로 시도하고, 다 안 되면 텔레그램으로 알린다."""
        for cmd in (["sudo", "-n", "/sbin/reboot"], ["systemctl", "reboot"]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                continue
        try:
            telegram.send("⚠️ 재부팅하지 못했어요 (권한이 없어요). 파이에서 한 번만 이렇게 해 주세요:\n"
                          'echo "$USER ALL=(root) NOPASSWD: /sbin/reboot" | sudo tee /etc/sudoers.d/mypie-reboot', creds)
        except telegram.TelegramError:
            pass

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
        return self.book.slot(slot.id)["coin"] * price >= MIN_ORDER_KRW

    def _tag(self, slot: Slot) -> str:
        return f"[{'실거래' if self.executor.mode == 'live' else '모의'} {slot.coin}] "

    # ---- 한 번의 루프 ----

    def step(self) -> None:
        day = trading_day()
        self._check_network()
        self._recover()  # 끊기기 전 주문이 있으면 장부부터 맞춘다 (확인 못 하면 여기서 멈추고 다음 루프에 재시도)
        self._handle_requests()
        markets = sorted({s.market for s in self.slots})
        prices = upbit.get_prices(markets) if markets else {}
        self.prices.update(prices)
        self._sync(prices)
        for s in [s for s in self.slots if s.closing and s.market in prices]:
            self._close_out(s, prices[s.market])

        bars = {s.id: self._bar(s) for s in self.slots}
        ready = [s for s in self.slots
                 if not s.closing and (s.plan_day == bars[s.id] or self._start_bar(s, bars[s.id]))]
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
        sold = set()
        for s in todo:
            if self._holding(s, prices[s.market]) and s.plan.sell_at_open:
                if s.strategy.exit_next_open:
                    reason = "산 다음 날 09시 매도" if s.tf == "1d" else "산 다음 봉 시작에 매도"
                else:
                    reason = f"매도 신호 · {s.plan.note}"
                self._sell(s, prices[s.market], reason)
                self._set_sstate(s.id, open_done_day=bars[s.id])
                sold.add(s.id)
        for s in todo:
            cut = self._sstate(s).get("cut")
            if cut and not s.plan.buy_at_open:
                cut = False  # 손절 뒤 매수 신호가 한 번 꺼졌다 → 다음 신호부터 다시 산다
                self._set_sstate(s.id, cut=False)
            # 방금 판 줄은 같은 봉 시작에 다시 사지 않는다 (살 때·팔 때 조합에서 사자·팔자가 같이 켜진 경우. 시뮬레이션과 같은 규칙)
            if s.id not in sold and not cut and not self._holding(s, prices[s.market]) and s.plan.buy_at_open:
                self._buy(s, prices[s.market], bars[s.id], f"매수 신호 · {s.plan.note}")
            self._set_sstate(s.id, open_done_day=bars[s.id])  # 재시작해도 봉 시작 매매를 두 번 하지 않게

    def _loss_line(self, slot: Slot) -> float | None:
        b = self.book.slot(slot.id)
        return stoploss.line(b["cost"], b["coin"], slot.stop_loss)

    def _close_out(self, slot: Slot, price: float) -> None:
        """'팔고 빼기'를 누른 계좌: 들고 있는 코인을 팔고, 다 팔렸으면 원화를 금고로 뺀다 (계좌는 0원 지켜보기로 남는다)."""
        if self._holding(slot, price):
            self._sell(slot, price, "팔고 빼기")
        if not self._holding(slot, price):
            moved = self._withdraw(slot.id)
            self.event("info", f"{self._tag(slot)}다 팔고 원화 {moved:,.0f}원을 금고로 뺐어요 · "
                               "계좌는 0원 지켜보기로 남아요 (필요 없으면 '지우기')", notify=True)

    def _withdraw(self, sid: str) -> float:
        """팔고 빼기 마무리: 원화를 전부 금고로 빼고 계좌는 지켜보기로 남긴다. 뺀 금액을 돌려준다."""
        if self.book.slot(sid)["coin"] > 0:
            self.book.clear_dust(sid)  # 최소 주문 금액보다 적은 자투리는 장부에서만 정리
        moved = self.book.slot(sid)["krw"]
        if moved > 0:
            self.book.transfer(sid, -moved)
        self.book.set_closing(sid, False)
        self._load_slots()
        return moved

    def _intraday(self, slot: Slot, price: float, bar: str) -> None:
        plan, st = slot.plan, self._sstate(slot)
        if self._holding(slot, price):
            loss = self._loss_line(slot)  # 손절선은 산 직후에도 지킨다
            if loss is not None and price <= loss:
                self._sell(slot, price, f"{stoploss.text(slot.stop_loss)} ({loss:,.0f}원) 이탈")
                self._set_sstate(slot.id, last_stop_day=bar, cut=True)
                return
            if plan.sell_stop is not None and st.get("last_buy_day") != bar and price <= plan.sell_stop:
                self._sell(slot, price, f"{plan.sell_stop:,.0f}원 이탈")
                self._set_sstate(slot.id, last_stop_day=bar)
                return
        elif (plan.buy_stop is not None and price >= plan.buy_stop
              and st.get("last_buy_day") != bar and st.get("last_stop_day") != bar):
            self._buy(slot, price, bar, f"{plan.buy_stop:,.0f}원 돌파")

    # ---- 주문 ----
    # 주문 오류는 그대로 위로 올려 보낸다. 루프가 잠시 기다렸다 다시 시도하고,
    # 결과를 모르는 주문은 다음 루프의 _recover()가 업비트에 물어서 정리한다.

    def _buy(self, slot: Slot, price: float, day: str, reason: str) -> bool:
        """그 계좌 원화로만 산다. 원화가 없는 계좌는 지켜보기만 한다."""
        amount = self.book.slot(slot.id)["krw"]
        if amount < MIN_ORDER_KRW:
            if amount > 0 and (slot.id, day) not in self.warned:
                self.warned.add((slot.id, day))
                self.event("warning", f"{self._tag(slot)}매수 신호가 났지만 계좌 원화 {amount:,.0f}원이 최소 주문 금액 "
                                      f"{MIN_ORDER_KRW:,}원보다 적어요 · 금고에서 넣어 주세요")
            return False
        note = {"slot": slot.id, "reason": reason, "day": day}
        volume, funds, fee = self.executor.buy(slot.market, amount, price, note)
        trade = self.book.apply_buy(slot.id, volume, funds, fee, reason)
        self.executor.clear_pending()
        self._set_sstate(slot.id, last_buy_day=day, cut=False)
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
            slot_id = pending.get("slot")
            volume, funds, _ = result["fill"]
            if not self.book.has(slot_id):  # 그사이 계좌가 없어졌으면 잔고 맞추기가 코인·원화를 금고·바이앤홀드로 정리한다
                self.executor.clear_pending()
                self.event("warning", f"[실거래] 끊기기 전에 낸 {market} {side} 주문이 체결돼 있었지만 그 계좌가 없어서 "
                                      "잔고 맞추기로 정리해요", notify=True)
                return
            reason = f"{pending.get('reason', '')} · 재시작 후 확인".lstrip(" ·")
            apply = self.book.apply_buy if pending["side"] == "bid" else self.book.apply_sell
            trade = apply(slot_id, *result["fill"], reason)
            self.executor.clear_pending()
            self.db.add_trade(self.executor.mode, market, trade)
            if pending["side"] == "bid" and pending.get("day"):
                self._set_sstate(slot_id, last_buy_day=pending["day"])
            self.event("warning", f"[실거래] 끊기기 전에 낸 {market} {side} 주문이 체결돼 있어서 장부에 반영했어요 "
                                  f"({volume:.8g} @ {funds / volume:,.0f}원)", notify=True)
        else:
            self.event("info", f"[실거래] 끊기기 전 {market} {side} 주문은 들어가지 않았어요. 그대로 이어서 매매해요",
                       notify=True)

    def _sync(self, prices: dict) -> None:
        """실거래: 업비트 실제 잔고로 장부를 맞춘다 (입금·출금은 금고로, 봇이 모르는 코인은 바이앤홀드 계좌로)."""
        if self.executor.mode != "live":
            return
        try:
            accounts = self.executor.client.accounts()
        except upbit.UpbitError as e:
            if network.is_ip_error(e):
                self._mark_ip_error()
            raise
        self._mark_upbit_ok()
        first = not self.book.data.get("synced")
        changes = self.book.sync(accounts, prices, self._market_names())
        if changes["added"]:
            self._load_slots()  # 봇이 모르는 코인을 모을 바이앤홀드 계좌를 새로 만들었다
        if first:
            self.event("info", f"[실거래] 업비트 계좌를 연결했어요 · 원화 {self.book.vault:,.0f}원은 금고로", notify=True)
        elif changes["cash"]:
            kind = "입금" if changes["cash"] > 0 else "출금"
            self.event("info", f"[실거래] 계좌에 {kind}(또는 직접 매매)이 있었어요 · {changes['cash']:+,.0f}원을 금고에 "
                               "반영했어요 (넣은 돈 합계에도 반영해서 수익률이 틀어지지 않게)", notify=True)
        slots = self.book.data["slots"]
        for market, delta in changes["coins"].items():
            coin = market.split("-")[-1]
            hold = next((slots[x]["name"] for x in slots if slots[x]["market"] == market and self.book.is_hold(x)), coin)
            what = (f"원래 있던(또는 직접 산) {coin} {delta:.8g}개를 '{hold}' 계좌(바이앤홀드)로 가져왔어요" if delta > 0
                    else f"{coin} {-delta:.8g}개가 밖에서 팔렸어요 (바이앤홀드 계좌부터 뺐어요)")
            self.event("info", f"[실거래] {what}", notify=True)

    # ---- 웹페이지 요청 ----

    def _handle_requests(self) -> None:
        """웹페이지 요청을 하나씩 처리하고 결과를 남긴다. 통신 오류면 요청을 남겨 두고 다음 루프에서 다시."""
        for key, request in self.db.states(REQUEST_PREFIX):
            try:
                message, ok = self._apply_request(request), True
            except VaultError as e:
                message, ok = str(e), False
            except (KeyError, TypeError, ValueError) as e:
                message, ok = f"잘못된 요청이에요 ({e})", False
            self.db.delete_state(key)
            results = (self.db.get_state("request_results") or [])[-(MAX_RESULTS - 1):]
            results.append({"id": request.get("id"), "ok": ok, "message": message, "at": ts()})
            self.db.set_state("request_results", results)
            self.event("info" if ok else "warning", message, notify=ok)

    def _rule_text(self, rule: dict, tf: str, stop: float) -> str:
        text = rule_label(rule, lambda n: STRATEGIES[n].label) + f" · {timeframes.TIMEFRAMES[tf]['short']}"
        return text + (f" · {stoploss.text(stop)}" if stop else "")

    def _apply_request(self, r: dict) -> str:
        action = r["action"]
        if action == "mode":
            return self._switch_mode(r["mode"])
        if action == "paper_cash":
            if self.executor.mode != "paper":
                raise VaultError("가상 입금·출금은 모의투자에서만 돼요")
            amount = float(r["amount"])
            self.book.deposit(amount)
            return f"모의 금고에 {'입금' if amount > 0 else '출금'} {abs(amount):,.0f}원"
        if action == "add":
            market = str(r["market"])
            if not MARKET_PATTERN.match(market):
                raise VaultError(f"잘못된 종목이에요: {market}")
            rule, tf, stop = check_account(r)
            if len(self.book.data["slots"]) >= MAX_SLOTS:
                raise VaultError(f"코인 계좌는 {MAX_SLOTS}개까지 만들 수 있어요")
            amount = float(r.get("amount") or 0)
            if amount > self.book.vault + 0.5:
                raise VaultError(f"금고에 {self.book.vault:,.0f}원밖에 없어요")
            if market not in self.prices:
                try:
                    upbit.get_prices([market])  # 업비트에 진짜 있는 종목인지
                except upbit.UpbitError as e:
                    if e.status in (400, 404):
                        raise VaultError(f"업비트에 없는 종목이에요: {market}") from e
                    raise  # 통신 오류면 요청을 남겨 두고 다음 루프에서 다시
            sid = new_slot_id()
            name = str(r.get("name") or self._market_names().get(market) or market.split("-")[-1])[:40]
            self.book.add_slot(sid, market, name, rule, tf)
            self.book.set_stop(sid, stop)
            if amount > 0:
                self.book.transfer(sid, amount)
            self._load_slots()
            return (f"{self.book.slot(sid)['name']} 계좌를 만들었어요 · {self._rule_text(rule, tf, stop)}"
                    + (f" · 금고에서 {amount:,.0f}원" if amount > 0 else ""))

        sid = str(r["slot"])
        if not self.book.has(sid):
            raise VaultError("그 코인 계좌가 없어요")
        s = self.book.slot(sid)
        name = s["name"]
        price = self.prices.get(s["market"]) or s["avg_price"]
        holding = s["coin"] * price >= MIN_ORDER_KRW
        if action == "remove":  # 지우기: 원화도 코인도 없는(지켜보기) 계좌만
            if holding:
                raise VaultError("코인이 남아 있어서 지울 수 없어요. '팔고 빼기'를 먼저 써 주세요")
            if s["krw"] >= 1:
                raise VaultError(f"원화 {s['krw']:,.0f}원이 남아 있어서 지울 수 없어요. '팔고 빼기'로 금고에 먼저 빼 주세요")
            if s["coin"] > 0:
                self.book.clear_dust(sid)  # 팔 수 없는 자투리는 장부에서만 정리
            self.book.remove_slot(sid)
            self._drop_sstate(sid)
            self._load_slots()
            return f"{name} 계좌를 지웠어요"
        if action == "close":  # 팔고 빼기: 코인을 바로 다 팔고 원화를 금고로 뺀다 (계좌는 0원 지켜보기로 남는다)
            if not holding:
                moved = self._withdraw(sid)
                return (f"{name}: 원화 {moved:,.0f}원을 금고로 뺐어요 · 계좌는 0원 지켜보기로 남아요" if moved
                        else f"{name}: 뺄 돈이 없어요 · 이미 0원 지켜보기예요")
            self.book.set_closing(sid, True)
            self._load_slots()
            return f"{name}: 코인을 다 팔고 나서 원화를 금고로 빼요 · 계좌는 지켜보기로 남아요"
        if action == "cancel_close":
            self.book.set_closing(sid, False)
            self._load_slots()
            return f"{name}: 팔고 빼기를 취소했어요 · 다시 전략대로 사고팔아요"
        if action == "strategy":
            rule, tf, stop = check_account(r)
            same = normalize_rule(s) == rule and s.get("tf") == tf
            self.book.set_rule(sid, rule, tf)
            self.book.set_stop(sid, stop)
            if not same:
                self._drop_sstate(sid)  # 새 전략이 처음부터 판단한다 (들고 있는 코인은 그대로)
            self._load_slots()
            if same:
                return f"{name} 손절선을 바꿨어요 · " + (stoploss.text(stop) if stop else "안 씀")
            return f"{name} 전략을 바꿨어요 · {self._rule_text(rule, tf, stop)} · 코인은 그대로 두고 새 전략이 판단해요"
        if action == "transfer":
            if r.get("all"):
                amount = self.book.vault if r.get("direction") == "in" else -s["krw"]
            else:
                amount = float(r["amount"]) * (1 if r.get("direction", "in") == "in" else -1)
            if not amount:
                raise VaultError("옮길 돈이 없어요")
            self.book.transfer(sid, amount)
            arrow = "금고 → " + name if amount > 0 else name + " → 금고"
            return f"{arrow} {abs(amount):,.0f}원 옮겼어요" + (" · 매수 신호가 날 때 사요" if amount > 0 else "")
        raise VaultError(f"알 수 없는 요청이에요: {action}")

    def _switch_mode(self, mode: str) -> str:
        """모의투자 ↔ 실거래. 금고·코인 계좌는 따로라서, 바꿔도 아무것도 팔지 않는다."""
        if mode not in ("paper", "live"):
            raise VaultError("계좌를 다시 골라 주세요")
        if mode == self.settings["mode"]:
            return "이미 그 계좌예요"
        if mode == "live":
            keys = load_keys()
            try:
                if not keys:
                    raise upbit.UpbitError("API 키가 없어요. 파이에서 python3 -m mypie.keys 로 등록하세요")
                upbit.UpbitClient(*keys).accounts()  # 키와 허용 IP가 맞는지 미리 확인
            except upbit.UpbitError as e:
                if network.is_ip_error(e):
                    self._mark_ip_error()
                raise VaultError(f"실거래로 바꾸지 못했어요 · {e}") from e
            self._mark_upbit_ok()
        self.settings["mode"] = mode
        self.db.set_state("settings", self.settings)
        self.executor = self.executor_factory(mode)
        self.book = self._open_book()
        self.slots = []
        self._load_slots()
        self.exchange = None
        self.last_exchange_check = self.last_snapshot = 0.0
        self.key_day = None  # 키 만료일도 바로 다시 확인
        label = "실거래" if mode == "live" else "모의투자"
        return f"계좌를 {label}로 바꿨어요 · {label} 금고와 코인 계좌를 써요"

    def _describe(self) -> str:
        account = "실거래" if self.settings["mode"] == "live" else "모의투자"
        parts = ", ".join(f"{s.name} {s.strategy.label}"
                          f"{'' if s.tf == '1d' else '(' + timeframes.TIMEFRAMES[s.tf]['short'] + ')'}"
                          f"{' ' + stoploss.text(s.stop_loss) if s.stop_loss else ''}"
                          for s in self.slots)
        return f"{account} · 금고 {self.book.vault:,.0f}원" + (f" · {parts}" if parts else "")

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
        if (self.db.get_state("key_alert") or {}).get("expired"):  # 만료 알림 뒤 새 키로 다시 통했다
            self.db.delete_state("key_alert")
            self.key_day = None  # 새 키의 만료일을 바로 다시 확인
            self.event("info", "업비트 API 키가 다시 연결됐어요", notify=True)

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
        if slot.closing:
            return "팔고 빼는 중"
        if plan is None:
            return "계획을 세우는 중"
        daily = slot.tf == "1d"
        at = next_bar(slot.tf).astimezone(KST).strftime("%H:%M")
        krw = self.book.slot(slot.id)["krw"]
        if not self._holding(slot, price) and krw < MIN_ORDER_KRW:
            return ("원화가 없어서 지켜보기만 해요 · 금고에서 넣으면 이 전략대로 사고팔아요" if krw <= 0
                    else f"원화 {krw:,.0f}원은 최소 주문 금액({MIN_ORDER_KRW:,}원)보다 적어요 · 금고에서 더 넣어 주세요")
        if self._holding(slot, price):
            loss = self._loss_line(slot)
            cut = f" · {stoploss.text(slot.stop_loss)} {loss:,.0f}원" if loss is not None else ""
            if slot.strategy.exit_next_open:
                return ("내일 09시에 매도 예정" if daily else f"다음 봉 시작({at})에 매도 예정") + cut
            if plan.sell_stop is not None:
                return f"{plan.sell_stop:,.0f}원 아래로 내려가면 매도" + cut
            return "보유 유지 · 매도 신호가 나면 " + ("09시에 매도" if daily else "다음 봉 시작에 매도") + cut
        if st.get("last_buy_day") == bar or st.get("last_stop_day") == bar:
            return "오늘 매매 완료 · 내일 다시 판단" if daily else f"이번 봉 매매 완료 · {at}에 다시 판단"
        if st.get("cut"):
            return "손절 뒤 쉬는 중 · 매수 신호가 한 번 꺼졌다가 다시 켜지면 사요"
        if plan.buy_stop is not None:
            return f"{plan.buy_stop:,.0f}원 이상 오르면 매수"
        return "매수 신호를 기다리는 중 · " + ("매일 09시 판단" if daily else f"다음 판단 {at}")

    def _write_status(self, day: str) -> None:
        equity = self.book.equity(self.prices)
        contributions = self.book.data["contributions"]
        slots = []
        changed = False
        for s in self.slots:
            b = self.book.slot(s.id)
            price = self.prices.get(s.market, 0.0)
            value = b["coin"] * price
            slot_equity = b["krw"] + value
            holding = self._holding(s, price)
            if "hold" not in b:  # 바이앤홀드 비교가 생기기 전부터 있던 줄: 첫 매매 가격부터 들고 있었다고 친다
                first = self.db.trades(self.executor.mode, s.market)[:1]
                buyhold.start(b, first[0]["price"] if first else None)
                changed = True
            changed |= bool(b.get("hold_cash")) and price > 0
            hold_value = buyhold.settle(b, price)
            slots.append({
                "id": s.id, "name": s.name, "market": s.market, "coin": s.coin, "price": price,
                "tf": s.tf, "tf_label": timeframes.TIMEFRAMES[s.tf]["short"], "bar": s.plan_day,
                "next_at": ts(next_bar(s.tf)), "rank_key": s.rank_key,
                "strategy": {"name": s.strategy.name, "label": s.strategy.label, "description": s.strategy.description,
                             "guide": s.strategy.guide, "exit_next_open": s.strategy.exit_next_open},
                "rule": s.rule,  # 살 때·팔 때 전략
                "stop_loss": s.stop_loss, "stop_price": self._loss_line(s) if holding else None,  # 손절선
                "closing": s.closing,  # 팔고 빼는 중
                "plan": asdict(s.plan) if s.plan else None,
                "state_text": self._state_text(s, price),
                "holding": holding,
                "book": b, "value": value, "equity": slot_equity,
                "return_pct": (slot_equity / b["contrib"] - 1) * 100 if b["contrib"] > 0 else None,
                # 바이앤홀드였다면 (같은 돈이 같은 때 들어와서 그냥 들고 있었다면)
                "hold_return_pct": (hold_value / b["contrib"] - 1) * 100
                if hold_value is not None and b["contrib"] > 0 else None,
                "hold_pnl": hold_value - b["contrib"] if hold_value is not None else None,
                "unrealized": value - b["cost"] if holding else None,
                "unrealized_pct": (value / b["cost"] - 1) * 100 if holding and b["cost"] > 0 else None,
                "actual_weight": slot_equity / equity * 100 if equity > 0 else 0.0,
            })
        if changed:
            self.book.save()
        net = self._network()
        self.db.set_state("status", {
            "updated_at": ts(),
            "mode": self.executor.mode,
            "day": day,
            "interval_sec": self.interval,
            "settings": self.settings,
            "slots": slots,
            "account": {"vault": self.book.vault, "cash": self.book.cash(), "equity": equity,
                        "contributions": contributions, "synced": self.book.data.get("synced", False),
                        "return_pct": (equity / contributions - 1) * 100 if contributions > 0 else None},
            "exchange": self.exchange,
            "network": {**net, "ip_changed": bool(net.get("upbit_ok_ip") and net.get("ip")
                                                  and net["ip"] != net["upbit_ok_ip"])},
        })

    def _update_markets(self, day: str) -> None:
        """하루 한 번: 원화 종목 전부, 거래대금 순 (웹페이지 종목 선택용)."""
        self.markets_day = day
        try:
            self.db.set_state("markets", {"day": day, "all": True, "list": upbit.get_krw_markets(None)})
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
            self._check_key_error(e)
            log.warning("업비트 키 만료일을 확인하지 못했어요: %s", e)
            return
        self._mark_upbit_ok()
        self.db.set_state("key_info", {"expire_at": expire_at, "checked": day})
        left = days_left(expire_at) if expire_at else None
        if left is not None and left < 0:
            self._key_alert(expired_text(expire_at), expired=True)
        elif left is not None and left <= 30:
            self.event("warning", f"업비트 API 키가 {expire_at[:10]}에 만료돼요 (앞으로 {left}일). "
                                  "새 키를 받아 python3 -m mypie.keys 로 등록하세요")
            if left in KEY_WARN_DAYS:  # 텔레그램은 30·14·7·3·1일 전에만 (매일 오면 귀찮으니)
                self._key_alert(expiring_text(expire_at, left))

    def _check_key_error(self, error: BaseException) -> None:
        if network.is_key_error(error):
            self._key_alert(expired_text((self.db.get_state("key_info") or {}).get("expire_at")), expired=True)

    def _key_alert(self, text: str, expired: bool = False) -> None:
        """API 키 만료 알림: 갱신 방법까지 텔레그램으로, 하루 한 번 (매매 알림을 꺼도 보낸다). 화면 기록에는 첫 줄만."""
        day = trading_day()
        if (self.db.get_state("key_alert") or {}).get("day") == day:
            return
        self.db.set_state("key_alert", {"day": day, "at": ts(), "expired": expired})
        first = text.splitlines()[0]
        log.warning(first)
        self.db.add_event("error" if expired else "warning", first)
        self.notifier("🥧 " + text)

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
        # SSH 로그인 알림: 보안 알림이라 웹페이지의 '매매 알림 끄기'와 상관없이 보낸다
        threading.Thread(target=sshwatch.SshWatch(self.notifier).run, args=(self.db.path, self.stop_event),
                         name="ssh-watch", daemon=True).start()
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
                self._check_key_error(e)
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
