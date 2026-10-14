import datetime as dt

import pandas as pd

from src.agents import alerts, execution, shadow
from src.config import TW_TZ
from src.state import portfolio as pfm


def test_actual_breakdown_timing_vs_open(monkeypatch):
    idx = pd.DatetimeIndex(["2026-10-05"])
    panel = {"6213": pd.DataFrame({"Open": [690.0], "Close": [700.0]}, index=idx)}
    monkeypatch.setattr(pfm, "load_trades", lambda: [
        {"date": "2026-10-05", "code": "6213", "side": "BUY", "shares": "1000", "price": "726", "asset": "STOCK"},
        {"date": "2026-10-05", "code": "6213", "side": "BUY", "shares": "5", "price": "700", "asset": "STOCK"}])
    b = shadow.actual_breakdown(dt.date(2026, 10, 5), dt.date(2026, 10, 5), panel)
    assert b["timing"] == -(36 * 1000 + 10 * 5)          # 買在開盤價之上 → 負數
    assert b["fragment_fees"] < 0                         # 5 股零股付最低手續費 20 元


def test_open_limits_use_latest_close_before_open(monkeypatch):
    q = {"6196": {"price": 656.0, "prev_close": 641.0, "date": "20261014"}}
    monkeypatch.setattr(alerts, "realtime", lambda codes: q)
    lim = execution.open_limits([{"code": "6196", "action": "BUY", "shares": 2000, "limit_price": 661.0}],
                                dt.date(2026, 10, 15))[0]
    assert lim["prev_close"] == 656.0 and lim["open_limit"] == 675.0 and "開盤價成交" in lim["text"]
    odd = execution.open_limits([{"code": "6196", "action": "BUY", "shares": 158, "limit_price": 661.0}], dt.date(2026, 10, 15))[0]
    assert odd["odd"] and "09:10" in odd["text"]


def test_preopen_only_in_window():
    assert execution.preopen_alerts(dt.datetime(2026, 10, 15, 8, 20, tzinfo=TW_TZ)) == []
    assert execution.preopen_alerts(dt.datetime(2026, 10, 18, 8, 45, tzinfo=TW_TZ)) == []  # 週日
