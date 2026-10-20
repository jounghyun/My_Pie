"""시간 관련 도우미. 업비트 일봉은 매일 09:00 KST(= 00:00 UTC)에 바뀐다."""

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_kst() -> datetime:
    return datetime.now(KST)


def ts(dt: datetime | None = None) -> str:
    """DB와 화면에 쓰는 KST 시각 문자열."""
    return (dt or now_kst()).astimezone(KST).strftime(TS_FORMAT)


def trading_day(now: datetime | None = None) -> str:
    """현재 거래일(YYYY-MM-DD). 09:00 KST에 다음 날로 넘어간다."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date().isoformat()
