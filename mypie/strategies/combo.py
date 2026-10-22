"""살 때·팔 때 전략 조합: 살 때와 팔 때 각각 전략 하나(A), 둘 다(A 그리고 B), 하나라도(A 또는 B)를 고른다.

규칙
  - 주식(코인)이 없을 땐 '살 때' 쪽만, 있을 땐 '팔 때' 쪽만 본다 (봇·백테스트가 이미 그렇게 쓴다)
  - 전략마다 '혼자 굴렸다면 지금 들고 있을까'(그림자 계좌, Shadow)를 봉마다 따라가서 그 상태로 판단한다.
    RSI처럼 신호가 잠깐만 나는 전략도 '그리고'가 엇갈리지 않게
  - 살 때는 '새로 사자는 때'만 산다: 살 쪽 상태가 '안 삼 → 삼'으로 바뀐 봉. 팔 때 쪽 전략이 다르면
    판 뒤 바로 다시 사는 일(사고팔기 반복)이 없게
  - 장중 돌파·이탈 가격(buy_stop·sell_stop)은 조건에 맞게 합친다
  - 과거 봉이 모자라 판단을 못 하는 전략은 '팔자'로 치지 않는다 (혼자 쓸 때처럼 들고 있는다)
  - 살 때·팔 때가 같은 전략 하나면 그 전략을 그대로 쓴다 (예전과 똑같이 움직인다)
"""

from .base import DayPlan, Strategy

OPS = ("and", "or")
SHORT = "데이터 부족"  # 전략들이 과거 봉이 모자랄 때 남기는 설명의 앞부분
OP_TEXT = {"and": "그리고", "or": "또는"}


def normalize_rule(conf: dict) -> dict:
    """줄 설정에서 살 때·팔 때 규칙을 꺼낸다. 예전 설정(전략 하나)은 살 때·팔 때 모두 그 전략."""
    main = str(conf.get("strategy") or "")

    def side(names) -> list[str]:
        names = [str(n) for n in names][:2] if isinstance(names, list) and names else [main]
        return names[:1] if len(names) == 2 and names[0] == names[1] else names

    def op(value, names) -> str:  # 전략이 하나뿐이면 그리고·또는은 의미가 없어서 '그리고'로 맞춘다
        return value if value in OPS and len(names) == 2 else "and"

    buy, sell = side(conf.get("buy")), side(conf.get("sell"))
    return {"strategy": buy[0], "buy": buy, "buy_op": op(conf.get("buy_op"), buy),
            "sell": sell, "sell_op": op(conf.get("sell_op"), sell)}


def check_rule(conf: dict, registry: dict) -> tuple[dict | None, str | None]:
    """웹페이지에서 온 규칙 검사. (정리된 규칙, 문제) 중 하나."""
    rule = normalize_rule(conf)
    if any(n not in registry for n in rule["buy"] + rule["sell"]):
        return None, "알 수 없는 전략이 있어요"
    return rule, None


def rule_fields(rule: dict) -> dict:
    """설정에 저장할 값: 간단한 규칙(같은 전략 하나)이면 예전처럼 전략 이름만."""
    if is_simple(rule):
        return {"strategy": rule["strategy"]}
    return {k: rule[k] for k in ("strategy", "buy", "buy_op", "sell", "sell_op")}


def is_simple(rule: dict) -> bool:
    return len(rule["buy"]) == 1 and rule["buy"] == rule["sell"]


def rule_label(rule: dict, label_of) -> str:
    """화면용 이름. 예: '살 때 골든크로스 · 팔 때 골든크로스 그리고 슈퍼트렌드'."""
    if is_simple(rule):
        return label_of(rule["buy"][0])
    side = lambda names, op: f" {OP_TEXT[op]} ".join(label_of(n) for n in names)
    return f"살 때 {side(rule['buy'], rule['buy_op'])} · 팔 때 {side(rule['sell'], rule['sell_op'])}"


def make_rule(rule: dict, make) -> Strategy:
    """규칙 → 전략. make(이름)은 전략 하나를 만든다. 간단한 규칙이면 그 전략 그대로."""
    rule = normalize_rule(rule)
    if is_simple(rule):
        return make(rule["buy"][0])
    return Combo(rule, make)


class Shadow:
    """전략 하나가 혼자 굴렸다면 들고 있을지를 봉마다 따라간다 (backtest.simulate와 같은 규칙)."""

    def __init__(self, strategy: Strategy):
        self.strategy = strategy
        self.holding = False
        self.plan: DayPlan | None = None   # 지금 봉의 계획
        self.bought = False                # 지금 봉 시작에 샀는지
        self.last: str | None = None       # 계획을 세울 때 마지막 완성 봉의 키
        self.before = False                # 지금 봉 시작 매매 전 상태 (같은 봉을 다시 계획할 때 되돌리기용)
        self.state = False                 # 지금 봉 시작 매매 뒤 상태
        self.prev_state = False            # 직전 봉 시작 매매 뒤 상태

    def _open(self, plan: DayPlan) -> None:
        self.before, self.bought = self.holding, False
        if self.holding and plan.sell_at_open:
            self.holding = False
        elif not self.holding and plan.buy_at_open:
            self.holding = self.bought = True
        self.plan = plan
        self.prev_state, self.state = self.state, self.holding

    def _intraday(self, bar: dict) -> None:
        plan, stopped = self.plan, False
        if self.holding and not self.bought and plan.sell_stop is not None and bar["low"] <= plan.sell_stop:
            self.holding, stopped = False, True
        if not self.holding and not self.bought and not stopped and plan.buy_stop is not None and bar["high"] >= plan.buy_stop:
            self.holding = True

    def _replay(self, history: list[dict]) -> None:
        """처음이거나 봉을 건너뛰었으면 받은 과거 봉으로 처음부터 다시 굴린다."""
        self.holding = self.state = self.prev_state = False
        w = self.strategy.window
        for j in range(1, len(history)):
            self._open(self.strategy.plan(history[max(0, j - w):j], history[j]["open"]))
            self._intraday(history[j])

    def advance(self, history: list[dict], today_open: float) -> DayPlan:
        last = history[-1]["day"] if history else None
        if self.plan is not None and last == self.last:
            self.holding, self.state = self.before, self.prev_state  # 같은 봉을 다시 계획: 시작 매매를 되돌린다
        elif self.plan is not None and len(history) >= 2 and history[-2]["day"] == self.last:
            self._intraday(history[-1])  # 직전 봉 장중 결과 반영
        else:
            self._replay(history)
        self.last = last
        plan = self.strategy.plan(history[-self.strategy.window:], today_open)
        self._open(plan)
        return plan


class Combo(Strategy):
    name = "combo"
    exit_next_open = False

    def __init__(self, rule: dict, make):
        super().__init__({})
        self.rule = normalize_rule(rule)
        names = list(dict.fromkeys(self.rule["buy"] + self.rule["sell"]))
        self.parts = {n: make(n) for n in names}
        self.shadows = {n: Shadow(s) for n, s in self.parts.items()}
        self.window = max(s.window for s in self.parts.values())
        self.label = rule_label(self.rule, lambda n: self.parts[n].label)
        self.description = self.label
        self.guide = " / ".join(f"{s.label}: {s.description}" for s in self.parts.values())
        sell = self.rule["sell"]
        self.exit_next_open = len(sell) == 1 and self.parts[sell[0]].exit_next_open

    def plan(self, history, today_open):
        plans = {n: sh.advance(history, today_open) for n, sh in self.shadows.items()}
        now = {n: sh.state for n, sh in self.shadows.items()}
        before = {n: sh.prev_state for n, sh in self.shadows.items()}
        known = {n: not plans[n].note.startswith(SHORT) for n in plans}

        # 살 때: 살 쪽 상태가 이번 봉에 새로 '삼'이 되면 시작가에 산다. 아니면 장중 돌파 가격
        buy, buy_op = self.rule["buy"], self.rule["buy_op"]
        joined = all if buy_op == "and" else any
        buy_at_open = joined(now[n] for n in buy) and not joined(before[n] for n in buy)
        waiting = [n for n in buy if not now[n]]
        stops = [plans[n].buy_stop for n in waiting if plans[n].buy_stop is not None]
        buy_stop = None
        if not buy_at_open and not joined(now[n] for n in buy) and stops:
            if buy_op == "or":
                buy_stop = min(stops)                       # 하나라도 돌파하면
            elif len(stops) == len(waiting):
                buy_stop = max(stops)                       # 아직 안 산 쪽이 모두 돌파해야

        # 팔 때: 팔 쪽이 '안 들고 있음'이면 시작가에 판다. 아니면 장중 이탈 가격
        sell, sell_op = self.rule["sell"], self.rule["sell_op"]
        out = [n for n in sell if known[n] and not now[n]]
        sell_at_open = len(out) == len(sell) if sell_op == "and" else bool(out)
        sell_stop = None
        if not sell_at_open:
            holding = [n for n in sell if now[n]]
            stops = [plans[n].sell_stop for n in holding if plans[n].sell_stop is not None]
            if stops and sell_op == "or":
                sell_stop = max(stops)                      # 하나라도 이탈하면
            elif stops and len(stops) == len(holding):
                sell_stop = min(stops)                      # 아직 들고 있는 쪽이 모두 이탈해야

        state = lambda n: "보유" if now[n] else "관망" if known[n] else SHORT
        note = " · ".join(f"{self.parts[n].label}: {state(n)}" for n in self.parts)
        return DayPlan(buy_at_open=buy_at_open, sell_at_open=sell_at_open, buy_stop=buy_stop,
                       sell_stop=sell_stop, note=note)
