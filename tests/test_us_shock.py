"""美股重挫預警：一般重挫續抱、嚴重先賣後買回、嚴重事件在買回日前持續提醒（假資料，不連網）。"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.agents import us_shock


def _setup(monkeypatch, sox_moves, today, taiex_trend=1.0):
    idx = pd.bdate_range(end=pd.Timestamp(today) - pd.Timedelta(days=1), periods=120)
    px = 100 * np.cumprod(np.r_[np.full(len(idx) - len(sox_moves), 1.002), 1 + np.array(sox_moves)])
    sox = pd.DataFrame({"Close": px}, index=idx)
    tx = pd.DataFrame({"Close": 20000 * np.cumprod(np.full(len(idx), taiex_trend))}, index=idx)
    from src.agents import market_data, on_demand

    monkeypatch.setattr(market_data, "us_history", lambda *a, **k: {"^SOX": sox})
    monkeypatch.setattr(on_demand, "context", lambda: {"taiex": tx})
    monkeypatch.setattr(us_shock, "now_tw", lambda: dt.datetime.combine(today, dt.time(7, 0)))
    us_shock._CACHE.update(t=0.0, v=None)


TODAY = dt.date(2026, 11, 3)  # 週二


def test_normal(monkeypatch):
    _setup(monkeypatch, [0.001, -0.01], TODAY)
    assert us_shock.assess()["level"] == "正常"


def test_ordinary_shock_hold(monkeypatch):
    _setup(monkeypatch, [0.001, -0.035], TODAY)  # 單日 −3.5%、仍在 50 日線上
    a = us_shock.assess()
    assert a["level"] == "一般重挫" and not a["sell_now"] and "不要開盤殺低" in a["advice"]


def test_two_day_severe_sell_and_rebuy(monkeypatch):
    _setup(monkeypatch, [-0.035, -0.03], TODAY)  # 兩日合計約 −6.4%
    a = us_shock.assess()
    assert a["level"] == "嚴重" and a["sell_now"]
    assert a["rebuy_date"] > a["first_tw_day"]


def test_severe_persists_until_rebuy(monkeypatch):
    # 嚴重重挫隔晚費半反彈：買回日之前仍提醒
    _setup(monkeypatch, [-0.035, -0.03, 0.02], TODAY)
    assert us_shock.assess()["level"] == "嚴重"


@pytest.mark.parametrize("trend,level", [(0.998, "嚴重"), (1.002, "一般重挫")])
def test_trend_break_needs_taiex_below_ma20(monkeypatch, trend, level):
    # 費半長期走弱（跌破 50 日線）＋單日 −3.5%：加權在月線下才算嚴重
    _setup(monkeypatch, list(np.full(60, -0.004)) + [-0.035], TODAY, taiex_trend=trend)
    assert us_shock.assess()["level"] == level
