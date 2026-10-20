"""주문 실행. 모의투자(PaperBroker)와 실거래(LiveBroker)가 같은 모양(account/buy/sell)을 가진다.

두 브로커 모두 '봇 전용 장부'를 따로 관리한다:
  - 모의투자: 가상 원화로 시작
  - 실거래: 투자 한도(예: 10만 원)만큼만 쓰고, 봇이 산 코인만 판다
    → 업비트 계좌에 원래 있던 다른 돈이나 코인은 건드리지 않는다
"""

import math

from .db import Database
from .upbit import OrderNotPlaced, UpbitClient, UpbitError

MIN_ORDER_KRW = 5000  # 업비트 최소 주문 금액


class PaperBroker:
    """가상 계좌. 실제 주문 없이 현재가에 바로 체결됐다고 가정하고 수수료만 뗀다."""

    mode = "paper"
    STATE_KEY = "paper_account"

    def __init__(self, db: Database, market: str, initial_krw: float, fee_rate: float):
        self.db = db
        self.market = market
        self.fee_rate = float(fee_rate)
        self.acct = db.get_state(self.STATE_KEY)
        if self.acct is None:
            self.acct = {
                "krw": float(initial_krw),   # 원화 잔고
                "coin": 0.0,                 # 코인 수량
                "avg_price": 0.0,            # 평균 매수가
                "cost": 0.0,                 # 매수에 쓴 원화 (수수료 포함)
                "initial_krw": float(initial_krw),
            }
            self._save()

    def _save(self) -> None:
        self.db.set_state(self.STATE_KEY, self.acct)

    def account(self) -> dict:
        return dict(self.acct)

    def buy(self, price: float, krw_amount: float, reason: str = "") -> dict:
        """krw_amount원어치 시장가 매수. 수수료는 이 금액 안에서 뗀다."""
        krw_amount = min(krw_amount, self.acct["krw"])
        fee = krw_amount * self.fee_rate / (1 + self.fee_rate)
        volume = (krw_amount - fee) / price
        coin = self.acct["coin"] + volume
        self.acct.update(
            krw=self.acct["krw"] - krw_amount,
            coin=coin,
            avg_price=(self.acct["avg_price"] * self.acct["coin"] + price * volume) / coin,
            cost=self.acct["cost"] + krw_amount,
        )
        self._save()
        return {"side": "buy", "price": price, "volume": volume, "krw": krw_amount, "fee": fee,
                "profit": None, "reason": reason}

    def sell(self, price: float, reason: str = "") -> dict:
        """보유 코인 전량 시장가 매도."""
        volume = self.acct["coin"]
        gross = volume * price
        fee = gross * self.fee_rate
        proceeds = gross - fee
        cost = self.acct["cost"]
        self.acct.update(krw=self.acct["krw"] + proceeds, coin=0.0, avg_price=0.0, cost=0.0)
        self._save()
        return {"side": "sell", "price": price, "volume": volume, "krw": proceeds, "fee": fee,
                "profit": proceeds - cost, "cost": cost, "reason": reason}


class LiveBroker:
    """업비트 실거래. 시장가로 주문하고, 실제 체결 내역으로 봇 전용 장부를 갱신한다."""

    mode = "live"
    STATE_KEY = "live_account"

    def __init__(self, db: Database, market: str, budget: float, fee_rate: float, client: UpbitClient):
        self.db = db
        self.market = market
        self.currency = market.split("-")[-1]
        self.fee_rate = float(fee_rate)
        self.client = client
        self.acct = db.get_state(self.STATE_KEY)
        if self.acct is None or self.acct.get("market") != market or self.acct.get("budget") != budget:
            if self.acct and self.acct.get("coin"):
                raise UpbitError("실거래 장부에 아직 팔지 않은 코인이 있어서 한도/종목을 바꿀 수 없어요")
            self.acct = {"market": market, "budget": float(budget), "krw": float(budget), "coin": 0.0,
                         "avg_price": 0.0, "cost": 0.0, "initial_krw": float(budget)}
            self._save()

    def _save(self) -> None:
        self.db.set_state(self.STATE_KEY, self.acct)

    def account(self) -> dict:
        return dict(self.acct)

    def _balance(self, currency: str) -> float:
        try:
            return self.client.balance(currency)
        except UpbitError as e:
            raise OrderNotPlaced(f"잔고 조회 실패 ({e})") from e

    def buy(self, price: float, krw_amount: float, reason: str = "") -> dict:
        """krw_amount원(수수료 포함) 안에서 시장가 매수."""
        krw_amount = min(krw_amount, self.acct["krw"], self._balance("KRW"))
        order_krw = math.floor(krw_amount / (1 + self.fee_rate))  # 업비트는 수수료를 주문 금액에 더해서 뗀다
        if order_krw < MIN_ORDER_KRW:
            raise UpbitError(f"주문 금액 {order_krw:,}원이 최소 주문 금액보다 적어요 (업비트 원화 잔고를 확인하세요)")
        placed = self.client.place_order({"market": self.market, "side": "bid", "ord_type": "price",
                                          "price": str(order_krw)})
        order = self.client.wait_order(placed["uuid"])
        volume, funds = _filled(order)
        if volume <= 0:
            raise UpbitError(f"매수 주문이 체결되지 않았어요 (상태: {order.get('state')})")
        fee = float(order.get("paid_fee") or 0)
        spent = funds + fee
        coin = self.acct["coin"] + volume
        self.acct.update(
            krw=self.acct["krw"] - spent,
            coin=coin,
            avg_price=(self.acct["avg_price"] * self.acct["coin"] + funds) / coin,
            cost=self.acct["cost"] + spent,
        )
        self._save()
        return {"side": "buy", "price": funds / volume, "volume": volume, "krw": spent, "fee": fee,
                "profit": None, "reason": reason, "uuid": placed["uuid"]}

    def sell(self, price: float, reason: str = "") -> dict:
        """봇이 산 코인만 시장가 매도 (업비트에 원래 있던 코인은 건드리지 않음)."""
        volume = min(self.acct["coin"], self._balance(self.currency))
        volume = math.floor(volume * 1e8) / 1e8  # 업비트는 소수점 8자리까지
        cost = self.acct["cost"]
        if volume * price < MIN_ORDER_KRW:
            # 너무 적어서 팔 수 없는 자투리. 장부에서만 정리한다.
            self.acct.update(coin=0.0, avg_price=0.0, cost=0.0)
            self._save()
            return {"side": "sell", "price": price, "volume": 0.0, "krw": 0.0, "fee": 0.0,
                    "profit": -cost, "cost": cost, "reason": f"{reason} (최소 주문 금액 미만 자투리 정리)"}
        placed = self.client.place_order({"market": self.market, "side": "ask", "ord_type": "market",
                                          "volume": f"{volume:.8f}"})
        order = self.client.wait_order(placed["uuid"])
        sold, funds = _filled(order)
        if sold <= 0:
            raise UpbitError(f"매도 주문이 체결되지 않았어요 (상태: {order.get('state')})")
        fee = float(order.get("paid_fee") or 0)
        proceeds = funds - fee
        share = min(sold / self.acct["coin"], 1.0) if self.acct["coin"] else 1.0
        sold_cost = cost * share
        remaining = max(self.acct["coin"] - sold, 0.0)
        self.acct.update(krw=self.acct["krw"] + proceeds, coin=remaining if remaining * price >= 1 else 0.0,
                         cost=cost - sold_cost if remaining * price >= 1 else 0.0)
        if not self.acct["coin"]:
            self.acct["avg_price"] = 0.0
        self._save()
        return {"side": "sell", "price": funds / sold, "volume": sold, "krw": proceeds, "fee": fee,
                "profit": proceeds - sold_cost, "cost": sold_cost, "reason": reason, "uuid": placed["uuid"]}


def _filled(order: dict) -> tuple[float, float]:
    """체결된 (수량, 원화 금액). 체결 내역을 읽을 수 없으면 오류를 내서 봇이 매매를 멈추게 한다."""
    trades = order.get("trades") or []
    volume = sum(float(t["volume"]) for t in trades)
    funds = sum(float(t["funds"]) for t in trades)
    if float(order.get("executed_volume") or 0) > 0 and (volume <= 0 or funds <= 0):
        raise UpbitError(f"주문 {order.get('uuid')}의 체결 내역을 읽지 못했어요. 업비트에서 직접 확인하세요")
    return volume, funds
