"""ModelResearchAgent：測試新特徵（月營收、三大法人）能不能讓一個月模型更好；通過嚴格驗證才建議採用。

特徵（全部只用「當時已公布」的資料）：
- 月營收：年增、月增、近 3 個月年增平均、營收創 12 個月新高、公布後經過天數（次月 11 日起才可用）
- 法人：外資／投信 5、20 日買超占成交量比、投信連續買超天數（當天收盤後公布，用於隔日開盤交易）
驗證（和 full_backtest 相同的管線）：
- 同一段期間（法人資料起點起）、同樣的訓練窗、每年重訓，比較「只有價量」vs「價量＋新特徵」
- 指標：隔日開盤成交的組合年化（5 種起始日中位數）、每天前 5／前 20 名 20 日平均報酬
- 採用條件：前後兩半段的組合年化都變好，且全期提升 ≥ 3 個百分點；否則記錄「已測試、無優勢」
結果：data/strategy/research.json（模型頁顯示），不自動改線上模型（採用需要另外接上每日特徵）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

import numpy as np
import pandas as pd

from src.agents import alpha_model as am
from src.agents import full_backtest as fb
from src.agents import strategy_tuner as st
from src.state import params

log = logging.getLogger(__name__)
RESULT = st.DIR / "research.json"
REV_FEATS = ["rev_yoy", "rev_mom", "rev_yoy3", "rev_hi12", "rev_age"]
CHIP_FEATS = ["f5", "f20", "t5", "t20", "t_streak"]


def revenue_features(data: pd.DataFrame) -> pd.DataFrame:
    from src.data import history_store as hs

    rv = hs.load_revenue()
    if rv.empty:
        return data.assign(**{k: np.nan for k in REV_FEATS})
    rv = rv.sort_values(["code", "ym"])
    g = rv.groupby("code")
    rv["rev_yoy3"] = g["yoy"].transform(lambda s: s.rolling(3, min_periods=1).mean())
    rv["rev_hi12"] = (rv["revenue"] >= g["revenue"].transform(lambda s: s.rolling(12, min_periods=6).max())).astype(float)
    rv = rv.rename(columns={"yoy": "rev_yoy", "mom": "rev_mom"})[["code", "available", "rev_yoy", "rev_mom", "rev_yoy3", "rev_hi12"]]
    rv = rv.sort_values("available")
    left = data[["date", "code"]].reset_index().sort_values("date")
    m = pd.merge_asof(left, rv, left_on="date", right_on="available", by="code", direction="backward")
    m["rev_age"] = (m["date"] - m["available"]).dt.days
    m = m.set_index("index").reindex(data.index)
    out = data.copy()
    for k in REV_FEATS:
        out[k] = m[k].astype("float32").clip(-500, 500) if k in ("rev_yoy", "rev_mom", "rev_yoy3") else m[k].astype("float32")
    return out


def chip_features(data: pd.DataFrame, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    from src.data import history_store as hs

    ch = hs.load_chip()
    if ch.empty:
        return data.assign(**{k: np.nan for k in CHIP_FEATS})
    fo = ch.pivot_table(index="date", columns="code", values="foreign", aggfunc="sum")
    tr = ch.pivot_table(index="date", columns="code", values="trust", aggfunc="sum")
    codes = [c for c in fo.columns if c in panel]
    vol = pd.DataFrame({c: panel[c]["Volume"] for c in codes}).reindex(fo.index)
    fo, tr = fo[codes].fillna(0), tr.reindex(columns=codes).fillna(0)
    feats = {}
    for n in (5, 20):
        v = vol.rolling(n, min_periods=max(2, n // 2)).sum().replace(0, np.nan)
        feats[f"f{n}"] = fo.rolling(n, min_periods=max(2, n // 2)).sum() / v
        feats[f"t{n}"] = tr.rolling(n, min_periods=max(2, n // 2)).sum() / v
    pos = (tr > 0).astype(int)
    feats["t_streak"] = pos.apply(lambda s: s.groupby((s != s.shift()).cumsum()).cumsum() * s)  # 投信連續買超天數
    long = pd.concat({k: v.stack() for k, v in feats.items()}, axis=1)
    long.index.names = ["date", "code"]
    out = data.merge(long.reset_index(), on=["date", "code"], how="left")
    for k in CHIP_FEATS:
        out[k] = out[k].astype("float32").clip(-1, 1) if k != "t_streak" else out[k].astype("float32")
    return out


def _walk(data: pd.DataFrame, feats: list[str], start: pd.Timestamp, retrain: int = 250, min_train_days: int = 500) -> pd.DataFrame:
    cfg = params.alpha()
    hp = {**am.DEFAULT_HP, **cfg.get("hyperparams", {})}
    d = data[data["date"] >= start]
    dates = np.sort(d["date"].unique())
    lab = d.dropna(subset=["y_newhigh", "y_excess"])
    ld = np.sort(lab["date"].unique())
    lab = lab[lab["date"].isin(set(ld[::cfg["sample_every"]]))]
    out = []
    for s in range(min_train_days, len(dates), retrain):
        emb = dates[max(0, s - cfg["horizon"] - 1)]
        train = lab[lab["date"] < emb]
        if len(train) < 5000:
            continue
        clf, reg = am._models(hp)
        clf.fit(train[feats], train["y_newhigh"])
        reg.fit(train[feats], train["y_excess"].clip(-0.5, 0.5))
        test = d[(d["date"] >= dates[s]) & (d["date"] <= dates[min(s + retrain, len(dates)) - 1])].copy()
        test["p"] = clf.predict_proba(test[feats])[:, 1]
        test["e"] = reg.predict(test[feats])
        out.append(st._compact(test))
    return pd.concat(out, ignore_index=True)


def _score(preds: pd.DataFrame, panel: dict, mkt: pd.Series) -> dict:
    sim = st.Sim(preds, panel, execution="next_open")
    half = sim.n // 2
    p = st.current_pf()
    full, a, b = st.evaluate_pf(sim, p, 0, sim.n), st.evaluate_pf(sim, p, 0, half), st.evaluate_pf(sim, p, half, sim.n)
    cal = fb.calibration(preds, panel, mkt)
    return {"cagr": round(full["median"], 4), "first_half": round(a["median"], 4), "second_half": round(b["median"], 4),
            "max_dd": round(full["max_dd"], 4), "from": full["from"], "to": full["to"],
            "top5_avg20": next((x["avg"] for x in cal["buckets"] if x["rank"] == "1～5"), None),
            "top5_win": next((x["win"] for x in cal["buckets"] if x["rank"] == "1～5"), None)}


def run(variants: tuple[str, ...] = ("base", "revenue", "chip", "both")) -> dict:
    t0 = time.time()
    mkt = fb.load_taiex_max()
    panel = {c: fb.clean_jumps(df) for c, df in fb.load_panel_max().items()}
    from src.data import history_store as hs

    ch = hs.load_chip()
    start = pd.Timestamp(ch["date"].min()) if len(ch) else pd.Timestamp("2016-01-01")
    data = fb.build_data(panel, mkt)
    data = data[data["date"] >= start - pd.Timedelta(days=40)].reset_index(drop=True)
    data = revenue_features(data)
    data = chip_features(data, panel)
    sets = {"base": am.FEATURES, "revenue": am.FEATURES + REV_FEATS, "chip": am.FEATURES + CHIP_FEATS,
            "both": am.FEATURES + REV_FEATS + CHIP_FEATS}
    res = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "data_from": str(start.date()),
           "coverage": {"chip_rows": int(len(ch)), "rev_nonnull": round(float(data["rev_yoy"].notna().mean()), 3),
                        "chip_nonnull": round(float(data["f20"].notna().mean()), 3)}, "variants": {}}
    for v in variants:
        t = time.time()
        preds = _walk(data, sets[v], start)
        res["variants"][v] = {**_score(preds, panel, mkt), "features": len(sets[v]), "seconds": round(time.time() - t)}
        log.info("研究 %s：%s", v, res["variants"][v])
    base = res["variants"].get("base")
    verdicts = {}
    for v, r in res["variants"].items():
        if v == "base" or not base:
            continue
        ok = (r["first_half"] > base["first_half"] and r["second_half"] > base["second_half"]
              and r["cagr"] - base["cagr"] >= 0.03)
        verdicts[v] = {"adopt": ok, "delta_cagr": round(r["cagr"] - base["cagr"], 4),
                       "text": ("建議採用：前後兩段都變好、全期年化提升 ≥ 3 個百分點" if ok else
                                f"已測試、無足夠優勢（全期年化 {(r['cagr'] - base['cagr']) * 100:+.1f} 個百分點；"
                                f"前半 {(r['first_half'] - base['first_half']) * 100:+.1f}、後半 {(r['second_half'] - base['second_half']) * 100:+.1f}）")}
    res["verdicts"] = verdicts
    res["seconds"] = round(time.time() - t0)
    RESULT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return res


def result() -> dict:
    return json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.exists() else {}
