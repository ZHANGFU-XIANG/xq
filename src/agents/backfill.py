"""BackfillAgent：回補歷史預測紀錄，讓調整 Agent 從第一天就有足夠的「已對答案」樣本。

做法（不偷看未來）：在過去 N 個交易日中每隔 step 天，假裝當天早上執行系統——
一個月模型只用該日之前的資料訓練、隔夜模型只用該日之前的美股與台股資料——把當時的預測寫入
data/prediction_log.csv（extra 標記 backfill）。之後 tuner.evaluate() 會自動拿實際結果對答案。

注意：回補只用得到「價格類」因子（模型、創新高機率、隔夜預測）；法人、營收、新聞情緒的歷史值
沒有逐日保存，回補紀錄中這些因子為 0，調整 Agent 計算因子相關性時會自動略過。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging

import pandas as pd

from src.agents import alpha_model, market_data, predictor, tuner, universe
from src.config import stock_meta
from src.state import params

log = logging.getLogger(__name__)


def _rows_for_day(panel, tx, us_hist, target: dt.date) -> list[dict]:
    codes = universe.liquid_universe(panel, target)
    etf = {c for c in codes if stock_meta(c)["is_etf"]}
    out = alpha_model.run(panel, codes, tx.Close, target, etf, evaluate=False)
    preds = out["predictions"]
    k = params.alpha()["top_k"]
    w = params.score_weights()
    rows = []
    off = sorted(preds, key=lambda c: -preds[c]["combo"])[:k]
    dfn = sorted([c for c in preds if preds[c]["eligible_def"]], key=lambda c: -preds[c]["def_combo"])[:k]
    for basket, codes_b in (("offense", off), ("defense", dfn)):
        for c in codes_b:
            p = preds[c]
            comps = {"alpha": round(p["alpha_rank"] * 2 - 1, 3), "newhigh": round(p["nh_rank"] * 2 - 1, 3),
                     "institutional": 0.0, "revenue": 0.0, "overnight": 0.0, "sentiment": 0.0}
            score = sum(w.get(kk, 0) * v for kk, v in comps.items())
            rows.append({"kind": "alpha", "code": c, "pred": p["exp_excess"], "band": p["p_newhigh"],
                         "extra": json.dumps({"hi252": p["hi252"], "close": p["close"], "basket": basket, "p_up": p["p_up"],
                                              "components": comps, "score": round(score, 3), "backfill": 1})})
    ov = predictor.predict(off[:10], us_hist, target)
    for c, p in ov.items():
        rows.append({"kind": "gap", "code": c, "pred": p["gap_pct"], "band": p["gap_band_pct"], "extra": '{"backfill":1}'})
        rows.append({"kind": "intra", "code": c, "pred": p["intraday_pct"], "band": p.get("intra_band_pct"), "extra": '{"backfill":1}'})
    for r in rows:
        r.update({"run_date": target.isoformat(), "target_date": target.isoformat()})
    return rows


def run(days: int = 60, step: int = 2) -> dict:
    panel = universe.load_panel(refresh=False)
    market_data.set_panel(panel)
    tx = market_data.taiex(5)
    us_hist = market_data.us_history()
    dates = [d.date() for d in tx.index[-days:]][::step]
    existing = tuner.load_log()
    have = {(r["run_date"], r["kind"]) for r in existing}
    new_rows = []
    for i, d in enumerate(dates, 1):
        if (d.isoformat(), "alpha") in have:
            continue
        try:
            rows = _rows_for_day(panel, tx, us_hist, d)
            new_rows += rows
            log.info("回補 %s（%d/%d）：%d 筆", d, i, len(dates), len(rows))
        except Exception as e:  # noqa: BLE001
            log.warning("回補 %s 失敗：%s", d, e)
    merged = existing + new_rows
    merged.sort(key=lambda r: (r["run_date"], r["kind"], r["code"]))
    with tuner.LOG_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=tuner.FIELDS)
        w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k, "") for k in tuner.FIELDS})
    ev = tuner.evaluate(panel, tx)
    return {"dates": len(dates), "new_rows": len(new_rows), "stats": tuner.summary_stats(ev),
            "by_basket": {b: _basket_stats(ev, b) for b in ("offense", "defense")}}


def _basket_stats(ev: dict, basket: str) -> dict:
    rows = [a for a in ev["alpha"] if a.get("basket") == basket]
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    return {"n": len(df), "avg_ret": round(float(df["real_ret"].mean()), 4), "avg_excess": round(float(df["real_excess"].mean()), 4),
            "up_rate": round(float((df["real_ret"] > 0).mean()), 3), "newhigh_rate": round(float(df["real_newhigh"].mean()), 3)}


def rebuild_nav_history(start: dt.date, end: dt.date) -> list[dict]:
    """Recompute data/nav_history.json per trading day from trades.csv and that day's closes (App convention: total value, holding ratio).

    Used to repair history after the trade log was wrong (10/07–10/08 stock rows were missing, which recorded a 0% holding ratio and
    −2.07% return; that history feeds drawdown and allocation decisions). Futures are marked at that day's TAIFEX settlement price."""
    from src.agents import futures_data
    from src.config import is_trading_day
    from src.state import portfolio as pfm

    trades = pfm.load_trades()
    fut = {r["time"]: r for r in futures_data.history("TX", 40)}
    out = []
    d = start
    while d <= end:
        if is_trading_day(d):
            rows = [t for t in trades if t["date"] <= d.isoformat()]
            pf = pfm.build_portfolio(rows)
            prices = {}
            for c in pf.positions:
                px = market_data.tw_price(c)
                px = px[px.index <= pd.Timestamp(d)]
                if px.empty or px.index[-1].date() != d:
                    raise ValueError(f"{c} 缺少 {d} 收盤價，無法重算")
                prices[c] = float(px.Close.iloc[-1])
            for c, dv in pf.derivs.items():
                bar = fut.get(d.isoformat())
                if not bar or bar["contract"] != c or not bar.get("settlement"):
                    raise ValueError(f"{c} 缺少 {d} 結算價，無法重算")
                prices[c] = bar["settlement"]
            nav = pfm.app_total(pf, prices)
            pfm.record_history(d.isoformat(), nav, pf.holding_ratio(prices), {c: p.shares for c, p in pf.positions.items()})
            out.append({"date": d.isoformat(), "nav": round(nav), "holding_ratio": round(pf.holding_ratio(prices), 4)})
        d += dt.timedelta(days=1)
    return out
