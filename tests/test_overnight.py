import datetime as dt

from src.agents import overnight as o
from src.config import TW_TZ


def _t(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=TW_TZ)


def test_windows():
    assert not o.in_window(_t(2026, 10, 14, 10))       # 盤中不算隔夜
    assert o.in_window(_t(2026, 10, 14, 16))
    assert o.in_window(_t(2026, 10, 15, 7))
    assert o.night_session_open(_t(2026, 10, 14, 20))   # 週三晚上
    assert o.night_session_open(_t(2026, 10, 15, 4, 30))
    assert not o.night_session_open(_t(2026, 10, 15, 6))
    assert not o.night_session_open(_t(2026, 10, 17, 20))  # 週六晚上沒有夜盤


def test_alert_levels():
    tx = {"chg_pct": -2.3, "change": -1100, "price": 47700, "ref": 48800}
    us = [{"ticker": "^SOX", "name": "費城半導體", "chg_pct": -3.5, "two_day_pct": -6.5, "price": 1, "prev_close": 1, "time": ""},
          {"ticker": "^VIX", "name": "VIX", "chg_pct": 20.0, "two_day_pct": 0, "price": 22, "prev_close": 18, "time": ""}]
    al = {a["key"]: a for a in o._alerts(tx, us, {"total": -300000}, 9_700_000)}
    assert al["tx-2"]["level"] == "嚴重" and "−3.09%" not in al["tx-2"]["body"] and "-300,000" in al["tx-2"]["body"]
    assert al["sox-severe"]["level"] == "嚴重" and "vix" in al
    assert o._alerts({"chg_pct": 0.3, "change": 150, "price": 1, "ref": 1}, [], {"total": 0}, None) == []
