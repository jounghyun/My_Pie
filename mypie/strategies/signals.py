"""종가 기준 신호형 전략: 매일 09시에 어제 종가까지 보고 사거나 판다."""

from ..indicators import bollinger, macd, rsi, sma, stochastic
from .base import SignalStrategy, closes

NOT_ENOUGH = (None, "데이터 부족")


class MaCross(SignalStrategy):
    name = "ma_cross"
    label = "이동평균 골든크로스"
    description = "{N:5} 평균이 {N:20} 평균 위에 있으면 보유, 아래로 내려가면 매도"
    guide = ("최근 {N:5} 평균 가격이 {N:20} 평균보다 높으면 '상승 추세'로 보고 들고 있다가, 아래로 내려가면 팔아요. "
             "큰 추세를 길게 타기 좋지만, 신호가 늦게 나와서 고점 근처에서 사고 저점 근처에서 팔 때도 있어요.")
    defaults = {"short": 5, "long": 20}

    def want(self, history):
        s, l = int(self.params["short"]), int(self.params["long"])
        c = closes(history)
        short, long_ = sma(c, s), sma(c, l)
        if long_ is None:
            return NOT_ENOUGH
        return short > long_, f"{s}{self.bar}선 {short:,.0f} / {l}{self.bar}선 {long_:,.0f}"


class Rsi(SignalStrategy):
    name = "rsi"
    label = "RSI 역추세"
    description = "RSI가 30 아래(과매도)면 매수, 70 위(과매수)면 매도"
    guide = ("RSI는 최근 {N:14} 동안 오른 힘과 내린 힘을 0~100으로 나타낸 숫자예요. 30 아래면 '너무 많이 떨어졌다'고 보고 사고, "
             "70 위면 '너무 많이 올랐다'고 보고 팔아요. 반등을 노리는 전략이라, 계속 떨어지기만 하는 하락장에서는 손실이 커질 수 있어요.")
    defaults = {"period": 14, "buy_below": 30, "sell_above": 70}

    def want(self, history):
        r = rsi(closes(history), int(self.params["period"]))
        if r is None:
            return NOT_ENOUGH
        if r < self.params["buy_below"]:
            return True, f"RSI {r:.1f} · 과매도"
        if r > self.params["sell_above"]:
            return False, f"RSI {r:.1f} · 과매수"
        return None, f"RSI {r:.1f}"


class Bollinger(SignalStrategy):
    name = "bollinger"
    label = "볼린저 밴드"
    description = "종가가 밴드 하단 아래로 떨어지면 매수, 중심선 위로 올라오면 매도"
    guide = ("{N:20} 평균 가격 위아래로 '평소 흔들리는 폭'만큼 띠를 그려요. 가격이 띠 아래로 떨어지면 평소보다 많이 싸진 걸로 보고 사고, "
             "평균선 위로 돌아오면 팔아요. 일정 범위를 오르내리는 박스권에 강하고, 폭락장에서는 더 떨어질 수 있어요.")
    defaults = {"period": 20, "std": 2.0}

    def want(self, history):
        c = closes(history)
        band = bollinger(c, int(self.params["period"]), float(self.params["std"]))
        if band is None:
            return NOT_ENOUGH
        lower, mid, _ = band
        if c[-1] < lower:
            return True, f"하단 {lower:,.0f} 아래 · 매수 신호"
        if c[-1] > mid:
            return False, f"중심선 {mid:,.0f} 위"
        return None, f"하단 {lower:,.0f} · 중심 {mid:,.0f}"


class Macd(SignalStrategy):
    name = "macd"
    label = "MACD"
    description = "MACD선이 시그널선 위에 있으면 보유, 아래로 내려가면 매도"
    guide = ("{N:12} 평균과 {N:26} 평균의 차이(MACD)가 그 차이의 {N:9} 평균(시그널)보다 크면 '오르는 힘이 붙는 중'으로 보고 들고 있어요. "
             "추세가 바뀌는 걸 비교적 빨리 잡지만, 횡보장에서는 사고팔기를 자주 반복해서 수수료가 쌓여요.")
    defaults = {"fast": 12, "slow": 26, "signal": 9}

    def want(self, history):
        m = macd(closes(history), int(self.params["fast"]), int(self.params["slow"]), int(self.params["signal"]))
        if m is None:
            return NOT_ENOUGH
        line, sig = m
        return line > sig, f"MACD {line:,.0f} / 시그널 {sig:,.0f}"


class Momentum(SignalStrategy):
    name = "momentum"
    label = "모멘텀"
    description = "최근 {N:30} 수익률이 플러스면 보유, 마이너스면 매도"
    guide = ("지금 가격이 {N:30} 전보다 높으면 들고 있고, 낮으면 팔아요. '오르는 건 계속 오르는 경향'을 이용하는 아주 단순한 전략이에요. "
             "대신 추세가 바뀌는 구간에서는 반응이 늦어요.")
    defaults = {"lookback": 30}

    def want(self, history):
        n = int(self.params["lookback"])
        c = closes(history)
        if len(c) <= n:
            return NOT_ENOUGH
        ret = c[-1] / c[-1 - n] - 1
        return ret > 0, f"{n}{self.bar} 수익률 {ret * 100:+.1f}%"


class Stochastic(SignalStrategy):
    name = "stochastic"
    label = "스토캐스틱"
    description = "%K가 20 아래에서 %D 위로 올라서면 매수, 80 위에서 %D 아래로 내려가면 매도"
    guide = ("최근 {N:14} 가격 범위에서 지금 가격이 어디쯤인지를 0~100으로 봐요. 20 아래(바닥권)에서 다시 올라서기 시작하면 사고, "
             "80 위(꼭대기권)에서 꺾이면 팔아요. 박스권에 강하지만, 강하게 오르는 장에서는 너무 일찍 팔 수 있어요.")
    defaults = {"period": 14, "smooth": 3, "low": 20, "high": 80}

    def want(self, history):
        st = stochastic([c["high"] for c in history], [c["low"] for c in history], closes(history),
                        int(self.params["period"]), int(self.params["smooth"]))
        if st is None:
            return NOT_ENOUGH
        k, d = st
        if k < self.params["low"] and k > d:
            return True, f"%K {k:.1f} / %D {d:.1f} · 과매도에서 반등"
        if k > self.params["high"] and k < d:
            return False, f"%K {k:.1f} / %D {d:.1f} · 과매수에서 꺾임"
        return None, f"%K {k:.1f} / %D {d:.1f}"


class BuyAndHold(SignalStrategy):
    name = "buy_and_hold"
    label = "바이앤홀드 (기준)"
    description = "사서 계속 들고 있기 · 다른 전략이 이것보다 나은지 비교하는 기준"
    guide = ("아무 판단 없이 사서 계속 들고 있어요. 다른 전략들이 '그냥 들고 있기'보다 나은지 비교하는 기준이에요. "
             "상승장에서는 이기기 어려운 상대지만, 하락장에서는 떨어지는 만큼 손실을 그대로 다 맞아요.")

    def want(self, history):
        return True, "계속 보유"
