"""全歷史回測：用所有能查到的日 K（Yahoo 台股約 1993～2000 年起）重建樣本外預測，回測主策略。

為什麼要做（2026-10-14）：
- 原本策略調參只回測 2021-11～2026-10（約 5 年，幾乎都是多頭），沒有經歷 2008 金融海嘯、2011 歐債、2015、2018、2022 空頭
- 原本模擬「t 日收盤算預測、同一個收盤價成交」，實戰做不到；改用「t+1 日開盤成交」（盤後算完、隔日預掛單）

做法（盡量貼近實戰，避免偷看未來）：
1. 股票池＝目前所有上市櫃普通股與 ETF（排除槓桿／反向／債券），各股抓 Yahoo 最長歷史
2. 每天「當時」流動性篩選：20 日均成交值 ≥ universe.min_avg_value20、股價 ≥ min_price（不用今天的流動性去挑過去）
3. 除權息／減資造成超過漲跌幅的跳動視為公司行動，往前調整價格（不讓假漲跌進到報酬）
4. Walk-forward：每 retrain_days 個交易日重新訓練一次，只用「測試起點前 horizon 日」以前有標籤的資料，至少 min_train_years 年
5. 模擬沿用 strategy_tuner.Sim（同一套結單轉股規則、競賽來回成本 0.585%）

已知限制（數字可能偏樂觀）：已下市股票 Yahoo 查不到 → 存活者偏誤；早年（2000～2005）可交易的股票較少。

用法：python -m src.main fullbacktest            （首次約 30～60 分鐘：下載＋逐年訓練，之後有快取）
      python -m src.main fullbacktest --tune     （另外在全歷史上調參，前半段挑、後半段驗證，通過才寫入）
結果：data/strategy/full_history.json（模型頁「演算法回測總表」顯示），預測：data/strategy/wf_preds_full.csv.gz
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pickle
import time

import numpy as np
import pandas as pd

from src.agents import alpha_model as am
from src.agents import strategy_tuner as st
from src.config import CACHE_DIR, settings
from src.data import yahoo_gateway as yg
from src.state import params

log = logging.getLogger(__name__)
PANEL_MAX = CACHE_DIR / "panel_max.pkl"
TAIEX_MAX = CACHE_DIR / "taiex_max.pkl"
PRED_FULL = st.DIR / "wf_preds_full.csv.gz"
RESULT = st.DIR / "full_history.json"
COMP_DAYS = 38            # 競賽 2026-10-05～11-27 共 38 個交易日
RETRAIN_DAYS = 250        # 約一年重訓一次（實戰每天重訓，這裡為了計算量；模型每年都只用當時以前的資料）
MIN_TRAIN_YEARS = 3


# ---------------------------------------------------------------- 資料
def universe_codes() -> list[str]:
    """目前所有上市櫃普通股＋ETF（不看今天的成交值，避免只挑「現在很熱」的股票）。"""
    from src.agents import universe

    cfg = settings()["universe"]
    out = []
    for code, m in universe.stock_info().items():
        if not universe.CODE_RE.match(code) or m["raw_industry"] in cfg["exclude_industries"]:
            continue
        if m["is_etf"] and (not cfg["include_etf"] or any(k in m["name"] for k in cfg["exclude_name_keywords"])):
            continue
        out.append(code)
    return sorted(out)


def load_panel_max(refresh: bool = False) -> dict[str, pd.DataFrame]:
    from src.agents import universe

    if PANEL_MAX.exists() and not refresh and time.time() - PANEL_MAX.stat().st_mtime < 7 * 86400:
        return pickle.loads(PANEL_MAX.read_bytes())
    codes = universe_codes()
    log.info("全歷史：下載 %d 檔最長歷史", len(codes))
    panel = universe._download(codes, "max")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PANEL_MAX.write_bytes(pickle.dumps(panel))
    return panel


def load_taiex_max() -> pd.Series:
    if TAIEX_MAX.exists() and time.time() - TAIEX_MAX.stat().st_mtime < 7 * 86400:
        return pickle.loads(TAIEX_MAX.read_bytes())
    df = yg.history("^TWII", period="max", auto_adjust=False)
    df.index = pd.to_datetime(df.index.date)
    s = df["Close"].dropna()
    TAIEX_MAX.write_bytes(pickle.dumps(s))
    return s


def clean_jumps(df: pd.DataFrame) -> pd.DataFrame:
    """超過當時漲跌幅（2015-06-01 前 7%、之後 10%）的單日跳動視為除權息／減資等公司行動，往前調整價格。
    上市前 5 個交易日不設漲跌幅、停牌超過 10 天後的復牌跳動屬真實漲跌，保留。"""
    c = df["Close"]
    r = c.pct_change()
    lim = np.where(df.index < pd.Timestamp("2015-06-01"), 0.07, 0.10) * 1.05
    gap = pd.Series(df.index, index=df.index).diff().dt.days
    bad = (r.abs() > lim) & (gap <= 10)
    bad.iloc[:6] = False
    if not bad.any():
        return df
    f = pd.Series(1.0, index=df.index)
    for d in r[bad].index:
        f[f.index < d] *= 1 + r[d]
    out = df.copy()
    for k in ("Open", "High", "Low", "Close"):
        out[k] = out[k] * f
    out["Volume"] = out["Volume"] / f  # 成交值（價×量）不變
    return out


def build_data(panel: dict[str, pd.DataFrame], mkt: pd.Series) -> pd.DataFrame:
    """所有股票、所有日期的特徵與標籤，只保留「當天」符合流動性門檻的列。"""
    cfg, ucfg = params.alpha(), settings()["universe"]
    etf = {c for c in panel if c.startswith("00")}
    frames = []
    for code, df in panel.items():
        if len(df) < cfg["high_window"] + 60:
            continue
        f = am.stock_features(df, mkt, cfg["horizon"], cfg["high_window"], code in etf, cfg.get("horizon_long", 40))
        f = f.iloc[cfg["high_window"]:]
        liquid = (np.expm1(f["log_val20"]) >= ucfg["min_avg_value20"]) & (f["close"] >= ucfg["min_price"])
        f = f[liquid]
        if f.empty:
            continue
        f["code"] = code
        num = f.select_dtypes("float64").columns
        f[num] = f[num].astype("float32")
        frames.append(f)
    data = pd.concat(frames)
    data.index.name = "date"
    data = data.reset_index().replace([np.inf, -np.inf], np.nan)
    for k in am.RANK_FEATURES:
        data[f"{k}_rk"] = data.groupby("date")[k].rank(pct=True).astype("float32")
    data["y_excess"] = data["y_ret"] - data.groupby("date")["y_ret"].transform("median")
    return data


def walk_forward(data: pd.DataFrame) -> pd.DataFrame:
    cfg = params.alpha()
    hp = {**am.DEFAULT_HP, **cfg.get("hyperparams", {})}
    dates = np.sort(data["date"].unique())
    lab = data.dropna(subset=["y_newhigh", "y_excess"])
    lab_dates = np.sort(lab["date"].unique())
    sampled = set(lab_dates[::cfg["sample_every"]])
    lab = lab[lab["date"].isin(sampled)]
    start = int(np.searchsorted(dates, dates[0] + np.timedelta64(int(365.25 * MIN_TRAIN_YEARS), "D")))
    out = []
    for s in range(start, len(dates), RETRAIN_DAYS):
        embargo = dates[max(0, s - cfg["horizon"] - 1)]
        train = lab[lab["date"] < embargo]
        if len(train) < 5000:
            continue
        t0 = time.time()
        clf, reg = am._fit(train, hp)
        test = data[(data["date"] >= dates[s]) & (data["date"] <= dates[min(s + RETRAIN_DAYS, len(dates)) - 1])].copy()
        test["p"] = clf.predict_proba(test[am.FEATURES])[:, 1]
        test["e"] = reg.predict(test[am.FEATURES])
        out.append(st._compact(test))
        log.info("全歷史 walk-forward：%s 起 %d 天，訓練 %d 列（%.0f 秒）", pd.Timestamp(dates[s]).date(),
                 test["date"].nunique(), len(train), time.time() - t0)
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------- 評估
def _window_stats(nav: np.ndarray, n: int = COMP_DAYS) -> dict:
    """所有連續 n 個交易日區間（＝一次競賽長度）的報酬分布。"""
    if len(nav) <= n:
        return {}
    w = nav[n:] / nav[:-n] - 1
    return {"p_positive": round(float((w > 0).mean()), 4), "median": round(float(np.median(w)), 4),
            "p10": round(float(np.percentile(w, 10)), 4), "p90": round(float(np.percentile(w, 90)), 4),
            "worst": round(float(w.min()), 4), "n": int(len(w))}


def _yearly(nav: np.ndarray, dates: list) -> dict:
    s = pd.Series(nav, index=pd.DatetimeIndex(dates))
    y = s.groupby(s.index.year).last()
    first = s.iloc[0]
    prev = pd.concat([pd.Series([first], index=[y.index[0] - 1]), y]).shift(1).iloc[1:]
    return {int(k): round(float(v), 4) for k, v in (y / prev - 1).items()}


def evaluate(sim: st.Sim, p: dict, mkt: pd.Series) -> dict:
    nav = sim.run(p, 0, sim.n)
    d0, d1 = sim.dates[0], sim.dates[-1]
    m = mkt.reindex(pd.DatetimeIndex(sim.dates)).ffill().to_numpy()
    res = {"from": str(d0.date()), "to": str(d1.date()), **st._stats(nav, d0, d1),
           "window": _window_stats(nav), "yearly": _yearly(nav, sim.dates),
           "taiex": {**st._stats(m, d0, d1), "window": _window_stats(m), "yearly": _yearly(m, sim.dates)}}
    res["robust"] = st.evaluate_pf(sim, p, 0, sim.n)  # 5 種起始日
    return res


def calibration(preds: pd.DataFrame, panel: dict[str, pd.DataFrame], mkt: pd.Series, h: int = 20) -> dict:
    """推薦的真實勝率（2026-10-14 使用者問「準確率能否 90% 以上」）：依當天排名分組，
    隔日開盤買、h 個交易日後開盤賣、扣來回成本，算賺錢機率、平均報酬、贏大盤機率。"""
    dates = sorted(preds["date"].unique())
    codes = sorted(set(preds["code"]) & set(panel))
    op = pd.DataFrame({c: panel[c]["Open"] for c in codes}).reindex(pd.DatetimeIndex(dates))
    fwd = op.shift(-(h + 1)) / op.shift(-1) - 1 - st.COST
    m = mkt.reindex(pd.DatetimeIndex(dates)).ffill()
    mret = m.shift(-(h + 1)) / m.shift(-1) - 1
    p = preds[preds["code"].isin(codes)].copy()
    p["rank"] = p.groupby("date")["e"].rank(ascending=False)
    long = fwd.stack().rename("ret").reset_index()
    long.columns = ["date", "code", "ret"]
    p = p.merge(long, on=["date", "code"], how="inner").dropna(subset=["ret"])
    p["beat"] = p["ret"].to_numpy() - mret.reindex(p["date"]).to_numpy() > 0
    out = []
    for a, b in ((1, 1), (1, 5), (6, 20), (21, 50), (51, 150)):
        x = p[(p["rank"] >= a) & (p["rank"] <= b)]
        out.append({"rank": f"{a}" if a == b else f"{a}～{b}", "n": int(len(x)), "win": round(float((x["ret"] > 0).mean()), 4),
                    "avg": round(float(x["ret"].mean()), 4), "median": round(float(x["ret"].median()), 4),
                    "beat_market": round(float(x["beat"].mean()), 4)})
    top5 = p[p["rank"] <= 5].groupby("date")["ret"].mean()
    hi = p[(p["rank"] <= 20) & (p["p"] >= 0.9)]
    return {"horizon": h, "buckets": out, "market_win": round(float((mret.dropna() > 0).mean()), 4),
            "top5_portfolio_win": round(float((top5 > 0).mean()), 4), "top5_portfolio_avg": round(float(top5.mean()), 4),
            "high_p_newhigh_win": round(float((hi["ret"] > 0).mean()), 4),
            "note": ("單一推薦 20 天後賺錢的機率約五成，平均報酬為正是因為賺的時候賺比較多；"
                     "「創新高機率」是「20 天內碰到 52 週新高」的機率，不是賺錢機率。股票沒有 90% 以上的準確率，宣稱有的多半是偷看答案或過度擬合。")}


def run(tune: bool = False, refresh: bool = False) -> dict:
    t0 = time.time()
    mkt = load_taiex_max()
    raw = load_panel_max(refresh)
    panel = {c: clean_jumps(df) for c, df in raw.items()}
    if refresh or not PRED_FULL.exists():
        data = build_data(panel, mkt)
        log.info("全歷史特徵：%d 列、%d 檔、%s～%s", len(data), data["code"].nunique(),
                 data["date"].min().date(), data["date"].max().date())
        preds = walk_forward(data)
        del data
        st.DIR.mkdir(parents=True, exist_ok=True)
        w = preds.copy()
        w["date"] = pd.to_datetime(w["date"]).dt.strftime("%Y-%m-%d")
        w.round(5).to_csv(PRED_FULL, index=False, compression="gzip")
    preds = pd.read_csv(PRED_FULL, dtype={"code": str}, parse_dates=["date"])
    cur = st.current_pf()
    out = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "params": cur,
           "codes": len(panel), "note": "已下市股票查不到（存活者偏誤），數字可能偏樂觀；模型約每年重訓一次"}
    sim = st.Sim(preds, panel, execution="next_open")
    out["next_open"] = evaluate(sim, cur, mkt)
    out["close_same_day"] = evaluate(st.Sim(preds, panel, execution="close"), cur, mkt)
    out["calibration"] = calibration(preds, panel, mkt)
    if tune:
        ch, res = st.tune_portfolio(sim)
        out["tuning"] = st._num(res)
        if ch:
            ch["reason"] = ch["reason"].replace("十年樣本外", "全歷史樣本外（隔日開盤成交）")
            out["change"] = ch
            out["next_open_tuned"] = evaluate(sim, ch["after"], mkt)
    out["seconds"] = round(time.time() - t0)
    RESULT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out


def result() -> dict:
    return json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.exists() else {}
