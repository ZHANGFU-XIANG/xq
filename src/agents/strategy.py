"""StrategyAgent：綜合評分（全部程式計算），並產生「為何選這檔」的理由。

主軸是「未來一個月創新高／超額報酬」模型，再以法人籌碼、月營收、隔夜美股預測、新聞做加減分。
各因子分數範圍 -1 ~ +1，權重見 settings.yaml score_weights（調整 Agent 可修改）。
"""
from __future__ import annotations

import numpy as np

from src.agents.alpha_model import explain, explain_def
from src.config import stock_meta
from src.state import params

COMPONENT_NAMES = {"alpha": "月超額報酬模型", "newhigh": "創新高機率", "institutional": "法人籌碼",
                   "revenue": "月營收", "overnight": "隔夜美股", "sentiment": "新聞情緒"}


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(np.clip(x, lo, hi))


def score_stock(code: str, feat: dict, alpha: dict | None, pred: dict | None, senti: dict | None,
                revenue: dict | None, us_snap: dict[str, dict], base_rate: float) -> dict:
    meta = stock_meta(code)
    reasons: list[str] = []
    risks: list[str] = []
    comps = {k: 0.0 for k in COMPONENT_NAMES}

    # 1) 一個月模型
    if alpha:
        comps["alpha"] = _clip(alpha["alpha_rank"] * 2 - 1)
        comps["newhigh"] = _clip(alpha["nh_rank"] * 2 - 1)
        txt = (f"一個月模型：創 52 週新高機率 {alpha['p_newhigh'] * 100:.1f}%（全市場基準 {base_rate * 100:.1f}%，"
               f"排名前 {(1 - alpha['nh_rank']) * 100:.0f}%），預期超額報酬 {alpha['exp_excess'] * 100:+.2f}%"
               f"（排名前 {(1 - alpha['alpha_rank']) * 100:.0f}%）")
        (reasons if alpha["combo"] >= 0.5 else risks).append(txt)
        for line in explain(alpha):
            (risks if "過熱" in line else reasons).append(line)

    # 2) 法人
    if feat:
        comps["institutional"] = _clip(feat["inst_net5_ratio"] / 0.05)
        lots5 = feat["inst_net5"] / 1000
        if abs(lots5) >= 1:
            txt = (f"籌碼面：三大法人近 5 日{'買超' if lots5 > 0 else '賣超'} {abs(lots5):,.0f} 張"
                   f"（外資 {feat['foreign_net5'] / 1000:+,.0f} 張、投信 {feat['trust_net5'] / 1000:+,.0f} 張）")
            if feat["inst_buy_streak"] >= 3:
                txt += f"，連續買超 {feat['inst_buy_streak']} 日"
            (reasons if comps["institutional"] > 0 else risks).append(txt)

    # 3) 月營收
    if revenue:
        comps["revenue"] = _clip(revenue["yoy"] / 0.3)
        txt = (f"基本面：{revenue['month']} 月營收 {revenue['revenue_100m']} 億元，年增 {revenue['yoy'] * 100:+.1f}%、"
               f"月增 {revenue['mom'] * 100:+.1f}%" + (f"，近 3 月累計年增 {revenue['yoy_3m'] * 100:+.1f}%"
                                                      if revenue.get("yoy_3m") is not None else ""))
        (reasons if revenue["yoy"] > 0 else risks).append(txt)

    # 4) 隔夜美股 → 開盤／盤中（開盤後才進場，以開盤後走勢為主）
    if pred:
        g_gap = _clip(pred["gap_pct"] / max(pred["gap_band_pct"], 0.3)) * (1.0 if pred["reliable"] else 0.3)
        g_intra = _clip(pred["intraday_pct"] / max(pred.get("intra_band_pct") or 1.0, 0.3)) * (
            1.0 if pred.get("intra_reliable") else 0.3)
        comps["overnight"] = 0.4 * g_gap + 0.6 * g_intra
        drivers = "、".join(f"{us_snap[d]['name']} {us_snap[d]['change_pct']:+.2f}%"
                            for d in meta.get("drivers", []) if d in us_snap)
        txt = (f"隔夜美股：{drivers}；預測開盤 {pred['gap_pct']:+.2f}%（{pred['direction']}，命中率 "
               f"{pred['gap_hit_rate'] * 100:.1f}%）、開盤後 {pred['intraday_pct']:+.2f}%（{pred['after_open']}）")
        (reasons if comps["overnight"] > 0 else risks).append(txt)

    # 5) 新聞
    if senti:
        comps["sentiment"] = _clip(senti["score"] / 2) * (1.0 if senti["n_sources"] >= 2 else 0.6)
        txt = "新聞：" + "；".join(senti["events"][:2])
        (reasons if comps["sentiment"] >= 0 else risks).append(txt)

    w = params.score_weights()
    composite = sum(w.get(k, 0) * v for k, v in comps.items())
    return {"code": code, "name": meta["name"], "sector": meta["sector"], "score": round(composite, 3),
            "components": {k: round(v, 3) for k, v in comps.items()}, "reasons": reasons, "risks": risks,
            "features": feat, "prediction": pred, "alpha": alpha, "revenue": revenue}


DEF_NAMES = {"up": "收正報酬機率", "drawdown": "回檔小", "return": "預期報酬", "lowvol": "低波動", "institutional": "法人籌碼", "revenue": "營收穩定"}


def score_defense(s: dict, up_base: float) -> dict:
    """穩健籃評分（同一檔股票也會有進攻分數；最後依兩籃名次分配到其中一籃）。"""
    a, rev = s.get("alpha"), s.get("revenue")
    comps = {k: 0.0 for k in DEF_NAMES}
    reasons, risks = [], []
    if a:
        # 收正與報酬用長期（約 40 日＝到比賽結束）模型，回檔用一個月模型；舊快取沒有長期欄位時退回一個月
        comps["up"] = _clip(a.get("up_long_rank", a["up_rank"]) * 2 - 1)
        comps["drawdown"] = _clip(a["dd_rank"] * 2 - 1)
        comps["return"] = _clip(a.get("ret_long_rank", a["alpha_rank"]) * 2 - 1)
        vol = (a.get("features") or {}).get("vol20")
        if vol is not None:
            comps["lowvol"] = _clip((0.025 - vol) / 0.015)
        reasons += explain_def(a, up_base)
    comps["institutional"] = s["components"].get("institutional", 0.0)
    if rev:
        comps["revenue"] = _clip(rev["yoy"] / 0.2) if rev["yoy"] > -0.1 else -1.0
    if comps["institutional"] < 0:
        risks += [r for r in s["risks"] if r.startswith("籌碼面")]
    if rev and rev["yoy"] < 0:
        risks += [r for r in s["risks"] if r.startswith("基本面")]
    else:
        reasons += [r for r in s["reasons"] if r.startswith(("基本面", "籌碼面"))]
    w = params.get("defense_weights")
    total = sum(w.values()) or 1
    score = sum(w.get(k, 0) / total * v for k, v in comps.items())
    return {"def_score": round(score, 3), "def_components": {k: round(v, 3) for k, v in comps.items()},
            "def_reasons": reasons, "def_risks": risks, "eligible_def": bool(a and a.get("eligible_def"))}


def _overheated(s: dict) -> str | None:
    ac = params.basket("offense")["anti_chase"]
    f = (s.get("alpha") or {}).get("features") or {}
    if f.get("rsi14") is not None and f["rsi14"] > ac["max_rsi"]:
        return f"RSI14 = {f['rsi14']:.1f} > {ac['max_rsi']}，過熱不追"
    if f.get("r20") is not None and f["r20"] > ac["max_r20"]:
        return f"近 20 日已漲 {f['r20'] * 100:+.1f}% > {ac['max_r20'] * 100:.0f}%，漲幅過大不追"
    return None


def rank(scored: list[dict]) -> list[dict]:
    """設定兩籃訊號：signal（進攻籃）與 def_signal（穩健籃）。"""
    st = params.strategy()
    off_th, def_th = params.basket("offense")["buy_threshold"], params.basket("defense")["buy_threshold"]
    for s in scored:
        hot = _overheated(s)
        s["overheat"] = hot
        if hot and s["score"] >= off_th:
            s["signal"] = "過熱不追"
            s["risks"] = s["risks"] + [hot]
        else:
            s["signal"] = ("買進候選" if s["score"] >= off_th
                           else "偏空" if s["score"] <= st["sell_score_threshold"] else "觀望")
        s["def_signal"] = "買進候選" if s.get("eligible_def") and s.get("def_score", -1) >= def_th else "不符資格"
    return sorted(scored, key=lambda s: -s["score"])
