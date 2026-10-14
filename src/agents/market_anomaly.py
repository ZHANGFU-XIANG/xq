"""MarketAnomalyAgent：分析台股市場波動是否異常（全部由程式計算，盤中用證交所即時資料）。

1. 大盤：今天漲跌相當於幾倍平常波動（z＝報酬 ÷ 近 60 日日報酬標準差）、20 日實現波動率（年化）在近 10 年的百分位、
   開盤跳空與日內振幅
2. 市場寬度：選股池上漲／下跌家數、漲跌停家數、漲跌超過 5% 的比例
3. 產業：各產業今天漲跌中位數，找出偏離大盤最多的產業（資金輪動或產業利空）
4. 個股：漲跌超過自己平常 4 倍波動、且量能放大 3 倍以上的股票（排除已在資訊事件掃描裡的漲停利多）
5. 成交值：選股池今天成交值和 20 日平均相比
6. 歷史對照（延遲一天、隔日開盤才動作）：波動率前 10% 或單日 |z| ≥ 3 之後，加權 20 日報酬與跌逾 5% 機率 vs 平常
狀態：平靜（波動率百分位 < 20）／正常／波動升高（≥ 70 或 |z| ≥ 2）／極端（≥ 95 或 |z| ≥ 3）；只提醒，不改建議。
"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from src.config import now_tw, stock_meta

log = logging.getLogger(__name__)
_CACHE: dict = {}


def _taiex_hist() -> pd.DataFrame:
    from src.agents import risk_lab

    d = risk_lab._swan_data().get("^TWII")
    if d is None or d.empty:
        from src.agents import on_demand

        d = on_demand.context()["taiex"]
    return d.dropna(subset=["Close"])


def _context_stats(tw: pd.DataFrame, h: int = 20) -> dict:
    """波動率前 10% 或 |z| ≥ 3 的日子之後（隔日開盤進場）20 日報酬 vs 全部。"""
    c, o, lo = tw.Close, tw.Open.where(tw.Open > 0), tw.Low
    r = c.pct_change()
    vol20 = r.rolling(20).std() * np.sqrt(252)
    pct = vol20.rolling(2520, min_periods=500).rank(pct=True)
    z = r / r.rolling(60).std().shift(1)
    nxt_open = o.shift(-1)
    fwd = c.shift(-h) / nxt_open - 1
    dd = lo[::-1].rolling(h, min_periods=h).min()[::-1].shift(-1) / nxt_open - 1
    base = pd.DataFrame({"fwd": fwd, "dd": dd}).dropna()

    def stat(mask, name):
        ev = base[mask.reindex(base.index).fillna(False)]
        if len(ev) < 10:
            return {"name": name, "n": int(len(ev))}
        return {"name": name, "n": int(len(ev)), "fwd_mean": round(float(ev.fwd.mean()) * 100, 2),
                "p_dd5": round(float((ev.dd <= -0.05).mean()), 3)}

    return {"base": {"fwd_mean": round(float(base.fwd.mean()) * 100, 2), "p_dd5": round(float((base.dd <= -0.05).mean()), 3)},
            "high_vol": stat(pct >= 0.9, "波動率前 10%"), "big_down": stat(z <= -3, "單日跌超過 3 倍平常波動"),
            "big_up": stat(z >= 3, "單日漲超過 3 倍平常波動"), "vol_pct_series": pct, "z_series": z}


def snapshot() -> dict:
    hit = _CACHE.get("v")
    from src.agents import alerts, on_demand

    trading = alerts.is_trading_hours()
    if hit and time.time() - hit[0] < (60 if trading else 900):
        return hit[1]
    ctx = on_demand.context()
    tw = _taiex_hist()
    st = _CACHE.get("ctx")
    if not st or time.time() - st[0] > 6 * 3600:
        st = (time.time(), _context_stats(tw))
        _CACHE["ctx"] = st
    cs = st[1]
    r = tw.Close.pct_change()
    sd60 = float(r.tail(61).iloc[:-1].std())
    # 今天（盤中用即時加權）
    live = None
    try:
        from src.agents.intraday import _taiex_rt

        live = _taiex_rt()
    except Exception:  # noqa: BLE001
        pass
    today_ret = (live["chg_pct"] / 100) if live and trading else float(r.iloc[-1])
    z_today = today_ret / sd60 if sd60 else 0.0
    vol_pct = float(cs["vol_pct_series"].dropna().iloc[-1])
    vol20 = float(r.tail(20).std() * np.sqrt(252))
    # 市場寬度與產業（選股池即時報價）
    codes = [c for c in ctx["codes"] if not stock_meta(c).get("is_etf")]
    rt = alerts.realtime(codes)
    rets, sectors, big = {}, {}, []
    tv_today = 0.0
    for c in codes:
        q = rt.get(c) or {}
        if not q.get("price") or not q.get("prev_close"):
            continue
        ret = q["price"] / q["prev_close"] - 1
        rets[c] = ret
        sectors.setdefault(stock_meta(c).get("sector") or "其他", []).append(ret)
        tv_today += (q.get("volume_lots") or 0) * 1000 * q["price"]
        df = ctx["panel"].get(c)
        if df is not None and len(df) > 80:
            sd = float(df.Close.pct_change().tail(60).std())
            v20 = float(df.Volume.tail(20).mean()) / 1000
            vr = (q.get("volume_lots") or 0) / v20 if v20 else 0
            if sd and abs(ret) >= 4 * sd and vr >= 3 and not q.get("at_limit_up"):
                big.append({"code": c, "name": stock_meta(c)["name"], "chg_pct": round(ret * 100, 2),
                            "sigma": round(ret / sd, 1), "volume_ratio": round(vr, 1)})
    n = len(rets)
    up = sum(1 for v in rets.values() if v > 0)
    down = sum(1 for v in rets.values() if v < 0)
    lim_up = sum(1 for c in rets if (rt.get(c) or {}).get("at_limit_up"))
    lim_dn = sum(1 for c in rets if (rt.get(c) or {}).get("at_limit_down"))
    mkt = float(np.median(list(rets.values()))) if rets else 0.0
    sec = sorted(({"sector": s, "median_pct": round(float(np.median(v)) * 100, 2), "n": len(v),
                   "vs_market": round((float(np.median(v)) - mkt) * 100, 2)} for s, v in sectors.items() if len(v) >= 5),
                 key=lambda x: -abs(x["vs_market"]))
    tv_hist = []
    for c in codes[:400]:
        df = ctx["panel"].get(c)
        if df is not None and len(df) > 25:
            tv_hist.append((df.Close * df.Volume).tail(21).iloc[:-1].mean())
    tv_ratio = None
    if tv_hist and tv_today:
        sample = sum((rt.get(c) or {}).get("volume_lots", 0) * 1000 * (rt.get(c) or {}).get("price", 0) for c in codes[:400])
        tv_ratio = round(sample / sum(tv_hist), 2) if sum(tv_hist) else None
    level = ("極端" if vol_pct >= 0.95 or abs(z_today) >= 3 else "波動升高" if vol_pct >= 0.7 or abs(z_today) >= 2
             else "平靜" if vol_pct < 0.2 else "正常")
    hist_lines = []
    for k in ("high_vol", "big_down", "big_up"):
        s = cs[k]
        if s.get("fwd_mean") is not None:
            hist_lines.append(f"{s['name']}之後（隔日開盤買）20 日加權平均 {s['fwd_mean']:+.2f}%（平常 {cs['base']['fwd_mean']:+.2f}%），"
                              f"20 日內跌逾 5% 機率 {s['p_dd5'] * 100:.0f}%（平常 {cs['base']['p_dd5'] * 100:.0f}%），共 {s['n']} 次")
    from src.state import portfolio as pfm

    held = list(pfm.build_portfolio().positions)
    hot_secs = {s["sector"] for s in sec[:3]}
    exposure = [{"code": c, "name": stock_meta(c)["name"], "sector": stock_meta(c).get("sector")} for c in held
                if stock_meta(c).get("sector") in hot_secs]
    alerts_ = []
    if level in ("波動升高", "極端"):
        alerts_.append({"key": f"anomaly-{level}", "level": "嚴重" if level == "極端" else "注意",
                        "title": f"台股波動{level}：加權 {today_ret * 100:+.2f}%（約 {z_today:+.1f} 倍平常波動）、波動率百分位 {vol_pct * 100:.0f}",
                        "body": "；".join(hist_lines[:2]) + "（只提醒，不改建議）"})
    if n and lim_dn >= 30:
        alerts_.append({"key": "anomaly-limitdown", "level": "嚴重", "title": f"選股池跌停 {lim_dn} 家",
                        "body": "恐慌性賣壓：跌停股掛賣不易成交，持股若在名單內請提早掛單"})
    out = {"time": now_tw().strftime("%m/%d %H:%M"), "trading": trading, "level": level,
           "taiex": {"ret_pct": round(today_ret * 100, 2), "z": round(z_today, 2), "vol20_pct": round(vol20 * 100, 1),
                     "vol_percentile": round(vol_pct * 100, 1)},
           "breadth": {"n": n, "up": up, "down": down, "limit_up": lim_up, "limit_down": lim_dn,
                       "big_move_ratio": round(sum(1 for v in rets.values() if abs(v) >= 0.05) / n, 3) if n else None,
                       "median_pct": round(mkt * 100, 2)},
           "turnover_ratio": tv_ratio, "sectors": sec[:8], "stocks": sorted(big, key=lambda x: -abs(x["sigma"]))[:12],
           "history": hist_lines, "holding_exposure": exposure, "alerts": alerts_,
           "note": "歷史對照用 1997 年起加權指數、隔日開盤才動作；只提醒、不改建議"}
    _CACHE["v"] = (time.time(), out)
    return out
