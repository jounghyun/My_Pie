"""주식 장부: 실제 증권 계좌 하나를 '금고'와 '종목 계좌' 여러 개로 나눠서 쓴다.

  - 금고(vault): 쉬는 돈. 계좌에 입금하면 금고로 들어오고, 출금하면 금고에서 빠진다.
  - 종목 계좌(slot): 종목 하나 + 전략 하나. 자기 잔액(cash)과 자기 주식(qty)만 가지고 사고판다.
      · 팔면 그 돈은 종목 계좌 잔액에 남는다 (금고로 가지 않는다)
      · 금고 ↔ 종목 계좌 이체는 사람이 직접 한다. 이체해도 바로 사지 않고, 전략이 매수 신호를 낼 때만 산다
      · 같은 종목도 전략을 달리해서 여러 계좌를 만들 수 있다 (이름 뒤에 번호가 붙는다)
  - 실거래에서 봇이 모르는 주식(원래 있던 것, 직접 산 것)은 그 종목의 바이앤홀드 계좌로 모은다.
    없으면 '<종목> · 원래 있던 주식' 계좌를 만든다 (가져올 때 시세를 원가로 잡는다). 밖에서 팔린 주식은 거기서 먼저 뺀다.
  - contrib: 그 종목 계좌에 넣은 돈 합계 (이체·가져온 주식 − 빼낸 돈), 종목 계좌 수익률 계산용
    contributions: 봇이 굴리는 돈 전체에 넣은 돈 합계 (입금 − 출금), 전체 수익률 계산용
"""

import uuid

from .buyhold import add_flow
from .db import Database
from .strategies.combo import is_simple, normalize_rule, rule_fields

FLOW_THRESHOLD = 100  # 이보다 작은 장부·실제 잔고 차이는 반올림 오차로 본다 (원)
HOLD_STRATEGY = "buy_and_hold"
OUTSIDE_NAME = "{name} · 원래 있던 주식"  # 봇이 모르는 주식을 모으는 바이앤홀드 계좌 이름


def new_slot_id() -> str:
    return "k" + uuid.uuid4().hex[:6]


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

    def add_slot(self, sid: str, symbol: str, name: str, rule) -> dict:
        """rule: 전략 이름 하나, 또는 살 때·팔 때 규칙 (strategies/combo.py).
        같은 종목 계좌가 이미 있어도 된다. 이름이 겹치면 '삼성전자 2'처럼 번호를 붙인다."""
        s = self.data["slots"][sid] = {"symbol": symbol, "name": self._unique_name(name), "cash": 0.0, "qty": 0,
                                       "cost": 0.0, "avg_price": 0.0, "contrib": 0.0, "hold": 0.0, "hold_cash": 0.0}
        self.set_rule(sid, rule)
        return s

    def _unique_name(self, name: str) -> str:
        names = {s["name"] for s in self.data["slots"].values()}
        n = 2
        unique = name
        while unique in names:
            unique, n = f"{name} {n}", n + 1
        return unique

    def is_hold(self, sid: str) -> bool:
        """그냥 들고 있는(바이앤홀드) 계좌인지."""
        rule = normalize_rule(self.slot(sid))
        return is_simple(rule) and rule["strategy"] == HOLD_STRATEGY

    def set_rule(self, sid: str, rule) -> None:
        """전략(살 때·팔 때)만 바꾼다. 들고 있는 주식은 그대로 두고 새 전략이 판단한다."""
        rule = normalize_rule({"strategy": rule} if isinstance(rule, str) else rule)
        s = self.slot(sid)
        for key in ("buy", "buy_op", "sell", "sell_op"):
            s.pop(key, None)
        s.update(rule_fields(rule))
        self.save()

    def set_stop(self, sid: str, pct: float) -> None:
        """손절선 % (0이면 안 씀)."""
        s = self.slot(sid)
        if pct:
            s["stop_loss"] = pct
        else:
            s.pop("stop_loss", None)
        self.save()

    def remove_slot(self, sid: str) -> float:
        """주식이 없을 때만 지운다. 남은 잔액은 금고로. 옮긴 금액을 돌려준다."""
        s = self.slot(sid)
        if s["qty"] > 0:
            raise VaultError(f"주식이 {s['qty']:,}주 남아 있어서 지울 수 없어요. '팔고 빼기'를 써 주세요")
        self.data["vault"] += s["cash"]
        del self.data["slots"][sid]
        self.save()
        return s["cash"]

    def set_closing(self, sid: str, on: bool) -> None:
        """'팔고 빼기': 켜 두면 봇이 장중에 이 계좌 주식을 다 팔고 잔액을 금고로 뺀다 (계좌는 지켜보기로 남는다) (그동안은 새로 사지 않는다)."""
        s = self.slot(sid)
        if on:
            s["closing"] = True
        else:
            s.pop("closing", None)
        self.save()

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
        return {"slot": sid, "side": "buy", "price": price, "volume": qty, "krw": spent, "fee": fee, "profit": None,
                "reason": reason}

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
        return {"slot": sid, "side": "sell", "price": price, "volume": qty, "krw": proceeds, "fee": fee + tax,
                "profit": proceeds - sold_cost, "cost": sold_cost, "reason": reason}

    # ---- 실거래: 실제 계좌와 맞추기 ----

    def sync(self, cash: float, holdings: dict, prices: dict) -> dict:
        """실제 주문가능 현금과 보유 주식으로 장부를 맞춘다.
        {"cash": 금고에 반영한 입출금, "shares": {종목: 바뀐 주수}, "added": [새로 만든 바이앤홀드 계좌]}.

        - 종목 계좌가 있는 종목은 계좌에 있는 그 종목 주식 전부가 종목 계좌들 것이다 (같은 종목 계좌가 여럿이면 합쳐서).
          밖에서 늘면(원래 있던 주식, 직접 산 것) 그 종목의 바이앤홀드 계좌로, 밖에서 줄면 바이앤홀드 계좌부터 뺀다.
          지금 시세로 원가와 넣은 돈에 반영한다
        - 현금 차이는 입금·출금으로 보고 금고에 반영한다. 출금이 금고보다 크면 종목 계좌 잔액에서 나눠서 뺀다
        """
        changes, added = {}, []
        groups: dict[str, list[str]] = {}
        for sid, s in self.data["slots"].items():
            groups.setdefault(s["symbol"], []).append(sid)
        for symbol, sids in groups.items():
            held = holdings.get(symbol, {})
            delta = int(held.get("qty", 0)) - sum(self.slot(x)["qty"] for x in sids)
            if not delta:
                continue
            price = prices.get(symbol) or held.get("avg_price") or max(self.slot(x)["avg_price"] for x in sids)
            if delta > 0:
                sid = next((x for x in sids if self.is_hold(x)), None)
                if sid is None:
                    sid = new_slot_id()
                    name = held.get("name") or self.slot(sids[0])["name"]
                    self.add_slot(sid, symbol, OUTSIDE_NAME.format(name=name), HOLD_STRATEGY)
                    added.append(sid)
                self._shares_in(self.slot(sid), delta, price)
            else:
                need = -delta  # 바이앤홀드 계좌부터, 그다음 주식이 많은 계좌부터
                for x in sorted(sids, key=lambda x: (not self.is_hold(x), -self.slot(x)["qty"])):
                    take = min(need, self.slot(x)["qty"])
                    if take:
                        self._shares_out(self.slot(x), take, price)
                        need -= take
            changes[symbol] = delta

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
        return {"cash": flow, "shares": changes, "added": added}

    def _shares_in(self, s: dict, qty: int, price: float) -> None:
        """밖에서 생긴 주식을 지금 시세를 원가로 해서 넣는다."""
        value = qty * price
        s["cost"] += value
        s["avg_price"] = (s["avg_price"] * s["qty"] + value) / (s["qty"] + qty)
        s["qty"] += qty
        add_flow(s, value)
        self.data["contributions"] += value

    def _shares_out(self, s: dict, qty: int, price: float) -> None:
        """밖에서 팔린 주식을 뺀다 (지금 시세만큼 빼낸 돈으로 친다)."""
        left = s["qty"] - qty
        s["cost"] = s["cost"] * left / s["qty"] if s["qty"] else 0.0
        if not left:
            s["avg_price"] = 0.0
        s["qty"] = left
        add_flow(s, -qty * price)
        self.data["contributions"] -= qty * price
