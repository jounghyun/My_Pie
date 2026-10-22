"""설정 파일(config.toml) 읽기. 없으면 config.example.toml을 쓴다."""

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "general": {"market": "KRW-BTC", "mode": "paper", "interval_sec": 10, "db_path": "data/mypie.db"},
    "strategy": {"name": "volatility_breakout"},
    "order": {"invest_ratio": 1.0, "min_order_krw": 5000},
    "paper": {"initial_krw": 1_000_000, "fee_rate": 0.0005},
    "backtest": {"days": 1095, "rank_by": "return"},
    "params": {},
    "web": {"host": "0.0.0.0", "port": 8000, "login": True, "code_digits": 2, "session_hours": 24, "trust_hours": 24},
    # 주식 봇 (토스증권 · 국내 주식). 수수료는 사고팔 때 모두, 증권거래세는 팔 때만 붙는다
    "stock": {"db_path": "data/stock.db", "mode": "paper", "paper_cash": 1_000_000, "fee_rate": 0.00015,
              "sell_tax": 0.002, "interval_sec": 10, "idle_sec": 60, "backtest_days": 1095, "rank_by": "return"},
}

def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("MYPIE_CONFIG")
    candidates = [Path(path)] if path else [ROOT / "config.toml", ROOT / "config.example.toml"]

    cfg = {section: dict(values) for section, values in DEFAULTS.items()}
    cfg["_path"] = None
    for candidate in candidates:
        if candidate.exists():
            with candidate.open("rb") as f:
                for section, values in tomllib.load(f).items():
                    cfg.setdefault(section, {}).update(values)
            cfg["_path"] = str(candidate)
            break

    for section in ("general", "stock"):
        db_path = Path(cfg[section]["db_path"])
        if not db_path.is_absolute():
            cfg[section]["db_path"] = str(ROOT / db_path)
    return cfg
