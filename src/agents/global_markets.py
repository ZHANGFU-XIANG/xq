"""GlobalMarketsAgent：美、日、韓、陸港、歐股與台股相關 ADR、AI 半導體、商品、匯率、利率的收盤總結＋對持股的估計影響。

全部由程式計算（Yahoo 日 K，經 yahoo_gateway 限流），重點句由規則產生：
- 依「漲跌幅 ÷ 該指數近 60 日日波動」排序，超過 1.5 倍標準差才列為重點（避免每天都說一樣的話）
- 對持股的影響：macro.sensitivities 的因子 β（費半、標普、日經、中國、美元台幣、油價、VIX）× 今天的變動 × 持股市值
"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from src.config import now_tw, settings, stock_meta

log = logging.getLogger(__name__)
GROUPS: dict[str, dict[str, str]] = {
    "美股": {"^GSPC": "標普 500", "^IXIC": "那斯達克", "^SOX": "費城半導體", "^DJI": "道瓊"},
    "亞洲": {"^N225": "日經 225", "^KS11": "韓國綜合", "000300.SS": "滬深 300", "^HSI": "恆生"},
    "歐洲": {"^STOXX50E": "歐洲 STOXX 50", "^GDAXI": "德國 DAX"},
    "台股 ADR／AI 半導體": {"TSM": "台積電 ADR", "UMC": "聯電 ADR", "ASX": "日月光 ADR", "NVDA": "NVIDIA", "AMD": "AMD",
                        "AVGO": "博通", "MU": "美光"},
    "商品／匯率／利率": {"CL=F": "西德州原油", "GC=F": "黃金", "TWD=X": "美元兌台幣", "JPY=X": "美元兌日圓",
                   "KRW=X": "美元兌韓元", "^TNX": "美國 10 年債殖利率", "^VIX": "VIX"},
}
_CACHE: dict = {}


def _fetch() -> dict[str, pd.DataFrame]:
    from src.data import yahoo_gateway as yg

    hit = _CACHE.get("d")
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    syms = [s for g in GROUPS.values() for s in g]
    d = yg.download(syms, period="6mo", interval="1d")
    _CACHE["d"] = (time.time(), d)
    return d


def table() -> list[dict]:
    d = _fetch()
    rows = []
    for group, syms in GROUPS.items():
        for s, name in syms.items():
            df = d.get(s)
            if df is None or "Close" not in df:
                continue
            c = df["Close"].dropna()
            if len(c) < 30:
                continue
            r = c.pct_change().dropna()
            chg = float(c.iloc[-1] / c.iloc[-2] - 1)
            vol = float(r.tail(60).std()) or 1e-9
            rows.append({"group": group, "ticker": s, "name": name, "close": round(float(c.iloc[-1]), 4),
                         "date": str(c.index[-1].date()), "chg_pct": round(chg * 100, 2),
                         "chg_5d_pct": round(float(c.iloc[-1] / c.iloc[-6] - 1) * 100, 2) if len(c) > 6 else None,
                         "z": round(chg / vol, 2)})
    return rows


def highlights(rows: list[dict]) -> list[str]:
    hot = sorted([r for r in rows if abs(r["z"]) >= 1.5], key=lambda r: -abs(r["z"]))[:6]
    out = [f"{r['name']} {r['chg_pct']:+.2f}%（約 {abs(r['z']):.1f} 倍平常波動，{'大漲' if r['z'] > 0 else '大跌'}）" for r in hot]
    us = {r["ticker"]: r for r in rows}
    if "^SOX" in us and "TSM" in us:
        out.append(f"費半 {us['^SOX']['chg_pct']:+.2f}%、台積電 ADR {us['TSM']['chg_pct']:+.2f}%：台股電子權值股開盤方向參考")
    if not hot:
        out.insert(0, "國際市場沒有異常波動（沒有任何指數漲跌超過 1.5 倍平常波動）")
    return out


def impact(rows: list[dict]) -> dict:
    """持股 × 因子 β × 今天因子變動。"""
    from src.agents import macro, on_demand
    from src.state import portfolio as pfm

    pf = pfm.build_portfolio()
    if not pf.positions:
        return {"total": 0, "items": []}
    ctx = on_demand.context()
    codes = list(pf.positions)
    sens = macro.sensitivities(codes, ctx["panel"], ctx["us_hist"], pd.Timestamp(now_tw().date() + pd.Timedelta(days=1)))
    by_t = {r["ticker"]: r for r in rows}
    fac = settings()["macro_factors"]
    moves = {k: by_t[v["ticker"]]["chg_pct"] / 100 for k, v in fac.items() if v["ticker"] in by_t and v.get("mode") == "pct"}
    items, total = [], 0.0
    for c, p in pf.positions.items():
        px = float(ctx["panel"][c].Close.iloc[-1]) if c in ctx["panel"] else p.avg_cost
        mv = p.shares * px
        parts = {k: (sens.get(c, {}).get(k) or {}).get("beta", 0.0) * m for k, m in moves.items()}
        est = sum(parts.values())
        if not np.isfinite(est):
            continue
        top = max(parts, key=lambda k: abs(parts[k])) if parts else None
        items.append({"code": c, "name": stock_meta(c)["name"], "est_pct": round(est * 100, 2), "amount": round(mv * est),
                      "main_factor": fac[top]["name"] if top else None})
        total += mv * est
    items.sort(key=lambda x: x["amount"])
    return {"total": round(total), "items": items,
            "basis": "各持股對國際因子的歷史 β（近 250 日）× 今天的變動；只是估計，同一天多個因子會互相抵銷"}


def run() -> dict:
    rows = table()
    try:
        imp = impact(rows)
    except Exception as e:  # noqa: BLE001
        log.warning("國際影響估計失敗：%s", e)
        imp = {"total": None, "items": [], "error": str(e)[:120]}
    return {"time": now_tw().strftime("%m/%d %H:%M"), "rows": rows, "highlights": highlights(rows), "impact": imp}
