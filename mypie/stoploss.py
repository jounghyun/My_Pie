"""손절선: 산 값(수수료 포함 평단)보다 정한 %만큼 떨어지면 전략과 상관없이 판다. 0이면 안 쓴다.

손절로 판 뒤에는 그 전략의 매수 신호가 한 번 꺼졌다가 다시 켜질 때까지 사지 않는다 (cut).
신호가 계속 켜져 있는 전략(이동평균 등)이 바로 다시 사서 손절이 소용없어지지 않게.
돌파 가격(buy_stop) 매수는 그 자체가 새 신호라서 그대로 산다.
봇(코인·주식)과 백테스트가 같은 규칙을 쓴다.
"""

MAX_PCT = 50.0


def parse(value) -> tuple[float | None, str | None]:
    """웹페이지·설정에서 온 값 검사. (% 또는 None, 문제)."""
    try:
        pct = float(value or 0)
    except (TypeError, ValueError):
        return None, "손절선은 숫자로 넣어 주세요"
    if pct and not 1 <= pct <= MAX_PCT:
        return None, f"손절선은 1~{MAX_PCT:g}% 사이로 넣어 주세요 (안 쓰면 끄기)"
    return round(pct, 2), None


def line(cost: float, units: float, pct: float) -> float | None:
    """손절 가격: 1개(1코인·1주)를 산 값 × (1 − pct%). 안 쓰거나 들고 있지 않으면 None."""
    if not pct or units <= 0 or cost <= 0:
        return None
    return cost / units * (1 - pct / 100)


def text(pct: float) -> str:
    return f"손절선 −{pct:g}%"
