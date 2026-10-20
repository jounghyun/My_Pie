"""업비트 API 클라이언트. 표준 라이브러리만 쓴다.

- 시세(공개) API: 키 없이 쓸 수 있다. 모의투자는 이것만 쓴다.
- 거래(개인) API: Access/Secret 키로 서명해서 잔고 조회와 주문을 한다. 실거래에서만 쓴다.
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_URL = "https://api.upbit.com/v1"
USER_AGENT = "My_Pie/0.2"


class UpbitError(Exception):
    pass


class OrderNotPlaced(UpbitError):
    """주문을 넣기 전에 실패함 (예: 잔고 조회 통신 오류). 주문이 안 들어갔으니 나중에 다시 시도해도 안전하다."""


def _read_error(e: urllib.error.HTTPError) -> str:
    try:
        err = json.loads(e.read().decode("utf-8")).get("error", {})
        return f"{err.get('name', '')}: {err.get('message', '')}".strip(": ")
    except Exception:
        return ""


def _get(path: str, params: dict, timeout: float = 5, retries: int = 3, headers: dict | None = None):
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}" if params else f"{BASE_URL}{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                                                   **(headers or {})})
    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}"
            # 429(요청 과다)와 5xx는 잠시 후 재시도, 나머지 4xx는 요청 자체가 잘못된 것
            if 400 <= e.code < 500 and e.code != 429:
                raise UpbitError(f"{path} 요청 거부 (HTTP {e.code}) {_read_error(e)}") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last_error = str(getattr(e, "reason", e))
        time.sleep(0.5 * (attempt + 1))
    raise UpbitError(f"{path} 요청 실패 ({last_error})")


# ---- 시세 (공개) ----

def get_price(market: str) -> float:
    """현재가."""
    data = _get("/ticker", {"markets": market})
    return float(data[0]["trade_price"])


def get_daily_candles(market: str, count: int = 200, to: str | None = None) -> list[dict]:
    """일봉 목록 (최대 200개). 최신순이며 to가 없으면 [0]이 오늘(진행 중) 일봉이다.

    to: 이 거래일(YYYY-MM-DD)보다 이전 일봉만 받는다. 과거 데이터를 거슬러 받을 때 쓴다.
    """
    params = {"market": market, "count": min(count, 200)}
    if to:
        params["to"] = f"{to}T00:00:00"  # UTC 기준
    data = _get("/candles/days", params)
    return [
        {
            "day": raw["candle_date_time_utc"][:10],
            "open": float(raw["opening_price"]),
            "high": float(raw["high_price"]),
            "low": float(raw["low_price"]),
            "close": float(raw["trade_price"]),
            "volume": float(raw.get("candle_acc_trade_volume") or 0),
        }
        for raw in data
    ]


def get_krw_markets(limit: int = 30) -> list[dict]:
    """원화 마켓을 24시간 거래대금 순으로."""
    markets = [m for m in _get("/market/all", {"isDetails": "false"}) if m["market"].startswith("KRW-")]
    names = {m["market"]: m["korean_name"] for m in markets}
    tickers = _get("/ticker", {"markets": ",".join(names)})
    tickers.sort(key=lambda t: t.get("acc_trade_price_24h") or 0, reverse=True)
    return [{"market": t["market"], "name": names[t["market"]], "volume_24h": t.get("acc_trade_price_24h")}
            for t in tickers[:limit]]


# ---- 거래 (개인, 키 필요) ----

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_token(access_key: str, secret_key: str, params: dict | None = None) -> str:
    """업비트 인증용 JWT (HS256). 파라미터가 있으면 그 해시(SHA512)를 함께 서명한다."""
    payload = {"access_key": access_key, "nonce": str(uuid.uuid4())}
    if params:
        query = urllib.parse.unquote(urllib.parse.urlencode(params))
        payload["query_hash"] = hashlib.sha512(query.encode("utf-8")).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret_key.encode("utf-8"), f"{header}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(signature)}"


class UpbitClient:
    def __init__(self, access_key: str, secret_key: str):
        self.access_key = access_key
        self.secret_key = secret_key

    def _auth(self, params: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {make_token(self.access_key, self.secret_key, params)}"}

    def _private_get(self, path: str, params: dict | None = None):
        return _get(path, params or {}, headers=self._auth(params))

    def accounts(self) -> list[dict]:
        """보유 자산 목록."""
        return self._private_get("/accounts")

    def balance(self, currency: str) -> float:
        """주문 가능한 잔고 (주문에 묶인 금액 제외)."""
        for a in self.accounts():
            if a["currency"] == currency:
                return float(a["balance"])
        return 0.0

    def get_order(self, order_uuid: str) -> dict:
        return self._private_get("/order", {"uuid": order_uuid})

    def place_order(self, params: dict) -> dict:
        """주문. 같은 주문이 두 번 들어가지 않도록 절대 자동 재시도하지 않는다."""
        request = urllib.request.Request(
            f"{BASE_URL}/orders", method="POST", data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                     "User-Agent": USER_AGENT, **self._auth(params)},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise UpbitError(f"주문 거부 (HTTP {e.code}) {_read_error(e)}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UpbitError(f"주문 요청 중 통신 오류 ({getattr(e, 'reason', e)}) · 주문이 들어갔는지 업비트에서 확인하세요") from e

    def wait_order(self, order_uuid: str, timeout: float = 15) -> dict:
        """주문이 체결 완료(done) 또는 종료(cancel)될 때까지 기다린다. 시장가 주문은 보통 바로 끝난다."""
        deadline = time.monotonic() + timeout
        while True:
            order = self.get_order(order_uuid)
            if order.get("state") in ("done", "cancel"):
                return order
            if time.monotonic() > deadline:
                raise UpbitError(f"주문 {order_uuid}이(가) {timeout:.0f}초 안에 끝나지 않았어요 (상태: {order.get('state')})")
            time.sleep(0.5)
