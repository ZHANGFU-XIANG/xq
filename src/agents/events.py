"""EventAgent：資訊事件（法說會、營收、重大訊息等）造成的「爆量跳空大漲」偵測與建議（規則化，新聞標題只用來說明原因）。

MOPS 的歷史法說會行事曆拿不到，所以用價量辨識資訊衝擊：任何重大消息都會表現成「跳空＋爆量＋收在高點」。
2026-10-12 研究（台股流動性股票、2022-01～2026-09、單日漲 ≥5% 且量 ≥ 20 日均量 2 倍共 15,960 次；
報酬已扣來回成本 0.585%，超額＝減去同期隨機買進的平均：1 日 −0.48%、5 日 −0.03%、20 日 +1.68%）：
- 漲停收盤＋站上季線（4,882 次）：當天收盤買 1 日 +1.25%、5 日 +2.48%、20 日 +6.70%（超額 +5.0%）
  但漲停鎖住時多半買不到；隔天開盤買 20 日 +4.32%（超額約 +2.6%）、5 日 +0.20%
- 未漲停＋跳空 ≥3%＋收在當日高點 80% 以上＋站上季線＋量 ≥4 倍（165 次）：收盤前買 5 日 +1.46%、20 日 +8.10%（超額 +6.4%）
- 未漲停＋收高點＋站上季線（3,100 次）：5 日 +0.59%、20 日 +4.45%（超額 +2.8%）
- 開高走低（收在當日區間 50% 以下）：5 日 −0.20%，沒有優勢 → 不列入
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from src.config import now_tw, stock_meta

log = logging.getLogger(__name__)
SESSION_MIN = 270

TIERS = {
    "A": {"label": "資訊衝擊（買得到）", "verdict": "可以買",
          "rule": "未漲停、跳空 ≥3%、漲 ≥5%、收在高點、量 ≥4 倍、站上季線",
          "stats": "歷史 165 次：收盤前買 5 日 +1.5%、20 日 +8.1%（同期隨機買 20 日 +1.7%）"},
    "L": {"label": "漲停（資訊衝擊）", "verdict": "排隊買／明天開盤買",
          "rule": "漲停、量 ≥2 倍、站上季線",
          "stats": "歷史 4,882 次：買得到時隔天 +1.3%、20 日 +6.7%；買不到改隔天開盤買 20 日 +4.3%（隨機 +1.7%）"},
    "B": {"label": "強勢放量", "verdict": "可以買",
          "rule": "未漲停、漲 ≥5%、收在高點、量 ≥2 倍、站上季線",
          "stats": "歷史 3,100 次：收盤前買 5 日 +0.6%、20 日 +4.5%（隨機 +1.7%）"},
}
HOLD_DAYS = 20

_CACHE: dict = {"t": 0.0, "v": None}
_NEWS: dict = {}


def _headlines(code: str, name: str) -> list[dict]:
    """這檔最近的新聞與重大訊息標題（說明為什麼漲；10 分鐘快取）。"""
    hit = _NEWS.get(code)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = []
    try:
        from src.agents import mops

        out += [{"title": n["title"], "date": n["date"], "source": "公開資訊觀測站", "url": n.get("url")}
                for n in mops.material_news(code, days=7)[:3]]
    except Exception as e:  # noqa: BLE001
        log.info("重大訊息 %s 失敗：%s", code, e)
    try:
        from src.agents import on_demand

        out += [{"title": n.get("title"), "date": (n.get("time") or "")[:10], "source": n.get("source"), "url": n.get("url")}
                for n in on_demand.search_news(f"{name} {code}", 4)[:4]]
    except Exception as e:  # noqa: BLE001
        log.info("新聞 %s 失敗：%s", code, e)
    _NEWS[code] = (time.time(), out[:6])
    return out[:6]


def scan(limit: int = 15) -> dict:
    """全市場掃描今天的資訊事件（盤中 30 秒快取、盤後 10 分鐘）。"""
    from src.agents import alerts, on_demand
    from src.api.data import add_trading_days

    trading = alerts.is_trading_hours()
    if _CACHE["v"] is not None and time.time() - _CACHE["t"] < (30 if trading else 600):
        return _CACHE["v"]
    ctx = on_demand.context()
    now = now_tw()
    today = pd.Timestamp(now.date())
    codes = [c for c in ctx["codes"] if not stock_meta(c).get("is_etf")]
    rt = alerts.realtime(codes)
    minutes = max(5, min(SESSION_MIN, now.hour * 60 + now.minute - 540)) if trading else SESSION_MIN
    rows = []
    for c in codes:
        q = rt.get(c)
        if not q or not q.get("price") or not q.get("open") or not q.get("prev_close"):
            continue
        if str(q.get("date", "")).replace("-", "")[:8] not in ("", now.strftime("%Y%m%d")) and trading:
            continue
        price, op, prev, hi, lo = q["price"], q["open"], q["prev_close"], q.get("high") or q["price"], q.get("low") or q["price"]
        ret, gap = price / prev - 1, op / prev - 1
        if ret < 0.05:
            continue
        df = ctx["panel"].get(c)
        if df is None:
            continue
        d = df[df.index < today]
        if len(d) < 61:
            continue
        v20 = float(d.Volume.tail(20).mean()) / 1000
        vr = (q.get("volume_lots") or 0) / (v20 * minutes / SESSION_MIN) if v20 else 0
        ma60 = float(d.Close.tail(60).mean())
        pos = (price - lo) / (hi - lo) if hi > lo else 1.0
        up = prev > ma60
        if not up or pos < 0.5:
            continue
        if q.get("at_limit_up") and vr >= 2:
            tier = "L"
        elif gap >= 0.03 and pos >= 0.8 and vr >= 4:
            tier = "A"
        elif pos >= 0.8 and vr >= 2:
            tier = "B"
        else:
            continue
        rows.append({"code": c, "name": stock_meta(c)["name"], "sector": stock_meta(c).get("sector"), "tier": tier,
                     "label": TIERS[tier]["label"], "verdict": TIERS[tier]["verdict"], "price": price,
                     "chg_pct": round(ret * 100, 2), "gap_pct": round(gap * 100, 2), "volume_ratio": round(vr, 1),
                     "range_pos": round(pos, 2), "dist_ma60_pct": round((prev / ma60 - 1) * 100, 1),
                     "lot_cost": round(price * 1000), "stats": TIERS[tier]["stats"]})
    order = {"A": 0, "L": 1, "B": 2}
    rows.sort(key=lambda r: (order[r["tier"]], -r["volume_ratio"]))
    rows = rows[:limit]
    entry = now.date() if trading else on_demand.current_target()
    exit_day = add_trading_days(entry, HOLD_DAYS)
    for r in rows:
        if r["tier"] == "L":
            r["plan"] = (f"漲停 {r['price']} 排隊買；沒買到就 {on_demand.current_target() if not trading else '明天'} 開盤直接買，"
                         f"抱約 {HOLD_DAYS} 個交易日（到 {exit_day:%m/%d}）")
        else:
            r["plan"] = f"{'收盤前（13:25 前）' if trading else '下一個交易日開盤'}買進，抱約 {HOLD_DAYS} 個交易日（到 {exit_day:%m/%d}）"
        r["news"] = _headlines(r["code"], r["name"])
    out = {"time": now.strftime("%Y-%m-%d %H:%M:%S"), "trading": trading, "items": rows,
           "counts": {k: sum(1 for r in rows if r["tier"] == k) for k in order},
           "tiers": TIERS, "hold_days": HOLD_DAYS,
           "note": ("價量辨識資訊衝擊（法說會、營收、重大訊息等）；新聞與重大訊息標題用來說明原因，不影響判斷。"
                    "量比盤中以「今日累計量 ÷（20 日均量 × 已開盤時間比例）」估計。研究期 2022～2026 以多頭為主，數字是歷史平均，不是保證。")}
    _CACHE.update(t=time.time(), v=out)
    return out
