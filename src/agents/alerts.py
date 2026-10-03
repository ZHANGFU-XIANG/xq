"""AlertAgent：市場警示與持股風險提醒。

市場層級：全市場漲停／跌停（依產業歸類、附新聞標題為可能原因）、注意股與處置股、大盤與美股劇烈波動。
持股層級：跌停、跌破／接近停損、單日重挫、負面新聞、處置／注意、法人連賣 → 依規則算出「建議賣出幾股、限價多少」。
買進前檢查：今日建議買進的標的若有負面新聞、處置、已漲停（追價）等，提醒先暫緩或減量。

防幻覺：漲跌停原因只列出實際新聞標題與連結（不經 LLM）；負面新聞的嚴重度由 LLM 判斷，但每則都必須引用新聞編號，
數字需出自新聞原文；賣出股數一律由程式依規則計算。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
import requests
from pydantic import BaseModel, Field

from src.agents import market_data, sizer
from src.agents.verifier import verify
from src.config import now_tw, stock_meta
from src.llm.client import LLMError, get_client
from src.state import params

log = logging.getLogger(__name__)
LEVEL_ORDER = {"嚴重": 0, "警告": 1, "提醒": 2}
_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def _roc_date(s) -> str | None:
    """民國日期 1151002 → 2026-10-02。"""
    s = str(s or "").strip()
    if re.fullmatch(r"\d{7}", s):
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    return s or None


def _is_stock_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{4}|00\d{2,4}[A-Z]?", code))


# ---------------------------------------------------------------- 全市場行情與漲跌停
def market_quotes() -> list[dict]:
    """最近交易日全市場收盤（上市＋上櫃），排除權證等。"""

    def fetch():
        out = []
        try:
            for x in requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=30).json():
                close, chg = _num(x.get("ClosingPrice")), _num(x.get("Change"))
                if not _is_stock_code(x["Code"]) or not close or chg is None:
                    continue
                prev = close - chg
                out.append({"code": x["Code"], "name": x["Name"], "market": "上市", "close": close, "change": chg,
                            "chg_pct": round(chg / prev * 100, 2) if prev else 0.0,
                            "value": _num(x.get("TradeValue")) or 0, "date": x.get("Date")})
        except Exception as e:  # noqa: BLE001
            log.warning("上市行情失敗：%s", e)
        try:
            for x in requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=60).json():
                code = x["SecuritiesCompanyCode"]
                close, chg = _num(x.get("Close")), _num(x.get("Change"))
                if not _is_stock_code(code) or not close or chg is None:
                    continue
                prev = close - chg
                out.append({"code": code, "name": x["CompanyName"], "market": "上櫃", "close": close, "change": chg,
                            "chg_pct": round(chg / prev * 100, 2) if prev else 0.0,
                            "value": _num(x.get("TransactionAmount")) or 0, "date": x.get("Date")})
        except Exception as e:  # noqa: BLE001
            log.warning("上櫃行情失敗：%s", e)
        return out

    return _cached("quotes", 600, fetch)


def limit_moves(quotes: list[dict], threshold: float = 9.5) -> dict:
    up = sorted([q for q in quotes if q["chg_pct"] >= threshold], key=lambda q: -q["value"])
    down = sorted([q for q in quotes if q["chg_pct"] <= -threshold], key=lambda q: -q["value"])

    def by_industry(rows):
        g: dict[str, list[str]] = {}
        for q in rows:
            g.setdefault(stock_meta(q["code"])["sector"], []).append(q["name"])
        return sorted(({"industry": k, "count": len(v), "names": v[:8]} for k, v in g.items()), key=lambda x: -x["count"])

    return {"up": up, "down": down, "up_industries": by_industry(up), "down_industries": by_industry(down)}


def _news_titles(code: str, name: str, n: int = 2) -> list[dict]:
    from src.agents.on_demand import search_news

    return [{"title": x["title"], "url": x["url"], "source": x["source"], "time": x.get("time", "")}
            for x in search_news(f"{name} {code}", 6)[:n]]


def explain_limits(lim: dict, top: int = 10) -> dict:
    """前 N 檔（依成交值）附新聞標題作為可能原因；其餘只標產業。"""
    picks = [("up", q) for q in lim["up"][:top]] + [("down", q) for q in lim["down"][:top]]
    with ThreadPoolExecutor(8) as ex:
        news = list(ex.map(lambda p: _cached(f"ln_{p[1]['code']}_{dt.date.today()}", 3600,
                                             lambda: _news_titles(p[1]["code"], p[1]["name"])), picks))
    for (side, q), nws in zip(picks, news):
        q["news"] = nws
        peers = [g for g in lim[f"{side}_industries"] if g["industry"] == stock_meta(q["code"])["sector"]]
        q["industry"] = stock_meta(q["code"])["sector"]
        q["peer_count"] = peers[0]["count"] if peers else 1
        q["reason"] = ("可能原因（新聞）：" + "；".join(n["title"] for n in nws)) if nws else \
            ("同產業 %d 檔同步%s，偏向產業或題材因素" % (q["peer_count"], "漲停" if side == "up" else "跌停")
             if q["peer_count"] >= 3 else "近 7 日查無相關新聞，可能為籌碼或技術面因素")
    return lim


# ---------------------------------------------------------------- 注意股／處置股
def attention_lists() -> dict[str, dict]:
    """{code: {"type": 注意/處置, "reason": ..., "period": ...}}"""

    def fetch():
        out: dict[str, dict] = {}
        sources = [
            ("https://openapi.twse.com.tw/v1/announcement/notice", "注意", "Code", "TradingInfoForAttention", None),
            ("https://openapi.twse.com.tw/v1/announcement/punish", "處置", "Code", "ReasonsOfDisposition", "DispositionPeriod"),
            ("https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information", "注意", "SecuritiesCompanyCode",
             "TradingInformation", None),
            ("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information", "處置", "SecuritiesCompanyCode",
             "DispositionReasons", "DispositionPeriod"),
        ]
        for url, kind, ck, rk, pk in sources:
            try:
                for x in requests.get(url, timeout=30).json():
                    code = (x.get(ck) or "").strip()
                    if not _is_stock_code(code):
                        continue
                    if kind == "處置" or code not in out:
                        out[code] = {"type": kind, "reason": (x.get(rk) or "").strip()[:120],
                                     "period": (x.get(pk) or "").strip() if pk else ""}
            except Exception as e:  # noqa: BLE001
                log.warning("警示名單 %s 失敗：%s", kind, e)
        return out

    return _cached("attention", 1800, fetch)


# ---------------------------------------------------------------- 盤中即時報價
def realtime(codes: list[str]) -> dict[str, dict]:
    """證交所 MIS 即時行情（含漲跌停價）。非交易時段回傳最後收盤狀態。"""
    if not codes:
        return {}
    chans = "|".join(f"{'otc' if stock_meta(c).get('market') == 'TPEx' else 'tse'}_{c}.tw" for c in codes)
    try:
        r = requests.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                         params={"ex_ch": chans, "json": 1, "delay": 0}, timeout=15)
        out = {}
        for m in r.json().get("msgArray", []):
            last = _num(m.get("z")) or _num((m.get("b") or "").split("_")[0]) or _num(m.get("o"))
            prev = _num(m.get("y"))
            up, down = _num(m.get("u")), _num(m.get("w"))
            if not last or not prev:
                continue
            out[m["c"]] = {"price": last, "prev_close": prev, "chg_pct": round((last / prev - 1) * 100, 2),
                           "limit_up": up, "limit_down": down, "at_limit_up": bool(up and last >= up),
                           "at_limit_down": bool(down and last <= down), "time": m.get("t", ""), "date": m.get("d", "")}
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("即時行情失敗：%s", e)
        return {}


# ---------------------------------------------------------------- 負面新聞（LLM 判斷嚴重度，必須引用）
class RiskItem(BaseModel):
    code: str
    severity: int = Field(ge=-2, le=0, description="-2 重大利空（財務惡化、違約、弊案、重大訴訟、大客戶流失、大幅下修）、-1 一般利空、0 無影響")
    issue: str = Field(description="一句話說明利空內容，只能根據新聞，不可加入新聞沒有的數字")
    news_ids: list[int]


class RiskResult(BaseModel):
    items: list[RiskItem]


def news_risk(stocks: dict[str, str]) -> dict[str, list[dict]]:
    """{code: [{"severity","issue","news":[...], "multi_source"}]}（只列出 severity < 0）。"""
    if not stocks:
        return {}
    key = f"risk_{'_'.join(sorted(stocks))}_{now_tw().strftime('%Y%m%d%H')}{now_tw().minute // 30}"
    hit = _CACHE.get(key)
    if hit:
        return hit[1]
    with ThreadPoolExecutor(6) as ex:
        lists = list(ex.map(lambda kv: _news_titles(kv[0], kv[1], 6), stocks.items()))
    pool, idx = [], 1
    for (code, name), items in zip(stocks.items(), lists):
        for n in items:
            pool.append({"id": idx, "code": code, "name": name, **n})
            idx += 1
    out: dict[str, list[dict]] = {}
    client = get_client()
    if pool and client.available():
        lines = [f"[{n['id']}]（{n['code']} {n['name']}／{n['source']} {n['time']}）{n['title']}" for n in pool]
        try:
            res = client.structured(
                "以下是各股票近 7 日新聞標題。請找出『對股價不利的事件或基本面消息』，每則判斷都要引用新聞編號。\n"
                "算：營收／獲利衰退或下修、財報更正、虧損、違約、弊案、訴訟、處分、大股東申讓或減資、大客戶流失、產品問題、"
                "法人調降評等或目標價、被列注意／處置。\n"
                "不算：單純描述股價下跌或技術面走勢、每日法人買賣超統計、與該公司無關的同名新聞或廣告、利多新聞。"
                "若某檔沒有符合的新聞就不要列出。\n\n" + "\n".join(lines), RiskResult,
                system="你是嚴謹的風險控管分析師，只根據提供的新聞標題判斷，不可臆測或加入新聞以外的資訊。")
            by_id = {n["id"]: n for n in pool}
            for it in res.items:
                ids = [i for i in it.news_ids if i in by_id and by_id[i]["code"] == it.code]
                if it.severity >= 0 or not ids or it.code not in stocks:
                    continue
                if not verify(it.issue, [by_id[i] for i in ids])[0]:
                    continue
                srcs = {by_id[i]["source"] for i in ids}
                out.setdefault(it.code, []).append({
                    "severity": it.severity, "issue": it.issue, "multi_source": len(ids) >= 2 or len(srcs) >= 2,
                    "news": [{k: by_id[i][k] for k in ("title", "url", "source", "time")} for i in ids]})
        except LLMError as e:
            log.warning("負面新聞判斷失敗：%s", e)
    _CACHE[key] = (time.time(), out)
    return out


# ---------------------------------------------------------------- 規則：建議賣出股數
def _sell_plan(shares: int, frac: float, price: float, limit_down: float | None, etf: bool) -> tuple[int, float]:
    st = params.strategy()
    qty = sizer._round_sell(shares * frac, shares, st["allow_odd_lot"]) if frac < 1 else shares
    limit = sizer.round_tick(price * 0.99, etf)
    if limit_down:
        limit = max(limit, limit_down)
    return qty, limit


def holding_alerts(pf, meta: dict, quotes_rt: dict, closes: dict, feats: dict, att_lists: dict,
                   risks: dict) -> list[dict]:
    out = []
    for code, pos in pf.positions.items():
        m = stock_meta(code)
        rt = quotes_rt.get(code, {})
        price = rt.get("price") or closes.get(code)
        if not price:
            continue
        f = feats.get(code) or {}
        stop = (meta.get(code) or {}).get("stop")
        chg = rt.get("chg_pct")
        if chg is None and f:
            chg = round(f["ret_1d"] * 100, 2)
        reasons, frac, level = [], 0.0, None
        tighten = None

        def bump(lv: str, why: str, fr: float = 0.0):
            nonlocal level, frac
            reasons.append(why)
            frac = max(frac, fr)
            if level is None or LEVEL_ORDER[lv] < LEVEL_ORDER[level]:
                level = lv

        if rt.get("at_limit_down"):
            bump("嚴重", f"跌停（{price}，跌停價 {rt['limit_down']}）：跌停鎖住時通常賣不出去；若跌停打開，依下列建議賣出", 0.5)
        if stop and price <= stop:
            bump("嚴重", f"已跌破停損價 {stop}（目前 {price}）：依紀律全數出場", 1.0)
        elif stop and price <= stop * 1.03:
            bump("提醒", f"距停損價 {stop} 只剩 {(price / stop - 1) * 100:.1f}%，先掛好停損單")
        for rk in risks.get(code, []):
            below_ma = bool(f) and price < f.get("ma20", 0)
            if rk["severity"] == -2:
                fr = 1.0 if (below_ma or (chg is not None and chg <= -3)) else 0.5
                bump("嚴重", f"重大負面新聞：{rk['issue']}" + ("（多來源）" if rk["multi_source"] else "")
                     + ("；股價已跌破月線，全數出場" if fr == 1.0 else "；先減碼一半，保留部位觀察後續"), fr)
            else:
                weak = below_ma or (stop and price <= stop * 1.05)
                bump("警告", f"負面新聞：{rk['issue']}" + ("；股價偏弱，減碼三分之一" if weak else "；股價尚穩，先上調停損"),
                     1 / 3 if weak else 0.0)
                if not weak and f:
                    tighten = round(max(stop or 0, price - f["atr14"]), 2)
        if chg is not None and chg <= -5 and not rt.get("at_limit_down"):
            bump("警告", f"單日重挫 {chg:+.2f}%：先減碼三分之一，確認原因後再決定", 1 / 3)
        a = att_lists.get(code)
        if a:
            if a["type"] == "處置":
                bump("警告", f"列為處置股（{a['reason']}，{a['period']}）：改為分盤撮合、可能需預收款，流動性變差，減碼三分之一", 1 / 3)
            else:
                bump("提醒", f"列為注意股：{a['reason']}，交易異常，留意隔日是否轉處置")
        if f and f.get("inst_buy_streak", 0) == 0 and f.get("inst_net5_ratio", 0) < -0.05:
            bump("提醒", f"法人近 5 日賣超 {abs(f['inst_net5']) / 1000:,.0f} 張，籌碼轉弱")
        if rt.get("at_limit_up"):
            bump("提醒", f"漲停（{price}）：強勢續抱，依停利規則分批出場")
        if not reasons:
            continue
        qty, limit = (0, None)
        if frac > 0:
            qty, limit = _sell_plan(pos.shares, frac, price, rt.get("limit_down"), m.get("is_etf", False))
        action = (f"建議賣出 {sizer.fmt_shares(qty)}（持有 {pos.shares:,} 股的 {qty / pos.shares * 100:.0f}%），限價 {limit}"
                  if qty else ("停損上調至 " + str(tighten) if tighten else "續抱，持續觀察"))
        why_qty = None
        if qty:
            why_qty = ("觸發條件中最嚴重者決定賣出比例：" + ("全數出場" if frac >= 1 else f"{frac * 100:.0f}%")
                       + "；限價取最近價 × 0.99" + ("（不低於跌停價）" if rt.get("limit_down") else "") + "以提高成交機率")
        out.append({"scope": "持股", "level": level, "code": code, "name": m["name"], "title": f"{code} {m['name']}",
                    "price": price, "chg_pct": chg, "reasons": reasons, "action": action, "sell_shares": qty,
                    "limit_price": limit, "why_qty": why_qty, "new_stop": tighten,
                    "news": [n for rk in risks.get(code, []) for n in rk["news"]][:4]})
    return out


def order_alerts(orders: list[dict], att_lists: dict, risks: dict, quotes_rt: dict) -> list[dict]:
    """今日建議買進的標的：買進前檢查。"""
    out = []
    for o in orders:
        if o.get("action") != "BUY":
            continue
        code = o["code"]
        reasons, level, act = [], None, None
        for rk in risks.get(code, []):
            if rk["severity"] == -2:
                reasons.append(f"重大負面新聞：{rk['issue']}")
                level, act = "嚴重", "暫緩買進，等利空明朗"
            else:
                reasons.append(f"負面新聞：{rk['issue']}")
                level = level or "警告"
                act = act or f"減半買進（{sizer.fmt_shares(max(1, o['shares'] // 2))}）或等回測支撐"
        a = att_lists.get(code)
        if a and a["type"] == "處置":
            reasons.append(f"處置股（{a['reason']}）：流動性差、需預收款")
            level, act = "嚴重", "暫緩買進"
        elif a:
            reasons.append(f"注意股：{a['reason']}")
            level = level or "提醒"
        rt = quotes_rt.get(code, {})
        if rt.get("at_limit_up"):
            reasons.append(f"盤中已漲停（{rt['price']}）：追價風險高、可能買不到")
            level = level or "提醒"
            act = act or "不追價，維持原限價"
        if reasons:
            out.append({"scope": "買進前", "level": level, "code": code, "name": o["name"], "title": f"{code} {o['name']}",
                        "reasons": reasons, "action": act or "照原計畫，但留意", "news": [n for rk in risks.get(code, []) for n in rk["news"]][:3]})
    return out


def market_alerts(lim: dict, us_snapshot: list[dict], taiex_chg: float | None) -> list[dict]:
    out = []
    if taiex_chg is not None and abs(taiex_chg) >= 2:
        out.append({"scope": "大盤", "level": "警告", "title": f"加權指數單日 {taiex_chg:+.2f}%",
                    "reasons": ["大盤劇烈波動，系統性風險升高"], "action": "檢查持股率與停損，避免追高殺低"})
    us = {r["ticker"]: r for r in us_snapshot}
    if "^SOX" in us and us["^SOX"]["change_pct"] <= -3:
        out.append({"scope": "國際", "level": "警告", "title": f"費城半導體重挫 {us['^SOX']['change_pct']:+.2f}%",
                    "reasons": ["電子權值股開盤大概率承壓"], "action": "半導體／電子持股開盤先觀察，不急著加碼"})
    if "^VIX" in us and us["^VIX"]["change_pct"] >= 15:
        out.append({"scope": "國際", "level": "警告", "title": f"VIX 恐慌指數大漲 {us['^VIX']['change_pct']:+.2f}%",
                    "reasons": ["全球風險偏好下降"], "action": "提高穩健籃比例、確認停損"})
    if len(lim["down"]) >= 10:
        top = "、".join(f"{g['industry']} {g['count']} 檔" for g in lim["down_industries"][:3])
        out.append({"scope": "大盤", "level": "警告", "title": f"{len(lim['down'])} 檔跌停",
                    "reasons": [f"集中在：{top}"], "action": "同產業持股留意連動下跌"})
    for g in lim["up_industries"][:2]:
        if g["count"] >= 3:
            out.append({"scope": "題材", "level": "提醒", "title": f"{g['industry']} {g['count']} 檔漲停",
                        "reasons": ["同產業集體漲停：" + "、".join(g["names"][:6])], "action": "題材發酵中，但不追漲停"})
    return out


def sort_alerts(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda a: (LEVEL_ORDER.get(a["level"], 9), a["scope"] != "持股"))


def taiex_today() -> dict | None:
    """證交所最近交易日加權指數漲跌。"""

    def fetch():
        try:
            for x in requests.get("https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX", timeout=30).json():
                if x.get("指數") == "發行量加權股價指數":
                    pct = _num(x.get("漲跌百分比")) or 0.0
                    return {"close": _num(x.get("收盤指數")), "chg_pct": -pct if x.get("漲跌") == "-" else pct,
                            "date": x.get("日期")}
        except Exception as e:  # noqa: BLE001
            log.warning("加權指數失敗：%s", e)
        return None

    return _cached("taiex_today", 600, fetch)


def is_trading_hours() -> bool:
    from src.config import is_trading_day

    n = now_tw()
    return is_trading_day(n.date()) and (9, 0) <= (n.hour, n.minute) <= (13, 35)


def collect(pf, meta: dict, orders: list[dict], us_snapshot: list[dict], target: dt.date) -> dict:
    """彙整全部警示（每日報告與網頁即時查詢共用）。"""
    holdings = {c: stock_meta(c)["name"] for c in pf.positions}
    buys = {o["code"]: o["name"] for o in orders if o.get("action") == "BUY"}
    watch = {**holdings, **buys}
    rt = realtime(list(watch)) if watch else {}
    feats = {c: market_data.tw_features(c, target) for c in holdings}
    closes = {c: f["close"] for c, f in feats.items() if f}
    att = attention_lists()
    risks = news_risk(watch)
    quotes = market_quotes()
    lim = explain_limits(limit_moves(quotes))
    tx = taiex_today()
    items = (holding_alerts(pf, meta, rt, closes, feats, att, risks) + order_alerts(orders, att, risks, rt)
             + market_alerts(lim, us_snapshot, tx["chg_pct"] if tx else None))
    items = sort_alerts(items)
    slim = lambda q: {k: q.get(k) for k in ("code", "name", "market", "close", "chg_pct", "value", "industry",  # noqa: E731
                                            "peer_count", "reason", "news")}
    return {
        "generated_at": now_tw().strftime("%Y-%m-%d %H:%M"), "trading_now": is_trading_hours(),
        "quotes_date": _roc_date(quotes[0]["date"]) if quotes else None, "taiex": tx,
        "counts": {lv: sum(1 for a in items if a["level"] == lv) for lv in LEVEL_ORDER},
        "alerts": items,
        "limit_up": [slim(q) for q in lim["up"][:40]], "limit_down": [slim(q) for q in lim["down"][:40]],
        "limit_up_count": len(lim["up"]), "limit_down_count": len(lim["down"]),
        "up_industries": lim["up_industries"][:8], "down_industries": lim["down_industries"][:8],
        "attention_watch": [{"code": c, "name": watch[c], **att[c]} for c in watch if c in att],
        "attention_total": {"注意": sum(1 for v in att.values() if v["type"] == "注意"),
                            "處置": sum(1 for v in att.values() if v["type"] == "處置")},
        "realtime": rt,
    }
