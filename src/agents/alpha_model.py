"""MonthlyAlphaAgent：全市場「未來一個月」選股模型（純統計／機器學習，不使用 LLM）。

進攻籃目標（每檔、每個日期 t）：
- y_newhigh：未來 20 個交易日內最高價 > 過去 252 日最高價（創 52 週新高）
- y_excess ：未來 20 日報酬 − 同日全市場中位數報酬（超額報酬）
穩健籃目標（提高「正報酬才能排名」的晉級機率）：
- y_up ：未來 20 日報酬 > 0
- y_dd ：未來 20 日內最低價相對 t 日收盤的最大回檔

特徵：多期動能、距 52 週高點、波動、量能、均線排列、RSI、相對大盤強弱……（皆以 t 日收盤前已知資料計算）
驗證：滾動時間外樣本（walk-forward），訓練與測試間留 20 日空窗避免標籤洩漏。
已知偏誤：股票池以「目前」流動性篩選，存在存活者偏誤，回測數字可能偏樂觀。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import pickle

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import roc_auc_score

from src.config import CACHE_DIR
from src.state import params

log = logging.getLogger(__name__)

RAW_FEATURES = ["r5", "r20", "r60", "r120", "r250", "dist_hi", "dist_lo", "days_since_hi", "vol20", "vol_ratio",
                "log_val20", "val_ratio", "c_ma20", "ma20_ma60", "ma60_ma120", "rsi14", "rs20", "rs60", "mdd20",
                "up_ratio20", "gap_freq20", "beta60", "is_etf"]
RANK_FEATURES = ["r20", "r60", "r120", "dist_hi", "log_val20", "vol20"]
FEATURES = RAW_FEATURES + [f"{f}_rk" for f in RANK_FEATURES]
FEATURE_NAMES = {
    "r5": "近 5 日漲幅", "r20": "近 20 日漲幅", "r60": "近 60 日漲幅", "r120": "近 120 日漲幅", "r250": "近一年漲幅",
    "dist_hi": "距 52 週高點", "dist_lo": "距 52 週低點", "days_since_hi": "距上次創高天數", "vol20": "20 日波動度",
    "vol_ratio": "波動擴張比", "log_val20": "成交值規模", "val_ratio": "量能放大比（5 日/60 日）", "c_ma20": "股價相對月線",
    "ma20_ma60": "月線相對季線", "ma60_ma120": "季線相對半年線", "rsi14": "RSI14", "rs20": "20 日相對大盤強弱",
    "rs60": "60 日相對大盤強弱", "mdd20": "20 日回檔幅度", "up_ratio20": "20 日上漲天數比", "gap_freq20": "跳空頻率",
    "beta60": "60 日 β", "is_etf": "ETF",
}


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return rsi.where(dn > 0, 100.0).where(up.notna())  # 只漲不跌時 RSI = 100


def stock_features(df: pd.DataFrame, mkt: pd.Series, horizon: int, hw: int, is_etf: bool) -> pd.DataFrame:
    c, h, lo, v = df.Close, df.High, df.Low, df.Volume
    ret = c.pct_change()
    val = c * v
    hi252 = h.rolling(hw, min_periods=hw).max()
    m = mkt.reindex(c.index).ffill()
    f = pd.DataFrame(index=c.index)
    for n in (5, 20, 60, 120, 250):
        f[f"r{n}"] = c / c.shift(n) - 1
    f["dist_hi"] = c / hi252 - 1
    f["dist_lo"] = c / lo.rolling(hw, min_periods=hw).min() - 1
    at_hi = (h >= hi252).astype(int)
    grp = at_hi.cumsum()
    f["days_since_hi"] = (grp.groupby(grp).cumcount()).where(grp > 0, hw) / hw
    f["vol20"] = ret.rolling(20).std()
    f["vol_ratio"] = f["vol20"] / ret.rolling(120).std()
    f["log_val20"] = np.log1p(val.rolling(20).mean())
    f["val_ratio"] = val.rolling(5).mean() / val.rolling(60).mean()
    ma20, ma60, ma120 = c.rolling(20).mean(), c.rolling(60).mean(), c.rolling(120).mean()
    f["c_ma20"] = c / ma20 - 1
    f["ma20_ma60"] = ma20 / ma60 - 1
    f["ma60_ma120"] = ma60 / ma120 - 1
    f["rsi14"] = _rsi(c)
    f["rs20"] = f["r20"] - (m / m.shift(20) - 1)
    f["rs60"] = f["r60"] - (m / m.shift(60) - 1)
    f["mdd20"] = c / c.rolling(20).max() - 1
    f["up_ratio20"] = (ret > 0).rolling(20).mean()
    f["gap_freq20"] = ((df.Open / c.shift(1) - 1).abs() > 0.02).rolling(20).mean()
    mret = m.pct_change()
    f["beta60"] = ret.rolling(60).cov(mret) / mret.rolling(60).var()
    f["is_etf"] = float(is_etf)
    # 標籤：t+1 ~ t+horizon
    fut_high = h[::-1].rolling(horizon, min_periods=horizon).max()[::-1].shift(-1)
    f["y_newhigh"] = np.where(fut_high.notna() & hi252.notna(), (fut_high > hi252).astype(float), np.nan)
    f["y_ret"] = c.shift(-horizon) / c - 1
    f["y_up"] = np.where(f["y_ret"].notna(), (f["y_ret"] > 0).astype(float), np.nan)
    fut_low = lo[::-1].rolling(horizon, min_periods=horizon).min()[::-1].shift(-1)
    f["y_dd"] = (fut_low / c - 1).clip(upper=0)
    f["close"] = c
    f["hi252"] = hi252
    return f


def build_panel(panel: dict[str, pd.DataFrame], codes: list[str], mkt: pd.Series, asof: dt.date,
                etf_codes: set[str]) -> pd.DataFrame:
    cfg = params.alpha()
    frames = []
    for code in codes:
        df = panel[code]
        df = df[df.index < pd.Timestamp(asof)]
        if len(df) < cfg["high_window"] + 30:
            continue
        f = stock_features(df, mkt, cfg["horizon"], cfg["high_window"], code in etf_codes)
        f["code"] = code
        frames.append(f.iloc[cfg["high_window"]:])
    data = pd.concat(frames)
    data.index.name = "date"
    data = data.reset_index().replace([np.inf, -np.inf], np.nan)
    for f in RANK_FEATURES:
        data[f"{f}_rk"] = data.groupby("date")[f].rank(pct=True)
    data["y_excess"] = data["y_ret"] - data.groupby("date")["y_ret"].transform("median")
    return data


def _models(hp: dict):
    common = dict(learning_rate=hp["learning_rate"], max_iter=hp["max_iter"], max_leaf_nodes=hp["max_leaf_nodes"],
                  l2_regularization=hp["l2"], min_samples_leaf=200, random_state=7)
    return HistGradientBoostingClassifier(**common), HistGradientBoostingRegressor(**common)


DEFAULT_HP = {"learning_rate": 0.05, "max_iter": 250, "max_leaf_nodes": 31, "l2": 1.0, "half_life_days": 0}


def _weights(dates: pd.Series, half_life: int) -> np.ndarray | None:
    if not half_life:
        return None
    age = (dates.max() - dates).dt.days.to_numpy()
    return 0.5 ** (age / half_life)


def _fit(train: pd.DataFrame, hp: dict):
    clf, reg = _models(hp)
    w = _weights(train["date"], hp.get("half_life_days", 0))
    clf.fit(train[FEATURES], train["y_newhigh"], sample_weight=w)
    reg.fit(train[FEATURES], train["y_excess"].clip(-0.5, 0.5), sample_weight=w)
    return clf, reg


def _fit_def(train: pd.DataFrame, hp: dict):
    """穩健籃：收正報酬機率（分類）＋ 預期最大回檔（迴歸）。"""
    clf, reg = _models(hp)
    w = _weights(train["date"], hp.get("half_life_days", 0))
    clf.fit(train[FEATURES], train["y_up"], sample_weight=w)
    reg.fit(train[FEATURES], train["y_dd"].clip(-0.5, 0), sample_weight=w)
    return clf, reg


def defense_eligible(df: pd.DataFrame) -> pd.Series:
    """穩健籃資格：不過熱、不遠離月線、β 不高（不追高）。"""
    e = params.get("baskets")["defense"]["eligible"]
    return (df["rsi14"] < e["max_rsi"]) & df["c_ma20"].between(*e["c_ma20"]) & (df["beta60"] < e["max_beta"])


def _def_score(t: pd.DataFrame) -> pd.Series:
    """收正機率＋回檔小＋預期超額報酬（2026-10-05 加入報酬：樣本外 4 期 20 日報酬 +4.5% → +6.7%）。"""
    g = t.groupby("date")
    return g["p_up"].rank(pct=True) + g["e_dd"].rank(pct=True) + g["e"].rank(pct=True)


def walk_forward(data: pd.DataFrame, hp: dict) -> dict:
    """滾動樣本外驗證。回傳 AUC、前 K 名創新高命中率 vs 基準、前 K 名平均超額報酬、IC。"""
    cfg = params.alpha()
    lab = data.dropna(subset=["y_newhigh", "y_ret"])
    dates = np.sort(lab["date"].unique())
    sampled = lab[lab["date"].isin(dates[::cfg["sample_every"]])]
    all_dates = np.sort(data["date"].unique())
    folds = []
    for k in range(cfg["n_folds"], 0, -1):
        end_i = len(dates) - (k - 1) * cfg["fold_days"]
        start_i = end_i - cfg["fold_days"]
        if start_i <= 150:
            continue
        test_start, test_end = dates[start_i], dates[end_i - 1]
        embargo = all_dates[max(0, np.searchsorted(all_dates, test_start) - cfg["horizon"] - 1)]
        train = sampled[sampled["date"] < embargo]
        test = lab[(lab["date"] >= test_start) & (lab["date"] <= test_end)]
        test = test[test["date"].isin(dates[start_i:end_i:cfg["sample_every"]])]
        if len(train) < 5000 or test.empty:
            continue
        clf, reg = _fit(train, hp)
        dclf, dreg = _fit_def(train.dropna(subset=["y_dd"]), hp)
        t = test.copy()
        t["p"] = clf.predict_proba(t[FEATURES])[:, 1]
        t["e"] = reg.predict(t[FEATURES])
        t["s"] = t.groupby("date")["e"].rank(pct=True) + t.groupby("date")["p"].rank(pct=True)
        top = t[t.groupby("date")["s"].rank(ascending=False) <= cfg["top_k"]]
        t["p_up"] = dclf.predict_proba(t[FEATURES])[:, 1]
        t["e_dd"] = dreg.predict(t[FEATURES])
        el = t[defense_eligible(t)].copy()
        el["ds"] = _def_score(el)
        dtop = el[el.groupby("date")["ds"].rank(ascending=False) <= cfg["top_k"]]
        folds.append({
            "def_up_rate": float(dtop["y_up"].mean()) if len(dtop) else None,
            "def_ret": float(dtop["y_ret"].mean()) if len(dtop) else None,
            "def_dd": float(dtop["y_dd"].mean()) if len(dtop) else None,
            "all_up_rate": float(t["y_up"].mean()),
            "off_up_rate": float(top["y_up"].mean()),
            "off_dd": float(top["y_dd"].mean()),
            "start": str(pd.Timestamp(test_start).date()), "end": str(pd.Timestamp(test_end).date()),
            "auc": float(roc_auc_score(t["y_newhigh"], t["p"])) if t["y_newhigh"].nunique() > 1 else 0.5,
            "base_rate": float(t["y_newhigh"].mean()),
            "topk_newhigh": float(top["y_newhigh"].mean()),
            "topk_excess": float(top["y_excess"].mean()),
            "topk_ret": float(top["y_ret"].mean()),
            "mkt_ret": float(t.groupby("date")["y_ret"].median().mean()),
            "ic": float(t.groupby("date").apply(lambda g: g["e"].corr(g["y_excess"], method="spearman")).mean()),
            "n": int(len(t)),
        })
    if not folds:
        return {"folds": [], "score": -1}
    agg = {k: float(np.mean([f[k] for f in folds if f.get(k) is not None])) for k in
           ("auc", "base_rate", "topk_newhigh", "topk_excess", "topk_ret", "mkt_ret", "ic", "def_up_rate", "def_ret",
            "def_dd", "all_up_rate", "off_up_rate", "off_dd")}
    agg["folds"] = folds
    # 綜合目標：前 K 名超額報酬 + 創新高命中率提升
    agg["score"] = agg["topk_excess"] + 0.5 * (agg["topk_newhigh"] - agg["base_rate"])
    return agg


def _cache_key(asof: dt.date, codes: list[str], hp: dict) -> str:
    h = hashlib.md5((",".join(codes) + json.dumps(hp, sort_keys=True) + json.dumps(params.alpha(), sort_keys=True)
                       + json.dumps(params.get("baskets"), sort_keys=True)).encode())
    return f"alpha_v2_{asof}_{h.hexdigest()[:10]}"  # v2：每檔取最近 5 天內最新一筆


def run(panel: dict[str, pd.DataFrame], codes: list[str], mkt: pd.Series, asof: dt.date, etf_codes: set[str],
        evaluate: bool = True) -> dict:
    """訓練（含回測）並對 asof 前最後一個交易日的所有股票打分數。"""
    hp = {**DEFAULT_HP, **params.get("alpha").get("hyperparams", {})}
    key = _cache_key(asof, codes, hp)
    path = CACHE_DIR / f"{key}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    data = build_panel(panel, codes, mkt, asof, etf_codes)
    metrics = walk_forward(data, hp) if evaluate else {}
    cfg = params.alpha()
    lab = data.dropna(subset=["y_newhigh", "y_ret"])
    dates = np.sort(lab["date"].unique())
    train = lab[lab["date"].isin(dates[::cfg["sample_every"]])]
    clf, reg = _fit(train, hp)
    dclf, dreg = _fit_def(train.dropna(subset=["y_dd"]), hp)
    last_date = data["date"].max()
    # 每檔取最近 5 天內自己的最新一筆（部分 ETF／個股的資料會晚一天更新，只取全市場最新日期會漏掉它們）
    recent = data[data["date"] >= pd.Timestamp(last_date) - pd.Timedelta(days=5)]
    latest = recent.sort_values("date").groupby("code").tail(1).dropna(subset=["r60", "dist_hi"]).copy()
    latest["p_newhigh"] = clf.predict_proba(latest[FEATURES])[:, 1]
    latest["exp_excess"] = reg.predict(latest[FEATURES])
    latest["alpha_rank"] = latest["exp_excess"].rank(pct=True)
    latest["nh_rank"] = latest["p_newhigh"].rank(pct=True)
    latest["combo"] = (latest["alpha_rank"] + latest["nh_rank"]) / 2
    latest["p_up"] = dclf.predict_proba(latest[FEATURES])[:, 1]
    latest["e_dd"] = dreg.predict(latest[FEATURES])
    latest["eligible_def"] = defense_eligible(latest.fillna({"rsi14": 100, "c_ma20": 1, "beta60": 9}))
    latest["up_rank"] = latest["p_up"].rank(pct=True)
    latest["dd_rank"] = latest["e_dd"].rank(pct=True)  # 回檔越小（越接近 0）排名越高
    latest["def_combo"] = (latest["up_rank"] + latest["dd_rank"] + latest["alpha_rank"]) / 3
    mkt_mean20 = float(lab.groupby("date")["y_ret"].median().mean())
    preds = {}
    for _, r in latest.iterrows():
        preds[r["code"]] = {
            "p_newhigh": round(float(r["p_newhigh"]), 4), "exp_excess": round(float(r["exp_excess"]), 4),
            "exp_ret": round(float(r["exp_excess"]) + mkt_mean20, 4),
            "alpha_rank": round(float(r["alpha_rank"]), 4), "nh_rank": round(float(r["nh_rank"]), 4),
            "combo": round(float(r["combo"]), 4), "dist_hi": round(float(r["dist_hi"]), 4),
            "p_up": round(float(r["p_up"]), 4), "exp_dd": round(float(r["e_dd"]), 4),
            "up_rank": round(float(r["up_rank"]), 4), "dd_rank": round(float(r["dd_rank"]), 4),
            "def_combo": round(float(r["def_combo"]), 4), "eligible_def": bool(r["eligible_def"]),
            "hi252": round(float(r["hi252"]), 2), "close": round(float(r["close"]), 2),
            "features": {f: (None if pd.isna(r[f]) else round(float(r[f]), 4)) for f in RAW_FEATURES},
        }
    # 特徵重要性：用與目標的相關近似（HGB 無內建重要性）
    corr = train[FEATURES + ["y_excess"]].corr(method="spearman")["y_excess"].drop("y_excess")
    out = {
        "asof": str(pd.Timestamp(last_date).date()), "n_stocks": len(preds), "n_train": int(len(train)),
        "base_rate": round(float(lab["y_newhigh"].mean()), 4), "mkt_mean20": round(mkt_mean20, 4),
        "up_base_rate": round(float(lab["y_up"].mean()), 4),
        "metrics": metrics, "hyperparams": hp, "predictions": preds,
        "feature_ic": {k: round(float(v), 4) for k, v in corr.sort_values(key=abs, ascending=False).head(8).items()},
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(out))
    return out


def explain_def(pred: dict, up_base: float) -> list[str]:
    """穩健籃理由：收正報酬機率、預期回檔、波動與 β、位置不追高。"""
    f = pred["features"]
    out = [f"穩健模型：未來一個月收正報酬機率 {pred['p_up'] * 100:.1f}%（全市場基準 {up_base * 100:.1f}%），"
           f"預期最大回檔 {pred['exp_dd'] * 100:.1f}%（回檔幅度排名全市場最小的前 {(1 - pred['dd_rank']) * 100:.0f}%）"]
    if f.get("beta60") is not None and f.get("vol20") is not None:
        out.append(f"風險：60 日 β = {f['beta60']:.2f}（大盤漲跌 1% 時約變動 {f['beta60']:.2f}%），日波動 {f['vol20'] * 100:.2f}%")
    if f.get("c_ma20") is not None and f.get("rsi14") is not None:
        out.append(f"位置：股價相對月線 {f['c_ma20'] * 100:+.1f}%、RSI14 = {f['rsi14']:.1f}，未過熱、不追高")
    return out


def explain(pred: dict) -> list[str]:
    """把模型輸入特徵轉成白話理由（數字皆為實際特徵值）。"""
    f = pred["features"]
    out = []
    if f.get("dist_hi") is not None:
        out.append(f"距 52 週高點 {f['dist_hi'] * 100:+.1f}%（52 週高 {pred['hi252']}，現價 {pred['close']}）"
                   + ("，正處於創高區" if f["dist_hi"] > -0.03 else ""))
    if f.get("r20") is not None and f.get("r60") is not None:
        out.append(f"動能：近 20 日 {f['r20'] * 100:+.1f}%、近 60 日 {f['r60'] * 100:+.1f}%，"
                   f"相對大盤 20 日 {f['rs20'] * 100:+.1f}%、60 日 {f['rs60'] * 100:+.1f}%")
    if f.get("val_ratio") is not None:
        out.append(f"量能：近 5 日成交值為 60 日均值的 {f['val_ratio']:.2f} 倍" + ("（放量）" if f["val_ratio"] > 1.3 else ""))
    if f.get("ma20_ma60") is not None:
        trend = "多頭排列" if f["c_ma20"] > 0 and f["ma20_ma60"] > 0 and f["ma60_ma120"] > 0 else "均線未全面多頭"
        out.append(f"均線：股價相對月線 {f['c_ma20'] * 100:+.1f}%、月線相對季線 {f['ma20_ma60'] * 100:+.1f}%（{trend}）")
    if f.get("rsi14") is not None:
        out.append(f"RSI14 = {f['rsi14']:.1f}" + ("（過熱，追價風險高）" if f["rsi14"] > 80 else ""))
    return out
