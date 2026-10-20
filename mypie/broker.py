"""주문 실행. 모의투자(PaperBroker)와 실거래(LiveBroker)가 같은 모양(account/buy/sell/recover)을 가진다.

두 브로커 모두 '봇 전용 장부'를 따로 관리한다:
  - 모의투자: 가상 원화로 시작
  - 실거래: 투자 한도(예: 10만 원)로 시작해서 번 돈·잃은 돈이 장부에 그대로 쌓이고, 봇이 산 코인만 판다
    → 업비트 계좌에 원래 있던 다른 코인은 건드리지 않는다
"""

import math
import uuid

from .db import Database
from .timeutil import ts
from .upbit import OrderNotPlaced, OrderRejected, OrderUnknown, UpbitClient, UpbitError

MIN_ORDER_KRW = 5000  # 업비트 최소 주문 금액
FULL_MARGIN = 0.005   # 전액 모드에서 살 때 남겨 두는 여유분 (0.5%). 수수료는 따로 뺀다
FLOW_THRESHOLD = 100  # 이보다 작은 장부·실제 잔고 차이는 반올림 오차로 본다 (원)


class PaperBroker:
    """가상 계좌. 실제 주문 없이 현재가에 바로 체결됐다고 가정하고 수수료만 뗀다."""

    mode = "paper"
    full = False
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

    def recover(self) -> None:
        return None  # 모의투자는 주문이 끊길 일이 없다

    def sync(self, price: float) -> None:
        return None  # 모의투자는 맞출 실제 계좌가 없다

    def buy(self, price: float, krw_amount: float, reason: str = "", day: str | None = None) -> dict:
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
    """업비트 실거래. 시장가로 주문하고, 실제 체결 내역으로 봇 전용 장부를 갱신한다.

    한도 모드 (budget = 숫자): 그 금액으로 시작하는 봇 전용 장부. 봇이 산 코인만 판다.
    전액 모드 (budget = "all"): 업비트 계좌의 원화와 그 종목 코인 전체를 봇이 운용한다.
        매 루프마다 실제 잔고로 장부를 맞추고(sync), 원래 있던 코인도 보유분으로 본다.
        살 때는 그때의 원화 잔고 전체(여유분 0.5% 제외)를 쓴다.
        입금·출금은 '넣은 돈'(initial_krw)에 더하고 빼서 수익률이 틀어지지 않게 한다.

    주문마다 우리가 만든 번호(identifier)를 붙이고, 주문 직전에 '진행 중인 주문'으로 DB에 적어 둔다.
    정전·통신 끊김으로 결과를 모르게 되면, 다음에 켜졌을 때 recover()가 그 번호로 업비트에 물어봐서
    체결됐으면 장부에 반영하고 안 들어갔으면 지운다. 사람이 확인할 필요 없이 바로 이어서 매매한다.
    """

    mode = "live"
    STATE_KEY = "live_account"
    PENDING_KEY = "live_pending_order"

    def __init__(self, db: Database, market: str, budget, fee_rate: float, client: UpbitClient):
        self.db = db
        self.market = market
        self.currency = market.split("-")[-1]
        self.fee_rate = float(fee_rate)
        self.client = client
        self.full = budget == "all"
        budget = "all" if self.full else float(budget)
        self.acct = db.get_state(self.STATE_KEY)
        if self.acct is None or self.acct.get("market") != market or self.acct.get("budget") != budget:
            if self.acct and self.acct.get("coin") and not self.acct.get("budget") == "all":
                raise UpbitError("실거래 장부에 아직 팔지 않은 코인이 있어서 한도/종목을 바꿀 수 없어요")
            start = 0.0 if self.full else budget
            self.acct = {"market": market, "budget": budget, "krw": start, "coin": 0.0,
                         "avg_price": 0.0, "cost": 0.0, "initial_krw": start, "needs_sync": self.full}
            self._save()

    def sync(self, price: float) -> float | None:
        """전액 모드: 실제 잔고로 장부를 맞춘다. 설명 안 되는 차이(입금·출금 등)를 돌려준다 (원)."""
        if not self.full or self.pending():
            return None
        try:
            accounts = {a["currency"]: a for a in self.client.accounts()}
        except UpbitError as e:
            raise OrderNotPlaced(f"잔고 확인 실패 ({e})", status=e.status, name=e.name) from e
        krw = float(accounts.get("KRW", {}).get("balance", 0))
        entry = accounts.get(self.currency, {})
        coin = float(entry.get("balance", 0))
        avg = float(entry.get("avg_buy_price", 0) or 0) if coin else 0.0
        if self.acct.get("needs_sync"):
            flow = 0.0
            self.acct.update(needs_sync=False, initial_krw=krw + coin * price)
        else:
            flow = (krw - self.acct["krw"]) + (coin - self.acct["coin"]) * price
            if abs(flow) >= FLOW_THRESHOLD:
                self.acct["initial_krw"] += flow
            else:
                flow = 0.0
        self.acct.update(krw=krw, coin=coin, avg_price=avg, cost=coin * avg)
        self._save()
        return flow

    def _save(self) -> None:
        self.db.set_state(self.STATE_KEY, self.acct)

    def account(self) -> dict:
        return dict(self.acct)

    def pending(self) -> dict | None:
        return self.db.get_state(self.PENDING_KEY)

    def _balance(self, currency: str) -> float:
        try:
            return self.client.balance(currency)
        except UpbitError as e:
            raise OrderNotPlaced(f"잔고 조회 실패 ({e})", status=e.status, name=e.name) from e

    def _place(self, params: dict, note: dict) -> dict:
        """주문을 내고 체결이 끝날 때까지 기다린다. 결과를 모르게 되면 OrderUnknown."""
        identifier = f"mypie-{uuid.uuid4()}"
        self.db.set_state(self.PENDING_KEY, {**note, "identifier": identifier, "side": params["side"],
                                             "market": self.market, "at": ts()})
        try:
            placed = self.client.place_order({**params, "identifier": identifier})
        except OrderRejected:
            self.db.delete_state(self.PENDING_KEY)  # 거절됐으니 아무것도 안 바뀌었다
            raise
        try:
            return self.client.wait_order(placed["uuid"])
        except UpbitError as e:
            raise OrderUnknown(f"주문은 들어갔지만 체결 확인 실패 ({e})", status=e.status, name=e.name) from e

    def buy(self, price: float, krw_amount: float, reason: str = "", day: str | None = None) -> dict:
        """krw_amount원(수수료 포함) 안에서 시장가 매수. 전액 모드는 원화 잔고에서 여유분을 남긴다."""
        available = self._balance("KRW") * ((1 - FULL_MARGIN) if self.full else 1)
        krw_amount = min(krw_amount, self.acct["krw"], available)
        order_krw = math.floor(krw_amount / (1 + self.fee_rate))  # 업비트는 수수료를 주문 금액에 더해서 뗀다
        if order_krw < MIN_ORDER_KRW:
            raise OrderRejected(f"주문 금액 {order_krw:,}원이 최소 주문 금액보다 적어요 (업비트 원화 잔고를 확인하세요)")
        order = self._place({"market": self.market, "side": "bid", "ord_type": "price", "price": str(order_krw)},
                            {"reason": reason, "day": day})
        return self._apply(order, "bid", reason)

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
        order = self._place({"market": self.market, "side": "ask", "ord_type": "market", "volume": f"{volume:.8f}"},
                            {"reason": reason})
        return self._apply(order, "ask", reason)

    def recover(self) -> dict | None:
        """끊기기 전에 낸 주문이 있으면 업비트에 물어서 장부를 맞춘다.

        돌려주는 값: None (확인할 주문 없음) / {"found": False, "pending": ...} (주문이 안 들어갔음)
                    / {"found": True, "pending": ..., "trade": ...} (체결돼서 장부에 반영함)
        통신이 안 되면 OrderNotPlaced를 내서, 봇이 매매하지 않고 다음 루프에 다시 확인하게 한다.
        """
        pending = self.pending()
        if not pending:
            return None
        try:
            order = self.client.get_order(identifier=pending["identifier"])
            if order.get("state") not in ("done", "cancel"):
                order = self.client.wait_order(order["uuid"])
        except UpbitError as e:
            if e.status == 404 or e.name == "order_not_found":
                self.db.delete_state(self.PENDING_KEY)
                return {"found": False, "pending": pending}
            raise OrderNotPlaced(f"끊기기 전 주문을 확인하는 중 ({e})", status=e.status, name=e.name) from e
        try:
            trade = self._apply(order, pending["side"], pending.get("reason", ""))
        except OrderRejected:  # 들어갔지만 하나도 체결 안 되고 끝난 주문
            return {"found": False, "pending": pending}
        return {"found": True, "pending": pending, "trade": trade}

    def _apply(self, order: dict, side: str, reason: str) -> dict:
        """체결된 주문을 장부에 반영하고 '진행 중인 주문' 표시를 지운다."""
        volume, funds = _filled(order)
        if volume <= 0:
            self.db.delete_state(self.PENDING_KEY)
            raise OrderRejected(f"주문이 체결되지 않고 끝났어요 (상태: {order.get('state')})")
        fee = float(order.get("paid_fee") or 0)
        uuid_ = order.get("uuid")
        if side == "bid":
            spent = funds + fee
            coin = self.acct["coin"] + volume
            self.acct.update(
                krw=self.acct["krw"] - spent,
                coin=coin,
                avg_price=(self.acct["avg_price"] * self.acct["coin"] + funds) / coin,
                cost=self.acct["cost"] + spent,
            )
            trade = {"side": "buy", "price": funds / volume, "volume": volume, "krw": spent, "fee": fee,
                     "profit": None, "reason": reason, "uuid": uuid_}
        else:
            proceeds = funds - fee
            held = self.acct["coin"]
            sold_cost = self.acct["cost"] * min(volume / held, 1.0) if held else self.acct["cost"]
            remaining = max(held - volume, 0.0)
            keep = remaining * (funds / volume) >= 1  # 1원어치도 안 남으면 다 판 것으로 본다
            self.acct.update(krw=self.acct["krw"] + proceeds, coin=remaining if keep else 0.0,
                             cost=self.acct["cost"] - sold_cost if keep else 0.0)
            if not keep:
                self.acct["avg_price"] = 0.0
            trade = {"side": "sell", "price": funds / volume, "volume": volume, "krw": proceeds, "fee": fee,
                     "profit": proceeds - sold_cost, "cost": sold_cost, "reason": reason, "uuid": uuid_}
        self._save()
        self.db.delete_state(self.PENDING_KEY)
        return trade


def _filled(order: dict) -> tuple[float, float]:
    """체결된 (수량, 원화 금액). 체결 내역을 읽을 수 없으면 오류를 낸다 (장부를 엉터리로 고치지 않게)."""
    trades = order.get("trades") or []
    volume = sum(float(t["volume"]) for t in trades)
    funds = sum(float(t["funds"]) for t in trades)
    if float(order.get("executed_volume") or 0) > 0 and (volume <= 0 or funds <= 0):
        raise UpbitError(f"주문 {order.get('uuid')}의 체결 내역을 읽지 못했어요. 업비트에서 직접 확인하세요")
    return volume, funds
