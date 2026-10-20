"""SQLite 저장소: 봇 상태, 매매 기록, 자산 추이, 이벤트 로그.

봇이 쓰고 웹페이지가 읽는다. 두 프로세스가 동시에 써도 되도록 WAL 모드를 쓴다.
"""

import json
import sqlite3
from pathlib import Path

from .timeutil import ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    mode    TEXT NOT NULL,
    market  TEXT NOT NULL,
    side    TEXT NOT NULL,
    price   REAL NOT NULL,
    volume  REAL NOT NULL,
    krw     REAL NOT NULL,
    fee     REAL NOT NULL,
    profit  REAL,
    reason  TEXT
);
CREATE TABLE IF NOT EXISTS equity_log (
    account TEXT NOT NULL,   -- paper(모의투자) | live(실거래)
    ts      TEXT NOT NULL,
    price   REAL NOT NULL,
    equity  REAL NOT NULL,
    PRIMARY KEY (account, ts)
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candles (     -- 예전 버전(일봉만)의 표. 처음 켤 때 bars로 옮긴다
    market TEXT NOT NULL,
    day    TEXT NOT NULL,
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (market, day)
);
CREATE TABLE IF NOT EXISTS bars (
    market TEXT NOT NULL,
    tf     TEXT NOT NULL,   -- 1d | 240m | 60m | 15m
    day    TEXT NOT NULL,   -- 봉 키 (시작 시각, UTC): 일봉 2026-10-21, 분봉 2026-10-21T04:00
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (market, tf, day)
);
"""

MAX_EVENTS = 2000


class Database:
    def __init__(self, path, create: bool = True):
        self.path = Path(path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        if create:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            if not self.conn.execute("SELECT 1 FROM bars LIMIT 1").fetchone():
                with self.conn:  # 예전 일봉 표를 새 표로 옮기기
                    self.conn.execute("INSERT OR IGNORE INTO bars SELECT market, '1d', day, open, high, low, close, volume "
                                      "FROM candles")
        # WAL에서는 NORMAL이어도 안전하고, SD카드 쓰기가 줄어든다
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def close(self) -> None:
        self.conn.close()

    def get_state(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), ts()),
            )

    def delete_state(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    def upsert_candles(self, market: str, candles: list[dict], tf: str = "1d") -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO bars (market, tf, day, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(market, tf, c["day"], c["open"], c["high"], c["low"], c["close"], c.get("volume", 0))
                 for c in candles],
            )

    def candle_history(self, market: str, limit: int, tf: str = "1d") -> list[dict]:
        """완성된 봉 최근 limit개 (오래된 것 → 최신)."""
        rows = self._rows("SELECT day, open, high, low, close, volume FROM bars "
                          "WHERE market = ? AND tf = ? ORDER BY day DESC LIMIT ?", (market, tf, limit))
        return rows[::-1]

    def candle_range(self, market: str, tf: str = "1d") -> tuple[int, str | None]:
        row = self.conn.execute("SELECT COUNT(*) AS n, MIN(day) AS oldest FROM bars WHERE market = ? AND tf = ?",
                                (market, tf)).fetchone()
        return row["n"], row["oldest"]

    def add_trade(self, mode: str, market: str, trade: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO trades (ts, mode, market, side, price, volume, krw, fee, profit, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts(), mode, market, trade["side"], trade["price"], trade["volume"], trade["krw"],
                 trade["fee"], trade.get("profit"), trade.get("reason", "")),
            )

    def add_event(self, level: str, message: str) -> None:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (ts, level, message) VALUES (?, ?, ?)", (ts(), level, message)
            )
            self.conn.execute("DELETE FROM events WHERE id <= ?", (cur.lastrowid - MAX_EVENTS,))

    def add_equity(self, account: str, price: float, equity: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO equity_log (account, ts, price, equity) VALUES (?, ?, ?, ?)",
                (account, ts(), price, equity),
            )

    def trades(self, mode: str, market: str | None = None, since: str = "") -> list[dict]:
        """한 계좌(모의/실거래)의 매매 기록 (오래된 것 → 최신)."""
        if market:
            return self._rows("SELECT * FROM trades WHERE mode = ? AND market = ? AND ts >= ? ORDER BY id",
                              (mode, market, since))
        return self._rows("SELECT * FROM trades WHERE mode = ? AND ts >= ? ORDER BY id", (mode, since))

    def recent_trades(self, limit: int = 30) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def recent_events(self, limit: int = 30) -> list[dict]:
        return self._rows("SELECT ts, level, message FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def equity_since(self, account: str, since: str) -> list[dict]:
        return self._rows("SELECT ts, price, equity FROM equity_log WHERE account = ? AND ts >= ? ORDER BY ts",
                          (account, since))

    def _rows(self, sql: str, params: tuple) -> list[dict]:
        return [dict(row) for row in self.conn.execute(sql, params)]
