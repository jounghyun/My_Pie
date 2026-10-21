"""해외 인기 전략: TradingView 커뮤니티 스크립트 '역대 인기(Top)' 상위권 지표를 매매 규칙으로 옮긴 것.

  1위 Squeeze Momentum [LazyBear]          → SqueezeMomentum
  2위 Smart Money Concepts [LuxAlgo]       → SmartMoney (그중 시장 구조 돌파 BOS/CHoCH만)
  3위 MACD [ChrisMoody]                    → 이미 있는 signals.Macd
  5위 SuperTrend [KivancOzbilgic]          → SuperTrend (4위 Williams Vix Fix는 매도 규칙이 없어서 뺌)

원작은 차트에 그리는 지표라서, 사고파는 때는 그 지표를 쓰는 사람들이 흔히 쓰는 읽는 법을 따랐다.
다른 신호형 전략처럼 봉이 끝난 뒤(종가) 보고 다음 봉 시가에 사고판다.
"""

from ..indicators import bollinger, linreg, rma_series, sma, true_ranges
from .base import SignalStrategy, closes

NOT_ENOUGH = (None, "데이터 부족")


class SqueezeMomentum(SignalStrategy):
    name = "squeeze_momentum"
    label = "스퀴즈 모멘텀"
    description = "가격이 좁게 뭉쳤다가 풀리면서 오르는 힘이 커지면 매수, 그 힘이 줄어들면 매도"
    guide = ("트레이딩뷰에서 역대 '좋아요'를 가장 많이 받은 지표(LazyBear)예요. 가격이 한동안 좁은 범위에 갇혀 있으면 "
             "'힘을 모으는 중(스퀴즈)'으로 봐요. 그게 풀리면서 최근 {N:20} 흐름의 오르는 힘이 커지면 사고, "
             "그 힘이 한 번이라도 줄어들면 팔아요. 터지는 움직임을 잘 잡지만, 빨리 팔아서 긴 상승을 다 못 먹을 때가 많아요.")
    # 원작 기본값: 볼린저·켈트너 모두 20봉. 원작 스크립트는 볼린저 폭에도 켈트너 배수(1.5)를 쓰는데 그대로 따른다
    defaults = {"length": 20, "bb_mult": 1.5, "kc_mult": 1.5}

    def want(self, history):
        n = int(self.params["length"])
        if len(history) < 2 * n + 1:
            return NOT_ENOUGH
        h, l, c = [x["high"] for x in history], [x["low"] for x in history], closes(history)

        def momentum(end: int) -> float:
            # 종가 − (최근 n봉 가운데값과 n봉 평균의 평균)을 n봉 동안 직선으로 맞춘 끝값
            gaps = [c[j] - ((max(h[j - n + 1:j + 1]) + min(l[j - n + 1:j + 1])) / 2 + sum(c[j - n + 1:j + 1]) / n) / 2
                    for j in range(end - n, end)]
            return linreg(gaps, n)

        val, prev = momentum(len(c)), momentum(len(c) - 1)
        bb_low, _, bb_up = bollinger(c, n, float(self.params["bb_mult"]))
        ma, rng = sma(c, n), sma(true_ranges(h, l, c), n)
        k = float(self.params["kc_mult"])
        squeeze = bb_low > ma - rng * k and bb_up < ma + rng * k  # 볼린저 띠가 켈트너 띠 안에 쏙 들어감
        power = f"힘 {val / c[-1] * 100:+.2f}% ({'커지는 중' if val > prev else '줄어드는 중'})"
        if val > 0 and val > prev and not squeeze:
            return True, f"스퀴즈 풀림 · {power}"
        if val < prev:
            return False, power
        return None, ("스퀴즈 중 (힘 모으는 중) · " if squeeze else "") + power


class SmartMoney(SignalStrategy):
    name = "smart_money"
    label = "스마트 머니 (시장 구조)"
    description = "종가가 직전 고점(스윙 하이)을 넘으면 상승 구조로 보고 매수, 직전 저점(스윙 로우)을 깨면 매도"
    guide = ("트레이딩뷰 인기 2위 'Smart Money Concepts'(LuxAlgo)의 핵심인 '시장 구조'만 따왔어요. 양옆 {N:5}보다 높은 꼭짓점을 "
             "고점, 낮은 바닥을 저점으로 잡아요. 종가가 직전 고점을 넘으면(구조 돌파) '큰손이 올리는 중'으로 보고 사고, "
             "직전 저점을 깨면 팔아요. 추세가 분명할 때 강하고, 위아래로 흔드는 장에서는 고점에 사서 저점에 팔기 쉬워요. "
             "원작의 오더블록·FVG 같은 나머지 표시는 쓰지 않아요.")
    defaults = {"swing": 5}

    def want(self, history):
        n = int(self.params["swing"])
        if len(history) < 2 * n + 2:
            return NOT_ENOUGH
        h, l, c = [x["high"] for x in history], [x["low"] for x in history], closes(history)
        trend, top, bottom = 0, None, None
        for i in range(2 * n, len(c)):
            p = i - n  # n봉 전 봉이 양옆 n봉 중 가장 높으면(낮으면) 지금 고점(저점)으로 확정
            if h[p] == max(h[p - n:i + 1]):
                top = h[p]
            if l[p] == min(l[p - n:i + 1]):
                bottom = l[p]
            if top is not None and c[i] > top:
                trend, top = 1, None      # 고점 돌파 = 상승 구조. 한 번 쓴 고점은 지운다
            if bottom is not None and c[i] < bottom:
                trend, bottom = -1, None  # 저점 이탈 = 하락 구조
        if trend == 0:
            return None, "아직 구조 돌파 없음"
        if trend == 1:
            return True, "상승 구조" + (f" · 저점 {bottom:,.0f} 깨지면 매도" if bottom is not None else "")
        return False, "하락 구조" + (f" · 고점 {top:,.0f} 넘으면 매수" if top is not None else "")


class SuperTrend(SignalStrategy):
    name = "supertrend"
    label = "슈퍼트렌드"
    description = "종가가 평소 흔들림(ATR)의 3배 띠를 위로 뚫으면 매수, 아래로 뚫으면 매도"
    guide = ("트레이딩뷰에서 가장 많이 쓰는 추세 지표 중 하나(KivancOzbilgic)예요. 최근 {N:10} 평균 흔들림(ATR)의 3배만큼 "
             "떨어진 곳에 선을 긋고, 가격이 오르면 선도 따라 올라와요(내려가지는 않음). 종가가 그 선 아래로 떨어지면 팔고, "
             "반대쪽 선을 위로 넘으면 사요. 큰 추세를 끝까지 타기 좋지만, 횡보장에서는 몇 번씩 속을 수 있어요.")
    defaults = {"period": 10, "mult": 3.0}

    def want(self, history):
        n, k = int(self.params["period"]), float(self.params["mult"])
        if len(history) < n + 2:
            return NOT_ENOUGH
        h, l, c = [x["high"] for x in history], [x["low"] for x in history], closes(history)
        atr = rma_series(true_ranges(h, l, c), n)
        trend, up_prev, dn_prev = 1, None, None
        for i in range(n - 1, len(c)):
            mid, a = (h[i] + l[i]) / 2, atr[i - n + 1]
            up, dn = mid - k * a, mid + k * a
            if up_prev is not None:
                if c[i - 1] > up_prev:
                    up = max(up, up_prev)  # 아래 선은 올라가기만 한다
                if c[i - 1] < dn_prev:
                    dn = min(dn, dn_prev)  # 위 선은 내려가기만 한다
                if trend == -1 and c[i] > dn_prev:
                    trend = 1
                elif trend == 1 and c[i] < up_prev:
                    trend = -1
            up_prev, dn_prev = up, dn
        if trend == 1:
            return True, f"상승 추세 · 종가가 {up_prev:,.0f} 아래면 매도"
        return False, f"하락 추세 · 종가가 {dn_prev:,.0f} 위면 매수"
