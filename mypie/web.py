"""모니터링 웹페이지. 봇이 DB에 남긴 기록을 보여주고, PIN을 알면 종목·계좌·전략을 바꿀 수 있다.

실행: python3 -m mypie.web   →   http://<파이 IP>:8000

  GET  /api/status   봇 상태, 순위, 기록 (5초마다)
  GET  /api/candles  차트용 일봉과 매매 지점 (?market=KRW-BTC&days=90)
  GET  /api/stats    성과 통계, 종목별 성적, 날짜별 손익
  GET  /api/system   라즈베리파이 상태 (온도, 메모리, 디스크, 전원)
  POST /api/control  포트폴리오(종목·전략·비중)·계좌·한도 변경 (PIN 필요)
  POST /api/ranking  아직 순위가 없는 종목의 전략 순위 계산 부탁 (PIN 불필요)
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
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import pin
from .bot import validate_slots
from .broker import MIN_ORDER_KRW
from .config import load_config
from .db import Database
from .keys import load_keys
from .strategies import STRATEGIES
from .timeutil import now_kst, ts

log = logging.getLogger("mypie.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
EQUITY_DAYS = 7
CALENDAR_DAYS = 84            # 손익 캘린더 기간 (12주)
MAX_PIN_FAILURES = 5          # 이만큼 틀리면
PIN_LOCK_SEC = 600            # 이 시간 동안 잠근다
MAX_BODY_BYTES = 2048
MARKET_PATTERN = re.compile(r"^KRW-[A-Z0-9]{1,15}$")

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
    finally:
        db.close()
    return payload


def build_candles(db_path: str, market: str, days: int) -> dict:
    """차트용: 한 종목의 일봉 + 그 기간의 매매 지점."""
    db = _open(db_path)
    if not db or not MARKET_PATTERN.match(market):
        return {"market": market, "candles": [], "trades": []}
    try:
        status = db.get_state("status") or {}
        candles = db.candle_history(market, days)
        since = f"{candles[0]['day']} 09:00:00" if candles else ""
        trades = db.trades(status.get("mode", "paper"), market, since)
        tickers = (db.get_state("market_info") or {}).get("tickers") or {}
        return {"market": market, "candles": candles, "today": tickers.get(market),
                "trades": [{k: t[k] for k in ("ts", "side", "price", "profit")} for t in trades]}
    finally:
        db.close()


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
        if data.get("confirm_live") is not True:
            return 400, "실거래는 '실제 돈으로 거래한다'에 체크해야 바꿀 수 있어요."
    if not Path(db_path).exists():
        return 503, "봇이 아직 시작되지 않았어요."
    request = {"slots": slots, "mode": mode, "budget": "all" if budget_all else budget, "requested_at": ts()}
    with _pin_lock:  # 한 번에 하나씩만 확인해서 무작위 대입을 느리게 만든다
        db = Database(db_path, create=False)
        try:
            failure = _check_pin(db, str(data.get("pin", "")))
            if failure:
                return failure
            db.set_state("control_request", request)
        finally:
            db.close()
    log.info("설정 변경 요청: %s", request)
    return 200, "봇에게 전했어요. 10초 안에 적용돼요."


def request_ranking(db_path: str, market: str) -> tuple[int, str]:
    """아직 순위가 없는 종목의 전략 순위를 계산해 달라고 봇에게 부탁한다 (읽기만 하는 일이라 PIN 없이)."""
    if not MARKET_PATTERN.match(market):
        return 400, "잘못된 종목이에요."
    db = _open(db_path)
    if not db:
        return 503, "봇이 아직 시작되지 않았어요."
    try:
        queue = (db.get_state("ranking_request") or {}).get("markets", [])
        if market not in queue:
            queue = (queue + [market])[-10:]
        db.set_state("ranking_request", {"markets": queue, "requested_at": ts()})
    finally:
        db.close()
    return 200, f"{market} 전략 순위를 계산할게요. 10~30초쯤 걸려요."


class Handler(BaseHTTPRequestHandler):
    server_version = "MyPie"
    db_path = ""
    max_budget = 1_000_000.0

    def do_GET(self) -> None:
        url = urlparse(self.path)
        route, query = url.path, parse_qs(url.query)
        if route in ("/", "/index.html"):
            self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        builders = {
            "/api/status": lambda: build_payload(self.db_path, self.max_budget),
            "/api/candles": lambda: build_candles(self.db_path, query.get("market", ["KRW-BTC"])[0],
                                                  max(20, min(int(query.get("days", ["90"])[0]), 400))),
            "/api/stats": lambda: build_stats(self.db_path),
            "/api/system": lambda: build_system(self.db_path),
        }
        if route in builders:
            try:
                self._json(200, builders[route]())
            except Exception as e:
                log.exception("%s 조회 실패", route)
                self._json(500, {"error": str(e)})
        elif route == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in ("/api/control", "/api/ranking"):
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "message": "요청이 너무 커요."})
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
            if route == "/api/control":
                code, message = request_control(self.db_path, data, self.max_budget)
            else:
                code, message = request_ranking(self.db_path, str(data.get("market", "")))
        except ValueError:
            code, message = 400, "잘못된 요청이에요."
        self._json(code, {"ok": code == 200, "message": message})

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        pass  # 5초마다 오는 새로고침 요청으로 로그가 넘치지 않게


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    Handler.db_path = cfg["general"]["db_path"]
    Handler.max_budget = float(cfg["live"]["max_budget"])
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
