"""업비트 봉(일봉·분봉)을 DB에 모아 두기. 백테스트와 전략 계산에 쓴다."""

import logging
import time

from . import upbit
from .db import Database

log = logging.getLogger("mypie.history")


def sync_candles(db: Database, market: str, wanted: int, tf: str = "1d") -> dict:
    """완성된 봉을 DB에 저장하고(부족하면 과거로 거슬러 채움), 지금 진행 중인 봉을 돌려준다."""
    stored = db.candle_history(market, 1, tf)
    latest = upbit.get_candles(market, tf, 200)
    db.upsert_candles(market, latest[1:], tf)

    # 한동안 안 받았던 종목이면 저장된 마지막 봉과 방금 받은 봉 사이가 비어 있으니 거슬러 채운다
    edge = latest[-1]["day"] if latest else None
    while stored and edge and edge > stored[0]["day"]:
        older = upbit.get_candles(market, tf, 200, to=edge)
        if not older or older[-1]["day"] >= edge:
            break
        db.upsert_candles(market, older, tf)
        edge = older[-1]["day"]
        time.sleep(0.2)

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
