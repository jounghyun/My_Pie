"""전략 목록. 새 전략을 만들면 STRATEGIES에 추가한다."""

from .base import DayPlan, SignalStrategy, Strategy
from .breakout import Turtle, VolatilityBreakout, VolatilityBreakoutMA
from .popular import SmartMoney, SqueezeMomentum, SuperTrend
from .signals import Bollinger, BuyAndHold, Macd, MaCross, Momentum, Rsi, Stochastic

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (VolatilityBreakout, VolatilityBreakoutMA, MaCross, Rsi, Bollinger,
                Macd, Turtle, Momentum, Stochastic, SqueezeMomentum, SmartMoney, SuperTrend, BuyAndHold)
}


def create_strategy(name: str, params: dict | None = None) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"알 수 없는 전략: {name} (사용 가능: {', '.join(STRATEGIES)})") from None
    return cls(params)


def strategy_params(cfg: dict, name: str) -> dict:
    """config.toml의 [params.<전략이름>] 값. 예전 형식([strategy]에 k 등을 적은 것)도 받아준다."""
    params = {}
    if cfg.get("strategy", {}).get("name") == name:
        params.update({k: v for k, v in cfg["strategy"].items() if k != "name"})
    params.update(cfg.get("params", {}).get(name, {}))
    return params


__all__ = ["DayPlan", "SignalStrategy", "Strategy", "STRATEGIES", "create_strategy", "strategy_params"]
