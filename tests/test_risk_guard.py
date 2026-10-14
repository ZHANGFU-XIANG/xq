import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd

from src.agents import alerts, data_guard, risk_lab, trade_calendar
from src.config import TW_TZ


def _panel(last: str, n=30, close=100.0):
    idx = pd.bdate_range(end=last, periods=n)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1e6}, index=idx)


def test_data_guard_fills_missing_bar_and_fixes_close(monkeypatch):
    now = dt.datetime(2026, 10, 14, 16, 0, tzinfo=TW_TZ)
    panel = {"1111": _panel("2026-10-13"), "2222": _panel("2026-10-14", close=50.0)}
    q = {"1111": {"price": 105.0, "open": 101.0, "high": 106.0, "low": 100.0, "volume_lots": 10, "date": "20261014"},
         "2222": {"price": 55.0, "open": 50.0, "high": 55.0, "low": 50.0, "volume_lots": 5, "date": "20261014"}}
    monkeypatch.setattr(alerts, "realtime", lambda codes: {c: q[c] for c in codes if c in q})
    monkeypatch.setattr(alerts, "is_trading_hours", lambda: False)
    r = data_guard.check(panel, [], ["1111", "2222"], now=now)
    assert r["level"] == "corrected" and not r["held_missing"]
    assert panel["1111"].index[-1] == pd.Timestamp("2026-10-14") and panel["1111"].Close.iloc[-1] == 105.0
    assert panel["2222"].Close.iloc[-1] == 55.0 and r["corrections"]["2222"]["type"] == "收盤價校正"


def test_data_guard_severe_when_held_price_missing(monkeypatch):
    now = dt.datetime(2026, 10, 14, 16, 0, tzinfo=TW_TZ)
    panel = {"1111": _panel("2026-10-13")}
    monkeypatch.setattr(alerts, "realtime", lambda codes: {})
    monkeypatch.setattr(alerts, "is_trading_hours", lambda: False)
    r = data_guard.check(panel, [], ["1111"], now=now)
    assert r["level"] == "severe" and "勿依此下單" in data_guard.banner(r)


def test_margin_call_points(monkeypatch):
    from src.agents import futures_data

    monkeypatch.setattr(futures_data, "margins", lambda: {"TX": {"initial": 701_000, "maintenance": 538_000}})
    d = SimpleNamespace(kind="FUT", qty=2, multiplier=200, root="TX", avg_price=49875.0)
    pf = SimpleNamespace(derivs={"TX202610": d}, futures_equity=lambda prices: 1_568_640)
    m = risk_lab.margin_call(pf, {"TX202610": 48798.0})
    assert m["points_to_call"] == round((1_568_640 - 1_076_000) / 400) and m["call_level"] < 48798


def test_swan_backtest_needs_lagged_edge():
    idx = pd.bdate_range("2000-01-03", periods=800)
    rng = np.random.default_rng(1)
    c = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, len(idx)))
    tw = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c}, index=idx)
    hyg = pd.DataFrame({"Close": 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx)))}, index=idx)
    out = risk_lab.swan_backtest({"^TWII": tw, "HYG": hyg})
    b = out["高收益債 HYG 單日 ≤ −1.5%"]
    assert b["n"] > 5 and b["edge"] is False  # 隨機資料不該被判定有預測力


def test_roc_dates():
    assert trade_calendar.roc("1151015") == dt.date(2026, 10, 15)
    assert trade_calendar.roc("115/10/19") == dt.date(2026, 10, 19)
