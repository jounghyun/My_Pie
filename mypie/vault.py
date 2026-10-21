"""주식 장부: 실제 증권 계좌 하나를 '금고'와 '종목 계좌' 여러 개로 나눠서 쓴다.

  - 금고(vault): 쉬는 돈. 계좌에 입금하면 금고로 들어오고, 출금하면 금고에서 빠진다.
  - 종목 계좌(slot): 종목 하나 + 전략 하나. 자기 잔액(cash)과 자기 주식(qty)만 가지고 사고판다.
      · 팔면 그 돈은 종목 계좌 잔액에 남는다 (금고로 가지 않는다)
      · 금고 ↔ 종목 계좌 이체는 사람이 직접 한다. 이체해도 바로 사지 않고, 전략이 매수 신호를 낼 때만 산다
  - 실거래에서는 계좌에 원래 있던 그 종목 주식도 종목 계좌로 가져온다 (가져올 때 시세를 원가로 잡는다).
  - contrib: 그 종목 계좌에 넣은 돈 합계 (이체·가져온 주식 − 빼낸 돈), 종목 계좌 수익률 계산용
    contributions: 봇이 굴리는 돈 전체에 넣은 돈 합계 (입금 − 출금), 전체 수익률 계산용
"""

from .buyhold import add_flow
from .db import Database

FLOW_THRESHOLD = 100  # 이보다 작은 장부·실제 잔고 차이는 반올림 오차로 본다 (원)


class VaultError(ValueError):
    """사람이 요청한 이체·삭제가 안 될 때. 메시지는 화면에 그대로 보여준다."""


class VaultBook:
    def __init__(self, db: Database, key: str, start_cash: float):
        self.db = db
        self.key = key
        self.data = db.get_state(key) or {"vault": float(start_cash), "slots": {},
                                         "contributions": float(start_cash), "synced": False}
        self.save()

    def save(self) -> None:
        self.db.set_state(self.key, self.data)

    @property
    def vault(self) -> float:
        return self.data["vault"]

    def slot(self, sid: str) -> dict:
        return self.data["slots"][sid]

    def has(self, sid: str) -> bool:
        return sid in self.data["slots"]

    def cash_total(self) -> float:
        return self.data["vault"] + sum(s["cash"] for s in self.data["slots"].values())

    def equity(self, prices: dict) -> float:
        return self.cash_total() + sum(s["qty"] * prices.get(s["symbol"], s["avg_price"])
                                       for s in self.data["slots"].values())

    # ---- 종목 계좌 만들기·지우기·이체 ----

    def add_slot(self, sid: str, symbol: str, name: str, strategy: str) -> dict:
        if any(s["symbol"] == symbol for s in self.data["slots"].values()):
            raise VaultError("이미 그 종목 계좌가 있어요")
        s = self.data["slots"][sid] = {"symbol": symbol, "name": name, "strategy": strategy, "cash": 0.0, "qty": 0,
                                       "cost": 0.0, "avg_price": 0.0, "contrib": 0.0, "hold": 0.0, "hold_cash": 0.0}
        self.save()
        return s

    def set_strategy(self, sid: str, strategy: str) -> None:
        """전략만 바꾼다. 들고 있는 주식은 그대로 두고 새 전략이 판단한다."""
        self.slot(sid)["strategy"] = strategy
        self.save()

    def remove_slot(self, sid: str) -> float:
        """주식이 없을 때만 지운다. 남은 잔액은 금고로. 옮긴 금액을 돌려준다."""
        s = self.slot(sid)
        if s["qty"] > 0:
            raise VaultError(f"주식이 {s['qty']:,}주 남아 있어서 지울 수 없어요. 전부 판 뒤에 지워 주세요")
        self.data["vault"] += s["cash"]
        del self.data["slots"][sid]
        self.save()
        return s["cash"]

    def transfer(self, sid: str, amount: float) -> None:
        """amount > 0: 금고 → 종목 계좌, amount < 0: 종목 계좌 → 금고."""
        s = self.slot(sid)
        if amount > 0 and amount > self.data["vault"] + 0.5:
            raise VaultError(f"금고에 {self.data['vault']:,.0f}원밖에 없어요")
        if amount < 0 and -amount > s["cash"] + 0.5:
            raise VaultError(f"그 종목 계좌 잔액이 {s['cash']:,.0f}원밖에 없어요")
        amount = min(amount, self.data["vault"]) if amount > 0 else -min(-amount, s["cash"])
        self.data["vault"] -= amount
        s["cash"] += amount
        add_flow(s, amount)
        self.save()

    def deposit(self, amount: float) -> None:
        """금고에 입금(+)·출금(−). 모의투자의 가상 입출금과 실거래 잔고 맞추기에 쓴다."""
        if amount < 0 and -amount > self.data["vault"] + 0.5:
            raise VaultError(f"금고에 {self.data['vault']:,.0f}원밖에 없어요")
        self.data["vault"] = max(self.data["vault"] + amount, 0.0)
        self.data["contributions"] += amount
        self.save()

    # ---- 체결 반영 ----

    def apply_buy(self, sid: str, qty: int, price: float, fee: float, reason: str) -> dict:
        s = self.slot(sid)
        spent = qty * price + fee
        total = s["qty"] + qty
        s.update(cash=s["cash"] - spent, qty=total, cost=s["cost"] + spent,
                 avg_price=(s["avg_price"] * s["qty"] + qty * price) / total if total else 0.0)
        if s["cash"] < 0:  # 체결가가 예상보다 조금 높아 넘친 건 금고에서 채운다
            self.data["vault"] += s["cash"]
            add_flow(s, -s["cash"])
            s["cash"] = 0.0
        self.save()
        return {"side": "buy", "price": price, "volume": qty, "krw": spent, "fee": fee, "profit": None, "reason": reason}

    def apply_sell(self, sid: str, qty: int, price: float, fee: float, tax: float, reason: str) -> dict:
        s = self.slot(sid)
        qty = min(qty, s["qty"])
        proceeds = qty * price - fee - tax
        sold_cost = s["cost"] * qty / s["qty"] if s["qty"] else 0.0
        remaining = s["qty"] - qty
        s.update(cash=s["cash"] + proceeds, qty=remaining, cost=s["cost"] - sold_cost if remaining else 0.0)
        if not remaining:
            s["avg_price"] = 0.0
        self.save()
        return {"side": "sell", "price": price, "volume": qty, "krw": proceeds, "fee": fee + tax,
                "profit": proceeds - sold_cost, "cost": sold_cost, "reason": reason}

    # ---- 실거래: 실제 계좌와 맞추기 ----

    def sync(self, cash: float, holdings: dict, prices: dict) -> dict:
        """실제 주문가능 현금과 보유 주식으로 장부를 맞춘다. {"cash": 금고에 반영한 입출금, "shares": {종목: 바뀐 주수}}.

        - 종목 계좌의 종목은 계좌에 있는 주식 전부가 그 종목 계좌 것이다. 밖에서 늘거나 줄면(직접 사고팖, 원래 있던 주식)
          지금 시세로 원가와 넣은 돈에 반영한다
        - 현금 차이는 입금·출금으로 보고 금고에 반영한다. 출금이 금고보다 크면 종목 계좌 잔액에서 나눠서 뺀다
        """
        changes = {}
        for s in self.data["slots"].values():
            actual = int(holdings.get(s["symbol"], {}).get("qty", 0))
            delta = actual - s["qty"]
            if not delta:
                continue
            price = prices.get(s["symbol"]) or holdings.get(s["symbol"], {}).get("avg_price") or s["avg_price"]
            value = delta * price
            if delta > 0:
                s["cost"] += value
                s["avg_price"] = (s["avg_price"] * s["qty"] + value) / actual
            else:
                s["cost"] = s["cost"] * actual / s["qty"] if s["qty"] else 0.0
                if not actual:
                    s["avg_price"] = 0.0
            s["qty"] = actual
            add_flow(s, value)
            self.data["contributions"] += value
            changes[s["symbol"]] = delta

        flow = cash - self.cash_total()
        self.data["vault"] += flow  # 차이는 전부 금고에 (처음 맞출 때는 계좌 현금 전부, 그 뒤로는 입금·출금)
        if self.data.get("synced") and abs(flow) < FLOW_THRESHOLD:
            flow = 0.0  # 수수료 반올림 같은 작은 오차는 넣은 돈으로 치지 않는다
        else:
            self.data["contributions"] += flow
        if self.data["vault"] < 0:  # 금고보다 많이 출금됐으면 종목 계좌 잔액에서 나눠서 뺀다
            short, slot_cash = -self.data["vault"], sum(s["cash"] for s in self.data["slots"].values())
            for s in self.data["slots"].values():
                cut = min(s["cash"], short * s["cash"] / slot_cash) if slot_cash > 0 else 0.0
                s["cash"] -= cut
                add_flow(s, -cut)
            self.data["vault"] = 0.0
        self.data["synced"] = True
        self.save()
        return {"cash": flow, "shares": changes}
