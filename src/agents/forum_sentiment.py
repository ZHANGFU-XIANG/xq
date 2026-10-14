"""ForumSentimentAgent：PTT Stock 版近 2 天的討論風向（提及次數、推文熱度、多空方向），持股與前 20 名再用 LLM 判讀語意。

- 抓取：https://www.ptt.cc/bbs/Stock/index.html（over18 cookie），往前翻到超過 2 天為止（最多 40 頁）
- 辨識股票：標題中的 4 碼代號或股票名稱（只比對流動性選股池與持股的名稱，避免「台灣」「全台」誤判）
- 熱度：提及次數 × 推文數；與資料庫中前幾天的平均比較得到 z 分數（data/forum_log.csv 每天累積）
- 方向：[標的] 文的「多／空」、標題關鍵字；持股與前 20 名的相關標題再交給 LLM 判讀（必須引用編號）
- 預測力：論壇資料要累積 ≥ 60 個交易日才回測「過熱之後 5／20 日報酬」；在那之前一律標「僅資訊（累積中）」
Yahoo 股市討論區：2026-10 網址改版（舊網址 404），暫時無法讀取。
PTT 封鎖 GitHub Actions 的 IP（403）：每日紀錄由網站（agents_bg._log_forum）收盤後寫回 GitHub。
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
import time
from collections import defaultdict

import requests

from src.config import DATA_DIR, now_tw, stock_meta

log = logging.getLogger(__name__)
BASE = "https://www.ptt.cc"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36"}
LOG = DATA_DIR / "forum_log.csv"
BULL = ("多", "噴", "漲", "買", "抄底", "上車", "利多", "起飛", "看好")
BEAR = ("空", "崩", "跌", "賣", "逃", "下車", "利空", "倒", "看壞", "放空")
_CACHE: dict = {}
ENT = re.compile(r'<div class="r-ent">.*?<div class="nrec">(.*?)</div>.*?<div class="title">\s*(?:<a href="([^"]+)">(.*?)</a>|.*?)\s*</div>'
                 r'.*?<div class="date">\s*(.*?)</div>', re.S)


def _push(nrec: str) -> int:
    t = re.sub(r"<[^>]+>", "", nrec).strip()
    if t == "爆":
        return 100
    if t.startswith("X"):
        return -10 * (int(t[1:]) if t[1:].isdigit() else 10)
    return int(t) if t.isdigit() else 0


def crawl(days: int = 2, max_pages: int = 40) -> list[dict]:
    today = now_tw().date()
    oldest = today - dt.timedelta(days=days)
    url, posts = f"{BASE}/bbs/Stock/index.html", []
    for _ in range(max_pages):
        r = requests.get(url, cookies={"over18": "1"}, headers=UA, timeout=20)
        r.raise_for_status()
        page = []
        for nrec, href, title, date in ENT.findall(r.text):
            if not href or "[公告]" in title:
                continue
            m, d = (int(x) for x in date.strip().split("/"))
            year = today.year if m <= today.month else today.year - 1
            page.append({"title": re.sub(r"<[^>]+>", "", title).strip(), "url": BASE + href, "push": _push(nrec),
                         "date": dt.date(year, m, d)})
        posts += page
        prev = re.search(r'href="(/bbs/Stock/index\d+\.html)">&lsaquo;', r.text)
        if not prev or (page and min(p["date"] for p in page) < oldest):
            break
        url = BASE + prev.group(1)
        time.sleep(0.3)
    return [p for p in posts if p["date"] >= oldest]


def _names(universe: list[str]) -> dict[str, str]:
    out = {}
    for c in universe:
        n = stock_meta(c).get("name") or ""
        if len(n) >= 2 and n not in ("台灣", "全台", "台灣大"):
            out[n] = c
    return out


def tag(posts: list[dict], universe: list[str]) -> dict[str, list[dict]]:
    names = _names(universe)
    uni = set(universe)
    by: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        hit = {c for c in re.findall(r"(?<!\d)(\d{4})(?!\d)", p["title"]) if c in uni}
        spans = [(m.start(), m.end(), c) for n, c in names.items() for m in re.finditer(re.escape(n), p["title"])]
        # 「南亞」落在「南亞科」裡面時不算南亞（較短名稱被較長名稱包住就丟掉）
        hit |= {c for s, e, c in spans if not any(s2 <= s and e <= e2 and (e2 - s2) > (e - s) for s2, e2, _ in spans)}
        for c in hit:
            by[c].append(p)
    return by


def _direction(title: str) -> int:
    if title.startswith("[標的]"):
        tail = title[-4:]
        if "多" in tail:
            return 1
        if "空" in tail:
            return -1
    b, s = sum(w in title for w in BULL), sum(w in title for w in BEAR)
    return 1 if b > s else -1 if s > b else 0


def _history() -> dict[str, list[int]]:
    hist: dict[str, list[int]] = defaultdict(list)
    if LOG.exists():
        for r in csv.DictReader(open(LOG, encoding="utf-8")):
            hist[r["code"]].append(int(r["mentions"]))
    return hist


def log_today(stats: dict[str, dict]) -> None:
    day = now_tw().date().isoformat()
    rows = []
    if LOG.exists():
        rows = [r for r in csv.DictReader(open(LOG, encoding="utf-8")) if r["date"] != day]
    rows += [{"date": day, "code": c, "mentions": s["mentions"], "push": s["push"], "direction": s["direction"]}
             for c, s in stats.items()]
    with open(LOG, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "code", "mentions", "push", "direction"])
        w.writeheader()
        w.writerows(rows[-20000:])


def run(focus: list[str] | None = None, universe: list[str] | None = None, write_log: bool = False) -> dict:
    from src.agents import on_demand
    from src.state import portfolio as pfm

    key = now_tw().strftime("%Y%m%d%H") + str(now_tw().minute // 20)
    if key in _CACHE:
        return _CACHE[key]
    held = list(pfm.build_portfolio().positions)
    focus = list(dict.fromkeys(held + (focus or [])))
    universe = universe or list(dict.fromkeys(on_demand.context()["codes"] + held))
    posts = crawl()
    by = tag(posts, universe)
    hist = _history()
    stats = {}
    for c, ps in by.items():
        mentions = len(ps)
        base = hist.get(c, [])[-20:]
        mean = sum(base) / len(base) if base else None
        sd = (sum((x - mean) ** 2 for x in base) / len(base)) ** 0.5 if base and len(base) >= 5 else None
        dirs = [_direction(p["title"]) for p in ps]
        stats[c] = {"code": c, "name": stock_meta(c).get("name"), "mentions": mentions, "push": sum(p["push"] for p in ps),
                    "direction": round(sum(dirs) / len(dirs), 2) if dirs else 0.0,
                    "z": round((mentions - mean) / sd, 2) if sd else None,
                    "posts": sorted(ps, key=lambda p: -p["push"])[:4]}
    if write_log:
        log_today(stats)
    hot = sorted(stats.values(), key=lambda s: -(s["mentions"] * 3 + max(s["push"], 0) / 10))[:15]
    n_days = len({r for r in (open(LOG, encoding="utf-8").read().splitlines()[1:] if LOG.exists() else [])
                  for r in [r.split(",")[0]]})
    out = {"time": now_tw().strftime("%m/%d %H:%M"), "posts": len(posts), "hot": hot,
           "focus": [stats[c] for c in focus if c in stats],
           "label": "僅資訊（累積中）" if n_days < 60 else "待回測", "history_days": n_days,
           "note": "PTT Stock 版近 2 天；Yahoo 股市討論區網址改版暫時無法讀取。論壇風向噪音大，未經驗證前不影響任何建議"}
    for s in out["hot"] + out["focus"]:
        for p in s["posts"]:
            p["date"] = str(p["date"])
    _CACHE.clear()
    _CACHE[key] = out
    return out
