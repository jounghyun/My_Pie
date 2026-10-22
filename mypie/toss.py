"""토스증권 Open API 연결 (국내 주식). 표준 라이브러리만 쓴다.

키 등록 (파이에서): python3 -m mypie.toss          client_id·client_secret 입력 → 연결 확인 → 계좌 저장
연결 확인:          python3 -m mypie.toss --check

공식 문서: https://developers.tossinvest.com (REST 주소 https://openapi.tossinvest.com)
  - 인증: client_id·client_secret으로 하루짜리 토큰을 받는다. 토큰은 하나만 유효해서(새로 받으면 예전 것이 끊김)
    주식 봇 DB에 저장해 두고 봇과 이 명령이 같이 쓴다
  - 계좌·주문 API에는 X-Tossinvest-Account(계좌 순번 accountSeq)를 붙인다
  - 허용 IP에 없는 곳에서 부르면 403 (ip-not-allowed). 집 IP가 바뀌면 토스증권 WTS에서 허용 IP를 고쳐야 한다
  - 현재가 API는 가격만 준다. 오늘 시가·전날 종가는 일봉(없으면 오늘 첫 1분봉)으로 하루 한 번 구한다
"""

import argparse
import getpass
import gzip
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .keys import ENV_PATH, read_env, write_env
from .timeutil import now_kst

log = logging.getLogger("mypie.toss")

BASE = "https://openapi.tossinvest.com"
CLIENT_ID = "TOSS_CLIENT_ID"
CLIENT_SECRET = "TOSS_CLIENT_SECRET"
ACCOUNT = "TOSS_ACCOUNT"          # 계좌 순번 (accountSeq). 계좌번호가 아니다
TOKEN_KEY = "toss_token"          # 주식 봇 DB에 저장하는 토큰
TOKEN_SKEW = 60                   # 만료 이만큼 전에 미리 새로 받는다 (초)
AUTH_ERRORS = ("invalid-token", "expired-token", "token-revoked")
# 넥스트레이드(NXT) 개장일. 이날부터 NXT 종목의 일봉은 08:00~20:00 거래를 다 합친 값이라, 봇이 사고파는
# 정규장(09:00~15:30) 일봉은 1분봉으로 따로 만든다. 1분봉 ts는 봉이 끝난 시각이라 09:01~15:30 봉이 정규장이다
NXT_START = "2025-03-04"
REGULAR = ("09:00", "15:30")
IP_ERRORS = ("ip-not-allowed", "access_denied")
# 같은 종류의 호출 사이 최소 간격 (초). 토스증권 호출 한도보다 넉넉하게
PACING = {"/api/v1/stocks/all": 1.1, "/api/v1/accounts": 1.1, "/api/v1/buying-power": 0.4,
          "/api/v1/candles": 0.06, "/api/v1/market-calendar": 0.4, "/api/v1/stocks": 0.25}
STATUS = {"FILLED": "filled", "CANCELED": "cancelled", "REJECTED": "rejected", "REPLACED": "cancelled",
          "PARTIAL_FILLED": "partial"}  # 나머지(PENDING 등)는 'open'


class TossError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


class OrderRejected(TossError):
    """토스증권이 주문을 확실히 거절했다 (잔고 부족, 장 시간 아님 등). 진행 중인 주문이 아니다."""


class OrderUnknown(TossError):
    """주문이 들어갔는지 모른다. 다음 루프에서 recover()가 확인한다."""


KEY_ERRORS = ("invalid_client", "login-user-not-found")  # Client ID·Secret이 만료·취소됐거나 틀림
RENEW_STEPS = ("갱신 방법\n"
               "1) PC에서 토스증권 웹(WTS) → 설정 → Open API → Client ID·Secret 새로 발급 (허용 IP에 집 IP)\n"
               "2) 파이에서: cd ~/My_Pie && python3 -m mypie.toss  (두 값 붙여넣기)\n"
               "3) 확인: python3 -m mypie.toss --check")


def is_key_error(error: BaseException | None) -> bool:
    """토스증권이 '키를 쓸 수 없다'고 거절한 오류인지 (원인 오류까지 따라가며 확인)."""
    while error is not None:
        if isinstance(error, TossError) and error.status == 401 and error.code in KEY_ERRORS:
            return True
        error = error.__cause__
    return False


def expired_text() -> str:
    return ("🔑 토스증권 API 키(Client ID·Secret)가 만료됐거나 더 쓸 수 없어요. 주식 주문과 시세 확인이 멈춰 있어요.\n\n"
            + RENEW_STEPS)


def is_ip_error(error: BaseException | None) -> bool:
    """허용 IP에 없는 곳에서 불러서 거절당한 오류인지 (원인 오류까지 따라가며 확인)."""
    while error is not None:
        if isinstance(error, TossError) and error.status == 403 and error.code in IP_ERRORS:
            return True
        error = error.__cause__
    return False


def load_credentials() -> tuple[str, str, str] | None:
    """(client_id, client_secret, 계좌 순번). 등록 안 했으면 None."""
    env = read_env()
    if env.get(CLIENT_ID) and env.get(CLIENT_SECRET) and env.get(ACCOUNT):
        return env[CLIENT_ID], env[CLIENT_SECRET], env[ACCOUNT]
    return None


def _decode(raw: bytes, headers) -> dict | None:
    if raw[:2] == b"\x1f\x8b" or (headers.get("Content-Encoding") or "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8")) if raw else None


def _num(value) -> float:
    return float(value) if value not in (None, "") else 0.0


def _day(timestamp: str) -> str:
    return timestamp[:10]


class TossClient:
    def __init__(self, client_id: str, client_secret: str, account: str | None = None, db=None, timeout: float = 15):
        self.client_id, self.client_secret = client_id, client_secret
        self.account = str(account) if account is not None else None
        self.db = db
        self.timeout = timeout
        self._token: dict | None = None
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._day_info: dict[tuple[str, str], dict] = {}

    # ---- 인증과 호출 ----

    def _issue_token(self) -> dict:
        data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.client_secret}).encode()
        request = urllib.request.Request(BASE + "/oauth2/token", data=data, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = _decode(response.read(), response.headers)
        except urllib.error.HTTPError as e:
            body = _safe_body(e)
            code = body.get("error") if isinstance(body.get("error"), str) else None
            if e.code == 403:
                raise TossError("토스증권이 이 IP를 허용하지 않아요. WTS → 설정 → Open API → 허용 IP에 집 IP를 등록하세요",
                                403, code or "access_denied") from None
            raise TossError(f"토스증권 인증 실패 ({body.get('error_description') or code or e.code})", e.code, code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TossError(f"토스증권 통신 오류 ({getattr(e, 'reason', e)})") from None
        token = {"token": body["access_token"], "expires_at": time.time() + int(body.get("expires_in", 86400)),
                 "client": self.client_id[-6:]}
        if self.db is not None:
            self.db.set_state(TOKEN_KEY, token)
        return token

    def _current_token(self, refresh: bool = False, used: str | None = None) -> str:
        with self._lock:
            if self.db is not None and (refresh or self._token is None):
                saved = self.db.get_state(TOKEN_KEY)  # 다른 프로세스(키 확인 명령)가 새로 받았을 수 있다
                if saved and saved.get("client") == self.client_id[-6:] and saved["token"] != used:
                    self._token = saved
            if (self._token is None or self._token["expires_at"] - TOKEN_SKEW < time.time()
                    or (refresh and self._token["token"] == used)):
                self._token = self._issue_token()
            return self._token["token"]

    def _pace(self, path: str) -> None:
        for prefix, gap in PACING.items():
            if path.startswith(prefix):
                wait = self._last.get(prefix, 0) + gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last[prefix] = time.monotonic()
                return

    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None,
              account: bool = False, retry: bool = True):
        url = BASE + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        token = self._current_token()
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "Accept-Encoding": "gzip"}
        if account:
            if not self.account:
                raise TossError("토스증권 계좌가 정해지지 않았어요. python3 -m mypie.toss 로 다시 등록하세요")
            headers["X-Tossinvest-Account"] = self.account
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        self._pace(path)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = _decode(response.read(), response.headers) or {}
            return payload.get("result")
        except urllib.error.HTTPError as e:
            payload = _safe_body(e)
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = error.get("code")
            if retry and e.code == 401 and code in AUTH_ERRORS:
                self._current_token(refresh=True, used=token)
                return self._call(method, path, query, body, account, retry=False)
            if retry and e.code == 429:
                time.sleep(min(float(e.headers.get("Retry-After") or 1), 5))
                return self._call(method, path, query, body, account, retry=False)
            message = error.get("message") or code or f"HTTP {e.code}"
            if e.code == 403 and code == "ip-not-allowed":
                message = "토스증권이 이 IP를 허용하지 않아요. WTS → 설정 → Open API → 허용 IP를 고쳐 주세요"
            raise TossError(f"{message} ({code or e.code})", e.code, code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TossError(f"토스증권 통신 오류 ({getattr(e, 'reason', e)})") from None

    # ---- 시세 (토큰만 있으면 된다) ----

    def daily_candles(self, symbol: str, count: int, before: str | None = None) -> list[dict]:
        """일봉 (최신 → 과거). before(YYYY-MM-DD)를 주면 그날 이전 것만."""
        query = {"symbol": symbol, "interval": "1d", "count": min(count + (1 if before else 0), 200)}
        if before:
            query["before"] = f"{before}T00:00:00+09:00"  # before는 그 시각을 포함해서 하나 더 받고 걸러낸다
        result = self._call("GET", "/api/v1/candles", query) or {}
        rows = [{"day": _day(c["timestamp"]), "open": _num(c["openPrice"]), "high": _num(c["highPrice"]),
                 "low": _num(c["lowPrice"]), "close": _num(c["closePrice"]), "volume": _num(c.get("volume"))}
                for c in result.get("candles") or []]
        if before:
            rows = [r for r in rows if r["day"] < before]
        return rows[:count]

    def minute_candles(self, symbol: str, before: str | None = None) -> tuple[list[dict], str | None]:
        """1분봉 200개 (최신 → 과거)와 그 앞을 받을 커서. 'ts'는 봉이 끝난 시각 (09:01 봉 = 09:00~09:01)."""
        query = {"symbol": symbol, "interval": "1m", "count": 200, "before": before}
        result = self._call("GET", "/api/v1/candles", query) or {}
        rows = [{"ts": c["timestamp"][:16], "open": _num(c["openPrice"]), "high": _num(c["highPrice"]),
                 "low": _num(c["lowPrice"]), "close": _num(c["closePrice"]), "volume": _num(c.get("volume"))}
                for c in result.get("candles") or []]
        return rows, result.get("nextBefore")

    def _today_info(self, symbol: str, today: str) -> dict:
        """오늘 시가와 전날 종가. 시가는 첫 거래가 생긴 뒤에 한 번 구하면 하루 동안 그대로 쓴다.
        시가는 정규장 첫 1분봉의 시가다 (일봉 시가는 NXT 종목이면 08:00 프리마켓 가격이라 봇이 살 수 없는 값)."""
        cached = self._day_info.get((symbol, today))
        if cached and cached.get("open"):
            return cached
        daily = self.daily_candles(symbol, 2)
        info = {"open": None, "prev_close": None}
        if daily:
            info["prev_close"] = (daily[1]["close"] if len(daily) > 1 else None) if daily[0]["day"] == today \
                else daily[0]["close"]
        if now_kst().strftime("%H:%M") >= REGULAR[0]:
            info["open"] = self._regular_open(symbol, today)
        self._day_info = {k: v for k, v in self._day_info.items() if k[1] == today}
        self._day_info[(symbol, today)] = info
        return info

    def _regular_open(self, symbol: str, today: str) -> float | None:
        for until in ("09:30", "15:30"):  # 장 초반 30분에 거래가 없던 종목이면 그 뒤에서 찾는다
            rows, _ = self.minute_candles(symbol, f"{today}T{until}:00+09:00")
            regular = [m for m in rows if m["ts"][:10] == today and REGULAR[0] < m["ts"][11:16] <= REGULAR[1]]
            if regular:
                return min(regular, key=lambda m: m["ts"])["open"]
        return None

    def regular_day(self, symbol: str, day: str) -> dict | None:
        """그날 정규장(09:00~15:30)만의 일봉. 1분봉을 두 번(15:30부터, 12:10부터 거꾸로 200개씩) 받아 만든다.
        정규장 거래가 없었으면 None."""
        bars: dict[str, dict] = {}
        for until in ("15:30", "12:10"):
            rows, _ = self.minute_candles(symbol, f"{day}T{until}:00+09:00")
            bars.update({m["ts"]: m for m in rows if m["ts"][:10] == day and REGULAR[0] < m["ts"][11:16] <= REGULAR[1]})
        if not bars:
            return None
        part = [bars[k] for k in sorted(bars)]
        return {"day": day, "open": part[0]["open"], "high": max(m["high"] for m in part),
                "low": min(m["low"] for m in part), "close": part[-1]["close"], "volume": sum(m["volume"] for m in part)}

    def nxt_supported(self, symbols: list[str]) -> dict[str, bool]:
        """종목마다 넥스트레이드(NXT)에서도 거래되는지."""
        result = self._call("GET", "/api/v1/stocks", {"symbols": ",".join(symbols)}) or []
        return {s["symbol"]: bool((s.get("koreanMarketDetail") or {}).get("nxtSupported")) for s in result}

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """{종목: {"price", "open", "prev_close"}}. 현재가는 한 번에 받고, 시가·전날 종가는 종목마다 하루 한 번."""
        if not symbols:
            return {}
        today = now_kst().date().isoformat()
        out = {}
        for p in self._call("GET", "/api/v1/prices", {"symbols": ",".join(symbols)}) or []:
            if p.get("lastPrice") in (None, ""):
                continue
            out[p["symbol"]] = {"price": _num(p["lastPrice"]), **self._today_info(p["symbol"], today)}
        return out

    def is_trading_day(self, day: str) -> bool:
        """토스증권 장 달력으로 그날 정규장이 열리는지."""
        result = self._call("GET", "/api/v1/market-calendar/KR", {"date": day}) or {}
        today = (result.get("today") or {})
        return bool(today.get("integrated") and today["integrated"].get("regularMarket"))

    def stocks(self) -> list[dict]:
        """국내 주식 종목 목록 (코스피·코스닥)."""
        out = []
        for market in ("KOSPI", "KOSDAQ"):
            for s in self._call("GET", "/api/v1/stocks/all", {"market": market}) or []:
                out.append({"symbol": s["symbol"], "name": s["name"], "market": market})
        return out

    # ---- 계좌 (계좌 순번 필요) ----

    def accounts(self) -> list[dict]:
        return self._call("GET", "/api/v1/accounts") or []

    def cash(self) -> float:
        """미수 없이 살 수 있는 현금 (주문가능 현금)."""
        result = self._call("GET", "/api/v1/buying-power", {"currency": "KRW"}, account=True) or {}
        return _num(result.get("cashBuyingPower"))

    def holdings(self) -> dict[str, dict]:
        """{종목: {"qty", "avg_price", "name"}} (국내 주식만)."""
        result = self._call("GET", "/api/v1/holdings", account=True) or {}
        return {i["symbol"]: {"qty": int(_num(i["quantity"])), "avg_price": _num(i.get("averagePurchasePrice")),
                              "name": i.get("name") or i["symbol"]}
                for i in result.get("items") or [] if i.get("marketCountry", "KR") == "KR"}

    # ---- 주문 ----

    def place_order(self, symbol: str, side: str, qty: int, price: int, client_id: str) -> str:
        """지정가 주문. 주문번호를 돌려준다. 확실히 거절되면 OrderRejected."""
        body = {"clientOrderId": client_id, "symbol": symbol, "side": side.upper(), "orderType": "LIMIT",
                "quantity": str(int(qty)), "price": str(int(price)), "confirmHighValueOrder": qty * price >= 100_000_000}
        try:
            result = self._call("POST", "/api/v1/orders", body=body, account=True)
        except TossError as e:
            if e.status in (400, 422) or (e.status == 409 and e.code != "request-in-progress"):
                raise OrderRejected(str(e), e.status, e.code) from None
            raise
        return result["orderId"]

    def order(self, order_id: str) -> dict:
        o = self._call("GET", f"/api/v1/orders/{urllib.parse.quote(order_id, safe='')}", account=True)
        return self._order(o)

    @staticmethod
    def _order(o: dict) -> dict:
        ex = o.get("execution") or {}
        tax = ex.get("tax")
        commission = ex.get("commission")
        return {"id": o["orderId"], "status": STATUS.get(o.get("status"), "open"), "side": (o.get("side") or "").lower(),
                "qty": int(_num(o.get("quantity"))), "price": _num(o.get("price")), "ordered_at": o.get("orderedAt"),
                "filled_qty": int(_num(ex.get("filledQuantity"))), "avg_price": _num(ex.get("averageFilledPrice")),
                "fee": _num(commission) if commission not in (None, "") else None,
                "tax": _num(tax) if tax not in (None, "") else None}

    def find_order(self, client_id: str, symbol: str, side: str | None = None, qty: int | None = None,
                   price: float | None = None, since: str | None = None) -> dict | None:
        """주문번호를 적기 전에 끊겼을 때: 오늘 주문 중에 종목·방향·수량·가격·시각이 맞는 것을 찾는다.
        (토스증권은 우리 번호로 주문을 찾는 기능이 없다)"""
        today = now_kst().date().isoformat()
        orders = (self._call("GET", "/api/v1/orders", {"status": "OPEN", "symbol": symbol}, account=True) or {}).get("orders") or []
        cursor = None
        while True:
            page = self._call("GET", "/api/v1/orders", {"status": "CLOSED", "symbol": symbol, "from": today, "to": today,
                                                        "limit": 100, "cursor": cursor}, account=True) or {}
            orders += page.get("orders") or []
            if not page.get("hasNext"):
                break
            cursor = page.get("nextCursor")
        for o in sorted(orders, key=lambda x: x.get("orderedAt") or ""):
            info = self._order(o)
            if side and info["side"] != side:
                continue
            if qty and info["qty"] != qty:
                continue
            if price and info["price"] and abs(info["price"] - price) > 0.5:
                continue
            if since and (o.get("orderedAt") or "")[:19].replace("T", " ") < since:
                continue
            return info
        return None

    def cancel(self, order_id: str) -> None:
        try:
            self._call("POST", f"/api/v1/orders/{urllib.parse.quote(order_id, safe='')}/cancel", body={}, account=True)
        except TossError as e:
            if e.status != 409:  # 이미 체결·취소된 주문이면 그대로 둔다
                raise


def _safe_body(e: urllib.error.HTTPError) -> dict:
    try:
        return _decode(e.read(), e.headers) or {}
    except (ValueError, OSError):
        return {}


def _connect(client_id: str, secret: str):
    from .config import load_config
    from .db import Database

    cfg = load_config()
    return TossClient(client_id, secret, db=Database(cfg["stock"]["db_path"]))


def check() -> None:
    creds = load_credentials()
    if not creds:
        sys.exit("등록된 토스증권 키가 없어요. python3 -m mypie.toss 로 등록하세요.")
    client = _connect(*creds[:2])
    client.account = creds[2]
    try:
        cash, holdings = client.cash(), client.holdings()
    except TossError as e:
        sys.exit(f"토스증권 연결 실패: {e}")
    print(f"연결 정상 · 계좌 순번 {creds[2]} · 주문가능 현금 {cash:,.0f}원 · 국내 주식 {len(holdings)}종목")


def main() -> None:
    parser = argparse.ArgumentParser(description="토스증권 Open API 키 등록/확인")
    parser.add_argument("--check", action="store_true", help="등록된 키가 되는지만 확인")
    if parser.parse_args().check:
        check()
        return

    print("토스증권 Open API 키를 등록해요. 붙여넣어도 화면에는 안 보여요.\n"
          "(토스증권 PC 웹 → 설정 → Open API 에서 발급, 허용 IP에 집 IP를 꼭 등록하세요)")
    client_id = getpass.getpass("Client ID: ").strip()
    secret = getpass.getpass("Client Secret: ").strip()
    if not client_id or not secret:
        sys.exit("키가 비어 있어요. 다시 해 주세요.")
    client = _connect(client_id, secret)
    print("토스증권에 연결해 보는 중...")
    try:
        accounts = [a for a in client.accounts() if a.get("accountType", "BROKERAGE") == "BROKERAGE"]
    except TossError as e:
        sys.exit(f"연결 실패: {e}")
    if not accounts:
        sys.exit("종합매매 계좌를 찾지 못했어요. 토스증권 앱에서 계좌를 확인해 주세요.")
    choice = accounts[0]
    if len(accounts) > 1:
        for i, a in enumerate(accounts, 1):
            print(f"  {i}) 계좌 ****{str(a['accountNo'])[-4:]}")
        pick = input("쓸 계좌 번호를 고르세요 (1): ").strip() or "1"
        choice = accounts[int(pick) - 1]
    client.account = str(choice["accountSeq"])
    try:
        cash = client.cash()
    except TossError as e:
        sys.exit(f"계좌 조회 실패: {e}")
    write_env({CLIENT_ID: client_id, CLIENT_SECRET: secret, ACCOUNT: client.account})
    print(f"연결 성공! 키를 {ENV_PATH} 에 저장했어요 (나만 읽기 가능).")
    print(f"  계좌 ****{str(choice['accountNo'])[-4:]} · 주문가능 현금 {cash:,.0f}원")
    print("토스증권 앱에서 '투자자지시 거래소'를 '통합(SOR)'으로 해 두세요 (아니면 주문이 거절돼요).")
    print("주식 봇이 1분 안에 키를 알아채고 모의투자로 시작해요. 웹페이지 '📈 주식' 화면을 보세요.")


if __name__ == "__main__":
    main()
