"""ConferenceAgent：法說會行事曆（公開資訊觀測站 t100sb02_1）＋歷史事件研究的結論（程式整理，不經 LLM）。

- 即將召開：持股與排名前段股票，未來 N 天內的法說會（公司通常 2～3 週前公告）
- 剛開完：反應日（下午 1:30 後召開算隔一個交易日）漲跌，跌 ≥3% 的持股標示
2026-10-14 事件研究（2011-01～2026-09，24,800 場、日均成交值 > 3,000 萬的股票；報酬扣來回 0.585%，
超額＝減去同期全部股票等權平均）：
- 法說會前 5 個交易日開盤買、前一天收盤賣：平均超額 +0.44%，16 年每年都是正的；但中位數 −0.18%
  （大部分沒有動，少數先漲拉高平均）。前 10 個交易日：+0.65%（16 年中 14 年為正）
- 反應日隔天開盤才買：5 日 −0.06%、20 日 0.00% → 追法說會利多沒有優勢
- 反應日收盤前買（當天漲 ≥5%、量 ≥2 倍）：5 日超額 +1.1%（前後兩段 +1.3%／+1.1%）→ 和 EventAgent 的「資訊衝擊」相同
- 反應日跌 ≥3%：之後 20 日超額 −1.0%（前後兩段 −0.9%／−1.0%）
- 全部法說會之後 20 日：超額 −0.8%（16 年中 13 年為負）
模型特徵驗證（距下次／上次法說會天數、反應日漲跌，2013-01～2026-10 樣本外、隔日開盤成交）：
只用價量 30.8% → 加法說會 25.5%（前半 +7.4、後半 +1.7 個百分點，但全期 −5.4、最大回檔 −64% → −82%）→ 不採用，
法說會只做日曆提醒與統計說明（data/strategy/research_conference.json）。
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd

from src.config import now_tw, stock_meta

log = logging.getLogger(__name__)
_CACHE: dict = {}
STATS = {
    "pre": "歷史上法說會前 5 個交易日平均比大盤多 0.4%（扣成本，16 年每年為正；但中位數為負，多數沒有動）",
    "chase": "法說會後隔天開盤才追：之後 5 日、20 日平均沒有超額，不建議追",
    "drop": "反應日跌 ≥3% 的股票，之後 20 日平均比大盤少 1.0%（前後兩段都一樣）",
    "pop": "反應日收盤前漲 ≥5%、量 ≥2 倍：之後 5 日平均比大盤多 1.1%（要在當天收盤前買）",
}


def calendar(months_ahead: int = 1) -> pd.DataFrame:
    """本月起 months_ahead 個月的法說會（上市＋上櫃）；6 小時快取。"""
    from src.data import history_store as hs

    hit = _CACHE.get("cal")
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    today = now_tw().date()
    frames = []
    y, m = today.year, today.month
    for _ in range(months_ahead + 1):
        for mk in ("sii", "otc"):
            try:
                frames.append(hs.conference_month(y, m, mk, refresh=True))
            except Exception as e:  # noqa: BLE001
                log.warning("法說會行事曆 %s %d/%d 失敗：%s", mk, y, m, e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    df = pd.concat([f for f in frames if len(f)], ignore_index=True) if any(len(f) for f in frames) else \
        pd.DataFrame(columns=["code", "date", "time", "summary", "file_date"])
    df = df.drop_duplicates(["code", "date"]).sort_values(["date", "time"]).reset_index(drop=True)
    if len(df) or not hit:
        _CACHE["cal"] = (time.time(), df)
    return df if len(df) or not hit else hit[1]


def _react_day(d: dt.date, t: str) -> str:
    hh = str(t)[:2]
    return "隔天" if (not hh.isdigit() or int(hh) >= 13) else "當天"


def upcoming(codes: list[str], days: int = 14, today: dt.date | None = None) -> list[dict]:
    today = today or now_tw().date()
    df = calendar()
    if df.empty:
        return []
    want = set(codes)
    out = []
    for x in df.itertuples():
        d = dt.date.fromisoformat(str(x.date)[:10])
        if x.code in want and today <= d <= today + dt.timedelta(days=days):
            out.append({"code": x.code, "name": stock_meta(x.code).get("name") or "", "date": d.isoformat(),
                        "time": x.time, "react": _react_day(d, x.time), "days": (d - today).days,
                        "summary": str(x.summary)[:80] if str(x.summary) != "nan" else ""})
    return out


def recent(codes: list[str], days: int = 7, today: dt.date | None = None) -> list[dict]:
    """最近 days 天內開完的法說會與反應日漲跌（用 on_demand 日 K）。"""
    from src.agents import on_demand

    today = today or now_tw().date()
    df = calendar()
    if df.empty:
        return []
    panel = on_demand.context().get("panel") or {}
    out = []
    for x in df.itertuples():
        d = dt.date.fromisoformat(str(x.date)[:10])
        if x.code not in codes or not (today - dt.timedelta(days=days) <= d <= today):
            continue
        rec = {"code": x.code, "name": stock_meta(x.code).get("name") or "", "date": d.isoformat(), "time": x.time,
               "react_pct": None}
        k = panel.get(x.code)
        if k is not None and len(k):
            idx = k.index
            i = idx.searchsorted(pd.Timestamp(d))
            if i < len(idx) and idx[i].date() == d and _react_day(d, x.time) == "隔天":
                i += 1
            if 1 <= i < len(idx):
                rec["react_date"] = idx[i].date().isoformat()
                rec["react_pct"] = round(float(k["Close"].iat[i] / k["Close"].iat[i - 1] - 1) * 100, 2)
        out.append(rec)
    return out


def run(picks: list[str] | None = None) -> dict:
    from src.state import portfolio as pfm

    held = list(pfm.build_portfolio().positions)
    picks = [c for c in (picks or []) if c not in held]
    up_held, up_picks = upcoming(held), upcoming(picks)
    rec = recent(held + picks)
    alerts = []
    for r in rec:
        if r["code"] in held and r.get("react_pct") is not None and r["react_pct"] <= -3:
            alerts.append({"level": "注意", "code": r["code"],
                           "title": f"{r['code']} {r['name']} 法說會後反應日跌 {r['react_pct']:.1f}%",
                           "body": STATS["drop"] + "（僅資訊，持股建議以系統為準）"})
    return {"time": now_tw().strftime("%m/%d %H:%M"), "held": up_held, "picks": up_picks, "recent": rec,
            "alerts": alerts, "stats": STATS,
            "note": "法說會日期來自公開資訊觀測站；歷史統計只是平均，大部分公司法說會前後沒有明顯變動"}
