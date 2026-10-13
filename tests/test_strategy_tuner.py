import numpy as np
import pandas as pd

from src.agents import strategy_tuner as st


def _fake(n_days=300, n_codes=30, seed=1):
    """合成資料：預期報酬 e 與未來實際漲幅正相關，模擬「模型有效」的情況。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)
    drift = rng.normal(0, 0.002, n_codes)
    rets = drift + rng.normal(0, 0.01, (n_days, n_codes))
    close = 100 * np.cumprod(1 + rets, axis=0)
    codes = [f"{1000 + i}" for i in range(n_codes)]
    panel = {c: pd.DataFrame({"Close": close[:, i]}, index=dates) for i, c in enumerate(codes)}
    rows = [{"date": d, "code": c, "e": drift[i] * 20 + rng.normal(0, 0.005), "p": 0.5}
            for d in dates for i, c in enumerate(codes)]
    preds = pd.DataFrame(rows)
    preds["e_floor"] = preds.groupby("date")["e"].transform("min")
    return st.Sim(preds, panel)


def test_sim_runs_and_respects_positions():
    sim = _fake()
    p = {"max_positions": 5, "min_hold_days": 10, "swap_margin": 0.01, "profit_swap_min_pnl": 0.1,
         "profit_swap_margin": 0.02, "expiry_days": 40}
    nav = sim.run(p, 0, sim.n)
    assert len(nav) == sim.n and nav[0] == 1.0
    r = st.evaluate_pf(sim, p, 0, sim.n)
    assert r["worst"] <= r["median"] and r["max_dd"] <= 0


def test_model_signal_beats_random_pick():
    sim = _fake()
    p = {"max_positions": 5, "min_hold_days": 5, "swap_margin": 0.01, "profit_swap_min_pnl": 9.0,
         "profit_swap_margin": 0.02, "expiry_days": 999}
    good = st.evaluate_pf(sim, p, 0, sim.n)["median"]
    sim.top = [list(reversed(t)) for t in sim.top]  # 反過來挑最差的
    sim.e = [{c: -v for c, v in e.items()} for e in sim.e]
    bad = st.evaluate_pf(sim, p, 0, sim.n)["median"]
    assert good > bad


def test_tune_portfolio_never_below_min_positions():
    sim = _fake(n_days=260)
    ch, res = st.tune_portfolio(sim)
    assert res["candidate"]["max_positions"] >= st.MIN_POSITIONS
    if ch:  # 採用時驗證段不能輸
        assert res["new_valid"]["median"] >= res["cur_valid"]["median"]


def test_apply_change_writes_strategy_and_basket():
    cur = {}
    after = {"max_positions": 6, "min_hold_days": 5, "swap_margin": 0.02, "profit_swap_min_pnl": 0.1,
             "profit_swap_margin": 0.02, "expiry_days": 40}
    assert st.apply_change(cur, {"param": "strategy.portfolio", "after": after})
    assert cur["strategy"]["min_hold_days"] == 5
    off = cur["baskets"]["offense"]
    assert off["max_positions"] == 6 and "name" in off and "anti_chase" in off  # 整份進攻籃設定都保留
    assert st.apply_change(cur, {"param": "daytrade", "after": {"stop_atr": 1.0}})
    assert cur["daytrade"] == {"stop_atr": 1.0}
    assert not st.apply_change(cur, {"param": "strategy.risk_per_trade", "after": 0.01})
