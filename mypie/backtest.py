"""백테스트: 과거 봉(일봉·분봉)으로 "이 전략을 썼다면?"을 계산해서 순위를 매긴다.

실행 규칙은 strategies/base.py 설명과 같고, 실제 봇(bot.py)과도 같다.
일봉만 보기 때문에 장중 체결가는 이렇게 가정한다:
  - buy_stop 돌파 매수: 시가가 이미 위면 시가, 아니면 buy_stop 가격
  - sell_stop 이탈 매도: 시가가 이미 아래면 시가, 아니면 sell_stop 가격
"""

from .strategies import STRATEGIES, Strategy

WINDOW = 150         # plan()에 넘기는 과거 일봉 수 (봇과 같음)
WARMUP = 60          # 앞부분은 지표 계산용으로만 쓰고, 모든 전략이 같은 날부터 시작한다
CURVE_POINTS = 80    # 화면에 그릴 자산 곡선 점 개수


# 전략 시뮬레이션에서 고를 수 있는 기간 (일, 이름). 판단 주기마다 봇이 모아 두는 봉 개수 안에서 고른다
SIM_PERIODS = {
    "1d":   [(30, "1달"), (90, "3달"), (180, "6달"), (365, "1년"), (1095, "3년")],
    "240m": [(7, "1주"), (30, "1달"), (90, "3달"), (180, "6달")],
    "60m":  [(7, "1주"), (14, "2주"), (30, "1달"), (60, "2달")],
    "15m":  [(1, "1일"), (3, "3일"), (7, "1주"), (14, "2주")],
}


def simulate(strategy: Strategy, candles: list[dict], fee_rate: float, warmup: int = WARMUP,
             bars_per_year: float = 365, detail: bool = False, sell_cost: float = 0.0) -> dict:
    """candles: 완성된 봉 (오래된 것 → 최신). 시작 자금 1로 계산한다. bars_per_year: 1년에 봉이 몇 개인지 (일봉 365).
    detail이면 줄이지 않은 자산 곡선과 매매 지점(fills: 몇 번째 봉에서 사고팔았나)도 돌려준다."""
    cash, coin, cost = 1.0, 0.0, 0.0
    trade_returns: list[float] = []
    curve: list[float] = []
    fills: list[dict] = []
    peak, mdd, days_holding = 1.0, 0.0, 0

    def buy(price: float) -> None:
        nonlocal cash, coin, cost
        coin, cost, cash = cash / ((1 + fee_rate) * price), cash, 0.0
        fills.append({"i": len(curve), "side": "buy", "price": price})

    def sell(price: float) -> None:
        nonlocal cash, coin, cost
        cash = coin * price * (1 - fee_rate - sell_cost)  # sell_cost: 주식의 증권거래세처럼 팔 때만 붙는 비용
        trade_returns.append(cash / cost - 1)
        fills.append({"i": len(curve), "side": "sell", "price": price, "return": cash / cost - 1})
        coin, cost = 0.0, 0.0

    for i in range(warmup, len(candles)):
        day = candles[i]
        plan = strategy.plan(candles[max(0, i - strategy.window):i], day["open"])
        bought = stopped = False

        if coin and plan.sell_at_open:
            sell(day["open"])
        elif not coin and plan.buy_at_open:
            buy(day["open"])
            bought = True

        if coin and not bought and plan.sell_stop is not None and day["low"] <= plan.sell_stop:
            sell(min(day["open"], plan.sell_stop))
            stopped = True
        if not coin and not bought and not stopped and plan.buy_stop is not None and day["high"] >= plan.buy_stop:
            buy(max(day["open"], plan.buy_stop))

        equity = cash + coin * day["close"]
        days_holding += 1 if coin else 0
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
        curve.append(equity)

    days = len(curve)
    final = curve[-1] if curve else 1.0
    years = days / bars_per_year
    step = max(1, days // CURVE_POINTS)
    result = {
        "total_return": final - 1,
        # 반년도 안 되는 기간(짧은 분봉)을 1년으로 늘려 계산하면 숫자가 터무니없어져서 비워 둔다
        "cagr": final ** (1 / years) - 1 if years >= 0.5 and final > 0 else None,
        "mdd": mdd,
        "trades": len(trade_returns),
        "win_rate": sum(r > 0 for r in trade_returns) / len(trade_returns) if trade_returns else None,
        "exposure": days_holding / days if days else 0.0,
        "curve": [round(v, 4) for v in curve[::step]] + ([round(final, 4)] if (days - 1) % step else []),
    }
    if detail:
        result["detail"] = {"equity": [round(v, 5) for v in curve], "fills": fills, "holding": bool(coin)}
    return result


def _score(result: dict, rank_by: str) -> float:
    if rank_by == "calmar":  # 최대 낙폭 대비 연평균 수익
        return (result["cagr"] or -1) / max(abs(result["mdd"]), 0.01)
    return result["total_return"]


def rank_strategies(candles: list[dict], fee_rate: float, rank_by: str = "return", bars_per_year: float = 365,
                    params: dict | None = None, warmup: int = WARMUP, detail: bool = False,
                    sell_cost: float = 0.0, strategies: dict | None = None) -> list[dict]:
    """모든 전략을 백테스트해서 순위대로 돌려준다. params: {전략이름: 파라미터}.
    strategies: 전략 목록 (기본은 코인·주식 공통 STRATEGIES, 주식은 STOCK_STRATEGIES)."""
    results = []
    for name, cls in (strategies or STRATEGIES).items():
        strategy = cls((params or {}).get(name))
        results.append({"name": name, "label": cls.label, "description": cls.description, "guide": cls.guide,
                        **simulate(strategy, candles, fee_rate, warmup, bars_per_year, detail, sell_cost)})
    results.sort(key=lambda r: _score(r, rank_by), reverse=True)
    for rank, r in enumerate(results, 1):
        r["rank"] = rank
    return results


def history_needed(strategies: dict | None = None) -> int:
    """전략들이 계획을 세우는 데 필요한 과거 봉 개수 중 가장 큰 것."""
    return max(cls.window for cls in (strategies or STRATEGIES).values())


def period_bars(tf_minutes: int, days: int) -> int:
    return days * 1440 // tf_minutes


def rank_period(candles: list[dict], bars: int, fee_rate: float, rank_by: str = "return",
                bars_per_year: float = 365, params: dict | None = None, sell_cost: float = 0.0,
                strategies: dict | None = None) -> list[dict]:
    """최근 bars개 봉 동안만 굴려 본 순위 (전략 시뮬레이션). 그 앞의 봉은 지표 계산에만 쓴다."""
    history = min(history_needed(strategies), len(candles) - bars)
    if history < WARMUP:
        raise ValueError("이 기간을 시험하기엔 모아 둔 봉이 모자라요")
    return rank_strategies(candles[-(bars + history):], fee_rate, rank_by, bars_per_year, params,
                           warmup=history, detail=True, sell_cost=sell_cost, strategies=strategies)
