"""장부와 주문 실행.

장부(Book): 업비트 계좌 하나를 '금고'와 '코인 계좌' 여러 개로 나눠서 쓴다 (주식 장부 vault.py와 같은 생각).
  - 금고(vault): 쉬는 원화. 입금하면 금고로 들어오고, 출금하면 금고에서 빠진다.
  - 코인 계좌(slot): 코인 하나 + 전략 + 판단 주기. 자기 원화(krw)와 코인(coin)만 가지고 사고판다.
      · 팔면 그 돈은 계좌에 남는다 (금고로 가지 않는다)
      · 금고 ↔ 계좌 이체는 사람이 직접 한다. 이체해도 바로 사지 않고, 매수 신호가 날 때만 산다
      · 같은 코인도 전략을 달리해서 여러 계좌를 만들 수 있다 (이름 뒤에 번호가 붙는다)
  - 실거래는 업비트 계좌 전체를 쓴다: 원화는 금고 + 계좌들 것, 계좌가 있는 코인은 그 계좌들 것.
    봇이 모르는 코인(원래 있던 것, 직접 산 것)은 그 코인의 바이앤홀드 계좌로 모은다 (없으면 만든다).
    밖에서 팔린 코인은 바이앤홀드 계좌부터 뺀다. 계좌가 없는 코인은 건드리지 않는다.
  - contrib: 그 계좌에 넣은 돈 합계 (계좌 수익률 계산용), contributions: 봇이 굴리는 돈 전체에 넣은 돈 합계

주문 실행(Executor): 실제로 사고파는 부분.
  - PaperExecutor: 모의투자. 현재가에 바로 체결됐다고 가정하고 수수료만 뗀다.
  - LiveExecutor: 업비트 실거래. 주문마다 우리가 만든 번호(identifier)를 붙이고 주문 직전에
    '진행 중인 주문'으로 DB에 적어 둔다. 정전·통신 끊김으로 결과를 모르게 되면 recover()가
    그 번호로 업비트에 물어서 체결됐으면 장부에 반영하게 하고, 안 들어갔으면 지운다.
"""

import math
import uuid

from .buyhold import add_flow
from .db import Database
from .strategies.combo import is_simple, normalize_rule, rule_fields
from .timeutil import ts
from .upbit import OrderNotPlaced, OrderRejected, OrderUnknown, UpbitClient, UpbitError
from .vault import VaultError

MIN_ORDER_KRW = 5000  # 업비트 최소 주문 금액
FULL_MARGIN = 0.005   # 살 때 남겨 두는 여유분 (0.5%). 수수료는 따로 뺀다
FLOW_THRESHOLD = 100  # 이보다 작은 장부·실제 잔고 차이는 반올림 오차로 본다 (원)
HOLD_STRATEGY = "buy_and_hold"
OUTSIDE_NAME = "{name} · 원래 있던 코인"  # 봇이 모르는 코인을 모으는 바이앤홀드 계좌 이름


def new_slot_id() -> str:
    return "s" + uuid.uuid4().hex[:6]


class Book:
    def __init__(self, db: Database, key: str, start_krw: float):
        self.db = db
        self.key = key
        self.data = db.get_state(key) or {"vault": float(start_krw), "slots": {},
                                         "contributions": float(start_krw), "synced": False}
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

    def cash(self) -> float:
        return self.data["vault"] + sum(s["krw"] for s in self.data["slots"].values())

    def equity(self, prices: dict) -> float:
        return self.cash() + sum(s["coin"] * prices.get(s["market"], s["avg_price"])
                                 for s in self.data["slots"].values())

    # ---- 코인 계좌 만들기·바꾸기·지우기·이체 ----

    def add_slot(self, sid: str, market: str, name: str, rule, tf: str) -> dict:
        """rule: 전략 이름 하나, 또는 살 때·팔 때 규칙. 이름이 겹치면 '비트코인 2'처럼 번호를 붙인다."""
        s = self.data["slots"][sid] = {"market": market, "name": self._unique_name(name), "krw": 0.0, "coin": 0.0,
                                       "avg_price": 0.0, "cost": 0.0, "contrib": 0.0, "hold": 0.0, "hold_cash": 0.0}
        self.set_rule(sid, rule, tf)
        return s

    def _unique_name(self, name: str) -> str:
        names = {s.get("name") for s in self.data["slots"].values()}
        n, unique = 2, name
        while unique in names:
            unique, n = f"{name} {n}", n + 1
        return unique

    def set_rule(self, sid: str, rule, tf: str | None = None) -> None:
        """전략(살 때·팔 때)과 판단 주기만 바꾼다. 들고 있는 코인은 그대로 두고 새 전략이 판단한다."""
        rule = normalize_rule({"strategy": rule} if isinstance(rule, str) else rule)
        s = self.slot(sid)
        for key in ("strategy", "buy", "buy_op", "sell", "sell_op"):
            s.pop(key, None)
        s.update(rule_fields(rule))
        if tf:
            s["tf"] = tf
        self.save()

    def set_stop(self, sid: str, pct: float) -> None:
        """손절선 % (0이면 안 씀)."""
        s = self.slot(sid)
        if pct:
            s["stop_loss"] = pct
        else:
            s.pop("stop_loss", None)
        self.save()

    def set_closing(self, sid: str, on: bool) -> None:
        """'팔고 빼기': 켜 두면 봇이 이 계좌 코인을 다 팔고 원화를 금고로 뺀다 (계좌는 지켜보기로 남는다) (그동안은 새로 사지 않는다)."""
        s = self.slot(sid)
        if on:
            s["closing"] = True
        else:
            s.pop("closing", None)
        self.save()

    def is_hold(self, sid: str) -> bool:
        """그냥 들고 있는(바이앤홀드) 계좌인지."""
        rule = normalize_rule(self.slot(sid))
        return is_simple(rule) and rule["strategy"] == HOLD_STRATEGY

    def remove_slot(self, sid: str) -> float:
        """코인이 없을 때만 지운다. 남은 원화는 금고로. 옮긴 금액을 돌려준다."""
        s = self.slot(sid)
        if s["coin"] > 0:
            raise VaultError("코인이 남아 있어서 지울 수 없어요. '팔고 빼기'를 써 주세요")
        self.data["vault"] += s["krw"]
        del self.data["slots"][sid]
        self.save()
        return s["krw"]

    def transfer(self, sid: str, amount: float) -> None:
        """amount > 0: 금고 → 코인 계좌, amount < 0: 코인 계좌 → 금고."""
        s = self.slot(sid)
        if amount > 0 and amount > self.data["vault"] + 0.5:
            raise VaultError(f"금고에 {self.data['vault']:,.0f}원밖에 없어요")
        if amount < 0 and -amount > s["krw"] + 0.5:
            raise VaultError(f"그 계좌 원화가 {s['krw']:,.0f}원밖에 없어요")
        amount = min(amount, self.data["vault"]) if amount > 0 else -min(-amount, s["krw"])
        self.data["vault"] -= amount
        s["krw"] += amount
        add_flow(s, amount)
        self.save()

    def deposit(self, amount: float) -> None:
        """금고에 입금(+)·출금(−). 모의투자의 가상 입출금에 쓴다."""
        if amount < 0 and -amount > self.data["vault"] + 0.5:
            raise VaultError(f"금고에 {self.data['vault']:,.0f}원밖에 없어요")
        self.data["vault"] = max(self.data["vault"] + amount, 0.0)
        self.data["contributions"] += amount
        self.save()

    # ---- 체결 반영 ----

    def apply_buy(self, slot_id: str, volume: float, funds: float, fee: float, reason: str) -> dict:
        s = self.data["slots"][slot_id]
        spent = funds + fee
        coin = s["coin"] + volume
        s.update(krw=s["krw"] - spent, coin=coin, cost=s["cost"] + spent,
                 avg_price=(s["avg_price"] * s["coin"] + funds) / coin if coin else 0.0)
        if s["krw"] < 0:  # 수수료 반올림 등으로 조금 넘친 건 금고에서 채운다
            self.data["vault"] += s["krw"]
            s["krw"] = 0.0
        self.save()
        return {"slot": slot_id, "side": "buy", "price": funds / volume, "volume": volume, "krw": spent, "fee": fee,
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
        return {"slot": slot_id, "side": "sell", "price": funds / volume, "volume": volume, "krw": proceeds,
                "fee": fee, "profit": proceeds - sold_cost, "cost": sold_cost, "reason": reason}

    def clear_dust(self, slot_id: str) -> None:
        """너무 적어서 팔 수 없는 자투리 코인은 장부에서만 정리한다."""
        s = self.data["slots"][slot_id]
        s.update(coin=0.0, cost=0.0, avg_price=0.0)
        self.save()

    # ---- 실거래: 업비트 실제 잔고와 맞추기 ----

    def sync(self, accounts: list[dict], prices: dict, names: dict | None = None) -> dict:
        """업비트 실제 원화·코인으로 장부를 맞춘다.
        {"cash": 금고에 반영한 입출금, "coins": {종목: 바뀐 수량}, "added": [새로 만든 바이앤홀드 계좌]}.

        - 원화 차이는 입금·출금으로 보고 금고에 반영한다. 출금이 금고보다 크면 계좌들 원화에서 나눠서 뺀다
        - 계좌가 있는 코인은 업비트에 있는 그 코인 전부가 그 계좌들 것이다 (같은 코인 계좌가 여럿이면 합쳐서).
          밖에서 늘면(원래 있던 코인, 직접 산 것) 그 코인의 바이앤홀드 계좌로, 밖에서 줄면 바이앤홀드 계좌부터 뺀다.
          지금 시세로 원가와 넣은 돈에 반영한다
        """
        acc = {a["currency"]: a for a in accounts}
        changes, added = {}, []
        groups: dict[str, list[str]] = {}
        for sid, s in self.data["slots"].items():
            groups.setdefault(s["market"], []).append(sid)
        for market, sids in groups.items():
            entry = acc.get(market.split("-")[-1], {})
            actual = float(entry.get("balance", 0)) + float(entry.get("locked", 0) or 0)
            delta = actual - sum(self.slot(x)["coin"] for x in sids)
            price = prices.get(market) or float(entry.get("avg_buy_price") or 0) \
                or max(self.slot(x)["avg_price"] for x in sids)
            if abs(delta) * price < FLOW_THRESHOLD:
                continue  # 수수료 반올림 같은 자투리
            if delta > 0:
                sid = next((x for x in sids if self.is_hold(x)), None)
                if sid is None:
                    sid = new_slot_id()
                    name = (names or {}).get(market) or self.slot(sids[0]).get("name") or market
                    self.add_slot(sid, market, OUTSIDE_NAME.format(name=name), HOLD_STRATEGY, "1d")
                    added.append(sid)
                self._coins_in(self.slot(sid), delta, price)
            else:
                need = -delta  # 바이앤홀드 계좌부터, 그다음 코인이 많은 계좌부터
                for x in sorted(sids, key=lambda x: (not self.is_hold(x), -self.slot(x)["coin"])):
                    take = min(need, self.slot(x)["coin"])
                    if take > 0:
                        self._coins_out(self.slot(x), take, price)
                        need -= take
            changes[market] = delta

        krw = float(acc.get("KRW", {}).get("balance", 0)) + float(acc.get("KRW", {}).get("locked", 0) or 0)
        flow = krw - self.cash()
        self.data["vault"] += flow  # 차이는 전부 금고에 (처음 맞출 때는 계좌 원화 전부, 그 뒤로는 입금·출금)
        if self.data.get("synced") and abs(flow) < FLOW_THRESHOLD:
            flow = 0.0  # 수수료 반올림 같은 작은 오차는 넣은 돈으로 치지 않는다
        else:
            self.data["contributions"] += flow
        if self.data["vault"] < 0:  # 금고보다 많이 출금됐으면 계좌들 원화에서 나눠서 뺀다
            short, slot_krw = -self.data["vault"], sum(s["krw"] for s in self.data["slots"].values())
            for s in self.data["slots"].values():
                cut = min(s["krw"], short * s["krw"] / slot_krw) if slot_krw > 0 else 0.0
                s["krw"] -= cut
                add_flow(s, -cut)
            self.data["vault"] = 0.0
        self.data["synced"] = True
        self.save()
        return {"cash": flow, "coins": changes, "added": added}

    def _coins_in(self, s: dict, volume: float, price: float) -> None:
        """밖에서 생긴 코인을 지금 시세를 원가로 해서 넣는다."""
        value = volume * price
        s["cost"] += value
        s["avg_price"] = (s["avg_price"] * s["coin"] + value) / (s["coin"] + volume)
        s["coin"] += volume
        add_flow(s, value)
        self.data["contributions"] += value

    def _coins_out(self, s: dict, volume: float, price: float) -> None:
        """밖에서 팔린 코인을 뺀다 (지금 시세만큼 빼낸 돈으로 친다)."""
        left = s["coin"] - volume
        s["cost"] = s["cost"] * left / s["coin"] if s["coin"] else 0.0
        if left * price < 1:
            left, s["cost"], s["avg_price"] = 0.0, 0.0, 0.0
        s["coin"] = left
        add_flow(s, -volume * price)
        self.data["contributions"] -= volume * price


def migrate_book(data: dict, defs: dict) -> dict:
    """비중(%) 시절 장부를 금고 장부로 옮긴다. defs: {줄 id: 그 줄 설정(종목·전략·주기·손절선·이름)}.
    대기 원화는 금고로, 줄마다의 원화·코인은 그대로 그 계좌 것으로. 설정이 없는 줄은 바이앤홀드 계좌로."""
    data = dict(data)
    data["vault"] = data.pop("reserve", 0.0)
    data["synced"] = data.get("budget") == "all" and not data.get("needs_sync")
    for key in ("budget", "needs_alloc", "needs_sync"):
        data.pop(key, None)
    slots = {}
    for sid, s in data.get("slots", {}).items():
        conf = defs.get(sid) or {"strategy": HOLD_STRATEGY, "tf": "1d"}
        rule = normalize_rule(conf)
        entry = {**s, **rule_fields(rule), "tf": conf.get("tf") or "1d", "name": conf.get("name") or s["market"]}
        if conf.get("stop_loss"):
            entry["stop_loss"] = conf["stop_loss"]
        slots[sid] = entry
    data["slots"] = slots
    return data


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

    def __init__(self, db: Database, client: UpbitClient, fee_rate: float, full: bool = True):
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
