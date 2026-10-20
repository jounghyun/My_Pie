"""장중 돌파형 전략: 정해 둔 가격을 넘으면 그 자리에서 매수한다."""

from ..indicators import sma
from .base import DayPlan, Strategy, closes

NOT_ENOUGH = "데이터 부족"


class VolatilityBreakout(Strategy):
    name = "volatility_breakout"
    label = "변동성 돌파"
    description = "{cur} 시가 + {prev} 변동폭 × k를 넘으면 매수하고 {next} 매도"
    guide = ("가격이 '{cur} 시가 + {prev} 움직인 폭의 절반'을 넘으면 '오르는 흐름'으로 보고 사고, "
             "{next} 무조건 팔아요. {hold} 넘게 들고 있지 않아서 큰 하락을 피하기 쉬워요. "
             "대신 오르는 척하다 떨어지는 '가짜 돌파'가 잦은 횡보장에서는 자주 손해를 봐요.")
    exit_next_open = True
    defaults = {"k": 0.5}

    def plan(self, history, today_open):
        if not history:
            return DayPlan(sell_at_open=True, note=NOT_ENOUGH)
        y = history[-1]
        k = float(self.params["k"])
        target = today_open + (y["high"] - y["low"]) * k
        return DayPlan(sell_at_open=True, buy_stop=target, note=f"목표가 {target:,.0f}원 (k={k:g})")


class VolatilityBreakoutMA(VolatilityBreakout):
    name = "volatility_breakout_ma"
    label = "변동성 돌파 + 이동평균"
    description = "{prev} 종가가 {N:5} 이동평균 위(상승 추세)일 때만 변동성 돌파로 매수"
    guide = ("변동성 돌파와 같지만, {prev} 종가가 최근 {N:5} 평균보다 높을 때(상승 추세)만 사요. "
             "하락장에서는 쉬어서 손실을 줄이는 대신, 바닥에서 막 반등하는 구간은 놓칠 수 있어요.")
    defaults = {"k": 0.5, "ma": 5}

    def plan(self, history, today_open):
        n = int(self.params["ma"])
        ma = sma(closes(history), n)
        if ma is None:
            return DayPlan(sell_at_open=True, note=NOT_ENOUGH)
        if history[-1]["close"] <= ma:
            return DayPlan(sell_at_open=True, note=f"하락 추세 (종가 < {n}{self.bar}선) · 이번엔 매수 안 함")
        plan = super().plan(history, today_open)
        plan.note += f" · {n}{self.bar}선 위"
        return plan


class Turtle(Strategy):
    name = "turtle"
    label = "터틀 트레이딩"
    description = "{N:20} 최고가를 돌파하면 매수, {N:10} 최저가를 깨면 매도"
    guide = ("1980년대 전설적인 '터틀' 트레이더들의 규칙이에요. 최근 {N:20} 최고가를 넘으면 사고, "
             "최근 {N:10} 최저가를 깨면 팔아요. 큰 상승장을 끝까지 타는 데 강하지만, "
             "작게 자주 잃고 가끔 크게 버는 구조라 승률은 낮은 편이에요.")
    defaults = {"entry": 20, "exit": 10}

    def plan(self, history, today_open):
        entry, exit_ = int(self.params["entry"]), int(self.params["exit"])
        if len(history) < max(entry, exit_):
            return DayPlan(note=NOT_ENOUGH)
        high = max(c["high"] for c in history[-entry:])
        low = min(c["low"] for c in history[-exit_:])
        return DayPlan(buy_stop=high, sell_stop=low,
                       note=f"{entry}{self.bar} 최고가 {high:,.0f}원 · {exit_}{self.bar} 최저가 {low:,.0f}원")
