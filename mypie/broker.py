"""장부와 주문 실행.

장부(Book): 포트폴리오 전체의 돈 관리.
  - 줄(slot)마다 자기 몫의 원화(krw)와 코인(coin)을 따로 가진다. 한 줄이 다른 줄의 돈을 쓰지 않는다.
  - 비중에 안 들어간 원화는 대기 원화(reserve)로 남는다.
  - contrib: 그 줄에 들어간 돈 합계 (줄별 수익률 계산용), contributions: 포트폴리오 전체에 넣은 돈 합계

주문 실행(Executor): 실제로 사고파는 부분.
  - PaperExecutor: 모의투자. 현재가에 바로 체결됐다고 가정하고 수수료만 뗀다.
  - LiveExecutor: 업비트 실거래. 주문마다 우리가 만든 번호(identifier)를 붙이고 주문 직전에
    '진행 중인 주문'으로 DB에 적어 둔다. 정전·통신 끊김으로 결과를 모르게 되면 recover()가
    그 번호로 업비트에 물어서 체결됐으면 장부에 반영하게 하고, 안 들어갔으면 지운다.

계좌 방식:
  - 모의투자: 가상 원화로 시작
  - 실거래 금액 한도: 그 금액으로 시작하는 봇 전용 장부. 봇이 산 코인만 판다
  - 실거래 계좌 전액: 업비트 계좌의 원화와 포트폴리오 종목 코인 전체를 봇이 굴린다.
    매 루프마다 실제 잔고로 장부를 맞추고(sync_full), 입금은 비중대로 나눠 담는다.
"""

import math
import uuid

from .buyhold import add_flow
from .db import Database
from .timeutil import ts
from .upbit import OrderNotPlaced, OrderRejected, OrderUnknown, UpbitClient, UpbitError

MIN_ORDER_KRW = 5000  # 업비트 최소 주문 금액
FULL_MARGIN = 0.005   # 계좌 전액 모드에서 살 때 남겨 두는 여유분 (0.5%). 수수료는 따로 뺀다
FLOW_THRESHOLD = 100  # 이보다 작은 장부·실제 잔고 차이는 반올림 오차로 본다 (원)


class Book:
    def __init__(self, db: Database, key: str, budget, start_krw: float):
        """budget이 바뀌면(예: 실거래 한도 변경) 새 장부로 시작한다. budget == "all"이면 계좌 전액 모드."""
        self.db = db
        self.key = key
        data = db.get_state(key)
        if data is None or data.get("budget") != budget:
            data = {"budget": budget, "reserve": float(start_krw), "slots": {},
                    "contributions": float(start_krw), "needs_sync": budget == "all", "needs_alloc": True}
        self.data = data
        self.save()

    @property
    def full(self) -> bool:
        return self.data["budget"] == "all"

    def save(self) -> None:
        self.db.set_state(self.key, self.data)

    def slot(self, slot_id: str, market: str | None = None) -> dict:
        s = self.data["slots"].get(slot_id)
        if s is None:
            s = self.data["slots"][slot_id] = {"market": market, "krw": 0.0, "coin": 0.0, "avg_price": 0.0,
                                               "cost": 0.0, "contrib": 0.0, "hold": 0.0, "hold_cash": 0.0}
        return s

    def cash(self) -> float:
        return self.data["reserve"] + sum(s["krw"] for s in self.data["slots"].values())

    def equity(self, prices: dict) -> float:
        return self.cash() + sum(s["coin"] * prices.get(s["market"], s["avg_price"])
                                 for s in self.data["slots"].values())

    def remove_slot(self, slot_id: str) -> None:
        """줄을 뺀다. 남은 원화는 대기 원화로 (코인은 미리 팔아 둬야 한다)."""
        s = self.data["slots"].pop(slot_id, None)
        if s:
            self.data["reserve"] += s["krw"]

    def allocate(self, weights: dict, prices: dict) -> None:
        """원화를 비중대로 다시 나눈다. 각 줄은 (전체 자산 × 비중 − 보유 코인 가치)만큼 원화를 받는다."""
        slots = self.data["slots"]
        cash = self.cash()
        values = {sid: s["coin"] * prices.get(s["market"], s["avg_price"]) for sid, s in slots.items()}
        equity = cash + sum(values.values())
        need = {sid: max(0.0, equity * weights.get(sid, 0) / 100 - values[sid]) for sid in slots}
        total_need = sum(need.values())
        scale = min(1.0, cash / total_need) if total_need > 0 else 0.0
        for sid, s in slots.items():
            new_krw = need[sid] * scale
            add_flow(s, new_krw - s["krw"])
            s["krw"] = new_krw
        self.data["reserve"] = cash - sum(s["krw"] for s in slots.values())
        self.data["needs_alloc"] = False
        self.save()

    def apply_buy(self, slot_id: str, volume: float, funds: float, fee: float, reason: str) -> dict:
        s = self.data["slots"][slot_id]
        spent = funds + fee
        coin = s["coin"] + volume
        s.update(krw=s["krw"] - spent, coin=coin, cost=s["cost"] + spent,
                 avg_price=(s["avg_price"] * s["coin"] + funds) / coin if coin else 0.0)
        if s["krw"] < 0:  # 수수료 반올림 등으로 조금 넘친 건 대기 원화에서 채운다
            self.data["reserve"] += s["krw"]
            s["krw"] = 0.0
        self.save()
        return {"side": "buy", "price": funds / volume, "volume": volume, "krw": spent, "fee": fee,
                "profit": None, "reason": reason}

    def apply_sell(self, slot_id: str, volume: float, funds: float, fee: float, reason: str) -> dict:
        s = self.data["slots"][slot_id]
        proceeds = funds - fee
        held = s["coin"]
        sold_cost = s["cost"] * min(volume / held, 1.0) if held else s["cost"]
        remaining = max(held - volume, 0.0)
        keep = remaining * (funds / volume) >= 1  # 1원어치도 안 남으면 다 판 것으로 본다
        s.update(krw=s["krw"] + proceeds, coin=remaining if keep else 0.0,
                 cost=s["cost"] - sold_cost if keep else 0.0)
        if not keep:
            s["avg_price"] = 0.0
        self.save()
        return {"side": "sell", "price": funds / volume, "volume": volume, "krw": proceeds, "fee": fee,
                "profit": proceeds - sold_cost, "cost": sold_cost, "reason": reason}

    def release(self, slot_id: str) -> None:
        """그 줄의 원화를 대기 원화로 옮긴다 (비중 0% 줄이 코인을 팔았을 때)."""
        s = self.data["slots"][slot_id]
        self.data["reserve"] += s["krw"]
        add_flow(s, -s["krw"])
        s["krw"] = 0.0
        self.save()

    def clear_dust(self, slot_id: str) -> None:
        """너무 적어서 팔 수 없는 자투리 코인은 장부에서만 정리한다."""
        s = self.data["slots"][slot_id]
        s.update(coin=0.0, cost=0.0, avg_price=0.0)
        self.save()


def sync_full(book: Book, accounts: list[dict], slot_markets: dict, weights: dict, prices: dict) -> float:
    """계좌 전액 모드: 실제 잔고로 장부를 맞춘다. 설명 안 되는 차이(입금·출금·직접 매매)를 돌려준다 (원).

    - 원화가 늘면(입금) 비중대로 각 줄에 나누고 나머지는 대기 원화로
    - 원화가 줄면(출금) 대기 원화에서 먼저, 모자라면 각 줄의 원화에서 나눠서 뺀다
    - 코인 수량은 실제 잔고 그대로 (원래 있던 코인도 그 종목 줄의 보유분)
    """
    acc = {a["currency"]: a for a in accounts}
    krw = float(acc.get("KRW", {}).get("balance", 0))
    data = book.data
    for sid, market in slot_markets.items():
        book.slot(sid, market)["market"] = market

    if data.get("needs_sync"):
        data["reserve"] = krw
        for sid, market in slot_markets.items():
            s = data["slots"][sid]
            entry = acc.get(market.split("-")[-1], {})
            coin = float(entry.get("balance", 0))
            # 원래 있던 코인은 넘겨받은 순간의 시세로 장부에 올린다. 업비트 평균 매수가로 올리면
            # 봇이 오기 전에 오른 만큼까지 봇의 실현 손익으로 잡힌다
            price = (prices.get(market) or float(entry.get("avg_buy_price") or 0)) if coin else 0.0
            s.update(krw=0.0, coin=coin, avg_price=price, cost=coin * price, contrib=coin * price,
                     hold=coin, hold_cash=0.0)  # 바이앤홀드였다면 원래 있던 코인을 그대로 들고 있다
        data["contributions"] = krw + sum(s["coin"] * prices.get(s["market"], s["avg_price"]) for s in data["slots"].values())
        data.update(needs_sync=False, needs_alloc=True)
        book.save()
        return 0.0

    flows = 0.0
    diff = krw - book.cash()
    if diff >= FLOW_THRESHOLD:  # 입금
        for sid, s in data["slots"].items():
            share = diff * weights.get(sid, 0) / 100
            s["krw"] += share
            add_flow(s, share)
        data["reserve"] += diff * (100 - sum(weights.get(sid, 0) for sid in data["slots"])) / 100
        flows += diff
    elif diff <= -FLOW_THRESHOLD:  # 출금
        take = min(-diff, max(data["reserve"], 0.0))
        data["reserve"] -= take
        rest = -diff - take
        slot_cash = sum(s["krw"] for s in data["slots"].values())
        for s in data["slots"].values():
            cut = rest * s["krw"] / slot_cash if slot_cash > 0 else 0.0
            s["krw"] -= cut
            add_flow(s, -cut)
        flows += diff
    else:
        data["reserve"] += diff  # 반올림 오차는 대기 원화로

    for sid, market in slot_markets.items():
        s = data["slots"][sid]
        coin = float(acc.get(market.split("-")[-1], {}).get("balance", 0))
        price = prices.get(market) or s["avg_price"]
        delta = (coin - s["coin"]) * price
        if abs(delta) >= FLOW_THRESHOLD:
            add_flow(s, delta)
            flows += delta
        # 원가는 봇 장부를 그대로 쓴다. 밖에서 들어온 코인은 지금 시세로 더하고, 나간 코인은 그만큼 덜어 낸다
        if coin <= 0:
            s.update(coin=0.0, cost=0.0, avg_price=0.0)
        elif coin > s["coin"]:
            added = coin - s["coin"]
            s.update(cost=s["cost"] + added * price, avg_price=(s["avg_price"] * s["coin"] + added * price) / coin, coin=coin)
        else:
            s.update(cost=s["cost"] * coin / s["coin"], coin=coin)
    data["contributions"] += flows
    book.save()
    return flows


class PaperExecutor:
    """모의투자. 실제 주문 없이 현재가에 바로 체결됐다고 가정하고 수수료만 뗀다."""

    mode = "paper"
    client = None

    def __init__(self, fee_rate: float):
        self.fee_rate = float(fee_rate)

    def buy(self, market: str, krw_amount: float, price: float, note: dict) -> tuple[float, float, float]:
        """krw_amount원(수수료 포함)어치 매수 → (수량, 체결 금액, 수수료)."""
        fee = krw_amount * self.fee_rate / (1 + self.fee_rate)
        funds = krw_amount - fee
        return funds / price, funds, fee

    def sell(self, market: str, volume: float, price: float, note: dict) -> tuple[float, float, float] | None:
        funds = volume * price
        return volume, funds, funds * self.fee_rate

    def recover(self) -> None:
        return None  # 모의투자는 주문이 끊길 일이 없다

    def pending(self) -> None:
        return None

    def clear_pending(self) -> None:
        pass


class LiveExecutor:
    """업비트 실거래 주문."""

    mode = "live"
    PENDING_KEY = "live_pending_order"

    def __init__(self, db: Database, client: UpbitClient, fee_rate: float, full: bool):
        self.db = db
        self.client = client
        self.fee_rate = float(fee_rate)
        self.full = full

    def pending(self) -> dict | None:
        return self.db.get_state(self.PENDING_KEY)

    def clear_pending(self) -> None:
        self.db.delete_state(self.PENDING_KEY)

    def _balance(self, currency: str) -> float:
        try:
            return self.client.balance(currency)
        except UpbitError as e:
            raise OrderNotPlaced(f"잔고 조회 실패 ({e})", status=e.status, name=e.name) from e

    def _place(self, params: dict, note: dict) -> dict:
        """주문을 내고 체결이 끝날 때까지 기다린다. 결과를 모르게 되면 OrderUnknown (표시는 남겨 둔다)."""
        identifier = f"mypie-{uuid.uuid4()}"
        self.db.set_state(self.PENDING_KEY, {**note, "identifier": identifier, "side": params["side"],
                                             "market": params["market"], "at": ts()})
        try:
            placed = self.client.place_order({**params, "identifier": identifier})
        except OrderRejected:
            self.clear_pending()  # 거절됐으니 아무것도 안 바뀌었다
            raise
        try:
            return self.client.wait_order(placed["uuid"])
        except UpbitError as e:
            raise OrderUnknown(f"주문은 들어갔지만 체결 확인 실패 ({e})", status=e.status, name=e.name) from e

    def buy(self, market: str, krw_amount: float, price: float, note: dict) -> tuple[float, float, float]:
        """krw_amount원(수수료 포함) 안에서 시장가 매수 → (수량, 체결 금액, 수수료).
        성공해도 '진행 중인 주문' 표시는 남겨 두고, 장부에 반영한 뒤 clear_pending()으로 지운다."""
        available = self._balance("KRW") * ((1 - FULL_MARGIN) if self.full else 1)
        order_krw = math.floor(min(krw_amount, available) / (1 + self.fee_rate))  # 수수료는 주문 금액에 더해서 뗀다
        if order_krw < MIN_ORDER_KRW:
            raise OrderRejected(f"주문 금액 {order_krw:,}원이 최소 주문 금액보다 적어요 (업비트 원화 잔고를 확인하세요)")
        order = self._place({"market": market, "side": "bid", "ord_type": "price", "price": str(order_krw)}, note)
        return self._filled_or_reject(order)

    def sell(self, market: str, volume: float, price: float, note: dict) -> tuple[float, float, float] | None:
        """시장가 매도. 팔 수 없을 만큼 적으면(최소 주문 금액 미만) None."""
        volume = math.floor(min(volume, self._balance(market.split("-")[-1])) * 1e8) / 1e8  # 소수점 8자리까지
        if volume * price < MIN_ORDER_KRW:
            return None
        order = self._place({"market": market, "side": "ask", "ord_type": "market", "volume": f"{volume:.8f}"}, note)
        return self._filled_or_reject(order)

    def _filled_or_reject(self, order: dict) -> tuple[float, float, float]:
        volume, funds = _filled(order)
        if volume <= 0:
            self.clear_pending()
            raise OrderRejected(f"주문이 체결되지 않고 끝났어요 (상태: {order.get('state')})")
        return volume, funds, float(order.get("paid_fee") or 0)

    def recover(self) -> dict | None:
        """끊기기 전에 낸 주문이 있으면 업비트에 결과를 묻는다.

        돌려주는 값: None (확인할 주문 없음)
                    {"pending": ..., "fill": None} (주문이 안 들어갔음 → 표시를 지움)
                    {"pending": ..., "fill": (수량, 금액, 수수료)} (체결됨 → 봇이 장부에 반영하고 clear_pending)
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
                self.clear_pending()
                return {"pending": pending, "fill": None}
            raise OrderNotPlaced(f"끊기기 전 주문을 확인하는 중 ({e})", status=e.status, name=e.name) from e
        volume, funds = _filled(order)
        if volume <= 0:
            self.clear_pending()
            return {"pending": pending, "fill": None}
        return {"pending": pending, "fill": (volume, funds, float(order.get("paid_fee") or 0))}


def _filled(order: dict) -> tuple[float, float]:
    """체결된 (수량, 원화 금액). 체결 내역을 읽을 수 없으면 오류를 낸다 (장부를 엉터리로 고치지 않게)."""
    trades = order.get("trades") or []
    volume = sum(float(t["volume"]) for t in trades)
    funds = sum(float(t["funds"]) for t in trades)
    if float(order.get("executed_volume") or 0) > 0 and (volume <= 0 or funds <= 0):
        raise UpbitError(f"주문 {order.get('uuid')}의 체결 내역을 읽지 못했어요. 업비트에서 직접 확인하세요")
    return volume, funds
