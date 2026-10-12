"""新 Agent 的單元測試：一個月模型標籤、漲跌歸因、虧損補回、國際因子、調整 Agent。"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.agents import alpha_model, attribution, macro, recovery, tuner
from src.state import params
from src.state.portfolio import build_portfolio


def _df(closes, start="2025-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6})


def test_newhigh_label_uses_only_future_window():
    # 前 300 日平盤 100，之後第 305 日跳到 120 → 只有 t 在 [285, 304] 時未來 20 日內會創新高
    closes = [100.0] * 305 + [120.0] * 30
    df = _df(closes)
    mkt = df.Close
    f = alpha_model.stock_features(df, mkt, horizon=20, hw=252, is_etf=False)
    y = f["y_newhigh"]
    assert y.iloc[284] == 0 and y.iloc[285] == 1 and y.iloc[304] == 1
    assert np.isnan(y.iloc[-1])  # 最後 20 日沒有完整未來資料 → 無標籤
    # 特徵不得使用未來：dist_hi 在 t=304 應為 100/101-1
    assert f["dist_hi"].iloc[304] == pytest.approx(100 / 101 - 1)


def test_attribution_parts_sum_to_return():
    n = 200
    rng = np.random.default_rng(1)
    mk = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    taiex = _df(mk)
    panel = {}
    for i, code in enumerate(["2330", "2303", "2454", "3711"]):
        panel[code] = _df(50 * np.cumprod(1 + rng.normal(0, 0.02, n)))
    target = taiex.index[-1] + pd.Timedelta(days=1)
    att = attribution.attribute({"2330": 1000}, panel, taiex, list(panel), target, {}, {})
    r = att["rows"][0]
    assert r["mkt_pct"] + r["ind_pct"] + r["idio_pct"] == pytest.approx(r["ret_pct"], abs=0.02)
    assert r["pnl"] == pytest.approx(1000 * (r["close"] - r["prev_close"]), abs=10)  # 價格顯示已取到 2 位小數


def _ranked_alt(exp_ret, p):
    return [{"code": "9999", "name": "替代股", "signal": "買進候選", "score": 0.8,
             "alpha": {"exp_ret": exp_ret, "p_newhigh": p}}]


def test_recovery_swaps_only_when_benefit_exceeds_cost():
    pf = build_portfolio([{"date": "d", "code": "2303", "side": "BUY", "shares": "1000", "price": "100", "margin": "0"}])
    prices = {"2303": 90.0}
    a = {"2303": {"exp_ret": 0.01, "p_newhigh": 0.2, "combo": 0.5}}
    scores = {"2303": {"score": 0.1, "name": "聯電", "features": {"close": 90, "ma20": 95}}}
    meta = {"2303": {"stop": 80}}
    res = recovery.plan(pf, prices, scores, a, meta, _ranked_alt(0.10, 0.6), 10_000_000, 10_000_000, {})
    assert res["actions"]["2303"]["decision"] == "換股"
    # 替代標的優勢不足（扣成本 0.585% 後 < swap_margin 1%）→ 續抱
    res = recovery.plan(pf, prices, scores, a, meta, _ranked_alt(0.02, 0.6), 10_000_000, 10_000_000, {})
    assert "2303" not in res["actions"]
    # 跌破停損 → 停損換股
    res = recovery.plan(pf, {"2303": 79.0}, scores, a, meta, _ranked_alt(0.03, 0.6), 10_000_000, 10_000_000, {})
    assert res["actions"]["2303"]["decision"] == "停損換股"



def test_recovery_no_swap_within_min_hold_days():
    """剛買進（未滿 min_hold_days 個交易日）不因換股而賣；跌破停損仍出場。"""
    from src.config import now_tw

    today = now_tw().date().isoformat()
    pf = build_portfolio([{"date": today, "code": "2303", "side": "BUY", "shares": "1000", "price": "100", "margin": "0"}])
    a = {"2303": {"exp_ret": 0.01, "p_newhigh": 0.2, "combo": 0.5}}
    scores = {"2303": {"score": 0.1, "name": "聯電", "features": {"close": 90, "ma20": 95}}}
    meta = {"2303": {"stop": 80}}
    res = recovery.plan(pf, {"2303": 90.0}, scores, a, meta, _ranked_alt(0.10, 0.6), 10_000_000, 10_000_000, {})
    assert "2303" not in res["actions"]
    res = recovery.plan(pf, {"2303": 79.0}, scores, a, meta, _ranked_alt(0.10, 0.6), 10_000_000, 10_000_000, {})
    assert res["actions"]["2303"]["decision"] == "停損換股"


def test_macro_ignores_insignificant_correlation():
    ev = [{"event": "油價下跌", "factor": "OIL", "direction": -1, "horizon": "短期", "news": []}]
    sens = {"1303": {"OIL": {"beta": 0.5, "sigma": 0.02, "impact_1sd_pct": 1.0, "corr": 0.30}},
            "2330": {"OIL": {"beta": 0.5, "sigma": 0.02, "impact_1sd_pct": 1.0, "corr": 0.05}}}
    out = macro.impacts(ev, {"1303": 1_000_000, "2330": 1_000_000}, sens, 10_000_000, {}, {})[0]
    rows = {r["code"]: r for r in out["rows"]}
    assert rows["1303"]["impact_pct"] == -1.0 and rows["1303"]["amount"] == -10_000
    assert rows["2330"]["amount"] == 0 and "未達統計顯著" in rows["2330"]["decision"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tuner, "LOG_CSV", tmp_path / "log.csv")
    monkeypatch.setattr(tuner, "TUNE_LOG", tmp_path / "tune.json")
    monkeypatch.setattr(params, "LEARNED", tmp_path / "learned.yaml")
    monkeypatch.setattr(tuner, "load_history", lambda: [{"date": "2026-10-05", "nav": 10_000_000},
                                                        {"date": "2026-10-06", "nav": 9_000_000}])
    return tmp_path


def test_tuner_detects_drawdown_and_reduces_risk_then_rollback(isolated):
    issues = tuner.health({"overnight": [], "alpha": []})
    assert any(i["type"] == "drawdown" for i in issues)
    before = params.strategy()["risk_per_trade"]
    changes = tuner.tune_risk(issues)
    tuner.apply(changes, issues, dt.date(2026, 10, 6))
    after = params.strategy()["risk_per_trade"]
    assert after == pytest.approx(before * 0.8)
    assert params.strategy()["max_weight"] < 0.25
    assert tuner.rollback() == "2026-10-06"
    assert params.strategy()["risk_per_trade"] == before


def test_tuner_flags_overnight_failures(isolated):
    over = [{"code": "TAIEX", "kind": "gap", "target_date": f"2026-10-{d:02d}", "pred": "0.5", "real": -0.6,
             "hit": False, "err_bands": 1.0} for d in range(5, 15)]
    issues = tuner.health({"overnight": over, "alpha": []})
    assert any(i["type"] == "overnight" for i in issues)


def test_tuner_weight_shift_bounded(isolated):
    rng = np.random.default_rng(0)
    alpha = []
    for i in range(40):
        good = rng.normal()
        alpha.append({"components": {"alpha": good, "newhigh": rng.normal(), "institutional": rng.normal(),
                                     "revenue": rng.normal(), "overnight": -good, "sentiment": rng.normal()},
                      "real_excess": good * 0.05, "target_date": f"2025-{1 + i // 4:02d}-{1 + (i % 4) * 7:02d}"})  # 10 個月、不重疊期間足夠
    ch = tuner.tune_weights({"alpha": alpha})
    assert ch and ch["after"]["alpha"] > ch["before"]["alpha"] and ch["after"]["overnight"] < ch["before"]["overnight"]
    assert all(0.0 < v <= 0.5 for v in ch["after"].values())


def _taiex_series(rets):
    idx = pd.bdate_range("2025-01-01", periods=len(rets))
    c = pd.Series(100 * np.cumprod(1 + np.array(rets)), index=idx)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1.0})


def test_allocation_reacts_to_regime_drawdown_and_endgame():
    from src.agents import allocation
    calm = _taiex_series([0.0005, -0.0004] * 60)
    t = calm.index[-1] + pd.Timedelta(days=1)
    base = allocation.decide(calm, t, [], 10_000_000, 10_000_000, 30, [])
    assert base["stock_split"]["offense"] == 0.60 and base["etf"] == 0.15
    assert abs(base["offense"] + base["defense"] + base["etf"] - 1) < 0.01  # 三籃合計 100%
    hot = _taiex_series([0.0] * 80 + [0.01] * 25)  # 連漲：2026-10-06 起依回測改為綠燈加攻（舊「過熱就減碼」規則回測無益已移除）
    r = allocation.decide(hot, hot.index[-1] + pd.Timedelta(days=1), [], 10_000_000, 10_000_000, 30, [])
    assert r["stock_split"]["offense"] == 0.85 and any("綠燈" in x for x in r["reasons"])
    assert r["etf"] == 0.10 and any("綠燈" in x for x in r["etf_reasons"])  # 強勢時資金優先給進攻個股
    hist = [{"date": "d", "nav": 10_500_000}]
    r = allocation.decide(calm, t, hist, 10_000_000, 9_600_000, 30, [])  # 回撤 8.6%
    assert r["stock_split"]["offense"] == 0.40 and r["etf"] == 0.25
    r = allocation.decide(calm, t, [], 10_000_000, 10_300_000, 10, [])  # 尾聲且為正報酬 → 保住晉級
    assert r["stock_split"]["offense"] == 0.50 and r["etf"] == 0.25
    r = allocation.decide(calm, t, [], 10_000_000, 9_900_000, 10, [])  # 尾聲但虧損 → 爭取轉正
    assert r["stock_split"]["offense"] == 0.70 and r["etf"] == 0.15


def test_overheated_stock_not_bought_in_offense():
    from src.agents import strategy
    s = {"code": "1", "score": 0.9, "risks": [], "eligible_def": False, "def_score": 0,
         "alpha": {"features": {"rsi14": 90, "r20": 0.1}}}
    strategy.rank([s])
    assert s["signal"] == "過熱不追"


def test_tuning_log_dedupes_no_change_checks(tmp_path, monkeypatch):
    from src.agents import tuner

    monkeypatch.setattr(tuner, "TUNE_LOG", tmp_path / "log.json")
    tuner._append_log({"date": "2026-10-03", "issues": [], "changes": [], "note": "無需調整"})
    tuner._append_log({"date": "2026-10-03", "issues": [], "changes": [{"param": "x", "before": 1, "after": 2}], "note": "改"})
    tuner._append_log({"date": "2026-10-03", "issues": [], "changes": [], "note": "無需調整"})
    tuner._append_log({"date": "2026-10-04", "issues": [], "changes": [], "note": "無需調整"})
    tuner._append_log({"date": "2026-10-04", "issues": [], "changes": [], "note": "無需調整"})
    h = tuner.history()
    assert [(e["date"], bool(e["changes"])) for e in h] == [("2026-10-03", False), ("2026-10-03", True), ("2026-10-04", False)]
    assert h[1]["checks"] == 2 and h[2]["checks"] == 2


def test_weekly_report_collect_and_build(monkeypatch, tmp_path):
    import datetime as dt

    import pandas as pd

    from src.agents import market_data, weekly
    from src.reports import weekly_pptx
    from src.state import portfolio as pfm

    idx = pd.to_datetime(["2026-09-25", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"])
    tx = pd.DataFrame({"Close": [100.0, 99.0, 100.0, 101.0, 102.0]}, index=idx)
    monkeypatch.setattr(market_data, "taiex", lambda years=1: tx)
    monkeypatch.setattr(weekly, "_close_on", lambda code, d: 50.0)
    monkeypatch.setattr(weekly, "_facts_for", lambda d: [])
    monkeypatch.setattr(pfm, "load_trades", lambda: [
        {"date": "2026-09-30", "code": "2886", "side": "BUY", "shares": "1000", "price": "48", "margin": "0", "note": "看好金融股殖利率"},
        {"date": "2026-10-01", "code": "2886", "side": "SELL", "shares": "400", "price": "49", "margin": "0", "note": ""}])
    w = weekly.collect(dt.date(2026, 10, 1))
    assert w["monday"] == "2026-09-28" and not w["daily"][0]["trading"]  # 9/28 加權沒有資料 → 休市
    assert [t["reason"]["source"] for t in w["trades"]] == ["記錄時的備註", "不在系統建議中（自行判斷）"]
    assert w["taiex_week_pct"] == 2.0 and w["trade_count_week"] == 2
    text = weekly._template(w, {})
    out = weekly_pptx.build(w, text, False, tmp_path / "w.pptx")
    from pptx import Presentation

    assert out.exists() and len(Presentation(str(out)).slides) >= 8


def test_tuner_independent_periods_block_overlapping_samples():
    """60 筆連續日期的重疊樣本只算約 3 個不重疊期間，不足 allocation_min_periods → 不調攻守比例。"""
    import datetime as dt

    from src.agents import tuner

    start = dt.date(2026, 7, 1)
    alpha = []
    for i in range(60):
        d = (start + dt.timedelta(days=i)).isoformat()
        alpha += [{"target_date": d, "basket": "offense", "real_ret": 0.08}, {"target_date": d, "basket": "defense", "real_ret": 0.02}]
    periods = tuner.independent_periods(alpha, horizon=20)
    assert 2 <= len(periods) <= 3
    assert tuner.tune_allocation({"alpha": alpha}) is None


def test_tuner_needs_significance_and_consistent_halves():
    import datetime as dt

    from src.agents import tuner

    def make(diffs):
        out = []
        for i, df in enumerate(diffs):
            d = (dt.date(2025, 1, 1) + dt.timedelta(days=30 * i)).isoformat()
            out += [{"target_date": d, "basket": "offense", "real_ret": 0.02 + df}, {"target_date": d, "basket": "defense", "real_ret": 0.02}]
        return out
    assert tuner.tune_allocation({"alpha": make([0.05, -0.04, 0.06, -0.05, 0.04, -0.03, 0.05, -0.02])}) is None  # 不顯著
    ch = tuner.tune_allocation({"alpha": make([0.05, 0.04, 0.06, 0.05, 0.045, 0.055, 0.05, 0.04])})
    assert ch and ch["after"] == ch["before"] + 0.05 and "不重疊期間" in ch["reason"]


def test_trim_small_odd_lot():
    from src.agents import sizer

    assert sizer.trim_odd(2, 300, 26.0)[:2] == (2, 0)          # 300 股 × 26 = 7,800 < 14,035 → 不買零股
    assert sizer.trim_odd(2, 300, 100.0)[:2] == (2, 300)       # 30,000 元的零股保留
    assert sizer.trim_odd(0, 8, 2330.0)[:2] == (0, 8)          # 沒有整張時零股是唯一買法，保留


# ---- 結單轉股（傳入綜合排名 alts）
def _alt(score_pct, code="9999", price=50.0):
    return [{"code": code, "name": "替代股", "score": score_pct, "p_up": 0.6, "confidence": "高", "price": price,
             "verdict": "可以買"}]


def _held(days_ago: int, price_buy=100):
    import datetime as dt

    from src.config import now_tw

    d = (now_tw().date() - dt.timedelta(days=days_ago)).isoformat()
    return build_portfolio([{"date": d, "code": "2303", "side": "BUY", "shares": "100000", "price": str(price_buy), "margin": "0"}])


_A = {"2303": {"exp_ret": 0.01, "p_newhigh": 0.2, "combo": 0.5}}
_S = {"2303": {"score": 0.1, "name": "聯電", "features": {"close": 100, "ma20": 95}}}
_M = {"2303": {"stop": 50}}


def test_rotation_after_min_hold_with_margin():
    pf = _held(30)
    res = recovery.plan(pf, {"2303": 101.0}, _S, _A, _M, [], 10_000_000, 10_000_000, {}, alts=_alt(4.0))
    it = res["items"][0]
    assert it["decision"] == "獲利換股" and it["swap"]["rule"] == "換股" and it["swap"]["to"]["code"] == "9999"
    assert it["swap"]["orders"][0]["action"] == "SELL" and it["swap"]["orders"][1]["shares"] > 0
    # 優勢不到 1% → 不換
    res = recovery.plan(pf, {"2303": 101.0}, _S, _A, _M, [], 10_000_000, 10_000_000, {}, alts=_alt(2.0))
    assert "2303" not in res["actions"]


def test_profit_swap_same_day_inside_min_hold():
    pf = _held(1)  # 剛買，未滿 10 個交易日
    # 獲利 12%、扣成本後多 2.4% ≥ 2% → 可以當天結單
    res = recovery.plan(pf, {"2303": 112.0}, _S, _A, _M, [], 10_000_000, 10_000_000, {}, alts=_alt(4.0))
    assert res["items"][0]["swap"]["rule"] == "獲利結單" and res["actions"]["2303"]["decision"] == "獲利換股"
    # 獲利只有 5% → 仍受最短持有限制
    res = recovery.plan(pf, {"2303": 105.0}, _S, _A, _M, [], 10_000_000, 10_000_000, {}, alts=_alt(4.0))
    assert "2303" not in res["actions"]


def test_rotation_blocked_when_holding_ratio_would_drop(monkeypatch):
    from src.agents import compliance

    def fake(pf, prices, orders, today, nav):
        return [{"item": "執行建議後持股率 ≥ 70%", "status": "未通過", "detail": "預估 60.0%（目標 90%）"}]

    monkeypatch.setattr(compliance, "check", fake)
    pf = _held(30)
    res = recovery.plan(pf, {"2303": 101.0}, _S, _A, _M, [], 10_000_000, 10_000_000, {}, alts=_alt(4.0))
    it = res["items"][0]
    assert "2303" not in res["actions"] and not it["swap"]["compliance_ok"] and "70%" in it["why"]
