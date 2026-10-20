"""업비트 일봉을 DB에 모아 두기. 백테스트와 전략 계산에 쓴다."""

import logging
import time

from . import upbit
from .db import Database

log = logging.getLogger("mypie.history")


def sync_candles(db: Database, market: str, days_wanted: int) -> dict:
    """완성된 일봉을 DB에 저장하고(부족하면 과거로 거슬러 채움), 오늘(진행 중) 일봉을 돌려준다."""
    latest = upbit.get_daily_candles(market, 200)
    db.upsert_candles(market, latest[1:])

    count, oldest = db.candle_range(market)
    while count < days_wanted and oldest:
        older = upbit.get_daily_candles(market, 200, to=oldest)
        if not older:
            break  # 상장일까지 다 받음
        db.upsert_candles(market, older)
        count, new_oldest = db.candle_range(market)
        if new_oldest == oldest:
            break
        oldest = new_oldest
        log.info("과거 일봉 받는 중... %d일치 (%s부터)", count, oldest)
        time.sleep(0.2)  # 업비트 요청 제한 배려
    return latest[0]
