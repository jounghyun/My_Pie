"""주식 주문 실행.

  - PaperStockExecutor: 모의투자. 지금 가격에 바로 체결됐다고 보고 수수료·세금만 뗀다.
  - LiveStockExecutor: 토스증권 실거래. 주문마다 우리가 만든 번호(client_id)를 붙이고, 주문 직전에
    '진행 중인 주문'으로 DB에 적어 둔다. 정전·통신 끊김으로 결과를 모르게 되면 recover()가
    토스증권에 물어서 체결됐으면 장부에 반영하게 하고, 안 들어갔으면 지운다.

실거래 주문은 시장가 대신 '지금 가격 ±1%' 지정가로 낸다. 바로 체결되고(가장 좋은 호가에 체결), 국내 시장가 매수가
상한가 기준으로 돈을 묶어서 살 수 있는 수량이 줄어드는 문제도 없다. 정해진 시간 안에 다 체결되지 않으면
남은 수량은 취소하고 체결된 만큼만 장부에 적는다.
"""

import time
import uuid

from . import krx, toss
from .db import Database
from .timeutil import ts

SLIPPAGE = 0.01       # 지정가를 지금 가격에서 얼마나 불리하게 잡을지 (바로 체결되게)
FILL_WAIT_SEC = 20    # 이 시간 안에 다 안 되면 남은 수량을 취소한다
FINAL = ("filled", "cancelled", "rejected")


class PaperStockExecutor:
    mode = "paper"

    def __init__(self, fee_rate: float, sell_tax: float):
        self.fee_rate, self.sell_tax = fee_rate, sell_tax

    def buy(self, symbol: str, cash: float, price: float, note: dict) -> tuple[int, float, float] | None:
        """(주수, 체결가, 수수료). 1주도 못 사면 None."""
        qty = int(cash // (price * (1 + self.fee_rate)))
        if qty < 1:
            return None
        return qty, price, qty * price * self.fee_rate

    def sell(self, symbol: str, qty: int, price: float, note: dict) -> tuple[int, float, float, float]:
        """(주수, 체결가, 수수료, 세금)."""
        value = qty * price
        return qty, price, value * self.fee_rate, value * self.sell_tax

    def recover(self):
        return None

    def clear_pending(self) -> None:
        pass


class LiveStockExecutor:
    mode = "live"
    PENDING_KEY = "stock_pending_order"

    def __init__(self, db: Database, client, fee_rate: float, sell_tax: float,
                 wait_sec: float = FILL_WAIT_SEC, sleep=time.sleep):
        self.db, self.client = db, client
        self.fee_rate, self.sell_tax = fee_rate, sell_tax
        self.wait_sec, self.sleep = wait_sec, sleep

    def buy(self, symbol: str, cash: float, price: float, note: dict) -> tuple[int, float, float] | None:
        limit = krx.round_tick(price * (1 + SLIPPAGE), up=True)
        qty = int(cash // (limit * (1 + self.fee_rate)))
        if qty < 1:
            return None
        fill = self._place(symbol, "buy", qty, limit, note)
        return fill["qty"], fill["price"], fill["fee"]

    def sell(self, symbol: str, qty: int, price: float, note: dict) -> tuple[int, float, float, float]:
        limit = max(krx.round_tick(price * (1 - SLIPPAGE), up=False), 1)
        fill = self._place(symbol, "sell", qty, limit, note)
        return fill["qty"], fill["price"], fill["fee"], fill["tax"]

    def _place(self, symbol: str, side: str, qty: int, limit: int, note: dict) -> dict:
        pending = {"client_id": "mypie-" + uuid.uuid4().hex[:20], "symbol": symbol, "side": side, "qty": qty,
                   "price": limit, "at": ts(), **note}
        self.db.set_state(self.PENDING_KEY, pending)  # 주문 직전에 적어 둔다 (정전 대비)
        try:
            order_id = self.client.place_order(symbol, side, qty, limit, pending["client_id"])
        except toss.OrderRejected:
            self.db.delete_state(self.PENDING_KEY)  # 확실히 거절됐으면 진행 중인 주문이 아니다
            raise
        except toss.TossError as e:
            raise toss.OrderUnknown(f"주문 결과를 확인하지 못했어요: {e}") from e
        pending["order_id"] = order_id
        self.db.set_state(self.PENDING_KEY, pending)
        return self._fill(self._settle(order_id), side)

    def _settle(self, order_id: str) -> dict:
        """다 체결되거나 끝날 때까지 기다린다. 시간이 지나면 남은 수량을 취소하고 최종 결과를 돌려준다."""
        deadline = time.monotonic() + self.wait_sec
        try:
            while True:
                order = self.client.order(order_id)
                if order["status"] in FINAL:
                    return order
                if time.monotonic() >= deadline:
                    break
                self.sleep(1)
            self.client.cancel(order_id)
            for _ in range(10):
                order = self.client.order(order_id)
                if order["status"] in FINAL:
                    return order
                self.sleep(1)
        except toss.TossError as e:
            raise toss.OrderUnknown(f"주문 체결을 확인하지 못했어요: {e}") from e
        raise toss.OrderUnknown("주문 취소를 확인하지 못했어요")

    def _fill(self, order: dict, side: str) -> dict:
        qty = int(order.get("filled_qty") or 0)
        price = float(order.get("avg_price") or 0)
        value = qty * price
        fee = order.get("fee")
        tax = order.get("tax")
        return {"qty": qty, "price": price,
                "fee": float(fee) if fee is not None else value * self.fee_rate,
                "tax": float(tax) if tax is not None else (value * self.sell_tax if side == "sell" else 0.0)}

    def pending(self) -> dict | None:
        return self.db.get_state(self.PENDING_KEY)

    def clear_pending(self) -> None:
        self.db.delete_state(self.PENDING_KEY)

    def recover(self) -> dict | None:
        """끊기기 전 주문이 있으면 {"pending": 그 주문, "fill": 체결 결과 또는 None(안 들어감·체결 0)}."""
        pending = self.pending()
        if not pending:
            return None
        try:
            if pending.get("order_id"):
                order = self.client.order(pending["order_id"])
            else:
                order = self.client.find_order(pending["client_id"], pending["symbol"])
            if order is not None and order["status"] not in FINAL:
                order = self._settle(order["id"])
        except toss.TossError as e:
            raise toss.OrderUnknown(f"끊기기 전 주문을 확인하지 못했어요: {e}") from e
        fill = self._fill(order, pending["side"]) if order else None
        return {"pending": pending, "fill": fill if fill and fill["qty"] else None}
