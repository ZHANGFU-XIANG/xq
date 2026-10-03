"""MopsAgent：公開資訊觀測站（MOPS）公司資料。

來源：
- mops.twse.com.tw 新版網站 API：t146sb05（公司概況：基本資料、財報摘要、月營收、重大訊息、內部人持股、股利）、
  t05st03（基本資料含完整主要經營業務）、t05st01_detail（重大訊息全文）
- FinMind TaiwanStockFinancialStatements：近 8 季損益（營收、毛利率、營益率、稅後淨利、EPS）
全部為官方公告數字，不經 LLM。觀測站金額單位為新台幣仟元。
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd
import requests

from src.agents.market_data import _finmind

log = logging.getLogger(__name__)
API = "https://mops.twse.com.tw/mops/api/"
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "Origin": "https://mops.twse.com.tw",
           "Referer": "https://mops.twse.com.tw/mops/"}
_CACHE: dict[str, tuple[float, object]] = {}


def _post(api: str, body: dict, ttl: float = 12 * 3600):
    key = api + str(sorted(body.items()))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    for attempt in range(2):
        try:
            r = requests.post(API + api, json=body, headers=HEADERS, timeout=30)
            j = r.json()
            if j.get("code") == 200:
                _CACHE[key] = (time.time(), j["result"])
                return j["result"]
            log.info("MOPS %s %s：%s", api, body, j.get("message"))
            break
        except Exception as e:  # noqa: BLE001
            log.warning("MOPS %s 失敗(%d)：%s", api, attempt, e)
            time.sleep(2)
    return None


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _val(d: dict, k: str):
    v = d.get(k)
    if isinstance(v, dict):
        return None if v.get("isHidden") else v.get("value")
    return v


def roc_to_iso(s: str) -> str:
    """115/09/29 → 2026-09-29"""
    try:
        y, m, d = str(s).split("/")
        return f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return str(s)


def overview(code: str) -> dict:
    """公司概況：基本資料、主要業務、財報摘要（近期各期）、月營收、重大訊息、內部人持股、股利。"""
    r = _post("t146sb05", {"companyId": code})
    if not r:
        return {"code": code, "error": "公開資訊觀測站查無資料（ETF 或已下市股票沒有公司資料）"}
    b = r.get("basic_info") or {}
    full = _post("t05st03", {"companyId": code}, ttl=7 * 86400) or {}
    basic = {
        "name": _val(full, "companyName") or b.get("companyName"),
        "abbr": _val(full, "companyAbbreviation"),
        "industry": _val(full, "industryCategory") or b.get("industryCategory"),
        "market": _val(full, "marketName"),
        "chairman": _val(full, "chairman") or b.get("chairman"),
        "president": _val(full, "president"),
        "established": roc_to_iso(_val(full, "establishDate") or ""),
        "listed": roc_to_iso(_val(full, "listingDate") or _val(full, "OTCDate") or ""),
        "capital": _val(full, "capitalAmount") or b.get("capitalAmount"),
        "shares": _val(full, "commonStockAmount"),
        "website": _val(full, "internetAddress") or b.get("internetAddress"),
        "main_business": (_val(full, "mainBusiness") or b.get("mainBusiness") or "").strip(),
        "auditor": _val(full, "accountingOffice"),
        "dividend_frequency": _val(full, "commonStockDistribute"),
        "address": _val(full, "address") or b.get("address"),
    }
    fr = r.get("financial_report_information") or {}
    periods = [t["main"] for t in (fr.get("titles") or [])[1:]]

    def table(rows):
        return [{"item": row[0], "values": [_num(v) for v in row[1:]], "raw": row[1:]} for row in rows or []]

    financial = {"periods": periods, "income": table(fr.get("CCSI")), "balance": table(fr.get("CAL")),
                 "cashflow": table(fr.get("CCFS")), "unit": "新台幣仟元（每股項目為元）",
                 "report_url": fr.get("moreInfoUrl")}
    revenue = []
    for blk in (r.get("revenue_information") or {}).get("revenueInformation", []):
        t = [x["main"] for x in blk.get("titles", [])]
        d = blk.get("data", [])
        if len(t) == 3 and len(d) == 3:
            revenue.append({"label": t[0], "value": _num(d[0]), "compare_label": t[1], "compare": _num(d[1]),
                            "growth_pct": _num(d[2])})
    news = []
    for row in (r.get("recent_important_news") or {}).get("data", [])[:15]:
        if len(row) >= 3:
            news.append({"date": roc_to_iso(row[0]), "title": " ".join(str(row[1]).split()),
                         "params": (row[2] or {}).get("parameters")})
    ins = r.get("changes_insider_ownership") or {}
    insider = [{"period": row[0], "lots": row[1], "change_pct": row[2], "ratio": row[3]} for row in ins.get("data", [])]
    dv = r.get("recent_dividend_distributions") or {}
    dividend = {k: dv.get(k) for k in ("dividendBelongsYear", "resolutionProgress", "boardResolutionDate",
                                       "cashDividendPaymentDate", "exDividendTradingDate", "cashDividend",
                                       "stockDividend") if dv.get(k)}
    for k, v in dv.items():  # 欄位名稱可能隨期別不同，保留與現金股利相關的欄位
        if "cash" in k.lower() and "dividend" in k.lower() and v and k not in dividend:
            dividend[k] = v
    return {"code": code, "basic": basic, "financial": financial, "revenue": revenue, "news": news,
            "insider": insider, "dividend": dividend,
            "mops_url": f"https://mops.twse.com.tw/mops/#/web/t146sb05?companyId={code}"}


def news_detail(params: dict) -> dict:
    """重大訊息全文。params 來自 overview()['news'][i]['params']。"""
    r = _post("t05st01_detail", params, ttl=7 * 86400)
    if not r or not r.get("data"):
        return {"error": "查無重大訊息內容"}
    row = r["data"][0]
    return {"date": roc_to_iso(row[1]), "time": row[2], "title": " ".join(str(row[6]).split()), "clause": row[7].strip(),
            "fact_date": roc_to_iso(row[8]), "description": str(row[9]).strip()}


def quarterly(code: str, n: int = 8) -> list[dict]:
    """近 n 季損益：營收、毛利率、營益率、稅後淨利、EPS 與年增率（FinMind，來源為公開資訊觀測站財報）。"""
    key = f"q_{code}_{dt.date.today()}"
    hit = _CACHE.get(key)
    if hit:
        return hit[1]
    df = _finmind("TaiwanStockFinancialStatements", code, (dt.date.today() - dt.timedelta(days=365 * 3 + 60)).isoformat())
    out: list[dict] = []
    if not df.empty:
        p = df.pivot_table(index="date", columns="type", values="value", aggfunc="first").sort_index()
        p.index = pd.to_datetime(p.index)
        for d, row in p.iterrows():
            rev = row.get("Revenue")
            if rev is None or pd.isna(rev) or rev == 0:
                continue
            gp, op, ni, eps = (row.get(k) for k in ("GrossProfit", "OperatingIncome", "IncomeAfterTaxes", "EPS"))
            f = lambda v, scale=1.0: round(float(v) / scale, 2) if v is not None and pd.notna(v) else None  # noqa: E731
            out.append({"quarter": f"{d.year}Q{(d.month - 1) // 3 + 1}", "date": d.date().isoformat(),
                        "revenue_100m": f(rev, 1e8),
                        "gross_margin": round(float(gp) / float(rev) * 100, 2) if pd.notna(gp) else None,
                        "op_margin": round(float(op) / float(rev) * 100, 2) if pd.notna(op) else None,
                        "net_income_100m": f(ni, 1e8), "eps": f(eps)})
        for i, q in enumerate(out):  # 年增率：與 4 季前比較
            if i >= 4:
                prev = out[i - 4]
                q["revenue_yoy"] = round((q["revenue_100m"] / prev["revenue_100m"] - 1) * 100, 2) if prev["revenue_100m"] else None
                q["eps_yoy"] = round(q["eps"] - prev["eps"], 2) if q["eps"] is not None and prev["eps"] is not None else None
        out = out[-n:]
    _CACHE[key] = (time.time(), out)
    return out


def material_news(code: str, days: int = 14) -> list[dict]:
    """近期重大訊息（給警示用）：標題、日期、連結。"""
    ov = overview(code)
    if ov.get("error"):
        return []
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return [{"title": n["title"], "date": n["date"], "source": "公開資訊觀測站", "time": n["date"][5:],
             "url": ov["mops_url"]} for n in ov["news"] if n["date"] >= cutoff]


def company(code: str) -> dict:
    """前端與 AI 助理用：概況＋近 8 季損益。"""
    ov = overview(code)
    if not ov.get("error"):
        ov["quarterly"] = quarterly(code)
    return ov
