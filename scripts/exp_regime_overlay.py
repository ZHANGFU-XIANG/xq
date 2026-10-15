"""驗證實驗：regime 部位控管（現金 overlay）對 38 天競賽窗的影響。

資料用已是樣本外的 wf_preds_full（walk-forward OOS 預測）。
方法：
  1. baseline＝st.Sim next_open 永遠滿倉 top-N nav（＝現在 OFFENSE 全 1.0 的狀態）。
  2. 由 TAIEX 以「因果」方式算每日 regime 狀態（只用到當日以前的 MA20/MA60/r20）。
  3. overlay：隔日報酬 × 當日決定的投入比例 f（其餘為現金，報酬 0）。
     f 在 t 日收盤決定、套用到 t→t+1 報酬，無前視。
  4. 比較各政策的 38 天窗 P(>0)/median/p10/worst 與 CAGR/maxDD。
  5. 兩段一致性：把所有 38 天窗依起始日切前/後半，要求 P(>0) 改善在兩段都成立才算數。
不改任何正式程式；只讀資料、印結果。
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from src.agents import full_backtest as fb
from src.agents import strategy_tuner as st
from src.agents import regime as rg

COMP = fb.COMP_DAYS  # 38


def window_ret(nav: np.ndarray, n: int = COMP) -> np.ndarray:
    return nav[n:] / nav[:-n] - 1


def stats(w: np.ndarray) -> dict:
    return {"p_pos": round(float((w > 0).mean()), 4), "median": round(float(np.median(w)), 4),
            "p10": round(float(np.percentile(w, 10)), 4), "worst": round(float(w.min()), 4), "n": len(w)}


def cagr_dd(nav, dates):
    yrs = max((dates[-1] - dates[0]).days / 365.25, 0.1)
    return (float((nav[-1] / nav[0]) ** (1 / yrs) - 1),
            float((nav / np.maximum.accumulate(nav) - 1).min()))


def main():
    mkt = fb.load_taiex_max()
    panel = {c: fb.clean_jumps(df) for c, df in fb.load_panel_max(False).items()}
    preds = pd.read_csv(fb.PRED_FULL, dtype={"code": str}, parse_dates=["date"])
    sim = st.Sim(preds, panel, execution="next_open")
    p = st.current_pf()
    dates = pd.DatetimeIndex(sim.dates)
    base_nav = sim.run(p, 0, sim.n)
    base_ret = np.concatenate([[0.0], base_nav[1:] / base_nav[:-1] - 1])  # 每日報酬

    # 因果 regime 狀態：regime.states 用 rolling，天然只看過去
    states = rg.states(mkt).reindex(dates).ffill()
    st_arr = states.to_numpy()

    policies = {
        "滿倉(現狀 1/1/1)":      {"強勢": 1.0, "中性": 1.0, "轉弱": 1.0},
        "紅燈減半 1/1/0.5":      {"強勢": 1.0, "中性": 1.0, "轉弱": 0.5},
        "舊值 0.85/0.6/0.3":     {"強勢": 0.85, "中性": 0.6, "轉弱": 0.3},
        "溫和 1/0.8/0.4":        {"強勢": 1.0, "中性": 0.8, "轉弱": 0.4},
        "紅燈清空 1/1/0":        {"強勢": 1.0, "中性": 1.0, "轉弱": 0.0},
    }

    # 前/後半切點（依 38 天窗的起始日）
    half = len(dates) // 2
    print(f"資料期間 {dates[0].date()} ~ {dates[-1].date()}，{len(dates)} 交易日，前後半切於 {dates[half].date()}\n")
    print(f"{'政策':22} {'P(>0)':>7} {'中位':>8} {'p10':>8} {'最差':>8} {'CAGR':>8} {'maxDD':>8}  兩段P(>0)")
    base_p = None
    for name, off in policies.items():
        f = np.array([off.get(s, 1.0) for s in st_arr])
        f_lag = np.concatenate([[1.0], f[:-1]])  # t 日決定、套 t→t+1
        ov_ret = base_ret * f_lag
        nav = np.cumprod(1 + ov_ret)
        w = window_ret(nav)
        s = stats(w)
        cg, dd = cagr_dd(nav, dates)
        # 兩段
        starts = np.arange(len(w))
        w1, w2 = w[starts < half], w[starts >= half]
        pp1, pp2 = float((w1 > 0).mean()), float((w2 > 0).mean())
        if base_p is None:
            base_p = (s["p_pos"], pp1, pp2)
        print(f"{name:22} {s['p_pos']:7.3f} {s['median']:8.4f} {s['p10']:8.4f} {s['worst']:8.4f} "
              f"{cg:8.3f} {dd:8.3f}  前{pp1:.3f}/後{pp2:.3f}")
    print("\n判讀：相對『滿倉(現狀)』，若某政策 P(>0) 在整體與前、後半都提高，且 worst/p10 改善，才值得進一步納入。")
    print("（存活者偏誤使絕對數字偏樂觀；這裡只看政策間的相對差異。）")


if __name__ == "__main__":
    main()
