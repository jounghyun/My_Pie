"""주식 전용 전략: 국내 주식 투자자들이 많이 쓰는 규칙. 주식 봇(stockbot.py)에서만 보인다.

거래량과 52주(250거래일) 같은 긴 기간을 보는 규칙이라 코인보다 주식에서 뜻이 분명하다.
설명의 {N:20}은 주식 화면에서 '20거래일'로 보인다.
"""

from ..indicators import sma
from .base import DayPlan, SignalStrategy, Strategy, closes

NOT_ENOUGH = (None, "데이터 부족")


class NewHigh52(Strategy):
    name = "new_high_52w"
    label = "52주 신고가 돌파"
    description = "최근 {N:250}(약 1년) 최고가를 넘으면 매수, 종가가 {N:20} 평균 아래로 내려가면 매도"
    guide = ("주가가 1년 동안 한 번도 못 넘은 가격(52주 신고가)을 넘으면 '새로운 상승이 시작됐다'고 보고 그 자리에서 사요. "
             "윌리엄 오닐·다바스 같은 유명 주식 투자자들이 쓴 방법이에요. 종가가 {N:20} 평균 아래로 내려가면 {next} 팔아요. "
             "크게 오르는 주도주를 잘 잡지만, 신고가 근처에서 꺾이는 종목도 많아서 작은 손절이 잦아요.")
    defaults = {"lookback": 250, "exit_ma": 20}
    window = 260

    def plan(self, history, today_open):
        n, m = int(self.params["lookback"]), int(self.params["exit_ma"])
        if len(history) < n:
            return DayPlan(note="데이터 부족 (1년치 일봉 필요)")
        high = max(c["high"] for c in history[-n:])
        ma = sma(closes(history), m)
        below = history[-1]["close"] < ma
        return DayPlan(buy_stop=None if below else high, sell_at_open=below,
                       note=f"52주 최고가 {high:,.0f}원" + (f" · {m}{self.bar}선 {ma:,.0f} 아래 (보유 중이면 매도)" if below else ""))


class VolumeBreakout(SignalStrategy):
    name = "volume_breakout"
    label = "거래량 돌파"
    description = "거래량이 평소의 2배 넘게 터지면서 {N:20} 최고 종가를 넘으면 매수, {N:10} 평균 아래로 내려가면 매도"
    guide = ("평소보다 거래가 2배 넘게 몰리면서 최근 {N:20} 중 가장 높은 종가로 마감하면 '돈이 들어온다'고 보고 {next} 사요. "
             "종가가 {N:10} 평균 아래로 내려가면 팔아요. 거래량이 받쳐 주는 진짜 돌파만 고르려는 전략이에요. "
             "뉴스로 하루 반짝 오르고 마는 종목에서는 손해를 볼 수 있어요.")
    defaults = {"lookback": 20, "volume_mult": 2.0, "exit_ma": 10}

    def want(self, history):
        n, m = int(self.params["lookback"]), int(self.params["exit_ma"])
        if len(history) < n + 1:
            return NOT_ENOUGH
        c = closes(history)
        avg_volume = sum(x["volume"] for x in history[-n - 1:-1]) / n
        ratio = history[-1]["volume"] / avg_volume if avg_volume else 0.0
        ma = sma(c, m)
        if c[-1] > max(c[-n - 1:-1]) and ratio >= float(self.params["volume_mult"]):
            return True, f"거래량 {ratio:.1f}배 · {n}{self.bar} 최고 종가 돌파"
        if c[-1] < ma:
            return False, f"{m}{self.bar}선 {ma:,.0f} 아래"
        return None, f"거래량 {ratio:.1f}배 · {m}{self.bar}선 {ma:,.0f}"


class Disparity(SignalStrategy):
    name = "disparity"
    label = "이격도 매매"
    description = "종가가 {N:20} 평균보다 8% 넘게 낮으면 매수, 평균보다 2% 높아지면 매도"
    guide = ("이격도는 지금 가격이 {N:20} 평균에서 얼마나 떨어져 있는지예요. 평균보다 8% 넘게 빠지면 '너무 많이 떨어졌다'고 보고 "
             "사고, 평균을 2% 넘게 되찾으면 팔아요. 국내 주식에서 오래 써 온 반등 매매예요. "
             "박스권에서 잘 맞지만, 계속 떨어지는 종목에서는 더 떨어질 수 있어요.")
    defaults = {"period": 20, "buy_below": 92, "sell_above": 102}

    def want(self, history):
        n = int(self.params["period"])
        ma = sma(closes(history), n)
        if ma is None:
            return NOT_ENOUGH
        gap = history[-1]["close"] / ma * 100
        if gap <= self.params["buy_below"]:
            return True, f"이격도 {gap:.1f} · 평균보다 많이 빠짐"
        if gap >= self.params["sell_above"]:
            return False, f"이격도 {gap:.1f} · 평균 위로 회복"
        return None, f"이격도 {gap:.1f}"


class MaAlignment(SignalStrategy):
    name = "ma_alignment"
    label = "이동평균 정배열"
    description = "{N:5}·{N:20}·{N:60}·{N:120} 평균이 위에서부터 차례로 놓이면(정배열) 보유, {N:5} 평균이 {N:20} 평균 아래로 가면 매도"
    guide = ("짧은 평균일수록 위에 있는 '정배열'은 단기·중기·장기 흐름이 모두 오르는 중이라는 뜻이에요. "
             "정배열이 되면 사서 들고 있다가, {N:5} 평균이 {N:20} 평균 아래로 내려가 흐름이 꺾이면 팔아요. "
             "큰 상승 추세를 오래 타는 데 좋지만, 정배열이 완성될 때는 이미 많이 오른 뒤일 때가 많아요.")
    defaults = {"periods": [5, 20, 60, 120]}

    def want(self, history):
        periods = [int(p) for p in self.params["periods"]]
        c = closes(history)
        mas = [sma(c, p) for p in periods]
        if mas[-1] is None:
            return NOT_ENOUGH
        if all(a > b for a, b in zip(mas, mas[1:])):
            return True, "정배열 " + " > ".join(f"{p}{self.bar}" for p in periods)
        if mas[0] < mas[1]:
            return False, f"{periods[0]}{self.bar}선이 {periods[1]}{self.bar}선 아래"
        return None, "정배열 아님 · 기다리는 중"
