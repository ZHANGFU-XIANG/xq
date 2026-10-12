"""當沖排行：盤中判斷規則、回測閘門、日線回測（全部用假資料，不連網）。"""
import datetime as dt

import numpy as np
import pandas as pd

from src.agents import daytrade

NOW = dt.datetime(2026, 10, 13, 11, 0)
BASE = {"code": "2603", "name": "長榮", "market": "上市", "short_ok": True, "is_etf": False, "close": 100.0,
        "atr": 3.0, "atr_pct": 3.0, "val20_e8": 20.0, "vol20_lots": 10000, "win60": 0.45, "oc_mean60": 0.1,
        "oh_mean60": 1.5, "trend": "站上月線", "base_score": 70.0, "cost_pct": 0.585, "data_date": "2026-10-12"}
EDGE = {"long": {"edge": True, "avg_net_pct": 0.2, "worst": {"win_rate": 0.5}, "best": {"win_rate": 0.55}},
        "short": {"edge": True, "avg_net_pct": 0.1, "worst": {"win_rate": 0.5}, "best": {"win_rate": 0.5}}}
NO_EDGE = {"long": {"edge": False, "avg_net_pct": -0.8, "worst": {"win_rate": 0.34}, "best": {"win_rate": 0.36}},
           "short": {"edge": False, "avg_net_pct": -0.7, "worst": {"win_rate": 0.38}, "best": {"win_rate": 0.4}}}


def q(price, op=100.0, hi=None, lo=None, prev=100.0, vol=8000, up=False, down=False):
    return {"price": price, "open": op, "high": hi or max(price, op), "low": lo or min(price, op), "prev_close": prev,
            "chg_pct": round((price / prev - 1) * 100, 2), "volume_lots": vol, "limit_up": 110.0, "limit_down": 90.0,
            "at_limit_up": up, "at_limit_down": down, "time": "11:00:00"}


def ev(quote, ph="盤中", bt=EDGE, base=BASE):
    return daytrade.evaluate(base, quote, ph, NOW, 10_000_000, 400_000, bt)


def test_long_signal_with_levels_and_size():
    r = ev(q(102.0, lo=99.5))  # 站上開盤與昨收、在區間上緣、量比 8000/(10000×120/270)=1.8
    assert r["verdict"] == "可以當沖做多" and r["direction"] == "long"
    assert r["stop"] < r["entry"] < r["target"] <= 109.5
    assert r["shares"] % 1000 == 0 and r["shares"] > 0
    assert r["profit_at_target"] > 0 > r["loss_at_stop"]
    # 觸停損的虧損不超過 risk_per_trade（含成本，取整張後只會更小）
    assert -r["loss_at_stop"] <= 10_000_000 * daytrade.cfg()["risk_per_trade"] + 1


def test_no_gate_by_default():
    assert ev(q(102.0, lo=99.5), bt=NO_EDGE)["verdict"] == "可以當沖做多"  # 使用者要求放開：回測沒優勢也照訊號給


def test_backtest_gate_when_enabled(monkeypatch):
    base = daytrade.cfg
    monkeypatch.setattr(daytrade, "cfg", lambda: {**base(), "require_edge": True})
    r = ev(q(102.0, lo=99.5), bt=NO_EDGE)
    assert r["verdict"] == "先不要當沖" and r["gated"] and r["signal"] == "做多訊號成立"
    assert "沒有優勢" in r["reasons"][0] and r["plan"].startswith("僅供參考")


def test_short_only_when_allowed():
    weak = q(97.5, hi=100.5)
    assert ev(weak)["verdict"] == "可以當沖做空"
    r = ev(weak, base={**BASE, "short_ok": False})
    assert r["verdict"] != "可以當沖做空" and r["direction"] is None


def test_limit_and_time_rules():
    assert ev(q(110.0, up=True))["verdict"] == "等打開再進" and ev(q(110.0, up=True))["plan"]
    assert ev(q(90.0, down=True))["verdict"] == "等打開再進"
    assert ev(q(102.0, lo=99.5), ph="收盤前")["verdict"] == "準備平倉"
    assert ev(q(102.0, lo=99.5), ph="盤前")["verdict"] == "開盤後觀察"
    assert ev(None, ph="盤中")["verdict"] == "開盤後觀察"


def test_near_limit_up_not_worth_it():
    r = ev(q(109.5, op=104.0, lo=103.5, prev=100.0))  # 目標被截在漲停前一檔＝進場價，扣成本後沒有空間
    assert r["verdict"] == "等回檔再進" and "離漲停太近" in r["reasons"][0] and r["trigger"] < 109.5 and r["plan"]


def test_always_recommend():
    """使用者要求一定要推薦：量很小、沒訊號也給突破價與計畫，不出現「先不要當沖」。"""
    r = ev(q(99.0, op=100.0, hi=100.2, lo=98.8, vol=500))
    assert r["verdict"] == "等突破再進" and r["trigger"] and r["plan"]


def test_wait_for_breakout_trigger():
    r = ev(q(99.8, op=100.0, hi=100.6, lo=99.0, vol=6000))
    assert r["verdict"] == "等突破再進" and r["trigger"] > 100.6


def test_signal_backtest_math():
    """單日假資料：做多進場價＝max(開盤, 昨收)×1.001，碰到目標就以 2 倍停損出場。"""
    idx = pd.to_datetime(["2026-10-01", "2026-10-02"])
    one = lambda a, b: pd.DataFrame({"X": [a, b]}, index=idx)  # noqa: E731
    f = {"O": one(100, 100), "H": one(100, 110), "L": one(100, 99.9), "C": one(100, 105),
         "atr_pct": one(2.0, 2.0), "cost": pd.Series({"X": 0.585})}
    worst, best, hit = daytrade._signal_returns(f, "long")
    assert bool(hit.loc[idx[1], "X"])
    # 停損 max(0.5×2%, 0.8%)=1%，目標 2%；低點 99.9 沒碰到停損 → 兩種假設都是 +2% − 成本
    assert abs(worst.loc[idx[1], "X"] - (2 - 0.585)) < 1e-9 and abs(best.loc[idx[1], "X"] - (2 - 0.585)) < 1e-9


def test_backtest_runs_on_random_panel(monkeypatch):
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2025-01-01", periods=420)
    panel = {}
    for i in range(30):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, len(idx))))
        o = c * (1 + rng.normal(0, 0.005, len(idx)))
        panel[f"{1100 + i}"] = pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.01, "Low": np.minimum(o, c) * 0.99,
                                             "Close": c, "Volume": 5e6}, index=idx)
    monkeypatch.setattr(daytrade, "_cost_pct", lambda code: 0.585)
    f = daytrade._features(panel, list(panel), idx[-1].date() + dt.timedelta(days=1), 400)
    bt = daytrade._backtest(f)
    assert bt and bt["long"] and bt["short"] and bt["open_close"]
    assert bt["long"]["worst"]["avg_net_pct"] <= bt["long"]["best"]["avg_net_pct"]


def test_board_price_cap(monkeypatch):
    rows = {c: {**BASE, "code": c, "close": p, "base_score": s} for c, p, s in
            [("A", 50.0, 90), ("B", 150.0, 80), ("C", 450.0, 70), ("D", 900.0, 60)]}
    monkeypatch.setattr(daytrade, "daily", lambda: {"ranked": list(rows), "rows": rows, "backtest": EDGE,
                                                    "target": "2026-10-13", "source": "測試"})
    monkeypatch.setattr(daytrade, "eligible", lambda: set(rows))
    monkeypatch.setattr(daytrade, "phase", lambda now: ("休市", ""))
    monkeypatch.setattr(daytrade, "_account", lambda rt: (10_000_000, 400_000))
    monkeypatch.setattr(daytrade.pfm, "build_portfolio", lambda: type("P", (), {"positions": {}})())
    monkeypatch.setattr(daytrade, "_BOARD", {})
    codes = lambda cap: [r["code"] for r in daytrade.board(max_price=cap)["rows"]]  # noqa: E731
    assert codes(None) == ["A", "B", "C", "D"]
    assert codes(100) == ["A"] and codes(200) == ["A", "B"] and codes(500) == ["A", "B", "C"]
    b = daytrade.board(max_price=200)
    assert b["max_price"] == 200 and b["n_in_range"] == 2 and [r["rank"] for r in b["rows"]] == [1, 2]
    assert b["verdict"].startswith("當沖推薦") and "A" in b["verdict"] and b["picks"][0]["code"] == "A"
