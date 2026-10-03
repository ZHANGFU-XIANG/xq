"""OpenPredictorAgent：用「美股隔夜表現」預測台股開盤跳空與開盤後（開→收）走向。

純統計模型（Ridge 迴歸），不使用 LLM。每個標的都附上樣本外回測命中率，
命中率不足的模型在策略中會被降權。

時間對齊：台灣交易日 t 的特徵 = 台灣日期 t 之前（不含）最後一個美股交易日的報酬。
"""
from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from src.agents.market_data import taiex as taiex_df
from src.agents.market_data import tw_price
from src.config import stock_meta
from src.state import params

log = logging.getLogger(__name__)
BASE_FEATURES = ["^SOX", "NQ=F", "TSM", "^VIX", "TWD=X"]


def _us_returns(us_hist: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rets = {t: df.Close.pct_change() for t, df in us_hist.items()}
    out = pd.DataFrame(rets).sort_index()
    out["us_date"] = out.index
    return out


def _align(tw_dates: pd.DatetimeIndex, us_ret: pd.DataFrame) -> pd.DataFrame:
    left = pd.DataFrame({"tw_date": pd.to_datetime(tw_dates).astype("datetime64[ns]")})
    right = us_ret.reset_index(drop=True).sort_values("us_date")
    right["us_date"] = right["us_date"].astype("datetime64[ns]")
    m = pd.merge_asof(left, right, left_on="tw_date", right_on="us_date", allow_exact_matches=False)
    # 美股休市時會重複對到同一根 K 棒，避免重複計入
    dup = m["us_date"].eq(m["us_date"].shift(1))
    m.loc[dup, [c for c in m.columns if c not in ("tw_date", "us_date")]] = 0.0
    return m.set_index("tw_date")


def _build(code: str, tw: pd.DataFrame, us_ret: pd.DataFrame, drivers: list[str]):
    feats = [f for f in BASE_FEATURES if f in us_ret]
    drv = [d for d in drivers if d in us_ret]
    aligned = _align(tw.index, us_ret)
    X = pd.DataFrame(index=tw.index)
    for f in feats:
        X[f] = aligned[f]
    X["drivers_avg"] = aligned[drv].mean(axis=1) if drv else 0.0
    X["own_prev_ret"] = tw.Close.pct_change()  # 前一日自身報酬（已在 t-1 收盤確定）
    y_gap = tw.Open / tw.Close.shift(1) - 1
    y_intra = tw.Close / tw.Open - 1
    # own_prev_ret 對第 t 列應使用 t-1 的報酬
    X["own_prev_ret"] = X["own_prev_ret"].shift(1)
    return X, y_gap, y_intra


def _fit_eval(X: pd.DataFrame, y: pd.Series, ridge_alpha: float = 5.0) -> dict:
    data = pd.concat([X, y.rename("y")], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < 120:
        return {}
    split = int(len(data) * 0.75)
    tr, te = data.iloc[:split], data.iloc[split:]
    model = make_pipeline(StandardScaler(), Ridge(alpha=ridge_alpha))
    model.fit(tr[X.columns], tr.y)
    pred = model.predict(te[X.columns])
    mask = te.y.abs() > 1e-4
    hit = float((np.sign(pred[mask]) == np.sign(te.y[mask])).mean()) if mask.any() else 0.5
    base_hit = float(max((te.y[mask] > 0).mean(), (te.y[mask] < 0).mean())) if mask.any() else 0.5
    mae = float(np.mean(np.abs(pred - te.y)))
    final = make_pipeline(StandardScaler(), Ridge(alpha=ridge_alpha))
    final.fit(data[X.columns], data.y)
    resid_std = float(np.std(data.y - final.predict(data[X.columns])))
    coefs = dict(zip(X.columns, final[-1].coef_ / final[0].scale_))
    return {"model": final, "hit_rate": round(hit, 3), "baseline_hit": round(base_hit, 3), "mae": mae,
            "resid_std": resid_std, "n_test": int(len(te)), "coefs": coefs}


def _predict_row(us_ret: pd.DataFrame, target: dt.date, drivers: list[str], own_prev_ret: float) -> pd.DataFrame:
    row = _align(pd.DatetimeIndex([pd.Timestamp(target)]), us_ret)
    X = pd.DataFrame(index=row.index)
    for f in BASE_FEATURES:
        if f in us_ret:
            X[f] = row[f]
    drv = [d for d in drivers if d in us_ret]
    X["drivers_avg"] = row[drv].mean(axis=1) if drv else 0.0
    X["own_prev_ret"] = own_prev_ret
    return X.fillna(0.0), row["us_date"].iloc[0]


def _trim(df: pd.DataFrame, target: dt.date, years: int) -> pd.DataFrame:
    df = df[df.index < pd.Timestamp(target)]
    return df[df.index >= pd.Timestamp(target) - pd.Timedelta(days=365 * years)]


def recent_hit_rate(taiex: pd.DataFrame, us_hist: dict, target: dt.date, ridge_alpha: float, years: int,
                    window: int = 60) -> float:
    """大盤跳空模型：以 window 日前的資料訓練，計算最近 window 日的方向命中率（給調整 Agent 比較參數）。"""
    us_ret = _us_returns(us_hist)
    tw = _trim(taiex, target, years + 1)
    X, y_gap, _ = _build("TAIEX", tw, us_ret, ["^SOX", "TSM", "NQ=F"])
    data = pd.concat([X, y_gap.rename("y")], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    train = data.iloc[:-window]
    train = train[train.index >= train.index[-1] - pd.Timedelta(days=365 * years)]
    test = data.iloc[-window:]
    model = make_pipeline(StandardScaler(), Ridge(alpha=ridge_alpha)).fit(train[X.columns], train.y)
    pred = model.predict(test[X.columns])
    mask = test.y.abs() > 1e-4
    return float((np.sign(pred[mask]) == np.sign(test.y[mask])).mean())


def predict(codes: list[str], us_hist: dict[str, pd.DataFrame], target: dt.date) -> dict:
    """回傳 {code: {...預測...}}，含 "TAIEX" 大盤。百分比單位為 %。"""
    us_ret = _us_returns(us_hist)
    cfg = params.predictor()
    min_hit = cfg["min_hit_rate"]
    out: dict[str, dict] = {}
    universe = [("TAIEX", ["^SOX", "TSM", "NQ=F"])] + [(c, stock_meta(c).get("drivers", [])) for c in codes]
    for code, drivers in universe:
        try:
            tw = _trim(taiex_df(3) if code == "TAIEX" else tw_price(code), target, cfg["history_years"])
            if len(tw) < 150:
                continue
            X, y_gap, y_intra = _build(code, tw, us_ret, drivers)
            gap_m = _fit_eval(X, y_gap, cfg["ridge_alpha"])
            intra_m = _fit_eval(X, y_intra, cfg["ridge_alpha"])
            if not gap_m:
                continue
            own_prev = float(tw.Close.iloc[-1] / tw.Close.iloc[-2] - 1)
            Xt, us_date = _predict_row(us_ret, target, drivers, own_prev)
            gap = float(gap_m["model"].predict(Xt[X.columns])[0])
            intra = float(intra_m["model"].predict(Xt[X.columns])[0]) if intra_m else 0.0
            reliable = gap_m["hit_rate"] >= min_hit and gap_m["hit_rate"] > gap_m["baseline_hit"]
            last_close = float(tw.Close.iloc[-1])
            top = sorted(((k, v * float(Xt[k].iloc[0])) for k, v in gap_m["coefs"].items()),
                         key=lambda kv: -abs(kv[1]))[:3]
            out[code] = {
                "gap_pct": round(gap * 100, 2),
                "gap_band_pct": round(gap_m["resid_std"] * 100, 2),
                "intraday_pct": round(intra * 100, 2),
                "pred_open": round(last_close * (1 + gap), 2),
                "last_close": round(last_close, 2),
                "gap_hit_rate": gap_m["hit_rate"],
                "gap_baseline_hit": gap_m["baseline_hit"],
                "intra_hit_rate": intra_m.get("hit_rate") if intra_m else None,
                "intra_baseline_hit": intra_m.get("baseline_hit") if intra_m else None,
                "intra_band_pct": round(intra_m["resid_std"] * 100, 2) if intra_m else None,
                "intra_reliable": bool(intra_m and intra_m["hit_rate"] >= min_hit
                                       and intra_m["hit_rate"] > intra_m["baseline_hit"]),
                "n_test": gap_m["n_test"],
                "reliable": bool(reliable),
                "us_date": pd.Timestamp(us_date).date().isoformat() if pd.notna(us_date) else None,
                "top_drivers": [{"feature": k, "contribution_pct": round(v * 100, 2)} for k, v in top],
                "direction": "開高" if gap > 0.001 else ("開低" if gap < -0.001 else "平盤"),
                "after_open": "續強" if intra > 0.001 else ("回落" if intra < -0.001 else "震盪"),
            }
        except Exception as e:  # noqa: BLE001
            log.warning("預測 %s 失敗: %s", code, e)
    return out
