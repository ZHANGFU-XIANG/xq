"""FactCheckAgent：過濾假消息、誇大標題與未證實傳聞（規則計算，不經 LLM）。

每則新聞給可信度 0～1 與標籤（已證實／待證實／未證實）：
- 來源分級：官方（公開資訊觀測站、證交所、櫃買、期交所、公司公告）1.0；主流財經媒體 0.8；一般 0.5；
  未知或內容農場 0.3；論壇 0.2
- 多來源證實：同一事件（標題字元相似度 ≥ 0.5）有 2 個以上不同來源 → +0.2
- 對到公開資訊觀測站重大訊息 → 直接視為已證實
- 「澄清」「否認」「不實」→ 標示「已澄清」，該則利空／利多可信度降低
- 誇大用語（狂噴、穩賺、必漲、內線、驚爆、保證……）每個 −0.15
"""
from __future__ import annotations

import re

OFFICIAL = ("公開資訊觀測站", "mops", "證交所", "臺灣證券交易所", "櫃買", "期交所", "重大訊息", "重訊", "金管會")
MAINSTREAM = ("鉅亨", "cnyes", "中央社", "經濟日報", "工商時報", "自由財經", "自由時報", "Yahoo", "MoneyDJ", "聯合新聞網",
              "udn", "財訊", "商業周刊", "商周", "天下", "今周刊", "路透", "Reuters", "Bloomberg", "彭博", "日經", "CNBC",
              "WSJ", "華爾街日報", "數位時代", "科技新報", "DIGITIMES", "ETtoday", "中時", "TVBS", "公視", "富聯網")
FORUM = ("PTT", "ptt", "Dcard", "爆料同學會", "討論區", "論壇", "Mobile01")
HYPE = ("狂噴", "穩賺", "必漲", "內線", "驚爆", "保證", "飆漲", "噴出", "暴賺", "神股", "起飛", "翻倍", "天價", "必買", "老師")
CLARIFY = ("澄清", "否認", "不實", "非事實", "未經證實", "假消息", "謠言")


def tier(source: str, title: str = "") -> float:
    s = f"{source} {title}"
    if any(k in s for k in OFFICIAL):
        return 1.0
    if any(k in s for k in FORUM):
        return 0.2
    if any(k in s for k in MAINSTREAM):
        return 0.8
    if source.startswith("Google新聞"):  # Google 新聞的實際來源寫在標題尾端「 - 媒體名」
        tail = title.rsplit(" - ", 1)[-1] if " - " in title else ""
        return tier(tail) if tail else 0.5
    return 0.5 if source else 0.3


def _chars(t: str) -> set[str]:
    t = re.sub(r"\s+-\s+[^-]+$", "", t)  # 去掉「 - 媒體名」
    t = re.sub(r"[\W_]+", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similar(a: str, b: str) -> float:
    x, y = _chars(a), _chars(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def outlet(it: dict) -> str:
    """實際媒體：Google 新聞的來源寫在標題尾端「 - 媒體名」。"""
    t, src = it.get("title", ""), it.get("source", "")
    if (src.startswith("Google新聞") or not src) and " - " in t:
        return t.rsplit(" - ", 1)[-1].strip()
    return src


def score(items: list[dict], official_titles: list[str] | None = None) -> list[dict]:
    """items：[{title, source, ...}] → 每則加上 credibility、fc_label、fc_notes（不改原本欄位）。"""
    official_titles = official_titles or []
    out = []
    for i, it in enumerate(items):
        title, src = it.get("title", ""), it.get("source", "")
        base = tier(src, title)
        notes = []
        me = outlet(it)
        others = {outlet(o) for j, o in enumerate(items) if j != i and outlet(o) != me
                  and similar(title, o.get("title", "")) >= 0.5}
        if others:
            base += 0.2
            notes.append(f"{len(others) + 1} 個來源報導")
        if any(similar(title, t) >= 0.4 for t in official_titles):
            base = 1.0
            notes.append("對到公開資訊觀測站重大訊息")
        hype = [w for w in HYPE if w in title]
        if hype:
            base -= 0.15 * len(hype)
            notes.append("誇大用語：" + "、".join(hype))
        clarified = any(w in title for w in CLARIFY)
        if clarified:
            notes.append("內容涉及澄清／否認")
        c = round(min(1.0, max(0.0, base)), 2)
        label = "已證實" if c >= 0.8 else "待證實" if c >= 0.5 else "未證實"
        out.append({**it, "credibility": c, "fc_label": label, "fc_notes": notes, "clarified": clarified})
    return out


def run(items: list[dict], official_titles: list[str] | None = None) -> dict:
    scored = score(items, official_titles)
    return {"items": scored, "counts": {k: sum(1 for x in scored if x["fc_label"] == k) for k in ("已證實", "待證實", "未證實")},
            "flagged": [x for x in scored if x["fc_label"] == "未證實" or x["clarified"]][:20]}
