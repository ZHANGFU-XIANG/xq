"""MacroAgent：國際大事 → 對手中股票的影響、幅度與後續決策。

分工（防幻覺）：
- LLM：只從新聞中辨識國際事件，並對應到固定的國際因子（利率、匯率、油價、美股科技…）與方向，必附新聞編號
- 程式：用過去一年資料估計每檔持股對各因子的敏感度 β，換算「因子變動 1 個標準差」的影響幅度與金額，
        並依規則產生決策建議
"""
from __future__ import annotations

import logging
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from src.agents.verifier import verify
from src.config import settings, stock_meta
from src.llm.client import LLMError, get_client

log = logging.getLogger(__name__)
ASIA = {"^N225"}  # 與台股同時段交易，用同日資料
MIN_CORR = 0.125  # 約 250 個樣本下 5% 顯著水準的相關係數門檻
FACTOR_KEYS = Literal["US_RATES", "USD_TWD", "OIL", "US_TECH", "US_MARKET", "CHINA", "JAPAN", "RISK", "GOLD"]


class MacroEvent(BaseModel):
    event: str = Field(description="國際事件一句話摘要，只能根據新聞，不可加入新聞沒有的數字")
    factor: FACTOR_KEYS = Field(description="此事件最直接影響的國際因子")
    direction: int = Field(ge=-1, le=1, description="事件使該因子上升=1、下降=-1（RISK 上升代表恐慌升高）")
    horizon: Literal["短期", "中期"] = Field(description="短期=數日內，中期=數週")
    news_ids: list[int]


class MacroResult(BaseModel):
    events: list[MacroEvent]


SYSTEM = ("你是總體經濟分析師。只根據提供的新聞，找出會影響全球金融市場的國際大事（央行利率、關稅貿易、地緣政治、"
          "能源、匯率、中國與美國經濟數據等），每件事對應一個國際因子並標示方向與新聞編號。禁止使用新聞以外的資訊。")


def extract_events(news: list[dict]) -> list[dict]:
    client = get_client()
    if not news or not client.available():
        return []
    lines = [f"[{n['id']}] ({n['source']}) {n['title']}｜{n['summary'][:120]}" for n in news]
    try:
        res = client.structured("新聞列表：\n" + "\n".join(lines) + "\n\n請列出最重要的國際大事（最多 8 件）。",
                                MacroResult, system=SYSTEM)
    except LLMError as e:
        log.warning("國際事件辨識失敗：%s", e)
        return []
    by_id = {n["id"]: n for n in news}
    out = []
    for ev in res.events:
        ids = [i for i in ev.news_ids if i in by_id]
        if not ids or not verify(ev.event, [by_id[i] for i in ids])[0]:
            continue
        out.append({"event": ev.event, "factor": ev.factor, "direction": ev.direction, "horizon": ev.horizon,
                    "news": [{"id": i, "title": by_id[i]["title"], "url": by_id[i]["url"]} for i in ids]})
    return out


def _factor_series(us_hist: dict[str, pd.DataFrame], key: str) -> pd.Series | None:
    cfg = settings()["macro_factors"][key]
    df = us_hist.get(cfg["ticker"])
    if df is None or len(df) < 60:
        return None
    s = df.Close.diff() if cfg["mode"] == "diff" else df.Close.pct_change()
    return s.dropna()


def sensitivities(codes: list[str], panel: dict[str, pd.DataFrame], us_hist: dict[str, pd.DataFrame],
                  target: pd.Timestamp) -> dict[str, dict[str, dict]]:
    """{code: {factor: {beta, sigma, impact_1sd_pct}}}。β = 台股日報酬對前一個因子交易日變動的迴歸係數。"""
    out: dict[str, dict[str, dict]] = {}
    factors = {k: _factor_series(us_hist, k) for k in settings()["macro_factors"]}
    for code in codes:
        df = panel.get(code)
        if df is None:
            continue
        r = df.Close.pct_change()
        r = r[r.index < target].tail(250).dropna()
        res = {}
        for key, f in factors.items():
            if f is None:
                continue
            ticker = settings()["macro_factors"][key]["ticker"]
            left = pd.DataFrame({"d": r.index.astype("datetime64[ns]"), "r": r.values})
            right = pd.DataFrame({"fd": f.index.astype("datetime64[ns]"), "f": f.values}).sort_values("fd")
            m = pd.merge_asof(left, right, left_on="d", right_on="fd", allow_exact_matches=ticker in ASIA).dropna()
            m = m[~m["fd"].duplicated(keep="first")]
            if len(m) < 60 or m["f"].var() == 0:
                continue
            beta = float(np.cov(m["r"], m["f"])[0, 1] / m["f"].var())
            sigma = float(m["f"].std())
            res[key] = {"beta": round(beta, 4), "sigma": round(sigma, 5), "impact_1sd_pct": round(beta * sigma * 100, 2),
                        "corr": round(float(m["r"].corr(m["f"])), 2)}
        out[code] = res
    return out


def impacts(events: list[dict], holdings: dict[str, float], sens: dict[str, dict[str, dict]],
            nav: float, stops: dict[str, float], prices: dict[str, float]) -> list[dict]:
    """每件國際大事 × 每檔持股：方向、幅度（1σ）、金額與決策。holdings = {code: 市值}。"""
    names = settings()["macro_factors"]
    out = []
    for ev in events:
        rows = []
        for code, mv in holdings.items():
            s = sens.get(code, {}).get(ev["factor"])
            if not s:
                continue
            significant = abs(s["corr"]) >= MIN_CORR
            eff = ev["direction"] * s["impact_1sd_pct"] if significant else 0.0  # 事件方向下，因子變動 1σ 的預估影響(%)
            amount = round(mv * eff / 100)
            if not significant:
                decision = f"歷史相關性 {s['corr']:+.2f} 未達統計顯著（|r| < {MIN_CORR}），不列入影響"
            elif abs(eff) < 0.3:
                decision = "影響有限，維持原決策"
            elif eff < 0 and abs(eff) >= 1.0:
                decision = (f"不利且敏感度高：嚴守停損 {stops.get(code, '—')} 元、暫停加碼；"
                            f"若事件延續且跌破月線，先減碼一半")
            elif eff < 0:
                decision = "輕度不利：列入觀察，暫不調整部位"
            else:
                decision = "有利：支持續抱；若一個月模型仍在前段班，可作為加碼依據"
            rows.append({"code": code, "name": stock_meta(code)["name"], "beta": s["beta"], "corr": s["corr"],
                         "impact_pct": round(eff, 2), "amount": amount, "decision": decision})
        total = sum(r["amount"] for r in rows)
        out.append({**ev, "factor_name": names[ev["factor"]]["name"], "rows": rows, "total_amount": total,
                    "total_pct_nav": round(total / nav * 100, 2) if nav else 0.0})
    return out
