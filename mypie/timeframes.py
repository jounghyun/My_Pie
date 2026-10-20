"""판단 주기(봉 단위). 줄마다 일봉·4시간봉·1시간봉·15분봉 중에서 고른다.

업비트 봉은 UTC 기준으로 나뉜다: 일봉은 00:00 UTC(= 09:00 KST)에, 4시간봉은 00·04·08·12·16·20시 UTC에 바뀐다.
봉 키(key)는 봉이 시작한 UTC 시각이다: 일봉 "2026-10-21", 분봉 "2026-10-21T04:00".
"""

from datetime import datetime, timedelta, timezone

TIMEFRAMES = {
    "1d":   {"label": "일봉 (하루)", "short": "일봉", "minutes": 1440, "bar": "일", "backtest_bars": None},
    "240m": {"label": "4시간봉", "short": "4시간", "minutes": 240, "bar": "봉", "backtest_bars": 1500},
    "60m":  {"label": "1시간봉", "short": "1시간", "minutes": 60, "bar": "봉", "backtest_bars": 2000},
    "15m":  {"label": "15분봉", "short": "15분", "minutes": 15, "bar": "봉", "backtest_bars": 2000},
}
DEFAULT = "1d"


def minutes(tf: str) -> int:
    return TIMEFRAMES[tf]["minutes"]


def bar_start(tf: str, now: datetime | None = None) -> datetime:
    """지금 진행 중인 봉이 시작한 시각 (UTC)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    step = minutes(tf)
    return midnight + timedelta(minutes=(now - midnight).seconds // 60 // step * step)


def bar_key(tf: str, now: datetime | None = None) -> str:
    """지금 진행 중인 봉의 키. 일봉이면 거래일(YYYY-MM-DD)과 같다."""
    start = bar_start(tf, now)
    return start.strftime("%Y-%m-%d") if tf == "1d" else start.strftime("%Y-%m-%dT%H:%M")


def next_bar(tf: str, now: datetime | None = None) -> datetime:
    """다음 봉이 시작하는 시각 (UTC) = 다음 판단 시각."""
    return bar_start(tf, now) + timedelta(minutes=minutes(tf))


def bars_per_year(tf: str) -> float:
    return 365 * 1440 / minutes(tf)


def key_to_utc(key: str) -> datetime:
    """봉 키 → 시작 시각 (UTC)."""
    fmt = "%Y-%m-%d" if len(key) == 10 else "%Y-%m-%dT%H:%M"
    return datetime.strptime(key, fmt).replace(tzinfo=timezone.utc)
