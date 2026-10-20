"""업비트 봉(일봉·분봉)을 DB에 모아 두기. 백테스트와 전략 계산에 쓴다."""

import logging
import time

from . import upbit
from .db import Database

log = logging.getLogger("mypie.history")


def sync_candles(db: Database, market: str, wanted: int, tf: str = "1d") -> dict:
    """완성된 봉을 DB에 저장하고(부족하면 과거로 거슬러 채움), 지금 진행 중인 봉을 돌려준다."""
    latest = upbit.get_candles(market, tf, 200)
    db.upsert_candles(market, latest[1:], tf)

    count, oldest = db.candle_range(market, tf)
    while count < wanted and oldest:
        older = upbit.get_candles(market, tf, 200, to=oldest)
        if not older:
            break  # 상장일까지 다 받음
        db.upsert_candles(market, older, tf)
        count, new_oldest = db.candle_range(market, tf)
        if new_oldest == oldest:
            break
        oldest = new_oldest
        log.info("%s %s 과거 봉 받는 중... %d개 (%s부터)", market, tf, count, oldest)
        time.sleep(0.2)  # 업비트 요청 제한 배려
    return latest[0]
