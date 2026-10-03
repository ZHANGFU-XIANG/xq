"""期貨建議：回測門檻、模式、口數與風險上限、選擇權避險預算（不連網，行情與訊號用假資料）。"""
import datetime as dt

import pytest

from src.agents import futures
from src.agents import futures_data as fd
from src.state import portfolio as pfm

MARGINS = {"TX": {"initial": 701000, "maintenance": 538000}, "MTX": {"initial": 175250, "maintenance": 134500},
           "TMF": {"initial": 35050, "maintenance": 26900}}


def _sig(p_up=0.5, long_ok=False, short_ok=False, above=True):
    return {"date": "2026-10-02", "horizon_days": 5, "p_up": p_up, "close": 48000.0, "atr14": 600.0,
            "above_ma60": above, "dist_ma60_pct": 5.0 if above else -3.0, "long_enabled": long_ok,
            "short_enabled": short_ok, "gate_reasons": ["做多：x", "放空：y"],
            "drivers": [{"feature": "距季線", "push": "看多", "weight": 0.2}],
            "backtest": {"period": "2024～2026", "always_long": {"n": 100, "hit": 0.66, "avg_ret_pct": 0.8}}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(fd, "margins", lambda: MARGINS)
    monkeypatch.setattr(fd, "quote", lambda root, ym=None: {"price": 48000.0, "contract": f"{root}{ym}"})
    monkeypatch.setattr(fd, "put_chain", lambda ym: [{"strike": 46000.0, "contract": f"TXO{ym}P46000", "price": 100.0},
                                                     {"strike": 47000.0, "contract": f"TXO{ym}P47000", "price": 180.0}])
    monkeypatch.setattr(pfm, "load_history", lambda: [])
    return monkeypatch


def _plan(env, sig, nav=10_000_000, alloc=None, beta=0.0, us=None):
    env.setattr(futures, "signal", lambda target=None: sig)
    return futures.plan(pfm.build_portfolio([]), {}, nav, alloc, beta, dt.date(2026, 10, 5), us)


def test_no_edge_no_trade(env):
    r = _plan(env, _sig(p_up=0.7))
    assert r["orders"] == [] and any("沒有足夠的擇時優勢" in x for x in r["reasons"])


def test_gated_long_sizes_within_risk_and_margin(env):
    r = _plan(env, _sig(p_up=0.7, long_ok=True))
    o = r["orders"][0]
    assert o["action"] == "BUY" and o["max_loss"] <= 10_000_000 * 0.01 + 1
    assert o["margin"] <= 10_000_000 * 0.06 and r["reserve"] >= o["margin"] * 2.5


def test_recovery_overlay_only_above_ma60(env):
    r = _plan(env, _sig(), nav=9_400_000)
    assert r["mode"] == "補回虧損" and r["orders"][0]["mode"].startswith("補回虧損")
    assert "不是預測優勢" in r["orders"][0]["why"][0]
    r2 = _plan(env, _sig(above=False), nav=9_400_000)
    assert r2["orders"] == [] and any("季線下" in x for x in r2["reasons"])


def test_contract_choice():
    root, qty, _ = futures._contract_qty(100_000, 1200, MARGINS, 1e9)
    assert (root, qty) == ("TMF", 8)
    root, qty, _ = futures._contract_qty(300_000, 1200, MARGINS, 1e9)
    assert (root, qty) == ("MTX", 5)
    root, qty, notes = futures._contract_qty(300_000, 1200, MARGINS, 400_000)
    assert qty == 2 and any("保證金上限" in n for n in notes)


def test_put_hedge_budget_and_trigger(env):
    alloc = {"taiex": {"rsi14": 80, "dist_ma60_pct": 4}}
    r = _plan(env, _sig(), alloc=alloc, beta=20_000_000)
    h = r["hedge"]
    assert h and h["strike"] == 46000.0 and h["premium"] <= 10_000_000 * 0.005
    assert _plan(env, _sig(), alloc={"taiex": {"rsi14": 60, "dist_ma60_pct": 2}}, beta=20_000_000)["hedge"] is None
    assert _plan(env, _sig(), alloc=alloc, beta=0)["hedge"] is None  # 沒有股票部位不需要避險


def test_settlement_and_front_month():
    assert fd.settlement_date("202610") == dt.date(2026, 10, 21)
    assert fd.front_month(dt.date(2026, 10, 22)) == "202611"
    assert fd.front_month(dt.date(2026, 10, 19), min_days=3) == "202611"
