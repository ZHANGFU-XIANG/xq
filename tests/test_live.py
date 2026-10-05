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
