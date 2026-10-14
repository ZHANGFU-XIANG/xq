"""SearchAgent：遇到沒看過的錯誤或異常時，上網找可能原因（給 SRE 通報與 AI 助理用）。

來源：DuckDuckGo HTML 搜尋（免金鑰）、Google 新聞 RSS（事件類）；設定 FIRECRAWL_API_KEY 時改用 Firecrawl 搜尋。
只回傳標題、網址、摘要，不經 LLM（避免捏造）。結果快取 6 小時，同一個錯誤不重複搜尋。
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
import urllib.parse

import requests

log = logging.getLogger(__name__)
_CACHE: dict = {}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}


def normalize(err: str) -> str:
    """把錯誤訊息裡的數字、網址、路徑拿掉，變成可搜尋的關鍵字。"""
    s = re.sub(r"https?://\S+", " ", err)
    s = re.sub(r"[A-Za-z]:\\\S+|/\S+/\S+", " ", s)
    s = re.sub(r"\b\d[\d.,:]*\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:160]


def _ddg(q: str, n: int) -> list[dict]:
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": q}, headers=UA, timeout=20)
    out = []
    for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
                         r.text, re.S):
        url = m.group(1)
        if "uddg=" in url:
            url = urllib.parse.unquote(url.split("uddg=")[1].split("&")[0])
        strip = lambda x: html.unescape(re.sub(r"<[^>]+>", "", x)).strip()  # noqa: E731
        out.append({"title": strip(m.group(2)), "url": url, "snippet": strip(m.group(3))[:240], "source": "DuckDuckGo"})
        if len(out) >= n:
            break
    return out


def _firecrawl(q: str, n: int) -> list[dict]:
    key = os.getenv("FIRECRAWL_API_KEY")
    r = requests.post("https://api.firecrawl.dev/v1/search", headers={"Authorization": f"Bearer {key}"},
                      json={"query": q, "limit": n}, timeout=30)
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("description") or "")[:240],
             "source": "Firecrawl"} for x in r.json().get("data", [])][:n]


def _news(q: str, n: int) -> list[dict]:
    import feedparser

    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    return [{"title": e.get("title", ""), "url": e.get("link", ""), "snippet": "", "source": "Google 新聞"}
            for e in feedparser.parse(url).entries[:n]]


def search(query: str, n: int = 5, kind: str = "web") -> list[dict]:
    """kind：web（技術問題）或 news（市場事件）。失敗回傳空清單，不丟例外。"""
    key = (kind, query, n)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    out: list[dict] = []
    try:
        if kind == "news":
            out = _news(query, n)
        elif os.getenv("FIRECRAWL_API_KEY"):
            out = _firecrawl(query, n)
        if not out and kind != "news":
            out = _ddg(query, n)
    except Exception as e:  # noqa: BLE001
        log.info("SearchAgent 搜尋失敗：%s", e)
    _CACHE[key] = (time.time(), out)
    return out


def explain_error(err: str, context: str = "") -> list[dict]:
    """錯誤訊息 → 可能原因的網頁（例如資料源改版、API 限流）。"""
    q = normalize(err)
    return search(f"{context} {q}".strip(), 4) if q else []
