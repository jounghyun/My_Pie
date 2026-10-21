"""모니터링 웹페이지. 봇이 DB에 남긴 기록을 보여주고, PIN을 알면 종목·계좌·전략을 바꿀 수 있다.

실행: python3 -m mypie.web   →   http://<파이 IP>:8000

  GET  /api/status   봇 상태, 순위, 기록 (5초마다)
  GET  /api/candles  차트용 봉과 매매 지점 (?market=KRW-BTC&tf=60m&bars=72  최근 봉 개수)
  GET  /api/simulate 전략 시뮬레이션 (?market=KRW-BTC&tf=240m&days=90&strategy=ma_cross)
  GET  /api/stats    성과 통계, 종목별 성적, 날짜별 손익
  GET  /api/system   라즈베리파이 상태 (온도, 메모리, 디스크, 전원)
  POST /api/control  포트폴리오(종목·전략·비중)·계좌·한도 변경 (PIN 필요)
  POST /api/ranking  아직 순위가 없는 종목의 전략 순위 계산 부탁 (PIN 불필요)
  POST /api/telegram/start|link|action   웹페이지에서 텔레그램 연결·관리 (PIN 필요)
  POST /api/login/send|verify, /api/logout  텔레그램 코드 로그인

텔레그램이 연결돼 있으면 모든 페이지·API에 로그인이 필요하다 (/healthz 제외).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import auth, backtest, krx, pin, telegram, timeframes, toss
from .bot import validate_slots
from .broker import MIN_ORDER_KRW
from .config import load_config
from .db import Database
from .keys import load_keys, read_env, write_env
from .strategies import STOCK_STRATEGIES, STRATEGIES, strategy_params
from .timeutil import now_kst, ts

log = logging.getLogger("mypie.web")

BOT_NAME_KEY = "TELEGRAM_BOT_NAME"

STATIC_DIR = Path(__file__).resolve().parent / "static"
EQUITY_DAYS = 7
CALENDAR_DAYS = 84            # 손익 캘린더 기간 (12주)
MAX_PIN_FAILURES = 5          # 이만큼 틀리면
PIN_LOCK_SEC = 600            # 이 시간 동안 잠근다
MAX_BODY_BYTES = 2048
MARKET_PATTERN = re.compile(r"^KRW-[A-Z0-9]{1,15}$")
STOCK_SYMBOL = re.compile(r"^[0-9A-Z]{6}$")  # 국내 주식 종목 코드 (예: 005930, 0101N0)

_pin_lock = threading.Lock()
_pin_failures: deque[float] = deque()


def _open(db_path: str) -> Database | None:
    return Database(db_path, create=False) if Path(db_path).exists() else None


def build_payload(db_path: str, max_budget: float) -> dict:
    payload = {
        "server_time": ts(), "status": None, "trades": [], "events": [], "equity": [],
        "rankings": {}, "markets": [], "market_info": None, "key_info": None, "ranking_request": None,
        "pin_set": False, "pending": None, "live_ready": load_keys() is not None,
        "limits": {"min_budget": MIN_ORDER_KRW, "max_budget": max_budget},
        "strategies": [{"name": c.name, "label": c.label, "description": c.description, "guide": c.guide}
                       for c in STRATEGIES.values()],
        "timeframes": [{"id": k, "label": v["label"], "short": v["short"], "minutes": v["minutes"],
                        "periods": [{"days": d, "label": label} for d, label in backtest.SIM_PERIODS[k]]}
                       for k, v in timeframes.TIMEFRAMES.items()],
    }
    db = _open(db_path)
    if not db:
        return payload
    try:
        status = db.get_state("status")
        payload["status"] = status
        payload["trades"] = db.recent_trades(30)
        payload["events"] = db.recent_events(40)
        if status:
            payload["equity"] = db.equity_since(status["mode"], ts(now_kst() - timedelta(days=EQUITY_DAYS)))
            payload["pending_order"] = db.get_state("live_pending_order") if status["mode"] == "live" else None
        payload["rankings"] = db.get_state("rankings") or {}
        payload["ranking_request"] = db.get_state("ranking_request")
        payload["markets"] = (db.get_state("markets") or {}).get("list", [])
        payload["market_info"] = db.get_state("market_info")
        payload["key_info"] = db.get_state("key_info")
        payload["pin_set"] = db.get_state(pin.STATE_KEY) is not None
        payload["pending"] = db.get_state("control_request")
        payload["telegram"] = telegram_status(db)
    finally:
        db.close()
    return payload


def build_candles(db_path: str, market: str, count: int, tf: str = "1d") -> dict:
    """차트용: 한 종목의 봉 (일봉·분봉) + 그 기간의 매매 지점."""
    db = _open(db_path)
    if not db or not MARKET_PATTERN.match(market) or tf not in timeframes.TIMEFRAMES:
        return {"market": market, "tf": tf, "candles": [], "trades": []}
    try:
        status = db.get_state("status") or {}
        candles = db.candle_history(market, count, tf)
        since = ts(timeframes.key_to_utc(candles[0]["day"])) if candles else ""
        trades = db.trades(status.get("mode", "paper"), market, since)
        tickers = (db.get_state("market_info") or {}).get("tickers") or {}
        return {"market": market, "tf": tf, "candles": candles, "today": tickers.get(market) if tf == "1d" else None,
                "trades": [{k: t[k] for k in ("ts", "side", "price", "profit")} for t in trades]}
    finally:
        db.close()


SIM_CACHE_SIZE = 12
_sim_lock = threading.Lock()
_sim_cache: dict[tuple, list[dict]] = {}
# 주식 시뮬레이션 기간 (일, 이름). 1년에 거래일은 250일쯤이라 봉 개수는 그만큼 줄여서 센다
STOCK_SIM_PERIODS = [(30, "1달"), (90, "3달"), (180, "6달"), (365, "1년"), (1095, "3년")]


def build_simulation(db_path: str, cfg: dict, market: str, tf: str, days: int, strategy: str) -> dict:
    """코인 전략 시뮬레이션: 한 종목·판단 주기로 최근 days일 동안 모든 전략을 굴려 본 순위와, 고른 전략의 매매 지점.
    봇이 모아 둔 봉으로 계산한다. 봉이 모자라거나 오래됐으면 need_data / stale로 알려서 봇에게 받아 달라고 하게 한다."""
    if not MARKET_PATTERN.match(market) or tf not in timeframes.TIMEFRAMES:
        return {"market": market, "tf": tf, "days": days, "periods": [], "ready": False, "message": "잘못된 종목이에요."}
    minutes = timeframes.minutes(tf)
    return _simulate(db_path, market, tf, days, strategy, periods=backtest.SIM_PERIODS[tf],
                     bars_of=lambda d: backtest.period_bars(minutes, d), strategies=STRATEGIES,
                     fee=float(cfg["paper"]["fee_rate"]), sell_cost=0.0, rank_by=cfg["backtest"]["rank_by"],
                     per_year=timeframes.bars_per_year(tf), cfg=cfg,
                     is_stale=lambda last: timeframes.key_to_utc(last) + timedelta(minutes=minutes * 2) < now_kst())


def build_stock_simulation(db_path: str, cfg: dict, symbol: str, days: int, strategy: str) -> dict:
    """주식 전략 시뮬레이션 (일봉, 주식용 전략 목록, 증권거래세 반영)."""
    if not STOCK_SYMBOL.match(symbol):
        return {"market": symbol, "tf": "1d", "days": days, "periods": [], "ready": False, "message": "잘못된 종목이에요."}
    sc = cfg["stock"]
    return _simulate(db_path, symbol, "1d", days, strategy, periods=STOCK_SIM_PERIODS,
                     bars_of=lambda d: d * 250 // 365, strategies=STOCK_STRATEGIES, fee=float(sc["fee_rate"]),
                     sell_cost=float(sc["sell_tax"]), rank_by=sc.get("rank_by", "return"), per_year=250, cfg=cfg,
                     is_stale=lambda last: last < krx.previous_trading_day(now_kst().date()).isoformat())


def _simulate(db_path: str, market: str, tf: str, days: int, strategy: str, *, periods, bars_of, strategies: dict,
              fee: float, sell_cost: float, rank_by: str, per_year: float, cfg: dict, is_stale) -> dict:
    out = {"market": market, "tf": tf, "days": days, "periods": [], "ready": False}
    db = _open(db_path)
    if not db:
        return {**out, "message": "봇이 아직 시작되지 않았어요."}
    history = backtest.history_needed(strategies)
    try:
        candles = db.candle_history(market, bars_of(periods[-1][0]) + history, tf)
    finally:
        db.close()
    out["periods"] = [{"days": d, "label": label, "ok": len(candles) - bars_of(d) >= backtest.WARMUP}
                      for d, label in periods]
    usable = [p["days"] for p in out["periods"] if p["ok"]]
    if not usable:
        return {**out, "need_data": True, "message": "과거 봉을 모으는 중이에요 (처음 고른 종목은 1분쯤 걸려요)"}
    if days not in usable:
        days = out["days"] = usable[-1]
    last = candles[-1]["day"]
    out["stale"] = is_stale(last)  # 끝난 봉이 하나라도 빠졌다

    bars = bars_of(days)
    key = (id(strategies), market, tf, days, last)
    with _sim_lock:  # 파이 3B에선 몇 초 걸리니 같은 계산을 겹쳐 하지 않는다
        results = _sim_cache.get(key)
        if results is None:
            params = {name: strategy_params(cfg, name) for name in strategies}
            results = backtest.rank_period(candles, bars, fee, rank_by, per_year, params, sell_cost, strategies)
            _sim_cache[key] = results
            while len(_sim_cache) > SIM_CACHE_SIZE:
                _sim_cache.pop(next(iter(_sim_cache)))
    window = candles[-bars:]
    chosen = next((r for r in results if r["name"] == strategy), results[0])
    hold = next((r for r in results if r["name"] == "buy_and_hold"), chosen)
    return {
        **out, "ready": True, "rank_by": rank_by,
        "from": window[0]["day"], "to": last, "bars": bars,
        "results": [{k: v for k, v in r.items() if k not in ("detail", "description", "guide")} for r in results],
        "detail": {"name": chosen["name"], "keys": [c["day"] for c in window], "close": [c["close"] for c in window],
                   "equity": chosen["detail"]["equity"], "hold": hold["detail"]["equity"],
                   "fills": chosen["detail"]["fills"], "holding": chosen["detail"]["holding"]},
    }


# ---- 주식 (토스증권, 주식 봇 DB) ----

STOCK_ACTIONS = ("add", "remove", "strategy", "transfer", "mode", "paper_cash")


def _slim_rankings(rankings: dict) -> dict:
    """화면에 보낼 순위: 전략 설명 글은 빼서 가볍게 (설명은 strategies에 한 번만)."""
    return {sym: {**r, "results": [{k: v for k, v in x.items() if k not in ("description", "guide")}
                                   for x in r.get("results") or []]}
            for sym, r in rankings.items()}


def build_stock_payload(stock_db: str, main_db: str) -> dict:
    payload = {
        "server_time": ts(), "status": None, "trades": [], "events": [], "equity": [], "rankings": {},
        "results": [], "pending": 0, "pending_order": None, "keys": toss.load_credentials() is not None,
        "pin_set": False, "network": None,
        "strategies": [{"name": c.name, "label": c.label, "description": c.description, "guide": c.guide,
                        "stock_only": c.name not in STRATEGIES} for c in STOCK_STRATEGIES.values()],
        "periods": [{"days": d, "label": label} for d, label in STOCK_SIM_PERIODS],
    }
    main = _open(main_db)  # PIN과 집 IP는 코인 봇 DB에 있다
    if main:
        try:
            payload["pin_set"] = main.get_state(pin.STATE_KEY) is not None
            payload["network"] = (main.get_state("status") or {}).get("network")
        finally:
            main.close()
    db = _open(stock_db)
    if not db:
        return payload
    try:
        status = db.get_state("status")
        mode = (status or {}).get("mode", "paper")
        payload["status"] = status
        payload["trades"] = [t for t in db.recent_trades(60) if t["mode"] == mode][:30]
        payload["events"] = db.recent_events(40)
        payload["equity"] = db.equity_since(mode, ts(now_kst() - timedelta(days=EQUITY_DAYS)))
        payload["rankings"] = _slim_rankings(db.get_state("rankings") or {})
        payload["results"] = db.get_state("request_results") or []
        payload["pending"] = len(db.states("request:"))
        payload["pending_order"] = db.get_state("stock_pending_order")
    finally:
        db.close()
    return payload


def stock_search(stock_db: str, q: str, limit: int = 20) -> list[dict]:
    """종목 찾기: 코드가 같거나 이름이 같은 것 → 이름이 그 글자로 시작하는 것 → 이름에 들어 있는 것."""
    q = (q or "").strip()
    db = _open(stock_db)
    if not q or not db:
        return []
    try:
        stocks = (db.get_state("stocks") or {}).get("list", [])
    finally:
        db.close()
    low, up = q.lower(), q.upper()
    ranked = []
    for s in stocks:
        name = s["name"].lower()
        if s["symbol"] == up or name == low:
            ranked.append((0, s))
        elif name.startswith(low) or s["symbol"].startswith(up):
            ranked.append((1, s))
        elif low in name:
            ranked.append((2, s))
    ranked.sort(key=lambda x: (x[0], x[1]["name"]))
    return [s for _, s in ranked[:limit]]


def build_stock_candles(stock_db: str, symbol: str, count: int) -> dict:
    """주식 차트용: 일봉 + 그 기간의 매매 지점 (지금 계좌 것만)."""
    db = _open(stock_db)
    if not db or not STOCK_SYMBOL.match(symbol):
        return {"symbol": symbol, "candles": [], "trades": []}
    try:
        mode = (db.get_state("status") or {}).get("mode", "paper")
        candles = db.candle_history(symbol, count, "1d")
        trades = db.trades(mode, symbol, f"{candles[0]['day']} 00:00:00") if candles else []
        return {"symbol": symbol, "candles": candles,
                "trades": [{k: t[k] for k in ("ts", "side", "price", "profit", "volume")} for t in trades]}
    finally:
        db.close()


INTRADAY_SPANS = {"1d": (5, 1), "1w": (30, 5)}  # 차트 기간 → (몇 분봉으로 묶을지, 거래일 수)
INTRADAY_CACHE_SEC = 60
_intraday_lock = threading.Lock()
_intraday_cache: dict[tuple, tuple[float, dict]] = {}


def build_stock_intraday(stock_db: str, symbol: str, span: str) -> dict:
    """주식 차트 1일·1주: 토스증권에서 1분봉을 받아 5분·30분봉으로 묶는다 (정규장 09:00~15:30만).
    토큰은 주식 봇 DB에 저장된 것을 같이 쓰고, 같은 차트는 1분 동안 다시 받지 않는다."""
    out = {"symbol": symbol, "span": span, "intraday": True, "candles": [], "trades": []}
    if not STOCK_SYMBOL.match(symbol) or span not in INTRADAY_SPANS:
        return {**out, "message": "잘못된 요청이에요."}
    creds = toss.load_credentials()
    if not creds:
        return {**out, "message": "토스증권 키를 등록하면 볼 수 있어요."}
    with _intraday_lock:
        hit = _intraday_cache.get((symbol, span))
        if hit and time.monotonic() - hit[0] < INTRADAY_CACHE_SEC:
            return hit[1]
    minutes, days = INTRADAY_SPANS[span]
    db = _open(stock_db)
    if not db:
        return {**out, "message": "주식 봇이 아직 시작되지 않았어요."}
    try:
        client = toss.TossClient(*creds, db=db)
        bars, before = {}, None
        for _ in range(40):  # 최신 → 과거로 받다가 거래일이 days개를 넘으면 그만
            rows, before = client.minute_candles(symbol, before)
            for r in rows:
                start = datetime.fromisoformat(r["ts"]) - timedelta(minutes=1)  # 1분봉 시각은 끝난 시각
                if "09:00" <= start.strftime("%H:%M") <= "15:30":
                    bars[start] = r
            if len({t.date() for t in bars}) > days or not rows or not before:
                break
        keep = sorted({t.date() for t in bars})[-days:]
        candles: dict[str, dict] = {}
        for start in sorted(t for t in bars if t.date() in keep):
            r = bars[start]
            open_at = start.replace(hour=9, minute=0)
            bucket = open_at + timedelta(minutes=(start - open_at).seconds // 60 // minutes * minutes)
            key = bucket.strftime("%Y-%m-%dT%H:%M")
            c = candles.get(key)
            if c is None:
                candles[key] = {"day": key, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                                "volume": r["volume"]}
            else:
                c.update(high=max(c["high"], r["high"]), low=min(c["low"], r["low"]), close=r["close"],
                         volume=c["volume"] + r["volume"])
        mode = (db.get_state("status") or {}).get("mode", "paper")
        trades = db.trades(mode, symbol, f"{keep[0].isoformat()} 00:00:00") if keep else []
    except toss.TossError as e:
        return {**out, "message": str(e)}
    finally:
        db.close()
    result = {**out, "minutes": minutes, "candles": list(candles.values()),
              "trades": [{k: t[k] for k in ("ts", "side", "price", "profit", "volume")} for t in trades]}
    with _intraday_lock:
        _intraday_cache[(symbol, span)] = (time.monotonic(), result)
    return result


def request_stock(stock_db: str, main_db: str, data: dict) -> tuple[int, dict]:
    """주식 봇에게 보내는 요청 (종목 계좌 추가·삭제·전략 변경·금고 이체·계좌 변경). PIN이 필요하다."""
    action = str(data.get("action", ""))
    if action not in STOCK_ACTIONS:
        return 400, {"ok": False, "message": "알 수 없는 요청이에요."}
    request: dict = {"action": action}
    try:
        if action == "add":
            symbol, strategy = str(data.get("symbol", "")), str(data.get("strategy", ""))
            if not STOCK_SYMBOL.match(symbol):
                return 400, {"ok": False, "message": "종목을 골라 주세요."}
            if strategy not in STOCK_STRATEGIES:
                return 400, {"ok": False, "message": "전략을 골라 주세요."}
            request.update(symbol=symbol, name=str(data.get("name") or symbol)[:40], strategy=strategy,
                           amount=max(0.0, float(data.get("amount") or 0)))
        elif action in ("remove", "strategy", "transfer"):
            request["slot"] = str(data.get("slot", ""))[:20]
            if action == "strategy":
                if str(data.get("strategy")) not in STOCK_STRATEGIES:
                    return 400, {"ok": False, "message": "전략을 골라 주세요."}
                request["strategy"] = str(data["strategy"])
            if action == "transfer":
                direction = "out" if data.get("direction") == "out" else "in"
                request.update(direction=direction, all=bool(data.get("all")))
                if not request["all"]:
                    amount = float(data.get("amount") or 0)
                    if not 0 < amount <= 1e11:
                        return 400, {"ok": False, "message": "옮길 금액을 넣어 주세요."}
                    request["amount"] = amount
        elif action == "mode":
            mode = str(data.get("mode", ""))
            if mode not in ("paper", "live"):
                return 400, {"ok": False, "message": "계좌를 다시 골라 주세요."}
            if mode == "live":
                if toss.load_credentials() is None:
                    return 400, {"ok": False, "message": "토스증권 키가 아직 없어요. 파이에서 python3 -m mypie.toss 로 먼저 등록하세요."}
                if data.get("confirm_live") is not True:
                    return 400, {"ok": False, "message": "실거래는 '실제 돈으로 거래한다'에 체크해야 바꿀 수 있어요."}
            request["mode"] = mode
        elif action == "paper_cash":
            amount = float(data.get("amount") or 0)
            if not amount or abs(amount) > 1e11:
                return 400, {"ok": False, "message": "금액을 넣어 주세요."}
            request["amount"] = amount
    except (TypeError, ValueError):
        return 400, {"ok": False, "message": "숫자를 다시 확인해 주세요."}
    failure = _pin_ok(main_db, str(data.get("pin", "")))
    if failure:
        return failure[0], {"ok": False, "message": failure[1]}
    db = _open(stock_db)
    if not db:
        return 503, {"ok": False, "message": "주식 봇이 아직 시작되지 않았어요."}
    try:
        request["id"] = str(time.time_ns())
        db.set_state(f"request:{request['id']}", request)
    finally:
        db.close()
    return 200, {"ok": True, "message": "주식 봇에게 전했어요. 곧 처리돼요.", "id": request["id"]}


def request_stock_ranking(stock_db: str, symbol: str) -> tuple[int, str]:
    """주식 봇에게 이 종목의 일봉을 받아서 전략 순위를 계산해 달라고 한다 (PIN 불필요)."""
    if not STOCK_SYMBOL.match(symbol):
        return 400, "잘못된 종목이에요."
    db = _open(stock_db)
    if not db:
        return 503, "주식 봇이 아직 시작되지 않았어요."
    try:
        db.set_state(f"ranking_request:{symbol}", {"at": ts()})
    finally:
        db.close()
    return 200, "과거 일봉을 받아서 계산할게요. 1분쯤 걸려요."


def build_stats(db_path: str) -> dict:
    """지금 계좌(모의/실거래)의 성과 통계와 날짜별 확정 손익."""
    db = _open(db_path)
    if not db:
        return {}
    try:
        status = db.get_state("status") or {}
        mode = status.get("mode", "paper")
        trades = db.trades(mode)
        equity = db.equity_since(mode, "")
    finally:
        db.close()
    sells = [t for t in trades if t["side"] == "sell" and t["volume"] > 0]
    profits = [t["profit"] or 0 for t in sells]
    returns = [(t["profit"] or 0) / (t["krw"] - (t["profit"] or 0)) * 100 for t in sells if t["krw"] - (t["profit"] or 0) > 0]

    streak, kind = 0, None  # 최근 연승/연패
    for p in reversed(profits):
        k = "win" if p > 0 else "loss"
        if kind in (None, k):
            kind, streak = k, streak + 1
        else:
            break

    peak, mdd = 0.0, 0.0
    for row in equity:
        peak = max(peak, row["equity"])
        if peak:
            mdd = min(mdd, row["equity"] / peak - 1)

    by_market: dict[str, dict] = {}  # 종목(줄)별 성적
    for t in sells:
        m = by_market.setdefault(t["market"], {"round_trips": 0, "wins": 0, "realized": 0.0})
        m["round_trips"] += 1
        m["wins"] += (t["profit"] or 0) > 0
        m["realized"] += t["profit"] or 0

    daily: dict[str, float] = {}
    cutoff = (now_kst() - timedelta(days=CALENDAR_DAYS)).strftime("%Y-%m-%d")
    for t in sells:
        day = t["ts"][:10]
        if day >= cutoff:
            daily[day] = daily.get(day, 0.0) + (t["profit"] or 0)

    return {
        "mode": mode,
        "trades": len(trades),
        "round_trips": len(sells),
        "wins": sum(p > 0 for p in profits),
        "win_rate": sum(p > 0 for p in profits) / len(profits) if profits else None,
        "realized": sum(profits),
        "avg_return_pct": sum(returns) / len(returns) if returns else None,
        "best": max(sells, key=lambda t: t["profit"] or 0, default=None),
        "worst": min(sells, key=lambda t: t["profit"] or 0, default=None),
        "fees": sum(t["fee"] for t in trades),
        "streak": {"kind": kind, "count": streak},
        "mdd": mdd,
        "first_trade": trades[0]["ts"] if trades else None,
        "daily": daily,
        "by_market": by_market,
        "today": now_kst().strftime("%Y-%m-%d"),
    }


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def build_system(db_path: str) -> dict:
    """라즈베리파이 상태. 파이가 아닌 곳에서는 알 수 있는 것만."""
    info: dict = {"server_time": ts()}
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    if temp and temp.isdigit():
        info["temp_c"] = int(temp) / 1000
    try:
        info["load"] = os.getloadavg()[0]
        info["cpus"] = os.cpu_count()
    except OSError:
        pass
    meminfo = _read("/proc/meminfo")
    if meminfo:
        mem = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in meminfo.splitlines() if ":" in line)}
        info["mem_total_mb"] = mem.get("MemTotal", 0) // 1024
        info["mem_used_mb"] = (mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) // 1024
    uptime = _read("/proc/uptime")
    if uptime:
        info["uptime_sec"] = int(float(uptime.split()[0]))
    disk = shutil.disk_usage("/")
    info["disk_total_gb"] = disk.total / 1e9
    info["disk_used_gb"] = disk.used / 1e9
    if Path(db_path).exists():
        info["db_mb"] = Path(db_path).stat().st_size / 1e6
    try:  # 라즈베리파이 전원 부족·과열 기록 (0x0이면 정상)
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        flags = int(out.strip().split("=")[1], 16)
        info["undervoltage_now"] = bool(flags & 0x1)
        info["throttled_now"] = bool(flags & 0x4)
        info["temp_limit_now"] = bool(flags & 0x8)
        info["undervoltage_ever"] = bool(flags & 0x10000)
        info["throttled_ever"] = bool(flags & 0x40000)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return info


def _check_pin(db: Database, pin_input: str) -> tuple[int, str] | None:
    """PIN이 맞으면 None, 틀리면 (상태 코드, 안내 문구). _pin_lock 안에서 불러야 한다."""
    now = time.monotonic()
    while _pin_failures and now - _pin_failures[0] > PIN_LOCK_SEC:
        _pin_failures.popleft()
    if len(_pin_failures) >= MAX_PIN_FAILURES:
        return 429, "PIN을 여러 번 틀려서 10분 동안 잠겼어요."
    stored = db.get_state(pin.STATE_KEY)
    if not stored:
        return 403, "PIN이 아직 없어요. 파이에서 python3 -m mypie.pin 으로 먼저 만들어 주세요."
    if not pin.verify(pin_input, stored):
        _pin_failures.append(now)
        return 403, f"PIN이 틀렸어요. (남은 기회 {MAX_PIN_FAILURES - len(_pin_failures)}번)"
    return None


def request_control(db_path: str, data: dict, max_budget: float) -> tuple[int, str]:
    """포트폴리오·계좌·한도 변경 요청. (HTTP 상태 코드, 안내 문구)를 돌려준다."""
    slots, problem = validate_slots(data.get("slots"))
    if problem:
        return 400, problem + "."
    mode = str(data.get("mode", ""))
    budget_all = data.get("budget") == "all"  # 실거래: 업비트 계좌 전액
    try:
        budget = 0.0 if budget_all else float(data.get("budget") or 0)
    except (TypeError, ValueError):
        return 400, "투자 한도를 숫자로 입력해 주세요."
    if mode not in ("paper", "live"):
        return 400, "계좌를 다시 골라 주세요."
    if mode == "live":
        if not budget_all and not (MIN_ORDER_KRW <= budget <= max_budget):
            return 400, f"투자 한도는 {MIN_ORDER_KRW:,}원 ~ {max_budget:,.0f}원 사이로 정해 주세요."
        if load_keys() is None:
            return 400, "업비트 API 키가 아직 없어요. 파이에서 python3 -m mypie.keys 로 먼저 등록하세요."
    if not Path(db_path).exists():
        return 503, "봇이 아직 시작되지 않았어요."
    request = {"slots": slots, "mode": mode, "budget": "all" if budget_all else budget, "requested_at": ts()}
    with _pin_lock:  # 한 번에 하나씩만 확인해서 무작위 대입을 느리게 만든다
        db = Database(db_path, create=False)
        try:
            # '실제 돈' 체크는 실거래로 넘어갈 때와 계좌 전액으로 바꿀 때만 받는다 (이미 실거래 중인 종목·비중 변경은 X)
            current = (db.get_state("status") or {}).get("settings") or {}
            if (mode == "live" and data.get("confirm_live") is not True
                    and (current.get("mode") != "live" or (budget_all and current.get("budget") != "all"))):
                return 400, "실거래는 '실제 돈으로 거래한다'에 체크해야 바꿀 수 있어요."
            failure = _check_pin(db, str(data.get("pin", "")))
            if failure:
                return failure
            db.set_state("control_request", request)
        finally:
            db.close()
    log.info("설정 변경 요청: %s", request)
    return 200, "봇에게 전했어요. 10초 안에 적용돼요."


def request_ranking(db_path: str, market: str, tf: str = "1d") -> tuple[int, str]:
    """아직 순위가 없는 종목·판단 주기의 전략 순위를 계산해 달라고 봇에게 부탁한다 (읽기만 하는 일이라 PIN 없이)."""
    if not MARKET_PATTERN.match(market) or tf not in timeframes.TIMEFRAMES:
        return 400, "잘못된 종목이에요."
    db = _open(db_path)
    if not db:
        return 503, "봇이 아직 시작되지 않았어요."
    item = f"{market}|{tf}"
    try:
        queue = (db.get_state("ranking_request") or {}).get("markets", [])
        if item not in queue:
            queue = (queue + [item])[-10:]
        db.set_state("ranking_request", {"markets": queue, "requested_at": ts()})
    finally:
        db.close()
    return 200, f"{market} {timeframes.TIMEFRAMES[tf]['short']} 전략 순위를 계산할게요. 10~30초쯤 걸려요."


# ---- 텔레그램 연결 (웹페이지에서) ----

TG_SETTINGS_KEY = "telegram_settings"
_tg_lock = threading.Lock()
# 연결을 기다리는 봇 토큰은 .env에 잠깐 적어 둔다 (권한 600). 웹 서버가 재시작돼도 이어서 기다린다
PENDING_TOKEN, PENDING_BOT, PENDING_UNTIL = "TELEGRAM_PENDING_TOKEN", "TELEGRAM_PENDING_BOT", "TELEGRAM_PENDING_UNTIL"
LINK_WINDOW_SEC = 900  # 15분 안에 봇에게 메시지를 보내면 연결된다
TOKEN_PATTERN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")


def telegram_settings(db: Database) -> dict:
    return {"notify": True, "login": True, **(db.get_state(TG_SETTINGS_KEY) or {})}


def _pending() -> dict | None:
    env = read_env()
    try:
        until = float(env.get(PENDING_UNTIL) or 0)
    except ValueError:
        until = 0.0
    if env.get(PENDING_TOKEN) and until > time.time():
        return {"token": env[PENDING_TOKEN], "bot": env.get(PENDING_BOT, ""), "expires": until}
    return None


def telegram_status(db: Database | None) -> dict:
    env = read_env()
    settings = telegram_settings(db) if db else {"notify": True, "login": True}
    return {"connected": telegram.load() is not None, "bot": env.get(BOT_NAME_KEY) or None,
            "linking": (_pending() or {}).get("bot"), **settings}


def _pin_ok(db_path: str, pin_input: str) -> tuple[int, str] | None:
    if not Path(db_path).exists():
        return 503, "봇이 아직 시작되지 않았어요."
    with _pin_lock:
        db = Database(db_path, create=False)
        try:
            return _check_pin(db, pin_input)
        finally:
            db.close()


def telegram_start(db_path: str, data: dict) -> tuple[int, dict]:
    """봇 토큰을 확인하고, 그 봇에게 메시지가 오기를 기다리기 시작한다."""
    failure = _pin_ok(db_path, str(data.get("pin", "")))
    if failure:
        return failure[0], {"ok": False, "message": failure[1]}
    token = str(data.get("token", "")).strip()
    if not TOKEN_PATTERN.match(token):
        return 400, {"ok": False, "message": "봇 토큰 모양이 아니에요. BotFather가 준 '숫자:영문' 전체를 붙여넣어 주세요."}
    try:
        me = telegram.call(token, "getMe")
        telegram.call(token, "deleteWebhook")
    except telegram.TelegramError as e:
        return 400, {"ok": False, "message": f"이 토큰으로 텔레그램에 연결할 수 없어요 ({e})"}
    with _tg_lock:
        write_env({PENDING_TOKEN: token, PENDING_BOT: me["username"], PENDING_UNTIL: str(int(time.time() + LINK_WINDOW_SEC))})
    return 200, {"ok": True, "bot": me["username"],
                 "message": f"이제 텔레그램에서 @{me['username']} 을(를) 찾아 아무 메시지나 보내 주세요 (예: /start). 15분 안에요."}


def telegram_link() -> tuple[int, dict]:
    """기다리던 봇에게 메시지가 왔으면 그 채팅으로 연결을 마친다 (웹페이지가 몇 초마다 묻는다)."""
    with _tg_lock:
        pending = _pending()
        if not pending:
            return 400, {"ok": False, "message": "연결 시간이 지났어요. 토큰을 넣고 다시 시작해 주세요."}
        token, bot = pending["token"], pending["bot"]
        try:
            updates = telegram.call(token, "getUpdates", {"timeout": 0})
        except telegram.TelegramError as e:
            return 502, {"ok": False, "message": f"텔레그램 통신 오류 ({e})"}
        # 연결 시작 전에 보낸 메시지라도 최근 30분 안이면 인정한다 (먼저 보내 놓는 경우가 많아서)
        private = [u for u in updates if (u.get("message") or {}).get("chat", {}).get("type") == "private"]
        recent = [u for u in private if u["message"].get("date", 0) >= time.time() - 1800]
        chat = recent[-1]["message"]["chat"]["id"] if recent else None
        if chat is None:
            others = len(updates) - len(private)
            seen = (f"봇이 받은 메시지 {len(private)}개" + (" (30분이 지난 것뿐이에요)" if private else "")
                    + (f" · 메시지가 아닌 소식 {others}개" if others else ""))
            left = int(pending["expires"] - time.time()) // 60
            return 200, {"ok": True, "linked": False,
                         "message": f"@{bot} 에게 메시지를 기다리는 중… {seen} · {left}분 남음"}
        try:  # 읽은 메시지는 정리해 둔다
            telegram.call(token, "getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0})
        except telegram.TelegramError:
            pass
        write_env({telegram.TOKEN: token, telegram.CHAT: str(chat), BOT_NAME_KEY: bot,
                   PENDING_TOKEN: "", PENDING_BOT: "", PENDING_UNTIL: ""})
    try:
        telegram.send("🥧 My_Pie와 연결됐어요!\n이제 웹페이지 로그인 코드와 매매 알림이 여기로 와요.")
    except telegram.TelegramError:
        pass
    return 200, {"ok": True, "linked": True, "message": f"@{bot} 연결 완료! 텔레그램을 확인해 보세요."}


def telegram_action(db_path: str, data: dict) -> tuple[int, dict]:
    """연결된 텔레그램 관리: 테스트 메시지, 알림·로그인 켜고 끄기, 연결 끊기 (PIN 필요)."""
    failure = _pin_ok(db_path, str(data.get("pin", "")))
    if failure:
        return failure[0], {"ok": False, "message": failure[1]}
    action = data.get("action")
    if action == "test":
        try:
            telegram.send("🥧 My_Pie 테스트 메시지예요. 잘 오고 있어요!")
        except telegram.TelegramError as e:
            return 502, {"ok": False, "message": f"보내지 못했어요 ({e})"}
        return 200, {"ok": True, "message": "테스트 메시지를 보냈어요."}
    if action == "off":
        write_env({telegram.TOKEN: "", telegram.CHAT: "", BOT_NAME_KEY: ""})
        return 200, {"ok": True, "message": "텔레그램 연결을 끊었어요. 이제 로그인 없이 열려요."}
    if action in ("notify", "login"):
        db = Database(db_path, create=False)
        try:
            settings = telegram_settings(db)
            settings[action] = bool(data.get("value"))
            db.set_state(TG_SETTINGS_KEY, settings)
        finally:
            db.close()
        label = "매매 알림" if action == "notify" else "로그인 코드 요구"
        return 200, {"ok": True, "message": f"{label}을(를) {'켰어요' if settings[action] else '껐어요'}."}
    return 400, {"ok": False, "message": "알 수 없는 요청이에요."}


# ---- 로그인 (텔레그램 코드) ----

_login_lock = threading.Lock()


def login_required(db_path: str, enabled_in_config: bool) -> bool:
    if not enabled_in_config or telegram.load() is None:
        return False
    db = _open(db_path)
    if not db:
        return False
    try:
        return telegram_settings(db)["login"]
    finally:
        db.close()


def login_send(db_path: str, digits: int, client: dict) -> tuple[int, dict]:
    creds = telegram.load()
    db = _open(db_path)
    if not creds or not db:
        return 400, {"ok": False, "message": "텔레그램이 연결되지 않았어요."}
    try:
        with _login_lock:
            code = auth.issue_code(db, digits)
    except auth.TooSoon as e:
        return 429, {"ok": False, "message": str(e), "wait": e.wait}
    finally:
        db.close()
    try:
        telegram.send(f"{code} ({client['ip']})", creds)  # 짧게: 코드와 요청한 기기 IP만
    except telegram.TelegramError as e:
        return 502, {"ok": False, "message": f"텔레그램으로 보내지 못했어요 ({e}). "
                                             "파이에서 python3 -m mypie.telegram --code 로 코드를 받을 수도 있어요."}
    return 200, {"ok": True, "message": "텔레그램으로 로그인 코드를 보냈어요.", "wait": auth.RESEND_SEC}


def login_verify(db_path: str, code: str, days: int, client: dict) -> tuple[int, dict, str | None]:
    db = _open(db_path)
    if not db:
        return 503, {"ok": False, "message": "봇이 아직 시작되지 않았어요."}, None
    try:
        with _login_lock:
            ok, message = auth.check_code(db, code)
            token = auth.create_session(db, days, client) if ok else None
    finally:
        db.close()
    if not ok:
        return 403, {"ok": False, "message": message}, None
    try:
        telegram.send(f"🔓 로그인 ({client['ip']})")
    except telegram.TelegramError:
        pass
    return 200, {"ok": True, "message": "로그인했어요."}, token


class Handler(BaseHTTPRequestHandler):
    server_version = "MyPie"
    db_path = ""
    stock_db_path = ""
    max_budget = 1_000_000.0
    login_enabled = True
    code_digits = 2
    session_days = 30
    cfg: dict = {}
    COOKIE = "mypie_session"

    def _cookie_token(self) -> str | None:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except CookieError:
            return None
        return jar[self.COOKIE].value if self.COOKIE in jar else None

    def _client(self) -> dict:
        return {"ip": self.client_address[0], "ua": (self.headers.get("User-Agent") or "?")[:80]}

    def _authed(self) -> bool:
        if not login_required(self.db_path, self.login_enabled):
            return True
        db = _open(self.db_path)
        try:
            return bool(db) and auth.valid_session(db, self._cookie_token())
        finally:
            if db:
                db.close()

    def _login_page(self) -> None:
        page = (STATIC_DIR / "login.html").read_text(encoding="utf-8").replace("__DIGITS__", str(self.code_digits))
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        route, query = url.path, parse_qs(url.query)
        if route == "/app.css":  # 화면 모양만 있는 공용 파일이라 로그인 전에도 준다
            self._send(200, (STATIC_DIR / "app.css").read_bytes(), "text/css; charset=utf-8")
            return
        if route == "/healthz":
            self._send(200, b"ok", "text/plain")
            return
        if not self._authed():
            if route.startswith("/api/"):
                self._json(401, {"ok": False, "login": True, "message": "로그인이 필요해요."})
            else:
                self._login_page()
            return
        if route in ("/stock", "/stock.html"):
            self._send(200, (STATIC_DIR / "stock.html").read_bytes(), "text/html; charset=utf-8")
            return
        if route in ("/", "/index.html"):
            self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        builders = {
            "/api/status": lambda: build_payload(self.db_path, self.max_budget),
            "/api/candles": lambda: build_candles(self.db_path, query.get("market", ["KRW-BTC"])[0],
                                                  max(20, min(int(query.get("bars", query.get("days", ["90"]))[0]), 400)),
                                                  query.get("tf", ["1d"])[0]),
            "/api/simulate": lambda: build_simulation(self.db_path, self.cfg, query.get("market", ["KRW-BTC"])[0],
                                                      query.get("tf", ["1d"])[0], int(query.get("days", ["0"])[0] or 0),
                                                      query.get("strategy", [""])[0]),
            "/api/stats": lambda: build_stats(self.db_path),
            "/api/system": lambda: build_system(self.db_path),
            "/api/stock/status": lambda: build_stock_payload(self.stock_db_path, self.db_path),
            "/api/stock/search": lambda: stock_search(self.stock_db_path, query.get("q", [""])[0][:40]),
            "/api/stock/candles": lambda: (
                build_stock_intraday(self.stock_db_path, query.get("symbol", [""])[0], query["span"][0])
                if query.get("span", [""])[0] in INTRADAY_SPANS else
                build_stock_candles(self.stock_db_path, query.get("symbol", [""])[0],
                                    max(20, min(int(query.get("bars", ["120"])[0]), 800)))),
            "/api/stock/simulate": lambda: build_stock_simulation(self.stock_db_path, self.cfg, query.get("symbol", [""])[0],
                                                                  int(query.get("days", ["0"])[0] or 0),
                                                                  query.get("strategy", [""])[0]),
        }
        if route in builders:
            try:
                self._json(200, builders[route]())
            except Exception as e:
                log.exception("%s 조회 실패", route)
                self._json(500, {"error": str(e)})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        public = ("/api/login/send", "/api/login/verify")
        private = ("/api/control", "/api/ranking", "/api/logout",
                   "/api/telegram/start", "/api/telegram/link", "/api/telegram/action",
                   "/api/stock/control", "/api/stock/ranking")
        if route not in public + private:
            self._send(404, b"not found", "text/plain")
            return
        if route in private and not self._authed():
            self._json(401, {"ok": False, "login": True, "message": "로그인이 필요해요."})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "message": "요청이 너무 커요."})
            return
        cookie = None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
            if route == "/api/control":
                code, message = request_control(self.db_path, data, self.max_budget)
                body = {"ok": code == 200, "message": message}
            elif route == "/api/ranking":
                code, message = request_ranking(self.db_path, str(data.get("market", "")), str(data.get("tf") or "1d"))
                body = {"ok": code == 200, "message": message}
            elif route == "/api/stock/control":
                code, body = request_stock(self.stock_db_path, self.db_path, data)
            elif route == "/api/stock/ranking":
                code, message = request_stock_ranking(self.stock_db_path, str(data.get("symbol", "")))
                body = {"ok": code == 200, "message": message}
            elif route == "/api/login/send":
                code, body = login_send(self.db_path, self.code_digits, self._client())
            elif route == "/api/login/verify":
                code, body, token = login_verify(self.db_path, str(data.get("code", "")), self.session_days, self._client())
                if token:
                    cookie = (f"{self.COOKIE}={token}; Max-Age={self.session_days * 86400}; Path=/; "
                              "HttpOnly; SameSite=Strict")
            elif route == "/api/logout":
                db = _open(self.db_path)
                if db:
                    auth.end_session(db, self._cookie_token())
                    db.close()
                code, body = 200, {"ok": True, "message": "로그아웃했어요."}
                cookie = f"{self.COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
            elif route == "/api/telegram/start":
                code, body = telegram_start(self.db_path, data)
            elif route == "/api/telegram/link":
                code, body = telegram_link()
            else:
                code, body = telegram_action(self.db_path, data)
        except ValueError:
            code, body = 400, {"ok": False, "message": "잘못된 요청이에요."}
        self._json(code, body, cookie)

    def _json(self, code: int, data: dict, cookie: str | None = None) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", cookie)

    def _send(self, code: int, body: bytes, content_type: str, cookie: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        pass  # 5초마다 오는 새로고침 요청으로 로그가 넘치지 않게


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    Handler.db_path = cfg["general"]["db_path"]
    Handler.max_budget = float(cfg["live"]["max_budget"])
    Handler.login_enabled = bool(cfg["web"]["login"])
    Handler.code_digits = max(2, min(int(cfg["web"]["code_digits"]), 8))
    Handler.session_days = int(cfg["web"]["session_days"])
    Handler.cfg = cfg
    Handler.stock_db_path = cfg["stock"]["db_path"]
    host, port = cfg["web"]["host"], int(cfg["web"]["port"])
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("모니터링 웹페이지 시작: http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
