"""DebateAgent：持股與前 5 名推薦的多空辯論。數字全部來自程式算好的「事實清單」，LLM 只能引用事實編號寫論點。

流程：
1. 事實（程式產生，每則有正負方向與權重）：模型預期報酬、收正機率、真實勝率表、目前損益、壓力測試 β、
   產業新聞分數、論壇風向、負面新聞、系統結論與出場條件
2. 多方／空方論點：LLM 一次處理全部股票，每個論點必須列出引用的事實編號；引用不存在或數字對不上（Verifier）的論點刪除
   LLM 不可用時直接用事實文字當論點
3. 評審（規則）：多方權重和 vs 空方權重和 → 偏多／偏空／拉鋸；主要分歧＝權重最大的多空事實；
   「什麼情況會改變結論」＝系統既有的停損、結單轉股條件
結論仍以系統規則（持股建議、綜合排名）為準，辯論只負責把理由攤開。
"""
from __future__ import annotations

import logging
import time

from pydantic import BaseModel, Field

from src.config import now_tw, stock_meta

log = logging.getLogger(__name__)
_CACHE: dict = {}


class Point(BaseModel):
    text: str = Field(description="一句論點，只能使用事實中的數字")
    fact_ids: list[int]


class StockDebate(BaseModel):
    code: str
    bull: list[Point]
    bear: list[Point]


class DebateList(BaseModel):
    items: list[StockDebate]


def facts_for(code: str, item: dict | None, pick: dict | None, industry: dict, forum: dict) -> list[dict]:
    """[{id, text, sign, weight}]：sign +1 支持多方、−1 支持空方、0 中性。"""
    fs: list[dict] = []

    def add(text, sign, weight):
        fs.append({"id": len(fs) + 1, "text": text, "sign": sign, "weight": weight})

    m = (item or {}).get("model") or ({"exp_ret": pick.get("exp_ret"), "p_up": pick.get("p_up")} if pick else {})
    if m.get("exp_ret") is not None:
        e = m["exp_ret"] * 100
        add(f"一個月模型預期報酬 {e:+.1f}%", 1 if e > 1 else -1 if e < 0 else 0, min(abs(e) / 5, 2))
    if m.get("p_up") is not None:
        add(f"模型估計一個月收正機率 {m['p_up'] * 100:.0f}%", 1 if m["p_up"] > 0.55 else -1 if m["p_up"] < 0.45 else 0, 0.8)
    add("歷史上排名前 5 的推薦 20 天後賺錢機率約 50%、平均 +2.1%（優勢來自賺多賠少，不是勝率）", 0, 0)
    if item:
        add(f"目前損益 {item['pnl_pct']:+.1f}%、占總資產 {item['weight_pct']:.1f}%", 0, 0)
        add(f"系統結論：{item['verdict']}（{(item.get('reasons') or [''])[0][:60]}）",
            1 if item["verdict"] in ("續抱", "加碼") else -1, 3.0)  # 系統結論權重最高：辯論傾向不得和持股建議矛盾
        for cnd in (item.get("conditions") or [])[:2]:
            add(f"出場條件：{cnd[:70]}", 0, 0)
    if pick:
        add(f"綜合排名第 {pick.get('rank')} 名，綜合預期 {pick.get('score', 0):+.1f}%", 1, 1.2)
    sec = stock_meta(code).get("sector")
    s = (industry.get("sectors") or {}).get(sec)
    if s and (s["pos"] or s["neg"]):
        add(f"{sec}產業新聞：利多 {s['pos']} 則、利空 {s['neg']} 則（可信度加權分數 {s['score']:+.1f}）",
            1 if s["score"] > 0.5 else -1 if s["score"] < -0.5 else 0, min(abs(s["score"]) / 3, 1))
    f = next((x for x in (forum.get("focus") or []) + (forum.get("hot") or []) if x["code"] == code), None)
    if f:
        add(f"PTT 近 2 天提及 {f['mentions']} 次、推文 {f['push']}、多空方向 {f['direction']:+.1f}（論壇僅供參考）",
            1 if f["direction"] > 0.3 else -1 if f["direction"] < -0.3 else 0, 0.3)
    return fs


def judge(code: str, fs: list[dict], bull: list[dict], bear: list[dict], item: dict | None) -> dict:
    pos = sum(f["weight"] for f in fs if f["sign"] > 0)
    neg = sum(f["weight"] for f in fs if f["sign"] < 0)
    lean = "偏多" if pos > neg * 1.3 else "偏空" if neg > pos * 1.3 else "拉鋸"
    tp = max((f for f in fs if f["sign"] > 0), key=lambda f: f["weight"], default=None)
    tn = max((f for f in fs if f["sign"] < 0), key=lambda f: f["weight"], default=None)
    change = [c for c in (item or {}).get("conditions", [])[:2]] or ["綜合排名掉出前 20 名或出現更好的替代標的（結單轉股規則）"]
    return {"code": code, "name": stock_meta(code).get("name"), "lean": lean, "bull_weight": round(pos, 2),
            "bear_weight": round(neg, 2), "bull": bull, "bear": bear, "facts": fs,
            "key_dispute": f"多方最強：{tp['text'] if tp else '無'}；空方最強：{tn['text'] if tn else '無'}",
            "what_changes": change, "verdict": (item or {}).get("verdict")}


def run(top: int = 5, use_llm: bool = True) -> dict:
    from src.agents import forum_sentiment, industry_news, on_demand
    from src.agents.verifier import verify
    from src.api import data as apidata
    from src.llm.client import get_client

    key = now_tw().strftime("%Y%m%d%H")
    if key in _CACHE:
        return _CACHE[key]
    plan = on_demand.portfolio_plan()
    items = {i["code"]: i for i in plan.get("items", [])}
    picks = apidata.high_return_picks(limit=20).get("items", [])
    pick_by = {p["code"]: {**p, "rank": i + 1} for i, p in enumerate(picks)}
    codes = list(items) + [p["code"] for p in picks[:top] if p["code"] not in items]
    try:
        industry = industry_news.run([p["code"] for p in picks])
    except Exception as e:  # noqa: BLE001
        log.info("辯論：產業新聞失敗 %s", e)
        industry = {}
    try:
        forum = forum_sentiment.run([p["code"] for p in picks])
    except Exception as e:  # noqa: BLE001
        log.info("辯論：論壇失敗 %s", e)
        forum = {}
    facts = {c: facts_for(c, items.get(c), pick_by.get(c), industry, forum) for c in codes}
    args: dict[str, tuple[list, list]] = {}
    client = get_client()
    if use_llm and client.available():
        blocks = []
        for c in codes:
            blocks.append(f"### {c} {stock_meta(c).get('name')}\n" + "\n".join(f"[{f['id']}] {f['text']}" for f in facts[c]))
        try:
            res = client.structured(
                "以下每檔股票有一份編號事實。請為每檔各寫 1～3 個多方論點與 1～3 個空方論點，"
                "每個論點必須列出引用的事實編號，只能使用事實裡出現的數字，不可加入任何新資訊。\n\n" + "\n\n".join(blocks),
                DebateList, system="你是嚴謹的投資辯論主持人，多空兩方都要公平呈現，不可臆測。")
            for d in res.items:
                if d.code not in facts:
                    continue
                fid = {f["id"]: f for f in facts[d.code]}

                def keep(ps):
                    out = []
                    for p in ps:
                        cited = [fid[i] for i in p.fact_ids if i in fid]
                        if cited and verify(p.text, cited)[0]:
                            out.append({"text": p.text, "facts": [f["id"] for f in cited]})
                    return out

                args[d.code] = (keep(d.bull), keep(d.bear))
        except Exception as e:  # noqa: BLE001
            log.warning("辯論 LLM 失敗，改用事實清單：%s", e)
    out_items = []
    for c in codes:
        bull, bear = args.get(c, ([], []))
        if not bull:
            bull = [{"text": f["text"], "facts": [f["id"]]} for f in facts[c] if f["sign"] > 0][:3]
        if not bear:
            bear = [{"text": f["text"], "facts": [f["id"]]} for f in facts[c] if f["sign"] < 0][:3]
        out_items.append(judge(c, facts[c], bull, bear, items.get(c)))
    out = {"time": now_tw().strftime("%m/%d %H:%M"), "items": out_items, "llm": bool(args),
           "note": "論點只引用程式算好的事實；結論仍以系統規則為準，辯論只負責把多空理由攤開"}
    _CACHE.clear()
    _CACHE[key] = out
    return out
