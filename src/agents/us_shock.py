"""USShockAgent：費城半導體（美股）重挫時，台股電子股該續抱多久、什麼情況要開盤先賣（全部規則化，不經 LLM）。

2026-10-12 研究（台股 413 檔流動性電子股、2021-12～2026-08 費半單日 ≤ −3% 共 117 次；
數字為「隔天開盤賣」對比「續抱到第 k 天收盤」的平均差，正數代表續抱較好）：
- 全部：開盤平均跳空 −1.41%；續抱 3 日 +0.12%、5 日 +0.59%、10 日 +1.19%；回到事件前收盤的天數中位數 2 天
  → 一般大跌不要開盤殺低
- 費半兩日合計 ≤ −6%（24 次）：續抱當天 −0.50%、3 日 −1.93%，5 日起才轉正、回本中位 4 天
- 費半跌破 50 日線且加權已在月線下（31 次）：續抱 3 日 −1.64%，20 日 +3.31%
  → 這兩種「嚴重」情況：開盤先賣、約第 3 個交易日收盤前買回（省下約 1.6～1.9%，扣來回成本 0.585% 仍有利）
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from src.config import now_tw, stock_meta

log = logging.getLogger(__name__)

STUDY = {
    "period": "2021-12～2026-08", "events": 117, "universe": "台股流動性電子股 413 檔",
    "normal": {"gap": -1.41, "hold3": 0.12, "hold5": 0.59, "hold10": 1.19, "recover_days": 2},
    "two_day": {"n": 24, "hold_day": -0.50, "hold3": -1.93, "hold5": 0.07, "recover_days": 4},
    "trend_break": {"n": 31, "hold3": -1.64, "hold20": 3.31},
}
SHOCK = -0.03       # 費半單日跌幅門檻
TWO_DAY = -0.06     # 兩日合計
REBUY_DAYS = 3      # 嚴重時開盤賣、第 3 個交易日買回
MIN_CORR = 0.15     # 個股與費半連動顯著才列入

_CACHE: dict = {"t": 0.0, "v": None}


def _trading_days_after(d, n: int):
    from src.api.data import add_trading_days

    return add_trading_days(d, n)


def assess() -> dict:
    """最近一個美股交易日的費半狀態與對台股電子股的建議（10 分鐘快取）。"""
    if _CACHE["v"] is not None and time.time() - _CACHE["t"] < 600:
        return _CACHE["v"]
    from src.agents import market_data, on_demand

    today = now_tw().date()
    sox = market_data.us_history().get("^SOX")
    if sox is None or len(sox) < 60:
        return {"level": "無資料", "headline": "抓不到費半資料"}
    s_all = sox[sox.index < pd.Timestamp(today)].Close.dropna()
    tx_all = on_demand.context()["taiex"].Close

    def state(k: int):
        """倒數第 k 個美股交易日（0＝最近）的狀態。"""
        s = s_all.iloc[:len(s_all) - k]
        us_date = s.index[-1].date()
        r1, r2 = float(s.iloc[-1] / s.iloc[-2] - 1), float(s.iloc[-1] / s.iloc[-3] - 1)
        below50 = bool(s.iloc[-1] < s.tail(50).mean())
        first_tw = _trading_days_after(us_date, 1)  # 美股收盤後第一個台股交易日
        tx = tx_all[tx_all.index < pd.Timestamp(first_tw)]
        tx_below20 = bool(tx.iloc[-1] < tx.tail(20).mean())
        two_day = r2 <= TWO_DAY and r1 < 0
        trend = r1 <= SHOCK and below50 and tx_below20
        return us_date, r1, r2, below50, tx_below20, first_tw, two_day, trend

    us_date, r1, r2, below50, tx_below20, first_tw, two_day, trend = state(0)
    if not (two_day or trend):  # 前幾晚的嚴重重挫，在買回日之前持續提醒
        for k in range(1, REBUY_DAYS + 1):
            st_k = state(k)
            if (st_k[6] or st_k[7]) and _trading_days_after(st_k[5], REBUY_DAYS - 1) >= today:
                us_date, r1, r2, below50, tx_below20, first_tw, two_day, trend = st_k
                break
        else:  # 一般重挫：在預期回本日之前持續顯示
            if r1 > SHOCK:
                for k in range(1, 3):
                    st_k = state(k)
                    if st_k[1] <= SHOCK and _trading_days_after(st_k[5], STUDY["normal"]["recover_days"] - 1) >= today:
                        us_date, r1, r2, below50, tx_below20, first_tw, two_day, trend = st_k
                        break
    base = {"us_date": us_date.isoformat(), "sox_ret_pct": round(r1 * 100, 2), "sox_2d_pct": round(r2 * 100, 2),
            "sox_below_ma50": below50, "taiex_below_ma20": tx_below20, "first_tw_day": first_tw.isoformat(), "study": STUDY}
    if r1 > SHOCK and not two_day:
        out = {**base, "level": "正常",
               "headline": f"費半 {us_date:%m/%d} {r1 * 100:+.2f}%，未達重挫門檻（−3%）",
               "advice": "電子股照原計畫操作"}
    elif two_day or trend:
        rebuy = _trading_days_after(first_tw, REBUY_DAYS - 1)
        why = (f"費半兩日合計 {r2 * 100:+.1f}%" if two_day else
               f"費半 {r1 * 100:+.1f}% 且跌破 50 日線、加權已在月線下")
        st = STUDY["two_day"] if two_day else STUDY["trend_break"]
        out = {**base, "level": "嚴重", "rebuy_date": rebuy.isoformat(), "sell_now": True,
               "headline": f"美股嚴重重挫：{why}",
               "advice": (f"高連動電子股 {first_tw:%m/%d} 開盤先賣，{rebuy:%m/%d}（第 {REBUY_DAYS} 個交易日）收盤前買回："
                          f"歷史上同樣情況（{st['n']} 次）續抱 3 日平均再少 {abs(st['hold3']):.1f}%，"
                          "扣來回成本約 0.585% 仍划算；5～10 日後通常回到原價")}
    else:
        rec = STUDY["normal"]["recover_days"]
        out = {**base, "level": "一般重挫", "sell_now": False,
               "recover_date": _trading_days_after(first_tw, rec - 1).isoformat(),
               "headline": f"費半 {us_date:%m/%d} {r1 * 100:+.2f}% 重挫",
               "advice": (f"電子股開盤平均跳空約 {STUDY['normal']['gap']:.1f}%，不要開盤殺低：歷史 {STUDY['events']} 次中位數 "
                          f"{rec} 個交易日回到原價，續抱 5 日平均比開盤賣多 +{STUDY['normal']['hold5']:.1f}%、10 日多 "
                          f"+{STUDY['normal']['hold10']:.1f}%；若再出現「兩日合計 −6%」或「費半破 50 日線＋加權破月線」才改成先賣")}
    _CACHE.update(t=time.time(), v=out)
    return out


def exposure(codes: list[str]) -> dict[str, dict]:
    """個股與費半的連動（過去一年日報酬 β 與相關係數）＋依昨晚費半漲跌估的開盤影響。"""
    if not codes:
        return {}
    from src.agents import macro, on_demand

    ctx = on_demand.context()
    a = assess()
    sens = macro.sensitivities(codes, ctx["panel"], ctx["us_hist"], pd.Timestamp(ctx["target"]))
    out = {}
    for c in codes:
        s = (sens.get(c) or {}).get("US_TECH")
        if not s:
            continue
        out[c] = {"beta": round(s["beta"], 2), "corr": round(s["corr"], 2), "linked": abs(s["corr"]) >= MIN_CORR and s["beta"] > 0,
                  "exp_impact_pct": round(s["beta"] * (a.get("sox_ret_pct") or 0), 2)}
    return out


def view(codes: list[str] | None = None) -> dict:
    """前端與 AI 用：整體建議＋每檔（持股或指定代號）的連動與動作。"""
    from src.state import portfolio as pfm

    a = assess()
    codes = codes if codes is not None else list(pfm.build_portfolio().positions)
    ex = exposure(codes) if a.get("level") in ("一般重挫", "嚴重") or codes else {}
    rows = []
    for c in codes:
        e = ex.get(c)
        if not e:
            continue
        if a["level"] == "嚴重" and e["linked"]:
            act = f"{a['first_tw_day'][5:]} 開盤先賣，{a['rebuy_date'][5:]} 收盤前買回"
        elif a["level"] == "一般重挫" and e["linked"]:
            act = f"續抱，不要殺低（歷史中位 {STUDY['normal']['recover_days']} 天回本）"
        elif e["linked"]:
            act = "照原計畫"
        else:
            act = "與費半連動低，不受影響"
        rows.append({"code": c, "name": stock_meta(c)["name"], **e, "action": act})
    rows.sort(key=lambda r: -r["beta"])
    return {**a, "items": rows}
