"""設定載入與共用路徑。"""
from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
FACTS_DIR = DATA_DIR / "facts"
PDF_DIR = DATA_DIR / "pdfs"
REPORT_DIR = ROOT / "reports"

TW_TZ = dt.timezone(dt.timedelta(hours=8))


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_dotenv()


@lru_cache
def rules() -> dict:
    return yaml.safe_load((CONFIG_DIR / "rules.yaml").read_text(encoding="utf-8"))


@lru_cache
def settings() -> dict:
    return yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))


def watchlist() -> list[dict]:
    return settings()["watchlist"]


def stock_meta(code: str) -> dict:
    """名稱／產業／市場／美股驅動因子（全市場，資料來自 UniverseAgent）。"""
    from src.agents.universe import meta  # 延遲匯入避免循環

    return meta(code)


def now_tw() -> dt.datetime:
    return dt.datetime.now(TW_TZ)


def holidays() -> set[dt.date]:
    return {d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)) for d in rules()["holidays"]}


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in holidays()


def next_trading_day(d: dt.date, include_today: bool = False) -> dt.date:
    cur = d if include_today else d + dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur += dt.timedelta(days=1)
    return cur
