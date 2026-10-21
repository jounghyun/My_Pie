"""전략 목록. 새 전략을 만들면 STRATEGIES에 추가한다."""

from .base import DayPlan, SignalStrategy, Strategy
from .breakout import Turtle, VolatilityBreakout, VolatilityBreakoutMA
from .popular import SmartMoney, SqueezeMomentum, SuperTrend
from .signals import Bollinger, BuyAndHold, Macd, MaCross, Momentum, Rsi, Stochastic
from .stock import Disparity, MaAlignment, NewHigh52, VolumeBreakout

STRATEGIES: dict[str, type[Strategy]] = {  # 코인·주식 공통
    cls.name: cls
    for cls in (VolatilityBreakout, VolatilityBreakoutMA, MaCross, Rsi, Bollinger,
                Macd, Turtle, Momentum, Stochastic, SqueezeMomentum, SmartMoney, SuperTrend, BuyAndHold)
}

# 주식 봇이 쓰는 목록: 공통 전략 + 주식 전용 전략 (바이앤홀드는 비교 기준이라 맨 끝)
STOCK_STRATEGIES: dict[str, type[Strategy]] = {
    **{k: v for k, v in STRATEGIES.items() if k != BuyAndHold.name},
    **{cls.name: cls for cls in (NewHigh52, VolumeBreakout, Disparity, MaAlignment)},
    BuyAndHold.name: BuyAndHold,
}


def create_strategy(name: str, params: dict | None = None, registry: dict | None = None) -> Strategy:
    registry = registry or STRATEGIES
    try:
        cls = registry[name]
    except KeyError:
        raise ValueError(f"알 수 없는 전략: {name} (사용 가능: {', '.join(registry)})") from None
    return cls(params)


def strategy_params(cfg: dict, name: str) -> dict:
    """config.toml의 [params.<전략이름>] 값. 예전 형식([strategy]에 k 등을 적은 것)도 받아준다."""
    params = {}
    if cfg.get("strategy", {}).get("name") == name:
        params.update({k: v for k, v in cfg["strategy"].items() if k != "name"})
    params.update(cfg.get("params", {}).get(name, {}))
    return params


__all__ = ["DayPlan", "SignalStrategy", "Strategy", "STOCK_STRATEGIES", "STRATEGIES", "create_strategy", "strategy_params"]
