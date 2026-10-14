"""IndustryNewsAgent：依持股與前 20 名推薦所屬產業抓產業新聞，經 FactCheck 評可信度，只對「提到這些公司或產業」的新聞做利多／利空判讀。

- 查詢：每個產業一組 Google 新聞查詢（產業名＋供應鏈關鍵字），最多 8 個產業、每個 8 則，去重
- 判讀：LLM 只看持股＋前 20 名相關的標題，必須引用新聞編號，經 Verifier 核對數字；LLM 不可用時用關鍵字規則
- 輸出：各產業的利多／利空則數與重點、各股相關新聞（含可信度標籤）
"""
from __future__ import annotations

import logging
import time

from pydantic import BaseModel, Field

from src.agents import fact_check, search_agent
from src.config import now_tw, stock_meta

log = logging.getLogger(__name__)
_CACHE: dict = {}
EXTRA = {"半導體業": "晶圓 AI 伺服器", "電子零組件業": "PCB 載板 AI", "電腦及週邊設備業": "AI 伺服器 筆電", "光電業": "面板 光通訊",
         "通信網路業": "網通 低軌衛星", "電子通路業": "電子通路", "金融保險業": "金控 升息 獲利", "航運業": "運價 貨櫃",
         "鋼鐵工業": "鋼價", "塑膠工業": "塑化 報價", "生技醫療業": "新藥 臨床", "建材營造業": "房市 營建",
         "電機機械": "機械 自動化", "化學工業": "化工 報價", "紡織纖維": "紡織 接單", "其他電子業": "電子 AI"}
POS = ("創新高", "接單", "擴產", "調升", "上修", "成長", "大單", "需求強", "漲價", "供不應求", "獲利增", "優於預期", "受惠")
NEG = ("下修", "衰退", "停工", "砍單", "虧損", "跌價", "庫存", "調降", "利空", "火災", "裁員", "違約", "低於預期", "延後")


class NewsJudge(BaseModel):
    id: int
    code: str = Field(description="相關股票代號；整個產業都受影響就填產業內任一相關持股")
    sentiment: int = Field(ge=-2, le=2, description="-2 重大利空、-1 利空、0 無關或中性、1 利多、2 重大利多")
    reason: str = Field(description="一句話，只能根據標題，不可加入標題沒有的數字")


class NewsJudgeList(BaseModel):
    items: list[NewsJudge]


def _rule(title: str) -> int:
    p, n = sum(w in title for w in POS), sum(w in title for w in NEG)
    return 1 if p > n else -1 if n > p else 0


def run(codes: list[str] | None = None, use_llm: bool = True) -> dict:
    from src.agents.verifier import verify
    from src.llm.client import get_client
    from src.state import portfolio as pfm

    held = list(pfm.build_portfolio().positions)
    codes = list(dict.fromkeys(held + (codes or [])))
    key = ",".join(sorted(codes)) + now_tw().strftime("%Y%m%d%H")
    if key in _CACHE:
        return _CACHE[key]
    names = {c: stock_meta(c)["name"] for c in codes}
    sectors: dict[str, list[str]] = {}
    for c in codes:
        sectors.setdefault(stock_meta(c).get("sector") or "其他", []).append(c)
    sec_order = sorted(sectors, key=lambda s: (-sum(c in held for c in sectors[s]), -len(sectors[s])))[:8]
    pool, seen = [], set()
    for sec in sec_order:
        q = f"{sec.replace('業', '')} {EXTRA.get(sec, '')} 台股".strip()
        for n in search_agent.search(q, 8, "news"):
            t = n["title"]
            if t in seen:
                continue
            seen.add(t)
            src = t.rsplit(" - ", 1)[-1] if " - " in t else "Google新聞"
            pool.append({"id": len(pool) + 1, "sector": sec, "title": t, "url": n["url"], "source": src})
    pool = fact_check.score(pool)
    rel = [n for n in pool if any(nm in n["title"] or c in n["title"] for c, nm in names.items())
           or n["sector"] in sec_order[:4]]
    judged: dict[int, dict] = {}
    client = get_client()
    if use_llm and rel and client.available():
        lines = [f"[{n['id']}]（{n['sector']}）{n['title']}" for n in rel[:60]]
        stocks = "、".join(f"{c} {nm}（{stock_meta(c).get('sector')}）" for c, nm in names.items())
        try:
            res = client.structured(
                f"關注股票：{stocks}\n\n以下是產業新聞標題。判斷每則對關注股票的影響（只根據標題），無關的填 0。\n" + "\n".join(lines),
                NewsJudgeList, system="你是嚴謹的產業分析師，只根據提供的標題判斷，不可臆測或加入標題以外的資訊。")
            by = {n["id"]: n for n in rel}
            for it in res.items:
                if it.id in by and it.sentiment != 0 and verify(it.reason, [by[it.id]])[0]:
                    judged[it.id] = {"sentiment": it.sentiment, "reason": it.reason, "code": it.code if it.code in names else None,
                                     "by": "LLM"}
        except Exception as e:  # noqa: BLE001
            log.warning("產業新聞判讀失敗，改用關鍵字規則：%s", e)
    for n in rel:
        if n["id"] not in judged and _rule(n["title"]):
            judged[n["id"]] = {"sentiment": _rule(n["title"]), "reason": "關鍵字規則", "code": None, "by": "規則"}
    by_sector = {}
    for sec in sec_order:
        items = [{**n, **judged[n["id"]]} for n in pool if n["sector"] == sec and n["id"] in judged]
        items.sort(key=lambda x: (-x["credibility"], -abs(x["sentiment"])))
        w = [x["sentiment"] * x["credibility"] for x in items]
        by_sector[sec] = {"codes": sectors[sec], "names": [names[c] for c in sectors[sec]], "score": round(sum(w), 2),
                          "pos": sum(1 for x in items if x["sentiment"] > 0), "neg": sum(1 for x in items if x["sentiment"] < 0),
                          "items": items[:6]}
    out = {"time": now_tw().strftime("%m/%d %H:%M"), "sectors": by_sector, "n_news": len(pool),
           "n_judged": len(judged), "llm": any(v["by"] == "LLM" for v in judged.values())}
    _CACHE.clear()
    _CACHE[key] = out
    return out
