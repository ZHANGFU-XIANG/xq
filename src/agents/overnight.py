"""OvernightAgent：收盤後到隔天開盤前的即時監控（夜盤、美股、重大新聞 → 對持股開盤的影響金額與提醒）。

使用者 2026-10-14：「隔夜後開始整理開盤消息以及夜盤訊息，美股等提醒不夠快速即時」。
原本只有 07:00 盤前、08:20 開盤前兩次報告，夜間美股大跌要到早上才知道。

資料（全部由程式計算，不經 LLM）：
- 台指期夜盤（期交所 MIS，MarketType=1，15:00～隔日 05:00）：相對日盤結算價的漲跌 → 最直接的「明天開盤」估計
- 美股：S&P／那斯達克期貨、費城半導體、台積電 ADR、NVIDIA、VIX、新台幣匯率（yfinance 5 分 K，含盤前盤後）
- 持股：估計開盤跳空＝β60 × 夜盤漲跌，換算金額；台指期部位用夜盤價算未實現變化
- 新聞：網站即時新聞（每 5 分鐘）＋持股負面新聞（盤後每 30 分鐘判讀）

提醒門檻（與 us_shock.py 的研究一致：一般大跌不要開盤殺低，嚴重才開盤先賣）：
- 夜盤 ≤ −1% 注意、≤ −2% 嚴重；≥ +1.5% 利多
- 費半 ≤ −3% → us_shock 規則；那斯達克／S&P 期貨 ≤ −1.5%；VIX ≥ 25 或單日 +15%；台積電 ADR ≤ −3%
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

import numpy as np
import pandas as pd
import requests

from src.config import is_trading_day, next_trading_day, now_tw, stock_meta

log = logging.getLogger(__name__)
US = {"ES=F": "S&P 500 期貨", "NQ=F": "那斯達克期貨", "^SOX": "費城半導體", "TSM": "台積電 ADR", "NVDA": "NVIDIA",
      "^VIX": "VIX 恐慌指數", "TWD=X": "美元兌新台幣"}
TX_POINT = {"TX": 200, "MTX": 50, "TMF": 10}
_CACHE: dict = {}
_LOCK = threading.Lock()


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            v = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("隔夜監控 %s 失敗：%s", key, e)
            v = hit[1] if hit else None  # 失敗時沿用上一筆
        _CACHE[key] = (time.time(), v)
        return v


def in_window(now: dt.datetime | None = None) -> bool:
    """收盤後（13:35 起）到下一個交易日 09:00 前。"""
    now = now or now_tw()
    t = now.time()
    if is_trading_day(now.date()) and dt.time(9, 0) <= t < dt.time(13, 35):
        return False
    return True


def night_session_open(now: dt.datetime | None = None) -> bool:
    """期貨夜盤：交易日 15:00～隔日 05:00（週六凌晨 05:00 收）。"""
    now = now or now_tw()
    t, d = now.time(), now.date()
    if t >= dt.time(15, 0):
        return is_trading_day(d)
    if t < dt.time(5, 0):
        return is_trading_day(d - dt.timedelta(days=1))
    return False


# ---------------------------------------------------------------- 夜盤
def tx_night() -> dict | None:
    def fetch():
        from src.agents import futures_data as fd

        r = requests.post(fd.MIS_URL, json={"MarketType": "1", "SymbolType": "F", "KindID": "1", "CID": "TXF",
                                           "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "",
                                           "AscDesc": "A"}, headers=fd.UA, timeout=15).json()
        ym = fd.front_month()
        for x in r.get("RtData", {}).get("QuoteList", []):
            sid = x.get("SymbolID", "")
            if not sid.endswith("-M") or "W" in x.get("DispCName", ""):
                continue
            import re

            m = re.match(r"^[A-Z]+([A-L])(\d)-M$", sid)
            if not m or fd._ym_from_code(m.group(1), m.group(2), fd.CALL_MONTHS) != ym:
                continue
            ref, last = fd._f(x.get("CRefPrice")), fd._f(x.get("CLastPrice"))
            if not ref:
                return None
            price = last or ref
            return {"contract": f"TX{ym}", "price": price, "ref": ref, "change": round(price - ref, 1),
                    "chg_pct": round((price / ref - 1) * 100, 2), "high": fd._f(x.get("CHighPrice")),
                    "low": fd._f(x.get("CLowPrice")), "traded": bool(last),
                    "time": fd._fmt_time(x.get("CDate", ""), x.get("CTime", "")), "source": "期交所 MIS 夜盤"}
        return None

    return _cached("tx_night", 20, fetch)


# ---------------------------------------------------------------- 美股
def us_live() -> list[dict]:
    def fetch():
        import yfinance as yf

        intra = yf.download(list(US), period="2d", interval="5m", prepost=True, group_by="ticker",
                            progress=False, auto_adjust=False, threads=True)
        daily = yf.download(list(US), period="10d", interval="1d", group_by="ticker", progress=False,
                            auto_adjust=False, threads=True)
        out = []
        for t, name in US.items():
            try:
                d = daily[t]["Close"].dropna()
                i = intra[t]["Close"].dropna()
            except KeyError:
                continue
            if len(d) < 3 or i.empty:
                continue
            last, last_t = float(i.iloc[-1]), i.index[-1]
            # 前一個「已收盤」日收盤：日 K 最後一列若是今天（盤中）就用前一列
            today_us = last_t.tz_convert("America/New_York").date() if last_t.tzinfo else last_t.date()
            closes = d[d.index.date < today_us] if d.index[-1].date() >= today_us else d
            prev = float(closes.iloc[-1])
            prev2 = float(closes.iloc[-2]) if len(closes) >= 2 else None
            out.append({"ticker": t, "name": name, "price": round(last, 2), "prev_close": round(prev, 2),
                        "chg_pct": round((last / prev - 1) * 100, 2),
                        "two_day_pct": round((last / prev2 - 1) * 100, 2) if prev2 else None,
                        "time": last_t.tz_convert("Asia/Taipei").strftime("%m/%d %H:%M") if last_t.tzinfo else str(last_t)})
        return out

    return _cached("us_live", 120, fetch) or []


# ---------------------------------------------------------------- 持股影響
def _betas(codes: list[str]) -> dict[str, float]:
    def fetch():
        from src.agents import on_demand

        ctx = on_demand.context()
        m = ctx["taiex"].Close.pct_change()
        out = {}
        for c in codes:
            df = ctx["panel"].get(c)
            if df is None or len(df) < 80:
                out[c] = 1.0
                continue
            x = pd.concat([df.Close.pct_change(), m], axis=1, join="inner").dropna().tail(120)
            b = float(x.iloc[:, 0].cov(x.iloc[:, 1]) / x.iloc[:, 1].var()) if len(x) > 30 else 1.0
            out[c] = round(float(np.clip(b, 0.2, 2.5)), 2) if np.isfinite(b) else 1.0
        return out

    return _cached("betas_" + ",".join(sorted(codes)), 3600, fetch) or {c: 1.0 for c in codes}


def impact(tx: dict | None) -> dict:
    from src.agents import alerts
    from src.state import portfolio as pfm

    pf = pfm.build_portfolio()
    codes = list(pf.positions)
    if not codes:
        return {"items": [], "stock_amount": 0, "futures_amount": 0, "total": 0}
    rt = alerts.realtime(codes)
    pct = (tx or {}).get("chg_pct") or 0.0
    betas = _betas(codes)
    items, stock_amt = [], 0.0
    for c, p in pf.positions.items():
        px = (rt.get(c) or {}).get("price") or p.avg_cost
        mv = p.shares * px
        est = betas.get(c, 1.0) * pct
        amt = mv * est / 100
        stock_amt += amt
        items.append({"code": c, "name": stock_meta(c)["name"], "price": px, "beta": betas.get(c, 1.0),
                      "est_gap_pct": round(est, 2), "amount": round(amt), "market_value": round(mv)})
    fut_amt = 0.0
    if tx and pf.derivs:
        for contract, dv in pf.derivs.items():  # 只算與夜盤同月份的台指類期貨（TX／MTX／TMF）
            if dv.kind == "FUT" and dv.root in TX_POINT and contract.endswith(tx["contract"][2:]):
                fut_amt += dv.qty * dv.multiplier * tx["change"]
    items.sort(key=lambda x: x["amount"])
    return {"items": items, "stock_amount": round(stock_amt), "futures_amount": round(fut_amt),
            "total": round(stock_amt + fut_amt), "basis": f"β60 × 夜盤 {pct:+.2f}%（估計值，不是保證）"}


# ---------------------------------------------------------------- 提醒
def _alerts(tx: dict | None, us: list[dict], imp: dict, nav: float | None) -> list[dict]:
    out = []
    by = {u["ticker"]: u for u in us}
    amt = imp.get("total", 0)
    amt_txt = f"，估計持股＋期貨開盤影響 {amt:+,.0f} 元" + (f"（總資產 {amt / nav * 100:+.2f}%）" if nav else "")
    if tx:
        p = tx["chg_pct"]
        if p <= -2:
            out.append({"key": "tx-2", "level": "嚴重", "title": f"台指期夜盤重挫 {p:+.2f}%（{tx['change']:+,.0f} 點）",
                        "body": f"夜盤 {tx['price']:,.0f}，日盤結算 {tx['ref']:,.0f}{amt_txt}。明天開盤可能大幅跳空，"
                                "先看 08:20 開盤前更新；一般大跌開盤不殺低（歷史上中位數 2 天回到原位），已跌破停損的照停損執行。"})
        elif p <= -1:
            out.append({"key": "tx-1", "level": "注意", "title": f"台指期夜盤下跌 {p:+.2f}%（{tx['change']:+,.0f} 點）",
                        "body": f"夜盤 {tx['price']:,.0f}{amt_txt}。不急著賣，開盤後看是否止跌。"})
        elif p >= 1.5:
            out.append({"key": "tx+", "level": "利多", "title": f"台指期夜盤上漲 {p:+.2f}%（{tx['change']:+,.0f} 點）",
                        "body": f"夜盤 {tx['price']:,.0f}{amt_txt}。預掛買單注意開盤可能跳空成交在較高價。"})
    sox = by.get("^SOX")
    if sox and sox["chg_pct"] <= -3:
        severe = (sox.get("two_day_pct") or 0) <= -6
        out.append({"key": f"sox{'-severe' if severe else ''}", "level": "嚴重" if severe else "注意",
                    "title": f"費城半導體 {sox['chg_pct']:+.2f}%" + (f"（兩日 {sox['two_day_pct']:+.2f}%）" if severe else ""),
                    "body": ("兩日合計跌逾 6%：歷史上續抱 3 天平均再跌約 1.9% → 高連動電子股開盤先賣、約第 3 個交易日買回（見持股動向）"
                             if severe else "單日跌逾 3%：台股電子股開盤平均跳空約 −1.4%，但續抱 5 日平均反而較好 → 不要開盤殺低")})
    for t, th in (("NQ=F", -1.5), ("ES=F", -1.5), ("TSM", -3.0)):
        u = by.get(t)
        if u and u["chg_pct"] <= th:
            out.append({"key": f"{t}-down", "level": "注意", "title": f"{u['name']} {u['chg_pct']:+.2f}%",
                        "body": f"現價 {u['price']}（{u['time']}），相對前收 {u['prev_close']}。"})
    vix = by.get("^VIX")
    if vix and (vix["price"] >= 25 or vix["chg_pct"] >= 15):
        out.append({"key": "vix", "level": "注意", "title": f"VIX 恐慌指數 {vix['price']}（{vix['chg_pct']:+.1f}%）",
                    "body": "VIX > 25 之後 20 日內台股跌逾 5% 的機率約 41%（平常 20%）；先暫停追高，已持有的照停損操作。"})
    return out


def _when(t) -> dt.datetime | None:
    """新聞時間（'MM/DD HH:MM' 字串或 datetime）→ 台灣時間（不含時區）。"""
    if not t:
        return None
    if isinstance(t, str):
        try:
            now = now_tw()
            w = dt.datetime.strptime(f"{now.year}/{t}", "%Y/%m/%d %H:%M")
            return w.replace(year=now.year - 1) if w > now.replace(tzinfo=None) + dt.timedelta(days=1) else w
        except ValueError:
            return None
    ts = pd.Timestamp(t)
    return (ts.tz_convert("Asia/Taipei").tz_localize(None) if ts.tzinfo else ts).to_pydatetime()


def _since() -> dt.datetime:
    """這一輪隔夜的起點：最近一次收盤（13:30）。"""
    now = now_tw().replace(tzinfo=None)
    d = now.date() if now.time() >= dt.time(13, 30) else now.date() - dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(13, 30))


def _news(limit: int = 15) -> list[dict]:
    from src.api import data

    try:
        items = data.live_news().get("news", [])
    except Exception:  # noqa: BLE001
        return []
    since = _since()
    out = []
    for n in items:
        w = _when(n.get("time"))
        if w is not None and w < since:
            continue
        out.append({"title": n["title"], "source": n["source"], "url": n["url"], "time": n.get("time") or "",
                    "category": n.get("category")})
    return out[:limit]


def _holding_news() -> list[dict]:
    from src.agents import alerts
    from src.state import portfolio as pfm

    names = {c: stock_meta(c)["name"] for c in pfm.build_portfolio().positions}
    out = []
    for code, risks in (alerts.news_risk(names) or {}).items():
        for r in risks[:1]:
            n = (r.get("news") or [{}])[0]
            out.append({"code": code, "name": names.get(code, code), "issue": r.get("issue"), "severity": r.get("severity"),
                        "title": n.get("title"), "url": n.get("url"), "time": n.get("time")})
    return out


def snapshot() -> dict:
    """隔夜即時總覽（網站每分鐘、通知器每 2 分鐘呼叫；各資料源自有快取）。"""
    now = now_tw()
    tx = tx_night()
    us = us_live()
    imp = impact(tx)
    nav = None
    try:
        from src.state import portfolio as pfm

        hist = pfm.load_history()
        nav = hist[-1]["nav"] if hist else None
    except Exception:  # noqa: BLE001
        pass
    try:
        hn = _cached("holding_news", 600, _holding_news) or []
    except Exception:  # noqa: BLE001
        hn = []
    al = _alerts(tx, us, imp, nav)
    for h in hn:
        w = _when(h.get("time"))
        if (h.get("severity") or 0) <= -2 and w is not None and w >= _since():  # 只提醒收盤後才出現的新消息
            al.append({"key": f"news-{h['code']}-{(h.get('title') or '')[:20]}", "level": "嚴重",
                       "title": f"{h['code']} {h['name']} 重大負面消息", "body": f"{h.get('issue') or ''}：{h.get('title') or ''}"})
    target = next_trading_day(now.date(), include_today=now.time() < dt.time(9, 0))
    return {"time": now.strftime("%m/%d %H:%M:%S"), "active": in_window(now), "night_open": night_session_open(now),
            "target_date": target.isoformat(), "tx_night": tx, "us": us, "impact": imp, "alerts": al,
            "holding_news": hn, "news": _news(), "nav": nav}
