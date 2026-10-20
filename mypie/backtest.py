"""백테스트: 과거 일봉으로 "이 전략을 썼다면?"을 계산해서 순위를 매긴다.

실행 규칙은 strategies/base.py 설명과 같고, 실제 봇(bot.py)과도 같다.
일봉만 보기 때문에 장중 체결가는 이렇게 가정한다:
  - buy_stop 돌파 매수: 시가가 이미 위면 시가, 아니면 buy_stop 가격
  - sell_stop 이탈 매도: 시가가 이미 아래면 시가, 아니면 sell_stop 가격
"""

from .strategies import STRATEGIES, Strategy, create_strategy

WINDOW = 150         # plan()에 넘기는 과거 일봉 수 (봇과 같음)
WARMUP = 60          # 앞부분은 지표 계산용으로만 쓰고, 모든 전략이 같은 날부터 시작한다
CURVE_POINTS = 80    # 화면에 그릴 자산 곡선 점 개수


def simulate(strategy: Strategy, candles: list[dict], fee_rate: float, warmup: int = WARMUP) -> dict:
    """candles: 완성된 일봉 (오래된 것 → 최신). 시작 자금 1로 계산한다."""
    cash, coin, cost = 1.0, 0.0, 0.0
    trade_returns: list[float] = []
    curve: list[float] = []
    peak, mdd, days_holding = 1.0, 0.0, 0

    def buy(price: float) -> None:
        nonlocal cash, coin, cost
        coin, cost, cash = cash / ((1 + fee_rate) * price), cash, 0.0

    def sell(price: float) -> None:
        nonlocal cash, coin, cost
        cash = coin * price * (1 - fee_rate)
        trade_returns.append(cash / cost - 1)
        coin, cost = 0.0, 0.0

    for i in range(warmup, len(candles)):
        day = candles[i]
        plan = strategy.plan(candles[max(0, i - WINDOW):i], day["open"])
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
    years = days / 365
    step = max(1, days // CURVE_POINTS)
    return {
        "total_return": final - 1,
        "cagr": final ** (1 / years) - 1 if years > 0 and final > 0 else None,
        "mdd": mdd,
        "trades": len(trade_returns),
        "win_rate": sum(r > 0 for r in trade_returns) / len(trade_returns) if trade_returns else None,
        "exposure": days_holding / days if days else 0.0,
        "curve": [round(v, 4) for v in curve[::step]] + ([round(final, 4)] if (days - 1) % step else []),
    }


def _score(result: dict, rank_by: str) -> float:
    if rank_by == "calmar":  # 최대 낙폭 대비 연평균 수익
        return (result["cagr"] or -1) / max(abs(result["mdd"]), 0.01)
    return result["total_return"]


def rank_strategies(candles: list[dict], fee_rate: float, rank_by: str = "return",
                    params: dict | None = None) -> list[dict]:
    """모든 전략을 백테스트해서 순위대로 돌려준다. params: {전략이름: 파라미터}."""
    results = []
    for name, cls in STRATEGIES.items():
        strategy = create_strategy(name, (params or {}).get(name))
        results.append({"name": name, "label": cls.label, "description": cls.description, "guide": cls.guide,
                        **simulate(strategy, candles, fee_rate)})
    results.sort(key=lambda r: _score(r, rank_by), reverse=True)
    for rank, r in enumerate(results, 1):
        r["rank"] = rank
    return results
