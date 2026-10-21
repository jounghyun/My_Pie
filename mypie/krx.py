"""한국 주식시장(KRX) 시간 규칙: 장 시간, 휴장일, 호가 단위.

정규장: 09:00 ~ 15:30 KST
  - 09:00 ~ 15:20  접속매매 (봇이 사고파는 시간)
  - 15:20 ~ 15:30  장 마감 동시호가 (주문은 받지만 체결은 15:30에 한 번) → 봇은 새 주문을 내지 않는다
주말과 휴장일에는 장이 안 열린다. 휴장일 목록은 아래에 적어 두고, 그래도 모르는 휴장일(임시 공휴일 등)은
봇이 장 시작 때 토스증권 장 달력으로 한 번 더 확인해서 쉰다 (stockbot.StockBot._market_open_today).
"""

from datetime import date, datetime, time, timedelta

from .timeutil import KST

OPEN = time(9, 0)
LAST_ORDER = time(15, 20)   # 이 시각부터는 마감 동시호가라 새 주문을 내지 않는다
CLOSE = time(15, 30)

# 한국거래소 휴장일 (주말 제외). 해마다 12월에 거래소가 다음 해 휴장일을 발표하면 더한다.
HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-02", "2026-05-01", "2026-05-05",
    "2026-05-25", "2026-06-03", "2026-08-17", "2026-09-24", "2026-09-25", "2026-10-05", "2026-10-09",
    "2026-12-25", "2026-12-31",
}


def is_trading_day(day: date, extra_holidays: set[str] | frozenset = frozenset()) -> bool:
    key = day.isoformat()
    return day.weekday() < 5 and key not in HOLIDAYS and key not in extra_holidays


def session(now: datetime, extra_holidays: set[str] | frozenset = frozenset()) -> str:
    """지금 장 상태: 'closed'(휴장일) | 'pre'(장 전) | 'open'(접속매매) | 'closing'(마감 동시호가) | 'after'(장 마감 뒤)."""
    now = now.astimezone(KST)
    if not is_trading_day(now.date(), extra_holidays):
        return "closed"
    t = now.time()
    if t < OPEN:
        return "pre"
    if t < LAST_ORDER:
        return "open"
    if t < CLOSE:
        return "closing"
    return "after"


def next_open(now: datetime, extra_holidays: set[str] | frozenset = frozenset()) -> datetime:
    """다음 장 시작 시각 (지금이 장 전이면 오늘 09:00)."""
    now = now.astimezone(KST)
    day = now.date()
    if is_trading_day(day, extra_holidays) and now.time() < OPEN:
        return datetime.combine(day, OPEN, KST)
    day += timedelta(days=1)
    while not is_trading_day(day, extra_holidays):
        day += timedelta(days=1)
    return datetime.combine(day, OPEN, KST)


def tick_size(price: float) -> int:
    """호가 단위 (2023년부터 코스피·코스닥 같음)."""
    for limit, tick in ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500)):
        if price < limit:
            return tick
    return 1_000


def round_tick(price: float, up: bool) -> int:
    """가격을 호가 단위에 맞춘다 (up이면 올림, 아니면 내림)."""
    tick = tick_size(price)
    steps = -(-price // tick) if up else price // tick
    return int(steps * tick)


def previous_trading_day(day: date, extra_holidays: set[str] | frozenset = frozenset()) -> date:
    """그날 바로 전 거래일 (일봉이 다 만들어진 마지막 날)."""
    day -= timedelta(days=1)
    while not is_trading_day(day, extra_holidays):
        day -= timedelta(days=1)
    return day
