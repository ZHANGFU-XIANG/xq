"""NewsAgent：抓 RSS／鉅亨網／Google News，去重後輸出帶編號的新聞清單。
SentimentAgent：用 LLM 把新聞轉成「個股事件＋情緒分數」，每筆都必須引用新聞編號。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.parse
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime

import feedparser
import requests
from pydantic import BaseModel, Field

from src.agents.verifier import verify
from src.config import TW_TZ, settings
from src.llm.client import LLMError, get_client

log = logging.getLogger(__name__)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _parse_time(entry) -> dt.datetime | None:
    for key in ("published", "updated"):
        if entry.get(key):
            try:
                return parsedate_to_datetime(entry[key]).astimezone(TW_TZ)
            except (TypeError, ValueError):
                pass
    return None


# ---------------------------------------------------------------- 只留金融經濟新聞
FINANCE_SOURCES = ("鉅亨網", "中央社財經", "Yahoo股市")  # 本身就是財經版
FINANCE_WORDS = (
    "股", "債", "殖利率", "利率", "升息", "降息", "央行", "聯準會", "Fed", "FOMC", "通膨", "CPI", "PCE", "PMI", "GDP",
    "就業", "非農", "失業", "經濟", "景氣", "匯", "美元", "台幣", "日圓", "人民幣", "歐元", "油價", "原油", "OPEC", "天然氣",
    "金價", "黃金", "期貨", "關稅", "貿易", "出口", "進口", "外銷", "反傾銷", "制裁", "產能", "營收", "財報", "獲利", "虧損",
    "EPS", "毛利", "目標價", "法人", "外資", "投信", "自營商", "市值", "IPO", "併購", "收購", "增資", "減資", "配息", "除息",
    "ETF", "基金", "半導體", "晶片", "晶圓", "記憶體", "伺服器", "訂單", "出貨", "供應鏈", "投資", "資金", "銀行", "金融",
    "房貸", "比特幣", "加密貨幣", "Nasdaq", "那斯達克", "道瓊", "標普", "費半", "日經", "華爾街", "漲", "跌",
)
NOT_FINANCE_WORDS = ("新婚", "婚禮", "車禍", "命案", "颱風", "地震", "氣象", "玄學", "拜佛", "選舉登場", "戰機", "空軍", "飛彈")


def headline(title: str) -> str:
    """Google News 標題尾巴的「 - 媒體名」與「| 財經 |」等分類字樣不算內容。"""
    t = re.sub(r"\s+-\s+[^-]+$", "", title or "")
    return re.sub(r"[|｜]\s*(財經|新聞|國際|證券)\s*[|｜]?", " ", t)


def is_finance(item: dict) -> bool:
    t = headline(item.get("title", ""))
    if any(w in t for w in NOT_FINANCE_WORDS):
        return False
    if str(item.get("source", "")).startswith(FINANCE_SOURCES):
        return True
    return any(w.lower() in t.lower() for w in FINANCE_WORDS)


ETF_WORDS = ("ETF", "配息", "除息", "成分股", "折溢價", "受益人", "高股息", "00878", "0050", "00919", "00929", "主動式")
FUT_WORDS = ("台指期", "臺指期", "期貨", "未平倉", "結算", "選擇權", "夜盤", "小台", "微台", "期交所", "外資期")


def asset_category(item: dict) -> str:
    """新聞分類：期貨與選擇權／ETF／股票（標題關鍵字，媒體名稱不算）。"""
    t = headline(item.get("title", ""))
    if any(w in t for w in FUT_WORDS):
        return "期貨"
    if any(w in t for w in ETF_WORDS) or re.search(r"(?<![0-9])00\d{2,4}[A-Z]?(?![0-9])", t):
        return "ETF"
    return "股票"


def _cnyes_api() -> list[dict]:
    out = []
    for cat in ("tw_stock", "us_stock", "tech"):
        try:
            r = requests.get(f"https://api.cnyes.com/media/api/v1/newslist/category/{cat}?limit=30", timeout=20)
            for it in r.json()["items"]["data"]:
                out.append({
                    "source": "鉅亨網API", "title": it.get("title", ""),
                    "summary": _clean(it.get("summary", ""))[:300],
                    "url": f"https://news.cnyes.com/news/id/{it['newsId']}",
                    "time": dt.datetime.fromtimestamp(it["publishAt"], TW_TZ),
                })
        except Exception as e:  # noqa: BLE001
            log.warning("鉅亨網 API %s 失敗: %s", cat, e)
    return out


def fetch_news(now: dt.datetime) -> list[dict]:
    cfg = settings()["news"]
    feeds = list(cfg["rss"])
    for q in cfg["google_news_queries"]:
        feeds.append({"name": f"Google新聞:{q}",
                      "url": "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d")
                             + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"})
    def one(f):
        try:
            parsed = feedparser.parse(f["url"])
            return [{"source": f["name"], "title": _clean(e.get("title", "")),
                     "summary": _clean(e.get("summary", ""))[:300], "url": e.get("link", ""),
                     "time": _parse_time(e)} for e in parsed.entries[:40]]
        except Exception as e:  # noqa: BLE001
            log.warning("RSS %s 失敗: %s", f["name"], e)
            return []

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:  # 平行抓取，手動刷新時幾秒內完成
        cnyes = ex.submit(_cnyes_api)
        items: list[dict] = [it for lst in ex.map(one, feeds) for it in lst]
        items += cnyes.result()

    cutoff = now - dt.timedelta(hours=cfg["lookback_hours"])
    items = [i for i in items if i["title"] and i["url"] and (i["time"] is None or i["time"] >= cutoff) and is_finance(i)]
    items.sort(key=lambda i: i["time"] or now, reverse=True)
    deduped: list[dict] = []
    for it in items:  # 標題相似度 > 0.8 視為同一則
        if any(SequenceMatcher(None, it["title"], d["title"]).ratio() > 0.8 for d in deduped):
            continue
        deduped.append(it)
    deduped = deduped[: cfg["max_items"]]
    for idx, it in enumerate(deduped, 1):
        it["id"] = idx
        it["category"] = asset_category(it)
        it["time"] = it["time"].strftime("%m/%d %H:%M") if it["time"] else ""
    return deduped


# ---------------------------------------------------------------- Sentiment
class NewsEvent(BaseModel):
    stock_codes: list[str] = Field(description="受影響的台股代號，只能從提供的清單中選；若為整體台股市場則填 MARKET")
    sentiment: int = Field(ge=-2, le=2, description="-2 強烈利空, -1 利空, 0 中性, 1 利多, 2 強烈利多")
    event: str = Field(description="事件一句話摘要，只能根據新聞原文，不得加入新聞沒有的數字")
    news_ids: list[int] = Field(description="支持此事件的新聞編號（必填）")


class SentimentResult(BaseModel):
    events: list[NewsEvent]
    market_view: str = Field(description="整體市場氛圍一句話，只根據新聞")


SYSTEM = (
    "你是嚴謹的台股新聞分析員。只能根據使用者提供的新聞列表判斷，禁止使用外部知識、禁止臆測、"
    "禁止產生新聞中沒有的數字。每個事件都必須列出支持它的新聞編號。不確定就給 0 或不要列出。"
)


def analyze_sentiment(news: list[dict], codes: dict[str, str]) -> dict:
    """codes = {代號: 名稱}（持股＋候選）。回傳 {"by_code": {...}, "market": {...}, "events": [...], "llm_used": bool}。"""
    empty = {"by_code": {}, "market": {"score": 0.0, "n_sources": 0}, "events": [], "market_view": "",
             "llm_used": False}
    client = get_client()
    if not news or not client.available():
        return empty
    lines = [f"[{n['id']}] ({n['source']} {n['time']}) {n['title']}｜{n['summary'][:150]}" for n in news]
    prompt = ("持股與候選清單：" + "、".join(f"{c} {n}" for c, n in codes.items())
              + "\n\n新聞列表：\n" + "\n".join(lines)
              + "\n\n請找出會影響觀察清單個股或台股整體的重要事件（最多 25 個）。")
    try:
        res = client.structured(prompt, SentimentResult, system=SYSTEM)
    except LLMError as e:
        log.warning("情緒分析失敗，改為中性：%s", e)
        return empty

    valid_ids = {n["id"] for n in news}
    by_id = {n["id"]: n for n in news}
    agg: dict[str, dict] = {}
    events = []
    for ev in res.events:
        ids = [i for i in ev.news_ids if i in valid_ids]
        targets = [c for c in ev.stock_codes if c in codes or c == "MARKET"]
        if not ids or not targets:  # 沒有來源或代號不在清單 → 丟棄（防幻覺）
            continue
        if not verify(ev.event, [by_id[i] for i in ids])[0]:  # 事件中的數字必須出自引用的新聞
            log.info("丟棄含未出處數字的事件：%s", ev.event)
            continue
        sources = sorted({by_id[i]["source"].split(":")[0] for i in ids})
        weight = 1.0 if len(sources) >= 2 else 0.6  # 單一來源降權
        events.append({"codes": targets, "sentiment": ev.sentiment, "event": ev.event,
                       "news": [{"id": i, "title": by_id[i]["title"], "url": by_id[i]["url"]} for i in ids],
                       "multi_source": len(sources) >= 2})
        for c in targets:
            a = agg.setdefault(c, {"sum": 0.0, "w": 0.0, "events": [], "n_sources": 0})
            a["sum"] += ev.sentiment * weight
            a["w"] += 1
            a["events"].append(ev.event)
            a["n_sources"] += len(ids)
    by_code = {c: {"score": round(a["sum"] / a["w"], 2), "events": a["events"], "n_sources": a["n_sources"]}
               for c, a in agg.items() if c != "MARKET"}
    market = agg.get("MARKET")
    return {"by_code": by_code,
            "market": {"score": round(market["sum"] / market["w"], 2) if market else 0.0,
                       "n_sources": market["n_sources"] if market else 0},
            "events": events, "market_view": res.market_view if verify(res.market_view, news)[0] else "",
            "llm_used": True}
