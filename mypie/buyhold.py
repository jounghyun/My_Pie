"""'그냥 사서 들고 있었다면(바이앤홀드)?' 비교값. 코인 줄과 주식 종목 계좌가 같이 쓴다.

줄(종목 계좌)에 돈이 들어오면 그 돈으로 그 종목을 사서 계속 들고 있었다고 치고, 돈이 빠지면 그만큼 판 셈 친다.
그래서 줄 수익률과 같은 돈 흐름으로 비교된다. 수수료·세금은 빼지 않은 대략값이다.

  - hold:      바이앤홀드였다면 들고 있을 수량
  - hold_cash: 아직 사고팔지 않은 들어온(+)·나간(−) 돈. 가격을 아는 settle()에서 그때 가격으로 사고판다
"""


def add_flow(s: dict, amount: float) -> None:
    """줄에 돈이 들어오거나(+) 나간다(−): 넣은 돈 합계와 바이앤홀드 쪽 돈을 같이 바꾼다."""
    s["contrib"] += amount
    s["hold_cash"] = s.get("hold_cash", 0.0) + amount


def start(s: dict, first_price: float | None) -> None:
    """예전부터 있던 줄: 첫 매매 가격(없으면 지금 가격)에 넣은 돈 전부를 샀다고 치고 시작한다."""
    if first_price:
        s["hold"], s["hold_cash"] = s["contrib"] / first_price, 0.0
    else:
        s["hold"], s["hold_cash"] = 0.0, s["contrib"]


def settle(s: dict, price: float | None) -> float | None:
    """바이앤홀드였다면 지금 평가금액. 쌓인 돈 흐름을 지금 가격에 사고판 셈 친다."""
    if not price or price <= 0 or "hold" not in s:
        return None
    if s.get("hold_cash"):
        s["hold"] = max(s["hold"] + s["hold_cash"] / price, 0.0)
        s["hold_cash"] = 0.0
    return s["hold"] * price
