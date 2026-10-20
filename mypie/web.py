"""모니터링 웹페이지. 봇이 DB에 남긴 기록을 보여주고, PIN을 알면 종목·계좌·전략을 바꿀 수 있다.

실행: python3 -m mypie.web   →   http://<파이 IP>:8000
"""

import json
import logging
import re
import threading
import time
from collections import deque
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import pin
from .broker import MIN_ORDER_KRW
from .config import load_config
from .db import Database
from .keys import load_keys
from .strategies import STRATEGIES
from .timeutil import now_kst, ts

log = logging.getLogger("mypie.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
EQUITY_DAYS = 7
MAX_PIN_FAILURES = 5          # 이만큼 틀리면
PIN_LOCK_SEC = 600            # 이 시간 동안 잠근다
MAX_BODY_BYTES = 2048
MARKET_PATTERN = re.compile(r"^KRW-[A-Z0-9]{1,15}$")

_pin_lock = threading.Lock()
_pin_failures: deque[float] = deque()


def build_payload(db_path: str, max_budget: float) -> dict:
    payload = {
        "server_time": ts(), "status": None, "trades": [], "events": [], "equity": [],
        "ranking": None, "markets": [], "pin_set": False, "pending": None,
        "live_ready": load_keys() is not None,
        "limits": {"min_budget": MIN_ORDER_KRW, "max_budget": max_budget},
        "strategies": [{"name": c.name, "label": c.label, "description": c.description, "guide": c.guide}
                       for c in STRATEGIES.values()],
    }
    if not Path(db_path).exists():
        return payload
    db = Database(db_path, create=False)
    try:
        status = db.get_state("status")
        payload["status"] = status
        payload["trades"] = db.recent_trades(30)
        payload["events"] = db.recent_events(30)
        if status:
            since = ts(now_kst() - timedelta(days=EQUITY_DAYS))
            payload["equity"] = db.equity_since(status["mode"], since)
        payload["ranking"] = db.get_state("ranking")
        payload["markets"] = (db.get_state("markets") or {}).get("list", [])
        payload["pin_set"] = db.get_state(pin.STATE_KEY) is not None
        payload["pending"] = db.get_state("control_request")
    finally:
        db.close()
    return payload


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


def _with_pin(db_path: str, pin_input: str, key: str, value: dict) -> tuple[int, str] | None:
    """PIN을 확인하고 봇에게 보낼 요청을 DB에 남긴다. 실패하면 (상태 코드, 안내 문구)."""
    if not Path(db_path).exists():
        return 503, "봇이 아직 시작되지 않았어요."
    with _pin_lock:  # 한 번에 하나씩만 확인해서 무작위 대입을 느리게 만든다
        db = Database(db_path, create=False)
        try:
            failure = _check_pin(db, pin_input)
            if failure:
                return failure
            db.set_state(key, {**value, "requested_at": ts()})
        finally:
            db.close()
    return None


def request_control(db_path: str, data: dict, max_budget: float) -> tuple[int, str]:
    """종목·계좌·한도·전략 변경 요청. (HTTP 상태 코드, 안내 문구)를 돌려준다."""
    strategy, market, mode = str(data.get("strategy", "")), str(data.get("market", "")), str(data.get("mode", ""))
    try:
        budget = float(data.get("budget") or 0)
    except (TypeError, ValueError):
        return 400, "투자 한도를 숫자로 입력해 주세요."
    if strategy not in STRATEGIES:
        return 400, "알 수 없는 전략이에요."
    if not MARKET_PATTERN.match(market):
        return 400, "종목을 다시 골라 주세요."
    if mode not in ("paper", "live"):
        return 400, "계좌를 다시 골라 주세요."
    if mode == "live":
        if not (MIN_ORDER_KRW <= budget <= max_budget):
            return 400, f"투자 한도는 {MIN_ORDER_KRW:,}원 ~ {max_budget:,.0f}원 사이로 정해 주세요."
        if load_keys() is None:
            return 400, "업비트 API 키가 아직 없어요. 파이에서 python3 -m mypie.keys 로 먼저 등록하세요."
        if data.get("confirm_live") is not True:
            return 400, "실거래는 '실제 돈으로 거래한다'에 체크해야 바꿀 수 있어요."
    request = {"strategy": strategy, "market": market, "mode": mode, "budget": budget}
    failure = _with_pin(db_path, str(data.get("pin", "")), "control_request", request)
    if failure:
        return failure
    log.info("설정 변경 요청: %s", request)
    return 200, "봇에게 전했어요. 10초 안에 적용돼요."


def request_resume(db_path: str, pin_input: str) -> tuple[int, str]:
    failure = _with_pin(db_path, pin_input, "resume_request", {})
    return failure or (200, "다시 시작하라고 봇에게 전했어요.")


class Handler(BaseHTTPRequestHandler):
    server_version = "MyPie"
    db_path = ""
    max_budget = 1_000_000.0

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif route == "/api/status":
            try:
                self._json(200, build_payload(self.db_path, self.max_budget))
            except Exception as e:
                log.exception("상태 조회 실패")
                self._json(500, {"error": str(e)})
        elif route == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in ("/api/control", "/api/resume"):
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
                code, message = request_resume(self.db_path, str(data.get("pin", "")))
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
