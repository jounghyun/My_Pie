"""자동매매 봇 본체.

실행: python3 -m mypie.bot          (계속 실행)
      python3 -m mypie.bot --once   (한 번만 돌고 상태 출력, 점검용)
"""

import argparse
import json
import logging
import re
import signal
import threading
import time
from dataclasses import asdict

from . import backtest, upbit
from .broker import MIN_ORDER_KRW, LiveBroker, PaperBroker
from .config import load_config
from .db import Database
from .history import sync_candles
from .keys import load_keys
from .strategies import STRATEGIES, DayPlan, Strategy, create_strategy, strategy_params
from .timeutil import trading_day, ts

log = logging.getLogger("mypie.bot")

EQUITY_SNAPSHOT_SEC = 300   # 자산 추이 기록 간격
EXCHANGE_REFRESH_SEC = 60   # 실거래일 때 업비트 실제 잔고 확인 간격
MAX_BACKOFF_SEC = 300       # 오류가 계속될 때 최대 대기 시간
DUST_KRW = 1                # 이보다 평가금액이 작으면 보유하지 않은 것으로 본다
MARKET_PATTERN = re.compile(r"^KRW-[A-Z0-9]{1,15}$")


class Bot:
    def __init__(self, cfg: dict, db: Database, broker_factory=None):
        self.cfg = cfg
        self.db = db
        self.broker_factory = broker_factory or self._make_broker
        self.interval = float(cfg["general"]["interval_sec"])
        self.invest_ratio = float(cfg["order"]["invest_ratio"])
        self.min_order_krw = max(float(cfg["order"]["min_order_krw"]), MIN_ORDER_KRW)
        self.fee_rate = float(cfg["paper"]["fee_rate"])
        self.max_budget = float(cfg["live"]["max_budget"])
        self.backtest_days = int(cfg["backtest"]["days"])
        self.rank_by = cfg["backtest"]["rank_by"]

        saved = db.get_state("settings") or {}
        self.settings = {
            "market": saved.get("market", cfg["general"]["market"]),
            "mode": saved.get("mode", cfg["general"]["mode"]),
            "budget": float(saved.get("budget", cfg["live"]["budget"])),
        }
        try:
            self.broker = self.broker_factory(**self.settings)
        except Exception as e:  # 예: 실거래인데 키가 없어짐 → 모의투자로 안전하게 시작
            self.event("error", f"실거래 계좌를 열 수 없어서 모의투자로 시작합니다: {e}")
            self.settings["mode"] = "paper"
            self.broker = self.broker_factory(**self.settings)
        self.db.set_state("settings", self.settings)

        self.strategy = self._load_strategy()
        self.plan: DayPlan | None = None
        self.plan_day: str | None = None
        ranking = db.get_state("ranking") or {}
        self.ranked_day: str | None = ranking.get("day") if ranking.get("market") == self.market else None
        self.markets_day: str | None = (db.get_state("markets") or {}).get("day")
        self.last_snapshot = 0.0
        self.last_exchange_check = 0.0
        self.exchange: dict | None = None
        self.last_error: str | None = None
        self.warned: set = set()
        self.stop_event = threading.Event()

    @property
    def market(self) -> str:
        return self.settings["market"]

    @property
    def coin(self) -> str:
        return self.market.split("-")[-1]

    def _make_broker(self, mode: str, market: str, budget: float):
        if mode == "live":
            keys = load_keys()
            if not keys:
                raise upbit.UpbitError("업비트 API 키가 없어요. 파이에서 python3 -m mypie.keys 로 등록하세요")
            return LiveBroker(self.db, market, budget, self.fee_rate, upbit.UpbitClient(*keys))
        return PaperBroker(self.db, market, self.cfg["paper"]["initial_krw"], self.fee_rate)

    def _load_strategy(self) -> Strategy:
        """웹페이지에서 고른 전략이 있으면 그걸, 없으면 config.toml의 전략을 쓴다."""
        chosen = (self.db.get_state("active_strategy") or {}).get("name")
        name = chosen if chosen in STRATEGIES else self.cfg["strategy"]["name"]
        return create_strategy(name, strategy_params(self.cfg, name))

    def event(self, level: str, message: str) -> None:
        getattr(log, level)(message)
        self.db.add_event(level, message)

    def _state(self) -> dict:
        return self.db.get_state("bot_state", {})

    def _set_state(self, **changes) -> None:
        self.db.set_state("bot_state", {**self._state(), **changes})

    def _holding(self, price: float) -> bool:
        return self.broker.account()["coin"] * price >= DUST_KRW

    def _halted(self) -> dict | None:
        return self._state().get("halted")

    def _halt(self, reason: str) -> None:
        self._set_state(halted={"reason": reason, "at": ts()})
        self.event("error", f"실거래 매매를 멈췄어요 · {reason} · 업비트에서 확인한 뒤 웹페이지에서 '다시 시작'을 누르세요")

    # ---- 한 번의 루프 ----

    def step(self) -> None:
        day = trading_day()
        self._handle_requests()
        if day != self.plan_day and not self._start_day(day):
            return
        price = upbit.get_price(self.market)
        self._intraday(price, day)
        self._refresh_exchange()
        self._write_status(price, day)
        if time.monotonic() - self.last_snapshot >= EQUITY_SNAPSHOT_SEC:
            acct = self.broker.account()
            self.db.add_equity(self.broker.mode, price, acct["krw"] + acct["coin"] * price)
            self.last_snapshot = time.monotonic()
        if self.markets_day != day:
            self._update_markets(day)
        if self.ranked_day != day:
            self._update_ranking(day)

    def _start_day(self, day: str) -> bool:
        """새 거래일(또는 재시작·설정 변경 직후): 일봉을 받아 오늘 계획을 세우고 09시 매매를 한다."""
        today = sync_candles(self.db, self.market, self.backtest_days + backtest.WARMUP)
        if today["day"] != day:
            log.info("오늘 일봉이 아직 없어 다음 루프에서 다시 시도합니다")
            return False
        history = self.db.candle_history(self.market, backtest.WINDOW)
        self.plan = self.strategy.plan(history, today["open"])
        self.plan_day = day
        self.event("info", f"[{day}] {self.market} · {self.strategy.label} · {self.plan.note}")

        if self._state().get("open_done_day") != day:  # 재시작해도 09시 매매를 두 번 하지 않게
            price = today["close"]  # 진행 중인 일봉의 종가 = 현재가
            holding = self._holding(price)
            if holding and self.plan.sell_at_open:
                reason = "산 다음 날 09시 매도" if self.strategy.exit_next_open else f"매도 신호 · {self.plan.note}"
                self._sell(price, reason)
            elif not holding and self.plan.buy_at_open:
                self._buy(price, day, f"매수 신호 · {self.plan.note}")
            self._set_state(open_done_day=day)
        return True

    def _intraday(self, price: float, day: str) -> None:
        plan, st = self.plan, self._state()
        if self._holding(price):
            if plan.sell_stop is not None and st.get("last_buy_day") != day and price <= plan.sell_stop:
                if self._sell(price, f"{plan.sell_stop:,.0f}원 이탈"):
                    self._set_state(last_stop_day=day)
        elif (plan.buy_stop is not None and price >= plan.buy_stop
              and st.get("last_buy_day") != day and st.get("last_stop_day") != day):
            self._buy(price, day, f"{plan.buy_stop:,.0f}원 돌파")

    # ---- 웹페이지 요청 ----

    def _handle_requests(self) -> None:
        if self.db.get_state("resume_request") is not None:
            self.db.delete_state("resume_request")
            if self._halted():
                self._set_state(halted=None)
                self.event("info", "매매를 다시 시작합니다")
        request = self.db.get_state("control_request")
        if request is not None:
            self._apply_control(request)

    def _apply_control(self, request: dict) -> None:
        """종목·계좌·한도·전략 변경. 보유 중이면 먼저 판 다음 새 설정으로 다시 시작한다."""
        new = {
            "market": request.get("market") or self.settings["market"],
            "mode": request.get("mode") or self.settings["mode"],
            "budget": float(request.get("budget") or self.settings["budget"]),
        }
        strategy_name = request.get("strategy") or self.strategy.name
        problem = None
        if strategy_name not in STRATEGIES:
            problem = f"알 수 없는 전략: {strategy_name}"
        elif new["mode"] not in ("paper", "live"):
            problem = f"알 수 없는 계좌: {new['mode']}"
        elif not MARKET_PATTERN.match(new["market"]):
            problem = f"잘못된 종목: {new['market']}"
        elif new["mode"] == "live" and not (MIN_ORDER_KRW <= new["budget"] <= self.max_budget):
            problem = f"투자 한도는 {MIN_ORDER_KRW:,}원 ~ {self.max_budget:,.0f}원 사이여야 해요"
        if new["mode"] == "paper":
            new["budget"] = self.settings["budget"]  # 모의투자에서는 한도를 쓰지 않는다
        if problem:
            self.db.delete_state("control_request")
            self.event("warning", f"설정 변경 실패 · {problem}")
            return
        settings_changed = new != self.settings
        if not settings_changed and strategy_name == self.strategy.name:
            self.db.delete_state("control_request")
            return

        if settings_changed and new["mode"] == "live":
            keys = load_keys()
            try:
                if not keys:
                    raise upbit.UpbitError("API 키가 없어요. 파이에서 python3 -m mypie.keys 로 등록하세요")
                upbit.UpbitClient(*keys).accounts()  # 키와 허용 IP가 맞는지 미리 확인
            except upbit.UpbitError as e:
                self.db.delete_state("control_request")
                self.event("error", f"실거래로 바꾸지 못했어요 · {e}")
                return

        price = upbit.get_price(self.market)  # 통신 오류면 요청을 남겨 두고 다음 루프에서 다시 시도
        if self._holding(price) and not self._sell(price, "설정 변경"):
            self.db.delete_state("control_request")
            self.event("error", "보유 코인을 팔지 못해서 설정을 바꾸지 않았어요")
            return

        before = self._describe()
        if settings_changed:
            self.broker = self.broker_factory(**new)
            if new["market"] != self.settings["market"]:
                self.ranked_day = None  # 새 종목으로 순위를 다시 계산
            self.settings = new
            self.db.set_state("settings", new)
            self.exchange = None
            self.last_exchange_check = 0.0
            self.last_snapshot = 0.0
        if strategy_name != self.strategy.name:
            self.strategy = create_strategy(strategy_name, strategy_params(self.cfg, strategy_name))
            self.db.set_state("active_strategy", {"name": strategy_name, "changed_at": ts()})
        self._set_state(open_done_day=None, last_buy_day=None, last_stop_day=None)
        self.plan_day = None  # 바로 다음 단계에서 새 설정으로 오늘 계획을 다시 세운다
        self.db.delete_state("control_request")
        self.event("info", f"설정 변경 · {before} → {self._describe()}")

    def _describe(self) -> str:
        account = "모의투자" if self.settings["mode"] == "paper" else f"실거래(한도 {self.settings['budget']:,.0f}원)"
        return f"{account} · {self.market} · {self.strategy.label}"

    # ---- 주문 ----

    def _buy(self, price: float, day: str, reason: str) -> bool:
        if self._halted():
            return False
        amount = self.broker.account()["krw"] * self.invest_ratio
        if amount < self.min_order_krw:
            if ("min_order", day) not in self.warned:
                self.warned.add(("min_order", day))
                self.event("warning", f"주문 가능 금액 {amount:,.0f}원이 최소 주문 금액 "
                                      f"{self.min_order_krw:,.0f}원보다 적어 매수하지 않습니다")
            return False
        try:
            trade = self.broker.buy(price, amount, reason)
        except upbit.OrderNotPlaced:
            raise  # 주문이 안 들어갔으니 다음 루프에서 다시 시도
        except Exception as e:
            if self.broker.mode != "live":
                raise
            self._halt(f"매수 실패: {e}")
            return False
        self._set_state(last_buy_day=day)
        self.db.add_trade(self.broker.mode, self.market, trade)
        self.event("info", f"{self._tag()}매수 {trade['volume']:.8f} {self.coin} @ {trade['price']:,.0f}원 "
                           f"({trade['krw']:,.0f}원) · {reason}")
        return True

    def _sell(self, price: float, reason: str) -> bool:
        if self._halted():
            return False
        try:
            trade = self.broker.sell(price, reason)
        except upbit.OrderNotPlaced:
            raise
        except Exception as e:
            if self.broker.mode != "live":
                raise
            self._halt(f"매도 실패: {e}")
            return False
        self.db.add_trade(self.broker.mode, self.market, trade)
        pct = trade["profit"] / trade["cost"] * 100 if trade.get("cost") else 0.0
        self.event("info", f"{self._tag()}매도 {trade['volume']:.8f} {self.coin} @ {trade['price']:,.0f}원 · "
                           f"손익 {trade['profit']:+,.0f}원 ({pct:+.2f}%) · {trade['reason']}")
        return True

    def _tag(self) -> str:
        return "[실거래] " if self.broker.mode == "live" else ""

    # ---- 화면용 상태 ----

    def _refresh_exchange(self) -> None:
        """실거래일 때 업비트 실제 잔고를 가끔 확인해서 화면에 보여준다."""
        if self.broker.mode != "live":
            self.exchange = None
            return
        if time.monotonic() - self.last_exchange_check < EXCHANGE_REFRESH_SEC:
            return
        self.last_exchange_check = time.monotonic()
        try:
            balances = {a["currency"]: float(a["balance"]) + float(a["locked"]) for a in self.broker.client.accounts()}
            self.exchange = {"krw": balances.get("KRW", 0.0), "coin": balances.get(self.coin, 0.0), "updated_at": ts()}
        except Exception as e:
            log.warning("업비트 잔고 확인 실패: %s", e)

    def _state_text(self, price: float, day: str) -> str:
        plan, st = self.plan, self._state()
        if st.get("halted"):
            return "⚠️ 매매 멈춤 · 웹페이지에서 '다시 시작'을 눌러야 해요"
        if self._holding(price):
            if self.strategy.exit_next_open:
                return "내일 09시에 매도 예정"
            if plan.sell_stop is not None:
                return f"{plan.sell_stop:,.0f}원 아래로 내려가면 매도"
            return "매도 신호를 기다리는 중 (매일 09시 판단)"
        if st.get("last_buy_day") == day or st.get("last_stop_day") == day:
            return "오늘 매매 완료 · 내일 다시 판단"
        if plan.buy_stop is not None:
            return f"{plan.buy_stop:,.0f}원 이상 오르면 매수"
        return "매수 신호를 기다리는 중 (매일 09시 판단)"

    def _write_status(self, price: float, day: str) -> None:
        acct = self.broker.account()
        equity = acct["krw"] + acct["coin"] * price
        initial = acct.get("initial_krw")
        self.db.set_state("status", {
            "updated_at": ts(),
            "mode": self.broker.mode,
            "market": self.market,
            "coin": self.coin,
            "day": day,
            "price": price,
            "interval_sec": self.interval,
            "settings": self.settings,
            "strategy": {"name": self.strategy.name, "label": self.strategy.label,
                         "description": self.strategy.description, "guide": self.strategy.guide,
                         "exit_next_open": self.strategy.exit_next_open},
            "plan": asdict(self.plan),
            "state_text": self._state_text(price, day),
            "account": {**acct, "equity": equity,
                        "return_pct": (equity / initial - 1) * 100 if initial else None},
            "holding": self._holding(price),
            "halted": self._halted(),
            "exchange": self.exchange,
        })

    def _update_markets(self, day: str) -> None:
        """하루 한 번: 거래대금 상위 원화 종목 목록 (웹페이지 종목 선택용)."""
        self.markets_day = day
        try:
            self.db.set_state("markets", {"day": day, "list": upbit.get_krw_markets(30)})
        except upbit.UpbitError as e:
            log.warning("종목 목록을 받지 못했어요: %s", e)

    def _update_ranking(self, day: str) -> None:
        """하루 한 번: 모든 전략을 과거 데이터로 백테스트해서 순위를 저장한다."""
        self.ranked_day = day
        candles = self.db.candle_history(self.market, self.backtest_days + backtest.WARMUP)
        if len(candles) < backtest.WARMUP + 30:
            log.info("일봉이 부족해서 전략 순위를 계산하지 않습니다")
            return
        started = time.monotonic()
        params = {name: strategy_params(self.cfg, name) for name in STRATEGIES}
        results = backtest.rank_strategies(candles, self.fee_rate, self.rank_by, params)
        self.db.set_state("ranking", {
            "day": day, "market": self.market, "computed_at": ts(), "rank_by": self.rank_by,
            "from": candles[backtest.WARMUP]["day"], "to": candles[-1]["day"],
            "days": len(candles) - backtest.WARMUP, "results": results,
        })
        top = results[0]
        log.info("전략 순위 계산 %.1f초", time.monotonic() - started)
        self.event("info", f"{self.market} 전략 순위 갱신 · 1위 {top['label']} ({top['total_return'] * 100:+.1f}%)")

    # ---- 계속 실행 ----

    def run(self) -> None:
        self.event("info", f"봇 시작 · {self._describe()}")
        wait = self.interval
        while not self.stop_event.is_set():
            try:
                self.step()
                if self.last_error:
                    self.event("info", "업비트 통신이 복구되었습니다")
                    self.last_error = None
                wait = self.interval
            except Exception as e:  # 어떤 오류가 나도 봇은 멈추지 않고 재시도한다
                message = f"업비트 통신 오류: {e}" if isinstance(e, upbit.UpbitError) else f"오류: {e!r}"
                if message != self.last_error:
                    if isinstance(e, upbit.UpbitError):
                        self.event("warning", message)
                    else:
                        log.exception("예상치 못한 오류")
                        self.db.add_event("error", message)
                    self.last_error = message
                wait = min(wait * 2, MAX_BACKOFF_SEC)
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
