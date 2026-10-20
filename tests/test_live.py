"""即時資料：每檔報價快取、盤中建議失敗冷卻、即時 RSI（不連網）。"""
import pandas as pd

from src.agents import alerts
from src.api import data


def test_realtime_per_code_cache_only_fetches_missing(monkeypatch):
    calls = []

    def fake(codes):
        calls.append(list(codes))
        return {c: {"price": 10.0} for c in codes}

    monkeypatch.setattr(alerts, "_realtime_fetch", fake)
    monkeypatch.setattr(alerts, "_RT", {})
    assert set(alerts.realtime(["2330", "2317"])) == {"2330", "2317"}
    assert set(alerts.realtime(["2317", "2454"])) == {"2317", "2454"}  # 不同組合共用快取，只補抓 2454
    assert calls == [["2330", "2317"], ["2454"]]


def test_intraday_failure_backs_off(monkeypatch):
    """背景重算失敗後 2 分鐘內不再重開重算，並把錯誤回給前端。"""
    import threading
    import time

    started = []
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: started.append(1)})())
    monkeypatch.setattr(data, "_INTRA", {"slot": "old", "key": None, "v": {"time": "t0", "orders": []}})
    monkeypatch.setattr(data, "_INTRA_ERR", {"t": time.time(), "msg": "boom"})
    out = data.live_intraday()
    assert out["stale"] and "boom" in out["error"] and not started
    data._INTRA_ERR.update(t=0.0, msg=None)
    data.live_intraday()
    assert started


def test_live_rsi_drops_on_sharp_fall():
    close = pd.Series([100 + i * 0.5 for i in range(40)], dtype=float)
    hi = data._live_rsi(close, float(close.iloc[-1]) + 0.5)
    lo = data._live_rsi(close, float(close.iloc[-1]) * 0.9)
    assert hi > 90 and lo < hi


def test_fee_adjustment_row_reduces_cash_not_trade_count():
    from src.state import portfolio as pfm

    base = [{"date": "2026-10-05", "code": "2330", "side": "BUY", "shares": "1000", "price": "1000", "margin": "0"}]
    a = pfm.build_portfolio(base)
    b = pfm.build_portfolio(base + [{"date": "2026-10-05", "code": "FEE", "side": "ADJ", "shares": "1", "price": "163", "margin": "0"}])
    assert round(a.cash - b.cash) == 163 and a.trade_count == b.trade_count == 1


def test_notifier_sends_once_per_status_per_day(monkeypatch):
    from src.api import notifier
    from src.reports import emailer

    sent = []
    monkeypatch.setattr(alerts, "is_trading_hours", lambda: True)
    monkeypatch.setattr(notifier, "_events", lambda: [("3591|盡快賣出", "盡快賣出：3591 艾笛森", "跌破停損")])
    monkeypatch.setattr(notifier, "_risk_events", lambda: [])
    monkeypatch.setattr(notifier, "_close_events", lambda: [])
    monkeypatch.setattr(notifier, "_fin_events", lambda: [])
    monkeypatch.setattr(notifier, "_anomaly_events", lambda: [])
    monkeypatch.setattr(emailer, "send", lambda s, b, a: sent.append(s) or True)
    monkeypatch.setattr(notifier, "_sent", set())
    assert notifier.tick() == 1 and notifier.tick() == 0
    assert len(sent) == 1 and "艾笛森" in sent[0]


def test_live_candle_appended_only_when_missing(monkeypatch):
    from src.agents import on_demand

    q = {"price": 101.0, "open": 100.0, "high": 102.0, "low": 99.0, "volume_lots": 1234, "date": "20261006"}
    monkeypatch.setattr(alerts, "realtime", lambda codes: {"2330": q})
    old = [{"time": "2026-10-05", "close": 100.0}] * 60
    c = on_demand._live_candle("2330", old)
    assert c["time"] == "2026-10-06" and c["close"] == 101.0 and c["volume"] == 1234 and c["ma5"] == 100.2
    assert on_demand._live_candle("2330", old + [c]) is None


def test_notifier_dedupe_key_ignores_live_numbers():
    from src.api.notifier import _stable

    a = _stable("期貨權益數 752,640 元低於維持保證金 1,076,000 元（追繳）")
    b = _stable("期貨權益數 725,440 元低於維持保證金 1,076,000 元（追繳）")
    assert a == b  # 金額每分鐘變動不算新警示（2026-10-19 一天重寄 300 多封）
    assert _stable("2330 跌停 -9.9%") == _stable("2330 跌停 -10.0%")
